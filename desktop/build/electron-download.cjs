'use strict';
// Keep the npm package's checksum authority, cache layout, and native extractor.
const fs = require('node:fs');
const path = require('node:path');
const {createHash} = require('node:crypto');
const {createRequire} = require('node:module');
const {pathToFileURL} = require('node:url');
const {setTimeout: sleep} = require('node:timers/promises');

const DEFAULT_MIRROR = 'https://github.com/electron/electron/releases/download/';
const FALLBACK_MIRROR = 'https://npmmirror.com/mirrors/electron/';
const DOWNLOAD_TIMEOUT_MS = 600000;

function mirrorAliases(name) {
  const snake = name.replace(/([a-z])([A-Z])/g, '$1_$2').toLowerCase();
  return [...new Set([`npm_config_electron_${name.toLowerCase()}`, `NPM_CONFIG_ELECTRON_${snake.toUpperCase()}`,
    `npm_config_electron_${snake}`, `npm_package_config_electron_${name}`,
    `npm_package_config_electron_${snake}`, `ELECTRON_${snake.toUpperCase()}`])];
}

function mirrorSetting(env, name) {
  return mirrorAliases(name).map(key => env[key]).find(Boolean);
}

function networkEnvironment(input) {
  const env = {...input};
  for (const name of ['HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY']) {
    if (env[name] === undefined && env[name.toLowerCase()] === undefined && env[`GLOBAL_AGENT_${name}`]) {
      env[name] = env[`GLOBAL_AGENT_${name}`];
    }
  }
  if (['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'].some(name => env[name])) {
    env.ELECTRON_GET_USE_PROXY = '1';
  }
  for (const name of mirrorAliases('customVersion')) delete env[name];
  for (const name of ['electron_use_remote_checksums', 'npm_config_electron_use_remote_checksums']) delete env[name];
  return env;
}

function redact(value, env = process.env) {
  let text = String(value);
  // Error messages may repeat proxy credentials outside the original URL.
  const secrets = [];
  for (const [name, value] of Object.entries(env)) {
    if (!/(proxy|mirror)/i.test(name) || !value) continue;
    try {
      const url = new URL(value);
      secrets.push(url.username, url.password, ...url.searchParams.values());
      if (url.username) secrets.push(decodeURIComponent(url.username));
      if (url.password) secrets.push(decodeURIComponent(url.password));
    } catch { /* Non-URL settings, including NO_PROXY. */ }
  }
  text = text.replace(/\b(?:https?|socks[45]?):\/\/[^\s"'<>]+/gi, raw => {
    try {
      const url = new URL(raw);
      url.username = ''; url.password = ''; url.search = ''; url.hash = '';
      return url.toString();
    } catch { return '[redacted URL]'; }
  });
  for (const secret of secrets.filter(Boolean).sort((a, b) => b.length - a.length)) {
    text = text.split(secret).join('[redacted]');
  }
  return text;
}

function errorChain(error) {
  const seen = new Set(), result = [];
  function visit(item) {
    if (!item || seen.has(item) || result.length >= 20) return;
    seen.add(item); result.push(item);
    visit(item.cause);
    if (Array.isArray(item.errors)) item.errors.forEach(visit);
  }
  visit(error);
  return result;
}

function formatError(error, env = process.env) {
  return errorChain(error).map((item, index) => {
    const status = item.response?.status || item.statusCode || item.status;
    const details = [item.code, status ? `HTTP ${status}` : '', item.syscall, item.hostname, item.address].filter(Boolean);
    return redact(`${index ? 'Caused by: ' : ''}${item.name || 'Error'}: ${item.message || item}${details.length ? ` [${details.join(', ')}]` : ''}`, env);
  }).join('\n');
}

function classifiedError(stage, message, cause) {
  const error = new Error(message, {cause});
  error.stage = stage;
  return error;
}

function downloadFailure(error) {
  const chain = errorChain(error);
  if (chain.some(item => /checksum|sha-?256/i.test(`${item.name} ${item.message}`))) return {network: false, retry: false};
  if (chain.some(item => /^(?:EACCES|EPERM|ENOENT|ENOSPC|EROFS|EISDIR|ENOTDIR|ENOTEMPTY|EMFILE|ENFILE|EIO|EINVAL)$/.test(item.code || ''))) {
    return {network: false, retry: false};
  }
  const status = chain.map(item => item.response?.status || item.statusCode || item.status).find(value => Number.isInteger(value));
  if (status) return {network: true, retry: status === 408 || status === 429 || status >= 500};
  const network = chain.some(item => /^(?:EAI_AGAIN|ENOTFOUND|ECONNRESET|ECONNREFUSED|ECONNABORTED|ETIMEDOUT|EHOSTUNREACH|ENETUNREACH|EPIPE|UND_ERR_|ERR_TLS_|ERR_SSL_|CERT_|DEPTH_ZERO_|SELF_SIGNED_|UNABLE_TO_)/.test(item.code || '') ||
    /fetch failed|network|socket|timed? ?out|timeout|certificate|TLS|SSL|response body is empty/i.test(item.message || '') || item.name === 'TimeoutError');
  const tls = chain.some(item => /CERT|TLS|SSL|SELF_SIGNED|UNABLE_TO_VERIFY/i.test(`${item.code} ${item.message}`));
  return {network, retry: network && !tls};
}

