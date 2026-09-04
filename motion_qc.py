#!/usr/bin/env python3
"""Cheap advisory motion evidence for rendered videos.

This module never gates a render.  It reuses the existing black/freeze scan and
adds a tiny grayscale frame-diff pass; optical flow and threshold calibration are
explicitly deferred until real baselines exist.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
from pathlib import Path
from typing import Any

from qc_gate import probe_duration, run_video_filters

SAMPLE_WIDTH, SAMPLE_HEIGHT = 64, 36


class MotionQcError(ValueError):
    pass


def _positive(value: object, name: str, *, zero_ok: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise MotionQcError(f"{name} must be numeric") from exc
    if number < 0 or (not zero_ok and number == 0):
        raise MotionQcError(f"{name} must be {'non-negative' if zero_ok else 'positive'}")
    return number


def mean_abs_diff(previous: bytes, current: bytes) -> float:
    if len(previous) != len(current) or not previous:
        raise MotionQcError("sample frames must have the same non-zero size")
    return round(sum(abs(left - right) for left, right in zip(previous, current)) / len(current), 4)


def sustained_windows(diffs: list[float], *, at_or_below: float | None = None,
                      at_or_above: float | None = None, min_pairs: int = 3) -> list[tuple[int, int]]:
    if (at_or_below is None) == (at_or_above is None):
        raise MotionQcError("choose exactly one window threshold direction")
    matches = [value <= at_or_below if at_or_below is not None else value >= at_or_above for value in diffs]
    windows: list[tuple[int, int]] = []; start: int | None = None
    for index, matched in enumerate(matches + [False]):
        if matched and start is None:
            start = index
        elif not matched and start is not None:
            if index - start >= min_pairs:
                windows.append((start, index))
            start = None
    return windows


def sampled_diffs(path: str | Path, *, sample_fps: float) -> list[float]:
    sample_fps = _positive(sample_fps, "sample_fps")
    frame_size = SAMPLE_WIDTH * SAMPLE_HEIGHT
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path), "-an", "-vf",
         f"fps={sample_fps}:round=down,scale={SAMPLE_WIDTH}:{SAMPLE_HEIGHT}:flags=area,format=gray",
         "-f", "rawvideo", "-"], capture_output=True, timeout=300,
    )
    if result.returncode:
        raise MotionQcError(f"frame sampling failed: {result.stderr.decode(errors='replace')[-200:]}")
    raw = result.stdout
    if len(raw) % frame_size:
        raise MotionQcError("frame sampler produced a partial grayscale frame")
    frames = [raw[offset:offset + frame_size] for offset in range(0, len(raw), frame_size)]
    return [mean_abs_diff(left, right) for left, right in zip(frames, frames[1:])]


def _evidence(kind: str, windows: list[tuple[int, int]], *, sample_fps: float,
              threshold: float) -> list[dict[str, Any]]:
    return [{"kind": kind, "start_sec": round(start / sample_fps, 3),
             "end_sec": round((end + 1) / sample_fps, 3), "pair_count": end - start,
             "threshold": threshold} for start, end in windows]


def analyze(path: str | Path, *, sample_fps: float = 2.0, low_diff: float = 1.0,
            high_diff: float = 45.0, min_pairs: int = 3) -> dict[str, Any]:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(source)
    sample_fps = _positive(sample_fps, "sample_fps")
    low_diff = _positive(low_diff, "low_diff", zero_ok=True)
    high_diff = _positive(high_diff, "high_diff")
    if high_diff <= low_diff or min_pairs < 1:
        raise MotionQcError("high_diff must exceed low_diff and min_pairs must be positive")
    duration = probe_duration(source)
    if duration is None or duration <= 0:
        raise MotionQcError("video duration is unavailable")
    diffs = sampled_diffs(source, sample_fps=sample_fps)
    findings = [{"kind": "qc_gate", "detail": detail} for detail in run_video_filters(source)]
    findings += _evidence("near_static_window", sustained_windows(
        diffs, at_or_below=low_diff, min_pairs=min_pairs), sample_fps=sample_fps, threshold=low_diff)
    findings += _evidence("high_change_window", sustained_windows(
        diffs, at_or_above=high_diff, min_pairs=min_pairs), sample_fps=sample_fps, threshold=high_diff)
    return {"schema": 1, "advisory": True, "video": {"path": str(source), "duration_sec": round(duration, 3)},
            "sampling": {"fps": sample_fps, "size": [SAMPLE_WIDTH, SAMPLE_HEIGHT], "pair_count": len(diffs)},
            "thresholds": {"low_diff": low_diff, "high_diff": high_diff, "min_pairs": min_pairs},
            "diff_summary": ({"min": min(diffs), "median": round(statistics.median(diffs), 4), "max": max(diffs)} if diffs else None),
            "findings": findings}


def main() -> int:
    parser = argparse.ArgumentParser(description="Write advisory motion-QC evidence JSON")
    parser.add_argument("video"); parser.add_argument("--out", required=True)
    parser.add_argument("--sample-fps", type=float, default=2.0); parser.add_argument("--low-diff", type=float, default=1.0)
    parser.add_argument("--high-diff", type=float, default=45.0); parser.add_argument("--min-pairs", type=int, default=3)
    args = parser.parse_args()
    report = analyze(args.video, sample_fps=args.sample_fps, low_diff=args.low_diff,
                     high_diff=args.high_diff, min_pairs=args.min_pairs)
    output = Path(args.out); output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"advisory": True, "findings": len(report["findings"]), "out": str(output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
