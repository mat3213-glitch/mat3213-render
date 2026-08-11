"""Deterministic continuity metadata for adjacent storyboard shots.

The state deliberately describes only image-space properties that can be carried
across a cut.  Identity, faces and characters are outside this contract.
"""
from __future__ import annotations

from typing import Any


STATE_FIELDS = ("camera_vector", "light", "palette", "motif_position")
UNKNOWN = "unspecified"

CAMERA_VECTORS = {
    "static", "forward", "backward", "left", "right", "up", "down", "mixed",
    UNKNOWN,
}

_MOTION_VECTOR = {
    "static": "static",
    "slow_push": "forward",
    "zoom_in": "forward",
    "zoom_out": "backward",
    "drift": "mixed",
    "handheld": "mixed",
    "tilt": "mixed",
    "pan": "mixed",
}

_OPPOSITE = {
    ("forward", "backward"),
    ("left", "right"),
    ("up", "down"),
}

_WEIGHTS = {
    "camera_vector": 0.35,
    "light": 0.25,
    "palette": 0.25,
    "motif_position": 0.15,
}


def _token(value: Any) -> str:
    """Make free-text state values stable without inventing visual semantics."""
    if not isinstance(value, str):
        return UNKNOWN
    value = " ".join(value.strip().lower().split())
    return value or UNKNOWN


def default_state(motion: str | None = None) -> dict[str, str]:
    """Backward-compatible state for a legacy shot.

    Camera direction is safely derivable from the existing motion vocabulary.
    Other visual facts remain unknown and therefore do not increase risk.
    """
    return {
        "camera_vector": _MOTION_VECTOR.get(str(motion or ""), UNKNOWN),
        "light": UNKNOWN,
        "palette": UNKNOWN,
        "motif_position": UNKNOWN,
    }


def normalize_state(value: Any, *, motion: str | None = None) -> dict[str, str]:
    fallback = default_state(motion)
    raw = value if isinstance(value, dict) else {}
    out = {field: _token(raw.get(field, fallback[field])) for field in STATE_FIELDS}
    if out["camera_vector"] not in CAMERA_VECTORS:
        out["camera_vector"] = fallback["camera_vector"]
    return out


def _camera_delta(left: str, right: str) -> float:
    if left == right:
        return 0.0
    if "mixed" in (left, right):
        return 0.5
    if "static" in (left, right):
        return 0.6
    if (left, right) in _OPPOSITE or (right, left) in _OPPOSITE:
        return 1.0
    return 0.75


def handoff_risk(exit_state: Any, entry_state: Any) -> dict[str, Any]:
    """Score an adjacent cut from known state only, deterministically.

    Unknown values are excluded from the denominator.  A legacy storyboard thus
    remains usable and is not branded risky merely because metadata is absent.
    """
    left = normalize_state(exit_state)
    right = normalize_state(entry_state)
    weighted_delta = 0.0
    known_weight = 0.0
    mismatches: list[str] = []
    deltas: dict[str, float] = {}

    for field in STATE_FIELDS:
        a, b = left[field], right[field]
        if UNKNOWN in (a, b):
            continue
        delta = _camera_delta(a, b) if field == "camera_vector" else float(a != b)
        deltas[field] = delta
        known_weight += _WEIGHTS[field]
        weighted_delta += delta * _WEIGHTS[field]
        if delta:
            mismatches.append(field)

    score = round(weighted_delta / known_weight, 3) if known_weight else 0.0
    level = "high" if score >= 0.67 else ("medium" if score >= 0.34 else "low")
    return {
        "score": score,
        "level": level,
        "known_fields": list(deltas),
        "mismatches": mismatches,
        "deltas": deltas,
    }


def annotate_handoffs(shots: list[dict[str, Any]]) -> None:
    """Normalize states and attach incoming handoff risk to every shot in-place."""
    for shot in shots:
        motion = shot.get("motion")
        shot["entry_state"] = normalize_state(shot.get("entry_state"), motion=motion)
        shot["exit_state"] = normalize_state(shot.get("exit_state"), motion=motion)

    if not shots:
        return
    shots[0]["handoff_risk"] = None
    for index in range(1, len(shots)):
        risk = handoff_risk(shots[index - 1]["exit_state"], shots[index]["entry_state"])
        shots[index]["handoff_risk"] = {
            "from_idx": shots[index - 1].get("idx", index - 1),
            **risk,
        }
