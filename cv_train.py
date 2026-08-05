#!/usr/bin/env python3
"""cv_train.py — обучение СВОЕЙ головы поверх CLIP: «лицо нефотографической природы».

ПОЧЕМУ ИМЕННО ЭТА ЗАДАЧА ПЕРВАЯ. Фотолицо у нас уже ловит YOLOv8-face (`source_qc`),
а вот портрет МАСЛОМ он не берёт by design — обучен на фотографиях. Именно так портрет
XVII века прошёл три слоя гейтов и уехал в пул (04.08). VLM-панель его тоже пропустила.
Дыра узкая, значит и модель нужна узкая.

ГОТОВАЯ РАЗМЕТКА ВМЕСТО РУЧНОЙ. `huggan/wikiart` (81k работ) размечен по жанрам:
`portrait` = лицо крупно, `landscape`/`still_life`/`cityscape` = его гарантированно нет.
Это ровно наши два класса, размеченные искусствоведами, — размечать руками нечего.
Берём срез стримингом, всю базу (5.3 ГБ) не качаем.

ЧТО НА ВЫХОДЕ. Логистическая регрессия на 512-мерных векторах CLIP: веса — килобайты,
инференс на CPU без единого обращения наружу. Плюс эмбеддинги НАШИХ кадров — чтобы
следующие головы (тип плана, одинокая фигура) обучались без повторного прогона картинок.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

POS_GENRES = {"portrait"}                                    # лицо есть
NEG_GENRES = {"landscape", "cityscape", "still_life", "abstract_painting"}   # лица нет


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def embed_folder(model, preprocess, paths, log_every=25):
    """Картинки → матрица нормированных векторов CLIP."""
    import torch
    from PIL import Image
    out, kept = [], []
    for i, p in enumerate(paths, 1):
        try:
            img = preprocess(Image.open(p).convert("RGB")).unsqueeze(0)
        except Exception as exc:
            print(f"  ⚠️ {Path(p).name}: {exc}", flush=True)
            continue
        with torch.no_grad():
            v = model.encode_image(img)
        out.append((v / v.norm(dim=-1, keepdim=True))[0].numpy())
        kept.append(str(p))
        if i % log_every == 0:
            print(f"  [{i}/{len(paths)}]", flush=True)
    return (np.stack(out) if out else np.zeros((0, 512), dtype=np.float32)), kept


def fit_logreg(X, y, epochs=400, lr=0.5, l2=1e-3):
    """Логрег вручную: sklearn на раннер тянуть незачем ради одного слоя на 512 входов."""
    n, d = X.shape
    w = np.zeros(d, dtype=np.float64)
    b = 0.0
    for _ in range(epochs):
        z = X @ w + b
        p = 1.0 / (1.0 + np.exp(-z))
        gw = X.T @ (p - y) / n + l2 * w
        gb = float(np.mean(p - y))
        w -= lr * gw
        b -= lr * gb
    return w, b


def predict(X, w, b):
    return 1.0 / (1.0 + np.exp(-(X @ w + b)))


def main() -> int:
    ap = argparse.ArgumentParser(description="Обучить голову «лицо на живописи» поверх CLIP")
    ap.add_argument("--pools", default="", help="папки на ЯД через ';' (наши кадры)")
    ap.add_argument("--out", required=True, help="куда сложить веса и эмбеддинги (ЯД)")
    ap.add_argument("--per-class", type=int, default=400, help="сколько картин на класс из WikiArt")
    ap.add_argument("--model", default="ViT-B-32")
    ap.add_argument("--pretrained", default="laion2b_s34b_b79k")
    args = ap.parse_args()

    import open_clip
    from datasets import load_dataset

    model, _, preprocess = open_clip.create_model_and_transforms(args.model,
                                                                pretrained=args.pretrained)
    model.eval()

    # ── 1. Обучающий лист из WikiArt (стримингом, без скачивания 5.3 ГБ) ──────────
    print(f"WikiArt: тяну по {args.per_class} на класс (стримингом)…", flush=True)
    ds = load_dataset("huggan/wikiart", split="train", streaming=True)
    genre_names = None
    pos_imgs, neg_imgs = [], []
    seen = 0
    for row in ds:
        seen += 1
        if genre_names is None:
            # ClassLabel int → имя жанра; берём схему из самого датасета, не из памяти
            genre_names = ds.features["genre"].names
        g = genre_names[row["genre"]] if isinstance(row["genre"], int) else str(row["genre"])
        if g in POS_GENRES and len(pos_imgs) < args.per_class:
            pos_imgs.append(row["image"])
        elif g in NEG_GENRES and len(neg_imgs) < args.per_class:
            neg_imgs.append(row["image"])
        if len(pos_imgs) >= args.per_class and len(neg_imgs) >= args.per_class:
            break
        if seen % 2000 == 0:
            print(f"  просмотрено {seen}: portrait={len(pos_imgs)} прочие={len(neg_imgs)}",
                  flush=True)
    print(f"обучающий лист: portrait={len(pos_imgs)} прочие={len(neg_imgs)} "
          f"(просмотрено строк: {seen})", flush=True)
    if len(pos_imgs) < 20 or len(neg_imgs) < 20:
        print("слишком мало примеров — обучать нечего", file=sys.stderr)
        return 1

    import torch
    def embed_pils(pils):
        out = []
        for i, im in enumerate(pils, 1):
            with torch.no_grad():
                v = model.encode_image(preprocess(im.convert("RGB")).unsqueeze(0))
            out.append((v / v.norm(dim=-1, keepdim=True))[0].numpy())
            if i % 100 == 0:
                print(f"  эмбеддинг {i}/{len(pils)}", flush=True)
        return np.stack(out)

    Xp, Xn = embed_pils(pos_imgs), embed_pils(neg_imgs)
    X = np.concatenate([Xp, Xn]).astype(np.float64)
    y = np.concatenate([np.ones(len(Xp)), np.zeros(len(Xn))])

    # Честная проверка: держим отложенную выборку, а не хвалимся точностью на обучении
    rng = np.random.default_rng(2013)
    idx = rng.permutation(len(X))
    X, y = X[idx], y[idx]
    cut = int(len(X) * 0.8)
    w, b = fit_logreg(X[:cut], y[:cut])
    pv = predict(X[cut:], w, b)
    acc = float(np.mean((pv > 0.5) == (y[cut:] > 0.5)))
    tp = int(np.sum((pv > 0.5) & (y[cut:] == 1)))
    fp = int(np.sum((pv > 0.5) & (y[cut:] == 0)))
    fn = int(np.sum((pv <= 0.5) & (y[cut:] == 1)))
    print(f"\n=== ОТЛОЖЕННАЯ ВЫБОРКА ({len(X) - cut} шт) ===")
    print(f"точность {acc:.3f} | поймано портретов {tp} | ложных {fp} | пропущено {fn}")

    # ── 2. Наши кадры: как голова видит реальный пул ─────────────────────────────
    work = Path("cv_train_work")
    work.mkdir(exist_ok=True)
    ours: list[Path] = []
    for i, pool in enumerate([p.strip() for p in args.pools.split(";") if p.strip()]):
        dst = work / f"pool{i}"
        dst.mkdir(exist_ok=True)
        # Без ограничения глубины: арты пулов лежат в подпапках set_1…set_7, и с
        # `--max-depth 2` прогон 05.08 забрал только плоские папки (43 из 133 кадров).
        sh(f'rclone copy "{pool}" "{dst}" --include "*.jpg" --include "*.png"')
        ours += sorted(dst.rglob("*.jpg")) + sorted(dst.rglob("*.png"))
    print(f"\nнаших кадров: {len(ours)}", flush=True)

    report = []
    if ours:
        Xo, kept = embed_folder(model, preprocess, ours)
        po = predict(Xo.astype(np.float64), w, b)
        order = np.argsort(-po)
        print("\n=== НАШИ КАДРЫ: верх рейтинга «здесь лицо» ===")
        for j in order[:12]:
            print(f"  {po[j]:.3f}  {Path(kept[j]).name[:60]}")
        print("  … низ:")
        for j in order[-3:]:
            print(f"  {po[j]:.3f}  {Path(kept[j]).name[:60]}")
        report = [{"file": Path(kept[j]).name, "p_face": float(po[j])} for j in order]
        np.savez_compressed(work / "ours_embeddings.npz",
                            X=Xo.astype(np.float32), files=np.array(kept))

    np.savez(work / "head_face_painting.npz", w=w.astype(np.float32), b=np.float32(b),
             model=args.model, pretrained=args.pretrained, holdout_acc=acc)
    (work / "REPORT.json").write_text(json.dumps(
        {"holdout": {"n": len(X) - cut, "acc": acc, "tp": tp, "fp": fp, "fn": fn},
         "train": {"portrait": len(Xp), "other": len(Xn)},
         "ours": report}, ensure_ascii=False, indent=1))

    for f in ("head_face_painting.npz", "ours_embeddings.npz", "REPORT.json"):
        if (work / f).exists():
            sh(f'rclone copyto "{work / f}" "{args.out}/{f}"')
    print(f"\nвеса и эмбеддинги → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
