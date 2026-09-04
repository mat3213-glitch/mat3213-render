#!/usr/bin/env python3
"""
art_judge.py — VLM-СУДЬЯ БРЕНД-БРАКА ПО КАРТИНКЕ (гейт пула артов).

ЗАЧЕМ. `brand_guard.py` судит ТЕКСТ промпта и by design пропускает брак, который появляется
только в пикселях: одинокую фигуру у машины, лицо ребёнка за стеклом, этикетку на сумке.
Ровно это выпустило в рендер stemgate-A/B (29.07). Этот файл смотрит на ГОТОВЫЙ АРТ.

ЧТО ОТДАЁТ (два выхода, оба нужны):
  1. violations — бинарный гейт брака (лицо / одинокая фигура / текст в кадре / неон /
     вектор / глянец). Не мерджит и ничего не удаляет: последнее слово за yaromat.
  2. shot_type — ТИП ПЛАНА каждого арта. Это главный выход: «логика кадра» важнее единого
     лука (решение yaromat 29.07). Сводка показывает, есть ли в пуле разброс планов или
     это опять 14 одинаковых коридоров, как в пуле v1.

КАЛИБРОВКА (29.07, 4 арта с моими метками глазами, `probe_rubric.py`):
  ансамбль совпал 3/4, единственный расход — ложный `neon` на бирюзовом кадре. Вылечено
  явной оговоркой в рубрике: teal-грейд = наша палитра, не неон. Llama-4-Scout выкинута из
  ансамбля — путала поля (совала shot_type в violations) и выдумывала text_in_frame.
  Мажоритарная агрегация (≥2 из 3): один шумный голос не должен браковать годный кадр.

Транспорт — ТРИ РАЗНЫХ ПРОВАЙДЕРА (30.07), потому что три модели одного вендора умирают одной
квотой: `cf:` — наш CF Worker /analyze-frame (Workers AI, ключи CLOUDFLARE_WORKER+WORKER_SECRET),
`or:` — OpenRouter free (OPENROUTER_API_KEY).
Всё бесплатно. Судья без своего ключа молча выбывает, остальные работают.

Запуск:
  python3 art_judge.py --src "ydrive:.../pool" --out "ydrive:.../result_art_judge"
  python3 art_judge.py --local ./arts --out "ydrive:.../result_art_judge" --limit 10
"""
import argparse
import base64
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter

OR_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

# АНСАМБЛЬ ИЗ ТРЁХ РАЗНЫХ ПРОВАЙДЕРОВ (решение yaromat 30.07). Разные транспорты
# нужны, чтобы одна общая квота не выключала сразу весь судейский ансамбль.
# Gemini отклонён (yaromat: «быстро тратятся токены»), Qwen отклонён (2 мин/кадр через браузер).
# Замеры 30.07 на реальном арте: CF 1.3с, nemotron 3с, gpt-4o-mini ~2с; JSON отдают все.
# Замер кандидатов на 4 размеченных кадрах (портрет маслом / логотип на пианино /
# лесная тропа / вода с бликами):
#   gemma-4-26b  — 2/2 пойманных браков, 6–14с. Ложняки были ТОЛЬКО на `text_in_frame`,
#                  а его теперь судит OCR (см. aggregate) → лучший голос из живых.
#   cf:llama-3.2 — 2с, но 2 промаха и 1 ложняк; на одном портрете при temperature=0 дал
#                  разные ответы в двух прогонах. Годится ТОЛЬКО как дополнительный голос.
#   nemotron-nano— 504 «Upstream idle timeout» на длинной рубрике (на короткой отвечает за 2с).
#   gemma-4-31b  — 429 на каждом кадре, все ретраи. В панель не берём.
JUDGES = [
    "or:google/gemma-4-26b-a4b-it:free",        # OpenRouter free, самый точный из живых
    "cf:llama-3.2-11b-vision",                  # наш CF Worker /analyze-frame, без внешних квот
    "or:nvidia/nemotron-nano-12b-v2-vl:free",   # нестабилен по таймауту, но иногда отвечает
    # Резерв: включается сам, когда кто-то выше выбывает — иначе на длинном пуле
    # можно остаться с одним голосом, а бренд судится большинством.
    "or:nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "or:google/gemma-4-31b-it:free",
]
MODELS = JUDGES        # обратная совместимость: style_judge импортирует MODELS
PANEL = 3              # сколько судей опрашиваем на кадр (остальные в списке — резерв)
VALID = {"face", "lone_figure", "text_in_frame", "neon", "vector_cartoon", "glossy_ad"}
FLAWS = {"proportions", "anatomy", "geometry", "physics", "light", "density"}
SHOTS = {"wide", "medium", "detail", "texture", "motion", "unclear"}
MAX_SIDE = 768   # больше не нужно: судим композицию и наличие объектов, не микро-детали

