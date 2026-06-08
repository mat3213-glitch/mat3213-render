"""
PROBE veoaifree.com (Seedance 2.0 t2v) на раннере GH Actions (US-IP, без логина).
Разведка + попытка генерации в одном прогоне:
  - открывает страницу, дампит inputs/buttons/textareas/video/iframe + скриншоты
  - пробует ввести промпт и нажать Generate
  - ждёт <video>/download, ловит src → качает mp4
  - всё (report.txt + скрины + видео) льёт на ЯД для разбора с локальной машины

Env: YADISK_LOGIN/YADISK_PASSWORD, PROMPT (есть дефолт), DEST_FOLDER
"""
import os, time, json, requests
from pathlib import Path
from urllib.parse import quote as urlquote
from playwright.sync_api import sync_playwright

YADISK_LOGIN = os.environ["YADISK_LOGIN"]; YADISK_PASS = os.environ["YADISK_PASSWORD"]
PROMPT = os.environ.get("PROMPT",
    "slow cinematic drift through deep blue water, soft light rays descending into the dark, "
    "film grain, calm meditative depth, no text, no people")
URL = os.environ.get("VEO_URL", "https://veoaifree.com/seedance-2-0-video-generator-free/")
DEST = os.environ.get("DEST_FOLDER", "Content factory/_probe_veo")
WEBDAV = "https://webdav.yandex.ru"; AUTH = (YADISK_LOGIN, YADISK_PASS)
TMP = Path("/tmp/veo"); TMP.mkdir(exist_ok=True)
R = []
def log(s): print(s, flush=True); R.append(str(s))

def yd_mkcol(path):
    cur=""
    for p in path.split("/"):
        cur=f"{cur}/{p}" if cur else p
        requests.request("MKCOL", f"{WEBDAV}/{urlquote(cur)}", auth=AUTH, timeout=30)
def yd_put(local, remote):
    for a in range(3):
        try:
            with open(local,"rb") as f:
                r=requests.put(f"{WEBDAV}/{urlquote(remote)}", data=f, auth=AUTH, timeout=300)
            if r.status_code in (200,201,204): print(f"  up ok {remote}",flush=True); return True
        except Exception as e: print(f"  up err {e}",flush=True)
        time.sleep(3)
    return False

log(f"=== VEOFREE PROBE (runner US-IP) ===\nURL: {URL}\nPROMPT: {PROMPT}")
try:
    ip=requests.get("https://api.ipify.org",timeout=15).text
    log(f"runner IP: {ip}")
except: pass

video_url=None
with sync_playwright() as pw:
    br=pw.chromium.launch(headless=True, args=["--no-sandbox"])
    ctx=br.new_context(viewport={"width":1280,"height":900},
                       user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")
    pg=ctx.new_page()
    # ловим сетевые ответы с видео
    seen=set()
    def on_resp(resp):
        u=resp.url
        if any(x in u for x in [".mp4",".m3u8"]) and u not in seen:
            seen.add(u); log(f"  [net video] {u[:140]}")
    pg.on("response", on_resp)
    try:
        pg.goto(URL, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log(f"goto err: {e}")
    pg.wait_for_timeout(5000)
    log(f"title: {pg.title()}")
    pg.screenshot(path=str(TMP/"01_loaded.png"), full_page=True)

    # дамп интерактивных элементов
    def dump(sel, attrs):
        out=[]
        for el in pg.query_selector_all(sel):
            try:
                d={a:(el.get_attribute(a) or "") for a in attrs}
                d["text"]=(el.inner_text() or "")[:40]
                out.append(d)
            except: pass
        return out
    log("\n-- textareas --");   [log(f"  {x}") for x in dump("textarea",["placeholder","name","id"])[:10]]
    log("-- inputs --");        [log(f"  {x}") for x in dump("input",["placeholder","name","type","id"])[:15]]
    log("-- contenteditable --");[log(f"  {x}") for x in dump("[contenteditable]",["id","class"])[:5]]
    log("-- buttons --");       [log(f"  {x}") for x in dump("button",["id","class"])[:20]]
    log("-- video --");         [log(f"  {x}") for x in dump("video",["src","id"])[:5]]
    log("-- iframe --");        [log(f"  {x}") for x in dump("iframe",["src","id"])[:5]]

    # попытка ввода промпта
    filled=False
    for sel in ["textarea","[contenteditable='true']","input[type='text']"]:
        el=pg.query_selector(sel)
        if el:
            try:
                el.click(); el.fill(PROMPT) if sel=="textarea" or sel.startswith("input") else el.type(PROMPT)
                log(f"  filled prompt via {sel}"); filled=True; break
            except Exception as e: log(f"  fill {sel} err {e}")
    log(f"prompt filled: {filled}")

    # кнопка генерации (по тексту)
    clicked=False
    for txt in ["Generate","Создать","Create","Generate Video","生成","Render","Сгенерировать"]:
        try:
            b=pg.get_by_role("button", name=txt, exact=False)
            if b.count()>0:
                b.first.click(timeout=8000); log(f"  clicked button '{txt}'"); clicked=True; break
        except Exception as e: pass
    if not clicked:
        log("  no generate button matched — пробую первую видимую кнопку")
        try: pg.query_selector_all("button")[0].click(timeout=5000); clicked=True
        except Exception as e: log(f"  fallback click err {e}")
    log(f"generate clicked: {clicked}")

    # ждём видео до 150с
    for i in range(30):
        pg.wait_for_timeout(5000)
        v=pg.query_selector("video")
        src=v.get_attribute("src") if v else None
        if src and src.startswith("http"): video_url=src; log(f"  <video src> найден: {src[:120]}"); break
        if seen: video_url=sorted(seen)[-1]; break
    pg.screenshot(path=str(TMP/"02_after_gen.png"), full_page=True)
    br.close()

# скачать видео если поймали
if video_url and ".mp4" in video_url:
    try:
        r=requests.get(video_url, timeout=120, headers={"User-Agent":"Mozilla/5.0"})
        if r.status_code==200 and len(r.content)>10000:
            (TMP/"veo_out.mp4").write_bytes(r.content)
            log(f"\n✅ DOWNLOADED {len(r.content)//1024}KB")
        else: log(f"\n video dl status {r.status_code} bytes {len(r.content)}")
    except Exception as e: log(f"\n video dl err {e}")
else:
    log(f"\n video_url: {video_url} (m3u8 или не пойман — см. скрины/отчёт)")

# заливка результатов на ЯД
(TMP/"report.txt").write_text("\n".join(R), encoding="utf-8")
yd_mkcol(DEST)
for f in ["report.txt","01_loaded.png","02_after_gen.png","veo_out.mp4"]:
    p=TMP/f
    if p.exists(): yd_put(p, f"{DEST}/{f}")
log("DONE")
