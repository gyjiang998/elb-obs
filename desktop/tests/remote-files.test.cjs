'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const {EventEmitter} = require('node:events');
const {
  DesktopRuntime,
  captureLocalDestinationFingerprint,
  replaceDownloadedFile,
} = require('../runtime/index.cjs');
const {MAX_REMOTE_TEXT_BYTES, RemoteFileError, RemoteFileService, normalizeRelativePath} = require('../runtime/remote-files.cjs');

function attrs(stat) {
  return {
    mode: stat.mode, size: stat.size, mtime: Math.floor(stat.mtimeMs / 1000),
    isFile: () => stat.isFile(), isDirectory: () => stat.isDirectory(), isSymbolicLink: () => stat.isSymbolicLink(),
  };
}

function fakeSftp(serverRoot, evidence) {
  serverRoot = fs.realpathSync(serverRoot);
  const openHandles = new Set();
  const local = remote => {
    if (!path.posix.isAbsolute(remote)) remote = path.posix.join('/workspace', remote);
    return path.join(serverRoot, ...path.posix.normalize(remote).split('/').filter(Boolean));
  };
  const remote = localPath => {
    const relative = path.relative(serverRoot, localPath);
    return relative && !relative.startsWith('..') && !path.isAbsolute(relative)
      ? `/${relative.split(path.sep).join('/')}` : `/outside/${path.basename(localPath)}`;
  };
  const callback = promise => promise.then(value => queueMicrotask(() => value.callback(null, value.result)), value => queueMicrotask(() => value.callback(value.error)));
  return Object.assign(new EventEmitter(), {
    realpath(filename, done) {
      if (evidence.hangNextRealpath > 0) { evidence.hangNextRealpath--; return; }
      if (evidence.failNextRealpathCode) {
        const code = evidence.failNextRealpathCode;
        evidence.failNextRealpathCode = '';
        return queueMicrotask(() => done(new RemoteFileError(code, 'simulated SFTP transport failure')));
      }
      callback(fsp.realpath(local(filename)).then(result => ({callback: done, result: remote(result)}), error => Promise.reject({callback: done, error})));
    },
    lstat(filename, done) { callback(fsp.lstat(local(filename)).then(result => ({callback: done, result: attrs(result)}), error => Promise.reject({callback: done, error}))); },
    stat(filename, done) { callback(fsp.stat(local(filename)).then(result => ({callback: done, result: attrs(result)}), error => Promise.reject({callback: done, error}))); },
    readdir(filename, done) {
      callback(fsp.readdir(local(filename)).then(async names => ({callback: done, result: await Promise.all(names.map(async name => ({filename: name, attrs: attrs(await fsp.lstat(path.join(local(filename), name)))})))}), error => Promise.reject({callback: done, error})));
    },
    open(filename, _flags, done) {
      evidence.reads.push(filename);
      fs.open(local(filename), 'r', (error, handle) => {
        if (!error) openHandles.add(handle);
        done(error, handle);
      });
    },
    read(handle, buffer, offset, length, position, done) {
      fs.read(handle, buffer, offset, length, position, (error, bytesRead) => done(error, bytesRead));
    },
    close(handle, done) {
      const complete = () => fs.close(handle, error => {
        if (!error) openHandles.delete(handle);
        evidence.handleCloses++;
        done(error);
      });
      if (evidence.closeDelayMs) setTimeout(complete, evidence.closeDelayMs);
      else complete();
    },
    writeFile(filename, data, options, done) {
      evidence.writes.push({filename, bytes: Buffer.byteLength(data)});
      fsp.writeFile(local(filename), data, options).then(async () => {
        if (evidence.afterWrite) await evidence.afterWrite({filename, local: local(filename)});
        done(null);
      }, done);
    },
    fastPut(localPath, remotePath, options, done) {
      evidence.uploads.push({remotePath, options: {...options}});
      fsp.copyFile(localPath, local(remotePath)).then(async () => {
        if (Number.isInteger(options?.mode)) await fsp.chmod(local(remotePath), options.mode);
        if (evidence.afterFastPut) await evidence.afterFastPut({localPath, remotePath, remote: local(remotePath)});
        done(null);
      }, done);
    },
    rename(source, target, done) {
      evidence.renames.push({source, target});
      if (evidence.renameError) return queueMicrotask(() => done(evidence.renameError));
      fsp.rename(local(source), local(target)).then(() => done(null), done);
    },
    unlink(filename, done) {
      evidence.unlinks.push(filename);
      fsp.unlink(local(filename)).then(() => done(null), done);
    },
    end() {
      evidence.channelsClosed++;
      evidence.openHandlesAtChannelEnd.push(openHandles.size);
    },
  });
}

async function fixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-remote-files-'));
  const serverRoot = path.join(directory, 'server'); const workspace = path.join(serverRoot, 'workspace');
  fs.mkdirSync(path.join(workspace, 'src'), {recursive: true});
  fs.writeFileSync(path.join(workspace, 'src', 'hello.txt'), 'hello 远程文件\n');
  fs.writeFileSync(path.join(workspace, 'src', 'bom-crlf.txt'), Buffer.concat([
    Buffer.from([0xef, 0xbb, 0xbf]), Buffer.from('first\r\nsecond\r\n', 'utf8'),
  ]));
  fs.writeFileSync(path.join(workspace, 'src', 'classic-mac.txt'), 'first\rsecond\r');
  fs.writeFileSync(path.join(workspace, 'src', 'mixed.txt'), 'first\r\nsecond\nthird\r');
  fs.writeFileSync(path.join(workspace, 'binary.dat'), Buffer.from([0, 1, 2, 3]));
  fs.writeFileSync(path.join(workspace, 'large.txt'), Buffer.alloc(MAX_REMOTE_TEXT_BYTES + 1, 0x61));
  fs.writeFileSync(path.join(serverRoot, 'secret.txt'), 'outside');
  let symlinksAvailable = true;
  try {
    fs.symlinkSync(process.platform === 'win32' ? path.join(workspace, 'src') : 'src', path.join(workspace, 'src-link'), process.platform === 'win32' ? 'junction' : undefined);
    fs.symlinkSync(process.platform === 'win32' ? path.join(serverRoot, 'secret.txt') : '../secret.txt', path.join(workspace, 'escape-link'));
    fs.symlinkSync(process.platform === 'win32' ? path.join(workspace, 'src', 'hello.txt') : 'src/hello.txt', path.join(workspace, 'file-link'));
  } catch (error) {
    if (error.code !== 'EPERM') throw error;
    symlinksAvailable = false;
    for (const name of ['src-link', 'escape-link', 'file-link']) fs.rmSync(path.join(workspace, name), {recursive: true, force: true});
  }
  const runtime = new DesktopRuntime({
    userDataDir: path.join(directory, 'prefs'), resourcesDir: path.resolve(__dirname, '..'), version: 'test',
    // This suite exercises the legacy RemoteFileService directly through its
    // runtime adapter. The production Desktop never enables SFTP write fallback.
    allowLegacySftpWriteFallback: true,
  });
  await runtime.getState();
  const evidence = {reads: [], writes: [], uploads: [], renames: [], unlinks: [], channels: [], channelsOpened: 0, channelsClosed: 0,
    handleCloses: 0, openHandlesAtChannelEnd: [], closeDelayMs: 0, hangNextRealpath: 0, failNextRealpathCode: '', afterFastPut: null};
  const createClient = ({delayMs = 0} = {}) => ({sftp: done => {
    const complete = () => {
      evidence.channelsOpened++;
      const channel = fakeSftp(serverRoot, evidence);
      evidence.channels.push(channel);
      done(null, channel);
    };
    if (delayMs) setTimeout(complete, delayMs); else complete();
  }, end() {}, destroy() {}});
  const client = createClient();
  runtime.client = client;
  runtime.ssh = {state: 'connected', shellState: 'closed', cwd: '/workspace', remoteRootPath: '/workspace'};
  t.after(async () => { await runtime.dispose(); fs.rmSync(directory, {recursive: true, force: true}); });
  return {runtime, workspace, evidence, symlinksAvailable, createClient};
}

function installNamedSshTarget(runtime, {
  host = 'old.example', username = 'developer', cwd = '/workspace', root = '/workspace',
} = {}) {
  runtime.ssh = {
    ...runtime.ssh, state: 'connected', host, port: 22, username, cwd,
    remoteRootPath: root, connectionGeneration: runtime.generation,
  };
  return {
    target: {host, port: 22, username, cwd},
    root,
    generation: runtime.generation,
  };
}

