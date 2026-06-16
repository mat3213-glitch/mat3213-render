#!/usr/bin/env python3
"""
qwen_daily.py — ежедневный авто-генератор картинок и видео через Qwen.

Генерирует N_IMG картинок + N_VID видео по вайб-промптам,
заливает на ЯД в Content factory/qwen_pool/<YYYY-MM-DD>/,
шлёт TG-уведомления старт/финиш.

Запуск:
  python3 qwen_daily.py                          # дефолт: 4 картинки, 2 видео
  N_IMG=1 N_VID=0 python3 qwen_daily.py          # тест: 1 картинка
  N_IMG=6 N_VID=1 RATIO=9:16 python3 qwen_daily.py

# === crontab (ежедневно 04:00, nice -n 15) ===
# 0 4 * * * nice -n 15 /usr/bin/python3 /home/yaro/content_factory/Instrument/Qwen/qwen_daily.py >> /home/yaro/content_factory/logs/qwen_daily.log 2>&1
"""
import os
import sys
import json
import time
import random
import subprocess
from pathlib import Path
from datetime import date
import requests

SCRIPT_DIR = Path(__file__).parent
GEN_PY = SCRIPT_DIR / "GEN.py"
WORK_DIR = Path("/tmp/qwen_daily_work")
WORK_DIR.mkdir(parents=True, exist_ok=True)

N_IMG = int(os.environ.get("N_IMG", "4"))
N_VID = int(os.environ.get("N_VID", "2"))
RATIO = os.environ.get("RATIO", "16:9")

# ── Вайб yaromat: тёмное/фотографичное/внутренняя глубина, БЕЗ неона, без лиц/масок ──

IMAGE_PROMPTS = [
    "foggy dark forest at twilight, moody atmosphere, muted tones",
    "empty room with dim window light, dust particles floating, cinematic",
    "old clock close up moody, shallow depth of field, dark warm tones",
    "dust particles in light beam dark room, volumetric light, photorealistic",
    "rain on window at night, bokeh city lights behind, melancholic mood",
    "candle flame in darkness, warm glow on old wood, intimate atmosphere",
    "misty field at dawn, muted earth tones, vast empty space",
    "abandoned interior with deep shadows, peeling paint, soft natural light",
    "dark moody clouds over still water, reflection, desaturated",
    "cracked earth texture close up, dark tones, abstract natural pattern",
    "fog rolling through bare trees, silhouette, monochrome feel",
    "old book pages in dim light, dust, shallow focus, warm shadows",
]

VIDEO_PROMPTS = [
    "slow drifting fog through dark forest, ethereal, no people",
    "curtain gently moving in wind in a dim empty room, soft light",
    "rain slowly running down a window pane at night, bokeh background",
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


def yd_put(local: Path, remote: str) -> bool:
    r = subprocess.run(
        ["rclone", "copyto", str(local), f"ydrive:{remote}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"  [yd] ошибка: {r.stderr[:200]}")
    return r.returncode == 0


def run_gen(mode: str, prompt: str, out_path: Path) -> bool:
    """Запуск GEN.py. Возвращает True если файл создан."""
    cmd = [
        sys.executable, str(GEN_PY), mode, prompt,
        "--ratio", RATIO, "--out", str(out_path),
    ]
    print(f"\n{'='*60}")
    print(f"[gen] {mode}: {prompt[:70]}")
    print(f"[gen] cmd: {' '.join(cmd)}")
    print(f"{'='*60}")
    try:
        r = subprocess.run(cmd, timeout=700, capture_output=False)
        if r.returncode == 0 and out_path.exists() and out_path.stat().st_size > 1024:
            print(f"[gen] OK → {out_path} ({out_path.stat().st_size // 1024} KB)")
            return True
        else:
            print(f"[gen] FAIL (rc={r.returncode}, exists={out_path.exists()})")
            return False
    except subprocess.TimeoutExpired:
        print(f"[gen] TIMEOUT 700с")
        return False
    except Exception as e:
        print(f"[gen] ERROR: {e}")
        return False


def main():
    load_env()

    today = date.today().isoformat()
    yd_base = f"Content factory/qwen_pool/{today}"
    work = WORK_DIR / today
    work.mkdir(parents=True, exist_ok=True)

    random.shuffle(IMAGE_PROMPTS)
    random.shuffle(VIDEO_PROMPTS)

    img_prompts = IMAGE_PROMPTS[:N_IMG]
    vid_prompts = VIDEO_PROMPTS[:N_VID]

    tg(f"🟢 Qwen daily начался\n{N_IMG} картинок + {N_VID} видео · ratio {RATIO}\nпул: {yd_base}")

    t0 = time.time()
    ok_img = ok_vid = fail = 0

    # ── Картинки ──
    for i, prompt in enumerate(img_prompts, 1):
        out = work / f"img_{i:02d}.png"
        if run_gen("image", prompt, out):
            remote = f"{yd_base}/img_{i:02d}.png"
            if yd_put(out, remote):
                ok_img += 1
                print(f"  ✅ img_{i:02d} → ЯД")
            else:
                fail += 1
            out.unlink(missing_ok=True)
        else:
            fail += 1
        if i < len(img_prompts):
            time.sleep(5)

    # ── Видео ──
    for i, prompt in enumerate(vid_prompts, 1):
        out = work / f"vid_{i:02d}.mp4"
        if run_gen("video", prompt, out):
            remote = f"{yd_base}/vid_{i:02d}.mp4"
            if yd_put(out, remote):
                ok_vid += 1
                print(f"  ✅ vid_{i:02d} → ЯД")
            else:
                fail += 1
            out.unlink(missing_ok=True)
        else:
            fail += 1
        if i < len(vid_prompts):
            time.sleep(5)

    dt = int(time.time() - t0)
    status = "✅" if fail == 0 else "⚠️"
    msg = (
        f"{status} Qwen daily готов ({dt}с)\n"
        f"+{ok_img} картинок +{ok_vid} видео"
        + (f" · ошибок: {fail}" if fail else "")
        + f"\nпул: {yd_base}"
    )
    tg(msg)
    print(f"\n{'='*60}")
    print(f"ИТОГО: +{ok_img} img +{ok_vid} vid · ошибок {fail} · {dt}с")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
