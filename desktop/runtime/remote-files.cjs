'use strict';

const crypto = require('node:crypto');
const path = require('node:path');

const MAX_REMOTE_TEXT_BYTES = 1024 * 1024;
const MAX_REMOTE_DIRECTORY_ENTRIES = 5000;
const REMOTE_TREE_PREFETCH_DEFAULTS = Object.freeze({
  maxDepth: 5,
  maxDirectories: 192,
  maxEntries: 5000,
  concurrency: 4,
  timeBudgetMs: 15000,
});
const REMOTE_TREE_PREFETCH_HARD_LIMITS = Object.freeze({
  maxDepth: 12,
  maxDirectories: 512,
  maxEntries: 15000,
  concurrency: 8,
  timeBudgetMs: 30000,
});
const REMOTE_TREE_PREFETCH_SKIPPED_NAMES = new Set([
  '.git', '.cache', '.cargo', '.heterogeneous-workbench', '.local', '.mypy_cache',
  '.next', '.npm', '.pytest_cache', '.rustup', '.venv', '.vscode-server',
  '__pycache__', 'build', 'dist', 'node_modules', 'out', 'target', 'venv',
]);
const REMOTE_PATH_LIMIT = 4096;
const SFTP_OPERATION_TIMEOUT_MS = 15000;
const REMOTE_UPLOAD_CHUNK_BYTES = 128 * 1024;
const REMOTE_UPLOAD_CONCURRENCY = 8;
const SFTP_READ_CHUNK_BYTES = 64 * 1024;
const SFTP_STATUS_EOF = 1;
const S_IFMT = 0o170000;
const S_IFREG = 0o100000;
const S_IFDIR = 0o040000;
const S_IFLNK = 0o120000;

class RemoteFileError extends Error {
  constructor(code, message, cause) {
    super(message, cause ? {cause} : undefined);
    this.name = 'RemoteFileError';
    this.code = code;
  }
}

function failure(code, message, cause) { return new RemoteFileError(code, message, cause); }

function normalizeRelativePath(value, {allowRoot = true} = {}) {
  if (value === undefined || value === null) value = '';
  if (typeof value !== 'string' || value.length > REMOTE_PATH_LIMIT || value.includes('\0'))
    throw failure('REMOTE_PATH_INVALID', '远程路径无效。');
  if (value.includes('\\') || path.posix.isAbsolute(value))
    throw failure('REMOTE_PATH_OUTSIDE_ROOT', '远程路径必须位于当前 SSH 工作目录内。');
  const segments = value.split('/');
  if (segments.some(segment => segment === '..'))
    throw failure('REMOTE_PATH_OUTSIDE_ROOT', '远程路径超出当前 SSH 工作目录，已拒绝访问。');
  const normalized = segments.filter(segment => segment && segment !== '.').join('/');
  if (!allowRoot && !normalized) throw failure('REMOTE_PATH_INVALID', '请选择一个远程文件。');
  return normalized;
}

function canonicalPath(value) {
  const normalized = path.posix.normalize(String(value || ''));
  if (!path.posix.isAbsolute(normalized)) throw failure('REMOTE_PATH_INVALID', 'SSH 工作目录未解析为绝对路径。');
  return normalized;
}

function isInsideRoot(root, candidate) {
  root = canonicalPath(root); candidate = canonicalPath(candidate);
  return root === '/' ? candidate.startsWith('/') : candidate === root || candidate.startsWith(`${root}/`);
}

function attributesKind(attributes) {
  if (attributes && typeof attributes.isSymbolicLink === 'function' && attributes.isSymbolicLink()) return 'symlink';
  if (attributes && typeof attributes.isDirectory === 'function' && attributes.isDirectory()) return 'directory';
  if (attributes && typeof attributes.isFile === 'function' && attributes.isFile()) return 'file';
  const mode = Number(attributes?.mode || 0) & S_IFMT;
  if (mode === S_IFLNK) return 'symlink';
  if (mode === S_IFDIR) return 'directory';
  if (mode === S_IFREG) return 'file';
  return 'other';
}

function callSftp(sftp, method, timeoutMs, ...args) {
  return new Promise((resolve, reject) => {
    if (!sftp || typeof sftp[method] !== 'function') return reject(failure('REMOTE_SFTP_UNAVAILABLE', `SSH 服务器不支持 ${method} 文件操作。`));
    let settled = false;
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (error) reject(error); else resolve(value);
    };
    const timer = setTimeout(() => finish(failure('REMOTE_SFTP_TIMEOUT', `SSH 文件操作超时：${method}。`)), timeoutMs);
    try { sftp[method](...args, finish); }
    catch (error) { finish(error); }
  });
}

