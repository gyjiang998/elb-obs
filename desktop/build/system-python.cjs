'use strict';

const fs = require('node:fs');
const path = require('node:path');
const {spawnSync} = require('node:child_process');

const MIN_VERSION = [3, 11];
const MAX_VERSION_EXCLUSIVE = [3, 16];
const ISOLATED_ARGUMENTS = ['-B', '-I', '-S', '-X', 'utf8'];
const PROBE = [
  'import json, sys, importlib, platform',
  `for name in ${JSON.stringify(['argparse', 'ctypes', 'datetime', 'hashlib', 'json', 'pathlib', 'shutil', 'signal', 'socket', 'statistics', 'subprocess', 'tempfile', 'threading', 'zipfile'])}: importlib.import_module(name)`,
  'print(json.dumps({"executable": sys.executable, "version": list(sys.version_info[:3]), "implementation": sys.implementation.name, "machine": platform.machine()}))',
].join('\n');

function supported(version) {
  if (!Array.isArray(version) || version.length < 3 || !version.every(Number.isInteger)) return false;
  const [major, minor] = version;
  return major === 3 && minor >= MIN_VERSION[1] && minor < MAX_VERSION_EXCLUSIVE[1];
}

function absolutePath(filename, platform) {
  return (platform === 'win32' ? path.win32 : path.posix).isAbsolute(filename);
}

function asciiJson(value) {
  return JSON.stringify(value).replace(/[\u007f-\uffff]/g,
    character => `\\u${character.charCodeAt(0).toString(16).padStart(4, '0')}`);
}

function probeSystemPython(value, options = {}) {
  if (typeof value !== 'string' || !value.trim() || /[\0\r\n]/.test(value)) {
    throw new Error('HETERO_DESKTOP_PYTHON must be one executable path or PATH command, such as C:\\Python315\\python.exe or python.exe.');
  }
  const command = value.trim();
  const platform = options.platform || process.platform;
  const result = (options.spawnSync || spawnSync)(command, [...ISOLATED_ARGUMENTS, '-c', PROBE], {
    env: {...(options.env || process.env)}, encoding: 'utf8', windowsHide: true,
    timeout: 15000, maxBuffer: 1024 * 1024, shell: false,
  });
  if (result.error) throw new Error(`Configured system Python could not start: ${result.error.message}`);
  if (result.status !== 0 || result.signal) {
    const detail = `${result.stderr || result.stdout || result.signal || `exit ${result.status}`}`.trim().slice(0, 2000);
    throw new Error(`Configured system Python probe failed${detail ? `: ${detail}` : '.'}`);
  }
  let identity;
  try { identity = JSON.parse(`${result.stdout || ''}`.trim()); }
  catch { throw new Error('Configured system Python returned an invalid identity response.'); }
  if (!supported(identity.version)) {
    const found = Array.isArray(identity.version) ? identity.version.join('.') : 'unknown';
    throw new Error(`Configured system Python version ${found} is unsupported; Python 3.11 through 3.15 is required.`);
  }
  if (identity.implementation !== 'cpython') {
    throw new Error(`Configured system Python implementation ${identity.implementation || 'unknown'} is unsupported; CPython is required.`);
  }
  if (typeof identity.executable !== 'string' || !absolutePath(identity.executable, platform)) {
    throw new Error('Configured system Python did not report an absolute executable path.');
  }
  const exists = options.existsSync || fs.existsSync;
  if (!exists(identity.executable)) throw new Error(`Configured system Python executable does not exist: ${identity.executable}`);
  return {executable: identity.executable, version: identity.version, implementation: identity.implementation, machine: identity.machine};
}

if (require.main === module) {
  try {
    const value = process.argv[2];
    if (process.argv.length !== 3) throw new Error('Usage: node build/system-python.cjs <python-executable>');
    // Windows PowerShell 5.1 decodes native stdout using a legacy code page.
    // ASCII-only JSON preserves paths containing Chinese or other Unicode;
    // ConvertFrom-Json reconstructs the original string from \u escapes.
    process.stdout.write(`${asciiJson(probeSystemPython(value))}\n`);
  } catch (error) {
    console.error(`System Python probe failed: ${error.message}`);
    process.exitCode = 1;
  }
}

module.exports = {probeSystemPython, supported, asciiJson, ISOLATED_ARGUMENTS, MIN_VERSION, MAX_VERSION_EXCLUSIVE};
