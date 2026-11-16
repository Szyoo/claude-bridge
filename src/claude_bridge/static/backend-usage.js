// A backend's own usage page (the account page with ?svc=<name>): balance and lifetime spend, the spend over 24 h /
// 7 d / 30 d (stacked by model, in the viewer's time zone), the spend by model, the provider's price list, and a
// what-if: the same turns priced on another of its models. Rows come from GET /api/backends/<name>/usage; the server
// prices the what-if (`as_model`) with the same rule it bills with, each row keeping its own peak / off-peak.
import { api, esc, toast, fmtMoney, fmtTok, fmtBalance } from '/static/bridge/bridge-pages.js';

const RANGES = [[1, '24 小时'], [7, '7 天'], [30, '30 天']];
const bare = (m) => String(m || '').replace(/\[1m\]$/, '');
const count = (v) => Number(v || 0).toLocaleString('zh-CN');
const utc = (s) => new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(s) ? s : `${String(s).replace(' ', 'T')}Z`);
const tokens = (r) => r.input + r.cache_read + r.cache_write + r.output;
const turns = (rows) => new Set(rows.map((r, i) => r.job ?? `row${i}`)).size;
const sum = (rows, f) => rows.reduce((a, r) => a + f(r), 0);

// the provider's peak hours ([[start, end), …] in UTC) in the viewer's own clock
function peakText(peak) {
  const hours = peak?.utc_hours || [];
  if (!hours.length) return '';
  const off = -new Date().getTimezoneOffset() / 60;
  const local = (h) => ((h + off) % 24 + 24) % 24;
  const spans = hours.map(([a, b]) => `${local(a)}–${local(b)} 点`).join('、');
  return `高峰时段（你的时区）${peak.weekdays_only ? '工作日 ' : ''}${spans}，按 ${peak.multiplier || 1} 倍计价`;
}

// buckets in the viewer's time zone: hours for one day, days for longer
function buckets(days) {
  const now = new Date(), out = [];
  if (days === 1) {
    const end = new Date(now); end.setMinutes(0, 0, 0);
    for (let i = 23; i >= 0; i--) {
      const t = new Date(end.getTime() - i * 3600e3);
      out.push({ start: t.getTime(), end: t.getTime() + 3600e3, label: `${t.getHours()}:00`, tip: `${t.getMonth() + 1}/${t.getDate()} ${t.getHours()}:00` });
    }
  } else {
    const end = new Date(now); end.setHours(0, 0, 0, 0);
    for (let i = days - 1; i >= 0; i--) {
      const t = new Date(end); t.setDate(end.getDate() - i);
      const next = new Date(t); next.setDate(t.getDate() + 1);
      out.push({ start: t.getTime(), end: next.getTime(), label: `${t.getMonth() + 1}/${t.getDate()}`, tip: `${t.getMonth() + 1}/${t.getDate()}` });
    }
  }
  return out;
}

