#!/usr/bin/env python3
import unittest
from unittest.mock import patch

from screenplay_pipeline import final_qc


class FinalQcTests(unittest.TestCase):
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
