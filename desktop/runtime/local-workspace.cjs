'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const path = require('node:path');

const MAX_TEXT_BYTES = 1024 * 1024;
const MAX_DIRECTORY_ENTRIES = 5000;
const MAX_SEARCH_RESULTS = 1000;
const MAX_SEARCH_FILES = 20000;
const MAX_SEARCH_DIRECTORIES = 5000;
const MAX_SEARCH_QUEUE_ENTRIES = 20000;
const MAX_SEARCH_DIRECTORY_ENTRIES = 5000;
const LOCAL_HANDLE_TRANSIENT_CODES = new Set(['EPERM', 'EACCES', 'EBUSY']);
const DEFAULT_SKIPPED_DIRECTORIES = new Set([
  '.git', '.cache', '.next', '.venv', '__pycache__', 'build', 'dist',
  'node_modules', 'out', 'target', 'venv',
]);

class LocalWorkspaceError extends Error {
  constructor(code, message, cause = null) {
    super(message, cause ? {cause} : undefined);
    this.name = 'LocalWorkspaceError';
    this.code = code;
  }
}

function failure(code, message, cause = null) {
  return new LocalWorkspaceError(code, message, cause);
}

function normalizeRelativePath(value = '', {allowRoot = true} = {}) {
  if (typeof value !== 'string' || value.includes('\0') || value.includes('\\'))
    throw failure('LOCAL_WORKSPACE_PATH_INVALID', '本地工作区路径无效。');
  if (path.posix.isAbsolute(value))
    throw failure('LOCAL_WORKSPACE_PATH_INVALID', '本地工作区只接受根目录内的相对路径。');
  const normalized = path.posix.normalize(value || '.');
  if (normalized === '..' || normalized.startsWith('../'))
    throw failure('LOCAL_WORKSPACE_PATH_OUTSIDE_ROOT', '路径位于已授权的本地工作区之外。');
  const relative = normalized === '.' ? '' : normalized.replace(/^\.\//, '');
  if (!allowRoot && !relative) throw failure('LOCAL_WORKSPACE_PATH_INVALID', '请选择工作区内的文件。');
  if (Buffer.byteLength(relative, 'utf8') > 4096)
    throw failure('LOCAL_WORKSPACE_PATH_INVALID', '本地工作区路径过长。');
  return relative;
}

function isInsideRoot(root, candidate) {
  const relative = path.relative(root, candidate);
  return relative === '' || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative));
}

function revision(bytes) {
  return `sha256:${crypto.createHash('sha256').update(bytes).digest('hex')}`;
}

function metadata(attributes) {
  return {
    size: Number(attributes.size),
    mtimeMs: Math.trunc(Number(attributes.mtimeMs)),
    modifiedAt: attributes.mtime.toISOString(),
  };
}

function fileIdentity(attributes) {
  return {
    device: String(attributes?.dev ?? ''),
    inode: String(attributes?.ino ?? ''),
  };
}

function sameFileIdentity(left, right) {
  const a = fileIdentity(left); const b = fileIdentity(right);
  return Boolean(a.device && a.inode && a.device === b.device && a.inode === b.inode);
}

function directoryEntryType(attributes) {
  if (attributes.isSymbolicLink()) return 'symlink';
  if (attributes.isDirectory()) return 'directory';
  if (attributes.isFile()) return 'file';
  return 'other';
}

function direntType(entry) {
  if (entry.isSymbolicLink()) return 'symlink';
  if (entry.isDirectory()) return 'directory';
  if (entry.isFile()) return 'file';
  // Some filesystems do not populate d_type. In that case lstat below is the
  // source of truth and the second snapshot still verifies the object ID.
  return 'unknown';
}

async function readHandleBytes(handle, size) {
  const length = Number(size);
  if (!Number.isSafeInteger(length) || length < 0 || length > MAX_TEXT_BYTES)
    throw failure('LOCAL_WORKSPACE_FILE_TOO_LARGE', '本地文本文件超过 1 MiB，无法在编辑器中打开。');
  const bytes = Buffer.alloc(length);
  let offset = 0;
  while (offset < length) {
    const {bytesRead} = await handle.read(bytes, offset, length - offset, offset);
    if (!bytesRead) break;
    offset += bytesRead;
  }
  return offset === length ? bytes : bytes.subarray(0, offset);
}

async function writeHandleBytes(handle, bytes) {
  await handle.truncate(0);
  let offset = 0;
  while (offset < bytes.length) {
    const {bytesWritten} = await handle.write(bytes, offset, bytes.length - offset, offset);
    if (!bytesWritten) throw failure('LOCAL_WORKSPACE_WRITE_FAILED', '本地文件写入没有取得进展。');
    offset += bytesWritten;
  }
  await handle.sync();
}

