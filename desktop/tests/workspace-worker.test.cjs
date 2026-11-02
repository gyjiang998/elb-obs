'use strict';

const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn, spawnSync} = require('node:child_process');
const test = require('node:test');

const {
  MAX_RESPONSE_BYTES, READY_LINE, RESPONSE_HEADER, SCHEMA_VERSION, STREAM_PROTOCOL, WorkspaceRpcChannel,
} = require('../runtime/workspace-rpc-channel.cjs');
const {
  createWorkspaceWorkerLaunch, workspaceWorkerSource,
} = require('../runtime/workspace-worker.cjs');

const python = spawnSync('/usr/bin/env', ['sh', '-c', 'command -v python3'], {encoding: 'utf8'}).stdout.trim();
const pythonAvailable = Boolean(python);

const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

function controlledSshClient() {
  const writes = [];
  const stream = new EventEmitter();
  stream.stderr = new EventEmitter();
  stream.write = value => { writes.push(Buffer.from(value)); return true; };
  stream.end = () => {};
  stream.close = () => {};
  stream.signal = () => {};
  return {
    stream, writes,
    client: {exec: (_command, done) => done(null, stream)},
  };
}

function responseFrame(id, result) {
  const body = Buffer.from(JSON.stringify({schema_version: SCHEMA_VERSION, id, ok: true, result}), 'utf8');
  return Buffer.concat([Buffer.from(`${RESPONSE_HEADER}${body.length}\n`, 'ascii'), body]);
}

function requestBodies(writes) {
  return writes.map(frame => {
    const newline = frame.indexOf(0x0a);
    return JSON.parse(frame.subarray(newline + 1).toString('utf8'));
  });
}

async function waitFor(predicate, timeoutMs = 2000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await delay(10);
  }
  assert.fail('timed out waiting for worker-side condition');
}

function localSshClient(evidence, {noise = ''} = {}) {
  return {
    exec(command, done) {
      evidence.commands.push(command);
      const child = spawn('/bin/sh', ['-c', noise ? `printf ${JSON.stringify(noise)}; ${command}` : command], {
        stdio: ['pipe', 'pipe', 'pipe'],
      });
      evidence.children.push(child);
      const stream = new EventEmitter();
      stream.stderr = child.stderr;
      stream.write = value => child.stdin.write(value);
      stream.end = () => child.stdin.end();
      stream.close = () => child.stdin.end();
      stream.signal = value => { try { child.kill(value === 'TERM' ? 'SIGTERM' : value); } catch {} };
      child.stdout.on('data', value => stream.emit('data', value));
      child.stdout.on('error', error => stream.emit('error', error));
      child.on('error', error => stream.emit('error', error));
      child.on('close', (code, signal) => stream.emit('close', code, signal));
      queueMicrotask(() => done(null, stream));
    },
  };
}

async function fixture(t, options = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-workspace-worker-'));
  const root = path.join(directory, 'workspace');
  const outside = path.join(directory, 'outside.txt');
  fs.mkdirSync(path.join(root, 'src', 'nested'), {recursive: true});
  fs.writeFileSync(path.join(root, 'src', 'hello.txt'), 'first 远程\nsecond marker HETERO_WORKSPACE_RPC_RESPONSE\nthird\n');
  fs.writeFileSync(path.join(root, 'src', 'nested', 'code.cpp'), 'int main() { return 0; }\n');
  fs.writeFileSync(path.join(root, 'binary.dat'), Buffer.from([0, 1, 2, 3]));
  fs.writeFileSync(outside, 'secret\n');
  let symlinkAvailable = true;
  try { fs.symlinkSync(outside, path.join(root, 'escape.txt')); }
  catch (error) { if (error.code === 'EPERM') symlinkAvailable = false; else throw error; }
  const source = options.source || workspaceWorkerSource();
  const launch = createWorkspaceWorkerLaunch({
    workspaceRoot: root,
    pythonExecutable: python,
    source,
    maxTextBytes: options.maxTextBytes || 1024 * 1024,
    workers: options.workers || 4,
  });
  const evidence = {commands: [], children: []};
  const channel = new WorkspaceRpcChannel({
    client: localSshClient(evidence, options), command: launch.command,
    bootstrapPayload: launch.bootstrapPayload, readyTimeoutMs: 5000, requestTimeoutMs: 5000,
    maxPendingRequests: options.channelMaxPendingRequests || 64,
  });
  await channel.ready;
  t.after(async () => {
    channel.close();
    for (const child of evidence.children) if (child.exitCode === null) child.kill('SIGKILL');
    fs.rmSync(directory, {recursive: true, force: true});
  });
  return {channel, root, outside, evidence, symlinkAvailable, launch};
}

