#!/usr/bin/env python3
"""
Модуль чанкового рендера видео с кэшем на Яндекс.Диске.

Зачем нужен:
    GitHub Actions режет длинные рендеры по времени. Этот модуль разбивает видео
    на независимые сегменты, рендерит каждый отдельно и кэширует результат по
    хэшу параметров. При перезапуске готовые сегменты берутся из кэша, а не
    пересчитываются заново.

Как встроить:
    1. Установи rclone и настрой remote 'ydrive:' для Яндекс.Диска.
    2. Импортируй модуль: from chunk_render import render_chunks
    3. Подготовь список сегментов:
       segments = [
           {
               'id': 'segment_001',
               'cmd': ['ffmpeg', '-i', 'input.mp4', '-ss', '0', '-t', '10', '{OUT}'],
               'params': {'input': 'input.mp4', 'start': 0, 'duration': 10}
           },
           ...
       ]
    4. Вызови: result = render_chunks(segments, 'output.mp4', workers=2, cache=True)
    5. Результат: {'ok': True/False, 'out': 'output.mp4', 'rendered': N, 'from_cache': M, ...}

Кэш хранится в: ydrive:Content factory/cloud_io/render_cache/
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional


REMOTE_CACHE = "ydrive:Content factory/cloud_io/render_cache/"
TIMEOUT_PER_CHUNK = 900
TIMEOUT_RCLONE = 300


def chunk_key(params: Dict[str, Any]) -> str:
    """
    Вычисляет ключ кэша по параметрам сегмента.
    
    Args:
        params: Словарь параметров, влияющих на результат рендера.
    
    Returns:
        Первые 16 hex-символов SHA1 от канонического JSON.
    """
    canonical = json.dumps(params, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


def _run_command(cmd: List[str], timeout: int, description: str) -> subprocess.CompletedProcess:
    """
    Выполняет команду с таймаутом и обработкой ошибок.
    
    Args:
        cmd: Команда для выполнения.
        timeout: Таймаут в секундах.
        description: Описание команды для логов.
    
    Returns:
        CompletedProcess с результатом выполнения.
    
    Raises:
        subprocess.TimeoutExpired: Если команда не уложилась в таймаут.
        subprocess.CalledProcessError: Если команда завершилась с ошибкой.
    """
    print(f"[{description}] Запуск: {' '.join(cmd[:5])}...", file=sys.stderr)
    result = subprocess.run(
        cmd,
        timeout=timeout,
        capture_output=True,
        text=True,
        check=False
    )
    if result.returncode != 0:
        print(f"[{description}] Ошибка (код {result.returncode}):", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    return result


def _get_cache_files() -> set:
    """
    Получает список файлов в кэше одним вызовом rclone lsf.
    
    Returns:
        Множество имён файлов в кэше.
    """
    try:
        result = _run_command(
            ["rclone", "lsf", REMOTE_CACHE],
            TIMEOUT_RCLONE,
            "rclone lsf"
        )
        files = set(result.stdout.strip().split("\n")) if result.stdout.strip() else set()
        print(f"[Кэш] Найдено файлов: {len(files)}", file=sys.stderr)
        return files
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"[Кэш] Не удалось получить список файлов: {e}", file=sys.stderr)
        return set()


def _download_from_cache(remote_path: str, local_path: str) -> bool:
    """
    Скачивает файл из кэша через rclone copyto.
    
    Args:
        remote_path: Путь к файлу в кэше (ydrive:.../filename.mp4).
        local_path: Локальный путь для сохранения.
    
    Returns:
        True если успешно, False иначе.
    """
    try:
        _run_command(
            ["rclone", "copyto", remote_path, local_path],
            TIMEOUT_RCLONE,
            f"rclone copyto {Path(remote_path).name}"
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"[Кэш] Ошибка скачивания {Path(remote_path).name}: {e}", file=sys.stderr)
        return False


def _upload_to_cache(local_path: str, remote_path: str) -> bool:
    """
    Загружает файл в кэш через rclone copyto.
    
    Args:
        local_path: Локальный путь к файлу.
        remote_path: Путь в кэше (ydrive:.../filename.mp4).
    
    Returns:
        True если успешно, False иначе.
    """
    try:
        _run_command(
            ["rclone", "copyto", local_path, remote_path],
            TIMEOUT_RCLONE,
            f"rclone copyto {Path(local_path).name}"
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"[Кэш] Ошибка загрузки {Path(local_path).name}: {e}", file=sys.stderr)
        return False


def _render_segment(segment: Dict[str, Any], work_dir: Path) -> tuple:
    """
    Рендерит один сегмент через ffmpeg.
    
    Args:
        segment: Описание сегмента с полями id, cmd, params.
        work_dir: Рабочая директория для временных файлов.
    
    Returns:
        Кортеж (segment_id, success: bool, error_msg: Optional[str]).
    """
    seg_id = segment["id"]
    cmd_template = segment["cmd"]
    out_file = work_dir / f"{seg_id}.mp4"
    
    cmd = [arg.replace("{OUT}", str(out_file)) for arg in cmd_template]
    
    try:
        _run_command(cmd, TIMEOUT_PER_CHUNK, f"ffmpeg {seg_id}")
        return (seg_id, True, None)
    except subprocess.TimeoutExpired:
        return (seg_id, False, "Таймаут рендера")
    except subprocess.CalledProcessError as e:
        return (seg_id, False, f"Код ошибки {e.returncode}")


def render_chunks(
    segments: List[Dict[str, Any]],
    out_path: str,
    workers: int = 2,
    cache: bool = True,
    cache_dir: Optional[str] = None
) -> Dict[str, Any]:
    """
    Рендерит видео по сегментам с кэшированием.
    
    Args:
        segments: Список сегментов. Каждый сегмент:
                  {'id': str, 'cmd': list[str], 'params': dict}
                  В cmd выходной файл обозначен плейсхолдером '{OUT}'.
        out_path: Путь для итогового склеенного видео.
        workers: Количество параллельных воркеров для рендера.
        cache: Использовать ли кэш.
        cache_dir: Локальная директория для кэша (по умолчанию временная).
    
    Returns:
        Словарь с результатами:
        {
            'ok': bool,
            'out': str,
            'rendered': int,
            'from_cache': int,
            'failed': list,
            'elapsed': float
        }
    """
    start_time = time.time()
    
    work_dir = Path(cache_dir) if cache_dir else Path(tempfile.mkdtemp(prefix="render_cache_"))
    work_dir.mkdir(parents=True, exist_ok=True)
    
    cache_files = _get_cache_files() if cache else set()
    
    segment_results = {}
    from_cache_count = 0
    to_render = []
    
    for segment in segments:
        seg_id = segment["id"]
        key = chunk_key(segment["params"])
        cache_filename = f"{seg_id}_{key}.mp4"
        local_path = work_dir / cache_filename
        remote_path = f"{REMOTE_CACHE}{cache_filename}"
        
        if cache and cache_filename in cache_files:
            print(f"[{seg_id}] Найден в кэше, скачиваю...", file=sys.stderr)
            if _download_from_cache(remote_path, str(local_path)):
                segment_results[seg_id] = local_path
                from_cache_count += 1
                continue
            else:
                print(f"[{seg_id}] Не удалось скачать из кэша, буду рендерить", file=sys.stderr)
        
        to_render.append((segment, local_path, remote_path))
    
    rendered_count = 0
    failed_segments = []
    
    if to_render:
        print(f"[Рендер] Начинаю рендер {len(to_render)} сегментов в {workers} потоков...", file=sys.stderr)
        
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_render_segment, segment, work_dir): (segment, local_path, remote_path)
                for segment, local_path, remote_path in to_render
            }
            
            for future in as_completed(futures):
                segment, local_path, remote_path = futures[future]
                seg_id, success, error_msg = future.result()
                
                if success:
                    actual_file = work_dir / f"{seg_id}.mp4"
                    if actual_file.exists():
                        actual_file.rename(local_path)
                        segment_results[seg_id] = local_path
                        rendered_count += 1
                        
                        if cache:
                            print(f"[{seg_id}] Загружаю в кэш...", file=sys.stderr)
                            _upload_to_cache(str(local_path), remote_path)
                else:
                    failed_segments.append({"id": seg_id, "error": error_msg})
                    print(f"[{seg_id}] Ошибка рендера: {error_msg}", file=sys.stderr)
    
    print(f"\n[Итог] Из кэша: {from_cache_count}, отрендерено: {rendered_count}, упало: {len(failed_segments)}", file=sys.stderr)
    
    if failed_segments:
        return {
            "ok": False,
            "out": out_path,
            "rendered": rendered_count,
            "from_cache": from_cache_count,
            "failed": failed_segments,
            "elapsed": time.time() - start_time
        }
    
    concat_list = work_dir / "concat_list.txt"
    with open(concat_list, "w", encoding="utf-8") as f:
        for segment in segments:
            seg_id = segment["id"]
            if seg_id in segment_results:
                f.write(f"file '{segment_results[seg_id]}'\n")
    
    try:
        _run_command(
            # -y обязателен: без него ffmpeg на существующем выходе ждёт ответа «Overwrite?»
            # и падает с rc=1. Ловится только ПОВТОРНЫМ прогоном — первый проходит зелёным.
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
             "-c", "copy", out_path],
            TIMEOUT_PER_CHUNK,
            "ffmpeg concat"
        )
        print(f"[Склейка] Готово: {out_path}", file=sys.stderr)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"[Склейка] Ошибка: {e}", file=sys.stderr)
        return {
            "ok": False,
            "out": out_path,
            "rendered": rendered_count,
            "from_cache": from_cache_count,
            "failed": [{"id": "concat", "error": str(e)}],
            "elapsed": time.time() - start_time
        }
    
    return {
        "ok": True,
        "out": out_path,
        "rendered": rendered_count,
        "from_cache": from_cache_count,
        "failed": [],
        "elapsed": time.time() - start_time
    }


class SegmentCache:
    """
    Кэш готовых сегментов для движков, которые рендерят их СВОИМИ функциями
    (vzrosly и подобные: там ffmpeg-команда собирается внутри, а склейка идёт
    через xfade, а не concat — поэтому render_chunks целиком им не подходит,
    а вот кэш по хэшу параметров подходит полностью).

    Листинг кэша тянется ОДИН раз на прогон (rclone lsf), дальше проверка в памяти.
    Любая ошибка кэша — не ошибка рендера: промах просто означает «рендерь сам».

        cache = SegmentCache(enabled=True)
        key = chunk_key({...всё, что влияет на кадр...})
        if not cache.fetch(key, path):
            render(...)              # свой рендер
            cache.store(key, path)
    """

    def __init__(self, enabled: bool = True, prefix: str = "seg"):
        self.enabled = enabled
        self.prefix = prefix
        self.hits = 0
        self.misses = 0
        self._files = _get_cache_files() if enabled else set()

    def _name(self, key: str) -> str:
        return f"{self.prefix}_{key}.mp4"

    def fetch(self, key: str, local_path) -> bool:
        """Достать сегмент из кэша. True — файл на месте, рендерить не надо."""
        if not self.enabled or self._name(key) not in self._files:
            self.misses += 1
            return False
        if _download_from_cache(REMOTE_CACHE + self._name(key), str(local_path)):
            self.hits += 1
            return True
        self.misses += 1
        return False

    def store(self, key: str, local_path) -> None:
        """Положить свежий сегмент в кэш. Провал заливки рендер не роняет."""
        if not self.enabled:
            return
        if _upload_to_cache(str(local_path), REMOTE_CACHE + self._name(key)):
            self._files.add(self._name(key))

    def summary(self) -> str:
        return f"кэш сегментов: {self.hits} из кэша, {self.misses} рендерилось"


def purge_cache(older_than_days: int) -> None:
    """
    Удаляет старые файлы из кэша.
    
    Args:
        older_than_days: Возраст файлов в днях для удаления.
    """
    print(f"[Очистка] Удаляю файлы старше {older_than_days} дней из кэша...", file=sys.stderr)
    
    try:
        _run_command(
            ["rclone", "delete", REMOTE_CACHE, "--min-age", f"{older_than_days}d"],
            TIMEOUT_RCLONE,
            "rclone delete"
        )
        print(f"[Очистка] Готово", file=sys.stderr)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"[Очистка] Ошибка: {e}", file=sys.stderr)


def _demo():
    """Демонстрационный рендер 3 сегментов из testsrc."""
    print("[DEMO] Генерирую 3 тестовых сегмента...", file=sys.stderr)
    
    segments = [
        {
            "id": "demo_seg_001",
            "cmd": [
                "ffmpeg", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=25",
                "-vf", "colorbalance=rs=0.5:gs=0:bs=0", "-y", "{OUT}"
            ],
            "params": {"source": "testsrc", "duration": 2, "color": "red"}
        },
        {
            "id": "demo_seg_002",
            "cmd": [
                "ffmpeg", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=25",
                "-vf", "colorbalance=rs=0:gs=0.5:bs=0", "-y", "{OUT}"
            ],
            "params": {"source": "testsrc", "duration": 2, "color": "green"}
        },
        {
            "id": "demo_seg_003",
            "cmd": [
                "ffmpeg", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=25",
                "-vf", "colorbalance=rs=0:gs=0:bs=0.5", "-y", "{OUT}"
            ],
            "params": {"source": "testsrc", "duration": 2, "color": "blue"}
        }
    ]
    
    result = render_chunks(segments, "demo_output.mp4", workers=2, cache=True)
    
    print(f"\n[DEMO] Результат:", file=sys.stderr)
    print(f"  Успешно: {result['ok']}", file=sys.stderr)
    print(f"  Выходной файл: {result['out']}", file=sys.stderr)
    print(f"  Отрендерено: {result['rendered']}", file=sys.stderr)
    print(f"  Из кэша: {result['from_cache']}", file=sys.stderr)
    print(f"  Время: {result['elapsed']:.2f}с", file=sys.stderr)
    
    if result['failed']:
        print(f"  Ошибки: {result['failed']}", file=sys.stderr)


if __name__ == "__main__":
    if "--demo" in sys.argv:
        _demo()
    else:
        print("Использование: python chunk_render.py --demo", file=sys.stderr)
        print("  --demo  Запустить демонстрационный рендер", file=sys.stderr)
