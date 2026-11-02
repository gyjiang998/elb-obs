'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {RecursiveRefreshTimer} = require('../renderer/remote-tree-refresh.cjs');

const flush = () => new Promise(resolve => setImmediate(resolve));

test('ten-minute recursive refresh waits for completion and never overlaps', async () => {
  const timers = new Map(); let nextTimer = 1;
  const setTimer = (callback, delay) => { const id = nextTimer++; timers.set(id, {callback, delay}); return id; };
  const clearTimer = id => timers.delete(id);
  let runs = 0; let release;
  const scheduler = new RecursiveRefreshTimer({
    intervalMs: 10 * 60 * 1000,
    setTimer,
    clearTimer,
    run: () => { runs++; return new Promise(resolve => { release = resolve; }); },
  });
  scheduler.start();
  assert.equal(timers.size, 1);
  const first = [...timers.values()][0];
  assert.equal(first.delay, 600000);
  timers.clear();
  void first.callback();
  await flush();
  assert.equal(runs, 1);
  assert.equal(timers.size, 0, 'the next timer is not armed while refresh is running');
  release();
  await flush();
  assert.equal(timers.size, 1, 'the next ten-minute period starts after completion');
  scheduler.stop();
  assert.equal(timers.size, 0);
});

test('disconnecting while a periodic refresh runs prevents rearming', async () => {
  const timers = new Map(); let nextTimer = 1;
  let release;
  const scheduler = new RecursiveRefreshTimer({
    intervalMs: 600000,
    setTimer(callback, delay) { const id = nextTimer++; timers.set(id, {callback, delay}); return id; },
    clearTimer(id) { timers.delete(id); },
    run: () => new Promise(resolve => { release = resolve; }),
  });
  scheduler.start();
  const first = [...timers.values()][0]; timers.clear();
  void first.callback();
  await flush();
  scheduler.stop();
  release();
  await flush();
  assert.equal(timers.size, 0);
});
