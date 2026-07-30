#!/usr/bin/env python3
"""Чистый HTTP-воркер для Kimi Chat без браузера.

На ноутбуке всего 1.8 ГБ RAM — Chromium вызывает thrashing свопа.
Сессия один раз снимается auth.py (Playwright storage_state) и живёт ~90 дней
(refresh_token). Этот скрипт ходит только по urllib: обновляет access_token
и стримит Connect-фреймы ChatService.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator, Optional

CHROME_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
REFRESH_URL = "https://auth.kimi.com/api/account.gateway.v1.AuthService/RefreshToken"
CHAT_URL = "https://www.kimi.com/apiv2/kimi.gateway.chat.v1.ChatService/Chat"
SCENARIO = "SCENARIO_K2D5"
ACCESS_CACHE_NAME = ".kimi_access.json"
# CJK Unified Ideographs + Ext A/B ranges commonly seen in leaks
_CJK_RE = re.compile(
    r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF"
    r"\U00020000-\U0002A6DF\U0002A700-\U0002B73F"
    r"\U0002B740-\U0002B81F\U0002B820-\U0002CEAF]+"
)


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def default_session_path() -> Path:
    return Path(__file__).resolve().parent / "kimi_session.json"


def access_cache_path(session_path: Path) -> Path:
    return session_path.resolve().parent / ACCESS_CACHE_NAME


def load_refresh_token(session_path: Path) -> str:
    if not session_path.is_file():
        eprint(f"Файл сессии не найден: {session_path}")
        sys.exit(2)
    try:
        data = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        eprint(f"Не удалось прочитать сессию: {exc}")
        sys.exit(2)

    for origin in data.get("origins") or []:
        for item in origin.get("localStorage") or []:
            if item.get("name") == "refresh_token":
                value = (item.get("value") or "").strip()
                if value:
                    return value
    eprint("В сессии отсутствует refresh_token (origins[].localStorage)")
    sys.exit(2)


def b64url_decode(segment: str) -> bytes:
    pad = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + pad)


def jwt_exp(token: str) -> Optional[int]:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = json.loads(b64url_decode(parts[1]).decode("utf-8"))
        exp = payload.get("exp")
        return int(exp) if exp is not None else None
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return None


def load_cached_access(cache_path: Path) -> Optional[str]:
    if not cache_path.is_file():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        token = data.get("token") or ""
        exp = data.get("exp")
        if not token or exp is None:
            return None
        if int(exp) - time.time() > 60:
            return str(token)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return None


def save_access_cache(cache_path: Path, token: str) -> None:
    exp = jwt_exp(token)
    if exp is None:
        # access_token ~15 min; fallback so we still cache briefly
        exp = int(time.time()) + 14 * 60
    payload = {"token": token, "exp": exp}
    # Атомарно: fanout зовёт драйвер параллельно (2 потока), и прямая запись даёт
    # шанс прочитать половину файла — ровно грабля «гонка за общий файл»
    # из reference_known_pitfalls. Пишем во временный рядом и подменяем rename'ом.
    try:
        tmp = cache_path.with_suffix(cache_path.suffix + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, cache_path)
    except OSError as exc:
        eprint(f"Не удалось записать кэш access_token: {exc}")


def refresh_access_token(refresh_token: str, timeout: float) -> str:
    body = json.dumps({"refresh_token": refresh_token}).encode("utf-8")
    req = urllib.request.Request(
        REFRESH_URL,
        data=body,
        method="POST",
        headers={
            "content-type": "application/json",
            "connect-protocol-version": "1",
            "user-agent": CHROME_UA,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        eprint(f"Обновление токена не удалось (HTTP {exc.code}): {err_body[:300]}")
        sys.exit(3)
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            eprint("Сетевой таймаут при обновлении токена")
        else:
            eprint(f"Сетевая ошибка при обновлении токена: {reason}")
        sys.exit(3)
    except TimeoutError:
        eprint("Сетевой таймаут при обновлении токена")
        sys.exit(3)

    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        eprint("Обновление токена: некорректный JSON в ответе")
        sys.exit(3)

    token = data.get("accessToken") or data.get("access_token")
    if not token:
        eprint(f"Обновление токена не удалось: нет accessToken (HTTP {status})")
        sys.exit(3)
    return str(token)


def get_access_token(session_path: Path, timeout: float, force_refresh: bool = False) -> str:
    cache = access_cache_path(session_path)
    if not force_refresh:
        cached = load_cached_access(cache)
        if cached:
            return cached
    rt = load_refresh_token(session_path)
    token = refresh_access_token(rt, timeout)
    save_access_cache(cache, token)
    return token


def connect_frame(payload: bytes, flag: int = 0) -> bytes:
    return bytes([flag]) + len(payload).to_bytes(4, "big") + payload


def build_chat_body(prompt: str, thinking: bool) -> bytes:
    obj: dict[str, Any] = {
        "scenario": SCENARIO,
        "tools": [],
        "message": {
            "role": "user",
            "blocks": [
                {
                    "message_id": "",
                    "text": {"content": prompt},
                }
            ],
            "scenario": SCENARIO,
            "is_goal": False,
        },
        "options": {
            "thinking": thinking,
            "enable_plugin": False,
            "reasoning_effort": "REASONING_EFFORT_LOW",
        },
        "project_id": "",
    }
    return connect_frame(json.dumps(obj, ensure_ascii=False).encode("utf-8"), flag=0)


def iter_connect_frames(stream: Any) -> Iterator[tuple[int, bytes]]:
    """Parse Connect frames: [1 byte flag][4 byte BE length][payload]."""
    buf = b""
    while True:
        chunk = stream.read(8192)
        if not chunk:
            break
        buf += chunk
        while True:
            if len(buf) < 5:
                break
            flag = buf[0]
            length = int.from_bytes(buf[1:5], "big")
            if len(buf) < 5 + length:
                break
            payload = buf[5 : 5 + length]
            buf = buf[5 + length :]
            yield flag, payload


class KimiRefused(RuntimeError):
    """Kimi отказал по своей причине (перегрузка, лимит) — это НЕ пустой ответ."""


def check_event_exception(event: dict[str, Any]) -> None:
    """Поймать отказ сервиса, спрятанный в потоке.

    29.07: на длинных запросах Kimi присылает block.exception с
    REASON_COMPLETION_OVERLOADED («System is currently busy»), текста при этом нет.
    Драйвер молча отдавал «пустой ответ модели», и это было неотличимо от бага
    парсера — час ушёл на диагностику не той гипотезы. Показываем причину как есть.
    """
    block = event.get("block")
    if not isinstance(block, dict):
        return
    exc = block.get("exception")
    if not isinstance(exc, dict):
        return
    err = exc.get("error") or {}
    reason = err.get("reason") or "UNKNOWN"
    msg = ((err.get("localizedMessage") or {}).get("message") or "").strip()
    raise KimiRefused(f"{reason}: {msg}" if msg else reason)


def extract_text_from_event(event: dict[str, Any]) -> str:
    block = event.get("block")
    if not isinstance(block, dict):
        return ""
    text = block.get("text")
    if not isinstance(text, dict):
        return ""
    if "content" in text and text["content"] is not None:
        return str(text["content"])
    if "delta" in text and text["delta"] is not None:
        return str(text["delta"])
    return ""


def strip_cjk(text: str) -> str:
    return _CJK_RE.sub("", text)


def chat_once(
    access_token: str,
    prompt: str,
    timeout: float,
    thinking: bool,
) -> tuple[str, int, Optional[int]]:
    """Returns (answer, event_count, http_error_code_or_None)."""
    body = build_chat_body(prompt, thinking)
    req = urllib.request.Request(
        CHAT_URL,
        data=body,
        method="POST",
        headers={
            "content-type": "application/connect+json",
            "connect-protocol-version": "1",
            "authorization": f"Bearer {access_token}",
            "x-msh-platform": "web",
            "x-language": "en-US",
            "origin": "https://www.kimi.com",
            "referer": "https://www.kimi.com/",
            "user-agent": CHROME_UA,
        },
    )
    pieces: list[str] = []
    events = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for _flag, payload in iter_connect_frames(resp):
                if not payload:
                    continue
                try:
                    event = json.loads(payload.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                events += 1
                check_event_exception(event)     # отказ сервиса → наружу с причиной
                piece = extract_text_from_event(event)
                if piece:
                    pieces.append(piece)
    except urllib.error.HTTPError as exc:
        return "", events, int(exc.code)
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            eprint("Сетевой таймаут при запросе к чату")
        else:
            eprint(f"Сетевая ошибка при запросе к чату: {reason}")
        sys.exit(3)
    except TimeoutError:
        eprint("Сетевой таймаут при запросе к чату")
        sys.exit(3)

    return "".join(pieces), events, None


def run_chat(
    prompt: str,
    session_path: Path,
    timeout: float,
    thinking: bool,
) -> tuple[str, int]:
    token = get_access_token(session_path, timeout, force_refresh=False)
    # Перегрузка у Kimi транзиентна: та же задача через паузу проходит. Три попытки
    # с нарастающей паузой дешевле, чем отдавать оркестратору пустоту.
    for attempt in range(3):
        try:
            answer, events, err = chat_once(token, prompt, timeout, thinking)
            break
        except KimiRefused as refusal:
            if "OVERLOADED" not in str(refusal) or attempt == 2:
                eprint(f"Kimi отказал: {refusal}")
                sys.exit(3)
            pause = 10 * (attempt + 1)
            eprint(f"Kimi занят ({refusal}) — повтор через {pause}с "
                   f"[попытка {attempt + 2} из 3]")
            time.sleep(pause)
    if err in (401, 403):
        eprint(f"Чат вернул HTTP {err}, обновляю токен и повторяю…")
        token = get_access_token(session_path, timeout, force_refresh=True)
        answer, events, err = chat_once(token, prompt, timeout, thinking)
        if err is not None:
            eprint(f"Повтор после обновления токена не удался (HTTP {err})")
            sys.exit(3)
    elif err is not None:
        eprint(f"Ошибка чата (HTTP {err})")
        sys.exit(3)
    return strip_cjk(answer), events


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="kimi_chat.py",
        description="Headless HTTP-драйвер Kimi Chat (stdlib only)",
    )
    p.add_argument("prompt", help="Текст запроса к модели")
    p.add_argument(
        "--session",
        type=Path,
        default=None,
        help="Путь к Playwright storage_state JSON (по умолчанию kimi_session.json рядом со скриптом)",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Таймаут HTTP в секундах (по умолчанию 120)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help='Печатать {"ok","answer","elapsed","events"} в stdout',
    )
    p.add_argument(
        "--thinking",
        action="store_true",
        help="Включить options.thinking=true",
    )
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(argv)
    if not args.prompt or not str(args.prompt).strip():
        eprint("Пустой prompt — укажите текст запроса")
        sys.exit(2)
    if args.timeout <= 0:
        eprint("Таймаут должен быть положительным числом")
        sys.exit(2)

    session_path = args.session if args.session is not None else default_session_path()
    t0 = time.monotonic()
    answer, events = run_chat(
        prompt=str(args.prompt),
        session_path=session_path,
        timeout=float(args.timeout),
        thinking=bool(args.thinking),
    )
    elapsed = time.monotonic() - t0
    ok = bool(answer.strip())

    if args.json:
        out = {
            "ok": ok,
            "answer": answer,
            "elapsed": round(elapsed, 3),
            "events": events,
        }
        print(json.dumps(out, ensure_ascii=False), flush=True)
    else:
        # stdout — только ответ модели
        sys.stdout.write(answer)
        if answer and not answer.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()

    if not ok:
        eprint("Пустой ответ модели")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
