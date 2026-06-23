#!/usr/bin/env python3
"""signal_hunt.py — РЕАЛЬНЫЙ автономный скаут (замена фейк-автономии Grok).

Grok оказался симулятором (нет background-раннера, пушил «понарошку»). Это — настоящая
ежедневная замена на GH Actions (US-IP). Два источника кандидатов:
  1. litellm-brainstorm: free-LLM на US-IP раннере (Groq и др.) по профилю+рубрике предлагает
     8-15 URL инструментов/LLM-эндпоинтов. LLM = генератор идей, ЗАЗЕМЛЕНИЕ = auto_analyst
     (мёртвые/выдуманные URL отсеются на fetch, реальные получат балл).
  2. МОСТ Grok→auto_analyst: source_link'и из свежего signals/incoming/grok_<date>.json
     (ручной Grok отдаёт ИНСТРУМЕНТЫ без media_url → визуальный analyze их не берёт → ведём сюда).
Дедуп против verified_tools/ → пишем в ЯД analyst_queue/pending/hunt_<date>.txt →
диспатчим auto_analyst.yml (matrix --from-queue) → вердикты в тред 634 + уведомления.
"""
import base64
import json
import os
import re
import subprocess
import urllib.request
from datetime import datetime

YD = "ydrive:Content factory"
QUEUE = f"{YD}/cloud_io/CreativeLab/analyst_queue/pending"
TOOLS = f"{YD}/verified_tools"
SIGNALS_REPO = "mat3213-glitch/mat3213-signals"
RENDER_REPO = "mat3213-glitch/mat3213-render"
GH_TOKEN = os.environ.get("GH_DISPATCH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")

PROFILE = """Ищем БЕСПЛАТНЫЕ инструменты/нейросети для AI-фабрики музыкальных клипов (электроника).
ОСОБЫЙ УПОР: free-LLM для ОРКЕСТРОВКИ — gateway/router/CLI/free-tier API, OpenAI-совместимые,
RU-доступные, без карты/KYC (расширяют пул воркеров fanout.py).
ТАКЖЕ: CPU/CLI медиа-крафт (БЕЗ GPU) — нарезка/переходы/грейд/глитч/процедурная анимация/
типографика/аудио-обработка/транскрипция/эстетик-скоринг (экосистема ffmpeg/OpenCV/PIL).
Критерий 100/100: бесплатно + CPU/без GPU + препарируемо (модуль/CLI) + прямо в стек + дешёвая интеграция.
НЕ брать: GPU-тяжёлое, платный монолит, нарушает ToS."""


def litellm_brainstorm(n=12):
    import litellm
    litellm.drop_params = True
    if os.environ.get("GITHUB_TOKEN") and not os.environ.get("GITHUB_API_KEY"):
        os.environ["GITHUB_API_KEY"] = os.environ["GITHUB_TOKEN"]
    models = [m for m in os.environ.get("LITELLM_GH_MODELS", "").split(",") if m.strip()] or [
        "groq/llama-3.3-70b-versatile",
        "openrouter/meta-llama/llama-3.3-70b-instruct:free",
        "github/gpt-4o-mini",
    ]
    prompt = (PROFILE + f"\n\nПредложи {n} КОНКРЕТНЫХ реально существующих живых GitHub-репозиториев "
              "под этот профиль, разнообразных (не только самые популярные — ищи рычаг). Только список "
              "ПОЛНЫХ URL вида https://github.com/owner/repo, по одному на строку, без описаний и нумерации.")
    for m in models:
        try:
            r = litellm.completion(model=m, messages=[{"role": "user", "content": prompt}],
                                   timeout=120, max_tokens=800)
            txt = r.choices[0].message.content or ""
            urls = re.findall(r"https://github\.com/[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+", txt)
            if urls:
                print(f"[brainstorm] {m}: {len(urls)} URL")
                return urls
        except Exception as e:
            print(f"[brainstorm] {m} fail: {str(e)[:120]}")
    return []


def grok_signals_urls():
    """source_link'и из свежего grok_*.json (ручной Grok) — это и есть мост Grok→auto_analyst."""
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{SIGNALS_REPO}/contents/signals/incoming",
            headers={"Authorization": f"token {GH_TOKEN}", "User-Agent": "curl/8.0"})
        files = json.load(urllib.request.urlopen(req, timeout=30))
        groks = sorted(f["name"] for f in files if f["name"].startswith("grok_"))
        if not groks:
            return []
        latest = groks[-1]
        req2 = urllib.request.Request(
            f"https://api.github.com/repos/{SIGNALS_REPO}/contents/signals/incoming/{latest}",
            headers={"Authorization": f"token {GH_TOKEN}", "User-Agent": "curl/8.0"})
        doc = json.loads(base64.b64decode(json.load(urllib.request.urlopen(req2, timeout=30))["content"]))
        items = doc.get("candidates") or doc.get("items") or []
        urls = [it.get("source_link", "") for it in items
                if str(it.get("source_link", "")).startswith("https://github.com/")]
        print(f"[bridge] {latest}: {len(urls)} github URL")
        return urls
    except Exception as e:
        print(f"[bridge] fail: {str(e)[:120]}")
        return []


def existing_slugs():
    r = subprocess.run(["rclone", "lsf", TOOLS, "--dirs-only"], capture_output=True, text=True)
    return {x.strip().rstrip("/") for x in r.stdout.splitlines() if x.strip()}


def slug(u):
    return u.replace("https://", "").replace("/", "__").replace(".", "__").replace("-", "__").rstrip("_")


def main():
    cands = litellm_brainstorm() + grok_signals_urls()
    seen = existing_slugs()
    out, uniq = [], set()
    for u in cands:
        u = u.rstrip("/")
        if u in uniq:
            continue
        uniq.add(u)
        if slug(u) in seen:
            continue  # уже в verified_tools — не гоняем повторно
        out.append(u)
    if not out:
        print("нет новых кандидатов (всё уже проверено или пусто)")
        return
    out = out[:15]
    date = datetime.now().strftime("%Y-%m-%d")
    open("hunt.txt", "w").write("\n".join(out) + "\n")
    subprocess.run(["rclone", "copyto", "hunt.txt", f"{QUEUE}/hunt_{date}.txt"], check=True)
    print(f"в очередь: {len(out)} URL → {QUEUE}/hunt_{date}.txt")
    for u in out:
        print("  +", u)
    # диспатч auto_analyst (пустой targets → --from-queue в воркфлоу)
    body = json.dumps({"ref": "main", "inputs": {"targets": ""}}).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{RENDER_REPO}/actions/workflows/auto_analyst.yml/dispatches",
        data=body, method="POST",
        headers={"Authorization": f"token {GH_TOKEN}", "User-Agent": "curl/8.0",
                 "Accept": "application/vnd.github+json"})
    try:
        urllib.request.urlopen(req, timeout=30)
        print("auto_analyst диспатчнут (--from-queue)")
    except Exception as e:
        print(f"dispatch fail (очередь подхватит недельный cron): {str(e)[:120]}")


if __name__ == "__main__":
    main()
