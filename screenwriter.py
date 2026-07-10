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

# библиотека архетипов лежит ВНУТРИ submodule (чтобы была видна на GH Actions),
# не в content_factory/Instrument/... (вне чекаута submodule).
ARCHETYPES_PATH = Path(__file__).resolve().parent / "archetypes"

SYSTEM = """Ты — сценарист-драматург музыкальных клипов для электронного музыканта yaromat
(future garage / downtempo, инструментал). Твоя задача — придать клипу НАСТОЯЩУЮ ДРАМАТУРГИЮ.

ЖЁСТКИЕ ПРАВИЛА:
- Музыка инструментальная, БЕЗ слов. ЛИЦА ЗАПРЕЩЕНЫ, людей не тащить (можно руки/спина/тени).
- «Герой» — это МОТИВ/объект/пространство/стихия, который ПРЕТЕРПЕВАЕТ ИЗМЕНЕНИЕ во времени
  (свеча догорает, туман сквозь голый лес, свет уходит из комнаты, дождь на стекле).
- НЕТ неону; моторика фотографична/органична (реальный футаж), НЕ синтетика/вектор/мультяшность.
- Эмоция = внутренняя глубина, НЕ одиночество; не лепить одинокую фигуру; нет маскам/эзотерике.

АРХЕТИПЫ:
Тебе дана библиотека архетипов (JSON-массив). Каждый архетип — шаблон дуги с beats.
ОБЯЗАТЕЛЬНО выбери ОДИН архетип из библиотеки, который лучше всего подходит треку
(по эмоции, энергии, BPM). Используй beats выбранного архетипа как ШАБЛОН — наполняй
конкретикой трека (мотив, визуальные cue, темп), НЕ копируй текст шаблона.
Если ни один архетип не подходит идеально — бери ближайший и адаптируй.

ДРАМАТУРГИЯ:
- 3-актная дуга по структуре трека: intro=ЗАВЯЗКА (установить мотив/пространство),
  body/drop=ЭСКАЛАЦИЯ+ПЕРЕЛОМ (напряжение, мотив под давлением), climax=КУЛЬМИНАЦИЯ
  (пик/обнажение сути), outro=РАЗВЯЗКА/ПОСЛЕВКУСИЕ (принятие/опустошение/тишина).
- ОДИН центральный метафорический мотив (through-line) из narrative_angle, несём ~50% хронометража.
- Напряжение БЕЗ сюжета-истории: темпом склеек, масштабом (общий→макро), светом, повтором мотива
  и его ПЛАТЕЖОМ в кульминации.

РЕФЕРЕНСЫ (если даны ниже): это реальные живые клипы похожего вайба/BPM с YouTube, уже
деконструированные (хук/ритм/цвет/композиция + почему работает + топ-комментарии зрителей).
Используй их как ОРИЕНТИР структуры — скопируй 50-75% их сюжетно-монтажной логики (что цепляет в
первые секунды, где перелом, как закрывается), но НЕ копируй 1:1 и НЕ бери referenced verdict=="мимо".
Оставшиеся 25-50% — твоя собственная драматургия под ЭТОТ трек и архетип. Если референсов нет —
работай только по архетипу и брифу как раньше.

Верни СТРОГО JSON по схеме:
{
 "archetype_id": "id выбранного архетипа из библиотеки",
 "logline": "одна фраза — драматическая посылка (без персонажей)",
 "central_motif": "сквозной мотив + что он значит",
 "throughline_ratio": 0.5,
 "emotional_curve": "intro→body→climax→outro одной строкой",
 "beats": [ {"section":"intro","intent":"...","visual":"...","imagery_cues":["...","..."],
             "pacing":"slow|medium|fast","scale":"wide|medium|macro"} ],
 "avoid": ["лица","неон","одинокая фигура","синтетика"]
}
beats — по одному на каждую секцию структуры трека, наполнены конкретикой трека на основе
выбранного архетипа. Только JSON, без пояснений."""


def brief_avoid(brief: dict) -> list:
    """Запреты из брифа (constraints.what_to_avoid) — авторитетный список, доезжает в treatment.avoid."""
    av = (brief.get("constraints", {}) or {}).get("what_to_avoid") or []
    return [str(x).strip() for x in av if str(x).strip()]


