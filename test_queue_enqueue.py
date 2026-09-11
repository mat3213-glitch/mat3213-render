#!/usr/bin/env python3
"""Тесты патч-аппенда + reconcile publish-очереди (аудит 2026-09-06, P1):
- оба набора элементов двух writers сохраняются (гонка не теряет элемент навсегда);
- идемпотентность, сохранение чужих platform-позиций;
- повреждённый queue.json НЕ перезаписывается;
- crash после submit без reconcile добирается следующим reconcile."""
from __future__ import annotations

import json
import os
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
    require_writer_context,
    main,
    QUEUE_DST,
)


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
        self.cats: list[str] = []


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
            self.remote.cats.append(path)
            return self._result(self.remote.store[path])
        elif op == "lsf":
            folder = argv[-1].removeprefix("ydrive:") + "/"
            names = [name for name in self.remote.store if name.startswith(folder)
                     and "/" not in name[len(folder):]]
            return self._result("\n".join(name.rsplit("/", 1)[-1] + "\n" for name in names))
        elif op == "moveto":
            src = argv[1].removeprefix("ydrive:")
            dst = argv[2].removeprefix("ydrive:")
            if src not in self.remote.store:
                raise AssertionError(f"moveto несуществующего файла: {src}")
            self.remote.store[dst] = self.remote.store.pop(src)
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
        self.env = patch.dict(os.environ, {
            "GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "owner/render",
            "GITHUB_WORKFLOW_REF": "owner/render/.github/workflows/reconcile_publish_queue.yml@refs/heads/main",
            "GITHUB_REF": "refs/heads/main", "GITHUB_JOB": "reconcile",
            "RUNNER_ENVIRONMENT": "github-hosted", "GITHUB_EVENT_NAME": "workflow_dispatch",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

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
            {"platform": "vk", "entry": {"id": "ok_1", "media": "m", "caption": "c"}}]}))), 1)
        with self.assertRaisesRegex(ValueError, "безопасный"):
            parse_patch(json.dumps({"items": [
                {"platform": "vk", "entry": {"id": "../up", "media": "m", "caption": "c"}}]}))

    def test_parse_patch_rejects_malformed_publish_entries(self) -> None:
        with self.assertRaisesRegex(ValueError, "media"):
            parse_patch(json.dumps({"items": [
                {"platform": "tg", "entry": {"id": "poison", "caption": "c"}}]}))
        with self.assertRaisesRegex(ValueError, "caption"):
            parse_patch(json.dumps({"items": [
                {"platform": "vk", "entry": {"id": "poison", "media": "m.mp4"}}]}))
        with self.assertRaisesRegex(ValueError, "own_track"):
            parse_patch(json.dumps({"items": [
                {"platform": "youtube", "entry": {
                    "id": "poison", "media": "m.mp4", "title": "x", "own_track": "false"}}]}))
        with self.assertRaisesRegex(ValueError, "platform"):
            parse_patch(json.dumps({"items": [
                {"platform": "unknown", "entry": {"id": "x", "media": "m.mp4", "caption": "c"}}]}))

    def test_parse_patch_accepts_remove_by_id(self) -> None:
        items = parse_patch(json.dumps({"items": [
            {"platform": "vk", "op": "remove", "entry": {"id": "old_vk"}}]}))
        self.assertEqual(items, [{"platform": "vk", "entry": {"id": "old_vk"}, "op": "remove"}])

    def test_parse_patch_rejects_remove_without_id_and_unknown_op(self) -> None:
        with self.assertRaisesRegex(ValueError, "id"):
            parse_patch(json.dumps({"items": [
                {"platform": "vk", "op": "remove", "entry": {}}]}))
        with self.assertRaisesRegex(ValueError, "операция"):
            parse_patch(json.dumps({"items": [
                {"platform": "vk", "op": "wipe", "entry": {"id": "x"}}]}))

    def test_fold_removes_by_id_keeping_others(self) -> None:
        q = base_queue()  # te tg/ok/vk buckets
        self.assertEqual(fold(q, [{"platform": "vk", "entry": {"id": "old_vk"}, "op": "remove"}]), 1)
        self.assertEqual(q["vk"], [])
        self.assertEqual(q["tg"], [{"id": "keep_tg", "media": "a/1.mp4"}])

    def test_fold_remove_idempotent_and_reconcile_applies_remove(self) -> None:
        remote = RemoteStore()
        runner = RemoteSubprocess(remote)
        with patch("queue_enqueue.rclone", side_effect=runner):
            submit_patch(QUEUE_DST, [{"platform": "vk", "op": "remove", "entry": {"id": "old_vk"}}],
                         "cleanup", work=self.work)
            added, code = reconcile(QUEUE_DST, work=self.work)
            self.assertEqual((added, code), (1, 0))
            q = load_queue(remote.store[QUEUE_DST])
            self.assertEqual(q["vk"], [])
            self.assertEqual(q["tg"], [{"id": "keep_tg", "media": "a/1.mp4"}])
            # повторный reconcile (патч уже в queue.processed) — без изменений
            added, code = reconcile(QUEUE_DST, work=self.work)
            self.assertEqual((added, code), (0, 0))

    def test_fold_remove_and_add_last_wins(self) -> None:
        q = base_queue()
        fold(q, [{"platform": "vk", "entry": {"id": "old_vk"}, "op": "remove"}])
        fold(q, [{"platform": "vk", "entry": {"id": "old_vk", "media": "new/full.mp4", "caption": "c"}}])
        self.assertEqual(q["vk"], [{"id": "old_vk", "media": "new/full.mp4", "caption": "c"}])

    def test_submit_then_reconcile(self) -> None:
        remote = RemoteStore()
        runner = RemoteSubprocess(remote)
        with patch("queue_enqueue.rclone", side_effect=runner):
            name, _ = submit_patch(QUEUE_DST, frozen_items("frozen_v3", "T"),
                                   "frozen_v3", work=self.work)
            self.assertIn(f"{QUEUE_DST.rsplit('/', 1)[0]}/queue.d/{name}", remote.store)
            added, code = reconcile(QUEUE_DST, work=self.work)
        self.assertEqual(added, 5)
        self.assertEqual(code, 0)
        self.assertNotIn(f"{QUEUE_DST.rsplit('/', 1)[0]}/queue.d/{name}", remote.store)
        self.assertIn(f"{QUEUE_DST.rsplit('/', 1)[0]}/queue.processed/{name}", remote.store)
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
            first_cats = len(remote.cats)
            added, code = reconcile(QUEUE_DST, work=self.work)
        self.assertEqual((added, code), (0, 0))
        self.assertEqual(len(remote.cats), first_cats)
        self.assertEqual(len(json.loads(remote.store[QUEUE_DST])["youtube"]), 4)

    def test_second_writer_blocked_before_read_and_existing_prefix_preserved(self) -> None:
        remote = RemoteStore()
        other = {"platform": "youtube", "entry": {
            "id": "other_slug_short_1", "media": "other/section_1.mp4",
            "type": "video", "title": "Other", "own_track": True}}
        runner = RemoteSubprocess(remote)
        other_work = self.work / "other"
        other_work.mkdir()
        attempted = False
        def interleave(argv, **kwargs):
            nonlocal attempted
            if argv[0] == "copyto" and argv[2] == "ydrive:" + QUEUE_DST and not attempted:
                attempted = True
                submit_patch(QUEUE_DST, [other], "other_slug", work=other_work)
                with self.assertRaisesRegex(RuntimeError, "another reconcile"):
                    reconcile(QUEUE_DST, work=other_work)
                self.assertFalse((other_work / "queue.json").exists())
            return runner(argv, **kwargs)
        with patch("queue_enqueue.rclone", side_effect=interleave):
            submit_patch(QUEUE_DST, frozen_items("frozen_v3", "T"), "frozen_v3",
                         work=self.work)
            added, code = reconcile(QUEUE_DST, work=self.work)
            self.assertEqual((added, code), (5, 0))
            prefix = load_queue(remote.store[QUEUE_DST])["youtube"]
            added, code = reconcile(QUEUE_DST, work=other_work)
        self.assertEqual((added, code), (1, 0))
        self.assertTrue(attempted)
        self.assertEqual(load_queue(remote.store[QUEUE_DST])["youtube"][:len(prefix)], prefix)
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

    def test_local_and_foreign_workflow_fail_before_remote_io(self):
        for key in ("GITHUB_ACTIONS", "GITHUB_WORKFLOW_REF", "GITHUB_REF", "GITHUB_JOB", "RUNNER_ENVIRONMENT"):
            with patch.dict(os.environ, {key: "wrong"}), patch("queue_enqueue.rclone") as remote:
                with self.assertRaises(RuntimeError):
                    reconcile(QUEUE_DST, work=self.work)
                remote.assert_not_called()
        with self.assertRaises(RuntimeError):
            require_writer_context("other/queue.json")

    def test_cli_matches_workflow_argument_order(self):
        remote = RemoteStore()
        with patch("queue_enqueue.rclone", side_effect=RemoteSubprocess(remote)):
            self.assertEqual(main(["submit", "--queue-dst", QUEUE_DST, "--slug", "cli", "--title", "T"]), 0)
            self.assertEqual(main(["reconcile", "--queue-dst", QUEUE_DST]), 0)

    def test_crash_during_write_releases_lock_and_retry_is_idempotent(self):
        remote = RemoteStore()
        runner = RemoteSubprocess(remote)
        def crash(argv, **kwargs):
            result = runner(argv, **kwargs)
            if argv[0] == "copyto" and argv[2] == "ydrive:" + QUEUE_DST:
                raise RuntimeError("crash after accepted upload")
            return result
        with patch("queue_enqueue.rclone", side_effect=runner):
            submit_patch(QUEUE_DST, frozen_items("retry", "T"), "retry", work=self.work)
        with patch("queue_enqueue.rclone", side_effect=crash), self.assertRaises(RuntimeError):
            reconcile(QUEUE_DST, work=self.work)
        before = remote.store[QUEUE_DST]
        with patch("queue_enqueue.rclone", side_effect=runner):
            self.assertEqual(reconcile(QUEUE_DST, work=self.work), (0, 0))
        self.assertEqual(remote.store[QUEUE_DST], before)
        self.assertFalse(any("/queue.d/" in key for key in remote.store))

    def test_listing_failure_and_broken_patch_do_not_write_queue(self):
        remote = RemoteStore()
        runner = RemoteSubprocess(remote)
        def fail_list(argv, **kwargs):
            if argv[0] == "lsf":
                raise RuntimeError("unavailable")
            return runner(argv, **kwargs)
        with patch("queue_enqueue.rclone", side_effect=fail_list), self.assertRaises(RuntimeError):
            reconcile(QUEUE_DST, work=self.work)
        remote.store[QUEUE_DST.rsplit('/', 1)[0] + "/queue.d/broken.json"] = "{broken"
        with patch("queue_enqueue.rclone", side_effect=runner), self.assertRaises(ValueError):
            reconcile(QUEUE_DST, work=self.work)
        self.assertEqual(remote.store[QUEUE_DST], sample())

    def test_workflow_has_one_serialized_writer_and_recovery_triggers(self):
        import yaml
        folder = Path(__file__).parent / ".github/workflows"
        writer = yaml.safe_load((folder / "reconcile_publish_queue.yml").read_text())
        triggers = writer.get("on", writer.get(True))
        self.assertIn("schedule", triggers)
        self.assertIn("workflow_dispatch", triggers)
        self.assertEqual(writer["concurrency"], {"group": "publish_queue_write", "cancel-in-progress": False})
        self.assertEqual(list(writer["jobs"]), ["reconcile"])
        self.assertEqual(writer["jobs"]["reconcile"]["runs-on"], "ubuntu-latest")
        for filename in ("publish_frozen_v3.yml", "platform_variants.yml"):
            producer = yaml.safe_load((folder / filename).read_text())
            self.assertIn(producer["name"], triggers["workflow_run"]["workflows"])
            self.assertNotEqual(producer["concurrency"]["group"], writer["concurrency"]["group"])
        writers = []
        for path in folder.glob("*.yml"):
            data = yaml.safe_load(path.read_text())
            for job in (data.get("jobs", {}) if isinstance(data, dict) else {}).values():
                for step in job.get("steps", []):
                    if "queue_enqueue.py reconcile" in step.get("run", ""):
                        writers.append(path.name)
        self.assertEqual(writers, ["reconcile_publish_queue.yml"])


if __name__ == "__main__":
    unittest.main()