const workerTest = pythonAvailable ? test : test.skip;

test('workspace worker launch is install-free, portless and shell-quotes its root', () => {
  const source = Buffer.from('print("fixture")\n');
  const launch = createWorkspaceWorkerLaunch({
    workspaceRoot: "/srv/work space/it's-here", pythonExecutable: '/opt/Python 3/bin/python3', source,
  });
  assert.equal(launch.bootstrapPayload, source);
  assert.equal(launch.sourceBytes, source.length);
  assert.match(launch.sourceSha256, /^[0-9a-f]{64}$/);
  assert.match(launch.command, /sys\.stdin\.buffer\.read\(n\)/);
  assert.match(launch.command, /--stdio-server/);
  assert.doesNotMatch(launch.command, /(?:--port|listen|base64)/i);
  assert.match(launch.command, /'\/srv\/work space\/it'"'"'s-here'/);
});

test('workspace channel accepts split ready/header/body data and merges out-of-order responses by id', async t => {
  const f = controlledSshClient();
  const channel = new WorkspaceRpcChannel({
    client: f.client, command: 'controlled-worker', readyTimeoutMs: 1000, requestTimeoutMs: 1000,
  });
  t.after(() => channel.close());
  const ready = Buffer.from(`${READY_LINE}\n`, 'utf8');
  f.stream.emit('data', ready.subarray(0, 7));
  f.stream.emit('data', ready.subarray(7));
  await channel.ready;

  const first = channel.request({requestId: 'split-a', method: 'ping'});
  const second = channel.request({requestId: 'split-b', method: 'ping'});
  await waitFor(() => f.writes.length === 2);
  const [firstWireId, secondWireId] = requestBodies(f.writes).map(value => value.id);
  const merged = Buffer.concat([
    responseFrame(secondWireId, {value: 'second'}),
    responseFrame(firstWireId, {value: 'first'}),
  ]);
  for (const boundary of [3, 19, 57, merged.length]) {
    const start = boundary === 3 ? 0 : [3, 19, 57][[3, 19, 57, merged.length].indexOf(boundary) - 1];
    f.stream.emit('data', merged.subarray(start, boundary));
  }
  assert.deepEqual((await first).result, {value: 'first'});
  assert.deepEqual((await second).result, {value: 'second'});
  assert.equal(channel.state, 'ready');
  assert.equal(f.writes.length, 2);
});

test('workspace channel closes only its lane when a response frame exceeds the protocol limit', async () => {
  const f = controlledSshClient();
  const channel = new WorkspaceRpcChannel({
    client: f.client, command: 'controlled-worker', readyTimeoutMs: 1000, requestTimeoutMs: 1000,
  });
  f.stream.emit('data', Buffer.from(`${READY_LINE}\n`, 'utf8'));
  await channel.ready;
  const pending = channel.request({requestId: 'oversized', method: 'ping'});
  await waitFor(() => f.writes.length === 1);
  f.stream.emit('data', Buffer.from(`${RESPONSE_HEADER}${MAX_RESPONSE_BYTES + 1}\n`, 'ascii'));
  await assert.rejects(pending, error => error.code === 'WORKSPACE_RPC_FRAME_TOO_LARGE');
  assert.equal(channel.state, 'closed');
});

test('workspace channel gives every retry a unique wire id so a late cancelled response cannot resolve it', async t => {
  const f = controlledSshClient();
  const channel = new WorkspaceRpcChannel({
    client: f.client, command: 'controlled-worker', readyTimeoutMs: 1000, requestTimeoutMs: 1000,
  });
  t.after(() => channel.close());
  f.stream.emit('data', Buffer.from(`${READY_LINE}\n`, 'utf8'));
  await channel.ready;

  const cancelled = channel.request({requestId: 'logical-retry', method: 'ping'});
  await waitFor(() => requestBodies(f.writes).some(value => value.method === 'ping'));
  const firstWireId = requestBodies(f.writes)[0].id;
  assert.equal(channel.cancel('logical-retry'), true);
  await assert.rejects(cancelled, error => error.code === 'WORKSPACE_RPC_CANCELLED');

  const retried = channel.request({requestId: 'logical-retry', method: 'ping'});
  await waitFor(() => requestBodies(f.writes).filter(value => value.method === 'ping').length === 2);
  const businessRequests = requestBodies(f.writes).filter(value => value.method === 'ping');
  const secondWireId = businessRequests.at(-1).id;
  assert.notEqual(secondWireId, firstWireId);
  let settled = false;
  retried.finally(() => { settled = true; }).catch(() => {});
  f.stream.emit('data', responseFrame(firstWireId, {value: 'stale'}));
  await delay(10);
  assert.equal(settled, false, 'the old attempt response must remain an ignored tombstone');
  f.stream.emit('data', responseFrame(secondWireId, {value: 'current'}));
  assert.deepEqual((await retried).result, {value: 'current'});
});

