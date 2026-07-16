#!/usr/bin/env python3
"""
looks.py — применение ЗАМКА ПАЛИТРЫ к промпту генерации.

Замок = неизменный хвост-константа. Формула: "<что в кадре>, <lock>".
Хвост не меняется никогда → единая палитра через любые сюжеты, БЕЗ пост-грейда.
Пресеты — в looks.json. НЕ путать со styles.json (там грейды ffmpeg ПОСЛЕ рендера).

Происхождение: реф yaromat → деконструкция → reference_recipes.json[color] → строка.
Урок 2026-07-16: реф даёт ЯЗЫК (свет/палитра/ритм), а не сюжет.

Usage:
    from looks import apply_look, get_look
    prompt = apply_look("child silhouette in hood by car window", "cold_noir_portishead")

    python3 looks.py --list
    python3 looks.py --look cold_noir_portishead --prompt "narrow dark corridor"
"""
import argparse
import json
import sys
from pathlib import Path

LOOKS_PATH = Path(__file__).with_name("looks.json")


def _load():
    return json.loads(LOOKS_PATH.read_text(encoding="utf-8"))["looks"]


def get_look(name):
    """→ dict пресета. KeyError со списком доступных, если имени нет."""
    looks = _load()
    if name not in looks:
        raise KeyError(f"нет лука '{name}'. Есть: {', '.join(looks)}")
    return looks[name]


def apply_look(subject, name):
    """'<что в кадре>' + замок → готовый промпт."""
    return f"{subject.rstrip().rstrip(',')}, {get_look(name)['lock']}"


def negative_for(name):
    """Негатив пресета (для движков с отдельным полем negative — CF/SDXL).
    ⚠️ В промпт ТЕКСТОМ не вставлять: на диффузии 'no face' повышает шанс лица."""
    return get_look(name).get("negative", "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="показать пресеты")
    ap.add_argument("--look", default="cold_noir_portishead")
    ap.add_argument("--prompt", help="'что в кадре' → печатает промпт с замком")
    a = ap.parse_args()

    if a.list:
        for n, v in _load().items():
            print(f"{n}\n  {v.get('title','')}\n  источник: {v.get('source_ref','—')}\n"
                  f"  статус: {v.get('status','—')}\n  замок: {v['lock']}\n")
        return
    if not a.prompt:
        sys.exit("нужен --prompt или --list")
    print(apply_look(a.prompt, a.look))
    neg = negative_for(a.look)
    if neg:
        print(f"\n[negative] {neg}")


if __name__ == "__main__":
    main()
