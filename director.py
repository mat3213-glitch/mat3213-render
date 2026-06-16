#!/usr/bin/env python3
"""
director.py — агент-режиссёр-раскадровщик: treatment.json → storyboard.json.

Берёт драматургический трактат сценариста + музыкальный таймлайн (analyze.py: bpm + сегменты)
и раскладывает по кадрам с конкретными источниками футажа, масштабом, движением, склейками.
Методология: skills/craft/director/SKILL.md.

Принцип: ОДИН кадр на сегмент. Код владеет таймкодами (из analyze — без галлюцинаций LLM),
LLM аннотирует каждый сегмент (section/scale/motion/transition/base/overlay), затем код
детерминированно резолвит overlay-категории в реальные клипы каталога (asset_catalog.pick, seed).

Free-LLM: Groq → Gemini фолбэк (как сценарист).

Usage:
  python3 director.py treatment.json --track audio.mp3              # analyze сам
  python3 director.py treatment.json --segments segs.json          # готовый таймлайн
  python3 director.py treatment.json --duration 119 --bpm 147      # fallback-сетка (тест/standalone)
  python3 director.py treatment.json ... --seed nichego -o storyboard.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path

import requests
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except Exception:
    pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import asset_catalog  # pick / load

GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL     = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL   = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

MOTIONS     = {"static", "slow_push", "zoom_in", "zoom_out", "drift", "handheld", "tilt"}
TRANSITIONS = {"cut", "crossfade", "dip_to_black"}
SCALES      = {"wide", "medium", "macro"}

SYSTEM = """Ты — режиссёр-раскадровщик музыкальных клипов yaromat (future garage/downtempo,
инструментал, БЕЗ лиц). Тебе дан драматургический трактат (treatment) и РЕАЛЬНЫЙ музыкальный
таймлайн — пронумерованные сегменты трека (с энергией). Разложи дугу трактата по кадрам:
РОВНО ОДИН кадр на каждый сегмент (по его номеру seg).

На каждый сегмент реши:
- section: к какому beat трактата относится (intro/body/climax/outro) — по позиции на таймлайне.
- scale: wide|medium|macro (дуга масштаба: общий план в завязке → макро к кульминации).
- motion: static|slow_push|zoom_in|zoom_out|drift|handheld|tilt — ОРГАНИЧНАЯ камера (не вектор).
- transition: cut|crossfade|dip_to_black (плотнее/жёстче склейки к дропу; мягче в интро/аутро).
- intent: одной фразой — драматическое назначение кадра в дуге.
- base: СУБЪЕКТ кадра. {"kind":"search","query":"<англ. запрос футажа из imagery_cues>","provider":"openverse"}
  или {"kind":"generate","prompt":"<промпт Qwen/i2v>"}. Это фотографичный реальный футаж/генерация.
- overlay_category: "overlay" (филмик-текстура поверх) | "soundwave" (на ритм/кульминацию) |
  "vinil" (если кадр про объект-винил) | null. Это слой из каталога, его подберёт код.

ЖЁСТКО: соблюдай avoid из трактата — НИКОГДА не выдавай в base то, что в avoid (лица/неон/
одинокая фигура/синтетика и явные запреты cue). Мотив трактата (central_motif) держи сквозным.

Верни СТРОГО JSON:
{"shots":[{"seg":0,"section":"intro","scale":"wide","motion":"slow_push","transition":"crossfade",
           "intent":"...","base":{"kind":"search","query":"...","provider":"openverse"},
           "overlay_category":"overlay"}], "notes":"один-два слова о замысле"}
