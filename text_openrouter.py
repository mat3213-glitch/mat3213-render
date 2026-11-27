#!/usr/bin/env python3
"""OpenRouter как текстовый воркер (stdlib urllib, без браузера и без сессии).

Заменяет Qwen-чат в s2c/auto_analyst: chat.qwen.ai мёртв (токен отозван +
Aliyun WAF-капча на /completions), а OpenRouter — обычный HTTPS с API-ключом.
Заголовки HTTP-Referer/X-Title обязательны: без них free-тир режет.

При 429/5xx перебирает цепочку моделей (OPENROUTER_MODELS через запятую).
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODELS = ("cohere/north-mini-code:free", "google/gemma-4-31b-it:free", "openrouter/free")


def model_chain() -> list[str]:
    raw = os.environ.get("OPENROUTER_MODELS", "").strip()
    models = [m.strip() for m in raw.split(",") if m.strip()] or list(DEFAULT_MODELS)
    if "openrouter/free" not in models:
        models.append("openrouter/free")   # авто-роутер — последний шанс, если free-модели отвалились
    return models


STRICT_SUFFIX = ("""

ВАЖНО ФОРМАТА: ответь ТОЛЬКО одним JSON-объектом. Никаких рассуждений, никакого
текста до или после. Первый символ ответа — «{{», последний — «}}".""")


def generate(prompt: str, model: str | None = None, timeout: int = 120,
             max_tokens: int = 4096, json_mode: bool = False,
             strict: bool = False, report_model: bool = False, web: bool = False):
    """Один запрос (с цепочкой моделей). Бросает RuntimeError, если не ответил никто.

    json_mode/strict — для второго дубля: авто-роутер free-тира иногда отдаёт
    reasoning-модель, которая печатает «Here's a thinking process:» вместо JSON.

    report_model=True — вернуть (текст, модель), которая реально ответила;
    нужен чату в Telegram: видно, кто из цепочки отвечает (цепочка фолбэкится).

    web=True — попросить платформенный веб-поиск у OpenRouter (флаг владельца 15.11).
    Запрос с web может отказать МОДЕЛЬ (400/422) или ВЕДЕТЬ К ТАРИФУ
    (402 web-доступа при живой базе — наблюдение 15.11); в обоих случаях та же
    модель перепрашивается без web, и цепочка идёт дальше как обычно. Квота и
    5xx не трогаем — там фолбэк по цепочке должна быть.
    """
    if strict:
        prompt = prompt + STRICT_SUFFIX
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise RuntimeError("нет OPENROUTER_API_KEY в окружении")

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/mat3213-glitch",
        "X-Title": "content-factory",
    }
    chain = [model] if model else model_chain()
    errors = []
    for m in chain:
        web_remaining = web
        while True:
            payload = {
                "model": m,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0.2,
            }
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
            if web_remaining:
                payload["plugins"] = [{"id": "web"}]
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(URL, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as exc:
                if web_remaining and exc.code in (400, 422, 402):
                    # web-поле не поддерживается моделью (400/422) или — наблюдение
                    # 15.11 — ведёт к тарифу (402) при живой базе; та же модель
                    # перепрашивается без web
                    web_remaining = False
                    print(f"  [openrouter] web-поле отклонено ({exc.code}) — повтор без web",
                          file=sys.stderr)
                    continue
                errors.append(f"{m}: HTTP {exc.code}")
                break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                errors.append(f"{m}: {type(exc).__name__}")
                break
            except json.JSONDecodeError:
                errors.append(f"{m}: не-JSON ответ")
                break
            try:
                text = (data["choices"][0]["message"].get("content") or "").strip()
            except (KeyError, IndexError, TypeError):
                errors.append(f"{m}: пустая структура ответа")
                break
            if text:
                return (text, m) if report_model else text
            errors.append(f"{m}: пустой ответ")
            break
    raise RuntimeError("OpenRouter не ответил — " + " | ".join(errors))