async function realpath(sftp, value, timeoutMs) {
  try { return canonicalPath(await callSftp(sftp, 'realpath', timeoutMs, value)); }
  catch (error) {
    if (error instanceof RemoteFileError) throw error;
    throw failure('REMOTE_PATH_NOT_FOUND', `远程路径不存在或无法访问：${value}`, error);
  }
}

async function optionalLstat(sftp, value, timeoutMs) {
  try { return await callSftp(sftp, 'lstat', timeoutMs, value); }
  catch (error) {
    if (error instanceof RemoteFileError) throw error;
    if (error?.code === 'ENOENT' || error?.code === 2) return null;
    throw failure('REMOTE_STAT_FAILED', `远程路径属性读取失败：${value}`, error);
  }
}

async function stat(sftp, value, timeoutMs) {
  try { return await callSftp(sftp, 'stat', timeoutMs, value); }
  catch (error) {
    if (error instanceof RemoteFileError) throw error;
    throw failure('REMOTE_PATH_NOT_FOUND', `远程路径不存在或无法访问：${value}`, error);
  }
}

function joinRoot(root, relative) { return relative ? path.posix.join(root, relative) : root; }

async function resolveExistingPath(sftp, root, relative, timeoutMs) {
  const requested = joinRoot(root, relative);
  const canonical = await realpath(sftp, requested, timeoutMs);
  if (!isInsideRoot(root, canonical))
    throw failure('REMOTE_SYMLINK_OUTSIDE_ROOT', '远程符号链接指向 SSH 工作目录之外，已拒绝访问。');
  return canonical;
}

function modifiedMetadata(attributes) {
  const mtimeMs = Number.isFinite(Number(attributes?.mtime)) ? Number(attributes.mtime) * 1000 : null;
  return {size: Number.isFinite(Number(attributes?.size)) ? Number(attributes.size) : null,
    mtimeMs, modifiedAt: mtimeMs === null ? null : new Date(mtimeMs).toISOString()};
}

function decodeTextDocument(bytes) {
  if (bytes.includes(0)) throw failure('REMOTE_BINARY_FILE', '远程文件包含二进制数据，无法在文本编辑器中打开。');
  const bom = bytes.length >= 3 && bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf;
  const body = bom ? bytes.subarray(3) : bytes;
  let text;
  try { text = new TextDecoder('utf-8', {fatal: true}).decode(body); }
  catch (error) { throw failure('REMOTE_BINARY_FILE', '远程文件不是有效的 UTF-8 文本，无法在编辑器中打开。', error); }
  let controls = 0;
  for (const byte of body) if (byte < 0x20 && byte !== 0x09 && byte !== 0x0a && byte !== 0x0c && byte !== 0x0d) controls++;
  if (controls > Math.max(2, Math.floor(body.length * 0.01)))
    throw failure('REMOTE_BINARY_FILE', '远程文件包含大量控制字符，无法在文本编辑器中打开。');
  const endings = new Set();
  for (const match of text.matchAll(/\r\n|\r|\n/g)) endings.add(match[0] === '\r\n' ? 'crlf' : match[0] === '\r' ? 'cr' : 'lf');
  const lineEnding = endings.size > 1 ? 'mixed' : endings.values().next().value || 'lf';
  return {content: text.replace(/\r\n?|\n/g, '\n'), bom, lineEnding};
}

function decodeText(bytes) { return decodeTextDocument(bytes).content; }

function encodeTextDocument(content, {bom = false, lineEnding = 'lf'} = {}) {
  if (typeof content !== 'string' || content.includes('\0'))
    throw failure('REMOTE_TEXT_INVALID', '只能保存不含 NUL 字节的 UTF-8 文本。');
  if (typeof bom !== 'boolean') throw failure('REMOTE_TEXT_INVALID', 'UTF-8 BOM 元数据无效。');
  if (lineEnding === 'mixed')
    throw failure('REMOTE_LINE_ENDING_MIXED', '该文件使用混合换行符，请先选择统一的换行格式再保存。');
  if (!['lf', 'crlf', 'cr'].includes(lineEnding)) throw failure('REMOTE_TEXT_INVALID', '换行符元数据无效。');
  const normalized = content.replace(/\r\n?|\n/g, '\n');
  const separator = lineEnding === 'crlf' ? '\r\n' : lineEnding === 'cr' ? '\r' : '\n';
  const body = Buffer.from(separator === '\n' ? normalized : normalized.replace(/\n/g, separator), 'utf8');
  return bom ? Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), body]) : body;
}

