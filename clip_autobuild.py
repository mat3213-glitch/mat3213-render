#!/usr/bin/env python3
"""
clip_autobuild.py — автономная сборка клипа из частей БЕЗ БУКА (задание yaromat 2026-08-06).

Зачем: раньше цепочка держалась на буке — он ждал часть, мерил её длину, считал сдвиг и пускал
следующую. Посчитать длины заранее НЕЛЬЗЯ: кодирование округляет сегменты до кадра, xfade съедает
overlap → расчётные 22.13с против фактических 22.6с, за 7 частей 3с рассинхрона с меткам стемов.

Как решено: воркфлоу-матрица max-parallel:1 (схема LTX-пула). Каждая часть при старте читает с ЯД
ФАКТИЧЕСКИЕ длины всех предыдущих, сама считает свой audio_start, рендерит и дописывает свою длину.
Финальный шаг склеивает части и кладёт трек ОДНИМ файлом ("audio":"none" в шаблоне обязателен).

Раскладка на ЯД (CLIP_YD = cloud_io/render_jobs/<clip_id>, clip_id = "ГГГГ-ММ-ДД/имя"):
  job_template.json   — шаблон job для vzrosly_clip_job (без audio_start/out_name/seed)
  assets/             — материалы части: track.mp3, *.png, видео-ключи *.mp4
  parts/len_p<N>.json — {"part":N,"dur":22.6,"audio_start":...} ← пишет часть N, читает часть N+1
  parts/p<N>.mp4      — готовая часть (её же кладёт vzrosly в свою job-папку)
  <name>.mp4          — финал (assemble)

Режимы:
  MODE=part      env: CLIP_ID, PART_INDEX, PARTS_TOTAL, BASE_START, SEED
  MODE=assemble  env: CLIP_ID, PARTS_TOTAL, BASE_START, SEND_TG
"""
import json, os, subprocess, sys, time
from pathlib import Path

REMOTE   = "ydrive"
JOBS_YD  = "Content factory/cloud_io/render_jobs"
WORK     = Path("/tmp/clip_autobuild"); WORK.mkdir(parents=True, exist_ok=True)
REPO     = Path(__file__).resolve().parent

CLIP_ID     = os.environ.get("CLIP_ID", "")
PARTS_TOTAL = int(os.environ.get("PARTS_TOTAL", "0") or 0)
BASE_START  = float(os.environ.get("BASE_START", "0") or 0)
CLIP_YD     = f"{JOBS_YD}/{CLIP_ID}"


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def yd_get(remote: str, local: Path) -> bool:
    local.parent.mkdir(parents=True, exist_ok=True)
    return run(["rclone", "copyto", f"{REMOTE}:{remote}", str(local)]).returncode == 0


def yd_put(local: Path, remote: str) -> bool:
    return run(["rclone", "copyto", str(local), f"{REMOTE}:{remote}"]).returncode == 0


def yd_put_text(text: str, remote: str) -> bool:
    p = WORK / "_tmp.txt"; p.write_text(text, encoding="utf-8")
    return yd_put(p, remote)


def probe_dur(path: Path) -> float:
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)])
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def read_part_len(idx: int, tries: int = 6, pause: float = 10.0) -> float:
    """Фактическая длина части idx с ЯД. Ретраи — ЯД отдаёт запись не мгновенно
    (eventual consistency, [[reference_known_pitfalls]]): между концом части N и стартом N+1
    проходит минута на setup раннера, но полагаться на это нельзя."""
    dst = WORK / f"len_p{idx}.json"
    for t in range(tries):
        if yd_get(f"{CLIP_YD}/parts/len_p{idx}.json", dst):
            try:
                return float(json.loads(dst.read_text())["dur"])
            except Exception as e:
                print(f"  len_p{idx}.json битый: {e}", flush=True)
        if t < tries - 1:
            print(f"  жду len_p{idx}.json ({t + 1}/{tries})…", flush=True)
            time.sleep(pause)
    sys.exit(f"нет длины части {idx} — предыдущая часть не дописала результат, сдвиг считать не из чего")


