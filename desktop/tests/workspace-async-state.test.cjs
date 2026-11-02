'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
  WorkspaceReadTokens,
  WorkspaceSearchSession,
  sameWorkspaceBinding,
} = require('../renderer/workspace-async-state.cjs');

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((accept, decline) => {
    resolve = accept;
    reject = decline;
  });
  return {promise, resolve, reject};
}

function remoteBinding({query = '', generation = 1, host = 'server-a'} = {}) {
  return {
    query,
    caseSensitive: false,
    source: 'remote',
    identity: `remote:developer@${host}:22:/workspace`,
    connection: {
      target: {host, port: 22, username: 'developer', cwd: '/workspace'},
      root: '/workspace',
      generation,
    },
    grantId: '',
  };
}

function localBinding({query = '', grantId = 'grant-a'} = {}) {
  return {
    query,
    caseSensitive: false,
    source: 'local',
    identity: `local:${grantId}`,
    connection: null,
    grantId,
  };
}

test('typing invalidates displayed search data immediately and a late old result cannot restore it', async () => {
  const searches = new WorkspaceSearchSession();
  const firstBinding = remoteBinding({query: 'old'});
  const first = searches.begin(firstBinding, 'request-old');
  const response = deferred();
  const completion = response.promise.then(result => searches.accept(first, result, firstBinding));

  // The input event performs this synchronously, before its debounce starts a replacement search.
  searches.invalidate();
  assert.equal(searches.snapshotFor(firstBinding), null);

  response.resolve({matches: [{path: 'old.cc', revision: 'sha256:old'}]});
  assert.equal(await completion, false);
  assert.equal(searches.finish(first), false, 'a late finally block does not finish a newer session');
  assert.equal(searches.snapshotFor(firstBinding), null);

  const nextBinding = remoteBinding({query: 'new'});
  const next = searches.begin(nextBinding, 'request-new');
  assert.equal(searches.accept(next, {matches: [{path: 'new.cc'}]}, nextBinding), true);
  assert.equal(searches.snapshotFor(nextBinding)?.result.matches[0].path, 'new.cc');
});

test('remote target or generation changes clear snapshots and forbid replace from an old search', async () => {
  const searches = new WorkspaceSearchSession();
  const original = remoteBinding({query: 'needle', generation: 7});
  const completed = searches.begin(original, 'request-completed-7');
  const accepted = {matches: [{path: 'a.cc', revision: `sha256:${'a'.repeat(64)}`}]};
  assert.equal(searches.accept(completed, accepted, original), true);
  assert.equal(searches.finish(completed), true);
  assert.ok(searches.snapshotFor(original), 'the matching connection may use the snapshot for replace');

  const response = deferred();
  const late = searches.begin(original, 'request-late-7');
  let currentBinding = original;
  const completion = response.promise.then(result => searches.accept(late, result, currentBinding));
  currentBinding = remoteBinding({query: 'needle', generation: 8});
  searches.invalidate();
  assert.equal(searches.snapshotFor(original), null);
  assert.equal(searches.snapshotFor(currentBinding), null,
    'same identity with a new connection generation must search again before replace');

  response.resolve({matches: [{path: 'stale.cc'}]});
  assert.equal(await completion, false);

  const otherTarget = remoteBinding({query: 'needle', generation: 1, host: 'server-b'});
  assert.equal(searches.snapshotFor(otherTarget), null);
});

test('switching source invalidates an in-flight read, clears loading and permits a clean retry', async () => {
  const reads = new WorkspaceReadTokens();
  const remote = remoteBinding();
  const tab = {id: 'tab-a', status: 'loading', content: ''};
  const first = reads.begin(remote, tab.id);
  const response = deferred();
  const completion = response.promise.then(result => {
    if (reads.accept(first, result, localBinding())) {
      tab.content = result.content;
      tab.status = 'ready';
    }
    if (reads.finish(first)) tab.status = 'error';
  });

  const invalidated = reads.invalidate();
  assert.deepEqual(invalidated, [first]);
  for (const token of invalidated) {
    if (token.key === tab.id) tab.status = 'error';
  }
  assert.equal(tab.status, 'error', 'source switching can synchronously remove the permanent loading state');

  const retryBinding = localBinding();
  tab.status = 'loading';
  const retry = reads.begin(retryBinding, tab.id);
  response.resolve({content: 'stale remote bytes'});
  await completion;
  assert.equal(tab.status, 'loading', 'the old finally block cannot clear the retry loading state');
  assert.equal(tab.content, '');

  const retryResult = {content: 'current local bytes'};
  assert.equal(reads.accept(retry, retryResult, retryBinding), true);
  tab.content = retryResult.content;
  tab.status = 'ready';
  assert.equal(reads.finish(retry), true);
  assert.equal(tab.content, 'current local bytes');
});

test('a remote generation change observed while local is active forces revalidation when returning', async () => {
  const reads = new WorkspaceReadTokens();
  const loadedUnder = remoteBinding({generation: 20});
  const tab = {id: 'remote-tab', status: 'ready', loadedUnder, revisionNeedsVerification: false};

  // SSH reconnects while Explorer remains on the local source. Identity is unchanged but generation advances.
  const reconnected = remoteBinding({generation: 21});
  assert.equal(loadedUnder.identity, reconnected.identity);
  assert.equal(sameWorkspaceBinding(loadedUnder, reconnected), false);

  // Returning to remote must compare the full binding and start a fresh read, rather than trusting identity alone.
  if (!sameWorkspaceBinding(tab.loadedUnder, reconnected)) {
    tab.status = 'loading';
    tab.revisionNeedsVerification = true;
  }
  const refresh = reads.begin(reconnected, tab.id);
  const response = deferred();
  const completion = response.promise.then(result => {
    if (!reads.accept(refresh, result, reconnected)) return false;
    tab.status = 'ready';
    tab.loadedUnder = reconnected;
    tab.revisionNeedsVerification = false;
    reads.finish(refresh);
    return true;
  });

  response.resolve({content: 'fresh bytes', revision: 'sha256:fresh'});
  assert.equal(await completion, true);
  assert.equal(tab.status, 'ready');
  assert.equal(tab.revisionNeedsVerification, false);
  assert.equal(sameWorkspaceBinding(tab.loadedUnder, reconnected), true);
});
