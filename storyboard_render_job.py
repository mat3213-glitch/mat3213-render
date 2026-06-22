#!/usr/bin/env python3
"""
storyboard_render_job.py — GitHub Actions рендер по РАСКАДРОВКЕ режиссёра (director.py).

Вшивает storyboard.json в рендер: каждый кадр = base-клип каталога (cover под формат,
trim/loop под t_dur) + overlay-клип каталога (screen-бленд). Кадры конкатятся в порядке
раскадровки, под них кладётся аудио-окно трека на дропе (highlight — интро пропускается).

Источник моторики — сам футаж (винил крутится, волна движется, оверлей течёт) =
фотографичное органичное движение, не синтетика.

Вход (ЯД render_jobs/<JOB_ID>/): storyboard.json, track.mp3.
Клипы каталога тянутся по их path ("footage_catalog/<cat>/ref_*.mp4") прямо с ЯД.
Выход: result.mp4 + status.txt → ЯД render_jobs/<JOB_ID>/.

Environment: JOB_ID
"""
import json
import os
import subprocess
import sys
from pathlib import Path

from analyze import find_highlight_offset

JOB_ID = os.environ.get("JOB_ID", "")
if not JOB_ID:
    sys.exit("JOB_ID not set")

REMOTE   = "ydrive"
CF       = "Content factory"
JOB_YD   = f"{CF}/cloud_io/render_jobs/{JOB_ID}"
WORKDIR  = Path("/tmp/sb_job")
CLIPS    = WORKDIR / "clips"
SHOTS    = WORKDIR / "shots"
for d in (WORKDIR, CLIPS, SHOTS):
    d.mkdir(parents=True, exist_ok=True)

COVER = {
    "vertical":  "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
    "square":    "scale=1080:1080:force_original_aspect_ratio=increase,crop=1080:1080",
    "landscape": "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080",
}
OVERLAY_OPACITY = 0.45


def yd_get(remote_path: str, local: Path) -> bool:
    local.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["rclone", "copyto", f"{REMOTE}:{remote_path}", str(local)],
                       capture_output=True, text=True)
    return r.returncode == 0


def yd_put(local: Path, remote_path: str) -> bool:
    r = subprocess.run(["rclone", "copyto", str(local), f"{REMOTE}:{remote_path}"],
                       capture_output=True, text=True)
    return r.returncode == 0


def ff(args: list[str]) -> bool:
    r = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  ffmpeg err: {r.stderr[-300:]}", flush=True)
    return r.returncode == 0


def pull_clip(path: str) -> Path | None:
    """Клип каталога по его манифест-path ('footage_catalog/...') с ЯД (кэш в CLIPS)."""
    name = Path(path).name
    local = CLIPS / name
    if local.exists():
        return local
    if yd_get(f"{CF}/assets/{path}", local):
        return local
    print(f"  ✗ не стянул клип {path}", flush=True)
    return None


