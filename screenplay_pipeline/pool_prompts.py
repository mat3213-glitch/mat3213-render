#!/usr/bin/env python3
"""
pool_prompts.py — осмысленные промпты для daily-пулов (Qwen/VeoFree/Hunyuan) вместо шаблонных
списков. Берёт реальный бриф ПОСЛЕДНЕГО трека, зашедшего в конвейер (render_jobs/*/brief_full.yaml
на ЯД, самый свежий по времени модификации) → строит промпты комбинаторикой (варьирует
масштаб/ракурс/деталь вокруг central_motif/visual_mood/narrative_angle трека), НЕ шаблонными
generic-фразами не привязанными ни к чему. Детерминированно (без LLM — комбинаторика по реальным
полям, тот же принцип что rebalance_motion в director.py).

Если брифов ещё нет (конвейер пуст) — деградирует на старый generic-список (не ломает daily-крон).

Usage (как модуль):
    from pool_prompts import build_image_prompts, build_video_prompts, build_motion_prompts
    prompts = build_image_prompts(n=4)
"""
import json
import subprocess
import sys
from pathlib import Path

YD_ROOT = "ydrive:Content factory/cloud_io/render_jobs"

# фолбэк на случай пустого конвейера — старый generic-вайб (не выкидываем, это safety net)
FALLBACK_MOOD_WORDS = ["melancholic", "nostalgic", "atmospheric", "muted", "desaturated"]
FALLBACK_VISUAL = "dark moody empty space, dim light, film grain"
FALLBACK_GENRE = "future garage downtempo"

FRAMING = ["wide establishing shot of", "extreme close-up detail of", "macro texture shot of",
           "medium shot of", "low angle shot of", "overhead shot of"]
LIGHT_VARIANTS = ["soft diffused light", "harsh single light source", "backlit silhouette",
                  "warm practical lighting", "cold blue-hour light", "candlelight glow"]

BRAND_SUFFIX = (", no faces, no people, no text, no neon, no watermark, photographic, film grain, "
                "cinematic, muted desaturated palette")


def _sh(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return r.stdout if r.returncode == 0 else ""


def get_latest_track_brief() -> dict | None:
    """Самый свежий brief_full.yaml на ЯД render_jobs/*/. None если конвейер пуст."""
    out = _sh(["rclone", "lsjson", YD_ROOT, "--dirs-only"])
    if not out:
        return None
    try:
        dirs = json.loads(out)
    except Exception:
        return None
    dirs.sort(key=lambda d: d.get("ModTime", ""), reverse=True)
    for d in dirs[:5]:  # смотрим 5 самых свежих job-папок, берём первую с реальным brief_full.yaml
        job = d["Name"]
        check = _sh(["rclone", "lsf", f"{YD_ROOT}/{job}/", "--include", "brief_full.yaml"])
        if "brief_full.yaml" not in check:
            continue
        r = subprocess.run(["rclone", "cat", f"{YD_ROOT}/{job}/brief_full.yaml"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            continue
        try:
            import yaml
            return yaml.safe_load(r.stdout)
        except Exception:
            continue
    return None


def _track_vocab(brief: dict | None) -> dict:
    if not brief:
        return {"visual": FALLBACK_VISUAL, "mood_words": FALLBACK_MOOD_WORDS, "genre": FALLBACK_GENRE,
               "narrative": FALLBACK_VISUAL}
    c = brief.get("content", {}) or {}
    p = brief.get("production", {}) or {}
    return {
        "visual": c.get("visual_mood") or FALLBACK_VISUAL,
        "mood_words": c.get("mood_words") or FALLBACK_MOOD_WORDS,
        "genre": p.get("genre") or FALLBACK_GENRE,
        "narrative": c.get("narrative_angle") or "",
    }


def build_image_prompts(n: int, brief: dict | None = None) -> list[str]:
    if brief is None:
        brief = get_latest_track_brief()
    v = _track_vocab(brief)
    out = []
    for i in range(n):
        framing = FRAMING[i % len(FRAMING)]
        light = LIGHT_VARIANTS[i % len(LIGHT_VARIANTS)]
        mood = v["mood_words"][i % len(v["mood_words"])] if v["mood_words"] else "atmospheric"
        out.append(f"{framing} {v['visual']}, {light}, {mood} mood, {v['genre']}{BRAND_SUFFIX}")
    return out


def build_video_prompts(n: int, brief: dict | None = None) -> list[str]:
    if brief is None:
        brief = get_latest_track_brief()
    v = _track_vocab(brief)
    out = []
    for i in range(n):
        framing = FRAMING[i % len(FRAMING)]
        light = LIGHT_VARIANTS[(i + 2) % len(LIGHT_VARIANTS)]
        mood = v["mood_words"][i % len(v["mood_words"])] if v["mood_words"] else "atmospheric"
        motion = ["slow subtle drift", "gentle photographic motion", "still with faint movement"][i % 3]
        out.append(f"{framing} {v['visual']}, {motion}, {light}, {mood} mood{BRAND_SUFFIX}")
    return out


def build_motion_prompts(n: int, brief: dict | None = None) -> list[str]:
    """Для Hunyuan i2v daily (оживление УЖЕ существующих фото пулов) — промпт только про ДВИЖЕНИЕ,
    без описания сцены (сцена уже задана исходным фото)."""
    if brief is None:
        brief = get_latest_track_brief()
    v = _track_vocab(brief)
    motions = ["slow subtle drift, gentle movement within the frame",
              "faint photographic motion, barely perceptible drift",
              "gentle parallax-like drift, subtle depth movement",
              "slow breathing motion, soft ambient movement"]
    out = []
    for i in range(n):
        mood = v["mood_words"][i % len(v["mood_words"])] if v["mood_words"] else "atmospheric"
        out.append(f"{motions[i % len(motions)]}, {mood} mood{BRAND_SUFFIX}")
    return out


if __name__ == "__main__":
    brief = get_latest_track_brief()
    print(f"brief найден: {bool(brief)}", file=sys.stderr)
    print(json.dumps({
        "image": build_image_prompts(4, brief),
        "video": build_video_prompts(2, brief),
        "motion": build_motion_prompts(2, brief),
    }, ensure_ascii=False, indent=2))
