"""
Download video clips from Wikimedia Commons and upload to Yandex.Disk via WebDAV.
Used by GitHub Actions — no API key needed, CC0/CC-BY public domain content.

Env vars (set as GitHub Secrets):
  YADISK_LOGIN     — Yandex login (значение в секретах/.env)
  YADISK_PASSWORD  — WebDAV password
  QUERIES          — comma-separated search terms, e.g. "night city,rain street,fog forest"
  COUNT            — clips per query (default: 10)
  DEST_FOLDER      — ЯД folder, e.g. "Content factory/music_vibe/clips" (default)
"""

import os
import sys
import time
import json
import subprocess
import requests
from pathlib import Path
from urllib.parse import quote as urlquote

# ── Config ────────────────────────────────────────────────────────────────────
YADISK_LOGIN = os.environ["YADISK_LOGIN"]
YADISK_PASS  = os.environ["YADISK_PASSWORD"]
QUERIES_RAW  = os.environ.get("QUERIES", "rain night city")
COUNT        = int(os.environ.get("COUNT", "10"))
DEST_FOLDER  = os.environ.get("DEST_FOLDER", "Content factory/music_vibe/clips")

WEBDAV_BASE  = "https://webdav.yandex.ru"
WIKI_API     = "https://commons.wikimedia.org/w/api.php"
TIMEOUT      = 60
AUTH_YD      = (YADISK_LOGIN, YADISK_PASS)

MIN_SIZE_KB  = 500    # минимум 500KB
MAX_SIZE_KB  = 80_000 # максимум 80MB до конвертации

# ── Wikimedia search ──────────────────────────────────────────────────────────
def search_wikimedia(query: str, limit: int = 30) -> list[dict]:
    """Search Wikimedia Commons for video files, return list of {title, url, size}."""
    # Попытка 1: поиск с фильтром видео
    for search_query in [f"{query} filemime:video", query]:
        params = {
            "action":    "query",
            "list":      "search",
            "srsearch":  search_query,
            "srnamespace": 6,
            "srlimit":   limit,
            "format":    "json",
        }
        try:
            r = requests.get(WIKI_API, params=params, timeout=TIMEOUT,
                             headers={"User-Agent": "YaromatContentFactory/1.0"})
            r.raise_for_status()
            results = r.json().get("query", {}).get("search", [])
        except Exception as e:
            print(f"  [wiki] search error: {e}")
            return []

        titles = [res["title"] for res in results
                  if res["title"].lower().endswith((".webm", ".ogv", ".mp4"))]
        if titles:
            break
        print(f"  [wiki] no results for '{search_query}', trying broader...")

    if not titles:
        print(f"  [wiki] no video results for '{query}'")
        return []

    return get_video_urls(titles)


def get_video_urls(titles: list[str]) -> list[dict]:
    """Fetch direct download URLs and sizes for a list of File: titles."""
    results = []
    for batch in [titles[i:i+25] for i in range(0, len(titles), 25)]:
        params = {
            "action":  "query",
            "titles":  "|".join(batch),
            "prop":    "videoinfo",
            "viprop":  "url|size|mime",
            "format":  "json",
        }
        try:
            r = requests.get(WIKI_API, params=params, timeout=TIMEOUT,
                             headers={"User-Agent": "YaromatContentFactory/1.0"})
            r.raise_for_status()
            pages = r.json().get("query", {}).get("pages", {})
            for page in pages.values():
                vi = page.get("videoinfo", [{}])[0]
                url  = vi.get("url", "")
                size = vi.get("size", 0)
                mime = vi.get("mime", "")
                if not url or not size:
                    continue
                size_kb = size // 1024
                if size_kb < MIN_SIZE_KB or size_kb > MAX_SIZE_KB:
                    continue
                if "video" not in mime:
                    continue
                results.append({
                    "title": page.get("title", ""),
                    "url":   url,
                    "size_kb": size_kb,
                    "ext":   url.rsplit(".", 1)[-1].split("?")[0].lower(),
                })
        except Exception as e:
            print(f"  [wiki] videoinfo error: {e}")
    return results


