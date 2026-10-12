# Repo Scout — 2026-10-12T19:38:55.707987
Всего в шортлисте: 2
- **azianmike/monthly-winner-research** ⭐0 [source]
  - https://github.com/azianmike/monthly-winner-research
  - ⚠️ fallback: query-grounded
  - 🎯 gap `stock_source_adapters` (priority 7): Надёжные headless-адаптеры легального stock-видео с лицензией, retry и метаданными происхождения.
  - 🔎 evidence: query matched: stock video api
  - 🧩 integration cost: low
  - ♻ duplicate-risk: high — fallback candidate; metadata did not contain exact evidence terms; high — Coverr/Pexels/Wikimedia уже работают; ценен только новый источник или provenance
  - 💡 Репозиторий описывает бэктест для выявления ежемесячных победителей на фондовом рынке, включая интерактивный отчёт. Несмотря на это, он был отмечен как потенциальный источник для адаптеров стокового видео, что могло бы закрыть потребность в надёжных headless-адаптерах. Однако, риск дублирования высок, и его ценность сомнительна без явного подтверждения нового источника или метаданных происхождения.
  - 📄 An audited monthly stock-winner backtest: interactive report, data, reproducible Python, Agents API workflow, and 30-second video.
- **Prithpal-Sooriya/tiny-video-renderer** ⭐0 [video]
  - https://github.com/Prithpal-Sooriya/tiny-video-renderer
  - ⚠️ fallback: query-grounded
  - 🎯 gap `render_reproducibility` (priority 6): Усиление воспроизводимости CPU/GitHub Actions рендера: manifests, hashes, resumable chunks и проверяемые receipts.
  - 🔎 evidence: query matched: deterministic video render
  - 🧩 integration cost: low
  - ♻ duplicate-risk: high — fallback candidate; metadata did not contain exact evidence terms; high — chunk cache и Video Receipt частично внедрены; нужен конкретный незакрытый guardrail
  - 💡 Репозиторий предлагает компактный рендерер видео на TypeScript, который генерирует детерминированный MP4-файл из сцен. Это напрямую способствует усилению воспроизводимости рендера на CPU/GitHub Actions, что соответствует указанному gap. Однако, несмотря на низкую стоимость интеграции, риск дублирования высок, так как часть функционала уже внедрена, и нужен конкретный незакрытый guardrail.
  - 📄 A one-file, zero-dependency video renderer small enough to embed in an agent skill: TypeScript scenes in, deterministic MP4 out.