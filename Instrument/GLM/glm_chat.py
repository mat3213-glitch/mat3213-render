#!/usr/bin/env python3
"""GLM browser worker: authenticated chat.z.ai, DOM answer -> stdout.

Use GLM_HEADLESS=0 with xvfb-run on GH. Session stays outside git.
--diagnostics writes timings/status only, never session or request headers.
"""
import re
import os
import sys
import json
import argparse
import asyncio
import time
from pathlib import Path
from playwright.async_api import async_playwright
from glm_state import split_state, restore_session_storage, capture_state, save_private, state_counts

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


async def chat(prompt: str, model: str, timeout: int, diagnostics=None) -> str:
    diagnostics = diagnostics if diagnostics is not None else {}
    diagnostics["status"] = "starting"
    if not SESSION_FILE.exists():
        print("Нет сессии. Сначала: python3 Instrument/GLM/glm_auth.py", file=sys.stderr)
        diagnostics["status"] = "session_missing"
        return ""

    state = json.loads(SESSION_FILE.read_text())
    diagnostics["state_input"] = state_counts(state)
    native_state, session_storage = split_state(state)
    print(f"  [glm-chat] model={model or 'current'} prompt_chars={len(prompt)}", file=sys.stderr)

    headless = os.environ.get("GLM_HEADLESS", "1").strip().lower() not in {"0", "false", "no"}
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(**chromium_launch_kwargs(channel="chrome", headless=headless))
        except Exception:
            browser = await p.chromium.launch(**chromium_launch_kwargs(headless=headless))
        ctx = await browser.new_context(storage_state=native_state,
                                        viewport={"width": 1280, "height": 900})
        await restore_session_storage(ctx, session_storage)
        page = await ctx.new_page()

        # 180с, а не 60: на Atom под nice+cgroup-лимитом SPA z.ai не успевает за минуту
        # (сеть при этом здорова — curl отдаёт 200 за 2.5с, проверено 2026-07-29).
        diagnostics["status"] = "navigation"
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=180000)
        await page.wait_for_timeout(3500)
        await _dismiss_modal(page)

        # Промо/CAPTCHA могут быть поверх SPA. Не пытаться отправлять текст под challenge:
        # это только превращает понятную причину в 240-секундный таймаут.
        if await _captcha_visible(page):
            print("  [captcha] обнаружена security verification; нужна headed-проверка",
                  file=sys.stderr)
            await page.screenshot(path=str(OUTPUTS / "glm_captcha.png"))
            diagnostics["status"] = "captcha"
            await browser.close()
            return ""

        # An explicit model is a contract: never silently use another model.
        diagnostics["status"] = "model_selection"
        try:
            trigger = page.locator(
                "button.modelSelectorButton[aria-label='Select a model']"
            ).first
            await trigger.wait_for(state="visible", timeout=15000)
            current = _model_label(await trigger.inner_text())
            if model and current != model:
                await trigger.click(timeout=8000)
                await _wait_model_menu(page, trigger, expanded=True)
                menu = page.locator("[role='menu']:visible").last
                await menu.get_by_text(model, exact=True).click(timeout=15000)
                await _wait_model_menu(page, trigger, expanded=False)
                await _wait_model_value(page, trigger, model)
            actual = _model_label(await trigger.inner_text())
            if not actual or (model and actual != model):
                raise RuntimeError("model label not confirmed")
            diagnostics["actual_model"] = actual
            print(f"  [model] confirmed {actual}", file=sys.stderr)
        except Exception as exc:
            diagnostics["status"] = "model_unconfirmed"
            diagnostics["error_type"] = type(exc).__name__
            await page.screenshot(path=str(OUTPUTS / "glm_model_fail.png"))
            await browser.close()
            return ""

        diagnostics["status"] = "input"
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
            submitted_at = time.monotonic()
            await page.keyboard.press("Enter")
            print("  [submit] Enter", file=sys.stderr)
        except Exception as e:
            diagnostics["status"] = "submit_failed"
            diagnostics["error_type"] = type(e).__name__
            print(f"  [submit] ошибка: {type(e).__name__}", file=sys.stderr)
            await page.screenshot(path=str(OUTPUTS / "glm_chat_fail.png"))
            await browser.close()
            return ""

        text = await _read_answer(page, timeout, diagnostics, submitted_at)
        await page.screenshot(path=str(OUTPUTS / "glm_chat_last.png"))
        if text and diagnostics.get("status") == "completed" and os.environ.get("GLM_STATE_OUTPUT"):
            try:
                refreshed = await capture_state(ctx, page)
                save_private(os.environ["GLM_STATE_OUTPUT"], refreshed)
                diagnostics["state_output"] = state_counts(refreshed)
            except Exception as exc:
                diagnostics["state_save_error"] = type(exc).__name__
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


def _model_label(text: str) -> str:
    # Do not match GLM-5.3 as a substring of GLM-5.3-Flash.
    labels = re.findall(r"\bGLM-\d+(?:\.\d+)*(?:-[A-Za-z0-9]+)*", text)
    return labels[0] if len(labels) == 1 else ""


