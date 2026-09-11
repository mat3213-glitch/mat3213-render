#!/usr/bin/env python3
"""queue_enqueue.py — патч-аппенд + детерминированный reconcile publish-очереди ЯД.

`Content factory/cloud_io/publish_queue/queue.json` — общий артефакт: читают
`publish_gh/publish_gh.py` и `publish_gh/watchdog.py`, пишут GH-воркфлоу
`publish_frozen_v3.yml` и `platform_variants.yml`. Единственный локальный писатель
не зарегистрирован (проверено 2026-09-06).

Старый read-modify-write с CAS не устранял окно «повторное чтение → запись»
(rclone на Яндекс.Диске не даёт атомарной условной записи), поэтому writers
НИКОГДА не правят queue.json напрямую:

  submit    — кладёт уникально-именованный патч в queue.d/<slug>_<stamp>.json.
              Новое имя = нет lost update: одному писателю не нужно наблюдать
              чужую запись, чтобы не потерять свою.
  reconcile — единственный, кто переписывает queue.json: тянет его, фолдит
              патчи (детерминированно, идемпотентно по (platform,id)),
              сверяет текущую очередь непосредственно перед записью и
              verify-после-записи. Запуск разрешён только выделенному workflow
              reconcile_publish_queue.yml с concurrency publish_queue_write.
              Локальные/другие workflow writers запрещены до чтения очереди.
              Патчи НЕ удаляются: повтор после падения не теряет заявки.
              Повреждённый queue.json → отказ БЕЗ перезаписи.

Идемпотентно: повторный submit/reconcile не дублирует элементы, чужие
platform-позиции сохраняются как есть.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")
REMOTE = "ydrive:"
CAPTION = "тишина — это не пустота. внутри всегда есть точка покоя.\n\nyaromat — Frozen Space\nвключи звук"

PATCH_DIRNAME = "queue.d"
PROCESSED_DIRNAME = "queue.processed"
QUEUE_DST = "Content factory/cloud_io/publish_queue/queue.json"
WRITER_WORKFLOW = "reconcile_publish_queue.yml"
PLATFORMS = ("tg", "vk", "ok", "pinterest", "youtube")
RCLONE_TIMEOUT = 300


def validate_slug(slug: str) -> str:
    if not SLUG_RE.fullmatch(slug):
        raise ValueError(f"slug должен быть [A-Za-z0-9_-], получен: {slug!r}")
    return slug


def frozen_items(slug: str, title: str) -> list[dict]:
    """Элементы frozen-v3: vk-full + 4 youtube-shorts (схема патча platform/entry)."""
    validate_slug(slug)
    items = [
        {"platform": "vk", "entry": {
            "id": f"{slug}_vk", "media": f"{slug}/{slug}_full.mp4",
            "type": "video", "caption": CAPTION, "own_track": True}},
    ]
    for n in range(1, 5):
        items.append({"platform": "youtube", "entry": {
            "id": f"{slug}_short_{n}", "media": f"{slug}/{slug}_section_{n}.mp4",
            "type": "video", "title": f"{title} · часть {n}/4 #Shorts",
            "description": CAPTION + "\n\n#Shorts #futuregarage #downtempo #yaromat",
            "own_track": True}})
    return items


def enqueue_frozen(queue: dict, slug: str, title: str) -> int:
    """Backward-совместимый fold frozen-items в локальную очередь. Возвращает число добавленных."""
    return fold(queue, frozen_items(slug, title))


def load_queue(text: str) -> dict:
    try:
        queue = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"queue.json повреждён: {exc}")
    if not isinstance(queue, dict):
        raise ValueError("queue.json должен быть объектом")
    for platform, bucket in queue.items():
        if (not isinstance(bucket, list) or any(not isinstance(entry, dict)
                or not isinstance(entry.get("id"), str) or not entry["id"] for entry in bucket)):
            raise ValueError(f"queue.json: invalid bucket {platform}")
        if len({entry["id"] for entry in bucket}) != len(bucket):
            raise ValueError(f"queue.json: duplicate ids in {platform}")
    return queue


def validate_entry(platform: str, entry: dict, *, op: str = "add") -> None:
    if platform not in PLATFORMS:
        raise ValueError(f"неизвестная platform: {platform!r}")
    if not isinstance(entry.get("id"), str) or not entry["id"]:
        raise ValueError("entry.id обязателен")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", entry["id"]):
        raise ValueError(f"небезопасный entry.id: {entry['id']!r}")
    if op == "remove":
        # Удаление по (platform,id): кроме безопасного id ничего не требуется.
        return
    media = entry.get("media")
    if not isinstance(media, str) or not media or media.startswith("/") or ".." in Path(media).parts:
        raise ValueError("entry.media обязателен и должен быть относительным безопасным путём")
    if platform in {"tg", "vk", "ok", "pinterest"}:
        caption = entry.get("caption")
        if not isinstance(caption, str) or not caption.strip():
            raise ValueError(f"{platform}: entry.caption обязателен")
    if platform == "youtube":
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("youtube: entry.title обязателен")
        if entry.get("own_track") is not True:
            raise ValueError("youtube: own_track должен быть true")


def parse_patch(text: str) -> list[dict]:
    """Валидирует патч и возвращает items [{"platform", "entry", "op"}, ...].

    op = "add" (default) | "remove". remove удаляет по (platform,id).
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"патч повреждён: {exc}")
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("патч должен содержать объект с списком 'items'")
    items: list[dict] = []
    for item in data["items"]:
        if not isinstance(item, dict):
            raise ValueError("элемент патча должен быть объектом")
        platform = item.get("platform")
        entry = item.get("entry")
        op = item.get("op", "add")
        if not isinstance(platform, str) or not platform or not isinstance(entry, dict):
            raise ValueError("элемент патча требует platform (str) и entry (dict)")
        if op not in ("add", "remove"):
            raise ValueError(f"неизвестная операция патча: {op!r}")
        validate_entry(platform, entry, op=op)
        items.append({"platform": platform, "entry": entry, "op": op})
    return items


