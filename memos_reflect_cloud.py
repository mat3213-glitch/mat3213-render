#!/usr/bin/env python3
"""
memos_reflect_cloud.py — облачная рефлексия + оптимизатор (GH Actions, бук-независимо).

Самодостаточный (stdlib + rclone + GH Models по умолчному токену с permissions: models:read).
Запускается по cron на GH Actions. Анализирует БЕЗ бука:
  • GitHub API — ВСЕ раны репо (новые воркфлоу подхватываются сами, без хардкод-списка);
  • ЯД memory_os_feed/ — em-ит рендеров/скриптов (исход/ошибки/инсайты);
  • ЯД AUDIT/CHECKPOINT.md — последнее состояние сессии (запросы юзера + действия Claude).

Мозг — GitHub Models. Два прохода: reflect (ошибки акторов + точность задач юзера) и
optimize (конкретные предложения: fix/optimize/simplify). Результат:
  • ЯД AUDIT/REFLECTION.md + AUDIT/PROPOSALS.md (человекочитаемо);
  • findings-ошибки как записи в ЯД-фид (бук ingest вольёт в локальную БД);
  • дайджест в TG-тред 1007 (🧠 MEMORY OPS).
"""
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

YD = "ydrive:Content factory"
YD_FEED = f"{YD}/memory_os_feed"
YD_AUDIT = f"{YD}/AUDIT"
GH_URL = "https://models.github.ai/inference/chat/completions"
GH_MODELS = ("openai/gpt-4o-mini", "meta/llama-3.3-70b-instruct")
REPO = os.environ.get("GITHUB_REPOSITORY", "mat3213-glitch/mat3213-render")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
TG_WORKER = os.environ.get("CLOUDFLARE_WORKER", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("SCOUT_CHAT_ID", "")
TG_THREAD = "1007"


def log(m): print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def rclone(*a, timeout=120):
    return subprocess.run(["rclone"] + list(a), capture_output=True, text=True, timeout=timeout)


def _tg_one(text: str):
    data = urllib.parse.urlencode({"chat_id": TG_CHAT, "message_thread_id": TG_THREAD,
                                   "text": text, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(f"{TG_WORKER}/bot{TG_TOKEN}/sendMessage", data=data,
                                 headers={"User-Agent": "curl/8.5.0"})
    urllib.request.urlopen(req, timeout=30)


def tg(text: str, chunk: int = 3800):
    """TG-лимит 4096 → длинное РАЗБИВАЕМ по строкам, не обрезаем (грабля №8)."""
    if not (TG_WORKER and TG_TOKEN and TG_CHAT):
        log("TG: секреты не заданы"); return
    parts, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > chunk:
            if buf:
                parts.append(buf)
            while len(line) > chunk:
                parts.append(line[:chunk]); line = line[chunk:]
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        parts.append(buf)
    for i, p in enumerate(parts or [text]):
        try:
            _tg_one(p + (f"\n<i>…({i+1}/{len(parts)})</i>" if len(parts) > 1 else ""))
        except Exception as e:
            log(f"TG err: {e}")


# ── сбор контекста ───────────────────────────────────────────────────────────
def gh_runs() -> list:
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{REPO}/actions/runs?per_page=30",
            headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
                     "User-Agent": "curl/8.5.0"})
        with urllib.request.urlopen(req, timeout=25) as r:
            runs = json.loads(r.read()).get("workflow_runs", [])
        return [f"{w['name']}: {w.get('conclusion') or w['status']}" for w in runs]
    except Exception as e:
        log(f"gh_runs err: {e}"); return []


def pull_feed() -> list:
    tmp = Path("/tmp/feed"); tmp.mkdir(exist_ok=True)
    r = rclone("lsf", "--files-only", YD_FEED, timeout=60)
    if r.returncode != 0:
        return []
    recs = []
    for fn in [x.strip() for x in r.stdout.splitlines() if x.strip().endswith(".jsonl")]:
        if rclone("copyto", f"{YD_FEED}/{fn}", str(tmp / fn), timeout=60).returncode != 0:
            continue
        for line in (tmp / fn).read_text(errors="ignore").splitlines():
            try:
                recs.append(json.loads(line))
            except Exception:
                pass
        rclone("moveto", f"{YD_FEED}/{fn}", f"{YD_FEED}/done/{fn}", timeout=60)
    return recs


def pull_checkpoint() -> str:
    if rclone("copyto", f"{YD_AUDIT}/CHECKPOINT.md", "/tmp/CHECKPOINT.md", timeout=60).returncode == 0:
        return Path("/tmp/CHECKPOINT.md").read_text(errors="ignore")[:5000]
    return ""


def ask(system: str, user: str, max_tokens=1500):
    if not TOKEN:
        return None
    for model in GH_MODELS:
        try:
            body = json.dumps({"model": model, "max_tokens": max_tokens, "temperature": 0.4,
                               "messages": [{"role": "system", "content": system},
                                            {"role": "user", "content": user}]}).encode()
            req = urllib.request.Request(GH_URL, data=body,
                                         headers={"Authorization": f"Bearer {TOKEN}",
                                                  "Content-Type": "application/json",
                                                  "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=90) as r:
                txt = json.loads(r.read())["choices"][0]["message"]["content"]
            if txt and txt.strip():
                t = txt.strip()
                if t.startswith("```"):
                    t = t.split("```")[1].lstrip("json").strip()
                a, b = t.find("["), t.rfind("]")
                a2, b2 = t.find("{"), t.rfind("}")
                for lo, hi in ((a, b), (a2, b2)):
                    if lo >= 0 and hi > lo:
                        try:
                            return json.loads(t[lo:hi + 1])
                        except Exception:
                            pass
        except Exception as e:
            log(f"models {model} err: {e}")
    return None


REFLECT_SYS = (
    "Ты — облачный слой рефлексии фабрики контента. Акторы: claude, mimo, gh-ai, api, user. "
    "Найди ошибки/трения/потери и как избежать/исправить. Для user оцени точность постановки задач. "
    "СТРОГО JSON: {\"errors\":[{\"actor\",\"what\",\"root_cause\",\"how_avoid\",\"how_fix\",\"severity\"}],"
    "\"interactions\":[{\"precision\":0,\"ambiguities\",\"suggestion\"}],\"summary\":\"\"}"
)
OPTIMIZE_SYS = (
    "Ты — оптимизатор фабрики (Claude-дирижёр + бесплатные mimo/GH Models, рендеры на GH, "
    "железо Atom/1.8GB). По ошибкам/инсайтам предложи КОНКРЕТНЫЕ скриптовые улучшения для "
    "исключения ошибки или буста (меньше токенов, проще, быстрее). СТРОГО JSON-массив: "
    "[{\"type\":\"fix|optimize|simplify|automate\",\"target\",\"proposal\",\"benefit\",\"effort\"}]"
)


def main():
    runs = gh_runs()
    feed = pull_feed()
    checkpoint = pull_checkpoint()

    feed_txt = "\n".join(f"- [{r.get('kind')}] {r.get('actor')}/{r.get('action')} "
                         f"{r.get('status')}: {r.get('detail','')}" for r in feed[-40:]) or "(пусто)"
    ctx = (f"=== GH RUNS (все воркфлоу репо) ===\n" + "\n".join(f"- {x}" for x in runs) +
           f"\n\n=== ФИД emit (рендеры/скрипты) ===\n{feed_txt}"
           f"\n\n=== CHECKPOINT сессии ===\n{checkpoint}")

    reflection = ask(REFLECT_SYS, ctx) or {}
    errors = reflection.get("errors", []) if isinstance(reflection, dict) else []
    inter = reflection.get("interactions", []) if isinstance(reflection, dict) else []

    opt_ctx = "ОШИБКИ:\n" + "\n".join(
        f"- [{e.get('severity')}] {e.get('actor')}: {e.get('what')} | фикс: {e.get('how_fix')}"
        for e in errors) + f"\n\nФИД:\n{feed_txt}"
    proposals = ask(OPTIMIZE_SYS, opt_ctx) or []
    if isinstance(proposals, dict):
        proposals = proposals.get("proposals") or proposals.get("items") or []

    ts = time.strftime("%Y-%m-%d %H:%M UTC")
    # REFLECTION.md
    rmd = [f"# REFLECTION (cloud) — {ts}", "", f"**Сводка:** {reflection.get('summary','') if isinstance(reflection,dict) else ''}", "", "## Ошибки акторов"]
    for e in errors:
        rmd += [f"### [{e.get('severity','med')}] {e.get('actor','?')} — {e.get('what','')}",
                f"- корень: {e.get('root_cause','')}", f"- избежать: {e.get('how_avoid','')}",
                f"- исправить: {e.get('how_fix','')}", ""]
    rmd += ["## Точность задач пользователя"]
    for it in inter:
        rmd += [f"- precision {it.get('precision','?')}/10 — {it.get('ambiguities','')}; совет: {it.get('suggestion','')}"]
    Path("/tmp/REFLECTION.md").write_text("\n".join(rmd), encoding="utf-8")
    rclone("copyto", "/tmp/REFLECTION.md", f"{YD_AUDIT}/REFLECTION.md", timeout=60)

    # PROPOSALS.md
    pmd = [f"# PROPOSALS (cloud) — {ts}", "", "> Внедрение — за yaromat/Claude.", ""]
    for p in proposals:
        pmd += [f"## [{p.get('type','?').upper()}] {p.get('target','')} · effort={p.get('effort','?')}",
                f"- что: {p.get('proposal','')}", f"- выигрыш: {p.get('benefit','')}", ""]
    Path("/tmp/PROPOSALS.md").write_text("\n".join(pmd), encoding="utf-8")
    rclone("copyto", "/tmp/PROPOSALS.md", f"{YD_AUDIT}/PROPOSALS.md", timeout=60)

    # findings-ошибки → фид (бук ingest вольёт в локальную БД)
    if errors:
        fl = Path("/tmp/reflect_findings.jsonl")
        with open(fl, "w", encoding="utf-8") as f:
            for e in errors:
                f.write(json.dumps({"ts": ts, "kind": "error", "actor": e.get("actor", "?"),
                                    "action": "cloud_reflect", "status": "fail",
                                    "detail": e.get("what", ""), "root_cause": e.get("root_cause", ""),
                                    "how_fix": e.get("how_fix", ""), "severity": e.get("severity", "med")},
                                   ensure_ascii=False) + "\n")
        rclone("copyto", str(fl), f"{YD_FEED}/cloud_reflect_{int(time.time())}.jsonl", timeout=60)

    # TG дайджест
    prec = [it.get("precision", 0) for it in inter]
    avg = round(sum(prec) / len(prec), 1) if prec else "—"
    tg(f"☁️ <b>Облачная рефлексия</b> ({ts})\n"
       f"{reflection.get('summary','')[:300] if isinstance(reflection,dict) else ''}\n"
       f"ошибок: {len(errors)} · точность задач: {avg}/10 · предложений: {len(proposals)}\n"
       f"→ AUDIT/REFLECTION.md + PROPOSALS.md")
    log(f"done: errors={len(errors)} proposals={len(proposals)}")


if __name__ == "__main__":
    main()
