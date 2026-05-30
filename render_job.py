#!/usr/bin/env python3
"""
render_job.py — GitHub Actions runner для BLEND-рендера.

YaDisk контракт:
  Вход (ноут кладёт перед trigger):
    Content factory/render_jobs/<JOB_ID>/clips/*.mp4  — стилизованные клипы
    Content factory/render_jobs/<JOB_ID>/track.mp3    — аудио-трек
    Content factory/render_jobs/<JOB_ID>/job.json     — параметры рендера

  Выход (раннер кладёт по завершению):
    Content factory/render_jobs/<JOB_ID>/result.mp4   — готовое видео
    Content factory/render_jobs/<JOB_ID>/status.txt   — "done" или "error: ..."

job.json:
  {"track_dur": 196.78, "out_name": "ty_prosti_blend.mp4"}

Env vars (GitHub Secrets + workflow inputs):
  YADISK_LOGIN     — mat3213@yandex.ru
  YADISK_PASSWORD  — WebDAV app-password
  JOB_ID           — render job ID (e.g. 2026-05-30_ty_prosti)
"""

import json
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

JOB_ID = os.environ["JOB_ID"]

REMOTE = "ydrive"
JOB_YD = f"Content factory/render_jobs/{JOB_ID}"


# ── rclone YaDisk ─────────────────────────────────────────────────────────────

def yd_ls(remote_dir: str) -> list[str]:
    r = subprocess.run(["rclone", "lsf", f"{REMOTE}:{remote_dir}", "--files-only"],
                       capture_output=True, text=True)
    return [f.strip() for f in r.stdout.splitlines() if f.strip()]


def yd_get(remote_path: str, local: Path) -> bool:
    local.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["rclone", "copyto", f"{REMOTE}:{remote_path}", str(local)],
                       capture_output=True, text=True)
    return r.returncode == 0


def yd_put(local: Path, remote_path: str) -> bool:
    r = subprocess.run(["rclone", "copyto", str(local), f"{REMOTE}:{remote_path}"],
                       capture_output=True, text=True)
    ok = r.returncode == 0
    print(f"  PUT {remote_path.split('/')[-1]}: {'ok' if ok else 'FAIL'}")
    return ok


def yd_status(text: str):
    tmp = Path(f"/tmp/_status_{os.getpid()}.txt")
    tmp.write_text(text)
    subprocess.run(["rclone", "copyto", str(tmp), f"{REMOTE}:{JOB_YD}/status.txt"],
                   capture_output=True)
    tmp.unlink(missing_ok=True)


# ── FFmpeg ────────────────────────────────────────────────────────────────────

W, H, SEG_DUR = 1280, 720, 7

ENCODE_TMP = ["-c:v", "libx264", "-profile:v", "baseline", "-level:v", "3.1",
              "-pix_fmt", "yuv420p", "-preset", "ultrafast", "-crf", "28",
              "-movflags", "+faststart"]

ENCODE_OUT = ["-c:v", "libx264", "-profile:v", "baseline", "-level:v", "3.1",
              "-pix_fmt", "yuv420p", "-preset", "ultrafast", "-crf", "23",
              "-movflags", "+faststart"]

NORMALIZE_VF = (
    f"scale={W}:{H}:force_original_aspect_ratio=increase,"
    f"crop={W}:{H},setsar=1,"
    "eq=brightness=0.04:contrast=1.08:saturation=0.75,"
    "fps=25"
)

BLEND_FC = (
    "[0:v]unsharp=5:5:2.0:5:5:0.0[base];"
    "[1:v]eq=brightness=-0.15:contrast=0.88[top];"
    "[base][top]blend=all_mode=normal:all_opacity=0.45[v]"
)

UNIQUIZE_VF = (
    "colorchannelmixer=rr=0.87:gg=0.91:bb=1.10,"
    "eq=saturation=0.80:contrast=1.06:brightness=0.06,"
    "vignette=PI*0.25"
)

FILM_VF = "unsharp=5:5:1.5:5:5:0.0,noise=alls=14:allf=u"


def ff(cmd: list, desc: str) -> bool:
    print(f"  → {desc}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  FAIL: {r.stderr[-600:]}")
    return r.returncode == 0


