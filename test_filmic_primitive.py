from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from filmic_primitive import apply_primitive


def test_off_primitive_is_neutral(tmp_path: Path):
    source = tmp_path / "missing.mp4"
    with pytest.raises(FileNotFoundError):
        apply_primitive(source, tmp_path / "out.mp4", primitive="off")


def test_unknown_primitive_fails_closed(tmp_path: Path):
    source = tmp_path / "input.mp4"
    source.write_bytes(b"not media")
    with pytest.raises(Exception):
        apply_primitive(source, tmp_path / "out.mp4", primitive="unknown")
