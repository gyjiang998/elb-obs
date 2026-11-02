'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {prepareElectron, inspectElectron} = require('../build/prepare-electron.cjs');

function write(file, value) {
  fs.mkdirSync(path.dirname(file), {recursive: true});
  fs.writeFileSync(file, typeof value === 'string' ? value : JSON.stringify(value));
}

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-electron-runtime-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const electronRoot = path.join(root, 'node_modules/electron');
  write(path.join(root, 'package.json'), {devDependencies: {electron: '45.0.0'}});
  write(path.join(electronRoot, 'package.json'), {name: 'electron', version: '45.0.0'});
  write(path.join(electronRoot, 'install.js'), '// official installer fixture');
  write(path.join(electronRoot, 'checksums.json'), {});
  let installs = 0, probes = 0;
  const calls = [];
  function install() {
    write(path.join(electronRoot, 'path.txt'), 'electron.exe');
    write(path.join(electronRoot, 'dist/version'), '45.0.0');
    write(path.join(electronRoot, 'dist/electron.exe'), 'valid executable fixture');
  }
  const options = {runtime: {platform: 'win32', arch: 'x64'}, nodeExecutable: 'node-fixture', log: () => {},
    resolveNpmDownloadEnvironment: env => env,
    spawnSync: (...args) => {
      calls.push(args);
      if (args[0] === 'node-fixture') { installs++; install(); return {status: 0}; }
      probes++;
      if (fs.readFileSync(args[0], 'utf8') !== 'valid executable fixture') return {status: null, error: new Error('ENOEXEC')};
      return {status: 0, stdout: JSON.stringify({version: '45.0.0', platform: 'win32', arch: 'x64'})};
    },
  };
  return {root, electronRoot, options, calls, install, counts: () => ({installs, probes})};
}

test('fresh npm package downloads Electron explicitly and reuses the verified executable', t => {
  const f = fixture(t);
  assert.equal(inspectElectron(f.root, f.options).ready, false);
  assert.equal(prepareElectron(f.root, f.options).ready, true);
  assert.equal(prepareElectron(f.root, f.options).ready, true);
  assert.deepEqual(f.counts(), {installs: 1, probes: 2});
  const installation = f.calls[0];
  assert.equal(installation[0], 'node-fixture');
  assert.deepEqual(installation[1], [path.resolve(__dirname, '../build/electron-download.cjs'), f.electronRoot, 'win32', 'x64']);
});

test('missing marker, missing binary, version mismatch, and unusable executable are independently repaired', t => {
  const f = fixture(t); f.install();
  for (const corrupt of [
    () => fs.unlinkSync(path.join(f.electronRoot, 'path.txt')),
    () => fs.unlinkSync(path.join(f.electronRoot, 'dist/electron.exe')),
    () => write(path.join(f.electronRoot, 'dist/version'), '44.0.0'),
    () => write(path.join(f.electronRoot, 'dist/electron.exe'), 'corrupt executable'),
    () => write(path.join(f.electronRoot, 'path.txt'), '../outside.exe'),
  ]) {
    corrupt();
    assert.equal(prepareElectron(f.root, f.options).ready, true);
  }
  assert.equal(f.counts().installs, 5);
});

test('installer uses the pinned host platform and checksum manifest while retaining network settings', t => {
  const f = fixture(t);
  const env = {ELECTRON_INSTALL_PLATFORM: 'darwin', ELECTRON_INSTALL_ARCH: 'arm64', npm_config_platform: 'darwin', npm_config_arch: 'arm64',
    ELECTRON_OVERRIDE_DIST_PATH: 'wrong/runtime', ELECTRON_SKIP_BINARY_DOWNLOAD: '1', electron_use_remote_checksums: '1', npm_config_electron_use_remote_checksums: '1',
    ELECTRON_MIRROR: 'https://mirror.example/', HTTPS_PROXY: 'http://proxy.example:8080', electron_config_cache: 'D:\\cache', ELECTRON_RUN_AS_NODE: '1',
    ELECTRON_CUSTOM_VERSION: '44.0.0', npm_config_electron_custom_version: '44.0.1', NPM_CONFIG_ELECTRON_CUSTOMVERSION: '44.0.2',
    HETERO_ELECTRON_ARCHIVE: 'D:\\runtime downloads\\electron.zip'};
  prepareElectron(f.root, {...f.options, env});
  const installerEnv = f.calls[0][2].env;
  assert.equal(installerEnv.ELECTRON_INSTALL_PLATFORM, 'win32');
  assert.equal(installerEnv.ELECTRON_INSTALL_ARCH, 'x64');
  assert.equal(installerEnv.npm_config_platform, undefined);
  assert.equal(installerEnv.npm_config_arch, undefined);
  for (const name of ['ELECTRON_OVERRIDE_DIST_PATH', 'ELECTRON_SKIP_BINARY_DOWNLOAD', 'electron_use_remote_checksums', 'npm_config_electron_use_remote_checksums', 'ELECTRON_CUSTOM_VERSION', 'npm_config_electron_custom_version', 'NPM_CONFIG_ELECTRON_CUSTOMVERSION']) assert.equal(installerEnv[name], undefined);
  for (const name of ['ELECTRON_MIRROR', 'HTTPS_PROXY', 'electron_config_cache', 'HETERO_ELECTRON_ARCHIVE']) assert.equal(installerEnv[name], env[name]);
  assert.equal(env.ELECTRON_INSTALL_PLATFORM, 'darwin');
  const probe = f.calls[1];
  assert.equal(probe[2].env.ELECTRON_RUN_AS_NODE, '1');
  assert.equal(probe[2].timeout, 15000);
});

