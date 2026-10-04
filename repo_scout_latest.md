# Repo Scout — 2026-10-04T15:10:22.996747
Всего в шортлисте: 2
- **quanluo/awesome-gemini-omni-prompts** ⭐1 [craft]
  - https://github.com/quanluo/awesome-gemini-omni-prompts
  - ⚠️ fallback: query-grounded
  - 🎯 gap `camera_prompt_contracts` (priority 8): Проверяемые операторские словари и prompt-схемы для управляемой i2v-моторики через уже доступные облачные генераторы.
  - 🔎 evidence: query matched: camera movement prompt
  - 🧩 integration cost: low
  - ♻ duplicate-risk: high — fallback candidate; metadata did not contain exact evidence terms; high — camera_moves.json и prompt_writer уже есть; брать только новые проверяемые формулировки
  - 💡 Репозиторий содержит проверенные промпты, формулы движений камеры и рецепты для генерации видео через Gemini Omni AI. Он предлагает проверяемые операторские словари для управляемой i2v-моторики. Интеграция недорогая, но ценность ограничена новыми, уникальными формулировками из-за высокого риска дублирования с уже имеющимися внутренними инструментами.
  - 📄 Curated cinema-grade prompts, camera movement control formulas, and keyframe directing recipes for Gemini Omni AI Video Generator.
- **cyc05/video-ocr-storyboard-skill** ⭐0 [vision]
  - https://github.com/cyc05/video-ocr-storyboard-skill
  - ⚠️ fallback: query-grounded
  - 🎯 gap `preview_verification_contracts` (priority 8): Renderer-agnostic preview evidence: sampled keyframes/contact sheet и machine-readable ошибки поверх существующего рендера.
  - 🔎 evidence: query matched: video visual verification contact sheet
  - 🧩 integration cost: low
  - ♻ duplicate-risk: high — fallback candidate; metadata did not contain exact evidence terms; high — contact sheets и preview receipts уже есть; ценен только единый renderer-agnostic contract
  - 💡 Этот репозиторий извлекает текст из видео и создает контактные листы для визуальной проверки данных. Он закрывает потребность в независимых от рендера доказательствах предпросмотра через семплированные кадры и машиночитаемые ошибки. Интеграция имеет низкую стоимость, но оправдана лишь для получения единого renderer-agnostic контракта, так как функционал контактных листов уже существует.
  - 📄 A Trae/Claude skill for extracting on-screen text from videos without ffmpeg. Per-second frame sampling with OpenCV, batch OCR via RapidOCR (Chinese + English),