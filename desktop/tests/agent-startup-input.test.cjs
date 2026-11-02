'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const {DesktopRuntime} = require('../runtime/index.cjs');

function createPty(writes, environmentSink = null) {
  let exitHandler = null;
  return {
    module: {
      spawn(_file, _args, options) {
        if (environmentSink) Object.assign(environmentSink, options.env);
        return {
          pid: 42,
          write(value) { writes.push({value, at: Date.now()}); },
          resize() {},
          kill() { exitHandler?.({exitCode: 0, signal: 0}); },
          onData() {},
          onExit(handler) { exitHandler = handler; },
        };
      },
    },
  };
}

async function fixture(t, {startupDelayMs = 0, bootstrapContext = true, loadPersisted = false} = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-agent-no-bootstrap-'));
  const workspace = path.join(directory, 'workspace');
  fs.mkdirSync(workspace);
  const persistedAgent = {
    demo: false, executable: process.execPath, args: [], cwd: workspace,
    submitMode: 'plain_text_cr', submitDelayMs: 0, startupDelayMs, bootstrapContext,
  };
  if (loadPersisted) {
    const preferences = path.join(directory, 'prefs');
    fs.mkdirSync(preferences);
    fs.writeFileSync(path.join(preferences, 'settings.json'), JSON.stringify({agent: persistedAgent}));
  }
  const writes = [];
  const environment = {};
  const pty = createPty(writes, environment);
  const runtime = new DesktopRuntime({
    userDataDir: path.join(directory, 'prefs'),
    resourcesDir: path.resolve(__dirname, '..'),
    version: 'test',
    ptyModule: pty.module,
  });
  const events = [];
  runtime.on('event', event => events.push(event));
  t.after(async () => {
    await runtime.dispose();
    fs.rmSync(directory, {recursive: true, force: true});
  });
  await runtime.getState();
  await runtime.startAgent(loadPersisted ? {} : persistedAgent);
  return {runtime, writes, environment, events, workspace};
}

test('starting a custom Agent never writes a bootstrap prompt into its PTY', async t => {
  const {runtime, writes, events} = await fixture(t, {bootstrapContext: true});
  await new Promise(resolve => setTimeout(resolve, 50));
  assert.deepEqual(writes, []);
  const state = await runtime.getState();
  assert.equal(state.agent.bootstrapContextStatus.state, 'disabled');
  assert.equal(state.agent.bootstrapContextStatus.enabled, false);
  assert.ok(!events.some(event => event.kind === 'bootstrap_context'));
});

test('a persisted bootstrapContext=true value is compatibility-only and the first task is sent verbatim', async t => {
  const {runtime, writes} = await fixture(t, {bootstrapContext: true, loadPersisted: true});
  await runtime.submitAgent({text: '检查远端构建脚本'});
  assert.deepEqual(writes.map(item => item.value), ['检查远端构建脚本', '\r']);
  assert.ok(!writes.some(item => item.value.includes('工作台上下文')));
  assert.ok(!writes.some(item => item.value.includes('HETERO_CONTEXT_READY')));
  assert.equal(Object.hasOwn((await runtime.getState()).settings.agent, 'bootstrapContext'), false);
});

test('first task still observes the configured CLI startup delay without hidden input', async t => {
  const startedAt = Date.now();
  const {runtime, writes} = await fixture(t, {startupDelayMs: 40});
  assert.deepEqual(writes, []);
  await runtime.submitAgent({text: 'FIRST-TASK'});
  assert.deepEqual(writes.map(item => item.value), ['FIRST-TASK', '\r']);
  assert.ok(writes[0].at - startedAt >= 30);
});

test('manual Agent terminal input has no bootstrap dependency', async t => {
  const {runtime, writes} = await fixture(t);
  await runtime.terminalInput({target: 'agent', data: 'x'});
  const interrupt = await runtime.terminalInput({target: 'agent', data: '\x03'});
  assert.deepEqual(writes.map(item => item.value), ['x', '\x03']);
  assert.equal(interrupt.bypassedQueue, true);
});

test('desktop tools remain available through the Agent environment without prompt injection', async t => {
  const {runtime, writes, environment} = await fixture(t);
  assert.deepEqual(writes, []);
  assert.ok(environment.HETERO_DESKTOP_TOOLS);
  assert.ok(environment.HETERO_DESKTOP_MCP_CONFIG);
  assert.ok(environment.HETERO_AGENT_MCP_CONFIG);
  assert.ok(environment.HETERO_DESKTOP_BROKER_URL);
  assert.ok(environment.HETERO_DESKTOP_BROKER_TOKEN);
  assert.ok(environment.HETERO_AGENT_SESSION_ID);
  assert.equal((await runtime.getState()).agent.state, 'running');
});

test('legacy disabled setting also starts silently and sends tasks normally', async t => {
  const {runtime, writes} = await fixture(t, {bootstrapContext: false});
  assert.deepEqual(writes, []);
  await runtime.submitAgent({text: 'DIRECT'});
  assert.deepEqual(writes.map(item => item.value), ['DIRECT', '\r']);
  assert.equal(Object.hasOwn((await runtime.getState()).settings.agent, 'bootstrapContext'), false);
});
