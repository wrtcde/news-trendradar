/**
 * news-archive — TrendRadar 报告归档浏览
 *
 * 数据源：R2 桶（绑定名 ARCHIVE），对象键约定 html/<YYYY-MM-DD>/<HH-MM>.html
 * 路由：
 *   GET /                          最新一期（默认首页）
 *   GET /latest                    同上
 *   GET /html/<date>/<file>.html   指定期次
 *   GET /archive                   完整归档索引
 *   GET /healthz                   探活
 *
 * 报告 HTML 由 TrendRadar 生成、原样存放在 R2；本 Worker 只在响应前注入一个日期
 * 选择控件（右上角浮层），不修改存储内容。
 */

const PREFIX = 'html/';
const MAX_KEYS_PER_PAGE = 1000;
const MAX_PAGES = 10;

export default {
  async fetch(request, env) {
    if (!env.ARCHIVE) return text('缺少 R2 绑定 ARCHIVE', 500);

    const path = decodeURIComponent(new URL(request.url).pathname);

    if (path === '/healthz') return text('ok');

    if (path === '/' || path === '/latest') {
      const days = await loadDays(env);
      if (!days.length) return html(WELCOME);
      return serveReport(env, PREFIX + days[0].date + '/' + days[0].files[0], days);
    }

    if (path.startsWith('/html/')) {
      const key = path.slice(1);
      if (!isSafeKey(key)) return text('非法路径', 400);
      const obj = await env.ARCHIVE.get(key);
      if (!obj) return text('报告不存在', 404);
      return serveReport(env, key);
    }

    if (path === '/archive' || path === '/archive/') {
      const keys = await listKeys(env);
      return html(renderIndex(keys));
    }

    return text('未找到', 404);
  },
};

/** 只允许 html/YYYY-MM-DD/HH-MM.html 这一种形状，避免路径穿越 */
function isSafeKey(key) {
  return /^html\/\d{4}-\d{2}-\d{2}\/[\w.-]+\.html$/.test(key);
}

async function listKeys(env) {
  const keys = [];
  let cursor;
  for (let page = 0; page < MAX_PAGES; page++) {
    const res = await env.ARCHIVE.list({ prefix: PREFIX, limit: MAX_KEYS_PER_PAGE, cursor });
    for (const obj of res.objects) keys.push(obj.key);
    if (!res.truncated) break;
    cursor = res.cursor;
    if (!cursor) break;
  }
  keys.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  return keys;
}

/** 取回报告并补上期次导航；days 可由调用方传入以复用同一次列举 */
async function serveReport(env, key, preDays) {
  const obj = await env.ARCHIVE.get(key);
  if (!obj) return text('报告不存在', 404);

  const [raw, days] = await Promise.all([obj.text(), preDays || loadDays(env)]);
  const out = isBrief(raw)
    ? applyBriefNav(raw, days, key)
    : insertBeforeBody(raw, renderWidget(days, key));

  return new Response(out, {
    headers: {
      'content-type': 'text/html; charset=utf-8',
      'cache-control': 'public, max-age=60',
    },
  });
}

function insertBeforeBody(html, extra) {
  const cut = html.lastIndexOf('</body>');
  return cut >= 0 ? html.slice(0, cut) + extra + html.slice(cut) : html + extra;
}

/* ══ 自建渲染层（热点简报）：模板留了占位，Worker 只负责填归档数据 ══════════ */
const BRIEF_MARK = 'id="brief-nav"';

function isBrief(html) {
  return html.includes(BRIEF_MARK);
}

function applyBriefNav(html, days, currentKey) {
  const nav = html.match(/<div class="issue-switch" id="brief-nav"[^>]*>[\s\S]*?<\/div>/);
  if (!nav) return html;
  html = html.replace(/<span id="railIssueSlot"><\/span>/,
    '<button class="chip" id="railIssue" type="button">切换日期 / 期次</button>');
  html = html.replace(nav[0], () => renderBriefNav(days, currentKey, nav[0]));
  return insertBeforeBody(html, renderBriefPop(days, currentKey));
}

