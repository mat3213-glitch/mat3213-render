#!/usr/bin/env python3
"""
repo_scout.py — еженедельный наблюдатель GitHub: ищет репо для улучшения проекта.

Запуск на GitHub Actions (repo_scout.yml). Состояние (seen.json) коммитится
обратно в репо — дедуп переживает между прогонами.

Дайджест НОВЫХ репо шлётся в Telegram через CF Worker (как bot_service).

ENV (из GH secrets):
  CLOUDFLARE_WORKER   — база CF Worker (прокси к api.telegram.org)
  TELEGRAM_BOT_TOKEN  — токен бота
  ADMIN_CHAT_ID       — кому слать дайджест
  GITHUB_TOKEN        — для GitHub Search API (даёт сам Actions)
  GEMINI_API_KEY      — (опц.) объяснение «что автоматизирует / чем полезно».
                        Нет ключа или ошибка → fallback на описание репо.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from datetime import datetime
from pathlib import Path

import requests

HERE = Path(__file__).parent
QUERY_FILE = HERE / "repo_scout_queries.json"
SEEN_FILE = HERE / "repo_scout_seen.json"
REPORT_FILE = HERE / "repo_scout_latest.md"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# Короткий контекст проекта — чтобы LLM объяснял пользу именно ДЛЯ нас, а не вообще.
PROJECT_CONTEXT = (
    "Проект yaromat — автоматизация музыкального контента (Future Garage / Downtempo). "
    "Компоненты: нарезка клипов под BPM (ffmpeg + librosa, тяжёлое крутится на GitHub Actions), "
    "генерация артов и AI-видео/фото (Qwen, Hunyuan, Gemini/Imagen), автопостинг в "
    "Telegram/VK/OK/Pinterest/YouTube, Telegram-бот-админка. "
    "Локальное железо слабое (Atom, 1.8 ГБ RAM, без GPU) — всё тяжёлое выносим на GH Actions или в облако."
)

KEYWORDS = {
    "automation": ["automation", "workflow", "bot", "scheduler", "pipeline", "autopost", "scraper"],
    "social": ["social", "instagram", "tiktok", "twitter", "telegram", "pinterest"],
    "video": ["video", "shorts", "reels", "clip", "render", "ffmpeg"],
    "audio": ["audio", "music", "librosa", "beat", "mix", "master", "playlist"],
    "workflow": ["workflow", "github actions", "ci", "orchestrator"],
}

RELEVANCE_TERMS = [
    "video", "audio", "music", "ffmpeg", "render", "rendering", "clip", "clips",
    "reels", "shorts", "automation", "automate", "social", "telegram",
    "instagram", "tiktok", "youtube", "scheduler", "scheduling", "pipeline",
    "scraper", "scraping", "tempo", "onset", "playlist", "visualizer",
    "playwright", "autopost", "beat-sync", "music-video",
]
_REL_RE = re.compile(r"\b(" + "|".join(re.escape(t) for t in RELEVANCE_TERMS) + r")\b")

DEFAULT_QUERIES = [
    {"label": "ffmpeg python automation", "category": "video", "query": "ffmpeg python video automation OR pipeline"},
    {"label": "beat synced video", "category": "video", "query": "beat sync video OR music visualizer OR audio reactive"},
    {"label": "AI video generation", "category": "video", "query": "text to video OR image to video generation open source"},
    {"label": "reels shorts generator", "category": "video", "query": "shorts OR reels generator automation"},
    {"label": "audio beat detection", "category": "audio", "query": "beat detection OR onset OR tempo librosa OR aubio"},
    {"label": "social media scheduler", "category": "social", "query": "social media scheduler self-hosted autopost"},
    {"label": "playwright automation", "category": "automation", "query": "playwright automation scraper bot python"},
    {"label": "content pipeline", "category": "workflow", "query": "content pipeline orchestration automation"},
    {"label": "github actions media", "category": "workflow", "query": "github actions ffmpeg OR video render"},
    {"label": "telegram bot framework", "category": "community", "query": "telegram bot framework python media"},
    {"label": "video uniquization", "category": "video", "query": "video uniquify OR deduplication OR variation generator"},
]


def gh_headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
         "User-Agent": "yaromat-repo-scout"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def load_queries() -> list[dict]:
    if QUERY_FILE.exists():
        try:
            data = json.loads(QUERY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                return [q for q in data if isinstance(q, dict) and q.get("query")]
        except Exception:
            pass
    return DEFAULT_QUERIES


def load_seen() -> set[str]:
    if SEEN_FILE.exists():
        try:
            d = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
            if isinstance(d, list):
                return set(d)
        except Exception:
            pass
    return set()


def save_seen(seen: set[str]):
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=2, ensure_ascii=False), encoding="utf-8")


def search_github(query: str, per_page: int = 5) -> list[dict]:
    r = requests.get("https://api.github.com/search/repositories",
                     params={"q": query, "sort": "stars", "order": "desc", "per_page": per_page},
                     headers=gh_headers(), timeout=30)
    if r.status_code != 200:
        print(f"  search HTTP {r.status_code} for '{query[:40]}'")
        return []
    items = r.json().get("items", [])
    return items if isinstance(items, list) else []


def text_of(repo: dict) -> str:
    return " ".join(str(repo.get(k, "") or "") for k in ["name", "full_name", "description", "language"]).lower()


def categorize(repo: dict) -> str:
    t = text_of(repo)
    for cat, words in KEYWORDS.items():
        if any(w in t for w in words):
            return cat
    return "misc"


def is_relevant(repo: dict) -> bool:
    desc = str(repo.get("description") or "").lower()
    return bool(desc) and bool(_REL_RE.search(desc))


def score(repo: dict) -> float:
    stars = int(repo.get("stargazers_count") or 0)
    s = math.log10(stars + 1) * 10
    desc = str(repo.get("description") or "").lower()
    if any(k in desc for k in ["music", "audio", "video", "ffmpeg", "automation", "render", "social"]):
        s += 3
    if str(repo.get("updated_at") or "")[:4] == str(datetime.now().year):
        s += 1.5
    return round(s, 2)


def build_candidates(max_per_query: int = 5) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for q in load_queries():
        for repo in search_github(str(q["query"]).strip(), per_page=max_per_query):
            fn = str(repo.get("full_name") or "")
            if not fn or fn in seen or not is_relevant(repo):
                continue
            seen.add(fn)
            out.append({
                "full_name": fn,
                "html_url": repo.get("html_url", ""),
                "description": repo.get("description", ""),
                "language": repo.get("language", ""),
                "stars": int(repo.get("stargazers_count") or 0),
                "category": categorize(repo),
                "score": score(repo),
            })
    out.sort(key=lambda x: (x["score"], x["stars"]), reverse=True)
    return out


def _call_groq(prompt: str) -> str | None:
    """Groq (OpenAI-совместимый). Возвращает сырой текст ответа или None.
    На раннере GH Actions (не-RU IP) работает; локально из РФ может быть 403 (гео)."""
    if not GROQ_API_KEY:
        return None
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": GROQ_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.4,
                "response_format": {"type": "json_object"},
            },
            timeout=90,
        )
        if r.status_code == 200:
            print(f"[llm] обогащение через Groq ({GROQ_MODEL})")
            return r.json()["choices"][0]["message"]["content"].strip()
        print(f"[llm] Groq HTTP {r.status_code} — пробую Gemini")
    except Exception as e:
        print(f"[llm] Groq сеть ({e}) — пробую Gemini")
    return None


def _call_gemini(prompt: str) -> str | None:
    """Gemini-фолбэк. Цепочка моделей на случай 429 (квота) / 503 (перегрузка)."""
    if not GEMINI_API_KEY:
        return None
    models = [GEMINI_MODEL] + [m for m in ("gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest")
                               if m != GEMINI_MODEL]
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "response_mime_type": "application/json"},
    }
    for model in models:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent?key={GEMINI_API_KEY}")
        for attempt in range(2):  # 2 попытки: transient 503/429 часто проходят
            try:
                r = requests.post(url, json=payload, timeout=90)
            except Exception as e:
                print(f"[llm] Gemini {model}: сеть ({e})")
                break
            if r.status_code == 200:
                print(f"[llm] обогащение через Gemini ({model})")
                return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            if r.status_code in (429, 503) and attempt == 0:
                continue
            print(f"[llm] Gemini {model}: HTTP {r.status_code}")
            break
    return None


def enrich_with_llm(items: list[dict]) -> None:
    """Добавляет каждому репо поле 'blurb' — простой русский текст
    «что автоматизирует / чем полезно проекту». Одним батч-запросом к LLM.

    Провайдеры по приоритету: Groq (основной) → Gemini (фолбэк).
    Бесшумный fallback: нет ключей, ошибка сети, лимит или кривой ответ →
    blurb не выставляется, дайджест откатывается на описание. Скаут не падает.
    """
    if not items or not (GROQ_API_KEY or GEMINI_API_KEY):
        return

    catalog = [
        {
            "full_name": it["full_name"],
            "stars": it["stars"],
            "language": it.get("language") or "",
            "description": (it.get("description") or "")[:300],
        }
        for it in items
    ]
    prompt = (
        f"{PROJECT_CONTEXT}\n\n"
        "Ниже список GitHub-репозиториев (имя, звёзды, язык, описание). Описания бывают "
        "обрезанными или не на русском — додумай по названию и контексту.\n"
        "Для КАЖДОГО репо напиши короткое объяснение простым русским языком: "
        "(1) что он автоматизирует/делает, (2) чем конкретно может быть полезен нашему проекту "
        "(или честно: 'для проекта вряд ли пригодится — <почему>', если связи нет).\n"
        "2–3 предложения, без воды и маркетинга, без markdown.\n"
        "Верни СТРОГО JSON-массив объектов {\"full_name\":..., \"blurb\":...} в том же порядке, "
        "без обёрток и пояснений.\n\n"
        f"Репозитории:\n{json.dumps(catalog, ensure_ascii=False, indent=1)}"
    )

    # Groq основной, Gemini фолбэк. Первый, кто вернёт текст — побеждает.
    raw = _call_groq(prompt) or _call_gemini(prompt)
    if raw is None:
        print("[llm] все провайдеры недоступны — fallback на описания")
        return

    try:
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip()).strip()
        blurbs = json.loads(raw)
        by_name = {str(b.get("full_name")): str(b.get("blurb") or "").strip()
                   for b in blurbs if isinstance(b, dict)}
        n = 0
        for it in items:
            b = by_name.get(it["full_name"])
            if b:
                it["blurb"] = b
                n += 1
        print(f"[llm] обогащено {n}/{len(items)} репо")
    except Exception as e:
        print(f"[llm] разбор ответа не удался ({e}) — fallback на описания")


def build_digest(new_items: list[dict], total: int) -> str:
    if not new_items:
        return ""
    lines = [f"🔭 GitHub scout: {len(new_items)} новых репо для проекта\n"]
    for it in new_items[:12]:
        lines.append(f"⭐ {it['stars']}  {it['full_name']}")
        blurb = it.get("blurb") or (it.get("description") or "")[:140]
        if blurb:
            lines.append(f"{blurb}")
        lines.append(f"{it['html_url']}\n")
    return "\n".join(lines)


def write_report(items: list[dict]):
    lines = [f"# Repo Scout — {datetime.now().isoformat()}", "", f"Всего в шортлисте: {len(items)}", ""]
    for it in items:
        lines += [f"- **{it['full_name']}** ⭐{it['stars']} [{it['category']}]",
                  f"  - {it['html_url']}"]
        if it.get("blurb"):
            lines.append(f"  - 💡 {it['blurb']}")
        lines.append(f"  - 📄 {(it.get('description') or '')[:160]}")
    REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")


def send_tg(text: str):
    if not text:
        return
    worker = os.environ.get("CLOUDFLARE_WORKER", "")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("SCOUT_CHAT_ID", "")
    thread = os.environ.get("SCOUT_THREAD_ID", "")
    if not token or not chat:
        print("[tg] нет TELEGRAM_BOT_TOKEN/SCOUT_CHAT_ID — печатаю:")
        print(text)
        return
    payload = {"chat_id": chat, "text": text[:3900], "disable_web_page_preview": True}
    if thread:
        payload["message_thread_id"] = int(thread)
    try:
        r = requests.post(f"{worker}/bot{token}/sendMessage", json=payload, timeout=30)
        print(f"[tg] sendMessage HTTP {r.status_code} → chat={chat} thread={thread}")
    except Exception as e:
        print(f"[tg] ошибка: {e}")
        print(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--seed", action="store_true", help="Пометить текущее виденным без дайджеста")
    args = ap.parse_args()

    items = build_candidates()[: max(1, args.top)]
    enrich_with_llm(items)
    write_report(items)

    seen = load_seen()
    new_items = [it for it in items if it["full_name"] not in seen]
    seen.update(it["full_name"] for it in items)
    save_seen(seen)

    if args.seed:
        print(f"🌱 seed: {len(items)} репо помечены виденными")
        return

    digest = build_digest(new_items, len(items))
    if digest:
        print(digest)
        send_tg(digest)
    else:
        print(f"новых репо нет (просканировано {len(items)})")


if __name__ == "__main__":
    main()
