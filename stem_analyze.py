#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stem_analyze.py — анализ музыкального трека по отдельным инструментам (стемам).

Зачем: при монтаже клипа, привязанном к общему BPM, сетка одна на весь трек —
и это скучно. Нужен слой меток по ОТДЕЛЬНЫМ инструментам: резать под барабаны,
менять глубину/масштаб под бас, ставить акценты под вокал. Модуль разделяет
трек через demucs (MIT) на 4 стема (drums, bass, vocals, other) и по каждому
считает onset-метки, нормализованную кривую RMS и пиковые моменты. Плюс общий
BPM и длительность. Результат — JSON, который удобно тащить в монтаж.

Почему только в CI: demucs жрёт много RAM и CPU, на слабом ноутбуке не тянет.
GitHub Actions с Linux, без GPU, с ffmpeg/librosa/numpy/rclone на борту — то,
что нужно. Локально запускать только если ноут не жалеешь.

Как встроить:
    from stem_analyze import analyze_stems, stem_cues
    data = analyze_stems("song.mp3", "stems.json")
    drum_hits = stem_cues(data, "drums", min_gap=0.25)
    # drum_hits — список времен в секундах, по ним режешь видеоряд.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from typing import Optional

import numpy as np

try:
    import librosa
except ImportError:
    sys.stderr.write(
        "librosa не установлен. Установите: pip install librosa numpy\n"
    )
    sys.exit(1)


REMOTE_CACHE_DIR = "ydrive:Content factory/cloud_io/stem_cache"
RCLONE_TIMEOUT = 300
DEMUCS_TIMEOUT = 1800
STEM_NAMES = ("drums", "bass", "vocals", "other")


def _log(msg: str) -> None:
    """Вывод прогресса в stderr по-русски."""
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def _check_demucs() -> None:
    """Проверить, что demucs доступен как CLI-модуль. Дать понятную ошибку."""
    try:
        subprocess.run(
            ["python3", "-m", "demucs", "--help"],
            capture_output=True,
            timeout=30,
            check=True,
        )
        return
    except FileNotFoundError:
        raise RuntimeError(
            "python3 не найден в PATH. Нужен Python 3.10+."
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("demucs --help завис — проверьте установку.")
    except subprocess.CalledProcessError:
        raise RuntimeError(
            "demucs не установлен или не работает.\n"
            "Установите: pip install demucs\n"
            "Документация: https://github.com/adefossez/demucs"
        )


def _cache_key(audio_path: str, model: str) -> str:
    """Ключ кэша = sha1 от (имя файла + размер + mtime + model), первые 16 hex."""
    stat = os.stat(audio_path)
    raw = f"{os.path.basename(audio_path)}:{stat.st_size}:{stat.st_mtime}:{model}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _rclone_download(remote: str, local: str, timeout: int = RCLONE_TIMEOUT) -> bool:
    """Скачать файл с remote в local. True если получилось."""
    try:
        subprocess.run(
            ["rclone", "copyto", remote, local],
            timeout=timeout,
            check=True,
            capture_output=True,
        )
        return True
    except FileNotFoundError:
        raise RuntimeError(
            "rclone не найден. Установите: https://rclone.org/install/"
        )
    except subprocess.TimeoutExpired:
        _log(f"Таймаут rclone ({timeout}с) при скачивании {remote}")
        return False
    except subprocess.CalledProcessError:
        return False


def _rclone_upload(local: str, remote: str, timeout: int = RCLONE_TIMEOUT) -> None:
    """Залить local в remote."""
    try:
        subprocess.run(
            ["rclone", "copyto", local, remote],
            timeout=timeout,
            check=True,
            capture_output=True,
        )
    except subprocess.TimeoutExpired:
        _log(f"Таймаут rclone ({timeout}с) при загрузке {remote}")
        raise
    except subprocess.CalledProcessError as e:
        _log(f"rclone ошибка при загрузке: {e}")
        raise


def _compute_onsets(y: np.ndarray, sr: int) -> list[float]:
    """Onset-метки в секундах."""
    times = librosa.onset.onset_detect(y=y, sr=sr, units="time")
    return [float(t) for t in np.atleast_1d(times).tolist()]


def _compute_rms_curve(
    y: np.ndarray, sr: int, hop_length: int
) -> list[list[float]]:
    """Кривая RMS, нормализованная 0..1, как [[t, v], ...]."""
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=hop_length)[0]
    rms_max = float(rms.max()) if rms.size else 0.0
    if rms_max > 0:
        rms_norm = rms / rms_max
    else:
        rms_norm = rms
    times = librosa.frames_to_time(np.arange(len(rms_norm)), sr=sr, hop_length=hop_length)
    return [[float(t), float(v)] for t, v in zip(times, rms_norm)]


