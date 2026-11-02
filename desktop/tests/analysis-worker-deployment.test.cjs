'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {prepareAnalysisWorker} = require('../build/prepare-analysis-worker.cjs');
const {loadAnalysisWorkerBundle} = require('../runtime/analysis-worker.cjs');
const {DesktopRuntime} = require('../runtime/index.cjs');
const desktopVersion = require('../package.json').version;

let temporary, resourcesDir, bundle;
test.before(() => {
  temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'analysis-worker-deploy-'));
  resourcesDir = path.join(temporary, 'desktop');
  prepareAnalysisWorker({output: path.join(resourcesDir, 'build/resources/analysis-worker')});
  bundle = loadAnalysisWorkerBundle({resourcesDir, resourcesPath: '/missing', desktopVersion});
});
test.after(() => fs.rmSync(temporary, {recursive: true, force: true}));

function fixture() {
  const target = {host: 'worker.example', port: 22, username: 'tester', cwd: '/srv/workspace'};
  const profile = {profileId: 'project-a', workerRepoRoot: '/srv/product', pythonBin: '/usr/bin/python3'};
  const runtime = Object.create(DesktopRuntime.prototype);
  Object.assign(runtime, {
    ready: Promise.resolve(), resourcesDir, version: desktopVersion, settings: {serverProfiles: {}, serverProfile: profile,
      serverProfileTarget: target, ssh: target},
    ssh: {state: 'connected', ...target}, client: {}, pendingAnalysisWorkerDeployments: new Map(),
  });
  const scopeId = runtime.serverProfileScopeId(target);
  const baseRoot = `/home/tester/.local/share/heterogeneous-workbench/server-profiles/targets/${scopeId}/project-a/assets/analysis-worker`;
  const finalRoot = `${baseRoot}/${bundle.manifest.identity}`;
  return {runtime, target, profile, scopeId, baseRoot, finalRoot};
}

function result(stdout = '', stderr = '', exitCode = 0) { return {stdout, stderr, exitCode}; }

test('missing worker uploads the verified bundle to a versioned profile directory and activates it', async () => {
  const f = fixture();
  const commands = []; const uploads = [];
  f.runtime.executeRemote = async ({command, expectedTarget}) => {
    assert.deepEqual(expectedTarget, f.target); commands.push(command);
    if (command.includes('HETERO_ANALYSIS_WORKER_ROOT')) return result(`HETERO_ANALYSIS_WORKER_ROOT\t${f.baseRoot}\n`);
    if (command.includes('worker_root=') && !command.includes('staging=')) return result('HETERO_ANALYSIS_WORKER_STATUS\tCORRUPT\tfixture corrupt\n');
    if (command.includes('staging=') && command.includes('HETERO_ANALYSIS_WORKER_READY'))
      return result(`HETERO_ANALYSIS_WORKER_READY\t${f.finalRoot}\n`);
    return result();
  };
  f.runtime.withSftpTransfer = async (_target, operation, options) => {
    assert.equal(options.label, '分析 Worker 部署');
    return operation({fastPut(source, destination, _options, callback) {
      uploads.push({source, destination}); callback(null);
    }});
  };
  const deployed = await f.runtime.ensureRemoteAnalysisWorker({profile: f.profile, target: f.target});
  assert.equal(deployed.root, f.finalRoot);
  assert.equal(deployed.reused, false);
  assert.equal(uploads.length, bundle.files.length + 1);
  assert.ok(uploads.every(item => item.destination.startsWith(`${f.baseRoot}/.partial-`)));
  assert.ok(uploads.some(item => item.destination.endsWith('/analysis-worker-manifest.json')));
  assert.ok(commands.some(command => command.includes('analysis worker file path is invalid')));
  assert.ok(commands.some(command => command.includes('mv -- "$staging" "$final"')));
  assert.ok(commands.every(command => !command.includes('git pull') && !command.includes('pip install') && !command.includes('sudo ')));
});

test('an already verified worker is reused without SFTP upload', async () => {
  const f = fixture(); let transfers = 0; const commands = [];
  f.runtime.executeRemote = async ({command}) => {
    commands.push(command);
    if (command.includes('HETERO_ANALYSIS_WORKER_ROOT')) return result(`HETERO_ANALYSIS_WORKER_ROOT\t${f.baseRoot}\n`);
    return result('HETERO_ANALYSIS_WORKER_STATUS\tREADY\tready\n');
  };
  f.runtime.withSftpTransfer = async () => { transfers++; throw new Error('must not upload'); };
  const deployed = await f.runtime.ensureRemoteAnalysisWorker({profile: f.profile, target: f.target});
  assert.equal(deployed.reused, true);
  assert.equal(transfers, 0);
  assert.equal(commands.length, 2);
});

