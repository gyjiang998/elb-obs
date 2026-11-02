const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const {gzipSync} = require('node:zlib');
const test = require('node:test');
const assert = require('node:assert/strict');

const root = path.resolve(__dirname, '..');
const {CODE_GRAPH_REQUEST_TIMEOUT_MS, DEFAULT_PLATFORM_REQUEST_CONCURRENCY, DEFAULT_PLATFORM_REQUEST_TIMEOUT_MS, REMOTE_WORKER_UNAVAILABLE_EXIT_CODE, RPC_GZIP_MARKER, RPC_MARKER, RPC_REPAIR_MARKER, RPC_SCHEMA, SEARCH_PLATFORM_REQUEST_TIMEOUT_MS, assertPlatformContextUnchanged, buildPlatformRequest, createConcurrentTaskQueue, createPlatformContextMonitor, parsePlatformResponse, platformContextKey, platformRequestTimeout, platformResultError, recoverRemoteWorker, remoteWorkerRecoveryRequired, requestBody, resolvePlatformContext, serverProfileInitializationRequired} = require('../renderer/platform-transport.cjs');

function encode(value) { return Buffer.from(JSON.stringify(value)).toString('base64'); }

test('desktop platform requests are bounded and queued instead of exhausting SSH channels', async () => {
  const queue = createConcurrentTaskQueue(2);
  const releases = [];
  let active = 0; let peak = 0;
  const tasks = Array.from({length: 12}, (_, index) => queue.run(async () => {
    active += 1; peak = Math.max(peak, active);
    await new Promise(resolve => releases.push(resolve));
    active -= 1;
    return index;
  }));
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(queue.snapshot(), {active: 2, pending: 10, limit: 2});
  assert.equal(peak, 2);
  while (releases.length) {
    releases.shift()();
    await new Promise(resolve => setImmediate(resolve));
  }
  assert.deepEqual(await Promise.all(tasks), Array.from({length: 12}, (_, index) => index));
  assert.equal(peak, 2);
  assert.deepEqual(queue.snapshot(), {active: 0, pending: 0, limit: 2});
});

test('queued platform requests can be cancelled before opening an SSH channel', async () => {
  const queue = createConcurrentTaskQueue(1);
  let release;
  const first = queue.run(() => new Promise(resolve => { release = resolve; }));
  const controller = new AbortController();
  let secondStarted = false;
  const second = queue.run(() => { secondStarted = true; }, {signal: controller.signal});
  controller.abort();
  await assert.rejects(second, error => error?.name === 'AbortError');
  assert.equal(secondStarted, false);
  release();
  await first;
});

test('desktop page worker uses one in-flight request and dynamically prioritizes the active page', async () => {
  assert.equal(DEFAULT_PLATFORM_REQUEST_CONCURRENCY, 1);
  const queue = createConcurrentTaskQueue();
  let release;
  const order = [];
  const blocker = queue.run(() => new Promise(resolve => { release = resolve; }));
  await new Promise(resolve => setImmediate(resolve));
  let activePage = 'workload-features';
  const hidden = queue.run(() => { order.push('hidden'); }, {priority: () => activePage === 'workload-features' ? 1 : 0});
  activePage = 'parallel-modeling';
  const currentFirst = queue.run(() => { order.push('current-first'); }, {priority: () => activePage === 'parallel-modeling' ? 1 : 0});
  const currentSecond = queue.run(() => { order.push('current-second'); }, {priority: () => activePage === 'parallel-modeling' ? 1 : 0});
  release();
  await Promise.all([blocker, hidden, currentFirst, currentSecond]);
  assert.deepEqual(order, ['current-first', 'current-second', 'hidden'], 'priority is re-evaluated at dispatch and ties remain FIFO');
});

