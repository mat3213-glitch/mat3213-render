"""
PROBE i2v-таба veoaifree (Image to Video Seedance 2.0, tab3) на US-раннере.
Переключает на таб i2v, грузит картинку (с ЯД), генерит видео по ней, качает → ЯД.
Разведка структуры таба + щедрые скрины (таб незнаком).

Env: YADISK_LOGIN/PASSWORD, IMG_REMOTE (путь картинки на ЯД), PROMPT, DEST_FOLDER, OUT_NAME
"""
import os, time, requests
from pathlib import Path
from urllib.parse import quote as urlquote
from playwright.sync_api import sync_playwright

YL=os.environ["YADISK_LOGIN"]; YP=os.environ["YADISK_PASSWORD"]
IMG_REMOTE=os.environ.get("IMG_REMOTE","Content factory/_probe_i2v_in/anchor.png")
PROMPT=os.environ.get("PROMPT","slow immersive sinking deeper into clear blue water, gentle light rays, subtle drift, no text, no people")
DEST=os.environ.get("DEST_FOLDER","Content factory/_probe_i2v")
OUT=os.environ.get("OUT_NAME","i2v_clip.mp4")
URL=os.environ.get("VEO_URL","https://veoaifree.com/photo-and-image-to-video-generator/")
WEBDAV="https://webdav.yandex.ru"; AUTH=(YL,YP); TMP=Path("/tmp/i2v"); TMP.mkdir(exist_ok=True)
R=[]
def log(s): print(s,flush=True); R.append(str(s))
def yd_mkcol(p):
    c=""
    for x in p.split("/"):
        c=f"{c}/{x}" if c else x; requests.request("MKCOL",f"{WEBDAV}/{urlquote(c)}",auth=AUTH,timeout=30)
def yd_get(remote,local):
    r=requests.get(f"{WEBDAV}/{urlquote(remote)}",auth=AUTH,timeout=120)
    if r.status_code==200: Path(local).write_bytes(r.content); return True
    log(f"yd_get {remote} -> {r.status_code}"); return False
def yd_put(local,remote):
    for _ in range(4):
        try:
            with open(local,"rb") as f:
                if requests.put(f"{WEBDAV}/{urlquote(remote)}",data=f,auth=AUTH,timeout=600).status_code in (200,201,204):
                    log(f"  up ok {remote}"); return True
        except Exception as e: log(f"  up err {e}")
        time.sleep(4)
    return False
def paywall(pg):
    for sel in [".pf-btn","#pfEmail",".plan-btn",".btn-month",".btn-life"]:
        try:
            el=pg.query_selector(sel)
            if el and el.is_visible(): return True
        except: pass
    return False
def dismiss(pg):
    for sel in ["#pfClose",".pf-close","#closeBtn",".close-btn","#ab-allow",
                "button:has-text('Accept')","button:has-text('Got it')"]:
        try:
            el=pg.query_selector(sel)
            if el and el.is_visible(): el.click(timeout=2000); pg.wait_for_timeout(400)
        except: pass

log(f"=== VEOFREE i2v PROBE === IMG={IMG_REMOTE}\nPROMPT: {PROMPT}")
try: log(f"runner IP: {requests.get('https://api.ipify.org',timeout=15).text}")
except: pass
if not yd_get(IMG_REMOTE, TMP/"in.png"):
    log("НЕТ входной картинки — выход");
else:
    log(f"input image: {(TMP/'in.png').stat().st_size//1024}KB")

