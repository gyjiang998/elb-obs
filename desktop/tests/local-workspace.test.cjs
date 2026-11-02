'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const {LocalWorkspaceRegistry} = require('../runtime/local-workspace.cjs');

function fixture(t) {
  const container = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-local-workspace-'));
  const root = path.join(container, 'root');
  const outside = path.join(container, 'outside');
  fs.mkdirSync(path.join(root, 'src'), {recursive: true});
  fs.mkdirSync(outside);
  fs.writeFileSync(path.join(root, 'src', 'hello.txt'), 'first needle\nsecond\n');
  fs.writeFileSync(path.join(root, 'src', 'other.txt'), 'another NEEDLE\n');
  fs.writeFileSync(path.join(root, 'binary.bin'), Buffer.from([0, 1, 2]));
  fs.writeFileSync(path.join(outside, 'secret.txt'), 'outside');
  fs.writeFileSync(path.join(outside, 'hello.txt'), 'outside must stay unchanged\n');
  try { fs.symlinkSync(path.join(outside, 'secret.txt'), path.join(root, 'outside-link')); } catch {}
  t.after(() => fs.rmSync(container, {recursive: true, force: true}));
  return {root, outside};
}

function swapDirectoryForSymlink(t, root, directoryName, outside) {
  const directory = path.join(root, directoryName);
  const held = path.join(root, `${directoryName}-held`);
  fs.renameSync(directory, held);
  try { fs.symlinkSync(outside, directory, process.platform === 'win32' ? 'junction' : 'dir'); }
  catch (error) { fs.renameSync(held, directory); throw error; }
  let restored = false;
  const restore = () => {
    if (restored) return;
    restored = true;
    try { fs.unlinkSync(directory); } catch {}
    try { fs.renameSync(held, directory); } catch {}
  };
  t.after(restore);
  return restore;
}

function supportsDirectorySymlink(root, outside) {
  const probe = path.join(root, `.symlink-probe-${Date.now()}`);
  try {
    fs.symlinkSync(outside, probe, process.platform === 'win32' ? 'junction' : 'dir');
    fs.unlinkSync(probe);
    return true;
  } catch {
    try { fs.unlinkSync(probe); } catch {}
    return false;
  }
}

function injectOneDirectoryAba(registry, t, root, outside, directoryName = 'src') {
  const openDirectory = registry.openDirectory.bind(registry);
  const target = path.resolve(fs.realpathSync(root), directoryName);
  let injected = false;
  registry.openDirectory = async filename => {
    if (injected || path.resolve(filename) !== target) return openDirectory(filename);
    injected = true;
    const restore = swapDirectoryForSymlink(t, root, directoryName, outside);
    const directory = await openDirectory(filename);
    return {
      async *[Symbol.asyncIterator]() {
        try { for await (const entry of directory) yield entry; }
        finally { restore(); }
      },
      async close() {
        restore();
        await directory.close().catch(() => {});
      },
    };
  };
}

test('local workspace grant fences paths per renderer owner and blocks outside symlinks', async t => {
  const f = fixture(t);
  const registry = new LocalWorkspaceRegistry();
  t.after(() => registry.revokeOwner(11));
  const grant = await registry.authorize(11, f.root, {source: 'selected'});
  const listed = await registry.list(11, {grantId: grant.grantId, path: ''});
  assert.ok(listed.entries.some(entry => entry.path === 'src' && entry.type === 'directory'));
  const link = listed.entries.find(entry => entry.path === 'outside-link');
  if (link) assert.equal(link.accessible, false);
  await assert.rejects(registry.read(11, {grantId: grant.grantId, path: '../outside/secret.txt'}),
    error => error.code === 'LOCAL_WORKSPACE_PATH_OUTSIDE_ROOT');
  await assert.rejects(registry.read(12, {grantId: grant.grantId, path: 'src/hello.txt'}),
    error => error.code === 'LOCAL_WORKSPACE_GRANT_INVALID');
});

