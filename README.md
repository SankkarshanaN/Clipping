# Clipping

Turns a long video (a YouTube VOD or a local file) into 9:16 YouTube Shorts with burned-in captions.

Pipeline:

1. **yt-dlp** downloads the video (or just a section of it).
2. **faster-whisper** transcribes it with word timestamps.
3. The **`claude` CLI** (`claude -p`) reads the transcript in 10-minute windows and picks the funniest or most intense moments, as start/end times.
4. **ffmpeg** cuts each moment, crops it to 1080x1920, and burns in yellow word-pop captions.

Output goes to `live_clips_session/clips/`.

## Requirements

- Windows 10/11 (the commands below are PowerShell; the script itself is cross-platform)
- Python 3.12 (3.11 and 3.13 should also work)
- **ffmpeg** with `ffmpeg` and `ffprobe` on PATH
- **Claude Code CLI** (`claude`) on PATH, logged in to your account
- About 2 GB of disk space for the Whisper `small` model and temp files, plus room for the clips

Install ffmpeg (once):

```powershell
winget install Gyan.FFmpeg
```

Open a new terminal afterwards so PATH updates.

## Setup (once)

```powershell
git clone https://github.com/SankkarshanaN/Clipping.git
cd Clipping
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

The `requirements.txt` pins `av==18.1.0` on purpose. PyAV 19.x removed an argument that faster-whisper 1.2.1 uses, and transcription crashes with `unexpected keyword argument 'metadata_errors'`. Don't upgrade `av` unless you also upgrade faster-whisper.

## Usage

Every time you open a new terminal:

```powershell
cd Clipping
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks the activate script, skip activation and call the venv's Python directly. The commands below work either way when you prefix them with `.\.venv\Scripts\python`.

Make clips from a YouTube video:

```powershell
python clipper.py vod "https://www.youtube.com/watch?v=VIDEO_ID"
```

Make clips from only part of a long video (faster, and less to download):

```powershell
python clipper.py vod "https://www.youtube.com/watch?v=VIDEO_ID" --section 00:05:00-00:25:00
```

Make clips from a file you already have:

```powershell
python clipper.py vod "C:\path\to\video.mp4"
```

Options:

| Option | Default | What it does |
|---|---|---|
| `--section START-END` | whole video | Only downloads and processes this part (`HH:MM:SS-HH:MM:SS`). |
| `--layout crop` or `blur` | `crop` | `crop` fills the frame by cropping the centre. `blur` keeps the whole frame and puts a blurred copy behind it. |
| `--model` | `small` | Whisper model: `base` (fastest, least accurate), `small` (default), `medium` (slowest, most accurate). |

Each clip is saved as `<video-id>_NN_<title>_9x16.mp4`. The `.srt` files next to them are the caption files used for that clip.

## Expected results

On a 19-minute video, CPU only, with the default `small` model:

- transcription: about 6 minutes
- Claude picking clips: about 1 minute
- rendering: about 20 seconds per clip
- total: about 12 minutes (the first run is longer because it downloads the Whisper model)

## Checking a clip

Every clip should be 1080x1920, 20 to 58 seconds long, and have an audio track:

```powershell
ffprobe -v error -show_entries stream=codec_type,width,height -show_entries format=duration -of default=nw=1 "live_clips_session\clips\CLIP.mp4"
```

## Troubleshooting

- **`ffmpeg` or `yt-dlp` not found:** open a new terminal after installing ffmpeg. The venv installs yt-dlp, so run commands from the venv (`.\.venv\Scripts\python`).
- **`claude` CLI not found:** make sure `claude` runs in a fresh terminal and that you're logged in.
- **`metadata_errors` TypeError:** run `.\.venv\Scripts\python -m pip install -r requirements.txt` again to reinstall the pinned `av`.
- **`Invalid JSON from Claude` warnings:** Claude returned text instead of JSON for one 10-minute window. That window is skipped. Other windows are unaffected.
- **Symlink warnings from Hugging Face on Windows:** harmless. The model still downloads and caches, just with more disk use.

## Contributing

1. Make a branch: `git checkout -b my-change`
2. Make your change and test it on a short `--section`.
3. Commit and push the branch, then open a pull request into `main`.

Don't commit `.venv/`, `downloads/` or `live_clips_session/`. They're in `.gitignore`.
