#!/usr/bin/env python3
"""
clip_producer_job.py — GitHub Actions runner for CLIP_PRODUCER pipeline.

Steps:
  1. Download track.mp3 + source videos + job.json from YaDisk
  2. Analyze track with librosa (BPM + energy segmentation)
  3. Assign sources to segments, compute src_starts
  4. Cut segments with ffmpeg, concat, mix audio
  5. Upload result.mp4 + status.txt to YaDisk

Environment: JOB_ID (from workflow input)
"""

import json
import os
import random
import subprocess
import sys
from pathlib import Path

# analyze.py sits next to this file in the repo
from analyze import analyze_track, Segment, find_highlight_offset

JOB_ID  = os.environ.get("JOB_ID", "")
if not JOB_ID:
    sys.exit("JOB_ID not set")

REMOTE  = "ydrive"
JOBS_YD = "Content factory/cloud_io/render_jobs"
JOB_YD  = f"{JOBS_YD}/{JOB_ID}"
WORKDIR = Path("/tmp/clip_job")
WORKDIR.mkdir(parents=True, exist_ok=True)

FMT_FILTERS = {
    "square":    "scale=1080:1080:force_original_aspect_ratio=increase,crop=1080:1080",
    "vertical":  "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
    "landscape": "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080",
}


# ── rclone ─────────────────────────────────────────────────────────────────────

def yd_get(remote_path: str, local: Path) -> bool:
    local.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["rclone", "copyto", f"{REMOTE}:{remote_path}", str(local)],
        capture_output=True, text=True,
    )
    return r.returncode == 0

def yd_put(local: Path, remote_path: str) -> bool:
    r = subprocess.run(
        ["rclone", "copyto", str(local), f"{REMOTE}:{remote_path}"],
        capture_output=True, text=True,
    )
    return r.returncode == 0

def yd_put_text(text: str, remote_path: str):
    tmp = WORKDIR / "_status.txt"
    tmp.write_text(text)
    yd_put(tmp, remote_path)


# ── helpers ────────────────────────────────────────────────────────────────────

def video_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 60.0

def assign_sources(segments: list[Segment], sources: list[str]) -> list[Segment]:
    for seg in segments:
        seg.source = random.choice(sources)
    return segments

