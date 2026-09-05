#!/usr/bin/env python3
"""Тесты CAS-обновления publish-очереди (P1.5b): идемпотентность, сохранение
других platform-позиций и conflict/mismatch без молчаливой перезаписи queue.json."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from queue_enqueue import cas_update, enqueue_frozen, load_queue, validate_slug

REMOTE_KEY = "xxx/queue.json"


def base_queue() -> dict:
    return {
        "tg": [{"id": "keep_tg", "media": "a/1.mp4"}],
        "ok": [{"id": "keep_ok", "media": "b/2.jpg"}],
        "vk": [{"id": "old_vk", "media": "old/full.mp4"}],
    }


def sample() -> str:
    return json.dumps(base_queue(), ensure_ascii=False, indent=2)


class FakeRemote:
    """In-memory rclone-копия: download copyto remote→local, upload copyto local→remote."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {REMOTE_KEY: sample()}

    def run(self, argv: list[str]) -> int:
        if argv[0] != "copyto":
            raise AssertionError(f"неожиданная команда rclone: {argv}")
        if argv[1] == f"ydrive:{REMOTE_KEY}":
            Path(argv[2]).write_text(self.store[REMOTE_KEY], encoding="utf-8")
        elif argv[2] == f"ydrive:{REMOTE_KEY}":
            self.store[REMOTE_KEY] = Path(argv[1]).read_text(encoding="utf-8")
        else:
            raise AssertionError(f"неизвестный путь: {argv}")
        return 0


class QueueEnqueueTests(unittest.TestCase):
    def test_enqueue_adds_vk_and_4_shorts_keeping_others(self) -> None:
        q = base_queue()
        added = enqueue_frozen(q, "frozen_v3", "yaromat — Frozen Space")
        self.assertEqual(added, 5)
        self.assertEqual(q["tg"], [{"id": "keep_tg", "media": "a/1.mp4"}])
        self.assertEqual(q["ok"], [{"id": "keep_ok", "media": "b/2.jpg"}])
        self.assertEqual([x["id"] for x in q["vk"]], ["old_vk", "frozen_v3_vk"])
        self.assertEqual([x["id"] for x in q["youtube"]],
                         [f"frozen_v3_short_{n}" for n in range(1, 5)])

    def test_enqueue_is_idempotent(self) -> None:
        q = base_queue()
        enqueue_frozen(q, "frozen_v3", "T")
        self.assertEqual(enqueue_frozen(q, "frozen_v3", "T"), 0)
        self.assertEqual(len(q["vk"]), 2)
        self.assertEqual(len(q["youtube"]), 4)

    def test_slug_must_be_safe(self) -> None:
        with self.assertRaisesRegex(ValueError, "slug"):
            validate_slug("../../escape")
        with self.assertRaisesRegex(ValueError, "slug"):
            validate_slug("a/b")
        self.assertEqual(validate_slug("frozen_v3"), "frozen_v3")

    def test_load_queue_fails_closed_on_broken_json(self) -> None:
        with self.assertRaisesRegex(ValueError, "повреждён"):
            load_queue("{broken")

    def test_cas_ok_path(self) -> None:
        remote = FakeRemote()
        with patch("queue_enqueue.rclone", side_effect=remote.run):
            with tempfile.TemporaryDirectory() as td:
                added, code = cas_update(REMOTE_KEY, "frozen_v3", "T", work=Path(td))
        self.assertEqual(added, 5)
        self.assertEqual(code, 0)
        self.assertEqual([x["id"] for x in json.loads(remote.store[REMOTE_KEY])["youtube"]],
                         [f"frozen_v3_short_{n}" for n in range(1, 5)])

    def test_cas_conflict_aborts_without_upload(self) -> None:
        store = {"before": sample(), "now": json.dumps({"youtube": [{"id": "intruder"}]})}
        uploads: list[list[str]] = []

        def fake_rclone(argv: list[str]) -> int:
            if argv[0] != "copyto":
                raise AssertionError(argv)
            if argv[1] == f"ydrive:{REMOTE_KEY}":
                key = "before" if "queue_before" in argv[2] else "now"
                Path(argv[2]).write_text(store[key], encoding="utf-8")
            else:
                uploads.append(argv)
            return 0

        with patch("queue_enqueue.rclone", side_effect=fake_rclone):
            with tempfile.TemporaryDirectory() as td:
                added, code = cas_update(REMOTE_KEY, "frozen_v3", "T", work=Path(td))
        self.assertEqual(added, 5)
        self.assertEqual(code, 3)
        self.assertEqual(uploads, [])

    def test_cas_upload_mismatch_reports_manual_review(self) -> None:
        staged_content: dict[str, str] = {}

        def fake_rclone(argv: list[str]) -> int:
            if argv[0] != "copyto":
                raise AssertionError(argv)
            if argv[1] == f"ydrive:{REMOTE_KEY}":
                # download: before/current/verified фазы
                if "queue_before" in argv[2] or "queue_current" in argv[2]:
                    Path(argv[2]).write_text(sample(), encoding="utf-8")
                else:
                    # verified: эмулируем порчу после аплоада (глюк без проверки)
                    Path(argv[2]).write_text(json.dumps({"youtube": [{"id": "corrupt"}]}),
                                             encoding="utf-8")
            else:
                staged_content["queue_staged.json"] = Path(argv[1]).read_text(encoding="utf-8")
            return 0

        with patch("queue_enqueue.rclone", side_effect=fake_rclone):
            with tempfile.TemporaryDirectory() as td:
                added, code = cas_update(REMOTE_KEY, "frozen_v3", "T", work=Path(td))
        self.assertEqual(added, 5)
        self.assertEqual(code, 4)
        self.assertIn("queue_staged.json", staged_content)

    def test_broken_remote_queue_skips_without_write(self) -> None:
        downloads = 0
        uploads = 0

        def fake_rclone(argv: list[str]) -> int:
            nonlocal downloads, uploads
            if argv[0] != "copyto":
                raise AssertionError(argv)
            if argv[1] == f"ydrive:{REMOTE_KEY}":
                downloads += 1
                Path(argv[2]).write_text("{broken", encoding="utf-8")
            else:
                uploads += 1
            return 0

        with patch("queue_enqueue.rclone", side_effect=fake_rclone):
            with tempfile.TemporaryDirectory() as td:
                added, code = cas_update(REMOTE_KEY, "frozen_v3", "T", work=Path(td))
        self.assertEqual((added, code), (0, 0))
        self.assertEqual(downloads, 1)
        self.assertEqual(uploads, 0)


if __name__ == "__main__":
    unittest.main()