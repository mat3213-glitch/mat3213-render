#!/usr/bin/env python3
"""Regression tests for Auto Analyst targets and ledger-backed board projection."""
import json
import tempfile
from pathlib import Path
from unittest import mock

import auto_analyst
from auto_analyst import (
    board_rows_from_reports,
    normalize_analyst_target,
    process,
    process_targets,
    verify_remote_json,
)
from scout_ledger import ScoutLedger


def test_target_validation_preserves_external_route() -> None:
    assert normalize_analyst_target("https://github.com/Owner/Repo/issues/2") == \
        "https://github.com/Owner/Repo"
    assert normalize_analyst_target("https://github.com") is None
    assert normalize_analyst_target("https://github.com/search?q=video") is None
    assert normalize_analyst_target("https://github.com/topics/video") is None
    assert normalize_analyst_target("mentions)") is None
    assert normalize_analyst_target("https://example.com/article?id=1") == \
        "https://example.com/article?id=1"


def test_board_is_ledger_view_and_drops_github_garbage() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.json"
        path.write_text(json.dumps({"schema": 1, "repos": {
            "Owner/Done": {"status": "adopted"},
            "Owner/No": {"status": "rejected"},
            "Owner/Later": {"status": "park"},
            "Owner/Try": {"status": "pilot"},
            "Owner/Missing": {"status": "rejected"},
        }}), encoding="utf-8")
        ledger = ScoutLedger(path)
        reports = [
            {"url": "https://github.com/owner/done", "slug": "done", "score": 90, "route": "TOOL"},
            {"url": "https://github.com/Owner/No", "slug": "no", "score": 80, "route": "SKIP"},
            {"url": "https://github.com/Owner/Later", "slug": "later", "score": 70, "route": "WATCH"},
            {"url": "https://github.com/Owner/Try", "slug": "try", "score": 60, "route": "TOOL"},
            {"url": "https://github.com/Owner/New", "slug": "new", "score": 50, "route": "TOOL"},
            {"url": "https://example.com/article", "slug": "article", "score": 40, "route": "WATCH"},
            {"url": "https://github.com", "slug": "root", "score": 99, "route": "TOOL"},
            {"url": "https://github.com/search?q=video", "slug": "search", "score": 99, "route": "TOOL"},
            {"url": "mentions)", "slug": "mentions", "score": 99, "route": "TOOL"},
        ]
        rows, dropped = board_rows_from_reports(reports, ledger)
        by_url = {row["url"]: row for row in rows}
        assert dropped == 3 and len(rows) == 7
        assert by_url["https://github.com/owner/done"]["status"] == "✅ ADOPTED"
        assert by_url["https://github.com/Owner/No"]["status"] == "❌ REJECTED"
        assert by_url["https://github.com/Owner/Later"]["status"] == "🅿 PARK"
        assert by_url["https://github.com/Owner/Try"]["status"] == "🧪 PILOT"
        assert by_url["https://github.com/Owner/New"]["status"] == "PENDING"
        assert by_url["https://example.com/article"]["status"] == "PENDING"
        assert by_url["https://github.com/Owner/Missing"] == {
            "url": "https://github.com/Owner/Missing",
            "slug": "github__com__Owner__Missing",
            "score": "—",
            "route": "LEDGER ONLY",
            "status": "❌ REJECTED",
        }


def test_target_failure_cannot_be_reported_as_success() -> None:
    with mock.patch.object(auto_analyst, "process", side_effect=RuntimeError("no report")), \
         mock.patch.object(auto_analyst, "tg"):
        try:
            process_targets(["https://example.com/broken"], {})
        except RuntimeError as exc:
            assert "failed to produce a verified report" in str(exc)
        else:
            raise AssertionError("missing report must fail the analyst job")


def test_text_worker_failure_writes_analysis_error_not_zero_score() -> None:
    """05.10: Qwen-WAF ронял разбор, но отчёт уходил в доску/TG как «0/100 — мимо».
    Теперь score=null + ANALYSIS_ERROR: разбор не выполнен ≠ отказ."""
    rubric = {
        "hard_rejects": [{"id": "dead_and_no_concept"}],
        "criteria": {"relevance": {"weight": 100}},
        "thresholds": {"smoke_test": 60, "concept_note": 7, "watch": 40},
    }
    ctx = {
        "url": "https://github.com/Owner/Broken",
        "kind": "repo",
        "meta": {"full_name": "Owner/Broken"},
        "readme": "",
        "tree": "",
        "manifests": {},
        "license": "?",
        "local": "/tmp/unused",
    }
    ok = mock.Mock(returncode=0, stdout="")
    with tempfile.TemporaryDirectory() as td, \
         mock.patch.object(auto_analyst, "OUTDIR", Path(td)), \
         mock.patch.object(auto_analyst, "fetch", return_value=ctx), \
         mock.patch.object(auto_analyst, "analyze", return_value={"_error": "bad json"}), \
         mock.patch.object(auto_analyst, "rclone", return_value=ok), \
         mock.patch.object(auto_analyst, "verify_remote_json"), \
         mock.patch.object(auto_analyst, "tg"):
        result = process("https://github.com/Owner/Broken", rubric)
        report_path = Path(td) / result["slug"] / "report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
    assert result["score"] is None
    assert result["analysis_failed"] is True
    assert "ANALYSIS_ERROR" in result["route"]
    assert report["score"] is None
    assert report["analysis"]["_analysis_error"] == "bad json"


