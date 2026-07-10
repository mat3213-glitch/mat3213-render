#!/usr/bin/env python3
"""
whisper_demo_video.py — ВИДЕО-демо корректности whisperx→резы (гейт Ф1b).

Собирает короткий ролик: реальные клипы пула, СМЕНА КЛИПА (рез) на каждом спетом
слове (beat_locked онсет whisperx), поверх РЕАЛЬНОГО аудио трека, со вспышкой +
словом на стыке. yaromat смотрит/слушает: рез совпадает со спетым словом? Это
превью-гейт (НЕ финальный рендер) — визуальная проверка тайминга виспера.

Выход: whisper_demo.mp4 → ЯД гейт-папка.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import vocal_sync

YD = "ydrive:Content factory"
W, H, FPS = 720, 1280, 25
MERGE_GAP = 0.40      # онсеты ближе этого сливаем (не мельчить резами)
MAX_CUTS = 14
PAD_END = 2.0


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def pull(rel, dest):
    return sh(["rclone", "copyto", f"{YD}/{rel}", str(dest)]).returncode == 0


def pick_pool_clips(n: int, work: Path) -> list[Path]:
    """N разных видео-клипов пула из ai_pool_catalog (только mp4, разные даты/движки)."""
    cat = sh(["rclone", "cat", f"{YD}/cloud_io/ai_pool_catalog.jsonl"]).stdout
    rows = [json.loads(l) for l in cat.splitlines() if l.strip()]
    vids, seen = [], set()
    for r in rows:
        if r.get("ext") == ".mp4":
            key = (r.get("engine"), r.get("date"))
            if key not in seen:
                vids.append(r); seen.add(key)
    clips = []
    for i, r in enumerate(vids):
        if len(clips) >= n:
            break
        dst = work / f"clip_{i}.mp4"
        if pull(r["path"], dst):
            clips.append(dst)
    return clips


def safe_word(w: str) -> str:
    w = re.sub(r"[^0-9A-Za-zА-Яа-яЁё ]", "", w).strip().upper()[:18]
    return w or "•"


FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def normalize_segment(clip: Path, dur: float, out: Path):
    """Клип → 720x1280, CFR, ровно dur. БЕЗ текста/вспышки (текст накладываем на
    финальный таймлайн). closed GOP → чистый вход для concat-фильтра (нет фризов)."""
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
          f"fps={FPS},setsar=1,format=yuv420p")
    sh(["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(clip), "-t", f"{dur:.3f}",
        "-vf", vf, "-an", "-r", str(FPS), "-vsync", "cfr",
        "-g", str(FPS * 2), "-keyint_min", str(FPS * 2),
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(out)])


def main() -> int:
    audio = sys.argv[1] if len(sys.argv) > 1 else "track_audio"
    work = Path(tempfile.mkdtemp(prefix="wdemo_"))

    lang, words = vocal_sync.transcribe_words(audio)
    # ДЛЯ ВИДЕО-ДЕМО режем на КАЖДОМ спетом слове (не только beat_locked — тот строгий
    # фильтр нужен director-снапу; здесь цель — глазами/ушами проверить тайминг виспера).
    onset_words = sorted((w for w in words if w.get("start") is not None),
                         key=lambda w: w["start"])
    if len(onset_words) < 2:
        print(f"[demo] слишком мало распознанных слов ({len(onset_words)}) — нечего показывать",
              file=sys.stderr)
        return 1

    # слить близкие онсеты
    cuts = [onset_words[0]]
    for w in onset_words[1:]:
        if w["start"] - cuts[-1]["start"] >= MERGE_GAP:
            cuts.append(w)
    cuts = cuts[:MAX_CUTS]
    win_start = max(0.0, cuts[0]["start"] - 0.3)
    win_end = cuts[-1]["start"] + PAD_END
    print(f"[demo] lang={lang} слов={len(words)} резов={len(cuts)} окно {win_start:.1f}..{win_end:.1f}с")

    clips = pick_pool_clips(len(cuts) + 1, work)
    if len(clips) < 2:
        print("[demo] мало клипов пула", file=sys.stderr); return 1

    # границы сегментов = онсеты (+ конец окна). Нормализуем сегменты (без текста).
    bounds = [c["start"] for c in cuts] + [win_end]
    segs, durs, cut_words = [], [], []
    for i in range(len(cuts)):
        d = bounds[i + 1] - bounds[i]
        if d < 0.15:
            continue
        seg = work / f"seg_{i}.mp4"
        normalize_segment(clips[i % len(clips)], d, seg)
        if seg.exists():
            segs.append(seg); durs.append(d); cut_words.append(cuts[i]["word"])
    if len(segs) < 2:
        print("[demo] мало валидных сегментов", file=sys.stderr); return 1

    # абсолютные видео-времена резов (начало каждого сегмента на финальном таймлайне)
    starts = [0.0]
    for d in durs[:-1]:
        starts.append(round(starts[-1] + d, 3))

    # ОДИН проход: concat-фильтр (непрерывный PTS → нет микрофризов) + текст по
    # абсолютным временам резов (жёсткий синк со звуком, аудио -ss с первого онсета).
    out = Path("whisper_demo.mp4")
    inputs = []
    for s in segs:
        inputs += ["-i", str(s)]
    aidx = len(segs)
    concat_in = "".join(f"[{i}:v]" for i in range(len(segs)))
    fc = f"{concat_in}concat=n={len(segs)}:v=1:a=0[vc]"
    label = "vc"
    for i, (st, w) in enumerate(zip(starts, cut_words)):
        nl = f"v{i}"
        fc += (f";[{label}]drawtext=fontfile={FONT}:text='{safe_word(w)}':fontcolor=white:"
               f"fontsize=54:x=(w-tw)/2:y=h-220:box=1:boxcolor=black@0.55:boxborderw=12:"
               f"enable='between(t,{st:.3f},{st + 0.7:.3f})'[{nl}]")
        label = nl
    cmd = (["ffmpeg", "-y"] + inputs + ["-ss", f"{bounds[0]:.3f}", "-i", audio,
           "-filter_complex", fc, "-map", f"[{label}]", "-map", f"{aidx}:a",
           "-shortest", "-r", str(FPS), "-vsync", "cfr",
           "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "160k", str(out)])
    r = sh(cmd)
    if not out.exists():
        print(f"[demo] сборка упала: {r.stderr[-500:]}", file=sys.stderr); return 1
    print(f"[demo] ✅ {out} ({out.stat().st_size // 1024}КБ), резов={len(segs)}, "
          f"тексты по абс.времени, без вспышки/copy-concat")
    return 0


if __name__ == "__main__":
    sys.exit(main())
