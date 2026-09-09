#!/usr/bin/env python3
"""Serialized GitHub Actions runner for the browser GLM fanout worker."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

YD_IN = "ydrive:Content factory/cloud_io/glm_ci/in"
YD_OUT = "ydrive:Content factory/cloud_io/glm_ci/out"
ROOT = Path(__file__).resolve().parent
GLM_CHAT = ROOT / "Instrument" / "GLM" / "glm_chat.py"
SESSION = ROOT / "Instrument" / "GLM" / "glm_session.json"
REFRESHED = ROOT / "glm-session-refreshed.json"
MODEL = "GLM-5.3-Flash"
THINKING_MODE = "High"
SAFE_BATCH_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


def _task_timeout(value: object) -> int:
    try:
        return min(max(int(value or 240), 60), 900)
    except (TypeError, ValueError):
        return 240


def run_task(task: dict) -> dict:
    started = time.time()
    timeout = _task_timeout(task.get("timeout"))
    diagnostics = ROOT / f"glm-diagnostics-{task.get('id', 'task')}.json"
    args = [
        "xvfb-run", "-a", "-s", "-screen 0 1280x900x24", "nice", "-n", "15",
        sys.executable, str(GLM_CHAT), "--stdin", "--model", MODEL,
        "--thinking-mode", THINKING_MODE, "--timeout", str(timeout),
        "--diagnostics", str(diagnostics),
    ]
    env = dict(os.environ)
    env["GLM_HEADLESS"] = "0"
    env["GLM_STATE_OUTPUT"] = str(REFRESHED)
    try:
        result = subprocess.run(args, input=str(task["prompt"]), capture_output=True,
                                text=True, timeout=timeout + 120, env=env)
        text = result.stdout.strip()
        ok = result.returncode == 0 and bool(text)
        error = "" if ok else (result.stderr or f"empty output (rc={result.returncode})")[-300:]
        if not ok:
            text = ""
        if ok and REFRESHED.is_file() and REFRESHED.stat().st_size > 1000:
            os.replace(REFRESHED, SESSION)
    except subprocess.TimeoutExpired:
        text, ok, error = "", False, f"subprocess timeout {timeout + 120}s"
    except Exception as exc:
        text, ok, error = "", False, f"{type(exc).__name__} {str(exc)[:200]}"
    diag = {}
    try:
        diag = json.loads(diagnostics.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {
        "id": task.get("id"), "ok": ok, "text": text, "model": MODEL,
        "thinking_mode": THINKING_MODE, "elapsed": round(time.time() - started, 1),
        "error": error, "diagnostics": {
            key: diag[key] for key in ("status", "actual_model", "actual_thinking_mode",
                                       "first_text_s", "completion_s", "total_s") if key in diag
        },
    }


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: glm_runner.py BATCH_ID")
    batch_id = sys.argv[1]
    if not SAFE_BATCH_ID.fullmatch(batch_id):
        raise SystemExit("invalid batch id")
    subprocess.run(["rclone", "copyto", f"{YD_IN}/{batch_id}.json", "batch.json"], check=True)
    with open("batch.json", encoding="utf-8") as stream:
        tasks = json.load(stream)
    results = [run_task(task) for task in tasks]
    with open("out.json", "w", encoding="utf-8") as stream:
        json.dump(results, stream, ensure_ascii=False, indent=2)
    subprocess.run(["rclone", "copyto", "out.json", f"{YD_OUT}/{batch_id}.json"], check=True)
    print(f"glm-gh: {sum(bool(x['ok']) for x in results)}/{len(results)} ok")


if __name__ == "__main__":
    main()
