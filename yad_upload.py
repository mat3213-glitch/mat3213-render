#!/usr/bin/env python3
"""yad_upload.py — YaD upload с retry + throttle-aware настройками.

Общий модуль для всех workflow/скриптов, пишущих на Яндекс.Диск через rclone.
Вызывается из bash: ``python3 yad_upload.py SRC DST``
Импортируется из Python: ``from yad_upload import upload``

rclone copyto с:
  --checkers 2 --transfers 2 --low-level-retries 4 --retries 3
retry: 3 попытки, 900с таймаут на попытку, бэкофф 10*i секунд.
"""
from __future__ import annotations

import subprocess
import sys
import time

RCLONE_FLAGS = ["--checkers", "2", "--transfers", "2",
                "--low-level-retries", "4", "--retries", "3"]
DEFAULT_TIMEOUT = 900
DEFAULT_ATTEMPTS = 3


def upload(src: str, dst: str, *, attempts: int = DEFAULT_ATTEMPTS,
           timeout: int = DEFAULT_TIMEOUT) -> bool:
    """rclone copyto с retry. Возвращает True при успехе."""
    for attempt in range(1, attempts + 1):
        try:
            r = subprocess.run(
                ["rclone", "copyto", src, dst, *RCLONE_FLAGS],
                capture_output=False, timeout=timeout)
            if r.returncode == 0:
                return True
        except subprocess.TimeoutExpired:
            pass
        if attempt < attempts:
            time.sleep(10 * attempt)
    return False


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: yad_upload.py SRC DST", file=sys.stderr)
        raise SystemExit(2)
    ok = upload(sys.argv[1], sys.argv[2])
    raise SystemExit(0 if ok else 1)
