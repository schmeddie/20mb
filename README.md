# 20mb

Compress any video so it fits under 20 MB (or any size you choose) for uploading to Discord.

It works out the bitrate from the video's length so the output lands just under the limit
(typically 93–98% of it), then lowers the resolution (1080p → 720p → 480p → … → 144p) and
caps high frame rates at 30 fps as far as needed to keep that bitrate looking decent.
Long videos get small; a 5-minute 1080p60 clip comes out at 360p, a 30-minute one at 144p.

Uses two-pass H.264 + AAC in an MP4, so Discord plays it inline.

## Setup

1. Install [ffmpeg](https://ffmpeg.org/download.html) (`ffmpeg` and `ffprobe` must be on your PATH).
   - Windows: `winget install ffmpeg` · macOS: `brew install ffmpeg` · Linux: `sudo apt install ffmpeg`
2. `pip install -r requirements.txt` (only needed for the web UI)

## Web UI (upload a file)

```
python app.py
```

Open http://localhost:5000, drop a video in, and download the result.

## Command line

```
python compress.py video.mp4              # -> video_20mb.mp4
python compress.py video.mov -t 10        # 10 MB target
python compress.py video.mkv -o small.mp4
```

"MB" means 1,000,000 bytes, so a 20 MB output is safely under Discord's limit either way.
Files already under the target are copied unchanged. Videos too long to reach the target at any
usable quality (roughly over 45 minutes for 20 MB) are rejected with an error.
