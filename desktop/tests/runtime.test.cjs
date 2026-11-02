'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const http = require('node:http');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const {Server} = require('ssh2');
const {DesktopRuntime, MAX_AGENT_REMOTE_TIMEOUT_MS, MAX_PLATFORM_REMOTE_TIMEOUT_MS, normalizeAgent,
  normalizeAgentTaskContext, createAgentLaunch, remoteExecutionTimeout, shellQuote} = require('../runtime/index.cjs');

const hostKey = crypto.generateKeyPairSync('rsa', {modulusLength: 2048}).privateKey.export({type: 'pkcs1', format: 'pem'});
const posixFixture = {skip: process.platform === 'win32' ? 'This SSH test server executes POSIX commands; Windows native PTY is tested separately below.' : false};
async function fixture(t, runtimeOptions = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-desktop-runtime-'));
  const workspace = path.join(directory, "workspace with quote ' and spaces"); fs.mkdirSync(workspace);
  fs.writeFileSync(path.join(workspace, 'server-evidence.txt'), 'live SSH test\n');
  const clients = new Set(); const processes = new Set();
  const evidence = {connections: 0, authentications: 0, commands: [], terminalInput: '', shellChannels: [], shellSessions: []};
  const server = new Server({hostKeys: [hostKey]}, client => {
    evidence.connections++; clients.add(client);
    client.on('error', () => {}); client.on('close', () => clients.delete(client));
    client.on('authentication', context => { evidence.authentications++; if (context.method === 'password' && context.username === 'tester' && context.password === 'fixture-secret') context.accept(); else context.reject(['password']); });
    client.on('ready', () => client.on('session', accept => {
      const session = accept(); session.on('pty', accept => accept());
      const shellEvidence = {input: '', windowChanges: []};
      session.on('window-change', (accept, _reject, info) => { shellEvidence.windowChanges.push(info); accept?.(); });
      session.on('shell', accept => { const stream = accept(); evidence.shellChannels.push(stream); evidence.shellSessions.push(shellEvidence); stream.write('fixture-terminal-ready\r\n'); stream.on('data', data => { const value = data.toString(); evidence.terminalInput += value; shellEvidence.input += value; stream.write(`echo:${data}`); }); stream.on('error', () => {}); });
      session.on('exec', (accept, reject, info) => {
        evidence.commands.push(info.command);
        const channel = accept();
        const child = spawn('/bin/sh', ['-c', info.command], {stdio: ['pipe', 'pipe', 'pipe']}); processes.add(child);
        child.stdin.on('error', () => {});
        channel.on('data', data => { if (!child.stdin.destroyed && !child.stdin.writableEnded) try { child.stdin.write(data); } catch {} });
        channel.on('end', () => { if (!child.stdin.destroyed && !child.stdin.writableEnded) child.stdin.end(); });
        child.stdout.on('data', data => { if (!channel.destroyed) channel.write(data); }); child.stderr.on('data', data => { if (!channel.destroyed) channel.stderr.write(data); });
        child.on('close', code => { processes.delete(child); if (!channel.destroyed) { channel.exit(code ?? 1); channel.end(); } });
        channel.on('close', () => child.kill()); channel.on('error', () => child.kill());
        session.on('signal', () => child.kill());
      });
    }));
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const runtime = new DesktopRuntime({userDataDir: path.join(directory, 'prefs'), resourcesDir: path.resolve(__dirname, '..'), version: 'test', ...runtimeOptions});
  await runtime.getState();
  const events = []; runtime.on('event', event => events.push(event));
  t.after(async () => { await runtime.dispose(); for (const child of processes) child.kill(); for (const client of clients) client.end(); await new Promise(resolve => server.close(resolve)); fs.rmSync(directory, {recursive: true, force: true}); });
  const config = {host: '127.0.0.1', port: server.address().port, username: 'tester', password: 'fixture-secret', auth: 'password', cwd: workspace};
  return {directory, workspace, runtime, server, config, evidence, events};
}
function collectChild(child) {
  return new Promise((resolve, reject) => { let stdout = '', stderr = ''; child.stdout.on('data', data => stdout += data); child.stderr.on('data', data => stderr += data); child.on('error', reject); child.on('close', code => resolve({stdout, stderr, code})); });
}
async function until(predicate, timeout = 6000) { const started = Date.now(); while (!predicate()) { if (Date.now() - started > timeout) throw new Error('condition timed out'); await new Promise(resolve => setTimeout(resolve, 20)); } }
async function brokerCall(runtime, token, method, params = {}) {
  const {brokerUrl} = await runtime.getIntegration();
  return new Promise((resolve, reject) => {
    const req = http.request(brokerUrl, {method: 'POST', headers: {Authorization: `Bearer ${token}`, 'Content-Type': 'application/json'}}, res => {
      let body = ''; res.on('data', chunk => body += chunk); res.on('end', () => resolve({status: res.statusCode, ...JSON.parse(body)}));
    }); req.on('error', reject); req.end(JSON.stringify({method, params}));
  });
}
function writePlatformStreamWorker(workspace, filename = 'platform-stream-fixture.py') {
  const worker = path.join(workspace, filename);
  fs.writeFileSync(worker, [
    'import base64, json, sys, time',
    'print("HETERO_DESKTOP_RPC_STREAM_READY\\tdesktop_platform_rpc_stream/v1", flush=True)',
    'for line in sys.stdin:',
    '    frame = json.loads(line)',
    '    request = json.loads(base64.b64decode(frame["request_base64"]))',
    '    if request["path"].startswith("/slow") or request.get("body",{}).get("input",{}).get("test_delay"): time.sleep(3)',
    '    response = {"schema_version":"desktop_platform_rpc/v2","ok":True,"result":{"kind":"json","value":{"path":request["path"]}}}',
    '    payload = base64.b64encode(json.dumps(response, separators=(",", ":")).encode()).decode()',
    '    print("HETERO_DESKTOP_RPC_STREAM_RESPONSE\\t" + frame["request_id"] + "\\tHETERO_DESKTOP_RPC\\t" + payload, flush=True)',
  ].join('\n'));
  return {worker, startupCommand: `exec python3 ${shellQuote(worker)}`};
}

test('platform analysis may outlive 120 seconds while Agent remote tools retain their bound', () => {
  assert.equal(remoteExecutionTimeout(600000, false), 600000);
  assert.equal(remoteExecutionTimeout(Number.MAX_SAFE_INTEGER, false), MAX_PLATFORM_REMOTE_TIMEOUT_MS);
  assert.equal(remoteExecutionTimeout(600000, true), MAX_AGENT_REMOTE_TIMEOUT_MS);
  assert.equal(remoteExecutionTimeout(undefined, false), 30000);
});

test('Agent task context retains typed recovery identity and drops untrusted extra fields', () => {
  const context = normalizeAgentTaskContext({
    task_kind: 'workbench_error_recovery', repair_request_id: 'repair-1',
    page: 'capture', page_label: '负载采样', operation: '在设备执行采样',
    error_code: 'CAPTURE_FAILED', recovery_operation_key: 'capture-start',
    recovery_authorization_scopes: ['device'], operation_attempt_id: 'attempt-1',
    selected_device: 'PHONE-A', untrusted_instruction: 'keep me',
  });
  assert.deepEqual(context, {
    task_kind: 'workbench_error_recovery', repair_request_id: 'repair-1',
    page: 'capture', page_label: '负载采样', operation: '在设备执行采样',
    error_code: 'CAPTURE_FAILED', recovery_operation_key: 'capture-start',
    recovery_authorization_scopes: ['device'], operation_attempt_id: 'attempt-1',
    selected_device: 'PHONE-A',
  });
});

test('real SSH terminal and command channels share one authenticated connection and preserve cwd', posixFixture, async t => {
  const f = await fixture(t); await f.runtime.connectSsh(f.config);
  const initialTerminals = await f.runtime.listSshTerminals();
  assert.equal(initialTerminals.terminals.length, 1);
  assert.equal(initialTerminals.terminals[0].terminalId, initialTerminals.primaryTerminalId);
  const connectedState = (await f.runtime.getState()).ssh;
  assert.equal(connectedState.terminalCount, 1);
  assert.equal(connectedState.auth, 'password');
  assert.equal(connectedState.privateKeyPath, '');
  const result = await f.runtime.execRemote({command: 'pwd && cat server-evidence.txt && uname -s'});
  assert.equal(result.exitCode, 0); assert.match(result.stdout, /live SSH test/);
  assert.ok(result.stdout.startsWith(connectedState.remoteRootPath));
  assert.equal(result.transport, 'workspace-worker');
  const platformOperations = f.events.filter(event => event.type === 'operation' && event.command === 'pwd && cat server-evidence.txt && uname -s');
  assert.deepEqual(platformOperations.map(event => [event.state, event.kind, event.origin]), [
    ['running', 'remote_exec', 'platform'], ['completed', 'remote_exec', 'platform'],
  ]);
  f.runtime.terminalInput({target: 'ssh', data: 'interactive-check\r'});
  await until(() => f.evidence.terminalInput.includes('interactive-check'));
  assert.ok(f.evidence.terminalInput.includes(`cd -- ${shellQuote(f.workspace)} && exec bash\r`));
  assert.equal(f.evidence.connections, 1); assert.equal(f.evidence.authentications, 2); // SSH first attempts none, then password.
  assert.ok(f.events.some(event => event.type === 'terminal' && event.target === 'ssh' && event.terminalId === initialTerminals.primaryTerminalId && event.data.includes('fixture-terminal-ready')));
  const settings = fs.readFileSync(path.join(f.directory, 'prefs', 'settings.json'), 'utf8');
  assert.ok(!settings.includes('fixture-secret')); assert.ok(!JSON.stringify(await f.runtime.getState()).includes('fixture-secret'));
  assert.equal(Object.keys(JSON.parse(fs.readFileSync(path.join(f.directory, 'prefs', 'known-hosts.json')))).length, 1);
});

test('worker startup failure falls back to direct exec and a corrected profile retries immediately', posixFixture, async t => {
  const f = await fixture(t); await f.runtime.connectSsh(f.config);
  const target = f.runtime.currentSshTarget();
  await f.runtime.saveSettings({
    serverProfile: {pythonBin: '/definitely/missing/hetero-python'},
    serverProfileTarget: target,
  });
  const executed = await f.runtime.execRemote({command: 'printf direct-fallback'});
  assert.equal(executed.stdout, 'direct-fallback');
  assert.equal(executed.transport, 'ssh-exec-fallback');
  assert.equal(f.evidence.connections, 1, 'fallbacks keep using the authenticated SSH client');
  assert.equal(f.evidence.commands.filter(value => value.includes('/definitely/missing/hetero-python')).length, 1,
    'the platform lane records one bounded startup failure');
  await f.runtime.saveSettings({serverProfile: {pythonBin: 'python3'}, serverProfileTarget: target});
  const recovered = await f.runtime.execRemote({command: 'printf worker-recovered'});
  assert.equal(recovered.transport, 'workspace-worker', 'saving the corrected profile clears stale startup failures');
  assert.equal(recovered.stdout, 'worker-recovered');
});

test('an obsolete opening worker cannot restore cooldown after its Python profile is corrected', posixFixture, async t => {
  const f = await fixture(t); await f.runtime.connectSsh(f.config);
  const target = f.runtime.currentSshTarget();
  const delayedFailure = path.join(f.directory, 'delayed-python-failure.sh');
  fs.writeFileSync(delayedFailure, '#!/bin/sh\nsleep 0.5\nexit 1\n', {mode: 0o700});
  await f.runtime.saveSettings({serverProfile: {pythonBin: delayedFailure}, serverProfileTarget: target});
  const obsolete = f.runtime.execRemote({command: 'printf obsolete-fallback'});
  await until(() => f.runtime.remoteWorkspaceConnections.has('platform'));
  await f.runtime.saveSettings({serverProfile: {pythonBin: 'python3'}, serverProfileTarget: target});
  const recovered = await f.runtime.execRemote({command: 'printf corrected-worker'});
  assert.equal(recovered.transport, 'workspace-worker');
  assert.equal(recovered.stdout, 'corrected-worker');
  await obsolete;
  await new Promise(resolve => setTimeout(resolve, 50));
  const stillHealthy = await f.runtime.execRemote({command: 'printf still-healthy'});
  assert.equal(stillHealthy.transport, 'workspace-worker');
  assert.equal(stillHealthy.stdout, 'still-healthy');
});

test('disconnect reaps persistent workspace workers instead of leaving remote processes', posixFixture, async t => {
  const f = await fixture(t); await f.runtime.connectSsh(f.config);
  const record = await f.runtime.acquireRemoteWorkspaceConnection({role: 'interactive'});
  const pid = (await record.request({method: 'ping', internal: true})).result.pid;
  assert.doesNotThrow(() => process.kill(pid, 0));
  await f.runtime.disconnectSsh();
  await until(() => {
    try { process.kill(pid, 0); return false; }
    catch (error) { return error.code === 'ESRCH'; }
  }, 3000);
});

test('slow prefetch lane work cannot head-of-line block interactive file reads', posixFixture, async t => {
  const f = await fixture(t); await f.runtime.connectSsh(f.config);
  const marker = path.join(f.workspace, 'prefetch-lane-busy');
  const slow = f.runtime.workspaceRequest({role: 'prefetch', method: 'proc.exec', params: {
    command: `printf 1 > ${shellQuote(marker)}; sleep 0.4`, timeoutMs: 2000,
  }, timeoutMs: 3000});
  await until(() => fs.existsSync(marker));
  const started = Date.now();
  const opened = await f.runtime.readRemoteFile({path: 'server-evidence.txt'});
  assert.equal(opened.content, 'live SSH test\n');
  assert.ok(Date.now() - started < 250, 'interactive read uses a separate physical lane');
  await slow;
  assert.equal(f.evidence.commands.filter(value => value.includes('workspace-file-worker.py')).length, 2);
});

test('one authenticated SSH client hosts independent stable interactive terminal sessions', posixFixture, async t => {
  const f = await fixture(t); await f.runtime.connectSsh(f.config);
  const initial = await f.runtime.listSshTerminals();
  const firstId = initial.primaryTerminalId;
  const created = await f.runtime.createSshTerminal({cols: 132, rows: 41});
  const secondId = created.terminal.terminalId;
  assert.notEqual(secondId, firstId);
  assert.deepEqual((await f.runtime.listSshTerminals()).terminals.map(item => item.terminalId), [firstId, secondId]);
  assert.equal(f.evidence.connections, 1, 'new shells reuse the authenticated ssh2 Client');

  f.runtime.inputSshTerminal({terminalId: firstId, data: 'first-only\r'});
  f.runtime.inputSshTerminal({terminalId: secondId, data: 'second-only\r'});
  await until(() => f.evidence.shellSessions.length === 2
    && f.evidence.shellSessions[0].input.includes('first-only')
    && f.evidence.shellSessions[1].input.includes('second-only'));
  assert.ok(!f.evidence.shellSessions[0].input.includes('second-only'));
  assert.ok(!f.evidence.shellSessions[1].input.includes('first-only'));

  assert.deepEqual(f.runtime.resizeSshTerminal({terminalId: secondId, cols: 144, rows: 45}), {ok: true, terminalId: secondId, cols: 144, rows: 45});
  await until(() => f.evidence.shellSessions[1].windowChanges.some(value => value.cols === 144 && value.rows === 45));
  const stable = await f.runtime.listSshTerminals();
  assert.equal(stable.terminals.find(item => item.terminalId === secondId).cols, 144);

  const closed = await f.runtime.closeSshTerminal({terminalId: firstId});
  assert.equal(closed.primaryTerminalId, secondId, 'closing the primary promotes an existing shell without changing its id');
  await assert.rejects(async () => f.runtime.inputSshTerminal({terminalId: firstId, data: 'closed'}), /不存在或已关闭/);
  f.runtime.terminalInput({target: 'ssh', data: 'legacy-primary\r'});
  await until(() => f.evidence.shellSessions[1].input.includes('legacy-primary'));

  const command = await f.runtime.execRemote({command: 'printf independent-exec'});
  assert.equal(command.stdout, 'independent-exec');
  assert.equal(command.transport, 'workspace-worker');
  assert.ok(!f.evidence.shellSessions[1].input.includes('independent-exec'), 'remote_exec remains an independent exec channel');
  assert.equal((await f.runtime.getState()).ssh.state, 'connected');
});

test('SSH terminal sessions enforce the limit and disconnect closes every terminal', posixFixture, async t => {
  const f = await fixture(t); await f.runtime.connectSsh(f.config);
  for (let index = 1; index < 8; index++) await f.runtime.createSshTerminal();
  const before = await f.runtime.listSshTerminals();
  assert.equal(before.terminals.length, 8);
  await assert.rejects(f.runtime.createSshTerminal(), /最多可同时打开 8 个/);
  await f.runtime.disconnectSsh();
  assert.deepEqual(await f.runtime.listSshTerminals(), {terminals: [], terminalCount: 0, primaryTerminalId: null, shellState: 'closed'});
  const closedIds = new Set(f.events.filter(event => event.type === 'ssh-terminal' && event.action === 'closed' && event.reason === 'disconnected').map(event => event.terminalId));
  assert.deepEqual(closedIds, new Set(before.terminals.map(item => item.terminalId)));
  assert.ok(f.events.filter(event => event.type === 'terminal' && event.target === 'ssh').every(event => event.terminalId));
});

test('nonzero exit and stderr are preserved; timeout ends channel without disconnecting SSH', posixFixture, async t => {
  const f = await fixture(t); await f.runtime.connectSsh(f.config);
  const failed = await f.runtime.execRemote({command: 'printf failure >&2; exit 7'});
  assert.equal(failed.exitCode, 7); assert.equal(failed.stderr, 'failure');
  await assert.rejects(f.runtime.execRemote({command: 'sleep 3', timeoutMs: 100}), /超过 100 ms/);
  for (const event of f.events.filter(value => value.type === 'operation' && ['printf failure >&2; exit 7', 'sleep 3'].includes(value.command))) {
    assert.equal(event.kind, 'remote_exec');
    assert.equal(event.origin, 'platform');
  }
  assert.equal(f.events.find(value => value.type === 'operation' && value.command === 'printf failure >&2; exit 7' && value.state === 'failed')?.kind, 'remote_exec');
  assert.equal(f.events.find(value => value.type === 'operation' && value.command === 'sleep 3' && value.state === 'error')?.origin, 'platform');
  const next = await f.runtime.execRemote({command: 'printf alive'}); assert.equal(next.stdout, 'alive');
  assert.equal(f.evidence.connections, 1);
});

test('output limit and disconnect cancel pending remote jobs', posixFixture, async t => {
  const f = await fixture(t); await f.runtime.connectSsh(f.config);
  await assert.rejects(f.runtime.execRemote({command: 'yes evidence | head -c 1200000'}), /超过 1 MiB/);
  const pending = f.runtime.execRemote({command: 'sleep 3'});
  const rejection = assert.rejects(pending, /已断开/); await new Promise(resolve => setTimeout(resolve, 50)); await f.runtime.disconnectSsh(); await rejection;
});

test('renderer platform requests are target-fenced; cancellation retires and rebuilds the worker', posixFixture, async t => {
  const f = await fixture(t); await f.runtime.connectSsh(f.config);
  const expectedTarget = (await f.runtime.getState()).ssh;
  const {startupCommand} = writePlatformStreamWorker(f.workspace, 'platform-cancel-fixture.py');
  const request = pathValue => ({schema_version: 'desktop_platform_rpc/v2', method: 'GET', path: pathValue, body: {}});
  await assert.rejects(
    f.runtime.platformRequest({startupCommand, requestId: 'wrong-target', request: request('/wrong-target'),
      expectedTarget: {...expectedTarget, cwd: '/another-workspace'}}),
    /SSH 目标已改变/,
  );
  const pending = f.runtime.platformRequest({startupCommand, requestId: 'page-request-1',
    request: request('/slow/cancel'), expectedTarget});
  const rejection = assert.rejects(pending, /服务器分析请求已取消/);
  await until(() => [...f.runtime.platformRpcConnections.values()]
    .some(record => record.pending.has('page-request-1')));
  assert.deepEqual(await f.runtime.cancelPlatformRequest({requestId: 'page-request-1', expectedTarget}),
    {requestId: 'page-request-1', cancelled: true});
  await rejection;
  assert.deepEqual(await f.runtime.cancelPlatformRequest({requestId: 'page-request-1', expectedTarget}),
    {requestId: 'page-request-1', cancelled: false});
  const rebuilt = await f.runtime.platformRequest({startupCommand, requestId: 'page-request-2',
    request: request('/alive-after-cancel'), expectedTarget});
  assert.equal(rebuilt.channelReused, false, 'cancellation closes the synchronous worker before the next request');
  assert.equal(f.evidence.commands.filter(value => value.includes('platform-cancel-fixture.py')).length, 2,
    'the request after cancellation starts exactly one replacement worker');
});

test('cancelling while the persistent worker is opening prevents the request from being sent', posixFixture, async t => {
  const f = await fixture(t); await f.runtime.connectSsh(f.config);
  const expectedTarget = (await f.runtime.getState()).ssh;
  const marker = path.join(f.workspace, 'request-was-sent');
  const worker = path.join(f.workspace, 'platform-delayed-ready.py');
  fs.writeFileSync(worker, [
    'import base64, json, pathlib, sys, time',
    'time.sleep(1)',
    'print("HETERO_DESKTOP_RPC_STREAM_READY\\tdesktop_platform_rpc_stream/v1", flush=True)',
    'for line in sys.stdin:',
    `    pathlib.Path(${JSON.stringify(marker)}).write_text("sent", encoding="utf-8")`,
    '    frame=json.loads(line)',
    '    response={"schema_version":"desktop_platform_rpc/v2","ok":True,"result":{"kind":"json","value":{}}}',
    '    payload=base64.b64encode(json.dumps(response,separators=(",", ":")).encode()).decode()',
    '    print("HETERO_DESKTOP_RPC_STREAM_RESPONSE\\t"+frame["request_id"]+"\\tHETERO_DESKTOP_RPC\\t"+payload,flush=True)',
  ].join('\n'));
  const pending = f.runtime.platformRequest({startupCommand: `exec python3 ${shellQuote(worker)}`,
    requestId: 'cancel-before-ready', expectedTarget,
    request: {schema_version: 'desktop_platform_rpc/v2', method: 'POST', path: '/mutating', body: {}}});
  const rejection = assert.rejects(pending, /已取消/);
  await until(() => f.runtime.pendingPlatformRequests.has('cancel-before-ready'));
  assert.deepEqual(await f.runtime.cancelPlatformRequest({requestId: 'cancel-before-ready', expectedTarget}),
    {requestId: 'cancel-before-ready', cancelled: true});
  await rejection;
  await new Promise(resolve => setTimeout(resolve, 1100));
  assert.equal(fs.existsSync(marker), false, 'a cancelled opening request never reaches the Python dispatcher');
});

test('platform requests reuse one framed SSH exec worker and preserve request identity', posixFixture, async t => {
  const f = await fixture(t); await f.runtime.connectSsh(f.config);
  const {startupCommand} = writePlatformStreamWorker(f.workspace);
  const expectedTarget = (await f.runtime.getState()).ssh;
  const first = await f.runtime.platformRequest({startupCommand, expectedTarget, requestId: 'rpc-1',
    request: {schema_version: 'desktop_platform_rpc/v2', method: 'GET', path: '/first', body: {}}});
  const second = await f.runtime.platformRequest({startupCommand, expectedTarget, requestId: 'rpc-2',
    request: {schema_version: 'desktop_platform_rpc/v2', method: 'GET', path: '/second', body: {}}});
  assert.equal(first.exitCode, 0); assert.equal(first.channelReused, false);
  assert.equal(second.exitCode, 0); assert.equal(second.channelReused, true);
  assert.match(first.stdout, /^HETERO_DESKTOP_RPC\t/);
  assert.equal(f.evidence.commands.filter(value => value.includes('platform-stream-fixture.py')).length, 1,
    'both requests share one SSH exec channel and one Python worker');
  assert.deepEqual(f.events.filter(event => event.kind === 'platform_rpc').map(event => event.state),
    ['running', 'completed', 'running', 'completed']);
});

test('Desktop Agent application-flow tools use a persistent Agent lane bound to the page context', posixFixture, async t => {
  const f = await fixture(t); await f.runtime.connectSsh(f.config);
  const {startupCommand} = writePlatformStreamWorker(f.workspace, 'platform-agent-flow-fixture.py');
  const expectedTarget = (await f.runtime.getState()).ssh;
  await f.runtime.platformRequest({startupCommand, expectedTarget, requestId: 'page-flow-catalog',
    request: {schema_version: 'desktop_platform_rpc/v2', method: 'GET', path: '/v1/application-flows', body: {}}});
  f.runtime.toolSession.taskContext = {task_kind: 'application_flow_generation',
    application_flow_analysis_id: 'analysis-fixture', source_connector_id: 'connector-fixture'};
  const result = await f.runtime.applicationFlowTool('application_flow_list_assets', {limit: 20});
  await f.runtime.applicationFlowTool('application_flow_list_assets', {limit: 10});
  assert.equal(result.path, '/v1/application-flows/agent-action');
  assert.equal(f.evidence.commands.filter(value => value.includes('platform-agent-flow-fixture.py')).length, 2,
    'page and Agent use isolated persistent lanes so either side can be cancelled safely');
  assert.deepEqual(f.events.filter(event => event.kind === 'platform_rpc' && event.origin === 'agent'
    && event.state === 'completed').map(event => event.channelReused), [false, true]);
});

test('rotating the Agent tool session cancels an in-flight application-flow operation without killing the page lane', posixFixture, async t => {
  const f = await fixture(t); await f.runtime.connectSsh(f.config);
  const {startupCommand} = writePlatformStreamWorker(f.workspace, 'platform-agent-cancel-fixture.py');
  const expectedTarget = (await f.runtime.getState()).ssh;
  await f.runtime.platformRequest({startupCommand, expectedTarget, requestId: 'page-before-agent-cancel',
    request: {schema_version: 'desktop_platform_rpc/v2', method: 'GET', path: '/page-before', body: {}}});
  f.runtime.toolSession.taskContext = {task_kind: 'application_flow_generation',
    application_flow_analysis_id: 'analysis-fixture', source_connector_id: 'connector-fixture'};
  const pending = f.runtime.applicationFlowTool('application_flow_list_assets', {limit: 20, test_delay: true});
  const rejection = assert.rejects(pending, /Agent 工具会话已结束|已取消/);
  await until(() => [...f.runtime.pendingPlatformRequests.values()]
    .some(value => value.toolSessionId === f.runtime.toolSession.id && value.record?.pending.has(value.requestId)));
  f.runtime.rotateToolSession();
  await rejection;
  const pageAfter = await f.runtime.platformRequest({startupCommand, expectedTarget, requestId: 'page-after-agent-cancel',
    request: {schema_version: 'desktop_platform_rpc/v2', method: 'GET', path: '/page-after', body: {}}});
  assert.equal(pageAfter.channelReused, true, 'Agent cancellation does not retire the independent page worker');
});

test('disconnect rejects pending Platform RPC and reconnect creates a fresh reusable worker', posixFixture, async t => {
  const f = await fixture(t); await f.runtime.connectSsh(f.config);
  const {startupCommand} = writePlatformStreamWorker(f.workspace, 'platform-reconnect-fixture.py');
  let expectedTarget = (await f.runtime.getState()).ssh;
  const request = pathValue => ({schema_version: 'desktop_platform_rpc/v2', method: 'GET', path: pathValue, body: {}});
  const beforeDisconnect = await f.runtime.platformRequest({startupCommand, expectedTarget, requestId: 'before-disconnect',
    request: request('/before-disconnect')});
  assert.equal(beforeDisconnect.channelReused, false);
  const pending = f.runtime.platformRequest({startupCommand, expectedTarget, requestId: 'during-disconnect',
    request: request('/slow/disconnect')});
  const rejection = assert.rejects(pending, /SSH 已断开|服务器分析中断/);
  await until(() => [...f.runtime.platformRpcConnections.values()]
    .some(record => record.pending.has('during-disconnect')));
  await f.runtime.disconnectSsh();
  await rejection;
  assert.equal(f.runtime.platformRpcConnections.size, 0);

  await f.runtime.connectSsh(f.config);
  expectedTarget = (await f.runtime.getState()).ssh;
  const afterReconnect = await f.runtime.platformRequest({startupCommand, expectedTarget, requestId: 'after-reconnect-1',
    request: request('/after-reconnect-1')});
  const reusedAfterReconnect = await f.runtime.platformRequest({startupCommand, expectedTarget, requestId: 'after-reconnect-2',
    request: request('/after-reconnect-2')});
  assert.equal(afterReconnect.channelReused, false);
  assert.equal(reusedAfterReconnect.channelReused, true);
  assert.equal(f.evidence.commands.filter(value => value.includes('platform-reconnect-fixture.py')).length, 2,
    'one worker belongs to each authenticated SSH generation');
});

test('typed capture archive transfer hashes locally, writes only reserved managed SFTP target and fences SSH identity', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-capture-transfer-'));
  const runtime = new DesktopRuntime({userDataDir: path.join(directory, 'prefs'), resourcesDir: path.resolve(__dirname, '..'), version: 'test'});
  await runtime.getState();
  t.after(async () => { await runtime.dispose(); fs.rmSync(directory, {recursive: true, force: true}); });
  const archive = path.join(directory, 'capture.capture.zip'); fs.writeFileSync(archive, 'complete capture package');
  runtime.capture.traceArchiveForAnalysis = ({id}) => {
    assert.equal(id, 'trace_fixture');
    return {archivePath: archive, attributes: fs.statSync(archive)};
  };
  const target = {host: 'worker.example', port: 22, username: 'tester', cwd: '/srv/workspace'};
  const scope = runtime.serverProfileScopeId(target);
  const managed = path.join(directory, '.local', 'share', 'heterogeneous-workbench', 'server-profiles', 'targets', scope, 'default', 'state', 'desktop-capture-uploads'); fs.mkdirSync(managed, {recursive: true});
  const token = 'a'.repeat(32); const remotePath = path.join(managed, `${token}.capture.zip`).replaceAll(path.sep, '/');
  const sftp = {
    on() {}, end() {},
    lstat(filename, callback) { fs.lstat(filename, callback); },
    fastPut(source, destination, _options, callback) { fs.copyFile(source, destination, callback); },
    unlink(filename, callback) { fs.unlink(filename, callback); },
  };
  runtime.client = {sftp: callback => callback(null, sftp), end() {}, destroy() {}};
  runtime.ssh = {state: 'connected', ...target};
  const descriptor = await runtime.prepareTraceArchive({traceId: 'trace_fixture'});
  assert.equal(descriptor.archiveSizeBytes, fs.statSync(archive).size);
  assert.match(descriptor.archiveSha256, /^[a-f0-9]{64}$/);
  await assert.rejects(runtime.uploadTraceArchive({traceId: 'trace_fixture', uploadToken: token,
    remotePath: remotePath.replace(`/${scope}/`, '/ffffffffffffffff/'), archiveSha256: descriptor.archiveSha256,
    archiveSizeBytes: descriptor.archiveSizeBytes, expectedTarget: target}), /托管路径无效/);
  await assert.rejects(runtime.uploadTraceArchive({traceId: 'trace_fixture', uploadToken: token, remotePath,
    archiveSha256: descriptor.archiveSha256, archiveSizeBytes: descriptor.archiveSizeBytes,
    expectedTarget: {...target, cwd: '/different'}}), /SSH 目标已改变/);
  const uploaded = await runtime.uploadTraceArchive({traceId: 'trace_fixture', uploadToken: token, remotePath,
    archiveSha256: descriptor.archiveSha256, archiveSizeBytes: descriptor.archiveSizeBytes, expectedTarget: target});
  assert.equal(uploaded.uploaded, true); assert.equal(fs.readFileSync(remotePath, 'utf8'), 'complete capture package');
  assert.deepEqual(await runtime.discardTraceUpload({uploadToken: token, remotePath, expectedTarget: target}), {removed: true, uploadToken: token});
  assert.equal(fs.existsSync(remotePath), false);
});

test('typed calibration measurement transfer validates UTF-8 hash and writes only its reserved profile staging path', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-calibration-transfer-'));
  const runtime = new DesktopRuntime({userDataDir: path.join(directory, 'prefs'), resourcesDir: path.resolve(__dirname, '..'), version: 'test'});
  await runtime.getState();
  t.after(async () => { await runtime.dispose(); fs.rmSync(directory, {recursive: true, force: true}); });
  const target = {host: 'worker.example', port: 22, username: 'tester', cwd: '/srv/workspace'};
  const scope = runtime.serverProfileScopeId(target);
  const managed = path.join(directory, '.local', 'share', 'heterogeneous-workbench', 'server-profiles', 'targets', scope,
    'default', 'state', 'parallel-calibration-measurement-uploads');
  fs.mkdirSync(managed, {recursive: true});
  const token = 'b'.repeat(32);
  const remotePath = path.join(managed, `${token}.measurements.json`).replaceAll(path.sep, '/');
  const text = JSON.stringify({provenance: {device: 'fixture'}, samples: [{cpu_time_ns: 123456}]});
  const contents = Buffer.from(text, 'utf8');
  const digest = crypto.createHash('sha256').update(contents).digest('hex');
  const sftp = {
    on() {}, end() {},
    lstat(filename, callback) { fs.lstat(filename, callback); },
    writeFile(filename, value, _options, callback) { fs.writeFile(filename, value, {flag: 'wx', mode: 0o600}, callback); },
    unlink(filename, callback) { fs.unlink(filename, callback); },
  };
  runtime.client = {sftp: callback => callback(null, sftp), end() {}, destroy() {}};
  runtime.ssh = {state: 'connected', ...target};
  await assert.rejects(runtime.uploadCalibrationMeasurements({uploadToken: token,
    remotePath: remotePath.replace('/parallel-calibration-measurement-uploads/', '/other-uploads/'),
    measurementText: text, measurementSha256: digest, measurementSizeBytes: contents.length, expectedTarget: target}),
  /托管路径无效/);
  await assert.rejects(runtime.uploadCalibrationMeasurements({uploadToken: token, remotePath,
    measurementText: text, measurementSha256: '0'.repeat(64), measurementSizeBytes: contents.length, expectedTarget: target}),
  /大小或 SHA-256 不一致/);
  await assert.rejects(runtime.uploadCalibrationMeasurements({uploadToken: token, remotePath,
    measurementText: text, measurementSha256: digest, measurementSizeBytes: contents.length,
    expectedTarget: {...target, cwd: '/different'}}), /SSH 目标已改变/);
  const uploaded = await runtime.uploadCalibrationMeasurements({uploadToken: token, remotePath,
    measurementText: text, measurementSha256: digest, measurementSizeBytes: contents.length, expectedTarget: target});
  assert.deepEqual(uploaded, {uploaded: true, uploadToken: token,
    measurementSha256: digest, measurementSizeBytes: contents.length});
  assert.equal(fs.readFileSync(remotePath, 'utf8'), text);
  assert.deepEqual(await runtime.discardCalibrationMeasurementUpload({uploadToken: token, remotePath, expectedTarget: target}),
    {removed: true, uploadToken: token});
  assert.equal(fs.existsSync(remotePath), false);
});

