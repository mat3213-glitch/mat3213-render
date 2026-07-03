#!/usr/bin/env python3
"""
scene_dispatch.py — Фаза 1, Стадия 4: детерминированная раздача сцен по AI-генераторам.

Читает storyboard.json (от director.py), для каждого шота строит промпт из intent/visual/
imagery_cues, диспатчит генерацию сцены на GH Actions (submit_render_job.py), синхронно ждёт
результат, гейтит через plastic_gate_core.judge_media() (ретрай до MAX_RETRIES при REJECT),
переписывает shot.base на {"kind":"generated","path":"generated/scene_<idx>.mp4"}.

v1: ВСЕ сцены → Hunyuan (единственный подключённый движок, API-based, самый надёжный).
Точка расширения: ENGINE_FOR() — round-robin Qwen/VeoFree/Hunyuan, когда появятся
sp_scene_qwen.yml/sp_scene_veofree.yml (тот же паттерн, что sp_scene_hunyuan.yml).
Никакого LLM в этом файле — только детерминированная логика (по принципу пайплайна).

Usage:
  python3 scene_dispatch.py --storyboard path/to/storyboard.json --job-id JOB_ID \\
    [--max-retries 3] [--poll 15] [--timeout 1200]
"""

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plastic_gate_core as pgc
from submit_render_job import submit

YD_ROOT = "ydrive:Content factory"
GATE_THRESHOLD = 55
MAX_RETRIES_DEFAULT = 3

RATIO_BY_FORMAT = {"square": "1:1", "vertical": "9:16", "landscape": "16:9"}

ENGINE_WORKFLOWS = {
    "hunyuan": "sp_scene_hunyuan.yml",
    "veofree": "sp_scene_veofree.yml",   # i2v, watermark-free, ВСЕГДА выход 9:16 (см. project_video_gen_veofree)
    "qwen":    "sp_scene_qwen.yml",      # t2v, квота ~4-5 видео/день
}
ENGINE_ORDER = ["hunyuan", "veofree", "qwen"]  # round-robin по кругу; hunyuan первый пока не убедимся, что квота не исчерпана


def engine_for(shot: dict) -> str:
    """Round-robin по idx — распределяет нагрузку между движками (устойчивость через
    разнообразие: если один упёрся в дневной лимит/500, другие сцены не встанут)."""
    idx = shot.get("idx", 0)
    return ENGINE_ORDER[idx % len(ENGINE_ORDER)]


# Qwen (t2v, "с нуля") ловлен вживую 2026-07-03 на генерации ЧИТАЕМОГО ТЕКСТА на предметах
# (билет с надписями Departure/Platform/дата) — нарушает бренд-правило «без текста в кадре».
# У chat.qwen.ai нет отдельного negative-prompt поля (единственное текстовое окно) — единственный
# рычаг: усилить запрет прямо в промпте, с двух сторон (начало+конец — модели лучше держат края).
QWEN_TEXT_SUPPRESS_PREFIX = "no readable text, no writing, no labels, no captions, blank surfaces, "
QWEN_TEXT_SUPPRESS_SUFFIX = ", absolutely no text or lettering anywhere in frame, unmarked blank objects"


def engine_inputs(engine: str, idx: int, prompt: str, still_query: str, ratio: str) -> dict:
    """У каждого движка свой набор workflow_dispatch inputs — gh CLI падает на незаявленных -f."""
    if engine == "hunyuan":
        return {"scene_idx": str(idx), "prompt": prompt, "ratio": ratio, "still_query": still_query}
    if engine == "veofree":
        return {"scene_idx": str(idx), "prompt": prompt, "still_query": still_query, "aspect": "9:16"}
    if engine == "qwen":
        qwen_prompt = QWEN_TEXT_SUPPRESS_PREFIX + prompt + QWEN_TEXT_SUPPRESS_SUFFIX
        return {"scene_idx": str(idx), "prompt": qwen_prompt, "ratio": ratio}
    return {"scene_idx": str(idx), "prompt": prompt}


def build_scene_prompt(shot: dict) -> str:
    intent = shot.get("intent", "")
    visual = shot.get("visual", "") or shot.get("base", {}).get("query", "")
    cues = shot.get("imagery_cues") or []
    parts = [p for p in [visual, intent] + list(cues) if p]
    return ", ".join(parts)[:800]


def build_still_query(shot: dict) -> str:
    """Короткий запрос для Openverse (сток-стилл под Hunyuan i2v). Openverse (как YouTube Data
    API) не любит длинные многословные запросы — держим 3-4 слова, берём только 'visual' (не
    intent/cues, те часто на русском или слишком абстрактны для стокового поиска)."""
    visual = shot.get("visual", "") or shot.get("base", {}).get("query", "")
    words = [w for w in visual.replace(",", " ").split() if w.isascii()]
    return " ".join(words[:4]) or "abstract atmospheric texture"


def rclone_exists(remote_path: str) -> bool:
    r = subprocess.run(["rclone", "lsf", remote_path], capture_output=True, text=True)
    return r.returncode == 0 and bool(r.stdout.strip())


def rclone_pull(remote_path: str, local: Path) -> bool:
    r = subprocess.run(["rclone", "copyto", remote_path, str(local)], capture_output=True, text=True)
    return r.returncode == 0 and local.exists()