# ── режим part ────────────────────────────────────────────────────────────────
def mode_part():
    idx = int(os.environ["PART_INDEX"])
    seed_base = int(os.environ.get("SEED", "2013"))
    print(f"[part {idx}/{PARTS_TOTAL}] clip={CLIP_ID}", flush=True)

    # 1. сдвиг = база + СУММА ФАКТИЧЕСКИХ длин предыдущих частей
    prev = [read_part_len(i) for i in range(1, idx)]
    audio_start = round(BASE_START + sum(prev), 3)
    print(f"  предыдущие длины: {[round(p, 3) for p in prev]} → audio_start={audio_start}", flush=True)

    # 2. job части = шаблон + свои поля
    tpl = WORK / "job_template.json"
    if not yd_get(f"{CLIP_YD}/job_template.json", tpl):
        sys.exit("нет job_template.json на ЯД")
    job = json.loads(tpl.read_text())
    out_name = f"p{idx}.mp4"
    # "audio":"none" ставится ПРИНУДИТЕЛЬНО, что бы ни лежало в шаблоне: часть со звуком принесёт
    # свой fade и провал RMS на шве (замер 06.08 — ноль звука ~2с на каждом из шести швов).
    job.update({"audio_start": audio_start, "out_name": out_name,
                "seed": seed_base + idx * 17, "audio": "none", "audio_fade": False})

    job_id = f"{CLIP_ID}_p{idx}"
    job_yd = f"{JOBS_YD}/{job_id}"

    # 3. материалы: серверная копия ЯД→ЯД (бук/раннер файлы не качает)
    r = run(["rclone", "copy", f"{REMOTE}:{CLIP_YD}/assets", f"{REMOTE}:{job_yd}"])
    if r.returncode != 0:
        sys.exit(f"assets не скопировались: {r.stderr[-400:]}")
    n_src = len(run(["rclone", "lsf", f"{REMOTE}:{CLIP_YD}/assets"]).stdout.split())
    n_dst = len(run(["rclone", "lsf", f"{REMOTE}:{job_yd}"]).stdout.split())
    print(f"  assets: {n_src} в источнике → {n_dst} в job-папке", flush=True)
    if n_dst < n_src:
        sys.exit("assets скопировались НЕ полностью — «Завершено» ≠ всё залито")

    jf = WORK / "job.json"; jf.write_text(json.dumps(job, ensure_ascii=False, indent=1))
    if not yd_put(jf, f"{job_yd}/job.json"):
        sys.exit("job.json не залился")

    # 4. рендер части штатным движком
    env = dict(os.environ, JOB_ID=job_id)
    r = subprocess.run([sys.executable, "-u", str(REPO / "vzrosly_clip_job.py")], env=env)
    if r.returncode != 0:
        yd_put_text(f"error: part {idx} rc={r.returncode}", f"{CLIP_YD}/status.txt")
        sys.exit(f"часть {idx} не отрендерилась")

    # 5. ФАКТИЧЕСКАЯ длина → на ЯД (её ждёт следующая часть)
    local = Path("/tmp/vzrosly_job") / out_name
    if not local.exists():
        sys.exit(f"нет готового файла {local}")
    dur = round(probe_dur(local), 3)
    if dur <= 0:
        sys.exit("ffprobe не дал длину части")
    if not yd_put(local, f"{CLIP_YD}/parts/{out_name}"):
        sys.exit("часть не залилась в parts/")
    lf = WORK / f"len_p{idx}.json"
    lf.write_text(json.dumps({"part": idx, "dur": dur, "audio_start": audio_start}))
    if not yd_put(lf, f"{CLIP_YD}/parts/len_p{idx}.json"):
        sys.exit("длина части не залилась — следующая часть встанет")
    print(f"✅ часть {idx}: {dur}s (audio_start={audio_start} → конец {round(audio_start + dur, 3)})", flush=True)


# ── режим assemble ────────────────────────────────────────────────────────────
def send_tg(video: Path, caption: str):
    """Пинг с раннера через CF Worker (api.telegram.org с RU-IP закрыт; на раннере egress чистый,
    но канал держим единый). Best-effort: клип уже на ЯД, падать из-за TG незачем."""
    worker = os.environ.get("CLOUDFLARE_WORKER")
    token  = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat   = os.environ.get("TG_CHAT_ID")
    thread = os.environ.get("TG_THREAD_ID", "")
    if not (worker and token and chat):
        print("  [tg] секреты не заданы — пропуск"); return
    proxy = WORK / "tg_proxy.mp4"
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
         "-vf", "scale=-2:1280", "-c:v", "libx264", "-crf", "30", "-preset", "veryfast",
         "-c:a", "aac", "-b:a", "96k", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(proxy)])
    send = proxy if proxy.exists() and proxy.stat().st_size > 5000 else video
    cmd = ["curl", "-sf", "-m", "180", "-F", f"chat_id={chat}"]
    if thread:
        cmd += ["-F", f"message_thread_id={thread}"]
    cmd += ["-F", f"caption={caption}", "-F", f"video=@{send}", f"{worker}/bot{token}/sendVideo"]
    rr = run(cmd)
    print(f"  [tg] sendVideo rc={rr.returncode} ({send.stat().st_size // 1024}KB)")


