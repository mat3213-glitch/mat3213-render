#!/usr/bin/env python3
"""
[ВОРКЕР] glm — текст-чат GLM-5.2 через chat.z.ai (как qwen-coder).

chat.z.ai = OpenWebUI-форк. Прямой requests к /api/v2/chat/completions упирается в
captcha-сигнатуру (frontend сам её считает) → гоним через БРАУЗЕР с тёплой сессией:
Playwright вводит промпт (фронт подписывает запрос), CDPSession ловит chat_id из
ответа /api/v1/chats/new, затем requests поллит /api/v1/chats/{chat_id} и возвращает
ТЕКСТ ответа ассистента. Ответ → stdout (для fanout-захвата).

Сессия: glm_session.json (из glm_auth.py). Тёплую держать daily-пингом.

Запуск:
  python3 Instrument/GLM/glm_chat.py "напиши python-функцию факториала"
  python3 Instrument/GLM/glm_chat.py "..." --model GLM-5.2 --timeout 240
"""
import re
import os
import sys
import json
import argparse
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime_safety import chromium_launch_kwargs  # noqa: E402

HERE = Path(__file__).parent
SESSION_FILE = HERE / "glm_session.json"
OUTPUTS = HERE / "outputs"
BASE_URL = "https://chat.z.ai"
# GLM-5.2 на free-тарифе в пиковые часы отдаёт "Model at capacity / peak hours,
# ⚠️ ПЕРЕСМОТРЕНО 2026-07-29. Прежний дефолт `GLM-5-Turbo` оказался ловушкой: именно
# Turbo стабильно отвечает «No response, Please try again later», и это выглядело как
# «z.ai free лежит». На деле GLM-5.2 в том же аккаунте отвечает нормально (проверено
# и владельцем в браузере, и мной вручную через Playwright — вернул «Да»).
# Пустой дефолт = НЕ трогать селектор модели: в аккаунте уже выбрана 5.2, а клик по
# селектору хрупкий (Locator.click ловил таймаут 6с и оставлял открытым попап).
DEFAULT_MODEL = ""
# текст ошибок ёмкости/пика — чтобы не принять за ответ
CAPACITY_MARKERS = ("at capacity", "peak hours", "try again later", "switch to",
                    "currently at capacity", "MODEL_CONCURRENCY")
CAPTCHA_SELECTORS = (
    "#aliyunCaptcha-sliding-body",
    "#aliyunCaptcha-puzzle",
    "text=Please complete security verification",
)
_CJK = re.compile(r"[　-〿㐀-䶿一-鿿豈-﫿＀-￯]")


def _strip_cjk(t: str) -> str:
    return _CJK.sub("", t).strip()