# ⚠️ ДВА ОТДЕЛЬНЫХ ВЫЗОВА, НЕ ОДИН. Пробовал слить бренд и пластик в один промпт ради экономии
# (вдвое меньше запросов) — ЗАМЕРЕНО, ЧТО ЭТО ЛОМАЕТ ОБА СИГНАЛА: на `anchor.png` голоса за
# lone_figure упали 3/3 → 1/3 (мажоритарный порог погасил, явный брак прошёл как годный), на
# `cold_03.png` пластик упал 1/3 → 0/3. Модель, получив два разных задания, отвечает «в среднем».
# Цена раздельных вызовов — вдвое больше запросов. Платим: гейт, который пропускает брак, не нужен.
BRAND_PROMPT = """Ты — контролёр кадра для музыкальных клипов. Смотри на КАРТИНКУ и отвечай о том, что в ней ВИДНО. Не оценивай красоту.

Ответь строго JSON без пояснений:
{"shot_type":"...","corridor":true/false,"violations":[...],"note":"..."}

shot_type — тип плана, ровно одно значение:
  "wide"    — общий план, читается пространство/среда целиком
  "medium"  — средний: объект и его окружение поровну
  "detail"  — крупный план одного предмета
  "texture" — кадр про фактуру/поверхность/материю (асфальт, стекло, вода, ткань), объекта-героя нет
  "motion"  — кадр построен на смазе/движении/следе света
  "unclear" — не читается

corridor — true, если это коридорная перспектива с точкой схода примерно по центру
  (уходящая вдаль дорога/коридор/тоннель/ряд по центру кадра). Иначе false.

violations — перечисли ТОЛЬКО то, что реально видишь. Пустой список, если ничего:
  "face"          — различимы черты лица человека (глаза/нос/рот), даже мелко.
                    ⚠️ Лицо ЛЮБОЙ природы: фотография, ЖИВОПИСЬ, портрет маслом, рисунок,
                    скульптура, статуя, кукла, лицо на плакате или на экране внутри кадра.
                    «Это картина, а не человек» — НЕ повод промолчать: ставь "face".
  "lone_figure"   — одинокая человеческая фигура в пустом пространстве является СМЫСЛОВЫМ ЦЕНТРОМ
                    кадра (стоит/идёт одна, кадр построен вокруг неё).
                    НЕ ставь, если человек — лишь след присутствия (рука, спина крупно, тень)
                    или крошечная точка на дальнем плане, а кадр про среду/фактуру.
  "neon"          — неоновые ВЫВЕСКИ и кислотные пурпурно-розовые/электрик-синие свечения,
                    киберпанк-эстетика.
                    ⚠️ ВАЖНО: бирюзово-зелёный (teal) грейд всей картинки — это ФИРМЕННАЯ палитра
                    заказчика, она НЕ является неоном. Уличные фонари, фары, отражения в лужах,
                    тёплые огни вдали — тоже НЕ неон, даже если кадр целиком бирюзовый.
                    Ставь "neon" ТОЛЬКО если видишь неоновую вывеску/трубку или кислотный
                    пурпурно-розовый свет.
  "vector_cartoon"— векторная/мультяшная/3D-рендер стилизация вместо снятого кадра
  "glossy_ad"     — глянцевая рекламная/стоковая картинка, «демо возможностей генератора»

⚠️ Надписи, буквы, логотипы и вотермарки НЕ ОЦЕНИВАЙ и в violations НЕ пиши: их ловит
отдельный детектор по пикселям. Замер 05.08: на чистых кадрах (лесная тропа, вода с бликами)
модели ставили "text_in_frame" там, где текста нет — это был главный источник ложных тревог.

note — одна короткая фраза по-русски: что в кадре.
"""