function sourcesFor(metadata, env) {
  const mirrorName = metadata.version.includes('nightly') ? 'nightlyMirror' : 'mirror';
  const dedicatedMirror = mirrorName === 'mirror' ? env.HETERO_ELECTRON_MIRROR : undefined;
  const mirror = dedicatedMirror || mirrorSetting(env, mirrorName);
  const customDir = mirrorSetting(env, 'customDir');
  const customFilename = mirrorSetting(env, 'customFilename');
  const dir = (customDir || `v${metadata.version}`).replace('{{ version }}', metadata.version);
  const filename = customFilename || metadata.filename;
  const defaultBase = mirrorName === 'nightlyMirror' ? 'https://github.com/electron/nightlies/releases/download/' : DEFAULT_MIRROR;
  const explicit = Boolean(mirror || customDir || customFilename);
  const source = {url: `${(mirror || defaultBase).replace(/\/?$/, '/')}${dir}/${filename}`, explicit};
  return explicit ? [source] : [source, {url: `${FALLBACK_MIRROR}v${metadata.version}/${metadata.filename}`, explicit: false}];
}

async function withDownloadEnvironment(env, callback) {
  // @electron/get reads process.env itself. This worker is an isolated process;
  // restore the relevant settings as well for callers importing this helper.
  const names = new Set(['DEBUG', 'ELECTRON_GET_USE_PROXY', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'no_proxy',
    ...mirrorAliases('customVersion')]);
  const previous = new Map([...names].map(name => [name, process.env[name]]));
  for (const name of names) {
    if (env[name] === undefined) delete process.env[name]; else process.env[name] = env[name];
  }
  // Upstream debug output includes unredacted URLs and proxy causes. The
  // worker emits its own redacted diagnostics for these namespaces.
  if (env.DEBUG) process.env.DEBUG = `${env.DEBUG},-@electron/get:*`;
  try { return await callback(); }
  finally {
    for (const [name, value] of previous) {
      if (value === undefined) delete process.env[name]; else process.env[name] = value;
    }
  }
}

async function downloadRuntime(metadata, options = {}) {
  const env = networkEnvironment(options.env || process.env);
  const log = message => (options.log || console.log)(redact(message, env));
  const sources = sourcesFor(metadata, env);
  async function download(downloadArtifact) {
    for (let index = 0; index < sources.length; index++) {
      const source = sources[index];
      for (let attempt = 1; attempt <= 2; attempt++) {
        log(`Electron download ${attempt}/2: ${source.url}`);
        try {
          return await downloadArtifact({version: metadata.version, platform: metadata.platform, arch: metadata.arch,
            artifactName: 'electron', checksums: metadata.checksums,
            cacheRoot: env.electron_config_cache,
            // @electron/get 5 uses ElectronDownloadCacheMode.WriteOnly (2).
            cacheMode: env.force_no_cache === 'true' ? 2 : 0,
            mirrorOptions: {customVersion: metadata.version, resolveAssetURL: () => source.url},
            downloadOptions: {signal: AbortSignal.timeout(DOWNLOAD_TIMEOUT_MS), quiet: true}});
        } catch (error) {
          log(formatError(error, env));
          const failure = downloadFailure(error);
          if (failure.retry && attempt < 2) {
            log('Retrying Electron download after a temporary network failure...');
            await (options.sleep || sleep)(1000);
            continue;
          }
          if (failure.network && index + 1 < sources.length) {
            log('GitHub download failed; trying the npmmirror Electron mirror with the same bundled SHA-256.');
            break;
          }
          throw classifiedError('download', `Electron download failed${source.explicit ? ' using the configured mirror' : ''}. Check proxy, DNS, TLS certificates, and network access, or set HETERO_ELECTRON_ARCHIVE to the matching official ZIP.`, error);
        }
      }
    }
  }
  if (options.downloadArtifact) return download(options.downloadArtifact);
  return withDownloadEnvironment(env, async () => {
    const localRequire = createRequire(path.join(metadata.electronRoot, 'package.json'));
    const getPath = localRequire.resolve('@electron/get');
    if (env.ELECTRON_GET_USE_PROXY) {
      // initializeProxy silently ignores a missing optional dependency. Fail
      // clearly instead of unexpectedly making a direct request.
      createRequire(getPath)('undici');
      for (const key of ['http_proxy', 'HTTP_PROXY', 'https_proxy', 'HTTPS_PROXY']) {
        if (env[key] && !['http:', 'https:'].includes(new URL(env[key]).protocol)) {
          throw classifiedError('download', `Unsupported ${key} protocol; use an HTTP(S) proxy.`);
        }
      }
    }
    const get = await import(pathToFileURL(getPath).href);
    if (env.ELECTRON_GET_USE_PROXY) get.initializeProxy();
    return download(get.downloadArtifact);
  });
}