test('TOFU remembers first host and rejects changed fingerprint', posixFixture, async t => {
  const f = await fixture(t); await f.runtime.connectSsh(f.config); await f.runtime.disconnectSsh();
  const file = path.join(f.directory, 'prefs', 'known-hosts.json'); const known = JSON.parse(fs.readFileSync(file)); known[`127.0.0.1:${f.config.port}`].sha256 = 'changed'; fs.writeFileSync(file, JSON.stringify(known));
  await assert.rejects(f.runtime.connectSsh(f.config), /指纹发生变化/);
  assert.equal((await f.runtime.getState()).ssh.state, 'error');
});

test('bad credentials never report connected', async t => {
  const f = await fixture(t); await assert.rejects(f.runtime.connectSsh({...f.config, password: 'wrong'}), /authentication/i);
  assert.ok(!f.events.some(event => event.type === 'status' && event.target === 'ssh' && event.state === 'connected'));
});

test('saved SSH password is reused without returning it to renderer state or settings', posixFixture, async t => {
  const passwords = new Map();
  const key = target => `${target.username}@${target.host}:${target.port}`;
  const credentialStore = {
    getSshPasswordStatus: target => ({available: true, saved: passwords.has(key(target))}),
    getSshPassword: target => passwords.get(key(target)) || null,
    setSshPassword: (target, password) => passwords.set(key(target), password),
    deleteSshPassword: target => passwords.delete(key(target)),
  };
  const f = await fixture(t, {credentialStore});
  const connected = await f.runtime.connectSsh(f.config);
  assert.equal(connected.settings.ssh.passwordCredential.saved, true);
  assert.ok(!JSON.stringify(connected).includes('fixture-secret'));
  await f.runtime.disconnectSsh();
  await f.runtime.connectSsh({...f.config, password: ''});
  assert.equal(f.evidence.connections, 2);
  assert.equal(passwords.get(key(f.config)), 'fixture-secret');
  const settings = fs.readFileSync(path.join(f.directory, 'prefs', 'settings.json'), 'utf8');
  assert.ok(!settings.includes('fixture-secret'));
  assert.ok(!settings.includes('passwordCredential'));
  const forgotten = await f.runtime.forgetSshPassword();
  assert.equal(forgotten.settings.ssh.passwordCredential.saved, false);
  assert.equal(passwords.has(key(f.config)), false);
  await assert.rejects(f.runtime.connectSsh({...f.config, password: ''}), /没有已保存的密码/);
});