test('parallel search keeps one remote command open longer without changing ordinary request limits', () => {
  assert.equal(platformRequestTimeout('/v1/parallel-modeling/searches', 'POST'), SEARCH_PLATFORM_REQUEST_TIMEOUT_MS);
  assert.equal(platformRequestTimeout('/v1/desktop-capture/import-runtime-trace', 'POST'), SEARCH_PLATFORM_REQUEST_TIMEOUT_MS);
  assert.equal(SEARCH_PLATFORM_REQUEST_TIMEOUT_MS, 10 * 60 * 1000);
  assert.equal(platformRequestTimeout('/v1/parallel-modeling/searches', 'GET'), DEFAULT_PLATFORM_REQUEST_TIMEOUT_MS);
  assert.equal(platformRequestTimeout('/v1/parallel-modeling/datasets', 'GET'), DEFAULT_PLATFORM_REQUEST_TIMEOUT_MS);
  assert.equal(CODE_GRAPH_REQUEST_TIMEOUT_MS, 30 * 60 * 1000);
  assert.equal(platformRequestTimeout('/v1/code-graphs/estimates', 'POST'), CODE_GRAPH_REQUEST_TIMEOUT_MS);
  assert.equal(platformRequestTimeout('/v1/code-graphs/builds', 'POST'), CODE_GRAPH_REQUEST_TIMEOUT_MS);
  assert.equal(platformRequestTimeout('/v1/code-graphs/mcg_123/confirm', 'POST'), CODE_GRAPH_REQUEST_TIMEOUT_MS);
  assert.equal(platformRequestTimeout('/v1/code-graphs', 'GET'), DEFAULT_PLATFORM_REQUEST_TIMEOUT_MS);
});

test('desktop platform request uses the selected managed profile and an encoded JSON contract', () => {
  const {request, command} = buildPlatformRequest({
    profileId: 'project-a',
    scopeId: '1234567890abcdef',
    path: '/v1/workload-features/analyze',
    method: 'POST',
    body: JSON.stringify({dataset_id: 'pm_123'}),
  });
  assert.deepEqual(request, {
    schema_version: RPC_SCHEMA,
    method: 'POST',
    path: '/v1/workload-features/analyze',
    body: {dataset_id: 'pm_123'},
  });
  assert.match(command, /server-profiles\/targets\/1234567890abcdef\/project-a\/platform\.env/);
  assert.match(command, /backend\.tools\.desktop_platform_rpc/);
  assert.match(command, /HETERO_PLATFORM_REPO_ROOT/);
  assert.match(command, /"\$connection_root"/);
  assert.match(command, /rev-parse --show-toplevel/);
  assert.match(command, /if \[ -n "\$git_root" \]; then add_rpc_root/);
  assert.doesNotMatch(command, /pm_123/, 'request data stays encoded in the shell command');
});

test('streaming platform request starts a reusable stdio worker without embedding one request', () => {
  const spec = buildPlatformRequest({
    profileId: 'default', scopeId: '1234567890abcdef', streaming: true,
    path: '/v1/application-flows', method: 'GET',
  });
  assert.equal(spec.streaming, true);
  assert.equal(spec.request.path, '/v1/application-flows');
  assert.match(spec.command, /backend\.tools\.desktop_platform_rpc --stdio-server/);
  assert.doesNotMatch(spec.command, /--request-base64/);
});

test('a verified managed worker root is preferred without exposing repair markers to users', () => {
  const managed = '/home/user/.local/share/heterogeneous-workbench/server-profiles/targets/1234567890abcdef/default/assets/analysis-worker/aabb';
  const {command} = buildPlatformRequest({profileId: 'default', scopeId: '1234567890abcdef',
    platformWorkerRoot: managed, path: '/v1/workload-features/catalog'});
  assert.ok(command.indexOf(`add_rpc_root '${managed}'`) < command.indexOf('add_rpc_root "${HETERO_PLATFORM_REPO_ROOT:-}"'));
  assert.throws(() => buildPlatformRequest({profileId: 'default', scopeId: '1234567890abcdef',
    platformWorkerRoot: 'relative/worker', path: '/v1/workload-features/catalog'}), /托管分析 Worker 路径/);
  const result = {exitCode: REMOTE_WORKER_UNAVAILABLE_EXIT_CODE,
    stderr: `${RPC_REPAIR_MARKER}REMOTE_WORKER_MISSING\t未找到 Desktop 分析 Worker。\n`};
  assert.equal(remoteWorkerRecoveryRequired(result), true);
  const error = platformResultError(result);
  assert.equal(error.code, 'REMOTE_WORKER_MISSING');
  assert.equal(error.repairKind, 'worker');
  assert.equal(error.message, '未找到 Desktop 分析 Worker。');
  assert.doesNotMatch(error.message, /HETERO_/);
  assert.equal(remoteWorkerRecoveryRequired({exitCode: 1, stderr: result.stderr}), false);
  const flattened = {exitCode: REMOTE_WORKER_UNAVAILABLE_EXIT_CODE,
    stderr: 'HETERO_DESKTOP_REPAIRABLE_REMOTE_WORKER_MISSING 未找到 Desktop 分析 Worker。'};
  assert.equal(remoteWorkerRecoveryRequired(flattened), true);
  assert.equal(platformResultError(flattened).message, '未找到 Desktop 分析 Worker。');
});

