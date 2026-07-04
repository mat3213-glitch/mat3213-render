#!/usr/bin/env python3
"""
scene_dispatch.py — Фаза 1, Стадия 4: детерминированная раздача сцен по AI-генераторам.

Читает storyboard.json (от director.py), для каждого шота строит промпт из intent/visual/
imagery_cues, диспатчит генерацию сцены на GH Actions (submit_render_job.py), синхронно ждёт
результат, гейтит через plastic_gate_core.judge_media() (ретрай до MAX_RETRIES при REJECT),
переписывает shot.base на {"kind":"generated","path":"generated/scene_<idx>.mp4"}.

РОУТИНГ ПО ЗНАЧИМОСТИ КАДРА (вариант C, развилка 2026-07-04, я+mimo-local независимо сошлись):
climax-шоты (несут вес истории) → свежая AI-генерация как раньше; intro/body/outro (атмосфера/
переходы, большинство кадров по счёту в energy-сегментированной раскадровке) → подбор из уже
накопленного и протегированного AI-пула (pool_matcher.py/pool_tagger.py), НЕ новая генерация.
Обоснование: LLM режиссёра сам решает число кадров по энергосегментам ПОЛНОГО трека (десятки
шотов на 2-4-мин трек) — прогнать ВСЕ через генерацию упёрлось бы в лимит VeoFree (1 ген/IP/
прогон) и дневную квоту Hunyuan на первом же реальном треке (та же стена, что уже ловили).
hero_object для пул-подбора берётся из archetypes/library.yaml по storyboard["archetype_id"]
(passthrough уже есть в director.py::assemble(), схема storyboard.json не менялась). Пул-клип
гейтится тем же plastic_gate_core.judge_media() перед использованием (пул не панацея —
найдены реальные пробелы в его собственном гейте на тегировании, см. project_screenplay_pipeline)
и заливается в ТОТ ЖЕ generated/scene_<idx>.mp4 — storyboard_render_job.py не меняется вообще
(pool-клип неотличим от AI-generated на этом этапе). --all-generated возвращает старое
поведение (все шоты через AI-генерацию), если понадобится полный дорогой прогон.

v1 генератора: round-robin Qwen/VeoFree/Hunyuan (sp_scene_*.yml, тот же паттерн). Никакого LLM
в этом файле — только детерминированная логика (по принципу пайплайна).

Usage:
  python3 scene_dispatch.py --storyboard path/to/storyboard.json --job-id JOB_ID \\
    [--max-retries 3] [--poll 15] [--timeout 1200] [--all-generated]
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
import pool_matcher
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


def load_hero_object(archetype_id: str) -> str:
    """archetypes/library.yaml — тот же файл, что уже читают screenwriter.py/director.py.
    hero_object передаётся через archetype_id (passthrough, уже есть в storyboard.json).
    Fallback пустой строкой — main() подставит central_motif (ручные/тестовые storyboard
    без archetype_id, как пул-рил этой сессии, не должны падать)."""
    if not archetype_id:
        return ""
    lib_path = Path(__file__).resolve().parent.parent / "archetypes" / "library.yaml"
    if not lib_path.exists():
        return ""
    try:
        import yaml
        data = yaml.safe_load(lib_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return ""
    for a in data.get("archetypes", []):
        if a.get("id") == archetype_id:
            return a.get("hero_object", "")
    return ""


def pool_fill_shot(job_id: str, idx: int, shot: dict, hero_object: str,
                    tmpdir: Path, used_pool_ids: set) -> dict | None:
    """Атмосферный/переходный шот (section != climax) → подбор из тегированного AI-пула
    (pool_matcher.py), НЕ свежая генерация. used_pool_ids не даёт повторить один физический
    клип дважды в одном треке (мотив повторяется, конкретные кадры — разные, per yaromat).
    Гейтится тем же judge_media() перед использованием (пул сам по себе не гарантия — нашли
    реальный пробел в его же ночном гейте на тегировании этой сессией)."""
    cues = shot.get("imagery_cues") or []
    scale = shot.get("scale")
    cand = pool_matcher.pick(hero_object=hero_object, imagery_cues=cues, scale=scale,
                             n=6, seed=f"{job_id}-{idx}")
    if not cand:  # scale-фильтр может быть слишком строг — второй заход без него
        cand = pool_matcher.pick(hero_object=hero_object, imagery_cues=cues, n=6,
                                 seed=f"{job_id}-{idx}")
    cand = [c for c in cand if c["id"] not in used_pool_ids]
    if not cand:
        print(f"  scene {idx}: пул не дал кандидатов под hero={hero_object!r} — пропуск",
              file=sys.stderr)
        return None

    for entry in cand[:3]:  # пробуем до 3 кандидатов, если первый не пройдёт гейт
        local = pool_matcher.fetch(entry, tmpdir)
        verdict = pgc.judge_media(str(local), threshold=GATE_THRESHOLD)
        print(f"  scene {idx} [pool:{entry['engine']}] {entry['id']}: "
              f"gate={verdict['verdict']} score={verdict['score']}")
        if verdict["verdict"] == "REJECT":
            continue
        used_pool_ids.add(entry["id"])
        dest_remote = f"{YD_ROOT}/cloud_io/render_jobs/{job_id}/generated/scene_{idx}.mp4"
        r = subprocess.run(["rclone", "copyto", str(local), dest_remote],
                          capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  scene {idx}: не залил pool-клип на ЯД: {r.stderr[:120]}", file=sys.stderr)
            return None
        return {"kind": "generated", "path": f"generated/scene_{idx}.mp4",
                "engine": f"pool:{entry['engine']}", "pool_id": entry["id"]}

    print(f"  scene {idx}: все пул-кандидаты REJECT на гейте", file=sys.stderr)
    return None


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
    ap.add_argument("--all-generated", action="store_true",
                    help="старое поведение: ВСЕ шоты через AI-генерацию, пул не участвует "
                         "(дорого — упрётся в лимиты на реальном треке, см. докстринг)")
    ap.add_argument("-o", "--out", help="куда писать обновлённый storyboard.json (default: рядом)")
    args = ap.parse_args()

    sb_path = Path(args.storyboard)
    storyboard = json.loads(sb_path.read_text(encoding="utf-8"))
    shots = storyboard.get("shots", [])
    if not shots:
        sys.exit("storyboard без shots")
    ratio = RATIO_BY_FORMAT.get(storyboard.get("format", "square"), "1:1")

    archetype_id = storyboard.get("archetype_id", "")
    hero_object = load_hero_object(archetype_id) or storyboard.get("central_motif", "")
    print(f"hero_object для пул-подбора: {hero_object!r} (archetype_id={archetype_id!r})")

    tmpdir = Path(tempfile.mkdtemp(prefix="scene_dispatch_"))
    used_pool_ids: set = set()
    ok, failed, hunyuan_calls = 0, 0, 0
    for shot in shots:
        idx = shot.get("idx")
        if idx is None:
            continue
        section = shot.get("section", "")
        use_pool = (section != "climax") and not args.all_generated

        if use_pool:
            base = pool_fill_shot(args.job_id, idx, shot, hero_object, tmpdir, used_pool_ids)
        else:
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
                hunyuan_calls += args.max_retries  # верхняя оценка (сколько попыток реально ушло — не знаем)

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
