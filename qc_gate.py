#!/usr/bin/env python3
"""qc_gate.py — QC-гейт стоковых файлов перед выкладкой в пул (ЯД).

Идея — koryglenn/MV (шорт-лист 23.08 п.5): blackdetect + freezedetect + громкость EBU R128.
Ловит битые/зависшие/чёрные загрузки ДО попадания в пул (урок fetch_media.yml:
«зелёный job ≠ файл жив»). Лицензии у исходного репо нет — реализация своя, только идея.

usage:
  python3 qc_gate.py FILE.mp4 [FILE2.mp4 ...] [--quarantine-dir DIR]
exit 0 = все годны; exit 1 = есть брак (список в stdout, помечен FAIL).
Пороги заточены под slow-mo/тёмный downtempo-сток: freeze детект мягкий (шум зерна
даёт движение даже в «статике»), чёрный = 98% пикселей ниже luma 0.02 непрерывно >1.5с.
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

FFPROBE_TIMEOUT = 60
FILTER_TIMEOUT = 300


def probe_duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, timeout=FFPROBE_TIMEOUT)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def run_video_filters(path):
    """Проход видеографа: blackdetect + freezedetect. Возвращает список находок."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
           "-vf", "blackdetect=d=1.5:pix_th=0.02,freezedetect=n=-60dB:d=3",
           "-an", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=FILTER_TIMEOUT)
    log = proc.stderr or ""
    finds = []
    for line in log.splitlines():
        if "black_start:" in line:  # [blackdetect @..] black_start:S black_end:E black_duration:D
            finds.append("ЧЁРНЫЙ: " + line.split("] ")[-1])
        elif "lavfi.freezedetect" in line and "freeze_start" in line:
            finds.append("FREEZE: " + line.split("freeze_start:")[-1].strip())
    return finds


def mean_volume_db(path):
    """Средняя громкость (dB); None если аудиодорожки нет."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
           "-map", "0:a:0?", "-af", "volumedetect", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=FILTER_TIMEOUT)
    for line in (proc.stderr or "").splitlines():
        if "mean_volume" in line:
            try:
                return float(line.split(":")[-1].replace("dB", "").strip())
            except ValueError:
                return None
    return None


def qc_one(path, min_dur=2.0):
    problems = []
    dur = probe_duration(path)
    if dur is None:
        return ["файл не читается ffprobe"], None
    if dur < min_dur:
        problems.append(f"длительность {dur:.1f}s < {min_dur}s")
    problems += run_video_filters(path)
    rms = mean_volume_db(path)
    if rms is not None and rms < -55:
        problems.append(f"ТИШИНА: средняя громкость {rms:.1f} dB")
    return problems, dur


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--quarantine-dir", default=None,
                    help="переложить брак сюда вместо простого отчёта")
    a = ap.parse_args()
    bad_total = 0
    for f in a.files:
        p = Path(f)
        if not p.exists():
            print(f"FAIL {p}: нет файла")
            bad_total += 1
            continue
        problems, dur = qc_one(p)
        if problems:
            bad_total += 1
            print(f"FAIL {p.name} ({dur and round(dur, 1)}s):")
            for pr in problems:
                print(f"   - {pr}")
            if a.quarantine_dir:
                qd = Path(a.quarantine_dir)
                qd.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), qd / p.name)
                print(f"   → карантин: {qd / p.name}")
        else:
            print(f"OK {p.name} ({round(dur or 0, 1)}s)")
    sys.exit(1 if bad_total else 0)


if __name__ == "__main__":
    main()
