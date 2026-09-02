#!/usr/bin/env python3
"""
Batch uniqueizer for Pinterest board raw videos on GitHub Actions.

Downloads a raw YaD folder, runs the same FFmpeg uniqueization recipe over every
MP4, and uploads the result into a sibling uniq/ folder.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

EFFECTS_JSON = Path(__file__).parent / "effects.json"

def sh(cmd: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def load_effects() -> dict:
    """Загружает effeкты из JSON."""
    return json.loads(EFFECTS_JSON.read_text(encoding="utf-8"))


def probe_duration(path: Path) -> float:
    r = sh([
        "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
        "-of", "csv=p=0", str(path)
    ], timeout=60)
    try:
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def probe_fps(path: Path) -> float:
    r = sh([
        "ffprobe", "-v", "quiet", "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate",
        "-of", "csv=p=0", str(path)
    ], timeout=60)
    try:
        num, den = r.stdout.strip().split("/")
        fps = float(num) / float(den)
        return fps if fps > 0 else 24.0
    except Exception:
        return 24.0


def pick_chain(effects_db: dict, n: int | None = None) -> list[str]:
    """Choose exactly one visible effect for one source video.

    ``bleach_negate`` remains available for manual diagnostics, but is excluded
    from production randomization because a full-frame negative washes objects out.
    """
    del n  # The production contract is one effect, never an effect chain.
    names = [*effects_db["vf"], *effects_db["complex"]]
    production = [name for name in names if name != "bleach_negate"]
    if not production:
        raise ValueError("effects database has no production effects")
    return [random.choice(production)]


def gen_parallax_filter() -> str:
    """Параллакс: медленный дрейф oversized кадра."""
    scale = round(random.uniform(1.04, 1.10), 2)
    sw = int(1280 * scale)
    sh = int(720 * scale)
    dx = sw - 1280
    dy = sh - 720
    direction = random.choice(["h", "v", "diag"])
    speed = round(random.uniform(0.12, 0.28), 3)
    if direction == "h":
        x_expr = f"'{dx * 0.1}+{dx * 0.8}*sin(t*{speed})'"
        y_expr = f"'{dy / 2}'"
    elif direction == "v":
        x_expr = f"'{dx / 2}'"
        y_expr = f"'{dy * 0.1}+{dy * 0.8}*cos(t*{speed * 0.7})'"
    else:
        x_expr = f"'{dx * 0.1}+{dx * 0.8}*sin(t*{speed})'"
        y_expr = f"'{dy * 0.1}+{dy * 0.8}*cos(t*{speed * 0.85})'"
    return f"scale={sw}:{sh},crop=1280:720:{x_expr}:{y_expr}"


def gen_slide_crop_filter() -> str:
    """Неглубокий горизонтальный дрейф, zoom не больше 1.10x."""
    return "scale=1408:792,crop=1280:720:'64+48*sin(t*0.25)':'36'"


def gen_corner_sweep_filter() -> str:
    """Неглубокая дуга внутри 1.10x crop."""
    return "scale=1408:792,crop=1280:720:x='64+48*sin(t*0.8)':y='36+28*cos(t*0.8)'"


def gen_zoom_drift_filter() -> str:
    """Неглубокий zoom 1.10x + синусоидальный дрейф crop'а."""
    return "scale=1408:792,crop=1280:720:x='64+48*sin(t*0.3)':y='36+28*cos(t*0.25)'"


def gen_diagonal_crop_filter() -> str:
    """Неглубокий диагональный дрейф внутри 1.10x crop."""
    return "scale=1408:792,crop=1280:720:'128*t/24':'72*t/24'"


def gen_split_drift_complex() -> str:
    """кадр делится на 2 половины, разъезжаются."""
    return (
        "[0:v]split[bg][work];"
        "[bg]scale=1280:720,boxblur=18:3,eq=brightness=-0.08[bgd];"
        "[work]scale=1280:720,split[top][bot];"
        "[top]crop=1280:360:0:0[topc];"
        "[bot]crop=1280:360:0:360[botc];"
        "[bgd][topc]overlay=x=0:y='0-120*(t/6)':shortest=1[o1];"
        "[o1][botc]overlay=x=0:y='360+120*(t/6)':shortest=1"
    )


