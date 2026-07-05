"""
wan_i2v.py — Wan2.1 image-to-video на GH (US-IP) через HF Space + gradio_client.
Замена мёртвого Hunyuan i2v (модель снята). Watermark-free. Оживляет ОДИН стилл → клип → ЯД.

Зеркалит env-контракт и rclone-хелперы veofree_i2v_gen.py; отличие — ядро генерации не Playwright,
а gradio_client к HF Space (по умолчанию Wan-AI/Wan2.1-I2V-14B-720P). Сигнатура predict у Space
может меняться → делаем РОБАСТНО: печатаем view_api в лог + перебираем кандидатные вызовы, первый
валидный mp4 побеждает. Точный api_name можно зафиксировать env API_NAME после первого живого прогона.

Env: IMG_REMOTE (стилл на ЯД), PROMPT (движение), NEG_PROMPT, DEST_FOLDER, OUT_NAME,
     SPACE (HF Space id), API_NAME (опц.), HUGGINGFACE_TOKEN.
"""
import os, sys, time, shutil, subprocess
from pathlib import Path

from gradio_client import Client, handle_file

IMG_REMOTE = os.environ["IMG_REMOTE"]
PROMPT = os.environ.get("PROMPT", "slow subtle cinematic motion, gentle drift, film grain, no text, no people")
# дефолтный негатив из документации Wan2.1 (артефакты/лишние пальцы/статика/текст)
NEG = os.environ.get("NEG_PROMPT",
    "色调艳丽,过曝,静态,细节模糊不清,字幕,风格,作品,画作,画面,静止,整体发灰,最差质量,低质量,"
    "extra fingers, deformed hand, mutated hands, malformed, watermark, text, jpeg artifacts, "
    "still image, no motion, low quality")
DEST = os.environ.get("DEST_FOLDER", "Content factory/cloud_io/render_jobs/vzrosly_2026-07-05/wan2")
OUT = os.environ.get("OUT_NAME", "wan_clip.mp4"); OUT = OUT if OUT.endswith(".mp4") else OUT + ".mp4"
SPACE = os.environ.get("SPACE", "Wan-AI/Wan2.1-I2V-14B-720P")
API_NAME = os.environ.get("API_NAME") or None
HF_TOKEN = os.environ.get("HUGGINGFACE_TOKEN") or None
TMP = Path("/tmp/wan_i2v"); TMP.mkdir(parents=True, exist_ok=True)


def log(s): print(s, flush=True)


def yd_get(remote, local):
    r = subprocess.run(["rclone", "copyto", f"ydrive:{remote}", str(local)],
                       capture_output=True, text=True, timeout=180)
    if r.returncode == 0 and Path(local).exists():
        return True
    log(f"yd_get {remote} -> rc={r.returncode} {r.stderr[:200]}"); return False


def yd_put(local, remote):
    for _ in range(3):
        r = subprocess.run(["rclone", "copyto", str(local), f"ydrive:{remote}"],
                           capture_output=True, text=True, timeout=600)
        if r.returncode == 0:
            log(f"  up ok {remote}"); return True
        log(f"  up err rc={r.returncode} {r.stderr[:200]}"); time.sleep(4)
    return False


def resolve_video(res):
    """Достаёт путь к mp4 из результата predict (str | dict{video/path} | tuple/list)."""
    def one(x):
        if isinstance(x, str) and x.lower().endswith(".mp4"):
            return x
        if isinstance(x, dict):
            for k in ("video", "path", "name", "url"):
                v = x.get(k)
                if isinstance(v, str) and v.lower().endswith(".mp4"):
                    return v
        return None
    if isinstance(res, (list, tuple)):
        for x in res:
            p = one(x)
            if p:
                return p
        return None
    return one(res)


log(f"=== WAN2 i2v GEN === OUT={OUT} SPACE={SPACE}\nIMG={IMG_REMOTE}\nPROMPT: {PROMPT}")
try:
    import requests
    log(f"runner IP: {requests.get('https://api.ipify.org', timeout=15).text}")
except Exception:
    pass

if not yd_get(IMG_REMOTE, TMP / "in.png"):
    (TMP / f"{OUT}.FAILED.txt").write_text("no input image", encoding="utf-8")
    yd_put(TMP / f"{OUT}.FAILED.txt", f"{DEST}/{OUT}.FAILED.txt")
    raise SystemExit("no input")
log(f"input image: {(TMP/'in.png').stat().st_size//1024}KB")

status = "?"; out_local = None
try:
    client = Client(SPACE, hf_token=HF_TOKEN)
    # интроспекция api в лог — увидим реальную сигнатуру для последующей фиксации API_NAME
    try:
        log("── view_api ──")
        log(str(client.view_api(return_format="str"))[:2500])
    except Exception as e:
        log(f"view_api err: {e}")

    img = handle_file(str(TMP / "in.png"))
    # кандидатные вызовы: сначала явный API_NAME (если задан), потом типовые для i2v-Space.
    # Разные Space принимают разный порядок/набор — первый успешный с валидным mp4 побеждает.
    attempts = []
    if API_NAME:
        attempts.append(dict(api_name=API_NAME, args=(img, PROMPT)))
        attempts.append(dict(api_name=API_NAME, args=(img, PROMPT, NEG)))
    attempts += [
        dict(api_name="/generate", args=(img, PROMPT, NEG)),
        dict(api_name="/i2v_generation", args=(img, PROMPT, NEG)),
        dict(api_name="/generate", args=(img, PROMPT)),
        dict(api_name="/predict", args=(img, PROMPT)),
        dict(api_name=None, args=(img, PROMPT)),
    ]
    for a in attempts:
        try:
            log(f"predict try api_name={a['api_name']} nargs={len(a['args'])}")
            res = client.predict(*a["args"], api_name=a["api_name"]) if a["api_name"] \
                else client.predict(*a["args"])
            vid = resolve_video(res)
            if vid and Path(vid).exists() and Path(vid).stat().st_size > 10000:
                out_local = TMP / OUT
                shutil.copy(vid, out_local)
                status = "ok"
                log(f"got video {Path(vid).stat().st_size//1024}KB via api_name={a['api_name']}")
                break
            log(f"  no valid mp4 (res type={type(res).__name__})")
        except Exception as e:
            log(f"  predict err: {str(e)[:200]}")
    if status != "ok":
        status = "no-endpoint-matched"
except Exception as e:
    status = f"client-error: {str(e)[:200]}"
    log(status)

ok = False
if status == "ok" and out_local and out_local.exists():
    ok = yd_put(out_local, f"{DEST}/{OUT}")
if not ok:
    (TMP / f"{OUT}.FAILED.txt").write_text(f"status={status}", encoding="utf-8")
    yd_put(TMP / f"{OUT}.FAILED.txt", f"{DEST}/{OUT}.FAILED.txt")
log("DONE ok" if ok else f"DONE fail ({status})")
sys.exit(0 if ok else 1)
