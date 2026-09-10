import unittest
from unittest.mock import patch
import base64
import json
import os
import tempfile
from pathlib import Path
from urllib.error import HTTPError
import s2c_daily as m

class DailyTests(unittest.TestCase):
    def setUp(self):
        m._IMAGEFREE_STOPPED = False
        m._IMAGEFREE_TASKS = 0
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = patch.object(m.s2c_images, 'CACHE_DIR', Path(self.tmp.name))
        self.cache.start()
        self.addCleanup(self.cache.stop)

    def test_title(self):
        self.assertEqual(m.split_post('**НОВЫЙ ЗАГОЛОВОК**\n\nТело **текста**.'), ('НОВЫЙ ЗАГОЛОВОК', 'Тело текста.'))

    def test_generated_source_line_removed(self):
        self.assertEqual(m.split_post('ЗАГОЛОВОК\n\nТело.\n\nИсточник: https://example.org/a'), ('ЗАГОЛОВОК', 'Тело.'))

    def test_generated_markdown_source_line_removed(self):
        self.assertEqual(m.split_post('ЗАГОЛОВОК\n\nТело.\n\nИсточник: [https://example.org/a](https://example.org/a)'), ('ЗАГОЛОВОК', 'Тело.'))

    def test_imagefree_png_bytes(self):
        candidate = {'id':'arxiv:1','title':'AI paper','text':'facts','url':'https://example.org/paper'}
        png = b'\x89PNG\r\n\x1a\n' + b'0' * 6000
        brief={'who':'Механический манипулятор','does_what':'поднимает стеклянную сферу',
               'subject':'a mechanical gripper','action':'lifting a glass sphere','setting':'a plain empty background','simplified_scene':'a mechanical gripper holding glass','reason':'Shows fragile object manipulation'}
        with patch.object(m, '_imagefree_brief', return_value=brief), patch.object(m.s2c_images, 'check_image', return_value={'ok':True,'reason':'clean'}), patch.object(m, '_imagefree_submit', return_value=('task-1', None)), patch.object(m, '_imagefree_wait', return_value=('https://cdn.example/a.png', None)), patch.object(m, '_download_png', return_value=png), patch.object(m.time, 'sleep'):
            self.assertEqual(m.imagefree_image_bytes(candidate), png)

    def test_worker_add_multipart_with_image(self):
        captured = {}
        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self): return b'{"ok": true}'
        def fake_urlopen(req, timeout):
            captured['content_type'] = req.headers.get('Content-type') or req.headers.get('Content-Type')
            captured['body'] = req.data
            return FakeResponse()
        png = b'\x89PNG\r\n\x1a\n' + b'0' * 6000
        draft = {'id':'1','title':'T','text':'B','image_url':None}
        with patch.object(m.urllib.request, 'urlopen', side_effect=fake_urlopen):
            ok, _ = m.worker_add('https://worker.example', 'secret', draft, png)
        self.assertTrue(ok)
        self.assertIn('multipart/form-data', captured['content_type'])
        self.assertIn(b'name="image"; filename="imagefree.png"', captured['body'])

    def test_official_blog_rss(self):
        feed=b'<rss><channel><item><title>New model</title><link>https://example.org/model</link><description><![CDATA[<b>Details</b>]]></description></item></channel></rss>'
        with patch.object(m, '_OFFICIAL_FEEDS', {'openai':'https://example.org/feed'}), patch.object(m, '_http_bytes', return_value=feed):
            out=m.official_blogs_fetch(5,{})
        self.assertEqual(out[0]['source'],'openai')
        self.assertEqual(out[0]['text'],'Details')

    def test_rotation(self):
        buckets = {k:[{'id': k}] for k in ['hn','lob','arxiv','grok','chatgpt']}
        first = m.fair_candidates(buckets, 0)[:3]
        second = m.fair_candidates(buckets, 3)[:3]
        self.assertEqual([k for k,c in second[:2]], ['grok','chatgpt'])
        self.assertEqual(len({c['id'] for k,c in first + second}), 5)

    def test_report_window_and_fields(self):
        calls=[]
        def fake(url, token):
            calls.append(url)
            return {'content':base64.b64encode(json.dumps({'findings':[{'title':'AI news','summary':'Useful facts','url':'https://example.org'}]}).encode()).decode()}
        with patch.dict(os.environ, {'GH_PAT':'test'}), patch.object(m, '_gh_json', side_effect=fake):
            out=m.chatgpt_fetch(12,{})
        self.assertEqual(len(calls),14)
        self.assertEqual(out[0]['text'],'Useful facts')

    def test_auth_error_visible(self):
        with patch.dict(os.environ, {'GH_PAT':'test'}), patch.object(m, '_gh_json', side_effect=HTTPError('test',403,'forbidden',{},None)):
            with self.assertRaises(HTTPError): m.grok_fetch(12,{})

    def test_youtube_feed(self):
        feed = b'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015"><entry><yt:videoId>abc123</yt:videoId><title>New AI model</title></entry></feed>'''
        with patch.object(m, '_youtube_feed_bytes', return_value=feed):
            out=m.youtube_fetch(4,{})
        self.assertEqual(out[0]['id'],'youtube:abc123')
        self.assertEqual(out[0]['image_url'],'https://i.ytimg.com/vi/abc123/hqdefault.jpg')

    def test_youtube_page_fallback(self):
        with patch.object(m, '_youtube_feed_bytes', side_effect=HTTPError('feed',404,'missing',{},None)), patch.object(m, '_youtube_page_videos', return_value=[('abcdefghijk','New robot')]), patch.object(m, '_invidious_latest', return_value=[]):
            out=m.youtube_fetch(4,{})
        self.assertEqual(out[0]['id'],'youtube:abcdefghijk')

    def test_youtube_ytdlp_fallback(self):
        with patch.object(m, '_youtube_feed_bytes', side_effect=HTTPError('feed',404,'missing',{},None)), patch.object(m, '_youtube_page_videos', return_value=[]), patch.object(m, '_invidious_latest', return_value=[]), patch.object(m, '_yt_dlp_latest', return_value=[('zyxwvutsrqp','AI release')]):
            out=m.youtube_fetch(4,{})
        self.assertEqual(out[0]['id'],'youtube:zyxwvutsrqp')

    def test_skip_replaced_and_not_counted_as_sent(self):
        items=[{'id':str(i),'title':'AI','text':'facts','url':'https://example.org','score':5-i} for i in range(3)]
        state={'sent_ids':[], 'collected':{}}
        delivered=[]
        def add(url,secret,draft,image=None):
            delivered.append(draft)
            return True,{'ok':True}
        with patch.dict(os.environ, {'S2C_WORKER_SECRET':'test','S2C_MAX_DRAFTS':'1'}), patch.object(m,'SOURCES',{'grok':(lambda *args:items,True)}), patch.object(m,'load_state',return_value=state), patch.object(m,'qwen_generate',side_effect=['SKIP','**TITLE**\n\nBody']), patch.object(m,'og_image',return_value=None), patch.object(m,'imagefree_image_bytes',return_value=b'validated image'), patch.object(m,'worker_add',side_effect=add), patch.object(m,'save_state_and_push'), patch.object(m.sys,'argv',['test']):
            self.assertEqual(m.main(),0)
        self.assertEqual(len(delivered),1)
        self.assertEqual(delivered[0]['id'],'1')
        self.assertEqual(delivered[0]['title'],'TITLE')
        self.assertEqual(delivered[0]['text'],'Body')
        self.assertEqual(state['sent_ids'],['1'])
        self.assertIn('0',state['deferred_until'])

    def test_one_generation_error_does_not_fail_full_delivery(self):
        items=[{'id':str(i),'title':'AI','text':'facts','url':'https://example.org','score':5-i} for i in range(2)]
        with patch.dict(os.environ, {'S2C_WORKER_SECRET':'test','S2C_MAX_DRAFTS':'1'}), patch.object(m,'SOURCES',{'grok':(lambda *args:items,True)}), patch.object(m,'load_state',return_value={'sent_ids':[],'collected':{}}), patch.object(m,'qwen_generate',side_effect=[RuntimeError('temporary'),'TITLE\n\nBody']), patch.object(m,'candidate_image',return_value=None), patch.object(m,'imagefree_image_bytes',return_value=b'validated image'), patch.object(m,'worker_add',return_value=(True,{'ok':True})), patch.object(m,'save_state_and_push'), patch.object(m.sys,'argv',['test']):
            self.assertEqual(m.main(),0)

if __name__=='__main__': unittest.main()
