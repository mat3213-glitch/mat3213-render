#!/usr/bin/env python3
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from render_contract import (
    RenderContractError,
    assert_media_contract,
    creative_qc_policy,
    load_approval,
    require_complete,
    validate_render_job,
    write_render_receipt,
)


SHA = "a" * 64


class RenderContractTests(unittest.TestCase):
    def test_legacy_job_is_preview(self):
        job = {"duration": 15}
        self.assertEqual(validate_render_job(job, pipeline="test"), "preview")
        self.assertEqual(job["render_mode"], "preview")

    def test_preview_must_be_a_real_proxy(self):
        with self.assertRaisesRegex(RenderContractError, "proxy limit"):
            validate_render_job({"render_mode": "preview", "duration": 15.01}, pipeline="test")

    def test_storyboard_preview_duration_is_derived_from_shots(self):
        job = {"shots": [{"t_dur": 5}, {"t_dur": 7.5}]}
        self.assertEqual(validate_render_job(job, pipeline="test"), "preview")

    def test_full_without_separate_approval_is_refused(self):
        job = {"render_mode": "full", "preview_job_id": "p1", "preview_sha256": SHA}
        with self.assertRaisesRegex(RenderContractError, "approval.json"):
            validate_render_job(job, pipeline="test")

    def test_full_requires_exact_owner_approval(self):
        job = {"render_mode": "full", "preview_job_id": "p1", "preview_sha256": SHA}
        approval = {
            "approved": True,
            "approved_by": "yaromat",
            "preview_job_id": "p1",
            "preview_sha256": SHA,
        }
        receipt = {
            "schema": 1, "mode": "preview", "pipeline": "test",
            "job_id": "p1", "sha256": SHA,
        }
        self.assertEqual(
            validate_render_job(
                job, pipeline="test", approval=approval, preview_receipt=receipt
            ), "full"
        )
        approval["preview_sha256"] = "b" * 64
        with self.assertRaisesRegex(RenderContractError, "does not match"):
            validate_render_job(
                job, pipeline="test", approval=approval, preview_receipt=receipt
            )

    def test_full_requires_real_preview_receipt(self):
        job = {"render_mode": "full", "preview_job_id": "p1", "preview_sha256": SHA}
        approval = {
            "approved": True, "approved_by": "yaromat",
            "preview_job_id": "p1", "preview_sha256": SHA,
        }
        with self.assertRaisesRegex(RenderContractError, "source preview"):
            validate_render_job(job, pipeline="test", approval=approval)

    def test_preview_job_id_cannot_escape_render_jobs(self):
        job = {"render_mode": "full", "preview_job_id": "../other", "preview_sha256": SHA}
        with self.assertRaisesRegex(RenderContractError, "safe preview_job_id"):
            validate_render_job(job, pipeline="test")

    def test_approval_loader_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "approval.json"
            p.write_text("[]", encoding="utf-8")
            with self.assertRaises(RenderContractError):
                load_approval(p)

    def test_completeness_is_blocking(self):
        require_complete(4, 4, label="shots")
        with self.assertRaisesRegex(RenderContractError, "3/4"):
            require_complete(4, 3, label="shots")

    def test_preview_receipt_contains_approval_identity(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "preview.mp4"
            output.write_bytes(b"preview bytes")
            receipt_path = Path(td) / "render_receipt.json"
            receipt = write_render_receipt(
                receipt_path, output_path=output, job_id="preview-1",
                mode="preview", pipeline="storyboard_render",
            )
            self.assertEqual(receipt["job_id"], "preview-1")
            self.assertEqual(len(receipt["sha256"]), 64)
            self.assertEqual(
                receipt["approval_template"]["preview_sha256"], receipt["sha256"]
            )
            self.assertEqual(json.loads(receipt_path.read_text())["mode"], "preview")

    def test_preview_qc_is_advisory_but_full_qc_blocks(self):
        self.assertEqual(creative_qc_policy("preview", 2),
                         (False, "preview_ready_manual_qc"))
        self.assertEqual(creative_qc_policy("preview", 0),
                         (False, "preview_ready_qc_pass"))
        self.assertEqual(creative_qc_policy("full", 2),
                         (True, "full_qc_failed"))
        self.assertEqual(creative_qc_policy("full", 0),
                         (False, "full_qc_pass"))

    @patch("render_contract.subprocess.run")
    def test_media_contract(self, run):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "result.mp4"
            p.write_bytes(b"0" * 2000)
            run.return_value.returncode = 0
            run.return_value.stderr = ""
            run.return_value.stdout = json.dumps({
                "format": {"duration": "15.040"},
                "streams": [
                    {"codec_type": "video", "pix_fmt": "yuv420p", "duration": "15.040"},
                    {"codec_type": "audio", "duration": "15.000"},
                ],
            })
            report = assert_media_contract(p, expected_duration=15.0)
            self.assertEqual(report["pix_fmt"], "yuv420p")

    @patch("render_contract.subprocess.run")
    def test_media_duration_mismatch_is_refused(self, run):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "result.mp4"
            p.write_bytes(b"0" * 2000)
            run.return_value.returncode = 0
            run.return_value.stderr = ""
            run.return_value.stdout = json.dumps({
                "format": {"duration": "13.0"},
                "streams": [
                    {"codec_type": "video", "pix_fmt": "yuv420p", "duration": "13.0"},
                    {"codec_type": "audio", "duration": "13.0"},
                ],
            })
            with self.assertRaisesRegex(RenderContractError, "duration mismatch"):
                assert_media_contract(p, expected_duration=15.0)

    @patch("render_contract.subprocess.run")
    def test_av_drift_is_refused(self, run):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "result.mp4"
            p.write_bytes(b"0" * 2000)
            run.return_value.returncode = 0
            run.return_value.stderr = ""
            run.return_value.stdout = json.dumps({
                "format": {"duration": "15.0"},
                "streams": [
                    {"codec_type": "video", "pix_fmt": "yuv420p", "duration": "15.0"},
                    {"codec_type": "audio", "duration": "14.5"},
                ],
            })
            with self.assertRaisesRegex(RenderContractError, "duration drift"):
                assert_media_contract(p, expected_duration=15.0)


if __name__ == "__main__":
    unittest.main()
