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

        # Заливка картинки идёт асинхронно и в разметке имя файла появляется СРАЗУ,
        # ещё до отправки байтов (на этом первый заход и погорел: Enter ушёл через
        # секунду, и Qwen ответил «вы не прикрепили картинку»). Поэтому ждём не UI,
        # а фактический ответ хранилища.
        uploads = []

        def on_response(params):
            url = params.get("response", {}).get("url", "")
            status = params.get("response", {}).get("status", 0)
            # cdn.qwenlm.ai/output/... — это демо-ролики главной страницы, не наша заливка:
            # на них первый заход и попался, приняв их за подтверждение загрузки.
            if "cdn.qwenlm.ai/output" in url:
                return
            if status < 400 and any(k in url for k in ("aliyuncs", "oss-", "upload", "/sts")):
                uploads.append(url)
                print(f"  [upload-net] {status} {url[:90]}", file=sys.stderr)

        cdp.on("Network.responseReceived", on_response)

        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)
        await page.keyboard.press("Escape")   # на всякий случай снять попапы

        # 1) кладём картинку. Запись напрямую в скрытый input[type=file] НЕ работает:
        #    React-композер Qwen её не подхватывает (проверено — модель отвечает
        #    «вы не прикрепили изображение»). Идём человеческим путём: файловый диалог
        #    по кнопке-скрепке, а если кнопку не нашли — эмулируем drop с DataTransfer.
        put_ok = False
        for sel in ("button[aria-label*='upload' i]", "button[aria-label*='attach' i]",
                    "button[title*='上传' i]", "button:has(svg[class*='paperclip' i])",
                    "input[type=file]"):
            try:
                if sel == "input[type=file]":
                    raise RuntimeError("оставляем на фолбэк")
                async with page.expect_file_chooser(timeout=8000) as fc:
                    await page.locator(sel).first.click(timeout=6000)
                chooser = await fc.value
                await chooser.set_files(str(image))
                print(f"  [upload] файловый диалог через {sel}", file=sys.stderr)
                put_ok = True
                break
            except Exception:
                continue

        if not put_ok:
            data_url = "data:image/jpeg;base64," + __import__("base64").b64encode(
                image.read_bytes()).decode()
            dropped = await page.evaluate(
                """async ({dataUrl, name}) => {
                    const res = await fetch(dataUrl);
                    const blob = await res.blob();
                    const file = new File([blob], name, {type: 'image/jpeg'});
                    const dt = new DataTransfer();
                    dt.items.add(file);
                    const target = document.querySelector('textarea')?.closest('div') || document.body;
                    for (const type of ['dragenter', 'dragover', 'drop']) {
                        target.dispatchEvent(new DragEvent(type,
                            {dataTransfer: dt, bubbles: true, cancelable: true}));
                    }
                    const inp = document.querySelector('input[type=file]');
                    if (inp) {
                        inp.files = dt.files;
                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                    }
                    return true;
                }""",
                {"dataUrl": data_url, "name": image.name})
            print(f"  [upload] фолбэк drop+change: {dropped}", file=sys.stderr)

        # 2) ждём ФАКТИЧЕСКУЮ заливку (сетевой ответ хранилища), а не появление имени в UI
        for _ in range(90):
            await page.wait_for_timeout(1000)
            if uploads:
                break
        print(f"  [upload] сетевых ответов хранилища: {len(uploads)}", file=sys.stderr)
        await page.wait_for_timeout(8000)   # дать композеру дорисовать превью после заливки
        body = await page.locator("body").inner_text()
        print(f"  [upload] имя файла в разметке: {image.name in body or image.stem in body}",
              file=sys.stderr)
        await page.screenshot(path=str(OUTPUTS / "qwen_vision_before_submit.png"), full_page=True)
        if not uploads:
            print("  [upload] ⚠️ заливка не подтверждена сетью — отправляю как есть", file=sys.stderr)

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
