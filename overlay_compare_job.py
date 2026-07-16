#!/usr/bin/env python3
"""
overlay_compare_job.py — сравнительный прогон оверлеев на живом кадре лупа qwen_6 + движение под бит.

Зачем: контакт-листы врут. Оверлей судится только поверх РЕАЛЬНОГО кадра, в движении,
с нашим грейдом. Клип делится на 3 части по стыкам лупа (9.47с / 18.94с) — на каждой свой
оверлей с подписью, плюс вспышка-переход на ПЕРВОМ стыке.

ДВИЖЕНИЕ ПОД БИТ (оба слоя, выбор yaromat): зум-дыхание камеры + пульс яркости, драйвер —
сглаженная басовая огибающая трека (BPM 87.9, доля 0.683с). Жёсткой бит-сетки НЕТ намеренно:
огибающая даёт органичное дыхание, сетка — механический строб.

База = ground truth v9 (грейд принят yaromat), НО синтетический шум ВЫКЛЮЧЕН (NOISE_SIGMA=0
по умолчанию): судим оверлей начисто, без второго слоя зерна.

⚠️ ГРАБЛЯ 1: screen-бленд ТОЛЬКО в gbrp. На yuv420p блендятся плоскости цветности →
   screen(0.5,0.5)=0.75 → U,V вверх → пурпур (замерено +68). См. память.
⚠️ ГРАБЛЯ 2: бит-пасс ТОЛЬКО ПОСЛЕ сборки 29с. На 10с-юните огибающая всего трека сожмётся
   в юнит и повторится трижды → реакция разъедется с музыкой.

Ручки: ZOOM_AMP=0.02, AR_AMP=0.10, NOISE_SIGMA=0.
Запуск: JOB_ID=qwen6_ovl_compare python3 overlay_compare_job.py
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
SEAM1 = 9.4697
SEAM2 = 18.9394
OUT_DUR = 29.0
AUDIO_SS = 33.0
FPS = 25
W, H = 1920, 1080
YD = "ydrive:Content factory"
SRC_REMOTE = f"{YD}/cloud_io/pool_gate/adult_dnb_2026-07-11/qwen_6_clip.mp4"
BOARD = f"{YD}/assets/overlay_assets/board"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
MAX_BYTES = 19 * 1024 * 1024

# сегмент → (id пина, подпись, прозрачность)
SEGMENTS = [
    ("621848661095641317", "A — GRUNGE / пыль+волоски", 0.40, 0.0, SEAM1),
    ("621848661095641432", "B — ЦАРАПИНЫ плёнки", 0.40, SEAM1, SEAM2),
    ("621848661095641324", "C — ЧАСТИЦЫ в воздухе", 0.40, SEAM2, OUT_DUR),
]
# вспышка-переход на ПЕРВОМ стыке
FLASH = ("621848661095641297", "вспышка-переход", 0.55, SEAM1 - 0.5, SEAM1 + 0.7)


def sh(cmd, check=False, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if check and r.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(str(c) for c in cmd)}\n{r.stderr[-2000:]}")
    return r


def probe_wh(path):
    r = sh(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
            "stream=width,height", "-of", "csv=p=0", path], check=True)
    p = r.stdout.strip().split(",")
    return int(p[0]), int(p[1])


def esc(t):
    """drawtext: экранировать спецсимволы."""
    return t.replace("\\", "\\\\").replace(":", "\\:").replace("'", "").replace(",", "\\,")


def main():
    tmp = tempfile.mkdtemp(prefix="ovl_cmp_")
    job_id = os.environ.get("JOB_ID", "qwen6_ovl_compare")
    try:
        _run(tmp, job_id)
    except Exception as e:
        sh(["rclone", "rcat", f"{YD}/cloud_io/render_jobs/{job_id}/status.txt"],
           input=f"error: {e}")
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def beat_envelopes(track, n_frames, tmp):
    """Две огибающие трека, растянутые на n_frames СОБРАННОГО клипа (не юнита!)."""
    wav = os.path.join(tmp, "seg.wav")
    sh(["ffmpeg", "-y", "-ss", str(AUDIO_SS), "-t", str(OUT_DUR), "-i", track,
        "-ac", "1", "-ar", "22050", wav], check=True)
    y, sr = librosa.load(wav, sr=22050, mono=True)
    hop = 512
    b_coef, a_coef = butter(4, 150.0 / (sr / 2.0), btype="low")   # бас < 150 Гц
    bass_rms = librosa.feature.rms(y=filtfilt(b_coef, a_coef, y), hop_length=hop)[0]
    full_rms = librosa.feature.rms(y=y, hop_length=hop)[0]

    def norm01(sig):
        p5, p95 = np.percentile(sig, 5), np.percentile(sig, 95)
        if p95 - p5 < 1e-9:
            return np.full_like(sig, 0.5, dtype=np.float64)
        return np.clip((sig - p5) / (p95 - p5), 0.0, 1.0)

    win = max(1, int(0.175 * sr / hop))          # сглаживание = дыхание, не строб
    kern = np.ones(win, dtype=np.float64) / win
    envs = []
    for rms in (bass_rms, full_rms):
        e = np.convolve(norm01(rms), kern, mode="same")
        envs.append(np.interp(np.linspace(0, 1, n_frames),
                              np.linspace(0, 1, len(e)), e))
    return envs


def beat_pass(loop29, track, tmp, zoom_amp, ar_amp):
    """Зум-дыхание + пульс яркости по СОБРАННЫМ 29с."""
    cap = cv2.VideoCapture(loop29)
    fps = cap.get(cv2.CAP_PROP_FPS) or FPS
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if n <= 0:
        cap.release()
        raise RuntimeError("loop29 has no frames")
    print(f"  beat: {w}x{h} @{fps}fps {n} frames | ZOOM_AMP={zoom_amp} AR_AMP={ar_amp}")
    bass_env, full_env = beat_envelopes(track, n, tmp)

    raw = os.path.join(tmp, "beat_raw.mp4")
    writer = cv2.VideoWriter(raw, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    if not writer.isOpened():
        cap.release()
        raise RuntimeError("VideoWriter failed to open")
    for i in range(n):
        ok, frame = cap.read()
        if not ok:
            break
        z = 1.0 + zoom_amp * float(bass_env[i])          # дыхание камеры на басу
        if z > 1.0 + 1e-9:
            nw, nh = int(round(w * z)), int(round(h * z))
            big = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
            x0, y0 = (nw - w) // 2, (nh - h) // 2
            frame = big[y0:y0 + h, x0:x0 + w]
            if frame.shape[0] != h or frame.shape[1] != w:
                frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_LINEAR)
        f = frame.astype(np.float32)
        c = 1.0 + ar_amp * 1.2 * (float(full_env[i]) - 0.5)   # пульс яркости
        writer.write(np.clip((f - 128.0) * c + 128.0 * c, 0, 255).astype(np.uint8))
        if i % 100 == 0:
            print(f"    frame {i}/{n}")
    cap.release()
    writer.release()
    return raw


def _run(tmp, job_id):
    noise_sigma = int(float(os.environ.get("NOISE_SIGMA") or 0))
    zoom_amp = float(os.environ.get("ZOOM_AMP") or 0.02)
    ar_amp = float(os.environ.get("AR_AMP") or 0.10)
    base_remote = f"{YD}/cloud_io/render_jobs/{job_id}"

    print("Stage 0: fetch")
    src = os.path.join(tmp, "src.mp4")
    track = os.path.join(tmp, "track.mp3")
    sh(["rclone", "copyto", SRC_REMOTE, src], check=True)
    sh(["rclone", "copyto", f"{base_remote}/track.mp3", track], check=True)

    ovls = []
    for pid, label, op, t0, t1 in SEGMENTS + [FLASH]:
        p = os.path.join(tmp, f"{pid}.mp4")
        sh(["rclone", "copyto", f"{BOARD}/{pid}.mp4", p], check=True)
        ovls.append((p, label, op, t0, t1))
        print(f"  overlay {pid} -> {label}")

    print("Stage 1: minterpolate 2x unit")
    unit = os.path.join(tmp, "unit.mp4")
    sh(["ffmpeg", "-y", "-itsscale", "2", "-i", src, "-vf",
        f"minterpolate=mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1:fps={FPS}",
        "-an", "-c:v", "libx264", "-crf", "16", "-preset", "veryfast",
        "-pix_fmt", "yuv420p", unit], check=True)

    print("Stage 2: assemble 29s (xfade)")
    loop29 = os.path.join(tmp, "loop29.mp4")
    fc_asm = (
        f"[0:v]split=3[s0][s1][s2];"
        f"[s0]trim=0:{UNIT_LEN},setpts=PTS-STARTPTS[a];"
        f"[s1]trim=0:{UNIT_LEN},setpts=PTS-STARTPTS[b];"
        f"[s2]trim=0:{UNIT_LEN},setpts=PTS-STARTPTS[c];"
        f"[a][b]xfade=transition=fade:duration={XFADE_DUR}:offset={SEAM1}[ab];"
        f"[ab][c]xfade=transition=fade:duration={XFADE_DUR}:offset={SEAM2}[v]"
    )
    sh(["ffmpeg", "-y", "-i", unit, "-filter_complex", fc_asm, "-map", "[v]",
        "-t", str(OUT_DUR), "-an", "-c:v", "libx264", "-crf", "16",
        "-preset", "veryfast", "-pix_fmt", "yuv420p", loop29], check=True)

    print("Stage 3: beat pass (зум-дыхание + пульс яркости от баса)")
    beat_raw = beat_pass(loop29, track, tmp, zoom_amp, ar_amp)

    print("Stage 4: grade + overlays + labels")
    out_local = os.path.join(tmp, "qwen_6_overlay_compare.mp4")

    grade = (
        f"scale=2400:1350,setsar=1,"
        f"crop={W}:{H}:x='(2400-{W})*(t/{OUT_DUR:g})':y='(1350-{H})*(t/{OUT_DUR:g})',"
        f"colorbalance=rs=-0.08:bs=0.18:rm=-0.06:bm=0.12:bh=0.06,"
        f"eq=contrast=1.06:saturation=1.12,vignette=PI/5"
    )
    if noise_sigma > 0:
        grade += f",format=yuv420p,noise=c0s={noise_sigma}:c0f=t"

    # ГРАБЛЯ: в gbrp ДО блендов
    parts = [f"[0:v]{grade},format=gbrp[base];"]

    cmd = ["ffmpeg", "-y", "-i", beat_raw, "-ss", str(AUDIO_SS), "-i", track]
    cur = "base"
    for idx, (path, label, op, t0, t1) in enumerate(ovls):
        n = idx + 2                       # 0=unit, 1=track
        cmd += ["-stream_loop", "-1", "-t", str(OUT_DUR), "-i", path]
        gw, gh = probe_wh(path)
        pre = "transpose=1," if gh > gw else ""     # автоориентация
        parts.append(
            f"[{n}:v]{pre}scale={W}:{H},fps={FPS},hue=s=0,format=gbrp[o{idx}];")
        nxt = f"b{idx}"
        parts.append(
            f"[{cur}][o{idx}]blend=all_mode=screen:all_opacity={op}:"
            f"enable='between(t,{t0:g},{t1:g})'[{nxt}];")
        cur = nxt

    # подписи: своя на каждый сегмент + отдельная на вспышку
    draws = []
    for path, label, op, t0, t1 in ovls:
        y = "h-140" if label != FLASH[1] else "h-220"
        col = "white" if label != FLASH[1] else "yellow"
        draws.append(
            f"drawtext=fontfile={FONT}:text='{esc(label)}':fontsize=44:"
            f"fontcolor={col}:borderw=3:bordercolor=black@0.8:x=60:y={y}:"
            f"enable='between(t,{t0:g},{t1:g})'")
    # движение под бит — тоже эффект, идёт весь клип → подпись сверху
    beat_label = esc(f"движение под бит: зум {zoom_amp:g} + яркость {ar_amp:g} (от баса)")
    draws.append(
        f"drawtext=fontfile={FONT}:text='{beat_label}':fontsize=36:"
        f"fontcolor=cyan:borderw=3:bordercolor=black@0.8:x=60:y=60")
    parts.append(f"[{cur}]" + ",".join(draws) + ",format=yuv420p[v]")

    fc = "".join(parts)
    cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "1:a", "-t", str(OUT_DUR),
            "-af", "afade=t=out:st=27.5:d=1.5",
            "-c:v", "libx264", "-b:v", "4M", "-maxrate", "5M", "-bufsize", "8M",
            "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", out_local]
    sh(cmd, check=True)

    size = os.path.getsize(out_local)
    print(f"Final: {size / 1048576:.2f} MB")
    if size > MAX_BYTES:
        raise RuntimeError(f"{size / 1048576:.2f}MB > 19MB — TG режет >20МБ")

    dest = f"{YD}/cloud_io/preview/qwen_6_blue_2026-07-16/qwen_6_overlay_compare.mp4"
    sh(["rclone", "copyto", out_local, dest], check=True)
    sh(["rclone", "rcat", f"{base_remote}/status.txt"], input="done", check=True)
    print(f"done -> {dest}")


if __name__ == "__main__":
    main()
