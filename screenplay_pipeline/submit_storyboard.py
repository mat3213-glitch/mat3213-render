#!/usr/bin/env python3
"""submit_storyboard.py — упаковать storyboard-джоб (превью или фулл) на ЯД и запустить рендер.

Собирает render_jobs/<JOB_ID>/{storyboard.json, track.mp3, generated/*} из:
  - локальной папки с UNIQUized исходниками ref_*_uniq.mp4 (берутся из uniq/ на ЯД),
  - полного трека (mp3),
  - storyboard.json, который генерирует beat_storyboard.py прямо здесь (buku) из тех же исходников.

Два режима:
  --mode preview  → storyboard.json slice [--start, +--window]; dispatch storyboard_render.yml (render_mode=preview)
  --mode full     → storyboard.json на весь трек + approval.json (approved=yaromat, ссылка на preview) + dispatch
Также умеет взять approval из аргументов и положить preview receipt в нужном виде.

Перед фуллом раннер читает render_receipt.json ПРЕВЬЮ-джоба: значит превью уже должен быть
отрендерен и лежать в render_jobs/<PREVIEW_JID>/render_receipt.json.

Usage (buku, где есть rclone + gh):
  python3 submit_storyboard.py --mode preview \
      --sources-remote "Content factory/cloud_io/2026-08-28/kimi_pinterest_asset/33138361898/uniq" \
      --track /path/hurts.mp3 --job-id hurts_pv_1 --start 55 --window 32 --seed 17509
  python3 submit_storyboard.py --mode full \
      --sources-remote ... --track ... --job-id hurts_full_1 \
      --preview-job-id hurts_pv_1 --preview-sha256 <sha> --approve
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
BEAT = REPO_ROOT / "beat_storyboard.py"
YD_ROOT = "ydrive:Content factory"
RENDER_JOB_ROOT = f"{YD_ROOT}/cloud_io/render_jobs"
DEFAULT_REPO = "mat3213-glitch/mat3213-render"
WORKFLOW = "storyboard_render.yml"


def sh(cmd) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {r.stderr[:500]}")
    return r


def download_sources(remote_dir: str, local: Path) -> list[Path]:
    local.mkdir(parents=True, exist_ok=True)
    sh(["rclone", "copy", f"ydrive:{remote_dir}", str(local)])
    files = sorted(local.glob("ref_*_uniq.mp4"))
    if not files:
        files = sorted(local.glob("ref_*.mp4"))
    if not files:
        raise RuntimeError(f"no ref_*_uniq.mp4 in {remote_dir}")
    print(f"[bridge] {len(files)} uniquized sources in {local}")
    return files


def gen_storyboard(track: str, sources: Path, out: Path, *,
                   seed: int, bpm: float | None, mode: str,
                   start: float | None, window: float | None, duration: float | None) -> None:
    cmd = [sys.executable, str(BEAT), track, str(sources), "-o", str(out), "--seed", str(seed)]
    if bpm:
        cmd += ["--bpm", str(bpm)]
    if mode == "preview":
        cmd += ["--start", str(start), "--window", str(window)]
    if duration and mode == "full":
        cmd += ["--duration", str(duration)]
    r = sh(cmd)
    sys.stdout.write(r.stdout)


def sha256_of(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def upload_job(job_id: str, local_job: Path) -> None:
    job_yd = f"{RENDER_JOB_ROOT}/{job_id}"
    sh(["rclone", "copyto", str(local_job / "storyboard.json"), f"{job_yd}/storyboard.json"])
    sh(["rclone", "copyto", str(local_job / "track.mp3"), f"{job_yd}/track.mp3"])
    gen_dir = local_job / "generated"
    if gen_dir.is_dir():
        sh(["rclone", "copy", str(gen_dir), f"{job_yd}/generated"])
    for extra in ("approval.json",):
        p = local_job / extra
        if p.exists():
            sh(["rclone", "copyto", str(p), f"{job_yd}/{extra}"])
    print(f"[bridge] job {job_id} uploaded → {job_yd}")


def dispatch(job_id: str, repo: str = DEFAULT_REPO) -> None:
    sh(["gh", "workflow", "run", WORKFLOW, "--repo", repo, "-f", f"job_id={job_id}"])
    print(f"[bridge] dispatched {WORKFLOW} job_id={job_id}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["preview", "full"], required=True)
    ap.add_argument("--sources-remote", required=True, help="ЯД папка uniq (без ydrive:)")
    ap.add_argument("--track", required=True, help="локальный путь полного трека (mp3)")
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--seed", type=int, default=17509)
    ap.add_argument("--bpm", type=float, default=127.98)
    ap.add_argument("--duration", type=float, default=None, help="полный трек, с; default=auto")
    ap.add_argument("--start", type=float, default=None, help="preview slice start")
    ap.add_argument("--window", type=float, default=None, help="preview slice length")
    ap.add_argument("--preview-job-id", default="")
    ap.add_argument("--preview-sha256", default="")
    ap.add_argument("--reel", action="store_true", help="(внутр.) порядок превью→фулл с approval")
    a = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="storyboard_submit_"))
    sources = work / "sources"
    download_sources(a.sources_remote, sources)

    job = work / "job"
    (job / "generated").mkdir(parents=True, exist_ok=True)
    # generated/ uses the same ref_*_uniq.mp4 names the EDL references
    for f in sources.iterdir():
        if f.suffix == ".mp4":
            (job / "generated" / f.name).write_bytes(f.read_bytes())

    sh(["ffmpeg", "-y", "-v", "error", "-i", a.track, "-c:a", "libmp3lame", "-b:a", "192k",
        str(job / "track.mp3")])

    gen_storyboard(a.track, sources, job / "storyboard.json",
                   seed=a.seed, bpm=a.bpm, mode=a.mode,
                   start=a.start, window=a.window, duration=a.duration)

    if a.mode == "full":
        if not (a.preview_job_id and a.preview_sha256):
            sys.exit("--mode full требует --preview-job-id и --preview-sha256")
        # full render needs the preview reference IN storyboard.json (render_contract.preview_reference)
        sb = json.loads((job / "storyboard.json").read_text())
        sb["preview_job_id"] = a.preview_job_id
        sb["preview_sha256"] = a.preview_sha256
        (job / "storyboard.json").write_text(json.dumps(sb, ensure_ascii=False, indent=2))
        approval = {
            "approved": True, "approved_by": "yaromat",
            "preview_job_id": a.preview_job_id, "preview_sha256": a.preview_sha256,
        }
        (job / "approval.json").write_text(json.dumps(approval, indent=2))

    upload_job(a.job_id, job)
    dispatch(a.job_id)


if __name__ == "__main__":
    main()