function decodeTextDocument(bytes) {
  if (bytes.includes(0)) throw failure('LOCAL_WORKSPACE_BINARY_FILE', '文件包含二进制数据，无法作为文本打开。');
  const bom = bytes.length >= 3 && bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf;
  const body = bom ? bytes.subarray(3) : bytes;
  let text;
  try { text = new TextDecoder('utf-8', {fatal: true}).decode(body); }
  catch (error) { throw failure('LOCAL_WORKSPACE_BINARY_FILE', '文件不是有效的 UTF-8 文本。', error); }
  const crlf = (text.match(/\r\n/g) || []).length;
  const withoutCrlf = text.replaceAll('\r\n', '');
  const cr = (withoutCrlf.match(/\r/g) || []).length;
  const lf = (withoutCrlf.match(/\n/g) || []).length;
  const types = [crlf > 0, cr > 0, lf > 0].filter(Boolean).length;
  const lineEnding = types > 1 ? 'mixed' : crlf ? 'crlf' : cr ? 'cr' : 'lf';
  return {content: text.replace(/\r\n?/g, '\n'), bom, lineEnding};
}

function encodeTextDocument(content, {bom = false, lineEnding = 'lf'} = {}) {
  if (typeof content !== 'string') throw failure('LOCAL_WORKSPACE_CONTENT_INVALID', '文件内容必须是文本。');
  if (!['lf', 'crlf', 'cr'].includes(lineEnding))
    throw failure('LOCAL_WORKSPACE_CONTENT_INVALID', '保存所用换行格式无效。');
  let normalized = content.replace(/\r\n?/g, '\n');
  if (lineEnding === 'crlf') normalized = normalized.replaceAll('\n', '\r\n');
  else if (lineEnding === 'cr') normalized = normalized.replaceAll('\n', '\r');
  const body = Buffer.from(normalized, 'utf8');
  const bytes = bom ? Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), body]) : body;
  if (bytes.length > MAX_TEXT_BYTES)
    throw failure('LOCAL_WORKSPACE_FILE_TOO_LARGE', '本地文本文件超过 1 MiB，无法保存。');
  return bytes;
}

function replacementExpression({query, regex = false, caseSensitive = false}) {
  if (typeof query !== 'string' || !query || Buffer.byteLength(query, 'utf8') > 4096)
    throw failure('LOCAL_WORKSPACE_SEARCH_INVALID', '搜索内容必须是非空短文本。');
  // Local workspace operations execute in Electron's Main Process. Keep the
  // first UI contract literal-only so an untrusted renderer cannot submit a
  // catastrophic backtracking expression and freeze the whole application.
  if (regex) throw failure('LOCAL_WORKSPACE_SEARCH_INVALID', '本地工作区首版仅支持文本搜索。');
  const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp(escaped, `${caseSensitive ? '' : 'i'}g`);
}

class LocalWorkspaceRegistry {
  constructor() {
    this.grants = new Map();
    this.activeSearches = new Map();
  }

  async openPathHandle(filename, flags, mode) { return fs.open(filename, flags, mode); }
  async openDirectory(filename) { return fs.opendir(filename); }

  directoryHandleUnsupported(error) {
    return ['EISDIR', 'EPERM', 'EACCES', 'EINVAL', 'ENOTSUP', 'ENOSYS'].includes(error?.code);
  }

  ownerGrants(ownerId) {
    const key = String(ownerId);
    let grants = this.grants.get(key);
    if (!grants) { grants = new Map(); this.grants.set(key, grants); }
    return grants;
  }

