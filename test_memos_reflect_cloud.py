#!/usr/bin/env python3
"""Тесты облачной рефлексии: контракт «rclone не бросает» и живой сбой 17.10.

Инцидент 2026-10-17: один нечитаемый объект на Яндекс.Диске (HTTP 500 на битом
файле) ронял весь memos reflect — код выше уже умеет такой файл пропускать, но
subprocess.TimeoutExpired escapes ломал этот контракт.
"""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import memos_reflect_cloud as m


GOOD = '{"ts": "2026-10-14 15:58:30", "kind": "error", "actor": "gh-ai"}\n'


def fake_remote(*a, timeout=120):
    """lsf отдаёт два файла, один из которых ЯД прочитать не даёт."""
    op = a[0]
    if op == "lsf":
        return subprocess.CompletedProcess(a, 0, "broken.jsonl\ngood.jsonl\n", "")
    if op == "copyto" and a[1].endswith("broken.jsonl"):
        return subprocess.CompletedProcess(a, 124, "", f"timeout {timeout}s")
    if op == "copyto":
        if not a[1].startswith(m.YD_FEED):
            return subprocess.CompletedProcess(a, 124, "", f"timeout {timeout}s")
        Path(a[2]).write_text(GOOD, encoding="utf-8")
        return subprocess.CompletedProcess(a, 0, "", "")
    return subprocess.CompletedProcess(a, 0, "", "")


class RcloneTimeoutIsNotAnException(unittest.TestCase):
    def test_timeout_returns_returncode_not_raises(self):
        with patch.object(m.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("rclone", 60)) as run:
            result = m.rclone("copyto", "ydrive:x", "/tmp/x", timeout=60)
        self.assertEqual(result.returncode, 124)
        self.assertIn("timeout", result.stderr)
        self.assertEqual(run.call_args.kwargs["timeout"], 60)

    def test_pull_feed_skips_unreadable_feed_file(self):
        with patch.object(m, "rclone", side_effect=fake_remote):
            recs = m.pull_feed()
        self.assertEqual([r["actor"] for r in recs], ["gh-ai"])

    def test_pull_checkpoint_survives_unreadable_object(self):
        with patch.object(m, "rclone", side_effect=fake_remote):
            self.assertEqual(m.pull_checkpoint(), "")


if __name__ == "__main__":
    unittest.main()