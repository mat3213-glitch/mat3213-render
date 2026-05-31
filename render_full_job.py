#!/usr/bin/env python3
"""
render_full_job.py — GitHub Actions runner для FULL_RENDER пайплайна.

YaDisk контракт:
  Вход (ноут кладёт перед trigger):
    Content factory/render_jobs/<JOB_ID>/art.png       — обложка
    Content factory/render_jobs/<JOB_ID>/track.mp3     — аудио-трек
    Content factory/render_jobs/<JOB_ID>/job.json      — {"text": "...", "out_name": "..."}

  Выход (раннер кладёт по завершению):
    Content factory/render_jobs/<JOB_ID>/<out_name>    — готовое видео
    Content factory/render_jobs/<JOB_ID>/status.txt    — "done" или "error: ..."

Env vars (GitHub Secrets + workflow inputs):
  YDRIVE_CLIENT_ID / YDRIVE_CLIENT_SECRET / YDRIVE_TOKEN  — через rclone
  JOB_ID  — render job ID
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from FULL_RENDER import render

JOB_ID = os.environ["JOB_ID"]
REMOTE = "ydrive"
JOB_YD = f"Content factory/render_jobs/{JOB_ID}"


def yd_get(remote_path: str, local: Path) -> bool:
    local.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["rclone", "copyto", f"{REMOTE}:{remote_path}", str(local)],
                       capture_output=True, text=True)
    return r.returncode == 0


def yd_put(local: Path, remote_path: str) -> bool:
    r = subprocess.run(["rclone", "copyto", str(local), f"{REMOTE}:{remote_path}"],
                       capture_output=True, text=True)
    ok = r.returncode == 0
    print(f"  PUT {remote_path.split('/')[-1]}: {'ok' if ok else 'FAIL'}")
    return ok


def yd_status(text: str):
    tmp = Path(f"/tmp/_status_{os.getpid()}.txt")
    tmp.write_text(text)
    subprocess.run(["rclone", "copyto", str(tmp), f"{REMOTE}:{JOB_YD}/status.txt"],
                   capture_output=True)
    tmp.unlink(missing_ok=True)


def main():
    local = Path("/tmp/render_full_job")
    local.mkdir(parents=True, exist_ok=True)

    print(f"Job: {JOB_ID}")
    print(f"YaDisk: {JOB_YD}/")

    # 1. job.json
    job_path = local / "job.json"
    if not yd_get(f"{JOB_YD}/job.json", job_path):
        sys.exit("job.json не найден на ЯД")
    job = json.loads(job_path.read_text())
    text     = job.get("text", "yaromat")
    out_name = job.get("out_name", "result.mp4")
    print(f"text='{text}'  out={out_name}")

    # 2. art.png
    art_path = local / "art.png"
    if not yd_get(f"{JOB_YD}/art.png", art_path):
        sys.exit("art.png не найден на ЯД")
    print(f"Арт: {art_path.stat().st_size // 1024}KB")

    # 3. track.mp3
    track_path = local / "track.mp3"
    if not yd_get(f"{JOB_YD}/track.mp3", track_path):
        sys.exit("track.mp3 не найден на ЯД")
    print(f"Трек: {track_path.stat().st_size // 1024}KB")

    # 4. рендер
    out_path = local / out_name
    print(f"\nРендерю → {out_name}...")
    try:
        render(str(art_path), str(track_path), text, str(out_path))
    except Exception as e:
        yd_status(f"error: {e}")
        sys.exit(f"Рендер упал: {e}")

    if not out_path.exists():
        yd_status("error: output not found after render")
        sys.exit("Выходной файл не создан")

    size_mb = out_path.stat().st_size / 1024 / 1024
    print(f"\nРезультат: {size_mb:.1f}MB")

    # 5. upload result
    if not yd_put(out_path, f"{JOB_YD}/{out_name}"):
        yd_status("error: upload failed")
        sys.exit("Upload упал")

    yd_status("done")
    print(f"\n✅ Готово. ЯД: {JOB_YD}/{out_name}")


if __name__ == "__main__":
    main()