function installUploadCommitWorker(f, {beforeCommit = null} = {}) {
  let commits = 0;
  f.runtime.workspaceRequest = async ({method, params, expectedIdentity}) => {
    assert.equal(method, 'fs.rename');
    assert.equal(expectedIdentity.client, f.runtime.client);
    assert.equal(expectedIdentity.generation, f.runtime.generation);
    assert.equal(expectedIdentity.root, '/workspace');
    commits += 1;
    if (beforeCommit) await beforeCommit({params, commits});
    const source = path.join(f.workspace, ...params.path.split('/'));
    const parent = path.dirname(source);
    const destination = path.join(parent, params.newName);
    try { await fsp.link(source, destination); }
    catch (error) {
      if (error?.code === 'EEXIST') {
        const collision = new Error('目标名称已存在。');
        collision.code = 'WORKSPACE_FILE_EXISTS';
        throw collision;
      }
      throw error;
    }
    await fsp.unlink(source);
    const value = await fsp.stat(destination);
    return {
      path: path.relative(f.workspace, destination).split(path.sep).join('/'),
      previousPath: params.path,
      renamed: true,
      type: 'file',
      size: value.size,
      mtimeMs: Math.trunc(value.mtimeMs),
    };
  };
  return () => commits;
}

test('concurrent Explorer operations share one interactive SFTP initialization', async t => {
  const f = await fixture(t);
  const [root, source, opened] = await Promise.all([
    f.runtime.listRemoteFiles({path: ''}),
    f.runtime.listRemoteFiles({path: 'src'}),
    f.runtime.readRemoteFile({path: 'src/hello.txt'}),
  ]);
  assert.equal(root.path, '');
  assert.equal(source.path, 'src');
  assert.equal(opened.content, 'hello 远程文件\n');
  assert.equal(f.evidence.channelsOpened, 1, 'all callers await the same channel creation promise');
});

test('background prefetch is isolated from interactive traffic and all persistent channels close on reconnect', async t => {
  const f = await fixture(t);
  await Promise.all([
    f.runtime.readRemoteFile({path: 'src/hello.txt'}),
    f.runtime.prefetchRemoteFiles({paths: ['src'], maxDirectories: 4}),
  ]);
  assert.equal(f.evidence.channelsOpened, 2, 'interactive and background traffic have independent reusable channels');
  await f.runtime.disconnectSsh();
  assert.equal(f.evidence.channelsClosed, 2, 'disconnect closes both persistent channels');

  f.runtime.client = f.createClient();
  f.runtime.ssh = {state: 'connected', shellState: 'closed', cwd: '/workspace', remoteRootPath: '/workspace'};
  const reopened = await f.runtime.readRemoteFile({path: 'src/hello.txt'});
  assert.equal(reopened.content, 'hello 远程文件\n');
  assert.equal(f.evidence.channelsOpened, 3, 'the reconnected target creates a fresh channel');
});

test('a failed persistent SFTP channel is discarded and the next operation recreates it', async t => {
  const f = await fixture(t);
  await f.runtime.readRemoteFile({path: 'src/hello.txt'});
  assert.equal(f.evidence.channelsOpened, 1);
  f.evidence.channels[0].emit('error', new Error('simulated channel reset'));
  const reopened = await f.runtime.readRemoteFile({path: 'src/hello.txt'});
  assert.equal(reopened.content, 'hello 远程文件\n');
  assert.equal(f.evidence.channelsOpened, 2);
});

test('disconnect during SFTP initialization fences the late channel before target reuse', async t => {
  const f = await fixture(t);
  f.runtime.client = f.createClient({delayMs: 30});
  const pending = f.runtime.readRemoteFile({path: 'src/hello.txt'});
  await new Promise(resolve => setTimeout(resolve, 5));
  await f.runtime.disconnectSsh();
  await assert.rejects(pending, error => error.code === 'REMOTE_CONNECTION_CHANGED');
  assert.equal(f.evidence.channelsOpened, 1);
  assert.equal(f.evidence.channelsClosed, 1, 'a channel delivered after disconnect is closed immediately');
});

test('a late channel from the old generation cannot evict the reconnected target channel', async t => {
  const f = await fixture(t);
  f.runtime.client = f.createClient({delayMs: 30});
  const stale = f.runtime.readRemoteFile({path: 'src/hello.txt'});
  await new Promise(resolve => setTimeout(resolve, 5));
  await f.runtime.disconnectSsh();

  const currentClient = f.createClient();
  f.runtime.client = currentClient;
  f.runtime.ssh = {state: 'connected', shellState: 'closed', cwd: '/workspace', remoteRootPath: '/workspace'};
  assert.equal((await f.runtime.readRemoteFile({path: 'src/hello.txt'})).content, 'hello 远程文件\n');
  await assert.rejects(stale, error => error.code === 'REMOTE_CONNECTION_CHANGED');

  assert.equal((await f.runtime.readRemoteFile({path: 'src/hello.txt'})).content, 'hello 远程文件\n');
  assert.equal(f.evidence.channelsOpened, 2, 'the late old channel and the current channel are the only channels created');
  assert.equal(f.evidence.channelsClosed, 1, 'only the stale channel is closed');
  assert.equal(f.runtime.remoteFileConnections.get('interactive')?.client, currentClient,
    'closing the stale record must not remove the current generation record');
});

test('workspace transport failure never replays a read or write after the SSH target changes', async t => {
  const f = await fixture(t);
  const installFailingWorkspaceLane = () => {
    let rejectRequest;
    const record = {
      role: 'interactive', client: f.runtime.client, generation: f.runtime.generation,
      root: f.runtime.ssh.remoteRootPath, pythonExecutable: 'python3', state: 'ready',
      request: () => new Promise((_resolve, reject) => { rejectRequest = reject; }),
      close() { this.state = 'closed'; },
    };
    record.workspaceReady = Promise.resolve(record);
    f.runtime.remoteWorkspaceConnections.set('interactive', record);
    return error => rejectRequest(error);
  };
  const switchTarget = () => {
    f.runtime.client = f.createClient();
    f.runtime.generation += 1;
    f.runtime.ssh = {...f.runtime.ssh, state: 'connected', remoteRootPath: '/workspace'};
  };
  const transportError = () => Object.assign(new Error('simulated worker reset'), {code: 'WORKSPACE_RPC_CHANNEL_CLOSED'});
  const installOpeningWorkspaceLane = () => {
    let rejectReady;
    const record = {
      role: 'interactive', client: f.runtime.client, generation: f.runtime.generation,
      root: f.runtime.ssh.remoteRootPath, pythonExecutable: 'python3', state: 'opening',
      close() { this.state = 'closed'; },
    };
    record.workspaceReady = new Promise((_resolve, reject) => { rejectReady = reject; });
    f.runtime.remoteWorkspaceConnections.set('interactive', record);
    return () => rejectReady(Object.assign(new Error('worker failed before request send'), {
      code: 'WORKSPACE_RPC_UNAVAILABLE', workspaceTransportUnavailable: true,
    }));
  };

  let fail = installFailingWorkspaceLane();
  const read = f.runtime.readRemoteFile({path: 'src/hello.txt'});
  await new Promise(resolve => setImmediate(resolve));
  switchTarget(); fail(transportError());
  await assert.rejects(read, error => error.code === 'REMOTE_CONNECTION_CHANGED');
  assert.equal(f.evidence.channelsOpened, 0, 'read is not replayed through the new target SFTP channel');

  fail = installOpeningWorkspaceLane();
  const save = f.runtime.saveRemoteFile({path: 'src/new-target.txt', content: 'must not cross targets'});
  await new Promise(resolve => setImmediate(resolve));
  switchTarget(); fail(transportError());
  await assert.rejects(save, error => error.code === 'REMOTE_CONNECTION_CHANGED');
  assert.equal(fs.existsSync(path.join(f.workspace, 'src', 'new-target.txt')), false);
  assert.equal(f.evidence.channelsOpened, 0, 'write is not replayed through the new target SFTP channel');

  // Also fence the yield inside SFTP acquisition itself. Passing the first
  // fallback check must not authorize a later connection after target switch.
  f.runtime.client = f.createClient({delayMs: 30});
  f.runtime.generation += 1;
  fail = installOpeningWorkspaceLane();
  const lateSave = f.runtime.saveRemoteFile({path: 'src/late-target.txt', content: 'must stay absent'});
  await new Promise(resolve => setImmediate(resolve));
  fail();
  await new Promise(resolve => setTimeout(resolve, 5));
  switchTarget();
  await assert.rejects(lateSave, error => error.code === 'REMOTE_CONNECTION_CHANGED');
  assert.equal(fs.existsSync(path.join(f.workspace, 'src', 'late-target.txt')), false);
  assert.equal(f.evidence.channelsOpened, 1, 'only the stale delayed SFTP channel was delivered');
  assert.equal(f.evidence.channelsClosed, 1, 'the stale channel is closed before any operation runs');
});

