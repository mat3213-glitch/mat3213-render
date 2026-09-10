"""One real news -> semantic brief -> ImageFree -> pixel QA. Never calls Worker."""
import json
from pathlib import Path

import s2c_daily as daily
import s2c_images as images


def main():
    output = Path('s2c_image_smoke_output')
    output.mkdir(exist_ok=True)
    candidates = daily.arxiv_fetch(5, {})
    if not candidates:
        raise RuntimeError('No live arXiv candidates')
    candidate = candidates[0]
    (output / 'news.json').write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Live news:', candidate['title'], candidate['url'], flush=True)
    result = daily.imagefree_image_bytes(candidate)
    brief = images.load_brief(candidate)
    (output / 'brief.json').write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding='utf-8')
    if not result:
        raise RuntimeError('No accepted image; inspect logs for planning, provider or QA rejection')
    verdict = images.check_image(result)
    (output / 'qa.json').write_text(json.dumps(verdict, indent=2), encoding='utf-8')
    if not verdict.get('ok'):
        raise RuntimeError('Recheck failed')
    (output / 'image.png').write_bytes(result)
    print(json.dumps({'status':'PASS', 'bytes':len(result), 'qa':verdict}, ensure_ascii=False))


if __name__ == '__main__':
    main()
