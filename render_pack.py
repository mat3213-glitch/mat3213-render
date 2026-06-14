#!/usr/bin/env python3
"""
render_pack.py — S3.3: «рендер-пак» под тренд. Тащит CC-картинки с Openverse по тренд-запросам,
накладывает предложенный тренд-грейд, собирает контакт-лист и шлёт в тред 634 — чтобы yaromat
выбрал, что гнать в боевой тест-рендер.

ВАЖНО: используем CC0/BY с Openverse (легально), НЕ скачанные с X референсы.
mimo НЕ нужен (детерминированная сборка) → дёшево.

Входы env (dispatch-inputs от облачного бота): GRADE_JSON {name,eq,balance}, QUERIES_JSON [..].
Реюз Openverse-клиента и TG-отправки из style_scout.py.
"""
import json, os, subprocess
from pathlib import Path
import style_scout as ss

HERE = Path(__file__).resolve().parent
PACKS = HERE / "packs"
WORK = Path("/tmp/render_pack"); WORK.mkdir(parents=True, exist_ok=True)


def apply_grade(src, grade, out):
    eq = grade.get("eq", "contrast=1.05:saturation=0.7")
    bal = grade.get("balance")
    vf = f"scale=480:480:force_original_aspect_ratio=increase,crop=480:480,format=gbrp,eq={eq}"
    if bal:
        vf += f",colorbalance={bal}"
    vf += ",format=yuv420p,noise=alls=12:all_seed=7:allf=t+u,vignette=angle=PI/4.5"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vf", vf, str(out)],
                   capture_output=True, timeout=60)
    return out.exists()


def montage(tiles, out):
    from PIL import Image, ImageDraw
    cols = min(3, len(tiles)); rows = (len(tiles) + cols - 1) // cols; W = 480
    sheet = Image.new("RGB", (cols * W, rows * W), (15, 18, 24))
    dr = ImageDraw.Draw(sheet)
    for i, fp in enumerate(tiles):
        try:
            im = Image.open(fp).convert("RGB"); im.thumbnail((W, W))
            x, y = (i % cols) * W, (i // cols) * W
            sheet.paste(im, (x, y))
            dr.rectangle([x + 2, y + 2, x + 32, y + 26], fill=(0, 0, 0))
            dr.text((x + 9, y + 7), str(i + 1), fill=(255, 255, 255))
        except Exception:
            continue
    sheet.save(out, quality=85)
    return True


def main():
    grade = json.loads(os.environ.get("GRADE_JSON", "{}") or "{}")
    queries = json.loads(os.environ.get("QUERIES_JSON", "[]") or "[]") or ss.QUERIES[:3]
    token = ss.ov_token()
    print(f"[pack] грейд={grade.get('name','?')} | запросов={len(queries)}")

    urls = []
    for q in queries[:4]:
        urls += ss.ov_search(q, 2, token)
    tiles, srcs = [], []
    for i, u in enumerate(urls[:8]):
        raw = WORK / f"r{i}.jpg"
        if ss.ov_download(u, raw):
            g = WORK / f"g{i}.jpg"
            if apply_grade(raw, grade, g):
                tiles.append(g); srcs.append(u)
        if len(tiles) >= 6:
            break
    print(f"[pack] картинок в паке: {len(tiles)}")
    if not tiles:
        return

    sheet = WORK / "pack.jpg"
    montage(tiles, sheet)
    # ПЕРСИСТ манифеста пака в репо (для /pack_render N): grade + источники по порядку тайлов
    PACKS.mkdir(parents=True, exist_ok=True)
    (PACKS / "latest.json").write_text(
        json.dumps({"grade": grade, "sources": srcs}, ensure_ascii=False, indent=2), encoding="utf-8")
    cap = (f"🎨 Рендер-пак — тренд-лук «{grade.get('name','?')}» на CC-картинках (Openverse). "
           f"{len(tiles)} вариантов. В видео-тест: /pack_render N")
    ss.tg_photo(sheet, cap)
    print("[pack] готово")


if __name__ == "__main__":
    main()
