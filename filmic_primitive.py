#!/usr/bin/env python3
"""Run one isolated filmic A/B primitive; never part of production rendering."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from effect_registry import registry_version, resolve
from render_contract import sha256_file


def apply_primitive(source: str | Path, output: str | Path, *, primitive: str,
                   consumer: str = "filmic", fmt: str = "vertical",
                   max_size_ratio: float = 2.0) -> dict[str, Any]:
    source_path, output_path = Path(source), Path(output)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if max_size_ratio <= 0:
        raise ValueError("max_size_ratio must be positive")
    resolved = resolve(primitive, consumer=consumer, fmt=fmt) if primitive != "off" else resolve("", consumer=consumer, fmt=fmt)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    vf = resolved["filter"] or "null"
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source_path),
         "-vf", vf, "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "fast",
         "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(output_path)],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode or not output_path.is_file():
        raise RuntimeError(f"filmic primitive failed: {result.stderr[-300:]}")
    ratio = output_path.stat().st_size / max(source_path.stat().st_size, 1)
    if ratio > max_size_ratio:
        raise RuntimeError(f"output size ratio {ratio:.3f} exceeds {max_size_ratio:.3f}")
    return {
        "schema": 1, "primitive": resolved["name"] or "off", "consumer": consumer, "format": fmt,
        "policy": resolved["policy"], "filter_hash": resolved["filter_hash"],
        "registry_version": registry_version(), "input_sha256": sha256_file(source_path),
        "output_sha256": sha256_file(output_path), "input_bytes": source_path.stat().st_size,
        "output_bytes": output_path.stat().st_size, "size_ratio": round(ratio, 4),
        "advisory": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run isolated filmic A/B primitive")
    parser.add_argument("input"); parser.add_argument("output"); parser.add_argument("--primitive", default="off")
    parser.add_argument("--receipt", required=True); parser.add_argument("--max-size-ratio", type=float, default=2.0)
    args = parser.parse_args()
    receipt = apply_primitive(args.input, args.output, primitive=args.primitive, max_size_ratio=args.max_size_ratio)
    Path(args.receipt).write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"primitive": receipt["primitive"], "size_ratio": receipt["size_ratio"], "advisory": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
