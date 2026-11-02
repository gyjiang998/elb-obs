'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const {dependencyFingerprint, dependenciesReady, recordPackages, recordDependencies, launch} = require('../build/source-launcher.cjs');
const {prepareElectron} = require('../build/prepare-electron.cjs');
const {preparePython, verifiedRuntime} = require('../build/prepare-python.cjs');

function temporary(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-source-update-'));
  t.after(() => fs.rmSync(directory, {recursive: true, force: true}));
  return directory;
}
function write(filename, content) { fs.mkdirSync(path.dirname(filename), {recursive: true}); fs.writeFileSync(filename, typeof content === 'string' ? content : JSON.stringify(content)); }
function dependencyFixture(t) {
  const root = temporary(t);
  const pkg = {name: 'app', version: '0.1.27', dependencies: {'node-pty': '1.1.0'}, devDependencies: {electron: '45.0.0', esbuild: '0.28.2', 'electron-builder': '26.18.0'}};
  const lock = {name: pkg.name, version: pkg.version, lockfileVersion: 3, packages: {'': pkg}};
  for (const [name, version] of Object.entries({...pkg.dependencies, ...pkg.devDependencies})) {
    lock.packages[`node_modules/${name}`] = {version, integrity: `integrity-${name}`};
    write(path.join(root, 'node_modules', name, 'package.json'), {name, version});
  }
  write(path.join(root, 'package.json'), pkg);
  write(path.join(root, 'package-lock.json'), lock);
  for (const filename of ['electron/dist/electron.exe', 'esbuild/bin/esbuild', 'electron-builder/cli.js', 'node-pty/prebuilds/win32-x64/pty.node', 'node-pty/prebuilds/win32-x64/conpty.node']) write(path.join(root, 'node_modules', filename), 'runtime fixture');
  write(path.join(root, 'node_modules/electron/path.txt'), 'electron.exe');
  write(path.join(root, 'node_modules/electron/dist/version'), '45.0.0');
  write(path.join(root, 'node_modules/electron/install.js'), '// Official installer fixture');
  write(path.join(root, 'node_modules/electron/checksums.json'), {});
  const probe = {spawnSync: () => ({status: 0, stdout: JSON.stringify({version: '45.0.0', platform: 'win32', arch: 'x64'})})};
  return {root, pkg, lock, probe, runtime: {platform: 'win32', arch: 'x64', versions: {node: '24.0.0', modules: '137'}}};
}

test('source dependency cache ignores app release versions but tracks resolved dependencies', t => {
  const f = dependencyFixture(t);
  assert.equal(dependenciesReady(f.root, f.runtime), false);
  recordDependencies(f.root, f.runtime, f.probe);
  const previous = dependencyFingerprint(f.root, f.runtime);
  f.pkg.version = '0.1.28'; f.lock.version = f.pkg.version; f.lock.packages[''].version = f.pkg.version;
  write(path.join(f.root, 'package.json'), f.pkg); write(path.join(f.root, 'package-lock.json'), f.lock);
  assert.equal(dependencyFingerprint(f.root, f.runtime), previous);
  assert.equal(dependenciesReady(f.root, f.runtime), true);
  f.lock.packages['node_modules/electron'].version = '45.0.1';
  write(path.join(f.root, 'package-lock.json'), f.lock);
  assert.notEqual(dependencyFingerprint(f.root, f.runtime), previous);
  assert.equal(dependenciesReady(f.root, f.runtime), false);
});

test('source dependency cache detects missing binaries, changed runtime and incomplete installation', t => {
  const f = dependencyFixture(t); recordDependencies(f.root, f.runtime, f.probe);
  assert.equal(dependenciesReady(f.root, {...f.runtime, versions: {node: '25.0.0', modules: '141'}}), false);
  fs.unlinkSync(path.join(f.root, 'node_modules/node-pty/prebuilds/win32-x64/conpty.node'));
  assert.equal(dependenciesReady(f.root, f.runtime), false);
  assert.throws(() => recordDependencies(f.root, f.runtime, f.probe), /ENOENT/);
});

test('fresh Electron npm package is recorded before binary download and download failures retain package cache', t => {
  const f = dependencyFixture(t);
  fs.rmSync(path.join(f.root, 'node_modules/electron/path.txt'));
  fs.rmSync(path.join(f.root, 'node_modules/electron/dist'), {recursive: true});
  recordPackages(f.root, f.runtime);
  assert.equal(dependenciesReady(f.root, f.runtime), true);
  assert.throws(() => recordDependencies(f.root, f.runtime, f.probe), /Electron runtime is not ready/);
  assert.equal(fs.existsSync(path.join(f.root, 'build/downloads/source-dependencies.json')), false);
  assert.throws(() => prepareElectron(f.root, {runtime: f.runtime, log: () => {}, spawnSync: () => ({status: 1})}), /runtime preparation failed/);
  assert.equal(dependenciesReady(f.root, f.runtime), true);
  assert.equal(fs.existsSync(path.join(f.root, 'build/downloads/source-dependencies.json')), false);
});