test('an explicit SSH password can connect and replace a recoverably damaged credential store', posixFixture, async t => {
  let replacement = null;
  const credentialStore = {
    getSshPasswordStatus: () => replacement
      ? {available: true, saved: true}
      : {available: true, saved: false, recoverable: true, error: '旧凭据无法解密'},
    getSshPassword: () => replacement,
    setSshPassword: (_target, password) => { replacement = password; },
    deleteSshPassword: () => false,
  };
  const f = await fixture(t, {credentialStore});
  const connected = await f.runtime.connectSsh(f.config);
  assert.equal(connected.ssh.state, 'connected');
  assert.equal(replacement, 'fixture-secret');
  assert.equal(connected.settings.ssh.passwordCredential.saved, true);
});

test('real CLI helper and MCP tool calls execute against the same remote session', posixFixture, async t => {
  const f = await fixture(t); await f.runtime.connectSsh(f.config);
  const integration = await f.runtime.getIntegration();
  const env = {...process.env, HETERO_DESKTOP_BROKER_URL: integration.brokerUrl, HETERO_DESKTOP_BROKER_TOKEN: f.runtime.brokerToken};
  const cli = await collectChild(spawn(process.execPath, [integration.helperPath, 'remote_exec', 'cat server-evidence.txt'], {env}));
  assert.equal(cli.code, 0, cli.stderr); assert.equal(JSON.parse(cli.stdout).stdout, 'live SSH test\n');
  const mcp = spawn(process.execPath, [integration.helperPath, '--mcp'], {env});
  let stdout = ''; mcp.stdout.on('data', data => stdout += data);
  mcp.stdin.write(`${JSON.stringify({jsonrpc: '2.0', id: 1, method: 'initialize', params: {protocolVersion: '2025-03-26'}})}\n`);
  mcp.stdin.write(`${JSON.stringify({jsonrpc: '2.0', id: 2, method: 'tools/list'})}\n`);
  mcp.stdin.write(`${JSON.stringify({jsonrpc: '2.0', id: 3, method: 'tools/call', params: {name: 'remote_exec', arguments: {command: 'printf mcp-live'}}})}\n`);
  await until(() => stdout.includes('mcp-live')); mcp.stdin.end(); await once(mcp, 'close');
  const messages = stdout.trim().split('\n').map(line => JSON.parse(line));
  assert.equal(messages.find(value => value.id === 1).result.protocolVersion, '2025-03-26');
  const exposedTools = messages.find(value => value.id === 2).result.tools.map(tool => tool.name);
  for (const name of ['remote_exec', 'remote_status', 'workspace_stat', 'workspace_list', 'workspace_read',
    'workspace_search', 'workspace_write', 'capture_catalog', 'capture_save_plan', 'capture_update_plan', 'capture_start', 'trace_list',
    'application_flow_publish_candidates', 'application_flow_compile_mermaid', 'application_flow_inspect_runtime_trace'])
    assert.ok(exposedTools.includes(name));
  assert.equal(messages.find(value => value.id === 3).result.structuredContent.stdout, 'mcp-live');
  const agentOperations = f.events.filter(event => event.type === 'operation' && event.kind === 'remote_exec');
  assert.ok(agentOperations.length >= 4);
  assert.ok(agentOperations.every(event => event.origin === 'agent'));
  for (const id of new Set(agentOperations.map(event => event.id))) {
    const lifecycle = agentOperations.filter(event => event.id === id);
    assert.equal(lifecycle[0].state, 'running');
    assert.equal(lifecycle.at(-1).state, 'completed');
  }
  assert.equal(f.evidence.connections, 1);
  assert.ok(!fs.readFileSync(integration.mcpConfigPath, 'utf8').includes(f.runtime.brokerToken));
});