def _compute_peaks(
    y: np.ndarray, sr: int, hop_length: int, rms_norm: Optional[np.ndarray] = None
) -> list[float]:
    """Локальные максимумы RMS выше медианы — пиковые моменты для акцентов."""
    if rms_norm is None:
        rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=hop_length)[0]
        rms_max = float(rms.max()) if rms.size else 0.0
        rms_norm = rms / rms_max if rms_max > 0 else rms
    if len(rms_norm) < 3:
        return []
    median = float(np.median(rms_norm))
    # Знаки разностей: переход с + на - означает локальный максимум.
    diff = np.diff(rms_norm)
    sign_diff = np.sign(diff)
    changes = np.diff(sign_diff)
    peak_idx = np.where(changes < 0)[0] + 1
    peak_idx = [int(i) for i in peak_idx if rms_norm[i] > median]
    times = librosa.frames_to_time(np.array(peak_idx), sr=sr, hop_length=hop_length)
    return [float(t) for t in np.atleast_1d(times).tolist()]


def analyze_stems(
    audio_path: str,
    out_json: str,
    cache: bool = True,
    device: str = "cpu",
    model: str = "htdemucs",
    demucs_timeout: int = DEMUCS_TIMEOUT,
) -> dict:
    """
    Разделить трек на стемы и посчитать метки.

    :param audio_path: путь к исходному аудио.
    :param out_json: путь для сохранения JSON-результата.
    :param cache: использовать кэш на Яндекс.Диске.
    :param device: 'cpu' или 'cuda' (в CI только cpu).
    :param model: имя модели demucs (htdemucs, htdemucs_ft, mdx_extra и т.п.).
    :param demucs_timeout: таймаут на разделение в секундах.
    :return: словарь с метками по стемам, BPM и длительностью.
    """
    if not os.path.isfile(audio_path):
        raise FileNotFoundError(f"Аудио не найдено: {audio_path}")

    _check_demucs()

    key = _cache_key(audio_path, model)
    remote_path = f"{REMOTE_CACHE_DIR}/{key}.json"

    # Попытка взять из кэша.
    if cache:
        tmp_cache = tempfile.NamedTemporaryFile(
            suffix=".json", delete=False
        ).name
        try:
            if _rclone_download(remote_path, tmp_cache):
                _log(f"Кэш найден на ЯД: {key}")
                with open(tmp_cache, "r", encoding="utf-8") as f:
                    data = json.load(f)
                os.makedirs(os.path.dirname(os.path.abspath(out_json)) or ".", exist_ok=True)
                with open(out_json, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                return data
            else:
                _log(f"Кэша нет, будем считать: {key}")
        except Exception as e:
            _log(f"Ошибка чтения кэша, считаем заново: {e}")
        finally:
            if os.path.exists(tmp_cache):
                os.unlink(tmp_cache)

    # Разделение через demucs.
    _log(f"Разделяем трек на стемы через {model} (device={device})...")
    t0 = time.time()

    with tempfile.TemporaryDirectory(prefix="stem_analyze_") as tmpdir:
        cmd = [
            "python3", "-m", "demucs",
            "--out", tmpdir,
            "--device", device,
            "-n", model,
            audio_path,
        ]
        try:
            subprocess.run(cmd, timeout=demucs_timeout, check=True, capture_output=True)
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"demucs не уложился в {demucs_timeout}с — увеличьте таймаут "
                f"или возьмите трек покороче."
            )
        _log(f"Разделение заняло {time.time() - t0:.1f}с")

        track_name = os.path.splitext(os.path.basename(audio_path))[0]
        stems_dir = os.path.join(tmpdir, model, track_name)
        if not os.path.isdir(stems_dir):
            raise RuntimeError(
                f"demucs не создал папку со стемами: {stems_dir}"
            )

        # Общие метрики по оригиналу.
        sr = 22050
        hop_length = int(0.05 * sr)  # ~50 мс
        _log("Считаем BPM и длительность по оригиналу...")
        y_full, _ = librosa.load(audio_path, sr=sr, mono=True)
        duration = float(librosa.get_duration(y=y_full, sr=sr))
        tempo, _ = librosa.beat.beat_track(y=y_full, sr=sr)
        bpm = float(np.atleast_1d(tempo).item(0))

        stems_result: dict[str, dict] = {}
        for stem_name in STEM_NAMES:
            stem_path = os.path.join(stems_dir, f"{stem_name}.wav")
            if not os.path.isfile(stem_path):
                _log(f"Стем {stem_name} не найден в выходе demucs — пропускаем")
                continue

            _log(f"  обрабатываем стем {stem_name}...")
            y, _ = librosa.load(stem_path, sr=sr, mono=True)

            onsets = _compute_onsets(y, sr)
            rms_curve = _compute_rms_curve(y, sr, hop_length)
            rms_arr = np.array([v for _, v in rms_curve], dtype=float)
            peaks = _compute_peaks(y, sr, hop_length, rms_norm=rms_arr)

            stems_result[stem_name] = {
                "onsets": onsets,
                "rms": rms_curve,
                "peaks": peaks,
            }
            _log(
                f"  {stem_name}: {len(onsets)} onsets, "
                f"{len(rms_curve)} точек RMS, {len(peaks)} пиков"
            )

    data = {
        "source": os.path.basename(audio_path),
        "duration": duration,
        "bpm": bpm,
        "model": model,
        "stems": stems_result,
    }

    # Сохраняем локально.
    out_dir = os.path.dirname(os.path.abspath(out_json))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    _log(f"Результат записан: {out_json}")

    # Заливаем в кэш.
    if cache:
        try:
            _rclone_upload(out_json, remote_path)
            _log(f"Кэш залит на ЯД: {remote_path}")
        except Exception as e:
            _log(f"Не удалось залить кэш (не критично): {e}")

    return data