def gen_grid_2x2_complex() -> str:
    """4 версии кадра в сетке, внутренний дрейф."""
    return (
        "[0:v]scale=640:360,split=4[a][b][c][d];"
        "[a]crop=640:360:x='30*sin(t*1.5)':y='20*cos(t*1.2)'[s1];"
        "[b]crop=640:360:x='30*cos(t*1.3)':y='20*sin(t*1.7)'[s2];"
        "[c]crop=640:360:x='25*sin(t*1.8+1)':y='25*cos(t*1.1+2)'[s3];"
        "[d]crop=640:360:x='25*cos(t*1.4+3)':y='25*sin(t*1.6+1)'[s4];"
        "[s1][s2]hstack[top];[s3][s4]hstack[bot];[top][bot]vstack,scale=1280:720"
    )


def gen_split_converge_complex() -> str:
    """2 горизонтальные полосы сходятся к центру."""
    return (
        "[0:v]scale=1280:720,split=2[a][b];"
        "[a]crop=1280:360:0:0[tc];[b]crop=1280:360:0:360[bc];"
        "color=c=black:s=1280x720:d=7[bg];"
        "[bg][tc]overlay=x=0:y='(0-100+80*(t/7))':shortest=1[o1];"
        "[o1][bc]overlay=x=0:y='(360+100-80*(t/7))':shortest=1"
    )


VF_GENERATORS = {
    "parallax": gen_parallax_filter,
    "slide_crop": gen_slide_crop_filter,
    "corner_sweep": gen_corner_sweep_filter,
    "zoom_drift": gen_zoom_drift_filter,
    "diagonal_crop": gen_diagonal_crop_filter,
}

COMPLEX_GENERATORS = {
    "strobo": None,  # используется strobo_graph
    "flash": None,   # используется strobo_graph
    "split_drift": gen_split_drift_complex,
    "grid_2x2": gen_grid_2x2_complex,
    "split_converge": gen_split_converge_complex,
}


def resolve_effect_filter(name: str, effects_db: dict, fps: float, duration: float) -> tuple[str, str]:
    """Возвращает (type, filter_string) для эффекта. type = 'vf' | 'complex'."""
    if name in effects_db.get("vf", {}):
        eff = effects_db["vf"][name]
        if "_generator" in eff:
            return "vf", VF_GENERATORS[name]()
        if "complex" in eff:
            return "complex", eff["complex"]
        return "vf", eff["filter"]
    if name in effects_db.get("complex", {}):
        eff = effects_db["complex"][name]
        if name in ("strobo", "flash"):
            fp_range = eff["flash_pct_range"]
            fp = round(random.uniform(fp_range[0], fp_range[1]), 2)
            graph, _ = strobo_graph(duration, fps, flash_pct=fp)
            return "complex", graph
        if "_generator" in eff:
            return "complex", COMPLEX_GENERATORS[name]()
        if "complex" in eff:
            return "complex", eff["complex"]
        return "complex", eff.get("filter", "")
    raise ValueError(f"unknown effect: {name}")


def color_chain(kind: str, fps: float) -> str:
    """Цветовой вариант поверх базового рецепта. "" = без эффекта."""
    if kind == "invert":
        return "negate"
    if kind == "bright":
        hue = random.randint(0, 359)
        sat = round(random.uniform(1.7, 2.6), 2)
        con = round(random.uniform(1.06, 1.18), 3)
        bri = round(random.uniform(0.02, 0.05), 3)
        return f"hue=h={hue}:s={sat},eq=contrast={con}:brightness={bri}"
    return ""