def _load_archetypes() -> list[dict]:
    """Загрузить библиотеку архетипов."""
    lib_path = ARCHETYPES_PATH / "library.yaml"
    if not lib_path.exists():
        return []
    try:
        import yaml as _yaml
        return _yaml.safe_load(lib_path.read_text(encoding="utf-8")).get("archetypes", [])
    except Exception:
        return []


def _match_archetypes(emotion: str, bpm: float, archetypes: list[dict], top: int = 3) -> list[dict]:
    """Подобрать архетипы по эмоции и BPM (упрощённый matcher без archetype_match.py)."""
    if not archetypes:
        return []
    emotion_lower = emotion.lower().strip() if emotion else ""

    ENERGY_MAP = {"low": (60, 110), "medium": (100, 135), "high": (125, 180)}
    bpm_energy = None
    for e, (lo, hi) in ENERGY_MAP.items():
        if lo <= bpm <= hi:
            bpm_energy = e
            break

    scored = []
    for arch in archetypes:
        score = 0.0
        profile = arch.get("emotional_profile", "").lower()
        for word in emotion_lower.split():
            if word in profile:
                score += 2.0
        if bpm_energy and arch.get("energy"):
            arch_energy = arch["energy"].split("→")[0].strip()
            if arch_energy == bpm_energy:
                score += 1.5
        name_desc = (arch.get("name", "") + " " + arch.get("description", "")).lower()
        for word in emotion_lower.split():
            if len(word) > 3 and word in name_desc:
                score += 0.5
        scored.append({"archetype": arch, "score": round(score, 2)})
    scored.sort(key=lambda x: -x["score"])
    return scored[:top]


def _references_block(references: list[dict] | None) -> str:
    """Сжатая сводка reference_recipes.json (стадия 2 screenplay-pipeline) для промпта.
    Отбрасывает verdict=='мимо' — это референсы, которые деконструктор счёл нерелевантными."""
    if not references:
        return ""
    usable = [r for r in references if r.get("verdict") != "мимо"]
    if not usable:
        return ""
    items = []
    for r in usable[:5]:
        items.append({
            "title": r.get("title", ""),
            "hook": r.get("hook", ""),
            "rhythm": r.get("rhythm", ""),
            "motion": r.get("motion", ""),
            "color": r.get("color", ""),
            "composition": r.get("composition", ""),
            "why_works": r.get("why_works", ""),
            "verdict": r.get("verdict", ""),
            "top_comments": (r.get("comments") or [])[:5],
        })
    return "\n\n=== РЕФЕРЕНСЫ (живые клипы похожего вайба, деконструированы) ===\n" + \
        json.dumps(items, ensure_ascii=False, indent=1)


def build_prompt(brief: dict, references: list[dict] | None = None) -> tuple[str, dict | None]:
    """→ (prompt, selected_archetype_dict | None). references — опционально, из reference_recipes.json."""
    t = brief.get("track", {})
    c = brief.get("content", {})
    s = brief.get("structure", {})
    av = brief_avoid(brief)
    emotion = c.get("core_emotion", "")
    bpm = float(t.get("bpm", 120))

    archetypes = _load_archetypes()
    matches = _match_archetypes(emotion, bpm, archetypes)
    selected = matches[0]["archetype"] if matches else None

    arch_block = ""
    if matches:
        arch_list = []
        for m in matches[:3]:
            a = m["archetype"]
            arch_list.append({
                "id": a["id"], "name": a.get("name", ""),
                "emotional_profile": a.get("emotional_profile", ""),
                "energy": a.get("energy", ""),
                "central_motif_pattern": a.get("central_motif_pattern", ""),
                "hero_object": a.get("hero_object", ""),
                "beats": a.get("beats", []),
            })
        arch_block = "\n\n=== БИБЛИОТЕКА АРХЕТИПОВ (выбери лучший) ===\n" + json.dumps(arch_list, ensure_ascii=False, indent=1)

    prompt = (
        SYSTEM
        + arch_block
        + _references_block(references)
        + "\n\n=== БРИФ ТРЕКА ===\n"
        + f"Название: {t.get('title')}\nBPM: {t.get('bpm')} | тональность: {t.get('key')} | "
        + f"длительность: {t.get('duration')}с\n"
        + f"Структура: {json.dumps(s, ensure_ascii=False)}\n"
        + f"Эмоция: {c.get('core_emotion')}\n"
        + f"Визуальный настрой: {c.get('visual_mood')}\n"
        + f"Нарратив/смысл: {c.get('narrative_angle')}\n"
        + f"Слова-настроения: {c.get('mood_words')}\n"
        + f"Жанр: {brief.get('production',{}).get('genre')}\n"
        + (f"ЗАПРЕЩЕНО брифом (ОБЯЗАТЕЛЬНО в avoid, не выдавать такие cue): {av}\n" if av else "")
    )
    return prompt, selected


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


