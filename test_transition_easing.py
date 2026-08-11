#!/usr/bin/env python3
"""Лёгкие contract-тесты transition easing; FFmpeg не запускается."""

import sys
from pathlib import Path


PIPELINE = Path(__file__).resolve().parent / "screenplay_pipeline"
sys.path.insert(0, str(PIPELINE))

import transition_render as render  # noqa: E402
import transition_router as router  # noqa: E402


def test_easing_allowlist_is_small_and_deterministic():
    assert set(router.XFADE_EASING) == {"fade", "dissolve"}
    first = router.xfade_render_spec("gl-dissolve")
    assert first == router.xfade_render_spec("gl-dissolve")
    assert first[0] == "custom"
    assert "P*P*(3-2*P)" in first[1]


def test_non_soft_transitions_do_not_get_custom_expressions():
    assert router.xfade_render_spec("dip") == ("fadeblack", None)
    assert router.xfade_render_spec("film-burn") == ("fadegrays", None)
    assert router.xfade_render_spec("hard-cut") == (None, None)
    assert router.xfade_render_spec("unknown") == (None, None)


def test_dissolve_profile_is_explicitly_deterministic():
    profile = router.XFADE_EASING["dissolve"]
    assert profile["curve"] == "smoothstep"
    assert "random" not in profile["expr"].lower()
    assert all(term in profile["expr"] for term in ("X*12.9898", "Y*78.233", "PLANE*37.719"))


def test_renderer_emits_custom_expression_without_changing_timeline():
    _, expr = router.xfade_render_spec("gl-dissolve")
    fc, label, total = render.build_xfade_chain(
        [2.7, 2.0],
        [None, ("custom", 0.7, expr)],
    )
    assert "xfade=transition=custom:duration=0.700:offset=2.000:expr=" in fc
    assert expr in fc
    assert label == "[x1]"
    assert total == 4.0


def test_renderer_keeps_legacy_pair_contract_and_rejects_bad_shape():
    fc, _, total = render.build_xfade_chain([1.5, 1.5], [None, ("fadeblack", 0.28)])
    assert "transition=fadeblack" in fc
    assert ":expr=" not in fc
    assert total == 2.72

    try:
        render.build_xfade_chain([1.0, 1.0], [None, ("fade",)])
    except ValueError as exc:
        assert "(name, duration[, expr])" in str(exc)
    else:
        raise AssertionError("invalid transition tuple was accepted")
