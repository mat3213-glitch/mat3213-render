#!/usr/bin/env python3
"""
[ФИКС 25.09] qwen-coder воркер — текстовый чат через chat.qwen.ai (кодер-модель).

Тот же приём, что GEN.py: Playwright открывает чат с ТЁПЛЫМ persistent-ПРОФИЛЕМ,
CDPSession ловит реальный chat_id из запроса браузера, затем requests поллит
/api/v2/chats/{chat_id} и возвращает ТЕКСТ ответа ассистента (а не media-URL).

ПОЧЕМУ профиль, а не storage_state (с 22.09):
  chat.qwen.ai детектит автоматизацию (собственный сканер «Browser detection»)
  и сбрасывает сессию в гостевой режим «Войти» — сабмит больше не создаёт чат.
  При этом токен серверно остаётся валиден (GET API 200). Лечится двумя вещами
  вместе: (1) SPHT-патч фингерпринтов через add_init_script до загрузки SPA,
  (2) полный persistent-профиль (sessionStorage/IndexedDB/SW): storage_state +
  stealth отдельно НЕ держат сабмит. Checked on 2026-09-25 (локально, 2 успешных
  прогона подряд).

Ожидание на раннере: qwen/qwen_profile/ распакован из qwen_profile.tar.gz
(залит с ЯД). Сессию (JWT/q_session) после прогона пишем в qwen_session.json
для keepalive-репортинга, а сам профиль пакетом возвращается на ЯД.

Запуск:
  python3 qwen_chat.py "напиши python-функцию факториала"
  python3 qwen_chat.py "fix this bug ..." --model "Qwen3-Coder"
"""
import sys
import json
import os
import re
import time
import argparse
import asyncio
import requests
from pathlib import Path
from playwright.async_api import async_playwright

HERE = Path(__file__).parent
SESSION_FILE = HERE / "qwen_session.json"
PROFILE_DIR = HERE / "qwen_profile"
OUTPUTS = HERE / "outputs"
BASE_URL = "https://chat.qwen.ai"
DEFAULT_MODEL = "Qwen3-Coder"
MAX_PROMPT_CHARS = 100_000

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [
  {name:'Chrome PDF Plugin',description:'Portable Document Format',filename:'internal-pdf-viewer'},
  {name:'Chrome PDF Viewer',filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai'},
  {name:'Native Client',filename:'internal-nacl-plugin'}
]});
Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU','ru','en-US','en']});
window.chrome = window.chrome || { runtime: {}, loadTimes: function(){}, csi: function(){} };
const gp = navigator.permissions && navigator.permissions.query;
if (gp) { navigator.permissions.query = (p) => (p && p.name === 'notifications')
  ? Promise.resolve({state: Notification.permission, onchange: null})
  : gp(p); }
