"""Consumer-aware, deterministic effect registry.

The old project has two valid consumers with different geometry: the landscape
uniquizer and the vertical storyboard renderer.  An effect name is therefore
not enough to select a filter: a caller must declare its consumer and format.
This module is deliberately read-only; it does not pick or stack effects.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


EFFECTS_PATH = Path(__file__).with_name("effects.json")
OFF_EFFECT = {
    "name": "", "type": "off", "filter": "", "policy": "allowed", "filter_hash": ""
}


class EffectRegistryError(ValueError):
    """The requested effect is absent or unavailable for this consumer."""


def load() -> dict[str, Any]:
    try:
        data = json.loads(EFFECTS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EffectRegistryError(f"cannot load effects registry: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict):
        raise EffectRegistryError("effects registry has no profiles object")
    return data


def registry_version() -> str:
    """Content hash makes a receipt reproducible without mutating its schema."""
    return hashlib.sha256(EFFECTS_PATH.read_bytes()).hexdigest()


def receipt_entry(name: str | None, *, consumer: str, fmt: str) -> dict[str, str]:
    """Return the small, non-filter receipt view of a resolved declaration."""
    resolved = resolve(name, consumer=consumer, fmt=fmt)
    return {key: resolved[key] for key in ("name", "type", "policy", "filter_hash")}


def resolve_spec(name: str | None, *, consumer: str, fmt: str) -> dict[str, Any]:
    """Resolve a declared effect specification or fail closed.

    A profile may declare a literal FFmpeg filter or a named legacy generator.
    The latter is needed by the landscape uniquizer: its generator owns the
    established random draw and must stay untouched during this migration.
    """
    effect_name = str(name or "").strip()
    if not effect_name:
        return dict(OFF_EFFECT)
    data = load()
    profiles = data["profiles"].get(consumer)
    if not isinstance(profiles, dict):
        raise EffectRegistryError(f"unknown effect consumer: {consumer}")
    profile = profiles.get(fmt) or profiles.get("vertical") or profiles.get("default")
    if not isinstance(profile, dict):
        raise EffectRegistryError(f"no {consumer}/{fmt} effect profile")
    spec = profile.get(effect_name)
    if not isinstance(spec, dict):
        raise EffectRegistryError(
            f"unknown effect {effect_name!r} for {consumer}/{fmt}"
        )
    policy = str(spec.get("policy", "allowed"))
    if policy not in {"allowed", "restricted", "forbidden"}:
        raise EffectRegistryError(f"invalid policy for effect {effect_name!r}")
    effect_type = str(spec.get("type", ""))
    filt = spec.get("filter", "")
    generator = spec.get("generator", "")
    if effect_type not in {"vf", "complex"}:
        raise EffectRegistryError(f"invalid type for effect {effect_name!r}")
    if not isinstance(filt, str) or not isinstance(generator, str) or (not filt and not generator):
        raise EffectRegistryError(f"effect {effect_name!r} has no filter or generator")
    resolved = {
        "name": effect_name,
        "type": effect_type,
        "filter": filt,
        "generator": generator,
        "policy": policy,
        "filter_hash": hashlib.sha256(
            json.dumps(spec, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
    if "flash_pct_range" in spec:
        resolved["flash_pct_range"] = spec["flash_pct_range"]
    return resolved


def resolve(name: str | None, *, consumer: str, fmt: str) -> dict[str, str]:
    """Resolve one literal filter declaration or fail closed for an unknown name.

    Exact format wins; ``default`` is a compatibility profile for legacy
    storyboard formats.  A blank effect is explicitly neutral so old EDLs
    without a creative effect keep rendering unchanged.
    """
    resolved = resolve_spec(name, consumer=consumer, fmt=fmt)
    if resolved["type"] == "off":
        return resolved
    if resolved["generator"]:
        raise EffectRegistryError(
            f"effect {resolved['name']!r} for {consumer}/{fmt} requires its consumer generator"
        )
    return {key: resolved[key] for key in ("name", "type", "filter", "policy", "filter_hash")}
