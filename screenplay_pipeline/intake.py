#!/usr/bin/env python3
"""
intake.py — stage 0 of screenplay pipeline: fetch audio + mini-brief from Yandex.Disk,
run analyze_track, produce brief_full.yaml for screenwriter.py.

Mini-brief is a hand-written YAML (no LLM) with: title, key, core_emotion, visual_mood,
narrative_angle, mood_words, genre, what_to_avoid. BPM and duration come from analyze_track.

Usage:
  python3 intake.py --folder "ydrive:Content factory/cloud_io/track_intake/<slug>" --job-id JOB_ID
"""

import argparse
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from analyze import analyze_track

AUDIO_EXTS = {".mp3", ".wav", ".flac"}
YAML_EXTS = {".yaml", ".yml"}

# ДУБЛИКАТ лексикона из Instrument/ContentAgent/brief_autofill.py (yaromat 2026-07-04, найдено
# на реальном треке "conversation inside" — intake.py был написан 2026-07-03 под ВЫДУМАННЫЙ
# плоский мини-бриф-формат, ни разу не сверенный с реальным music_post_template_v2.yaml,
# который используют ВСЕ треки проекта). github_actions_clips — отдельный git-репо (свой
# чекаут на GH Actions), Instrument/ оттуда не виден — тот же принцип, что уже есть у
# analyze.py ("копия для runner, синхронизировать вручную после каждого изменения").
_EMOTION_LEXICON: dict[str, dict] = {
    "выгорание":     {"palette": "пыльно-серый + блёклый охряной", "texture": "выцветшая плёнка, офисный люминесцент", "words": ["усталость", "оцепенение", "серость", "перегруз", "пустота"]},
    "тоска":         {"palette": "холодный синий + тусклый тил",    "texture": "зерно, дождь по стеклу, дальний свет",      "words": ["потеря", "даль", "сумерки", "тишина", "память"]},
    "надежда":       {"palette": "тёплый янтарный + мягкий кремовый","texture": "засвет, рассеянный свет, мягкое зерно",     "words": ["рассвет", "тепло", "дыхание", "путь", "свет"]},
    "отстранённость":{"palette": "монохром + один холодный акцент",  "texture": "длинная выдержка, motion-смаз, стекло",     "words": ["дистанция", "наблюдатель", "сквозь", "молчание", "глубина"]},
    "эйфория":       {"palette": "контрастный дуотон, выбитые света", "texture": "строб, световые росчерки, высокий контраст","words": ["разгон", "пульс", "вспышка", "поток", "высота"]},
    "тревога":       {"palette": "красный + чёрный, жёсткий контраст","texture": "глитч, рваные края, нервная нарезка",       "words": ["напряжение", "край", "пульс", "разлом", "клаустрофобия"]},
    "нежность":      {"palette": "приглушённый розово-серый + крем",  "texture": "мягкий фокус, плёнка, ручной коллаж",       "words": ["близость", "тепло", "хрупкость", "касание", "тишина"]},
    "одиночество":   {"palette": "глубокий синий + один тёплый блик", "texture": "негативное пространство, дальний якорь",    "words": ["один", "простор", "эхо", "ночь", "окно"]},
    "погружение":    {"palette": "тёплый кремовый + мягкий янтарный", "texture": "мягкий фокус, домашний свет, лёгкое зерно",  "words": ["тепло", "покой", "внутреннее", "дыхание", "свет"]},
}
_DEFAULT_LEX = {"palette": "ограниченная палитра 2–3 цвета", "texture": "плёночное зерно, видна рука (коллаж/scratch)", "words": ["глубина", "внутреннее", "фактура", "ручная работа", "контраст"]}


def _adapt_minimal_brief(mini: dict, title_fallback: str) -> dict:
    """Реальный on-disk формат (music_post_template_v2.yaml): track.title/soul.about/
    soul.core_emotion/optional.avoid — НЕ плоский формат, под который был писан этот файл.
    Возвращает плоский словарь, совместимый с остальной логикой main() ниже."""
    t = mini.get("track", {}) or {}
    soul = mini.get("soul", {}) or {}
    opt = mini.get("optional", {}) or {}

    core = str(soul.get("core_emotion") or "").strip()
    about = str(soul.get("about") or "").strip()
    avoid_raw = str(opt.get("avoid") or "").strip()

    lex = _DEFAULT_LEX
    for k, v in _EMOTION_LEXICON.items():
        if k in core.lower():
            lex = v
            break

    # ВАЖНО: optional.avoid в реальных брифах порой несёт ПОЗИТИВНОЕ пожелание ("хочется
    # показать...") несмотря на название поля (yaromat, "conversation inside" — прямой пример).
    # Дословный дамп в constraints.what_to_avoid перевернул бы смысл на противоположный —
    # screenwriter.py подаёт этот блок в промпт как строгий запрет ("не выдавать такие cue").
    # Безопаснее добавить как доп. контекст к narrative_angle, не как запрет.
    narrative_angle = about
    if avoid_raw:
        narrative_angle = f"{about}\n(доп. пожелание к визуалу: {avoid_raw})".strip()

    return {
        "title": str(t.get("title") or "").strip() or title_fallback,
        "core_emotion": core,
        "visual_mood": f"DRAFT: {lex['palette']}; фактура — {lex['texture']}",
        "narrative_angle": narrative_angle,
        "mood_words": lex["words"],
        "genre": "future garage / downtempo",
        "key": "",
        "what_to_avoid": [],
    }


