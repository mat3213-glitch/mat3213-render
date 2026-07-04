#!/usr/bin/env python3
"""
pool_tagger.py — семантическое тегирование AI-пула (qwen_pool/veofree_pool/hunyuan_pool).

Цель: превратить накопленный generic-пул в ИЩЕЙ каталог (аналог asset_catalog.py для
footage_catalog), чтобы раскадровка могла подбирать атмосферные кадры под imagery_cues
beat'а, а не брать что попало (найдено вживую 2026-07-04 — пул-микс без сюжета, разбор
на развилке с mimo → вариант C: climax через scene_dispatch, атмосфера — из этого каталога).

hero_candidate (yaromat 2026-07-04): у каждого кадра — короткая фраза, главный визуальный
мотив. Нужно чтобы matcher (следующий шаг, не этот файл) мог собрать НЕСКОЛЬКО кадров под
ОДИН hero_candidate — мотив архетипа обязан вернуться ≥3 раза (intro/body/climax/outro),
иначе "главный герой" истории не читается (правило уже было в archetypes/library.yaml как
комментарий, но нигде не проверялось программно).

Изоляция (тот же принцип, что plastic_gate_nightly.py): отдельный проход, не трогает
daily-генераторы/гейт. Идемпотентно — уже тегированные id (по каталогу) пропускает.

Запуск:
    python3 pool_tagger.py                      # untagged за сегодня/вчера, все 3 пула
    python3 pool_tagger.py --pool veofree_pool --date 2026-07-02
    python3 pool_tagger.py --backfill           # ВСЕ даты пула (разовый прогон по истории)
"""
import os
import sys
import json
import argparse
import datetime
import tempfile
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from plastic_gate_core import frames_of, make_strip, extract_json, MIMO, sh  # переиспользуем

YD = "ydrive:Content factory"
CATALOG_REL = "cloud_io/ai_pool_catalog.jsonl"
POOLS = ["qwen_pool", "veofree_pool", "hunyuan_pool"]
SCALES = ("wide", "medium", "macro", "close")

TAG_RUBRIC = (
    "Перед тобой кадр(ы) AI-сгенерированного видео/картинки для клипа yaromat "
    "(Future Garage/Downtempo, тёмный меланхоличный вайб, БЕЗ лиц крупным планом/неона/текста). "
    "Определи:\n"
    "1. tags: 3-6 конкретных визуальных существительных/понятий, что буквально в кадре "
    "(напр. 'туман', 'рельсы', 'фонарь', 'силуэт человека', 'берёзы', 'снег').\n"
    "2. hero_candidate: ОДНА короткая фраза (1-3 слова) — главный визуальный объект/мотив кадра, "
    "который мог бы стать повторяющимся 'главным героем' истории (что узнаваемо в ДРУГОМ кадре: "
    "напр. 'туман', 'дальняя фигура', 'вода', 'заброшенное здание').\n"
    "3. mood: 2-4 слова эмоционального тона (напр. 'тихий, ожидание').\n"
    "4. scale: один из wide/medium/macro/close — масштаб плана.\n"
    "Ответь СТРОГО одним JSON: "
    '{"tags": [...], "hero_candidate": "...", "mood": "...", "scale": "..."}.'
)


