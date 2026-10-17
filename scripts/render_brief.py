#!/usr/bin/env python3
"""把 TrendRadar 生成的报告 HTML 重渲染成「热点简报」设计版（自建渲染层）。

用法:
    python scripts/render_brief.py 文件1.html [文件2.html ...]

读取环境变量:
    BRIEF_NAME   报名（默认 热点简报）
    BRIEF_TAG    报头副句
    AI_API_KEY / AI_API_BASE / AI_MODEL   与 TrendRadar 的 AI 配置同名

设计约定:
- 解析 → 翻译 → 渲染；任何一步失败就原样退出，绝不因为渲染环节弄坏报告
- 页面上出现的每个数字都从报告里算出来，模板不写死当期内容
"""
from __future__ import annotations

import base64
import datetime as dt
import io
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, 'brief_template.html')
LOGOS_JSON = os.path.join(HERE, '..', 'assets', 'logos.json')

BRIEF_NAME = (os.environ.get('BRIEF_NAME') or '').strip() or '热点简报'
BRIEF_TAG = (os.environ.get('BRIEF_TAG') or '').strip() or '每日行业热点 · 内部速览'
CRAWL_HOURS = [8, 20]          # 与 config.yaml 的 morning_evening 预设对应
BATCH_SIZE = 12
TIMEOUT = 180
RETRIES = 2

CJK = re.compile(u'[一-鿿㐀-䶿]')

# ── 解析：报告是机器生成的固定结构，按锚点切片比套 DOM 更稳 ────────────────
GROUP_RE = re.compile(r'<div class="feed-name">(.*?)</div>', re.S)
ITEM_RE = re.compile(r'<div class="rss-item">', re.S)
TIME_RE = re.compile(r'<span class="rss-time">(.*?)</span>', re.S)
AUTHOR_RE = re.compile(r'<span class="rss-author"[^>]*>(.*?)</span>', re.S)
LINK_RE = re.compile(r'<a\s+href="([^"]*)"[^>]*class="rss-link"[^>]*>(.*?)</a>', re.S)
BI_ZH_RE = re.compile(r'<span class="bi-zh">(.*?)</span>', re.S)
BI_EN_RE = re.compile(r'<span class="bi-en">(.*?)</span>', re.S)
INFO_RE = re.compile(
    r'<span class="info-label">(.*?)</span>\s*<span class="info-value">(.*?)</span>', re.S)

ENTITIES = {'&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39': "'", '&nbsp;': ' '}


def unesc(s: str) -> str:
    s = re.sub(r'&#(\d+);', lambda m: chr(int(m.group(1))), s)
    s = re.sub(r'&#([0-9a-fA-F]+);', lambda m: chr(int(m.group(1), 16)), s)
    for k, v in ENTITIES.items():
        s = s.replace(k, v)
    return s.replace('&#x27', "'").replace('&#39;', "'")


def strip_tags(s: str) -> str:
    return re.sub(r'<[^>]+>', '', s).strip()


def is_cjk_heavy(s: str) -> bool:
    c = len(CJK.findall(s))
    return c >= max(2, len(s) * 0.15)


