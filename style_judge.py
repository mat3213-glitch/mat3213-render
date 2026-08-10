#!/usr/bin/env python3
"""
style_judge.py — Фаза 1 агента-разнообразия: ГЛАЗ+СУДЬЯ цвето-кандидатов Скаута.

Запускается ПОСЛЕ style_scout.py (в style_scout.yml). Берёт контакт-лист (сетка грейдов
на нашем футаже) + кандидатов из style_proposals.json + бренд-рубрику → зовёт vision-ансамбль →
пишет style_judge.json (вердикт/скор/причина на каждый лук) → постит рекомендацию в TG-тред 634.

Транспорт — живой CF Workers AI + OpenRouter ансамбль из
`art_judge.ask_vision`. Mimo и GitHub Models удалены.

ВАЖНО: judge НЕ мерджит в прод. styles.json не трогается. Последнее слово — за yaromat
(он запускает style_scout_merge.py по рекомендации). Best-effort: не валит воркфлоу.

Env: OPENROUTER_API_KEY, CLOUDFLARE_WORKER/WORKER_SECRET/TELEGRAM_BOT_TOKEN/STYLE_SCOUT_CHAT_ID/
     STYLE_SCOUT_THREAD_ID (TG). Контакт-лист ищется в /tmp/style_scout/style_scout_*.jpg.
"""
import os, sys, json, re, glob, base64, time
from collections import defaultdict
from pathlib import Path
from datetime import datetime

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from art_judge import MODELS, ask_vision   # общий транспорт и ансамбль

PROPOSALS = HERE / "style_proposals.json"
JUDGE_OUT = HERE / "style_judge.json"
SHEET_GLOB = "/tmp/style_scout/style_scout_*.jpg"

# Бренд-рубрика yaromat (Future Garage / downtempo). Жёсткие правила из памяти проекта.
#
# 🔴 ПАЛИТРА ВПИСАНА 2026-07-29 по указанию yaromat. До этого рубрика знала только «приглушённо,
# без неона» и НЕ знала про deep teal — из-за чего судья зарубил кандидата за «слишком холодный
# тон», хотя холод и есть наш фирменный цвет, утверждённый владельцем («цвет шикарный, и он один
# и тот же на всех артах», 16.07). Замер: замок палитры даёт hue 178-180°, цвет НЕ ЧИНИТСЯ по
# своей инициативе. Источник значений — DESIGN.md + [[reference_palette_lock]].
RUBRIC = (
    "Артист yaromat: Future Garage / downtempo. Эстетика — кинематографичная, приглушённая, "
    "мрачноватая ВНУТРЕННЯЯ ГЛУБИНА (не уныние, не одиночество).\n"
    "ЦЕЛЕВАЯ ПАЛИТРА (утверждена владельцем, это ЭТАЛОН, а не недостаток): холодный "
    "сине-зелёный «deep teal» (#1F4E4A, оттенок ~178°), тени уходят в холодный чёрно-зелёный "
    "(#0E2B2A), дымка — холодный серый (#8C9A9B), световой акцент мягкий не-белый (#D7DEDA). "
    "Свет — ОДИН источник холодного контрового света, холодный нуар.\n"
    "⚠️ ХОЛОДНЫЙ ТОН И БИРЮЗОВО-ЗЕЛЁНЫЙ СДВИГ — ЭТО НАШ ФИРМЕННЫЙ ЦВЕТ. НЕ отбраковывай "
    "кандидата за то, что он холодный, бирюзовый или зеленоватый: чем ближе к deep teal, тем "
    "ЛУЧШЕ. Тёплый оранжево-янтарный грейд, наоборот, от бренда ДАЛЬШЕ.\n"
    "ЖЁСТКО ОТБРАКОВЫВАЙ: неон и кислотные пурпурно-розовые свечения; кричащую насыщенность; "
    "плоско-выцветшее; ГРЯЗНЫЙ каст «как дешёвый фильтр» — болотно-жёлтую или салатовую зелень "
    "без синевы, розово-зелёный сдвиг кожи, тонировку, ломающую чёрный. "
    "Отличай ЧИСТЫЙ холодный teal (наш) от грязного жёлто-зелёного каста (брак).\n"
    "Грейд должен ощущаться дорого и цельно под даунтемпо-вайб. "
    "ОТБРАКОВЫВАЙ кадры с видимым текстом/надписями/вотермарками/логотипами — не наш визуал."
)


def strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s)


def extract_json(text: str) -> dict | None:
    """Достаёт первый сбалансированный {...} (оставлено: пригодится на сыром ответе)."""
    s = strip_ansi(text)
    start = s.find("{")
    if start < 0:
        return None
    depth, instr, esc = 0, False, False
    for i in range(start, len(s)):
        c = s[i]
        if instr:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                instr = False
        else:
            if c == '"':
                instr = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start:i + 1])
                    except Exception:
                        return None
    return None


def build_prompt(names: list[str]) -> str:
    order = ", ".join(f"{i+1}={n}" for i, n in enumerate(names))
    return (
        "Ты арт-директор. На прикреплённом контакт-листе — НАШ кадр под несколькими "
        f"цвето-грейдами (кандидаты), слева-направо сверху-вниз в порядке: {order}. "
        "Каждый тайл подписан именем грейда.\n\n"
        f"{RUBRIC}\n\n"
        "⚠️ СУДИ ТОЛЬКО ТЕ ТАЙЛЫ, КОТОРЫЕ РЕАЛЬНО ВИДИШЬ НА КАРТИНКЕ. Если имени из списка на "
        "контакт-листе НЕТ — НЕ включай его в ответ и НЕ придумывай ему вердикт: тайл мог не "
        "отрисоваться. Лучше вернуть меньше вердиктов, чем выдуманные.\n"
        "Для КАЖДОГО увиденного кандидата реши: keep=true (попадает в бренд, стоит добавить "
        "в ротацию) или keep=false. Дай score 0-10 и причину <=12 слов ПО-РУССКИ "
        "(вердикт читает владелец, английский не годится).\n"
        "Ответь СТРОГО одним JSON-объектом без пояснений и без markdown:\n"
        '{"verdicts":[{"name":"<имя>","keep":true,"score":7,"reason":"<кратко>"}],'
        '"recommend_merge":["<имена с keep=true>"]}'
    )


def tg_text(msg: str):
    import urllib.request, urllib.parse
    worker = os.environ.get("CLOUDFLARE_WORKER"); token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("STYLE_SCOUT_CHAT_ID"); thread = os.environ.get("STYLE_SCOUT_THREAD_ID")
    if not (worker and token and chat):
        print("  [tg] нет секретов — пропуск"); return
    data = {"chat_id": chat, "text": msg[:3500]}
    if thread:
        data["message_thread_id"] = str(int(thread))
    try:
        req = urllib.request.Request(f"{worker}/bot{token}/sendMessage",
                                     data=urllib.parse.urlencode(data).encode(),
                                     headers={"User-Agent": "curl/8.5.0"})  # CF режет дефолтный urllib UA → 1010
        urllib.request.urlopen(req, timeout=60).read()
        print("  [tg] sendMessage ok")
    except Exception as e:
        print(f"  [tg] send fail: {e}")


