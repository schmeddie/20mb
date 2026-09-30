#!/usr/bin/env python3
"""Compress a video so it fits under a target file size (default 20 MB).

The bitrate is worked out from the video's length so the output lands just
under the target, and the resolution is lowered as far as needed to keep that
bitrate looking reasonable. Long videos end up small (down to 144p).

Usage:
    python compress.py input.mp4                 # -> input_20mb.mp4
    python compress.py input.mov -o out.mp4 -t 25
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

# Standard heights to step down through, largest first.
HEIGHT_LADDER = [2160, 1440, 1080, 720, 540, 480, 360, 240, 180, 144]

# Minimum bits per pixel per frame for x264 to look acceptable. Lower than
# this and we drop to the next resolution down.
MIN_BPP = 0.06

# Leave room for the MP4 container and bitrate wobble.
OVERHEAD = 0.96

MIN_VIDEO_KBPS = 25
MAX_ATTEMPTS = 5


class CompressError(Exception):
    pass


def require_ffmpeg():
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise CompressError(
                f"'{tool}' was not found. Install ffmpeg (https://ffmpeg.org/download.html) "
                "and make sure it is on your PATH."
            )


def probe(path):
    """Return duration (s), width, height, fps and whether there is audio."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", path],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise CompressError(f"Could not read video: {out.stderr.strip()}")
    info = json.loads(out.stdout)

    video = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    if video is None:
        raise CompressError("No video stream found in file.")
    has_audio = any(s.get("codec_type") == "audio" for s in info["streams"])

    duration = float(info["format"].get("duration") or video.get("duration") or 0)
    if duration <= 0:
        raise CompressError("Could not determine video duration.")

    width, height = int(video["width"]), int(video["height"])
    # Phone videos often store rotation as metadata; swap so we scale correctly.
    rotation = 0
    for sd in video.get("side_data_list", []):
        if "rotation" in sd:
            rotation = abs(int(sd["rotation"]))
    if rotation in (90, 270):
        width, height = height, width

    fps = 30.0
    rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "30/1"
    try:
        num, den = rate.split("/")
        if float(den) > 0 and float(num) > 0:
            fps = float(num) / float(den)
    except ValueError:
        pass

    return duration, width, height, fps, has_audio


def pick_audio_kbps(total_kbps):
    for kbps in (128, 96, 64, 48, 32):
        if kbps <= total_kbps * 0.12:
            return kbps
    return 24


def plan(total_kbps, width, height, fps, has_audio):
    """Pick audio bitrate, video bitrate, output height and fps."""
    audio_kbps = pick_audio_kbps(total_kbps) if has_audio else 0
    video_kbps = total_kbps - audio_kbps
    if video_kbps < MIN_VIDEO_KBPS:
        raise CompressError(
            "Video is too long to fit in the target size at any usable quality "
            f"(only {total_kbps:.0f} kbps available)."
        )

    short_side = min(width, height)
    candidates = [h for h in HEIGHT_LADDER if h <= short_side] or [short_side]
    if short_side not in HEIGHT_LADDER and short_side > candidates[0]:
        candidates.insert(0, short_side)

    def bpp(short, frame_rate):
        long_side = short * max(width, height) / short_side
        return video_kbps * 1000 / (short * long_side * frame_rate)

    # High frame rates eat bitrate; cap at 30 when things get tight.
    out_fps = fps
    if fps > 31 and bpp(candidates[0], fps) < MIN_BPP * 2:
        out_fps = 30

    chosen = candidates[-1]
    for short in candidates:
        if bpp(short, out_fps) >= MIN_BPP:
            chosen = short
            break
    return audio_kbps, video_kbps, chosen, out_fps


def scale_filter(short, width, height):
    # Scale the shorter side to `short`, keep aspect, force even dimensions.
    if width >= height:
        return f"scale=-2:{short}"
    return f"scale={short}:-2"


def run_ffmpeg(args, duration, on_progress=None, base=0.0, span=1.0):
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-progress", "pipe:1", "-nostats"] + args
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in proc.stdout:
        if on_progress and line.startswith("out_time_us="):
            try:
                t = int(line.split("=", 1)[1]) / 1_000_000
            except ValueError:
                continue
            on_progress(base + span * min(max(t / duration, 0), 1))
    err = proc.stderr.read()
    if proc.wait() != 0:
        raise CompressError(f"ffmpeg failed: {err.strip()[-800:]}")


