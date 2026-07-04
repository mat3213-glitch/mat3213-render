#!/usr/bin/env python3
"""
pool_matcher.py — потребитель ai_pool_catalog.jsonl (см. pool_tagger.py).

Аналог asset_catalog.py::pick()/fetch(), но для AI-пула: вместо точной категории —
fuzzy-скоринг по hero_object архетипа (архетип: "свеча, лёд, туман, стекло с дождём") и
imagery_cues beat'а (director.py: ["крупный план объекта", "туман сквозь щель", ...]),
т.к. теги в каталоге — свободный текст от mimo (project_screenplay_pipeline, развилка C
2026-07-04: climax → scene_dispatch точная генерация, intro/body/outro → этот matcher).

Использование из сборки раскадровки:
    from pool_matcher import pick, fetch
    cand = pick(hero_object=treatment["hero_object"], imagery_cues=beat["imagery_cues"], n=3, seed=track_seed)
    paths = [fetch(e, workdir) for e in cand]

CLI (проверка):
    python3 pool_matcher.py --hero "туман, лёд, свеча" --n 5
    python3 pool_matcher.py --hero "силуэт человека" --cues "тень, окно" --engine veofree --n 3
"""
from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
from pathlib import Path

YD = "ydrive:Content factory"
CATALOG_REL = "cloud_io/ai_pool_catalog.jsonl"
_CACHE: list | None = None


def _rclone(*args, timeout=300):
    return subprocess.run(["rclone", *args], capture_output=True, text=True, timeout=timeout)


def load(force: bool = False) -> list[dict]:
    """ai_pool_catalog.jsonl с ЯД → список записей (кэш в процессе)."""
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE
    r = _rclone("cat", f"{YD}/{CATALOG_REL}")
    out = []
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    else:
        print(f"[pool_matcher] не прочитал {CATALOG_REL}: {r.stderr[:100]}", file=sys.stderr)
    _CACHE = out
    return out


def _hero_terms(hero_object: str) -> list[str]:
    """'свеча, лёд, туман, стекло с дождём' → ['свеча','лёд','туман','стекло с дождём']."""
    parts = re.split(r"[,/]", hero_object or "")
    return [p.strip().lower() for p in parts if p.strip()]


def _cue_words(imagery_cues: list[str] | None) -> set[str]:
    words: set[str] = set()
    for cue in (imagery_cues or []):
        for w in re.split(r"[\s,]+", cue.lower()):
            if len(w.strip()) > 2:
                words.add(w.strip())
    return words


def score_entry(e: dict, hero_terms: list[str], cue_words: set[str]) -> float:
    """hero_object — основной сигнал (вес 2.0/термин, частичное совпадение слов внутри
    многословного термина); imagery_cues — вспомогательный (вес 0.5/слово)."""
    text = (e.get("hero_candidate", "") + " " + " ".join(e.get("tags") or [])).lower()
    score = 0.0
    for term in hero_terms:
        term_words = term.split()
        overlap = sum(1 for w in term_words if w and w in text)
        if overlap:
            score += 2.0 * (overlap / max(1, len(term_words)))
    for w in cue_words:
        if w in text:
            score += 0.5
    return score


def pick(hero_object: str, imagery_cues: list[str] | None = None,
         engine: str | None = None, scale: str | None = None,
         n: int = 1, seed=None, min_score: float = 0.5) -> list[dict]:
    """До n записей, отсортированных по score(hero_object, imagery_cues), с детерминированным
    (seed) шаффлом top-3n кандидатов — чтобы не всегда брать один и тот же самый частый мотив,
    но и не терять релевантность. seed — тот же, что и на весь трек (per-track детерминизм)."""
    items = load()
    hero_terms = _hero_terms(hero_object)
    cue_words = _cue_words(imagery_cues)

    def ok(e: dict) -> bool:
        if engine and e.get("engine") != engine:
            return False
        if scale and e.get("scale") != scale:
            return False
        return True

    scored = [(score_entry(e, hero_terms, cue_words), e) for e in items if ok(e)]
    scored = [(s, e) for s, e in scored if s >= min_score]
    scored.sort(key=lambda x: -x[0])

    pool_top = scored[: max(n * 3, n)]
    rng = random.Random(seed)
    rng.shuffle(pool_top)
    return [e for _, e in pool_top[:n]]


def fetch(entry: dict, dest_dir) -> Path:
    """JIT-скачать клип записи с ЯД в dest_dir → локальный путь."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    local = dest_dir / Path(entry["path"]).name
    if not local.exists():
        r = _rclone("copyto", f"{YD}/{entry['path']}", str(local))
        if r.returncode != 0:
            raise RuntimeError(f"fetch {entry['id']} не вышел: {r.stderr[:120]}")
    return local


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hero", required=True, help="hero_object архетипа, через запятую")
    ap.add_argument("--cues", help="imagery_cues через запятую (опционально)")
    ap.add_argument("--engine", choices=["qwen", "veofree", "hunyuan"])
    ap.add_argument("--scale", choices=["wide", "medium", "macro", "close"])
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--seed")
    ap.add_argument("--fetch", help="скачать выбранное в указанную папку")
    a = ap.parse_args()

    cues = [c.strip() for c in a.cues.split(",")] if a.cues else None
    sel = pick(hero_object=a.hero, imagery_cues=cues, engine=a.engine, scale=a.scale,
               n=a.n, seed=a.seed)
    print(f"hero_object={a.hero!r} cues={cues} → найдено {len(sel)}:")
    for e in sel:
        print(f"  {e['id']} | hero_candidate={e.get('hero_candidate')!r} | "
              f"tags={e.get('tags')} | engine={e.get('engine')} scale={e.get('scale')}")
    if a.fetch:
        for e in sel:
            p = fetch(e, a.fetch)
            print(f"  ↓ {p}")


if __name__ == "__main__":
    main()
