#!/usr/bin/env python3
"""
vinyl_viral_job.py — GH Actions runner: viral vinyl snippet (3 bg modes).

Vinyl: CD-style — art fills entire disc, shiny plastic rim, 36% frame size.

bg_type: "blend" | "video"
  blend: clip_urls in job.json — Pexels clips downloaded & double-exposure blended
  video: bg_video.mp4 uploaded to ЯД job dir (Qwen / Pinterest+blend)

job.json: {bg_type, duration, format, out_name, audio_start, title, track_name,
           clip_urls? (list for blend)}
"""

import json, math, os, subprocess, sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

JOB_ID = os.environ.get("JOB_ID", "")
if not JOB_ID:
    sys.exit("JOB_ID not set")

REMOTE  = "ydrive"
JOBS_YD = "Content factory/cloud_io/render_jobs"
JOB_YD  = f"{JOBS_YD}/{JOB_ID}"
WORKDIR = Path("/tmp/vinyl_viral")
WORKDIR.mkdir(parents=True, exist_ok=True)

FMT_DIMS = {"square": (1080, 1080), "vertical": (1080, 1920)}
FONT_B   = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_R   = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
RPM      = 33.333
RPS      = RPM / 60.0
FPS      = 25


# ─── ЯД helpers ───────────────────────────────────────────────────────────────

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
    tmp = WORKDIR / "_status.txt"
    tmp.write_text(text)
    yd_put(tmp, remote_path)


# ─── Download helpers ─────────────────────────────────────────────────────────

def download_url(url: str, out_path: Path) -> Path:
    import requests
    out_path.parent.mkdir(parents=True, exist_ok=True)
    r = requests.get(url, stream=True, timeout=90,
                     headers={"User-Agent": "Mozilla/5.0 (compatible)"})
    r.raise_for_status()
    with open(out_path, "wb") as f:
        for chunk in r.iter_content(65536):
            f.write(chunk)
    kb = out_path.stat().st_size // 1024
    print(f"  ↓ {out_path.name} ({kb}KB)", flush=True)
    return out_path


def ffrun(cmd: list, desc: str = ""):
    if desc:
        print(f"  → {desc}", flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  STDERR: {r.stderr[-400:]}")
        sys.exit(f"ffmpeg failed: {desc}")


# ─── Clip prep (runs on GH Actions, no local ffmpeg on laptop) ───────────────

def normalize_clip(src: Path, dst: Path, W: int, H: int, duration: int = 30):
    ffrun([
        "ffmpeg", "-y", "-stream_loop", "-1", "-t", str(duration), "-i", str(src),
        "-vf", (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},setsar=1,"
                f"eq=brightness=0.02:contrast=1.05:saturation=0.72,fps={FPS}"),
        "-c:v", "libx264", "-crf", "28", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-an", str(dst),
    ], f"normalize {src.name}")


def uniquize_clip(src: Path, dst: Path):
    import random as _r
    rr  = round(_r.uniform(0.84, 0.90), 3)
    gg  = round(_r.uniform(0.88, 0.93), 3)
    bb  = round(_r.uniform(1.07, 1.13), 3)
    sat = round(_r.uniform(0.75, 0.85), 3)
    ns  = _r.randint(7, 12)
    vf  = (f"colorchannelmixer=rr={rr}:gg={gg}:bb={bb},"
           f"eq=saturation={sat}:contrast=1.06:brightness=0.04,"
           f"noise=alls={ns}:allf=t+u,unsharp=3:3:0.4:3:3:0.0,"
           f"vignette=PI*0.24")
    ffrun([
        "ffmpeg", "-y", "-i", str(src), "-vf", vf,
        "-c:v", "libx264", "-crf", "26", "-preset", "fast",
        "-pix_fmt", "yuv420p", "-an", str(dst),
    ], f"uniquize {src.name}")


def loop_to_duration(src: Path, dst: Path, duration: float):
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(src)], capture_output=True, text=True)
    src_dur = float(probe.stdout.strip() or "5")
    repeat = math.ceil((duration + 2) / max(src_dur, 1)) + 1
    concat_f = WORKDIR / "_concat_loop.txt"
    concat_f.write_text("\n".join(f"file '{src}'" for _ in range(repeat)))
    ffrun([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_f),
        "-t", str(duration + 2),
        "-c:v", "libx264", "-crf", "25", "-preset", "fast",
        "-pix_fmt", "yuv420p", "-an", str(dst),
    ], "loop to duration")


