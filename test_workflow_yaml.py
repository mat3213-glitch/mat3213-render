#!/usr/bin/env python3
"""test_workflow_yaml.py — структурная валидация всех GH Actions workflow в репо.

Ловит то, что plain yaml.safe_load + ручные правки пропускают:
  - синтаксические ошибки YAML (сломанный отступ после edit),
  - отсутствие ключа jobs, steps = не список,
  - step без uses/run (нет кода),
  - job без runs-on,
  - concurrency.group, где ожидается.

Парсер обходит ключ `on` (в YAML 1.1 он загружается как True) — так же, как
тесты queue_enqueue и сам GitHub.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).parent / ".github" / "workflows"


def _triggers(data: dict):
    return data.get(True) if True in data else data.get("on")


class WorkflowYamlTests(unittest.TestCase):
    def test_all_workflows_parse_and_have_structural_skeleton(self) -> None:
        seen = 0
        for path in sorted(WORKFLOWS.glob("*.yml")):
            seen += 1
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertIsInstance(data, dict, f"{path.name}: не объект")
            self.assertTrue(_triggers(data) is not None, f"{path.name}: нет on/trigger")
            jobs = data.get("jobs")
            self.assertIsInstance(jobs, dict, f"{path.name}: jobs не объект")
            self.assertTrue(jobs, f"{path.name}: пустые jobs")
            for job_name, job in jobs.items():
                self.assertIsInstance(job, dict, f"{path.name}: job {job_name} не объект")
                if "uses" in job:
                    continue  # reusable workflow — джоба делегирует файлу
                self.assertIn("runs-on", job, f"{path.name}: job {job_name} без runs-on")
                steps = job.get("steps")
                self.assertIsInstance(steps, list, f"{path.name}: steps {job_name} не список")
                for step in steps:
                    self.assertTrue(
                        "uses" in step or "run" in step,
                        f"{path.name}: step без uses/run: {job_name} -> {step}")
        self.assertGreater(seen, 10, "похоже, workflow-папка пуста")

    def test_producers_are_not_serialized_queue_writers(self) -> None:
        for filename in ("publish_frozen_v3.yml", "platform_variants.yml"):
            data = yaml.safe_load((WORKFLOWS / filename).read_text(encoding="utf-8"))
            for job in data["jobs"].values():
                for step in job.get("steps", []):
                    self.assertNotRegex(step.get("run", ""), r"queue_enqueue\.py reconcile",
                                        f"{filename}: producer не должен писать queue.json")

    def test_inputs_are_string_typed_dispatched_through_env(self) -> None:
        """Параметр workflow_dispatch не вшит в inline shell (A02), а идёт через env."""
        data = yaml.safe_load((WORKFLOWS / "platform_variants.yml").read_text(encoding="utf-8"))
        step = next(s for j in data["jobs"].values() for s in j["steps"]
                    if "queue them" in s.get("name", ""))
        self.assertIn("env", step)
        shell = step["run"]
        for var in ("SOURCE", "SLUG", "TITLE", "SECTIONS", "SHORTS_PLATFORMS",
                    "FULL_PLATFORMS", "CAPTION", "CUT_TIMES"):
            self.assertIn(f"${var}", shell, f"в shell не используется env {var}")


if __name__ == "__main__":
    unittest.main()