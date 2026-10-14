// 消息区状态组件（工具行 / 状态图标 / 思考头）的纯函数测试；由 test_widget_js.py 跑。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  splitTurn, stepsHtml, fmtDur, callProgress, tickJobs, turnTiming, latticeHtml, markHtml, messageStrings, TOOL_EXPECTED_MS,
} from '../src/claude_bridge/static/bridge-widget.js';

const T = (sec) => `2026-10-14 03:00:${String(sec).padStart(2, '0')}`;
const at = (sec) => Date.parse(`2026-10-14T03:00:${String(sec).padStart(2, '0')}Z`);
const tool = (id, sec, cmd = 'ls', name = 'Bash') => ({ type: 'tool_use', created_at: T(sec), data: { id, name, input: name === 'Bash' ? { command: cmd } : { file_path: cmd }, at: 0 } });
const result = (id, sec, content = 'ok', is_error = false) => ({ type: 'tool_result', created_at: T(sec), data: { tool_use_id: id, content, is_error } });
const S = messageStrings('zh');

test('fmtDur: precise vs coarse (second-resolution server timestamps)', () => {
  assert.equal(fmtDur(0), '0.0s'); assert.equal(fmtDur(12345), '12.3s'); assert.equal(fmtDur(65000), '1m 05s');
  assert.equal(fmtDur(400, true), '<1s'); assert.equal(fmtDur(3400, true), '3s'); assert.equal(fmtDur(125000, true), '2m 05s');
  assert.equal(fmtDur(-1), ''); assert.equal(fmtDur(NaN), '');
});

test('callProgress: linear to 90% at the expected time, then only creeps; never reaches 1', () => {
  assert.equal(callProgress(0, 1000), 0);
  assert.ok(Math.abs(callProgress(500, 1000) - 0.45) < 1e-9);
  assert.ok(Math.abs(callProgress(1000, 1000) - 0.9) < 1e-9);
  let prev = 0.9;
  for (const ms of [1500, 3000, 10000, 1e6]) { const p = callProgress(ms, 1000); assert.ok(p >= prev && p < 0.99); prev = p; }
  assert.equal(callProgress(100, 0), 0); assert.equal(callProgress(-5, 1000), 0);
});

test('splitTurn records server timestamps on tool items; stepsHtml shows the call duration', () => {
  const items = splitTurn('', [tool('t1', 1), result('t1', 4, 'x')])[0].items;
  assert.equal(items[0].t0, at(1)); assert.equal(items[0].t1, at(4));
  const html = stepsHtml(items, { now: at(9) });
  assert.match(html, /<span class="bridge-time">3s<\/span>/);
  assert.match(html, /data-st="done"/); assert.match(html, /<svg class="bridge-mark" data-st="done"/);
  assert.doesNotMatch(html, /data-t0/);
  assert.match(html, /\$ ls/); assert.match(html, /<pre class="out">x<\/pre>/);
});

test('running tool: live timer + fill + determinate arc, expectation per tool and overridable', () => {
  const items = splitTurn('', [tool('t1', 0, 'sleep 9')])[0].items;
  const html = stepsHtml(items, { streaming: true, now: at(3) });
  assert.match(html, /<summary data-t0="\d+" data-exp="6000">/);
  assert.match(html, new RegExp(`data-t0="${at(0)}"`));
  assert.match(html, /scaleX\(0\.450\)/); assert.match(html, /stroke-dasharray:0\.450 1/);
  assert.match(html, /data-timer>3\.0s</);
  assert.match(html, /class="bridge-step tool running"/);
  const over = stepsHtml(items, { streaming: true, now: at(3), expected: { Bash: 30000 } });
  assert.match(over, /data-exp="30000"/); assert.match(over, /scaleX\(0\.090\)/);
  assert.match(stepsHtml(items, { streaming: true, now: at(3), expected: () => 12000 }), /data-exp="12000"/);
  assert.equal(TOOL_EXPECTED_MS.Bash, 6000);
});

