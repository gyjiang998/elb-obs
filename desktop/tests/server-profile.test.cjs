'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const {DesktopRuntime} = require('../runtime/index.cjs');
const {
  RUNTIME_PROFILES,
  SDK_SURFACE,
  buildServerProfileInspectScript,
  buildServerProfilePrepareScript,
  normalizeServerProfile,
  parseServerProfileInspect,
} = require('../runtime/server-profile.cjs');
const {
  normalizeSshTarget,
  serverProfileTargetKey,
  parseServerProfileTargetKey,
} = require('../runtime/server-profile-target.cjs');
const posixFixture = {skip: process.platform === 'win32' ? 'Remote server profile scripts execute in bash on the Linux SSH target.' : false};

function executable(filename, contents = '#!/bin/sh\nexit 0\n') {
  fs.mkdirSync(path.dirname(filename), {recursive: true});
  fs.writeFileSync(filename, contents, {mode: 0o700});
  return filename;
}

function fakeAarch64Elf(filename) {
  const header = Buffer.alloc(20);
  header.set([0x7f, 0x45, 0x4c, 0x46, 2, 1], 0);
  header.writeUInt16LE(183, 18);
  fs.mkdirSync(path.dirname(filename), {recursive: true});
  fs.writeFileSync(filename, header);
}

function runRemote(command, home) {
  const child = spawnSync('/bin/bash', ['-c', command], {
    env: {...process.env, HOME: home}, encoding: 'utf8', maxBuffer: 2 * 1024 * 1024,
  });
  return {stdout: child.stdout, stderr: child.stderr, exitCode: child.status, signal: child.signal, durationMs: 0, cwd: '~'};
}

function completeProfile(root, {runtimeLibraries = [...new Set(Object.values(RUNTIME_PROFILES).flat())]} = {}) {
  const workerRepoRoot = path.join(root, 'worker repo with spaces');
  fs.symlinkSync(path.resolve(__dirname, '../..'), workerRepoRoot, 'dir');
  const sourceRoot = path.join(root, 'product source with spaces'); fs.mkdirSync(sourceRoot);
  const sdk = path.join(root, 'sdk surface with spaces');
  for (const [, , relative, kind] of SDK_SURFACE) {
    const target = path.join(sdk, ...relative.split('/'));
    if (kind === 'directory') fs.mkdirSync(target, {recursive: true});
    else { fs.mkdirSync(path.dirname(target), {recursive: true}); fs.writeFileSync(target, 'TYPEDEF int fixture_type;\n'); }
  }
  const llvm = path.join(root, 'llvm bin with spaces');
  for (const tool of ['clang', 'clang++', 'ld.lld']) executable(path.join(llvm, tool));
  const clangHeaders = path.join(path.dirname(llvm), 'lib', 'clang', '18', 'include');
  fs.mkdirSync(clangHeaders, {recursive: true});
  fs.writeFileSync(path.join(clangHeaders, 'stddef.h'), 'typedef __SIZE_TYPE__ size_t;\n');
  fs.writeFileSync(path.join(clangHeaders, 'stdint.h'), 'typedef __INT32_TYPE__ int32_t;\n');
  const tools = path.join(root, 'standalone tools with spaces');
  const glslangBin = executable(path.join(tools, 'glslang'));
  const traceStreamer = executable(path.join(tools, 'trace streamer'));
  const runtimeLibraryRoot = path.join(root, 'runtime libraries with spaces');
  for (const library of runtimeLibraries) fakeAarch64Elf(path.join(runtimeLibraryRoot, library));
  const pythonBin = spawnSync('/usr/bin/env', ['sh', '-c', 'command -v python3'], {encoding: 'utf8'}).stdout.trim();
  return normalizeServerProfile({
    profileId: 'device-build', workerRepoRoot, sourceRoots: [sourceRoot],
    openHarmonySdkRoot: sdk, llvmBinRoot: llvm, glslangBin, traceStreamer,
    runtimeLibraryRoot, pythonBin,
  });
}

async function runtimeFixture(t, remoteHome) {
  const prefs = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-server-profile-prefs-'));
  const runtime = new DesktopRuntime({userDataDir: prefs, resourcesDir: path.resolve(__dirname, '..'), version: 'test'});
  await runtime.getState();
  const commands = [];
  runtime.executeRemote = async ({command}) => { commands.push(command); return runRemote(command, remoteHome); };
  t.after(async () => { await runtime.dispose(); fs.rmSync(prefs, {recursive: true, force: true}); });
  return {runtime, prefs, commands};
}

