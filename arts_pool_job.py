#!/usr/bin/env python3
"""
arts_pool_job.py — пул артов на ПОЛНЫЙ трек: CF Workers AI + Pollinations дублем, на одних промптах.

Зачем: vzrosly собирает 28с (не весь трек) из 11 именованных слотов → полный трек 196.7с =
7 сегментов × 11 слотов = 77 артов, каждый сегмент со своим seed → своя хореография монтажа.
Два движка на одном промпте = честное сравнение + запас, если один мажет.

Читает с ЯД: <base>/pool_prompts.json
  {"sets":[{"set":1,"act":"car","slots":{"anchor":"что в кадре", ...}}, ...]}
Кладёт на ЯД:  <base>/pool/set_<N>/cf/<slot>.png  и  <base>/pool/set_<N>/poll/<slot>.png
               <base>/pool/manifest.json (что сгенерилось, чем, почём)

ЗАМОК ПАЛИТРЫ дописывается КОДОМ (looks.json, пресет cold_noir_portishead) — промпты его НЕ содержат.
Единый цвет через любые сюжеты, без пост-грейда. См. [[reference_palette_lock]].

БЮДЖЕТ CF (замерен по докам 2026-07-16): free = 10 000 нейронов/сутки, сброс 00:00 UTC.
flux-1-schnell = 4.8 нейрона/тайл 512×512 + 9.6/шаг. Наши 1024×1024 = 4 тайла = 19.2 + шаги.
  steps=4 (родной режим schnell) → 57.6 н/арт → ~173 арта/сутки
  steps=8 (максимум)            → 96.0 н/арт → ~104 арта/сутки
Скрипт СЧИТАЕТ расход и останавливает CF, не дойдя до лимита (Pollinations продолжает — он без лимита).
⚠️ ОБА движка игнорят width/height: CF flux даёт 1024×1024 квадрат, Pollinations — 576×1024.

Ручки: JOB_ID, STEPS (4), NEURON_BUDGET (9500), PROVIDERS (cf,poll), ONLY_SET (пусто = все).
"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from looks import apply_look, negative_for   # noqa: E402

YD = "ydrive:Content factory"
LOOK = os.environ.get("LOOK") or "cold_noir_portishead"
# ГРАБЛЯ: .get(k, default) при ПУСТОЙ переменной вернёт "", не дефолт. Только `or`.
CF_URL = (os.environ.get("IMG_WORKER_URL") or "https://yaromat-img.mat3213.workers.dev").rstrip("/")
CF_SECRET = os.environ.get("IMG_WORKER_SECRET", "")
CF_MODEL = "@cf/black-forest-labs/flux-1-schnell"
STEPS = int(os.environ.get("STEPS") or 4)
BUDGET = int(os.environ.get("NEURON_BUDGET") or 9500)
WORK = Path("/tmp/arts_pool")


def neurons(steps):
    """1024×1024 = 4 тайла × 4.8 + steps × 9.6"""
    return 4 * 4.8 + steps * 9.6


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, **kw)


def yd_get(remote, local):
    return sh(["rclone", "copyto", f"{YD}/{remote}", str(local)]).returncode == 0


def yd_put(local, remote):
    return sh(["rclone", "copyto", str(local), f"{YD}/{remote}"]).returncode == 0


def gen_cf(prompt, out):
    import requests
    body = {"prompt": prompt, "model": CF_MODEL, "steps": STEPS,
            "negative_prompt": negative_for(LOOK)}
    try:
        r = requests.post(f"{CF_URL}/gen", headers={"X-Worker-Secret": CF_SECRET},
                          json=body, timeout=(15, 200))
    except Exception as e:
        return False, f"net: {e}"
    if r.status_code != 200 or not r.content or len(r.content) < 5000:
        return False, f"http {r.status_code} len={len(r.content or b'')}"
    Path(out).write_bytes(r.content)
    return True, ""


def gen_poll(prompt, out, seed):
    # без ключа, без аккаунта; width/height игнорятся (всегда 576×1024)
    url = (f"https://image.pollinations.ai/prompt/{urllib.parse.quote(prompt)}"
           f"?width=1024&height=1024&nologo=true&seed={seed}")
    r = sh(["curl", "-s", "-A", "curl/8", "--max-time", "170", "-o", str(out), "-w", "%{http_code}", url])
    code = (r.stdout or b"").decode().strip()
    if code != "200" or not Path(out).exists() or Path(out).stat().st_size < 5000:
        return False, f"http {code}"
    return True, ""


def main():
    job = os.environ.get("JOB_ID")
    if not job:
        sys.exit("JOB_ID not set")
    provs = [p.strip() for p in (os.environ.get("PROVIDERS") or "cf,poll").split(",") if p.strip()]
    if "cf" in provs and not CF_SECRET:
        sys.exit("IMG_WORKER_SECRET пуст — CF не сгенерит ни одного арта. Стоп (а не 77 тихих провалов).")
    only = (os.environ.get("ONLY_SET") or "").strip()
    base = f"cloud_io/render_jobs/{job}"
    WORK.mkdir(parents=True, exist_ok=True)

    pf = WORK / "pool_prompts.json"
    if not yd_get(f"{base}/pool_prompts.json", pf):
        sys.exit("нет pool_prompts.json на ЯД")
    sets = json.loads(pf.read_text(encoding="utf-8"))["sets"]
    if only:
        sets = [s for s in sets if str(s["set"]) == only]
    total = sum(len(s["slots"]) for s in sets)
    print(f"наборов: {len(sets)}, артов: {total}, провайдеры: {provs}, steps={STEPS}")
    print(f"бюджет CF: {BUDGET} нейронов | цена арта {neurons(STEPS):.1f} → "
          f"влезает {int(BUDGET / neurons(STEPS))} шт")

    spent, man, seed = 0.0, [], 1000
    for st in sets:
        n, act = st["set"], st.get("act", "")
        for slot, subj in st["slots"].items():
            seed += 1
            prompt = apply_look(subj, LOOK)   # ЗАМОК ПАЛИТРЫ дописывается тут
            rec = {"set": n, "act": act, "slot": slot, "seed": seed, "subject": subj}
            if "cf" in provs:
                if spent + neurons(STEPS) > BUDGET:
                    print(f"  [set{n}/{slot}] CF: БЮДЖЕТ ИСЧЕРПАН ({spent:.0f}/{BUDGET}) — пропуск")
                    rec["cf"] = "budget_exhausted"
                else:
                    out = WORK / f"cf_{n}_{slot}.png"
                    ok, err = gen_cf(prompt, out)
                    if ok:
                        spent += neurons(STEPS)
                        yd_put(out, f"{base}/pool/set_{n}/cf/{slot}.png")
                        rec["cf"] = "ok"
                        print(f"  [set{n}/{slot}] CF ✓ {out.stat().st_size//1024}КБ "
                              f"({spent:.0f}/{BUDGET} н)")
                    else:
                        rec["cf"] = f"fail: {err}"
                        print(f"  [set{n}/{slot}] CF ✗ {err}")
            if "poll" in provs:
                out = WORK / f"poll_{n}_{slot}.png"
                ok, err = gen_poll(prompt, out, seed)
                if ok:
                    yd_put(out, f"{base}/pool/set_{n}/poll/{slot}.png")
                    rec["poll"] = "ok"
                    print(f"  [set{n}/{slot}] POLL ✓ {out.stat().st_size//1024}КБ")
                else:
                    rec["poll"] = f"fail: {err}"
                    print(f"  [set{n}/{slot}] POLL ✗ {err}")
                time.sleep(2)   # вежливость к бесплатному сервису
            man.append(rec)

    mf = WORK / "manifest.json"
    mf.write_text(json.dumps({
        "job": job, "look": LOOK, "steps": STEPS,
        "neurons_spent": round(spent), "neurons_budget": BUDGET,
        "cf_ok": sum(1 for r in man if r.get("cf") == "ok"),
        "poll_ok": sum(1 for r in man if r.get("poll") == "ok"),
        "items": man}, ensure_ascii=False, indent=1), encoding="utf-8")
    yd_put(mf, f"{base}/pool/manifest.json")
    print(f"\nИТОГ: CF {sum(1 for r in man if r.get('cf')=='ok')}/{total}, "
          f"Pollinations {sum(1 for r in man if r.get('poll')=='ok')}/{total}, "
          f"нейронов {spent:.0f}/{BUDGET}")
    print(f"→ {YD}/{base}/pool/")


if __name__ == "__main__":
    main()