video_url=None; status="?"
with sync_playwright() as pw:
    br=pw.chromium.launch(headless=True,args=["--no-sandbox"])
    ctx=br.new_context(viewport={"width":1280,"height":1200},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")
    pg=ctx.new_page()
    seen=set()
    pg.on("response", lambda r: seen.add(r.url) if ".mp4" in r.url and "/video/uploads/" in r.url else None)
    pg.goto(URL,wait_until="domcontentloaded",timeout=60000); pg.wait_for_timeout(5000)
    dismiss(pg)

    # выделенная i2v-страница: таб не нужен, но если есть #tab3-btn — кликнем (best-effort, коротко)
    try:
        t3=pg.query_selector("#tab3-btn")
        if t3 and t3.is_visible():
            t3.click(timeout=3000); pg.wait_for_timeout(2000); log("кликнул #tab3-btn")
        else:
            log("tab3 нет/скрыт — это выделенная i2v-страница, ок")
    except Exception as e:
        log(f"tab3 best-effort: {e}")
    pg.screenshot(path=str(TMP/"01_tab3.png"))

    # разведка структуры i2v-таба (видимые элементы)
    def dump(sel, attrs):
        out=[]
        for el in pg.query_selector_all(sel):
            try:
                if not el.is_visible(): continue
                d={a:(el.get_attribute(a) or "") for a in attrs}; d["txt"]=(el.inner_text() or "")[:30]
                out.append(d)
            except: pass
        return out
    log("-- file inputs (видимые/все) --")
    allfiles=pg.query_selector_all("input[type=file]")
    log(f"  всего file inputs: {len(allfiles)}")
    [log(f"  vis: {x}") for x in dump("input[type=file]",["id","name","accept"])]
    log("-- textareas видимые --"); [log(f"  {x}") for x in dump("textarea",["id","name","placeholder"])]
    log("-- кнопки видимые --"); [log(f"  {x}") for x in dump("button",["id","class"])[:20]]

    # грузим картинку в первый file input (видимый приоритетно, иначе любой)
    fi=None
    for el in allfiles:
        try:
            if el.is_visible(): fi=el; break
        except: pass
    if not fi and allfiles: fi=allfiles[0]
    if fi:
        try:
            fi.set_input_files(str(TMP/"in.png")); log("картинка загружена в file input")
            pg.wait_for_timeout(6000)   # дать обработать загрузку (превью/аплоад)
        except Exception as e: log(f"set_input_files err: {e}")
    else:
        log("НЕ нашёл file input")
    pg.screenshot(path=str(TMP/"02_uploaded.png"))

    # промпт — прицельно в i2v-textarea
    ta=pg.query_selector("#fn__include_textarea_img_video") or next(
        (t for t in pg.query_selector_all("textarea") if t.is_visible()), None)
    if ta:
        try: ta.click(); ta.fill(PROMPT); log("промпт введён")
        except Exception as e: log(f"prompt err {e}")

    # GENERATE — прицельно по id i2v-кнопки, со scroll + force
    clicked=False
    gb=pg.query_selector("#generate_it_img_video") or pg.query_selector("#generate_it")
    if gb:
        try: gb.scroll_into_view_if_needed(timeout=4000)
        except: pass
        dismiss(pg)
        for force in (False, True):
            try:
                gb.click(timeout=8000, force=force); clicked=True
                log(f"клик GENERATE id={gb.get_attribute('id')} force={force}"); break
            except Exception as e: log(f"click force={force} err: {e}")
    log(f"generate clicked: {clicked}")

    for _ in range(40):
        pg.wait_for_timeout(5000)
        if paywall(pg): status="paywall"; break
        v=pg.query_selector("video"); src=v.get_attribute("src") if v else None
        if (src and src.startswith("http")) or seen:
            video_url=src if (src and src.startswith("http")) else sorted(seen)[-1]; status="ok"; break
    else: status="timeout"
    pg.screenshot(path=str(TMP/"03_after_gen.png"))
    br.close()

log(f"status: {status}  video_url: {video_url}")
ok=False
if video_url and ".mp4" in (video_url or ""):
    try:
        r=requests.get(video_url,timeout=180,headers={"User-Agent":"Mozilla/5.0"})
        if r.status_code==200 and len(r.content)>10000:
            (TMP/OUT).write_bytes(r.content); log(f"downloaded {len(r.content)//1024}KB")
            ok=True
    except Exception as e: log(f"dl err {e}")

(TMP/"report.txt").write_text("\n".join(R),encoding="utf-8")
yd_mkcol(DEST)
for f in TMP.iterdir():
    if f.name!="in.png": yd_put(f,f"{DEST}/{f.name}")
log("DONE ok" if ok else "DONE (см. отчёт/скрины)")
