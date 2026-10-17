#!/usr/bin/env python3
"""AI 上游探针：打一枪，看是不是真的能拿到非空回答，并量延迟。

两种用法：
    走兜底代理   python3 scripts/ai_probe.py --base http://127.0.0.1:8788/v1 --key local
    直连某一家   AI_BASE=... AI_KEY=... AI_MODEL_NAME=... python3 scripts/ai_probe.py --direct

退出码 0=正常；1=请求失败；2=HTTP 200 但 content 为空（正是 litellm 看不出来的那种故障）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

SYSTEM = ('你是学术出版与科技领域的专业译者。把用户给出的英文标题逐条翻译成简体中文，'
          '保留期刊名、机构名、产品名的通行写法，不要添加解释、引号或序号。')
USER = ('只输出 JSON 数组，元素是与输入同序的字符串，数量必须等于 2 条：\n'
        '["Retraction Watch story exposes paper mill after author-for-sale probe",'
        ' "Meta may use sweeping new commercial TOS to avoid millions in AI licensing fees"]')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='')
    ap.add_argument('--key', default='')
    ap.add_argument('--model', default='via-failover')
    ap.add_argument('--direct', action='store_true', help='不走代理，直连 env 里的 AI_BASE/AI_KEY/AI_MODEL_NAME')
    ap.add_argument('--timeout', type=int, default=90)
    a = ap.parse_args()

    if a.direct:
        base, key, model = os.environ['AI_BASE'], os.environ['AI_KEY'], os.environ['AI_MODEL_NAME']
    else:
        base, key, model = a.base, a.key, a.model
    url = base.rstrip('/') + '/chat/completions'
    payload = {'model': model, 'temperature': 0.2,
               'messages': [{'role': 'system', 'content': SYSTEM},
                            {'role': 'user', 'content': USER}]}
    t0 = time.time()
    try:
        r = urllib.request.urlopen(
            urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), method='POST',
                                   headers={'Authorization': 'Bearer ' + key,
                                            'Content-Type': 'application/json'}),
            timeout=a.timeout)
        data = json.loads(r.read().decode('utf-8', 'ignore'))
    except Exception as exc:
        print('❌ %.1fs  %s  %r' % (time.time() - t0, url, exc))
        return 1

    el = time.time() - t0
    choices = data.get('choices') or []
    content = ((choices[0] if choices else {}).get('message') or {}).get('content')
    if isinstance(content, list):
        content = '\n'.join(c.get('text', str(c)) if isinstance(c, dict) else str(c)
                            for c in content)
    if not (content or '').strip():
        print('❌ %.1fs  HTTP 200 但 content 为空（model 回显 %s）' % (el, data.get('model')))
        return 2
    print('✅ %.1fs  model=%s  %s' % (el, data.get('model'), ' '.join(content.split())[:100]))
    return 0


if __name__ == '__main__':
    sys.exit(main())
