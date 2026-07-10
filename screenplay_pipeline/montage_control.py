#!/usr/bin/env python3
"""
montage_control.py — КОНТРОЛЬНЫЙ тест сборки видео БЕЗ whisper (диагностика).

Та же механика склейки, что у whisper-демо (concat-фильтр + drawtext по абс.времени),
но резы — фиксированная сетка каждые CUT_SEC, подпись — просто номер реза. Whisperx/
librosa НЕ участвуют. Цель — изолировать причину «фризы/текст только в начале»:
  • если и тут фризы/текст ломается → виноват монтаж/клипы/плеер/кэш ЯД, НЕ whisper;
  • если чисто → whisper-путь ни при чём, дело было в разрежённом вокале/кэше.

Выход: control_montage.mp4 (короткий, плотные резы).
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

YD = "ydrive:Content factory"
W, H, FPS = 720, 1280, 25
CUT_SEC = 2.5
TOTAL = 20.0
AUDIO_SS = 20.0   # взять кусок трека с 20с (где уже есть движение/бит)
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def pick_clips(n, work):
    cat = sh(["rclone", "cat", f"{YD}/cloud_io/ai_pool_catalog.jsonl"]).stdout
    rows = [json.loads(l) for l in cat.splitlines() if l.strip()]
    vids, seen = [], set()
    for r in rows:
        if r.get("ext") == ".mp4":
            k = (r.get("engine"), r.get("date"))
            if k not in seen:
                vids.append(r); seen.add(k)
    clips = []
    for i, r in enumerate(vids):
        if len(clips) >= n:
            break
        dst = work / f"c_{i}.mp4"
        if sh(["rclone", "copyto", f"{YD}/{r['path']}", str(dst)]).returncode == 0:
            clips.append((dst, r.get("engine", "?"), r.get("date", "?")))
    return clips


def norm(clip, dur, out):
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
          f"fps={FPS},setsar=1,format=yuv420p")
    sh(["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(clip), "-t", f"{dur:.3f}",
        "-vf", vf, "-an", "-r", str(FPS), "-vsync", "cfr",
        "-g", str(FPS * 2), "-keyint_min", str(FPS * 2),
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(out)])


def main():
    audio = sys.argv[1] if len(sys.argv) > 1 else "track_audio"
    work = Path(tempfile.mkdtemp(prefix="ctrl_"))
    ncuts = int(TOTAL // CUT_SEC)
    clips = pick_clips(ncuts, work)
    if len(clips) < 2:
        print("[ctrl] мало клипов", file=sys.stderr); return 1

    segs, labels = [], []
    for i in range(ncuts):
        seg = work / f"s_{i}.mp4"
        norm(clips[i % len(clips)][0], CUT_SEC, seg)
        if seg.exists():
            segs.append(seg)
            labels.append(f"CUT {i+1}  {clips[i % len(clips)][1]}")
    if len(segs) < 2:
        print("[ctrl] мало сегментов", file=sys.stderr); return 1

    starts = [round(i * CUT_SEC, 3) for i in range(len(segs))]
    inputs = []
    for s in segs:
        inputs += ["-i", str(s)]
    aidx = len(segs)
    fc = "".join(f"[{i}:v]" for i in range(len(segs))) + f"concat=n={len(segs)}:v=1:a=0[vc]"
    label = "vc"
    for i, (st, lab) in enumerate(zip(starts, labels)):
        nl = f"v{i+1}"
        txt = re.sub(r"[^0-9A-Za-z ]", "", lab)
        fc += (f";[{label}]drawtext=fontfile={FONT}:text='{txt}':fontcolor=white:"
               f"fontsize=52:x=(w-tw)/2:y=h-240:box=1:boxcolor=black@0.55:boxborderw=14:"
               f"enable='between(t,{st},{st + CUT_SEC})'[{nl}]")   # держим ВЕСЬ сегмент
        label = nl

    out = Path("control_montage.mp4")
    cmd = (["ffmpeg", "-y"] + inputs + ["-ss", f"{AUDIO_SS}", "-i", audio,
           "-filter_complex", fc, "-map", f"[{label}]", "-map", f"{aidx}:a",
           "-shortest", "-r", str(FPS), "-vsync", "cfr",
           "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "160k", str(out)])
    r = sh(cmd)
    if not out.exists():
        print(f"[ctrl] сборка упала: {r.stderr[-500:]}", file=sys.stderr); return 1
    print(f"[ctrl] ✅ {out} ({out.stat().st_size//1024}КБ), {len(segs)} резов по {CUT_SEC}с, "
          f"подпись держится весь сегмент")
    return 0


if __name__ == "__main__":
    sys.exit(main())
