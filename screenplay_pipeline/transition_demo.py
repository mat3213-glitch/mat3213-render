#!/usr/bin/env python3
"""
transition_demo.py — гейт-демо transition-router (Ф3): показывает 4 приёма стыка,
которые роутер выбирает по контексту. Каждый стык подписан (тип + условие).

Собирает 4 join'а (пара клипов → переход) через встроенный ffmpeg xfade
(gl-dissolve=fade, glitch=pixelize, film-burn=fadegrays) + hard-cut=concat,
конкатит, кладёт аудио трека. Отдаём в TG (плеер ЯД ненадёжен для ревью).
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import transition_router as tr

YD = "ydrive:Content factory"
W, H, FPS = 720, 1280, 25
SEG = 2.5
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# (тип, подпись-условие) — по одному репрезентативному контексту на приём
DEMOS = [
    ("gl-dissolve", "GL-DISSOLVE  intro/atmo"),
    ("dip",         "DIP  body/high"),
    ("film-burn",   "FILM-BURN  смена типа"),
    ("hard-cut",    "HARD-CUT  climax/high"),
]


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def pick_clips(n, work):
    cat = sh(["rclone", "cat", f"{YD}/cloud_io/ai_pool_catalog.jsonl"]).stdout
    rows = [json.loads(l) for l in cat.splitlines() if l.strip()]
    vids, seen, clips = [], set(), []
    for r in rows:
        if r.get("ext") == ".mp4" and (r.get("engine"), r.get("date")) not in seen:
            vids.append(r); seen.add((r.get("engine"), r.get("date")))
    for i, r in enumerate(vids):
        if len(clips) >= n:
            break
        dst = work / f"c_{i}.mp4"
        if sh(["rclone", "copyto", f"{YD}/{r['path']}", str(dst)]).returncode == 0:
            clips.append(dst)
    return clips


def norm(clip, out):
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
          f"fps={FPS},setsar=1,format=yuv420p")
    sh(["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(clip), "-t", f"{SEG:.3f}",
        "-vf", vf, "-an", "-r", str(FPS), "-vsync", "cfr", "-g", str(FPS * 2),
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(out)])


def build_join(a, b, ttype, label, out, work, idx):
    na, nb = work / f"na{idx}.mp4", work / f"nb{idx}.mp4"
    norm(a, na); norm(b, nb)
    xf = tr.xfade_name(ttype)
    lbl = f"drawtext=fontfile={FONT}:text='{label}':fontcolor=white:fontsize=44:" \
          f"x=(w-tw)/2:y=90:box=1:boxcolor=black@0.55:boxborderw=12"
    if xf is None:  # hard-cut → concat, затем подпись
        tmp = work / f"hc{idx}.mp4"
        lst = work / f"l{idx}.txt"; lst.write_text(f"file '{na}'\nfile '{nb}'\n")
        sh(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(tmp)])
        sh(["ffmpeg", "-y", "-i", str(tmp), "-vf", lbl, "-r", str(FPS),
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(out)])
    else:
        tdur = tr.transition_duration()
        off = SEG - tdur
        fc = (f"[0:v][1:v]xfade=transition={xf}:duration={tdur}:offset={off:.3f}[x];"
              f"[x]{lbl}[v]")
        sh(["ffmpeg", "-y", "-i", str(na), "-i", str(nb), "-filter_complex", fc,
            "-map", "[v]", "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast",
            "-pix_fmt", "yuv420p", str(out)])
    return out.exists()


def main():
    audio = sys.argv[1] if len(sys.argv) > 1 else "track_audio"
    work = Path(tempfile.mkdtemp(prefix="trdemo_"))
    clips = pick_clips(len(DEMOS) + 1, work)
    if len(clips) < 2:
        print("[trdemo] мало клипов", file=sys.stderr); return 1

    joins = []
    for i, (ttype, label) in enumerate(DEMOS):
        j = work / f"join_{i}.mp4"
        a, b = clips[i % len(clips)], clips[(i + 1) % len(clips)]
        if build_join(a, b, ttype, label, j, work, i):
            joins.append(j)
    if len(joins) < 2:
        print("[trdemo] мало join'ов", file=sys.stderr); return 1

    lst = work / "joins.txt"
    lst.write_text("\n".join(f"file '{j}'" for j in joins))
    silent = work / "silent.mp4"
    sh(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-r", str(FPS),
        str(silent)])   # ре-энкод concat (единый PTS)

    out = Path("transition_demo.mp4")
    r = sh(["ffmpeg", "-y", "-i", str(silent), "-ss", "20", "-i", audio,
            "-map", "0:v", "-map", "1:a", "-shortest",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", str(out)])
    if not out.exists():
        print(f"[trdemo] мукс упал: {r.stderr[-400:]}", file=sys.stderr); return 1
    print(f"[trdemo] ✅ {out} ({out.stat().st_size//1024}КБ), {len(joins)} приёмов")
    return 0


if __name__ == "__main__":
    sys.exit(main())