def stretch_to_duration(src: Path, dst: Path, target: float):
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(src)], capture_output=True, text=True)
    src_dur = float(probe.stdout.strip() or "5")
    if src_dur >= target * 0.9:
        loop_to_duration(src, dst, target)
        return
    factor = round(target / src_dur, 3)
    print(f"  stretch ×{factor:.2f} ({src_dur:.1f}s → {target:.0f}s)")
    ffrun([
        "ffmpeg", "-y", "-i", str(src), "-vf", f"setpts={factor}*PTS",
        "-c:v", "libx264", "-crf", "25", "-preset", "fast",
        "-pix_fmt", "yuv420p", "-an", str(dst),
    ], "stretch slow-motion")


def prepare_overlay_segments(src: Path, dst: Path, W: int, H: int,
                              seg_duration: float, total_duration: float):
    """Normalize src then cut into BPM-aligned segments and concat."""
    import random as _r
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(src)], capture_output=True, text=True)
    src_dur = float(probe.stdout.strip() or "30")

    n_segs = math.ceil((total_duration + 2) / max(seg_duration, 0.1))
    seg_dir = WORKDIR / "overlay_segs"; seg_dir.mkdir(exist_ok=True)

    seg_files = []
    for i in range(n_segs):
        max_start = max(0.0, src_dur - seg_duration - 0.2)
        start = _r.uniform(0, max_start) if max_start > 0 else 0
        seg_out = seg_dir / f"seg_{i:03d}.mp4"
        ffrun([
            "ffmpeg", "-y", "-ss", f"{start:.3f}", "-t", f"{seg_duration + 0.05:.3f}",
            "-i", str(src),
            "-vf", (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                    f"crop={W}:{H},setsar=1,fps={FPS}"),
            "-c:v", "libx264", "-crf", "28", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-an", str(seg_out),
        ], f"overlay seg {i+1}/{n_segs}")
        seg_files.append(seg_out)

    concat_f = seg_dir / "concat.txt"
    concat_f.write_text("\n".join(f"file '{f}'" for f in seg_files))
    ffrun([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_f),
        "-t", f"{total_duration + 2:.1f}",
        "-c:v", "libx264", "-crf", "26", "-preset", "fast",
        "-pix_fmt", "yuv420p", "-an", str(dst),
    ], "concat overlay segments")


# ─── Blend background (Pexels clips) ─────────────────────────────────────────

def prepare_blend_bg(clip_paths: list, W: int, H: int, duration: float, tmp: Path) -> Path:
    SEG = 8
    norm_dir = tmp / "norm"; norm_dir.mkdir(exist_ok=True)
    norm = []
    for i, src in enumerate(clip_paths):
        dst = norm_dir / f"n{i:02d}.mp4"
        ffrun([
            "ffmpeg", "-y", "-stream_loop", "-1", "-t", str(SEG), "-i", str(src),
            "-vf", (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                    f"crop={W}:{H},setsar=1,"
                    f"eq=brightness=0.02:contrast=1.04:saturation=0.68,fps={FPS}"),
            "-c:v", "libx264", "-crf", "28", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-an", str(dst),
        ], f"norm {i+1}/{len(clip_paths)}")
        norm.append(dst)

    blend_dir = tmp / "blend"; blend_dir.mkdir(exist_ok=True)
    blended = []
    for i in range(len(norm) - 1):
        dst = blend_dir / f"b{i:02d}.mp4"
        ffrun([
            "ffmpeg", "-y", "-i", str(norm[i]), "-i", str(norm[(i+1) % len(norm)]),
            "-filter_complex",
            "[0:v]unsharp=5:5:1.8[base];[1:v]eq=brightness=-0.12:contrast=0.85[top];"
            "[base][top]blend=all_mode=normal:all_opacity=0.42[v]",
            "-map", "[v]", "-c:v", "libx264", "-crf", "28", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-an", str(dst),
        ], f"blend {i+1}/{len(norm)-1}")
        blended.append(dst)

    if not blended:
        blended = norm

    needed = duration + 3
    repeat  = math.ceil(needed / (SEG * len(blended))) + 1
    concat_f = tmp / "concat.txt"
    concat_f.write_text("\n".join(f"file '{f}'" for f in blended * repeat))

    bg_path = tmp / "blend_bg.mp4"
    ffrun([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_f),
        "-t", str(needed + 1),
        "-vf", "colorchannelmixer=rr=0.84:gg=0.90:bb=1.10",
        "-c:v", "libx264", "-crf", "26", "-preset", "fast",
        "-pix_fmt", "yuv420p", "-an", str(bg_path),
    ], "concat → blend_bg")
    return bg_path