def parse_report(html: str) -> dict:
    """→ {'info': {...}, 'items': [...]}，items 按报告里出现的顺序。"""
    info = {strip_tags(unesc(k)): strip_tags(unesc(v)) for k, v in INFO_RE.findall(html)}

    heads = [(m.start(), strip_tags(unesc(m.group(1)))) for m in GROUP_RE.finditer(html)]
    starts = [m.start() for m in ITEM_RE.finditer(html)]
    bounds = sorted(starts + [h[0] for h in heads] + [len(html)])

    items = []
    for pos in starts:
        end = min(b for b in bounds if b > pos)
        chunk = html[pos:end]
        group = next((g for p, g in reversed(heads) if p < pos), '未分组')

        tm = TIME_RE.search(chunk)
        time_str = strip_tags(unesc(tm.group(1))) if tm else ''

        authors = [strip_tags(unesc(a)) for a in AUTHOR_RE.findall(chunk)]
        new = any(a.strip().upper() == 'NEW' for a in authors)
        source = next((a for a in authors if a.strip().upper() != 'NEW'), '')
        if 'badge-new' in chunk:
            new = True

        zh = en = ''
        url = ''
        lm = LINK_RE.search(chunk)
        if lm:
            url = unesc(lm.group(1).strip())
            inner = lm.group(2)
            z = BI_ZH_RE.search(inner)
            e = BI_EN_RE.search(inner)
            if z and e:
                zh, en = strip_tags(unesc(z.group(1))), strip_tags(unesc(e.group(1)))
            else:
                title = strip_tags(unesc(inner))
                if is_cjk_heavy(title):
                    zh = title
                else:
                    en = title
        if not zh and not en:
            continue
        items.append({'group': group, 'source': source, 'time': time_str,
                      'url': url, 'zh': zh, 'en': en, 'new': new})

    # 报告分「RSS 新增更新」与「RSS 订阅更新」两段渲染，同一条会出现两次。
    # 按 URL 去重并保留首次出现（新增段在前），否则条目数会翻倍。
    uniq, seen, dropped = [], {}, 0
    for it in items:
        k = it['url'] or (it['zh'] or it['en'], it['time'], it['source'])
        if k in seen:
            dropped += 1
            continue
        seen[k] = True
        uniq.append(it)
    if dropped:
        print('[brief] 两段重复合并 %d 条 → 实际 %d 条' % (dropped, len(uniq)))
    return {'info': info, 'items': uniq}


# ── 翻译：只在缺中文时调用；失败就照常渲染，条目会标「中译待补」──────────────
def translate(titles):
    key = os.environ.get('AI_API_KEY', '').strip()
    if not key or not titles:
        return {}
    base = os.environ.get('AI_API_BASE', '').strip().rstrip('/') or 'https://api.openai.com/v1'
    model = os.environ.get('AI_MODEL', '').strip() or 'openai/gpt-4o-mini'
    # AI_MODEL 是 LiteLLM 风格的路由名；直连 OpenAI 兼容接口时只认真实的模型 id
    model_id = model[len('openai/'):] if model.startswith('openai/') else model
    print('[brief] 待译 %d 条 · 模型 %s · 接口 %s' % (len(titles), model_id, base))
    out = {}

    def once(batch):
        """一次请求。成功返回 {原文: 译文}；失败返回 None，交给上层重试或拆批。"""
        payload = {
            'model': model_id,
            'temperature': 0.2,
            'messages': [
                {'role': 'system',
                 'content': '你是学术出版与科技领域的专业译者。把用户给出的英文标题逐条翻译成简体中文，'
                            '保留期刊名、机构名、产品名的通行写法，不要添加解释、引号或序号。'},
                {'role': 'user',
                 'content': '只输出 JSON 数组，元素是与输入同序的字符串，数量必须等于 %d 条：\n%s'
                            % (len(batch), json.dumps(batch, ensure_ascii=False))},
            ],
        }
        req = urllib.request.Request(
            base + '/chat/completions',
            data=json.dumps(payload).encode('utf-8'),
            headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'},
            method='POST')
        try:
            resp = json.loads(urllib.request.urlopen(req, timeout=TIMEOUT).read().decode('utf-8', 'ignore'))
            content = (resp.get('choices') or [{}])[0].get('message', {}).get('content') or ''
            arr = json.loads(re.search(r'\[.*\]', content, re.S).group(0))
            if isinstance(arr, list) and len(arr) == len(batch):
                return {t: str(a).strip() for t, a in zip(batch, arr)}
            print('[brief] 返回数量不符（%d / %d），拆批重试' % (len(arr) if isinstance(arr, list) else -1, len(batch)))
        except Exception as exc:
            print('[brief] 请求失败：%r' % exc)
        return None

    def run(batch, depth=0):
        got = None
        for _ in range(RETRIES + 1):
            got = once(batch)
            if got is not None:
                break
        if got is None:
            if len(batch) == 1 or depth >= 5:
                print('[brief] 放弃 1 条（将按英文原题直录）：%s' % batch[0][:50])
                return
            mid = len(batch) // 2
            run(batch[:mid], depth + 1)
            run(batch[mid:], depth + 1)
            return
        out.update({t: a for t, a in got.items() if a})

    for start in range(0, len(titles), BATCH_SIZE):
        run(titles[start:start + BATCH_SIZE])
    print('[brief] 翻译成功 %d / %d 条' % (len(out), len(titles)))
    return out


