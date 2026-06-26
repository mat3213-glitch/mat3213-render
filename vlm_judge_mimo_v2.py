#!/usr/bin/env python3
"""
vlm_judge_mimo_v2.py — mimo-судья пластмассовости, ОБУЧЕННЫЙ рубрикой Claude.

mimo v1 дал −0.10 (путал атмосферную гладкость с пластиком). Claude сам прогонял кадры и
дистиллировал РУБРИКУ маркеров (что пластик vs что норма) — вшита в промпт. Плюс mimo получает
СКЛЕЙКУ 3 кадров (как контакт-лист деконструктора) → видит динамику/изменение, не один стилл.

Вход: ЯД frames3/<file>_{1,2,3}.jpg + labels.csv. Выход: .../result_mimo_v2/.
"""
import os, csv, re, subprocess, tempfile, json
import numpy as np
from PIL import Image

YD = "ydrive:Content factory/cloud_io/plastic_aesthetic"
WORK = tempfile.mkdtemp(prefix="mimo2_")
FR = os.path.join(WORK, "frames3")
STRIPS = os.path.join(WORK, "strips"); os.makedirs(STRIPS, exist_ok=True)
MIMO = os.path.expanduser("~/.mimocode/bin/mimo")

# РУБРИКА Claude (дистиллят из ручного прогона 12 кадров) — «обучение» mimo
RUBRIC = (
    "Перед тобой СКЛЕЙКА 3 кадров одного AI-видео (моменты 20%/50%/80%, слева направо). "
    "Оцени ПЛАСТМАССОВОСТЬ = неправдоподобие (НЕ красоту) по рубрике эксперта:\n\n"
    "ВЫСОКИЙ пластик 70-100:\n"
    "- AI-ТЕКСТ: цифры/буквы как деформированные каракули, не читаются.\n"
    "- ИДЕАЛЬНО симметричный/искусственный свет, нереальный источник света, CGI-рендер заставки, "
    "идеальное зеркальное отражение.\n"
    "- ВОСКОВАЯ гладкая кожа рук, деформации/искажения анатомии (лишние пальцы, спинки).\n"
    "- Огонь «приклеен» ровным контуром, искры как фейерверк.\n\n"
    "СРЕДНИЙ 40-70:\n"
    "- МОХ/лишайник «налеплен», ПОВТОРЯЮЩАЯСЯ одинаковая текстура коры/листвы/папоротника.\n"
    "- ГЕОМЕТРИЯ фасадов/зданий «плывёт», нерегулярный паттерн окон, кривые/сливающиеся объекты, "
    "неестественное расположение объектов в пространстве.\n"
    "- Объекты СТАТИЧНЫ там, где должны двигаться (вода не течёт между кадрами, капли висят без гравитации).\n\n"
    "НИЗКИЙ 0-30 (НОРМА, НЕ штрафуй!):\n"
    "- Туман, лес, небо, облака, ландшафт, плёночное зерно, дымка — даже если ГЛАДКИЕ.\n"
    "- Реалистичное пламя свечи, боке огней, капли на стекле с правильной оптикой, рукопись старой книги.\n"
    "- Тёмный/чёрный фон, малое движение, статичная сцена — это НЕ артефакт сами по себе.\n\n"
    "ГЛАВНОЕ: отличай НАЛЕПЛЕННУЮ/ПОВТОРЯЮЩУЮСЯ текстуру и искажённую геометрию (= пластик) от "
    "РОВНОЙ АТМОСФЕРЫ (= норма). Гладкое небо/туман = норма; одинаковый повторяющийся мох/окна = пластик.\n\n"
    "Ответь СТРОГО одним JSON: {\"plastic\": <0-100>, \"reason\": \"<главный артефакт или 'правдоподобно'>\"}. Только JSON."
)

def sh(c): return subprocess.run(c, shell=True, capture_output=True, text=True)
def strip_ansi(t): return re.sub(r'\x1B\[[0-9;]*[A-Za-z]', '', t)

def extract_json(t):
    t = strip_ansi(t); depth=0; start=-1
    for i,ch in enumerate(t):
        if ch=='{':
            if depth==0: start=i
            depth+=1
        elif ch=='}':
            depth-=1
            if depth==0 and start>=0:
                try: return json.loads(t[start:i+1])
                except Exception: start=-1
    return None

