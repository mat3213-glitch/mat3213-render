#!/usr/bin/env python3
"""Build a full-track Pinterest EDL from aubio beat/energy analysis.

The planner consumes every source from its first frame through its final frame
before allowing hero repetition.  Drop shots are one detected beat each, so a
``flash`` effect starts on every kick rather than on arbitrary 4-second cuts.

Energy drives motion: low/medium (intro, verse, outro) are calm (speed capped,
soft effects); high (drop) runs at native pace with kick-aligned flash and
active effects.  All source material is walked start-to-end before repeats —
no cut may replay a source's opening while the rest stays unseen.

CLI:
  python3 beat_storyboard.py TRACK sources_dir -o out.json \
      [--seed N] [--bpm BPM] [--hero-repeats N] \
      [--duration D] [--start S --window W] [--full]
  --start/--window produce a preview slice [S, S+W]; --full emits a full-track job.
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from analyze import analyze_track

# calm pool — intro/verse/outro (energy low/medium) — retained for reference; the
# actual per-clip colour variety is baked by the uniquize 17-effect chain.
FLASH = "flash"


def duration(p: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(p)], capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("track", type=Path)
    ap.add_argument("sources", type=Path, help="directory containing ref_<pin>.mp4")
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument("--seed", type=int, default=17509)
    ap.add_argument("--bpm", type=float, default=None, help="override detected BPM")
    ap.add_argument("--hero-repeats", type=int, default=None,
                    help="pool repeat factor; default computed from track duration")
    ap.add_argument("--duration", type=float, default=None,
                    help="reel duration (default: full track)")
    ap.add_argument("--start", type=float, default=None,
                    help="preview slice start (audio_start) in seconds")
    ap.add_argument("--window", type=float, default=None,
                    help="preview slice length in seconds (implies --start)")
    a = ap.parse_args()

    rng = random.Random(a.seed)
    bpm, groups = analyze_track(a.track, duration=None, seed=a.seed, start=0)
    bpm = a.bpm or bpm
    track_full = duration(a.track)
    reel = a.duration or track_full
    preview_slice = a.window is not None or a.start is not None
    if preview_slice and a.start is None:
        a.start = 0.0
    if preview_slice and a.window is None:
        a.window = min(30.0, reel - a.start)

    files = sorted(a.sources.glob("ref_*.mp4"))
    if len(files) < 3:
        raise SystemExit("need Pinterest source clips (ref_<pin>.mp4)")

    lengths = {p: duration(p) for p in files}
    total_source = sum(lengths.values())
    if a.hero_repeats is None:
        # enough pool so the planner never runs dry before the track ends
        a.hero_repeats = max(2, int((reel * 1.6) // total_source) + 1)
    # All distinct source material is consumed once in source order; the pool is
    # then repeated round-robin (hero repetition) to extend the timeline, never
    # substituting the rest of the pool with a single loop.
    pool = files * (1 + a.hero_repeats)
    cursors = [0.0 for _ in pool]
    pi = 0

    def uniq_name(p: Path) -> str:
        # uniquize produces ref_<pin>_uniq.mp4 from ref_<pin>.mp4.  When the source
        # dir already holds uniqued files (ref_<pin>_uniq.mp4) the generated clip
        # keeps that exact name — never double-suffix to _uniq_uniq.
        if p.stem.endswith("_uniq"):
            return p.stem + ".mp4"
        return p.stem + "_uniq.mp4"

    def next_effect(energy: str, drop: bool) -> str:
        """Render-time effect on top of the uniquely baked clip.  The uniquize step
        already applies a random 2-3 effect chain per clip (17-effect plugin), so
        only the kick-flash on drop beats is re-applied at render (a brightness
        gate, not a colour re-grade — it never double-processes the image)."""
        if drop:
            return FLASH
        return ""

    def speed_for(energy: str, drop: bool, rem: float, d: float) -> float:
        # Energy scales motion: calm sections stay near-native pace; the drop may
        # push to 1.65x so cuts keep up with the kick grid.
        base = min(1.65, rem / d) if rem else 1.0
        if drop or energy == "high":
            return max(0.9, min(1.65, base))
        if energy == "medium":
            return max(0.75, min(1.1, base))
        return max(0.6, min(0.95, base))  # low / intro / outro — calm

    # Build global beat slots.  A contiguous high-energy run (>= 6s) is a drop:
    # split into one-beat slots so flash lands on each kick.  Short high bursts /
    # medium / low stay as single slots.  analyze_track splits a continuous drop
    # into many 1-3s sub-segments, so coalesce consecutive high groups first.
    beat = 60.0 / bpm
    slots: list[tuple[float, float, str, bool]] = []  # (t_start, d, energy, drop)
    first = groups[0].track_pos
    if first > 0.04:
        slots.append((0.0, first, "low", False))
    i = 0
    while i < len(groups):
        t0, d0, en = float(groups[i].track_pos), float(groups[i].duration), groups[i].energy
        if en == "high":
            run_t, run_d = t0, 0.0
            while i < len(groups) and groups[i].energy == "high":
                run_d += float(groups[i].duration)
                i += 1
            if run_d >= 6.0:
                t = run_t
                left = run_d
                while left > 0.08:
                    dd = min(beat, left)
                    slots.append((t, dd, "high", True))
                    t += dd
                    left -= dd
            else:
                slots.append((run_t, run_d, "high", False))
        else:
            slots.append((t0, d0, en, False))
            i += 1

    # Optional preview slice: keep only shots whose global start is in [S, S+W]
    # and snap audio_start to the first retained shot boundary (no leading gap).
    audio_start = 0.0
    if preview_slice:
        S, W = a.start, a.window
        keep: list[tuple[float, float, str, bool]] = []
        cut_at: float | None = None
        for (t0, d, en, drop) in slots:
            if t0 >= S and t0 < S + W:
                keep.append((t0, d, en, drop))
            if t0 >= S and cut_at is None:
                cut_at = t0
        if not keep:
            raise SystemExit(f"preview window [{S}, {S+W:.0f}) has no shots")
        slots = keep
        audio_start = cut_at

    shots: list[dict] = []
    for t, d, energy, drop in slots:
        # advance only when all of this source was actually exposed
        while pi < len(pool) and cursors[pi] >= lengths[pool[pi]] - 0.02:
            pi += 1
        active = min(pi, len(pool) - 1)
        p = pool[active]
        rem = max(0.12, lengths[p] - cursors[active])
        speed = speed_for(energy, drop, rem, d)
        consume = min(rem, d * speed)
        shots.append({"t_dur": round(d, 3), "section": "climax" if drop else "body",
                      "energy": energy, "effect": next_effect(energy, drop),
                      "speed": round(speed, 4), "source_start": round(cursors[active], 3),
                      "base": {"kind": "generated", "path": "generated/" + uniq_name(p)}})
        cursors[active] += consume

    # EDL must end at the exact cut duration; compensate the final shot when that
    # does not distort it.  Small aubio over/under-run is trimmed by the renderer
    # (tpad/trim to reel_dur), so we never force a degenerate last frame.
    target = round(reel if not preview_slice else a.window, 3)
    total = sum(s["t_dur"] for s in shots)
    delta = round(target - total, 3)
    if abs(delta) > 0.001 and shots[-1]["t_dur"] + delta >= 0.4:
        shots[-1]["t_dur"] = round(shots[-1]["t_dur"] + delta, 3)

    render_mode = "preview" if preview_slice else "full"
    job = {
        "render_mode": render_mode,
        "duration": round(reel if not preview_slice else a.window, 3),
        "format": "vertical",
        "audio_start": round(audio_start, 3),
        "title": "HURTS — aubio BPM / full-source EDL",
        "bpm": round(bpm, 3),
        "notes": ("Aubio beat EDL; every Pinterest source is consumed start-to-end "
                  "before hero repeats; motion scales with section energy."),
        "shots": shots,
    }
    if render_mode == "full":
        # beat-synchronous flash on kicks is intentional creative grammar — allow
        # final_qc to keep its report without rejecting it as "plastic".
        job["allow_rhythmic_flash"] = True
    a.output.write_text(json.dumps(job, ensure_ascii=False, indent=2))
    print(f"BPM={bpm:.3f}; shots={len(shots)}; duration={sum(s['t_dur'] for s in shots):.3f}"
          f" mode={render_mode} audio_start={audio_start:.2f}")
    covered = sum(min(cursors[i], lengths[p]) for i, p in enumerate(files))
    print(f"unique coverage {covered:.1f} of {total_source:.1f}s "
          f"({100.0 * covered / total_source:.1f}%)")


if __name__ == '__main__':
    main()