workerTest('workspace worker serves list, stat, framed text reads and bounded prefetch on one SSH channel', async t => {
  const f = await fixture(t, {noise: 'shell-profile-noise\\n'});
  const ping = await f.channel.request({method: 'ping'});
  assert.equal(ping.result.protocol, SCHEMA_VERSION);
  assert.equal(STREAM_PROTOCOL, 'remote_workspace_rpc_stream/v1');
  assert.match(f.channel.stdoutNoise, /shell-profile-noise/);

  const listed = (await f.channel.request({method: 'fs.list', params: {path: 'src'}})).result;
  assert.deepEqual(listed.entries.map(value => value.path), ['src/nested', 'src/hello.txt']);
  const attributes = (await f.channel.request({method: 'fs.stat', params: {path: 'src/hello.txt'}})).result;
  assert.equal(attributes.type, 'file');

  const opened = (await f.channel.request({method: 'fs.readText', params: {path: 'src/hello.txt'}})).result;
  assert.match(opened.content, /HETERO_WORKSPACE_RPC_RESPONSE/);
  assert.match(opened.revision, /^sha256:[0-9a-f]{64}$/);
  assert.equal(opened.lineEnding, 'lf');

  const recursive = (await f.channel.request({method: 'fs.list', params: {
    path: 'src', recursive: true, maxDepth: 3, maxEntries: 20,
  }})).result;
  assert.equal(recursive.recursive, true);
  assert.ok(recursive.entries.some(value => value.path === 'src/nested/code.cpp'));

  const prefetched = (await f.channel.request({method: 'fs.prefetch', params: {
    paths: ['src'], maxDepth: 3, maxDirectories: 20, maxEntries: 100,
  }})).result;
  assert.deepEqual(Object.keys(prefetched).sort(), ['cancelled', 'directories', 'failures', 'reason', 'stats', 'truncated']);
  assert.ok(prefetched.directories.some(value => value.path === 'src/nested'));
  assert.equal(f.evidence.commands.length, 1, 'all calls reuse one SSH exec channel');
});

workerTest('prefetch reports truncation when one directory exceeds the list ceiling', async t => {
  const f = await fixture(t);
  const crowded = path.join(f.root, 'crowded'); fs.mkdirSync(crowded);
  for (let index = 0; index < 5001; index++) fs.writeFileSync(path.join(crowded, `f-${index}.txt`), '');
  const result = (await f.channel.request({method: 'fs.prefetch', params: {
    paths: ['crowded'], maxDepth: 1, maxDirectories: 2, maxEntries: 10000, timeBudgetMs: 30000,
  }, timeoutMs: 35000})).result;
  assert.equal(result.truncated, true);
  assert.equal(result.reason, 'entry_limit');
  assert.equal(result.stats.entryCount, 5000);
});

workerTest('workspace reads support line slices while revision continues to fence the complete file', async t => {
  const f = await fixture(t);
  const complete = (await f.channel.request({method: 'fs.readText', params: {path: 'src/hello.txt'}})).result;
  const slice = (await f.channel.request({method: 'fs.readText', params: {
    path: 'src/hello.txt', lineStart: 2, lineEnd: 2,
  }})).result;
  assert.equal(slice.content, 'second marker HETERO_WORKSPACE_RPC_RESPONSE\n');
  assert.equal(slice.lineStart, 2);
  assert.equal(slice.lineEnd, 2);
  assert.equal(slice.totalLines, 3);
  assert.equal(slice.revision, complete.revision);
  await assert.rejects(f.channel.request({method: 'fs.readText', params: {
    path: 'src/hello.txt', lineStart: 0, lineEnd: 2,
  }}), error => error.code === 'WORKSPACE_INVALID_LINE_RANGE');
});

workerTest('workspace path fence rejects traversal, binary content and symlink escape', async t => {
  const f = await fixture(t);
  await assert.rejects(f.channel.request({method: 'fs.readText', params: {path: '../outside.txt'}}),
    error => error.code === 'WORKSPACE_PATH_OUTSIDE_ROOT');
  await assert.rejects(f.channel.request({method: 'fs.readText', params: {path: 'binary.dat'}}),
    error => error.code === 'WORKSPACE_BINARY_FILE');
  if (f.symlinkAvailable) {
    const listed = (await f.channel.request({method: 'fs.list', params: {path: ''}})).result;
    assert.equal(listed.entries.find(value => value.name === 'escape.txt').accessible, false);
    await assert.rejects(f.channel.request({method: 'fs.readText', params: {path: 'escape.txt'}}),
      error => error.code === 'WORKSPACE_SYMLINK_OUTSIDE_ROOT');
  }
});

