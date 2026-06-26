"""
VeoFree (Seedance 2.0) — ОДНА генерация на прогон (= свежий IP раннера, обход лимита 1/IP).
Промпт → видео → ЯД (DEST_FOLDER/OUT_NAME). Без full_page-скринов (висли на WebDAV).

Env (GitHub Secrets / inputs):
  YADISK_LOGIN / YADISK_PASSWORD
  PROMPT      — текст промпта
  DEST_FOLDER — папка ЯД (напр. "Content factory/cloud_io/veofree/2026-06-08_1200")
  OUT_NAME    — имя файла (напр. "clip_01.mp4")
"""
import os, sys, time, subprocess, requests
from pathlib import Path
from playwright.sync_api import sync_playwright

PROMPT=os.environ.get("PROMPT","slow cinematic drift through deep blue water, light rays into the dark, film grain, no text, no people")
DEST=os.environ.get("DEST_FOLDER","Content factory/cloud_io/veofree/batch")
OUT=os.environ.get("OUT_NAME","clip.mp4")
if not OUT.endswith(".mp4"): OUT+=".mp4"
URL="https://veoaifree.com/seedance-2-0-video-generator-free/"
TMP=Path("/tmp/veogen"); TMP.mkdir(exist_ok=True)

def log(s): print(s,flush=True)
# ЯД через rclone ydrive: (WebDAV мёртв → SSLError; copyto сам создаёт родительские папки)
def yd_mkcol(p): pass  # no-op: rclone copyto создаёт дерево папок при заливке
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

log(f"=== VEOFREE GEN === OUT={OUT}\nPROMPT: {PROMPT}")
try: log(f"runner IP: {requests.get('https://api.ipify.org',timeout=15).text}")
except: pass

video_url=None; status="?"
with sync_playwright() as pw:
    br=pw.chromium.launch(headless=True,args=["--no-sandbox"])
    ctx=br.new_context(viewport={"width":1280,"height":900},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")
    pg=ctx.new_page()
    seen=set()
    pg.on("response", lambda r: seen.add(r.url) if ".mp4" in r.url and "/video/uploads/" in r.url else None)
    pg.goto(URL,wait_until="domcontentloaded",timeout=60000); pg.wait_for_timeout(5000)
    dismiss(pg)
    ta=pg.query_selector("textarea#fn__include_textarea") or pg.query_selector("textarea")
    btn=pg.query_selector("#generate_it")
    if ta and btn:
        ta.click(); ta.fill(PROMPT)
        dismiss(pg)                                   # убрать рекламу/попапы перед кликом
        try: btn.scroll_into_view_if_needed(timeout=4000)
        except: pass
        try: btn.click(timeout=8000)
        except Exception:
            try: btn.click(timeout=8000, force=True)   # сквозь возможный оверлей
            except: pass
        for _ in range(40):                            # до 200с (генерация бывает медленной)
            pg.wait_for_timeout(5000)
            if paywall(pg): status="paywall"; break
            v=pg.query_selector("video"); src=v.get_attribute("src") if v else None
            if (src and src.startswith("http")) or seen:
                video_url=src if (src and src.startswith("http")) else sorted(seen)[-1]
                status="ok"; break
        else: status="timeout"
    else:
        status="no_ui"
    if status!="ok":
        try: pg.screenshot(path=str(TMP/"fail.png"))  # viewport-only, лёгкий
        except: pass
    br.close()

log(f"status: {status}  video_url: {video_url}")
ok=False
if video_url:
    try:
        r=requests.get(video_url,timeout=180,headers={"User-Agent":"Mozilla/5.0"})
        if r.status_code==200 and len(r.content)>10000:
            (TMP/OUT).write_bytes(r.content); log(f"downloaded {len(r.content)//1024}KB")
            yd_mkcol(DEST); ok=yd_put(TMP/OUT,f"{DEST}/{OUT}")
        else: log(f"dl status {r.status_code} bytes {len(r.content)}")
    except Exception as e: log(f"dl err {e}")
if not ok:
    yd_mkcol(DEST)
    (TMP/f"{OUT}.FAILED.txt").write_text(f"status={status}\nurl={video_url}\nprompt={PROMPT}",encoding="utf-8")
    yd_put(TMP/f"{OUT}.FAILED.txt", f"{DEST}/{OUT}.FAILED.txt")
    if (TMP/"fail.png").exists(): yd_put(TMP/"fail.png", f"{DEST}/{OUT}.fail.png")
log("DONE ok" if ok else "DONE fail")
sys.exit(0 if ok else 1)   # честный код: daily/воркфлоу видят реальный исход аплоада
