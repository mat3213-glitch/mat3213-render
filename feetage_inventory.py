#!/usr/bin/env python3
"""
feetage_inventory.py — инвентаризация футаж-библиотеки (рецепт 2, блок А, п.5).

Для каждого видео из feetage_dir на ЯД:
  1. Численно: доминирующая палитра (Pillow, QUANT_NUMS цветов) и интенсивность
     смены кадров (scenedetect по среднему абсолютному разбросу кадров) —
     эти числа модель СЧИТАТЬ НЕ УМЕЕТ (замеры: «у цифр есть проверяемость»), берём кодом.
  2. Vision-ансамбль (art_judge.ask_vision) смотрит контакт-лист из N кадров
     по времени и отдаёт качественные теги: сюжет/вайб/темп, куда такой
     футаж ляжет (палитра, движение), что на кадре (неон/люди/текст = брак).
  3. Лента уходит на ЯД в feetage_dir/inventory/<имя>.json (по одному на видео)
     + контакт-листы в feetage_dir/inventory/sheets/.

Запуск — на GH Actions (взрослые рендеры и скачивание футажей только там).
Env: OPENROUTER_API_KEY + CLOUDFLARE_WORKER/WORKER_SECRET (vision-транспорты),
     YDRIVE_* (rclone конфиг готовит воркфлоу).
CLI: python feetage_inventory.py --feetage-dir "Content factory/cloud_io/.../raw"
"""
import argparse
import base64
import json
import os
import re
import sys
import subprocess
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from art_judge import MODELS, PANEL, ask_vision, to_jpeg_b64

REMOTE = "ydrive:"
QUANT_NUMS = 5        # сколько доминирующих цветов возвращать
SHEET_FRAMES = 6      # кадров в контакт-листе
SHEET_W = 360         # ширина тайла
SAMPLE_SEC = 0.5      # шаг выборки кадров для числа смен
SAMPLE_N = 40         # макс. выборок для числа смен (источник может быть длинным)


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def probe_dur(path: Path) -> float:
    r = sh(["ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "csv=p=0", str(path)])
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def sample_frames(path: Path, dur: float, n: int, tmp: Path) -> list[Path]:
    """n кадров равномерно по времени → списки jpg в tmp."""
    if dur <= 0:
        return []
    out = []
    for i in range(n):
        t = dur * (i + 0.5) / n
        p = tmp / f"fr_{i:02d}.jpg"
        r = sh(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}",
                "-i", str(path), "-frames:v", "1", "-vf", "scale=640:-2", str(p)])
        if r.returncode == 0 and p.exists():
            out.append(p)
    return out


def dominant_palette(frames: list[Path]) -> list[dict]:
    """Доминирующие цвета по всем кадрам: hex, доля, upscaled-образец окраски."""
    try:
        from PIL import Image
    except ImportError:
        return []
    samples = []
    for fr in frames:
        with Image.open(fr) as im:
            samples.append(list(im.convert("RGB").resize((64, 64)).getdata()))
    if not samples:
        return []
    flat = [p for s in samples for p in s]
    q = Image.new("RGB", (len(flat), 1))
    q.putdata(flat)
    q = q.quantize(colors=QUANT_NUMS, method=Image.MEDIANCUT)
    pal = q.getpalette()
    cnt = {}
    for v in q.getdata():
        cnt[v] = cnt.get(v, 0) + 1
    tot = len(flat)
    out = []
    for idx, count in sorted(cnt.items(), key=lambda kv: -kv[1])[:QUANT_NUMS]:
        r, g, b = pal[idx * 3: idx * 3 + 3]
        out.append({"hex": "#%02X%02X%02X" % (r, g, b), "share": round(count / tot, 3)})
    return out


def scene_change_score(path: Path, dur: float, tmp: Path) -> float:
    """Интенсивность смены кадров = смен в минуту. Грубая, по разности соседних
    сэмплов (не вызов ffmpeg-скан сцены на весь файл — дёшево на раннере)."""
    n = max(3, min(int(dur / SAMPLE_SEC), SAMPLE_N))
    frames = sample_frames(path, dur, n, tmp)
    if len(frames) < 2:
        return 0.0
    try:
        from PIL import Image
    except ImportError:
        return 0.0
    prev = None
    jumps = 0.0
    for fr in frames:
        with Image.open(fr) as im:
            cur = im.convert("L").resize((32, 32))
            px = list(cur.getdata())
        if prev is not None:
            diff = sum(abs(a - b) for a, b in zip(prev, px)) / (32 * 32)
            if diff > 18:
                jumps += 1
        prev = px
    span = dur * (len(frames) - 1) / len(frames)
    return round(jumps * 60.0 / max(span, 1.0), 1) if span > 0 else 0.0


