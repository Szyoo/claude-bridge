// Reference chat UI for claude-bridge. Framework-free; all styling via bridge-widget.css (--bridge-* tokens).
//
// Exports, roughly in layers a host can pick from:
//   pure helpers   describeTool, sessionSummary, splitTurn, DEFAULT_PREFS, loadPrefs, savePrefs
//   renderers      renderTurn, renderSteps, renderContextPanel, renderSessionPanel, renderPrefsPanel, applyPrefs, placePopover
//   full widget    mountBridgeWidget(el, client, opts) -> { destroy(), openThread(id), refresh() }
// Renderers emit `bridge-*` structural class names; a host either loads bridge-widget.css or skins those classes itself.

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function defaultMarkdown(text) {
  const safe = esc(text || '');
  if (globalThis.marked?.parse) return globalThis.marked.parse(safe, { breaks: true, gfm: true });
  return safe.replace(/\n/g, '<br>');
}

const CONTEXT_WINDOWS = { default: 200000 };
function contextWindow(model, table, used = 0) {
  if (model && /\[1m\]/i.test(model)) return 1_000_000;
  if (model) for (const [k, v] of Object.entries(table)) if (k !== 'default' && model.includes(k)) return v;
  // 窗口大小 CLI 不报；已用量超过 200k 说明这个模型是 1M 窗口
  return used > table.default ? 1_000_000 : table.default;
}

export function describeTool(ev) {
  const inp = ev.input || {};
  if (ev.name === 'Bash') return inp.command || '';
  if (['Read', 'Glob', 'Grep', 'Edit', 'Write'].includes(ev.name)) return inp.file_path || inp.pattern || inp.path || '';
  const s = JSON.stringify(inp);
  return s.length > 120 ? s.slice(0, 120) + '…' : s;
}

function fmtTokens(n) {
  if (n == null) return '–';
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(n % 1_000_000 === 0 ? 0 : 1) + 'M';
  return n >= 1000 ? (n / 1000).toFixed(n >= 100000 ? 0 : 1) + 'k' : String(n);
}

