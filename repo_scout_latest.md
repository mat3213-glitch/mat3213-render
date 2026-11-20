# Repo Scout — 2026-11-20T18:21:20.860546
Всего в шортлисте: 2
- **KhusumithVeera/TranscribeAI** ⭐0 [video]
  - https://github.com/KhusumithVeera/TranscribeAI
  - ⚠️ fallback: query-grounded
  - 🎯 gap `timeline_review` (priority 9): Компактное timeline/transcript-представление для дешёвого ревью режиссуры без передачи видео целиком в LLM.
  - 🔎 evidence: query matched: transcript based video editing
  - 🧩 integration cost: low
  - ♻ duplicate-risk: high — fallback candidate; metadata did not contain exact evidence terms; medium — WhisperX и EDL уже есть; ценен только компактный review surface
  - 💡 KhusumithVeera/TranscribeAI — это ИИ-редактор, позволяющий изменять видео через текстовую расшифровку, что удобно для быстрого ревью режиссуры. Он предлагает компактное представление для дешёвого анализа без полной передачи видео в LLM. Хотя риск дублирования функций высок из-за уже используемых WhisperX и EDL, его низкая стоимость интеграции оправдана ценностью именно этого компактного интерфейса ревью.
  - 📄 An AI-powered, transcript-based video editor that lets you edit videos as easily as editing a document. Delete words, remove filler phrases, and automatically e
- **omkarcloud/adobe-stock-scraper** ⭐0 [source]
  - https://github.com/omkarcloud/adobe-stock-scraper
  - ⚠️ fallback: query-grounded
  - 🎯 gap `stock_source_adapters` (priority 7): Надёжные headless-адаптеры легального stock-видео с лицензией, retry и метаданными происхождения.
  - 🔎 evidence: query matched: stock video api
  - 🧩 integration cost: low
  - ♻ duplicate-risk: high — fallback candidate; metadata did not contain exact evidence terms; high — Coverr/Pexels/Wikimedia уже работают; ценен только новый источник или provenance
  - 💡 omkarcloud/adobe-stock-scraper — это бесплатный парсер Adobe Stock, позволяющий получать видео, фото и метаданные в JSON. Он закрывает потребность в новых headless-адаптерах для легального стокового видео, добавляя Adobe Stock как источник. Несмотря на низкую стоимость интеграции, его оправдывает только новый источник контента или метаданные происхождения, поскольку уже используются Coverr/Pexels/Wikimedia, что создаёт высокий риск дублирования.
  - 📄 Adobe Stock Scraper — free, open-source Adobe Stock scraper. Unlimited photos, videos, vectors, keywords & downloads as clean JSON, no API key, no proxies.