def contact_sheet(frames: list[Path], label: str, out: Path) -> bool:
    if not frames:
        return False
    cols = min(3, len(frames))
    rows = (len(frames) + cols - 1) // cols
    font = next((f for f in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                             "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]
                 if os.path.exists(f)), None)
    import shutil
    work = out.parent / "tiles"; work.mkdir(exist_ok=True)
    tiles = []
    for i, fr in enumerate(frames):
        tp = work / f"t{i}.png"
        vf = f"scale={SHEET_W}:{SHEET_W}:force_original_aspect_ratio=increase,crop={SHEET_W}:{SHEET_W}"
        if font:
            vf += (f",drawtext=fontfile={font}:text='{label} #{i}':x=10:y=H-30:fontsize=20:"
                   f"fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=4")
        r = sh(["ffmpeg", "-y", "-loglevel", "error", "-i", str(fr), "-vf", vf, str(tp)])
        if r.returncode == 0 and tp.exists():
            tiles.append(tp)
    if not tiles:
        return False
    inputs, layout = [], []
    for i, t in enumerate(tiles):
        inputs += ["-i", str(t)]
        layout.append(f"{(i % cols) * SHEET_W}_{(i // cols) * SHEET_W}")
    r = sh(["ffmpeg", "-y", "-loglevel", "error", *inputs,
            "-filter_complex",
            f"xstack=inputs={len(tiles)}:layout={'|'.join(layout)}:fill=black",
            "-q:v", "3", str(out)])
    shutil.rmtree(work, ignore_errors=True)
    return out.exists()


def build_prompt(name: str, palette: list[dict], scpm: float) -> str:
    pal = ", ".join(p["hex"] for p in palette)
    return (
        "Ты арт-директор видеомонтажа yaromat (Future Garage / downtempo). "
        f"На контакт-листе — кадры ОДНОГО видео-футажа «{name}» (метки #0..#N по времени).\n"
        f"Численно замерено: доминирующая палитра [{pal}], смена кадров ≈ {scpm} в минуту "
        "(0-5 = почти статика/плавный дрейф, >20 = активный монтаж/склейки).\n\n"
        "Верни СТРОГО одним JSON без markdown:\n"
        '{"subject":"<что на кадре, 3-6 слов>","vibe":"<вайб: внутренняя глубина/нуар/вода/...>",'
        '"motion":"low|mid|high",'
        '"palette_ok":true|false,"scale_hint":"<во сколько раз уйти внутрь кадра для спокойного '
        'дрейфа, напр. 1.15>",'
        '"flaws":["<только реально видимое: текст/лицо/неон/лого/грязь>"],'
        '"use":"<куда ляжет: например под дымкой/после дождя/на выдохе клипа>"}\n'
        "Если кадр ИСПОРЧЕН (резкий мыльный шум, полный чёрный, гигантский логотип) — "
        "flaws содержит 'брак'. Не выдумывай, чего не видно."
    )


