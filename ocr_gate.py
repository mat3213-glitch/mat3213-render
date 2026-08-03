"""Детерминированный OCR-гейт: есть ли в кадре ЧИТАЕМАЯ надпись.

Зачем отдельно от `art_judge.py`: ансамбль VLM надпись ПРОПУСКАЕТ (лист A 29.07,
кадр `child.png` прошёл голосование как чистый). Надпись — не вопрос вкуса, тут не
нужно мнение трёх моделей, нужен факт. Тессеракт даёт его детерминированно, повторяемо,
без квоты и примерно за секунду на кадр.

Пороги НЕ выдуманы, а откалиброваны на размеченном листе A (`--grid`), и смотреть надо
сразу ДВА числа — пойманное и ложное: на одном «пойманном» гейт вырождается в ручной
просмотр ([[feedback_number_lies_look_at_frames]]).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from PIL import Image


def find_text(
    path: str,
    *,
    min_conf: float = 60,
    min_len: int = 3,
    min_words: int = 2,
    big_word_ratio: float = 0.025,
    scales: tuple[float, ...] = (1.0, 2.0),
    lang: str = "eng+rus",
) -> dict[str, Any]:
    try:
        import pytesseract
    except Exception as exc:
        return {"has_text": False, "words": [], "reason": f"import error: {exc}", "available": False}

    try:
        img = Image.open(path)
    except Exception as exc:
        return {"has_text": False, "words": [], "reason": f"open error: {exc}", "available": False}

    words: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    orig_w, orig_h = img.size
    failures: list[str] = []

    for scale in scales:
        try:
            if scale != 1.0:
                scaled = img.resize((int(orig_w * scale), int(orig_h * scale)), Image.LANCZOS)
            else:
                scaled = img
            data = pytesseract.image_to_data(scaled, lang=lang, output_type=pytesseract.Output.DICT)
            n = len(data["text"])
            for i in range(n):
                text = str(data["text"][i]).strip()
                try:
                    conf = float(data["conf"][i])
                except (TypeError, ValueError):
                    continue
                if conf < min_conf:
                    continue
                cleaned = "".join(ch for ch in text if ch.isalnum())
                if len(cleaned) < min_len:
                    continue
                if not any(ch.isalpha() for ch in cleaned):
                    continue
                left = float(data["left"][i])
                height = float(data["height"][i])
                h_ratio = height / (orig_h * scale)
                key = (cleaned.lower(), round(left / scale / 20))
                if key in seen:
                    continue
                seen.add(key)
                words.append({"text": cleaned, "conf": conf, "h_ratio": h_ratio})
        except Exception as exc:
            # Падение ОДНОГО масштаба не должно стирать найденное на другом: 2× — это
            # ресайз в 4 раза по пикселям, он первым упирается в память. Гейт слепнет
            # (available=False) только когда не отработал НИ ОДИН масштаб.
            failures.append(f"scale {scale}: {exc}")
            continue

    if len(failures) == len(scales):
        return {"has_text": False, "words": [], "reason": "; ".join(failures)[:200],
                "available": False}

    big = [w for w in words if w["h_ratio"] > big_word_ratio]
    if big:
        w = big[0]
        return {
            "has_text": True,
            "words": words,
            "reason": f"big word '{w['text']}' h={w['h_ratio']:.3f}",
            "available": True,
        }
    if len(words) >= min_words:
        return {
            "has_text": True,
            "words": words,
            "reason": f"{len(words)} words >=conf{int(min_conf)}",
            "available": True,
        }
    return {"has_text": False, "words": words, "reason": "clean", "available": True}


def calibrate(
    paths_labeled: list[tuple[str, bool]],
    grid: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for params in grid:
        tp = fn = fp = tn = 0
        for path, label in paths_labeled:
            res = find_text(path, **params)
            if label and res["has_text"]:
                tp += 1
            elif label and not res["has_text"]:
                fn += 1
            elif not label and res["has_text"]:
                fp += 1
            else:
                tn += 1
        results.append({"params": params, "tp": tp, "fn": fn, "fp": fp, "tn": tn})
    results.sort(key=lambda r: (r["fp"], -r["tp"]))
    return results


def scan_dir(directory: str) -> list[str]:
    exts = {".png", ".jpg", ".jpeg"}
    paths: list[str] = []
    for root, _, files in os.walk(directory):
        for name in files:
            if Path(name).suffix.lower() in exts:
                paths.append(os.path.join(root, name))
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description="OCR gate for AI-generated art")
    parser.add_argument("--dir", help="Directory to scan recursively")
    parser.add_argument("--file", help="Single image (smoke check)")
    parser.add_argument("--positive", default="text_in_frame", help="Substring indicating positive sample")
    parser.add_argument("--grid", action="store_true", help="Run parameter grid search")
    args = parser.parse_args()

    if not args.dir and not args.file:
        parser.error("нужен --dir или --file")

    if args.file:
        res = find_text(args.file)
        print(f"{'REJECT' if res['has_text'] else 'clean'}  {Path(args.file).name}  "
              f"{res['reason']}  available={res['available']}")
        for w in res["words"][:10]:
            print(f"    '{w['text']}' conf={w['conf']:.0f} h={w['h_ratio']:.3f}")
        return 0

    paths = scan_dir(args.dir)
    labeled = [(p, args.positive in p) for p in paths]
    print(f"файлов: {len(paths)} (позитивов по «{args.positive}»: {sum(l for _, l in labeled)})")

    if args.grid:
        grid = [
            {"min_conf": c, "min_words": w, "big_word_ratio": r}
            for c in (40, 50, 60, 70)
            for w in (1, 2, 3)
            for r in (0.02, 0.025, 0.03)
        ]
        results = calibrate(labeled, grid)
        print("conf/words/ratio | пойманное tp/(tp+fn) | ЛОЖНОЕ fp/(fp+tn)")
        for r in results:
            p = r["params"]
            print(f"{p['min_conf']:>3}/{p['min_words']}/{p['big_word_ratio']:<5} | "
                  f"{r['tp']}/{r['tp'] + r['fn']} | {r['fp']}/{r['fp'] + r['tn']}")
        best = results[0]
        print(
            f"\nbest: {best['params']} "
            f"caught={best['tp']}/{best['tp'] + best['fn']} "
            f"false={best['fp']}/{best['fp'] + best['tn']}"
        )
    else:
        reject = clean = 0
        for path, _ in labeled:
            res = find_text(path)
            name = Path(path).name
            if res["has_text"]:
                print(f"REJECT  {name}  {res['reason']}")
                reject += 1
            else:
                print(f"clean  {name}  {res['reason']}")
                clean += 1
        print(f"\ntotals: REJECT={reject} clean={clean}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
