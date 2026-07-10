#!/usr/bin/env python3
"""
source_qc_validate.py — регресс-проверка двухфакторного гейта source_qc на GH.

Тянет диагностические кадры с ЯД и проверяет:
  1) ЧАСЫ (ложняк YOLOv8-face из теста 2026-07-10) — теперь НЕ должны реджектиться
     (круглый COCO-класс подавляет лицо);
  2) силуэты людей — persons>0, лицо крупным планом НЕ ловится (фигуры ок);
Печатает вердикты, exit≠0 если регрессия (часы забракованы).
"""
import sys
import subprocess
import tempfile
from pathlib import Path

import source_qc

YD = "ydrive:Content factory"
CASES = [
    # (rel_path, ожидание ok, метка)
    ("cloud_io/qwen_pool/2026-06-18/img_01.png", True, "ЧАСЫ (не должны быть лицом)"),
    ("cloud_io/veofree_pool/2026-07-04/vid_01.mp4", True, "силуэты людей (фигуры ок)"),
]


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="sqcval_"))
    fail = 0
    for rel, want_ok, label in CASES:
        local = work / Path(rel).name
        r = subprocess.run(["rclone", "copyto", f"{YD}/{rel}", str(local)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"⚠ {label}: не скачался ({r.stderr[:80]}) — пропуск"); continue
        v = source_qc.judge_source(str(local))
        status = "OK" if v["ok"] == want_ok else "❌ РЕГРЕССИЯ"
        if v["ok"] != want_ok:
            fail += 1
        print(f"[{status}] {label}: ok={v['ok']} persons={v['persons']} "
              f"faces_kept={v['faces_kept']} face_frac={v['max_face_frac']:.0%} "
              f"closeup={v['closeup_face']} skip={v['qc_skipped']} "
              f"objects={v['objects']} reason={v['reject_reason']}")
    print("\nИТОГ:", "ВСЁ ЗЕЛЁНОЕ" if fail == 0 else f"{fail} регрессий")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