PLASTIC_PROMPT = """Ты — технический контролёр AI-генерации. Ищи признаки того, что картинку СГЕНЕРИРОВАЛА нейросеть и ошиблась в правдоподобии. Красоту, настроение, цвет и композицию НЕ оценивай.

Ответь строго JSON без пояснений:
{"flaws":[...],"worst":"...","note":"..."}

flaws — перечисли ТОЛЬКО те дефекты, которые реально ВИДНО. Пустой список, если правдоподобно:
  "proportions" — пропорции узнаваемого объекта неправильные: машина слишком длинная/короткая,
                  колёса не того размера или не на месте, кузов/окна/двери не сходятся,
                  предмет «почти как настоящий, но форма не та»
  "anatomy"     — искажения тела: руки, пальцы, конечности, посадка головы
  "geometry"    — невозможная или плывущая конструкция: детали сливаются друг с другом,
                  предмет не сходится сам с собой, лишние или пропавшие части
  "physics"     — огонь, вода, дым, брызги, частицы ведут себя не как в реальности;
                  предмет висит/парит без опоры
  "light"       — свет и тени не соответствуют источнику; симметричное «CGI»-свечение;
                  отражение не совпадает с тем, что отражается
  "density"     — неправдоподобная плотность или расположение объектов в пространстве

⚠️ НЕ считай дефектом: мягкий туман, дымку, боке, зерно, тёмный кадр, размытый дальний план,
плавную атмосферу, ровное небо, лес, воду в покое — это нормальная съёмка, а не ошибка модели.
Дефект — только НЕПРАВДОПОДОБИЕ формы, физики или света.

worst — одна строка: самый грубый дефект правдоподобия своими словами, или "нет".
note — одна короткая фраза по-русски: что в кадре.
"""


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def extract_json(txt):
    """Первый сбалансированный {...}; терпит ```json-заборы и прозу вокруг."""
    txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
    depth = 0
    start = None
    for i, ch in enumerate(txt):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    return json.loads(txt[start:i + 1])
                except Exception:
                    start = None
    return None