  async authorize(ownerId, selectedRoot, {source = 'selected'} = {}) {
    if (typeof selectedRoot !== 'string' || !selectedRoot.trim())
      throw failure('LOCAL_WORKSPACE_ROOT_INVALID', '请选择本地工作目录。');
    if (!path.isAbsolute(selectedRoot))
      throw failure('LOCAL_WORKSPACE_ROOT_INVALID', '本地工作目录必须是绝对路径。');
    const ownerKey = String(ownerId);
    // Capture the owner's map before the first await. revokeOwner removes this
    // exact map, so a window destroyed while the picker/handle work is pending
    // cannot receive a grant in an orphaned map or leak the root handle.
    const grants = this.ownerGrants(ownerId);
    const absolute = path.resolve(selectedRoot);
    let root;
    let attributes;
    try {
      root = await fs.realpath(absolute);
      attributes = await fs.stat(root);
    } catch (error) {
      throw failure('LOCAL_WORKSPACE_ROOT_INVALID', '本地工作目录不存在或无法访问。', error);
    }
    if (!attributes.isDirectory()) throw failure('LOCAL_WORKSPACE_ROOT_INVALID', '所选本地工作区不是目录。');
    const existing = [...grants.values()].find(grant => grant.root === root);
    if (existing) {
      if (this.grants.get(ownerKey) !== grants)
        throw failure('LOCAL_WORKSPACE_GRANT_INVALID', '工作区窗口已关闭，本地目录授权未生效。');
      return this.publicGrant(existing);
    }
    let rootHandle = null;
    let rootIdentity;
    try {
      rootHandle = await this.openPathHandle(root, fs.constants.O_RDONLY | (fs.constants.O_DIRECTORY || 0));
      rootIdentity = await rootHandle.stat({bigint: true});
    } catch (error) {
      await rootHandle?.close().catch(() => {});
      rootHandle = null;
      if (!this.directoryHandleUnsupported(error))
        throw failure('LOCAL_WORKSPACE_ROOT_INVALID', '无法建立本地工作区根目录句柄。', error);
      // libuv cannot open directory FileHandles on some Windows builds. Keep
      // the canonical directory identity and revalidate it before and after
      // every operation; ordinary files still require a verified handle.
      rootIdentity = await fs.stat(root, {bigint: true});
    }
    if (this.grants.get(ownerKey) !== grants) {
      await rootHandle?.close().catch(() => {});
      throw failure('LOCAL_WORKSPACE_GRANT_INVALID', '工作区窗口已关闭，本地目录授权未生效。');
    }
    const concurrent = [...grants.values()].find(grant => grant.root === root);
    if (concurrent) {
      await rootHandle?.close().catch(() => {});
      if (this.grants.get(ownerKey) !== grants)
        throw failure('LOCAL_WORKSPACE_GRANT_INVALID', '工作区窗口已关闭，本地目录授权未生效。');
      return this.publicGrant(concurrent);
    }
    const grant = {id: crypto.randomUUID(), root, rootHandle, rootIdentity, source, createdAt: Date.now()};
    grants.set(grant.id, grant);
    return this.publicGrant(grant);
  }

  publicGrant(grant) {
    return {grantId: grant.id, root: grant.root, name: path.basename(grant.root) || grant.root, source: grant.source};
  }

  requireGrant(ownerId, grantId) {
    const grant = this.grants.get(String(ownerId))?.get(String(grantId || ''));
    if (!grant) throw failure('LOCAL_WORKSPACE_GRANT_INVALID', '本地工作区授权已失效，请重新选择目录。');
    return grant;
  }

  async revokeOwner(ownerId) {
    const prefix = `${String(ownerId)}:`;
    for (const [key, controller] of this.activeSearches) {
      if (!key.startsWith(prefix)) continue;
      controller.abort();
      this.activeSearches.delete(key);
    }
    const grants = this.grants.get(String(ownerId));
    this.grants.delete(String(ownerId));
    const records = grants ? [...grants.values()] : [];
    await Promise.all(records.map(grant => grant.rootHandle?.close().catch(() => {})));
  }

  async assertGrantRoot(grant) {
    let held; let current; let canonical;
    try {
      held = grant.rootHandle ? await grant.rootHandle.stat({bigint: true}) : grant.rootIdentity;
      canonical = await fs.realpath(grant.root);
      current = await fs.stat(canonical, {bigint: true});
    } catch (error) {
      throw failure('LOCAL_WORKSPACE_ROOT_CHANGED', '已授权的本地工作区根目录已变化，请重新选择目录。', error);
    }
    if (canonical !== grant.root || !sameFileIdentity(held, grant.rootIdentity)
        || !sameFileIdentity(current, grant.rootIdentity))
      throw failure('LOCAL_WORKSPACE_ROOT_CHANGED', '已授权的本地工作区根目录身份已变化，请重新选择目录。');
  }

  async resolveExisting(grant, requestedPath = '', {allowRoot = true} = {}) {
    await this.assertGrantRoot(grant);
    const relative = normalizeRelativePath(requestedPath, {allowRoot});
    const lexical = path.join(grant.root, ...relative.split('/').filter(Boolean));
    let canonical;
    try { canonical = await fs.realpath(lexical); }
    catch (error) { throw failure('LOCAL_WORKSPACE_PATH_NOT_FOUND', `本地工作区路径不存在：${relative || '.'}`, error); }
    if (!isInsideRoot(grant.root, canonical))
      throw failure('LOCAL_WORKSPACE_PATH_OUTSIDE_ROOT', '符号链接指向已授权工作区之外，已拒绝访问。');
    return {relative, lexical, canonical};
  }

