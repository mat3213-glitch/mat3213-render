# Repo Scout — 2026-09-30T16:16:47.862372
Всего в шортлисте: 2
- **raphaelguilhem-dot/fashion-camera-movements** ⭐0 [craft]
  - https://github.com/raphaelguilhem-dot/fashion-camera-movements
  - ⚠️ fallback: query-grounded
  - 🎯 gap `camera_prompt_contracts` (priority 8): Проверяемые операторские словари и prompt-схемы для управляемой i2v-моторики через уже доступные облачные генераторы.
  - 🔎 evidence: query matched: camera movement prompt
  - 🧩 integration cost: low
  - ♻ duplicate-risk: high — fallback candidate; metadata did not contain exact evidence terms; high — camera_moves.json и prompt_writer уже есть; брать только новые проверяемые формулировки
  - 💡 Этот репозиторий содержит промпты для управления движением камеры в AI-видео, что помогает создать проверяемые операторские словари и схемы для i2v-генерации. Интеграция недорогая, но из-за высокого риска дублирования брать стоит только новые, уникальные формулировки, дополняющие уже существующие инструменты.
  - 📄 Pletor Camera Movement Library: camera movement prompts for AI fashion videos
- **obvirm/AutoTimingSFX** ⭐0 [video]
  - https://github.com/obvirm/AutoTimingSFX
  - ⚠️ fallback: query-grounded
  - 🎯 gap `render_reproducibility` (priority 6): Усиление воспроизводимости CPU/GitHub Actions рендера: manifests, hashes, resumable chunks и проверяемые receipts.
  - 🔎 evidence: query matched: render manifest ffmpeg
  - 🧩 integration cost: low
  - ♻ duplicate-risk: high — fallback candidate; metadata did not contain exact evidence terms; high — chunk cache и Video Receipt частично внедрены; нужен конкретный незакрытый guardrail
  - 💡 Этот репозиторий использует ИИ для определения таймингов SFX и создания манифестов для рендера через FFmpeg. Он способствует повышению воспроизводимости рендера на CPU/GitHub Actions за счет использования манифестов. Интеграция недорогая, но оправдана только для закрытия конкретных, еще не реализованных механизмов контроля, так как часть функций уже есть.
  - 📄 AI timing picker untuk SFX library sendiri: video -> VLM (video+audio) -> manifest beat -> picker RAG -> render FFmpeg