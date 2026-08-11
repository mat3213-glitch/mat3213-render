#!/usr/bin/env python3
"""
auto_analyst.py — автономный анализ-тест репо/сайтов по рубрике пользы (analyst_rubric.yaml).

Запускается на GH Actions раннере (песочница: эфемерная, изолированная, бесплатная,
ноль нагрузки на бук). Для каждой цели:
  1. fetch        — git clone (репо) / GH API (org) / curl (сайт) в /tmp
  2. collect      — README, дерево файлов, манифесты, лицензия, метаданные (звёзды/коммиты)
  3. Stage A      — mimo оценивает каждый критерий рубрики 0-10 + риски + план препарации;
                    Python считает взвешенный скор ДЕТЕРМИНИРОВАННО (mimo судит, код считает)
  4. routing      — hard-reject → skip; инстр.скор>=порог → smoke-test; concept>=порог → концепт-заметка
  5. Stage B      — smoke-test в песочнице (детект экосистемы → install → smoke), best-effort
  6. outputs      — verified_tools/<slug>/README.md + report.json на ЯД (НЕ теряется)
  7. board        — ADOPTION_BOARD.md: view из report.json + repo_scout_ledger.json
  8. TG-отчёт     — в тред 634 (STYLE SCOUT): вердикт + что вытащить/выкинуть +
                    РИСКИ (перегруз связей с архитектурой + устойчивость) + автономность.
                    Финал «внедряем/нет» — за yaromat.

Цели берутся из: --targets "url1 url2" | --targets-file f | ЯД-очередь analyst_queue/pending/.

Запуск:
  python3 auto_analyst.py --targets "https://github.com/owner/repo ..."
  python3 auto_analyst.py --from-queue
  python3 auto_analyst.py --from-scout       # автономно: последние находки repo_scout
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import urllib.request
import urllib.parse

try:
    import yaml
except Exception:
    yaml = None

HERE = Path(__file__).resolve().parent
RUBRIC_PATH = HERE / "analyst_rubric.yaml"
QWEN_CHAT = HERE / "qwen" / "qwen_chat.py"   # [2026-07-27] mimo free снят → Qwen-чат (текст)
LEDGER_PATH = HERE / "repo_scout_ledger.json"

from scout_ledger import ScoutLedger, github_full_name  # noqa: E402

YD = "ydrive:Content factory"
YD_QUEUE_PENDING = f"{YD}/cloud_io/CreativeLab/analyst_queue/pending"
YD_QUEUE_DONE = f"{YD}/cloud_io/CreativeLab/analyst_queue/done"
YD_TOOLS = f"{YD}/verified_tools"
YD_BOARD = f"{YD}/verified_tools/ADOPTION_BOARD.md"
YD_SCOUT = f"{YD}/repo_scout"   # находки repo_scout (если есть)

WORKDIR = Path("/tmp/analyst")
OUTDIR = Path("/tmp/analyst_out")

TG_WORKER = os.environ.get("CLOUDFLARE_WORKER", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("SCOUT_CHAT_ID", "")
TG_THREAD = os.environ.get("SCOUT_THREAD", "1653")  # тред задаётся диспатчером: 1653=GROK SCOUT, 1699=REPO SCOUT
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")

BOARD_STATUS = {
    "adopted": "✅ ADOPTED",
    "rejected": "❌ REJECTED",
    "park": "🅿 PARK",
    "pilot": "🧪 PILOT",
}


def log(m): print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


def sh(cmd, timeout=120, cwd=None):
    """Запуск shell-команды, best-effort. Возвращает (rc, out)."""
    try:
        r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                           text=True, timeout=timeout, cwd=cwd, stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout + r.stderr)
    except subprocess.TimeoutExpired:
        return 124, f"timeout {timeout}s"
    except Exception as e:
        return 1, f"{type(e).__name__}: {e}"


def rclone(*args):
    return subprocess.run(["rclone"] + list(args), capture_output=True, text=True)


SERVICE_THREAD = "2182"   # «🔧 сервис · логи» — статус-пинги, не курируемый скаут-тред

def _tg_one(text: str, thread: str | None = None):
    data = urllib.parse.urlencode({
        "chat_id": TG_CHAT, "message_thread_id": thread or TG_THREAD,
        "text": text, "parse_mode": "HTML",
    }).encode()
    # CF Worker режет дефолтный Python-urllib UA (403/1010) — притворяемся curl
    req = urllib.request.Request(f"{TG_WORKER}/bot{TG_TOKEN}/sendMessage", data=data,
                                 headers={"User-Agent": "curl/8.5.0"})
    urllib.request.urlopen(req, timeout=30)


def tg(text: str, chunk: int = 3800, thread: str | None = None):
    """Отчёт в TG-тред через CF Worker. thread=None → скаут-тред (TG_THREAD); задан → override.
    TG-лимит 4096 → длинное РАЗБИВАЕМ (грабля №8), не обрезаем."""
    if not (TG_WORKER and TG_TOKEN and TG_CHAT):
        log("TG: секреты не заданы — пропуск"); return
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
            _tg_one(p + (f"\n<i>…({i+1}/{len(parts)})</i>" if len(parts) > 1 else ""), thread=thread)
        except Exception as e:
            log(f"TG err: {e}")
    log("TG: отчёт отправлен")


# ── рубрика ──────────────────────────────────────────────────────────────────────

def load_rubric() -> dict:
    if yaml is None:
        raise RuntimeError("pyyaml не установлен (нужен на раннере)")
    return yaml.safe_load(RUBRIC_PATH.read_text(encoding="utf-8"))


# ── цели ─────────────────────────────────────────────────────────────────────────

def normalize_analyst_target(raw: str) -> str | None:
    """Return a canonical analyst target or reject malformed/non-repo GitHub pages.

    External HTTP(S) tools/articles are a valid separate analyst route and keep their
    URL. GitHub is stricter: root/search/topics/org/mentions-like pages are not repos;
    deep repository links normalize to one ``https://github.com/owner/repo`` target.
    """
    value = str(raw or "").strip()
    try:
        parsed = urllib.parse.urlparse(value)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    if parsed.netloc.casefold() in ("github.com", "www.github.com"):
        full_name = github_full_name(value)
        return f"https://github.com/{full_name}" if full_name else None
    return value.rstrip("/")


def lifecycle_status(url: str, ledger: ScoutLedger) -> str:
    full_name = github_full_name(url)
    entry = ledger.entry(full_name) if full_name else None
    return BOARD_STATUS[entry["status"]] if entry else "PENDING"


def board_rows_from_reports(reports: list[dict], ledger: ScoutLedger) -> tuple[list[dict], int]:
    """Pure board projection. Ledger is source of truth; invalid targets are omitted."""
    rows, dropped = [], 0
    for report in reports:
        raw_url = report.get("url", "")
        url = normalize_analyst_target(raw_url)
        if not url:
            dropped += 1
            continue
        rows.append({
            "url": url,
            "slug": report.get("slug") or slug_of(url),
            "score": report.get("score", 0),
            "route": report.get("route", "?"),
            "status": lifecycle_status(url, ledger),
        })
    rows.sort(key=lambda x: x["score"], reverse=True)
    return rows, dropped

def collect_targets(args) -> list[str]:
    targets = []
    if args.targets:
        targets += re.split(r"\s+", args.targets.strip())
    if args.targets_file:
        targets += [ln.strip() for ln in Path(args.targets_file).read_text().splitlines() if ln.strip()]
    if args.from_queue:
        r = rclone("lsf", YD_QUEUE_PENDING, "--include", "*.txt")
        for fn in [f.strip() for f in r.stdout.splitlines() if f.strip()]:
            c = rclone("cat", f"{YD_QUEUE_PENDING}/{fn}")
            targets += [ln.strip() for ln in c.stdout.splitlines() if ln.strip().startswith("http")]
            rclone("moveto", f"{YD_QUEUE_PENDING}/{fn}", f"{YD_QUEUE_DONE}/{fn}")
    if args.from_scout:
        # АВТОНОМНЫЙ источник: локальный дайджест repo_scout (коммитится в этот же репо,
        # воркфлоу его чекаутит — ЯД-обвязка не нужна). Дедуп против уже проверенных.
        rep = HERE / "repo_scout_latest.md"
        if rep.exists():
            urls = re.findall(r"https://github\.com/[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+", rep.read_text(errors="replace"))
            already = _existing_slugs()
            for u in urls:
                if slug_of(u) not in already:
                    targets.append(u)
    # Уникализируем и валидируем ДО matrix/Qwen. Ledger — source of truth решений:
    # adopted/rejected/park/pilot повторно не анализируем. Legacy seen здесь намеренно
    # не используем: свежая находка Repo Scout уже записана в seen перед первым анализом.
    ledger = ScoutLedger(LEDGER_PATH)
    seen, out = set(), []
    for raw in targets:
        target = normalize_analyst_target(raw)
        if not target:
            log(f"target rejected (invalid/non-repo GitHub): {str(raw)[:120]}")
            continue
        full_name = github_full_name(target)
        if full_name and ledger.contains(full_name):
            log(f"target excluded by scout ledger: {full_name} [{ledger.entry(full_name)['status']}]")
            continue
        key = target.casefold() if full_name else target
        if key not in seen:
            seen.add(key); out.append(target)
    return out


def _existing_slugs() -> set[str]:
    """Слаги уже проверенных инструментов на ЯД (для дедупа автономного прохода)."""
    r = rclone("lsf", YD_TOOLS, "--dirs-only")
    return {x.strip().rstrip("/") for x in r.stdout.splitlines() if x.strip()}


def slug_of(url: str) -> str:
    s = re.sub(r"^https?://", "", url).rstrip("/")
    s = re.sub(r"[^A-Za-z0-9]+", "__", s)
    return s[:80].strip("_")


# ── fetch + collect ────────────────────────────────────────────────────────────────

def gh_api(path: str) -> dict | list | None:
    try:
        req = urllib.request.Request(f"https://api.github.com{path}",
            headers={"Accept": "application/vnd.github+json",
                     **({"Authorization": f"token {GH_TOKEN}"} if GH_TOKEN else {})})
        return json.loads(urllib.request.urlopen(req, timeout=30).read())
    except Exception as e:
        log(f"gh_api {path} err: {e}"); return None


def fetch(url: str, dest: Path) -> dict:
    """Скачать цель. Возвращает контекст: kind, meta, readme, tree, manifests, license."""
    dest.mkdir(parents=True, exist_ok=True)
    ctx = {"url": url, "kind": "site", "meta": {}, "readme": "", "tree": "",
           "manifests": {}, "license": "", "local": str(dest)}
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", url)
    org = re.match(r"https?://github\.com/(?:orgs/)?([^/]+)/?$", url) or \
          re.match(r"https?://github\.com/orgs/([^/]+)/repositories", url)
    if m:
        owner, repo = m.group(1), m.group(2)
        ctx["kind"] = "repo"
        meta = gh_api(f"/repos/{owner}/{repo}") or {}
        ctx["meta"] = {k: meta.get(k) for k in
                       ("full_name", "description", "stargazers_count", "pushed_at",
                        "language", "archived", "open_issues_count")}
        ctx["license"] = (meta.get("license") or {}).get("spdx_id") or "?"
        rc, _ = sh(["git", "clone", "--depth", "1", url, str(dest)], timeout=180)
        if rc == 0:
            for name in ("README.md", "README.rst", "readme.md", "README"):
                p = dest / name
                if p.exists():
                    ctx["readme"] = p.read_text(errors="replace")[:6000]; break
            files = [str(p.relative_to(dest)) for p in dest.rglob("*")
                     if p.is_file() and ".git/" not in str(p)]
            ctx["tree"] = "\n".join(sorted(files)[:200])
            for mf in ("package.json", "requirements.txt", "pyproject.toml", "setup.py",
                       "go.mod", "Cargo.toml", "Dockerfile", "Makefile"):
                p = dest / mf
                if p.exists():
                    ctx["manifests"][mf] = p.read_text(errors="replace")[:1500]
    elif org:
        owner = org.group(1)
        ctx["kind"] = "org"
        repos = gh_api(f"/orgs/{owner}/repos?per_page=30&sort=pushed") or \
                gh_api(f"/users/{owner}/repos?per_page=30&sort=pushed") or []
        ctx["meta"] = {"owner": owner, "n_repos": len(repos) if isinstance(repos, list) else 0}
        if isinstance(repos, list):
            ctx["tree"] = "\n".join(
                f"{r.get('name')} ★{r.get('stargazers_count')} — {r.get('description') or ''}"[:160]
                for r in repos)
    else:
        # сайт
        rc, body = sh(["curl", "-sL", "-m", "40", "--compressed", url], timeout=50)
        text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", body)
        text = re.sub(r"<[^>]+>", " ", text)
        ctx["readme"] = re.sub(r"\s+", " ", text)[:6000]
    return ctx


# ── Stage A: Qwen-анализ по рубрике (текст) ─────────────────────────────────────────

def _extract_json(s: str) -> dict | None:
    s = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s)
    depth, start = 0, -1
    for i, ch in enumerate(s):
        if ch == "{":
            if depth == 0: start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    return json.loads(s[start:i + 1])
                except Exception:
                    start = -1
    return None


def analyze(ctx: dict, rubric: dict) -> dict:
    crit = rubric["criteria"]
    crit_lines = "\n".join(f'  - "{cid}": {c["desc"]}' for cid, c in crit.items())
    hr_lines = "\n".join(f'  - "{h["id"]}": {h["desc"]}' for h in rubric["hard_rejects"])
    arch = rubric.get("architecture_context", "")

    ctx_block = (
        f"ЦЕЛЬ: {ctx['url']} (тип: {ctx['kind']})\n"
        f"МЕТА: {json.dumps(ctx['meta'], ensure_ascii=False)}\n"
        f"ЛИЦЕНЗИЯ: {ctx['license']}\n"
        f"README/КОНТЕНТ:\n{ctx['readme']}\n\n"
        f"ДЕРЕВО ФАЙЛОВ (до 200):\n{ctx['tree']}\n\n"
        f"МАНИФЕСТЫ: {json.dumps(list(ctx['manifests'].keys()))}\n"
        + "\n".join(f"--- {k} ---\n{v}" for k, v in ctx["manifests"].items())
    )[:14000]

    prompt = f"""Ты — технический разведчик инструментов для контент-фабрики. Оцени цель по рубрике и верни СТРОГО ОДИН JSON-объект, без текста до/после.