workerTest('workspace file actions reuse the framed lane and preserve path and revision fences', async t => {
  const f = await fixture(t);
  const sourceStat = (await f.channel.request({method: 'fs.stat', params: {path: 'src/hello.txt'}})).result;
  const source = (await f.channel.request({method: 'fs.readText', params: {path: 'src/hello.txt'}})).result;
  const copied = (await f.channel.request({method: 'fs.copy', params: {
    path: 'src/hello.txt', destinationPath: 'src/hello-copy.txt',
    expectedRevision: source.revision,
  }})).result;
  assert.equal(copied.copied, true);
  assert.equal(fs.readFileSync(path.join(f.root, 'src', 'hello-copy.txt'), 'utf8'), source.content);
  await assert.rejects(f.channel.request({method: 'fs.copy', params: {
    path: 'src/hello.txt', destinationPath: 'src/hello-copy.txt',
    expectedSize: sourceStat.size, expectedMtimeMs: sourceStat.mtimeMs,
  }}), error => error.code === 'WORKSPACE_FILE_EXISTS');

  const renamed = (await f.channel.request({method: 'fs.rename', params: {
    path: 'src/hello-copy.txt', newName: 'renamed.txt', expectedSize: copied.size,
    expectedMtimeMs: copied.mtimeMs,
  }})).result;
  assert.equal(renamed.path, 'src/renamed.txt');
  assert.equal(fs.existsSync(path.join(f.root, 'src', 'hello-copy.txt')), false);
  assert.equal(fs.readFileSync(path.join(f.root, 'src', 'renamed.txt'), 'utf8'), source.content);
  const directoryStat = (await f.channel.request({method: 'fs.stat', params: {path: 'src/nested'}})).result;
  const renamedDirectory = (await f.channel.request({method: 'fs.rename', params: {
    path: 'src/nested', newName: 'renamed-nested', expectedMtimeMs: directoryStat.mtimeMs,
  }})).result;
  assert.equal(renamedDirectory.path, 'src/renamed-nested');
  assert.equal(fs.readFileSync(path.join(f.root, 'src', 'renamed-nested', 'code.cpp'), 'utf8'), 'int main() { return 0; }\n');

  const largeDownload = Buffer.alloc(6 * 1024 * 1024 + 137);
  for (let index = 0; index < largeDownload.length; index += 65537) largeDownload[index] = index % 251;
  fs.writeFileSync(path.join(f.root, 'large-download.bin'), largeDownload);
  const chunks = [];
  let downloadOffset = 0;
  let downloadSnapshot = '';
  do {
    const downloaded = (await f.channel.request({method: 'fs.readChunk', params: {
      path: 'large-download.bin', offset: downloadOffset, length: 512 * 1024,
      ...(downloadSnapshot ? {expectedSnapshot: downloadSnapshot} : {}),
    }})).result;
    downloadSnapshot ||= downloaded.snapshot;
    assert.equal(downloaded.snapshot, downloadSnapshot);
    assert.equal(downloaded.offset, downloadOffset);
    const bytes = Buffer.from(downloaded.contentBase64, 'base64');
    assert.equal(bytes.length, downloaded.bytesRead);
    chunks.push(bytes);
    downloadOffset += bytes.length;
    if (downloaded.eof) break;
  } while (true);
  assert.equal(Buffer.concat(chunks).equals(largeDownload), true, 'downloads are not bounded by one RPC frame');
  const verified = (await f.channel.request({method: 'fs.readChunk', params: {
    path: 'large-download.bin', offset: largeDownload.length, length: 1,
    expectedSnapshot: downloadSnapshot,
  }})).result;
  assert.equal(verified.eof, true);
  assert.equal(verified.bytesRead, 0);

  const firstMutableChunk = (await f.channel.request({method: 'fs.readChunk', params: {
    path: 'binary.dat', offset: 0, length: 2,
  }})).result;
  fs.appendFileSync(path.join(f.root, 'binary.dat'), Buffer.from([4]));
  await assert.rejects(f.channel.request({method: 'fs.readChunk', params: {
    path: 'binary.dat', offset: 2, length: 2, expectedSnapshot: firstMutableChunk.snapshot,
  }}), error => error.code === 'WORKSPACE_FILE_CHANGED');
  await assert.rejects(f.channel.request({method: 'fs.readChunk', params: {
    path: 'binary.dat', offset: 0, length: 512 * 1024 + 1,
  }}), error => error.code === 'WORKSPACE_INVALID_REQUEST');

  const helloIdentity = (await f.channel.request({method: 'fs.readText', params: {
    path: 'src/hello.txt',
  }})).result;
  const identityChecked = (await f.channel.request({method: 'fs.readChunk', params: {
    path: 'src/hello.txt', offset: 0, length: 4,
    expectedSize: helloIdentity.size,
    expectedMtimeMs: helloIdentity.mtimeMs,
    expectedRevision: helloIdentity.revision,
  }})).result;
  assert.equal(Buffer.from(identityChecked.contentBase64, 'base64').toString('utf8'), 'firs');
  await assert.rejects(f.channel.request({method: 'fs.readChunk', params: {
    path: 'src/hello.txt', offset: 0, length: 4,
    expectedSize: helloIdentity.size + 1,
  }}), error => error.code === 'WORKSPACE_FILE_CHANGED');
  await assert.rejects(f.channel.request({method: 'fs.readChunk', params: {
    path: 'src/hello.txt', offset: 4, length: 4,
    expectedMtimeMs: helloIdentity.mtimeMs,
  }}), error => error.code === 'WORKSPACE_INVALID_REQUEST');
  const oversized = fs.openSync(path.join(f.root, 'over-32g.bin'), 'w');
  fs.ftruncateSync(oversized, 32 * 1024 * 1024 * 1024 + 1);
  fs.closeSync(oversized);
  await assert.rejects(f.channel.request({method: 'fs.readChunk', params: {
    path: 'over-32g.bin', offset: 0, length: 1,
  }}), error => error.code === 'WORKSPACE_DOWNLOAD_TOO_LARGE' && /32 GiB/.test(error.message));
  assert.equal(f.evidence.commands.length, 1, 'list/edit actions continue sharing one SSH exec channel');

  await assert.rejects(f.channel.request({method: 'fs.rename', params: {
    path: 'src/renamed.txt', newName: '../escape.txt',
  }}), error => error.code === 'WORKSPACE_PATH_INVALID');
  await assert.rejects(f.channel.request({method: 'fs.copy', params: {
    path: 'src/renamed.txt', destinationPath: '../escape.txt',
  }}), error => error.code === 'WORKSPACE_PATH_OUTSIDE_ROOT');
  if (f.symlinkAvailable) {
    await assert.rejects(f.channel.request({method: 'fs.rename', params: {
      path: 'escape.txt', newName: 'escaped.txt',
    }}), error => error.code === 'WORKSPACE_SYMLINK_WRITE_BLOCKED');
  }
});

