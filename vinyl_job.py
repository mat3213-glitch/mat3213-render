#!/usr/bin/env python3
"""
vinyl_job.py — GitHub Actions runner: spinning vinyl record teaser.

PIL рендерит каждый кадр (rotate+composite), пайпит в ffmpeg.
Без filter_complex — чисто и предсказуемо.

job.json: {duration, format, out_name, audio_start, title, track_name, bg_color}
Environment: JOB_ID
"""

import json, math, os, subprocess, sys
from pathlib import Path

JOB_ID = os.environ.get("JOB_ID", "")
if not JOB_ID:
    sys.exit("JOB_ID not set")

REMOTE  = "ydrive"
JOBS_YD = "Content factory/cloud_io/render_jobs"
JOB_YD  = f"{JOBS_YD}/{JOB_ID}"
WORKDIR = Path("/tmp/vinyl_job")
WORKDIR.mkdir(parents=True, exist_ok=True)

FMT_DIMS = {"square": (1080, 1080), "vertical": (1080, 1920)}
FONT     = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
RPM = 33.333
RPS = RPM / 60.0
FPS = 25


def yd_get(remote_path: str, local: Path) -> bool:
    local.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["rclone", "copyto", f"{REMOTE}:{remote_path}", str(local)],
                       capture_output=True, text=True)
    return r.returncode == 0

def yd_put(local: Path, remote_path: str) -> bool:
    r = subprocess.run(["rclone", "copyto", str(local), f"{REMOTE}:{remote_path}"],
                       capture_output=True, text=True)
    return r.returncode == 0

def yd_put_text(text: str, remote_path: str):
    tmp = WORKDIR / "_s.txt"; tmp.write_text(text); yd_put(tmp, remote_path)


def parse_color(h: str) -> tuple:
    """'0x0a0a12' или '#0a0a12' → (10, 10, 18). Безопасно."""
    h = h.strip().replace("0x", "").replace("#", "").zfill(6)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def make_vinyl(size: int, label_art_path, title: str, track_name: str) -> "Image":
    """Рисует vinyl PNG (RGBA, прозрачный фон). Текст фиксирован — не вращается."""
    from PIL import Image, ImageDraw, ImageFont, ImageChops
    ImageChops_multiply = ImageChops.multiply
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx, cy = size // 2, size // 2
    r = size // 2 - 6

    # Тело пластинки
    draw.ellipse([cx-r, cy-r, cx+r, cy+r], fill=(14, 14, 20, 255))

    # Дорожки
    groove_start = int(r * 0.30)
    step = max(3, size // 220)
    for gr in range(groove_start, int(r * 0.97), step):
        draw.ellipse([cx-gr, cy-gr, cx+gr, cy+gr], outline=(55, 55, 72, 200), width=1)

    # Центральная метка
    lr = int(r * 0.27)
    if label_art_path and label_art_path.exists():
        art = Image.open(label_art_path).convert("RGBA").resize((lr*2, lr*2), Image.LANCZOS)
        mask = Image.new("L", (lr*2, lr*2), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, lr*2-1, lr*2-1], fill=255)
        art.putalpha(mask)
        img.paste(art, (cx-lr, cy-lr), art)
    else:
        for i in range(lr, 0, -1):
            t = 1 - i / lr
            c = (int(12+28*t), int(12+18*t), int(35+45*t), 255)
            draw.ellipse([cx-i, cy-i, cx+i, cy+i], fill=c)

    # Шпиндель
    hr = max(5, size // 110)
    draw.ellipse([cx-hr, cy-hr, cx+hr, cy+hr], fill=(6, 6, 8, 255))

    # Шин — симуляция отражённого света (верхний левый сектор)
    # Нарисован отдельным слоем, накладывается поверх пластинки
    sheen = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sheen)
    # Большой светлый эллипс смещён влево-вверх, почти за пластинкой
    sx, sy = int(cx - r * 0.25), int(cy - r * 0.25)
    sw, sh = int(r * 1.1), int(r * 0.9)
    # Рисуем несколько слоёв убывающей яркости
    for alpha, expand in [(22, 0), (16, int(r*0.08)), (10, int(r*0.16))]:
        sd.ellipse([sx-expand, sy-expand, sx+sw+expand, sy+sh+expand],
                   fill=(160, 165, 185, alpha))
    # Обрезаем по контуру пластинки (маскируем за пределами круга)
    vinyl_mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(vinyl_mask).ellipse([cx-r, cy-r, cx+r, cy+r], fill=255)
    sheen.putalpha(ImageChops_multiply(sheen.getchannel("A"), vinyl_mask))
    img = Image.alpha_composite(img, sheen)

    return img


