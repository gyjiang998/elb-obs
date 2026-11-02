function normalizedCwd(value) {
  return String(value || '~').trim().replace(/\/+$/, '') || '/';
}

function normalizedAuth(value) {
  return ['password', 'key', 'agent'].includes(value) ? value : 'password';
}

function sameSshConnectionProfile(left = {}, right = {}) {
  const leftAuth = normalizedAuth(left.auth);
  const rightAuth = normalizedAuth(right.auth);
  if (leftAuth !== rightAuth) return false;
  if (leftAuth === 'key'
    && String(left.privateKeyPath || '').trim() !== String(right.privateKeyPath || '').trim()) return false;
  return String(left.host || '').trim().toLowerCase() === String(right.host || '').trim().toLowerCase()
    && Number(left.port || 22) === Number(right.port || 22)
    && String(left.username || '').trim() === String(right.username || '').trim()
    && normalizedCwd(left.cwd) === normalizedCwd(right.cwd);
}

module.exports = { sameSshConnectionProfile };