test('running Agent prewarms its persistent Workspace lane before the first file tool call', posixFixture, async t => {
  const f = await fixture(t);
  await f.runtime.connectSsh(f.config);
  await f.runtime.startAgent({demo: true, cwd: f.workspace, startupDelayMs: 0});
  await until(() => f.runtime.remoteWorkspaceConnections.get('agent')?.state === 'ready');

  const response = await brokerCall(f.runtime, f.runtime.brokerToken, 'workspace_read', {
    path: 'server-evidence.txt',
  });
  assert.equal(response.status, 200);
  assert.equal(response.result.content, 'live SSH test\n');
  assert.equal(response.result.transport, 'workspace-worker');
  assert.equal(response.result.channelReused, false,
    'the warmup ping is internal and the first business request remains identifiable');
  assert.ok(response.result.channelAcquireDurationMs < 100,
    `prewarmed Agent lane should be immediately available, got ${response.result.channelAcquireDurationMs} ms`);
  assert.ok(Number.isFinite(response.result.channelStartupDurationMs));
  assert.ok(Number.isFinite(response.result.transportDurationMs));
  const completed = f.events.find(event => event.kind === 'workspace_rpc'
    && event.method === 'fs.readText' && event.state === 'completed');
  assert.equal(completed.transportDurationMs, response.result.transportDurationMs);
  assert.equal(completed.channelAcquireDurationMs, response.result.channelAcquireDurationMs);
});