def stem_cues(
    data: dict,
    stem: str,
    min_gap: float = 0.25,
    mode: str = "strongest",
) -> list[float]:
    """
    Прорядить onsets стема до меток монтажа: в каждом окне min_gap остаётся ОДНА.

    ⚠️ ЗАМЕР 2026-07-29 на «взрослый (dnb vers)», из-за которого появился `mode`:
    у drums 1003 onset'а на 196с, и **86% интервалов между сырыми onset'ами короче
    0.25с** (пики на 0.16 / 0.09 / 0.07с) — это сплошной поток транзиентов брейка,
    а не отдельные удары. Прежний режим («первый в окне») в таком потоке выбирает
    метку ПО ФАЗЕ ЛИНЕЙКИ, а не по музыке: 177 из 488 полученных интервалов легли
    ровно на пол min_gap, то есть сетка резов оказалась бы артефактом фильтра.
    Числа при этом выглядели правдоподобно — ровно тот случай, о котором
    [[feedback_number_lies_look_at_frames]]: судить по ушам/кадрам, не по метрике.

    :param data: словарь, возвращённый analyze_stems.
    :param stem: имя стема ('drums', 'bass', 'vocals', 'other').
    :param min_gap: минимальный интервал между метками в секундах.
    :param mode: 'strongest' — в окне берётся САМЫЙ ГРОМКИЙ onset (по кривой RMS
                 того же стема): метка садится на акцент, а не на случайный транзиент.
                 'first' — прежнее поведение (первый в окне), оставлено для сверки.
    :return: отфильтрованный список времён в секундах.
    """
    if stem not in data.get("stems", {}):
        raise KeyError(
            f"Стем '{stem}' отсутствует в данных. Доступны: "
            f"{list(data.get('stems', {}).keys())}"
        )
    if mode not in ("strongest", "first"):
        raise ValueError(f"mode должен быть 'strongest' или 'first', получено: {mode!r}")

    onsets = [float(t) for t in data["stems"][stem]["onsets"]]
    if mode == "first":
        result: list[float] = []
        last = -float("inf")
        for t in onsets:
            if t - last >= min_gap:
                result.append(t)
                last = t
        return result

    # rms — список пар [время, значение]; без него честно падаем в 'first'
    rms = data["stems"][stem].get("rms") or []
    if not rms or not isinstance(rms[0], (list, tuple)):
        return stem_cues(data, stem, min_gap, mode="first")
    times = [float(p[0]) for p in rms]
    vals = [float(p[1]) for p in rms]

    def strength(t: float) -> float:
        i = bisect.bisect_left(times, t)
        return vals[min(i, len(vals) - 1)]

    # Жадный отбор по силе: идём от самого громкого onset'а к тихому и берём метку,
    # если она не ближе min_gap к уже взятой. Наивное «самый громкий в окне» тут не
    # годится — окна нарезаются по началу кластера, и выбранная метка из соседних окон
    # может встать ближе min_gap (поймано тестом 29.07). Здесь интервал гарантирован
    # самим правилом отбора, а приоритет остаётся у акцентов.
    chosen: list[float] = []
    for t in sorted(onsets, key=strength, reverse=True):
        i = bisect.bisect_left(chosen, t)
        if i > 0 and t - chosen[i - 1] < min_gap:
            continue
        if i < len(chosen) and chosen[i] - t < min_gap:
            continue
        chosen.insert(i, t)
    return chosen


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Анализ трека по стемам: onset-метки, RMS, пики, BPM."
    )
    parser.add_argument("track", help="Путь к аудиофайлу (mp3/wav/flac).")
    parser.add_argument(
        "--out", default="stems.json", help="Путь для выходного JSON (по умолчанию stems.json)."
    )
    parser.add_argument(
        "--no-cache", action="store_true", help="Не использовать кэш на ЯД."
    )
    parser.add_argument(
        "--model", default="htdemucs", help="Модель demucs (по умолчанию htdemucs)."
    )
    parser.add_argument(
        "--device", default="cpu", help="Устройство для demucs (cpu/cuda)."
    )
    parser.add_argument(
        "--stem",
        choices=list(STEM_NAMES),
        help="Стем для --print-cues (drums/bass/vocals/other).",
    )
    parser.add_argument(
        "--print-cues",
        action="store_true",
        help="Вывести отфильтрованные метки выбранного стема в stdout.",
    )
    parser.add_argument(
        "--min-gap",
        type=float,
        default=0.25,
        help="Минимальный интервал между метками в секундах (по умолчанию 0.25).",
    )
    parser.add_argument(
        "--cue-mode",
        choices=("strongest", "first"),
        default="strongest",
        help="Какую метку оставлять в окне: strongest — самую громкую (по RMS стема), "
             "first — первую (прежнее поведение, для сверки).",
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if not os.path.isfile(args.track):
        _log(f"Файл не найден: {args.track}")
        sys.exit(1)

    if args.print_cues and not args.stem:
        _log("--stem обязателен вместе с --print-cues")
        sys.exit(1)

    try:
        data = analyze_stems(
            args.track,
            args.out,
            cache=not args.no_cache,
            device=args.device,
            model=args.model,
        )
    except RuntimeError as e:
        _log(f"Ошибка: {e}")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        _log("Таймаут при разделении на стемы")
        sys.exit(1)
    except Exception as e:
        _log(f"Непредвиденная ошибка: {type(e).__name__}: {e}")
        sys.exit(1)

    if args.print_cues and args.stem:
        cues = stem_cues(data, args.stem, min_gap=args.min_gap, mode=args.cue_mode)
        _log(
            f"Метки стема '{args.stem}' (min_gap={args.min_gap}с, "
            f"режим={args.cue_mode}): {len(cues)} шт."
        )
        for t in cues:
            sys.stdout.write(f"{t:.3f}\n")


if __name__ == "__main__":
    main()