test('stale renderer connection identity rejects every Explorer operation before a worker frame', async t => {
  const f = await fixture(t);
  const stale = installNamedSshTarget(f.runtime);
  f.runtime.client = f.createClient();
  f.runtime.generation += 1;
  installNamedSshTarget(f.runtime, {host: 'new.example'});
  let workspaceFrames = 0;
  f.runtime.workspaceRequest = async () => { workspaceFrames += 1; return {}; };
  const requests = [
    () => f.runtime.listRemoteFiles({path: '', expectedConnection: stale}),
    () => f.runtime.prefetchRemoteFiles({paths: ['src'], expectedConnection: stale}),
    () => f.runtime.readRemoteFile({path: 'src/hello.txt', expectedConnection: stale}),
    () => f.runtime.saveRemoteFile({path: 'src/hello.txt', content: 'stale', expectedConnection: stale}),
    () => f.runtime.renameRemoteFile({path: 'src/hello.txt', newName: 'stale.txt', expectedConnection: stale}),
    () => f.runtime.copyRemoteFile({path: 'src/hello.txt', destinationPath: 'src/stale.txt', expectedConnection: stale}),
  ];
  for (const request of requests)
    await assert.rejects(request(), error => error.code === 'REMOTE_CONNECTION_CHANGED');
  assert.equal(workspaceFrames, 0, 'a stale renderer target must not acquire or write a worker frame');
  assert.equal(f.evidence.channelsOpened, 0, 'a stale renderer target must not fall back to SFTP');
});

test('renderer generation fence rejects an ABA reconnect to the same logical SSH target', async t => {
  const f = await fixture(t);
  const stale = installNamedSshTarget(f.runtime);
  f.runtime.client = f.createClient();
  f.runtime.generation += 1;
  installNamedSshTarget(f.runtime);
  let workspaceFrames = 0;
  f.runtime.workspaceRequest = async () => { workspaceFrames += 1; return {}; };
  await assert.rejects(
    f.runtime.readRemoteFile({path: 'src/hello.txt', expectedConnection: stale}),
    error => error.code === 'REMOTE_CONNECTION_CHANGED',
  );
  assert.equal(workspaceFrames, 0);
});

test('Explorer connection expectation is consumed by Runtime and never forwarded to the worker', async t => {
  const f = await fixture(t);
  const expectedConnection = installNamedSshTarget(f.runtime);
  const frames = [];
  const installLane = role => {
    const record = {
      role, client: f.runtime.client, generation: f.runtime.generation,
      root: f.runtime.ssh.remoteRootPath, pythonExecutable: 'python3', state: 'ready',
      request: async frame => {
        frames.push(frame);
        return {result: frame.method === 'fs.prefetch'
          ? {directories: [], failures: [], stats: {}} : {}, channelReused: true, durationMs: 1};
      },
      close() { this.state = 'closed'; },
    };
    record.workspaceReady = Promise.resolve(record);
    f.runtime.remoteWorkspaceConnections.set(role, record);
  };
  installLane('interactive');
  installLane('prefetch');
  await f.runtime.listRemoteFiles({path: '', expectedConnection});
  await f.runtime.prefetchRemoteFiles({paths: ['src'], expectedConnection});
  await f.runtime.readRemoteFile({path: 'src/hello.txt', expectedConnection});
  await f.runtime.saveRemoteFile({path: 'src/hello.txt', content: 'safe', expectedConnection});
  await f.runtime.renameRemoteFile({path: 'src/hello.txt', newName: 'safe.txt', expectedConnection});
  await f.runtime.copyRemoteFile({path: 'src/hello.txt', destinationPath: 'src/safe.txt', expectedConnection});
  assert.equal(frames.length, 6);
  for (const frame of frames) {
    assert.equal(Object.hasOwn(frame.params, 'expectedConnection'), false);
    assert.equal(Object.hasOwn(frame, 'expectedIdentity'), false,
      'private client/generation/root identity stays in Runtime and off the worker wire');
  }
});

test('an ambiguous worker write failure is never replayed through SFTP', async t => {
  const f = await fixture(t);
  const record = {
    role: 'interactive', client: f.runtime.client, generation: f.runtime.generation,
    root: f.runtime.ssh.remoteRootPath, pythonExecutable: 'python3', state: 'ready',
    request: async () => { throw Object.assign(new Error('response lost after send'), {code: 'WORKSPACE_RPC_CHANNEL_CLOSED'}); },
    close() { this.state = 'closed'; },
  };
  record.workspaceReady = Promise.resolve(record);
  f.runtime.remoteWorkspaceConnections.set('interactive', record);
  await assert.rejects(f.runtime.saveRemoteFile({path: 'src/ambiguous.txt', content: 'do not replay'}),
    error => {
      assert.equal(error.code, 'WORKSPACE_RPC_CHANNEL_CLOSED');
      assert.equal(error.commitUnknown, true);
      return true;
    });
  assert.equal(f.evidence.channelsOpened, 0);
  assert.equal(f.evidence.writes.length, 0);
  assert.equal(fs.existsSync(path.join(f.workspace, 'src', 'ambiguous.txt')), false);
});

test('a write response discarded after an SSH generation change is reported commit-unknown', async t => {
  const f = await fixture(t);
  const expectedConnection = installNamedSshTarget(f.runtime);
  const record = {
    role: 'interactive', client: f.runtime.client, generation: f.runtime.generation,
    root: f.runtime.ssh.remoteRootPath, pythonExecutable: 'python3', state: 'ready',
    request: async () => {
      f.runtime.generation += 1;
      f.runtime.ssh.connectionGeneration = f.runtime.generation;
      return {result: {revision: `sha256:${'a'.repeat(64)}`}, channelReused: true, durationMs: 1};
    },
    close() { this.state = 'closed'; },
  };
  record.workspaceReady = Promise.resolve(record);
  f.runtime.remoteWorkspaceConnections.set('interactive', record);

  await assert.rejects(f.runtime.saveRemoteFile({
    path: 'src/hello.txt', content: 'write may already have committed', expectedConnection,
  }), error => {
    assert.equal(error.code, 'REMOTE_CONNECTION_CHANGED');
    assert.equal(error.commitUnknown, true);
    return true;
  });
  assert.equal(f.evidence.channelsOpened, 0, 'the uncertain write is never replayed through SFTP');
});

test('bulk replace reports the current file separately when its remote commit is unknown', async t => {
  const f = await fixture(t);
  const expectedConnection = installNamedSshTarget(f.runtime);
  const content = 'hello remote file\n';
  const fileRevision = `sha256:${crypto.createHash('sha256').update(content).digest('hex')}`;
  f.runtime.readRemoteFile = async ({path: requestedPath}) => ({
    path: requestedPath,
    content,
    revision: fileRevision,
    bom: false,
    lineEnding: 'lf',
  });
  f.runtime.saveRemoteFile = async () => {
    const error = new RemoteFileError('WORKSPACE_RPC_REQUEST_TIMEOUT', 'response lost after write dispatch');
    error.commitUnknown = true;
    throw error;
  };
  await assert.rejects(f.runtime.replaceRemoteFiles({
    query: 'hello',
    replacement: 'updated',
    expectedConnection,
    files: [{path: 'src/hello.txt', revision: fileRevision}],
  }), error => {
    assert.equal(error.commitUnknown, true);
    assert.deepEqual(error.details?.saved, []);
    assert.equal(error.details?.commitUnknown?.path, 'src/hello.txt');
    assert.match(error.message, /提交状态未知/);
    assert.match(error.message, /原目标/);
    return true;
  });
});

test('production runtime refuses SFTP write fallback when the Workspace Worker is unavailable', async t => {
  const f = await fixture(t);
  f.runtime.allowLegacySftpWriteFallback = false;
  f.runtime.workspaceRequest = async () => {
    throw Object.assign(new Error('worker failed before request send'), {
      code: 'WORKSPACE_RPC_UNAVAILABLE', workspaceTransportUnavailable: true,
    });
  };
  await assert.rejects(
    f.runtime.saveRemoteFile({path: 'src/no-fallback.txt', content: 'must not land'}),
    error => error.code === 'REMOTE_WORKSPACE_WRITE_UNAVAILABLE',
  );
  assert.equal(f.evidence.channelsOpened, 0);
  assert.equal(fs.existsSync(path.join(f.workspace, 'src', 'no-fallback.txt')), false);
});

