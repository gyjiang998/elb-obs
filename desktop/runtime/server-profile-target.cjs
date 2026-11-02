'use strict';

function normalizeRemoteCwd(value) {
  let cwd = String(value || '~').trim() || '~';
  if (cwd === '~/') return '~';
  if (cwd !== '/' && cwd.endsWith('/')) cwd = cwd.replace(/\/+$/, '');
  return cwd || '/';
}

function normalizeSshTarget(value = {}) {
  if (!value || Array.isArray(value) || typeof value !== 'object') value = {};
  const port = Number(value.port);
  return {
    host: String(value.host || '').trim().toLowerCase(),
    port: Number.isInteger(port) && port > 0 && port <= 65535 ? port : 22,
    username: String(value.username || '').trim(),
    cwd: normalizeRemoteCwd(value.cwd),
  };
}

function hasSshTarget(value) {
  const target = normalizeSshTarget(value);
  return Boolean(target.host && target.username);
}

function sameSshTarget(left, right) {
  if (!hasSshTarget(left) || !hasSshTarget(right)) return false;
  const a = normalizeSshTarget(left);
  const b = normalizeSshTarget(right);
  return a.host === b.host && a.port === b.port && a.username === b.username && a.cwd === b.cwd;
}

function serverProfileTargetKey(value) {
  if (!hasSshTarget(value)) return '';
  const target = normalizeSshTarget(value);
  return JSON.stringify([target.host, target.port, target.username, target.cwd]);
}

function parseServerProfileTargetKey(value) {
  try {
    const tuple = JSON.parse(String(value));
    if (!Array.isArray(tuple) || tuple.length !== 4) return null;
    const target = normalizeSshTarget({host: tuple[0], port: tuple[1], username: tuple[2], cwd: tuple[3]});
    return serverProfileTargetKey(target) === String(value) ? target : null;
  } catch { return null; }
}

function sshTargetLabel(value) {
  if (!hasSshTarget(value)) return 'SSH 目标未配置';
  const target = normalizeSshTarget(value);
  return `${target.username}@${target.host}:${target.port} · ${target.cwd}`;
}

module.exports = {
  normalizeRemoteCwd,
  normalizeSshTarget,
  hasSshTarget,
  sameSshTarget,
  serverProfileTargetKey,
  parseServerProfileTargetKey,
  sshTargetLabel,
};
