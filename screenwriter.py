#!/usr/bin/env python3
"""
screenwriter.py — агент-сценарист: бриф трека → драматургический treatment.json.

Превращает трек в произведение с дугой (не энергонарезку): пишет логлайн, центральный
мотив-метафору и beats по структуре (intro/body/climax/outro). Драма БЕЗ персонажей/лиц —
«герой» это мотив/объект/пространство, претерпевающий изменение под музыкальную структуру.
Методология: skills/craft/screenwriter/SKILL.md.

Free-LLM: Groq → Gemini фолбэк (как scout-агенты; локально из РФ Groq может 403 → Gemini).
Лёгкий, без тяжёлых зависимостей.

Usage:
  python3 screenwriter.py brief_full.yaml                 # → treatment.json рядом
  python3 screenwriter.py brief_full.yaml -o out.json     # явный выход
  python3 screenwriter.py brief_full.yaml --print         # только в stdout
"""

import argparse
import json
import os
import sys
from pathlib import Path

import requests
import yaml
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except Exception:
    pass

GROQ_API_KEY  = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL    = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL   = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

REQUIRED_KEYS = {"logline", "central_motif", "emotional_curve", "beats", "avoid"}

SYSTEM = """Ты — сценарист-драматург музыкальных клипов для электронного музыканта yaromat
(future garage / downtempo, инструментал). Твоя задача — придать клипу НАСТОЯЩУЮ ДРАМАТУРГИЮ.

ЖЁСТКИЕ ПРАВИЛА:
- Музыка инструментальная, БЕЗ слов. ЛИЦА ЗАПРЕЩЕНЫ, людей не тащить (можно руки/спина/тени).
- «Герой» — это МОТИВ/объект/пространство/стихия, который ПРЕТЕРПЕВАЕТ ИЗМЕНЕНИЕ во времени
  (свеча догорает, туман сквозь голый лес, свет уходит из комнаты, дождь на стекле).
- НЕТ неону; моторика фотографична/органична (реальный футаж), НЕ синтетика/вектор/мультяшность.
- Эмоция = внутренняя глубина, НЕ одиночество; не лепить одинокую фигуру; нет маскам/эзотерике.

ДРАМАТУРГИЯ:
- 3-актная дуга по структуре трека: intro=ЗАВЯЗКА (установить мотив/пространство),
  body/drop=ЭСКАЛАЦИЯ+ПЕРЕЛОМ (напряжение, мотив под давлением), climax=КУЛЬМИНАЦИЯ
  (пик/обнажение сути), outro=РАЗВЯЗКА/ПОСЛЕВКУСИЕ (принятие/опустошение/тишина).
- ОДИН центральный метафорический мотив (through-line) из narrative_angle, несём ~50% хронометража.
- Напряжение БЕЗ сюжета-истории: темпом склеек, масштабом (общий→макро), светом, повтором мотива
  и его ПЛАТЕЖОМ в кульминации.

Верни СТРОГО JSON по схеме:
{
 "logline": "одна фраза — драматическая посылка (без персонажей)",
 "central_motif": "сквозной мотив + что он значит",
 "throughline_ratio": 0.5,
 "emotional_curve": "intro→body→climax→outro одной строкой",
 "beats": [ {"section":"intro","intent":"...","visual":"...","imagery_cues":["...","..."],
             "pacing":"slow|medium|fast","scale":"wide|medium|macro"} ],
 "avoid": ["лица","неон","одинокая фигура","синтетика"]
}
beats — по одному на каждую секцию структуры трека. Только JSON, без пояснений."""


def build_prompt(brief: dict) -> str:
    t = brief.get("track", {})
    c = brief.get("content", {})
    s = brief.get("structure", {})
    return (
        SYSTEM
        + "\n\n=== БРИФ ТРЕКА ===\n"
        + f"Название: {t.get('title')}\nBPM: {t.get('bpm')} | тональность: {t.get('key')} | "
        + f"длительность: {t.get('duration')}с\n"
        + f"Структура: {json.dumps(s, ensure_ascii=False)}\n"
        + f"Эмоция: {c.get('core_emotion')}\n"
        + f"Визуальный настрой: {c.get('visual_mood')}\n"
        + f"Нарратив/смысл: {c.get('narrative_angle')}\n"
        + f"Слова-настроения: {c.get('mood_words')}\n"
        + f"Жанр: {brief.get('production',{}).get('genre')}\n"
    )


def _call_groq(prompt: str) -> str | None:
    if not GROQ_API_KEY:
        return None
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.7, "response_format": {"type": "json_object"}},
            timeout=90,
        )
        if r.status_code == 200:
            print(f"[llm] сценарий через Groq ({GROQ_MODEL})")
            return r.json()["choices"][0]["message"]["content"].strip()
        print(f"[llm] Groq HTTP {r.status_code} — пробую Gemini")
    except Exception as e:
        print(f"[llm] Groq сеть ({e}) — пробую Gemini")
    return None


def _call_gemini(prompt: str) -> str | None:
    if not GEMINI_API_KEY:
        return None
    models = [GEMINI_MODEL] + [m for m in ("gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest")
                               if m != GEMINI_MODEL]
    payload = {"contents": [{"parts": [{"text": prompt}]}],
               "generationConfig": {"temperature": 0.7, "response_mime_type": "application/json"}}
    for model in models:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent?key={GEMINI_API_KEY}")
        for attempt in range(2):
            try:
                r = requests.post(url, json=payload, timeout=90)
            except Exception as e:
                print(f"[llm] Gemini {model}: сеть ({e})"); break
            if r.status_code == 200:
                print(f"[llm] сценарий через Gemini ({model})")
                return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            if r.status_code in (429, 503) and attempt == 0:
                continue
            print(f"[llm] Gemini {model}: HTTP {r.status_code}"); break
    return None


def generate_treatment(brief: dict) -> dict:
    prompt = build_prompt(brief)
    raw = _call_groq(prompt) or _call_gemini(prompt)
    if not raw:
        sys.exit("Ни Groq, ни Gemini не ответили (проверь ключи/гео).")
    try:
        treatment = json.loads(raw)
    except json.JSONDecodeError:
        # вырезаем возможную обёртку ```json ... ```
        raw2 = raw.strip().lstrip("`").replace("json", "", 1).strip().rstrip("`")
        treatment = json.loads(raw2)
    missing = REQUIRED_KEYS - treatment.keys()
    if missing:
        sys.exit(f"LLM вернул неполный treatment, нет ключей: {missing}")
    if not isinstance(treatment.get("beats"), list) or not treatment["beats"]:
        sys.exit("treatment.beats пуст или не список")
    return treatment


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("brief", help="путь к brief_full.yaml")
    ap.add_argument("-o", "--out", help="куда писать treatment.json (default: рядом с брифом)")
    ap.add_argument("--print", action="store_true", dest="to_stdout", help="только stdout")
    args = ap.parse_args()

    brief_path = Path(args.brief)
    brief = yaml.safe_load(brief_path.read_text(encoding="utf-8"))
    treatment = generate_treatment(brief)

    out_json = json.dumps(treatment, ensure_ascii=False, indent=2)
    print("\n=== TREATMENT ===")
    print(out_json)

    if not args.to_stdout:
        out = Path(args.out) if args.out else brief_path.with_name("treatment.json")
        out.write_text(out_json, encoding="utf-8")
        print(f"\n✅ treatment → {out}")


if __name__ == "__main__":
    main()