test('an operation timeout retires the persistent channel so the next request can recover', async t => {
  const f = await fixture(t);
  await f.runtime.withRemoteFiles(service => service.list({path: 'src'}), {operationTimeoutMs: 15});
  assert.equal(f.evidence.channelsOpened, 1);

  f.evidence.hangNextRealpath = 1;
  await assert.rejects(
    f.runtime.withRemoteFiles(service => service.list({path: 'src'}), {operationTimeoutMs: 15}),
    error => error.code === 'REMOTE_SFTP_TIMEOUT',
  );
  assert.equal(f.evidence.channelsClosed, 1, 'the timed-out persistent channel is not reused');

  const recovered = await f.runtime.withRemoteFiles(service => service.list({path: 'src'}), {operationTimeoutMs: 15});
  assert.equal(recovered.path, 'src');
  assert.equal(f.evidence.channelsOpened, 2, 'the next request creates a healthy replacement channel');
});

test('a transport failure reported inside partial prefetch results retires only its background channel', async t => {
  const f = await fixture(t);
  await Promise.all([
    f.runtime.readRemoteFile({path: 'src/hello.txt'}),
    f.runtime.prefetchRemoteFiles({paths: ['src'], maxDirectories: 4}),
  ]);
  assert.equal(f.evidence.channelsOpened, 2);

  f.evidence.failNextRealpathCode = 'REMOTE_SFTP_TIMEOUT';
  const partial = await f.runtime.prefetchRemoteFiles({paths: ['src'], maxDirectories: 4});
  assert.equal(partial.stats.failedDirectories, 1, 'prefetch still returns its bounded partial result');
  assert.equal(partial.failures[0].code, 'REMOTE_SFTP_TIMEOUT');
  assert.equal(f.evidence.channelsClosed, 1, 'only the failed prefetch channel is retired');

  assert.equal((await f.runtime.readRemoteFile({path: 'src/hello.txt'})).content, 'hello 远程文件\n');
  assert.equal(f.evidence.channelsOpened, 2, 'interactive traffic keeps using its healthy channel');
  await f.runtime.prefetchRemoteFiles({paths: ['src'], maxDirectories: 4});
  assert.equal(f.evidence.channelsOpened, 3, 'the next background crawl gets a replacement channel');
});

test('remote file contract lists lazily, reads UTF-8 and saves with revision fencing', async t => {
  const f = await fixture(t);
  const root = await f.runtime.listRemoteFiles({path: ''});
  assert.deepEqual(Object.keys(root).sort(), [
    'channelReused', 'entries', 'parentPath', 'path', 'transport', 'transportDurationMs', 'truncated',
  ]);
  assert.equal(root.transport, 'sftp-fallback');
  assert.equal(root.channelReused, false);
  assert.equal(root.path, ''); assert.equal(root.parentPath, null); assert.equal(root.truncated, false);
  assert.equal(root.entries.find(entry => entry.name === 'src').type, 'directory');
  if (f.symlinksAvailable) {
    const sourceLink = root.entries.find(entry => entry.name === 'src-link');
    assert.equal(sourceLink.path, 'src-link'); assert.equal(sourceLink.type, 'directory');
    assert.equal(sourceLink.symlink, true); assert.equal(sourceLink.accessible, true);
    assert.equal(root.entries.find(entry => entry.name === 'escape-link').accessible, false);
  }

  const nested = await f.runtime.listRemoteFiles({path: 'src'});
  assert.equal(nested.transport, 'sftp-fallback');
  assert.equal(nested.channelReused, true);
  assert.equal(nested.parentPath, '');
  assert.deepEqual(nested.entries.map(entry => entry.path), [
    'src/bom-crlf.txt', 'src/classic-mac.txt', 'src/hello.txt', 'src/mixed.txt',
  ]);
  const opened = await f.runtime.readRemoteFile({path: 'src/hello.txt'});
  assert.equal(opened.content, 'hello 远程文件\n'); assert.equal(opened.encoding, 'utf8'); assert.match(opened.revision, /^sha256:[0-9a-f]{64}$/);
  assert.equal(typeof opened.size, 'number'); assert.equal(typeof opened.modifiedAt, 'string');

  const saved = await f.runtime.saveRemoteFile({path: 'src/hello.txt', content: 'edited\n', expectedRevision: opened.revision});
  assert.equal(saved.saved, true); assert.equal(saved.path, 'src/hello.txt'); assert.equal(saved.size, 7);
  assert.equal(fs.readFileSync(path.join(f.workspace, 'src', 'hello.txt'), 'utf8'), 'edited\n');
  assert.equal(f.evidence.renames.length, 1);
  assert.match(f.evidence.renames[0].source, /\.hetero-[0-9a-f-]+\.tmp$/);
  await assert.rejects(f.runtime.saveRemoteFile({path: 'src/hello.txt', content: 'stale', expectedRevision: opened.revision}), error => error.code === 'REMOTE_FILE_CHANGED');

  const created = await f.runtime.saveRemoteFile({path: 'src/new.txt', content: 'new file'});
  assert.equal(created.saved, true); assert.equal(fs.readFileSync(path.join(f.workspace, 'src/new.txt'), 'utf8'), 'new file');
  await assert.rejects(f.runtime.saveRemoteFile({path: 'src/hello.txt', content: 'unsafe overwrite'}),
    error => error.code === 'REMOTE_FILE_CHANGED');
  assert.equal(fs.readFileSync(path.join(f.workspace, 'src/hello.txt'), 'utf8'), 'edited\n');
  assert.equal(f.evidence.channelsOpened, 1, 'interactive directory, read and save operations reuse one SFTP channel');
  assert.equal(f.evidence.channelsClosed, 0, 'the interactive SFTP channel remains warm while SSH stays connected');
  await f.runtime.disconnectSsh();
  assert.equal(f.evidence.channelsClosed, 1, 'disconnect closes the persistent SFTP channel');
  assert(f.evidence.openHandlesAtChannelEnd.every(value => value === 0), 'every remote file handle must close before its SFTP channel');
});

test('remote tree prefetch uses one bounded SFTP session and never descends through symlinks or heavy caches', async t => {
  const f = await fixture(t);
  fs.mkdirSync(path.join(f.workspace, 'src', 'feature', 'nested'), {recursive: true});
  fs.writeFileSync(path.join(f.workspace, 'src', 'feature', 'nested', 'work.txt'), 'work');
  fs.mkdirSync(path.join(f.workspace, 'node_modules', 'large-package'), {recursive: true});
  fs.writeFileSync(path.join(f.workspace, 'node_modules', 'large-package', 'index.js'), 'ignored');

  const channelsBefore = f.evidence.channelsOpened;
  const result = await f.runtime.prefetchRemoteFiles({
    paths: ['src', 'node_modules', ...(f.symlinksAvailable ? ['src-link'] : [])],
    maxDepth: 8,
    maxDirectories: 20,
    maxEntries: 100,
    concurrency: 3,
    timeBudgetMs: 2000,
  });
  const prefetched = result.directories.map(directory => directory.path);
  assert.deepEqual(prefetched, ['src', 'src/feature', 'src/feature/nested']);
  assert.equal(result.cancelled, false);
  assert.equal(result.stats.directoryCount, 3);
  assert.equal(result.stats.failedDirectories, 0);
  if (f.symlinksAvailable) assert.ok(result.stats.skippedDirectories >= 1, 'symlink seeds are skipped');
  assert.equal(f.evidence.channelsOpened - channelsBefore, 1, 'the whole traversal opens one reusable background SFTP channel');
  await f.runtime.prefetchRemoteFiles({paths: ['src'], maxDirectories: 2});
  assert.equal(f.evidence.channelsOpened - channelsBefore, 1, 'later background traversals reuse the same SFTP channel');
});

test('remote tree prefetch is bounded and observes cancellation without failing the file browser', async t => {
  const f = await fixture(t);
  for (let index = 0; index < 8; index++) {
    fs.mkdirSync(path.join(f.workspace, 'src', `folder-${index}`));
    fs.writeFileSync(path.join(f.workspace, 'src', `folder-${index}`, 'value.txt'), String(index));
  }
  const sftp = fakeSftp(path.dirname(f.workspace), f.evidence);
  const service = await RemoteFileService.create(sftp, '/workspace');
  let checks = 0;
  const cancelled = await service.prefetchTree({paths: ['src'], maxDirectories: 50}, {
    shouldContinue: () => ++checks < 2,
  });
  sftp.end();
  assert.equal(cancelled.cancelled, true);
  assert.equal(cancelled.reason, 'cancelled');

  const limited = await f.runtime.prefetchRemoteFiles({
    paths: ['src'], maxDepth: 8, maxDirectories: 2, maxEntries: 100, concurrency: 1,
  });
  assert.ok(limited.directories.length <= 2);
  assert.equal(limited.truncated, true);
  assert.equal(limited.reason, 'directory_limit');
});

