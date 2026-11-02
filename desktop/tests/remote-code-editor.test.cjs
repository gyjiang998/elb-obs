'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
  languageFromPath,
  modelUriFor,
  monacoThemeFor,
  stableHash,
} = require('../renderer/remote-code-editor-contract.cjs');

test('remote editor maps common source, configuration and documentation files to Monaco languages', () => {
  assert.equal(languageFromPath('src/render/build_item.cc'), 'cpp');
  assert.equal(languageFromPath('include/render/build_item.h'), 'c');
  assert.equal(languageFromPath('scripts/capture_gallery.py'), 'python');
  assert.equal(languageFromPath('desktop/renderer/remote-files.js'), 'javascript');
  assert.equal(languageFromPath('app/page.tsx'), 'typescript');
  assert.equal(languageFromPath('docs/design.md'), 'markdown');
  assert.equal(languageFromPath('compose.yaml'), 'yaml');
  assert.equal(languageFromPath('CMakeLists.txt'), 'cmake');
  assert.equal(languageFromPath('containers/Dockerfile'), 'dockerfile');
  assert.equal(languageFromPath('containers/Dockerfile.arm64'), 'dockerfile');
  assert.equal(languageFromPath('settings/.env.production'), 'ini');
  assert.equal(languageFromPath('trace.bin'), 'plaintext');
});

test('model URIs are stable and namespace tabs without exposing SSH connection identities', () => {
  const input = {
    id: 'remote-tab-4',
    identity: 'developer:secret@example.internal:22',
    path: 'src/目录/build item.cc',
  };
  const first = modelUriFor(input);
  const second = modelUriFor(input);
  assert.equal(first, second);
  assert.match(first, /^remote-workspace:\/\/[a-z0-9]+\/remote-tab-4\//);
  assert.match(first, /src\/%E7%9B%AE%E5%BD%95\/build%20item%2Ecc$/);
  assert.doesNotMatch(first, /developer|secret|example/);
  assert.notEqual(modelUriFor({...input, identity: 'other-host'}), first);
  assert.notEqual(modelUriFor({...input, id: 'remote-tab-5'}), first);
  assert.equal(stableHash(input.identity), stableHash(input.identity));
});

test('editor theme follows workbench dark and light surfaces', () => {
  assert.equal(monacoThemeFor('dark'), 'vs-dark');
  assert.equal(monacoThemeFor('light'), 'vs');
  assert.equal(monacoThemeFor('hybrid'), 'vs');
  assert.equal(monacoThemeFor('unexpected'), 'vs');
});