test('an installed worker with an unavailable dependency reports a stable environment error without re-upload', async () => {
  const f = fixture(); let transfers = 0; let activations = 0; let inspections = 0;
  f.runtime.executeRemote = async ({command}) => {
    if (command.includes('HETERO_ANALYSIS_WORKER_ROOT'))
      return result(`HETERO_ANALYSIS_WORKER_ROOT\t${f.baseRoot}\n`);
    if (command.includes('worker_root=') && !command.includes('staging=')) {
      inspections++;
      return inspections === 1
        ? result('HETERO_ANALYSIS_WORKER_STATUS\tABSENT\tmissing\n')
        : result('HETERO_ANALYSIS_WORKER_STATUS\tDEPENDENCY_UNAVAILABLE\tModuleNotFoundError: missing-package\n');
    }
    if (command.includes('staging=') && command.includes('HETERO_ANALYSIS_WORKER_READY')) {
      activations++;
      return result('', 'HETERO_ANALYSIS_WORKER_STATUS\tDEPENDENCY_UNAVAILABLE\tModuleNotFoundError: missing-package\n', 7);
    }
    return result();
  };
  f.runtime.withSftpTransfer = async (_target, operation) => {
    transfers++;
    return operation({fastPut(_source, _destination, _options, callback) { callback(null); }});
  };
  for (let attempt = 0; attempt < 2; attempt++) {
    await assert.rejects(f.runtime.ensureRemoteAnalysisWorker({profile: f.profile, target: f.target}), error =>
      error.code === 'DESKTOP_ANALYSIS_WORKER_DEPENDENCY_UNAVAILABLE'
      && /missing-package/.test(error.message) && !/HETERO_/.test(error.message));
  }
  assert.equal(transfers, 1, 'integrity-valid deployed content must not be uploaded again');
  assert.equal(activations, 1);
  assert.equal(inspections, 2);
});

test('failed upload removes only its managed staging directory', async () => {
  const f = fixture(); const commands = [];
  f.runtime.executeRemote = async ({command}) => {
    commands.push(command);
    if (command.includes('HETERO_ANALYSIS_WORKER_ROOT')) return result(`HETERO_ANALYSIS_WORKER_ROOT\t${f.baseRoot}\n`);
    if (command.includes('worker_root=')) return result('HETERO_ANALYSIS_WORKER_STATUS\tABSENT\tmissing\n');
    return result();
  };
  f.runtime.withSftpTransfer = async (_target, operation) => operation({
    fastPut(_source, _destination, _options, callback) { callback(new Error('fixture transfer failure')); },
  });
  await assert.rejects(f.runtime.ensureRemoteAnalysisWorker({profile: f.profile, target: f.target}), /上传分析 Worker 失败/);
  const cleanup = commands.at(-1);
  assert.match(cleanup, /^case /);
  assert.match(cleanup, /\.partial-/);
  assert.match(cleanup, /rm -rf --/);
  assert.ok(cleanup.includes(f.baseRoot));
  assert.ok(!cleanup.includes('/srv/product'));
});

test('concurrent requests share one worker deployment and target changes stop before upload', async () => {
  const f = fixture(); let rootCalls = 0; let release;
  const gate = new Promise(resolve => { release = resolve; });
  f.runtime.executeRemote = async ({command}) => {
    if (command.includes('HETERO_ANALYSIS_WORKER_ROOT')) {
      rootCalls++; await gate; return result(`HETERO_ANALYSIS_WORKER_ROOT\t${f.baseRoot}\n`);
    }
    return result('HETERO_ANALYSIS_WORKER_STATUS\tREADY\tready\n');
  };
  f.runtime.withSftpTransfer = async () => { throw new Error('must reuse'); };
  const first = f.runtime.ensureRemoteAnalysisWorker({profile: f.profile, target: f.target});
  const second = f.runtime.ensureRemoteAnalysisWorker({profile: f.profile, target: f.target});
  await Promise.resolve(); release();
  const values = await Promise.all([first, second]);
  assert.equal(rootCalls, 1);
  assert.deepEqual(values[0], values[1]);

  const changed = fixture(); let transfers = 0;
  changed.runtime.executeRemote = async ({command}) => {
    if (command.includes('HETERO_ANALYSIS_WORKER_ROOT')) {
      changed.runtime.ssh = {...changed.runtime.ssh, cwd: '/srv/another-workspace'};
      return result(`HETERO_ANALYSIS_WORKER_ROOT\t${changed.baseRoot}\n`);
    }
    return result('', '', 3);
  };
  changed.runtime.withSftpTransfer = async () => { transfers++; };
  await assert.rejects(changed.runtime.ensureRemoteAnalysisWorker({profile: changed.profile, target: changed.target}), /SSH 目标在服务器环境操作期间发生变化/);
  assert.equal(transfers, 0);
});

test('deployment de-duplication includes the complete normalized profile fingerprint', async () => {
  const f = fixture(); let rootCalls = 0;
  f.runtime.executeRemote = async ({command}) => {
    if (command.includes('HETERO_ANALYSIS_WORKER_ROOT')) {
      rootCalls++;
      await new Promise(resolve => setTimeout(resolve, 20));
      return result(`HETERO_ANALYSIS_WORKER_ROOT\t${f.baseRoot}\n`);
    }
    return result('HETERO_ANALYSIS_WORKER_STATUS\tREADY\tready\n');
  };
  f.runtime.withSftpTransfer = async () => { throw new Error('must reuse'); };
  const python310 = {...f.profile, pythonBin: '/usr/bin/python3.10'};
  const python311 = {...f.profile, pythonBin: '/usr/bin/python3.11'};
  await Promise.all([
    f.runtime.ensureRemoteAnalysisWorker({profile: python310, target: f.target}),
    f.runtime.ensureRemoteAnalysisWorker({profile: python311, target: f.target}),
  ]);
  assert.equal(rootCalls, 2);
});
