#!/usr/bin/env python3
"""
finish_job.py — GitHub Actions runner: FINISH render для пилота.

Берёт тот же детерминированный монтаж, что CLIP_PRODUCER (seed → идентичный кат),
и добавляет финиш-обработку:
  - посегментная уникализация (зеркало / инверсия / зум по кускам монтажа)
  - грейд (desat + warm-green маска)
  - грязь/шум (grit + scratch оверлеи screen-блендом + noise)
  - тайтл-карта в интро (yaromat крупно / название трека мельче, fade-out к дропу)

job.json:
  {
    "duration": 119, "format": "landscape", "out_name": "finish.mp4",
    "sources": ["src_01", ...], "seed": 33,
    "finish": {
      "mask_color": "0x8a9a60", "mask_opacity": 0.14,
      "grit_opacity": 0.30, "scratch_opacity": 0.22,
      "noise": 30, "uniquize": true,
      "title": {"line1": "yaromat", "line2": "ничего не поздно",
                "fade_in": 1.0, "hold_to": 8.0, "fade_out_to": 9.5}
    }
  }

Если блока "finish" нет — ведёт себя как обычный clip_producer (без обработки).

Environment: JOB_ID
"""

import json
import os
import random
import subprocess
import sys
from pathlib import Path

from analyze import analyze_track, Segment, find_highlight_offset

JOB_ID = os.environ.get("JOB_ID", "")
if not JOB_ID:
    sys.exit("JOB_ID not set")

REMOTE  = "ydrive"
JOBS_YD = "Content factory/render_jobs"
JOB_YD  = f"{JOBS_YD}/{JOB_ID}"
WORKDIR = Path("/tmp/finish_job")
WORKDIR.mkdir(parents=True, exist_ok=True)

REPO_DIR = Path(__file__).resolve().parent
GRIT_OV    = REPO_DIR / "assets" / "grit_overlay.mp4"
SCRATCH_OV = REPO_DIR / "assets" / "scratch_overlay.mp4"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

FMT_DIMS = {
    "square":    (1080, 1080),
    "vertical":  (1080, 1920),
    "landscape": (1920, 1080),
}
FMT_FILTERS = {
    "square":    "scale=1080:1080:force_original_aspect_ratio=increase,crop=1080:1080",
    "vertical":  "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
    "landscape": "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080",
}


# ── rclone ─────────────────────────────────────────────────────────────────────

def yd_get(remote_path: str, local: Path) -> bool:
    local.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["rclone", "copyto", f"{REMOTE}:{remote_path}", str(local)],
        capture_output=True, text=True,
    )
    return r.returncode == 0

def yd_put(local: Path, remote_path: str) -> bool:
    r = subprocess.run(
        ["rclone", "copyto", str(local), f"{REMOTE}:{remote_path}"],
        capture_output=True, text=True,
    )
    return r.returncode == 0

def yd_put_text(text: str, remote_path: str):
    tmp = WORKDIR / "_status.txt"
    tmp.write_text(text)
    yd_put(tmp, remote_path)


# ── helpers ────────────────────────────────────────────────────────────────────

def video_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 60.0

def assign_sources(segments: list[Segment], sources: list[str]) -> list[Segment]:
    for seg in segments:
        seg.source = random.choice(sources)
    return segments

def assign_src_starts(segments: list[Segment], durations: dict[str, float]) -> list[Segment]:
    cursors: dict[str, float] = {
        src: random.uniform(0.0, max(0.0, dur * 0.2))
        for src, dur in durations.items()
    }
    for seg in segments:
        src = seg.source
        dur = durations.get(src, 60.0)
        cursor = cursors.get(src, 0.0)
        if cursor + seg.duration > dur:
            cursor = random.uniform(0.0, max(0.0, dur - seg.duration - 0.5))
            cursor = max(cursor, 0.0)
        seg.src_start = round(cursor, 4)
        cursors[src] = cursor + seg.duration
    return segments


def seg_uniquize_filter(fmt_filter: str, W: int, H: int) -> str:
    """Посегментная уникализация. Рандом ТЯНЕТСЯ ПОСЛЕ assign_* → кат не меняется.
    зеркало (~50%) + зум 1.0–1.12 (центр) + редкая инверсия (~12%)."""
    parts = [fmt_filter]
    z = round(random.uniform(1.0, 1.12), 3)
    if z > 1.005:
        parts.append(f"crop=iw/{z}:ih/{z}")
        parts.append(f"scale={W}:{H}")
    if random.random() < 0.5:
        parts.append("hflip")
    if random.random() < 0.12:
        parts.append("negate")
    parts.append("setsar=1")
    return ",".join(parts)