test('missing remote worker deploys once and retries the same request with the managed root', async () => {
  const missing = {exitCode: REMOTE_WORKER_UNAVAILABLE_EXIT_CODE,
    stderr: `${RPC_REPAIR_MARKER}REMOTE_WORKER_MISSING\t未找到 Desktop 分析 Worker。\n`};
  const calls = [];
  const recovered = await recoverRemoteWorker(missing, {
    deploy: async () => { calls.push('deploy'); return {root: '/managed/worker'}; },
    retry: async root => { calls.push(`retry:${root}`); return {exitCode: 0, stdout: 'ok'}; },
  });
  assert.deepEqual(calls, ['deploy', 'retry:/managed/worker']);
  assert.deepEqual(recovered, {result: {exitCode: 0, stdout: 'ok'}, root: '/managed/worker'});
  const normal = {exitCode: 2, stderr: 'ordinary failure'};
  assert.deepEqual(await recoverRemoteWorker(normal, {
    deploy: async () => { throw new Error('must not run'); }, retry: async () => normal,
  }), {result: normal, root: ''});
  await assert.rejects(recoverRemoteWorker(missing, {
    deploy: async () => { throw new Error('HETERO_DESKTOP_REPAIRABLE\tREMOTE_WORKER_MISSING\tworker missing'); }, retry: async () => normal,
  }), error => error.code === 'DESKTOP_PLATFORM_WORKER_UNAVAILABLE' && error.repairKind === 'worker'
    && !error.message.includes('HETERO_'));
});

test('desktop platform response accepts plain and gzip versioned marker envelopes', async () => {
  const value = {schema_version: RPC_SCHEMA, ok: true, result: {kind: 'json', value: {profiles: []}}};
  assert.deepEqual(await parsePlatformResponse(`diagnostic\n${RPC_MARKER}${encode(value)}\n`), value);
  const compressed = gzipSync(Buffer.from(JSON.stringify(value))).toString('base64');
  assert.deepEqual(await parsePlatformResponse(`${RPC_GZIP_MARKER}${compressed}\n`), value);
  await assert.rejects(parsePlatformResponse('diagnostic only'), /没有返回/);
  await assert.rejects(parsePlatformResponse(`${RPC_MARKER}${encode({...value, schema_version: 'old'})}`), /版本不兼容/);
});

test('desktop platform request validates profile IDs and JSON-only bodies', () => {
  assert.deepEqual(requestBody(undefined), {});
  assert.deepEqual(requestBody('{"ok":true}'), {ok: true});
  assert.throws(() => requestBody('[]'), /JSON 对象/);
  assert.throws(() => requestBody('{'), /有效 JSON/);
  assert.throws(() => buildPlatformRequest({profileId: '../escape', path: '/v1/workload-features/catalog'}), /Profile ID/);
  assert.throws(() => buildPlatformRequest({profileId: 'default', scopeId: 'unscoped', path: '/v1/workload-features/catalog'}), /目标作用域/);
  assert.throws(() => buildPlatformRequest({profileId: 'default', scopeId: '1234567890abcdef', path: '/v1/workload-features/analyze', method: 'POST', body: JSON.stringify({measurements: 'x'.repeat(26000)})}), /32 KiB/);
});

test('desktop platform rejects direct calibration fits before encoding measurement contents', () => {
  const request = {profileId: 'default', scopeId: '1234567890abcdef',
    path: '/v1/parallel-calibration/fits', method: 'POST'};
  assert.throws(() => buildPlatformRequest({...request, body: '{not valid JSON'}), /受管暂存上传/);
  assert.throws(() => buildPlatformRequest({...request,
    body: JSON.stringify({measurements: 'secret-measurement'.repeat(3000)})}), /受管暂存上传/);
});

test('only the explicit missing or incomplete managed environment triggers automatic initialization', () => {
  assert.equal(serverProfileInitializationRequired({exitCode: 78, stderr: '服务器环境尚未初始化：/home/user/platform.env'}), true);
  assert.equal(serverProfileInitializationRequired({exitCode: 78, stderr: '服务器环境配置已更新：异构工作仓目录需要重新初始化。'}), true);
  assert.equal(serverProfileInitializationRequired({exitCode: 78, stderr: 'HETERO_REPO_ROOT 未配置。'}), true);
  assert.equal(serverProfileInitializationRequired({exitCode: 78, stderr: 'HETERO_PYTHON_BIN 未配置。'}), true);
  assert.equal(serverProfileInitializationRequired({exitCode: 1, stderr: '服务器环境尚未初始化'}), false);
  assert.equal(serverProfileInitializationRequired({exitCode: 78, stderr: 'unrelated validation failure'}), false);
});

