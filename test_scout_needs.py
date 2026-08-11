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


if __name__ == "__main__":
    test_mandatory_evidence_and_forbidden_topics()
    test_priority_dominates_coverage()
    test_queries_only_come_from_versioned_needs()
    test_latest_and_digest_explain_decision()
    print("scout current needs: all tests passed")