workerTest('workspace copy rejects a source changed while bytes are being copied', async t => {
  const source = workspaceWorkerSource().toString('utf8');
  const readLine = '                chunk = os.read(source_fd, 256 * 1024)\n';
  assert.ok(source.includes(readLine), 'test hook must match the copy read loop');
  const slowSource = source.replace(
    readLine,
    `${readLine}                if chunk:\n                    time.sleep(0.05)\n`,
  );
  const f = await fixture(t, {source: slowSource});
  const sourcePath = path.join(f.root, 'src', 'changing.bin');
  fs.writeFileSync(sourcePath, Buffer.alloc(4 * 1024 * 1024, 0x31));

  const copying = f.channel.request({method: 'fs.copy', params: {
    path: 'src/changing.bin', destinationPath: 'src/changing-copy.bin',
  }});
  await waitFor(() => fs.readdirSync(path.join(f.root, 'src'))
    .some(name => name.startsWith('.hetero-workspace-') && name.endsWith('.tmp')));
  const descriptor = fs.openSync(sourcePath, 'r+');
  fs.writeSync(descriptor, Buffer.from('changed'), 0, 7, 1024);
  fs.closeSync(descriptor);

  await assert.rejects(copying, error => error.code === 'WORKSPACE_FILE_CHANGED');
  assert.equal(fs.existsSync(path.join(f.root, 'src', 'changing-copy.bin')), false,
    'a torn copy must never be committed');
  assert.deepEqual(fs.readdirSync(path.join(f.root, 'src'))
    .filter(name => name.startsWith('.hetero-workspace-')), [],
  'the rejected copy cleans its temporary file');
});

