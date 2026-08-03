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


_RAPIDOCR_ENGINE: Any = None


def _rapidocr_engine(factory: Any) -> Any:
    """Один экземпляр RapidOCR на процесс (ленивый синглтон)."""
    global _RAPIDOCR_ENGINE
    if _RAPIDOCR_ENGINE is None:
        _RAPIDOCR_ENGINE = factory()
    return _RAPIDOCR_ENGINE


def find_text_regions(
    path: str,
    *,
    min_boxes: int = 2,
    big_box_ratio: float = 0.025,
    min_area_ratio: float = 0.00015,
    min_score: float = 0.0,
    max_side: int = 1600,
    with_rec: bool = False,
) -> dict[str, Any]:
    """Детектор текстовых РЕГИОНОВ (RapidOCR/DB), без распознавания.

    Зачем второй движок: замер 03.08 на листе A показал, что tesseract не берёт наш брак
    вообще (0/1 при всех 36 порогах). Причина видна глазами — на кадре `child.png` не текст,
    а AI-ПСЕВДОТЕКСТ: мелкие нечитаемые закорючки на бирке рюкзака. Распознаватель не
    прочтёт то, что буквами не является; детектор регионов отвечает на другой вопрос —
    «есть ли в кадре нечто текстоподобное», и именно он тут уместен.
    """
    try:
        from rapidocr_onnxruntime import RapidOCR
        import numpy as np
    except Exception as exc:
        return {"has_text": False, "boxes": [], "reason": f"import error: {exc}",
                "available": False}

    try:
        img = Image.open(path).convert("RGB")
        w, h = img.size
        if max(w, h) > max_side:
            s = max_side / max(w, h)
            img = img.resize((int(w * s), int(h * s)), Image.LANCZOS)
            w, h = img.size
        arr = np.array(img)
    except Exception as exc:
        return {"has_text": False, "boxes": [], "reason": f"open error: {exc}",
                "available": False}

    try:
        # ONNX-сессия поднимается ~секунду; на калибровке (27 порогов × 11 кадров) это
        # 297 загрузок одной и той же модели. Держим единственный экземпляр на процесс.
        engine = _rapidocr_engine(RapidOCR)
        if with_rec:
            # с распознаванием: у каждого бокса появляется score — на псевдотексте он
            # заметно выше, чем на текстуре, и это шанс развести их порогом
            raw, _ = engine(arr, use_det=True, use_cls=False, use_rec=True)
            result = [r[0] for r in (raw or [])]
            scores = [float(r[2]) for r in (raw or [])]
            texts = [str(r[1]) for r in (raw or [])]
        else:
            result, _ = engine(arr, use_det=True, use_cls=False, use_rec=False)
            scores, texts = [], []
    except Exception as exc:
        return {"has_text": False, "boxes": [], "reason": f"detect error: {exc}",
                "available": False}

    boxes: list[dict[str, Any]] = []
    for i, box in enumerate(result or []):
        pts = box.tolist() if hasattr(box, "tolist") else list(box)
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        w_ratio = (max(xs) - min(xs)) / w
        h_ratio = (max(ys) - min(ys)) / h
        area_ratio = w_ratio * h_ratio
        score = scores[i] if i < len(scores) else None
        if area_ratio < min_area_ratio:
            continue
        if score is not None and score < min_score:
            continue
        boxes.append({"h_ratio": h_ratio, "w_ratio": w_ratio, "area_ratio": area_ratio,
                      "score": score, "text": texts[i] if i < len(texts) else None})

    big = [b for b in boxes if b["h_ratio"] > big_box_ratio]
    if len(boxes) >= min_boxes:
        reason = f"{len(boxes)} text regions"
    elif big:
        reason = f"big region h={big[0]['h_ratio']:.3f}"
    else:
        reason = "clean"
    return {"has_text": bool(boxes and (len(boxes) >= min_boxes or big)),
            "boxes": boxes, "reason": reason, "available": True}


ENGINES = {"tesseract": find_text, "rapidocr": find_text_regions}

GRIDS = {
    "tesseract": [
        {"min_conf": c, "min_words": w, "big_word_ratio": r}
        for c in (40, 50, 60, 70) for w in (1, 2, 3) for r in (0.02, 0.025, 0.03)
    ],
    "rapidocr": [
        {"min_boxes": b, "big_box_ratio": r, "min_area_ratio": a}
        for b in (1, 2, 3) for r in (0.02, 0.025, 0.03)
        for a in (0.00008, 0.00015, 0.0003)
    ],
}


