#!/usr/bin/env python3
"""Regression tests for Repo Scout lifecycle/current-needs reuse in Grok bridge."""
from pathlib import Path

from scout_ledger import github_full_name
from scout_needs import CurrentNeeds
from signal_hunt import NEEDS_FILE, assess_grok_github_item, filter_lifecycle_urls, grok_signals


def test_github_url_normalization() -> None:
    assert github_full_name("https://github.com/Owner/Repo") == "Owner/Repo"
    assert github_full_name("https://github.com/Owner/Repo.git?x=1#readme") == "Owner/Repo"
    assert github_full_name("https://github.com/Owner/Repo/issues/42") == "Owner/Repo"
    assert github_full_name("https://example.com/Owner/Repo") is None
    assert github_full_name("https://github.com/topics/video") is None


def test_grok_bridge_filters_lifecycle_before_queue() -> None:
    urls = [
        "https://github.com/Owner/Adopted",
        "https://github.com/OWNER/SEEN/tree/main",
        "https://github.com/Owner/Fresh",
        "https://example.com/tool",
    ]
    kept, dropped = filter_lifecycle_urls(urls, {"owner/adopted", "owner/seen"})
    assert kept == ["https://github.com/Owner/Fresh", "https://example.com/tool"]
    assert [name.casefold() for name in dropped] == ["owner/adopted", "owner/seen"]


def test_grok_github_link_requires_current_need_evidence() -> None:
    needs = CurrentNeeds(Path(NEEDS_FILE))
    useful = assess_grok_github_item({
        "source_link": "https://github.com/Owner/Transitions",
        "what": "FFmpeg transition library",
        "why_us": "Adds xfade easing expressions",
    }, needs)
    saturated = assess_grok_github_item({
        "source_link": "https://github.com/Owner/Agent",
        "what": "Free LLM coding agent",
        "why_us": "Generic automation",
    }, needs)
    unrelated = assess_grok_github_item({
        "source_link": "https://github.com/Owner/Popular",
        "what": "Popular general purpose project",
    }, needs)
    assert useful and useful["accepted"] and useful["need_id"] == "organic_transitions"
    assert saturated and not saturated["accepted"] and saturated["reason"] == "saturated/forbidden"
    assert unrelated and not unrelated["accepted"] and unrelated["reason"] == "no mandatory evidence"


def test_signal_source_failure_is_blocking() -> None:
    from unittest import mock

    with mock.patch("signal_hunt.subprocess.run", side_effect=TimeoutError):
        try:
            grok_signals()
        except RuntimeError as exc:
            assert str(exc) == "Grok signal source failed: TimeoutError"
        else:
            raise AssertionError("signal source failure must not become a successful empty run")


if __name__ == "__main__":
    test_github_url_normalization()
    test_grok_bridge_filters_lifecycle_before_queue()
    test_grok_github_link_requires_current_need_evidence()
    test_signal_source_failure_is_blocking()
    print("signal hunt lifecycle: all tests passed")
