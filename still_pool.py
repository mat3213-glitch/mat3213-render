#!/usr/bin/env python3
"""
still_pool.py — сборка ПУЛА СТИЛЛОВ под трек из стока (Openverse + Pexels) с гейтами.

Зачем отдельный инструмент: `fetch_still.py` берёт ОДИН кадр под одну сцену, а пул — это
десятки кандидатов, которые надо ещё и просеять до того, как тратить GPU. LTX-оживление
стоит ~10 минут Kaggle на шот, поэтому брак обязан отваливаться ЗДЕСЬ, а не после.

СОГЛАСОВАННОСТЬ ПУЛА = ЕДИНАЯ ЛОГИКА КАДРА (решение yaromat 2026-08-04). Поэтому запрос
несёт тип плана: `texture:sunlight on wall`. Тип едет в имя файла и в манифест, и на
контактном листе сразу видно, держится схема или пул перекосило в один тип — ровно та
болезнь, что выжгла монтаж пула v1 (14 коридоров из 26).

Гейты (те же, что в проде, не свои):
  • `ocr_gate.gate(profile="still")` — надпись в кадре;
  • `source_qc.judge_source()` — лицо крупным планом.
Оба fail-open, но «не проверено» пишется в манифест отдельно от «чисто»: молчаливый
пропуск гейта выглядит как пройденный гейт, и это дороже громкой ошибки.

Режимы:
  --queries "texture:sunlight on wall;detail:piano keys"   добыть с Openverse
  --from-yd "<путь на ЯД>"                                 просеять уже скачанное (Pexels)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "screenplay_pipeline"))

SHOT_TYPES = ("texture", "detail", "medium", "wide")


def _slug(s: str, n: int = 26) -> str:
    out = "".join(c if c.isalnum() else "_" for c in s.lower()).strip("_")
    return out[:n] or "q"


def _safe_name(s: str, n: int = 46) -> str:
    """Причина брака едет в ИМЯ файла, а в ней бывают слэши: `h=0.040/0.025/0.038`.
    Без чистки os.replace уводит файл в несуществующий подкаталог и роняет весь прогон
    (поймано боем, run 30914202230: 34 кадра из 35 остались непросеянными)."""
    out = "".join(c if (c.isalnum() or c in "-.=") else "_" for c in s)
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")[:n] or "reject"


def parse_queries(raw: str) -> list[tuple[str, str]]:
    """`texture:sunlight on wall` → ('texture', 'sunlight on wall'). Без префикса → unclear."""
    out = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        shot, _, q = chunk.partition(":")
        if q and shot.strip() in SHOT_TYPES:
            out.append((shot.strip(), q.strip()))
        else:
            out.append(("unclear", chunk))
    return out


_brand_dead: dict[str, int] = {}   # общий на прогон: иначе предохранитель квоты обнуляется


def judge_brand(path: str) -> dict:
    """VLM-судья бренда (`art_judge`) — ТРЕТИЙ слой, и он обязателен.

    🔴 Замер 04.08 на пуле `conversation inside` показал, чего НЕ ловят первые два:
    портрет маслом с лицом крупным планом прошёл `source_qc` (YOLOv8-face обучен на
    фотолицах и живопись не берёт), а одинокая фигура на ночной улице не является
    ни надписью, ни лицом — но это прямое нарушение вайба.
    Детерминированные гейты ловят ФАКТ (буквы, лицо), бренд-брак ловит только VLM.
    """
    try:
        from art_judge import judge_file
        r = judge_file(path, dead=_brand_dead)
    except Exception as exc:
        return {"ok": True, "skipped": True, "reason": f"судья недоступен: {exc}"}
    viol = r.get("violations") or []
    flaws = r.get("flaws") or []
    partial = r.get("verdict") == "PARTIAL"
    ok = r.get("verdict") != "REJECT"
    # 🔴 ЗАМЕР 04–05.08: на 15 кадрах из 43 панель дала ОДИН голос (провайдеры отваливались
    # молча), и `PARTIAL` уходил в пул как годный — так туда попал портрет маслом, которому
    # единственный судья поставил `face`. Мажоритарного порога на одном голосе нет, но и
    # молча пропускать голос за ЖЁСТКОЕ табу нельзя: лицо и надпись у нас абсолютны.
    # Поэтому третий исход — не в пул и не в брак, а на глаз владельцу.
    hard = [x for x in viol if x in ("face", "text_in_frame")]
    # `disputed` — за лицо/одинокую фигуру голос БЫЛ, но кворума не набралось. На усохшей
    # панели (05.08: надёжен один судья) молчание шумного гасит верный голос, поэтому такие
    # кадры тоже не пускаем в пул молча — они уезжают на глаз владельцу.
    disputed = [x for x in (r.get("disputed") or []) if x in ("face", "lone_figure")]
    flagged = hard if (partial and hard) else (disputed if (ok and disputed) else [])
    if flagged:
        ok = False
    return {"ok": ok, "skipped": partial, "partial_hard": bool(flagged),
            "reason": ("review_" + ",".join(flagged)) if flagged else (",".join(viol) or None),
            "flaws": ",".join(flaws) or None,
            "shot_type_vlm": r.get("shot_type"), "n_votes": r.get("n_votes", 0)}


def judge(path: str) -> dict:
    """Оба гейта по одному кадру. Возвращает вердикт + причину брака."""
    verdict = {"ok": True, "reason": None, "ocr_skipped": False, "qc_skipped": False}
    try:
        from ocr_gate import gate
        ocr = gate(path, profile="still")
        if not ocr.get("available"):
            verdict["ocr_skipped"] = True
        elif ocr.get("has_text"):
            verdict.update(ok=False, reason=f"text__{ocr.get('reason', '')[:40]}")
            return verdict
    except Exception as exc:
        verdict["ocr_skipped"] = True
        print(f"    ⚠️ ocr-гейт: {exc}")

    try:
        import source_qc
        sq = source_qc.judge_source(path)
        verdict["qc_skipped"] = bool(sq.get("qc_skipped"))
        if not sq["ok"]:
            verdict.update(ok=False, reason=f"qc__{(sq.get('reject_reason') or '')[:40]}")
    except Exception as exc:
        verdict["qc_skipped"] = True
        print(f"    ⚠️ source_qc: {exc}")
    return verdict


def fetch_openverse(queries: list[tuple[str, str]], per: int, work: Path) -> list[dict]:
    import fetch_still
    cid = os.environ.get("OPENVERSE_CLIENT_ID", "")
    sec = os.environ.get("OPENVERSE_CLIENT_SECRET", "")
    if not (cid and sec):
        print("[still_pool] нет ключей Openverse — источник пропущен", file=sys.stderr)
        return []
    token = fetch_still.get_token(cid, sec)
    got = []
    for shot, q in queries:
        try:
            items = fetch_still.search_images(q, token)
        except Exception as exc:
            print(f"  [openverse] «{q}»: поиск упал ({exc})", file=sys.stderr)
            continue
        if not items:
            print(f"  [openverse] «{q}»: пусто")
            continue
        for i, item in enumerate(items[:per]):
            url = item.get("url")
            dst = work / f"{shot}__ov_{_slug(q)}_{i}.jpg"
            if url and fetch_still.download(url, str(dst)):
                got.append({"path": dst, "shot_type": shot, "query": q, "src": "openverse",
                            "title": (item.get("title") or "")[:80],
                            "license": item.get("license", ""),
                            "creator": (item.get("creator") or "")[:40]})
    return got


def _norm(s: str) -> str:
    """`sunlight-on-wall` и `sunlight_on_wall` — одно и то же имя запроса."""
    return "".join(c for c in s.lower() if c.isalnum())


def pull_from_yd(remote: str, work: Path, shot_map: dict[str, str] | None = None) -> list[dict]:
    """Забрать уже скачанное (пул Pexels лежит на ЯД подпапками-запросами).

    🔑 Тип плана в имени папки Pexels НЕ хранится (`sunlight-on-wall`), поэтому он
    восстанавливается по карте запросов: иначе половина пула уехала бы в `unclear` и
    сводка логики кадра — главный выход этого инструмента — стала бы бессмысленной.
    """
    dst = work / "_yd"
    dst.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["rclone", "copy", f"ydrive:{remote}", str(dst),
                        "--include", "*.jpg", "--include", "*.jpeg", "--include", "*.png", "-v"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[still_pool] ЯД: {r.stderr[:200]}", file=sys.stderr)
    got = []
    for p in sorted(dst.rglob("*")):
        if p.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            continue
        q = p.parent.name
        # Тип плана ищем по убыванию надёжности: префикс ИМЕНИ ФАЙЛА (его кладём сами
        # при заливке, поэтому он переживает переливку между папками) → карта запросов
        # по имени подпапки (пул Pexels) → префикс подпапки.
        head = p.name.split("__", 1)[0]
        shot = (head if head in SHOT_TYPES else None) \
            or (shot_map or {}).get(_norm(q)) \
            or next((s for s in SHOT_TYPES if q.startswith(s)), "unclear")
        src = "openverse" if "_ov_" in p.name else "pexels"
        # Имя внутри пула всегда несёт тип плана: без этого повторный прогон по
        # объединённой папке потерял бы логику кадра — единственное, чем мы меряем
        # согласованность пула.
        target = p if p.name.startswith(f"{shot}__") else p.with_name(f"{shot}__{src}_{p.name}")
        if target != p:
            try:
                p = p.replace(target)
            except OSError:
                pass
        got.append({"path": p, "shot_type": shot, "query": q, "src": src,
                    "title": p.name, "license": "Pexels" if src == "pexels" else "CC",
                    "creator": ""})
    return got


def contact_sheet(rows: list[dict], out: Path, cols: int = 4, tile: int = 360) -> bool:
    """Плитка из кадров пула с подписью «тип плана · источник». Логика кадра видна глазом."""
    if not rows:
        return False
    work = out.parent / "_tiles"
    work.mkdir(parents=True, exist_ok=True)
    font = next((f for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                             "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf")
                 if os.path.exists(f)), None)
    tiles = []
    for i, r in enumerate(rows):
        label = f"{r['shot_type']} · {r['src']}"
        draw = ""
        if font:
            draw = (f",drawtext=fontfile={font}:text='{label}':x=8:y=H-30:fontsize=20:"
                    f"fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=5")
        tp = work / f"t{i:03d}.png"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(r["path"]),
                        "-vf", f"scale={tile}:{tile}:force_original_aspect_ratio=increase,"
                               f"crop={tile}:{tile}{draw}", str(tp)], check=False)
        if tp.exists():
            tiles.append(tp)
    if not tiles:
        return False
    cols = min(cols, len(tiles))
    inputs = []
    for t in tiles:
        inputs += ["-i", str(t)]
    layout = "|".join(f"{(i % cols) * tile}_{(i // cols) * tile}" for i in range(len(tiles)))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex",
                    f"xstack=inputs={len(tiles)}:layout={layout}:fill=black",
                    "-q:v", "3", str(out)], capture_output=True, text=True)
    return out.exists()


def main() -> int:
    ap = argparse.ArgumentParser(description="Пул стиллов под трек: сток + гейты + лист")
    ap.add_argument("--queries", default="", help="'texture:sunlight on wall;detail:piano keys'")
    ap.add_argument("--per", type=int, default=3, help="кандидатов на запрос (Openverse)")
    ap.add_argument("--from-yd", default="", help="просеять уже скачанное (папка на ЯД)")
    ap.add_argument("--out-yd", required=True, help="куда положить пул (папка на ЯД)")
    ap.add_argument("--no-brand", action="store_true",
                    help="без VLM-судьи бренда (быстро, но лицо на живописи и одинокая "
                         "фигура пройдут — их не ловят детерминированные гейты)")
    ap.add_argument("--work", default="pool_work")
    args = ap.parse_args()

    work = Path(args.work)
    (work / "ok").mkdir(parents=True, exist_ok=True)
    (work / "rejected").mkdir(parents=True, exist_ok=True)

    parsed = parse_queries(args.queries) if args.queries else []
    rows: list[dict] = []
    # Запросы нужны обоим источникам: Openverse по ним качает, а для готового пула
    # Pexels они восстанавливают тип плана по имени папки.
    if args.queries and not args.from_yd:
        rows += fetch_openverse(parsed, args.per, work)
    if args.from_yd:
        rows += pull_from_yd(args.from_yd, work, {_norm(q): shot for shot, q in parsed})
    if not rows:
        print("[still_pool] кандидатов нет", file=sys.stderr)
        return 1

    print(f"\nкандидатов: {len(rows)} — прогон через гейты (надпись + лицо)\n", flush=True)
    kept, manifest = [], []
    for i, r in enumerate(rows, 1):
        v = judge(str(r["path"]))
        name = Path(r["path"]).name
        rec = {**{k: r[k] for k in ("shot_type", "query", "src", "title", "license", "creator")},
               "file": name, "ok": v["ok"], "reject_reason": v["reason"],
               "ocr_skipped": v["ocr_skipped"], "qc_skipped": v["qc_skipped"]}
        # Бренд-судья зовётся ТОЛЬКО на переживших дешёвые гейты: он платный по времени
        # (три модели на кадр), и тратить его на кадр с очевидной надписью незачем.
        if v["ok"] and not args.no_brand:
            b = judge_brand(str(r["path"]))
            rec.update(brand_ok=b["ok"], brand_reason=b.get("reason"),
                       brand_flaws=b.get("flaws"), brand_votes=b.get("n_votes"),
                       brand_partial=b.get("skipped"), shot_type_vlm=b.get("shot_type_vlm"))
            if not b["ok"]:
                v = {**v, "ok": False, "reason": f"brand__{b['reason']}"}
                rec.update(ok=False, reject_reason=v["reason"])
        if v["ok"]:
            dst = work / "ok" / name
            kept.append(r)
            print(f"[{i}/{len(rows)}] ✅ {r['shot_type']:8} {name}", flush=True)
        else:
            dst = work / "rejected" / f"{_safe_name(v['reason'])}__{name}"
            print(f"[{i}/{len(rows)}] 🔴 {r['shot_type']:8} {name} — {v['reason']}", flush=True)
        # Один неудачный файл не должен ронять прогон: сбор пула — пакетная работа,
        # а падение на 2-м из 35 оставляет остальные 33 непросеянными.
        try:
            Path(r["path"]).replace(dst)
            r["path"] = dst
        except OSError as exc:
            print(f"      ⚠️ не перемещён: {exc}", flush=True)
        rec["path"] = str(dst.relative_to(work))
        manifest.append(rec)

    # Сводка по логике кадра — главный выход: перекос в один тип = монотонный монтаж
    dist: dict[str, int] = {}
    for r in kept:
        dist[r["shot_type"]] = dist.get(r["shot_type"], 0) + 1
    total = max(1, len(kept))
    print(f"\nгодных {len(kept)} из {len(rows)}. ЛОГИКА КАДРА:")
    for s in SHOT_TYPES + ("unclear",):
        if dist.get(s):
            print(f"  {s:8} {dist[s]:3}  {dist[s] * 100 // total:3}%")

    (work / "MANIFEST.json").write_text(
        json.dumps({"track": os.environ.get("TRACK", ""), "kept": len(kept),
                    "total": len(rows), "shot_types": dist, "items": manifest},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    sheet = work / "CONTACT_SHEET.jpg"
    if contact_sheet(kept, sheet):
        print(f"контактный лист: {sheet}")

    dest = f"ydrive:{args.out_yd}"
    for sub in ("ok", "rejected"):
        subprocess.run(["rclone", "copy", str(work / sub), f"{dest}/{sub}"], check=False)
    for f in ("MANIFEST.json", "CONTACT_SHEET.jpg"):
        if (work / f).exists():
            subprocess.run(["rclone", "copyto", str(work / f), f"{dest}/{f}"], check=False)
    print(f"\n✓ пул → {args.out_yd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
