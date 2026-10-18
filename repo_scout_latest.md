# Repo Scout — 2026-10-18T13:47:06.390271
Всего в шортлисте: 1
- **tecnomanu/motion-video-styles** ⭐2 [video]
  - https://github.com/tecnomanu/motion-video-styles
  - ⚠️ fallback: query-grounded
  - 🎯 gap `render_reproducibility` (priority 6): Усиление воспроизводимости CPU/GitHub Actions рендера: manifests, hashes, resumable chunks и проверяемые receipts.
  - 🔎 evidence: query matched: deterministic video render
  - 🧩 integration cost: low
  - ♻ duplicate-risk: high — fallback candidate; metadata did not contain exact evidence terms; high — chunk cache и Video Receipt частично внедрены; нужен конкретный незакрытый guardrail
  - 💡 Репозиторий генерирует анимированные видео с детерминированным рендером, что напрямую способствует усилению воспроизводимости рендера на CPU/GitHub Actions. Хотя он частично внедряет кэширование и квитанции, риск дублирования высок, поскольку решение не полностью покрывает все аспекты требуемых манифестов и хешей, несмотря на низкую стоимость интеграции.
  - 📄 Agent skill: animated vertical/horizontal videos (reels, shorts) in 6 styles for any brand — HTML+GSAP scenes synced to voice, karaoke captions, deterministic r