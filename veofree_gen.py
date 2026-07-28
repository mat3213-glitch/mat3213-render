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

# Хосты и признаки РЕКЛАМНОГО видео. Расширив детектор до «любого .mp4», я тут же поймал
# 15-секундный ролик Peacock TV с ad-сервера 2mdn.net и залил его как готовую генерацию
# (2026-07-28). Слепоту поменял на доверчивость — нужен фильтр, а не отсутствие фильтра.
AD_MARKERS = ("2mdn.net", "doubleclick", "googlesyndication", "googleadservices",
              "adservice", "web_video_ads", "/ads/", "videoplayback")


def is_ad(u):
    u = (u or "").lower()
    return any(m in u for m in AD_MARKERS)


def find_video(pg):
    """Ссылка на готовый ролик. Сайт мог сменить вёрстку — щупаем ВСЕ разумные места,
    а не один селектор <video src> (из-за него 100%-готовые генерации читались как таймаут)."""
    try:
        return pg.evaluate("""() => {
            const AD = ["2mdn.net","doubleclick","googlesyndication","googleadservices",
                        "adservice","web_video_ads","/ads/","videoplayback"];
            const ok = u => {
                if (!u || !/^https?:/.test(u)) return false;
                const l = u.toLowerCase();
                if (!l.includes('.mp4')) return false;
                return !AD.some(m => l.includes(m));      // реклама — не наш результат
            };
            const inAd = e => !!e.closest('[id*="google_ads"],[class*="adsbygoogle"],[id^="lx_"],ins,iframe');
            for (const v of document.querySelectorAll('video')) {
                if (inAd(v)) continue;                     // <video> внутри рекламного блока
                if (ok(v.src)) return v.src;
                if (ok(v.currentSrc)) return v.currentSrc;
                for (const s of v.querySelectorAll('source')) if (ok(s.src)) return s.src;
            }
            for (const a of document.querySelectorAll('a[href],[download]')) {
                const u = a.href || a.getAttribute('download'); if (ok(u)) return u;
            }
            return null;
        }""")
    except Exception:
        return None


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

