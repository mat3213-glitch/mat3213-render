#!/usr/bin/env python3
"""
text_behind_object.py — размещение текста позади движущегося объекта.

Использует Depth-Anything-V2 для сегментации foreground/background по карте глубины.
Работает на GH Actions (CPU, ~1-2 fps на 720p).

Вход: видео (mp4) + текст
Выход: видео с текстом, расположенным позади foreground-объектов.

Запуск (GH Actions):
  python3 text_behind_object.py --input clip.mp4 --output result.mp4 \
      --text "YOUR TEXT" --depth-threshold 0.45 --font-size 96

Запуск локально (нужен torch + opencv):
  python3 text_behind_object.py --input clip.mp4 --output out.mp4 --text "HELLO"
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def load_depth_model():
    from transformers import pipeline as hf_pipeline
    return hf_pipeline("depth-estimation", model="depth-anything/Depth-Anything-V2-Small-hf")


def estimate_depth(depth_pipe, image_pil):
    """Карта глубины [0..1], 1 = ближе к камере."""
    result = depth_pipe(image_pil)
    depth = np.array(result["depth"], dtype=np.float32)
    dmin, dmax = float(depth.min()), float(depth.max())
    if dmax - dmin > 1e-6:
        depth = (depth - dmin) / (dmax - dmin)
    else:
        depth = np.zeros_like(depth)
    return depth


def depth_to_foreground_mask(depth, threshold=0.45, feather=15,
                              prev_mask=None, temporal_blend=0.65):
    """
    Преобразует карту глубины в маску foreground.
    threshold: порог — объекты ближе этого порога = foreground.
    feather: размытие краёв для плавного перехода.
    prev_mask: маска предыдущего кадра для временного сглаживания (anti-strobe).
    temporal_blend: доля предыдущей маски в смеси (0=только текущая, 1=только предыдущая).
    """
    mask = (depth > threshold).astype(np.uint8) * 255
    # Морфология: убираем шум, заполняем дыры (aggressive)
    kernel_large = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11))
    kernel_small = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel_large, iterations=3)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel_small, iterations=2)
    # Feather: Gaussian blur для плавных краёв
    if feather > 0:
        mask = cv2.GaussianBlur(mask, (feather * 2 + 1, feather * 2 + 1), 0)
    # Temporal smoothing: blend с предыдущей маской против стробоскопа
    if prev_mask is not None and prev_mask.shape == mask.shape:
        mask = cv2.addWeighted(prev_mask, temporal_blend,
                               mask, 1.0 - temporal_blend, 0)
    return mask


def create_text_layer(text, width, height, font_path, font_size,
                       color=(255, 255, 255, 230), shadow=True):
    """Создаёт RGBA-слой с текстом по центру."""
    img = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(font_path, font_size)
    except (OSError, IOError):
        font = ImageFont.load_default()
        print(f"[warn] шрифт {font_path} не найден, используем дефолтный")

    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (width - tw) // 2
    y = (height - th) // 2

    # Тень для читаемости
    if shadow:
        for dx, dy in [(2, 2), (3, 3)]:
            draw.text((x + dx, y + dy), text, fill=(0, 0, 0, 140), font=font)
    draw.text((x, y), text, fill=color, font=font)
    return img


def composite_frame(original_bgr, text_rgba, fg_mask, bg_mask_inv=None):
    """
    Композит 3 слоёв:
    - background: оригинал, но с областью под текстом (чтобы текст не перекрывал foreground)
    - text: текстовый слой
    - foreground: оригинальный кадр, замаскированный маской foreground

    Финал: background → text → foreground (сверху вниз по z-order).
    """
    h, w = original_bgr.shape[:2]

    # BGRA for original
    original_rgba = cv2.cvtColor(original_bgr, cv2.COLOR_BGR2BGRA)

    # Text layer as numpy
    text_np = np.array(text_rgba)  # (h, w, 4) RGBA

    # Alpha канал text → float
    text_alpha = text_np[:, :, 3:4].astype(np.float32) / 255.0

    # Foreground mask → float (0..1)
    fg_float = fg_mask.astype(np.float32) / 255.0
    fg_alpha = fg_float[:, :, np.newaxis]

    # Step 1: Background = original * (1 - text_alpha)
    # ( text не перекрывает фон ТОЛЬКО в области foreground )
    # Actually, simpler approach:
    # final = foreground_on_top * fg + (original * (1-text_alpha) + text * text_alpha) * (1-fg)

    # Text RGBA → float BGR
    text_bgr = text_np[:, :, :3].astype(np.float32)

    # Blended where text exists: bg behind text
    bg_behind_text = original_rgba[:, :, :3].astype(np.float32)
    blended_text = bg_behind_text * (1 - text_alpha) + text_bgr * text_alpha

    # Where foreground exists: use original (text is behind)
    # Where no foreground: use blended (text is visible)
    final = original_rgba[:, :, :3].astype(np.float32) * fg_alpha + \
            blended_text * (1 - fg_alpha)

    return final.astype(np.uint8)


def process_video(input_path, output_path, text, depth_threshold=0.45,
                   feather=15, font_path="/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                   font_size=96, max_frames=None):
    """Основной pipeline: видео → depth → mask → composite → видео."""

    # Получаем info о видео
    probe = sh([
        'ffprobe', '-v', 'error', '-select_streams', 'v:0',
        '-show_entries', 'stream=width,height,r_frame_rate,nb_frames',
        '-of', 'json', input_path
    ])
    import json
    info = json.loads(probe.stdout)['streams'][0]
    width = int(info['width'])
    height = int(info['height'])
    fps_parts = info['r_frame_rate'].split('/')
    fps = round(int(fps_parts[0]) / int(fps_parts[1]))
    nb_frames = int(info.get('nb_frames', 0))
    if max_frames and nb_frames > max_frames:
        nb_frames = max_frames
    print(f"[text_behind] {width}x{height} @ {fps}fps, {nb_frames} frames")

    # Создаём рабочие папки
    tmpdir = tempfile.mkdtemp(prefix="textbehind_")
    frames_dir = os.path.join(tmpdir, "frames")
    masks_dir = os.path.join(tmpdir, "masks")
    text_dir = os.path.join(tmpdir, "text")
    comp_dir = os.path.join(tmpdir, "composite")
    for d in [frames_dir, masks_dir, text_dir, comp_dir]:
        os.makedirs(d)

    try:
        # Step 1: Извлекаем кадры
        print("[1/4] Извлечение кадров...")
        sh([
            'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
            '-i', input_path, '-vf', f'fps={fps}',
            os.path.join(frames_dir, 'f%06d.png')
        ])

        frame_files = sorted(Path(frames_dir).glob('*.png'))
        if max_frames:
            frame_files = frame_files[:max_frames]
        print(f"  Извлечено {len(frame_files)} кадров")

        # Step 2: Depth + mask для каждого кадра (с temporal smoothing)
        print("[2/4] Depth-сегментация...")
        depth_pipe = load_depth_model()
        prev_mask = None

        for idx, fp in enumerate(frame_files, 1):
            img_pil = Image.open(fp).convert('RGB')
            depth = estimate_depth(depth_pipe, img_pil)
            mask = depth_to_foreground_mask(depth, threshold=depth_threshold,
                                            feather=feather, prev_mask=prev_mask,
                                            temporal_blend=0.65)
            prev_mask = mask.copy()
            cv2.imwrite(os.path.join(masks_dir, fp.name), mask)

            if idx % 25 == 0 or idx == len(frame_files):
                print(f"  [{idx}/{len(frame_files)}] depth+mask готово")

        # Step 3: Текстовый слой
        print("[3/4] Создание текстового слоя...")
        text_rgba = create_text_layer(text, width, height, font_path, font_size)
        text_rgba.save(os.path.join(text_dir, 'text.png'))

        # Step 4: Композит
        print("[4/4] Композитинг...")
        for idx, fp in enumerate(frame_files, 1):
            original = cv2.imread(str(fp))
            mask = cv2.imread(str(os.path.join(masks_dir, fp.name)), cv2.IMREAD_GRAYSCALE)
            composite = composite_frame(original, text_rgba, mask)
            cv2.imwrite(os.path.join(comp_dir, fp.name), composite)

            if idx % 25 == 0 or idx == len(frame_files):
                print(f"  [{idx}/{len(frame_files)}] composite готово")

        # Собираем видео
        print("Сборка видео...")
        sh([
            'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
            '-framerate', str(fps),
            '-i', os.path.join(comp_dir, 'f%06d.png'),
            '-i', input_path,
            '-map', '0:v', '-map', '1:a?',
            '-c:v', 'libx264', '-profile:v', 'main', '-pix_fmt', 'yuv420p',
            '-crf', '20', '-movflags', '+faststart',
            '-shortest',
            output_path
        ])

        ok = os.path.exists(output_path) and os.path.getsize(output_path) > 10000
        print(f"\n{'✅ ГОТОВО' if ok else '❌ ОШИБКА'}: {output_path}")
        return ok

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="Текст позади объекта (depth-сегментация)")
    ap.add_argument("--input", required=True, help="Входное видео (mp4)")
    ap.add_argument("--output", required=True, help="Выходное видео (mp4)")
    ap.add_argument("--text", required=True, help="Текст для размещения")
    ap.add_argument("--depth-threshold", type=float, default=0.45,
                    help="Порог глубины для foreground (0..1, выше = ближе к камере)")
    ap.add_argument("--feather", type=int, default=15,
                    help="Размытие краёв маски (px)")
    ap.add_argument("--font-size", type=int, default=192,
                    help="Размер шрифта (дефолт 192)")
    ap.add_argument("--font-path", type=str,
                    default="/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                    help="Путь к шрифту")
    ap.add_argument("--max-frames", type=int, default=None,
                    help="Макс. кадров для обработки (для тестов)")
    args = ap.parse_args()

    ok = process_video(
        args.input, args.output, args.text,
        depth_threshold=args.depth_threshold,
        feather=args.feather,
        font_path=args.font_path,
        font_size=args.font_size,
        max_frames=args.max_frames,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
