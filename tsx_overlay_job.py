#!/usr/bin/env python3
"""
tsx_overlay_job.py — GH Actions runner: накладывает TSX-ОВЕРЛЕЙ (графический хук
с альфой) поверх базового клипа в нужный момент. Это ОСНОВНОЕ применение TSX
(à la CapCut: акценты/переходы/динамика), не полнокадровый текст.

Флоу: рендер overlay-композиции прозрачной (ProRes 4444) → ffmpeg overlay на base в [at..at+dur].

С ЯД (render_jobs/<JOB_ID>/): job.json, <base_clip>.mp4
Из репо: remotion/ (оверлеи)

job.json:
  {"overlay":"FocusBracket", "format":"vertical|square", "out_name":"...mp4",
   "base_clip":"base.mp4", "at":3.0, "overlay_dur":2.0, "seed":7,
   "palette":["#..",...] (опц), "accent_text":"..." (опц)}

Env: JOB_ID + (опц.) CLOUDFLARE_WORKER/TELEGRAM_BOT_TOKEN/TG_CHAT_ID/TG_THREAD_ID
"""
import json, os, subprocess, sys
from pathlib import Path

JOB_ID = os.environ.get("JOB_ID", "")
if not JOB_ID:
    sys.exit("JOB_ID not set")

REMOTE   = "ydrive"
JOB_YD   = f"Content factory/cloud_io/render_jobs/{JOB_ID}"
WORK     = Path("/tmp/tsx_overlay"); WORK.mkdir(parents=True, exist_ok=True)
REPO     = Path(__file__).resolve().parent
REMOTION = REPO / "remotion"


def run(cmd, **kw):
    print("  $", " ".join(str(c) for c in cmd[:8]), "...", flush=True)
    return subprocess.run(cmd, **kw)

def yd_get(remote, local: Path) -> bool:
    local.parent.mkdir(parents=True, exist_ok=True)
    return run(["rclone", "copyto", f"{REMOTE}:{remote}", str(local)],
               capture_output=True, text=True).returncode == 0

def yd_put(local: Path, remote) -> bool:
    return run(["rclone", "copyto", str(local), f"{REMOTE}:{remote}"],
               capture_output=True, text=True).returncode == 0

def yd_put_text(text, remote):
    t = WORK / "_s.txt"; t.write_text(text); yd_put(t, remote)


def read_approval(name: str, kind: str = "overlays"):
    """Читает <Name>.md рядом с компонентом → (approved: bool|None, text).
    approved=None означает «README нет». Ранер ОБЯЗАН вызвать перед использованием хука."""
    md = REMOTION / "src" / kind / f"{name}.md"
    if not md.exists():
        return None, ""
    text = md.read_text(encoding="utf-8")
    approved = False
    for line in text.splitlines():
        s = line.strip().lower()
        if s.startswith("approved:"):
            approved = s.split(":", 1)[1].strip() in ("yes", "true", "да")
            break
    return approved, text


def send_tg(result: Path, label: str):
    """Пинг превью в TG С РАННЕРА (чистый egress). Best-effort."""
    worker = os.environ.get("CLOUDFLARE_WORKER"); token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat   = os.environ.get("TG_CHAT_ID"); thread = os.environ.get("TG_THREAD_ID", "")
    if not (worker and token and chat):
        print("  [tg] секреты не заданы — пропуск"); return
    proxy = WORK / "tg_proxy.mp4"
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(result),
         "-vf", "scale=-2:1280", "-c:v", "libx264", "-crf", "30", "-preset", "veryfast",
         "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(proxy)],
        capture_output=True, text=True)
    send = proxy if proxy.exists() and proxy.stat().st_size > 5000 else result
    cmd = ["curl", "-sf", "-m", "120", "-F", f"chat_id={chat}"]
    if thread:
        cmd += ["-F", f"message_thread_id={thread}"]
    cmd += ["-F", f"caption={label}", "-F", f"video=@{send}",
            f"{worker}/bot{token}/sendVideo"]
    rr = run(cmd, capture_output=True, text=True)
    print(f"  [tg] sendVideo rc={rr.returncode} ({send.stat().st_size//1024}KB)")