# ── Download + convert ────────────────────────────────────────────────────────
def download(url: str, dest: Path) -> bool:
    if dest.exists() and dest.stat().st_size > MIN_SIZE_KB * 1024:
        print(f"  skip (exists): {dest.name}")
        return True
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=(10, 120), stream=True,
                             headers={"User-Agent": "YaromatContentFactory/1.0"})
            if r.status_code == 429:
                wait = 30 * (attempt + 1)
                print(f"  429 rate limit, waiting {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            tmp = dest.with_suffix(".tmp")
            size = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(65536):
                    f.write(chunk)
                    size += len(chunk)
            if size < MIN_SIZE_KB * 1024:
                tmp.unlink(missing_ok=True)
                print(f"  skip (too small {size//1024}KB): {dest.name}")
                return False
            tmp.rename(dest)
            print(f"  downloaded {size//1024}KB → {dest.name}")
            return True
        except Exception as e:
            print(f"  download error (attempt {attempt+1}): {e}")
            if attempt < 2:
                time.sleep(10)
    return False


def to_mp4(src: Path, dst: Path) -> bool:
    """Convert webm/ogv to mp4 via ffmpeg. Returns True on success."""
    if src.suffix.lower() == ".mp4":
        src.rename(dst)
        return True
    try:
        result = subprocess.run([
            "ffmpeg", "-y", "-i", str(src),
            "-c:v", "libx264", "-crf", "26", "-preset", "fast",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(dst)
        ], capture_output=True, timeout=300)
        src.unlink(missing_ok=True)
        if result.returncode != 0 or not dst.exists():
            print(f"  ffmpeg error: {result.stderr[-200:].decode()}")
            return False
        print(f"  converted → {dst.name} ({dst.stat().st_size//1024}KB)")
        return True
    except Exception as e:
        print(f"  convert error: {e}")
        return False


# ── YaDisk WebDAV ─────────────────────────────────────────────────────────────
def yd_mkdir(remote_path: str):
    url = f"{WEBDAV_BASE}/{urlquote(remote_path)}"
    requests.request("MKCOL", url, auth=AUTH_YD, timeout=30)


def yd_upload(local: Path, remote_path: str) -> bool:
    url = f"{WEBDAV_BASE}/{urlquote(remote_path)}"
    try:
        with open(local, "rb") as f:
            r = requests.put(url, data=f, auth=AUTH_YD, timeout=180)
        ok = r.status_code in (200, 201, 204)
        print(f"  upload {'ok' if ok else 'FAIL '+str(r.status_code)}: {remote_path.split('/')[-1]}")
        return ok
    except Exception as e:
        print(f"  upload error: {e}")
        return False


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    queries = [q.strip() for q in QUERIES_RAW.split(",") if q.strip()]
    print(f"Source: Wikimedia Commons (CC0/CC-BY, no API key)")
    print(f"Queries: {queries}")
    print(f"Count per query: {COUNT}")
    print(f"Destination: {DEST_FOLDER}/")

    tmp_dir = Path("/tmp/clips")
    tmp_dir.mkdir(exist_ok=True)

    total_uploaded = 0

    for query in queries:
        slug = query.lower().replace(" ", "_")[:30]
        remote_dir = f"{DEST_FOLDER}/{slug}"

        print(f"\n── query: '{query}' → /{remote_dir}")
        yd_mkdir(DEST_FOLDER)
        yd_mkdir(remote_dir)

        videos = search_wikimedia(query, limit=COUNT * 3)
        print(f"  found {len(videos)} video files on Wikimedia")

        local_dir = tmp_dir / slug
        local_dir.mkdir(exist_ok=True)

        uploaded = 0
        for i, v in enumerate(videos):
            if uploaded >= COUNT:
                break

            safe_title = v["title"].replace("File:", "").replace("/", "_")[:60]
            raw_path = local_dir / f"wiki_{slug}_{i:02d}.{v['ext']}"
            mp4_path = local_dir / f"wiki_{slug}_{i:02d}.mp4"

            print(f"\n  [{i+1}] {safe_title} ({v['size_kb']}KB)")

            if not download(v["url"], raw_path):
                continue
            if not to_mp4(raw_path, mp4_path):
                continue

            remote_path = f"{remote_dir}/{mp4_path.name}"
            if yd_upload(mp4_path, remote_path):
                uploaded += 1
                total_uploaded += 1
            mp4_path.unlink(missing_ok=True)
            time.sleep(1)

        print(f"  → uploaded {uploaded}/{COUNT} clips for '{query}'")

    print(f"\nDone. Total uploaded: {total_uploaded} clips.")
    print(f"ЯД path: {DEST_FOLDER}/")


if __name__ == "__main__":
    main()
