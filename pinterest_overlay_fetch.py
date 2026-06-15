#!/usr/bin/env python3
"""
pinterest_overlay_fetch.py — GitHub Actions: качает филмик-оверлеи с Pinterest
по ПОИСКОВОЙ выдаче и складывает на ЯД. БЕЗ КУК / БЕЗ ЛОГИНА.

── ЗАЩИТА ОТ БАНА (ключевое требование) ──────────────────────────────────────
• Используется АНОНИМНЫЙ поиск Pinterest (resource endpoint, как у незалогиненного
  браузера). Куки/токен аккаунта НЕ передаются НИГДЕ → нет аккаунта, который можно
  забанить. Риск только IP-уровня (раннер, ротируемый GH-IP), не на акк yaromat.
• Вежливый темп: рандомные паузы 2–5с между запросами и скачиваниями, лимит пинов
  на запрос и на прогон, реалистичный User-Agent (ротация), backoff на 429/503,
  мягкий abort при блоке (не долбим).

Поиск отдаёт video_list с прямыми URL (mp4/hls на pinimg CDN). Скачиваем yt-dlp
(без кук), дедуп по pin id, заливаем rclone на ЯД, уведомляем ТГ старт/стоп.

ENV (GH secrets / workflow inputs):
  CLOUDFLARE_WORKER, TELEGRAM_BOT_TOKEN — ТГ-уведомления (через CF Worker /tg-relay)
  PIN_CHAT_ID (default -1003946370426), PIN_THREAD_ID (default 228 = "pinterest yaromat")
  QUERIES        — запросы через перевод строки или ';' (есть дефолты)
  PER_QUERY      — сколько пинов на запрос (default 8)
  DEST_FOLDER    — папка на ЯД (default "Content factory/overlay_assets/pinterest")
  rclone ydrive  — настроен воркфлоу из YDRIVE_* секретов
"""

import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REMOTE = "ydrive"
DEST = os.environ.get("DEST_FOLDER", "Content factory/overlay_assets/pinterest")
PER_QUERY = int(os.environ.get("PER_QUERY", "8"))
WORKDIR = Path("/tmp/pin_ovl"); WORKDIR.mkdir(parents=True, exist_ok=True)

DEFAULT_QUERIES = [
    "light leak overlay black",
    "film burn overlay black background",
    "bokeh overlay black background",
    "dust particles overlay black",
    "film grain overlay black",
    "light streaks overlay black",
]
_q_env = os.environ.get("QUERIES", "").strip()
QUERIES = [q.strip() for q in re.split(r"[;\n]", _q_env) if q.strip()] or DEFAULT_QUERIES

UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
]

def polite_sleep(a=2.0, b=5.0):
    time.sleep(random.uniform(a, b))

def download_pause():
    """Человекоподобная пауза между скачиваниями: главное — не скорость, а незаметность.
    В основном 18–52с, ~25% случаев тянется к минуте (рандомные вариации = не похоже на бота)."""
    base = random.uniform(18.0, 52.0)
    if random.random() < 0.25:
        base += random.uniform(5.0, 12.0)   # иногда до ~64с
    print(f"  [pause] {base:.0f}s")
    time.sleep(base)


# ── TG ───────────────────────────────────────────────────────────────────────

def send_tg(text: str):
    worker = os.environ.get("CLOUDFLARE_WORKER", "https://api.telegram.org")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("PIN_CHAT_ID", "-1003946370426")
    thread = os.environ.get("PIN_THREAD_ID", "228")
    if not token:
        print("[tg] нет TELEGRAM_BOT_TOKEN — печатаю:\n" + text)
        return
    payload = {"chat_id": chat, "text": text[:3900], "disable_web_page_preview": True}
    if thread:
        payload["message_thread_id"] = int(thread)
    try:
        import requests
        r = requests.post(f"{worker}/bot{token}/sendMessage", json=payload, timeout=30)
        print(f"[tg] HTTP {r.status_code} → chat={chat} thread={thread}")
    except Exception as e:
        print(f"[tg] ошибка: {e}\n{text}")


# ── rclone ─────────────────────────────────────────────────────────────────────