export function mountBackendUsage(el, name, { topUpUrl = '' } = {}) {
  let days = 1, data = null, simFrom = '', simTo = '', sim = null, seq = 0;
  const cur = () => data?.currency || 'USD';
  const money = (v) => fmtMoney(v, cur());
  const nameOf = (m) => data?.models.find(x => bare(x.model) === bare(m))?.name || bare(m) || '（未知模型）';

  async function load() {
    const my = ++seq;
    try {
      const d = await api('GET', `/api/backends/${encodeURIComponent(name)}/usage?days=${days}`);
      if (my !== seq) return;
      data = d; sim = null;
      if (!simTo || !data.models.some(m => m.model === simTo)) simTo = data.models.find(m => !bare(simFrom) || bare(m.model) !== bare(simFrom))?.model || '';
      render();
      if (simTo) runSim();
    } catch (e) { toast(e.message, true); }
  }
  async function runSim() {
    const my = seq;
    try {
      const d = await api('GET', `/api/backends/${encodeURIComponent(name)}/usage?days=${days}&as_model=${encodeURIComponent(simTo)}`);
      if (my !== seq) return;
      sim = d.rows; renderSim();
    } catch (e) { toast(e.message, true); }
  }

  function render() {
    const rows = data.rows, total = sum(rows, r => r.cost), bal = fmtBalance(data.balance);
    const models = [...new Set(rows.map(r => bare(r.model)))];
    const color = (m) => `bp-u-c${Math.max(0, models.indexOf(bare(m))) % 4}`;
    const low = data.balance?.is_available === false;

    // spend over time, stacked by model
    const bks = buckets(days).map(b => {
      const inb = rows.filter(r => { const t = utc(r.at).getTime(); return t >= b.start && t < b.end; });
      return { ...b, total: sum(inb, r => r.cost), parts: models.map(m => [m, sum(inb.filter(r => bare(r.model) === m), r => r.cost)]) };
    });
    const max = Math.max(...bks.map(b => b.total), 0);
    const step = Math.max(1, Math.ceil(bks.length / 6));
    const bars = bks.map((b, i) => `<div class="bp-u-col" title="${esc(`${b.tip}  ${money(b.total)}${b.parts.filter(p => p[1]).map(p => `\n${nameOf(p[0])} ${money(p[1])}`).join('')}`)}">`
      + `<div class="bp-u-stack">${b.parts.filter(p => p[1] > 0).map(([m, v]) => `<i class="${color(m)}" style="height:${max ? (v / max * 100).toFixed(2) : 0}%"></i>`).join('')}</div>`
      + `<span class="bp-u-x">${i % step === 0 || i === bks.length - 1 ? esc(b.label) : ''}</span></div>`).join('');

    // by model
    const byModel = models.map(m => {
      const rs = rows.filter(r => bare(r.model) === m);
      const inp = sum(rs, r => r.input + r.cache_write), hit = sum(rs, r => r.cache_read);
      return `<tr><td><span class="bp-u-dot ${color(m)}"></span>${esc(nameOf(m))}</td><td class="n">${count(turns(rs))}</td>`
        + `<td class="n">${fmtTok(inp)}</td><td class="n">${fmtTok(hit)}</td><td class="n">${fmtTok(sum(rs, r => r.output))}</td>`
        + `<td class="n">${inp + hit ? `${Math.round(hit / (inp + hit) * 100)}%` : '—'}</td><td class="n"><b>${esc(money(sum(rs, r => r.cost)))}</b></td></tr>`;
    }).join('');

    // the price list (per million tokens): off-peak, and peak at the multiplier
    const mult = Number(data.peak?.multiplier || 1);
    const pp = (v) => v == null ? '—' : `${esc(money(v))}${mult > 1 ? `<span class="muted"> / ${esc(money(v * mult))}</span>` : ''}`;
    const prices = data.models.map(m => `<tr><td>${esc(m.name)}<div class="tiny muted mono">${esc(m.model)}</div></td>`
      + `<td class="n">${pp(m.price?.cache_read ?? m.price?.input)}</td><td class="n">${pp(m.price?.input)}</td><td class="n">${pp(m.price?.output)}</td>`
      + `<td>${m.images === false ? '<span class="pill">不能看图</span>' : '<span class="pill ok">能看图</span>'}</td></tr>`).join('');

    const fromOpts = [['', '全部模型'], ...models.map(m => [m, nameOf(m)])];
    el.innerHTML = `
      <div class="stat-grid bp-u-top">
        <div class="stat glass"><div class="stat-label">充值余额${low ? ' <span class="pill err">不足</span>' : ''}</div>
          <div class="stat-value${low ? ' err-text' : ''}">${esc(bal || '—')}</div>
          ${topUpUrl ? `<div class="stat-foot"><a class="btn-ghost btn-small" href="${esc(topUpUrl)}" target="_blank" rel="noopener">去充值</a></div>` : ''}</div>
        <div class="stat glass"><div class="stat-label">累计消费金额</div><div class="stat-value">${esc(money(data.lifetime_cost))}</div>
          <div class="stat-foot"><span class="stat-sub">bridge 记下的全部回合，按 API 价格估算</span></div></div>
      </div>
      <div class="bp-u-bar">
        <div class="seg" role="group" aria-label="时间范围">${RANGES.map(([d, t]) => `<button type="button" data-days="${d}"${d === days ? ' class="active" aria-pressed="true"' : ' aria-pressed="false"'}>${t}</button>`).join('')}</div>
        <span class="tiny muted">时间按你设备的时区显示</span>
      </div>
      <div class="stat-grid bp-u-sum">
        <div class="stat glass"><div class="stat-label">消费金额</div><div class="stat-value">${esc(money(total))}</div></div>
        <div class="stat glass"><div class="stat-label">回合数</div><div class="stat-value">${count(turns(rows))}<span class="stat-unit">次</span></div>
          <div class="stat-foot"><span class="stat-sub">一次回答算一次（DeepSeek 平台按 API 请求计，会更多）</span></div></div>
        <div class="stat glass"><div class="stat-label">Tokens</div><div class="stat-value">${count(sum(rows, tokens))}</div></div>
      </div>
      <section class="glass panel">
        <div class="bp-u-head"><h2 class="panel-title">消费金额（${esc(cur())}） ${esc(money(total))}</h2>
          <div class="bp-u-legend">${models.map(m => `<span><span class="bp-u-dot ${color(m)}"></span>${esc(nameOf(m))}</span>`).join('')}</div></div>
        ${rows.length ? `<div class="bp-u-chart" style="--cols:${bks.length}"><div class="bp-u-ymax tiny muted">${esc(money(max))}</div>${bars}</div>`
          : '<p class="muted small">这段时间没有用量。</p>'}
      </section>
      ${rows.length ? `<section class="glass panel"><h2 class="panel-title">按模型</h2>
        <div class="table-wrap"><table class="tbl bp-u-table"><thead><tr><th>模型</th><th class="n">回合</th><th class="n">输入（未命中）</th><th class="n">缓存命中</th><th class="n">输出</th><th class="n">命中率</th><th class="n">金额</th></tr></thead>
        <tbody>${byModel}</tbody></table></div></section>` : ''}
      <section class="glass panel"><h2 class="panel-title">模型定价</h2>
        <p class="muted small bp-lead">每百万 tokens，${esc(cur())}${mult > 1 ? `；斜杠后是高峰价。${esc(peakText(data.peak))}` : ''}。以官网当期价格为准。</p>
        <div class="table-wrap"><table class="tbl bp-u-table"><thead><tr><th>模型</th><th class="n">输入（缓存命中）</th><th class="n">输入（未命中）</th><th class="n">输出</th><th>图片</th></tr></thead>
        <tbody>${prices}</tbody></table></div></section>
      <section class="glass panel" id="bp-u-sim"><h2 class="panel-title">换个模型要花多少</h2>
        <p class="muted small bp-lead">把这段时间里选中模型的回合，按同样的 token 数、同样的高峰 / 空闲时段，换成另一个模型的价格重算。只比较价格，换模型后实际用到的 token 数也会不同。</p>
        <div class="bp-u-simform">
          <label class="form-row"><span>这些回合</span><select class="field" data-sim="from">${fromOpts.map(([v, t]) => `<option value="${esc(v)}"${v === simFrom ? ' selected' : ''}>${esc(t)}</option>`).join('')}</select></label>
          <span class="bp-u-arrow" aria-hidden="true">→</span>
          <label class="form-row"><span>全部换成</span><select class="field" data-sim="to">${data.models.map(m => `<option value="${esc(m.model)}"${m.model === simTo ? ' selected' : ''}>${esc(m.name)}</option>`).join('')}</select></label>
        </div>
        <div class="bp-u-simout" aria-live="polite"></div>
      </section>`;
    renderSim();
  }

  function renderSim() {
    const out = el.querySelector('.bp-u-simout');
    if (!out || !data) return;
    if (!sim) { out.innerHTML = data.rows.length ? '<p class="muted small">计算中…</p>' : '<p class="muted small">这段时间没有用量，没什么可比的。</p>'; return; }
    const pick = (rows) => rows.filter(r => !simFrom || bare(r.model) === bare(simFrom));
    const actual = sum(pick(data.rows), r => r.cost), what = sum(pick(sim), r => r.sim_cost);
    const n = turns(pick(data.rows));
    if (!n) { out.innerHTML = '<p class="muted small">这段时间没有这个模型的回合。</p>'; return; }
    const diff = what - actual, pct = actual > 0 ? Math.round(diff / actual * 100) : null;
    const cls = diff > 0 ? 'err' : diff < 0 ? 'ok' : '';
    out.innerHTML = `<div class="stat-grid bp-u-simres">
        <div class="stat inner"><div class="stat-label">实际（${esc(simFrom ? nameOf(simFrom) : '全部模型')}，${count(n)} 个回合）</div><div class="stat-value">${esc(money(actual))}</div></div>
        <div class="stat inner"><div class="stat-label">换成 ${esc(nameOf(simTo))}</div><div class="stat-value">${esc(money(what))}</div>
          <div class="stat-foot"><span class="stat-trend ${diff > 0 ? 'up' : diff < 0 ? 'down' : 'flat'} ${cls}">${diff >= 0 ? '+' : '−'}${esc(money(Math.abs(diff)))}${pct == null ? '' : `（${pct >= 0 ? '+' : ''}${pct}%）`}</span></div></div>
      </div>`;
  }

  el.addEventListener('click', (e) => {
    const b = e.target.closest('[data-days]');
    if (!b || Number(b.dataset.days) === days) return;
    days = Number(b.dataset.days); load();
  });
  el.addEventListener('change', (e) => {
    const s = e.target.closest('[data-sim]');
    if (!s) return;
    if (s.dataset.sim === 'from') { simFrom = s.value; renderSim(); return; }
    simTo = s.value; sim = null; renderSim(); runSim();
  });
  load();
  return { reload: load };
}
