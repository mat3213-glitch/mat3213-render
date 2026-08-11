from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
TARGETS = (
    "screenplay_pipeline/plastic_gate_core.py",
    "art_judge.py",
    "aesthetic_check.py",
    "gemini_judge.py",
    "cv_probe.py",
    "plastic_gate.py",
    "cv_train.py",
    "plastic_gate_nightly.py",
    "gemini_judge_v2.py",
    "plastic_calib.py",
)


class ShellFreeSubprocessTests(unittest.TestCase):
    def test_target_modules_do_not_enable_shell(self):
        for rel in TARGETS:
            tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"), filename=rel)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for kw in node.keywords:
                    if kw.arg == "shell":
                        self.assertIsInstance(kw.value, ast.Constant, rel)
                        self.assertFalse(kw.value.value, rel)

    def test_shell_wrapper_calls_receive_literal_argv(self):
        """Prevent a future string command from silently reintroducing parsing risk."""
        for rel in TARGETS:
            tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"), filename=rel)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                if node.func.id == "sh" and node.args:
                    self.assertIsInstance(node.args[0], ast.List, f"{rel}:{node.lineno}")

    def test_rclone_globs_are_literal_argv_values(self):
        sources = "\n".join((ROOT / rel).read_text(encoding="utf-8") for rel in TARGETS)
        self.assertNotIn("2>/dev/null", sources)
        self.assertIn('"--include", "*.mp4"', sources)
        self.assertIn('"--include", "*.jpg"', sources)


if __name__ == "__main__":
    unittest.main()
