#!/usr/bin/env python3
"""
freellm_seed_and_run.py — драйвер для freellmapi на GH Actions (воркер freellm-gh).

Запускается на раннере ПОСЛЕ старта локального freellmapi (localhost:3001).
Полностью STATELESS: каждый ран сам бутстрапит прокси (нет персистентной БД).

Шаги:
  1. POST /api/auth/setup (или /login если уже есть) → session-токен.
  2. POST /api/keys по каждому провайдеру (ключи из env = GH Secrets).
  3. GET /api/settings/api-key → unified-ключ `freellmapi-…`.
  4. Прогон батча: для каждой задачи POST /v1/chat/completions (Bearer unified).
  5. Запись результатов в --out (воркфлоу rclone-копирует на ЯД).

Ключи из env → платформа freellmapi:
  GEMINI_API_KEY / _2 / _3   → google   (пул из нескольких ключей)
  GROQ_API_KEY               → groq
  OPENROUTER_API_KEY         → openrouter
  HUGGINGFACE_TOKEN          → huggingface
  GH_MODELS_TOKEN/GITHUB_TOKEN → github
Stdlib-only (urllib) — на раннере без доп. зависимостей.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("FREELLM_BASE", "http://127.0.0.1:3001").rstrip("/")
DEFAULT_MODEL = os.environ.get("FREELLM_DEFAULT_MODEL", "gemini-2.5-flash")
SETUP_EMAIL = os.environ.get("FREELLM_EMAIL", "ci@freellm.local")
SETUP_PASS = os.environ.get("FREELLM_PASSWORD", "ci-bootstrap-pass-8+")

# env-имя ключа → платформа freellmapi (список по порядку = порядок добавления)
KEY_MAP = [
    ("GEMINI_API_KEY", "google"),
    ("GEMINI_API_KEY_2", "google"),
    ("GEMINI_API_KEY_3", "google"),
    ("GROQ_API_KEY", "groq"),
    ("OPENROUTER_API_KEY", "openrouter"),
    ("HUGGINGFACE_TOKEN", "huggingface"),
    ("GH_MODELS_TOKEN", "github"),
]


def _req(method: str, path: str, token: str | None = None,
         body: dict | None = None, timeout: int = 60) -> tuple[int, dict]:
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode(errors="replace")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"_raw": raw[:300]}


def wait_ready(retries: int = 60, delay: float = 1.0):
    """Ждём, пока сервер ответит на /api/auth/status (без авторизации)."""
    for _ in range(retries):
        try:
            code, _ = _req("GET", "/api/auth/status", timeout=5)
            if code == 200:
                return True
        except Exception:
            pass
        time.sleep(delay)
    return False


def bootstrap() -> str:
    """Setup (или login) → session-токен."""
    code, j = _req("POST", "/api/auth/setup",
                   body={"email": SETUP_EMAIL, "password": SETUP_PASS})
    if code == 201 and j.get("token"):
        return j["token"]
    # уже инициализирован (персистентная БД) → логинимся
    code, j = _req("POST", "/api/auth/login",
                   body={"email": SETUP_EMAIL, "password": SETUP_PASS})
    if code == 200 and j.get("token"):
        return j["token"]
    raise RuntimeError(f"bootstrap failed: setup/login HTTP {code} {j}")


def seed_keys(session: str) -> dict:
    """Добавить ключи провайдеров. Возвращает сводку {env_name: ok/err}."""
    summary = {}
    for env_name, platform in KEY_MAP:
        key = os.environ.get(env_name, "").strip()
        if not key:
            continue
        code, j = _req("POST", "/api/keys", token=session,
                       body={"platform": platform, "key": key,
                             "label": f"ci-{env_name.lower()}"})
        summary[env_name] = "ok" if code in (200, 201) else f"HTTP {code}: {str(j)[:120]}"
    return summary


def get_unified(session: str) -> str:
    code, j = _req("GET", "/api/settings/api-key", token=session)
    if code == 200 and j.get("apiKey"):
        return j["apiKey"]
    raise RuntimeError(f"unified key fetch failed: HTTP {code} {j}")


def run_task(unified: str, task: dict, timeout: int = 120) -> dict:
    model = task.get("model") or DEFAULT_MODEL
    messages = ([{"role": "system", "content": task["system"]}] if task.get("system") else []) + \
               [{"role": "user", "content": task["prompt"]}]
    started = time.time()
    code, j = _req("POST", "/v1/chat/completions", token=unified,
                   body={"model": model, "messages": messages,
                         "max_tokens": task.get("max_tokens", 1024)}, timeout=timeout)
    elapsed = round(time.time() - started, 1)
    if code == 200:
        text = (j.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
        return {"id": task["id"], "ok": bool(text), "text": text,
                "model": j.get("model", model), "elapsed": elapsed,
                "usage": j.get("usage") or {},
                "error": "" if text else "пустой ответ"}
    return {"id": task["id"], "ok": False, "text": "", "model": model,
            "elapsed": elapsed, "usage": {}, "error": f"HTTP {code}: {str(j)[:200]}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", required=True, help="JSON [{id,prompt,model,system}]")
    ap.add_argument("--out", required=True, help="куда писать результаты JSON")
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args()

    if not wait_ready():
        print("ERR: freellmapi не поднялся (/api/auth/status)", file=sys.stderr)
        sys.exit(1)

    session = bootstrap()
    seeded = seed_keys(session)
    print(f"seeded keys: {json.dumps(seeded, ensure_ascii=False)}", file=sys.stderr)
    unified = get_unified(session)
    print(f"unified key: {unified[:16]}… ({len(unified)} chars)", file=sys.stderr)

    tasks = json.loads(open(args.batch, encoding="utf-8").read())
    if isinstance(tasks, dict):
        tasks = [tasks]
    results = [run_task(unified, t, timeout=args.timeout) for t in tasks]

    open(args.out, "w", encoding="utf-8").write(
        json.dumps(results, ensure_ascii=False, indent=2))
    ok = sum(1 for r in results if r["ok"])
    print(f"done: {ok}/{len(results)} ok → {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
