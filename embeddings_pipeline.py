#!/usr/bin/env python3
"""
embeddings_pipeline.py — SigLIP-эмбеддинги клипов для семантического поиска.

Каталог клипов (overlay/vinil/soundwave) → ffmpeg кадр → SigLIP 768-dim → LanceDB.
Аудио НЕ используется (клипы беззвучные оверлеи).

Usage:
  python3 embeddings_pipeline.py --build              # пересчитать все
  python3 embeddings_pipeline.py --stats              # статистика
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

try:
    import lancedb
    HAS_LANCE = True
except ImportError:
    HAS_LANCE = False

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "embeddings.lance"
CATALOG_YD = "ydrive:Content factory/assets/footage_catalog"
SIGLIP_MODEL = "google/siglip-base-patch16-224"


def _rclone(*args, timeout=300):
    return subprocess.run(["rclone", *args], capture_output=True, text=True, timeout=timeout)


def load_catalog() -> list[dict]:
    r = _rclone("cat", f"{CATALOG_YD}/catalog.jsonl")
    items = []
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            line = line.strip()
            if line:
                try:
                    items.append(json.loads(line))
                except Exception:
                    pass
    return items


def fetch_clip(entry: dict, dest: Path) -> Path | None:
    rel = entry["path"].split("footage_catalog/", 1)[-1]
    local = dest / Path(rel).name
    if local.exists():
        return local
    r = _rclone("copyto", f"{CATALOG_YD}/{rel}", str(local))
    if r.returncode == 0 and local.exists():
        return local
    return None


def extract_frame(clip_path: Path, timestamp: float = 1.0) -> Path | None:
    """Кадр из видео через ffmpeg (3 точки: 1с, 25%, 75%)."""
    out = clip_path.parent / f"{clip_path.stem}_frames"
    out.mkdir(exist_ok=True)
    frames = []
    duration_r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(clip_path)],
        capture_output=True, text=True, timeout=10
    )
    try:
        dur = float(duration_r.stdout.strip())
    except ValueError:
        dur = 5.0

    for t in [1.0, dur * 0.25, dur * 0.75]:
        frame = out / f"frame_{int(t*1000)}.jpg"
        if not frame.exists():
            subprocess.run(
                ["ffmpeg", "-y", "-ss", str(t), "-i", str(clip_path),
                 "-vframes", "1", "-q:v", "2", str(frame)],
                capture_output=True, timeout=15
            )
        if frame.exists():
            frames.append(frame)
    return frames if frames else None


_SIGLIP_PROCESSOR = None
_SIGLIP_MODEL = None


def _load_siglip():
    global _SIGLIP_PROCESSOR, _SIGLIP_MODEL
    if _SIGLIP_PROCESSOR is None:
        from transformers import AutoProcessor, AutoModel
        print(f"[embeddings] загружаю SigLIP {SIGLIP_MODEL}...")
        _SIGLIP_PROCESSOR = AutoProcessor.from_pretrained(SIGLIP_MODEL)
        _SIGLIP_MODEL = AutoModel.from_pretrained(SIGLIP_MODEL)
        _SIGLIP_MODEL.eval()
        print("[embeddings] SigLIP загружен")
    return _SIGLIP_PROCESSOR, _SIGLIP_MODEL


def extract_siglip_embedding(frame_paths: list[Path]) -> np.ndarray | None:
    """SigLIP: усреднённый эмбеддинг нескольких кадров (768-dim). Модель грузится один раз."""
    if not HAS_PIL:
        return None
    try:
        import torch
        processor, model = _load_siglip()
        embeddings = []
        for fp in frame_paths:
            try:
                image = Image.open(fp).convert("RGB")
                inputs = processor(images=image, return_tensors="pt")
                with torch.no_grad():
                    # get_image_features возвращает тензор (batch, dim), не NamedTuple
                    emb = model.get_image_features(**inputs).squeeze().numpy()
                embeddings.append(emb)
            except Exception as e:
                print(f" (frame err: {e})", end="")
                continue

        if not embeddings:
            return None

        mean_emb = np.mean(embeddings, axis=0)
        norm = np.linalg.norm(mean_emb)
        return mean_emb / norm if norm > 0 else mean_emb
    except Exception as e:
        print(f" (siglip err: {e})", end="")
        return None


def build_database(into_db: Path = DB_PATH):
    if not HAS_LANCE:
        sys.exit("pip install lancedb")
    if not HAS_PIL:
        sys.exit("pip install Pillow")

    catalog = load_catalog()
    print(f"[embeddings] каталог: {len(catalog)} клипов")

    db = lancedb.connect(str(into_db))
    records = []

    with tempfile.TemporaryDirectory() as tmp:
        for i, entry in enumerate(catalog):
            clip_id = entry.get("id", f"clip_{i}")
            print(f"  [{i+1}/{len(catalog)}] {clip_id} ({entry.get('category', '?')})", end=" ")

            clip = fetch_clip(entry, Path(tmp))
            if not clip:
                print("SKIP (нет файла)")
                continue

            frames = extract_frame(clip)
            if not frames:
                print("SKIP (нет кадров)")
                continue

            emb = extract_siglip_embedding(frames)
            if emb is None:
                print("SKIP (SigLIP не удался)")
                continue

            records.append({
                "id": clip_id,
                "category": entry.get("category", ""),
                "tags": json.dumps(entry.get("tags", [])),
                "duration": entry.get("duration", 0),
                "orientation": entry.get("orientation", ""),
                "path": entry.get("path", ""),
                "visual_embedding": emb.tolist(),
            })
            print(f"OK (dim={len(emb)})")

    if not records:
        print("[embeddings] нет записей")
        return

    try:
        db.drop_table("clips")
    except Exception:
        pass
    tbl = db.create_table("clips", records)
    tbl.create_index(metric="cosine", num_partitions=min(len(records) // 10 + 1, 256))
    print(f"\n[embeddings] база: {len(records)} записей, dim=768 → {into_db}")


def show_stats(db_path: Path = DB_PATH):
    if not HAS_LANCE:
        sys.exit("pip install lancedb")
    if not db_path.exists():
        print("База не найдена. Запусти --build")
        return
    db = lancedb.connect(str(db_path))
    try:
        tbl = db.open_table("clips")
    except Exception as e:
        print(f"Таблица clips не найдена (--build не завершил запись): {e}")
        return
    df = tbl.to_pandas()
    print(f"Всего: {len(df)} клипов")
    if "category" in df.columns:
        for cat, cnt in df["category"].value_counts().items():
            print(f"  {cat}: {cnt}")
    if "visual_embedding" in df.columns:
        print(f"dim: {len(df['visual_embedding'].iloc[0])}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    args = ap.parse_args()

    if args.stats:
        show_stats(args.db)
    elif args.build:
        build_database(args.db)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
