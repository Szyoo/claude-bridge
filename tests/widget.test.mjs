// Pure-function tests for bridge-widget.js (run by test_widget_js.py via `node --test`).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  splitTurn, stepsHtml, sessionSummary, sanitizePrefs, loadPrefs, isSendKey, fmtTime, DEFAULT_PREFS, planImage, IMAGE_LIMITS, modelOptionsHtml,
} from '../src/claude_bridge/static/bridge-widget.js';

const tool = (id, at, cmd = 'ls', name = 'Bash') => ({ type: 'tool_use', data: { id, name, input: name === 'Bash' ? { command: cmd } : { file_path: cmd }, at } });
const result = (id, content, is_error = false) => ({ type: 'tool_result', data: { tool_use_id: id, content, is_error } });
const kinds = (segs) => segs.map(s => s.kind === 'text' ? `T(${JSON.stringify(s.text)})` : `S[${s.items.map(i => i.kind === 'tool' ? i.id : i.kind).join(',')}]`);

test('no events → one text segment; empty content → nothing', () => {
  assert.deepEqual(splitTurn('hi', []), [{ kind: 'text', text: 'hi' }]);
  assert.deepEqual(splitTurn('', []), []);
});

test('tool at 0 goes before the text', () => {
  assert.deepEqual(kinds(splitTurn('A。\n\n', [tool('t1', 0)])), ['S[t1]', 'T("A。\\n\\n")']);
});

test('tool exactly at a paragraph boundary splits there; mid-paragraph moves to the next boundary', () => {
  const content = 'A。\n\nB。\n\n';
  assert.deepEqual(kinds(splitTurn(content, [tool('t1', 4)])), ['T("A。\\n\\n")', 'S[t1]', 'T("B。\\n\\n")']);
  assert.deepEqual(kinds(splitTurn(content, [tool('t1', 1)])), ['T("A。\\n\\n")', 'S[t1]', 'T("B。\\n\\n")']);
});

test('adjacent tools with no text between merge into one group, results pair by id', () => {
  const evs = [tool('t1', 4, 'a'), result('t1', 'out1'), tool('t2', 4, 'b'), result('t2', 'boom', true)];
  const segs = splitTurn('A。\n\nB', evs);
  assert.deepEqual(kinds(segs), ['T("A。\\n\\n")', 'S[t1,t2]', 'T("B")']);
  const [a, b] = segs[1].items;
  assert.equal(a.result.content, 'out1'); assert.equal(b.result.is_error, true);
});

test('never cuts inside a fenced code block', () => {
  const content = 'x\n\n```\na\n\nb\n```\n\ny';
  assert.deepEqual(kinds(splitTurn(content, [tool('t1', 5)])), ['T("x\\n\\n```\\na\\n\\nb\\n```\\n\\n")', 'S[t1]', 'T("y")']);
});

test('at beyond the streamed text (lag) → group at the end; later text lands after it', () => {
  assert.deepEqual(kinds(splitTurn('A。', [tool('t1', 50)])), ['T("A。")', 'S[t1]']);
});

test('error / cancel notes sit at the end, compact at the start; usage/init/rate_limit are ignored', () => {
  const evs = [{ type: 'usage', data: {} }, { type: 'init', data: {} }, { type: 'rate_limit', data: {} },
    { type: 'error', data: { message: 'x' } }, { type: 'status', data: { phase: 'cancel_requested' } },
    { type: 'compact', data: { trigger: 'auto', pre_tokens: 1000, post_tokens: 100 } }];
  assert.deepEqual(kinds(splitTurn('A。\n\nB', evs)), ['S[note]', 'T("A。\\n\\nB")', 'S[note,note]']);
});

test('`at` counts code points, not UTF-16 units', () => {
  assert.deepEqual(kinds(splitTurn('😀😀\n\nB', [tool('t1', 4)])), ['T("😀😀\\n\\n")', 'S[t1]', 'T("B")']);
});

test('thinking uses its own at (falls back to 0 for old events)', () => {
  const evs = [{ type: 'thinking', data: { text: 'hmm' } }, { type: 'thinking', data: { text: 'again', at: 4 } }];
  assert.deepEqual(kinds(splitTurn('A。\n\nB', evs)), ['S[thinking]', 'T("A。\\n\\n")', 'S[thinking]', 'T("B")']);
});

