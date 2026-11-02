'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const DEFAULT_MAX_TEXT_BYTES = 1024 * 1024;
const DEFAULT_WORKERS = 8;

function shellQuote(value) {
  value = String(value);
  return `'${value.replaceAll("'", `'"'"'`)}'`;
}

function shellExecutable(value) {
  value = String(value);
  return value.startsWith('~/') ? `"$HOME"/${shellQuote(value.slice(2))}` : shellQuote(value);
}

function validateAbsolutePosixPath(value, label) {
  if (typeof value !== 'string' || !value.startsWith('/') || value.includes('\0') || value.includes('\\'))
    throw new TypeError(`${label} must be an absolute POSIX path.`);
  return value;
}

function workspaceWorkerSource(sourcePath = path.join(__dirname, 'workspace-file-worker.py')) {
  const source = fs.readFileSync(sourcePath);
  if (!source.length || source.includes(0)) throw new Error('Workspace worker source is empty or invalid.');
  return source;
}

/**
 * Build a portless, install-free worker launch.
 *
 * The SSH exec command first reads exactly source.length bytes from stdin and
 * executes them. The same stdin/stdout then remains attached to framed RPC, so
 * the server needs no listening port and keeps no deployed worker artifact.
 */
function createWorkspaceWorkerLaunch({
  workspaceRoot,
  pythonExecutable = 'python3',
  maxTextBytes = DEFAULT_MAX_TEXT_BYTES,
  workers = DEFAULT_WORKERS,
  source = workspaceWorkerSource(),
} = {}) {
  validateAbsolutePosixPath(workspaceRoot, 'workspaceRoot');
  if (typeof pythonExecutable !== 'string' || !pythonExecutable.trim() || pythonExecutable.includes('\0'))
    throw new TypeError('pythonExecutable is required.');
  source = Buffer.isBuffer(source) ? source : Buffer.from(String(source), 'utf8');
  if (!source.length || source.length > 2 * 1024 * 1024)
    throw new RangeError('Workspace worker source must be between 1 byte and 2 MiB.');
  maxTextBytes = Number(maxTextBytes);
  workers = Number(workers);
  if (!Number.isInteger(maxTextBytes) || maxTextBytes < 1024 || maxTextBytes > 4 * 1024 * 1024)
    throw new RangeError('maxTextBytes must be between 1024 and 4194304.');
  if (!Number.isInteger(workers) || workers < 1 || workers > 16)
    throw new RangeError('workers must be between 1 and 16.');
  const bootstrap = [
    'import sys',
    'n=int(sys.argv[1])',
    'source=sys.stdin.buffer.read(n)',
    'sys.argv=["workspace-file-worker.py"]+sys.argv[2:]',
    'scope={"__name__":"__main__","__file__":"<hetero-workspace-worker>"}',
    'exec(compile(source,"<hetero-workspace-worker>","exec"),scope,scope)',
  ].join(';');
  const command = [
    'exec env PYTHONDONTWRITEBYTECODE=1', shellExecutable(pythonExecutable), '-u -c', shellQuote(bootstrap),
    String(source.length), '--stdio-server', '--workspace-root', shellQuote(workspaceRoot),
    '--max-text-bytes', String(maxTextBytes), '--workers', String(workers),
  ].join(' ');
  return {
    command,
    bootstrapPayload: source,
    sourceBytes: source.length,
    sourceSha256: crypto.createHash('sha256').update(source).digest('hex'),
    workspaceRoot,
    maxTextBytes,
    workers,
  };
}

module.exports = {
  DEFAULT_MAX_TEXT_BYTES,
  DEFAULT_WORKERS,
  createWorkspaceWorkerLaunch,
  shellQuote,
  workspaceWorkerSource,
};
