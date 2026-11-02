'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const {DesktopAgentRunStore} = require('../runtime/agent-run-store.cjs');

test('Desktop Agent runs persist tasks, merge operation lifecycle events and expose bottlenecks', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-agent-run-store-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const store = new DesktopAgentRunStore(root);

  store.start({sessionId: 'session-1', executable: '/opt/bin/code-agent', cwd: '/srv/workspace'});
  store.submit('analyse the selected trace');
  store.agentOutput('\r\n\u001b[2K');
  store.agentOutput('reading trace\n');
  store.operation({type: 'operation', id: 'page-rpc', target: 'platform', kind: 'platform_rpc', origin: 'platform',
    method: 'GET', path: '/v1/application-flows', state: 'completed', durationMs: 900});
  store.operation({type: 'operation', id: 'rpc-1', target: 'platform', kind: 'platform_rpc', origin: 'agent',
    method: 'POST', path: '/v1/application-flows', state: 'running'});
  store.operation({type: 'operation', id: 'rpc-1', target: 'platform', kind: 'platform_rpc', origin: 'agent',
    method: 'POST', path: '/v1/application-flows', state: 'completed', durationMs: 420, exitCode: 0,
    channelReused: true});
  store.operation({type: 'operation', id: 'ssh-1', target: 'ssh', kind: 'remote_exec', origin: 'agent',
    state: 'completed', durationMs: 80, transportDurationMs: 96, exitCode: 0});
  store.operation({type: 'operation', id: 'workspace-1', target: 'workspace', kind: 'workspace_read', origin: 'agent',
    state: 'completed', durationMs: 30, queueWaitMs: 4, transportDurationMs: 18,
    channelAcquireDurationMs: 2, channelStartupDurationMs: 410,
    transport: 'worker', channelReused: true});
  store.finish({status: 'stopped', exitCode: 0});

  const catalog = store.list();
  assert.equal(catalog.schema_version, 'desktop_agent_run_catalog/v1');
  assert.equal(catalog.runs.length, 1);
  assert.deepEqual(catalog.runs[0].bottlenecks.map(item => [item.kind, item.count, item.duration_ms]), [
    ['platform_rpc', 1, 420], ['remote_exec', 1, 80], ['workspace_read', 1, 30],
  ]);
  assert.equal(catalog.runs[0].task_count, 1);
  assert.equal(catalog.runs[0].operation_count, 3, 'running and completed events update one logical operation');
  assert.ok(Number.isFinite(catalog.runs[0].first_output_latency_ms));

  const restored = new DesktopAgentRunStore(root).get({sessionId: 'session-1'});
  assert.equal(restored.status, 'stopped');
  assert.equal(restored.tasks[0].prompt_preview, 'analyse the selected trace');
  assert.equal(restored.tasks[0].output_bytes,
    Buffer.byteLength('\r\n\u001b[2K') + Buffer.byteLength('reading trace\n'));
  assert.equal(restored.tasks[0].output_preview, 'reading trace');
  assert.equal(restored.operations[0].state, 'completed');
  assert.equal(restored.operations[0].channel_reused, true);
  assert.equal(restored.operations[2].transport, 'worker');
  assert.equal(restored.operations[2].queue_wait_ms, 4);
  assert.equal(restored.operations[2].transport_duration_ms, 18);
  assert.equal(restored.operations[2].channel_acquire_duration_ms, 2);
  assert.equal(restored.operations[2].channel_startup_duration_ms, 410);
  assert.equal(restored.operations[1].duration_scope, 'remote_execution');
  assert.equal(restored.operations[1].transport_duration_ms, 96);
  assert.equal(restored.operations[2].duration_scope, 'broker_wall');
  assert.equal(catalog.runs[0].bottlenecks.find(item => item.kind === 'remote_exec').transport_duration_ms, 96);
  assert.equal(catalog.runs[0].bottlenecks.find(item => item.kind === 'workspace_read').channel_acquire_duration_ms, 2);
  assert.equal(catalog.runs[0].bottlenecks.find(item => item.kind === 'workspace_read').max_channel_startup_duration_ms, 410);
  assert.ok(restored.operations[0].started_at);
  assert.ok(restored.finished_at);
  assert.equal(restored.tasks[0].status, 'interrupted');
  assert.ok(restored.tasks[0].completed_at);
});

