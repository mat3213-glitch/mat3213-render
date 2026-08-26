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
                       color=(255, 255, 255, 255), shadow=True, pos=None):
    """Создаёт RGBA-слой с текстом + тёмная полоса-фон для читаемости. pos=(x,y) — центр."""
    img = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(font_path, font_size)
    except (OSError, IOError):
        font = ImageFont.load_default()
        print(f"[warn] шрифт {font_path} не найден, используем дефолтный")

    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if pos is not None:
        x = pos[0] - tw // 2
        y = pos[1] - th // 2
    else:
        x = (width - tw) // 2
        y = (height - th) // 2

    # Тёмная полоса-фон за текстом для гарантированной читаемости
    pad = 20
    bar_bbox = (x - pad, y - pad, x + tw + pad, y + th + pad)
    draw.rounded_rectangle(bar_bbox, radius=12, fill=(0, 0, 0, 160))

    # Текст с тёмной обводкой
    for dx in range(-3, 4):
        for dy in range(-3, 4):
            if dx*dx + dy*dy <= 9:
                draw.text((x + dx, y + dy), text, fill=(0, 0, 0, 220), font=font)
    draw.text((x, y), text, fill=color, font=font)
    return img


def composite_frame(original_bgr, text_rgba, fg_mask):
    """
    Композит: текст позади foreground.
    final = original * fg + (original_bg_behind_text + text) * (1 - fg)
    Текст виден ТОЛЬКО в областях без foreground (на фоне).
    """
    h, w = original_bgr.shape[:2]
    orig_f = original_bgr.astype(np.float32)

    text_np = np.array(text_rgba)
    text_alpha = text_np[:, :, 3:4].astype(np.float32) / 255.0
    text_bgr = text_np[:, :, :3].astype(np.float32)

    fg = fg_mask.astype(np.float32) / 255.0
    fg3 = fg[:, :, np.newaxis]

    # Blend text onto original (text replaces original where text exists)
    bg_with_text = orig_f * (1 - text_alpha) + text_bgr * text_alpha

    # Final: original where foreground, text-blended where background
    final = orig_f * fg3 + bg_with_text * (1 - fg3)
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

        # Step 3+4: Композит с текстом позади объекта (текст = на centroid foreground)
        print("[3/4] Композитинг (текст за объектом)...")
        for idx, fp in enumerate(frame_files, 1):
            original = cv2.imread(str(fp))
            mask = cv2.imread(str(os.path.join(masks_dir, fp.name)), cv2.IMREAD_GRAYSCALE)

            # Находим центр mass foreground объекта
            moments = cv2.moments(mask)
            if moments["m00"] > 0:
                cx = int(moments["m10"] / moments["m00"])
                cy = int(moments["m01"] / moments["m00"])
            else:
                cx, cy = width // 2, height // 2

            # Текст позиционируется по центру объекта
            text_rgba = create_text_layer(text, width, height, font_path, font_size, pos=(cx, cy))
            composite = composite_frame(original, text_rgba, mask)
            cv2.imwrite(os.path.join(comp_dir, fp.name), composite)

            if idx % 25 == 0 or idx == len(frame_files):
                print(f"  [{idx}/{len(frame_files)}] composite @ ({cx},{cy})")

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
