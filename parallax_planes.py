#!/usr/bin/env python3
"""
parallax_planes.py — генератор video_keys через 2.5D depth-параллакс.

Альтернатива внешнему Hunyuan i2v (animate_planes.py): из ОДНОГО стилла делает план с
медленным объёмным движением камеры. Depth считается ОДИН раз на стилл (Depth-Anything-V2),
дальше дешёвый numpy/cv2 warp по кадрам → по времени на GH укладывается (в отличие от
покадрового i2v/upscale). Без внешних API и без вотермарков.

Контракт video_keys идентичен animate_planes.py: стилл <name>.png → <name>.mp4 → залить под
всеми ключами STILL_TO_KEYS + патч job.json (scenario=kenburns + video_keys).

Запуск (боевой — на GH Actions, воркфлоу parallax_planes.yml; локально cv2/torch на Atom нет):
  python3 parallax_planes.py --job-id 2026-06-26_bilet_i2v_probe
  python3 parallax_planes.py --job-id <id> --stills anchor,cold_01

Эстетика (жёстко): движение ОЧЕНЬ тонкое/медленное — «фотографическое дыхание», один мягкий
дрейф камеры малой амплитуды. НЕ повышаем резкость/не деноайзим/не апскейлим — мягкость
исходника сохраняется как есть (см. память feedback_no_upscale_sharpen). Без текста/лиц/неона.

Черновик gh-mimo + ревью-доводка Claude (2026-06-29): фиксы INTER_LINEAR, направление
параллакса (ближе=сильнее), убрана occlusion-маска, гасившая эффект, один плавный sin-проход.
"""
import argparse
import json
import os
import subprocess
import tempfile
import shutil
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

YD = "ydrive:Content factory/cloud_io/render_jobs"

STILL_TO_KEYS = {
    "anchor":  ["anchor", "anchorp"],
    "child":   ["child"],
    "cold_01": ["c1", "c1f"], "cold_02": ["c2", "c2f"],
    "cold_03": ["c3"],        "cold_04": ["c4", "c4f"],
    "crowd":   ["crowd"],     "clock":   ["clock"],
    "art1":    ["a1"],        "art2":    ["a2", "a2f"], "art4": ["a4"],
}


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def load_depth_model():
    from transformers import pipeline as hf_pipeline
    return hf_pipeline("depth-estimation", model="depth-anything/Depth-Anything-V2-Small-hf")


def estimate_depth(depth_pipe, image_pil):
    """Возвращает карту глубины [0..1], где 1 = ближе к камере (для параллакса)."""
    result = depth_pipe(image_pil)
    depth = np.array(result["depth"], dtype=np.float32)
    dmin, dmax = float(depth.min()), float(depth.max())
    if dmax - dmin > 1e-6:
        depth = (depth - dmin) / (dmax - dmin)
    else:
        depth = np.zeros_like(depth)
    return depth


def make_parallax_frames(still_path, depth, num_frames=150, size=960):
    """2.5D параллакс: ближние пиксели (depth→1) смещаются сильнее дальних.
    Траектория — один мягкий sin-проход (медленное «дыхание»), малая амплитуда."""
    img = cv2.imread(still_path)
    h0, w0 = img.shape[:2]
    scale = size / min(h0, w0)
    nw, nh = int(round(w0 * scale)), int(round(h0 * scale))
    img_s = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    depth_s = cv2.resize(depth, (nw, nh), interpolation=cv2.INTER_LINEAR)

    # центр-кроп size×size
    cy, cx = nh // 2, nw // 2
    half = size // 2
    y1 = min(max(0, cy - half), max(0, nh - size))
    x1 = min(max(0, cx - half), max(0, nw - size))
    crop = img_s[y1:y1 + size, x1:x1 + size].copy()
    cd = depth_s[y1:y1 + size, x1:x1 + size].astype(np.float32)

    # Амплитуда параллакса — доля кадра, ТЮНИТСЯ через env PARALLAX_AMP (без правки кода).
    # Дефолт 0.10 (макс. выраженное движение по запросу yaromat: «увеличь на максимум, статично»).
    # Y берём 0.6 от X. На больших значениях возможны occlusion-артефакты по краям объектов —
    # это естественный предел 2.5D-параллакса из одного кадра (это калибровочная ручка).
    amp_frac = float(os.environ.get("PARALLAX_AMP") or "0.10")
    amp_x = size * amp_frac
    amp_y = size * amp_frac * 0.6
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)

    frames = []
    for i in range(num_frames):
        t = i / max(1, num_frames - 1)          # 0..1
        ease = t * t * (3.0 - 2.0 * t)          # smoothstep 0..1 (плавный старт/стоп)
        s = ease * 2.0 - 1.0                    # -1 → +1: МОНОТОННЫЙ проезд камеры через сцену
        sx = amp_x * s
        sy = amp_y * s * 0.5
        dx = sx * cd                            # ближе (cd→1) двигается сильнее = параллакс
        dy = sy * cd
        map_x = (xx - dx).astype(np.float32)
        map_y = (yy - dy).astype(np.float32)
        warp = cv2.remap(crop, map_x, map_y, cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_REFLECT_101)
        frames.append(warp)
    return frames


