# Repo Scout — 2026-11-21T17:08:05.677988
Всего в шортлисте: 1
- **K9i-0/kasaneru** ⭐0 [video]
  - https://github.com/K9i-0/kasaneru
  - ⚠️ fallback: query-grounded
  - 🎯 gap `render_reproducibility` (priority 6): Усиление воспроизводимости CPU/GitHub Actions рендера: manifests, hashes, resumable chunks и проверяемые receipts.
  - 🔎 evidence: query matched: render manifest ffmpeg
  - 🧩 integration cost: low
  - ♻ duplicate-risk: high — fallback candidate; metadata did not contain exact evidence terms; high — chunk cache и Video Receipt частично внедрены; нужен конкретный незакрытый guardrail
  - 💡 Репозиторий K9i-0/kasaneru рендерит видео из JSON-манифестов с использованием ffmpeg. Это помогает усилить воспроизводимость рендера на CPU/GitHub Actions, закрывая потребность в использовании манифестов. Хотя стоимость интеграции низка, есть высокий риск дублирования, и он рассматривается как запасной вариант, так как некоторые функции (кеш чанков, квитанции) уже частично внедрены.
  - 📄 Render video from a JSON manifest with ffmpeg and a browser or Flutter rendering backend