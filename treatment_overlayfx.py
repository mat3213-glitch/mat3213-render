#!/usr/bin/env python3
"""
treatment_overlayfx.py — Track A лоу-фай: ВНЕШНИЕ Pinterest-оверлеи (на чёрном) screen-блендом.
  - film-grain на всю длину (низкая непрозрачность) = общий шум
  - film-burn ВСПЫШКОЙ по времени на стыке склейки = переход

Оверлеи на чёрном фоне: screen-бленд гасит чёрное, оставляет только свет/зерно. Burn паддится
чёрным до/после окна вспышки (на чёрном screen=ничего) → горит только на стыке.

Usage:
  python3 treatment_overlayfx.py BASE.mp4 GRAIN.mp4 BURN.mp4 OUT.mp4 --seam 16.0 \
    [--w 720 --h 1280 --fps 24 --grain-op 0.20 --burn-len 3.0]
"""
import argparse
import subprocess
import sys


def probe_dur(path: str) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True, timeout=30,
    )
    return float(r.stdout.strip())


def main():
    p = argparse.ArgumentParser()
    p.add_argument("base"); p.add_argument("grain"); p.add_argument("burn"); p.add_argument("out")
    p.add_argument("--w", type=int, default=720)
    p.add_argument("--h", type=int, default=1280)
    p.add_argument("--fps", type=int, default=24)
    p.add_argument("--seam", type=float, required=True, help="секунда стыка склейки")
    p.add_argument("--grain-op", type=float, default=0.20, help="непрозрачность зерна (screen)")
    p.add_argument("--burn-len", type=float, default=3.0, help="длина вспышки film-burn, с")
    p.add_argument("--no-grain", action="store_true",
                   help="только film-burn на стыке, БЕЗ зерна (для объединённого пресета: "
                        "зерно уже даёт слой B/texture_pass)")
    p.add_argument("--burn-op", type=float, default=1.0, help="непрозрачность film-burn (screen)")
    p.add_argument("--timeout", type=int, default=600)
    a = p.parse_args()

    dur = probe_dur(a.base)
    # burn растягиваем симметрично вокруг стыка: bleeds в ОБА клипа (переход не резкий).
    burn_start = max(0.0, a.seam - a.burn_len / 2.0)     # центр окна на стыке
    stop = max(0.0, dur - burn_start - a.burn_len)       # чёрный хвост до конца базы

    # burn: обрезаем/ускоряем клип до burn_len (setpts тянет вспышку на всё окно),
    # паддим чёрным до/после (на чёрном screen=ничего). Оставляем ЦВЕТНЫМ (оранж прожиг).
    burn_src_dur = probe_dur(a.burn)
    burn_speed = a.burn_len / burn_src_dur               # setpts-фактор: тянет/жмёт вспышку РОВНО в окно
    burn_chain = (
        f"[2:v]scale={a.w}:{a.h},fps={a.fps},setpts={burn_speed}*PTS,"
        f"trim=duration={a.burn_len},setpts=PTS-STARTPTS,"
        f"tpad=start_duration={burn_start}:stop_duration={stop}:color=black,"
        f"format=gbrp,setpts=PTS-STARTPTS[b];"
    )
    if a.no_grain:
        fc = (
            f"[0:v]scale={a.w}:{a.h},fps={a.fps},format=gbrp,setpts=PTS-STARTPTS[v];"
            + burn_chain +
            f"[v][b]blend=all_mode=screen:all_opacity={a.burn_op}:shortest=1[vout]"
        )
        inputs = ["-i", a.base, "-i", a.burn]
        # переиндексируем [2:v]→[1:v] когда нет grain-входа
        fc = fc.replace("[2:v]", "[1:v]")
    else:
        fc = (
            f"[0:v]scale={a.w}:{a.h},fps={a.fps},format=gbrp,setpts=PTS-STARTPTS[v];"
            f"[1:v]scale={a.w}:{a.h},fps={a.fps},format=gray,format=gbrp,setpts=PTS-STARTPTS[g];"
            + burn_chain +
            f"[v][g]blend=all_mode=screen:all_opacity={a.grain_op}:shortest=1[b1];"
            f"[b1][b]blend=all_mode=screen:all_opacity={a.burn_op}:shortest=1[vout]"
        )
        inputs = ["-i", a.base, "-stream_loop", "-1", "-i", a.grain, "-i", a.burn]
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        *inputs,
        "-filter_complex", fc, "-map", "[vout]", "-an", "-shortest",
        "-c:v", "libx264", "-crf", "20", "-preset", "medium",
        "-r", str(a.fps), "-pix_fmt", "yuv420p", a.out,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=a.timeout)
    if r.returncode != 0:
        sys.exit("ffmpeg A failed: " + r.stderr[-1000:])
    print(f"A ok: {a.out} (dur={dur:.1f}s seam={a.seam} burn={burn_start:.1f}..{burn_start+a.burn_len:.1f})")


if __name__ == "__main__":
    main()
