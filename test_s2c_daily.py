import unittest
from unittest.mock import patch
import base64
import json
import os
from urllib.error import HTTPError
import s2c_daily as m

class DailyTests(unittest.TestCase):
    def test_title(self):
        self.assertEqual(m.split_post('**НОВЫЙ ЗАГОЛОВОК**\n\nТело **текста**.'), ('НОВЫЙ ЗАГОЛОВОК', 'Тело текста.'))

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

    def test_skip_replaced_and_not_counted_as_sent(self):
        items=[{'id':str(i),'title':'AI','text':'facts','url':'https://example.org','score':5-i} for i in range(3)]
        state={'sent_ids':[], 'collected':{}}
        delivered=[]
        def add(url,secret,draft):
            delivered.append(draft)
            return True,{'ok':True}
        with patch.dict(os.environ, {'S2C_WORKER_SECRET':'test','S2C_MAX_DRAFTS':'1'}), patch.object(m,'SOURCES',{'grok':(lambda *args:items,True)}), patch.object(m,'load_state',return_value=state), patch.object(m,'qwen_generate',side_effect=['SKIP','**TITLE**\n\nBody']), patch.object(m,'og_image',return_value=None), patch.object(m,'worker_add',side_effect=add), patch.object(m,'save_state_and_push'), patch.object(m.sys,'argv',['test']):
            self.assertEqual(m.main(),0)
        self.assertEqual(len(delivered),1)
        self.assertEqual(delivered[0]['id'],'1')
        self.assertEqual(delivered[0]['title'],'TITLE')
        self.assertEqual(delivered[0]['text'],'Body')
        self.assertEqual(state['sent_ids'],['1'])
        self.assertIn('0',state['deferred_until'])

if __name__=='__main__': unittest.main()

