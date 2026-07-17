#!/usr/bin/env python3
"""
vzrosly_clip_job.py — GitHub Actions runner: биполярный коллаж-тизер по clip_style_guide.

Техника из гайда «примеры-клипов»: held↔строб ритм, читаемый scratch-слой («видна рука»),
рукописный хук, тёплый детский контраст в аутро. Палитра/смысл — из брифа трека «взрослый».

С ЯД (render_jobs/<JOB_ID>/): job.json, track.mp3, anchor.png, cold_01..04.png, child.png
Из репо (assets/): Caveat.ttf (рукописный кириллический), scratch_overlay.mp4

job.json:
  {"format":"square|vertical", "out_name":"...mp4", "audio_start":8, "duration":28,
   "word":"ВЗРОСЛЫЙ",
   "hook":"Никто не спасёт тебя.\\nВзрослый тут ты.",
   "outro":"yaromat — взрослый\\nскоро · включи звук"}

Env: JOB_ID
"""
import json, os, subprocess, sys, math
from pathlib import Path

JOB_ID = os.environ.get("JOB_ID", "")
if not JOB_ID:
    sys.exit("JOB_ID not set")

REMOTE  = "ydrive"
JOBS_YD = "Content factory/cloud_io/render_jobs"
JOB_YD  = f"{JOBS_YD}/{JOB_ID}"
WORK    = Path("/tmp/vzrosly_job"); WORK.mkdir(parents=True, exist_ok=True)
REPO    = Path(__file__).resolve().parent
FONT    = str(REPO / "assets" / "Caveat.ttf")
SCRATCH = str(REPO / "assets" / "scratch_overlay.mp4")
GRIT    = str(REPO / "assets" / "grit_overlay.mp4")

FMT = {"square": (1080, 1080), "vertical": (1080, 1920), "landscape": (1920, 1080)}
FPS = 25
BEAT = 60.0 / 87.0  # 0.6897s

# Моторные ручки (Фаза 2: управляются рецептом через job → recipe_to_treatment).
# Дефолты = откалиброванные значения yaromat (-50%). main() переопределяет из job.
MOTION_SPEED  = 0.5    # скорость/плотность дрейфа
MOTION_AMP    = 1.15   # амплитуда движения (margin-фактор)
BLEND_OPACITY = 0.5    # сила двойной экспозиции

# Тинт видео-футажа под лук замка палитры (cold_noir_portishead, hue ~178° teal).
# Реальный сток приходит серым/цветным и без него выпадает из пула артов. Проверено на storm-футаже:
# hue 0°/sat 0%/ярк 92 → hue 176°/sat 44%/ярк 61 (арты: 178°/60%/53). job["footage_tint"] переопределяет.
FOOTAGE_TINT = ("colorbalance=rs=-0.14:gs=0.04:bs=0.10:rm=-0.12:gm=0.04:bm=0.09:"
                "rh=-0.08:gh=0.02:bh=0.05,eq=saturation=0.9:brightness=-0.06")

STYLES_FILE = REPO / "styles.json"
_DEFAULT_STYLE = {"name": "cold_steel",
                  "eq": "contrast=1.14:saturation=0.9:brightness=-0.02:gamma=0.96",
                  "balance": None, "grain": [10, 15], "vignette": "angle=PI/4.5"}


def load_styles() -> list:
    """Пул визуальных луков из styles.json (грейд+колорбаланс+зерно+виньетка)."""
    try:
        st = json.loads(STYLES_FILE.read_text())["styles"]
        return st or [_DEFAULT_STYLE]
    except Exception as e:
        print(f"  WARN: styles.json не прочитан ({e}) — дефолтный {_DEFAULT_STYLE['name']}")
        return [_DEFAULT_STYLE]


def pick_style(styles: list, seed: int, override: str = "") -> dict:
    """Стиль на трек: явный job['style'] либо детерминированно от seed (разный трек → разный лук)."""
    if override:
        for s in styles:
            if s["name"] == override:
                return s
        print(f"  WARN: стиль '{override}' не найден в styles.json — выбор по seed")
    return styles[seed % len(styles)]


def yd_get(remote: str, local: Path) -> bool:
    local.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.run(["rclone", "copyto", f"{REMOTE}:{remote}", str(local)],
                          capture_output=True, text=True).returncode == 0

def yd_put(local: Path, remote: str) -> bool:
    return subprocess.run(["rclone", "copyto", str(local), f"{REMOTE}:{remote}"],
                          capture_output=True, text=True).returncode == 0

def scene_qc(video: Path, expected_cuts: int) -> str:
    """QC рендера (PySceneDetect, скор repo_scout #5): сравнивает реально детектированные
    склейки с ожидаемым числом сегментов таймлайна. Не блокирует — только предупреждает
    в status.txt, если сцены слиплись (xfade съел переход) или расползлись (лишние резкие смены)."""
    try:
        from scenedetect import detect, ContentDetector
        scenes = detect(str(video), ContentDetector())
        found = len(scenes)
    except Exception as e:
        return f"scene_qc: skipped ({e})"
    if expected_cuts <= 0:
        return f"scene_qc: found={found}"
    ratio = found / expected_cuts
    if ratio < 0.4:
        return f"scene_qc: WARN слиплись сегменты (found={found}, expected~{expected_cuts})"
    if ratio > 2.5:
        return f"scene_qc: WARN лишние резкие смены (found={found}, expected~{expected_cuts})"
    return f"scene_qc: ok (found={found}, expected~{expected_cuts})"


def yd_put_text(text: str, remote: str):
    t = WORK / "_s.txt"; t.write_text(text); yd_put(t, remote)

