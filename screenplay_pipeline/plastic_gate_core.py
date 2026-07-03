#!/usr/bin/env python3
"""
plastic_gate_core.py — импортируемое ядро plastic gate (кадры/контакт-стрип/mimo-судья).

Вынесено из plastic_gate.py (был флат-скрипт на env-переменных, пул-свип) без изменения
логики — чтобы синхронно гейтить ОДНУ свежую сцену (Фаза 1 screenplay-pipeline), а не только
ночной свип целого пула. plastic_gate.py продолжает работать как есть (не переписан).
"""
import os
import re
import subprocess
import json
import tempfile
from PIL import Image

MIMO = os.path.expanduser("~/.mimocode/bin/mimo")

RUBRIC = (
    "Перед тобой кадр(ы) AI-видео/картинки. Оцени ПЛАСТМАССОВОСТЬ = неправдоподобие (НЕ красоту):\n"
    "ВЫСОКИЙ 70-100: AI-текст-каракули (нечитаемые цифры/буквы); идеально симметричный/искусственный "
    "свет, нереальный источник, CGI-рендер, идеальное зеркальное отражение; восковая гладкая кожа рук, "
    "деформации анатомии; огонь приклеен ровным контуром, искры как фейерверк.\n"
    "СРЕДНИЙ 40-70: мох/лишайник «налеплен», повторяющаяся одинаковая текстура коры/листвы; геометрия "
    "фасадов/зданий «плывёт», нерегулярные окна, кривые/сливающиеся объекты; объекты статичны там, где "
    "должны двигаться (вода не течёт, капли висят без гравитации).\n"
    "НИЗКИЙ 0-30 (НОРМА, НЕ штрафуй): туман/небо/лес/облака/ландшафт/дымка даже ГЛАДКИЕ; реалистичное "
    "пламя свечи, боке огней, капли на стекле с правильной оптикой, рукопись; тёмный фон и малое движение.\n"
    "ГЛАВНОЕ: отличай НАЛЕПЛЕННУЮ/ПОВТОРЯЮЩУЮСЯ текстуру и искажённую геометрию (пластик) от РОВНОЙ "
    "АТМОСФЕРЫ (норма).\n"
    "Ответь СТРОГО одним JSON: {\"plastic\": <0-100>, \"reason\": \"<кратко>\"}. Только JSON."
)


def sh(c):
    return subprocess.run(c, shell=True, capture_output=True, text=True)


def strip_ansi(t):
    return re.sub(r'\x1B\[[0-9;]*[A-Za-z]', '', t)


def extract_json(t):
    t = strip_ansi(t)
    depth = 0
    start = -1
    for i, ch in enumerate(t):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    return json.loads(t[start:i + 1])
                except Exception:
                    start = -1
    return None


def frames_of(path, strips_dir):
    """png → [сам файл]; mp4 → 3 извлечённых кадра (20/50/80%)."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".png", ".jpg", ".jpeg"):
        return [path]
    dur = sh(f'ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "{path}"').stdout.strip()
    try:
        dur = float(dur)
    except Exception:
        dur = 6.0
    out = []
    base = os.path.splitext(os.path.basename(path))[0]
    for k, pct in enumerate((0.2, 0.5, 0.8), 1):
        f = os.path.join(strips_dir, f"{base}_{k}.jpg")
        sh(f'ffmpeg -nostdin -y -ss {dur*pct:.2f} -i "{path}" -frames:v 1 -q:v 3 -vf scale=512:-1 "{f}"')
        if os.path.exists(f):
            out.append(f)
    return out


def make_strip(frames, name, strips_dir):
    imgs = [Image.open(f).convert("RGB") for f in frames if os.path.exists(f)]
    if not imgs:
        return None
    if len(imgs) == 1:
        out = os.path.join(strips_dir, f"{name}_one.jpg")
        imgs[0].save(out, quality=88)
        return out
    h = min(im.height for im in imgs)
    imgs = [im.resize((int(im.width * h / im.height), h)) for im in imgs]
    strip = Image.new("RGB", (sum(im.width for im in imgs), h))
    x = 0
    for im in imgs:
        strip.paste(im, (x, 0))
        x += im.width
    out = os.path.join(strips_dir, f"{name}_strip.jpg")
    strip.save(out, quality=88)
    return out


def judge(strip, timeout=180):
    try:
        r = subprocess.run([MIMO, "run", "--pure", "--dangerously-skip-permissions", RUBRIC, "-f", strip],
                           capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    d = extract_json(r.stdout or "")
    if not d or "plastic" not in d:
        return None, "no-json"
    try:
        v = max(0.0, min(100.0, float(d["plastic"])))
    except Exception:
        return None, "bad-num"
    return v, re.sub(r'[一-鿿]', '', str(d.get("reason", "")))[:70]


def judge_media(path: str, threshold: float = 55, timeout: int = 180) -> dict:
    """Синхронный гейт ОДНОГО файла (png/jpg/mp4) — для Фазы 1 (per-scene gate).
    Возвращает {"score": float|None, "verdict": "pass"|"REJECT"|"skip", "reason": str}."""
    with tempfile.TemporaryDirectory(prefix="scene_gate_") as strips_dir:
        name = os.path.splitext(os.path.basename(path))[0]
        strip = make_strip(frames_of(path, strips_dir), name, strips_dir)
        if not strip:
            return {"score": None, "verdict": "skip", "reason": "нет кадров"}
        score, reason = judge(strip, timeout=timeout)
        verdict = "skip" if score is None else ("REJECT" if score >= threshold else "pass")
        return {"score": score, "verdict": verdict, "reason": reason}