test('local workspace edit uses revision CAS and preserves text format', async t => {
  const f = fixture(t);
  const registry = new LocalWorkspaceRegistry();
  t.after(() => registry.revokeOwner('window-a'));
  const grant = await registry.authorize('window-a', f.root);
  const opened = await registry.read('window-a', {grantId: grant.grantId, path: 'src/hello.txt'});
  assert.match(opened.revision, /^sha256:[0-9a-f]{64}$/);
  const saved = await registry.save('window-a', {grantId: grant.grantId, path: 'src/hello.txt',
    content: 'changed\nvalue\n', expectedRevision: opened.revision, bom: true, lineEnding: 'crlf'});
  assert.equal(saved.saved, true);
  assert.deepEqual(fs.readFileSync(path.join(f.root, 'src', 'hello.txt')).subarray(0, 3), Buffer.from([0xef, 0xbb, 0xbf]));
  assert.match(fs.readFileSync(path.join(f.root, 'src', 'hello.txt'), 'utf8'), /changed\r\nvalue\r\n/);
  await assert.rejects(registry.save('window-a', {grantId: grant.grantId, path: 'src/hello.txt',
    content: 'stale', expectedRevision: opened.revision, bom: false, lineEnding: 'lf'}),
  error => error.code === 'LOCAL_WORKSPACE_FILE_CHANGED');
});

test('Windows directory-handle fallback keeps local authorization and file handles usable', async t => {
  const f = fixture(t);
  const registry = new LocalWorkspaceRegistry();
  t.after(() => registry.revokeOwner('windows-directory-window'));
  const openPathHandle = registry.openPathHandle.bind(registry);
  registry.openPathHandle = async (filename, flags, mode) => {
    if (fs.statSync(filename).isDirectory())
      throw Object.assign(new Error('simulated libuv directory open limitation'), {code: 'EISDIR'});
    return openPathHandle(filename, flags, mode);
  };
  const grant = await registry.authorize('windows-directory-window', f.root);
  const listed = await registry.list('windows-directory-window', {grantId: grant.grantId, path: 'src'});
  assert.ok(listed.entries.some(entry => entry.path === 'src/hello.txt'));
  const opened = await registry.read('windows-directory-window', {grantId: grant.grantId, path: 'src/hello.txt'});
  const saved = await registry.save('windows-directory-window', {
    grantId: grant.grantId,
    path: 'src/hello.txt',
    content: 'works without directory FileHandle\n',
    expectedRevision: opened.revision,
  });
  assert.equal(saved.saved, true);
  assert.equal(fs.readFileSync(path.join(f.root, 'src', 'hello.txt'), 'utf8'), 'works without directory FileHandle\n');
});

test('directory snapshots reject a junction swapped in only during enumeration', async t => {
  const f = fixture(t);
  if (!supportsDirectorySymlink(f.root, f.outside)) { t.skip('directory symlinks require additional privileges'); return; }
  fs.writeFileSync(path.join(f.outside, 'outside-only.txt'), 'must never be listed or searched\n');
  const registry = new LocalWorkspaceRegistry();
  t.after(() => registry.revokeOwner('directory-aba-window'));
  const openPathHandle = registry.openPathHandle.bind(registry);
  registry.openPathHandle = async (filename, flags, mode) => {
    if (fs.statSync(filename).isDirectory())
      throw Object.assign(new Error('simulated Windows directory handle limitation'), {code: 'EISDIR'});
    return openPathHandle(filename, flags, mode);
  };
  const grant = await registry.authorize('directory-aba-window', f.root);
  injectOneDirectoryAba(registry, t, f.root, f.outside);
  await assert.rejects(registry.list('directory-aba-window', {
    grantId: grant.grantId,
    path: 'src',
  }), error => error.code === 'LOCAL_WORKSPACE_DIRECTORY_CHANGED');

  injectOneDirectoryAba(registry, t, f.root, f.outside);
  await assert.rejects(registry.search('directory-aba-window', {
    grantId: grant.grantId,
    requestId: 'search-directory-aba',
    path: 'src',
    query: 'must never be listed',
  }), error => error.code === 'LOCAL_WORKSPACE_DIRECTORY_CHANGED');
});

test('search never opens a child from an unverified first directory snapshot', async t => {
  const f = fixture(t);
  const registry = new LocalWorkspaceRegistry();
  t.after(() => registry.revokeOwner('unverified-search-window'));
  const grant = await registry.authorize('unverified-search-window', f.root);
  const takeDirectorySnapshot = registry.takeDirectorySnapshot.bind(registry);
  const outsideAttributes = fs.statSync(path.join(f.outside, 'secret.txt'), {bigint: true});
  let snapshotPass = 0;
  registry.takeDirectorySnapshot = async (...args) => {
    snapshotPass += 1;
    if (snapshotPass === 1) return {
      entries: [{
        name: 'outside-only.txt', type: 'file', symlink: false,
        identity: {device: String(outsideAttributes.dev), inode: String(outsideAttributes.ino)},
        attributes: outsideAttributes,
      }],
      overflow: false,
      stopped: null,
    };
    return takeDirectorySnapshot(...args);
  };
  const openVerified = registry.openVerified.bind(registry);
  let unverifiedChildOpens = 0;
  registry.openVerified = async (targetGrant, requestedPath, options) => {
    if (requestedPath === 'src/outside-only.txt') unverifiedChildOpens += 1;
    return openVerified(targetGrant, requestedPath, options);
  };
  await assert.rejects(registry.search('unverified-search-window', {
    grantId: grant.grantId,
    requestId: 'search-unverified-snapshot',
    path: 'src',
    query: 'outside',
  }), error => error.code === 'LOCAL_WORKSPACE_DIRECTORY_CHANGED');
  assert.equal(unverifiedChildOpens, 0);
});

