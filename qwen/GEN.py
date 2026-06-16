#!/usr/bin/env python3
"""
[РАБОЧИЙ] Шаг 2 из 2.

Генерация видео и изображений через chat.qwen.ai.
Playwright управляет UI, CDPSession захватывает реальный запрос браузера (chat_id + payload).
Затем requests поллит результат.

Запуск:
  python3 qwen/qwen_gen.py video "dark atmospheric city at night"
  python3 qwen/qwen_gen.py image "foggy forest cinematic"
  python3 qwen/qwen_gen.py video "rain falling" --ratio 9:16
  python3 qwen/qwen_gen.py image "portrait" --out ~/Desktop/result.png
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
DEFAULT_MODEL = "Qwen3.7-Max"


def download(url: str, out_path: Path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    r = requests.get(url, stream=True, timeout=120)
    r.raise_for_status()
    with open(out_path, "wb") as f:
        for chunk in r.iter_content(65536):
            f.write(chunk)
    size_kb = out_path.stat().st_size // 1024
    print(f"Сохранено → {out_path} ({size_kb} KB)")


def poll_for_url(cookies: dict, chat_id: str, timeout: int = 600) -> str:
    token = cookies.get("token", "")
    s = requests.Session()
    s.cookies.update(cookies)
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/148.0.0.0 Safari/537.36",
    })
    print(f"Ожидание результата", end="", flush=True)
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
                        print(f"\nОшибка генерации: {err}")
                        sys.exit(1)
                    if not msg.get("extra", {}).get("endTime"):
                        continue
                    for item in (msg.get("content_list") or []):
                        url = item.get("content", "")
                        if url and url.startswith("http"):
                            print(" готово")
                            return url
                    content = msg.get("content", "")
                    if content and content.startswith("http"):
                        print(" готово")
                        return content
        except requests.RequestException:
            pass
        print(".", end="", flush=True)
        time.sleep(12)
    print(" timeout")
    sys.exit(1)


async def generate(mode: str, prompt: str, ratio: str, out_path: Path, model: str = DEFAULT_MODEL):
    if not SESSION_FILE.exists():
        print("Нет сессии. Сначала: python3 qwen/qwen_auth.py")
        sys.exit(1)

    state = json.loads(SESSION_FILE.read_text())
    cookies = {c["name"]: c["value"] for c in state.get("cookies", [])
               if any(d in c.get("domain", "") for d in ["qwen.ai", "alibaba", "aliyun"])}

    chat_mode_label = "Создать видео" if mode == "video" else "Создать изображение"
    chat_type_expected = "t2v" if mode == "video" else "t2i"

    print(f"Запускаю {mode} генерацию [{model}]: «{prompt[:60]}»")

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chrome", headless=True)
        except Exception:
            browser = await p.chromium.launch(headless=True)

        ctx = await browser.new_context(storage_state=state, viewport={"width": 1280, "height": 720})
        page = await ctx.new_page()

        # CDPSession для захвата реального chat_id из запроса браузера
        cdp = await ctx.new_cdp_session(page)
        await cdp.send("Network.enable")
        chat_ids = []
        captured_chat_types = {}

        def on_request(params):
            url = params.get("request", {}).get("url", "")
            body = params.get("request", {}).get("postData", "")
            if "completions" in url and "chat_id=" in url:
                cid = url.split("chat_id=")[-1].split("&")[0]
                if cid not in chat_ids:
                    try:
                        bd = json.loads(body)
                        msgs = bd.get("messages", [])
                        ct = msgs[0].get("chat_type", "unknown") if msgs else bd.get("chat_type", "unknown")
                        mdl = bd.get("model", "unknown")
                    except Exception:
                        ct = "unknown"
                        mdl = "unknown"
                    chat_ids.append(cid)
                    captured_chat_types[cid] = ct
                    print(f"  [captured] chat_id={cid} chat_type={ct} model={mdl}")

        cdp.on("Network.requestWillBeSent", on_request)

        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)

        # Шаг 0: переключаем модель (кликаем на текущую модель в хедере → выбираем нужную)
        try:
            model_trigger = page.locator("header").locator("text=/Qwen/i").first
            await model_trigger.click()
            await page.wait_for_timeout(1000)
            model_option = page.locator(f"text={model}").first
            await model_option.click()
            await page.wait_for_timeout(1500)
            print(f"  [model] выбрал {model}")
        except Exception as e:
            print(f"  [model] не удалось переключить: {e} — продолжаю с текущей")

        # Шаг 1: кликаем "+" в инпут-баре чтобы открыть меню режимов
        await page.mouse.click(398, 328)
        await page.wait_for_timeout(1500)

        # Шаг 2: кликаем нужный пункт меню ("Создать видео" / "Создать изображение")
        try:
            item = page.locator("li").filter(has_text=chat_mode_label)
            await item.wait_for(timeout=5000)
            # JS-клик обходит проблему перехвата события Playwright'ом
            await item.dispatch_event("click")
            print(f"  [mode] кликнул '{chat_mode_label}' из меню")
        except Exception as e:
            print(f"  [mode] ошибка: {e}")
            await page.screenshot(path=str(OUTPUTS / "debug_menu_fail.png"))
            await browser.close()
            sys.exit(1)
        await page.wait_for_timeout(1500)

        # Вводим промпт в textarea (после переключения режима placeholder меняется)
        textarea = page.locator("textarea").first
        await textarea.click()
        await textarea.fill(prompt)
        await page.wait_for_timeout(300)

        # Enter для отправки
        await page.keyboard.press("Enter")
        print("  [submit] Enter pressed")

        # Ждём захвата chat_id
        for _ in range(30):
            if chat_ids:
                break
            await page.wait_for_timeout(500)

        await page.screenshot(path=str(OUTPUTS / "debug_after_submit.png"))
        await browser.close()

    if not chat_ids:
        print("❌ chat_id не перехвачен. Проверь debug_after_submit.png")
        sys.exit(1)

    chat_id = chat_ids[-1]
    ct = captured_chat_types.get(chat_id, "unknown")
    print(f"  [chat_id={chat_id}] [chat_type={ct}]")

    if ct not in (chat_type_expected, "unknown"):
        print(f"⚠️  Ожидали {chat_type_expected}, получили {ct}. Продолжаем...")

    media_url = poll_for_url(cookies, chat_id, timeout=600)
    print(f"URL: {media_url[:80]}...")
    download(media_url, out_path)


def main():
    parser = argparse.ArgumentParser(description="Генерация медиа через chat.qwen.ai")
    parser.add_argument("mode", choices=["image", "video"], help="Тип генерации")
    parser.add_argument("prompt", help="Текстовый промпт")
    parser.add_argument("--ratio", default="16:9", choices=["16:9", "9:16", "1:1"],
                        help="Соотношение сторон (по умолчанию 16:9)")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"Модель Qwen (по умолчанию {DEFAULT_MODEL})")
    parser.add_argument("--out", default="", help="Путь для сохранения файла")
    args = parser.parse_args()

    ts = int(time.time())
    ext = "mp4" if args.mode == "video" else "png"
    out_path = Path(args.out) if args.out else OUTPUTS / f"qwen_{args.mode}_{ts}.{ext}"

    asyncio.run(generate(args.mode, args.prompt, args.ratio, out_path, args.model))


if __name__ == "__main__":
    main()
