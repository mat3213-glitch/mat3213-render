#!/usr/bin/env python3
"""
scene_cut_pool.py — нарезка RAW-видео-источников на лупы по РЕАЛЬНЫМ сценам
(PySceneDetect ContentDetector) + добивка равномерной дробью самых длинных до
целевого числа луп. Каждый луп затем уникализируется ОДНИМ случайным
инструментом (single-effect, base=False) — «1 уникализатор на 1 отрезок».

Зачем: для режима video_pool движка vzrosly_clip_job пул из N дискретных
источников даёт больше комбинаций монтажа, чем 18 длинных файлов. Режем по
фактическим сменам движения (не арифметикой), чтобы луп попадал в реальную
границу сцены.

Пайплайн (только GH Actions, бук не грузим):
  1. скачать raw/*.mp4 с ЯД (результат pinterest_board_fetch)
  2. на каждый файл — PySceneDetect -> границы сцен
  3. нарезать ffmpeg на сегменты сцен, сохранить 1280x720 landscape (как в исходнике)
  4. если сцен < target — раздробить самые длинные сегменты равномерно до ~target
  5. каждый луп — 1 случайный эффект (uniquize base=False)
  6. залить в sibling pool/ на ЯД

Usage:
  python3 scene_cut_pool.py --source-folder "Content factory/cloud_io/.../raw" \
      --target 54 --out-base "Content factory/cloud_io/.../pool"
Env: rclone "ydrive:" через ~/.config/rclone (как в flow).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from collections import OrderedDict

from pinterest_board_uniquize import uniquize, pick_chain, load_effects

REMOTE = "ydrive:"
MIN_SEG = 1.2          # не резать короче этого (иначе мусор)
TARGET_DEFAULT = 54


def sh(cmd, timeout=300):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def probe_duration(path: Path) -> float:
    r = sh(["ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "csv=p=0", str(path)], timeout=60)
    try:
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def scene_cuts(path: Path, threshold: float = 27.0) -> list[float]:
    """Вернуть времена смен сцен (секунды) через PySceneDetect ContentDetector."""
    try:
        from scenedetect import detect, ContentDetector
        cuts = detect(str(path), ContentDetector(threshold=threshold))
    except Exception as e:
        print(f"  [cuts] scenedetect fail ({e}) → 1 сцена", flush=True)
        return []
    return [c[0].get_seconds() for c in cuts]


def split_segment(inp: Path, seg: tuple[float, float], dst: Path) -> bool:
    s, e = seg
    w, h = 1280, 720
    dur = e - s
    if dur < MIN_SEG:
        return False
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{s:.3f}", "-t", f"{dur:.3f}", "-i", str(inp),
           "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-pix_fmt", "yuv420p", "-an", str(dst)]
    r = sh(cmd, timeout=180)
    if r.returncode != 0:
        print(f"  ✗ {dst.name}: {r.stderr[-300:]}", flush=True)
        return False
    return dst.exists() and dst.stat().st_size > 1000


def cut_to_target(segments: list[tuple[float, float]], target: int,
                  min_seg: float = MIN_SEG) -> list[tuple[float, float]]:
    """Из списка (start,end) сегментов добить до target равномерной дробью самых длинных."""
    segs = [list(s) for s in segments]
    while len(segs) < target:
        # отсортировать по длине убыв, дробим самый длинный пополам
        longest = max(range(len(segs)), key=lambda i: segs[i][1] - segs[i][0])
        s, e = segs[longest]
        dur = e - s
        if dur < 2 * min_seg:
            break
        mid = s + dur / 2.0
        segs[longest] = [s, mid]
        segs.append([mid, e])
    segs.sort(key=lambda x: x[0])
    # отфильтровать слишком короткие
    return [tuple(t) for t in segs if (t[1] - t[0]) >= min_seg]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-folder", required=True,
                    help="ЯД папка с уникализированными видео (uniq)")
    ap.add_argument("--out-base", required=True,
                    help="ЯД базовая папка для pool/ (создастся pool/)")
    ap.add_argument("--target", type=int, default=TARGET_DEFAULT,
                    help="целевое число луп (~54)")
    a = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="scene_cut_"))
    src_dir = work / "src"; src_dir.mkdir()
    out_work = work / "out"; out_work.mkdir()

    # 1. список исходников
    r = sh(["rclone", "lsf", f"{REMOTE}{a.source_folder}"])
    if r.returncode != 0:
        sys.exit(f"rclone lsf failed: {r.stderr}")
    names = sorted(n.strip() for n in r.stdout.splitlines()
                   if n.lower().endswith((".mp4", ".mov", ".mkv", ".webm")))
    if not names:
        sys.exit("нет видео в source-folder")
    print(f"[pool] источников: {len(names)}", flush=True)

    # 2. скачать
    files = []
    for name in names:
        dst = src_dir / name
        if sh(["rclone", "copyto", f"{REMOTE}{a.source_folder}/{name}", str(dst)]).returncode != 0:
            print(f"  ✗ скачать {name}", flush=True)
            continue
        files.append(dst)
    print(f"[pool] скачано: {len(files)}", flush=True)

    # 3. нарезать по сценам
    all_segs = OrderedDict()   # key -> [(start,end)]
    for i, f in enumerate(files):
        dur = probe_duration(f)
        cuts = scene_cuts(f)
        bounds = [0.0] + cuts + [dur]
        segs = []
        for j in range(len(bounds) - 1):
            s, e = bounds[j], bounds[j + 1]
            if e - s >= MIN_SEG:
                segs.append((s, min(e, dur)))
        # если сцен НЕТ или файл суперкороткий — один сегмент
        if not segs:
            segs = [(0.0, max(dur, MIN_SEG))]
        all_segs[f.parent.name if False else f.name] = segs
        print(f"[pool] {f.name}: dur={dur:.1f}s сцен={len(cuts)} → сегментов={len(segs)}", flush=True)

    # 4. добить до target: распилить самые длинные сегменты по всему пулу
    flat = []
    for name, ss in all_segs.items():
        for seg in ss:
            flat.append((name, seg))
    flat_sorted = sorted(flat, key=lambda x: (x[1][1] - x[1][0]), reverse=True)
    all_work = [tuple(x) for x in flat_sorted]
    # дробить самый длинный пополам пока не хватает
    while len(all_work) < a.target:
        # найти самый длинный, разбить пополам
        if not all_work:
            break
        longest_i = max(range(len(all_work)), key=lambda i: all_work[i][1][1] - all_work[i][1][0])
        name, seg = all_work[longest_i]
        s, e = seg
        if e - s < 2 * MIN_SEG:
            break
        mid = s + (e - s) / 2.0
        all_work[longest_i] = (name, (s, mid))
        all_work.append((name, (mid, e)))

    print(f"[pool] после добивки: {len(all_work)} луп (target {a.target})", flush=True)
    if len(all_work) < a.target:
        print(f"  ⚠ не добрали: {len(all_work)} < {a.target}", flush=True)

    # 5. нарезать, каждый луп — 1 случайный уникализатор (single-effect), залить
    effects_db = load_effects()
    uploaded = 0
    for idx, (name, seg) in enumerate(all_work):
        out_name = f"loop_{idx:03d}.mp4"
        out = out_work / out_name
        src = src_dir / name
        s, e = seg
        chain = pick_chain(effects_db)
        try:
            uniquize(src, out, effects_chain=chain, effects_db=effects_db,
                     base=False, in_ss=s, in_t=e - s, out_wh=(1280, 720), drop_audio=True)
        except Exception as ex:
            print(f"  ✗ {out_name}: uniquize fail ({ex})", flush=True)
            continue
        if sh(["rclone", "copyto", str(out),
               f"{REMOTE}{a.out_base}/pool/{out_name}"]).returncode != 0:
            print(f"  ✗ upload {out_name}", flush=True)
            continue
        uploaded += 1

    # манифест
    m_simple = [{"loop": i, "src": nm, "start": round(seg[0], 2), "end": round(seg[1], 2)}
                for i, (nm, seg) in enumerate(all_work)]
    mp = work / "manifest.json"
    mp.write_text(json.dumps(m_simple, ensure_ascii=False, indent=2), encoding="utf-8")
    sh(["rclone", "copyto", str(mp), f"{REMOTE}{a.out_base}/pool/manifest.json"])

    print(f"[pool] ИТОГО залито: {uploaded} луп → {a.out_base}/pool/", flush=True)


if __name__ == "__main__":
    main()
