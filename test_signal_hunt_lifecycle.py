#!/usr/bin/env python3
"""Regression tests for Repo Scout lifecycle reuse in the Grok bridge."""
from scout_ledger import github_full_name
from signal_hunt import filter_lifecycle_urls


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


if __name__ == "__main__":
    test_github_url_normalization()
    test_grok_bridge_filters_lifecycle_before_queue()
    print("signal hunt lifecycle: all tests passed")
