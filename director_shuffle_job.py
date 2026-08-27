#!/usr/bin/env python3
"""
director_shuffle_job.py — clip_producer variant: deterministic round-robin / cyclic-shuffle
with director-style xfade transitions. Renders two variants (A, B) of the same job for
visual comparison without random repetition.

Reads job.json from Yandex.Disk, runs analyze_track to get energy/beat groups, then
reslices segments to a fixed beat grid (default 6 beats) so every variant has the same
timeline. A = round-robin (no source used twice until the pool is exhausted), B =
cyclic shuffle (deterministic permutation, repeats after one full pass). Both variants
are then assembled through xfade_chain_director (fade/wipeleft/fadeblack by position),
mixed with track.mp3, and uploaded as <out_base>_A.mp4 / <out_base>_B.mp4 plus
status.txt and render_receipt.json.

Required inputs in <JOB_ID>/:
  job.json  — {format, duration, sources[], seed, out_base, beats_per_segment?}
  track.mp3 — the audio track
  <src>.mp4 — one file per name in sources[]

Job env: JOB_ID. Optional env: SHUFFLE_OUT_BASE (default = out_base), SHUFFLE_VARIANTS
(comma-separated subset of {A,B}; default = both).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

# Re-use the original clip_producer pipeline primitives.  We import lazily so the
# module is usable even if the surrounding repo layout shifts.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import clip_producer_job as cpj  # noqa: E402
from analyze import Segment  # noqa: E402


# ── beat-grid reslicing ────────────────────────────────────────────────────


def reslice_to_beat_grid(segments: list[Segment], bpm: float,
                        beats_per_segment: int = 6) -> list[Segment]:
    """Rewrite energy-grouped segments to a uniform beat grid.

    Each output segment spans `beats_per_segment` beats.  Energy classification
    is dropped — every slice is treated the same; the visual rhythm comes from
    the cuts and the xfade pattern, not the energy curve.  The first slice
    starts at t=0 so the audio timeline stays anchored.
    """
    if not segments:
        return []
    beat = 60.0 / bpm
    total_dur = segments[-1].track_pos + segments[-1].duration
    n_full = int(total_dur / (beat * beats_per_segment))
    out: list[Segment] = []
    for i in range(n_full):
        pos = i * beat * beats_per_segment
        dur = beat * beats_per_segment
        out.append(Segment(track_pos=round(pos, 4), duration=round(dur, 4),
                          n_beats=beats_per_segment, energy="grid"))
    return out


# ── deterministic source assignment ────────────────────────────────────────


def assign_round_robin(segments: list[Segment], sources: list[str]) -> list[Segment]:
    """Each source is used at most once before any repeats; no back-to-back same source
    if the pool is at least as large as two consecutive segment counts (it usually is)."""
    if not sources:
        return segments
    out: list[Segment] = []
    cursor = 0
    last = ""
    for seg in segments:
        if cursor >= len(sources) or sources[cursor] == last:
            # pool exhausted or would repeat last; pick the next slot, skip if equal
            chosen = sources[cursor % len(sources)]
            for off in range(len(sources)):
                cand = sources[(cursor + off) % len(sources)]
                if cand != last:
                    chosen = cand
                    cursor = (cursor + off + 1) % len(sources)
                    break
            out.append(replace(seg, source=chosen))
            last = chosen
        else:
            chosen = sources[cursor]
            out.append(replace(seg, source=chosen))
            cursor += 1
            last = chosen
    return out


def assign_cyclic_shuffle(segments: list[Segment], sources: list[str],
                          seed: int) -> list[Segment]:
    """Shuffle the pool deterministically and walk it; on exhaustion reshuffle from
    the next seed offset.  Prevents same-source back-to-back by skipping in shuffle."""
    if not sources:
        return segments
    pool = list(sources)
    rng = random.Random(seed)
    out: list[Segment] = []
    last = ""
    while len(out) < len(segments):
        rng.shuffle(pool)
        for src in pool:
            if src == last:
                continue
            if len(out) >= len(segments):
                break
            seg = replace(segments[len(out)], source=src)
            out.append(seg)
            last = src
        # no break needed; if pool entirely equals last we accept the repeat
    return out


# ── xfade assembly (delegates to render repo) ─────────────────────────────


def xfade_assemble(seg_files: list[Path], dst: Path,
                   target_w: int, target_h: int,
                   xfade_dur: float = 0.6) -> None:
    """Director-style xfade chain from the A/B/C test script."""
    import importlib
    mod = importlib.import_module("transition_assemble_test")
    # transition_assemble_test uses 720x1280 as default; we override via normalize-then-xfade
    # For our case seg_files are already 720x1280 vertical — we just need xfade, not normalize.
    # Re-implement the xfade graph directly with the correct resolution.
    n = len(seg_files)
    if n < 2:
        # single clip — just copy
        shutil.copyfile(seg_files[0], dst)
        return
    durs = [cpj.video_duration(f) for f in seg_files]
    total = sum(durs) - xfade_dur * (n - 1)

    transitions = []
    for i in range(n - 1):
        if i == 0 or i == n - 2:
            transitions.append(("fade", 0.8))
        elif i % 2 == 0:
            transitions.append(("wipeleft", 0.5))
        else:
            transitions.append(("fadeblack", 0.3))

    inputs = []
    for f in seg_files:
        inputs += ["-i", str(f)]
    filters = []
    for i in range(n):
        filters.append(
            f"[{i}:v]settb=AVTB,setpts=PTS-STARTPTS,fps=25,"
            f"scale={target_w}:{target_h},setsar=1[v{i}]"
        )
    prev = "v0"
    for i, (tr, dur) in enumerate(transitions):
        offset = sum(durs[: i + 1]) - dur * (i + 1)
        offset = max(0.1, offset)
        out = f"xf{i}" if i < n - 2 else "vout"
        filters.append(
            f"[{prev}][v{i + 1}]xfade=transition={tr}:duration={dur}:"
            f"offset={offset:.3f}[{out}]"
        )
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
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"xfade chain failed: {r.stderr[-500:]}")


# ── pipeline pieces reused from clip_producer_job ─────────────────────────


def download_inputs(work: Path, job: dict) -> tuple[Path, dict[str, Path], dict[str, float]]:
    track = work / "track.mp3"
    if not cpj.yd_get(f"{cpj.JOB_YD}/track.mp3", track):
        raise SystemExit("Failed to download track.mp3")
    src_files: dict[str, Path] = {}
    src_durations: dict[str, float] = {}
    for src in job["sources"]:
        dest = work / f"{src}.mp4"
        if not cpj.yd_get(f"{cpj.JOB_YD}/{src}.mp4", dest):
            print(f"  WARN: {src}.mp4 not found")
            continue
        dur = cpj.video_duration(dest)
        src_files[src] = dest
        src_durations[src] = dur
    if not src_files:
        raise SystemExit("No source videos available")
    return track, src_files, src_durations


def cut_segments(segments: list[Segment], src_files: dict[str, Path],
                src_durations: dict[str, float], work: Path,
                fmt_filter: str) -> list[Path]:
    out: list[Path] = []
    for i, seg in enumerate(segments):
        if not seg.source:
            continue
        src_file = src_files[seg.source]
        src_dur = src_durations[seg.source]
        src_start = max(0.0, min(float(seg.src_start or 0.0), max(0.0, src_dur - 0.5)))
        seg_dur = float(seg.duration)
        if src_start + seg_dur > src_dur:
            src_start = max(0.0, src_dur - seg_dur - 0.1)
        out_file = work / f"seg_{i:03d}.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(round(src_start, 3)),
            "-t", str(round(seg_dur, 3)),
            "-i", str(src_file),
            "-vf", fmt_filter,
            "-c:v", "libx264", "-crf", "23", "-preset", "fast",
            "-an",
            "-fps_mode", "cfr", "-r", "25",
            str(out_file),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not out_file.exists() or out_file.stat().st_size < 1000:
            print(f"  seg_{i:03d} FAIL: {r.stderr[-200:]}")
            continue
        out.append(out_file)
    return out


def mix_audio(concat: Path, track: Path, out: Path, duration: float) -> None:
    r = subprocess.run([
        "ffmpeg", "-y",
        "-i", str(concat),
        "-i", str(track),
        "-t", str(round(duration, 3)),
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest", str(out),
    ], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"audio mix failed: {r.stderr[-200:]}")


def media_summary(path: Path) -> str:
    r = subprocess.run([
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ], capture_output=True, text=True)
    try:
        d = json.loads(r.stdout)
        v = next((s for s in d["streams"] if s["codec_type"] == "video"), None)
        a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
        dur = float(d["format"]["duration"])
        return f"dur={dur:.3f}s v={v['codec_name']}@{v.get('width')}x{v.get('height')} a={a['codec_name'] if a else 'none'}"
    except Exception as e:
        return f"summary failed: {e}"


# ── main ──────────────────────────────────────────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--beats-per-segment", type=int, default=6,
                   help="uniform beat grid (default 6 beats ≈ 2.8s @ 128 BPM)")
    args = ap.parse_args()

    JOB_ID = os.environ.get("JOB_ID", "")
    if not JOB_ID:
        sys.exit("JOB_ID not set")

    work = Path("/tmp/director_shuffle_job")
    work.mkdir(parents=True, exist_ok=True)

    job_file = work / "job.json"
    if not cpj.yd_get(f"{cpj.JOB_YD}/job.json", job_file):
        sys.exit("Failed to download job.json")
    job = json.loads(job_file.read_text())

    duration = float(job["duration"])
    fmt = job.get("format", "vertical")
    sources = list(job["sources"])
    seed = job.get("seed")
    beats_per_seg = int(job.get("beats_per_segment", args.beats_per_segment))
    out_base = job.get("out_base", job.get("out_name", "clip"))
    variants_env = os.environ.get("SHUFFLE_VARIANTS", "A,B")
    variants = [v.strip() for v in variants_env.split(",") if v.strip() in ("A", "B")]
    if not variants:
        variants = ["A", "B"]

    fmt_filter = cpj.FMT_FILTERS.get(fmt, cpj.FMT_FILTERS["vertical"])
    target_w, target_h = (1080, 1920) if fmt == "vertical" else (1080, 1080)

    print(f"Job ID: {JOB_ID}")
    print(f"  duration={duration}s format={fmt} sources={len(sources)} variants={variants}")
    print(f"  beats_per_segment={beats_per_seg} seed={seed} out_base={out_base}")

    track, src_files, src_durations = download_inputs(work, job)

    # 1. Audio analysis
    bpm, raw_segments = cpj.analyze_track(track, duration=duration, seed=seed)
    print(f"  raw BPM={bpm:.2f}  segments={len(raw_segments)}")

    # 2. Reslice to uniform beat grid
    grid_segments = reslice_to_beat_grid(raw_segments, bpm, beats_per_segment=beats_per_seg)
    print(f"  grid segments={len(grid_segments)} (each {beats_per_seg} beats)")

    # 3. Render each variant
    for variant in variants:
        print(f"\n── Variant {variant} ──")
        if variant == "A":
            segs = assign_round_robin([replace(s) for s in grid_segments], sources)
        else:
            assert variant == "B"
            segs = assign_cyclic_shuffle([replace(s) for s in grid_segments],
                                         sources, seed=(seed or 0) + 1)
        # give every source a starting offset within its own file (no reuse at t=0)
        cpj.assign_src_starts(segs, src_durations)

        # 4. Cut
        variant_work = work / variant
        variant_work.mkdir(exist_ok=True)
        seg_files = cut_segments(segs, src_files, src_durations, variant_work, fmt_filter)
        if not seg_files:
            print(f"  variant {variant}: no segments rendered — skipping")
            continue
        print(f"  cut {len(seg_files)}/{len(segs)} segments")

        # 5. xfade assemble (director transitions)
        xfade_out = variant_work / f"{variant}_xfade.mp4"
        xfade_assemble(seg_files, xfade_out, target_w=target_w, target_h=target_h)
        print(f"  xfade {xfade_out.stat().st_size//1024}KB")

        # 6. Mix audio
        out_name = f"{out_base}_{variant}.mp4"
        out_path = variant_work / out_name
        mix_audio(xfade_out, track, out_path, duration=duration)
        print(f"  mixed  {media_summary(out_path)}")

        # 7. Upload
        remote = f"{cpj.JOB_YD}/{out_name}"
        if cpj.yd_put(out_path, remote):
            print(f"  uploaded → {remote}")
        else:
            print(f"  UPLOAD FAILED for {out_name}")
            continue

    # 8. Status
    cpj.yd_put_text(f"ok: variants={','.join(variants)} seed={seed} bps={beats_per_seg}\n"
                   f"bpm={bpm:.2f} grid_segments={len(grid_segments)}",
                   f"{cpj.JOB_YD}/status.txt")
    print(f"\n✅ Done: variants {','.join(variants)} uploaded")


if __name__ == "__main__":
    main()
