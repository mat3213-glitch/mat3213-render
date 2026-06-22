#!/usr/bin/env python3
"""
img_gen_job.py — GitHub Actions runner: пакетная генерация картинок через CF Worker
yaromat-img (Workers AI). Гоняется на раннере (US-IP), т.к. RU↔CF рвёт /gen-запросы.

С ЯД (render_jobs/<JOB_ID>/gen_job.json):
  {"items": [
     {"name": "anchor", "model": "sdxl", "prompt": "...", "negative": "...",
      "width": 1080, "height": 1080, "steps": 20},
     {"name": "art1", "model": "flux", "prompt": "..."}
  ]}

На ЯД кладёт render_jobs/<JOB_ID>/<name>.png по каждому item + status.txt.

Env: JOB_ID, IMG_WORKER_URL, IMG_WORKER_SECRET
"""
import json, os, subprocess, sys
from pathlib import Path

import requests

JOB_ID = os.environ.get("JOB_ID", "")
if not JOB_ID:
    sys.exit("JOB_ID not set")

URL    = os.environ.get("IMG_WORKER_URL", "https://yaromat-img.mat3213.workers.dev").rstrip("/")
SECRET = os.environ.get("IMG_WORKER_SECRET", "")

REMOTE  = "ydrive"
JOB_YD  = f"Content factory/cloud_io/render_jobs/{JOB_ID}"
WORK    = Path("/tmp/img_gen_job"); WORK.mkdir(parents=True, exist_ok=True)

MODELS = {
    "flux": "@cf/black-forest-labs/flux-1-schnell",
    "sdxl": "@cf/stabilityai/stable-diffusion-xl-base-1.0",
    "sdxl-lightning": "@cf/bytedance/stable-diffusion-xl-lightning",
}


def yd_get(remote: str, local: Path) -> bool:
    local.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.run(["rclone", "copyto", f"{REMOTE}:{remote}", str(local)],
                          capture_output=True, text=True).returncode == 0

def yd_put(local: Path, remote: str) -> bool:
    return subprocess.run(["rclone", "copyto", str(local), f"{REMOTE}:{remote}"],
                          capture_output=True, text=True).returncode == 0

def yd_put_text(text: str, remote: str):
    t = WORK / "_s.txt"; t.write_text(text); yd_put(t, remote)


def gen_one(item: dict) -> bool:
    name = item["name"]
    body = {"prompt": item["prompt"], "model": MODELS.get(item.get("model", "flux"), item.get("model"))}
    for k in ("negative", "width", "height", "steps"):
        v = item.get(k)
        if v:
            body["negative_prompt" if k == "negative" else k] = v
    try:
        r = requests.post(f"{URL}/gen", headers={"X-Worker-Secret": SECRET}, json=body, timeout=(15, 200))
    except Exception as e:
        print(f"[{name}] worker error: {e}")
        return False
    ct = r.headers.get("content-type", "")
    if r.status_code != 200 or not ct.startswith("image"):
        print(f"[{name}] FAIL HTTP {r.status_code} | {ct} | {r.text[:200]}")
        return False
    out = WORK / f"{name}.png"
    out.write_bytes(r.content)
    ok = yd_put(out, f"{JOB_YD}/{name}.png")
    print(f"[{name}] {'✅' if ok else '⚠️ ген ок, upload FAIL'} {len(r.content)//1024}KB ({item.get('model','flux')})")
    return ok


def main():
    if not SECRET:
        yd_put_text("error: IMG_WORKER_SECRET не задан", f"{JOB_YD}/status.txt")
        sys.exit("IMG_WORKER_SECRET not set")

    spec = WORK / "gen_job.json"
    if not yd_get(f"{JOB_YD}/gen_job.json", spec):
        sys.exit("Failed to download gen_job.json")
    items = json.loads(spec.read_text()).get("items", [])
    print(f"Job {JOB_ID}: {len(items)} картинок")

    ok_n = sum(1 for it in items if gen_one(it))
    status = f"done: {ok_n}/{len(items)}" if ok_n == len(items) else f"partial: {ok_n}/{len(items)}"
    yd_put_text(status, f"{JOB_YD}/status.txt")
    print(f"── {status} ──")
    if ok_n == 0:
        sys.exit("all generations failed")


if __name__ == "__main__":
    main()
