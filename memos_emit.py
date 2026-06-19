#!/usr/bin/env python3
"""
memos_emit.py — репорт события/ошибки в memory_os-фид на ЯД (для GH Actions / рендеров).

Автономный (stdlib + rclone, без зависимости от пакета memory_os): пишет JSONL-строку и
заливает в ydrive:Content factory/memory_os_feed/. Локальный коллектор (memory_os.emit.ingest)
вольёт её в БД перед рефлексией — так ошибки/инсайты облачных ранов попадают в самообучение.

Использование (в шаге воркфлоу):
  python3 memos_emit.py <actor> <action> <status> "<detail>"
  напр.: python3 memos_emit.py gh-ai auto_analyst ${{ job.status }} "target=${{ matrix.target }}"
"""
import json
import os
import subprocess
import sys
import time

actor = sys.argv[1] if len(sys.argv) > 1 else "gh-ai"
action = sys.argv[2] if len(sys.argv) > 2 else "ci"
status = sys.argv[3] if len(sys.argv) > 3 else "ok"
detail = sys.argv[4] if len(sys.argv) > 4 else ""

kind = "error" if status in ("failure", "fail", "cancelled") else "event"
rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "actor": actor,
       "action": action, "status": status, "detail": detail}

rid = os.environ.get("GITHUB_RUN_ID", "local")
att = os.environ.get("GITHUB_RUN_ATTEMPT", "0")
fn = f"/tmp/memos_{rid}_{att}_{int(time.time() * 1000) % 100000}.jsonl"
with open(fn, "w", encoding="utf-8") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")

subprocess.run(["rclone", "copyto", fn, f"ydrive:Content factory/memory_os_feed/{os.path.basename(fn)}"],
               check=False)
print("emitted:", os.path.basename(fn), rec["status"])
