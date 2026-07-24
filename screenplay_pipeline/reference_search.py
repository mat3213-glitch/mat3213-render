#!/usr/bin/env python3
"""
reference_search.py — поиск референс-клипов на YouTube по вайбу+БПМ.

Детерминированный текстовый запрос (без LLM) из параметров трека:
  <genre> <mood_word> <visual_mood>   (BPM НЕ идёт в текст запроса — см. build_query)
→ поиск видео → детали (duration/viewCount) → фильтрация 60-600с → сортировка по просмотрам → top N.

Два движка поиска (--engine):
  • ytdlp (по умолчанию) — yt-dlp `ytsearchN:query`, БЕЗ API-ключа и БЕЗ квоты Data API.
                            Запускать на US-раннере (с RU-IP yt-dlp по YouTube ловит таймауты).
  • api                  — YouTube Data API v3 (search.list=100 units/запрос). Нужен YT_API_KEY.
                            Обязателен для --cc-only (videoLicense=creativeCommon есть только в API).

Usage:
  python3 reference_search.py --brief path/to/brief_full.yaml --job-id JOB_ID [--top 5]
  python3 reference_search.py --queries "q1; q2" --job-id JOB_ID --engine api --cc-only

Result saved to references.json and uploaded to Yandex.Disk (rclone).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
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
# job_id вклеивается в remote-путь rclone → жёсткий allowlist против path-traversal (grok-аудит 2026-07-24)
JOB_ID_RE = re.compile(r"[A-Za-z0-9_.\-]{1,64}$")
YT_DLP = "yt-dlp"


def iso8601_to_seconds(duration: str) -> int:
    m = ISO8601_RE.match(duration)
    if not m:
        return 0
    h = int(m.group(1) or 0)
    mi = int(m.group(2) or 0)
    s = int(m.group(3) or 0)
    return h * 3600 + mi * 60 + s


def _safe_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def build_query(brief: dict) -> str:
    # ВАЖНО: буквальный токен "<N>bpm" в запросе почти всегда даёт 0 результатов —
    # проверено вживую (87bpm+future+garage+... → totalResults=0, без "87bpm" → 3508).
    # BPM влияет на выбор genre/mood слов заранее, но НЕ идёт в текст запроса.
    # ВАЖНО #2: кириллица в запросе тоже убивает результаты — отбрасываем её детерминированно.
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
    # cap длины запроса (grok-аудит): очень длинный q деградирует поиск
    query = query[:200].strip()
    if not query:
        query = "aesthetic ambient visual"
    return query


# ────────────────────────────── движок API (YouTube Data API v3) ──────────────────────────────

def _api_get(url: str, params: dict, label: str, retries: int = 2):
    """GET к Data API с ретраями на transient (429/5xx/сеть). Возвращает JSON или None (не роняет процесс)."""
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, timeout=30)
        except requests.RequestException as e:
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            print(f"[yt] {label} сеть: {e}", file=sys.stderr)
            return None
        if r.status_code == 200:
            return r.json()
        # 429/5xx = transient → ретрай; 4xx (кроме 429) = fatal (ключ/квота/запрос)
        if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            time.sleep(2 * (attempt + 1))
            continue
        print(f"[yt] {label} HTTP {r.status_code}: {r.text[:300]}", file=sys.stderr)
        return None
    return None


def yt_search(query: str, max_results: int = 15, cc_only: bool = False, order: str = "relevance") -> list[dict]:
    url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "part": "snippet",
        "type": "video",
        "order": order,
        "maxResults": max_results,
        "q": query,
        "key": YT_API_KEY,
    }
    if cc_only:
        params["videoLicense"] = "creativeCommon"
    data = _api_get(url, params, "search.list")
    if not data:
        return []
    out = []
    for it in data.get("items", []):
        vid = it.get("id", {}).get("videoId")
        if not vid:
            continue
        sn = it.get("snippet") or {}  # grok-аудит: snippet может отсутствовать в аномальном ответе
        out.append({"video_id": vid, "title": sn.get("title", ""), "channel": sn.get("channelTitle", "")})
    return out


def yt_video_details(video_ids: list[str]) -> dict[str, dict]:
    """videos.list. Режем id чанками по 50 (жёсткий лимит API — grok-аудит 2026-07-24)."""
    url = "https://www.googleapis.com/youtube/v3/videos"
    result: dict[str, dict] = {}
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i + 50]
        data = _api_get(url, {"part": "contentDetails,statistics", "id": ",".join(chunk), "key": YT_API_KEY},
                        "videos.list")
        if not data:
            continue
        for it in data.get("items", []):
            vid = it.get("id")
            if not vid:
                continue
            dur_str = it.get("contentDetails", {}).get("duration", "PT0S")
            result[vid] = {
                "duration_sec": iso8601_to_seconds(dur_str),
                "view_count": _safe_int(it.get("statistics", {}).get("viewCount", 0)),
            }
    return result


# ────────────────────────────── движок yt-dlp (без ключа/квоты) ──────────────────────────────
# Автор ядра — mimo-local (бук), спека+интеграция+ревью — Claude (2026-07-24).

def yt_search_ytdlp(query: str, max_results: int = 15, order: str = "relevance") -> list[dict]:
    """yt-dlp ytsearch → id/title/channel(+duration/views если flat-playlist их дал). Не падает."""
    try:
        result = subprocess.run(
            [YT_DLP, f"ytsearch{max_results}:{query}",
             "--flat-playlist", "--dump-json", "--skip-download", "--no-warnings"],
            capture_output=True, text=True, timeout=120,
        )
    except (subprocess.TimeoutExpired, Exception) as e:
        print(f"[yt-dlp] search «{query}»: {e}", file=sys.stderr)
        return []
    if result.returncode != 0 and not result.stdout.strip():
        print(f"[yt-dlp] search «{query}» rc={result.returncode}: {result.stderr[:200]}", file=sys.stderr)
        return []
    videos = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" not in e:
            continue
        videos.append({
            "video_id": e["id"],
            "title": e.get("title", "") or "",
            "channel": e.get("channel") or e.get("uploader", "") or "",
            "duration_sec": _safe_int(e.get("duration")),
            "view_count": _safe_int(e.get("view_count")),
        })
    return videos


def yt_details_ytdlp(video_ids: list[str]) -> dict[str, dict]:
    """До-обогащение duration/view_count для видео, где flat-playlist их не дал. Не падает."""
    if not video_ids:
        return {}
    urls = [f"https://youtube.com/watch?v={v}" for v in video_ids]
    try:
        result = subprocess.run(
            [YT_DLP, "--dump-json", "--skip-download", "--no-warnings", *urls],
            capture_output=True, text=True, timeout=180,
        )
    except (subprocess.TimeoutExpired, Exception) as e:
        print(f"[yt-dlp] details: {e}", file=sys.stderr)
        return {}
    details = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        vid = e.get("id")
        if not vid:
            continue
        details[vid] = {"duration_sec": _safe_int(e.get("duration")), "view_count": _safe_int(e.get("view_count"))}
    return details


# ────────────────────────────── общий слой ──────────────────────────────

def search_candidates(query: str, max_results: int, order: str, cc_only: bool, engine: str) -> list[dict]:
    if engine == "ytdlp":
        return yt_search_ytdlp(query, max_results, order)
    return yt_search(query, max_results, cc_only, order)


def collect_details(candidates: list[dict], engine: str) -> dict[str, dict]:
    ids = [c["video_id"] for c in candidates]
    if engine == "ytdlp":
        # ytdlp-кандидаты часто уже несут duration/views из flat-playlist; добираем только нули
        det = {c["video_id"]: {"duration_sec": c.get("duration_sec", 0), "view_count": c.get("view_count", 0)}
               for c in candidates}
        missing = [i for i in ids if det.get(i, {}).get("duration_sec", 0) == 0]
        if missing:
            det.update(yt_details_ytdlp(missing))
        return det
    return yt_video_details(ids)


def filter_and_sort(candidates: list[dict], details: dict[str, dict], top: int) -> list[dict]:
    filtered = []
    for c in candidates:
        d = details.get(c["video_id"], {})
        dur = d.get("duration_sec", 0)
        if 60 <= dur <= 600:
            # новый dict вместо мутации исходника (grok-аудит)
            filtered.append({
                "video_id": c["video_id"], "title": c.get("title", ""), "channel": c.get("channel", ""),
                "duration_sec": dur, "view_count": d.get("view_count", 0),
                "url": f"https://youtube.com/watch?v={c['video_id']}",
            })
    filtered.sort(key=lambda x: -x["view_count"])
    return filtered[:top]


def upload_yd(path: str, job_id: str):
    dst = f"{YD_ROOT}/cloud_io/render_jobs/{job_id}/references.json"
    r = subprocess.run(["rclone", "copyto", path, dst], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[rclone] copyto failed: {r.stderr[:300]}", file=sys.stderr)
        print(f"[rclone] локальный артефакт остаётся: {path}", file=sys.stderr)
        sys.exit(2)  # soft-fail (2): локальный файл цел, оркестратор может забрать иначе
    print(f"[rclone] uploaded → {dst}")


def main():
    ap = argparse.ArgumentParser(description="Поиск референс-клипов на YouTube по параметрам трека.")
    ap.add_argument("--brief", default=None, help="путь к brief_full.yaml (не нужен при --queries)")
    ap.add_argument("--queries", type=str, default=None, help="явные запросы через ; (Reference Heist)")
    ap.add_argument("--engine", choices=["ytdlp", "api"], default="ytdlp",
                    help="ytdlp (без ключа/квоты, US-раннер) | api (Data API, нужен для --cc-only). Деф: ytdlp")
    ap.add_argument("--cc-only", action="store_true", help="только Creative Commons (требует --engine api)")
    ap.add_argument("--top-views", action="store_true", help="сортировать по просмотрам (мы и так сортируем по views)")
    ap.add_argument("--job-id", required=True, help="ID задачи (для пути на Яндекс.Диск)")
    ap.add_argument("--top", type=int, default=5, help="сколько результатов вернуть (default: 5)")
    args = ap.parse_args()

    if not JOB_ID_RE.match(args.job_id):
        print(f"[error] недопустимый --job-id «{args.job_id}» (разрешено [A-Za-z0-9_.-], до 64)", file=sys.stderr)
        sys.exit(1)
    if args.top < 1:
        print("[error] --top должен быть ≥ 1", file=sys.stderr)
        sys.exit(1)

    engine = args.engine
    if args.cc_only and engine != "api":
        print("[warn] --cc-only требует Data API → переключаюсь на --engine api", file=sys.stderr)
        engine = "api"
    if engine == "api" and not YT_API_KEY:
        print("[error] engine=api требует YT_API_KEY в окружении", file=sys.stderr)
        sys.exit(1)

    order = "viewCount" if args.top_views else "relevance"

    if args.queries:
        queries = [q.strip() for q in args.queries.split(";") if q.strip()]
        if not queries:
            print("[yt] --queries задан, но пуст после разбора", file=sys.stderr)
            sys.exit(1)
        candidates, seen = [], set()
        for q in queries:
            batch = search_candidates(q, 6, order, args.cc_only, engine)  # один упавший запрос не рушит остальные
            new = [c for c in batch if c["video_id"] not in seen]
            seen.update(c["video_id"] for c in new)
            candidates.extend(new)
            print(f"[{engine}] запрос «{q}»: {len(batch)} найдено, {len(new)} новых")
    else:
        if not args.brief:
            print("[yt] нужен --queries или --brief", file=sys.stderr)
            sys.exit(1)
        try:
            brief = yaml.safe_load(Path(args.brief).read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[error] не прочитать brief {args.brief}: {e}", file=sys.stderr)
            sys.exit(1)
        if not isinstance(brief, dict):
            print(f"[error] brief {args.brief} пуст/не словарь", file=sys.stderr)
            sys.exit(1)
        query = build_query(brief)
        print(f"[{engine}] query: {query}")
        candidates = search_candidates(query, 15, order, args.cc_only, engine)

    if not candidates:
        print(f"[{engine}] ничего не найдено", file=sys.stderr)
        sys.exit(1)
    print(f"[{engine}] кандидатов: {len(candidates)}")

    details = collect_details(candidates, engine)
    results = filter_and_sort(candidates, details, args.top)
    if not results:
        print(f"[{engine}] нет видео, подходящих по длительности (60-600 сек)", file=sys.stderr)
        sys.exit(1)
    print(f"[{engine}] после фильтрации: {len(results)}")

    with tempfile.TemporaryDirectory(prefix="ref_search_") as work:
        out_path = os.path.join(work, "references.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"[{engine}] сохранено → {out_path}")
        upload_yd(out_path, args.job_id)


if __name__ == "__main__":
    main()
