#!/usr/bin/env python3
"""
loop_finish_job.py — сборка лупа qwen_6 v10. Грейд = ground truth принятого v9.

Стадии (порядок ВАЖЕН):
  1. юнит: источник 5.04с → minterpolate 2x → ~10.07с @25fps  (setpts запрещён — забракован)
  2. сборка: юнит ×3 через xfade 0.6с → 29с
  3. бит-пасс (OpenCV) ПО СОБРАННЫМ 29с: зум-дыхание от басовой огибающей + пульс яркости
  4. финиш: параллакс-свип + грейд + виньетка + шум + оверлей screen-блендом + мукс

⚠️ ГРАБЛЯ 1: бит-пасс ТОЛЬКО после сборки. На 10с-юните огибающая 29с трека сожмётся в юнит и
   повторится трижды → реакция разъедется с музыкой. Синхрон с битом = смысл задачи.
⚠️ ГРАБЛЯ 2: screen-бленд ТОЛЬКО в gbrp. На yuv420p блендятся плоскости цветности:
   screen(0.5,0.5)=0.75 → U,V вверх → синий+красный = ПУРПУР (замерено: сдвиг +68).

Ручки (env): ZOOM_AMP=0.02, AR_AMP=0.10, NOISE_SIGMA=26 (0=выкл), GRAIN_FILE/GRAIN_RCLONE,
GRAIN_OPACITY=0.35, AR_GRAIN=0. A/B синтетика-vs-оверлей — переменными, без правки кода.
"""
import os
import shutil
import subprocess
import sys
import tempfile

import cv2
import librosa
import numpy as np
from scipy.signal import butter, filtfilt

UNIT_LEN = 10.0697
XFADE_DUR = 0.6
XFADE_OFF1 = 9.4697
XFADE_OFF2 = 18.9394
OUT_DUR = 29.0
AUDIO_SS = 33.0
FPS = 25
W, H = 1920, 1080
SRC_REMOTE = ("ydrive:Content factory/cloud_io/pool_gate/"
              "adult_dnb_2026-07-11/qwen_6_clip.mp4")
OUT_REMOTE = ("ydrive:Content factory/cloud_io/preview/"
              "qwen_6_blue_2026-07-16/qwen_6_v10.mp4")
MAX_BYTES = 19 * 1024 * 1024


def sh(cmd, check=False, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if check and r.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(str(c) for c in cmd)}\n{r.stderr}")
    return r


def write_status(job_id, text):
    sh(["rclone", "rcat",
        f"ydrive:Content factory/cloud_io/render_jobs/{job_id}/status.txt"], input=text)


def probe_wh(path):
    r = sh(["ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=p=0", path], check=True)
    p = r.stdout.strip().split(",")
    return int(p[0]), int(p[1])


def env_float(name, default):
    raw = os.environ.get(name)
    return float(default) if raw is None or raw == "" else float(raw)


