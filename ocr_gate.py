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
    min_small: int = 2,
    small_h_lo: float = 0.008,
    small_h_hi: float = 0.05,
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

    # 🔑 ЧТО ПОКАЗАЛ ЗАМЕР НА ЛИСТЕ A (run 30810539477) — решает РАЗМЕР, а не количество:
    #   child.png (надпись)      2 региона, ОБА мелкие h=0.034
    #   anchor/cold_02/crowd     по одному региону h=0.56 / 0.34 / 0.23 — это куски кадра
    #   cold_03                  один регион h=0.105 — тоже не надпись
    #   art1                     один мелкий h=0.047 — РЕАЛЬНЫЙ номер машины вдали,
    #                            который yaromat разрешил; одного мелкого мало для REJECT
    # Отсюда правило: считаем ТОЛЬКО мелкие регионы и требуем ДВА.
    small = [b for b in boxes if small_h_lo <= b["h_ratio"] <= small_h_hi]
    big = [b for b in boxes if b["h_ratio"] > big_box_ratio]
    has_text = len(small) >= min_small
    if has_text:
        hs = "/".join(f"{b['h_ratio']:.3f}" for b in small[:3])
        reason = f"{len(small)} мелких регионов h={hs}"
    else:
        reason = f"clean (мелких {len(small)}, всего {len(boxes)})"
    return {"has_text": has_text, "boxes": boxes, "small": small, "big": big,
            "reason": reason, "available": True}


ENGINES = {"tesseract": find_text, "rapidocr": find_text_regions}

# ── ПРОФИЛИ ПОРОГОВ ПОД РАЗНЫЙ МАТЕРИАЛ ────────────────────────────────────────────
# Один порог на всё — ошибка, и она уже стоила замера: правило «два мелких региона»
# выведено на AI-АРТАХ, где надпись это псевдотекст (мелкие закорючки на бирке).
# У стокового кадра брак ДРУГОЙ ПРИРОДЫ: настоящая читаемая надпись, часто крупная —
# дорожный знак «USE LOWER GEARS» на стилле 29.07 прошёл бы правило «мелких» насквозь.
# Поэтому профиль выбирается точкой встройки, а не угадывается на месте вызова.
PROFILES: dict[str, dict[str, Any]] = {
    # арт из пула: замер на листе A (run 30811081122) — 1/1 пойманного, 0/10 ложных.
    # Слово НЕ проверяем: псевдотекст не читается, распознавание там даёт ноль регионов.
    "art": {"min_small": 2, "small_h_lo": 0.008, "small_h_hi": 0.05},
    # сток/стилл: замер на 24 кадрах Openverse (run 30911422434). Правило «двух мелких»
    # ловит знаки и таблички, но КРУПНУЮ надпись теряет — «MIND THE GAP» во весь кадр
    # (h=0.318) прошло чистым. Добавлено второе условие ПО СЛОВУ: на стоке текст
    # настоящий и читается (score 0.94–0.98), в отличие от псевдотекста на артах.
    "still": {"min_small": 2, "small_h_lo": 0.008, "small_h_hi": 0.05,
              "min_word_len": 3, "min_word_score": 0.8},
}

# Порог по слову вынесен в константы, чтобы не расползался по коду
WORD_LEN_DEFAULT = 3
WORD_SCORE_DEFAULT = 0.8


def find_words(path: str, *, min_word_len: int = WORD_LEN_DEFAULT,
               min_word_score: float = WORD_SCORE_DEFAULT, **kw: Any) -> dict[str, Any]:
    """Прочитанные СЛОВА в кадре (распознавание). Отдельным проходом — намеренно.

    RapidOCR с `use_rec=True` режет выдачу внутренним text_score, и набор боксов
    получается ДРУГОЙ: на `wet_asphalt_night__2` детекция без распознавания дала
    5 регионов, с распознаванием — ноль. Поэтому правило «мелких регионов» и правило
    «прочитанного слова» нельзя считать за один проход: они смотрят разное.
    """
    res = find_text_regions(path, with_rec=True, min_area_ratio=0.0, **kw)
    if not res.get("available"):
        return {"available": False, "words": [], "reason": res.get("reason")}
    words = []
    for b in res.get("boxes") or []:
        text = (b.get("text") or "").strip()
        score = b.get("score") or 0.0
        letters = "".join(ch for ch in text if ch.isalnum())
        # Одиночный символ — шум текстуры, а не надпись: на абстрактном мазке
        # прочиталось 'S'(0.82), на манускрипте '9'(0.74). Слово начинается с трёх.
        if len(letters) >= min_word_len and score >= min_word_score:
            words.append({"text": text, "score": score, "h_ratio": b["h_ratio"]})
    return {"available": True, "words": words,
            "reason": "; ".join(f"{w['text']!r}({w['score']:.2f})" for w in words[:3])}