def run(cmd) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def make_cover(src: Path, dst: Path, W: int, H: int, zoom: float = 1.0, flip: bool = False):
    """Cover-crop картинки к WxH (+опц. зум-кроп для 'punch', +опц. флип)."""
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
          f"crop={W}:{H}")
    if zoom > 1.0:
        cw, ch = int(W / zoom), int(H / zoom)
        vf += f",crop={cw}:{ch},scale={W}:{H}"
    if flip:
        vf += ",hflip"
    vf += ",format=yuv420p"
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vf", vf,
         "-frames:v", "1", str(dst)])


def grid_seg(srcs: list, dur: float, out: Path, W: int, H: int,
             crf: str = "22", preset: str = "veryfast", noise: int = 34) -> bool:
    """Сетка-панель 2×2 В ДВИЖЕНИИ (приём mimo «мультиэкран»), режиссура yaromat:
    левая колонка едет ВНИЗ, правая ВВЕРХ; внутри каждого слота дрейф; сильный шум до помех.
    Зазоры от хода колонок прикрыты размытым тёмным фоном. Чистый ffmpeg."""
    srcs = (srcs * 4)[:4]                       # ровно 4 слота (2×2)
    cw, ch = W // 2, H // 2
    OW, OH = int(cw * 1.22), int(ch * 1.22)     # запас под внутренний дрейф
    dx, dy = OW - cw, OH - ch
    DY = int(ch * 0.18)                          # ход колонок (вниз/вверх)
    ins = []
    for s in srcs:
        ins += ["-loop", "1", "-t", f"{dur:.3f}", "-i", str(s)]
    p = [f"[0]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
         f"boxblur=20:2,eq=brightness=-0.12,setsar=1[bg]"]
    for i in range(4):                           # слоты с внутренним дрейфом (разная фаза)
        ph = i * math.pi / 2
        xe = f"{dx//2}+{dx//2}*sin(2*PI*(t/{dur:.3f})*0.5+{ph:.2f})"
        ye = f"{dy//2}+{dy//2}*cos(2*PI*(t/{dur:.3f})*0.5+{ph:.2f})"
        p.append(f"[{i}]scale={OW}:{OH}:force_original_aspect_ratio=increase,"
                 f"crop={cw}:{ch}:x='{xe}':y='{ye}',setsar=1[c{i}]")
    d = f"(t/{dur:.3f})*{DY}"                     # величина хода колонок (без знака)
    p.append(f"[bg][c0]overlay=x=0:y='0+{d}'[o0]")         # лев-верх — вниз
    p.append(f"[o0][c2]overlay=x=0:y='{ch}+{d}'[o1]")      # лев-низ — вниз
    p.append(f"[o1][c1]overlay=x={cw}:y='0-{d}'[o2]")      # прав-верх — вверх
    p.append(f"[o2][c3]overlay=x={cw}:y='{ch}-{d}'[o3]")   # прав-низ — вверх
    p.append(f"[o3]noise=alls={noise}:allf=t+u,format=yuv420p[v]")
    enc = ["-r", str(FPS), "-c:v", "libx264", "-crf", crf, "-preset", preset,
           "-video_track_timescale", "12800", str(out)]
    r = run(["ffmpeg", "-y", "-loglevel", "error", *ins,
             "-filter_complex", ";".join(p), "-map", "[v]"] + enc)
    if r.returncode != 0:
        print("grid_seg:", r.stderr[-400:])
    return r.returncode == 0 and out.exists()


