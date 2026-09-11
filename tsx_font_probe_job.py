#!/usr/bin/env python3
"""
tsx_font_probe_job.py — проба ШРИФТОВ титра MobyTitle на реальном отрезке видео.

Рендер ONLY на GH Actions (workflow tsx_font_probe.yml). Делает:
  1. скачивает исходный вертикальный клип с ЯД
  2. режет короткий отрезок (SEG_AT..SEG_AT+SEG_DUR)
  3. для каждого titleFont-ключа из TITLE_FONT_CANDIDATES:
       рендер MobyTitle прозрачным (ProRes 4444) → композит ПОВЕРХ отрезка
  4. заливает результат на ЯД и шлёт все варианты в TG (фактори thread)

Env: SOURCE_YD, SEG_AT, SEG_DUR, AT, OVERLAY_DUR, TITLE, TG_CHAT_ID, TG_THREAD_ID
     + CLOUDFLARE_WORKER/TELEGRAM_BOT_TOKEN (s3cret — из workflow inputs)
"""
import json, os, subprocess, sys
from pathlib import Path

SOURCE_YD = os.environ.get("SOURCE_YD", "")
SEG_AT    = os.environ.get("SEG_AT", "45")
SEG_DUR   = os.environ.get("SEG_DUR", "6")
AT        = os.environ.get("AT", "1.2")
OVERLAY_DUR = os.environ.get("OVERLAY_DUR", "3.0")
TITLE     = os.environ.get("TITLE", "Submarina")
TG_CHAT   = os.environ.get("TG_CHAT_ID", "")
TG_THREAD = os.environ.get("TG_THREAD_ID", "5")

REMOTE   = "ydrive"
WORK     = Path("/tmp/tsx_font_probe"); WORK.mkdir(parents=True, exist_ok=True)
REPO     = Path(__file__).resolve().parent
REMOTION = REPO / "remotion"

FONT_KEYS = ["sans", "serif", "hand", "mono"]


def run(cmd, **kw):
    print("  $", " ".join(str(c) for c in cmd[:8]), "...", flush=True)
    return subprocess.run(cmd, **kw)


def yd_get(remote, local: Path) -> bool:
    local.parent.mkdir(parents=True, exist_ok=True)
    return run(["rclone", "copyto", f"{REMOTE}:{remote}", str(local)],
               capture_output=True, text=True).returncode == 0


def yd_put(local: Path, remote) -> None:
    rr = run(["rclone", "copyto", str(local), f"{REMOTE}:{remote}"],
             capture_output=True, text=True)
    if rr.returncode != 0:
        print(f"  ⚠ заливка {local.name} rc={rr.returncode}: {(rr.stderr or '')[:200]}", flush=True)


def send_tg(video: Path, label: str):
    worker = os.environ.get("CLOUDFLARE_WORKER"); token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not (worker and token and TG_CHAT):
        print("  [tg] секреты не заданы — пропуск"); return
    cmd = ["curl", "-sf", "-m", "120", "-F", f"chat_id={TG_CHAT}"]
    if TG_THREAD:
        cmd += ["-F", f"message_thread_id={TG_THREAD}"]
    cmd += ["-F", f"caption={label}", "-F", f"video=@{video}",
            f"{worker}/bot{token}/sendVideo"]
    rr = run(cmd, capture_output=True, text=True)
    print(f"  [tg] sendVideo rc={rr.returncode} ({video.stat().st_size//1024}KB)")


def read_approval(name: str, kind: str = "overlays"):
    """Читает <Name>.md рядом с компонентом → (approved: bool|None). Гейт yaromat."""
    md = REMOTION / "src" / kind / f"{name}.md"
    if not md.exists():
        return None
    for line in md.read_text(encoding="utf-8").splitlines():
        s = line.strip().lower()
        if s.startswith("approved:"):
            return s.split(":", 1)[1].strip() in ("yes", "true", "да")
    return False


def main():
    print(f"TSX font probe: {SOURCE_YD} · seg@{SEG_AT}+{SEG_DUR}s · overlay@{AT}s/{OVERLAY_DUR}s")

    # --- гейт апрува: титр MobyTitle одобрен yaromat? ---
    if not read_approval("MobyTitle", "overlays"):
        sys.exit("REFUSE: MobyTitle не помечен approved: yes — проба шрифтов запрещена")

    seg = WORK / "next" / "base_seg.mp4"
    if not yd_get(f"Content factory/cloud_io/{SOURCE_YD}", seg):
        sys.exit("no source")
    # отрезать короткий отрезок — ре-энкод только сегмента, не всего исходника
    base = WORK / "base.mp4"
    r = run(["ffmpeg", "-y", "-loglevel", "error", "-ss", SEG_AT, "-t", SEG_DUR,
             "-i", str(seg), "-c:v", "libx264", "-crf", "20", "-preset", "fast",
             "-c:a", "aac", "-b:a", "128k", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
             str(base)], capture_output=True, text=True)
    if r.returncode != 0 or not base.exists():
        print((r.stderr or "")[-600:]); sys.exit("segment cut fail")

    # размеры базы — для скейла оверлея
    bp = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
              "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(base)],
             capture_output=True, text=True)
    bw, bh = (int(x) for x in (bp.stdout or "").strip().split("x")[:2])

    for key in FONT_KEYS:
        # рендер MobyTitle прозрачным
        ov = WORK / f"ov_{key}.mov"
        props = {"seed": 42, "format": "vertical", "durationSec": float(OVERLAY_DUR),
                 "accentText": TITLE, "titleFont": key}
        (REMOTION / "props.json").write_text(json.dumps(props, ensure_ascii=False))
        rr = run(["npx", "remotion", "render", "src/index.ts", "MobyTitle", str(ov),
                  "--props=./props.json", "--codec=prores", "--prores-profile=4444",
                  "--image-format=png", "--pixel-format=yuva444p10le"],
                 cwd=str(REMOTION), capture_output=True, text=True)
        if rr.returncode != 0 or not ov.exists():
            print((rr.stderr or "")[-800:]); sys.exit(f"render {key} fail")

        # композит поверх отрезка
        end = round(float(AT) + float(OVERLAY_DUR), 3)
        fc = (f"[1:v]setpts=PTS-STARTPTS+{AT}/TB,scale={bw}:{bh}[ov];"
              f"[0:v][ov]overlay=0:0:enable='between(t,{AT},{end})':eof_action=pass,"
              f"format=yuv420p[v]")
        out = WORK / f"probe_{key}.mp4"
        r = run(["ffmpeg", "-y", "-loglevel", "error",
                 "-i", str(base), "-i", str(ov),
                 "-filter_complex", fc, "-map", "[v]", "-map", "0:a:0?",
                 "-c:v", "libx264", "-crf", "20", "-preset", "fast",
                 "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)],
                capture_output=True, text=True)
        if r.returncode != 0 or not out.exists() or out.stat().st_size < 5000:
            print((r.stderr or "")[-800:]); sys.exit(f"composite {key} fail")

        # залить и отправить
        yd_put(out, f"Content factory/cloud_io/tsx_font_probe/probe_{key}.mp4")
        send_tg(out, f"TSX шрифт · {key} · «{TITLE}» · {bw}x{bh} — на ревью")

    print("✅ все 4 шрифта: sans / serif / hand / mono")


if __name__ == "__main__":
    main()