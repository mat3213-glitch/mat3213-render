#!/usr/bin/env python3
"""
graphics_dispatcher.py — Ф5 (L7): выбор ОДНОГО 2D-оверлея под контекст кадра.

Архитектура v4.1: три движка не запускаем; dispatcher детерминированно выбирает
ОДИН оверлей из готового Remotion-набора (tsx_overlay движок) по секции/энергии.
manim выкинут, motion-canvas — позже по нужде. Здесь — только РОУТИНГ (движок и
рендер уже есть: tsx_overlay.yml + tsx_overlay_job.py, оверлеи с alpha-каналом).

Оверлеи (готовые в remotion/src/overlays): MobyTitle (лайнер-титр, края трека),
AccentBurst (акцент на пике), BeatPulse (ритм-пульс на энергии), FocusBracket
(рамка-фокус на герое/крупном). Оверлей самоценен графикой — текст максимум бренд-акцент.
"""

from pathlib import Path

OVERLAYS = ("MobyTitle", "AccentBurst", "BeatPulse", "FocusBracket")
_OVL_DIR = Path(__file__).resolve().parent.parent / "remotion" / "src" / "overlays"


def approved_overlays() -> set:
    """Множество ОДОБРЕННЫХ оверлеев (approved: yes в <Name>.md). Гейт yaromat."""
    ok = set()
    if _OVL_DIR.exists():
        for md in _OVL_DIR.glob("*.md"):
            for line in md.read_text(encoding="utf-8").splitlines():
                if line.strip().lower().startswith("approved:"):
                    if line.split(":", 1)[1].strip().lower() in ("yes", "true", "да"):
                        ok.add(md.stem)
                    break
    return ok


def pick_overlay(section: str, energy: str, scale: str = "",
                 approved_only: bool = False) -> str | None:
    """ОДИН оверлей под кадр, или None (без графики — большинство кадров чистые).
    Графику ставим ТОЧЕЧНО (не на каждый кадр), иначе перегруз. approved_only=True →
    неодобренный оверлей → None (прод не падает на гейте, ждёт одобрения yaromat)."""
    s = (section or "body").lower()
    e = (energy or "medium").lower()
    sc = (scale or "").lower()
    if s in ("intro", "outro"):
        pick = "MobyTitle"            # лайнер-титр в начале/конце
    elif s == "climax":
        pick = "AccentBurst"          # акцент-хит на кульминации
    elif s == "body" and e == "high":
        pick = "BeatPulse"            # ритм-пульс на энергичном body
    elif sc in ("macro", "close"):
        pick = "FocusBracket"         # рамка-фокус на крупном/герое
    else:
        pick = None                   # атмосферный проходной кадр — без графики
    if approved_only and pick is not None and pick not in approved_overlays():
        return None
    return pick


def overlay_job(overlay: str, base_clip: str, fmt: str = "vertical",
                at: float = 1.0, overlay_dur: float = 2.0, seed: int = 42,
                out_name: str = "graphics_out.mp4", accent_text: str = "") -> dict:
    """job.json для tsx_overlay_job.py (готовый движок рендера+композита)."""
    job = {"overlay": overlay, "format": fmt, "out_name": out_name,
           "base_clip": base_clip, "at": at, "overlay_dur": overlay_dur, "seed": seed}
    if accent_text:
        job["accentText"] = accent_text
    return job


if __name__ == "__main__":
    for c in [("intro", "low", "wide"), ("climax", "high", "macro"),
              ("body", "high", "medium"), ("body", "low", "close"),
              ("body", "medium", "wide")]:
        print(c, "→", pick_overlay(*c))