def motion_seg(cover: Path, dur: float, mode: str, theta: float, blend: str,
               out: Path, W: int, H: int, crf: str = "22", preset: str = "veryfast") -> bool:
    """Сегмент-видео: 2 копии арта дрейфуют + blend (двойная экспозиция → виден бленд
    + движение). mode: pan (встречный дрейф) | inward (схождение к центру) | single.
    Амплитуда и скорость движения снижены (фидбэк yaromat: -50% и то и то)."""
    enc = ["-r", str(FPS), "-c:v", "libx264", "-crf", crf, "-preset", preset,
           "-video_track_timescale", "12800", str(out)]
    SPEED = MOTION_SPEED              # плотность/скорость движения (рецепт-управляемо)
    BW, BH = int(W * MOTION_AMP), int(H * MOTION_AMP)   # амплитуда (margin) (рецепт-управляемо)
    pre = f"scale={BW}:{BH}:force_original_aspect_ratio=increase,crop={BW}:{BH}"
    mx, my = BW - W, BH - H
    cx, cy = mx / 2, my / 2
    ax, ay = mx / 2 * math.cos(theta) * SPEED, my / 2 * math.sin(theta) * SPEED
    # фаза: pan — дрейф через центр (-1..1); inward — схождение к центру (1..0)
    ph_pan = f"((t/{dur:.4f})*2-1)"
    ph_in  = f"(1-(t/{dur:.4f}))"
    def drift(c, a, sign, ph): return f"{c:.1f}+({sign}({a:.2f}))*{ph}"

    if mode == "single":   # один слой, нежный дрейф (для outro/рисунка)
        fc = (f"[0]{pre},crop={W}:{H}:"
              f"x='{drift(cx, ax, '+', ph_pan)}':y='{drift(cy, ay, '+', ph_pan)}',"
              f"format=yuv420p[v]")
    elif mode == "split":  # распил кадра на 2 части, половины дрейфуют в разные стороны
        # ось (гориз/верт) и направление (apart/together) из theta → рандом по сегментам.
        # фон = размытая затемнённая копия заполняет открывающийся зазор.
        ph = f"(t/{dur:.4f})"
        horiz = math.cos(theta) >= 0
        sgn = 1 if math.sin(theta) >= 0 else -1
        if horiz:
            amp = H * 0.09 * sgn
            fc = (f"[0]scale={W}:{H},crop={W}:{H},setsar=1[bz];[bz]split=3[bg][t][b];"
                  f"[bg]scale=ceil({W}*1.18/2)*2:ceil({H}*1.18/2)*2,crop={W}:{H},boxblur=14:2,eq=brightness=-0.05,format=yuv420p[bgd];"
                  f"[t]crop={W}:{H//2}:0:0,format=yuv420p[top];"
                  f"[b]crop={W}:{H//2}:0:{H//2},format=yuv420p[bot];"
                  f"[bgd][top]overlay=x=0:y='0-({amp:.1f})*{ph}'[o1];"
                  f"[o1][bot]overlay=x=0:y='{H//2}+({amp:.1f})*{ph}',format=yuv420p[v]")
        else:
            amp = W * 0.09 * sgn
            fc = (f"[0]scale={W}:{H},crop={W}:{H},setsar=1[bz];[bz]split=3[bg][l][r];"
                  f"[bg]scale=ceil({W}*1.18/2)*2:ceil({H}*1.18/2)*2,crop={W}:{H},boxblur=14:2,eq=brightness=-0.05,format=yuv420p[bgd];"
                  f"[l]crop={W//2}:{H}:0:0,format=yuv420p[lft];"
                  f"[r]crop={W//2}:{H}:{W//2}:0,format=yuv420p[rgt];"
                  f"[bgd][lft]overlay=x='0-({amp:.1f})*{ph}':y=0[o1];"
                  f"[o1][rgt]overlay=x='{W//2}+({amp:.1f})*{ph}':y=0,format=yuv420p[v]")
    else:
        ph = ph_in if mode == "inward" else ph_pan
        # format=gbrp на входах blend → бленд в RGB (иначе screen/lighten сдвигают цвет в фиолет)
        fc = (f"[0]{pre},split[a][b];"
              f"[a]crop={W}:{H}:x='{drift(cx, ax, '+', ph)}':y='{drift(cy, ay, '+', ph)}',setsar=1,format=gbrp[ca];"
              f"[b]crop={W}:{H}:x='{drift(cx, ax, '-', ph)}':y='{drift(cy, ay, '-', ph)}',setsar=1,format=gbrp[cb];"
              f"[ca][cb]blend=all_mode={blend}:all_opacity={BLEND_OPACITY},format=yuv420p[v]")
    r = run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-t", f"{dur:.4f}",
             "-i", str(cover), "-filter_complex", fc, "-map", "[v]"] + enc)
    if r.returncode != 0:
        print("motion_seg:", r.stderr[-400:])
    return r.returncode == 0 and out.exists()


def make_video_seg(src: Path, dur: float, out: Path, W: int, H: int,
                   crf: str = "22", preset: str = "veryfast", tint: str = "") -> bool:
    """Сегмент из видео-футажа (Pexels): slice длины dur, cover-crop в WxH, fps.
    Короткий футаж зацикливается (-stream_loop). Грейд под стиль — общим density-пассом тела.
    Энкод как у motion_seg (timescale 12800) → совместимо с xfade_chain.
    tint: арты цветные ПО ГЕНЕРАЦИИ (замок палитры), реальный сток — нет. Общий density-пасс
    (eq контраст/сатурация) серый футаж в палитру НЕ приводит → грозовые облака садятся
    серо-белым пятном посреди нуара. Тинт красит футаж в лук замка ДО сборки."""
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
          + (f"{tint}," if tint else "")
          + f"fps={FPS},setsar=1,format=yuv420p")
    r = run(["ffmpeg", "-y", "-loglevel", "error", "-stream_loop", "-1",
             "-t", f"{dur:.4f}", "-i", str(src), "-vf", vf, "-an",
             "-r", str(FPS), "-c:v", "libx264", "-crf", crf, "-preset", preset,
             "-video_track_timescale", "12800", str(out)])
    if r.returncode != 0:
        print("make_video_seg:", r.stderr[-300:])
    return r.returncode == 0 and out.exists()


def probe_dur(p: Path) -> float:
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(p)])
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def mean_luma(p: Path) -> float:
    """Средняя яркость кадра 0..255 (ffmpeg signalstats YAVG). Для выбора контраста текста."""
    import re
    r = run(["ffmpeg", "-i", str(p), "-vf", "signalstats,metadata=print:file=-",
             "-frames:v", "1", "-f", "null", "-"])
    m = re.search(r"YAVG[:=]([0-9.]+)", r.stdout + r.stderr)
    return float(m.group(1)) if m else 128.0


def contrast_text(luma: float):
    """По яркости фона → (fontcolor, bordercolor) для читаемости на любом кадре."""
    return ("white", "black") if luma < 120 else ("0x222018", "white")


def xfade_chain(segs, durs, trans, tdurs, out: Path,
                crf: str = "20", preset: str = "veryfast") -> bool:
    """Склейка сегментов через xfade с разными переходами. Возвращает успех.
    segs[i] — путь, durs[i] — реальная длительность, trans[i]/tdurs[i] — переход
    ВХОДА в сегмент i (i>=1)."""
    inputs = []
    for s in segs:
        inputs += ["-i", str(s)]
    parts = []
    prev = "0:v"
    running = durs[0]
    for i in range(1, len(segs)):
        d = tdurs[i]
        off = max(0.0, running - d)
        lbl = f"x{i}"
        parts.append(f"[{prev}][{i}:v]xfade=transition={trans[i]}:"
                     f"duration={d:.3f}:offset={off:.3f}[{lbl}]")
        prev = lbl
        running = running + durs[i] - d
    fc = ";".join(parts)
    r = run(["ffmpeg", "-y", "-loglevel", "error"] + inputs +
            ["-filter_complex", fc, "-map", f"[{prev}]",
             "-r", str(FPS), "-c:v", "libx264", "-crf", crf, "-preset", preset,
             "-pix_fmt", "yuv420p", str(out)])
    if r.returncode != 0:
        print("xfade_chain:", r.stderr[-600:])
    return r.returncode == 0 and out.exists()


