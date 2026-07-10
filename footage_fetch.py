#!/usr/bin/env python3
"""
footage_fetch.py — дренаж внешних ре-пинов в каталог футажа (L0, Ф4).

Бук с RU-IP не может забрать внешние источники (IG/TikTok/YT) с досок Pinterest —
catalog_build.py складывает их в pending_external.json. Этот скрипт (US-раннер GH)
тянет их yt-dlp, валидирует ffprobe, кладёт в assets/footage_catalog/<cat>/ref_<id>.mp4
и дописывает в catalog.jsonl тем же форматом → asset_catalog.pick/fetch видит их сразу.

Имя ref_<id>.mp4 → авто-блок YouTube-публикации (publish_gh: «ref_» в media = чужое).
Дедуп по id против существующего каталога. Успешные — удаляются из pending.
Per-entry try/except: один битый URL (логин-волл/канал) не роняет прогон.

Инструмент — yt-dlp (эти источники ВИДЕО; gallery-dl оставлен на image-галереи в будущем).
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

YD = "ydrive:Content factory"
CAT_DIR = "assets/footage_catalog"
MIN_DUR, MAX_DUR = 1.5, 180.0


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def yd_cat(rel):
    r = sh(["rclone", "cat", f"{YD}/{rel}"])
    return r.stdout if r.returncode == 0 else ""


def yd_put(local, rel):
    return sh(["rclone", "copyto", str(local), f"{YD}/{rel}"]).returncode == 0


def probe(path):
    r = sh(["ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "json", str(path)])
    try:
        d = json.loads(r.stdout)
        st = (d.get("streams") or [{}])[0]
        w, h = int(st.get("width", 0)), int(st.get("height", 0))
        dur = float(d.get("format", {}).get("duration", 0))
        return w, h, dur
    except Exception:
        return 0, 0, 0.0


def orient(w, h):
    if not w or not h:
        return "unknown"
    r = w / h
    return "vertical" if r < 0.9 else ("horizontal" if r > 1.15 else "square")


def download(url, cookies, work) -> Path | None:
    out = work / "dl.%(ext)s"
    cmd = ["yt-dlp", "--no-playlist", "--max-downloads", "1",
           "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
           "--merge-output-format", "mp4", "-o", str(out)]
    if cookies:
        cmd += ["--cookies", cookies]
    cmd.append(url)
    sh(cmd)
    mp4s = list(work.glob("dl.*"))
    vids = [p for p in mp4s if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")]
    return vids[0] if vids else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cookies", default="")
    ap.add_argument("--limit", type=int, default=99)
    args = ap.parse_args()

    pending = json.loads(yd_cat(f"{CAT_DIR}/pending_external.json") or "[]")
    cat_raw = yd_cat(f"{CAT_DIR}/catalog.jsonl")
    existing = {json.loads(l)["id"] for l in cat_raw.splitlines() if l.strip()}
    if not pending:
        print("[fetch] pending_external пуст — нечего дренировать")
        return 0

    work = Path(tempfile.mkdtemp(prefix="ffetch_"))
    new_entries, done_ids, fails = [], set(), []
    for e in pending[:args.limit]:
        pid = str(e.get("pin_id") or e.get("id"))
        cat = e.get("category", "overlay")
        url = e.get("source_url", "")
        if pid in existing:
            print(f"[skip] {pid} уже в каталоге"); done_ids.add(pid); continue
        print(f"[fetch] {pid} {e.get('domain')} → {url[:60]}")
        try:
            for f in work.glob("dl.*"):
                f.unlink()
            vid = download(url, args.cookies, work)
            if not vid:
                fails.append((pid, "нет видео (логин-волл/канал/недоступно)")); continue
            w, h, dur = probe(vid)
            if dur < MIN_DUR or dur > MAX_DUR or not w:
                fails.append((pid, f"невалид (dur={dur:.1f} {w}x{h})")); continue
            rel = f"{CAT_DIR}/{cat}/ref_{pid}.mp4"
            fixed = work / f"ref_{pid}.mp4"
            # перекодировать в чистый mp4/faststart (единый формат каталога)
            sh(["ffmpeg", "-y", "-i", str(vid), "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "22", "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart", str(fixed)])
            if not fixed.exists() or not yd_put(fixed, rel):
                fails.append((pid, "аплоад/энкод не вышел")); continue
            new_entries.append({
                "id": pid, "category": cat,
                "path": f"footage_catalog/{cat}/ref_{pid}.mp4",
                "source": "external", "domain": e.get("domain", ""),
                "title": e.get("title", ""), "duration": round(dur, 1),
                "width": w, "height": h, "orientation": orient(w, h),
                "blend": e.get("blend", "normal"), "tags": [],
            })
            done_ids.add(pid)
            print(f"  ✓ {cat}/ref_{pid}.mp4 {w}x{h} {dur:.1f}с")
        except Exception as ex:
            fails.append((pid, f"исключение: {ex}"))

    if new_entries:
        merged = cat_raw.rstrip("\n") + ("\n" if cat_raw.strip() else "") + \
                 "\n".join(json.dumps(e, ensure_ascii=False) for e in new_entries) + "\n"
        tmp = work / "catalog.jsonl"; tmp.write_text(merged, encoding="utf-8")
        yd_put(tmp, f"{CAT_DIR}/catalog.jsonl")
    remaining = [e for e in pending if str(e.get("pin_id") or e.get("id")) not in done_ids]
    tmp = work / "pending.json"; tmp.write_text(json.dumps(remaining, ensure_ascii=False, indent=2))
    yd_put(tmp, f"{CAT_DIR}/pending_external.json")

    print(f"\n[fetch] добавлено {len(new_entries)}, осталось pending {len(remaining)}, провалов {len(fails)}")
    for pid, why in fails:
        print(f"   ✗ {pid}: {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