def tag_media(local_path: str, timeout: int = 180) -> dict | None:
    """Один уже скачанный локально файл → словарь тегов, или None при сбое mimo/JSON."""
    with tempfile.TemporaryDirectory(prefix="pool_tag_") as strips_dir:
        name = os.path.splitext(os.path.basename(local_path))[0]
        strip = make_strip(frames_of(local_path, strips_dir), name, strips_dir)
        if not strip:
            return None
        try:
            r = subprocess.run(
                [MIMO, "run", "--pure", "--dangerously-skip-permissions", TAG_RUBRIC, "-f", strip],
                capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return None
        d = extract_json(r.stdout or "")
        if not d:
            return None
        scale = d.get("scale") if d.get("scale") in SCALES else "medium"
        return {
            "tags": [str(t)[:40] for t in (d.get("tags") or [])][:6],
            "hero_candidate": str(d.get("hero_candidate", ""))[:60],
            "mood": str(d.get("mood", ""))[:60],
            "scale": scale,
        }


def load_catalog() -> list[dict]:
    r = sh(f'rclone cat "{YD}/{CATALOG_REL}"')
    out = []
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


def append_catalog(entries: list[dict]):
    """jsonl маленький (десятки строк) — читаем целиком+дописываем+заливаем обратно.
    Единственный писатель (этот скрипт по cron/ручному прогону) — гонки не ожидается."""
    if not entries:
        return
    existing = sh(f'rclone cat "{YD}/{CATALOG_REL}"').stdout
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
        if existing:
            f.write(existing if existing.endswith("\n") else existing + "\n")
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        tmp = f.name
    sh(f'rclone copyto "{tmp}" "{YD}/{CATALOG_REL}"')
    os.unlink(tmp)


def media_list(pool: str, date: str) -> list[str]:
    """--max-depth 1 ОБЯЗАТЕЛЕН — иначе rclone lsf рекурсивно затянет _rejected/ (брак гейта).
    Живой баг найден в первом backfill-прогоне 2026-07-04: --include "*.png" ловил не только
    img_01.png (реальный кадр), но и *.fail.png (скриншот НЕУДАЧНОЙ генерации VeoFree — не
    футаж) + rclone lsf иногда отдавал "_rejected/" САМ каталог как запись (trailing slash),
    который потом падал на copyto/mimo. Фильтруем оба класса явно."""
    r = sh(f'rclone lsf "{YD}/cloud_io/{pool}/{date}/" --max-depth 1 '
           f'--include "*.mp4" --include "*.png" --include "*.jpg"')
    out = []
    for x in r.stdout.splitlines():
        x = x.strip()
        if not x or x.endswith("/"):
            continue
        if x.endswith(".fail.png") or ".FAILED." in x:
            continue
        out.append(x)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", choices=POOLS)
    ap.add_argument("--date")
    ap.add_argument("--backfill", action="store_true", help="все даты пула, не только сегодня/вчера")
    a = ap.parse_args()

    pools = [a.pool] if a.pool else POOLS
    today = datetime.datetime.utcnow().date()

    if a.date:
        dates_by_pool = {p: [a.date] for p in pools}
    elif a.backfill:
        dates_by_pool = {}
        for p in pools:
            r = sh(f'rclone lsf "{YD}/cloud_io/{p}/" --dirs-only')
            dates_by_pool[p] = [d.strip("/") for d in r.stdout.splitlines() if d.strip()]
    else:
        dates = [today.isoformat(), (today - datetime.timedelta(days=1)).isoformat()]
        dates_by_pool = {p: dates for p in pools}

    catalog = load_catalog()
    tagged_ids = {e["id"] for e in catalog}

    new_entries = []
    for pool in pools:
        for date in dates_by_pool.get(pool, []):
            for fname in media_list(pool, date):
                entry_id = f"{pool}_{date}_{fname}"
                if entry_id in tagged_ids:
                    continue
                rel = f"cloud_io/{pool}/{date}/{fname}"
                with tempfile.TemporaryDirectory(prefix="pool_dl_") as dl_dir:
                    local = os.path.join(dl_dir, fname)
                    r = sh(f'rclone copyto "{YD}/{rel}" "{local}"')
                    if r.returncode != 0 or not os.path.exists(local):
                        print(f"  ✗ не скачал {rel}")
                        continue
                    print(f"  → тегирую {rel}")
                    tags = tag_media(local)
                    if tags is None:
                        print(f"    ✗ mimo не ответил/битый JSON")
                        continue
                    entry = {
                        "id": entry_id, "pool": pool, "date": date,
                        "engine": pool.replace("_pool", ""),
                        "path": rel, "ext": os.path.splitext(fname)[1],
                        **tags,
                    }
                    new_entries.append(entry)
                    print(f"    ✓ hero_candidate={tags['hero_candidate']!r} tags={tags['tags']}")

    append_catalog(new_entries)
    print(f"\nИтого протегировано новых: {len(new_entries)} (каталог: {CATALOG_REL})")


if __name__ == "__main__":
    main()
