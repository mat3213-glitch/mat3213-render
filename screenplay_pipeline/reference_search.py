#!/usr/bin/env python3
"""
reference_search.py — поиск референс-клипов на YouTube по вайбу+БПМ.

Детерминированный текстовый запрос (без LLM) из параметров трека:
  <bpm>bpm <genre> <mood_word> <visual_mood>
→ YouTube Data API v3 search.list → videos.list для duration/viewCount → фильтрация → top N.

Usage:
  python3 reference_search.py --brief path/to/brief_full.yaml --job-id JOB_ID [--top 5]

Requires: YT_API_KEY env var. Result saved to WORK/references.json and uploaded to Yandex.Disk.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import requests
import yaml

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env", override=False)
except Exception:
    pass

YT_API_KEY = os.environ.get("YT_API_KEY", "")
YD_ROOT = "ydrive:Content factory"

ISO8601_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def iso8601_to_seconds(duration: str) -> int:
    m = ISO8601_RE.match(duration)
    if not m:
        return 0
    h = int(m.group(1) or 0)
    mi = int(m.group(2) or 0)
    s = int(m.group(3) or 0)
    return h * 3600 + mi * 60 + s


def build_query(brief: dict) -> str:
    # ВАЖНО: буквальный токен "<N>bpm" в запросе почти всегда даёт 0 результатов —
    # проверено вживую (87bpm+future+garage+melancholic+night+rain → totalResults=0,
    # тот же запрос без "87bpm" → 3508). Никто не пишет BPM в заголовках видео этим
    # словом+числом слитно так, как строился запрос — YouTube ищет буквальное совпадение
    # фразы, а не парсит BPM семантически. BPM влияет на выбор genre/mood слов заранее
    # (вызывающий код может подобрать mood_words под темп), но НЕ идёт в текст запроса.
    # ВАЖНО #2: кириллица в запросе тоже убивает результаты — проверено вживую (mixed
    # RU/EN запрос "future garage nostalgic дождь на стекле, ночной город" → 0 результатов,
    # тот же запрос БЕЗ кириллицы "future garage nostalgic" → 324k). Мини-бриф владелец пишет
    # по-русски (visual_mood/narrative_angle обычно кириллица) — отбрасываем кириллицу из
    # запроса ДЕТЕРМИНИРОВАННО (без LLM-перевода, это не одна из 3 разрешённых LLM-точек).
    c = brief.get("content", {})
    p = brief.get("production", {})
    genre = p.get("genre", "")
    mood_words = c.get("mood_words", [])
    mood0 = mood_words[0] if mood_words else ""
    visual_mood = c.get("visual_mood", "")
    parts = [genre, mood0, visual_mood]
    raw = " ".join(p for p in parts if p)
    no_cyrillic = re.sub(r"[а-яёА-ЯЁ]+", "", raw)
    query = re.sub(r"[\s,]+", " ", no_cyrillic).strip(" ,")
    if not query:
        # весь бриф оказался кириллицей (genre/mood_words тоже могут быть по-русски) —
        # без хоть какого-то английского токена запрос пуст. Фолбэк на голый жанр латиницей
        # если такой найдётся в mood_words, иначе — общий "aesthetic ambient visual".
        query = "aesthetic ambient visual"
    return query


def yt_search(query: str, max_results: int = 15, cc_only: bool = False) -> list[dict]:
    url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "part": "snippet",
        "type": "video",
        "order": "relevance",
        "maxResults": max_results,
        "q": query,
        "key": YT_API_KEY,
    }
    if cc_only:
        # только Creative Commons (CC-BY 3.0) — легально скачивать и использовать в рендере
        params["videoLicense"] = "creativeCommon"
    r = requests.get(url, params=params, timeout=30)
    if r.status_code != 200:
        print(f"[yt] search.list HTTP {r.status_code}: {r.text[:300]}", file=sys.stderr)
        sys.exit(1)
    data = r.json()
    items = data.get("items", [])
    return [
        {
            "video_id": it["id"]["videoId"],
            "title": it["snippet"]["title"],
            "channel": it["snippet"]["channelTitle"],
        }
        for it in items
        if it.get("id", {}).get("videoId")
    ]


def yt_video_details(video_ids: list[str]) -> dict[str, dict]:
    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {
        "part": "contentDetails,statistics",
        "id": ",".join(video_ids),
        "key": YT_API_KEY,
    }
    r = requests.get(url, params=params, timeout=30)
    if r.status_code != 200:
        print(f"[yt] videos.list HTTP {r.status_code}: {r.text[:300]}", file=sys.stderr)
        sys.exit(1)
    result = {}
    for it in r.json().get("items", []):
        vid = it["id"]
        dur_str = it.get("contentDetails", {}).get("duration", "PT0S")
        result[vid] = {
            "duration_sec": iso8601_to_seconds(dur_str),
            "view_count": int(it.get("statistics", {}).get("viewCount", 0)),
        }
    return result


def filter_and_sort(candidates: list[dict], details: dict[str, dict], top: int) -> list[dict]:
    filtered = []
    for c in candidates:
        d = details.get(c["video_id"], {})
        dur = d.get("duration_sec", 0)
        if 60 <= dur <= 600:
            c["duration_sec"] = dur
            c["view_count"] = d.get("view_count", 0)
            c["url"] = f"https://youtube.com/watch?v={c['video_id']}"
            filtered.append(c)
    filtered.sort(key=lambda x: -x["view_count"])
    return filtered[:top]


def upload_yd(path: str, job_id: str):
    dst = f"{YD_ROOT}/cloud_io/render_jobs/{job_id}/references.json"
    r = subprocess.run(
        ["rclone", "copyto", path, dst],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"[rclone] copyto failed: {r.stderr[:300]}", file=sys.stderr)
        sys.exit(1)
    print(f"[rclone] uploaded → {dst}")


def main():
    ap = argparse.ArgumentParser(description="Поиск референс-клипов на YouTube по параметрам трека.")
    ap.add_argument("--brief", required=False, default=None, help="путь к brief_full.yaml (не нужен при --queries)")
    ap.add_argument("--queries", type=str, default=None, help="явные запросы через ; (Reference Heist — минует build_query/бриф)")
    ap.add_argument("--cc-only", action="store_true", help="только Creative Commons видео (для скачивания футажа в рендер)")
    ap.add_argument("--job-id", required=True, help="ID задачи (для пути на Яндекс.Диск)")
    ap.add_argument("--top", type=int, default=5, help="сколько результатов вернуть (default: 5)")
    args = ap.parse_args()

    if not YT_API_KEY:
        print("[error] YT_API_KEY не задан в переменных окружения", file=sys.stderr)
        sys.exit(1)

    if args.queries:
        # Reference Heist: явные прицельные запросы под мир трека, минуя build_query.
        queries = [q.strip() for q in args.queries.split(";") if q.strip()]
        if not queries:
            print("[yt] --queries задан, но пуст после разбора", file=sys.stderr)
            sys.exit(1)
        candidates = []
        seen = set()
        for q in queries:
            batch = yt_search(q, max_results=6, cc_only=args.cc_only)
            new = [c for c in batch if c["video_id"] not in seen]
            seen.update(c["video_id"] for c in new)
            candidates.extend(new)
            print(f"[yt] запрос «{q}»: {len(batch)} найдено, {len(new)} новых")
    else:
        if not args.brief:
            print("[yt] нужен --queries или --brief", file=sys.stderr)
            sys.exit(1)
        brief = yaml.safe_load(Path(args.brief).read_text(encoding="utf-8"))
        query = build_query(brief)
        print(f"[yt] query: {query}")
        candidates = yt_search(query, cc_only=args.cc_only)

    if not candidates:
        print("[yt] ничего не найдено", file=sys.stderr)
        sys.exit(1)
    print(f"[yt] кандидатов: {len(candidates)}")

    details = yt_video_details([c["video_id"] for c in candidates])
    results = filter_and_sort(candidates, details, args.top)
    if not results:
        print("[yt] нет видео, подходящих по длительности (60-600 сек)", file=sys.stderr)
        sys.exit(1)
    print(f"[yt] после фильтрации: {len(results)}")

    work = tempfile.mkdtemp(prefix="ref_search_")
    out_path = os.path.join(work, "references.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"[yt] сохранено → {out_path}")

    upload_yd(out_path, args.job_id)


if __name__ == "__main__":
    main()