class _ProviderError(Exception):
    """Отказ провайдера, приехавший ТЕЛОМ ответа при HTTP 200 (так делает OpenRouter)."""

    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def _post(url, payload, headers, timeout=90):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _chat_completions(url, token, model, prompt, b64, timeout=90):
    """Общий формат OpenAI-совместимого API OpenRouter."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]}],
        "temperature": 0.0,
    }
    d = _post(url, payload, {
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        "Accept": "application/json", "User-Agent": "curl/8.0",
    }, timeout)
    # 🔴 OpenRouter отдаёт отказ провайдера ТЕЛОМ при HTTP 200 («Upstream error … 502»,
    # «rate limited»). Раньше код лез прямо в choices и получал KeyError — судья выбывал
    # молча, а прогон выглядел полноценным (замер 05.08: два голоса из трёх падали так).
    err = d.get("error")
    if err:
        msg = err.get("message") if isinstance(err, dict) else str(err)
        code = err.get("code") if isinstance(err, dict) else None
        raise _ProviderError(f"{code or 'error'}: {str(msg)[:120]}", retryable=code in (429, 502, 503))
    try:
        return d["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise _ProviderError(f"нет choices: {json.dumps(d, ensure_ascii=False)[:120]}")


def ask_vision(judge, prompt, b64, token=None):
    """Один vision-вызов → (распарсенный JSON, None) либо (None, причина).

    `judge` — «провайдер:модель»: `cf:` (наш CF Worker /analyze-frame) или
    `or:` (OpenRouter). Транспорты разные, контракт один:
    JSON по рубрике.
    """
    prov, _, model = judge.partition(":")
    if not model:
        return None, "нужен префикс cf: или or:"

    for attempt in range(3):
        try:
            if prov == "cf":
                # Наш воркер сам зовёт Workers AI и сам парсит ответ: в `analysis` уже
                # объект, если модель отдала JSON, иначе строка — обрабатываем оба случая.
                base = os.environ.get("CLOUDFLARE_WORKER", "").rstrip("/")
                if not base:
                    return None, "нет CLOUDFLARE_WORKER"
                d = _post(f"{base}/analyze-frame", {"image": b64, "prompt": prompt},
                          {"Content-Type": "application/json", "User-Agent": "curl/8.0",
                           "X-Worker-Secret": os.environ.get("WORKER_SECRET", "")})
                a = d.get("analysis")
                v = a if isinstance(a, dict) else extract_json(a or "")
            elif prov == "or":
                key = os.environ.get("OPENROUTER_API_KEY", "")
                if not key:
                    return None, "нет OPENROUTER_API_KEY"
                v = extract_json(_chat_completions(OR_ENDPOINT, key, model, prompt, b64))
            else:
                return None, f"неподдерживаемый провайдер: {prov}"
            return (v, None) if v else (None, "битый JSON")
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                wait = min(int(e.headers.get("Retry-After", 8 * (attempt + 1))), 30)
                print(f"      {judge} {e.code}, жду {wait}с")
                time.sleep(wait)
                continue
            if e.code == 410:
                return None, "провайдер снят (410)"
            return None, f"HTTP {e.code}"
        except _ProviderError as e:
            if e.retryable and attempt < 2:
                time.sleep(6 * (attempt + 1))
                continue
            return None, str(e)
        except Exception as e:
            return None, f"{type(e).__name__}"
    return None, "rate-limit исчерпан"


def ask_both(model, b64, token):
    """Два РАЗДЕЛЬНЫХ вызова на кадр: бренд и пластик. Слияние промптов проверено и отвергнуто
    (см. комментарий у BRAND_PROMPT). Возвращает объединённый словарь голоса одной модели."""
    brand, e1 = ask_vision(model, BRAND_PROMPT, b64, token)
    time.sleep(0.6)
    plastic, e2 = ask_vision(model, PLASTIC_PROMPT, b64, token)
    if brand is None and plastic is None:
        return None, e1 or e2
    v = dict(brand or {})
    v.update({k: (plastic or {}).get(k) for k in ("flaws", "worst")})
    if brand is None:
        v["_brand_err"] = e1
    if plastic is None:
        v["_plastic_err"] = e2
    return v, None


def to_jpeg_b64(path):
    """Ужимаем до MAX_SIDE и в JPEG — иначе base64 PNG раздувает запрос. Fail-open в оригинал."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            im = im.convert("RGB")
            k = MAX_SIDE / max(im.size)
            if k < 1:
                im = im.resize((int(im.width * k), int(im.height * k)))
            tmp = tempfile.mktemp(suffix=".jpg")
            im.save(tmp, "JPEG", quality=80)
            b = open(tmp, "rb").read()
            os.unlink(tmp)
            return base64.b64encode(b).decode()
    except Exception:
        pass
    tmp = tempfile.mktemp(suffix=".jpg")
    r = sh(["ffmpeg", "-y", "-loglevel", "error", "-i", path, "-vf",
            f"scale='min({MAX_SIDE},iw)':-2", "-q:v", "4", tmp])
    if r.returncode == 0 and os.path.exists(tmp):
        b = open(tmp, "rb").read()
        os.unlink(tmp)
        return base64.b64encode(b).decode()
    return base64.b64encode(open(path, "rb").read()).decode()