test('remote tree prefetch enforces depth, entry and time budgets', async t => {
  const f = await fixture(t);
  fs.mkdirSync(path.join(f.workspace, 'src', 'level-2', 'level-3'), {recursive: true});
  fs.writeFileSync(path.join(f.workspace, 'src', 'level-2', 'level-3', 'leaf.txt'), 'leaf');

  const depthLimited = await f.runtime.prefetchRemoteFiles({
    paths: ['src'], maxDepth: 1, maxDirectories: 20, maxEntries: 100, concurrency: 1,
  });
  assert.deepEqual(depthLimited.directories.map(directory => directory.path), ['src']);

  const entryLimited = await f.runtime.prefetchRemoteFiles({
    paths: ['src'], maxDepth: 8, maxDirectories: 20, maxEntries: 1, concurrency: 1,
  });
  assert.equal(entryLimited.reason, 'entry_limit');
  assert.equal(entryLimited.truncated, true);
  assert.equal(entryLimited.directories.length, 0, 'an incomplete directory is not published as a complete cache entry');

  const sftp = fakeSftp(path.dirname(f.workspace), f.evidence);
  const service = await RemoteFileService.create(sftp, '/workspace');
  const originalList = service.list.bind(service);
  service.list = async value => {
    await new Promise(resolve => setTimeout(resolve, 10));
    return originalList(value);
  };
  const timed = await service.prefetchTree({
    paths: ['src'], maxDepth: 8, maxDirectories: 20, maxEntries: 100, concurrency: 1, timeBudgetMs: 1,
  });
  sftp.end();
  assert.equal(timed.reason, 'time_budget');
  assert.equal(timed.truncated, true);
  assert.ok(timed.stats.durationMs >= 1);
});

test('delayed remote CLOSE acknowledgement completes before the SFTP channel ends', async t => {
  const f = await fixture(t);
  f.evidence.closeDelayMs = 40;
  const opened = await f.runtime.readRemoteFile({path: 'src/hello.txt'});
  assert.equal(opened.content, 'hello 远程文件\n');
  assert.equal(f.evidence.handleCloses, 1);
  await f.runtime.disconnectSsh();
  assert.deepEqual(f.evidence.openHandlesAtChannelEnd, [0]);
});

test('remote text editing preserves UTF-8 BOM and uniform line endings', async t => {
  const f = await fixture(t);
  const crlf = await f.runtime.readRemoteFile({path: 'src/bom-crlf.txt'});
  assert.equal(crlf.content, 'first\nsecond\n');
  assert.equal(crlf.bom, true);
  assert.equal(crlf.lineEnding, 'crlf');
  const saved = await f.runtime.saveRemoteFile({
    path: crlf.path,
    content: 'changed\nsecond\n',
    expectedRevision: crlf.revision,
    bom: crlf.bom,
    lineEnding: crlf.lineEnding,
  });
  assert.equal(saved.bom, true);
  assert.equal(saved.lineEnding, 'crlf');
  assert.deepEqual(fs.readFileSync(path.join(f.workspace, 'src', 'bom-crlf.txt')),
    Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), Buffer.from('changed\r\nsecond\r\n')]));

  const classicMac = await f.runtime.readRemoteFile({path: 'src/classic-mac.txt'});
  assert.equal(classicMac.content, 'first\nsecond\n');
  assert.equal(classicMac.bom, false);
  assert.equal(classicMac.lineEnding, 'cr');
  await f.runtime.saveRemoteFile({...classicMac, content: 'one\ntwo\n', expectedRevision: classicMac.revision});
  assert.equal(fs.readFileSync(path.join(f.workspace, 'src', 'classic-mac.txt'), 'utf8'), 'one\rtwo\r');
});

test('mixed line endings are identified and require explicit normalization before save', async t => {
  const f = await fixture(t);
  const mixed = await f.runtime.readRemoteFile({path: 'src/mixed.txt'});
  assert.equal(mixed.content, 'first\nsecond\nthird\n');
  assert.equal(mixed.lineEnding, 'mixed');
  await assert.rejects(f.runtime.saveRemoteFile({
    path: mixed.path,
    content: mixed.content,
    expectedRevision: mixed.revision,
    bom: mixed.bom,
    lineEnding: mixed.lineEnding,
  }), error => error.code === 'REMOTE_LINE_ENDING_MIXED');
  assert.equal(fs.readFileSync(path.join(f.workspace, 'src', 'mixed.txt'), 'utf8'), 'first\r\nsecond\nthird\r');
});

test('atomic save rechecks revision and removes the temporary file on conflicts', async t => {
  const f = await fixture(t);
  const opened = await f.runtime.readRemoteFile({path: 'src/hello.txt'});
  let mutated = false;
  f.evidence.afterWrite = async ({filename}) => {
    if (!mutated && /\.hetero-/.test(filename)) {
      mutated = true;
      await fsp.writeFile(path.join(f.workspace, 'src', 'hello.txt'), 'concurrent edit\n');
    }
  };
  await assert.rejects(f.runtime.saveRemoteFile({
    path: opened.path, content: 'my edit\n', expectedRevision: opened.revision,
  }), error => error.code === 'REMOTE_FILE_CHANGED');
  assert.equal(fs.readFileSync(path.join(f.workspace, 'src', 'hello.txt'), 'utf8'), 'concurrent edit\n');
  assert.deepEqual(fs.readdirSync(path.join(f.workspace, 'src')).filter(name => name.startsWith('.hetero-')), []);
  assert.equal(f.evidence.renames.length, 0);
  assert.equal(f.evidence.unlinks.length, 1);
});

test('atomic rename failure leaves the original intact and cleans up', async t => {
  const f = await fixture(t);
  const opened = await f.runtime.readRemoteFile({path: 'src/hello.txt'});
  f.evidence.renameError = Object.assign(new Error('rename denied'), {code: 'EACCES'});
  await assert.rejects(f.runtime.saveRemoteFile({
    path: opened.path, content: 'replacement\n', expectedRevision: opened.revision,
  }), error => error.code === 'REMOTE_ATOMIC_RENAME_FAILED');
  assert.equal(fs.readFileSync(path.join(f.workspace, 'src', 'hello.txt'), 'utf8'), 'hello 远程文件\n');
  assert.deepEqual(fs.readdirSync(path.join(f.workspace, 'src')).filter(name => name.startsWith('.hetero-')), []);
});

test('remote paths cannot traverse cwd or escape through symbolic links', async t => {
  const f = await fixture(t);
  for (const value of ['../secret.txt', '/secret.txt', 'src\\hello.txt'])
    await assert.rejects(f.runtime.readRemoteFile({path: value}), error => error.code === 'REMOTE_PATH_OUTSIDE_ROOT');
  if (f.symlinksAvailable) {
    await assert.rejects(f.runtime.readRemoteFile({path: 'escape-link'}), error => error.code === 'REMOTE_SYMLINK_OUTSIDE_ROOT');
    await assert.rejects(f.runtime.listRemoteFiles({path: 'escape-link'}), error => error.code === 'REMOTE_SYMLINK_OUTSIDE_ROOT');
    await assert.rejects(f.runtime.saveRemoteFile({path: 'file-link', content: 'overwrite'}), error => error.code === 'REMOTE_SYMLINK_WRITE_BLOCKED');
  }
  assert.equal(fs.readFileSync(path.join(f.workspace, 'src', 'hello.txt'), 'utf8'), 'hello 远程文件\n');
});

test('binary, oversized and disconnected remote file errors are explicit', async t => {
  const f = await fixture(t);
  await assert.rejects(f.runtime.readRemoteFile({path: 'binary.dat'}), error => error.code === 'REMOTE_BINARY_FILE' && /二进制/.test(error.message));
  await assert.rejects(f.runtime.readRemoteFile({path: 'large.txt'}), error => error.code === 'REMOTE_FILE_TOO_LARGE' && /1 MiB/.test(error.message));
  await assert.rejects(f.runtime.saveRemoteFile({path: 'src/too-large.txt', content: 'a'.repeat(MAX_REMOTE_TEXT_BYTES + 1)}), error => error.code === 'REMOTE_FILE_TOO_LARGE');
  await f.runtime.disconnectSsh();
  await assert.rejects(f.runtime.listRemoteFiles({path: ''}), error => error.code === 'REMOTE_NOT_CONNECTED' && /请先连接 SSH 服务器/.test(error.message));
});

test('SFTP operations fail with a bounded timeout', async () => {
  const hung = {realpath() {}};
  const startedAt = Date.now();
  await assert.rejects(RemoteFileService.create(hung, '/workspace', {operationTimeoutMs: 15}),
    error => error.code === 'REMOTE_SFTP_TIMEOUT' && /realpath/.test(error.message));
  assert.ok(Date.now() - startedAt < 500, 'timeout should not leave the operation hanging');
});

test('remote path normalization keeps only cwd-relative POSIX paths', () => {
  assert.equal(normalizeRelativePath('./src//file.txt'), 'src/file.txt');
  assert.throws(() => normalizeRelativePath('src/../secret'), error => error.code === 'REMOTE_PATH_OUTSIDE_ROOT');
  assert.throws(() => normalizeRelativePath('/etc/passwd'), error => error.code === 'REMOTE_PATH_OUTSIDE_ROOT');
});