Кадров РОВНО столько, сколько сегментов. Только JSON."""


# ── музыкальный таймлайн ─────────────────────────────────────────────────────
def load_segments(args) -> tuple[float, list[dict]]:
    """→ (bpm, [{track_pos,duration,n_beats,energy}]). Источник: --segments / --track / fallback."""
    if args.segments:
        data = json.loads(Path(args.segments).read_text(encoding="utf-8"))
        segs = data["segments"] if isinstance(data, dict) else data
        bpm = (data.get("bpm") if isinstance(data, dict) else None) or args.bpm or 120.0
        return float(bpm), [dict(s) for s in segs]
    if args.track:
        from analyze import analyze_track
        bpm, segs = analyze_track(args.track, duration=args.duration, seed=args.seed_int)
        return bpm, [{"track_pos": s.track_pos, "duration": s.duration,
                      "n_beats": s.n_beats, "energy": s.energy} for s in segs]
    # fallback-сетка из duration+bpm (без аудио): для теста/standalone
    bpm = float(args.bpm or 120.0)
    dur = float(args.reel_dur or args.duration or 60.0)
    reel = bool(args.reel_dur)
    beat = 60.0 / bpm
    segs, t = [], 0.0
    while t < dur - 0.5:
        frac = t / dur
        if reel:   # рил = окно дропа, интро уже отрезано → без low; пик в середине
            energy = "high" if frac < 0.6 else "medium"
        else:
            energy = "low" if frac < 0.25 else ("high" if 0.45 < frac < 0.8 else "medium")
        nb = {"low": 8, "medium": 8, "high": 4}[energy]
        sd = min(nb * beat, dur - t)
        segs.append({"track_pos": round(t, 2), "duration": round(sd, 2),
                     "n_beats": nb, "energy": energy})
        t += sd
    return bpm, segs


# ── LLM (Groq → Gemini) ──────────────────────────────────────────────────────
def _call_groq(prompt: str) -> str | None:
    if not GROQ_API_KEY:
        return None
    try:
        r = requests.post("https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.6, "response_format": {"type": "json_object"}}, timeout=120)
        if r.status_code == 200:
            print(f"[llm] раскадровка через Groq ({GROQ_MODEL})")
            return r.json()["choices"][0]["message"]["content"].strip()
        print(f"[llm] Groq HTTP {r.status_code} — пробую Gemini")
    except Exception as e:
        print(f"[llm] Groq сеть ({e}) — пробую Gemini")
    return None


def _call_gemini(prompt: str) -> str | None:
    if not GEMINI_API_KEY:
        return None
    models = [GEMINI_MODEL] + [m for m in ("gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest")
                               if m != GEMINI_MODEL]
    payload = {"contents": [{"parts": [{"text": prompt}]}],
               "generationConfig": {"temperature": 0.6, "response_mime_type": "application/json"}}
    for model in models:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
               f"?key={GEMINI_API_KEY}")
        for attempt in range(2):
            try:
                r = requests.post(url, json=payload, timeout=120)
            except Exception as e:
                print(f"[llm] Gemini {model}: сеть ({e})"); break
            if r.status_code == 200:
                print(f"[llm] раскадровка через Gemini ({model})")
                return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            if r.status_code in (429, 503) and attempt == 0:
                continue
            print(f"[llm] Gemini {model}: HTTP {r.status_code}"); break
    return None


VISUALIZER_NOTE = """
=== РЕЖИМ ВИЗУАЛАЙЗЕРА (вертикальный рил из каталога) ===
base — НЕ внешний футаж, а слой из НАШЕГО каталога: {"kind":"catalog","category":"vinil"|"soundwave"}.
  vinil = устойчивый грув/дыхание/тело трека; soundwave = пики/ритм/кульминация.