def build_finish_pass(concat_mp4: Path, track_file: Path, result: Path,
                      duration: float, W: int, H: int, fin: dict,
                      audio_offset: float):
    """Грейд + warm-green маска + grit/scratch + noise + тайтл-карта → mux audio."""
    mask_color = fin.get("mask_color", "0x8a9a60")
    mask_op    = float(fin.get("mask_opacity", 0.14))
    grit_op    = float(fin.get("grit_opacity", 0.30))
    scr_op     = float(fin.get("scratch_opacity", 0.22))
    noise_lvl  = int(fin.get("noise", 30))
    title      = fin.get("title")

    # тайтл-карта: тексты в файлы (UTF-8, без эскейп-ада)
    t1f = WORKDIR / "title1.txt"
    t2f = WORKDIR / "title2.txt"
    title_chain = ""
    if title:
        fi = float(title.get("fade_in", 1.0))
        ht = float(title.get("hold_to", 8.0))
        fo = float(title.get("fade_out_to", 9.5))
        t1f.write_text(title.get("line1", ""))
        t2f.write_text(title.get("line2", ""))
        # alpha: 0 до fi, нарастает к 1 за 1с, держится до ht, гаснет к fo
        alpha = (f"if(lt(t,{fi}),0,"
                 f"if(lt(t,{fi}+1),(t-{fi}),"
                 f"if(lt(t,{ht}),1,"
                 f"if(lt(t,{fo}),({fo}-t)/({fo}-{ht}),0))))")
        fs1 = max(48, H // 10)        # yaromat крупно
        fs2 = max(28, H // 22)        # название трека мельче
        title_chain = (
            f",drawtext=fontfile={FONT}:textfile={t1f}:fontcolor=white@0.92:"
            f"fontsize={fs1}:x=(w-text_w)/2:y=(h/2)-{fs1}:alpha='{alpha}'"
            f",drawtext=fontfile={FONT}:textfile={t2f}:fontcolor=white@0.85:"
            f"fontsize={fs2}:x=(w-text_w)/2:y=(h/2)+10:alpha='{alpha}'"
        )

    fc = (
        f"[0:v]eq=saturation=0.60:contrast=1.13:brightness=-0.02,"
        f"colorbalance=rm=0.02:gm=0.04:bm=-0.05:rs=0.01:gs=0.03:bs=-0.04,setsar=1[g];"
        f"[3:v]format=rgba,colorchannelmixer=aa={mask_op}[mask];"
        f"[g][mask]overlay=shortest=1[gm];"
        f"[1:v]scale={W}:{H},setsar=1,format=gbrp[grit];"
        f"[gm][grit]blend=all_mode=screen:all_opacity={grit_op}[d1];"
        f"[2:v]scale={W}:{H},setsar=1,format=gbrp[scr];"
        f"[d1][scr]blend=all_mode=screen:all_opacity={scr_op}[d2];"
        f"[d2]noise=alls={noise_lvl}:allf=t+u,vignette=PI/5{title_chain}[v]"
    )

    audio_in = ["-i", str(track_file)]
    if audio_offset > 0:
        audio_in = ["-ss", str(round(audio_offset, 3)), "-i", str(track_file)]

    cmd = [
        "ffmpeg", "-y",
        "-i", str(concat_mp4),                       # 0 montage
        "-stream_loop", "-1", "-i", str(GRIT_OV),    # 1 grit (loop)
        "-stream_loop", "-1", "-i", str(SCRATCH_OV), # 2 scratch (loop)
        "-f", "lavfi", "-t", str(round(duration, 3)),
        "-i", f"color=c={mask_color}:s={W}x{H}",     # 3 warm-green mask
        *audio_in,                                   # 4 audio
        "-filter_complex", fc,
        "-map", "[v]", "-map", "4:a",
        "-t", str(round(duration, 3)),
        "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-tune", "grain",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-shortest",
        str(result),
    ]
    return subprocess.run(cmd, capture_output=True, text=True)


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    print(f"Job ID: {JOB_ID}")

    print("\n── Downloading inputs ──")
    job_file = WORKDIR / "job.json"
    if not yd_get(f"{JOB_YD}/job.json", job_file):
        sys.exit("Failed to download job.json")

    job = json.loads(job_file.read_text())
    duration = float(job["duration"])
    fmt      = job.get("format", "square")
    out_name = job["out_name"]
    sources  = job["sources"]
    seed     = job.get("seed")
    fin      = job.get("finish") or {}
    fmt_filter = FMT_FILTERS.get(fmt, FMT_FILTERS["square"])
    W, H = FMT_DIMS.get(fmt, FMT_DIMS["square"])

    print(f"  duration={duration}s  format={fmt}  sources={sources}  finish={'yes' if fin else 'no'}")

    track_file = WORKDIR / "track.mp3"
    if not yd_get(f"{JOB_YD}/track.mp3", track_file):
        sys.exit("Failed to download track.mp3")
    print(f"  track.mp3  {track_file.stat().st_size//1024}KB")

    src_files: dict[str, Path] = {}
    src_durations: dict[str, float] = {}
    for src in sources:
        dest = WORKDIR / f"{src}.mp4"
        if not yd_get(f"{JOB_YD}/{src}.mp4", dest):
            print(f"  WARNING: {src}.mp4 not found — skipping")
            continue
        dur = video_duration(dest)
        src_files[src] = dest
        src_durations[src] = dur
        print(f"  {src}.mp4  {dest.stat().st_size//1024}KB  {dur:.1f}s")

    if not src_files:
        sys.exit("No source videos available")

    # ── Analyze (тот же seed → тот же кат, что draft3) ──
    print("\n── Audio analysis (librosa/aubio) ──")
    if seed is not None:
        random.seed(seed)

    highlight = bool(job.get("highlight", False))
    hl_offset = 0.0
    if highlight:
        hl_offset = find_highlight_offset(track_file, window=duration)

    bpm, segments = analyze_track(track_file, duration=duration, seed=seed, start=hl_offset)
    print(f"  BPM={bpm:.1f}  segments={len(segments)}  highlight_offset={hl_offset:.1f}s")

    active_sources = list(src_files.keys())
    segments = assign_sources(segments, active_sources)
    segments = assign_src_starts(segments, src_durations)
    # ВАЖНО: уникализационные рандомы тянем ПОСЛЕ assign_* → кат идентичен draft3
    uniquize = bool(fin.get("uniquize", False))
    seg_filters = []
    for _ in segments:
        seg_filters.append(seg_uniquize_filter(fmt_filter, W, H) if uniquize else f"{fmt_filter},setsar=1")

    # ── Cut segments (+ посегментная уникализация) ──
    print(f"\n── Cutting {len(segments)} segments  (uniquize={uniquize}) ──")
    seg_files: list[Path] = []
    for i, seg in enumerate(segments):
        src_file = src_files[seg.source]
        out_file = WORKDIR / f"seg_{i:03d}.mp4"
        src_dur  = src_durations[seg.source]

        src_start = float(seg.src_start)
        seg_dur   = float(seg.duration)
        if src_start + seg_dur > src_dur:
            src_start = max(0.0, src_dur - seg_dur - 0.1)

        cmd = [
            "ffmpeg", "-y",
            "-ss", str(round(src_start, 3)),
            "-t",  str(round(seg_dur, 3)),
            "-i",  str(src_file),
            "-vf", seg_filters[i],
            "-c:v", "libx264", "-crf", "20", "-preset", "fast",
            "-an", "-fps_mode", "cfr", "-r", "25",
            str(out_file),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not out_file.exists() or out_file.stat().st_size < 1000:
            print(f"  seg_{i:03d} FAIL: {r.stderr[-160:]}")
            continue
        seg_files.append(out_file)

    if not seg_files:
        sys.exit("No segments rendered")
    print(f"  {len(seg_files)}/{len(segments)} segments OK")

    # ── Concat ──
    print("\n── Concatenating ──")
    concat_list = WORKDIR / "concat.txt"
    concat_list.write_text("\n".join(f"file '{f}'" for f in seg_files))
    concat_mp4 = WORKDIR / "concat.mp4"
    r = subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_list), "-c", "copy", str(concat_mp4),
    ], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"Concat failed: {r.stderr[-200:]}")
    print(f"  concat.mp4  {concat_mp4.stat().st_size//1024}KB")

    # ── Finish pass (grade + dirt + title) или простой mux ──
    result = WORKDIR / out_name
    if fin:
        print("\n── Finish pass (grade + grit/scratch + noise + title) ──")
        r = build_finish_pass(concat_mp4, track_file, result, duration, W, H, fin, hl_offset)
    else:
        print("\n── Mixing audio (no finish) ──")
        audio_in = ["-i", str(track_file)]
        if hl_offset > 0:
            audio_in = ["-ss", str(round(hl_offset, 3)), "-i", str(track_file)]
        r = subprocess.run([
            "ffmpeg", "-y", "-i", str(concat_mp4), *audio_in,
            "-t", str(round(duration, 3)),
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest", str(result),
        ], capture_output=True, text=True)

    if r.returncode != 0 or not result.exists():
        yd_put_text(f"error: render failed\n{r.stderr[-400:]}", f"{JOB_YD}/status.txt")
        sys.exit(f"Render failed: {r.stderr[-400:]}")

    mb = result.stat().st_size / 1024 / 1024
    print(f"  {out_name}  {mb:.1f}MB")

    print(f"\n── Uploading {out_name} ──")
    if not yd_put(result, f"{JOB_YD}/{out_name}"):
        sys.exit("Upload failed")
    yd_put_text("done", f"{JOB_YD}/status.txt")
    print(f"\n✅ Done: {out_name} ({mb:.1f}MB)")


if __name__ == "__main__":
    main()
