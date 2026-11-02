'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const {resolveNpmDownloadEnvironment, resolveNpmNetworkEnvironment} = require('../build/npm-download-config.cjs');

test('maps allowlisted npm lifecycle settings without logging their values', () => {
  const logs = [];
  const baseEnv = {
    npm_config_proxy: 'http://proxy-user:proxy-password@proxy.example:8080',
    npm_config_https_proxy: 'http://secure-user:secure-password@proxy.example:8443',
    npm_config_noproxy: 'localhost,.corp.example',
    npm_config_cafile: 'C:\\ProgramData\\公司 证书\\root CA.pem',
    npm_config_electron_mirror: 'https://mirror-user:mirror-password@mirror.example/electron/?token=mirror-secret',
    npm_config_electron_custom_dir: 'v{{ version }}',
    npm_config_electron_custom_filename: 'Electron Windows.zip',
    npm_config_electron_config_cache: 'D:\\构建缓存\\Electron Runtime',
  };
  const resolved = resolveNpmDownloadEnvironment(baseEnv, {log: message => logs.push(message)});
  assert.notEqual(resolved, baseEnv);
  assert.equal(resolved.HTTP_PROXY, baseEnv.npm_config_proxy);
  assert.equal(resolved.HTTPS_PROXY, baseEnv.npm_config_https_proxy);
  assert.equal(resolved.NO_PROXY, baseEnv.npm_config_noproxy);
  assert.equal(resolved.NODE_EXTRA_CA_CERTS, baseEnv.npm_config_cafile);
  assert.equal(resolved.CURL_CA_BUNDLE, baseEnv.npm_config_cafile);
  assert.equal(resolved.ELECTRON_MIRROR, baseEnv.npm_config_electron_mirror);
  assert.equal(resolved.ELECTRON_CUSTOM_DIR, baseEnv.npm_config_electron_custom_dir);
  assert.equal(resolved.ELECTRON_CUSTOM_FILENAME, baseEnv.npm_config_electron_custom_filename);
  assert.equal(resolved.electron_config_cache, baseEnv.npm_config_electron_config_cache);
  for (const value of Object.values(baseEnv)) assert.equal(logs.join('\n').includes(value), false);
});

test('direct downloader environment has precedence and the dedicated mirror beats npm config', () => {
  const baseEnv = {
    http_proxy: '', HTTPS_PROXY: 'http://explicit.example:8443', GLOBAL_AGENT_NO_PROXY: 'localhost',
    NODE_EXTRA_CA_CERTS: 'C:\\explicit\\corp.pem', HETERO_ELECTRON_MIRROR: 'https://dedicated.example/electron/',
    ELECTRON_CUSTOM_DIR: 'explicit-dir', ELECTRON_CUSTOM_FILENAME: 'explicit.zip', electron_config_cache: 'D:\\explicit cache',
    npm_config_proxy: 'http://ignored.example', npm_config_https_proxy: 'http://ignored.example',
    npm_config_electron_mirror: 'https://ignored.example/electron/',
  };
  assert.deepEqual(resolveNpmDownloadEnvironment(baseEnv, {log: () => {}}), baseEnv);
});

test('false disables npm proxy import and invalid config payloads cannot inject environment fields', () => {
  const logs = [];
  const resolved = resolveNpmDownloadEnvironment({
    KEEP: 'yes', npm_config_proxy: 'false', npm_config_https_proxy: '', npm_config_cafile: 'null',
    npm_config_electron_mirror: 'safe\nINJECTED_NAME=owned',
  }, {log: message => logs.push(message)});
  assert.equal(resolved.HTTP_PROXY, undefined);
  assert.equal(resolved.HTTPS_PROXY, undefined);
  assert.equal(resolved.NODE_EXTRA_CA_CERTS, undefined);
  assert.equal(resolved.CURL_CA_BUNDLE, undefined);
  assert.equal(resolved.ELECTRON_MIRROR, undefined);
  assert.equal(resolved.INJECTED_NAME, undefined);
  assert.equal(resolved.KEEP, 'yes');
  assert.equal(logs.join('\n').includes('owned'), false);
});

test('explicit Node and curl CA settings independently take precedence over npm cafile', () => {
  const npmCa = 'C:\\npm\\root.pem';
  const resolved = resolveNpmDownloadEnvironment({
    npm_config_cafile: npmCa,
    NODE_EXTRA_CA_CERTS: 'C:\\node\\root.pem',
    CURL_CA_BUNDLE: 'C:\\curl\\root.pem',
  }, {log: () => {}});
  assert.equal(resolved.NODE_EXTRA_CA_CERTS, 'C:\\node\\root.pem');
  assert.equal(resolved.CURL_CA_BUNDLE, 'C:\\curl\\root.pem');
});