import random

# 8 направлений дрейфа (радианы) для двойной экспозиции
DIRS = [i * math.pi / 4 for i in range(8)]
# палитра переходов xfade для грува/выдоха (разнообразие «смены кадров»)
GROOVE_TR = ["dissolve", "wipeleft", "wiperight", "slideup", "slidedown",
             "smoothright", "smoothleft", "circleopen", "fadegrays"]
STROBE_TR = ["fade", "fade", "fade", "fadewhite", "slideleft", "fade",
             "fadeblack", "slideup", "fade", "fadewhite"]


def _timeline_collage(variant="full", bpm=87.0, seed=42, calm=False, split=False):
    """Список сегментов: dict(key,dur,mode,theta,blend,tin,tdur,region).
    variant: 'full' (~28с) | 'short' (~14с, для X) — та же биполярная структура, сжата.
    bpm: темп трека — задаёт долю (held↔строб ритм матчит бит). Дефолт 87 (трек «взрослый»).
    seed: per-track — одинаков для square/vertical одного трека (два формата = один клип),
    но РАЗНЫЙ между треками → разная хореография монтажа (режимы/бленды/направления/переходы).
    calm: для безударных/амбиентных версий — строб режется вдвое медленнее и короче,
    переходы мягкие (без вспышек в белый), движение спокойнее. Энергия видео = энергии трека."""
    random.seed(seed)  # детерминизм внутри трека; разнообразие между треками
    b = 60.0 / bpm
    raw = []
    if variant == "short":
        raw.append(("anchor", 3 * b, "intro"))             # 0: held intro
        groove = ["c1", "anchor", "clock", "c2", "anchor", "crowd"]
        raw += [(k, b, "groove") for k in groove]          # 1..6
        strobe_pool = ["clock", "c1", "crowd", "a4", "c2", "a2", "anchor", "c3",
                       "a1", "c4"]
        random.shuffle(strobe_pool)                  # порядок строба варьируется по seed
        n_str = 6 if calm else 8
        d_str = (1.0 if calm else 0.75) * b          # строб на 0.75 доли (не слепые 0.5) → в ритм
        for i in range(n_str):
            raw.append((strobe_pool[i % len(strobe_pool)], d_str, "strobe"))  # 7..
        raw.append(("anchorp", 3 * b, "breath"))           # 17: held breath
        raw.append(("child", 2.3, "outro"))                # 18: warm outro
    else:
        raw.append(("anchor", 6 * b, "intro"))             # 0: held intro
        groove = ["c1", "anchor", "clock", "c2", "anchor", "crowd",
                  "c3", "anchor", "c4", "a1", "anchor", "a2"]
        raw += [(k, b, "groove") for k in groove]          # 1..12
        strobe_pool = ["clock", "c1", "crowd", "a4", "c2", "a2", "anchor", "c3",
                       "a1", "c4", "crowd", "c1f", "a2f", "c2f", "clock", "c4f"]
        random.shuffle(strobe_pool)                  # порядок строба варьируется по seed
        n_str = 10 if calm else 14
        d_str = (1.0 if calm else 0.75) * b          # строб на 0.75 доли (не слепые 0.5) → в ритм
        for i in range(n_str):
            raw.append((strobe_pool[i % len(strobe_pool)], d_str, "strobe"))  # 13..
        raw.append(("anchorp", 6 * b, "breath"))           # 33: held breath
        raw.append(("child", 4.54, "outro"))               # 34: warm outro

    seq = []
    for i, (key, dur, region) in enumerate(raw):
        # движение: held — мягкий дрейф; грув/строб — полный + случайный zoom; outro — нежный single
        if region in ("intro", "breath"):
            mode, blend = "pan", "average"
        elif region == "outro":
            mode, blend = "single", "none"
        elif split and region in ("groove", "strobe"):
            mode, blend = "split", "none"   # распил+дрейф половин (тема «ускользающий»)
        elif calm:
            # спокойнее: больше мягкого дрейфа, меньше схождения/жёстких блендов
            mode = random.choice(["pan", "pan", "inward"])
            blend = random.choice(["average", "average", "screen"])
        else:
            # рандомно встречный дрейф или схождение «внутрь» (фидбэк yaromat)
            mode = random.choice(["pan", "inward", "inward"])
            blend = random.choice(["average", "average", "screen", "lighten"])
        theta = random.choice(DIRS)
        # переход ВХОД в сегмент i (граница i-1 → i)
        if i == 0:
            tin, tdur = None, 0.0
        elif region == "groove":
            tin, tdur = random.choice(GROOVE_TR), 0.15
        elif region == "strobe":
            if calm:
                tin, tdur = random.choice(["fade", "fade", "dissolve", "fadeblack"]), 0.25
            else:
                tin, tdur = random.choice(STROBE_TR), (0.05 if raw[i][1] < 0.4 else 0.1)
        elif region == "breath":
            tin, tdur = random.choice(["fadeblack", "dissolve"]), 0.30
        elif region == "outro":
            tin, tdur = "fadewhite", 0.40    # вспышка в белый → тёплый переворот
        else:
            tin, tdur = "fade", 0.05
        seq.append(dict(key=key, dur=dur, mode=mode, theta=theta,
                        blend=blend, tin=tin, tdur=tdur, region=region))
    total = sum(s["dur"] for s in seq)
    return seq, total


