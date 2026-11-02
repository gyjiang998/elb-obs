'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const {preparePython, verifiedRuntime, cleanDownloadEnvironment} = require('../build/prepare-python.cjs');

const version = '3.13.17';
const filename = `python-${version}-embed-amd64.zip`;

function temporary(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-python-runtime-'));
  t.after(() => fs.rmSync(directory, {recursive: true, force: true}));
  return directory;
}

function write(filename, content) {
  fs.mkdirSync(path.dirname(filename), {recursive: true});
  fs.writeFileSync(filename, content);
}

function fixture(t, archiveBytes = Buffer.from('verified Python archive fixture')) {
  const root = temporary(t);
  const distribution = {
    version,
    url: `https://www.python.org/ftp/python/${version}/${filename}`,
    sha256: crypto.createHash('sha256').update(archiveBytes).digest('hex'),
  };
  const calls = {downloads: [], extracts: [], networkResolution: 0};
  const config = {
    downloads: path.join(root, 'generated downloads'),
    output: path.join(root, 'generated resources/python-win-x64'),
    distribution,
    log: () => {},
    download: (destination, context = {}) => {
      calls.downloads.push({destination, context});
      fs.writeFileSync(destination, archiveBytes);
    },
    extract: async (source, destination) => {
      calls.extracts.push(source);
      for (const required of ['python.exe', 'python313.dll', 'python313.zip']) {
        write(path.join(destination, required), `fixture ${required}`);
      }
    },
    resolveNpmNetworkEnvironment: env => {
      calls.networkResolution++;
      return {...env, HTTPS_PROXY: 'http://resolved-proxy.example:8443', CURL_CA_BUNDLE: 'C:\\corp\\root.pem'};
    },
  };
  return {root, distribution, archiveBytes, config, calls};
}

test('offline Python archive takes precedence, remains untouched, and bypasses every network hook', async t => {
  const f = fixture(t);
  const archive = path.join(f.root, '用户下载', 'Python 离线运行时.zip');
  write(archive, f.archiveBytes);
  const before = fs.readFileSync(archive);
  const beforeMtime = fs.statSync(archive).mtimeMs;
  const config = {
    ...f.config,
    env: {HETERO_PYTHON_ARCHIVE: archive, HTTPS_PROXY: 'not-a-proxy'},
    download: () => assert.fail('offline mode must not invoke the downloader'),
    resolveNpmNetworkEnvironment: () => assert.fail('offline mode must not resolve npm network settings'),
  };

  await preparePython(config);

  assert.equal(verifiedRuntime(config.output, f.distribution), true);
  assert.deepEqual(fs.readFileSync(archive), before);
  assert.equal(fs.statSync(archive).mtimeMs, beforeMtime);
  assert.equal(f.calls.extracts.length, 1);
});

test('missing or wrong offline Python archive never falls back to the network or extraction', async t => {
  const f = fixture(t);
  const missing = path.join(f.root, 'missing Python.zip');
  let downloadCalls = 0;
  let extractCalls = 0;
  const offlineConfig = archive => ({
    ...f.config,
    env: {HETERO_PYTHON_ARCHIVE: archive},
    download: () => { downloadCalls++; },
    extract: async () => { extractCalls++; },
    resolveNpmNetworkEnvironment: () => assert.fail('offline mode must not resolve npm network settings'),
  });

  await assert.rejects(preparePython(offlineConfig(missing)), error => {
    assert.match(error.message, /HETERO_PYTHON_ARCHIVE/i);
    assert.match(error.message, /does not exist|not found|cannot be read/i);
    return true;
  });

  const damaged = path.join(f.root, 'damaged Python.zip');
  write(damaged, 'not the pinned Python archive');
  await assert.rejects(preparePython(offlineConfig(damaged)), /checksum mismatch|SHA-256 mismatch/i);
  assert.equal(fs.readFileSync(damaged, 'utf8'), 'not the pinned Python archive');
  assert.equal(downloadCalls, 0);
  assert.equal(extractCalls, 0);
  assert.equal(fs.existsSync(f.config.output), false);
});

test('Python mirror composes the pinned version path while runtime identity remains canonical', async t => {
  const f = fixture(t);
  const mirror = 'https://packages.example/internal-python';
  const config = {...f.config, env: {HETERO_PYTHON_MIRROR: mirror}};

  await preparePython(config);

  assert.equal(f.calls.networkResolution, 1);
  assert.equal(f.calls.downloads.length, 1);
  assert.equal(f.calls.downloads[0].context.url, `${mirror}/${version}/${filename}`);
  assert.equal(f.calls.downloads[0].context.env.HTTPS_PROXY, 'http://resolved-proxy.example:8443');
  assert.equal(f.calls.downloads[0].context.env.CURL_CA_BUNDLE, 'C:\\corp\\root.pem');
  const receipt = JSON.parse(fs.readFileSync(path.join(config.output, 'distribution.json'), 'utf8'));
  assert.equal(receipt.url, f.distribution.url);
  assert.equal(verifiedRuntime(config.output, f.distribution), true);
});

