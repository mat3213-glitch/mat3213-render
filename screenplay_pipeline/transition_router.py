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

XFADE_MAP = {"blend": "fade"}
# One blend on every seam.  1.75s sits inside the approved 1.5–2.0s delay range.
TDUR = {"blend": 1.75}
DEFAULT_TDUR = 1.75

# Закрытый список примитивов, которым разрешено менять временную кривую. P в xfade
# идёт от 1 (первый кадр) к 0 (второй), поэтому smoothstep(P) можно напрямую использовать
# как вес A. Запятые в expr экранированы для синтаксиса filter_complex.
_SMOOTHSTEP = "P*P*(3-2*P)"
XFADE_EASING = {
    "fade": {
        "curve": "smoothstep",
        "expr": f"A*({_SMOOTHSTEP})+B*(1-({_SMOOTHSTEP}))",
    },
}


def lookup_transition(section: str, energy: str,
                      prev_type: str = "atmosphere", next_type: str = "atmosphere") -> str:
    """Один приём на стык. Первое совпадение = выбор (порядок = приоритет)."""
    s = (section or "body").lower()
    e = (energy or "medium").lower()
    p = (prev_type or "atmosphere").lower()
    n = (next_type or "atmosphere").lower()

    del section, energy, prev_type, next_type
    return "blend"


def transition_duration(ttype: str = None, base: float = None,
                        prev_slowmo: bool = False, next_slowmo: bool = False) -> float:
    """Every seam is the same 1.75-second blend, including slow-motion neighbours."""
    del ttype, base, prev_slowmo, next_slowmo
    return DEFAULT_TDUR


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