def generate_treatment(brief: dict, references: list[dict] | None = None) -> dict:
    prompt, selected_arch = build_prompt(brief, references)
    raw = _call_groq(prompt) or _call_gemini(prompt)
    if not raw:
        sys.exit("Ни Groq, ни Gemini не ответили (проверь ключи/гео).")
    try:
        treatment = json.loads(raw)
    except json.JSONDecodeError:
        raw2 = raw.strip().lstrip("`").replace("json", "", 1).strip().rstrip("`")
        treatment = json.loads(raw2)
    missing = REQUIRED_KEYS - treatment.keys()
    if missing:
        sys.exit(f"LLM вернул неполный treatment, нет ключей: {missing}")
    if not isinstance(treatment.get("beats"), list) or not treatment["beats"]:
        sys.exit("treatment.beats пуст или не список")
    # архетип: LLM выбрал свой или мы подсказали
    if not treatment.get("archetype_id") and selected_arch:
        treatment["archetype_id"] = selected_arch["id"]
        treatment["archetype_name"] = selected_arch.get("name", "")
    elif treatment.get("archetype_id") and not treatment.get("archetype_name"):
        for m in _match_archetypes("", 0, _load_archetypes()):
            if m["archetype"]["id"] == treatment["archetype_id"]:
                treatment["archetype_name"] = m["archetype"].get("name", "")
                break
    # брифовые запреты — авторитетны: доезжают в treatment.avoid (дальше режиссёр их соблюдает)
    av = treatment.get("avoid") or []
    if not isinstance(av, list):
        av = []
    for x in brief_avoid(brief):
        if x not in av:
            av.append(x)
    treatment["avoid"] = av
    return treatment


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("brief", help="путь к brief_full.yaml")
    ap.add_argument("-o", "--out", help="куда писать treatment.json (default: рядом с брифом)")
    ap.add_argument("--references", help="путь к reference_recipes.json (стадия 2, опционально)")
    ap.add_argument("--print", action="store_true", dest="to_stdout", help="только stdout")
    args = ap.parse_args()

    brief_path = Path(args.brief)
    brief = yaml.safe_load(brief_path.read_text(encoding="utf-8"))
    references = None
    if args.references:
        rp = Path(args.references)
        if rp.exists():
            try:
                references = json.loads(rp.read_text(encoding="utf-8"))
                n = len(references) if isinstance(references, list) else "?"
                print(f"[refs] загружено {n} референсов из {rp.name}", file=sys.stderr)
            except Exception as e:
                print(f"[refs] WARN: не смог прочитать {rp} ({e}) — работаю без референсов", file=sys.stderr)
                references = None
        else:
            print(f"[refs] WARN: {rp} нет — работаю без референсов", file=sys.stderr)
    treatment = generate_treatment(brief, references)

    out_json = json.dumps(treatment, ensure_ascii=False, indent=2)
    print("\n=== TREATMENT ===")
    print(out_json)

    if not args.to_stdout:
        out = Path(args.out) if args.out else brief_path.with_name("treatment.json")
        out.write_text(out_json, encoding="utf-8")
        print(f"\n✅ treatment → {out}")


if __name__ == "__main__":
    main()
