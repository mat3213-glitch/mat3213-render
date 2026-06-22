#!/usr/bin/env python3
"""
style_scout.py — агент-насмотренность (Фаза 3 разнообразия пула).

Тянет CC-референсы с Openverse по эстетическим запросам, алгоритмически извлекает из
кадров грейд-кандидаты (eq + colorbalance + зерно + виньетка) в формате styles.json,
строит контакт-лист (кандидаты на тест-кадре нашего футажа) и кладёт ПРЕДЛОЖЕНИЯ
(style_proposals.json + сетку) на ревью — НЕ вливает в боевой styles.json сам.
Гейт: yaromat одобряет → style_scout_merge.py мерджит выбранных в styles.json.

Запуск: GitHub Actions (style_scout.yml, еженедельно) или локально с --local.
Env (GH secrets, всё опционально — есть фолбэки):
  OPENVERSE_CLIENT_ID/SECRET — токен Openverse (иначе анонимно)
  CLOUDFLARE_WORKER, TELEGRAM_BOT_TOKEN, STYLE_SCOUT_CHAT_ID, STYLE_SCOUT_THREAD_ID — TG-пинг
  (ЯД-заливка — через rclone remote 'ydrive', настроенный в workflow)
"""
import os, sys, json, argparse, subprocess, time, urllib.request, urllib.parse
from pathlib import Path
from datetime import datetime

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
TESTFRAME = HERE / "style_scout_testframe.jpg"   # кадр нашего футажа для контакт-листа
PROPOSALS = HERE / "style_proposals.json"
OV_API = "https://api.openverse.org/v1"

QUERIES = [
    "cinematic film still moody", "faded analog film photo", "teal shadow cinematic",
    "warm vintage film grain", "desaturated film noir", "muted earthy color grade",
    "blue hour cinematic still", "sepia toned portrait film",
]

# Тренд-запросы из анализа сигналов (S3.2): подмешиваются, если одобрены через /trend_apply.
_EXTRA_Q = HERE / "extra_queries.json"
if _EXTRA_Q.exists():
    try:
        QUERIES = QUERIES + [q for q in json.loads(_EXTRA_Q.read_text(encoding="utf-8")) if q]
    except Exception:
        pass


# ── Openverse (минимальный клиент, креды из env или анонимно) ──────────────────

def ov_token() -> str | None:
    cid, csec = os.environ.get("OPENVERSE_CLIENT_ID"), os.environ.get("OPENVERSE_CLIENT_SECRET")
    if not (cid and csec):
        return None
    try:
        data = json.loads(_post(f"{OV_API}/auth_tokens/token/",
              urllib.parse.urlencode({"grant_type": "client_credentials",
                                      "client_id": cid, "client_secret": csec}).encode()))
        return data.get("access_token")
    except Exception as e:
        print(f"  [ov] token fail ({e}) — анонимно"); return None


def _post(url, data):
    req = urllib.request.Request(url, data=data,
          headers={"Content-Type": "application/x-www-form-urlencoded",
                   "User-Agent": "curl/8.5.0"})  # некоторые API/прокси режут дефолтный urllib UA
    return urllib.request.urlopen(req, timeout=20).read().decode()


def ov_search(query: str, count: int, token: str | None) -> list[str]:
    params = urllib.parse.urlencode({"q": query, "license": "cc0,pdm,by",
                                     "page_size": count, "mature": "false"})
    headers = {"User-Agent": "yaromat-style-scout/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(f"{OV_API}/images/?{params}", headers=headers)
        res = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
        return [it["url"] for it in res.get("results", []) if it.get("url")]
    except Exception as e:
        print(f"  [ov] search '{query}' fail: {e}"); return []


def ov_download(url: str, dst: Path) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "yaromat-style-scout/1.0"})
        dst.write_bytes(urllib.request.urlopen(req, timeout=30).read())
        Image.open(dst).verify()
        return True
    except Exception:
        return False


# ── Анализ кадра → грейд-кандидат ─────────────────────────────────────────────