  async openVerified(grant, requestedPath = '', {
    allowRoot = true,
    flags = fs.constants.O_RDONLY,
    expectedType = null,
    rejectFinalSymlink = false,
  } = {}) {
    const resolved = await this.resolveExisting(grant, requestedPath, {allowRoot});
    let resolvedAttributes;
    try { resolvedAttributes = await fs.stat(resolved.canonical, {bigint: true}); }
    catch (error) { throw failure('LOCAL_WORKSPACE_PATH_CHANGED', '路径在打开前发生变化，请重试。', error); }
    if (rejectFinalSymlink) {
      const lexicalAttributes = await fs.lstat(resolved.lexical).catch(error => {
        throw failure('LOCAL_WORKSPACE_PATH_NOT_FOUND', `本地工作区路径不存在：${resolved.relative || '.'}`, error);
      });
      if (lexicalAttributes.isSymbolicLink())
        throw failure('LOCAL_WORKSPACE_SYMLINK_WRITE_BLOCKED', '为避免覆盖符号链接目标，本地工作区不保存符号链接。');
    }
    let handle;
    try {
      try { handle = await this.openPathHandle(resolved.canonical, flags | (fs.constants.O_NOFOLLOW || 0)); }
      catch (error) {
        if (!resolvedAttributes.isDirectory() || !this.directoryHandleUnsupported(error)) throw error;
      }
      const held = handle ? await handle.stat({bigint: true}) : resolvedAttributes;
      const currentCanonical = await fs.realpath(resolved.lexical);
      if (!isInsideRoot(grant.root, currentCanonical))
        throw failure('LOCAL_WORKSPACE_PATH_OUTSIDE_ROOT', '路径在打开期间离开了已授权工作区，已拒绝访问。');
      const current = await fs.stat(currentCanonical, {bigint: true});
      if (!sameFileIdentity(held, current) || (!handle && !sameFileIdentity(resolvedAttributes, current)))
        throw failure('LOCAL_WORKSPACE_PATH_CHANGED', '路径在打开期间发生变化，请重试。');
      if (rejectFinalSymlink) {
        const currentLexical = await fs.lstat(resolved.lexical, {bigint: true});
        if (currentLexical.isSymbolicLink())
          throw failure('LOCAL_WORKSPACE_SYMLINK_WRITE_BLOCKED', '文件在打开期间变成了符号链接，已拒绝保存。');
        if (!sameFileIdentity(held, currentLexical))
          throw failure('LOCAL_WORKSPACE_PATH_CHANGED', '文件目录项在打开期间发生变化，请重试。');
      }
      if (expectedType === 'file' && !held.isFile())
        throw failure('LOCAL_WORKSPACE_NOT_FILE', '所选路径不是普通文件。');
      if (expectedType === 'directory' && !held.isDirectory())
        throw failure('LOCAL_WORKSPACE_NOT_DIRECTORY', '所选路径不是目录。');
      await this.assertGrantRoot(grant);
      return {...resolved, handle, identity: held, directoryHandleFallback: !handle};
    } catch (error) {
      await handle?.close().catch(() => {});
      if (error instanceof LocalWorkspaceError) throw error;
      throw failure('LOCAL_WORKSPACE_PATH_CHANGED', '无法安全打开本地工作区路径，请重试。', error);
    }
  }

  async revalidateOpenPath(grant, opened) {
    await this.assertGrantRoot(grant);
    let held; let canonical; let current;
    try {
      held = opened.handle ? await opened.handle.stat({bigint: true}) : opened.identity;
      canonical = await fs.realpath(opened.lexical);
      current = await fs.stat(canonical, {bigint: true});
    } catch (error) {
      throw failure('LOCAL_WORKSPACE_PATH_CHANGED', '本地工作区路径在操作期间发生变化，请重试。', error);
    }
    if (!isInsideRoot(grant.root, canonical) || !sameFileIdentity(held, opened.identity)
        || !sameFileIdentity(current, opened.identity))
      throw failure('LOCAL_WORKSPACE_PATH_CHANGED', '本地工作区路径在操作期间发生变化，请重试。');
  }

  async statOpened(opened, options = undefined) {
    return opened.handle ? opened.handle.stat(options) : fs.stat(opened.canonical, options);
  }

