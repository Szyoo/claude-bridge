// claude-bridge browser client: REST calls + a live subscription (EventSource with polling fallback).
// Framework-free ES module. Auth is whatever cookie the page already has; a 401 calls onAuthLost.

export class BridgeAuthError extends Error {}

// ---- "is there a newer claude-bridge?" — anonymous GitHub tags API (60 req/h/IP), cached in localStorage ----
const parseVer = (t) => { const m = /^v?(\d+)\.(\d+)\.(\d+)$/.exec(String(t || '').trim()); return m ? [+m[1], +m[2], +m[3]] : null; };
const newer = (a, b) => { for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] > b[i]; return false; };

// current: the version the server reports (GET /settings → bridge.version). → { current, latest, hasUpdate, compareUrl }
export async function checkBridgeUpdate({ current, repo = 'Szyoo/claude-bridge', cacheHours = 6, force = false,
  cacheKey = 'claude_bridge_latest', fetchFn = (...a) => globalThis.fetch(...a) } = {}) {
  let latest = null;
  if (!force) {
    try { const c = JSON.parse(localStorage.getItem(cacheKey) || 'null'); if (c?.repo === repo && c.latest && Date.now() - c.at < cacheHours * 3600e3) latest = c.latest; }
    catch { /* no / broken cache */ }
  }
  if (!latest) {
    const res = await fetchFn(`https://api.github.com/repos/${repo}/tags?per_page=100`, { headers: { Accept: 'application/vnd.github+json' } });
    if (!res.ok) throw new Error(`GitHub API ${res.status}`);
    let best = null;
    for (const t of await res.json()) { const v = parseVer(t?.name); if (v && (!best || newer(v, best.v))) best = { v, name: t.name }; }
    if (!best) throw new Error('no semver tags');
    latest = best.name;
    try { localStorage.setItem(cacheKey, JSON.stringify({ at: Date.now(), repo, latest })); } catch { /* not cached */ }
  }
  const cur = parseVer(current), lat = parseVer(latest);
  return { current, latest: latest.replace(/^v/, ''), hasUpdate: !!(cur && lat && newer(lat, cur)),
    compareUrl: `https://github.com/${repo}/compare/v${current}...${latest}` };
}

function detailText(detail, status) {
  if (typeof detail === 'string' && detail) return detail;
  if (Array.isArray(detail)) {
    return detail.map(d => {
      const field = (d.loc || []).filter(x => x !== 'body').join('.');
      return `${field ? field + ': ' : ''}${d.msg || d.type || 'invalid'}`;
    }).join('; ') || `HTTP ${status}`;
  }
  if (detail && typeof detail === 'object') return JSON.stringify(detail);
  return `HTTP ${status}`;
}

export class BridgeClient {
  constructor({ baseUrl = '/api/bridge', fetch: fetchFn = null, onAuthLost = null, pollInterval = 1500, idlePollInterval = 10000 } = {}) {
    this.baseUrl = baseUrl.replace(/\/$/, '');
    this.fetch = fetchFn || ((...a) => globalThis.fetch(...a));
    this.onAuthLost = onAuthLost;
    this.pollInterval = pollInterval;
    this.idlePollInterval = idlePollInterval;
  }

  async request(method, path, body, { raw = null, type = '' } = {}) {
    const opts = { method, headers: {} };
    if (raw) { opts.headers['Content-Type'] = type || raw.type || 'application/octet-stream'; opts.body = raw; }
    else if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    const res = await this.fetch(this.baseUrl + path, opts);
    if (res.status === 401) { this.onAuthLost?.(); throw new BridgeAuthError('未登录'); }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(detailText(data.detail, res.status));
    return data;
  }

