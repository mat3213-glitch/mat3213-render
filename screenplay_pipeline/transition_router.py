#!/usr/bin/env python3
"""
transition_router.py — детерминированный выбор ОДНОГО приёма на стык (L6, архитектура v4.1).

Решает узел конфликтов: gl-dissolve / glitch / film-burn / hard-cut — НЕ стек всех,
а один по контексту стыка. Чистая функция, без LLM, без внешних зависимостей.
Таблица mimo, адаптированная под НАШИ секции (intro/body/climax/outro) + energy
(low/medium/high) + тип соседних кадров (atmosphere/subject/black).

Развязка от treatment (per-clip грейд/зерно) и slowmo — на уровне рендера:
router даёт ТОЛЬКО тип стыка; slowmo-сосед → длительность перехода ×1.5.

Приёмы → рендер-примитивы (встроенный ffmpeg xfade, без кастом-билда gl-transitions):
  gl-dissolve → xfade=fade ; glitch → xfade=pixelize ; film-burn → xfade=fadegrays ;
  hard-cut → concat (без перехода). ffglitch/настоящий film-burn overlay — апгрейд позже.
"""

XFADE_MAP = {
    "gl-dissolve": "fade",
    "dip": "fadeblack",      # быстрый дип-в-чёрное на энергии (не цифровой глитч — yaromat 07-10)
    "film-burn": "fadegrays",
    "hard-cut": None,        # concat без перехода
}
# длительность перехода по приёму: dip рубленый (короткий), растворение/плёнка мягче
TDUR = {"gl-dissolve": 0.7, "dip": 0.28, "film-burn": 0.6, "hard-cut": 0.0}
DEFAULT_TDUR = 0.7
SLOWMO_FACTOR = 1.5


def lookup_transition(section: str, energy: str,
                      prev_type: str = "atmosphere", next_type: str = "atmosphere") -> str:
    """Один приём на стык. Первое совпадение = выбор (порядок = приоритет)."""
    s = (section or "body").lower()
    e = (energy or "medium").lower()
    p = (prev_type or "atmosphere").lower()
    n = (next_type or "atmosphere").lower()

    if p == "black" or n == "black":
        return "hard-cut"                 # к/от черноты — только резко
    if s == "climax" and e == "high":
        return "hard-cut"                 # пик энергии — рубленый монтаж
    if s == "climax":
        return "dip"                      # вход в кульминацию — рубленый дип по биту
    if s in ("intro", "outro"):
        return "gl-dissolve"              # края трека — мягко
    if s == "body" and e == "high":
        return "dip"
    if p == "atmosphere" and n == "atmosphere":
        return "gl-dissolve"              # атмосфера↔атмосфера — растворение
    if p != n:
        return "film-burn"                # смена типа кадра — плёночный стык
    return "gl-dissolve"                  # дефолт — мягкое растворение


def transition_duration(ttype: str = None, base: float = None,
                        prev_slowmo: bool = False, next_slowmo: bool = False) -> float:
    """Длительность перехода по приёму; slowmo-сосед → ×1.5 (сглаживает скачок скорости)."""
    d = base if base is not None else TDUR.get(ttype, DEFAULT_TDUR)
    return round(d * (SLOWMO_FACTOR if (prev_slowmo or next_slowmo) else 1.0), 3)


def xfade_name(ttype: str) -> str | None:
    """ffmpeg xfade-имя для приёма (None = hard-cut/concat)."""
    return XFADE_MAP.get(ttype)


if __name__ == "__main__":
    # быстрый смоук
    cases = [
        ("climax", "high", "subject", "atmosphere"),
        ("intro", "low", "atmosphere", "atmosphere"),
        ("body", "high", "atmosphere", "atmosphere"),
        ("body", "low", "atmosphere", "subject"),
        ("body", "medium", "atmosphere", "atmosphere"),
        ("outro", "low", "atmosphere", "black"),
    ]
    for c in cases:
        print(c, "→", lookup_transition(*c))