workerTest('workspace search supports glob, case and regex without silently weakening the query', async t => {
  const f = await fixture(t);
  fs.writeFileSync(path.join(f.root, 'src', 'root.cpp'), 'int root_only_marker = 1;\n');
  const plain = (await f.channel.request({method: 'fs.search', params: {
    query: 'RETURN', path: 'src', glob: '**/*.cpp', caseSensitive: false,
  }})).result;
  assert.equal(plain.matches.length, 1);
  assert.equal(plain.matches[0].path, 'src/nested/code.cpp');
  assert.match(plain.matches[0].revision, /^sha256:[0-9a-f]{64}$/);
  const regex = (await f.channel.request({method: 'fs.search', params: {
    query: 'r.t.rn\\s+0', path: 'src', glob: '**/*.cpp', regex: true,
  }})).result;
  assert.equal(regex.matches.length, 1);
  assert.match(regex.matches[0].revision, /^sha256:[0-9a-f]{64}$/);
  const recursiveGlob = (await f.channel.request({method: 'fs.search', params: {
    query: 'root_only_marker', path: 'src', glob: '**/*.cpp', caseSensitive: true,
  }})).result;
  assert.deepEqual(recursiveGlob.matches.map(value => value.path), ['src/root.cpp']);
  const singleSegmentGlob = (await f.channel.request({method: 'fs.search', params: {
    query: 'return 0', path: 'src', glob: '*.cpp', caseSensitive: true,
  }})).result;
  assert.equal(singleSegmentGlob.matches.length, 0, '*.cpp must not span into src/nested');
  await assert.rejects(f.channel.request({method: 'fs.search', params: {
    query: '[', path: 'src', regex: true,
  }}), error => error.code === 'WORKSPACE_INVALID_REGEX');
});

workerTest('catastrophic regex is isolated and bounded without poisoning the shared lane', async t => {
  const f = await fixture(t);
  fs.writeFileSync(path.join(f.root, 'src', 'redos.txt'), `${'a'.repeat(100000)}X\n`);
  const started = Date.now();
  const searched = (await f.channel.request({method: 'fs.search', params: {
    query: '(a+)+$', path: 'src', glob: '**/*.txt', regex: true,
    timeBudgetMs: 200, maxFiles: 10,
  }, timeoutMs: 3000})).result;
  assert.equal(searched.reason, 'time_budget');
  assert.ok(Date.now() - started < 2000, 'regex helper must be killable at the search deadline');
  const healthy = await f.channel.request({method: 'fs.stat', params: {path: 'src/hello.txt'}});
  assert.equal(healthy.result.type, 'file');
});

workerTest('workspace writes require explicit create and use complete-content revision fencing', async t => {
  const f = await fixture(t);
  const opened = (await f.channel.request({method: 'fs.readText', params: {path: 'src/hello.txt'}})).result;
  const saved = (await f.channel.request({method: 'fs.writeText', params: {
    path: 'src/hello.txt', content: 'edited\n', expectedRevision: opened.revision,
    bom: false, lineEnding: 'lf', create: false,
  }})).result;
  assert.equal(saved.saved, true);
  assert.equal(fs.readFileSync(path.join(f.root, 'src', 'hello.txt'), 'utf8'), 'edited\n');
  await assert.rejects(f.channel.request({method: 'fs.writeText', params: {
    path: 'src/hello.txt', content: 'format metadata omitted', expectedRevision: saved.revision, create: false,
  }}), error => error.code === 'WORKSPACE_TEXT_METADATA_REQUIRED');
  await assert.rejects(f.channel.request({method: 'fs.writeText', params: {
    path: 'src/hello.txt', content: 'stale', expectedRevision: opened.revision, create: false,
  }}), error => error.code === 'WORKSPACE_FILE_CHANGED');
  await assert.rejects(f.channel.request({method: 'fs.writeText', params: {
    path: 'src/new.txt', content: 'new', create: false,
  }}), error => error.code === 'WORKSPACE_CREATE_REQUIRED');
  const created = (await f.channel.request({method: 'fs.writeText', params: {
    path: 'src/new.txt', content: 'new', create: true,
  }})).result;
  assert.equal(created.saved, true);
  assert.equal(fs.statSync(path.join(f.root, 'src', 'new.txt')).mode & 0o077, 0,
    'new workspace files are private even under a permissive process umask');
  await assert.rejects(f.channel.request({method: 'fs.writeText', params: {
    path: 'src/new.txt', content: 'duplicate', create: true,
  }}), error => error.code === 'WORKSPACE_FILE_EXISTS');
});

workerTest('forced SSH lane shutdown removes an in-flight write temporary file before process exit', async t => {
  const original = workspaceWorkerSource().toString('utf8');
  const marker = '                output.write(data)\n';
  assert.ok(original.includes(marker));
  const slowed = Buffer.from(original.replace(marker,
    `${marker}                output.flush()\n                time.sleep(5.0)\n`), 'utf8');
  const f = await fixture(t, {source: slowed});
  const pending = f.channel.request({requestId: 'shutdown-write', method: 'fs.writeText', params: {
    path: 'src/shutdown.txt', content: 'x'.repeat(1024 * 1024), create: true,
    bom: false, lineEnding: 'lf',
  }, timeoutMs: 10000});
  await waitFor(() => fs.readdirSync(path.join(f.root, 'src'))
    .some(name => name.startsWith('.hetero-workspace-') && name.endsWith('.tmp')));
  f.channel.close();
  await assert.rejects(pending, /关闭|closed/i);
  await waitFor(() => f.evidence.children[0].exitCode !== null, 3000);
  assert.deepEqual(fs.readdirSync(path.join(f.root, 'src'))
    .filter(name => name.startsWith('.hetero-workspace-') && name.endsWith('.tmp')), []);
  assert.equal(fs.existsSync(path.join(f.root, 'src', 'shutdown.txt')), false);
});

