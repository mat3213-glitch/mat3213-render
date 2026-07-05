#!/usr/bin/env python3
"""
motion_gen.py — предрендер моторики для screenplay-EDL (клип «взрослый», пересборка 2026-07-05).

Читает storyboard.json (упорядоченный 14-кадровый EDL с полем `src`) и на КАЖДЫЙ кадр
производит готовый видео-клип `generated/scene_NNN.mp4`, который дальше собирает
storyboard_render_job.py (он движение НЕ добавляет — ждёт готовый видео-base):
  • src="still:NAME" → depth-параллакс из стилла src_stills/NAME.png (ядро parallax_planes.py),
    амплитуда по типу motion (макс. выраженное движение по запросу yaromat, но под порогом
    occlusion-артефактов 2.5D — это калибровочная ручка, не серебряная пуля; настоящий макс —
    i2v/wan2, отдельный слой A).
  • src="cc:VIDID" → CC-футаж cc_footage/VIDID.mp4: cover-crop в квадрат + trim под t_dur
    (нативное движение). motion="native_warm" → тёплый сдвиг грейда (разрыв серый→янтарь).

Формат квадрат 1:1 (SIZE): квадратные стиллы ложатся нативно без кроп-потери руки; parallax-ядро
квадратное. Слоу 0.75× применяется НЕ здесь, а на сборке (storyboard_render_job shot.speed).

Вход (ЯД render_jobs/<JOB_ID>/): storyboard.json, src_stills/*.png, cc_footage/*.mp4.
Выход: generated/scene_NNN.mp4 → ЯД render_jobs/<JOB_ID>/generated/.
Env: JOB_ID. Боевой на GH (torch/cv2/depth; на Atom не тянет).
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

import parallax_planes as pp   # переиспользуем depth+parallax ядро (без дублирования)

JOB_ID = os.environ.get("JOB_ID", "")
if not JOB_ID:
    sys.exit("JOB_ID not set")

REMOTE = "ydrive"
CF = "Content factory"
JOB_YD = f"{CF}/cloud_io/render_jobs/{JOB_ID}"
WORK = Path("/tmp/motion_gen")
WORK.mkdir(parents=True, exist_ok=True)

SIZE = 1080
FPS = 25
# Амплитуда параллакса по типу движения (доля кадра). Выше дефолта 0.10 — «макс движение»
# (yaromat), но под порогом резины. parallax_punch — пик S11 (кулак-удар), самый сильный проезд.
AMP = {"parallax": 0.16, "parallax_punch": 0.24}


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def yd_get(remote_path, local: Path) -> bool:
    local.parent.mkdir(parents=True, exist_ok=True)
    return sh(["rclone", "copyto", f"{REMOTE}:{remote_path}", str(local)]).returncode == 0


def yd_put(local: Path, remote_path) -> bool:
    return sh(["rclone", "copyto", str(local), f"{REMOTE}:{remote_path}"]).returncode == 0


def cover_cc(src: Path, dst: Path, dur: float, warm: bool) -> bool:
    """CC-футаж → квадрат SIZE cover-crop + trim под dur (короткий зацикливается). Опц. тёплый грейд."""
    vf = (f"scale={SIZE}:{SIZE}:force_original_aspect_ratio=increase,crop={SIZE}:{SIZE},"
          f"fps={FPS},setsar=1")
    if warm:
        # разрыв серый→янтарь: лёгкий тёплый сдвиг + чуть насыщенности (без неона)
        vf += ",eq=saturation=1.06:gamma=1.02,colorbalance=rm=0.06:gm=0.02:bm=-0.08"
    vf += ",format=yuv420p"
    r = sh(["ffmpeg", "-y", "-loglevel", "error", "-stream_loop", "-1",
            "-t", f"{dur:.3f}", "-i", str(src), "-vf", vf, "-an",
            "-r", str(FPS), "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", str(dst)])
    if r.returncode != 0:
        print(f"  cover_cc err: {r.stderr[-300:]}", flush=True)
    return r.returncode == 0 and dst.exists()


def parse_src(src: str):
    """'still:s01_hand_fog' → ('still','s01_hand_fog'); 'cc:-zMbx1p1dmQ' → ('cc','-zMbx1p1dmQ')."""
    kind, _, name = src.partition(":")
    return kind, name


def main():
    print(f"motion_gen job={JOB_ID}", flush=True)
    sb_file = WORK / "storyboard.json"
    if not yd_get(f"{JOB_YD}/storyboard.json", sb_file):
        sys.exit("нет storyboard.json на ЯД")
    sb = json.loads(sb_file.read_text(encoding="utf-8"))
    shots = sb.get("shots", [])
    if not shots:
        sys.exit("storyboard без shots")

    depth_pipe = None   # ленивая загрузка — только если есть stills
    done, fail = [], []
    for shot in shots:
        idx = int(shot["idx"])
        t_dur = max(0.4, float(shot["t_dur"]))
        motion = shot.get("motion", "parallax")
        kind, name = parse_src(shot["src"])
        out = WORK / f"scene_{idx:03d}.mp4"
        print(f"── scene {idx:03d}: {shot['src']} motion={motion} t={t_dur}s", flush=True)

        try:
            if kind == "still":
                still = WORK / f"{name}.png"
                if not yd_get(f"{JOB_YD}/src_stills/{name}.png", still):
                    print(f"  ✗ нет стилла {name}", flush=True); fail.append(idx); continue
                if depth_pipe is None:
                    depth_pipe = pp.load_depth_model()
                depth = pp.estimate_depth(depth_pipe, Image.open(still).convert("RGB"))
                nframes = int(round(t_dur * FPS))
                os.environ["PARALLAX_AMP"] = str(AMP.get(motion, AMP["parallax"]))
                frames = pp.make_parallax_frames(str(still), depth, num_frames=nframes, size=SIZE)
                if not pp.write_video(frames, str(out), fps=FPS):
                    print("  ✗ parallax видео не записалось", flush=True); fail.append(idx); continue
            elif kind == "cc":
                clip = WORK / f"cc_{name}.mp4"
                if not yd_get(f"{JOB_YD}/cc_footage/{name}.mp4", clip):
                    print(f"  ✗ нет CC {name}", flush=True); fail.append(idx); continue
                warm = motion == "native_warm"
                if not cover_cc(clip, out, t_dur, warm):
                    fail.append(idx); continue
            else:
                print(f"  ✗ неизвестный src-kind {kind}", flush=True); fail.append(idx); continue
        except Exception as e:
            print(f"  ✗ ошибка scene {idx}: {e}", flush=True); fail.append(idx); continue

        if yd_put(out, f"{JOB_YD}/generated/scene_{idx:03d}.mp4"):
            done.append(idx)
            print(f"  ✓ scene_{idx:03d}.mp4 → ЯД", flush=True)
        else:
            print(f"  ✗ upload scene {idx}", flush=True); fail.append(idx)

    print(f"\nmotion_gen ГОТОВО: ✅ {len(done)}  ❌ {len(fail)} ({fail or '—'})", flush=True)
    if fail:
        sys.exit(f"{len(fail)} сцен не сгенерированы")


if __name__ == "__main__":
    main()
