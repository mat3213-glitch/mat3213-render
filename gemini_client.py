"""Gemini engine (kimi/openrouter-free not required) + key pool + rotation on rate-limit.

Pool: GEMINI_API_KEY, GEMINI_API_KEY_2, GEMINI_API_KEY_3 (non-empty, deduped).
On HTTP 429 / 403 / 5xx / transient URLError -> short backoff, retry key, then next key.
Non-retryable errors (400/401/404...) -> re-raised immediately. No secrets logged.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

BASE = "https://generativelanguage.googleapis.com/v1beta"
KEY_ENVS = ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3")
RETRY_STATUS = {429, 500, 502, 503, 504, 403}
DEFAULT_MODEL = "gemini-2.5-flash"


def keys() -> list[str]:
    out: list[str] = []
    for name in KEY_ENVS:
        v = os.environ.get(name, "").strip()
        if v and v not in out:
            out.append(v)
    return out


def _post(model: str, body: dict, key: str, timeout: int) -> dict:
    req = urllib.request.Request(
        f"{BASE}/models/{model}:generateContent",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def _err_body(e: urllib.error.HTTPError) -> str:
    try:
        return e.read().decode("utf-8", "replace")[:200]
    except Exception:
        return ""


def _extract(msg: dict, r: dict) -> dict:
    cand = (r.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    gm = cand.get("groundingMetadata") or {}
    sources = [s.get("web", {}).get("uri", "") for s in gm.get("groundingChunks", [])]
    sources = [s for s in sources if s]
    return {
        "text": text,
        "queries": gm.get("webSearchQueries") or [],
        "sources": sources,
        "model": r.get("modelVersion", msg.get("model", "")),
        "usage": r.get("usageMetadata", {}),
    }


def generate(
    prompt: str | None = None,
    *,
    model: str | None = None,
    system: str | None = None,
    image_b64: str | None = None,
    image_mime: str = "image/jpeg",
    web: bool = False,
    max_tokens: int | None = None,
    timeout: int = 90,
    max_retries_per_key: int = 1,
    start: int = 0,
    sleep_base: float = 0.5,
) -> dict:
    ks = keys()
    if not ks:
        raise RuntimeError("нет GEMINI_API_KEY / GEMINI_API_KEY_2 / GEMINI_API_KEY_3")
    model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)

    parts: list[dict] = []
    if prompt:
        parts.append({"text": prompt})
    if image_b64:
        parts.append({"inline_data": {"mime_type": image_mime, "data": image_b64}})

    body: dict = {"contents": [{"role": "user", "parts": parts}]}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if web:
        body["tools"] = [{"google_search": {}}]
    if max_tokens:
        body["generationConfig"] = {"maxOutputTokens": max_tokens}

    n = len(ks)
    off = start % n
    order = ks[off:] + ks[:off]
    last_err: Exception | None = None

    for ki, key in enumerate(order):
        key_index = (off + ki) % n
        for attempt in range(max_retries_per_key + 1):
            try:
                r = _post(model, body, key, timeout)
                out = _extract({"model": model}, r)
                out["key_index"] = key_index
                out["keys_total"] = n
                return out
            except urllib.error.HTTPError as e:
                last_err = RuntimeError(f"HTTP {e.code} {_err_body(e)}")
                if e.code in RETRY_STATUS:
                    time.sleep(min(sleep_base * (2 ** attempt), 4.0))
                    continue
                raise last_err from e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_err = RuntimeError(f"network {type(e).__name__} {e}")
                time.sleep(min(sleep_base * (2 ** attempt), 4.0))
                continue
        # key exhausted -> next key in pool

    raise RuntimeError(f"все {n} Gemini-ключей исчерпаны: {last_err}") from last_err


def generate_rotating(prompt: str, **kw) -> dict:
    """Round-robin start across calls (module-level counter), e.g. for scouts."""
    global _RR
    try:
        _RR
    except NameError:
        _RR = 0
    kw.setdefault("start", _RR)
    _RR += 1
    return generate(prompt, **kw)


_RR = 0
