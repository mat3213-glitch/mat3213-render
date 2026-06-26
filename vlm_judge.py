#!/usr/bin/env python3
"""
vlm_judge.py — VLM-СУДЬЯ пластмассовости на ансамбле GitHub Models (vision).

Каждый кадр → несколько vision-моделей (gpt-4o / gpt-4.1 / llama-3.2-vision) с промптом
по критериям yaromat (правдоподобие физики/анатомии/геометрии/света, НЕ красота) → каждая
даёт plastic 0-100 → усреднение ансамбля → корреляция с разметкой yaromat (Spearman/Pearson/AUC).

Цель: проверить, совпадает ли вердикт VLM с метками (в отличие от aesthetic-predictor,
который дал ОБРАТНУЮ корреляцию). Если совпадает → рабочий детектор для гейта пула.

Вход: ЯД cloud_io/plastic_aesthetic/{frames/*.jpg, labels.csv}. Всё на GH.
Auth: GITHUB_TOKEN (permissions: models: read). Выход: .../result_vlm/{scores.csv, summary.md}.
"""
import os, csv, json, re, time, base64, tempfile, glob, urllib.request, urllib.error
import numpy as np
import subprocess

YD = "ydrive:Content factory/cloud_io/plastic_aesthetic"
WORK = tempfile.mkdtemp(prefix="vlm_")
FRAMES = os.path.join(WORK, "frames")
ENDPOINT = "https://models.github.ai/inference/chat/completions"
TOKEN = os.environ["GITHUB_TOKEN"]

# ансамбль vision-моделей (недоступные пропускаются автоматически)
MODELS = ["openai/gpt-4o", "openai/gpt-4.1", "meta/Llama-3.2-90B-Vision-Instruct"]

PROMPT = (
    "Ты эксперт по выявлению «пластмассовости» AI-генерации в кадре. "
    "«Пластмассовость» = НЕПРАВДОПОДОБИЕ, а НЕ красота. Оценивай ТОЛЬКО:\n"
    "- физика: огонь/вода/дым/искры/частицы ведут себя как в реальности?\n"
    "- анатомия: руки/тела/предметы без искажений?\n"
    "- геометрия и расположение объектов в пространстве: плотность и размещение естественны "
    "(не «слишком плотно», не «непонятно где»)?\n"
    "- освещение: натуральное или аляповатое/неестественное свечение?\n"
    "НЕ оценивай художественную красоту, композицию, цвет.\n"
    "Верни СТРОГО JSON: {\"plastic\": <0-100>, \"reason\": \"<кратко главный артефакт или 'правдоподобно'>\"}. "
    "0 = полностью правдоподобно/как реальное фото, 100 = явный пластиковый AI-артефакт."
)

def sh(c): return subprocess.run(c, shell=True, capture_output=True, text=True)

def ask(model, b64):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]}],
        "temperature": 0.0,
    }).encode()
    req = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
        "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json",
        "Accept": "application/json", "User-Agent": "curl/8.0",
    })
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                txt = json.load(r)["choices"][0]["message"]["content"]
            return parse_score(txt), txt[:120]
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                wait = int(e.headers.get("Retry-After", 8 * (attempt + 1)))
                print(f"    {model} {e.code}, жду {wait}с"); time.sleep(min(wait, 30)); continue
            if e.code == 404:
                return None, f"404 модель недоступна"
            return None, f"HTTP {e.code}: {e.read()[:80]}"
        except Exception as e:
            return None, f"err {e}"
    return None, "rate-limit исчерпан"

def parse_score(txt):
    m = re.search(r'"plastic"\s*:\s*(\d+(?:\.\d+)?)', txt)
    if not m:
        m = re.search(r'\b(\d{1,3})\b', txt)
    if not m: return None
    v = float(m.group(1))
    return max(0.0, min(100.0, v))

# ── данные ──
sh(f'rclone copy "{YD}/frames" "{FRAMES}" --transfers 8')
sh(f'rclone copyto "{YD}/labels.csv" "{WORK}/labels.csv"')
labels = {}
with open(f"{WORK}/labels.csv") as fh:
    for row in csv.DictReader(fh):
        labels[row["file"]] = (int(row["plastic_pct"]), row["gen"])
print(f"меток: {len(labels)}")

