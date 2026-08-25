#!/usr/bin/env python3
"""
Batch uniqueizer for Pinterest board raw videos on GitHub Actions.

Downloads a raw YaD folder, runs the same FFmpeg uniqueization recipe over every
MP4, and uploads the result into a sibling uniq/ folder.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

def sh(cmd: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def probe_duration(path: Path) -> float:
    r = sh([
        "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
        "-of", "csv=p=0", str(path)
    ], timeout=60)
    try:
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def probe_fps(path: Path) -> float:
    r = sh([
        "ffprobe", "-v", "quiet", "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate",
        "-of", "csv=p=0", str(path)
    ], timeout=60)
    try:
        num, den = r.stdout.strip().split("/")
        fps = float(num) / float(den)
        return fps if fps > 0 else 24.0
    except Exception:
        return 24.0


def color_chain(kind: str, fps: float) -> str:
    """Цветовой вариант поверх базового рецепта (кроме strobo — он через граф). "" = без цвета."""
    if kind == "invert":
        return "negate"
    if kind == "bright":
        hue = random.randint(0, 359)
        sat = round(random.uniform(1.7, 2.6), 2)
        con = round(random.uniform(1.06, 1.18), 3)
        bri = round(random.uniform(0.02, 0.05), 3)
        return f"hue=h={hue}:s={sat},eq=contrast={con}:brightness={bri}"
    return ""


def strobo_graph(duration: float, fps: float, in_label: str = "0:v",
                 out_label: str = "vs") -> tuple[str, int]:
    """Стробо: чередование ярких и инвертированных сегментов (trim/negate/concat).
    Возвращает (граф для -filter_complex, число сегментов). Яркая фаза = базовая картинка
    с усиленной яркостью, инверсная = negate."""
    freq = round(random.uniform(1.6, 4.0), 2)
    period = max(2, int(round(fps / freq)))
    total_frames = max(period * 2, int(math.ceil(duration * fps)))
    segs = min(120, max(2, math.ceil(total_frames / period)))
    head = f"[{in_label}]split={segs}" + "".join(f"[g{i}]" for i in range(segs))
    parts = [head]
    labels = []
    for i in range(segs):
        start = i * period
        end = min((i + 1) * period, total_frames)
        effect = "" if i % 2 == 0 else ",negate,eq=brightness=0.08"
        parts.append(
            f"[g{i}]trim=start_frame={start}:end_frame={end},setpts=PTS-STARTPTS{effect}[v{i}]"
        )
        labels.append(f"[v{i}]")
    parts.append("".join(labels) + f"concat=n={segs}:v=1:a=0[{out_label}]")
    return ";".join(parts), segs


def uniquize(src: Path, dst: Path, *, color: str = "", fps: float = 24.0) -> None:
    speed = round(random.uniform(0.97, 1.03), 3)
    pts_factor = round(1.0 / speed, 4)
    flip = random.choice(["hflip,", ""])
    crop_pct = round(random.uniform(0.93, 0.96), 3)
    margin = round((1.0 - crop_pct) / 2, 4)
    crop = f"crop=iw*{crop_pct}:ih*{crop_pct}:iw*{margin}:ih*{margin},"
    rr = round(random.uniform(0.84, 0.90), 3)
    gg = round(random.uniform(0.89, 0.93), 3)
    bb = round(random.uniform(1.07, 1.12), 3)
    color_mix = f"colorchannelmixer=rr={rr}:gg={gg}:bb={bb},"
    sat = round(random.uniform(0.75, 0.85), 3)
    con = round(random.uniform(1.04, 1.09), 3)
    bri = round(random.uniform(0.02, 0.06), 3)
    eq = f"eq=saturation={sat}:contrast={con}:brightness={bri},"
    noise_str = random.randint(6, 11)
    noise = f"noise=alls={noise_str}:allf=t+u,"
    unsharp = "unsharp=3:3:0.4:3:3:0.0,"
    shake_amp_x = random.randint(10, 18)
    shake_amp_y = random.randint(6, 12)
    margin_x = shake_amp_x + 6
    margin_y = shake_amp_y + 4
    crop_w = 1280 - 2 * margin_x
    crop_h = 720 - 2 * margin_y
    base_freq = random.uniform(10.0, 13.5)
    freq_x = round(base_freq, 1)
    freq_y = round(base_freq * random.uniform(0.7, 0.85), 1)
    shake = (
        f"crop={crop_w}:{crop_h}:"
        f"'{margin_x}+{shake_amp_x}*sin(t*{freq_x})':"
        f"'{margin_y}+{shake_amp_y}*cos(t*{freq_y})',"
        f"scale=1280:720,"
    )
    vignette = f"vignette=PI*{round(random.uniform(0.22, 0.30), 2)}"
    chain = [
        flip,
        crop,
        "scale=1280:720",
        f"setpts={pts_factor}*PTS",
        shake,
        color_mix,
        eq,
        noise,
        unsharp,
        vignette,
    ]
    color_kind = color
    color = color_chain(color, fps)
    if color:
        chain.append(color)
    vf = ",".join(c.rstrip(",") for c in chain if c)

    probe = sh([
        "ffprobe", "-v", "quiet", "-select_streams", "a",
        "-show_entries", "stream=index", "-of", "csv=p=0", str(src)
    ], timeout=30)
    has_audio = bool(probe.stdout.strip())

    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]
    if color_kind == "strobo":
        graph, segs = strobo_graph(probe_duration(src), fps, in_label="pre")
        graph = f"[0:v]{vf}[pre];{graph}"
        if has_audio:
            cmd += [
                "-filter_complex", f"{graph};[0:a]atempo={speed}[a]",
                "-map", "[vs]", "-map", "[a]",
                "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "160k",
            ]
        else:
            cmd += ["-filter_complex", graph, "-map", "[vs]", "-an"]
    elif has_audio:
        cmd += [
            "-filter_complex",
            f"[0:v]{vf}[v];[0:a]atempo={speed}[a]",
            "-map", "[v]", "-map", "[a]",
            "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "160k",
        ]
    else:
        cmd += ["-vf", vf, "-an"]
    cmd += [
        "-threads", "2",
        "-c:v", "libx264", "-profile:v", "baseline", "-level:v", "3.1",
        "-pix_fmt", "yuv420p", "-preset", "fast", "-crf", "23",
        "-movflags", "+faststart",
        str(dst),
    ]

    r = sh(cmd, timeout=900)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-600:])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-folder", required=True, help="YaD raw folder, without ydrive:")
    ap.add_argument("--color", choices=["off", "random", "all"], default="random",
                    help="цветовой вариант: off=как раньше, random=1 случайный на клип, "
                         "all=три файла (invert/bright/strobo) на клип")
    args = ap.parse_args()

    try:
        os.nice(15)
    except Exception:
        pass

    source_folder = args.source_folder.rstrip("/")
    if source_folder.endswith("/raw"):
        dest_folder = source_folder[:-4] + "/uniq"
    else:
        dest_folder = source_folder + "/uniq"

    work_root = Path(tempfile.mkdtemp(prefix="pinterest_board_uniquize_"))
    raw_local = work_root / "raw"
    uniq_local = work_root / "uniq"
    raw_local.mkdir(parents=True, exist_ok=True)
    uniq_local.mkdir(parents=True, exist_ok=True)

    r = sh(["rclone", "copy", f"ydrive:{source_folder}", str(raw_local)], timeout=1800)
    if r.returncode != 0:
        print(r.stderr[-1000:], flush=True)
        return 2

    sources = sorted(p for p in raw_local.glob("*.mp4") if p.is_file())
    if not sources:
        print(f"[uniq] no mp4 files in {source_folder}", flush=True)
        return 1

    ok = 0
    failures: list[dict] = []
    records: list[dict] = []
    color_kinds = ["invert", "bright", "strobo"]
    for src in sources:
        fps = probe_fps(src)
        if args.color == "all":
            jobs = [(c, uniq_local / f"{src.stem}_uniq_{c}.mp4") for c in color_kinds]
        elif args.color == "random":
            jobs = [(random.choice(color_kinds), uniq_local / f"{src.stem}_uniq.mp4")]
        else:
            jobs = [("", uniq_local / f"{src.stem}_uniq.mp4")]
        for color, dst in jobs:
            print(f"[uniq] {src.name} -> {dst.name} (color={color or 'off'}, fps={fps:.0f})", flush=True)
            try:
                uniquize(src, dst, color=color, fps=fps)
                records.append(
                    {
                        "source": src.name,
                        "output": dst.name,
                        "color": color,
                        "source_bytes": src.stat().st_size,
                        "output_bytes": dst.stat().st_size,
                        "source_duration": round(probe_duration(src), 3),
                    }
                )
                ok += 1
            except Exception as exc:
                failures.append({"source": src.name, "color": color, "error": str(exc)})
                print(f"[uniq] FAIL {src.name} (color={color}): {exc}", flush=True)

    (uniq_local / "unique_manifest.json").write_text(
        json.dumps(
            {
                "source_folder": source_folder,
                "dest_folder": dest_folder,
                "color_mode": args.color,
                "count_source": len(sources),
                "count_ok": ok,
                "count_failed": len(failures),
                "records": records,
                "failures": failures,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    r = sh(["rclone", "mkdir", f"ydrive:{dest_folder}"], timeout=120)
    if r.returncode != 0:
        print(r.stderr[-1000:], flush=True)
        return 2
    r = sh(["rclone", "copy", str(uniq_local), f"ydrive:{dest_folder}"], timeout=1800)
    if r.returncode != 0:
        print(r.stderr[-1000:], flush=True)
        return 2

    print(f"[uniq] uploaded -> ydrive:{dest_folder}", flush=True)
    print(f"[uniq] summary ok={ok} failed={len(failures)} total={len(sources)}", flush=True)
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