/** 页首那一排：更新一期 / 当期 / 更早一期，都指向归档里真实存在的期次 */
function renderBriefNav(days, currentKey, original) {
  const flat = [];
  for (const d of days) for (const f of d.files) flat.push({ date: d.date, file: f });
  const i = flat.findIndex((x) => PREFIX + x.date + '/' + x.file === currentKey);
  const cur = i >= 0 ? flat[i] : null;
  const older = i >= 0 && i + 1 < flat.length ? flat[i + 1] : null;
  const newer = i > 0 ? flat[i - 1] : null;
  const link = (x, txt) => `<a class="nav" href="/${PREFIX}${x.date}/${x.file}"` +
    ` title="${x.date} ${label(x.file)}">${txt}</a>`;
  const dead = (txt) => `<button class="nav" type="button" disabled aria-disabled="true">${txt}</button>`;
  // 期次类型由渲染层写进占位里（全天汇总 / 时段汇总…），Worker 原样读回来，不自己编
  const type = ((original.match(/<em>([^<]*)<\/em>/) || [])[1] || '').trim() || '本期';
  const date = cur ? cur.date : '';
  return `<div class="issue-switch" ${BRIEF_MARK} data-current="${currentKey}"` +
    ` role="group" aria-label="切换日期与期次">
      ${newer ? link(newer, '◀ 更新一期') : dead('◀ 更新一期')}
      <button class="cur" id="issueBtn" type="button" aria-haspopup="dialog" aria-expanded="false">
        ${date}<em>${type} ▾</em>
      </button>
      ${older ? link(older, '更早一期 ▶') : dead('更早一期 ▶')}
    </div>`;
}

/** 日历弹层：为每个「有归档的月份」预生成格子，◀/▶ 只在真实月份之间跳 */
function renderBriefPop(days, currentKey) {
  const m = currentKey.match(/html\/(\d{4})-(\d{2})-(\d{2})/) || [];
  const curY = Number(m[1]), curMo = Number(m[2]) - 1, curDay = Number(m[3]);
  const byMonth = new Map();
  const note = (date) => {
    const k = date.slice(0, 7);
    if (!byMonth.has(k)) byMonth.set(k, []);
    byMonth.get(k).push(date);
  };
  days.forEach((d) => note(d.date));
  note(`${curY}-${pad(curMo + 1)}-${pad(curDay || 1)}`);

  const months = [...byMonth.keys()].sort();
  const cal = {};
  for (const ym of months) {
    const [y, mo] = ym.split('-').map(Number);
    cal[ym] = monthCells(y, mo - 1, days, ym === `${curY}-${pad(curMo + 1)}` ? curDay : -1);
  }
  const total = days.reduce((n, d) => n + d.files.length, 0);
  const curIdx = months.indexOf(`${curY}-${pad(curMo + 1)}`);

  return `<div class="pop" id="pop" role="dialog" aria-modal="false" aria-label="切换日期与期次">
  <div class="pop-head">
    <button type="button" id="calPrev" aria-label="上一月">◀</button>
    <span class="m" id="calMonth">${curY} 年 ${curMo + 1} 月</span>
    <button type="button" id="calNext" aria-label="下一月">▶</button>
  </div>
  <div class="grid7" id="calGrid">${WEEK_HEAD + cal[`${curY}-${pad(curMo + 1)}`]}</div>
  <p class="pop-key"><i></i>实心方块 = 有归档的日期 · 共 ${days.length} 天 ${total} 期</p>
  <p class="pop-note pretty">归档存放在 R2 的 html/&lt;日期&gt;/&lt;时刻&gt;.html；一天多次抓取会并列多期，点日期进当天最新一期。</p>
  <p class="pop-live" id="calLive" aria-live="polite"></p>
</div>
<script>
(function(){
  var CAL=${JSON.stringify(cal)}, MO=${JSON.stringify(months)}, k=${curIdx};
  var pop=document.getElementById("pop"), grid=document.getElementById("calGrid"),
      head=document.getElementById("calMonth"), live=document.getElementById("calLive");
  var btn=document.getElementById("issueBtn"), rail=document.getElementById("railIssue");
  var prev=document.getElementById("calPrev"), next=document.getElementById("calNext");
  function open(on){pop.classList.toggle("is-open",on);
    if(btn)btn.setAttribute("aria-expanded",String(on));}
  function paint(){
    var ym=MO[k], p=ym.split("-");
    head.textContent=p[0]+" 年 "+Number(p[1])+" 月";
    grid.innerHTML=${JSON.stringify(WEEK_HEAD)}+CAL[ym];
    prev.disabled = k<=0; next.disabled = k>=MO.length-1;
    live.textContent = prev.disabled && next.disabled ? "归档只覆盖这一个月" : "";
  }
  prev.disabled = k<=0; next.disabled = k>=MO.length-1;
  if(btn)btn.addEventListener("click",function(){open(!pop.classList.contains("is-open"));});
  if(rail)rail.addEventListener("click",function(){open(true);scrollTo({top:0,behavior:"auto"});});
  prev.addEventListener("click",function(){if(k>0){k--;paint();}});
  next.addEventListener("click",function(){if(k<MO.length-1){k++;paint();}});
  document.addEventListener("keydown",function(e){if(e.key==="Escape")open(false);});
  document.addEventListener("click",function(e){
    if(pop.classList.contains("is-open")&&!pop.contains(e.target)&&
       !e.target.closest(".issue-switch")&&e.target.id!=="railIssue")open(false);
  });
})();
</script>`;
}