def main():
    tmp = tempfile.mkdtemp(prefix="loop_finish_")
    job_id = os.environ.get("JOB_ID", "unknown")
    try:
        _run(tmp)
    except Exception as e:
        try:
            write_status(job_id, f"error: {e}")
        except Exception as se:
            print(f"status write failed: {se}", file=sys.stderr)
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run(tmp):
    job_id = os.environ.get("JOB_ID")
    if not job_id:
        raise RuntimeError("JOB_ID not set")

    zoom_amp = env_float("ZOOM_AMP", 0.02)
    ar_amp = env_float("AR_AMP", 0.10)
    ar_grain = env_float("AR_GRAIN", 0)
    grain_opacity = env_float("GRAIN_OPACITY", 0.35)
    noise_sigma = int(env_float("NOISE_SIGMA", 26))

    grain_file = os.environ.get("GRAIN_FILE", "").strip()
    grain_rclone = os.environ.get("GRAIN_RCLONE", "").strip()
    if not grain_rclone and grain_file:
        grain_rclone = f"ydrive:Content factory/assets/overlay_assets/board/{grain_file}"

    job_base = f"ydrive:Content factory/cloud_io/render_jobs/{job_id}"
    src = os.path.join(tmp, "source.mp4")
    track = os.path.join(tmp, "track.mp3")
    print("Stage 0: fetch inputs")
    sh(["rclone", "copyto", SRC_REMOTE, src], check=True)
    sh(["rclone", "copyto", f"{job_base}/track.mp3", track], check=True)

    grain_local = None
    if grain_rclone:
        grain_local = os.path.join(tmp, "grain.mp4")
        print(f"Fetching overlay: {grain_rclone}")
        sh(["rclone", "copyto", grain_rclone, grain_local], check=True)

    # --- Stage 1: unit (minterpolate 2x; setpts is forbidden) ---
    print("Stage 1: minterpolate 2x unit")
    unit = os.path.join(tmp, "unit.mp4")
    sh(["ffmpeg", "-y", "-itsscale", "2", "-i", src,
        "-vf", f"minterpolate=mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1:fps={FPS}",
        "-an", "-c:v", "libx264", "-crf", "16", "-preset", "veryfast",
        "-pix_fmt", "yuv420p", unit], check=True)

    # --- Stage 2: assemble 29s BEFORE the beat pass (ГРАБЛЯ 1) ---
    print("Stage 2: xfade assembly -> 29s")
    loop29 = os.path.join(tmp, "loop29.mp4")
    fc_asm = (
        f"[0:v]split=3[s0][s1][s2];"
        f"[s0]trim=0:{UNIT_LEN},setpts=PTS-STARTPTS[a];"
        f"[s1]trim=0:{UNIT_LEN},setpts=PTS-STARTPTS[b];"
        f"[s2]trim=0:{UNIT_LEN},setpts=PTS-STARTPTS[c];"
        f"[a][b]xfade=transition=fade:duration={XFADE_DUR}:offset={XFADE_OFF1}[ab];"
        f"[ab][c]xfade=transition=fade:duration={XFADE_DUR}:offset={XFADE_OFF2}[v]"
    )
    sh(["ffmpeg", "-y", "-i", unit, "-filter_complex", fc_asm, "-map", "[v]",
        "-t", str(OUT_DUR), "-an", "-c:v", "libx264", "-crf", "16",
        "-preset", "veryfast", "-pix_fmt", "yuv420p", loop29], check=True)

    # --- Stage 3: beat pass over the assembled 29s ---
    print("Stage 3: beat-reactive pass")
    wav = os.path.join(tmp, "seg.wav")
    sh(["ffmpeg", "-y", "-ss", str(AUDIO_SS), "-t", str(OUT_DUR), "-i", track,
        "-ac", "1", "-ar", "22050", wav], check=True)
    y, sr = librosa.load(wav, sr=22050, mono=True)

    hop = 512
    b_coef, a_coef = butter(4, 150.0 / (sr / 2.0), btype="low")
    bass_rms = librosa.feature.rms(y=filtfilt(b_coef, a_coef, y), hop_length=hop)[0]
    full_rms = librosa.feature.rms(y=y, hop_length=hop)[0]

    def norm01(sig):
        p5, p95 = np.percentile(sig, 5), np.percentile(sig, 95)
        if p95 - p5 < 1e-9:
            return np.full_like(sig, 0.5, dtype=np.float64)
        return np.clip((sig - p5) / (p95 - p5), 0.0, 1.0)

    win = max(1, int(0.175 * sr / hop))
    kern = np.ones(win, dtype=np.float64) / win
    bass_env = np.convolve(norm01(bass_rms), kern, mode="same")
    full_env = np.convolve(norm01(full_rms), kern, mode="same")

    cap = cv2.VideoCapture(loop29)
    fps = cap.get(cv2.CAP_PROP_FPS) or FPS
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"  loop29: {w}x{h} @{fps}fps {n} frames | ZOOM_AMP={zoom_amp} AR_AMP={ar_amp}")
    if n <= 0:
        cap.release()
        raise RuntimeError("loop29 has no frames")

    def to_frames(env):
        return np.interp(np.linspace(0, 1, n), np.linspace(0, 1, len(env)), env)

    bass_env, full_env = to_frames(bass_env), to_frames(full_env)

    raw = os.path.join(tmp, "beat_raw.mp4")
    writer = cv2.VideoWriter(raw, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    if not writer.isOpened():
        cap.release()
        raise RuntimeError("VideoWriter failed to open")

    for i in range(n):
        ok, frame = cap.read()
        if not ok:
            break
        z = 1.0 + zoom_amp * float(bass_env[i])
        if z > 1.0 + 1e-9:
            nw, nh = int(round(w * z)), int(round(h * z))
            big = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
            x0, y0 = (nw - w) // 2, (nh - h) // 2
            frame = big[y0:y0 + h, x0:x0 + w]
            if frame.shape[0] != h or frame.shape[1] != w:
                frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_LINEAR)
        f = frame.astype(np.float32)
        c = 1.0 + ar_amp * 1.2 * (float(full_env[i]) - 0.5)
        out = (f - 128.0) * c + 128.0 * c
        if ar_grain:
            out = out + np.random.randn(h, w, 1).astype(np.float32) * (
                255.0 * (0.02 + ar_grain * 0.55 * float(bass_env[i])))
        writer.write(np.clip(out, 0, 255).astype(np.uint8))
        if i % 100 == 0:
            print(f"  frame {i}/{n}")
    cap.release()
    writer.release()

    # --- Stage 4: finish — parallax + grade + noise + overlay + mux ---
    print("Stage 4: finish")
    out_local = os.path.join(tmp, "qwen_6_v10.mp4")
    grade = (
        f"scale=2400:1350,setsar=1,"
        f"crop={W}:{H}:x='(2400-{W})*(t/{OUT_DUR:g})':y='(1350-{H})*(t/{OUT_DUR:g})',"
        f"colorbalance=rs=-0.08:bs=0.18:rm=-0.06:bm=0.12:bh=0.06,"
        f"eq=contrast=1.06:saturation=1.12,"
        f"vignette=PI/5"
    )
    if noise_sigma > 0:
        # c0 = luma only: цвету взяться неоткуда, бленда нет
        grade += f",format=yuv420p,noise=c0s={noise_sigma}:c0f=t"

    cmd = ["ffmpeg", "-y", "-i", raw, "-ss", str(AUDIO_SS), "-i", track]

    if grain_local:
        gw, gh = probe_wh(grain_local)
        pre = "transpose=1," if gh > gw else ""     # автоориентация: не гадать
        cmd += ["-stream_loop", "-1", "-t", str(OUT_DUR), "-i", grain_local]
        # ГРАБЛЯ 2: оба входа бленда в gbrp, обратно в yuv420p ПОСЛЕ бленда
        fc = (f"[0:v]{grade},format=gbrp[graded];"
              f"[2:v]{pre}scale={W}:{H},fps={FPS},hue=s=0,format=gbrp[gr];"
              f"[graded][gr]blend=all_mode=screen:all_opacity={grain_opacity},"
              f"format=yuv420p[v]")
    else:
        fc = f"[0:v]{grade},format=yuv420p[v]"

    cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "1:a", "-t", str(OUT_DUR),
            "-af", "afade=t=out:st=27.5:d=1.5",
            "-c:v", "libx264", "-b:v", "4M", "-maxrate", "5M", "-bufsize", "8M",
            "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", out_local]
    sh(cmd, check=True)

    size = os.path.getsize(out_local)
    print(f"Final: {size / 1048576:.2f} MB")
    if size > MAX_BYTES:
        raise RuntimeError(
            f"output {size / 1048576:.2f}MB > 19MB — TG sendVideo by URL rejects >~20MB")

    sh(["rclone", "copyto", out_local, OUT_REMOTE], check=True)
    write_status(job_id, "done")
    print(f"done -> {OUT_REMOTE}")


if __name__ == "__main__":
    main()
