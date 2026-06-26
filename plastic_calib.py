#!/usr/bin/env python3
"""
plastic_calib.py — КАЛИБРОВОЧНЫЙ прогон детекторов "пластмассовости" AI-генерации.

ЦЕЛЬ (yaromat 2026-06-26): генерация "отдаёт пластмассой". Прежде чем встраивать фильтр
в пайплайн — эмпирически проверить, КАКАЯ метрика реально разделяет "пластик" и "ок"
на НАШИХ кадрах, размеченных вердиктами yaromat. Не гадать про пороги — измерить.

Метки (CALIB_SET ниже, легко править):
  bad  = "пластик"      : flux-пул билета ("дешевизна"), Qwen ("сильнее пластмассой")
  good = "ориентир-ОК"  : VeoFree ("менее пластмассовый")

Метрики-кандидаты (все CPU, считаются на кадр):
  fft_hf_ratio  — доля энергии высоких частот (AI сглаживает → ниже у пластика)
  lap_var       — variance of Laplacian, детализация/резкость (пластик гладкий → ниже)
  texture_std   — средний локальный контраст текстур (пластик однороден → ниже)
  sat_mean      — насыщенность HSV (пластик часто пересыщен → выше) — диагностика
  brisque       — no-reference IQA (piq); ВНИМАНИЕ: штрафует шум/зерно → может быть ОБРАТНА
  clip_plastic  — CLIP zero-shot: cos("glossy plastic 3d render, cgi") − cos("analog film photo, grain")
                  выше = более пластмассово (самый прямой к восприятию)

Выход: cloud_io/plastic_calib/<ts>/metrics.csv + summary.md (разделимость each метрики: AUC + mean good/bad).
Всё в облаке (GH Actions). Бук не трогаем.
"""
import os, sys, csv, glob, json, math, shutil, subprocess, datetime, tempfile

YD = "ydrive:Content factory/cloud_io"
TS = datetime.datetime.utcnow().strftime("%Y-%m-%d_%H%M%S")
OUT_YD = f"{YD}/plastic_calib/{TS}"
WORK = tempfile.mkdtemp(prefix="calib_")
FRAMES = os.path.join(WORK, "frames")
os.makedirs(FRAMES, exist_ok=True)

# --- размеченный набор: (yd-папка ОТНОСИТЕЛЬНО cloud_io, glob, label, source, media) ---
# label: 1 = good (ориентир-ОК), 0 = bad (пластик). media: png|mp4
CALIB_SET = [
    {"dir": "render_jobs/2026-06-25_taste_bilet_square", "glob": "*.png", "label": 0, "source": "flux_bilet", "media": "png"},
    {"dir": "qwen_pool/2026-06-26",                        "glob": "*.png", "label": 0, "source": "qwen",       "media": "png"},
    {"dir": "render_jobs/2026-06-26_qwen_landscape_test",  "glob": "*.mp4", "label": 0, "source": "qwen_vid",   "media": "mp4"},
    {"dir": "veofree_pool/2026-06-22",                     "glob": "*.mp4", "label": 1, "source": "veofree",    "media": "mp4"},
    {"dir": "veofree_pool/2026-06-26",                     "glob": "vid_*.mp4", "label": 1, "source": "veofree", "media": "mp4"},
    {"dir": "render_jobs/2026-06-26_veofree_vertical_test","glob": "*.mp4", "label": 1, "source": "veofree_vid","media": "mp4"},
]

def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)

def pull_group(entry):
    """Скачивает файлы группы локально, возвращает список локальных путей к КАДРАМ (png).
    Для mp4 извлекает 3 кадра (20/50/80%)."""
    src = f"{YD}/{entry['dir']}"
    local = os.path.join(WORK, entry["dir"].replace("/", "__"))
    os.makedirs(local, exist_ok=True)
    r = sh(f'rclone copy "{src}" "{local}" --include "{entry["glob"]}" --transfers 6')
    if r.returncode != 0:
        print(f"  ! rclone copy fail {src}: {r.stderr[:200]}")
    found = sorted(glob.glob(os.path.join(local, entry["glob"])))
    frames = []
    for f in found:
        if entry["media"] == "png":
            frames.append(f)
        else:  # mp4 → извлечь кадры
            dur = video_duration(f)
            base = os.path.splitext(os.path.basename(f))[0]
            for pct in (0.2, 0.5, 0.8):
                t = max(0.0, dur * pct)
                out = os.path.join(FRAMES, f"{entry['source']}__{base}__{int(pct*100)}.png")
                rr = sh(f'ffmpeg -nostdin -y -ss {t:.2f} -i "{f}" -frames:v 1 -q:v 2 "{out}" 2>/dev/null')
                if os.path.exists(out):
                    frames.append(out)
    return frames

def video_duration(path):
    r = sh(f'ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "{path}"')
    try:
        return float(r.stdout.strip())
    except Exception:
        return 8.0

# ---------------- метрики ----------------
import numpy as np
from PIL import Image
try:
    import cv2
except Exception:
    cv2 = None

