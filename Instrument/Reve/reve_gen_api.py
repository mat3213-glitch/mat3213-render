#!/usr/bin/env python3
"""
Reve Image Generation Script (API-based)
Direct API calls using bearer token auth.

Usage:
  export REVE_BEARER_TOKEN="your-jwt-token-here"
  python3 reve_gen_api.py

This script tries two approaches:
1. POST /api/misc/chat (chat-based generation)
2. POST /api/proto/model_infer_sync (direct model inference)
"""

import json
import os
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    print("Installing requests...")
    os.system(f"{sys.executable} -m pip install requests -q")
    import requests

BASE_URL = "https://app.reve.com"
BEARER_TOKEN = os.environ.get("REVE_BEARER_TOKEN", "")
PROMPT = "foggy forest at dawn, cinematic, desaturated, no people"
OUTPUT_FILE = Path(__file__).parent / "reve_test_output.jpg"
TIMEOUT_S = 120


def get_headers():
    return {
        "Authorization": f"Bearer {BEARER_TOKEN}",
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        "Origin": "https://app.reve.com",
        "Referer": "https://app.reve.com/",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }


def get_user_info():
    print("Getting user info...")
    resp = requests.get(f"{BASE_URL}/api/misc/userinfo", headers=get_headers())
    if resp.status_code == 200:
        data = resp.json()
        user = data.get("user", {})
        print(f"  User: {user.get('name', 'unknown')} ({user.get('email', 'unknown')})")
        project_id = user.get("default_project", "")
        print(f"  Default project: {project_id}")
        return user, project_id
    else:
        print(f"  ERROR {resp.status_code}: {resp.text[:300]}")
        return None, None


def try_chat_generation(project_id):
    print("\n--- Approach 1: POST /api/misc/chat ---")

    chat_payload = {
        "project_id": project_id,
        "messages": [
            {
                "role": "user",
                "content": PROMPT,
            }
        ],
    }

    print(f"  Payload: {json.dumps(chat_payload, indent=2)[:500]}")

    headers = get_headers()
    headers["Cache-Control"] = "max-age=0, no-cache, must-revalidate, proxy-revalidate"

    start = time.time()
    resp = requests.post(
        f"{BASE_URL}/api/misc/chat",
        json=chat_payload,
        headers=headers,
        timeout=TIMEOUT_S,
    )
    elapsed = time.time() - start

    print(f"  Response: {resp.status_code} ({elapsed:.1f}s)")
    print(f"  Body: {resp.text[:1000]}")

    if resp.status_code == 200:
        try:
            data = resp.json()
            return data
        except json.JSONDecodeError:
            print("  Response is not JSON")
    return None


def try_model_infer(project_id):
    print("\n--- Approach 2: POST /api/proto/model_infer_sync ---")

    infer_payload = {
        "model_id": "reve-2.0",
        "project_id": project_id,
        "inputs": {
            "prompt": PROMPT,
        },
        "origin": "rnd",
    }

    print(f"  Payload: {json.dumps(infer_payload, indent=2)[:500]}")

    start = time.time()
    resp = requests.post(
        f"{BASE_URL}/api/proto/model_infer_sync",
        json=infer_payload,
        headers=get_headers(),
        timeout=TIMEOUT_S,
    )
    elapsed = time.time() - start

    print(f"  Response: {resp.status_code} ({elapsed:.1f}s)")
    print(f"  Body: {resp.text[:1000]}")

    if resp.status_code == 200:
        try:
            data = resp.json()
            return data
        except json.JSONDecodeError:
            pass
    return None


def poll_generation(project_id, generation_id):
    print(f"\n  Polling generation {generation_id}...")
    start = time.time()

    while time.time() - start < TIMEOUT_S:
        resp = requests.get(
            f"{BASE_URL}/api/project/{project_id}/generation/{generation_id}",
            headers=get_headers(),
            timeout=30,
        )

        if resp.status_code == 200:
            data = resp.json()
            status = data.get("item", {}).get("status", "unknown")
            print(f"  Status: {status}")

            if status in ("completed", "done", "success"):
                return data
            elif status in ("failed", "error"):
                print(f"  Generation failed: {data}")
                return None
        else:
            print(f"  Poll error {resp.status_code}: {resp.text[:200]}")

        time.sleep(3)

    print(f"  Timeout after {TIMEOUT_S}s")
    return None


def download_image(project_id, image_id):
    print(f"\n  Downloading image {image_id}...")
    url = f"{BASE_URL}/api/project/{project_id}/image/{image_id}/url/filename/{image_id}"
    resp = requests.get(url, headers=get_headers(), timeout=30)

    if resp.status_code == 200:
        with open(OUTPUT_FILE, 'wb') as f:
            f.write(resp.content)
        print(f"  Saved: {OUTPUT_FILE} ({len(resp.content):,} bytes)")
        return True
    else:
        print(f"  Download failed: {resp.status_code} {resp.text[:200]}")
        return False


def main():
    if not BEARER_TOKEN:
        print("ERROR: REVE_BEARER_TOKEN environment variable is not set.")
        print()
        print("To get your token:")
        print("  1. Open app.reve.com in your browser and login")
        print("  2. Open DevTools → Application → Local Storage → app.reve.com")
        print("  3. Copy the value of 'reve:bearer_token'")
        print("  4. Export it: export REVE_BEARER_TOKEN='your-token'")
        sys.exit(1)

    start_time = time.time()

    user, project_id = get_user_info()
    if not user or not project_id:
        print("ERROR: Could not get user info. Token may be invalid.")
        sys.exit(1)

    result = try_chat_generation(project_id)

    if not result:
        result = try_model_infer(project_id)

    if result:
        print(f"\nGeneration result:\n{json.dumps(result, indent=2)[:2000]}")

        generation_id = None
        image_id = None

        if "id" in result:
            generation_id = result["id"]
        elif "item" in result:
            generation_id = result["item"].get("id")

        if generation_id:
            gen_data = poll_generation(project_id, generation_id)
            if gen_data:
                image_id = gen_data.get("item", {}).get("data", {}).get("image_id")

        if not image_id and "image_id" in str(result):
            import re
            match = re.search(r'"image_id"\s*:\s*"([^"]+)"', str(result))
            if match:
                image_id = match.group(1)

        if image_id:
            download_image(project_id, image_id)
        else:
            print("\nNo image_id found in response. Save the response for manual inspection.")
            with open(Path(__file__).parent / "reve_last_response.json", "w") as f:
                json.dump(result, f, indent=2, default=str)
    else:
        print("\nBoth approaches failed. Check reve_api_notes.md for manual steps.")

    elapsed = time.time() - start_time
    print(f"\nTotal time: {elapsed:.1f}s")

    if OUTPUT_FILE.exists():
        size = OUTPUT_FILE.stat().st_size
        print(f"Output: {OUTPUT_FILE} ({size:,} bytes / {size/1024:.1f} KB)")


if __name__ == "__main__":
    main()