# ── logo：本期真实出现的来源才内嵌，取不到就退回字母记号 ────────────────────
MIME = {'.png': 'image/png', '.svg': 'image/svg+xml', '.ico': 'image/x-icon',
        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp'}


def load_logos():
    if not os.path.exists(LOGOS_JSON):
        return []
    rows = json.load(io.open(LOGOS_JSON, encoding='utf-8'))
    out = []
    for r in rows:
        if not r.get('file'):
            continue
        path = os.path.normpath(os.path.join(HERE, '..', r['file']))
        if not os.path.exists(path):
            continue
        ext = os.path.splitext(path)[1].lower()
        data = base64.b64encode(io.open(path, 'rb').read()).decode()
        out.append((r['source'].lower(), 'data:%s;base64,%s' % (MIME.get(ext, 'image/png'), data)))
    out.sort(key=lambda x: -len(x[0]))      # 长名优先，避免 "Science" 抢走 "Science News"
    return out


def src_key(source: str) -> str:
    """'Scholarly Kitchen (学术出版行业)' → 'Scholarly Kitchen'；纯中文名保持原样。"""
    head = re.split(r'\s*[（(]', source)[0].strip()
    return head or source.strip()


def monogram(name: str) -> str:
    if is_cjk_heavy(name):
        return name[:2]
    words = [w for w in re.split(r'[^A-Za-z0-9]+', name) if w]
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    return (words[0][:2] if words else '··').upper()


def build_src(items, logos):
    src, used = {}, 0
    for it in items:
        name = it['source'] or '未知来源'
        if name in src:
            continue
        key = src_key(name).lower()
        data_uri = ''
        for pat, uri in logos:
            if pat in key or key in pat:
                data_uri = uri
                used += 1
                break
        src[name] = {'n': src_key(name), 'm': monogram(src_key(name)), 'd': data_uri}
    return src, used


# ── 时间模型：把 'MM-DD HH:MM' 换算成「距 base 的分钟数」 ──────────────────
def parse_stamp(s: str, ref: dt.datetime) -> dt.datetime:
    m = re.match(r'(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})', s.strip())
    if not m:
        return ref
    mo, d, h, mi = (int(x) for x in m.groups())
    best = None
    for y in (ref.year - 1, ref.year, ref.year + 1):
        try:
            c = dt.datetime(y, mo, d, h, mi)
        except ValueError:
            continue
        if best is None or abs((c - ref).total_seconds()) < abs((best - ref).total_seconds()):
            best = c
    return best or ref


def date_from_path(path: str) -> dt.date:
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})', os.path.basename(os.path.dirname(path)) or path)
    if m:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})', os.path.basename(path))
    if m:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return dt.date.today()


def floor_hour(t: dt.datetime) -> dt.datetime:
    return t.replace(minute=0, second=0, microsecond=0)