test('npm noproxy array export is normalized to the comma-separated proxy contract', () => {
  const resolved = resolveNpmDownloadEnvironment({npm_config_noproxy: 'localhost\n\n.corp.example\r\n10.0.0.0/8'}, {log: () => {}});
  assert.equal(resolved.NO_PROXY, 'localhost,.corp.example,10.0.0.0/8');
});

test('custom registry is explained but never synthesized into an Electron mirror', () => {
  const logs = [];
  const registry = 'https://registry-user:registry-password@packages.example/npm/?token=registry-secret';
  const resolved = resolveNpmDownloadEnvironment({npm_config_registry: registry}, {log: message => logs.push(message)});
  assert.equal(resolved.ELECTRON_MIRROR, undefined);
  assert.match(logs.join('\n'), /registry.*Electron runtime archives.*separately.*not used as a mirror/i);
  for (const secret of ['registry-user', 'registry-password', 'registry-secret', 'packages.example']) {
    assert.equal(logs.join('\n').includes(secret), false);
  }
});

test('default npm registry does not create a mirror or diagnostic', () => {
  const logs = [];
  const resolved = resolveNpmDownloadEnvironment({npm_config_registry: 'https://registry.npmjs.org/'}, {log: message => logs.push(message)});
  assert.equal(resolved.ELECTRON_MIRROR, undefined);
  assert.equal(logs.some(message => /registry.*Electron runtime archives/i.test(message)), false);
});

test('generic network resolver explains a custom registry for Python without treating it as an archive mirror', () => {
  const logs = [];
  const registry = 'https://registry-user:registry-password@packages.example/npm/?token=registry-secret';
  const resolved = resolveNpmNetworkEnvironment({npm_config_registry: registry}, {
    archiveLabel: 'Python runtime archives',
    mirrorVariable: 'HETERO_PYTHON_MIRROR',
    log: message => logs.push(message),
  });
  assert.equal(resolved.HETERO_PYTHON_MIRROR, undefined);
  assert.match(logs.join('\n'), /Python runtime archives.*HETERO_PYTHON_MIRROR.*not used as a mirror/i);
  for (const secret of ['registry-user', 'registry-password', 'registry-secret', 'packages.example']) {
    assert.equal(logs.join('\n').includes(secret), false);
  }
});

test('real npm lifecycle exports protected proxy and legacy underscore Electron settings', {skip: !process.env.npm_execpath}, t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-npm-lifecycle-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const npmrc = path.join(root, '.npmrc');
  const caFile = path.join(root, 'company root CA.pem');
  fs.writeFileSync(caFile, 'test certificate placeholder');
  fs.writeFileSync(npmrc, [
    'registry=https://packages.example/npm/',
    'proxy=http://proxy-user:proxy-password@proxy.example:8080',
    `cafile=${caFile.replace(/\\/g, '/')}`,
    'noproxy[]=localhost',
    'noproxy[]=.corp.example',
    'electron_mirror=https://mirror.example/electron/',
    'electron_custom_dir=v{{ version }}',
  ].join('\n'));
  fs.writeFileSync(path.join(root, 'package.json'), JSON.stringify({private: true, scripts: {probe: 'node probe.cjs'}}));
  fs.writeFileSync(path.join(root, 'probe.cjs'), `console.log(JSON.stringify(Object.fromEntries(Object.entries(process.env).filter(([name]) => [
    'npm_config_registry','npm_config_proxy','npm_config_cafile','npm_config_noproxy','npm_config_electron_mirror','npm_config_electron_custom_dir'
  ].includes(name)))))`);
  const result = spawnSync(process.execPath, [process.env.npm_execpath, '--prefix', root, '--userconfig', npmrc, 'run', '--silent', 'probe'], {
    encoding: 'utf8', windowsHide: true, timeout: 10000, maxBuffer: 64 * 1024,
  });
  assert.equal(result.status, 0, result.stderr);
  const lifecycle = JSON.parse(result.stdout.trim());
  assert.equal(lifecycle.npm_config_proxy, 'http://proxy-user:proxy-password@proxy.example:8080');
  assert.equal(path.resolve(lifecycle.npm_config_cafile), path.resolve(caFile));
  assert.match(lifecycle.npm_config_noproxy, /localhost[\s\S]*\.corp\.example/);
  assert.equal(lifecycle.npm_config_electron_mirror, 'https://mirror.example/electron/');
  const resolved = resolveNpmDownloadEnvironment(lifecycle, {log: () => {}});
  assert.equal(resolved.HTTP_PROXY, lifecycle.npm_config_proxy);
  assert.equal(resolved.NODE_EXTRA_CA_CERTS, lifecycle.npm_config_cafile);
  assert.equal(resolved.CURL_CA_BUNDLE, lifecycle.npm_config_cafile);
  assert.equal(resolved.NO_PROXY, 'localhost,.corp.example');
  assert.equal(resolved.ELECTRON_MIRROR, lifecycle.npm_config_electron_mirror);
  assert.equal(resolved.ELECTRON_CUSTOM_DIR, 'v{{ version }}');
});
