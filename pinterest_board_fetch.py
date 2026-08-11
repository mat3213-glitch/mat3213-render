#!/usr/bin/env python3
"""
Pinterest board fetch for GitHub Actions.

Fetches every video reachable from a board or pin.it shortlink, then uploads the
raw result into a YaD folder that already follows the date-first project rule.

The script is self-contained so the workflow can run from the
`github_actions_clips` checkout without depending on the parent repo.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import shutil
from collections import Counter
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except Exception as exc:  # pragma: no cover - runtime env only
    raise SystemExit(f"playwright import failed: {exc}")

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TMP = Path("/tmp/pinterest_board_fetch")


def sh(cmd: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def load_cookies() -> list[dict]:
    cookie_file = Path(__file__).parent / "Instrument" / "Pinterest" / "cookies" / "pinterest_cookies.txt"
    if not cookie_file.exists():
        return []
    cookies: list[dict] = []
    with cookie_file.open() as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            domain, _, path, secure, expires, name, value = parts[:7]
            if "pinterest.com" not in domain:
                continue
            cookie = {
                "name": name,
                "value": value,
                "domain": domain,
                "path": path,
                "secure": secure.upper() == "TRUE",
            }
            try:
                exp = int(expires)
            except ValueError:
                exp = 0
            if exp > 0:
                cookie["expires"] = exp
            cookies.append(cookie)
    return cookies


def board_url_and_slug(board_ref: str) -> tuple[str, str]:
    ref = board_ref.strip()
    if "://" in ref:
        url = ref
    elif ref.startswith("pin.it/"):
        url = f"https://{ref}"
    else:
        url = f"https://www.pinterest.com/yaromat/{ref}/"
    m = re.search(r"pinterest\.com/[^/]+/([^/?#]+)/?", url)
    slug = m.group(1) if m else url.rstrip("/").split("/")[-1]
    return url, slug


def walk_pins(obj, out: dict):
    if isinstance(obj, dict):
        pid = obj.get("id")
        if pid and isinstance(pid, str) and pid.isdigit() and ("videos" in obj or "images" in obj or "pinner" in obj):
            from urllib.parse import unquote

            bd = obj.get("board") or {}
            burl = unquote(bd.get("url") or "") if isinstance(bd, dict) else ""
            info = out.setdefault(pid, {"video_url": "", "link": "", "domain": "", "title": "", "board_url": ""})
            info["board_url"] = info["board_url"] or burl
            info["link"] = info["link"] or (obj.get("link") or "")
            info["domain"] = info["domain"] or (obj.get("domain") or "")
            info["title"] = info["title"] or (obj.get("title") or obj.get("grid_title") or "")
            vids = obj.get("videos") or {}
            vlist = (vids.get("video_list") or {}) if isinstance(vids, dict) else {}
            best = ""
            for q in ("V_720P", "V_EXP7", "V_HLSV4", "V_HLSV3_MOBILE", "V_HLSV3_WEB"):
                if isinstance(vlist.get(q), dict) and vlist[q].get("url"):
                    best = vlist[q]["url"]
                    break
            if not best:
                for v in vlist.values():
                    if isinstance(v, dict) and v.get("url"):
                        best = v["url"]
                        break
            info["video_url"] = info["video_url"] or best
        for v in obj.values():
            walk_pins(v, out)
    elif isinstance(obj, list):
        for v in obj:
            walk_pins(v, out)


def collect_pins(board_ref: str, max_idle: int = 6) -> dict:
    pins: dict = {}
    bodies: list[bytes] = []
    board_url, board_slug = board_url_and_slug(board_ref)

    def on_response(r):
        if "BoardFeedResource" in r.url or "BoardResource" in r.url:
            try:
                bodies.append(r.body())
            except Exception:
                pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 1000})
        cookies = load_cookies()
        if cookies:
            ctx.add_cookies(cookies)
        page = ctx.new_page()
        page.on("response", on_response)
        page.goto(board_url, wait_until="load", timeout=40000)
        time.sleep(3)
        resolved_url = page.url or board_url
        pin_match = re.search(r"pinterest\.com/pin/(\d+)/?", resolved_url)
        if pin_match:
            pid = pin_match.group(1)
            pins[pid] = {
                "video_url": "",
                "link": resolved_url,
                "domain": "pinterest.com",
                "title": "",
                "board_url": resolved_url,
            }
            browser.close()
            return pins
        if "pinterest.com/" in resolved_url:
            board_slug = re.search(r"pinterest\.com/[^/]+/([^/?#]+)/?", resolved_url).group(1) if re.search(r"pinterest\.com/[^/]+/([^/?#]+)/?", resolved_url) else board_slug
        idle = 0
        for _ in range(60):
            before = len(bodies)
            page.evaluate("window.scrollBy(0, 1500)")
            time.sleep(1.2)
            idle = idle + 1 if len(bodies) == before else 0
            if idle >= max_idle:
                break
        browser.close()

    for raw in bodies:
        try:
            walk_pins(json.loads(raw.decode("utf-8", "replace")), pins)
        except Exception:
            pass

    want = f"/{board_slug}/".lower()
    scoped = {pid: i for pid, i in pins.items() if want in (i.get("board_url") or "").lower()}
    seen_ig, dedup = set(), {}
    for pid, i in scoped.items():
        link = i.get("link") or ""
        m = re.search(r"instagram\.com/(?:p|reel)/([^/?]+)", link)
        if m:
            if m.group(1) in seen_ig:
                continue
            seen_ig.add(m.group(1))
        dedup[pid] = i
    return dedup


def download_native(video_url: str, dst: Path) -> bool:
    try:
        if ".m3u8" in video_url:
            r = sh(["yt-dlp", "--no-warnings", "-q", "-o", str(dst), video_url], timeout=600)
            return r.returncode == 0 and dst.exists() and dst.stat().st_size > 1000
        r = sh(["curl", "-fsSL", "-A", UA, video_url, "-o", str(dst)], timeout=300)
        return r.returncode == 0 and dst.exists() and dst.stat().st_size > 1000
    except Exception as exc:
        print(f"[board] download_native error: {exc}", flush=True)
        return False


def ytdlp_url(url: str, dst: Path) -> bool:
    cmd = [
        "yt-dlp", "--no-warnings", "-q",
        "-o", str(dst.with_suffix(".%(ext)s")),
        "--merge-output-format", "mp4",
        url,
    ]
    try:
        sh(cmd, timeout=900)
    except Exception:
        return False
    hit = dst if dst.exists() else next(iter(dst.parent.glob(dst.stem + ".*")), None)
    if hit and hit.stat().st_size > 1000:
        if hit != dst:
            hit.rename(dst)
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", default="pin.it/3wpVSWYhI")
    ap.add_argument("--dest-folder", required=True, help="YaD destination folder, no ydrive: prefix")
    args = ap.parse_args()

    shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    dest_folder = args.dest_folder.rstrip("/")
    raw_remote = f"ydrive:{dest_folder}/raw"
    sh(["rclone", "mkdir", raw_remote], timeout=120)

    print(f"[board] collect: {args.board}", flush=True)
    pins = collect_pins(args.board)
    print(f"[board] pins={len(pins)}", flush=True)

    tasks: list[dict] = []
    got = 0
    for pid, info in pins.items():
        link = info.get("link") or ""
        vurl = info.get("video_url") or ""
        dom = info.get("domain") or ""
        if vurl:
            method, url = "native", vurl
        elif re.search(r"(instagram|tiktok|youtube|youtu\.be|vimeo)", link):
            method, url = "external", link
        else:
            method, url = "pin_page", f"https://www.pinterest.com/pin/{pid}/"

        dst = TMP / f"ref_{pid}.mp4"
        ok = False
        if method == "native" and vurl.endswith(".mp4"):
            ok = download_native(vurl, dst)
        elif method == "native":
            ok = ytdlp_url(f"https://www.pinterest.com/pin/{pid}/", dst)
        else:
            ok = ytdlp_url(url, dst) or ytdlp_url(f"https://www.pinterest.com/pin/{pid}/", dst)
        task = {"pin_id": pid, "method": method, "url": url, "domain": dom, "title": info.get("title", ""), "ok": ok}
        tasks.append(task)
        if ok:
            got += 1
            print(f"[board] ✓ {pid} {method} {dom}", flush=True)
        else:
            print(f"[board] ✗ {pid} {method} {dom}", flush=True)

    (TMP / "fetch_tasks.json").write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")
    (TMP / "manifest.json").write_text(
        json.dumps(
            [{"pin_id": t["pin_id"], "kind": t["method"], "domain": t["domain"], "title": t["title"]} for t in tasks],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (TMP / "run_meta.json").write_text(
        json.dumps(
            {"board": args.board, "dest_folder": dest_folder, "count": len(tasks), "downloaded": got},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    for local in sorted(TMP.iterdir()):
        if not local.is_file():
            continue
        remote_file = f"{raw_remote}/{local.name}"
        r = sh(["rclone", "copyto", str(local), remote_file], timeout=300)
        if r.returncode != 0:
            print(r.stderr[-1000:], flush=True)
            return 2

    kinds = Counter(t["method"] for t in tasks)
    print(f"[board] uploaded raw -> {raw_remote}", flush=True)
    print(f"[board] summary count={len(tasks)} got={got} kinds={dict(kinds)}", flush=True)
    return 0 if got else 1


if __name__ == "__main__":
    raise SystemExit(main())