def build_model(items, gen_dt):
    """base = 分布带左端点；所有 m 都是距 base 的分钟数。"""
    for it in items:
        it['dt'] = parse_stamp(it['time'], gen_dt)

    lo = min([it['dt'] for it in items] + [gen_dt])
    hi = max([it['dt'] for it in items] + [gen_dt])
    x0 = floor_hour(lo)
    x1 = floor_hour(hi) + dt.timedelta(hours=1)
    span = max(180, int((x1 - x0).total_seconds() // 60))

    for it in items:
        t = it['dt']
        it['m'] = int((t - x0).total_seconds() // 60)
        it['ts'] = '%02d-%02d %02d:%02d' % (t.month, t.day, t.hour, t.minute)
        it['late'] = t > gen_dt

    # 换日线与小时刻度
    daymarks, firstDay = [], x0.strftime('%m-%d')
    d = (x0 + dt.timedelta(days=1)).replace(hour=0, minute=0)
    while d < x1:
        daymarks.append({'m': int((d - x0).total_seconds() // 60), 'label': d.strftime('%m-%d')})
        d += dt.timedelta(days=1)
    step = 120 if span <= 30 * 60 else (180 if span <= 54 * 60 else 360)
    ticks = []
    t = x0 + dt.timedelta(minutes=step)
    while t < x1:
        ticks.append({'m': int((t - x0).total_seconds() // 60),
                      'label': t.strftime('%H'),
                      'major': t.hour == 0})
        t += dt.timedelta(minutes=step)

    # 抓取时刻：窗口内的每一次 08:00 / 20:00
    crawls = []
    day = x0.replace(hour=0, minute=0)
    while day < x1:
        for h in CRAWL_HOURS:
            c = day + dt.timedelta(hours=h)
            if x0 < c < x1:
                crawls.append({'m': int((c - x0).total_seconds() // 60),
                               'label': '抓取 %02d:00' % h})
        day += dt.timedelta(days=1)

    day0 = daymarks[0]['m'] if daymarks else -1
    gap = max(120, int(span * 0.18) // 30 * 30)
    model = {
        # base 用 Z 结尾：把「墙上时间」当作 UTC 时刻发出，前端用 getUTC* 读回的就是北京时间墙钟。
        # 若写成 +08:00，读出来会整体早 8 小时。
        'base': x0.strftime('%Y-%m-%dT%H:%M:00Z'),
        'span': span, 'day0': day0, 'gen': int((gen_dt - x0).total_seconds() // 60),
        'genLabel': '汇总生成 ' + gen_dt.strftime('%H:%M'),
        'gap': gap, 'ticks': ticks, 'daymarks': daymarks, 'firstDay': firstDay,
        'crawls': crawls,
    }
    return model


def cluster_notes(items, model):
    """给每个时刻簇一句定性，全部由数据算出来，不写死。"""
    gap, span = model['gap'], model['span']
    ordered = sorted(items, key=lambda i: i['m'])
    clusters = []
    for it in ordered:
        if clusters:
            prev = clusters[-1]
            crossed = any(prev['first'] < d['m'] <= it['m'] for d in model['daymarks'])
            if it['m'] - prev['last'] > gap or crossed:
                clusters.append({'first': it['m'], 'last': it['m'], 'n': 1})
                continue
            prev['last'] = it['m']
            prev['n'] += 1
        else:
            clusters.append({'first': it['m'], 'last': it['m'], 'n': 1})
    for c in clusters:
        c['rate'] = c['n'] / max((c['last'] - c['first']) / 60.0, 0.5)
    top = max(range(len(clusters)), key=lambda i: clusters[i]['rate']) if clusters else -1
    late_n = sum(1 for i in items if i['late'])
    notes = {}
    for idx, c in enumerate(clusters):
        key = chr(65 + idx)
        starts_prev = c['first'] < (model['day0'] if model['day0'] >= 0 else span)
        txt = ''
        if idx == 0 and starts_prev and len(clusters) > 1:
            txt = '隔夜余流 · 上一日的傍晚到深夜'
        elif idx == top:
            txt = '全天主脉冲 · 分布带上最高的一段'
        elif idx == len(clusters) - 1 and c['n'] <= 4:
            txt = '尾段孤柱' + (' · 含 %d 条晚于汇总时刻' % late_n if late_n else '')
        notes[key] = txt
    return notes


def band_aria(items, model):
    ordered = sorted(items, key=lambda i: i['m'])
    span_h = model['span'] / 60.0
    return ('本期 %d 条条目的时间分布带：%s 至 %s，横向覆盖 %.1f 小时；'
            '虚线为每天 %s 的抓取时刻，实线为汇总生成时刻；'
            '柱高表示该时刻前后 45 分钟内的条目数。'
            % (len(ordered), ordered[0]['ts'], ordered[-1]['ts'], span_h,
               ' / '.join('%02d:00' % h for h in CRAWL_HOURS)))


# ── 文案令牌 ─────────────────────────────────────────────────────────────
CN_NUM = u'〇一二三四五六七八九'


def cn_two(n: int) -> str:
    """1–31 的中文读法：十七 / 二十 / 二十三。"""
    if n < 10:
        return CN_NUM[n]
    if n == 10:
        return u'十'
    if n < 20:
        return u'十' + CN_NUM[n % 10]
    return CN_NUM[n // 10] + u'十' + (CN_NUM[n % 10] if n % 10 else u'')


def cn_date(d: dt.date) -> str:
    y = ''.join(CN_NUM[int(c)] for c in '%04d' % d.year)
    return u'%s年%s月%s日' % (y, cn_two(d.month), cn_two(d.day))


SEP = '<span class="sep">/</span>'


def build_tokens(info, items, model, date_iso, report_type, current_key, src, logos_used):
    n = len(items)
    n_src = len(src)
    late = sum(1 for i in items if i['late'])
    no_zh = sum(1 for i in items if not i['zh'])
    rss = info.get('RSS 命中', '—')
    feeds = info.get('RSS 源', '—')
    crawl_txt = SEP.join('%02d:00' % h for h in CRAWL_HOURS).replace(SEP, ' 与 ')
    n_feeds = feeds.split('/')[1].strip() if '/' in feeds else str(n_src)

    mast_meta = (
        '  <p class="mast-meta pretty">\n'
        '    <b>生成于 %s</b>%s<b>%d 条条目</b>%s<b>%d 个来源</b>%s\n'
        '    RSS 命中 %s%s定向信息源 %s%s每天 %s 各抓取一次\n'
        '  </p>' % (info.get('生成时间', '—'), SEP, n, SEP, n_src, SEP,
                    rss, SEP, feeds, SEP, crawl_txt))

    d = dt.date.fromisoformat(date_iso)
    mast_title = (
        '  <h1 class="mast-title">\n'
        '    <span>\n'
        '      <span class="yr">ISSUE · %d · %s</span>\n'
        '      <span class="big">%s<span class="sep">·</span>%s</span>\n'
        '    </span>\n'
        '    <span class="kind">%s<small>%s 个定向信息源，经筛选后保留 %d 条，'
        '按各自发布时间铺成下方这条分布带</small></span>\n'
        '  </h1>' % (d.year, report_type, d.strftime('%m'), d.strftime('%d'),
                     cn_date(d), n_feeds, n))

    legend_late = ('<span><i class="k k-g"></i><b>空心柱</b> = 发布晚于汇总时刻'
                   '（%s）</span>' % (u'%d 条，原样保留' % late if late else u'本期无'))

    notes = []
    if late:
        notes.append(u'%d 条发布时间晚于本汇总时刻，未做修正' % late)
    if no_zh:
        notes.append(u'%d 条中译待补，按英文原题直录' % no_zh)
    if n_src != logos_used:
        notes.append(u'来源标识 %d/%d 取自各站点官方图标，其余以字母记号占位'
                     % (logos_used, n_src))
    footer = (
        '  <div class="f-in">\n'
        '    <div class="f-mark">%s\n'
        '      <span>每天 %s 两次抓取 · 历史期次永久归档，可在页首切换日期回看</span>\n'
        '    </div>\n'
        '    <div class="f-right pretty">\n'
        '      <b>%s 期 · %s · %d 条</b><br>\n'
        '      中文译文为主条，英文原标题降为副行（副行前标 EN）<br>\n'
        '%s    </div>\n'
        '  </div>' % (BRIEF_NAME, crawl_txt, date_iso, report_type, n,
                      ''.join('      %s<br>\n' % s for s in notes)))

    noscript = (u'本页由内联的 %d 条真实数据渲染时间分布带与条目列表，需在启用 JavaScript '
                u'的浏览器中打开；数据同时保存在 #brief-data 里，可直接取用。' % n)

    return {
        'PAGE_TITLE': '%s · %s %s' % (BRIEF_NAME, date_iso, report_type),
        'NAME': BRIEF_NAME, 'TAGLINE': BRIEF_TAG, 'DATE_ISO': date_iso,
        'REPORT_TYPE': report_type, 'CURRENT_KEY': current_key,
        'MAST_META': mast_meta, 'MAST_TITLE': mast_title,
        'LEGEND_LATE': legend_late, 'FOOTER': footer, 'NOSCRIPT': noscript,
    }


def render(path: str) -> bool:
    html = io.open(path, encoding='utf-8').read()
    parsed = parse_report(html)
    items = parsed['items']
    if not items:
        print('[brief] %s 里没解析到条目，保持原文件' % path)
        return False

    info = parsed['info']
    hit = re.match(r'\s*(\d+)', info.get('RSS 命中', ''))
    if hit and int(hit.group(1)) != len(items):
        print('[brief] 注意：解析到 %d 条，但报告自称 RSS 命中 %s 条——'
              '上游结构可能变了，请核对' % (len(items), hit.group(1)))
    report_date = date_from_path(path)
    gen = parse_stamp(info.get('生成时间', '') or '01-01 00:00',
                      dt.datetime.combine(report_date, dt.time(23, 59)))
    if gen.date() != report_date:
        gen = dt.datetime.combine(report_date, gen.time())
    report_type = info.get('报告类型', '全天汇总')

    need = sorted({i['en'] for i in items if not i['zh'] and i['en']})
    if need:
        mapping = translate(need)
        for it in items:
            if not it['zh'] and it['en'] in mapping:
                it['zh'] = mapping[it['en']]
            if it['zh'] and it['zh'] == it['en']:
                it['en'] = ''          # 原文本就是中文，别排出一条重复的 EN 副行

    model = build_model(items, gen)
    notes = cluster_notes(items, model)
    model['clusterNotes'] = notes
    model['aria'] = band_aria(items, model)

    logos = load_logos()
    src, used = build_src(items, logos)

    raw = [{'m': i['m'], 'ts': i['ts'], 's': i['source'], 'g': i['group'],
            'zh': i['zh'], 'en': i['en'], 'u': i['url'], 'late': 1 if i['late'] else 0,
            'new': 1 if i['new'] else 0} for i in items]
    groups = []
    for i in items:
        if i['group'] not in groups:
            groups.append(i['group'])

    def abbrev(g):
        head = re.split(u'[与/·]', g)[0].strip()
        return head[:6] if len(head) > 6 else head

    abbr = {g: abbrev(g) for g in groups}

    data = {'raw': raw, 'src': src, 'groups': groups, 'abbr': abbr, 'model': model}
    tpl = io.open(TEMPLATE, encoding='utf-8').read()
    tokens = build_tokens(info, items, model, report_date.isoformat(), report_type,
                          current_key(path), src, used)
    tokens['DATA'] = json.dumps(data, ensure_ascii=False, separators=(',', ':')) \
        .replace('</', '<\\/')

    out = tpl
    for k, v in tokens.items():
        out = out.replace('{{%s}}' % k, v)
    left = re.findall(r'\{\{[A-Z_]+\}\}', out)
    if left:
        raise RuntimeError('模板里有令牌没被替换：%s' % set(left))

    io.open(path, 'w', encoding='utf-8', newline='\n').write(out)
    print('[brief] 已重渲染 %s：%d 条 / %d 来源 / 中译待补 %d / 晚于汇总 %d'
          % (path, len(items), len(src), sum(1 for i in items if not i['zh']),
             sum(1 for i in items if i['late'])))
    return True


def current_key(path: str) -> str:
    d = os.path.basename(os.path.dirname(path))
    f = os.path.basename(path)
    return ('html/%s/%s' % (d, f)) if re.match(r'\d{4}-\d{2}-\d{2}$', d) else f


def main(paths):
    ok = 0
    for p in paths:
        try:
            if render(p):
                ok += 1
        except Exception as exc:
            print('[brief] %s 渲染失败：%r（保持原报告不动）' % (p, exc))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
