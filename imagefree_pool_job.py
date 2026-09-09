#!/usr/bin/env python3
"""
imagefree_pool_job.py — пул стиллов через бесплатный imagefree.net (Z-Image-Turbo).

Что это: вторичный источник AI-стиллов в пул (постеры/концепты/Style Scout), НЕ основной
клип-сток. Сервис «как есть», без SLA и аккаунтов: POST /api/generate → taskId →
поллинг → PNG с Cloudflare R2. Проверено живым вызовом 2026-09-09 (без turnstile-токена).

Анти-блок меры (осознанный дизайн, не «обход защиты»):
  1. Строго ПОСЛЕДОВАТЕЛЬНО, один taskId в полёте. Сайт сам режет параллель:
     errorCode FREE_GENERATION_ACTIVE / FREE_TASK_BROWSER_ACTIVE / FREE_TASK_IP_ACTIVE.
  2. Джиттер между сабмитами MIN_DELAY..MAX_DELAY (default 6–15с) — «справедливый доступ»
     по их ToS, а не ровный автоматный ритм.
  3. Потолок на прогон MAX_TASKS (default 8). Никаких пачек на сотню.
  4. Умный поллинг: растущая задержка по той же схеме, что их фронтенд.
  5. СТОП прогона (без ретраев!) на сетевом/IP-лимите и CF-челлендже — ретраи = почти бан.
     separately: подряд идущие ошибки → тоже стоп (сайт явно непущен).
  6. Судья на входе (art_judge, fail-open): брак в пул не попадает (правило yaromat 30.07).

Склад на ЯД (правило именования yaromat 30.07): cloud_io/<YYYY-MM-DD>/imagefree_pool/
    ├─ set_<NN>/if_<NN>_<sha8>.png   (или pool_rejected/… при вердикте REJECT)
    ├─ manifest.json                 (что, чем, почём; всё для «video receipt»)
    └─ status.txt

Ручки (env): PROMPTS (построчно) · ASPECT (1:1|3:4|4:3|9:16|16:9, default 9:16) ·
MAX_TASKS (8) · MIN_DELAY/MAX_DELAY (6/15) · JUDGE (on/off, default on) ·
JUDGE_MODELS (панель как у arts_pool) · TG_TOKEN/TG_SECRET (опц. TG-пинг).

Exit codes: 0 = ок/частично; 2 = конфиг; 3 = стоп сайт-лимитом/IP; 4 = стоп CF-челленджем.
"""
import hashlib
import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# ── сеть ────────────────────────────────────────────────────────────────────
API = "https://imagefree.net"
UA  = "curl/8.5.0"                      # проверен живым вызовом (RU-бук, без токена)
TTL_STOP = 40                            # задержка молчания перед стопом (не висим в прогоне)

# растущие интервалы поллинга: та же схема, что у их фронтенда
POLL_MS = [6000, 4000, 10000, 15000, 20000, 30000, 30000, 30000, 30000, 30000,
           30000, 30000, 30000, 30000, 30000, 30000, 30000, 30000, 30000, 30000]

ASPECTS = {"1:1", "3:4", "4:3", "9:16", "16:9"}

# ── склад ───────────────────────────────────────────────────────────────────
YD = "ydrive:Content factory"
WORK = Path(os.environ.get("IMAGE_FREE_WORK") or "/tmp/imagefree_pool")
TODAY = date.today().isoformat()
REMOTE_ROOT = f"cloud_io/{TODAY}/imagefree_pool"

# ── судья (правило yaromat 30.07: «судья смотрит каждую генерацию, брак не в пул») ──
JUDGE_ON = (os.environ.get("JUDGE") or "on").lower() != "off"
JUDGE_MODELS = [m.strip() for m in (os.environ.get("JUDGE_MODELS") or
                "or:nvidia/nemotron-nano-12b-v2-vl:free,gh:openai/gpt-4o-mini,"
                "or:nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free").split(",") if m.strip()]