def load_gray(path, maxside=512):
    img = Image.open(path).convert("L")
    w, h = img.size
    s = maxside / max(w, h)
    if s < 1:
        img = img.resize((int(w*s), int(h*s)))
    return np.asarray(img, dtype=np.float32)

def load_rgb(path, maxside=512):
    img = Image.open(path).convert("RGB")
    w, h = img.size
    s = maxside / max(w, h)
    if s < 1:
        img = img.resize((int(w*s), int(h*s)))
    return img

def m_fft_hf_ratio(g):
    f = np.fft.fftshift(np.fft.fft2(g))
    mag = np.abs(f)
    h, w = g.shape
    cy, cx = h // 2, w // 2
    r = min(h, w) // 8  # low-freq радиус
    Y, X = np.ogrid[:h, :w]
    low = ((Y-cy)**2 + (X-cx)**2) <= r*r
    total = mag.sum() + 1e-9
    hf = mag[~low].sum()
    return float(hf / total)

def m_lap_var(g):
    if cv2 is not None:
        return float(cv2.Laplacian(g, cv2.CV_32F).var())
    # numpy fallback (дискретный лапласиан)
    lap = (-4*g + np.roll(g,1,0)+np.roll(g,-1,0)+np.roll(g,1,1)+np.roll(g,-1,1))
    return float(lap.var())

def m_texture_std(g):
    # средний локальный std в окне 7x7 (вариативность текстуры)
    if cv2 is not None:
        mean = cv2.blur(g, (7,7))
        sq = cv2.blur(g*g, (7,7))
        var = np.clip(sq - mean*mean, 0, None)
        return float(np.sqrt(var).mean())
    return float(g.std())

def m_sat_mean(path):
    img = np.asarray(load_rgb(path), dtype=np.float32) / 255.0
    mx = img.max(2); mn = img.min(2)
    sat = np.where(mx > 0, (mx-mn)/(mx+1e-9), 0)
    return float(sat.mean())

def m_rgb_corr(path):
    # межканальная корреляция RGB (mimo-ревью): пластик/CGI = каналы сильно
    # коррелированы (монотонная окраска → ближе к 1); натуральное фото — независимее.
    rgb = np.asarray(load_rgb(path), dtype=np.float32).reshape(-1, 3)
    c = np.corrcoef(rgb, rowvar=False)
    return float((c[0,1] + c[0,2] + c[1,2]) / 3.0)

# BRISQUE + CLIP — ленивая инициализация (тяжёлые)
_piq = None
def m_brisque(path):
    global _piq
    try:
        import torch, piq
        from torchvision import transforms
        if _piq is None:
            _piq = (torch, piq, transforms.ToTensor())
        torch, piq, to_t = _piq
        img = load_rgb(path)
        t = to_t(img).unsqueeze(0)
        with torch.no_grad():
            return float(piq.brisque(t, data_range=1.0))
    except Exception as e:
        return None

_clip = None
def m_clip_plastic(path):
    global _clip
    try:
        import torch, open_clip
        if _clip is None:
            model, _, preprocess = open_clip.create_model_and_transforms(
                "ViT-B-32", pretrained="laion2b_s34b_b79k")
            tok = open_clip.get_tokenizer("ViT-B-32")
            model.eval()
            pos = tok(["a glossy plastic 3d render, cgi, artificial, smooth"])
            neg = tok(["an analog film photograph, natural texture, grain, realistic"])
            with torch.no_grad():
                tp = model.encode_text(pos); tp /= tp.norm(dim=-1, keepdim=True)
                tn = model.encode_text(neg); tn /= tn.norm(dim=-1, keepdim=True)
            _clip = (torch, model, preprocess, tp, tn)
        torch, model, preprocess, tp, tn = _clip
        img = preprocess(load_rgb(path)).unsqueeze(0)
        with torch.no_grad():
            f = model.encode_image(img); f /= f.norm(dim=-1, keepdim=True)
            return float((f @ tp.T).item() - (f @ tn.T).item())
    except Exception as e:
        print(f"  ! clip fail: {e}")
        return None

METRICS = ["fft_hf_ratio", "lap_var", "texture_std", "sat_mean", "rgb_corr", "brisque", "clip_plastic"]

def measure(path):
    g = load_gray(path)
    return {
        "fft_hf_ratio": safe(m_fft_hf_ratio, g),
        "lap_var":      safe(m_lap_var, g),
        "texture_std":  safe(m_texture_std, g),
        "sat_mean":     safe(m_sat_mean, path),
        "rgb_corr":     safe(m_rgb_corr, path),
        "brisque":      m_brisque(path),
        "clip_plastic": m_clip_plastic(path),
    }

def safe(fn, arg):
    try:
        return fn(arg)
    except Exception as e:
        print(f"  ! metric fail {fn.__name__}: {e}")
        return None

# ---------------- разделимость ----------------
def auc(good_vals, bad_vals):
    """AUC: вероятность что случайный good > случайный bad по метрике (Mann-Whitney)."""
    g = [v for v in good_vals if v is not None]
    b = [v for v in bad_vals if v is not None]
    if not g or not b:
        return None
    wins = 0.0
    for x in g:
        for y in b:
            wins += 1.0 if x > y else (0.5 if x == y else 0.0)
    return wins / (len(g)*len(b))

