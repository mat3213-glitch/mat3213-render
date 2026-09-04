from unittest.mock import patch

from screenplay_pipeline import source_qc


def test_full_mode_rejects_when_ocr_is_unavailable():
    with patch.object(source_qc, "_judge_text", return_value={
        "available": False, "has_text": False, "reason": "missing OCR",
    }):
        verdict = source_qc.judge_source("unused.png", require_checks=True)
    assert verdict["ok"] is False
    assert verdict["ocr_skipped"] is True
    assert "OCR недоступен" in verdict["reject_reason"]


def test_preview_mode_keeps_unavailable_ocr_advisory():
    with patch.object(source_qc, "_judge_text", return_value={
        "available": False, "has_text": False, "reason": "missing OCR",
    }), patch.object(source_qc, "_load"), patch.object(source_qc, "_yolo", None), \
            patch.object(source_qc, "_face", None):
        verdict = source_qc.judge_source("unused.png")
    assert verdict["ok"] is True
    assert verdict["ocr_skipped"] is True
