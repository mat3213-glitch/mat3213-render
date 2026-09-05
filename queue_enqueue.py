#!/usr/bin/env python3
"""queue_enqueue.py — дополняет общую publish-очередь ЯД items frozen-v3.

`Content factory/cloud_io/publish_queue/queue.json` — общий read-modify-write
артефакт. Скрипт обновляет его с CAS-защитой: перед загрузкой ещё раз тянет
текущую очередь и отказывается перезаписывать, если её изменил другой writer
(конфликт, retryable, exit 3). После загрузки тянет файл обратно и сверяет hash
(exit 4 при расхождении) — как relay из P0.

Идемпотентно: уже присутствующие id не дублируются, чужие/иные platform-позиции
сохраняются как есть.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")
REMOTE = "ydrive:"
CAPTION = "тишина — это не пустота. внутри всегда есть точка покоя.\n\nyaromat — Frozen Space\nвключи звук"


def validate_slug(slug: str) -> str:
    if not SLUG_RE.fullmatch(slug):
        raise ValueError(f"slug должен быть [A-Za-z0-9_-], получен: {slug!r}")
    return slug


def enqueue_frozen(queue: dict, slug: str, title: str) -> int:
    """Дополняет очередь vk-full + 4 youtube-shorts для slug. Возвращает число добавленных."""
    validate_slug(slug)
    vk_id = f"{slug}_vk"
    added = 0
    vk = queue.setdefault("vk", [])
    if all(isinstance(x, dict) and x.get("id") != vk_id for x in vk):
        vk.append({"id": vk_id, "media": f"{slug}/{slug}_full.mp4",
                   "type": "video", "caption": CAPTION, "own_track": True})
        added += 1
    yt = queue.setdefault("youtube", [])
    for n in range(1, 5):
        item_id = f"{slug}_short_{n}"
        if all(isinstance(x, dict) and x.get("id") != item_id for x in yt):
            yt.append({
                "id": item_id,
                "media": f"{slug}/{slug}_section_{n}.mp4",
                "type": "video", "title": f"{title} · часть {n}/4 #Shorts",
                "description": CAPTION + "\n\n#Shorts #futuregarage #downtempo #yaromat",
                "own_track": True})
            added += 1
    return added


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def rclone(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["rclone", *argv], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"rclone {' '.join(argv[:3])} failed ({result.returncode}): "
                           f"{result.stderr.strip()[:300]}")
    return result


def load_queue(text: str) -> dict:
    try:
        queue = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"queue.json повреждён: {exc}")
    if not isinstance(queue, dict):
        raise ValueError("queue.json должен быть объектом")
    return queue


def cas_update(queue_dst: str, slug: str, title: str, *, work: Path) -> tuple[int, int]:
    """Полный CAS-цикл. Возвращает (added, exit_code); >0 при необходимости ручного retry."""
    before = work / "queue_before.json"
    rclone(["copyto", f"{REMOTE}{queue_dst}", str(before)])
    try:
        queue = load_queue(before.read_text(encoding="utf-8"))
        added = enqueue_frozen(queue, slug, title)
    except ValueError as exc:
        print(f"CONFLICT/SKIP: {exc}")
        return 0, 0

    if added == 0:
        print(f"items уже в очереди (added=0) — обновление не требуется")
        return 0, 0

    staged = work / "queue_staged.json"
    staged.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")

    current = work / "queue_current.json"
    rclone(["copyto", f"{REMOTE}{queue_dst}", str(current)])
    if sha256_file(current) != sha256_file(before):
        print("CAS CONFLICT: очередь изменена другим writer — НЕ перезаписываю. "
              "Повтори dispatch.")
        return added, 3

    rclone(["copyto", str(staged), f"{REMOTE}{queue_dst}"])
    verified = work / "queue_verified.json"
    rclone(["copyto", f"{REMOTE}{queue_dst}", str(verified)])
    if sha256_file(verified) != sha256_file(staged):
        print("UPLOAD MISMATCH: загруженный файл отличается от локального — "
              "состояние требует ручной проверки.")
        return added, 4
    print(f"enqueued {added} items (vk={slug}_vk, youtube={slug}_short_1..4)")
    return added, 0


def main() -> int:
    ap = argparse.ArgumentParser(description="CAS-дополнение publish-очереди ЯД")
    ap.add_argument("--queue-dst", required=True,
                    help="rclone путь queue.json без REMOTE, e.g. Content factory/cloud_io/publish_queue/queue.json")
    ap.add_argument("--slug", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--workdir", default=None, help="куда писать временные файлы (default: tmp)")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as td:
        work = Path(args.workdir) if args.workdir else Path(td)
        work.mkdir(parents=True, exist_ok=True)
        try:
            _, code = cas_update(args.queue_dst, args.slug, args.title, work=work)
        except RuntimeError as exc:
            print(f"FAIL: {exc}")
            return 2
        return code


if __name__ == "__main__":
    raise SystemExit(main())