test('stale managed worker and Python settings trigger automatic profile re-initialization', () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-platform-stale-profile-'));
  const profileDirectory = path.join(home, '.local/share/heterogeneous-workbench/server-profiles/targets/1234567890abcdef/project-a');
  fs.mkdirSync(profileDirectory, {recursive: true});
  const envFile = path.join(profileDirectory, 'platform.env');
  fs.writeFileSync(envFile, 'HETERO_REPO_ROOT="/srv/old-worker"\nHETERO_PYTHON_BIN="/usr/bin/python3.10"\n');
  const workerRequest = buildPlatformRequest({
    profileId: 'project-a', scopeId: '1234567890abcdef', profile: {workerRepoRoot: '/srv/new-worker', pythonBin: '/usr/bin/python3.10'},
    path: '/v1/workload-features/catalog',
  });
  const workerResult = spawnSync('bash', ['-c', workerRequest.command], {cwd: home, env: {...process.env, HOME: home}, encoding: 'utf8'});
  assert.equal(workerResult.status, 78);
  assert.match(workerResult.stderr, /异构工作仓目录需要重新初始化/);
  assert.equal(serverProfileInitializationRequired({exitCode: workerResult.status, stderr: workerResult.stderr}), true);

  fs.writeFileSync(envFile, 'HETERO_REPO_ROOT="/srv/new-worker"\nHETERO_PYTHON_BIN="/usr/bin/python3.10"\n');
  const pythonRequest = buildPlatformRequest({
    profileId: 'project-a', scopeId: '1234567890abcdef', profile: {workerRepoRoot: '/srv/new-worker', pythonBin: '/opt/python/bin/python3'},
    path: '/v1/workload-features/catalog',
  });
  const pythonResult = spawnSync('bash', ['-c', pythonRequest.command], {cwd: home, env: {...process.env, HOME: home}, encoding: 'utf8'});
  assert.equal(pythonResult.status, 78);
  assert.match(pythonResult.stderr, /Python 路径需要重新初始化/);
  assert.equal(serverProfileInitializationRequired({exitCode: pythonResult.status, stderr: pythonResult.stderr}), true);
  fs.rmSync(home, {recursive: true, force: true});
});

test('generated command expands HOME and sources a real profile fixture', () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-platform-home-'));
  const repo = path.join(home, 'repo');
  const profileDirectory = path.join(home, '.local/share/heterogeneous-workbench/server-profiles/targets/1234567890abcdef/project-a');
  const fakePython = path.join(home, 'fake-python');
  fs.mkdirSync(path.join(repo, 'backend/tools'), {recursive: true});
  fs.writeFileSync(path.join(repo, 'backend/tools/desktop_platform_rpc.py'), '# fixture\n');
  fs.mkdirSync(profileDirectory, {recursive: true});
  fs.writeFileSync(fakePython, '#!/bin/sh\nprintf \'STATE_ROOT=%s\\n\' "$HETERO_STATE_ROOT"\nprintf \'PYTHONPATH_ROOT=%s\\n\' "${PYTHONPATH%%:*}"\nprintf \'HETERO_DESKTOP_RPC\\tdGVzdA==\\n\'\n', {mode: 0o700});
  fs.writeFileSync(path.join(profileDirectory, 'platform.env'), `HETERO_REPO_ROOT="${repo}"\nHETERO_PYTHON_BIN="${fakePython}"\nHETERO_STATE_ROOT="${home}/state"\n`);
  const {command} = buildPlatformRequest({profileId: 'project-a', scopeId: '1234567890abcdef', path: '/v1/workload-features/catalog'});
  const result = spawnSync('bash', ['-c', command], {cwd: home, env: {...process.env, HOME: home}, encoding: 'utf8'});
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /^HETERO_DESKTOP_RPC\t/m);
  assert.match(result.stdout, new RegExp(`STATE_ROOT=${home.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}/state`));
  assert.match(result.stdout, new RegExp(`PYTHONPATH_ROOT=${repo.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`));
  fs.rmSync(home, {recursive: true, force: true});
});

