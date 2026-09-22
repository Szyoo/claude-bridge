// Reference chat UI for claude-bridge. Framework-free; all styling via bridge-widget.css (--bridge-* tokens).
// mountBridgeWidget(el, client, opts) -> { destroy(), openThread(id), refresh() }

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
  if (used) {
    const win = context?.window || contextWindow(model, contextWindows, used);
    const pct = Math.min(100, Math.round(used / win * 100));
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
    const pct = Math.round(w.utilization * 100);
    parts.push({ key: name, label: `${name} 已用 ${pct}%`, bar: pct, title: `订阅的${name}滚动窗口已用 ${pct}%，${fmtReset(w.resets_at)} 重置（来自 claude 命令行自己报告的限额）` });
  };
  win(rate?.five_hour, '5 小时额度');
  win(rate?.seven_day, '7 天额度');
  return { model, usage, rate, context, parts };
}

const CATEGORY_ZH = {
  'System prompt': '系统提示', 'System tools': '系统工具', 'MCP tools': 'MCP 工具', 'MCP tools (deferred)': 'MCP 工具（按需加载，不计入）',
  'System tools (deferred)': '系统工具（按需加载，不计入）', Skills: '技能说明', 'Memory files': '记忆文件', Messages: '对话消息（历史 + 工具输出）',
  'Autocompact buffer': '自动压缩预留', 'Free space': '剩余空间',
};
const CATEGORY_COLORS = ['#3b6df2', '#e0703a', '#2e9b5d', '#d4a72c', '#8b8b8b', '#6f6f6f', '#3b6df2', '#555555'];