async def chat(prompt: str, model: str, timeout: int) -> str:
    if not SESSION_FILE.exists():
        print("Нет сессии. Сначала: python3 Instrument/GLM/glm_auth.py", file=sys.stderr)
        sys.exit(2)

    state = json.loads(SESSION_FILE.read_text())
    print(f"  [glm-chat] model={model} prompt=«{prompt[:60]}»", file=sys.stderr)

    headless = os.environ.get("GLM_HEADLESS", "1").strip().lower() not in {"0", "false", "no"}
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(**chromium_launch_kwargs(channel="chrome", headless=headless))
        except Exception:
            browser = await p.chromium.launch(**chromium_launch_kwargs(headless=headless))
        ctx = await browser.new_context(storage_state=state,
                                        viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()

        # 180с, а не 60: на Atom под nice+cgroup-лимитом SPA z.ai не успевает за минуту
        # (сеть при этом здорова — curl отдаёт 200 за 2.5с, проверено 2026-07-29).
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=180000)
        await page.wait_for_timeout(3500)
        await _dismiss_modal(page)

        # Промо/CAPTCHA могут быть поверх SPA. Не пытаться отправлять текст под challenge:
        # это только превращает понятную причину в 240-секундный таймаут.
        if await _captcha_visible(page):
            print("  [captcha] обнаружена security verification; нужна headed-проверка",
                  file=sys.stderr)
            await page.screenshot(path=str(OUTPUTS / "glm_captcha.png"))
            await browser.close()
            return ""

        # Переключить модель. Используем стабильный публичный контракт DOM, а не
        # `[class*='model']`: тот матчится на посторонние элементы и первый locator
        # мог быть невидимым/не кликабельным. После выбора ждём, пока UI подтвердит
        # новое значение, иначе сообщение уйдёт в старую модель.
        if model:
            try:
                trigger = page.locator(
                    "button.modelSelectorButton[aria-label='Select a model']"
                ).first
                await trigger.wait_for(state="visible", timeout=15000)
                await trigger.click(timeout=8000)
                await _wait_model_menu(page, trigger, expanded=True)

                option = None
                menu = page.locator("[role='menu']:visible").last
                deadline = asyncio.get_event_loop().time() + 15000 / 1000
                while asyncio.get_event_loop().time() < deadline:
                    # Ограничиваем поиск открытым menu: глобальный get_by_text также
                    # видит сам trigger с текущей моделью и может кликнуть его вместо
                    # пункта списка.
                    candidates = (await menu.get_by_text(model, exact=True).all()
                                  if await menu.count() else [])
                    for candidate in candidates:
                        if await candidate.is_visible():
                            option = candidate
                            break
                    if option is not None:
                        break
                    await page.wait_for_timeout(250)
                if option is None:
                    raise RuntimeError(f"пункт модели {model!r} не появился в меню")
                await option.click(timeout=6000)
                await _wait_model_menu(page, trigger, expanded=False)
                await _wait_model_value(page, trigger, model)
                print(f"  [model] выбрал {model}", file=sys.stderr)
            except Exception as e:
                print(f"  [model] НЕ подтверждён ({str(e)[:100]})", file=sys.stderr)
                await browser.close()
                return ""
            finally:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(400)

        try:
            # Поле ждём до 45с: на Atom SPA z.ai рисуется дольше фиксированной паузы
            # (у MiniMax через 9с DOM был вообще пуст — тот же класс проблемы, 29.07).
            textarea = None
            for _ in range(15):
                await _dismiss_modal(page, allow_escape=True)
                loc = page.locator("textarea").first
                if await loc.count() and await loc.is_visible():
                    textarea = loc
                    break
                await page.wait_for_timeout(3000)
            if textarea is None:
                raise RuntimeError("поле ввода не появилось за 45с")
            # Оверлей может съесть и клик, и фокус (грабля Kimi): бьём насквозь и
            # фокусируем элемент напрямую, затем проверяем, что текст реально в поле.
            try:
                await textarea.click(timeout=5000)
            except Exception:
                await textarea.click(force=True)
            try:
                await textarea.evaluate("el => el.focus()")
            except Exception:
                pass
            # ⚠️ НЕ `fill()`. Он ставит значение напрямую, и фронт z.ai (Vue) не видит
            # события ввода → уходит ПУСТОЕ сообщение, а сервис отвечает
            # «No response, Please try again later». Ровно это три прогона подряд
            # выглядело как «GLM лежит» (29.07). Печатаем посимвольно — так ответ приходит.
            await page.keyboard.type(prompt)
            await page.wait_for_timeout(300)
            typed = (await textarea.input_value() or "").strip()
            if prompt[:12] not in typed:
                raise RuntimeError(f"текст не попал в поле (в поле: «{typed[:40]}»)")
            await page.keyboard.press("Enter")
            print("  [submit] Enter", file=sys.stderr)
        except Exception as e:
            print(f"  [submit] ошибка: {e}", file=sys.stderr)
            await page.screenshot(path=str(OUTPUTS / "glm_chat_fail.png"))
            await browser.close()
            sys.exit(1)

        text = await _read_answer(page, timeout)
        await page.screenshot(path=str(OUTPUTS / "glm_chat_last.png"))
        await browser.close()

    return text


async def _dismiss_modal(page, allow_escape: bool = True):
    """Закрыть попап 'peak hours / capacity' если всплыл (иначе блокирует ввод).

    `allow_escape=False` — для цикла чтения ответа. Слепой Escape каждые 3 секунды
    ОТМЕНЯЕТ идущую генерацию: страница отдаёт «No response, Please try again later»,
    и это неотличимо от отказа сервиса (29.07 сожгло несколько прогонов и едва не
    закрыло ветку GLM выводом «free-тариф мёртв»). Escape нужен ровно один раз — перед вводом.
    """
    # Промо-модалка «GLM-5.3-Flash → Model Upgraded» (с кнопкой «Explore the GLM-5.3
    # Series») появляется каждый заход и съедает клики: Enter улетал в неё, и чат
    # оставался пустым (найдено 30.08 через grok-vision по скрину). Закрываем её.
    for sel in ("button[aria-label*='Close' i]", "button[aria-label*='Dismiss' i]",
                "button[aria-label*='×' i]", "button:has-text('Close')",
                "button:has-text('Skip')", "button:has-text('Later')",
                "button:has-text('Not now')", "button:has-text('Cancel')",
                "[role='dialog'] button[class*='close']"):
        try:
            el = page.locator(sel).first
            if await el.count() and await el.is_visible():
                await el.click(timeout=2000)
                await page.wait_for_timeout(500)
                return
        except Exception:
            pass
    if allow_escape:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(300)
        await page.keyboard.press("Escape")