def yd_put(local: Path, remote_path: str) -> bool:
    r = subprocess.run(["rclone", "copyto", str(local), f"{REMOTE}:{remote_path}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  [rclone] FAIL {remote_path}: {r.stderr[-160:]}")
    return r.returncode == 0

def yd_read_json(remote_path: str):
    r = subprocess.run(["rclone", "cat", f"{REMOTE}:{remote_path}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None

def yd_write_json(obj, remote_path: str):
    tmp = WORKDIR / "_tmp.json"
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2))
    yd_put(tmp, remote_path)


# ── Pinterest anon search ────────────────────────────────────────────────────

def search_pins(query: str, page_size: int = 25, bookmark: str | None = None):
    """Анонимный resource-эндпоинт Pinterest. Возвращает (results, next_bookmark)."""
    options = {"query": query, "scope": "pins", "page_size": page_size}
    if bookmark:
        options["bookmarks"] = [bookmark]
    data = {"options": options, "context": {}}
    src = f"/search/pins/?q={query}"
    url = ("https://www.pinterest.com/resource/BaseSearchResource/get/"
           f"?source_url={urllib.parse.quote(src)}&data={urllib.parse.quote(json.dumps(data))}")
    headers = {
        "User-Agent": random.choice(UAS),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "X-Pinterest-PWS-Handler": "www/search/[scope].js",
        "Referer": "https://www.pinterest.com" + src,
    }
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.load(r)
            rr = d.get("resource_response", {})
            results = rr.get("data", {}).get("results", []) or []
            nb = rr.get("bookmark")
            return results, nb
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                wait = (attempt + 1) * 8 + random.uniform(0, 4)
                print(f"  [search] {e.code} rate-limit → backoff {wait:.0f}s")
                time.sleep(wait)
                continue
            print(f"  [search] HTTP {e.code} — стоп по запросу")
            return [], None
        except Exception as e:
            print(f"  [search] err {type(e).__name__}: {str(e)[:120]}")
            time.sleep(5)
    return [], None

def best_video_url(pin: dict) -> str | None:
    vids = (pin.get("videos") or {}).get("video_list") or {}
    if not vids:
        return None
    # предпочитаем прямой mp4 макс. ширины, иначе hls (yt-dlp вытянет)
    mp4 = [v for v in vids.values() if str(v.get("url", "")).endswith(".mp4")]
    pool = mp4 or list(vids.values())
    best = sorted(pool, key=lambda v: v.get("width", 0) or 0)[-1]
    return best.get("url")


def fetch_query(query: str, per_query: int, seen: set, dest_root: str) -> int:
    slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")[:40]
    print(f"\n── query: «{query}» → {slug} ──")
    got = 0
    bookmark = None
    pages = 0
    while got < per_query and pages < 4:
        pages += 1
        results, bookmark = search_pins(query, page_size=25, bookmark=bookmark)
        if not results:
            break
        for pin in results:
            if got >= per_query:
                break
            pid = str(pin.get("id") or "")
            if not pid or pid in seen:
                continue
            vurl = best_video_url(pin)
            if not vurl:
                continue
            seen.add(pid)
            out = WORKDIR / f"{pid}.mp4"
            download_pause()
            cmd = ["python", "-m", "yt_dlp", "--no-warnings", "--no-playlist",
                   "-f", "bestvideo[ext=mp4]/best[ext=mp4]/best/bv*+ba/b",
                   "--remux-video", "mp4",
                   "--user-agent", random.choice(UAS),
                   "-o", str(out), vurl]
            r = subprocess.run(cmd, capture_output=True, text=True)
            real = out if out.exists() else next(iter(WORKDIR.glob(f"{pid}.*")), None)
            if r.returncode != 0 or not real or real.stat().st_size < 20_000:
                print(f"  pin {pid}: download fail")
                if real and real.exists():
                    real.unlink()
                continue
            if yd_put(real, f"{dest_root}/{slug}/{real.name}"):
                got += 1
                print(f"  pin {pid}: ✅ {real.stat().st_size//1024}KB → {slug}/{real.name}")
            real.unlink(missing_ok=True)
        if not bookmark or bookmark == "-end-":
            break
        polite_sleep(3, 6)
    print(f"  «{query}»: {got} новых")
    return got


def main():
    print(f"Queries: {QUERIES}\nPer query: {PER_QUERY}\nDest: {DEST}")
    send_tg(f"📌 Pinterest overlay fetch — СТАРТ\n"
            f"запросов: {len(QUERIES)} × до {PER_QUERY} пинов\n"
            f"режим: анонимно (без кук), вежливый темп\n→ {DEST}")

    ids_path = f"{DEST}/_fetched_ids.json"
    seen_list = yd_read_json(ids_path) or []
    seen = set(seen_list)
    print(f"Дедуп: уже скачано {len(seen)} пинов")

    total = 0
    lines = []
    for q in QUERIES:
        n = fetch_query(q, PER_QUERY, seen, DEST)
        total += n
        lines.append(f"• {q}: {n}")
        polite_sleep(15, 35)

    yd_write_json(sorted(seen), ids_path)

    send_tg(f"✅ Pinterest overlay fetch — ГОТОВО\n"
            f"новых клипов: {total}\n" + "\n".join(lines) +
            f"\nвсего в дедупе: {len(seen)}\n→ {DEST}")
    print(f"\n✅ Done: {total} new clips, {len(seen)} total in dedup")


if __name__ == "__main__":
    main()