def _rclone_copy(src: str, dst: str):
    r = subprocess.run(
        ["rclone", "copy", src, dst],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"[rclone] copy failed: {r.stderr[:300]}", file=sys.stderr)
        sys.exit(1)


def _rclone_copyto(src: str, dst: str):
    r = subprocess.run(
        ["rclone", "copyto", src, dst],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"[rclone] copyto failed: {r.stderr[:300]}", file=sys.stderr)
        sys.exit(1)
    print(f"[rclone] uploaded → {dst}")


def main():
    ap = argparse.ArgumentParser(description="Stage 0: fetch track + mini-brief from Yandex.Disk, produce brief_full.yaml.")
    ap.add_argument("--folder", required=True, help="Yandex.Disk folder with one audio + one .yaml mini-brief")
    ap.add_argument("--job-id", required=True, help="Job ID for render_jobs path on Yandex.Disk")
    args = ap.parse_args()

    tmpdir = tempfile.mkdtemp(prefix="intake_")
    print(f"[intake] downloading → {tmpdir}")
    _rclone_copy(args.folder, tmpdir)

    audio_files = [f for f in Path(tmpdir).iterdir() if f.suffix.lower() in AUDIO_EXTS]
    yaml_files = [f for f in Path(tmpdir).iterdir() if f.suffix.lower() in YAML_EXTS]

    if len(audio_files) != 1 or len(yaml_files) != 1:
        print(
            f"[intake] expected exactly 1 audio and 1 yaml in {args.folder}, "
            f"found {len(audio_files)} audio, {len(yaml_files)} yaml",
            file=sys.stderr,
        )
        sys.exit(1)

    audio_path = audio_files[0]
    yaml_path = yaml_files[0]
    print(f"[intake] audio: {audio_path.name}, brief: {yaml_path.name}")

    mini_brief = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    if not isinstance(mini_brief, dict):
        print("[intake] mini-brief is not a dict", file=sys.stderr)
        sys.exit(1)

    if "track" in mini_brief and "soul" in mini_brief:
        print("[intake] реальный минимальный бриф (music_post_template_v2) — адаптирую через лексикон")
        mini_brief = _adapt_minimal_brief(mini_brief, title_fallback=audio_path.stem)

    for key in ("title", "core_emotion", "visual_mood", "narrative_angle", "mood_words", "genre"):
        if key not in mini_brief:
            print(f"[intake] mini-brief missing required key: {key}", file=sys.stderr)
            sys.exit(1)

    bpm, segments = analyze_track(str(audio_path), duration=None)
    print(f"[intake] BPM={bpm:.1f}, {len(segments)} segments")

    energy_order = []
    seen = set()
    for seg in segments:
        if seg.energy not in seen:
            seen.add(seg.energy)
            energy_order.append(seg.energy)
    avg_energy = Counter(seg.energy for seg in segments).most_common(1)[0][0]

    last_seg = segments[-1]
    total_duration = round(last_seg.track_pos + last_seg.duration, 2)

    brief_full = {
        "track": {
            "title": mini_brief["title"],
            "bpm": round(bpm, 1),
            "key": mini_brief.get("key", ""),
            "duration": total_duration,
        },
        "content": {
            "core_emotion": mini_brief["core_emotion"],
            "visual_mood": mini_brief["visual_mood"],
            "narrative_angle": mini_brief["narrative_angle"],
            "mood_words": mini_brief["mood_words"],
        },
        "structure": {},
        "production": {
            "genre": mini_brief["genre"],
        },
        "constraints": {
            "what_to_avoid": mini_brief.get("what_to_avoid", []),
        },
        "audio_features": {
            "energy_profile": energy_order,
            "avg_energy": avg_energy,
        },
    }

    local_brief = Path(tmpdir) / "brief_full.yaml"
    local_brief.write_text(
        yaml.safe_dump(brief_full, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"[intake] brief_full → {local_brief}")

    remote_dir = f"ydrive:Content factory/cloud_io/render_jobs/{args.job_id}"
    _rclone_copyto(str(local_brief), f"{remote_dir}/brief_full.yaml")

    track_ext = audio_path.suffix
    remote_track = f"{remote_dir}/track{track_ext}"
    _rclone_copyto(str(audio_path), remote_track)
    print(f"[intake] done → {remote_dir}")


if __name__ == "__main__":
    main()
