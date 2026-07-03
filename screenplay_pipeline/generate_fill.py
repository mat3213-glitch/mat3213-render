#!/usr/bin/env python3
"""
generate_fill.py — тематический арт-фон для заливки зелёных зон vinil-хромакея (стадия 6a).

Без fill.mp4 vinil-клипы с chroma-зоной рендерятся с голым зелёным экраном (пойман вживую
2026-07-03). Берёт central_motif/logline из treatment.json → art_gen.generate_image() (free,
Pollinations→HuggingFace) → зацикливает в короткое видео → заливает на ЯД как fill.mp4.

Usage:
  python3 generate_fill.py --treatment path/to/treatment.json --job-id JOB_ID --duration 30
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "yaromat_music"))
from art_gen import generate_image

YD_ROOT = "ydrive:Content factory"

# бренд-правила (brand_constants/screenwriter SYSTEM): без лиц/неона/текста/синтетики
QUALITY_TAIL = (
    "no faces, no people, no text, no neon, no watermark, photographic, film grain, "
    "muted desaturated palette, atmospheric, cinematic lighting"
)


def build_prompt(treatment: dict) -> str:
    motif = treatment.get("central_motif", "")
    logline = treatment.get("logline", "")
    return f"{motif}, {logline}".strip(", ")


def loop_to_video(image_path: str, out_path: str, duration: float, w: int = 1080, h: int = 1080) -> None:
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-loop", "1", "-i", image_path,
        "-t", str(duration),
        "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps=25",
        "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p",
        out_path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or not Path(out_path).exists():
        raise RuntimeError(f"ffmpeg loop failed: {r.stderr[:300]}")


def upload_yd(path: str, job_id: str):
    dst = f"{YD_ROOT}/cloud_io/render_jobs/{job_id}/fill.mp4"
    r = subprocess.run(["rclone", "copyto", path, dst], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[rclone] copyto failed: {r.stderr[:300]}", file=sys.stderr)
        sys.exit(1)
    print(f"[rclone] uploaded → {dst}")


def main():
    ap = argparse.ArgumentParser(description="Генерация fill.mp4 (арт-заливка зелёных зон vinil).")
    ap.add_argument("--treatment", required=True, help="путь к treatment.json")
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--duration", type=float, default=30.0, help="длительность fill.mp4 (с)")
    ap.add_argument("--w", type=int, default=1080)
    ap.add_argument("--h", type=int, default=1080)
    args = ap.parse_args()

    treatment = json.loads(Path(args.treatment).read_text(encoding="utf-8"))
    prompt = build_prompt(treatment)
    print(f"[fill] prompt: {prompt}")

    tmpdir = tempfile.mkdtemp(prefix="fill_")
    img_path = generate_image(prompt, filename_hint="fill", width=args.w, height=args.h,
                              out_dir=Path(tmpdir), quality_tail=QUALITY_TAIL)
    if not img_path:
        print("[error] art_gen не смог сгенерировать изображение (оба движка упали)", file=sys.stderr)
        sys.exit(1)

    out_video = str(Path(tmpdir) / "fill.mp4")
    loop_to_video(img_path, out_video, args.duration, args.w, args.h)
    print(f"[fill] видео готово → {out_video}")

    upload_yd(out_video, args.job_id)


if __name__ == "__main__":
    main()
