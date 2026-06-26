#!/usr/bin/env python3
"""
gemini_judge.py — VLM-судья пластмассовости на Gemini, МУЛЬТИКАДР (3 кадра/видео) + few-shot.

Лечит ограничения прошлых попыток:
- aesthetic +0.66 ОБРАТНАЯ ось (красота), gh-ансамбль +0.28 (слабо), mimo −0.10 (путал гладкость с пластиком).
- МУЛЬТИКАДР: 3 момента клипа → Gemini видит ДИНАМИКУ (поведение огня/воды/камеры), невидимую в стилле.
- FEW-SHOT: вшита карта yaromat (триггеры/топ-зоны) + явный запрет штрафовать атмосферную гладкость.

Модель gemini-2.5-flash. Ротация ключей GEMINI_API_KEY/_2/_3 (один пул, но fallback на 429).
Вход: ЯД cloud_io/plastic_aesthetic/{frames3/<file>_{1,2,3}.jpg, labels.csv}. Выход: .../result_gemini/.
"""
import os, csv, json, re, time, base64, tempfile, subprocess
import urllib.request, urllib.error
import numpy as np

YD = "ydrive:Content factory/cloud_io/plastic_aesthetic"
WORK = tempfile.mkdtemp(prefix="gem_")
FR = os.path.join(WORK, "frames3")
MODEL = "gemini-2.5-flash"
KEYS = [os.environ[k] for k in ("GEMINI_API_KEY","GEMINI_API_KEY_2","GEMINI_API_KEY_3") if os.environ.get(k)]
_ki = 0

SYS = (
    "Ты эксперт по выявлению «пластмассовости» AI-видео. Тебе дают 3 КАДРА одного клипа "
    "(моменты 20%/50%/80%) — оценивай их КАК ДИНАМИКУ (поведение огня/воды/дыма/частиц/камеры во времени).\n"
    "«Пластмассовость» = НЕПРАВДОПОДОБИЕ, НЕ красота. Смотри строго на:\n"
    "- физика движения: огонь/вода/искры/дым/капли ведут себя как в реальности? (искры как фейерверк, "
    "флюиды огня, протяжные струи воды, дёрганая камера = пластик)\n"
    "- анатомия: руки/тела/предметы без искажений (морщинистые/восковые руки, лишние спинки/конечности = пластик)\n"
    "- геометрия и плотность/расположение объектов в пространстве (слишком плотный поток машин/капель/пыли, "
    "непонятное размещение в пространстве, неестественное расположение зданий = пластик)\n"
    "- освещение: натуральное или аляповатое/слишком симметричное свечение, нереальная игра света на стекле = пластик\n\n"
    "КАЛИБРОВКА (вердикты эксперта-человека):\n"
    "- огонь/искры как фейерверк, восковые руки, неестественный свет на стекле + расположение домов → 90-100\n"
    "- слишком плотный поток машин/капель/пыли, непонятное расположение капель в пространстве → 50-90\n"
    "- ВАЖНО: туман/лес/небо/тучи/дерево/ландшафт/реалистичные отражения (стекло, воск)/плавная камера → 0-25. "
    "ЭТО НОРМА. НЕ штрафуй за «гладкость» атмосферы, неба, тумана, коры — гладкая атмосфера ≠ пластик!\n\n"
    "Верни СТРОГО один JSON: {\"plastic\": <0-100>, \"reason\": \"<главный артефакт или 'правдоподобно'>\"}."
)

def sh(c): return subprocess.run(c, shell=True, capture_output=True, text=True)

def ask(b64s):
    global _ki
    parts = [{"text": SYS}] + [{"inline_data": {"mime_type": "image/jpeg", "data": b}} for b in b64s]
    body = json.dumps({
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json"},
    }).encode()
    for _ in range(len(KEYS) * 2):
        key = KEYS[_ki % len(KEYS)]
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={key}"
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                d = json.load(r)
            txt = d["candidates"][0]["content"]["parts"][0]["text"]
            return parse(txt)
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                _ki += 1; time.sleep(3); continue
            return None, f"HTTP {e.code}: {e.read()[:80]}"
        except Exception as e:
            return None, f"err {e}"
    return None, "rate-limit (все ключи)"

