#!/usr/bin/env python3
"""
reve_gen_job.py — GH Actions runner (US-IP, геоблок обойдён).
Читает REVE_BEARER_TOKEN, REVE_PROMPT, REVE_COUNT из env.
Результат: /tmp/reve_outputs/<n>.jpg
"""
import json
import os
import sys
import time
from pathlib import Path

import requests

BASE_URL     = "https://app.reve.com"
BEARER_TOKEN = os.environ["REVE_BEARER_TOKEN"]
PROMPT       = os.environ.get("REVE_PROMPT", "foggy forest at dawn, cinematic, desaturated, no people")
COUNT        = int(os.environ.get("REVE_COUNT", "1"))
OUT_DIR      = Path("/tmp/reve_outputs")
OUT_DIR.mkdir(parents=True, exist_ok=True)


def headers():
    return {
        "Authorization": f"Bearer {BEARER_TOKEN}",
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        "Cache-Control": "max-age=0, no-cache",
        "Origin": "https://app.reve.com",
        "Referer": "https://app.reve.com/",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    }


def get_user():
    r = requests.get(f"{BASE_URL}/api/misc/userinfo", headers=headers(), timeout=15)
    if r.status_code != 200:
        sys.exit(f"userinfo {r.status_code}: {r.text[:300]}")
    user = r.json()["user"]
    print(f"User: {user['name']} | project: {user['default_project']} | energy: {user['regular_energy']}")
    return user["default_project"]


def chat_generate(project_id: str, prompt: str) -> dict | None:
    payload = {
        "project_id": project_id,
        "conversation": [{"role": "user", "content": prompt}],
    }
    r = requests.post(f"{BASE_URL}/api/misc/chat", json=payload, headers=headers(), timeout=60)
    print(f"chat {r.status_code}: {r.text[:400]}")
    if r.status_code == 200:
        try:
            return r.json()
        except Exception:
            pass
    return None


def infer_generate(project_id: str, prompt: str) -> dict | None:
    payload = {
        "model_id": "reve-2.0",
        "project_id": project_id,
        "inputs": {"prompt": prompt},
        "origin": "rnd",
    }
    r = requests.post(f"{BASE_URL}/api/proto/model_infer_sync", json=payload, headers=headers(), timeout=60)
    print(f"infer {r.status_code}: {r.text[:400]}")
    if r.status_code == 200:
        try:
            return r.json()
        except Exception:
            pass
    return None


def poll(project_id: str, generation_id: str, timeout=90) -> dict | None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = requests.get(
            f"{BASE_URL}/api/project/{project_id}/generation/{generation_id}",
            headers=headers(), timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            status = data.get("item", {}).get("status", "?")
            print(f"  poll: {status}")
            if status in ("completed", "done", "success"):
                return data
            if status in ("failed", "error"):
                print(f"  FAILED: {data}")
                return None
        time.sleep(4)
    print("  timeout")
    return None


def download_image(project_id: str, image_id: str, dest: Path) -> bool:
    url = f"{BASE_URL}/api/project/{project_id}/image/{image_id}/url/filename/{image_id}"
    r = requests.get(url, headers=headers(), timeout=30)
    if r.status_code == 200:
        dest.write_bytes(r.content)
        print(f"  saved: {dest} ({len(r.content):,} bytes)")
        return True
    print(f"  download {r.status_code}: {r.text[:200]}")
    return False


def extract_image_id(data: dict) -> str | None:
    # Пробуем разные пути в ответе
    for path in [
        ("item", "data", "image_id"),
        ("item", "image_id"),
        ("image_id",),
        ("data", "image_id"),
    ]:
        v = data
        for k in path:
            if isinstance(v, dict):
                v = v.get(k)
            else:
                v = None
                break
        if v:
            return str(v)
    # Поиск по тексту как fallback
    import re
    m = re.search(r'"image_id"\s*:\s*"([^"]+)"', json.dumps(data))
    return m.group(1) if m else None


def extract_generation_id(data: dict) -> str | None:
    for path in [("id",), ("item", "id"), ("generation_id",)]:
        v = data
        for k in path:
            if isinstance(v, dict):
                v = v.get(k)
            else:
                v = None
                break
        if v:
            return str(v)
    return None


def run_one(project_id: str, prompt: str, idx: int) -> bool:
    print(f"\n[{idx}] Prompt: {prompt[:80]}")

    result = chat_generate(project_id, prompt)
    if not result:
        result = infer_generate(project_id, prompt)
    if not result:
        print("  Both approaches failed")
        # Сохраняем raw response для диагностики
        return False

    # Сохраняем ответ
    (OUT_DIR / f"response_{idx}.json").write_text(json.dumps(result, indent=2))

    # Пробуем сразу получить image_id
    image_id = extract_image_id(result)
    if not image_id:
        # Нужен polling
        gen_id = extract_generation_id(result)
        if gen_id:
            gen_data = poll(project_id, gen_id)
            if gen_data:
                image_id = extract_image_id(gen_data)

    if image_id:
        return download_image(project_id, image_id, OUT_DIR / f"reve_{idx:02d}.jpg")

    print(f"  No image_id. Response: {json.dumps(result)[:500]}")
    return False


def main():
    project_id = get_user()
    ok = 0
    for i in range(COUNT):
        if run_one(project_id, PROMPT, i + 1):
            ok += 1
    print(f"\nDone: {ok}/{COUNT} images saved to {OUT_DIR}")
    files = list(OUT_DIR.iterdir())
    for f in files:
        print(f"  {f.name}: {f.stat().st_size:,} bytes")
    if ok == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
