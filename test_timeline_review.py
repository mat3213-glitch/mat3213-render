from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from timeline_review import TimelineReviewError, cut_plan, normalize_shots, sample_times


def test_normalize_storyboard_derives_sequential_in_points():
    shots = normalize_shots({"shots": [{"t_dur": 2, "section": "intro"}, {"t_dur": 3, "effect": "hflip"}]}, duration=5)
    assert shots == [
        {"index": 0, "t_in": 0.0, "t_dur": 2.0, "t_out": 2.0, "section": "intro"},
        {"index": 1, "t_in": 2.0, "t_dur": 3.0, "t_out": 5.0, "effect": "hflip"},
    ]


def test_storyboard_overlap_and_overrun_fail_before_media_work():
    with pytest.raises(TimelineReviewError, match="overlaps"):
        normalize_shots([{"t_in": 0, "t_dur": 2}, {"t_in": 1, "t_dur": 2}], duration=4)
    with pytest.raises(TimelineReviewError, match="exceeds"):
        normalize_shots([{"t_in": 0, "t_dur": 5}], duration=4)


def test_cut_plan_and_sample_times_are_stable_and_clamped():
    shots = normalize_shots([{"t_dur": 2}, {"t_dur": 2}], duration=4)
    assert sample_times(4, 3) == [1.0, 2.0, 3.0]
    assert cut_plan(shots, duration=4, radius=1.5) == [{
        "index": 0, "t": 2.0, "left_shot": 0, "right_shot": 1,
        "frame_times": [0.5, 2.0, 3.5],
    }]


def test_invalid_sample_count_fails_closed():
    with pytest.raises(TimelineReviewError, match="positive"):
        sample_times(1, 0)