def calibrate(
    paths_labeled: list[tuple[str, bool]],
    grid: list[dict[str, Any]],
    engine: str = "tesseract",
) -> list[dict[str, Any]]:
    detect = ENGINES[engine]
    results: list[dict[str, Any]] = []
    for params in grid:
        tp = fn = fp = tn = 0
        for path, label in paths_labeled:
            res = detect(path, **params)
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
    parser.add_argument("--grid", action="store_true", help="Перебор порогов по сетке")
    parser.add_argument("--dump", action="store_true", help="Сырые числа по кадрам (без вердикта)")
    parser.add_argument("--engine", default="tesseract", choices=sorted(ENGINES),
                        help="tesseract = распознаватель, rapidocr = детектор регионов")
    args = parser.parse_args()

    if not args.dir and not args.file:
        parser.error("нужен --dir или --file")
    detect = ENGINES[args.engine]

    if args.file:
        res = detect(args.file)
        print(f"[{args.engine}] {'REJECT' if res['has_text'] else 'clean'}  "
              f"{Path(args.file).name}  {res['reason']}  available={res['available']}")
        for w in (res.get("words") or [])[:10]:
            print(f"    '{w['text']}' conf={w['conf']:.0f} h={w['h_ratio']:.3f}")
        for b in (res.get("boxes") or [])[:10]:
            print(f"    region h={b['h_ratio']:.3f} w={b['w_ratio']:.3f} area={b['area_ratio']:.5f}")
        return 0

    paths = scan_dir(args.dir)
    labeled = [(p, args.positive in p) for p in paths]
    print(f"файлов: {len(paths)} (позитивов по «{args.positive}»: {sum(l for _, l in labeled)})")

    if args.grid:
        results = calibrate(labeled, GRIDS[args.engine], engine=args.engine)
        keys = list(GRIDS[args.engine][0].keys())
        print(f"[{args.engine}] {'/'.join(keys)} | пойманное tp/(tp+fn) | ЛОЖНОЕ fp/(fp+tn)")
        for r in results:
            p = r["params"]
            print(f"{'/'.join(str(p[k]) for k in keys):<24} | "
                  f"{r['tp']}/{r['tp'] + r['fn']} | {r['fp']}/{r['fp'] + r['tn']}")
        best = results[0]
        print(
            f"\nbest: {best['params']} "
            f"caught={best['tp']}/{best['tp'] + best['fn']} "
            f"false={best['fp']}/{best['fp'] + best['tn']}"
        )
    elif args.dump:
        # СНАЧАЛА ДАННЫЕ, ПОТОМ ПОРОГ. Сетка 03.08 показала одинаковый результат во всех
        # 27 комбинациях — верный признак, что ручки крутятся вне разделяющей зоны.
        # Здесь печатаются сырые числа по каждому кадру, чтобы увидеть, есть ли граница.
        # 🔴 with_rec=True в прошлом прогоне дал НОЛЬ регионов на всех кадрах: RapidOCR
        # режет выдачу внутренним text_score, и распознать псевдотекст он не может по
        # определению. Поэтому дамп идёт по чистой детекции.
        print(f"{'кадр':38} {'позитив':7} {'всего':>5} {'мелких':>6} {'max_h':>6} "
              f"{'мед_h':>6} {'сум.площадь':>11}")
        for path, label in sorted(labeled, key=lambda t: not t[1]):
            res = find_text_regions(path, min_area_ratio=0.0)
            bs = res.get("boxes") or []
            hs = sorted(b["h_ratio"] for b in bs)
            # «мелкие» = похожие на настоящую надпись на предмете: бирка, лейбл, ценник.
            small = [h for h in hs if 0.008 <= h <= 0.05]
            med = hs[len(hs) // 2] if hs else 0.0
            print(f"{Path(path).name:38} {'ДА' if label else '—':7} {len(bs):>5} "
                  f"{len(small):>6} {max(hs, default=0.0):>6.3f} {med:>6.3f} "
                  f"{sum(b['area_ratio'] for b in bs):>11.5f}")
    else:
        reject = clean = 0
        for path, _ in labeled:
            res = detect(path)
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
