#!/usr/bin/env python3
"""
asset_catalog.py — потребитель КАТАЛОГА ФУТАЖЕЙ: рендер сам подтягивает нужный футаж.

Каталог собирает Instrument/Pinterest/catalog_build.py (доски Pinterest → ЯД
footage_catalog/<категория>/ + catalog.jsonl). Здесь — выбор по категории/тегам и
JIT-загрузка только нужных файлов (склад не держим локально).

Использование из рендера:
    from asset_catalog import pick, fetch
    overlays = pick(category="overlay", orientation="horizontal", n=2, seed=track_seed)
    paths = [fetch(e, workdir) for e in overlays]   # → локальные mp4

CLI (проверка):
    python3 asset_catalog.py --list
    python3 asset_catalog.py --category overlay --n 3
    python3 asset_catalog.py --category vinil --n 1 --fetch /tmp/pick
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
from pathlib import Path

YD = "ydrive:Content factory/footage_catalog"
_CACHE: list | None = None


def _rclone(*args, timeout=300):
    return subprocess.run(["rclone", *args], capture_output=True, text=True, timeout=timeout)


def load(force: bool = False) -> list:
    """catalog.jsonl с ЯД → список записей (кэш в процессе)."""
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE
    r = _rclone("cat", f"{YD}/catalog.jsonl")
    out = []
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    else:
        print(f"[catalog] не прочитал catalog.jsonl: {r.stderr[:100]}", file=sys.stderr)
    _CACHE = out
    return out


def pick(category: str | None = None, tags: list[str] | None = None,
         orientation: str | None = None, min_dur: float | None = None,
         max_dur: float | None = None, n: int = 1, seed=None) -> list[dict]:
    """Выбрать до n записей под фильтр. tags — ИЛИ (любой совпавший). seed — детерминизм на трек."""
    items = load()

    def ok(e: dict) -> bool:
        if category and e.get("category") != category:
            return False
        if orientation and e.get("orientation") != orientation:
            return False
        if min_dur is not None and e.get("duration", 0) < min_dur:
            return False
        if max_dur is not None and e.get("duration", 1e9) > max_dur:
            return False
        if tags:
            et = set(e.get("tags") or [])
            if not (et & set(tags)):
                return False
        return True

    cand = [e for e in items if ok(e)]
    rng = random.Random(seed)
    rng.shuffle(cand)
    return cand[:n]


def fetch(entry: dict, dest_dir) -> Path:
    """JIT-скачать клип записи с ЯД в dest_dir → локальный путь."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    # path в манифесте = footage_catalog/<cat>/ref_<id>.mp4 ; ЯД-корень = YD без footage_catalog
    rel = entry["path"].split("footage_catalog/", 1)[-1]
    local = dest_dir / Path(rel).name
    if not local.exists():
        r = _rclone("copyto", f"{YD}/{rel}", str(local))
        if r.returncode != 0:
            raise RuntimeError(f"fetch {entry['id']} не вышел: {r.stderr[:120]}")
    return local


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="сводка по категориям")
    ap.add_argument("--category")
    ap.add_argument("--orientation", choices=["horizontal", "vertical", "square"])
    ap.add_argument("--tags", help="через запятую")
    ap.add_argument("--min-dur", type=float)
    ap.add_argument("--max-dur", type=float)
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--seed")
    ap.add_argument("--fetch", help="скачать выбранное в указанную папку")
    a = ap.parse_args()

    items = load()
    if a.list or not (a.category or a.tags):
        by: dict = {}
        for e in items:
            by.setdefault(e.get("category", "?"), []).append(e)
        print(f"каталог: {len(items)} клипов")
        for c, es in sorted(by.items()):
            ori = {}
            for e in es:
                ori[e.get("orientation", "?")] = ori.get(e.get("orientation", "?"), 0) + 1
            print(f"  {c}: {len(es)}  {ori}")
        if not (a.category or a.tags):
            return

    sel = pick(category=a.category, orientation=a.orientation,
               tags=a.tags.split(",") if a.tags else None,
               min_dur=a.min_dur, max_dur=a.max_dur, n=a.n, seed=a.seed)
    print(f"\nвыбрано {len(sel)}:")
    for e in sel:
        print(f"  {e['id']} | {e['category']} | {e.get('width')}x{e.get('height')} "
              f"{e.get('duration')}s {e.get('orientation')} | blend={e.get('blend')} | {e['path']}")
    if a.fetch:
        for e in sel:
            p = fetch(e, a.fetch)
            print(f"  ↓ {p}")


if __name__ == "__main__":
    main()