test('remote download streams files larger than one RPC frame and commits after snapshot verification', async t => {
  const f = await fixture(t);
  const bytes = Buffer.alloc(6 * 1024 * 1024 + 93);
  for (let index = 0; index < bytes.length; index += 32771) bytes[index] = index % 241;
  const snapshot = `snapshot:${'a'.repeat(64)}`;
  const approvedRevision = `sha256:${'f'.repeat(64)}`;
  const requests = [];
  f.runtime.workspaceRequest = async ({method, params}) => {
    assert.equal(method, 'fs.readChunk');
    requests.push(params);
    if (params.expectedSnapshot) assert.equal(params.expectedSnapshot, snapshot);
    const chunk = bytes.subarray(params.offset, Math.min(bytes.length, params.offset + params.length));
    return {
      path: params.path, offset: params.offset, bytesRead: chunk.length,
      contentBase64: chunk.toString('base64'), eof: params.offset + chunk.length >= bytes.length,
      snapshot, size: bytes.length, mtimeMs: 1,
    };
  };
  const destination = path.join(path.dirname(f.workspace), 'downloaded.bin');
  fs.writeFileSync(destination, 'previous download');
  const result = await f.runtime.downloadRemoteFileToLocal({
    path: 'trace/large.trace', destinationPath: destination,
    expectedSize: bytes.length, expectedMtimeMs: 1, expectedRevision: approvedRevision,
  });
  assert.equal(result.size, bytes.length);
  assert.equal(result.sha256, crypto.createHash('sha256').update(bytes).digest('hex'));
  assert.equal(fs.readFileSync(destination).equals(bytes), true);
  assert.ok(requests.length > 10, 'large files must be transferred in bounded chunks');
  assert.equal(requests.at(-1).offset, bytes.length, 'the final request revalidates the stable snapshot');
  assert.equal(requests.at(-1).length, 1);
  assert.equal(requests[0].expectedSize, bytes.length);
  assert.equal(requests[0].expectedMtimeMs, 1);
  assert.equal(requests[0].expectedRevision, approvedRevision);
  assert.equal('expectedSize' in requests[1], false, 'remote identity is checked only on the first chunk');
  assert.equal('expectedMtimeMs' in requests[1], false);
  assert.equal('expectedRevision' in requests[1], false);
  assert.deepEqual(fs.readdirSync(path.dirname(destination)).filter(name => name.includes('.hetero-download-')), []);
});

test('target switch while the Save dialog is open is rejected before download chunk zero', async t => {
  const f = await fixture(t);
  const expectedConnection = installNamedSshTarget(f.runtime);
  await f.runtime.validateRemoteFileConnection(expectedConnection); // Main: before showSaveDialog.

  // Simulate the native dialog remaining open while the user reconnects. The
  // same host/root is intentional: only the connection generation catches ABA.
  f.runtime.client = f.createClient();
  f.runtime.generation += 1;
  installNamedSshTarget(f.runtime);
  let workspaceFrames = 0;
  f.runtime.workspaceRequest = async () => { workspaceFrames += 1; return {}; };
  const destination = path.join(path.dirname(f.workspace), 'dialog-race.trace');

  await assert.rejects(
    f.runtime.validateRemoteFileConnection(expectedConnection),
    error => error.code === 'REMOTE_CONNECTION_CHANGED',
  );
  await assert.rejects(
    f.runtime.downloadRemoteFileToLocal({
      path: 'trace/dialog-race.trace', destinationPath: destination, expectedConnection,
    }),
    error => error.code === 'REMOTE_CONNECTION_CHANGED',
  );
  assert.equal(workspaceFrames, 0, 'no fs.readChunk frame is sent after a dialog-time target switch');
  assert.equal(fs.existsSync(destination), false, 'no local partial or destination is created');
});

test('download commit rejects a destination edited after the user-approved fingerprint', async t => {
  const f = await fixture(t);
  const destination = path.join(path.dirname(f.workspace), 'changed-while-downloading.trace');
  fs.writeFileSync(destination, 'approved content');
  const expectedDestinationFingerprint = await captureLocalDestinationFingerprint(destination);
  const bytes = Buffer.from('new remote bytes');
  let calls = 0;
  f.runtime.workspaceRequest = async ({params}) => {
    calls += 1;
    if (calls === 1) fs.writeFileSync(destination, 'new local content from another application');
    return {
      path: params.path, offset: params.offset,
      bytesRead: params.offset ? 0 : bytes.length,
      contentBase64: params.offset ? '' : bytes.toString('base64'),
      eof: true, snapshot: `snapshot:${'d'.repeat(64)}`, size: bytes.length, mtimeMs: 1,
    };
  };
  await assert.rejects(
    f.runtime.downloadRemoteFileToLocal({
      path: 'trace/current.trace', destinationPath: destination, expectedDestinationFingerprint,
    }),
    error => error.code === 'REMOTE_LOCAL_DESTINATION_CHANGED',
  );
  assert.equal(fs.readFileSync(destination, 'utf8'), 'new local content from another application');
  assert.deepEqual(fs.readdirSync(path.dirname(destination)).filter(name => name.includes('.hetero-download-')), []);
});

test('download commit does not overwrite a target created after an approved nonexistent state', async t => {
  const f = await fixture(t);
  const destination = path.join(path.dirname(f.workspace), 'created-while-downloading.trace');
  const expectedDestinationFingerprint = await captureLocalDestinationFingerprint(destination);
  assert.deepEqual(expectedDestinationFingerprint, {exists: false});
  const bytes = Buffer.from('remote bytes');
  let calls = 0;
  f.runtime.workspaceRequest = async ({params}) => {
    calls += 1;
    if (calls === 1) fs.writeFileSync(destination, 'new local file');
    return {
      path: params.path, offset: params.offset,
      bytesRead: params.offset ? 0 : bytes.length,
      contentBase64: params.offset ? '' : bytes.toString('base64'),
      eof: true, snapshot: `snapshot:${'e'.repeat(64)}`, size: bytes.length, mtimeMs: 1,
    };
  };
  await assert.rejects(
    f.runtime.downloadRemoteFileToLocal({
      path: 'trace/current.trace', destinationPath: destination, expectedDestinationFingerprint,
    }),
    error => error.code === 'REMOTE_LOCAL_DESTINATION_CHANGED',
  );
  assert.equal(fs.readFileSync(destination, 'utf8'), 'new local file');
});

test('fingerprinted Windows-style replacement retries a transient move and commits safely', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-download-fingerprint-windows-'));
  t.after(() => fs.rmSync(directory, {recursive: true, force: true}));
  const temporary = path.join(directory, '.artifact.trace.part');
  const destination = path.join(directory, 'artifact.trace');
  fs.writeFileSync(temporary, 'new trace');
  fs.writeFileSync(destination, 'old trace');
  const expectedDestinationFingerprint = await captureLocalDestinationFingerprint(destination);
  let moveAttempts = 0;
  const fileSystem = {
    lstat: (...args) => fsp.lstat(...args),
    link: (...args) => fsp.link(...args),
    unlink: (...args) => fsp.unlink(...args),
    rename: async (source, target) => {
      if (source === destination && target.includes('backup') && moveAttempts++ === 0)
        throw Object.assign(new Error('simulated Windows indexer hold'), {code: 'EPERM'});
      return fsp.rename(source, target);
    },
  };
  const result = await replaceDownloadedFile(temporary, destination, {
    fileSystem, expectedDestinationFingerprint, randomUUID: () => 'fingerprint-windows',
  });
  assert.equal(result.replacedExisting, true);
  assert.equal(moveAttempts, 2);
  assert.equal(fs.readFileSync(destination, 'utf8'), 'new trace');
  assert.deepEqual(fs.readdirSync(directory).filter(name => name.includes('backup')), []);
});

test('fingerprinted replacement preserves the existing file when publish fails', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-download-fingerprint-rollback-'));
  t.after(() => fs.rmSync(directory, {recursive: true, force: true}));
  const temporary = path.join(directory, '.artifact.trace.part');
  const destination = path.join(directory, 'artifact.trace');
  fs.writeFileSync(temporary, 'new trace');
  fs.writeFileSync(destination, 'old trace');
  const expectedDestinationFingerprint = await captureLocalDestinationFingerprint(destination);
  const fileSystem = {
    lstat: (...args) => fsp.lstat(...args),
    unlink: (...args) => fsp.unlink(...args),
    rename: (...args) => fsp.rename(...args),
    link: async () => { throw Object.assign(new Error('simulated publish failure'), {code: 'EIO'}); },
  };
  await assert.rejects(
    replaceDownloadedFile(temporary, destination, {
      fileSystem, expectedDestinationFingerprint, randomUUID: () => 'fingerprint-rollback',
    }),
    error => error.code === 'EIO',
  );
  assert.equal(fs.readFileSync(destination, 'utf8'), 'old trace');
  assert.equal(fs.readFileSync(temporary, 'utf8'), 'new trace');
  assert.deepEqual(fs.readdirSync(directory).filter(name => name.includes('backup')), []);
});

