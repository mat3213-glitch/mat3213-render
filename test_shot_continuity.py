from screenplay_pipeline.shot_continuity import (
    UNKNOWN,
    annotate_handoffs,
    handoff_risk,
    normalize_state,
)
from screenplay_pipeline.prompt_writer import build_prompt
from director import assemble


def state(camera="forward", light="cold window left", palette="slate blue, grey", motif="center"):
    return {
        "camera_vector": camera,
        "light": light,
        "palette": palette,
        "motif_position": motif,
    }


def test_legacy_state_derives_camera_and_keeps_unknowns_neutral():
    derived = normalize_state(None, motion="zoom_in")
    assert derived == {
        "camera_vector": "forward",
        "light": UNKNOWN,
        "palette": UNKNOWN,
        "motif_position": UNKNOWN,
    }
    assert handoff_risk(derived, derived)["score"] == 0.0


def test_opposite_camera_and_visual_break_is_high_risk():
    risk = handoff_risk(
        state(camera="left"),
        state(camera="right", light="warm practical right", palette="amber, black", motif="left"),
    )
    assert risk["score"] == 1.0
    assert risk["level"] == "high"
    assert risk["mismatches"] == ["camera_vector", "light", "palette", "motif_position"]


def test_unknown_fields_are_excluded_instead_of_counted_as_matches():
    risk = handoff_risk(
        {"camera_vector": "left"},
        {"camera_vector": "right", "palette": "amber"},
    )
    assert risk["score"] == 1.0
    assert risk["known_fields"] == ["camera_vector"]


def test_annotation_attaches_deterministic_incoming_risk():
    shots = [
        {"idx": 4, "motion": "pan", "entry_state": state("left"), "exit_state": state("right")},
        {"idx": 5, "motion": "static", "entry_state": state("right"), "exit_state": state("static")},
    ]
    annotate_handoffs(shots)
    assert shots[0]["handoff_risk"] is None
    assert shots[1]["handoff_risk"] == {
        "from_idx": 4,
        "score": 0.0,
        "level": "low",
        "known_fields": ["camera_vector", "light", "palette", "motif_position"],
        "mismatches": [],
        "deltas": {"camera_vector": 0.0, "light": 0.0, "palette": 0.0, "motif_position": 0.0},
    }


def test_director_schema_and_prompt_writer_carry_continuity_contract():
    explicit = state(camera="left", motif="right")
    storyboard = assemble(
        {"track": "test", "central_motif": "paper circle"},
        120.0,
        [
            {"track_pos": 0, "duration": 2, "energy": "low"},
            {"track_pos": 2, "duration": 2, "energy": "medium"},
        ],
        [
            {"seg": 0, "motion": "pan", "entry_state": explicit, "exit_state": explicit},
            {"seg": 1, "motion": "static"},
        ],
        seed="test",
        orientation="vertical",
    )
    assert storyboard["shots"][0]["entry_state"] == explicit
    assert storyboard["shots"][1]["entry_state"]["camera_vector"] == "static"
    assert storyboard["shots"][1]["handoff_risk"]["from_idx"] == 0

    prompt = build_prompt(storyboard, {"content": {}}, [])
    assert "CONTINUITY CONTRACT" in prompt
    assert '"entry_state"' in prompt
    assert '"handoff_risk"' in prompt
