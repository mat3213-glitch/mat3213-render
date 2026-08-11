#!/usr/bin/env python3
"""Regression: Repo Scout must not depend on a long-lived PAT."""
from pathlib import Path


def test_repo_scout_uses_repository_token() -> None:
    workflow = (Path(__file__).parent / ".github" / "workflows" / "repo_scout.yml").read_text()
    assert "secrets.SCOUT_PAT" not in workflow
    assert "secrets.GH_MODELS_TOKEN" not in workflow
    assert workflow.count("${{ github.token }}") >= 2
    assert "actions: write" in workflow


def test_signal_hunt_uses_repository_token_for_dispatch() -> None:
    workflow = (Path(__file__).parent / ".github" / "workflows" / "signal_hunt.yml").read_text()
    assert "actions: write" in workflow
    assert "GH_DISPATCH_TOKEN: ${{ github.token }}" in workflow
    assert "GH_DISPATCH_TOKEN: ${{ secrets.GH_MODELS_TOKEN }}" not in workflow
    assert "secrets.GH_MODELS_TOKEN" not in workflow


if __name__ == "__main__":
    test_repo_scout_uses_repository_token()
    test_signal_hunt_uses_repository_token_for_dispatch()
    print("repo scout workflow auth: all tests passed")