def kill_overlays(pg):
    # clickio-сплэш (__lxG__splash) + google-ads iframe ПЕРЕИНЖЕКТЯТСЯ и перехватывают pointer
    # events → клик "Generate" не проходит (лог фейла 2026-07-11, и после однократного сноса тоже).
    # Снос + ПОСТОЯННЫЙ стиль (глушит будущий переинжект). Настоящий клик — через JS (см. ниже).
    try:
        pg.evaluate("""() => {
            const sels = ['.__lxG__splash','div[id^="lx_"]','[id^="google_ads_iframe"]',
                          'iframe[title*="Advertisement"]','iframe[aria-label*="Advertisement"]',
                          '.adsbygoogle','[class*="__lxG__"]','[id^="clickio"]'];
            // ЗАЩИТА (2026-07-28): '[class*="__lxG__"]' — подстрочный матч по class. Скрипты
            // согласия вешают свои классы на <body>/<html>, и тогда e.remove() сносил ВЕСЬ
            // документ. Симптом: статус timeout при найденных ta/btn + белый пустой fail.png
            // (13 провалов VeoFree с 11.07 — все скриншоты байт-в-байт одинаковые).
            // Никогда не удаляем корень и контейнер, внутри которого лежит кнопка генерации.
            const keep = document.querySelector('#generate_it') || document.querySelector('textarea');
            const safeRemove = e => {
                if (!e || e === document.body || e === document.documentElement) return;
                if (keep && e.contains(keep)) return;
                e.remove();
            };
            sels.forEach(s => document.querySelectorAll(s).forEach(safeRemove));
            if (!document.getElementById('__killads')) {
                const st = document.createElement('style'); st.id = '__killads';
                // :not(body):not(html) — БЕЗ этого правило прячет ВЕСЬ САЙТ. Скрипт согласия
                // вешает класс с '__lxG__' на body, подстрочный селектор его цепляет, и
                // display:none гасит страницу целиком. DOM при этом ЖИВОЙ (querySelector
                // находит и body, и кнопку — скрытые элементы никуда не делись), поэтому
                // диагностика выглядела здоровой, а скриншот был белым. Ровно это давало
                // 13 «таймаутов» с 11.07: клики уходили в невидимую страницу.
                st.textContent = '[class*="__lxG__"]:not(body):not(html),div[id^="lx_"],[id^="google_ads_iframe"],iframe[title*="Advertisement"],iframe[aria-label*="Advertisement"],.adsbygoogle{display:none!important;pointer-events:none!important;visibility:hidden!important}';
                document.head.appendChild(st);
            }
        }""")
        pg.wait_for_timeout(200)
    except Exception:
        pass

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
    # ЛОВИМ ЛЮБОЙ .mp4, не только /video/uploads/. Скриншот 2026-07-28 показал «Generating
    # Video... 100%» при status=timeout: генерация ДОХОДИЛА ДО КОНЦА, а детектор искал
    # устаревший путь и ссылку не узнавал. Узкий фильтр = слепота.
    pg.on("response", lambda r: seen.add(r.url)
          if (".mp4" in r.url.lower() and not is_ad(r.url)) else None)
    pg.goto(URL,wait_until="domcontentloaded",timeout=60000); pg.wait_for_timeout(5000)
    dismiss(pg)
    ta=pg.query_selector("textarea#fn__include_textarea") or pg.query_selector("textarea")
    btn=pg.query_selector("#generate_it")
    try:
      if ta and btn:
          # ПОРЯДОК ВАЖЕН (фикс 2026-07-28): раньше сначала кликали в textarea, а сносили рекламу
          # ПОСЛЕ — и до сноса дело не доходило. Рекламный баннер-iframe 970x250 перехватывал
          # pointer events, ta.click() падал по таймауту 30с, скрипт умирал НЕОБРАБОТАННЫМ
          # исключением (ни скриншота, ни диагностики). Чистим страницу ДО первого клика.
          dismiss(pg); kill_overlays(pg)
          # Заполняем через JS: клик по элементу принципиально уязвим к перехвату оверлеем,
          # а установка value + input/change события до реакта долетает и без клика.
          try:
              pg.evaluate("""(txt) => {
                  const t = document.querySelector('textarea#fn__include_textarea')
                         || document.querySelector('textarea');
                  if (!t) return false;
                  const setter = Object.getOwnPropertyDescriptor(
                      window.HTMLTextAreaElement.prototype, 'value').set;
                  setter.call(t, txt);
                  t.dispatchEvent(new Event('input',  {bubbles: true}));
                  t.dispatchEvent(new Event('change', {bubbles: true}));
                  return true;
              }""", PROMPT)
          except Exception as e:
              log(f"  [fill] JS не прошёл ({e}) — пробую обычный fill")
              try: ta.fill(PROMPT)
              except Exception as e2: log(f"  [fill] и обычный не прошёл: {e2}")
          kill_overlays(pg)
          try: btn.scroll_into_view_if_needed(timeout=4000)
          except: pass
          # КЛИК ЧЕРЕЗ JS в ЦИКЛЕ — element.click() игнорирует перехват pointer-events переинжекчёным
          # сплэшем; цикл лечит флак по времени рекламы (сплэш выпадает не сразу). Выходим, как только
          # генерация стартовала (появилось video/сеть-mp4). Лог фейлов 2026-07-11.
          # КЛИК: JS-клик (element.click()) порождает НЕДОВЕРЕННОЕ событие (isTrusted=false).
          # Диагностика 2026-07-28 показала, что сайт его игнорирует: промпт залит (taLen=785),
          # страница видна, кнопка 152x40 и enabled — но после 5 JS-кликов её состояние НЕ
          # изменилось ('GENERATE', disabled=False) и видео не появилось за 420с.
          # Поэтому сначала пробуем НАСТОЯЩИЕ клики (Playwright шлёт их через CDP, isTrusted=true),
          # и только последним средством — JS. Оверлеи к этому моменту уже снесены.
          for attempt in range(5):
              kill_overlays(pg)
              how="-"
              try:
                  btn.click(timeout=5000); how="playwright"
              except Exception:
                  try:
                      box=pg.evaluate("""() => { const b=document.querySelector('#generate_it');
                          if(!b) return null; const r=b.getBoundingClientRect();
                          return {x:r.left+r.width/2, y:r.top+r.height/2}; }""")
                      if box:
                          pg.mouse.click(box["x"], box["y"]); how="mouse"
                  except Exception:
                      try:
                          pg.evaluate("() => { const b=document.querySelector('#generate_it'); if(b) b.click(); }")
                          how="js"
                      except Exception: pass
              st=pg.evaluate("""() => { const b=document.querySelector('#generate_it');
                  return b ? {t:b.innerText.trim().slice(0,24), d:!!b.disabled} : null; }""")
              log(f"  [click {attempt+1}/5] способ={how} кнопка={st}")
              pg.wait_for_timeout(2500)
              if seen or find_video(pg): break
          # ГИПОТЕЗА (2026-07-28): сервис ad-gated. Мы вешаем ПОСТОЯННЫЙ стиль, который прячет
          # рекламу навсегда — и если выдача ролика завязана на открут рекламы, она никогда
          # не наступает. Симптом ровно такой: «Generating Video... 100%» висит бесконечно,
          # плейсхолдер серый, видео не появляется. Клик уже сделан, перехватывать нечего —
          # снимаем глушилку и даём рекламе крутиться, пока ждём результат.
          try:
              pg.evaluate("() => { const s=document.getElementById('__killads'); if(s) s.remove(); }")
              log("  [ads] глушилка снята на время ожидания результата")
          except Exception: pass
          try:
              post=pg.evaluate("""() => {
                  const b=document.querySelector('#generate_it');
                  const t=document.querySelector('textarea');
                  return {btnText:(b?b.innerText.trim().slice(0,40):'-'),
                          btnDisabled:(b?!!b.disabled:null),
                          taLen:(t?t.value.length:0),
                          spinner: !!document.querySelector('[class*="load"],[class*="spin"],[class*="progress"]')};
              }""")
              log(f"  [after-click] {post}")
          except Exception as e: log(f"  [after-click] н/д: {e}")
          for _ in range(84):                            # до 420с: 200с могло не хватать на очередь Seedance
              pg.wait_for_timeout(5000)
              if paywall(pg): status="paywall"; break
              src=find_video(pg)
              if src or seen:
                  video_url=src or sorted(seen)[-1]
                  status="ok"; break
          else: status="timeout"
      else:
        status="no_ui"
    except Exception as e:
        # Любой сбой Playwright (перехваченный клик, таймаут) НЕ должен уносить диагностику:
        # раньше исключение здесь убивало скрипт до скриншота, и причина терялась.
        status=f"exception: {type(e).__name__}"
        log(f"  [exc] {e}")
    if status!="ok":
        # ДИАГНОСТИКА: пустой белый скриншот сам по себе ничего не объясняет. Печатаем,
        # жив ли вообще документ — если body исчез, виноваты МЫ (см. safeRemove выше),
        # а не сайт. Раньше это стоило 13 провалов и 17 дней тишины.
        try:
            # ВАЖНО: наличия элементов в DOM НЕДОСТАТОЧНО — querySelector находит и скрытые.
            # Меряем ВИДИМОСТЬ: display у body и реальный размер кнопки. Именно этой строки
            # не хватало, чтобы отличить «сайт не отдал видео» от «мы погасили себе страницу».
            st=pg.evaluate("""() => {
                const b=document.body, g=document.querySelector('#generate_it');
                const r=g?g.getBoundingClientRect():null;
                return {
                  body: !!b, len: (b?b.innerHTML.length:0),
                  bodyDisplay: b?getComputedStyle(b).display:'-',
                  bodyVisible: b?(b.getBoundingClientRect().height>0):false,
                  btn: !!g, btnBox: r?`${Math.round(r.width)}x${Math.round(r.height)}`:'-',
                  ta: !!document.querySelector('textarea'),
                  video: !!document.querySelector('video'), url: location.href };
            }""")
            log(f"  [dom] {st}")
            # ПРЕДЫДУЩИЙ дамп ловил <style> (в нём тоже встречается «%») — бесполезно.
            # Теперь собираем ФАКТЫ: все src у video (включая blob:, который наш детектор
            # отвергает из-за требования ^https?:), все source/href и HTML контейнера,
            # в котором реально написано «Generating Video».
            res=pg.evaluate("""() => {
                const vids=[...document.querySelectorAll('video')].map(v => ({
                    src:(v.src||'').slice(0,120), cur:(v.currentSrc||'').slice(0,120),
                    inAd:!!v.closest('[id*="google_ads"],[class*="adsbygoogle"],ins,iframe'),
                    w:v.videoWidth, h:v.videoHeight, ready:v.readyState}));
                const srcs=[...document.querySelectorAll('source')].map(s=>(s.src||'').slice(0,120));
                const links=[...document.querySelectorAll('a[href]')]
                    .map(a=>a.href).filter(u=>/\.mp4|download|blob:/i.test(u)).slice(0,6);
                const node=[...document.querySelectorAll('div,section')].find(e =>
                    /Generating Video/i.test(e.textContent||'') && e.children.length<12);
                return {vids, srcs, links,
                        holder: node ? node.outerHTML.replace(/\s+/g,' ').slice(0,900) : 'нет'};
            }""")
            log(f"  [videos] {res.get('vids')}")
            log(f"  [sources] {res.get('srcs')}  [links] {res.get('links')}")
            log(f"  [holder] {res.get('holder')}")
        except Exception as e: log(f"  [dom] недоступен: {e}")
        try:
            # проматываем К ГЕНЕРАТОРУ: вьюпорт-скриншот со случайной позиции показывал
            # маркетинговый текст, а не форму, из-за которой пришли
            pg.evaluate("() => { const b=document.querySelector('#generate_it'); if(b) b.scrollIntoView({block:'center'}); }")
            pg.wait_for_timeout(500)
        except Exception: pass
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