test('server profile normalization preserves spaces and rejects control or env expansion input', () => {
  const value = normalizeServerProfile({profileId: 'team.a-1', workerRepoRoot: '/srv/工作 仓', sourceRoots: ['/src/one', '/src/one', '~/源码/two'], pythonBin: 'python3.11'});
  assert.equal(value.workerRepoRoot, '/srv/工作 仓');
  assert.deepEqual(value.sourceRoots, ['/src/one', '~/源码/two']);
  assert.equal(value.pythonBin, 'python3.11');
  assert.equal(normalizeServerProfile({pythonBin: '~/工具/python3'}).pythonBin, '~/工具/python3');
  assert.equal(normalizeServerProfile({pythonBin: ''}).pythonBin, 'python3');
  for (const invalid of ['/srv/repo\nnext', '/srv/$HOME', '/srv/"quoted"', '/srv/path\\escape'])
    assert.throws(() => normalizeServerProfile({workerRepoRoot: invalid}), /不能安全写入/);
  for (const [field, invalid] of [['workerRepoRoot', 'relative/repo'], ['openHarmonySdkRoot', 'sdk'], ['glslangBin', 'glslangValidator'], ['traceStreamer', 'bin/trace_streamer']])
    assert.throws(() => normalizeServerProfile({[field]: invalid}), /以 \/ 或 ~\//);
  assert.throws(() => normalizeServerProfile({pythonBin: 'tools/python3'}), /以 \/ 或 ~\//);
  assert.throws(() => normalizeServerProfile({pythonBin: 'python 3'}), /PATH 命令/);
  assert.throws(() => normalizeServerProfile({profileId: '../escape'}), /Profile ID/);
  assert.throws(() => normalizeServerProfile({sourceRoots: 'not-an-array'}), /字符串数组/);
  assert.throws(() => normalizeServerProfile({sourceRoots: Array.from({length: 4}, (_, index) => `/src/${index}/${'x'.repeat(3500)}`)}), /不能超过 12 KiB/);
  assert.throws(() => normalizeServerProfile({workerRepoRoot: `/${"'".repeat(2500)}`}), /Shell 编码不能超过 12 KiB/);
});

test('server profiles are isolated by canonical SSH host, port, user and cwd with legacy migration', async t => {
  const prefs = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-server-profile-scope-'));
  const targetA = {host: 'Build.EXAMPLE.com', port: '22', username: 'alice', cwd: '/srv/work-a/'};
  const canonicalA = normalizeSshTarget(targetA);
  const targetB = {host: 'build.example.com', port: 22, username: 'alice', cwd: '/srv/work-b'};
  const legacyA = normalizeServerProfile({profileId: 'server-a', workerRepoRoot: '/srv/work-a/platform'});
  fs.mkdirSync(prefs, {recursive: true});
  fs.writeFileSync(path.join(prefs, 'settings.json'), `${JSON.stringify({ssh: targetA, serverProfile: legacyA}, null, 2)}\n`);
  const runtime = new DesktopRuntime({userDataDir: prefs, resourcesDir: path.resolve(__dirname, '..'), version: 'test'});
  t.after(async () => { await runtime.dispose(); fs.rmSync(prefs, {recursive: true, force: true}); });

  const migrated = await runtime.getState();
  const keyA = serverProfileTargetKey(canonicalA);
  assert.deepEqual(parseServerProfileTargetKey(keyA), canonicalA);
  assert.deepEqual(migrated.settings.serverProfileTarget, canonicalA);
  assert.equal(migrated.settings.serverProfiles[keyA].profileId, 'server-a');

  const switched = await runtime.saveSettings({ssh: targetB});
  assert.deepEqual(switched.settings.serverProfileTarget, normalizeSshTarget(targetB));
  assert.equal(switched.settings.serverProfile.profileId, 'default');
  assert.equal(switched.settings.serverProfile.workerRepoRoot, '', 'a new SSH target must not inherit target A paths');
  assert.notEqual(switched.settings.serverProfileScopeId, migrated.settings.serverProfileScopeId);
  assert.notEqual(switched.settings.serverProfileManagedRoot, migrated.settings.serverProfileManagedRoot);
  assert.match(switched.settings.serverProfileManagedRoot, new RegExp(`/targets/${switched.settings.serverProfileScopeId}/default$`));

  const editedA = {...legacyA, workerRepoRoot: '/srv/work-a/platform-v2'};
  const stillB = await runtime.saveSettings({
    ssh: targetB,
    serverProfile: editedA,
    serverProfileTarget: targetA,
  });
  assert.equal(stillB.settings.serverProfile.workerRepoRoot, '', 'saving an A-scoped form while selecting B must keep B isolated');

  const profileB = normalizeServerProfile({profileId: 'server-b', workerRepoRoot: '/srv/work-b/platform'});
  await runtime.saveSettings({serverProfile: profileB, serverProfileTarget: targetB});
  const restoredA = await runtime.saveSettings({ssh: targetA});
  assert.equal(restoredA.settings.serverProfile.workerRepoRoot, '/srv/work-a/platform-v2');
  const restoredB = await runtime.saveSettings({ssh: targetB});
  assert.equal(restoredB.settings.serverProfile.workerRepoRoot, '/srv/work-b/platform');

  const saved = JSON.parse(fs.readFileSync(path.join(prefs, 'settings.json'), 'utf8'));
  assert.equal(Object.keys(saved.serverProfiles).length, 2);
  assert.equal(Object.hasOwn(saved.ssh, 'password'), false);
});

test('a legacy unscoped profile migrates only to the first configured SSH target', async t => {
  const prefs = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-server-profile-first-target-'));
  const legacy = normalizeServerProfile({profileId: 'legacy', workerRepoRoot: '/srv/legacy/platform'});
  fs.writeFileSync(path.join(prefs, 'settings.json'), `${JSON.stringify({serverProfile: legacy}, null, 2)}\n`);
  let runtime = new DesktopRuntime({userDataDir: prefs, resourcesDir: path.resolve(__dirname, '..'), version: 'test'});
  t.after(async () => { await runtime?.dispose(); fs.rmSync(prefs, {recursive: true, force: true}); });

  await runtime.saveSettings({hdcPath: '/opt/hdc'});
  assert.equal(JSON.parse(fs.readFileSync(path.join(prefs, 'settings.json'), 'utf8')).serverProfileLegacyPending, true);
  await runtime.dispose();
  runtime = new DesktopRuntime({userDataDir: prefs, resourcesDir: path.resolve(__dirname, '..'), version: 'test'});

  const first = {host: 'first.example.com', username: 'builder', port: 22, cwd: '/srv/first'};
  const second = {host: 'second.example.com', username: 'builder', port: 22, cwd: '/srv/second'};
  const firstState = await runtime.saveSettings({ssh: first});
  assert.equal(firstState.settings.serverProfile.workerRepoRoot, '/srv/legacy/platform');
  const secondState = await runtime.saveSettings({ssh: second});
  assert.equal(secondState.settings.serverProfile.workerRepoRoot, '');
});

test('inspect rejects a profile explicitly bound to another connected SSH target', async t => {
  const prefs = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-server-profile-cross-target-'));
  const runtime = new DesktopRuntime({userDataDir: prefs, resourcesDir: path.resolve(__dirname, '..'), version: 'test'});
  await runtime.getState();
  t.after(async () => { runtime.client = null; runtime.ssh = {state: 'disconnected', shellState: 'closed'}; await runtime.dispose(); fs.rmSync(prefs, {recursive: true, force: true}); });
  const targetA = {host: 'a.example.com', port: 22, username: 'builder', cwd: '/srv/a'};
  const targetB = {host: 'b.example.com', port: 22, username: 'builder', cwd: '/srv/b'};
  runtime.client = {};
  runtime.ssh = {state: 'connected', shellState: 'open', ...targetB};
  let executed = false;
  runtime.executeRemote = async () => { executed = true; throw new Error('must not execute'); };
  await assert.rejects(runtime.inspectServerProfile({profile: normalizeServerProfile({workerRepoRoot: '/srv/a/repo'}), target: targetA}), /拒绝.*其他目标/);
  assert.equal(executed, false);
});

test('inspect script is deterministic and read-only while reporting missing capabilities', posixFixture, () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-server-profile-readonly-'));
  try {
    const home = path.join(root, 'remote home'); fs.mkdirSync(home);
    const profile = normalizeServerProfile({profileId: 'missing', workerRepoRoot: path.join(root, 'missing repo')});
    const firstScript = buildServerProfileInspectScript(profile);
    assert.equal(firstScript, buildServerProfileInspectScript(profile));
    for (const mutator of ['mkdir ', 'chmod ', 'mv -', 'rm -', 'touch ']) assert.ok(!firstScript.includes(mutator), `inspect unexpectedly contains ${mutator}`);
    const before = fs.readdirSync(home);
    const result = runRemote(firstScript, home);
    assert.equal(result.exitCode, 0, result.stderr);
    const report = parseServerProfileInspect(result.stdout, profile);
    assert.equal(report.schemaVersion, 'server_profile_report/v1');
    assert.equal(report.summary.ready, false);
    assert.equal(report.summary.readyFor.codeAnalysis, false);
    assert.equal(report.summary.readyFor.traceAnalysis, false);
    assert.equal(report.summary.readyFor.deviceBuild, false);
    assert.equal(report.checks.find(check => check.id === 'repo.worker').status, 'missing');
    assert.equal(report.checks.find(check => check.id === 'sources.external').status, 'skipped');
    assert.deepEqual(fs.readdirSync(home), before, 'inspection must not create its managed profile root');
  } finally { fs.rmSync(root, {recursive: true, force: true}); }
});

test('generated server scripts keep the Python 3.10 gate and stream runtime-library hashes', () => {
  const profile = normalizeServerProfile({workerRepoRoot: '/srv/heterogeneous-enable-web'});
  const scripts = [
    ['inspect', buildServerProfileInspectScript(profile)],
    ['prepare', buildServerProfilePrepareScript(profile)],
  ];

  for (const [kind, script] of scripts) {
    assert.match(script, /sys\.version_info >= \(3, 10\)/, `${kind} must accept the supported Python 3.10 server`);
    assert.doesNotMatch(script, /sys\.version_info >= \(3, 11\)/, `${kind} must not restore the old Python 3.11 gate`);
    assert.match(script, /def file_sha256\(path\):/, `${kind} must define the compatible hash helper`);
    assert.match(script, /stream\.read\(1024 \* 1024\)/, `${kind} must hash bounded chunks instead of loading a library at once`);
    assert.match(script, /digest\.update\(chunk\)/, `${kind} must feed each bounded chunk into SHA-256`);
    assert.match(script, /file_sha256\(path\)==item\.get\("sha256"\)/, `${kind} must use the helper for manifest verification`);
    assert.doesNotMatch(script, /hashlib\.file_digest/, `${kind} cannot use the Python 3.11 file_digest API`);
    assert.doesNotMatch(script, /read_bytes\(\)/, `${kind} cannot load a runtime library into memory to hash it`);
  }
});

test('fake SSH integration prepares a complete profile idempotently with a 0600 environment file', posixFixture, async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-server-profile-integration-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const remoteHome = path.join(root, 'remote home with spaces'); fs.mkdirSync(remoteHome);
  const profile = completeProfile(root);
  assert.ok(fs.existsSync(profile.pythonBin), `test needs Python at ${profile.pythonBin}`);
  const {runtime, prefs, commands} = await runtimeFixture(t, remoteHome);

  const beforePrepare = await runtime.inspectServerProfile({profile});
  assert.equal(beforePrepare.summary.readyFor.deviceBuild, false, 'dynamic builds require the managed hash registry');
  assert.equal(beforePrepare.checks.find(check => check.id === 'runtime.registry').status, 'missing');

  const first = await runtime.prepareServerProfile({profile});
  assert.equal(first.schemaVersion, 'server_profile_report/v1');
  assert.equal(first.summary.ready, true);
  assert.deepEqual(first.summary.readyFor, {codeAnalysis: true, traceAnalysis: true, deviceBuild: true});
  assert.ok(Object.values(first.capabilityMatrix.buildProfiles).every(item => item.ready));
  assert.equal(first.preparation.checks.find(check => check.id === 'runtime.registry').status, 'ready');

  const envFile = first.managedPaths.envFile;
  const registryFile = path.join(first.managedPaths.state, 'build-worker', 'runtime-library-source.json');
  assert.equal(fs.statSync(envFile).mode & 0o777, 0o600);
  assert.ok(fs.existsSync(registryFile));
  const environment = fs.readFileSync(envFile, 'utf8');
  assert.match(environment, new RegExp(`HETERO_REPO_ROOT="${profile.workerRepoRoot.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}"`));
  assert.ok(environment.includes(`HETERO_STATE_ROOT="${first.managedPaths.state}"`));
  assert.ok(environment.includes(`HETERO_CACHE_ROOT="${first.managedPaths.cache}"`));
  assert.ok(environment.includes(`HETERO_RUNTIME_ASSET_ROOT="${first.managedPaths.assets}"`));
  assert.ok(environment.includes(`HETERO_OPENHARMONY_SDK_ROOT="${profile.openHarmonySdkRoot}"`));
  assert.ok(environment.includes(`HETERO_RUNTIME_LIBRARY_ROOT="${profile.runtimeLibraryRoot}"`));
  assert.ok(environment.includes('HETERO_DEPLOYMENT_MODE="worker"'));
  assert.ok(environment.includes('HETERO_AGENT_ENGINE="legacy"'));
  assert.ok(!environment.includes('HETERO_ROOTLESS_RUNTIME_ROOT=""'));
  const firstEnvironment = environment;
  const firstRegistry = fs.readFileSync(registryFile, 'utf8');

  const second = await runtime.prepareServerProfile(profile); // raw payload remains backward compatible
  assert.equal(fs.readFileSync(envFile, 'utf8'), firstEnvironment);
  assert.equal(fs.readFileSync(registryFile, 'utf8'), firstRegistry);
  assert.equal(second.preparation.checks.find(check => check.id === 'runtime.registry').status, 'ready');
  assert.equal(fs.readdirSync(path.dirname(envFile)).some(name => name.includes('.tmp.')), false);

  const saved = JSON.parse(fs.readFileSync(path.join(prefs, 'settings.json'), 'utf8'));
  assert.deepEqual(saved.serverProfile, profile);
  assert.ok(!Object.hasOwn(saved.serverProfile, 'stateRoot'));
  assert.ok(!Object.hasOwn(saved.serverProfile, 'cacheRoot'));
  assert.ok(commands.length >= 5);
  assert.ok(commands.every(command => !command.includes('password')));

  const unavailableSource = {...profile, sourceRoots: [...profile.sourceRoots, path.join(root, 'missing product source')]};
  const sourceReport = await runtime.inspectServerProfile({profile: unavailableSource});
  assert.equal(sourceReport.checks.find(check => check.id === 'source.1').status, 'missing');
  assert.equal(sourceReport.checks.find(check => check.id === 'source.1').required, true);
  assert.equal(sourceReport.summary.readyFor.codeAnalysis, false, 'every explicitly configured source root gates code analysis');
  assert.equal(sourceReport.summary.readyFor.traceAnalysis, true, 'a source root does not gate raw trace conversion');
  assert.equal(sourceReport.summary.readyFor.deviceBuild, true, 'a source root does not gate experiment builds');
});