def main():
    print(f"TSX overlay job: {JOB_ID}")
    jf = WORK / "job.json"
    if not yd_get(f"{JOB_YD}/job.json", jf):
        sys.exit("no job.json")
    job = json.loads(jf.read_text())

    overlay    = job["overlay"]
    fmt        = job.get("format", "vertical")
    out_name   = job["out_name"]
    base_clip  = job.get("base_clip", "base.mp4")
    at         = float(job.get("at", 3.0))
    ov_dur     = float(job.get("overlay_dur", 2.0))
    seed       = int(job.get("seed", 42))
    print(f"  overlay={overlay} fmt={fmt} base={base_clip} at={at}s dur={ov_dur}s seed={seed}")

    # --- гейт апрува: ранер читает README хука ПЕРЕД использованием ---
    approved, _ = read_approval(overlay, "overlays")
    allow = bool(job.get("allow_unapproved", False))
    if approved is None:
        yd_put_text(f"refused: no README for {overlay}", f"{JOB_YD}/status.txt")
        sys.exit(f"REFUSE: у хука {overlay} нет README — добавь remotion/src/overlays/{overlay}.md с полем approved:")
    print(f"  approval: {'YES' if approved else 'NO'} (allow_unapproved={allow})")
    if not approved and not allow:
        yd_put_text(f"refused: {overlay} not prod-approved", f"{JOB_YD}/status.txt")
        sys.exit(f"REFUSE: {overlay} помечен approved: no — прод-использование запрещено. "
                 f"Для ревью-рендера поставь \"allow_unapproved\": true в job.json")
    approval_tag = "" if approved else " ⚠ НЕ ПРОД-АПРУВ"

    base = WORK / "base.mp4"
    if not yd_get(f"{JOB_YD}/{base_clip}", base):
        sys.exit(f"no base {base_clip}")

    # props оверлея
    props = {"seed": seed, "format": fmt, "durationSec": ov_dur}
    if job.get("palette"):     props["palette"] = job["palette"]
    if job.get("accent_text"): props["accentText"] = job["accent_text"]
    (REMOTION / "props.json").write_text(json.dumps(props, ensure_ascii=False))

    # рендер оверлея ПРОЗРАЧНЫМ (ProRes 4444 несёт альфу)
    # --image-format=png: кадры в PNG (а не JPEG) → несёт альфу
    # --pixel-format=yuva444p10le: encoder пишет АЛЬФА-ПЛОСКОСТЬ (y u v a) в ProRes
    #   Без этого флага ProRes 4444 кодируется без alpha → чёрный непрозрачный слой.
    ov = WORK / "overlay.mov"
    r = run(["npx", "remotion", "render", "src/index.ts", overlay, str(ov),
             "--props=./props.json", "--codec=prores", "--prores-profile=4444",
             "--image-format=png", "--pixel-format=yuva444p10le"],
            cwd=str(REMOTION))
    if r.returncode != 0 or not ov.exists():
        yd_put_text(f"error: overlay render rc={r.returncode}", f"{JOB_YD}/status.txt")
        sys.exit("overlay render fail")
    print(f"  overlay.mov {ov.stat().st_size//1024}KB")

    # --- диагностика альфы: ffprobe pix_fmt overlay.mov ---
    pf = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
              "-show_entries", "stream=pix_fmt", "-of", "csv=p=0", str(ov)],
             capture_output=True, text=True)
    pix_fmt = (pf.stdout or "").strip()
    print(f"  overlay.mov pix_fmt = {pix_fmt}")
    has_alpha = "a" in pix_fmt.lower()  # yuva444p10le → True
    if not has_alpha:
        print(f"  ⚠ WARN: pix_fmt={pix_fmt} — альфа-канал ОТСУТСТВУЕТ, композит будет непрозрачным!")
    else:
        print(f"  ✓ alpha channel present ({pix_fmt})")

    # размеры базы: оверлей рендерится в FORMAT_DIMS (напр. 1080x1920), а база может
    # быть меньше (VeoFree i2v = 720x1280). Тот же 9:16, но overlay=0:0 без скейла обрезал
    # бы оверлей и увёл графику за кадр — поэтому скейлим оверлей ПОД размер базы.
    bp = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
              "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(base)],
             capture_output=True, text=True)
    try:
        bw, bh = (int(x) for x in (bp.stdout or "").strip().split("x")[:2])
        scale_ov = f",scale={bw}:{bh}"
        print(f"  base dims {bw}x{bh} → scale overlay to match")
    except Exception:
        scale_ov = ""  # не смогли определить — не скейлим (совпадающие размеры)
        print("  ⚠ base dims неизвестны — оверлей без скейла")

    # композит: сдвинуть оверлей на [at], показать [at..at+dur], сохранить аудио базы
    end = round(at + ov_dur, 3)
    fc = (f"[1:v]setpts=PTS-STARTPTS+{at}/TB{scale_ov}[ov];"
          f"[0:v][ov]overlay=0:0:enable='between(t,{at},{end})':eof_action=pass,format=yuv420p[v]")
    result = WORK / out_name
    r = run(["ffmpeg", "-y", "-loglevel", "error",
             "-i", str(base), "-i", str(ov),
             "-filter_complex", fc, "-map", "[v]", "-map", "0:a:0?",
             "-c:v", "libx264", "-crf", "23", "-preset", "fast",
             "-c:a", "copy", "-movflags", "+faststart", str(result)],
            capture_output=True, text=True)
    if r.returncode != 0 or not result.exists() or result.stat().st_size < 5000:
        print((r.stderr or "")[-800:])
        yd_put_text(f"error: composite rc={r.returncode}", f"{JOB_YD}/status.txt")
        sys.exit("composite fail")

    mb = result.stat().st_size / 1024 / 1024
    print(f"  {out_name} {mb:.1f}MB")
    if not yd_put(result, f"{JOB_YD}/{out_name}"):
        yd_put_text("error: upload", f"{JOB_YD}/status.txt"); sys.exit("upload fail")
    yd_put_text("done", f"{JOB_YD}/status.txt")
    print(f"✅ done {out_name} ({mb:.1f}MB)")

    # доставка на гейт = ЯД-подпапка сессии (job.preview_dir), НЕ TG (правило yaromat 2026-07-08)
    prev_dir = job.get("preview_dir")
    if prev_dir:
        if yd_put(result, f"{prev_dir.rstrip('/')}/{out_name}"):
            print(f"  ✅ превью в сессионную папку: {prev_dir}")
        else:
            print(f"  ⚠ не удалось скопировать в preview_dir {prev_dir}")

    # TG-пинг гейта ВЫКЛЮЧЕН по умолчанию (yaromat: «на гейт в тг не скидывай»).
    # Включить точечно можно env GATE_TO_TG=1.
    if os.environ.get("GATE_TO_TG"):
        try:
            send_tg(result, f"TSX overlay · {overlay} · {fmt} · @{at}s — на ревью{approval_tag}")
        except Exception as e:
            print(f"  [tg] ping err: {e} (клип на ЯД — не критично)")


if __name__ == "__main__":
    main()