def fold(queue: dict, items: list[dict]) -> int:
    """Дополняет/правит очередь по (platform,id); чужие позиции сохраняются.

    op="add" — добавить, если id ещё нет; op="remove" — удалить id.
    Возвращает число применённых изменений.
    """
    changed = 0
    for item in items:
        platform = item["platform"]
        entry = item["entry"]
        op = item.get("op", "add")
        bucket = queue.setdefault(platform, [])
        if op == "remove":
            before = len(bucket)
            bucket[:] = [x for x in bucket if x.get("id") != entry["id"]]
            if len(bucket) != before:
                changed += 1
            continue
        if all(isinstance(x, dict) and x.get("id") != entry["id"] for x in bucket):
            bucket.append(entry)
            changed += 1
    return changed


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def rclone(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["rclone", *argv], capture_output=True, text=True, timeout=RCLONE_TIMEOUT)
    if check and result.returncode != 0:
        raise RuntimeError(f"rclone {' '.join(argv[:3])} failed ({result.returncode}): "
                           f"{result.stderr.strip()[:300]}")
    return result


def pull_text(dst: str) -> str:
    """rclone cat удалённого файла; возвращает его текст."""
    return rclone(["cat", f"{REMOTE}{dst}"]).stdout


def pull_file(dst: str, local: Path) -> None:
    rclone(["copyto", f"{REMOTE}{dst}", str(local)])


def push_file(local: Path, dst: str) -> None:
    rclone(["copyto", str(local), f"{REMOTE}{dst}"])


def list_patch_names(queue_dst: str) -> list[str]:
    """Имена файлов в queue.d/ рядом с queue.json, без подкаталогов."""
    folder = f"{'/'.join(queue_dst.split('/')[:-1])}/{PATCH_DIRNAME}"
    result = rclone(["lsf", "--files-only", f"{REMOTE}{folder}"])
    names = sorted(name.strip() for name in result.stdout.splitlines() if name.strip())
    if any(not re.fullmatch(r"[A-Za-z0-9_-]+\.json", name) for name in names):
        raise ValueError("queue.d contains an invalid patch name")
    return names


def patch_remote_path(queue_dst: str, name: str) -> str:
    folder = "/".join(queue_dst.split("/")[:-1])
    return f"{folder}/{PATCH_DIRNAME}/{name}"


def processed_remote_path(queue_dst: str, name: str) -> str:
    folder = "/".join(queue_dst.split("/")[:-1])
    return f"{folder}/{PROCESSED_DIRNAME}/{name}"


def archive_patch(queue_dst: str, name: str) -> None:
    rclone(["moveto", f"{REMOTE}{patch_remote_path(queue_dst, name)}",
            f"{REMOTE}{processed_remote_path(queue_dst, name)}"])


def archive_patches(queue_dst: str, names: list[str]) -> None:
    for name in names:
        archive_patch(queue_dst, name)


def submit_patch(queue_dst: str, items: list[dict], slug: str, *, work: Path) -> tuple[str, str]:
    """Пишет патч в queue.d/<slug>_<stamp>_<rand>.json. Возвращает (имя, remote-путь)."""
    validate_slug(slug)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{slug}_{stamp}_{uuid.uuid4().hex[:8]}.json"
    patch = {"items": items, "created_at": stamp}
    local = work / name
    local.write_text(json.dumps(patch, ensure_ascii=False, indent=2), encoding="utf-8")
    remote = patch_remote_path(queue_dst, name)
    push_file(local, remote)
    return name, remote


