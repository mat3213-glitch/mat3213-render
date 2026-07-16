#!/usr/bin/env python3
"""
self_blend_compare_job.py — сравнение режимов «кадр на себя со встречным дрейфом».

Идея yaromat: взять оживлённый параллаксом арт и наложить его НА СЕБЯ, но так, чтобы
две копии дрейфовали в РАЗНЫЕ стороны. Даёт двойную экспозицию с расходящимся движением.
Встречный дрейф получаем реверсом времени (`reverse`): параллакс едет туда — реверс едет обратно.

Выход: 3 панели рядом с подписями — режимы бленда. Плюс шум-оверлей на всех панелях
(константа, чтобы сравнивался ТОЛЬКО бленд).

⚠️ Бленд ТОЛЬКО в gbrp (на yuv420p блендятся плоскости цветности → пурпур +68).

Ручки: JOB_ID, ART (art_5), MODES (average,screen,lighten), NOISE_ID, NOISE_OP, SELF_OP.
"""
import os
import shutil
import subprocess
import sys
import tempfile

YD = "ydrive:Content factory"
BOARD = f"{YD}/assets/overlay_assets/board"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FPS = 25


def sh(cmd, check=False):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"fail: {' '.join(str(c) for c in cmd)}\n{r.stderr[-1500:]}")
    return r


def probe(path):
    r = sh(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
            "stream=width,height", "-of", "csv=p=0", path], check=True)
    return [int(float(x)) for x in r.stdout.strip().split(",")[:2]]


def cover(w, h, W, H):
    s = max(W / w, H / h)
    nw, nh = int(w * s // 2 * 2), int(h * s // 2 * 2)
    return f"scale={nw}:{nh},crop={W}:{H}:(iw-{W})/2:(ih-{H})/2"


def main():
    tmp = tempfile.mkdtemp(prefix="selfblend_")
    try:
        _run(tmp)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run(tmp):
    job = os.environ["JOB_ID"]
    art = os.environ.get("ART") or "art_5"
    modes = [m.strip() for m in (os.environ.get("MODES") or "average,screen,lighten").split(",") if m.strip()]
    noise_id = (os.environ.get("NOISE_ID") or "").strip()
    noise_op = float(os.environ.get("NOISE_OP") or 0.35)
    self_op = float(os.environ.get("SELF_OP") or 0.5)

    base = f"{YD}/cloud_io/render_jobs/{job}"
    src = os.path.join(tmp, "src.mp4")
    sh(["rclone", "copyto", f"{base}/{art}.mp4", src], check=True)
    W, H = probe(src)
    print(f"арт {art}: {W}x{H} | режимы: {modes} | self_op={self_op}")

    noise_p = None
    if noise_id:
        noise_p = os.path.join(tmp, "noise.mp4")
        sh(["rclone", "copyto", f"{BOARD}/{noise_id}.mp4", noise_p], check=True)
        nw, nh = probe(noise_p)

    panels = []
    for i, mode in enumerate(modes):
        out = os.path.join(tmp, f"p{i}.mp4")
        inputs = ["-i", src, "-i", src]
        # [0]=прямой дрейф, [1]=реверс времени = дрейф ВСТРЕЧНЫЙ
        fc = (f"[0:v]fps={FPS},format=gbrp,setpts=PTS-STARTPTS[a];"
              f"[1:v]fps={FPS},reverse,format=gbrp,setpts=PTS-STARTPTS[b];"
              f"[a][b]blend=all_mode={mode}:all_opacity={self_op}[sb];")
        chain = "sb"
        if noise_p:
            inputs += ["-stream_loop", "-1", "-i", noise_p]
            fc += f"[2:v]{cover(nw,nh,W,H)},fps={FPS},format=gbrp[nz];"
            fc += f"[{chain}][nz]blend=all_mode=screen:all_opacity={noise_op}[wn];"
            chain = "wn"
        label = mode
        fc += (f"[{chain}]drawtext=fontfile={FONT}:text='{label}':fontsize=40:fontcolor=white:"
               f"borderw=3:bordercolor=black@0.8:x=25:y=h-60,format=yuv420p[v]")
        cmd = ["ffmpeg", "-y"] + inputs + ["-filter_complex", fc, "-map", "[v]",
               "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
               "-pix_fmt", "yuv420p", out]
        sh(cmd, check=True)
        panels.append(out)
        print(f"  панель {mode} ✓")

    # эталон без self-blend — для честного сравнения
    ref = os.path.join(tmp, "ref.mp4")
    fc = f"[0:v]fps={FPS},format=gbrp[a];"
    inputs = ["-i", src]
    chain = "a"
    if noise_p:
        inputs += ["-stream_loop", "-1", "-i", noise_p]
        fc += f"[1:v]{cover(nw,nh,W,H)},fps={FPS},format=gbrp[nz];"
        fc += f"[{chain}][nz]blend=all_mode=screen:all_opacity={noise_op}[wn];"
        chain = "wn"
    fc += (f"[{chain}]drawtext=fontfile={FONT}:text='БЕЗ self-blend':fontsize=40:fontcolor=yellow:"
           f"borderw=3:bordercolor=black@0.8:x=25:y=h-60,format=yuv420p[v]")
    sh(["ffmpeg", "-y"] + inputs + ["-filter_complex", fc, "-map", "[v]",
        "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
        "-pix_fmt", "yuv420p", ref], check=True)
    panels.insert(0, ref)
    print("  эталон ✓")

    out = os.path.join(tmp, "self_blend_compare.mp4")
    inputs = []
    for p in panels:
        inputs += ["-i", p]
    n = len(panels)
    fc = "".join(f"[{i}:v]scale=480:480[s{i}];" for i in range(n))
    fc += "".join(f"[s{i}]" for i in range(n)) + f"hstack=inputs={n}[v]"
    sh(["ffmpeg", "-y"] + inputs + ["-filter_complex", fc, "-map", "[v]",
        "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", out], check=True)
    print(f"→ {os.path.getsize(out)/1048576:.2f} MB")

    dest = f"{YD}/cloud_io/preview/qwen_6_blue_2026-07-16/self_blend_compare.mp4"
    sh(["rclone", "copyto", out, dest], check=True)
    print(f"done -> {dest}")


if __name__ == "__main__":
    main()