test('a real Agent request retries a failed speculative Workspace warmup without cooldown', posixFixture, async t => {
  const f = await fixture(t);
  await f.runtime.connectSsh(f.config);
  const target = f.runtime.currentSshTarget();
  const marker = path.join(f.directory, 'flaky-agent-worker.marker');
  const flakyPython = path.join(f.directory, 'flaky-agent-python.sh');
  fs.writeFileSync(flakyPython, [
    '#!/bin/sh',
    `if [ ! -f ${shellQuote(marker)} ]; then : > ${shellQuote(marker)}; sleep 0.2; exit 17; fi`,
    'exec python3 "$@"',
  ].join('\n'), {mode: 0o700});
  await f.runtime.saveSettings({serverProfile: {pythonBin: flakyPython}, serverProfileTarget: target});
  await f.runtime.startAgent({demo: true, cwd: f.workspace, startupDelayMs: 0});
  await until(() => f.runtime.remoteWorkspaceConnections.get('agent')?.speculative === true);

  // This request joins the opening speculative lane. Its first launch fails,
  // then the same real request must retry authoritatively instead of inheriting
  // a 30-second speculative cooldown.
  const response = await brokerCall(f.runtime, f.runtime.brokerToken, 'workspace_read', {
    path: 'server-evidence.txt',
  });
  assert.equal(response.status, 200);
  assert.equal(response.result.content, 'live SSH test\n');
  assert.equal(response.result.transport, 'workspace-worker');
  assert.equal(f.runtime.remoteWorkspaceFailures.has('agent'), false);
  assert.equal(f.evidence.commands.filter(command => command.includes(flakyPython)).length, 2,
    'one failed speculative launch is followed immediately by one real launch');
});

test('Agent workspace tools and remote commands reuse one target-fenced persistent lane', posixFixture, async t => {
  const f = await fixture(t); await f.runtime.connectSsh(f.config);
  const explorer = await f.runtime.listRemoteFiles({path: ''});
  assert.equal(explorer.transport, 'workspace-worker');
  const launchesBefore = f.evidence.commands.filter(command => command.includes('workspace-file-worker.py')).length;
  const token = f.runtime.brokerToken;

  const listed = await brokerCall(f.runtime, token, 'workspace_list', {path: ''});
  assert.equal(listed.status, 200); assert.equal(listed.result.transport, 'workspace-worker');
  assert.equal(listed.result.channelReused, false, 'the first business request is a cold lane use');
  assert.ok(listed.result.entries.some(entry => entry.path === 'server-evidence.txt'));
  const opened = await brokerCall(f.runtime, token, 'workspace_read', {path: 'server-evidence.txt'});
  assert.equal(opened.result.content, 'live SSH test\n');
  assert.equal(opened.result.channelReused, true, 'the next request reuses the Agent lane');
  assert.match(opened.result.revision, /^sha256:[0-9a-f]{64}$/);
  const found = await brokerCall(f.runtime, token, 'workspace_search', {query: 'live SSH', path: ''});
  assert.equal(found.result.matches[0].path, 'server-evidence.txt');
  const created = await brokerCall(f.runtime, token, 'workspace_write', {
    path: 'agent-created.txt', content: 'structured workspace\n', create: true,
    bom: true, lineEnding: 'crlf',
  });
  assert.equal(created.result.saved, true);
  assert.deepEqual(fs.readFileSync(path.join(f.workspace, 'agent-created.txt')),
    Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), Buffer.from('structured workspace\r\n')]));
  const attributes = await brokerCall(f.runtime, token, 'workspace_stat', {path: 'agent-created.txt'});
  assert.equal(attributes.result.type, 'file');
  const command = await brokerCall(f.runtime, token, 'remote_exec', {command: 'printf lane-reused'});
  assert.equal(command.result.stdout, 'lane-reused');
  assert.equal(command.result.transport, 'workspace-worker');
  assert.equal(command.result.channelReused, true);

  const launchesAfter = f.evidence.commands.filter(value => value.includes('workspace-file-worker.py')).length;
  assert.equal(launchesAfter - launchesBefore, 1, 'all Agent file and command calls share one persistent Agent lane');
  assert.equal(f.evidence.connections, 1, 'all lanes share the authenticated SSH client');
  const workspaceOperations = f.events.filter(event => event.kind === 'workspace_rpc');
  assert.ok(workspaceOperations.length >= 10);
  assert.ok(workspaceOperations.every(event => event.origin === 'agent'));
  assert.ok(workspaceOperations.some(event => event.method === 'fs.readText'
    && event.state === 'completed' && event.transport === 'workspace-worker'
    && event.channelReused === true));
});

test('Explorer search returns revision snapshots and bulk replace refuses stale remote files', posixFixture, async t => {
  const f = await fixture(t);
  const connected = await f.runtime.connectSsh(f.config);
  const ssh = connected.ssh;
  const expectedConnection = {
    target: {host: ssh.host, port: ssh.port, username: ssh.username, cwd: ssh.cwd},
    root: ssh.remoteRootPath,
    generation: ssh.connectionGeneration,
  };
  const searched = await f.runtime.searchRemoteFiles({
    requestId: 'explorer-search-1', query: 'live SSH', path: '', expectedConnection,
  });
  assert.equal(searched.matches.length, 1);
  assert.match(searched.matches[0].revision, /^sha256:[0-9a-f]{64}$/);
  const replaced = await f.runtime.replaceRemoteFiles({
    query: 'live SSH', replacement: 'persistent SSH', expectedConnection,
    files: [{path: searched.matches[0].path, revision: searched.matches[0].revision}],
  });
  assert.equal(replaced.replacedFiles, 1);
  assert.equal(replaced.atomic, false);
  assert.equal(fs.readFileSync(path.join(f.workspace, 'server-evidence.txt'), 'utf8'), 'persistent SSH test\n');

  const stale = await f.runtime.searchRemoteFiles({
    requestId: 'explorer-search-2', query: 'persistent SSH', path: '', expectedConnection,
  });
  fs.appendFileSync(path.join(f.workspace, 'server-evidence.txt'), 'changed after search\n');
  await assert.rejects(f.runtime.replaceRemoteFiles({
    query: 'persistent SSH', replacement: 'stale overwrite', expectedConnection,
    files: [{path: stale.matches[0].path, revision: stale.matches[0].revision}],
  }), error => error.code === 'REMOTE_FILE_CHANGED');
});

test('Agent session rotation fences workspace and command results resolved in the same turn', posixFixture, async t => {
  const f = await fixture(t); await f.runtime.connectSsh(f.config);
  const context = () => {
    f.runtime.toolStatus();
    return {
      id: f.runtime.toolSession.id,
      token: f.runtime.brokerToken,
      boundTarget: f.runtime.currentSshTarget(),
    };
  };
  const installControlledLane = role => {
    let resolveRequest;
    const record = {
      role, client: f.runtime.client, generation: f.runtime.generation,
      root: f.runtime.ssh.remoteRootPath, pythonExecutable: 'python3', state: 'ready',
      request: () => new Promise(resolve => { resolveRequest = resolve; }),
      cancel: () => false,
      close() { this.state = 'closed'; },
    };
    record.workspaceReady = Promise.resolve(record);
    f.runtime.remoteWorkspaceConnections.set(role, record);
    return value => resolveRequest(value);
  };

  let resolveRequest = installControlledLane('agent');
  const workspace = f.runtime.workspaceRead({path: 'server-evidence.txt'}, context());
  await until(() => f.runtime.pendingWorkspaceRequests.size === 1);
  resolveRequest({result: {path: 'server-evidence.txt', content: 'stale'}, durationMs: 1, channelReused: true});
  f.runtime.rotateToolSession();
  await assert.rejects(workspace, /会话已结束|已取消/);

  const nextContext = context();
  resolveRequest = installControlledLane('agent');
  const command = f.runtime.executeRemote({command: 'printf stale'}, nextContext);
  await until(() => f.runtime.pendingExecs.size === 1);
  resolveRequest({result: {stdout: 'stale', stderr: '', exitCode: 0, durationMs: 1, queueWaitMs: 0},
    durationMs: 1, channelReused: true});
  f.runtime.rotateToolSession();
  await assert.rejects(command, /会话已结束|已取消|通道已关闭/);
});

test('real native PTY demo receives submitted Enter and calls real remote tools', posixFixture, async t => {
  const f = await fixture(t); await f.runtime.connectSsh(f.config);
  await f.runtime.startAgent({demo: true, cwd: f.workspace, startupDelayMs: 200, submitDelayMs: 20});
  await f.runtime.submitAgent({text: '检查服务器工作目录'});
  await until(() => f.events.some(event => event.type === 'terminal' && event.target === 'agent' && event.data.includes('server-evidence.txt')));
  assert.ok(f.events.some(event => event.kind === 'remote_exec' && event.origin === 'agent'
    && event.command === 'pwd && uname -a && ls -la' && event.state === 'completed'
    && event.transport === 'workspace-worker'));
  assert.equal(f.evidence.connections, 1);
  await f.runtime.stopAgent(); assert.equal((await f.runtime.getState()).agent.state, 'stopped');
});

