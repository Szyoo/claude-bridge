// Reference chat UI for claude-bridge. Framework-free; all styling via bridge-widget.css (--bridge-* tokens).
//
// Exports, roughly in layers a host can pick from:
//   pure helpers   describeTool, sessionSummary, splitTurn, DEFAULT_PREFS, loadPrefs, savePrefs
//   renderers      renderTurn, renderSteps, renderContextPanel, renderSessionPanel, renderPrefsPanel, applyPrefs, placePopover
//   composer       parseToken, modelMenuRows, modelLabel, effortState, composerCommands, morphPath, mountVoicePill, startDictation, speechLang
//   full widget    mountBridgeWidget(el, client, opts) -> { destroy(), openThread(id), refresh(), input }
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

// 对话列表里「占本周额度」：服务端给的 quota_pct（未标定时为 null，不显示）
export function quotaText(pct) {
  if (pct == null) return '';
  return ` · 周额度 ${pct > 0 && pct < 0.1 ? '<0.1' : pct < 10 ? (Math.round(pct * 10) / 10) : Math.round(pct)}%`;
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
// /context segments: the host's chart palette via --bridge-chart-1…6 (bridge-widget.css → @szyyw/design --chart-*); the inline
// fallback chain keeps them coloured where the panel is rendered outside a .bridge-root (hosts that only use renderContextPanel)
const CHART_FALLBACK = ['#3b6df2', '#e0703a', '#2e9b5d', '#d4a72c', '#8b8b8b', '#6f6f6f'];
const categoryColor = (i) => { const n = (i % CHART_FALLBACK.length) + 1; return `var(--bridge-chart-${n}, var(--chart-${n}, ${CHART_FALLBACK[n - 1]}))`; };

// Server UTC text 'YYYY-MM-DD HH:MM:SS' → epoch ms (null when absent / unparsable).
const parseUtc = (utc) => {
  if (!utc) return null;
  const t = Date.parse(String(utc).replace(' ', 'T') + (/Z$|[+-]\d\d:?\d\d$/.test(utc) ? '' : 'Z'));
  return Number.isNaN(t) ? null : t;
};

// Elapsed m:ss since a server UTC timestamp ('YYYY-MM-DD HH:MM:SS').
export function fmtElapsed(utc, now = Date.now()) {
  if (!utc) return '0:00';
  const t = parseUtc(utc);
  if (t == null) return '';
  const sec = Math.max(0, Math.round((now - t) / 1000));
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`;
}

// Duration for the message status chips: 12.3s / 1m 05s. `coarse` (derived from second-resolution server timestamps)
// drops the tenths and shows '<1s' for sub-second spans instead of pretending to know.
export function fmtDur(ms, coarse = false) {
  if (!Number.isFinite(ms) || ms < 0) return '';
  if (coarse) {
    const s = Math.round(ms / 1000);
    if (s < 1) return '<1s';
    return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`;
  }
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  const s = Math.floor(ms / 1000);
  return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`;
}

// Fill fraction of a running call chip: linear to `hold` at the expected duration, then creeps towards 98% (it never
// completes by itself — only the result event does).
export function callProgress(ms, expected, hold = 0.9) {
  if (!(expected > 0)) return 0;
  const x = Math.max(0, ms) / expected;
  return x < 1 ? hold * x : hold + (0.98 - hold) * (1 - Math.exp(1 - x));
}

// Refreshes every live counter under `root` (the widget calls it ~4×/s while something is running): session job timers,
// running tool chips (timer + fill + progress arc) and the turn's think timer.
export function tickJobs(root = document, now = Date.now()) {
  for (const el of root.querySelectorAll('[data-job-since]')) el.textContent = fmtElapsed(el.dataset.jobSince, now);
  for (const el of root.querySelectorAll('[data-t0]')) {
    const ms = Math.max(0, now - Number(el.dataset.t0));
    const t = el.querySelector('[data-timer]');
    if (t) t.textContent = fmtDur(ms);
    if (el.dataset.exp) {
      const p = callProgress(ms, Number(el.dataset.exp));
      const fill = el.querySelector('.bridge-fill'); if (fill) fill.style.transform = `scaleX(${p.toFixed(3)})`;
      const ring = el.querySelector('.bridge-mark .ring'); if (ring) ring.style.strokeDasharray = `${p.toFixed(3)} 1`;
    }
  }
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
    const segs = counted.map((c, i) => `<i style="width:${Math.max(0.3, (c.tokens || 0) / win * 100)}%;background:${categoryColor(i)}" title="${esc(CATEGORY_ZH[c.name] || c.name)} ${fmtTokens(c.tokens)}"></i>`).join('');
    const rows = (ctx.categories || []).map((c) => {
      const counting = !c.deferred && c.name !== 'Free space';
      const dot = counting ? `<i class="dot" style="background:${categoryColor(counted.indexOf(c))}"></i>` : '<i class="dot none"></i>';
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
      <p class="bridge-ctx-warn" role="alert" hidden></p>
    </div>`;
  }
  el.querySelector('[data-ctx="refresh"]')?.addEventListener('click', () => opts.onRefresh?.());
  // DESIGN §8: the first click arms the button and says what will happen; the second (within 5 s) compacts
  el.querySelector('[data-ctx="compact"]')?.addEventListener('click', (e) => {
    if (armConfirm(e.currentTarget, { label: '再点一次确认压缩', warnEl: el.querySelector('.bridge-ctx-warn'),
      warning: 'Claude 会把这段对话的历史压缩成摘要：细节会丢失，但上下文会明显变小。' })) opts.onCompact?.();
  });
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
  const liveThinking = new Map(), nativeByMessage = new Map(), closedThinking = [];
  const nativeItem = (mid, block, at, index) => {
    const rows = nativeByMessage.get(mid) || [];
    const it = { kind: 'thinking', native: true, text: block.thinking || '', rawText: block.thinking || '',
      redacted: block.type === 'redacted_thinking', truncated: false, streaming: true,
      signed: false, key: `native-thinking:${mid}:${index}`, at: at ?? 0, unknownAt: at == null, fullSeen: false };
    items.push(it); rows.push(it); nativeByMessage.set(mid, rows); return it;
  };
  for (const ev of events || []) {
    const d = ev.data || {};
    switch (ev.type) {
      case 'content_stream': {
        const e = d.event || {}, mid = d.native_message_id || '', key = `${mid}:${e.index ?? 0}`;
        if (e.type === 'content_block_start' && ['thinking', 'redacted_thinking'].includes(e.content_block?.type)) {
          const it = nativeItem(mid, e.content_block, d.at, e.index ?? 0);
          liveThinking.set(key, it);
        } else if (e.type === 'content_block_delta' && e.delta?.type === 'thinking_delta') {
          const it = liveThinking.get(key);
          if (it) {
            it.rawText += e.delta.thinking || '';
            if (!it.fullSeen) it.text = it.rawText;
          }
        } else if (e.type === 'content_block_delta' && e.delta?.type === 'signature_delta') {
          const it = liveThinking.get(key);
          if (it) it.signed ||= !!e.delta.signature; // presence only; opaque bytes are never rendered
        } else if (e.type === 'content_block_stop') {
          const it = liveThinking.get(key);
          if (it) { it.streaming = false; if (!it.previewSeen) closedThinking.push(it); liveThinking.delete(key); }
        }
        break;
      }
      case 'content_message': {
        const message = d.message || {}, mid = message.id || '';
        let ordinal = 0;
        for (const block of message.content || []) {
          if (!['thinking', 'redacted_thinking'].includes(block.type)) continue;
          const redacted = block.type === 'redacted_thinking', text = block.thinking || '';
          const rows = nativeByMessage.get(mid) || [];
          let it = rows.find(row => row.redacted === redacted && !row.fullSeen
            && (redacted || text.startsWith(row.rawText)));
          // Cumulative assistant messages can repeat already finalized blocks.
          if (!it) it = rows.find(row => row.fullSeen && row.redacted === redacted && row.text === text);
          if (!it) { it = nativeItem(mid, block, d.at, `full-${ordinal}`); closedThinking.push(it); }
          it.text = text; it.fullSeen = true; it.signed ||= !!block.signature;
          // A full block can precede its raw stop event. Keep its live status
          // until that stop, while no-partial workers are already complete.
          if (![...liveThinking.values()].includes(it)) it.streaming = false;
          ordinal++;
        }
        break;
      }
      case 'tool_use': { const it = { kind: 'tool', id: d.id || '', name: d.name || '?', input: d.input || {}, result: null, at: d.at ?? 0, t0: parseUtc(ev.created_at), t1: null }; items.push(it); if (d.id) byTool.set(d.id, it); break; }
      case 'tool_result': { const it = byTool.get(d.tool_use_id); if (it) { it.result = d; it.t1 = parseUtc(ev.created_at); } break; }
      case 'thinking': {
        const index = closedThinking.findIndex(it => it.at === (d.at ?? 0) || it.unknownAt);
        const it = index >= 0 ? closedThinking.splice(index, 1)[0]
          : [...liveThinking.values()].find(row => !row.previewSeen && row.at === (d.at ?? 0));
        if (it) {
          // A display preview must never replace or truncate the native block.
          it.previewSeen = true;
          if (it.unknownAt) { it.at = d.at ?? 0; it.unknownAt = false; }
        } else items.push({ kind: 'thinking', text: d.text || '', truncated: !!d.truncated, at: d.at ?? 0 });
        break;
      }
      case 'compact': items.push({ kind: 'note', sub: 'compact', text: describeCompact(d), at: 0 }); break;
      case 'error': items.push({ kind: 'note', sub: 'error', text: d.message || '出错', at: END }); break;
      case 'status':
        if (d.phase === 'legacy_trace') items.push({ kind: 'trace', text: d.text || '', at: END });
        else if (d.phase === 'cancel_requested') items.push({ kind: 'note', sub: 'cancel', text: '已请求停止…', at: END });
        break;
      default: break;
    }
  }
  for (let i = items.length - 1; i >= 0; i--) {
    if (items[i].kind === 'thinking' && !items[i].streaming && !items[i].text && !items[i].redacted && !items[i].signed) items.splice(i, 1);
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

// ---------- message status: call chip / status mark / lattice loader ----------
// The tool rows, status icons and think header below are CSS / inline-SVG re-implementations of three React Bits
// components (Call Chip, Status Mark, Lattice Loader — https://reactbits.dev, MIT + Commons Clause; the author is aware
// of the licence terms). Motion → CSS animations, icons → hand-drawn inline SVG (no icon package).
// Timing comes from the events' server timestamps (`created_at`, second resolution), so reloading a page keeps the values.

const L10N = {
  zh: { thinking: 'Claude 正在思考…', waitingHelper: '等待 helper 接单…', thought: '已思考', stopped: '已中断，用时',
    markPending: '未开始', markRunning: '进行中', markDone: '已完成', markFailed: '失败', markNoResult: '无结果' },
  ja: { thinking: 'Claude が考えています…', waitingHelper: 'helper の応答待ち…', thought: '思考時間', stopped: '中断まで',
    markPending: '未開始', markRunning: '実行中', markDone: '完了', markFailed: '失敗', markNoResult: '結果なし' },
  en: { thinking: 'Claude is thinking…', waitingHelper: 'Waiting for the helper…', thought: 'Thought for', stopped: 'Stopped after',
    markPending: 'Pending', markRunning: 'In progress', markDone: 'Done', markFailed: 'Failed', markNoResult: 'No result' },
};
// Strings of the message area for 'zh' (default) / 'ja' / 'en'; hosts spread this into `strings` (renderTurn) or pass `lang`.
export const messageStrings = (lang) => L10N[String(lang || 'zh').slice(0, 2).toLowerCase()] || L10N.zh;

// Expected run time per tool (ms) — only paces the fill of the call chip; override with opts.expected = { Bash: 20000 } or (item) => ms.
export const TOOL_EXPECTED_MS = { Bash: 6000, Read: 1200, Glob: 1500, Grep: 2000, Edit: 1500, MultiEdit: 2000, Write: 1500,
  NotebookEdit: 1500, TodoWrite: 800, WebFetch: 6000, WebSearch: 8000, Task: 60000, Agent: 60000 };
const DEFAULT_EXPECTED_MS = 4000;
function expectedMs(it, o) {
  const e = typeof o.expected === 'function' ? o.expected(it) : o.expected?.[it.name];
  return Number.isFinite(e) && e > 0 ? e : (TOOL_EXPECTED_MS[it.name] ?? DEFAULT_EXPECTED_MS);
}

const MARK_CHECK = 'M7.5 12.25 10.5 15.25 16.75 8.75', MARK_CROSS = 'M8.5 8.5 15.5 15.5M15.5 8.5 8.5 15.5';
// Status Mark: hollow ring (pending) → progress arc (running; determinate with `progress` 0–1, else a spinning arc) → check / cross.
// status: 'pending' | 'running' | 'done' | 'failed' | 'cancelled'. The spin phase follows the wall clock so re-renders don't restart it.
export function markHtml(status, { progress, now = Date.now(), label = '' } = {}) {
  const det = status === 'running' && Number.isFinite(progress);
  const ring = status !== 'running' ? '' : det ? ` style="stroke-dasharray:${progress.toFixed(3)} 1"` : ` style="animation-delay:-${now % 1100}ms"`;
  return `<svg class="bridge-mark" data-st="${status}" viewBox="0 0 24 24" role="img" aria-label="${esc(label)}"><g transform="rotate(-90 12 12)">`
    + `<circle class="trk" cx="12" cy="12" r="9"/><circle class="ring" cx="12" cy="12" r="9" pathLength="1"${ring}/></g>`
    + `<path class="chk" d="${MARK_CHECK}" pathLength="1"/><path class="crs" d="${MARK_CROSS}" pathLength="1"/></svg>`;
}

const firstLine = (t, n = 72) => { const s = (t || '').trim().split('\n')[0]; return s.length > n ? s.slice(0, n) + '…' : s; };
const markLabel = (st, S) => ({ pending: S.markPending, running: S.markRunning, done: S.markDone, failed: S.markFailed, cancelled: S.markNoResult }[st]);

function toolRow(it, o) {
  const running = !it.result && o.streaming;
  const err = !!it.result?.is_error;
  const st = running ? 'running' : err ? 'failed' : it.result ? 'done' : 'cancelled';
  const timed = running && it.t0 != null;
  const ms = timed ? Math.max(0, o.now - it.t0) : 0, exp = expectedMs(it, o), p = timed ? callProgress(ms, exp) : 0;
  const dur = it.result && it.t0 != null && it.t1 != null ? fmtDur(it.t1 - it.t0, true) : '';
  const time = timed ? `<span class="bridge-time" data-timer>${fmtDur(ms)}</span>` : dur ? `<span class="bridge-time">${dur}</span>` : '';
  const state = err ? '<span class="bridge-step-state err">失败</span>' : !running && !it.result ? '<span class="bridge-step-state">无结果</span>' : '';
  const out = it.result ? (it.result.content || '(无输出)') + (it.result.truncated ? '\n…（输出已截断）' : '') : running ? '运行中…' : '';
  const cmd = it.name === 'Bash' ? `$ ${it.input.command || ''}` : JSON.stringify(it.input, null, 1);
  return `<details class="bridge-step tool${running ? ' running' : ''}${err ? ' error' : ''}" data-k="${esc(it.id)}" data-st="${st}"${o.open ? ' open' : ''}>`
    + `<summary${timed ? ` data-t0="${it.t0}" data-exp="${exp}"` : ''}><span class="bridge-fill"${timed ? ` style="transform:scaleX(${p.toFixed(3)})"` : ''}></span>`
    + `<i class="bridge-caret"></i>${markHtml(st, { progress: timed ? p : undefined, now: o.now, label: markLabel(st, o.S) })}`
    + `<span class="bridge-step-name">${esc(it.name)}</span><span class="bridge-step-desc">${esc(describeTool(it))}</span>${time}${state}</summary>`
    + `<div class="bridge-term"><pre class="cmd">${esc(cmd)}</pre>${out ? `<pre class="out">${esc(out)}</pre>` : ''}</div></details>`;
}

function toolGroup(run, o, key) {
  const n = run.length, finished = run.filter(t => t.result);
  const done = finished.length;
  const running = o.streaming && done < n;
  const err = run.some(t => t.result?.is_error);
  const allBash = run.every(t => t.name === 'Bash');
  const label = running ? `执行中 · ${done}/${n}` : allBash ? `执行了 ${n} 条命令` : `${n} 次工具调用`;
  const st = running ? 'running' : err ? 'failed' : done === n ? 'done' : 'cancelled';
  const t0s = run.map(t => t.t0).filter(v => v != null), t0 = t0s.length ? Math.min(...t0s) : null;
  // progress = finished calls + the running ones' partial fill, over all calls
  let frac = done;
  for (const t of run) if (!t.result && o.streaming && t.t0 != null) frac += callProgress(Math.max(0, o.now - t.t0), expectedMs(t, o));
  const p = Math.min(0.98, Math.max(0.04, frac / n));
  const t1s = run.map(t => t.t1).filter(v => v != null);
  const dur = !running && t0 != null && t1s.length === n ? fmtDur(Math.max(...t1s) - t0, true) : '';
  const time = running && t0 != null ? `<span class="bridge-time" data-timer>${fmtDur(Math.max(0, o.now - t0))}</span>` : dur ? `<span class="bridge-time">${dur}</span>` : '';
  const state = err ? '<span class="bridge-step-state err">有失败</span>' : '';
  return `<details class="bridge-step group${running ? ' running' : ''}${err ? ' error' : ''}" data-k="${esc(key)}" data-st="${st}"${o.open ? ' open' : ''}>`
    + `<summary${running && t0 != null ? ` data-t0="${t0}"` : ''}><span class="bridge-fill"${running ? ` style="transform:scaleX(${p.toFixed(3)})"` : ''}></span>`
    + `<i class="bridge-caret"></i>${markHtml(st, { progress: running ? p : undefined, now: o.now, label: markLabel(st, o.S) })}`
    + `<span class="bridge-step-name">${label}</span>${time}${state}</summary>`
    + `<div class="bridge-step-list">${run.map(t => toolRow(t, o)).join('')}</div></details>`;
}

// items → HTML. opts: { streaming, open (expand by default), key (stable prefix for data-k), expected, strings, now }
export function stepsHtml(items, opts = {}) {
  const o = { streaming: false, open: false, key: 's', now: Date.now(), ...opts };
  o.S = { ...messageStrings(o.lang), ...o.strings };
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
      const running = !!it.streaming && o.streaming;
      const signatureOnly = it.signed && !it.text && !running;
      const label = it.redacted ? '思考内容未公开' : signatureOnly ? '未返回思考文本' : running ? '正在思考' : '思考过程';
      const text = it.redacted ? '模型未公开这段思考内容。' : signatureOnly ? '上游只提供了思考块签名，没有提供可显示的思考文本。' : it.text;
      const st = running ? 'running' : 'done';
      out.push(`<details class="bridge-step thinking${running ? ' running' : ''}" data-k="${esc(it.key || `${o.key}-t${i}`)}" data-st="${st}"${o.open || running ? ' open' : ''}><summary><i class="bridge-caret"></i>${markHtml(st, { now: o.now, label: markLabel(st, o.S) })}<span class="bridge-step-name">${label}</span><span class="bridge-step-desc">${esc(firstLine(text))}</span></summary>`
        + `<div class="bridge-step-body">${esc(text)}${it.truncated ? '\n…（已截断）' : ''}</div></details>`);
    } else if (it.kind === 'trace') {
      out.push(`<details class="bridge-step trace" data-k="${o.key}-r${i}"><summary><i class="bridge-caret"></i><span class="bridge-step-name">执行轨迹</span></summary><div class="bridge-term"><pre class="out">${esc(it.text)}</pre></div></details>`);
    } else if (it.kind === 'note') {
      out.push(`<div class="bridge-note${it.sub === 'error' ? ' error' : ''}">${esc(it.text)}</div>`);
    }
    i++;
  }
  return out.join('');
}

