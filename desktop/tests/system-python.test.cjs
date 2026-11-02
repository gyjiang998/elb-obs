'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {probeSystemPython, asciiJson} = require('../build/system-python.cjs');
const {captureEnvironment} = require('../runtime/capture-environment.cjs');

function successfulProbe(executable, version = [3, 15, 2]) {
  return {
    status: 0,
    stdout: `${JSON.stringify({executable, version, implementation: 'cpython', machine: 'AMD64'})}\n`,
    stderr: '',
  };
}

test('system Python accepts a Windows executable path containing spaces and returns its canonical path', () => {
  const configured = 'C:\\Program Files\\Python 3.15\\python.exe';
  const canonical = 'C:\\Program Files\\Python 3.15\\python.exe';
  let invocation;
  const result = probeSystemPython(configured, {
    platform: 'win32',
    env: {PATH: 'C:\\Windows\\System32'},
    existsSync: value => value === configured,
    spawnSync: (...args) => {
      invocation = args;
      return successfulProbe(canonical);
    },
  });

  assert.equal(result.executable, canonical);
  assert.deepEqual(result.version, [3, 15, 2]);
  assert.equal(invocation[0], configured, 'a path containing spaces must remain one executable argument');
  assert.deepEqual(invocation[1].slice(0, 6), ['-B', '-I', '-S', '-X', 'utf8', '-c']);
  assert.notEqual(invocation[2].shell, true);
  assert.equal(invocation[2].windowsHide, true);
  assert.deepEqual(invocation[2].env, {PATH: 'C:\\Windows\\System32'});
  assert.match(invocation[1][6], /importlib\.import_module/);
  assert.match(invocation[1][6], /json/);
  assert.match(invocation[1][6], /pathlib|zipfile/,
    'the probe must import standard-library modules used by the capture runtime');
});

test('system Python accepts a PATH command and freezes the interpreter reported by sys.executable', () => {
  const canonical = 'C:\\Users\\tester\\AppData\\Local\\Programs\\Python\\Python315\\python.exe';
  let invoked = 0;
  const result = probeSystemPython('python.exe', {
    platform: 'win32', env: {PATH: 'fixture-path'}, existsSync: value => value === canonical,
    spawnSync: (executable, args, options) => {
      invoked++;
      assert.equal(executable, 'python.exe');
      assert.notEqual(options.shell, true);
      assert.ok(args.includes('-c'));
      return successfulProbe(canonical, [3, 11, 0]);
    },
  });

  assert.equal(invoked, 1);
  assert.equal(result.executable, canonical);
  assert.deepEqual(result.version, [3, 11, 0]);
});

test('system Python enforces the supported 3.11 through 3.15 contract', () => {
  const probe = version => probeSystemPython('python.exe', {
    platform: 'win32', existsSync: () => true,
    spawnSync: () => successfulProbe('C:\\Python\\python.exe', version),
  });

  assert.deepEqual(probe([3, 11, 0]).version, [3, 11, 0]);
  assert.deepEqual(probe([3, 15, 99]).version, [3, 15, 99]);
  assert.throws(() => probe([3, 10, 14]), /Python 3\.11|3\.11.*required|requires.*3\.11/i);
  assert.throws(() => probe([3, 16, 0]), /Python 3\.15|below 3\.16|3\.11.*3\.15|unsupported/i);
});

test('an explicit invalid system Python fails closed instead of silently selecting another runtime', () => {
  let calls = 0;
  const spawnSync = () => { calls++; return {status: 1, stdout: '', stderr: 'launcher unavailable'}; };

  for (const value of ['', '   ', 'python.exe\n--version', 'python.exe\0ignored']) {
    assert.throws(() => probeSystemPython(value, {platform: 'win32', spawnSync, existsSync: () => false}),
      /HETERO_DESKTOP_PYTHON|Python|non-empty|invalid/i);
  }
  assert.equal(calls, 0, 'structurally invalid values must be rejected before process launch');

  assert.throws(() => probeSystemPython('missing-python.exe', {
    platform: 'win32', existsSync: () => false,
    spawnSync: () => ({error: Object.assign(new Error('not found'), {code: 'ENOENT'}), status: null, stdout: '', stderr: ''}),
  }), /missing-python|not found|ENOENT|cannot.*run/i);

  assert.throws(() => probeSystemPython('broken-python.exe', {
    platform: 'win32', existsSync: () => false,
    spawnSync: () => ({status: 7, stdout: '', stderr: 'stdlib import failed'}),
  }), /stdlib import failed|self-check|probe|exit/i);
});

