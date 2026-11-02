'use strict';

function normalizeConnectionIdentity(ssh = {}) {
  const port = Number(ssh.port);
  return JSON.stringify([
    String(ssh.username || ''),
    String(ssh.host || ''),
    Number.isInteger(port) && port > 0 ? port : 22,
    String(ssh.remoteRootPath || ssh.cwd || '~'),
  ]);
}

function isPathWithin(candidate, root) {
  candidate = String(candidate || '');
  root = String(root || '');
  return root === '' || candidate === root || candidate.startsWith(`${root}/`);
}

function ancestorPaths(value) {
  const paths = [];
  let path = String(value || '');
  while (true) {
    paths.push(path);
    if (!path) return paths;
    const separator = path.lastIndexOf('/');
    path = separator === -1 ? '' : path.slice(0, separator);
  }
}

class RemoteTreeCacheMetadata {
  constructor({now = () => Date.now()} = {}) {
    this.now = now;
    this.epoch = 0;
    this.versions = new Map();
    this.refreshedAt = new Map();
    this.subtreeTombstones = new Map();
  }

  reset() {
    this.epoch = 0;
    this.versions.clear();
    this.refreshedAt.clear();
    this.subtreeTombstones.clear();
  }

  markRefreshed(path, {versioned = false, refreshedAt = this.now()} = {}) {
    path = String(path || '');
    if (versioned) this.versions.set(path, (this.versions.get(path) || 0) + 1);
    this.refreshedAt.set(path, Number(refreshedAt));
  }

  markSubtreeRemoved(root, knownPaths = []) {
    root = String(root || '');
    const epoch = ++this.epoch;
    // A tombstone on the removed root fences every descendant, including a
    // directory that was only discovered by an already-running prefetch and
    // therefore has no renderer cache entry yet. Descendant tombstones become
    // redundant once their ancestor carries the newer epoch.
    for (const path of [...this.subtreeTombstones.keys()]) {
      if (isPathWithin(path, root)) this.subtreeTombstones.delete(path);
    }
    this.subtreeTombstones.set(root, epoch);
    for (const path of new Set([root, ...knownPaths].map(value => String(value || '')))) {
      if (!isPathWithin(path, root)) continue;
      this.versions.set(path, (this.versions.get(path) || 0) + 1);
      this.refreshedAt.delete(path);
    }
    return epoch;
  }

  snapshot() {
    return {epoch: this.epoch, versions: new Map(this.versions)};
  }

  canApplyPrefetch(path, snapshot) {
    path = String(path || '');
    if (!snapshot || (this.versions.get(path) || 0) !== (snapshot.versions?.get(path) || 0)) return false;
    return !ancestorPaths(path).some(ancestor => (this.subtreeTombstones.get(ancestor) || 0) > snapshot.epoch);
  }

  isStale(path, maxAgeMs, at = this.now()) {
    const refreshedAt = this.refreshedAt.get(String(path || ''));
    return !Number.isFinite(refreshedAt) || Number(at) - refreshedAt >= maxAgeMs;
  }
}

module.exports = {RemoteTreeCacheMetadata, isPathWithin, normalizeConnectionIdentity};