def strobo_graph(duration: float, fps: float, in_label: str = "0:v",
                 out_label: str = "vs", flash_pct: float | None = None) -> tuple[str, int]:
    """Стробо/вспышка: чередование яркой и инвертированной фазы.
    flash_pct — доля инверсной фазы в периоде (None = рандом 0.20–0.50)."""
    freq = round(random.uniform(1.6, 4.0), 2)
    period = max(4, int(round(fps / freq)))
    if flash_pct is None:
        flash_pct = round(random.uniform(0.20, 0.50), 2)
    flash_len = max(1, int(round(period * flash_pct)))
    bright_len = period - flash_len
    total_frames = max(period * 2, int(math.ceil(duration * fps)))
    boundaries = []
    pos = 0
    while pos < total_frames:
        bright_end = min(pos + bright_len, total_frames)
        boundaries.append((pos, bright_end, False))
        if bright_end < total_frames:
            flash_end = min(bright_end + flash_len, total_frames)
            boundaries.append((bright_end, flash_end, True))
            pos = flash_end
        else:
            pos = total_frames
    segs = len(boundaries)
    head = f"[{in_label}]split={segs}" + "".join(f"[g{i}]" for i in range(segs))
    parts = [head]
    labels = []
    for i, (start, end, is_flash) in enumerate(boundaries):
        if is_flash:
            # Keep object contours readable: a 24% negative blend, not a white full-frame negate.
            parts.append(
                f"[g{i}]trim=start_frame={start}:end_frame={end},setpts=PTS-STARTPTS[seg{i}];"
                f"[seg{i}]split[orig{i}][negsrc{i}];"
                f"[negsrc{i}]negate,eq=brightness=-0.04[neg{i}];"
                f"[orig{i}][neg{i}]blend=all_mode=normal:all_opacity=0.24[v{i}]"
            )
        else:
            parts.append(f"[g{i}]trim=start_frame={start}:end_frame={end},setpts=PTS-STARTPTS[v{i}]")
        labels.append(f"[v{i}]")
    parts.append("".join(labels) + f"concat=n={segs}:v=1:a=0[{out_label}]")
    return ";".join(parts), segs