def mode_assemble():
    name = os.environ.get("OUT_NAME") or f"{CLIP_ID.split('/')[-1]}.mp4"
    print(f"[assemble] {CLIP_ID} → {name}", flush=True)

    parts, durs = [], []
    for i in range(1, PARTS_TOTAL + 1):
        p = WORK / f"p{i}.mp4"
        if not yd_get(f"{CLIP_YD}/parts/p{i}.mp4", p):
            sys.exit(f"нет части {i} на ЯД")
        d = probe_dur(p)
        parts.append(p); durs.append(d)
        print(f"  p{i}: {d:.3f}s", flush=True)

    # склейка: части рендерились одним кодом и одними параметрами → concat без перекодирования
    lst = WORK / "concat.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in parts))
    body = WORK / "body.mp4"
    r = run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", str(lst), "-c", "copy", str(body)])
    if r.returncode != 0 or not body.exists():
        print(f"  concat -c copy не прошёл ({r.stderr[-300:]}) — перекодирую", flush=True)
        r = run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                 "-i", str(lst), "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
                 "-pix_fmt", "yuv420p", str(body)])
        if r.returncode != 0:
            sys.exit(f"склейка не собралась: {r.stderr[-500:]}")

    total = probe_dur(body)
    drift = total - sum(durs)
    print(f"  склейка: {total:.3f}s (сумма частей {sum(durs):.3f}s, расхождение {drift:+.3f}s)", flush=True)
    if abs(drift) > 0.5:
        print(f"  ⚠️ расхождение {drift:+.3f}s больше полукадра-допуска — смотреть швы", flush=True)

    # трек ОДНИМ файлом поверх готовой склейки (никаких фейдов на швах)
    track = WORK / "track.mp3"
    if not yd_get(f"{CLIP_YD}/assets/track.mp3", track):
        sys.exit("нет track.mp3 в assets")
    final = WORK / name
    r = run(["ffmpeg", "-y", "-loglevel", "error",
             "-i", str(body), "-ss", str(BASE_START), "-t", f"{total:.3f}", "-i", str(track),
             "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-shortest", str(final)])
    if r.returncode != 0 or not final.exists():
        sys.exit(f"трек не лёг: {r.stderr[-500:]}")

    # mp4 «наружу» — только yuv420p + faststart (грабля 06.08: файлы не открылись у yaromat)
    pf = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
              "-show_entries", "stream=pix_fmt", "-of", "default=nw=1:nk=1", str(final)]).stdout.strip()
    fs = run(["ffprobe", "-v", "error", "-show_entries", "format_tags=major_brand",
              "-of", "default=nw=1:nk=1", str(final)]).stdout.strip()
    mb = final.stat().st_size / 1048576
    fdur = probe_dur(final)
    print(f"  финал: {fdur:.3f}s {mb:.1f}MB pix_fmt={pf} brand={fs}", flush=True)
    if pf != "yuv420p":
        sys.exit(f"pix_fmt={pf} — такой файл не откроется у плееров, чинить фильтры")

    if not yd_put(final, f"{CLIP_YD}/{name}"):
        sys.exit("финал не залился на ЯД")
    yd_put_text(f"done\nparts={PARTS_TOTAL} dur={fdur:.3f} size={mb:.1f}MB", f"{CLIP_YD}/status.txt")
    print(f"✅ {name} на ЯД: {CLIP_YD}/{name}", flush=True)

    if os.environ.get("SEND_TG", "true").lower() == "true":
        send_tg(final, f"{name} · {fdur:.0f}с · {PARTS_TOTAL} частей · автосборка в облаке")


if __name__ == "__main__":
    if not CLIP_ID:
        sys.exit("CLIP_ID не задан")
    mode = os.environ.get("MODE", "part")
    {"part": mode_part, "assemble": mode_assemble}.get(mode, lambda: sys.exit(f"режим {mode}?"))()
