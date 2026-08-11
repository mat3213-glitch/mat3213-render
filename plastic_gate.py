#!/usr/bin/env python3
"""
plastic_gate.py — БОЕВОЙ ГЕЙТ пластмассовости для пула генерации.

Переиспользуемый шаг конвейера: берёт папку-пул на ЯД (png/mp4), склеивает
3 кадра видео и опрашивает CF/OpenRouter vision-ансамбль.

БЕЗОПАСНО: по умолчанию --dry-run (только gate_report.json, ничего не двигает).
С --enforce отбракованные ПЕРЕМЕЩАЮТСЯ в <pool>/_rejected/ (ОБРАТИМО, не удаляются).

Запуск (GH workflow plastic_gate.yml): POOL=cloud_io/<pool> THRESHOLD=55 ENFORCE=0|1 python plastic_gate.py
"""
import base64
import os, re, json, subprocess, tempfile, glob
from PIL import Image
from art_judge import JUDGES, PANEL, ask_vision

YD_ROOT = "ydrive:Content factory"
POOL = os.environ["POOL"].strip().lstrip("/")                  # путь относительно "Content factory/", напр. cloud_io/qwen_pool/2026-06-26
POOL_YD = f"{YD_ROOT}/{POOL}"
THRESHOLD = float(os.environ.get("THRESHOLD", "55"))           # reject если score >= THRESHOLD
ENFORCE = os.environ.get("ENFORCE", "0") == "1"                # двигать отбракованные
WORK = tempfile.mkdtemp(prefix="gate_")
LOCAL = os.path.join(WORK, "media"); os.makedirs(LOCAL, exist_ok=True)
STRIPS = os.path.join(WORK, "strips"); os.makedirs(STRIPS, exist_ok=True)

RUBRIC = (
    "Перед тобой кадр(ы) AI-видео/картинки. Оцени ПЛАСТМАССОВОСТЬ = неправдоподобие (НЕ красоту):\n"
    "ВЫСОКИЙ 70-100: AI-текст-каракули (нечитаемые цифры/буквы); идеально симметричный/искусственный "
    "свет, нереальный источник, CGI-рендер, идеальное зеркальное отражение; восковая гладкая кожа рук, "
    "деформации анатомии; огонь приклеен ровным контуром, искры как фейерверк.\n"
    "СРЕДНИЙ 40-70: мох/лишайник «налеплен», повторяющаяся одинаковая текстура коры/листвы; геометрия "
    "фасадов/зданий «плывёт», нерегулярные окна, кривые/сливающиеся объекты; объекты статичны там, где "
    "должны двигаться (вода не течёт, капли висят без гравитации).\n"
    "НИЗКИЙ 0-30 (НОРМА, НЕ штрафуй): туман/небо/лес/облака/ландшафт/дымка даже ГЛАДКИЕ; реалистичное "
    "пламя свечи, боке огней, капли на стекле с правильной оптикой, рукопись; тёмный фон и малое движение.\n"
    "ГЛАВНОЕ: отличай НАЛЕПЛЕННУЮ/ПОВТОРЯЮЩУЮСЯ текстуру и искажённую геометрию (пластик) от РОВНОЙ "
    "АТМОСФЕРЫ (норма).\n"
    "Ответь СТРОГО одним JSON: {\"plastic\": <0-100>, \"reason\": \"<кратко>\"}. Только JSON."
)

def sh(c): return subprocess.run(c, capture_output=True, text=True)
def strip_ansi(t): return re.sub(r'\x1B\[[0-9;]*[A-Za-z]', '', t)

def extract_json(t):
    t=strip_ansi(t); depth=0; start=-1
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

def frames_of(path):
    """png → [сам файл]; mp4 → 3 извлечённых кадра (20/50/80%)."""
    ext=os.path.splitext(path)[1].lower()
    if ext in (".png",".jpg",".jpeg"): return [path]
    dur=sh(["ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", path]).stdout.strip()
    try: dur=float(dur)
    except Exception: dur=6.0
    out=[]
    base=os.path.splitext(os.path.basename(path))[0]
    for k,pct in enumerate((0.2,0.5,0.8),1):
        f=os.path.join(STRIPS,f"{base}_{k}.jpg")
        sh(["ffmpeg", "-nostdin", "-y", "-ss", f"{dur*pct:.2f}", "-i", path,
            "-frames:v", "1", "-q:v", "3", "-vf", "scale=512:-1", f])
        if os.path.exists(f): out.append(f)
    return out

