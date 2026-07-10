#!/usr/bin/env python3
"""
vocal_sync.py — вокал-слой тайминга (L1, whisperx). Для ВОКАЛЬНЫХ треков.

Даёт пословные таймкоды (whisperx transcribe+align) и привязывает их к бит-сетке:
каждое слово → ближайший бит, offset. Правило v4.1: energy = каркас, whisperx =
точечная коррекция реза ±TOL_MS от бита. Слова, попавшие в ±TOL от бита, помечаются
`beat_locked` — на них режиссёру ХОРОШО ставить рез (вокал+бит совпали).

НЕ-БЛОКИРУЮЩИЙ (fail-open): любая ошибка ASR / инструментал без слов / нет вокала →
пустые markers + reason, пайплайн падает на energy-каркас. whisperx НЕ single point
of failure (урок критики mimo).

Выход: vocal_markers.json {lang, bpm, tol_ms, n_words, n_beat_locked, words[]}.
"""
import argparse
import json
import sys
from pathlib import Path

TOL_MS = 50


def compute_bpm(audio_path: str) -> float:
    import librosa
    y, sr = librosa.load(audio_path, sr=22050, mono=True)
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    return float(tempo if not hasattr(tempo, "__len__") else tempo[0])


def transcribe_words(audio_path: str) -> tuple[str, list[dict]]:
    """whisperx transcribe + align → [{word,start,end}]. Без диаризации (pyannote не нужен)."""
    import whisperx
    device, compute = "cpu", "int8"
    model = whisperx.load_model("base", device, compute_type=compute)
    audio = whisperx.load_audio(audio_path)
    result = model.transcribe(audio, batch_size=8)
    lang = result.get("language", "en")
    try:
        amodel, meta = whisperx.load_align_model(language_code=lang, device=device)
        aligned = whisperx.align(result["segments"], amodel, meta, audio, device,
                                 return_char_alignments=False)
        segs = aligned["segments"]
    except Exception as e:
        print(f"[vocal_sync] align недоступен для '{lang}' ({e}) — беру сегментные тайминги")
        segs = result["segments"]
    words = []
    for seg in segs:
        for w in seg.get("words", []) or []:
            if w.get("start") is not None:
                words.append({"word": w.get("word", "").strip(),
                              "start": round(float(w["start"]), 3),
                              "end": round(float(w.get("end", w["start"])), 3)})
        if not seg.get("words") and seg.get("start") is not None:  # фолбэк: слово=сегмент
            words.append({"word": seg.get("text", "").strip()[:40],
                          "start": round(float(seg["start"]), 3),
                          "end": round(float(seg.get("end", seg["start"])), 3)})
    return lang, words


def snap_to_beats(words: list[dict], bpm: float, tol_ms: int) -> tuple[list[dict], int]:
    period = 60.0 / bpm
    tol = tol_ms / 1000.0
    locked = 0
    for w in words:
        beat_idx = round(w["start"] / period)
        beat_t = beat_idx * period
        off = w["start"] - beat_t
        w["beat_idx"] = int(beat_idx)
        w["beat_offset"] = round(off, 3)
        w["beat_locked"] = abs(off) <= tol
        if w["beat_locked"]:
            locked += 1
    return words, locked


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--bpm", type=float, default=0.0, help="если 0 — посчитать librosa")
    ap.add_argument("--tol-ms", type=int, default=TOL_MS)
    ap.add_argument("--out", default="vocal_markers.json")
    args = ap.parse_args()

    out = {"lang": None, "bpm": None, "tol_ms": args.tol_ms, "n_words": 0,
           "n_beat_locked": 0, "words": [], "reason": None}
    try:
        bpm = args.bpm or compute_bpm(args.audio)
        out["bpm"] = round(bpm, 1)
        lang, words = transcribe_words(args.audio)
        out["lang"] = lang
        if not words:
            out["reason"] = "нет вокала/слов (инструментал?) — fall back на energy"
            print(f"[vocal_sync] {out['reason']}")
        else:
            words, locked = snap_to_beats(words, bpm, args.tol_ms)
            out.update(n_words=len(words), n_beat_locked=locked, words=words)
            print(f"[vocal_sync] lang={lang} bpm={bpm:.1f} слов={len(words)} "
                  f"на-бите={locked} (±{args.tol_ms}мс)")
    except Exception as e:
        out["reason"] = f"whisperx упал: {e} — fall back на energy (не блокируем пайплайн)"
        print(f"[vocal_sync] {out['reason']}", file=sys.stderr)

    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[vocal_sync] → {args.out}")
    return 0  # всегда 0: не-блокирующий


if __name__ == "__main__":
    sys.exit(main())
