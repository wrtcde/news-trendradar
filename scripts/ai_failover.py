#!/usr/bin/env python3
"""AI 主备切换转发层（OpenAI 兼容，纯标准库）

GitHub Actions 里在跑爬虫之前把它后台起来，然后把 AI_API_BASE 指到本进程：
TrendRadar 的 AI 总结、翻译，以及 render_brief.py 的标题翻译，全部走同一个兜底逻辑。

为什么要这么一层：litellm 的 fallback_models 只在「请求报错」时切换，
而实测遇到过的真实故障是上游返回 HTTP 200 但 choices[0].message.content 为空
（推理模型把预算全花在 reasoning 上）。这种响应在客户端看是成功，
只有在这一层才能判定为失败并换上游。

配置（环境变量）：
    AI_UPSTREAMS       JSON 数组，按顺序即优先级：
                       [{"label":"主","base":"https://.../v1","key":"sk-...",
                         "model":"DeepSeek-V4-Flash","timeout":45}, ...]
    没给 AI_UPSTREAMS 时，退而从这几组拼出来：
      AI_API_BASE / AI_API_KEY / AI_MODEL                     主
      AI_FALLBACK_API_BASE / _KEY / _MODEL                    备 1
      AI_FALLBACK2_API_BASE / _KEY / _MODEL                   备 2
    AI_FAILOVER_PORT   监听端口，默认 8788
    AI_FAILOVER_TIMEOUT 单个上游的超时秒数，默认取上游自己的值或 45
    AI_FAILOVER_TRIPS  同一上游连续失败几次后本次运行不再尝试，默认 2
"""
from __future__ import annotations

import http.server
import json
import os
import re
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.request

PORT = int(os.environ.get('AI_FAILOVER_PORT', '8788'))
DEFAULT_TIMEOUT = int(os.environ.get('AI_FAILOVER_TIMEOUT', '45'))
TRIPS = int(os.environ.get('AI_FAILOVER_TRIPS', '2'))


def log(msg: str) -> None:
    print('[failover] %s %s' % (time.strftime('%H:%M:%S'), msg), flush=True)


def build_upstreams():
    raw = (os.environ.get('AI_UPSTREAMS') or '').strip()
    if raw:
        try:
            rows = json.loads(raw)
            if isinstance(rows, list) and rows:
                return [norm(r) for r in rows if r.get('base')]
        except Exception as exc:
            log('AI_UPSTREAMS 不是合法 JSON（%r），改用散装变量' % exc)

    rows = []
    for prefix, label in (('AI_', '主'), ('AI_FALLBACK_', '备1'), ('AI_FALLBACK2_', '备2')):
        base = (os.environ.get(prefix + 'API_BASE') or '').strip()
        key = (os.environ.get(prefix + 'API_KEY') or '').strip()
        model = (os.environ.get(prefix + 'MODEL') or '').strip()
        if base and key and model:
            rows.append(norm({'label': label, 'base': base, 'key': key, 'model': model}))
    return rows


def norm(r: dict) -> dict:
    model = re.sub(r'^(openai|azure)/', '', str(r.get('model', '')).strip())
    return {'label': str(r.get('label') or r.get('model')), 'base': r['base'].rstrip('/'),
            'key': r['key'], 'model': model,
            'timeout': int(r.get('timeout') or DEFAULT_TIMEOUT), 'fails': 0, 'dead': False}


UPSTREAMS = build_upstreams()
LOCK = threading.Lock()


def usable():
    with LOCK:
        live = [u for u in UPSTREAMS if not u['dead']]
        if not live:
            log('所有上游都已熔断，临时全部恢复一轮')
            for u in UPSTREAMS:
                u['dead'] = False
                u['fails'] = 0
            live = list(UPSTREAMS)
        return live


def upstream_url(up, path):
    """客户端打的是 /v1/chat/completions；base 若已含 /v1 就别重复拼。"""
    if up['base'].endswith('/v1'):
        return up['base'] + re.sub(r'^/v1(?=/|$)', '', path)
    return up['base'] + path


def call(up, body, path, want_stream):
    req = urllib.request.Request(upstream_url(up, path), data=json.dumps(body).encode('utf-8'),
                                 method='POST',
                                 headers={'Authorization': 'Bearer ' + up['key'],
                                          'Content-Type': 'application/json'})
    t0 = time.time()
    try:
        r = urllib.request.urlopen(req, timeout=up['timeout'])
        raw = r.read()
    except urllib.error.HTTPError as exc:
        return False, 'HTTP %s %s' % (exc.code, exc.read()[:160].decode('utf-8', 'ignore')), None
    except Exception as exc:
        return False, repr(exc)[:160], None

    if want_stream:
        return True, None, raw

    try:
        data = json.loads(raw.decode('utf-8', 'ignore'))
    except Exception as exc:
        return False, '响应不是 JSON：%r' % exc, None
    choices = data.get('choices') or []
    content = ((choices[0] if choices else {}).get('message') or {}).get('content')
    if isinstance(content, list):                      # 内容块形态，拼成纯文本
        content = '\n'.join(c.get('text', str(c)) if isinstance(c, dict) else str(c) for c in content)
    if not (content or '').strip():
        return False, '200 但 content 为空（疑似 reasoning 占满）', None
    return True, None, raw


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _reply(self, code, payload, ctype='application/json'):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path.rstrip('/').endswith('/models') or self.path == '/healthz':
            ok = [u['label'] for u in UPSTREAMS if not u['dead']]
            self._reply(200, json.dumps({'object': 'list', 'data': [], 'live': ok}).encode())
        else:
            self._reply(404, b'{"error":"not found"}')

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(n).decode('utf-8'))
        except Exception as exc:
            self._reply(400, json.dumps({'error': 'bad request: %r' % exc}).encode())
            return

        path = self.path
        want_stream = bool(body.get('stream'))
        errs = []
        for up in usable():
            body['model'] = up['model']
            body.pop('stream', None) if want_stream else None
            ok, err, raw = call(up, body, path, want_stream)
            if ok:
                with LOCK:
                    up['fails'] = 0
                if errs:
                    log('%s 接管成功（前面 %d 个上游失败）' % (up['label'], len(errs)))
                self._reply(200, raw, 'text/event-stream' if want_stream else 'application/json')
                return
            errs.append('%s: %s' % (up['label'], err))
            with LOCK:
                up['fails'] += 1
                if up['fails'] >= TRIPS:
                    up['dead'] = True
                    log('%s 连续失败 %d 次，本次运行内熔断' % (up['label'], up['fails']))
            log('%s 失败：%s' % (up['label'], err))

        self._reply(502, json.dumps({'error': '所有上游都失败', 'detail': errs},
                                    ensure_ascii=False).encode())


class Server(socketserver.ThreadingTCPServer):
    # 默认 False：前一个实例被杀后，残留的 TIME_WAIT 连接会让重新 bind 报 Address in use
    allow_reuse_address = True
    daemon_threads = True


def main():
    if not UPSTREAMS:
        log('没有可用上游（AI_UPSTREAMS / AI_API_BASE 都没配），退出')
        return 1
    log('监听 127.0.0.1:%d，上游顺序：%s' % (PORT, ' → '.join(
        '%s(%s)' % (u['label'], u['model']) for u in UPSTREAMS)))
    with Server(('127.0.0.1', PORT), Handler) as httpd:
        httpd.serve_forever()
    return 0


if __name__ == '__main__':
    if '--count-upstreams' in sys.argv:
        print(len(UPSTREAMS))
        sys.exit(0)
    sys.exit(main())
