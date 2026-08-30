"""Shared safety primitives for active Instrument jobs.

Keep this module dependency-light: browser/path/ffmpeg helpers are usable even
when ``requests`` is not installed in a particular worker environment.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable, Iterable


CHROMIUM_HARDENED_ARGS = ("--no-sandbox", "--disable-dev-shm-usage")
RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_LEAF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def chromium_launch_kwargs(*, headless: bool = True, **kwargs) -> dict:
    """Return Playwright launch kwargs suitable for GH/headless Chromium."""
    existing = list(kwargs.pop("args", ()) or ())
    args = [*existing, *(arg for arg in CHROMIUM_HARDENED_ARGS if arg not in existing)]
    return {"headless": headless, "args": args, **kwargs}


def http_call(
    method: str,
    url: str,
    *,
    session=None,
    retries: int = 3,
    backoff: float = 0.5,
    retry_non_idempotent: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs,
):
    """Call ``requests`` with bounded exponential retry/backoff.

    GET/HEAD/OPTIONS are retried by default. POST-like calls require an explicit
    opt-in because a lost response can otherwise duplicate a generation job.
    The final HTTP response is returned unchanged so existing status handling
    remains authoritative.
    """
    if retries < 0 or backoff < 0:
        raise ValueError("retries/backoff must be non-negative")
    import requests

    verb = method.upper()
    can_retry = verb in {"GET", "HEAD", "OPTIONS"} or retry_non_idempotent
    client = session or requests
    last_error = None
    for attempt in range(retries + 1):
        try:
            response = client.request(verb, url, **kwargs)
            retryable = can_retry and response.status_code in RETRYABLE_HTTP_STATUSES
            if not retryable or attempt == retries:
                return response
            retry_after = response.headers.get("Retry-After", "")
            response.close()
            delay = min(float(retry_after), 10.0) if retry_after.isdigit() else backoff * (2 ** attempt)
        except requests.RequestException as exc:
            last_error = exc
            if not can_retry or attempt == retries:
                raise
            delay = backoff * (2 ** attempt)
        sleep(delay)
    raise last_error or RuntimeError("unreachable HTTP retry state")


def safe_leaf_name(value: str, *, field: str = "name", suffixes: Iterable[str] = ()) -> str:
    """Validate an argv/rclone leaf such as ``out_name``."""
    if not isinstance(value, str) or not _LEAF_RE.fullmatch(value):
        raise ValueError(f"{field}: unsafe leaf name")
    allowed = tuple(s.lower() for s in suffixes)
    if allowed and Path(value).suffix.lower() not in allowed:
        raise ValueError(f"{field}: unsupported suffix")
    return value


def safe_remote_path(value: str, *, field: str = "remote path", suffixes: Iterable[str] = ()) -> str:
    """Validate a relative rclone path without traversal or remote injection."""
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError(f"{field}: empty or too long")
    if value.startswith(("/", "\\")) or "\\" in value or ":" in value or "\x00" in value:
        raise ValueError(f"{field}: absolute/remote paths are forbidden")
    parts = value.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise ValueError(f"{field}: traversal or empty component")
    for part in parts:
        if not all(ch.isalnum() or ch in " ._-" for ch in part):
            raise ValueError(f"{field}: unsupported character")
    allowed = tuple(s.lower() for s in suffixes)
    if allowed and Path(parts[-1]).suffix.lower() not in allowed:
        raise ValueError(f"{field}: unsupported suffix")
    return value


def safe_local_path(value: str, *, roots: Iterable[Path], field: str = "path") -> Path:
    """Resolve a user path and require it to stay under an explicit root."""
    candidate = Path(value).expanduser().resolve()
    allowed = [Path(root).expanduser().resolve() for root in roots]
    if not any(candidate == root or candidate.is_relative_to(root) for root in allowed):
        raise ValueError(f"{field}: path is outside allowed roots")
    return candidate


def ffmpeg_argv(*args: str, threads: int = 2, overwrite: bool = True) -> list[str]:
    """Build a quiet, non-interactive, resource-bounded ffmpeg argv."""
    if threads < 1 or threads > 16:
        raise ValueError("ffmpeg threads must be between 1 and 16")
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-threads", str(threads), *(["-y"] if overwrite else []), *map(str, args),
    ]


def ffmpeg_input(path: Path | str, *, seek: float | None = None) -> list[str]:
    """Build an input fragment, placing fast seek before ``-i``."""
    prefix = ["-ss", f"{seek:.3f}"] if seek is not None else []
    return [*prefix, "-i", str(path)]
