#!/usr/bin/env python3
import unittest
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from screenplay_pipeline import final_qc


class FinalQcTests(unittest.TestCase):
    def test_report_contains_local_caller_identity_and_actual_clip_hash(self):
        from render_contract import sha256_file, post_qc_decision
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            clip = directory / "result.mp4"
            clip.write_bytes(b"synthetic clip; no ffmpeg")
            local_report = directory / "bound.json"
            identity = dict(job_id="job", clip=clip.name, clip_sha256=sha256_file(clip), check_id="unique")
            verdict = dict(cuts_ok=True, texture_consistent=True, fonts_ok=True,
                           plastic_score=75, reject_reason="rhythmic_flash", reason="flash")
            argv = ["final_qc", "--clip", str(clip), "--job-id", "job",
                    "--check-id", "unique", "--report-path", str(local_report)]
            with patch.object(sys, "argv", argv), patch.object(final_qc, "WORK", td), \
                 patch.object(final_qc, "frames_of", return_value=["mock frame"]), \
                 patch.object(final_qc, "make_strip", return_value="mock strip"), \
                 patch.object(final_qc, "judge", return_value=(verdict, "ok")), \
                 patch.object(final_qc, "upload_yd") as upload, \
                 self.assertRaises(SystemExit) as stopped:
                final_qc.main()
            self.assertEqual(stopped.exception.code, 2)
            upload.assert_called_once()
            report = json.loads(local_report.read_text())
            for key, value in identity.items():
                self.assertEqual(report[key], value)
            self.assertFalse(post_qc_decision("full", 2, qc_report=report,
                expected_identity=identity, allow_rhythmic_flash=True)[0])

    @patch.object(final_qc, "JUDGES", ["mock"])
    @patch.object(final_qc, "ask_vision")
    @patch("pathlib.Path.read_bytes", return_value=b"image")
    def test_string_boole_are_rejected(self, _read, ask):
        ask.return_value = ({
            "cuts_ok": "false",
            "texture_consistent": True,
            "fonts_ok": True,
            "plastic_score": 10,
        }, None)
        result, status = final_qc.judge("unused.jpg")
        self.assertIsNone(result)
        self.assertEqual(status, "bad-cuts_ok-type")

    @patch.object(final_qc, "JUDGES", ["mock"])
    @patch.object(final_qc, "ask_vision")
    @patch("pathlib.Path.read_bytes", return_value=b"image")
    def test_real_boole_are_preserved(self, _read, ask):
        ask.return_value = ({
            "cuts_ok": False,
            "texture_consistent": True,
            "fonts_ok": True,
            "plastic_score": 10,
            "reject_reason": "other",
            "reason": "cut mismatch",
        }, None)
        result, status = final_qc.judge("unused.jpg")
        self.assertEqual(status, "ok")
        self.assertIs(result["cuts_ok"], False)
        self.assertEqual(result["reject_reason"], "other")

    @patch.object(final_qc, "JUDGES", ["mock"])
    @patch.object(final_qc, "ask_vision")
    @patch("pathlib.Path.read_bytes", return_value=b"image")
    def test_flash_reason_requires_no_other_defect(self, _read, ask):
        # rhythmic_flash без независимых дефектов — валидно.
        ask.return_value = ({
            "cuts_ok": True, "texture_consistent": True, "fonts_ok": True,
            "plastic_score": 78, "reject_reason": "rhythmic_flash",
        }, None)
        result, status = final_qc.judge("unused.jpg")
        self.assertEqual(status, "ok")
        self.assertEqual(result["reject_reason"], "rhythmic_flash")
        # rhythmic_flash при дефекте шрифтов — противоречивый отчёт, брак.
        ask.return_value = ({
            "cuts_ok": True, "texture_consistent": True, "fonts_ok": False,
            "plastic_score": 78, "reject_reason": "rhythmic_flash",
        }, None)
        result, status = final_qc.judge("unused.jpg")
        self.assertIsNone(result)
        self.assertEqual(status, "mixed-reject")

    @patch.object(final_qc, "JUDGES", ["mock"])
    @patch.object(final_qc, "ask_vision")
    @patch("pathlib.Path.read_bytes", return_value=b"image")
    def test_unknown_reject_reason_is_rejected(self, _read, ask):
        ask.return_value = ({
            "cuts_ok": True, "texture_consistent": True, "fonts_ok": True,
            "plastic_score": 78, "reject_reason": "flashy",
        }, None)
        result, status = final_qc.judge("unused.jpg")
        self.assertIsNone(result)
        self.assertEqual(status, "bad-reject-reason")

    @patch.object(final_qc, "JUDGES", ["mock"])
    @patch.object(final_qc, "ask_vision")
    @patch("pathlib.Path.read_bytes", return_value=b"image")
    def test_absent_reject_reason_is_ok(self, _read, ask):
        # Старые судьи без reject_reason остаются валидными (override просто не сработает).
        ask.return_value = ({
            "cuts_ok": True, "texture_consistent": True, "fonts_ok": True,
            "plastic_score": 20,
        }, None)
        result, status = final_qc.judge("unused.jpg")
        self.assertEqual(status, "ok")
        self.assertIsNone(result["reject_reason"])


if __name__ == "__main__":
    unittest.main()
