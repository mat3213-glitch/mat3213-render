#!/usr/bin/env python3
"""pool_daily.py — авто-пул промтов на выбор (задание yaromat 2026-07-11).

Тянет из pool_prompts.json на ЯД-гейте СЛЕДУЮЩИЙ несделанный промт для Qwen,
генерит ОДИН клип, кладёт в ту же гейт-папку как <engine>_<id>_<slug>.mp4. Сама гейт-папка = журнал
прогресса (идемпотентно: файл есть → пропуск). Крутится по qwen_daily под квоту.

Env: ENGINE=qwen, GATE (ydrive:...pool_gate/<pool>), LIMIT (дефолт 1).
"""
import os
import re
import sys
import json
import subprocess

ENGINE = os.environ.get("ENGINE", "qwen")
GATE = os.environ.get("GATE", "ydrive:Content factory/cloud_io/pool_gate/adult_dnb_2026-07-11")
LIMIT = int(os.environ.get("LIMIT", "1"))


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def slug(t):
    t = re.sub(r"[^a-z0-9]+", "_", (t or "").lower().encode("ascii", "ignore").decode()).strip("_")
    return t[:24] or "clip"


def pick_todo():
    if sh(["rclone", "copyto", f"{GATE}/pool_prompts.json", "pool_prompts.json"]).returncode != 0:
        sys.exit("[pool_daily] нет pool_prompts.json на гейте")
    prompts = json.load(open("pool_prompts.json"))["prompts"]
    existing = sh(["rclone", "lsf", GATE]).stdout or ""
    # СДЕЛАННЫМ считается ТОЛЬКО реальный .mp4. Раньше искали подстроку `<engine>_<id>_`
    # по всему листингу: маркер провала не должен считаться готовым mp4.
    done, failed = set(), set()
    for line in existing.splitlines():
        line = line.strip()
        m = re.match(rf"{ENGINE}_(\d+)_.*\.mp4$", line)
        if m:
            done.add(int(m.group(1)))
            continue
        m = re.match(rf"{ENGINE}_(\d+)_.*\.(FAILED\.txt|fail\.png)$", line)
        if m:
            failed.add(int(m.group(1)))
    failed -= done                      # провалился, потом добился — уже не провал
    todo = [p for p in prompts if int(p["id"]) not in done]
    print(f"[pool_daily {ENGINE}] сделано={len(done)} осталось={len(todo)}", flush=True)
    if failed:
        print(f"[pool_daily {ENGINE}] ⚠ ретрай после провала: {sorted(failed)} "
              f"({len(failed)} шт). Если один и тот же id висит тут прогон за прогоном — "
              f"промпт или движок не тянут, чинить руками.", flush=True)
    return todo


def gen_one(p):
    pid = p["id"]
    out = f"{ENGINE}_{pid}_{slug(p.get('title'))}.mp4"
    prompt = p["gen_prompt"]
    print(f"[pool_daily] #{pid} «{p.get('title')}» → {out}", flush=True)
    if ENGINE == "qwen":
        r = subprocess.run(["python3", "GEN.py", "video", prompt, "--ratio", "9:16",
                            "--out", out, "--timeout", "1200"], cwd="qwen")
        if r.returncode != 0:
            return False
        return sh(["rclone", "copyto", f"qwen/{out}", f"{GATE}/{out}"]).returncode == 0
    sys.exit(f"неизвестный ENGINE={ENGINE}")


def main():
    todo = pick_todo()
    if not todo:
        print("[pool_daily] пул для движка ПОЛНЫЙ — простой", flush=True)
        return
    ok = 0
    for p in todo[:LIMIT]:
        if gen_one(p):
            ok += 1
    print(f"[pool_daily] сгенерено за прогон: {ok}/{min(LIMIT, len(todo))}", flush=True)
    if ok == 0:
        sys.exit("[pool_daily] ни один не сгенерился")


if __name__ == "__main__":
    main()