async function readBytes(sftp, filename, knownSize, timeoutMs) {
  if (knownSize > MAX_REMOTE_TEXT_BYTES)
    throw failure('REMOTE_FILE_TOO_LARGE', '远程文本文件超过 1 MiB，无法在编辑器中打开。');
  const chunks = [];
  let total = 0;
  let handle = null;
  let operationError = null;
  try {
    handle = await callSftp(sftp, 'open', timeoutMs, filename, 'r');
    while (total <= MAX_REMOTE_TEXT_BYTES) {
      const length = Math.min(SFTP_READ_CHUNK_BYTES, MAX_REMOTE_TEXT_BYTES + 1 - total);
      const buffer = Buffer.allocUnsafe(length);
      let bytesRead;
      try { bytesRead = await callSftp(sftp, 'read', timeoutMs, handle, buffer, 0, length, total); }
      catch (error) {
        if (error?.code === SFTP_STATUS_EOF || error?.code === 'EOF') break;
        throw error;
      }
      bytesRead = Number(bytesRead);
      if (!Number.isInteger(bytesRead) || bytesRead < 0 || bytesRead > length)
        throw failure('REMOTE_SFTP_INVALID_RESPONSE', 'SSH 服务器返回了无效的文件读取长度。');
      if (bytesRead === 0) break;
      total += bytesRead;
      if (total > MAX_REMOTE_TEXT_BYTES)
        throw failure('REMOTE_FILE_TOO_LARGE', '远程文本文件超过 1 MiB，无法在编辑器中打开。');
      chunks.push(buffer.subarray(0, bytesRead));
    }
  } catch (error) { operationError = error; }
  finally {
    if (handle !== null) {
      try { await callSftp(sftp, 'close', timeoutMs, handle); }
      catch (error) { if (!operationError) operationError = error; }
    }
  }
  if (operationError instanceof RemoteFileError) throw operationError;
  if (operationError) throw failure('REMOTE_READ_FAILED', `远程文件读取失败：${operationError.message}`, operationError);
  return Buffer.concat(chunks, total);
}

async function removeTemporaryFile(sftp, filename, timeoutMs) {
  try { await callSftp(sftp, 'unlink', timeoutMs, filename); }
  catch (error) {
    if (error?.code !== 'ENOENT' && error?.code !== 2) return false;
  }
  return true;
}

async function atomicRename(sftp, source, target, timeoutMs) {
  const errors = [];
  if (typeof sftp.ext_openssh_rename === 'function') {
    try { await callSftp(sftp, 'ext_openssh_rename', timeoutMs, source, target); return; }
    catch (error) { errors.push(error); }
  }
  try { await callSftp(sftp, 'rename', timeoutMs, source, target); }
  catch (error) {
    errors.push(error);
    const cause = errors.find(value => value instanceof RemoteFileError) || errors[errors.length - 1];
    throw failure('REMOTE_ATOMIC_RENAME_FAILED', `远程服务器无法原子替换文件：${cause.message}`, cause);
  }
}

function revision(bytes) { return `sha256:${crypto.createHash('sha256').update(bytes).digest('hex')}`; }

function entrySort(a, b) {
  const order = {directory: 0, file: 1, symlink: 2, other: 3};
  return (order[a.type] ?? 4) - (order[b.type] ?? 4) || a.name.localeCompare(b.name, 'zh-CN', {numeric: true, sensitivity: 'base'});
}

function boundedPrefetchInteger(value, fallback, maximum) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? Math.min(parsed, maximum) : fallback;
}

