'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.resolve(__dirname, '..');
const source = relative => fs.readFileSync(path.join(root, relative), 'utf8');

test('OS file drops cross a dedicated preload bridge without exposing native paths to the renderer', () => {
  const preload = source('preload.cjs');
  const main = source('main.cjs');
  assert.match(preload, /webUtils/);
  assert.match(preload, /webUtils\.getPathForFile\(file\)/);
  assert.match(preload, /desktop:upload-dropped-files/);
  assert.doesNotMatch(preload, /file\.path/);
  assert.match(main, /ipcMain\.handle\('desktop:upload-dropped-files'/);
  assert.match(main, /event\.sender !== window\?\.webContents/);
  assert.match(main, /runtime\.uploadRemoteFilesFromLocal/);
  assert.doesNotMatch(
    main.slice(main.indexOf('const runtimeMethods'), main.indexOf('const fencedRemoteExplorerMethods')),
    /uploadRemoteFilesFromLocal/,
    'the generic renderer invoke allowlist must not accept arbitrary workstation paths',
  );
});

test('Explorer accepts external files only on the remote root or real directory rows', () => {
  const renderer = source('renderer/remote-files.js');
  assert.match(renderer, /dataTransfer\?\.types/);
  assert.match(renderer, /includes\('Files'\)/);
  assert.match(renderer, /sourceMode !== 'remote'/);
  assert.match(renderer, /row\.dataset\.directory !== 'true'/);
  assert.match(renderer, /row\.dataset\.dropEnabled !== 'true'/);
  assert.match(renderer, /directory && !entry\.symlink && entry\.accessible !== false/);
  for (const event of ['dragenter', 'dragover', 'dragleave', 'drop'])
    assert.match(renderer, new RegExp(`listen\\(elements\\.explorer, '${event}'`));
  assert.match(renderer, /uploadDroppedFiles\(files/);
  assert.match(renderer, /Array\.from\(event\.dataTransfer\?\.files \|\| \[\]\)/,
    'FileList must be snapshotted before crossing the isolated Electron bridge');
  assert.match(renderer, /expectedConnection: connectionFence\(\)\.expectedConnection/);
  assert.match(renderer, /loadDirectory\(directory, \{force: true/);
  assert.match(renderer, /if \(!isFileDrag\(event\)\) return;\s*event\.preventDefault\(\);\s*const target = uploadTargetForEvent\(event\)/s,
    'file drops are consumed even when the current row is not a valid target');
});

test('Explorer shows a bounded drop target and upload status without replacing the file tree', () => {
  const html = source('renderer/index.html');
  const styles = source('renderer/remote-files.css');
  assert.match(html, /id="remote-upload-status"/);
  assert.match(html, /aria-live="polite"/);
  assert.match(styles, /\.remote-upload-status/);
  assert.match(styles, /\.remote-tree-row\.drop-target/);
  assert.match(styles, /\.remote-root\.drop-target/);
  assert.match(styles, /\.remote-explorer\.upload-dragging/);
  assert.match(styles, /\.remote-upload-status\s*\{[^}]*position: absolute/s,
    'drop status must not move the directory row underneath the pointer');
  assert.match(styles, /\.remote-upload-status\s*\{[^}]*pointer-events: none/s);
});

