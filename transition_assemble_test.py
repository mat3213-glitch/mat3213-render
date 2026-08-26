#!/usr/bin/env python3
"""
A/B/C test: 3 ways to join uniqueized clips into a montage.

A = simple xfade chain (dissolve everywhere)
B = director-style xfade (different transitions by position)
C = hard cut (concat)

Usage:
  python transition_assemble_test.py --source-folder "Content factory/cloud_io/trans_test/raw"
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

def sh(cmd: list[str], *, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

def probe(path: Path) -> dict:
    r = sh(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", str(path)])
    return json.loads(r.stdout)

def probe_duration(path: Path) -> float:
    r = sh(["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    try:
        return float(r.stdout.strip())
    except Exception:
        return 0.0

def probe_fps(path: Path) -> float:
    r = sh(["ffprobe", "-v", "quiet", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", str(path)])
    try:
        num, den = r.stdout.strip().split("/")
        fps = float(num) / float(den)
        return fps if fps > 0 else 24.0
    except Exception:
        return 24.0

def normalize(src: Path, dst: Path, target_w: int = 720, target_h: int = 1280, duration: float = 8.0) -> None:
    """Crop+scale to target resolution, trim to duration."""
    info = probe(src)
    stream = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if not stream:
        raise RuntimeError(f"no video stream in {src}")
    src_w = int(stream["width"])
    src_h = int(stream["height"])
    src_aspect = src_w / src_h
    tgt_aspect = target_w / target_h

    if src_aspect > tgt_aspect:
        crop_w = int(src_h * tgt_aspect)
        crop_h = src_h
        crop_x = (src_w - crop_w) // 2
        crop_y = 0
    else:
        crop_w = src_w
        crop_h = int(src_w / tgt_aspect)
        crop_x = 0
        crop_y = (src_h - crop_h) // 2

    vf = f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y},scale={target_w}:{target_h}:flags=lanczos,setsar=1"
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-t", str(duration),
        "-vf", vf,
        "-an",
        "-c:v", "libx264", "-profile:v", "baseline", "-level:v", "3.1",
        "-pix_fmt", "yuv420p", "-preset", "fast", "-crf", "23",
        "-movflags", "+faststart",
        str(dst),
    ]
    r = sh(cmd, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"normalize failed: {r.stderr[-500:]}")

def uniquize_clip(src: Path, dst: Path, fps: float) -> None:
    """Apply base uniquize recipe + random effect chain."""
    sys.path.insert(0, str(Path(__file__).parent))
    from importlib import import_module
    mod = import_module("pinterest_board_uniquize")
    effects_db = mod.load_effects()
    chain = mod.pick_chain(effects_db)
    print(f"  effect chain: {'+'.join(chain)}")
    mod.uniquize(src, dst, fps=fps, effects_chain=chain, effects_db=effects_db)

def xfade_chain_simple(clips: list[Path], dst: Path, xfade_dur: float = 0.6) -> None:
    """A: dissolve between every pair."""
    n = len(clips)
    if n < 2:
        raise RuntimeError("need >= 2 clips")
    durs = [probe_duration(c) for c in clips]
    total = sum(durs) - xfade_dur * (n - 1)

    inputs = []
    for c in clips:
        inputs += ["-i", str(c)]

    filters = []
    # Normalize all inputs
    for i in range(n):
        filters.append(f"[{i}:v]settb=AVTB,setpts=PTS-STARTPTS,fps=25,scale=720:1280,setsar=1[v{i}]")

    # Chain xfade
    prev = "v0"
    for i in range(1, n):
        offset = sum(durs[:i]) - xfade_dur * i
        offset = max(0.1, offset)
        out = f"xf{i}" if i < n - 1 else "vout"
        filters.append(f"[{prev}][v{i}]xfade=transition=fade:duration={xfade_dur}:offset={offset:.3f}[{out}]")
        prev = out

    graph = ";".join(filters)
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        *inputs,
        "-filter_complex", graph,
        "-map", "[vout]", "-an",
        "-t", str(total),
        "-c:v", "libx264", "-profile:v", "baseline", "-level:v", "3.1",
        "-pix_fmt", "yuv420p", "-preset", "fast", "-crf", "23",
        "-movflags", "+faststart",
        str(dst),
    ]
    r = sh(cmd, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"xfade_chain_simple failed: {r.stderr[-600:]}")

def xfade_chain_director(clips: list[Path], dst: Path, xfade_dur: float = 0.6) -> None:
    """B: different transitions by position (intro=fade, body=wipeleft, climax=fadeblack, outro=fade)."""
    n = len(clips)
    if n < 2:
        raise RuntimeError("need >= 2 clips")
    durs = [probe_duration(c) for c in clips]
    total = sum(durs) - xfade_dur * (n - 1)

    # Transition assignments by position
    transitions = []
    for i in range(n - 1):
        if i == 0:
            transitions.append(("fade", 0.8))        # intro → body: slow dissolve
        elif i == n - 2:
            transitions.append(("fade", 0.8))        # body → outro: slow dissolve
        elif i % 2 == 0:
            transitions.append(("wipeleft", 0.5))    # body alternates
        else:
            transitions.append(("fadeblack", 0.3))   # dip to black

    inputs = []
    for c in clips:
        inputs += ["-i", str(c)]

    filters = []
    for i in range(n):
        filters.append(f"[{i}:v]settb=AVTB,setpts=PTS-STARTPTS,fps=25,scale=720:1280,setsar=1[v{i}]")

    prev = "v0"
    for i, (tr, dur) in enumerate(transitions):
        offset = sum(durs[:i + 1]) - dur * (i + 1)
        offset = max(0.1, offset)
        out = f"xf{i}" if i < n - 2 else "vout"
        filters.append(f"[{prev}][v{i + 1}]xfade=transition={tr}:duration={dur}:offset={offset:.3f}[{out}]")
        prev = out

    graph = ";".join(filters)
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        *inputs,
        "-filter_complex", graph,
        "-map", "[vout]", "-an",
        "-t", str(total),
        "-c:v", "libx264", "-profile:v", "baseline", "-level:v", "3.1",
        "-pix_fmt", "yuv420p", "-preset", "fast", "-crf", "23",
        "-movflags", "+faststart",
        str(dst),
    ]
    r = sh(cmd, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"xfade_chain_director failed: {r.stderr[-600:]}")

def hard_cut_concat(clips: list[Path], dst: Path) -> None:
    """C: concat demuxer, hard cut."""
    list_file = dst.parent / "concat_list.txt"
    with open(list_file, "w") as f:
        for c in clips:
            f.write(f"file '{c}'\n")
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c:v", "libx264", "-profile:v", "baseline", "-level:v", "3.1",
        "-pix_fmt", "yuv420p", "-preset", "fast", "-crf", "23",
        "-movflags", "+faststart",
        str(dst),
    ]
    r = sh(cmd, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"hard_cut_concat failed: {r.stderr[-600:]}")

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-folder", required=True, help="YaD folder with raw mp4s")
    ap.add_argument("--max-clips", type=int, default=4, help="number of clips to test with")
    ap.add_argument("--clip-duration", type=float, default=8.0, help="each clip trimmed to this")
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="trans_test_"))
    raw_dir = work / "raw"
    norm_dir = work / "norm"
    out_dir = work / "out"
    raw_dir.mkdir()
    norm_dir.mkdir()
    out_dir.mkdir()

    # Download
    print(f"[test] downloading from {args.source_folder}...")
    r = sh(["rclone", "copy", f"ydrive:{args.source_folder}", str(raw_dir)], timeout=600)
    if r.returncode != 0:
        print(f"rclone error: {r.stderr[-500:]}")
        return 2

    sources = sorted(raw_dir.glob("*.mp4"))[:args.max_clips]
    if len(sources) < 2:
        print(f"need >= 2 mp4 files, got {len(sources)}")
        return 1
    print(f"[test] using {len(sources)} clips: {[s.name for s in sources]}")

    # Normalize
    print("[test] normalizing clips...")
    normed = []
    for i, src in enumerate(sources):
        dst = norm_dir / f"clip_{i:02d}.mp4"
        normalize(src, dst, duration=args.clip_duration)
        normed.append(dst)
        print(f"  {src.name} -> {dst.name}")

    # Uniquize each
    print("[test] uniquizing clips...")
    uniquized = []
    for i, src in enumerate(normed):
        dst = norm_dir / f"uniq_{i:02d}.mp4"
        fps = probe_fps(src)
        uniquize_clip(src, dst, fps)
        uniquized.append(dst)
        print(f"  {src.name} -> {dst.name}")

    # Assembly A: simple dissolve
    print("[test] assembling A: simple dissolve chain...")
    try:
        dst_a = out_dir / "A_simple_dissolve.mp4"
        xfade_chain_simple(uniquized, dst_a)
        print(f"  OK -> {dst_a.name}")
    except Exception as e:
        print(f"  FAIL: {e}")

    # Assembly B: director transitions
    print("[test] assembling B: director transitions...")
    try:
        dst_b = out_dir / "B_director.mp4"
        xfade_chain_director(uniquized, dst_b)
        print(f"  OK -> {dst_b.name}")
    except Exception as e:
        print(f"  FAIL: {e}")

    # Assembly C: hard cut
    print("[test] assembling C: hard cut concat...")
    try:
        dst_c = out_dir / "C_hard_cut.mp4"
        hard_cut_concat(uniquized, dst_c)
        print(f"  OK -> {dst_c.name}")
    except Exception as e:
        print(f"  FAIL: {e}")

    # Summary
    print("\n[test] === RESULTS ===")
    for f in sorted(out_dir.glob("*.mp4")):
        dur = probe_duration(f)
        sz = f.stat().st_size / 1024 / 1024
        print(f"  {f.name}: {dur:.1f}s, {sz:.1f}MB")

    # Upload to YaD
    dest_folder = args.source_folder.rstrip("/") + "/trans_test_results"
    sh(["rclone", "mkdir", f"ydrive:{dest_folder}"], timeout=60)
    r = sh(["rclone", "copy", str(out_dir), f"ydrive:{dest_folder}"], timeout=600)
    if r.returncode == 0:
        print(f"\n[test] uploaded -> ydrive:{dest_folder}")
    else:
        print(f"\n[test] upload failed: {r.stderr[-500:]}")

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