test('authorization closes a root handle when the renderer is revoked while authorize is pending', async t => {
  const f = fixture(t);
  const registry = new LocalWorkspaceRegistry();
  const rootIdentity = fs.statSync(fs.realpathSync(f.root), {bigint: true});
  let release;
  let enteredResolve;
  const gate = new Promise(resolve => { release = resolve; });
  const entered = new Promise(resolve => { enteredResolve = resolve; });
  let closeCount = 0;
  registry.openPathHandle = async () => {
    enteredResolve();
    await gate;
    return {
      stat: async () => rootIdentity,
      close: async () => { closeCount += 1; },
    };
  };
  const pending = registry.authorize('revoked-during-authorize', f.root);
  await entered;
  await registry.revokeOwner('revoked-during-authorize');
  release();
  await assert.rejects(pending, error => error.code === 'LOCAL_WORKSPACE_GRANT_INVALID');
  assert.equal(closeCount, 1);
  assert.equal(registry.grants.has('revoked-during-authorize'), false);
});

test('verified file handles block a parent-directory symlink swap during read', async t => {
  const f = fixture(t);
  if (!supportsDirectorySymlink(f.root, f.outside)) { t.skip('directory symlinks require additional privileges'); return; }
  const registry = new LocalWorkspaceRegistry();
  t.after(() => registry.revokeOwner('read-swap-window'));
  const grant = await registry.authorize('read-swap-window', f.root);
  const revalidateOpenPath = registry.revalidateOpenPath.bind(registry);
  let attempted = false;
  registry.revalidateOpenPath = async (targetGrant, opened) => {
    if (!attempted && opened.relative === 'src/hello.txt') {
      attempted = true;
      swapDirectoryForSymlink(t, f.root, 'src', f.outside);
    }
    return revalidateOpenPath(targetGrant, opened);
  };
  await assert.rejects(registry.read('read-swap-window', {
    grantId: grant.grantId,
    path: 'src/hello.txt',
  }), error => ['LOCAL_WORKSPACE_PATH_OUTSIDE_ROOT', 'LOCAL_WORKSPACE_PATH_CHANGED'].includes(error.code));
  assert.equal(fs.readFileSync(path.join(f.outside, 'hello.txt'), 'utf8'), 'outside must stay unchanged\n');
});

test('verified writable handles block a parent-directory symlink swap during save', async t => {
  const f = fixture(t);
  if (!supportsDirectorySymlink(f.root, f.outside)) { t.skip('directory symlinks require additional privileges'); return; }
  const registry = new LocalWorkspaceRegistry();
  t.after(() => registry.revokeOwner('save-swap-window'));
  const grant = await registry.authorize('save-swap-window', f.root);
  const opened = await registry.read('save-swap-window', {grantId: grant.grantId, path: 'src/hello.txt'});
  const writeOpenedBytes = registry.writeOpenedBytes.bind(registry);
  let attempted = false;
  let restoreDirectory;
  registry.writeOpenedBytes = async (handle, bytes) => {
    if (!attempted) {
      attempted = true;
      restoreDirectory = swapDirectoryForSymlink(t, f.root, 'src', f.outside);
    }
    return writeOpenedBytes(handle, bytes);
  };
  await assert.rejects(registry.save('save-swap-window', {
    grantId: grant.grantId,
    path: 'src/hello.txt',
    content: 'must not escape\n',
    expectedRevision: opened.revision,
  }), error => ['LOCAL_WORKSPACE_PATH_OUTSIDE_ROOT', 'LOCAL_WORKSPACE_PATH_CHANGED'].includes(error.code));
  assert.equal(fs.readFileSync(path.join(f.outside, 'hello.txt'), 'utf8'), 'outside must stay unchanged\n');
  restoreDirectory();
  assert.equal(fs.readFileSync(path.join(f.root, 'src', 'hello.txt'), 'utf8'), opened.content);
});