const WEEK_HEAD = '<div class="wd">日</div><div class="wd">一</div><div class="wd">二</div>' +
  '<div class="wd">三</div><div class="wd">四</div><div class="wd">五</div><div class="wd">六</div>';

/** 一个月的格子：有归档的日期是链接（带当期数角标），没有的按模板 nodata 样式弱化 */
function monthCells(y, moIndex, days, curDay) {
  const byDate = new Map(days.map((d) => [d.date, d]));
  const first = new Date(Date.UTC(y, moIndex, 1));
  const span = new Date(Date.UTC(y, moIndex + 1, 0)).getUTCDate();
  let cells = '';
  for (let i = 0; i < first.getUTCDay(); i++) cells += '<span class="day empty"></span>';
  for (let d = 1; d <= span; d++) {
    const iso = `${y}-${pad(moIndex + 1)}-${pad(d)}`;
    const rec = byDate.get(iso);
    if (!rec) { cells += `<span class="day nodata">${d}</span>`; continue; }
    const n = rec.files.length;
    const cls = d === curDay ? 'day today' : 'day';
    cells += `<a class="${cls}" href="/${PREFIX}${iso}/${rec.files[0]}"` +
      ` aria-label="${iso} 有 ${n} 期">${d}${n > 1 ? `<b>${n}</b>` : ''}</a>`;
  }
  return cells;
}

function pad(n) {
  return n < 10 ? '0' + n : '' + n;
}

async function loadDays(env) {
  return groupByDate(await listKeys(env));
}

function groupByDate(keys) {
  const map = new Map();
  for (const key of keys) {
    const rest = key.slice(PREFIX.length);
    const slash = rest.indexOf('/');
    if (slash < 0) continue;
    const date = rest.slice(0, slash);
    const file = rest.slice(slash + 1);
    if (!map.has(date)) map.set(date, []);
    map.get(date).push(file);
  }
  return [...map.entries()]
    .map(([date, files]) => ({ date, files: files.sort().reverse() }))
    .sort((a, b) => (a.date < b.date ? 1 : -1));
}

function label(file) {
  return file.replace(/\.html$/, '').replace('-', ':');
}

