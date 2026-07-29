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

Auth: GITHUB_TOKEN (permissions: models: read). Всё бесплатно, транспорт = GitHub Models.

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

ENDPOINT = "https://models.github.ai/inference/chat/completions"
# Ансамбль проверен зондом 29.07: эти три отвечают валидным JSON и сходятся между собой.
# По точности на бренд-рубрике: gpt-4o 11/11, gpt-4o-mini 10/11, gpt-4.1 9/11 (2 ложняка).
MODELS = ["openai/gpt-4o", "openai/gpt-4.1", "openai/gpt-4o-mini"]
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
  "face"          — различимы черты лица человека (глаза/нос/рот), даже мелко
  "lone_figure"   — одинокая человеческая фигура в пустом пространстве является СМЫСЛОВЫМ ЦЕНТРОМ
                    кадра (стоит/идёт одна, кадр построен вокруг неё).
                    НЕ ставь, если человек — лишь след присутствия (рука, спина крупно, тень)
                    или крошечная точка на дальнем плане, а кадр про среду/фактуру.
  "text_in_frame" — ЛЮБЫЕ буквы, цифры, надписи, вывески, логотипы, этикетки, вотермарки
                    внутри кадра — даже мелкие, размытые или нечитаемые.
  "neon"          — неоновые ВЫВЕСКИ и кислотные пурпурно-розовые/электрик-синие свечения,
                    киберпанк-эстетика.
                    ⚠️ ВАЖНО: бирюзово-зелёный (teal) грейд всей картинки — это ФИРМЕННАЯ палитра
                    заказчика, она НЕ является неоном. Уличные фонари, фары, отражения в лужах,
                    тёплые огни вдали — тоже НЕ неон, даже если кадр целиком бирюзовый.
                    Ставь "neon" ТОЛЬКО если видишь неоновую вывеску/трубку или кислотный
                    пурпурно-розовый свет.
  "vector_cartoon"— векторная/мультяшная/3D-рендер стилизация вместо снятого кадра
  "glossy_ad"     — глянцевая рекламная/стоковая картинка, «демо возможностей генератора»

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
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


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


def ask_vision(model, prompt, b64, token):
    """Один vision-вызов к GitHub Models → распарсенный JSON или (None, причина).

    Вынесено отдельно, чтобы этим же транспортом пользовался `style_judge.py`: у него своя
    рубрика, но те же модели, тот же ретрай на 429/503 и тот же UA=curl.
    """
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]}],
        "temperature": 0.0,
    }).encode()
    req = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        "Accept": "application/json", "User-Agent": "curl/8.0",
    })
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                raw = json.load(r)["choices"][0]["message"]["content"]
            v = extract_json(raw)
            return (v, None) if v else (None, "битый JSON")
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                wait = min(int(e.headers.get("Retry-After", 8 * (attempt + 1))), 30)
                print(f"      {model} {e.code}, жду {wait}с")
                time.sleep(wait)
                continue
            return None, f"HTTP {e.code}"
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
    r = sh(f'ffmpeg -y -loglevel error -i "{path}" -vf '
           f'"scale=\'min({MAX_SIDE},iw)\':-2" -q:v 4 "{tmp}"')
    if r.returncode == 0 and os.path.exists(tmp):
        b = open(tmp, "rb").read()
        os.unlink(tmp)
        return base64.b64encode(b).decode()
    return base64.b64encode(open(path, "rb").read()).decode()


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
            if x in VALID:
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
    return {
        "violations": sorted(x for x, c in viol.items() if c >= need),   # большинство
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
    if not token:
        print("нет GITHUB_TOKEN (нужны permissions: models: read)", file=sys.stderr)
        return 2
    models = [m.strip() for m in args.models.split(",") if m.strip()] or MODELS

    work = tempfile.mkdtemp(prefix="art_judge_")
    if args.local:
        frames = args.local
    else:
        frames = os.path.join(work, "frames")
        os.makedirs(frames, exist_ok=True)
        print(f"качаю арты из {args.src}")
        sh(f'rclone copy "{args.src}" "{frames}" --include "*.png" --include "*.jpg" '
           f'--include "*.jpeg" --transfers 8')

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
        for m in models:
            v, err = ask_both(m, b64, token)
            short = m.split("/")[-1]
            if v:
                votes.append(v)
                notes.append(f"{short}:{'+'.join(v.get('violations') or ['ok'])}"
                             f"~{'+'.join(v.get('flaws') or ['ok'])}/{v.get('shot_type')}")
            else:
                notes.append(f"{short}:✗{err}")
            time.sleep(1.0)
        agg = aggregate(votes)
        # бренд-брак — жёсткий REJECT; пластик — FLAG «посмотреть глазами», не отбраковка
        if not votes:
            verdict = "ERROR"
        elif agg["violations"]:
            verdict = "REJECT"
        elif agg["flaws"]:
            verdict = "FLAG"
        else:
            verdict = "OK"
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
        print(f"[{i}/{len(files)}] {mark} {rel:34} {agg['shot_type']:8} "
              f"{'коридор ' if agg['corridor'] else ''}{tail}", flush=True)

    ok = [r for r in rows if r["verdict"] == "OK"]
    rej = [r for r in rows if r["verdict"] == "REJECT"]
    flg = [r for r in rows if r["verdict"] == "FLAG"]
    err = [r for r in rows if r["verdict"] == "ERROR"]

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
        L += [f"| {r['file']} | {r['shot_type']} | {r['violations']} |" for r in rej]
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
        sh(f'rclone copy "{f}" "{args.out}/"')
    print(f"\n✓ результат → {args.out}/")
    if not args.local:
        shutil.rmtree(work, ignore_errors=True)
    return 0 if (ok or rej) else 1


if __name__ == "__main__":
    sys.exit(main())
