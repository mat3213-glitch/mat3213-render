#!/usr/bin/env python3
"""
transition_render.py — сборка кадров в timeline с переходами transition-router (L6).

Проблема: xfade перекрывает клипы и СЪЕДАЕТ время → в energy-locked EDL накопился бы
дрейф синка с музыкой. Решение: каждый кадр рендерится с ЗАПАСОМ-хвостом = длит.
исходящего перехода; xfade перекрытие съедает ровно этот запас → нетто-длительность
кадра = его t_dur, тайминг EDL сохраняется.

build_xfade_chain(durs, trans) строит filter_complex цепочки xfade. hard-cut → крошечный
xfade (1 кадр) = визуально рез, но цепочка однородна. Проверено локально на длительность.
"""
FRAME = 0.04   # «hard-cut» = xfade в 1 кадр (однородная цепочка, визуально рез)


def build_xfade_chain(durs: list[float], trans: list[tuple]):
    """durs[i] = РЕНДЕРНАЯ длительность клипа i (уже с хвостом). trans[i]=(name,d) для
    стыка, ВХОДЯЩЕГО в клип i (i>=1). Возвращает (filter_complex, финальный_label, total)."""
    parts = []
    label = "[0:v]"
    acc = durs[0]
    for i in range(1, len(durs)):
        name, d = trans[i]
        d = max(d, FRAME)
        off = max(0.0, acc - d)
        out = f"[x{i}]"
        parts.append(f"{label}[{i}:v]xfade=transition={name}:duration={d:.3f}:"
                     f"offset={off:.3f}{out}")
        label = out
        acc = acc + durs[i] - d
    return ";".join(parts), label, round(acc, 3)


def render_tail(t_dur: float, out_trans_d: float) -> float:
    """Сколько рендерить клип: его t_dur + запас на исходящий переход (съест xfade)."""
    return round(t_dur + max(out_trans_d, 0.0), 3)
