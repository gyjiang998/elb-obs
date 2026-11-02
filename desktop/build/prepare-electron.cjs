'use strict';
// Electron 45 installs its npm package without downloading the executable.
// Prepare the locked, host-platform runtime explicitly before recording or
// launching it. The download worker uses the official downloader, extractor and
// bundled checksums, with network diagnostics and an offline archive path.
const fs = require('node:fs');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const {resolveNpmDownloadEnvironment} = require('./npm-download-config.cjs');
const desktopRoot = path.resolve(__dirname, '..');

function expectedExecutable(platform) {
  if (platform === 'win32') return 'electron.exe';
  if (platform === 'darwin') return 'Electron.app/Contents/MacOS/Electron';
  if (['linux', 'freebsd', 'openbsd'].includes(platform)) return 'electron';
  throw new Error(`Electron runtime is not supported on ${platform}.`);
}

function packageMetadata(root, runtime) {
  const electronRoot = path.join(root, 'node_modules/electron');
  const installed = JSON.parse(fs.readFileSync(path.join(electronRoot, 'package.json'), 'utf8'));
  const application = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
  const expectedVersion = application.devDependencies?.electron || application.dependencies?.electron;
  if (!expectedVersion || installed.version !== expectedVersion) {
    throw new Error(`Electron npm package version mismatch: expected ${expectedVersion}, found ${installed.version}. Run npm ci --include=dev in desktop first.`);
  }
  const relativeExecutable = expectedExecutable(runtime.platform);
  return {electronRoot, version: installed.version, relativeExecutable, executable: path.join(electronRoot, 'dist', relativeExecutable)};
}

function inspectElectron(root = desktopRoot, options = {}) {
  const runtime = options.runtime || process;
  try {
    const metadata = packageMetadata(root, runtime);
    const marker = fs.readFileSync(path.join(metadata.electronRoot, 'path.txt'), 'utf8').trim();
    if (marker !== metadata.relativeExecutable) throw new Error('Electron path.txt does not match this operating system.');
    const version = fs.readFileSync(path.join(metadata.electronRoot, 'dist/version'), 'utf8').trim().replace(/^v/, '');
    if (version !== metadata.version) throw new Error(`Electron executable version mismatch: expected ${metadata.version}, found ${version}.`);
    if (!fs.statSync(metadata.executable).isFile()) throw new Error('Electron executable is missing.');
    if (runtime.platform !== 'win32') fs.accessSync(metadata.executable, fs.constants.X_OK);
    const env = {...(options.env || process.env), ELECTRON_RUN_AS_NODE: '1'};
    delete env.NODE_OPTIONS;
    const result = (options.spawnSync || spawnSync)(metadata.executable, [
      '-p', 'JSON.stringify({version:process.versions.electron,platform:process.platform,arch:process.arch})',
    ], {cwd: root, env, encoding: 'utf8', windowsHide: true, timeout: 15000, maxBuffer: 1024 * 1024});
    if (result.error || result.status !== 0 || result.signal) {
      throw new Error(`Electron executable could not start: ${result.error?.message || result.stderr?.trim() || result.signal || `exit ${result.status}`}`);
    }
    const reported = JSON.parse(result.stdout.trim());
    if (reported.version !== metadata.version || reported.platform !== runtime.platform || reported.arch !== runtime.arch) {
      throw new Error(`Electron executable identity mismatch: expected ${metadata.version}/${runtime.platform}/${runtime.arch}, found ${reported.version}/${reported.platform}/${reported.arch}.`);
    }
    return {ready: true, ...metadata};
  } catch (error) { return {ready: false, reason: error.message}; }
}

function prepareElectron(root = desktopRoot, options = {}) {
  const runtime = options.runtime || process;
  const log = options.log || console.log;
  const existing = inspectElectron(root, options);
  if (existing.ready) {
    log(`Reusing Electron ${existing.version} (${runtime.platform}-${runtime.arch}).`);
    return existing;
  }
  const metadata = packageMetadata(root, runtime);
  log(`Preparing Electron ${metadata.version} (${runtime.platform}-${runtime.arch})...`);
  // Only completed and verified extraction may publish this generated marker.
  fs.rmSync(path.join(metadata.electronRoot, 'path.txt'), {force: true});
  const baseEnv = {...(options.env || process.env),
    ELECTRON_INSTALL_PLATFORM: runtime.platform, ELECTRON_INSTALL_ARCH: runtime.arch,
    npm_config_platform: runtime.platform, npm_config_arch: runtime.arch,
  };
  const resolveDownloadEnvironment = options.resolveNpmDownloadEnvironment || resolveNpmDownloadEnvironment;
  // Offline import has no network configuration to resolve. It should also
  // remain usable when npm proxy configuration itself is unavailable.
  const env = baseEnv.HETERO_ELECTRON_ARCHIVE ? baseEnv : resolveDownloadEnvironment(baseEnv, {log});
  // npm lifecycle exports the complete effective configuration, potentially
  // including registry credentials. Downloader-relevant values were normalized
  // above; do not pass the remaining npm/package configuration to the worker.
  for (const name of Object.keys(env)) {
    if (/^(?:npm_config_|npm_package_config_)/i.test(name)) delete env[name];
    else if (/^(?:NPM_TOKEN|NODE_AUTH_TOKEN)$/i.test(name)) delete env[name];
  }
  // Select this checkout's pinned host runtime and checksum manifest. Preserve
  // download proxy, mirror, and cache settings used on corporate networks.
  for (const name of ['ELECTRON_OVERRIDE_DIST_PATH', 'ELECTRON_SKIP_BINARY_DOWNLOAD',
    'electron_use_remote_checksums', 'npm_config_electron_use_remote_checksums']) delete env[name];
  for (const name of Object.keys(env)) {
    if (/^(?:electron_custom_version|npm_config_electron_custom_?version|npm_package_config_electron_custom_?version)$/i.test(name)) delete env[name];
  }
  const installer = path.join(__dirname, 'electron-download.cjs');
  for (const file of [installer, path.join(metadata.electronRoot, 'checksums.json')]) {
    if (!fs.statSync(file).isFile()) throw new Error(`Electron installer file is missing: ${file}. Run npm ci --include=dev in desktop first.`);
  }
  const result = (options.spawnSync || spawnSync)(options.nodeExecutable || process.execPath, [installer, metadata.electronRoot, runtime.platform, runtime.arch], {
    cwd: root, env, stdio: 'inherit', windowsHide: true,
  });
  if (result.error || result.status !== 0 || result.signal) {
    throw new Error(`Electron runtime preparation failed (${result.error?.message || result.signal || `exit ${result.status}`}). See the download or extraction error above. Configure HTTPS_PROXY for your network, or set HETERO_ELECTRON_ARCHIVE to the downloaded official ZIP, then run desktop/run-desktop.ps1 again. Installed npm dependencies are retained; do not create path.txt manually.`);
  }
  const prepared = inspectElectron(root, options);
  if (!prepared.ready) throw new Error(`Electron runtime verification failed: ${prepared.reason} Run desktop/run-desktop.ps1 again after checking the download output or executable access.`);
  log(`Electron ${prepared.version} is ready.`);
  return prepared;
}

if (require.main === module) {
  try { prepareElectron(); } catch (error) { console.error(error.message); process.exitCode = 1; }
}

module.exports = {expectedExecutable, inspectElectron, prepareElectron};
