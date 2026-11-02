'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {
  EVENT_NAME,
  buildAgentErrorTask,
  buildAgentRecoveryTask,
  contextualizeNotification,
  createNotificationStore,
  errorCodeOf,
  normalizeNotificationText,
  safeNotificationContext,
} = require('../renderer/notification-center.cjs');

const desktopRoot = path.resolve(__dirname, '..');
const repositoryRoot = path.resolve(desktopRoot, '..');

test('notification store keeps errors until dismissal, deduplicates and preserves concurrent failures', () => {
  let latest = [];
  let dismissed = 0;
  const store = createNotificationStore({maxEntries: 4, onChange: entries => { latest = entries; }});
  const first = store.show({tone: 'error', source: 'calibration', text: 'benchmark invalid', onDismiss: () => { dismissed += 1; }});
  const duplicate = store.show({tone: 'error', source: 'calibration', text: 'benchmark invalid'});
  const second = store.show({tone: 'error', source: 'capture', text: 'device unavailable'});
  assert.equal(first, duplicate);
  assert.notEqual(first, second);
  assert.equal(latest.length, 2);
  assert.equal(latest[0].repeated, 2);
  assert.equal(latest[0].timeoutMs, 0, 'errors must remain visible until explicitly dismissed');
  assert.equal(store.dismiss(first), true);
  assert.equal(dismissed, 1);
  assert.deepEqual(store.snapshot().map(item => item.text), ['device unavailable']);
});

test('notification store bounds the stack and normalizes Electron IPC prefixes', () => {
  const store = createNotificationStore({maxEntries: 4});
  store.show({tone: 'success', source: 'a', text: 'one'});
  for (const text of ['two', 'three', 'four', 'five']) store.show({tone: 'error', source: text, text});
  assert.equal(store.snapshot().length, 4);
  assert.deepEqual(store.snapshot().map(item => item.text), ['two', 'three', 'four', 'five']);
  assert.equal(normalizeNotificationText("Error invoking remote method 'desktop:invoke': Error: 受管校准微基准校验失败"), '受管校准微基准校验失败');
});

test('automatic stack eviction never invokes a page dismissal callback', () => {
  let businessDismissals = 0;
  const store = createNotificationStore({maxEntries: 2});
  store.show({tone: 'error', source: 'first', text: 'first', onDismiss: () => { businessDismissals += 1; }});
  store.show({tone: 'error', source: 'second', text: 'second'});
  store.show({tone: 'error', source: 'third', text: 'third'});
  assert.equal(businessDismissals, 0, 'capacity management must not mutate page state');
  assert.deepEqual(store.snapshot().map(item => item.text), ['second', 'third']);
});

test('agent error task carries page, operation and error code while removing credentials', () => {
  const task = buildAgentErrorTask({
    tone: 'error',
    text: '请求失败 error_code: BRIDGE_JOB_DEADLINE token=plain-token https://alice:secret@example.test/path',
    source: 'capture-notice',
    page: 'capture',
    pageLabel: '负载采样',
    operation: '在设备执行采样',
    context: {
      operation_key: 'capture-start',
      selected_device: 'device-01',
      password: 'do-not-send',
      nested: {authorization: 'Bearer hidden', state: 'running'},
    },
  });
  assert.equal(task.taskContext.task_kind, 'workbench_error_diagnosis');
  assert.equal(task.taskContext.page, 'capture');
  assert.equal(task.taskContext.operation, '在设备执行采样');
  assert.equal(task.taskContext.error_code, 'BRIDGE_JOB_DEADLINE');
  assert.match(task.prompt, /负载采样/);
  assert.match(task.prompt, /在设备执行采样/);
  assert.match(task.prompt, /BRIDGE_JOB_DEADLINE/);
  assert.match(task.prompt, /device-01/);
  assert.doesNotMatch(task.prompt, /plain-token|alice:secret|do-not-send|Bearer hidden/);
  assert.match(task.prompt, /\[已隐藏\]/);
});

test('automatic recovery task carries a typed completion handshake and frozen operation identity', () => {
  const retry = () => {};
  const task = buildAgentRecoveryTask({
    tone: 'error',
    text: '采样失败 token=plain-token',
    page: 'capture',
    pageLabel: '负载采样',
    operation: '在设备执行采样',
    context: {operation_key: 'capture-start'},
    recovery: {
      operationAttemptId: 'capture-attempt-17',
      operationKey: 'capture-start',
      selectedDevice: 'device-01',
      planId: 'plan-01',
      planRevision: 'revision-01',
      retry,
    },
  }, {requestId: 'desktop_repair_17', authorizationScopes: ['device', 'device', 'invalid']});
  assert.equal(task.taskContext.task_kind, 'workbench_error_recovery');
  assert.equal(task.taskContext.repair_request_id, 'desktop_repair_17');
  assert.equal(task.taskContext.operation_attempt_id, 'capture-attempt-17');
  assert.equal(task.taskContext.selected_device, 'device-01');
  assert.equal(task.taskContext.capture_plan_id, 'plan-01');
  assert.equal(task.taskContext.capture_plan_expected_updated_at, 'revision-01');
  assert.deepEqual(task.taskContext.recovery_authorization_scopes, ['device']);
  assert.match(task.prompt, /desktop_recovery_complete/);
  assert.match(task.prompt, /不要自行重放原操作/);
  assert.doesNotMatch(task.prompt, /plain-token/);
});