test('Windows-style destination collision safely replaces an existing download', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-download-replace-'));
  t.after(() => fs.rmSync(directory, {recursive: true, force: true}));
  const temporary = path.join(directory, '.artifact.trace.part');
  const destination = path.join(directory, 'artifact.trace');
  fs.writeFileSync(temporary, 'new trace');
  fs.writeFileSync(destination, 'old trace');
  let directCommit = true;
  const fileSystem = {
    lstat: (...args) => fsp.lstat(...args),
    unlink: (...args) => fsp.unlink(...args),
    rename: async (source, target) => {
      if (source === temporary && target === destination && directCommit) {
        directCommit = false;
        throw Object.assign(new Error('simulated Windows destination collision'), {code: 'EPERM'});
      }
      return fsp.rename(source, target);
    },
  };
  const result = await replaceDownloadedFile(temporary, destination, {
    fileSystem, randomUUID: () => 'test-overwrite',
  });
  assert.equal(result.replacedExisting, true);
  assert.equal(fs.readFileSync(destination, 'utf8'), 'new trace');
  assert.equal(fs.existsSync(temporary), false);
  assert.deepEqual(fs.readdirSync(directory).filter(name => name.includes('backup')), []);
});

test('Windows-style replacement failure restores the existing destination', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-download-rollback-'));
  t.after(() => fs.rmSync(directory, {recursive: true, force: true}));
  const temporary = path.join(directory, '.artifact.trace.part');
  const destination = path.join(directory, 'artifact.trace');
  fs.writeFileSync(temporary, 'new trace');
  fs.writeFileSync(destination, 'old trace');
  let commitAttempts = 0;
  const fileSystem = {
    lstat: (...args) => fsp.lstat(...args),
    unlink: (...args) => fsp.unlink(...args),
    rename: async (source, target) => {
      if (source === temporary && target === destination) {
        commitAttempts += 1;
        throw Object.assign(new Error(commitAttempts === 1 ? 'simulated Windows collision' : 'simulated commit failure'), {
          code: commitAttempts === 1 ? 'EPERM' : 'EIO',
        });
      }
      return fsp.rename(source, target);
    },
  };
  await assert.rejects(
    replaceDownloadedFile(temporary, destination, {fileSystem, randomUUID: () => 'test-rollback'}),
    error => error.code === 'EIO',
  );
  assert.equal(fs.readFileSync(destination, 'utf8'), 'old trace');
  assert.equal(fs.readFileSync(temporary, 'utf8'), 'new trace');
  assert.deepEqual(fs.readdirSync(directory).filter(name => name.includes('backup')), []);
});

test('remote download failure preserves an existing destination and removes its partial file', async t => {
  const f = await fixture(t);
  const destination = path.join(path.dirname(f.workspace), 'existing.trace');
  fs.writeFileSync(destination, 'existing');
  let calls = 0;
  f.runtime.workspaceRequest = async ({params}) => {
    calls += 1;
    if (calls > 1) throw Object.assign(new Error('remote file changed'), {code: 'WORKSPACE_FILE_CHANGED'});
    const chunk = Buffer.alloc(512 * 1024, 7);
    return {
      path: params.path, offset: 0, bytesRead: chunk.length, contentBase64: chunk.toString('base64'),
      eof: false, snapshot: `snapshot:${'b'.repeat(64)}`, size: chunk.length * 2, mtimeMs: 1,
    };
  };
  await assert.rejects(
    f.runtime.downloadRemoteFileToLocal({path: 'trace/changing.trace', destinationPath: destination}),
    error => error.code === 'REMOTE_FILE_CHANGED',
  );
  assert.equal(fs.readFileSync(destination, 'utf8'), 'existing');
  assert.deepEqual(fs.readdirSync(path.dirname(destination)).filter(name => name.includes('.hetero-download-')), []);
});

test('remote download freezes the SSH target across every chunk', async t => {
  const f = await fixture(t);
  const destination = path.join(path.dirname(f.workspace), 'target-fenced.trace');
  fs.writeFileSync(destination, 'existing');
  let calls = 0;
  f.runtime.workspaceRequest = async ({params}) => {
    calls += 1;
    const chunk = Buffer.from('old target bytes');
    f.runtime.client = f.createClient();
    f.runtime.generation += 1;
    return {
      path: params.path, offset: 0, bytesRead: chunk.length,
      contentBase64: chunk.toString('base64'), eof: true,
      snapshot: `snapshot:${'c'.repeat(64)}`, size: chunk.length, mtimeMs: 1,
    };
  };
  await assert.rejects(
    f.runtime.downloadRemoteFileToLocal({path: 'trace/target.trace', destinationPath: destination}),
    error => error.code === 'REMOTE_CONNECTION_CHANGED',
  );
  assert.equal(calls, 1, 'the download does not issue another chunk on the new target');
  assert.equal(fs.readFileSync(destination, 'utf8'), 'existing');
  assert.deepEqual(fs.readdirSync(path.dirname(destination)).filter(name => name.includes('.hetero-download-')), []);
});

test('remote download refuses a newly acquired target before sending a chunk', async t => {
  const f = await fixture(t);
  const destination = path.join(path.dirname(f.workspace), 'acquire-fenced.trace');
  let requestCalls = 0;
  f.runtime.acquireRemoteWorkspaceConnection = async () => {
    const newClient = f.createClient();
    f.runtime.client = newClient;
    f.runtime.generation += 1;
    return {
      client: newClient,
      generation: f.runtime.generation,
      root: f.runtime.ssh.remoteRootPath,
      request: async () => { requestCalls += 1; return {result: {}}; },
    };
  };
  await assert.rejects(
    f.runtime.downloadRemoteFileToLocal({path: 'trace/new-target.trace', destinationPath: destination}),
    error => error.code === 'REMOTE_CONNECTION_CHANGED',
  );
  assert.equal(requestCalls, 0, 'no read frame is sent after channel acquisition crosses targets');
  assert.equal(fs.existsSync(destination), false);
  assert.deepEqual(fs.readdirSync(path.dirname(destination)).filter(name => name.includes('.hetero-download-')), []);
});