def judge_file(path, models=None, token=None, dead=None, b64=None):
    """Судейство ОДНОГО кадра ансамблем → вердикт и детали.

    Вынесено из main, чтобы тем же судьёй пользовался генератор пула (`arts_pool_job.py`):
    решение yaromat 30.07 — «судья должен смотреть каждую генерацию, брак не попадает в пул».
    `dead` — общий на прогон словарь выбывших по квоте судей: создаётся ОДИН раз снаружи,
    иначе предохранитель обнуляется на каждом кадре и снова жжёт ожидания.
    """
    models = models or MODELS
    token = token if token is not None else os.environ.get("GITHUB_TOKEN")
    dead = dead if dead is not None else {}

    # ── OCR-гейт ДО моделей ────────────────────────────────────────────────────────
    # Надпись в кадре — факт, а не вкус, и ансамбль её ПРОПУСКАЛ (child.png прошёл
    # голосование чистым 29.07). Детектор регионов ловит её детерминированно, поэтому
    # он идёт первым: пойманный кадр не тратит три вызова моделей.
    # Правило (замер на листе A, run 30811081122): ДВА мелких региона; 1/1 пойманного,
    # 0/10 ложных, устойчиво по всей полосе порогов. Выключатель: OCR=off.
    if os.environ.get("OCR", "on").lower() != "off":
        try:
            from ocr_gate import gate
            ocr = gate(path, profile="art")
            if ocr.get("available") and ocr.get("has_text"):
                return {
                    "verdict": "REJECT", "violations": ["text_in_frame"], "flaws": [],
                    "shot_type": None, "corridor": None, "n_votes": 0,
                    "detail": f"ocr:{ocr['reason']}", "_votes": [], "_notes": [],
                    # `_agg` обязан иметь ТУ ЖЕ форму, что у aggregate(): сводка в main()
                    # читает из него shot_type/corridor напрямую и падала на пустом словаре
                    "_agg": {"violations": ["text_in_frame"], "flaws": [],
                             "shot_type": None, "corridor": None,
                             "votes_raw": [], "worst": None},
                    "_ocr": ocr,
                }
        except Exception as exc:
            # fail-open: гейт не обязан ронять судейство (та же логика, что у судьи в пуле)
            print(f"      ⚠️ ocr-гейт пропущен: {exc}", flush=True)

    if b64 is None:
        b64 = to_jpeg_b64(path)

    votes, notes, asked = [], [], 0
    for m in list(models):
        # Спрашиваем ровно PANEL ЖИВЫХ судей: список длиннее панели намеренно — хвост это
        # резерв, он подключается сам, когда кто-то выбыл по квоте. Поэтому отсев мёртвых
        # идёт ДО счётчика, а не срезом models[:PANEL] (иначе резерв недостижим).
        if asked >= PANEL:
            break
        if dead.get(m, 0) >= 2:
            continue
        asked += 1
        v, err = ask_both(m, b64, token)
        # Предохранитель: при исчерпанной СУТОЧНОЙ квоте судья отвечает 429 с Retry-After
        # в десятки минут, и ретраи жгут по полторы минуты на кадр впустую (замер 30.07:
        # gpt-4o-mini умер на первом же арте). Два подряд — исключаем до конца прогона.
        if v is None and err and "rate-limit" in err:
            dead[m] = dead.get(m, 0) + 1
            if dead[m] >= 2:
                print(f"      ⛔ {m} выбывает из прогона: квота исчерпана", flush=True)
        elif v is not None:
            dead[m] = 0
        short = m.split(":", 1)[0] + "/" + m.split("/")[-1].split(":")[0]
        if v:
            # чей это голос — нужно и для отладки, и чтобы схему агрегации можно было
            # пересчитать ОФЛАЙН по сохранённым голосам, не гоняя модели заново
            v["_judge"] = m
            votes.append(v)
            notes.append(f"{short}:{'+'.join(v.get('violations') or ['ok'])}"
                         f"~{'+'.join(v.get('flaws') or ['ok'])}/{v.get('shot_type')}")
        else:
            notes.append(f"{short}:✗{err}")
        time.sleep(1.0)

    agg = aggregate(votes)
    # бренд-брак — жёсткий REJECT; пластик — FLAG «посмотреть глазами», не отбраковка.
    # n_votes<2 — НЕ полноценный вердикт: порог бренда = большинство (≥2 из 3), на одном
    # голосе он вырождается в «что сказала единственная выжившая модель».
    if not votes:
        verdict = "ERROR"
    elif len(votes) < 2:
        verdict = "PARTIAL"
    elif agg["violations"]:
        verdict = "REJECT"
    elif agg["flaws"]:
        verdict = "FLAG"
    else:
        verdict = "OK"
    return {
        "verdict": verdict, "violations": agg["violations"], "flaws": agg["flaws"],
        "disputed": agg.get("disputed") or [], "shot_type": agg["shot_type"],
        "corridor": agg["corridor"],
        "n_votes": len(votes), "detail": " | ".join(notes),
        "_votes": votes, "_notes": notes, "_agg": agg,
    }