workerTest('workspace accepts a 1 MiB document whose JSON representation expands beyond 2 MiB', async t => {
  const f = await fixture(t, {maxTextBytes: 2 * 1024 * 1024});
  const content = '\\"'.repeat(512 * 1024);
  assert.equal(Buffer.byteLength(content), 1024 * 1024);
  const saved = (await f.channel.request({method: 'fs.writeText', params: {
    path: 'src/json-expanded.txt', content, create: true, bom: false, lineEnding: 'lf',
  }, timeoutMs: 10000})).result;
  assert.equal(saved.size, Buffer.byteLength(content));
  const opened = (await f.channel.request({method: 'fs.readText', params: {
    path: 'src/json-expanded.txt', maxBytes: 2 * 1024 * 1024,
  }, timeoutMs: 10000})).result;
  assert.equal(opened.content, content);
});

workerTest('workspace rejects lone JSON surrogates without taking down the shared channel', async t => {
  const f = await fixture(t);
  await assert.rejects(f.channel.request({method: 'fs.writeText', params: {
    path: 'src/surrogate.txt', content: '\ud800', create: true, bom: false, lineEnding: 'lf',
  }}), error => error.code === 'WORKSPACE_RPC_INVALID_REQUEST');
  const healthy = await f.channel.request({method: 'fs.stat', params: {path: 'src/hello.txt'}});
  assert.equal(healthy.result.type, 'file');
});

workerTest('cancelled queued write never lands after its client deadline', async t => {
  const f = await fixture(t, {workers: 1});
  const started = path.join(f.root, 'worker-busy');
  const busy = f.channel.request({requestId: 'occupy-only-worker', method: 'proc.exec', params: {
    command: `${JSON.stringify(python)} -c 'from pathlib import Path;import time;Path("worker-busy").write_text("1");time.sleep(.35)'`,
    timeoutMs: 2000,
  }, timeoutMs: 3000});
  await waitFor(() => fs.existsSync(started));
  await assert.rejects(f.channel.request({requestId: 'cancelled-late-write', method: 'fs.writeText', params: {
    path: 'src/must-not-land.txt', content: 'cancelled', create: true, bom: false, lineEnding: 'lf',
  }, timeoutMs: 120}), error => error.code === 'WORKSPACE_RPC_REQUEST_TIMEOUT');
  await busy;
  await delay(80);
  assert.equal(fs.existsSync(path.join(f.root, 'src', 'must-not-land.txt')), false);
});

workerTest('workspace server bounds submitted work and responds BUSY above 64 pending requests', async t => {
  const f = await fixture(t, {workers: 1, channelMaxPendingRequests: 80});
  const requests = Array.from({length: 65}, (_, index) => f.channel.request({
    requestId: `bounded-pending-${index}`, method: 'proc.exec',
    params: {command: 'sleep 5', timeoutMs: 10000}, timeoutMs: 12000,
  }).catch(error => error));
  const overflow = await requests[64];
  assert.equal(overflow.code, 'WORKSPACE_RPC_BUSY');
  f.channel.close();
  await Promise.all(requests);
});

workerTest('separate worker processes serialize revision CAS and reject one concurrent overwrite', async t => {
  const f = await fixture(t);
  const evidence = {commands: [], children: []};
  const launch = createWorkspaceWorkerLaunch({
    workspaceRoot: f.root, pythonExecutable: python, source: workspaceWorkerSource(),
    maxTextBytes: 1024 * 1024, workers: 2,
  });
  const second = new WorkspaceRpcChannel({
    client: localSshClient(evidence), command: launch.command,
    bootstrapPayload: launch.bootstrapPayload, readyTimeoutMs: 5000, requestTimeoutMs: 5000,
  });
  await second.ready;
  try {
    const opened = (await f.channel.request({method: 'fs.readText', params: {path: 'src/hello.txt'}})).result;
    const writes = await Promise.allSettled([
      f.channel.request({method: 'fs.writeText', params: {
        path: 'src/hello.txt', content: `worker-a-${'a'.repeat(256 * 1024)}`,
        expectedRevision: opened.revision, create: false, bom: false, lineEnding: 'lf',
      }}),
      second.request({method: 'fs.writeText', params: {
        path: 'src/hello.txt', content: `worker-b-${'b'.repeat(256 * 1024)}`,
        expectedRevision: opened.revision, create: false, bom: false, lineEnding: 'lf',
      }}),
    ]);
    assert.equal(writes.filter(value => value.status === 'fulfilled').length, 1);
    const rejected = writes.find(value => value.status === 'rejected');
    assert.equal(rejected?.reason?.code, 'WORKSPACE_FILE_CHANGED');
    const final = fs.readFileSync(path.join(f.root, 'src', 'hello.txt'), 'utf8');
    assert.ok(final.startsWith('worker-a-') || final.startsWith('worker-b-'));
  } finally {
    second.close();
    for (const child of evidence.children) if (child.exitCode === null) child.kill('SIGKILL');
  }
});