test('Agent launch maintains literal argv and resolves existing adapter placeholders', async t => {
  const f = await fixture(t);
  const fake = path.join(f.directory, 'cli-entry.cjs'); fs.writeFileSync(fake, 'console.log(JSON.stringify({args:process.argv.slice(2),env:process.env.TEAM_SETTING})); setInterval(()=>{},1000);');
  process.env.HETERO_RUNTIME_TEST_SOURCE = 'inherited-setting'; t.after(() => delete process.env.HETERO_RUNTIME_TEST_SOURCE);
  await f.runtime.startAgent({demo: false, executable: process.execPath, args: [fake, '{workspace}', '{mcp_config}', '{agent_session_id}', 'literal ; $HOME'], cwd: f.workspace, environment: {TEAM_SETTING: '{env:HETERO_RUNTIME_TEST_SOURCE}', API_KEY: 'not-saved'}});
  await until(() => f.events.some(event => event.target === 'agent' && event.type === 'terminal' && event.data.includes('inherited-setting')));
  const output = f.events.filter(event => event.target === 'agent' && event.type === 'terminal').map(event => event.data).join('');
  assert.ok(output.includes('literal ; $HOME')); assert.ok(output.includes('inherited-setting'));
  assert.ok(!fs.readFileSync(path.join(f.directory, 'prefs', 'settings.json'), 'utf8').includes('not-saved'));
});

test('local HDC discovery executes argv locally and returns real device ids', posixFixture, async t => {
  const f = await fixture(t); const hdc = path.join(f.directory, 'fake hdc'); fs.writeFileSync(hdc, '#!/bin/sh\n[ "$1" = list ] && [ "$2" = targets ] || exit 9\nprintf "LOCAL-DEVICE-01\\n"\n', {mode: 0o700});
  const result = await f.runtime.discoverDevices({hdcPath: hdc}); assert.equal(result.devices[0].serial, 'LOCAL-DEVICE-01'); assert.equal(result.transport, 'local');
});

test('local HDC hardware profile extraction is exposed through the desktop runtime', posixFixture, async t => {
  const f = await fixture(t); const hdc = path.join(f.directory, 'fake profile hdc');
  const output = [
    'HWP\tBEGIN\tv1',
    'HWP\tMETA\tmodel\tRuntime Fixture',
    'HWP\tMETA\tmanufacturer\tHuawei',
    'HWP\tSYSTEM\tcpu_present\t0-7',
    'HWP\tSYSTEM\tcpu_online\t0-7',
    'HWP\tPOLICY:policy0\trelated_cpus\t0-3',
    'HWP\tPOLICY:policy0\tcpuinfo_min_freq\t400000',
    'HWP\tPOLICY:policy0\tcpuinfo_max_freq\t1800000',
    'HWP\tPOLICY:policy0\tscaling_available_frequencies\t400000 1800000',
    'HWP\tPOLICY:policy4\trelated_cpus\t4-6',
    'HWP\tPOLICY:policy4\tcpuinfo_min_freq\t600000',
    'HWP\tPOLICY:policy4\tcpuinfo_max_freq\t2400000',
    'HWP\tPOLICY:policy4\tscaling_available_frequencies\t600000 2400000',
    'HWP\tPOLICY:policy7\trelated_cpus\t7',
    'HWP\tPOLICY:policy7\tcpuinfo_min_freq\t800000',
    'HWP\tPOLICY:policy7\tcpuinfo_max_freq\t3000000',
    'HWP\tPOLICY:policy7\tscaling_available_frequencies\t800000 3000000',
    'HWP\tEND\tv1',
  ].join('\n');
  fs.writeFileSync(hdc, `#!/bin/sh\n[ "$1" = "-t" ] && [ "$2" = "RUNTIME-DEVICE" ] && [ "$3" = "shell" ] || exit 9\ncat <<'PROFILE'\n${output}\nPROFILE\n`, {mode: 0o700});
  const result = await f.runtime.extractHardwareProfile({hdcPath: hdc, deviceSerial: 'RUNTIME-DEVICE'});
  assert.deepEqual(result.profile.cpu_groups.map(group => group.kind), ['small', 'middle', 'big']);
  assert.equal(result.profile.evidence.device_serial_sha256, crypto.createHash('sha256').update('RUNTIME-DEVICE').digest('hex'));
  assert.equal(JSON.stringify(result).includes('RUNTIME-DEVICE'), false);
  assert.ok(f.events.some(event => event.kind === 'hardware_profile_extract' && event.state === 'completed' && event.domainCount === 3));
});

test('Windows cmd launcher and manifest normalization preserve supported interface', () => {
  const launch = createAgentLaunch('C:\\Agent Tools\\agent.cmd', ['--workspace', 'C:\\Work & Files'], {ComSpec: 'C:\\Windows\\System32\\cmd.exe'}, 'win32');
  assert.equal(launch.file, 'C:\\Windows\\System32\\cmd.exe'); assert.equal(launch.args, '/d /s /v:off /c ""C:\\Agent Tools\\agent.cmd" "--workspace" "C:\\Work & Files""');
  assert.throws(() => createAgentLaunch('agent.cmd', ['%PATH%'], {}, 'win32'), /CMD 展开字符/);
  assert.equal(normalizeAgent({submit_mode: 'plain_text_lf', args: ['{agent_session_id}']}).submitMode, 'plain_text_lf');
  assert.equal(normalizeAgent({}).submitDelayMs, 1000);
  assert.equal(normalizeAgent({submitDelayMs: 0}).submitDelayMs, 100);
  assert.equal(normalizeAgent({submitDelayMs: 100}).submitDelayMs, 100);
  assert.equal(normalizeAgent({submitDelayMs: 6000}).submitDelayMs, 5000);
  assert.throws(() => normalizeAgent({args: ['{unsupported}']}), /占位符/);
});

test('broker rejects missing token and web origins', async t => {
  const f = await fixture(t); const {brokerUrl} = await f.runtime.getIntegration();
  async function request(headers) { return new Promise((resolve, reject) => { const req = http.request(brokerUrl, {method: 'POST', headers}, res => { res.resume(); res.on('end', () => resolve(res.statusCode)); }); req.on('error', reject); req.end('{}'); }); }
  assert.equal(await request({}), 401);
  assert.equal(await request({Authorization: `Bearer ${f.runtime.brokerToken}`, Origin: 'https://example.com'}), 401);
  assert.equal(await request({Authorization: `Bearer ${'é'.repeat(64)}`}), 401);
});

test('capture plan refinement broker contract allows only one exact scoped update', async t => {
  const f = await fixture(t);
  const calls = [];
  f.runtime.updateCapturePlan = async value => {
    calls.push(value);
    return {requestId: value.requestId, planId: value.planId, changed: Boolean(value.title)};
  };
  const context = {
    task_kind: 'capture_plan_refinement',
    capture_plan_id: 'plan-scoped-1',
    capture_plan_request_id: 'request-scoped-1',
    capture_plan_expected_updated_at: '2026-10-31T10:00:00.000Z',
  };
  f.runtime.toolSession.taskContext = context;
  f.runtime.toolSession.taskContract = null;
  const token = f.runtime.brokerToken;

  // Discovery, remote and workspace calls are broker RPCs and therefore not
  // part of this narrowly-scoped task. Rejection itself does not execute the
  // one permitted update, so the Agent may still correct course once.
  for (const [method, params] of [
    ['remote_status', {}],
    ['workspace_read', {path: 'README.md'}],
    ['capture_catalog', {}],
    ['capture_status', {planId: context.capture_plan_id}],
    ['capture_save_plan', {plan: {title: 'must not save'}}],
    ['capture_start', {planId: context.capture_plan_id, deviceSerial: 'PHONE'}],
  ]) {
    const denied = await brokerCall(f.runtime, token, method, params);
    assert.equal(denied.status, 400, `${method} must be denied`);
    assert.match(denied.error, /只允许调用 capture_update_plan/);
  }
  assert.equal(calls.length, 0);

  const exact = {
    planId: context.capture_plan_id,
    requestId: context.capture_plan_request_id,
    expectedUpdatedAt: context.capture_plan_expected_updated_at,
    title: '一次差量更新',
  };
  const accepted = await brokerCall(f.runtime, token, 'capture_update_plan', exact);
  assert.equal(accepted.status, 200);
  assert.equal(accepted.result.planId, context.capture_plan_id);
  assert.deepEqual(calls, [exact]);

  const duplicate = await brokerCall(f.runtime, token, 'capture_update_plan', exact);
  assert.equal(duplicate.status, 400);
  assert.match(duplicate.error, /已经结束/);
  assert.equal(calls.length, 1, 'a completed scoped request is never executed twice');
});

test('capture plan refinement consumes mismatched and failed update attempts', async t => {
  const f = await fixture(t);
  const token = f.runtime.brokerToken;
  let calls = 0;
  f.runtime.updateCapturePlan = async () => { calls += 1; return {changed: false}; };
  const bind = requestId => {
    f.runtime.toolSession.taskContext = {
      task_kind: 'capture_plan_refinement', capture_plan_id: 'plan-match',
      capture_plan_request_id: requestId, capture_plan_expected_updated_at: 'revision-match',
    };
    f.runtime.toolSession.taskContract = null;
  };

  bind('request-mismatch');
  const mismatch = await brokerCall(f.runtime, token, 'capture_update_plan', {
    planId: 'plan-other', requestId: 'request-mismatch', expectedUpdatedAt: 'revision-match',
    confirmUnchanged: true,
  });
  assert.equal(mismatch.status, 400);
  assert.match(mismatch.error, /参数与页面请求不匹配/);
  assert.equal(calls, 0);
  const mismatchRetry = await brokerCall(f.runtime, token, 'capture_update_plan', {
    planId: 'plan-match', requestId: 'request-mismatch', expectedUpdatedAt: 'revision-match',
    confirmUnchanged: true,
  });
  assert.equal(mismatchRetry.status, 400);
  assert.match(mismatchRetry.error, /已经结束/);
  assert.equal(f.events.filter(event => event.kind === 'agent-plan-failed'
    && event.requestId === 'request-mismatch').length, 1,
  'the trusted request gets one terminal failure event even when submitted IDs differ');

  bind('request-runtime-failure');
  f.runtime.updateCapturePlan = async () => { calls += 1; throw new Error('fixture update failed'); };
  const failureParams = {planId: 'plan-match', requestId: 'request-runtime-failure',
    expectedUpdatedAt: 'revision-match', confirmUnchanged: true};
  const failed = await brokerCall(f.runtime, token, 'capture_update_plan', failureParams);
  assert.equal(failed.status, 400); assert.match(failed.error, /fixture update failed/);
  const failedRetry = await brokerCall(f.runtime, token, 'capture_update_plan', failureParams);
  assert.equal(failedRetry.status, 400); assert.match(failedRetry.error, /已经结束/);
  assert.equal(calls, 1, 'a failed scoped update is not retried through the same request');
  assert.equal(f.events.filter(event => event.kind === 'agent-plan-failed'
    && event.requestId === 'request-runtime-failure').length, 1);
});

