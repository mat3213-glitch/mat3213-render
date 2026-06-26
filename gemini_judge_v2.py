#!/usr/bin/env python3
"""
gemini_judge_v2.py — ДОКРУЧЕННЫЙ Gemini-судья пластмассовости.

v1 дал Spearman +0.47 (лучший из 4, но не боевой). Промахи: тонкая анатомия (восковые руки→назвал
реалистичными), занижал средний Qwen-пластик (мох/геометрия), путал статику с артефактом.

ДОКРУТКА:
1. gemini-2.5-pro (сильнее flash в тонком reasoning), fallback на flash при исчерпании квоты.
2. КАРТИНОЧНЫЙ few-shot — 4 якоря с вердиктами yaromat как multi-turn примеры (учат анатомии/огню/
   атмосфере/среднему пластику). Якоря ИСКЛЮЧЕНЫ из оценки (честно: не train-on-test).
3. systemInstruction докручен: анатомия рук, «статика ≠ пластик», средний пластик мха/геометрии.

Вход: ЯД frames3/<file>_{1,2,3}.jpg + labels.csv. Выход: .../result_gemini_v2/.
"""
import os, csv, json, re, time, base64, tempfile, subprocess
import urllib.request, urllib.error
import numpy as np

YD = "ydrive:Content factory/cloud_io/plastic_aesthetic"
WORK = tempfile.mkdtemp(prefix="gem2_")
FR = os.path.join(WORK, "frames3")
MODELS = ["gemini-2.5-pro", "gemini-2.5-flash"]  # pro первично, flash fallback
KEYS = [os.environ[k] for k in ("GEMINI_API_KEY","GEMINI_API_KEY_2","GEMINI_API_KEY_3") if os.environ.get(k)]
_ki = 0

SYS = (
    "Ты эксперт по выявлению «пластмассовости» AI-видео. Тебе дают 3 КАДРА одного клипа (20%/50%/80%) — "
    "оценивай их КАК ДИНАМИКУ. «Пластмассовость» = НЕПРАВДОПОДОБИЕ, НЕ красота. Критерии:\n"
    "- ФИЗИКА движения: огонь/вода/искры/дым/капли как в реальности? (искры-фейерверк, флюиды огня, "
    "протяжные струи воды, дёрганая камера = пластик).\n"
    "- АНАТОМИЯ (ОСОБОЕ ВНИМАНИЕ): руки/пальцы/кожа — восковая гладкость, неверные пальцы/суставы, "
    "деформации = СИЛЬНЫЙ пластик. Не называй восковые руки реалистичными.\n"
    "- ТЕКСТУРА/ГЕОМЕТРИЯ: повторяющаяся неестественная текстура (мох, листва, паутина), неестественная "
    "геометрия предметов (лишние спинки/конечности), неправильное расположение/плотность объектов = пластик, "
    "ДАЖЕ если атмосфера красивая.\n"
    "- ОСВЕЩЕНИЕ: аляповатый/слишком симметричный свет, нереальная игра света на стекле = пластик.\n\n"
    "НЕ ПУТАЙ:\n"
    "- Гладкость АТМОСФЕРЫ (туман/небо/облака/кора/ландшафт) — это НОРМА, НЕ штрафуй (0-25).\n"
    "- Малое движение / статичная сцена — само по себе НЕ пластик, если элементы правдоподобны. "
    "Отсутствие динамики между кадрами НЕ считать артефактом.\n\n"
    "Верни СТРОГО один JSON: {\"plastic\": <0-100>, \"reason\": \"<главный артефакт или 'правдоподобно'>\"}."
)

# few-shot якоря: (имя в frames3, вердикт-метка, reason) — ИСКЛЮЧЕНЫ из оценки
ANCHORS = [
    ("s_qwen01", 100, "восковые гладкие руки, деформированные цифры — анатомия неправдоподобна"),
    ("s_qwen02", 85,  "искры огня как фейерверк, неестественные флюиды огня и дыма"),
    ("m_veo2",   5,   "естественный туман в лесу; гладкость атмосферы — это норма, не пластик"),
    ("m_qwen4",  60,  "мох и растительность с повторяющейся неестественной текстурой"),
]
ANCHOR_NAMES = {a[0] for a in ANCHORS}

def sh(c): return subprocess.run(c, shell=True, capture_output=True, text=True)

def b64(name, i):
    p = os.path.join(FR, f"{name}_{i}.jpg")
    return base64.b64encode(open(p, "rb").read()).decode() if os.path.exists(p) else None

def imgs(name):
    return [b for b in (b64(name, i) for i in (1,2,3)) if b]

