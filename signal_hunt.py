#!/usr/bin/env python3
"""signal_hunt.py — МОСТ Grok→auto_analyst (ТОЛЬКО мост, без LLM-brainstorm).

История: brainstorm на litellm выпилен — LLM без реального поиска либо повторяет
очевидное, либо ВЫДУМЫВАЕТ (проверка 2026-06-23: 0/13 неочевидных репо реальны).
Дискавери = заземлённый поиск: repo_scout (GitHub Search) + ручной Grok (X/Reddit/Habr).

Этот скрипт = «перекладыватель ссылок»: берёт source_link из свежего
signals/incoming/grok_<date>.json (что прислал ручной Grok — реальные находки) →
дедуп против verified_tools → ЯД analyst_queue/pending/ → диспатч auto_analyst.yml
(matrix --from-queue, тред 1653 GROK SCOUT). Песочница auto_analyst заземляет: реально
клонит/курлит каждый URL, мёртвое отсеивается.
"""
import base64
import json
import os
import subprocess
import urllib.request
from datetime import datetime

YD = "ydrive:Content factory"
QUEUE = f"{YD}/cloud_io/CreativeLab/analyst_queue/pending"
TOOLS = f"{YD}/verified_tools"
SIGNALS_REPO = "mat3213-glitch/mat3213-signals"
RENDER_REPO = "mat3213-glitch/mat3213-render"
GROK_THREAD = "1653"   # GROK SCOUT
GH_TOKEN = os.environ.get("GH_DISPATCH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")


def grok_signals_urls():
    """source_link'и из свежего grok_*.json (ручной Grok) — это и есть мост Grok→auto_analyst."""
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{SIGNALS_REPO}/contents/signals/incoming",
            headers={"Authorization": f"token {GH_TOKEN}", "User-Agent": "curl/8.0"})
        files = json.load(urllib.request.urlopen(req, timeout=30))
        groks = sorted(f["name"] for f in files if f["name"].startswith("grok_"))
        if not groks:
            print("нет grok_*.json")
            return []
        latest = groks[-1]
        req2 = urllib.request.Request(
            f"https://api.github.com/repos/{SIGNALS_REPO}/contents/signals/incoming/{latest}",
            headers={"Authorization": f"token {GH_TOKEN}", "User-Agent": "curl/8.0"})
        doc = json.loads(base64.b64decode(json.load(urllib.request.urlopen(req2, timeout=30))["content"]))
        items = doc.get("candidates") or doc.get("items") or []
        urls = [it.get("source_link", "") for it in items
                if str(it.get("source_link", "")).startswith("https://github.com/")]
        print(f"[мост] {latest}: {len(urls)} github URL")
        return urls
    except Exception as e:
        print(f"[мост] fail: {str(e)[:140]}")
        return []


def existing_slugs():
    r = subprocess.run(["rclone", "lsf", TOOLS, "--dirs-only"], capture_output=True, text=True)
    return {x.strip().rstrip("/") for x in r.stdout.splitlines() if x.strip()}


def slug(u):
    return u.replace("https://", "").replace("/", "__").replace(".", "__").replace("-", "__").rstrip("_")


def main():
    seen = existing_slugs()
    out, uniq = [], set()
    for u in grok_signals_urls():
        u = u.rstrip("/")
        if u in uniq or slug(u) in seen:
            continue
        uniq.add(u)
        out.append(u)
    if not out:
        print("нет новых ссылок от Grok (всё уже проверено или файла нет)")
        return
    out = out[:15]
    date = datetime.now().strftime("%Y-%m-%d")
    open("hunt.txt", "w").write("\n".join(out) + "\n")
    subprocess.run(["rclone", "copyto", "hunt.txt", f"{QUEUE}/grok_{date}.txt"], check=True)
    print(f"в очередь: {len(out)} URL → {QUEUE}/grok_{date}.txt")
    for u in out:
        print("  +", u)
    # диспатч auto_analyst (пустой targets → --from-queue; тред 1653 GROK SCOUT)
    body = json.dumps({"ref": "main", "inputs": {"targets": "", "thread": GROK_THREAD}}).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{RENDER_REPO}/actions/workflows/auto_analyst.yml/dispatches",
        data=body, method="POST",
        headers={"Authorization": f"token {GH_TOKEN}", "User-Agent": "curl/8.0",
                 "Accept": "application/vnd.github+json"})
    try:
        urllib.request.urlopen(req, timeout=30)
        print(f"auto_analyst диспатчнут (--from-queue, тред {GROK_THREAD})")
    except Exception as e:
        print(f"dispatch fail (очередь подхватит cron): {str(e)[:140]}")


if __name__ == "__main__":
    main()