test('stepsHtml: two Bash tools collapse into one summary line; running / error states', () => {
  const items = splitTurn('', [tool('t1', 0, 'a'), result('t1', 'o'), tool('t2', 0, 'b')])[0].items;
  assert.match(stepsHtml(items, { streaming: true }), /执行中 · 1\/2/);
  const done = stepsHtml(items, { streaming: false });
  assert.match(done, /执行了 2 条命令/); assert.match(done, /无结果/);
  const mixed = splitTurn('', [tool('t1', 0, 'a'), tool('t2', 0, 'f.txt', 'Read')])[0].items;
  assert.match(stepsHtml(mixed), /2 次工具调用/);
  const err = splitTurn('', [tool('t1', 0, 'a'), result('t1', 'bad', true)])[0].items;
  assert.match(stepsHtml(err), /class="bridge-step tool error"/); assert.match(stepsHtml(err), /失败/);
  assert.match(stepsHtml(err, { open: true }), /<details[^>]* open>/);
  assert.match(stepsHtml(err), /\$ a/);
});

test('stepsHtml escapes tool output', () => {
  const items = splitTurn('', [tool('t1', 0, 'x'), result('t1', '<script>alert(1)</script>')])[0].items;
  assert.doesNotMatch(stepsHtml(items), /<script>/);
});

test('sanitizePrefs clamps and falls back; loadPrefs survives broken storage', () => {
  const p = sanitizePrefs({ scale: 9, height: 10, density: 'huge', width: 'x', sendKey: 'y', sideW: 10, inputH: 9999, timestamps: 0 });
  assert.equal(p.scale, 1.4); assert.equal(p.height, 50); assert.equal(p.density, 'cozy'); assert.equal(p.width, 'cozy');
  assert.equal(p.sendKey, 'enter'); assert.equal(p.sideW, 180); assert.equal(p.inputH, 600); assert.equal(p.timestamps, false);
  assert.deepEqual(sanitizePrefs(null), DEFAULT_PREFS);
  globalThis.localStorage = { getItem() { throw new Error('blocked'); } };
  assert.deepEqual(loadPrefs('k'), DEFAULT_PREFS);
  globalThis.localStorage = { getItem() { return '{not json'; } };
  assert.deepEqual(loadPrefs('k'), DEFAULT_PREFS);
  globalThis.localStorage = { getItem() { return JSON.stringify({ scale: 1.2, sendKey: 'mod' }); } };
  assert.equal(loadPrefs('k').scale, 1.2); assert.equal(loadPrefs('k').sendKey, 'mod');
});

test('isSendKey follows the preference', () => {
  const enter = { key: 'Enter' }, shift = { key: 'Enter', shiftKey: true }, mod = { key: 'Enter', metaKey: true }, ime = { key: 'Enter', isComposing: true };
  assert.equal(isSendKey(enter, { sendKey: 'enter' }), true);
  assert.equal(isSendKey(shift, { sendKey: 'enter' }), false);
  assert.equal(isSendKey(enter, { sendKey: 'mod' }), false);
  assert.equal(isSendKey(mod, { sendKey: 'mod' }), true);
  assert.equal(isSendKey(ime, { sendKey: 'enter' }), false);
});

test('sessionSummary exposes pct for the pill and the context part', () => {
  const msgs = [{ events: [{ type: 'usage', data: { model: 'claude-opus-5', context_tokens: 100000, output_tokens: 10, num_turns: 3 } }] }];
  const s = sessionSummary(msgs);
  assert.equal(s.pct, 50);
  assert.ok(s.parts.find(p => p.key === 'context').clickable);
  assert.equal(s.parts[0].label, 'opus-5');
});