def make_label_overlay(W: int, H: int, title: str, track_name: str) -> "Image":
    """Статичная текстовая плашка поверх видео (не вращается)."""
    from PIL import Image, ImageDraw, ImageFont
    img  = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    fs_title = max(14, W // 22)
    fs_track = max(12, W // 32)
    try:
        fnt_b = ImageFont.truetype(FONT, fs_title)
        fnt_r = ImageFont.truetype(FONT_REG, fs_track)
    except Exception:
        fnt_b = fnt_r = ImageFont.load_default()

    # Позиция: под виниловой пластинкой (vinyl_size = 0.72 от кадра)
    vinyl_size = int(min(W, H) * 0.72)
    vy_center  = H // 2
    text_y     = vy_center + vinyl_size // 2 + max(14, H // 55)

    for fnt, text, fs, dy in [
        (fnt_b, title,      fs_title, 0),
        (fnt_r, track_name, fs_track, fs_title + 10),
    ]:
        if not text:
            continue
        bb = draw.textbbox((0, 0), text, font=fnt)
        tw = bb[2] - bb[0]
        x = (W - tw) // 2
        y = text_y + dy
        if y + fs < H - 10:
            draw.text((x+2, y+2), text, font=fnt, fill=(0, 0, 0, 130))
            draw.text((x, y), text, font=fnt, fill=(210, 215, 228, 235))

    return img


def make_bg(W: int, H: int, bg_rgb: tuple) -> "Image":
    """Тёмный фон с радиальной виньеткой (numpy)."""
    import numpy as np
    from PIL import Image

    Y, X = np.ogrid[:H, :W]
    dist = np.sqrt((X - W/2)**2 + (Y - H/2)**2)
    max_d = math.sqrt((W/2)**2 + (H/2)**2)
    vig = np.clip(1.0 - 0.65 * (dist / max_d) ** 1.4, 0.2, 1.0)

    arr = np.array(bg_rgb, dtype=np.float32) * vig[..., np.newaxis]
    arr = arr.clip(0, 255).astype(np.uint8)
    return Image.fromarray(arr, "RGB")


def render_frames(
    bg: "Image", vinyl: "Image", label: "Image",
    W: int, H: int, vinyl_size: int,
    duration: float, ffmpeg_proc,
):
    """Рендерит кадры PIL и пишет RGB bytes в stdin ffmpeg."""
    from PIL import Image

    vx = (W - vinyl_size) // 2
    vy = (H - vinyl_size) // 2
    total = int(duration * FPS)
    angle_step = 360.0 * RPS / FPS  # градусы на кадр

    for i in range(total):
        angle = angle_step * i
        rotated = vinyl.rotate(-angle, resample=Image.BICUBIC, expand=False)
        frame = bg.copy().convert("RGBA")
        frame.paste(rotated, (vx, vy), rotated)
        # Статичный текст поверх
        frame = Image.alpha_composite(frame, label)
        ffmpeg_proc.stdin.write(frame.convert("RGB").tobytes())

        if i % 125 == 0:
            print(f"  frame {i}/{total} ({i*100//total}%)", flush=True)

    ffmpeg_proc.stdin.close()


def main():
    print(f"Job: {JOB_ID}")

    # 1. Download
    job_file = WORKDIR / "job.json"
    if not yd_get(f"{JOB_YD}/job.json", job_file):
        sys.exit("Failed: job.json")
    job = json.loads(job_file.read_text())

    duration    = float(job["duration"])
    fmt         = job.get("format", "square")
    out_name    = job["out_name"]
    audio_start = float(job.get("audio_start", 0))
    title       = job.get("title", "YAROMAT")
    track_name  = job.get("track_name", "")
    bg_color    = job.get("bg_color", "0x0a0a12")
    W, H = FMT_DIMS.get(fmt, FMT_DIMS["square"])
    bg_rgb = parse_color(bg_color)

    print(f"  {W}x{H} {duration}s audio_start={audio_start}s bg={bg_rgb}")

    track_file = WORKDIR / "track.mp3"
    if not yd_get(f"{JOB_YD}/track.mp3", track_file):
        sys.exit("Failed: track.mp3")

    label_art = WORKDIR / "label_art.png"
    if not yd_get(f"{JOB_YD}/label_art.png", label_art):
        label_art = None

    # 2. Generate assets
    print("\n── PIL assets ──")
    subprocess.run(["pip", "install", "-q", "Pillow", "numpy"], capture_output=True)

    vinyl_size = int(min(W, H) * 0.72)
    vinyl  = make_vinyl(vinyl_size, label_art, title, track_name)
    bg     = make_bg(W, H, bg_rgb)
    label  = make_label_overlay(W, H, title, track_name)
    vx, vy = (W - vinyl_size) // 2, (H - vinyl_size) // 2
    print(f"  vinyl {vinyl_size}px  bg {W}x{H}  vx={vx} vy={vy}")

    # 3. Render frames → ffmpeg
    result    = WORKDIR / out_name
    afade_out = max(0.0, duration - 1.5)
    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{W}x{H}", "-pix_fmt", "rgb24", "-r", str(FPS), "-i", "pipe:0",
        "-ss", str(audio_start), "-t", str(duration), "-i", str(track_file),
        "-map", "0:v", "-map", "1:a",
        "-af", f"afade=t=in:st=0:d=1,afade=t=out:st={afade_out}:d=1.5",
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        "-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p",
        "-t", str(duration), str(result),
    ]

    print(f"\n── Rendering {int(duration*FPS)} frames → ffmpeg ──")
    with subprocess.Popen(cmd, stdin=subprocess.PIPE) as proc:
        render_frames(bg, vinyl, label, W, H, vinyl_size, duration, proc)
        ret = proc.wait()

    if ret != 0 or not result.exists() or result.stat().st_size < 5000:
        yd_put_text(f"error: ffmpeg rc={ret}", f"{JOB_YD}/status.txt")
        sys.exit(f"ffmpeg failed rc={ret}")

    mb = result.stat().st_size / 1024 / 1024
    print(f"  {out_name} {mb:.1f}MB")

    # 4. Upload
    print("\n── Uploading ──")
    if not yd_put(result, f"{JOB_YD}/{out_name}"):
        yd_put_text("error: upload failed", f"{JOB_YD}/status.txt")
        sys.exit("upload failed")

    yd_put_text("done", f"{JOB_YD}/status.txt")
    print(f"\n✅ Done: {out_name} ({mb:.1f}MB)")


if __name__ == "__main__":
    main()