def analyze_frame(path: Path) -> dict | None:
    try:
        im = Image.open(path).convert("RGB"); im.thumbnail((240, 240))
    except Exception:
        return None
    a = np.asarray(im, dtype=np.float32)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    luma = 0.299 * r + 0.587 * g + 0.114 * b
    mean_l = float(luma.mean())

    def cast(mask):
        if mask.sum() < 30:
            return (0.0, 0.0, 0.0)
        rr, gg, bb = r[mask].mean(), g[mask].mean(), b[mask].mean()
        m = (rr + gg + bb) / 3.0
        return ((rr - m) / 255, (gg - m) / 255, (bb - m) / 255)

    sh, mid, hi = cast(luma < 85), cast((luma >= 85) & (luma <= 170)), cast(luma > 170)
    contrast   = float(np.clip(0.9 + (luma.std() - 50) / 180, 0.90, 1.35))
    mx, mn     = a.max(axis=2), a.min(axis=2)
    sat_meas   = float(((mx - mn) / (mx + 1e-3)).mean())
    saturation = float(np.clip(0.45 + sat_meas * 0.95, 0.45, 1.10))
    brightness = float(np.clip((mean_l - 128) / 640, -0.10, 0.06))
    gamma      = float(np.clip(1.0 - (mean_l - 128) / 600, 0.90, 1.08))

    def s(c):  # колорбаланс-сдвиг, демпфированный + клип
        return round(float(np.clip(c * 0.7, -0.10, 0.10)), 3)
    parts = []
    for pre, (cr, cg, cb) in [("s", sh), ("m", mid), ("h", hi)]:
        for ch, v in zip("rgb", (cr, cg, cb)):
            sv = s(v)
            if abs(sv) >= 0.015:
                parts.append(f"{ch}{pre}={sv}")
    balance = ":".join(parts) or None

    name = _name(sh, mean_l, saturation, contrast)
    return {"name": name, "note": f"scout: {name.replace('scout_','').replace('_',' ')}",
            "eq": f"contrast={round(contrast,2)}:saturation={round(saturation,2)}:"
                  f"brightness={round(brightness,3)}:gamma={round(gamma,3)}",
            "balance": balance, "grain": [12, 17], "vignette": "angle=PI/4.5",
            "_src": path.name}


def _name(sh, mean_l, sat, contrast) -> str:
    cr, cg, cb = sh
    hue = "cool" if cb > cr and cb > 0.01 else ("warm" if cr > cb and cr > 0.01 else
          ("green" if cg > cr and cg > cb and cg > 0.01 else "neutral"))
    tone = "night" if mean_l < 95 else ("bright" if mean_l > 160 else "mid")
    tex = "muted" if sat < 0.7 else ("hard" if contrast > 1.22 else "soft")
    return f"scout_{hue}_{tone}_{tex}"


def dedupe(cands: list[dict], limit: int = 6) -> list[dict]:
    seen, out = set(), []
    for c in cands:
        key = (c["name"], round(float(c["eq"].split("contrast=")[1].split(":")[0]), 1))
        if key in seen:
            continue
        seen.add(key)
        nm, i = c["name"], 2
        while c["name"] in {o["name"] for o in out}:
            c["name"] = f"{nm}_{i}"; i += 1
        out.append(c)
        if len(out) >= limit:
            break
    return out


# ── Контакт-лист (кандидаты на тест-кадре) ────────────────────────────────────

def contact_sheet(cands: list[dict], out: Path) -> bool:
    if not TESTFRAME.exists():
        print(f"  нет тест-кадра {TESTFRAME}"); return False
    W, work = 480, Path("/tmp/scout_tiles"); work.mkdir(exist_ok=True)
    font = next((f for f in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]
                 if os.path.exists(f)), None)

    def lbl(t):
        if not font:
            return ""
        return (f",drawtext=fontfile={font}:text='{t}':x=10:y=H-34:fontsize=22:"
                f"fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=6")
    tiles = []
    for i, c in enumerate(cands):
        grade = f"eq={c['eq']}" + (f",colorbalance={c['balance']}" if c.get("balance") else "")
        vig = f",vignette={c['vignette']}" if c.get("vignette") else ""
        nz = c["grain"][0]
        vf = (f"scale={W}:{W}:force_original_aspect_ratio=increase,crop={W}:{W},"
              f"format=gbrp,{grade},format=yuv420p,noise=alls={nz}:all_seed=7:allf=t+u{vig}{lbl(c['name'])}")
        tp = work / f"t{i}.png"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(TESTFRAME),
                        "-vf", vf, str(tp)], check=False)
        if tp.exists():
            tiles.append(tp)
    if not tiles:
        return False
    cols = min(3, len(tiles)); rows = (len(tiles) + cols - 1) // cols
    inputs = []
    for t in tiles:
        inputs += ["-i", str(t)]
    layout = "|".join(f"{(i % cols) * W}_{(i // cols) * W}" for i in range(len(tiles)))
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *inputs,
                        "-filter_complex", f"xstack=inputs={len(tiles)}:layout={layout}:fill=black",
                        "-q:v", "3", str(out)], capture_output=True, text=True)  # JPG, лёгкий
    return out.exists()


# ── Доставка ──────────────────────────────────────────────────────────────────

def yd_put(local: Path, remote: str):
    subprocess.run(["rclone", "copyto", str(local), f"ydrive:{remote}"],
                   capture_output=True, text=True)