test('system Python rejects malformed or incomplete probe identity', () => {
  const run = result => probeSystemPython('python.exe', {
    platform: 'win32', existsSync: () => false, spawnSync: () => result,
  });
  assert.throws(() => run({status: 0, stdout: 'not json\n', stderr: ''}), /JSON|identity|probe|output/i);
  assert.throws(() => run(successfulProbe('', [3, 15, 2])), /executable|identity|probe/i);
  assert.throws(() => run({status: 0, stdout: JSON.stringify({executable: 'C:\\Python\\python.exe', version: '3.15.2'}), stderr: ''}), /version|identity|probe|unsupported/i);
  assert.throws(() => run({status: 0, stdout: JSON.stringify({executable: 'C:\\Python\\python.exe', version: [3, 15, 2], implementation: 'pypy'}), stderr: ''}), /CPython|implementation/i);
});

test('capture runtime launches an explicitly selected interpreter in isolated UTF-8 mode', () => {
  const previous = process.env.HETERO_DESKTOP_PYTHON;
  const selected = process.platform === 'win32'
    ? 'C:\\Program Files\\Python 3.15\\python.exe'
    : '/opt/Python 3.15/bin/python3';
  process.env.HETERO_DESKTOP_PYTHON = selected;
  try {
    const environment = captureEnvironment({
      appPath: path.resolve(__dirname, '..'),
      resourcesPath: path.resolve(__dirname, '../fixture resources'),
      platform: process.platform,
    });
    assert.deepEqual(environment.pythonCommand, [selected, '-B', '-I', '-S', '-X', 'utf8']);
    assert.equal(environment.launchOptions.env.PYTHONUTF8, '1');
  } finally {
    if (previous === undefined) delete process.env.HETERO_DESKTOP_PYTHON;
    else process.env.HETERO_DESKTOP_PYTHON = previous;
  }
});

test('system Python CLI identity stays ASCII-safe for Windows PowerShell 5.1 Unicode paths', () => {
  const identity = {executable: 'C:\\Users\\测试 用户\\Python 3.15\\python.exe', version: [3, 15, 2]};
  const encoded = asciiJson(identity);
  assert.doesNotMatch(encoded, /[^\x00-\x7f]/);
  assert.deepEqual(JSON.parse(encoded), identity);
});

test('packaged Windows runtime ignores workstation Python overrides and uses its embedded interpreter', () => {
  const temporary = fs.mkdtempSync(path.join(require('node:os').tmpdir(), 'hetero-packaged-python-'));
  const entry = path.join(temporary, 'capture-runtime/tools/scenario_capture/desktop_entry.py');
  const bundled = path.join(temporary, 'python-win-x64/python.exe');
  fs.mkdirSync(path.dirname(entry), {recursive: true});
  fs.mkdirSync(path.dirname(bundled), {recursive: true});
  fs.writeFileSync(entry, '');
  fs.writeFileSync(bundled, '');
  const previous = process.env.HETERO_DESKTOP_PYTHON;
  process.env.HETERO_DESKTOP_PYTHON = 'C:\\Python315\\python.exe';
  try {
    const environment = captureEnvironment({resourcesPath: temporary, appPath: temporary, platform: 'win32'});
    assert.deepEqual(environment.pythonCommand, [bundled]);
  } finally {
    if (previous === undefined) delete process.env.HETERO_DESKTOP_PYTHON;
    else process.env.HETERO_DESKTOP_PYTHON = previous;
    fs.rmSync(temporary, {recursive: true, force: true});
  }
});

test('source launcher skips embedded Python only for a validated explicit interpreter; packaging still prepares it', () => {
  const desktopRoot = path.resolve(__dirname, '..');
  const launcher = fs.readFileSync(path.join(desktopRoot, 'run-desktop.ps1'), 'utf8');
  const pkg = JSON.parse(fs.readFileSync(path.join(desktopRoot, 'package.json'), 'utf8'));

  assert.match(launcher, /HETERO_DESKTOP_PYTHON/);
  assert.match(launcher, /system-python\.cjs/);
  assert.match(launcher, /\$null -ne \$originalDesktopPython/,
    'a whitespace-only explicit override must reach validation and fail closed');
  assert.match(launcher, /\$Package/);
  assert.match(launcher, /prepare:python/);
  assert.match(launcher, /build\/check-capture\.cjs|build\\check-capture\.cjs/,
    'source startup must still run the real catalog/save-package capture self-check');
  assert.match(pkg.scripts['prepare:win'], /prepare:python/);
  assert.ok(pkg.build.win.extraResources.some(resource => resource.to === 'python-win-x64'),
    'Windows packages must continue to include the verified embedded runtime');
});
