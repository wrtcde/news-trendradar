#!/usr/bin/env python3
"""把仓库里的 news-archive.worker.mjs 部署到 Cloudflare Workers。

由 deploy-archive-worker.yml 在「该文件被改动」时调用，所以这里不再做
内容比对（Cloudflare 的 /content 端点对本 Token 返回 405，也读不到线上源码）。

为什么不用 wrangler deploy：这个 Worker 挂着自定义域名 news.ttwrt.top，
域名不是在 wrangler 配置里声明的，用 wrangler 有把路由收敛掉的风险。
这里走与手工部署完全一样的 API PUT，并且**先把线上现有绑定读出来原样回传**
（R2 绑定 ARCHIVE 一旦被覆盖，整站立刻取不到归档）。

安全网：
  1. 读不到 ARCHIVE 绑定 → 直接中止，不部署；
  2. 部署后冒烟检查；失败就用 git 里的上一版重新 PUT 回去（和部署同一条
     已验证路径，不用 Cloudflare 那个对本 Token 返回 405 的 rollback 端点）。

需要环境变量：CLOUDFLARE_API_TOKEN、CLOUDFLARE_ACCOUNT_ID。
"""
import hashlib
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REL = 'news-archive.worker.mjs'
SRC = os.environ.get('ARCHIVE_WORKER_SRC') or os.path.join(ROOT, REL)
SCRIPT = os.environ.get('ARCHIVE_WORKER_NAME', 'news-archive')
SMOKE = os.environ.get('ARCHIVE_SMOKE_URL', 'https://news.ttwrt.top/')
API = 'https://api.cloudflare.com/client/v4'

TOKEN = (os.environ.get('CLOUDFLARE_API_TOKEN') or '').strip()
ACCT = (os.environ.get('CLOUDFLARE_ACCOUNT_ID') or '').strip()
if not TOKEN or not ACCT:
    sys.exit('缺少 CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID')


def js(path, method='GET', body=None, raw=None, ctype='application/json'):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={'Authorization': 'Bearer ' + TOKEN,
                                          'Content-Type': ctype})
    try:
        r = urllib.request.urlopen(req, timeout=180)
        status, payload = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, payload = e.code, e.read()
    except Exception as e:
        print('  请求异常：', repr(e)[:160])
        return 0, {'success': False}
    try:
        return status, json.loads(payload.decode('utf-8', 'ignore'))
    except Exception:
        return status, {'raw': payload[:200].decode('utf-8', 'ignore')}


def norm(b):
    b = b.replace(b'\r\n', b'\n')
    return b if b.endswith(b'\n') else b + b'\n'


def read_bindings():
    st, d = js('/accounts/%s/workers/scripts/%s/settings' % (ACCT, SCRIPT))
    if st != 200 or not d.get('success'):
        sys.exit('读不到 %s 的 settings（HTTP %s），中止部署' % (SCRIPT, st))
    res = d.get('result') or {}
    keep = []
    for b in res.get('bindings') or []:
        t = b.get('type')
        if t == 'r2_bucket':
            keep.append({'type': 'r2_bucket', 'name': b['name'], 'bucket_name': b['bucket_name']})
        elif t == 'plain_text':
            keep.append({'type': 'plain_text', 'name': b['name'], 'text': b.get('text', '')})
        elif t == 'json':
            keep.append({'type': 'json', 'name': b['name'], 'json': b.get('json')})
        elif t == 'kv_namespace':
            keep.append({'type': 'kv_namespace', 'name': b['name'], 'namespace_id': b.get('namespace_id')})
        else:
            sys.exit('未处理的绑定类型 %s（%s），人工确认后再部署' % (t, b.get('name')))
    return res, keep


