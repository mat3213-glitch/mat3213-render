#!/usr/bin/env python3
"""Build a full-track Pinterest EDL from aubio beat/energy analysis.

The planner consumes every source from its first frame through its final frame
before allowing hero repetition.  Drop shots are one detected beat each, so a
``flash`` effect starts on every kick rather than on arbitrary 4-second cuts.
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

EFFECTS = [
    "faded_film", "hue_rotate", "color_pulse", "bleach_negate", "film_burn",
    "mirror_split", "screen_interference", "parallax", "slide_crop", "corner_sweep",
    "zoom_drift", "diagonal_crop", "strobo", "flash", "split_drift", "grid_2x2",
    "split_converge", "negative_echo", "self_blend_reverse", "motion_pan",
]

def duration(p: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(p)], capture_output=True, text=True, check=True)
    return float(r.stdout.strip())

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("track", type=Path)
    ap.add_argument("sources", type=Path, help="directory containing src_<pin>.mp4")
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument("--seed", type=int, default=17509)
    a = ap.parse_args()
    bpm, groups = analyze_track(a.track, duration=None, seed=a.seed, start=0)
    files = sorted(a.sources.glob("src_*.mp4"))
    if len(files) < 3:
        raise SystemExit("need Pinterest source clips")
    # All distinct source material is consumed once in source order.  Hero files
    # then extend the timeline; never substitute the rest of the pool with a loop.
    hero = [p for p in files if p.stem.endswith("215372") or p.stem.endswith("973547")]
    pool = files + hero * 20
    lengths = {p: duration(p) for p in files}
    # Cursor belongs to an occurrence, not a filename: hero repetitions restart
    # from frame zero only after the first complete pass through the whole pool.
    cursors = [0.0 for _ in pool]
    pi = 0
    rng = random.Random(a.seed)
    effect_bag: list[str] = []
    prev_effect = ""
    def next_effect(drop: bool) -> str:
        nonlocal effect_bag, prev_effect
        if drop:
            prev_effect = "flash"; return "flash"
        if not effect_bag:
            effect_bag = EFFECTS[:]; rng.shuffle(effect_bag)
            if effect_bag[0] == prev_effect: effect_bag.append(effect_bag.pop(0))
        x = effect_bag.pop(0); prev_effect = x; return x
    # Exact aubio groups outside the drop; split high-energy groups into one-beat
    # slots.  The first detected beat is at ~3s, so preserve the intro explicitly.
    slots: list[tuple[float, float, str, bool]] = []
    first = groups[0].track_pos
    if first > 0.04: slots.append((0.0, first, "low", False))
    beat = 60.0 / bpm
    for g in groups:
        t, left, high = float(g.track_pos), float(g.duration), g.energy == "high" and g.track_pos >= 93.0
        if high:
            while left > 0.08:
                d = min(beat, left); slots.append((t, d, "high", True)); t += d; left -= d
        else:
            slots.append((t, left, g.energy, False))
    shots=[]
    def generated_name(p: Path) -> str:
        suffix = p.stem[-6:]
        return {"215372": "birds", "973547": "horse"}.get(suffix, suffix) + ".mp4"
    for t, d, energy, drop in slots:
        # advance only when all of this source was actually exposed
        while pi < len(pool) and cursors[pi] >= lengths[pool[pi]] - 0.02:
            pi += 1
        p = pool[min(pi, len(pool)-1)]
        active = min(pi, len(pool)-1)
        rem = max(0.12, lengths[p] - cursors[active])
        # speed maps the remaining unique interval exactly to the beat-aligned slot.
        speed = max(0.35, min(1.65, rem / d))
        consume = min(rem, d * speed)
        shots.append({"t_dur": round(d, 3), "section": "climax" if drop else "body",
                      "energy": energy, "effect": next_effect(drop),
                      "speed": round(speed, 4), "source_start": round(cursors[active], 3),
                      "base": {"kind": "generated", "path": "generated/" + generated_name(p)}})
        cursors[active] += consume
    # EDL must end at exact audio duration; compensate only the final real hero shot.
    total=sum(s["t_dur"] for s in shots)
    shots[-1]["t_dur"] = round(shots[-1]["t_dur"] + (192.0-total), 3)
    a.output.write_text(json.dumps({"render_mode":"full", "duration":192.0, "format":"vertical",
        "audio_start":0.0, "title":"HURTS — aubio BPM / full-source EDL", "bpm":round(bpm,3),
        "notes":"Aubio beat EDL; every Pinterest source is consumed start-to-end before hero repeats.",
        "shots":shots}, ensure_ascii=False, indent=2))
    print(f"BPM={bpm:.3f}; shots={len(shots)}; duration={sum(s['t_dur'] for s in shots):.3f}")
    print("unique coverage", sum(min(cursors[i],lengths[p]) for i,p in enumerate(files)), "of", sum(lengths.values()))

if __name__ == '__main__': main()