test('platform request automatically uses the SSH working tree when the build-worker repo is different', () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-platform-split-root-'));
  const buildWorkerRepo = path.join(home, 'build-worker');
  const platformRepo = path.join(home, 'platform repo');
  const profileDirectory = path.join(home, '.local/share/heterogeneous-workbench/server-profiles/targets/1234567890abcdef/project-a');
  const fakePython = path.join(home, 'fake-python');
  fs.mkdirSync(path.join(buildWorkerRepo, 'backend/tools'), {recursive: true});
  fs.mkdirSync(path.join(platformRepo, 'backend/tools'), {recursive: true});
  fs.mkdirSync(profileDirectory, {recursive: true});
  fs.writeFileSync(path.join(buildWorkerRepo, 'backend/tools/desktop_platform_rpc.py'), '# stale build-worker fixture\n');
  fs.writeFileSync(path.join(platformRepo, 'backend/tools/desktop_platform_rpc.py'), '# fixture\n');
  fs.writeFileSync(fakePython, '#!/bin/sh\nprintf \'RPC_CWD=%s\\n\' "$PWD"\nprintf \'HETERO_DESKTOP_RPC\\tdGVzdA==\\n\'\n', {mode: 0o700});
  fs.writeFileSync(path.join(profileDirectory, 'platform.env'), `HETERO_REPO_ROOT="${buildWorkerRepo}"\nHETERO_PYTHON_BIN="${fakePython}"\n`);
  const {command} = buildPlatformRequest({profileId: 'project-a', scopeId: '1234567890abcdef', path: '/v1/workload-features/catalog'});
  const result = spawnSync('bash', ['-c', command], {cwd: platformRepo, env: {...process.env, HOME: home}, encoding: 'utf8'});
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, new RegExp(`RPC_CWD=${fs.realpathSync(platformRepo).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`));
  fs.rmSync(home, {recursive: true, force: true});
});

test('platform request falls back to a repository virtual environment when configured Python cannot import the worker', () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-platform-venv-'));
  const repo = path.join(home, 'repo');
  const profileDirectory = path.join(home, '.local/share/heterogeneous-workbench/server-profiles/targets/1234567890abcdef/project-a');
  const brokenPython = path.join(home, 'broken-python');
  const venvPython = path.join(repo, '.venv/bin/python');
  fs.mkdirSync(path.join(repo, 'backend/tools'), {recursive: true});
  fs.mkdirSync(path.dirname(venvPython), {recursive: true});
  fs.mkdirSync(profileDirectory, {recursive: true});
  fs.writeFileSync(path.join(repo, 'backend/tools/desktop_platform_rpc.py'), '# fixture\n');
  fs.writeFileSync(brokenPython, '#!/bin/sh\nexit 1\n', {mode: 0o700});
  fs.writeFileSync(venvPython, '#!/bin/sh\nprintf \'VENV_RPC=1\\n\'\nprintf \'HETERO_DESKTOP_RPC\\tdGVzdA==\\n\'\n', {mode: 0o700});
  fs.writeFileSync(path.join(profileDirectory, 'platform.env'), `HETERO_REPO_ROOT="${repo}"\nHETERO_PYTHON_BIN="${brokenPython}"\n`);
  const {command} = buildPlatformRequest({profileId: 'project-a', scopeId: '1234567890abcdef', path: '/v1/workload-features/catalog'});
  const result = spawnSync('bash', ['-c', command], {cwd: home, env: {...process.env, HOME: home}, encoding: 'utf8'});
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /VENV_RPC=1/);
  fs.rmSync(home, {recursive: true, force: true});
});

test('platform request probes the complete repository and Python matrix instead of stopping at a bad first root', () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-platform-root-matrix-'));
  const staleRepo = path.join(home, 'stale-platform');
  const readyRepo = path.join(home, 'ready-platform');
  const profileDirectory = path.join(home, '.local/share/heterogeneous-workbench/server-profiles/targets/1234567890abcdef/project-a');
  const fakePython = path.join(home, 'root-aware-python');
  for (const repo of [staleRepo, readyRepo]) {
    fs.mkdirSync(path.join(repo, 'backend/tools'), {recursive: true});
    fs.writeFileSync(path.join(repo, 'backend/tools/desktop_platform_rpc.py'), '# fixture\n');
  }
  fs.mkdirSync(profileDirectory, {recursive: true});
  fs.writeFileSync(fakePython, `#!/bin/sh\n[ "$PWD" = "${staleRepo}" ] && exit 3\nprintf 'SELECTED_ROOT=%s\\n' "$PWD"\nprintf 'HETERO_DESKTOP_RPC\\tdGVzdA==\\n'\n`, {mode: 0o700});
  fs.writeFileSync(path.join(profileDirectory, 'platform.env'), `HETERO_PLATFORM_REPO_ROOT="${staleRepo}"\nHETERO_REPO_ROOT="${readyRepo}"\nHETERO_PYTHON_BIN="${fakePython}"\n`);
  const {command} = buildPlatformRequest({profileId: 'project-a', scopeId: '1234567890abcdef', path: '/v1/workload-features/catalog'});
  const result = spawnSync('bash', ['-c', command], {cwd: home, env: {...process.env, HOME: home}, encoding: 'utf8'});
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, new RegExp(`SELECTED_ROOT=${readyRepo.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`));
  fs.rmSync(home, {recursive: true, force: true});
});

