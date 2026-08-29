# Repo Scout — 2026-08-29T14:57:09.211414
Всего в шортлисте: 2
- **artnebo/cinematic-frame-director** ⭐2 [craft]
  - https://github.com/artnebo/cinematic-frame-director
  - ⚠️ fallback: query-grounded
  - 🎯 gap `camera_prompt_contracts` (priority 8): Проверяемые операторские словари и prompt-схемы для управляемой i2v-моторики через уже доступные облачные генераторы.
  - 🔎 evidence: query matched: cinematography prompt
  - 🧩 integration cost: low
  - ♻ duplicate-risk: high — fallback candidate; metadata did not contain exact evidence terms; high — camera_moves.json и prompt_writer уже есть; брать только новые проверяемые формулировки
  - 💡 Этот репозиторий генерирует качественные кинематографические промты для изображений, описывая сцену, ракурс и освещение. Он закрывает потребность в проверяемых операторских словарях и prompt-схемах для управляемой i2v-моторики через облачные генераторы. Интеграция оправдана только для получения новых, уникальных формулировок, так как это запасной вариант и часть функционала уже существует.
  - 📄 Claude Code skill: turn any idea into ONE production-grade cinematic image prompt — decisive moment, mise-en-scène, motivated camera, 60:30:10 lighting
- **mecrimino/deepvideo** ⭐5 [video]
  - https://github.com/mecrimino/deepvideo
  - ⚠️ fallback: query-grounded
  - 🎯 gap `render_reproducibility` (priority 6): Усиление воспроизводимости CPU/GitHub Actions рендера: manifests, hashes, resumable chunks и проверяемые receipts.
  - 🔎 evidence: query matched: deterministic video render
  - 🧩 integration cost: low
  - ♻ duplicate-risk: high — fallback candidate; metadata did not contain exact evidence terms; high — chunk cache и Video Receipt частично внедрены; нужен конкретный незакрытый guardrail
  - 💡 Данный AI-видеоредактор превращает идеи в редактируемые таймлайны и использует FFmpeg для рендеринга, поддерживая детерминированные резервы. Он может усилить воспроизводимость рендера на CPU/GitHub Actions за счёт манифестов и чеков. Интеграция оправдана, если репозиторий закрывает конкретные, ещё не реализованные аспекты воспроизводимости, так как часть функций уже внедрена.
  - 📄 AI video editor with an agentic core — turns a script or idea into an editable timeline, then edits it by chat. Runs keyless (degrades to deterministic fallback