workerTest('workspace proc.exec runs concurrently with file RPC and enforces timeout and output caps', async t => {
  const f = await fixture(t);
  const slow = f.channel.request({requestId: 'slow-exec', method: 'proc.exec', params: {
    command: `${JSON.stringify(python)} -c 'import time;time.sleep(.35);print("slow")'`, timeoutMs: 2000,
  }, timeoutMs: 3000});
  const started = Date.now();
  const opened = await f.channel.request({method: 'fs.readText', params: {path: 'src/hello.txt'}});
  assert.ok(Date.now() - started < 300, 'file read is not queued behind the running process');
  assert.match((await slow).result.stdout, /slow/);

  await assert.rejects(f.channel.request({method: 'proc.exec', params: {
    command: `${JSON.stringify(python)} -c 'import time;time.sleep(2)'`, timeoutMs: 100,
  }, timeoutMs: 2000}), error => error.code === 'WORKSPACE_EXEC_TIMEOUT');
  await assert.rejects(f.channel.request({method: 'proc.exec', params: {
    command: `${JSON.stringify(python)} -c 'print("x"*10000)'`, maxOutputBytes: 1024,
  }}), error => error.code === 'WORKSPACE_EXEC_OUTPUT_LIMIT');
});

workerTest('proc.exec reports worker queue wait separately from command duration', async t => {
  const f = await fixture(t, {workers: 1});
  const started = path.join(f.root, 'queue-started');
  const first = f.channel.request({method: 'proc.exec', params: {
    command: `${JSON.stringify(python)} -c 'from pathlib import Path;import time;Path("queue-started").write_text("1");time.sleep(.3)'`,
    timeoutMs: 2000,
  }});
  await waitFor(() => fs.existsSync(started));
  const second = f.channel.request({method: 'proc.exec', params: {
    command: 'printf queued', timeoutMs: 2000,
  }});
  const result = (await second).result;
  await first;
  assert.ok(result.queueWaitMs >= 200, `expected real queue wait, received ${result.queueWaitMs} ms`);
  assert.ok(result.durationMs < result.queueWaitMs, 'command runtime must exclude its worker queue delay');
});

workerTest('closing the SSH workspace channel kills in-flight command process groups', async t => {
  const f = await fixture(t);
  const started = path.join(f.root, 'close-started');
  const lateMarker = path.join(f.root, 'must-not-appear-after-close');
  const running = f.channel.request({requestId: 'close-process-group', method: 'proc.exec', params: {
    command: `${JSON.stringify(python)} -c 'from pathlib import Path;import time;Path("close-started").write_text("1");time.sleep(.5);Path("must-not-appear-after-close").write_text("bad")'`,
    timeoutMs: 3000,
  }, timeoutMs: 4000}).catch(error => error);
  await waitFor(() => fs.existsSync(started));
  f.channel.close();
  const result = await running;
  assert.match(result.code, /WORKSPACE_RPC_CHANNEL_CLOSED|WORKSPACE_RPC_CANCELLED/);
  await delay(700);
  assert.equal(fs.existsSync(lateMarker), false);
});

workerTest('single request cancellation does not reset unrelated requests on the shared channel', async t => {
  const f = await fixture(t);
  const hanging = f.channel.request({requestId: 'cancel-me', method: 'proc.exec', params: {
    command: `${JSON.stringify(python)} -c 'import time;time.sleep(5)'`, timeoutMs: 10000,
  }, timeoutMs: 150});
  await assert.rejects(hanging, error => error.code === 'WORKSPACE_RPC_REQUEST_TIMEOUT');
  const healthy = await f.channel.request({method: 'fs.stat', params: {path: 'src/hello.txt'}});
  assert.equal(healthy.result.type, 'file');
  assert.equal(f.channel.state, 'ready');
});