/** 右上角浮层：一个按钮 + 展开后的日期面板 */
function renderWidget(days, currentKey) {
  const rows = days
    .map((d) => {
      const chips = d.files
        .map((f) => {
          const href = '/' + PREFIX + d.date + '/' + f;
          const on = currentKey === PREFIX + d.date + '/' + f ? ' on' : '';
          return `<a class="chip${on}" href="${href}">${label(f)}</a>`;
        })
        .join('');
      return `<div class="day"><span class="day-name">${d.date}</span><span class="chips">${chips}</span></div>`;
    })
    .join('');

  const empty = days.length ? '' : '<p class="none">还没有归档记录。</p>';
  const newest = days.length ? days[0].date + ' ' + label(days[0].files[0]) : '无';

  return `<div id="tr-cal">
<button id="tr-cal-btn" type="button" aria-expanded="false" aria-controls="tr-cal-panel">
  <span class="tr-ico" aria-hidden="true"></span>日历<span class="tr-caret" aria-hidden="true"></span>
</button>
<div id="tr-cal-panel" role="dialog" aria-label="选择日期" hidden>
  <div class="tr-head"><b>选择期次</b><span class="tr-sub">当前最新 ${newest}</span>
    <a class="tr-all" href="/archive">全部归档</a>
  </div>
  ${empty}
  <div class="tr-days">${rows}</div>
</div>
</div>
<style>
#tr-cal{position:fixed;top:12px;left:12px;z-index:2147483000;font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;font-size:13px}
#tr-cal-btn{display:inline-flex;align-items:center;gap:.4em;padding:.35rem .7rem;border:1px solid rgba(0,0,0,.14);
  border-radius:999px;background:rgba(255,255,255,.92);color:#1f2328;cursor:pointer;
  box-shadow:0 1px 3px rgba(0,0,0,.12);backdrop-filter:blur(6px)}
#tr-cal-btn:hover{background:#fff}
.tr-ico{width:.85em;height:.85em;border:1.5px solid currentColor;border-radius:2px;position:relative;display:inline-block}
.tr-ico::after{content:"";position:absolute;left:0;right:0;top:.22em;height:1.5px;background:currentColor}
.tr-caret{border-left:3.5px solid transparent;border-right:3.5px solid transparent;border-top:4.5px solid currentColor;opacity:.65}
#tr-cal-panel{margin-top:.4rem;width:min(20rem,calc(100vw - 24px));max-height:70vh;overflow:auto;
  background:#fff;border:1px solid #e3e6ea;border-radius:10px;box-shadow:0 8px 28px rgba(0,0,0,.16)}
#tr-cal-panel[hidden]{display:none}
.tr-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:.5rem;padding:.7rem .9rem;border-bottom:1px solid #eceff2}
.tr-sub{color:#57606a;font-size:.85rem}
.tr-all{margin-left:auto;color:#0969da;text-decoration:none;font-size:.85rem}
.tr-all:hover{text-decoration:underline}
.tr-days{padding:.35rem .9rem .8rem}
.day{padding:.5rem 0;border-bottom:1px dashed #eceff2}
.day:last-child{border-bottom:0}
.day-name{display:block;font-variant-numeric:tabular-nums;font-weight:600;margin-bottom:.35rem}
.chips{display:flex;flex-wrap:wrap;gap:.35rem}
.chip{padding:.2rem .55rem;border-radius:6px;background:#f2f3f5;color:#0969da;text-decoration:none;
  font-variant-numeric:tabular-nums}
.chip:hover{background:#e7e9ee}
.chip.on{background:#0969da;color:#fff}
.none{padding:1rem;color:#57606a}
body.dark-mode #tr-cal-btn,body.dark-mode #tr-cal-panel{background:#161b22;color:#e6edf3;border-color:#30363d}
body.dark-mode .chip{background:#21262d;color:#79c0ff}
body.dark-mode .chip.on{background:#1f6feb;color:#fff}
body.dark-mode .tr-head,body.dark-mode .day{border-color:#30363d}
@media (prefers-color-scheme:dark){
  #tr-cal-btn,#tr-cal-panel{background:#161b22;color:#e6edf3;border-color:#30363d}
  .chip{background:#21262d;color:#79c0ff}
  .chip.on{background:#1f6feb;color:#fff}
  .tr-sub,.none{color:#8b949e}
  .tr-all{color:#79c0ff}
  .tr-head,.day{border-color:#30363d}
}
</style>
<script>
(function(){
  var btn=document.getElementById('tr-cal-btn'), panel=document.getElementById('tr-cal-panel');
  if(!btn||!panel)return;
  btn.addEventListener('click',function(e){
    e.stopPropagation();
    var open=panel.hasAttribute('hidden');
    if(open){panel.removeAttribute('hidden');}else{panel.setAttribute('hidden','');}
    btn.setAttribute('aria-expanded',open?'true':'false');
  });
  document.addEventListener('click',function(e){
    if(!panel.hasAttribute('hidden')&&!panel.contains(e.target)&&e.target!==btn){
      panel.setAttribute('hidden','');btn.setAttribute('aria-expanded','false');
    }
  });
  document.addEventListener('keydown',function(e){
    if(e.key==='Escape'){panel.setAttribute('hidden','');btn.setAttribute('aria-expanded','false');}
  });
})();
</script>`;
}

