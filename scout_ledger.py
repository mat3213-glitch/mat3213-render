#!/usr/bin/env python3
"""Durable lifecycle decisions for Repo Scout repositories.

The ledger is deliberately separate from ``repo_scout_seen.json``:

* ``seen`` is the append-only compatibility journal of names already shown;
* the ledger records an explicit project decision: adopted/rejected/park/pilot.

Both sources are used as an early deny-list by :mod:`repo_scout`, before shortlist
construction and before any LLM call.  The file uses only stdlib JSON so it can be
edited and reviewed like any other repository state file.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

STATUSES = frozenset({"adopted", "rejected", "park", "pilot"})
SCHEMA_VERSION = 1
_GITHUB_NON_REPO_ROOTS = frozenset({
    "about", "apps", "collections", "customer-stories", "enterprise", "events",
    "explore", "features", "marketplace", "new", "notifications", "orgs",
    "pricing", "search", "security", "settings", "site", "sponsors", "topics",
})


def canonical_name(full_name: str) -> str:
    """GitHub repository names are case-insensitive; keep comparisons that way."""
    return str(full_name or "").strip().strip("/").casefold()


def github_full_name(url: str) -> str | None:
    """Extract ``owner/repo`` from a GitHub repository URL.

    Deep links (issues/tree/blob), query strings, fragments and a trailing ``.git``
    are accepted.  Non-GitHub URLs and GitHub pages without two path components
    intentionally return ``None``.
    """
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or parsed.netloc.casefold() not in {
        "github.com", "www.github.com"
    }:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    if owner.casefold() in _GITHUB_NON_REPO_ROOTS:
        return None
    if repo.casefold().endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo:
        return None
    return f"{owner}/{repo}"


class ScoutLedger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.repos: dict[str, dict] = {}
        self._by_canonical: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid scout ledger {self.path}: {exc}") from exc

        # v1 is {"schema": 1, "repos": {"owner/repo": {...}}}.  Accept a bare
        # mapping as a migration convenience for early hand-written ledgers.
        repos = raw.get("repos", {}) if isinstance(raw, dict) and "repos" in raw else raw
        if not isinstance(repos, dict):
            raise ValueError(f"invalid scout ledger {self.path}: repos must be an object")

        for key, value in repos.items():
            if not isinstance(value, dict):
                raise ValueError(f"invalid scout ledger entry {key!r}: object expected")
            full_name = str(value.get("full_name") or key).strip().strip("/")
            status = str(value.get("status") or "").strip().lower()
            if not full_name or status not in STATUSES:
                raise ValueError(
                    f"invalid scout ledger entry {key!r}: status must be one of {sorted(STATUSES)}"
                )
            entry = dict(value)
            entry["full_name"] = full_name
            entry["status"] = status
            canon = canonical_name(full_name)
            if canon in self._by_canonical:
                raise ValueError(f"duplicate scout ledger repository (case-insensitive): {full_name}")
            self.repos[full_name] = entry
            self._by_canonical[canon] = entry

    def contains(self, full_name: str) -> bool:
        return canonical_name(full_name) in self._by_canonical

    def entry(self, full_name: str) -> dict | None:
        return self._by_canonical.get(canonical_name(full_name))

    def excluded_names(self) -> set[str]:
        return set(self._by_canonical)


def load_seen_names(path: Path) -> set[str]:
    """Load legacy seen state, tolerating a missing/corrupt file as old scout did."""
    path = Path(path)
    if not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(raw, list):
        return set()
    return {canonical_name(name) for name in raw if canonical_name(name)}


def load_excluded_names(ledger_path: Path, seen_path: Path) -> tuple[ScoutLedger, set[str]]:
    """Compatibility migration view: explicit ledger decisions plus legacy seen names."""
    ledger = ScoutLedger(ledger_path)
    return ledger, ledger.excluded_names() | load_seen_names(seen_path)