test('analysis environment initialization is automatic, target-scoped and de-duplicated', posixFixture, async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-server-profile-ensure-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const remoteHome = path.join(root, 'remote home'); fs.mkdirSync(remoteHome);
  const profile = completeProfile(root);
  const {runtime, commands} = await runtimeFixture(t, remoteHome);

  const [first, second] = await Promise.all([
    runtime.ensureServerProfile({profile}),
    runtime.ensureServerProfile({profile}),
  ]);
  assert.equal(first.preparation.prepared, true);
  assert.equal(second.managedPaths.envFile, first.managedPaths.envFile);
  assert.equal(commands.length, 2, 'one prepare and one follow-up inspection serve concurrent analysis requests');
  assert.ok(fs.existsSync(first.managedPaths.envFile));

  await assert.rejects(
    runtime.ensureServerProfile({profile: normalizeServerProfile({profileId: 'missing-repo'})}),
    /配置异构工作仓目录.*首次分析时自动初始化/,
  );
});

test('registry capability is evaluated per build profile instead of treating a valid subset as complete', posixFixture, async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-server-profile-subset-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const remoteHome = path.join(root, 'remote home'); fs.mkdirSync(remoteHome);
  const profile = completeProfile(root, {runtimeLibraries: RUNTIME_PROFILES.gpu_gles3});
  const {runtime} = await runtimeFixture(t, remoteHome);
  const report = await runtime.prepareServerProfile({profile});

  assert.equal(report.checks.find(check => check.id === 'runtime.registry').status, 'ready');
  assert.equal(report.checks.find(check => check.id === 'runtime.registry.gpu_gles3').status, 'ready');
  assert.equal(report.checks.find(check => check.id === 'runtime.registry.npu_nnrt').status, 'missing');
  assert.equal(report.capabilityMatrix.buildProfiles.gpu_gles3.ready, true);
  assert.equal(report.capabilityMatrix.buildProfiles.npu_nnrt.ready, false);
  assert.ok(report.capabilityMatrix.buildProfiles.npu_nnrt.missing.includes('runtime.registry.npu_nnrt'));
  assert.equal(report.summary.readyFor.deviceBuild, false);

  for (const library of RUNTIME_PROFILES.npu_nnrt) fakeAarch64Elf(path.join(profile.runtimeLibraryRoot, library));
  const refreshed = await runtime.prepareServerProfile({profile});
  assert.equal(refreshed.checks.find(check => check.id === 'runtime.registry.npu_nnrt').status, 'ready', 'prepare refreshes a valid subset when new known libraries appear');
  assert.equal(refreshed.capabilityMatrix.buildProfiles.gpu_gles3.ready, true);
  assert.equal(refreshed.capabilityMatrix.buildProfiles.npu_nnrt.ready, true);

  fs.appendFileSync(path.join(profile.runtimeLibraryRoot, 'libneural_network_core.so'), 'changed-after-registration');
  const changed = await runtime.inspectServerProfile({profile});
  assert.equal(changed.checks.find(check => check.id === 'runtime.registry').status, 'missing');
  assert.equal(changed.checks.find(check => check.id === 'runtime.registry.npu_nnrt').status, 'missing');
  assert.equal(changed.capabilityMatrix.buildProfiles.npu_nnrt.ready, false);
  assert.equal(changed.checks.find(check => check.id === 'runtime.registry.gpu_gles3').status, 'ready');
  assert.equal(changed.capabilityMatrix.buildProfiles.gpu_gles3.ready, true, 'an unrelated NPU hash change must not block the independently valid GLES profile');
});