def uniquize(src: Path, dst: Path, *, color: str = "", fps: float = 24.0,
             effects_chain: list[str] | None = None, effects_db: dict | None = None,
             base: bool = True) -> None:
    speed = round(random.uniform(0.97, 1.03), 3) if base else 1.0
    pts_factor = round(1.0 / speed, 4)
    flip = random.choice(["hflip,", ""])
    # Base crop plus the later shake crop must remain within the 1.10x zoom ceiling.
    crop_pct = round(random.uniform(0.95, 0.97), 3)
    margin = round((1.0 - crop_pct) / 2, 4)
    crop = f"crop=iw*{crop_pct}:ih*{crop_pct}:iw*{margin}:ih*{margin},"
    rr = round(random.uniform(0.84, 0.90), 3)
    gg = round(random.uniform(0.89, 0.93), 3)
    bb = round(random.uniform(1.07, 1.12), 3)
    color_mix = f"colorchannelmixer=rr={rr}:gg={gg}:bb={bb},"
    sat = round(random.uniform(0.75, 0.85), 3)
    con = round(random.uniform(1.04, 1.09), 3)
    bri = round(random.uniform(0.02, 0.06), 3)
    eq = f"eq=saturation={sat}:contrast={con}:brightness={bri},"
    noise_str = random.randint(6, 11)
    noise = f"noise=alls={noise_str}:allf=t+u,"
    unsharp = "unsharp=3:3:0.4:3:3:0.0,"
    shake_amp_x = random.randint(10, 18)
    shake_amp_y = random.randint(6, 12)
    margin_x = shake_amp_x + 6
    margin_y = shake_amp_y + 4
    crop_w = 1280 - 2 * margin_x
    crop_h = 720 - 2 * margin_y
    base_freq = random.uniform(10.0, 13.5)
    freq_x = round(base_freq, 1)
    freq_y = round(base_freq * random.uniform(0.7, 0.85), 1)
    shake = (
        f"crop={crop_w}:{crop_h}:"
        f"'{margin_x}+{shake_amp_x}*sin(t*{freq_x})':"
        f"'{margin_y}+{shake_amp_y}*cos(t*{freq_y})',"
        f"scale=1280:720,"
    )
    vignette = f"vignette=PI*{round(random.uniform(0.22, 0.30), 2)}"

    # Базовый vf-цепочка (пропускается в режиме single-effect: 1 инструмент на луп)
    color_kind = color
    color_f = color_chain(color, fps) if base else ""
    base_chain = []
    if base:
        base_chain = [
            flip, crop, "scale=1280:720",
            f"setpts={pts_factor}*PTS",
            shake, color_mix, eq, noise, unsharp, vignette,
        ]
        if color_f:
            base_chain.append(color_f)

    vf_parts = [c.rstrip(",") for c in base_chain if c]

    # Дополнительные vf-эффекты из цепочки
    complex_parts = []
    if effects_chain and effects_db:
        dur = probe_duration(src)
        for eff_name in effects_chain:
            eff_type, eff_filter = resolve_effect_filter(eff_name, effects_db, fps, dur)
            if eff_type == "vf":
                vf_parts.append(eff_filter)
            else:
                complex_parts.append(eff_filter)

    vf = ",".join(vf_parts)
    pre_graph = f"[0:v]{vf}[pre]" if vf else "[0:v]null[pre]"

    probe = sh([
        "ffprobe", "-v", "quiet", "-select_streams", "a",
        "-show_entries", "stream=index", "-of", "csv=p=0", str(src)
    ], timeout=30)
    has_audio = bool(probe.stdout.strip())

    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]

    # Если есть complex-эффекты — собираем filter_complex
    if complex_parts:
        # Один complex-эффект ( strobo/flash/split_drift/... )
        complex_graph = complex_parts[0]
        strobo_effect = bool(effects_chain and effects_chain[0] in ("strobo", "flash"))
        if strobo_effect:
            # strobo/flash emit [vs] themselves; do not append a second output label.
            if has_audio:
                cmd += [
                    "-filter_complex", f"{pre_graph};{complex_graph.replace('[0:v]', '[pre]')};[0:a]atempo={speed}[a]",
                    "-map", "[vs]" if "[vs]" in complex_graph else "[v]",
                    "-map", "[a]",
                    "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "160k",
                ]
            else:
                cmd += [
                    "-filter_complex", f"{pre_graph};{complex_graph.replace('[0:v]', '[pre]')}",
                    "-map", "[vs]" if "[vs]" in complex_graph else "[v]", "-an",
                ]
        else:
            # Другой complex-эффект (split_drift, grid_2x2, motion_pan, negative_echo, self_blend_reverse)
            fixed_graph = complex_graph.replace("[0:v]", "[pre]")
            if has_audio:
                cmd += [
                    "-filter_complex", f"{pre_graph};{fixed_graph}[v];[0:a]atempo={speed}[a]",
                    "-map", "[v]", "-map", "[a]",
                    "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "160k",
                ]
            else:
                cmd += [
                    "-filter_complex", f"{pre_graph};{fixed_graph}[v]",
                    "-map", "[v]", "-an",
                ]
    elif color_kind in ("strobo", "flash"):
        fp = round(random.uniform(0.35, 0.50), 2) if color_kind == "strobo" else round(random.uniform(0.18, 0.28), 2)
        graph, segs = strobo_graph(probe_duration(src), fps, in_label="pre", flash_pct=fp)
        graph = f"{pre_graph};{graph}"
        if has_audio:
            cmd += [
                "-filter_complex", f"{graph};[0:a]atempo={speed}[a]",
                "-map", "[vs]", "-map", "[a]",
                "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "160k",
            ]
        else:
            cmd += ["-filter_complex", graph, "-map", "[vs]", "-an"]
    elif has_audio:
        cmd += [
            "-filter_complex",
            f"[0:v]{vf}[v];[0:a]atempo={speed}[a]",
            "-map", "[v]", "-map", "[a]",
            "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "160k",
        ]
    else:
        cmd += ["-vf", vf, "-an"]
    cmd += [
        "-threads", "2",
        "-c:v", "libx264", "-profile:v", "baseline", "-level:v", "3.1",
        "-pix_fmt", "yuv420p", "-preset", "fast", "-crf", "23",
        "-movflags", "+faststart",
        str(dst),
    ]

    r = sh(cmd, timeout=900)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-600:])