def encode(src, dst, duration, width, height, audio_kbps, video_kbps, short, fps,
           has_audio, workdir, on_progress=None, base=0.0, span=1.0):
    vf = scale_filter(short, width, height) + f",fps={fps:.3f},format=yuv420p"
    passlog = os.path.join(workdir, "pass")
    common = ["-i", src, "-map", "0:v:0", "-vf", vf,
              "-c:v", "libx264", "-preset", "slow", "-b:v", f"{video_kbps:.0f}k",
              "-passlogfile", passlog]

    run_ffmpeg(common + ["-pass", "1", "-an", "-f", "mp4", os.devnull],
               duration, on_progress, base, span / 2)

    audio = ["-map", "0:a:0", "-c:a", "aac", "-b:a", f"{audio_kbps}k", "-ac", "2"] if has_audio else ["-an"]
    run_ffmpeg(common + ["-pass", "2"] + audio + ["-movflags", "+faststart", dst],
               duration, on_progress, base + span / 2, span / 2)


def compress(src, dst, target_mb=20.0, on_progress=None, log=print):
    """Compress `src` into `dst` so it is at most `target_mb` megabytes.

    `on_progress` is called with a float 0..1. Returns the output size in bytes.
    """
    require_ffmpeg()
    limit = int(target_mb * 1_000_000)  # MB, not MiB, so it's safe either way.

    if os.path.getsize(src) <= limit:
        log("Already under the target size, copying as-is.")
        shutil.copyfile(src, dst)
        if on_progress:
            on_progress(1.0)
        return os.path.getsize(dst)

    duration, width, height, fps, has_audio = probe(src)
    log(f"Input: {width}x{height} @ {fps:.2f} fps, {duration:.1f}s, "
        f"{os.path.getsize(src) / 1e6:.1f} MB")

    total_kbps = limit * 8 / duration / 1000 * OVERHEAD
    best = None
    workdir = tempfile.mkdtemp(prefix="20mb_")
    try:
        for attempt in range(MAX_ATTEMPTS):
            audio_kbps, video_kbps, short, out_fps = plan(total_kbps, width, height, fps, has_audio)
            log(f"Attempt {attempt + 1}: {short}p @ {out_fps:.0f} fps, "
                f"video {video_kbps:.0f} kbps, audio {audio_kbps} kbps")

            tmp_out = os.path.join(workdir, f"try{attempt}.mp4")
            encode(src, tmp_out, duration, width, height, audio_kbps, video_kbps, short,
                   out_fps, has_audio, workdir, on_progress)
            size = os.path.getsize(tmp_out)
            log(f"  -> {size / 1e6:.2f} MB")

            if size <= limit:
                if best is None or size > best[1]:
                    best = (tmp_out, size)
                # Close enough to the target: done.
                if size >= limit * 0.93:
                    break
            # Scale the bitrate by how far off we were and try again.
            total_kbps *= (limit * 0.97) / size

        if best is None:
            raise CompressError("Could not get the video under the target size.")
        shutil.move(best[0], dst)
        if on_progress:
            on_progress(1.0)
        return best[1]
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="Compress a video to fit under a file size limit.")
    parser.add_argument("input", help="video file to compress")
    parser.add_argument("-o", "--output", help="output file (default: <input>_<size>mb.mp4)")
    parser.add_argument("-t", "--target", type=float, default=20.0, help="target size in MB (default 20)")
    args = parser.parse_args()

    out = args.output or f"{os.path.splitext(args.input)[0]}_{args.target:g}mb.mp4"

    interactive = sys.stderr.isatty()

    def progress(p):
        if interactive:
            print(f"\r  {p * 100:5.1f}%", end="", file=sys.stderr, flush=True)

    def log(msg):
        if interactive:
            print("\r" + " " * 10 + "\r", end="", file=sys.stderr, flush=True)
        print(msg, flush=True)

    try:
        size = compress(args.input, out, args.target, progress, log)
    except CompressError as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"\nDone: {out} ({size / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()