def write_video(frames, output_path, fps=25):
    h, w = frames[0].shape[:2]
    tmpdir = tempfile.mkdtemp(prefix="parallax_frames_")
    try:
        for i, frame in enumerate(frames):
            cv2.imwrite(os.path.join(tmpdir, f"frame_{i:05d}.png"), frame)
        sh([
            "ffmpeg", "-y", "-framerate", str(fps),
            "-i", os.path.join(tmpdir, "frame_%05d.png"),
            "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p",
            "-an", output_path,
        ])
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return os.path.exists(output_path) and os.path.getsize(output_path) > 10000


def main():
    ap = argparse.ArgumentParser(description="depth-параллакс генератор video_keys")
    ap.add_argument("--job-id", required=True, type=str)
    ap.add_argument("--stills", type=str, default=None)
    ap.add_argument("--keep-collage", action="store_true")
    args = ap.parse_args()

    base = f"{YD}/{args.job_id}"
    workdir = tempfile.mkdtemp(prefix=f"parallax_{args.job_id}_")
    print(f"[parallax] job={args.job_id} work={workdir}")

    # источник списка планов: gen_job.json если есть, иначе *.png в папке джоба ∩ маппинг.
    # (parallax-у нужен только список ИМЁН — промпт движения, в отличие от Hunyuan i2v, не требуется.)
    local_gen = os.path.join(workdir, "gen_job.json")
    if sh(["rclone", "copyto", f"{base}/gen_job.json", local_gen]).returncode == 0:
        names = [it["name"] for it in
                 json.loads(Path(local_gen).read_text(encoding="utf-8")).get("items", [])]
        src = "gen_job.json"
    else:
        lsf = sh(["rclone", "lsf", base, "--include", "*.png"])
        pngs = {Path(x).stem for x in lsf.stdout.split()}
        names = [n for n in STILL_TO_KEYS if n in pngs]
        src = "png-листинг (фолбэк, нет gen_job.json)"
    if args.stills:
        want = {s.strip() for s in args.stills.split(",") if s.strip()}
        names = [n for n in names if n in want]
    items = [{"name": n} for n in names]
    print(f"[parallax] планов: {len(items)} (источник: {src})")

    depth_pipe = load_depth_model()
    video_keys, done, fail = [], [], []
    for idx, item in enumerate(items, 1):
        name = item["name"]
        keys = STILL_TO_KEYS.get(name)
        if not keys:
            print(f"  [{idx}/{len(items)}] {name}: нет в маппинге — пропуск")
            continue
        still_png = os.path.join(workdir, f"{name}.png")
        if sh(["rclone", "copyto", f"{base}/{name}.png", still_png]).returncode != 0:
            print(f"  [{idx}/{len(items)}] {name}: нет стилла на ЯД — пропуск")
            fail.append(name)
            continue
        print(f"  [{idx}/{len(items)}] {name} → depth+parallax (ключи {keys})")
        try:
            depth = estimate_depth(depth_pipe, Image.open(still_png).convert("RGB"))
            frames = make_parallax_frames(still_png, depth, num_frames=150, size=960)
            mp4 = os.path.join(workdir, f"{name}.mp4")
            if not write_video(frames, mp4):
                print(f"    [{name}] видео не записалось — пропуск")
                fail.append(name)
                continue
        except Exception as e:
            print(f"    [{name}] ошибка параллакса: {e}")
            fail.append(name)
            continue
        for k in keys:
            sh(["rclone", "copyto", mp4, f"{base}/{k}.mp4"])
            video_keys.append(k)
        done.append(name)

    if not video_keys:
        print("[parallax] ни один план не сгенерирован — стоп")
        return

    local_job = os.path.join(workdir, "job.json")
    if sh(["rclone", "copyto", f"{base}/job.json", local_job]).returncode == 0:
        jj = json.loads(Path(local_job).read_text(encoding="utf-8"))
    else:
        jj = {}
    if not args.keep_collage:
        jj["scenario"] = "kenburns"
    jj["video_keys"] = sorted(set(video_keys))
    Path(local_job).write_text(json.dumps(jj, ensure_ascii=False, indent=2), encoding="utf-8")
    sh(["rclone", "copyto", local_job, f"{base}/job.json"])

    print(f"\n[parallax] ГОТОВО: ✅ {len(done)}  ❌ {len(fail)} ({fail or '—'})")
    print(f"  video_keys={jj['video_keys']}")
    print(f'  → рендер: gh workflow run "Vzrosly Clip" -f job_id="{args.job_id}"')


if __name__ == "__main__":
    main()
