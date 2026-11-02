'use strict';
// Official CPython embeddable distribution, pinned and verified before extraction.
// Both the downloaded archive and the extracted runtime are cached independently.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {execFileSync} = require('node:child_process');
const {resolveNpmNetworkEnvironment} = require('./npm-download-config.cjs');
const version = '3.13.17';
const sha256 = '9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38';
const name = `python-${version}-embed-amd64.zip`;
const url = `https://www.python.org/ftp/python/${version}/${name}`;
const pythonPathFile = 'python313._pth';
const pythonPathContent = 'python313.zip\n.\n../capture-runtime\n';
const requiredFiles = ['python.exe', 'python313.dll', 'python313.zip', pythonPathFile];
const digest = file => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');

function archiveName(distribution) {
  return distribution.name || `python-${distribution.version}-embed-amd64.zip`;
}

function mirrorUrl(distribution, env) {
  const mirror = env.HETERO_PYTHON_MIRROR;
  if (!mirror) return distribution.url;
  if (typeof mirror !== 'string' || !mirror.trim() || /[\0\r\n]/.test(mirror)) {
    throw new Error('HETERO_PYTHON_MIRROR must be a non-empty HTTP(S) URL.');
  }
  let parsed;
  try { parsed = new URL(mirror.trim()); }
  catch { throw new Error('HETERO_PYTHON_MIRROR must be a valid HTTP(S) URL.'); }
  if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error('HETERO_PYTHON_MIRROR must use HTTP or HTTPS.');
  parsed.pathname = `${parsed.pathname.replace(/\/+$/, '')}/${distribution.version}/${archiveName(distribution)}`;
  return parsed.toString();
}

function redact(value, env) {
  let text = String(value);
  const secrets = [];
  for (const [key, setting] of Object.entries(env)) {
    if (!/(?:proxy|mirror)/i.test(key) || !setting) continue;
    try {
      const url = new URL(setting);
      secrets.push(url.username, url.password, ...url.searchParams.values());
      if (url.username) secrets.push(decodeURIComponent(url.username));
      if (url.password) secrets.push(decodeURIComponent(url.password));
    } catch { /* NO_PROXY and malformed values have no embedded URL secret. */ }
  }
  text = text.replace(/\b(?:https?|socks[45]?):\/\/[^\s"'<>]+/gi, raw => {
    try {
      const url = new URL(raw);
      url.username = ''; url.password = ''; url.search = ''; url.hash = '';
      return url.toString();
    } catch { return '[redacted URL]'; }
  });
  for (const secret of secrets.filter(Boolean).sort((a, b) => b.length - a.length)) text = text.split(secret).join('[redacted]');
  return text;
}

function cleanDownloadEnvironment(input) {
  const env = {...input};
  if (!env.HTTPS_PROXY && !env.https_proxy && (env.HTTP_PROXY || env.http_proxy)) {
    env.HTTPS_PROXY = env.HTTP_PROXY || env.http_proxy;
  }
  for (const key of Object.keys(env)) {
    if (/^(?:npm_config_|npm_package_config_)/i.test(key)) delete env[key];
    else if (/^(?:NPM_TOKEN|NODE_AUTH_TOKEN)$/i.test(key)) delete env[key];
  }
  return env;
}

function offlineArchive(value, output) {
  let archive;
  try { archive = fs.realpathSync(path.resolve(value)); }
  catch { throw new Error(`HETERO_PYTHON_ARCHIVE does not exist or cannot be read: ${path.resolve(value)}`); }
  if (!fs.statSync(archive).isFile()) throw new Error('HETERO_PYTHON_ARCHIVE must point to the downloaded ZIP file, not a directory.');
  const outputPath = path.resolve(output);
  if (archive === outputPath || archive.startsWith(`${outputPath}${path.sep}`)) {
    throw new Error('HETERO_PYTHON_ARCHIVE must be outside the generated Python runtime directory so the original ZIP is retained.');
  }
  return archive;
}

function verifyArchive(archive, expected, label) {
  const actual = digest(archive);
  if (actual !== expected.toLowerCase()) {
    throw new Error(`${label} checksum mismatch (SHA-256): expected ${expected}, found ${actual}. Obtain the official Windows embeddable x64 ZIP for this exact version; extraction was not attempted.`);
  }
}

function runtimeFiles(directory) {
  const files = [];
  function visit(current) {
    for (const entry of fs.readdirSync(current, {withFileTypes: true})) {
      if (entry.isSymbolicLink()) throw new Error('Python runtime may not contain symbolic links');
      const filename = path.join(current, entry.name);
      if (entry.isDirectory()) visit(filename);
      else if (entry.isFile()) files.push({path: path.relative(directory, filename).split(path.sep).join('/'), size: fs.statSync(filename).size, sha256: digest(filename)});
    }
  }
  visit(directory);
  return files;
}

