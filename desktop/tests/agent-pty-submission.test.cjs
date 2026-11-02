'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const {agentPasteTiming, waitForAgentPasteSettle} = require('../runtime/index.cjs');

test('Desktop submit delay UI exposes the same 100-5000 ms contract as runtime', () => {
  const html = fs.readFileSync(path.resolve(__dirname, '../renderer/index.html'), 'utf8');
  const renderer = fs.readFileSync(path.resolve(__dirname, '../renderer/renderer.js'), 'utf8');
  const input = html.match(/<input id="agent-submit-delay"[^>]*>/)?.[0] || '';
  assert.match(input, /min="100"/);
  assert.match(input, /max="5000"/);
  assert.match(input, /value="1000"/);
  assert.match(renderer, /function updateSubmitDelay\(\)\s*\{\s*const minimum = 100;/);
});

test('long bracketed-paste prompts receive a larger bounded settle window', () => {
  const short = agentPasteTiming('短消息', 100);
  const long = agentPasteTiming('长消息'.repeat(12000), 100);
  assert.equal(short.minimumMs, 100);
  assert.equal(short.noOutputWaitMs, 100);
  assert.ok(long.minimumMs > short.minimumMs);
  assert.ok(long.minimumMs <= 2100);
  assert.ok(long.maximumWaitMs > long.noOutputWaitMs);
});

test('a short prompt without terminal output submits at the configured 100 ms floor', async () => {
  let clock = 0;
  const result = await waitForAgentPasteSettle({
    text: '短消息', configuredDelayMs: 100, outputSequenceBefore: 3,
    readOutputSequence: () => 3,
    readLastOutputAt: () => 0,
    now: () => clock,
    sleep: async ms => { clock += ms; },
  });
  assert.equal(result.minimumMs, 100);
  assert.equal(result.actualWaitMs, 100);
  assert.equal(result.waitReason, 'no_output_wait_elapsed');
});

test('paste settling ignores stale output from before this paste', async () => {
  let clock = 1000;
  const result = await waitForAgentPasteSettle({
    text: '本次粘贴没有回显', configuredDelayMs: 0, outputSequenceBefore: 7,
    readOutputSequence: () => 7,
    // A recent timestamp alone must not count as acknowledgement for the new
    // paste. This was the lastAgentOutputAt race in the former implementation.
    readLastOutputAt: () => 999,
    now: () => clock,
    sleep: async ms => { clock += ms; },
    timingOverrides: {minimumMs: 40, noOutputWaitMs: 90, maximumWaitMs: 150, quietMs: 20},
  });
  assert.equal(result.waitReason, 'no_output_wait_elapsed');
  assert.equal(result.outputObserved, false);
  assert.ok(result.actualWaitMs >= 90);
});

test('paste settling waits for output from this paste and a quiet editor window', async () => {
  let clock = 0;
  let sequence = 4;
  let lastOutputAt = 0;
  const result = await waitForAgentPasteSettle({
    text: '全屏编辑器分批渲染', configuredDelayMs: 0, outputSequenceBefore: sequence,
    readOutputSequence: () => sequence,
    readLastOutputAt: () => lastOutputAt,
    now: () => clock,
    sleep: async ms => {
      clock += ms;
      if (clock >= 30 && sequence === 4) { sequence++; lastOutputAt = clock; }
      if (clock >= 70 && sequence === 5) { sequence++; lastOutputAt = clock; }
    },
    timingOverrides: {minimumMs: 80, noOutputWaitMs: 120, maximumWaitMs: 180, quietMs: 30},
  });
  assert.equal(result.waitReason, 'output_settled');
  assert.equal(result.outputObserved, true);
  assert.ok(result.actualWaitMs >= 100);
});

test('paste settling fences a replaced Agent session before Enter', async () => {
  let current = true;
  let clock = 0;
  await assert.rejects(waitForAgentPasteSettle({
    text: '不会落入新会话', configuredDelayMs: 0, outputSequenceBefore: 0,
    readOutputSequence: () => 0,
    readLastOutputAt: () => 0,
    sessionCurrent: () => current,
    now: () => clock,
    sleep: async ms => { clock += ms; current = false; },
    timingOverrides: {minimumMs: 40, noOutputWaitMs: 80, maximumWaitMs: 120, quietMs: 20},
  }), /会话已关闭/);
});
