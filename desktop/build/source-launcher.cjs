'use strict';
// Source-checkout launcher state. User settings remain in Electron's normal
// HeterogeneousWorkbench AppData directory, not in this checkout.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {spawnSync} = require('node:child_process');
const {inspectElectron} = require('./prepare-electron.cjs');
const desktopRoot = path.resolve(__dirname, '..');

function dependencyFingerprint(root = desktopRoot, runtime = process) {
  const lock = JSON.parse(fs.readFileSync(path.join(root, 'package-lock.json'), 'utf8'));
  // App releases change these fields even when every installed dependency is
  // identical. Keep actual resolved dependency versions/integrity in the key.
  delete lock.name;
  delete lock.version;
  if (lock.packages?.['']) {
    delete lock.packages[''].name;
    delete lock.packages[''].version;
  }
  return crypto.createHash('sha256').update(JSON.stringify({
    lock, platform: runtime.platform, arch: runtime.arch,
    node: runtime.versions.node, abi: runtime.versions.modules,
  })).digest('hex');
}

function dependencyFiles(root = desktopRoot, platform = process.platform, arch = process.arch) {
  const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
  const files = [];
  for (const [name, version] of Object.entries({...pkg.dependencies, ...pkg.devDependencies})) {
    const file = path.join(root, 'node_modules', name, 'package.json');
    const installed = JSON.parse(fs.readFileSync(file, 'utf8'));
    if (installed.version !== version) throw new Error(`Dependency ${name}: expected ${version}, found ${installed.version}`);
    files.push(file);
  }
  // npm package completeness is independent of Electron's lazy binary install.
  files.push(path.join(root, 'node_modules/electron/install.js'));
  files.push(path.join(root, 'node_modules/electron/checksums.json'));
  files.push(path.join(root, 'node_modules/esbuild/bin/esbuild'));
  files.push(path.join(root, 'node_modules/electron-builder/cli.js'));
  if (platform === 'win32' || platform === 'darwin') {
    files.push(path.join(root, 'node_modules/node-pty/prebuilds', `${platform}-${arch}`, 'pty.node'));
    if (platform === 'win32') files.push(path.join(root, 'node_modules/node-pty/prebuilds', `${platform}-${arch}`, 'conpty.node'));
  }
  for (const file of files) if (!fs.statSync(file).isFile()) throw new Error(`Dependency file is missing: ${file}`);
  return files;
}

function statePath(root = desktopRoot) { return path.join(root, 'build/downloads/source-dependencies.json'); }
function packageStatePath(root = desktopRoot) { return path.join(root, 'build/downloads/source-packages.json'); }

function dependenciesReady(root = desktopRoot, runtime = process) {
  try {
    // Accept the old complete-runtime stamp during migration. Runtime repair
    // must never force a fresh npm ci when the locked packages remain installed.
    const state = JSON.parse(fs.readFileSync(fs.existsSync(packageStatePath(root)) ? packageStatePath(root) : statePath(root), 'utf8'));
    if (state.fingerprint !== dependencyFingerprint(root, runtime)) return false;
    dependencyFiles(root, runtime.platform, runtime.arch);
    return true;
  } catch { return false; }
}

function recordPackages(root = desktopRoot, runtime = process) {
  dependencyFiles(root, runtime.platform, runtime.arch);
  const target = packageStatePath(root);
  fs.mkdirSync(path.dirname(target), {recursive: true});
  fs.writeFileSync(target, JSON.stringify({fingerprint: dependencyFingerprint(root, runtime), recordedAt: new Date().toISOString()}, null, 2));
}

function recordDependencies(root = desktopRoot, runtime = process, options = {}) {
  dependencyFiles(root, runtime.platform, runtime.arch);
  const electron = inspectElectron(root, {...options, runtime});
  if (!electron.ready) throw new Error(`Electron runtime is not ready: ${electron.reason} Run node build/prepare-electron.cjs in desktop first.`);
  const target = statePath(root);
  fs.mkdirSync(path.dirname(target), {recursive: true});
  fs.writeFileSync(target, JSON.stringify({fingerprint: dependencyFingerprint(root, runtime), recordedAt: new Date().toISOString()}, null, 2));
}

function launch(root = desktopRoot, options = {}) {
  let executable = options.executable;
  if (!executable) {
    const electron = inspectElectron(root, options);
    if (!electron.ready) throw new Error(`Electron runtime is not ready: ${electron.reason} Run desktop/run-desktop.ps1 to prepare it.`);
    executable = electron.executable;
  }
  const env = {...(options.env || process.env)};
  delete env.ELECTRON_RUN_AS_NODE;
  const result = (options.spawnSync || spawnSync)(executable, [root], {cwd: root, env, stdio: 'inherit', windowsHide: false});
  if (result.error) throw result.error;
  if (result.signal) throw new Error(`Desktop exited on signal ${result.signal}`);
  return result.status ?? 1;
}

if (require.main === module) {
  try {
    const command = process.argv[2];
    if (command === '--check-deps') process.exitCode = dependenciesReady() ? 0 : 10;
    else if (command === '--record-packages') recordPackages();
    else if (command === '--record-deps') recordDependencies();
    else if (command === '--launch') process.exitCode = launch();
    else throw new Error('Expected --check-deps, --record-packages, --record-deps or --launch');
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}

module.exports = {dependencyFingerprint, dependencyFiles, dependenciesReady, recordPackages, recordDependencies, launch};