test('missing Electron marker or executable does not invalidate npm package cache', t => {
  const f = dependencyFixture(t); recordDependencies(f.root, f.runtime, f.probe);
  fs.unlinkSync(path.join(f.root, 'node_modules/electron/path.txt'));
  assert.equal(dependenciesReady(f.root, f.runtime), true);
  fs.unlinkSync(path.join(f.root, 'node_modules/electron/dist/electron.exe'));
  assert.equal(dependenciesReady(f.root, f.runtime), true);
  assert.throws(() => recordDependencies(f.root, f.runtime, f.probe), /Electron runtime is not ready/);
});

test('source launcher preserves literal path and returns app failure without Node-mode environment', () => {
  const inherited = {ELECTRON_RUN_AS_NODE: '1', KEEP_SETTING: 'yes'};
  let invocation;
  const result = launch('C:\\work space\\测试', {executable: 'electron.exe', env: inherited, spawnSync: (...args) => { invocation = args; return {status: 7}; }});
  assert.equal(result, 7);
  assert.deepEqual(invocation[1], ['C:\\work space\\测试']);
  assert.equal(invocation[2].env.ELECTRON_RUN_AS_NODE, undefined);
  assert.equal(invocation[2].env.KEEP_SETTING, 'yes');
  assert.equal(inherited.ELECTRON_RUN_AS_NODE, '1');
  assert.throws(() => launch('.', {executable: 'electron', spawnSync: () => ({error: new Error('cannot launch')})}), /cannot launch/);
});

function pythonFixture(t) {
  const root = temporary(t), bytes = Buffer.from('verified archive fixture');
  let downloads = 0, extracts = 0;
  const config = {
    downloads: path.join(root, 'downloads'), output: path.join(root, 'resources/python-win-x64'), log: () => {},
    distribution: {version: '3.13.17', url: 'https://example.invalid/python.zip', sha256: crypto.createHash('sha256').update(bytes).digest('hex')},
    download: destination => { downloads++; fs.writeFileSync(destination, bytes); },
    extract: async (_source, destination) => { extracts++; for (const filename of ['python.exe', 'python313.dll', 'python313.zip', 'LICENSE.txt']) write(path.join(destination, filename), `fixture ${filename}`); },
  };
  return {root, config, counts: () => ({downloads, extracts}), archive: path.join(config.downloads, 'python-3.13.17-embed-amd64.zip')};
}

test('Python is downloaded/extracted once and verified files are reused without network', async t => {
  const f = pythonFixture(t);
  await preparePython(f.config); await preparePython(f.config);
  assert.deepEqual(f.counts(), {downloads: 1, extracts: 1});
  assert.equal(verifiedRuntime(f.config.output, f.config.distribution), true);
  fs.unlinkSync(f.archive);
  await preparePython(f.config);
  assert.deepEqual(f.counts(), {downloads: 1, extracts: 1});
});

test('damaged Python DLL and import path are restored from the verified cached archive', async t => {
  const f = pythonFixture(t); await preparePython(f.config);
  write(path.join(f.config.output, 'python313.dll'), 'damaged');
  await preparePython(f.config);
  assert.deepEqual(f.counts(), {downloads: 1, extracts: 2});
  write(path.join(f.config.output, 'python313._pth'), 'wrong path');
  await preparePython(f.config);
  assert.deepEqual(f.counts(), {downloads: 1, extracts: 3});
  assert.equal(verifiedRuntime(f.config.output, f.config.distribution), true);
});

test('damaged Python archive is replaced before extraction, checksum failure never installs', async t => {
  const f = pythonFixture(t); await preparePython(f.config);
  fs.unlinkSync(path.join(f.config.output, 'python.exe'));
  fs.writeFileSync(f.archive, 'damaged archive');
  await preparePython(f.config);
  assert.deepEqual(f.counts(), {downloads: 2, extracts: 2});
  fs.unlinkSync(path.join(f.config.output, 'python.exe'));
  fs.unlinkSync(f.archive);
  await assert.rejects(preparePython({...f.config, download: target => fs.writeFileSync(target, 'incorrect download')}), /checksum mismatch/);
  assert.equal(fs.existsSync(f.archive), false);
  assert.equal(fs.readdirSync(f.config.downloads).some(name => name.includes('.partial')), false);
});

test('Python extraction failure preserves prior resources and cleans only generated staging', async t => {
  const f = pythonFixture(t); await preparePython(f.config);
  write(path.join(f.config.output, 'python313.dll'), 'old damaged DLL');
  write(path.join(f.root, 'user-data/capture.txt'), 'keep this capture');
  await assert.rejects(preparePython({...f.config, extract: async () => { throw new Error('extraction failed'); }}), /extraction failed/);
  assert.equal(fs.readFileSync(path.join(f.config.output, 'python313.dll'), 'utf8'), 'old damaged DLL');
  assert.equal(fs.readFileSync(path.join(f.root, 'user-data/capture.txt'), 'utf8'), 'keep this capture');
  assert.equal(fs.readdirSync(path.dirname(f.config.output)).some(name => name.includes('.partial-')), false);
});
