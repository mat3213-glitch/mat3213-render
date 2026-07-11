#!/usr/bin/env python3
"""
storyboard_render_job.py — GitHub Actions рендер по РАСКАДРОВКЕ режиссёра (director.py).

Вшивает storyboard.json в рендер: каждый кадр = base-клип каталога (cover под формат,
trim/loop под t_dur) + overlay-клип каталога (screen-бленд). Кадры конкатятся в порядке
раскадровки, под них кладётся аудио-окно трека на дропе (highlight — интро пропускается).

Источник моторики — сам футаж (винил крутится, волна движется, оверлей течёт) =
фотографичное органичное движение, не синтетика.

Вход (ЯД render_jobs/<JOB_ID>/): storyboard.json, track.mp3.
Клипы каталога тянутся по их path ("footage_catalog/<cat>/ref_*.mp4") прямо с ЯД.
Выход: result.mp4 + status.txt → ЯД render_jobs/<JOB_ID>/.

Environment: JOB_ID
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "screenplay_pipeline"))
import transition_router as _tr        # L6: выбор приёма стыка
import transition_render as _trn       # L6: xfade-цепочка с сохранением тайминга

JOB_ID = os.environ.get("JOB_ID", "")
if not JOB_ID:
    sys.exit("JOB_ID not set")

REMOTE   = "ydrive"
CF       = "Content factory"
JOB_YD   = f"{CF}/cloud_io/render_jobs/{JOB_ID}"
WORKDIR  = Path("/tmp/sb_job")
CLIPS    = WORKDIR / "clips"
SHOTS    = WORKDIR / "shots"
for d in (WORKDIR, CLIPS, SHOTS):
    d.mkdir(parents=True, exist_ok=True)

COVER = {
    "vertical":  "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
    "square":    "scale=1080:1080:force_original_aspect_ratio=increase,crop=1080:1080",
    "landscape": "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080",
}
OVERLAY_OPACITY = 0.45


def yd_get(remote_path: str, local: Path) -> bool:
    local.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["rclone", "copyto", f"{REMOTE}:{remote_path}", str(local)],
                       capture_output=True, text=True)
    return r.returncode == 0


def yd_put(local: Path, remote_path: str) -> bool:
    r = subprocess.run(["rclone", "copyto", str(local), f"{REMOTE}:{remote_path}"],
                       capture_output=True, text=True)
    return r.returncode == 0


def ff(args: list[str]) -> bool:
    r = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  ffmpeg err: {r.stderr[-300:]}", flush=True)
    return r.returncode == 0


def _is_still(path: Path) -> bool:
    """AI-пул иногда отдаёт стилл (PNG) под именем scene_N.mp4 — ffprobe по кодеку потока,
    не по расширению. Стилл рендерится через Ken Burns (см. render_shot), не -stream_loop."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1",
                        str(path)], capture_output=True, text=True)
    return (r.stdout or "").strip() in {"png", "mjpeg", "bmp", "tiff", "webp", "gif"}


def pull_clip(path: str) -> Path | None:
    """Клип каталога по его манифест-path ('footage_catalog/...') с ЯД (кэш в CLIPS)."""
    name = Path(path).name
    local = CLIPS / name
    if local.exists():
        return local
    if yd_get(f"{CF}/assets/{path}", local):
        return local
    print(f"  ✗ не стянул клип {path}", flush=True)
    return None


def pull_generated(path: str) -> Path | None:
    """Сгенерированный AI-клип сцены (Фаза 1, base.kind='generated') — путь ОТНОСИТЕЛЬНО
    JOB_YD (напр. 'generated/scene_003.mp4'), не манифест каталога. Кэш в CLIPS по имени файла
    (совпадений между job'ами не бывает — имя включает job-специфичный scene-индекс)."""
    name = Path(path).name
    local = CLIPS / name
    if local.exists():
        return local
    if yd_get(f"{JOB_YD}/{path}", local):
        return local
    print(f"  ✗ не стянул сгенерированный клип {path}", flush=True)
    return None


