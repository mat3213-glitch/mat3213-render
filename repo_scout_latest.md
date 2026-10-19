# Repo Scout — 2026-10-19T16:49:18.878134
Всего в шортлисте: 1
- **AiLaoHuYu/webrtc-recovery** ⭐0 [vision]
  - https://github.com/AiLaoHuYu/webrtc-recovery
  - 🎯 gap `motion_continuity_qc` (priority 9): Детерминированный CPU-QC фризов, дубликатов кадров, оптических разрывов и непрерывности движения.
  - 🔎 evidence: freeze detection
  - 🧩 integration cost: medium
  - ♻ duplicate-risk: medium — scene-count QC существует, но не проверяет органичность моторики
  - 💡 Этот репозиторий обнаруживает зависания в видеопотоках, что критично для детерминированного CPU-контроля качества фризов и непрерывности движения. Несмотря на среднюю стоимость интеграции и риск дублирования, его ценность оправдана, так как он специфически проверяет органичность моторики, что не делают другие QC-инструменты.
  - 📄 Recovery ladder for WebRTC viewers: ICE restart, full rejoin, freeze detection and a plain-English diagnosis. Zero dependencies.