def run_inventory(feetage_dir: str, workdir: Path) -> list[Path]:
    listing = sh(["rclone", "lsf", f"{REMOTE}{feetage_dir}"])
    if listing.returncode != 0:
        sys.exit(f"нельзя прочитать {feetage_dir}: {listing.stderr}")
    names = sorted(n.strip() for n in listing.stdout.splitlines()
                   if n.lower().endswith((".mp4", ".mov", ".mkv", ".webm")))
    if not names:
        sys.exit(f"в {feetage_dir} нет видео")
    inv_dir = workdir / "inventory"; inv_dir.mkdir(exist_ok=True)
    sheets_dir = inv_dir / "sheets"; sheets_dir.mkdir(exist_ok=True)
    ok, fail = [], []
    for name in names:
        src = workdir / name
        if sh(["rclone", "copyto", f"{REMOTE}{feetage_dir}/{name}", str(src)]).returncode != 0:
            fail.append((name, "rclone download"));
            continue
        dur = probe_dur(src)
        try:
            with tempfile.TemporaryDirectory(dir=str(workdir)) as td:
                tmp = Path(td)
                frames = sample_frames(src, dur, SHEET_FRAMES, tmp)
                palette = dominant_palette(frames)
                scpm = scene_change_score(src, dur, tmp)
                sheet = sheets_dir / f"{Path(name).stem}.jpg"
                sheet_ok = contact_sheet(frames, Path(name).stem, sheet)
                record = {"file": name, "dur_s": round(dur, 2), "palette": palette,
                          "scene_cpm": scpm, "sheet": sheet.name if sheet_ok else None}
                if sheet_ok:
                    # Ансамбль с failover, как в art_judge.judge_file: PANEL живых судей,
                    # резерв подключается при выбытии по квоте (dead), а не один MODELS[0].
                    b64 = to_jpeg_b64(sheet)
                    votes, dead = [], {}
                    for m in MODELS:
                        if len(votes) >= PANEL:
                            break
                        if dead.get(m, 0) >= 2:
                            continue
                        v, err = ask_vision(m, build_prompt(name, palette, scpm), b64)
                        if v is None and err and "rate-limit" in err:
                            dead[m] = dead.get(m, 0) + 1
                        elif v is not None:
                            dead[m] = 0
                            v["_judge"] = m
                            votes.append(v)
                        time.sleep(1)
                    if votes:
                        record["labels"] = {k: votes[0].get(k) for k in
                                            ("subject", "vibe", "motion", "palette_ok",
                                             "scale_hint", "flaws", "use")}
                        record["labels"]["_judges"] = [v.get("_judge") for v in votes]
                        if len(votes) > 1:
                            record["labels"]["_n"] = len(votes)
                    else:
                        record["labels"] = {"_err": "нет живых судей (квота)"}
                (inv_dir / f"{Path(name).stem}.json").write_text(
                    json.dumps(record, ensure_ascii=False), encoding="utf-8")
                ok.append(name)
                print(f"  • {name}: {dur:.1f}с, {scpm} смен/мин, "
                      f"цветов {len(palette)}" + (f", лист {sheet.name}" if sheet_ok else ", нет листа"))
        except Exception as e:
            fail.append((name, str(e)))
    idx = inv_dir / "inventory.json"
    idx.write_text(json.dumps({"feetage_dir": feetage_dir, "count": len(ok),
                               "failed": [{"name": a, "err": b} for a, b in fail]},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    return ok


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--feetage-dir", required=True)
    p.add_argument("--workdir", default="/tmp/feetage_inv")
    args = p.parse_args()
    if not (os.environ.get("OPENROUTER_API_KEY") or os.environ.get("CLOUDFLARE_WORKER")):
        print("[inv] нет vision-транспорта — только числа")
    else:
        print(f"[inv] vision-ансамбль: {len(MODELS)} моделей, ведущая {MODELS[0]}")
    workdir = Path(args.workdir); workdir.mkdir(parents=True, exist_ok=True)
    ok = run_inventory(args.feetage_dir, workdir)
    dst = f"{REMOTE}{args.feetage_dir}"
    r = sh(["rclone", "copyto", str(workdir / "inventory/inventory.json"),
            f"{dst}/inventory/inventory.json"])
    print(f"[inv] {len(ok)} занесено; inventory.json -> {dst}/inventory/ "
          f"({'ok' if r.returncode == 0 else 'UPLOAD FAIL'})")
    for name in ok:
        stem = Path(name).stem
        # листы и per-video json — отдельными copyto, по одному файлу
        r1 = sh(["rclone", "copyto", str(workdir / "inventory" / f"{stem}.json"),
                 f"{dst}/inventory/{stem}.json"])
        r2 = sh(["rclone", "copyto", str(workdir / "inventory/sheets" / f"{stem}.jpg"),
                 f"{dst}/inventory/sheets/{stem}.jpg"])
        if r1.returncode or r2.returncode:
            print(f"[inv] WARN: upload {name}")


if __name__ == "__main__":
    main()