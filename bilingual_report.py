#!/usr/bin/env python3
"""把 TrendRadar 生成的报告 HTML 改成中英双语标题。

用法:
    python bilingual_report.py 文件1.html [文件2.html ...]

读取环境变量（与 TrendRadar 的 AI 配置同名）:
    AI_API_KEY / AI_MODEL / AI_API_BASE

设计要点:
- 只处理 RSS 条目标题（<div class="rss-title"><a ... class="rss-link">标题</a>）
- 所有文件里的标题先去重，再一次性批量翻译，避免重复调用
- 翻译失败或数量不匹配就原样返回，绝不因为翻译环节弄坏报告
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request

BATCH_SIZE = 40
TIMEOUT = 180

TITLE_RE = re.compile(
    r'(<div class="rss-title">)\s*'
    r'(<a\b[^>]*class="rss-link"[^>]*>)(.*?)(</a>)',
    re.S,
)

STYLE = """<style id="bilingual-style">
.rss-title .bi-zh{display:block;font-size:1.02em;line-height:1.5}
.rss-title .bi-en{display:block;margin-top:.18rem;font-size:.86em;line-height:1.45;opacity:.62}
.rss-title .rss-link{text-decoration:none}
</style>
"""


def collect(html_texts: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for text in html_texts:
        for m in TITLE_RE.finditer(text):
            title = re.sub(r'<[^>]+>', '', m.group(3)).strip()
            if title:
                seen.setdefault(title, None)
    return list(seen.keys())


def translate(titles: list[str]) -> dict[str, str]:
    key = os.environ.get('AI_API_KEY', '').strip()
    base = os.environ.get('AI_API_BASE', '').strip().rstrip('/') or 'https://api.openai.com/v1'
    model = os.environ.get('AI_MODEL', '').strip() or 'openai/gpt-4o-mini'
    # AI_MODEL 是 LiteLLM 风格的路由名；直连 OpenAI 兼容接口时只认真实的模型 id
    model_id = model[len('openai/'):] if model.startswith('openai/') else model
    if not key:
        print('[bilingual] 未配置 AI_API_KEY，跳过')
        return {}
    print('[bilingual] 模型 %s，接口 %s' % (model_id, base))

    out: dict[str, str] = {}
    for start in range(0, len(titles), BATCH_SIZE):
        batch = titles[start:start + BATCH_SIZE]
        payload = {
            'model': model_id,
            'temperature': 0.2,
            'messages': [
                {
                    'role': 'system',
                    'content': '你是学术出版与科技领域的专业译者。把用户给出的英文标题逐条翻译成简体中文，'
                               '保留期刊名、机构名、产品名的通行写法，不要添加解释、引号或序号。',
                },
                {
                    'role': 'user',
                    'content': '只输出 JSON 数组，元素是与输入同序的字符串，数量必须等于 %d 条：\n%s'
                               % (len(batch), json.dumps(batch, ensure_ascii=False)),
                },
            ],
        }
        req = urllib.request.Request(
            base + '/chat/completions',
            data=json.dumps(payload).encode('utf-8'),
            headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'},
            method='POST',
        )
        try:
            resp = json.loads(urllib.request.urlopen(req, timeout=TIMEOUT).read().decode('utf-8', 'ignore'))
            content = (resp.get('choices') or [{}])[0].get('message', {}).get('content') or ''
            arr = json.loads(re.search(r'\[.*\]', content, re.S).group(0))
            if isinstance(arr, list) and len(arr) == len(batch):
                out.update({t: str(a).strip() for t, a in zip(batch, arr) if str(a).strip()})
                print('[bilingual] 翻译 %d 条成功' % len(batch))
            else:
                print('[bilingual] 第 %d 批返回数量不符，跳过该批' % (start // BATCH_SIZE + 1))
        except Exception as exc:  # 翻译环节绝不允许弄坏报告
            print('[bilingual] 第 %d 批失败：%r' % (start // BATCH_SIZE + 1, exc))
    return out


def apply_map(html: str, mapping: dict[str, str]) -> str:
    def repl(m: re.Match) -> str:
        head, a_open, inner, a_close = m.group(1), m.group(2), m.group(3), m.group(4)
        plain = re.sub(r'<[^>]+>', '', inner).strip()
        zh = mapping.get(plain)
        if not zh or zh == plain:
            return m.group(0)
        return '%s\n%s<span class="bi-zh">%s</span><span class="bi-en">%s</span>%s' % (
            head, a_open, zh, inner, a_close)

    out = TITLE_RE.sub(repl, html)
    if out != html and 'bilingual-style' not in out:
        out = out.replace('</head>', STYLE + '</head>', 1)
    return out


def main(paths: list[str]) -> int:
    texts, loaded = [], []
    for p in paths:
        try:
            with open(p, encoding='utf-8') as fh:
                texts.append(fh.read())
            loaded.append(p)
        except OSError as exc:
            print('[bilingual] 读取失败 %s: %r' % (p, exc))
    if not loaded:
        return 0

    titles = collect(texts)
    print('[bilingual] 待翻译标题 %d 条，涉及 %d 个文件' % (len(titles), len(loaded)))
    if not titles:
        return 0

    mapping = translate(titles)
    if not mapping:
        return 0

    for p, text in zip(loaded, texts):
        new = apply_map(text, mapping)
        if new != text:
            with open(p, 'w', encoding='utf-8') as fh:
                fh.write(new)
            print('[bilingual] 已改写 %s' % p)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
