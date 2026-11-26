# Repo Scout — 2026-11-26T19:26:00.821414
Всего в шортлисте: 1
- **voyager67/cliprally** ⭐1 [video]
  - https://github.com/voyager67/cliprally
  - ⚠️ fallback: query-grounded
  - 🎯 gap `render_reproducibility` (priority 6): Усиление воспроизводимости CPU/GitHub Actions рендера: manifests, hashes, resumable chunks и проверяемые receipts.
  - 🔎 evidence: query matched: render manifest ffmpeg
  - 🧩 integration cost: low
  - ♻ duplicate-risk: high — fallback candidate; metadata did not contain exact evidence terms; high — chunk cache и Video Receipt частично внедрены; нужен конкретный незакрытый guardrail
  - 💡 ClipRally нарезает длинные видео на вертикальные клипы с субтитрами, используя FFmpeg для рендеринга. Он усиливает воспроизводимость рендера на CPU/GitHub Actions благодаря работе с манифестами и FFmpeg. Несмотря на низкую стоимость интеграции, риск дублирования функционала высок, так как схожие решения уже частично внедрены.
  - 📄 Turn long videos into captioned vertical clips. ClipRally is a local-first AI video clipper for streams, gameplay, podcasts and recordings—with speech transcrip