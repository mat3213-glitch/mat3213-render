#!/usr/bin/env python3
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from render_contract import (
    RenderContractError,
    assert_media_contract,
    creative_qc_policy,
    load_approval,
    parse_qc_report,
    post_qc_decision,
    qc_report_flash_only,
    run_final_qc,
    sha256_file,
    require_complete,
    validate_render_job,
    write_render_receipt,
)


SHA = "a" * 64
IDENTITY = {"job_id": "job-1", "clip": "result.mp4", "clip_sha256": SHA, "check_id": "check-1"}


def flash_report(**overrides):
    report = {
        **IDENTITY,
        "cuts_ok": True, "texture_consistent": True, "fonts_ok": True,
        "plastic_score": 82, "reject_reason": "rhythmic_flash",
        "reason": "kick flash ramps",
    }
    report.update(overrides)
    return report


class RenderContractTests(unittest.TestCase):
    def test_legacy_job_is_preview(self):
        job = {"duration": 15}
        self.assertEqual(validate_render_job(job, pipeline="test"), "preview")
        self.assertEqual(job["render_mode"], "preview")

    def test_preview_must_be_a_real_proxy(self):
        with self.assertRaisesRegex(RenderContractError, "proxy limit"):
            validate_render_job({"render_mode": "preview", "duration": 30.01}, pipeline="test")

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

    def test_receipt_accepts_additive_metadata_only(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "preview.mp4"
            output.write_bytes(b"preview bytes")
            receipt = write_render_receipt(
                Path(td) / "receipt.json", output_path=output, job_id="p1",
                mode="preview", pipeline="test", metadata={"registry_version": "abc"},
            )
            self.assertEqual(receipt["metadata"]["registry_version"], "abc")
            with self.assertRaisesRegex(RenderContractError, "overwrite identity"):
                write_render_receipt(
                    Path(td) / "bad.json", output_path=output, job_id="p1",
                    mode="preview", pipeline="test", metadata={"sha256": "bad"},
                )

    def test_preview_qc_is_advisory_but_full_qc_blocks(self):
        self.assertEqual(creative_qc_policy("preview", 2),
                         (False, "preview_ready_manual_qc"))
        self.assertEqual(creative_qc_policy("preview", 0),
                         (False, "preview_ready_qc_pass"))
        self.assertEqual(creative_qc_policy("full", 2),
                         (True, "full_qc_failed"))
        self.assertEqual(creative_qc_policy("full", 0),
                         (False, "full_qc_pass"))

    def test_flash_override_excuses_only_structured_flash_report(self):
        rc2 = 2
        # Чистый flash по структурированному отчёту — единственный разрешённый случай.
        self.assertEqual(post_qc_decision("full", rc2, qc_report=flash_report(),
                                          expected_identity=IDENTITY,
                                          allow_rhythmic_flash=True),
                         (False, "full_qc_pass_flash_override"))
        # Без декларированного флэша отказ блокируется.
        self.assertEqual(post_qc_decision("full", rc2, allow_rhythmic_flash=False),
                         (True, "full_qc_failed"))
        # Инфраструктурные сбои (судья недоступен = 1) и timeout (124) — клип не
        # ревьюился: flash-грамматика не снимает блок.
        self.assertEqual(post_qc_decision("full", 1, qc_report=flash_report(),
                                          allow_rhythmic_flash=True),
                         (True, "full_qc_failed"))
        self.assertEqual(post_qc_decision("full", 124, qc_report=flash_report(),
                                          allow_rhythmic_flash=True),
                         (True, "full_qc_failed"))
        # Превью остаётся advisory независимо от кода судьи.
        self.assertEqual(post_qc_decision("preview", 1, qc_report=flash_report(),
                                          allow_rhythmic_flash=True),
                         (False, "preview_ready_manual_qc"))
        # Обычный успешный QC остаётся успешным.
        self.assertEqual(post_qc_decision("full", 0, qc_report=flash_report(),
                                          allow_rhythmic_flash=True),
                         (False, "full_qc_pass"))

    def test_flash_override_requires_absence_of_other_defects(self):
        rc2 = 2
        # Независимый дефект даже рядом с флэшем → блок.
        self.assertEqual(post_qc_decision(
            "full", rc2, qc_report=flash_report(fonts_ok=False),
            expected_identity=IDENTITY, allow_rhythmic_flash=True), (True, "full_qc_failed"))
        self.assertEqual(post_qc_decision(
            "full", rc2, qc_report=flash_report(texture_consistent=False),
            expected_identity=IDENTITY, allow_rhythmic_flash=True), (True, "full_qc_failed"))
        self.assertEqual(post_qc_decision(
            "full", rc2, qc_report=flash_report(cuts_ok=False),
            expected_identity=IDENTITY, allow_rhythmic_flash=True), (True, "full_qc_failed"))
        # Причина не «rhythmic_flash», а пластик без декларации — блок.
        self.assertEqual(post_qc_decision(
            "full", rc2, qc_report=flash_report(reject_reason="other"),
            expected_identity=IDENTITY, allow_rhythmic_flash=True), (True, "full_qc_failed"))
        # Низкий plastic при декларации flash — это не расход, блок.
        self.assertEqual(post_qc_decision(
            "full", rc2, qc_report=flash_report(plastic_score=40),
            expected_identity=IDENTITY, allow_rhythmic_flash=True), (True, "full_qc_failed"))

    def test_flash_override_rejects_missing_broken_or_foreign_report(self):
        rc2 = 2
        # Свободного rc=2 без отчёта недостаточно.
        self.assertEqual(post_qc_decision("full", rc2, allow_rhythmic_flash=True),
                         (True, "full_qc_failed"))
        # Повреждённый / чужой / не-объект отчёт → блок (fail-closed).
        for bad in (None, "not-a-dict", {"bogus": True},
                    flash_report(cuts_ok="false"),
                    flash_report(plastic_score="high"),
                    flash_report(reject_reason="flashy")):
            with self.subTest(bad=bad):
                self.assertEqual(post_qc_decision("full", rc2, qc_report=bad,
                                                  expected_identity=IDENTITY,
                                                  allow_rhythmic_flash=True),
                                 (True, "full_qc_failed"))

    def test_parse_qc_report_contract(self):
        self.assertEqual(parse_qc_report(flash_report())[
            "reject_reason"], "rhythmic_flash")
        self.assertTrue(qc_report_flash_only(parse_qc_report(flash_report())))
        # Независимый дефект / другая причина / низкий plastic — структурно
        # валидны, но НЕ flash-only (override им не даётся).
        self.assertFalse(qc_report_flash_only(
            parse_qc_report(flash_report(fonts_ok=False))))
        self.assertFalse(qc_report_flash_only(
            parse_qc_report(flash_report(reject_reason="other"))))
        self.assertFalse(qc_report_flash_only(
            parse_qc_report(flash_report(plastic_score=40))))
        # Смешанный отчёт (флэш + независимый дефект) структурно валиден, но не flash-only.
        mixed = {"cuts_ok": False, "texture_consistent": True, "fonts_ok": True,
                 "plastic_score": 70, "reject_reason": "rhythmic_flash"}
        self.assertFalse(qc_report_flash_only(parse_qc_report(mixed)))
        # Структурный брак — RenderContractError (fail-closed).
        for bad in ("not-a-dict", flash_report(cuts_ok="false"),
                    flash_report(plastic_score="high"),
                    flash_report(reject_reason="flashy")):
            with self.subTest(bad=bad):
                with self.assertRaises(RenderContractError):
                    parse_qc_report(bad)

    def test_foreign_or_stale_identity_blocks_override(self):
        for key in IDENTITY:
            for value in (None, "foreign-value"):
                with self.subTest(key=key, value=value):
                    self.assertTrue(post_qc_decision(
                        "full", 2, qc_report=flash_report(**{key: value}),
                        expected_identity=IDENTITY, allow_rhythmic_flash=True)[0])
        self.assertTrue(post_qc_decision("full", 2, qc_report=flash_report(),
                                        allow_rhythmic_flash=True)[0])

    def test_invalid_scores_block_even_with_matching_identity(self):
        for score in (float("inf"), float("nan"), -1, 101):
            self.assertTrue(post_qc_decision(
                "full", 2, qc_report=flash_report(plastic_score=score),
                expected_identity=IDENTITY, allow_rhythmic_flash=True)[0])

    def test_bound_qc_reads_this_invocation_only(self):
        with tempfile.TemporaryDirectory() as td:
            clip = Path(td) / "result.mp4"
            clip.write_bytes(b"synthetic clip")
            def judge(argv, **kwargs):
                report = flash_report(job_id="job", clip_sha256=sha256_file(clip),
                                      check_id=argv[argv.index("--check-id") + 1])
                Path(argv[argv.index("--report-path") + 1]).write_text(json.dumps(report))
                return subprocess.CompletedProcess(argv, 2)
            with patch("render_contract.subprocess.run", side_effect=judge):
                rc, report, identity = run_final_qc(clip, "job")
            self.assertFalse(post_qc_decision("full", rc, qc_report=report,
                expected_identity=identity, allow_rhythmic_flash=True)[0])
            with patch("render_contract.subprocess.run", return_value=subprocess.CompletedProcess([], 2)):
                rc, report, second = run_final_qc(clip, "job")
            self.assertNotEqual(identity["check_id"], second["check_id"])
            self.assertIsNone(report)
            self.assertTrue(post_qc_decision("full", rc, qc_report=report,
                expected_identity=second, allow_rhythmic_flash=True)[0])

    def test_bound_qc_timeout_and_changed_clip_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            clip = Path(td) / "result.mp4"
            clip.write_bytes(b"before")
            with patch("render_contract.subprocess.run", side_effect=subprocess.TimeoutExpired([], 180)):
                self.assertEqual(run_final_qc(clip, "job")[0], 124)
            def judge(argv, **kwargs):
                Path(argv[argv.index("--report-path") + 1]).write_text(json.dumps(flash_report()))
                clip.write_bytes(b"after")
                return subprocess.CompletedProcess(argv, 2)
            with patch("render_contract.subprocess.run", side_effect=judge):
                self.assertEqual(run_final_qc(clip, "job")[0], 1)

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
