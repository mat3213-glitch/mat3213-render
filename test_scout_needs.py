#!/usr/bin/env python3
"""Deterministic tests for versioned Repo Scout current-needs scoring."""
from pathlib import Path

from scout_needs import CurrentNeeds

HERE = Path(__file__).resolve().parent


def test_mandatory_evidence_and_forbidden_topics() -> None:
    needs = CurrentNeeds(HERE / "repo_scout_current_needs.v1.json")
    useful = needs.assess({
        "full_name": "owner/xfade-tools",
        "description": "FFmpeg transition toolkit with xfade easing and crossfade expression presets",
        "language": "Python",
    })
    generic = needs.assess({
        "full_name": "owner/popular-video",
        "description": "The most powerful platform for video creators",
    })
    forbidden = needs.assess({
        "full_name": "owner/agent-video",
        "description": "Free LLM coding agent with an FFmpeg transition helper",
    })
    assert useful["accepted"] and useful["need_id"] == "organic_transitions"
    assert useful["priority"] == 10 and useful["evidence"]
    assert not generic["accepted"] and generic["reason"] == "no mandatory evidence"
    assert not forbidden["accepted"] and forbidden["reason"] == "saturated/forbidden"


def test_priority_dominates_coverage() -> None:
    needs = CurrentNeeds(HERE / "repo_scout_current_needs.v1.json")
    high = needs.assess({"description": "An xfade easing helper"})
    lower = needs.assess({
        "description": "Stock video API and creative commons video catalog with video license metadata"
    })
    assert high["accepted"] and lower["accepted"]
    assert high["need_score"] > lower["need_score"]


def test_vcr_is_pattern_evidence_not_renderer_adoption() -> None:
    needs = CurrentNeeds(HERE / "repo_scout_current_needs.v1.json")
    assessment = needs.assess({
        "full_name": "coltonbatts/VCR",
        "description": ("Headless Rust motion graphics renderer with a visual verification loop, "
                        "snapshot previews, video contact sheet and JSON error contract"),
        "language": "Rust",
    })
    assert assessment["accepted"]
    assert assessment["need_id"] == "preview_verification_contracts"
    assert assessment["integration_cost"] == "medium"
    assert assessment["duplicate_risk"].startswith("high")


def test_queries_only_come_from_versioned_needs() -> None:
    needs = CurrentNeeds(HERE / "repo_scout_current_needs.v1.json")
    queries = needs.queries()
    text = " ".join(q["query"].casefold() for q in queries)
    for forbidden in ("free llm", "coding agent", "social media scheduler", "procedural motion graphics"):
        assert forbidden not in text
    assert all(q.get("need_id") for q in queries)


def test_latest_and_digest_explain_decision() -> None:
    import tempfile
    import repo_scout

    item = {
        "full_name": "owner/tool", "stars": 10, "category": "video",
        "html_url": "https://github.com/owner/tool", "description": "description",
        "need_id": "organic_transitions", "priority": 10, "gap": "organic gap",
        "evidence": ["xfade easing"], "integration_cost": "low",
        "duplicate_risk": "medium — base exists",
    }
    digest = repo_scout.build_digest([item], 1)
    assert "gap: organic_transitions" in digest
    assert "интеграция: low" in digest and "дубль-риск:" in digest

    old_report = repo_scout.REPORT_FILE
    try:
        with tempfile.TemporaryDirectory() as td:
            repo_scout.REPORT_FILE = Path(td) / "latest.md"
            repo_scout.write_report([item])
            report = repo_scout.REPORT_FILE.read_text(encoding="utf-8")
            assert "gap `organic_transitions`" in report
            assert "integration cost: low" in report and "duplicate-risk:" in report
    finally:
        repo_scout.REPORT_FILE = old_report


def test_empty_strict_result_uses_query_grounded_fallback() -> None:
    import repo_scout

    needs = CurrentNeeds(HERE / "repo_scout_current_needs.v1.json")
    old_queries = repo_scout.load_queries
    old_search = repo_scout.search_github
    old_trending = repo_scout.fetch_trending
    old_sleep = repo_scout.time.sleep
    try:
        repo_scout.load_queries = lambda *_: [
            {"query": "camera movement prompt", "category": "craft",
             "need_id": "camera_prompt_contracts", "label": "test"}
        ]
        repo_scout.search_github = lambda *args, **kwargs: [
            {"full_name": "Owner/Fallback", "html_url": "https://github.com/Owner/Fallback",
             "description": "Useful repository with sparse metadata", "language": "Python",
             "stargazers_count": 7, "pushed_at": ""},
            {"full_name": "Owner/Seen", "html_url": "https://github.com/Owner/Seen",
             "description": "Useful repository with sparse metadata", "language": "Python",
             "stargazers_count": 9, "pushed_at": ""},
        ]
        repo_scout.fetch_trending = lambda *args, **kwargs: []
        repo_scout.time.sleep = lambda *_: None

        items = repo_scout.build_candidates(
            excluded_names={"owner/seen"}, needs=needs, fallback_total=3,
        )
        assert [item["full_name"] for item in items] == ["Owner/Fallback"]
        assert items[0]["fallback_mode"] == "query-grounded"
        assert "query matched:" in items[0]["evidence"][0]
        assert repo_scout.LAST_BUILD_STATS["fallback_candidates"] == 1
    finally:
        repo_scout.load_queries = old_queries
        repo_scout.search_github = old_search
        repo_scout.fetch_trending = old_trending
        repo_scout.time.sleep = old_sleep


def test_zero_status_digest_is_not_empty() -> None:
    import repo_scout

    digest = repo_scout.build_status_digest({
        "shortlist": 0,
        "candidates": 0,
        "lifecycle_dropped": 20,
        "needs_dropped": 75,
    })
    assert "Repo Scout: прогон завершён" in digest
    assert "shortlist: 0" in digest
    assert "auto_analyst: не запускался" in digest


if __name__ == "__main__":
    test_mandatory_evidence_and_forbidden_topics()
    test_priority_dominates_coverage()
    test_vcr_is_pattern_evidence_not_renderer_adoption()
    test_queries_only_come_from_versioned_needs()
    test_latest_and_digest_explain_decision()
    test_empty_strict_result_uses_query_grounded_fallback()
    test_zero_status_digest_is_not_empty()
    print("scout current needs: all tests passed")
