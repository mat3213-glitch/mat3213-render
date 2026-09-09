"""Regression tests for false timeout/success in browser GLM smoke (no browser)."""
import unittest
from unittest.mock import patch

import glm_chat as glm


class Locator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    @property
    def first(self):
        return self

    @property
    def last(self):
        return self

    async def count(self):
        if "chat-assistant" in self.selector:
            return int(bool(self.page.frame[0]))
        if "Stop" in self.selector or "[class*='stop']" in self.selector:
            return int(self.page.frame[1])
        return 0

    async def is_visible(self):
        return bool(await self.count())

    async def inner_text(self):
        return self.page.frame[0]


class Page:
    def __init__(self, frames):
        self.frames = frames
        self.tick = 0
        self.now = 0.0

    @property
    def frame(self):
        return self.frames[min(max(self.tick - 1, 0), len(self.frames) - 1)]

    async def wait_for_timeout(self, ms):
        self.now += ms / 1000
        self.tick += 1

    def locator(self, selector):
        return Locator(self, selector)


class AnswerTests(unittest.IsolatedAsyncioTestCase):
    async def read(self, frames, timeout=15):
        page = Page(frames)
        diagnostics = {}
        with patch.object(glm.time, "monotonic", lambda: page.now):
            text = await glm._read_answer(page, timeout, diagnostics)
        return text, diagnostics

    async def test_short_answers_complete_without_timeout(self):
        for answer in ("Готов", "Да", "4"):
            with self.subTest(answer=answer):
                text, d = await self.read([(answer, False)])
                self.assertEqual(text, answer)
                self.assertEqual(d["status"], "completed")
                self.assertEqual(d["first_text_s"], 1.0)
                self.assertLess(d["completion_s"], 15)

    async def test_stable_text_during_generation_is_not_complete(self):
        text, d = await self.read([("Partial answer", True)], timeout=5)
        self.assertEqual(text, "")
        self.assertEqual(d["status"], "timeout")
        self.assertIsNone(d["completion_s"])

    async def test_waits_for_stop_and_final_text(self):
        text, d = await self.read([("Thinking", True)] * 4 + [("Готов", False)] * 3)
        self.assertEqual(text, "Готов")
        self.assertGreaterEqual(d["completion_s"], 7)

    async def test_unstable_partial_is_not_success_on_timeout(self):
        text, d = await self.read([("abc" * n, False) for n in range(1, 8)], timeout=5)
        self.assertEqual(text, "")
        self.assertEqual(d["status"], "timeout")

    async def test_empty_timeout(self):
        text, d = await self.read([("", False)], timeout=5)
        self.assertEqual(text, "")
        self.assertIsNone(d["first_text_s"])
        self.assertEqual(d["status"], "timeout")

    async def test_refusal_is_not_short_answer(self):
        text, d = await self.read([("Model at capacity", False)])
        self.assertEqual(text, "")
        self.assertEqual(d["status"], "refused")

    def test_model_names_are_exact(self):
        self.assertEqual(glm._model_label("GLM-5.3\nSelect a model"), "GLM-5.3")
        self.assertEqual(glm._model_label("GLM-5.3-Flash"), "GLM-5.3-Flash")
        self.assertNotEqual(glm._model_label("GLM-5.3-Flash"), "GLM-5.3")
        self.assertEqual(glm._model_label("GLM-5.3 GLM-5.3-Flash"), "")


if __name__ == "__main__":
    unittest.main()
