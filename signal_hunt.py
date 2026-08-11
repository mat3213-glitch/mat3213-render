#!/usr/bin/env python3
"""signal_hunt.py — МОСТ Grok→auto_analyst (ТОЛЬКО мост, без LLM-brainstorm).

История: brainstorm на litellm выпилен — LLM без реального поиска либо повторяет
очевидное, либо ВЫДУМЫВАЕТ (проверка 2026-06-23: 0/13 неочевидных репо реальны).
Дискавери = заземлённый поиск: repo_scout (GitHub Search) + ручной Grok (X/Reddit/Habr).

Этот скрипт = «перекладыватель ссылок»: берёт source_link из свежего
signals/incoming/grok_<date>.json (что прислал ручной Grok — реальные находки) →
дедуп против verified_tools → ЯД analyst_queue/pending/ → диспатч auto_analyst.yml
(matrix --from-queue, тред 1653 GROK SCOUT). Песочница auto_analyst заземляет: реально
клонит/курлит каждый URL, мёртвое отсеивается.
"""
import base64
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scout_filters import NoveltyIndex, is_saturated  # noqa: E402
from scout_ledger import canonical_name, github_full_name, load_excluded_names  # noqa: E402
from scout_needs import CurrentNeeds  # noqa: E402

YD = "ydrive:Content factory"
QUEUE = f"{YD}/cloud_io/CreativeLab/analyst_queue/pending"
TOOLS = f"{YD}/verified_tools"
SIGNALS_REPO = "mat3213-glitch/mat3213-signals"
RENDER_REPO = "mat3213-glitch/mat3213-render"
GROK_THREAD = "1653"   # GROK SCOUT — РОУТИНГ ПО ИСТОЧНИКУ: всё, что нашёл Grok (даже репо), идёт сюда
GH_TOKEN = os.environ.get("GH_DISPATCH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
HERE = Path(__file__).resolve().parent
LEDGER_FILE = HERE / "repo_scout_ledger.json"
SEEN_FILE = HERE / "repo_scout_seen.json"
NEEDS_FILE = HERE / "repo_scout_current_needs.v1.json"


def filter_lifecycle_urls(urls: list[str], excluded_names: set[str]) -> tuple[list[str], list[str]]:
    """Remove decided/seen GitHub repositories before the analyst queue.

    Grok may link an issue, branch or ``.git`` URL rather than the repository root;
    all variants resolve to the same case-insensitive ``owner/repo`` ledger key.
    Non-GitHub tool/article URLs are outside Repo Scout lifecycle and pass through.
    Returns ``(kept, dropped_repo_names)`` for an auditable bridge log.
    """
    excluded = {canonical_name(name) for name in excluded_names}
    kept, dropped = [], []
    for url in urls:
        full_name = github_full_name(url)
        if full_name and canonical_name(full_name) in excluded:
            dropped.append(full_name)
            continue
        kept.append(url)
    return kept, dropped


def assess_grok_github_item(item: dict, needs: CurrentNeeds) -> dict | None:
    """Apply the same mandatory-evidence gate to a Grok GitHub finding.

    Grok's grounded ``what/why_us/proof`` fields are the metadata available before
    analyst cloning. Non-GitHub links return ``None`` because they belong to the
    deep-web digest or the analyst's generic URL path, not Repo Scout needs.
    """
    link = str(item.get("source_link") or "")
    full_name = github_full_name(link)
    if not full_name:
        return None
    return needs.assess({
        "full_name": full_name,
        "description": " ".join(str(item.get(key) or "") for key in ("what", "why_us", "proof")),
    })


def grok_signals(needs: CurrentNeeds | None = None):
    """Кандидаты из свежего grok_*.json. Возвращает (в_анализатор, в_дайджест).

    🔴 Правка 07.08 (задание yaromat «пусть грок тащит не репо с гитхаба, а глубинный интернет —
    лайфхаки, бесплатные периоды, истории контент-фабрик»): раньше мост брал ТОЛЬКО ссылки на
    github.com и всё остальное молча выбрасывал. С новым профилем скаута так терялось бы почти всё,
    что он приносит. Теперь маршрута два:
      • репо/инструмент → песочница auto_analyst (клонит, курлит, smoke-тестит) — как было;
      • приём/акция/история/источник → ПРЯМО в дайджест TG: гонять статью через песочницу
        бессмысленно, она там ничего не соберёт, а прочитать её должен человек.
    """
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{SIGNALS_REPO}/contents/signals/incoming",
            headers={"Authorization": f"token {GH_TOKEN}", "User-Agent": "curl/8.0"})
        files = json.load(urllib.request.urlopen(req, timeout=30))
        groks = sorted(f["name"] for f in files if f["name"].startswith("grok_"))
        if not groks:
            print("нет grok_*.json")
            return [], []
        latest = groks[-1]
        req2 = urllib.request.Request(
            f"https://api.github.com/repos/{SIGNALS_REPO}/contents/signals/incoming/{latest}",
            headers={"Authorization": f"token {GH_TOKEN}", "User-Agent": "curl/8.0"})
        doc = json.loads(base64.b64decode(json.load(urllib.request.urlopen(req2, timeout=30))["content"]))
        items = doc.get("candidates") or doc.get("items") or []
        to_analyst, to_digest, sat, needs_drop = [], [], 0, 0
        for it in items:
            link = str(it.get("source_link", ""))
            if not link.startswith("http"):
                continue
            # Замер 07.08 по четырём дням подряд: 38 из 55 кандидатов Grok'а — «бесплатные
            # LLM-шлюзы», класс, которым проект давно закрыт своим пулом воркеров. На выборке
            # ложных срабатываний ноль. Резать надо ЗДЕСЬ: каждый такой URL — это ещё один
            # прогон анализатора (минуты GH) ради вердикта SKIP.
            if is_saturated(f"{it.get('what', '')} {it.get('why_us', '')} {link}"):
                sat += 1
                continue
            kind = str(it.get("type", "")).lower()
            github_assessment = assess_grok_github_item(it, needs) if needs else None
            if github_assessment is not None and not github_assessment["accepted"]:
                needs_drop += 1
                continue
            if github_assessment is not None:
                print(f"  [needs] {github_full_name(link)} → {github_assessment['need_id']} "
                      f"evidence={github_assessment['evidence']}")
            if kind in ("tool", "repo") or github_full_name(link):
                to_analyst.append(link)
            else:
                to_digest.append(it)
        print(f"[мост] {latest}: в анализатор {len(to_analyst)}, в дайджест {len(to_digest)}, "
              f"отсеяно как насыщенная тема {sat}, без current-needs evidence {needs_drop}")
        return to_analyst, to_digest
    except Exception as e:
        print(f"[мост] fail: {str(e)[:140]}")
        return [], []