test('starting a replacement Agent session closes the previous run and validates lookup ids', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-agent-run-restart-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const store = new DesktopAgentRunStore(root);

  store.start({sessionId: 'session-a', executable: 'agent-a', cwd: '/workspace/a'});
  store.start({sessionId: 'session-b', executable: 'agent-b', cwd: '/workspace/b'});
  store.finish({status: 'error', exitCode: 7, signal: 'TERM'});

  assert.equal(store.get({sessionId: 'session-a'}).status, 'restarted');
  const second = store.get({sessionId: 'session-b'});
  assert.equal(second.status, 'error');
  assert.equal(second.exit_code, 7);
  assert.equal(second.signal, 'TERM');
  assert.deepEqual(store.list({limit: 1}).runs.map(run => run.session_id), ['session-b']);
  assert.throws(() => store.get({sessionId: '../escape'}), /Agent 会话 ID 无效/);
  assert.throws(() => store.get({sessionId: 'missing'}), /Agent 历史会话不存在/);
});

test('capture plan completion is correlated to the matching Agent task request', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-agent-run-capture-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const store = new DesktopAgentRunStore(root);
  store.start({sessionId: 'session-capture', executable: 'code-agent', cwd: '/workspace'});
  store.submit('older task', {task_kind: 'other'});
  store.submit('refine capture plan', {task_kind: 'capture_plan_refinement', capture_plan_request_id: 'capture_plan_req_1'});
  store.operation({type: 'operation', id: 'capture-op', target: 'capture', kind: 'capture_tool', origin: 'agent',
    method: 'capture_update_plan', requestId: 'capture_plan_req_1', state: 'running'});
  store.operation({type: 'operation', id: 'capture-op', target: 'capture', kind: 'capture_tool', origin: 'agent',
    method: 'capture_update_plan', requestId: 'capture_plan_req_1', state: 'completed', durationMs: 93});
  store.finish({status: 'stopped'});
  const run = store.get({sessionId: 'session-capture'});
  assert.equal(run.tasks[0].status, 'interrupted');
  assert.equal(run.tasks[1].status, 'completed');
  assert.ok(run.tasks[1].completed_at);
  assert.ok(Number.isFinite(run.tasks[1].completion_latency_ms));
  assert.ok(run.tasks[1].first_tool_at);
  assert.ok(Number.isFinite(run.tasks[1].first_tool_latency_ms));
  assert.equal(run.operations[0].request_id, 'capture_plan_req_1');
});

test('automatic recovery context and explicit completion remain auditable', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-agent-run-recovery-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const store = new DesktopAgentRunStore(root);
  store.start({sessionId: 'session-recovery', executable: 'code-agent', cwd: '/workspace'});
  store.submit('repair selected capture failure', {
    task_kind: 'workbench_error_recovery', repair_request_id: 'repair-audit-1',
    recovery_operation_key: 'capture-start', recovery_authorization_scopes: ['device'],
    operation_attempt_id: 'attempt-audit-1', selected_device: 'PHONE-A',
    page: 'capture', page_label: '负载采样', operation: '在设备执行采样', error_code: 'CAPTURE_FAILED',
  });
  store.operation({type: 'operation', id: 'recovery-invalid-1', target: 'automation',
    kind: 'agent_recovery', origin: 'agent', method: 'desktop_recovery_complete',
    requestId: 'repair-audit-1', state: 'failed', message: 'request id mismatch'});
  store.operation({type: 'operation', id: 'recovery-complete-1', target: 'automation',
    kind: 'agent_recovery', origin: 'agent', method: 'desktop_recovery_complete',
    requestId: 'repair-audit-1', state: 'completed', durationMs: 3});
  store.finish({status: 'stopped'});
  const run = store.get({sessionId: 'session-recovery'});
  assert.deepEqual(run.tasks[0].task_context, {
    task_kind: 'workbench_error_recovery', repair_request_id: 'repair-audit-1',
    recovery_operation_key: 'capture-start', recovery_authorization_scopes: ['device'],
    operation_attempt_id: 'attempt-audit-1', selected_device: 'PHONE-A',
    page: 'capture', page_label: '负载采样', operation: '在设备执行采样', error_code: 'CAPTURE_FAILED',
  });
  assert.equal(run.tasks[0].status, 'completed');
  assert.equal(run.operations[0].request_id, 'repair-audit-1');
});

