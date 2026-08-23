#!/usr/bin/env python3
"""art_tagger.py — zero-shot таггер артов для контентных LTX-промптов.

Зачем: LTX двигает то, что НАЗВАНО (замер 08.2026: шаблонные промпты дали 2/29
живых сцен, контентные — 5/8). Промпт строится из реального содержимого кадра,
а не из шаблона по типу плана. Техника — CLIP zero-shot на CPU раннера
(паттерн cv_probe.py): без сети, без чужих API, без квот.

Вход: плоская папка ЯД с jpg. Выход (на ЯД): tags.json
  {файл: {"objects": [[имя, скор]...], "named": [...], "painting": скор,
          "edge": энергия, "prompt": готовый контентный промпт}}
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

# Наблюдаемые формулировки — то же правило, что в cv_probe: конкретная сцена
# отвечается надёжнее абстрактного класса.
OBJECT_ANCHORS = {
    "door": ["an open doorway with a door", "a closed door in a wall"],
    "corridor": ["a long empty corridor with perspective", "an archway passage"],
    "staircase": ["a staircase with steps", "a stairwell seen from above"],
    "escalator": ["a moving escalator", "escalators in a station"],
    "train": ["a subway train on a platform", "railway tracks"],
    "boat": ["a boat on the water", "a ship on a river"],
    "window": ["large windows in a wall", "windows with light coming through"],
    "lamp": ["a street lamp post", "glowing lamps at night"],
    "tree": ["a tree with branches", "silhouettes of trees"],
    "figure": ["a single lone human figure standing", "a person silhouette walking away"],
    "smoke": ["smoke or steam rising", "chimney smoke in the air"],
    "rain": ["rain puddles reflecting light", "wet street after rain"],
    "birds": ["birds flying in the sky"],
    "bridge": ["a bridge over water", "an overpass"],
    "tower": ["a tall tower or clock tower", "city buildings skyline"],
    "interior": ["an empty office room interior", "desks and chairs in a room"],
}

# Моторные фразы под класс объекта. Это ядро лечения «пиксельного распада»:
# каждый названный объект получает ЯВНОЕ движение в промпте.
MOTION_SENTENCES = {
    "door": "the heavy door slowly swings open, spilling light through the doorway",
    "corridor": "deep perspective slowly pulls the viewer down the corridor",
    "staircase": "soft shadows crawl up the staircase steps",
    "escalator": "the escalator handrails glide steadily upward",
    "train": "a train glides past the platform, lights streaking",
    "boat": "the boat drifts gently on rippling water",
    "window": "curtains flutter softly by the windows",
    "lamp": "the street lamp flickers with a warm halo",
    "tree": "branches sway gently in the wind",
    "figure": "the lone figure walks slowly away from the camera",
    "smoke": "thin smoke rises and drifts across the frame",
    "rain": "light rain falls, puddles shimmering",
    "birds": "a few birds cross the sky slowly",
    "bridge": "traffic lights stream across the bridge",
    "tower": "clouds slide slowly behind the tower",
    "interior": "dust motes drift through the room's window light",
}
DEFAULT_MOTION = "subtle motion stirs around it"

STYLE_POS = ["a flat poster illustration or drawing artwork", "a duotone graphic art print"]
STYLE_NEG = ["a photograph taken with a camera", "a photo of a real scene"]


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def edge_energy(path: Path) -> float:
    """Средняя градиентная энергия серого изображения = структурность кадра.

    Гейт отбора: у однородных полей (туман, плоское небо) LTX'у не за что
    цепляться — заведомый брак (гипотеза с дорогой в тумане, NOW 29.07).
    """
    from PIL import Image

    gray = np.asarray(Image.open(path).convert("L").resize((256, 256)), dtype=np.float32)
    gy, gx = np.gradient(gray)
    return float(np.mean(np.abs(gx) + np.abs(gy)))


def build_prompt(scores, painting: float) -> str:
    """Контентный промпт: стиль-декларация + названные объекты с движением."""
    style_decl = (
        "A flat 2D poster illustration, duotone art style, bold graphic shapes."
        if painting > 0
        else "A moody photographic scene."
    )
    med = float(np.median([s for _, s in scores]))
    named = [k for k, s in scores[:3] if s > med]
    sentences = [MOTION_SENTENCES.get(k, DEFAULT_MOTION) for k in named[:3]]
    parts = [style_decl] + [s.capitalize() + "." for s in sentences]
    parts.append("Slow cinematic push-in, subtle filmic motion.")
    return " ".join(parts), named


def main() -> int:
    ap = argparse.ArgumentParser(description="zero-shot таггинг артов → tags.json")
    ap.add_argument("--src", required=True, help="rclone-путь к папке артов")
    ap.add_argument("--out", required=True, help="rclone-путь для tags.json")
    ap.add_argument("--model", default="ViT-B-32")
    ap.add_argument("--pretrained", default="laion2b_s34b_b79k")
    args = ap.parse_args()

    import torch
    import open_clip
    from PIL import Image

    work = Path("art_tagger_work")
    work.mkdir(exist_ok=True)
    r = sh(["rclone", "copy", args.src, str(work), "--include", "*.jpg"])
    if r.returncode != 0:
        print(r.stderr[-1000:], file=sys.stderr)
        return 1
    files = sorted(work.glob("*.jpg"))
    if not files:
        print("jpg не скачались — проверь --src", file=sys.stderr)
        return 1
    print(f"артов: {len(files)}", flush=True)

    model, _, preprocess = open_clip.create_model_and_transforms(args.model, pretrained=args.pretrained)
    model.eval()
    tokenizer = open_clip.get_tokenizer(args.model)

    def unit_text(prompts):
        with torch.no_grad():
            t = model.encode_text(tokenizer(prompts))
            return (t / t.norm(dim=-1, keepdim=True)).mean(0)

    anchors = {k: unit_text(v) for k, v in OBJECT_ANCHORS.items()}
    style_pos, style_neg = unit_text(STYLE_POS), unit_text(STYLE_NEG)

    result = {}
    for i, p in enumerate(files, 1):
        try:
            img = preprocess(Image.open(p).convert("RGB")).unsqueeze(0)
            with torch.no_grad():
                v = model.encode_image(img)
                v = (v / v.norm(dim=-1, keepdim=True))[0]
        except Exception as exc:
            print(f"  skip {p.name}: {exc}", flush=True)
            continue
        scores = sorted(((k, float(v @ a)) for k, a in anchors.items()), key=lambda x: -x[1])
        painting = float(v @ style_pos - v @ style_neg)
        prompt, named = build_prompt(scores, painting)
        result[p.name] = {
            "objects": [[k, round(s, 4)] for k, s in scores[:5]],
            "named": named,
            "painting": round(painting, 4),
            "edge": round(edge_energy(p), 2),
            "prompt": prompt,
        }
        print(f"  [{i}/{len(files)}] {p.name}: named={named} painting={painting:+.2f}", flush=True)

    out_file = work / "tags.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    r = sh(["rclone", "copyto", str(out_file), f"{args.out.rstrip('/')}/tags.json"])
    if r.returncode != 0:
        print(r.stderr[-500:], file=sys.stderr)
        return 1
    print(f"tags.json -> {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
