#!/usr/bin/env python3
"""
style_judge.py — Фаза 1 агента-разнообразия: mimo как ГЛАЗ+СУДЬЯ цвето-кандидатов Скаута.

Запускается ПОСЛЕ style_scout.py (в style_scout.yml). Берёт контакт-лист (сетка грейдов
на нашем футаже) + кандидатов из style_proposals.json + бренд-рубрику → зовёт mimo (зрение) →
пишет style_judge.json (вердикт/скор/причина на каждый лук) → постит рекомендацию в TG-тред 634.

ВАЖНО: judge НЕ мерджит в прод. styles.json не трогается. Последнее слово — за yaromat
(он запускает style_scout_merge.py по рекомендации). Best-effort: не валит воркфлоу.

Env: MIMO_BIN (путь к mimo), CLOUDFLARE_WORKER/TELEGRAM_BOT_TOKEN/STYLE_SCOUT_CHAT_ID/
     STYLE_SCOUT_THREAD_ID (TG). Контакт-лист ищется в /tmp/style_scout/style_scout_*.jpg.
"""
import os, sys, json, re, glob, subprocess
from pathlib import Path
from datetime import datetime

HERE = Path(__file__).resolve().parent
PROPOSALS = HERE / "style_proposals.json"
JUDGE_OUT = HERE / "style_judge.json"
MIMO = os.environ.get("MIMO_BIN", os.path.expanduser("~/.mimocode/bin/mimo"))
SHEET_GLOB = "/tmp/style_scout/style_scout_*.jpg"

# Бренд-рубрика yaromat (Future Garage / downtempo). Жёсткие правила из памяти проекта.
RUBRIC = (
    "Артист yaromat: Future Garage / downtempo. Эстетика — кинематографичная, приглушённая, "
    "мрачноватая ВНУТРЕННЯЯ ГЛУБИНА (не уныние, не одиночество). ЖЁСТКО: НИКАКОГО неона; "
    "не кричащая насыщенность; не плоско-выцветшее; без грубого цветного каста «как дешёвый фильтр»; "
    "грейд должен ощущаться дорого и цельно под даунтемпо-вайб. "
    "ОТБРАКОВЫВАЙ кадры с видимым текстом/надписями/вотермарками/логотипами — не наш визуал."
)


def strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s)


def extract_json(text: str) -> dict | None:
    """Достаёт первый сбалансированный {...} из вывода mimo."""
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
        "Для КАЖДОГО кандидата реши: keep=true (попадает в бренд, стоит добавить в ротацию) "
        "или keep=false. Дай score 0-10 и причину <=12 слов.\n"
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
                                     data=urllib.parse.urlencode(data).encode())
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
    print(f"[judge] сетка={sheet} | кандидатов={len(names)} | mimo={MIMO}")

    if not Path(MIMO).exists():
        print(f"[judge] mimo не найден ({MIMO}) — пропуск"); return

    # mimo: message ПЕРВЫМ, -f в КОНЦЕ, stdin закрыт (иначе виснет)
    cmd = [MIMO, "run", "--pure", "--dangerously-skip-permissions", build_prompt(names), "-f", sheet]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=420)
    except Exception as e:
        print(f"[judge] mimo упал: {e}")
        tg_text(f"Style Scout · судья mimo недоступен ({e}). Кандидаты в style_proposals.json — реши вручную.")
        return

    verdict = extract_json(r.stdout)
    if not verdict or "verdicts" not in verdict:
        print("[judge] не распарсил JSON от mimo. stdout(хвост):")
        print(strip_ansi(r.stdout)[-600:])
        tg_text("Style Scout · судья mimo вернул неразборчивый ответ — реши кандидатов вручную.")
        return

    ts = datetime.now().strftime("%Y-%m-%d")
    verdict["_generated"] = ts
    verdict["_judge"] = "mimo-auto"
    JUDGE_OUT.write_text(json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[judge] → {JUDGE_OUT}")

    keep = verdict.get("recommend_merge") or [v["name"] for v in verdict["verdicts"] if v.get("keep")]
    lines = []
    for v in verdict["verdicts"]:
        mark = "✅" if v.get("keep") else "✖"
        lines.append(f"{mark} {v['name']} ({v.get('score','?')}/10) — {v.get('reason','')}")
    cmd_hint = ("python3 style_scout_merge.py --names " + " ".join(keep)) if keep else "(mimo ничего не рекомендует)"
    msg = ("Style Scout · судья mimo:\n" + "\n".join(lines) +
           f"\n\nРекомендую влить: {', '.join(keep) if keep else '—'}\n"
           f"Твой мердж (последнее слово за тобой):\n{cmd_hint}")
    tg_text(msg)
    print("[judge] готово")


if __name__ == "__main__":
    main()
