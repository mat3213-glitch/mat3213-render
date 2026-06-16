#!/usr/bin/env python3
"""
veofree_daily.py — ежедневная обёртка генерации VeoFree (t2v).

Генерирует N_VID видео по вайб-промптам через боевой veofree_gen.py,
который сам заливает результат на ЯД. Шлёт TG-уведомления старт/финиш.

Запуск:
  python3 veofree_daily.py              # дефолт: 2 видео
  python3 veofree_daily.py --dry        # печать плана без вызова сети
  N_VID=4 python3 veofree_daily.py
"""
import os
import sys
import json
import time
import random
import argparse
import subprocess
from pathlib import Path
from datetime import date
import requests

# Устойчивый импорт media_register (работает и из подпапки, и из корня репо)
_here = Path(__file__).resolve()
for _p in (_here.parent, _here.parent.parent):
    sys.path.insert(0, str(_p))
try:
    import media_register
except Exception:
    media_register = None


def _register(path, pool, mtype, prompt, date):
    """Регистрация в media_catalog (best-effort, не валит генерацию)."""
    if media_register is None:
        return
    try:
        media_register.register(path, pool, mtype, "gen", prompt, date)
        print(f"  [register] {path} → media_catalog")
    except Exception as e:
        print(f"  [register] пропуск: {e}")


SCRIPT_DIR = Path(__file__).parent
GEN_PY = SCRIPT_DIR / "veofree_gen.py"

N_VID = int(os.environ.get("N_VID", "2"))

# ── Вайб yaromat: тёмное/фотографичное/атмосферное, downtempo/Future Garage ──
# Без неона, без людей/лиц, без текста

VIDEO_PROMPTS = [
    "slow drifting fog through dark forest at dusk, ethereal, no people, film grain, muted tones",
    "rain slowly running down a window pane at night, bokeh city lights behind, melancholic mood, cinematic",
    "water surface with slow ripples, dark reflection, deep blue hour light, no text, photorealistic",
    "light rays through thick fog over still water, volumetric, desaturated, no people, slow drift",
    "mist rolling over abandoned industrial landscape at dawn, muted earth tones, vast empty space, cinematic",
    "raindrops on dark glass with blurred street lights behind, shallow depth of field, downtempo atmosphere",
]


def load_env():
    """Читаем .env если переменные не в окружении."""
    env_path = Path("/home/yaro/content_factory/.env")
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k not in os.environ:
            os.environ[k] = v


def tg(text):
    """Отправка TG-уведомления через Cloudflare Worker."""
    worker = os.environ.get("CLOUDFLARE_WORKER")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TG_CHAT_ID") or os.environ.get("FACTORY_CHAT_ID")
    thread = os.environ.get("TG_THREAD_ID") or os.environ.get("FACTORY_THREAD_ID", "5")
    if not (worker and token and chat):
        print("[tg] секреты не заданы — пропуск")
        return
    try:
        body = {"chat_id": chat, "text": text[:3800]}
        if thread:
            body["message_thread_id"] = int(thread)
        r = requests.post(f"{worker}/bot{token}/sendMessage", json=body, timeout=30)
        if r.status_code == 200:
            print(f"[tg] отправлено")
        else:
            print(f"[tg] ошибка HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[tg] ошибка: {e}")


def run_veofree(prompt, dest_folder, out_name, dry=False):
    """Запуск боевого генератора veofree_gen.py через subprocess."""
    gen_env = os.environ.copy()
    gen_env["PROMPT"] = prompt
    gen_env["DEST_FOLDER"] = dest_folder
    gen_env["OUT_NAME"] = out_name
    cmd = [sys.executable, str(GEN_PY)]

    if dry:
        print(f"  [dry] cmd: {' '.join(cmd)}")
        print(f"  [dry] PROMPT={prompt}")
        print(f"  [dry] DEST_FOLDER={dest_folder}")
        print(f"  [dry] OUT_NAME={out_name}")
        print(f"  [dry] YADISK_LOGIN={gen_env.get('YADISK_LOGIN', '?')}")
        print(f"  [dry] YADISK_PASSWORD={'***' if gen_env.get('YADISK_PASSWORD') else '?'}")
        return True

    print(f"\n{'='*60}")
    print(f"[veofree] промпт: {prompt[:80]}")
    print(f"[veofree] cmd: {' '.join(cmd)}")
    print(f"{'='*60}")
    try:
        r = subprocess.run(cmd, env=gen_env, timeout=900)
        if r.returncode == 0:
            print(f"[veofree] OK → {out_name}")
            return True
        else:
            print(f"[veofree] FAIL (rc={r.returncode})")
            return False
    except subprocess.TimeoutExpired:
        print(f"[veofree] TIMEOUT 900с → {out_name}")
        return False
    except Exception as e:
        print(f"[veofree] ERROR: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="VeoFree daily — ежедневная генерация видео")
    parser.add_argument("--dry", action="store_true", help="Печать плана без вызова сети")
    args = parser.parse_args()

    load_env()

    today = date.today().isoformat()
    dest_base = f"Content factory/veofree_pool/{today}"

    random.shuffle(VIDEO_PROMPTS)
    prompts = VIDEO_PROMPTS[:N_VID]

    print(f"План на {today}: {N_VID} видео")
    for i, p in enumerate(prompts, 1):
        print(f"  {i}. {p[:80]}")

    if args.dry:
        print("\n=== DRY RUN — сетевые вызовы не выполняются ===\n")
        # в dry НЕ шлём TG (чтобы не было ложного «старт» в тред при проверке плана)
        for i, prompt in enumerate(prompts, 1):
            out_name = f"vid_{i:02d}.mp4"
            run_veofree(prompt, dest_base, out_name, dry=True)
        print("\n=== DRY RUN завершён ===")
        return

    tg(f"🟢 VeoFree daily: {N_VID} видео\nпул: {dest_base}")

    t0 = time.time()
    ok = 0
    fail = 0

    for i, prompt in enumerate(prompts, 1):
        out_name = f"vid_{i:02d}.mp4"
        if run_veofree(prompt, dest_base, out_name):
            ok += 1
            _register(f"{dest_base}/{out_name}", "veofree_pool", "video", prompt, today)
        else:
            fail += 1

    dt = int(time.time() - t0)
    status = "✅" if fail == 0 else "⚠️"
    msg = (
        f"{status} VeoFree daily готов ({dt}с)\n"
        f"+{ok} видео"
        + (f" · ошибок: {fail}" if fail else "")
        + f"\nпул: {dest_base}"
    )
    tg(msg)
    print(f"\n{'='*60}")
    print(f"ИТОГО: +{ok} vid · ошибок {fail} · {dt}с")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
