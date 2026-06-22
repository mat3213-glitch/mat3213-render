#!/usr/bin/env python3
"""
media_register.py — регистрация медиа в единый каталог на ЯД.

Обёртка над classify.py, добавляющая синк манифеста media_catalog.jsonl на ЯД.
Схема:
  1. pull_manifest(tmp)  — скачать манифест с ЯД во временный файл
  2. register(...)       — классифицировать + upsert в локальный манифест
  3. push_manifest(tmp)  — залить манифест обратно на ЯД
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

# Локальный импорт classify
sys.path.insert(0, str(Path(__file__).resolve().parent))
import classify

# Путь к манифесту на ЯД
YD_MANIFEST = "ydrive:Content factory/media_catalog.jsonl"

# ---------------------------------------------------------------------------
# Синк с ЯД
# ---------------------------------------------------------------------------

def pull_manifest(tmp: str) -> None:
    """Скачать манифест с ЯД во временный файл tmp. Если файла нет — пустой."""
    try:
        subprocess.run(
            ["rclone", "copyto", YD_MANIFEST, tmp],
            check=False, capture_output=True, text=True,
        )
    except FileNotFoundError:
        # rclone не найден — оставляем пустой файл
        pass


def push_manifest(tmp: str) -> None:
    """Залить манифест tmp обратно на ЯД."""
    try:
        subprocess.run(
            ["rclone", "copyto", tmp, YD_MANIFEST],
            check=False, capture_output=True, text=True,
        )
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Основная логика
# ---------------------------------------------------------------------------

def register(
    path: str,
    pool: str,
    type_: str,
    source: str,
    prompt: str,
    date: str,
    no_sync: bool = False,
) -> dict:
    """
    Классифицировать медиа и записать в манифест.
    Если no_sync=False — синкает с ЯД.
    Возвращает записанную запись.
    """
    # Получить запись через classify
    if source == "gen":
        record = classify.classify_generated(path, pool, type_, prompt, date)
    else:
        record = classify.classify_external(path, pool, type_, date)

    if no_sync:
        # Только локальный файл рядом со скриптом
        manifest_path = str(Path(__file__).resolve().parent / "media_catalog.jsonl")
        classify.add(record, manifest=manifest_path)
    else:
        # Синк с ЯД
        tmp = tempfile.mktemp(suffix=".jsonl")
        try:
            pull_manifest(tmp)
            classify.add(record, manifest=tmp)
            push_manifest(tmp)
        finally:
            Path(tmp).unlink(missing_ok=True)

    return record


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def run_self_test() -> bool:
    """
    Прогон на 3 примерах с --no-sync (без rclone):
    1. gen с промптом → теги есть
    2. external → tags=[]
    3. повторный register того же id → не дублируется
    """
    import os as _os

    tmp_manifest = Path(tempfile.mktemp(suffix=".jsonl"))
    passed = 0
    failed = 0

    def check(cond: bool, desc: str) -> None:
        nonlocal passed, failed
        if cond:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL: {desc}", file=sys.stderr)

    try:
        # --- Тест 1: gen с промптом ---
        r1 = register(
            path="Content factory/cloud_io/qwen_pool/2026-06-16/img_01.png",
            pool="qwen_pool",
            type_="image",
            source="gen",
            prompt="fog dark forest, muted tones, film grain",
            date="2026-06-16",
            no_sync=True,
        )
        check(len(r1["tags"]) > 0, f"test1: expected non-empty tags, got {r1['tags']}")
        check("haze" in r1["tags"], f"test1: expected 'haze' in tags, got {r1['tags']}")
        check("grain" in r1["tags"], f"test1: expected 'grain' in tags, got {r1['tags']}")
        check(r1["source"] == "gen", "test1: source should be 'gen'")
        check(r1["prompt"] == "fog dark forest, muted tones, film grain", "test1: prompt mismatch")

        # --- Тест 2: external → tags=[] ---
        r2 = register(
            path="Content factory/cloud_io/pexels_pool/2026-06-16/photo.jpg",
            pool="pexels_pool",
            type_="image",
            source="external",
            prompt="",
            date="2026-06-16",
            no_sync=True,
        )
        check(r2["tags"] == [], f"test2: expected empty tags, got {r2['tags']}")
        check(r2["source"] == "external", "test2: source should be 'external'")

        # --- Тест 3: повторный register → не дублируется ---
        r3 = register(
            path="Content factory/cloud_io/qwen_pool/2026-06-16/img_01.png",
            pool="qwen_pool",
            type_="image",
            source="gen",
            prompt="fog dark forest, muted tones, film grain",
            date="2026-06-16",
            no_sync=True,
        )
        # Проверяем количество записей в локальном манифесте
        manifest_path = Path(__file__).resolve().parent / "media_catalog.jsonl"
        with open(manifest_path, "r", encoding="utf-8") as fh:
            lines = [l.strip() for l in fh if l.strip()]
        ids = [json.loads(l)["id"] for l in lines]
        check(len(ids) == 2, f"test3: expected 2 unique records, got {len(ids)}")
        check(len(ids) == len(set(ids)), "test3: no duplicate ids")

        # Итого
        print(f"\n{'='*40}")
        if failed == 0:
            print(f"PASS: {passed}/{passed} assertions")
        else:
            print(f"FAIL: {failed} failed, {passed} passed")
        print(f"{'='*40}")

    finally:
        # Очистить локальный манифест, созданный self-test
        manifest_path = Path(__file__).resolve().parent / "media_catalog.jsonl"
        if manifest_path.exists():
            manifest_path.unlink()
        if tmp_manifest.exists():
            tmp_manifest.unlink()

    return failed == 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Регистрация медиа в единый каталог на ЯД"
    )
    parser.add_argument("--path", type=str, default=None,
                        help="Путь к медиафайлу на ЯД")
    parser.add_argument("--pool", type=str, default=None,
                        help="Пул (qwen_pool, veofree_pool, pexels_pool и т.д.)")
    parser.add_argument("--type", type=str, default=None, choices=["image", "video"],
                        help="Тип контента")
    parser.add_argument("--source", type=str, default=None, choices=["gen", "external"],
                        help="Источник: gen или external")
    parser.add_argument("--prompt", type=str, default="",
                        help="Промпт (для gen)")
    parser.add_argument("--date", type=str, default=None,
                        help="Дата YYYY-MM-DD")
    parser.add_argument("--no-sync", action="store_true",
                        help="Не трогать ЯД (только локальный манифест)")
    parser.add_argument("--self-test", action="store_true",
                        help="Запустить встроенный self-test")

    args = parser.parse_args()

    if args.self_test:
        ok = run_self_test()
        sys.exit(0 if ok else 1)

    if not all([args.path, args.pool, args.type, args.source, args.date]):
        parser.error("--path, --pool, --type, --source, --date обязательны")

    record = register(
        path=args.path,
        pool=args.pool,
        type_=args.type,
        source=args.source,
        prompt=args.prompt,
        date=args.date,
        no_sync=args.no_sync,
    )
    print(json.dumps(record, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