def auc_ci(good_vals, bad_vals, n_boot=1000):
    """Bootstrap 95% CI для AUC (mimo-ревью: при n~25 голый AUC шумный).
    Метрика ЗНАЧИМА, только если CI не пересекает 0.5."""
    g = [v for v in good_vals if v is not None]
    b = [v for v in bad_vals if v is not None]
    if len(g) < 2 or len(b) < 2:
        return (auc(g, b), None, None)
    base = auc(g, b)
    rng = np.random.default_rng(42)
    boots = []
    for _ in range(n_boot):
        gs = list(rng.choice(g, len(g), replace=True))
        bs = list(rng.choice(b, len(b), replace=True))
        boots.append(auc(gs, bs))
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return (base, float(lo), float(hi))

def main():
    rows = []
    for entry in CALIB_SET:
        print(f"[pull] {entry['dir']} ({entry['source']}, label={entry['label']})")
        frames = pull_group(entry)
        print(f"       {len(frames)} кадров")
        for fr in frames:
            print(f"  [measure] {os.path.basename(fr)}")
            m = measure(fr)
            rows.append({"file": os.path.basename(fr), "label": entry["label"],
                         "source": entry["source"], **m})

    if not rows:
        print("НЕТ кадров — проверь пути CALIB_SET")
        sys.exit(1)

    # CSV
    csv_path = os.path.join(WORK, "metrics.csv")
    with open(csv_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["file","label","source"]+METRICS)
        w.writeheader()
        w.writerows(rows)

    # summary: разделимость каждой метрики
    good = [r for r in rows if r["label"] == 1]
    bad  = [r for r in rows if r["label"] == 0]
    lines = [f"# Калибровка детектора пластмассовости — {TS}",
             f"\nКадров: {len(rows)} (good/OK={len(good)}, bad/пластик={len(bad)})",
             "\n| Метрика | mean good | mean bad | AUC | 95% CI (bootstrap) | |AUC−0.5| | значима? |",
             "|---|---|---|---|---|---|---|"]
    ranking = []
    for mt in METRICS:
        gv = [r[mt] for r in good]; bv = [r[mt] for r in bad]
        gm = mean(gv); bm = mean(bv)
        a, lo, hi = auc_ci(gv, bv)
        sep = abs(a - 0.5) if a is not None else -1
        # значима, если CI целиком по одну сторону от 0.5
        sig = (lo is not None and (lo > 0.5 or hi < 0.5))
        ranking.append((sep, mt, a, gm, bm, lo, hi, sig))
        ci = f"[{fmt(lo)}, {fmt(hi)}]" if lo is not None else "—"
        lines.append(f"| {mt} | {fmt(gm)} | {fmt(bm)} | {fmt(a)} | {ci} | {fmt(sep)} | {'✅' if sig else '—'} |")
    ranking.sort(reverse=True)
    lines.append("\n## Вывод")
    lines.append("Чем дальше AUC от 0.5 — тем лучше метрика разделяет пластик и ОК.")
    lines.append("AUC>0.5 → у good метрика выше; <0.5 → у good ниже (метрика обратная, но рабочая).")
    lines.append("**Доверять только метрикам со «значима?»=✅** (bootstrap 95% CI не пересекает 0.5 — "
                 "при выборке n~25 голый AUC шумный, mimo-ревью).")
    sig_ranked = [r for r in ranking if r[7]]
    if sig_ranked:
        best = sig_ranked[0]
        lines.append(f"\n**Лучшая ЗНАЧИМАЯ метрика: `{best[1]}`** (AUC={fmt(best[2])}, CI=[{fmt(best[5])}, {fmt(best[6])}]).")
    else:
        best = ranking[0]
        lines.append(f"\n⚠️ **Ни одна метрика не значима** при текущей выборке (CI всех пересекают 0.5). "
                     f"Лучшая по точечному AUC — `{best[1]}` ({fmt(best[2])}), но нужна БОЛЬШЕ кадров для подтверждения.")
    lines.append("\nДальше: взять значимую метрику(и), выбрать порог по точке разделения, "
                 "встроить в гейт пула (на GH, после генерации). Если значимых нет — добрать размеченных кадров.")
    summary = "\n".join(lines)
    sum_path = os.path.join(WORK, "summary.md")
    with open(sum_path, "w") as fh:
        fh.write(summary)
    print("\n"+summary)

    # залить на ЯД
    sh(f'rclone copy "{csv_path}" "{OUT_YD}/"')
    sh(f'rclone copy "{sum_path}" "{OUT_YD}/"')
    print(f"\n✓ Результаты на ЯД: {OUT_YD}/ (metrics.csv + summary.md)")

def mean(vals):
    v = [x for x in vals if x is not None]
    return sum(v)/len(v) if v else None

def fmt(x):
    return f"{x:.4f}" if isinstance(x, float) else ("—" if x is None else str(x))

if __name__ == "__main__":
    main()