def iter_video_frames(path: Path, W: int, H: int):
    """Infinite looping generator of RGB PIL frames."""
    while True:
        cmd = [
            "ffmpeg", "-v", "quiet", "-i", str(path),
            "-vf", (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                    f"crop={W}:{H},fps={FPS}"),
            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        frame_size = W * H * 3
        got = False
        while True:
            data = proc.stdout.read(frame_size)
            if len(data) < frame_size:
                break
            got = True
            yield Image.frombytes("RGB", (W, H), data)
        proc.stdout.close()
        proc.wait()
        if not got:
            break


# ─── CD disc assets ───────────────────────────────────────────────────────────

def make_cd_base(size: int, label_art_path) -> Image.Image:
    """CD-style disc: art fills entire circle, no grooves, clean center hole."""
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx = cy = size // 2
    r  = size // 2 - 4   # disc outer radius (4px inset for rim)
    rim_w = 4             # rim width in px
    lr = r - rim_w        # label/art fills inside the rim

    # Rim base (dark plastic edge, highlight added in sheen)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(22, 22, 30, 255))

    # Label art fills entire inner disc
    if label_art_path and Path(label_art_path).exists():
        art = Image.open(label_art_path).convert("RGBA").resize((lr * 2, lr * 2), Image.LANCZOS)
        mask = Image.new("L", (lr * 2, lr * 2), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, lr * 2 - 1, lr * 2 - 1], fill=255)
        art.putalpha(mask)
        img.paste(art, (cx - lr, cy - lr), art)
    else:
        # Gradient disc if no art
        for i in range(lr, 0, -1):
            t = 1 - i / lr
            draw.ellipse([cx - i, cy - i, cx + i, cy + i],
                         fill=(int(10 + 25 * t), int(10 + 15 * t), int(25 + 45 * t), 255))

    # Center hole (CD proportions: ~14% of radius)
    hr = max(6, int(r * 0.14))
    draw.ellipse([cx - hr, cy - hr, cx + hr, cy + hr], fill=(0, 0, 0, 0))

    return img


def make_cd_sheen(size: int) -> Image.Image:
    """Static plastic specular overlay: iridescent rim + bright blob.
    Does NOT rotate — fixed light source from upper-left."""
    cx = cy = size // 2
    r  = size // 2 - 4
    rim_w = 5
    hr = max(6, int(r * 0.14))  # hole radius

    Y, X = np.ogrid[:size, :size]
    dist  = np.sqrt(((X - cx) ** 2 + (Y - cy) ** 2).astype(np.float32))
    angle = np.arctan2((Y - cy).astype(np.float32), (X - cx).astype(np.float32))

    in_disc = (dist <= r) & (dist > hr)
    rim = (dist >= r - rim_w) & (dist <= r) & in_disc
    in_hole = dist <= hr

    # Iridescent rim: different hue channels peak at different angles
    # Creates CD-like plastic shimmer
    rf = (np.cos(angle - math.radians(30))  + 1) / 2   # R peaks upper-right
    gf = (np.cos(angle - math.radians(170)) + 1) / 2   # G peaks left
    bf = (np.cos(angle - math.radians(260)) + 1) / 2   # B peaks upper-left
    # Overall brightness: brightest at upper-left (225°)
    bright = (np.cos(angle - math.radians(225)) + 1) / 2
    rim_a  = (bright * 200 + 55).astype(np.uint8)

    # Soft specular blob upper-left (plastic is more reflective than vinyl)
    bx = cx - int(r * 0.20)
    by = cy - int(r * 0.30)
    bd = np.sqrt(((X - bx) ** 2 + (Y - by) ** 2).astype(np.float32))
    br = r * 0.58
    blob_a = np.clip((1 - bd / br) ** 2.2 * 72, 0, 72).astype(np.float32) * in_disc

    rgba = np.zeros((size, size, 4), dtype=np.uint8)

    # Blob (bright cool white)
    m = blob_a > 2
    rgba[m, 0] = np.clip(180 + blob_a[m] * 0.5, 0, 228).astype(np.uint8)
    rgba[m, 1] = np.clip(190 + blob_a[m] * 0.5, 0, 232).astype(np.uint8)
    rgba[m, 2] = np.clip(212 + blob_a[m] * 0.4, 0, 240).astype(np.uint8)
    rgba[m, 3] = blob_a[m].astype(np.uint8)

    # Iridescent rim
    rgba[rim, 0] = np.clip(rf[rim] * 120 + 135, 0, 255).astype(np.uint8)
    rgba[rim, 1] = np.clip(gf[rim] * 100 + 145, 0, 255).astype(np.uint8)
    rgba[rim, 2] = np.clip(bf[rim] * 120 + 155, 0, 255).astype(np.uint8)
    rgba[rim, 3] = rim_a[rim]

    # Punch hole transparent
    rgba[in_hole] = 0

    return Image.fromarray(rgba, "RGBA")


