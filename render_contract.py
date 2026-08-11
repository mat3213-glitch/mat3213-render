#!/usr/bin/env python3
"""Shared fail-closed contract for production video renders.

Legacy jobs without ``render_mode`` are treated as previews.  A full render is
accepted only when a separate ``approval.json`` records yaromat's approval of
the exact preview job and SHA-256.  This deliberately keeps the human creative
gate outside ``job.json``: regenerating a job cannot silently promote itself.
"""

from __future__ import annotations

import json
import hashlib
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RenderContractError(ValueError):
    """The render must stop before spending compute or publishing an output."""


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_ALLOWED_MODES = {"preview", "full"}
MAX_PREVIEW_SECONDS = 15.0


def requested_duration(job: dict[str, Any]) -> float:
    raw = job.get("duration")
    if raw is None and isinstance(job.get("shots"), list):
        try:
            raw = sum(float(s["t_dur"]) for s in job["shots"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RenderContractError("storyboard shots have invalid t_dur") from exc
    try:
        duration = float(raw)
    except (TypeError, ValueError) as exc:
        raise RenderContractError("job requires a numeric positive duration") from exc
    if duration <= 0:
        raise RenderContractError("job duration must be positive")
    return duration


def load_approval(path: str | Path) -> dict[str, Any] | None:
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RenderContractError(f"approval.json unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise RenderContractError("approval.json must contain a JSON object")
    return data


def validate_render_job(
    job: dict[str, Any],
    *,
    pipeline: str,
    approval: dict[str, Any] | None = None,
    preview_receipt: dict[str, Any] | None = None,
) -> str:
    """Validate preview/full policy and return the normalized render mode."""
    if not isinstance(job, dict):
        raise RenderContractError("job.json must contain a JSON object")

    mode = str(job.get("render_mode") or "preview").strip().lower()
    if mode not in _ALLOWED_MODES:
        raise RenderContractError(
            f"{pipeline}: render_mode must be preview|full, got {mode!r}"
        )
    job["render_mode"] = mode
    if mode == "preview":
        duration = requested_duration(job)
        if duration > MAX_PREVIEW_SECONDS:
            raise RenderContractError(
                f"{pipeline}: preview duration {duration:.3f}s exceeds "
                f"{MAX_PREVIEW_SECONDS:.0f}s proxy limit"
            )
        return mode

    preview_job_id, preview_sha256 = preview_reference(job, pipeline=pipeline)

    if approval is None:
        raise RenderContractError(
            f"{pipeline}: full render refused without separate approval.json"
        )
    if preview_receipt is None:
        raise RenderContractError(
            f"{pipeline}: full render refused without source preview render_receipt.json"
        )

    receipt_mode = str(preview_receipt.get("mode") or "").strip().lower()
    receipt_job = str(preview_receipt.get("job_id") or "").strip()
    receipt_sha = str(preview_receipt.get("sha256") or "").strip().lower()
    receipt_pipeline = str(preview_receipt.get("pipeline") or "").strip()
    if (preview_receipt.get("schema") != 1 or receipt_mode != "preview"
            or receipt_pipeline != pipeline or receipt_job != preview_job_id
            or receipt_sha != preview_sha256):
        raise RenderContractError(
            f"{pipeline}: source preview receipt does not match the requested preview id/hash"
        )

    approved = approval.get("approved") is True
    approved_by = str(approval.get("approved_by") or "").strip().lower()
    approved_job = str(approval.get("preview_job_id") or "").strip()
    approved_sha = str(approval.get("preview_sha256") or "").strip().lower()
    if not approved or approved_by != "yaromat":
        raise RenderContractError(
            f"{pipeline}: approval.json must contain approved=true and approved_by=yaromat"
        )
    if approved_job != preview_job_id or approved_sha != preview_sha256:
        raise RenderContractError(
            f"{pipeline}: approval.json does not match the requested preview id/hash"
        )
    return mode


def preview_reference(job: dict[str, Any], *, pipeline: str) -> tuple[str, str]:
    """Validate a preview reference before it is interpolated into an rclone path."""
    preview_job_id = str(job.get("preview_job_id") or "").strip()
    preview_sha256 = str(job.get("preview_sha256") or "").strip().lower()
    safe_preview_id = bool(_JOB_ID.fullmatch(preview_job_id)) and ".." not in preview_job_id
    if not safe_preview_id or not _SHA256.fullmatch(preview_sha256):
        raise RenderContractError(
            f"{pipeline}: full render requires safe preview_job_id and 64-char preview_sha256"
        )
    return preview_job_id, preview_sha256


def require_complete(expected: int, rendered: int, *, label: str = "scenes") -> None:
    if expected <= 0:
        raise RenderContractError(f"{label}: expected count must be positive")
    if rendered != expected:
        raise RenderContractError(f"{label}: incomplete render {rendered}/{expected}")


def creative_qc_policy(mode: str, returncode: int) -> tuple[bool, str]:
    """Return ``(must_block, status)`` for the fallible VLM creative judge.

    A technically valid preview must remain visible to yaromat even when the
    external judge is rate-limited or disagrees.  Full render stays fail-closed.
    """
    if mode == "preview":
        return (False, "preview_ready_qc_pass" if returncode == 0
                else "preview_ready_manual_qc")
    if mode == "full":
        return (returncode != 0, "full_qc_pass" if returncode == 0 else "full_qc_failed")
    raise RenderContractError(f"creative QC received unknown mode {mode!r}")


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_render_receipt(
    receipt_path: str | Path,
    *,
    output_path: str | Path,
    job_id: str,
    mode: str,
    pipeline: str,
) -> dict[str, Any]:
    """Write the immutable identity that a later full-render approval references."""
    if mode not in _ALLOWED_MODES:
        raise RenderContractError(f"cannot receipt unknown render mode {mode!r}")
    output = Path(output_path)
    if not output.is_file():
        raise RenderContractError(f"cannot receipt missing output: {output}")
    digest = sha256_file(output)
    receipt = {
        "schema": 1,
        "job_id": job_id,
        "mode": mode,
        "pipeline": pipeline,
        "output_name": output.name,
        "sha256": digest,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if mode == "preview":
        receipt["approval_template"] = {
            "approved": True,
            "approved_by": "yaromat",
            "preview_job_id": job_id,
            "preview_sha256": digest,
            "approved_at": "<ISO-8601>",
        }
    Path(receipt_path).write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return receipt


def probe_media(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file() or p.stat().st_size < 1000:
        raise RenderContractError(f"output missing or empty: {p}")
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration:stream=codec_type,pix_fmt,duration",
            "-of", "json", str(p),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RenderContractError(f"ffprobe failed: {result.stderr[-200:]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RenderContractError("ffprobe returned invalid JSON") from exc


def assert_media_contract(
    path: str | Path,
    *,
    expected_duration: float,
    tolerance: float = 0.30,
) -> dict[str, Any]:
    """Blocking delivery checks shared by preview and full renders."""
    data = probe_media(path)
    streams = data.get("streams") or []
    videos = [s for s in streams if s.get("codec_type") == "video"]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    if len(videos) != 1 or not audios:
        raise RenderContractError(
            f"media streams invalid: video={len(videos)} audio={len(audios)}"
        )
    pix_fmt = videos[0].get("pix_fmt")
    if pix_fmt != "yuv420p":
        raise RenderContractError(f"delivery pix_fmt must be yuv420p, got {pix_fmt!r}")
    try:
        video_duration = float(videos[0]["duration"])
        audio_durations = [float(s["duration"]) for s in audios]
    except (KeyError, TypeError, ValueError) as exc:
        raise RenderContractError("audio/video stream durations are unavailable") from exc
    audio_duration = max(audio_durations)
    av_delta = abs(video_duration - audio_duration)
    if av_delta > 0.15:
        raise RenderContractError(
            f"audio/video duration drift: video={video_duration:.3f}s "
            f"audio={audio_duration:.3f}s delta={av_delta:.3f}s"
        )
    try:
        actual_duration = float((data.get("format") or {})["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RenderContractError("output duration is unavailable") from exc
    delta = abs(actual_duration - float(expected_duration))
    if delta > tolerance:
        raise RenderContractError(
            f"output duration mismatch: actual={actual_duration:.3f}s "
            f"expected={expected_duration:.3f}s delta={delta:.3f}s"
        )
    return {
        "duration": actual_duration,
        "video_duration": video_duration,
        "audio_duration": audio_duration,
        "av_delta": av_delta,
        "pix_fmt": pix_fmt,
        "audio_streams": len(audios),
    }
