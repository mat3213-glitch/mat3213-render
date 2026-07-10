#!/usr/bin/env python3
"""
whisper_stage_test.py — гейт-артефакт этапа whisperx→director (Ф1b, тайминг-демо).

Прогоняет РЕАЛЬНЫЙ whisperx + РЕАЛЬНУЮ функцию director.snap_cuts_to_vocals на
бит-сетке трека и показывает, КУДА лягут резы относительно спетых слов. Это НЕ
рендер клипа (полный рендер только после OK yaromat на превью) — это лист резки
для творческого гейта: «резать на спетых словах — верно?».

Выход: whisper_cut_sheet.md + whisper_stage.json.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # screenplay_pipeline
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # github_actions_clips (director)

import vocal_sync
import director

CUT_EVERY_BEATS = 8   # базовая сетка резов: раз в 2 такта (демо-каркас вместо LLM-раскадровки)
WINDOW = 0.2


def main() -> int:
    audio = sys.argv[1] if len(sys.argv) > 1 else "track_audio"

    bpm, beats = vocal_sync.compute_beats(audio)
    lang, words = vocal_sync.transcribe_words(audio)
    words, locked = vocal_sync.snap_to_beats(words, beats, vocal_sync.TOL_MS)
    onsets = sorted(w["start"] for w in words if w.get("beat_locked"))

    total = (beats[-1] if beats else 0.0) + (60.0 / bpm if bpm else 2.0)
    grid = beats[::CUT_EVERY_BEATS] if beats else []
    shots = [{"t_start": round(t, 2), "t_dur": 0.0} for t in grid]
    for i in range(len(shots)):
        end = shots[i + 1]["t_start"] if i + 1 < len(shots) else total
        shots[i]["t_dur"] = round(max(0.1, end - shots[i]["t_start"]), 2)

    snapped = director.snap_cuts_to_vocals(shots, onsets, total, window=WINDOW)

    def word_at(t):
        best = min(words, key=lambda w: abs(w["start"] - t), default=None)
        return best if best and abs(best["start"] - t) <= WINDOW else None

    rows = []
    for sh in shots:
        vs = sh.get("vocal_snap")
        w = word_at(sh["t_start"])
        rows.append((sh["t_start"], vs["from"] if vs else sh["t_start"],
                     (sh["t_start"] - vs["from"]) if vs else 0.0,
                     w["word"] if w else "—", bool(vs)))

    md = [f"# Whisper-этап — лист резки (гейт Ф1b)\n",
          f"Трек: `{Path(audio).name}` · lang={lang} · bpm={bpm:.1f} · "
          f"слов={len(words)} · на-бите(±{vocal_sync.TOL_MS}мс)={locked}\n",
          f"Резов подвинуто к спетому слову: **{snapped}/{len(shots)}** (окно ±{WINDOW*1e3:.0f}мс).\n",
          "> Это ТАЙМИНГ-ДЕМО слоя whisperx (куда лягут резы), НЕ рендер клипа.",
          "> Гейт: рез, ложащийся на спетое слово, — верное направление?\n",
          "| рез t | было(бит) | сдвиг | спетое слово | снап |",
          "|---|---|---|---|---|"]
    for t, was, d, word, sn in rows:
        md.append(f"| {t:.2f}с | {was:.2f}с | {d:+.2f}с | {word!r} | {'🎯' if sn else '—'} |")
    md += ["\n## Спетые слова на бите (кандидаты в точки реза)",
           ", ".join(f"{w['word']!r}@{w['start']:.1f}с" for w in words if w.get("beat_locked")) or "—"]

    Path("whisper_cut_sheet.md").write_text("\n".join(md), encoding="utf-8")
    import json
    Path("whisper_stage.json").write_text(json.dumps(
        {"track": Path(audio).name, "lang": lang, "bpm": round(bpm, 1),
         "n_words": len(words), "n_beat_locked": locked, "cuts": len(shots),
         "cuts_snapped": snapped, "shots": shots, "words": words},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[stage] cut-sheet готов: {snapped}/{len(shots)} резов на спетых словах")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    sys.exit(main())
