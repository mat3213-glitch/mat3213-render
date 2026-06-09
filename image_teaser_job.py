#!/usr/bin/env python3
"""
image_teaser_job.py — GitHub Actions runner: статичный арт → тизер-клип.

Делает на раннере (не на буке):
  1. Скачивает job.json + track.mp3 + image.png из ЯД (render_jobs/<JOB_ID>/)
  2. Ken Burns (медленный зум) по картинке в нужном формате
  3. Накладывает хук-текст (если задан) с фоновой плашкой и fade-in
  4. Подмешивает сегмент трека (audio_start..+duration) с afade
  5. Загружает result + status.txt обратно в ЯД

job.json:
  {"duration": 30, "format": "square|vertical|landscape",
   "out_name": "name.mp4", "hook_text": "...", "audio_start": 60}

Environment: JOB_ID
"""

import json
import os
import subprocess
import sys
from pathlib import Path

JOB_ID = os.environ.get("JOB_ID", "")
if not JOB_ID:
    sys.exit("JOB_ID not set")

REMOTE  = "ydrive"
JOBS_YD = "Content factory/render_jobs"
JOB_YD  = f"{JOBS_YD}/{JOB_ID}"
WORKDIR = Path("/tmp/image_teaser_job")
WORKDIR.mkdir(parents=True, exist_ok=True)

FMT_DIMS = {
    "square":    (1080, 1080),
    "vertical":  (1080, 1920),
    "landscape": (1920, 1080),
}

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def yd_get(remote_path: str, local: Path) -> bool:
    local.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["rclone", "copyto", f"{REMOTE}:{remote_path}", str(local)],
                       capture_output=True, text=True)
    return r.returncode == 0

def yd_put(local: Path, remote_path: str) -> bool:
    r = subprocess.run(["rclone", "copyto", str(local), f"{REMOTE}:{remote_path}"],
                       capture_output=True, text=True)
    return r.returncode == 0

def yd_put_text(text: str, remote_path: str):
    tmp = WORKDIR / "_status.txt"
    tmp.write_text(text)
    yd_put(tmp, remote_path)


def build_filter(W: int, H: int, dur: float, frames: int, has_text: bool) -> str:
    # Cover+crop к 3x целевого, затем zoompan вниз к WxH с медленным зумом.
    bigW, bigH = W * 3, H * 3
    vf = (
        f"scale={bigW}:{bigH}:force_original_aspect_ratio=increase,"
        f"crop={bigW}:{bigH},"
        f"zoompan=z='min(zoom+0.0005,1.25)':d={frames}:"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"fps=25:s={W}x{H},"
        f"format=yuv420p"
    )
    if has_text:
        # Размер от ШИРИНЫ, иначе в вертикали (H большое) текст шире кадра и срезается.
        fontsize = int(W * 0.052)
        # textfile с реальными переносами строк → многострочный хук; fade-in с t=0.8
        vf += (
            f",drawtext=fontfile={FONT}:textfile={WORKDIR}/hook.txt:"
            f"fontcolor=white:fontsize={fontsize}:line_spacing=12:"
            f"box=1:boxcolor=black@0.45:boxborderw=28:"
            f"x=(w-text_w)/2:y=h*0.70:"
            f"alpha='if(lt(t,0.8),0,if(lt(t,1.8),(t-0.8),1))'"
        )
    return vf


def main():
    print(f"Job ID: {JOB_ID}")

    job_file = WORKDIR / "job.json"
    if not yd_get(f"{JOB_YD}/job.json", job_file):
        sys.exit("Failed to download job.json")
    job = json.loads(job_file.read_text())

    duration   = float(job["duration"])
    fmt        = job.get("format", "square")
    out_name   = job["out_name"]
    hook_text  = job.get("hook_text", "").strip()
    audio_start = float(job.get("audio_start", 0))
    W, H = FMT_DIMS.get(fmt, FMT_DIMS["square"])
    frames = int(round(duration * 25))

    print(f"  duration={duration}s format={fmt} ({W}x{H}) audio_start={audio_start}s")
    print(f"  hook={'yes' if hook_text else 'no'}")

    track_file = WORKDIR / "track.mp3"
    if not yd_get(f"{JOB_YD}/track.mp3", track_file):
        sys.exit("Failed to download track.mp3")

    image_file = WORKDIR / "image.png"
    if not yd_get(f"{JOB_YD}/image.png", image_file):
        sys.exit("Failed to download image.png")
    print(f"  image {image_file.stat().st_size//1024}KB  track {track_file.stat().st_size//1024}KB")

    if hook_text:
        (WORKDIR / "hook.txt").write_text(hook_text, encoding="utf-8")

    vf = build_filter(W, H, duration, frames, bool(hook_text))

    result = WORKDIR / out_name
    afade_out = max(0.0, duration - 1.5)
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-t", str(duration), "-i", str(image_file),
        "-ss", str(audio_start), "-t", str(duration), "-i", str(track_file),
        "-vf", vf,
        "-map", "0:v", "-map", "1:a",
        "-af", f"afade=t=in:st=0:d=1,afade=t=out:st={afade_out}:d=1.5",
        "-c:v", "libx264", "-crf", "20", "-preset", "medium", "-r", "25",
        "-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p",
        "-shortest", str(result),
    ]
    print("\n── Rendering (ffmpeg) ──")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not result.exists() or result.stat().st_size < 5000:
        print(r.stderr[-600:])
        yd_put_text(f"error: ffmpeg rc={r.returncode}", f"{JOB_YD}/status.txt")
        sys.exit("ffmpeg failed")

    mb = result.stat().st_size / 1024 / 1024
    print(f"  {out_name}  {mb:.1f}MB")

    print(f"\n── Uploading {out_name} ──")
    if not yd_put(result, f"{JOB_YD}/{out_name}"):
        yd_put_text("error: upload failed", f"{JOB_YD}/status.txt")
        sys.exit("Upload failed")

    yd_put_text("done", f"{JOB_YD}/status.txt")
    print(f"\n✅ Done: {out_name} ({mb:.1f}MB)")


if __name__ == "__main__":
    main()