_judge_dead = {}


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, **kw)


NO_UPLOAD = os.environ.get("NO_UPLOAD") == "1"


def yd_put(local, remote):
    if NO_UPLOAD:
        print(f"  [no-upload] -> {YD}/{remote}")
        return True
    return sh(["rclone", "copyto", str(local), f"{YD}/{remote}"]).returncode == 0


def _post(path, body):
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": UA},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _http_error_kind(e: urllib.error.HTTPError) -> str:
    """Классификация HTTP: 403/412/503 CF-frontend могут быть челленджем."""
    body = ""
    try:
        body = e.read(2000).decode("utf-8", "replace").lower()
    except Exception:
        pass
    if e.code in (403, 412) or any(k in body for k in ("turnstile", "challenge", "cf-error", "forbidden")):
        return "cf_challenge"
    if e.code == 429:
        return "rate_limited"
    return f"http_{e.code}"


def submit(prompt, aspect):
    """Проба сабмита. Возвращает (taskId, err). err=None при успехе."""
    body = {"prompt": prompt, "aspect_ratio": aspect, "turnstile_token": ""}
    try:
        d = _post("/api/generate", body)
    except urllib.error.HTTPError as e:
        return None, _http_error_kind(e)
    except Exception as e:
        return None, f"net:{type(e).__name__}"
    if not isinstance(d, dict):
        return None, "bad_body"
    tid = d.get("taskId")
    if not tid:
        if d.get("errorCode"):
            return None, f"site:{d['errorCode']}"
        return None, f"site:{d.get('error', 'no taskId')}"
    return tid, None


