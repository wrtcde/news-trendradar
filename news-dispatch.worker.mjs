/**
 * news-dispatch — 用 Cloudflare Cron 外部触发 TrendRadar 抓取
 *
 * 背景：GitHub 对新创建仓库的 scheduled workflow 会降权/延迟（社区报告 8-14 小时甚至不触发），
 * 所以「每天早晚各一次」不能只靠 workflow 里的 on.schedule。这个 Worker 做外部保险：
 * 到点先问 GitHub「最近是不是已经跑过了」，没跑才 dispatch —— 两边都活着时不会推两条。
 *
 * 路由：
 *   GET  /                状态页：最近 6 次运行 + 本定时器会怎么判断
 *   POST /dispatch        立刻执行一次「查最近 + 需要则触发」（cron 走的就是这条逻辑）
 *   POST /dispatch?force=1  跳过判断强制触发（手动验证用）
 *   GET  /healthz         探活
 *
 * 需要的环境变量（wrangler vars）：
 *   GH_OWNER   wrtcde
 *   GH_REPO    news-trendradar
 *   GH_WORKFLOW  crawler.yml
 *   GH_REF     master
 *   GH_MINUTES 45      最近 N 分钟内已有运行就跳过，避免与 GitHub 自带 schedule 撞车
 *   CRON_LABEL 08:00 / 20:00（北京时间） 仅用于状态页展示
 * 密钥（wrangler secret put）：
 *   GH_PAT     fine-grained PAT，只给该仓库的 Actions: read/write
 *   ALERT_WEBHOOK  可选：填企业微信机器人地址则只在失败时告警
 */

const GH = 'https://api.github.com';

const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function cfg(env) {
  return {
    owner: env.GH_OWNER || 'wrtcde',
    repo: env.GH_REPO || 'news-trendradar',
    workflow: env.GH_WORKFLOW || 'crawler.yml',
    ref: env.GH_REF || 'master',
    minutes: Number(env.GH_MINUTES || 45),
    label: env.CRON_LABEL || '08:00 / 20:00（北京时间）',
  };
}

function gh(path, init, env) {
  const headers = Object.assign({
    Accept: 'application/vnd.github+json',
    'User-Agent': 'news-dispatch-cloudflare-worker',
  }, init && init.headers || {});
  if (env.GH_PAT) headers.Authorization = 'Bearer ' + env.GH_PAT;
  return fetch(GH + path, Object.assign({}, init, { headers }));
}

async function recentRun(env, c) {
  const res = await gh(`/repos/${c.owner}/${c.repo}/actions/workflows/${c.workflow}/runs?per_page=1`, {}, env);
  if (!res.ok) throw new Error(`读取运行记录失败 HTTP ${res.status}`);
  const data = await res.json();
  const run = (data.workflow_runs || [])[0];
  if (!run) return { run: null, fresh: false };
  const ageMin = (Date.now() - new Date(run.created_at).getTime()) / 60000;
  return { run, fresh: ageMin <= c.minutes, ageMin };
}

async function dispatchRun(env, c) {
  const res = await gh(`/repos/${c.owner}/${c.repo}/actions/workflows/${c.workflow}/dispatches`, {
    method: 'POST',
    'Content-Type': 'application/json',
    body: JSON.stringify({ ref: c.ref }),
  }, env);
  if (res.status !== 204) {
    const body = await res.text().catch(() => '');
    throw new Error(`触发失败 HTTP ${res.status} ${body.slice(0, 200)}`);
  }
  return true;
}

async function alert(env, text) {
  if (!env.ALERT_WEBHOOK) return;
  try {
    await fetch(env.ALERT_WEBHOOK, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ msgtype: 'text', text: { content: text } }),
    });
  } catch (e) { /* 告警本身失败不再递归 */ }
}

