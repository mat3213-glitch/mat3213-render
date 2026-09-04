"""Contract tests for consumer-aware effects; no media or FFmpeg required."""
from __future__ import annotations

import ast
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import effect_registry as registry  # noqa: E402
import pinterest_board_uniquize as uniquizer  # noqa: E402


def _legacy_storyboard_effects() -> dict[str, str]:
    """Read the legacy literal without importing its JOB_ID-bound script."""
    source = (ROOT / "storyboard_render_job.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "EFFECT_VF"
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("legacy EFFECT_VF literal is missing")


def test_blank_effect_is_explicitly_neutral():
    off = registry.resolve("", consumer="storyboard", fmt="vertical")
    assert off == {
        "name": "", "type": "off", "filter": "", "policy": "allowed", "filter_hash": ""
    }


def test_unknown_effect_fails_closed():
    try:
        registry.resolve("not_an_effect", consumer="storyboard", fmt="vertical")
    except registry.EffectRegistryError:
        return
    raise AssertionError("unknown effect silently resolved")


def test_vertical_profile_matches_legacy_storyboard_contract():
    for name, legacy_filter in _legacy_storyboard_effects().items():
        resolved = registry.resolve(name, consumer="storyboard", fmt="vertical")
        assert resolved["type"] == "vf"
        assert resolved["filter"] == legacy_filter


def test_vertical_profile_never_uses_landscape_crop_dimensions():
    for spec in registry.load()["profiles"]["storyboard"]["vertical"].values():
        assert "crop=1280:720" not in spec["filter"]


def test_resolution_is_stable_and_receiptable():
    first = registry.resolve("faded_film", consumer="storyboard", fmt="vertical")
    assert first == registry.resolve("faded_film", consumer="storyboard", fmt="vertical")
    assert len(registry.registry_version()) == 64
    assert len(first["filter_hash"]) == 64
    assert registry.receipt_entry("faded_film", consumer="storyboard", fmt="vertical")["name"] == "faded_film"


def test_legacy_nonvertical_falls_back_to_vertical_profile():
    vertical = registry.resolve("faded_film", consumer="storyboard", fmt="vertical")
    assert registry.resolve("faded_film", consumer="storyboard", fmt="landscape") == vertical


def test_profile_policies_are_valid_and_explicit():
    policies = {
        spec["policy"]
        for spec in registry.load()["profiles"]["storyboard"]["vertical"].values()
    }
    assert policies <= {"allowed", "restricted", "forbidden"}
    assert registry.resolve("flash", consumer="storyboard", fmt="vertical")["policy"] == "restricted"
    assert registry.resolve("color_pulse", consumer="storyboard", fmt="vertical")["policy"] == "forbidden"


def test_landscape_profile_preserves_legacy_uniquize_declarations():
    data = registry.load()
    for effect_type in ("vf", "complex"):
        for name, legacy in data[effect_type].items():
            resolved = registry.resolve_spec(name, consumer="uniquize", fmt="landscape")
            assert resolved["type"] == effect_type
            if "_generator" in legacy:
                assert resolved["generator"] == legacy["_generator"]
            else:
                assert resolved["filter"] == legacy.get("filter", legacy.get("complex", ""))
            if "flash_pct_range" in legacy:
                assert resolved["flash_pct_range"] == legacy["flash_pct_range"]


def test_landscape_runtime_matches_legacy_generator_draws():
    legacy_db = registry.load()
    names = [*legacy_db["vf"], *legacy_db["complex"]]
    for name in names:
        random.seed(417)
        legacy = uniquizer._legacy_resolve_effect_filter(name, legacy_db, fps=24.0, duration=1.0)
        random.seed(417)
        migrated = uniquizer.resolve_effect_filter(name, legacy_db, fps=24.0, duration=1.0)
        assert migrated == legacy


def test_landscape_unknown_effect_fails_closed():
    try:
        registry.resolve_spec("not_an_effect", consumer="uniquize", fmt="landscape")
    except registry.EffectRegistryError:
        return
    raise AssertionError("unknown uniquize effect silently resolved")
