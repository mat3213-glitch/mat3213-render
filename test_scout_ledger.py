#!/usr/bin/env python3
"""Regression tests for Repo Scout durable lifecycle state (stdlib only)."""
import json
import tempfile
from pathlib import Path

from scout_ledger import ScoutLedger, load_excluded_names


def test_ledger_and_seen_are_case_insensitive() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ledger = root / "ledger.json"
        seen = root / "seen.json"
        ledger.write_text(json.dumps({"schema": 1, "repos": {
            "Owner/Adopted": {"status": "adopted"},
            "Owner/Parked": {"status": "park"},
        }}), encoding="utf-8")
        seen.write_text(json.dumps(["Legacy/Seen"]), encoding="utf-8")

        loaded, excluded = load_excluded_names(ledger, seen)
        assert loaded.contains("owner/adopted")
        assert loaded.entry("OWNER/PARKED")["status"] == "park"
        assert excluded == {"owner/adopted", "owner/parked", "legacy/seen"}


def test_bare_mapping_migrates() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.json"
        path.write_text(json.dumps({"Owner/Repo": {"status": "pilot"}}), encoding="utf-8")
        assert ScoutLedger(path).contains("owner/repo")


def test_invalid_status_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.json"
        path.write_text(json.dumps({"repos": {"Owner/Repo": {"status": "maybe"}}}), encoding="utf-8")
        try:
            ScoutLedger(path)
        except ValueError:
            return
        raise AssertionError("invalid lifecycle status was accepted")


def test_repo_scout_excludes_before_shortlist() -> None:
    """Exercise the actual candidate collector, not only the ledger helper."""
    import repo_scout

    old_queries = repo_scout.load_queries
    old_search = repo_scout.search_github
    old_trending = repo_scout.fetch_trending
    old_sleep = repo_scout.time.sleep
    try:
        repo_scout.load_queries = lambda: [
            {"query": "video", "category": "video", "label": "test"}
        ]
        repo_scout.search_github = lambda *args, **kwargs: [
            {"full_name": "Owner/Adopted", "html_url": "https://github.com/Owner/Adopted",
             "description": "video render", "language": "Python", "stargazers_count": 10,
             "pushed_at": ""},
            {"full_name": "Owner/Fresh", "html_url": "https://github.com/Owner/Fresh",
             "description": "video render", "language": "Python", "stargazers_count": 5,
             "pushed_at": ""},
        ]
        repo_scout.fetch_trending = lambda *args, **kwargs: []
        repo_scout.time.sleep = lambda *_: None

        items = repo_scout.build_candidates(excluded_names={"owner/adopted"})
        assert [item["full_name"] for item in items] == ["Owner/Fresh"]
    finally:
        repo_scout.load_queries = old_queries
        repo_scout.search_github = old_search
        repo_scout.fetch_trending = old_trending
        repo_scout.time.sleep = old_sleep


if __name__ == "__main__":
    test_ledger_and_seen_are_case_insensitive()
    test_bare_mapping_migrates()
    test_invalid_status_fails_closed()
    test_repo_scout_excludes_before_shortlist()
    print("scout ledger: all tests passed")
