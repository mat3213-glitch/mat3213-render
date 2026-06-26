#!/usr/bin/env python3
"""
vlm_judge_mimo.py — VLM-судья пластмассовости на ЗРЕНИИ mimo (mimo-auto vision, free).

mimo умеет vision (vision_tagger.py): `mimo run --pure --dangerously-skip-permissions "<промпт>" -f <img>`.
Каждый кадр → mimo судит по критериям yaromat (правдоподобие физики/анатомии/геометрии/света) →
plastic 0-100 → корреляция с разметкой (Spearman/Pearson/AUC).

Сравнение: aesthetic-predictor = +0.66 ОБРАТНАЯ ось; gh-ансамбль (gpt-4o+4.1) = +0.28 слабо.
Вход: ЯД cloud_io/plastic_aesthetic/{frames,labels.csv}. Выход: .../result_vlm_mimo/.
"""
import os, csv, json, re, subprocess, tempfile, glob
import numpy as np

YD = "ydrive:Content factory/cloud_io/plastic_aesthetic"
WORK = tempfile.mkdtemp(prefix="vlmm_")
FRAMES = os.path.join(WORK, "frames")
MIMO = os.path.expanduser("~/.mimocode/bin/mimo")

PROMPT = (
    "Оцени этот кадр из AI-видео на ПЛАСТМАССОВОСТЬ = неправдоподобие (НЕ красота!). "
    "Смотри: физика (огонь/вода/дым/частицы как в реальности?), анатомия (руки/тела/предметы без искажений?), "
    "геометрия и плотность/расположение объектов в пространстве (естественны?), освещение (натуральное или аляповатое?). "
    "НЕ оценивай художественную красоту/цвет/композицию. "
    "Ответь СТРОГО одним JSON: {\"plastic\": <число 0-100>, \"reason\": \"<кратко главный артефакт или 'правдоподобно'>\"}. "
    "0 = выглядит как реальное фото, 100 = явный пластиковый AI-артефакт. Только JSON, без лишнего текста."
)

def sh(c): return subprocess.run(c, shell=True, capture_output=True, text=True)

def strip_ansi(t): return re.sub(r'\x1B\[[0-9;]*[A-Za-z]', '', t)

def extract_json(t):
    t = strip_ansi(t)
    depth = 0; start = -1
    for i, ch in enumerate(t):
        if ch == '{':
            if depth == 0: start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start >= 0:
                try: return json.loads(t[start:i+1])
                except Exception: start = -1
    return None

def judge(path, timeout=180):
    try:
        r = subprocess.run([MIMO, "run", "--pure", "--dangerously-skip-permissions", PROMPT, "-f", path],
                           capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    d = extract_json(r.stdout or "")
    if not d or "plastic" not in d:
        return None, f"no-json: {strip_ansi(r.stdout or '')[-100:]}"
    try:
        v = float(d["plastic"]); v = max(0.0, min(100.0, v))
    except Exception:
        return None, f"bad-num {d.get('plastic')}"
    reason = re.sub(r'[一-鿿]', '', str(d.get("reason", "")))[:60]  # чистим CJK-заразу mimo
    return v, reason

# данные
sh(f'rclone copy "{YD}/frames" "{FRAMES}" --transfers 8')
sh(f'rclone copyto "{YD}/labels.csv" "{WORK}/labels.csv"')
labels = {}
with open(f"{WORK}/labels.csv") as fh:
    for row in csv.DictReader(fh):
        labels[row["file"]] = (int(row["plastic_pct"]), row["gen"])
print(f"меток: {len(labels)}")

rows = []
for f, (pct, gen) in sorted(labels.items()):
    p = os.path.join(FRAMES, f)
    if not os.path.exists(p): print(f"  ! нет {f}"); continue
    sc, note = judge(p)
    rows.append({"file": f, "gen": gen, "plastic_pct": pct, "vlm": sc, "reason": note})
    print(f"  {f:16} метка={pct:3}%  mimo={sc}  ({note})")

# корреляция
ok = [r for r in rows if r["vlm"] is not None]
def spearman(x, y):
    def rk(v):
        o = sorted(range(len(v)), key=lambda i: v[i]); r=[0]*len(v)
        for p,i in enumerate(o): r[i]=p
        return r
    n=len(x)
    if n<2: return 0.0
    rx,ry=rk(x),rk(y); d2=sum((a-b)**2 for a,b in zip(rx,ry))
    return 1-6*d2/(n*(n*n-1))
def pearson(x,y):
    x,y=np.array(x),np.array(y)
    return 0.0 if x.std()==0 or y.std()==0 else float(np.corrcoef(x,y)[0,1])
def auc(g,b):
    if not g or not b: return None
    return sum(1.0 if a>c else 0.5 if a==c else 0.0 for a in g for c in b)/(len(g)*len(b))

pct=[r["plastic_pct"] for r in ok]; vlm=[r["vlm"] for r in ok]
sp=spearman(pct,vlm); pe=pearson(pct,vlm)
bad=[r["vlm"] for r in ok if r["plastic_pct"]>=50]; good=[r["vlm"] for r in ok if r["plastic_pct"]<50]
a=auc(bad,good)

csv_p=os.path.join(WORK,"scores.csv")
with open(csv_p,"w",newline="") as fh:
    w=csv.DictWriter(fh,fieldnames=["file","gen","plastic_pct","vlm","reason"]); w.writeheader(); w.writerows(rows)

L=["# VLM-судья (mimo зрение) vs разметка пластмассовости yaromat",
   f"\nКадров оценено: {len(ok)}/{len(rows)} (пластик ≥50%: {len(bad)}, живые <50%: {len(good)})",
   "\n## Корреляция (метка пластик_% ↔ mimo-score)",
   f"- **Spearman:** {sp:+.3f}",
   f"- **Pearson:**  {pe:+.3f}",
   f"- **AUC (mimo у пластика > у живых):** {None if a is None else round(a,3)}",
   "\n## Сравнение детекторов",
   "- aesthetic-predictor: Spearman +0.66 ОБРАТНАЯ ось (хвалит пластик) — не годится.",
   "- gh-ансамбль (gpt-4o+gpt-4.1): Spearman +0.28, AUC 0.67 — слабо.",
   f"- mimo-зрение: Spearman {sp:+.2f}, AUC {None if a is None else round(a,2)} — см. вывод.",
   f"\nСредний mimo: живые={np.mean(good):.1f} / пластик={np.mean(bad):.1f}" if good and bad else "",
   "\n## Таблица (по метке)","| кадр | ген | метка% | mimo | причина |","|---|---|---|---|---|"]
for r in sorted(rows, key=lambda r: r["plastic_pct"]):
    L.append(f"| {r['file']} | {r['gen']} | {r['plastic_pct']} | {r['vlm']} | {r['reason']} |")
verdict=("mimo-зрение РАБОТАЕТ детектором (Spearman %.2f) — кандидат в гейт пула, free." % sp
         if sp>0.5 else "mimo-зрение слабо совпало (Spearman %.2f) — нужен мультикадр/др. модель." % sp)
L.append(f"\n## Вывод\n**{verdict}**")
summ="\n".join([x for x in L if x!=""])
open(os.path.join(WORK,"summary.md"),"w").write(summ)
print("\n"+summ)
sh(f'rclone copy "{csv_p}" "{YD}/result_vlm_mimo/"')
sh(f'rclone copy "{os.path.join(WORK,"summary.md")}" "{YD}/result_vlm_mimo/"')
print(f"\n✓ результат → {YD}/result_vlm_mimo/")
