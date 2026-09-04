#!/usr/bin/env python3
"""Create a compact, deterministic human-review package for a rendered video.

This is deliberately outside every render and publish path.  It reads a video plus
an optional storyboard, writes a contact sheet, three-frame cut windows and a JSON
manifest.  Use it on GitHub Actions for real media; its pure planning functions are
also suitable for cheap local tests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw


class TimelineReviewError(ValueError):
    pass


def _number(value: object, field: str, *, positive: bool = False) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise TimelineReviewError(f"{field} must be numeric") from exc
    if not math.isfinite(result) or (result <= 0 if positive else result < 0):
        raise TimelineReviewError(f"{field} must be {'positive' if positive else 'non-negative'}")
    return result


def probe_video(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(source)
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "format=duration:stream=width,height", "-of", "json", str(source)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode:
        raise TimelineReviewError(f"ffprobe failed: {result.stderr[-200:]}")
    try:
        data = json.loads(result.stdout)
        stream = (data.get("streams") or [])[0]
        duration = _number((data.get("format") or {}).get("duration"), "video duration", positive=True)
        width, height = int(stream["width"]), int(stream["height"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TimelineReviewError("ffprobe returned incomplete video metadata") from exc
    return {"path": str(source), "duration_sec": round(duration, 3), "width": width, "height": height}


def normalize_shots(payload: object, *, duration: float) -> list[dict[str, Any]]:
    rows = payload.get("shots") if isinstance(payload, dict) else payload
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise TimelineReviewError("storyboard shots must be a list")
    shots: list[dict[str, Any]] = []
    cursor = 0.0
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise TimelineReviewError(f"shot {index} must be an object")
        start = _number(row.get("t_in", cursor), f"shot {index}.t_in")
        shot_duration = _number(row.get("t_dur"), f"shot {index}.t_dur", positive=True)
        end = start + shot_duration
        if start + 1e-3 < cursor or end > duration + 1e-3:
            raise TimelineReviewError(f"shot {index} overlaps another shot or exceeds video duration")
        shot = {"index": index, "t_in": round(start, 3), "t_dur": round(shot_duration, 3),
                "t_out": round(end, 3)}
        for key in ("section", "effect"):
            if key in row:
                if not isinstance(row[key], str):
                    raise TimelineReviewError(f"shot {index}.{key} must be a string")
                shot[key] = row[key]
        shots.append(shot)
        cursor = end
    return shots


def sample_times(duration: float, count: int) -> list[float]:
    if count < 1:
        raise TimelineReviewError("sample count must be positive")
    duration = _number(duration, "video duration", positive=True)
    return [round(duration * (index + 1) / (count + 1), 3) for index in range(count)]


def cut_plan(shots: list[dict[str, Any]], *, duration: float, radius: float = 1.5) -> list[dict[str, Any]]:
    radius = _number(radius, "cut radius", positive=True)
    cuts = []
    for index, (left, right) in enumerate(zip(shots, shots[1:])):
        cut = left["t_out"]
        if abs(cut - right["t_in"]) > 1e-3:
            continue
        times = [max(0.0, cut - radius), cut, min(duration, cut + radius)]
        cuts.append({"index": index, "t": round(cut, 3), "left_shot": left["index"],
                     "right_shot": right["index"], "frame_times": [round(t, 3) for t in times]})
    return cuts


def _extract_frame(video: Path, timestamp: float, output: Path) -> None:
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{timestamp:.3f}",
         "-i", str(video), "-frames:v", "1", "-q:v", "3", str(output)],
        capture_output=True, text=True, timeout=90,
    )
    if result.returncode or not output.is_file():
        raise TimelineReviewError(f"frame extraction failed at {timestamp:.3f}s: {result.stderr[-200:]}")


def _sheet(frames: list[tuple[Path, str]], output: Path, *, columns: int = 4) -> None:
    tile_w, tile_h, label_h = 320, 180, 28
    rows = math.ceil(len(frames) / columns)
    canvas = Image.new("RGB", (columns * tile_w, rows * (tile_h + label_h)), (12, 14, 18))
    draw = ImageDraw.Draw(canvas)
    for index, (frame, label) in enumerate(frames):
        image = Image.open(frame).convert("RGB")
        image.thumbnail((tile_w, tile_h))
        x, y = (index % columns) * tile_w, (index // columns) * (tile_h + label_h)
        canvas.paste(image, (x + (tile_w - image.width) // 2, y + (tile_h - image.height) // 2))
        draw.text((x + 8, y + tile_h + 6), label, fill=(235, 235, 235))
    canvas.save(output, quality=88)


def build_review(video_path: str | Path, *, out_dir: str | Path, storyboard_path: str | Path | None = None,
                 samples: int = 12, cut_radius: float = 1.5) -> dict[str, Any]:
    if samples < 1 or samples > 24:
        raise TimelineReviewError("samples must be in 1..24")
    video = Path(video_path)
    video_meta = probe_video(video)
    storyboard: object = []
    if storyboard_path is not None:
        try:
            storyboard = json.loads(Path(storyboard_path).read_text(encoding="utf-8"))
        except OSError as exc:
            raise FileNotFoundError(storyboard_path) from exc
        except json.JSONDecodeError as exc:
            raise TimelineReviewError(f"storyboard JSON invalid: {exc}") from exc
    shots = normalize_shots(storyboard, duration=video_meta["duration_sec"])
    cuts = cut_plan(shots, duration=video_meta["duration_sec"], radius=cut_radius)
    out = Path(out_dir); frames_dir = out / "frames"; cuts_dir = out / "cuts"
    frames_dir.mkdir(parents=True, exist_ok=True); cuts_dir.mkdir(exist_ok=True)
    contact_frames = []
    for index, timestamp in enumerate(sample_times(video_meta["duration_sec"], samples)):
        frame = frames_dir / f"sample_{index:02d}.jpg"; _extract_frame(video, timestamp, frame)
        contact_frames.append((frame, f"{timestamp:.3f}s"))
    contact_sheet = out / "contact_sheet.jpg"; _sheet(contact_frames, contact_sheet)
    for cut in cuts:
        triptych = []
        for position, timestamp in enumerate(cut["frame_times"]):
            frame = frames_dir / f"cut_{cut['index']:03d}_{position}.jpg"; _extract_frame(video, timestamp, frame)
            triptych.append((frame, f"{timestamp:.3f}s"))
        cut["sheet"] = str(Path("cuts") / f"cut_{cut['index']:03d}.jpg")
        _sheet(triptych, out / cut["sheet"], columns=3)
    manifest = {"schema": 1, "video": video_meta, "shots": shots, "cuts": cuts,
                "contact_sheet": contact_sheet.name, "generated_at": datetime.now(timezone.utc).isoformat()}
    manifest_path = out / "timeline_review.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"contact_sheet": str(contact_sheet), "manifest": str(manifest_path), "cuts": cuts}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a compact review package for video cuts")
    parser.add_argument("video"); parser.add_argument("--out", required=True)
    parser.add_argument("--storyboard"); parser.add_argument("--samples", type=int, default=12)
    parser.add_argument("--cut-radius", type=float, default=1.5)
    args = parser.parse_args()
    result = build_review(args.video, out_dir=args.out, storyboard_path=args.storyboard,
                          samples=args.samples, cut_radius=args.cut_radius)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