  async takeDirectorySnapshot(filename, {maxEntries, stop = null} = {}) {
    const entries = [];
    let overflow = false;
    let stopped = null;
    const directory = await this.openDirectory(filename);
    try {
      for await (const item of directory) {
        stopped = stop?.() || null;
        if (stopped) break;
        if (entries.length >= maxEntries) { overflow = true; break; }
        const itemPath = path.join(filename, item.name);
        let attributes;
        try { attributes = await fs.lstat(itemPath, {bigint: true}); }
        catch (error) {
          throw failure('LOCAL_WORKSPACE_DIRECTORY_CHANGED',
            '目录在枚举期间发生变化，未返回本次结果。', error);
        }
        const observedType = direntType(item);
        const currentType = directoryEntryType(attributes);
        if (observedType !== 'unknown' && observedType !== currentType)
          throw failure('LOCAL_WORKSPACE_DIRECTORY_CHANGED',
            '目录项类型在枚举期间发生变化，未返回本次结果。');
        entries.push({name: item.name, type: currentType,
          symlink: currentType === 'symlink', identity: fileIdentity(attributes), attributes});
      }
    } finally { await directory.close().catch(() => {}); }
    entries.sort((left, right) => left.name < right.name ? -1 : left.name > right.name ? 1 : 0);
    return {entries, overflow, stopped};
  }

  sameDirectorySnapshot(left, right) {
    if (left.overflow !== right.overflow || left.entries.length !== right.entries.length) return false;
    return left.entries.every((entry, index) => {
      const current = right.entries[index];
      return entry.name === current.name && entry.type === current.type
        && entry.symlink === current.symlink
        && entry.identity.device === current.identity.device
        && entry.identity.inode === current.identity.inode;
    });
  }

  async verifiedDirectorySnapshot(grant, opened, {maxEntries, stop = null} = {}) {
    const first = await this.takeDirectorySnapshot(opened.canonical, {maxEntries, stop});
    if (first.stopped) return first;
    await this.revalidateOpenPath(grant, opened);
    // An overflow never exposes or queues its prefix. Return the bounded
    // outcome directly: comparing two arbitrary readdir prefixes would turn a
    // normal oversized directory into a false change error on filesystems
    // whose enumeration order is not stable.
    if (first.overflow) return first;
    // Windows/libuv may not provide a directory FileHandle. A junction can be
    // swapped out and restored between opendir and path revalidation (ABA), so
    // enumerate the current authorized path again and compare every name,
    // type and filesystem identity before any result is exposed or queued.
    const second = await this.takeDirectorySnapshot(opened.canonical, {maxEntries, stop});
    if (second.stopped) return second;
    await this.revalidateOpenPath(grant, opened);
    if (!this.sameDirectorySnapshot(first, second))
      throw failure('LOCAL_WORKSPACE_DIRECTORY_CHANGED',
        '目录在枚举期间发生变化，未返回本次结果。');
    return second;
  }

  async list(ownerId, value = {}) {
    const grant = this.requireGrant(ownerId, value.grantId);
    const opened = await this.openVerified(grant, value.path || '', {expectedType: 'directory'});
    try {
      const snapshot = await this.verifiedDirectorySnapshot(grant, opened, {maxEntries: MAX_DIRECTORY_ENTRIES});
      if (snapshot.overflow)
        throw failure('LOCAL_WORKSPACE_DIRECTORY_TOO_LARGE', `目录包含超过 ${MAX_DIRECTORY_ENTRIES} 个条目，请缩小浏览范围。`);
      const entries = [];
      for (const item of snapshot.entries) {
          const entryRelative = opened.relative ? `${opened.relative}/${item.name}` : item.name;
          const lexical = path.join(opened.canonical, item.name);
          const symlink = item.symlink;
          let accessible = true;
          let effective = item.attributes;
          if (symlink) {
            try {
              const canonical = await fs.realpath(lexical);
              accessible = isInsideRoot(grant.root, canonical);
              if (accessible) effective = await fs.stat(canonical);
            } catch { accessible = false; }
          }
          const type = effective.isDirectory() ? 'directory' : effective.isFile() ? 'file' : symlink ? 'symlink' : 'other';
          entries.push({name: item.name, path: entryRelative, type, symlink, accessible, ...metadata(effective)});
      }
      await this.revalidateOpenPath(grant, opened);
      entries.sort((left, right) => {
        const leftDir = left.type === 'directory'; const rightDir = right.type === 'directory';
        return leftDir === rightDir ? left.name.localeCompare(right.name) : leftDir ? -1 : 1;
      });
      return {path: opened.relative, entries, truncated: false, grant: this.publicGrant(grant)};
    } finally { await opened.handle?.close().catch(() => {}); }
  }