# ── судим каждый кадр ансамблем ──
rows = []
for f, (pct, gen) in sorted(labels.items()):
    p = os.path.join(FRAMES, f)
    if not os.path.exists(p):
        print(f"  ! нет {f}"); continue
    b64 = base64.b64encode(open(p, "rb").read()).decode()
    votes, reasons = [], []
    for mdl in MODELS:
        sc, note = ask(mdl, b64)
        if sc is not None:
            votes.append(sc); reasons.append(f"{mdl.split('/')[-1]}={sc:.0f}")
        else:
            reasons.append(f"{mdl.split('/')[-1]}:{note}")
        time.sleep(1.5)
    ens = round(sum(votes)/len(votes), 1) if votes else None
    rows.append({"file": f, "gen": gen, "plastic_pct": pct, "vlm": ens,
                 "n_votes": len(votes), "detail": "; ".join(reasons)})
    print(f"  {f:16} метка={pct:3}%  VLM={ens}  ({'; '.join(reasons)})")

# ── корреляция (только где ансамбль дал число) ──
ok = [r for r in rows if r["vlm"] is not None]

def spearman(x, y):
    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i]); r = [0]*len(v)
        for pos, i in enumerate(order): r[i] = pos
        return r
    n = len(x)
    if n < 2: return 0.0
    rx, ry = rank(x), rank(y)
    d2 = sum((a-b)**2 for a, b in zip(rx, ry))
    return 1 - 6*d2/(n*(n*n-1))

def pearson(x, y):
    x, y = np.array(x), np.array(y)
    if x.std()==0 or y.std()==0: return 0.0
    return float(np.corrcoef(x, y)[0,1])

def auc(good, bad):
    if not good or not bad: return None
    w = sum(1.0 if g>b else 0.5 if g==b else 0.0 for g in good for b in bad)
    return w/(len(good)*len(bad))

pct = [r["plastic_pct"] for r in ok]; vlm = [r["vlm"] for r in ok]
sp = spearman(pct, vlm); pe = pearson(pct, vlm)
# бинарно: пластик метка>=50; ловит ли VLM? (у пластика VLM-score должен быть ВЫШЕ)
bad_vlm  = [r["vlm"] for r in ok if r["plastic_pct"] >= 50]
good_vlm = [r["vlm"] for r in ok if r["plastic_pct"] < 50]
a = auc(bad_vlm, good_vlm)  # P(VLM(плохой) > VLM(хороший)) — хотим →1.0

csv_p = os.path.join(WORK, "scores.csv")
with open(csv_p, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=["file","gen","plastic_pct","vlm","n_votes","detail"])
    w.writeheader(); w.writerows(rows)

L = ["# VLM-судья (gh-ансамбль) vs разметка пластмассовости yaromat",
     f"\nМодели ансамбля: {', '.join(MODELS)}",
     f"Кадров оценено: {len(ok)}/{len(rows)} (пластик ≥50%: {len(bad_vlm)}, живые <50%: {len(good_vlm)})",
     "\n## Корреляция (метка пластик_% ↔ VLM-score)",
     f"- **Spearman:** {sp:+.3f}",
     f"- **Pearson:**  {pe:+.3f}",
     f"- **AUC (VLM у пластика > у живых):** {None if a is None else round(a,3)}",
     "\n## Как читать",
     "- VLM РАБОТАЕТ как детектор → Spearman сильно ПОЛОЖИТЕЛЬНЫЙ (метка↑→VLM↑), AUC→1.0.",
     "- (Сравни: aesthetic-predictor дал Spearman +0.66 но это была ОБРАТНАЯ ось — он про красоту.",
     "  Здесь положительный Spearman = ПРАВИЛЬНО, т.к. VLM-score и метка измеряют ОДНО — пластик.)",
     f"\nСредний VLM: живые={np.mean(good_vlm):.1f} / пластик={np.mean(bad_vlm):.1f}" if good_vlm and bad_vlm else "",
     "\n## Таблица (по метке)", "| кадр | ген | метка% | VLM | голоса |", "|---|---|---|---|---|"]
for r in sorted(rows, key=lambda r: r["plastic_pct"]):
    L.append(f"| {r['file']} | {r['gen']} | {r['plastic_pct']} | {r['vlm']} | {r['detail']} |")
verdict = ("VLM РАБОТАЕТ детектором — совпадает с разметкой (Spearman %.2f, AUC %s). Брать в гейт пула." % (sp, None if a is None else round(a,2))
           if sp > 0.5 else
           "VLM слабо/не совпал (Spearman %.2f) — крутить промпт-судью или модели ансамбля." % sp)
L.append(f"\n## Вывод\n**{verdict}**")
summ = "\n".join([x for x in L if x != ""])
open(os.path.join(WORK, "summary.md"), "w").write(summ)
print("\n"+summ)
sh(f'rclone copy "{csv_p}" "{YD}/result_vlm/"')
sh(f'rclone copy "{os.path.join(WORK, "summary.md")}" "{YD}/result_vlm/"')
print(f"\n✓ результат → {YD}/result_vlm/")
