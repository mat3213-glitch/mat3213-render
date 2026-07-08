# Оверлей-хуки — конвенция апрува (ЧИТАЙ ПЕРЕД ИСПОЛЬЗОВАНИЕМ)

Каждый оверлей `<Name>.tsx` ОБЯЗАН иметь рядом `<Name>.md` с frontmatter-полем `approved:`.

```
---
component: <Name>
type: overlay
approved: yes | no
---
```

**Ранер (`tsx_overlay_job.py`) читает `<Name>.md` ПЕРЕД тем как взять хук:**
- нет README → отказ (хук без апрува использовать нельзя);
- `approved: no` → отказ для прод/пул-использования. Для ревью-рендера превью можно
  поставить `"allow_unapproved": true` в job.json (caption пометит «⚠ НЕ ПРОД-АПРУВ»);
- `approved: yes` → можно брать в пул/прод.

## ✅ Баг композита ИСПРАВЛЕН (2026-06-15, подтв. 2026-07-08 на MobyTitle)
Ранее альфа терялась на ffmpeg-шаге. Фикс: `remotion render` с `--image-format=png
--pixel-format=yuva444p10le` (ProRes 4444 несёт альфа-плоскость) + ffprobe-диагностика
pix_fmt. Композит кладёт хук ПОВЕРХ живого кадра. `tsx_overlay_job.py` также скейлит
оверлей под размер базы (1080×1920 canvas → 720×1280 i2v-база, тот же 9:16).

## Текущий статус
| хук | approved | прим. |
|-----|----------|-------|
| MobyTitle | **yes** | liner-note титр (регистр раннего Moby); заякорен на реф → не дёшево; Skills-assisted, гейт пройден 2026-07-08 |
| FocusBracket | no | ядро движка; тех. работает, прод-апрув не дан |
| ShapeWipe | no | mimo; сочинён с нуля → дёшево |
| AccentBurst | no | mimo; сочинён с нуля → дёшево |
| BeatPulse | no | mimo; сочинён с нуля → дёшево |
| InnerScratch | no | mimo; штрихи невзрачны |
