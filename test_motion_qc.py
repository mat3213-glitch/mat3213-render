from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from motion_qc import MotionQcError, mean_abs_diff, sustained_windows


def test_mean_absolute_difference_is_exact():
    assert mean_abs_diff(bytes([0, 10]), bytes([4, 14])) == 4.0


def test_sustained_windows_are_deterministic_and_minimum_bounded():
    assert sustained_windows([0.2, 0.3, 4, 0.1, 0.2, 0.3], at_or_below=0.5, min_pairs=3) == [(3, 6)]
    assert sustained_windows([50, 51, 10, 52, 53], at_or_above=45, min_pairs=2) == [(0, 2), (3, 5)]


def test_invalid_window_configuration_fails_closed():
    with pytest.raises(MotionQcError, match="exactly one"):
        sustained_windows([1], at_or_below=1, at_or_above=2)
    with pytest.raises(MotionQcError, match="same non-zero"):
        mean_abs_diff(b"", b"")