test('notification store keeps the executable recovery descriptor without exposing it in context', () => {
  const retry = () => {};
  const store = createNotificationStore();
  const id = store.show({
    tone: 'error', source: 'capture', text: '采样失败',
    recovery: {operationAttemptId: 'attempt-1', operationKey: 'capture-start', scopes: ['device'], retry},
  });
  const entry = store.snapshot().find(item => item.id === id);
  assert.equal(entry.recovery.retry, retry);
  assert.equal(entry.recovery.operationAttemptId, 'attempt-1');
  assert.equal(entry.recoveryState, '');
});

test('notification context filtering removes sensitive keys recursively', () => {
  assert.equal(errorCodeOf({text: '错误码：CALIBRATION_FAILED'}), 'CALIBRATION_FAILED');
  assert.deepEqual(safeNotificationContext({path: '/tmp/data', apiKey: 'secret', nested: {cookie: 'value', ready: true}}), {
    path: '/tmp/data',
    nested: {ready: true},
  });
});

test('undefined page metadata cannot erase the runtime operation location', () => {
  const merged = contextualizeNotification({
    tone: 'error',
    text: '读取失败',
    page: undefined,
    pageLabel: undefined,
    operation: undefined,
    context: {operation_key: 'load-features'},
  }, {
    page: 'workload-features',
    pageLabel: '负载特征',
    operation: '逐阶段负载特征 / 读取数据',
    context: {ui_section: '逐阶段负载特征', ui_control: '读取数据'},
  });
  assert.equal(merged.page, 'workload-features');
  assert.equal(merged.pageLabel, '负载特征');
  assert.equal(merged.operation, '逐阶段负载特征 / 读取数据');
  assert.deepEqual(merged.context, {
    ui_section: '逐阶段负载特征',
    ui_control: '读取数据',
    operation_key: 'load-features',
  });
});

test('Desktop notification center is viewport-level, copyable, dismissible and shared by product pages', () => {
  const html = fs.readFileSync(path.join(desktopRoot, 'renderer/index.html'), 'utf8');
  const css = fs.readFileSync(path.join(desktopRoot, 'renderer/workbench-theme.css'), 'utf8');
  const center = fs.readFileSync(path.join(desktopRoot, 'renderer/notification-center.cjs'), 'utf8');
  const renderer = fs.readFileSync(path.join(desktopRoot, 'renderer/renderer.js'), 'utf8');
  const main = fs.readFileSync(path.join(desktopRoot, 'main.cjs'), 'utf8');
  const hook = fs.readFileSync(path.join(repositoryRoot, 'app/components/WorkbenchErrorNotification.tsx'), 'utf8');
  assert.match(html, /id="workbench-notification-root"/);
  assert.match(css, /\.workbench-notification-viewport\s*\{[^}]*position:\s*fixed;[^}]*z-index:/s);
  assert.match(css, /top:\s*calc\(var\(--desktop-titlebar-height/);
  assert.match(center, /workbench-notification-content[\s\S]+copyText\(entry\.text, card\)/);
  assert.match(center, /workbench-notification-copy[\s\S]+workbench-notification-close/);
  assert.match(center, /workbench-notification-agent[\s\S]+发送给助手/);
  assert.match(center, /onSendToAgent\(buildAgentErrorTask\(entry\)\)/);
  assert.match(center, /setAttribute\('role', entry\.tone === 'error' \? 'alert' : 'status'\)/);
  assert.match(renderer, /createNotificationCenter\(\{[\s\S]*?document,[\s\S]*?window,[\s\S]*?clipboard:/);
  assert.match(renderer, /getContext:\s*notificationRuntimeContext/);
  assert.match(renderer, /onSendToAgent:[\s\S]*submitAgentTask/);
  assert.match(renderer, /getAutomation:[\s\S]*normalizeAutomationSettings/);
  assert.match(renderer, /onAutoRecover:[\s\S]*submitAgentTask/);
  assert.match(renderer, /event\.type === 'agent-recovery'[\s\S]*handleRecoveryEvent/);
  assert.match(renderer, /publishWorkbenchNotification\(\{ tone: 'error'/);
  assert.match(main, /method === 'writeClipboard'/, 'copy must use the privileged Electron clipboard path');
  assert.match(hook, /currentKeyRef\.current === key/, 'closing a stale card must not clear newer page state');
  assert.equal(EVENT_NAME, 'heterogeneous-workbench:notification');

  for (const relative of [
    'app/components/OptimizationCalibrationStudio.tsx',
    'app/components/WorkloadFeaturesStudio.tsx',
    'app/components/HardwareModelsStudio.tsx',
    'app/components/ParallelModelingStudio.tsx',
    'app/components/ScenarioCaptureStudio.tsx',
    'app/components/TraceLibraryStudio.tsx',
    'app/components/ApplicationFlowStudio.tsx',
    'app/components/ReasoningLab.tsx',
    'app/components/DeviceVerificationPanel.tsx',
    'app/components/CodeKnowledgeStudio.tsx',
    'app/components/SkillLibraryManager.tsx',
    'app/components/ExperienceEvolutionStudio.tsx',
  ]) assert.match(fs.readFileSync(path.join(repositoryRoot, relative), 'utf8'), /useWorkbenchErrorNotification/,
    `${relative} must route transient errors through the viewport notification center`);
});

