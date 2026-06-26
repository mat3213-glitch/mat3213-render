"""
VeoFree i2v — ОДНА генерация видео из фото на прогон (свежий IP, обход лимита 1/IP).
Берёт СЫРОЙ стоковый кадр (не уникализированный — i2v сам = уникализация), кропит 9:16, генерит видео → ЯД.

Страница: veoaifree.com/photo-and-image-to-video-generator/
Флоу: upload файла → crop-модалка (выбрать ASPECT в select → кнопка Upload) → промпт → GENERATE → видео.

Env: YADISK_LOGIN/PASSWORD, IMG_REMOTE (путь кадра на ЯД), PROMPT, DEST_FOLDER, OUT_NAME, ASPECT (деф 9:16)
"""
import os, time, subprocess, requests
from pathlib import Path
from playwright.sync_api import sync_playwright

IMG_REMOTE=os.environ["IMG_REMOTE"]
PROMPT=os.environ.get("PROMPT","slow subtle cinematic motion, gentle drift, film grain, no text, no people")
DEST=os.environ.get("DEST_FOLDER","Content factory/cloud_io/veofree_i2v/batch")
OUT=os.environ.get("OUT_NAME","i2v_clip.mp4");  OUT = OUT if OUT.endswith(".mp4") else OUT+".mp4"
ASPECT=os.environ.get("ASPECT","9:16")
URL="https://veoaifree.com/photo-and-image-to-video-generator/"
TMP=Path("/tmp/i2vgen"); TMP.mkdir(exist_ok=True)
def log(s): print(s,flush=True)
# ЯД через rclone ydrive: (WebDAV мёртв → SSLError; copyto сам создаёт родительские папки)
def yd_mkcol(p): pass  # no-op: rclone copyto создаёт дерево папок при заливке
def yd_get(remote,local):
    r=subprocess.run(["rclone","copyto",f"ydrive:{remote}",str(local)],
                     capture_output=True,text=True,timeout=180)
    if r.returncode==0 and Path(local).exists(): return True
    log(f"yd_get {remote} -> rc={r.returncode} {r.stderr[:200]}"); return False
def yd_put(local,remote):
    for _ in range(3):
        r=subprocess.run(["rclone","copyto",str(local),f"ydrive:{remote}"],
                         capture_output=True,text=True,timeout=600)
        if r.returncode==0: log(f"  up ok {remote}"); return True
        log(f"  up err rc={r.returncode} {r.stderr[:200]}"); time.sleep(4)
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

