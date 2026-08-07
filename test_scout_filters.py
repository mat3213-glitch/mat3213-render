#!/usr/bin/env python3
"""Регресс фильтров скаутов (07.08.2026). Запуск: python3 test_scout_filters.py

Закрепляет два урока замера, а не «проверяет функции вообще»:
  1. Крафтовое слово ПЕРЕВЕШИВАЕТ агентное. Первая версия фильтра выбросила
     browser-use/video-use — то самое репо, которое yaromat в тот день репостнул руками.
  2. Порог новизны 0.5 разделяет «то же другими словами» (0.53–0.82 на нашей истории)
     и просто разные темы (<0.35).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scout_filters import NoveltyIndex, is_saturated, similarity

FAIL = []


def check(cond, msg):
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        FAIL.append(msg)


print("— насыщенные темы отсекаются:")
for t in ["freellmpool — free LLM proxy pool, OpenAI compatible",
          "free-llm-gateway (MrFadiAi) inference router",
          "VulnClaw — AI agent for automated pentest",
          "opencode — the open source coding agent"]:
    check(is_saturated(t), f"отсеяно: {t[:48]}")

print("— наше ремесло НЕ отсекается, даже если про агентов:")
for t in ["browser-use/video-use — Edit videos with coding agents",
          "VCR — headless motion graphics renderer, YAML to video",
          "xfade-easing — ffmpeg transition easing",
          "agent that does audio-reactive music visualizer"]:
    check(not is_saturated(t), f"сохранено: {t[:48]}")

print("— новизна:")
check(similarity("freellmpool free llm proxy pool", "freellmpool free llm proxy pool") > 0.95,
      "тот же текст ≈ 1.0")
check(0.5 <= similarity("freellm.net + awesome-freellm-apis daily live catalog",
                        "awesome-free-ai-api + freellm.net daily live") <= 1.0,
      "перефразированный дубль ≥ порога 0.5")
check(similarity("ffmpeg xfade easing transitions", "free llm gateway router") < 0.35,
      "разные темы < 0.35")

idx = NoveltyIndex(Path("/tmp/_nov_test.json"), threshold=0.5)
idx.add("a/b", "ffmpeg film grain overlay generator")
ok, sim, _ = idx.is_novel("ffmpeg film grain overlay generator")
check(not ok, f"повтор пойман гейтом (sim={sim:.2f})")
ok2, sim2, _ = idx.is_novel("demucs stem separation cli")
check(ok2, f"другая тема проходит (sim={sim2:.2f})")
Path("/tmp/_nov_test.json").unlink(missing_ok=True)

print()
print(f"ИТОГ: {'ВСЁ ЗЕЛЁНОЕ' if not FAIL else str(len(FAIL)) + ' ПРОВАЛОВ'}")
sys.exit(1 if FAIL else 0)