НАША АРХИТЕКТУРА (для оценки рисков внедрения):
{arch}

КРИТЕРИИ (оцени каждый ЦЕЛЫМ числом 0-10, где 10=отлично подходит нам):
{crit_lines}

ЖЁСТКИЕ ОТСЕЧКИ (true если применимо — тогда инструмент не тестируем):
{hr_lines}

ПРАВИЛО ЛИЦЕНЗИИ: если у цели нет лицензии (пусто, «?», NOASSERTION), what_extract обязан
ограничиваться идеей/подходом и явно запрещать копирование кода; формулировки вроде «взять как
модуль» в этом случае недопустимы.

ЦЕЛЬ ДЛЯ ОЦЕНКИ:
{ctx_block}

Верни JSON ровно такой формы:
{{
  "summary": "1-2 предложения: что за проект, что делает, для кого. Нейтрально, без оценок и без слов «брать/не брать»",
  "scores": {{ {", ".join(f'"{cid}": <0-10>' for cid in crit)} }},
  "hard_rejects": {{ {", ".join(f'"{h["id"]}": <true|false>' for h in rubric["hard_rejects"])} }},
  "what_extract": "что КОНКРЕТНО вытащить из инструмента (модуль/идея/подход)",
  "what_discard": "что откинуть (платное/лишнее/несовместимое)",
  "concept_note": "если продукт не подходит напрямую — какую идею/концепцию адаптировать к нам (иначе пусто)",
  "risk_architecture": "риск ПЕРЕГРУЗА СВЯЗЕЙ с текущей архитектурой: новые зависимости/точки отказа/пересечения с узлами (ЯД/cron/очередь/GH/бук). Оцени low/medium/high + почему",
  "risk_stability": "риск УСТОЙЧИВОСТИ: хрупкость, внешние платные API, нестабильный upstream, нагрузка на бук/раннер. low/medium/high + почему",
  "autonomy": "можно ли сделать этот инструмент НЕЗАВИСИМЫМ от внешних триггеров (человека/ИИ) — на cron/GH Actions/self-feed? yes/partial/no + как именно",
  "verdict": "1-2 предложения: брать/на заметку/мимо и почему"
}}"""

    rc, out = sh(["python3", str(QWEN_CHAT), prompt, "--model", "", "--timeout", "420"],
                 timeout=480)
    data = _extract_json(out)
    if not data:
        return {"_error": f"qwen не вернул JSON (rc={rc}): {out[:300]}"}
    return data


def weighted_score(scores: dict, rubric: dict) -> int:
    crit = rubric["criteria"]
    total = 0.0
    for cid, c in crit.items():
        v = scores.get(cid, 0)
        try:
            v = max(0, min(10, float(v)))
        except Exception:
            v = 0
        total += v / 10.0 * c["weight"]
    return round(total)


# ── Stage B: smoke-test в песочнице ─────────────────────────────────────────────────

def smoke_test(ctx: dict) -> dict:
    d = Path(ctx["local"]); mf = ctx["manifests"]
    steps = []

    def step(name, cmd, timeout=180):
        rc, out = sh(cmd, timeout=timeout, cwd=str(d))
        steps.append({"step": name, "rc": rc, "tail": out[-600:]})
        return rc

    if "package.json" in mf:
        if step("npm install", "npm install --no-audit --no-fund --ignore-scripts") == 0:
            pj = json.loads((d / "package.json").read_text(errors="replace") or "{}")
            scr = pj.get("scripts", {})
            if "build" in scr: step("npm run build", "npm run build")
            elif "test" in scr: step("npm test", "npm test")
    elif "requirements.txt" in mf:
        step("pip install -r", "pip install -r requirements.txt")
    elif "pyproject.toml" in mf or "setup.py" in mf:
        step("pip install .", "pip install .")
    elif "go.mod" in mf:
        step("go build", "go build ./...")
    elif "Cargo.toml" in mf:
        step("cargo build", "cargo build")
    else:
        steps.append({"step": "n/a", "rc": 0, "tail": "нет распознанного манифеста — smoke пропущен"})
    ok = all(s["rc"] == 0 for s in steps if s["step"] != "n/a") and any(s["step"] != "n/a" for s in steps)
    return {"ok": ok, "steps": steps}


# ── outputs ─────────────────────────────────────────────────────────────────────────

def make_readme(ctx, a, score, route, smoke) -> str:
    return f"""# {ctx['url']}