// ---- Lattice Loader (turn header / waiting state) ----
const LAT = { cells: [0, 1, 2, 7, null, 3, 6, 5, 4], step: 108, cycle: 864, done: [2, 3, 5, 7], stopped: [0, 2, 4, 6, 8] };

// Think time of one assistant turn → { state: 'live' | 'done' | 'stopped' | 'none', start, dur, coarse, pending }.
// Sources, in order: the `usage` event's duration_ms (the CLI's own wall time) → server timestamps (the first event, or the
// message's created_at, to the message's updated_at) → the client's own first-frame / done times (m._t0 / m._t1, stamped by renderTurn).
export function turnTiming(m, now = Date.now()) {
  const evs = m.events || [];
  const pending = m.status === 'pending';
  const live = pending || m.status === 'streaming';
  let start = pending ? null : parseUtc((evs.find(e => e.type === 'init') || evs[0])?.created_at);
  start ??= parseUtc(m.created_at);
  let coarse = true;
  if (start == null && m._t0 != null) { start = m._t0; coarse = false; }
  if (live) return { state: 'live', start: start ?? now, dur: null, coarse, pending };
  let dur = null;
  for (let i = evs.length - 1; i >= 0; i--) {
    const d = evs[i].type === 'usage' ? evs[i].data?.duration_ms : null;
    if (Number.isFinite(d) && d > 0) { dur = d; coarse = false; break; }
  }
  if (dur == null && start != null) {
    const end = m._t1 ?? parseUtc(m.updated_at);
    if (end != null && end >= start) dur = end - start;
  }
  const state = m.status === 'error' || m.status === 'cancelled' ? 'stopped' : 'done';
  return dur == null ? { state: 'none', start, dur, coarse, pending: false } : { state, start, dur, coarse, pending: false };
}

// Lattice Loader: 3×3 wave + stopwatch while the turn runs; frozen to "已思考 12.3s" (a tick / cross of cells) afterwards.
// The wave phase follows the wall clock so re-renders (every streamed chunk) don't restart it.
export function latticeHtml(t, S, now = Date.now()) {
  if (t.state === 'none') return '';
  const live = t.state === 'live';
  const label = live ? (t.pending ? S.waitingHelper : S.thinking) : t.state === 'stopped' ? S.stopped : S.thought;
  let cells;
  if (live) cells = LAT.cells.map((u) => u == null ? '<i data-hole></i>'
    : `<i style="animation-delay:-${(((now - u * LAT.step) % LAT.cycle) + LAT.cycle) % LAT.cycle}ms"></i>`).join('');
  else { const on = LAT[t.state]; cells = LAT.cells.map((_, i) => `<i${on.includes(i) ? ' data-on' : ''}></i>`).join(''); }
  const timer = live ? `<span class="bridge-lat-timer" data-timer>${fmtDur(Math.max(0, now - t.start))}</span>`
    : `<span class="bridge-lat-timer">${fmtDur(t.dur, t.coarse)}</span>`;
  return `<span class="bridge-think" data-k="think" data-st="${live ? 'running' : t.state === 'stopped' ? 'failed' : 'done'}"${live ? ` data-t0="${t.start}"` : ''}>`
    + `<span class="bridge-lat" aria-hidden="true">${cells}</span><span class="bridge-lat-label">${esc(label)}</span>${timer}</span>`;
}

export function renderSteps(el, items, opts = {}) { el.innerHTML = stepsHtml(items, opts); }

function messageModel(m) {
  let model = '';
  for (const ev of m.events || []) if ((ev.type === 'init' || ev.type === 'usage') && ev.data?.model) model = ev.data.model;
  return model.replace(/^claude-/, '');
}

