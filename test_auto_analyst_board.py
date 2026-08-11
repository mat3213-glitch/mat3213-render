#!/usr/bin/env python3
"""Regression tests for Auto Analyst targets and ledger-backed board projection."""
import json
import tempfile
from pathlib import Path

from auto_analyst import board_rows_from_reports, normalize_analyst_target
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
        assert dropped == 3 and len(rows) == 6
        assert by_url["https://github.com/owner/done"]["status"] == "✅ ADOPTED"
        assert by_url["https://github.com/Owner/No"]["status"] == "❌ REJECTED"
        assert by_url["https://github.com/Owner/Later"]["status"] == "🅿 PARK"
        assert by_url["https://github.com/Owner/Try"]["status"] == "🧪 PILOT"
        assert by_url["https://github.com/Owner/New"]["status"] == "PENDING"
        assert by_url["https://example.com/article"]["status"] == "PENDING"


if __name__ == "__main__":
    test_target_validation_preserves_external_route()
    test_board_is_ledger_view_and_drops_github_garbage()
    print("auto analyst board lifecycle: all tests passed")