test('unusable Python combinations return a stable repair classification marker', () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-platform-bad-python-'));
  const repo = path.join(home, 'repo');
  const profileDirectory = path.join(home, '.local/share/heterogeneous-workbench/server-profiles/targets/1234567890abcdef/project-a');
  const brokenPython = path.join(home, 'broken-python');
  fs.mkdirSync(path.join(repo, 'backend/tools'), {recursive: true});
  fs.mkdirSync(profileDirectory, {recursive: true});
  fs.writeFileSync(path.join(repo, 'backend/tools/desktop_platform_rpc.py'), '# fixture\n');
  fs.writeFileSync(brokenPython, '#!/bin/sh\nprintf \'probe failed\\n\' >&2\nexit 1\n', {mode: 0o700});
  fs.writeFileSync(path.join(profileDirectory, 'platform.env'), `HETERO_REPO_ROOT="${repo}"\nHETERO_PYTHON_BIN="${brokenPython}"\n`);
  const {command} = buildPlatformRequest({profileId: 'project-a', scopeId: '1234567890abcdef', path: '/v1/workload-features/catalog'});
  const result = spawnSync('bash', ['-c', command], {cwd: home, env: {...process.env, HOME: home}, encoding: 'utf8'});
  assert.equal(result.status, REMOTE_WORKER_UNAVAILABLE_EXIT_CODE);
  assert.match(result.stderr, new RegExp(`${RPC_REPAIR_MARKER}REMOTE_WORKER_PYTHON_UNAVAILABLE`));
  fs.rmSync(home, {recursive: true, force: true});
});

test('platform request reports a repairable worker error instead of invoking Python from the wrong directory', () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-platform-missing-worker-'));
  const profileDirectory = path.join(home, '.local/share/heterogeneous-workbench/server-profiles/targets/1234567890abcdef/project-a');
  const fakePython = path.join(home, 'fake-python');
  fs.mkdirSync(profileDirectory, {recursive: true});
  fs.writeFileSync(fakePython, '#!/bin/sh\nprintf \'python must not run\\n\' >&2\nexit 99\n', {mode: 0o700});
  fs.writeFileSync(path.join(profileDirectory, 'platform.env'), `HETERO_REPO_ROOT="${home}/wrong"\nHETERO_PYTHON_BIN="${fakePython}"\n`);
  const {command} = buildPlatformRequest({profileId: 'project-a', scopeId: '1234567890abcdef', path: '/v1/workload-features/catalog'});
  const result = spawnSync('bash', ['-c', command], {cwd: home, env: {...process.env, HOME: home}, encoding: 'utf8'});
  assert.equal(result.status, REMOTE_WORKER_UNAVAILABLE_EXIT_CODE);
  assert.match(result.stderr, new RegExp(`${RPC_REPAIR_MARKER}REMOTE_WORKER_MISSING`));
  assert.match(result.stderr, /未找到服务器分析服务/);
  assert.doesNotMatch(result.stderr, /python must not run/);
  assert.doesNotMatch(result.stderr, /No module named backend/);
  fs.rmSync(home, {recursive: true, force: true});
});

test('platform result is discarded when saved SSH target or profile changes during a request', () => {
  const targetA = {host: 'worker-a.example', port: 22, username: 'alice', cwd: '/srv/a'};
  const connectedA = {state: 'connected', ...targetA};
  const initial = {
    ssh: connectedA,
    settings: {serverProfileTarget: targetA, serverProfileScopeId: '1234567890abcdef', serverProfile: {profileId: 'default'}},
  };
  const snapshot = resolvePlatformContext(initial);
  assert.doesNotThrow(() => assertPlatformContextUnchanged(snapshot, initial));
  const savedBWhileStillConnectedA = {
    ssh: connectedA,
    settings: {
      serverProfileTarget: {host: 'worker-b.example', port: 22, username: 'alice', cwd: '/srv/b'},
      serverProfileScopeId: 'fedcba0987654321',
      serverProfile: {profileId: 'default'},
    },
  };
  assert.throws(() => assertPlatformContextUnchanged(snapshot, savedBWhileStillConnectedA), /已丢弃旧结果/);
  assert.throws(() => assertPlatformContextUnchanged(snapshot, {
    ...initial,
    settings: {...initial.settings, serverProfile: {profileId: 'another-profile'}},
  }), /已丢弃旧结果/);
});

