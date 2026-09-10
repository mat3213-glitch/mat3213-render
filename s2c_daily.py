#!/usr/bin/env python3
"""s2c_daily.py — автономный цикл Signal-to-Channel на GH-раннере.

Поток (полностью на раннере, бук не участвует):
  1. собрать свежие сигналы из нескольких источников (HN, Lobsters, arXiv, Grok),
  2. отфильтровать по профилю канала (s2c_channel_profile.json),
  3. отсечь уже отправленные (общий дедуп по namespaced-id, состояние на ЯД),
  4. для каждого отобранного — сгенерить авторский рус. пост через Qwen
     (qwen/qwen_chat.py --stdin), SKIP если нерелевантно/мало данных,
  5. отправить через Cloudflare Worker POST /add (X-Worker-Secret) →
     модерация с кнопками в ЛС владельца → публикация в канале (Worker+KV).

Архитектура (вариант A): ОДИН крон/одна сущность, НЕСКОЛЬКО источников,
общий дедуп. Никаких отдельных кронов на источник — нет гонок за state.json.
Периодичность каждого источника регулируется ВНУТРИ прогона (collected-таймстамп).

Секреты/токены — только через env (GH secrets), в логи — только имена.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin

HERE = Path(__file__).resolve().parent
PROFILE_FILE = HERE / "s2c_channel_profile.json"
QWEN_CHAT = HERE / "qwen" / "qwen_chat.py"

DEFAULT_WORKER_URL = "https://s2c-moderation-1.mat3213.workers.dev"
DEFAULT_YD_STATE = "Content factory/cloud_io/s2c/state.json"

HN_BASE = "https://hacker-news.firebaseio.com/v0"
LOB_BASE = "https://lobste.rs"
ARXIV_API = "https://export.arxiv.org/api/query"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) signal-to-channel/1.0"
IMAGEFREE_API = "https://imagefree.net"
IMAGEFREE_UA = "curl/8.5.0"
IMAGEFREE_POLL_MS = [6000, 4000, 10000, 15000, 20000, 30000, 30000, 30000]

# Отправной редакторский промпт (голос «ИИшницы») — тот же, что в SignalToChannel writer.py.
_EDITOR_PROMPT = """Ты редактор русскоязычного Telegram-канала «ИИшница» про нейросети, AI-инструменты, роботов и технологии.
Сделай самостоятельный короткий пост по публичному сигналу. Пиши как живой наблюдательный редактор с характером.