def parse(txt):
    m = re.search(r'"plastic"\s*:\s*(\d+(?:\.\d+)?)', txt)
    if not m: m = re.search(r'\b(\d{1,3})\b', txt)
    if not m: return None, txt[:80]
    v = max(0.0, min(100.0, float(m.group(1))))
    rm = re.search(r'"reason"\s*:\s*"([^"]*)"', txt)
    return v, (rm.group(1)[:70] if rm else "")

# данные
sh(f'rclone copy "{YD}/frames3" "{FR}" --transfers 8')
sh(f'rclone copyto "{YD}/labels.csv" "{WORK}/labels.csv"')
labels = {}
with open(f"{WORK}/labels.csv") as fh:
    for row in csv.DictReader(fh):
        labels[row["file"].replace(".jpg","")] = (int(row["plastic_pct"]), row["gen"])
print(f"меток: {len(labels)}, ключей: {len(KEYS)}")

rows = []
for name, (pct, gen) in sorted(labels.items()):
    b64s = []
    for i in (1, 2, 3):
        p = os.path.join(FR, f"{name}_{i}.jpg")
        if os.path.exists(p):
            b64s.append(base64.b64encode(open(p, "rb").read()).decode())
    if not b64s:
        print(f"  ! нет кадров {name}"); continue
    sc, reason = ask(b64s)
    rows.append({"file": name, "gen": gen, "plastic_pct": pct, "vlm": sc, "reason": reason})
    print(f"  {name:12} метка={pct:3}%  gemini={sc}  ({reason})")
    time.sleep(1)

# корреляция
ok = [r for r in rows if r["vlm"] is not None]
def spearman(x, y):
    def rk(v):
        o=sorted(range(len(v)),key=lambda i:v[i]); r=[0]*len(v)
        for p,i in enumerate(o): r[i]=p
        return r
    n=len(x)
    if n<2: return 0.0
    rx,ry=rk(x),rk(y); d2=sum((a-b)**2 for a,b in zip(rx,ry))
    return 1-6*d2/(n*(n*n-1))
def pearson(x,y):
    x,y=np.array(x),np.array(y); return 0.0 if x.std()==0 or y.std()==0 else float(np.corrcoef(x,y)[0,1])
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
L=["# Gemini-судья (мультикадр+few-shot) vs разметка пластмассовости",
   f"\nМодель: {MODEL}, 3 кадра/видео, few-shot калибровка yaromat",
   f"Кадров-видео: {len(ok)}/{len(rows)} (пластик ≥50%: {len(bad)}, живые <50%: {len(good)})",
   "\n## Корреляция (метка ↔ gemini)",
   f"- **Spearman:** {sp:+.3f}",
   f"- **Pearson:**  {pe:+.3f}",
   f"- **AUC (gemini у пластика > у живых):** {None if a is None else round(a,3)}",
   "\n## Все детекторы",
   "- aesthetic-predictor: +0.66 ОБРАТНАЯ ось (хвалит пластик)",
   "- gh-ансамбль (gpt-4o+4.1): +0.28, AUC 0.67",
   "- mimo-зрение: −0.10 (путал гладкость с пластиком)",
   f"- **Gemini мультикадр+few-shot: {sp:+.2f}, AUC {None if a is None else round(a,2)}**",
   f"\nСредний gemini: живые={np.mean(good):.1f} / пластик={np.mean(bad):.1f}" if good and bad else "",
   "\n## Таблица (по метке)","| видео | ген | метка% | gemini | причина |","|---|---|---|---|---|"]
for r in sorted(rows, key=lambda r: r["plastic_pct"]):
    L.append(f"| {r['file']} | {r['gen']} | {r['plastic_pct']} | {r['vlm']} | {r['reason']} |")
verdict=("Gemini РАБОТАЕТ детектором (Spearman %.2f, AUC %s) — брать в гейт пула." % (sp, None if a is None else round(a,2))
         if sp>0.5 else "Gemini лучше прежних, но Spearman %.2f — добрать метки/докрутить few-shot." % sp)
L.append(f"\n## Вывод\n**{verdict}**")
summ="\n".join([x for x in L if x!=""])
open(os.path.join(WORK,"summary.md"),"w").write(summ)
print("\n"+summ)
sh(f'rclone copy "{csv_p}" "{YD}/result_gemini/"')
sh(f'rclone copy "{os.path.join(WORK,"summary.md")}" "{YD}/result_gemini/"')
print(f"\n✓ → {YD}/result_gemini/")
