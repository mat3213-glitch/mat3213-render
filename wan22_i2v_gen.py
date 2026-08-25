#!/usr/bin/env python3
"""Смоук Wan2.2-I2V-A14B (GGUF + Lightning-лорки) на Kaggle против LTX.

Механика пуш/полл/выкачивание переиспользуется из ltx_i2v_gen. Отличия:
- отдельный слаг ядра (не трогаем ltx-scene-gen);
- арта НЕ гамма-лифтится (лифт был нужен только LTX);
- в ядре двойной путь: Lightning 4 шага, при отказе лорок — полный 30 шагов cfg 5.
"""

import base64
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image

from ltx_i2v_gen import (
    TARGET_HEIGHT,
    TARGET_WIDTH,
    fail,
    find_contact_sheet,
    find_output_video,
    prepare_kaggle_credentials,
    print_motion_metric,
    print_kernel_log_tail,
    pull_output,
    required_env,
    run,
    sanitize_slug,
    say,
    status_until_done,
    wait_until_started,
)


def crop_resize(source: Path) -> Image.Image:
    try:
        with Image.open(source) as opened:
            image = opened.convert("RGB")
    except (OSError, ValueError) as exc:
        fail(f"Could not load input still {source}: {exc}")
    target_ratio = TARGET_WIDTH / TARGET_HEIGHT
    source_ratio = image.width / image.height
    if source_ratio > target_ratio:
        crop_width = int(round(image.height * target_ratio))
        left = (image.width - crop_width) // 2
        image = image.crop((left, 0, left + crop_width, image.height))
    else:
        crop_height = int(round(image.width / target_ratio))
        top = (image.height - crop_height) // 2
        image = image.crop((0, top, image.width, top + crop_height))
    lanczos = getattr(Image, "Resampling", Image).LANCZOS
    return image.resize((TARGET_WIDTH, TARGET_HEIGHT), lanczos)