test('shared page context identity changes across SSH targets and server profiles', () => {
  const targetA = {host: 'WORKER-A.example', port: 22, username: 'alice', cwd: '/srv/a/'};
  const targetB = {host: 'worker-b.example', port: 22, username: 'alice', cwd: '/srv/b'};
  const state = (target, profileId = 'default', scopeId = '1234567890abcdef') => ({
    ssh: {state: 'connected', ...target},
    settings: {serverProfileTarget: target, serverProfileScopeId: scopeId, serverProfile: {profileId}},
  });
  const keyA = platformContextKey(state(targetA));
  assert.equal(keyA, platformContextKey(state({...targetA, host: 'worker-a.example', cwd: '/srv/a'})), 'canonical target aliases retain one context');
  assert.notEqual(keyA, platformContextKey(state(targetB, 'default', 'fedcba0987654321')), 'A to B changes context');
  assert.notEqual(keyA, platformContextKey(state(targetA, 'another-profile')), 'profile switch changes context');
  assert.notEqual(keyA, platformContextKey({...state(targetA), settings: {...state(targetA).settings,
    serverProfile: {profileId: 'default', workerRepoRoot: '/srv/new-worker'}}}), 'profile resource changes reset mounted server data');
  assert.notEqual(keyA, platformContextKey({...state(targetA), ssh: {state: 'disconnected'}}), 'disconnect invalidates server-backed page state');

  let current = state(targetA);
  const transitions = [];
  const monitor = createPlatformContextMonitor(() => current, transition => transitions.push(transition));
  assert.equal(monitor.sync(), false);
  current = state({...targetA, host: 'worker-a.example', cwd: '/srv/a'});
  assert.equal(monitor.sync(), false, 'canonical spelling changes do not reset a page');
  current = state(targetB, 'default', 'fedcba0987654321');
  assert.equal(monitor.sync(), true, 'A to B resets mounted server data');
  current = state(targetB, 'analysis-v2', 'fedcba0987654321');
  assert.equal(monitor.sync(), true, 'profile changes reset mounted server data');
  assert.equal(transitions.length, 2);
});

