#!/usr/bin/env python3
"""cv_probe.py — ФАЗА 0 собственного зрения: замер CLIP zero-shot на НАШИХ размеченных кадрах.

ЗАЧЕМ. Панель VLM-судей держится на чужих free-тирах: GitHub Models сворачивают (410
`github_models_retirement_brownout`), OpenRouter даёт 429/502 по настроению, надёжный
голос остался один. Гейт бренда нельзя строить на том, что отключат.

ГИПОТЕЗА, КОТОРУЮ ЗДЕСЬ ПРОВЕРЯЕМ: генеративная VLM для наших вопросов — избыточна.
Вопросы гейта узкие и бинарные («есть лицо?», «какой тип плана?»), а на такие отвечает
CLIP-эмбеддинг + сравнение с текстовыми якорями — на CPU, без сети, без квот.
Если zero-shot уже разделяет классы, следующий шаг — линейная голова на наших вердиктах
(веса в килобайтах, обучение минуты), и внешний судья больше не нужен.

Выход: точность zero-shot по каждой задаче + сырые скоры в CSV для обучения головы.
"""

import argparse
import csv
import json
import os
import subprocess
import sys
from pathlib import Path

# Текстовые якоря: пара «за/против» на каждый вопрос гейта. Формулировки НАБЛЮДАЕМЫЕ —
# то же правило, что и в рубрике VLM: конкретный вопрос отвечается надёжнее абстрактного.
TASKS = {
    "face": (
        ["a photo of a human face", "a portrait of a person, face visible",
         "a painting of a person's face", "a sculpture of a human head"],
        ["a landscape without people", "an empty room", "a close-up of an object",
         "a texture of a surface"],
    ),
    "lone_figure": (
        ["a single lone person standing in an empty space",
         "one solitary human figure in the middle of the frame"],
        ["an empty scene with no people", "a crowd of people",
         "a close-up of an object without people"],
    ),
    "painting": (
        ["an oil painting", "a classical painting artwork", "a drawing or illustration"],
        ["a photograph taken with a camera", "a photo of a real scene"],
    ),
}
SHOT_ANCHORS = {
    "wide": "a wide establishing shot of a whole space",
    "medium": "a medium shot: an object and its surroundings",
    "detail": "a close-up shot of a single object",
    "texture": "a texture: surface, material, water, fabric, no main subject",
    "motion": "a motion-blurred frame, light trails",
}


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def load_labels(manifest: Path) -> dict:
    """Метки из манифеста пула: имя файла → что мы про него знаем (наш вердикт)."""
    data = json.loads(manifest.read_text())
    out = {}
    for it in data.get("items", []):
        reason = (it.get("reject_reason") or "")
        out[it["file"]] = {
            "ok": bool(it.get("ok")),
            "face": "face_painting" in reason or "face" in reason,
            "shot_type": it.get("shot_type"),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="CLIP zero-shot на размеченных кадрах пула")
    ap.add_argument("--src", required=True, help="папка на ЯД с кадрами (ok/ и rejected/)")
    ap.add_argument("--manifest", default="", help="MANIFEST.json там же (метки)")
    ap.add_argument("--out", default="", help="куда положить CSV со скорами (rclone-путь)")
    ap.add_argument("--model", default="ViT-B-32", help="бэкбон open_clip")
    ap.add_argument("--pretrained", default="laion2b_s34b_b79k")
    args = ap.parse_args()

    import torch
    import open_clip
    from PIL import Image

    work = Path("cv_probe_work")
    work.mkdir(exist_ok=True)
    for sub in ("ok", "rejected"):
        sh(["rclone", "copy", f"{args.src}/{sub}", str(work / sub),
            "--include", "*.jpg", "--include", "*.png"])
    if args.manifest:
        sh(["rclone", "copyto", args.manifest, str(work / "MANIFEST.json")])
    labels = load_labels(work / "MANIFEST.json") if (work / "MANIFEST.json").exists() else {}

    frames = sorted([p for sub in ("ok", "rejected") for p in (work / sub).glob("*")
                     if p.suffix.lower() in (".jpg", ".png")])
    if not frames:
        print("кадров нет — проверь --src", file=sys.stderr)
        return 1
    print(f"кадров: {len(frames)} | меток из манифеста: {len(labels)}", flush=True)

    model, _, preprocess = open_clip.create_model_and_transforms(args.model,
                                                                pretrained=args.pretrained)
    model.eval()
    tokenizer = open_clip.get_tokenizer(args.model)

    def embed_text(prompts):
        with torch.no_grad():
            t = model.encode_text(tokenizer(prompts))
            return (t / t.norm(dim=-1, keepdim=True)).mean(0)

    anchors = {name: (embed_text(pos), embed_text(neg)) for name, (pos, neg) in TASKS.items()}
    shots = {k: embed_text([v]) for k, v in SHOT_ANCHORS.items()}

    rows = []
    for i, p in enumerate(frames, 1):
        try:
            img = preprocess(Image.open(p).convert("RGB")).unsqueeze(0)
        except Exception as exc:
            print(f"  ⚠️ {p.name}: {exc}", flush=True)
            continue
        with torch.no_grad():
            v = model.encode_image(img)
            v = (v / v.norm(dim=-1, keepdim=True))[0]
        row = {"file": p.name, "folder": p.parent.name}
        for name, (pos, neg) in anchors.items():
            row[name] = float(v @ pos - v @ neg)          # >0 → скорее «да»
        row["shot_pred"] = max(shots, key=lambda k: float(v @ shots[k]))
        # Имя файла — единственный носитель типа плана, переживающий переливку папок
        row["shot_true"] = p.name.split("__")[0].split("_")[-1] if "__" in p.name else ""
        lab = labels.get(p.name) or labels.get(p.name.split("__", 2)[-1]) or {}
        row["label_face"] = int(bool(lab.get("face"))) if lab else ""
        rows.append(row)
        if i % 10 == 0:
            print(f"  [{i}/{len(frames)}]", flush=True)

    csv_path = work / "cv_probe.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # Разделяет ли скор наши классы — единственный вопрос фазы 0.
    print("\n=== ЛИЦО: скор по кадрам, которые МЫ пометили ===")
    known = [r for r in rows if r["label_face"] != ""]
    pos = [r for r in known if r["label_face"] == 1]
    neg = [r for r in known if r["label_face"] == 0]
    for r in sorted(rows, key=lambda r: -r["face"])[:6]:
        print(f"  {r['face']:+.3f}  {r['file'][:58]}")
    print("  … хвост:")
    for r in sorted(rows, key=lambda r: r["face"])[:3]:
        print(f"  {r['face']:+.3f}  {r['file'][:58]}")
    if pos and neg:
        print(f"\n  средний скор: помеченные лицом {sum(r['face'] for r in pos)/len(pos):+.3f} | "
              f"остальные {sum(r['face'] for r in neg)/len(neg):+.3f}")

    print("\n=== ЖИВОПИСЬ (отдельный признак — VLM его путал) ===")
    for r in sorted(rows, key=lambda r: -r["painting"])[:5]:
        print(f"  {r['painting']:+.3f}  {r['file'][:58]}")

    print("\n=== ТИП ПЛАНА: zero-shot против имени файла ===")
    hit = sum(1 for r in rows if r["shot_true"] and r["shot_pred"] == r["shot_true"])
    tot = sum(1 for r in rows if r["shot_true"])
    print(f"  совпало {hit}/{tot}")

    if args.out:
        sh(["rclone", "copyto", str(csv_path), f"{args.out}/cv_probe.csv"])
        print(f"\nCSV → {args.out}/cv_probe.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
