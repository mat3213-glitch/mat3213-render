#!/usr/bin/env python3
"""Зонд: умеет ли chat.qwen.ai судить КАРТИНКУ (кандидат на замену GH Models в art_judge).

Зачем: квота GH Models на vision-модели не тянет пул (116 артов = 232 запроса на модель,
429 с Retry-After 40+ мин). Задание yaromat 30.07: «проверь возможности кими и квена
на предмет замены судьи». Kimi отпал — его веб-API принимает загрузку, но файл уходит
в status=failed и модель картинку не видит. Здесь проверяем браузерный путь Qwen.

Отличие от боевого qwen_chat.py: перед отправкой промпта кладём файл в input[type=file]
и ждём, пока превью появится в композере. Всё остальное (перехват chat_id через CDP,
поллинг ответа, бережное обновление сессии) переиспользуем из qwen_chat.

Запуск (только на GH-раннере: Chromium на Atom не идёт):
    python3 qwen/qwen_vision_probe.py art.jpg --prompt "..."
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from qwen_chat import (  # noqa: E402
    BASE_URL, SESSION_FILE, OUTPUTS, poll_for_text, _token_exp, _exp_human,
)
from playwright.async_api import async_playwright  # noqa: E402


async def ask_image(image: Path, prompt: str, timeout: int) -> str:
    if not SESSION_FILE.exists():
        sys.exit("нет сессии qwen_session.json")
    state = json.loads(SESSION_FILE.read_text())
    cookies = {c["name"]: c["value"] for c in state.get("cookies", [])
               if any(d in c.get("domain", "") for d in ["qwen.ai", "alibaba", "aliyun"])}

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chrome", headless=True)
        except Exception:
            browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(storage_state=state, viewport={"width": 1280, "height": 900})
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
        await page.keyboard.press("Escape")   # на всякий случай снять попапы

        # 1) кладём картинку. input[type=file] у Qwen скрыт за кнопкой-скрепкой,
        #    поэтому не кликаем по UI, а пишем прямо в input — Playwright это умеет.
        inputs = page.locator("input[type=file]")
        n = await inputs.count()
        print(f"  [upload] найдено input[type=file]: {n}", file=sys.stderr)
        if n == 0:
            await page.screenshot(path=str(OUTPUTS / "qwen_vision_no_input.png"))
            await browser.close()
            sys.exit("input[type=file] не найден — UI изменился, смотри скриншот")
        await inputs.first.set_input_files(str(image))

        # 2) ждём, пока файл реально прикрепится: имя файла появляется в композере
        attached = False
        for _ in range(60):
            await page.wait_for_timeout(1000)
            body = await page.locator("body").inner_text()
            if image.name in body or image.stem in body:
                attached = True
                break
        print(f"  [upload] прикрепился: {attached}", file=sys.stderr)
        if not attached:
            await page.screenshot(path=str(OUTPUTS / "qwen_vision_not_attached.png"))

        # 3) промпт и отправка
        textarea = page.locator("textarea").first
        await textarea.fill(prompt)
        await page.wait_for_timeout(500)
        await page.keyboard.press("Enter")
        print("  [submit] Enter", file=sys.stderr)

        for _ in range(60):
            if chat_ids:
                break
            await page.wait_for_timeout(500)

        try:
            fresh = await ctx.storage_state()
            if _token_exp(fresh) >= _token_exp(state) and _token_exp(fresh) > 0:
                SESSION_FILE.write_text(json.dumps(fresh, ensure_ascii=False))
                print(f"  [session] обновлена, JWT до {_exp_human(fresh)}", file=sys.stderr)
        except Exception as e:
            print(f"  [session] не сохранил: {str(e)[:80]}", file=sys.stderr)

        await page.screenshot(path=str(OUTPUTS / "qwen_vision_final.png"), full_page=True)
        await browser.close()

    if not chat_ids:
        sys.exit("chat_id не перехвачен")
    return poll_for_text(cookies, chat_ids[-1], timeout=timeout)


def main():
    ap = argparse.ArgumentParser(description="Зонд vision у chat.qwen.ai")
    ap.add_argument("image", type=Path)
    ap.add_argument("--prompt", default="Опиши, что изображено на картинке. Есть ли на ней люди, "
                                        "лица, надписи? Ответь кратко, по-русски.")
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()
    OUTPUTS.mkdir(exist_ok=True)
    text = asyncio.run(ask_image(args.image, args.prompt, args.timeout))
    print(text or "(пусто)")


if __name__ == "__main__":
    main()