def aggregate(votes):
    """Ансамбль → один вердикт. ДВА РАЗНЫХ ПОРОГА — это не небрежность, а замер.

    БРЕНД (violations) — порог БОЛЬШИНСТВО (≥2 из 3). Объединение голосов даёт ложняки:
    gpt-4.1 навесил лишний `text_in_frame` на два годных кадра, большинство его погасило.

    ПЛАСТИК (flaws) — порог ОДИН ГОЛОС. На калибровке 29.07 эталонный брак `cold_03`
    («иишный авто с неправильными пропорциями», вердикт yaromat) увидел ТОЛЬКО gpt-4.1;
    gpt-4o и mini сказали «чисто», и большинство задавило верный сигнал. На правдоподобии
    роли моделей переворачиваются относительно бренда. Поэтому пластик — не автоотбраковка,
    а ФЛАГ НА ПРОСМОТР: цена пропуска выше цены лишней проверки. Плата — ложные тревоги.
    """
    viol, flaw, shots = Counter(), Counter(), Counter()
    corr = 0
    worst = []
    for v in votes:
        for x in set(v.get("violations") or []):
            # `text_in_frame` от VLM игнорируем НАМЕРЕННО: надписи судит детерминированный
            # OCR-гейт до опроса моделей, а модели ставили его на чистых кадрах (замер 05.08:
            # gemma навесила текст на лесную тропу и на воду с бликами — 2 ложняка из 2).
            if x in VALID and x != "text_in_frame":
                viol[x] += 1
        for x in set(v.get("flaws") or []):
            if x in FLAWS:
                flaw[x] += 1
        s = str(v.get("shot_type") or "").strip().lower()
        if s in SHOTS:
            shots[s] += 1
        if v.get("corridor") is True:
            corr += 1
        w = str(v.get("worst") or "").strip()
        if w and w.lower() not in ("нет", "none", "-", "—"):
            worst.append(w)
    need = len(votes) // 2 + 1 if votes else 1
    # 🔴 СПОРНЫЕ ЖЁСТКИЕ ТАБУ (замер 05.08). Панель усохла: надёжен один судья (gemma),
    # остальные шумят или отваливаются. При двух голосах большинство = 2 из 2, то есть
    # МОЛЧАНИЕ шумного судьи гасит верный голос — ровно так портрет маслом уехал в пул.
    # Автобраковать по одному голосу тоже нельзя: cf выдумал `face` на воде и на клавишах.
    # Поэтому третий исход: лицо/одинокая фигура, за которые голосовали без большинства,
    # едут в `disputed` — кадр не в пул и не в мусор, а на глаз владельцу.
    majority = sorted(x for x, c in viol.items() if c >= need)
    disputed = sorted(x for x, c in viol.items()
                      if c < need and x in ("face", "lone_figure"))
    return {
        "violations": majority,                                          # большинство
        "disputed": disputed,                                            # голос был, кворума нет
        "flaws": sorted(x for x, c in flaw.items() if c >= 1),           # любой голос
        "worst": worst[0][:120] if worst else "",
        "shot_type": shots.most_common(1)[0][0] if shots else "unclear",
        "corridor": corr >= need,
        "votes_raw": votes,
    }