function renderIndex(keys) {
  const days = groupByDate(keys);
  if (!days.length) {
    return page('归档索引', '<p class="empty">还没有任何归档。等 TrendRadar 跑完一次并上传后，这里就会出现条目。</p>');
  }
  const total = keys.length;
  const body = days
    .map((d) => {
      const items = d.files.map((f) => `<li><a href="/${PREFIX}${d.date}/${f}">${label(f)}</a></li>`).join('');
      return `<section class="day"><h2>${d.date}<span class="count">${d.files.length} 期</span></h2><ul class="times">${items}</ul></section>`;
    })
    .join('');
  return page('归档索引', `<p class="meta">共 ${days.length} 天、${total} 期报告。<a href="/latest">直接看最新一期 →</a></p>${body}`);
}

const WELCOME = page('归档索引', '<p class="empty">还没有任何归档。等 TrendRadar 跑完一次并上传后，这里就会出现内容。</p>');

function page(title, content) {
  return `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${title} · 热点简报归档</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         margin: 0; padding: 2rem 1rem; background: #f6f7f9; color: #1f2328; }
  main { max-width: 46rem; margin: 0 auto; }
  h1 { font-size: 1.5rem; margin: 0 0 .4rem; }
  .meta { color: #57606a; margin: 0 0 2rem; }
  .meta a { color: #0969da; }
  .day { background: #fff; border: 1px solid #e3e6ea; border-radius: 10px;
         padding: 1rem 1.25rem; margin-bottom: 1rem; }
  .day h2 { font-size: 1.05rem; margin: 0 0 .75rem; display: flex;
            justify-content: space-between; align-items: baseline; }
  .count { font-size: .8rem; font-weight: 400; color: #57606a; }
  ul.times { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: .5rem; }
  ul.times a { display: inline-block; padding: .3rem .7rem; border-radius: 6px;
               background: #f2f3f5; color: #0969da; text-decoration: none;
               font-variant-numeric: tabular-nums; }
  ul.times a:hover { background: #e7e9ee; }
  .empty { background: #fff; border: 1px dashed #d0d7de; padding: 2rem; border-radius: 10px;
           text-align: center; color: #57606a; }
  footer { margin-top: 2.5rem; text-align: center; color: #8b949e; font-size: .8rem; }
  @media (prefers-color-scheme: dark) {
    body { background: #0d1117; color: #e6edf3; }
    .day, .empty { background: #161b22; border-color: #30363d; }
    ul.times a { background: #21262d; color: #79c0ff; }
    ul.times a:hover { background: #30363d; }
    .meta, .count, .empty { color: #8b949e; }
    .meta a { color: #79c0ff; }
  }
</style>
</head>
<body>
<main>
  <h1>热点简报归档</h1>
  ${content}
  <footer>由 Cloudflare Worker + R2 提供 · <a href="/">回到最新一期</a></footer>
</main>
</body>
</html>`;
}

function html(body) {
  return new Response(body, {
    headers: { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' },
  });
}

function text(body, status = 200) {
  return new Response(body, { status, headers: { 'content-type': 'text/plain; charset=utf-8' } });
}
