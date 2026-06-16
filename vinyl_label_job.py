#!/usr/bin/env python3
"""
vinyl_label_job.py — GH-рендер «винил-визуалайзер с лого-лейблом».

Композит (целевое наложение, [[feedback_no_footage_on_footage]]):
  фон  = видео из клипа (fill.mp4) — за пределами пластинки;
  лейбл= ЛОГО (logo.png), вращается в ритме винила (целевая заливка зелёной зоны);
  верх = винил (vinyl.mp4) с вырезанным по хромакею зелёным, поверх фона+лого;
  старт= титр «артист — трек» (фейд-аут);
  аудио= окно трека на дропе (highlight — интро пропускается).
Один футаж + лого + фон — БЕЗ мешанины слоёв/оверлеев.

Вход (ЯД render_jobs/<JOB_ID>/): vinyl.mp4, fill.mp4, logo.png, track.mp3, params.json.
params.json: {label_cx,label_cy,label_r (норм 0..1), chroma, rot, reel_dur, artist, track}
Выход: result.mp4 + status.txt.

Environment: JOB_ID
"""
import json
import math
import os
import subprocess
import sys
from pathlib import Path

from analyze import find_highlight_offset

JOB_ID = os.environ.get("JOB_ID", "")
if not JOB_ID:
    sys.exit("JOB_ID not set")
REMOTE = "ydrive"
CF = "Content factory"
JOB_YD = f"{CF}/render_jobs/{JOB_ID}"
WORK = Path("/tmp/vlabel"); WORK.mkdir(parents=True, exist_ok=True)
W, H = 1080, 1920
COVER = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def yd_get(rel: str, local: Path) -> bool:
    local.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.run(["rclone", "copyto", f"{REMOTE}:{JOB_YD}/{rel}", str(local)],
                          capture_output=True, text=True).returncode == 0


def yd_put(local: Path, rel: str) -> bool:
    return subprocess.run(["rclone", "copyto", str(local), f"{REMOTE}:{JOB_YD}/{rel}"],
                          capture_output=True, text=True).returncode == 0


def main():
    print(f"Job: {JOB_ID}", flush=True)
    need = {"vinyl": "vinyl.mp4", "fill": "fill.mp4", "logo": "logo.png",
            "track": "track.mp3", "params": "params.json"}
    got = {}
    for k, fn in need.items():
        dst = WORK / fn
        if not yd_get(fn, dst):
            sys.exit(f"нет {fn} в job-папке")
        got[k] = dst
    p = json.loads(got["params"].read_text(encoding="utf-8"))

    reel = float(p.get("reel_dur", 22))
    chroma = p.get("chroma", "0x00D600")
    rot = float(p.get("rot", 1.13))                  # рад/с (≈0.18 об/с ≈ 11 rpm)
    LX = int(float(p["label_cx"]) * W)
    LY = int(float(p["label_cy"]) * H)
    LD = int(float(p["label_r"]) * 2 * W)
    LS = int(LD * 1.45)                              # лого крупнее лейбла → круг закрыт при вращении
    artist = p.get("artist", "yaromat")
    track = p.get("track", "")
    print(f"  label center=({LX},{LY}) diam={LD} logo={LS} rot={rot}рад/с reel={reel}с", flush=True)

    # титр: артист крупнее ([[feedback_brand_name]]), строчными; фейд-аут к 3.6с
    def dt(text, y, size, t_end=3.6):
        esc = text.replace(":", r"\:").replace("'", r"\\'")
        return (f"drawtext=fontfile={FONT}:text='{esc}':fontcolor=white:fontsize={size}:"
                f"x=(w-text_w)/2:y={y}:shadowcolor=black@0.6:shadowx=2:shadowy=2:"
                f"alpha='if(lt(t,{t_end-0.6}),1,if(lt(t,{t_end}),({t_end}-t)/0.6,0))'")

    title = f"{dt(artist, int(H*0.30), 76)},{dt(track, int(H*0.30)+96, 52)}" if track \
        else dt(artist, int(H*0.30), 76)

    fc = (
        f"[0:v]{COVER},eq=saturation=0.55:contrast=1.05,setsar=1[bg];"
        f"[2:v]format=rgba,scale={LS}:{LS},rotate=a='t*{rot}':c=none@0.0:ow={LS}:oh={LS}[logo];"
        f"[bg][logo]overlay={LX-LS//2}:{LY-LS//2}[bl];"
        f"[1:v]{COVER},chromakey={chroma}:0.16:0.10,setsar=1[rec];"
        f"[bl][rec]overlay[comp];"
        f"[comp]{title},format=yuv420p,fps=25[v]"
    )

    try:
        hl = find_highlight_offset(str(got["track"]), window=reel)
    except Exception as e:
        print(f"  highlight err ({e}) → 0", flush=True); hl = 0.0
    print(f"  highlight={hl:.1f}с (интро отрезано)", flush=True)

    result = WORK / "result.mp4"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-stream_loop", "-1", "-i", str(got["fill"]),
           "-stream_loop", "-1", "-i", str(got["vinyl"]),
           "-stream_loop", "-1", "-i", str(got["logo"]),
           "-ss", f"{hl:.3f}", "-i", str(got["track"]),
           "-filter_complex", fc, "-map", "[v]", "-map", "3:a",
           "-t", f"{reel:.3f}", "-r", "25",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
           "-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p",
           "-movflags", "+faststart", str(result)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  ffmpeg err: {r.stderr[-500:]}", flush=True)
        (WORK / "status.txt").write_text("error: ffmpeg failed")
        yd_put(WORK / "status.txt", "status.txt")
        sys.exit("render failed")

    sz = result.stat().st_size // 1024
    print(f"✅ result.mp4 {sz}KB", flush=True)
    yd_put(result, "result.mp4")
    (WORK / "status.txt").write_text(f"done: винил+лого-лейбл, {reel:.0f}с, vertical, {sz}KB")
    yd_put(WORK / "status.txt", "status.txt")


if __name__ == "__main__":
    main()