test('valid cached archive and verified runtime do not resolve npm network settings', async t => {
  const f = fixture(t);
  const cachedArchive = path.join(f.config.downloads, filename);
  write(cachedArchive, f.archiveBytes);
  const config = {
    ...f.config,
    env: {npm_config_proxy: 'http://must-not-be-read.example'},
    download: () => assert.fail('a verified cached archive must not download'),
    resolveNpmNetworkEnvironment: () => assert.fail('a verified cached archive must not resolve network settings'),
  };

  await preparePython(config);
  assert.equal(verifiedRuntime(config.output, f.distribution), true);
  await preparePython({...config, extract: async () => assert.fail('a verified runtime must not extract')});
});

test('mirror checksum failure cannot publish resources or retain a poisoned generated cache', async t => {
  const f = fixture(t);
  const config = {
    ...f.config,
    env: {HETERO_PYTHON_MIRROR: 'https://packages.example/python/'},
    download: (destination, context) => {
      f.calls.downloads.push({destination, context});
      fs.writeFileSync(destination, 'mirror returned an HTML login page');
    },
    extract: async () => assert.fail('checksum failure must stop before extraction'),
  };

  await assert.rejects(preparePython(config), /checksum mismatch|SHA-256 mismatch/i);
  assert.equal(fs.existsSync(config.output), false);
  assert.equal(fs.existsSync(path.join(config.downloads, filename)), false);
  assert.equal(fs.readdirSync(config.downloads).some(entry => entry.includes('.partial')), false);
});

test('Python mirror rejects non-HTTP and control-character values before invoking curl', async t => {
  for (const mirror of ['file:///C:/Downloads/python/', 'https://safe.example/python/\n--output=owned']) {
    const f = fixture(t);
    await assert.rejects(preparePython({
      ...f.config,
      env: {HETERO_PYTHON_MIRROR: mirror},
      download: () => assert.fail('invalid mirror must not invoke the downloader'),
    }), /HETERO_PYTHON_MIRROR.*HTTP|valid HTTP/i);
    assert.equal(fs.existsSync(f.config.output), false);
  }
});

test('download diagnostics preserve the failing host but redact mirror credentials and recommend offline recovery', async t => {
  const f = fixture(t);
  const mirror = 'https://mirror-user:mirror-password@private.example/python/?token=mirror-secret';
  const proxy = 'http://proxy-user:proxy-password@proxy.example:8080/?token=proxy-secret';
  const config = {
    ...f.config,
    env: {HETERO_PYTHON_MIRROR: mirror, HTTPS_PROXY: proxy},
    resolveNpmNetworkEnvironment: env => env,
    download: (_destination, context) => {
      throw new Error(`curl failed to connect to ${context.url} through ${context.env.HTTPS_PROXY}`);
    },
  };

  await assert.rejects(preparePython(config), error => {
    assert.match(error.message, /private\.example|Python runtime download/i);
    assert.match(error.message, /HETERO_PYTHON_ARCHIVE/i);
    for (const secret of ['mirror-user', 'mirror-password', 'mirror-secret', 'proxy-user', 'proxy-password', 'proxy-secret']) {
      assert.equal(error.message.includes(secret), false);
    }
    return true;
  });
});

test('Windows launcher runs Python preparation through the npm lifecycle network bridge', () => {
  const desktopRoot = path.resolve(__dirname, '..');
  const pkg = JSON.parse(fs.readFileSync(path.join(desktopRoot, 'package.json'), 'utf8'));
  const launcher = fs.readFileSync(path.join(desktopRoot, 'run-desktop.ps1'), 'utf8');
  assert.equal(pkg.scripts['prepare:python'], 'node build/prepare-python.cjs');
  assert.match(pkg.scripts['prepare:win'], /npm run --silent prepare:python/);
  assert.match(launcher, /\$npmCommand\.Source[^\r\n]+prepare:python[^\r\n]+Python runtime preparation/);
});

test('Python downloader strips npm configuration and token aliases without removing required proxy settings', () => {
  const env = cleanDownloadEnvironment({
    npm_config_registry: 'https://packages.example/npm/', npm_package_config_private: 'secret',
    NPM_TOKEN: 'upper', npm_token: 'lower', Node_Auth_Token: 'mixed', HTTPS_PROXY: 'http://proxy.example:8080',
  });
  assert.equal(env.npm_config_registry, undefined);
  assert.equal(env.npm_package_config_private, undefined);
  assert.equal(env.NPM_TOKEN, undefined);
  assert.equal(env.npm_token, undefined);
  assert.equal(env.Node_Auth_Token, undefined);
  assert.equal(env.HTTPS_PROXY, 'http://proxy.example:8080');
});
