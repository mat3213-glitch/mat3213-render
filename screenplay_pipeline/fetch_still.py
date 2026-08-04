#!/usr/bin/env python3
"""
fetch_still.py — сток-стилл (Openverse, CC0/CC-BY) под i2v-движки (Фаза 1, Стадия 4).

Источник кадра для оживления — ОПЕНСОРС/СТОК (Openverse), НЕ AI-генерация (yaromat 2026-07-03:
дороже и не нужно при бесплатном стоке). Самодостаточная копия логики Instrument/Openverse/GEN.py
(тот файл вне чекаута mat3213-render — не шарим кросс-репо императивную зависимость).

Usage:
  python3 fetch_still.py "dark rainy window night city" --out still.jpg
"""

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

# ocr_gate.py лежит в корне github_actions_clips, на уровень выше пайплайна
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

API_BASE = "https://api.openverse.org/v1"


def has_text_in_frame(path: str) -> dict:
    """Гейт надписей на СКАЧАННОМ кандидате (`ocr_gate`, профиль `still`).

    Зачем прямо здесь, а не только в source_qc: сток отдаёт кадры с настоящим текстом
    (знаки, вывески, вотермарки), и дешевле отбросить кандидата на месте — в выдаче
    Openverse есть ещё четыре. 29.07 по запросу «rain fog street» приехало шоссе со
    знаками «USE LOWER GEARS»/«NEXT 2 MILES», и это увидели только глазами.
    Fail-open: недоступный гейт не должен рвать добычу стилла.
    """
    try:
        from ocr_gate import gate
        return gate(path, profile="still")
    except Exception as exc:
        return {"available": False, "has_text": False, "reason": f"ocr недоступен: {exc}"}


def get_token(client_id: str, client_secret: str) -> str:
    r = requests.post(
        f"{API_BASE}/auth_tokens/token/",
        data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def search_images(query: str, token: str, license: str = "cc0") -> list[dict]:
    params = {"q": query, "license": license, "page_size": 5}
    url = f"{API_BASE}/images/?{urlencode(params)}"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=20)
    r.raise_for_status()
    return r.json().get("results", [])


def download(url: str, out_path: str) -> bool:
    # некоторые хосты (напр. wikimedia) режут дефолтный python-requests UA — пойман вживую
    # ("train ticket" → нашёлся результат, скачивание молча провалилось без UA).
    headers = {"User-Agent": "Mozilla/5.0 (compatible; yaromat-content-factory/1.0)"}
    try:
        r = requests.get(url, timeout=30, stream=True, headers=headers)
    except Exception:
        return False
    if r.status_code != 200:
        return False
    with open(out_path, "wb") as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)
    return Path(out_path).exists() and Path(out_path).stat().st_size > 1000


def main():
    ap = argparse.ArgumentParser(description="Поиск+загрузка стилла с Openverse (CC0).")
    ap.add_argument("query")
    ap.add_argument("--out", required=True)
    ap.add_argument("--client-id", default=None, help="или env OPENVERSE_CLIENT_ID")
    ap.add_argument("--client-secret", default=None, help="или env OPENVERSE_CLIENT_SECRET")
    ap.add_argument("--license", default="cc0")
    ap.add_argument("--no-ocr", action="store_true",
                    help="не проверять кандидатов гейтом надписей (по умолчанию проверяем)")
    args = ap.parse_args()

    import os
    client_id = args.client_id or os.environ.get("OPENVERSE_CLIENT_ID", "")
    client_secret = args.client_secret or os.environ.get("OPENVERSE_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        print("[error] нет OPENVERSE_CLIENT_ID/OPENVERSE_CLIENT_SECRET", file=sys.stderr)
        sys.exit(1)

    token = get_token(client_id, client_secret)
    # Openverse (как YouTube Data API — [[reference_yt_dlp_ci]] класс проблемы) не любит
    # длинные многословные запросы — проверено вживую: 5 слов "rainy window night city
    # blurred lights" → 0 результатов, "rainy window" (2 слова) → найдено. Прогрессивно
    # укорачиваем запрос слева, пока что-то не найдётся.
    words = args.query.split()
    tried = []
    items = []
    for n in (len(words), 3, 2, 1):
        if n > len(words):
            continue
        q = " ".join(words[:n])
        if q in tried:
            continue
        tried.append(q)
        items = search_images(q, token, args.license)
        if items:
            print(f"[fetch_still] запрос '{q}' → {len(items)} результатов")
            break
    if not items:
        print(f"[error] Openverse: ничего не найдено (пробовал: {tried})", file=sys.stderr)
        sys.exit(1)

    rejected = 0
    for item in items:
        img_url = item.get("url")
        if not (img_url and download(img_url, args.out)):
            print(f"[fetch_still] пропуск (не скачалось): {img_url}", file=sys.stderr)
            continue

        if not args.no_ocr:
            ocr = has_text_in_frame(args.out)
            if not ocr.get("available"):
                # Не «чисто», а «не проверено» — разница важная: молчаливый пропуск
                # выглядел бы как пройденный гейт (урок mimo_chat.yml, где обрыв job
                # читался пользователем как тишина, а не как ошибка).
                print(f"[fetch_still] ⚠️ гейт надписей не отработал: {ocr.get('reason')}",
                      file=sys.stderr)
            elif ocr.get("has_text"):
                rejected += 1
                print(f"[fetch_still] REJECT надпись в кадре ({ocr.get('reason')}): "
                      f"{item.get('title', '')} — беру следующего кандидата")
                Path(args.out).unlink(missing_ok=True)
                continue

        print(f"[ok] {args.out} ← {item.get('title', '')} ({item.get('license', '')})")
        return

    print(f"[error] ни один из {len(items)} результатов не подошёл "
          f"(отбраковано по надписи: {rejected})", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
