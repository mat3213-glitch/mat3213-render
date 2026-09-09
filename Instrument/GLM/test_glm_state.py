import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from glm_state import ORIGIN, capture_state, split_state, restore_session_storage, save_private


class StateTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_and_restore_preserves_extended_state(self):
        native = {'cookies': [], 'origins': [{'origin': ORIGIN, 'indexedDB': [{'name': 'test'}]}]}
        context = AsyncMock()
        context.storage_state.return_value = native
        page = AsyncMock()
        page.evaluate.return_value = {'test': "quote'\\\n"}
        state = await capture_state(context, page)
        context.storage_state.assert_awaited_once_with(indexed_db=True)
        clean, sessions = split_state(state)
        self.assertNotIn('glm_session_storage', clean)
        self.assertEqual(clean['origins'][0]['indexedDB'], [{'name': 'test'}])
        self.assertEqual(sessions[ORIGIN]['test'], "quote'\\\n")
        sessions['https://unrelated.example'] = {'secret': 'DO_NOT_INJECT'}
        await restore_session_storage(context, sessions)
        script = context.add_init_script.call_args.kwargs['script']
        self.assertNotIn('DO_NOT_INJECT', script)
        self.assertIn('location.origin', script)

    def test_legacy_and_atomic_private_save(self):
        old = {'cookies': [], 'origins': []}
        self.assertEqual(split_state(old), (old, {ORIGIN: {}}))
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'state.json'
            p.write_text('old')
            p.chmod(0o644)
            save_private(p, old)
            self.assertEqual(json.loads(p.read_text()), old)
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(tmp).iterdir()), [p])
