'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const {DesktopRuntime} = require('../runtime/index.cjs');

const desktopRoot = path.resolve(__dirname, '..');

test('automation settings are packaged and exposed through the connection menu and settings drawer', () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(desktopRoot, 'package.json'), 'utf8'));
  assert(pkg.build.files.includes('runtime/**'), 'packaged app must include all runtime modules');
  assert(pkg.build.asarUnpack.includes('runtime/**'), 'runtime modules must remain executable outside app.asar');

  const main = fs.readFileSync(path.join(desktopRoot, 'main.cjs'), 'utf8');
  assert.match(main, /settingsItem\('失败自动重试与授权…',\s*'automation'\)/);
  assert.match(main, /id:\s*`connection-\$\{tab\}`/);
  assert.match(main, /type:\s*'open-settings',\s*tab/);

  const html = fs.readFileSync(path.join(desktopRoot, 'renderer/index.html'), 'utf8');
  for (const id of [
    'tab-automation',
    'settings-automation',
    'automation-auto-recover-errors',
    'automation-allow-device-operations',
    'automation-allow-server-operations',
  ]) assert.match(html, new RegExp(`id=["']${id}["']`), `missing automation settings control ${id}`);
  assert.match(html, /出错自动发送助手解决并重试/);
  assert.match(html, /授权全部手机操作/);
  assert.match(html, /授权全部服务器操作/);
  assert.ok(html.indexOf('id="tab-automation"') < html.indexOf('id="tab-ssh"'), '失败自动重试应位于首屏可见的 Code Agent 之后');

  const capture = fs.readFileSync(path.join(desktopRoot, 'renderer/capture.js'), 'utf8');
  const renderer = fs.readFileSync(path.join(desktopRoot, 'renderer/renderer.js'), 'utf8');
  assert.match(capture, /id="capture-auto-recovery-status"/);
  assert.match(capture, /id="capture-auto-recovery-settings"/);
  assert.match(capture, /openSettings\?\.\('automation'\)/);
  assert.match(renderer, /createCaptureStudio\(\{[^}]*openSettings/s);

  const checkboxTags = [...html.matchAll(/<input\b[^>]*\bid="(automation-(?:auto-recover-errors|allow-device-operations|allow-server-operations))"[^>]*>/g)];
  assert.equal(checkboxTags.length, 3);
  for (const [, id] of checkboxTags) {
    const tag = checkboxTags.find(match => match[1] === id)[0];
    assert.match(tag, /\btype="checkbox"/);
    assert.doesNotMatch(tag, /\bchecked\b/, `${id} must remain opt-in in the static UI`);
  }
});

test('automation settings persist with independent device and server grants', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-desktop-automation-'));
  const userDataDir = path.join(directory, 'user-data');
  let runtime = new DesktopRuntime({userDataDir, resourcesDir: desktopRoot, version: 'test'});
  t.after(async () => {
    await runtime?.dispose();
    fs.rmSync(directory, {recursive: true, force: true});
  });

  await runtime.getState();
  await runtime.saveSettings({
    automation: {
      autoRecoverErrors: true,
      allowDeviceOperations: true,
      allowServerOperations: false,
    },
  });
  const persisted = JSON.parse(fs.readFileSync(path.join(userDataDir, 'settings.json'), 'utf8'));
  assert.deepEqual(persisted.automation, {
    autoRecoverErrors: true,
    allowDeviceOperations: true,
    allowServerOperations: false,
  });

  await runtime.dispose();
  runtime = new DesktopRuntime({userDataDir, resourcesDir: desktopRoot, version: 'test'});
  assert.deepEqual((await runtime.getState()).settings.automation, persisted.automation);

  await runtime.saveSettings({
    automation: {
      autoRecoverErrors: true,
      allowDeviceOperations: false,
      allowServerOperations: true,
    },
  });
  assert.deepEqual((await runtime.getState()).settings.automation, {
    autoRecoverErrors: true,
    allowDeviceOperations: false,
    allowServerOperations: true,
  });
});