test('local search is bounded, skips binary files and returns replace-safe snapshots', async t => {
  const f = fixture(t);
  const registry = new LocalWorkspaceRegistry();
  t.after(() => registry.revokeOwner(7));
  const grant = await registry.authorize(7, f.root);
  const result = await registry.search(7, {grantId: grant.grantId, requestId: 'search-1', query: 'needle',
    caseSensitive: false, maxResults: 20});
  assert.deepEqual(result.matches.map(match => match.path).sort(), ['src/hello.txt', 'src/other.txt']);
  assert.ok(result.matches.every(match => /^sha256:[0-9a-f]{64}$/.test(match.revision)));
  assert.ok(result.stats.skippedFiles >= 1);
  await assert.rejects(registry.search(7, {grantId: grant.grantId, requestId: 'search-regex',
    query: '(a+)+$', regex: true}), error => error.code === 'LOCAL_WORKSPACE_SEARCH_INVALID');
});

test('local search cancellation is registered before asynchronous root resolution', async t => {
  const f = fixture(t);
  const registry = new LocalWorkspaceRegistry();
  t.after(() => registry.revokeOwner('cancel-window'));
  const grant = await registry.authorize('cancel-window', f.root);
  const resolveExisting = registry.resolveExisting.bind(registry);
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  registry.resolveExisting = async (...args) => {
    await gate;
    return resolveExisting(...args);
  };
  const pending = registry.search('cancel-window', {
    grantId: grant.grantId,
    requestId: 'search-cancel-before-root',
    query: 'needle',
  });
  assert.equal(registry.cancelSearch('cancel-window', 'search-cancel-before-root').cancelled, true);
  release();
  const result = await pending;
  assert.equal(result.reason, 'cancelled');
  assert.equal(result.truncated, true);
  assert.deepEqual(result.matches, []);
});

test('local search bounds wide-directory enumeration and queued entries', async t => {
  const f = fixture(t);
  const wide = path.join(f.root, 'wide');
  fs.mkdirSync(wide);
  for (let index = 0; index < 40; index += 1)
    fs.writeFileSync(path.join(wide, `entry-${String(index).padStart(2, '0')}.txt`), 'bounded search\n');
  const registry = new LocalWorkspaceRegistry();
  t.after(() => registry.revokeOwner('wide-window'));
  const grant = await registry.authorize('wide-window', f.root);
  const queued = await registry.search('wide-window', {
    grantId: grant.grantId,
    requestId: 'search-wide-queue',
    path: 'wide',
    query: 'absent',
    maxQueueEntries: 8,
    maxDirectoryEntries: 100,
  });
  assert.equal(queued.reason, 'queue_limit');
  assert.equal(queued.truncated, true);
  assert.ok(queued.stats.discoveredEntries <= 9);
  const takeDirectorySnapshot = registry.takeDirectorySnapshot.bind(registry);
  let snapshotPasses = 0;
  registry.takeDirectorySnapshot = async (...args) => {
    snapshotPasses += 1;
    return takeDirectorySnapshot(...args);
  };
  const directory = await registry.search('wide-window', {
    grantId: grant.grantId,
    requestId: 'search-wide-directory',
    path: 'wide',
    query: 'absent',
    maxQueueEntries: 100,
    maxDirectoryEntries: 5,
  });
  assert.equal(directory.reason, 'directory_entry_limit');
  assert.equal(directory.truncated, true);
  assert.ok(directory.stats.discoveredEntries <= 6);
  assert.equal(snapshotPasses, 1, 'an overflow must not compare arbitrary prefixes from a second directory pass');
});

test('local replace deduplicates files and rejects a file changed after search', async t => {
  const f = fixture(t);
  const registry = new LocalWorkspaceRegistry();
  t.after(() => registry.revokeOwner(5));
  const grant = await registry.authorize(5, f.root);
  const result = await registry.search(5, {grantId: grant.grantId, requestId: 'search-replace', query: 'needle'});
  const snapshots = result.matches.map(match => ({path: match.path, revision: match.revision}));
  const replaced = await registry.replace(5, {grantId: grant.grantId, query: 'needle', replacement: 'marker',
    caseSensitive: false, files: [...snapshots, snapshots[0]]});
  assert.equal(replaced.replacedFiles, 2);
  assert.equal(replaced.atomic, false);
  assert.match(fs.readFileSync(path.join(f.root, 'src', 'hello.txt'), 'utf8'), /marker/);
  const staleSearch = await registry.search(5, {grantId: grant.grantId, requestId: 'search-stale', query: 'marker'});
  fs.appendFileSync(path.join(f.root, 'src', 'hello.txt'), 'changed after search\n');
  await assert.rejects(registry.replace(5, {grantId: grant.grantId, query: 'marker', replacement: 'again',
    files: staleSearch.matches.map(match => ({path: match.path, revision: match.revision}))}),
  error => error.code === 'LOCAL_WORKSPACE_FILE_CHANGED');
});