test('build readiness requires Clang resource headers next to the configured bin directory', posixFixture, async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-server-profile-clang-headers-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const remoteHome = path.join(root, 'remote home'); fs.mkdirSync(remoteHome);
  const profile = completeProfile(root);
  const {runtime} = await runtimeFixture(t, remoteHome);
  fs.rmSync(path.join(path.dirname(profile.llvmBinRoot), 'lib'), {recursive: true, force: true});

  const report = await runtime.prepareServerProfile({profile});
  const check = report.checks.find(item => item.id === 'llvm.resource-headers');
  assert.equal(check.status, 'missing');
  assert.equal(report.capabilityMatrix.buildProfiles.native_c.ready, false);
  assert.ok(report.capabilityMatrix.buildProfiles.native_c.missing.includes('llvm.resource-headers'));
  assert.equal(report.summary.readyFor.codeAnalysis, true);
});

test('prepare reports committed initialization when the follow-up inspection fails', posixFixture, async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-server-profile-partial-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const remoteHome = path.join(root, 'remote-home'); fs.mkdirSync(remoteHome);
  const {runtime} = await runtimeFixture(t, remoteHome);
  const profile = normalizeServerProfile({profileId: 'partial'});
  let calls = 0;
  runtime.executeRemote = async ({command}) => {
    calls += 1;
    return calls === 1 ? runRemote(command, remoteHome) : {stdout: '', stderr: 'transient inspection failure', exitCode: 9};
  };
  const result = await runtime.prepareServerProfile({profile});
  assert.equal(result.preparation.prepared, true);
  assert.ok(fs.existsSync(result.managedPaths.envFile));
  assert.match(result.inspectionError, /已初始化并保存.*transient inspection failure/);
  assert.equal(result.summary, null);
});

test('desktop IPC allowlist exposes inspect and prepare without a generic runtime dispatcher', () => {
  const main = fs.readFileSync(path.resolve(__dirname, '../main.cjs'), 'utf8');
  assert.match(main, /'inspectServerProfile', 'prepareServerProfile', 'ensureServerProfile'/);
  assert.match(main, /'cancelRemoteExec'/);
  assert.match(main, /runtimeMethods\.has\(method\)/);
  assert.doesNotMatch(main, /runtime\[method\]\(payload\)[\s\S]*else\s+return\s+runtime\[method\]/);
});