def send_tg(text: str):
    """Дайджест приёмов/акций в тред GROK SCOUT. Через CF Worker — api.telegram.org закрыт с RU-IP,
    и канал держим единый даже с чистого egress раннера."""
    worker = os.environ.get("CLOUDFLARE_WORKER", "")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("SCOUT_CHAT_ID", "")
    if not (worker and token and chat):
        print("[tg] секреты не заданы — печатаю:\n" + text)
        return
    payload = json.dumps({"chat_id": chat, "message_thread_id": int(GROK_THREAD),
                          "text": text[:3900], "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"{worker}/bot{token}/sendMessage", data=payload, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "curl/8.0"})
    try:
        urllib.request.urlopen(req, timeout=45)
        print(f"[tg] дайджест ушёл в тред {GROK_THREAD}")
    except Exception as e:
        print(f"[tg] ошибка: {str(e)[:140]}\n{text}")


def build_digest(items: list) -> str:
    """Срочное — вперёд: акция с дедлайном ценна ровно до даты окончания."""
    order = {"promo": 0, "hack": 1, "source": 2, "craft": 3, "case": 4}
    items = sorted(items, key=lambda x: order.get(str(x.get("type", "")).lower(), 9))
    mark = {"promo": "🎟", "hack": "🔧", "case": "📈", "craft": "🎬", "source": "📦"}
    lines = [f"🕳 Глубинный скаут — находок: {len(items)}\n"]
    for it in items[:10]:
        kind = str(it.get("type", "")).lower()
        lines.append(f"{mark.get(kind, '•')} {it.get('what', '')}")
        if it.get("why_us"):
            lines.append(f"  {it['why_us']}")
        if it.get("proof"):
            lines.append(f"  📌 {str(it['proof'])[:180]}")
        tail = []
        if it.get("expires"):
            tail.append(f"до {it['expires']}")
        if str(it.get("region_ok", "")).lower() == "no":
            tail.append("❌ не из РФ")
        if it.get("needs_card") is True:
            tail.append("нужна карта")
        if tail:
            lines.append("  ⏳ " + " · ".join(tail))
        lines.append(f"  {it.get('source_link', '')}\n")
    return "\n".join(lines)


def existing_slugs():
    r = subprocess.run(["rclone", "lsf", TOOLS, "--dirs-only"], capture_output=True, text=True)
    return {x.strip().rstrip("/") for x in r.stdout.splitlines() if x.strip()}


def slug(u):
    return u.replace("https://", "").replace("/", "__").replace(".", "__").replace("-", "__").rstrip("_")


def main():
    needs = CurrentNeeds(NEEDS_FILE)
    analyst_urls, digest_items = grok_signals(needs)

    # Repo Scout и Grok — два входа в ОДНУ analyst queue. Поэтому lifecycle должен
    # применяться к обоим: иначе adopted/rejected/park/pilot или просто уже показанное
    # репо немедленно возвращается через второй канал под deep-link URL.
    ledger, excluded = load_excluded_names(LEDGER_FILE, SEEN_FILE)
    analyst_urls, lifecycle_dropped = filter_lifecycle_urls(analyst_urls, excluded)
    print(f"[мост] lifecycle ledger={len(ledger.repos)}, deny-set={len(excluded)}, "
          f"отсеяно GitHub repo={len(lifecycle_dropped)}")
    for full_name in lifecycle_dropped[:8]:
        print(f"  [lifecycle] уже решено/показано: {full_name}")

    # ветка «глубинного интернета»: приёмы, акции, истории — человеку, а не в песочницу.
    # Гейт новизны против прошлых выпусков живёт на ЯД (у раннера своего состояния нет).
    if digest_items:
        nov_local = "grok_novelty.json"
        subprocess.run(["rclone", "copyto", f"{QUEUE}/{nov_local}", nov_local],
                       capture_output=True, text=True)
        nov = NoveltyIndex(Path(nov_local), threshold=0.5)
        fresh = []
        for it in digest_items:
            text = f"{it.get('what', '')} {it.get('why_us', '')}"
            ok, sim, who = nov.is_novel(text)
            if ok:
                fresh.append(it)
                nov.add(str(it.get("source_link", ""))[:80], text)
            else:
                print(f"  [новизна] «{str(it.get('what', ''))[:40]}» ~{sim:.2f} ≈ {who}")
        nov.save()
        subprocess.run(["rclone", "copyto", nov_local, f"{QUEUE}/{nov_local}"],
                       capture_output=True, text=True)
        print(f"[дайджест] новых {len(fresh)} из {len(digest_items)}")
        if fresh:
            send_tg(build_digest(fresh))

    seen = existing_slugs()
    out, uniq = [], set()
    for u in analyst_urls:
        u = u.rstrip("/")
        if u in uniq or slug(u) in seen:
            continue
        uniq.add(u)
        out.append(u)
    if not out:
        print("нет новых ссылок для анализатора (всё уже проверено или файла нет)")
        return
    out = out[:15]
    date = datetime.now().strftime("%Y-%m-%d")
    open("hunt.txt", "w").write("\n".join(out) + "\n")
    subprocess.run(["rclone", "copyto", "hunt.txt", f"{QUEUE}/grok_{date}.txt"], check=True)
    print(f"в очередь: {len(out)} URL → {QUEUE}/grok_{date}.txt")
    for u in out:
        print("  +", u)
    # диспатч auto_analyst (пустой targets → --from-queue; тред 1653 GROK SCOUT)
    body = json.dumps({"ref": "main", "inputs": {"targets": "", "thread": GROK_THREAD}}).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{RENDER_REPO}/actions/workflows/auto_analyst.yml/dispatches",
        data=body, method="POST",
        headers={"Authorization": f"token {GH_TOKEN}", "User-Agent": "curl/8.0",
                 "Accept": "application/vnd.github+json"})
    try:
        urllib.request.urlopen(req, timeout=30)
        print(f"auto_analyst диспатчнут (--from-queue, тред {GROK_THREAD})")
    except Exception as e:
        print(f"dispatch fail (очередь подхватит cron): {str(e)[:140]}")


if __name__ == "__main__":
    main()
