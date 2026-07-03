"""
veofree_probe_dom.py — ОДНОРАЗОВЫЙ диагностический пробник (не боевой скрипт).
Повторяет флоу veofree_i2v_gen.py, но вместо строгого детектора видео — дампит HTML вокруг
плейсхолдера на каждой итерации поллинга + финальный полный HTML, чтобы найти актуальный
селектор готового видео (сайт мог обновить вёрстку с момента последней проверки).

Env: IMG_REMOTE, PROMPT (опц.)
"""
import os, subprocess, time
from pathlib import Path
from playwright.sync_api import sync_playwright

IMG_REMOTE = os.environ["IMG_REMOTE"]
PROMPT = os.environ.get("PROMPT", "slow subtle drift, gentle rain motion, cinematic")
URL = "https://veoaifree.com/photo-and-image-to-video-generator/"
TMP = Path("/tmp/probe"); TMP.mkdir(exist_ok=True)


def log(s):
    print(s, flush=True)


def yd_get(remote, local):
    r = subprocess.run(["rclone", "copyto", f"ydrive:{remote}", str(local)],
                       capture_output=True, text=True, timeout=180)
    return r.returncode == 0


def yd_put(local, remote):
    subprocess.run(["rclone", "copyto", str(local), f"ydrive:{remote}"],
                   capture_output=True, text=True, timeout=300)


yd_get(IMG_REMOTE, TMP / "in.png")
log(f"input: {(TMP/'in.png').stat().st_size}B")

with sync_playwright() as pw:
    br = pw.chromium.launch(headless=True, args=["--no-sandbox"])
    ctx = br.new_context(viewport={"width": 1280, "height": 1200},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")
    pg = ctx.new_page()
    net_log = []
    pg.on("response", lambda r: net_log.append(r.url) if (".mp4" in r.url or "video" in r.url.lower()) else None)
    pg.goto(URL, wait_until="domcontentloaded", timeout=60000)
    pg.wait_for_timeout(5000)
    for sel in ["#pfClose", ".pf-close", "#closeBtn", ".close-btn", "#ab-allow"]:
        try:
            el = pg.query_selector(sel)
            if el and el.is_visible():
                el.click(timeout=2000)
        except Exception:
            pass

    fi = next((el for el in pg.query_selector_all("input[type=file]") if el.is_visible()), None) \
        or (pg.query_selector_all("input[type=file]") or [None])[0]
    if fi:
        fi.set_input_files(str(TMP / "in.png"))
        log("файл загружен")
    pg.wait_for_timeout(6000)

    cm = pg.query_selector("#cropModal")
    if cm and cm.is_visible():
        cb = pg.query_selector("#cropModal button:has-text('Upload')") or pg.query_selector("#cropModal .btn-primary")
        if cb:
            try:
                cb.click(timeout=8000)
            except Exception:
                cb.click(timeout=8000, force=True)
            pg.wait_for_timeout(3000)
            log("crop подтверждён")

    ta = pg.query_selector("#fn__include_textarea_img_video") or next(
        (t for t in pg.query_selector_all("textarea") if t.is_visible()), None)
    if ta:
        ta.click(); ta.fill(PROMPT)
        log("промпт введён")

    gb = pg.query_selector("#generate_it_img_video") or pg.query_selector("#generate_it")
    if gb:
        try:
            gb.scroll_into_view_if_needed(timeout=4000)
        except Exception:
            pass
        try:
            gb.click(timeout=8000)
        except Exception:
            gb.click(timeout=8000, force=True)
        log("GENERATE clicked")

    # поллинг с ДАМПОМ состояния каждые 20с (не просто ждать)
    for i in range(20):
        pg.wait_for_timeout(20000)
        elapsed = (i + 1) * 20
        videos = pg.query_selector_all("video")
        vinfo = [{"src": v.get_attribute("src"), "outerHTML_len": len(v.evaluate("el=>el.outerHTML"))} for v in videos]
        # родительский контейнер плейсхолдера — попробуем найти по тексту "%"
        percent_el = pg.query_selector("text=/\\d+%/")
        percent_text = percent_el.inner_text() if percent_el else None
        log(f"[{elapsed}s] video_tags={len(videos)} {vinfo} percent={percent_text!r} net_mp4_seen={len(net_log)}")
        if videos and any(v.get("src") for v in vinfo):
            log("✅ видео найдено с src — успех")
            pg.screenshot(path=str(TMP / "success.png"))
            html = pg.content()
            (TMP / "success_dom.html").write_text(html, encoding="utf-8")
            break
        if percent_text == "100%" and elapsed >= 60:
            # дампим DOM вокруг плейсхолдера на 100%, если через минуту после 100% видео так и нет
            html = pg.content()
            (TMP / f"dom_at_100pct_{elapsed}s.html").write_text(html, encoding="utf-8")
            pg.screenshot(path=str(TMP / f"screenshot_{elapsed}s.png"))
            log(f"[{elapsed}s] застряли на 100% — дамп сохранён")
    else:
        log("итог: видео не найдено за весь поллинг")
        pg.screenshot(path=str(TMP / "final_timeout.png"))
        (TMP / "final_dom.html").write_text(pg.content(), encoding="utf-8")

    log(f"net_log (.mp4/video urls): {net_log}")
    br.close()

# залить всё для анализа
for f in TMP.iterdir():
    yd_put(f, f"Content factory/cloud_io/veofree_probe_dom/{f.name}")
log("DONE")
