'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {createHash} = require('node:crypto');
const {prepareRuntime, downloadRuntime, networkEnvironment, mirrorAliases, formatError,
  DEFAULT_MIRROR, FALLBACK_MIRROR, DOWNLOAD_TIMEOUT_MS} = require('../build/electron-download.cjs');

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'electron-download-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const electronRoot = path.join(root, 'electron');
  fs.mkdirSync(electronRoot);
  const filename = 'electron-v45.0.0-win32-x64.zip';
  const archive = path.join(root, '离线 安装包.zip');
  fs.writeFileSync(archive, 'fixture ZIP bytes');
  const checksum = createHash('sha256').update(fs.readFileSync(archive)).digest('hex');
  const checksums = {[filename]: checksum};
  fs.writeFileSync(path.join(electronRoot, 'package.json'), JSON.stringify({version: '45.0.0'}));
  fs.writeFileSync(path.join(electronRoot, 'checksums.json'), JSON.stringify(checksums));
  const metadata = {electronRoot, filename, checksums, version: '45.0.0', platform: 'win32', arch: 'x64'};
  const calls = [], logs = [], delays = [];
  let extractions = 0;
  const options = {env: {}, log: message => logs.push(message), sleep: async ms => delays.push(ms),
    downloadArtifact: async details => { calls.push(details); return archive; },
    extract: async (zip, {dir}) => {
      extractions++;
      assert.equal(fs.realpathSync(zip), fs.realpathSync(archive));
      fs.writeFileSync(path.join(dir, 'version'), 'v45.0.0\n');
      fs.writeFileSync(path.join(dir, 'electron.exe'), 'fixture executable');
    }};
  return {root, electronRoot, archive, metadata, options, calls, logs, delays, extractions: () => extractions};
}

function networkError(code = 'ECONNRESET') {
  return new TypeError('fetch failed', {cause: Object.assign(new Error('socket disconnected'), {code})});
}

test('default GitHub source retries network failures, falls back once, and keeps the bundled checksum and cache', async t => {
  const f = fixture(t);
  const calls = [];
  await prepareRuntime(f.electronRoot, 'win32', 'x64', {...f.options,
    env: {electron_config_cache: 'C:\\Electron Cache', electron_use_remote_checksums: '1', ELECTRON_CUSTOM_VERSION: '99.0.0'},
    downloadArtifact: async details => {
      calls.push(details);
      if (calls.length < 3) throw networkError();
      return f.archive;
    }});
  assert.deepEqual(calls.map(call => call.mirrorOptions.resolveAssetURL()), [
    `${DEFAULT_MIRROR}v45.0.0/${f.metadata.filename}`,
    `${DEFAULT_MIRROR}v45.0.0/${f.metadata.filename}`,
    `${FALLBACK_MIRROR}v45.0.0/${f.metadata.filename}`,
  ]);
  for (const call of calls) {
    assert.equal(call.version, '45.0.0');
    assert.equal(call.arch, 'x64');
    assert.deepEqual(call.checksums, f.metadata.checksums);
    assert.equal(call.cacheRoot, 'C:\\Electron Cache');
    assert.ok(call.downloadOptions.signal instanceof AbortSignal);
    assert.equal(call.downloadOptions.signal.aborted, false);
  }
  assert.equal(DOWNLOAD_TIMEOUT_MS, 600000);
  assert.equal(new Set(calls.map(call => call.downloadOptions.signal)).size, 3);
  assert.deepEqual(f.delays, [1000]);
  assert.equal(f.extractions(), 1);
  assert.equal(fs.readFileSync(path.join(f.electronRoot, 'path.txt'), 'utf8'), 'electron.exe');
});

test('both sources have bounded retries and nested DNS diagnostics survive failure', async t => {
  const f = fixture(t);
  let attempts = 0;
  await assert.rejects(downloadRuntime(f.metadata, {...f.options, downloadArtifact: async () => {
    attempts++; throw networkError('ENOTFOUND');
  }}), error => {
    assert.match(formatError(error), /Caused by:.*ENOTFOUND/);
    assert.equal(error.stage, 'download');
    return true;
  });
  assert.equal(attempts, 4);
  assert.deepEqual(f.delays, [1000, 1000]);
});

