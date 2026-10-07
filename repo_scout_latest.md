# Repo Scout — 2026-10-07T17:18:25.286217
Всего в шортлисте: 1
- **brightsign/cerberus-anomoly-detection-extension** ⭐0 [vision]
  - https://github.com/brightsign/cerberus-anomoly-detection-extension
  - ⚠️ fallback: query-grounded
  - 🎯 gap `motion_continuity_qc` (priority 9): Детерминированный CPU-QC фризов, дубликатов кадров, оптических разрывов и непрерывности движения.
  - 🔎 evidence: query matched: freeze detection video
  - 🧩 integration cost: medium
  - ♻ duplicate-risk: high — fallback candidate; metadata did not contain exact evidence terms; medium — scene-count QC существует, но не проверяет органичность моторики
  - 💡 Этот репозиторий позволяет обнаруживать фризы, заикания и сбои воспроизведения в видео, что напрямую закрывает потребность в детерминированном контроле качества непрерывности движения и фризов в сгенерированном контенте yaromat. Несмотря на высокую вероятность дублирования функционала другими инструментами, его способность проверять органичность моторики видео оправдывает среднюю стоимость интеграции.
  - 📄 NPU-accelerated video wall monitoring on BrightSign device using camera-based ROI extraction and MobileNetV3 embedding reference matching to detect black screen