test('running tool without timestamps: indeterminate arc, no timer, no tick hook', () => {
  const items = [{ kind: 'tool', id: 't', name: 'Bash', input: { command: 'x' }, result: null, at: 0 }];
  const html = stepsHtml(items, { streaming: true });
  assert.doesNotMatch(html, /data-t0/); assert.doesNotMatch(html, /data-timer/);
  assert.match(html, /animation-delay:-\d+ms/);
});

test('failed tool: red mark, 失败, duration kept; no-result tool when not streaming', () => {
  const err = splitTurn('', [tool('t1', 0, 'false'), result('t1', 2, 'boom', true)])[0].items;
  const html = stepsHtml(err);
  assert.match(html, /class="bridge-step tool error"/); assert.match(html, /data-st="failed"/);
  assert.match(html, /bridge-step-state err">失败/); assert.match(html, /2s<\/span>/);
  const none = stepsHtml(splitTurn('', [tool('t1', 0)])[0].items, { streaming: false });
  assert.match(none, /data-st="cancelled"/); assert.match(none, /无结果/);
});

test('group: running count, partial progress, total timer; done keeps 执行了 N 条命令 with total span', () => {
  const evs = [tool('a', 0), result('a', 2), tool('b', 2), tool('c', 2)];
  const items = splitTurn('', evs)[0].items;
  const live = stepsHtml(items, { streaming: true, now: at(3) });
  assert.match(live, /执行中 · 1\/3/); assert.match(live, /<summary data-t0="\d+"><span class="bridge-fill" style="transform:scaleX\(/);
  assert.match(live, /data-timer>3\.0s</);
  const all = splitTurn('', [tool('a', 0), result('a', 2), tool('b', 2), result('b', 5, 'no', true)])[0].items;
  const done = stepsHtml(all);
  assert.match(done, /执行了 2 条命令/); assert.match(done, /有失败/); assert.match(done, /<span class="bridge-time">5s<\/span>/);
  assert.match(done, /class="bridge-step group error"/); assert.match(done, /data-st="failed"/);
});

test('thinking block uses the status mark instead of the blinking dot', () => {
  const html = stepsHtml([{ kind: 'thinking', text: 'hmm', streaming: true }], { streaming: true });
  assert.match(html, /bridge-mark" data-st="running"/); assert.doesNotMatch(html, /bridge-run/);
  assert.match(stepsHtml([{ kind: 'thinking', text: 'hmm' }]), /data-st="done"/);
});

test('turnTiming: usage.duration_ms > server timestamps > client first-frame/done', () => {
  const base = { role: 'assistant', created_at: T(0), updated_at: T(14), status: 'done' };
  assert.deepEqual(turnTiming({ ...base, events: [{ type: 'usage', data: { duration_ms: 12345 } }] }), { state: 'done', start: at(0), dur: 12345, coarse: false, pending: false });
  const srv = turnTiming({ ...base, events: [{ type: 'init', created_at: T(2), data: {} }] });
  assert.equal(srv.dur, 12000); assert.equal(srv.coarse, true); assert.equal(srv.state, 'done');
  const noStamp = turnTiming({ role: 'assistant', status: 'done', _t0: 1000, _t1: 4200, events: [] });
  assert.equal(noStamp.dur, 3200); assert.equal(noStamp.coarse, false);
  assert.equal(turnTiming({ role: 'assistant', status: 'done', events: [] }).state, 'none');
  assert.equal(turnTiming({ ...base, status: 'cancelled', events: [] }).state, 'stopped');
});

test('turnTiming: live turns start at init (running) or created_at (pending)', () => {
  const m = { role: 'assistant', created_at: T(0), status: 'streaming', events: [{ type: 'init', created_at: T(3), data: {} }] };
  assert.deepEqual(turnTiming(m, at(9)), { state: 'live', start: at(3), dur: null, coarse: true, pending: false });
  assert.deepEqual(turnTiming({ ...m, status: 'pending', events: [] }, at(9)), { state: 'live', start: at(0), dur: null, coarse: true, pending: true });
  assert.equal(turnTiming({ role: 'assistant', status: 'streaming', events: [] }, 777).start, 777);
});

test('latticeHtml: live wave + ticking timer; done freezes to "已思考 12.3s"; stopped; none renders nothing; i18n', () => {
  const live = latticeHtml({ state: 'live', start: at(0), pending: false }, S, at(5));
  assert.match(live, /data-st="running" data-t0="\d+"/); assert.match(live, /data-timer>5\.0s</); assert.match(live, /Claude 正在思考…/);
  assert.equal((live.match(/<i style="animation-delay:-\d+ms"><\/i>/g) || []).length, 8); assert.match(live, /<i data-hole>/);
  assert.match(latticeHtml({ state: 'live', start: at(0), pending: true }, S, at(5)), /等待 helper 接单…/);
  const done = latticeHtml({ state: 'done', dur: 12345, coarse: false }, S);
  assert.match(done, /已思考<\/span><span class="bridge-lat-timer">12\.3s</); assert.doesNotMatch(done, /data-t0/);
  assert.equal((done.match(/data-on/g) || []).length, 4);
  const stopped = latticeHtml({ state: 'stopped', dur: 4000, coarse: true }, S);
  assert.match(stopped, /data-st="failed"/); assert.match(stopped, /已中断，用时/); assert.equal((stopped.match(/data-on/g) || []).length, 5);
  assert.equal(latticeHtml({ state: 'none' }, S), '');
  assert.match(latticeHtml({ state: 'done', dur: 3000 }, messageStrings('ja')), /思考時間/);
  assert.match(latticeHtml({ state: 'done', dur: 3000 }, messageStrings('en')), /Thought for/);
  assert.equal(messageStrings('fr').thought, '已思考'); assert.equal(messageStrings('en-US').thought, 'Thought for');
});

test('wave phase follows the wall clock, so re-renders do not restart it', () => {
  const delays = (now) => [...latticeHtml({ state: 'live', start: 0, pending: false }, S, now).matchAll(/delay:-(\d+)ms/g)].map(m => +m[1]);
  const a = delays(10_000), b = delays(10_000 + 864);
  assert.deepEqual(a, b); assert.ok(a.every(v => v >= 0 && v < 864));
  assert.notDeepEqual(a, delays(10_100));
});

test('markHtml: states and escaping of the label', () => {
  for (const st of ['pending', 'running', 'done', 'failed', 'cancelled']) assert.match(markHtml(st), new RegExp(`data-st="${st}"`));
  assert.match(markHtml('running', { progress: 0.5 }), /stroke-dasharray:0\.500 1/);
  assert.doesNotMatch(markHtml('done', { label: '<b>' }), /<b>/);
});

test('tickJobs refreshes timers, fill and arc of live items (and still the job counters)', () => {
  const made = () => ({ textContent: '', style: {} });
  const timer = made(), fill = made(), ring = made(), job = Object.assign(made(), { dataset: { jobSince: '2026-10-14 03:00:00' } });
  const chip = { dataset: { t0: String(at(0)), exp: '6000' }, querySelector: (q) => ({ '[data-timer]': timer, '.bridge-fill': fill, '.bridge-mark .ring': ring })[q] };
  const plain = { dataset: { t0: String(at(0)) }, querySelector: (q) => q === '[data-timer]' ? timer : null };
  const root = (list) => ({ querySelectorAll: (q) => q === '[data-job-since]' ? [job] : q === '[data-t0]' ? list : [] });
  tickJobs(root([chip]), at(3));
  assert.equal(timer.textContent, '3.0s'); assert.equal(fill.style.transform, 'scaleX(0.450)'); assert.equal(ring.style.strokeDasharray, '0.450 1');
  assert.equal(job.textContent, '0:03');
  tickJobs(root([plain]), at(0) + 70000); assert.equal(timer.textContent, '1m 10s');
  tickJobs(root([chip]), at(0) - 5000); assert.equal(timer.textContent, '0.0s');
});
