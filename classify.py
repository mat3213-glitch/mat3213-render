#!/usr/bin/env python3
"""
classify.py — единый классификатор медиа-библиотеки.

Формирует манифест media_catalog.jsonl (одна строка/файл) с полями:
{id, pool, source, type, path, prompt, tags, date}

Промпты сгенерированного контента нормализуются через catalog_tagger.extract_tags.
Внешний контент получает теги из внешнего vision-шага (или tags=[]).
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

# Локальный импорт catalog_tagger
sys.path.insert(0, str(Path(__file__).resolve().parent))
import catalog_tagger as ct

# ---------------------------------------------------------------------------
# Вспомогательные
# ---------------------------------------------------------------------------

def _make_id(path: str) -> str:
    """Стабильный id: имя файла без расширения."""
    return Path(path).stem


def _normalize_tags_from_prompt(prompt: str) -> list[str]:
    """
    Разбить промпт на подоб caption + labels, извлечь теги через catalog_tagger.
    Промпт = «сырьё» для тегов.
    """
    # Промпт как caption + разбиваем на слова как labels
    text = prompt
    tags = ct.extract_tags(text)
    return sorted(tags)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def classify_generated(
    path: str,
    pool: str,
    type_: str,
    prompt: str,
    date: str,
) -> dict:
    """
    Классифицировать сгенерированный контент (Qwen/VeoFree/Hunyuan).
    Теги берутся ИЗ промпта через catalog_tagger.
    """
    tags = _normalize_tags_from_prompt(prompt)
    return {
        "id": _make_id(path),
        "pool": pool,
        "source": "gen",
        "type": type_,
        "path": path,
        "prompt": prompt,
        "tags": tags,
        "date": date,
    }


def classify_external(
    path: str,
    pool: str,
    type_: str,
    date: str,
    vision_tags: list[str] | None = None,
) -> dict:
    """
    Классифицировать внешний контент (Pexels/Openverse/Pinterest).
    Если vision_tags переданы — используем их; иначе tags=[].
    """
    tags = sorted(vision_tags) if vision_tags else []
    return {
        "id": _make_id(path),
        "pool": pool,
        "source": "external",
        "type": type_,
        "path": path,
        "prompt": "",
        "tags": tags,
        "date": date,
    }


def add(record: dict, manifest: str = "media_catalog.jsonl") -> None:
    """
    Upsert записи в манифест: если id уже есть — обновить, иначе добавить.
    Атомарная перезапись через temp-файл.
    """
    manifest_path = Path(manifest)

    # Загрузить существующий манифест
    existing: list[dict] = []
    if manifest_path.exists():
        with open(manifest_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    existing.append(json.loads(line))

    # Upsert по id
    record_id = record["id"]
    replaced = False
    for i, rec in enumerate(existing):
        if rec["id"] == record_id:
            existing[i] = record
            replaced = True
            break
    if not replaced:
        existing.append(record)

    # Атомарная запись через temp-файл
    dir_ = manifest_path.parent
    dir_.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(dir_), suffix=".jsonl")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for rec in existing:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        Path(tmp_path).replace(manifest_path)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise



# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Единый классификатор медиа-библиотеки"
    )
    parser.add_argument(
        "--gen", action="store_true",
        help="Режим генерации (из промпта)"
    )
    parser.add_argument(
        "--ext", action="store_true",
        help="Режим внешнего контента (vision-теги)"
    )
    parser.add_argument(
        "--path", type=str, default=None,
        help="Путь к файлу медиа"
    )
    parser.add_argument(
        "--pool", type=str, default=None,
        help="Пул (qwen_pool, veofree_pool, hunyuan_pool, pexels_pool, openverse_pool, footage_catalog)"
    )
    parser.add_argument(
        "--type", type=str, default=None, choices=["image", "video"],
        help="Тип контента: image или video"
    )
    parser.add_argument(
        "--prompt", type=str, default="",
        help="Промпт (для генерации)"
    )
    parser.add_argument(
        "--date", type=str, default=None,
        help="Дата YYYY-MM-DD"
    )
    parser.add_argument(
        "--manifest", type=str, default="media_catalog.jsonl",
        help="Путь к манифесту"
    )
    parser.add_argument(
        "--self-test", action="store_true",
        help="Запустить встроенный self-test"
    )

    args = parser.parse_args()

    if args.self_test:
        ok = run_self_test()
        sys.exit(0 if ok else 1)

    if args.gen:
        if not all([args.path, args.pool, args.type, args.date, args.prompt]):
            parser.error("--gen требует --path, --pool, --type, --date, --prompt")
        record = classify_generated(
            path=args.path,
            pool=args.pool,
            type_=args.type,
            prompt=args.prompt,
            date=args.date,
        )
        print(json.dumps(record, ensure_ascii=False, indent=2))
        add(record, manifest=args.manifest)
        print(f"Записано в {args.manifest}")

    elif args.ext:
        if not all([args.path, args.pool, args.type, args.date]):
            parser.error("--ext требует --path, --pool, --type, --date")
        record = classify_external(
            path=args.path,
            pool=args.pool,
            type_=args.type,
            date=args.date,
        )
        print(json.dumps(record, ensure_ascii=False, indent=2))
        add(record, manifest=args.manifest)
        print(f"Записано в {args.manifest}")

    else:
        parser.error("Укажите --gen или --ext (или --self-test)")


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def run_self_test() -> bool:
    """
    Прогон на 3-4 синтетических примерах:
    1. gen с промптом про fog/grain/vinyl → теги извлеклись
    2. gen с другим промптом
    3. external без vision → tags=[]
    4. upsert не плодит дубли
    """
    import os as _os

    passed = 0
    failed = 0

    def check(condition: bool, desc: str) -> None:
        nonlocal passed, failed
        if condition:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL: {desc}", file=sys.stderr)

    # Временный манифест
    tmp_manifest = Path(tempfile.mktemp(suffix=".jsonl"))
    try:
        # --- Тест 1: gen с промптом fog/grain/vinyl ---
        r1 = classify_generated(
            path="qwen_pool/2026-06-16/img_01.png",
            pool="qwen_pool",
            type_="image",
            prompt="dark fog forest at twilight, muted tones, old film grain texture, vinyl crackle vibe",
            date="2026-06-16",
        )
        check("haze" in r1["tags"], f"test1: expected 'haze' in tags, got {r1['tags']}")
        check("grain" in r1["tags"], f"test1: expected 'grain' in tags, got {r1['tags']}")
        check("vinyl" in r1["tags"], f"test1: expected 'vinyl' in tags, got {r1['tags']}")
        check(r1["source"] == "gen", "test1: source should be 'gen'")
        check(r1["prompt"] != "", "test1: prompt should not be empty")

        # --- Тест 2: gen с промптом glitch/flare ---
        r2 = classify_generated(
            path="veofree_pool/2026-06-16/vid_42.mp4",
            pool="veofree_pool",
            type_="video",
            prompt="digital noise scanlines, chromatic distortion, lens flare at night",
            date="2026-06-16",
        )
        check("glitch" in r2["tags"], f"test2: expected 'glitch' in tags, got {r2['tags']}")
        check("flare" in r2["tags"], f"test2: expected 'flare' in tags, got {r2['tags']}")
        check(r2["type"] == "video", "test2: type should be 'video'")

        # --- Тест 3: external без vision → tags=[] ---
        r3 = classify_external(
            path="pexels_pool/2026-06-16/pexels_007.jpg",
            pool="pexels_pool",
            type_="image",
            date="2026-06-16",
        )
        check(r3["tags"] == [], f"test3: expected empty tags, got {r3['tags']}")
        check(r3["source"] == "external", "test3: source should be 'external'")
        check(r3["prompt"] == "", "test3: prompt should be empty for external")

        # --- Тест 4: upsert не плодит дубли ---
        add(r1, manifest=str(tmp_manifest))
        add(r2, manifest=str(tmp_manifest))
        add(r3, manifest=str(tmp_manifest))
        # Добавляем r1 ещё раз — не должно стать два
        add(r1, manifest=str(tmp_manifest))

        with open(tmp_manifest, "r", encoding="utf-8") as fh:
            lines = [l.strip() for l in fh if l.strip()]
        ids = [json.loads(l)["id"] for l in lines]
        check(len(ids) == 3, f"test4: expected 3 unique records, got {len(ids)}")
        check(len(ids) == len(set(ids)), "test4: no duplicate ids allowed")

        # --- Итого ---
        print(f"\n{'='*40}")
        if failed == 0:
            print(f"PASS: {passed}/{passed} assertions")
        else:
            print(f"FAIL: {failed} failed, {passed} passed")
        print(f"{'='*40}")

    finally:
        if tmp_manifest.exists():
            tmp_manifest.unlink()

    return failed == 0


if __name__ == "__main__":
    main()
