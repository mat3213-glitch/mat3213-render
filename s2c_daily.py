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
import re
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError

HERE = Path(__file__).resolve().parent
PROFILE_FILE = HERE / "s2c_channel_profile.json"
QWEN_CHAT = HERE / "qwen" / "qwen_chat.py"

DEFAULT_WORKER_URL = "https://s2c-moderation-1.mat3213.workers.dev"
DEFAULT_YD_STATE = "Content factory/cloud_io/s2c/state.json"

HN_BASE = "https://hacker-news.firebaseio.com/v0"
LOB_BASE = "https://lobste.rs"
ARXIV_API = "http://export.arxiv.org/api/query"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) signal-to-channel/1.0"

# Отправной редакторский промпт (голос «ИИшницы») — тот же, что в SignalToChannel writer.py.
_EDITOR_PROMPT = """Ты редактор русскоязычного Telegram-канала «ИИшница» про нейросети, AI-инструменты, роботов и технологии.
Сделай самостоятельный короткий пост по публичному сигналу. Пиши как живой наблюдательный редактор с характером.

Стиль и формат:
* Заголовок — КАПС-кликбейт на русском (1 строка, без #), цепляет. Примеры: «ЭТО БЕСПЛАТНО?!», «РОБОТЫ УКРАЛИ РАБОТУ МОДЕРАТОРАМ», «GPT-5.6 РАЗДАЁТ БЕСПЛАТНЫЙ ДОСТУП»
* 3–5 коротких абзацев с пустой строкой между ними. Живой язык, хуки, лёгкий юмор или ирония.
* Хватай за внимание: конкретные цифры, неожиданные сравнения, бытовые аналогии.
* Не используй штампы «революционный», «заслуживает внимания», «в эпоху», «это не просто».
* Не добавляй фактов, которых нет в исходнике. Не обещай доходность и не давай инвестиционных советов.
* Последний абзац — вывод/мораль: 1–2 предложения, зачем это важно обычному читателю.
* Последняя строка: «Источник: <ссылка>».
* Если данных мало или тема не относится к ИИ/роботам/технологиям, ответь ровно: SKIP.
* Перед ответом молча проверь: заголовок цепляет, есть хук, есть юмор/irony, есть мораль, есть ссылка.
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


def _finding_to_candidate(f: dict) -> dict | None:
    if not isinstance(f, dict):
        return None
    title = str(f.get("title") or "").strip()
    url = str(f.get("source_url") or f.get("url") or f.get("source_link") or f.get("link") or "").strip()
    if not title:
        return None
    text = str(f.get("what_found") or f.get("what") or f.get("practical_value") or "").strip()
    return {
        "id": _sig_id(url, title),
        "title": title,
        "url": url or f"https://x.com/i/grok",
        "domain": _domain(url) if url.startswith("http") else "grok",
        "text": text or None,
        "score": 50,
    }


def grok_fetch(limit: int, profile: dict) -> list[dict]:
    token = os.getenv("GH_PAT", "").strip()
    if not token:
        print("[s2c] grok: нет GITHUB_TOKEN — пропуск")
        return []
    today = datetime.now(timezone.utc)
    dates = [(today - timedelta(days=d)).strftime("%Y-%m-%d") for d in range(2)]
    out = []
    for date_str in dates:
        path = f"signals/incoming/grok_{date_str}.json"
        url = f"https://api.github.com/repos/{_SIGNALS_REPO}/contents/{path}"
        try:
            raw = _gh_json(url, token)
            data = json.loads(
                __import__("base64").b64decode(raw["content"]).decode("utf-8", "replace")
            )
        except (HTTPError, URLError, OSError, KeyError, json.JSONDecodeError):
            continue
        # основной пул — candidates (Grok) или findings (legacy)
        for f in data.get("candidates", data.get("findings", [])):
            c = _finding_to_candidate(f)
            if c:
                out.append(c)
        # find_of_the_day / finding_of_the_day — приоритетный
        fotd = data.get("find_of_the_day") or data.get("finding_of_the_day")
        if isinstance(fotd, dict) and fotd.get("title"):
            c = _finding_to_candidate(fotd)
            if c:
                c["score"] = 100
                out.append(c)
        # from_the_depths / deep_internet
        for f in data.get("from_the_depths", data.get("deep_internet", [])):
            c = _finding_to_candidate(f)
            if c:
                out.append(c)
        # freebies_today / freebies_of_the_day
        for f in data.get("freebies_today", data.get("freebies_of_the_day", [])):
            if isinstance(f, dict):
                c = _finding_to_candidate(f)
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
        for f in data.get("build_today", []):
            if isinstance(f, dict):
                c = _finding_to_candidate(f)
                if c:
                    c["score"] = 60
                    out.append(c)
    return out[:limit]


# ----------------------------------------------------------------------------
# ChatGPT дневные сигналы из mat3213-signals/signals/incoming/chatgpt_YYYY-MM-DD.json
# Формат: {date, source:"chatgpt", findings:[{title, source_url, ...}], ...}
# ----------------------------------------------------------------------------

def chatgpt_fetch(limit: int, profile: dict) -> list[dict]:
    token = os.getenv("GH_PAT", "").strip()
    if not token:
        return []
    today = datetime.now(timezone.utc)
    dates = [(today - timedelta(days=d)).strftime("%Y-%m-%d") for d in range(2)]
    out = []
    for date_str in dates:
        path = f"signals/incoming/chatgpt_{date_str}.json"
        url = f"https://api.github.com/repos/{_SIGNALS_REPO}/contents/{path}"
        try:
            raw = _gh_json(url, token)
            data = json.loads(
                __import__("base64").b64decode(raw["content"]).decode("utf-8", "replace")
            )
        except (HTTPError, URLError, OSError, KeyError, json.JSONDecodeError):
            continue
        for f in data.get("findings", []):
            c = _finding_to_candidate(f)
            if c:
                c["score"] = 80
                out.append(c)
    return out[:limit]


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


# Реестр источников: name -> (fetch_func, auto_relevant)
# auto_relevant=True  — источник уже «по построению» про тему (arXiv AI, Grok, ChatGPT), фильтруем только exclude.
# auto_relevant=False — обычный новостной, применяем include_any фильтр (HN, Lobsters, RSS).
SOURCES = {
    "hn": (hn_fetch, False),
    "lob": (lob_fetch, False),
    "arxiv": (arxiv_fetch, True),
    "grok": (grok_fetch, True),
    "chatgpt": (chatgpt_fetch, True),
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
                       check=False, timeout=120)
        print(f"[s2c] состояние обновлено: {len(state['sent_ids'])} sent; collected={state['collected']}")
    except Exception as e:
        print(f"[s2c] не записал состояние: {type(e).__name__}")


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
    if m:
        return m.group(1).strip()
    m2 = re.compile(r'content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.I)
    m2 = m2.search(html)
    return m2.group(1).strip() if m2 else None


def worker_add(base_url: str, secret: str, draft: dict) -> tuple[bool, str]:
    body = json.dumps(draft, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/add", data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "X-Worker-Secret": secret, "User-Agent": _UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return bool(data.get("ok")), json.dumps(data, ensure_ascii=False)
    except HTTPError as e:
        return False, f"HTTP {e.code}"
    except (URLError, OSError) as e:
        return False, str(e)


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
    print(f"[s2c] профиль: {os.path.basename(str(PROFILE_FILE))}; уже отправлено: {len(sent_set)}")

    # 1–2. собрать + отфильтровать по каждому источнику
    buckets: dict[str, list[dict]] = {}
    for name, (fetch_fn, auto_relevant) in SOURCES.items():
        buckets.setdefault(name, [])
        try:
            items = fetch_fn(per_source, profile) or []
        except Exception as e:  # noqa: BLE001
            print(f"[s2c] {name}: ОШИБКА сбора — {type(e).__name__}: {e}")
            state["collected"][name] = now.isoformat()
            continue
        fresh = []
        for it in items:
            if it["id"] in sent_set:
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
    candidates = []
    order = list(buckets.keys())
    pos = {k: 0 for k in order}
    while len(candidates) < max_drafts:
        added = False
        for name in order:
            b = buckets[name]
            if pos[name] < len(b):
                candidates.append(b[pos[name]])
                pos[name] += 1
                added = True
                if len(candidates) >= max_drafts:
                    break
        if not added:
            break
    print(f"[s2c] ИТОГО кандидатов к генерации: {len(candidates)}")
    for c in candidates:
        print(f"  [sel] {c['id']} score={c.get('score')} {c['title']}")

    if dry:
        for c in candidates:
            print(f"  [dry] {c['id']} score={c['score']} {c['title']}")
        return 0

    if not worker_secret:
        print("[s2c] нет S2C_WORKER_SECRET (GH secret) — пропуск отправки")
        return 1

    new_sent = set(sent_set)
    for c in candidates:
        prompt = _EDITOR_PROMPT.format(title=c["title"], summary=(c["text"] or "нет"), source_url=(c["url"] or "не указан"))
        text = qwen_generate(prompt, qwen_model)
        if not text or text.strip().upper() == "SKIP":
            print(f"  [skip] {c['id']}: Qwen вернул SKIP/пусто")
            continue
        img = og_image(c["url"])
        draft = {"id": c["id"], "title": c["title"], "text": text, "image_url": img}
        ok, resp = worker_add(worker_url, worker_secret, draft)
        print(f"  [add] {draft['id']} ok={ok} resp={resp[:120]}")
        if ok:
            new_sent.add(c["id"])

    # 3. сохранить дедуп + таймстампы на ЯД (rclone сделает workflow / сам)
    # персистим нормализованный namespaced набор, чтобы состояние было чистым
    persisted = _normalize_sent(state.get("sent_ids", []))
    state["sent_ids"] = sorted(persisted | new_sent)
    print(f"[s2c] отправлено за прогон: {len(new_sent - sent_set)}; в состоянии всего: {len(state['sent_ids'])}")
    save_state_and_push(state, yandex_state)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"[s2c] FATAL: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
