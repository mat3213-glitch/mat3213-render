#!/usr/bin/env python3
"""
reference_deconstruct.py — деконструктор референс-клипов (стадия 2 screenplay-pipeline).

Берёт references.json (из reference_search.py) → скачивает видео, извлекает 16 кадров,
собирает контакт-лист 4x4 → mimo-анализ структуры/хуков → reference_recipes.json на ЯД.

Usage:
  python3 reference_deconstruct.py --references path/to/references.json --job-id JOB_ID

ВАЖНО: yt-dlp качает youtube.com напрямую — с RU-IP (бук) стабильно ловит read-timeout на
API-запросах (проверено вживую 2026-07-03). Как shorts_harvest — запускать на GH Actions
(US-раннер), не на буке. Остальная машинерия (кадры/контакт-лист/mimo) от IP не зависит,
проверена локально на не-YouTube видео.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env", override=False)
except Exception:
    pass

YD_ROOT = "ydrive:Content factory"
MIMO = os.path.expanduser("~/.mimocode/bin/mimo")

RUBRIC = (
    "Перед тобой контакт-лист из 16 кадров видео-референса (сетка 4x4, кадры идут по таймлайну слева-направо, сверху-вниз).\n"
    "Проанализируй структуру и приёмы этого клипа. Верни СТРОГО один JSON:\n"
    '{"hook": "как открывается видео, что цепляет в первые секунды",\n'
    ' "rhythm": "ритм монтажа: медленный/строб/волнообразный",\n'
    ' "motion": "тип движения камеры/объектов",\n'
    ' "color": "цветовая палитра/грейд",\n'
    ' "composition": "композиция кадра",\n'
    ' "scenes": ["разбей клип на 3-6 ключевых СЦЕН по контакт-листу; для каждой строкой: что в кадре + приём, который стоит скопировать своим материалом"],\n'
    ' "why_works": "почему это может работать (1 фраза)",\n'
    ' "verdict": "взять" | "частично" | "мимо"}\n'
    "Только JSON, без пояснений."
)


def strip_ansi(t):
    return re.sub(r'\x1B\[[0-9;]*[A-Za-z]', '', t)


def extract_json(t):
    t = strip_ansi(t)
    depth = 0
    start = -1
    for i, ch in enumerate(t):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    return json.loads(t[start:i + 1])
                except Exception:
                    start = -1
    return None


def download_video(video: dict, tmpdir: str, cookies: str | None = None) -> str | None:
    url = video["url"]
    vid = video["video_id"]
    # ВНИМАНИЕ: "--skip-download-if-exists" НЕ СУЩЕСТВУЕТ в yt-dlp (проверено вживую: exit 2,
    # "no such option") — предыдущая версия падала мгновенно (<0.2с) на этом флаге на КАЖДОМ
    # референсе, маскируясь под "видео не скачалось" из-за молчаливого проглатывания stderr.
    # Убран. -w/--no-overwrites — реальный аналог (не перезаписывать), не нужен на tmpdir.
    # Формат/cookies — рецепт [[reference_yt_dlp_ci]]: единый прогрессивный файл (без ffmpeg-мержа
    # видео+аудио отдельно, для анализа кадров хватает), cookies обходят бот-детект US-датацентра.
    cmd = [
        "yt-dlp", "--no-warnings",
        "-f", "b[ext=mp4]/bv*[ext=mp4]+ba[ext=m4a]/b",
        "--write-comments", "--write-info-json",
        "-o", os.path.join(tmpdir, f"{vid}.%(ext)s"),
    ]
    if cookies:
        cmd += ["--cookies", cookies]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    for f in os.listdir(tmpdir):
        if f.startswith(vid) and f.endswith((".mp4", ".webm", ".mkv", ".mov")):
            return os.path.join(tmpdir, f)
    print(f"  yt-dlp rc={r.returncode}: {(r.stderr or r.stdout)[-300:]}", file=sys.stderr)
    return None


def get_comments(video_id: str, tmpdir: str) -> list[str]:
    for f in os.listdir(tmpdir):
        if f.startswith(video_id) and f.endswith(".info.json"):
            try:
                data = json.loads(Path(os.path.join(tmpdir, f)).read_text(encoding="utf-8"))
                comments = data.get("comments", [])
                return [c.get("text", "") for c in comments[:10] if c.get("text")]
            except Exception:
                pass
    return []


def get_duration(video_path: str) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", video_path],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except Exception:
        return 6.0


def extract_frames(video_path: str, tmpdir: str, n: int = 16) -> list[str]:
    dur = get_duration(video_path)
    base = os.path.splitext(os.path.basename(video_path))[0]
    frames = []
    for i in range(n):
        pct = (i + 0.5) / n
        out = os.path.join(tmpdir, f"{base}_f{i:02d}.jpg")
        subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-ss", f"{dur * pct:.2f}",
             "-i", video_path, "-frames:v", "1", "-q:v", "3",
             "-vf", "scale=320:-1", out],
            capture_output=True, timeout=30,
        )
        if os.path.exists(out):
            frames.append(out)
    return frames


def make_contact_sheet(frames: list[str], tmpdir: str, name: str) -> str | None:
    imgs = [Image.open(f).convert("RGB") for f in frames if os.path.exists(f)]
    if len(imgs) < 4:
        return None
    cols, rows = 4, 4
    imgs = imgs[:cols * rows]
    w = imgs[0].width
    h = imgs[0].height
    sheet = Image.new("RGB", (w * cols, h * rows))
    for idx, im in enumerate(imgs):
        x = (idx % cols) * w
        y = (idx // cols) * h
        sheet.paste(im, (x, y))
    out = os.path.join(tmpdir, f"{name}_contact.jpg")
    sheet.save(out, quality=88)
    return out


def judge_video(contact_sheet: str, timeout: int = 180) -> dict | None:
    try:
        r = subprocess.run(
            [MIMO, "run", "--pure", "--dangerously-skip-permissions",
             RUBRIC, "-f", contact_sheet],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return None
    raw = re.sub(r'[一-鿿]', '', r.stdout or "")
    d = extract_json(raw)
    if not d:
        return None
    required = ["hook", "rhythm", "motion", "color", "composition", "why_works", "verdict"]
    if not all(k in d for k in required):
        return None
    return {k: str(d[k]) for k in required}


def upload_yd(path: str, job_id: str):
    dst = f"{YD_ROOT}/cloud_io/render_jobs/{job_id}/reference_recipes.json"
    r = subprocess.run(
        ["rclone", "copyto", path, dst],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"[rclone] copyto failed: {r.stderr[:300]}", file=sys.stderr)
        sys.exit(1)
    print(f"[rclone] uploaded → {dst}")


def main():
    ap = argparse.ArgumentParser(description="Деконструктор референс-клипов: структура, хуки, визуал.")
    ap.add_argument("--references", required=True, help="путь к references.json")
    ap.add_argument("--job-id", required=True, help="ID задачи (для пути на Яндекс.Диск)")
    ap.add_argument("--cookies", default=None, help="путь к YouTube cookies.txt (обход бот-детекта US-датацентра)")
    args = ap.parse_args()

    references = json.loads(Path(args.references).read_text(encoding="utf-8"))
    if not references:
        print("[error] references.json пуст", file=sys.stderr)
        sys.exit(1)

    tmpdir = tempfile.mkdtemp(prefix="ref_decon_")
    recipes = []
    errors = 0

    for i, ref in enumerate(references, 1):
        vid = ref["video_id"]
        print(f"[{i}/{len(references)}] {ref.get('title', vid)}")
        try:
            vpath = download_video(ref, tmpdir, cookies=args.cookies)
            if not vpath:
                print(f"  skip: видео не скачалось", file=sys.stderr)
                errors += 1
                continue

            comments = get_comments(vid, tmpdir)
            frames = extract_frames(vpath, tmpdir)
            sheet = make_contact_sheet(frames, tmpdir, vid)
            if not sheet:
                print(f"  skip: не удалось собрать контакт-лист", file=sys.stderr)
                errors += 1
                continue

            analysis = judge_video(sheet)
            if not analysis:
                print(f"  skip: mimo не вернул JSON", file=sys.stderr)
                errors += 1
                continue

            recipe = {
                "video_id": vid,
                "title": ref.get("title", ""),
                "url": ref.get("url", ""),
                "view_count": ref.get("view_count", 0),
                **analysis,
                "comments": comments,
            }
            recipes.append(recipe)
            print(f"  verdict={analysis['verdict']}")
        except Exception as e:
            print(f"  error: {e}", file=sys.stderr)
            errors += 1
            continue

    if not recipes:
        print(f"[error] все {len(references)} референсов упали ({errors} ошибок)", file=sys.stderr)
        sys.exit(1)

    out = os.path.join(tmpdir, "reference_recipes.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(recipes, f, ensure_ascii=False, indent=2)
    print(f"[ok] рецептов: {len(recipes)}/{len(references)}, ошибок: {errors}")
    print(f"[ok] сохранено → {out}")

    upload_yd(out, args.job_id)


if __name__ == "__main__":
    main()
