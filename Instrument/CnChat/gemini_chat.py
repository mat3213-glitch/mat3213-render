#!/usr/bin/env python3
"""Gemini движок облачного чат-бота @ghmimo_bot (kimi ИСКЛЮЧЁН; OpenRouter-брейн заменён на Gemini).

Протокол тредов/памяти/сборка промпта — переиспользуется из openrouter_chat (build_prompt).
run → gemini_client.generate_rotating (GEMINI_API_KEY/_2/_3 + ротация при 429/403/5xx).
Интернет (Google Search grounding): GEMINI_WEB=1 (дефолт включено) → tools google_search.
Совместимо с шагом голоса: /tmp/kimirun/{clean,err,model}.txt.

Режимы (argv[1]): build | run
"""
from __future__ import annotations

import os
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import openrouter_chat  # reuse prompt building + memory/threads
import gemini_client


def run_engine(prompt_path: str = "/tmp/prompt.txt", out_dir: str = "/tmp/kimirun") -> int:
    prompt = pathlib.Path(prompt_path).read_text(encoding="utf-8")
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "model.txt").write_text("", encoding="utf-8")
    (out / "err.txt").write_text("", encoding="utf-8")
    web = os.environ.get("GEMINI_WEB", os.environ.get("OR_WEB", "1")).strip().lower() in ("1", "true", "yes", "on")
    timeout = int(os.environ.get("GEMINI_TIMEOUT", os.environ.get("OR_TIMEOUT", "300")))
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    try:
        res = gemini_client.generate_rotating(
            prompt, model=model, timeout=timeout, web=web, max_tokens=8192, max_retries_per_key=1
        )
        text = (res.get("text") or "").strip()
        model_used = str(res.get("model") or model)
        key_index = res.get("key_index", 0)
        keys_total = res.get("keys_total", 1)
        (out / "clean.txt").write_text(text, encoding="utf-8")
        (out / "model.txt").write_text(model_used, encoding="utf-8")
        print(f"[gemini_chat] web={int(web)} model={model_used} key={key_index}/{keys_total} sources={len(res.get('sources', []))}")
        return 0
    except Exception as exc:  # fail-closed: пустой clean + rc!=0
        (out / "clean.txt").write_text("", encoding="utf-8")
        (out / "err.txt").write_text(str(exc)[:500], encoding="utf-8")
        print(f"[gemini_chat] error: {exc}", file=sys.stderr)
        return 1


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if mode == "build":
        openrouter_chat.build_prompt()
        return 0
    if mode == "run":
        return run_engine()
    print("usage: gemini_chat.py build|run", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
