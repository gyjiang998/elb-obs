const test = require('node:test');
const assert = require('node:assert/strict');
const { normalizeManifest } = require('../runtime-manifest.cjs');
const fs = require('node:fs');
const path = require('node:path');

test('imports the existing Linux catalog without interpreting argv as shell', () => {
  const document = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../../agent-resources/agent-cli-adapters.example.json')));
  const result = normalizeManifest(document, process.cwd());
  assert.equal(result.executable, '/home/user/bin/team-codeagent');
  assert.deepEqual(result.args, document.adapters[0].args);
  assert.equal(result.submitMode, 'bracketed_paste_cr');
  assert.equal(result.submitDelayMs, 120);
});
test('imports Windows executable and argv intact', () => {
  const document = { executable: 'C:\\Program Files\\Team Agent\\agent.exe', args: ['--name', '$(not a shell)'], submit_mode: 'plain_text_lf', bootstrapContext: true };
  const result = normalizeManifest(document, process.cwd());
  assert.equal(result.executable, document.executable);
  assert.deepEqual(result.args, document.args);
  assert.equal(result.submitMode, 'plain_text_lf');
  assert.equal(Object.hasOwn(result, 'bootstrapContext'), false, 'imported manifests cannot re-enable startup input');
});
test('normalizes imported submit delay to the Desktop 100-5000 ms contract', () => {
  assert.equal(normalizeManifest({executable: 'agent'}, process.cwd()).submitDelayMs, 1000);
  assert.equal(normalizeManifest({executable: 'agent', submit_delay_ms: 0}, process.cwd()).submitDelayMs, 100);
  assert.equal(normalizeManifest({executable: 'agent', submit_delay_ms: 6000}, process.cwd()).submitDelayMs, 5000);
  assert.equal(normalizeManifest({executable: 'agent', submit_delay_ms: 'invalid'}, process.cwd()).submitDelayMs, 100);
});
test('rejects a command string in place of argv', () => {
  assert.throws(() => normalizeManifest({ executable: 'agent', args: '--workdir x' }, process.cwd()), /字符串数组/);
});