def _timeline_kenburns(variant="full", bpm=87.0, seed=42):
    """Сценарий 'kenburns': кинематографичные длинные одиночные планы, нежный дрейф,
    мягкие переходы, БЕЗ строба. Спокойнее коллажа — для медитативной динамики."""
    random.seed(seed + 1001)
    b = 60.0 / bpm
    pool = ["c1", "c2", "c3", "c4", "crowd", "clock", "a1", "a2", "a4"]
    random.shuffle(pool)
    seq = [dict(key="anchor", dur=6 * b, mode="single", theta=random.choice(DIRS),
                blend="none", tin=None, tdur=0.0, region="intro")]
    n_mid  = 4 if variant == "short" else 6
    shot_b = 3.0 if variant == "short" else 3.5
    for i in range(n_mid):
        seq.append(dict(key=pool[i % len(pool)], dur=shot_b * b, mode="single",
                        theta=random.choice(DIRS), blend="none",
                        tin=random.choice(["dissolve", "fade", "fadegrays"]), tdur=0.5,
                        region="groove"))
    seq.append(dict(key="anchorp", dur=(4 if variant == "short" else 6) * b, mode="single",
                    theta=random.choice(DIRS), blend="none",
                    tin=random.choice(["fadeblack", "dissolve"]), tdur=0.5, region="breath"))
    seq.append(dict(key="child", dur=(2.3 if variant == "short" else 4.54), mode="single",
                    theta=random.choice(DIRS), blend="none", tin="fadewhite", tdur=0.4,
                    region="outro"))
    return seq, sum(s["dur"] for s in seq)


def _timeline_hardcut(variant="full", bpm=87.0, seed=42):
    """Сценарий 'hardcut': жёсткая нарезка по биту — равные 1-долевые планы (иногда 0.5),
    почти мгновенные переходы (~1 кадр = cut). Ритмичный, ударный характер."""
    random.seed(seed + 2002)
    b = 60.0 / bpm
    pool = ["c1", "c2", "c3", "c4", "c1f", "c2f", "c4f", "crowd", "clock",
            "a1", "a2", "a2f", "a4", "anchor"]
    random.shuffle(pool)
    seq = [dict(key="anchor", dur=4 * b, mode="single", theta=random.choice(DIRS),
                blend="none", tin=None, tdur=0.0, region="intro")]
    n_cut = 12 if variant == "short" else 20
    for i in range(n_cut):
        d = (0.5 if i % 4 == 3 else 1.0) * b          # ритм: в осн. 1 доля, иногда двойное время
        seq.append(dict(key=pool[i % len(pool)], dur=d, mode="single",
                        theta=random.choice(DIRS), blend="none",
                        tin="fade", tdur=0.04, region="groove"))   # tdur ~1 кадр = жёсткий cut
    seq.append(dict(key="anchorp", dur=4 * b, mode="single", theta=random.choice(DIRS),
                    blend="none", tin=random.choice(["fadeblack", "dissolve"]), tdur=0.25,
                    region="breath"))
    seq.append(dict(key="child", dur=(2.3 if variant == "short" else 4.54), mode="single",
                    theta=random.choice(DIRS), blend="none", tin="fadewhite", tdur=0.4,
                    region="outro"))
    return seq, sum(s["dur"] for s in seq)


def build_timeline(variant="full", bpm=87.0, seed=42, calm=False, split=False, scenario="collage"):
    """Диспетчер сценариев монтажа (ось разнообразия, независима от лука/стиля).
    collage (дефолт, биполярный коллаж + calm/split) | kenburns | hardcut."""
    if scenario == "kenburns":
        return _timeline_kenburns(variant, bpm, seed)
    if scenario == "hardcut":
        return _timeline_hardcut(variant, bpm, seed)
    return _timeline_collage(variant, bpm, seed, calm, split)


# источники-ассеты (имена файлов) и спецификация cover-кадров: ключ → (src, zoom, flip).
# Общий источник правды для рендера и для локального сториборда (preview tier 1).
SRC_ASSETS = ["anchor.png", "cold_01.png", "cold_02.png", "cold_03.png", "cold_04.png",
              "child.png", "crowd.png", "clock.png", "art1.png", "art2.png", "art4.png"]
COVER_SPEC = {
    "anchor":  ("anchor.png", 1.0,  False),
    "anchorp": ("anchor.png", 1.28, False),   # punch-in для выдоха
    "child":   ("child.png",  1.0,  False),
    "c1":  ("cold_01.png", 1.0, False), "c1f": ("cold_01.png", 1.06, True),
    "c2":  ("cold_02.png", 1.0, False), "c2f": ("cold_02.png", 1.06, True),
    "c3":  ("cold_03.png", 1.0, False),
    "c4":  ("cold_04.png", 1.0, False), "c4f": ("cold_04.png", 1.06, True),
    "crowd": ("crowd.png", 1.0, False),
    "clock": ("clock.png", 1.0, False),
    "a1": ("art1.png", 1.0, False),
    "a2": ("art2.png", 1.0, False), "a2f": ("art2.png", 1.06, True),
    "a4": ("art4.png", 1.0, False),
}