def main():
    ap = argparse.ArgumentParser(description="VLM-судья бренд-брака по картинке")
    ap.add_argument("--src", default="ydrive:Content factory/cloud_io/render_jobs/"
                                     "vzrosly_pool_v2_2026-07-17/pool")
    ap.add_argument("--out", required=True, help="куда класть результат (rclone-путь)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--models", default="")
    ap.add_argument("--local", default="", help="судить уже скачанную папку, без rclone")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    models = [m.strip() for m in args.models.split(",") if m.strip()] or MODELS

    # Ансамбль мультипровайдерный, поэтому нехватка одного ключа — не повод падать целиком:
    # выкидываем только тех судей, кому нечем ходить, и говорим об этом вслух.
    need = {"cf": ("CLOUDFLARE_WORKER", os.environ.get("CLOUDFLARE_WORKER")),
            "or": ("OPENROUTER_API_KEY", os.environ.get("OPENROUTER_API_KEY")),
            "gh": ("GITHUB_TOKEN", token)}
    alive = []
    for m in models:
        prov = m.split(":", 1)[0] if ":" in m else "gh"
        env_name, val = need.get(prov, ("", "1"))
        if val:
            alive.append(m)
        else:
            print(f"⚠️ судья {m} выбывает: нет {env_name}", file=sys.stderr)
    if not alive:
        print("не осталось ни одного судьи — нужен хотя бы один ключ "
              "(CLOUDFLARE_WORKER / OPENROUTER_API_KEY / GITHUB_TOKEN)", file=sys.stderr)
        return 2
    models = alive

    work = tempfile.mkdtemp(prefix="art_judge_")
    if args.local:
        frames = args.local
    else:
        frames = os.path.join(work, "frames")
        os.makedirs(frames, exist_ok=True)
        print(f"качаю арты из {args.src}")
        sh(["rclone", "copy", args.src, frames, "--include", "*.png", "--include", "*.jpg",
            "--include", "*.jpeg", "--transfers", "8"])

    files = []
    for root, _, names in os.walk(frames):
        for n in sorted(names):
            if n.lower().endswith((".png", ".jpg", ".jpeg")):
                files.append(os.path.join(root, n))
    files.sort()
    if args.limit:
        files = files[:args.limit]
    if not files:
        print(f"артов не найдено в {frames}", file=sys.stderr)
        return 1
    print(f"судим {len(files)} артов ансамблем: {', '.join(models)}\n")

    rows, full = [], []
    dead = {}          # судья → сколько раз подряд упёрся в исчерпанную квоту
    for i, p in enumerate(files, 1):
        rel = os.path.relpath(p, frames)
        try:
            b64 = to_jpeg_b64(p)
        except Exception as e:
            print(f"[{i}/{len(files)}] {rel}: не читается ({e})")
            rows.append({"file": rel, "shot_type": "", "corridor": "", "violations": "",
                         "n_votes": 0, "verdict": "ERROR", "detail": f"read: {e}"})
            continue
        votes, notes = [], []
        res = judge_file(p, models=models, token=token, dead=dead, b64=b64)
        votes, notes, agg, verdict = res["_votes"], res["_notes"], res["_agg"], res["verdict"]
        rows.append({
            "file": rel, "shot_type": agg["shot_type"], "corridor": agg["corridor"],
            "violations": ";".join(agg["violations"]), "flaws": ";".join(agg["flaws"]),
            "worst": agg["worst"], "n_votes": len(votes),
            "verdict": verdict, "detail": " | ".join(notes),
        })
        full.append({"file": rel, **agg, "verdict": verdict})
        mark = {"REJECT": "🔴", "FLAG": "🟡", "OK": "✅"}.get(verdict, "⚠️")
        tail = ",".join(agg["violations"]) or ("~" + ",".join(agg["flaws"]) if agg["flaws"] else "—")
        # flush обязателен: при выводе в файл прогресс копится в буфере, и на длинном прогоне
        # (116 артов ≈ 2ч) не отличить работу от зависания
        # shot_type может быть None (кадр отбракован OCR-гейтом до опроса моделей —
        # тип плана тогда никто не называл), а формат `:8` на None падает
        print(f"[{i}/{len(files)}] {mark} {rel:34} {str(agg['shot_type'] or '—'):8} "
              f"{'коридор ' if agg['corridor'] else ''}{tail}", flush=True)

    ok = [r for r in rows if r["verdict"] == "OK"]
    rej = [r for r in rows if r["verdict"] == "REJECT"]
    flg = [r for r in rows if r["verdict"] == "FLAG"]
    err = [r for r in rows if r["verdict"] == "ERROR"]
    part = [r for r in rows if r["verdict"] == "PARTIAL"]

    csv_p = os.path.join(work, "judgement.csv")
    with open(csv_p, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["file", "shot_type", "corridor", "violations",
                                           "flaws", "worst", "n_votes", "verdict", "detail"])
        w.writeheader()
        w.writerows(rows)
    json_p = os.path.join(work, "judgement.json")
    open(json_p, "w", encoding="utf-8").write(json.dumps(full, ensure_ascii=False, indent=1))

    shot_cnt = Counter(r["shot_type"] for r in rows if r["shot_type"])
    viol_cnt, flaw_cnt = Counter(), Counter()
    for r in rows:
        for v in filter(None, r["violations"].split(";")):
            viol_cnt[v] += 1
        for v in filter(None, r["flaws"].split(";")):
            flaw_cnt[v] += 1
    corridors = sum(1 for r in rows if r["corridor"] is True)

    L = ["# Судья пула артов — бренд-брак, пластик и логика кадра", "",
         f"Ансамбль: {', '.join(models)}  ·  источник: `{args.src if not args.local else args.local}`", "",
         f"- Всего артов: **{len(rows)}**",
         f"- ✅ Годных: **{len(ok)}**  ·  🔴 Бренд-брак: **{len(rej)}**  ·  "
         f"🟡 Флаг пластика: **{len(flg)}**  ·  Ошибок: {len(err)}",
         (f"- ⚠️ **Неполный ансамбль (1 голос из 3): {len(part)}** — вердикт по ним НЕ засчитан, "
          f"порог бренда требует большинства. Причина обычно одна: rate-limit провайдера."
          if part else "- Ансамбль отработал полностью на всех артах."),
         f"- Коридорная перспектива: **{corridors}** из {len(rows)}"
         f" ({100*corridors//max(len(rows),1)}%) — в пуле v1 это было 14/26 и выжгло монтаж",
         "", "## Логика кадра — разброс типов плана", "",
         "Главный выход. Ровный разброс = материал держит рез; перекос в один тип = монтаж",
         "будет монотонным, чем ни крой.", "",
         "| тип плана | штук | доля |", "|---|---|---|"]
    for s, c in shot_cnt.most_common():
        L.append(f"| {s} | {c} | {100*c//max(len(rows),1)}% |")
    L += ["", "## Что забраковано и почему", ""]
    if viol_cnt:
        L += ["| нарушение | срабатываний |", "|---|---|"]
        L += [f"| {v} | {c} |" for v, c in viol_cnt.most_common()]
    else:
        L.append("Нарушений не найдено.")
    if rej:
        L += ["", "### 🔴 Бренд-брак (порог: большинство голосов)", "",
              "| файл | тип плана | нарушения |", "|---|---|---|"]
        L += [f"| {r['file']} | {r['shot_type'] or '—'} | {r['violations']} |" for r in rej]
    L += ["", "## Пластик — флаг на просмотр, не отбраковка", ""]
    if flaw_cnt:
        L += ["| дефект | срабатываний |", "|---|---|"]
        L += [f"| {v} | {c} |" for v, c in flaw_cnt.most_common()]
        L += ["", "| файл | дефекты | что увидела модель |", "|---|---|---|"]
        L += [f"| {r['file']} | {r['flaws']} | {r['worst']} |"
              for r in rows if r["flaws"]]
    else:
        L.append("Дефектов правдоподобия не найдено.")
    L += ["", "---", "",
          "Судья НЕ удаляет и НЕ мерджит: решение по каждому арту за yaromat.",
          "Пороги разные и это замерено: **бренд — большинство (≥2 из 3)**, потому что один",
          "шумный голос иначе бракует годный кадр; **пластик — один голос**, потому что",
          "эталонный `cold_03` увидела ровно одна модель из трёх, и большинство его теряло."]
    summary = "\n".join(L)
    sum_p = os.path.join(work, "SUMMARY.md")
    open(sum_p, "w", encoding="utf-8").write(summary)
    print("\n" + summary)

    for f in (csv_p, json_p, sum_p):
        sh(["rclone", "copy", f, f"{args.out}/"])
    print(f"\n✓ результат → {args.out}/")
    if not args.local:
        shutil.rmtree(work, ignore_errors=True)
    return 0 if (ok or rej) else 1


if __name__ == "__main__":
    sys.exit(main())