def assign_src_starts(segments: list[Segment], durations: dict[str, float]) -> list[Segment]:
    cursors: dict[str, float] = {
        src: random.uniform(0.0, max(0.0, dur * 0.2))
        for src, dur in durations.items()
    }
    for seg in segments:
        src = seg.source
        dur = durations.get(src, 60.0)
        cursor = cursors.get(src, 0.0)
        if cursor + seg.duration > dur:
            cursor = random.uniform(0.0, max(0.0, dur - seg.duration - 0.5))
            cursor = max(cursor, 0.0)
        seg.src_start = round(cursor, 4)
        cursors[src] = cursor + seg.duration
    return segments


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    print(f"Job ID: {JOB_ID}")

    # 1. Download inputs
    print("\n── Downloading inputs ──")
    job_file = WORKDIR / "job.json"
    if not yd_get(f"{JOB_YD}/job.json", job_file):
        sys.exit("Failed to download job.json")

    job = json.loads(job_file.read_text())
    duration  = float(job["duration"])
    fmt       = job.get("format", "square")
    out_name  = job["out_name"]
    sources   = job["sources"]
    seed      = job.get("seed")
    fmt_filter = FMT_FILTERS.get(fmt, FMT_FILTERS["square"])

    print(f"  duration={duration}s  format={fmt}  sources={sources}")

    track_file = WORKDIR / "track.mp3"
    if not yd_get(f"{JOB_YD}/track.mp3", track_file):
        sys.exit("Failed to download track.mp3")
    print(f"  track.mp3  {track_file.stat().st_size//1024}KB")

    src_files: dict[str, Path] = {}
    src_durations: dict[str, float] = {}
    for src in sources:
        dest = WORKDIR / f"{src}.mp4"
        if not yd_get(f"{JOB_YD}/{src}.mp4", dest):
            print(f"  WARNING: {src}.mp4 not found — skipping")
            continue
        dur = video_duration(dest)
        src_files[src] = dest
        src_durations[src] = dur
        print(f"  {src}.mp4  {dest.stat().st_size//1024}KB  {dur:.1f}s")

    if not src_files:
        sys.exit("No source videos available")

    # 2. Analyze track
    print("\n── Audio analysis (librosa) ──")
    if seed is not None:
        random.seed(seed)

    # Highlight-детекция: окно на энергетическом/онсетном пике вместо интро.
    # Гейт уверенности внутри → ровный трек вернёт 0.0 (= прежнее поведение).
    highlight = bool(job.get("highlight", False))
    hl_offset = 0.0
    if highlight:
        print("── Highlight detection ──")
        hl_offset = find_highlight_offset(track_file, window=duration)

    bpm, segments = analyze_track(track_file, duration=duration, seed=seed, start=hl_offset)
    print(f"  BPM={bpm:.1f}  segments={len(segments)}  highlight_offset={hl_offset:.1f}s")

    # 3. Assign sources + src_starts
    active_sources = list(src_files.keys())
    segments = assign_sources(segments, active_sources)
    segments = assign_src_starts(segments, src_durations)

    print("\n  Cut plan (first 8):")
    for s in segments[:8]:
        print(f"    {s.track_pos:6.2f}s  {s.source:10s} @{s.src_start:.1f}s  "
              f"{s.duration:.2f}s [{s.energy}]")
    if len(segments) > 8:
        print(f"    ... ({len(segments)-8} more)")

    # 4. Cut segments
    print(f"\n── Cutting {len(segments)} segments ──")
    seg_files: list[Path] = []

    for i, seg in enumerate(segments):
        src_file = src_files[seg.source]
        out_file = WORKDIR / f"seg_{i:03d}.mp4"
        src_dur  = src_durations[seg.source]

        src_start = float(seg.src_start)
        seg_dur   = float(seg.duration)
        if src_start + seg_dur > src_dur:
            src_start = max(0.0, src_dur - seg_dur - 0.1)

        cmd = [
            "ffmpeg", "-y",
            "-ss", str(round(src_start, 3)),
            "-t",  str(round(seg_dur, 3)),
            "-i",  str(src_file),
            "-vf", fmt_filter,
            "-c:v", "libx264", "-crf", "23", "-preset", "fast",
            "-an",
            "-fps_mode", "cfr", "-r", "25",
            str(out_file),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not out_file.exists() or out_file.stat().st_size < 1000:
            print(f"  seg_{i:03d} FAIL: {r.stderr[-100:]}")
            continue

        seg_files.append(out_file)
        print(f"  seg_{i:03d}  {seg.source:10s}  {seg_dur:.2f}s [{seg.energy}]  "
              f"{out_file.stat().st_size//1024}KB")

    if not seg_files:
        sys.exit("No segments rendered")

    print(f"\n  {len(seg_files)}/{len(segments)} segments OK")

    # 5. Concat
    print("\n── Concatenating ──")
    concat_list = WORKDIR / "concat.txt"
    concat_list.write_text("\n".join(f"file '{f}'" for f in seg_files))

    concat_mp4 = WORKDIR / "concat.mp4"
    r = subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_list), "-c", "copy", str(concat_mp4),
    ], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"Concat failed: {r.stderr[-200:]}")
    print(f"  concat.mp4  {concat_mp4.stat().st_size//1024}KB")

    # 6. Mix audio
    print("\n── Mixing audio ──")
    result = WORKDIR / out_name
    audio_in = ["-i", str(track_file)]
    if hl_offset > 0:
        audio_in = ["-ss", str(round(hl_offset, 3)), "-i", str(track_file)]  # звук с того же пика
    r = subprocess.run([
        "ffmpeg", "-y",
        "-i", str(concat_mp4),
        *audio_in,
        "-t", str(round(duration, 3)),
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest", str(result),
    ], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"Audio mix failed: {r.stderr[-200:]}")

    mb = result.stat().st_size / 1024 / 1024
    print(f"  {out_name}  {mb:.1f}MB")

    # 7. Upload result
    print(f"\n── Uploading {out_name} ──")
    if not yd_put(result, f"{JOB_YD}/{out_name}"):
        sys.exit("Upload failed")

    yd_put_text("done", f"{JOB_YD}/status.txt")
    print(f"\n✅ Done: {out_name} ({mb:.1f}MB)")


if __name__ == "__main__":
    main()