def tg_photo(img: Path, caption: str):
    # Прямой multipart sendPhoto через CF Worker (проброс /bot* → api.telegram.org).
    # БЕЗ catbox: catbox блокирует IP GH-раннеров ("Invalid uploader"). Воркер
    # сохраняет Content-Type (boundary), Telegram принимает файл напрямую. Лист —
    # лёгкий JPG, так что аплоад через воркер мгновенный.
    worker = os.environ.get("CLOUDFLARE_WORKER"); token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("STYLE_SCOUT_CHAT_ID"); thread = os.environ.get("STYLE_SCOUT_THREAD_ID")
    if not (worker and token and chat):
        print("  [tg] нет секретов — пропуск пинга"); return
    import requests
    base = {"chat_id": chat, "caption": caption[:1000]}
    if thread:
        base["message_thread_id"] = str(int(thread))
    # 1) основной: multipart sendPhoto через воркер (GH-раннер — быстрый аплоад)
    try:
        with open(img, "rb") as f:
            r = requests.post(f"{worker}/bot{token}/sendPhoto", data=base,
                              files={"photo": (img.name, f, "image/jpeg")}, timeout=180)
        if r.status_code == 200 and r.json().get("ok"):
            print("  [tg] sendPhoto (multipart) ok"); return
        print(f"  [tg] multipart не ок (HTTP {r.status_code}) — пробую catbox→relay")
    except Exception as e:
        print(f"  [tg] multipart fail ({e}) — пробую catbox→relay")
    # 2) фолбэк: catbox→/tg-relay (работает с резидентного IP; catbox режет CI-IP)
    try:
        with open(img, "rb") as f:
            up = requests.post("https://catbox.moe/user/api.php",
                               data={"reqtype": "fileupload"},
                               files={"fileToUpload": (img.name, f, "image/jpeg")},
                               timeout=120).text.strip()
        if not up.startswith("http"):
            print(f"  [tg] catbox fail: {up[:80]}"); return
        payload = {"token": token, "chat_id": chat, "file_url": up,
                   "method": "sendPhoto", "field": "photo",
                   "extra": {k: v for k, v in base.items() if k != "chat_id"}}
        hdr = {"X-Worker-Secret": os.environ.get("WORKER_SECRET", "")}  # /tg-relay требует секрет
        r = requests.post(f"{worker}/tg-relay", json=payload, headers=hdr, timeout=120)
        print(f"  [tg] sendPhoto (relay) HTTP {r.status_code}")
    except Exception as e:
        print(f"  [tg] фото не ушло: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-query", type=int, default=4, help="кадров на запрос")
    ap.add_argument("--limit", type=int, default=6, help="макс кандидатов после дедупа")
    ap.add_argument("--local", action="store_true", help="без ЯД/TG — для теста")
    args = ap.parse_args()

    ts = datetime.now().strftime("%Y-%m-%d")
    work = Path("/tmp/style_scout"); work.mkdir(exist_ok=True)
    token = ov_token()
    print(f"[scout] Openverse {'token' if token else 'анонимно'} | запросов: {len(QUERIES)}")

    imgs = []
    for q in QUERIES:
        for j, url in enumerate(ov_search(q, args.per_query, token)):
            dst = work / f"{abs(hash(url)) % 10**8}.jpg"
            if ov_download(url, dst):
                imgs.append(dst)
        time.sleep(1)
    print(f"[scout] скачано референсов: {len(imgs)}")
    if not imgs:
        sys.exit("[scout] нет референсов — выход")

    cands = [c for c in (analyze_frame(p) for p in imgs) if c]
    cands = dedupe(cands, args.limit)
    print(f"[scout] кандидатов после дедупа: {len(cands)} → {[c['name'] for c in cands]}")

    sheet = work / f"style_scout_{ts}.jpg"
    ok = contact_sheet(cands, sheet)
    print(f"[scout] контакт-лист: {'OK '+str(sheet) if ok else 'FAIL'}")

    PROPOSALS.write_text(json.dumps({"_generated": ts, "_note": "ОС yaromat → merge.py",
                                     "candidates": cands}, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    print(f"[scout] предложения → {PROPOSALS}")

    if not args.local:
        if ok:
            yd_put(sheet, f"Content factory/cloud_io/style_scout/{ts}/style_scout_{ts}.jpg")
            tg_photo(sheet, f"Style Scout {ts}: {len(cands)} новых лук-кандидатов "
                            f"({', '.join(c['name'] for c in cands)}). "
                            f"ОК? → merge в styles.json. Предложения в репо: style_proposals.json")
        yd_put(PROPOSALS, f"Content factory/cloud_io/style_scout/{ts}/style_proposals.json")


if __name__ == "__main__":
    main()
