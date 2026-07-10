#!/usr/bin/env python3
"""
whisper_karaoke.py — ЧИСТАЯ проверка тайминга whisperx (гейт Ф1b).

Убирает монтаж/футаж совсем (они давали ложные «фризы» на near-статичных i2v +
путали проверку). Показывает: осциллограмму РЕАЛЬНОГО аудио (showwaves) + слово,
которое вспыхивает РОВНО на своём онсете whisperx. Ни склеек, ни петель → фризов
физически нет. Синк «слово ↔ звук» виден и слышен напрямую.

Берём первый ВОКАЛЬНЫЙ кластер (до паузы >GAP), чтобы демо было коротким и плотным.
Выход: whisper_karaoke.mp4.
"""
import re
import subprocess
import sys
from pathlib import Path

import vocal_sync

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
W, H, FPS = 720, 1280, 25
GAP = 6.0          # пауза больше этой = конец кластера
MAX_DUR = 40.0     # потолок длительности демо


def safe_word(w: str) -> str:
    return (re.sub(r"[^0-9A-Za-zА-Яа-яЁё ]", "", w).strip() or "•")[:20]


def dt_escape(s: str) -> str:
    return s.replace("'", "").replace(":", "").replace("\\", "")


def main() -> int:
    audio = sys.argv[1] if len(sys.argv) > 1 else "track_audio"
    lang, words = vocal_sync.transcribe_words(audio)
    words = sorted((w for w in words if w.get("start") is not None), key=lambda w: w["start"])
    if len(words) < 2:
        print(f"[karaoke] мало слов ({len(words)})", file=sys.stderr); return 1

    # первый вокальный кластер
    cluster = [words[0]]
    for w in words[1:]:
        if w["start"] - cluster[-1]["start"] > GAP:
            break
        cluster.append(w)
    win_start = max(0.0, cluster[0]["start"] - 0.4)
    win_end = min(cluster[-1].get("end", cluster[-1]["start"] + 0.5) + 1.2,
                  win_start + MAX_DUR)
    dur = round(win_end - win_start, 2)
    print(f"[karaoke] lang={lang} слов всего={len(words)} в кластере={len(cluster)} "
          f"окно {win_start:.1f}..{win_end:.1f}с ({dur}с)")

    # karaoke-энейблы: слово держится до следующего (всегда что-то на экране)
    draw = []
    for i, w in enumerate(cluster):
        r0 = max(0.0, w["start"] - win_start)
        r1 = (cluster[i + 1]["start"] - win_start) if i + 1 < len(cluster) \
            else (w.get("end", w["start"] + 0.6) - win_start + 0.8)
        draw.append((round(r0, 3), round(r1, 3), safe_word(w["word"])))

    fc = (f"[0:a]showwaves=s={W}x340:mode=cline:colors=0x8fd0ff,format=yuv420p[wav];"
          f"color=c=0x0b0e14:s={W}x{H}:d={dur}[bg];"
          f"[bg][wav]overlay=(W-w)/2:H/2-320[v0]")
    label = "v0"
    for i, (r0, r1, word) in enumerate(draw):
        nl = f"v{i+1}"
        fc += (f";[{label}]drawtext=fontfile={FONT}:text='{dt_escape(word)}':"
               f"fontcolor=white:fontsize=72:x=(w-tw)/2:y=H/2+80:"
               f"box=1:boxcolor=0x8fd0ff@0.18:boxborderw=18:"
               f"enable='between(t,{r0},{r1})'[{nl}]")
        label = nl
    # подпись-хедер
    fc += (f";[{label}]drawtext=fontfile={FONT}:text='whisperx timing check':"
           f"fontcolor=white@0.5:fontsize=30:x=(w-tw)/2:y=120[vout]")

    out = Path("whisper_karaoke.mp4")
    cmd = ["ffmpeg", "-y", "-ss", f"{win_start:.3f}", "-t", f"{dur:.3f}", "-i", audio,
           "-filter_complex", fc, "-map", "[vout]", "-map", "0:a",
           "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if not out.exists():
        print(f"[karaoke] сборка упала: {r.stderr[-600:]}", file=sys.stderr); return 1
    print(f"[karaoke] ✅ {out} ({out.stat().st_size // 1024}КБ, {dur}с, {len(draw)} слов)")
    for r0, r1, word in draw:
        print(f"   {r0:5.2f}s  {word!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