function verifiedRuntime(directory, distribution = {version, url, sha256}) {
  try {
    const record = JSON.parse(fs.readFileSync(path.join(directory, 'distribution.json'), 'utf8'));
    if (record.version !== distribution.version || record.sha256 !== distribution.sha256 || record.url !== distribution.url || record.cacheSchema !== 1) return false;
    if (!Array.isArray(record.files) || record.files.length < requiredFiles.length) return false;
    if (!requiredFiles.every(file => record.files.some(entry => entry.path === file))) return false;
    if (fs.readFileSync(path.join(directory, pythonPathFile), 'utf8') !== pythonPathContent) return false;
    return record.files.every(entry => {
      if (typeof entry.path !== 'string' || !entry.path || path.isAbsolute(entry.path) || entry.path.includes('\\') || entry.path.split('/').some(part => part === '..' || part === '.' || !part)) return false;
      const filename = path.join(directory, entry.path);
      const stat = fs.lstatSync(filename);
      return stat.isFile() && !stat.isSymbolicLink() && stat.size === entry.size && digest(filename) === entry.sha256;
    });
  } catch { return false; }
}

async function preparePython(options = {}) {
  const downloads = options.downloads || path.join(__dirname, 'downloads');
  const output = options.output || path.join(__dirname, 'resources/python-win-x64');
  const distribution = options.distribution || {version, url, sha256};
  const cachedArchive = path.join(downloads, archiveName(distribution));
  const log = options.log || console.log;
  const extract = options.extract || (async (source, destination) => {
    const zip = await require('unzipper').Open.file(source);
    await zip.extract({path: destination});
  });
  fs.mkdirSync(downloads, {recursive: true});
  if (verifiedRuntime(output, distribution)) {
    log(`Reusing verified CPython ${distribution.version} Windows x64.`);
    return output;
  }
  const baseEnv = {...(options.env || process.env)};
  if (!baseEnv.HETERO_PYTHON_ARCHIVE && fs.existsSync(cachedArchive) && digest(cachedArchive) !== distribution.sha256.toLowerCase()) {
    log('Cached Python archive is damaged; discarding it.');
    fs.rmSync(cachedArchive);
  }
  let archive;
  if (baseEnv.HETERO_PYTHON_ARCHIVE) {
    archive = offlineArchive(baseEnv.HETERO_PYTHON_ARCHIVE, output);
    log('Verifying offline CPython Windows x64 archive.');
    verifyArchive(archive, distribution.sha256, 'Python archive');
  } else if (fs.existsSync(cachedArchive)) {
    archive = cachedArchive;
  } else {
    const resolveNetwork = options.resolveNpmNetworkEnvironment || resolveNpmNetworkEnvironment;
    const resolved = resolveNetwork(baseEnv, {log, archiveLabel: 'Python runtime archives', mirrorVariable: 'HETERO_PYTHON_MIRROR'});
    const env = cleanDownloadEnvironment(resolved);
    const source = mirrorUrl(distribution, env);
    const download = options.download || ((destination) => {
      const args = ['--fail', '--location', '--retry', '2', '--max-time', '600'];
      if (env.NODE_EXTRA_CA_CERTS && !env.CURL_CA_BUNDLE) args.push('--cacert', env.NODE_EXTRA_CA_CERTS);
      args.push(source, '--output', destination);
      try {
        execFileSync(process.platform === 'win32' ? 'curl.exe' : 'curl', args, {stdio: 'inherit', env});
      } catch (error) {
        const reason = error.status === undefined ? (error.code || 'unknown error') : `exit ${error.status}`;
        throw new Error(`Python runtime download failed (${reason}). Check proxy, DNS, TLS certificates and network access, or set HETERO_PYTHON_ARCHIVE to the matching official ZIP.`);
      }
    });
    if (env.HETERO_PYTHON_MIRROR) log('Downloading CPython from the configured HETERO_PYTHON_MIRROR; URL hidden.');
    else log('Downloading CPython Windows x64 from python.org.');
    const temp = `${cachedArchive}.partial-${process.pid}`;
    try {
      try { await download(temp, {url: source, env}); }
      catch (error) {
        throw new Error(`Python runtime download failed: ${redact(error.message || error, env)} Configure the network, set HETERO_PYTHON_MIRROR, or set HETERO_PYTHON_ARCHIVE to the matching official ZIP.`, {cause: error});
      }
      verifyArchive(temp, distribution.sha256, 'Python archive');
      fs.renameSync(temp, cachedArchive);
    } finally { fs.rmSync(temp, {force: true}); }
    archive = cachedArchive;
  }
  const staging = `${output}.partial-${process.pid}`;
  fs.rmSync(staging, {recursive: true, force: true});
  fs.mkdirSync(staging, {recursive: true});
  try {
    await extract(archive, staging);
    fs.writeFileSync(path.join(staging, pythonPathFile), pythonPathContent);
    for (const file of requiredFiles) if (!fs.statSync(path.join(staging, file)).isFile()) throw new Error(`Python archive is missing ${file}`);
    const files = runtimeFiles(staging);
    fs.writeFileSync(path.join(staging, 'distribution.json'), JSON.stringify({name: 'CPython embeddable', ...distribution, cacheSchema: 1, files}, null, 2));
    if (!verifiedRuntime(staging, distribution)) throw new Error('Extracted Python runtime verification failed.');
    // These are generated build resources, never the user's AppData/captures.
    fs.rmSync(output, {recursive: true, force: true});
    fs.renameSync(staging, output);
  } finally { fs.rmSync(staging, {recursive: true, force: true}); }
  log(`Prepared CPython ${distribution.version} Windows x64 (${distribution.sha256.slice(0, 12)})`);
  return output;
}
if (require.main === module) preparePython().catch(error => { console.error(error.message); process.exitCode = 1; });
module.exports = {preparePython, verifiedRuntime, mirrorUrl, cleanDownloadEnvironment, verifyArchive};
