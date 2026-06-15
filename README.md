# github_actions_clips — рендер и сбор контента на GitHub Actions

Локальный чекаут публичного репо **`mat3213-glitch/mat3213-render`**. Сюда вынесена вся
тяжёлая работа (рендер, фетч, AI-генерация), которую нельзя гонять на буке (Atom, 1.8 GB RAM).
Принцип: **бук = дирижёр, тяжёлое = на GH Actions** ([[feedback_render_on_gh_actions]]).

Воркфлоу запускаются через `workflow_dispatch` (вручную / через REST API с токеном из git-remote).
Результаты складываются на Яндекс.Диск (`ydrive:` rclone, секреты `YDRIVE_*`). Уведомления в ТГ
через CF Worker (`CLOUDFLARE_WORKER` + `TELEGRAM_BOT_TOKEN`).

## Конвенция job'ов

Многие рендер-воркфлоу читают задание с ЯД: `Content factory/render_jobs/<JOB_ID>/`
с `job.json` (+ `track.mp3`, `src_NN.mp4`). Результат и `status.txt` пишутся туда же.

```jsonc
{ "duration": 119, "format": "landscape|square|vertical",
  "out_name": "result.mp4", "sources": ["src_01", ...], "seed": 33 }
```

---

## 1. Музыкальный клип — основной пайплайн

| Скрипт | Workflow | Назначение |
|---|---|---|
| `analyze.py` | — (библиотека) | librosa/aubio: BPM + энерго-сегментация трека |
| `clip_producer_job.py` | `clip_producer.yml` | CLIP_PRODUCER: энергонарезка футажа под трек, source→segment рандом (детерминирован seed) |
| **`finish_job.py`** ⭐ | **`finish_clip.yml`** | **FINISH-рендер: тот же кат + посегментная уникализация (зеркало/инверсия/зум) + грейд + грязь (grit/scratch/noise) + тайтл-карта.** job.json блок `finish` |
| `render_full_job.py` | `render_full.yml` | FULL_RENDER пайплайн (полнометражный клип) |
| `FULL_RENDER.py` | — (библиотека) | production-пайплайн полного клипа |
| `render_job.py` | `render_blend.yml` | BLEND-рендер (double-exposure, [[project_ffmpeg_blend]]) |
| **`screenwriter.py`** ⭐ | — (CLI/шаг) | **агент-сценарист: бриф трека → драматургический `treatment.json`** (логлайн, мотив-метафора, beats по структуре). Free-LLM (Groq→Gemini). Драма без лиц. Скилл `skills/craft/screenwriter/`. Режиссёр-раскадровщик (treatment→кадры) — следующий шаг |

## 2. Тизеры и форматные клипы

| Скрипт | Workflow | Назначение |
|---|---|---|
| `image_teaser_job.py` | `image_teaser.yml` | статичный арт → тизер-клип |
| `vinyl_job.py` | `vinyl_teaser.yml` | крутящаяся пластинка-тизер |
| `vinyl_viral_job.py` | `vinyl_viral.yml` | viral vinyl-сниппет (3 режима фона) |
| `vzrosly_clip_job.py` | `vzrosly_clip.yml` | биполярный коллаж-тизер по cli-флагам ([[project_vzrosly_seed]]) |
| `render_pack.py` / `pack_render.py` | `render_pack.yml` / `pack_render.yml` | рендер-пак под тренд: CC-картинки → видео-тест |

## 3. Сбор медиа (источники)

| Скрипт | Workflow | Назначение |
|---|---|---|
| `download_clips.py` | `download_clips.yml` | видео с Wikimedia Commons → ЯД |
| `fetch_runner.py` | `fetch_media.yml` | runner-side фетчер (US-IP снимает RU-блок Pexels/Pinterest-пинов) |
| **`pinterest_overlay_fetch.py`** ⭐ | **`pinterest_overlay_fetch.yml`** | **анонимный (БЕЗ кук) фетч филмик-оверлеев по поиску Pinterest → ЯД `overlay_assets/pinterest/`, ТГ-тред 228. Вежливый темп (паузы до ~60с), без риска бана акка** ([[project_pinterest_overlay_fetch]]) |

## 4. AI-генерация

| Скрипт | Workflow | Назначение |
|---|---|---|
| `img_gen_job.py` / `gen_cf_image.py` | `img_gen.yml` | пакетная генерация картинок через CF Workers AI (flux/SDXL) |
| `veofree_gen.py` | `veofree_gen.yml` | VeoFree (Seedance) t2v — 1 генерация/прогон (свежий IP раннера) |
| `veofree_i2v_gen.py` | `veofree_i2v_gen.yml` | VeoFree i2v — видео из фото, 1/прогон |

## 5. TSX / Remotion-движок (моторика)

| Скрипт | Workflow | Назначение |
|---|---|---|
| `tsx_clip_job.py` | `tsx_clip.yml` | боевой клип через TSX/Remotion ([[project_tsx_engine]]) |
| `tsx_overlay_job.py` | `tsx_overlay.yml` | TSX-оверлей (графика) поверх видео, альфа yuva444p10le |
| `tsx_scout_merge.py` | — | мердж одобренных TSX-кандидатов из `tsx_proposals.json` |
| — | `tsx_sandbox.yml` | песочница TSX-шаблонов |

## 6. Агенты разнообразия / насмотренности

| Скрипт | Workflow | Назначение |
|---|---|---|
| `style_scout.py` | `style_scout.yml` | Style Scout: Openverse → грейд-кандидаты (Фаза 3 разнообразия пула) |
| `style_judge.py` | — | mimo как глаз+судья цвето-кандидатов (Фаза 1) |
| `style_scout_merge.py` | `style_merge.yml` | мердж одобренных кандидатов из `style_proposals.json` |
| `trend_merge.py` | `trend_merge.yml` | применяет одобренные тренд-параметры |

## 7. Наблюдатели и mimo

| Скрипт | Workflow | Назначение |
|---|---|---|
| `repo_scout.py` | `repo_scout.yml` | еженедельный наблюдатель GitHub → дайджест в тему GITHAB (thread 468) |
| — | `mimo_probe.yml` | mimo probe (узкая генерация/дебаг по спеке) |

---

## Данные и ассеты (в репо)

- `assets/` — `grit_overlay.mp4`, `scratch_overlay.mp4` (грязь/зерно для финиша), `Caveat.ttf`
- `packs/`, `remotion/` — рендер-паки и TSX-проект
- `styles.json`, `*_proposals.json`, `*_seen.json` — состояние агентов (кандидаты/дедуп)

## Секреты (GitHub repo secrets)

`YDRIVE_CLIENT_ID` / `YDRIVE_CLIENT_SECRET` / `YDRIVE_TOKEN` — rclone на ЯД ·
`CLOUDFLARE_WORKER` / `TELEGRAM_BOT_TOKEN` — ТГ-уведомления · `GROQ_API_KEY` / `GEMINI_API_KEY` —
free-мозги для scout-агентов. **Ключей в коде нет** (репо публичный) — только в GH Secrets.

## Запуск воркфлоу (без gh CLI)

```bash
TOKEN=$(git remote get-url origin | sed -E 's#https://([^@]+)@.*#\1#')
curl -s -X POST -H "Authorization: token $TOKEN" -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/mat3213-glitch/mat3213-render/actions/workflows/<file>.yml/dispatches" \
  -d '{"ref":"main","inputs":{...}}'
```
