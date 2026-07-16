#!/usr/bin/env python3
"""
overlay_compare_job.py — сравнительный прогон оверлеев на живом кадре лупа qwen_6.

Зачем: контакт-листы врут. Оверлей судится только поверх РЕАЛЬНОГО кадра, в движении,
с нашим грейдом. Клип делится на 3 части по стыкам лупа (9.47с / 18.94с) — на каждой свой
оверлей с подписью, плюс вспышка-переход на ПЕРВОМ стыке.

База = ground truth v9 (грейд принят yaromat), НО синтетический шум ВЫКЛЮЧЕН (NOISE_SIGMA=0
по умолчанию): судим оверлей начисто, без второго слоя зерна.

⚠️ ГРАБЛЯ: screen-бленд ТОЛЬКО в gbrp. На yuv420p блендятся плоскости цветности →
   screen(0.5,0.5)=0.75 → U,V вверх → пурпур (замерено +68). См. память.

Запуск: JOB_ID=qwen6_ovl_compare python3 overlay_compare_job.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

UNIT_LEN = 10.0697
XFADE_DUR = 0.6
SEAM1 = 9.4697
SEAM2 = 18.9394
OUT_DUR = 29.0
AUDIO_SS = 33.0
FPS = 25
W, H = 1920, 1080
YD = "ydrive:Content factory"
SRC_REMOTE = f"{YD}/cloud_io/pool_gate/adult_dnb_2026-07-11/qwen_6_clip.mp4"
BOARD = f"{YD}/assets/overlay_assets/board"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
MAX_BYTES = 19 * 1024 * 1024

# сегмент → (id пина, подпись, прозрачность)
SEGMENTS = [
    ("621848661095641317", "A — GRUNGE / пыль+волоски", 0.40, 0.0, SEAM1),
    ("621848661095641432", "B — ЦАРАПИНЫ плёнки", 0.40, SEAM1, SEAM2),
    ("621848661095641324", "C — ЧАСТИЦЫ в воздухе", 0.40, SEAM2, OUT_DUR),
]
# вспышка-переход на ПЕРВОМ стыке
FLASH = ("621848661095641297", "вспышка-переход", 0.55, SEAM1 - 0.5, SEAM1 + 0.7)


def sh(cmd, check=False, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if check and r.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(str(c) for c in cmd)}\n{r.stderr[-2000:]}")
    return r


def probe_wh(path):
    r = sh(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
            "stream=width,height", "-of", "csv=p=0", path], check=True)
    p = r.stdout.strip().split(",")
    return int(p[0]), int(p[1])


def esc(t):
    """drawtext: экранировать спецсимволы."""
    return t.replace("\\", "\\\\").replace(":", "\\:").replace("'", "").replace(",", "\\,")


def main():
    tmp = tempfile.mkdtemp(prefix="ovl_cmp_")
    job_id = os.environ.get("JOB_ID", "qwen6_ovl_compare")
    try:
        _run(tmp, job_id)
    except Exception as e:
        sh(["rclone", "rcat", f"{YD}/cloud_io/render_jobs/{job_id}/status.txt"],
           input=f"error: {e}")
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run(tmp, job_id):
    noise_sigma = int(float(os.environ.get("NOISE_SIGMA") or 0))
    base_remote = f"{YD}/cloud_io/render_jobs/{job_id}"

    print("Stage 0: fetch")
    src = os.path.join(tmp, "src.mp4")
    track = os.path.join(tmp, "track.mp3")
    sh(["rclone", "copyto", SRC_REMOTE, src], check=True)
    sh(["rclone", "copyto", f"{base_remote}/track.mp3", track], check=True)

    ovls = []
    for pid, label, op, t0, t1 in SEGMENTS + [FLASH]:
        p = os.path.join(tmp, f"{pid}.mp4")
        sh(["rclone", "copyto", f"{BOARD}/{pid}.mp4", p], check=True)
        ovls.append((p, label, op, t0, t1))
        print(f"  overlay {pid} -> {label}")

    print("Stage 1: minterpolate 2x unit")
    unit = os.path.join(tmp, "unit.mp4")
    sh(["ffmpeg", "-y", "-itsscale", "2", "-i", src, "-vf",
        f"minterpolate=mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1:fps={FPS}",
        "-an", "-c:v", "libx264", "-crf", "16", "-preset", "veryfast",
        "-pix_fmt", "yuv420p", unit], check=True)

    print("Stage 2: assemble 29s + grade + overlays + labels")
    out_local = os.path.join(tmp, "qwen_6_overlay_compare.mp4")

    grade = (
        f"scale=2400:1350,setsar=1,"
        f"crop={W}:{H}:x='(2400-{W})*(t/{OUT_DUR:g})':y='(1350-{H})*(t/{OUT_DUR:g})',"
        f"colorbalance=rs=-0.08:bs=0.18:rm=-0.06:bm=0.12:bh=0.06,"
        f"eq=contrast=1.06:saturation=1.12,vignette=PI/5"
    )
    if noise_sigma > 0:
        grade += f",format=yuv420p,noise=c0s={noise_sigma}:c0f=t"

    parts = [
        f"[0:v]split=3[s0][s1][s2];",
        f"[s0]trim=0:{UNIT_LEN},setpts=PTS-STARTPTS[a];",
        f"[s1]trim=0:{UNIT_LEN},setpts=PTS-STARTPTS[b];",
        f"[s2]trim=0:{UNIT_LEN},setpts=PTS-STARTPTS[c];",
        f"[a][b]xfade=transition=fade:duration={XFADE_DUR}:offset={SEAM1}[ab];",
        f"[ab][c]xfade=transition=fade:duration={XFADE_DUR}:offset={SEAM2}[loop];",
        # ГРАБЛЯ: в gbrp ДО блендов
        f"[loop]{grade},format=gbrp[base];",
    ]

    cmd = ["ffmpeg", "-y", "-i", unit, "-ss", str(AUDIO_SS), "-i", track]
    cur = "base"
    for idx, (path, label, op, t0, t1) in enumerate(ovls):
        n = idx + 2                       # 0=unit, 1=track
        cmd += ["-stream_loop", "-1", "-t", str(OUT_DUR), "-i", path]
        gw, gh = probe_wh(path)
        pre = "transpose=1," if gh > gw else ""     # автоориентация
        parts.append(
            f"[{n}:v]{pre}scale={W}:{H},fps={FPS},hue=s=0,format=gbrp[o{idx}];")
        nxt = f"b{idx}"
        parts.append(
            f"[{cur}][o{idx}]blend=all_mode=screen:all_opacity={op}:"
            f"enable='between(t,{t0:g},{t1:g})'[{nxt}];")
        cur = nxt

    # подписи: своя на каждый сегмент + отдельная на вспышку
    draws = []
    for path, label, op, t0, t1 in ovls:
        y = "h-140" if label != FLASH[1] else "h-220"
        col = "white" if label != FLASH[1] else "yellow"
        draws.append(
            f"drawtext=fontfile={FONT}:text='{esc(label)}':fontsize=44:"
            f"fontcolor={col}:borderw=3:bordercolor=black@0.8:x=60:y={y}:"
            f"enable='between(t,{t0:g},{t1:g})'")
    parts.append(f"[{cur}]" + ",".join(draws) + ",format=yuv420p[v]")

    fc = "".join(parts)
    cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "1:a", "-t", str(OUT_DUR),
            "-af", "afade=t=out:st=27.5:d=1.5",
            "-c:v", "libx264", "-b:v", "4M", "-maxrate", "5M", "-bufsize", "8M",
            "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", out_local]
    sh(cmd, check=True)

    size = os.path.getsize(out_local)
    print(f"Final: {size / 1048576:.2f} MB")
    if size > MAX_BYTES:
        raise RuntimeError(f"{size / 1048576:.2f}MB > 19MB — TG режет >20МБ")

    dest = f"{YD}/cloud_io/preview/qwen_6_blue_2026-07-16/qwen_6_overlay_compare.mp4"
    sh(["rclone", "copyto", out_local, dest], check=True)
    sh(["rclone", "rcat", f"{base_remote}/status.txt"], input="done", check=True)
    print(f"done -> {dest}")


if __name__ == "__main__":
    main()
