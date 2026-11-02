'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const {buildAgentRepairPrompt, redactRepairText, repairableAgentError, safeServerContext} = require('../renderer/agent-error-repair.cjs');

test('only environment, dependency, package, and module failures are repairable', () => {
  assert.equal(repairableAgentError(new Error("ModuleNotFoundError: No module named 'backend'")), true);
  assert.equal(repairableAgentError(new Error("ImportError: cannot import name 'UTC' from datetime")), true);
  assert.equal(repairableAgentError(new Error('pip3: command not found')), true);
  assert.equal(repairableAgentError(new Error('HETERO_DESKTOP_REPAIRABLE\tREMOTE_WORKER_PYTHON_UNAVAILABLE')), true);
  assert.equal(repairableAgentError(new Error('未找到 Desktop 分析 Worker：已检查服务器 Profile 工作仓。')), true);
  assert.equal(repairableAgentError(new Error('未找到服务器分析服务：已检查当前工作目录与已配置的工作仓。')), true);
  assert.equal(repairableAgentError(new Error('请选择至少一个并行候选任务。')), false);
  assert.equal(repairableAgentError(new Error('请求期间服务器连接已改变。')), false);
});

test('structured repair kind and stable codes take precedence over message fallback', () => {
  assert.equal(repairableAgentError({message: 'generic failure', details: {repair_kind: 'python-environment'}}), true);
  assert.equal(repairableAgentError({message: "ModuleNotFoundError: No module named 'backend'", repairKind: 'network'}), false);
  assert.equal(repairableAgentError({message: 'worker unavailable', code: 'DESKTOP_PLATFORM_WORKER_UNAVAILABLE'}), true);
  assert.equal(repairableAgentError({message: 'HETERO_DESKTOP_REPAIRABLE\tREMOTE_WORKER_MISSING\tworker unavailable'}), true);
  assert.equal(repairableAgentError({message: "HETERO_DESKTOP_REPAIRABLE\tUNKNOWN_FAILURE\tModuleNotFoundError: No module named 'backend'"}), false);
  assert.equal(repairableAgentError({message: 'dependency unavailable', code: 'PARALLEL_MODELING_INVALID_INPUT'}), false);
});

test('cancelled, HTTP 4xx, SSH, and bridge timeout failures never offer Agent repair', () => {
  assert.equal(repairableAgentError({name: 'AbortError', message: "ModuleNotFoundError: No module named 'backend'"}), false);
  assert.equal(repairableAgentError({message: "ModuleNotFoundError: No module named 'backend'", statusCode: 422}), false);
  assert.equal(repairableAgentError({message: "HTTP 404: ModuleNotFoundError: No module named 'backend'"}), false);
  assert.equal(repairableAgentError({message: 'SSH connection timed out while waiting for output'}), false);
  assert.equal(repairableAgentError({message: 'timed out while waiting for Windows device bridge'}), false);
  assert.equal(repairableAgentError({message: 'bridge job expired', code: 'BRIDGE_JOB_DEADLINE'}), false);
  assert.equal(repairableAgentError({message: 'transport unavailable', code: 'WINDOWS_BRIDGE_TIMEOUT'}), false);
});

test('repair diagnostics redact credentials and private keys', () => {
  const value = redactRepairText([
    'https://alice:hunter2@example.test/repo',
    'BRIDGE_SECRET=secret-value',
    'Authorization: Bearer abc.def.ghi',
    '-----BEGIN PRIVATE KEY-----',
    'private-material',
    '-----END PRIVATE KEY-----',
  ].join('\n'));
  assert.doesNotMatch(value, /hunter2|secret-value|abc\.def\.ghi|private-material/);
  assert.match(value, /\[已隐藏\]|\[私钥已隐藏\]/);
});

test('repair prompt includes retry context but excludes SSH authentication state', () => {
  const state = {
    ssh: {host: 'build.example.test', port: 2202, username: 'builder', cwd: '/srv/work', password: 'do-not-send', privateKey: 'do-not-send-key'},
    settings: {
      ssh: {password: 'saved-secret', passphrase: 'saved-passphrase'},
      serverProfile: {profileId: 'dev', workerRepoRoot: '/srv/work/heterogeneous-enable', pythonBin: '/usr/bin/python3.10', apiKey: 'do-not-send-api-key'},
    },
  };
  assert.deepEqual(safeServerContext(state), {
    ssh: {host: 'build.example.test', port: 2202, username: 'builder', cwd: '/srv/work'},
    profile: {id: 'dev', worker_repo_root: '/srv/work/heterogeneous-enable', python: '/usr/bin/python3.10'},
  });
  const prompt = buildAgentRepairPrompt({
    error: new Error("ModuleNotFoundError: No module named 'backend'; token=raw-secret"),
    page: 'workload-features', pageLabel: '负载特征', path: '/v1/workload-features', method: 'GET', state,
  });
  assert.match(prompt, /负载特征/);
  assert.match(prompt, /GET \/v1\/workload-features/);
  assert.match(prompt, /build\.example\.test/);
  assert.match(prompt, /No module named/);
  assert.doesNotMatch(prompt, /do-not-send|raw-secret|saved-secret|saved-passphrase/);
});

test('shared analysis pages expose repairable failures through the real Agent submit path', () => {
  const rendererRoot = path.join(__dirname, '..', 'renderer');
  const shared = fs.readFileSync(path.join(rendererRoot, 'shared-pages.jsx'), 'utf8');
  const renderer = fs.readFileSync(path.join(rendererRoot, 'renderer.js'), 'utf8');
  assert.match(shared, /repairableAgentError\(error\)/);
  assert.match(shared, /使用 Code Agent 修复/);
  assert.match(shared, /await submitTask\(buildAgentRepairPrompt/);
  assert.match(shared, /transport\.requestText/);
  assert.match(shared, /const requestPage = analysisPages\.has\(context\.owner\) \? context\.owner : activePage/);
  assert.match(shared, /offerAgentRepair\(error, path, init, requestPage\)/);
  assert.match(shared, /clearAgentRepair\(requestPage, path, init\)/);
  assert.match(renderer, /submitTask: submitAgentTask/);
});