# ─── Text layer ────────────────────────────────────────────────────────────────

def make_text_base(W: int, H: int, title: str, track_name: str,
                   vinyl_size: int) -> Image.Image:
    img  = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    fs_t = max(14, W // 22)
    fs_r = max(12, W // 32)
    try:
        fb = ImageFont.truetype(FONT_B, fs_t)
        fr = ImageFont.truetype(FONT_R, fs_r)
    except Exception:
        fb = fr = ImageFont.load_default()

    text_y = H // 2 + vinyl_size // 2 + max(14, H // 55)
    for fnt, text, dy in [(fb, title, 0), (fr, track_name, fs_t + 8)]:
        if not text:
            continue
        bb = draw.textbbox((0, 0), text, font=fnt)
        tw = bb[2] - bb[0]
        x  = (W - tw) // 2
        y  = text_y + dy
        if y + max(fs_t, fs_r) < H - 8:
            draw.text((x + 2, y + 2), text, font=fnt, fill=(0, 0, 0, 130))
            draw.text((x, y),         text, font=fnt, fill=(210, 215, 228, 235))
    return img


def get_text_frame(text_base: Image.Image, frame_idx: int,
                   anim_frames: int = 38) -> Image.Image:
    t = min(frame_idx / anim_frames, 1.0)
    t = 1 - (1 - t) ** 2
    if t >= 1.0:
        return text_base
    y_shift = int(18 * (1 - t))
    layer = Image.new("RGBA", text_base.size, (0, 0, 0, 0))
    if t > 0.02:
        tc = text_base.copy()
        tc.putalpha(tc.getchannel("A").point(lambda p: int(p * t)))
        layer.paste(tc, (0, y_shift), tc)
    return layer


def make_vignette(W: int, H: int) -> Image.Image:
    Y, X = np.ogrid[:H, :W]
    dx = (X - W / 2) / (W / 2)
    dy = (Y - H / 2) / (H / 2)
    d  = np.sqrt(dx ** 2 + dy ** 2)
    a  = np.clip((d - 0.55) / 0.55, 0, 1) ** 1.7 * 0.72 * 255
    rgba = np.zeros((H, W, 4), dtype=np.uint8)
    rgba[:, :, 3] = a.astype(np.uint8)
    return Image.fromarray(rgba, "RGBA")


# ─── Render loop ──────────────────────────────────────────────────────────────

def render(bg_gen, cd_disc: Image.Image, sheen: Image.Image,
           text_base: Image.Image, vignette: Image.Image,
           W: int, H: int, vinyl_size: int,
           duration: float, track_path: Path, audio_start: float,
           out_path: Path,
           overlay_gen=None, overlay_opacity: float = 0.4) -> int:

    total      = int(duration * FPS)
    angle_step = 360.0 * RPS / FPS
    vx = (W - vinyl_size) // 2
    vy = (H - vinyl_size) // 2

    FADE_IN  = int(0.5 * FPS)
    FADE_OUT = int(1.5 * FPS)
    BLACK    = Image.new("RGB", (W, H), 0)

    afade_out = max(0.0, duration - 1.5)
    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{W}x{H}", "-pix_fmt", "rgb24", "-r", str(FPS), "-i", "pipe:0",
        "-ss", str(audio_start), "-t", str(duration), "-i", str(track_path),
        "-map", "0:v", "-map", "1:a",
        "-af", f"afade=t=in:st=0:d=0.5,afade=t=out:st={afade_out}:d=1.5",
        "-vf", "noise=alls=9:allf=u",
        "-c:v", "libx264", "-crf", "22", "-preset", "fast",
        "-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p",
        "-t", str(duration), str(out_path),
    ]

    with subprocess.Popen(cmd, stdin=subprocess.PIPE) as proc:
        for i in range(total):
            bg_frame = next(bg_gen)
            if overlay_gen:
                ov_frame = next(overlay_gen)
                frame = Image.blend(bg_frame, ov_frame, overlay_opacity).convert("RGBA")
            else:
                frame = bg_frame.convert("RGBA")

            # CD rotating
            angle   = angle_step * i
            rotated = cd_disc.rotate(-angle, resample=Image.BICUBIC, expand=False)
            frame.paste(rotated, (vx, vy), rotated)

            # Static plastic sheen
            frame.paste(sheen, (vx, vy), sheen)

            # Vignette
            frame = Image.alpha_composite(frame, vignette)

            # Text (animated)
            frame = Image.alpha_composite(frame, get_text_frame(text_base, i))

            # Fade
            rgb = frame.convert("RGB")
            if i < FADE_IN:
                rgb = Image.blend(BLACK, rgb, i / FADE_IN)
            elif i >= total - FADE_OUT:
                f = (total - i) / FADE_OUT
                rgb = Image.blend(BLACK, rgb, max(0.0, f))

            proc.stdin.write(rgb.tobytes())
            if i % 100 == 0:
                print(f"  frame {i}/{total} ({i * 100 // total}%)", flush=True)

        proc.stdin.close()
        return proc.wait()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"Job: {JOB_ID}", flush=True)

    job_file = WORKDIR / "job.json"
    if not yd_get(f"{JOB_YD}/job.json", job_file):
        sys.exit("Failed: job.json")
    job = json.loads(job_file.read_text())

    duration    = float(job["duration"])
    fmt         = job.get("format", "square")
    out_name    = job["out_name"]
    audio_start = float(job.get("audio_start", 0))
    title       = job.get("title", "yaromat")
    track_name  = job.get("track_name", "")
    bg_type     = job.get("bg_type", "blend")
    W, H = FMT_DIMS.get(fmt, FMT_DIMS["square"])

    print(f"  {W}×{H}  {duration}s  bg={bg_type}  audio_start={audio_start}s")

    track_file = WORKDIR / "track.mp3"
    if not yd_get(f"{JOB_YD}/track.mp3", track_file):
        sys.exit("Failed: track.mp3")

    subprocess.run(["pip", "install", "-q", "Pillow", "numpy", "requests"],
                   capture_output=True)

    # ── Label art (center of CD) ────────────────────────────────────────────
    label_art_file = WORKDIR / "label_art.png"
    yd_get(f"{JOB_YD}/label_art.png", label_art_file)
    label_art = label_art_file if label_art_file.exists() else None

    # ── Background source → generator ───────────────────────────────────────
    overlay_gen     = None
    overlay_opacity = float(job.get("overlay_opacity", 0.4))

    if bg_type == "blend":
        clip_urls = job.get("clip_urls", [])
        if not clip_urls:
            sys.exit("bg_type=blend but clip_urls missing")
        clips_dir = WORKDIR / "clips"; clips_dir.mkdir(exist_ok=True)
        print(f"  Downloading {len(clip_urls)} clips...")
        clip_paths = []
        for idx, url in enumerate(clip_urls):
            try:
                clip_paths.append(download_url(url, clips_dir / f"clip_{idx:02d}.mp4"))
            except Exception as e:
                print(f"  ! clip {idx} skip: {e}")
        if not clip_paths:
            sys.exit("No clips downloaded")
        blend_tmp = WORKDIR / "blend_tmp"; blend_tmp.mkdir(exist_ok=True)
        bg_video  = prepare_blend_bg(clip_paths, W, H, duration, blend_tmp)
        bg_gen    = iter_video_frames(bg_video, W, H)

    elif bg_type == "video":
        bg_vid_raw = WORKDIR / "bg_video_raw.mp4"
        if not yd_get(f"{JOB_YD}/bg_video.mp4", bg_vid_raw):
            sys.exit("Failed: bg_video.mp4")
        mb = bg_vid_raw.stat().st_size / 1_048_576
        print(f"  bg_video.mp4 {mb:.1f}MB")
        stretch_target = float(job.get("stretch_target", 0))
        if stretch_target > 0:
            bg_vid = WORKDIR / "bg_video.mp4"
            stretch_to_duration(bg_vid_raw, bg_vid, stretch_target)
        else:
            bg_vid = bg_vid_raw
        bg_gen = iter_video_frames(bg_vid, W, H)

    elif bg_type == "overlay":
        # Download raw clips
        clip_a_raw = WORKDIR / "clip_a.mp4"
        clip_b_raw = WORKDIR / "clip_b.mp4"
        if not yd_get(f"{JOB_YD}/clip_a.mp4", clip_a_raw):
            sys.exit("Failed: clip_a.mp4")
        if not yd_get(f"{JOB_YD}/clip_b.mp4", clip_b_raw):
            sys.exit("Failed: clip_b.mp4")
        mb_a = clip_a_raw.stat().st_size / 1_048_576
        mb_b = clip_b_raw.stat().st_size / 1_048_576
        seg_duration = float(job.get("overlay_seg_duration", 2.0))
        print(f"  clip_a={mb_a:.1f}MB  clip_b={mb_b:.1f}MB  opacity={overlay_opacity}  seg={seg_duration:.2f}s")

        # All prep runs on GH Actions
        print("\n── Prep base (normalize → uniquize → loop) ──")
        norm_a  = WORKDIR / "norm_a.mp4"
        uniq_a  = WORKDIR / "uniq_a.mp4"
        base_v  = WORKDIR / "base_video.mp4"
        normalize_clip(clip_a_raw, norm_a, W, H, duration=35)
        uniquize_clip(norm_a, uniq_a)
        loop_to_duration(uniq_a, base_v, duration + 3)

        print("\n── Prep overlay (normalize → BPM-segment) ──")
        ov_v = WORKDIR / "overlay_video.mp4"
        prepare_overlay_segments(clip_b_raw, ov_v, W, H, seg_duration, duration + 3)

        bg_gen      = iter_video_frames(base_v, W, H)
        overlay_gen = iter_video_frames(ov_v, W, H)

    else:
        sys.exit(f"Unknown bg_type: {bg_type}")

    # ── CD assets ────────────────────────────────────────────────────────────
    print("\n── CD assets ──")
    vinyl_size = int(min(W, H) * 0.36)
    cd_disc    = make_cd_base(vinyl_size, label_art)
    sheen      = make_cd_sheen(vinyl_size)
    text_base  = make_text_base(W, H, title, track_name, vinyl_size)
    vignette   = make_vignette(W, H)
    print(f"  cd_disc={vinyl_size}px  sheen ready  vignette ready")

    # ── Render ───────────────────────────────────────────────────────────────
    result = WORKDIR / out_name
    print(f"\n── Rendering {int(duration * FPS)} frames → ffmpeg ──")
    ret = render(bg_gen, cd_disc, sheen, text_base, vignette,
                 W, H, vinyl_size, duration, track_file, audio_start, result,
                 overlay_gen=overlay_gen, overlay_opacity=overlay_opacity)

    if ret != 0 or not result.exists() or result.stat().st_size < 10_000:
        yd_put_text(f"error: ffmpeg rc={ret}", f"{JOB_YD}/status.txt")
        sys.exit(f"ffmpeg failed rc={ret}")

    mb = result.stat().st_size / 1_048_576
    print(f"  {out_name}  {mb:.1f}MB")

    # ── Upload ───────────────────────────────────────────────────────────────
    print("\n── Uploading ──")
    if not yd_put(result, f"{JOB_YD}/{out_name}"):
        yd_put_text("error: upload failed", f"{JOB_YD}/status.txt")
        sys.exit("upload failed")

    yd_put_text("done", f"{JOB_YD}/status.txt")
    print(f"\n✅ Done: {out_name}  ({mb:.1f}MB)")


if __name__ == "__main__":
    main()
