"""
clipper.py - turn long videos into 9:16 YouTube Shorts with burned-in captions.

  python clipper.py vod <url-or-file> [--section 00:05:00-00:25:00] [--layout crop|blur]
  python clipper.py watch              # process chunk_*.mp4 files as they appear

Needs: ffmpeg, yt-dlp (for URLs), faster-whisper, and the `claude` CLI on PATH.
Clips land in ./live_clips_session/clips
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

WATCH_DIR = os.path.abspath("./live_clips_session")
CLIPS_DIR = os.path.join(WATCH_DIR, "clips")
DOWNLOAD_DIR = os.path.abspath("./downloads")
PROCESSED_FILE = os.path.join(WATCH_DIR, "processed.json")

MIN_CLIP = 20.0   # seconds
MAX_CLIP = 58.0   # hard cap so it uploads as a Short
WINDOW = 600.0    # transcript seconds sent to Claude per call
OVERLAP = 60.0    # window overlap so edge moments aren't lost

# Describe the content here to get better picks, e.g. "GTA gameplay with commentary"
CONTENT_HINT = ""

_model = None


def log(msg):
    print(msg, flush=True)


# ---------- download ----------

def download(url, section=None):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    suffix = "_" + re.sub(r"[^0-9]", "", section) if section else ""
    cmd = [
        "yt-dlp",
        "-f", "bv*[height<=1080]+ba/b",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "-o", os.path.join(DOWNLOAD_DIR, f"%(id)s{suffix}.%(ext)s"),
        "--print", "after_move:filepath",
    ]
    if section:
        cmd += ["--download-sections", f"*{section}", "--force-keyframes-at-cuts"]
    cmd.append(url)
    log(f"[Downloading] {url}")
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    lines = [l.strip() for l in res.stdout.splitlines() if l.strip()]
    if res.returncode != 0 or not lines or not os.path.exists(lines[-1]):
        sys.exit("[Error] yt-dlp failed:\n" + res.stderr[-1500:])
    return lines[-1]


# ---------- transcription ----------

def transcribe(path, model_name):
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        log(f"[Loading Whisper '{model_name}'] first run downloads the model")
        _model = WhisperModel(model_name, device="cpu", compute_type="int8")
    log(f"[Transcribing] {path}")
    t0 = time.time()
    segments_gen, info = _model.transcribe(path, word_timestamps=True, vad_filter=True)
    segments = []
    for s in segments_gen:
        segments.append(s)
        if len(segments) % 50 == 0:
            log(f"  ...{s.end / 60:.1f} of {info.duration / 60:.1f} min")
    log(f"[Transcribed] {len(segments)} segments in {time.time() - t0:.0f}s")
    return segments, info.duration


# ---------- highlight picking ----------

def ask_claude(transcript_text):
    claude = shutil.which("claude")
    if not claude:
        sys.exit("[Error] `claude` CLI not found on PATH.")
    hint = f"The video is: {CONTENT_HINT}. " if CONTENT_HINT else ""
    prompt = (
        "You are a viral YouTube Shorts editor. " + hint +
        "Find the top 1 or 2 funniest, most intense, or highest-retention moments in "
        "this transcript segment. Each clip MUST be between 25 and 58 seconds long, "
        "start at the beginning of a sentence and end on a finished thought. "
        "If nothing is worth clipping, return []. "
        "Return ONLY a valid JSON array of objects with keys: 'start' (float seconds), "
        "'end' (float seconds), 'title' (short lowercase slug using letters, digits and "
        "hyphens). Timestamps are the ones shown in the transcript. "
        "Output raw JSON only, no markdown fences or commentary.\n\n"
        f"Transcript:\n{transcript_text}"
    )
    try:
        # Prompt goes through stdin: avoids Windows command-line length limits.
        res = subprocess.run(
            [claude, "-p", "Follow the instructions given on stdin exactly."],
            input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300,
        )
    except subprocess.TimeoutExpired:
        log("[Warn] Claude timed out on this window, skipping it")
        return []
    raw = res.stdout.strip()
    a, b = raw.find("["), raw.rfind("]") + 1
    if a == -1 or b == 0:
        log(f"[Warn] No JSON from Claude, skipping window. Output began: {raw[:200]!r}")
        return []
    try:
        data = json.loads(raw[a:b])
    except json.JSONDecodeError:
        log(f"[Warn] Invalid JSON from Claude, skipping window: {raw[a:b][:200]!r}")
        return []
    return [d for d in data if isinstance(d, dict)]


def pick_highlights(segments, duration):
    clips = []
    win_start = 0.0
    while win_start < duration:
        win_end = win_start + WINDOW
        lines = [
            f"[{s.start:.1f} -> {s.end:.1f}] {s.text.strip()}"
            for s in segments if s.end > win_start and s.start < win_end
        ]
        if lines:
            log(f"[Asking Claude] {win_start / 60:.0f}-{min(win_end, duration) / 60:.0f} min")
            for c in ask_claude("\n".join(lines)):
                try:
                    start = max(0.0, float(c["start"]))
                    end = min(float(c["end"]), start + MAX_CLIP, duration)
                except (KeyError, TypeError, ValueError):
                    continue
                if end - start < MIN_CLIP:
                    continue
                # Windows overlap, so drop a clip that mostly repeats one we have.
                dup = any(
                    min(end, k["end"]) - max(start, k["start"]) > 0.5 * (end - start)
                    for k in clips
                )
                if not dup:
                    clips.append({"start": start, "end": end,
                                  "title": str(c.get("title") or "clip")})
        if win_end >= duration:
            break
        win_start = win_end - OVERLAP
    return sorted(clips, key=lambda k: k["start"])


# ---------- captions + render ----------

def srt_time(seconds):
    ms_total = int(round(seconds * 1000))
    h, rem = divmod(ms_total, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_word_pop_srt(segments, start, end, srt_path, words_per_caption=3):
    """Writes the SRT and returns how many words fell inside the clip."""
    words = [
        w for seg in segments if seg.words
        for w in seg.words if w.end > start and w.start < end
    ]
    length = end - start
    with open(srt_path, "w", encoding="utf-8") as f:
        idx = 1
        for i in range(0, len(words), words_per_caption):
            group = words[i:i + words_per_caption]
            s = max(0.0, group[0].start - start)
            e = min(length, group[-1].end - start)
            if e <= s:
                e = s + 0.2
            text = " ".join(w.word.strip().upper() for w in group)
            f.write(f"{idx}\n{srt_time(s)} --> {srt_time(e)}\n{text}\n\n")
            idx += 1
    return len(words)


STYLE = ("FontName=Arial,FontSize=18,Bold=1,PrimaryColour=&H0000FFFF,"
         "OutlineColour=&H00000000,BorderStyle=1,Outline=3,Shadow=1,"
         "Alignment=2,MarginV=75")


def render(src, start, end, srt_name, out_name, layout):
    """Runs ffmpeg inside CLIPS_DIR so the subtitles filter gets a plain filename
    (no drive letters or backslashes to escape)."""
    subs = f"subtitles={srt_name}:force_style='{STYLE}'"
    if layout == "blur":
        graph = (
            "[0:v]split[a][b];"
            "[a]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,boxblur=20:5[bg];"
            "[b]scale=1080:-2[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,{subs}[v]"
        )
    else:
        graph = f"[0:v]crop=ih*9/16:ih,scale=1080:1920,{subs}[v]"
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
        "-i", os.path.abspath(src),
        "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
        out_name,
    ]
    res = subprocess.run(cmd, cwd=CLIPS_DIR, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    out_path = os.path.join(CLIPS_DIR, out_name)
    if res.returncode != 0 or not os.path.exists(out_path):
        log(f"[ffmpeg failed] {out_name}\n{res.stderr[-800:]}")
        return None
    return out_path


def make_clips(src, segments, highlights, layout):
    os.makedirs(CLIPS_DIR, exist_ok=True)
    base = re.sub(r"[^A-Za-z0-9_-]", "_", os.path.splitext(os.path.basename(src))[0])[:40]
    made = []
    for i, clip in enumerate(highlights, 1):
        slug = re.sub(r"[^A-Za-z0-9_-]", "_", clip["title"])[:50] or "clip"
        stem = f"{base}_{i:02d}_{slug}"
        n_words = build_word_pop_srt(segments, clip["start"], clip["end"],
                                     os.path.join(CLIPS_DIR, stem + ".srt"))
        if n_words == 0:
            log(f"[Skip] {stem}: no speech in this range")
            os.remove(os.path.join(CLIPS_DIR, stem + ".srt"))
            continue
        out = render(src, clip["start"], clip["end"], stem + ".srt",
                     stem + "_9x16.mp4", layout)
        if out:
            log(f"[YT_SHORT_READY] {out}  "
                f"({clip['start']:.0f}s-{clip['end']:.0f}s of source)")
            made.append(out)
    return made


# ---------- modes ----------

def run_vod(args):
    src = args.source
    if re.match(r"^https?://", src):
        src = download(src, args.section)
    elif not os.path.exists(src):
        sys.exit(f"[Error] File not found: {src}")
    t0 = time.time()
    segments, duration = transcribe(src, args.model)
    if not segments:
        sys.exit("[Error] No speech found in the video.")
    highlights = pick_highlights(segments, duration)
    log(f"[Highlights] {len(highlights)} picked")
    made = make_clips(src, segments, highlights, args.layout)
    log(f"[Done] {len(made)} clips in {time.time() - t0:.0f}s -> {CLIPS_DIR}")


def load_processed():
    try:
        with open(PROCESSED_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def run_watch(args):
    os.makedirs(CLIPS_DIR, exist_ok=True)
    processed = load_processed()
    log(f"Watching {WATCH_DIR} for completed chunk_*.mp4 files...")
    while True:
        chunks = sorted(glob.glob(os.path.join(WATCH_DIR, "chunk_*.mp4")))
        # Skip the newest chunk: the recorder is still writing to it.
        for c in chunks[:-1]:
            name = os.path.basename(c)
            if name in processed:
                continue
            try:
                segments, duration = transcribe(c, args.model)
                if segments:
                    make_clips(c, segments, pick_highlights(segments, duration), args.layout)
            except Exception as e:
                log(f"[Error] {name}: {e}")
            # Mark done even on failure so a bad chunk is never retried in a loop.
            processed.add(name)
            with open(PROCESSED_FILE, "w", encoding="utf-8") as f:
                json.dump(sorted(processed), f)
        time.sleep(5)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Long video -> 9:16 Shorts")
    sub = p.add_subparsers(dest="mode", required=True)
    for name in ("vod", "watch"):
        sp = sub.add_parser(name)
        if name == "vod":
            sp.add_argument("source", help="video URL or local file")
            sp.add_argument("--section", help="download only this part, e.g. 00:05:00-00:25:00")
        sp.add_argument("--layout", choices=["crop", "blur"], default="crop")
        sp.add_argument("--model", default="small", help="Whisper model: base, small, medium")
    a = p.parse_args()
    run_vod(a) if a.mode == "vod" else run_watch(a)