test('local multi-file replace reports committed files when a later CAS save fails', async t => {
  const f = fixture(t);
  const registry = new LocalWorkspaceRegistry();
  t.after(() => registry.revokeOwner('partial-window'));
  const grant = await registry.authorize('partial-window', f.root);
  const result = await registry.search('partial-window', {
    grantId: grant.grantId,
    requestId: 'search-partial',
    query: 'needle',
    caseSensitive: false,
  });
  const snapshots = result.matches.map(match => ({path: match.path, revision: match.revision}));
  const save = registry.save.bind(registry);
  let saveCount = 0;
  registry.save = async (...args) => {
    saveCount += 1;
    if (saveCount === 2) {
      const error = new Error('simulated concurrent write');
      error.code = 'LOCAL_WORKSPACE_FILE_CHANGED';
      throw error;
    }
    return save(...args);
  };
  await assert.rejects(registry.replace('partial-window', {
    grantId: grant.grantId,
    query: 'needle',
    replacement: 'marker',
    caseSensitive: false,
    files: snapshots,
  }), error => {
    assert.equal(error.code, 'LOCAL_WORKSPACE_FILE_CHANGED');
    assert.match(error.message, /已替换 1 个文件/);
    assert.match(error.message, /不会自动回退/);
    assert.equal(error.details?.saved?.length, 1);
    return true;
  });
});

test('Windows-style replace collisions are avoided by handle CAS and partial writes roll back', async t => {
  const f = fixture(t);
  const registry = new LocalWorkspaceRegistry();
  t.after(() => registry.revokeOwner('windows-window'));
  const grant = await registry.authorize('windows-window', f.root);
  const pathValue = 'src/hello.txt';
  const opened = await registry.read('windows-window', {grantId: grant.grantId, path: pathValue});
  let pathnameRenameCalled = false;
  registry.renameTemporary = async () => {
    pathnameRenameCalled = true;
    throw Object.assign(new Error('simulated Windows destination collision'), {code: 'EPERM'});
  };
  const writeOpenedBytes = registry.writeOpenedBytes.bind(registry);
  let transientAttempts = 0;
  registry.writeOpenedBytes = async (handle, bytes) => {
    transientAttempts += 1;
    if (transientAttempts === 1) {
      await handle.truncate(0);
      throw Object.assign(new Error('simulated Windows scanner hold'), {code: 'EPERM'});
    }
    return writeOpenedBytes(handle, bytes);
  };
  const saved = await registry.save('windows-window', {
    grantId: grant.grantId,
    path: pathValue,
    content: 'committed through verified handle\n',
    expectedRevision: opened.revision,
  });
  assert.equal(saved.saved, true);
  assert.equal(pathnameRenameCalled, false, 'save must not depend on replace-rename of a mutable parent path');
  assert.equal(transientAttempts, 2, 'a transient handle failure should restore bytes before one bounded retry');
  assert.equal(fs.readFileSync(path.join(f.root, 'src', 'hello.txt'), 'utf8'), 'committed through verified handle\n');
  assert.deepEqual(fs.readdirSync(path.join(f.root, 'src')).filter(name => name.startsWith('.hetero-workspace-')), []);

  const beforeFailure = await registry.read('windows-window', {grantId: grant.grantId, path: pathValue});
  registry.writeOpenedBytes = async (handle, bytes) => {
    await handle.truncate(0);
    await handle.write(bytes.subarray(0, Math.min(4, bytes.length)), 0, Math.min(4, bytes.length), 0);
    throw Object.assign(new Error('simulated interrupted Windows write'), {code: 'EIO'});
  };
  await assert.rejects(registry.save('windows-window', {
    grantId: grant.grantId,
    path: pathValue,
    content: 'must be rolled back\n',
    expectedRevision: beforeFailure.revision,
  }), error => error.code === 'EIO');
  assert.equal(fs.readFileSync(path.join(f.root, 'src', 'hello.txt'), 'utf8'), beforeFailure.content);
  assert.deepEqual(fs.readdirSync(path.join(f.root, 'src')).filter(name => name.startsWith('.hetero-workspace-')), []);
});