  // ---- threads ----
  threads(scope = '') { return this.request('GET', `/threads?scope=${encodeURIComponent(scope)}`); }
  createThread({ scope = '', key = '', title = '' } = {}) { return this.request('POST', '/threads', { scope, key, title }); }
  findThread(scope, key) { return this.request('GET', `/threads/find?scope=${encodeURIComponent(scope)}&key=${encodeURIComponent(key)}`); }
  thread(id) { return this.request('GET', `/threads/${encodeURIComponent(id)}`); }
  selectThread(id, scope = '') { return this.request('POST', `/threads/${encodeURIComponent(id)}/select`, { scope }); }
  patchThread(id, patch) { return this.request('PATCH', `/threads/${encodeURIComponent(id)}`, patch); }
  deleteThread(id, scope = null) {
    const q = scope == null ? '' : `?scope=${encodeURIComponent(scope)}`;
    return this.request('DELETE', `/threads/${encodeURIComponent(id)}${q}`);
  }
  messages(id, { after = 0, limit = 200, events = true } = {}) {
    return this.request('GET', `/threads/${encodeURIComponent(id)}/messages?after=${after}&limit=${limit}&events=${events ? 1 : 0}`);
  }

  // ---- chat ----
  // `files` = ids returned by upload(); text may be empty when images are attached
  send(text, { thread = null, scope = '', key = '', newThread = false, files = [] } = {}) {
    if (thread) return this.request('POST', `/threads/${encodeURIComponent(thread)}/messages`, { text, files });
    return this.request('POST', '/send', { text, scope, key, new_thread: newThread, files });
  }
  // ---- images ----
  // the body is the image itself; the server identifies the format from its bytes → { id, name, mime, size }
  upload(blob, { name = '' } = {}) { return this.request('POST', `/files?name=${encodeURIComponent(name)}`, undefined, { raw: blob }); }
  fileUrl(id) { return `${this.baseUrl}/files/${encodeURIComponent(id)}`; }
  cancel(messageId) { return this.request('POST', `/messages/${messageId}/cancel`); }
  // session-level actions: refresh the /context breakdown, or ask Claude to compact the session history
  refreshContext(threadId) { return this.request('POST', `/threads/${encodeURIComponent(threadId)}/context`); }
  compact(threadId) { return this.request('POST', `/threads/${encodeURIComponent(threadId)}/compact`); }

  // ---- misc ----
  settings() { return this.request('GET', '/settings'); }
  saveSettings(patch) { return this.request('PUT', '/settings', patch); }
  jobs(limit = 20) { return this.request('GET', `/jobs?limit=${limit}`); }
  job(id) { return this.request('GET', `/jobs/${id}`); }
  status() { return this.request('GET', '/status'); }

  subscribe(threadId, handlers = {}, opts = {}) {
    return new Subscription(this, threadId, handlers, opts);
  }
}

// Keeps one thread's view in sync. Handlers: onSnapshot, onMessage, onDelta, onEvent, onStatus, onDone,
// onThread, onFallback(bool), onError(err). Deltas are applied only when rev === known + 1; a gap re-fetches the row.
export class Subscription {
  constructor(client, threadId, handlers, { after = 0, lastEventId = 0, maxErrors = 3, retrySseAfter = 30000 } = {}) {
    this.client = client;
    this.threadId = threadId;
    this.h = handlers;
    this.after = after;
    this.lastEventId = lastEventId;
    this.maxErrors = maxErrors;
    this.retrySseAfter = retrySseAfter;
    this.revs = new Map();
    this.inflight = null;
    this.closed = false;
    this.es = null;
    this.errors = 0;
    this.pollTimer = null;
    this.sseRetryTimer = null;
    this.fallback = false;
    this._open();
  }

  get streamUrl() {
    const q = new URLSearchParams({ after: String(this.after) });
    if (this.lastEventId) q.set('last_event_id', String(this.lastEventId));
    return `${this.client.baseUrl}/threads/${encodeURIComponent(this.threadId)}/stream?${q}`;
  }

