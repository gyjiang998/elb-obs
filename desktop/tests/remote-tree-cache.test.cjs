'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
  RemoteTreeCacheMetadata,
  normalizeConnectionIdentity,
} = require('../renderer/remote-tree-cache.cjs');

test('subtree tombstones fence late prefetch descendants without deleting monotonic versions', () => {
  let now = 100;
  const metadata = new RemoteTreeCacheMetadata({now: () => now});
  metadata.markRefreshed('src/removed', {versioned: true});
  const before = metadata.snapshot();
  const versionBefore = metadata.versions.get('src/removed');

  metadata.markSubtreeRemoved('src/removed', ['src/removed']);
  assert(metadata.versions.get('src/removed') > versionBefore, 'removal must advance rather than delete the version');
  assert.equal(metadata.canApplyPrefetch('src/removed', before), false);
  assert.equal(metadata.canApplyPrefetch('src/removed/only-known-to-prefetch', before), false,
    'the prefix tombstone also fences descendants absent from the renderer cache');

  const after = metadata.snapshot();
  assert.equal(metadata.canApplyPrefetch('src/removed/new-child', after), true,
    'a prefetch started after the deletion may repopulate a directory that legitimately reappeared');
});

test('directory freshness supports stale-while-revalidate without treating failures as fresh', () => {
  let now = 1000;
  const metadata = new RemoteTreeCacheMetadata({now: () => now});
  assert.equal(metadata.isStale('src', 600000), true);
  metadata.markRefreshed('src');
  assert.equal(metadata.isStale('src', 600000), false);
  now += 600000;
  assert.equal(metadata.isStale('src', 600000), true);
  metadata.markSubtreeRemoved('src');
  assert.equal(metadata.isStale('src', 600000), true);
});

test('connection identity is an unambiguous JSON tuple', () => {
  const first = normalizeConnectionIdentity({username: 'a|b', host: 'c', port: 22, cwd: '/work'});
  const second = normalizeConnectionIdentity({username: 'a', host: 'b|c', port: 22, cwd: '/work'});
  assert.notEqual(first, second);
  assert.deepEqual(JSON.parse(first), ['a|b', 'c', 22, '/work']);
  assert.deepEqual(JSON.parse(normalizeConnectionIdentity({host: 'server'})), ['', 'server', 22, '~']);
});