def test_analysis_error_fails_the_analyst_job() -> None:
    degraded = mock.Mock(return_value={"score": None, "analysis_failed": True,
                                       "route": "⚠️ ANALYSIS_ERROR (разбор не выполнен)"})
    with mock.patch.object(auto_analyst, "process", return_value=degraded.return_value), \
         mock.patch.object(auto_analyst, "tg"):
        try:
            process_targets(["https://github.com/Owner/Broken"], {})
        except RuntimeError as exc:
            assert "без валидного анализа" in str(exc)
        else:
            raise AssertionError("degraded analysis must not look like a green run")


def test_board_renders_analysis_error_without_zero_score() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.json"
        path.write_text(json.dumps({"schema": 1, "repos": {}}), encoding="utf-8")
        rows, _ = board_rows_from_reports([
            {"url": "https://github.com/Owner/Broken", "slug": "broken",
             "score": None, "route": "⚠️ ANALYSIS_ERROR (разбор не выполнен)"},
        ], ScoutLedger(path))
    assert rows[0]["score"] == "—"
    assert "ANALYSIS_ERROR" in rows[0]["route"]


def test_text_worker_defaults_to_kimi_with_openrouter_fallback() -> None:
    env = auto_analyst.os.environ
    saved = env.pop("ANALYST_TEXT_WORKER", None)
    try:
        with mock.patch.object(auto_analyst, "kimi_generate", return_value="kimi ok") as kimi, \
             mock.patch.object(auto_analyst, "openrouter_generate", return_value="or ok") as orf:
            assert auto_analyst.text_generate("p") == "kimi ok"
            kimi.assert_called_once()
            orf.assert_not_called()

            env["ANALYST_TEXT_WORKER"] = "auto"
            assert auto_analyst.text_generate("p") == "kimi ok"

            env["ANALYST_TEXT_WORKER"] = "openrouter"
            assert auto_analyst.text_generate("p") == "or ok"
            # kimi был вызван в дефолтном прогоне и в auto; на явном openrouter — нет
            assert kimi.call_count == 2
    finally:
        env.pop("ANALYST_TEXT_WORKER", None)
        if saved is not None:
            env["ANALYST_TEXT_WORKER"] = saved


def test_text_worker_falls_through_on_empty_kimi_answer() -> None:
    with mock.patch.dict(auto_analyst.os.environ, {"ANALYST_TEXT_WORKER": "auto"}), \
         mock.patch.object(auto_analyst, "kimi_generate", return_value=""), \
         mock.patch.object(auto_analyst, "openrouter_generate", return_value="or ok"):
        assert auto_analyst.text_generate("p") == "or ok"


def test_text_worker_falls_through_when_kimi_raises() -> None:
    with mock.patch.dict(auto_analyst.os.environ, {"ANALYST_TEXT_WORKER": "auto"}), \
         mock.patch.object(auto_analyst, "kimi_generate", side_effect=RuntimeError("kimi down")), \
         mock.patch.object(auto_analyst, "openrouter_generate", return_value="or ok"):
        assert auto_analyst.text_generate("p") == "or ok"


def test_text_worker_raises_when_nobody_answers() -> None:
    with mock.patch.dict(auto_analyst.os.environ, {"ANALYST_TEXT_WORKER": "auto"}), \
         mock.patch.object(auto_analyst, "kimi_generate", return_value=""), \
         mock.patch.object(auto_analyst, "openrouter_generate", return_value=""):
        try:
            auto_analyst.text_generate("p")
        except RuntimeError as exc:
            assert "текстовый воркер не ответил" in str(exc)
        else:
            raise AssertionError("silent empty answers must not pass as a verdict")


def test_analyze_rejects_response_without_scores() -> None:
    rubric = {
        "criteria": {"relevance": {"desc": "да", "weight": 100}},
        "hard_rejects": [{"id": "dead_and_no_concept", "desc": "нет"}],
    }
    ctx = {"url": "u", "kind": "repo", "meta": {}, "license": "?",
           "readme": "", "tree": "", "manifests": {}}
    with mock.patch.object(auto_analyst, "text_generate",
                           return_value='{"summary": "ок, но без оценок"}'):
        out = auto_analyst.analyze(ctx, rubric)
    assert "_error" in out and "scores" in out["_error"]


def test_remote_report_requires_matching_readback() -> None:
    ok = mock.Mock(returncode=0, stdout=json.dumps({
        "url": "https://example.com/tool", "slug": "tool"
    }))
    with mock.patch.object(auto_analyst, "rclone", return_value=ok):
        verify_remote_json("remote/report.json", url="https://example.com/tool", slug="tool")

    wrong = mock.Mock(returncode=0, stdout=json.dumps({
        "url": "https://example.com/other", "slug": "other"
    }))
    with mock.patch.object(auto_analyst, "rclone", return_value=wrong), \
         mock.patch.object(auto_analyst.time, "sleep"):
        try:
            verify_remote_json("remote/report.json", url="https://example.com/tool", slug="tool")
        except RuntimeError as exc:
            assert "remote report verification failed" in str(exc)
        else:
            raise AssertionError("mismatched remote report must fail")


if __name__ == "__main__":
    test_target_validation_preserves_external_route()
    test_board_is_ledger_view_and_drops_github_garbage()
    test_target_failure_cannot_be_reported_as_success()
    test_text_worker_failure_writes_analysis_error_not_zero_score()
    test_analysis_error_fails_the_analyst_job()
    test_board_renders_analysis_error_without_zero_score()
    test_text_worker_defaults_to_kimi_with_openrouter_fallback()
    test_text_worker_falls_through_on_empty_kimi_answer()
    test_text_worker_falls_through_when_kimi_raises()
    test_text_worker_raises_when_nobody_answers()
    test_analyze_rejects_response_without_scores()
    test_remote_report_requires_matching_readback()
    print("auto analyst board lifecycle: all tests passed")