  _open() {
    if (this.closed) return;
    if (typeof EventSource === 'undefined') { this._startPolling(); return; }
    this._stopPolling();
    const es = new EventSource(this.streamUrl);
    this.es = es;
    const on = (type, fn) => es.addEventListener(type, (e) => {
      this.errors = 0;
      if (e.lastEventId) this.lastEventId = Number(e.lastEventId) || this.lastEventId;
      let data = null;
      try { data = JSON.parse(e.data); } catch { return; }
      fn(data);
    });
    on('snapshot', (snap) => this._applySnapshot(snap));
    on('message', (m) => { this.revs.set(m.id, m.rev || 0); if (m.role === 'assistant' && (m.status === 'pending' || m.status === 'streaming')) this.inflight = m.id; this.h.onMessage?.(m); });
    on('delta', (d) => this._applyDelta(d));
    on('event', (ev) => this.h.onEvent?.(ev));
    on('status', (s) => { if (s.status !== 'pending' && s.status !== 'streaming' && this.inflight === s.message_id) this.inflight = null; this.h.onStatus?.(s); });
    on('done', (d) => { if (this.inflight === d.message_id) this.inflight = null; this.h.onDone?.(d); });
    on('thread', (t) => this.h.onThread?.(t));
    on('context', (c) => this.h.onContext?.(c));
    on('job', (j) => this.h.onJob?.(j));
    es.onerror = async () => {
      if (this.closed) return;
      this.errors += 1;
      // EventSource hides the status code; ask a cheap endpoint whether we were logged out
      try { await this.client.status(); } catch (err) {
        if (err instanceof BridgeAuthError) { this.close(); return; }
      }
      if (this.errors >= this.maxErrors) {
        es.close();
        this.es = null;
        this._startPolling();
        this.sseRetryTimer = setTimeout(() => { if (!this.closed && this.fallback) this._open(); }, this.retrySseAfter);
      }
    };
  }

  _applySnapshot(snap) {
    this.revs = new Map(snap.messages.map(m => [m.id, m.rev || 0]));
    this.inflight = snap.inflight ?? null;
    if (snap.cursor) this.lastEventId = Math.max(this.lastEventId, snap.cursor);
    this.h.onSnapshot?.(snap);
  }

  async _applyDelta(d) {
    const known = this.revs.get(d.message_id);
    if (known === undefined || d.rev === known + 1) {
      this.revs.set(d.message_id, d.rev);
      this.h.onDelta?.(d);
      return;
    }
    if (d.rev <= known) return; // duplicate
    try { // gap: replace the whole row
      const r = await this.client.messages(this.threadId, { after: d.message_id - 1, limit: 1 });
      const m = r.items?.[0];
      if (m) { this.revs.set(m.id, m.rev || 0); this.h.onMessage?.(m); }
    } catch (err) { this.h.onError?.(err); }
  }

  // ---- polling fallback ----
  _startPolling() {
    if (this.fallback) return;
    this.fallback = true;
    this.h.onFallback?.(true);
    const tick = async () => {
      if (this.closed || !this.fallback) return;
      try {
        const snap = await this.client.thread(this.threadId);
        this._applySnapshot({ ...snap, cursor: this.lastEventId });
      } catch (err) {
        if (err instanceof BridgeAuthError) { this.close(); return; }
        this.h.onError?.(err);
      }
      this.pollTimer = setTimeout(tick, this.inflight ? this.client.pollInterval : this.client.idlePollInterval);
    };
    tick();
  }

  _stopPolling() {
    if (!this.fallback) return;
    this.fallback = false;
    clearTimeout(this.pollTimer);
    this.pollTimer = null;
    this.h.onFallback?.(false);
  }

  // Call when the tab becomes visible again: reconnects a dead stream, or nudges the poller.
  wake() {
    if (this.closed) return;
    if (this.es && this.es.readyState === EventSource.CLOSED) { this.es = null; this.errors = 0; this._open(); }
    else if (this.fallback) { clearTimeout(this.pollTimer); this.errors = 0; this._open(); }
  }

  close() {
    this.closed = true;
    clearTimeout(this.sseRetryTimer);
    if (this.es) { this.es.close(); this.es = null; }
    const wasFallback = this.fallback;
    this.fallback = false;
    clearTimeout(this.pollTimer);
    if (wasFallback) this.h.onFallback?.(false);
  }
}
