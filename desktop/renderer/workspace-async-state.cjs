'use strict';

function normalizeConnection(connection) {
  if (!connection) return null;
  const target = connection.target || {};
  const rawGeneration = connection.generation;
  const generation = rawGeneration === null || rawGeneration === undefined || rawGeneration === ''
    ? null
    : Number(rawGeneration);
  return Object.freeze({
    target: Object.freeze({
      host: String(target.host || ''),
      port: Number(target.port) || 22,
      username: String(target.username || ''),
      cwd: String(target.cwd || '~'),
    }),
    root: String(connection.root || target.cwd || '~'),
    generation: Number.isSafeInteger(generation) ? generation : null,
  });
}

function normalizeWorkspaceBinding(binding = {}) {
  return Object.freeze({
    query: String(binding.query || ''),
    caseSensitive: Boolean(binding.caseSensitive),
    source: binding.source === 'local' ? 'local' : 'remote',
    identity: String(binding.identity || ''),
    connection: normalizeConnection(binding.connection),
    grantId: String(binding.grantId || ''),
  });
}

function workspaceBindingKey(binding = {}) {
  const value = normalizeWorkspaceBinding(binding);
  const connection = value.connection;
  return JSON.stringify([
    value.query,
    value.caseSensitive,
    value.source,
    value.identity,
    value.grantId,
    connection?.target.host || '',
    connection?.target.port || 0,
    connection?.target.username || '',
    connection?.target.cwd || '',
    connection?.root || '',
    connection?.generation ?? null,
  ]);
}

function sameWorkspaceBinding(left, right) {
  return workspaceBindingKey(left) === workspaceBindingKey(right);
}

class WorkspaceSearchSession {
  constructor() {
    this.sequence = 0;
    this.current = null;
    this.snapshot = null;
  }

  begin(binding, requestId) {
    this.invalidate();
    const normalized = normalizeWorkspaceBinding(binding);
    const token = Object.freeze({
      kind: 'workspace-search',
      sequence: ++this.sequence,
      requestId: String(requestId || ''),
      binding: normalized,
      bindingKey: workspaceBindingKey(normalized),
    });
    this.current = token;
    return token;
  }

  invalidate() {
    const invalidated = this.current;
    this.current = null;
    this.snapshot = null;
    return invalidated;
  }

  accept(token, result, currentBinding) {
    if (token !== this.current) return false;
    if (!sameWorkspaceBinding(token.binding, currentBinding)) return false;
    this.snapshot = Object.freeze({
      binding: token.binding,
      bindingKey: token.bindingKey,
      requestId: token.requestId,
      result,
    });
    return true;
  }

  finish(token) {
    if (token !== this.current) return false;
    this.current = null;
    return true;
  }

  snapshotFor(binding) {
    if (!this.snapshot) return null;
    return this.snapshot.bindingKey === workspaceBindingKey(binding) ? this.snapshot : null;
  }
}

class WorkspaceReadTokens {
  constructor() {
    this.sequence = 0;
    this.currentByKey = new Map();
  }

  begin(binding, key) {
    const normalizedKey = String(key || '');
    const normalized = normalizeWorkspaceBinding(binding);
    const token = Object.freeze({
      kind: 'workspace-read',
      sequence: ++this.sequence,
      key: normalizedKey,
      binding: normalized,
      bindingKey: workspaceBindingKey(normalized),
    });
    this.currentByKey.set(normalizedKey, token);
    return token;
  }

  invalidate(predicate = () => true) {
    const invalidated = [];
    for (const [key, token] of this.currentByKey) {
      if (!predicate(token)) continue;
      this.currentByKey.delete(key);
      invalidated.push(token);
    }
    return invalidated;
  }

  accept(token, _result, currentBinding) {
    return this.currentByKey.get(token?.key) === token
      && sameWorkspaceBinding(token.binding, currentBinding);
  }

  finish(token) {
    if (this.currentByKey.get(token?.key) !== token) return false;
    this.currentByKey.delete(token.key);
    return true;
  }

  pendingFor(key) {
    return this.currentByKey.get(String(key || '')) || null;
  }
}

module.exports = {
  WorkspaceReadTokens,
  WorkspaceSearchSession,
  normalizeWorkspaceBinding,
  sameWorkspaceBinding,
  workspaceBindingKey,
};