def build_fewshot():
    """multi-turn few-shot: user(3 кадра)→model(вердикт)."""
    turns = []
    for name, pct, reason in ANCHORS:
        bs = imgs(name)
        if not bs: continue
        turns.append({"role": "user", "parts": [{"text": "Оцени этот клип (3 кадра):"}] +
                      [{"inline_data": {"mime_type": "image/jpeg", "data": b}} for b in bs]})
        turns.append({"role": "model", "parts": [{"text": json.dumps({"plastic": pct, "reason": reason}, ensure_ascii=False)}]})
    return turns

FEWSHOT = None
def ask(name):
    global _ki, FEWSHOT
    if FEWSHOT is None: FEWSHOT = build_fewshot()
    bs = imgs(name)
    if not bs: return None, "нет кадров"
    contents = FEWSHOT + [{"role": "user", "parts": [{"text": "Оцени этот клип (3 кадра):"}] +
                          [{"inline_data": {"mime_type": "image/jpeg", "data": b}} for b in bs]}]
    body = json.dumps({
        "systemInstruction": {"parts": [{"text": SYS}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json"},
    }).encode()
    for model in MODELS:
        for _ in range(len(KEYS) * 2):
            key = KEYS[_ki % len(KEYS)]
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
            req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type":"application/json"})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    d = json.load(r)
                txt = d["candidates"][0]["content"]["parts"][0]["text"]
                sc, rs = parse(txt)
                return sc, f"[{model.split('-')[-1]}] {rs}"
            except urllib.error.HTTPError as e:
                if e.code in (429, 503):
                    _ki += 1; time.sleep(4); continue
                return None, f"HTTP {e.code}: {e.read()[:80]}"
            except Exception as e:
                return None, f"err {e}"
        # квота модели исчерпана → следующая модель
    return None, "все модели/ключи исчерпаны"

def parse(txt):
    m = re.search(r'"plastic"\s*:\s*(\d+(?:\.\d+)?)', txt)
    if not m: m = re.search(r'\b(\d{1,3})\b', txt)
    if not m: return None, txt[:70]
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
test = {k:v for k,v in labels.items() if k not in ANCHOR_NAMES}  # якоря вне теста
print(f"меток всего: {len(labels)}, якорей few-shot: {len(ANCHOR_NAMES)}, тест: {len(test)}, ключей: {len(KEYS)}")

rows = []
for name, (pct, gen) in sorted(test.items()):
    sc, reason = ask(name)
    rows.append({"file": name, "gen": gen, "plastic_pct": pct, "vlm": sc, "reason": reason})
    print(f"  {name:12} метка={pct:3}%  gemini={sc}  ({reason})")
    time.sleep(2)

ok = [r for r in rows if r["vlm"] is not None]
def spearman(x,y):
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
L=["# Gemini-судья v2 (pro + картиночный few-shot + докрут) vs разметка",
   f"\nМодели: {MODELS} (pro→flash fallback), few-shot {len(ANCHOR_NAMES)} якоря (вне теста), тест {len(ok)}",
   "\n## Корреляция (метка ↔ gemini, на тесте без якорей)",
   f"- **Spearman:** {sp:+.3f}",
   f"- **Pearson:**  {pe:+.3f}",
   f"- **AUC (gemini у пластика > у живых):** {None if a is None else round(a,3)}",
   "\n## Эволюция детектора",
   "- aesthetic: +0.66 обратная ось | gh-ансамбль: +0.28 | mimo: −0.10 | Gemini v1 (flash): +0.47",
   f"- **Gemini v2 (pro+few-shot): {sp:+.2f}, AUC {None if a is None else round(a,2)}**",
   f"\nСредний gemini: живые={np.mean(good):.1f} / пластик={np.mean(bad):.1f}" if good and bad else "",
   "\n## Таблица (по метке)","| видео | ген | метка% | gemini | причина |","|---|---|---|---|---|"]
for r in sorted(rows, key=lambda r: r["plastic_pct"]):
    L.append(f"| {r['file']} | {r['gen']} | {r['plastic_pct']} | {r['vlm']} | {r['reason']} |")
verdict=("Gemini v2 РАБОТАЕТ детектором (Spearman %.2f) — брать в гейт пула." % sp if sp>0.6 else
         ("Gemini v2 годен для БИНАРНОГО гейта (AUC %s) — отбраковка явного пластика." % (None if a is None else round(a,2)) if (a or 0)>0.75 else
          "Gemini v2: Spearman %.2f — прогресс, но тонкая градация всё ещё трудна; добрать метки." % sp))
L.append(f"\n## Вывод\n**{verdict}**")
summ="\n".join([x for x in L if x!=""])
open(os.path.join(WORK,"summary.md"),"w").write(summ)
print("\n"+summ)
sh(f'rclone copy "{csv_p}" "{YD}/result_gemini_v2/"')
sh(f'rclone copy "{os.path.join(WORK,"summary.md")}" "{YD}/result_gemini_v2/"')
print(f"\n✓ → {YD}/result_gemini_v2/")
