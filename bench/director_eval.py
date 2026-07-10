#!/usr/bin/env python3
"""
director_eval.py — бенч режиссёра на GH Actions (US-IP → Groq живой).

Гонит на каждом треке из bench/briefs/*.yaml:
  brief.yaml → screenwriter.py → treatment.json
  treatment.json → director (Groq) → storyboard_groq.json
  treatment.json → director (Gemini) → storyboard_gemini.json

До/после каждого director-рана делается 1-токен проба Groq → читаем
x-ratelimit-* заголовки (запас по запросам/токенам). На GH это работает
(на буке = 403 geo-block, для чего и переезд в GH).

Артефакты:
  workspace/<name>/ — brief.yaml (копия), treatment.json, storyboard_groq.json,
                      storyboard_gemini.json, eval_log.json
  REPORT.md — сводка
  Если задана ENV YDRIVE_OUT_PATH, всё это копируется туда rclone'ом.

Запуск:
  python3 bench/director_eval.py                  # все брифы в bench/briefs/
  python3 bench/director_eval.py own              # только один по имени файла
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests
import yaml

BENCH = Path(__file__).resolve().parent          # github_actions_clips/bench/
GHC = BENCH.parent                                # github_actions_clips/
BRIEFS_DIR = BENCH / "briefs"
WS = BENCH / "workspace"

GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")


def probe_groq_limits() -> dict:
    """1-токен запрос к Groq → читаем rate-limit headers."""
    if not GROQ_KEY:
        return {"error": "no GROQ_KEY"}
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}",
                     "Content-Type": "application/json"},
            json={"model": GROQ_MODEL, "max_tokens": 1,
                  "messages": [{"role": "user", "content": "ping"}]},
            timeout=15,
        )
        h = {k.lower(): v for k, v in r.headers.items()}
        return {
            "status": r.status_code,
            "limit_req": h.get("x-ratelimit-limit-requests"),
            "remain_req": h.get("x-ratelimit-remaining-requests"),
            "reset_req": h.get("x-ratelimit-reset-requests"),
            "limit_tok": h.get("x-ratelimit-limit-tokens"),
            "remain_tok": h.get("x-ratelimit-remaining-tokens"),
            "reset_tok": h.get("x-ratelimit-reset-tokens"),
            "body": r.text[:200] if r.status_code != 200 else "",
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def rclone_get(yd_path: str, local: Path) -> bool:
    """Скачать файл с ЯД rclone'ом."""
    local.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["rclone", "copyto", f"ydrive:{yd_path}", str(local)],
        capture_output=True, text=True, timeout=300,
    )
    if r.returncode != 0:
        print(f"!! rclone get failed: {r.stderr[-200:]}", flush=True)
        return False
    return local.exists() and local.stat().st_size > 0