test('npm download settings are resolved before starting the isolated runtime worker', t => {
  const f = fixture(t);
  let input;
  prepareElectron(f.root, {...f.options, env: {npm_config_registry: 'https://packages.example/'},
    resolveNpmDownloadEnvironment: env => {
      input = env;
      return {...env, HTTPS_PROXY: 'http://proxy.example:8080', ELECTRON_MIRROR: 'https://mirror.example/electron/',
        npm_config_registry: 'https://registry-user:registry-password@packages.example/',
        'npm_config_//packages.example/:_authToken': 'registry-secret', NPM_TOKEN: 'npm-secret', NODE_AUTH_TOKEN: 'node-secret',
        npm_token: 'lower-npm-secret', Node_Auth_Token: 'mixed-node-secret'};
    },
  });
  assert.equal(input.npm_config_registry, 'https://packages.example/');
  const workerEnv = f.calls[0][2].env;
  assert.equal(workerEnv.HTTPS_PROXY, 'http://proxy.example:8080');
  assert.equal(workerEnv.ELECTRON_MIRROR, 'https://mirror.example/electron/');
  assert.equal(workerEnv.npm_config_registry, undefined);
  assert.equal(workerEnv['npm_config_//packages.example/:_authToken'], undefined);
  assert.equal(workerEnv.NPM_TOKEN, undefined);
  assert.equal(workerEnv.NODE_AUTH_TOKEN, undefined);
  assert.equal(workerEnv.npm_token, undefined);
  assert.equal(workerEnv.Node_Auth_Token, undefined);
});

test('offline Electron archive bypasses npm environment resolution', t => {
  const f = fixture(t);
  prepareElectron(f.root, {...f.options, env: {HETERO_ELECTRON_ARCHIVE: 'C:\\Downloads\\electron.zip'},
    resolveNpmDownloadEnvironment: () => assert.fail('offline preparation must not resolve npm network config'),
  });
  assert.equal(f.calls[0][2].env.HETERO_ELECTRON_ARCHIVE, 'C:\\Downloads\\electron.zip');
});

test('offline installer failure preserves npm files and returns a repair instruction without fake marker', t => {
  const f = fixture(t);
  assert.throws(() => prepareElectron(f.root, {...f.options, spawnSync: () => ({status: 1})}), /preparation failed.*HTTPS_PROXY.*HETERO_ELECTRON_ARCHIVE.*dependencies are retained/);
  assert.equal(fs.existsSync(path.join(f.electronRoot, 'path.txt')), false);
  assert.equal(fs.existsSync(path.join(f.electronRoot, 'package.json')), true);
  assert.throws(() => prepareElectron(f.root, {...f.options, spawnSync: () => ({error: new Error('cannot start node')})}), /cannot start node/);
});

test('installer success without usable files and mismatching runtime identity never count as ready', t => {
  const f = fixture(t);
  assert.throws(() => prepareElectron(f.root, {...f.options, spawnSync: () => ({status: 0})}), /runtime verification failed/);
  f.install();
  const mismatch = {...f.options, spawnSync: executable => {
    if (executable === 'node-fixture') { f.install(); return {status: 0}; }
    return {status: 0, stdout: JSON.stringify({version: '44.0.0', platform: 'win32', arch: 'arm64'})};
  }};
  assert.equal(inspectElectron(f.root, mismatch).ready, false);
  assert.throws(() => prepareElectron(f.root, mismatch), /identity mismatch/);
});

test('unexpected npm package version cannot download an unpinned runtime', t => {
  const f = fixture(t);
  write(path.join(f.electronRoot, 'package.json'), {name: 'electron', version: '44.0.0'});
  assert.throws(() => prepareElectron(f.root, f.options), /npm package version mismatch/);
  assert.equal(f.counts().installs, 0);
});
