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

Для мягких fade/dissolve разрешён только локальный deterministic smoothstep. Он
выражается через штатный xfade=custom и не требует стороннего FFmpeg/binary.
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

# Закрытый список примитивов, которым разрешено менять временную кривую. P в xfade
# идёт от 1 (первый кадр) к 0 (второй), поэтому smoothstep(P) можно напрямую использовать
# как вес A. Запятые в expr экранированы для синтаксиса filter_complex.
_SMOOTHSTEP = "P*P*(3-2*P)"
XFADE_EASING = {
    "fade": {
        "curve": "smoothstep",
        "expr": f"A*({_SMOOTHSTEP})+B*(1-({_SMOOTHSTEP}))",
    },
    # Координатный hash фиксирован: один и тот же кадр/seed всегда даёт одинаковую маску.
    # Порог движется по smoothstep, а не линейно; PLANE сохраняет независимость каналов.
    "dissolve": {
        "curve": "smoothstep",
        "expr": (
            "if(lte(abs(sin(X*12.9898+Y*78.233+PLANE*37.719))\\,"
            f"{_SMOOTHSTEP})\\,A\\,B)"
        ),
    },
}


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


def xfade_render_spec(ttype: str) -> tuple[str | None, str | None]:
    """Возвращает ``(transition, expr)`` для рендера.

    Easing fail-closed: custom expression доступен только для fade/dissolve из
    XFADE_EASING. Все прочие приёмы сохраняют штатное имя и линейное поведение.
    """
    name = xfade_name(ttype)
    profile = XFADE_EASING.get(name)
    if profile is None:
        return name, None
    return "custom", profile["expr"]


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
