"""
real_cut.py — препроцессор РЕАЛЬНОГО футажа для клипа «взрослый» v3 (разворот 2026-07-06 после
провала AI-стиллов+параллакс: пластик/каша). Никакой генерации-из-стилла, никакого параллакса/слоу.

На каждый бит storyboard_v3.json готовит generated/scene_NNN.mp4 (дальше собирает storyboard_render_job):
  • src="hero"        → сегмент [in,out] из hero_raw.mp4 (реальная рука yaromat на ночном окне),
                         cover 9:16 + вариативный кроп (zoom/cx) + грейд (эскалация монтажом, не позой).
  • src="cc:<vid>"    → cc_footage/<vid>.mp4 (реальная атмосфера: дождь/пыль/комната), cover 9:16 + грейд.
  • src="black"       → чёрная вспышка t_dur (удар на пике трека; звук = сам трек).
  • грейд: "cold" (графит, приглушённый+синева) | "warm" (янтарь, катарсис). Единый LUT-вайб на клип.

Формат вертикаль 1080×1920 (рил, рука 60-70% кадра). Реальный футаж = фотографичное движение,
консистентность героя = ОДНА рука + единый грейд/кроп/match-cut (не одна поза).
Вход (ЯД render_jobs/<JOB_ID>/): storyboard_v3.json, hero_raw.mp4, cc_footage/*.mp4.
Env: JOB_ID.
"""
import json, os, subprocess, sys, tempfile
from pathlib import Path

JOB_ID = os.environ["JOB_ID"]
REMOTE = "ydrive"; CF = "Content factory"
JOB_YD = f"{CF}/cloud_io/render_jobs/{JOB_ID}"
WORK = Path(tempfile.mkdtemp(prefix="real_cut_"))
W, H, FPS = 1080, 1920, 25

GRADE = {
    # графит: приглушённая насыщенность + холодная тень/мид (без неона)
    "cold": "eq=saturation=0.72:contrast=1.07:brightness=-0.02,colorbalance=bs=0.07:bm=0.04:rs=-0.03",
    # янтарь-катарсис: тёплый сдвиг, чуть подняли гамму
    "warm": "eq=saturation=0.96:contrast=1.05:gamma=1.03,colorbalance=rm=0.11:rs=0.06:gm=0.02:bm=-0.09",
    "none": "eq=saturation=1.0",
}


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def yd_get(remote, local: Path) -> bool:
    local.parent.mkdir(parents=True, exist_ok=True)
    return sh(["rclone", "copyto", f"{REMOTE}:{remote}", str(local)]).returncode == 0


def yd_put(local: Path, remote) -> bool:
    return sh(["rclone", "copyto", str(local), f"{REMOTE}:{remote}"]).returncode == 0


def cover_vf(zoom: float, cx: float) -> str:
    """cover 9:16 + опц. zoom-кроп (>1 теснее) + горизонт. сдвиг cx (0 лево .. 1 право)."""
    vf = f"scale={W}:{H}:force_original_aspect_ratio=increase"
    # после scale ширина обычно > W (для 16:9→9:16 сильно). Кроп 1080 с оффсетом по cx.
    ox = f"(iw-{W})*{cx:.3f}"
    vf += f",crop={W}:{H}:x='{ox}':y=0"
    if zoom and zoom > 1.0:
        cw, ch = int(W / zoom), int(H / zoom)
        vf += f",crop={cw}:{ch}:(in_w-{cw})/2:(in_h-{ch})/2,scale={W}:{H}"
    return vf


def render_scene(shot: dict, hero: Path, out: Path) -> bool:
    idx = int(shot["idx"]); dur = max(0.3, float(shot["t_dur"]))
    src = shot["src"]; grade = GRADE.get(shot.get("grade", "cold"), GRADE["cold"])
    zoom = float(shot.get("zoom", 1.0)); cx = float(shot.get("cx", 0.5))
    enc = ["-r", str(FPS), "-c:v", "libx264", "-crf", "21", "-preset", "veryfast",
           "-pix_fmt", "yuv420p", "-an", str(out)]

    if src == "black":
        vf = f"format=yuv420p"
        r = sh(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                "-i", f"color=c=black:s={W}x{H}:r={FPS}", "-t", f"{dur:.3f}", "-vf", vf, *enc])
        return r.returncode == 0 and out.exists()

    if src == "hero":
        clip = hero
        ss = float(shot.get("in", 0.0))
        vf = f"{cover_vf(zoom, cx)},fps={FPS},setsar=1,{grade},format=yuv420p"
        r = sh(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{ss:.3f}", "-t", f"{dur:.3f}",
                "-i", str(clip), "-vf", vf, *enc])
        if r.returncode != 0:
            print(f"  scene {idx} hero err: {r.stderr[-250:]}", flush=True)
        return r.returncode == 0 and out.exists()

    if src.startswith("cc:"):
        vid = src.split(":", 1)[1]
        clip = WORK / f"cc_{vid}.mp4"
        if not yd_get(f"{JOB_YD}/cc_footage/{vid}.mp4", clip):
            print(f"  scene {idx}: нет cc {vid}", flush=True); return False
        vf = f"{cover_vf(zoom, cx)},fps={FPS},setsar=1,{grade},format=yuv420p"
        r = sh(["ffmpeg", "-y", "-loglevel", "error", "-stream_loop", "-1", "-t", f"{dur:.3f}",
                "-i", str(clip), "-vf", vf, *enc])
        if r.returncode != 0:
            print(f"  scene {idx} cc err: {r.stderr[-250:]}", flush=True)
        return r.returncode == 0 and out.exists()

    print(f"  scene {idx}: неизвестный src {src}", flush=True)
    return False


def main():
    sb = WORK / "storyboard_v3.json"
    if not yd_get(f"{JOB_YD}/storyboard_v3.json", sb):
        sys.exit("нет storyboard_v3.json на ЯД")
    shots = json.loads(sb.read_text(encoding="utf-8")).get("shots", [])
    hero = WORK / "hero_raw.mp4"
    need_hero = any(s["src"] == "hero" for s in shots)
    if need_hero and not yd_get(f"{JOB_YD}/hero_raw.mp4", hero):
        sys.exit("нет hero_raw.mp4 на ЯД")

    done, fail = [], []
    for shot in shots:
        idx = int(shot["idx"])
        out = WORK / f"scene_{idx:03d}.mp4"
        print(f"── scene {idx:03d}: {shot['src']} t={shot['t_dur']}s grade={shot.get('grade','cold')}", flush=True)
        if render_scene(shot, hero, out) and yd_put(out, f"{JOB_YD}/generated/scene_{idx:03d}.mp4"):
            done.append(idx); print("  ✓ → ЯД", flush=True)
        else:
            fail.append(idx); print("  ✗", flush=True)
    print(f"\nreal_cut ГОТОВО: ✅ {len(done)}  ❌ {len(fail)} ({fail or '—'})", flush=True)
    if fail:
        sys.exit(f"{len(fail)} сцен не готовы")


if __name__ == "__main__":
    main()