def parallax_filter(duration: float) -> str:
    """Параллакс: медленный дрейф oversized кадра, создаёт иллюзию глубины."""
    scale = round(random.uniform(1.04, 1.10), 2)
    sw = int(1280 * scale)
    sh = int(720 * scale)
    dx = sw - 1280
    dy = sh - 720
    direction = random.choice(["h", "v", "diag"])
    speed = round(random.uniform(0.12, 0.28), 3)
    if direction == "h":
        x_expr = f"'{dx * 0.1}+{dx * 0.8}*sin(t*{speed})'"
        y_expr = f"'{dy / 2}'"
    elif direction == "v":
        x_expr = f"'{dx / 2}'"
        y_expr = f"'{dy * 0.1}+{dy * 0.8}*cos(t*{speed * 0.7})'"
    else:
        x_expr = f"'{dx * 0.1}+{dx * 0.8}*sin(t*{speed})'"
        y_expr = f"'{dy * 0.1}+{dy * 0.8}*cos(t*{speed * 0.85})'"
    return f"scale={sw}:{sh},crop=1280:720:{x_expr}:{y_expr}"


def blend_videos(v1: Path, v2: Path, dst: Path, opacity: float | None = None) -> float:
    """Наложение v2 поверх v1 с полупрозрачностью. Возвращает фактический opacity."""
    if opacity is None:
        opacity = round(random.uniform(0.70, 0.88), 2)

    dur1 = probe_duration(v1)
    dur2 = probe_duration(v2)
    min_dur = min(dur1, dur2) if dur1 > 0 and dur2 > 0 else max(dur1, dur2)
    if min_dur <= 0:
        raise RuntimeError(f"both videos zero-length: {v1.name}, {v2.name}")

    modes = ["normal", "screen", "multiply"]
    mode = random.choice(modes)

    graph = (
        f"[0:v]scale=1280:720,setsar=1,trim=duration={min_dur},setpts=PTS-STARTPTS[b];"
        f"[1:v]scale=1280:720,setsar=1,trim=duration={min_dur},setpts=PTS-STARTPTS[o];"
        f"[b][o]blend=all_mode={mode}:all_opacity={opacity}[v]"
    )

    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(v1), "-i", str(v2),
        "-filter_complex", graph,
        "-map", "[v]", "-an",
        "-threads", "2",
        "-c:v", "libx264", "-profile:v", "baseline", "-level:v", "3.1",
        "-pix_fmt", "yuv420p", "-preset", "fast", "-crf", "23",
        "-movflags", "+faststart",
        str(dst),
    ]

    r = sh(cmd, timeout=900)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-600:])
    return opacity


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-folder", required=True, help="YaD raw folder, without ydrive:")
    ap.add_argument("--effects", choices=["off", "random", "all"], default="random",
                    help="эффекты: off=базовый рецепт, random=ровно один эффект из effects.json, "
                         "all=каждый эффект отдельным файлом")
    ap.add_argument("--blend", choices=["off", "random", "all"], default="off",
                    help="бленд двух видео: off=отключен, random=1 blend-пара на каждые "
                         "2 клипа, all=все возможные уникальные пары")
    ap.add_argument("--effects-json", default=None,
                    help="путь к effects.json (по умолчанию рядом со скриптом)")
    args = ap.parse_args()

    try:
        os.nice(15)
    except Exception:
        pass

    effects_db = load_effects()
    source_folder = args.source_folder.rstrip("/")
    if source_folder.endswith("/raw"):
        dest_folder = source_folder[:-4] + "/uniq"
    else:
        dest_folder = source_folder + "/uniq"

    work_root = Path(tempfile.mkdtemp(prefix="pinterest_board_uniquize_"))
    raw_local = work_root / "raw"
    uniq_local = work_root / "uniq"
    raw_local.mkdir(parents=True, exist_ok=True)
    uniq_local.mkdir(parents=True, exist_ok=True)

    r = sh(["rclone", "copy", f"ydrive:{source_folder}", str(raw_local)], timeout=1800)
    if r.returncode != 0:
        print(r.stderr[-1000:], flush=True)
        return 2

    sources = sorted(p for p in raw_local.glob("*.mp4") if p.is_file())
    if not sources:
        print(f"[uniq] no mp4 files in {source_folder}", flush=True)
        return 1

    all_effect_names = list(effects_db["vf"].keys()) + list(effects_db["complex"].keys())

    ok = 0
    failures: list[dict] = []
    records: list[dict] = []
    for src in sources:
        fps = probe_fps(src)
        if args.effects == "all":
            jobs = [(c, uniq_local / f"{src.stem}_uniq_{c}.mp4") for c in all_effect_names]
        elif args.effects == "random":
            chain = pick_chain(effects_db)
            tag = "+".join(chain)
            jobs = [(chain, uniq_local / f"{src.stem}_uniq.mp4")]
        else:
            jobs = [([], uniq_local / f"{src.stem}_uniq.mp4")]
        for chain, dst in jobs:
            tag = "+".join(chain) if chain else "base"
            print(f"[uniq] {src.name} -> {dst.name} (chain={tag}, fps={fps:.0f})", flush=True)
            try:
                uniquize(src, dst, fps=fps, effects_chain=chain, effects_db=effects_db)
                records.append(
                    {
                        "source": src.name,
                        "output": dst.name,
                        "effects_chain": chain,
                        "source_bytes": src.stat().st_size,
                        "output_bytes": dst.stat().st_size,
                        "source_duration": round(probe_duration(src), 3),
                    }
                )
                ok += 1
            except Exception as exc:
                failures.append({"source": src.name, "effects_chain": chain, "error": str(exc)})
                print(f"[uniq] FAIL {src.name} (chain={tag}): {exc}", flush=True)

    blend_dir = uniq_local / "blend"
    if args.blend != "off" and len(sources) >= 2:
        blend_dir.mkdir(exist_ok=True)
        shuffled = list(sources)
        random.shuffle(shuffled)
        if args.blend == "all":
            pairs = []
            for i in range(len(shuffled)):
                for j in range(i + 1, len(shuffled)):
                    pairs.append((shuffled[i], shuffled[j]))
        else:
            pairs = [(shuffled[i], shuffled[i + 1]) for i in range(0, len(shuffled) - 1, 2)]
        for v1, v2 in pairs:
            pair_tag = f"{v1.stem}__{v2.stem}"
            blend_dst = blend_dir / f"{pair_tag}_blend.mp4"
            print(f"[blend] {v1.name} + {v2.name} -> {blend_dst.name}", flush=True)
            try:
                opacity = blend_videos(v1, v2, blend_dst)
                records.append(
                    {
                        "source": f"{v1.name} + {v2.name}",
                        "output": str(blend_dst.relative_to(uniq_local)),
                        "effects_chain": ["blend"],
                        "blend_opacity": opacity,
                        "blend_mode": "mixed",
                        "source_bytes": v1.stat().st_size + v2.stat().st_size,
                        "output_bytes": blend_dst.stat().st_size,
                        "source_duration": round(min(probe_duration(v1), probe_duration(v2)), 3),
                    }
                )
                ok += 1
                # Blend is itself the one allowed effect for this output.  Do not
                # bake another effect on top of it, even in diagnostic all-mode.
            except Exception as exc:
                failures.append({"source": f"{v1.name}+{v2.name}", "effects_chain": ["blend"], "error": str(exc)})
                print(f"[blend] FAIL {v1.name}+{v2.name}: {exc}", flush=True)

    (uniq_local / "unique_manifest.json").write_text(
        json.dumps(
            {
                "source_folder": source_folder,
                "dest_folder": dest_folder,
                "effects_mode": args.effects,
                "blend_mode": args.blend,
                "count_source": len(sources),
                "count_ok": ok,
                "count_failed": len(failures),
                "records": records,
                "failures": failures,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    r = sh(["rclone", "mkdir", f"ydrive:{dest_folder}"], timeout=120)
    if r.returncode != 0:
        print(r.stderr[-1000:], flush=True)
        return 2
    r = sh(["rclone", "copy", str(uniq_local), f"ydrive:{dest_folder}"], timeout=1800)
    if r.returncode != 0:
        print(r.stderr[-1000:], flush=True)
        return 2

    print(f"[uniq] uploaded -> ydrive:{dest_folder}", flush=True)
    print(f"[uniq] summary ok={ok} failed={len(failures)} total={len(sources)}", flush=True)
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