test('analysis studios are real shared React pages with target-bound cancellable transport', () => {
  const html = fs.readFileSync(path.join(root, 'renderer/index.html'), 'utf8');
  const renderer = fs.readFileSync(path.join(root, 'renderer/renderer.js'), 'utf8');
  const shared = fs.readFileSync(path.join(root, 'renderer/shared-pages.jsx'), 'utf8');
  const transport = fs.readFileSync(path.join(root, 'renderer/platform-transport.cjs'), 'utf8');
  const modeling = fs.readFileSync(path.join(root, '..', 'app/components/ParallelModelingStudio.tsx'), 'utf8');
  const calibration = fs.readFileSync(path.join(root, '..', 'app/components/OptimizationCalibrationStudio.tsx'), 'utf8');
  assert.match(html, /data-page="workload-features"/);
  assert.match(html, /id="workload-features-root"/);
  assert.match(html, /data-page="hardware-models"/);
  assert.match(html, /id="hardware-models-root"/);
  assert.match(html, /data-page="optimization-calibration"/);
  assert.match(html, /id="optimization-calibration-root"/);
  assert.match(html, /data-page="parallel-modeling"/);
  assert.match(html, /id="parallel-modeling-root"/);
  assert.match(renderer, /createSharedPages/);
  assert.match(shared, /import \{ WorkloadFeaturesStudio \} from '\.\.\/\.\.\/app\/components\/WorkloadFeaturesStudio'/);
  assert.match(shared, /import \{ HardwareModelsStudio \} from '\.\.\/\.\.\/app\/components\/HardwareModelsStudio'/);
  assert.match(shared, /extractHardwareProfile/);
  assert.match(shared, /installPlatformTransport/);
  assert.match(shared, /analysisPages\.has\(context\.owner\) \? context\.owner : activePage/);
  assert.match(shared, /priority: \(\) => requestPage === activePage \? 1 : 0/);
  assert.match(modeling, /platformApiFor\('parallel-modeling'\)/);
  assert.match(shared, /buildPlatformRequest\(\{profileId, scopeId,[\s\S]{0,120}profile: state\.settings\?\.serverProfile/);
  assert.match(shared, /assistantEnabled=\{false\}/);
  assert.match(shared, /executionMode="inline"/);
  assert.match(shared, /measurementImportEnabled=\{false\}/);
  assert.match(calibration, /onFitMeasurements\?: CalibrationMeasurementFitHandler/);
  assert.match(calibration, /await onFitMeasurements\(\{ planId: plan\.id, file: measurementFile as File, text: measurementText \}\)/);
  assert.match(calibration, /measurementImportEnabled \|\| Boolean\(onFitMeasurements\)/,
    'A Desktop upload handler must enable the shared file-selection UI without embedding measurements in the SSH command');
  assert.match(calibration, /file\.size > measurementByteLimit/);
  assert.match(shared, /onFitMeasurements=\{fitCalibrationMeasurements\}/);
  assert.match(shared, /\/v1\/parallel-calibration\/measurement-staging/);
  assert.match(shared, /invoke\('uploadCalibrationMeasurements'/);
  assert.match(shared, /\/v1\/parallel-calibration\/fits\/from-staging/);
  assert.match(shared, /invoke\('discardCalibrationMeasurementUpload'/);
  assert.match(shared, /\/v1\/parallel-calibration\/measurement-staging\/discard/);
  assert.doesNotMatch(shared, /fits\/from-staging[\s\S]{0,400}measurementText/,
    'measurement contents must travel over typed SFTP IPC, never in a remote command body');
  assert.match(transport, /serverProfileScopeId/);
  assert.match(shared, /expectedTarget/);
  assert.match(shared, /requestId/);
  assert.match(shared, /invoke\('platformRequest'/);
  assert.match(shared, /invoke\('cancelPlatformRequest'/);
  assert.match(shared, /streaming: true/);
  assert.doesNotMatch(shared, /invoke\('execRemote'/,
    'shared analysis pages must use the framed Platform RPC worker instead of opening one exec channel per request');
  assert.match(shared, /platform\.cancelAll\(\)/);
  assert.match(shared, /invoke\('ensureServerProfile'/);
  assert.match(shared, /invoke\('ensureRemoteAnalysisWorker'/);
  assert.match(shared, /serverProfileInitializationRequired\(result\)/);
  assert.match(shared, /root\.unmount\(\)/);
  assert.match(renderer, /sharedPages\?\.syncContext\(\)/);
  assert.match(shared, /(?:const|let) response = await parsePlatformResponse[\s\S]{0,240}assertPlatformContextUnchanged/);
  assert.match(shared, /\/v1\/parallel-modeling\/datasets/);
  assert.match(shared, /listTraceAnalysisSources/);
  assert.match(shared, /prepareTraceArchive/);
  assert.match(shared, /uploadTraceArchive/);
  assert.match(shared, /\/v1\/desktop-capture\/staging/);
  assert.match(shared, /\/v1\/desktop-capture\/import-package/);
  assert.match(shared, /\/v1\/desktop-capture\/discard-staging/);
  assert.doesNotMatch(shared, /catch[\s\S]{0,500}\/v1\/desktop-capture\/import-package/,
    'failed transfers use the discard contract and never retry an import for cleanup');
  assert.match(shared, /capture\?\.run_id === source\.runId && capture\?\.step_id === source\.stepId/);
  assert.match(shared, /\/v1\/workload-features\/analyze/);
  assert.match(shared, /initialFeatureProfileId=\{featureProfileId\}/);
  assert.match(shared, /modelingHandoffRevision \+= 1/);
  assert.match(shared, /handoffRevision=\{modelingHandoffRevision\}/);
  assert.match(modeling, /selectDataset\(initialDatasetId, initialFeatureProfileId \?\? '', handoffRevision > 0\)/);
  assert.match(modeling, /\[initialDatasetId, initialFeatureProfileId, handoffRevision, selectDataset\]/);
  assert.match(modeling, /setDatasets\(\(current\) => \[next, \.\.\.current\.filter/);
  assert.match(modeling, /window\.addEventListener\('parallel-workspace-updated', updated\)/);
  assert.match(modeling, /已采集与历史归档负载/);
  const capture = fs.readFileSync(path.join(root, 'renderer/capture.js'), 'utf8');
  const main = fs.readFileSync(path.join(root, 'main.cjs'), 'utf8');
  assert.match(capture, /trace-analyze-features/);
  assert.match(capture, /analyzeTrace\(id, selectedStepId\)/);
  assert.match(capture, /trace-analysis-step/);
  for (const method of ['listTraceAnalysisSources', 'prepareTraceArchive', 'uploadTraceArchive', 'discardTraceUpload']) assert.match(main, new RegExp(`'${method}'`));
});