def main():
    if not PROPOSALS.exists():
        print("[judge] нет style_proposals.json — нечего судить"); return
    cands = json.loads(PROPOSALS.read_text(encoding="utf-8")).get("candidates", [])
    if not cands:
        print("[judge] пустой список кандидатов"); return
    names = [c["name"] for c in cands]

    sheets = sorted(glob.glob(SHEET_GLOB))
    if not sheets:
        print(f"[judge] контакт-лист не найден ({SHEET_GLOB}) — пропуск"); return
    sheet = sheets[-1]
    print(f"[judge] сетка={sheet} | кандидатов={len(names)} | ансамбль={len(MODELS)}")
    if not (os.environ.get("OPENROUTER_API_KEY") or os.environ.get("CLOUDFLARE_WORKER")):
        print("[judge] нет ни одного vision-транспорта — пропуск")
        tg_text("Style Scout · vision-транспорты не настроены. Кандидаты реши вручную.")
        return

    prompt = build_prompt(names)
    b64 = base64.b64encode(Path(sheet).read_bytes()).decode()
    per_name = defaultdict(lambda: {"keep": 0, "scores": [], "reasons": []})
    n_ok = 0
    for m in MODELS:
        v, err = ask_vision(m, prompt, b64)
        if not v or "verdicts" not in v:
            print(f"  ✗ {m.split('/')[-1]}: {err or 'нет verdicts'}")
            continue
        n_ok += 1
        for item in v["verdicts"]:
            nm = str(item.get("name", "")).strip()
            if nm not in names:
                continue          # модель выдумала имя — не наш кандидат
            rec = per_name[nm]
            if item.get("keep"):
                rec["keep"] += 1
            try:
                rec["scores"].append(float(item.get("score", 0)))
            except Exception:
                pass
            if item.get("reason"):
                rec["reasons"].append(str(item["reason"]))
        print(f"  ✓ {m.split('/')[-1]}: {len(v['verdicts'])} вердиктов")
        time.sleep(1)

    if not n_ok:
        print("[judge] ансамбль не ответил")
        tg_text("Style Scout · vision-ансамбль не ответил — реши кандидатов вручную.")
        return

    need = n_ok // 2 + 1          # keep — большинством, как в art_judge по бренду
    verdicts = []
    for nm in names:
        rec = per_name.get(nm)
        if not rec:
            # ни одна модель не увидела этот тайл — скорее всего он не отрисовался
            verdicts.append({"name": nm, "keep": False, "score": 0, "votes": f"0/{n_ok}",
                             "status": "не виден на листе", "reason": "тайл не отрисован?"})
            continue
        seen = len(rec["scores"]) or rec["keep"]
        score = round(sum(rec["scores"]) / len(rec["scores"]), 1) if rec["scores"] else 0
        keep = rec["keep"] >= need
        # при n_ok=2 «большинство» вырождается в ЕДИНОГЛАСИЕ, и кандидат со скором 8/10 молча
        # выпадает из-за одного расхождения. Такие не прячем — помечаем спорными для yaromat.
        status = "принят" if keep else ("спорный" if rec["keep"] > 0 else "отклонён")
        verdicts.append({"name": nm, "keep": keep, "score": score,
                         "votes": f"{rec['keep']}/{n_ok}", "status": status,
                         "reason": (rec["reasons"][0] if rec["reasons"] else "")[:80]})
    verdict = {"verdicts": verdicts,
               "recommend_merge": [v["name"] for v in verdicts if v["keep"]],
               "disputed": [v["name"] for v in verdicts if v.get("status") == "спорный"]}
    missing = [v["name"] for v in verdicts if v.get("status") == "не виден на листе"]
    if missing:
        print(f"[judge] ⚠️ не увидены на контакт-листе: {', '.join(missing)} "
              f"(проверь, что грейд применился — у кандидата мог быть balance=None)")

    ts = datetime.now().strftime("%Y-%m-%d")
    verdict["_generated"] = ts
    verdict["_judge"] = f"gh-vision-ensemble({n_ok}/{len(MODELS)})"
    JUDGE_OUT.write_text(json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[judge] → {JUDGE_OUT}")

    keep = verdict.get("recommend_merge") or [v["name"] for v in verdict["verdicts"] if v.get("keep")]
    MARK = {"принят": "✅", "спорный": "🟡", "отклонён": "✖", "не виден на листе": "⚠️"}
    lines = []
    for v in verdict["verdicts"]:
        mark = MARK.get(v.get("status"), "✖")
        lines.append(f"{mark} {v['name']} ({v.get('score','?')}/10, голоса {v.get('votes','?')}) — {v.get('reason','')}")
    cmd_hint = ("python3 style_scout_merge.py --names " + " ".join(keep)) if keep else "(судья ничего не рекомендует)"
    disputed = verdict.get("disputed") or []
    msg = ("Style Scout · судья (vision-ансамбль):\n" + "\n".join(lines) +
           f"\n\nРекомендую влить: {', '.join(keep) if keep else '—'}")
    if disputed:
        msg += (f"\n🟡 Спорные (голоса разделились, посмотри сам): {', '.join(disputed)}")
    msg += f"\nТвой мердж (последнее слово за тобой):\n{cmd_hint}"
    tg_text(msg)
    print("[judge] готово")


if __name__ == "__main__":
    main()
