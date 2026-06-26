#!/usr/bin/env python3
"""
aesthetic_check.py — ПРОВЕРКА ГИПОТЕЗЫ yaromat (2026-06-26):
совпадает ли оценка improved-aesthetic-predictor (эстетика 0-10) с его разметкой
«пластмассовости» (plastic_pct 0-100) на 19 реальных размеченных кадрах.

Если aesthetic ловит пластик → отрицательная корреляция (пластик↑ → эстетика↓).
Если ортогонально (наша гипотеза) → корреляция ≈ 0.

Модель: CLIP ViT-L/14 (openai) → L2-norm embedding → MLP (sac+logos+ava1-l14-linearMSE.pth).
Вход: ЯД cloud_io/plastic_aesthetic/{frames/*.jpg, labels.csv}. Всё на GH (CPU).
Выход: cloud_io/plastic_aesthetic/result/{scores.csv, summary.md}.
"""
import os, csv, subprocess, tempfile, glob, sys
import numpy as np

YD = "ydrive:Content factory/cloud_io/plastic_aesthetic"
WORK = tempfile.mkdtemp(prefix="aest_")
FRAMES = os.path.join(WORK, "frames")

def sh(c): return subprocess.run(c, shell=True, capture_output=True, text=True)

# ── 1. забрать данные с ЯД ──
sh(f'rclone copy "{YD}/frames" "{FRAMES}" --transfers 8')
sh(f'rclone copyto "{YD}/labels.csv" "{WORK}/labels.csv"')
labels = {}
with open(f"{WORK}/labels.csv") as fh:
    for row in csv.DictReader(fh):
        labels[row["file"]] = (int(row["plastic_pct"]), row["gen"])
print(f"меток: {len(labels)}, кадров: {len(glob.glob(FRAMES+'/*.jpg'))}")

# ── 2. модель: CLIP ViT-L/14 + MLP aesthetic-predictor ──
import torch, torch.nn as nn, open_clip
from PIL import Image

class MLP(nn.Module):
    def __init__(self, in_dim=768):
        super().__init__()
        self.layers = nn.Sequential(
            nn.Linear(in_dim, 1024), nn.Dropout(0.2),
            nn.Linear(1024, 128), nn.Dropout(0.2),
            nn.Linear(128, 64), nn.Dropout(0.1),
            nn.Linear(64, 16),
            nn.Linear(16, 1),
        )
    def forward(self, x): return self.layers(x)

WEIGHTS = "sac+logos+ava1-l14-linearMSE.pth"
if not os.path.exists(WEIGHTS):
    sh(f'wget -q "https://github.com/christophschuhmann/improved-aesthetic-predictor/raw/main/{WEIGHTS}" -O "{WEIGHTS}"')

mlp = MLP(768)
mlp.load_state_dict(torch.load(WEIGHTS, map_location="cpu"))
mlp.eval()
model, _, preprocess = open_clip.create_model_and_transforms("ViT-L-14", pretrained="openai")
model.eval()

def aesthetic(path):
    img = preprocess(Image.open(path).convert("RGB")).unsqueeze(0)
    with torch.no_grad():
        emb = model.encode_image(img)
        emb = emb / emb.norm(dim=-1, keepdim=True)
        return float(mlp(emb.float()).item())

# ── 3. score каждый кадр ──
rows = []
for f, (pct, gen) in sorted(labels.items()):
    p = os.path.join(FRAMES, f)
    if not os.path.exists(p):
        print(f"  ! нет кадра {f}"); continue
    a = aesthetic(p)
    rows.append({"file": f, "gen": gen, "plastic_pct": pct, "aesthetic": round(a, 3)})
    print(f"  {f:16} пластик={pct:3}%  aesthetic={a:.2f}")

# ── 4. корреляции ──
def spearman(x, y):
    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0]*len(v)
        for pos, i in enumerate(order): r[i] = pos
        return r
    rx, ry = rank(x), rank(y)
    n = len(x)
    d2 = sum((a-b)**2 for a, b in zip(rx, ry))
    return 1 - 6*d2/(n*(n*n-1)) if n > 1 else 0.0

def pearson(x, y):
    x = np.array(x); y = np.array(y)
    if x.std() == 0 or y.std() == 0: return 0.0
    return float(np.corrcoef(x, y)[0,1])

def auc(good, bad):  # P(good_aesthetic > bad_aesthetic)
    if not good or not bad: return None
    w = sum(1.0 if g > b else 0.5 if g == b else 0.0 for g in good for b in bad)
    return w/(len(good)*len(bad))

pct = [r["plastic_pct"] for r in rows]
aes = [r["aesthetic"] for r in rows]
sp = spearman(pct, aes); pe = pearson(pct, aes)
# бинарно: пластик>=50 = "плохо"; ловит ли aesthetic? (у "хороших" aesthetic должен быть ВЫШЕ)
good_aes = [r["aesthetic"] for r in rows if r["plastic_pct"] < 50]
bad_aes  = [r["aesthetic"] for r in rows if r["plastic_pct"] >= 50]
a_auc = auc(good_aes, bad_aes)

# ── 5. отчёт ──
csv_p = os.path.join(WORK, "scores.csv")
with open(csv_p, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=["file","gen","plastic_pct","aesthetic"]); w.writeheader(); w.writerows(rows)

L = ["# Aesthetic-predictor vs разметка пластмассовости yaromat",
     f"\nКадров: {len(rows)} (хороших <50%: {len(good_aes)}, пластик ≥50%: {len(bad_aes)})",
     "\n## Корреляция (пластик_% ↔ aesthetic-score)",
     f"- **Spearman:** {sp:+.3f}",
     f"- **Pearson:**  {pe:+.3f}",
     f"- **AUC (aesthetic у «хороших» > у «пластика»):** {a_auc if a_auc is None else round(a_auc,3)}",
     "\n## Как читать",
     "- Если aesthetic ЛОВИТ пластик → Spearman сильно ОТРИЦАТЕЛЬНЫЙ (пластик↑→эстетика↓), AUC→1.0.",
     "- Если ОРТОГОНАЛЕН (наша гипотеза) → Spearman ≈ 0 (±0.3), AUC ≈ 0.5.",
     "\n## Средний aesthetic по группам",
     f"- хорошие (<50% пластик): {np.mean(good_aes):.2f}" if good_aes else "- хорошие: —",
     f"- пластик (≥50%): {np.mean(bad_aes):.2f}" if bad_aes else "- пластик: —",
     "\n## Таблица (отсортировано по пластику)"]
L.append("| кадр | ген | пластик% | aesthetic |")
L.append("|---|---|---|---|")
for r in sorted(rows, key=lambda r: r["plastic_pct"]):
    L.append(f"| {r['file']} | {r['gen']} | {r['plastic_pct']} | {r['aesthetic']} |")
verdict = ("aesthetic ОРТОГОНАЛЕН пластмассовости — не годится как детектор (подтверждает гипотезу)"
           if abs(sp) < 0.35 else
           ("aesthetic ОТРИЦАТЕЛЬНО коррелирует — частично ловит пластик!" if sp < 0 else
            "aesthetic ПОЛОЖИТЕЛЬНО коррелирует — пластик оценивается как красивый (обратное детектору)"))
L.append(f"\n## Вывод\n**{verdict}**")
summ = "\n".join(L)
sp_p = os.path.join(WORK, "summary.md")
open(sp_p, "w").write(summ)
print("\n"+summ)
sh(f'rclone copy "{csv_p}" "{YD}/result/"')
sh(f'rclone copy "{sp_p}" "{YD}/result/"')
print(f"\n✓ результат → {YD}/result/")