test('fmtTime: today → HH:MM only, other days carry M/D', () => {
  const now = new Date(Date.UTC(2026, 8, 23, 12, 0, 0));
  assert.doesNotMatch(fmtTime('2026-09-23 03:00:00', now), /\//);
  assert.match(fmtTime('2026-09-01 03:00:00', now), /^9\/1 /);
  assert.equal(fmtTime(null), '');
});

// ---------- images ----------
const MB = 1024 * 1024;
const coverage = (tiles, len, axis) => {   // every destination pixel of the long side is covered, in order
  let reach = 0;
  for (const t of tiles) { const a = axis === 'y' ? t.sy : t.sx, l = axis === 'y' ? t.sh : t.sw; assert.ok(a <= reach + 1e-6, 'gap between tiles'); reach = Math.max(reach, a + l); }
  assert.ok(Math.abs(reach - len) < 1e-6, `tiles end at ${reach}, expected ${len}`);
};

test('screenshots that already fit go up untouched', () => {
  assert.deepEqual(planImage({ width: 800, height: 600, type: 'image/png', size: 200e3 }), { mode: 'asis' });
  assert.deepEqual(planImage({ width: 2000, height: 1000, type: 'image/jpeg', size: MB }), { mode: 'asis' });
});

test('iPhone screenshots are scaled to a 2000px long edge and stay PNG', () => {
  assert.deepEqual(planImage({ width: 1179, height: 2556, type: 'image/png', size: 2 * MB }), { mode: 'scale', w: 923, h: 2000, out: 'image/png' });
  assert.deepEqual(planImage({ width: 1290, height: 2796, type: 'image/png', size: 3 * MB }), { mode: 'scale', w: 923, h: 2000, out: 'image/png' });
});

test('camera photos (HEIC / big JPEG) are re-encoded as JPEG within the box', () => {
  assert.deepEqual(planImage({ width: 3024, height: 4032, type: 'image/heic', size: 3 * MB }), { mode: 'scale', w: 1500, h: 2000, out: 'image/jpeg' });
  // fits the box but too heavy: re-encode at the same size
  assert.deepEqual(planImage({ width: 1800, height: 1800, type: 'image/png', size: 9 * MB }), { mode: 'scale', w: 1800, h: 1800, out: 'image/png' });
});

test('a long screenshot is cut into overlapping 2000px pieces at full width', () => {
  const p = planImage({ width: 1179, height: 6000, type: 'image/png', size: 4 * MB });
  assert.equal(p.mode, 'tiles'); assert.equal(p.out, 'image/png'); assert.equal(p.tiles.length, 4);
  for (const t of p.tiles) { assert.equal(t.dw, 1179); assert.ok(t.dh <= IMAGE_LIMITS.max); assert.equal(t.sx, 0); assert.equal(t.sw, 1179); }
  coverage(p.tiles, 6000, 'y');
  for (let i = 1; i < p.tiles.length; i++) assert.ok(p.tiles[i - 1].sy + p.tiles[i - 1].sh - p.tiles[i].sy >= IMAGE_LIMITS.overlap, 'overlap too small');
});

test('extremely long screenshots are shrunk until they fit in 8 pieces', () => {
  const p = planImage({ width: 1000, height: 40000, type: 'image/png', size: 6 * MB });
  assert.equal(p.mode, 'tiles'); assert.equal(p.tiles.length, IMAGE_LIMITS.maxTiles);
  for (const t of p.tiles) { assert.ok(t.dh <= IMAGE_LIMITS.max); assert.ok(t.dw < 1000); }
  coverage(p.tiles, 40000, 'y');
});

test('wide panoramas are cut horizontally; a wide-but-short image with a huge short side is scaled first', () => {
  const p = planImage({ width: 8000, height: 1000, type: 'image/jpeg', size: 5 * MB });
  assert.equal(p.mode, 'tiles'); assert.equal(p.out, 'image/jpeg');
  for (const t of p.tiles) { assert.equal(t.dh, 1000); assert.ok(t.dw <= IMAGE_LIMITS.max); }
  coverage(p.tiles, 8000, 'x');
  const q = planImage({ width: 7000, height: 2500, type: 'image/png', size: 5 * MB });   // short side > 2000 → scaled to 2000 across
  assert.equal(q.mode, 'tiles'); for (const t of q.tiles) assert.equal(t.dh, 2000);
});

test('modelOptionsHtml: optgroups for reported models, unknown saved value kept and flagged', () => {
  const models = [{ id: '', label: '默认' }, { id: 'opus', label: '最新 Opus（5.5）', group: '跟随 CLI 最新' },
    { id: 'claude-opus-5', label: 'Opus 5', group: '固定版本' }];
  const html = modelOptionsHtml(models, 'opus');
  assert.match(html, /^<option value="">默认<\/option><optgroup label="跟随 CLI 最新"><option value="opus" selected>/);
  assert.match(html, /<\/optgroup><optgroup label="固定版本"><option value="claude-opus-5">Opus 5<\/option><\/optgroup>$/);
  const stale = modelOptionsHtml(models, 'claude-opus-9');
  assert.match(stale, /<option value="claude-opus-9" selected>claude-opus-9（helper 的 CLI 不认）<\/option>$/);
  assert.doesNotMatch(modelOptionsHtml([{ id: 'a', label: 'A' }], 'a'), /optgroup/);
});