test('automatic recovery completion is exact, one-shot and idempotent without replaying capture_start', async t => {
  const f = await fixture(t);
  await f.runtime.saveSettings({automation: {
    autoRecoverErrors: true, allowDeviceOperations: true, allowServerOperations: false,
  }});
  f.runtime.capture.updateDevices({devices: [{serial: 'PHONE-A'}]});
  await f.runtime.selectCaptureDevice({serial: 'PHONE-A'});
  f.runtime.toolSession.taskContext = {
    task_kind: 'workbench_error_recovery', repair_request_id: 'repair-exact-1',
    recovery_operation_key: 'capture-start', operation_attempt_id: 'attempt-exact-1',
    recovery_authorization_scopes: ['device'], selected_device: 'PHONE-A',
  };
  f.runtime.toolSession.taskContract = null;
  const token = f.runtime.brokerToken;

  const replay = await brokerCall(f.runtime, token, 'capture_start', {
    planId: 'plan-do-not-replay', deviceSerial: 'PHONE-A',
  });
  assert.equal(replay.status, 400);
  assert.match(replay.error, /不能直接调用 capture_start|工作台按冻结参数重试/);

  const mismatch = await brokerCall(f.runtime, token, 'desktop_recovery_complete', {
    repairRequestId: 'repair-other', outcome: 'repaired', summary: 'wrong request',
  });
  assert.equal(mismatch.status, 400);
  assert.match(mismatch.error, /不匹配/);
  const whitespaceMismatch = await brokerCall(f.runtime, token, 'desktop_recovery_complete', {
    repairRequestId: ' repair-exact-1', outcome: 'repaired', summary: 'wrong request spacing',
  });
  assert.equal(whitespaceMismatch.status, 400);
  assert.match(whitespaceMismatch.error, /不匹配/);

  const params = {repairRequestId: 'repair-exact-1', outcome: 'repaired', summary: '清理残留状态并验证设备可用'};
  const completed = await brokerCall(f.runtime, token, 'desktop_recovery_complete', params);
  assert.equal(completed.status, 200);
  assert.equal(completed.result.completed, true);
  assert.equal(completed.result.duplicate, false);
  assert.equal(completed.result.operationAttemptId, 'attempt-exact-1');
  const duplicate = await brokerCall(f.runtime, token, 'desktop_recovery_complete', params);
  assert.equal(duplicate.status, 200);
  assert.equal(duplicate.result.duplicate, true);
  assert.equal(f.events.filter(event => event.type === 'agent-recovery'
    && event.requestId === 'repair-exact-1').length, 1, 'idempotent completion emits one retry handshake');

  const conflict = await brokerCall(f.runtime, token, 'desktop_recovery_complete', {
    ...params, outcome: 'unable', summary: 'conflicting terminal result',
  });
  assert.equal(conflict.status, 400);
  assert.match(conflict.error, /另一结果结束/);
  const afterCompletion = await brokerCall(f.runtime, token, 'capture_status', {});
  assert.equal(afterCompletion.status, 400);
  assert.match(afterCompletion.error, /已经结束/);
});

test('automatic recovery permits one revision-fenced plan patch and returns its audited retry revision', async t => {
  const f = await fixture(t);
  await f.runtime.saveSettings({automation: {
    autoRecoverErrors: true, allowDeviceOperations: true, allowServerOperations: false,
  }});
  f.runtime.capture.updateDevices({devices: [{serial: 'PHONE-A'}]});
  await f.runtime.selectCaptureDevice({serial: 'PHONE-A'});
  const updates = [];
  f.runtime.updateCapturePlan = async params => {
    updates.push(params);
    return {requestId: params.requestId, planId: params.planId, updatedAt: 'revision-new', changed: true};
  };
  f.runtime.toolSession.taskContext = {
    task_kind: 'workbench_error_recovery', repair_request_id: 'repair-plan-1',
    recovery_operation_key: 'capture-start', operation_attempt_id: 'attempt-plan-1',
    recovery_authorization_scopes: ['device'], selected_device: 'PHONE-A',
    capture_plan_id: 'plan-frozen', capture_plan_expected_updated_at: 'revision-old',
  };
  f.runtime.toolSession.taskContract = null;
  const token = f.runtime.brokerToken;

  const newPlan = await brokerCall(f.runtime, token, 'capture_save_plan', {plan: {title: 'unsafe replacement'}});
  assert.equal(newPlan.status, 400);
  assert.match(newPlan.error, /不能新建或替换采集计划/);
  const wrongRevision = await brokerCall(f.runtime, token, 'capture_update_plan', {
    planId: 'plan-frozen', requestId: 'repair-plan-1', expectedUpdatedAt: 'revision-other',
    title: 'wrong revision',
  });
  assert.equal(wrongRevision.status, 400);
  assert.match(wrongRevision.error, /冻结的 planId、revision 和 repairRequestId/);

  const patchParams = {
    planId: 'plan-frozen', requestId: 'repair-plan-1', expectedUpdatedAt: 'revision-old',
    title: '修复后的计划',
  };
  const patched = await brokerCall(f.runtime, token, 'capture_update_plan', patchParams);
  assert.equal(patched.status, 200);
  assert.deepEqual(updates, [patchParams]);
  const duplicatePatch = await brokerCall(f.runtime, token, 'capture_update_plan', patchParams);
  assert.equal(duplicatePatch.status, 400);
  assert.match(duplicatePatch.error, /已经执行或结束/);

  const completed = await brokerCall(f.runtime, token, 'desktop_recovery_complete', {
    repairRequestId: 'repair-plan-1', outcome: 'repaired', summary: '计划已按原版本完成差量修复',
  });
  assert.equal(completed.status, 200);
  assert.equal(completed.result.retryPlanId, 'plan-frozen');
  assert.equal(completed.result.retryPlanRevision, 'revision-new');
  const event = f.events.find(value => value.type === 'agent-recovery' && value.requestId === 'repair-plan-1');
  assert.equal(event.retryPlanId, 'plan-frozen');
  assert.equal(event.retryPlanRevision, 'revision-new');
});

test('submitting another Agent task terminates an open automatic recovery immediately', async t => {
  const f = await fixture(t);
  await f.runtime.startAgent({demo: true, cwd: f.workspace, startupDelayMs: 0, submitDelayMs: 10});
  f.runtime.toolSession.taskContext = {
    task_kind: 'workbench_error_recovery', repair_request_id: 'repair-superseded-1',
    recovery_operation_key: 'capture-start', operation_attempt_id: 'attempt-superseded-1',
    recovery_authorization_scopes: ['device'], selected_device: 'PHONE-A',
  };
  f.runtime.toolSession.taskContract = null;
  f.runtime.workbenchRecoveryContract();
  await f.runtime.submitAgent({text: '执行新的用户任务', taskContext: {task_kind: 'other'}});
  const event = f.events.find(value => value.type === 'agent-recovery'
    && value.requestId === 'repair-superseded-1');
  assert.equal(event.outcome, 'unable');
  assert.match(event.summary, /新的 Agent 任务替换/);
  assert.equal(f.runtime.toolSession.taskContext.task_kind, 'other');
});

test('automatic recovery rechecks live grants and freezes device and SSH targets', posixFixture, async t => {
  const f = await fixture(t);
  await f.runtime.connectSsh(f.config);
  await f.runtime.saveSettings({automation: {
    autoRecoverErrors: true, allowDeviceOperations: false, allowServerOperations: false,
  }});
  f.runtime.capture.updateDevices({devices: [{serial: 'PHONE-A'}, {serial: 'PHONE-B'}]});
  await f.runtime.selectCaptureDevice({serial: 'PHONE-A'});
  f.runtime.toolSession.taskContext = {
    task_kind: 'workbench_error_recovery', repair_request_id: 'repair-scope-1',
    recovery_operation_key: 'capture-start', operation_attempt_id: 'attempt-scope-1',
    recovery_authorization_scopes: ['device', 'server'], selected_device: 'PHONE-A',
  };
  f.runtime.toolSession.taskContract = null;
  const token = f.runtime.brokerToken;

  const diagnosticRead = await brokerCall(f.runtime, token, 'workspace_read', {path: 'server-evidence.txt'});
  assert.equal(diagnosticRead.status, 200, 'read-only diagnosis does not consume a broad server grant');
  assert.equal(diagnosticRead.result.content, 'live SSH test\n');
  const deniedServer = await brokerCall(f.runtime, token, 'remote_exec', {command: 'printf denied'});
  assert.equal(deniedServer.status, 400);
  assert.match(deniedServer.error, /未获服务器操作授权/);
  const deniedDevice = await brokerCall(f.runtime, token, 'capture_cancel', {runId: 'desktop_run_missing'});
  assert.equal(deniedDevice.status, 400);
  assert.match(deniedDevice.error, /未获手机操作授权/);

  await f.runtime.saveSettings({automation: {
    autoRecoverErrors: true, allowDeviceOperations: true, allowServerOperations: true,
  }});
  const executed = await brokerCall(f.runtime, token, 'remote_exec', {command: 'printf recovery-live'});
  assert.equal(executed.status, 200);
  assert.equal(executed.result.stdout, 'recovery-live');

  await f.runtime.selectCaptureDevice({serial: 'PHONE-B'});
  const wrongDevice = await brokerCall(f.runtime, token, 'capture_cancel', {runId: 'desktop_run_missing'});
  assert.equal(wrongDevice.status, 400);
  assert.match(wrongDevice.error, /选中的手机已改变/);

  await f.runtime.saveSettings({automation: {
    autoRecoverErrors: true, allowDeviceOperations: true, allowServerOperations: false,
  }});
  const revoked = await brokerCall(f.runtime, token, 'workspace_write', {
    path: 'must-not-write.txt', content: 'blocked', create: true,
  });
  assert.equal(revoked.status, 400);
  assert.match(revoked.error, /未获服务器操作授权/);
  assert.equal(fs.existsSync(path.join(f.workspace, 'must-not-write.txt')), false);
});