def wait_for_scene(job_id: str, idx: int, timeout: int, poll: int, tmpdir: Path) -> Path | None:
    """Поллит ЯД до появления scene_<idx>.mp4 ИЛИ scene_<idx>.status.txt (ошибка)."""
    base = f"{YD_ROOT}/cloud_io/render_jobs/{job_id}/generated"
    scene_remote = f"{base}/scene_{idx}.mp4"
    err_remote = f"{base}/scene_{idx}.status.txt"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if rclone_exists(err_remote):
            print(f"  scene {idx}: воркфлоу упал (status.txt)", file=sys.stderr)
            return None
        if rclone_exists(scene_remote):
            local = tmpdir / f"scene_{idx}.mp4"
            if rclone_pull(scene_remote, local):
                return local
        time.sleep(poll)
    print(f"  scene {idx}: таймаут ({timeout}с) — сцена не появилась", file=sys.stderr)
    return None


def dispatch_and_gate(job_id: str, idx: int, shot: dict, ratio: str, timeout: int, poll: int,
                      max_retries: int, tmpdir: Path) -> dict | None:
    """Диспатчит сцену, ждёт, гейтит, ретраит при REJECT. Возвращает готовый base-dict или None."""
    engine = engine_for(shot)
    workflow = ENGINE_WORKFLOWS.get(engine)
    if not workflow:
        print(f"  scene {idx}: движок '{engine}' не подключён (нет workflow)", file=sys.stderr)
        return None
    prompt = build_scene_prompt(shot)
    still_query = build_still_query(shot)

    for attempt in range(1, max_retries + 1):
        print(f"  scene {idx} [{engine}] попытка {attempt}/{max_retries}: {prompt[:80]}...")
        try:
            submit(job_id=job_id, files={}, workflow=workflow,
                  inputs=engine_inputs(engine, idx, prompt, still_query, ratio))
        except RuntimeError as e:
            print(f"  scene {idx}: dispatch failed: {e}", file=sys.stderr)
            continue

        local = wait_for_scene(job_id, idx, timeout, poll, tmpdir)
        if not local:
            continue

        verdict = pgc.judge_media(str(local), threshold=GATE_THRESHOLD)
        print(f"  scene {idx}: gate={verdict['verdict']} score={verdict['score']} ({verdict['reason']})")
        if verdict["verdict"] != "REJECT":
            return {"kind": "generated", "path": f"generated/scene_{idx}.mp4", "engine": engine}
        # реджект → почистить перед ретраем, чтобы след. попытка не подобрала старый файл по ошибке
        subprocess.run(["rclone", "deletefile",
                       f"{YD_ROOT}/cloud_io/render_jobs/{job_id}/generated/scene_{idx}.mp4"],
                      capture_output=True)

    print(f"  scene {idx}: все {max_retries} попыток REJECT/fail", file=sys.stderr)
    return None


def main():
    ap = argparse.ArgumentParser(description="Раздача сцен по AI-генераторам (Фаза 1, Стадия 4).")
    ap.add_argument("--storyboard", required=True, help="путь к storyboard.json")
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--max-retries", type=int, default=MAX_RETRIES_DEFAULT)
    ap.add_argument("--poll", type=int, default=15, help="интервал поллинга ЯД (с)")
    ap.add_argument("--timeout", type=int, default=1200, help="таймаут на сцену (с)")
    ap.add_argument("--max-hunyuan-per-run", type=int, default=8,
                    help="консервативный потолок вызовов Hunyuan за один прогон (у Hunyuan есть "
                         "дневной лимит генераций, точное число не известно — yaromat 2026-07-03, "
                         "начинаем осторожно, не бьём пачками/параллельно)")
    ap.add_argument("-o", "--out", help="куда писать обновлённый storyboard.json (default: рядом)")
    args = ap.parse_args()

    sb_path = Path(args.storyboard)
    storyboard = json.loads(sb_path.read_text(encoding="utf-8"))
    shots = storyboard.get("shots", [])
    if not shots:
        sys.exit("storyboard без shots")
    ratio = RATIO_BY_FORMAT.get(storyboard.get("format", "square"), "1:1")

    tmpdir = Path(tempfile.mkdtemp(prefix="scene_dispatch_"))
    ok, failed, hunyuan_calls = 0, 0, 0
    for shot in shots:
        idx = shot.get("idx")
        if idx is None:
            continue
        engine = engine_for(shot)
        # дневной лимит Hunyuan не известен точно — консервативный потолок ЗА ПРОГОН, чтобы
        # один трек не сжёг всю дневную квоту (yaromat 2026-07-03). Каждая попытка (включая
        # ретраи) считается отдельным вызовом.
        if engine == "hunyuan" and hunyuan_calls + args.max_retries > args.max_hunyuan_per_run:
            print(f"  scene {idx}: потолок Hunyuan за прогон исчерпан "
                  f"({hunyuan_calls}/{args.max_hunyuan_per_run}) — пропуск", file=sys.stderr)
            failed += 1
            continue
        base = dispatch_and_gate(args.job_id, idx, shot, ratio, args.timeout, args.poll,
                                 args.max_retries, tmpdir)
        if engine == "hunyuan":
            hunyuan_calls += args.max_retries  # верхняя оценка (не знаем сколько попыток реально ушло)
        if base:
            shot["base"] = base
            ok += 1
        else:
            failed += 1

    print(f"\n=== ИТОГ: сцен={len(shots)} сгенерировано={ok} провалено={failed} ===")

    out = Path(args.out) if args.out else sb_path
    out.write_text(json.dumps(storyboard, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"storyboard (обновлённый) → {out}")

    if failed == len(shots):
        sys.exit("все сцены провалены")


if __name__ == "__main__":
    main()
