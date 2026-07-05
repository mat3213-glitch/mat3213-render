#!/usr/bin/env python3
"""
fetch_cc_clip.py — скачать Creative Commons видео с YouTube ЦЕЛИКОМ (для футажа в рендере
после уникализации), залить на Яндекс.Диск, записать манифест с атрибуцией.

Гоняется на GitHub Actions (US-IP) — скачивание YouTube с RU-IP блокируется.
Вход: references.json (список {video_id, url, title, channel, view_count}) от reference_search --cc-only.
Выход: render_jobs/<job_id>/cc_footage/<video_id>.mp4 + cc_manifest.json (атрибуция для описания поста).

Usage:
  python3 fetch_cc_clip.py --references references.json --job-id JOB --max 3 --cookies youtube_cookies.txt
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

YD = "ydrive:Content factory/cloud_io/render_jobs"


def download_clip(url: str, video_id: str, cookies: str, workdir: str):
    """yt-dlp: скачать клип ≤720p, слить в mp4. Возвращает путь к mp4 или None."""
    out_tmpl = str(Path(workdir) / f"{video_id}.%(ext)s")
    cmd = [
        "yt-dlp",
        "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "-o", out_tmpl,
    ]
    if cookies:
        cmd += ["--cookies", cookies]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[cc] yt-dlp FAIL {video_id}: {r.stderr[-300:]}", file=sys.stderr)
        return None
    mp4 = Path(workdir) / f"{video_id}.mp4"
    if mp4.exists():
        return mp4
    # мерж мог дать иное расширение — берём первый видеофайл этого id
    for f in Path(workdir).glob(f"{video_id}.*"):
        if f.suffix.lower() in (".mp4", ".mkv", ".webm"):
            return f
    return None


def upload(local: Path, dst: str) -> bool:
    r = subprocess.run(["rclone", "copyto", str(local), dst], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[cc] rclone FAIL → {dst}: {r.stderr[:200]}", file=sys.stderr)
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser(description="Скачать CC-видео с YouTube → ЯД + манифест атрибуции.")
    ap.add_argument("--references", required=True, help="references.json от reference_search --cc-only")
    ap.add_argument("--job-id", required=True, help="ID задачи (путь на ЯД)")
    ap.add_argument("--max", type=int, default=3, help="сколько клипов скачать (топ по просмотрам)")
    ap.add_argument("--cookies", default=None, help="путь к youtube_cookies.txt")
    args = ap.parse_args()

    refs = json.loads(Path(args.references).read_text(encoding="utf-8"))
    refs.sort(key=lambda x: -x.get("view_count", 0))
    refs = refs[: args.max]
    print(f"[cc] к скачиванию: {len(refs)} клипов (топ по просмотрам)")

    work = tempfile.mkdtemp(prefix="cc_")
    base = f"{YD}/{args.job_id}/cc_footage"
    manifest = []
    for r in refs:
        vid = r.get("video_id")
        url = r.get("url")
        if not vid or not url:
            continue
        print(f"[cc] качаю {vid} — {r.get('title', '')[:60]}")
        f = download_clip(url, vid, args.cookies, work)
        if not f:
            continue
        if upload(f, f"{base}/{vid}.mp4"):
            print(f"[cc] ✅ {vid}.mp4 → ЯД")
            manifest.append({
                "video_id": vid,
                "url": url,
                "title": r.get("title", ""),
                "channel": r.get("channel", ""),
                "license": "CC-BY",
                "view_count": r.get("view_count", 0),
            })

    if manifest:
        mf = Path(work) / "cc_manifest.json"
        mf.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        upload(mf, f"{base}/cc_manifest.json")
        print(f"[cc] манифест: {len(manifest)} клипов с атрибуцией")

    sys.exit(0 if manifest else 1)


if __name__ == "__main__":
    main()