def render_shot(i: int, shot: dict, cover: str, fill: Path | None) -> Path | None:
    """Кадр = base-футаж. Если у base ЗЕЛЁНАЯ зона (chroma) и есть fill → целевое наложение:
    фон-заливка (арт/он-тема) + винил с вырезанным зелёным сверху. БЕЗ футаж-на-футаж/оверлеев
    ([[feedback_no_footage_on_footage]])."""
    dur = max(0.4, float(shot["t_dur"]))
    base = shot.get("base") or {}
    bpath = base.get("path")
    if not bpath:
        print(f"  shot {i}: нет base.path — пропуск", flush=True)
        return None
    bfile = pull_clip(bpath)
    if not bfile:
        return None
    out = SHOTS / f"shot_{i:03d}.mp4"
    chroma = base.get("chroma")
    common = ["-t", f"{dur:.3f}", "-r", "25", "-pix_fmt", "yuv420p",
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-an", str(out)]
    if chroma and fill:
        fc = (f"[0:v]{cover},fps=25,setsar=1,eq=saturation=0.62:contrast=1.08[bg];"
              f"[1:v]{cover},fps=25,setsar=1,chromakey={chroma}:0.16:0.10[fg];"
              f"[bg][fg]overlay,format=yuv420p[v]")
        ok = ff(["-stream_loop", "-1", "-i", str(fill),
                 "-stream_loop", "-1", "-i", str(bfile),
                 "-filter_complex", fc, "-map", "[v]", *common])
    else:
        ok = ff(["-stream_loop", "-1", "-i", str(bfile),
                 "-vf", f"{cover},fps=25,setsar=1", *common])
    return out if ok else None


def main():
    print(f"Job: {JOB_ID}", flush=True)
    sb_file = WORKDIR / "storyboard.json"
    track   = WORKDIR / "track.mp3"
    if not yd_get(f"{JOB_YD}/storyboard.json", sb_file):
        sys.exit("нет storyboard.json в job-папке")
    if not yd_get(f"{JOB_YD}/track.mp3", track):
        sys.exit("нет track.mp3 в job-папке")
    sb = json.loads(sb_file.read_text(encoding="utf-8"))
    shots = sb.get("shots", [])
    fmt = sb.get("format", "vertical")
    cover = COVER.get(fmt, COVER["vertical"])
    reel_dur = float(sb.get("duration") or sum(float(s["t_dur"]) for s in shots))
    print(f"  кадров={len(shots)} format={fmt} reel≈{reel_dur:.1f}с", flush=True)
    if not shots:
        sys.exit("storyboard без shots")

    # fill для целевой заливки зелёных зон (арт/он-тема). Опционально.
    fill = None
    if sb.get("fill"):
        fcand = WORKDIR / "fill.mp4"
        if yd_get(f"{JOB_YD}/{sb['fill']}", fcand):
            fill = fcand
            print(f"  fill: {sb['fill']} ✓ (заливка зелёных зон)", flush=True)
        else:
            print(f"  ⚠ fill {sb['fill']} не стянут — зелёные зоны останутся", flush=True)

    # 1. рендер кадров
    print("\n── Рендер кадров ──", flush=True)
    rendered = []
    for i, sh in enumerate(shots):
        out = render_shot(i, sh, cover, fill)
        if out:
            rendered.append(out)
            b = sh.get("base") or {}
            tag = f"{b.get('category')}{'+заливка' if (b.get('chroma') and fill) else ''}"
            print(f"  ✓ shot {i}: {sh['t_dur']:.1f}с {tag}", flush=True)
    if not rendered:
        yd_put_status("FAIL: ни один кадр не отрендерился")
        sys.exit("0 кадров")

    # 2. concat (hard cut — v1; xfade/dip = v2)
    print("\n── Concat ──", flush=True)
    lst = WORKDIR / "list.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in rendered), encoding="utf-8")
    concat = WORKDIR / "concat.mp4"
    if not ff(["-f", "concat", "-safe", "0", "-i", str(lst),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
               "-pix_fmt", "yuv420p", str(concat)]):
        sys.exit("concat не вышел")

    # 3. аудио-окно на дропе (интро пропускается через highlight) + mux
    print("\n── Highlight + аудио + mux ──", flush=True)
    try:
        hl = find_highlight_offset(str(track), window=reel_dur)
    except Exception as e:
        print(f"  highlight err ({e}) → 0.0", flush=True)
        hl = 0.0
    print(f"  highlight_offset={hl:.1f}с (интро до него отрезано)", flush=True)
    result = WORKDIR / "result.mp4"
    if not ff(["-i", str(concat), "-ss", f"{hl:.3f}", "-t", f"{reel_dur:.3f}", "-i", str(track),
               "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "21", "-c:a", "aac", "-b:a", "192k", "-shortest",
               "-movflags", "+faststart", str(result)]):
        sys.exit("mux не вышел")

    sz = result.stat().st_size // 1024
    print(f"\n✅ result.mp4 {sz}KB → ЯД", flush=True)
    yd_put(result, f"{JOB_YD}/result.mp4")
    yd_put_status(f"done: {len(rendered)} кадров, {reel_dur:.0f}с, {fmt}, {sz}KB")


def yd_put_status(text: str):
    f = WORKDIR / "status.txt"
    f.write_text(text, encoding="utf-8")
    yd_put(f, f"{JOB_YD}/status.txt")


if __name__ == "__main__":
    main()
