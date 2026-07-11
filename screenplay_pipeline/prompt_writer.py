#!/usr/bin/env python3
"""
prompt_writer.py — «оператор-постановщик»: превращает шоты раскадровки в БОГАТЫЕ
gen-промты для t2v (Seedance/VeoFree, Qwen), а не в склейку стоко-запроса+слов+запретов.

ЗАЧЕМ (урок 2026-07-11): раньше промт для генератора собирался как
`director.base.query (стоко-поиск!) + intent + cues`, обёрнутый в запреты спереди. Генератор
получал «что-то тёмное, cinematic» и мазал. Насмотренность/камера-словарь/reference_recipes
НЕ доходили до промта. Этот модуль — недостающее звено: LLM пишет на КАЖДЫЙ шот мастер-промт
по схеме, вооружённый reference-грамматикой и правилами i2v-prompter скилла.

СХЕМА МАСТЕР-ПРОМТА (порядок = рычаг качества, из skills/craft/i2v-prompter):
  реалистичный субъект+якорь(«real, not a doll/toy») → что он ДЕЛАЕТ (непрерывное действие) →
  конкретный сеттинг/пропсы → ПОВЕДЕНИЕ света (один источник) → камера (оптика/движение/шаттер) →
  плёнка/стока+грейд → короткий негатив В КОНЦЕ. «Кинематографичным» кадр делает ЦЕПОЧКА, не слово.

Вход:  storyboard.json (от director) + brief_full.yaml (вайб/грейд) + reference_recipes.json (грамматика).
Выход: storyboard.json с полем shot["gen_prompt"] на каждом шоте (scene_dispatch берёт его).
Fail-open: если LLM недоступен — шоты без gen_prompt, пайплайн не падает (scene_dispatch фолбэк).

Usage:
  python3 prompt_writer.py --storyboard sb.json [--brief brief.yaml] [--references recipes.json] [-o out.json]
Требует GROQ_API_KEY (или GEMINI_API_KEY) в env.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

import requests
import yaml

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env", override=False)
except Exception:
    pass

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# Правила из skills/craft/i2v-prompter (жёсткие рамки yaromat) — вшиты в system, чтобы LLM
# не выдумывал: без лиц (силуэт/контур/спина/тень ок), без текста в кадре, анти-пластик,
# один источник света, фотографичная моторика, минимум негатива в конце.
SCHEMA_RULES = """Ты — оператор-постановщик и prompt-engineer под t2v-генераторы (Seedance 2.0 / Qwen).
Пиши ОДИН плотный английский промт (~70-95 слов) на каждый шот СТРОГО по схеме-цепочке:
1) SUBJECT + realism anchor: конкретный реалистичный субъект + якорь реализма ("a real <...>, not a doll, not a toy, natural human proportions and weight"). Анти-«игрушечность».
2) ACTION: что субъект ДЕЛАЕТ непрерывно сквозь кадр (глагол, микро-движение) — даёт связную моторику, не морфинг.
3) SETTING: конкретные существительные-пропсы (эпоха/модель/материал), время суток, погода.
4) LIGHT: ОДИН источник + его ПОВЕДЕНИЕ (sweeps/falls/glows-behind), цвет теней. Не смешивать источники.
5) CAMERA: оптика (mm) + крепление (locked-off/handheld micro-shake) + движение + "180-degree shutter motion blur".
6) TEXTURE/GRADE: описывай РЕЗУЛЬТАТ плёнки, НЕ называй сток дословно — «fine film grain, soft halation, gentle gate weave, organic analog texture» + грейд (muted desaturated cold и т.п.). ВАЖНО: дословные имена стоков/форматов ("16mm Kodak Vision3 500T") заставляют модель РИСОВАТЬ плёночную рамку с ЧИТАЕМЫМ текстом-маркировкой (проверено 2026-07-11) — НЕ называть.
7) NEGATIVE в самом КОНЦЕ, коротко: "Negative: text, letters, film border, sprocket holes, watermark, logos, visible face, doll, cgi plastic sheen".
ЖЁСТКО: без читаемого лица (силуэт/профиль-в-тени/контур/спина — ок), без читаемого текста/лого в кадре,
один источник света, фотореализм (борьба с пластиком), без неона. НЕ начинай с запретов — негатив только в конце.
Верни СТРОГО JSON: {"shots":[{"idx":<int>,"gen_prompt":"<english prompt>"}, ...]} для ВСЕХ шотов."""


def _refs_block(refs):
    if not refs:
        return ""
    usable = [r for r in refs if r.get("verdict") != "мимо"][:3]
    lines = []
    for r in usable:
        lines.append(
            f"- {r.get('title','')}: свет/цвет={r.get('color','')}; композиция={r.get('composition','')}; "
            f"движение={r.get('motion','')}; ритм={r.get('rhythm','')}"
        )
    return "РЕФЕРЕНС-ГРАММАТИКА (транспонируй язык света/цвета/камеры в промты, НЕ копируй сюжет):\n" + "\n".join(lines) + "\n"


FREEFORM_RULES = """Ты — оператор-постановщик и prompt-engineer под t2v (Seedance/VeoFree и Qwen).
Придумай {count} РАЗНООБРАЗНЫХ standalone t2v-промтов (по 5-8с каждый, вертикаль 9:16) под музыкальный клип на этот трек.
Из них {child_count} — развивают КОНЦЕПТ «ребёнок во взрослой жизни»: безликий (силуэт/капюшон/со спины/издалека), но
РАЗНЫЕ моменты взрослой рутины (метро, лифт, эскалатор, очередь, офис, автобус, кухня в 3 ночи, лестница, дождь у окна,
переход, лифтовый холл и т.п.) — единый герой, но разные сцены/оптика/палитра/погода/время.
Остальные {wild_count} — ВАЙЛДКАРД: свободные визуальные идеи/мотивы/стихии под настроение трека (тревога→выгорание→
мрачная решимость, ва-банк), НЕ обязательно ребёнок (объект, что претерпевает изменение; стихия; фактура; абстракция).
Каждый промт — ПО СХЕМЕ-ЦЕПОЧКЕ: субъект+realism-anchor → непрерывное ДЕЙСТВИЕ → конкретный сеттинг/пропсы →
ПОВЕДЕНИЕ одного источника света → камера (оптика mm + крепление + движение + 180-degree shutter) →
ТЕКСТУРА (описывай результат: fine film grain, soft halation, gentle gate weave; НЕ называй сток/формат дословно — иначе
модель рисует плёночную рамку с текстом) → короткий NEGATIVE в конце.
РАМКИ (свобода писателю, минимум): без читаемых ЛИЦ, без читаемого ТЕКСТА/лого/рамок. Палитры/время/оптику варьируй ШИРОКО.
Верни СТРОГО JSON: {{"prompts":[{{"id":<int>,"kind":"child"|"wild","title":"<кратко ру>","gen_prompt":"<english prompt>"}}, ...]}} — ровно {count} штук."""


def build_freeform_prompt(brief, refs, count, child_count):
    c = (brief or {}).get("content", {})
    parts = [
        FREEFORM_RULES.format(count=count, child_count=child_count, wild_count=count - child_count),
        "",
        f"ТРЕК-ВАЙБ: {c.get('core_emotion','')}",
        f"ВИЗУАЛ-МУД (опора, но варьируй): {c.get('visual_mood','')}",
        f"НАРРАТИВ: {c.get('narrative_angle','')}",
        f"ЛИРИКА (для образов): {(brief or {}).get('lyrics','')[:600]}",
        _refs_block(refs),
    ]
    return "\n".join(p for p in parts if p is not None)


def build_prompt(storyboard, brief, refs):
    c = (brief or {}).get("content", {})
    shots_brief = []
    for s in storyboard.get("shots", []):
        shots_brief.append({
            "idx": s.get("idx"),
            "section": s.get("section"),
            "scale": s.get("scale"),
            "motion": s.get("motion"),
            "intent": s.get("intent", ""),
            "imagery_cues": s.get("imagery_cues") or [],
            "hint_query": (s.get("base") or {}).get("query", ""),  # черновой запрос director'а — только как подсказка образа
        })
    parts = [
        SCHEMA_RULES,
        "",
        f"ТРЕК-ВАЙБ: {c.get('core_emotion','')}",
        f"ВИЗУАЛ-МУД (глобальный грейд/свет для ВСЕХ шотов, держи консистентно): {c.get('visual_mood','')}",
        f"ЦЕНТРАЛЬНЫЙ МОТИВ: {storyboard.get('central_motif','')}",
        f"ФОРМАТ: {storyboard.get('format','vertical')} (9:16).",
        _refs_block(refs),
        "ШОТЫ (напиши gen_prompt на КАЖДЫЙ, единый мир/герой сквозь все, различай по intent/scale/motion):",
        json.dumps(shots_brief, ensure_ascii=False, indent=1),
    ]
    return "\n".join(p for p in parts if p is not None)


def _call_groq(prompt):
    if not GROQ_API_KEY:
        return None
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.75, "response_format": {"type": "json_object"}},
            timeout=120,
        )
        if r.status_code == 200:
            print(f"[llm] промты через Groq ({GROQ_MODEL})")
            return r.json()["choices"][0]["message"]["content"].strip()
        print(f"[llm] Groq HTTP {r.status_code} — пробую Gemini")
    except Exception as e:
        print(f"[llm] Groq сеть ({e}) — пробую Gemini")
    return None


def _call_gemini(prompt):
    if not GEMINI_API_KEY:
        return None
    models = [GEMINI_MODEL] + [m for m in ("gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest")
                               if m != GEMINI_MODEL]
    payload = {"contents": [{"parts": [{"text": prompt}]}],
               "generationConfig": {"temperature": 0.75, "response_mime_type": "application/json"}}
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
        for attempt in range(2):
            try:
                r = requests.post(url, json=payload, timeout=120)
            except Exception as e:
                print(f"[llm] Gemini {model}: сеть ({e})"); break
            if r.status_code == 200:
                print(f"[llm] промты через Gemini ({model})")
                return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            if r.status_code in (429, 503) and attempt == 0:
                continue
            print(f"[llm] Gemini {model}: HTTP {r.status_code}"); break
    return None


def _extract_json(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def main():
    ap = argparse.ArgumentParser(description="prompt_writer — мастер-промты на шоты раскадровки ИЛИ N freeform-промтов.")
    ap.add_argument("--storyboard", default=None, help="раскадровка (per-shot режим)")
    ap.add_argument("--brief", default=None)
    ap.add_argument("--references", default=None)
    ap.add_argument("--freeform", action="store_true", help="сгенерить N разнообразных standalone-промтов (без раскадровки)")
    ap.add_argument("--count", type=int, default=20, help="freeform: сколько промтов")
    ap.add_argument("--child-count", type=int, default=13, help="freeform: сколько из них по концепту «ребёнок»")
    ap.add_argument("-o", "--out", default=None)
    args = ap.parse_args()

    brief_ff = yaml.safe_load(Path(args.brief).read_text(encoding="utf-8")) if args.brief and Path(args.brief).exists() else {}
    refs_ff = json.loads(Path(args.references).read_text(encoding="utf-8")) if args.references and Path(args.references).exists() else None

    if args.freeform:
        raw = _call_groq(build_freeform_prompt(brief_ff, refs_ff, args.count, args.child_count)) \
            or _call_gemini(build_freeform_prompt(brief_ff, refs_ff, args.count, args.child_count))
        data = _extract_json(raw)
        if not data or "prompts" not in data:
            sys.exit("[prompt_writer] LLM не дал JSON prompts")
        prompts = data["prompts"]
        for p in prompts:
            p["gen_prompt"] = re.sub(r"[一-鿿]", "", p.get("gen_prompt", "")).strip()
        out = Path(args.out) if args.out else Path("pool_prompts.json")
        out.write_text(json.dumps({"prompts": prompts}, ensure_ascii=False, indent=2), encoding="utf-8")
        kinds = {}
        for p in prompts:
            kinds[p.get("kind", "?")] = kinds.get(p.get("kind", "?"), 0) + 1
        print(f"[prompt_writer freeform] промтов: {len(prompts)} ({kinds}) → {out}")
        return

    if not args.storyboard:
        sys.exit("нужен --storyboard (per-shot) или --freeform")
    sb_path = Path(args.storyboard)
    storyboard = json.loads(sb_path.read_text(encoding="utf-8"))
    brief = yaml.safe_load(Path(args.brief).read_text(encoding="utf-8")) if args.brief and Path(args.brief).exists() else {}
    refs = json.loads(Path(args.references).read_text(encoding="utf-8")) if args.references and Path(args.references).exists() else None

    llm_prompt = build_prompt(storyboard, brief, refs)
    raw = _call_groq(llm_prompt) or _call_gemini(llm_prompt)
    data = _extract_json(raw)
    if not data or "shots" not in data:
        print("[prompt_writer] LLM не дал JSON — fail-open, gen_prompt не проставлены", file=sys.stderr)
        sys.exit(2)

    by_idx = {s.get("idx"): s.get("gen_prompt", "") for s in data["shots"] if s.get("gen_prompt")}
    n = 0
    for shot in storyboard.get("shots", []):
        gp = by_idx.get(shot.get("idx"))
        if gp:
            shot["gen_prompt"] = re.sub(r"[一-鿿]", "", gp).strip()  # чистка случайных CJK
            n += 1
    print(f"[prompt_writer] проставлено gen_prompt: {n}/{len(storyboard.get('shots', []))}")

    out = Path(args.out) if args.out else sb_path
    out.write_text(json.dumps(storyboard, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"storyboard с gen_prompt → {out}")


if __name__ == "__main__":
    main()