overlay_category — "overlay" (филмик-текстура поверх) | null.
Конкретные клипы подберёт код. Мотив трактата выражаем РИТМОМ/масштабом/движением, а не литералом."""


def build_prompt(treatment: dict, bpm: float, segs: list[dict], cat_summary: str,
                 visualizer: bool = False) -> str:
    seg_lines = "\n".join(
        f"  seg {i}: t={s['track_pos']}с dur={s['duration']}с energy={s['energy']}"
        for i, s in enumerate(segs))
    return (
        SYSTEM
        + (VISUALIZER_NOTE if visualizer else "")
        + "\n\n=== TREATMENT ===\n" + json.dumps(treatment, ensure_ascii=False, indent=1)
        + f"\n\n=== ТАЙМЛАЙН ТРЕКА (bpm={bpm:.0f}, сегментов={len(segs)}) ===\n" + seg_lines
        + "\n\n=== ИНВЕНТАРЬ КАТАЛОГА ===\n" + cat_summary
        + f"\n\nВыдай РОВНО {len(segs)} кадров (по одному на seg 0..{len(segs)-1})."
    )


def generate_shots(treatment: dict, bpm: float, segs: list[dict], cat_summary: str,
                   visualizer: bool = False) -> list[dict]:
    prompt = build_prompt(treatment, bpm, segs, cat_summary, visualizer)
    raw = _call_groq(prompt) or _call_gemini(prompt)
    if not raw:
        sys.exit("Ни Groq, ни Gemini не ответили (проверь ключи/гео).")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raw = raw.strip().lstrip("`").replace("json", "", 1).strip().rstrip("`")
        data = json.loads(raw)
    shots = data.get("shots") if isinstance(data, dict) else data
    if not isinstance(shots, list) or not shots:
        sys.exit("LLM не вернул shots[]")
    return shots


# ── сборка + резолв каталога ─────────────────────────────────────────────────
def _resolve_cat(cat: str, orientation, seed_key, used: set) -> dict | None:
    """Детерминированно подобрать клип каталога категории cat, избегая уже использованных."""
    if cat not in ("overlay", "soundwave", "vinil"):
        return None
    # ориентацию для base не зажимаем (вертикали в каталоге мало — кропнем при рендере)
    cand = asset_catalog.pick(category=cat, orientation=orientation, n=12, seed=seed_key) \
        or asset_catalog.pick(category=cat, n=12, seed=seed_key)
    cand = [c for c in cand if c["id"] not in used] or cand
    if not cand:
        return None
    e = cand[0]
    used.add(e["id"])
    return {"category": cat, "id": e["id"], "path": e["path"],
            "blend": e.get("blend", "screen"), "duration": e.get("duration")}


def assemble(treatment: dict, bpm: float, segs: list[dict], shots: list[dict],
             seed, orientation: str | None, visualizer: bool = False) -> dict:
    by_seg = {}
    for sh in shots:
        try:
            by_seg[int(sh.get("seg"))] = sh
        except (TypeError, ValueError):
            continue

    used: set[str] = set()       # не реюзать один клип подряд
    out_shots = []
    for i, seg in enumerate(segs):
        sh = by_seg.get(i, {})
        motion = sh.get("motion") if sh.get("motion") in MOTIONS else "slow_push"
        trans  = sh.get("transition") if sh.get("transition") in TRANSITIONS else "cut"
        scale  = sh.get("scale") if sh.get("scale") in SCALES else "medium"
        base   = sh.get("base") if isinstance(sh.get("base"), dict) else {}

        # base: в визуалайзере резолвим из каталога (целевой футаж), иначе оставляем запрос/генерацию
        if visualizer or base.get("kind") == "catalog":
            bcat = base.get("category")
            if bcat not in ("vinil", "soundwave"):
                bcat = "soundwave" if seg.get("energy") == "high" else "vinil"
            rb = _resolve_cat(bcat, orientation, f"{seed}-base-{i}", used)
            base = {"kind": "catalog", **rb} if rb else {"kind": "catalog", "category": bcat}

        overlay = _resolve_cat(sh.get("overlay_category"), orientation, f"{seed}-ov-{i}", used)

        out_shots.append({
            "idx": i,
            "t_start": round(float(seg["track_pos"]), 2),
            "t_dur": round(float(seg["duration"]), 2),
            "section": sh.get("section", ""),
            "energy": seg.get("energy", ""),
            "scale": scale, "motion": motion, "transition": trans,
            "intent": sh.get("intent", ""),
            "base": base,
            "overlay": overlay,
        })

    return {
        "track": treatment.get("track") or "",
        "bpm": round(bpm, 1),
        "duration": round(segs[-1]["track_pos"] + segs[-1]["duration"], 2) if segs else 0,
        "logline": treatment.get("logline", ""),
        "central_motif": treatment.get("central_motif", ""),
        "avoid": treatment.get("avoid", []),
        "shots": out_shots,
    }


def catalog_summary() -> str:
    items = asset_catalog.load()
    by: dict = {}
    for e in items:
        by.setdefault(e.get("category", "?"), []).append(e)
    lines = []
    for c, es in sorted(by.items()):
        ori = sorted({e.get("orientation", "?") for e in es})
        durs = sorted(e.get("duration", 0) for e in es)
        lines.append(f"  {c}: {len(es)} клипов, ориентации {ori}, "
                     f"длит. {durs[0]:.0f}–{durs[-1]:.0f}с")
    return "\n".join(lines) or "  (каталог пуст)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("treatment", help="treatment.json от сценариста")
    ap.add_argument("--track", help="аудиофайл → analyze.py сам")
    ap.add_argument("--segments", help="готовый segments.json от analyze")
    ap.add_argument("--duration", type=float, help="для fallback-сетки/analyze")
    ap.add_argument("--bpm", type=float, help="для fallback-сетки")
    ap.add_argument("--seed", default="default", help="детерминизм на трек")
    ap.add_argument("--orientation", choices=["horizontal", "vertical", "square"],
                    help="ориентация overlay-клипов под формат рендера")
    ap.add_argument("--reel-dur", type=float, help="длина рила (с): окно сегментов вместо полного")
    ap.add_argument("--skip-sec", type=float, default=0.0,
                    help="пропустить первые N с (интро брать ЗАПРЕЩЕНО → ставить ≥ длины интро)")
    ap.add_argument("--visualizer", action="store_true",
                    help="base из каталога (vinil/soundwave), не внешний футаж — рил-визуалайзер")
    ap.add_argument("-o", "--out", help="куда писать storyboard.json (default: рядом с treatment)")
    ap.add_argument("--print", action="store_true", dest="to_stdout")
    a = ap.parse_args()
    a.seed_int = abs(hash(a.seed)) % (2**31)

    treatment = json.loads(Path(a.treatment).read_text(encoding="utf-8"))
    bpm, segs = load_segments(a)
    if not segs:
        sys.exit("пустой таймлайн (нет сегментов)")

    # рил-окно: ИНТРО БРАТЬ ЗАПРЕЩЕНО → отрезаем первые skip-sec, берём reel-dur секунд
    if a.skip_sec > 0:
        segs = [s for s in segs if float(s["track_pos"]) >= a.skip_sec - 0.01]
    if a.reel_dur and segs:
        t0 = float(segs[0]["track_pos"]); win = []
        for s in segs:
            if float(s["track_pos"]) - t0 >= a.reel_dur:
                break
            win.append(s)
        segs = win
    if not segs:
        sys.exit("после рил-окна не осталось сегментов (проверь skip-sec/reel-dur)")
    print(f"[director] сегментов: {len(segs)} | bpm={bpm:.0f} | seed={a.seed} | "
          f"окно [{segs[0]['track_pos']:.1f}..{segs[-1]['track_pos']+segs[-1]['duration']:.1f}]с"
          f"{' (интро отрезано)' if a.skip_sec else ''}")

    cat = catalog_summary()
    print(f"[director] каталог:\n{cat}")
    shots = generate_shots(treatment, bpm, segs, cat, a.visualizer)
    storyboard = assemble(treatment, bpm, segs, shots, a.seed, a.orientation, a.visualizer)
    storyboard["format"] = "vertical" if a.orientation == "vertical" else (a.orientation or "landscape")
    storyboard["visualizer"] = a.visualizer

    out_json = json.dumps(storyboard, ensure_ascii=False, indent=2)
    print("\n=== STORYBOARD ===")
    print(out_json)
    if not a.to_stdout:
        out = Path(a.out) if a.out else Path(a.treatment).with_name("storyboard.json")
        out.write_text(out_json, encoding="utf-8")
        print(f"\n✅ storyboard → {out}")


if __name__ == "__main__":
    main()
