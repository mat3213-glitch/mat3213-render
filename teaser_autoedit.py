#!/usr/bin/env python3
"""
teaser_autoedit.py — Ф2: auto-editor как STANDALONE тизер-тул (НЕ в нарративном рендере).

Архитектура v4.1: auto-editor режет по энергии/тишине → дерётся с EDL режиссёра, поэтому
вынесен ОТДЕЛЬНО: из ГОТОВОГО клипа делает короткий энергичный тизер/шортс для промо.
Держит громкие (энергичные) участки трека → сжатая динамичная версия. Оригинальный
нарративный рендер не трогает.

Вход: видео с ЯД. Выход: teaser.mp4 → ЯД гейт-папка.
"""
import argparse
import subprocess
import sys
from pathlib import Path

YD = "ydrive:Content factory"


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="cloud_io/preview/2026-07-10_supervision_creative/"
                    "04_whisper_stage_GATE/control_montage.mp4",
                    help="rel-путь видео на ЯД")
    ap.add_argument("--threshold", default="4%", help="порог громкости auto-editor")
    ap.add_argument("--out", default="cloud_io/preview/2026-07-10_supervision_creative/"
                    "06_teaser_GATE/teaser.mp4")
    a = ap.parse_args()

    src = Path("src_video.mp4")
    if sh(["rclone", "copyto", f"{YD}/{a.input}", str(src)]).returncode != 0:
        print(f"[teaser] не скачал {a.input}", file=sys.stderr); return 1

    def dur(p):
        r = sh(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", str(p)])
        try:
            return float(r.stdout.strip())
        except Exception:
            return 0.0

    d0 = dur(src)
    out = Path("teaser.mp4")
    # keep = громкие участки (энергия трека), margin сглаживает рвань, faststart для TG
    r = sh(["auto-editor", str(src), "--edit", f"audio:threshold={a.threshold}",
            "--margin", "0.2s", "--no-open", "-o", str(out)])
    if not out.exists():
        print(f"[teaser] auto-editor не дал выход: {r.stderr[-400:]}", file=sys.stderr); return 1
    d1 = dur(out)
    # перекодировать в TG-совместимый mp4 (baseline+faststart)
    fixed = Path("teaser_tg.mp4")
    sh(["ffmpeg", "-y", "-i", str(out), "-c:v", "libx264", "-profile:v", "baseline",
        "-level:v", "3.1", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "128k", str(fixed)])
    up = fixed if fixed.exists() else out
    sh(["rclone", "copyto", str(up), f"{YD}/{a.out}"])
    print(f"[teaser] ✅ {d0:.1f}с → {d1:.1f}с (сжато {100*(1-d1/max(d0,0.1)):.0f}%) → ЯД {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
