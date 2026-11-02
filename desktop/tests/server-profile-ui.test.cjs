const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const assert = require('node:assert/strict');

const root = path.resolve(__dirname, '..');
const read = (name) => fs.readFileSync(path.join(root, name), 'utf8');
const {normalizeSshTarget, sameSshTarget, sshTargetLabel} = require('../renderer/server-profile-target.cjs');

test('server environment page exposes the desktop resource contract and three real actions', () => {
  const html = read('renderer/index.html');
  const source = read('renderer/server-profile.js');

  for (const id of [
    'server-profile-page', 'server-profile-configure', 'server-profile-inspect',
    'server-profile-prepare', 'server-profile-agent', 'server-profile-checks',
  ]) assert.match(`${html}\n${source}`, new RegExp(`id=["']${id}["']`), `${id} is part of the renderer contract`);

  assert.match(source, /invoke\('inspectServerProfile', \{ profile, target \}\)/);
  assert.match(source, /invoke\('prepareServerProfile', \{ profile, target \}\)/);
  assert.match(source, /submitTask\(agentPrompt\(\)\)/);
  assert.match(source, /未连接 SSH/);
  assert.match(source, /server_profile_report\/v1/);
});

test('editable profile contains only user-provided server resources', () => {
  const html = read('renderer/index.html');
  const source = read('renderer/server-profile.js');
  const renderer = read('renderer/renderer.js');

  for (const id of [
    'server-worker-repo-root', 'server-source-roots', 'server-openharmony-sdk-root',
    'server-llvm-bin-root', 'server-glslang-bin', 'server-trace-streamer',
    'server-runtime-library-root', 'server-python-bin',
  ]) assert.match(html, new RegExp(`id="${id}"`));

  const settingsSection = html.match(/<section id="settings-server-profile"[\s\S]*?<\/section>/)?.[0] || '';
  for (const internalName of ['HETERO_USER_HOME', 'HETERO_STATE_ROOT', 'HETERO_CACHE_ROOT', 'HETERO_RUNTIME_ASSET_ROOT']) {
    assert.doesNotMatch(settingsSection, new RegExp(internalName), `${internalName} must not become an editable setting`);
  }
  assert.match(settingsSection, /远端绝对路径并支持 ~\//);
  assert.match(settingsSection, /PATH 命令或 \/、~\/ 路径/);
  assert.match(source, /EGL.*OpenGLES.*Vulkan Headers.*musl.*Graphic.*Multimedia.*AI/);
  assert.match(renderer, /serverProfile: serverProfileStudio\.readProfile\(\)/);
  assert.match(renderer, /serverProfileTarget: serverProfileStudio\.readTarget\(\)/);
});

test('managed directories remain derived under a profile-specific remote root', () => {
  const source = read('renderer/server-profile.js');
  assert.match(source, /\$HOME\/\.local\/share\/heterogeneous-workbench\/server-profiles\/targets\/\$\{scopeId\}\/\$\{profileId \|\| 'default'\}/);
  for (const name of ['state', 'cache', 'assets', 'tmp', 'logs', 'run', 'envFile']) assert.match(source, new RegExp(`\\['${name}'`));
});

test('preflight identity is bound to the exact SSH host, port, user and cwd', () => {
  const inspected = normalizeSshTarget({host: 'Build.EXAMPLE.com', port: '2222', username: 'worker', cwd: '/srv/a'});
  assert.deepEqual(inspected, {host: 'build.example.com', port: 2222, username: 'worker', cwd: '/srv/a'});
  assert.equal(sshTargetLabel(inspected), 'worker@build.example.com:2222 · /srv/a');
  assert.equal(sameSshTarget(inspected, {...inspected}), true);
  for (const changed of [
    {...inspected, host: 'other.example.com'},
    {...inspected, port: 22},
    {...inspected, username: 'another-user'},
    {...inspected, cwd: '/srv/b'},
  ]) assert.equal(sameSshTarget(inspected, changed), false, `must invalidate ${JSON.stringify(changed)}`);
  assert.equal(sameSshTarget(inspected, null), false);
});

test('renderer clears cross-target reports and marks same-target offline data as historical', () => {
  const source = read('renderer/server-profile.js');
  assert.match(source, /report\?\.target && !sameSshTarget\(report\.target, selectedTarget\(\)\)/);
  assert.match(source, /report = null;/);
  assert.match(source, /SSH 目标在预检期间发生变化，已丢弃该结果/);
  assert.match(source, /上次检查就绪 · 当前离线/);
  assert.match(source, /`上次\$\{statusText\(normalized\)\}`/);
  const sharedTargetContract = read('runtime/server-profile-target.cjs');
  for (const field of ['host', 'port', 'username', 'cwd']) assert.match(sharedTargetContract, new RegExp(`\\b${field}\\b`));
  assert.match(read('renderer/server-profile-target.cjs'), /runtime\/server-profile-target\.cjs/);
  assert.match(source, /syncSettings\(settings/);
});