test('remote upload streams binary files on one persistent upload lane and never overwrites names', async t => {
  const f = await fixture(t);
  const expectedConnection = installNamedSshTarget(f.runtime);
  const commitCount = installUploadCommitWorker(f);
  const localRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-upload-source-'));
  t.after(() => fs.rmSync(localRoot, {recursive: true, force: true}));
  const binary = path.join(localRoot, 'payload.bin');
  const text = path.join(localRoot, 'notes.txt');
  const collision = path.join(localRoot, 'hello.txt');
  const binaryBytes = crypto.randomBytes(2 * 1024 * 1024 + 137);
  fs.writeFileSync(binary, binaryBytes);
  fs.writeFileSync(text, 'upload text\n');
  fs.writeFileSync(collision, 'must not overwrite\n');

  const result = await f.runtime.uploadRemoteFilesFromLocal({
    localPaths: [binary, text, collision],
    directory: 'src',
    expectedConnection,
  });
  assert.equal(result.directory, 'src');
  assert.equal(result.totalFiles, 3);
  assert.deepEqual(result.uploaded.map(item => item.name), ['payload.bin', 'notes.txt']);
  assert.deepEqual(result.uploaded.map(item => item.path), ['src/payload.bin', 'src/notes.txt']);
  assert.equal(result.failures.length, 1);
  assert.equal(result.failures[0].name, 'hello.txt');
  assert.equal(result.failures[0].code, 'REMOTE_FILE_EXISTS');
  assert.equal(fs.readFileSync(path.join(f.workspace, 'src', 'payload.bin')).equals(binaryBytes), true);
  assert.equal(fs.readFileSync(path.join(f.workspace, 'src', 'notes.txt'), 'utf8'), 'upload text\n');
  assert.equal(fs.readFileSync(path.join(f.workspace, 'src', 'hello.txt'), 'utf8'), 'hello 远程文件\n');
  assert.equal(commitCount(), 2);
  assert.equal(f.evidence.channelsOpened, 1, 'all files reuse the dedicated upload SFTP lane');
  assert.equal(f.evidence.uploads.length, 2);
  assert.ok(f.evidence.uploads.every(item => /\/\.hetero-upload-[0-9a-f-]{36}\.part$/.test(item.remotePath)));
  assert.deepEqual(fs.readdirSync(path.join(f.workspace, 'src')).filter(name => name.startsWith('.hetero-upload-')), []);
  assert.doesNotMatch(JSON.stringify(result), new RegExp(localRoot.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
});

test('remote upload detects a changed local source, cleans the hidden partial and does not publish it', async t => {
  const f = await fixture(t);
  const expectedConnection = installNamedSshTarget(f.runtime);
  const commitCount = installUploadCommitWorker(f);
  const localRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-upload-changing-'));
  t.after(() => fs.rmSync(localRoot, {recursive: true, force: true}));
  const changing = path.join(localRoot, 'changing.bin');
  fs.writeFileSync(changing, crypto.randomBytes(512 * 1024));
  f.evidence.afterFastPut = async ({localPath}) => {
    await fsp.appendFile(localPath, Buffer.from([0xff]));
    f.evidence.afterFastPut = null;
  };

  const result = await f.runtime.uploadRemoteFilesFromLocal({
    localPaths: [changing], directory: 'src', expectedConnection,
  });
  assert.equal(result.uploaded.length, 0);
  assert.equal(result.failures.length, 1);
  assert.equal(result.failures[0].code, 'REMOTE_UPLOAD_LOCAL_CHANGED');
  assert.equal(commitCount(), 0);
  assert.equal(fs.existsSync(path.join(f.workspace, 'src', 'changing.bin')), false);
  assert.deepEqual(fs.readdirSync(path.join(f.workspace, 'src')).filter(name => name.startsWith('.hetero-upload-')), []);
  assert.doesNotMatch(JSON.stringify(result), new RegExp(localRoot.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
});

test('remote upload loses a no-clobber publish race without replacing the remote file', async t => {
  const f = await fixture(t);
  const expectedConnection = installNamedSshTarget(f.runtime);
  const localRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-upload-race-'));
  t.after(() => fs.rmSync(localRoot, {recursive: true, force: true}));
  const source = path.join(localRoot, 'race.txt');
  fs.writeFileSync(source, 'uploaded value\n');
  installUploadCommitWorker(f, {beforeCommit: async () => {
    await fsp.writeFile(path.join(f.workspace, 'src', 'race.txt'), 'concurrent value\n');
  }});

  const result = await f.runtime.uploadRemoteFilesFromLocal({
    localPaths: [source], directory: 'src', expectedConnection,
  });
  assert.equal(result.uploaded.length, 0);
  assert.equal(result.failures[0].code, 'REMOTE_FILE_EXISTS');
  assert.equal(fs.readFileSync(path.join(f.workspace, 'src', 'race.txt'), 'utf8'), 'concurrent value\n');
  assert.deepEqual(fs.readdirSync(path.join(f.workspace, 'src')).filter(name => name.startsWith('.hetero-upload-')), []);
});

test('remote upload rejects a stale Explorer target before opening the upload lane', async t => {
  const f = await fixture(t);
  const expectedConnection = installNamedSshTarget(f.runtime);
  const localRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-upload-stale-'));
  t.after(() => fs.rmSync(localRoot, {recursive: true, force: true}));
  const source = path.join(localRoot, 'stale.txt');
  fs.writeFileSync(source, 'stale\n');
  const stale = {...expectedConnection, generation: expectedConnection.generation + 1};
  await assert.rejects(
    f.runtime.uploadRemoteFilesFromLocal({localPaths: [source], directory: 'src', expectedConnection: stale}),
    error => error.code === 'REMOTE_CONNECTION_CHANGED',
  );
  assert.equal(f.evidence.channelsOpened, 0);
  assert.equal(f.evidence.uploads.length, 0);
  assert.equal(fs.existsSync(path.join(f.workspace, 'src', 'stale.txt')), false);
});

test('remote upload rejects local non-files and remote symlink directories', async t => {
  const f = await fixture(t);
  const expectedConnection = installNamedSshTarget(f.runtime);
  installUploadCommitWorker(f);
  const localRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-upload-validation-'));
  t.after(() => fs.rmSync(localRoot, {recursive: true, force: true}));

  const localDirectoryResult = await f.runtime.uploadRemoteFilesFromLocal({
    localPaths: [localRoot], directory: 'src', expectedConnection,
  });
  assert.equal(localDirectoryResult.uploaded.length, 0);
  assert.equal(localDirectoryResult.failures[0].code, 'REMOTE_UPLOAD_LOCAL_NOT_FILE');

  if (f.symlinksAvailable) {
    const source = path.join(localRoot, 'blocked.txt');
    fs.writeFileSync(source, 'must not follow remote directory links\n');
    const linkedDirectoryResult = await f.runtime.uploadRemoteFilesFromLocal({
      localPaths: [source], directory: 'src-link', expectedConnection,
    });
    assert.equal(linkedDirectoryResult.uploaded.length, 0);
    assert.equal(linkedDirectoryResult.failures[0].code, 'REMOTE_SYMLINK_WRITE_BLOCKED');
    assert.equal(fs.existsSync(path.join(f.workspace, 'src', 'blocked.txt')), false);
  }
  assert.equal(f.evidence.uploads.length, 0);
});

test('desktop IPC allowlist exposes remote file and background prefetch contracts', () => {
  const source = fs.readFileSync(path.resolve(__dirname, '../main.cjs'), 'utf8');
  for (const method of [
    'listRemoteFiles', 'prefetchRemoteFiles', 'readRemoteFile', 'saveRemoteFile',
    'renameRemoteFile', 'copyRemoteFile', 'downloadRemoteFile',
  ]) assert.match(source, new RegExp(`['"]${method}['"]`));
  assert.match(source, /fencedRemoteExplorerMethods\.has\(method\)/);
  assert.match(source, /payload\?\.expectedConnection/);
});

test('download IPC asks for a local destination before reading and does not disclose that path to the renderer', () => {
  const source = fs.readFileSync(path.resolve(__dirname, '../main.cjs'), 'utf8');
  const branch = source.slice(source.indexOf("if (method === 'downloadRemoteFile')"), source.indexOf("if (method === 'choosePath')"));
  const validations = [...branch.matchAll(/runtime\.validateRemoteFileConnection\(expectedConnection\)/g)]
    .map(match => match.index);
  const dialogIndex = branch.indexOf('dialog.showSaveDialog');
  const transferIndex = branch.indexOf('runtime.downloadRemoteFileToLocal');
  assert.equal(validations.length, 2);
  assert.ok(validations[0] < dialogIndex, 'stale renderer identities are rejected before opening SaveDialog');
  assert.ok(dialogIndex < validations[1] && validations[1] < transferIndex,
    'a dialog-time connection change is rejected before reading remote chunk zero');
  assert.ok(branch.indexOf('captureLocalDestinationFingerprint') < branch.indexOf('runtime.downloadRemoteFileToLocal'));
  assert.match(branch, /expectedConnection,/);
  assert.match(branch, /expectedDestinationFingerprint/);
  assert.match(branch, /return \{canceled: false, size: remote\.size, sha256: remote\.sha256\}/);
  assert.doesNotMatch(branch, /return \{[^}]*path:/);
});

test('remote Explorer exposes safe Markdown preview and VS Code-style file actions', () => {
  const renderer = fs.readFileSync(path.resolve(__dirname, '../renderer/remote-files.js'), 'utf8');
  const html = fs.readFileSync(path.resolve(__dirname, '../renderer/index.html'), 'utf8');
  const main = fs.readFileSync(path.resolve(__dirname, '../main.cjs'), 'utf8');
  assert.match(renderer, /createRemoteMarkdownPreview/);
  assert.match(renderer, /isMarkdownPath/);
  assert.match(renderer, /downloadSnapshot/);
  assert.match(renderer, /expectedRevision/);
  assert.match(renderer, /remoteConnectionExpectation/);
  assert.match(renderer, /connectionGeneration/);
  assert.match(renderer,
    /const sourceInvoke = \(remoteMethod, localMethod, payload = \{\}\) => invoke\([\s\S]{0,180}\.\.\.connectionFence\(\)/,
    'shared list, read and save dispatch must carry the active source fence');
  assert.ok((renderer.match(/connectionFence\(\)/g) || []).length >= 5,
    'prefetch, source dispatch, rename, copy and download carry the visible workspace fence');
  for (const label of ['重命名', '复制文件路径', '复制', '下载', '预览'])
    assert.match(renderer, new RegExp(`['"]${label}['"]`));
  assert.match(html, /id="remote-markdown-preview"/);
  assert.match(html, /id="remote-markdown-toggle"/);
  assert.match(main, /target\.protocol === 'https:' \|\| target\.protocol === 'http:'/);
  assert.match(main, /shell\.openExternal\(target\.href\)/,
    'safe Markdown web links should open in the operating-system browser');
});
