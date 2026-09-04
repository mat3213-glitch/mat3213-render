from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from runtime_safety import (
    CHROMIUM_HARDENED_ARGS,
    chromium_launch_kwargs,
    ffmpeg_argv,
    ffmpeg_input,
    http_call,
    safe_leaf_name,
    safe_local_path,
    safe_remote_path,
)


class _Response:
    def __init__(self, status_code: int, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.closed = False

    def close(self):
        self.closed = True


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def request(self, method, url, **kwargs):
        self.calls += 1
        return self.responses.pop(0)


class RuntimeSafetyTests(unittest.TestCase):
    def test_get_retries_retryable_status_with_backoff(self):
        first, final = _Response(503), _Response(200)
        session = _Session([first, final])
        delays = []
        result = http_call(
            "GET", "https://example.test", session=session,
            retries=2, backoff=0.25, sleep=delays.append,
        )
        self.assertIs(result, final)
        self.assertTrue(first.closed)
        self.assertEqual(session.calls, 2)
        self.assertEqual(delays, [0.25])

    def test_post_does_not_retry_without_explicit_opt_in(self):
        session = _Session([_Response(503), _Response(200)])
        result = http_call("POST", "https://example.test", session=session, retries=3)
        self.assertEqual(result.status_code, 503)
        self.assertEqual(session.calls, 1)

    def test_chromium_args_are_present_and_deduplicated(self):
        kwargs = chromium_launch_kwargs(args=["--no-sandbox", "--mute-audio"], channel="chrome")
        self.assertTrue(kwargs["headless"])
        self.assertEqual(kwargs["args"].count("--no-sandbox"), 1)
        for arg in CHROMIUM_HARDENED_ARGS:
            self.assertIn(arg, kwargs["args"])

    def test_leaf_and_remote_paths_fail_closed(self):
        self.assertEqual(safe_leaf_name("clip_01.mp4", suffixes=(".mp4",)), "clip_01.mp4")
        self.assertEqual(safe_remote_path("Content factory/jobs/clip_01.mp4"),
                         "Content factory/jobs/clip_01.mp4")
        for bad in ("../clip.mp4", "/tmp/clip.mp4", "ydrive:clip.mp4", "a//b"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                safe_remote_path(bad)
        for bad in ("../clip.mp4", "dir/clip.mp4", "clip name.mp4"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                safe_leaf_name(bad)

    def test_local_path_stays_under_allowed_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(safe_local_path(str(root / "refs"), roots=(root,)), root / "refs")
            with self.assertRaises(ValueError):
                safe_local_path("/etc", roots=(root,))

    def test_ffmpeg_builder_is_bounded_and_seek_precedes_input(self):
        argv = ffmpeg_argv(*ffmpeg_input("clip.mp4", seek=3.5), "-frames:v", "1", "out.jpg")
        self.assertEqual(argv[0], "ffmpeg")
        self.assertIn("-nostdin", argv)
        self.assertEqual(argv[argv.index("-threads") + 1], "2")
        self.assertLess(argv.index("-ss"), argv.index("-i"))

if __name__ == "__main__":
    unittest.main()