async function runOnce(env, force) {
  const c = cfg(env);
  try {
    if (!env.GH_PAT) throw new Error('未配置 GH_PAT 密钥');
    const { run, fresh, ageMin } = await recentRun(env, c);
    if (run && fresh && !force) {
      console.log(`跳过：${Math.round(ageMin)} 分钟前已有运行 #${run.run_number}`);
      return { action: 'skip', reason: `最近 ${Math.round(ageMin)} 分钟前已有运行 #${run.run_number}`, run };
    }
    await dispatchRun(env, c);
    console.log(`已触发 workflow_dispatch（${c.workflow} @ ${c.ref}）`);
    return { action: 'dispatched', reason: run ? `上一条是 ${Math.round(ageMin)} 分钟前，已过 ${c.minutes} 分钟窗口` : '尚无运行记录', run };
  } catch (err) {
    const msg = String(err && err.message || err);
    console.error('失败：' + msg);
    await alert(env, `【热点简报】定时抓取触发失败：${msg}\n工作流：${c.owner}/${c.repo}/${c.workflow}`);
    return { action: 'error', reason: msg };
  }
}

function page(env, c) {
  return `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>news-dispatch 状态</title>
<body style="font:15px/1.7 ui-monospace,Menlo,Consolas,monospace;background:#131110;color:#f5efe4;max-width:52rem;margin:3rem auto;padding:0 1.2rem">
<h1 style="font-size:1.25rem;letter-spacing:.06em">news-dispatch · 外部定时器状态</h1>
<p>设计触发时刻：${esc(c.label)}（UTC cron 见 Worker 配置）· 防撞窗口 ${c.minutes} 分钟</p>
<p>目标：<code>${esc(c.owner)}/${esc(c.repo)}</code> 的 <code>${esc(c.workflow)}</code>，ref <code>${esc(c.ref)}</code></p>
<div id="out">读取中…</div>
<script>
fetch('/status').then(r=>r.json()).then(d=>{
  var rows=(d.runs||[]).map(r=>'<tr><td>'+r.run_number+'</td><td>'+r.event+'</td><td>'+r.status+' / '+(r.conclusion||'—')+'</td><td>'+r.created_at+'</td></tr>').join('');
  document.getElementById('out').innerHTML =
    '<p>GH_PAT 已配置：'+(d.hasPat?'是':'<b style=color:#e2703a>否</b>')+'</p>'
    + '<p>判断结果：'+esc(d.verdict)+'</p>'
    + '<table border=0 cellpadding=6 style="width:100%;font-size:.9rem;text-align:left">'
    + '<tr style="opacity:.6"><th>#</th><th>触发方式</th><th>状态</th><th>创建时间(UTC)</th></tr>'+rows+'</table>';
});
</script>`;
}

async function status(env) {
  const c = cfg(env);
  let runs = [], verdict = '';
  try {
    const res = await gh(`/repos/${c.owner}/${c.repo}/actions/workflows/${c.workflow}/runs?per_page=6`, {}, env);
    const data = await res.json();
    runs = (data.workflow_runs || []).map((r) => ({
      run_number: r.run_number, event: r.event, status: r.status,
      conclusion: r.conclusion, created_at: r.created_at,
    }));
    const first = runs[0];
    if (!first) verdict = '还没有任何运行记录';
    else {
      const age = Math.round((Date.now() - new Date(first.created_at).getTime()) / 60000);
      const ev = first.event === 'schedule' ? 'GitHub 自带 schedule' : '外部/手动';
      verdict = `最近一条是 #${first.run_number}（${ev}），${age} 分钟前 · `
        + (age <= c.minutes ? '在窗口内，定时器本次会跳过' : '超出窗口，定时器到点会补触发');
    }
  } catch (e) { verdict = '读取失败：' + e.message; }
  return { hasPat: !!env.GH_PAT, verdict, runs };
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(runOnce(env, false));
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    const json = (obj, code = 200) => new Response(JSON.stringify(obj, null, 2),
      { status: code, headers: { 'content-type': 'application/json; charset=utf-8' } });

    if (url.pathname === '/healthz') return new Response('ok');
    if (url.pathname === '/status') return json(await status(env));

    if (request.method === 'POST' && url.pathname === '/dispatch') {
      const r = await runOnce(env, url.searchParams.get('force') === '1');
      return json(r, r.action === 'error' ? 502 : 200);
    }
    return new Response(page(env, cfg(env)), { headers: { 'content-type': 'text/html; charset=utf-8' } });
  },
};