log(f"=== VEOFREE i2v GEN === OUT={OUT} ASPECT={ASPECT}\nIMG={IMG_REMOTE}\nPROMPT: {PROMPT}")
try: log(f"runner IP: {requests.get('https://api.ipify.org',timeout=15).text}")
except: pass
if not yd_get(IMG_REMOTE, TMP/"in.png"):
    yd_mkcol(DEST); (TMP/f"{OUT}.FAILED.txt").write_text("no input image",encoding="utf-8")
    yd_put(TMP/f"{OUT}.FAILED.txt", f"{DEST}/{OUT}.FAILED.txt"); raise SystemExit("no input")
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

    fi=next((el for el in pg.query_selector_all("input[type=file]") if el.is_visible()), None) \
       or (pg.query_selector_all("input[type=file]") or [None])[0]
    if fi:
        fi.set_input_files(str(TMP/"in.png")); log("картинка загружена"); pg.wait_for_timeout(6000)

    # crop-модалка: выбрать ASPECT в select, подтвердить Upload
    try:
        cm=pg.query_selector("#cropModal")
        if cm and cm.is_visible():
            sel=pg.query_selector("#cropModal select") or pg.query_selector("#cropModal [role=combobox]")
            if sel:
                opts=pg.evaluate("(s)=>Array.from(s.options).map(o=>({v:o.value,t:o.textContent.trim()}))", sel)
                log(f"crop aspect options: {opts}")
                want=ASPECT.replace(":", "").lower()  # '916'
                pick=None
                for o in opts:
                    t=o["t"].lower().replace(":","").replace(" ","")
                    if want in t or "vertical" in t or "portrait" in t or "9:16" in o["t"]:
                        pick=o["v"]; break
                if pick is not None:
                    pg.select_option("#cropModal select", value=pick)
                    # явно дёрнуть change/input — cropper.js слушает их
                    pg.eval_on_selector("#cropModal select",
                        "s=>{s.dispatchEvent(new Event('input',{bubbles:true}));s.dispatchEvent(new Event('change',{bubbles:true}));}")
                    pg.wait_for_timeout(2500)
                    pg.screenshot(path=str(TMP/"cropcheck.png"))   # диагностика: каким стал crop-бокс
                    log(f"aspect выбран: {pick}")
                else:
                    log("9:16 опция не найдена — оставляю дефолт")
            else:
                log("select соотношения не найден")
            cb=pg.query_selector("#cropModal button:has-text('Upload')") or pg.query_selector("#cropModal .btn-primary")
            if cb:
                try: cb.click(timeout=8000)
                except Exception: cb.click(timeout=8000, force=True)
                for _ in range(15):
                    pg.wait_for_timeout(1000)
                    c2=pg.query_selector("#cropModal")
                    if not (c2 and c2.is_visible()): break
                pg.wait_for_timeout(3000); log("crop подтверждён")
            else: log("кнопка Upload в crop не найдена")
        else:
            log("crop-модалка не появилась")
    except Exception as e: log(f"crop err: {e}")

    ta=pg.query_selector("#fn__include_textarea_img_video") or next(
        (t for t in pg.query_selector_all("textarea") if t.is_visible()), None)
    if ta:
        try: ta.click(); ta.fill(PROMPT); log("промпт введён")
        except Exception as e: log(f"prompt err {e}")
    gb=pg.query_selector("#generate_it_img_video") or pg.query_selector("#generate_it")
    clicked=False
    if gb:
        try: gb.scroll_into_view_if_needed(timeout=4000)
        except: pass
        dismiss(pg)
        for force in (False, True):
            try: gb.click(timeout=8000, force=force); clicked=True; log(f"GENERATE force={force}"); break
            except Exception as e: log(f"click force={force}: {e}")
    log(f"generate clicked: {clicked}")

    for _ in range(40):
        pg.wait_for_timeout(5000)
        if paywall(pg): status="paywall"; break
        v=pg.query_selector("video"); src=v.get_attribute("src") if v else None
        if (src and src.startswith("http")) or seen:
            video_url=src if (src and src.startswith("http")) else sorted(seen)[-1]; status="ok"; break
    else: status="timeout"
    if status!="ok":
        try: pg.screenshot(path=str(TMP/"fail.png"))
        except: pass
    br.close()

log(f"status: {status}  video_url: {video_url}")
ok=False
if video_url and ".mp4" in video_url:
    try:
        r=requests.get(video_url,timeout=180,headers={"User-Agent":"Mozilla/5.0"})
        if r.status_code==200 and len(r.content)>10000:
            (TMP/OUT).write_bytes(r.content); log(f"downloaded {len(r.content)//1024}KB")
            yd_mkcol(DEST); ok=yd_put(TMP/OUT,f"{DEST}/{OUT}")
    except Exception as e: log(f"dl err {e}")
if (TMP/"cropcheck.png").exists(): yd_put(TMP/"cropcheck.png", f"{DEST}/{OUT}.cropcheck.png")  # диагностика всегда
if not ok:
    yd_mkcol(DEST)
    (TMP/f"{OUT}.FAILED.txt").write_text(f"status={status}\nurl={video_url}",encoding="utf-8")
    yd_put(TMP/f"{OUT}.FAILED.txt", f"{DEST}/{OUT}.FAILED.txt")
    if (TMP/"fail.png").exists(): yd_put(TMP/"fail.png", f"{DEST}/{OUT}.fail.png")
log("DONE ok" if ok else "DONE fail")