function normalizePrefetchOptions(value = {}) {
  const paths = Array.isArray(value.paths) ? value.paths : [];
  const maxDirectories = boundedPrefetchInteger(value.maxDirectories,
    REMOTE_TREE_PREFETCH_DEFAULTS.maxDirectories, REMOTE_TREE_PREFETCH_HARD_LIMITS.maxDirectories);
  const normalizedPaths = [...new Set(paths.slice(0, maxDirectories).map(item => normalizeRelativePath(item)))]
    .filter(relative => !relative || !REMOTE_TREE_PREFETCH_SKIPPED_NAMES.has(path.posix.basename(relative)));
  return {
    paths: normalizedPaths,
    maxDepth: boundedPrefetchInteger(value.maxDepth,
      REMOTE_TREE_PREFETCH_DEFAULTS.maxDepth, REMOTE_TREE_PREFETCH_HARD_LIMITS.maxDepth),
    maxDirectories,
    maxEntries: boundedPrefetchInteger(value.maxEntries,
      REMOTE_TREE_PREFETCH_DEFAULTS.maxEntries, REMOTE_TREE_PREFETCH_HARD_LIMITS.maxEntries),
    concurrency: boundedPrefetchInteger(value.concurrency,
      REMOTE_TREE_PREFETCH_DEFAULTS.concurrency, REMOTE_TREE_PREFETCH_HARD_LIMITS.concurrency),
    timeBudgetMs: boundedPrefetchInteger(value.timeBudgetMs,
      REMOTE_TREE_PREFETCH_DEFAULTS.timeBudgetMs, REMOTE_TREE_PREFETCH_HARD_LIMITS.timeBudgetMs),
  };
}

class RemoteFileService {
  constructor(sftp, rootPath, {operationTimeoutMs = SFTP_OPERATION_TIMEOUT_MS} = {}) {
    this.sftp = sftp;
    this.rootPath = canonicalPath(rootPath);
    this.operationTimeoutMs = operationTimeoutMs;
  }

  static async create(sftp, rootCandidate, options = {}) {
    const operationTimeoutMs = options.operationTimeoutMs || SFTP_OPERATION_TIMEOUT_MS;
    const rootPath = await realpath(sftp, rootCandidate, operationTimeoutMs);
    const attributes = await stat(sftp, rootPath, operationTimeoutMs);
    if (attributesKind(attributes) !== 'directory') throw failure('REMOTE_ROOT_INVALID', 'SSH 工作目录不是可浏览的目录。');
    return new RemoteFileService(sftp, rootPath, {operationTimeoutMs});
  }

  async list({path: requestedPath = ''} = {}) {
    const relative = normalizeRelativePath(requestedPath);
    const canonical = await resolveExistingPath(this.sftp, this.rootPath, relative, this.operationTimeoutMs);
    const attributes = await stat(this.sftp, canonical, this.operationTimeoutMs);
    if (attributesKind(attributes) !== 'directory') throw failure('REMOTE_NOT_DIRECTORY', '所选远程路径不是目录。');
    let values;
    try { values = await callSftp(this.sftp, 'readdir', this.operationTimeoutMs, canonical); }
    catch (error) {
      if (error instanceof RemoteFileError) throw error;
      throw failure('REMOTE_LIST_FAILED', `远程目录读取失败：${error.message}`, error);
    }
    if (!Array.isArray(values)) values = [];
    values = values.filter(value => value && value.filename !== '.' && value.filename !== '..');
    if (values.length > MAX_REMOTE_DIRECTORY_ENTRIES)
      throw failure('REMOTE_DIRECTORY_TOO_LARGE', `远程目录包含超过 ${MAX_REMOTE_DIRECTORY_ENTRIES} 个条目，请缩小浏览范围。`);
    const entries = [];
    for (const value of values) {
      const name = String(value.filename || '');
      if (!name || name.includes('/') || name.includes('\0')) continue;
      const entryRelative = relative ? `${relative}/${name}` : name;
      const entryPath = path.posix.join(canonical, name);
      const originalKind = attributesKind(value.attrs);
      let type = originalKind; let accessible = true;
      if (originalKind === 'symlink') {
        try {
          const target = await realpath(this.sftp, entryPath, this.operationTimeoutMs);
          if (!isInsideRoot(this.rootPath, target)) accessible = false;
          else type = attributesKind(await stat(this.sftp, target, this.operationTimeoutMs));
        } catch { accessible = false; }
      }
      entries.push({name, path: entryRelative, type, symlink: originalKind === 'symlink', accessible,
        ...modifiedMetadata(value.attrs)});
    }
    entries.sort(entrySort);
    return {path: relative, parentPath: relative.includes('/') ? relative.slice(0, relative.lastIndexOf('/')) : relative ? '' : null,
      entries, truncated: false};
  }

