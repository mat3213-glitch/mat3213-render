#!/usr/bin/env python3
"""
scout_filters.py — общий фильтр насыщенных тем + гейт новизны для скаутов (07.08.2026).

Причина появления (замер 07.08, жалоба yaromat «скауты опять тащат одно и то же»):
- repo_scout: 21 из 25 позиций шортлиста = категория orchestration, 23 из 25 блёрбов сам скаут
  пишет «вряд ли пригодится», пересечение с прошлым прогоном 7–14 из 16–25. Дедуп при этом
  ИСПРАВЕН (seen.json, 183 имени) — ломается не он, а СОСТАВ: «новое» каждый день это новый
  агентный фреймворк.
- grok-скаут: половина кандидатов каждый день — «бесплатные LLM-шлюзы»; freellmpool и
  free-llm-proxy-router стоят буквально в двух днях из четырёх.

Отсюда два разных лекарства, и оба нужны:
  1. SATURATED — классы, которыми проект УЖЕ закрыт (свой пул воркеров, свой оркестратор).
     Репо этого класса не «плохое», оно просто не наша задача, а места занимает все.
  2. NoveltyIndex — «то же самое другими словами». Jaccard по символьным 3-граммам: без моделей,
     без платных API, миллисекунды на строку. sentence-transformers сознательно НЕ берём —
     вердикт grok'а: тащить модель ради сравнения коротких строк незачем.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# Классы, где проект уже самодостаточен: свой пул воркеров (kimi/openrouter/qwen/grok),
# свой fanout-оркестратор, своя память. Найденное здесь ничего не меняет — только занимает слот.
SATURATED = [
    "llm gateway", "llm-gateway", "llm proxy", "llm-proxy", "free llm", "free-llm",
    "openai compatible", "openai-compatible", "api aggregator", "model router",
    # 🔴 «free tier» ВЫНУТ 07.08: с новым профилем грока промо-периоды и бесплатные тиры —
    # ЦЕЛЕВАЯ находка, а не мусор. Замер на истории: без этого терма шлюзы ловятся ровно так же
    # (37 из 55), значит он ничего не держал, зато мог срезать легитимную акцию без крафт-слова.
    "inference router", "inference endpoint", "api key pool",
    "coding agent", "agent framework", "multi-agent", "agentic framework",
    "agent orchestration", "autonomous agent", "ai assistant", "chatbot",
    "mcp server", "rag framework", "vector database", "prompt management",
    "pentest", "vulnerability", "exploit", "red team",
]

# Наше ремесло: ради чего скаут вообще существует. Пересечение с SATURATED разрешает находку —
# «audio-reactive agent» нам интереснее, чем очередной роутер.
CRAFT = [
    # 🔴 Широкие корни стоят ПЕРВЫМИ и сознательно: на замере 07.08 фильтр выбросил
    # browser-use/video-use («Edit videos with coding agents») — ровно то репо, которое yaromat
    # в тот день репостнул руками. Узкие пары вроде «video edit» не ловят «edit videos».
    # Правило: слово о видео/звуке/монтаже в описании ПЕРЕВЕШИВАЕТ слово об агентах.
    "video", "clip", "footage", "montage", "audio", "music", "render",
    "ffmpeg", "video edit", "video editing", "cut detection", "scene detect",
    "shot boundary", "transition", "motion graphics", "remotion", "after effects",
    "color grade", "color grading", "lut", "film emulation", "grain", "halation",
    "light leak", "vignette", "datamosh", "glitch art", "vhs",
    "audio reactive", "audio-reactive", "music visualizer", "waveform", "spectrogram",
    "beat detect", "onset", "tempo", "bpm", "stem separation", "demucs", "librosa",
    "optical flow", "frame interpolation", "slow motion", "stabiliz",
    "image to video", "text to video", "i2v", "t2v", "video generation",
    "stock footage", "stock video", "royalty free", "creative commons",
    "aesthetic", "quality assessment", "watermark", "ocr", "clip embedding",
    "shader", "glsl", "parallax", "depth map", "compositing", "keyframe",
    "subtitle", "caption", "whisper",
]

_SAT_RE = re.compile("|".join(re.escape(t) for t in SATURATED))
_CRAFT_RE = re.compile("|".join(re.escape(t) for t in CRAFT))


def is_saturated(text: str) -> bool:
    """True — тема, которой проект уже закрыт, И без единого признака нашего ремесла.
    Крафтовый признак ПЕРЕВЕШИВАЕТ: «audio-reactive agent» пропускаем, «agent framework» нет."""
    t = (text or "").lower()
    if not t:
        return False
    return bool(_SAT_RE.search(t)) and not _CRAFT_RE.search(t)


def is_craft(text: str) -> bool:
    return bool(_CRAFT_RE.search((text or "").lower()))


def _norm(s: str) -> str:
    return re.sub(r"[^a-zа-я0-9 ]+", " ", (s or "").lower())


def trigrams(s: str) -> set[str]:
    t = re.sub(r"\s+", " ", _norm(s)).strip()
    return {t[i:i + 3] for i in range(len(t) - 2)} if len(t) >= 3 else set()


def similarity(a: str, b: str) -> float:
    """Jaccard по символьным 3-граммам: 1.0 = тот же текст, ~0 = ничего общего."""
    ta, tb = trigrams(a), trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


class NoveltyIndex:
    """История уже показанных находок (текст + ключ). Держит последние `limit` записей.

    Порог 0.55 взят по замеру на нашей истории: пары «то же самое другими словами»
    (free-llm-proxy-router ↔ free-llm-gateway) дают 0.55–0.75, разные темы — ниже 0.35.
    """

    def __init__(self, path: Path, limit: int = 400, threshold: float = 0.55):
        self.path = Path(path)
        self.limit = limit
        self.threshold = threshold
        self.items: list[dict] = []
        if self.path.exists():
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(d, list):
                    self.items = [x for x in d if isinstance(x, dict) and x.get("text")]
            except Exception:
                self.items = []
        self._grams = [(x, trigrams(x["text"])) for x in self.items]

    def max_similarity(self, text: str) -> tuple[float, str]:
        ta = trigrams(text)
        if not ta:
            return 0.0, ""
        best, who = 0.0, ""
        for item, tb in self._grams:
            if not tb:
                continue
            s = len(ta & tb) / len(ta | tb)
            if s > best:
                best, who = s, item.get("key", "")
        return best, who

    def is_novel(self, text: str) -> tuple[bool, float, str]:
        sim, who = self.max_similarity(text)
        return sim < self.threshold, sim, who

    def add(self, key: str, text: str):
        self.items.append({"key": key, "text": text})
        self._grams.append(({"key": key, "text": text}, trigrams(text)))

    def save(self):
        self.items = self.items[-self.limit:]
        self.path.write_text(json.dumps(self.items, ensure_ascii=False, indent=1), encoding="utf-8")
