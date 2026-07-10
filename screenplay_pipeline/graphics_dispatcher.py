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

OVERLAYS = ("MobyTitle", "AccentBurst", "BeatPulse", "FocusBracket")


def pick_overlay(section: str, energy: str, scale: str = "") -> str | None:
    """ОДИН оверлей под кадр, или None (без графики — большинство кадров чистые).
    Графику ставим ТОЧЕЧНО (не на каждый кадр), иначе перегруз."""
    s = (section or "body").lower()
    e = (energy or "medium").lower()
    sc = (scale or "").lower()
    if s in ("intro", "outro"):
        return "MobyTitle"            # лайнер-титр в начале/конце
    if s == "climax":
        return "AccentBurst"          # акцент-хит на кульминации
    if s == "body" and e == "high":
        return "BeatPulse"            # ритм-пульс на энергичном body
    if sc in ("macro", "close"):
        return "FocusBracket"         # рамка-фокус на крупном/герое
    return None                       # атмосферный проходной кадр — без графики


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