test('Agent run previews redact credentials and private keys while preserving useful context', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-agent-run-redaction-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const store = new DesktopAgentRunStore(root);

  store.start({sessionId: 'session-redacted', executable: 'code-agent', cwd: '/workspace'});
  store.submit([
    'retry repository inspection',
    'Authorization: Bearer prompt-bearer-value',
    'password="prompt password" token=prompt-token client_secret: prompt-secret',
    "OPENAI_API_KEY='sk-prompt-value' --api-key cli-prompt-value",
    '-----BEGIN PRIVATE KEY-----',
    'prompt-private-key-material',
    '-----END PRIVATE KEY-----',
    'keep diagnostic marker FLOW_STAGE_17',
  ].join('\n'));

  // Exercise chunk boundaries and an unfinished PEM block. operation() forces
  // an intermediate persist before the closing marker arrives.
  store.agentOutput('connecting with Authorization: Bearer output-');
  store.agentOutput('bearer-value\npassword=output-password\n');
  store.agentOutput('-----BEGIN OPENSSH PRIVATE KEY-----\noutput-private-key-material\n');
  store.operation({type: 'operation', id: 'persist-redaction', target: 'ssh', kind: 'remote_exec',
    origin: 'agent', state: 'running', message: 'retry failed: api_key=operation-secret'});
  let persisted = store.get({sessionId: 'session-redacted'});
  assert.doesNotMatch(persisted.tasks[0].output_preview, /output-private-key-material/);

  store.agentOutput('-----END OPENSSH PRIVATE KEY-----\nfinished FLOW_STAGE_17');
  store.finish({status: 'stopped'});
  persisted = store.get({sessionId: 'session-redacted'});

  const prompt = persisted.tasks[0].prompt_preview;
  const output = persisted.tasks[0].output_preview;
  for (const secret of [
    'prompt-bearer-value', 'prompt password', 'prompt-token', 'prompt-secret', 'sk-prompt-value',
    'cli-prompt-value', 'prompt-private-key-material', 'output-bearer-value', 'output-password',
    'output-private-key-material', 'operation-secret',
  ]) {
    assert.doesNotMatch(`${prompt}\n${output}`, new RegExp(secret.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  }
  assert.match(prompt, /Authorization: Bearer \[REDACTED\]/);
  assert.match(prompt, /password="\[REDACTED\]"/);
  assert.match(prompt, /OPENAI_API_KEY='\[REDACTED\]'/);
  assert.match(prompt, /\[REDACTED PRIVATE KEY\]/);
  assert.match(output, /Authorization: Bearer \[REDACTED\]/);
  assert.match(output, /password=\[REDACTED\]/);
  assert.match(output, /\[REDACTED OPENSSH PRIVATE KEY\]/);
  assert.match(persisted.operations[0].message, /api_key=\[REDACTED\]/);
  assert.match(prompt, /retry repository inspection/);
  assert.match(output, /finished FLOW_STAGE_17/);
});

test('live Agent telemetry is readable immediately and persisted off the tool critical path', async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-agent-run-async-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const store = new DesktopAgentRunStore(root);
  const originalWrite = fs.writeFileSync;
  let synchronousWrites = 0;
  fs.writeFileSync = (...args) => { synchronousWrites += 1; return originalWrite(...args); };
  try {
    store.start({sessionId: 'session-live', executable: 'agent', cwd: '/workspace'});
    store.submit('inspect one file');
    store.operation({type: 'operation', id: 'read-1', target: 'ssh', kind: 'workspace_rpc',
      origin: 'agent', method: 'fs.readText', path: 'README.md', state: 'completed', durationMs: 12});
    assert.equal(synchronousWrites, 0, 'live telemetry must not synchronously write on the broker path');
    assert.equal(store.get({sessionId: 'session-live'}).operations[0].method, 'fs.readText');
    assert.equal(store.list().runs[0].operation_count, 1);
    await new Promise(resolve => setTimeout(resolve, 350));
    await store.persistChain;
    const durable = JSON.parse(fs.readFileSync(store.filename('session-live'), 'utf8'));
    assert.equal(durable.operations[0].path, 'README.md');
  } finally {
    fs.writeFileSync = originalWrite;
    store.finish({status: 'stopped'});
  }
});

