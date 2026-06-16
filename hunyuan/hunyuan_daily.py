#!/usr/bin/env python3
"""
hunyuan_daily.py — ежедневная i2v-генерация (оживление фото → видео).

Берёт ГОТОВЫЕ фото из пулов на ЯД и «оживляет» их через hunyuan_video.py.
Шлёт TG-уведомления старт/финиш.

Запуск:
  python3 hunyuan_daily.py              # дефолт: 2 видео
  python3 hunyuan_daily.py --dry        # печать плана без вызова сети
  N_VID=4 RATIO=16:9 python3 hunyuan_daily.py
"""
import os
import sys
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
HUNYUAN_VIDEO_PY = SCRIPT_DIR / "hunyuan_video.py"

N_VID = int(os.environ.get("N_VID", "2"))
RATIO = os.environ.get("RATIO", "9:16")

# Пулы фото на ЯД
PHOTO_POOLS = [
    "ydrive:Content factory/qwen_pool/",
    "ydrive:Content factory/pexels_pool/",
    "ydrive:Content factory/openverse_pool/",
]

# Вайб движения: мягкий, без смены сцены
MOTION_PROMPTS = [
    "slow gentle camera drift, subtle motion, no scene change",
    "soft parallax, slow zoom in, cinematic",
    "gentle wind motion, drifting, atmospheric",
    "very subtle movement, cinematic breathing, no scene change",
    "slow dolly forward, gentle motion, dreamy atmosphere",
    "soft pan left, parallax effect, atmospheric depth",
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
            print("[tg] отправлено")
        else:
            print(f"[tg] ошибка HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[tg] ошибка: {e}")


def collect_photos(dry=False):
    """Собрать список фото-кандидатов с ЯД (рекурсивно, .png/.jpg/.jpeg)."""
    all_photos = []
    exts = ["*.png", "*.jpg", "*.jpeg"]
    for pool in PHOTO_POOLS:
        for ext in exts:
            try:
                cmd = ["rclone", "lsf", "-R", "--files-only", "--include", ext, pool]
                if dry:
                    print(f"  [dry] {' '.join(cmd)}")
                    continue
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if r.returncode != 0:
                    continue
                for line in r.stdout.strip().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    # Формируем полный relative-путь относительно pool
                    full = pool.replace("ydrive:", "") + line
                    all_photos.append(full)
            except (subprocess.TimeoutExpired, Exception) as e:
                print(f"  [rclone] ошибка {pool}{ext}: {e}")
    return all_photos


def yd_download(remote, local_path):
    """Скачать файл с ЯД."""
    try:
        r = subprocess.run(
            ["rclone", "copyto", f"ydrive:{remote}", str(local_path)],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            print(f"  [yd] ошибка скачивания: {r.stderr[:200]}")
            return False
        return local_path.exists() and local_path.stat().st_size > 1024
    except Exception as e:
        print(f"  [yd] исключение: {e}")
        return False


def yd_upload(local_path, remote):
    """Залить файл на ЯД."""
    try:
        r = subprocess.run(
            ["rclone", "copyto", str(local_path), f"ydrive:{remote}"],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            print(f"  [yd] ошибка заливки: {r.stderr[:200]}")
            return False
        return True
    except Exception as e:
        print(f"  [yd] исключение: {e}")
        return False


def run_hunyuan(prompt, image_path, ratio, out_path, timeout_sec=1800):
    """Запуск hunyuan_video.py (argv: prompt --image --ratio --out)."""
    cmd = [
        sys.executable, str(HUNYUAN_VIDEO_PY),
        prompt, "--image", str(image_path),
        "--ratio", ratio, "--out", str(out_path),
    ]
    print(f"\n{'='*60}")
    print(f"[hunyuan] промпт: {prompt[:80]}")
    print(f"[hunyuan] фото: {image_path}")
    print(f"[hunyuan] ratio: {ratio}")
    print(f"[hunyuan] cmd: {' '.join(cmd)}")
    print(f"{'='*60}")
    try:
        r = subprocess.run(cmd, timeout=timeout_sec)
        if r.returncode == 0 and out_path.exists() and out_path.stat().st_size > 1024:
            print(f"[hunyuan] OK → {out_path} ({out_path.stat().st_size // 1024} KB)")
            return True
        else:
            print(f"[hunyuan] FAIL (rc={r.returncode}, exists={out_path.exists()})")
            return False
    except subprocess.TimeoutExpired:
        print(f"[hunyuan] TIMEOUT {timeout_sec}с")
        return False
    except Exception as e:
        print(f"[hunyuan] ERROR: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Hunyuan i2v daily — оживление фото → видео")
    parser.add_argument("--dry", action="store_true", help="Печать плана без вызова сети")
    args = parser.parse_args()

    load_env()

    today = date.today().isoformat()
    yd_dest = f"Content factory/hunyuan_pool/{today}"
    work_dir = Path(f"/tmp/hunyuan_daily_{today}")
    work_dir.mkdir(parents=True, exist_ok=True)

    print(f"Сбор фото-кандидатов с ЯД...")
    photos = collect_photos(dry=args.dry)
    print(f"  найдено фото: {len(photos)}")

    if not photos and not args.dry:
        print("Нет фото для обработки — выход.")
        return

    random.shuffle(photos)
    selected = photos[:N_VID]
    random.shuffle(MOTION_PROMPTS)
    prompts = MOTION_PROMPTS[:N_VID]

    print(f"\nПлан на {today}: {len(selected)} видео из фото")
    for i, (photo, prompt) in enumerate(zip(selected, prompts), 1):
        print(f"  {i}. фото: {photo}")
        print(f"     промпт: {prompt}")

    if args.dry:
        print("\n=== DRY RUN — сетевые вызовы не выполняются ===\n")
        for i, (photo, prompt) in enumerate(zip(selected, prompts), 1):
            out_name = f"vid_{i:02d}.mp4"
            print(f"\n--- Видео {i}/{len(selected)} ---")
            print(f"  [dry] rclone copyto ydrive:{photo} /tmp/.../photo_{i:02d}.ext")
            print(f"  [dry] python3 hunyuan_video.py \"{prompt}\" --image photo_{i:02d}.ext --ratio {RATIO} --out {out_name}")
            print(f"  [dry] rclone copyto {out_name} ydrive:{yd_dest}/{out_name}")
            print(f"  [meta] out={out_name} src={photo} prompt={prompt}")
        print("\n=== DRY RUN завершён ===")
        return

    tg(f"🟢 Hunyuan i2v: {len(selected)} видео из фото\nпул: {yd_dest}")

    t0 = time.time()
    ok = 0
    fail = 0

    for i, (photo, prompt) in enumerate(zip(selected, prompts), 1):
        out_name = f"vid_{i:02d}.mp4"
        ext = Path(photo).suffix or ".jpg"
        local_photo = work_dir / f"photo_{i:02d}{ext}"
        local_out = work_dir / out_name

        print(f"\n--- Видео {i}/{len(selected)} ---")
        print(f"  скачиваю фото: {photo}")

        if not yd_download(photo, local_photo):
            print(f"  не удалось скачать фото — пропуск")
            fail += 1
            continue

        print(f"  запускаю i2v генерацию...")
        if run_hunyuan(prompt, local_photo, RATIO, local_out):
            remote = f"{yd_dest}/{out_name}"
            if yd_upload(local_out, remote):
                ok += 1
                print(f"  ✅ {out_name} → ЯД")
                _register(remote, "hunyuan_pool", "video", prompt, today)
            else:
                fail += 1
                print(f"  видео сгенерировано, но ошибка заливки на ЯД")
        else:
            fail += 1

        # Метаданные для классификатора
        print(f"[meta] out={out_name} src={photo} prompt={prompt}")

        # Чистим временные файлы
        local_photo.unlink(missing_ok=True)
        local_out.unlink(missing_ok=True)

    dt = int(time.time() - t0)
    status = "✅" if fail == 0 else "⚠️"
    msg = (
        f"{status} Hunyuan i2v готов ({dt}с)\n"
        f"+{ok} видео"
        + (f" · ошибок: {fail}" if fail else "")
        + f"\nпул: {yd_dest}"
    )
    tg(msg)
    print(f"\n{'='*60}")
    print(f"ИТОГО: +{ok} vid · ошибок {fail} · {dt}с")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
