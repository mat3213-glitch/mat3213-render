#!/usr/bin/env python3
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class RenderEntrypointPolicyTests(unittest.TestCase):
    def test_prohibited_workflows_fail_closed(self):
        workflows = [
            "tsx_clip.yml",
            "render_full.yml",
            "vinyl_teaser.yml",
            "vinyl_viral.yml",
            "vinyl_label.yml",
            "sp_scene_ltx.yml",
        ]
        for name in workflows:
            with self.subTest(workflow=name):
                text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
                self.assertIn("blocked:", text)
                self.assertIn("exit 1", text)
                self.assertIn("if: ${{ false }}", text)

    def test_scene_dispatch_cannot_submit_ltx(self):
        source = (ROOT / "screenplay_pipeline" / "scene_dispatch.py").read_text(encoding="utf-8")
        start = source.index("ENGINE_WORKFLOWS =")
        end = source.index("\n\n# LTX-Video", start)
        policy = source[start:end]
        self.assertNotIn('"ltx":', policy)
        self.assertIn('ENGINE_ORDER = ["qwen"]', policy)


if __name__ == "__main__":
    unittest.main()