def main():
    global MOTION_SPEED, MOTION_AMP, BLEND_OPACITY, SCRATCH, GRIT
    print(f"Job: {JOB_ID}")
    jf = WORK / "job.json"
    if not yd_get(f"{JOB_YD}/job.json", jf):
        sys.exit("no job.json")
    job = json.loads(jf.read_text())
    fmt = job.get("format", "square")
    W, H = FMT.get(fmt, FMT["square"])
    out_name = job["out_name"]
    audio_start = float(job.get("audio_start", 8))
    variant = job.get("variant", "full")
    bpm = float(job.get("bpm", 87.0))
    preview = bool(job.get("preview", False))
    word  = job.get("word", "")
    hook  = job.get("hook", "")
    outro = job.get("outro", "")
    track_credit = job.get("track_credit", "")   # старт: «Артист — Трек» (режим reference)
    watermark    = job.get("watermark", "")       # весь клип: кредит yaromat (привязка охватов)
    video_keys   = job.get("video_keys", [])      # ключи-сегменты из видео-футажа (Pexels) вместо стиллов
    # тинт футажа в лук замка палитры (замер 17.07: серые облака hue 0°/sat 0% → 176°/44%, к артам 178°/60%)
    footage_tint = job.get("footage_tint", FOOTAGE_TINT)
    seed         = int(job.get("seed", 42))        # per-track: монтаж + текстура (см. build_timeline)
    calm         = bool(job.get("calm", False))     # безударные/амбиентные версии: мягкий строб
    split        = bool(job.get("split", False))     # распил кадра на 2 части + дрейф половин
    styles = load_styles()
    style  = pick_style(styles, seed, job.get("style", ""))  # per-track лук (грейд/зерно/виньетка)
    scenario = job.get("scenario", "collage")                # per-track сценарий монтажа (ось разнообразия)
    grid     = bool(job.get("grid", False))                   # сетка-панель в hook-сегментах (приём mimo)
    # Фаза 2: моторные ручки из рецепта (дефолты = калибровка yaromat)
    MOTION_SPEED  = float(job.get("speed", MOTION_SPEED))
    MOTION_AMP    = float(job.get("amplitude", MOTION_AMP))
    BLEND_OPACITY = float(job.get("blend_opacity", BLEND_OPACITY))

    # preview tier 2: половинное разрешение + ultrafast → дешёвый proxy для ревью движения/плотности/ритма
    if preview:
        W, H = (W // 2) // 2 * 2, (H // 2) // 2 * 2   # half, чётное
    seg_crf, seg_preset = ("30", "ultrafast") if preview else ("22", "veryfast")
    body_crf, body_preset = ("30", "ultrafast") if preview else ("20", "veryfast")

    # downloads
    for name in ["track.mp3"] + SRC_ASSETS:
        if not yd_get(f"{JOB_YD}/{name}", WORK / name):
            sys.exit(f"missing {name}")
    # видео-футаж для video_keys (Pexels-вставки)
    for k in video_keys:
        if not yd_get(f"{JOB_YD}/{k}.mp4", WORK / f"{k}.mp4"):
            print(f"  WARN: нет видео {k}.mp4 — упаду на стилл")

    # оверлеи из библиотеки Pinterest (доска yaromat/overlay) вместо репо-дефолтов.
    # job["overlays"] = {"scratch": "<pin_id>", "grit": "<pin_id>"} → assets/overlay_assets/board/.
    # Оба идут в screen через format=gray → нейтральны by design (тон оверлея кадр не красит).
    for slot in ("scratch", "grit"):
        pid = (job.get("overlays") or {}).get(slot)
        if not pid:
            continue
        dst = WORK / f"ovl_{slot}.mp4"
        if yd_get(f"Content factory/assets/overlay_assets/board/{pid}.mp4", dst):
            if slot == "scratch": SCRATCH = str(dst)
            else:                 GRIT = str(dst)
            print(f"  оверлей {slot} ← доска {pid}")
        else:
            print(f"  WARN: оверлей {pid} не скачался — остаётся репо-дефолт")
    print(f"  format={fmt} {W}x{H} audio_start={audio_start} preview={preview}")

    # covers
    cov = WORK / "cov"; cov.mkdir(exist_ok=True)
    cover_path = {}
    for key, (src, zoom, flip) in COVER_SPEC.items():
        p = cov / f"{key}.png"
        make_cover(WORK / src, p, W, H, zoom, flip)
        cover_path[key] = p

    # сетка-панель (приём mimo): 4 ассета для анимированной сетки 2×2 в hook-сегментах
    grid_srcs = None
    if grid:
        gk = [k for k in ["anchor", "crowd", "clock", "c2", "c1", "c3", "c4", "a1"]
              if k in cover_path]
        grid_srcs = [cover_path[k] for k in gk[:4]] or None
        if grid_srcs:
            print(f"  grid: анимированная сетка 2×2 (лев↓/прав↑ + дрейф + шум) в hook из {len(grid_srcs)} ассетов")

    # timeline → motion-сегменты (двойная экспозиция с движением) → xfade-цепь
    seq, total = build_timeline(variant, bpm, seed, calm, split, scenario)
    print(f"  variant={variant} bpm={bpm} seed={seed} calm={calm} split={split} scenario={scenario}")
    print(f"  motion: speed={MOTION_SPEED} amp={MOTION_AMP} blend_opacity={BLEND_OPACITY}")
    print(f"  style={style['name']} — {style.get('note','')}")
    nominal = round(total, 3)
    print(f"  segments={len(seq)} nominal={nominal}s")
    segdir = WORK / "seg"; segdir.mkdir(exist_ok=True)
    seg_files, durs, trans, tdurs = [], [], [], []
    for i, s in enumerate(seq):
        sp = segdir / f"seg_{i:03d}.mp4"
        # длиннее на величину входящего перехода — xfade «съест» этот overlap, нетто=план
        enc_dur = s["dur"] + s["tdur"]
        vid = WORK / f"{s['key']}.mp4"
        if s["key"] in video_keys and vid.exists():
            ok = make_video_seg(vid, enc_dur, sp, W, H, crf=seg_crf, preset=seg_preset,
                                tint=footage_tint)
        elif grid_srcs and s["region"] in ("intro", "breath"):
            # анимированная сетка 2×2 (лев↓/прав↑ + внутренний дрейф + шум) в hook/выдохе
            ok = grid_seg(grid_srcs, enc_dur, sp, W, H, crf=seg_crf, preset=seg_preset)
        else:
            ok = motion_seg(cover_path[s["key"]], enc_dur, s["mode"], s["theta"],
                            s["blend"], sp, W, H, crf=seg_crf, preset=seg_preset)
        if not ok:
            yd_put_text(f"error: seg {i}", f"{JOB_YD}/status.txt"); sys.exit("seg fail")
        seg_files.append(sp)
        durs.append(probe_dur(sp))
        trans.append(s["tin"] or "fade")
        tdurs.append(s["tdur"])

    body = WORK / "body.mp4"
    if not xfade_chain(seg_files, durs, trans, tdurs, body, crf=body_crf, preset=body_preset):
        yd_put_text("error: body", f"{JOB_YD}/status.txt"); sys.exit("body fail")
    duration = round(probe_dur(body), 3)
    print(f"  body duration={duration}s")

    # onset каждого сегмента в финальном xfade-таймлайне (та же математика, что в xfade_chain)
    onsets, running = [0.0], durs[0]
    for i in range(1, len(seq)):
        onsets.append(max(0.0, running - tdurs[i]))
        running = running + durs[i] - tdurs[i]
    i_groove = next((i for i, s in enumerate(seq) if s["region"] == "groove"), 1)
    i_breath = next((i for i, s in enumerate(seq) if s["region"] == "breath"), len(seq) - 2)
    i_outro  = next((i for i, s in enumerate(seq) if s["region"] == "outro"),  len(seq) - 1)
    # тайм-карта текста из реального таймлайна (работает для full и short)
    w0, w1 = 0.4, 1.2
    w2 = max(w1 + 0.4, onsets[i_groove] - 0.2); w3 = w2 + 0.4   # слово-хук на held-интро
    hk0 = onsets[i_breath] + 0.4; hk1 = onsets[i_breath] + seq[i_breath]["dur"]  # хук на выдохе
    ot0 = onsets[i_outro] + 0.3                                 # аутро-подпись на тёплом кадре
    print(f"  text: word[{w0:.1f}-{w3:.1f}] hook[{hk0:.1f}-{hk1:.1f}] outro[{ot0:.1f}-{duration}]")

    # текст-слои (рукописный Caveat). enable по тайм-карте.
    fs_word  = int(W * 0.13)
    fs_hook  = int(W * 0.058)
    fs_outro = int(W * 0.05)
    fs_cred  = int(W * 0.046)   # старт-кредит «Артист — Трек»
    fs_wm    = int(W * 0.030)   # вотермарк yaromat
    hook_file  = WORK / "hook.txt";  hook_file.write_text(hook,  encoding="utf-8")
    outro_file = WORK / "outro.txt"; outro_file.write_text(outro, encoding="utf-8")

    # адаптивный контраст: яркость кадров под интро-текстом и под аутро
    luma_intro = mean_luma(cover_path["anchor"])
    luma_outro = mean_luma(cover_path.get("child", cover_path["anchor"]))
    fc_intro, bc_intro = contrast_text(luma_intro)
    fc_outro, bc_outro = contrast_text(luma_outro)
    bw_word  = max(2, int(fs_word * 0.04))
    bw_small = max(2, int(fs_outro * 0.07))
    print(f"  luma intro={luma_intro:.0f}({fc_intro}) outro={luma_outro:.0f}({fc_outro})")

    draw = []
    if word:
        draw.append(
            f"drawtext=fontfile={FONT}:text='{word}':fontcolor={fc_intro}:fontsize={fs_word}:"
            f"borderw={bw_word}:bordercolor={bc_intro}@0.6:"
            f"x=(w-text_w)/2:y=h*0.42:"
            f"alpha='if(lt(t,{w0}),0,if(lt(t,{w1}),(t-{w0})/{w1-w0:.3f},if(lt(t,{w2:.3f}),1,if(lt(t,{w3:.3f}),({w3:.3f}-t)/{w3-w2:.3f},0))))'")
    if track_credit:
        # старт: «Артист — Трек» (reference-режим), адаптивный контраст, под словом
        cred_file = WORK / "cred.txt"; cred_file.write_text(track_credit, encoding="utf-8")
        draw.append(
            f"drawtext=fontfile={FONT}:textfile={cred_file}:fontcolor={fc_intro}:fontsize={fs_cred}:"
            f"borderw={bw_small}:bordercolor={bc_intro}@0.6:"
            f"x=(w-text_w)/2:y=h*0.60:"
            f"alpha='if(lt(t,{w0}),0,if(lt(t,{w1}),(t-{w0})/{w1-w0:.3f},if(lt(t,{w2+1.2:.3f}),1,if(lt(t,{w3+1.2:.3f}),({w3+1.2:.3f}-t)/{w3-w2:.3f},0))))'")
    if hook:
        draw.append(
            f"drawtext=fontfile={FONT}:textfile={hook_file}:fontcolor=white:fontsize={fs_hook}:"
            f"line_spacing=10:box=1:boxcolor=black@0.35:boxborderw=26:"
            f"x=(w-text_w)/2:y=h*0.60:enable='between(t,{hk0:.3f},{hk1:.3f})':"
            f"alpha='if(lt(t,{hk0:.3f}),0,if(lt(t,{hk0+0.6:.3f}),(t-{hk0:.3f})/0.6,1))'")
    if outro:
        # адаптивный контраст под кадр аутро (фикс: на тёмном фоне тёмный текст пропадал)
        draw.append(
            f"drawtext=fontfile={FONT}:textfile={outro_file}:fontcolor={fc_outro}:fontsize={fs_outro}:"
            f"borderw={bw_small}:bordercolor={bc_outro}@0.6:line_spacing=8:"
            f"x=(w-text_w)/2:y=h*0.78:enable='between(t,{ot0:.3f},{duration})':"
            f"alpha='if(lt(t,{ot0+0.3:.3f}),(t-{ot0:.3f})/0.3,1)'")
    if watermark:
        # весь клип: кредит yaromat в нижнем углу (scroll-proof привязка охватов).
        # читаемость на любом фоне — белый текст + тёмная обводка, лёгкая прозрачность.
        wm_file = WORK / "wm.txt"; wm_file.write_text(watermark, encoding="utf-8")
        draw.append(
            f"drawtext=fontfile={FONT}:textfile={wm_file}:fontcolor=white@0.85:fontsize={fs_wm}:"
            f"borderw={bw_small}:bordercolor=black@0.7:"
            f"x=w-text_w-{int(W*0.04)}:y=h-text_h-{int(H*0.035)}")
    draw_chain = ("," + ",".join(draw)) if draw else ""

    # плотность: контраст/деసатурация → scratch + grit (screen) → зерно + виньетка → текст → аудио
    # уникализация текстуры per-track (тот же seed): флип/непрозрачность scratch+grit + сид/сила зерна
    # + сдвиг старта оверлеев (-ss) → «рукой» слой и зерно разные на каждом треке, square=vertical
    tex = random.Random(seed)
    scr_flip = ",hflip" if tex.random() < 0.5 else ""
    grt_flip = ",hflip" if tex.random() < 0.5 else ""
    # Сила оверлеев. Дефолты — под тусклые репо-ассеты. ЯРКИЙ оверлей с доски (635747 = нейтрал
    # 190/190/190) на этих значениях подмешивает белое в каждый кадр и ВЫСАСЫВАЕТ цвет: замер
    # 2026-07-17 — сатурация 46%→33%. Для доски передавать job["overlay_opacity"] ~0.15-0.25.
    ovl_op   = job.get("overlay_opacity")
    if ovl_op is not None:
        scr_op = grt_op = float(ovl_op)
    else:
        scr_op   = round(tex.uniform(0.5, 0.7), 2)
        grt_op   = round(tex.uniform(0.4, 0.6), 2)
    nz_str   = tex.randint(style["grain"][0], style["grain"][1])   # сила зерна — из стиля
    nz_seed  = tex.randint(1, 99999)
    scr_ss   = round(tex.uniform(0.0, 4.0), 2)   # старт scratch-петли
    grt_ss   = round(tex.uniform(0.0, 4.0), 2)   # старт grit-петли
    # грейд+виньетка — из per-track стиля (раньше были зашиты одним луком на все треки)
    grade = f"eq={style['eq']}"
    if style.get("balance"):
        grade += f",colorbalance={style['balance']}"
    vig = f"vignette={style['vignette']}," if style.get("vignette") else ""
    result = WORK / out_name
    # Аудио-фейды рассчитаны на ОДИНОЧНЫЙ клип. При сборке трека из сегментов каждый кусок принесёт
    # свой fade-in/fade-out → трек «дышит» затуханиями каждые 22с (услышано yaromat 2026-07-17).
    # job["audio_fade"]: true (деф.) | false (середина трека) | "in" (первый сегм.) | "out" (последний).
    af_mode = job.get("audio_fade", True)
    afade_out = max(0.0, duration - 1.5)
    af_parts = []
    if af_mode is True or af_mode == "in":
        af_parts.append("afade=t=in:st=0:d=0.6")
    if af_mode is True or af_mode == "out":
        af_parts.append(f"afade=t=out:st={afade_out}:d=1.5")
    af_chain = ",".join(af_parts) or "anull"
    fc = (
        f"[0:v]fps={FPS},{grade},"
        f"format=gbrp,setpts=PTS-STARTPTS[v];"
        f"[1:v]scale={W}:{H},fps={FPS},format=gray{scr_flip},format=gbrp,setpts=PTS-STARTPTS[scr];"
        f"[2:v]scale={W}:{H},fps={FPS},format=gray{grt_flip},format=gbrp,setpts=PTS-STARTPTS[grt];"
        f"[v][scr]blend=all_mode=screen:all_opacity={scr_op}[b1];"
        f"[b1][grt]blend=all_mode=screen:all_opacity={grt_op}[b2];"
        f"[b2]format=yuv420p,noise=alls={nz_str}:all_seed={nz_seed}:allf=t+u,"
        f"{vig}"
        f"trim=duration={duration},setpts=PTS-STARTPTS{draw_chain}[vout]"
    )
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(body),
        "-stream_loop", "-1", "-ss", str(scr_ss), "-i", SCRATCH,
        "-stream_loop", "-1", "-ss", str(grt_ss), "-i", GRIT,
        "-ss", str(audio_start), "-t", str(duration), "-i", str(WORK / "track.mp3"),
        "-filter_complex", fc,
        "-map", "[vout]", "-map", "3:a",
        "-af", af_chain,
        "-c:v", "libx264",
        *(["-crf", "30", "-preset", "ultrafast"] if preview
          else ["-crf", "23", "-preset", "fast", "-maxrate", "9M", "-bufsize", "18M"]),
        "-r", str(FPS),
        "-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p", "-shortest",
        str(result),
    ]
    r = run(cmd)
    if r.returncode != 0 or not result.exists() or result.stat().st_size < 5000:
        print(r.stderr[-1200:]); yd_put_text(f"error: render rc={r.returncode}", f"{JOB_YD}/status.txt"); sys.exit("render fail")

    mb = result.stat().st_size / 1024 / 1024
    print(f"  {out_name} {mb:.1f}MB")
    if not yd_put(result, f"{JOB_YD}/{out_name}"):
        yd_put_text("error: upload", f"{JOB_YD}/status.txt"); sys.exit("upload fail")
    qc = scene_qc(result, len(seq))
    print(f"  {qc}")
    yd_put_text(f"done\n{qc}", f"{JOB_YD}/status.txt")
    print(f"✅ done {out_name} ({mb:.1f}MB)")


if __name__ == "__main__":
    main()