def render_shot(i: int, shot: dict, cover: str, fill: Path | None) -> Path | None:
    """Кадр = base-футаж. Если у base ЗЕЛЁНАЯ зона (chroma) и есть fill → целевое наложение:
    фон-заливка (арт/он-тема) + винил с вырезанным зелёным сверху. БЕЗ футаж-на-футаж/оверлеев
    ([[feedback_no_footage_on_footage]]). base.kind=='generated' (Фаза 1 AI-генерация по сценам) —
    клип уже готовая сцена без chroma, тянется из render_jobs/<JOB_ID>/generated/, не из каталога.

    shot['speed'] (опционально, дефолт 1.0) — псевдо-слоу-мо через setpts (yaromat 2026-07-04):
    <1.0 замедляет (0.75 = 75% скорости), даёт тот же экранный хронометраж на МЕНЬШЕМ числе
    исходников — лекарство от тонкого дневного AI-пула. 1.0/отсутствие поля = старое поведение,
    без изменений."""
    dur = max(0.4, float(shot["t_dur"]))
    base = shot.get("base") or {}
    bpath = base.get("path")
    if not bpath:
        print(f"  shot {i}: нет base.path — пропуск", flush=True)
        return None
    bfile = pull_generated(bpath) if base.get("kind") == "generated" else pull_clip(bpath)
    if not bfile:
        return None
    out = SHOTS / f"shot_{i:03d}.mp4"
    chroma = base.get("chroma") if base.get("kind") != "generated" else None
    speed = float(shot.get("speed") or 1.0)
    speed_vf = f"setpts=PTS/{speed:.4f}," if speed != 1.0 else ""
    common = ["-t", f"{dur:.3f}", "-r", "25", "-pix_fmt", "yuv420p",
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-an", str(out)]
    if chroma and fill:
        fc = (f"[0:v]{cover},fps=25,setsar=1,eq=saturation=0.62:contrast=1.08[bg];"
              f"[1:v]{speed_vf}{cover},fps=25,setsar=1,chromakey={chroma}:0.16:0.10[fg];"
              f"[bg][fg]overlay,format=yuv420p[v]")
        ok = ff(["-stream_loop", "-1", "-i", str(fill),
                 "-stream_loop", "-1", "-i", str(bfile),
                 "-filter_complex", fc, "-map", "[v]", *common])
    elif _is_still(bfile):
        # base из AI-пула бывает СТИЛЛОМ (PNG под именем scene_N.mp4). -stream_loop дал бы
        # мёртвый кадр → Ken Burns медленный наезд ([[feedback_motion_must_be_photographic]]).
        # Пре-скейл ×2 от таргета = разрешение-запас zoompan → без джиттера. Слоу к стиллу неприм.
        m = re.search(r"crop=(\d+):(\d+)", cover)
        W, H = (int(m.group(1)), int(m.group(2))) if m else (1080, 1920)
        zoom = (f"scale={W*2}:{H*2}:force_original_aspect_ratio=increase,crop={W*2}:{H*2},"
                f"zoompan=z='min(zoom+0.0009,1.10)':d=1:"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps=25,setsar=1")
        ok = ff(["-loop", "1", "-i", str(bfile), "-vf", zoom, *common])
    else:
        ok = ff(["-stream_loop", "-1", "-i", str(bfile),
                 "-vf", f"{speed_vf}{cover},fps=25,setsar=1", *common])
    return out if ok else None


def _shot_type(shot: dict) -> str:
    """Тип кадра для роутера: subject (герой/крупно/climax) vs atmosphere."""
    if (shot.get("scale") in ("macro", "close")) or shot.get("section") == "climax":
        return "subject"
    return "atmosphere"


def plan_transitions(shots: list[dict]) -> list[tuple]:
    """Приём стыка, ВХОДЯЩЕГО в каждый кадр (индекс j = стык j-1→j). [0]=None.
    → [(name, d)]; d учитывает slowmo-соседа (speed<1.0)."""
    plan = [None]
    for j in range(1, len(shots)):
        prev, cur = shots[j - 1], shots[j]
        name = _tr.lookup_transition(cur.get("section"), cur.get("energy"),
                                     _shot_type(prev), _shot_type(cur))
        d = _tr.transition_duration(
            name,
            prev_slowmo=float(prev.get("speed") or 1.0) < 1.0,
            next_slowmo=float(cur.get("speed") or 1.0) < 1.0)
        plan.append((name, d))
    return plan


def main():
    print(f"Job: {JOB_ID}", flush=True)
    sb_file = WORKDIR / "storyboard.json"
    track   = WORKDIR / "track.mp3"
    if not yd_get(f"{JOB_YD}/storyboard.json", sb_file):
        sys.exit("нет storyboard.json в job-папке")
    if not yd_get(f"{JOB_YD}/track.mp3", track):
        sys.exit("нет track.mp3 в job-папке")
    sb = json.loads(sb_file.read_text(encoding="utf-8"))
    shots = sb.get("shots", [])
    fmt = sb.get("format", "vertical")
    cover = COVER.get(fmt, COVER["vertical"])
    reel_dur = float(sb.get("duration") or sum(float(s["t_dur"]) for s in shots))
    print(f"  кадров={len(shots)} format={fmt} reel≈{reel_dur:.1f}с", flush=True)
    if not shots:
        sys.exit("storyboard без shots")

    # fill для целевой заливки зелёных зон (арт/он-тема). Опционально.
    fill = None
    if sb.get("fill"):
        fcand = WORKDIR / "fill.mp4"
        if yd_get(f"{JOB_YD}/{sb['fill']}", fcand):
            fill = fcand
            print(f"  fill: {sb['fill']} ✓ (заливка зелёных зон)", flush=True)
        else:
            print(f"  ⚠ fill {sb['fill']} не стянут — зелёные зоны останутся", flush=True)

    # 1. рендер кадров. Каждый кадр — с ЗАПАСОМ-хвостом на исходящий переход (его съест
    #    xfade → нетто t_dur сохраняется, EDL не дрейфует относительно музыки). [[transition_router]]
    print("\n── Рендер кадров ──", flush=True)
    plan = plan_transitions(shots)     # приём стыка, входящего в каждый кадр
    rendered = []                      # [(path, shot, rendered_dur)]
    for i, sh in enumerate(shots):
        d_out = plan[i + 1][1] if i + 1 < len(shots) else 0.0
        rdur = _trn.render_tail(float(sh["t_dur"]), d_out)
        out = render_shot(i, {**sh, "t_dur": rdur}, cover, fill)
        if out:
            rendered.append((out, sh, rdur))
            b = sh.get("base") or {}
            tag = f"{b.get('category')}{'+заливка' if (b.get('chroma') and fill) else ''}"
            print(f"  ✓ shot {i}: {sh['t_dur']:.1f}с {tag}", flush=True)
    if not rendered:
        yd_put_status("FAIL: ни один кадр не отрендерился")
        sys.exit("0 кадров")

    # 2. склейка с переходами (L6 transition-router): xfade-цепочка, фолбэк на concat.
    print("\n── Склейка (переходы) ──", flush=True)
    paths = [r[0] for r in rendered]
    concat = WORKDIR / "concat.mp4"
    xfade_ok = False
    if len(rendered) >= 2:
        durs = [r[2] for r in rendered]
        trans = [None]
        names_log = []
        for k in range(1, len(rendered)):
            prev_sh, cur_sh = rendered[k - 1][1], rendered[k][1]
            name = _tr.lookup_transition(cur_sh.get("section"), cur_sh.get("energy"),
                                         _shot_type(prev_sh), _shot_type(cur_sh))
            d = _tr.transition_duration(
                name, prev_slowmo=float(prev_sh.get("speed") or 1.0) < 1.0,
                next_slowmo=float(cur_sh.get("speed") or 1.0) < 1.0)
            d = max(0.04, min(d, durs[k - 1] - 0.1, durs[k] - 0.1))  # не длиннее клипов
            trans.append((_tr.xfade_name(name) or "fade", d))
            names_log.append(name)
        fc, label, total = _trn.build_xfade_chain(durs, trans)
        # DIAG: реальные длины отрендеренных клипов (rdur ожидается)
        def _probe(p):
            try:
                r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                    "-of", "default=nw=1:nk=1", str(p)], capture_output=True, text=True)
                return float(r.stdout.strip() or 0)
            except Exception:
                return -1.0
        print(f"  DIAG durs(ожид rdur)={[round(d,2) for d in durs]}", flush=True)
        print(f"  DIAG клипы(факт)={[round(_probe(p),2) for p in paths]}", flush=True)
        inputs = []
        for p in paths:
            inputs += ["-i", str(p)]
        xfade_ok = ff(inputs + ["-filter_complex", fc, "-map", label,
                                "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                                "-pix_fmt", "yuv420p", str(concat)])
        exp = round(sum(float(r[1]["t_dur"]) for r in rendered), 1)
        print(f"  переходы: {names_log}", flush=True)
        print(f"  DIAG concat(факт)={round(_probe(concat),2)}с", flush=True)
        print(f"  xfade-цепочка: {'OK' if xfade_ok else 'FAIL'}, "
              f"timeline={total}с (ожид.~{exp}с)", flush=True)
    if not xfade_ok:
        print("  → фолбэк на concat (hard-cut)", flush=True)
        lst = WORKDIR / "list.txt"
        lst.write_text("".join(f"file '{p}'\n" for p in paths), encoding="utf-8")
        if not ff(["-f", "concat", "-safe", "0", "-i", str(lst),
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                   "-pix_fmt", "yuv420p", str(concat)]):
            sys.exit("concat не вышел")

    # 3. аудио-окно + mux. Полнотрековый EDL (раскадровка привязана к энергокарте ВСЕГО трека)
    # задаёт audio_start явно → highlight ПРОПУСКАЕТСЯ (иначе десинхрон: пик кадра ≠ пик трека).
    # Хайлайт-режим (короткий рил без audio_start) сохранён без изменений.
    print("\n── Аудио + mux ──", flush=True)
    audio_start = sb.get("audio_start")
    if audio_start is not None:
        hl = float(audio_start)
        print(f"  audio_start={hl:.1f}с (highlight пропущен — полнотрековый EDL)", flush=True)
    else:
        try:
            from analyze import find_highlight_offset  # ленивый: aubio нужен только в highlight-режиме
            hl = find_highlight_offset(str(track), window=reel_dur)
        except Exception as e:
            print(f"  highlight err ({e}) → 0.0", flush=True)
            hl = 0.0
        print(f"  highlight_offset={hl:.1f}с (интро до него отрезано)", flush=True)
    result = WORKDIR / "result.mp4"
    if not ff(["-i", str(concat), "-ss", f"{hl:.3f}", "-t", f"{reel_dur:.3f}", "-i", str(track),
               "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "21", "-c:a", "aac", "-b:a", "192k", "-shortest",
               "-movflags", "+faststart", str(result)]):
        sys.exit("mux не вышел")

    sz = result.stat().st_size // 1024
    print(f"\n✅ result.mp4 {sz}KB → ЯД", flush=True)
    yd_put(result, f"{JOB_YD}/result.mp4")
    yd_put_status(f"done: {len(rendered)} кадров, {reel_dur:.0f}с, {fmt}, {sz}KB")


def yd_put_status(text: str):
    f = WORKDIR / "status.txt"
    f.write_text(text, encoding="utf-8")
    yd_put(f, f"{JOB_YD}/status.txt")


if __name__ == "__main__":
    main()
