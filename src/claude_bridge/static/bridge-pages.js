// Helpers shared by the multi-user pages (app / account / admin). Framework-free ES module.

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

let toastTimer;
export function toast(msg, isErr = false) {
  let t = document.querySelector('.bridge-toast');
  if (!t) { t = document.createElement('div'); t.className = 'bridge-toast'; document.body.appendChild(t); }
  t.textContent = msg; t.classList.toggle('err', isErr); t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, isErr ? 5000 : 2200);
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

/** A usage bar: `used` fills it, `limit` (if any) draws a tick; both in percent. */
export function meterHtml({ label, used, limit, sub = '', right = null }) {
  const pct = used == null ? 0 : Math.min(100, used);
  const cls = limit != null && used != null && used >= limit ? 'full' : limit != null && used != null && used >= limit * 0.8 ? 'warn' : '';
  const tick = limit != null && limit < 100 ? `<s style="left:${limit}%"></s>` : '';
  const txt = right ?? (used == null ? '—' : limit != null ? `${fmtPct(used)} / ${fmtPct(limit)}` : fmtPct(used));
  return `<div class="bp-meter"><div class="bp-meter-top"><span>${esc(label)}</span><b>${esc(txt)}</b></div>`
    + `<div class="bp-bar ${cls}"><i style="width:${pct}%"></i>${tick}</div>${sub ? `<div class="bp-meter-sub">${sub}</div>` : ''}</div>`;
}

/** One window of a user's usage (from /api/me or /api/admin/users). */
export function userMeter(w, label) {
  const bits = [];
  if (w.used_pct == null) bits.push(`${fmtUsd(w.cost_usd)} 等价`);
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
