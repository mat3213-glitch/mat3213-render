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
            "reason": "cut mismatch",
        }, None)
        result, status = final_qc.judge("unused.jpg")
        self.assertEqual(status, "ok")
        self.assertIs(result["cuts_ok"], False)


if __name__ == "__main__":
    unittest.main()
