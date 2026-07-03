#!/usr/bin/env python3
"""
final_qc.py — Финальный QC на собранном клипе.

Использует mimo для проверки:
- Соответствия частоты склеек заявленному ритму.
- Консистентности текстуры/зерна по всему клипу.
- Читаемости текста/шрифтов.
- Общей "пластмассовости" (implausibility).

Usage:
  python3 final_qc.py --clip result.mp4 --job-id JOB_ID
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

MIMO = os.path.expanduser("~/.mimocode/bin/mimo")
WORK = tempfile.mkdtemp(prefix="qc_")
STRIPS = os.path.join(WORK, "strips")
os.makedirs(STRIPS, exist_ok=True)

YD_ROOT = "ydrive:Content factory"

RUBRIC = (
    "Ты — QC-инженер готового видеоклипа (сборка из сцен). Оцени СЛЕДУЮЩЕЕ:\n"
    "1. cuts_ok (bool): Частота склеек (смены кадров) консистентна и соответствует ритму? "
    "Если склейки слишком частые, рваные или хаотичные — false.\n"
    "2. texture_consistent (bool): Текстура (зерно, грит, скретчи) консистентна по всему клипу? "
    "Если между сценами меняется стиль зерна или оно пропадает/появляется резко — false.\n"
    "3. fonts_ok (bool): Если в кадре есть текст — он читаем, шрифты целостные, нет артефактов?\n"
    "4. plastic_score (0-100): ОБЩАЯ пластмассовость (AI-implausibility). "
    "Используй шкалу: 0-30 (норма, атмосферно), 40-70 (средний пластик, несовершенства), "
    "70-100 (высокий пластик, неправдоподобно).\n\n"
    "Верни СТРОГО JSON:\n"
    '{"cuts_ok": bool, "texture_consistent": bool, "fonts_ok": bool, "plastic_score": 0-100, "pass": bool, "reason": "краткое пояснение"}\n'
    "pass = true ТОЛЬКО если cuts_ok AND texture_consistent AND fonts_ok AND plastic_score < 55."
)


def strip_ansi(t):
    return re.sub(r'\x1B\[[0-9;]*[A-Za-z]', '', t)

def extract_json(t):
    t = strip_ansi(t)
    depth = 0
    start = -1
    for i, ch in enumerate(t):
        if ch == '{':
            if depth == 0: start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    return json.loads(t[start:i+1])
                except Exception:
                    start = -1
    return None


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def frames_of(path, n=8):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".png", ".jpg", ".jpeg"):
        return [path]
    
    dur_str = sh(f'ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "{path}"').stdout.strip()
    try:
        dur = float(dur_str)
    except Exception:
        dur = 6.0
    
    out = []
    base = os.path.splitext(os.path.basename(path))[0]
    
    for i in range(1, n + 1):
        pct = i / (n + 1)
        t = dur * pct
        f = os.path.join(STRIPS, f"{base}_{i}.jpg")
        sh(f'ffmpeg -nostdin -y -ss {t:.2f} -i "{path}" -frames:v 1 -q:v 3 -vf scale=512:-1 "{f}"')
        if os.path.exists(f):
            out.append(f)
    return out


def make_strip(frames, name):
    imgs = [Image.open(f).convert("RGB") for f in frames if os.path.exists(f)]
    if not imgs:
        return None
    if len(imgs) == 1:
        out = os.path.join(STRIPS, f"{name}_one.jpg")
        imgs[0].save(out, quality=88)
        return out
        
    h = min(im.height for im in imgs)
    imgs = [im.resize((int(im.width * h / im.height), h)) for im in imgs]
    strip = Image.new("RGB", (sum(im.width for im in imgs), h))
    x = 0
    for im in imgs:
        strip.paste(im, (x, 0))
        x += im.width
    out = os.path.join(STRIPS, f"{name}_strip.jpg")
    strip.save(out, quality=88)
    return out


def judge(strip, timeout=180):
    try:
        r = subprocess.run(
            [MIMO, "run", "--pure", "--dangerously-skip-permissions", RUBRIC, "-f", strip],
            capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
        )
    except subprocess.TimeoutExpired:
        return None, "timeout"
    
    d = extract_json(r.stdout or "")
    if not d:
        return None, "no-json"
    
    # Проверка всех обязательных полей
    required = ["cuts_ok", "texture_consistent", "fonts_ok", "plastic_score"]
    if not all(k in d for k in required):
        return None, f"missing-fields: {[k for k in required if k not in d]}"
        
    # Валидация типов и значений
    try:
        plastic = float(d["plastic_score"])
        plastic = max(0.0, min(100.0, plastic))
    except Exception:
        return None, "bad-plastic-score"
        
    # pass вычисляется на стороне вызывающего кода, но mimo может вернуть свой pass
    reason = str(d.get("reason", "N/A"))[:100]
    
    return {
        "cuts_ok": bool(d["cuts_ok"]),
        "texture_consistent": bool(d["texture_consistent"]),
        "fonts_ok": bool(d["fonts_ok"]),
        "plastic_score": plastic,
        "reason": reason
    }, "ok"


def upload_yd(path, job_id):
    dst = f"{YD_ROOT}/cloud_io/render_jobs/{job_id}/qc_report.json"
    r = subprocess.run(["rclone", "copyto", path, dst], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[rclone] copyto failed: {r.stderr[:300]}", file=sys.stderr)
        sys.exit(1)
    print(f"[rclone] uploaded -> {dst}")


def main():
    parser = argparse.ArgumentParser(description="Final QC for assembled clip.")
    parser.add_argument("--clip", required=True, help="Path to the final clip (MP4)")
    parser.add_argument("--job-id", required=True, help="Job ID for Yandex Disk upload")
    args = parser.parse_args()

    if not os.path.exists(args.clip):
        print(f"Error: Clip not found: {args.clip}", file=sys.stderr)
        sys.exit(1)

    print(f"Processing QC for: {args.clip}")
    frames = frames_of(args.clip)
    if not frames:
        print("Error: Could not extract frames from clip.", file=sys.stderr)
        sys.exit(1)

    strip = make_strip(frames, os.path.splitext(os.path.basename(args.clip))[0])
    if not strip:
        print("Error: Failed to create strip for judge.", file=sys.stderr)
        sys.exit(1)

    result, status = judge(strip)
    
    if status != "ok" or result is None:
        print(f"Error: Judge failed ({status}). Raw output may be in: {WORK}", file=sys.stderr)
        sys.exit(1)

    # Финальный расчёт pass
    final_pass = (
        result["cuts_ok"] and 
        result["texture_consistent"] and 
        result["fonts_ok"] and 
        result["plastic_score"] < 55
    )
    
    report = {
        "clip": os.path.basename(args.clip),
        "job_id": args.job_id,
        "cuts_ok": result["cuts_ok"],
        "texture_consistent": result["texture_consistent"],
        "fonts_ok": result["fonts_ok"],
        "plastic_score": result["plastic_score"],
        "pass": final_pass,
        "reason": result["reason"]
    }

    rp = os.path.join(WORK, "qc_report.json")
    with open(rp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    
    print(f"QC Result: {'PASS' if final_pass else 'FAIL'} (plastic={result['plastic_score']})")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    
    upload_yd(rp, args.job_id)


if __name__ == "__main__":
    main()