test('a delayed asynchronous Agent snapshot cannot rename over the synchronous final snapshot', async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-agent-run-race-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const store = new DesktopAgentRunStore(root);
  const originalWrite = fs.promises.writeFile;
  let releaseWrite;
  let markWriteStarted;
  const writeStarted = new Promise(resolve => { markWriteStarted = resolve; });
  const writeGate = new Promise(resolve => { releaseWrite = resolve; });
  fs.promises.writeFile = async (...args) => {
    markWriteStarted();
    await writeGate;
    return originalWrite(...args);
  };
  try {
    store.start({sessionId: 'session-race', executable: 'agent', cwd: '/workspace'});
    store.submit('first state');
    store.cancelScheduledPersist();
    store.queuePersist();
    await writeStarted;
    store.agentOutput('final visible output');
    // Commit the terminal state while the older async snapshot is held before
    // publication. Releasing it afterwards reproduces the old rename race.
    store.finish({status: 'stopped', exitCode: 0});
    releaseWrite();
    await store.persistChain;
    const durable = JSON.parse(fs.readFileSync(store.filename('session-race'), 'utf8'));
    assert.equal(durable.status, 'stopped');
    assert.equal(durable.exit_code, 0);
    assert.match(durable.tasks[0].output_preview, /final visible output/);
    assert.equal(fs.readdirSync(path.join(root, 'agent-runs')).some(name => name.includes('.tmp-')), false);
  } finally {
    releaseWrite();
    fs.promises.writeFile = originalWrite;
  }
});

test('slow Agent telemetry persistence coalesces a burst to one latest pending snapshot', async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-agent-run-coalesce-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const store = new DesktopAgentRunStore(root);
  const originalWrite = fs.promises.writeFile;
  let releaseFirst;
  let markFirstStarted;
  const firstStarted = new Promise(resolve => { markFirstStarted = resolve; });
  const firstGate = new Promise(resolve => { releaseFirst = resolve; });
  let writes = 0;
  fs.promises.writeFile = async (...args) => {
    writes += 1;
    if (writes === 1) {
      markFirstStarted();
      await firstGate;
    }
    return originalWrite(...args);
  };
  try {
    store.start({sessionId: 'session-coalesce', executable: 'agent', cwd: '/workspace'});
    store.cancelScheduledPersist();
    store.queuePersist();
    await firstStarted;
    for (let index = 1; index <= 40; index += 1) {
      store.operation({type: 'operation', id: `read-${index}`, target: 'ssh', kind: 'workspace_rpc',
        origin: 'agent', method: 'fs.readText', path: `file-${index}.txt`, state: 'completed', durationMs: index});
      store.cancelScheduledPersist();
      store.queuePersist();
    }
    assert.equal(store.pendingPersistSnapshot.value.operations.length, 40,
      'the pending slot must retain the newest complete state');
    releaseFirst();
    await store.persistChain;
    assert.equal(writes, 2, 'one in-flight write plus one latest snapshot is sufficient for the burst');
    const durable = JSON.parse(fs.readFileSync(store.filename('session-coalesce'), 'utf8'));
    assert.equal(durable.operations.length, 40);
    assert.equal(durable.operations.at(-1).path, 'file-40.txt');
  } finally {
    releaseFirst();
    fs.promises.writeFile = originalWrite;
    store.finish({status: 'stopped'});
  }
});
