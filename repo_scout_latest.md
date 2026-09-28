# Repo Scout — 2026-09-28T17:58:35.352763
Всего в шортлисте: 2
- **karlagli791/cappycat** ⭐0 [vision]
  - https://github.com/karlagli791/cappycat
  - ⚠️ fallback: query-grounded
  - 🎯 gap `motion_continuity_qc` (priority 9): Детерминированный CPU-QC фризов, дубликатов кадров, оптических разрывов и непрерывности движения.
  - 🔎 evidence: query matched: duplicate frame detection video
  - 🧩 integration cost: medium
  - ♻ duplicate-risk: high — fallback candidate; metadata did not contain exact evidence terms; medium — scene-count QC существует, но не проверяет органичность моторики
  - 💡 Cappycat обнаруживает дубликаты кадров в видео, что помогает закрыть часть задачи по детерминированному CPU-контролю качества видеоконтента, связанную с дубликатами кадров. Хотя интеграция имеет среднюю стоимость и высокий риск дублирования функционала, она может служить запасным вариантом для обнаружения таких проблем.
  - 📄 Local automated AI video editor: cut detection, duplicate-character removal by smart reframing, cast identification, voice separation, CapCut-style timeline and
- **alexgreensh/anidoodle** ⭐649 [video]
  - https://github.com/alexgreensh/anidoodle
  - ⚠️ fallback: query-grounded
  - 🎯 gap `render_reproducibility` (priority 6): Усиление воспроизводимости CPU/GitHub Actions рендера: manifests, hashes, resumable chunks и проверяемые receipts.
  - 🔎 evidence: query matched: deterministic video render
  - 🧩 integration cost: low
  - ♻ duplicate-risk: high — fallback candidate; metadata did not contain exact evidence terms; high — chunk cache и Video Receipt частично внедрены; нужен конкретный незакрытый guardrail
  - 💡 Anidoodle обеспечивает детерминированный рендеринг видео, что способствует усилению воспроизводимости рендера на CPU и GitHub Actions. Несмотря на низкую стоимость интеграции, риск дублирования функционала высок, так как кэш чанков и Video Receipt уже частично внедрены в проект, что требует уточнения конкретного незакрытого аспекта.
  - 📄 Art and animation, written as code. Illustrations, loops, interactive web art, launch-videos and scored films in dozens of styles, identical on every render.