"""
AudioAnalysis.analyze — BPM detection + energy-based segmentation using aubio.

No numba/JIT — pure C library (aubio) + numpy + scipy.
Works on any CPU without warm-up overhead.

Usage:
    from analyze import analyze_track, Segment
    bpm, segments = analyze_track("track.mp3", duration=30.0)
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

import subprocess
import tempfile

import aubio
import numpy as np
from scipy.ndimage import uniform_filter1d


@dataclass
class Segment:
    track_pos: float       # position in track (seconds)
    duration: float        # segment length (seconds)
    n_beats: int           # beat count
    energy: str            # 'low' | 'medium' | 'high'
    source: str = ""       # assigned later: 'wikimedia' / 'pexels' / etc.
    src_start: float = 0.0 # offset in source video (seconds)


_BEATS_BY_ENERGY: dict[str, list[int]] = {
    "high":   [2, 4],
    "medium": [4, 8],
    "low":    [8, 16],
}
_HIGH_THRESH   = 0.65
_MEDIUM_THRESH = 0.30


def analyze_track(
    track_path: str | Path,
    duration: float | None = 30.0,
    seed: int | None = None,
) -> tuple[float, list[Segment]]:
    """Analyze track, return (bpm, segments).

    duration=None → analyze full track.
    """
    if seed is not None:
        random.seed(seed)
        np.random.seed(seed)

    track_path = Path(track_path)
    hop = 512
    win = 1024
    print(f"[analyze] loading {track_path.name} ({duration or 'full'}s)...")

    # aubio pip wheel lacks libav — convert to WAV first (ffmpeg is always available)
    _wav_tmp = None
    if track_path.suffix.lower() != ".wav":
        _wav_tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(track_path), "-ar", "44100", "-ac", "1",
             "-t", str(duration) if duration else "-1",
             _wav_tmp.name],
            capture_output=True, check=True,
        )
        src_path = _wav_tmp.name
    else:
        src_path = str(track_path)

    src = aubio.source(src_path, hop_size=hop)
    sr = src.samplerate
    max_samples = int(duration * sr) if duration else None

    tempo_det = aubio.tempo("default", win, hop, sr)

    beat_times: list[float] = []
    rms_vals: list[float] = []
    pos = 0

    while True:
        samples, read = src()
        if read > 0:
            rms_vals.append(float(np.sqrt(np.mean(samples[:read] ** 2) + 1e-12)))
            if tempo_det(samples)[0]:
                beat_times.append(pos / sr)
        pos += hop
        if read < hop:
            break
        if max_samples and pos >= max_samples:
            break

    actual_dur = min(pos / sr, duration or pos / sr)
    bpm = float(tempo_det.get_bpm()) or 120.0  # fallback 120 if not enough data

    beat_arr = np.array(beat_times)
    print(f"[analyze] BPM={bpm:.1f}  beats={len(beat_arr)}  dur={actual_dur:.1f}s")

    if len(beat_arr) < 4:
        raise ValueError(f"Too few beats detected: {len(beat_arr)}")

    # RMS energy at each beat position
    rms_arr = np.array(rms_vals)

    def _rms_at(t: float) -> float:
        f = int(t * sr / hop)
        return float(rms_arr[min(f, len(rms_arr) - 1)])

    beat_rms = np.array([_rms_at(float(t)) for t in beat_arr])

    # Smooth over 4 beats, normalize 0→1
    smooth = uniform_filter1d(beat_rms, size=4)
    lo, hi = smooth.min(), smooth.max()
    norm = (smooth - lo) / (hi - lo) if hi > lo else np.zeros_like(smooth)

    energy_class = np.where(
        norm > _HIGH_THRESH, "high",
        np.where(norm > _MEDIUM_THRESH, "medium", "low")
    )

    # Group beats into variable-length segments
    segments: list[Segment] = []
    i = 0
    while i < len(beat_arr):
        level = str(energy_class[i])
        n = int(np.random.choice(_BEATS_BY_ENERGY[level]))
        j = i + n  # unclamped — may exceed len

        t_start = float(beat_arr[i])
        t_end   = float(beat_arr[j]) if j < len(beat_arr) else actual_dur
        dur = max(round(t_end - t_start, 4), 0.1)

        segments.append(Segment(
            track_pos=round(t_start, 4),
            duration=dur,
            n_beats=n,
            energy=level,
        ))
        i = min(j, len(beat_arr))  # exit loop when j >= len

    if _wav_tmp:
        Path(_wav_tmp.name).unlink(missing_ok=True)

    counts = {e: sum(1 for s in segments if s.energy == e) for e in ("high", "medium", "low")}
    print(f"[analyze] {len(segments)} segments → high={counts['high']} medium={counts['medium']} low={counts['low']}")
    return bpm, segments
