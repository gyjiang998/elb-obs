'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {RemoteEditorTabStore, tabIsDirty, tabNeedsRead} = require('../renderer/remote-editor-tabs.cjs');

function loaded(store, identity, path, content, source = 'remote') {
  const {tab} = store.open({identity, path, entry: {name: path}, rootPath: '/workspace'});
  Object.assign(tab, {content, pristine: content, revision: `rev:${path}`, busy: '', status: 'ready', source});
  return tab;
}

test('multiple remote files retain independent buffers, revisions and active selection', () => {
  const store = new RemoteEditorTabStore();
  const first = loaded(store, 'server-a', 'first.cc', 'FIRST\n');
  const second = loaded(store, 'server-a', 'second.cc', 'SECOND\n');
  first.content = 'FIRST EDITED\n';
  first.revisionNeedsVerification = true;

  assert.equal(store.all.length, 2);
  assert.equal(store.active, second);
  store.activate(first.id);
  assert.equal(store.active.content, 'FIRST EDITED\n');
  assert.equal(store.active.revision, 'rev:first.cc');
  assert.equal(store.active.revisionNeedsVerification, true);
  assert.equal(tabIsDirty(first), true);
  assert.equal(store.find('server-a', 'first.cc'), first);
});

test('changing one workspace source preserves tabs from the other source', () => {
  const store = new RemoteEditorTabStore();
  const remote = loaded(store, 'remote:server-a', 'remote.cc', 'REMOTE\n', 'remote');
  const local = loaded(store, 'local:grant-a', 'local.cc', 'LOCAL\n', 'local');
  const switched = store.transitionSourceIdentity('remote:server-b', 'remote');
  assert.deepEqual(switched.removed, [remote]);
  assert.equal(store.get(local.id), local);
  assert.equal(local.detached, false);
});

test('closing a tab selects a neighbour and does not discard another dirty buffer', () => {
  const store = new RemoteEditorTabStore();
  const first = loaded(store, 'server-a', 'first.cc', 'FIRST\n');
  const second = loaded(store, 'server-a', 'second.cc', 'SECOND\n');
  const third = loaded(store, 'server-a', 'third.cc', 'THIRD\n');
  first.content = 'DIRTY\n';

  store.activate(second.id);
  assert.equal(store.close(second.id), second);
  assert.equal(store.active, third);
  assert.deepEqual(store.dirtyTabs(), [first]);
});

test('SSH target changes retain every dirty tab read-only, discard clean tabs, and reattach matching tabs safely', () => {
  const store = new RemoteEditorTabStore();
  const dirtyA = loaded(store, 'server-a', 'dirty.cc', 'A\n');
  loaded(store, 'server-a', 'clean.cc', 'CLEAN\n');
  dirtyA.content = 'A EDITED\n';

  const switched = store.transitionToIdentity('server-b');
  assert.deepEqual(switched.retained, [dirtyA]);
  assert.equal(switched.removed.length, 1);
  assert.equal(dirtyA.detached, true);
  assert.equal(store.hasDirty(), true);

  const dirtyB = loaded(store, 'server-b', 'other.cc', 'B\n');
  dirtyB.content = 'B EDITED\n';
  store.transitionToIdentity('server-a');
  assert.equal(dirtyA.detached, false);
  assert.equal(dirtyA.revisionNeedsVerification, true);
  assert.equal(dirtyB.detached, true);
  assert.equal(store.active, dirtyA);
  assert.equal(store.dirtyTabs().length, 2);
});

test('reopening a transiently failed tab requests another remote read', () => {
  const store = new RemoteEditorTabStore();
  const first = store.open({identity: 'server-a', path: 'retry.cc', entry: {name: 'retry.cc'}});
  assert.equal(tabNeedsRead(first), true);
  Object.assign(first.tab, {busy: '', status: 'error', error: 'temporary SFTP failure'});
  const reopened = store.open({identity: 'server-a', path: 'retry.cc', entry: {name: 'retry.cc'}});
  assert.equal(reopened.created, false);
  assert.equal(tabNeedsRead(reopened), true);
  Object.assign(first.tab, {status: 'ready', error: ''});
  assert.equal(tabNeedsRead(store.open({identity: 'server-a', path: 'retry.cc'})), false);
});

test('a source-invalidated loading tab becomes retryable after its busy token is cleared', () => {
  const store = new RemoteEditorTabStore();
  const opened = store.open({identity: 'remote:server-a', path: 'late.cc', entry: {name: 'late.cc'}});
  Object.assign(opened.tab, {source: 'remote', status: 'loading', busy: ''});

  const retried = store.open({identity: 'remote:server-a', path: 'late.cc', entry: {name: 'late.cc'}});
  assert.equal(retried.created, false);
  assert.equal(tabNeedsRead(retried), true);

  retried.tab.busy = 'read';
  assert.equal(tabNeedsRead(retried), false, 'an active retry must not launch a duplicate read');
});