function fmtReset(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

// Server timestamps are UTC 'YYYY-MM-DD HH:MM:SS'; show local HH:MM, with the date when it is not today.
export function fmtTime(utc, now = new Date()) {
  if (!utc) return '';
  const d = new Date(String(utc).replace(' ', 'T') + (/Z$|[+-]\d\d:?\d\d$/.test(utc) ? '' : 'Z'));
  if (Number.isNaN(d.getTime())) return '';
  const hm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const sameDay = d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  return sameDay ? hm : `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
}

// 「昨天 / 3 天前」式的相对日期，给对话列表用
export function fmtRelative(utc, now = new Date()) {
  if (!utc) return '';
  const d = new Date(String(utc).replace(' ', 'T') + 'Z');
  if (Number.isNaN(d.getTime())) return '';
  const day = (x) => Math.floor((x - x.getTimezoneOffset() * 60000) / 86400000);
  const diff = day(now) - day(d);
  if (diff <= 0) return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (diff === 1) return '昨天';
  if (diff < 7) return `${diff} 天前`;
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

export const CONTEXT_HELP = '当前会话上下文 = 每次请求送给模型的全部内容：系统提示、工具定义、对话历史、工具输出。'
  + '越大每次回答越慢越贵；接近上限时 Claude Code 会自动压缩历史（保留摘要），也可手动压缩；新开对话则归零。';

// Builds the session line from a thread's events (+ the latest /context report if the server has one).
// Every part carries `title` explaining what the number is; `key` lets a host attach behaviour (e.g. open the context panel).
export function sessionSummary(messages, { contextWindows = CONTEXT_WINDOWS, context = null } = {}) {
  let init = null, usage = null, rate = null;
  for (const m of messages) for (const ev of m.events || []) {
    if (ev.type === 'init') init = ev.data;
    else if (ev.type === 'usage') usage = ev.data;
    else if (ev.type === 'rate_limit') rate = ev.data;
  }
  const model = context?.model || usage?.model || init?.model || '';
  const parts = [];
  if (model) parts.push({ key: 'model', label: model.replace(/^claude-/, ''), title: '上一条回答使用的模型' });
  // context occupancy: prefer the exact /context report, else the last request's input+cache size
  const used = context?.used ?? usage?.context_tokens ?? null;
  let pct = null;
  if (used) {
    const win = context?.window || contextWindow(model, contextWindows, used);
    pct = Math.min(100, Math.round(used / win * 100));
    const thr = context?.autocompact_pct;
    parts.push({
      key: 'context', label: `当前会话上下文 ${fmtTokens(used)}/${fmtTokens(win)} · ${pct}%`, bar: pct, clickable: true,
      title: `${CONTEXT_HELP}${thr ? ` 自动压缩阈值约 ${thr}%。` : ''} 点开看构成。`,
    });
  }
  if (usage) {
    if (usage.num_turns != null) parts.push({ key: 'turns', label: `本轮 ${usage.num_turns} 次调用`, title: '上一条回答里模型被调用的次数：每用一次工具就多一次' });
    const inp = usage.context_tokens || null;
    if (inp || usage.output_tokens != null) {
      parts.push({
        key: 'io', label: `本轮输入 ${inp ? fmtTokens(inp) : '–'} · 输出 ${fmtTokens(usage.output_tokens)}`,
        title: '输入 = 上一条回答时送给模型的 tokens（就是当时的上下文，大部分命中缓存）；输出 = 模型生成的 tokens',
      });
    }
  }
  const win = (w, name) => {
    if (w?.utilization == null) return;
    const p = Math.round(w.utilization * 100);
    parts.push({ key: name, label: `${name} 已用 ${p}%`, bar: p, title: `订阅的${name}滚动窗口已用 ${p}%，${fmtReset(w.resets_at)} 重置（来自 claude 命令行自己报告的限额）` });
  };
  win(rate?.five_hour, '5 小时额度');
  win(rate?.seven_day, '7 天额度');
  return { model, usage, rate, context, parts, pct };
}

const CATEGORY_ZH = {
  'System prompt': '系统提示', 'System tools': '系统工具', 'MCP tools': 'MCP 工具', 'MCP tools (deferred)': 'MCP 工具（按需加载，不计入）',
  'System tools (deferred)': '系统工具（按需加载，不计入）', Skills: '技能说明', 'Memory files': '记忆文件', Messages: '对话消息（历史 + 工具输出）',
  'Autocompact buffer': '自动压缩预留', 'Free space': '剩余空间',
};
const CATEGORY_COLORS = ['#3b6df2', '#e0703a', '#2e9b5d', '#d4a72c', '#8b8b8b', '#6f6f6f', '#3b6df2', '#555555'];

// Elapsed m:ss since a server UTC timestamp ('YYYY-MM-DD HH:MM:SS').
export function fmtElapsed(utc, now = Date.now()) {
  if (!utc) return '0:00';
  const t = Date.parse(String(utc).replace(' ', 'T') + (/Z$|[+-]\d\d:?\d\d$/.test(utc) ? '' : 'Z'));
  if (Number.isNaN(t)) return '';
  const sec = Math.max(0, Math.round((now - t) / 1000));
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`;
}
// Refreshes every elapsed counter under `root` (call once a second while a session job is active).
export function tickJobs(root = document) {
  for (const el of root.querySelectorAll('[data-job-since]')) el.textContent = fmtElapsed(el.dataset.jobSince);
}
const jobActive = (j) => !!j && (j.status === 'queued' || j.status === 'running');

// Status line for a queued / running `/compact` or `/context` job (the server pushes `job` frames as it moves).
export function jobBannerHtml(job) {
  if (!jobActive(job)) return '';
  const queued = job.status === 'queued';
  if (job.kind === 'compact') {
    const since = job.started_at || '';
    return `<div class="bridge-job"><i class="bridge-spin"></i><div><b>${queued ? '压缩任务排队中' : '正在压缩'}</b>`
      + (queued ? '' : ` · 已进行 <span data-job-since="${esc(since)}">${fmtElapsed(since)}</span>`)
      + `<div class="bridge-job-hint">${queued ? '等 helper 接手；helper 离线时会一直排队。'
        : 'Claude 在通读这段对话的全部历史并写成摘要，对话越长越久（通常 1–3 分钟）。完成后上下文数字会自动更新，不用手动刷新。'}</div></div></div>`;
  }
  return `<div class="bridge-job"><i class="bridge-spin"></i><div><b>${queued ? '刷新构成排队中' : '正在刷新构成…'}</b></div></div>`;
}

// Renders the /context breakdown into `el`. opts: { onRefresh, onCompact, busy, job }
export function renderContextPanel(el, ctx, opts = {}) {
  if (!ctx || ctx.used == null) {
    el.innerHTML = `<div class="bridge-ctx">${jobBannerHtml(opts.job)}<p class="bridge-ctx-help">${esc(CONTEXT_HELP)}</p><p class="bridge-muted bridge-tiny">还没有构成数据：回答一次后自动获取，或点「刷新构成」。</p>`
      + `<div class="bridge-ctx-foot"><span></span><button type="button" class="bridge-btn bridge-tiny" data-ctx="refresh"${opts.busy ? ' disabled' : ''}>刷新构成</button></div></div>`;
  } else {
    const win = ctx.window || 1;
    const counted = (ctx.categories || []).filter(c => !c.deferred && c.name !== 'Free space');
    const segs = counted.map((c, i) => `<i style="width:${Math.max(0.3, (c.tokens || 0) / win * 100)}%;background:${CATEGORY_COLORS[i % CATEGORY_COLORS.length]}" title="${esc(CATEGORY_ZH[c.name] || c.name)} ${fmtTokens(c.tokens)}"></i>`).join('');
    const rows = (ctx.categories || []).map((c) => {
      const counting = !c.deferred && c.name !== 'Free space';
      const dot = counting ? `<i class="dot" style="background:${CATEGORY_COLORS[counted.indexOf(c) % CATEGORY_COLORS.length]}"></i>` : '<i class="dot none"></i>';
      const pct = c.pct == null ? '—' : `${c.pct}%`;
      return `<tr class="${c.deferred ? 'deferred' : ''}"><td>${dot}${esc(CATEGORY_ZH[c.name] || c.name)}</td><td class="n">${fmtTokens(c.tokens)}</td><td class="n">${pct}</td></tr>`;
    }).join('');
    const thr = ctx.autocompact_pct ? `<p class="bridge-ctx-help">用到约 ${ctx.autocompact_pct}% 时 Claude Code 会自动压缩历史（保留摘要）；「压缩会话」现在就做同样的事，需要模型读一遍历史，会花一次调用。</p>` : '';
    el.innerHTML = `<div class="bridge-ctx">${jobBannerHtml(opts.job)}
      <p class="bridge-ctx-help">${esc(CONTEXT_HELP)}</p>
      <div class="bridge-ctx-head"><b>${fmtTokens(ctx.used)} / ${fmtTokens(win)}（${ctx.pct ?? Math.round(ctx.used / win * 100)}%）</b><span class="bridge-muted bridge-tiny">${esc(ctx.model || '')}</span></div>
      <div class="bridge-ctx-bar">${segs}</div>
      <table class="bridge-ctx-table"><tbody>${rows}</tbody></table>
      ${thr}
      <div class="bridge-ctx-foot"><span class="bridge-muted bridge-tiny">数据来自 claude /context${ctx.at ? ` · 更新于 ${esc(ctx.at)} UTC` : ''}</span>
        <button type="button" class="bridge-btn bridge-tiny" data-ctx="refresh"${opts.busy ? ' disabled' : ''}>刷新构成</button>
        <button type="button" class="bridge-btn bridge-tiny bridge-btn-danger" data-ctx="compact"${opts.busy ? ' disabled' : ''}>压缩会话</button></div>
    </div>`;
  }
  el.querySelector('[data-ctx="refresh"]')?.addEventListener('click', () => opts.onRefresh?.());
  el.querySelector('[data-ctx="compact"]')?.addEventListener('click', () => { if (confirm('让 Claude 把这段对话的历史压缩成摘要？细节会丢失，但上下文会明显变小。')) opts.onCompact?.(); });
}

// Bottom popover body: the session numbers (model / turns / io / quotas) above the /context breakdown.
// `summary` is a sessionSummary() result; ctxOpts go to renderContextPanel.
export function renderSessionPanel(el, summary, ctxOpts = {}) {
  const rows = (summary?.parts || []).filter(p => p.key !== 'context').map(p =>
    `<div class="bridge-sess-row" title="${esc(p.title || '')}"><span>${esc(p.label)}</span>${p.bar != null ? `<span class="bridge-meter${p.bar >= 80 ? ' hot' : ''}"><i style="width:${p.bar}%"></i></span>` : ''}</div>`).join('');
  el.innerHTML = `<div class="bridge-sess">${rows || '<div class="bridge-sess-row bridge-muted">还没有会话数据：回答一次后出现</div>'}</div><div class="bridge-sess-ctx"></div>`;
  renderContextPanel(el.querySelector('.bridge-sess-ctx'), summary?.context, ctxOpts);
}

// <option>s for a model <select>. Entries may carry `group` ("跟随 CLI 最新" / "固定版本" when the worker reported
// what its CLI supports) → <optgroup>. A saved value the list no longer has is kept, flagged, and selected.
export function modelOptionsHtml(models, current = '') {
  current = current || '';
  let html = '', group = null;
  for (const m of models || []) {
    const g = m.group || null;
    if (g !== group) { if (group) html += '</optgroup>'; if (g) html += `<optgroup label="${esc(g)}">`; group = g; }
    html += `<option value="${esc(m.id)}"${m.id === current ? ' selected' : ''}>${esc(m.label)}</option>`;
  }
  if (group) html += '</optgroup>';
  if (current && !(models || []).some(m => m.id === current)) html += `<option value="${esc(current)}" selected>${esc(current)}（helper 的 CLI 不认）</option>`;
  return html;
}

export function describeCompact(d) {
  return `已${d.trigger === 'auto' ? '自动' : '手动'}压缩会话：${fmtTokens(d.pre_tokens)} → ${fmtTokens(d.post_tokens)}`;
}

// ============================================================
// Turn splitting: interleave tool groups with the answer text in real order.
// Each tool_use / thinking event carries `at` = answer chars streamed when it happened; we cut the
// markdown at the next paragraph boundary (`\n\n`, never inside a fenced code block) and put the
// group there. Adjacent groups with no text between merge (→ "执行了 N 条命令").
// ============================================================

function fenceRanges(text) {
  const ranges = []; const re = /^ {0,3}(`{3,}|~{3,})/gm; let open = null, m;
  while ((m = re.exec(text))) {
    if (!open) open = { pos: m.index, fence: m[1] };
    else if (m[1][0] === open.fence[0] && m[1].length >= open.fence.length) { ranges.push([open.pos, m.index + m[0].length]); open = null; }
  }
  if (open) ranges.push([open.pos, text.length]);
  return ranges;
}
const inRanges = (ranges, i) => ranges.some(([a, b]) => i >= a && i < b);

// Python counted code points; JS indexes UTF-16 units. Only differs when astral chars (emoji) precede `cp`.
function cpIndex(text, cp) {
  let i = 0, n = 0;
  while (i < text.length && n < cp) { i += text.codePointAt(i) > 0xffff ? 2 : 1; n++; }
  return i;
}

// → [{kind:'text', text}, {kind:'steps', items:[…]}, …]; items: {kind:'tool'|'thinking'|'trace'|'note', …}
export function splitTurn(content, events) {
  content = content || '';
  const items = []; const byTool = new Map(); const END = Infinity;
  for (const ev of events || []) {
    const d = ev.data || {};
    switch (ev.type) {
      case 'tool_use': { const it = { kind: 'tool', id: d.id || '', name: d.name || '?', input: d.input || {}, result: null, at: d.at ?? 0 }; items.push(it); if (d.id) byTool.set(d.id, it); break; }
      case 'tool_result': { const it = byTool.get(d.tool_use_id); if (it) it.result = d; break; }
      case 'thinking': items.push({ kind: 'thinking', text: d.text || '', truncated: !!d.truncated, at: d.at ?? 0 }); break;
      case 'compact': items.push({ kind: 'note', sub: 'compact', text: describeCompact(d), at: 0 }); break;
      case 'error': items.push({ kind: 'note', sub: 'error', text: d.message || '出错', at: END }); break;
      case 'status':
        if (d.phase === 'legacy_trace') items.push({ kind: 'trace', text: d.text || '', at: END });
        else if (d.phase === 'cancel_requested') items.push({ kind: 'note', sub: 'cancel', text: '已请求停止…', at: END });
        break;
      default: break;
    }
  }
  if (!items.length) return content ? [{ kind: 'text', text: content }] : [];
  items.sort((a, b) => a.at - b.at);
  const fences = fenceRanges(content);
  const astral = /[\uD800-\uDBFF]/.test(content);
  const segs = []; let pos = 0;
  const pushText = (to) => { if (to > pos) { segs.push({ kind: 'text', text: content.slice(pos, to) }); pos = to; } };
  for (const it of items) {
    let cut;
    if (it.at === END) cut = content.length;
    else {
      const i = Math.min(astral ? cpIndex(content, it.at) : it.at, content.length);
      if (i <= pos) cut = pos;
      else if (i >= content.length) cut = content.length;
      else if (content.slice(i - 2, i) === '\n\n' && !inRanges(fences, i)) cut = i;
      else {
        let j = content.indexOf('\n\n', i);
        while (j !== -1 && inRanges(fences, j)) j = content.indexOf('\n\n', j + 2);
        cut = j === -1 ? content.length : j + 2;
      }
    }
    pushText(cut);
    const last = segs[segs.length - 1];
    if (last?.kind === 'steps') last.items.push(it); else segs.push({ kind: 'steps', items: [it] });
  }
  pushText(content.length);
  return segs;
}

// ---------- step rows (collapsed grey lines) ----------

const firstLine = (t, n = 72) => { const s = (t || '').trim().split('\n')[0]; return s.length > n ? s.slice(0, n) + '…' : s; };

function toolRow(it, o) {
  const running = !it.result && o.streaming;
  const err = !!it.result?.is_error;
  const state = running ? '<i class="bridge-run"></i>' : err ? '<span class="bridge-step-state err">失败</span>' : it.result ? '' : '<span class="bridge-step-state">无结果</span>';
  const out = it.result ? (it.result.content || '(无输出)') + (it.result.truncated ? '\n…（输出已截断）' : '') : running ? '运行中…' : '';
  const cmd = it.name === 'Bash' ? `$ ${it.input.command || ''}` : JSON.stringify(it.input, null, 1);
  return `<details class="bridge-step tool${running ? ' running' : ''}${err ? ' error' : ''}" data-k="${esc(it.id)}"${o.open ? ' open' : ''}>`
    + `<summary><i class="bridge-caret"></i><span class="bridge-step-name">${esc(it.name)}</span><span class="bridge-step-desc">${esc(describeTool(it))}</span>${state}</summary>`
    + `<div class="bridge-term"><pre class="cmd">${esc(cmd)}</pre>${out ? `<pre class="out">${esc(out)}</pre>` : ''}</div></details>`;
}

function toolGroup(run, o, key) {
  const done = run.filter(t => t.result).length, n = run.length;
  const running = o.streaming && done < n;
  const err = run.some(t => t.result?.is_error);
  const allBash = run.every(t => t.name === 'Bash');
  const label = running ? `执行中 · ${done}/${n}` : allBash ? `执行了 ${n} 条命令` : `${n} 次工具调用`;
  const state = running ? '<i class="bridge-run"></i>' : err ? '<span class="bridge-step-state err">有失败</span>' : '';
  return `<details class="bridge-step group${running ? ' running' : ''}${err ? ' error' : ''}" data-k="${esc(key)}"${o.open ? ' open' : ''}>`
    + `<summary><i class="bridge-caret"></i><span class="bridge-step-name">${label}</span>${state}</summary>`
    + `<div class="bridge-step-list">${run.map(t => toolRow(t, o)).join('')}</div></details>`;
}

// items → HTML. opts: { streaming, open (expand by default), key (stable prefix for data-k) }
export function stepsHtml(items, opts = {}) {
  const o = { streaming: false, open: false, key: 's', ...opts };
  const out = []; let i = 0;
  while (i < items.length) {
    const it = items[i];
    if (it.kind === 'tool') {
      let j = i; while (j < items.length && items[j].kind === 'tool') j++;
      const run = items.slice(i, j);
      out.push(run.length >= 2 ? toolGroup(run, o, `g:${run[0].id || `${o.key}-${i}`}`) : toolRow(run[0], o));
      i = j; continue;
    }
    if (it.kind === 'thinking') {
      out.push(`<details class="bridge-step thinking" data-k="${o.key}-t${i}"${o.open ? ' open' : ''}><summary><i class="bridge-caret"></i><span class="bridge-step-name">思考过程</span><span class="bridge-step-desc">${esc(firstLine(it.text))}</span></summary>`
        + `<div class="bridge-step-body">${esc(it.text)}${it.truncated ? '\n…（已截断）' : ''}</div></details>`);
    } else if (it.kind === 'trace') {
      out.push(`<details class="bridge-step trace" data-k="${o.key}-r${i}"><summary><i class="bridge-caret"></i><span class="bridge-step-name">执行轨迹</span></summary><div class="bridge-term"><pre class="out">${esc(it.text)}</pre></div></details>`);
    } else if (it.kind === 'note') {
      out.push(`<div class="bridge-note${it.sub === 'error' ? ' error' : ''}">${esc(it.text)}</div>`);
    }
    i++;
  }
  return out.join('');
}

export function renderSteps(el, items, opts = {}) { el.innerHTML = stepsHtml(items, opts); }

function messageModel(m) {
  let model = '';
  for (const ev of m.events || []) if ((ev.type === 'init' || ev.type === 'usage') && ev.data?.model) model = ev.data.model;
  return model.replace(/^claude-/, '');
}

// Renders one message into `el` (a .bridge-turn). Re-rendering keeps the user's expand/collapse choices.
// opts: { markdown, head (speaker line), timestamps, open (tools expanded by default), fileUrl(id) (image thumbnails), strings }
export function renderTurn(el, m, opts = {}) {
  const o = { markdown: defaultMarkdown, head: true, timestamps: true, open: false, strings: {}, ...opts };
  const S = { me: '我', assistant: 'Claude', waitingHelper: '等待 helper 接单…', thinking: 'Claude 正在思考…', ...o.strings };
  el.className = `bridge-turn ${m.role} ${m.status || 'done'}`;
  el.dataset.id = m.id;
  if (m.role === 'system') { el.textContent = m.content || ''; return; }
  const opened = new Set(), closed = new Set();
  for (const d of el.querySelectorAll('details[data-k]')) (d.open ? opened : closed).add(d.dataset.k);
  const time = o.timestamps ? fmtTime(m.created_at) : '';
  let head = '';
  if (o.head) {
    const meta = m.role === 'user' ? [time] : [messageModel(m), time];
    head = `<div class="bridge-turn-head"><span class="bridge-who ${m.role}">${esc(m.role === 'user' ? S.me : S.assistant)}</span>`
      + `<span class="bridge-turn-meta">${meta.filter(Boolean).map(esc).join(' · ')}</span></div>`;
  }
  let body;
  if (m.role === 'user') {
    const files = (m.files || []).map((f) => {
      const u = o.fileUrl?.(f.id);
      return u ? `<a href="${esc(u)}" target="_blank" rel="noopener" title="${esc(f.name || '查看原图')}"><img src="${esc(u)}" alt="${esc(f.name || '图片')}" loading="lazy"></a>`
        : '<span class="bridge-file-ph">[图片]</span>';
    }).join('');
    body = (files ? `<div class="bridge-files">${files}</div>` : '') + (m.content ? `<div class="bridge-user-text">${esc(m.content)}</div>` : '');
  }
  else {
    const streaming = m.status === 'pending' || m.status === 'streaming';
    const segs = splitTurn(m.content || '', m.events || []);
    body = segs.map((s, i) => s.kind === 'text'
      ? `<div class="bridge-md">${o.markdown(s.text)}</div>`
      : `<div class="bridge-steps">${stepsHtml(s.items, { streaming, open: o.open, key: `s${i}` })}</div>`).join('');
    if (streaming && !segs.length) body = `<div class="bridge-wait">${esc(m.status === 'pending' ? S.waitingHelper : S.thinking)}</div>`;
  }
  el.innerHTML = head + `<div class="bridge-turn-body">${body}</div>`;
  for (const d of el.querySelectorAll('details[data-k]')) { const k = d.dataset.k; if (opened.has(k)) d.open = true; else if (closed.has(k)) d.open = false; }
}

// ============================================================
// Per-device UI preferences (localStorage). Hosts may extend the object with their own keys.
// ============================================================

export const DEFAULT_PREFS = {
  scale: 1,           // 文字大小倍数 0.85–1.4
  density: 'cozy',    // compact 13px / cozy 14px / roomy 16px（同客户端三档）
  width: 'cozy',      // 正文宽度 cozy(68ch) / full
  sendKey: 'enter',   // enter = Enter 发送、Shift+Enter 换行；mod = ⌘/Ctrl+Enter 发送
  timestamps: true,
  toolsOpen: false,   // 工具调用默认展开
  codeWrap: false,    // 代码块自动换行
  sidebar: true,      // 桌面端对话列表
  height: 72,         // 聊天区高度 vh
  sideW: null,        // 侧栏宽度 px（拖过才有）
  inputH: null,       // 输入框高度 px（拖过才有；有值就不再自动长高）
};
export const DENSITY_PX = { compact: 13, cozy: 14, roomy: 16 };
const clamp = (v, lo, hi, d) => { v = Number(v); return Number.isFinite(v) ? Math.min(hi, Math.max(lo, v)) : d; };

export function sanitizePrefs(p, defaults = DEFAULT_PREFS) {
  const o = { ...defaults, ...(p && typeof p === 'object' ? p : {}) };
  o.scale = clamp(o.scale, 0.85, 1.4, defaults.scale);
  o.height = clamp(o.height, 50, 95, defaults.height);
  if (!(o.density in DENSITY_PX)) o.density = defaults.density;
  if (!['cozy', 'full'].includes(o.width)) o.width = defaults.width;
  if (!['enter', 'mod'].includes(o.sendKey)) o.sendKey = defaults.sendKey;
  o.sideW = o.sideW == null ? null : clamp(o.sideW, 180, 480, null);
  o.inputH = o.inputH == null ? null : clamp(o.inputH, 40, 600, null);
  for (const k of ['timestamps', 'toolsOpen', 'codeWrap', 'sidebar']) o[k] = !!o[k];
  return o;
}
export function loadPrefs(key = 'bridge_ui', defaults = DEFAULT_PREFS) {
  let raw = null;
  try { raw = localStorage.getItem(key); } catch { /* private mode / blocked */ }
  let p = {};
  if (raw) { try { p = JSON.parse(raw); } catch { p = {}; } }
  return sanitizePrefs(p, defaults);
}
export function savePrefs(prefs, key = 'bridge_ui') { try { localStorage.setItem(key, JSON.stringify(prefs)); } catch { /* ignore */ } }

// Pushes prefs onto the chat root as CSS variables + classes; the stylesheet does the rest.
export function applyPrefs(root, prefs) {
  root.style.setProperty('--chat-scale', String(prefs.scale));
  root.style.setProperty('--chat-density-px', `${DENSITY_PX[prefs.density] || 14}px`);
  root.style.setProperty('--chat-height', `${prefs.height}vh`);
  if (prefs.sideW) root.style.setProperty('--chat-side-w', `${prefs.sideW}px`); else root.style.removeProperty('--chat-side-w');
  root.dataset.density = prefs.density;
  root.dataset.width = prefs.width;
  root.classList.toggle('tools-open', !!prefs.toolsOpen);
  root.classList.toggle('code-wrap', !!prefs.codeWrap);
  root.classList.toggle('no-time', !prefs.timestamps);
  root.classList.toggle('side-hidden', !prefs.sidebar);
}

const IS_MAC = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || '');
export const MOD_KEY = IS_MAC ? '⌘' : 'Ctrl';
export const sendHint = (prefs) => prefs.sendKey === 'mod' ? `${MOD_KEY}+Enter 发送，Enter 换行` : 'Enter 发送，Shift+Enter 换行';
// true when this keydown should send under the current preference
export function isSendKey(e, prefs) {
  if (e.key !== 'Enter' || e.isComposing) return false;
  return prefs.sendKey === 'mod' ? !!(e.metaKey || e.ctrlKey) : !(e.shiftKey || e.metaKey || e.ctrlKey || e.altKey);
}

export const PREF_FIELDS = [
  { key: 'scale', type: 'range', label: '文字大小', min: 0.85, max: 1.4, step: 0.05, fmt: v => `${Math.round(v * 100)}%` },
  { key: 'density', type: 'chips', label: '消息密度', hint: '行距、间距和控件高度一起变', options: [['compact', '紧凑'], ['cozy', '舒适'], ['roomy', '宽松']] },
  { key: 'width', type: 'chips', label: '正文宽度', options: [['cozy', '舒适'], ['full', '全宽']] },
  { key: 'sendKey', type: 'chips', label: '发送键', options: [['enter', 'Enter'], ['mod', `${MOD_KEY} + Enter`]] },
  { key: 'timestamps', type: 'switch', label: '显示时间' },
  { key: 'toolsOpen', type: 'switch', label: '工具调用默认展开' },
  { key: 'codeWrap', type: 'switch', label: '代码块自动换行' },
  { key: 'sidebar', type: 'switch', label: '对话列表', hint: '桌面端左侧栏；手机上用 ☰', desktop: true },
  { key: 'height', type: 'range', label: '聊天区高度', min: 50, max: 95, step: 1, fmt: v => `${v}vh` },
];
const PREF_CLASSES = { root: 'bridge-prefs', col: 'bridge-pref-col', row: 'bridge-pref-row', label: 'bridge-pref-label', hint: 'bridge-pref-hint', value: 'bridge-pref-value', chips: 'bridge-chips', chip: 'bridge-chip', chipActive: 'active', switch: 'bridge-switch', range: 'bridge-range' };

// Renders the preference controls into `el` and mutates `prefs` in place as the user changes them.
// opts: { onChange(prefs, key), classes (override structural class names, e.g. with a design system's), fields (keys to show) }
export function renderPrefsPanel(el, prefs, opts = {}) {
  const C = { ...PREF_CLASSES, ...(opts.classes || {}) };
  const fields = PREF_FIELDS.filter(f => !opts.fields || opts.fields.includes(f.key));
  const label = (f) => `<span class="${C.label}">${esc(f.label)}${f.hint ? `<span class="${C.hint}">${esc(f.hint)}</span>` : ''}</span>`;
  el.innerHTML = `<div class="${C.root}">` + fields.map((f) => {
    if (f.type === 'range') return `<div class="${C.col}" data-field="${f.key}"><div class="${C.row}">${label(f)}<span class="${C.value}">${esc(f.fmt(prefs[f.key]))}</span></div>`
      + `<input type="range" class="${C.range}" data-pref="${f.key}" min="${f.min}" max="${f.max}" step="${f.step}" value="${prefs[f.key]}" /></div>`;
    if (f.type === 'chips') return `<div class="${C.row}" data-field="${f.key}">${label(f)}<div class="${C.chips}">${f.options.map(([v, t]) =>
      `<button type="button" class="${C.chip}${prefs[f.key] === v ? ` ${C.chipActive}` : ''}" data-pref="${f.key}" data-val="${esc(v)}">${esc(t)}</button>`).join('')}</div></div>`;
    return `<label class="${C.row}" data-field="${f.key}">${label(f)}<input type="checkbox" class="${C.switch}" data-pref="${f.key}"${prefs[f.key] ? ' checked' : ''} /></label>`;
  }).join('') + '</div>';
  const changed = (key) => opts.onChange?.(prefs, key);
  el.addEventListener('input', (e) => {
    const t = e.target; if (t.type !== 'range' || !t.dataset.pref) return;
    const f = fields.find(x => x.key === t.dataset.pref); prefs[f.key] = Number(t.value);
    const v = el.querySelector(`[data-field="${f.key}"] .${C.value.split(' ')[0]}`); if (v) v.textContent = f.fmt(prefs[f.key]);
    changed(f.key);
  });
  el.addEventListener('change', (e) => { const t = e.target; if (t.type === 'checkbox' && t.dataset.pref) { prefs[t.dataset.pref] = t.checked; changed(t.dataset.pref); } });
  el.addEventListener('click', (e) => {
    const b = e.target.closest('[data-pref][data-val]'); if (!b) return;
    prefs[b.dataset.pref] = b.dataset.val;
    for (const x of el.querySelectorAll(`[data-pref="${b.dataset.pref}"][data-val]`)) x.classList.toggle(C.chipActive, x === b);
    changed(b.dataset.pref);
  });
}

// Fixed-position popover next to `anchor` (above it when there is no room below). Narrow screens: the
// stylesheet turns .bridge-pop into a bottom sheet and ignores these coordinates.
export function placePopover(anchor, pop, { gap = 6, align = 'end' } = {}) {
  pop.hidden = false;
  const r = anchor.getBoundingClientRect();
  const w = pop.offsetWidth, h = pop.offsetHeight;
  let left = align === 'end' ? r.right - w : r.left;
  left = Math.max(8, Math.min(left, innerWidth - w - 8));
  const below = r.bottom + gap + h <= innerHeight - 8;
  const top = Math.min(innerHeight - h - 8, below ? r.bottom + gap : Math.max(8, r.top - gap - h));
  pop.style.left = `${left}px`; pop.style.top = `${top}px`;
  pop.classList.toggle('above', !below);
}

// Drag helper for resizers: onMove(dx, dy) per pointer move, onEnd() once. Returns a disposer.
export function attachDrag(handle, onMove, onEnd) {
  let sx = 0, sy = 0, active = false;
  const move = (e) => { if (active) { onMove(e.clientX - sx, e.clientY - sy); e.preventDefault(); } };
  const up = () => { if (!active) return; active = false; document.body.classList.remove('bridge-dragging'); onEnd?.(); };
  const down = (e) => { if (e.button) return; active = true; sx = e.clientX; sy = e.clientY; handle.setPointerCapture?.(e.pointerId); document.body.classList.add('bridge-dragging'); e.preventDefault(); };
  handle.addEventListener('pointerdown', down); handle.addEventListener('pointermove', move);
  handle.addEventListener('pointerup', up); handle.addEventListener('pointercancel', up);
  return () => { handle.removeEventListener('pointerdown', down); handle.removeEventListener('pointermove', move); handle.removeEventListener('pointerup', up); handle.removeEventListener('pointercancel', up); };
}

// ============================================================
// Images. Screenshots go up as-is (PNG keeps small text crisp; lossy JPEG smears it). Two size rules only:
// - long edge ≤ 2000px: once a request carries more than 20 images the API rejects any image over 2000px, and the
//   CLI re-sends every image in the session history on each turn;
// - very long screenshots (aspect > 2.4) are not shrunk (the text would become unreadable) but cut into
//   ≤2000px pieces that overlap a little and are sent in order.
// ============================================================

export const IMAGE_LIMITS = { max: 2000, maxBytes: 7 * 1024 * 1024, ratio: 2.4, overlap: 60, maxTiles: 8, maxFiles: 10 };
const PASSTHROUGH = ['image/png', 'image/jpeg', 'image/webp', 'image/gif'];

// Pure: what to do with an image of this size/type → {mode:'asis'} | {mode:'scale', w, h, out} |
// {mode:'tiles', out, tiles:[{sx, sy, sw, sh, dw, dh}]} (source rect → destination size).
export function planImage({ width: w, height: h, type = '', size = 0 }, limits = {}) {
  const L = { ...IMAGE_LIMITS, ...limits };
  const out = /png|gif|webp/.test(type) ? 'image/png' : 'image/jpeg';   // photos (JPEG / HEIC) stay JPEG
  const long = Math.max(w, h), short = Math.min(w, h);
  if (long <= L.max && size <= L.maxBytes && PASSTHROUGH.includes(type)) return { mode: 'asis' };
  if (long <= L.max || long / short <= L.ratio) {
    const k = Math.min(1, L.max / long);
    return { mode: 'scale', w: Math.max(1, Math.round(w * k)), h: Math.max(1, Math.round(h * k)), out };
  }
  const vertical = h >= w;
  const count = (k) => Math.max(1, Math.ceil((long * k - L.overlap) / (L.max - L.overlap)));
  let k = Math.min(1, L.max / short);                  // keep the short side legible
  if (count(k) > L.maxTiles) k = (L.maxTiles * (L.max - L.overlap) + L.overlap) / long;   // too long even so: shrink to fit
  const len = Math.round(long * k), n = Math.min(L.maxTiles, count(k));
  const across = Math.max(1, Math.round(short * k));
  const tiles = [];
  for (let i = 0; i < n; i++) {
    const d0 = n === 1 ? 0 : Math.round(i * (len - L.max) / (n - 1));   // evenly spread; overlap ≥ L.overlap
    const dl = Math.min(L.max, len - d0);
    tiles.push(vertical ? { sx: 0, sy: d0 / k, sw: w, sh: dl / k, dw: across, dh: dl }
      : { sx: d0 / k, sy: 0, sw: dl / k, sh: h, dw: dl, dh: across });
  }
  return { mode: 'tiles', out, tiles };
}

async function decodeImage(file) {
  if (globalThis.createImageBitmap) {
    try { const b = await createImageBitmap(file); return { src: b, width: b.width, height: b.height, close: () => b.close?.() }; }
    catch { /* e.g. HEIC outside Safari: try an <img> */ }
  }
  const url = URL.createObjectURL(file);
  const im = new Image();
  im.src = url;
  try { await im.decode(); }
  catch { URL.revokeObjectURL(url); throw new Error(`${file.name || '图片'} 解码失败，格式可能不支持`); }
  return { src: im, width: im.naturalWidth, height: im.naturalHeight, close: () => URL.revokeObjectURL(url) };
}

// One ≤2000×2000 canvas at a time (iOS caps canvas area), released right after encoding.
async function drawTo(src, r, type, maxBytes) {
  const c = document.createElement('canvas');
  c.width = r.dw; c.height = r.dh;
  const g = c.getContext('2d');
  g.imageSmoothingQuality = 'high';
  const paint = (bg) => { if (bg) { g.fillStyle = '#fff'; g.fillRect(0, 0, r.dw, r.dh); } g.drawImage(src, r.sx, r.sy, r.sw, r.sh, 0, 0, r.dw, r.dh); };
  const encode = (t, q) => new Promise((res) => c.toBlob(res, t, q));
  paint(type === 'image/jpeg');                       // transparent pixels would turn black in a JPEG
  let blob = await encode(type, 0.92);
  if (blob && blob.size > maxBytes && type !== 'image/jpeg') { paint(true); blob = await encode('image/jpeg', 0.9); }
  c.width = c.height = 0;
  if (!blob) throw new Error('图片处理失败');
  return blob;
}

// Files → [{blob, name}] ready to upload. `name` labels long-screenshot parts ("长截图 2/3").
export async function prepareImages(files, limits = {}) {
  const L = { ...IMAGE_LIMITS, ...limits };
  const out = [];
  for (const file of files) {
    if (!/^image\//.test(file.type) && !/\.(png|jpe?g|gif|webp|heic|heif)$/i.test(file.name || '')) throw new Error(`${file.name || '这个文件'}不是图片`);
    const img = await decodeImage(file);
    try {
      const plan = planImage({ width: img.width, height: img.height, type: file.type, size: file.size }, L);
      if (plan.mode === 'asis') out.push({ blob: file, name: '' });
      else if (plan.mode === 'scale') out.push({ blob: await drawTo(img.src, { sx: 0, sy: 0, sw: img.width, sh: img.height, dw: plan.w, dh: plan.h }, plan.out, L.maxBytes), name: '' });
      else for (const [i, t] of plan.tiles.entries()) out.push({ blob: await drawTo(img.src, t, plan.out, L.maxBytes), name: `长截图 ${i + 1}/${plan.tiles.length}` });
    } finally { img.close(); }
  }
  return out;
}

// Attachment tray for a composer: a button + hidden <input type=file>, paste into the textarea, drop onto
// `dropZone`. Each image is prepared and uploaded right away; the tray shows thumbnails with ✕ / spinner / error.
// Returns { ids(), busy(), count(), clear(), add(files), setEnabled(on), setLimits(l), destroy() }.
export function mountAttachments({ button, input, tray, textarea, dropZone, client, limits = {}, onChange, onError } = {}) {
  let L = { ...IMAGE_LIMITS, ...limits }, items = [], seq = 0, enabled = true;
  const changed = () => onChange?.({ busy: items.some(i => i.status === 'uploading'), count: items.length });
  const render = () => {
    tray.hidden = !items.length;
    tray.innerHTML = items.map(it => `<div class="bridge-att ${it.status}" data-k="${it.key}" title="${esc(it.error || it.name || '')}">`
      + `<img src="${it.url}" alt="">${it.name ? `<span class="bridge-att-name">${esc(it.name.replace('长截图 ', ''))}</span>` : ''}`
      + `${it.status === 'uploading' ? '<i class="bridge-att-spin"></i>' : ''}${it.status === 'error' ? '<i class="bridge-att-err">!</i>' : ''}`
      + '<button type="button" class="bridge-att-x" aria-label="移除">✕</button></div>').join('');
  };
  async function add(files) {
    files = [...(files || [])].filter(f => f && (f.type?.startsWith('image/') || /\.(heic|heif)$/i.test(f.name || '')));
    if (!files.length || !enabled) return;
    let pieces;
    try { pieces = await prepareImages(files, L); } catch (e) { onError?.(e.message); return; }
    const room = L.maxFiles - items.length;
    if (pieces.length > room) { onError?.(`一条消息最多 ${L.maxFiles} 张图（长截图会切成几张）`); pieces = pieces.slice(0, Math.max(0, room)); }
    const added = pieces.map(p => ({ key: ++seq, blob: p.blob, name: p.name, url: URL.createObjectURL(p.blob), status: 'uploading', id: null, error: '' }));
    items.push(...added); render(); changed();
    await Promise.all(added.map(async (it) => {
      try { it.id = (await client.upload(it.blob, { name: it.name })).id; it.status = 'ready'; }
      catch (e) { it.status = 'error'; it.error = e.message; onError?.(`图片上传失败：${e.message}`); }
      if (items.includes(it)) { render(); changed(); }
    }));
  }
  const remove = (key) => { const it = items.find(i => i.key === key); if (!it) return; URL.revokeObjectURL(it.url); items = items.filter(i => i !== it); render(); changed(); };
  const onPick = () => { add(input.files); input.value = ''; };
  const onButton = () => input.click();
  const onTray = (e) => { const x = e.target.closest('.bridge-att-x'); if (x) remove(Number(x.closest('.bridge-att').dataset.k)); };
  const onPaste = (e) => {
    const files = [...(e.clipboardData?.files || [])].filter(f => f.type.startsWith('image/'));
    if (files.length && enabled) { e.preventDefault(); add(files); }
  };
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes('Files');
  const onOver = (e) => { if (!hasFiles(e) || !enabled) return; e.preventDefault(); dropZone.classList.add('bridge-drop'); };
  const onLeave = (e) => { if (!dropZone.contains(e.relatedTarget)) dropZone.classList.remove('bridge-drop'); };
  const onDrop = (e) => { if (!hasFiles(e) || !enabled) return; e.preventDefault(); dropZone.classList.remove('bridge-drop'); add(e.dataTransfer.files); };
  button?.addEventListener('click', onButton); input?.addEventListener('change', onPick); tray.addEventListener('click', onTray);
  textarea?.addEventListener('paste', onPaste);
  dropZone?.addEventListener('dragover', onOver); dropZone?.addEventListener('dragleave', onLeave); dropZone?.addEventListener('drop', onDrop);
  return {
    ids: () => items.filter(i => i.status === 'ready').map(i => i.id),
    busy: () => items.some(i => i.status === 'uploading'),
    failed: () => items.some(i => i.status === 'error'),
    count: () => items.length,
    add,
    clear() { for (const it of items) URL.revokeObjectURL(it.url); items = []; render(); changed(); },
    setEnabled(on) { enabled = !!on; if (button) button.hidden = !enabled; },
    setLimits(l) { L = { ...L, ...l }; },
    destroy() {
      this.clear();
      button?.removeEventListener('click', onButton); input?.removeEventListener('change', onPick); tray.removeEventListener('click', onTray);
      textarea?.removeEventListener('paste', onPaste);
      dropZone?.removeEventListener('dragover', onOver); dropZone?.removeEventListener('dragleave', onLeave); dropZone?.removeEventListener('drop', onDrop);
    },
  };
}

export const IMAGE_ICON = '<svg viewBox="0 0 16 16" width="1.1em" height="1.1em" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2" y="3" width="12" height="10" rx="1.5"/><circle cx="6" cy="6.5" r="1.2"/><path d="M14 10.5 10.5 7 4 13"/></svg>';

// Popover chrome: a head row (title + ✕; on phones also a grip — swipe the head down to dismiss) above a body.
// Fills `pop`, returns the body element to render into. The ✕ sits outside the drag area: pointer capture on
// the drag handle would otherwise retarget its click.
export function popShell(pop, title, onClose) {
  pop.innerHTML = `<div class="bridge-pop-head"><div class="bridge-pop-drag"><i class="bridge-pop-grip"></i><span class="bridge-pop-name">${esc(title)}</span></div>`
    + '<button type="button" class="bridge-pop-x" aria-label="关闭" title="关闭">✕</button></div><div class="bridge-pop-body"></div>';
  pop.querySelector('.bridge-pop-x').addEventListener('click', () => onClose());
  let dy = 0;
  attachDrag(pop.querySelector('.bridge-pop-drag'),
    (_, y) => { dy = Math.max(0, y); pop.style.transform = dy ? `translateY(${dy}px)` : ''; },
    () => { pop.style.transform = ''; if (dy > 60) onClose(); dy = 0; });
  return pop.querySelector('.bridge-pop-body');
}

// iOS Safari fires no `click` for taps on non-interactive elements, so "tap outside to close" has to listen to
// pointerdown. `keep` = selector of elements that must not dismiss (the popovers themselves and their toggles).
// Freeze the page behind a modal sheet. iOS Safari ignores overflow:hidden on <body> for touch scrolling,
// so pin the body with position:fixed at the current offset and put the offset back on release.
// Idempotent: safe to call setScrollLock(false) from every close path.
let scrollLock = null;
export function setScrollLock(on) {
  if (!!on === !!scrollLock) return;
  const b = document.body, h = document.documentElement;
  if (on) {
    scrollLock = { y: window.scrollY, x: window.scrollX, body: b.style.cssText, html: h.style.overscrollBehavior };
    Object.assign(b.style, { position: 'fixed', top: `-${scrollLock.y}px`, left: `-${scrollLock.x}px`, right: '0', overflow: 'hidden' });
    h.style.overscrollBehavior = 'none';   // no rubber-band of the whole viewport either
  } else {
    const { x, y, body, html } = scrollLock; scrollLock = null;
    b.style.cssText = body; h.style.overscrollBehavior = html;
    window.scrollTo({ left: x, top: y, behavior: 'instant' });
  }
}
// Lock only when the popover is acting as a modal sheet (its scrim is visible = narrow screens).
export const lockIfModal = (scrim) => setScrollLock(getComputedStyle(scrim).display !== 'none');

export function onOutsidePointer(keep, close) {
  const h = (e) => { if (!e.target.closest(keep)) close(); };
  document.addEventListener('pointerdown', h);
  return () => document.removeEventListener('pointerdown', h);
}

// ============================================================
// Reference widget
// ============================================================

export function mountBridgeWidget(el, client, opts = {}) {
  const o = {
    scope: '', title: 'Claude', markdown: defaultMarkdown, showThreads: true, showSettings: true,
    contextWindows: CONTEXT_WINDOWS, placeholder: '输入消息', prefsKey: 'bridge_ui', strings: {}, ...opts,
  };
  const S = { threadTitle: '新对话', helperOff: 'helper 离线：消息会排队', polling: '轮询模式',
    waitingHelper: '等待 helper 接单…', thinking: 'Claude 正在思考…', empty: '问点什么吧', newThread: '＋ 新对话',
    rename: '重命名', pin: '置顶', unpin: '取消置顶', del: '删除对话', delConfirm: '删除这个对话？消息记录会一起删除。', stop: '停止', me: '我', assistant: 'Claude',
    ctx: '自动带背景', ...o.strings };
  const prefs = loadPrefs(o.prefsKey);

  el.innerHTML = `
    <div class="bridge ${o.showThreads ? '' : 'no-side'}${o.fill ? ' fill' : ''}">
      ${o.showThreads ? `<aside class="bridge-side"><button type="button" class="bridge-btn bridge-new" data-act="new">${esc(S.newThread)}</button><div class="bridge-threads"></div>${o.sideFoot ? `<div class="bridge-side-foot">${o.sideFoot}</div>` : ''}<div class="bridge-side-grip" title="拖动调整宽度"></div></aside><div class="bridge-side-scrim" data-act="side-close"></div>` : ''}
      <div class="bridge-main">
        <div class="bridge-top">
          ${o.showThreads ? '<button type="button" class="bridge-icon" data-act="side" title="对话列表">☰</button>' : ''}
          ${o.topStart || ''}
          <span class="bridge-title">${esc(o.title)}</span>
          <span class="bridge-agent"><i class="bridge-dot"></i><span class="txt"></span></span>
          ${o.showThreads ? `<button type="button" class="bridge-icon" data-act="menu" title="更多">⋯</button>
          <div class="bridge-menu" hidden><button type="button" data-act="rename">${esc(S.rename)}</button><button type="button" data-act="pin">${esc(S.pin)}</button><button type="button" class="danger" data-act="del">${esc(S.del)}</button></div>` : ''}
        </div>
        <div class="bridge-list"><div class="bridge-empty">${esc(S.empty)}</div></div>
        <form class="bridge-form">
          <div class="bridge-grip" title="拖动调整输入框高度，双击恢复自动"></div>
          <div class="bridge-tray" hidden></div>
          <textarea class="bridge-input" rows="1" placeholder="${esc(o.placeholder)}"></textarea>
          <div class="bridge-bar">
            <button type="button" class="bridge-icon" data-act="prefs" title="设置" aria-label="设置"><svg viewBox="0 0 16 16" width="1.1em" height="1.1em" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" aria-hidden="true"><path d="M2 4h7M13 4h1M2 8h2M8 8h6M2 12h8"/><circle cx="11" cy="4" r="1.7"/><circle cx="6" cy="8" r="1.7"/><circle cx="12" cy="12" r="1.7"/></svg></button>
            <button type="button" class="bridge-icon" data-act="attach" title="添加图片（也可以粘贴、拖进来）" aria-label="添加图片" hidden>${IMAGE_ICON}</button>
            <input type="file" class="bridge-file" accept="image/*" multiple hidden />
            <span class="grow"></span>
            ${o.showSettings ? `<select class="bridge-pick" data-set="effort" title="思考深度"></select><select class="bridge-pick" data-set="model" title="模型"></select>` : ''}
            <button type="button" class="bridge-pill" data-act="ctx" hidden title="当前会话上下文占用，点开看构成与额度"><span class="pct"></span><i class="ring"></i></button>
            <button type="button" class="bridge-btn bridge-stop" hidden title="${esc(S.stop)}" aria-label="${esc(S.stop)}"><svg viewBox="0 0 16 16" width="1em" height="1em" aria-hidden="true"><rect x="4" y="4" width="8" height="8" rx="1.5" fill="currentColor"/></svg></button>
            <button type="submit" class="bridge-btn bridge-send" title="发送" aria-label="发送"><svg viewBox="0 0 16 16" width="1.1em" height="1.1em" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 13V3.5M3.8 7.7 8 3.5l4.2 4.2"/></svg></button>
          </div>
        </form>
      </div>
    </div>`;
  const root = el.firstElementChild;
  const pops = { prefs: document.createElement('div'), ctx: document.createElement('div') };
  for (const [k, p] of Object.entries(pops)) { p.className = `bridge-pop bridge-root pop-${k}`; p.hidden = true; document.body.appendChild(p); }
  // 弹层 / 菜单都挂到 body：宿主祖先若有 transform / filter / backdrop-filter，会把 position:fixed 的参照系困在那个祖先里
  const menu = el.querySelector('.bridge-menu');
  if (menu) { menu.classList.add('bridge-root'); document.body.appendChild(menu); }
  // phones: popovers become bottom sheets that cover their own toggles; a scrim behind them closes on tap
  const scrim = document.createElement('div'); scrim.className = 'bridge-scrim'; scrim.hidden = true; document.body.appendChild(scrim);

  const $ = (sel) => el.querySelector(sel);
  const list = $('.bridge-list'), input = $('.bridge-input'), sendBtn = $('.bridge-send'), stopBtn = $('.bridge-stop'), pill = $('[data-act="ctx"]');
  const state = { thread: null, threadRow: null, msgs: new Map(), order: [], sub: null, inflight: null, settings: null, raf: null, dirty: new Set(), ctxBusy: false, agent: null, job: null, ticker: null };
  const att = mountAttachments({
    button: $('[data-act="attach"]'), input: $('.bridge-file'), tray: $('.bridge-tray'), textarea: input, dropZone: $('.bridge-form'), client,
    onChange: ({ busy }) => { sendBtn.disabled = !!state.inflight || busy; }, onError: (msg) => toast(msg, true),
  });

  // ---------- helpers ----------
  let toastTimer;
  function toast(msg, isErr = false) {
    let t = document.querySelector('.bridge-toast');
    if (!t) { t = document.createElement('div'); t.className = 'bridge-toast'; document.body.appendChild(t); }
    t.textContent = msg; t.classList.toggle('err', isErr); t.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, isErr ? 4000 : 2000);
  }
  const nearBottom = () => list.scrollHeight - list.scrollTop - list.clientHeight < 80;
  const scrollBottom = () => { list.scrollTop = list.scrollHeight; };
  const placeholder = () => o.placeholder + (o.placeholder.includes('发送') ? '' : `，${sendHint(prefs)}`);

  function setPending(on) {
    state.inflight = on ? state.inflight : null;
    sendBtn.disabled = !!on || att.busy();
    stopBtn.hidden = !on;
    input.placeholder = on ? 'Claude 正在回答，稍等…' : placeholder();
  }

  function usePrefs(key) {
    applyPrefs(root, prefs);
    savePrefs(prefs, o.prefsKey);
    if (key === 'sendKey' || !key) input.placeholder = state.inflight ? input.placeholder : placeholder();
    if (key === 'timestamps' || key === 'toolsOpen' || !key) for (const id of state.order) paint(id);
    if (key === 'inputH' || !key) { if (prefs.inputH) { input.style.height = `${prefs.inputH}px`; input.classList.add('fixed'); } else { input.classList.remove('fixed'); autoGrow(); } }
  }

  // ---------- rendering ----------
  function turnEl(m) { const d = document.createElement('div'); d.className = `bridge-turn ${m.role}`; d.dataset.id = m.id; return d; }
  function paint(id) {
    const m = state.msgs.get(id); if (!m) return;
    const b = list.querySelector(`.bridge-turn[data-id="${id}"]`); if (!b) return;
    const follow = nearBottom();
    renderTurn(b, m, { markdown: o.markdown, timestamps: prefs.timestamps, open: prefs.toolsOpen, fileUrl: (id) => client.fileUrl(id), strings: { me: S.me, assistant: S.assistant, waitingHelper: S.waitingHelper, thinking: S.thinking } });
    if (follow) scrollBottom();
  }
  function schedulePaint(id) {
    state.dirty.add(id);
    if (state.raf) return;
    state.raf = requestAnimationFrame(() => { state.raf = null; for (const i of state.dirty) paint(i); state.dirty.clear(); });
  }
  function upsert(m) {
    const existed = state.msgs.has(m.id);
    if (existed) { const prev = state.msgs.get(m.id); m.events = m.events?.length ? m.events : prev.events; }
    state.msgs.set(m.id, m);
    if (!existed) { state.order.push(m.id); list.querySelector('.bridge-empty')?.remove(); list.appendChild(turnEl(m)); }
    paint(m.id);
    if (!existed) scrollBottom();
  }

  function summary() { return sessionSummary([...state.msgs.values()], { contextWindows: o.contextWindows, context: state.threadRow?.context }); }
  function renderSession() {
    const s = summary(), job = jobActive(state.job) ? state.job : null, compacting = job?.kind === 'compact';
    pill.hidden = s.pct == null && !compacting;
    pill.classList.toggle('busy', compacting);
    if (compacting) pill.querySelector('.pct').innerHTML = job.status === 'running' ? `压缩中 <span data-job-since="${esc(job.started_at || '')}">${fmtElapsed(job.started_at)}</span>` : '压缩排队中';
    else if (s.pct != null) { pill.querySelector('.pct').textContent = `${s.pct}%`; pill.style.setProperty('--p', s.pct); pill.classList.toggle('hot', s.pct >= 80); }
    if (!pops.ctx.hidden) renderSessionPanel(ctxBody(), s, ctxActions());
    if (job && !state.ticker) state.ticker = setInterval(() => { tickJobs(el); tickJobs(pops.ctx); }, 1000);
    if (!job && state.ticker) { clearInterval(state.ticker); state.ticker = null; }
  }
  function ctxActions() {
    const run = (fn, msg) => async () => {
      try { state.ctxBusy = true; renderSession(); const r = await fn(state.thread); if (r?.job) state.job = r.job; state.ctxBusy = false; renderSession(); toast(msg); }
      catch (e) { state.ctxBusy = false; renderSession(); toast(e.message, true); }
    };
    return { busy: state.ctxBusy || jobActive(state.job), job: state.job, onRefresh: run((t) => client.refreshContext(t), '已请求刷新，helper 计算中…'), onCompact: run((t) => client.compact(t), '已请求压缩，进度见上下文胶囊') };
  }
  function renderAgent(agent, fallback = state.sub?.fallback) {
    if (agent) state.agent = agent;
    const a = state.agent || {};
    $('.bridge-dot').classList.toggle('on', !!a.online);
    $('.bridge-agent').title = a.online ? `helper 在线 · ${a.worker || ''}` : S.helperOff;
    $('.bridge-agent .txt').textContent = (a.online ? '' : S.helperOff) + (fallback ? (a.online ? S.polling : ` · ${S.polling}`) : '');
  }
  function applySnapshot(snap) {
    list.innerHTML = '';
    state.msgs.clear(); state.order = [];
    state.threadRow = snap.thread;
    state.job = (snap.jobs || []).find(j => j.kind === 'compact') || (snap.jobs || [])[0] || null;
    if (!snap.messages.length) list.innerHTML = `<div class="bridge-empty">${esc(S.empty)}</div>`;
    for (const m of snap.messages) upsert(m);
    state.inflight = snap.inflight;
    setPending(!!snap.inflight);
    $('.bridge-title').textContent = snap.thread?.title || S.threadTitle;
    renderAgent(snap.agent);
    renderSession();
    scrollBottom();
  }

  // ---------- subscription ----------
  function subscribe(threadId) {
    state.sub?.close();
    state.thread = threadId;
    state.sub = client.subscribe(threadId, {
      onSnapshot: applySnapshot,
      onMessage: (m) => { upsert(m); if (m.role === 'assistant' && (m.status === 'pending' || m.status === 'streaming')) { state.inflight = m.id; setPending(true); } },
      onDelta: (d) => { const m = state.msgs.get(d.message_id); if (!m) return; m.content += d.text; m.rev = d.rev; schedulePaint(d.message_id); },
      onEvent: (ev) => {
        const m = state.msgs.get(ev.message_id); if (!m) return;
        (m.events ||= []).push(ev);
        if (ev.type === 'usage' || ev.type === 'init' || ev.type === 'rate_limit') renderSession(); else schedulePaint(ev.message_id);
      },
      onStatus: (s) => { const m = state.msgs.get(s.message_id); if (!m) return; m.status = s.status; paint(s.message_id); if (s.status !== 'pending' && s.status !== 'streaming') setPending(false); },
      onDone: (d) => { const m = state.msgs.get(d.message_id); if (m) { m.status = d.status; paint(d.message_id); } setPending(false); renderSession(); loadThreads(); },
      onThread: (t) => { if (t.id === state.thread) { state.threadRow = { ...state.threadRow, ...t }; $('.bridge-title').textContent = t.title || S.threadTitle; } loadThreads(); },
      onContext: (c) => { if (state.threadRow) state.threadRow.context = c.context; state.ctxBusy = false; renderSession(); },
      onJob: (j) => {
        state.ctxBusy = false;
        if (jobActive(j)) { state.job = j; renderSession(); return; }
        if (state.job?.id === j.id) state.job = null;
        renderSession();
        if (j.kind === 'compact') {
          let r = null; try { r = JSON.parse(j.result || '{}'); } catch { r = null; }
          if (j.status === 'done' && r?.compact) toast(describeCompact({ ...r.compact, trigger: 'manual' }));
          else if (j.status !== 'done') toast(`压缩失败：${j.error || ''}`, true);
        } else if (j.status !== 'done') toast(`刷新构成失败：${j.error || ''}`, true);
      },
      onFallback: (on) => renderAgent(null, on),
      onError: (err) => { // transport errors repeat every poll while the server is down; say it once in a while
        const now = Date.now();
        if (now - (state.lastErrToast || 0) > 30000) { state.lastErrToast = now; toast(err.message, true); }
      },
    });
  }

  // ---------- threads ----------
  async function loadThreads() {
    if (!o.showThreads) return;
    try {
      const d = await client.threads(o.scope);
      $('.bridge-threads').innerHTML = d.items.map(t => `<button type="button" class="bridge-thread ${t.id === state.thread ? 'active' : ''}" data-id="${esc(t.id)}">`
        + `<span class="t">${t.pinned ? '<span class="pin">📌</span>' : ''}${esc(t.title || S.threadTitle)}</span>`
        + `<span class="p">${esc(t.preview || '')}</span><span class="m">${t.n ?? 0} 条 · ${esc(fmtRelative(t.updated_at))}</span></button>`).join('');
      if (!state.thread && d.current) subscribe(d.current);
    } catch (e) { toast(e.message, true); }
  }
  async function openThread(id) {
    try { await client.selectThread(id, o.scope); subscribe(id); loadThreads(); root.classList.remove('side-open'); } catch (e) { toast(e.message, true); }
  }

  // ---------- settings (server side: model / effort / auto_context) ----------
  function settingsHtml() {
    const s = state.settings; if (!s) return '';
    const opt = (v, t, cur) => `<option value="${esc(v)}"${v === (cur || '') ? ' selected' : ''}>${esc(t)}</option>`;
    return `<div class="bridge-prefs bridge-prefs-chat"><div class="bridge-pref-row"><span class="bridge-pref-label">模型</span><select class="bridge-pick" data-set="model">${modelOptionsHtml(s.models, s.chat.model)}</select></div>`
      + `<div class="bridge-pref-row"><span class="bridge-pref-label">思考深度</span><select class="bridge-pick" data-set="effort">${s.efforts.map(e => opt(e, e || '默认', s.chat.effort)).join('')}</select></div>`
      + `<div class="bridge-pref-row"><span class="bridge-pref-label">单次最多工具轮数</span><input type="number" class="bridge-num" data-set="max_turns" min="1" max="200" value="${Number(s.chat.max_turns) || 40}" /></div>`
      + `<label class="bridge-pref-row"><span class="bridge-pref-label">${esc(S.ctx)}<span class="bridge-pref-hint">新对话第一条自动附上宿主提供的背景</span></span><input type="checkbox" class="bridge-switch" data-set="auto_context"${s.chat.auto_context ? ' checked' : ''} /></label></div>`;
  }
  async function loadSettings() {
    if (!o.showSettings) return;
    try {
      const s = await client.settings();
      state.settings = s;
      const model = $('.bridge-bar [data-set="model"]'), effort = $('.bridge-bar [data-set="effort"]');
      model.innerHTML = modelOptionsHtml(s.models, s.chat.model);
      effort.innerHTML = s.efforts.map(e => `<option value="${esc(e)}">${e ? esc(e) : '默认'}</option>`).join('');
      effort.value = s.chat.effort || '';
      renderAgent(s.agent);
      att.setEnabled(!!s.uploads?.enabled);
      if (s.uploads) att.setLimits({ maxBytes: s.uploads.max_bytes, maxFiles: s.uploads.max_files });
    } catch (e) { toast(e.message, true); }
  }
  async function saveSetting(patch) {
    try { const d = await client.saveSettings(patch); if (state.settings) state.settings.chat = d.chat; toast('设置已保存，下一条消息生效'); loadSettings(); }
    catch (e) { toast(e.message, true); }
  }

  // ---------- popovers ----------
  function openPop(kind, anchor) {
    const p = pops[kind];
    if (!p.hidden) { closePops(); return; }
    closePops();
    if (kind === 'prefs') {
      const body = popShell(p, '设置', closePops);
      body.innerHTML = '<div class="bridge-pop-title">界面</div><div class="ui"></div>' + (o.showSettings && state.settings ? '<div class="bridge-pop-title">对话</div><div class="chat"></div>' : '');
      // fill: the widget is the whole page, so the chat-height preference has nothing to size
      const fields = o.fill ? PREF_FIELDS.map(f => f.key).filter(k => k !== 'height') : undefined;
      renderPrefsPanel(body.querySelector('.ui'), prefs, { onChange: (_, key) => usePrefs(key), fields });
      const c = body.querySelector('.chat'); if (c) c.innerHTML = settingsHtml();
    } else renderSessionPanel(ctxBody(), summary(), ctxActions());
    scrim.hidden = false; lockIfModal(scrim);
    placePopover(anchor, p, { align: kind === 'prefs' ? 'start' : 'end' });
  }
  function ctxBody() { return pops.ctx.querySelector('.bridge-pop-body') || popShell(pops.ctx, '当前会话', closePops); }
  function closePops() { for (const p of Object.values(pops)) p.hidden = true; if (menu) menu.hidden = true; scrim.hidden = true; setScrollLock(false); }
  scrim.addEventListener('click', closePops);

  // ---------- actions ----------
  async function send() {
    const text = input.value.trim();
    if (att.busy()) { toast('图片还在上传，稍等'); return; }
    if (!text && !att.ids().length) return;
    sendBtn.disabled = true;
    try {
      const files = att.ids();
      const r = await client.send(text, state.thread ? { thread: state.thread, files } : { scope: o.scope, files });
      input.value = ''; autoGrow(); att.clear();
      if (r.thread !== state.thread) subscribe(r.thread);
    } catch (e) { toast(e.message, true); sendBtn.disabled = false; }
  }
  function autoGrow() { if (prefs.inputH) return; input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, window.innerHeight * 0.4) + 'px'; }

  $('.bridge-form').addEventListener('submit', (e) => { e.preventDefault(); send(); });
  input.addEventListener('keydown', (e) => { if (isSendKey(e, prefs)) { e.preventDefault(); send(); } });
  input.addEventListener('input', autoGrow);
  stopBtn.addEventListener('click', async () => { if (!state.inflight) return; try { await client.cancel(state.inflight); } catch (e) { toast(e.message, true); } });
  // drag the grip above the input to fix its height; double-click restores auto-grow
  let h0 = 0;
  attachDrag($('.bridge-grip'), (_, dy) => { prefs.inputH = Math.max(40, Math.min(600, h0 - dy)); input.style.height = `${prefs.inputH}px`; input.classList.add('fixed'); }, () => usePrefs('inputH'));
  $('.bridge-grip').addEventListener('pointerdown', () => { h0 = input.offsetHeight; });
  $('.bridge-grip').addEventListener('dblclick', () => { prefs.inputH = null; usePrefs('inputH'); });
  if (o.showThreads) {
    let w0 = 0; const side = $('.bridge-side');
    $('.bridge-side-grip').addEventListener('pointerdown', () => { w0 = side.offsetWidth; });
    attachDrag($('.bridge-side-grip'), (dx) => { prefs.sideW = Math.max(180, Math.min(480, w0 + dx)); root.style.setProperty('--chat-side-w', `${prefs.sideW}px`); }, () => usePrefs('sideW'));
  }

  const onClick = async (e) => {
    const act = e.target.closest('[data-act]')?.dataset.act;
    const th = e.target.closest('.bridge-thread');
    if (th) return openThread(th.dataset.id);
    if (!act) return;
    try {
      if (act === 'new') { const r = await client.createThread({ scope: o.scope }); subscribe(r.thread); loadThreads(); root.classList.remove('side-open'); }
      else if (act === 'side-close') root.classList.remove('side-open');
      else if (act === 'side') { if (matchMedia('(max-width: 720px)').matches) root.classList.toggle('side-open'); else { prefs.sidebar = !prefs.sidebar; usePrefs('sidebar'); } }
      else if (act === 'menu') { const m = menu; const was = m.hidden; closePops(); if (was) { m.querySelector('[data-act="pin"]').textContent = state.threadRow?.pinned ? S.unpin : S.pin; placePopover(e.target.closest('[data-act]'), m); } }
      else if (act === 'prefs' || act === 'ctx') openPop(act, e.target.closest('[data-act]'));
      else if (act === 'rename') { closePops(); const t = prompt(S.rename, state.threadRow?.title || ''); if (t != null) await client.patchThread(state.thread, { title: t.trim() || null }); }
      else if (act === 'pin') { closePops(); await client.patchThread(state.thread, { pinned: !state.threadRow?.pinned }); }
      else if (act === 'del') { closePops(); if (!confirm(S.delConfirm)) return; const r = await client.deleteThread(state.thread, o.scope); state.thread = null; state.sub?.close(); if (r.current) subscribe(r.current); else { list.innerHTML = `<div class="bridge-empty">${esc(S.empty)}</div>`; } loadThreads(); }
    } catch (err) { toast(err.message, true); }
  };
  el.addEventListener('click', onClick); menu?.addEventListener('click', onClick);
  const onSet = (e) => {
    const key = e.target.dataset.set; if (!key) return;
    const v = e.target.type === 'checkbox' ? e.target.checked : e.target.type === 'number' ? Number(e.target.value) : e.target.value;
    saveSetting({ [key]: v });
  };
  el.addEventListener('change', onSet); pops.prefs.addEventListener('change', onSet);
  // the scrim closes on its own click (closing it on pointerdown would let the tap fall through to what is underneath)
  const offOutside = onOutsidePointer('.bridge-pop, .bridge-menu, .bridge-scrim, [data-act="prefs"], [data-act="ctx"], [data-act="menu"]', closePops);
  const onKey = (e) => { if (e.key === 'Escape') { closePops(); root.classList.remove('side-open'); } };
  const onVis = () => { if (document.visibilityState === 'visible') state.sub?.wake(); };
  document.addEventListener('keydown', onKey); document.addEventListener('visibilitychange', onVis);

  usePrefs();
  loadSettings();
  if (o.showThreads) loadThreads();
  else client.threads(o.scope).then(d => d.current && subscribe(d.current)).catch(e => toast(e.message, true));

  return {
    destroy() {
      state.sub?.close(); clearInterval(state.ticker);
      offOutside(); document.removeEventListener('keydown', onKey); document.removeEventListener('visibilitychange', onVis);
      el.removeEventListener('click', onClick); el.removeEventListener('change', onSet); // the host may mount again into el
      for (const p of Object.values(pops)) p.remove();
      menu?.remove(); scrim.remove(); setScrollLock(false); att.destroy();
      el.innerHTML = '';
    },
    openThread,
    refresh() { loadThreads(); loadSettings(); },
    get thread() { return state.thread; },
    get prefs() { return prefs; },
  };
}
