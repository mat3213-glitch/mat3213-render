#!/usr/bin/env python3
"""storyboard_assemble.py — runner-side: generate storyboard + pack generated/ for a render job.

Runs on the GitHub Actions runner (fast YaD access) so the buku never has to move
the source pool.  Steps:
  1. runs beat_storyboard.py over a local sources dir → out/storyboard.json
  2. copies every uniquized source into out/generated/ (the EDL references them)
  3. for --mode full: injects preview_job_id/preview_sha256 into storyboard.json
     (approval.json + the preview render_receipt.json must already be on YaD)

Usage (on runner):
  python3 storyboard_assemble.py --track TRACK --sources-dir DIR --out-dir OUT \
      --mode preview|full --seed N --bpm BPM [--start S --window W] [--duration D] \
      [--preview-job-id JID --preview-sha256 HASH]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BEAT = (HERE).parent / "beat_storyboard.py" if (HERE).name == "screenplay_pipeline" else HERE / "beat_storyboard.py"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--track", required=True)
    ap.add_argument("--sources-dir", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--mode", choices=["preview", "full"], required=True)
    ap.add_argument("--seed", type=int, default=17509)
    ap.add_argument("--bpm", type=float, default=127.98)
    ap.add_argument("--duration", type=float, default=None)
    ap.add_argument("--start", type=float, default=None)
    ap.add_argument("--window", type=float, default=None)
    ap.add_argument("--preview-job-id", default="")
    ap.add_argument("--preview-sha256", default="")
    a = ap.parse_args()

    sources = Path(a.sources_dir)
    out = Path(a.out_dir)
    (out / "generated").mkdir(parents=True, exist_ok=True)

    files = sorted(sources.glob("ref_*_uniq.mp4")) or sorted(sources.glob("ref_*.mp4"))
    if not files:
        sys.exit(f"no ref_*_uniq.mp4 in {sources}")
    for f in files:
        if f.suffix == ".mp4":
            shutil.copy2(f, out / "generated" / f.name)
    print(f"[assemble] copied {len(files)} sources -> generated/")

    cmd = [sys.executable, str(BEAT), a.track, str(sources), "-o", str(out / "storyboard.json"),
           "--seed", str(a.seed), "--bpm", str(a.bpm)]
    if a.mode == "preview":
        cmd += ["--start", str(a.start), "--window", str(a.window)]
    if a.mode == "full" and a.duration:
        cmd += ["--duration", str(a.duration)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    if r.returncode != 0:
        sys.exit(f"beat_storyboard failed: {r.stderr[-400:]}")

    if a.mode == "full":
        if not (a.preview_job_id and a.preview_sha256):
            sys.exit("--mode full requires --preview-job-id and --preview-sha256")
        sb_path = out / "storyboard.json"
        sb = json.loads(sb_path.read_text())
        sb["preview_job_id"] = a.preview_job_id
        sb["preview_sha256"] = a.preview_sha256
        sb_path.write_text(json.dumps(sb, ensure_ascii=False, indent=2))
        print(f"[assemble] full: inject preview reference {a.preview_job_id}")


if __name__ == "__main__":
    main()