Стиль и формат:
* Заголовок — КАПС-кликбейт на русском (1 строка, без #), цепляет. Примеры: «ЭТО БЕСПЛАТНО?!», «РОБОТЫ УКРАЛИ РАБОТУ МОДЕРАТОРАМ», «GPT-5.6 РАЗДАЁТ БЕСПЛАТНЫЙ ДОСТУП»
* 3–5 коротких абзацев с пустой строкой между ними. Живой язык, хуки, лёгкий юмор или ирония. Заголовок и тело вместе — не более 780 символов. Ссылку на источник не вставляй: транспорт добавит её сам.
* Хватай за внимание: конкретные цифры, неожиданные сравнения, бытовые аналогии.
* Не используй штампы «революционный», «заслуживает внимания», «в эпоху», «это не просто».
* Не добавляй фактов, которых нет в исходнике. Не обещай доходность и не давай инвестиционных советов.
* Последний абзац — вывод/мораль: 1–2 предложения, зачем это важно обычному читателю.
* Если данных мало или тема не относится к ИИ/роботам/технологиям, ответь ровно: SKIP.
* Рекламные интеграции, продвижение авторских каналов, курсов, вебинаров и реферальных предложений — SKIP. Новости о продуктах допустимы.
* Пиши обычный текст без Markdown и звёздочек. Заголовок только в первой строке, в теле не повторяй.
* Перед ответом молча проверь: заголовок цепляет, есть хук, есть юмор/irony, есть мораль, ссылки в ответе нет.
* Весь текст ТОЛЬКО на русском языке. Никакого английского в теле поста.

Заголовок сигнала: {title}
Описание: {summary}
Источник: {source_url}"""

_CITATION_RE = re.compile(r"\[\[\d+\]\]|\[\d+\]")


def _load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _http_bytes(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 fixed Host names
        return resp.read()


def _http_json(url: str, timeout: int = 20):
    return json.loads(_http_bytes(url, timeout).decode("utf-8", "replace"))


def _domain(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return (urlparse(url).netloc or "").lower().lstrip("www.")
    except Exception:
        return ""


# ----------------------------------------------------------------------------
# Источники. Каждый возвращает список кандидатов:
#   {"id": "<src>:<native_id>", "title", "url", "text", "score", "domain"}
# id обязателен, стабилен, namespaced — служит ключом общего дедупа.
# ----------------------------------------------------------------------------

def hn_fetch(limit: int, profile: dict) -> list[dict]:
    try:
        ids = _http_json(f"{HN_BASE}/topstories.json")
    except (HTTPError, URLError, OSError):
        return []
    out = []
    for item_id in [int(i) for i in ids[:limit]]:
        try:
            item = _http_json(f"{HN_BASE}/item/{item_id}.json")
        except (HTTPError, URLError, OSError):
            continue
        if not item or item.get("type") != "story":
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        if not title or not url.startswith("http"):
            continue
        out.append({
            "id": f"hn:{item_id}",
            "title": title,
            "url": url,
            "domain": _domain(url),
            "text": str(item.get("text") or "").strip() or None,
            "score": item.get("score") or 0,
        })
    return out


def lob_fetch(limit: int, profile: dict) -> list[dict]:
    try:
        stories = _http_json(f"{LOB_BASE}/newest.json?count={limit}")
    except (HTTPError, URLError, OSError):
        return []
    if not isinstance(stories, list):
        return []
    out = []
    for s in stories:
        title = str(s.get("title") or "").strip()
        url = str(s.get("url") or "").strip() or f"{LOB_BASE}/s/{s.get('short_id', '')}".strip()
        if not title or not url.startswith("http"):
            continue
        out.append({
            "id": f"lob:{s.get('short_id') or s.get('id') or title}",
            "title": title,
            "url": url,
            "domain": _domain(url),
            "text": str(s.get("description") or "").strip() or None,
            "score": s.get("score") or 0,
        })
    return out


_ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}
_ARXIV_QUERY = (
    "search_query=cat:cs.AI+OR+cat:cs.LG+OR+cat:cs.CL"
    "&sortBy=submittedDate&sortOrder=descending&max_results={limit}"
)


def _arxiv_id(entry_id: str) -> str:
    # entry_id вида http://arxiv.org/abs/2401.01234v1 -> берём короткий id
    m = re.search(r"/abs/([^/]+)", entry_id)
    return m.group(1) if m else entry_id


def arxiv_fetch(limit: int, profile: dict) -> list[dict]:
    url = f"{ARXIV_API}?{_ARXIV_QUERY.format(limit=limit)}"
    try:
        root = ET.fromstring(_http_bytes(url, timeout=60))
    except (HTTPError, URLError, OSError, ET.ParseError):
        return []
    out = []
    for entry in root.findall("atom:entry", _ARXIV_NS):
        eid = (entry.findtext("atom:id", default="", namespaces=_ARXIV_NS) or "").strip()
        title = re.sub(r"\s+", " ", entry.findtext("atom:title", default="", namespaces=_ARXIV_NS) or "").strip()
        summary = re.sub(r"\s+", " ", entry.findtext("atom:summary", default="", namespaces=_ARXIV_NS) or "").strip()
        if not title or not eid:
            continue
        out.append({
            "id": f"arxiv:{_arxiv_id(eid)}",
            "title": title,
            "url": eid,
            "domain": "arxiv.org",
            "text": summary or None,
            "score": 0,
        })
    return out


# ----------------------------------------------------------------------------
# Grok/Perplexity дневные сигналы из mat3213-signals/signals/incoming/
# Формат: grok_YYYY-MM-DD.json → {findings:[], finding_of_the_day:{}, deep_internet:[], ...}
# auto_relevant=True — это уже отфильтрованный AI-сигнал от разведчика.
# ----------------------------------------------------------------------------

_SIGNALS_REPO = "mat3213-glitch/mat3213-signals"


def _gh_json(url: str, token: str, timeout: int = 25):
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {token}",
        "User-Agent": _UA,
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _sig_id(source_url: str, title: str) -> str:
    key = source_url if source_url.startswith("http") else title
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    return f"sig:{h}"


def _finding_to_candidate(f: dict, source: str = "") -> dict | None:
    if not isinstance(f, dict):
        return None
    title = str(f.get("title") or f.get("signal") or f.get("name") or "").strip()
    url = str(f.get("source_url") or f.get("url") or f.get("source_link") or f.get("link") or "").strip()
    if not title:
        return None
    text = "\n".join(str(f[k]).strip() for k in ("what_found", "what", "summary", "description", "practical_value", "why_it_matters", "caveat", "free", "limit") if f.get(k))
    return {
        "id": _sig_id(url, title),
        "title": title,
        "url": url or f"https://x.com/i/grok",
        "domain": _domain(url) if url.startswith("http") else source or "grok",
        "text": text or None,
        "score": 50,
        "source": source,
    }


def grok_fetch(limit: int, profile: dict) -> list[dict]:
    token = os.getenv("GH_PAT", "").strip()
    if not token:
        raise RuntimeError("grok: missing GH_PAT")
        return []
    today = datetime.now(timezone.utc)
    dates = [(today - timedelta(days=d)).strftime("%Y-%m-%d") for d in range(7)]
    out = []
    for date_str in dates:
        path = f"signals/incoming/grok_{date_str}.json"
        url = f"https://api.github.com/repos/{_SIGNALS_REPO}/contents/{path}"
        try:
            raw = _gh_json(url, token)
            data = json.loads(
                __import__("base64").b64decode(raw["content"]).decode("utf-8", "replace")
            )
        except HTTPError as exc:
            if exc.code != 404:
                raise
            continue
        if str(data.get("source") or "grok").lower() == "chatgpt":
            continue
        # основной пул — candidates (Grok) или findings (legacy)
        for f in data.get("candidates", data.get("findings", [])):
            c = _finding_to_candidate(f, source="grok")
            if c:
                out.append(c)
        # find_of_the_day / finding_of_the_day — приоритетный
        fotd = data.get("find_of_the_day") or data.get("finding_of_the_day")
        if isinstance(fotd, dict) and fotd.get("title"):
            c = _finding_to_candidate(fotd, source="grok")
            if c:
                c["score"] = 100
                out.append(c)
        # from_the_depths / deep_internet
        for f in data.get("from_the_depths", data.get("deep_internet", [])):
            c = _finding_to_candidate(f, source="grok")
            if c:
                out.append(c)
        # freebies_today / freebies_of_the_day
        for f in data.get("freebies_today", data.get("freebies_of_the_day", [])):
            if isinstance(f, dict):
                c = _finding_to_candidate(f, source="grok")
                if c:
                    out.append(c)
            elif isinstance(f, str) and f.strip():
                out.append({
                    "id": _sig_id("grok-freebie", f),
                    "title": f.strip(),
                    "url": "https://x.com/i/grok",
                    "domain": "grok",
                    "text": None,
                    "score": 40,
                })
        # build_today — extra Grok field
        builds = data.get("build_today", [])
        for f in ([builds] if isinstance(builds, dict) else builds):
            if isinstance(f, dict):
                c = _finding_to_candidate(f, source="grok")
                if c:
                    c["score"] = 60
                    out.append(c)
    return out


# ----------------------------------------------------------------------------
# ChatGPT дневные сигналы из mat3213-signals/signals/incoming/chatgpt_YYYY-MM-DD.json
# Формат: {date, source:"chatgpt", findings:[{title, source_url, ...}], ...}
# ----------------------------------------------------------------------------

def chatgpt_fetch(limit: int, profile: dict) -> list[dict]:
    token = os.getenv("GH_PAT", "").strip()
    if not token:
        raise RuntimeError("chatgpt: missing GH_PAT")
    today = datetime.now(timezone.utc)
    dates = [(today - timedelta(days=d)).strftime("%Y-%m-%d") for d in range(7)]
    out = []
    for date_str in dates:
        for prefix in ("chatgpt", "grok"):
            path = f"signals/incoming/{prefix}_{date_str}.json"
            url = f"https://api.github.com/repos/{_SIGNALS_REPO}/contents/{path}"
            try:
                raw = _gh_json(url, token)
                data = json.loads(__import__("base64").b64decode(raw["content"]).decode("utf-8", "replace"))
            except HTTPError as exc:
                if exc.code != 404:
                    raise
                continue
            if str(data.get("source") or prefix).lower() != "chatgpt":
                continue
            for f in data.get("findings", []):
                c = _finding_to_candidate(f, source="chatgpt")
                if c:
                    c["score"] = 80
                    out.append(c)
    if not out:
        print("::warning::chatgpt: no reports with candidates in the last 7 days; check report delivery")
    return out


# ----------------------------------------------------------------------------
# RSS из Telegram-каналов (публичный t.me/s/ HTML)
# Список каналов задаётся через env S2C_TG_CHANNELS (через запятую) или по умолчанию.
# auto_relevant=False — фильтруем по include_any (тематика канала не гарантирована).
# ----------------------------------------------------------------------------

_DEFAULT_TG_CHANNELS = [
    "neuraldvig",
    "AI_Chad",
    "AI_to_business",
    "age_of_it",
    "svodkaai_ai",
    "NeuroRazvedka",
    "cryptoperchikk",
    "ai2smm",
    "inclient",
]


def _parse_tg_channel_slug(val: str) -> str:
    val = val.strip()
    val = re.sub(r"^https?://t\.me/", "", val)
    val = val.strip("/")
    return val.split("/")[-1] if val else ""


def _rss_candidate_from_post(link: str, title: str, text: str) -> dict | None:
    if not link or not title:
        return None
    domain = "t.me"
    m = re.search(r"t\.me/([^/]+)/\d+", link)
    chan = m.group(1) if m else ""
    return {
        "id": f"rss:{chan}:{link.split('/')[-1]}",
        "title": title.strip(),
        "url": link.strip(),
        "domain": domain,
        "text": text.strip() or None,
        "score": 10,
    }


def rss_fetch(limit: int, profile: dict) -> list[dict]:
    raw = os.getenv("S2C_TG_CHANNELS", "").strip()
    channels = [_parse_tg_channel_slug(c) for c in raw.split(",") if c.strip()] if raw else list(_DEFAULT_TG_CHANNELS)
    out = []
    for chan in channels:
        url = f"https://t.me/s/{chan}"
        try:
            html = _http_bytes(url, timeout=20).decode("utf-8", "replace")
        except (HTTPError, URLError, OSError):
            continue
        # парсим dev.to-style divs: class="tgme_widget_message_wrap"
        blocks = re.findall(
            r'<a\s+class="tgme_widget_message_wrap[^"]*"\s+href="(https://t\.me/[^"]+)"[^>]*>.*?'
            r'<div\s+class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
            html, re.S,
        )
        for link, body in blocks[:20]:
            clean = re.sub(r"<[^>]+>", "", body).strip()
            first_line = clean.split("\n")[0][:200] if clean else ""
            c = _rss_candidate_from_post(link, first_line, clean)
            if c:
                out.append(c)
    return out[:limit]


_OFFICIAL_FEEDS = {
    "openai": "https://openai.com/news/rss.xml",
    "deepmind": "https://deepmind.google/blog/rss.xml",
    "huggingface": "https://huggingface.co/blog/feed.xml",
    "microsoft": "https://www.microsoft.com/en-us/research/feed/",
    "nvidia": "https://blogs.nvidia.com/blog/category/generative-ai/feed/",
}


def official_blogs_fetch(limit: int, profile: dict) -> list[dict]:
    buckets = []
    for source, feed_url in _OFFICIAL_FEEDS.items():
        try:
            root = ET.fromstring(_http_bytes(feed_url, timeout=25))
        except (HTTPError, URLError, OSError, ET.ParseError) as exc:
            print(f"::warning::official blog {source}: {type(exc).__name__}: {exc}")
            continue
        entries = root.findall(".//item") or root.findall(".//{*}entry")
        source_items = []
        for entry in entries[:8]:
            title = (entry.findtext("title") or entry.findtext("{*}title") or "").strip()
            link_node = entry.find("link")
            if link_node is None:
                link_node = entry.find("{*}link")
            link = ""
            if link_node is not None:
                link = (link_node.get("href") or link_node.text or "").strip()
            summary = (entry.findtext("description") or entry.findtext("{*}summary")
                       or entry.findtext("{*}content") or "").strip()
            summary = re.sub(r"<[^>]+>", " ", summary)
            summary = re.sub(r"\s+", " ", summary).strip()
            if title and link:
                source_items.append({"id": f"official:{source}:{hashlib.sha1(link.encode()).hexdigest()[:16]}",
                                     "title": title, "url": link, "domain": _domain(link),
                                     "text": summary[:3000] or None, "score": 35, "source": source})
        if source_items:
            buckets.append(source_items)
    out = []
    for index in range(max((len(items) for items in buckets), default=0)):
        for items in buckets:
            if index < len(items):
                out.append(items[index])
                if len(out) >= limit:
                    return out
    return out


_DEFAULT_YOUTUBE_CHANNELS = {
    "OpenAI": "UCXZCJLdBC09xxGZ6gcdrc6A",
    "GoogleDeepMind": "UCP7jMXSY2xbc3KCAE0MHQ-A",
    "Anthropic": "UCrDwWp7EBBv4NwvScIpBDOA",
    "NVIDIA": "UCHuiy8bXnmK5nisYHUd1J5g",
}


def _youtube_feed_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128 Safari/537.36",
        "Accept": "application/atom+xml,application/xml,text/xml,*/*",
    })
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read()


def _youtube_page_videos(handle: str) -> list[tuple[str, str]]:
    html = _http_bytes(f"https://www.youtube.com/@{handle}/videos", timeout=30).decode("utf-8", "replace")
    out, seen = [], set()
    for match in re.finditer(r'"videoId":"([A-Za-z0-9_-]{11})"', html):
        video_id = match.group(1)
        if video_id in seen:
            continue
        nearby = html[match.end():match.end() + 1200]
        title_match = re.search(r'"title":\{"runs":\[\{"text":("(?:[^"\\]|\\.)*")', nearby)
        if not title_match:
            continue
        try:
            title = json.loads(title_match.group(1)).strip()
        except json.JSONDecodeError:
            continue
        if title:
            out.append((video_id, title))
            seen.add(video_id)
    return out


def _invidious_latest(channel_id: str) -> list[tuple[str, str]]:
    # Officially listed public instances; used only when YouTube blocks both its
    # Atom feed and public videos page on the GitHub runner.
    for host in ("inv.nadeko.net", "invidious.nerdvpn.de", "yt.chocolatemoo53.com"):
        try:
            data = _http_json(f"https://{host}/api/v1/channels/{channel_id}/latest", timeout=25)
        except (HTTPError, URLError, OSError, json.JSONDecodeError):
            continue
        videos = [(str(x.get("videoId") or ""), str(x.get("title") or "").strip()) for x in data if isinstance(x, dict)]
        if videos:
            return videos
    return []


def _yt_dlp_latest(handle: str) -> list[tuple[str, str]]:
    """Use yt-dlp's maintained YouTube extractor as the final no-key fallback."""
    proc = subprocess.run(
        [sys.executable, "-m", "yt_dlp", "--flat-playlist", "--playlist-end", "5",
         "--dump-single-json", "--no-warnings", f"https://www.youtube.com/@{handle}/videos"],
        capture_output=True, text=True, timeout=45, check=True,
    )
    data = json.loads(proc.stdout)
    return [(str(x.get("id") or ""), str(x.get("title") or "").strip())
            for x in data.get("entries", []) if isinstance(x, dict)]


def youtube_fetch(limit: int, profile: dict) -> list[dict]:
    raw = os.getenv("S2C_YOUTUBE_CHANNELS", "").strip()
    configured = [x.strip() for x in raw.split(",") if x.strip()]
    channels = {x:x for x in configured} if configured else _DEFAULT_YOUTUBE_CHANNELS
    out = []
    for label, channel_id in channels.items():
        videos = []
        try:
            feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
            feed = ET.fromstring(_youtube_feed_bytes(feed_url))
        except (HTTPError, URLError, OSError, ET.ParseError) as exc:
            print(f"::warning::youtube {label}: {type(exc).__name__}: {exc}")
            try:
                videos = _youtube_page_videos(label)
            except (HTTPError, URLError, OSError) as page_exc:
                print(f"::warning::youtube page {label}: {type(page_exc).__name__}: {page_exc}")
        else:
            ns = {"atom":"http://www.w3.org/2005/Atom", "yt":"http://www.youtube.com/xml/schemas/2015"}
            entries = feed.findall("atom:entry", ns) or feed.findall(".//{*}entry")
            videos = [(
                entry.findtext("yt:videoId", default="", namespaces=ns) or entry.findtext("{*}videoId", default=""),
                (entry.findtext("atom:title", default="", namespaces=ns) or entry.findtext("{*}title", default="")).strip(),
            ) for entry in entries[:5]]
        if not videos:
            videos = _invidious_latest(channel_id)
        if not videos:
            try:
                videos = _yt_dlp_latest(label)
            except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as ytdlp_exc:
                print(f"::warning::youtube yt-dlp {label}: {type(ytdlp_exc).__name__}: {ytdlp_exc}")
        if not videos:
            print(f"::warning::youtube {label}: no videos parsed from YouTube or fallbacks")
        for video_id, title in videos[:5]:
            if video_id and title:
                out.append({"id":f"youtube:{video_id}", "title":title,
                            "url":f"https://www.youtube.com/watch?v={video_id}",
                            "domain":"youtube.com", "text":f"Видео официального канала {label}",
                            "score":30, "image_url":f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"})
    return out[:limit]


# Реестр источников: name -> (fetch_func, auto_relevant)
# auto_relevant=True  — источник уже «по построению» про тему (arXiv AI, Grok, ChatGPT), фильтруем только exclude.
# auto_relevant=False — обычный новостной, применяем include_any фильтр (HN, Lobsters, RSS).
SOURCES = {
    "hn": (hn_fetch, False),
    "lob": (lob_fetch, False),
    "arxiv": (arxiv_fetch, True),
    "grok": (grok_fetch, True),
    "chatgpt": (chatgpt_fetch, True),
    "youtube": (youtube_fetch, True),
    "official": (official_blogs_fetch, True),
    "rss": (rss_fetch, False),
}


# ----------------------------------------------------------------------------
# ДЕДУП / состояние
# ----------------------------------------------------------------------------

def load_state() -> dict:
    state_path = Path("s2c_state.json")
    if not state_path.exists():
        return {"sent_ids": [], "collected": {}}
    try:
        data = _load_json(state_path)
        if not isinstance(data, dict):
            return {"sent_ids": [], "collected": {}}
        data.setdefault("sent_ids", [])
        data.setdefault("collected", {})
        return data
    except Exception:
        return {"sent_ids": [], "collected": {}}


def _normalize_sent(v) -> set:
    # миграция старых int-id (HN) в строковые namespaced id
    out = set()
    for x in v:
        if isinstance(x, int):
            out.add(f"hn:{x}")
        else:
            out.add(str(x))
    return out


def save_state_and_push(state: dict, yandex_state: str):
    try:
        with open("s2c_state.json", "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
        subprocess.run(["rclone", "copyto", "s2c_state.json", f"ydrive:{yandex_state}"],
                       check=True, timeout=120)
        print(f"[s2c] состояние обновлено: {len(state['sent_ids'])} sent; collected={state['collected']}")
    except Exception as e:
        print(f"[s2c] не записал состояние: {type(e).__name__}")
        raise


def relevant(item: dict, profile: dict) -> bool:
    hay = f"{item['title']} {item.get('domain') or ''}".lower()
    inc = [w.lower() for w in profile.get("include_any", [])]
    exc = [w.lower() for w in profile.get("exclude_any", [])]
    if any(e in hay for e in exc):
        return False
    return any(w in hay for w in inc)


# ----------------------------------------------------------------------------
# Qwen / og:image / Worker
# ----------------------------------------------------------------------------

def qwen_generate(prompt: str, model: str, timeout: int = 300) -> str:
    if not QWEN_CHAT.exists():
        return ""
    proc = subprocess.run(
        ["python3", str(QWEN_CHAT), "--stdin", "--model", model, "--timeout", str(timeout)],
        input=prompt, capture_output=True, text=True, timeout=timeout + 90,
    )
    if proc.returncode:
        raise RuntimeError(f"Qwen exited with code {proc.returncode}")
    text = (proc.stdout or "").strip()
    text = _CITATION_RE.sub("", text).replace("[[", "").replace("]]", "").strip()
    return text


def og_image(url: str, timeout: int = 15) -> str | None:
    if not url.startswith(("http://", "https://")):
        return None
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read(200_000).decode("utf-8", "replace")
    except (HTTPError, URLError, OSError):
        return None
    pat = re.compile(r'property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.I)
    m = pat.search(html)
    img = m.group(1).strip() if m else None
    if not img:
        m2 = re.compile(r'content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.I)
        m2 = m2.search(html)
        img = m2.group(1).strip() if m2 else None
    if img:
        img = urljoin(url, img)
    return img


def _imagefree_prompt(candidate: dict) -> str:
    title = str(candidate.get("title") or "").strip()
    summary = re.sub(r"\s+", " ", str(candidate.get("text") or "")).strip()
    topic = f"{title}. {summary[:500]}".strip()
    return (
        "Editorial technology news cover image, no text, no words, no letters, no typography, "
        "no captions, no logos, no watermark, no faces, no people, no portraits. "
        "Modern clean composition, realistic objects or abstract technical visual, "
        f"topic: {topic}"
    )


def _imagefree_post(path: str, body: dict, timeout: int = 45) -> dict:
    req = urllib.request.Request(
        f"{IMAGEFREE_API}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": IMAGEFREE_UA},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _imagefree_submit(prompt: str, aspect: str) -> tuple[str | None, str | None]:
    try:
        data = _imagefree_post("/api/generate", {
            "prompt": prompt,
            "aspect_ratio": aspect,
            "turnstile_token": "",
        })
    except HTTPError as exc:
        if exc.code in (403, 412):
            return None, "cf_challenge"
        if exc.code == 429:
            return None, "rate_limited"
        return None, f"http_{exc.code}"
    except (URLError, OSError, TimeoutError) as exc:
        return None, f"net:{type(exc).__name__}"
    if not isinstance(data, dict):
        return None, "bad_body"
    task_id = data.get("taskId")
    if task_id:
        return str(task_id), None
    if data.get("errorCode"):
        return None, f"site:{data['errorCode']}"
    return None, f"site:{data.get('error', 'no_task_id')}"


def _imagefree_poll(task_id: str) -> dict | None:
    url = f"{IMAGEFREE_API}/api/generate/status?taskId={urllib.parse.quote(task_id)}"
    req = urllib.request.Request(url, headers={"User-Agent": IMAGEFREE_UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except (HTTPError, URLError, OSError, TimeoutError, json.JSONDecodeError):
        return None


def _imagefree_wait(task_id: str) -> tuple[str | None, str | None]:
    for delay_ms in IMAGEFREE_POLL_MS:
        status = _imagefree_poll(task_id)
        if isinstance(status, dict):
            state = status.get("status")
            if state == "completed":
                image = status.get("image") or status.get("image_url") or status.get("imageUrl") or status.get("url")
                return (str(image), None) if image else (None, "no_image_url")
            if state == "failed":
                return None, str(status.get("error") or status.get("errorCode") or "failed")
            if status.get("errorCode"):
                return None, f"site:{status['errorCode']}"
        time.sleep(delay_ms / 1000.0)
    return None, "timeout"


def _download_png(url: str, timeout: int = 90) -> bytes | None:
    if not url.startswith(("http://", "https://")):
        return None
    req = urllib.request.Request(url, headers={"User-Agent": IMAGEFREE_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(9_500_001)
    except (HTTPError, URLError, OSError, TimeoutError):
        return None
    if data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) < 5000 or len(data) > 9_500_000:
        return None
    return data


def imagefree_image_bytes(candidate: dict, aspect: str = "4:3") -> bytes | None:
    prompt = _imagefree_prompt(candidate)
    task_id, err = _imagefree_submit(prompt, aspect)
    if err:
        print(f"::warning::imagefree submit {candidate.get('id')}: {err}")
        return None
    print(f"[s2c] imagefree task {task_id[:8]}… для {candidate.get('id')}")
    image_url, err = _imagefree_wait(task_id)
    if err:
        print(f"::warning::imagefree status {candidate.get('id')}: {err}")
        return None
    image = _download_png(image_url)
    if not image:
        print(f"::warning::imagefree download {candidate.get('id')}: bad_png")
    time.sleep(random.uniform(6, 15))
    return image


def candidate_image(candidate: dict) -> str | None:
    if candidate.get("image_url"):
        return candidate["image_url"]
    if candidate["id"].startswith("arxiv:"):
        paper_id = candidate["id"].split(":", 1)[1]
        try:
            html_url = f"https://arxiv.org/html/{paper_id}"
            html = _http_bytes(html_url, timeout=30).decode("utf-8", "replace")
            for src in re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html, re.I):
                if not re.search(r'logo|icon|badge', src, re.I):
                    return urljoin(html_url + "/", src)
        except (HTTPError, URLError, OSError):
            pass
        return None
    return og_image(candidate["url"])


def _multipart_body(fields: dict, image: bytes) -> tuple[bytes, str]:
    boundary = f"s2c-{hashlib.sha1(image[:1024]).hexdigest()[:24]}"
    chunks: list[bytes] = []
    for key, value in fields.items():
        if value is None:
            continue
        chunks.extend([
            f"--{boundary}\r\n".encode("ascii"),
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("ascii"),
            str(value).encode("utf-8"),
            b"\r\n",
        ])
    chunks.extend([
        f"--{boundary}\r\n".encode("ascii"),
        b'Content-Disposition: form-data; name="image"; filename="imagefree.png"\r\n',
        b"Content-Type: image/png\r\n\r\n",
        image,
        b"\r\n",
        f"--{boundary}--\r\n".encode("ascii"),
    ])
    return b"".join(chunks), boundary


def worker_add(base_url: str, secret: str, draft: dict, image: bytes | None = None) -> tuple[bool, dict | str]:
    headers = {"X-Worker-Secret": secret, "User-Agent": _UA}
    if image:
        body, boundary = _multipart_body(draft, image)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    else:
        body = json.dumps(draft, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(f"{base_url}/add", data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return bool(data.get("ok")), data
    except HTTPError as e:
        return False, f"HTTP {e.code}"
    except (URLError, OSError) as e:
        return False, str(e)


def split_post(text: str) -> tuple[str, str]:
    clean = text.replace("**", "").strip()
    clean = re.sub(r"\n*\s*Источник\s*:\s*(?:\[[^\]]+\]\([^\)]+\)|https?://\S+)\s*$", "", clean, flags=re.I).strip()
    lines = clean.splitlines()
    if len(lines) > 1 and len(lines[0]) <= 200 and lines[0].strip():
        body = "\n".join(lines[1:]).strip()
        if body:
            return lines[0].strip(), body
    return "", clean


def fair_candidates(buckets: dict, cursor: int):
    names = list(buckets)
    if not names:
        return []
    offset = cursor % len(names)
    order = names[offset:] + names[:offset]
    result, seen = [], set()
    for i in range(max((len(b) for b in buckets.values()), default=0)):
        for name in order:
            if i < len(buckets[name]):
                item = buckets[name][i]
                if item["id"] not in seen:
                    result.append((name, item))
                    seen.add(item["id"])
    return result


def main() -> int:
    dry = "--dry-run" in sys.argv
    worker_url = os.getenv("S2C_WORKER_URL", DEFAULT_WORKER_URL).rstrip("/")
    worker_secret = os.getenv("S2C_WORKER_SECRET", "").strip()
    qwen_model = os.getenv("QWEN_MODEL", "Qwen3-Coder").strip()
    per_source = int(os.getenv("S2C_PER_SOURCE", "12"))
    max_drafts = int(os.getenv("S2C_MAX_DRAFTS", "3"))
    yandex_state = os.getenv("S2C_YD_STATE", DEFAULT_YD_STATE)
    now = datetime.now(timezone.utc)

    profile = _load_json(PROFILE_FILE) if PROFILE_FILE.exists() else {"include_any": [], "exclude_any": []}
    state = load_state()
    sent_set = _normalize_sent(state.get("sent_ids", []))
    deferred = state.setdefault("deferred_until", {})
    failures = 0
    print(f"[s2c] профиль: {os.path.basename(str(PROFILE_FILE))}; уже отправлено: {len(sent_set)}")

    # 1–2. собрать + отфильтровать по каждому источнику
    buckets: dict[str, list[dict]] = {}
    for name, (fetch_fn, auto_relevant) in SOURCES.items():
        buckets.setdefault(name, [])
        try:
            items = fetch_fn(per_source, profile) or []
        except Exception as e:  # noqa: BLE001
            print(f"[s2c] {name}: ОШИБКА сбора — {type(e).__name__}: {e}")
            failures += 1
            continue
        fresh = []
        for it in items:
            if it["id"] in sent_set:
                continue
            if deferred.get(it["id"], "") > now.isoformat():
                continue
            if auto_relevant:
                hay = f"{it['title']} {it.get('domain') or ''}".lower()
                if any(e in hay for e in [w.lower() for w in profile.get("exclude_any", [])]):
                    continue
            else:
                if not relevant(it, profile):
                    continue
            fresh.append(it)
        fresh.sort(key=lambda x: x["score"], reverse=True)
        buckets[name] = fresh
        print(f"[s2c] {name}: собрано {len(items)}, релевантных новых: {len(fresh)}")
        state["collected"][name] = now.isoformat()

    # глобальный лимит max_drafts — round-robin по источникам, чтобы никто не голодал
    candidates = fair_candidates(buckets, int(state.get("source_cursor", 0)))
    print(f"[s2c] ИТОГО кандидатов к генерации: {len(candidates)}")
    for _, c in candidates[:max_drafts]:
        print(f"  [sel] {c['id']} score={c.get('score')} {c['title']}")

    if dry:
        for _, c in candidates[:max_drafts]:
            print(f"  [dry] {c['id']} score={c['score']} {c['title']}")
        return 0

    if not worker_secret:
        print("[s2c] нет S2C_WORKER_SECRET (GH secret) — пропуск отправки")
        return 1

    new_sent = set(sent_set)
    delivered = 0
    max_attempts = int(os.getenv("S2C_MAX_ATTEMPTS", "9"))
    for name, c in candidates[:max_attempts]:
        if delivered >= max_drafts:
            break
        state["source_cursor"] = (list(buckets).index(name) + 1) % len(buckets)
        prompt = _EDITOR_PROMPT.format(title=c["title"], summary=(c["text"] or "нет"), source_url=(c["url"] or "не указан"))
        try:
            text = qwen_generate(prompt, qwen_model)
        except Exception as exc:
            print(f"::error::generation {c['id']}: {type(exc).__name__}")
            failures += 1
            continue
        if not text or text.strip().upper() == "SKIP":
            print(f"  [skip] {c['id']}: Qwen вернул SKIP/пусто")
            if text.strip().upper() == "SKIP":
                deferred[c["id"]] = (now + timedelta(days=1)).isoformat()
            else:
                failures += 1
            continue
        img = candidate_image(c)
        image_bytes = None
        if not img:
            image_bytes = imagefree_image_bytes(c)
        title, body = split_post(text)
        draft = {"id": c["id"], "source": c.get("source") or name, "source_url": c.get("url") or "", "title": title, "text": body, "image_url": img,
                 "source_text": f"{c['title']}\n{c.get('text') or ''}"}
        ok, resp = worker_add(worker_url, worker_secret, draft, image_bytes)
        filtered = isinstance(resp, dict) and resp.get("skipped")
        print(f"  [add] {draft['id']} ok={ok} filtered={bool(filtered)}")
        if ok:
            new_sent.add(c["id"])
            if not filtered:
                delivered += 1
        else:
            failures += 1

    # 3. сохранить дедуп + таймстампы на ЯД (rclone сделает workflow / сам)
    # персистим нормализованный namespaced набор, чтобы состояние было чистым
    persisted = _normalize_sent(state.get("sent_ids", []))
    state["sent_ids"] = sorted(persisted | new_sent)
    print(f"[s2c] доставлено: {delivered}; обработано: {len(new_sent - sent_set)}; в состоянии всего: {len(state['sent_ids'])}")
    save_state_and_push(state, yandex_state)

    return 1 if failures and delivered < max_drafts else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"[s2c] FATAL: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