def code_cell(source: str) -> dict[str, object]:
    return {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": source.splitlines(keepends=True)}


def make_notebook(image_b64: str, prompt: str, seed: int) -> dict[str, object]:
    """Ядро Wan2.2. Все константы вшиваются текстом — env на Kaggle не доходит."""
    # torch 2.4.1+cu121 обязателен: Kaggle выдаёт P100 (sm_60), свежие колёса torch
    # собирают кернелы без sm_60 и падают «no kernel image» на первой же матмуле.
    install = """!pip install -q torch==2.4.1 torchvision==0.19.1 --index-url https://download.pytorch.org/whl/cu121
!pip uninstall -y -q flash-attn flash_attn
!pip install -q -U "diffusers==0.35.1" "transformers==4.51.3" "accelerate==1.6.0" peft gguf sentencepiece imageio-ffmpeg
# Пины не случайны: свежий transformers требует torch>=2.5 и «отключает» наш 2.4.1,
# а 2.4.1 нужен из-за P100/sm_60. diffusers>=0.35 нужен ради Wan2.2 MoE (transformer_2).
"""
    setup = """import base64, gc, io, json, os, time
import numpy as np
import torch
from PIL import Image
t0 = time.time()
def log(m):
    print(f"[{time.time()-t0:7.1f}s] {m}", flush=True)
assert torch.cuda.is_available(), "no GPU on Kaggle kernel"
_n = torch.cuda.device_count()
for _i in range(_n):
    _p = torch.cuda.get_device_properties(_i)
    log(f"GPU{_i}: {_p.name} | {_p.total_memory/2**30:.1f}GB")
log(f"gpu_count={_n}")

IMAGE_B64 = %s
image = Image.open(io.BytesIO(base64.b64decode(IMAGE_B64))).convert("RGB")
W, H = 480, 704
_tr = W / H
_sr = image.width / image.height
if _sr > _tr:
    _cw = int(round(image.height * _tr)); _l = (image.width - _cw) // 2
    image = image.crop((_l, 0, _l + _cw, image.height))
else:
    _ch = int(round(image.width / _tr)); _t = (image.height - _ch) // 2
    image = image.crop((0, _t, image.width, _t + _ch))
image = image.resize((W, H), Image.LANCZOS)
log(f"input ready {image.size}")

PROMPT = %s
NEGATIVE_PROMPT = (
    "static, still image, frozen frame, flickering, worst quality, jpeg artifacts, "
    "watermark, text, people, face, deformed"
)
SEED = %d
REPO = "Wan-AI/Wan2.2-I2V-A14B"
GGUF_REPO = "QuantStack/Wan2.2-I2V-A14B-GGUF"
LORA_REPO = "lightx2v/Wan2.2-Lightning"
LORA_DIR = "Wan2.2-I2V-A14B-4steps-lora-rank64-Seko-V1"
QUANT_PREF = ["Q4_K_M", "Q4_K_S", "Q4_0", "Q5_K_S", "Q3_K_L"]
""" % (
        json.dumps(image_b64),
        json.dumps(prompt),
        seed,
    )
    download = """from huggingface_hub import hf_hub_download, list_repo_files
_all = list_repo_files(GGUF_REPO)
picked = {}
for side in ("HighNoise", "LowNoise"):
    hit = None
    for q in QUANT_PREF:
        cand = sorted(f for f in _all if side.lower() in f.lower() and q in f and f.endswith(".gguf"))
        if cand:
            hit = cand[0]; break
    if hit is None:
        raise RuntimeError(f"no gguf for {side}; available sample: {[f for f in _all if f.endswith('.gguf')][:10]}")
    picked[side] = hit
log(f"picked {picked}")
def _dl(repo, fname):
    _s = time.time(); _p = hf_hub_download(repo, fname); log(f"downloaded {fname} in {time.time()-_s:.0f}s"); return _p
hn_path = _dl(GGUF_REPO, picked["HighNoise"])
ln_path = _dl(GGUF_REPO, picked["LowNoise"])
hn_lora = ln_lora = None
try:
    hn_lora = _dl(LORA_REPO, f"{LORA_DIR}/high_noise_model.safetensors")
    ln_lora = _dl(LORA_REPO, f"{LORA_DIR}/low_noise_model.safetensors")
except Exception as e:
    log(f"LORAS unavailable, will run full-distill path: {e}")
"""
    pipeline_cell = """from diffusers import (
    AutoencoderKLWan, FlowMatchEulerDiscreteScheduler, GGUFQuantizationConfig,
    WanImageToVideoPipeline, WanTransformer3DModel,
)
from transformers import AutoTokenizer, UMT5EncoderModel
# Диффузерс-зеркало даёт сабфолдеры text_encoder/vae/tokenizer/scheduler;
# оригинальный Wan-AI/Wan2.2-I2V-A14B лежит в исходном формате и для diffusers не годится.
DREPO = "Wan-AI/Wan2.2-I2V-A14B-Diffusers"

_qc = GGUFQuantizationConfig(compute_dtype=torch.float16)
transformer = WanTransformer3DModel.from_single_file(
    hn_path, config=DREPO, subfolder="transformer",
    quantization_config=_qc, torch_dtype=torch.float16,
)
log("high_noise gguf loaded")
transformer_2 = WanTransformer3DModel.from_single_file(
    ln_path, config=DREPO, subfolder="transformer_2",
    quantization_config=_qc, torch_dtype=torch.float16,
)
log("low_noise gguf loaded")

# RAM хоста ~13ГБ - главный потолок: TE (~10ГБ fp16) не живёт одновременно с весами.
# Кодируем оба промпта, ТЕ УДАЛЯЕМ, в пайплайн ставим пустышку (эмбедды уже готовы).
_tok = AutoTokenizer.from_pretrained(DREPO, subfolder="tokenizer")
_te = UMT5EncoderModel.from_pretrained(
    DREPO, subfolder="text_encoder", torch_dtype=torch.float16
).eval()

def _embed(text):
    _ti = _tok([text], padding="max_length", max_length=226, truncation=True,
               add_special_tokens=True, return_tensors="pt")
    with torch.no_grad():
        _e = _te(_ti.input_ids.to("cuda"), _ti.attention_mask.to("cuda")).last_hidden_state
    _sl = _ti.attention_mask.gt(0).sum(dim=1).long().tolist()
    _e = [u[:v] for u, v in zip(_e, _sl)]
    return torch.stack(
        [torch.cat([u, u.new_zeros(226 - u.size(0), u.size(1))]) for u in _e], dim=0
    ).to("cpu", torch.float16)

PROMPT_EMBEDS = _embed(PROMPT)
NEG_EMBEDS = _embed(NEGATIVE_PROMPT)
log(f"embeds ready {tuple(PROMPT_EMBEDS.shape)}")
del _te
gc.collect(); torch.cuda.empty_cache()
log("text encoder freed from RAM")

_vae = AutoencoderKLWan.from_pretrained(DREPO, subfolder="vae", torch_dtype=torch.float16)
_sched = FlowMatchEulerDiscreteScheduler.from_pretrained(DREPO, subfolder="scheduler")

class _DummyTE(torch.nn.Module):
    pass

_dummy = _DummyTE()
_dummy.dtype = torch.float16

pipe = WanImageToVideoPipeline(
    tokenizer=_tok, text_encoder=_dummy, vae=_vae,
    transformer=transformer, transformer_2=transformer_2, scheduler=_sched,
)
del transformer, transformer_2, _vae
gc.collect()
log("pipeline assembled")

pipe.enable_model_cpu_offload()
log("cpu offload on")

LORA_OK = False
if hn_lora and ln_lora:
    try:
        pipe.load_lora_weights(hn_lora, adapter_name="high_noise")
        pipe.load_lora_weights(ln_lora, adapter_name="low_noise")
        pipe.set_adapters(["high_noise", "low_noise"], adapter_weights=[1.0, 1.0])
        LORA_OK = True
        log("lightning loras attached")
    except Exception as e:
        log(f"lora attach failed, falling back to full distill: {str(e)[:300]}")
"""
    generate = """from diffusers.utils import export_to_video
_gen = torch.Generator(device="cuda").manual_seed(SEED)
_kw = dict(image=image, prompt_embeds=PROMPT_EMBEDS, negative_prompt_embeds=NEG_EMBEDS,
           width=W, height=H, num_frames=81, generator=_gen)
_s = time.time()
if LORA_OK:
    frames = pipe(num_inference_steps=4, guidance_scale=1.0, boundary_ratio=0.9, **_kw).frames[0]
else:
    frames = pipe(num_inference_steps=30, guidance_scale=5.0, **_kw).frames[0]
log(f"generated {len(frames)} frames in {time.time()-_s:.0f}s mode={'lightning' if LORA_OK else 'full'}")
export_to_video(frames, "raw.mp4", fps=16)
"""
    remux = """!ffmpeg -y -loglevel error -i raw.mp4 -c:v libx264 -crf 18 -preset veryfast -pix_fmt yuv420p -movflags +faststart out.mp4
"""
    metric = """def normalised_gray(frame):
    gray = np.asarray(frame.convert("L"), dtype=np.float32)
    return (gray - gray.mean()) / (gray.std() + 1e-6)

reference = normalised_gray(frames[0])
indices = (len(frames) // 4, len(frames) // 2, len(frames) - 1)
distances = [float(np.mean(np.abs(normalised_gray(frames[index]) - reference))) for index in indices]
metric_line = "MOTION_METRIC quarter=%.6f half=%.6f last=%.6f" % tuple(distances)
print(metric_line)

def _edge_energy(gray_arr):
    gy, gx = np.gradient(gray_arr.astype(np.float32))
    return float(np.mean(np.abs(gx) + np.abs(gy)))

_src = np.asarray(image.convert("L").resize((256, 256)))
_first = np.asarray(frames[0].convert("L").resize((256, 256)))
_last = np.asarray(frames[-1].convert("L").resize((256, 256)))
_retention = _edge_energy(_last) / max(_edge_energy(_src), 1e-6)
_freeze = float(np.mean(np.abs(_first.astype(np.float32) - _last.astype(np.float32))))
if _retention < 0.35:
    _verdict = "DECAYED"
elif _retention < 0.55 or _freeze < 2.0:
    _verdict = "WEAK"
else:
    _verdict = "ALIVE"
gate_line = "GATE verdict=%s retention=%.3f freeze=%.2f" % (_verdict, _retention, _freeze)
print(gate_line)

sheet = Image.new("RGB", (frames[0].width * 3, frames[0].height))
for _i, _fr in enumerate((frames[0], frames[len(frames) // 2], frames[-1])):
    sheet.paste(_fr, (_i * frames[0].width, 0))
sheet.save("contact_sheet.jpg", quality=88)

with open("motion_metric.log", "w", encoding="utf-8") as metric_file:
    metric_file.write(metric_line + "\\n")
    metric_file.write(gate_line + "\\n")
log("done")
"""
    return {
        "cells": [
            code_cell(install),
            code_cell(setup),
            code_cell(download),
            code_cell(pipeline_cell),
            code_cell(generate),
            code_cell(remux),
            code_cell(metric),
        ],
        # display_name обязателен и nbformat_minor=4 — уроки из make_notebook в ltx_i2v_gen.
        "metadata": {"kernelspec": {"name": "python3", "language": "python", "display_name": "Python 3"}},
        "nbformat": 4,
        "nbformat_minor": 4,
    }


def main() -> None:
    for executable in ("kaggle", "rclone"):
        if shutil.which(executable) is None:
            fail(f"Required command is not installed or not on PATH: {executable}.")

    prompt = required_env("PROMPT")
    image_path = Path(required_env("IMG_LOCAL"))
    destination = required_env("DEST_FOLDER")
    out_name = required_env("OUT_NAME")
    username = required_env("KAGGLE_USERNAME")
    key = required_env("KAGGLE_KEY")
    slug = sanitize_slug(os.environ.get("WAN_KERNEL_SLUG") or f"{username}/wan22-i2v-smoke")

    if not image_path.is_file():
        fail(f"IMG_LOCAL does not point to an existing file: {image_path}")

    prepare_kaggle_credentials(username, key)
    cropped = crop_resize(image_path)

    with tempfile.TemporaryDirectory(prefix="wan22-smoke-") as temp_name:
        workdir = Path(temp_name)
        input_path = workdir / "input.jpg"
        cropped.save(input_path, format="JPEG", quality=92)
        image_b64 = base64.b64encode(input_path.read_bytes()).decode("ascii")

        try:
            seed = int(os.environ.get("SEED", "42"))
        except ValueError:
            fail(f"SEED must be an integer; got {os.environ.get('SEED')!r}.")
        notebook_path = workdir / "wan22_i2v.ipynb"
        notebook_path.write_text(json.dumps(make_notebook(image_b64, prompt, seed), ensure_ascii=False), encoding="utf-8")
        metadata = {
            "id": slug,
            "title": slug.split("/", 1)[1],
            "code_file": notebook_path.name,
            "language": "python",
            "kernel_type": "notebook",
            "is_private": True,
            "enable_gpu": True,
            "enable_internet": True,
            # T4x2: больше системной RAM под cpu-offload (P100-инстанс держит только ~13ГБ).
            "accelerator": "nvidiaTeslaT4",
            "dataset_sources": [],
            "kernel_sources": [],
            "competition_sources": [],
            "model_sources": [],
        }
        (workdir / "kernel-metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        push = run(["kaggle", "kernels", "push", "-p", str(workdir)], check=False)
        push_text = "\n".join(p for p in (push.stdout, push.stderr) if p).strip()
        if push.returncode != 0 and "accelerator" in push_text.lower():
            # Не все версии API знают поле accelerator — ретраим на дефолтном GPU.
            say(f"push rejected accelerator field, retrying on default GPU: {push_text[-300:]}")
            metadata.pop("accelerator")
            (workdir / "kernel-metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
            push = run(["kaggle", "kernels", "push", "-p", str(workdir)])
            push_text = "\n".join(p for p in (push.stdout, push.stderr) if p).strip()
        elif push.returncode != 0:
            fail(f"kaggle kernels push failed: {push_text[-2000:]}")
        say(f"kaggle push: {push_text[-500:]}")

        wait_until_started(slug)
        status_until_done(slug, workdir)
        pull_output(slug, workdir, required=True)
        video = find_output_video(workdir)
        remote = f"ydrive:{destination.rstrip('/')}/{out_name.lstrip('/')}"
        run(["rclone", "copyto", str(video), remote])
        sheet = find_contact_sheet(workdir)
        if sheet is not None:
            sheet_remote = f"ydrive:{destination.rstrip('/')}/{Path(out_name).stem}_contact_sheet.jpg"
            run(["rclone", "copyto", str(sheet), sheet_remote])
        print_motion_metric(workdir)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