def put(code, meta, keep, compat, flags):
    boundary = hashlib.md5(code + str(time.time()).encode()).hexdigest()
    parts = []

    def add(name, payload, ctype, filename=None):
        disp = 'form-data; name="%s"' % name
        if filename:
            disp += '; filename="%s"' % filename
        parts.append(('--%s\r\nContent-Disposition: %s\r\nContent-Type: %s\r\n\r\n'
                      % (boundary, disp, ctype)).encode())
        parts.append(payload if isinstance(payload, bytes) else payload.encode())
        parts.append(b'\r\n')

    add('metadata', json.dumps({'main_module': 'index.mjs', 'compatibility_date': compat,
                                'compatibility_flags': flags, 'bindings': keep}),
        'application/json')
    add('index.mjs', code, 'application/javascript+module', filename='index.mjs')
    parts.append(('--%s--\r\n' % boundary).encode())
    return js('/accounts/%s/workers/scripts/%s' % (ACCT, SCRIPT), method='PUT',
              raw=b''.join(parts), ctype='multipart/form-data; boundary=%s' % boundary)


def smoke():
    """线上要还能 200 且拿到有分量的 HTML，否则视为部署坏了。"""
    url = SMOKE + ('&' if '?' in SMOKE else '?') + 'smoke=' + os.urandom(4).hex()
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'deploy-smoke'}),
                                   timeout=60)
        body = r.read()
        return r.status == 200 and len(body) > 2000, r.status, len(body)
    except Exception as e:
        return False, 0, repr(e)[:140]


def previous_version():
    """回滚素材：git 里上一版该文件。CI 的 checkout 需要 fetch-depth>=2。"""
    try:
        p = subprocess.run(['git', 'show', 'HEAD^:' + REL], cwd=ROOT, capture_output=True)
        if p.returncode == 0 and p.stdout:
            return norm(p.stdout)
        print('  git 里没有上一版（%s）→ 本次失败时无法自动回滚'
              % p.stderr.decode('utf-8', 'ignore').strip()[:80])
    except Exception as e:
        print('  取上一版异常：', repr(e)[:120])
    return None


def main():
    code = norm(io.open(SRC, 'rb').read())
    print('本地 %s：%d 字节 sha256=%s' % (REL, len(code), hashlib.sha256(code).hexdigest()[:12]))
    if b'export default {' not in code:
        sys.exit('源码里没有 export default，看起来不是 Worker 模块，中止')

    prev = previous_version()
    res, keep = read_bindings()
    if not any(b['name'] == 'ARCHIVE' and b['type'] == 'r2_bucket' for b in keep):
        sys.exit('没读到 ARCHIVE 绑定，中止——部署后整站会取不到归档')
    compat = res.get('compatibility_date') or '2025-09-01'
    flags = res.get('compatibility_flags') or []
    print('原样回传绑定：', [(b['type'], b['name']) for b in keep])
    print('compatibility_date=%s flags=%s · 回滚素材：%s'
          % (compat, flags, '已取到' if prev else '无'))

    st, d = put(code, None, keep, compat, flags)
    if not d.get('success'):
        print('❌ 部署失败 HTTP %s：%s' % (st, json.dumps(d.get('errors'), ensure_ascii=False)[:400]))
        return 3
    print('部署成功')
    time.sleep(3)

    ok, hstatus, hlen = smoke()
    print('冒烟检查 → HTTP %s · %s' % (hstatus, hlen))
    if ok:
        print('✅ 部署完成且线上可访问')
        return 0

    print('❌ 冒烟不通过')
    if not prev:
        print('   没有回滚素材，需要人工介入')
        return 4
    print('   回滚到上一版（%d 字节）' % len(prev))
    st2, d2 = put(prev, None, keep, compat, flags)
    print('   回滚 PUT HTTP %s success=%s' % (st2, d2.get('success')))
    time.sleep(3)
    ok3, s3, l3 = smoke()
    print('   回滚后冒烟：HTTP %s · %s → %s' % (s3, l3, '恢复 ✓' if ok3 else '仍不正常，人工介入'))
    return 4


if __name__ == '__main__':
    sys.exit(main())
