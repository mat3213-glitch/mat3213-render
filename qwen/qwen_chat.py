#!/usr/bin/env python3
"""
[ПРОТОТИП] qwen-coder воркер — текстовый чат через chat.qwen.ai (кодер-модель).

Тот же приём, что GEN.py: Playwright открывает чат с тёплой сессией, CDPSession ловит
реальный chat_id из запроса браузера, затем requests поллит /api/v2/chats/{chat_id}
и возвращает ТЕКСТ ответа ассистента (а не media-URL). Меню режимов НЕ трогаем —
обычный текст-чат (кодинг). Ответ печатается в stdout (для fanout-захвата).

Запуск:
  python3 Instrument/Qwen/qwen_chat.py "напиши python-функцию факториала"
  python3 Instrument/Qwen/qwen_chat.py "fix this bug ..." --model "Qwen3-Coder"
"""
import sys
import json
import time
import argparse
import asyncio
import requests
from pathlib import Path
from playwright.async_api import async_playwright

SESSION_FILE = Path(__file__).parent / "qwen_session.json"
OUTPUTS = Path(__file__).parent / "outputs"
BASE_URL = "https://chat.qwen.ai"
DEFAULT_MODEL = "Qwen3-Coder"


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
            r = s.get(f"{BASE_URL}/api/v2/chats/{chat_id}", timeout=15)
            if r.status_code == 200:
                data = r.json().get("data", {})
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
                    # собираем текст: content_list (если есть) либо content
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
    e = _token_exp(state)
    return datetime.fromtimestamp(e).strftime("%Y-%m-%d") if e else "нет токена"


async def chat(prompt: str, model: str, timeout: int) -> str:
    if not SESSION_FILE.exists():
        print("Нет сессии. Сначала: python3 Instrument/Qwen/AUTH.py", file=sys.stderr)
        sys.exit(2)

    state = json.loads(SESSION_FILE.read_text())
    cookies = {c["name"]: c["value"] for c in state.get("cookies", [])
               if any(d in c.get("domain", "") for d in ["qwen.ai", "alibaba", "aliyun"])}

    print(f"  [qwen-chat] model={model} prompt=«{prompt[:60]}»", file=sys.stderr)

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chrome", headless=True)
        except Exception:
            browser = await p.chromium.launch(headless=True)

        ctx = await browser.new_context(storage_state=state, viewport={"width": 1280, "height": 720})
        page = await ctx.new_page()

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

        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)

        # best-effort: переключаем модель на кодер (если не выйдет — текущая).
        # ВАЖНО: при любом исходе закрываем попап-селектор (Escape), иначе он
        # перехватывает клики по textarea и блокирует сабмит.
        if model:
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
                await page.keyboard.press("Escape")   # закрыть попап при любом исходе
                await page.wait_for_timeout(400)

        # обычный текст-чат: вводим промпт в textarea и Enter (меню режимов НЕ трогаем)
        try:
            textarea = page.locator("textarea").first
            await textarea.fill(prompt)
            await page.wait_for_timeout(300)
            await page.keyboard.press("Enter")
            print("  [submit] Enter", file=sys.stderr)
        except Exception as e:
            print(f"  [submit] ошибка: {e}", file=sys.stderr)
            await page.screenshot(path=str(OUTPUTS / "debug_chat_fail.png"))
            await browser.close()
            sys.exit(1)

        for _ in range(40):
            if chat_ids:
                break
            await page.wait_for_timeout(500)

        # Сохранить обновлённую сессию: Qwen может ротировать JWT в localStorage,
        # а раннер одноразовый — без этого ротация выбрасывается и токен доживает
        # до своего исходного exp (замер 28.07: JWT жил до 26.08). Перезаписываем
        # ТОЛЬКО если новый токен не хуже старого — иначе рискуем затереть рабочую
        # сессию огрызком (не залогинилось / редирект на форму входа).
        try:
            fresh = await ctx.storage_state()
            if _token_exp(fresh) >= _token_exp(state) and _token_exp(fresh) > 0:
                SESSION_FILE.write_text(json.dumps(fresh, ensure_ascii=False))
                print(f"  [session] обновлена, JWT до {_exp_human(fresh)}", file=sys.stderr)
            else:
                print(f"  [session] НЕ обновляю: новый токен хуже старого "
                      f"({_exp_human(fresh)} vs {_exp_human(state)})", file=sys.stderr)
        except Exception as e:
            print(f"  [session] не смог сохранить: {str(e)[:100]}", file=sys.stderr)

        await browser.close()

    if not chat_ids:
        print("❌ chat_id не перехвачен", file=sys.stderr)
        sys.exit(1)

    return poll_for_text(cookies, chat_ids[-1], timeout=timeout)


def main():
    ap = argparse.ArgumentParser(description="Qwen текст-чат (кодер) через chat.qwen.ai")
    ap.add_argument("prompt", help="Промпт")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Модель (default {DEFAULT_MODEL})")
    ap.add_argument("--timeout", type=int, default=240)
    args = ap.parse_args()

    OUTPUTS.mkdir(exist_ok=True)
    text = asyncio.run(chat(args.prompt, args.model, args.timeout))
    if not text:
        sys.exit(1)
    print(text)   # чистый ответ в stdout — fanout захватит


if __name__ == "__main__":
    main()
