# Repo Scout — 2026-09-25T15:01:04.441155
Всего в шортлисте: 1
- **Sba-Stuff/AI-Local-Video-Generator** ⭐1 [source]
  - https://github.com/Sba-Stuff/AI-Local-Video-Generator
  - ⚠️ fallback: query-grounded
  - 🎯 gap `stock_source_adapters` (priority 7): Надёжные headless-адаптеры легального stock-видео с лицензией, retry и метаданными происхождения.
  - 🔎 evidence: query matched: stock video api
  - 🧩 integration cost: low
  - ♻ duplicate-risk: high — fallback candidate; metadata did not contain exact evidence terms; high — Coverr/Pexels/Wikimedia уже работают; ценен только новый источник или provenance
  - 💡 Этот репозиторий генерирует короткие видео, используя бесплатные API стоковых видео (Pexels, Pixabay) и ffmpeg. Он предоставляет адаптеры для легального стокового видео, частично закрывая потребность в надёжных источниках. Однако, при высоком риске дублирования с уже работающими Pexels и отсутствии уникальных метаданных, его интеграция оправдана лишь как запасной вариант или для Pixabay.
  - 📄 Generate short, narrated videos automatically – powered by a local LLM (LM Studio), free stock video APIs (Pexels + Pixabay), and ffmpeg. No cloud costs, no API