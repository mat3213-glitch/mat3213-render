#!/usr/bin/env python3
"""
beat_footage_pilot.py — пилот нового видеопродакшена «взрослый» (2026-07-05, разворот на РЕАЛЬНЫЙ
футаж вместо AI-стиллов+параллакс, который дал пластик/кашу).

Для КАЖДОГО бита раскадровки: YouTube-поиск по релевантности (НЕ по просмотрам — view-сорт тянул
мусор-Sonic; лицензия любая по брифу, атмосфера/текстура важнее строгого CC для не-YT-площадок) →
скачивание КОРОТКОГО сегмента top-K кандидатов (не целиком — дёшево) → 3 кадра/кандидат → контактный
лист на бит. Отбор ГЛАЗАМИ (владелец/Claude) по контакту, потом финальный кроп/грейд отобранного.

Вход (ЯД render_jobs/<JOB_ID>/): pilot_beats.json = [{"id","query","k"}].
Выход: pilot_footage/<beat>/<vid>.mp4 (короткие сегменты) + preview/<JOB_ID>/pilot_<beat>.jpg (контакт).
Env: JOB_ID, YT_API_KEY, COOKIES (путь). Боевой на GH US-IP (yt-dlp-стек как fetch_cc).
"""
import json, os, subprocess, sys, tempfile
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

JOB_ID = os.environ["JOB_ID"]
YT_KEY = os.environ["YT_API_KEY"]
COOKIES = os.environ.get("COOKIES") or None
REMOTE = "ydrive"
CF = "Content factory"
JOB_YD = f"{CF}/cloud_io/render_jobs/{JOB_ID}"
PREV_YD = f"{CF}/cloud_io/preview/{JOB_ID}"
WORK = Path(tempfile.mkdtemp(prefix="beat_pilot_"))
SECTIONS = ["*30-40", "*10-20", "*0-10"]   # окна сегмента (skip интро; фолбэк если видео короткое)


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def yd_put(local, remote):
    return sh(["rclone", "copyto", str(local), f"{REMOTE}:{remote}"]).returncode == 0


def yd_get(remote, local):
    return sh(["rclone", "copyto", f"{REMOTE}:{remote}", str(local)]).returncode == 0


def yt_search(query, k):
    """YouTube Data API: релевантность, средняя длина (не часовые), только видео."""
    r = requests.get("https://www.googleapis.com/youtube/v3/search", params={
        "part": "snippet", "q": query, "type": "video", "order": "relevance",
        "videoDuration": "medium", "maxResults": k, "key": YT_KEY,
    }, timeout=30)
    if r.status_code != 200:
        print(f"  YT search {r.status_code}: {r.text[:200]}", flush=True)
        return []
    out = []
    for it in r.json().get("items", []):
        vid = it["id"].get("videoId")
        if vid:
            out.append({"vid": vid, "title": it["snippet"]["title"][:70],
                        "channel": it["snippet"]["channelTitle"][:40]})
    return out


def dl_segment(vid, dst: Path):
    """yt-dlp: короткий сегмент ≤720p mp4. Перебор окон сегмента (короткое видео → фолбэк)."""
    url = f"https://www.youtube.com/watch?v={vid}"
    for sec in SECTIONS:
        cmd = ["yt-dlp", "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]",
               "--merge-output-format", "mp4", "--no-playlist",
               "--download-sections", sec, "--force-keyframes-at-cuts",
               "-o", str(dst.with_suffix(".%(ext)s"))]
        if COOKIES:
            cmd += ["--cookies", COOKIES]
        cmd.append(url)
        sh(cmd)
        if dst.exists() and dst.stat().st_size > 20000:
            return True
        for f in WORK.glob(f"{dst.stem}.*"):
            if f.suffix.lower() in (".mkv", ".webm") and f.stat().st_size > 20000:
                sh(["ffmpeg", "-y", "-loglevel", "error", "-i", str(f), "-c", "copy", str(dst)])
                if dst.exists():
                    return True
    return False


