"""
Runner-side media fetcher (GitHub Actions, US-IP) — обходит блоки, которые мрут с RU-IP / CF-Worker.
Качает по списку URL и льёт на ЯД. Ключи API сюда НЕ передаются (репо публичный):
поиск/enumeration делается ЛОКАЛЬНО, на раннер уходят только готовые URL.

Env (GitHub Secrets / inputs):
  YADISK_LOGIN / YADISK_PASSWORD — WebDAV
  URLS_JSON   — JSON-список [{"url":..., "type":"pexels|pinterest|direct", "name":"file"}]
  DEST_FOLDER — папка на ЯД (напр. "Content factory/cloud_io/runner_fetch/batch1")

type:
  pexels|direct — прямой requests-download (раннер обходит CF-403 Pexels)
  pinterest     — yt-dlp (HLS-пины; работает с US-IP на URL отдельного ПИНА, не доски)
"""
import os, json, subprocess, requests
from pathlib import Path
from urllib.parse import quote as urlquote

YADISK_LOGIN = os.environ["YADISK_LOGIN"]
YADISK_PASS  = os.environ["YADISK_PASSWORD"]
URLS = json.loads(os.environ.get("URLS_JSON", "[]"))
DEST = os.environ.get("DEST_FOLDER", "Content factory/cloud_io/runner_fetch/batch")
WEBDAV = "https://webdav.yandex.ru"
AUTH = (YADISK_LOGIN, YADISK_PASS)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
TMP = Path("/tmp/fetch"); TMP.mkdir(exist_ok=True)

def yd_mkcol(path):
    # создаём дерево папок по сегментам
    parts, cur = path.split("/"), ""
    for p in parts:
        cur = f"{cur}/{p}" if cur else p
        requests.request("MKCOL", f"{WEBDAV}/{urlquote(cur)}", auth=AUTH, timeout=30)

def yd_put(local: Path, remote: str, retries: int = 4) -> bool:
    # ЯД WebDAV рвёт соединение на крупных файлах → ретраи с бэкоффом
    import time
    for attempt in range(retries):
        try:
            with open(local, "rb") as f:
                r = requests.put(f"{WEBDAV}/{urlquote(remote)}", data=f, auth=AUTH, timeout=600)
            if r.status_code in (200, 201, 204):
                print(f"  upload ok: {remote}", flush=True)
                return True
            print(f"  upload HTTP {r.status_code} (try {attempt+1})", flush=True)
        except Exception as e:
            print(f"  upload err {type(e).__name__} (try {attempt+1})", flush=True)
        time.sleep(3 * (attempt + 1))
    print(f"  upload FAIL after {retries}: {remote}", flush=True)
    return False

def fetch_direct(url: str, dest: Path) -> bool:
    r = requests.get(url, headers={"User-Agent": UA, "Referer": "https://www.pexels.com/"},
                     stream=True, timeout=(15, 180))
    if r.status_code not in (200, 206):
        print(f"  direct {r.status_code}", flush=True); return False
    with open(dest, "wb") as f:
        for ch in r.iter_content(1 << 16):
            f.write(ch)
    return dest.stat().st_size > 10000

def fetch_ytdlp(url: str, dest: Path) -> bool:
    # лучшее <=720p, склейка в mp4
    subprocess.run(["yt-dlp", "--no-warnings", "-f", "best[height<=720]/best",
                    "--merge-output-format", "mp4", "-o", str(dest), url],
                   capture_output=True, text=True, timeout=300)
    return dest.exists() and dest.stat().st_size > 10000

def main():
    print(f"DEST: {DEST}  items: {len(URLS)}", flush=True)
    yd_mkcol(DEST)
    ok = 0
    for i, item in enumerate(URLS):
        url, typ = item["url"], item.get("type", "direct")
        name = item.get("name") or f"clip_{i:02d}"
        if not name.endswith(".mp4"): name += ".mp4"
        dest = TMP / name
        print(f"\n[{i}] {typ}: {url[:80]}", flush=True)
        try:
            got = fetch_ytdlp(url, dest) if typ == "pinterest" else fetch_direct(url, dest)
        except Exception as e:
            print(f"  ERR {type(e).__name__}: {e}", flush=True); got = False
        if got:
            print(f"  got {dest.stat().st_size//1024}KB", flush=True)
            if yd_put(dest, f"{DEST}/{name}"): ok += 1
        else:
            print("  ❌ skip", flush=True)
    print(f"\n=== DONE {ok}/{len(URLS)} → {DEST} ===", flush=True)
    # маркер завершения
    Path("/tmp/_done.txt").write_text(f"{ok}/{len(URLS)}", encoding="utf-8")
    yd_put(Path("/tmp/_done.txt"), f"{DEST}/_done.txt")

if __name__ == "__main__":
    main()
