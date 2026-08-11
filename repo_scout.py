#!/usr/bin/env python3
"""
repo_scout.py — еженедельный наблюдатель GitHub: ищет репо для улучшения проекта.

Запуск на GitHub Actions (repo_scout.yml). ``seen.json`` остаётся совместимым
журналом показов, а явные решения проекта живут в ``repo_scout_ledger.json``.

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
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# github.com/trending — какие языки скрейпить как 2-й источник ("" = все языки)
TRENDING_LANGS = ["python", "typescript", ""]

HERE = Path(__file__).parent
NEEDS_FILE = HERE / "repo_scout_current_needs.v1.json"
SEEN_FILE = HERE / "repo_scout_seen.json"
REPORT_FILE = HERE / "repo_scout_latest.md"
NOVELTY_FILE = HERE / "repo_scout_novelty.json"   # тексты показанных находок (гейт «то же другими словами»)
LEDGER_FILE = HERE / "repo_scout_ledger.json"     # adopted/rejected/park/pilot — source of truth решений

from scout_filters import NoveltyIndex, is_saturated   # noqa: E402
from scout_ledger import canonical_name, load_excluded_names   # noqa: E402
from scout_needs import CurrentNeeds   # noqa: E402

# Жёсткий потолок на один current-need/category в готовом шортлисте — включая добивку.
# Не даёт даже полезному gap вытеснить остальные актуальные потребности.
CAT_CAP: dict[str, int] = {}
DEFAULT_CAT_CAP = 6

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# Короткий контекст проекта — чтобы LLM объяснял пользу именно ДЛЯ нас, а не вообще.
PROJECT_CONTEXT = (
    "Проект yaromat — автоматизация музыкального контента (Future Garage / Downtempo). "
    "Компоненты: нарезка клипов под BPM (ffmpeg + librosa, тяжёлое крутится на GitHub Actions), "
    "генерация артов и AI-видео/фото (Qwen, LTX, Gemini/Imagen), автопостинг в "
    "Telegram/VK/OK/Pinterest/YouTube, Telegram-бот-админка. "
    "Локальное железо слабое (Atom, 1.8 ГБ RAM, без GPU) — всё тяжёлое выносим на GH Actions или в облако."
)

def gh_headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
         "User-Agent": "yaromat-repo-scout"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def load_queries(needs: CurrentNeeds | None = None) -> list[dict]:
    """Current-needs config is authoritative; legacy queries are rollback-only."""
    return (needs or CurrentNeeds(NEEDS_FILE)).queries()


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


def axis_for_today() -> tuple[str, str]:
    """3-суточный цикл ОСИ поиска — антидот «hall-of-fame replay» (см. аудит 2026-07-24).
    Возвращает (sort, qualifier): классика по звёздам / недавно активные / новорождённые."""
    mode = datetime.now(timezone.utc).toordinal() % 3
    today = datetime.now(timezone.utc).date()
    if mode == 0:
        # «витрина по звёздам», НО живая: только пушенные за ~полгода (не мёртвые музеи)
        return "stars", f"pushed:>{(today - timedelta(days=180)).isoformat()}"
    if mode == 1:
        return "updated", f"pushed:>{(today - timedelta(days=30)).isoformat()}"  # живые
    return "", f"created:>{(today - timedelta(days=90)).isoformat()}"  # свежесозданные (best-match)


def search_github(query: str, per_page: int = 8, sort: str = "stars", qualifier: str = "") -> list[dict]:
    q = f"{query} {qualifier}".strip()
    params = {"q": q, "order": "desc", "per_page": per_page}
    if sort:
        params["sort"] = sort  # пустой sort = best-match (relevance)
    r = requests.get("https://api.github.com/search/repositories",
                     params=params, headers=gh_headers(), timeout=30)
    if r.status_code in (403, 429):
        # вторичный rate-limit GitHub Search → подождать и повторить один раз
        time.sleep(8)
        r = requests.get("https://api.github.com/search/repositories",
                         params=params, headers=gh_headers(), timeout=30)
    if r.status_code != 200:
        print(f"  search HTTP {r.status_code} for '{q[:40]}'")
        return []
    items = r.json().get("items", [])
    return items if isinstance(items, list) else []


def fetch_trending(languages: list[str], since: str = "daily") -> list[dict]:
    """2-й источник — скрейп github.com/trending (ось velocity: звёзды за период, не абсолютные).
    Регэкс-парсинг сверен с живой разметкой 2026-07-24. Не падает — язык с ошибкой пропускается."""
    seen, results = set(), []
    headers = {"User-Agent": "Mozilla/5.0"}
    for lang in languages:
        path = f"/{lang}" if lang else ""
        try:
            resp = requests.get(f"https://github.com/trending{path}?since={since}", headers=headers, timeout=30)
            if resp.status_code != 200:
                continue
        except Exception:
            continue
        for b in re.split(r'<article\s+class="Box-row">', resp.text)[1:]:
            m = re.search(r'<h2[^>]*>\s*<a[^>]*href="(/[^"]+)"', b, re.DOTALL)
            if not m:
                continue
            parts = m.group(1).strip("/").split("/")
            if len(parts) != 2:
                continue
            full_name = f"{parts[0]}/{parts[1]}"
            if full_name in seen:
                continue
            seen.add(full_name)
            dm = re.search(r'<p[^>]*class="col-9[^"]*"[^>]*>(.*?)</p>', b, re.DOTALL)
            desc = re.sub(r"<[^>]+>", "", dm.group(1)).strip() if dm else ""
            sm = re.search(r'href="/[^"]+/stargazers"[^>]*>\s*(?:<[^>]*>\s*)*([\d,]+)', b, re.DOTALL)
            stars = int(sm.group(1).replace(",", "")) if sm else 0
            pm = re.search(r'>\s*([\d,]+)\s+stars?\s+(?:today|this week|this month)\s*<', b, re.IGNORECASE)
            period = int(pm.group(1).replace(",", "")) if pm else 0
            results.append({"full_name": full_name, "html_url": f"https://github.com/{full_name}",
                            "description": desc, "language": lang or "", "stars": stars,
                            "period_stars": period, "source": "trending"})
    return results


def velocity_score(stars: int, period_stars: int, pushed_at: str) -> float:
    """Ранжирование с упором на свежесть/velocity, а НЕ на абсолютные звёзды (ядро mimo)."""
    base = math.log10(stars + 1) * 4          # звёзды — слабый вклад
    vel = math.log10(period_stars + 1) * 8    # звёзды за период — сильный (velocity)
    rec = 0.0
    if pushed_at:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))).days
            rec = max(0.0, 15.0 - age / 7.0)  # свежий push до +15, ~14 недель до нуля
        except Exception:
            rec = 0.0
    mid = 4.0 if 50 <= stars <= 3000 else 0.0  # bonus mid-star (не музей, не пусто)
    return round(base + vel + rec + mid, 2)


def diversify(items: list[dict], total: int = 25, per_cat: int = 4) -> list[dict]:
    """Round-robin по category с ЖЁСТКИМ капом per_cat на категорию — вместо глобального topN."""
    if not items:
        return []
    buckets: dict[str, deque] = defaultdict(deque)
    for it in items:
        buckets[it.get("category", "misc")].append(it)
    for cat in buckets:
        buckets[cat] = deque(sorted(buckets[cat], key=lambda x: x.get("score", 0), reverse=True))
    taken: dict[str, int] = defaultdict(int)
    result: list[dict] = []
    progress = True
    while len(result) < total and progress:
        progress = False
        for cat, q in buckets.items():
            if len(result) >= total:
                break
            if q and taken[cat] < min(per_cat, CAT_CAP.get(cat, DEFAULT_CAT_CAP)):
                result.append(q.popleft())
                taken[cat] += 1
                progress = True
    # backfill: добираем лучшим из остатка, НО с тем же потолком на категорию. Раньше добивка
    # шла без ограничений и одна категория съедала весь дайджест (замер 07.08: 21 из 25).
    # Короткий честный список лучше двадцати пяти позиций с пометкой «вряд ли пригодится».
    if len(result) < total:
        leftover = sorted((it for q in buckets.values() for it in q),
                          key=lambda x: x.get("score", 0), reverse=True)
        for it in leftover:
            if len(result) >= total:
                break
            cat = it.get("category", "misc")
            if taken[cat] >= CAT_CAP.get(cat, DEFAULT_CAT_CAP):
                continue
            result.append(it)
            taken[cat] += 1
    return result


def build_candidates(max_per_query: int = 8, excluded_names: set[str] | None = None,
                     needs: CurrentNeeds | None = None) -> list[dict]:
    """Два источника: Search API (ось ротируется по дню) + github.com/trending (ось velocity).
    Скоринг — velocity_score (звёзды слабо, свежесть/период сильно). Дедуп: выше score побеждает."""
    by_name: dict[str, dict] = {}
    sort, qualifier = axis_for_today()
    print(f"[scout] ось дня: sort='{sort or 'best-match'}' qualifier='{qualifier or '-'}'")

    excluded_names = {canonical_name(x) for x in (excluded_names or set())}
    needs = needs or CurrentNeeds(NEEDS_FILE)
    dropped = {"saturated": 0, "lifecycle": 0, "needs": 0}

    def consider(cand: dict):
        fn = cand["full_name"]
        # Самый ранний lifecycle-гейт. Решённые и уже показанные репо не попадают
        # в by_name, следовательно физически не могут дойти до diversify/LLM/latest.
        if canonical_name(fn) in excluded_names:
            dropped["lifecycle"] += 1
            return
        assessment = needs.assess(cand)
        if not assessment["accepted"]:
            dropped["needs"] += 1
            return
        # тема, которой проект уже закрыт (свой пул воркеров/оркестратор) и без признаков ремесла
        if is_saturated(f"{fn} {cand.get('description', '')}"):
            dropped["saturated"] += 1
            return
        cand.update({k: v for k, v in assessment.items() if k != "accepted"})
        cand["velocity_score"] = cand.get("score", 0)
        # Need coverage is the main ranking axis. Velocity is merely a small tie-breaker.
        cand["score"] = round(assessment["need_score"] + cand["velocity_score"] * 0.1, 2)
        old = by_name.get(fn)
        if old is None or cand["score"] > old["score"]:
            # при перезаписи сохраняем осмысленную категорию, если у победителя она "misc"
            if old is not None and cand.get("category") == "misc" and old.get("category") != "misc":
                cand["category"] = old["category"]
            by_name[fn] = cand

    # источник 1 — Search API, категория берётся из ЗАПРОСА (не keyword-угадайка)
    for q in load_queries(needs):
        time.sleep(2.5)  # антидот вторичному rate-limit Search API
        cat = str(q.get("category") or "misc")
        for repo in search_github(str(q["query"]).strip(), per_page=max_per_query,
                                  sort=sort, qualifier=qualifier):
            fn = str(repo.get("full_name") or "")
            if not fn:
                continue
            stars = int(repo.get("stargazers_count") or 0)
            consider({
                "full_name": fn,
                "html_url": repo.get("html_url", ""),
                "description": repo.get("description", ""),
                "language": repo.get("language", ""),
                "topics": repo.get("topics") or [],
                "stars": stars,
                "category": cat,
                "score": velocity_score(stars, 0, str(repo.get("pushed_at") or "")),
                "source": "search",
            })

    # источник 2 — trending (velocity), фильтр релевантности + категория по ключевым словам
    for repo in fetch_trending(TRENDING_LANGS, since="daily"):
        consider({
            "full_name": repo["full_name"],
            "html_url": repo["html_url"],
            "description": repo["description"],
            "language": repo["language"],
            "stars": repo["stars"],
            "category": "misc",  # CurrentNeeds overwrites this after evidence matching.
            "score": velocity_score(repo["stars"], repo["period_stars"], ""),
            "source": "trending",
        })

    out = list(by_name.values())
    out.sort(key=lambda x: (x["score"], x["stars"]), reverse=True)
    print(f"[scout] отсеяно как насыщенная тема (шлюзы/агенты/пентест): {dropped['saturated']}")
    print(f"[scout] отсеяно ledger/seen до shortlist+LLM: {dropped['lifecycle']}")
    print(f"[scout] отсеяно current-needs/evidence до shortlist+LLM: {dropped['needs']}")
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
            "need": it.get("need_id"),
            "gap": it.get("gap"),
            "evidence": it.get("evidence"),
            "integration_cost": it.get("integration_cost"),
            "duplicate_risk": it.get("duplicate_risk"),
        }
        for it in items
    ]
    prompt = (
        f"{PROJECT_CONTEXT}\n\n"
        "Ниже список GitHub-репозиториев, уже прошедших детерминированный current-needs gate. "
        "Не додумывай функции, которых нет в переданных evidence/описании.\n"
        "Для КАЖДОГО репо напиши короткое объяснение простым русским языком: "
        "(1) что он делает по evidence, (2) как закрывает указанный gap, "
        "(3) оправдана ли цена интеграции с учётом duplicate-risk.\n"
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
        lines.append(f"🎯 gap: {it.get('need_id')} — {it.get('gap')}")
        lines.append(f"🔎 evidence: {', '.join(it.get('evidence') or [])}")
        lines.append(f"🧩 интеграция: {it.get('integration_cost')} · дубль-риск: {it.get('duplicate_risk')}")
        blurb = it.get("blurb") or (it.get("description") or "")[:140]
        if blurb:
            lines.append(f"{blurb}")
        lines.append(f"{it['html_url']}\n")
    return "\n".join(lines)


def write_report(items: list[dict]):
    lines = [f"# Repo Scout — {datetime.now().isoformat()}", "", f"Всего в шортлисте: {len(items)}", ""]
    for it in items:
        lines += [f"- **{it['full_name']}** ⭐{it['stars']} [{it['category']}]",
                  f"  - {it['html_url']}",
                  f"  - 🎯 gap `{it.get('need_id')}` (priority {it.get('priority')}): {it.get('gap')}",
                  f"  - 🔎 evidence: {', '.join(it.get('evidence') or [])}",
                  f"  - 🧩 integration cost: {it.get('integration_cost')}",
                  f"  - ♻ duplicate-risk: {it.get('duplicate_risk')}"]
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

    ledger, excluded = load_excluded_names(LEDGER_FILE, SEEN_FILE)
    needs = CurrentNeeds(NEEDS_FILE)
    by_status: dict[str, int] = defaultdict(int)
    for entry in ledger.repos.values():
        by_status[entry["status"]] += 1
    print(f"[scout] ledger={len(ledger.repos)} {dict(sorted(by_status.items()))}; "
          f"legacy seen={len(load_seen())}; deny-set={len(excluded)}")

    print(f"[scout] current-needs v1: {len(needs.needs)} gaps, {len(needs.queries())} queries")
    candidates = build_candidates(excluded_names=excluded, needs=needs)
    items = diversify(candidates, total=max(1, args.top), per_cat=4)
    print(f"[scout] кандидатов {len(candidates)} → шортлист {len(items)} (round-robin по категориям)")

    seen = load_seen()
    seen.update(it["full_name"] for it in items)
    save_seen(seen)

    if args.seed:
        write_report([])
        print(f"🌱 seed: {len(items)} репо помечены виденными")
        return

    # Гейт новизны: seen.json ловит ТО ЖЕ ИМЯ, а этот — ТУ ЖЕ СУТЬ под новым именем
    # (замер 07.08 на истории грок-скаута: пары «то же другими словами» дают 0.53–0.82,
    # разные темы — ниже 0.35, поэтому порог 0.5).
    nov = NoveltyIndex(NOVELTY_FILE, threshold=0.5)
    fresh, stale = [], []
    for it in items:
        text = f"{it['full_name']} {it.get('description') or ''}"
        ok, sim, who = nov.is_novel(text)
        if ok:
            fresh.append(it)
            nov.add(it["full_name"], text)
        else:
            stale.append((it["full_name"], round(sim, 2), who))
    nov.save()
    if stale:
        print(f"[новизна] отсеяно как «уже было другими словами»: {len(stale)}")
        for fn, sim, who in stale[:8]:
            print(f"    {fn} ~{sim} ≈ {who}")
    items = fresh

    # LLM и latest.md получают только прошедшие ОБА ранних гейта: lifecycle/name
    # и смысловую новизну. latest.md — представление текущего запуска, не состояние.
    enrich_with_llm(items)
    write_report(items)

    digest = build_digest(items, len(items))
    if digest:
        print(digest)
        send_tg(digest)
    else:
        print(f"новых репо нет (просканировано {len(items)})")


if __name__ == "__main__":
    main()
