'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {DesktopRuntime} = require('../runtime/index.cjs');
const {captureEnvironment} = require('../runtime/capture-environment.cjs');

function runCli(helper, method, argument, env) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [helper, method, ...(argument ? [argument] : [])], {env, windowsHide: true});
    let stdout = '', stderr = '';
    child.stdout.setEncoding('utf8'); child.stderr.setEncoding('utf8');
    child.stdout.on('data', data => stdout += data); child.stderr.on('data', data => stderr += data);
    child.on('error', reject);
    child.on('close', code => resolve({code, stdout, stderr}));
  });
}

test('Agent CLI saves a shared-contract plan and reports failed local capture without requiring SSH', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-tools-'));
  const runtime = new DesktopRuntime({userDataDir: directory, resourcesDir: path.resolve(__dirname, '..'),
    captureOptions: captureEnvironment({appPath: path.resolve(__dirname, '..')})});
  t.after(async () => { await runtime.dispose(); fs.rmSync(directory, {recursive: true, force: true}); });
  const integration = await runtime.getIntegration();
  const env = {...process.env, HETERO_DESKTOP_BROKER_URL: integration.brokerUrl, HETERO_DESKTOP_BROKER_TOKEN: runtime.brokerToken};
  const catalogCall = await runCli(integration.helperPath, 'capture_catalog', null, env);
  assert.equal(catalogCall.code, 0, catalogCall.stderr);
  const catalog = JSON.parse(catalogCall.stdout);
  assert.equal(catalog.default_capture.pmu.period_us, 10);
  assert.equal(catalog.templates[0].steps.length, 17);
  const input = path.join(directory, 'plan request 中文.json');
  fs.writeFileSync(input, '\uFEFF' + JSON.stringify({plan: {template_id: 'gallery_17_steps_v1', title: '桌面独立采集', capture: {selected_step_ids: ['step_07'], collectors: ['trace']}}}));
  const savedCall = await runCli(integration.helperPath, 'capture_save_plan', `@${input}`, env);
  assert.equal(savedCall.code, 0, savedCall.stderr);
  const saved = JSON.parse(savedCall.stdout);
  assert.ok(fs.existsSync(path.join(saved.packageDirectory, 'package.json')));
  const updateInput = path.join(directory, 'plan update.json');
  fs.writeFileSync(updateInput, JSON.stringify({planId: saved.planId, expectedUpdatedAt: saved.updatedAt, requestId: 'capture_plan_cli_fixture', title: '桌面差量采集', summary: '更新计划名称。'}));
  const updatedCall = await runCli(integration.helperPath, 'capture_update_plan', `@${updateInput}`, env);
  assert.equal(updatedCall.code, 0, updatedCall.stderr);
  const updated = JSON.parse(updatedCall.stdout);
  assert.equal(updated.planId, saved.planId); assert.equal(updated.changed, true);
  assert.equal(runtime.capture.getState().plans.find(value => value.planId === saved.planId).plan.title, '桌面差量采集');
  runtime.capture.updateDevices({devices: [{serial: 'TEST_PHONE_A'}, {serial: 'TEST_PHONE_B'}]});
  await runtime.selectCaptureDevice({serial: 'TEST_PHONE_B'});
  // An existing executable rejects HDC argv. This reaches the ordinary failed
  // run/manifest/archive path without touching a phone or requiring HDC.
  await runtime.saveSettings({hdcPath: process.execPath});
  const implicit = await runCli(integration.helperPath, 'capture_start', JSON.stringify({planId: updated.planId}), env);
  assert.equal(implicit.code, 1); assert.match(implicit.stderr, /deviceSerial/);
  const wrong = await runCli(integration.helperPath, 'capture_start', JSON.stringify({planId: updated.planId, deviceSerial: 'TEST_PHONE_A'}), env);
  assert.equal(wrong.code, 1); assert.match(wrong.stderr, /选择不一致/);
  const started = await runCli(integration.helperPath, 'capture_start', JSON.stringify({planId: updated.planId, deviceSerial: 'TEST_PHONE_B'}), env);
  assert.equal(started.code, 0, started.stderr);
  const run = JSON.parse(started.stdout);
  const deadline = Date.now() + 15000;
  while (runtime.capture.getState().activeRun && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 30));
  const state = runtime.capture.getState();
  assert.equal(state.activeRun, null);
  const finished = state.runs.find(value => value.runId === run.runId);
  assert.equal(finished.status, 'failed');
  assert.equal(finished.deviceSerial, 'TEST_PHONE_B');
  assert.ok(fs.existsSync(finished.archivePath), JSON.stringify(finished));
  assert.equal(state.traces.length, 1);
  const list = await runCli(integration.helperPath, 'trace_list', null, env);
  assert.equal(list.code, 0, list.stderr);
  assert.equal(JSON.parse(list.stdout).traces[0].status, 'failed');
  assert.equal((await runtime.getState()).ssh.state, 'disconnected');
  const concise = await runtime.captureStatus({planId: updated.planId});
  assert.equal(concise.plan.planId, updated.planId);
  assert.ok(!('planSnapshot' in concise.runs[0]));
  assert.ok(!('plan' in concise.plans[0]));
});

test('capture resolves a PATH command to a frozen executable file before Python launch', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-hdc-path-'));
  const runtime = new DesktopRuntime({userDataDir: directory, resourcesDir: path.resolve(__dirname, '..')});
  t.after(async () => { await runtime.dispose(); fs.rmSync(directory, {recursive: true, force: true}); });
  await runtime.saveSettings({hdcPath: process.platform === 'win32' ? 'node.exe' : 'node'});
  let launch;
  runtime.capture.startCapture = value => { launch = value; return {runId: 'frozen-path'}; };
  const result = await runtime.startCapture({planId: 'test', deviceSerial: 'TEST_PHONE'});
  assert.equal(result.runId, 'frozen-path');
  assert.ok(path.isAbsolute(launch.hdcPath));
  assert.ok(fs.statSync(launch.hdcPath).isFile());
  assert.equal(launch.deviceSerial, 'TEST_PHONE');
});