**Проверено авто-анализатором:** {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}
**Тип:** {ctx['kind']} | **Лицензия:** {ctx['license']} | **Скор пользы:** {score}/100 | **Роут:** {route}

## О чём это
{a.get('summary', '?')}

## Вердикт
{a.get('verdict', '?')}

## Что вытащить
{a.get('what_extract', '?')}

## Что откинуть
{a.get('what_discard', '?')}

## Концепт-заметка (если продукт не подходит напрямую)
{a.get('concept_note') or '—'}

## Риски внедрения
- **Перегруз связей с архитектурой:** {a.get('risk_architecture', '?')}
- **Устойчивость:** {a.get('risk_stability', '?')}

## Автономность (независимость от человека/ИИ)
{a.get('autonomy', '?')}

## Скоринг по критериям
{json.dumps(a.get('scores', {}), ensure_ascii=False, indent=2)}

## Smoke-test
{json.dumps(smoke, ensure_ascii=False, indent=2) if smoke else 'не запускался (скор ниже порога / hard-reject)'}

## Метаданные цели
{json.dumps(ctx['meta'], ensure_ascii=False, indent=2)}

---
> Lifecycle хранится в `repo_scout_ledger.json`; `ADOPTION_BOARD.md` — только представление.
"""


def rebuild_board():
    """Пересобрать ADOPTION_BOARD.md из report.json + scout ledger (идемпотентно).

    Параллельные matrix-джобы пишут только свои report.json; доску собирает ОДНА финальная
    джоба этим вызовом. Ledger — единственный source of truth lifecycle; доска — view.
    """
    ledger = ScoutLedger(LEDGER_PATH)
    reports = []
    # One remote traversal instead of 1 + N separate rclone processes. With hundreds
    # of reports the old loop spent minutes on connection/setup latency alone.
    with tempfile.TemporaryDirectory(prefix="analyst-board-") as td:
        copied = rclone("copy", YD_TOOLS, td, "--include", "*/report.json")
        if copied.returncode != 0:
            raise RuntimeError(f"cannot download analyst reports: {copied.stderr[-300:]}")
        for path in Path(td).rglob("report.json"):
            try:
                j = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            j.setdefault("slug", path.parent.name)
            reports.append(j)
    rows, dropped = board_rows_from_reports(reports, ledger)

    board = ("# ADOPTION BOARD — представление проверенных инструментов\n\n"
             "> Source of truth lifecycle: `repo_scout_ledger.json`. Эта доска пересобирается и не хранит решения.\n"
             "> Статусы: ✅ ADOPTED · ❌ REJECTED · 🅿 PARK · 🧪 PILOT · PENDING.\n"
             "> Внешние non-GitHub инструменты/статьи сохраняются как отдельный analyst route.\n"
             f"> Обновлено: {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}\n\n"
             "| Инструмент | Скор | Роут | Статус | Папка |\n"
             "|---|---|---|---|---|\n")
    board += "".join(
        f"| {r['url']} | {r['score']} | {r['route']} | {r['status']} | verified_tools/{r['slug']}/ |\n"
        for r in rows)
    tmp = OUTDIR / "ADOPTION_BOARD.md"
    tmp.write_text(board, encoding="utf-8")
    rclone("copyto", str(tmp), YD_BOARD)
    pending = sum(1 for r in rows if r["status"] == "PENDING")
    tg(f"🏁 <b>Авто-анализатор</b> — доска обновлена: {len(rows)} инструментов, {pending} PENDING.\n"
       f"Решения: repo_scout_ledger.json; view: verified_tools/ADOPTION_BOARD.md",
       thread=SERVICE_THREAD)
    log(f"board rebuilt: {len(rows)} rows, {pending} pending, {dropped} invalid targets dropped")


# ── главный цикл ────────────────────────────────────────────────────────────────────

def route_of(a, score, rubric) -> str:
    th = rubric["thresholds"]
    if any(bool(v) for v in a.get("hard_rejects", {}).values()):
        return "SKIP (hard-reject)"
    cv = a.get("scores", {}).get("concept_value", 0)
    try: cv = float(cv)
    except Exception: cv = 0
    if score >= th["smoke_test"]:
        return "TOOL (smoke-test)"
    if cv >= th["concept_note"]:
        return "CONCEPT-NOTE"
    if score >= th["watch"]:
        return "WATCH"
    return "SKIP (низкий скор)"


def process(url, rubric) -> dict:
    slug = slug_of(url)
    log(f"▶ {url}  →  {slug}")
    ctx = fetch(url, WORKDIR / slug)
    a = analyze(ctx, rubric)
    if "_error" in a:
        tg(f"❌ <b>Анализатор</b>: {url}\nОшибка анализа: {a['_error'][:300]}")
        return {"url": url, "slug": slug, "score": 0, "route": "ERROR", "error": a["_error"]}
    score = weighted_score(a.get("scores", {}), rubric)
    route = route_of(a, score, rubric)

    smoke = None
    if route.startswith("TOOL") and ctx["kind"] == "repo":
        log("  smoke-test...")
        smoke = smoke_test(ctx)

    # outputs на ЯД
    out = OUTDIR / slug
    out.mkdir(parents=True, exist_ok=True)
    (out / "README.md").write_text(make_readme(ctx, a, score, route, smoke), encoding="utf-8")
    (out / "report.json").write_text(json.dumps(
        {"url": url, "slug": slug, "score": score, "route": route,
         "analysis": a, "smoke": smoke, "meta": ctx["meta"]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    rclone("copy", str(out), f"{YD_TOOLS}/{slug}")

    # TG-отчёт
    smoke_line = ""
    if smoke is not None:
        smoke_line = f"\n🧪 smoke: {'✅ ок' if smoke['ok'] else '⚠️ см. отчёт'}"
    # Лицензия — отдельной строкой и ДО вердикта. Без неё код брать нельзя ни целиком,
    # ни кусками: 29.07 анализатор дал 84/100 и «брать как модуль» репозиторию вообще
    # без лицензии (onojk/kaleido-video-generator). Идея не охраняется — код охраняется.
    lic = (ctx.get("license") or "").strip()
    if not lic or lic in ("?", "NOASSERTION"):
        lic_line = "⚠️ <b>Лицензии НЕТ — код брать нельзя, допустима только идея</b>"
    else:
        lic_line = f"📄 Лицензия: <code>{lic}</code>"
    tg(
        f"🔎 <b>Анализатор</b> — <code>{score}/100</code> — {route}\n"
        f"<b>{url}</b>\n\n"
        # Сначала «что это вообще такое», потом вердикт: раньше отчёт стартовал с
        # «брать/не брать», и читателю приходилось идти по ссылке, чтобы понять предмет.
        f"📖 {a.get('summary','?')[:400]}\n"
        f"{lic_line}\n\n"
        f"📌 {a.get('verdict','?')[:400]}\n\n"
        f"➕ Вытащить: {a.get('what_extract','?')[:300]}\n"
        f"➖ Откинуть: {a.get('what_discard','?')[:200]}\n"
        f"🧩 Концепт: {(a.get('concept_note') or '—')[:250]}\n\n"
        f"⚠️ Риск связей: {a.get('risk_architecture','?')[:250]}\n"
        f"⚠️ Устойчивость: {a.get('risk_stability','?')[:250]}\n"
        f"🤖 Автономность: {a.get('autonomy','?')[:250]}{smoke_line}\n\n"
        f"📁 verified_tools/{slug}/ — решение фиксируется в repo_scout_ledger.json"
    )
    return {"url": url, "slug": slug, "score": score, "route": route}


def list_targets_only(args):
    """Печатает JSON-массив целей (для matrix-фан-аута в воркфлоу). Очередь → done."""
    print(json.dumps(collect_targets(args)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", help="URL через пробел")
    ap.add_argument("--targets-file")
    ap.add_argument("--from-queue", action="store_true", help="брать из ЯД analyst_queue/pending/")
    ap.add_argument("--from-scout", action="store_true", help="брать из находок repo_scout (автономно)")
    ap.add_argument("--list-targets", action="store_true",
                    help="только напечатать JSON-список целей (для matrix), очередь→done")
    ap.add_argument("--rebuild-board", action="store_true",
                    help="финальная джоба: пересобрать ADOPTION_BOARD из report.json на ЯД")
    args = ap.parse_args()

    OUTDIR.mkdir(parents=True, exist_ok=True)

    if args.list_targets:
        list_targets_only(args); return
    if args.rebuild_board:
        rebuild_board(); return

    rubric = load_rubric()
    targets = collect_targets(args)
    if not targets:
        log("целей нет"); return
    log(f"целей: {len(targets)}")

    # ОБЫЧНЫЙ режим (одна джоба, несколько целей последовательно). В matrix-фан-ауте
    # каждая параллельная джоба зовёт скрипт с ОДНОЙ целью; доску собирает финальная джоба.
    for url in targets:
        try:
            process(url, rubric)
        except Exception as e:
            log(f"  ERR {url}: {e}")
            tg(f"❌ Анализатор упал на {url}: {str(e)[:200]}")
    log("done (per-target)")


if __name__ == "__main__":
    main()