function executableFor(platform) {
  if (platform === 'win32') return 'electron.exe';
  if (platform === 'darwin') return 'Electron.app/Contents/MacOS/Electron';
  if (['linux', 'freebsd', 'openbsd'].includes(platform)) return 'electron';
  throw new Error(`Unsupported Electron platform: ${platform}.`);
}

async function verifyArchive(archive, expectedHash) {
  const digest = createHash('sha256');
  for await (const chunk of fs.createReadStream(archive)) digest.update(chunk);
  const actual = digest.digest('hex');
  if (actual !== expectedHash.toLowerCase()) {
    throw classifiedError('checksum', `Electron ZIP SHA-256 mismatch: expected ${expectedHash}, found ${actual}. Obtain the official archive for this exact version/platform/architecture; extraction was not attempted.`);
  }
}

async function prepareRuntime(electronRoot, platform, arch, options = {}) {
  electronRoot = path.resolve(electronRoot);
  const env = options.env || process.env;
  const log = message => (options.log || console.log)(redact(message, env));
  const {version} = JSON.parse(await fs.promises.readFile(path.join(electronRoot, 'package.json'), 'utf8'));
  const checksums = JSON.parse(await fs.promises.readFile(path.join(electronRoot, 'checksums.json'), 'utf8'));
  const filename = `electron-v${version}-${platform}-${arch}.zip`;
  const checksum = checksums[filename];
  if (typeof checksum !== 'string' || !/^[a-f\d]{64}$/i.test(checksum)) {
    throw classifiedError('configuration', `The installed Electron ${version} checksum manifest has no valid SHA-256 for ${filename}. Check the platform/architecture and reinstall the pinned npm package if necessary.`);
  }
  const relativeExecutable = executableFor(platform);
  const metadata = {electronRoot, version, platform, arch, filename, checksums};
  const dist = path.join(electronRoot, 'dist');
  await fs.promises.rm(path.join(electronRoot, 'path.txt'), {force: true});
  let archive;
  if (env.HETERO_ELECTRON_ARCHIVE) {
    archive = await fs.promises.realpath(path.resolve(env.HETERO_ELECTRON_ARCHIVE));
    const distReal = await fs.promises.realpath(dist).catch(() => dist);
    if (archive === distReal || archive.startsWith(distReal + path.sep)) {
      throw classifiedError('configuration', 'HETERO_ELECTRON_ARCHIVE must be outside Electron dist so the original ZIP is retained.');
    }
    log(`Verifying offline Electron archive: ${archive}`);
  } else {
    archive = await downloadRuntime(metadata, options);
  }
  await verifyArchive(archive, checksum);
  const staging = await fs.promises.mkdtemp(path.join(electronRoot, '.electron-extract-'));
  try {
    log(`Extracting verified Electron ${version} (${platform}-${arch})...`);
    const localRequire = createRequire(path.join(electronRoot, 'package.json'));
    const extract = options.extract || (await import(pathToFileURL(localRequire.resolve('@electron-internal/extract-zip')).href)).extract;
    await extract(archive, {dir: staging});
    const extractedVersion = (await fs.promises.readFile(path.join(staging, 'version'), 'utf8')).trim().replace(/^v/, '');
    if (extractedVersion !== version) throw new Error(`Extracted version mismatch: expected ${version}, found ${extractedVersion}.`);
    const executable = path.join(staging, relativeExecutable);
    if (!(await fs.promises.stat(executable)).isFile()) throw new Error('Extracted Electron executable is missing.');
    if (platform !== 'win32') await fs.promises.access(executable, fs.constants.X_OK);
    await fs.promises.rm(dist, {recursive: true, force: true});
    await fs.promises.rename(staging, dist);
    await fs.promises.writeFile(path.join(electronRoot, 'path.txt'), relativeExecutable);
  } catch (error) {
    await fs.promises.rm(path.join(electronRoot, 'path.txt'), {force: true}).catch(() => {});
    throw classifiedError('extraction', 'Electron archive extraction/validation failed. Check free disk space, file permissions, and antivirus/quarantine logs. The verified ZIP is retained; rerun preparation after correcting the cause.', error);
  } finally {
    await fs.promises.rm(staging, {recursive: true, force: true});
  }
  return {...metadata, archive, executable: path.join(dist, relativeExecutable)};
}

if (require.main === module) {
  const [electronRoot, platform, arch] = process.argv.slice(2);
  const run = electronRoot && platform && arch ? prepareRuntime(electronRoot, platform, arch) :
    Promise.reject(new Error('Usage: node electron-download.cjs <electronRoot> <platform> <arch>'));
  run.catch(error => { console.error(formatError(error)); process.exitCode = 1; });
}

module.exports = {prepareRuntime, downloadRuntime, verifyArchive, networkEnvironment, mirrorAliases, formatError,
  DEFAULT_MIRROR, FALLBACK_MIRROR, DOWNLOAD_TIMEOUT_MS};