def make_strip(frames, name):
    imgs=[Image.open(f).convert("RGB") for f in frames if os.path.exists(f)]
    if not imgs: return None
    if len(imgs)==1:
        out=os.path.join(STRIPS,f"{name}_one.jpg"); imgs[0].save(out,quality=88); return out
    h=min(im.height for im in imgs)
    imgs=[im.resize((int(im.width*h/im.height),h)) for im in imgs]
    strip=Image.new("RGB",(sum(im.width for im in imgs),h)); x=0
    for im in imgs: strip.paste(im,(x,0)); x+=im.width
    out=os.path.join(STRIPS,f"{name}_strip.jpg"); strip.save(out,quality=88); return out

def judge(strip, timeout=180):
    b64 = base64.b64encode(open(strip, "rb").read()).decode()
    votes, reasons, errors = [], [], []
    for provider in JUDGES:
        d, err = ask_vision(provider, RUBRIC, b64)
        if d and "plastic" in d:
            try:
                votes.append(max(0.0, min(100.0, float(d["plastic"]))))
                reasons.append(re.sub(r'[一-鿿]', '', str(d.get("reason", "")))[:70])
            except (TypeError, ValueError):
                errors.append(f"{provider}:bad-num")
        else:
            errors.append(f"{provider}:{err or 'no-json'}")
        if len(votes) >= PANEL:
            break
    if not votes:
        return None, "; ".join(errors)[:160] or "no-votes"
    return round(sum(votes) / len(votes), 1), " | ".join(reasons)[:160]

# скачать пул (медиа)
sh(["rclone", "copy", POOL_YD, LOCAL, "--include", "*.mp4", "--include", "*.png",
    "--include", "*.jpg", "--include", "*.jpeg", "--transfers", "6", "--max-depth", "1"])
media=sorted([f for f in glob.glob(LOCAL+"/*") if os.path.splitext(f)[1].lower() in (".mp4",".png",".jpg",".jpeg")])
print(f"ГЕЙТ пула {POOL} — медиа: {len(media)}, порог reject>= {THRESHOLD}, enforce={ENFORCE}")

results=[]
for path in media:
    name=os.path.splitext(os.path.basename(path))[0]
    strip=make_strip(frames_of(path), name)
    if not strip:
        results.append({"file":os.path.basename(path),"score":None,"verdict":"skip","reason":"нет кадров"}); continue
    sc,reason=judge(strip)
    verdict = "skip" if sc is None else ("REJECT" if sc>=THRESHOLD else "pass")
    results.append({"file":os.path.basename(path),"score":sc,"verdict":verdict,"reason":reason})
    print(f"  {os.path.basename(path):28} score={sc}  {verdict}  ({reason})")

rejected=[r for r in results if r["verdict"]=="REJECT"]
passed  =[r for r in results if r["verdict"]=="pass"]
report={"pool":POOL,"threshold":THRESHOLD,"enforce":ENFORCE,"detector":"cf+openrouter vision ensemble",
        "total":len(results),"passed":len(passed),"rejected":len(rejected),"skipped":len(results)-len(passed)-len(rejected),
        "items":results}
rp=os.path.join(WORK,"gate_report.json"); open(rp,"w").write(json.dumps(report,ensure_ascii=False,indent=2))
sh(["rclone", "copyto", rp, f"{POOL_YD}/gate_report.json"])

print(f"\n=== ИТОГ: pass={len(passed)} reject={len(rejected)} skip={report['skipped']} ===")
if ENFORCE and rejected:
    for r in rejected:
        sh(["rclone", "moveto", f"{POOL_YD}/{r['file']}", f"{POOL_YD}/_rejected/{r['file']}"])
    print(f"ENFORCE: {len(rejected)} отбракованных → {POOL_YD}/_rejected/ (обратимо)")
elif rejected:
    print(f"DRY-RUN: {len(rejected)} помечены REJECT (не двигаю). Запусти с ENFORCE=1 чтобы переместить.")
print(f"отчёт → {POOL_YD}/gate_report.json")