def frames_of(clip: Path, n=3):
    """n кадров равномерно из клипа (thumbnails для контакта)."""
    import re
    dur = 8.0
    r = sh(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(clip)])
    try:
        dur = max(1.0, float(r.stdout.strip()))
    except ValueError:
        pass
    out = []
    for i in range(n):
        t = dur * (i + 0.5) / n
        fp = clip.with_name(f"{clip.stem}_f{i}.png")
        sh(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(clip),
            "-frames:v", "1", "-vf", "scale=360:-1", str(fp)])
        if fp.exists():
            out.append(fp)
    return out


def contact_sheet(beat_id, query, rows, dst: Path):
    """Контактный лист бита: строка = кандидат (3 кадра + подпись)."""
    TW, TH, PAD, LBL = 360, 202, 6, 26
    if not rows:
        img = Image.new("RGB", (800, 120), (20, 20, 20))
        ImageDraw.Draw(img).text((10, 10), f"{beat_id}: НЕТ кандидатов\n{query}", fill=(230, 120, 120))
        img.save(dst, quality=90); return
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 15)
    except Exception:
        font = ImageFont.load_default()
    row_h = TH + LBL + PAD
    W = 3 * TW + 4 * PAD
    H = PAD + len(rows) * row_h + LBL
    sheet = Image.new("RGB", (W, H), (15, 15, 15))
    d = ImageDraw.Draw(sheet)
    d.text((PAD, 2), f"{beat_id}  «{query}»", fill=(240, 220, 160), font=font)
    y = LBL
    for row in rows:
        d.text((PAD, y), f"{row['vid']}  {row['title']}  · {row['channel']}", fill=(200, 200, 210), font=font)
        yy = y + LBL - 4
        for j, fp in enumerate(row["frames"][:3]):
            try:
                th = Image.open(fp).convert("RGB").resize((TW, TH))
                sheet.paste(th, (PAD + j * (TW + PAD), yy))
            except Exception:
                pass
        y += row_h
    sheet.save(dst, quality=90)


def main():
    bf = WORK / "pilot_beats.json"
    if not yd_get(f"{JOB_YD}/pilot_beats.json", bf):
        sys.exit("нет pilot_beats.json на ЯД")
    beats = json.loads(bf.read_text(encoding="utf-8"))
    index = []
    for beat in beats:
        bid, query, k = beat["id"], beat["query"], int(beat.get("k", 4))
        print(f"\n══ бит {bid}: «{query}» (k={k})", flush=True)
        cands = yt_search(query, k)
        print(f"  найдено {len(cands)} кандидатов", flush=True)
        rows = []
        for c in cands:
            clip = WORK / f"{bid}_{c['vid']}.mp4"
            if not dl_segment(c["vid"], clip):
                print(f"  ✗ {c['vid']} сегмент не скачался", flush=True); continue
            yd_put(clip, f"{JOB_YD}/pilot_footage/{bid}/{c['vid']}.mp4")
            frames = frames_of(clip)
            rows.append({**c, "frames": frames})
            print(f"  ✓ {c['vid']} ({clip.stat().st_size//1024}KB, {len(frames)} кадра)", flush=True)
        sheet = WORK / f"pilot_{bid}.jpg"
        contact_sheet(bid, query, rows, sheet)
        yd_put(sheet, f"{PREV_YD}/pilot_{bid}.jpg")
        print(f"  контакт → preview/{JOB_ID}/pilot_{bid}.jpg", flush=True)
        index.append({"beat": bid, "query": query, "candidates": [r["vid"] for r in rows]})
    idx = WORK / "pilot_index.json"
    idx.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    yd_put(idx, f"{JOB_YD}/pilot_index.json")
    print(f"\n✅ пилот готов: {len(index)} бит, контакты на превью-канале", flush=True)


if __name__ == "__main__":
    main()
