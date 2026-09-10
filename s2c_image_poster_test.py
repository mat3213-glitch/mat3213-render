"""Manual ImageFree experiment only: WHO -> DOES WHAT, three flat colors.

The poster style is now the production policy (s2c-poster-v1): brief, prompt
and QA reuse s2c_images/s2c_daily exactly like the scheduled cycle. This script
differs only in polling one task up to 540 seconds and saving task.json before
waiting. No Worker or Telegram requests, no production cache writes.
"""
import json
import os
import time
import random
from pathlib import Path

import s2c_daily as daily
import s2c_images as images


def wait_for_test_image(task, timeout=540):
    """Keep polling this task only; ImageFree pool jobs also allow longer waits."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = daily._imagefree_poll(task)
        if isinstance(status, dict):
            if status.get('status') == 'completed':
                url = next((status[k] for k in ('image','image_url','imageUrl','url') if status.get(k)), None)
                return (str(url), None) if url else (None, 'missing_image')
            if status.get('status') == 'failed' or status.get('errorCode'):
                return None, str(status.get('errorCode') or status.get('error') or 'failed')
        time.sleep(min(15, max(0, deadline - time.monotonic())))
    return None, 'timeout'


def main():
    output = Path('s2c_image_smoke_output')
    output.mkdir(exist_ok=True)
    candidates = daily.arxiv_fetch(5, {})
    if not candidates:
        raise RuntimeError('No live news')
    candidate = candidates[0]
    (output / 'news.json').write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Live news:', candidate['title'], candidate['url'], flush=True)
    raw = daily.qwen_generate(images.brief_prompt(candidate),
                               os.getenv('QWEN_MODEL', 'Qwen3-Coder'))
    brief = images.parse_brief(raw, candidate)
    (output / 'brief.json').write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding='utf-8')
    print('WHO:', brief['who'], 'DOES WHAT:', brief['does_what'], flush=True)
    for attempt in range(2):
        prompt = images.render_prompt(brief, attempt)
        task, error = daily._imagefree_submit(prompt, '4:3')
        if error or not task:
            raise RuntimeError('ImageFree stopped: ' + str(error))
        (output / 'task.json').write_text(json.dumps({'task_id':task,'attempt':attempt+1,
            'prompt':prompt,'provider':'imagefree.net'},indent=2),encoding='utf-8')
        print('[s2c] ImageFree submitted task:', task, flush=True)
        url, error = wait_for_test_image(task)
        if error or not url:
            raise RuntimeError('ImageFree stopped: ' + str(error))
        image = daily._download_png(url)
        if not image:
            raise RuntimeError('Invalid PNG')
        time.sleep(random.uniform(6, 15))
        verdict = images.check_image(image)
        attempts = output / 'attempts'
        attempts.mkdir(exist_ok=True)
        (attempts / f'attempt-{attempt+1}.png').write_bytes(image)
        (attempts / f'attempt-{attempt+1}.json').write_text(json.dumps(
            {'prompt':prompt, 'qa':verdict, 'style':'who-does-what-flat-three-colors'},
            ensure_ascii=False, indent=2), encoding='utf-8')
        print('[s2c] poster image QA:', verdict, flush=True)
        if verdict.get('ok'):
            (output / 'image.png').write_bytes(image)
            (output / 'qa.json').write_text(json.dumps(verdict,indent=2), encoding='utf-8')
            print(json.dumps({'status':'PASS','bytes':len(image),'style':'who-does-what-flat-three-colors'}))
            return
        if verdict['reason'].startswith('qa_'):
            break
    raise RuntimeError('No accepted poster; inspect saved attempts')


if __name__ == '__main__':
    main()