  async prefetchTree(value = {}, {shouldContinue = () => true} = {}) {
    const options = normalizePrefetchOptions(value);
    const startedAt = Date.now();
    const directories = [];
    const failures = [];
    const queue = [];
    const seen = new Set();
    let cursor = 0;
    let entryCount = 0;
    let skippedDirectories = 0;
    let stopReason = '';
    let cancelled = false;

    const enqueue = (relative, {seed = false} = {}) => {
      if (seen.has(relative)) return;
      if (seen.size >= options.maxDirectories) {
        stopReason ||= 'directory_limit';
        return;
      }
      seen.add(relative);
      queue.push({path: relative, depth: relative ? relative.split('/').length : 0, seed});
    };
    for (const relative of options.paths) enqueue(relative, {seed: true});

    const canSchedule = () => {
      if (!shouldContinue()) { cancelled = true; stopReason ||= 'cancelled'; return false; }
      if (Date.now() - startedAt >= options.timeBudgetMs) { stopReason ||= 'time_budget'; return false; }
      if (entryCount >= options.maxEntries) { stopReason ||= 'entry_limit'; return false; }
      return true;
    };
    const fetchDirectory = async item => {
      let result;
      try {
        if (item.seed && item.path) {
          const seedAttributes = await optionalLstat(this.sftp, joinRoot(this.rootPath, item.path), this.operationTimeoutMs);
          if (!seedAttributes || attributesKind(seedAttributes) !== 'directory') return {item, skipped: true};
        }
        result = await this.list({path: item.path});
        return {item, result};
      } catch (error) {
        return {item, error};
      }
    };
    while (cursor < queue.length && canSchedule()) {
      const batch = queue.slice(cursor, cursor + options.concurrency);
      cursor += batch.length;
      const fetched = await Promise.all(batch.map(fetchDirectory));
      if (!shouldContinue()) { cancelled = true; stopReason ||= 'cancelled'; break; }
      for (const {item, result, error, skipped} of fetched) {
        if (skipped) { skippedDirectories++; continue; }
        if (error) {
          failures.push({path: item.path, code: String(error?.code || 'REMOTE_LIST_FAILED')});
          continue;
        }
        const entries = Array.isArray(result.entries) ? result.entries : [];
        if (entryCount + entries.length > options.maxEntries) {
          stopReason ||= 'entry_limit';
          continue;
        }
        directories.push(result);
        entryCount += entries.length;
        if (item.depth >= options.maxDepth) continue;
        for (const entry of entries) {
          if (entry?.type !== 'directory' || entry.accessible === false || entry.symlink) continue;
          if (REMOTE_TREE_PREFETCH_SKIPPED_NAMES.has(entry.name)) { skippedDirectories++; continue; }
          enqueue(entry.path);
        }
      }
    }
    return {
      directories,
      truncated: Boolean(stopReason && stopReason !== 'cancelled'),
      cancelled,
      reason: stopReason || null,
      stats: {
        directoryCount: directories.length,
        entryCount,
        failedDirectories: failures.length,
        skippedDirectories,
        durationMs: Date.now() - startedAt,
      },
      failures: failures.slice(0, 32),
    };
  }

  async read({path: requestedPath} = {}) {
    const relative = normalizeRelativePath(requestedPath, {allowRoot: false});
    const canonical = await resolveExistingPath(this.sftp, this.rootPath, relative, this.operationTimeoutMs);
    const attributes = await stat(this.sftp, canonical, this.operationTimeoutMs);
    if (attributesKind(attributes) !== 'file') throw failure('REMOTE_NOT_FILE', '所选远程路径不是普通文件。');
    const metadata = modifiedMetadata(attributes);
    const bytes = await readBytes(this.sftp, canonical, metadata.size, this.operationTimeoutMs);
    const document = decodeTextDocument(bytes);
    return {path: relative, ...document, encoding: 'utf8', ...metadata, revision: revision(bytes)};
  }

