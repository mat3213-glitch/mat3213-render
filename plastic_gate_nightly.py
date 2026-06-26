#!/usr/bin/env python3
"""
plastic_gate_nightly.py — ОРКЕСТРАТОР ночного гейта пластмассовости (вариант B).

Изолированный шаг конвейера (консенсус Claude+mimo): один nightly-воркфлоу ПОСЛЕ генераций
проходит свежие пулы трёх генераторов за сегодня/вчера (UTC) и гейтит каждый через
plastic_gate.py (mimo+рубрика Claude, enforce=1 → пластик в _rejected/, обратимо).

Изоляция: не трогает daily-генераторы, не дублирует restore mimo, fault-tolerant
(пул отсутствует → тихо пропустить; уже гейчен → skip по gate_report.json, если не FORCE).

Уведомление итога в TG-тред 5 (если есть CLOUDFLARE_WORKER+токен).
"""
import os, subprocess, datetime, json, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
GATE = os.path.join(HERE, "plastic_gate.py")
POOLS = ["qwen_pool", "veofree_pool", "hunyuan_pool"]
THRESHOLD = os.environ.get("THRESHOLD", "55")
FORCE = os.environ.get("FORCE", "0") == "1"
YD = "ydrive:Content factory"

today = datetime.datetime.utcnow().date()
DATES = [today.isoformat(), (today - datetime.timedelta(days=1)).isoformat()]

def sh(c): return subprocess.run(c, shell=True, capture_output=True, text=True)

def exists(rel):
    r = sh(f'rclone lsf "{YD}/{rel}/" --max-depth 1')
    return r.returncode == 0 and r.stdout.strip() != ""

def has_report(rel):
    return sh(f'rclone lsf "{YD}/{rel}/gate_report.json"').stdout.strip() != ""

def media_count(rel):
    r = sh(f'rclone lsf "{YD}/{rel}/" --max-depth 1 --include "*.mp4" --include "*.png" --include "*.jpg"')
    return len([x for x in r.stdout.splitlines() if x.strip()])

summary = []
for pool in POOLS:
    for d in DATES:
        rel = f"cloud_io/{pool}/{d}"
        if not exists(rel):
            continue
        if media_count(rel) == 0:
            continue
        if has_report(rel) and not FORCE:
            print(f"[skip] {rel} — уже гейчен (gate_report.json есть)")
            summary.append(f"⏭ {pool}/{d}: уже гейчен")
            continue
        print(f"\n=== ГЕЙТ {rel} ===")
        env = dict(os.environ, POOL=rel, THRESHOLD=THRESHOLD, ENFORCE="1")
        r = subprocess.run(["python", "-u", GATE], env=env)
        # прочитать отчёт
        rep = sh(f'rclone cat "{YD}/{rel}/gate_report.json"').stdout
        try:
            j = json.loads(rep)
            summary.append(f"✅ {pool}/{d}: всего {j['total']}, pass {j['passed']}, REJECT {j['rejected']}")
        except Exception:
            summary.append(f"⚠️ {pool}/{d}: гейт прошёл, отчёт не прочитан (rc={r.returncode})")

if not summary:
    summary.append("нет свежих пулов за сегодня/вчера — гейтить нечего")

text = "🚦 Ночной гейт пластмассовости (mimo+рубрика, порог " + THRESHOLD + ")\n" + "\n".join(summary)
print("\n" + text)

# TG-уведомление через CF Worker (как qwen_daily)
worker = os.environ.get("CLOUDFLARE_WORKER")
token = os.environ.get("TELEGRAM_BOT_TOKEN")
chat = os.environ.get("TG_CHAT_ID")
thread = os.environ.get("TG_THREAD_ID", "5")
if worker and token and chat:
    try:
        body = {"chat_id": chat, "text": text[:3800]}
        if thread:
            body["message_thread_id"] = int(thread)
        req = urllib.request.Request(f"{worker.rstrip('/')}/bot{token}/sendMessage",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": "curl/8.0"},
                                     method="POST")
        urllib.request.urlopen(req, timeout=30)
        print("[tg] уведомление отправлено")
    except Exception as e:
        print(f"[tg] не отправлено: {e}")