test('invalid remote cwd fails connect and grouped commands cannot escape a failed cd', posixFixture, async t => {
  const f = await fixture(t);
  await assert.rejects(f.runtime.connectSsh({...f.config, cwd: path.join(f.workspace, 'missing')}), /远程工作目录不可用/);
  await f.runtime.connectSsh(f.config);
  fs.rmSync(f.workspace, {recursive: true});
  const result = await f.runtime.execRemote({command: 'pwd; printf should-not-run'});
  assert.notEqual(result.exitCode, 0); assert.ok(!result.stdout.includes('should-not-run'));
});

test('native PTY brackets wait for asynchronous paste processing and serialize keyboard input', async t => {
  const f = await fixture(t);
  const fake = path.join(f.directory, 'async-tui.cjs');
  fs.writeFileSync(fake, `if(process.stdin.isTTY)process.stdin.setRawMode(true);let ready=false;process.stdout.write('READY\\n');process.stdin.on('data',d=>{const s=d.toString();if(s.includes('\\x1b[201~'))setTimeout(()=>{ready=true;process.stdout.write('PASTE_PROCESSED\\n')},500);if(s.includes('\\r'))process.stdout.write(ready?'SUBMITTED\\n':'DROPPED\\n');if(s==='x')process.stdout.write('KEYBOARD_AFTER\\n');});`);
  await f.runtime.startAgent({demo: false, executable: process.execPath, args: [fake], cwd: f.workspace, startupDelayMs: 0, submitDelayMs: 600, submitMode: 'bracketed_paste_cr'});
  const output = () => f.events.filter(event => event.type === 'terminal' && event.target === 'agent').map(event => event.data).join('');
  await until(() => output().includes('READY'));
  const submitting = f.runtime.submitAgent({text: 'task payload'});
  const typing = f.runtime.terminalInput({target: 'agent', data: 'x'});
  await Promise.all([submitting, typing]);
  await until(() => output().includes('KEYBOARD_AFTER'));
  assert.ok(output().includes('SUBMITTED')); assert.ok(!output().includes('DROPPED'));
  assert.ok(output().indexOf('SUBMITTED') < output().indexOf('KEYBOARD_AFTER'));
});

test('native PTY keeps LF submit behind a slow no-echo paste commit', async t => {
  const f = await fixture(t);
  const fake = path.join(f.directory, 'slow-no-echo-tui.cjs');
  fs.writeFileSync(fake, `if(process.stdin.isTTY)process.stdin.setRawMode(true);let committed=false;process.stdout.write('READY\\n');process.stdin.on('data',d=>{const s=d.toString();if(s.includes('\\x1b[201~'))setTimeout(()=>{committed=true},1500);if(s.includes('\\n'))process.stdout.write(committed?'SUBMITTED_LF\\n':'DROPPED_LF\\n');});`);
  await f.runtime.startAgent({demo: false, executable: process.execPath, args: [fake], cwd: f.workspace,
    startupDelayMs: 0, submitDelayMs: 1600, submitMode: 'bracketed_paste_lf'});
  const output = () => f.events.filter(event => event.type === 'terminal' && event.target === 'agent').map(event => event.data).join('');
  await until(() => output().includes('READY'));
  const result = await f.runtime.submitAgent({text: 'slow paste payload'});
  await until(() => output().includes('SUBMITTED_LF'));
  assert.ok(!output().includes('DROPPED_LF'));
  assert.equal(result.delivery.submitKey, 'lf');
  assert.equal(result.delivery.submitKeyCount, 1);
  assert.equal(result.delivery.waitReason, 'no_output_wait_elapsed');
  assert.ok(result.delivery.actualWaitMs >= 1600);
});

test('pending submit is fenced when Agent session is replaced', async t => {
  const f = await fixture(t); const fake = path.join(f.directory, 'idle.cjs'); fs.writeFileSync(fake, 'setInterval(()=>{},1000)');
  const config = {demo: false, executable: process.execPath, args: [fake], cwd: f.workspace, startupDelayMs: 300};
  await f.runtime.startAgent(config);
  const pending = f.runtime.submitAgent({text: 'must stay on old session'}); const rejection = assert.rejects(pending, /会话已变更/);
  await f.runtime.startAgent(config); await rejection;
});

test('overlapping SSH connect requests keep only the newest session', posixFixture, async t => {
  const f = await fixture(t);
  const older = f.runtime.connectSsh(f.config); const rejected = assert.rejects(older, /取消/);
  const latest = f.runtime.connectSsh(f.config);
  await Promise.all([rejected, latest]);
  assert.equal((await f.runtime.getState()).ssh.state, 'connected'); assert.equal(f.evidence.connections, 1);
});

test('Agent tools stay bound across SSH target changes and allow same-target reconnect', posixFixture, async t => {
  const first = await fixture(t); const second = await fixture(t);
  await first.runtime.connectSsh(first.config);
  await first.runtime.startAgent({demo: true, cwd: first.workspace});
  const token = first.runtime.brokerToken;
  const initial = await brokerCall(first.runtime, token, 'remote_exec', {command: 'printf first-server'});
  assert.equal(initial.result.stdout, 'first-server');
  await first.runtime.connectSsh(second.config);
  const changed = await brokerCall(first.runtime, token, 'remote_status');
  assert.equal(changed.result.targetMismatch, true); assert.equal(changed.result.boundTarget.port, first.config.port); assert.equal(changed.result.currentTarget.port, second.config.port);
  const refused = await brokerCall(first.runtime, token, 'remote_exec', {command: 'printf must-not-run'});
  assert.equal(refused.status, 400); assert.match(refused.error, /重启 Agent/);
  assert.ok(!second.evidence.commands.some(command => command.includes('must-not-run')));
  const manual = await first.runtime.execRemote({command: 'printf manual-current-target'}); assert.equal(manual.stdout, 'manual-current-target');
  await first.runtime.connectSsh(first.config);
  const recovered = await brokerCall(first.runtime, token, 'remote_exec', {command: 'printf same-target'});
  assert.equal(recovered.result.stdout, 'same-target');
  await first.runtime.connectSsh(second.config);
  await first.runtime.startAgent({demo: true, cwd: first.workspace});
  assert.notEqual(first.runtime.brokerToken, token);
  assert.equal((await brokerCall(first.runtime, token, 'remote_status')).status, 401);
  const rebound = await brokerCall(first.runtime, first.runtime.brokerToken, 'remote_exec', {command: 'printf second-server'});
  assert.equal(rebound.result.stdout, 'second-server');
  const runningToken = first.runtime.brokerToken; await first.runtime.stopAgent();
  assert.equal((await brokerCall(first.runtime, runningToken, 'remote_exec', {command: 'printf stale-agent'})).status, 401);
  assert.ok(!second.evidence.commands.some(command => command.includes('stale-agent')));
});

test('Agent started without SSH binds first tool connection and refuses a changed cwd', posixFixture, async t => {
  const f = await fixture(t); await f.runtime.startAgent({demo: true, cwd: f.workspace});
  const token = f.runtime.brokerToken;
  const disconnected = await brokerCall(f.runtime, token, 'remote_status'); assert.equal(disconnected.result.boundTarget, null);
  await f.runtime.connectSsh(f.config);
  const bound = await brokerCall(f.runtime, token, 'remote_status'); assert.equal(bound.result.boundTarget.cwd, f.workspace);
  const nextWorkspace = path.join(f.directory, 'another-workspace'); fs.mkdirSync(nextWorkspace);
  await f.runtime.connectSsh({...f.config, cwd: nextWorkspace});
  const refused = await brokerCall(f.runtime, token, 'remote_exec', {command: 'printf wrong-workspace'});
  assert.equal(refused.status, 400); assert.ok(!f.evidence.commands.some(command => command.includes('wrong-workspace')));
});

test('stopping Agent revokes its running tool channel but keeps manual SSH usable', posixFixture, async t => {
  const f = await fixture(t); await f.runtime.connectSsh(f.config); await f.runtime.startAgent({demo: true, cwd: f.workspace});
  const pending = brokerCall(f.runtime, f.runtime.brokerToken, 'remote_exec', {command: 'sleep 3'});
  await until(() => f.runtime.pendingExecs.size === 1);
  await f.runtime.stopAgent();
  const stopped = await pending; assert.equal(stopped.status, 400); assert.match(stopped.error, /工具会话已结束/);
  const manual = await f.runtime.execRemote({command: 'printf alive'}); assert.equal(manual.stdout, 'alive');
});

test('closed SSH terminal is reported separately while command channels stay usable', posixFixture, async t => {
  const f = await fixture(t); await f.runtime.connectSsh(f.config);
  assert.equal((await f.runtime.getState()).ssh.shellState, 'open');
  f.runtime.shell.close();
  await until(() => f.runtime.ssh.shellState === 'closed');
  assert.equal((await f.runtime.getState()).ssh.state, 'connected');
  assert.ok(f.events.some(event => event.type === 'status' && event.target === 'ssh' && event.shellState === 'closed' && event.state === 'connected'));
  const result = await f.runtime.execRemote({command: 'printf still-connected'}); assert.equal(result.stdout, 'still-connected');
});

test('settings exclude common literal Agent secrets while keeping environment references', async t => {
  const f = await fixture(t);
  await f.runtime.saveSettings({agent: {environment: {AUTHORIZATION: 'hidden-auth', ACCESS_KEY: 'hidden-access', AK: 'hidden-ak', SK: 'hidden-sk', TEAM_SETTING: 'ordinary', API_KEY: '{env:TEAM_TOKEN}'}}});
  const saved = fs.readFileSync(path.join(f.directory, 'prefs', 'settings.json'), 'utf8');
  assert.ok(!saved.includes('hidden-')); assert.ok(saved.includes('ordinary')); assert.ok(saved.includes('{env:TEAM_TOKEN}'));
});

test('real SSH shell decodes split UTF-8 Chinese bytes across stdout and stderr packets', posixFixture, async t => {
  const f = await fixture(t); await f.runtime.connectSsh(f.config);
  await until(() => f.evidence.terminalInput.includes('exec bash'));
  const channel = f.evidence.shellChannels[0];
  const output = () => f.events.filter(event => event.type === 'terminal' && event.target === 'ssh').map(event => event.data).join('');
  for (const [stream, text] of [[channel, '中文终端'], [channel.stderr, '错误诊断']]) {
    for (const byte of Buffer.from(text, 'utf8')) { stream.write(Buffer.from([byte])); await new Promise(resolve => setTimeout(resolve, 2)); }
  }
  channel.write('utf8-complete');
  await until(() => output().includes('utf8-complete'));
  assert.ok(output().includes('中文终端'), JSON.stringify(output())); assert.ok(output().includes('错误诊断'), JSON.stringify(output())); assert.ok(!output().includes('\uFFFD'), JSON.stringify(output()));
});