def get_dur(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def render_blend(clips: list[Path], track: Path, track_dur: float, out: Path) -> bool:
    tmp = Path("/tmp/blend_tmp")
    tmp.mkdir(exist_ok=True)

    # Шаг 1: нормализация
    print(f"\nШаг 1: нормализация {len(clips)} клипов")
    norm = []
    for i, src in enumerate(clips):
        dst = tmp / f"n{i:02d}.mp4"
        vf = NORMALIZE_VF + (",hflip" if random.random() < 0.5 else "")
        if ff(["ffmpeg", "-y", "-stream_loop", "-1",
               "-t", str(SEG_DUR), "-i", str(src),
               "-vf", vf, *ENCODE_TMP, "-an", str(dst)],
              f"{i+1}/{len(clips)} {src.name[:28]}"):
            norm.append(dst)

    if not norm:
        return False

    # Шаг 2: blend пары
    n = len(norm)
    needed = int(track_dur / SEG_DUR) + 4
    idx = list(range(n)); random.shuffle(idx)
    pairs = [(idx[i], idx[i+1]) for i in range(0, n-1, 2)]
    last = pairs[-1] if pairs else None
    while len(pairs) < needed:
        a, b = random.sample(range(n), 2)
        if (a, b) == last or (b, a) == last:
            continue
        pairs.append((a, b)); last = (a, b)

    print(f"\nШаг 2: blend {len(pairs)} пар")
    blended = []
    for i, (ai, bi) in enumerate(pairs):
        dst = tmp / f"b{i:02d}.mp4"
        if ff(["ffmpeg", "-y",
               "-i", str(norm[ai]), "-i", str(norm[bi]),
               "-filter_complex", BLEND_FC, "-map", "[v]",
               *ENCODE_TMP, "-an", str(dst)],
              f"blend {i+1}/{len(pairs)}"):
            blended.append(dst)

    if not blended:
        return False

    # Шаг 3: concat + uniquize + аудио
    print("\nШаг 3: concat + uniquize + аудио")
    concat_txt = tmp / "concat.txt"
    concat_txt.write_text("\n".join(f"file '{f}'" for f in blended))
    pre = tmp / "pre_film.mp4"
    ok = ff(["ffmpeg", "-y",
             "-f", "concat", "-safe", "0", "-i", str(concat_txt),
             "-i", str(track),
             "-vf", UNIQUIZE_VF, "-map", "0:v", "-map", "1:a",
             "-t", str(track_dur), *ENCODE_OUT,
             "-c:a", "aac", "-b:a", "160k", str(pre)],
            "concat + uniquize")
    if not ok:
        return False

    # Шаг 4: film pass
    print("\nШаг 4: film pass")
    ok = ff(["ffmpeg", "-y", "-i", str(pre),
             "-vf", FILM_VF, "-map", "0:v", "-map", "0:a",
             *ENCODE_OUT, "-c:a", "copy", str(out)],
            "film pass")

    shutil.rmtree(tmp, ignore_errors=True)
    return ok and out.exists()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    local = Path("/tmp/render_job")
    clips_dir = local / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    print(f"Job: {JOB_ID}")
    print(f"YaDisk: {JOB_YD}/")

    # 1. job.json
    job_path = local / "job.json"
    if not yd_get(f"{JOB_YD}/job.json", job_path):
        sys.exit("job.json не найден на ЯД")
    job = json.loads(job_path.read_text())
    track_dur = float(job["track_dur"])
    out_name  = job.get("out_name", "result.mp4")
    print(f"track_dur={track_dur:.1f}s  out={out_name}")

    # 2. track.mp3
    track_path = local / "track.mp3"
    if not yd_get(f"{JOB_YD}/track.mp3", track_path):
        sys.exit("track.mp3 не найден на ЯД")
    print(f"Трек: {track_path.stat().st_size // 1024}KB")

    # 3. клипы
    print("\nСкачиваю клипы...")
    clip_names = [n for n in yd_ls(f"{JOB_YD}/clips") if n.endswith(".mp4")]
    if not clip_names:
        sys.exit("Нет .mp4 в clips/ на ЯД")

    clips = []
    for name in sorted(clip_names):
        dst = clips_dir / name
        if yd_get(f"{JOB_YD}/clips/{name}", dst):
            clips.append(dst)
            print(f"  ✓ {name}  {dst.stat().st_size // 1024}KB")

    if not clips:
        sys.exit("Не удалось скачать клипы")

    # 4. рендер
    result = local / out_name
    print(f"\nРендерю → {out_name}...")
    ok = render_blend(clips, track_path, track_dur, result)

    if not ok:
        yd_status("error: render failed")
        sys.exit("Рендер упал")

    size_mb = result.stat().st_size / 1024 / 1024
    print(f"\nРезультат: {size_mb:.1f}MB")

    # 5. upload
    if not yd_put(result, f"{JOB_YD}/{out_name}"):
        yd_status("error: upload failed")
        sys.exit("Upload упал")

    yd_status("done")
    print(f"\n✅ Готово. ЯД: {JOB_YD}/{out_name}")


if __name__ == "__main__":
    main()