  async save({path: requestedPath, content, expectedRevision, bom = false, lineEnding = 'lf',
    create = expectedRevision === undefined} = {}) {
    const relative = normalizeRelativePath(requestedPath, {allowRoot: false});
    const bytes = encodeTextDocument(content, {bom, lineEnding});
    if (bytes.length > MAX_REMOTE_TEXT_BYTES)
      throw failure('REMOTE_FILE_TOO_LARGE', '远程文本文件超过 1 MiB，无法保存。');
    const basename = path.posix.basename(relative);
    const parentRelative = path.posix.dirname(relative) === '.' ? '' : path.posix.dirname(relative);
    const parent = await resolveExistingPath(this.sftp, this.rootPath, parentRelative, this.operationTimeoutMs);
    if (attributesKind(await stat(this.sftp, parent, this.operationTimeoutMs)) !== 'directory') throw failure('REMOTE_NOT_DIRECTORY', '远程文件的父路径不是目录。');
    const target = path.posix.join(parent, basename);
    const existing = await optionalLstat(this.sftp, target, this.operationTimeoutMs);
    if (existing) {
      if (attributesKind(existing) === 'symlink') {
        const resolved = await realpath(this.sftp, target, this.operationTimeoutMs);
        if (!isInsideRoot(this.rootPath, resolved))
          throw failure('REMOTE_SYMLINK_OUTSIDE_ROOT', '远程符号链接指向 SSH 工作目录之外，已拒绝保存。');
        throw failure('REMOTE_SYMLINK_WRITE_BLOCKED', '为避免覆盖符号链接目标，请打开真实文件后再保存。');
      }
      if (attributesKind(existing) !== 'file') throw failure('REMOTE_NOT_FILE', '所选远程路径不是普通文件。');
    }
    if (create && existing)
      throw failure('REMOTE_FILE_CHANGED', '远程文件已经存在，已拒绝覆盖；请重新打开文件后保存。');
    if (!create && expectedRevision === undefined)
      throw failure('REMOTE_FILE_CHANGED', '覆盖远程文件必须提供读取时的 revision。');
    let baselineRevision = null;
    if (existing) {
      const currentMetadata = modifiedMetadata(existing);
      const currentBytes = await readBytes(this.sftp, target, currentMetadata.size, this.operationTimeoutMs);
      baselineRevision = revision(currentBytes);
    }
    if (expectedRevision !== undefined) {
      if (!existing) throw failure('REMOTE_FILE_CHANGED', '远程文件已被删除，请重新加载目录后再保存。');
      if (baselineRevision !== expectedRevision)
        throw failure('REMOTE_FILE_CHANGED', '远程文件已在服务器上发生变化，请重新加载后再保存。');
    }
    let temporary = path.posix.join(parent, `.hetero-${crypto.randomUUID()}.tmp`);
    try {
      await callSftp(this.sftp, 'writeFile', this.operationTimeoutMs, temporary, bytes,
        {encoding: null, mode: existing?.mode ? existing.mode & 0o777 : 0o644, flag: 'wx'});

      // Re-read the destination after the complete temporary write. This
      // avoids truncating the original and narrows the compare-and-swap window.
      const latest = await optionalLstat(this.sftp, target, this.operationTimeoutMs);
      if (latest && attributesKind(latest) === 'symlink') throw failure('REMOTE_FILE_CHANGED', '远程文件已变更为符号链接，请重新加载后再保存。');
      if (latest && attributesKind(latest) !== 'file') throw failure('REMOTE_FILE_CHANGED', '远程文件类型已发生变化，请重新加载后再保存。');
      if (Boolean(latest) !== Boolean(existing)) throw failure('REMOTE_FILE_CHANGED', '远程文件已在保存期间发生变化，请重新加载后再保存。');
      if (latest) {
        const latestMetadata = modifiedMetadata(latest);
        const latestBytes = await readBytes(this.sftp, target, latestMetadata.size, this.operationTimeoutMs);
        if (revision(latestBytes) !== baselineRevision)
          throw failure('REMOTE_FILE_CHANGED', '远程文件已在保存期间发生变化，请重新加载后再保存。');
      }
      await atomicRename(this.sftp, temporary, target, this.operationTimeoutMs);
      temporary = null;
    } catch (error) {
      if (error instanceof RemoteFileError) throw error;
      throw failure('REMOTE_WRITE_FAILED', `远程文件保存失败：${error.message}`, error);
    } finally {
      if (temporary) await removeTemporaryFile(this.sftp, temporary, this.operationTimeoutMs);
    }
    const attributes = await stat(this.sftp, target, this.operationTimeoutMs);
    return {path: relative, saved: true, encoding: 'utf8', bom, lineEnding, ...modifiedMetadata(attributes), revision: revision(bytes)};
  }