  async read(ownerId, value = {}) {
    const grant = this.requireGrant(ownerId, value.grantId);
    const opened = await this.openVerified(grant, value.path, {allowRoot: false, expectedType: 'file'});
    try {
      const attributes = await opened.handle.stat();
      const bytes = await readHandleBytes(opened.handle, attributes.size);
      await this.revalidateOpenPath(grant, opened);
      const document = decodeTextDocument(bytes);
      return {path: opened.relative, ...document, encoding: 'utf8', ...metadata(attributes), revision: revision(bytes)};
    } finally { await opened.handle?.close().catch(() => {}); }
  }

  async writeOpenedBytes(handle, bytes) { await writeHandleBytes(handle, bytes); }

  async writeOpenedBytesWithRetry(handle, baseline, bytes, attempts = 3) {
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      try { await this.writeOpenedBytes(handle, bytes); return; }
      catch (error) {
        try { await writeHandleBytes(handle, baseline); }
        catch (restoreError) {
          const recovery = failure('LOCAL_WORKSPACE_REPLACE_RECOVERY_REQUIRED',
            '本地文件提交失败且原内容无法自动恢复；请从编辑器缓冲区另存内容。', error);
          recovery.restoreCause = restoreError;
          throw recovery;
        }
        if (LOCAL_HANDLE_TRANSIENT_CODES.has(error?.code) && attempt + 1 < attempts) {
          await new Promise(resolve => setTimeout(resolve, 25 * (attempt + 1)));
          continue;
        }
        error.localWorkspaceBaselineRestored = true;
        throw error;
      }
    }
  }

  async overwriteOpenedFile(grant, opened, baseline, bytes) {
    let writeStarted = false;
    try {
      const currentAttributes = await opened.handle.stat();
      const current = await readHandleBytes(opened.handle, currentAttributes.size);
      if (revision(current) !== revision(baseline))
        throw failure('LOCAL_WORKSPACE_FILE_CHANGED', '文件在保存期间发生变化，已保留磁盘内容。');
      writeStarted = true;
      await this.writeOpenedBytesWithRetry(opened.handle, baseline, bytes);
      const after = await opened.handle.stat();
      const observed = await readHandleBytes(opened.handle, after.size);
      if (revision(observed) !== revision(bytes))
        throw failure('LOCAL_WORKSPACE_WRITE_FAILED', '写入后的本地文件校验失败。');
      await this.revalidateOpenPath(grant, opened);
    } catch (error) {
      if (!writeStarted || error?.localWorkspaceBaselineRestored) throw error;
      try { await writeHandleBytes(opened.handle, baseline); }
      catch (restoreError) {
        const recovery = failure('LOCAL_WORKSPACE_REPLACE_RECOVERY_REQUIRED',
          '本地文件提交失败且原内容无法自动恢复；请从编辑器缓冲区另存内容。', error);
        recovery.restoreCause = restoreError;
        throw recovery;
      }
      throw error;
    }
  }

  async save(ownerId, value = {}) {
    const grant = this.requireGrant(ownerId, value.grantId);
    const relative = normalizeRelativePath(value.path, {allowRoot: false});
    const expectedRevision = String(value.expectedRevision || '');
    if (!/^sha256:[0-9a-f]{64}$/.test(expectedRevision))
      throw failure('LOCAL_WORKSPACE_FILE_CHANGED', '覆盖本地文件必须携带读取时的 revision。');
    const target = await this.openVerified(grant, relative, {
      allowRoot: false,
      flags: fs.constants.O_RDWR,
      expectedType: 'file',
      rejectFinalSymlink: true,
    });
    try {
      const attributes = await target.handle.stat();
      const baseline = await readHandleBytes(target.handle, attributes.size);
      if (revision(baseline) !== expectedRevision)
        throw failure('LOCAL_WORKSPACE_FILE_CHANGED', '文件已在磁盘上发生变化，请重新打开后保存。');
      const bytes = encodeTextDocument(value.content, {bom: Boolean(value.bom), lineEnding: value.lineEnding || 'lf'});
      await this.revalidateOpenPath(grant, target);
      // Commit through the verified file capability. A final pathname rename
      // would reopen the parent-directory symlink race and is also unreliable
      // when Windows scanners hold the destination. The handle write performs
      // a second revision CAS, fsyncs, verifies the result and restores the
      // original bytes through the same handle if any step fails.
      await this.overwriteOpenedFile(grant, target, baseline, bytes);
      const after = await target.handle.stat();
      return {path: relative, saved: true, encoding: 'utf8', bom: Boolean(value.bom),
        lineEnding: value.lineEnding || 'lf', ...metadata(after), revision: revision(bytes)};
    } finally {
      await target.handle.close().catch(() => {});
    }
  }

  searchKey(ownerId, requestId) { return `${String(ownerId)}:${String(requestId || '')}`; }

  cancelSearch(ownerId, requestId) {
    const key = this.searchKey(ownerId, requestId);
    const controller = this.activeSearches.get(key);
    if (!controller) return {requestId: String(requestId || ''), cancelled: false};
    controller.abort();
    return {requestId: String(requestId || ''), cancelled: true};
  }

  async search(ownerId, value = {}) {
    const grant = this.requireGrant(ownerId, value.grantId);
    const requestId = String(value.requestId || '');
    if (!/^[A-Za-z0-9._:-]{1,128}$/.test(requestId))
      throw failure('LOCAL_WORKSPACE_SEARCH_INVALID', '搜索请求 ID 无效。');
    const expression = replacementExpression(value);
    const maxResults = Math.min(MAX_SEARCH_RESULTS, Math.max(1, Number(value.maxResults) || 200));
    const maxFiles = Math.min(MAX_SEARCH_FILES, Math.max(1, Number(value.maxFiles) || 5000));
    const maxDirectories = Math.min(MAX_SEARCH_DIRECTORIES, Math.max(1, Number(value.maxDirectories) || 2000));
    const maxQueueEntries = Math.min(MAX_SEARCH_QUEUE_ENTRIES, Math.max(1, Number(value.maxQueueEntries) || 10000));
    const maxDirectoryEntries = Math.min(MAX_SEARCH_DIRECTORY_ENTRIES,
      Math.max(1, Number(value.maxDirectoryEntries) || MAX_SEARCH_DIRECTORY_ENTRIES));
    const maxFileBytes = Math.min(MAX_TEXT_BYTES, Math.max(1024, Number(value.maxFileBytes) || MAX_TEXT_BYTES));
    const timeBudgetMs = Math.min(30000, Math.max(100, Number(value.timeBudgetMs) || 10000));
    const controller = new AbortController();
    const key = this.searchKey(ownerId, requestId);
    this.activeSearches.get(key)?.abort();
    this.activeSearches.set(key, controller);
    const started = Date.now();
    const matches = [];
    let scannedFiles = 0; let scannedDirectories = 0; let skippedFiles = 0;
    let discoveredEntries = 0; let reason = null; let startRelative = '';
    const queue = [];
    try {
      const start = await this.resolveExisting(grant, value.path || '');
      startRelative = start.relative;
      queue.push({relative: start.relative});
      while (queue.length && !reason) {
        if (controller.signal.aborted) { reason = 'cancelled'; break; }
        if (Date.now() - started >= timeBudgetMs) { reason = 'time_budget'; break; }
        const current = queue.shift();
        let opened;
        try {
          opened = await this.openVerified(grant, current.relative);
        } catch (error) {
          if (error?.code === 'LOCAL_WORKSPACE_PATH_NOT_FOUND') { skippedFiles += 1; continue; }
          throw error;
        }
        try {
          const attributes = await this.statOpened(opened);
          if (attributes.isDirectory()) {
            if (current.relative && DEFAULT_SKIPPED_DIRECTORIES.has(path.posix.basename(current.relative))) continue;
            if (scannedDirectories >= maxDirectories) { reason = 'directory_limit'; break; }
            scannedDirectories += 1;
            const snapshot = await this.verifiedDirectorySnapshot(grant, opened, {
              maxEntries: maxDirectoryEntries,
              stop: () => controller.signal.aborted
                ? 'cancelled'
                : Date.now() - started >= timeBudgetMs ? 'time_budget' : null,
            });
            if (snapshot.stopped) { reason = snapshot.stopped; break; }
            if (snapshot.overflow) {
              discoveredEntries += snapshot.entries.length + 1;
              reason = 'directory_entry_limit';
              break;
            }
            for (const child of snapshot.entries) {
              discoveredEntries += 1;
              if (discoveredEntries > maxQueueEntries || queue.length >= maxQueueEntries) {
                reason = 'queue_limit'; break;
              }
              if (child.symlink) continue;
              queue.push({relative: current.relative ? `${current.relative}/${child.name}` : child.name});
            }
            continue;
          }
          if (!attributes.isFile()) { skippedFiles += 1; continue; }
          if (scannedFiles >= maxFiles) { reason = 'file_limit'; break; }
          scannedFiles += 1;
          if (attributes.size > maxFileBytes) { skippedFiles += 1; continue; }
          let bytes; let text;
          try {
            bytes = await readHandleBytes(opened.handle, attributes.size);
            text = decodeTextDocument(bytes).content;
          } catch (error) {
            if (error instanceof LocalWorkspaceError && error.code !== 'LOCAL_WORKSPACE_BINARY_FILE') throw error;
            skippedFiles += 1;
            continue;
          }
          await this.revalidateOpenPath(grant, opened);
          const fileRevision = revision(bytes);
          for (const [index, line] of text.split('\n').entries()) {
            expression.lastIndex = 0;
            const match = expression.exec(line);
            if (!match) continue;
            matches.push({path: current.relative, line: index + 1, column: match.index + 1,
              preview: line.slice(0, 500), revision: fileRevision, size: attributes.size,
              mtimeMs: Math.trunc(attributes.mtimeMs)});
            if (matches.length >= maxResults) { reason = 'result_limit'; break; }
          }
        } finally { await opened.handle?.close().catch(() => {}); }
      }
      return {query: value.query, path: startRelative, matches, truncated: Boolean(reason), reason,
        stats: {scannedFiles, scannedDirectories, skippedFiles, discoveredEntries,
          durationMs: Date.now() - started}};
    } finally {
      if (this.activeSearches.get(key) === controller) this.activeSearches.delete(key);
    }
  }

  async replace(ownerId, value = {}) {
    const grant = this.requireGrant(ownerId, value.grantId);
    const replacement = String(value.replacement ?? '');
    if (Buffer.byteLength(replacement, 'utf8') > 1024 * 1024)
      throw failure('LOCAL_WORKSPACE_REPLACE_INVALID', '替换文本超过 1 MiB。');
    const expression = replacementExpression(value);
    const snapshots = Array.isArray(value.files) ? value.files : [];
    if (!snapshots.length || snapshots.length > 200)
      throw failure('LOCAL_WORKSPACE_REPLACE_INVALID', '批量替换需要 1–200 个搜索快照。');
    const uniqueSnapshots = new Map();
    for (const snapshot of snapshots) {
      const relative = normalizeRelativePath(snapshot?.path, {allowRoot: false});
      if (!/^sha256:[0-9a-f]{64}$/.test(String(snapshot?.revision || '')))
        throw failure('LOCAL_WORKSPACE_REPLACE_INVALID', '搜索快照缺少有效 revision。');
      const existing = uniqueSnapshots.get(relative);
      if (existing && existing.revision !== snapshot.revision)
        throw failure('LOCAL_WORKSPACE_REPLACE_INVALID', `“${relative}”包含冲突的搜索快照。`);
      uniqueSnapshots.set(relative, {...snapshot, path: relative});
    }
    const prepared = [];
    for (const snapshot of uniqueSnapshots.values()) {
      const opened = await this.read(ownerId, {grantId: grant.id, path: snapshot?.path});
      if (opened.revision !== snapshot?.revision)
        throw failure('LOCAL_WORKSPACE_FILE_CHANGED', `“${snapshot?.path || ''}”在搜索后发生变化，未执行批量替换。`);
      expression.lastIndex = 0;
      const content = opened.content.replace(expression, () => replacement);
      if (content !== opened.content) prepared.push({snapshot, opened, content});
    }
    const saved = [];
    for (const item of prepared) {
      try {
        const result = await this.save(ownerId, {grantId: grant.id, path: item.snapshot.path,
          content: item.content, expectedRevision: item.opened.revision,
          bom: item.opened.bom, lineEnding: item.opened.lineEnding});
        saved.push({path: item.snapshot.path, revision: result.revision});
      } catch (error) {
        const partial = failure(error?.code || 'LOCAL_WORKSPACE_REPLACE_FAILED',
          saved.length
            ? `已替换 ${saved.length} 个文件，处理“${item.snapshot.path}”时失败并停止；已保存的文件不会自动回退。原因：${String(error?.message || '未知错误')}`
            : String(error?.message || '批量替换失败。'), error);
        partial.details = {saved};
        throw partial;
      }
    }
    return {replacedFiles: saved.length, saved, atomic: false};
  }
}

module.exports = {
  LocalWorkspaceError,
  LocalWorkspaceRegistry,
  MAX_TEXT_BYTES,
  normalizeRelativePath,
};