// Renders one message into `el` (a .bridge-turn). Re-rendering keeps the user's expand/collapse choices.
// opts: { markdown, head (speaker line), timestamps, open (tools expanded by default), fileUrl(id) (image thumbnails), strings, lang ('zh'|'ja'|'en'), expected (tool run-time hints), now }
export function renderTurn(el, m, opts = {}) {
  const o = { markdown: defaultMarkdown, head: true, timestamps: true, open: false, strings: {}, now: Date.now(), ...opts };
  const S = { me: '我', assistant: 'Claude', ...messageStrings(o.lang), ...o.strings };
  el.className = `bridge-turn ${m.role} ${m.status || 'done'}`;
  el.dataset.id = m.id;
  if (m.role === 'system') { el.textContent = m.content || ''; return; }
  const opened = new Set(), closed = new Set();
  for (const d of el.querySelectorAll('details[data-k]')) (d.open ? opened : closed).add(d.dataset.k);
  // rows that just left 'running' replay their finish animation once (kept for a moment across the re-renders streaming causes)
  const settling = el._settling ||= new Map();
  for (const n of el.querySelectorAll('[data-k][data-st]')) {
    if (n.dataset.st !== 'running') continue;
    const k = n.dataset.k; if (!settling.has(k)) settling.set(k, null);
  }
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
    // client-side fallback clock for turns whose events carry no timestamps (first frame seen → done seen)
    if (streaming && m._t0 == null) m._t0 = o.now;
    else if (!streaming && m._t0 != null && m._t1 == null) m._t1 = o.now;
    const segs = splitTurn(m.content || '', m.events || []);
    body = segs.map((s, i) => s.kind === 'text'
      ? `<div class="bridge-md">${o.markdown(s.text)}</div>`
      : `<div class="bridge-steps">${stepsHtml(s.items, { streaming, open: o.open, key: `s${i}`, expected: o.expected, strings: S, now: o.now })}</div>`).join('');
    const think = latticeHtml(turnTiming(m, o.now), S, o.now);
    if (streaming && !segs.length) body = `<div class="bridge-wait">${think}</div>`;
    else if (think) body = `<div class="bridge-think-row">${think}</div>` + body;
  }
  el.innerHTML = head + `<div class="bridge-turn-body">${body}</div>`;
  for (const d of el.querySelectorAll('details[data-k]')) { const k = d.dataset.k; if (opened.has(k)) d.open = true; else if (closed.has(k)) d.open = false; }
  for (const n of el.querySelectorAll('[data-k][data-st]')) {
    const k = n.dataset.k;
    if (n.dataset.st === 'running') { settling.set(k, null); continue; }
    if (!settling.has(k)) continue;
    const t0 = settling.get(k) ?? (settling.set(k, o.now), o.now), age = o.now - t0;
    if (age > 800) { settling.delete(k); continue; }
    n.classList.add('just'); n.style.setProperty('--jd', `-${age}ms`);
  }
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

// ---------- confirmations (host-agnostic; DESIGN §8 of @szyyw/design, but no dependency on it) ----------

// Two-step confirm for a destructive button: the first click arms it (red, `label`, `warning` shown in `warnEl`,
// announced via role="alert"); a second click within `timeout` ms returns true. Every other call returns false.
// Arming one button disarms any other armed button in the same document.
export function armConfirm(btn, { label = '再点一次确认', warnEl = null, warning = '', timeout = 5000 } = {}) {
  if (btn.dataset.bridgeArmed != null) { disarmConfirm(btn); return true; }
  for (const other of document.querySelectorAll('[data-bridge-armed]')) disarmConfirm(other);
  btn.dataset.bridgeArmed = btn.textContent;
  btn.textContent = label;
  btn.classList.add('bridge-armed');
  btn._bridgeWarn = warnEl;
  if (warnEl) { warnEl.textContent = warning; warnEl.hidden = false; }
  btn._bridgeDisarm = setTimeout(() => disarmConfirm(btn), timeout);
  return false;
}
export function disarmConfirm(btn) {
  if (btn?.dataset.bridgeArmed == null) return;
  clearTimeout(btn._bridgeDisarm);
  btn.textContent = btn.dataset.bridgeArmed;
  delete btn.dataset.bridgeArmed;
  btn.classList.remove('bridge-armed');
  if (btn._bridgeWarn) { btn._bridgeWarn.hidden = true; btn._bridgeWarn = null; }
}

let dlgSeq = 0;
// A small modal text prompt (replaces window.prompt): resolves the entered string, or null on 取消 / ✕ / Esc / a click
// outside. role="dialog" + aria-modal, labelled by its title; focus goes to the input, Tab stays inside, and focus
// returns to `returnFocus` (default: what had it before) on close. Styled only with --bridge-* tokens.
export function promptDialog({ title, label = '', value = '', placeholder = '', okLabel = '保存', cancelLabel = '取消', maxLength = 200, returnFocus } = {}) {
  const back = returnFocus ?? document.activeElement;
  const id = `bridge-dlg-${++dlgSeq}`;
  const wrap = document.createElement('div');
  wrap.className = 'bridge-root bridge-dlg-scrim';
  wrap.innerHTML = `<form class="bridge-dlg" role="dialog" aria-modal="true" aria-labelledby="${id}-t">
      <div class="bridge-dlg-head"><span class="bridge-dlg-title" id="${id}-t">${esc(title)}</span>
        <button type="button" class="bridge-pop-x" data-dlg="cancel" aria-label="关闭" title="关闭">✕</button></div>
      ${label ? `<label class="bridge-dlg-label" for="${id}-i">${esc(label)}</label>` : ''}
      <input class="bridge-dlg-input" id="${id}-i" maxlength="${maxLength}" autocomplete="off" placeholder="${esc(placeholder)}"${label ? '' : ` aria-labelledby="${id}-t"`} />
      <div class="bridge-dlg-foot"><button type="button" class="bridge-btn" data-dlg="cancel">${esc(cancelLabel)}</button>
        <button type="submit" class="bridge-btn bridge-btn-primary">${esc(okLabel)}</button></div>
    </form>`;
  const form = wrap.firstElementChild, input = form.querySelector('input');
  input.value = value;
  document.body.appendChild(wrap);
  return new Promise((resolve) => {
    const done = (v) => {
      wrap.remove();
      if (back && typeof back.focus === 'function' && back.isConnected) back.focus();
      resolve(v);
    };
    form.addEventListener('submit', (e) => { e.preventDefault(); done(input.value); });
    wrap.addEventListener('click', (e) => { if (e.target === wrap || e.target.closest('[data-dlg="cancel"]')) done(null); });
    wrap.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); done(null); return; }
      if (e.key !== 'Tab') return;
      const f = [...form.querySelectorAll('input, button')].filter(x => !x.disabled);
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    });
    input.focus(); input.select();
  });
}

// ============================================================
// Composer: a framework-free port of two React Bits components — Prompt Bar (auto-growing input, send ⇄ stop glyph
// morph, `/` command and `@` source menus, model picker, effort slider with sparks at the top level) and Voice Pill
// (mic button that swells into a capsule with live equalizer bars and a timer; hold / toggle, slide left to cancel).
//   Source: React Bits — https://reactbits.dev · https://github.com/DavidHDev/react-bits (PromptBar, VoicePill),
//   licensed MIT + Commons Clause. Changes: React → DOM, `motion` → CSS transitions + requestAnimationFrame,
//   colours → --bridge-* tokens, bridge data (commands, models, efforts, uploads, Web Speech dictation).
//   Icons: path data from Hugeicons free icons (@hugeicons/core-free-icons, MIT, © 2025 Hugeicons), inlined as SVG.
// ============================================================

const HUGEICONS = {
  plus: '<path d="M12 4V20M20 12H4"/>',
  chevron: '<path d="M18 9.00005C18 9.00005 13.5811 15 12 15C10.4188 15 6 9 6 9"/>',
  sparkles: '<path d="M15 2L15.5387 4.39157C15.9957 6.42015 17.5798 8.00431 19.6084 8.46127L22 9L19.6084 9.53873C17.5798 9.99569 15.9957 11.5798 15.5387 13.6084L15 16L14.4613 13.6084C14.0043 11.5798 12.4202 9.99569 10.3916 9.53873L8 9L10.3916 8.46127C12.4201 8.00431 14.0043 6.42015 14.4613 4.39158L15 2Z"/><path d="M7 12L7.38481 13.7083C7.71121 15.1572 8.84275 16.2888 10.2917 16.6152L12 17L10.2917 17.3848C8.84275 17.7112 7.71121 18.8427 7.38481 20.2917L7 22L6.61519 20.2917C6.28879 18.8427 5.15725 17.7112 3.70827 17.3848L2 17L3.70827 16.6152C5.15725 16.2888 6.28879 15.1573 6.61519 13.7083L7 12Z"/>',
  tick: '<path d="M5 14L8.5 17.5L19 6.5"/>',
  mic: '<path d="M7 6.5C7 4.01472 9.01472 2 11.5 2C13.9853 2 16 4.01472 16 6.5V11.5C16 13.9853 13.9853 16 11.5 16C9.01472 16 7 13.9853 7 11.5V6.5Z"/><path d="M11.5 19H11.0828C7.57267 19 4.57706 16.4623 4 13M11.5 19H11.9172C15.4273 19 18.4229 16.4623 19 13M11.5 19V22"/>',
  left: '<path d="M15 6C15 6 9.00001 10.4189 9 12C8.99999 13.5812 15 18 15 18"/>',
  help: '<circle cx="12" cy="12" r="10"/><path d="M9.5 9.5C9.5 8.11929 10.6193 7 12 7C13.3807 7 14.5 8.11929 14.5 9.5C14.5 10.3569 14.0689 11.1131 13.4117 11.5636C12.7283 12.0319 12 12.6716 12 13.5"/><path d="M12.125 16.75H12M12.25 16.75C12.25 16.8881 12.1381 17 12 17C11.8619 17 11.75 16.8881 11.75 16.75C11.75 16.6119 11.8619 16.5 12 16.5C12.1381 16.5 12.25 16.6119 12.25 16.75Z"/>',
  image: '<circle cx="7.5" cy="7.5" r="1.5"/><path d="M2.5 12C2.5 7.52166 2.5 5.28249 3.89124 3.89124C5.28249 2.5 7.52166 2.5 12 2.5C16.4783 2.5 18.7175 2.5 20.1088 3.89124C21.5 5.28249 21.5 7.52166 21.5 12C21.5 16.4783 21.5 18.7175 20.1088 20.1088C18.7175 21.5 16.4783 21.5 12 21.5C7.52166 21.5 5.28249 21.5 3.89124 20.1088C2.5 18.7175 2.5 16.4783 2.5 12Z"/><path d="M5 21C9.37246 15.775 14.2741 8.88406 21.4975 13.5424"/>',
  file: '<path d="M8 17H16"/><path d="M8 13H12"/><path d="M13 2.5V3C13 5.82843 13 7.24264 13.8787 8.12132C14.7574 9 16.1716 9 19 9H19.5M20 10.6569V14C20 17.7712 20 19.6569 18.8284 20.8284C17.6569 22 15.7712 22 12 22C8.22876 22 6.34315 22 5.17157 20.8284C4 19.6569 4 17.7712 4 14V9.45584C4 6.21082 4 4.58831 4.88607 3.48933C5.06508 3.26731 5.26731 3.06508 5.48933 2.88607C6.58831 2 8.21082 2 11.4558 2C12.1614 2 12.5141 2 12.8372 2.11401C12.9044 2.13772 12.9702 2.165 13.0345 2.19575C13.3436 2.34355 13.593 2.593 14.0919 3.09188L18.8284 7.82843C19.4065 8.40649 19.6955 8.69552 19.8478 9.06306C20 9.4306 20 9.83935 20 10.6569Z"/>',
};
// Hugeicons path data as an inline <svg>; unknown names render nothing (hosts may pass their own markup instead).
export function hugeicon(name, size = 16, strokeWidth = 1.8) {
  const body = HUGEICONS[name]; if (!body) return '';
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="${strokeWidth}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
}

// `/word` or `@word` right before the caret. `/` only opens at the very start of the draft (bridge commands are
// actions, not text — a path like "see /usr" must not pop a menu); `@` after start or whitespace.
export function parseToken(beforeCaret) {
  const m = /(^|\s)([@/])([^\s@/]*)$/.exec(beforeCaret || '');
  if (!m) return null;
  const start = m.index + m[1].length;
  if (m[2] === '/' && start !== 0) return null;
  return { kind: m[2] === '@' ? 'at' : 'slash', query: m[3].toLowerCase(), start };
}

// Rows of the model menu, from the same list and rules as modelOptionsHtml(): optional group headers
// ("跟随 CLI 最新" / "固定版本"), and a saved value the helper no longer reports kept, flagged and selected.
export function modelMenuRows(models, current = '') {
  current = current || '';
  const rows = []; let group = null;
  for (const m of models || []) {
    const g = m.group || null;
    if (g !== group) { rows.push({ kind: g ? 'group' : 'sep', label: g || '' }); group = g; }
    rows.push({ kind: 'model', key: m.id, name: m.label, selected: m.id === current });
  }
  if (rows[0]?.kind === 'sep') rows.shift();
  if (current && !(models || []).some(m => m.id === current)) rows.push({ kind: 'model', key: current, name: current, tag: 'helper 的 CLI 不认', unknown: true, selected: true });
  return rows;
}
export function modelLabel(models, current = '') {
  const m = (models || []).find(x => x.id === (current || ''));
  return m ? m.label : (current || '默认');
}

