#!/usr/bin/env python3
"""
pack_render.py — S3.3+: боевой видео-тест выбранной картинки из рендер-пака.
Берёт source N из packs/latest.json + грейд → ken-burns клип (зум) с лука → mp4 → в тред 634.

Без mimo. Вход env: INDEX (1-based). Источники/грейд — из packs/latest.json (персист render_pack).
"""
import json, os, subprocess
from pathlib import Path
import requests

HERE = Path(__file__).resolve().parent
PACKS = HERE / "packs"
WORK = Path("/tmp/pack_render"); WORK.mkdir(parents=True, exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0"}


def send_video(path, caption):
    worker = os.environ.get("CLOUDFLARE_WORKER"); token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("STYLE_SCOUT_CHAT_ID"); thread = os.environ.get("STYLE_SCOUT_THREAD_ID", "634")
    if not (worker and token and chat):
        print("[tg] нет секретов"); return
    data = {"chat_id": chat, "caption": caption[:1000]}
    if thread:
        data["message_thread_id"] = str(int(thread))
    try:
        with open(path, "rb") as f:
            r = requests.post(f"{worker}/bot{token}/sendVideo", data=data,
                              files={"video": (Path(path).name, f, "video/mp4")}, timeout=180)
        print(f"[tg] sendVideo {r.status_code}")
    except Exception as e:
        print(f"[tg] {e}")


def main():
    mf = PACKS / "latest.json"
    if not mf.exists():
        print("нет packs/latest.json — сначала /pack"); return
    pack = json.loads(mf.read_text(encoding="utf-8"))
    sources = pack.get("sources", []); grade = pack.get("grade", {})
    idx = int(os.environ.get("INDEX", "1")) - 1
    if idx < 0 or idx >= len(sources):
        print(f"индекс вне диапазона (1..{len(sources)})"); return
    url = sources[idx]
    print(f"[pack_render] N={idx+1} grade={grade.get('name','?')} url={url[:60]}")

    img = WORK / "src.jpg"
    try:
        r = requests.get(url, headers=UA, timeout=40)
        if r.status_code != 200 or len(r.content) < 1000:
            print("картинка не скачалась"); return
        img.write_bytes(r.content)
    except Exception as e:
        print(f"download fail: {e}"); return

    eq = grade.get("eq", "contrast=1.05:saturation=0.7")
    bal = grade.get("balance")
    grade_vf = f"eq={eq}" + (f",colorbalance={bal}" if bal else "")
    # ken-burns зум 6с, вертикаль 1080x1920 под Shorts
    vf = (f"scale=2160:3840:force_original_aspect_ratio=increase,crop=2160:3840,"
          f"zoompan=z='min(zoom+0.0010,1.25)':d=150:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920:fps=25,"
          f"{grade_vf},format=yuv420p,noise=alls=12:all_seed=7:allf=t+u,vignette=angle=PI/4.5")
    out = WORK / "pack_clip.mp4"
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", str(img),
                        "-t", "6", "-r", "25", "-vf", vf, "-c:v", "libx264", "-crf", "23",
                        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not out.exists():
        print((r.stderr or "")[-600:]); return
    mb = out.stat().st_size / 1024 / 1024
    print(f"[pack_render] клип {mb:.1f}MB")
    send_video(out, f"🎬 Видео-тест пака #{idx+1} · лук «{grade.get('name','?')}» · ken-burns 6с. "
                    f"Это превью лука в движении на CC-кадре.")


if __name__ == "__main__":
    main()