def gate(path: str, *, profile: str = "art", **overrides: Any) -> dict[str, Any]:
    """Боевая точка входа: вердикт по кадру с порогами нужного профиля.

    Возвращает то же, что `find_text_regions`, плюс `profile`. Вызывающему остаётся
    смотреть `available` (гейт вообще отработал?) и `has_text` (есть ли надпись).
    """
    params = dict(PROFILES.get(profile, PROFILES["art"]))
    params.update(overrides)
    word_len = params.pop("min_word_len", None)
    word_score = params.pop("min_word_score", None)

    res = find_text_regions(path, **params)
    res["profile"] = profile
    if res.get("has_text") or word_len is None or not res.get("available"):
        return res

    # Регионы чисты — но крупная читаемая надпись сюда и не попадает. Второй проход.
    w = find_words(path, min_word_len=word_len, min_word_score=word_score)
    res["words"] = w.get("words") or []
    if w.get("available") and res["words"]:
        res["has_text"] = True
        res["reason"] = f"прочитано: {w['reason']}"
    return res

GRIDS = {
    "tesseract": [
        {"min_conf": c, "min_words": w, "big_word_ratio": r}
        for c in (40, 50, 60, 70) for w in (1, 2, 3) for r in (0.02, 0.025, 0.03)
    ],
    "rapidocr": [
        {"min_small": n, "small_h_lo": lo, "small_h_hi": hi}
        for n in (1, 2, 3) for lo in (0.005, 0.008, 0.012)
        for hi in (0.04, 0.05, 0.06)
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
    parser.add_argument("--rec", action="store_true",
                        help="дамп С РАСПОЗНАВАНИЕМ: что именно прочиталось в регионе")
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
    elif args.rec:
        # 🔑 ЗАЧЕМ ОТДЕЛЬНЫЙ РЕЖИМ: на АРТАХ распознавание дало ноль регионов, и это
        # логично — псевдотекст не читается по определению. Но на СТОКЕ надпись
        # настоящая («MIND THE GAP», «USE LOWER GEARS»), и распознавание обязано её
        # брать — включая КРУПНУЮ, которую правило «двух мелких» пропускает.
        # Здесь печатается, что именно прочиталось, чтобы порог ставить по словам.
        print(f"{'кадр':38} {'регионов':>8}  прочитанное (score)")
        for path, _ in sorted(labeled):
            res = find_text_regions(path, min_area_ratio=0.0, with_rec=True)
            bs = res.get("boxes") or []
            got = [f"{(b.get('text') or '').strip()!r}({b['score']:.2f},h={b['h_ratio']:.3f})"
                   for b in bs if (b.get("text") or "").strip()]
            print(f"{Path(path).name:38} {len(bs):>8}  {'; '.join(got[:6]) or '—'}")
    elif args.dump:
        # СНАЧАЛА ДАННЫЕ, ПОТОМ ПОРОГ. Сетка 03.08 показала одинаковый результат во всех
        # 27 комбинациях — верный признак, что ручки крутятся вне разделяющей зоны.
        # Здесь печатаются сырые числа по каждому кадру, чтобы увидеть, есть ли граница.
        # 🔴 with_rec=True в прошлом прогоне дал НОЛЬ регионов на всех кадрах: RapidOCR
        # режет выдачу внутренним text_score, и распознать псевдотекст он не может по
        # определению. Поэтому дамп идёт по чистой детекции.
        print(f"{'кадр':38} {'позитив':7} {'всего':>5} {'мелких':>6} {'строк':>5} "
              f"{'max_h':>6} {'мед_h':>6} {'сум.площадь':>11}")
        for path, label in sorted(labeled, key=lambda t: not t[1]):
            res = find_text_regions(path, min_area_ratio=0.0)
            bs = res.get("boxes") or []
            hs = sorted(b["h_ratio"] for b in bs)
            # «мелкие» = похожие на настоящую надпись на предмете: бирка, лейбл, ценник.
            small = [h for h in hs if 0.008 <= h <= 0.05]
            # «строки» = вытянутые по горизонтали регионы ЛЮБОГО размера. Мелкие ловят
            # псевдотекст AI-артов, но на СТОКЕ брак другой — крупная читаемая надпись
            # (дорожный знак 29.07). Форма — единственное, чем она отличается от куска
            # кадра: строка широкая и плоская, кусок кадра ближе к квадрату.
            lines = [b for b in bs if b["h_ratio"] > 0 and b["w_ratio"] / b["h_ratio"] >= 3.0]
            med = hs[len(hs) // 2] if hs else 0.0
            print(f"{Path(path).name:38} {'ДА' if label else '—':7} {len(bs):>5} "
                  f"{len(small):>6} {len(lines):>5} {max(hs, default=0.0):>6.3f} "
                  f"{med:>6.3f} {sum(b['area_ratio'] for b in bs):>11.5f}")
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