// The effort slider runs over the non-empty levels (Faster → Smarter); '' (= the CLI's default) is a separate
// 「默认」 choice. index −1 = default. maxed = the top level, where the bar shows its sparks.
export function effortState(efforts, current = '') {
  const steps = (efforts || []).filter(Boolean);
  const index = steps.indexOf(current || '');
  return { steps, index, hasDefault: (efforts || []).includes(''), maxed: steps.length > 1 && index === steps.length - 1 };
}

// Built-in `/` commands = what the bridge actually does for a thread (its HTTP API), nothing the backend lacks.
// Host commands ({ name, description, run }) come after them.
export function composerCommands({ hasThread = false, settings = false, extra = [] } = {}) {
  const need = hasThread ? '' : '还没有对话';
  const rows = [
    { key: 'new', name: '/new', description: '开始一个新对话' },
    { key: 'compact', name: '/compact', description: need || '把历史压缩成摘要，让上下文变小（再确认一次）', disabled: !hasThread },
    { key: 'context', name: '/context', description: need || '查看当前会话的上下文构成与额度', disabled: !hasThread },
  ];
  if (settings) rows.push({ key: 'model', name: '/model', description: '切换模型' }, { key: 'effort', name: '/effort', description: '调整思考深度' });
  for (const [i, c] of (extra || []).entries()) {
    if (!c?.name) continue;
    rows.push({ key: `host:${i}`, name: c.name.startsWith('/') ? c.name : `/${c.name}`, description: c.description || '', host: c });
  }
  return rows;
}
export function filterCommands(rows, query = '') {
  const q = (query || '').toLowerCase();
  return rows.filter(c => c.name.replace(/^\//, '').toLowerCase().startsWith(q));
}
export function filterSources(rows, query = '') {
  const q = (query || '').toLowerCase();
  return (rows || []).filter(s => String(s.name || '').toLowerCase().includes(q));
}

// Send ⇄ stop glyph: one 7-point polygon morphing between an up arrow and a rounded square.
const ARROW_UP = [12, 4.5, 18.5, 11, 14.25, 11, 14.25, 19.5, 9.75, 19.5, 9.75, 11, 5.5, 11];
const SQUARE = [12, 6, 18, 6, 18, 12, 18, 18, 6, 18, 6, 12, 6, 6];
export function morphPath(t) {
  let d = '';
  for (let i = 0; i < ARROW_UP.length; i += 2) {
    const x = ARROW_UP[i] + (SQUARE[i] - ARROW_UP[i]) * t, y = ARROW_UP[i + 1] + (SQUARE[i + 1] - ARROW_UP[i + 1]) * t;
    d += `${i ? 'L' : 'M'}${x.toFixed(2)} ${y.toFixed(2)}`;
  }
  return `${d}Z`;
}
// cubic-bezier(x1, y1, x2, y2) as a function of progress (what motion's `ease: [..]` did upstream)
export function cubicBezier(x1, y1, x2, y2) {
  const bx = (t) => 3 * x1 * t * (1 - t) ** 2 + 3 * x2 * t * t * (1 - t) + t ** 3;
  const by = (t) => 3 * y1 * t * (1 - t) ** 2 + 3 * y2 * t * t * (1 - t) + t ** 3;
  return (x) => {
    if (x <= 0) return 0; if (x >= 1) return 1;
    let lo = 0, hi = 1, t = x;
    for (let i = 0; i < 24; i++) { t = (lo + hi) / 2; if (bx(t) < x) lo = t; else hi = t; }
    return by(t);
  };
}
const EASE_IN_OUT = cubicBezier(0.77, 0, 0.175, 1);
const reducedMotion = () => typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;

// ---------- Voice Pill: level meters ----------
const VOICE_LOOP = 4.8;
const SYLLABLES = [[0.1, 0.16, 0.9], [0.3, 0.12, 0.7], [0.5, 0.2, 1], [0.95, 0.14, 0.8], [1.15, 0.1, 0.6], [1.3, 0.22, 0.95], [1.9, 0.16, 0.85],
  [2.12, 0.12, 0.7], [2.3, 0.18, 0.9], [2.55, 0.1, 0.5], [3.05, 0.24, 1], [3.4, 0.12, 0.75], [3.6, 0.16, 0.9]];
const MIC_BINS = [[1, 4], [4, 11], [11, 33]];
const MIC_GAIN = 2.2;
// a speech-like envelope for `reactive: 'simulated'` (no microphone)
export function simulatedLevel(t) {
  const u = t % VOICE_LOOP;
  let a = 0.06;
  for (const [s, d, p] of SYLLABLES) { const x = (u - s) / d; if (x >= 0 && x <= 1) a = Math.max(a, p * 0.5 * (1 - Math.cos(2 * Math.PI * x))); }
  return a * (0.7 + 0.3 * Math.abs(Math.sin(2 * Math.PI * 7.1 * u)));
}
// loudness 0…~1 from an AnalyserNode's byte spectrum (speech bands only)
export function micLevel(buf) {
  let total = 0;
  for (const [lo, hi] of MIC_BINS) { let s = 0; for (let i = lo; i < hi; i++) s += buf[i] || 0; total += s / ((hi - lo) * 255); }
  return (total / MIC_BINS.length) * MIC_GAIN;
}
export const voiceClock = (ms) => { const s = Math.floor(Math.max(0, ms) / 1000); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };

// Web Speech: the constructor when the browser has one (Chrome / Edge / Safari), else null
export function speechRecognitionCtor(w = globalThis) { return w?.SpeechRecognition || w?.webkitSpeechRecognition || null; }
// widget locale → recognition language
export function speechLang(locale) {
  const l = String(locale || '').trim();
  if (/^zh/i.test(l)) return 'zh-CN';
  if (/^ja/i.test(l)) return 'ja-JP';
  if (/^en/i.test(l)) return 'en-US';
  return l || 'zh-CN';
}
// draft text while dictating: what was there before + the recognised words (a space only between Latin words)
export function joinDictation(base, text, lang = 'zh-CN') {
  text = (text || '').trim(); base = base || '';
  if (!text) return base;
  if (!base || /\s$/.test(base)) return base + text;
  return base + (/^(zh|ja)/i.test(lang) ? '' : ' ') + text;
}
export const VOICE_ERRORS = {
  'not-allowed': '麦克风权限被拒绝：在浏览器的网站设置里允许使用麦克风后再试',
  'service-not-allowed': '浏览器不允许这个页面使用语音识别',
  'audio-capture': '没有找到可用的麦克风',
  'no-speech': '没有听到声音',
  network: '语音识别服务连不上（浏览器的语音识别需要联网）',
  'language-not-supported': '浏览器不支持这种语言的语音识别',
};

async function openMic(s) {
  const Ctx = globalThis.AudioContext || globalThis.webkitAudioContext;
  if (!Ctx || !navigator.mediaDevices?.getUserMedia) return null;      // insecure context / very old browser: no levels
  s.audio ??= { ctx: new Ctx() };
  const a = s.audio;
  if (a.ctx.state === 'suspended') a.ctx.resume?.();
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  if (!s.listening) { stream.getTracks().forEach(t => t.stop()); return null; }
  a.stream = stream;
  a.src = a.ctx.createMediaStreamSource(stream);
  a.analyser = a.ctx.createAnalyser();
  a.analyser.fftSize = 256;
  a.analyser.smoothingTimeConstant = 0;
  a.src.connect(a.analyser);
  a.buf = new Uint8Array(a.analyser.frequencyBinCount);
  return stream;
}
function closeMic(s) {
  const a = s.audio;
  if (!a?.stream) return;
  a.stream.getTracks().forEach(t => t.stop());
  a.src?.disconnect();
  a.stream = a.src = a.analyser = a.buf = null;
}

export const VOICE_DEFAULTS = {
  size: 28, shape: 'pill', reach: 8, showTime: true, waveform: true, slideToCancel: true, cancelDistance: 64,
  attack: 40, release: 240, sensitivity: 1, floor: 0.1, openDuration: 200, pressScale: 0.95,
  mode: 'auto', holdAfter: 300, reactive: 'mic', disabled: false, ariaLabel: '语音输入', cancelLabel: '取消',
  accentColor: '', iconColor: '', background: '',
};

// Turns `btn` (an empty <button>) into a Voice Pill. opts = VOICE_DEFAULTS + onStart({source}),
// onStop({reason, duration}) (reason: release | tap | key | escape | blur | disabled | mic-denied | cancel | unmount,
// plus the host's own via api.stop(reason)), onError(err). mode 'auto': a press longer than holdAfter is hold-to-talk,
// a short tap toggles; 'hold' / 'toggle' force one. reactive 'mic' draws the real microphone level (getUserMedia +
// AnalyserNode), 'simulated' a speech-like envelope. Returns { start(), stop(reason), stream(), setDisabled(on),
// destroy(), listening }.
export function mountVoicePill(btn, options = {}) {
  const c = { ...VOICE_DEFAULTS, ...options };
  const size = Number(c.size) || 28;
  const radius = c.shape === 'rounded' ? Math.round(size * 0.29) : size / 2;
  const hit = Math.max(0, Math.min(10, (44 - size) / 2));
  const timeSize = Math.max(10, Math.round(size * 0.36));
  const clockW = c.showTime ? Math.round(timeSize * 2.5) + 4 : 0;
  const waveW = c.waveform ? Math.round(size * 1.9) : 0;
  btn.type = 'button';
  btn.classList.add('bridge-voice');
  btn.setAttribute('aria-label', c.ariaLabel); btn.title = c.ariaLabel;
  btn.setAttribute('aria-pressed', 'false');
  btn.dataset.state = 'idle'; btn.dataset.input = 'pointer';
  if (c.showTime) btn.dataset.time = '';
  const vars = { '--vp-size': `${size}px`, '--vp-radius': `${radius}px`, '--vp-reach': `${c.reach}px`, '--vp-extra': `${clockW + waveW}px`,
    '--vp-clock-w': `${clockW}px`, '--vp-wave-w': `${waveW}px`, '--vp-stop': `${Math.round(size * 0.32)}px`,
    '--vp-time-size': `${timeSize}px`, '--vp-open': `${c.openDuration}ms`, '--vp-press': String(c.pressScale), '--vp-hit': `${hit}px` };
  if (c.accentColor) vars['--vp-accent'] = c.accentColor;
  if (c.iconColor) vars['--vp-icon'] = c.iconColor;
  if (c.background) vars['--vp-bg'] = c.background;
  for (const [k, v] of Object.entries(vars)) btn.style.setProperty(k, v);
  btn.innerHTML = '<span class="bridge-voice-capsule" aria-hidden="true"></span>'
    + (c.waveform ? '<canvas class="bridge-voice-wave" aria-hidden="true"></canvas>' : '')
    + (c.slideToCancel ? `<span class="bridge-voice-cancel" aria-hidden="true">${hugeicon('left', 12, 2.2)}<span>${esc(c.cancelLabel)}</span></span>` : '')
    + (c.showTime ? '<span class="bridge-voice-time" aria-hidden="true">0:00</span>' : '')
    + `<span class="bridge-voice-glyph"><span class="bridge-voice-mic">${hugeicon('mic', Math.round(size * 0.54), 2)}</span><span class="bridge-voice-stop" aria-hidden="true"></span></span>`;
  const timeEl = btn.querySelector('.bridge-voice-time'), wave = btn.querySelector('.bridge-voice-wave');
  const s = { listening: false, pointerId: null, ownPress: false, downX: 0, sliding: false, hist: [], tick: 0, acc: 0, downAt: 0, startedAt: 0,
    raf: 0, last: 0, env: 0, t0: 0, audio: null, streamP: Promise.resolve(null), disabled: !!c.disabled, color: '' };
  const WAVE_EVERY = 4, WAVE_MAX = 80;

  function drawWave(level) {
    const dpr = Math.min(2, globalThis.devicePixelRatio || 1);
    const rect = wave.getBoundingClientRect();
    const W = Math.max(1, Math.round(rect.width * dpr)), H = Math.max(1, Math.round(rect.height * dpr));
    if (wave.width !== W || wave.height !== H) { wave.width = W; wave.height = H; }
    const ctx = wave.getContext('2d'); if (!ctx) return;
    s.acc = Math.max(s.acc, level);
    s.tick = (s.tick + 1) % WAVE_EVERY;
    if (s.tick === 0) { s.hist.push(s.acc); s.acc = 0; if (s.hist.length > WAVE_MAX) s.hist.shift(); }
    const bw = 2 * dpr, step = 3 * dpr, shift = (s.tick / WAVE_EVERY) * step;
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = s.color;
    for (let i = 0; i < s.hist.length; i++) {
      const v = s.hist[s.hist.length - 1 - i];
      const x = W - (i + 1) * step - shift;
      if (x + bw < 0) break;
      const h = Math.max(bw, (c.floor + (1 - c.floor) * v) * H);
      const t = Math.min(1, Math.max(0, (x + bw / 2) / (W * 0.55)));
      ctx.globalAlpha = (0.35 + 0.65 * v) * t * t * (3 - 2 * t);
      ctx.beginPath();
      if (ctx.roundRect) ctx.roundRect(x, (H - h) / 2, bw, h, bw / 2); else ctx.rect(x, (H - h) / 2, bw, h);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
  }
  function frame(now) {
    const dt = Math.min((now - s.last) / 1000, 0.05);
    s.last = now;
    let target = 0;
    if (s.listening) {
      if (s.audio?.analyser) { s.audio.analyser.getByteFrequencyData(s.audio.buf); target = micLevel(s.audio.buf); }
      else if (c.reactive !== 'mic') target = simulatedLevel((now - s.t0) / 1000);
    }
    target = Math.min(1, target * c.sensitivity);
    const tau = Math.max(1, target > s.env ? c.attack : c.release) / 1000;
    s.env += (target - s.env) * (1 - Math.exp(-dt / tau));
    if (s.listening && timeEl) { const text = voiceClock(now - s.startedAt); if (timeEl.textContent !== text) timeEl.textContent = text; }
    if (s.listening && wave) drawWave(s.env);
    s.raf = s.listening ? requestAnimationFrame(frame) : 0;
  }
  function begin(kind) {
    if (s.listening || s.disabled) return;
    s.listening = true;
    s.hist = []; s.tick = 0; s.acc = 0; s.env = 0;
    s.startedAt = s.t0 = s.last = performance.now();
    if (timeEl) timeEl.textContent = '0:00';
    btn.dataset.state = 'listening'; btn.dataset.input = kind; btn.setAttribute('aria-pressed', 'true');
    s.color = getComputedStyle(btn).color;          // the accent, resolved (canvas can't read var())
    if (!s.raf) s.raf = requestAnimationFrame(frame);
    s.streamP = c.reactive === 'mic' ? openMic(s).catch((err) => { c.onError?.(err); end('mic-denied'); return null; }) : Promise.resolve(null);
    c.onStart?.({ source: c.reactive });
  }
  function end(reason) {
    if (!s.listening) return;
    s.listening = false;
    closeMic(s);
    btn.dataset.state = 'idle'; btn.setAttribute('aria-pressed', 'false');
    btn.dataset.input = reason === 'key' || reason === 'escape' ? 'key' : 'pointer';
    c.onStop?.({ reason, duration: Math.round(performance.now() - s.startedAt) });
  }
  function settleSlide() {
    s.sliding = false; delete btn.dataset.sliding;
    btn.style.setProperty('--vp-slide', '0px'); btn.style.setProperty('--vp-cancel', '0');
  }
  const setPressed = (on) => { if (on) btn.dataset.pressed = ''; else delete btn.dataset.pressed; };
  const onMove = (e) => {
    if (s.pointerId !== e.pointerId || !c.slideToCancel || !s.listening || !s.ownPress) return;
    const dx = e.clientX - s.downX;
    if (!s.sliding && dx > -4) return;
    s.sliding = true; btn.dataset.sliding = '';
    const pull = Math.min(c.cancelDistance + 24, Math.max(0, -dx));
    btn.style.setProperty('--vp-slide', `${-pull}px`);
    const progress = Math.min(1, pull / c.cancelDistance);
    btn.style.setProperty('--vp-cancel', progress.toFixed(3));
    if (progress >= 1) { settleSlide(); end('cancel'); }
  };
  const onDown = (e) => {
    if (s.disabled || e.button !== 0 || !e.isPrimary || s.pointerId !== null) return;
    s.pointerId = e.pointerId; s.downX = e.clientX; s.downAt = performance.now();
    try { btn.setPointerCapture(e.pointerId); } catch { /* synthetic events */ }
    setPressed(true);
    s.ownPress = !s.listening;
    if (!s.listening) begin('pointer');
  };
  const onUp = (e) => {
    if (e.pointerId !== s.pointerId) return;
    s.pointerId = null; setPressed(false);
    if (s.sliding) settleSlide();
    try { if (btn.hasPointerCapture(e.pointerId)) btn.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
    if (!s.listening) return;
    const held = performance.now() - s.downAt;
    const isHold = c.mode === 'hold' || (c.mode === 'auto' && held >= c.holdAfter);
    if (s.ownPress) { if (isHold) end('release'); }
    else end(held < c.holdAfter ? 'tap' : 'release');
  };
  const onKey = (e) => {
    if (e.key === 'Escape') { if (s.listening) { e.stopPropagation(); end('escape'); } return; }
    if ((e.key === ' ' || e.key === 'Enter') && !e.repeat) { e.preventDefault(); if (s.listening) end('key'); else begin('key'); }
  };
  const onClick = (e) => {   // keyboard / assistive "click" without a pointer
    if (e.detail === 0 && s.pointerId === null && !e.pointerType) { if (s.listening) end('key'); else begin('key'); }
  };
  const onCtx = (e) => e.preventDefault();
  const onBlur = () => { if (btn.dataset.pressed != null) { setPressed(false); s.pointerId = null; end('blur'); } };
  const onVis = () => { if (document.hidden) onBlur(); };
  btn.addEventListener('pointerdown', onDown); btn.addEventListener('pointermove', onMove);
  btn.addEventListener('pointerup', onUp); btn.addEventListener('pointercancel', onUp); btn.addEventListener('lostpointercapture', onUp);
  btn.addEventListener('keydown', onKey); btn.addEventListener('click', onClick); btn.addEventListener('contextmenu', onCtx);
  globalThis.addEventListener?.('blur', onBlur); document.addEventListener('visibilitychange', onVis);
  btn.disabled = s.disabled;
  return {
    start: () => begin('key'),
    stop: (reason = 'key') => end(reason),
    stream: () => s.streamP,
    setDisabled(on) { s.disabled = !!on; btn.disabled = s.disabled; if (on) end('disabled'); },
    get listening() { return s.listening; },
    destroy() {
      end('unmount'); cancelAnimationFrame(s.raf); s.audio?.ctx.close?.();
      btn.removeEventListener('pointerdown', onDown); btn.removeEventListener('pointermove', onMove);
      btn.removeEventListener('pointerup', onUp); btn.removeEventListener('pointercancel', onUp); btn.removeEventListener('lostpointercapture', onUp);
      btn.removeEventListener('keydown', onKey); btn.removeEventListener('click', onClick); btn.removeEventListener('contextmenu', onCtx);
      globalThis.removeEventListener?.('blur', onBlur); document.removeEventListener('visibilitychange', onVis);
    },
  };
}

// One dictation session into a text field. Web Speech by default (interim results shown live, finals kept), or the
// host's `transcribe({ lang, stream, signal, onPartial })` → Promise<string> (stream = Promise<MediaStream|null>,
// signal aborts when the user stops; signal.reason === 'cancel' means discard). Calls write(text) with the full
// recognised text so far, done({ text, cancelled }) once, error(message) on failures.
export function startDictation({ lang, transcribe = null, stream = null, write, done, error, Recognition = speechRecognitionCtor() }) {
  let finals = '', interim = '', finished = false, cancelled = false;
  const finish = () => { if (finished) return; finished = true; done?.({ text: cancelled ? '' : (finals + interim).trim(), cancelled }); };
  if (typeof transcribe === 'function') {
    const ctrl = new AbortController();
    Promise.resolve().then(() => transcribe({ lang, stream, signal: ctrl.signal, onPartial: (t) => { if (!cancelled) { interim = t || ''; write?.(interim); } } }))
      .then((t) => { if (!cancelled && t != null) { finals = String(t); interim = ''; } finish(); },
        (e) => { if (!cancelled) error?.(e?.message || String(e)); finish(); });
    return { stop(reason) { if (reason === 'cancel') cancelled = true; ctrl.abort(reason); if (cancelled) finish(); } };
  }
  const rec = new Recognition();
  rec.lang = lang; rec.interimResults = true; rec.continuous = true; rec.maxAlternatives = 1;
  rec.onresult = (e) => {
    finals = ''; interim = '';
    for (let i = 0; i < e.results.length; i++) { const r = e.results[i]; if (r.isFinal) finals += r[0].transcript; else interim += r[0].transcript; }
    if (!cancelled) write?.(finals + interim);
  };
  rec.onerror = (e) => { if (e.error !== 'aborted' && !cancelled) error?.(VOICE_ERRORS[e.error] || `语音识别出错（${e.error}）`, e.error); };
  rec.onend = finish;
  try { rec.start(); } catch (e) { error?.(e.message); finish(); }
  // after stop() the browser still delivers the last results, then `end`; don't hang if it never comes
  return { stop(reason) { if (finished) return; if (reason === 'cancel') { cancelled = true; try { rec.abort(); } catch { /* ignore */ } finish(); return; }
    try { rec.stop(); } catch { /* ignore */ } setTimeout(finish, 2500); } };
}

// ============================================================
// Reference widget
// ============================================================

export function mountBridgeWidget(el, client, opts = {}) {
  const o = {
    scope: '', title: 'Claude', markdown: defaultMarkdown, showThreads: true, showSettings: true,
    contextWindows: CONTEXT_WINDOWS, placeholder: '输入消息', prefsKey: 'bridge_ui', strings: {},
    maxRows: 12, locale: '', voice: true, sources: null, commands: [], onSource: null, ...opts,
  };
  const S = { threadTitle: '新对话', helperOff: 'helper 离线：消息会排队', polling: '轮询模式',
    waitingHelper: '等待 helper 接单…', thinking: 'Claude 正在思考…', empty: '问点什么吧', newThread: '＋ 新对话',
    rename: '重命名', pin: '置顶', unpin: '取消置顶', del: '删除对话', delConfirm: '删除这个对话？消息记录会一起删除。', delArm: '再点一次确认删除', renameLabel: '对话标题', save: '保存', cancel: '取消', stop: '停止', me: '我', assistant: 'Claude',
    ctx: '自动带背景', send: '发送', voice: '语音输入', listening: '正在听…', attach: '添加图片或来源', image: '图片', imageHint: '从本机选择，也可以粘贴或拖进来',
    model: '模型', effort: '思考深度', effortDefault: '默认', faster: '更快', smarter: '更聪明', effortHelp: '越深想得越久再回答；「默认」跟随 helper 的 claude 配置',
    noMatch: '没有匹配', compactArm: '再点一次确认压缩', compactWarn: 'Claude 会把这段对话的历史压缩成摘要：细节会丢失，但上下文会明显变小。', ...o.strings };
  // voice input: on unless the host says `voice: false`; shown only where it can work (Web Speech, or the host's transcriber)
  const voiceCfg = o.voice === false ? null : (o.voice && typeof o.voice === 'object' ? o.voice : {});
  const voiceOn = !!voiceCfg && (typeof voiceCfg.transcribe === 'function' || !!speechRecognitionCtor());
  const hasSources = () => typeof o.sources === 'function' || (Array.isArray(o.sources) && o.sources.length > 0);
  const prefs = loadPrefs(o.prefsKey);
  const menuId = `bridge-pb-menu-${++dlgSeq}`;

  el.innerHTML = `
    <div class="bridge ${o.showThreads ? '' : 'no-side'}${o.fill ? ' fill' : ''}">
      ${o.showThreads ? `<aside class="bridge-side">${o.sideHead ? `<div class="bridge-side-head">${o.sideHead}</div>` : ''}<button type="button" class="bridge-btn bridge-new" data-act="new">${esc(S.newThread)}</button><div class="bridge-threads"></div>${o.sideFoot ? `<div class="bridge-side-foot">${o.sideFoot}</div>` : ''}<div class="bridge-side-grip" title="拖动调整宽度"></div></aside><div class="bridge-side-scrim" data-act="side-close"></div>` : ''}
      <div class="bridge-main">
        <div class="bridge-top">
          ${o.showThreads ? '<button type="button" class="bridge-icon" data-act="side" title="对话列表">☰</button>' : ''}
          ${o.topStart || ''}
          <span class="bridge-title">${esc(o.title)}</span>
          <span class="bridge-agent"><i class="bridge-dot"></i><span class="txt"></span></span>
          ${o.showThreads ? `<button type="button" class="bridge-icon" data-act="menu" title="更多">⋯</button>
          <div class="bridge-menu" hidden><button type="button" data-act="rename">${esc(S.rename)}</button><button type="button" data-act="pin">${esc(S.pin)}</button><button type="button" class="danger" data-act="del">${esc(S.del)}</button><p class="bridge-menu-warn" role="alert" hidden></p></div>` : ''}
          ${o.topEnd || ''}
        </div>
        <div class="bridge-list"><div class="bridge-empty">${esc(S.empty)}</div></div>
        <form class="bridge-form bridge-pb">
          <div class="bridge-grip" title="拖动调整输入框高度，双击恢复自动"></div>
          <div class="bridge-pb-menu" id="${menuId}" hidden></div>
          <canvas class="bridge-pb-sparks" aria-hidden="true"></canvas>
          <div class="bridge-tray" hidden></div>
          <textarea class="bridge-input" rows="1" placeholder="${esc(o.placeholder)}" aria-label="${esc(o.placeholder)}" aria-controls="${menuId}" aria-autocomplete="list"></textarea>
          <div class="bridge-bar">
            <button type="button" class="bridge-icon" data-act="prefs" title="设置" aria-label="设置"><svg viewBox="0 0 16 16" width="1.1em" height="1.1em" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" aria-hidden="true"><path d="M2 4h7M13 4h1M2 8h2M8 8h6M2 12h8"/><circle cx="11" cy="4" r="1.7"/><circle cx="6" cy="8" r="1.7"/><circle cx="12" cy="12" r="1.7"/></svg></button>
            <button type="button" class="bridge-icon bridge-pb-tool" data-pb="plus" title="${esc(S.attach)}" aria-label="${esc(S.attach)}" aria-expanded="false" hidden>${hugeicon('plus', 16, 2)}</button>
            <input type="file" class="bridge-file" accept="image/*" multiple hidden />
            ${o.showSettings ? `<button type="button" class="bridge-pb-pick" data-pb="model" title="${esc(S.model)}" aria-label="${esc(S.model)}" aria-haspopup="listbox" aria-expanded="false" hidden><span class="lbl"></span>${hugeicon('chevron', 12, 2.4)}</button>`
              + `<button type="button" class="bridge-pb-pick" data-pb="effort" title="${esc(S.effort)}" aria-label="${esc(S.effort)}" aria-haspopup="dialog" aria-expanded="false" hidden>${hugeicon('sparkles', 13, 2)}<span class="lbl"></span></button>` : ''}
            <span class="grow"></span>
            <button type="button" class="bridge-pill" data-act="ctx" hidden title="当前会话上下文占用，点开看构成与额度"><span class="pct"></span><i class="ring"></i></button>
            ${voiceOn ? '<button type="button" class="bridge-pb-voice"></button>' : ''}
            <button type="submit" class="bridge-btn bridge-send" title="${esc(S.send)}" aria-label="${esc(S.send)}" disabled><svg class="bridge-pb-glyph" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="2" stroke-linejoin="round" aria-hidden="true"><path d="${morphPath(0)}"/></svg></button>
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
  const list = $('.bridge-list'), input = $('.bridge-input'), sendBtn = $('.bridge-send'), pill = $('[data-act="ctx"]');
  const form = $('.bridge-form'), pbMenu = $('.bridge-pb-menu'), fileInput = $('.bridge-file'), plusBtn = $('[data-pb="plus"]');
  let voice = null;      // Voice Pill api (set up with the composer below)
  const state = { thread: null, threadRow: null, msgs: new Map(), order: [], sub: null, inflight: null, settings: null, raf: null, dirty: new Set(), ctxBusy: false, agent: null, job: null, ticker: null };
  // the ＋ button opens the file picker itself (or the sources menu when the host gave sources), so no `button` here
  const att = mountAttachments({
    input: fileInput, tray: $('.bridge-tray'), textarea: input, dropZone: form, client,
    onChange: () => syncSend(), onError: (msg) => toast(msg, true),
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
    syncSend();
    if (!voice?.listening) input.placeholder = on ? 'Claude 正在回答，稍等…' : placeholder();
  }

  function usePrefs(key) {
    applyPrefs(root, prefs);
    savePrefs(prefs, o.prefsKey);
    if (key === 'sendKey' || !key) input.placeholder = state.inflight ? input.placeholder : placeholder();
    if (key === 'timestamps' || key === 'toolsOpen' || !key) for (const id of state.order) paint(id);
    if (key === 'inputH' || !key) { if (prefs.inputH) { input.style.height = `${prefs.inputH}px`; input.style.overflowY = ''; input.classList.add('fixed'); } else { input.classList.remove('fixed'); autoGrow(); } }
  }

  // ---------- rendering ----------
  function turnEl(m) { const d = document.createElement('div'); d.className = `bridge-turn ${m.role}`; d.dataset.id = m.id; return d; }
  function paint(id) {
    const m = state.msgs.get(id); if (!m) return;
    const b = list.querySelector(`.bridge-turn[data-id="${id}"]`); if (!b) return;
    const follow = nearBottom();
    renderTurn(b, m, { markdown: o.markdown, timestamps: prefs.timestamps, open: prefs.toolsOpen, fileUrl: (id) => client.fileUrl(id), lang: o.lang, expected: o.toolExpected,
      strings: { me: S.me, assistant: S.assistant, ...(o.strings.waitingHelper && { waitingHelper: o.strings.waitingHelper }), ...(o.strings.thinking && { thinking: o.strings.thinking }), ...(o.strings.thought && { thought: o.strings.thought }), ...(o.strings.stopped && { stopped: o.strings.stopped }) } });
    if (follow) scrollBottom();
    syncTicker();
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
    syncTicker();
  }
  // One ticker for the compact-job timer and every running chip / think timer; runs only while something is live and the page is visible.
  function syncTicker() {
    const need = jobActive(state.job) || !!el.querySelector('[data-t0]');
    if (need && !state.ticker) state.ticker = setInterval(() => { if (document.hidden || !el.isConnected) return; tickJobs(el); tickJobs(pops.ctx); }, 250);
    if (!need && state.ticker) { clearInterval(state.ticker); state.ticker = null; }
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
        + `<span class="p">${esc(t.preview || '')}</span><span class="m">${t.n ?? 0} 条 · ${esc(fmtRelative(t.updated_at))}${quotaText(t.quota_pct)}</span></button>`).join('');
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
      cp.effort = s.chat.effort || '';
      renderPickers();
      if (cp.menu === 'model' || cp.menu === 'effort') renderMenu();
      renderAgent(s.agent);
      att.setEnabled(!!s.uploads?.enabled);
      cp.uploads = !!s.uploads?.enabled; plusBtn.hidden = !(cp.uploads || hasSources());
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
  function closePops() { for (const p of Object.values(pops)) p.hidden = true; if (menu) { menu.hidden = true; disarmConfirm(menu.querySelector('[data-act="del"]')); } scrim.hidden = true; setScrollLock(false); }
  scrim.addEventListener('click', closePops);

  // ---------- actions ----------
  async function send() {
    if (voice?.listening) { voice.stop('key'); return; }   // Enter while dictating ends the dictation; send after a look
    const text = input.value.trim();
    if (att.busy()) { toast('图片还在上传，稍等'); return; }
    if (!text && !att.ids().length) return;
    sendBtn.disabled = true;
    try {
      const files = att.ids();
      const r = await client.send(text, state.thread ? { thread: state.thread, files } : { scope: o.scope, files });
      input.value = ''; autoGrow(); att.clear(); closeMenu(); cp.dismissed = false;
      if (r.thread !== state.thread) subscribe(r.thread);
    } catch (e) { toast(e.message, true); }
    syncSend();
  }
  // grows with the text up to maxRows lines (and never past 40% of the viewport), then scrolls
  function autoGrow() {
    if (prefs.inputH) return;
    const cs = getComputedStyle(input);
    const line = parseFloat(cs.lineHeight) || 20, pad = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0);
    const cap = Math.min(line * Math.max(1, Number(o.maxRows) || 12) + pad, window.innerHeight * 0.4);
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, cap)}px`;
    input.style.overflowY = input.scrollHeight > cap + 1 ? 'auto' : 'hidden';
  }
  async function newThread() { const r = await client.createThread({ scope: o.scope }); subscribe(r.thread); loadThreads(); root.classList.remove('side-open'); }

  // ============ composer (Prompt Bar) ============
  // cp.menu: null | 'slash' | 'at' | 'plus' | 'model' | 'effort'. List menus keep focus in the textarea (↑↓ / Enter / Tab
  // / Esc work there); the effort menu is a slider that takes focus.
  const cp = { menu: null, rows: [], cursor: 0, token: null, dismissed: false, armed: null, armTimer: 0, uploads: false, effort: '',
    effortTimer: 0, srcSeq: 0, lastOpen: null, glyphT: 0, glyphRaf: 0, busy: false, sparks: null, energy: 0, strokes: 0 };
  const caretText = () => input.value.slice(0, input.selectionStart ?? input.value.length);
  const optionRows = () => cp.rows.filter(r => r.kind !== 'group' && r.kind !== 'sep');
  const setExpanded = () => {
    for (const b of form.querySelectorAll('[data-pb]')) {
      const on = cp.menu === b.dataset.pb;
      b.setAttribute('aria-expanded', String(on));
      if (on) b.dataset.on = ''; else delete b.dataset.on;
    }
    input.setAttribute('aria-expanded', String(!!cp.menu && cp.menu !== 'effort'));
  };

  function syncSend() {
    const busy = !!state.inflight;
    const can = !!input.value.trim() || att.count() > 0;
    const armed = busy || can;
    if (armed) sendBtn.dataset.armed = ''; else delete sendBtn.dataset.armed;
    sendBtn.disabled = !armed || (!busy && att.busy());
    const label = busy ? S.stop : S.send;
    sendBtn.title = label; sendBtn.setAttribute('aria-label', label);
    if (busy) form.dataset.busy = ''; else delete form.dataset.busy;
    if (busy !== cp.busy) { cp.busy = busy; morphGlyph(busy ? 1 : 0); }
  }
  // arrow ⇄ square with a little squash and tilt halfway (upstream: motion + ease [.77,0,.175,1], 240 ms)
  function morphGlyph(target) {
    const svg = sendBtn.querySelector('svg'), path = svg.querySelector('path');
    cancelAnimationFrame(cp.glyphRaf);
    const from = cp.glyphT, dir = target > from ? 1 : -1, dur = Number(o.morphDuration) || 240;
    const squash = o.squash ?? 0.12, tilt = o.tilt ?? 8;
    const paintAt = (t) => {
      cp.glyphT = t;
      path.setAttribute('d', morphPath(t));
      const goo = reducedMotion() ? 0 : Math.sin(t * Math.PI), sx = 1 - squash * goo;
      svg.style.transform = goo ? `rotate(${dir * tilt * goo}deg) scale(${sx}, ${1 / sx})` : '';
    };
    if (reducedMotion() || from === target) { paintAt(target); return; }
    const t0 = performance.now();
    const step = (now) => {
      const k = Math.min(1, (now - t0) / dur);
      paintAt(from + (target - from) * EASE_IN_OUT(k));
      cp.glyphRaf = k < 1 ? requestAnimationFrame(step) : 0;
    };
    cp.glyphRaf = requestAnimationFrame(step);
  }

  // ---------- pickers: model / effort (same data as the ⚙ selects) ----------
  function renderPickers() {
    const s = state.settings;
    const mb = $('[data-pb="model"]'), eb = $('[data-pb="effort"]');
    if (!mb || !eb) return;
    mb.hidden = !s || !(s.models || []).length;
    eb.hidden = !s || !(s.efforts || []).length;
    if (!s) return;
    const label = modelLabel(s.models, s.chat.model);
    mb.querySelector('.lbl').textContent = label; mb.title = `${S.model}：${label}`;
    const es = effortState(s.efforts, cp.effort);
    eb.querySelector('.lbl').textContent = cp.effort || S.effortDefault; eb.title = `${S.effort}：${cp.effort || S.effortDefault}`;
    if (es.maxed) { eb.dataset.max = ''; form.dataset.max = ''; } else { delete eb.dataset.max; delete form.dataset.max; }
    sparks(es.maxed);
  }

  // ---------- menus ----------
  function closeMenu() {
    if (!cp.menu) return;
    cp.menu = null; cp.rows = []; cp.token = null; cp.lastOpen = null;
    clearTimeout(cp.armTimer); cp.armed = null;
    pbMenu.hidden = true; pbMenu.innerHTML = '';
    input.removeAttribute('aria-activedescendant');
    setExpanded();
  }
  function openMenu(kind, rows = [], token = null, cursor = 0) {
    const fresh = cp.menu !== kind;
    cp.menu = kind; cp.rows = rows; cp.token = token;
    if (fresh) { clearTimeout(cp.armTimer); cp.armed = null; }
    const n = optionRows().length;
    cp.cursor = Math.max(0, Math.min(fresh ? cursor : cp.cursor, n - 1));
    renderMenu();
    setExpanded();
  }
  function commandRows() { return composerCommands({ hasThread: !!state.thread, settings: !!(o.showSettings && state.settings), extra: o.commands || [] }); }
  async function sourceRows(query) {
    if (typeof o.sources === 'function') return (await o.sources(query)) || [];
    return filterSources(o.sources, query);
  }
  // what the text before the caret asks for: `/cmd` → commands, `@src` → sources (only when the host gave any)
  async function syncToken() {
    if (cp.menu && !['slash', 'at'].includes(cp.menu)) return;
    const tok = cp.dismissed ? null : parseToken(caretText());
    if (tok?.kind === 'slash' && o.commands !== false) { openMenu('slash', filterCommands(commandRows(), tok.query), tok); return; }
    if (tok?.kind === 'at' && hasSources()) {
      const seq = ++cp.srcSeq;
      let rows = [];
      try { rows = await sourceRows(tok.query); } catch (e) { toast(e.message, true); }
      if (seq === cp.srcSeq && parseToken(caretText())?.kind === 'at' && !cp.dismissed) openMenu('at', rows.map((r, i) => ({ key: r.key ?? `s${i}`, ...r })), tok);
      return;
    }
    if (cp.menu) closeMenu();
  }
  function renderMenu() {
    const kind = cp.menu;
    if (!kind) return;
    pbMenu.hidden = false; pbMenu.dataset.kind = kind;
    if (kind === 'effort') { renderEffort(); return; }
    pbMenu.setAttribute('role', 'listbox');
    pbMenu.setAttribute('aria-label', { slash: '命令', at: '来源', plus: '添加', model: S.model }[kind]);
    let oi = -1;
    const html = cp.rows.map((r) => {
      if (r.kind === 'group') return `<div class="bridge-pb-group" role="presentation">${esc(r.label)}</div>`;
      if (r.kind === 'sep') return '<div class="bridge-pb-sep" role="presentation"></div>';
      oi++;
      const armed = kind === 'slash' && cp.armed === r.key;
      const icon = kind === 'at' || kind === 'plus'
        ? `<span class="bridge-pb-row-icon">${r.attach ? hugeicon('image', 15) : (r.icon && /^\s*</.test(r.icon) ? r.icon : hugeicon(r.icon || 'file', 15))}</span>` : '';
      const tail = kind === 'model' ? `${r.tag ? `<span class="bridge-pb-row-tag">${esc(r.tag)}</span>` : ''}<span class="bridge-pb-row-check"${r.selected ? ' data-on' : ''}>${hugeicon('tick', 13, 2.5)}</span>` : '';
      return `<button type="button" role="option" id="${menuId}-o${oi}" class="bridge-pb-row${armed ? ' armed' : ''}${r.unknown ? ' unknown' : ''}" data-i="${oi}" aria-selected="${oi === cp.cursor}"${r.disabled ? ' aria-disabled="true"' : ''}>`
        + `${icon}<span class="bridge-pb-row-name">${esc(armed ? S.compactArm : r.name)}</span>`
        + `${r.description && !armed ? `<span class="bridge-pb-row-desc">${esc(r.description)}</span>` : ''}${tail}</button>`;
    }).join('');
    const warn = kind === 'slash' && cp.armed ? `<p class="bridge-pb-warn" role="alert">${esc(S.compactWarn)}</p>` : '';
    pbMenu.innerHTML = `<span class="bridge-pb-glow" aria-hidden="true"></span>${html}`
      + (oi < 0 ? `<div class="bridge-pb-empty">${esc(S.noMatch)}${cp.token?.query ? `「${esc(cp.token.query)}」` : ''}</div>` : '') + warn;
    placeGlow();
  }
  function placeGlow() {
    const glow = pbMenu.querySelector('.bridge-pb-glow'); if (!glow) return;
    const row = pbMenu.querySelector(`.bridge-pb-row[data-i="${cp.cursor}"]`);
    for (const b of pbMenu.querySelectorAll('.bridge-pb-row')) b.setAttribute('aria-selected', String(b === row));
    if (!row) { glow.style.opacity = '0'; input.removeAttribute('aria-activedescendant'); return; }
    input.setAttribute('aria-activedescendant', row.id);
    const fresh = cp.lastOpen !== cp.menu; cp.lastOpen = cp.menu;
    if (fresh) glow.style.transition = 'none';
    glow.style.top = `${row.offsetTop}px`; glow.style.height = `${row.offsetHeight}px`; glow.style.opacity = '1';
    if (fresh) { void glow.offsetHeight; glow.style.transition = ''; }
    const top = row.offsetTop, bottom = top + row.offsetHeight;
    if (top < pbMenu.scrollTop) pbMenu.scrollTop = top - 4;
    else if (bottom > pbMenu.scrollTop + pbMenu.clientHeight) pbMenu.scrollTop = bottom - pbMenu.clientHeight + 4;
  }
  // replace the `/…` or `@…` being typed with `text`
  function replaceToken(text) {
    const caret = input.selectionStart ?? input.value.length;
    const start = cp.token ? cp.token.start : caret;
    input.value = input.value.slice(0, start) + text + input.value.slice(caret);
    const at = start + text.length;
    input.setSelectionRange(at, at);
    autoGrow(); syncSend();
  }
  async function pick(i) {
    const r = optionRows()[i]; if (!r || r.disabled) return;
    const kind = cp.menu;
    if (kind === 'model') {
      closeMenu(); input.focus({ preventScroll: true });
      if (state.settings && r.key !== (state.settings.chat.model || '')) { state.settings.chat.model = r.key; renderPickers(); saveSetting({ model: r.key }); }
      return;
    }
    if (kind === 'at' || kind === 'plus') {
      if (kind === 'at') replaceToken('');
      closeMenu(); cp.dismissed = false;
      if (r.attach) { fileInput.click(); return; }
      replaceToken(r.insert ?? `@${r.name} `);
      input.focus({ preventScroll: true });
      try { await (r.onPick?.(r) ?? o.onSource?.(r)); } catch (e) { toast(e.message, true); }
      return;
    }
    // slash: commands are actions; the typed `/word` is removed when one runs
    if (r.key === 'compact' && cp.armed !== 'compact') {
      cp.armed = 'compact'; clearTimeout(cp.armTimer);
      cp.armTimer = setTimeout(() => { cp.armed = null; if (cp.menu === 'slash') renderMenu(); }, 5000);
      renderMenu(); return;
    }
    replaceToken(''); closeMenu();
    try {
      if (r.key === 'new') await newThread();
      else if (r.key === 'compact') await ctxActions().onCompact();
      else if (r.key === 'context') openPop('ctx', pill.hidden ? form : pill);
      else if (r.key === 'model') openModel();
      else if (r.key === 'effort') openEffort();
      else if (r.host) { await r.host.run?.({ widget: api, thread: state.thread, input, toast }); autoGrow(); syncSend(); }
    } catch (e) { toast(e.message, true); }
    if (r.key !== 'effort' && r.key !== 'context') input.focus({ preventScroll: true });
  }
  function openModel() {
    if (!state.settings) return;
    const rows = modelMenuRows(state.settings.models, state.settings.chat.model);
    const sel = rows.filter(r => r.kind === 'model').findIndex(r => r.selected);
    openMenu('model', rows, null, Math.max(0, sel));
  }
  function openEffort() { if (state.settings) { openMenu('effort'); pbMenu.querySelector('.bridge-pb-track')?.focus({ preventScroll: true }); } }
  function openPlus() {
    const rows = [];
    if (cp.uploads) rows.push({ key: '__attach', name: S.image, description: S.imageHint, attach: true });
    Promise.resolve(hasSources() ? sourceRows('') : []).then((src) => openMenu('plus', [...rows, ...src.map((r, i) => ({ key: r.key ?? `s${i}`, ...r }))]))
      .catch(e => toast(e.message, true));
  }

  // effort: a stepped slider (Faster → Smarter) over the non-empty levels, plus 「默认」 = the CLI's own default
  const EDGE = 11;
  function renderEffort() {
    const refocus = !!document.activeElement?.closest?.('.bridge-pb-track');
    const es = effortState(state.settings?.efforts, cp.effort), n = es.steps.length;
    const stepAt = (i) => `calc(${EDGE}px + (100% - ${EDGE * 2}px) * ${i / Math.max(1, n - 1)})`;
    const fill = es.index < 0 ? '0px' : es.index === n - 1 ? '100%' : `calc(${stepAt(es.index)} + 7px)`;
    pbMenu.setAttribute('role', 'dialog'); pbMenu.setAttribute('aria-label', S.effort);
    pbMenu.innerHTML = `<div class="bridge-pb-effort-head"><span class="bridge-pb-effort-title">${esc(S.effort)}</span>`
      + `<span class="bridge-pb-effort-level">${esc(cp.effort || S.effortDefault)}</span>`
      + (es.hasDefault ? `<button type="button" class="bridge-pb-effort-reset"${es.index < 0 ? ' disabled' : ''}>${esc(S.effortDefault)}</button>` : '')
      + `<span class="bridge-pb-effort-help" title="${esc(S.effortHelp)}">${hugeicon('help', 14)}</span></div>`
      + `<div class="bridge-pb-effort-ends"><span>${esc(S.faster)}</span><span>${esc(S.smarter)}</span></div>`
      + `<div class="bridge-pb-track${es.index < 0 ? ' unset' : ''}" role="slider" tabindex="0" aria-label="${esc(S.effort)}" aria-valuemin="0" aria-valuemax="${Math.max(0, n - 1)}"`
      + ` aria-valuenow="${Math.max(0, es.index)}" aria-valuetext="${esc(cp.effort || S.effortDefault)}" style="--pb-effort-x:${es.index < 0 ? stepAt(0) : stepAt(es.index)};--pb-effort-fill:${fill}">`
      + `<span class="bridge-pb-effort-fill"></span>${es.steps.map((_, i) => `<i class="bridge-pb-effort-dot" style="left:${stepAt(i)}"></i>`).join('')}<span class="bridge-pb-effort-thumb"></span></div>`;
    if (refocus) pbMenu.querySelector('.bridge-pb-track')?.focus({ preventScroll: true });
  }
  function setEffort(v) {
    if (v === cp.effort) return;
    cp.effort = v;
    renderPickers(); if (cp.menu === 'effort') renderEffort();
    clearTimeout(cp.effortTimer);   // dragging across levels saves once, where it stops
    cp.effortTimer = setTimeout(() => { if (state.settings && (state.settings.chat.effort || '') !== cp.effort) { state.settings.chat.effort = cp.effort; saveSetting({ effort: cp.effort }); } }, 450);
  }
  const effortSteps = () => effortState(state.settings?.efforts, cp.effort);
  function effortFromPointer(e, track) {
    const { steps } = effortSteps(); if (!steps.length) return;
    const rect = track.getBoundingClientRect();
    const k = (e.clientX - rect.left - EDGE) / Math.max(1, rect.width - 2 * EDGE);
    setEffort(steps[Math.max(0, Math.min(steps.length - 1, Math.round(k * (steps.length - 1))))]);
  }
  pbMenu.addEventListener('pointerdown', (e) => {
    const track = e.target.closest('.bridge-pb-track');
    if (track && e.button === 0) { try { track.setPointerCapture(e.pointerId); } catch { /* ignore */ } track.focus({ preventScroll: true }); effortFromPointer(e, track); e.preventDefault(); }
  });
  pbMenu.addEventListener('pointermove', (e) => { const track = e.target.closest('.bridge-pb-track'); if (track && (e.buttons & 1)) effortFromPointer(e, track); });
  pbMenu.addEventListener('keydown', (e) => {
    if (!e.target.closest('.bridge-pb-track')) { if (e.key === 'Escape') { closeMenu(); input.focus(); } return; }
    const { steps, index } = effortSteps(); const last = steps.length - 1;
    const go = (i) => { e.preventDefault(); setEffort(steps[Math.max(0, Math.min(last, i))]); };
    if (e.key === 'ArrowRight' || e.key === 'ArrowUp') go(index + 1);
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') go(index < 0 ? 0 : index - 1);
    else if (e.key === 'Home') go(0);
    else if (e.key === 'End') go(last);
    else if (e.key === 'Escape' || e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); closeMenu(); input.focus({ preventScroll: true }); }
  });
  pbMenu.addEventListener('mousedown', (e) => { if (e.target.closest('.bridge-pb-row')) e.preventDefault(); });   // keep the caret in the textarea
  pbMenu.addEventListener('pointerover', (e) => { const r = e.target.closest('.bridge-pb-row'); if (r && Number(r.dataset.i) !== cp.cursor) { cp.cursor = Number(r.dataset.i); placeGlow(); } });
  pbMenu.addEventListener('click', (e) => {
    if (e.target.closest('.bridge-pb-effort-reset')) { setEffort(''); return; }
    const r = e.target.closest('.bridge-pb-row'); if (r) pick(Number(r.dataset.i));
  });
  form.addEventListener('click', (e) => {
    const b = e.target.closest('[data-pb]'); if (!b) return;
    const kind = b.dataset.pb;
    if (cp.menu === kind) { closeMenu(); input.focus({ preventScroll: true }); return; }
    closeMenu(); closePops();
    if (kind === 'model') openModel();
    else if (kind === 'effort') openEffort();
    else if (kind === 'plus') { if (hasSources()) openPlus(); else if (cp.uploads) fileInput.click(); }
    if (kind !== 'effort') input.focus({ preventScroll: true });
  });
  for (const b of form.querySelectorAll('[data-pb], .bridge-send')) b.addEventListener('mousedown', (e) => e.preventDefault());

  // ---------- sparks at the top effort level (upstream: canvas embers that speed up while typing) ----------
  function sparks(on) {
    const canvas = $('.bridge-pb-sparks');
    if (!on || reducedMotion() || !canvas?.getContext) { if (cp.sparks) { cp.sparks(); cp.sparks = null; } return; }
    if (cp.sparks) return;
    const ctx = canvas.getContext('2d'); if (!ctx) return;
    const color = getComputedStyle(canvas).color;
    let raf = 0, last = performance.now(), w = 0, h = 0, due = 0, speed = 1, pulse = 0;
    const parts = [];
    const resize = () => {
      const rect = canvas.getBoundingClientRect(), dpr = Math.min(2, globalThis.devicePixelRatio || 1);
      w = rect.width; h = rect.height; canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    const spawn = (burst) => parts.push({ x: Math.random() * w, y: burst ? h * (0.2 + Math.random() * 0.8) : h + 3, r: 0.9 + Math.random() * 1.1,
      vy: -(7 + Math.random() * 9), sway: (Math.random() - 0.5) * 10, phase: Math.random() * Math.PI * 2, life: burst ? Math.random() * 1.2 : 0, span: 2.4 + Math.random() * 2.4 });
    const tick = (now) => {
      const dt = Math.min(0.05, (now - last) / 1000); last = now;
      cp.energy *= Math.exp(-dt / 0.8); pulse *= Math.exp(-dt / 0.16);
      if (cp.strokes > 0) { cp.strokes = 0; pulse = 1; }
      const energy = cp.energy;
      speed += (1 + energy * 6 - speed) * (1 - Math.exp(-dt / 0.15));
      due += dt;
      while (due > 0.14) { due -= 0.14; if (parts.length < 30) spawn(false); }
      ctx.clearRect(0, 0, w, h);
      ctx.fillStyle = color; ctx.shadowColor = color; ctx.shadowBlur = 6 + energy * 10 + pulse * 6;
      for (let i = parts.length - 1; i >= 0; i--) {
        const p = parts[i]; p.life += dt;
        if (p.life > p.span) { parts.splice(i, 1); continue; }
        const k = p.life / p.span, twinkle = 0.7 + 0.3 * Math.sin((now / 160) * (1 + energy) + p.phase);
        p.y += p.vy * dt * speed;
        if (p.y < -4) { p.y = h + 3; p.x = Math.random() * w; }
        const edge = Math.min(1, Math.max(0, p.y / 14), Math.max(0, (h - p.y) / 14));
        ctx.globalAlpha = Math.min(1, Math.sin(k * Math.PI) * (0.9 + energy * 0.25) * twinkle) * edge;
        ctx.beginPath();
        ctx.arc(p.x + Math.sin((now / 900) * (1 + energy * 0.8) + p.phase) * p.sway, p.y, p.r * twinkle * (1 + energy * 0.35), 0, Math.PI * 2);
        ctx.fill();
      }
      raf = requestAnimationFrame(tick);
    };
    resize();
    for (let i = 0; i < 26; i++) spawn(true);
    const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(resize) : null; ro?.observe(canvas);
    raf = requestAnimationFrame(tick);
    cp.sparks = () => { cancelAnimationFrame(raf); ro?.disconnect(); ctx.clearRect(0, 0, w, h); };
  }

  // ---------- voice (Voice Pill + Web Speech, or the host's transcriber) ----------
  if (voiceOn) {
    const vc = voiceCfg;
    const lang = vc.lang || speechLang(o.locale || el.closest('[lang]')?.getAttribute('lang') || globalThis.navigator?.language);
    const pillOpts = Object.fromEntries(Object.keys(VOICE_DEFAULTS).filter(k => vc[k] !== undefined).map(k => [k, vc[k]]));
    let dict = null, base = '';
    const settle = ({ text, cancelled }) => {
      dict = null;
      input.value = cancelled ? base : joinDictation(base, text, lang);
      input.readOnly = false; delete form.dataset.listening;
      input.placeholder = state.inflight ? 'Claude 正在回答，稍等…' : placeholder();
      autoGrow(); syncSend();
      if (voice.listening) voice.stop('ended');
      if (!cancelled) { input.focus({ preventScroll: true }); const n = input.value.length; input.setSelectionRange(n, n); }
      vc.onResult?.({ text: cancelled ? '' : text, cancelled });
    };
    voice = mountVoicePill($('.bridge-pb-voice'), {
      ariaLabel: S.voice, ...pillOpts,
      onStart: (info) => {
        closeMenu();
        base = input.value; input.readOnly = true; form.dataset.listening = ''; input.placeholder = S.listening;
        dict = startDictation({
          lang, transcribe: vc.transcribe || null, stream: vc.transcribe ? voice.stream() : null,
          write: (t) => { input.value = joinDictation(base, t, lang); autoGrow(); syncSend(); },
          done: settle,
          error: (msg, code) => { toast(msg, true); if (voice.listening) voice.stop(code === 'not-allowed' ? 'mic-denied' : 'error'); },
        });
        vc.onStart?.(info);
      },
      onStop: (info) => { dict?.stop(info.reason === 'cancel' ? 'cancel' : info.reason); vc.onStop?.(info); },
      onError: (err) => toast(err?.name === 'NotAllowedError' || err?.name === 'SecurityError' ? VOICE_ERRORS['not-allowed']
        : err?.name === 'NotFoundError' ? VOICE_ERRORS['audio-capture'] : (err?.message || String(err)), true),
    });
  }

  form.addEventListener('submit', (e) => { e.preventDefault(); if (state.inflight && e.submitter === sendBtn) return; send(); });
  sendBtn.addEventListener('click', async (e) => {
    if (!state.inflight) return;
    e.preventDefault();       // busy: the same button is 停止
    try { await client.cancel(state.inflight); } catch (err) { toast(err.message, true); }
  });
  sendBtn.addEventListener('pointerdown', (e) => { if (e.button === 0 && sendBtn.dataset.armed != null) sendBtn.dataset.pressed = ''; });
  for (const t of ['pointerup', 'pointercancel', 'pointerleave']) sendBtn.addEventListener(t, () => { delete sendBtn.dataset.pressed; });
  input.addEventListener('keydown', (e) => {
    const composing = e.isComposing || e.keyCode === 229;
    const list = cp.menu && cp.menu !== 'effort';
    const n = list ? optionRows().length : 0;
    if (list && n && !composing) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); cp.cursor = (cp.cursor + (e.key === 'ArrowDown' ? 1 : n - 1)) % n; placeGlow(); return; }
      if ((e.key === 'Enter' && !e.shiftKey) || e.key === 'Tab') { e.preventDefault(); pick(cp.cursor); return; }
    }
    if (e.key === 'Escape' && cp.menu) { e.preventDefault(); if (cp.menu === 'slash' || cp.menu === 'at') cp.dismissed = true; closeMenu(); return; }
    if (isSendKey(e, prefs)) { e.preventDefault(); send(); }
  });
  input.addEventListener('input', () => {
    autoGrow(); syncSend();
    cp.energy = Math.min(1.6, cp.energy + 0.22); cp.strokes = Math.min(4, cp.strokes + 1);
    cp.dismissed = false;
    if (cp.menu && !['slash', 'at'].includes(cp.menu)) closeMenu();
    syncToken();
  });
  // moving the caret (arrows / click) can enter or leave a `/…` / `@…` token
  input.addEventListener('keyup', (e) => { if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) syncToken(); });
  input.addEventListener('click', () => { if (cp.menu && !['slash', 'at'].includes(cp.menu)) closeMenu(); else syncToken(); });
  const offMenuOutside = onOutsidePointer('.bridge-form', () => closeMenu());
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
      if (act === 'new') await newThread();
      else if (act === 'side-close') root.classList.remove('side-open');
      else if (act === 'side') { if (matchMedia('(max-width: 720px)').matches) root.classList.toggle('side-open'); else { prefs.sidebar = !prefs.sidebar; usePrefs('sidebar'); } }
      else if (act === 'menu') { const m = menu; const was = m.hidden; closePops(); disarmConfirm(m.querySelector('[data-act="del"]')); if (was) { m.querySelector('[data-act="pin"]').textContent = state.threadRow?.pinned ? S.unpin : S.pin; placePopover(e.target.closest('[data-act]'), m); } }
      else if (act === 'prefs' || act === 'ctx') openPop(act, e.target.closest('[data-act]'));
      else if (act === 'rename') {
        closePops();
        const t = await promptDialog({ title: S.rename, label: S.renameLabel, value: state.threadRow?.title || '', placeholder: S.threadTitle,
          okLabel: S.save, cancelLabel: S.cancel, returnFocus: $('[data-act="menu"]') });
        if (t != null) await client.patchThread(state.thread, { title: t.trim() || null });
      }
      else if (act === 'pin') { closePops(); await client.patchThread(state.thread, { pinned: !state.threadRow?.pinned }); }
      else if (act === 'del') {
        // two-step inside the ⋯ menu (it stays open while armed): first click warns, second deletes
        if (!armConfirm(e.target.closest('[data-act]'), { label: S.delArm, warnEl: menu.querySelector('.bridge-menu-warn'), warning: S.delConfirm })) return;
        closePops(); const r = await client.deleteThread(state.thread, o.scope); state.thread = null; state.sub?.close(); if (r.current) subscribe(r.current); else { list.innerHTML = `<div class="bridge-empty">${esc(S.empty)}</div>`; } loadThreads(); }
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
  plusBtn.hidden = !hasSources();
  syncSend();
  loadSettings();
  if (o.showThreads) loadThreads();
  else client.threads(o.scope).then(d => d.current && subscribe(d.current)).catch(e => toast(e.message, true));

  const api = {
    destroy() {
      state.sub?.close(); clearInterval(state.ticker);
      voice?.destroy(); sparks(false); cancelAnimationFrame(cp.glyphRaf); clearTimeout(cp.effortTimer); clearTimeout(cp.armTimer); offMenuOutside();
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
    get input() { return input; },
  };
  return api;
}