def make_strip(name):
    """склеить 3 кадра горизонтально в одну полоску."""
    imgs=[]
    for i in (1,2,3):
        p=os.path.join(FR,f"{name}_{i}.jpg")
        if os.path.exists(p): imgs.append(Image.open(p).convert("RGB"))
    if not imgs: return None
    h=min(im.height for im in imgs)
    imgs=[im.resize((int(im.width*h/im.height),h)) for im in imgs]
    W=sum(im.width for im in imgs)
    strip=Image.new("RGB",(W,h)); x=0
    for im in imgs: strip.paste(im,(x,0)); x+=im.width
    out=os.path.join(STRIPS,f"{name}.jpg"); strip.save(out,quality=88)
    return out

def judge(strip, timeout=180):
    try:
        r=subprocess.run([MIMO,"run","--pure","--dangerously-skip-permissions",RUBRIC,"-f",strip],
                         capture_output=True,text=True,timeout=timeout,stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None,"timeout"
    d=extract_json(r.stdout or "")
    if not d or "plastic" not in d: return None,f"no-json: {strip_ansi(r.stdout or '')[-90:]}"
    try: v=max(0.0,min(100.0,float(d["plastic"])))
    except Exception: return None,f"bad {d.get('plastic')}"
    reason=re.sub(r'[一-鿿]','',str(d.get("reason","")))[:60]
    return v,reason

# данные
sh(f'rclone copy "{YD}/frames3" "{FR}" --transfers 8')
sh(f'rclone copyto "{YD}/labels.csv" "{WORK}/labels.csv"')
labels={}
with open(f"{WORK}/labels.csv") as fh:
    for row in csv.DictReader(fh):
        labels[row["file"].replace(".jpg","")]=(int(row["plastic_pct"]),row["gen"])
print(f"меток: {len(labels)}")

rows=[]
for name,(pct,gen) in sorted(labels.items()):
    s=make_strip(name)
    if not s: print(f"  ! нет кадров {name}"); continue
    sc,reason=judge(s)
    rows.append({"file":name,"gen":gen,"plastic_pct":pct,"vlm":sc,"reason":reason})
    print(f"  {name:12} метка={pct:3}%  mimo={sc}  ({reason})")

ok=[r for r in rows if r["vlm"] is not None]
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
L=["# mimo-судья v2 (обучен рубрикой Claude + склейка 3 кадров) vs разметка",
   f"\nКадров: {len(ok)}/{len(rows)} (пластик ≥50%: {len(bad)}, живые <50%: {len(good)})",
   "\n## Корреляция (метка ↔ mimo-v2)",
   f"- **Spearman:** {sp:+.3f}",
   f"- **Pearson:**  {pe:+.3f}",
   f"- **AUC (mimo у пластика > у живых):** {None if a is None else round(a,3)}",
   "\n## Эволюция",
   "- mimo v1 (стилл, без рубрики): −0.10 (путал гладкость с пластиком)",
   f"- **mimo v2 (рубрика Claude + 3 кадра): {sp:+.2f}, AUC {None if a is None else round(a,2)}**",
   "- (для сравнения: Gemini v1 +0.47, Claude-ранг ~сильный)",
   f"\nСредний mimo: живые={np.mean(good):.1f} / пластик={np.mean(bad):.1f}" if good and bad else "",
   "\n## Таблица (по метке)","| видео | ген | метка% | mimo | причина |","|---|---|---|---|---|"]
for r in sorted(rows,key=lambda r:r["plastic_pct"]):
    L.append(f"| {r['file']} | {r['gen']} | {r['plastic_pct']} | {r['vlm']} | {r['reason']} |")
verdict=("РУБРИКА ПОМОГЛА — mimo v2 вышел в плюс (Spearman %.2f), free-детектор реален." % sp if sp>0.4 else
         "Рубрика сдвинула mimo (с −0.10 до %.2f), но %s." % (sp, "ещё слабо" if sp<0.4 else "ок"))
L.append(f"\n## Вывод\n**{verdict}**")
summ="\n".join([x for x in L if x!=""])
open(os.path.join(WORK,"summary.md"),"w").write(summ)
print("\n"+summ)
sh(f'rclone copy "{csv_p}" "{YD}/result_mimo_v2/"')
sh(f'rclone copy "{os.path.join(WORK,"summary.md")}" "{YD}/result_mimo_v2/"')
print(f"\n✓ → {YD}/result_mimo_v2/")