// Renders the /context breakdown into `el`. opts: { onRefresh, onCompact, busy }
export function renderContextPanel(el, ctx, opts = {}) {
  if (!ctx || ctx.used == null) {
    el.innerHTML = `<div class="bridge-ctx"><p class="bridge-ctx-help">${esc(CONTEXT_HELP)}</p><p class="bridge-muted bridge-tiny">还没有构成数据：回答一次后自动获取，或点「刷新构成」。</p>`
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
    el.innerHTML = `<div class="bridge-ctx">
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

export function describeCompact(d) {
  return `已${d.trigger === 'auto' ? '自动' : '手动'}压缩会话：${fmtTokens(d.pre_tokens)} → ${fmtTokens(d.post_tokens)}`;
}

export function mountBridgeWidget(el, client, opts = {}) {
  const o = {
    scope: '', title: 'Claude', markdown: defaultMarkdown, showThreads: true, showSettings: true,
    contextWindows: CONTEXT_WINDOWS, placeholder: '输入消息，Enter 发送，Shift+Enter 换行',
    strings: {}, ...opts,
  };
  const S = { threadTitle: '新对话', helperOn: 'helper 在线', helperOff: 'helper 离线：消息会排队', polling: '轮询模式',
    waitingHelper: '等待 helper 接单…', thinking: 'Claude 正在思考…', empty: '问点什么吧', newThread: '＋ 新对话',
    rename: '重命名', pin: '置顶', del: '删除这个对话？', ctx: '自动带背景', stop: '停止', ...o.strings };

  el.innerHTML = `
    <div class="bridge ${o.showThreads ? '' : 'no-side'}">
      ${o.showThreads ? `<aside class="bridge-side"><button type="button" class="bridge-btn" data-act="new">${esc(S.newThread)}</button><div class="bridge-threads"></div></aside>` : ''}
      <div class="bridge-main">
        <div class="bridge-top">
          <span class="bridge-dot" title="helper"></span>
          <span class="bridge-title">${esc(o.title)}</span>
          <span class="bridge-tiny bridge-muted bridge-agent"></span>
          ${o.showThreads ? `<button type="button" class="bridge-btn bridge-btn-ghost bridge-tiny" data-act="rename" title="${esc(S.rename)}">✎</button>
          <button type="button" class="bridge-btn bridge-btn-ghost bridge-tiny" data-act="pin" title="${esc(S.pin)}">📌</button>
          <button type="button" class="bridge-btn bridge-btn-ghost bridge-tiny bridge-btn-danger" data-act="del" title="${esc(S.del)}">🗑</button>` : ''}
          <div class="bridge-session"></div>
          <div class="bridge-ctxpanel" hidden></div>
        </div>
        <div class="bridge-list"><div class="bridge-empty">${esc(S.empty)}</div></div>
        <form class="bridge-form">
          <textarea class="bridge-input" rows="2" placeholder="${esc(o.placeholder)}"></textarea>
          <div class="bridge-bar">
            ${o.showSettings ? `<label class="bridge-tiny bridge-muted"><input type="checkbox" data-set="auto_context" /> ${esc(S.ctx)}</label>` : ''}
            <span class="grow"></span>
            ${o.showSettings ? `<select class="bridge-pick" data-set="effort"></select><select class="bridge-pick" data-set="model"></select>` : ''}
            <button type="button" class="bridge-btn bridge-stop" hidden title="${esc(S.stop)}">■</button>
            <button type="submit" class="bridge-btn bridge-send" title="发送">↑</button>
          </div>
        </form>
      </div>
    </div>`;

  const $ = (sel) => el.querySelector(sel);
  const list = $('.bridge-list'), input = $('.bridge-input'), sendBtn = $('.bridge-send'), stopBtn = $('.bridge-stop');
  const state = { thread: null, threadRow: null, msgs: new Map(), order: [], sub: null, inflight: null, settings: null, raf: null, dirty: new Set() };

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

  function setPending(on) {
    state.inflight = on ? state.inflight : null;
    sendBtn.disabled = !!on;
    stopBtn.hidden = !on;
    input.placeholder = on ? 'Claude 正在回答，稍等…' : o.placeholder;
  }

  // ---------- rendering ----------
  function bubbleEl(m) {
    const d = document.createElement('div');
    d.className = `bridge-bubble ${m.role} ${m.status}`;
    d.dataset.id = m.id;
    if (m.role === 'assistant') d.innerHTML = '<div class="bridge-steps"></div><div class="bridge-md"></div><div class="bridge-wait" hidden></div>';
    return d;
  }

  function renderStep(steps, ev) {
    const d = ev.data || {};
    if (ev.type === 'tool_use') {
      const card = document.createElement('details');
      card.className = 'bridge-card running';
      card.dataset.tool = d.id;
      card.innerHTML = `<summary><span class="name">${esc(d.name)}</span><span class="desc">${esc(describeTool(d))}</span><span class="state"></span></summary>` +
        `<pre class="in">${esc(JSON.stringify(d.input, null, 1))}</pre><pre class="out" hidden></pre>`;
      steps.appendChild(card);
    } else if (ev.type === 'tool_result') {
      const card = steps.querySelector(`.bridge-card[data-tool="${CSS.escape(d.tool_use_id || '')}"]`);
      if (!card) return;
      card.classList.remove('running');
      card.classList.toggle('error', !!d.is_error);
      card.querySelector('.state').textContent = d.is_error ? '失败' : '完成';
      const out = card.querySelector('.out');
      out.textContent = d.content || '(无输出)'; out.hidden = false;
    } else if (ev.type === 'thinking') {
      const card = document.createElement('details');
      card.className = 'bridge-card';
      card.innerHTML = `<summary><span class="name">思考</span><span class="desc bridge-muted">${esc((d.text || '').slice(0, 80))}</span></summary><pre>${esc(d.text || '')}</pre>`;
      steps.appendChild(card);
    } else if (ev.type === 'status' && d.phase === 'legacy_trace') {
      const card = document.createElement('details');
      card.className = 'bridge-card';
      card.innerHTML = `<summary><span class="name">执行轨迹</span></summary><pre>${esc(d.text || '')}</pre>`;
      steps.appendChild(card);
    } else if (ev.type === 'status' && d.phase === 'cancel_requested') {
      const n = document.createElement('div'); n.className = 'bridge-note'; n.textContent = '已请求停止…'; steps.appendChild(n);
    } else if (ev.type === 'compact') {
      const n = document.createElement('div'); n.className = 'bridge-note'; n.textContent = describeCompact(d); steps.appendChild(n);
    } else if (ev.type === 'error') {
      const n = document.createElement('div'); n.className = 'bridge-note error'; n.textContent = d.message || '出错'; steps.appendChild(n);
    }
  }

  function paint(id) {
    const m = state.msgs.get(id); if (!m) return;
    const b = list.querySelector(`.bridge-bubble[data-id="${id}"]`); if (!b) return;
    b.className = `bridge-bubble ${m.role} ${m.status}`;
    if (m.role !== 'assistant') { b.textContent = m.content; return; }
    const follow = nearBottom();
    b.querySelector('.bridge-md').innerHTML = o.markdown(m.content || '');
    const wait = b.querySelector('.bridge-wait');
    const streaming = m.status === 'pending' || m.status === 'streaming';
    wait.hidden = !(streaming && !m.content);
    wait.textContent = m.status === 'pending' ? S.waitingHelper : S.thinking;
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
    if (!existed) {
      state.order.push(m.id);
      list.querySelector('.bridge-empty')?.remove();
      const b = bubbleEl(m);
      list.appendChild(b);
      if (m.role === 'assistant') for (const ev of m.events || []) renderStep(b.querySelector('.bridge-steps'), ev);
    } else if (m.role === 'assistant') {
      const steps = list.querySelector(`.bridge-bubble[data-id="${m.id}"] .bridge-steps`);
      steps.innerHTML = '';
      for (const ev of m.events || []) renderStep(steps, ev);
    }
    paint(m.id);
    if (!existed) scrollBottom();
  }

  function renderSession() {
    const box = $('.bridge-session');
    const { parts } = sessionSummary([...state.msgs.values()], { contextWindows: o.contextWindows, context: state.threadRow?.context });
    box.innerHTML = parts.map(p => `<span class="${p.clickable ? 'clickable' : ''}" data-key="${esc(p.key || '')}" title="${esc(p.title || '')}">${esc(p.label)}${p.bar != null ? ` <span class="bar${p.bar >= 80 ? ' hot' : ''}"><i style="width:${p.bar}%"></i></span>` : ''}</span>`).join('');
    const panel = $('.bridge-ctxpanel');
    if (!panel.hidden) renderContextPanel(panel, state.threadRow?.context, ctxActions());
  }

  function ctxActions() {
    return {
      busy: state.ctxBusy,
      onRefresh: async () => { try { state.ctxBusy = true; renderSession(); await client.refreshContext(state.thread); toast('已请求刷新，helper 计算中…'); } catch (e) { state.ctxBusy = false; renderSession(); toast(e.message, true); } },
      onCompact: async () => { try { state.ctxBusy = true; renderSession(); await client.compact(state.thread); toast('已请求压缩，Claude 正在整理历史…'); } catch (e) { state.ctxBusy = false; renderSession(); toast(e.message, true); } },
    };
  }

  function renderAgent(agent, fallback = false) {
    $('.bridge-dot').classList.toggle('on', !!agent?.online);
    $('.bridge-agent').textContent = (agent?.online ? `${S.helperOn} · ${agent.worker || ''}` : S.helperOff) + (fallback ? ` · ${S.polling}` : '');
  }

  function applySnapshot(snap) {
    list.innerHTML = '';
    state.msgs.clear(); state.order = [];
    state.threadRow = snap.thread;
    if (!snap.messages.length) list.innerHTML = `<div class="bridge-empty">${esc(S.empty)}</div>`;
    for (const m of snap.messages) upsert(m);
    state.inflight = snap.inflight;
    setPending(!!snap.inflight);
    $('.bridge-title').textContent = snap.thread?.title || S.threadTitle;
    renderAgent(snap.agent, state.sub?.fallback);
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
        const steps = list.querySelector(`.bridge-bubble[data-id="${ev.message_id}"] .bridge-steps`);
        if (steps) renderStep(steps, ev);
        if (ev.type === 'usage' || ev.type === 'init' || ev.type === 'rate_limit') renderSession();
      },
      onStatus: (s) => { const m = state.msgs.get(s.message_id); if (!m) return; m.status = s.status; paint(s.message_id); if (s.status !== 'pending' && s.status !== 'streaming') setPending(false); },
      onDone: (d) => { const m = state.msgs.get(d.message_id); if (m) { m.status = d.status; paint(d.message_id); } setPending(false); renderSession(); loadThreads(); },
      onThread: (t) => { if (t.id === state.thread) $('.bridge-title').textContent = t.title || S.threadTitle; loadThreads(); },
      onContext: (c) => { if (state.threadRow) state.threadRow.context = c.context; state.ctxBusy = false; renderSession(); },
      onJob: (j) => {
        state.ctxBusy = false; renderSession();
        if (j.kind === 'compact') {
          let r = null; try { r = JSON.parse(j.result || '{}'); } catch { r = null; }
          if (j.status === 'done' && r?.compact) toast(describeCompact({ ...r.compact, trigger: 'manual' }));
          else if (j.status !== 'done') toast(`压缩失败：${j.error || ''}`, true);
        } else if (j.status !== 'done') toast(`刷新构成失败：${j.error || ''}`, true);
      },
      onFallback: (on) => renderAgent({ online: $('.bridge-dot').classList.contains('on') }, on),
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
      const box = $('.bridge-threads');
      box.innerHTML = d.items.map(t => `<button type="button" class="bridge-thread ${t.id === state.thread ? 'active' : ''}" data-id="${esc(t.id)}">` +
        `<span class="t">${t.pinned ? '<span class="pin">📌</span>' : ''}${esc(t.title || S.threadTitle)}</span><span class="p">${esc(t.preview || '')}</span></button>`).join('');
      if (!state.thread && d.current) subscribe(d.current);
    } catch (e) { toast(e.message, true); }
  }

  async function openThread(id) {
    try { await client.selectThread(id, o.scope); subscribe(id); loadThreads(); } catch (e) { toast(e.message, true); }
  }

  // ---------- settings ----------
  async function loadSettings() {
    if (!o.showSettings) return;
    try {
      const s = await client.settings();
      state.settings = s;
      const model = $('[data-set="model"]'), effort = $('[data-set="effort"]'), ctx = $('[data-set="auto_context"]');
      model.innerHTML = s.models.map(m => `<option value="${esc(m.id)}">${esc(m.label)}</option>`).join('');
      if (![...model.options].some(x => x.value === s.chat.model)) model.insertAdjacentHTML('beforeend', `<option value="${esc(s.chat.model)}">${esc(s.chat.model)}</option>`);
      model.value = s.chat.model || '';
      effort.innerHTML = s.efforts.map(e => `<option value="${esc(e)}">${e ? esc(e) : '默认'}</option>`).join('');
      effort.value = s.chat.effort || '';
      ctx.checked = !!s.chat.auto_context;
      renderAgent(s.agent, state.sub?.fallback);
    } catch (e) { toast(e.message, true); }
  }

  async function saveSetting(patch) {
    try { const d = await client.saveSettings(patch); if (state.settings) state.settings.chat = d.chat; toast('设置已保存，下一条消息生效'); }
    catch (e) { toast(e.message, true); }
  }

  // ---------- actions ----------
  async function send() {
    const text = input.value.trim(); if (!text) return;
    sendBtn.disabled = true;
    try {
      const r = await client.send(text, state.thread ? { thread: state.thread } : { scope: o.scope });
      input.value = ''; autoGrow();
      if (r.thread !== state.thread) subscribe(r.thread);
    } catch (e) { toast(e.message, true); sendBtn.disabled = false; }
  }

  function autoGrow() { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, window.innerHeight * 0.4) + 'px'; }

  $('.bridge-form').addEventListener('submit', (e) => { e.preventDefault(); send(); });
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });
  input.addEventListener('input', autoGrow);
  stopBtn.addEventListener('click', async () => { if (!state.inflight) return; try { await client.cancel(state.inflight); } catch (e) { toast(e.message, true); } });
  el.addEventListener('click', async (e) => {
    const chip = e.target.closest('.bridge-session .clickable');
    if (chip) { const panel = $('.bridge-ctxpanel'); panel.hidden = !panel.hidden; if (!panel.hidden) renderContextPanel(panel, state.threadRow?.context, ctxActions()); return; }
    const act = e.target.closest('[data-act]')?.dataset.act;
    const th = e.target.closest('.bridge-thread');
    if (th) return openThread(th.dataset.id);
    if (!act) return;
    try {
      if (act === 'new') { const r = await client.createThread({ scope: o.scope }); subscribe(r.thread); loadThreads(); }
      else if (act === 'rename') { const t = prompt(S.rename, state.threadRow?.title || ''); if (t != null) await client.patchThread(state.thread, { title: t.trim() }); }
      else if (act === 'pin') { await client.patchThread(state.thread, { pinned: !state.threadRow?.pinned }); state.threadRow.pinned = !state.threadRow.pinned; loadThreads(); }
      else if (act === 'del') { if (!confirm(S.del)) return; const r = await client.deleteThread(state.thread, o.scope); state.thread = null; state.sub?.close(); if (r.current) subscribe(r.current); else { list.innerHTML = `<div class="bridge-empty">${esc(S.empty)}</div>`; } loadThreads(); }
    } catch (err) { toast(err.message, true); }
  });
  el.addEventListener('change', (e) => {
    const key = e.target.dataset.set; if (!key) return;
    saveSetting({ [key]: e.target.type === 'checkbox' ? e.target.checked : e.target.value });
  });
  const onVis = () => { if (document.visibilityState === 'visible') state.sub?.wake(); };
  document.addEventListener('visibilitychange', onVis);

  loadSettings();
  if (o.showThreads) loadThreads();
  else client.threads(o.scope).then(d => d.current && subscribe(d.current)).catch(e => toast(e.message, true));

  return {
    destroy() { state.sub?.close(); document.removeEventListener('visibilitychange', onVis); el.innerHTML = ''; },
    openThread,
    refresh() { loadThreads(); loadSettings(); },
    get thread() { return state.thread; },
  };
}
