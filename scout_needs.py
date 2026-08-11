#!/usr/bin/env python3
"""Versioned, evidence-first matching for the project's current Repo Scout needs."""
from __future__ import annotations

import json
import re
from pathlib import Path


def _norm(value: object) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold())).strip()


def _matches(text: str, terms: list[str]) -> list[str]:
    padded = f" {text} "
    found = []
    for raw in terms:
        term = _norm(raw)
        if term and f" {term} " in padded:
            found.append(str(raw))
    return found


class CurrentNeeds:
    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid current-needs config {self.path}: {exc}") from exc
        if raw.get("schema") != "repo-scout-current-needs/v1" or raw.get("version") != 1:
            raise ValueError("unsupported current-needs schema/version")
        self.global_negative_terms = self._string_list(raw, "global_negative_terms")
        self.cost_terms = raw.get("integration_cost_terms") or {}
        self.needs = raw.get("needs")
        if not isinstance(self.needs, list) or not self.needs:
            raise ValueError("current-needs config requires a non-empty needs list")
        seen = set()
        for need in self.needs:
            if not isinstance(need, dict):
                raise ValueError("each current need must be an object")
            required = ("id", "category", "priority", "gap", "queries", "positive_terms",
                        "negative_terms", "integration_cost", "duplicate_risk")
            if any(key not in need for key in required):
                raise ValueError(f"current need is missing required fields: {need}")
            if need["id"] in seen or not isinstance(need["priority"], int):
                raise ValueError(f"duplicate/invalid current need: {need['id']}")
            seen.add(need["id"])
            for key in ("queries", "positive_terms", "negative_terms"):
                self._string_list(need, key)

    @staticmethod
    def _string_list(obj: dict, key: str) -> list[str]:
        value = obj.get(key)
        if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
            raise ValueError(f"{key} must be a non-empty string list")
        return value

    def queries(self) -> list[dict]:
        return [
            {"label": need["id"], "category": need["category"], "need_id": need["id"], "query": query}
            for need in self.needs
            for query in need["queries"]
        ]

    def assess(self, repo: dict) -> dict:
        topics = repo.get("topics") or []
        if not isinstance(topics, list):
            topics = []
        text = _norm(" ".join(str(repo.get(k) or "") for k in
                              ("full_name", "name", "description", "language"))
                     + " " + " ".join(str(x) for x in topics))
        global_negative = _matches(text, self.global_negative_terms)
        if global_negative:
            return {"accepted": False, "reason": "saturated/forbidden", "negative": global_negative}

        matches = []
        for need in self.needs:
            evidence = _matches(text, need["positive_terms"])
            negative = _matches(text, need["negative_terms"])
            # Evidence is mandatory. A query label or stars never substitutes for words
            # actually present in repository metadata supplied to the scorer.
            if evidence and not negative:
                matches.append((need, evidence))
        if not matches:
            return {"accepted": False, "reason": "no mandatory evidence", "negative": []}

        need, evidence = max(matches, key=lambda pair: (pair[0]["priority"], len(pair[1])))
        coverage = len(evidence) / len(need["positive_terms"])
        need_score = round(need["priority"] * 10 + coverage * 20, 2)
        cost = self._integration_cost(text, need["integration_cost"])
        return {
            "accepted": True,
            "need_id": need["id"],
            "category": need["category"],
            "priority": need["priority"],
            "gap": need["gap"],
            "evidence": evidence,
            "coverage": round(coverage, 3),
            "need_score": need_score,
            "integration_cost": cost,
            "duplicate_risk": need["duplicate_risk"],
        }

    def _integration_cost(self, text: str, base: str) -> str:
        for level in ("high", "medium", "low"):
            terms = self.cost_terms.get(level) or []
            if _matches(text, terms):
                # Never downgrade the configured baseline because a README says "CLI".
                order = {"low": 0, "medium": 1, "high": 2}
                return level if order[level] > order.get(base, 0) else base
        return base
