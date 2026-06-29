#!/usr/bin/env python3
"""Audio-reactive post-processing for Future Garage aesthetic video."""
import os
import sys
import subprocess
import tempfile
import shutil
import cv2
import numpy as np
import librosa
from scipy.signal import butter, filtfilt
def sh(cmd, check=False, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if check and r.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{r.stderr}")
    return r
def main():
    TMP = tempfile.mkdtemp(prefix="ar_")
    try:
        _run(TMP)
    except Exception as e:
        job_id = os.environ.get("JOB_ID", "unknown")
        base = f"ydrive:Content factory/cloud_io/render_jobs/{job_id}"
        sh(["rclone", "rcat", f"{base}/status.txt"], input=f"error: {e}")
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
def _run(TMP):
    JOB_ID = os.environ.get("JOB_ID")
    if not JOB_ID:
        raise RuntimeError("JOB_ID not set")
    BASE = f"ydrive:Content factory/cloud_io/render_jobs/{JOB_ID}"
    IN_NAME = os.environ.get("IN_NAME", "")
    OUT_NAME = os.environ.get("OUT_NAME", "")
    AR_AMP = float(os.environ.get("AR_AMP") or "0.12")
    if not IN_NAME:
        r = sh(["rclone", "lsf", BASE, "--include", "*.mp4"], check=True)
        files = r.stdout.strip().split("\n")
        if not files or not files[0]:
            raise RuntimeError("No .mp4 found in BASE")
        IN_NAME = files[0]
    stem = os.path.splitext(IN_NAME)[0]
    if not OUT_NAME:
        OUT_NAME = f"{stem}_ar.mp4"
    in_local = os.path.join(TMP, IN_NAME)
    sh(["rclone", "copyto", f"{BASE}/{IN_NAME}", in_local], check=True)
    cap = cv2.VideoCapture(in_local)
    fps = cap.get(cv2.CAP_PROP_FPS)
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    print(f"Video: {w}x{h} @ {fps}fps, {n_frames} frames, AR_AMP={AR_AMP}")
    audio_wav = os.path.join(TMP, "audio.wav")
    sh(["ffmpeg", "-y", "-i", in_local, "-vn", "-ac", "1", "-ar", "22050", audio_wav], check=True)
    y, sr = librosa.load(audio_wav, sr=22050, mono=True)
    nyq = sr / 2.0
    b_coef, a_coef = butter(4, 150.0 / nyq, btype="low")
    y_bass = filtfilt(b_coef, a_coef, y)
    hop_length = 512
    bass_rms = librosa.feature.rms(y=y_bass, hop_length=hop_length)[0]
    full_rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]
    def norm01(sig):
        p5 = np.percentile(sig, 5)
        p95 = np.percentile(sig, 95)
        if p95 - p5 < 1e-9:
            return np.full_like(sig, 0.5)
        return np.clip((sig - p5) / (p95 - p5), 0.0, 1.0)
    bass_env = norm01(bass_rms)
    full_env = norm01(full_rms)
    smooth_win = max(1, int(0.175 * sr / hop_length))
    kernel = np.ones(smooth_win) / smooth_win
    bass_env = np.convolve(bass_env, kernel, mode="same")
    full_env = np.convolve(full_env, kernel, mode="same")
    def resample_to_frames(env, n):
        x_old = np.linspace(0, 1, len(env))
        x_new = np.linspace(0, 1, n)
        return np.interp(x_new, x_old, env)
    bass_env = resample_to_frames(bass_env, n_frames)
    full_env = resample_to_frames(full_env, n_frames)
    raw_out = os.path.join(TMP, "raw_out.mp4")
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(raw_out, fourcc, fps, (w, h))
    cap = cv2.VideoCapture(in_local)
    for i in range(n_frames):
        ret, frame = cap.read()
        if not ret:
            break
        # Внутренние коэффициенты подняты (2026-06-29: PoC v1 был незаметен). AR_AMP=1.0 → максимум:
        # контраст/яркость до ±60%, зерно до ~150 sigma на басу. amp калибрует вниз от максимума.
        f = frame.astype(np.float32)
        c = 1.0 + AR_AMP * 1.2 * (full_env[i] - 0.5)
        bv = 1.0 + AR_AMP * 1.2 * (full_env[i] - 0.5)
        out = (f - 128.0) * c + 128.0 * bv
        noise_strength = 255.0 * (0.02 + AR_AMP * 0.55 * bass_env[i])
        noise = np.random.randn(h, w, 1) * noise_strength
        out = out + noise
        out = np.clip(out, 0, 255).astype(np.uint8)
        writer.write(out)
        if i % 100 == 0:
            print(f"  frame {i}/{n_frames}")
    cap.release()
    writer.release()
    print("Frames processed, muxing...")
    final_out = os.path.join(TMP, OUT_NAME)
    sh([
        "ffmpeg", "-y",
        "-i", raw_out,
        "-i", in_local,
        "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        final_out,
    ], check=True)
    sh(["rclone", "copyto", final_out, f"{BASE}/{OUT_NAME}"], check=True)
    sh(["rclone", "rcat", f"{BASE}/status.txt"], input="done", check=True)
    print(f"Done: {OUT_NAME}")
if __name__ == "__main__":
    main()
