# Repo Scout — 2026-10-28T16:27:18.782661
Всего в шортлисте: 1
- **uGmTEAM/video-cli** ⭐0 [video]
  - https://github.com/uGmTEAM/video-cli
  - ⚠️ fallback: query-grounded
  - 🎯 gap `organic_transitions` (priority 10): Органичные стыки поверх уже работающего FFmpeg xfade без дешёвого цифрового glitch-визуала.
  - 🔎 evidence: query matched: ffmpeg xfade transition
  - 🧩 integration cost: low
  - ♻ duplicate-risk: high — fallback candidate; metadata did not contain exact evidence terms; medium — базовые xfade и transition-router уже есть; нужен только новый органичный primitive
  - 💡 uGmTEAM/video-cli — это Docker-инструмент для синтеза видео на базе FFmpeg и Python, предлагающий 46 xfade переходов. Он потенциально закрывает потребность в органичных стыках, расширяя текущие возможности FFmpeg xfade за счёт большего выбора. Низкая стоимость интеграции оправдана для поиска нужных органичных примитивов, несмотря на средний риск дублирования базовых функций.
  - 📄 Docker CLI video synthesis tool — ffmpeg 5.1.9 + Python 3.12, 46 xfade transitions, multi-format output, resume recovery