def rclone_put(local: Path, yd_path: str) -> bool:
    """Залить файл/папку на ЯД."""
    if local.is_dir():
        cmd = ["rclone", "copy", str(local), f"ydrive:{yd_path}"]
    else:
        cmd = ["rclone", "copyto", str(local), f"ydrive:{yd_path}"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        print(f"!! rclone put failed: {r.stderr[-200:]}", flush=True)
        return False
    return True


def run_screenwriter(brief: Path, out: Path, references: Path | None = None) -> dict:
    t0 = time.time()
    cmd = [sys.executable, str(GHC / "screenwriter.py"), str(brief), "-o", str(out)]
    if references and references.exists():
        cmd += ["--references", str(references)]
    r = subprocess.run(
        cmd,
        capture_output=True, text=True, timeout=300,
    )
    return {
        "ok": r.returncode == 0 and out.exists(),
        "elapsed": round(time.time() - t0, 1),
        "stdout_tail": r.stdout[-500:],
        "stderr_tail": r.stderr[-500:],
        "rc": r.returncode,
    }


def run_director(treatment: Path, audio: Path, out: Path,
                 force_model: str) -> dict:
    """force_model = 'groq' | 'gemini' — глушим противоположный ключ."""
    env = os.environ.copy()
    if force_model == "groq":
        env["GEMINI_API_KEY"] = ""
    elif force_model == "gemini":
        env["GROQ_API_KEY"] = ""
    t0 = time.time()
    r = subprocess.run(
        [sys.executable, str(GHC / "director.py"), str(treatment),
         "--track", str(audio), "-o", str(out)],
        capture_output=True, text=True, timeout=900, env=env,
    )
    used = "unknown"
    if "раскадровка через Groq" in r.stdout:
        used = "groq"
    elif "раскадровка через Gemini" in r.stdout:
        used = "gemini"
    return {
        "model": force_model,
        "model_used": used,
        "ok": r.returncode == 0 and out.exists(),
        "elapsed": round(time.time() - t0, 1),
        "stdout_tail": r.stdout[-800:],
        "stderr_tail": r.stderr[-500:],
        "rc": r.returncode,
    }


def storyboard_stats(sb_path: Path) -> dict:
    if not sb_path.exists():
        return {"shots": 0}
    try:
        sb = json.loads(sb_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"shots": 0, "parse_error": str(e)}
    shots = sb.get("shots", [])
    motions = [s.get("motion") for s in shots]
    scales = [s.get("scale") for s in shots]
    sections = [s.get("section") for s in shots]
    overlay_ok = [s for s in shots if s.get("overlay")]
    return {
        "shots": len(shots),
        "unique_motion": len(set(filter(None, motions))),
        "unique_scale": len(set(filter(None, scales))),
        "unique_section": len(set(filter(None, sections))),
        "overlay_resolved": len(overlay_ok),
        "motions": dict((m, motions.count(m)) for m in set(filter(None, motions))),
        "scales": dict((s, scales.count(s)) for s in set(filter(None, scales))),
        "sections": dict((s, sections.count(s)) for s in set(filter(None, sections))),
    }


def process_brief(brief_path: Path) -> dict:
    name = brief_path.stem
    print(f"\n=== TRACK: {name} ===", flush=True)
    brief = yaml.safe_load(brief_path.read_text(encoding="utf-8"))
    yd_path = brief.get("track", {}).get("yadrive_path", "")
    title = brief.get("track", {}).get("title", name)

    log = {"name": name, "title": title,
           "brief_file": str(brief_path.relative_to(BENCH))}

    work = WS / name
    work.mkdir(parents=True, exist_ok=True)
    brief_copy = work / "brief.yaml"
    shutil.copy(brief_path, brief_copy)

    audio = work / Path(yd_path).name
    print(f"[1/5] rclone get → {audio.name}", flush=True)
    if not rclone_get(yd_path, audio):
        log["error"] = f"failed to download {yd_path}"
        return log
    log["audio_size_mb"] = round(audio.stat().st_size / 1024 / 1024, 1)

    # опц. reference-пища (стадия 2 screenplay-pipeline): reference_recipes.json с ЯД → сценаристу.
    # fail-open: нет на ЯД → сценарист работает как раньше. [[feedback_screenwriter_needs_reference_food]]
    refs = work / "reference_recipes.json"
    if not rclone_get(f"Content factory/cloud_io/render_jobs/{name}/reference_recipes.json", refs):
        refs = None

    treatment = work / "treatment.json"
    print(f"[2/5] screenwriter → {treatment.name}" + (" (+refs)" if refs else ""), flush=True)
    log["screenwriter"] = run_screenwriter(brief_copy, treatment, refs)
    if not log["screenwriter"]["ok"]:
        log["error"] = "screenwriter failed"
        return log

    print("[3/5] probe Groq (before)", flush=True)
    log["groq_before"] = probe_groq_limits()

    sb_groq = work / "storyboard_groq.json"
    print("[4/5] director через Groq", flush=True)
    log["director_groq"] = run_director(treatment, audio, sb_groq, "groq")
    log["storyboard_groq_stats"] = storyboard_stats(sb_groq)

    print("[5/5] probe Groq (между) + director через Gemini", flush=True)
    log["groq_between"] = probe_groq_limits()

    sb_gem = work / "storyboard_gemini.json"
    log["director_gemini"] = run_director(treatment, audio, sb_gem, "gemini")
    log["storyboard_gemini_stats"] = storyboard_stats(sb_gem)

    log["groq_after"] = probe_groq_limits()

    print(f"   → Groq: {log['storyboard_groq_stats']['shots']} кадров, "
          f"{log['director_groq']['elapsed']}s | "
          f"Gemini: {log['storyboard_gemini_stats']['shots']} кадров, "
          f"{log['director_gemini']['elapsed']}s", flush=True)

    return log


def render_report(results: list[dict]) -> str:
    lines = [
        "# Director Eval (GH Actions) — Groq лимиты + A/B качества",
        "",
        f"Запуск: {time.strftime('%Y-%m-%d %H:%M UTC')}",
        f"Модель Groq: `{GROQ_MODEL}`",
        f"Runner: ubuntu-latest (US-IP — Groq доступен)",
        "",
        "## Лимиты Groq",
        "",
        "| Трек | до | после Groq | после Gemini | потрачено req | потрачено tok |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        if r.get("error"):
            lines.append(f"| {r['name']} | ERROR: {r['error']} | — | — | — | — |")
            continue
        gb, gm, ga = r["groq_before"], r["groq_between"], r["groq_after"]
        try:
            spent_req = int(gb["remain_req"]) - int(ga["remain_req"])
            spent_tok = int(gb["remain_tok"]) - int(ga["remain_tok"])
        except Exception:
            spent_req = spent_tok = "?"
        lines.append(
            f"| {r['name']} "
            f"| {gb.get('remain_req')}/{gb.get('limit_req')} req · "
            f"{gb.get('remain_tok')}/{gb.get('limit_tok')} tok "
            f"| {gm.get('remain_req')} req · {gm.get('remain_tok')} tok "
            f"| {ga.get('remain_req')} req · {ga.get('remain_tok')} tok "
            f"| {spent_req} | {spent_tok} |"
        )

    lines += ["", "## Тайминги", "",
              "| Трек | screenwriter | Groq | Gemini |",
              "|---|---|---|---|"]
    for r in results:
        if r.get("error"):
            continue
        lines.append(
            f"| {r['name']} | {r['screenwriter']['elapsed']}s "
            f"| {r['director_groq']['elapsed']}s "
            f"({r['director_groq'].get('model_used','?')}) "
            f"| {r['director_gemini']['elapsed']}s "
            f"({r['director_gemini'].get('model_used','?')}) |"
        )

    lines += ["", "## Разнообразие storyboard'ов", "",
              "| Трек | модель | кадров | uniq motion | uniq scale | uniq section | overlay |",
              "|---|---|---|---|---|---|---|"]
    for r in results:
        if r.get("error"):
            continue
        for tag, key in [("Groq", "storyboard_groq_stats"),
                         ("Gemini", "storyboard_gemini_stats")]:
            s = r[key]
            lines.append(
                f"| {r['name']} | {tag} | {s.get('shots',0)} "
                f"| {s.get('unique_motion','?')}/7 "
                f"| {s.get('unique_scale','?')}/3 "
                f"| {s.get('unique_section','?')}/4 "
                f"| {s.get('overlay_resolved','?')} |"
            )

    lines += ["", "## Распределение по трекам", ""]
    for r in results:
        if r.get("error"):
            continue
        lines.append(f"### {r['name']}")
        for tag, key in [("Groq", "storyboard_groq_stats"),
                         ("Gemini", "storyboard_gemini_stats")]:
            s = r[key]
            lines.append(f"**{tag}**")
            lines.append(f"- motions: `{s.get('motions', {})}`")
            lines.append(f"- scales: `{s.get('scales', {})}`")
            lines.append(f"- sections: `{s.get('sections', {})}`")
        lines.append("")

    lines += ["## Следующий шаг — слепое сравнение", "",
              "Скачать оба storyboard.json с артефактов раннера, открыть рядом,",
              "выбрать какой кадровый ряд кинематографичнее. Без меток модели — слепо.",
              ""]
    return "\n".join(lines)


def main():
    if not BRIEFS_DIR.exists():
        sys.exit(f"!! no briefs dir: {BRIEFS_DIR}")
    args = sys.argv[1:]
    if args:
        briefs = [BRIEFS_DIR / f"{a}.yaml" for a in args]
    else:
        briefs = sorted(BRIEFS_DIR.glob("*.yaml"))
    if not briefs:
        sys.exit("!! no brief yamls found")

    print(f"Бенч: {len(briefs)} брифов, Groq доступен = {'да' if GROQ_KEY else 'НЕТ ключа'}",
          flush=True)

    results = []
    for b in briefs:
        if not b.exists():
            print(f"!! брифа нет: {b}")
            continue
        r = process_brief(b)
        results.append(r)
        out_log = WS / r["name"] / "eval_log.json"
        out_log.parent.mkdir(parents=True, exist_ok=True)
        out_log.write_text(json.dumps(r, ensure_ascii=False, indent=2))

    report = render_report(results)
    (BENCH / "REPORT.md").write_text(report, encoding="utf-8")
    print(f"\n→ REPORT: {BENCH / 'REPORT.md'}\n")
    print(report)

    yd_out = os.environ.get("YDRIVE_OUT_PATH", "").strip()
    if yd_out:
        print(f"\n→ rclone copy workspace → ydrive:{yd_out}/", flush=True)
        rclone_put(WS, yd_out)
        rclone_put(BENCH / "REPORT.md", f"{yd_out}/REPORT.md")


if __name__ == "__main__":
    main()
