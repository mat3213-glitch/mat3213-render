#!/usr/bin/env python3
"""s2c_daily.py — автономный цикл Signal-to-Channel на GH-раннере.

Поток (полностью на раннере, бук не участвует):
  1. собрать свежие сигналы Hacker News (открытый Firebase API, без ключа),
  2. отфильтровать по профилю канала (s2c_channel_profile.json),
  3. отсечь уже отправленные (дедуп по item_id, состояние на ЯД),
  4. для каждого отобранного — сгенерить авторский рус. пост через Qwen
     (qwen/qwen_chat.py --stdin, тот же раннер, US-IP), SKIP если нерелевантно/мало данных,
  5. отправить через Cloudflare Worker POST /add (X-Worker-Secret) →
     модерация с кнопками в ЛС владельца → публикация в канале (Worker+KV).

Секреты/токены — только через env (GH secrets), в логи — только имена.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError

HERE = Path(__file__).resolve().parent
PROFILE_FILE = HERE / "s2c_channel_profile.json"
QWEN_CHAT = HERE / "qwen" / "qwen_chat.py"

# Публичный URL Worker (не секрет). Переопределяется env S2C_WORKER_URL.
DEFAULT_WORKER_URL = "https://s2c-moderation-1.mat3213.workers.dev"
# Путь к файлу состояния (дедуп) на ЯД (pull/push через rclone в workflow).
DEFAULT_YD_STATE = "Content factory/cloud_io/s2c/state.json"

HN_BASE = "https://hacker-news.firebaseio.com/v0"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) signal-to-channel/1.0"

# Отправной редакторский промпт (голос «ИИшницы») — тот же, что в SignalToChannel writer.py.
_EDITOR_PROMPT = """Ты редактор русскоязычного Telegram-канала «ИИшница» про нейросети, AI-инструменты, роботов и технологии.
Сделай самостоятельный короткий пост по публичному сигналу. Пиши как живой наблюдательный редактор, а не как пресс-релиз или нейросеть.

Обязательные правила:
* 4–6 коротких абзацев с пустой строкой между ними; никаких стен текста.
* Начни с конкретной новости или пользы. Допускается 1–2 ироничных, бытовых, очень конкретных сравнения.
* Закончи отдельным абзацем с выводом: зачем новость нужна обычному читателю, что она меняет или что за ней наблюдать.
* Не используй штампы «революционный», «заслуживает внимания», «в эпоху», «это не просто».
* Не добавляй фактов, которых нет в исходнике. Не обещай доходность и не давай инвестиционных советов.
* Если данных мало или тема не относится к ИИ/роботам/технологиям, ответь ровно: SKIP.
* Последняя строка: «Источник: <ссылка>».
* Перед ответом молча проверь: мысль закончена, есть вывод, есть ссылка.

Заголовок сигнала: {title}
Описание: {summary}
Источник: {source_url}"""

_CITATION_RE = re.compile(r"\[\[\d+\]\]|\[\d+\]")


def _load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _http_json(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 fixed Host names
        return json.loads(resp.read().decode("utf-8"))


def hn_top_ids(limit: int) -> list[int]:
    ids = _http_json(f"{HN_BASE}/topstories.json")
    return [int(i) for i in ids[:limit]]


def hn_item(item_id: int) -> dict | None:
    try:
        item = _http_json(f"{HN_BASE}/item/{item_id}.json")
    except (HTTPError, URLError, OSError):
        return None
    if not item or item.get("type") != "story":
        return None
    title = str(item.get("title") or "").strip()
    url = str(item.get("url") or "").strip()
    if not title or not url.startswith("http"):
        return None
    return {
        "item_id": int(item_id),
        "title": title,
        "url": url,
        "score": item.get("score") or 0,
        "text": str(item.get("text") or "").strip() or None,
    }


def _domain(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return (urlparse(url).netloc or "").lower().lstrip("www.")
    except Exception:
        return ""


def relevant(item: dict, profile: dict) -> bool:
    hay = f"{item['title']} {_domain(item['url'])}".lower()
    inc = [w.lower() for w in profile.get("include_any", [])]
    exc = [w.lower() for w in profile.get("exclude_any", [])]
    if any(e in hay for e in exc):
        return False
    return any(w in hay for w in inc)


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
    hn_limit = int(os.getenv("HN_TOP", "60"))
    max_drafts = int(os.getenv("S2C_MAX_DRAFTS", "1"))
    yandex_state = os.getenv("S2C_YD_STATE", DEFAULT_YD_STATE)

    profile = _load_json(PROFILE_FILE) if PROFILE_FILE.exists() else {"include_any": [], "exclude_any": []}

    # дедуп: откуда уже отправляли
    sent: list = []
    state_path = Path("s2c_state.json")
    if state_path.exists():
        try:
            sent = _load_json(state_path).get("sent_ids", [])
        except Exception:
            sent = []
    sent_set = set(sent)
    print(f"[s2c] профиль: {os.path.basename(str(PROFILE_FILE))}; уже отправлено: {len(sent_set)}")

    # 1–2. собрать + отфильтровать
    top = hn_top_ids(hn_limit)
    candidates = []
    for item_id in top:
        it = hn_item(item_id)
        if it and relevant(it, profile) and it["item_id"] not in sent_set:
            candidates.append(it)
    print(f"[s2c] HN top {len(top)} → релевантных новых: {len(candidates)}")
    candidates.sort(key=lambda x: x["score"], reverse=True)
    candidates = candidates[:max_drafts]

    if dry:
        for c in candidates:
            print(f"  [dry] id={c['item_id']} score={c['score']} {c['title']}")
        return 0

    if not worker_secret:
        print("[s2c] нет S2C_WORKER_SECRET (GH secret) — пропуск отправки")
        return 1

    new_sent = list(sent_set)
    for c in candidates:
        prompt = _EDITOR_PROMPT.format(title=c["title"], summary=(c["text"] or "нет"), source_url=(c["url"] or "не указан"))
        text = qwen_generate(prompt, qwen_model)
        if not text or text.strip().upper() == "SKIP":
            print(f"  [skip] id={c['item_id']}: Qwen вернул SKIP/пусто")
            continue
        img = og_image(c["url"])
        draft = {"id": f"hn:{c['item_id']}", "title": c["title"], "text": text, "image_url": img}
        ok, resp = worker_add(worker_url, worker_secret, draft)
        print(f"  [add] id={draft['id']} ok={ok} resp={resp[:120]}")
        if ok:
            new_sent.append(int(c["item_id"]))

    # 6. сохранить дедуп на ЯД (rclone сделает workflow / сам)
    if new_sent != sent_set:
        try:
            with open("s2c_state.json", "w", encoding="utf-8") as fh:
                json.dump({"sent_ids": new_sent}, fh, ensure_ascii=False)
            subprocess.run(["rclone", "copyto", "s2c_state.json", f"ydrive:{yandex_state}"], check=False, timeout=120)
            print(f"[s2c] состояние обновлено: {len(new_sent)} id")
        except Exception as e:
            print(f"[s2c] не записал состояние: {type(e).__name__}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"[s2c] FATAL: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