  async stageLocalUpload({localPath, directory: requestedDirectory = '', name, expectedSize} = {}) {
    const directory = normalizeRelativePath(requestedDirectory);
    if (typeof localPath !== 'string' || !localPath || localPath.includes('\0'))
      throw failure('REMOTE_UPLOAD_LOCAL_PATH_INVALID', '待上传的本地文件路径无效。');
    if (typeof name !== 'string' || !name || name === '.' || name === '..'
        || name.includes('/') || name.includes('\\') || name.includes('\0')
        || Buffer.byteLength(name, 'utf8') > 255)
      throw failure('REMOTE_UPLOAD_NAME_INVALID', '待上传文件名无效。');
    if (!Number.isSafeInteger(expectedSize) || expectedSize < 0)
      throw failure('REMOTE_UPLOAD_LOCAL_FILE_INVALID', '待上传文件大小无效。');
    const requestedParent = joinRoot(this.rootPath, directory);
    const requestedAttributes = await optionalLstat(this.sftp, requestedParent, this.operationTimeoutMs);
    if (!requestedAttributes)
      throw failure('REMOTE_PATH_NOT_FOUND', '上传目标目录不存在。');
    if (attributesKind(requestedAttributes) === 'symlink')
      throw failure('REMOTE_SYMLINK_WRITE_BLOCKED', '为避免通过符号链接写入其他位置，已拒绝上传。');
    if (attributesKind(requestedAttributes) !== 'directory')
      throw failure('REMOTE_NOT_DIRECTORY', '上传目标不是远程目录。');
    const parent = await resolveExistingPath(this.sftp, this.rootPath, directory, this.operationTimeoutMs);
    if (attributesKind(await stat(this.sftp, parent, this.operationTimeoutMs)) !== 'directory')
      throw failure('REMOTE_NOT_DIRECTORY', '上传目标不是远程目录。');
    const target = path.posix.join(parent, name);
    if (await optionalLstat(this.sftp, target, this.operationTimeoutMs))
      throw failure('REMOTE_FILE_EXISTS', `远程目录中已存在“${name}”，未覆盖原文件。`);
    const temporaryName = `.hetero-upload-${crypto.randomUUID()}.part`;
    const temporary = path.posix.join(parent, temporaryName);
    const temporaryPath = directory ? `${directory}/${temporaryName}` : temporaryName;
    let staged = false;
    try {
      await callSftp(this.sftp, 'fastPut', this.operationTimeoutMs, localPath, temporary, {
        concurrency: REMOTE_UPLOAD_CONCURRENCY,
        chunkSize: REMOTE_UPLOAD_CHUNK_BYTES,
        fileSize: expectedSize,
        mode: 0o600,
      });
      const attributes = await stat(this.sftp, temporary, this.operationTimeoutMs);
      if (attributesKind(attributes) !== 'file' || Number(attributes.size) !== expectedSize)
        throw failure('REMOTE_UPLOAD_INCOMPLETE', '远程临时文件大小与本地文件不一致，未发布上传结果。');
      staged = true;
      return {directory, name, path: directory ? `${directory}/${name}` : name, temporaryPath, size: expectedSize};
    } catch (error) {
      if (error instanceof RemoteFileError) throw error;
      throw failure('REMOTE_UPLOAD_FAILED', '远程文件上传失败，请检查本地文件权限、SSH 连接和远程目录权限。', error);
    } finally {
      if (!staged) await removeTemporaryFile(this.sftp, temporary, this.operationTimeoutMs);
    }
  }

  async removeStagedUpload(requestedPath) {
    const relative = normalizeRelativePath(requestedPath, {allowRoot: false});
    const filename = path.posix.basename(relative);
    if (!/^\.hetero-upload-[0-9a-f-]{36}\.part$/.test(filename))
      throw failure('REMOTE_UPLOAD_TEMPORARY_INVALID', '远程上传临时文件标识无效。');
    const parentRelative = path.posix.dirname(relative) === '.' ? '' : path.posix.dirname(relative);
    const parent = await resolveExistingPath(this.sftp, this.rootPath, parentRelative, this.operationTimeoutMs);
    return removeTemporaryFile(this.sftp, path.posix.join(parent, filename), this.operationTimeoutMs);
  }
}

module.exports = {
  MAX_REMOTE_TEXT_BYTES,
  REMOTE_TREE_PREFETCH_DEFAULTS,
  REMOTE_TREE_PREFETCH_HARD_LIMITS,
  SFTP_OPERATION_TIMEOUT_MS,
  RemoteFileError,
  RemoteFileService,
  normalizeRelativePath,
  isInsideRoot,
  decodeText,
  decodeTextDocument,
  encodeTextDocument,
};