async def _captcha_visible(page) -> bool:
    """Вернуть True для Aliyun puzzle/verification, если challenge уже показан."""
    for sel in CAPTCHA_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                return True
        except Exception:
            pass
    return False


async def _wait_model_menu(page, trigger, expanded: bool, timeout: int = 5000):
    """Дождаться смены aria-expanded без передачи Locator в page JS."""
    wanted = "true" if expanded else "false"
    deadline = asyncio.get_event_loop().time() + timeout / 1000
    while asyncio.get_event_loop().time() < deadline:
        if await trigger.get_attribute("aria-expanded") == wanted:
            return
        await page.wait_for_timeout(100)
    raise RuntimeError(f"меню модели не перешло в aria-expanded={wanted}")


async def _wait_model_value(page, trigger, model: str, timeout: int = 5000):
    deadline = asyncio.get_event_loop().time() + timeout / 1000
    while asyncio.get_event_loop().time() < deadline:
        if model in (await trigger.inner_text()):
            return
        await page.wait_for_timeout(100)
    raise RuntimeError(f"контрол модели не подтвердил {model!r}")


async def _read_answer(page, timeout: int) -> str:
    """Читает ответ ассистента из DOM: ждёт, пока текст появится и СТАБИЛИЗИРУЕТСЯ.
    Возвращает финальный текст (или "" при ёмкости/таймауте)."""
    sel = "[class*='chat-assistant']"   # контейнер ответа (откалибровано на chat.z.ai)
    print("  [read] ожидание ответа", end="", file=sys.stderr, flush=True)
    deadline = asyncio.get_event_loop().time() + timeout
    last, stable, refusal = "", 0, 0
    while asyncio.get_event_loop().time() < deadline:
        await page.wait_for_timeout(3000)
        await _dismiss_modal(page, allow_escape=False)
        if await _captcha_visible(page):
            print(" CAPTCHA/security verification — ответ недоступен headless",
                  file=sys.stderr)
            await page.screenshot(path=str(OUTPUTS / "glm_captcha_detected.png"))
            return ""
        try:
            els = page.locator(sel)
            txt = (await els.last.inner_text()).strip() if await els.count() else ""
        except Exception:
            txt = ""
        low = txt.lower()
        # ВЕРДИКТ «ОТКАЗ» — ТОЛЬКО ЕСЛИ ОН УСТОЯЛСЯ. Раньше первый же цикл с маркером
        # возвращал «ёмкость/пик», и в это окно попадал баннер peak-hours, живущий в том
        # же контейнере: 29.07 драйвер трижды объявил отказ, пока GLM-5.2 спокойно
        # отвечал «Да» в ручной проверке. Ждём 3 совпадения подряд без роста текста.
        if txt and any(m.lower() in low for m in CAPACITY_MARKERS) and len(txt) < 200:
            refusal = refusal + 1 if txt == last else 1
            if refusal >= 3:
                # Печатаем сам текст: без него «отказ» неотличим от «прочитали не тот
                # контейнер», и мы уже дважды приняли второе за первое.
                print(f" ёмкость/пик — модель отказала (устойчиво). Текст: «{txt[:150]}»",
                      file=sys.stderr)
                return ""
        else:
            refusal = 0
        # «Текст не менялся 6с» — плохой признак готовности: у GLM между блоком
        # размышлений и самим ответом бывает пауза, и 29.07 драйвер отдал огрызок
        # «...Skip» вместо абзаца. Надёжнее спросить страницу, идёт ли генерация:
        # пока висит кнопка Stop — ответ ещё пишется.
        generating = False
        for stop_sel in ("button[aria-label*='Stop']", "button:has-text('Stop')",
                         "[class*='stop'][role='button']"):
            try:
                el = page.locator(stop_sel).first
                if await el.count() and await el.is_visible():
                    generating = True
                    break
            except Exception:
                pass

        body = _drop_thoughts(txt)
        if body and txt == last and not generating and len(body) > 10:
            stable += 1
            if stable >= 2:        # не менялся и генерация не идёт → готово
                print(" готово", file=sys.stderr, flush=True)
                return _strip_cjk(body)
        else:
            stable = 0
        last = txt
        print(".", end="", file=sys.stderr, flush=True)
    print(" timeout", file=sys.stderr, flush=True)
    return _strip_cjk(_drop_thoughts(last))


def _drop_thoughts(txt: str) -> str:
    """Убрать блок рассуждений: GLM кладёт его в тот же контейнер шапкой 'Thought Process'."""
    for marker in ("Thought Process", "Процесс размышлений", "Thinking"):
        if txt.startswith(marker):
            return txt[len(marker):].strip()
    return txt


def main():
    ap = argparse.ArgumentParser(description="GLM-5.2 текст-чат через chat.z.ai")
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
