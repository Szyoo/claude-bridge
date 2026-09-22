// Reference chat UI for claude-bridge. Framework-free; all styling via bridge-widget.css (--bridge-* tokens).
// mountBridgeWidget(el, client, opts) -> { destroy(), openThread(id), refresh() }

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function defaultMarkdown(text) {
  const safe = esc(text || '');
  if (globalThis.marked?.parse) return globalThis.marked.parse(safe, { breaks: true, gfm: true });
  return safe.replace(/\n/g, '<br>');
}

const CONTEXT_WINDOWS = { default: 200000 };
function contextWindow(model, table) {
  if (!model) return table.default;
  if (/\[1m\]/i.test(model)) return 1_000_000;
  for (const [k, v] of Object.entries(table)) if (k !== 'default' && model.includes(k)) return v;
  return table.default;
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
  return n >= 1000 ? (n / 1000).toFixed(n >= 100000 ? 0 : 1) + 'k' : String(n);
}

function fmtReset(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

// Builds the "model · turns · tokens · context · cost · quota" line from a thread's events.
export function sessionSummary(messages, { contextWindows = CONTEXT_WINDOWS } = {}) {
  let init = null, usage = null, rate = null;
  for (const m of messages) for (const ev of m.events || []) {
    if (ev.type === 'init') init = ev.data;
    else if (ev.type === 'usage') usage = ev.data;
    else if (ev.type === 'rate_limit') rate = ev.data;
  }
  const model = usage?.model || init?.model || '';
  const parts = [];
  if (model) parts.push({ label: model.replace(/^claude-/, '') });
  if (usage) {
    if (usage.num_turns != null) parts.push({ label: `${usage.num_turns} 轮` });
    parts.push({ label: `↑${fmtTokens((usage.input_tokens || 0) + (usage.cache_read_input_tokens || 0) + (usage.cache_creation_input_tokens || 0))} ↓${fmtTokens(usage.output_tokens)}` });
    if (usage.context_tokens) {
      const win = contextWindow(model, contextWindows);
      const pct = Math.min(100, Math.round(usage.context_tokens / win * 100));
      parts.push({ label: `上下文 ${pct}%`, bar: pct, title: `${fmtTokens(usage.context_tokens)} / ${fmtTokens(win)}` });
    }
    if (usage.total_cost_usd != null) parts.push({ label: `$${usage.total_cost_usd.toFixed(3)}` });
  }
  if (rate?.five_hour?.utilization != null) {
    const pct = Math.round(rate.five_hour.utilization * 100);
    parts.push({ label: `5h 额度 ${pct}%`, bar: pct, title: `重置 ${fmtReset(rate.five_hour.resets_at)}${rate.seven_day ? ` · 7 天 ${Math.round(rate.seven_day.utilization * 100)}%` : ''}` });
  }
  return { model, usage, rate, parts };
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
    const { parts } = sessionSummary([...state.msgs.values()], { contextWindows: o.contextWindows });
    box.innerHTML = parts.map(p => `<span title="${esc(p.title || '')}">${esc(p.label)}${p.bar != null ? ` <span class="bar${p.bar >= 80 ? ' hot' : ''}"><i style="width:${p.bar}%"></i></span>` : ''}</span>`).join('');
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
