'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.resolve(__dirname, '..');
const source = relative => fs.readFileSync(path.join(root, relative), 'utf8');

test('Explorer contains remote/local sources and inline search/replace without another activity item', () => {
  const html = source('renderer/index.html');
  assert.match(html, /id="workspace-source-remote"/);
  assert.match(html, /id="workspace-source-local"/);
  assert.match(html, /id="workspace-search-query"/);
  assert.match(html, /id="workspace-replace-value"/);
  assert.match(html, /id="workspace-replace-all"/);
  assert.doesNotMatch(html, /data-page="search"/);
});

test('workspace UI fences late searches, supports cancellation and locates results in Monaco', () => {
  const renderer = source('renderer/remote-files.js');
  assert.match(renderer, /WorkspaceSearchSession/);
  assert.match(renderer, /searchSession\.snapshotFor\(binding\)/,
    'replace must consume only a search snapshot for the current query, source and connection generation');
  assert.match(renderer, /sameWorkspaceBinding\(snapshot\.binding, currentSearchBinding\(\)\)/,
    'a completed replacement must not update a different source or SSH generation');
  assert.match(renderer, /cancelRemoteFileSearch/);
  assert.match(renderer, /cancelLocalFileSearch/);
  assert.match(renderer, /editorController\.revealPosition/);
  assert.match(renderer, /if \(sourceMode === 'remote'\) applySourceState\(\)/,
    'SSH status updates must not tear down the active local workspace');
  assert.match(renderer, /searchTruncated/);
  assert.match(renderer, /搜索结果已达到安全上限/);
  assert.match(renderer, /revision/);
});

test('Main Process owns local root grants instead of accepting renderer absolute roots', () => {
  const main = source('main.cjs');
  assert.match(main, /new LocalWorkspaceRegistry\(\)/);
  assert.match(main, /method === 'chooseLocalWorkspace'/);
  assert.match(main, /localWorkspaces\.authorize\(ownerId, result\.filePaths\[0\]/);
  assert.match(main, /method === 'getDefaultLocalWorkspace'/);
  assert.match(main, /settings\?\.agent\?\.cwd/);
  assert.match(main, /localWorkspaces\.list\(ownerId, payload\)/);
});

