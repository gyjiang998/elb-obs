const test = require('node:test');
const assert = require('node:assert/strict');
const { DEFAULT_PREFERENCES, normalizePreferences, resolveLayout } = require('../renderer/layout-geometry.cjs');

test('default layout uses a fixed activity rail, a full-height Agent and a page terminal', () => {
  const result = resolveLayout(DEFAULT_PREFERENCES, { width: 1500, height: 920 });
  assert.equal(result.sidebarWidth, 50);
  assert.equal(result.agentWidth, 600);
  assert.equal(result.terminalHeight, 307);
  assert.ok(1500 - result.sidebarWidth - result.agentWidth - 12 >= 430);
  const compact = resolveLayout(DEFAULT_PREFERENCES, { width: 1020, height: 700 });
  assert.equal(compact.sidebarWidth, 50);
  assert.equal(compact.agentWidth, 408);
  assert.equal(compact.terminalHeight, 233);
  assert.ok(1020 - compact.sidebarWidth - compact.agentWidth - 12 >= 430);
});

test('Agent can grow to a wider desktop limit while preserving the central work area', () => {
  const result = resolveLayout({ ...DEFAULT_PREFERENCES, agentWidth: 2000 }, { width: 1500, height: 920 });
  assert.equal(result.agentWidth, 900);
  assert.ok(1500 - result.sidebarWidth - result.agentWidth - 12 >= 430);
});

test('restored large dimensions clamp in a smaller window without destroying the saved preference', () => {
  const preferred = { ...DEFAULT_PREFERENCES, sidebarWidth: 300, agentWidth: 650, terminalHeight: 600 };
  const smaller = resolveLayout(preferred, { width: 1020, height: 650 });
  assert.equal(smaller.sidebarWidth, 50);
  assert.equal(smaller.agentWidth, 528);
  assert.equal(smaller.terminalHeight, 364);
  assert.ok(1020 - smaller.sidebarWidth - smaller.agentWidth - 12 >= 430);
  const larger = resolveLayout(preferred, { width: 1800, height: 1000 });
  assert.equal(larger.agentWidth, 650);
  assert.equal(larger.terminalHeight, 600);
  assert.equal(preferred.terminalHeight, 600);
});

test('minimum panel sizes and compact windows do not yield negative dimensions', () => {
  const small = resolveLayout({ ...DEFAULT_PREFERENCES, sidebarWidth: 10, agentWidth: 10, terminalHeight: 10 }, { width: 1020, height: 700 });
  assert.equal(small.sidebarWidth, 50);
  assert.equal(small.agentWidth, 340);
  assert.equal(small.terminalHeight, 180);
  const tiny = resolveLayout(DEFAULT_PREFERENCES, { width: 500, height: 200 });
  assert.ok(tiny.sidebarWidth >= 0);
  assert.ok(tiny.agentWidth >= 0);
  assert.ok(tiny.terminalHeight <= 80);
  assert.ok(tiny.sidebarWidth + tiny.agentWidth + 12 <= 500);
});

test('saved preferences accept only versioned dimensions and booleans, excluding unrelated settings', () => {
  const state = normalizePreferences({ version: 2, sidebarWidth: NaN, agentWidth: 412, terminalHeight: -8, agentCollapsed: 'true', terminalCollapsed: true, token: 'unrelated' });
  assert.deepEqual(state, { ...DEFAULT_PREFERENCES, agentWidth: 412, terminalCollapsed: true });
  assert.deepEqual(normalizePreferences({ version: 1, agentWidth: 600 }), DEFAULT_PREFERENCES);
  assert.deepEqual(normalizePreferences({ version: 3, agentWidth: 600 }), DEFAULT_PREFERENCES);
  assert.deepEqual(normalizePreferences(null), DEFAULT_PREFERENCES);
});
