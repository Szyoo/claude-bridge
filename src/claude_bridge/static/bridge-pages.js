// Helpers shared by the multi-user pages (app / account / admin). Framework-free ES module.
import { toast as dsToast } from '@szyyw/design/toast.js';

export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

export async function api(method, path, body) {
  const res = await fetch(path, {
    method, credentials: 'same-origin',
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 401) { location.href = '/login'; throw new Error('未登录'); }
  const data = res.headers.get('content-type')?.includes('json') ? await res.json() : {};
  if (!res.ok) {
    const d = data.detail;
    throw new Error(typeof d === 'string' ? d : Array.isArray(d) ? d.map(x => x.msg).join('; ') : `HTTP ${res.status}`);
  }
  return data;
}

/** Operation feedback: @szyyw/design's toast (bottom-centre stack, click to close). */
export function toast(msg, isErr = false) {
  return dsToast(msg, { tone: isErr ? 'err' : 'ok', timeout: isErr ? 5000 : 2200 });
}

/**
 * A modal sheet (@szyyw/design .overlay > .sheet). `.overlay` is display:flex, so `hidden` can't hide it: the overlay
 * is attached on open and removed on close. Closes on ✕ / Esc / a click on the backdrop. Returns { open, close, isOpen }.
 */
export function sheet(el) {
  const overlay = document.createElement('div');
  overlay.className = 'overlay';
  overlay.append(el);
  el.hidden = false;
  // Esc closes only the topmost sheet (a confirmation can sit on top of the user editor)
  const onKey = (e) => { if (e.key === 'Escape' && [...document.querySelectorAll('body > .overlay')].pop() === overlay) close(); };
  function close() { overlay.remove(); document.removeEventListener('keydown', onKey); }
  overlay.addEventListener('click', (e) => { if (e.target === overlay || e.target.closest('[data-close]')) close(); });
  return {
    open() { if (!overlay.isConnected) { document.body.append(overlay); document.addEventListener('keydown', onKey); } el.querySelector('input:not([type=hidden]), button')?.focus(); },
    close,
    get isOpen() { return overlay.isConnected; },
  };
}

/**
 * Two-step confirm for a destructive button (DESIGN §8): the first click turns it red into「再点一次确认」and shows
 * `warning` (the impact) in `warnEl` (a .callout); a second click within 5 s returns true. Returns false while arming.
 */
export function armConfirm(btn, { warnEl = null, warning = '', label = '再点一次确认' } = {}) {
  if (btn.dataset.armed) { disarm(btn, warnEl); return true; }
  for (const other of document.querySelectorAll('[data-armed]')) disarm(other, warnEl);
  btn.dataset.armed = btn.textContent;
  btn.textContent = label;
  btn.classList.add('bp-armed');
  if (warnEl) { warnEl.textContent = warning; warnEl.hidden = false; }
  btn._disarm = setTimeout(() => disarm(btn, warnEl), 5000);
  return false;
}
export function disarm(btn, warnEl = null) {
  if (!btn.dataset.armed) return;
  clearTimeout(btn._disarm);
  btn.textContent = btn.dataset.armed;
  delete btn.dataset.armed;
  btn.classList.remove('bp-armed');
  if (warnEl) warnEl.hidden = true;
}

/**
 * Typed confirmation in a sheet: the destructive button only enables once the input equals `expect`
 * (replaces `prompt("输入…确认") === expect`). Resolves true on confirm, false on cancel / ✕ / Esc / backdrop.
 */
export function confirmTyped({ title, message, expect, okLabel = '删除' }) {
  return new Promise((resolve) => {
    const el = document.createElement('form');
    el.className = 'sheet';
    el.innerHTML = `<div class="sheet-head"><h3 class="sheet-title">${esc(title)}</h3><button type="button" class="close-x" data-close aria-label="关闭">✕</button></div>
      <div class="sheet-body"><div class="callout err"><strong>无法恢复。</strong>${esc(message)}</div>
        <div class="form-row bp-gap"><label for="bp-confirm-input">输入「${esc(expect)}」确认</label>
        <input id="bp-confirm-input" class="field" autocomplete="off" autocapitalize="none" spellcheck="false" /></div></div>
      <div class="sheet-foot"><button type="submit" class="btn-ghost danger bp-armed" disabled>${esc(okLabel)}</button><button type="button" class="btn-ghost" data-close>取消</button></div>`;
    const input = el.querySelector('input'), ok = el.querySelector('[type=submit]');
    let done = false;
    const sh = sheet(el);
    const finish = (v) => { if (done) return; done = true; sh.close(); resolve(v); };
    input.addEventListener('input', () => { ok.disabled = input.value !== expect; });
    el.addEventListener('submit', (e) => { e.preventDefault(); if (input.value === expect) finish(true); });
    // ✕ / 取消 / backdrop / Esc all go through sheet(): watch for the overlay leaving the page
    new MutationObserver((_, mo) => { if (!el.isConnected) { mo.disconnect(); finish(false); } }).observe(document.body, { childList: true });
    sh.open();
    input.focus();
  });
}

export const WINDOWS = [['five_hour', '5 小时'], ['seven_day', '本周']];

export const fmtPct = (v) => v == null ? '—' : `${v < 10 && v % 1 ? v.toFixed(1) : Math.round(v)}%`;
export const fmtUsd = (v) => v == null ? '—' : `$${v < 1 ? v.toFixed(3) : v.toFixed(2)}`;

export function fmtWhen(epoch) {
  if (!epoch) return '';
  const d = new Date(epoch * 1000), now = Date.now(), diff = d - now;
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  const sameDay = new Date(now).toDateString() === d.toDateString();
  const abs = sameDay ? `今天 ${hm}` : `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
  if (diff <= 0) return abs;
  const h = diff / 3600e3;
  return `${abs}（${h < 1 ? `${Math.max(1, Math.round(diff / 60e3))} 分钟后` : h < 48 ? `${Math.round(h)} 小时后` : `${Math.round(h / 24)} 天后`}）`;
}

export function fmtSeen(text) {
  if (!text) return '从未登录';
  const t = new Date(text.replace(' ', 'T') + 'Z').getTime(), m = (Date.now() - t) / 60e3;
  if (m < 10) return '刚刚在线';
  if (m < 60) return `${Math.round(m)} 分钟前`;
  if (m < 48 * 60) return `${Math.round(m / 60)} 小时前`;
  return `${Math.round(m / 1440)} 天前`;
}

/** "iPhone · Safari" from a User-Agent string (rough, for the admin's access log). */
export function fmtDevice(ua) {
  if (!ua) return '未知设备';
  const os = /iPhone/.test(ua) ? 'iPhone' : /iPad/.test(ua) ? 'iPad' : /Android/.test(ua) ? 'Android'
    : /Mac OS X|Macintosh/.test(ua) ? 'Mac' : /Windows/.test(ua) ? 'Windows' : /Linux/.test(ua) ? 'Linux' : '';
  const br = /Edg\//.test(ua) ? 'Edge' : /OPR\//.test(ua) ? 'Opera' : /Firefox\//.test(ua) ? 'Firefox'
    : /(CriOS|Chrome)\//.test(ua) ? 'Chrome' : /Safari\//.test(ua) ? 'Safari' : /curl|python|httpx|requests/i.test(ua) ? '脚本' : '';
  return [os, br].filter(Boolean).join(' · ') || ua.slice(0, 40);
}

// ISP names: drop the company suffix; the big three Chinese carriers in Chinese
const CN_ISP = [[/china mobile|cmnet/i, '中国移动'], [/china unicom|\bcnc\b/i, '中国联通'], [/china telecom|chinanet/i, '中国电信']];
function shortIsp(isp = '') {
  const cn = CN_ISP.find(([re]) => re.test(isp));
  if (cn) return cn[1];
  return isp.replace(/^the\s+/i, '')
    .replace(/(,?\s+(communications?|corporation|corp\.?|co\.?,?\s*ltd\.?|inc\.?|llc|limited|k\.k\.))+\s*$/i, '').trim();
}

/** "日本 东京都 东京 · ARTERIA Networks" from the server's geo ({country, region, city, isp} or {label}); same as the portal's 公网 IP card. */
export function fmtGeo(g) {
  if (!g) return '';
  if (g.label) return g.label;
  const place = [g.country, g.region, g.city].filter((x, i, a) => x && a.indexOf(x) === i).join(' ');
  return [place, shortIsp(g.isp)].filter(Boolean).join(' · ');
}

/** A usage bar (@szyyw/design .bar): `used` fills it, `limit` (if any) draws a tick; both in percent. */
export function meterHtml({ label, used, limit, sub = '', right = null }) {
  const pct = used == null ? 0 : Math.min(100, used);
  const cls = limit != null && used != null && used >= limit ? ' over' : limit != null && used != null && used >= limit * 0.8 ? ' bp-warn' : '';
  const tick = limit != null && limit < 100 ? `<s style="left:${limit}%"></s>` : '';
  const txt = right ?? (used == null ? '—' : limit != null ? `${fmtPct(used)} / ${fmtPct(limit)}` : fmtPct(used));
  return `<div class="bp-meter"><div class="spread small"><span>${esc(label)}</span><b class="num">${esc(txt)}</b></div>`
    + `<div class="bar"><div class="bar-fill${cls}" style="width:${pct}%"></div>${tick}</div>${sub ? `<div class="tiny muted">${sub}</div>` : ''}</div>`;
}

/** One window of a user's usage (from /api/me or /api/admin/users). */
export function userMeter(w, label) {
  const bits = [];
  if (w.used_pct == null) bits.push(`${fmtUsd(w.cost_usd)} API 标价等价`);
  bits.push(`${w.turns} 次`);
  if (w.limit_pct == null) bits.push('不限');
  const right = w.used_pct == null && w.limit_pct != null ? `上限 ${fmtPct(w.limit_pct)}` : null;
  return meterHtml({ label, used: w.used_pct, limit: w.limit_pct, sub: esc(bits.join(' · ')), right });
}

export function copyText(text) {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text).then(() => toast('已复制'), () => toast('复制失败，请手动选中', true));
  toast('请手动选中复制', true);
  return Promise.resolve();
}
