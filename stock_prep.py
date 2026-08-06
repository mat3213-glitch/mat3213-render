#!/usr/bin/env python3
"""
stock_prep.py — стоковое видео → готовый вертикальный сегмент под клип.

Звено между банком (Coverr/Pexels/Wikimedia) и `treatment_overlayfx.py`, который вешает
филмик-оверлеи. Здесь только подготовка кадра: обрезка, замедление, вертикаль, уникализация,
грейд. Оверлеи и склейка — дальше по цепочке.

ПОЧЕМУ ЗАМЕДЛЕНИЕ ПЕРВЫМ ШАГОМ (решение yaromat 06.08): сток снят «нормальной» скоростью,
а трек — downtempo. Замедление ×0.5 и есть главный приём: оно и меняет темп под музыку, и
переписывает временной отпечаток файла (половина уникализации бесплатно).

ПРАВИЛА ПРОЕКТА, ЗАШИТЫЕ СЮДА:
  * футаж-на-футаж ЗАПРЕЩЁН — здесь ровно один вход, второго слоя нет by design;
  * зерно только `noise=allf=u` (`allf=t+u` бьёт по I-фреймам и раздувает файл ×10);
  * никакого неона: грейд уводит в холод и ГАСИТ насыщенность, а не поднимает;
  * fps унифицируется всегда — банки отдают 24/25/30/60, без унификации склейка дублирует кадры.

Usage:
  python3 stock_prep.py IN.mp4 OUT.mp4 [--start 2 --dur 8 --slow 2.0 --seed 2013]
"""
import argparse
import random
import subprocess
import sys


def probe(path: str, entries: str) -> str:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", entries, "-of", "default=nw=1:nk=1", path],
                       capture_output=True, text=True, timeout=60)
    return r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""


def measure_luma(path: str, at: float) -> float:
    """Средняя яркость одного кадра (0..255). Нужна, чтобы посадить весь пул в одну экспозицию."""
    import io
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(at), "-i", path, "-frames:v", "1",
                        "-f", "image2pipe", "-vcodec", "png", "-"],
                       capture_output=True, timeout=300)
    if r.returncode != 0 or not r.stdout:
        return -1.0
    try:
        from PIL import Image
        import numpy as np
        im = Image.open(io.BytesIO(r.stdout)).convert("L")
        return float(np.asarray(im, dtype="float32").mean())
    except Exception:
        return -1.0


def build_filter(a, mirror: bool, zoom: float) -> str:
    """Цепочка фильтров одним куском — промежуточных перекодировок не делаем."""
    steps = [f"setpts={a.slow}*PTS", f"fps={a.fps}"]
    if mirror:
        steps.append("hflip")
    # Вертикаль: режем по высоте исходника, центр. Зум добавляет крупности и заодно
    # срезает края — узнаваемость стокового кадра падает.
    steps.append(f"crop=in_h*{a.w}/{a.h}/{zoom}:in_h/{zoom}")
    steps.append(f"scale={a.w}:{a.h}")
    # Грейд: гасим насыщенность, чуть контраста, холодный сдвиг (teal — наш эталон).
    steps.append(f"eq=saturation={a.sat}:contrast={a.contrast}:brightness={a.bright}")
    steps.append("colorbalance=rs=-0.04:gs=0.02:bs=0.05:rm=-0.03:bm=0.04")
    if a.grain > 0:
        steps.append(f"noise=alls={a.grain}:allf=u")
    if a.vignette > 0:
        steps.append(f"vignette=PI*{a.vignette}")
    return ",".join(steps)


def main() -> int:
    p = argparse.ArgumentParser(description="Стоковое видео → вертикальный сегмент под клип")
    p.add_argument("src"); p.add_argument("out")
    p.add_argument("--start", type=float, default=0.0, help="секунда начала в исходнике")
    p.add_argument("--dur", type=float, default=0.0, help="сколько взять ИЗ ИСХОДНИКА (0=всё)")
    p.add_argument("--slow", type=float, default=2.0, help="множитель PTS: 2.0 = вдвое медленнее")
    p.add_argument("--w", type=int, default=720)
    p.add_argument("--h", type=int, default=1280)
    p.add_argument("--fps", type=int, default=24)
    p.add_argument("--zoom", type=float, default=1.0, help=">1 = ближе (кроп у́же)")
    p.add_argument("--sat", type=float, default=0.82)
    p.add_argument("--contrast", type=float, default=1.06)
    # Замер на первом пуле 06.08: bright −0.02 + виньетка 0.28 на и без того тёмных стоках
    # утопили рябь моря и горы в тумане в чёрное. Дефолты ослаблены.
    p.add_argument("--bright", type=float, default=0.0)
    p.add_argument("--grain", type=int, default=8, help="0 = без зерна")
    p.add_argument("--vignette", type=float, default=0.18, help="0 = без виньетки")
    p.add_argument("--no-mirror", action="store_true", help="не зеркалить (по умолчанию — по seed)")
    p.add_argument("--seed", type=int, default=0, help="детерминизм зеркала/зума на трек")
    p.add_argument("--norm-luma", type=float, default=0.0,
                   help="целевая средняя яркость 0..255 (напр. 96) — сажает пул в одну экспозицию; 0 = выкл")
    p.add_argument("--crf", type=int, default=22)
    a = p.parse_args()

    rng = random.Random(a.seed)
    mirror = (not a.no_mirror) and rng.random() < 0.5
    zoom = a.zoom if a.zoom != 1.0 else round(rng.uniform(1.0, 1.12), 3)

    # Нормализация экспозиции: без неё каждый сегмент грейдится сам по себе и в ряду
    # получаются светлые пятна вперемешку с провалами (замер на пуле 06.08).
    norm_note = ""
    if a.norm_luma > 0:
        cur = measure_luma(a.src, a.start + 0.5)
        if cur > 0:
            delta = max(-0.25, min(0.25, (a.norm_luma - cur) / 255.0))
            a.bright = round(a.bright + delta, 4)
            norm_note = f", яркость {cur:.0f}→{a.norm_luma} (bright {a.bright:+.3f})"

    src_dur = probe(a.src, "stream=duration") or probe(a.src, "format=duration")
    cmd = ["ffmpeg", "-v", "error", "-y"]
    if a.start:
        cmd += ["-ss", str(a.start)]
    if a.dur:
        cmd += ["-t", str(a.dur)]
    # 🔴 pix_fmt И faststart ОБЯЗАТЕЛЬНЫ (поймано 06.08 на первом пуле — 17 файлов не
    # открылись у владельца): фильтры noise/vignette работают в 4:4:4, и libx264 без явного
    # -pix_fmt сохранял «High 4:4:4 Predictive» — такой файл не играет ни ЯД-просмотр, ни
    # телефон. Без +faststart moov уезжает в хвост и потоковый просмотр не стартует.
    cmd += ["-i", a.src, "-vf", build_filter(a, mirror, zoom), "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(a.crf),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", a.out]

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        print(f"ffmpeg rc={r.returncode}: {r.stderr.strip()[-400:]}", file=sys.stderr)
        return 1

    out_dur = probe(a.out, "stream=duration") or probe(a.out, "format=duration")
    print(f"✅ {a.out}: {a.w}x{a.h} {a.fps}fps, {out_dur}с "
          f"(из {src_dur}с, ×{a.slow}, зеркало={'да' if mirror else 'нет'}, зум={zoom}{norm_note})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
