#!/usr/bin/env python3
"""Тесты патч-аппенда + reconcile publish-очереди (аудит 2026-09-06, P1):
- оба набора элементов двух writers сохраняются (гонка не теряет элемент навсегда);
- идемпотентность, сохранение чужих platform-позиций;
- повреждённый queue.json НЕ перезаписывается;
- crash после submit без reconcile добирается следующим reconcile."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from queue_enqueue import (
    enqueue_frozen,
    fold,
    frozen_items,
    load_queue,
    parse_patch,
    reconcile,
    submit_patch,
    validate_slug,
)

QUEUE_DST = "xxx/queue.json"


def base_queue() -> dict:
    return {
        "tg": [{"id": "keep_tg", "media": "a/1.mp4"}],
        "ok": [{"id": "keep_ok", "media": "b/2.jpg"}],
        "vk": [{"id": "old_vk", "media": "old/full.mp4"}],
    }


def sample() -> str:
    return json.dumps(base_queue(), ensure_ascii=False, indent=2)


class RemoteStore:
    def __init__(self) -> None:
        self.store: dict[str, str] = {QUEUE_DST: sample()}
        self.uploads: list[str] = []


class RemoteSubprocess(object):
    """FakeRemote в форме subprocess.CompletedProcess (stdout/stderr/returncode)."""

    def __init__(self, remote: FakeRemote) -> None:
        self.remote = remote

    def __call__(self, argv: list[str], *, check: bool = True):
        op = argv[0]
        if op == "copyto":
            if argv[1].startswith("ydrive:"):
                key = argv[1].removeprefix("ydrive:")
                if key not in self.remote.store:
                    raise AssertionError(f"copyto несуществующего файла: {key}")
                Path(argv[2]).write_text(self.remote.store[key], encoding="utf-8")
            elif argv[2].startswith("ydrive:"):
                self.remote.store[argv[2].removeprefix("ydrive:")] = \
                    Path(argv[1]).read_text(encoding="utf-8")
                self.remote.uploads.append(argv[1])
            else:
                raise AssertionError(f"неизвестный copyto-путь: {argv}")
        elif op == "cat":
            path = argv[1].removeprefix("ydrive:")
            if path not in self.remote.store:
                raise AssertionError(f"cat несуществующего файла: {path}")
            return self._result(self.remote.store[path])
        elif op == "lsf":
            folder = argv[-1].removeprefix("ydrive:") + "/"
            names = [name for name in self.remote.store if name.startswith(folder)
                     and "/" not in name[len(folder):]]
            return self._result("\n".join(name.rsplit("/", 1)[-1] + "\n" for name in names))
        else:
            raise AssertionError(f"неожиданная команда: {argv}")
        return self._result("")

    @staticmethod
    def _result(out: str):
        class _CP:
            pass
        cp = _CP()
        cp.returncode = 0
        cp.stdout = out
        cp.stderr = ""
        return cp


class QueueEnqueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_enqueue_adds_vk_and_4_shorts_keeping_others(self) -> None:
        q = base_queue()
        added = enqueue_frozen(q, "frozen_v3", "yaromat — Frozen Space")
        self.assertEqual(added, 5)
        self.assertEqual(q["tg"], [{"id": "keep_tg", "media": "a/1.mp4"}])
        self.assertEqual(q["ok"], [{"id": "keep_ok", "media": "b/2.jpg"}])
        self.assertEqual([x["id"] for x in q["vk"]], ["old_vk", "frozen_v3_vk"])
        self.assertEqual([x["id"] for x in q["youtube"]],
                         [f"frozen_v3_short_{n}" for n in range(1, 5)])

    def test_fold_is_idempotent(self) -> None:
        q = base_queue()
        items = frozen_items("frozen_v3", "T")
        self.assertEqual(fold(q, items), 5)
        self.assertEqual(fold(q, items), 0)
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

    def test_parse_patch_rejects_bad_ids(self) -> None:
        self.assertEqual(len(parse_patch(json.dumps({"items": [
            {"platform": "vk", "entry": {"id": "ok_1", "media": "m"}}]}))), 1)
        with self.assertRaisesRegex(ValueError, "безопасный"):
            parse_patch(json.dumps({"items": [
                {"platform": "vk", "entry": {"id": "../up", "media": "m"}}]}))

    def test_submit_then_reconcile(self) -> None:
        remote = RemoteStore()
        runner = RemoteSubprocess(remote)
        with patch("queue_enqueue.rclone", side_effect=runner):
            name, _ = submit_patch(QUEUE_DST, frozen_items("frozen_v3", "T"),
                                   "frozen_v3", work=self.work)
            self.assertIn(f"xxx/queue.d/{name}", remote.store)
            added, code = reconcile(QUEUE_DST, work=self.work)
        self.assertEqual(added, 5)
        self.assertEqual(code, 0)
        q = load_queue(remote.store[QUEUE_DST])
        self.assertEqual([x["id"] for x in q["youtube"]],
                         [f"frozen_v3_short_{n}" for n in range(1, 5)])
        self.assertEqual(q["tg"], [{"id": "keep_tg", "media": "a/1.mp4"}])
        self.assertTrue(remote.uploads)

    def test_reconcile_is_idempotent(self) -> None:
        remote = RemoteStore()
        runner = RemoteSubprocess(remote)
        with patch("queue_enqueue.rclone", side_effect=runner):
            submit_patch(QUEUE_DST, frozen_items("frozen_v3", "T"), "frozen_v3",
                         work=self.work)
            reconcile(QUEUE_DST, work=self.work)
            added, code = reconcile(QUEUE_DST, work=self.work)
        self.assertEqual((added, code), (0, 0))
        self.assertEqual(len(json.loads(remote.store[QUEUE_DST])["youtube"]), 4)

    def test_two_writers_converge_no_lost_update(self) -> None:
        """Гонка двух writers: даже «протухший» reconcile, записавший меньший набор
        ПОСЛЕ бОльшего, не теряет элементы навсегда — durable-патчи восстанавливают union."""
        remote = RemoteStore()
        other = {"platform": "youtube", "entry": {
            "id": "other_slug_short_1", "media": "other/section_1.mp4",
            "type": "video", "title": "Other", "own_track": True}}
        runner = RemoteSubprocess(remote)
        with patch("queue_enqueue.rclone", side_effect=runner):
            submit_patch(QUEUE_DST, frozen_items("frozen_v3", "T"), "frozen_v3",
                         work=self.work)
            submit_patch(QUEUE_DST, [other], "other_slug", work=self.work)
            added, code = reconcile(QUEUE_DST, work=self.work)
            self.assertEqual(added, 6)
            # Протухший reconcile: читал только патч frozen_v3 и переписывает им сейчас.
            stale_queue = base_queue()
            fold(stale_queue, frozen_items("frozen_v3", "T"))
            remote.store[QUEUE_DST] = json.dumps(stale_queue, ensure_ascii=False, indent=2)
            # Следующий reconcile видит ВСЕ патчи (никто их не удаляет) → union вернулся.
            added, code = reconcile(QUEUE_DST, work=self.work)
        self.assertEqual((added, code), (1, 0))
        youtube = [x["id"] for x in json.loads(remote.store[QUEUE_DST])["youtube"]]
        self.assertIn("frozen_v3_short_1", youtube)
        self.assertIn("other_slug_short_1", youtube)
        self.assertEqual(json.loads(remote.store[QUEUE_DST])["tg"],
                         [{"id": "keep_tg", "media": "a/1.mp4"}])

    def test_crash_after_submit_is_picked_up_later(self) -> None:
        remote = RemoteStore()
        runner = RemoteSubprocess(remote)
        with patch("queue_enqueue.rclone", side_effect=runner):
            submit_patch(QUEUE_DST, frozen_items("frozen_v3", "T"), "frozen_v3",
                         work=self.work)
            # writer упал до reconcile → queue.json не тронут.
        self.assertEqual(json.loads(remote.store[QUEUE_DST])["vk"],
                         [{"id": "old_vk", "media": "old/full.mp4"}])
        with patch("queue_enqueue.rclone", side_effect=runner):
            added, code = reconcile(QUEUE_DST, work=self.work)
        self.assertEqual((added, code), (5, 0))
        self.assertEqual(len(json.loads(remote.store[QUEUE_DST])["youtube"]), 4)

    def test_broken_remote_queue_not_overwritten(self) -> None:
        remote = RemoteStore()
        runner = RemoteSubprocess(remote)
        with patch("queue_enqueue.rclone", side_effect=runner):
            submit_patch(QUEUE_DST, frozen_items("frozen_v3", "T"), "frozen_v3",
                         work=self.work)
            remote.store[QUEUE_DST] = "{broken"
            before = remote.store[QUEUE_DST]
            added, code = reconcile(QUEUE_DST, work=self.work)
            self.assertEqual((added, code), (0, 2))
            self.assertEqual(remote.store[QUEUE_DST], before)


if __name__ == "__main__":
    unittest.main()