#!/usr/bin/env python3
"""prompt_memory.py — что показывает лента «вход → исход» по всем прогонам пула.

ЗАЧЕМ. Четыре недели подряд диагноз один: виноват ВХОД (промпты/исходники), не движок.
Но до сих пор мы не знали, КАКОЙ именно вход плох — пул чистился глазами, и знание
уходило вместе с сессией. `arts_pool_job.py` теперь пишет в ленту
`cloud_io/prompt_memory/prompt_stats.jsonl` по записи на каждый сгенерированный арт:
субъект, лук, хэш промпта, провайдер и вердикт судьи. Этот скрипт её читает.

Запуск:
    python3 prompt_memory.py                 # сводка по всей ленте
    python3 prompt_memory.py --top 15        # сколько строк показывать
    python3 prompt_memory.py --local лента.jsonl
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict

YD = "ydrive:Content factory"
REMOTE = "cloud_io/prompt_memory/prompt_stats.jsonl"


def load(local=None):
    path = local
    if not path:
        path = os.path.join(tempfile.mkdtemp(), "prompt_stats.jsonl")
        r = subprocess.run(["rclone", "copyto", f"{YD}/{REMOTE}", path],
                           capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(path):
            sys.exit("ленты ещё нет — она появится после первого прогона пула с судьёй")
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue          # битую строку пропускаем, лента append-only
    return rows


def rate(rows):
    """Доля отбраковки. PARTIAL/ERROR не считаем ни в числитель, ни в знаменатель:
    это «судья не смог», а не свойство промпта — иначе сбой квоты выглядел бы как плохой вход."""
    judged = [r for r in rows if r.get("verdict") in ("OK", "FLAG", "REJECT")]
    rej = [r for r in judged if r["verdict"] == "REJECT"]
    return len(rej), len(judged)


def group(rows, key):
    out = defaultdict(list)
    for r in rows:
        k = r.get(key)
        if k:
            out[k].append(r)
    return out


def table(title, groups, top, min_n=3):
    print(f"\n## {title}")
    items = []
    for k, rows in groups.items():
        rej, n = rate(rows)
        if n >= min_n:
            items.append((rej / n, rej, n, k))
    if not items:
        print("  (мало данных — нужно минимум 3 судейства на позицию)")
        return
    for share, rej, n, k in sorted(items, reverse=True)[:top]:
        bar = "█" * int(share * 20)
        print(f"  {share*100:5.1f}%  {rej:>3}/{n:<3} {bar:<20} {str(k)[:60]}")


def main():
    ap = argparse.ArgumentParser(description="Сводка памяти промптов")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--local", default="")
    args = ap.parse_args()

    rows = load(args.local or None)
    rej, judged = rate(rows)
    unjudged = sum(1 for r in rows if r.get("verdict") in ("PARTIAL", "ERROR"))
    jobs = len({r.get("job") for r in rows})
    print(f"записей: {len(rows)} · прогонов: {jobs} · отсужено: {judged} · "
          f"брак: {rej} ({100*rej//max(judged,1)}%) · судья не смог: {unjudged}")

    viol = defaultdict(int)
    for r in rows:
        for v in r.get("violations") or []:
            viol[v] += 1
    if viol:
        print("\n## Чем именно бракует")
        for v, c in sorted(viol.items(), key=lambda x: -x[1]):
            print(f"  {c:>4}  {v}")

    table("Субъекты с худшим выходом (кандидаты на переписывание)",
          group(rows, "subject"), args.top)
    table("Луки", group(rows, "look"), args.top)
    table("Провайдеры генерации", group(rows, "provider"), args.top, min_n=1)
    print("\nЧитать так: высокий процент у субъекта = дело не в движке, а в формулировке.")


if __name__ == "__main__":
    main()
