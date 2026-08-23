"""beats.py — beat detection for cut-to-the-beat editing. numpy only, no librosa.

Адаптация модуля scripts/beats.py из Fangyuan025/tiktok-video-skill (MIT, шорт-лист
внедрения 23.08, AUDIT/REPO_SCOUT_SHORTLIST_2026-08-23.md). Даёт онсеты и BPM прямо из
финального микса — для авторов VideogenPro (где дропы/кульминации) и нарезки стокового
пула под трек без ручных stem-меток.

Pipeline: decode to mono PCM (ffmpeg pipe) -> spectral-flux onset envelope ->
tempo by autocorrelation -> beat phase by comb alignment -> per-beat refinement.

CLI:
  python3 beats.py TRACK.mp3 [--bpm-hint 100] [--lo 60 --hi 180] [-o beats.json]
"""
import argparse
import json
import subprocess
import sys

import numpy as np

SR = 22050
N_FFT = 1024
HOP = 256   # 11.6ms frames — beat placement jitter stays under one video frame


def load_mono(path, sr=SR, max_secs=240):
    p = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-t", str(max_secs),
         "-f", "f32le", "-ac", "1", "-ar", str(sr), "-"],
        capture_output=True)
    if p.returncode != 0 or len(p.stdout) < sr * 4:
        raise RuntimeError(f"could not decode {path}: {p.stderr.decode()[-200:]}")
    return np.frombuffer(p.stdout, dtype=np.float32)


def onset_envelope(y, n_fft=N_FFT, hop=HOP):
    n = 1 + (len(y) - n_fft) // hop
    if n < 20:
        raise RuntimeError("audio too short for beat analysis")
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n)[:, None]
    frames = y[idx] * np.hanning(n_fft)
    mag = np.abs(np.fft.rfft(frames, axis=1))
    logmag = np.log1p(10 * mag)
    flux = np.maximum(0, np.diff(logmag, axis=0)).sum(axis=1)
    flux = np.concatenate([[0.0], flux])
    # local-mean subtraction sharpens peaks
    k = 16
    kernel = np.ones(2 * k + 1) / (2 * k + 1)
    local = np.convolve(flux, kernel, mode="same")
    env = np.maximum(0, flux - local)
    m = env.max()
    return env / m if m > 0 else env


def estimate_tempo(env, sr=SR, hop=HOP, lo=70, hi=180, bpm_hint=None):
    ac = np.correlate(env, env, mode="full")[len(env) - 1:]
    lag_lo = int(round(60 * sr / (hi * hop)))       # frames per beat at hi BPM
    lag_hi = int(round(60 * sr / (lo * hop)))
    lag_hi = min(lag_hi, len(ac) - 2)
    if lag_lo >= lag_hi:
        raise RuntimeError("audio too short for tempo estimation")
    lags = np.arange(lag_lo, lag_hi + 1)
    bpms = 60 * sr / (lags * hop)
    if bpm_hint:
        # a known tempo (e.g. pre-vibe analysis x speed factor) resolves the
        # classic 2x / 1.5x metrical-level ambiguity
        weight = np.exp(-0.5 * ((np.log2(bpms / bpm_hint)) / 0.2) ** 2)
    else:
        # mild preference for the 90-150 BPM band where short-video edits live
        weight = np.exp(-0.5 * ((np.log2(bpms / 120)) / 0.9) ** 2)
    li = int(np.argmax(ac[lag_lo:lag_hi + 1] * weight))
    l = lag_lo + li
    # parabolic interpolation around the peak -> sub-frame period, else the
    # integer quantization alone is a ~2% tempo error (a full beat over 60s)
    y0, y1, y2 = ac[l - 1], ac[l], ac[l + 1]
    denom = y0 - 2 * y1 + y2
    delta = 0.5 * (y0 - y2) / denom if abs(denom) > 1e-12 else 0.0
    period = l + float(np.clip(delta, -0.5, 0.5))
    return 60 * sr / (period * hop), period


def beat_grid(env, period_frames, sr=SR, hop=HOP):
    """Walk the track beat by beat: predict the next beat one period ahead,
    snap to the strongest nearby onset (tracks live-played tempo drift),
    fall back to the grid position through silent stretches."""
    period = float(period_frames)
    start = int(np.argmax(env[:max(2, int(2 * period))]))
    beats_f = [float(start)]
    f = float(start)
    while f + period < len(env):
        target = f + period
        a = max(0, int(round(target - 0.15 * period)))
        b = min(len(env), int(round(target + 0.15 * period)) + 1)
        if b <= a:
            break
        w = env[a:b] * (1 - 0.5 * np.abs(np.arange(a, b) - target) / period)
        nxt = a + int(np.argmax(w))
        if env[nxt] < 0.02:            # silence/breakdown: stay on the grid
            nxt = target
        beats_f.append(float(nxt))
        f = float(nxt)
    return [bf * hop / sr for bf in beats_f]


def analyze(path, bpm_hint=None, lo=70, hi=180):
    y = load_mono(path)
    env = onset_envelope(y)
    bpm, period = estimate_tempo(env, bpm_hint=bpm_hint, lo=lo, hi=hi)
    beats = beat_grid(env, period)
    # confidence: how much sharper the beat comb is vs average energy
    on = np.mean([env[min(int(round(t * SR / HOP)), len(env) - 1)] for t in beats])
    conf = float(on / (env.mean() + 1e-9))
    return {"bpm": round(float(bpm), 2), "beats": [round(t, 3) for t in beats],
            "confidence": round(conf, 2), "analyzed_secs": round(len(y) / SR, 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("-o", "--out", help="JSON: bpm, beats[], confidence, analyzed_secs")
    ap.add_argument("--bpm-hint", type=float, default=None,
                    help="известный темп трека — снимает двусмысленность x2/x1.5")
    ap.add_argument("--lo", type=float, default=70, help="нижняя граница поиска BPM")
    ap.add_argument("--hi", type=float, default=180, help="верхняя граница поиска BPM")
    a = ap.parse_args()
    res = analyze(a.audio, bpm_hint=a.bpm_hint, lo=a.lo, hi=a.hi)
    if a.out:
        with open(a.out, "w") as f:
            json.dump(res, f)
    print(f"[beats] {res['bpm']} BPM, {len(res['beats'])} beats, "
          f"confidence {res['confidence']}" + (f" -> {a.out}" if a.out else ""))


if __name__ == "__main__":
    main()
