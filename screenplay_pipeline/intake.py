#!/usr/bin/env python3
"""
intake.py — stage 0 of screenplay pipeline: fetch audio + mini-brief from Yandex.Disk,
run analyze_track, produce brief_full.yaml for screenwriter.py.

Mini-brief is a hand-written YAML (no LLM) with: title, key, core_emotion, visual_mood,
narrative_angle, mood_words, genre, what_to_avoid. BPM and duration come from analyze_track.

Usage:
  python3 intake.py --folder "ydrive:Content factory/cloud_io/track_intake/<slug>" --job-id JOB_ID
"""

import argparse
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from analyze import analyze_track

AUDIO_EXTS = {".mp3", ".wav", ".flac"}
YAML_EXTS = {".yaml", ".yml"}


def _rclone_copy(src: str, dst: str):
    r = subprocess.run(
        ["rclone", "copy", src, dst],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"[rclone] copy failed: {r.stderr[:300]}", file=sys.stderr)
        sys.exit(1)


def _rclone_copyto(src: str, dst: str):
    r = subprocess.run(
        ["rclone", "copyto", src, dst],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"[rclone] copyto failed: {r.stderr[:300]}", file=sys.stderr)
        sys.exit(1)
    print(f"[rclone] uploaded → {dst}")


def main():
    ap = argparse.ArgumentParser(description="Stage 0: fetch track + mini-brief from Yandex.Disk, produce brief_full.yaml.")
    ap.add_argument("--folder", required=True, help="Yandex.Disk folder with one audio + one .yaml mini-brief")
    ap.add_argument("--job-id", required=True, help="Job ID for render_jobs path on Yandex.Disk")
    args = ap.parse_args()

    tmpdir = tempfile.mkdtemp(prefix="intake_")
    print(f"[intake] downloading → {tmpdir}")
    _rclone_copy(args.folder, tmpdir)

    audio_files = [f for f in Path(tmpdir).iterdir() if f.suffix.lower() in AUDIO_EXTS]
    yaml_files = [f for f in Path(tmpdir).iterdir() if f.suffix.lower() in YAML_EXTS]

    if len(audio_files) != 1 or len(yaml_files) != 1:
        print(
            f"[intake] expected exactly 1 audio and 1 yaml in {args.folder}, "
            f"found {len(audio_files)} audio, {len(yaml_files)} yaml",
            file=sys.stderr,
        )
        sys.exit(1)

    audio_path = audio_files[0]
    yaml_path = yaml_files[0]
    print(f"[intake] audio: {audio_path.name}, brief: {yaml_path.name}")

    mini_brief = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    if not isinstance(mini_brief, dict):
        print("[intake] mini-brief is not a dict", file=sys.stderr)
        sys.exit(1)

    for key in ("title", "core_emotion", "visual_mood", "narrative_angle", "mood_words", "genre"):
        if key not in mini_brief:
            print(f"[intake] mini-brief missing required key: {key}", file=sys.stderr)
            sys.exit(1)

    bpm, segments = analyze_track(str(audio_path), duration=None)
    print(f"[intake] BPM={bpm:.1f}, {len(segments)} segments")

    energy_order = []
    seen = set()
    for seg in segments:
        if seg.energy not in seen:
            seen.add(seg.energy)
            energy_order.append(seg.energy)
    avg_energy = Counter(seg.energy for seg in segments).most_common(1)[0][0]

    last_seg = segments[-1]
    total_duration = round(last_seg.track_pos + last_seg.duration, 2)

    brief_full = {
        "track": {
            "title": mini_brief["title"],
            "bpm": round(bpm, 1),
            "key": mini_brief.get("key", ""),
            "duration": total_duration,
        },
        "content": {
            "core_emotion": mini_brief["core_emotion"],
            "visual_mood": mini_brief["visual_mood"],
            "narrative_angle": mini_brief["narrative_angle"],
            "mood_words": mini_brief["mood_words"],
        },
        "structure": {},
        "production": {
            "genre": mini_brief["genre"],
        },
        "constraints": {
            "what_to_avoid": mini_brief.get("what_to_avoid", []),
        },
        "audio_features": {
            "energy_profile": energy_order,
            "avg_energy": avg_energy,
        },
    }

    local_brief = Path(tmpdir) / "brief_full.yaml"
    local_brief.write_text(
        yaml.safe_dump(brief_full, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"[intake] brief_full → {local_brief}")

    remote_dir = f"ydrive:Content factory/cloud_io/render_jobs/{args.job_id}"
    _rclone_copyto(str(local_brief), f"{remote_dir}/brief_full.yaml")

    track_ext = audio_path.suffix
    remote_track = f"{remote_dir}/track{track_ext}"
    _rclone_copyto(str(audio_path), remote_track)
    print(f"[intake] done → {remote_dir}")


if __name__ == "__main__":
    main()
