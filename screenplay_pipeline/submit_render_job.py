#!/usr/bin/env python3
"""
submit_render_job.py — общая утилита «упаковать job → rclone upload → workflow_dispatch».

Используется и Стадией 4 (сцена-генерация), и Стадией 6 (сборка) — обоими A/B вариантами,
чтобы не дублировать логику "залить файлы на ЯД render_jobs/<JOB_ID>/ + вызвать GH workflow"
по разным местам. Авторизация — через `gh` CLI (уже аутентифицирован в среде), не сырой PAT.

Usage (как модуль):
    from submit_render_job import submit
    submit(job_id="...", files={"local/a.json": "a.json"}, workflow="sp_references.yml",
           repo="mat3213-glitch/mat3213-render", inputs={"job_id": "...", "top": "5"})

Usage (CLI):
    python3 submit_render_job.py --job-id ID --workflow sp_references.yml \\
      --file local/brief_full.yaml=brief_full.yaml --input top=5
"""

import argparse
import subprocess
import sys

YD_ROOT = "ydrive:Content factory"
DEFAULT_REPO = "mat3213-glitch/mat3213-render"


def upload_files(job_id: str, files: dict[str, str]) -> None:
    """files: {локальный_путь: относительный_путь_в_render_jobs/<job_id>/}."""
    job_yd = f"{YD_ROOT}/cloud_io/render_jobs/{job_id}"
    for local, remote_rel in files.items():
        dst = f"{job_yd}/{remote_rel}"
        r = subprocess.run(["rclone", "copyto", local, dst], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"rclone copyto {local} → {dst} failed: {r.stderr[:300]}")
        print(f"[submit] {local} → {dst}")


def dispatch_workflow(workflow: str, inputs: dict[str, str], repo: str = DEFAULT_REPO) -> None:
    cmd = ["gh", "workflow", "run", workflow, "--repo", repo]
    for k, v in inputs.items():
        cmd += ["-f", f"{k}={v}"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh workflow run {workflow} failed: {r.stderr[:300]}")
    print(f"[submit] dispatched {workflow} ({repo}) inputs={inputs}")


def submit(job_id: str, files: dict[str, str], workflow: str, inputs: dict[str, str],
           repo: str = DEFAULT_REPO) -> None:
    """files/inputs могут быть пустыми (напр. если job уже подготовлен другим шагом).
    job_id ВСЕГДА добавляется в inputs автоматически — пойман вживую: вызов из scene_dispatch.py
    без job_id в inputs дал HTTP 422 (required input not provided) на каждой сцене."""
    if files:
        upload_files(job_id, files)
    full_inputs = {"job_id": job_id, **inputs}
    dispatch_workflow(workflow, full_inputs, repo)


def main():
    ap = argparse.ArgumentParser(description="Упаковать job → ЯД → workflow_dispatch.")
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--workflow", required=True, help="имя .yml workflow в .github/workflows/")
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--file", action="append", default=[],
                    help="local=remote_rel (можно несколько раз), напр. brief.yaml=brief_full.yaml")
    ap.add_argument("--input", action="append", default=[],
                    help="key=value для workflow_dispatch inputs (job_id добавляется автоматически)")
    args = ap.parse_args()

    files = {}
    for f in args.file:
        if "=" not in f:
            sys.exit(f"--file ожидает local=remote_rel, получил: {f}")
        local, remote = f.split("=", 1)
        files[local] = remote

    inputs = {"job_id": args.job_id}
    for i in args.input:
        if "=" not in i:
            sys.exit(f"--input ожидает key=value, получил: {i}")
        k, v = i.split("=", 1)
        inputs[k] = v

    submit(args.job_id, files, args.workflow, inputs, args.repo)


if __name__ == "__main__":
    main()