async def _wait_model_value(page, trigger, model: str, timeout: int = 5000):
    deadline = asyncio.get_event_loop().time() + timeout / 1000
    while asyncio.get_event_loop().time() < deadline:
        if model == _model_label(await trigger.inner_text()):
            return
        await page.wait_for_timeout(100)
    raise RuntimeError(f"контрол модели не подтвердил {model!r}")


async def _read_answer(page, timeout: int, diagnostics=None, submitted_at=None) -> str:
    """Читает ответ ассистента из DOM: ждёт, пока текст появится и СТАБИЛИЗИРУЕТСЯ.
    Возвращает финальный текст (или "" при ёмкости/таймауте)."""
    sel = "[class*='chat-assistant']"   # контейнер ответа (откалибровано на chat.z.ai)
    print("  [read] ожидание ответа", end="", file=sys.stderr, flush=True)
    diagnostics = diagnostics if diagnostics is not None else {}
    started = time.monotonic() if submitted_at is None else submitted_at
    diagnostics.update(status="reading", first_text_s=None, completion_s=None)
    deadline = started + timeout
    last, stable, refusal = "", 0, 0
    while time.monotonic() < deadline:
        await page.wait_for_timeout(1000)
        # Never click Cancel/Skip or press Escape while generation is running.
        if time.monotonic() >= deadline:
            break
        if await _captcha_visible(page):
            print(" CAPTCHA/security verification — ответ недоступен headless",
                  file=sys.stderr)
            await page.screenshot(path=str(OUTPUTS / "glm_captcha_detected.png"))
            diagnostics["status"] = "captcha"
            return ""
        try:
            els = page.locator(sel)
            txt = (await els.last.inner_text()).strip() if await els.count() else ""
        except Exception:
            txt = ""
        if txt and diagnostics["first_text_s"] is None:
            diagnostics["first_text_s"] = round(time.monotonic() - started, 3)
            print(f" first_text={diagnostics['first_text_s']}s", file=sys.stderr)
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
                diagnostics["status"] = "refused"
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
        if body and txt == last and not generating and not refusal:
            stable += 1
            if stable >= 2:        # не менялся и генерация не идёт → готово
                diagnostics.update(status="completed",
                                   completion_s=round(time.monotonic() - started, 3),
                                   answer_chars=len(_strip_cjk(body)))
                print(f" готово ({diagnostics['completion_s']}s)", file=sys.stderr, flush=True)
                return _strip_cjk(body)
        else:
            stable = 0
        last = txt
        print(".", end="", file=sys.stderr, flush=True)
    print(" timeout", file=sys.stderr, flush=True)
    diagnostics.update(status="timeout", partial_chars=len(last))
    return ""


def _drop_thoughts(txt: str) -> str:
    """Убрать блок рассуждений: GLM кладёт его в тот же контейнер шапкой 'Thought Process'."""
    for marker in ("Thought Process", "Процесс размышлений", "Thinking"):
        if txt.startswith(marker):
            txt = txt[len(marker):].strip()
            break
    # Thinking... is an animated placeholder, not a completed short answer.
    if not txt.strip(". …\t\r\n"):
        return ""
    return txt


def main():
    ap = argparse.ArgumentParser(description="GLM текст-чат через chat.z.ai")
    ap.add_argument("prompt", nargs="?", default="", help="Промпт (или --stdin)")
    ap.add_argument("--stdin", action="store_true", help="Читать промпт из stdin")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Модель (default {DEFAULT_MODEL})")
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--diagnostics", type=Path, help="JSON timings/status, no credentials")
    ap.add_argument("--output", type=Path, help="Write only the completed answer to this file")
    args = ap.parse_args()
    if args.timeout <= 0:
        ap.error("--timeout must be positive")
    prompt = sys.stdin.read() if args.stdin else args.prompt
    if not prompt.strip():
        ap.error("пустой промпт: передай аргументом или через --stdin")

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text("")  # A failed retry must not leave a stale successful answer.
    OUTPUTS.mkdir(exist_ok=True)
    diagnostics = {"requested_model": args.model, "actual_model": None}
    started = time.monotonic()
    text = ""
    try:
        text = asyncio.run(chat(prompt, args.model, args.timeout, diagnostics))
    except Exception as exc:
        diagnostics.update(status="error", error_type=type(exc).__name__)
        print(f"[glm-chat] {type(exc).__name__}", file=sys.stderr)
    finally:
        diagnostics["total_s"] = round(time.monotonic() - started, 3)
        if args.diagnostics:
            args.diagnostics.parent.mkdir(parents=True, exist_ok=True)
            args.diagnostics.write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n")
        print("[diagnostics] " + json.dumps(diagnostics, ensure_ascii=False), file=sys.stderr)
    if not text:
        sys.exit(1)
    if args.output:
        args.output.write_text(text + "\n")
    print(text)   # чистый ответ в stdout — fanout захватит


if __name__ == "__main__":
    main()
