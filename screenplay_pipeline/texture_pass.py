#!/usr/bin/env python3
"""
texture_pass.py — текстурный пасс (зерно/винтаж/грейд) для клипов.

Публичная функция apply_texture() применяет фильтры FFmpeg (зерно, скретчи, грит, эгалайзер,
баланс, виньетка) к входному видео и сохраняет результат.

Usage:
  python3 texture_pass.py IN.mp4 OUT.mp4 --seed 42 --w 1080 --h 1080 [--fps 25] [--duration 60]
  --grain-min 8 --grain-max 20 --eq "contrast=1.05:brightness=-0.02" [--balance ...] [--vignette ...]
"""

import argparse
import random
import subprocess
import sys
from pathlib import Path

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
SCRATCH = ASSETS_DIR / "scratch_overlay.mp4"
GRIT = ASSETS_DIR / "grit_overlay.mp4"


def _probe_duration(path: str) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    return float(r.stdout.strip())


def apply_texture(
    in_path: str,
    out_path: str,
    seed: int,
    style: dict,
    w: int,
    h: int,
    fps: int = 25,
    duration: float | None = None,
    timeout: int = 600,
) -> None:
    # timeout=600 дефолт: на буке (Atom CPU) полный 1080p блендинг+noise на 30с клипе занимает
    # ~2.5-3 мин — старый дефолт 180с реально ловил TimeoutExpired (пойман вживую). GH Actions
    # раннер быстрее, но тяжёлый рендер всё равно принадлежит GH, не буку (feedback_render_on_gh_actions).
    if duration is None:
        # SCRATCH/GRIT идут с -stream_loop -1 (бесконечный луп) — без явного trim ffmpeg
        # зависает навечно (пойман вживую). Если длительность не передана — берём её из
        # самого in_path через ffprobe, всегда работаем с конечным duration.
        duration = _probe_duration(in_path)
    tex = random.Random(seed)
    scr_flip = ",hflip" if tex.random() < 0.5 else ""
    grt_flip = ",hflip" if tex.random() < 0.5 else ""
    scr_op = round(tex.uniform(0.5, 0.7), 2)
    grt_op = round(tex.uniform(0.4, 0.6), 2)
    
    # ЗЕРНО ВРЕМЕННО ОТКЛЮЧЕНО (yaromat 2026-07-03): один и тот же noise-паттерн читался
    # однотипно на всех клипах. Пока без него — вернуть, когда будет за что зацепиться
    # (per-track/per-scene вариативность зерна, не тот же noise=alls на каждом рендере).
    grain_on = bool(style.get("grain"))
    if grain_on:
        nz_str = tex.randint(style["grain"][0], style["grain"][1])
        nz_seed = tex.randint(1, 99999)
    scr_ss = round(tex.uniform(0.0, 4.0), 2)
    grt_ss = round(tex.uniform(0.0, 4.0), 2)

    grade = f"eq={style['eq']}"
    if style.get("balance"):
        grade += f",colorbalance={style['balance']}"

    # список непустых фрагментов постобработки, join через запятую — надёжнее ручной конкатенации
    # (лишняя/недостающая запятая на границе опциональных vignette/trim рвёт filter_complex).
    tail_parts = ["format=yuv420p"]
    if grain_on:
        tail_parts.append(f"noise=alls={nz_str}:all_seed={nz_seed}:allf=t+u")
    if style.get("vignette"):
        tail_parts.append(f"vignette={style['vignette']}")
    if duration is not None:
        tail_parts.append(f"trim=duration={duration}")
        tail_parts.append("setpts=PTS-STARTPTS")
    tail = ",".join(tail_parts)

    fc = (
        f"[0:v]fps={fps},{grade},"
        f"format=gbrp,setpts=PTS-STARTPTS[v];"
        f"[1:v]scale={w}:{h},fps={fps},format=gray{scr_flip},format=gbrp,setpts=PTS-STARTPTS[scr];"
        f"[2:v]scale={w}:{h},fps={fps},format=gray{grt_flip},format=gbrp,setpts=PTS-STARTPTS[grt];"
        f"[v][scr]blend=all_mode=screen:all_opacity={scr_op}[b1];"
        f"[b1][grt]blend=all_mode=screen:all_opacity={grt_op}[b2];"
        f"[b2]{tail}[vout]"
    )
    
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(in_path),
        "-stream_loop", "-1", "-ss", str(scr_ss), "-i", str(SCRATCH),
        "-stream_loop", "-1", "-ss", str(grt_ss), "-i", str(GRIT),
        "-filter_complex", fc, "-map", "[vout]", "-shortest",
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        "-r", str(fps), "-pix_fmt", "yuv420p", str(out_path),
    ]
    # -shortest ОБЯЗАТЕЛЕН: scratch/grit идут с -stream_loop -1 (бесконечный луп) — без -shortest
    # и без trim (duration=None) ffmpeg зависает навечно, ждя конца бесконечного потока
    # (пойман вживую: 3.5+ минуты без завершения на 5-секундном клипе, до фикса).

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffmpeg timeout ({timeout}s) — подвис или не успел на этом железе")

    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr}")
    
    out_file = Path(out_path)
    if not out_file.exists() or out_file.stat().st_size < 5000:
        raise RuntimeError(f"Output file not created or too small: {out_path}")


def main():
    parser = argparse.ArgumentParser(description="Apply texture pass (grain/scratch/grit) to video.")
    parser.add_argument("input", help="Input video file")
    parser.add_argument("output", help="Output video file")
    parser.add_argument("--seed", type=int, required=True, help="Random seed")
    parser.add_argument("--w", type=int, required=True, help="Width")
    parser.add_argument("--h", type=int, required=True, help="Height")
    parser.add_argument("--fps", type=int, default=25, help="Frame rate")
    parser.add_argument("--duration", type=float, default=None, help="Duration (seconds). If not set, full length.")
    parser.add_argument("--grain-min", type=int, default=None,
                        help="Grain min (омит вместе с --grain-max = БЕЗ зерна, дефолт 2026-07-03)")
    parser.add_argument("--grain-max", type=int, default=None, help="Grain max")
    parser.add_argument("--eq", type=str, required=True, help="FFmpeg eq parameters")
    parser.add_argument("--balance", type=str, default=None, help="FFmpeg colorbalance parameters")
    parser.add_argument("--vignette", type=str, default=None, help="FFmpeg vignette parameters")
    parser.add_argument("--timeout", type=int, default=600, help="ffmpeg timeout in seconds (default 600)")

    args = parser.parse_args()

    style = {
        "grain": (args.grain_min, args.grain_max) if args.grain_min is not None and args.grain_max is not None else None,
        "eq": args.eq,
        "balance": args.balance,
        "vignette": args.vignette,
    }

    try:
        apply_texture(
            args.input, args.output, args.seed, style,
            args.w, args.h, args.fps, args.duration, args.timeout
        )
        print(f"Texture pass applied: {args.output}")
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