test('default HTTP 403 and 404 failures immediately use the mirror', async t => {
  const f = fixture(t);
  for (const status of [403, 404]) {
    const calls = [];
    const result = await downloadRuntime(f.metadata, {...f.options, downloadArtifact: async details => {
      calls.push(details);
      if (calls.length === 1) throw Object.assign(new Error('Response denied'), {response: {status}});
      return f.archive;
    }});
    assert.equal(result, f.archive);
    assert.equal(calls.length, 2);
    assert.match(calls[1].mirrorOptions.resolveAssetURL(), /^https:\/\/npmmirror.com\//);
  }
  assert.deepEqual(f.delays, []);
});

test('every official mirror alias is honored without public fallback', async t => {
  const f = fixture(t);
  for (const alias of mirrorAliases('mirror')) {
    const urls = [];
    await assert.rejects(downloadRuntime(f.metadata, {...f.options, env: {[alias]: 'https://private.example/electron/'},
      downloadArtifact: async details => { urls.push(details.mirrorOptions.resolveAssetURL()); throw networkError(); },
    }), /configured mirror/);
    assert.deepEqual(urls, Array(2).fill(`https://private.example/electron/v45.0.0/${f.metadata.filename}`));
  }
});

test('dedicated application mirror takes precedence and never falls back to public sources', async t => {
  const f = fixture(t);
  const urls = [];
  await assert.rejects(downloadRuntime(f.metadata, {...f.options, env: {
    HETERO_ELECTRON_MIRROR: 'http://internal.example/electron/',
    ELECTRON_MIRROR: 'https://environment.example/electron/',
    npm_config_electron_mirror: 'https://npm.example/electron/',
  }, downloadArtifact: async details => {
    urls.push(details.mirrorOptions.resolveAssetURL());
    throw networkError();
  }}), /configured mirror/);
  assert.deepEqual(urls, Array(2).fill(`http://internal.example/electron/v45.0.0/${f.metadata.filename}`));
});

test('official alias precedence, custom directories, filenames, and nightly mirrors are preserved', async t => {
  const f = fixture(t);
  let received;
  await downloadRuntime(f.metadata, {...f.options, env: {
    npm_config_electron_mirror: 'https://first.example/', ELECTRON_MIRROR: 'https://last.example/',
    NPM_CONFIG_ELECTRON_CUSTOM_DIR: 'release-{{ version }}', npm_package_config_electron_customFilename: 'renamed.zip',
  }, downloadArtifact: async details => { received = details; return f.archive; }});
  assert.equal(received.mirrorOptions.resolveAssetURL(), 'https://first.example/release-45.0.0/renamed.zip');
  for (const alias of mirrorAliases('nightlyMirror')) {
    const metadata = {...f.metadata, version: '45.0.0-nightly.1'};
    await downloadRuntime(metadata, {...f.options, env: {[alias]: 'https://nightly.example/'},
      downloadArtifact: async details => { received = details; return f.archive; }});
    assert.match(received.mirrorOptions.resolveAssetURL(), /^https:\/\/nightly.example\//);
  }
});

test('standard proxy aliases activate proxy support and global-agent aliases translate without overriding standard settings', () => {
  for (const name of ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'GLOBAL_AGENT_HTTP_PROXY', 'GLOBAL_AGENT_HTTPS_PROXY']) {
    const input = {[name]: 'http://proxy.example:8080', ELECTRON_GET_USE_PROXY: ''};
    const env = networkEnvironment(input);
    assert.equal(env.ELECTRON_GET_USE_PROXY, '1', name);
    assert.equal(input.ELECTRON_GET_USE_PROXY, '');
    if (name.startsWith('GLOBAL_AGENT_')) assert.equal(env[name.replace('GLOBAL_AGENT_', '')], input[name]);
  }
  const env = networkEnvironment({http_proxy: 'http://first.example', GLOBAL_AGENT_HTTP_PROXY: 'http://last.example',
    GLOBAL_AGENT_NO_PROXY: 'localhost,.internal', no_proxy: '.private'});
  assert.equal(env.HTTP_PROXY, undefined);
  assert.equal(env.no_proxy, '.private');
  assert.equal(env.NO_PROXY, undefined);
  assert.equal(networkEnvironment({GLOBAL_AGENT_NO_PROXY: 'localhost'}).NO_PROXY, 'localhost');
  assert.equal(networkEnvironment({NO_PROXY: 'localhost'}).ELECTRON_GET_USE_PROXY, undefined);
});

test('every custom version override is removed without mutating the caller environment', () => {
  const input = Object.fromEntries(mirrorAliases('customVersion').map(name => [name, '99.0.0']));
  const env = networkEnvironment(input);
  for (const name of mirrorAliases('customVersion')) {
    assert.equal(env[name], undefined);
    assert.equal(input[name], '99.0.0');
  }
});

test('TLS diagnostics show nested causes and HTTP status while redacting mirror and proxy secrets', async t => {
  const f = fixture(t);
  const env = {ELECTRON_MIRROR: 'https://mirror-user:mirror-password@private.example/releases/?token=mirror-secret',
    HTTPS_PROXY: 'http://proxy-user:proxy-password@proxy.example:8080/?token=proxy-secret'};
  const cause = Object.assign(new Error(`unable to verify certificate ${env.HTTPS_PROXY} proxy-password`), {code: 'UNABLE_TO_VERIFY_LEAF_SIGNATURE'});
  const error = new TypeError(`fetch failed ${env.ELECTRON_MIRROR}`, {cause});
  let attempts = 0;
  await assert.rejects(downloadRuntime(f.metadata, {...f.options, env, downloadArtifact: async () => {
    attempts++; throw error;
  }}), thrown => {
    const message = formatError(thrown, env);
    assert.match(message, /UNABLE_TO_VERIFY_LEAF_SIGNATURE/);
    assert.match(message, /private.example/);
    assert.match(message, /proxy.example/);
    for (const secret of ['mirror-user', 'mirror-password', 'mirror-secret', 'proxy-user', 'proxy-password', 'proxy-secret']) {
      assert.equal(message.includes(secret), false);
      assert.equal(f.logs.join('\n').includes(secret), false);
    }
    return true;
  });
  assert.equal(attempts, 1);
  const http = Object.assign(new Error('request failed'), {response: {status: 407}});
  assert.match(formatError(new Error('fetch failed', {cause: http})), /HTTP 407/);
});

test('offline ZIP paths with spaces and Chinese characters require no network and retain source bytes', async t => {
  const f = fixture(t);
  const before = fs.readFileSync(f.archive);
  const beforeStat = fs.statSync(f.archive);
  const result = await prepareRuntime(f.electronRoot, 'win32', 'x64', {...f.options,
    env: {HETERO_ELECTRON_ARCHIVE: f.archive, HTTPS_PROXY: 'invalid-proxy'},
    downloadArtifact: async () => assert.fail('offline mode must not download')});
  assert.equal(result.archive, fs.realpathSync(f.archive));
  assert.deepEqual(fs.readFileSync(f.archive), before);
  assert.equal(fs.statSync(f.archive).mtimeMs, beforeStat.mtimeMs);
  assert.equal(f.extractions(), 1);
});

test('bad offline and downloaded SHA-256 prevent extraction and markers', async t => {
  const f = fixture(t);
  fs.writeFileSync(f.archive, 'wrong archive contents');
  for (const env of [{}, {HETERO_ELECTRON_ARCHIVE: f.archive}]) {
    await assert.rejects(prepareRuntime(f.electronRoot, 'win32', 'x64', {...f.options, env}), /SHA-256 mismatch/);
    assert.equal(f.extractions(), 0);
    assert.equal(fs.existsSync(path.join(f.electronRoot, 'path.txt')), false);
    assert.equal(fs.existsSync(f.archive), true);
  }
});

test('official checksum failures and local filesystem failures never retry or change source', async t => {
  const f = fixture(t);
  for (const error of [Object.assign(new Error('Checksum mismatch'), {name: 'ChecksumMismatchError'}),
    Object.assign(new Error('permission denied opening /network/cache.zip'), {code: 'EACCES'})]) {
    let attempts = 0;
    await assert.rejects(prepareRuntime(f.electronRoot, 'win32', 'x64', {...f.options, downloadArtifact: async () => {
      attempts++; throw error;
    }}));
    assert.equal(attempts, 1);
    assert.equal(f.extractions(), 0);
  }
  assert.deepEqual(f.delays, []);
});

test('extraction and extracted-version failures retain archive and prior dist without marker or download retry', async t => {
  const f = fixture(t);
  fs.mkdirSync(path.join(f.electronRoot, 'dist'));
  fs.writeFileSync(path.join(f.electronRoot, 'dist', 'old-file'), 'retain until extraction succeeds');
  for (const extract of [async () => { throw Object.assign(new Error('disk full'), {code: 'ENOSPC'}); },
    async (_, {dir}) => { fs.writeFileSync(path.join(dir, 'version'), '44.0.0'); },
    async (_, {dir}) => { fs.writeFileSync(path.join(dir, 'version'), '45.0.0'); }]) {
    fs.writeFileSync(path.join(f.electronRoot, 'path.txt'), 'stale marker');
    await assert.rejects(prepareRuntime(f.electronRoot, 'win32', 'x64', {...f.options, extract}), error => {
      assert.equal(error.stage, 'extraction');
      assert.match(error.message, /extraction\/validation failed/);
      return true;
    });
    assert.equal(fs.existsSync(path.join(f.electronRoot, 'path.txt')), false);
    assert.equal(fs.existsSync(path.join(f.electronRoot, 'dist', 'old-file')), true);
    assert.equal(fs.existsSync(f.archive), true);
    assert.equal(fs.readdirSync(f.electronRoot).some(name => name.startsWith('.electron-extract-')), false);
  }
  assert.equal(f.calls.length, 3);
});

test('unsupported checksum identity fails before network or extraction', async t => {
  const f = fixture(t);
  await assert.rejects(prepareRuntime(f.electronRoot, 'win32', 'ia32', f.options), /no valid SHA-256/);
  assert.equal(f.calls.length, 0);
  assert.equal(f.extractions(), 0);
});
