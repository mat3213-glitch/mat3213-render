#!/usr/bin/env python3
"""Generate one LTX image-to-video clip on Kaggle and copy it to Yandex Disk."""

import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image


TARGET_WIDTH = 480
TARGET_HEIGHT = 704
TARGET_LUMA = 120.0
POLL_SECONDS = 60
TIMEOUT_SECONDS = 45 * 60


def fail(message: str) -> None:
    raise RuntimeError(message)


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        fail(f"Required environment variable {name} is missing or empty.")
    return value


def run(command: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a command without exposing environment secrets in diagnostics."""
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as exc:
        fail(f"Required command is not installed: {command[0]} ({exc})")
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        fail(f"Command failed ({command[0]}), exit {result.returncode}: {detail[-2000:]}")
    return result


def sanitize_slug(slug: str) -> str:
    """Привести слаг к тому виду, в который его всё равно превратит Kaggle.

    Kaggle держит слаги строчными и из [a-z0-9-]. Наш job_id содержит подчёркивания
    (ltx_smoke_2026-07-28), и на пуше они молча становятся дефисами — а `kernels status`
    с исходным именем потом отвечает «Permission kernels.get was denied», что выглядит
    как проблема доступа, хотя это просто НЕ ТОТ слаг. Ловили на смоуке 2026-07-28.
    """
    owner, _, name = slug.partition("/")
    name = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not name:
        fail(f"Kernel slug has an empty name part after sanitising: {slug!r}")
    return f"{owner.lower()}/{name[:50].rstrip('-')}"


def prepare_kaggle_credentials(username: str, key: str) -> None:
    kaggle_dir = Path.home() / ".kaggle"
    try:
        kaggle_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        config = kaggle_dir / "kaggle.json"
        config.write_text(json.dumps({"username": username, "key": key}), encoding="utf-8")
        config.chmod(0o600)
    except OSError as exc:
        fail(f"Could not write Kaggle credentials to {kaggle_dir}: {exc}")


def crop_resize_and_lift(source: Path) -> Image.Image:
    """Center crop, resize, and apply the gamma lift required by LTX I2V."""
    try:
        with Image.open(source) as opened:
            image = opened.convert("RGB")
    except (OSError, ValueError) as exc:
        fail(f"Could not load input still {source}: {exc}")

    width, height = image.size
    if width <= 0 or height <= 0:
        fail(f"Input still has invalid dimensions: {width}x{height}.")
    target_ratio = TARGET_WIDTH / TARGET_HEIGHT
    source_ratio = width / height
    if source_ratio > target_ratio:
        crop_width = int(round(height * target_ratio))
        left = (width - crop_width) // 2
        image = image.crop((left, 0, left + crop_width, height))
    else:
        crop_height = int(round(width / target_ratio))
        top = (height - crop_height) // 2
        image = image.crop((0, top, width, top + crop_height))
    # Image.Resampling появился в Pillow 9.1 — на старых сборках (бук) падает AttributeError
    lanczos = getattr(Image, "Resampling", Image).LANCZOS
    image = image.resize((TARGET_WIDTH, TARGET_HEIGHT), lanczos)

    pixels = np.asarray(image, dtype=np.float32) / 255.0

    def luma_for_gamma(gamma: float) -> tuple[float, Image.Image]:
        # Делается uint8-округление до замера, как в итоговом сохранённом кадре.
        lifted_pixels = np.clip(np.power(pixels, gamma) * 255.0, 0, 255).astype(np.uint8)
        lifted = Image.fromarray(lifted_pixels, mode="RGB")
        return float(np.asarray(lifted.convert("L"), dtype=np.float32).mean()), lifted

    original_luma, original = luma_for_gamma(1.0)
    if original_luma >= TARGET_LUMA:
        return original

    low, high = 0.2, 1.0
    for _ in range(30):
        middle = (low + high) / 2.0
        luma, _ = luma_for_gamma(middle)
        if luma < TARGET_LUMA:
            high = middle
        else:
            low = middle

    # На границах дискретного uint8-изображения выбираем наиболее близкий результат.
    candidates = [(original_luma, original)]
    for gamma in (low, high, (low + high) / 2.0):
        candidates.append(luma_for_gamma(gamma))
    return min(candidates, key=lambda item: abs(item[0] - TARGET_LUMA))[1]


def code_cell(source: str) -> dict[str, object]:
    return {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": source.splitlines(keepends=True)}


def make_notebook(image_b64: str, prompt: str, seed: int) -> dict[str, object]:
    """Return the upload notebook. Keep installation and generation cells separate."""
    install = """!pip install torch==2.4.1 torchvision==0.19.1 --index-url https://download.pytorch.org/whl/cu121
!pip uninstall -y flash-attn flash_attn
!pip install diffusers==0.33.1 transformers==4.46.3 accelerate imageio-ffmpeg sentencepiece
"""
    decode = """import base64
import io
from PIL import Image

IMAGE_B64 = %s
image = Image.open(io.BytesIO(base64.b64decode(IMAGE_B64))).convert("RGB")
""" % json.dumps(image_b64)
    pipeline = """import os
import torch
from diffusers import LTXImageToVideoPipeline
from diffusers.utils import export_to_video

pipe = LTXImageToVideoPipeline.from_pretrained(
    "Lightricks/LTX-Video-0.9.5", torch_dtype=torch.float16
)
pipe.enable_model_cpu_offload()
pipe.vae.enable_tiling()
pipe.vae.enable_slicing()
"""
    # ВАЖНО: seed вшивается в текст ноутбука. Читать его через os.environ БЕСПОЛЕЗНО —
    # ноутбук исполняется на Kaggle, наших переменных окружения там нет (было в первой версии).
    generate = """prompt = %s
negative_prompt = (
    "static, still image, frozen frame, motionless, flickering, bright, overexposed, blurred, "
    "worst quality, jpeg artifacts, watermark, text, people, face, deformed"
)
generator = torch.Generator(device="cuda").manual_seed(%d)
frames = pipe(
    image=image,
    prompt=prompt,
    negative_prompt=negative_prompt,
    width=480,
    height=704,
    num_frames=73,
    num_inference_steps=50,
    guidance_scale=3.0,
    frame_rate=24,
    decode_timestep=0.05,
    decode_noise_scale=0.025,
    generator=generator,
).frames[0]
export_to_video(frames, "out.mp4", fps=24)
""" % (json.dumps(prompt), seed)
    metric = """import numpy as np

def normalised_gray(frame):
    gray = np.asarray(frame.convert("L"), dtype=np.float32)
    return (gray - gray.mean()) / (gray.std() + 1e-6)

reference = normalised_gray(frames[0])
indices = (len(frames) // 4, len(frames) // 2, len(frames) - 1)
distances = [float(np.mean(np.abs(normalised_gray(frames[index]) - reference))) for index in indices]
metric_line = "MOTION_METRIC quarter=%.6f half=%.6f last=%.6f" % tuple(distances)
print(metric_line)
with open("motion_metric.log", "w", encoding="utf-8") as metric_file:
    metric_file.write(metric_line + "\\n")
"""
    return {
        "cells": [code_cell(install), code_cell(decode), code_cell(pipeline), code_cell(generate), code_cell(metric)],
        # display_name в kernelspec ОБЯЗАТЕЛЕН — без него Kaggle валит прогон на nbconvert
        # уже ПОСЛЕ успешной генерации и метит ядро как ERROR (ловили 2026-07-28).
        # nbformat_minor=4, НЕ 5: схема 4.5 требует поле id у каждой ячейки, а мы их не ставим —
        # это тот же класс отказа на валидации.
        "metadata": {"kernelspec": {"name": "python3", "language": "python", "display_name": "Python 3"}},
        "nbformat": 4,
        "nbformat_minor": 4,
    }


def pull_output(slug: str, directory: Path, *, required: bool) -> subprocess.CompletedProcess[str]:
    result = run(["kaggle", "kernels", "output", slug, "-p", str(directory)], check=False)
    if required and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        fail(f"Could not pull Kaggle kernel output for {slug}: {detail[-2000:]}")
    return result


def print_kernel_log_tail(directory: Path, command_result: subprocess.CompletedProcess[str] | None = None) -> None:
    """Print the best available downloaded kernel log without treating binary video as text."""
    candidates = sorted(
        path for path in directory.rglob("*")
        if path.is_file() and (path.suffix.lower() in {".log", ".txt", ".ipynb"} or "log" in path.name.lower())
    )
    chunks: list[str] = []
    for path in candidates:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        chunks.append(f"--- {path.name} ---\n{content[-6000:]}")
    if command_result is not None:
        command_text = "\n".join(part for part in (command_result.stdout, command_result.stderr) if part)
        if command_text:
            chunks.append(f"--- kaggle kernels output ---\n{command_text[-6000:]}")
    if chunks:
        print("\n".join(chunks)[-12000:], file=sys.stderr)
    else:
        print("No downloadable kernel log was returned by Kaggle.", file=sys.stderr)


def wait_until_started(slug: str) -> None:
    """Дождаться, пока ядро УЙДЁТ из COMPLETE предыдущего прогона.

    Сцена ретраится (scene_dispatch --max-retries) тем же слагом, поэтому сразу после push
    статус может отдавать COMPLETE от ПРОШЛОГО запуска. Без этой паузы раннер решит, что всё
    готово, и утащит СТАРОЕ видео вместо нового.
    """
    deadline = time.monotonic() + 5 * 60
    while time.monotonic() < deadline:
        result = run(["kaggle", "kernels", "status", slug], check=False)
        text = "\n".join(p for p in (result.stdout, result.stderr) if p).upper()
        if "COMPLETE" not in text:
            return
        print("Kaggle still reports the previous run as COMPLETE; waiting for the new version.", file=sys.stderr)
        time.sleep(20)
    print("Warning: kernel never left COMPLETE; proceeding, output may be stale.", file=sys.stderr)


def status_until_done(slug: str, output_directory: Path) -> None:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    last_status = ""
    while time.monotonic() < deadline:
        result = run(["kaggle", "kernels", "status", slug], check=False)
        last_status = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        upper = last_status.upper()
        if result.returncode != 0:
            fail(f"Could not query Kaggle kernel status for {slug}: {last_status[-2000:]}")
        if "COMPLETE" in upper:
            return
        if "ERROR" in upper or "CANCEL" in upper:
            print(f"Kaggle kernel {slug} ended unsuccessfully: {last_status}", file=sys.stderr)
            output_result = pull_output(slug, output_directory, required=False)
            print_kernel_log_tail(output_directory, output_result)
            fail(f"Kaggle kernel {slug} ended with an error or cancellation.")
        print(f"Kaggle kernel status: {last_status}", file=sys.stderr)
        time.sleep(POLL_SECONDS)
    fail(f"Timed out after 45 minutes waiting for Kaggle kernel {slug}. Last status: {last_status}")


def find_output_video(directory: Path) -> Path:
    matches = sorted(path for path in directory.rglob("out.mp4") if path.is_file())
    if not matches:
        fail("Kaggle output did not contain out.mp4.")
    video = matches[0]
    try:
        size = video.stat().st_size
    except OSError as exc:
        fail(f"Could not inspect downloaded out.mp4: {exc}")
    if size <= 5000:
        fail(f"Downloaded out.mp4 is implausibly small ({size} bytes; expected more than 5000).")
    return video


def print_motion_metric(directory: Path) -> None:
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".log", ".txt", ".ipynb"}:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if "MOTION_METRIC" in line:
                print(line.strip())
                return
    # НЕ падаем: видео к этому моменту уже лежит на ЯД. Уронить прогон здесь = записать
    # маркер ошибки поверх удачно сгенерированной сцены.
    print("Warning: MOTION_METRIC line was not found in the Kaggle log.", file=sys.stderr)


def main() -> None:
    for executable in ("kaggle", "rclone"):
        if shutil.which(executable) is None:
            fail(f"Required command is not installed or not on PATH: {executable}.")

    job_id = required_env("JOB_ID")
    scene_idx = required_env("SCENE_IDX")
    prompt = required_env("PROMPT")
    image_path = Path(required_env("IMG_LOCAL"))
    destination = required_env("DEST_FOLDER")
    out_name = required_env("OUT_NAME")
    username = required_env("KAGGLE_USERNAME")
    key = required_env("KAGGLE_KEY")
    slug = sanitize_slug(os.environ.get("LTX_KERNEL_SLUG") or f"{username}/ltx-scene-gen")

    if not image_path.is_file():
        fail(f"IMG_LOCAL does not point to an existing file: {image_path}")
    if not slug or "/" not in slug:
        fail(f"LTX_KERNEL_SLUG must be a Kaggle kernel slug such as username/kernel-name; got {slug!r}.")

    prepare_kaggle_credentials(username, key)
    lifted = crop_resize_and_lift(image_path)

    with tempfile.TemporaryDirectory(prefix=f"ltx-i2v-{job_id}-{scene_idx}-") as temp_name:
        workdir = Path(temp_name)
        lifted_path = workdir / "lifted_input.jpg"
        try:
            lifted.save(lifted_path, format="JPEG", quality=92)
        except OSError as exc:
            fail(f"Could not save brightness-lifted input image: {exc}")
        image_b64 = base64.b64encode(lifted_path.read_bytes()).decode("ascii")

        try:
            seed = int(os.environ.get("SEED", "42"))
        except ValueError:
            fail(f"SEED must be an integer; got {os.environ.get('SEED')!r}.")
        notebook_path = workdir / "ltx_i2v.ipynb"
        notebook_path.write_text(json.dumps(make_notebook(image_b64, prompt, seed), ensure_ascii=False), encoding="utf-8")
        metadata = {
            "id": slug,
            "code_file": notebook_path.name,
            "language": "python",
            "kernel_type": "notebook",
            "is_private": True,
            "enable_gpu": True,
            "enable_internet": True,
            "dataset_sources": [],
            "kernel_sources": [],
            "competition_sources": [],
            "model_sources": [],
        }
        (workdir / "kernel-metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        run(["kaggle", "kernels", "push", "-p", str(workdir)])
        wait_until_started(slug)
        status_until_done(slug, workdir)
        pull_output(slug, workdir, required=True)
        video = find_output_video(workdir)
        remote = f"ydrive:{destination.rstrip('/')}/{out_name.lstrip('/')}"
        run(["rclone", "copyto", str(video), remote])
        print_motion_metric(workdir)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