def require_writer_context(queue_dst: str) -> None:
    """Operational guard; credentials/workflow permissions remain the trust boundary."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    expected = f"{repo}/.github/workflows/{WRITER_WORKFLOW}@refs/heads/main"
    if (queue_dst != QUEUE_DST or not repo or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("GITHUB_WORKFLOW_REF") != expected
            or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_JOB") != "reconcile"
            or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
            or os.environ.get("GITHUB_EVENT_NAME") not in {"schedule", "workflow_dispatch", "workflow_run"}):
        raise RuntimeError(f"queue writes require the serialized {WRITER_WORKFLOW} on main")


def reconcile(queue_dst: str, *, work: Path) -> tuple[int, int]:
    """Serialize before reading. Never repair an overwritten queue after readers saw it."""
    require_writer_context(queue_dst)
    lock_path = Path(tempfile.gettempdir()) / ("queue-reconcile-" + sha256_bytes(queue_dst.encode()) + ".lock")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another reconcile is running on this runner") from exc
        return _reconcile_locked(queue_dst, work=work)


def _reconcile_locked(queue_dst: str, *, work: Path) -> tuple[int, int]:
    qfile = work / "queue.json"
    cur = work / "queue_current.json"

    pull_file(queue_dst, qfile)
    try:
        queue = load_queue(qfile.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"FAIL: {exc} — очередь повреждена, НЕ перезаписываю")
        return 0, 2

    requested = list_patch_names(queue_dst)
    patch_items: list[tuple[str, list[dict]]] = []
    broken: list[str] = []
    for name in requested:
        try:
            patch_items.append((name, parse_patch(pull_text(patch_remote_path(queue_dst, name)))))
        except ValueError as exc:
            broken.append(f"{name}: {exc}")
    if broken:
        raise ValueError("invalid patches; queue unchanged: " + "; ".join(broken))

    items = [item for _, parsed in patch_items for item in parsed]
    added = fold(queue, items)
    if added == 0:
        archive_patches(queue_dst, [name for name, _ in patch_items])
        return 0, 0
    staged = work / "queue_staged.json"
    staged.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")
    pull_file(queue_dst, cur)
    if sha256_file(cur) != sha256_file(qfile):
        raise RuntimeError("queue changed outside serialized writer; refusing overwrite")
    push_file(staged, queue_dst)
    verified = work / "queue_verified.json"
    pull_file(queue_dst, verified)
    if sha256_file(verified) != sha256_file(staged):
        raise RuntimeError("queue verification failed; inspect external writer before retry")
    archive_patches(queue_dst, [name for name, _ in patch_items])
    print(f"reconciled {added} items; existing queue order preserved")
    return added, 0


def cmd_submit(args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory() as td:
        work = Path(args.workdir) if args.workdir else Path(td)
        work.mkdir(parents=True, exist_ok=True)
        if args.items_file:
            items = parse_patch(Path(args.items_file).read_text(encoding="utf-8"))
            slug = args.slug or "patch"
        else:
            if not args.slug or not args.title:
                print("FAIL: submit требует --slug/--title или --items-file")
                return 2
            items = frozen_items(args.slug, args.title)
            slug = args.slug
        try:
            name, remote = submit_patch(args.queue_dst, items, slug, work=work)
        except (RuntimeError, ValueError) as exc:
            print(f"FAIL: {exc}")
            return 2
        print(f"patched {len(items)} items → {remote}")
        return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory() as td:
        work = Path(args.workdir) if args.workdir else Path(td)
        work.mkdir(parents=True, exist_ok=True)
        try:
            _, code = reconcile(args.queue_dst, work=work)
        except (RuntimeError, ValueError) as exc:
            print(f"FAIL: {exc}")
            return 2
        return code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Патч-аппенд + reconcile publish-очереди ЯД")
    sub = ap.add_subparsers(dest="command", required=True)

    sub_p = sub.add_parser("submit", help="положить патч в queue.d/ (без чтения queue.json)")
    sub_p.add_argument("--slug", default=None)
    sub_p.add_argument("--title", default=None)
    sub_p.add_argument("--items-file", default=None, help="JSON {items:[{platform, entry}]}")
    sub_p.set_defaults(func=cmd_submit)

    sub_r = sub.add_parser("reconcile", help="свести патчи queue.d/ в queue.json")
    sub_r.set_defaults(func=cmd_reconcile)

    for parser in (sub_p, sub_r):
        parser.add_argument("--queue-dst", required=True, help="rclone queue.json path without REMOTE")
        parser.add_argument("--workdir", default=None, help="temporary working directory")

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