def poll(tid):
    """Один поллинг. Возвращает json статуса или None при сетевом сбое."""
    url = f"{API}/api/generate/status?taskId={urllib.parse.quote(tid)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def download_image(url, out: Path):
    """Скачивает PNG, проверяет магический байт и размер."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=90) as r:
        data = r.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return False, "not_png"
    out.write_bytes(data)
    if out.stat().st_size < 5000:
        return False, "too_small"
    return True, ""


def dims(path: Path):
    try:
        from PIL import Image
        return f"{Image.open(path).size[0]}x{Image.open(path).size[1]}"
    except Exception:
        return "?"


def judge(path):
    if not JUDGE_ON:
        return {"verdict": "OFF", "violations": [], "flaws": [], "n_votes": 0}
    try:
        from art_judge import judge_file
        return judge_file(path, models=JUDGE_MODELS, dead=_judge_dead)
    except Exception as e:
        return {"verdict": "ERROR", "violations": [], "flaws": [], "n_votes": 0,
                "note": f"{type(e).__name__}"}


def tg_notify(msg: str):
    """Опц. пик в тред фактори (5). Не-фатален: уведомление не должно ронять прогон."""
    token = os.environ.get("TG_TOKEN", "")
    secret = os.environ.get("TG_SECRET", "")
    worker = os.environ.get("TG_WORKER") or "https://content-factory-worker-2.mat3213.workers.dev"
    if not token or not secret:
        return
    data = json.dumps({"chat_id": -1003946370426, "message_thread_id": 5, "text": msg}).encode()
    req = urllib.request.Request(
        f"{worker}/bot{token}/sendMessage", data=data,
        headers={"Content-Type": "application/json", "X-Worker-Secret": secret, "User-Agent": UA})
    try:
        urllib.request.urlopen(req, timeout=25)
    except Exception:
        pass


def wait_for_task(tid) -> dict:
    """Поллинг до completed/failed/timeout. Возвращает статус-словарь."""
    for i, dt in enumerate(POLL_MS):
        p = poll(tid)
        url = ""
        if isinstance(p, dict):
            st = p.get("status")
            if st == "completed":
                return {"ok": True, "image": p.get("image"), "url": url}
            if st == "failed":
                return {"ok": False, "reason": p.get("error") or p.get("errorCode") or "failed",
                        "errorCode": p.get("errorCode")}
            if p.get("errorCode"):
                return {"ok": False, "reason": f"site:{p['errorCode']}",
                        "errorCode": p["errorCode"]}
        else:
            # сетевой обрыв: не паникуем, продолжаем поллить — таск живёт на их стороне
            pass
        time.sleep(dt / 1000.0)
    return {"ok": False, "reason": "timeout", "errorCode": "TIMEOUT"}


def parse_prompts() -> list[str]:
    """Промпты из PROMPTS env (построчно), как передаёт workflow input. Пустой → конфиг."""
    raw = os.environ.get("PROMPTS", "").strip()
    plist = [p.strip() for p in raw.splitlines() if p.strip()]
    return plist


def main():
    prompts = parse_prompts()
    if not prompts:
        print("нет PROMPTS — укажи промпты (workflow input prompts)", file=sys.stderr)
        sys.exit(2)
    aspect = os.environ.get("ASPECT") or "9:16"
    if aspect not in ASPECTS:
        print(f"ASPECT не из списка: {aspect}", file=sys.stderr)
        sys.exit(2)
    max_tasks = int(os.environ.get("MAX_TASKS") or 8)
    min_delay = float(os.environ.get("MIN_DELAY") or 6)
    max_delay = float(os.environ.get("MAX_DELAY") or 15)
    prompts = prompts[:max_tasks]

    WORK.mkdir(parents=True, exist_ok=True)
    print(f"imagefree_pool · {TODAY} · aspect={aspect} · задач={len(prompts)} "
          f"(max {max_tasks}) · джиттер {min_delay:.0f}..{max_delay:.0f}с · judge={JUDGE_ON}")

    manifest, exit_code, stop_reason = [], 0, ""
    consec_errors, task_num = 0, 0

    for prompt in prompts:
        if stop_reason:
            break
        task_num += 1
        sha = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:8]
        print(f"\n[{task_num}/{len(prompts)}] {prompt[:90]}")
        rec = {"n": task_num, "prompt": prompt, "prompt_sha": sha, "aspect": aspect}

        tid, err = submit(prompt, aspect)
        if err:
            rec["status"] = "submit_error"
            rec["error"] = err
            manifest.append(rec)
            if err == "cf_challenge":
                stop_reason, exit_code = "cf-challenge", 4
            elif err == "rate_limited" or err.startswith("site:FREE_TASK_IP"):
                stop_reason, exit_code = "site-ip-limit", 3
            else:
                consec_errors += 1
                if consec_errors >= 3:
                    stop_reason, exit_code = "repeated-errors", 3
            print(f"  ✗ submit: {err} → {stop_reason or 'продолжаем'}")
            if stop_reason:
                break
            time.sleep(random.uniform(min_delay, max_delay))
            continue
        rec["task_id"] = tid
        consec_errors = 0
        print(f"  taskId={tid[:8]}… (поллинг)")

        res = wait_for_task(tid)
        if res.get("errorCode") == "FREE_TASK_IP_ACTIVE":
            rec["status"] = "stopped"
            rec["error"] = "site:FREE_TASK_IP_ACTIVE"
            stop_reason, exit_code = "site-ip-limit", 3
            print(f"  ✗ IP-лимит сайта — СТОП прогона")
            manifest.append(rec)
            break
        if res.get("errorCode") == "FREE_GENERATION_ACTIVE":
            # параллельная генка на их стороне: страйк на счётчик, короткая выждать, retry-заход
            rec["status"] = "busy"
            rec["error"] = "site:FREE_GENERATION_ACTIVE"
            manifest.append(rec)
            consec_errors += 1
            if consec_errors >= 3:
                stop_reason, exit_code = "site-busy", 3
                print(f"  ✗ сайт занят (x3) — СТОП")
                break
            print(f"  ~ сайт занят, короткая пауза, попытка дальше")
            time.sleep(random.uniform(10, 20))
            continue

        if not res.get("ok"):
            rec["status"] = "gen_error"
            rec["error"] = res.get("reason", "unknown")
            manifest.append(rec)
            consec_errors += 1
            if consec_errors >= 3:
                stop_reason, exit_code = "repeated-errors", 3
                print(f"  ✗ ошибка генерации x3 — СТОП")
                break
            print(f"  ✗ {res.get('reason')}")
            time.sleep(random.uniform(min_delay, max_delay))
            continue

        img_url = res.get("image", "")
        if not img_url:
            rec["status"] = "gen_error"
            rec["error"] = "no_image_url"
            manifest.append(rec)
            continue

        out = WORK / f"if_{task_num:02d}_{sha}.png"
        ok, err = download_image(img_url, out)
        if not ok:
            rec["status"] = "dl_error"
            rec["error"] = err
            manifest.append(rec)
            print(f"  ✗ download: {err}")
            continue

        v = judge(out)
        rec["judge"] = {k: v[k] for k in ("verdict", "violations", "flaws", "n_votes")}
        if v["verdict"] == "REJECT":
            rec["status"] = "rejected"
            rec["violations"] = v.get("violations", [])
            print(f"  🔴 судья: {','.join(v.get('violations', []))}")
            if yd_put(out, f"{REMOTE_ROOT}/pool_rejected/if_{task_num:02d}_{sha}.png"):
                rec["upload"] = "rejected"
        else:
            rec["status"] = "ok"
            rec["dims"] = dims(out)
            rec["bytes"] = out.stat().st_size
            rec["image"] = img_url
            mark = {"FLAG": "🟡", "PARTIAL": "⚠️", "ERROR": "⚠️"}.get(v["verdict"], "✓")
            print(f"  {mark} {rec['dims']} {rec['bytes']//1024}КБ")
            if yd_put(out, f"{REMOTE_ROOT}/set_{task_num:02d}/if_{task_num:02d}_{sha}.png"):
                rec["upload"] = "ok"
            else:
                print(f"  ⚠️ ЯД upload не удался (осталось локально)")
        manifest.append(rec)
        consec_errors = 0
        time.sleep(random.uniform(min_delay, max_delay))

    # ── осадок ─────────────────────────────────────────────────────────────
    ok_n = sum(1 for r in manifest if r.get("status") == "ok")
    rej_n = sum(1 for r in manifest if r.get("status") == "rejected")
    mf = WORK / "manifest.json"
    mf.write_text(json.dumps({
        "date": TODAY, "aspect": aspect, "max_tasks": max_tasks,
        "judge": {"on": JUDGE_ON, "panel": JUDGE_MODELS},
        "stop": {"reason": stop_reason, "exit_code": exit_code},
        "summary": {"attempted": len(manifest), "ok": ok_n, "rejected": rej_n,
                    "by_verdict": dict(Counter(
                        (r.get("judge") or {}).get("verdict") for r in manifest))},
        "items": manifest}, ensure_ascii=False, indent=1), encoding="utf-8")
    yd_put(mf, f"{REMOTE_ROOT}/manifest.json")
    print(f"\nИТОГ: ok={ok_n}, rejected={rej_n}, attempted={len(manifest)} "
          f"stop={stop_reason or 'нет'}")
    print(f"→ {YD}/{REMOTE_ROOT}/")

    if stop_reason:
        tg_notify(f"⚠️ imagefree_pool: стоп ({stop_reason}). "
                  f"ok={ok_n} rej={rej_n} из {len(prompts)}. "
                  f"Детали: {YD}/{REMOTE_ROOT}/manifest.json")
    elif ok_n:
        tg_notify(f"✅ imagefree_pool: {ok_n} стиллов на ЯД "
                  f"({YD}/{REMOTE_ROOT}/) · rej={rej_n} из {len(prompts)}")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()