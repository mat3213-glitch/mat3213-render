import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import s2c_daily as daily
import s2c_images as images

NEWS = {'id':'arxiv:example', 'title':'New research finds missing specification details',
        'text':'Ambiguous specifications leave implementation choices undefined.',
        'url':'https://example.org/paper', 'score':10}
BRIEF = {'subject':'a structure of translucent blocks',
         'action':'several missing joints leave gaps between interlocking pieces',
         'setting':'a plain matte tabletop with soft side lighting',
         'simplified_scene':'two translucent blocks separated by a missing connector',
         'reason':'Missing joints represent unspecified implementation choices.'}


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        p = patch.object(images, 'CACHE_DIR', Path(self.temp.name))
        p.start()
        self.addCleanup(p.stop)
        daily._IMAGEFREE_STOPPED = False
        daily._IMAGEFREE_TASKS = 0

    def test_brief_retains_metaphor_not_headline(self):
        brief = images.parse_brief(json.dumps(BRIEF), NEWS)
        prompt = images.render_prompt(brief)
        self.assertIn('missing joints', prompt)
        self.assertNotIn(NEWS['title'], prompt)
        self.assertNotIn(NEWS['url'], prompt)
        self.assertIn('No faces', prompt)
        self.assertIn('No text', prompt)

    def test_unsafe_or_invalid_brief_is_rejected(self):
        for val in ('a laptop screen showing a report', 'a portrait of a scientist',
                    'a sculpture with human eyes', 'blocks branded with a logo',
                    'a robot carrying a poster', 'GPT 99 release', 'картинка'):
            with self.subTest(val=val), self.assertRaises(ValueError):
                images.parse_brief(json.dumps(dict(BRIEF, subject=val)), NEWS)
        with self.assertRaises(ValueError):
            images.parse_brief('{}', NEWS)

    def test_cached_brief_prevents_second_text_request(self):
        with patch.object(daily, 'qwen_generate', return_value=json.dumps(BRIEF)) as model:
            self.assertEqual(daily._imagefree_brief(NEWS), BRIEF)
            self.assertEqual(daily._imagefree_brief(NEWS), BRIEF)
        model.assert_called_once()

    def test_changed_news_invalidates_cache(self):
        images.save_brief(NEWS, BRIEF)
        self.assertIsNone(images.load_brief(dict(NEWS, text='A different discovery')))

    def test_rejected_image_cannot_enter_cache(self):
        with self.assertRaises(ValueError):
            images.save_image(NEWS, b'bad', 'p', '4:3', {'ok':False})

    def _generate(self, verdicts):
        from contextlib import ExitStack
        with ExitStack() as stack:
            stack.enter_context(patch.object(daily, '_imagefree_brief', return_value=BRIEF))
            submit = stack.enter_context(patch.object(daily, '_imagefree_submit', return_value=('id', None)))
            stack.enter_context(patch.object(daily, '_imagefree_wait', return_value=('https://cdn.example/a.png', None)))
            stack.enter_context(patch.object(daily, '_download_png', return_value=b'png'))
            stack.enter_context(patch.object(images, 'check_image', side_effect=verdicts))
            stack.enter_context(patch.object(daily.time, 'sleep'))
            result = daily.imagefree_image_bytes(NEWS)
            return result, submit.call_args_list

    def test_text_then_clean_retries_with_simpler_scene(self):
        result, calls = self._generate([{'ok':False, 'reason':'text_region'}, {'ok':True, 'reason':'clean'}])
        self.assertEqual(result, b'png')
        self.assertEqual(len(calls), 2)
        self.assertIn(BRIEF['simplified_scene'], calls[1].args[0])
        self.assertNotEqual(calls[0].args[0], calls[1].args[0])

    def test_two_rejections_return_no_image(self):
        result, calls = self._generate([{'ok':False,'reason':'text_region'}, {'ok':False,'reason':'face'}])
        self.assertIsNone(result)
        self.assertEqual(len(calls), 2)
        self.assertIsNone(images.load_image(NEWS, '4:3'))

    def test_qa_failure_stops_generation(self):
        result, calls = self._generate([{'ok':False,'reason':'qa_unavailable:ImportError'}])
        self.assertIsNone(result)
        self.assertEqual(len(calls), 1)
        self.assertTrue(daily._IMAGEFREE_STOPPED)

    def test_active_site_task_stops_all_new_submits(self):
        with patch.object(daily, '_imagefree_brief', return_value=BRIEF), patch.object(daily, '_imagefree_submit', return_value=(None,'site:FREE_TASK_IP_ACTIVE')) as submit:
            self.assertIsNone(daily.imagefree_image_bytes(NEWS))
            self.assertIsNone(daily.imagefree_image_bytes(dict(NEWS,id='other')))
        submit.assert_called_once()

    def test_good_cached_image_reused_without_service(self):
        images.save_image(NEWS, b'png', 'p', '4:3', {'ok':True})
        with patch.object(images, 'check_image', return_value={'ok':True}), patch.object(daily,'_imagefree_submit') as submit:
            self.assertEqual(daily.imagefree_image_bytes(NEWS), b'png')
        submit.assert_not_called()
        self.assertIsNone(images.load_image(NEWS,'16:9'))

    def test_corrupt_cache_is_not_reused(self):
        images.save_image(NEWS, b'png', 'p', '4:3', {'ok':True})
        (images.CACHE_DIR / images.news_key(NEWS) / 'image.png').write_bytes(b'broken')
        self.assertIsNone(images.load_image(NEWS, '4:3'))

    def test_failed_image_stays_unsent_for_later(self):
        state = {'sent_ids':[], 'collected':{}}
        with patch.dict(os.environ, {'S2C_WORKER_SECRET':'test'}), patch.object(daily.sys,'argv',['test']), patch.object(daily,'SOURCES',{'arxiv':(lambda *args:[NEWS],True)}), patch.object(daily,'load_state',return_value=state), patch.object(daily,'qwen_generate',return_value='TITLE\n\nBody'), patch.object(daily,'candidate_image',return_value=None), patch.object(daily,'imagefree_image_bytes',return_value=None), patch.object(daily,'worker_add') as add, patch.object(daily,'save_state_and_push'):
            self.assertEqual(daily.main(),1)
        add.assert_not_called()
        self.assertEqual(state['sent_ids'],[])

    def test_original_image_does_not_call_generator(self):
        with patch.dict(os.environ, {'S2C_WORKER_SECRET':'test','S2C_MAX_DRAFTS':'1'}), patch.object(daily.sys,'argv',['test']), patch.object(daily,'SOURCES',{'arxiv':(lambda *args:[NEWS],True)}), patch.object(daily,'load_state',return_value={'sent_ids':[],'collected':{}}), patch.object(daily,'qwen_generate',return_value='TITLE\n\nBody'), patch.object(daily,'candidate_image',return_value='https://example.org/photo.png'), patch.object(daily,'imagefree_image_bytes') as gen, patch.object(daily,'worker_add',return_value=(True,{'ok':True})), patch.object(daily,'save_state_and_push'):
            self.assertEqual(daily.main(),0)
        gen.assert_not_called()


if __name__ == '__main__':
    unittest.main()