"""

CHROMIUM_ARGS = ("--disable-blink-features=AutomationControlled", "--no-sandbox",
                 "--disable-dev-shm-usage", "--no-first-run")


def poll_for_text(cookies: dict, chat_id: str, timeout: int = 240) -> str:
    """Поллит чат-API, возвращает финальный ТЕКСТ ответа ассистента (endTime выставлен)."""
    token = cookies.get("token", "")
    s = requests.Session()
    s.cookies.update(cookies)
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/148.0.0.0 Safari/537.36",
    })
    print("  [poll] ожидание ответа", end="", file=sys.stderr, flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = s.get(f"{BASE_URL}/api/v2/chats/{chat_id}", timeout=15, stream=True)
            if r.status_code == 200:
                if int(r.headers.get("Content-Length", "0") or 0) > 2_000_000:
                    print("\n  [poll] ответ больше 2 MB", file=sys.stderr)
                    return ""
                raw = bytearray()
                for chunk in r.iter_content(64 * 1024):
                    raw.extend(chunk)
                    if len(raw) > 2_000_000:
                        print("\n  [poll] ответ больше 2 MB", file=sys.stderr)
                        return ""
                data = json.loads(raw).get("data", {})
                msgs = data.get("chat", {}).get("history", {}).get("messages", {})
                for msg in msgs.values():
                    if msg.get("role") != "assistant":
                        continue
                    err = msg.get("error")
                    if err:
                        print(f"\n  [poll] ошибка: {err}", file=sys.stderr)
                        return ""
                    if not msg.get("extra", {}).get("endTime"):
                        continue
                    parts = []
                    for item in (msg.get("content_list") or []):
                        c = item.get("content", "")
                        if c and not c.startswith("http"):
                            parts.append(c)
                    text = "\n".join(parts).strip() or (msg.get("content", "") or "").strip()
                    if text and not text.startswith("http"):
                        print(" готово", file=sys.stderr, flush=True)
                        return text
        except requests.RequestException:
            pass
        print(".", end="", file=sys.stderr, flush=True)
        time.sleep(6)
    print(" timeout", file=sys.stderr, flush=True)
    return ""


def _token_exp(state: dict) -> int:
    """Unix-время истечения JWT из localStorage сессии Qwen (0 = не нашли/не разобрали)."""
    import base64
    for o in (state or {}).get("origins", []):
        for kv in o.get("localStorage", []):
            v = kv.get("value", "")
            if kv.get("name") == "token" and v.count(".") == 2:
                try:
                    pl = v.split(".")[1]
                    pl += "=" * (-len(pl) % 4)
                    return int(json.loads(base64.urlsafe_b64decode(pl)).get("exp", 0))
                except Exception:
                    return 0
    return 0


def _exp_human(state: dict) -> str:
    from datetime import datetime
    e = _token_exp(state) or state.get("_auth_cookie_exp", 0)
    return datetime.fromtimestamp(e).strftime("%Y-%m-%d") if e else "нет токена"


def _cookie_auth_exp(cookies: dict) -> int:
    """Unix-exp JWT из httpOnly-куки `token` (главный авторизационный токен Qwen)."""
    import base64
    v = str(cookies.get("token", ""))
    if v.count(".") == 2:
        try:
            pl = v.split(".")[1]
            pl += "=" * (-len(pl) % 4)
            return int(json.loads(base64.urlsafe_b64decode(pl)).get("exp", 0))
        except Exception:
            return 0
    return 0


def _cookie_auth_exp_human(exp: int) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(exp).strftime("%Y-%m-%d") if exp else "нет токена"


async def chat(prompt: str, model: str, timeout: int) -> str:
    profile_mode = PROFILE_DIR.exists()
    if not profile_mode and not SESSION_FILE.exists():
        print("Нет сессии: нужен qwen/qwen_profile (tar профиля) или qwen_session.json",
              file=sys.stderr)
        sys.exit(2)
    state = None
    if SESSION_FILE.exists():
        try:
            state = json.loads(SESSION_FILE.read_text())
        except Exception:
            state = None
        SESSION_FILE.chmod(0o600)

    print(f"  [qwen-chat] model={model} prompt_chars={len(prompt)} профиль={profile_mode}",
          file=sys.stderr)

    async with async_playwright() as p:
        if profile_mode:
            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR), headless=True,
                viewport={"width": 1400, "height": 900},
                user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"),
                locale="ru-RU",
                args=list(CHROMIUM_ARGS))
            await ctx.add_init_script(STEALTH_JS)
        else:
            try:
                browser = await p.chromium.launch(channel="chrome", headless=True,
                                                  args=list(CHROMIUM_ARGS))
            except Exception:
                browser = await p.chromium.launch(headless=True, args=list(CHROMIUM_ARGS))
            ctx = await browser.new_context(storage_state=state,
                                            viewport={"width": 1280, "height": 720})
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        cdp = await ctx.new_cdp_session(page)
        await cdp.send("Network.enable")
        chat_ids = []

        def on_request(params):
            url = params.get("request", {}).get("url", "")
            if "completions" in url and "chat_id=" in url:
                cid = url.split("chat_id=")[-1].split("&")[0]
                if cid not in chat_ids:
                    chat_ids.append(cid)
                    print(f"  [captured] chat_id={cid}", file=sys.stderr)

        cdp.on("Network.requestWillBeSent", on_request)

        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(8000)

        body = await page.locator("body").inner_text()
        if "Войти" in body and not re.search(r"выход|выйти|logout", body, re.IGNORECASE):
            print("  [warn] SPA показывает «Войти» (гость?) — SPA не ожидает отказ сессии",
                  file=sys.stderr)

        # Изоляция от истории аккаунта: Qwen Studio открывает последнюю беседу из
        # «Все чаты», и модель отвечает в её контексте. Best-effort: клик «Новый чат»
        # и закрытие случайных оверлеев.
        async def _close_popups() -> None:
            try:
                for _ in range(2):
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(250)
            except Exception:
                pass

        await _close_popups()
        new_chat = page.get_by_role("button", name=re.compile(r"Новый чат|New chat", re.IGNORECASE))
        try:
            if await new_chat.count() and await new_chat.first.is_visible():
                await new_chat.first.click(timeout=5000)
                await page.wait_for_timeout(2000)
                print("  [fresh-chat] начал новую беседу", file=sys.stderr)
        except Exception as e:
            print(f"  [fresh-chat] не удался ({str(e)[:60]}) — работаю в текущей беседе",
                  file=sys.stderr)

        if model and model != DEFAULT_MODEL:
            try:
                model_trigger = page.locator("header").locator("text=/Qwen/i").first
                await model_trigger.click(timeout=8000)
                await page.wait_for_timeout(1000)
                await page.locator(f"text={model}").first.click(timeout=6000)
                await page.wait_for_timeout(1000)
                print(f"  [model] выбрал {model}", file=sys.stderr)
            except Exception as e:
                print(f"  [model] не переключил ({str(e)[:50]}) — текущая", file=sys.stderr)
            finally:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(400)

        def _url_chat_id(url: str) -> str:
            m = re.search(r"/c/([0-9a-zA-Z-]{16,})", url)
            return m.group(1) if m else ""

        async def _try_fresh_reset() -> None:
            try:
                nc = page.get_by_role("button", name=re.compile(r"Новый чат|New chat", re.IGNORECASE))
                if await nc.count() and await nc.first.is_visible():
                    await nc.first.click(timeout=4000)
                    await page.wait_for_timeout(2500)
            except Exception:
                pass
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass

        last_err = ""
        textarea = None
        for attempt_fill in range(3):
            try:
                ta = page.locator("textarea").first
                await ta.wait_for(state="visible", timeout=30000)
                await _close_popups()
                await ta.click(timeout=5000)
                await ta.fill(prompt, timeout=30000)
                textarea = ta
                break
            except Exception as e:
                last_err = str(e)
                print(f"  [submit] попытка {attempt_fill + 1}: {last_err[:90]}", file=sys.stderr)
                if attempt_fill < 2:
                    _try_fresh_reset()
        if textarea is None:
            print(f"  [submit] ошибка: {last_err}", file=sys.stderr)
            await page.screenshot(path=str(OUTPUTS / "debug_chat_fail.png"))
            await ctx.close()
            sys.exit(1)
        await page.wait_for_timeout(300)
        await page.keyboard.press("Enter")
        print("  [submit] Enter", file=sys.stderr)

        for attempt in range(3):
            for _ in range(14):
                if chat_ids:
                    break
                await page.wait_for_timeout(500)
            if chat_ids:
                break
            cid = _url_chat_id(page.url)
            if cid:
                chat_ids.append(cid)
                print(f"  [captured] chat_id={cid} (из URL)", file=sys.stderr)
                break
            if attempt == 2:
                break
            print(f"  [submit] повтор #{attempt + 1}: chat_id ещё нет", file=sys.stderr)
            try:
                textarea = page.locator("textarea").first
                if not str(await textarea.input_value()).strip():
                    await textarea.fill(prompt)
                await page.wait_for_timeout(300)
                try:
                    await page.locator(
                        "button[type='submit'], button[aria-label*='Отправить'], "
                        "button[aria-label*='Send']"
                    ).first.click(timeout=2500)
                    print("  [submit] клик по кнопке", file=sys.stderr)
                except Exception:
                    await textarea.focus()
                    await page.keyboard.press("Enter")
                    print("  [submit] Enter (повтор)", file=sys.stderr)
            except Exception as e:
                print(f"  [submit] повтор не удался: {str(e)[:80]}", file=sys.stderr)

        # Сессию в qwen_session.json обновляем (для keepalive-репортинга). Главный
        # токен — httpOnly-кука `token`; её exp кладём в _auth_cookie_exp.
        fresh_cookies = await ctx.cookies()
        post_cookies = {c["name"]: c["value"] for c in fresh_cookies
                        if any(d in c.get("domain", "") for d in ["qwen.ai", "alibaba", "aliyun"])}
        try:
            cookie_exp = _cookie_auth_exp(post_cookies)
            fresh = await ctx.storage_state()
            ref_exp = _token_exp(state) if state else 0
            if ref_exp <= 0:
                ref_exp = _token_exp(fresh) or cookie_exp
            if cookie_exp >= ref_exp and cookie_exp > 0:
                fresh["_auth_cookie_exp"] = cookie_exp
                tmp = SESSION_FILE.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(fresh, ensure_ascii=False))
                os.chmod(tmp, 0o600)
                os.replace(tmp, SESSION_FILE)
                print(f"  [session] обновлена, HTTP cookie JWT до"
                      f" {_cookie_auth_exp_human(cookie_exp)}", file=sys.stderr)
            else:
                print(f"  [session] НЕ обновляю: новый cookie-токен хуже старого "
                      f"({_exp_human(fresh)} vs {_exp_human(state or fresh)})", file=sys.stderr)
        except Exception as e:
            print(f"  [session] не смог сохранить: {str(e)[:100]}", file=sys.stderr)

        if not chat_ids:
            try:
                await page.screenshot(path=str(OUTPUTS / "debug_no_chatid.png"))
            except Exception:
                pass
        cookies = post_cookies
        await ctx.close()

    if not chat_ids:
        print("❌ chat_id не перехвачен (диагностика: qwen/qwen_chat outputs/debug_no_chatid.png)",
              file=sys.stderr)
        sys.exit(1)

    return poll_for_text(cookies, chat_ids[-1], timeout=timeout)


def main():
    ap = argparse.ArgumentParser(description="Qwen текст-чат (кодер) через chat.qwen.ai")
    ap.add_argument("prompt", nargs="?", help="Промпт")
    ap.add_argument("--stdin", action="store_true", help="Читать prompt из stdin")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Модель (default {DEFAULT_MODEL})")
    ap.add_argument("--timeout", type=int, default=240)
    args = ap.parse_args()

    OUTPUTS.mkdir(exist_ok=True)
    prompt = sys.stdin.read() if args.stdin else (args.prompt or "")
    if not prompt.strip():
        ap.error("пустой prompt")
    if len(prompt) > MAX_PROMPT_CHARS:
        ap.error(f"prompt длиннее {MAX_PROMPT_CHARS} символов")
    lock_path = SESSION_FILE.with_suffix(".lock")
    lock_path.touch(mode=0o600, exist_ok=True)
    try:
        import fcntl
        with lock_path.open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            text = asyncio.run(chat(prompt, args.model, args.timeout))
    except ImportError:
        text = asyncio.run(chat(prompt, args.model, args.timeout))
    if not text:
        sys.exit(1)
    print(text)   # чистый ответ в stdout — fanout захватит


if __name__ == "__main__":
    main()