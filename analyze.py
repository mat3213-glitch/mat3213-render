"""
AudioAnalysis.analyze — BPM detection + energy-based segmentation.

Returns a list of Segments aligned to the beat grid, sized by energy level:
  high  → 2 or 4 beats  (drops, peaks)
  medium → 4 or 8 beats  (main body)
  low   → 8 or 16 beats  (intros, breakdowns)

Usage:
    from analyze import analyze_track, Segment
    bpm, segments = analyze_track("track.mp3", duration=30.0)
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import librosa
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
    print(f"[analyze] loading {track_path.name} ({duration or 'full'}s)...")
    y, sr = librosa.load(str(track_path), duration=duration, mono=True)
    actual_dur = len(y) / sr

    # BPM and beat timestamps
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, units="frames")
    beat_times = librosa.frames_to_time(beat_frames, sr=sr)
    bpm = float(np.asarray(tempo).flat[0])  # librosa 0.10+ returns array
    print(f"[analyze] BPM={bpm:.1f}  beats={len(beat_times)}  dur={actual_dur:.1f}s")

    if len(beat_times) < 4:
        raise ValueError(f"Too few beats detected: {len(beat_times)}")

    # RMS energy sampled at each beat
    hop = 512
    rms = librosa.feature.rms(y=y, hop_length=hop)[0]

    def _rms_at(t: float) -> float:
        f = int(librosa.time_to_frames(t, sr=sr, hop_length=hop))
        return float(rms[min(f, len(rms) - 1)])

    beat_rms = np.array([_rms_at(float(t)) for t in beat_times])

    # Smooth over 4 beats, normalize
    smooth = uniform_filter1d(beat_rms, size=4)
    lo, hi = smooth.min(), smooth.max()
    norm = (smooth - lo) / (hi - lo) if hi > lo else np.zeros_like(smooth)

    energy_class = np.where(
        norm > _HIGH_THRESH, "high",
        np.where(norm > _MEDIUM_THRESH, "medium", "low")
    )

    # Group beats into variable-length segments by energy level
    segments: list[Segment] = []
    i = 0
    while i < len(beat_times):
        level = str(energy_class[i])
        n = int(np.random.choice(_BEATS_BY_ENERGY[level]))
        j = min(i + n, len(beat_times) - 1)

        t_start = float(beat_times[i])
        t_end   = float(beat_times[j]) if j < len(beat_times) - 1 else actual_dur
        dur = max(round(t_end - t_start, 4), 0.1)

        segments.append(Segment(
            track_pos=round(t_start, 4),
            duration=dur,
            n_beats=n,
            energy=level,
        ))
        i = j

    counts = {e: sum(1 for s in segments if s.energy == e) for e in ("high", "medium", "low")}
    print(f"[analyze] {len(segments)} segments → high={counts['high']} medium={counts['medium']} low={counts['low']}")
    return bpm, segments
