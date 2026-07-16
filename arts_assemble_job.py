#!/usr/bin/env python3
"""
arts_assemble_job.py — сборка клипа из оживлённых артов + оверлеи из библиотеки доски.

Узел арт-конвейера: арты (CF/Pollinations) → parallax_planes → СЮДА → клип.
Склейка N роликов + «шум» оверлеем на все кадры + футаж-вспышка на каждый стык.

⚠️ ГРАБЛЯ (стоила двух сессий): screen-бленд ТОЛЬКО в gbrp. На yuv420p блендятся
   плоскости цветности: screen(0.5,0.5)=0.75 → U,V вверх → синий+красный = ПУРПУР
   (замерено +68). Без явного format ffmpeg сам сведёт входы к yuv420p исходника.
⚠️ Оверлеи доски вертикальные, арты квадратные → scale=cover + crop по центру,
   ориентацию НЕ угадывать (ffprobe).

SELF-BLEND (yaromat 2026-07-16: «кадр на себя со встречным дрейфом», выбран режим screen):
   каждый арт блендится с СОБСТВЕННЫМ ЗЕРКАЛОМ (hflip). Параллакс едет туда — зеркало едет
   навстречу → двойная экспозиция с расходящимся движением.
   ⚠️ Зеркало, а НЕ реверс времени: реверс в СЕРЕДИНЕ клипа совпадает с оригиналом кадр в кадр
   → эффект там исчезает (замерено: расхождение 6.6 на краях → 1.0 в центре, двоение пульсирует).
   hflip встречен всегда, схлопываться нечему.

Ручки (env): JOB_ID, ARTS (art_1,art_5), NOISE_ID, NOISE_OP (0.35), FLASH_ID,
FLASH_OP (0.55), FLASH_WIN (1.2 = окно вспышки на стыке), NOISE_GRAY (0/1),
SEAM (cut|xfade), XFADE_DUR (0.4), SELF_BLEND (off|screen|average|lighten), SELF_OP (0.5).

Запуск: JOB_ID=arts_cold_noir_2026-07-16 ARTS=art_1,art_5 NOISE_ID=...757 FLASH_ID=...297 \
        python3 arts_assemble_job.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

YD = "ydrive:Content factory"
BOARD = f"{YD}/assets/overlay_assets/board"
FPS = 25


def sh(cmd, check=False):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"fail: {' '.join(str(c) for c in cmd)}\n{r.stderr[-1500:]}")
    return r


def probe(path, keys="width,height"):
    r = sh(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
            f"stream={keys}", "-of", "csv=p=0", path], check=True)
    return [int(float(x)) for x in r.stdout.strip().split(",")[:2]]


def dur(path):
    r = sh(["ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "csv=p=0", path], check=True)
    return float(r.stdout.strip())


def cover(w, h, W, H):
    """Оверлей → покрыть WxH без искажения: scale по большей стороне + crop по центру."""
    s = max(W / w, H / h)
    nw, nh = int(w * s // 2 * 2), int(h * s // 2 * 2)
    return f"scale={nw}:{nh},crop={W}:{H}:(iw-{W})/2:(ih-{H})/2"


def main():
    tmp = tempfile.mkdtemp(prefix="arts_asm_")
    job = os.environ.get("JOB_ID")
    if not job:
        sys.exit("JOB_ID not set")
    try:
        _run(tmp, job)
    except Exception as e:
        sh(["rclone", "rcat", f"{YD}/cloud_io/render_jobs/{job}/status.txt"],
           )  # best-effort
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run(tmp, job):
    arts = [a.strip() for a in (os.environ.get("ARTS") or "").split(",") if a.strip()]
    if len(arts) < 2:
        raise RuntimeError("ARTS: нужно минимум 2 (напр. art_1,art_5)")
    noise_id = (os.environ.get("NOISE_ID") or "").strip()
    flash_id = (os.environ.get("FLASH_ID") or "").strip()
    noise_op = float(os.environ.get("NOISE_OP") or 0.35)
    flash_op = float(os.environ.get("FLASH_OP") or 0.55)
    flash_win = float(os.environ.get("FLASH_WIN") or 1.2)
    noise_gray = (os.environ.get("NOISE_GRAY") or "0") == "1"
    seam = (os.environ.get("SEAM") or "cut").lower()
    xf = float(os.environ.get("XFADE_DUR") or 0.4)
    self_blend = (os.environ.get("SELF_BLEND") or "off").lower()
    self_op = float(os.environ.get("SELF_OP") or 0.5)

    base = f"{YD}/cloud_io/render_jobs/{job}"
    print(f"Stage 0: fetch {len(arts)} arts")
    locals_ = []
    for a in arts:
        p = os.path.join(tmp, f"{a}.mp4")
        sh(["rclone", "copyto", f"{base}/{a}.mp4", p], check=True)
        locals_.append(p)
    W, H = probe(locals_[0])
    durs = [dur(p) for p in locals_]
    print(f"  арты {W}x{H}, длительности {[round(d,2) for d in durs]}")

    noise_p = flash_p = None
    if noise_id:
        noise_p = os.path.join(tmp, "noise.mp4")
        sh(["rclone", "copyto", f"{BOARD}/{noise_id}.mp4", noise_p], check=True)
    if flash_id:
        flash_p = os.path.join(tmp, "flash.mp4")
        sh(["rclone", "copyto", f"{BOARD}/{flash_id}.mp4", flash_p], check=True)

    # --- входы фиксированным порядком: арты, затем шум, затем вспышка ---
    total = sum(durs) if seam != "xfade" else sum(durs) - xf * (len(durs) - 1)
    inputs = []
    for p in locals_:
        inputs += ["-i", p]
    i_noise = i_flash = None
    if noise_p:
        i_noise = len(locals_)
        inputs += ["-stream_loop", "-1", "-t", f"{total:.3f}", "-i", noise_p]
    if flash_p:
        i_flash = len(locals_) + (1 if noise_p else 0)
        inputs += ["-stream_loop", "-1", "-t", f"{total:.3f}", "-i", flash_p]

    parts = []
    for i in range(len(locals_)):
        # settb+setpts обязательны: реальные клипы несут грязный PTS → xfade схлопывается
        parts.append(f"[{i}:v]fps={FPS},scale={W}:{H},setsar=1,format=gbrp,"
                     f"settb=AVTB,setpts=PTS-STARTPTS"
                     + (f"[p{i}];" if self_blend != "off" else f"[a{i}];"))
        if self_blend != "off":
            # арт на СЕБЯ: копия-зеркало дрейфует навстречу (hflip, не reverse — см. шапку)
            parts.append(f"[p{i}]split=2[d{i}][m{i}];")
            parts.append(f"[m{i}]hflip[mf{i}];")
            parts.append(f"[d{i}][mf{i}]blend=all_mode={self_blend}:"
                         f"all_opacity={self_op}[a{i}];")
    if self_blend != "off":
        print(f"  self-blend: {self_blend}, зеркало hflip, op={self_op}")

    seams = []          # моменты стыков в готовом таймлайне
    if seam == "xfade":
        cur, t = "a0", 0.0
        for i in range(1, len(locals_)):
            off = t + durs[i - 1] - xf
            seams.append(off + xf / 2)
            nxt = f"x{i}"
            parts.append(f"[{cur}][a{i}]xfade=transition=fade:duration={xf}:offset={off:.3f}[{nxt}];")
            cur, t = nxt, off
        chain = cur
    else:
        t = 0.0
        for i in range(len(locals_) - 1):
            t += durs[i]
            seams.append(t)
        parts.append("".join(f"[a{i}]" for i in range(len(locals_))) +
                     f"concat=n={len(locals_)}:v=1:a=0[cat];")
        chain = "cat"
    print(f"  склейка: {seam}, стыки на {[round(s,2) for s in seams]}с, итого {total:.2f}с")

    # --- шум на все кадры (screen в gbrp) ---
    if i_noise is not None:
        nw, nh = probe(noise_p)
        gray = ",hue=s=0" if noise_gray else ""   # по умолчанию цвет СОХРАНЯЕМ: оверлей холодный, в палитру
        parts.append(f"[{i_noise}:v]{cover(nw,nh,W,H)},fps={FPS}{gray},format=gbrp[nz];")
        parts.append(f"[{chain}][nz]blend=all_mode=screen:all_opacity={noise_op}[wn];")
        chain = "wn"

    # --- вспышка на стыки ---
    if i_flash is not None and seams:
        fw, fh = probe(flash_p)
        parts.append(f"[{i_flash}:v]{cover(fw,fh,W,H)},fps={FPS},format=gbrp[fl];")
        en = "+".join(f"between(t,{s-flash_win/2:.3f},{s+flash_win/2:.3f})" for s in seams)
        parts.append(f"[{chain}][fl]blend=all_mode=screen:all_opacity={flash_op}:"
                     f"enable='{en}'[wf];")
        chain = "wf"

    parts.append(f"[{chain}]format=yuv420p[v]")
    fc = "".join(parts)
    out = os.path.join(tmp, "arts_assembly.mp4")
    cmd = (["ffmpeg", "-y"] + inputs + ["-filter_complex", fc, "-map", "[v]",
           "-t", f"{total:.3f}", "-c:v", "libx264", "-crf", "20",
           "-preset", "veryfast", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out])
    print("Stage 1: assemble")
    sh(cmd, check=True)
    print(f"  → {os.path.getsize(out)/1048576:.2f} MB")

    dest = f"{YD}/cloud_io/preview/qwen_6_blue_2026-07-16/arts_assembly.mp4"
    sh(["rclone", "copyto", out, dest], check=True)
    sh(["rclone", "rcat", f"{base}/status.txt"], check=False)
    print(f"done -> {dest}")


if __name__ == "__main__":
    main()
