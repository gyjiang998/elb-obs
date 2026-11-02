'use strict';

const {EventEmitter} = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const http = require('node:http');
const zlib = require('node:zlib');
const {execFile} = require('node:child_process');
const {Client} = require('ssh2');
const {CaptureManager} = require('./capture.cjs');
const {CalibrationManager, validateDeviceHardwareProfile} = require('./calibration.cjs');
const {captureEnvironment} = require('./capture-environment.cjs');
const {extractHardwareProfile: extractLocalHardwareProfile} = require('./hardware-profile.cjs');
const {TOOLS} = require('./agent-tools.cjs');
const {RemoteFileError, RemoteFileService, normalizeRelativePath} = require('./remote-files.cjs');
const {PlatformRpcChannel} = require('./platform-rpc-channel.cjs');
const {WorkspaceRpcChannel} = require('./workspace-rpc-channel.cjs');
const {createWorkspaceWorkerLaunch} = require('./workspace-worker.cjs');
const {DesktopAgentRunStore} = require('./agent-run-store.cjs');
const {CodeAgentSessionStore} = require('./code-agent-sessions.cjs');
const {prepareManagedAgentSkills} = require('./agent-skills.cjs');
const {normalizeAutomationSettings} = require('./automation-settings.cjs');
const {
  buildAnalysisWorkerActivateScript,
  buildAnalysisWorkerInspectScript,
  buildAnalysisWorkerPrepareScript,
  buildAnalysisWorkerRootScript,
  loadAnalysisWorkerBundle,
  parseAnalysisWorkerReady,
  parseAnalysisWorkerRoot,
  parseAnalysisWorkerStatus,
} = require('./analysis-worker.cjs');
const {
  buildCodeAnalysisRuntimePrepareScript,
  parseCodeAnalysisRuntime,
} = require('./code-analysis-runtime.cjs');
const {
  buildServerProfileInspectScript,
  buildServerProfilePrepareScript,
  normalizeServerProfile,
  parseServerProfileInspect,
  parseServerProfilePrepare,
  symbolicManagedPaths,
} = require('./server-profile.cjs');
const {
  normalizeSshTarget,
  hasSshTarget,
  sameSshTarget,
  serverProfileTargetKey,
  parseServerProfileTargetKey,
} = require('./server-profile-target.cjs');

const MAX_OUTPUT = 1024 * 1024;
const MAX_SSH_TERMINALS = 8;
const MAX_AGENT_REMOTE_TIMEOUT_MS = 120000;
const MAX_PLATFORM_REMOTE_TIMEOUT_MS = 30 * 60 * 1000;
const WORKSPACE_WORKER_FAILURE_COOLDOWN_MS = 30000;
const MAX_BROKER_REQUEST_BYTES = 4 * 1024 * 1024;
const CAPTURE_UPLOAD_TOKEN = /^[a-f0-9]{32}$/;
const SHA256 = /^[a-f0-9]{64}$/;
const MAX_CAPTURE_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024;
const MAX_CALIBRATION_MEASUREMENT_BYTES = 20 * 1024 * 1024;
const REMOTE_DOWNLOAD_CHUNK_BYTES = 512 * 1024;
const MAX_REMOTE_DOWNLOAD_BYTES = 32 * 1024 * 1024 * 1024;
const MAX_REMOTE_UPLOAD_FILES = 64;
const MAX_REMOTE_UPLOAD_BYTES = 32 * 1024 * 1024 * 1024;
const MAX_REMOTE_UPLOAD_TOTAL_BYTES = 64 * 1024 * 1024 * 1024;
const REMOTE_UPLOAD_TIMEOUT_MS = 2 * 60 * 60 * 1000;
const AGENT_SUBMIT_DELAY_MIN_MS = 100;
const AGENT_SUBMIT_DELAY_DEFAULT_MS = 1000;
const AGENT_PASTE_OUTPUT_QUIET_MS = 80;
const AGENT_PASTE_MAX_EXTRA_MS = 2000;
const SUBMIT_MODES = new Set(['bracketed_paste_cr', 'bracketed_paste_lf', 'plain_text_cr', 'plain_text_lf']);
const DEFAULTS = {
  ssh: {host: '', port: 22, username: '', cwd: '~', auth: 'password', privateKeyPath: ''},
  agent: {executable: '', args: [], cwd: '', environment: {}, submitMode: 'bracketed_paste_cr', submitDelayMs: AGENT_SUBMIT_DELAY_DEFAULT_MS, startupDelayMs: 800, demo: true},
  serverProfile: normalizeServerProfile(),
  serverProfileTarget: null,
  serverProfiles: {},
  hdcPath: 'hdc',
  automation: normalizeAutomationSettings(),
};
const PLATFORM_RPC_MARKER = 'HETERO_DESKTOP_RPC\t';
const PLATFORM_RPC_GZIP_MARKER = 'HETERO_DESKTOP_RPC_GZIP\t';
const APPLICATION_FLOW_TOOLS = Object.freeze({
  application_flow_list_assets: 'application_flow.list_assets',
  application_flow_inspect_repository: 'application_flow.inspect_repository',
  application_flow_search_source: 'application_flow.search_source',
  application_flow_read_source: 'application_flow.read_source',
  application_flow_inspect_trace_instrumentation: 'application_flow.inspect_trace_instrumentation',
  application_flow_read_asset: 'application_flow.read_asset',
  application_flow_publish_candidates: 'application_flow.publish_candidates',
  application_flow_compile_mermaid: 'application_flow.compile_mermaid',
  application_flow_enrich_trace_anchors: 'application_flow.enrich_trace_anchors',
  application_flow_inspect_runtime_trace: 'application_flow.inspect_runtime_trace',
  application_flow_apply_runtime_trace_alignment: 'application_flow.apply_runtime_trace_alignment',
});
const RECOVERY_SCOPES = new Set(['device', 'server']);
const RECOVERY_SERVER_MUTATIONS = new Set([
  'remote_exec', 'workspace_write',
  'application_flow_publish_candidates', 'application_flow_compile_mermaid',
  'application_flow_enrich_trace_anchors', 'application_flow_apply_runtime_trace_alignment',
]);
const RECOVERY_DEVICE_MUTATIONS = new Set([
  'capture_update_plan', 'capture_cancel',
]);
const shellQuote = value => `'${String(value).replace(/'/g, `'"'"'`)}'`;
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const clone = value => JSON.parse(JSON.stringify(value));
function agentPasteTiming(text, configuredDelayMs, overrides = {}) {
  const bytes = Buffer.byteLength(String(text || ''), 'utf8');
  // Large pasted prompts take longer to become editor state in full-screen
  // TUIs. Scale conservatively, but keep the wait bounded for the 64 KiB
  // submit contract.
  // A short prompt can submit at the configured 100 ms floor. Prompts larger
  // than 1 KiB receive a bounded size allowance so a full-screen editor has
  // time to consume the paste frame before Enter is emitted.
  const sizeExtraMs = Math.min(
    AGENT_PASTE_MAX_EXTRA_MS,
    Math.ceil(Math.max(0, bytes - 1024) / 4096) * 100,
  );
  const minimumMs = Math.max(
    AGENT_SUBMIT_DELAY_MIN_MS,
    Number(configuredDelayMs) || 0,
    AGENT_SUBMIT_DELAY_MIN_MS + sizeExtraMs,
  );
  const noOutputWaitMs = minimumMs;
  const maximumWaitMs = minimumMs + 1500;
  return {
    minimumMs: overrides.minimumMs ?? minimumMs,
    noOutputWaitMs: overrides.noOutputWaitMs ?? noOutputWaitMs,
    maximumWaitMs: overrides.maximumWaitMs ?? maximumWaitMs,
    quietMs: overrides.quietMs ?? AGENT_PASTE_OUTPUT_QUIET_MS,
    bytes,
  };
}
async function waitForAgentPasteSettle({
  text, configuredDelayMs, outputSequenceBefore, readOutputSequence, readLastOutputAt,
  sessionCurrent = () => true, now = Date.now, sleep = delay, timingOverrides,
} = {}) {
  const timing = agentPasteTiming(text, configuredDelayMs, timingOverrides);
  const startedAt = now();
  while (true) {
    if (!sessionCurrent()) throw new Error('Agent 会话已关闭。');
    const currentAt = now();
    const elapsedMs = Math.max(0, currentAt - startedAt);
    const outputObserved = Number(readOutputSequence?.() || 0) > Number(outputSequenceBefore || 0);
    const quietForMs = Math.max(0, currentAt - Number(readLastOutputAt?.() || 0));
    let waitReason = '';
    if (elapsedMs >= timing.maximumWaitMs) waitReason = 'maximum_wait_reached';
    else if (outputObserved && elapsedMs >= timing.minimumMs && quietForMs >= timing.quietMs) waitReason = 'output_settled';
    else if (!outputObserved && elapsedMs >= timing.noOutputWaitMs) waitReason = 'no_output_wait_elapsed';
    if (waitReason) return {...timing, actualWaitMs: elapsedMs, outputObserved, waitReason};
    const nextBoundary = outputObserved
      ? Math.max(startedAt + timing.minimumMs, Number(readLastOutputAt?.() || 0) + timing.quietMs)
      : startedAt + timing.noOutputWaitMs;
    await sleep(Math.max(1, Math.min(50, startedAt + timing.maximumWaitMs - currentAt, nextBoundary - currentAt)));
  }
}
const AGENT_TASK_CONTEXT_FIELDS = new Set([
  'task_kind', 'required_skill_id', 'application_flow_analysis_id', 'source_connector_id',
  'source_snapshot_id', 'subpath', 'graph_id', 'discovery_artifact_id', 'flow_id',
  'scenario_artifact_id', 'trace_analysis_artifact_id', 'analysis_artifact_id',
  'evidence_artifact_id', 'result_artifact_id', 'analysis_kind', 'source_scopes',
  'selected_device', 'scenario_capture_target', 'repair_request_id',
  'capture_plan_id', 'capture_plan_request_id', 'capture_plan_expected_updated_at',
  'page', 'page_label', 'operation', 'error_code', 'recovery_operation_key',
  'recovery_authorization_scopes', 'operation_attempt_id',
]);
function normalizeAgentTaskContext(value) {
  if (!value || Array.isArray(value) || typeof value !== 'object') return null;
  const context = Object.fromEntries(Object.entries(value)
    .filter(([key, item]) => AGENT_TASK_CONTEXT_FIELDS.has(key) && item !== undefined && item !== null && item !== ''));
  const serialized = JSON.stringify(context);
  if (Buffer.byteLength(serialized) > 256 * 1024) throw new Error('Agent 任务上下文超过 256 KiB。');
  return Object.keys(context).length ? JSON.parse(serialized) : null;
}
function boundedInt(value, fallback, low, high) { const n = Number(value); return Number.isInteger(n) ? Math.max(low, Math.min(high, n)) : fallback; }
function workspaceReplacementExpression({query, regex = false, caseSensitive = false} = {}) {
  if (typeof query !== 'string' || !query || Buffer.byteLength(query, 'utf8') > 4096)
    throw new Error('搜索内容必须是非空短文本。');
  if (regex) throw new Error('批量替换首版仅支持文本搜索。');
  const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp(escaped, `${caseSensitive ? '' : 'i'}g`);
}
function remoteExecutionTimeout(value, agentRequest = false) {
  return boundedInt(value, 30000, 100, agentRequest ? MAX_AGENT_REMOTE_TIMEOUT_MS : MAX_PLATFORM_REMOTE_TIMEOUT_MS);
}
function decodePlatformRpcResult(result) {
  if (Number(result?.exitCode) !== 0)
    throw new Error(String(result?.stderr || '').trim() || `服务器分析请求失败（exit ${result?.exitCode}）。`);
  const line = String(result?.stdout || '').split(/\r?\n/).reverse()
    .find(value => value.startsWith(PLATFORM_RPC_MARKER) || value.startsWith(PLATFORM_RPC_GZIP_MARKER));
  if (!line) throw new Error('服务器分析长驻通道没有返回结构化结果。');
  const compressed = line.startsWith(PLATFORM_RPC_GZIP_MARKER);
  const marker = compressed ? PLATFORM_RPC_GZIP_MARKER : PLATFORM_RPC_MARKER;
  let response;
  try {
    const wire = Buffer.from(line.slice(marker.length), 'base64');
    response = JSON.parse((compressed ? zlib.gunzipSync(wire) : wire).toString('utf8'));
  } catch { throw new Error('服务器分析长驻通道返回了无效结果。'); }
  if (response?.schema_version !== 'desktop_platform_rpc/v2' || typeof response?.ok !== 'boolean')
    throw new Error('服务器分析协议版本不兼容。');
  if (!response.ok) {
    const error = new Error(String(response.error?.message || '服务器分析操作失败。'));
    error.code = String(response.error?.code || 'DESKTOP_RPC_FAILED');
    error.details = response.error?.details || {};
    throw error;
  }
  return response.result?.kind === 'json' ? response.result.value : response.result;
}
function remoteFileConnectionFailed(error) {
  const fatalCodes = new Set([
    'REMOTE_SFTP_TIMEOUT', 'REMOTE_SFTP_UNAVAILABLE', 'REMOTE_SFTP_INVALID_RESPONSE',
    'ECONNABORTED', 'ECONNRESET', 'EPIPE', 'ENOTCONN', 'ETIMEDOUT',
    'ERR_STREAM_DESTROYED', 'NO_CONNECTION', 'CONNECTION_LOST',
    // SSH_FX_NO_CONNECTION and SSH_FX_CONNECTION_LOST.
    6, 7, '6', '7',
  ]);
  const seen = new Set();
  for (let current = error; current && !seen.has(current); current = current.cause) {
    seen.add(current);
    if (fatalCodes.has(current.code)) return true;
  }
  return false;
}

function workspaceTransportFailed(error) {
  const fatalCodes = new Set([
    'WORKSPACE_RPC_READY_TIMEOUT', 'WORKSPACE_RPC_REQUEST_TIMEOUT', 'WORKSPACE_RPC_CHANNEL_CLOSED',
    'WORKSPACE_RPC_UNAVAILABLE', 'WORKSPACE_RPC_INVALID_RESPONSE', 'WORKSPACE_RPC_INVALID_FRAME',
    'WORKSPACE_RPC_FRAME_TOO_LARGE',
    'ECONNABORTED', 'ECONNRESET', 'EPIPE', 'ENOTCONN', 'ETIMEDOUT',
    'ERR_STREAM_DESTROYED', 'NO_CONNECTION', 'CONNECTION_LOST',
  ]);
  const seen = new Set();
  for (let current = error; current && !seen.has(current); current = current.cause) {
    seen.add(current);
    if (fatalCodes.has(current.code)) return true;
  }
  return false;
}

function remoteFileErrorFromWorkspace(error) {
  const codes = {
    WORKSPACE_PATH_INVALID: 'REMOTE_PATH_INVALID',
    WORKSPACE_PATH_OUTSIDE_ROOT: 'REMOTE_PATH_OUTSIDE_ROOT',
    WORKSPACE_SYMLINK_OUTSIDE_ROOT: 'REMOTE_SYMLINK_OUTSIDE_ROOT',
    WORKSPACE_PATH_NOT_FOUND: 'REMOTE_PATH_NOT_FOUND',
    WORKSPACE_READ_FAILED: 'REMOTE_READ_FAILED',
    WORKSPACE_NOT_FILE: 'REMOTE_NOT_FILE',
    WORKSPACE_NOT_DIRECTORY: 'REMOTE_NOT_DIRECTORY',
    WORKSPACE_FILE_TOO_LARGE: 'REMOTE_FILE_TOO_LARGE',
    WORKSPACE_DOWNLOAD_TOO_LARGE: 'REMOTE_DOWNLOAD_TOO_LARGE',
    WORKSPACE_BINARY_FILE: 'REMOTE_BINARY_FILE',
    WORKSPACE_TEXT_INVALID: 'REMOTE_TEXT_INVALID',
    WORKSPACE_TEXT_METADATA_REQUIRED: 'REMOTE_TEXT_INVALID',
    WORKSPACE_LINE_ENDING_MIXED: 'REMOTE_LINE_ENDING_MIXED',
    WORKSPACE_LIST_FAILED: 'REMOTE_LIST_FAILED',
    WORKSPACE_SYMLINK_WRITE_BLOCKED: 'REMOTE_SYMLINK_WRITE_BLOCKED',
    WORKSPACE_FILE_CHANGED: 'REMOTE_FILE_CHANGED',
    WORKSPACE_REVISION_REQUIRED: 'REMOTE_FILE_CHANGED',
    WORKSPACE_FILE_EXISTS: 'REMOTE_FILE_CHANGED',
    WORKSPACE_CREATE_REQUIRED: 'REMOTE_FILE_CHANGED',
    WORKSPACE_ATOMIC_RENAME_FAILED: 'REMOTE_ATOMIC_RENAME_FAILED',
    WORKSPACE_RENAME_FAILED: 'REMOTE_RENAME_FAILED',
    WORKSPACE_WRITE_FAILED: 'REMOTE_WRITE_FAILED',
    WORKSPACE_CONNECTION_CHANGED: 'REMOTE_CONNECTION_CHANGED',
  };
  const code = codes[error?.code] || error?.code || 'REMOTE_WORKSPACE_FAILED';
  return new RemoteFileError(code, error?.message || '远程工作区操作失败。', error);
}
function expandHome(value) { return value === '~' ? os.homedir() : value.startsWith('~/') ? path.join(os.homedir(), value.slice(2)) : value; }
function remotePrefix(cwd) { return !cwd || cwd === '~' ? 'cd "$HOME" && ' : `cd -- ${shellQuote(cwd)} && `; }
function sha256File(filename) {
  return new Promise((resolve, reject) => {
    const digest = crypto.createHash('sha256');
    const stream = fs.createReadStream(filename);
    stream.on('data', chunk => digest.update(chunk));
    stream.on('error', reject);
    stream.on('end', () => resolve(digest.digest('hex')));
  });
}

const LOCAL_REPLACE_COLLISION_CODES = new Set(['EEXIST', 'EPERM', 'EACCES', 'ENOTEMPTY']);
const LOCAL_REPLACE_TRANSIENT_CODES = new Set(['EPERM', 'EACCES', 'EBUSY']);

async function retryLocalFileOperation(operation, attempts = 5) {
  let failure;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try { return await operation(); }
    catch (error) {
      failure = error;
      if (!LOCAL_REPLACE_TRANSIENT_CODES.has(error?.code) || attempt + 1 >= attempts) throw error;
      // Antivirus and indexers can hold a just-written file briefly on
      // Windows. Keep retries bounded; the caller still owns rollback.
      await delay(25 * (attempt + 1));
    }
  }
  throw failure;
}

function localFingerprintValue(value) {
  if (typeof value === 'bigint') return value.toString();
  return Number.isFinite(value) ? String(value) : '';
}

/**
 * Capture the namespace identity the user approved in the Save dialog.  The
 * fingerprint deliberately describes the directory entry, not its path
 * string: a replacement at the same path is therefore observable.
 */
async function captureLocalDestinationFingerprint(destination, {
  fileSystem = fs.promises,
} = {}) {
  try {
    const value = await fileSystem.lstat(destination);
    return {
      exists: true,
      regularFile: Boolean(value.isFile?.()),
      symbolicLink: Boolean(value.isSymbolicLink?.()),
      device: localFingerprintValue(value.dev),
      inode: localFingerprintValue(value.ino),
      size: localFingerprintValue(value.size),
      mtimeMs: localFingerprintValue(value.mtimeMs),
      ctimeMs: localFingerprintValue(value.ctimeMs),
      mode: localFingerprintValue(value.mode),
    };
  } catch (error) {
    if (error?.code === 'ENOENT') return {exists: false};
    throw error;
  }
}

function sameLocalDestinationFingerprint(left, right) {
  if (!left || !right || Boolean(left.exists) !== Boolean(right.exists)) return false;
  if (!left.exists) return true;
  return ['regularFile', 'symbolicLink', 'device', 'inode', 'size', 'mtimeMs', 'ctimeMs', 'mode']
    .every(key => left[key] === right[key]);
}

async function captureLocalUploadSource(filename) {
  if (typeof filename !== 'string' || !filename || filename.includes('\0') || !path.isAbsolute(filename))
    throw new RemoteFileError('REMOTE_UPLOAD_LOCAL_PATH_INVALID', '待上传文件必须是有效的本地绝对路径。');
  let fingerprint;
  try { fingerprint = await captureLocalDestinationFingerprint(filename); }
  catch (error) {
    throw new RemoteFileError('REMOTE_UPLOAD_LOCAL_UNAVAILABLE', '无法读取待上传的本地文件。', error);
  }
  if (!fingerprint.exists || !fingerprint.regularFile || fingerprint.symbolicLink)
    throw new RemoteFileError('REMOTE_UPLOAD_LOCAL_NOT_FILE', '待上传项目必须是本地普通文件，不能是目录或符号链接。');
  const size = Number(fingerprint.size);
  if (!Number.isSafeInteger(size) || size < 0 || size > MAX_REMOTE_UPLOAD_BYTES)
    throw new RemoteFileError('REMOTE_UPLOAD_LOCAL_TOO_LARGE', '单个上传文件大小无效或超过 32 GiB 上限。');
  const name = path.basename(filename);
  if (!name || name === '.' || name === '..' || name.includes('/') || name.includes('\\')
      || name.includes('\0') || Buffer.byteLength(name, 'utf8') > 255)
    throw new RemoteFileError('REMOTE_UPLOAD_NAME_INVALID', '待上传文件名无效。');
  return {filename, name, size, fingerprint};
}

async function assertLocalUploadSource(source) {
  let current;
  try { current = await captureLocalDestinationFingerprint(source.filename); }
  catch (error) {
    throw new RemoteFileError('REMOTE_UPLOAD_LOCAL_CHANGED', `“${source.name}”在上传期间无法继续读取，未发布远程文件。`, error);
  }
  if (!sameLocalDestinationFingerprint(current, source.fingerprint)
      || !current.exists || !current.regularFile || current.symbolicLink) {
    throw new RemoteFileError('REMOTE_UPLOAD_LOCAL_CHANGED', `“${source.name}”在上传期间发生变化，未发布远程文件。`);
  }
  return current;
}

function publicRemoteUploadFailure(error, name) {
  const known = error instanceof RemoteFileError;
  return {
    name,
    code: String(known ? error.code : 'REMOTE_UPLOAD_FAILED'),
    message: known
      ? String(error.message || '上传失败。')
      : `“${name}”上传失败，请检查本地文件权限、SSH 连接和远程目录权限。`,
  };
}

function sameMovedLocalDestinationFingerprint(left, right) {
  if (!left || !right || !left.exists || !right.exists) return false;
  // Renaming a directory entry may update ctime on some filesystems.  The
  // underlying file identity, content size/mtime, kind and mode must remain.
  return ['regularFile', 'symbolicLink', 'device', 'inode', 'size', 'mtimeMs', 'mode']
    .every(key => left[key] === right[key]);
}

async function assertLocalDestinationFingerprint(destination, expected, options = {}) {
  const actual = await captureLocalDestinationFingerprint(destination, options);
  if (!sameLocalDestinationFingerprint(actual, expected)) {
    throw new RemoteFileError(
      'REMOTE_LOCAL_DESTINATION_CHANGED',
      '保存对话框确认后，本地目标文件已发生变化；为避免覆盖新内容，下载未提交。',
    );
  }
  if (actual.exists && (!actual.regularFile || actual.symbolicLink)) {
    throw new RemoteFileError(
      'REMOTE_LOCAL_PATH_INVALID',
      '本地下载目标必须是普通文件，不能覆盖目录或符号链接。',
    );
  }
  return actual;
}

/**
 * Commit a complete sibling download without risking an existing destination.
 * POSIX rename replaces atomically. Windows commonly rejects that operation,
 * so preserve the old file under a private sibling name, commit the new file,
 * and roll the old file back if the second rename fails.
 */
async function replaceDownloadedFile(temporary, destination, {
  fileSystem = fs.promises,
  randomUUID = crypto.randomUUID,
  expectedDestinationFingerprint = null,
} = {}) {
  if (expectedDestinationFingerprint) {
    const approved = await assertLocalDestinationFingerprint(destination, expectedDestinationFingerprint, {fileSystem});
    // A target that did not exist when the user confirmed must stay absent.
    // link(2) is an atomic no-clobber publish because the temporary file is a
    // sibling on the same filesystem.  A file created during the download is
    // therefore never overwritten.
    if (!approved.exists) {
      try {
        await retryLocalFileOperation(() => fileSystem.link(temporary, destination));
      } catch (error) {
        if (error?.code === 'EEXIST') {
          throw new RemoteFileError(
            'REMOTE_LOCAL_DESTINATION_CHANGED',
            '下载期间本地目标文件已被创建；为避免覆盖新内容，下载未提交。',
            error,
          );
        }
        throw error;
      }
      try { await retryLocalFileOperation(() => fileSystem.unlink(temporary)); }
      catch (cleanupError) { console.error('Remote download temporary cleanup:', cleanupError.message); }
      return {replacedExisting: false};
    }

    const parent = path.dirname(destination);
    const filename = path.basename(destination);
    const backup = path.join(parent, `.${filename}.hetero-download-backup-${randomUUID()}`);
    await retryLocalFileOperation(() => fileSystem.rename(destination, backup));
    let backupContainsOriginal = true;
    let published = false;
    try {
      // Recheck the moved directory entry.  This closes the interval between
      // the final pre-commit check and reserving the destination pathname.
      const moved = await captureLocalDestinationFingerprint(backup, {fileSystem});
      if (!sameMovedLocalDestinationFingerprint(moved, approved)) {
        throw new RemoteFileError(
          'REMOTE_LOCAL_DESTINATION_CHANGED',
          '本地目标文件在下载提交时发生变化；已保留原内容并拒绝覆盖。',
        );
      }
      try {
        await retryLocalFileOperation(() => fileSystem.link(temporary, destination));
        published = true;
      } catch (error) {
        if (error?.code === 'EEXIST') {
          throw new RemoteFileError(
            'REMOTE_LOCAL_DESTINATION_CHANGED',
            '下载提交期间本地目标路径被占用；为避免覆盖新内容，下载未提交。',
            error,
          );
        }
        throw error;
      }
      try { await retryLocalFileOperation(() => fileSystem.unlink(temporary)); }
      catch (cleanupError) { console.error('Remote download temporary cleanup:', cleanupError.message); }
      try {
        await retryLocalFileOperation(() => fileSystem.unlink(backup));
        backupContainsOriginal = false;
      } catch (cleanupError) {
        // The new file is already published.  Retaining a private recovery
        // copy is safer than reporting an ambiguous failed download.
        console.error('Remote download backup cleanup:', cleanupError.message);
      }
      return {replacedExisting: true};
    } catch (commitError) {
      if (!published) {
        try {
          // Only restore into an empty path.  If another process created the
          // destination, retain the backup instead of overwriting that file.
          const current = await captureLocalDestinationFingerprint(destination, {fileSystem});
          if (!current.exists) {
            await retryLocalFileOperation(() => fileSystem.rename(backup, destination));
            backupContainsOriginal = false;
          }
        } catch (restoreError) {
          const recoveryError = new RemoteFileError(
            'REMOTE_LOCAL_REPLACE_RECOVERY_REQUIRED',
            '本地下载提交失败；原文件仍保存在同目录的恢复文件中。',
            commitError,
          );
          recoveryError.restoreCause = restoreError;
          throw recoveryError;
        }
      }
      throw commitError;
    } finally {
      // When restoration could not safely claim the original path, leave the
      // backup in place for explicit recovery.
      if (backupContainsOriginal && !published) {
        try {
          const current = await captureLocalDestinationFingerprint(destination, {fileSystem});
          if (!current.exists) {
            await retryLocalFileOperation(() => fileSystem.rename(backup, destination));
            backupContainsOriginal = false;
          }
        } catch {}
      }
    }
  }

  try {
    await fileSystem.rename(temporary, destination);
    return {replacedExisting: false};
  } catch (error) {
    if (!LOCAL_REPLACE_COLLISION_CODES.has(error?.code)) throw error;
    let existing;
    try { existing = await fileSystem.lstat(destination); }
    catch {
      // A collision without an existing regular file is either transient or
      // a permissions problem. Do not remove anything to force the commit.
      throw error;
    }
    if (!existing.isFile() || existing.isSymbolicLink()) throw error;
  }

  const parent = path.dirname(destination);
  const filename = path.basename(destination);
  const backup = path.join(parent, `.${filename}.hetero-download-backup-${randomUUID()}`);
  await retryLocalFileOperation(() => fileSystem.rename(destination, backup));
  let backupContainsOriginal = true;
  try {
    try {
      await retryLocalFileOperation(() => fileSystem.rename(temporary, destination));
    } catch (commitError) {
      try {
        await retryLocalFileOperation(() => fileSystem.rename(backup, destination));
        backupContainsOriginal = false;
      } catch (restoreError) {
        const recoveryError = new RemoteFileError(
          'REMOTE_LOCAL_REPLACE_RECOVERY_REQUIRED',
          '本地下载提交失败；原文件仍保存在同目录的恢复文件中。',
          commitError,
        );
        recoveryError.restoreCause = restoreError;
        throw recoveryError;
      }
      throw commitError;
    }
    // The new destination is committed. Failure to remove the private backup
    // must not turn a completed download into an ambiguous failure.
    try {
      await retryLocalFileOperation(() => fileSystem.unlink(backup));
      backupContainsOriginal = false;
    } catch (cleanupError) {
      console.error('Remote download backup cleanup:', cleanupError.message);
    }
    return {replacedExisting: true};
  } finally {
    // A failed commit normally restores the old destination in the catch
    // above. If restoration itself failed, retain the backup for recovery.
    if (backupContainsOriginal) {
      try {
        await fileSystem.lstat(destination);
      } catch (error) {
        if (error?.code === 'ENOENT') {
          try {
            await retryLocalFileOperation(() => fileSystem.rename(backup, destination));
            backupContainsOriginal = false;
          } catch {}
        }
      }
    }
  }
}
function normalizeAgent(value = {}) {
  if (Array.isArray(value.adapters)) value = value.adapters[0] || {};
  const result = {...DEFAULTS.agent, ...value};
  if (!Array.isArray(result.args) || result.args.some(arg => typeof arg !== 'string' || arg.includes('\0'))) throw new Error('Agent args 必须为字符串数组。');
  if (!result.environment || Array.isArray(result.environment) || typeof result.environment !== 'object' || Object.entries(result.environment).some(([key, val]) => !/^[A-Za-z_][A-Za-z0-9_]*$/.test(key) || typeof val !== 'string' || val.includes('\0'))) throw new Error('Agent environment 必须为字符串键值对象。');
  result.submitMode = value.submitMode || value.submit_mode || DEFAULTS.agent.submitMode;
  if (!SUBMIT_MODES.has(result.submitMode)) throw new Error('不支持的 Agent 提交模式。');
  result.submitDelayMs = boundedInt(
    value.submitDelayMs ?? value.submit_delay_ms,
    AGENT_SUBMIT_DELAY_DEFAULT_MS,
    AGENT_SUBMIT_DELAY_MIN_MS,
    5000,
  );
  result.startupDelayMs = boundedInt(value.startupDelayMs ?? value.startup_input_delay_ms, 800, 0, 10000);
  // Older profiles may still carry the removed startup-input switch. Accept
  // those files, then discard the field so a later save completes migration.
  delete result.bootstrapContext;
  delete result.bootstrap_context;
  result.executable = String(result.executable || '').trim();
  result.cwd = String(result.cwd || '');
  result.demo = Boolean(value.demo ?? !result.executable);
  const placeholders = new Set(['workspace', 'mcp_config', 'mcp_descriptor', 'agent_session_id', 'run_id', 'skill_root']);
  for (const argument of result.args) for (const match of argument.matchAll(/\{([a-z_]+)\}/g)) if (!placeholders.has(match[1])) throw new Error(`不支持的 Agent argv 占位符：{${match[1]}}`);
  return result;
}
function publicSsh(value = {}) {
  return {host: String(value.host || '').trim(), port: boundedInt(value.port, 22, 1, 65535), username: String(value.username || '').trim(), cwd: String(value.cwd || '~'), auth: ['password', 'key', 'agent'].includes(value.auth) ? value.auth : 'password', privateKeyPath: String(value.privateKeyPath || '')};
}
function normalizeServerProfiles(value) {
  const profiles = {};
  if (!value || Array.isArray(value) || typeof value !== 'object') return profiles;
  for (const [key, profile] of Object.entries(value)) {
    if (!parseServerProfileTargetKey(key)) continue;
    try { profiles[key] = normalizeServerProfile(profile); } catch {}
  }
  return profiles;
}
function resolveExecutable(executable, environment, platform = process.platform) {
  if (path.isAbsolute(executable) || executable.includes('/') || executable.includes('\\')) return expandHome(executable);
  const pathKey = Object.keys(environment).find(key => key.toLowerCase() === 'path') || 'PATH';
  const dirs = String(environment[pathKey] || '').split(platform === 'win32' ? ';' : path.delimiter);
  const extensions = platform === 'win32' && !path.extname(executable) ? String(environment.PATHEXT || '.COM;.EXE;.BAT;.CMD').split(';') : [''];
  for (const dir of dirs) for (const extension of extensions) { const candidate = path.join(dir, executable + extension); try { if (fs.statSync(candidate).isFile()) return candidate; } catch {} }
  return executable;
}
function isAgentControlInput(data) {
  if (typeof data !== 'string' || !data) return false;
  return [...data].every(character => {
    const code = character.codePointAt(0);
    return (code >= 1 && code <= 8) || (code >= 11 && code <= 12)
      || (code >= 14 && code <= 31) || code === 127;
  });
}
function createAgentLaunch(executable, args, environment, platform = process.platform) {
  const file = resolveExecutable(executable, environment, platform);
  if (platform !== 'win32' || !/\.(cmd|bat)$/i.test(file)) return {file, args};
  // CMD files necessarily use cmd.exe. Keep each argument one quoted token and
  // reject CMD expansion syntax rather than silently reinterpreting user argv.
  const tokens = [file, ...args];
  if (tokens.some(token => /[\r\n"%!^]/.test(token) || token.includes('\0'))) throw new Error('Windows .cmd/.bat 参数包含 CMD 展开字符；请使用对应 .exe，或 node.exe 加 CLI 的 .js 入口。');
  const commandLine = tokens.map(token => `"${token}"`).join(' ');
  return {file: environment.ComSpec || environment.COMSPEC || 'C:\\Windows\\System32\\cmd.exe', args: `/d /s /v:off /c "${commandLine}"`};
}

class DesktopRuntime extends EventEmitter {
  constructor({userDataDir, resourcesDir, executablePath = process.execPath, version = '0.1.0', ptyModule,
    captureOptions, calibrationOptions, credentialStore = null, allowLegacySftpWriteFallback = false} = {}) {
    super();
    if (!userDataDir) throw new Error('userDataDir is required');
    this.userDataDir = userDataDir;
    this.resourcesDir = resourcesDir || path.dirname(__dirname);
    this.executablePath = executablePath;
    this.version = version;
    this.ptyModule = ptyModule;
    this.credentialStore = credentialStore;
    // Production writes must use the Workspace Worker: its cross-process lock
    // makes revision checks and atomic commit one indivisible operation. This
    // injection point exists only for the isolated legacy SFTP contract tests.
    this.allowLegacySftpWriteFallback = allowLegacySftpWriteFallback === true;
    this.settings = clone(DEFAULTS);
    this.ssh = {state: 'disconnected', shellState: 'closed'};
    this.agent = {state: 'stopped'};
    this.client = null;
    this.shell = null;
    this.sshTerminals = new Map();
    this.primarySshTerminalId = null;
    this.agentPty = null;
    this.agentOutputSequence = 0;
    this.agentBootstrap = {generation: 0, state: 'idle', enabled: false, message: ''};
    this.generation = 0;
    this.agentGeneration = 0;
    this.pendingExecs = new Set();
    this.pendingWorkspaceRequests = new Set();
    this.pendingRemoteFileSearches = new Map();
    this.pendingServerProfileInitializations = new Map();
    this.pendingAnalysisWorkerDeployments = new Map();
    this.pendingCodeAnalysisRuntimePreparations = new Map();
    // Keep remote Explorer traffic on long-lived SFTP channels. Interactive
    // reads/writes and background tree prefetch use separate channels so a
    // bounded crawl cannot add head-of-line latency to opening a file.
    this.remoteFileConnections = new Map();
    // One authenticated SSH client carries independent persistent Workspace
    // lanes. They share a protocol and target fence without head-of-line delay.
    this.remoteWorkspaceConnections = new Map();
    this.remoteWorkspaceFailures = new Map();
    this.remoteWorkspaceEpoch = 0;
    this.platformRpcConnections = new Map();
    this.pendingPlatformRequests = new Map();
    this.activePlatformSpec = null;
    this.pendingConnect = null;
    this.disposed = false;
    this.submitQueue = Promise.resolve();
    this.brokerToken = crypto.randomBytes(32).toString('hex');
    this.toolSession = {id: crypto.randomUUID(), boundTarget: null, taskContext: null, taskContract: null};
    this.broker = null;
    this.integration = null;
    this.agentRuns = new DesktopAgentRunStore(userDataDir);
    this.codeAgentSessions = new CodeAgentSessionStore({
      userDataDir,
      integration: () => this.integration,
      agentRuns: this.agentRuns,
    });
    this.legacyServerProfilePending = false;
    this.capture = new CaptureManager({userDataDir, ...captureEnvironment({appPath: this.resourcesDir, resourcesPath: process.resourcesPath}), ...captureOptions});
    this.capture.on('event', event => this.emit('event', event));
    this.calibration = new CalibrationManager({userDataDir, resourcesDir: this.resourcesDir, ...calibrationOptions});
    this.calibration.on('event', event => this.emit('event', event));
    this.on('event', event => this.agentRuns.operation(event));
    this.ready = this.initialize();
  }
  async initialize() {
    fs.mkdirSync(this.userDataDir, {recursive: true, mode: 0o700});
    this.agentSkillRoot = prepareManagedAgentSkills({
      userDataDir: this.userDataDir,
      resourcesDir: this.resourcesDir,
      version: this.version,
    });
    try {
      const saved = JSON.parse(fs.readFileSync(path.join(this.userDataDir, 'settings.json'), 'utf8'));
      const ssh = publicSsh(saved.ssh);
      const hadScopedProfiles = Boolean(saved.serverProfiles && !Array.isArray(saved.serverProfiles) && typeof saved.serverProfiles === 'object');
      const legacyPending = saved.serverProfileLegacyPending === true;
      const serverProfiles = normalizeServerProfiles(saved.serverProfiles);
      const legacyProfile = normalizeServerProfile(saved.serverProfile);
      const target = hasSshTarget(ssh) ? normalizeSshTarget(ssh) : null;
      const key = serverProfileTargetKey(target);
      // A pre-0.1.41 singleton belongs only to the SSH target saved beside it.
      // Once the scoped map exists, an unknown target always starts from defaults.
      if (key && (!hadScopedProfiles || legacyPending) && Object.hasOwn(saved, 'serverProfile')) serverProfiles[key] = legacyProfile;
      this.legacyServerProfilePending = Boolean(!key && (legacyPending || (!hadScopedProfiles && Object.hasOwn(saved, 'serverProfile'))));
      this.settings = {
        ssh,
        agent: normalizeAgent(saved.agent),
        serverProfile: key ? clone(serverProfiles[key] || normalizeServerProfile()) : legacyProfile,
        serverProfileTarget: target,
        serverProfiles,
        hdcPath: String(saved.hdcPath || 'hdc'),
        automation: normalizeAutomationSettings(saved.automation),
      };
    } catch {}
    const toolDir = path.join(this.userDataDir, 'integration');
    fs.mkdirSync(toolDir, {recursive: true, mode: 0o700});
    const packagedRuntime = path.join(this.resourcesDir, 'runtime').replace(/app\.asar([/\\])/, 'app.asar.unpacked$1');
    this.helperPath = path.join(packagedRuntime, 'agent-tools.cjs');
    if (!fs.existsSync(this.helperPath)) this.helperPath = path.join(__dirname, 'agent-tools.cjs');
    this.demoPath = path.join(path.dirname(this.helperPath), 'demo-agent.cjs');
    this.broker = http.createServer((req, res) => this.handleBroker(req, res));
    this.broker.headersTimeout = 5000;
    this.broker.requestTimeout = 10000;
    await new Promise((resolve, reject) => { this.broker.once('error', reject); this.broker.listen(0, '127.0.0.1', resolve); });
    const brokerUrl = `http://127.0.0.1:${this.broker.address().port}/tools`;
    const mcpConfigPath = path.join(toolDir, 'mcp.json');
    const descriptor = {mcpServers: {'desktop-ssh': {command: this.executablePath, args: [this.helperPath, '--mcp'], env: {ELECTRON_RUN_AS_NODE: '1'}}}};
    fs.writeFileSync(mcpConfigPath, `${JSON.stringify(descriptor, null, 2)}\n`, {mode: 0o600});
    const cliPath = path.join(toolDir, process.platform === 'win32' ? 'desktop-remote.cmd' : 'desktop-remote');
    const wrapper = process.platform === 'win32'
      ? `@echo off\r\nsetlocal\r\nset "ELECTRON_RUN_AS_NODE=1"\r\n"${this.executablePath}" "${this.helperPath}" %*\r\nexit /b %errorlevel%\r\n`
      : `#!/bin/sh\nELECTRON_RUN_AS_NODE=1 exec ${shellQuote(this.executablePath)} ${shellQuote(this.helperPath)} "$@"\n`;
    fs.writeFileSync(cliPath, wrapper, {mode: 0o700});
    this.integration = {mcpConfigPath, cliPath, helperPath: this.helperPath, executablePath: this.executablePath,
      brokerUrl, skillRoot: this.agentSkillRoot, tools: TOOLS.map(tool => tool.name), credentialDelivery: 'inherited_environment'};
  }
  emitStatus(target, state, message, extra = {}) {
    this[target] = {
      ...this[target], state, message: message || '', ...extra,
      ...(target === 'ssh' ? {connectionGeneration: this.generation} : {}),
    };
    this.emit('event', {type: 'status', target, ...this[target]});
  }
  sshTerminalDescriptor(record) {
    return {
      terminalId: record.terminalId,
      state: record.state,
      createdAt: record.createdAt,
      cols: record.cols,
      rows: record.rows,
    };
  }
  sshTerminalSnapshot() {
    const terminals = [...this.sshTerminals.values()].map(record => this.sshTerminalDescriptor(record));
    return {
      terminals,
      terminalCount: terminals.length,
      primaryTerminalId: this.primarySshTerminalId,
      shellState: terminals.some(terminal => terminal.state === 'open') ? 'open' : 'closed',
    };
  }
  syncSshTerminalAliases() {
    let primary = this.primarySshTerminalId ? this.sshTerminals.get(this.primarySshTerminalId) : null;
    if (!primary || primary.state !== 'open' || !primary.stream) {
      primary = [...this.sshTerminals.values()].find(record => record.state === 'open' && record.stream) || null;
      this.primarySshTerminalId = primary?.terminalId || null;
    }
    this.shell = primary?.stream || null;
    const snapshot = this.sshTerminalSnapshot();
    this.ssh = {...this.ssh, ...snapshot};
    return snapshot;
  }
  emitSshTerminalLifecycle(action, record, reason = '') {
    const snapshot = this.syncSshTerminalAliases();
    this.emit('event', {
      type: 'ssh-terminal', target: 'ssh', action, terminalId: record.terminalId,
      terminal: this.sshTerminalDescriptor(record), reason, ...snapshot,
    });
  }
  removeSshTerminal(terminalId, stream, reason) {
    const record = this.sshTerminals.get(terminalId);
    if (!record || (stream && record.stream !== stream)) return false;
    this.sshTerminals.delete(terminalId);
    if (this.primarySshTerminalId === terminalId) this.primarySshTerminalId = null;
    record.state = 'closed';
    this.emitSshTerminalLifecycle('closed', record, reason);
    return true;
  }
  closeAllSshTerminals(reason) {
    const records = [...this.sshTerminals.values()];
    for (const record of records) {
      this.removeSshTerminal(record.terminalId, record.stream, reason);
      try { record.stream?.close(); } catch {}
    }
    this.syncSshTerminalAliases();
  }
  async openSshTerminal({client = this.client, generation = this.generation, cwd = this.ssh.cwd, cols = 100, rows = 28} = {}) {
    cols = boundedInt(cols, 100, 10, 500);
    rows = boundedInt(rows, 28, 3, 200);
    if (!client || client !== this.client || generation !== this.generation) throw new Error('SSH 连接已改变，无法创建终端。');
    if (this.sshTerminals.size >= MAX_SSH_TERMINALS) throw new Error(`最多可同时打开 ${MAX_SSH_TERMINALS} 个 SSH 终端。`);
    const terminalId = crypto.randomUUID();
    const record = {terminalId, state: 'opening', createdAt: new Date().toISOString(), cols, rows, stream: null};
    this.sshTerminals.set(terminalId, record);
    try {
      const stream = await new Promise((resolve, reject) => {
        client.shell({term: 'xterm-256color', cols, rows}, (error, channel) => error ? reject(error) : resolve(channel));
      });
      if (client !== this.client || generation !== this.generation || !this.sshTerminals.has(terminalId)) {
        try { stream.close(); } catch {}
        throw new Error('SSH 连接已改变，终端创建已取消。');
      }
      record.stream = stream;
      record.state = 'open';
      stream.setEncoding('utf8');
      stream.stderr.setEncoding('utf8');
      stream.on('data', data => this.emit('event', {type: 'terminal', target: 'ssh', terminalId, data}));
      stream.stderr.on('data', data => this.emit('event', {type: 'terminal', target: 'ssh', terminalId, data}));
      stream.on('error', error => this.emit('event', {type: 'operation', target: 'ssh', terminalId, state: 'error', message: error.message}));
      stream.on('close', () => {
        if (!this.removeSshTerminal(terminalId, stream, 'remote_closed')) return;
        if (this.client === client && this.ssh.state === 'connected') {
          const message = this.sshTerminals.size
            ? '一个远程交互终端已退出；其他终端和 SSH 工具连接仍可用。'
            : '远程交互终端已退出；SSH 工具连接仍可用，可新建终端。';
          this.ssh = {...this.ssh, message};
          this.emit('event', {type: 'status', target: 'ssh', ...clone(this.ssh), terminalId});
        }
      });
      if (!this.primarySshTerminalId) this.primarySshTerminalId = terminalId;
      this.syncSshTerminalAliases();
      stream.write(`${remotePrefix(cwd)}exec bash\r`);
      this.emitSshTerminalLifecycle('created', record);
      return this.sshTerminalDescriptor(record);
    } catch (error) {
      this.removeSshTerminal(terminalId, record.stream, 'create_failed');
      throw error;
    }
  }
  async sshPasswordCredentialStatus(target = this.settings.ssh) {
    if (!this.credentialStore) return {available: false, saved: false, error: '当前运行环境未提供系统安全存储，SSH 密码无法持久化。'};
    try { return await this.credentialStore.getSshPasswordStatus(target); }
    catch (error) { return {available: false, saved: false, error: error.message || 'SSH 密码安全存储状态无法读取。'}; }
  }
  async getState() {
    await this.ready;
    const settings = clone(this.settings);
    const profileTarget = hasSshTarget(settings.serverProfileTarget) ? settings.serverProfileTarget : null;
    const profileScopeId = this.serverProfileScopeId(profileTarget);
    settings.serverProfileScopeId = profileScopeId;
    settings.serverProfileManagedRoot = symbolicManagedPaths(settings.serverProfile.profileId, profileScopeId).managedRoot;
    settings.ssh.passwordCredential = await this.sshPasswordCredentialStatus(settings.ssh);
    return {version: this.version, platform: process.platform, settings,
      ssh: clone({...this.ssh, connectionGeneration: this.generation}),
      agent: clone(this.agent), integration: clone(this.integration), capture: await this.capture.getState()};
  }
  async forgetSshPassword(config = {}) {
    await this.ready;
    const target = publicSsh({...this.settings.ssh, ...config});
    if (!target.host || !target.username) throw new Error('请先填写 SSH 主机和用户名。');
    if (!this.credentialStore) throw new Error('当前运行环境未提供系统安全存储，无法删除 SSH 密码。');
    await this.credentialStore.deleteSshPassword(target);
    return this.getState();
  }
  async captureCatalog() { await this.ready; return this.capture.catalog(); }
  async saveCapturePlan(value) { await this.ready; return this.capture.savePlan(value); }
  async updateCapturePlan(value) { await this.ready; return this.capture.updatePlan(value); }
  async selectCaptureDevice(value) { await this.ready; return this.capture.selectDevice(value); }
  async startCapture(value = {}) {
    await this.ready;
    // Node discovery searches PATH, whereas the Python runner accepts a file.
    // Freeze the same executable as an absolute path for the child process.
    const executable = resolveExecutable(this.settings.hdcPath, process.env);
    return this.capture.startCapture({...value, hdcPath: path.resolve(executable)});
  }
  async captureStatus({planId} = {}) {
    await this.ready;
    const snapshot = this.capture.getState();
    const summarizeRun = run => {
      if (!run) return null;
      const summary = {...run};
      delete summary.planSnapshot;
      delete summary.result;
      summary.log = (summary.log || []).slice(-12);
      return summary;
    };
    const selectedPlan = planId ? snapshot.plans.find(plan => plan.planId === planId) : null;
    if (planId && !selectedPlan) throw new Error('采集计划不存在。');
    return {selectedDeviceSerial: snapshot.selectedDeviceSerial, devices: snapshot.devices, runtime: snapshot.runtime,
      recoveryNotice: snapshot.recoveryNotice, activeRun: summarizeRun(snapshot.activeRun),
      plans: snapshot.plans.slice(0, 30).map(({plan, ...summary}) => ({...summary, readiness: plan.readiness, capture: plan.capture})),
      runs: snapshot.runs.slice(0, 20).map(summarizeRun), traces: snapshot.traces.slice(0, 30),
      totals: {plans: snapshot.plans.length, runs: snapshot.runs.length, traces: snapshot.traces.length},
      ...(selectedPlan ? {plan: selectedPlan} : {})};
  }
  async cancelCapture(value) { await this.ready; return this.capture.cancelCapture(value); }
  async listTraces() { await this.ready; return this.capture.listTraces(); }
  async registerTrace(value) { await this.ready; return this.capture.registerTrace(value); }
  async getTrace(value) { await this.ready; return this.capture.getTrace(value); }
  async removeTrace(value) { await this.ready; return this.capture.removeTrace(value); }
  async listTraceAnalysisSources() { await this.ready; return {sources: this.capture.listTraceAnalysisSources()}; }
  async prepareTraceArchive({traceId} = {}) {
    await this.ready;
    const {archivePath, attributes} = this.capture.traceArchiveForAnalysis({id: traceId});
    if (!Number.isSafeInteger(attributes.size) || attributes.size < 1 || attributes.size > MAX_CAPTURE_ARCHIVE_BYTES)
      throw new Error('采集归档大小无效或超过 2 GiB 分析传输上限。');
    const archiveSha256 = await sha256File(archivePath);
    const after = fs.statSync(archivePath);
    if (attributes.dev !== after.dev || attributes.ino !== after.ino || attributes.size !== after.size || attributes.mtimeMs !== after.mtimeMs)
      throw new Error('采集归档在准备传输期间发生变化，请重试。');
    return {traceId, archiveSha256, archiveSizeBytes: attributes.size};
  }
  assertTraceTransferTarget(expectedTarget, label = '采集归档') {
    const normalized = normalizeSshTarget(expectedTarget);
    const current = this.currentSshTarget();
    if (!hasSshTarget(normalized) || !current || !sameSshTarget(normalized, current))
      throw new Error(`SSH 目标已改变，已停止${label}传输。`);
    return {target: normalized, client: this.client, generation: this.generation};
  }
  async withSftpTransfer(expectedTarget, operation, {label = '采集归档'} = {}) {
    await this.ready;
    if (this.ssh.state !== 'connected' || !this.client) throw new Error('请先连接 SSH 服务器。');
    const transfer = this.assertTraceTransferTarget(expectedTarget, label);
    const sftp = await new Promise((resolve, reject) => {
      let settled = false;
      const finish = (error, value) => {
        if (settled) { if (value) try { value.end(); } catch {} return; }
        settled = true; clearTimeout(timer);
        if (error) reject(new Error(`SSH ${label}通道初始化失败：${error.message}`)); else resolve(value);
      };
      const timer = setTimeout(() => finish(new Error('初始化 SFTP 超时。')), 15000);
      try { transfer.client.sftp((error, value) => finish(error, value)); }
      catch (error) { finish(error); }
    });
    let channelError = null;
    if (typeof sftp.on === 'function') sftp.on('error', error => { channelError = error; });
    try {
      this.assertTraceTransferTarget(transfer.target, label);
      const result = await operation(sftp, transfer);
      if (channelError) throw new Error(`SSH ${label}通道异常：${channelError.message}`);
      this.assertTraceTransferTarget(transfer.target, label);
      if (transfer.client !== this.client || transfer.generation !== this.generation) throw new Error(`SSH 连接在${label}传输期间发生变化。`);
      return result;
    } finally { try { sftp.end(); } catch {} }
  }
  validateTraceUploadTarget({uploadToken, remotePath, expectedTarget}) {
    const token = String(uploadToken || '');
    const target = String(remotePath || '');
    const sshTarget = normalizeSshTarget(expectedTarget);
    if (!hasSshTarget(sshTarget)) throw new Error('采集归档上传缺少有效的 SSH 目标。');
    const scopeId = this.serverProfileScopeId(sshTarget);
    const profileId = this.serverProfileForTarget(sshTarget).profileId;
    const managedSuffix = `/.local/share/heterogeneous-workbench/server-profiles/targets/${scopeId}/${profileId}/state/desktop-capture-uploads/${token}.capture.zip`;
    if (!CAPTURE_UPLOAD_TOKEN.test(token)) throw new Error('采集归档上传令牌无效。');
    if (!target.startsWith('/') || target.includes('\\') || target.includes('\0') || path.posix.normalize(target) !== target
        || !target.endsWith(managedSuffix) || path.posix.basename(target) !== `${token}.capture.zip`)
      throw new Error('服务器返回的采集归档托管路径无效。');
    return {token, target};
  }
  async uploadTraceArchive({traceId, uploadToken, remotePath, archiveSha256, archiveSizeBytes, expectedTarget} = {}) {
    await this.ready;
    this.assertTraceTransferTarget(expectedTarget);
    const upload = this.validateTraceUploadTarget({uploadToken, remotePath, expectedTarget});
    const expectedHash = String(archiveSha256 || '');
    const expectedSize = Number(archiveSizeBytes);
    if (!SHA256.test(expectedHash) || !Number.isSafeInteger(expectedSize) || expectedSize < 1 || expectedSize > MAX_CAPTURE_ARCHIVE_BYTES)
      throw new Error('采集归档传输校验信息无效。');
    const {archivePath, attributes} = this.capture.traceArchiveForAnalysis({id: traceId});
    if (attributes.size !== expectedSize || await sha256File(archivePath) !== expectedHash)
      throw new Error('采集归档与准备传输时的大小或 SHA-256 不一致，请重新开始分析。');
    return this.withSftpTransfer(expectedTarget, async sftp => {
      await new Promise((resolve, reject) => {
        sftp.lstat(upload.target, (error, value) => {
          if (!error || value) reject(new Error('服务器托管暂存文件已存在，已拒绝覆盖。'));
          else if (error.code === 2 || error.code === 'ENOENT') resolve();
          else reject(new Error(`无法核验服务器暂存路径：${error.message}`));
        });
      });
      await new Promise((resolve, reject) => {
        let settled = false;
        const finish = error => {
          if (settled) return;
          settled = true; clearTimeout(timer);
          if (error) reject(new Error(`采集归档上传失败：${error.message}`)); else resolve();
        };
        const timer = setTimeout(() => finish(new Error('传输超过 30 分钟。')), 30 * 60 * 1000);
        try { sftp.fastPut(archivePath, upload.target, {concurrency: 8, chunkSize: 128 * 1024}, finish); }
        catch (error) { finish(error); }
      });
      const remote = await new Promise((resolve, reject) => sftp.lstat(upload.target, (error, value) => error ? reject(error) : resolve(value)));
      if (!remote || Number(remote.size) !== expectedSize) {
        await new Promise(resolve => sftp.unlink(upload.target, () => resolve()));
        throw new Error('服务器暂存归档大小与本机不一致，已删除不完整文件。');
      }
      const after = fs.statSync(archivePath);
      if (attributes.dev !== after.dev || attributes.ino !== after.ino || attributes.size !== after.size || attributes.mtimeMs !== after.mtimeMs) {
        await new Promise(resolve => sftp.unlink(upload.target, () => resolve()));
        throw new Error('采集归档在传输期间发生变化，已删除服务器暂存文件。');
      }
      return {uploaded: true, uploadToken: upload.token, archiveSha256: expectedHash, archiveSizeBytes: expectedSize};
    });
  }
  async discardTraceUpload({uploadToken, remotePath, expectedTarget} = {}) {
    await this.ready;
    this.assertTraceTransferTarget(expectedTarget);
    const upload = this.validateTraceUploadTarget({uploadToken, remotePath, expectedTarget});
    return this.withSftpTransfer(expectedTarget, sftp => new Promise((resolve, reject) => {
      sftp.unlink(upload.target, error => {
        if (error && error.code !== 2 && error.code !== 'ENOENT') reject(new Error(`清理服务器暂存归档失败：${error.message}`));
        else resolve({removed: !error, uploadToken: upload.token});
      });
    }));
  }
  validateCalibrationMeasurementUploadTarget({uploadToken, remotePath, expectedTarget}) {
    const token = String(uploadToken || '');
    const target = String(remotePath || '');
    const sshTarget = normalizeSshTarget(expectedTarget);
    if (!hasSshTarget(sshTarget)) throw new Error('校准实测上传缺少有效的 SSH 目标。');
    const scopeId = this.serverProfileScopeId(sshTarget);
    const profileId = this.serverProfileForTarget(sshTarget).profileId;
    const managedSuffix = `/.local/share/heterogeneous-workbench/server-profiles/targets/${scopeId}/${profileId}/state/parallel-calibration-measurement-uploads/${token}.measurements.json`;
    if (!CAPTURE_UPLOAD_TOKEN.test(token)) throw new Error('校准实测上传令牌无效。');
    if (!target.startsWith('/') || target.includes('\\') || target.includes('\0') || path.posix.normalize(target) !== target
        || !target.endsWith(managedSuffix) || path.posix.basename(target) !== `${token}.measurements.json`)
      throw new Error('服务器返回的校准实测托管路径无效。');
    return {token, target};
  }
  async uploadCalibrationMeasurements({uploadToken, remotePath, measurementText, measurementSha256,
    measurementSizeBytes, expectedTarget} = {}) {
    await this.ready;
    this.assertTraceTransferTarget(expectedTarget, '校准实测');
    const upload = this.validateCalibrationMeasurementUploadTarget({uploadToken, remotePath, expectedTarget});
    if (typeof measurementText !== 'string') throw new Error('校准实测内容必须是 UTF-8 JSON 文本。');
    const contents = Buffer.from(measurementText, 'utf8');
    const expectedSize = Number(measurementSizeBytes);
    const expectedHash = String(measurementSha256 || '');
    const actualHash = crypto.createHash('sha256').update(contents).digest('hex');
    if (!Number.isSafeInteger(expectedSize) || expectedSize < 1 || expectedSize > MAX_CALIBRATION_MEASUREMENT_BYTES
        || contents.length !== expectedSize || !SHA256.test(expectedHash) || actualHash !== expectedHash)
      throw new Error('校准实测内容与准备上传时的大小或 SHA-256 不一致。');
    return this.withSftpTransfer(expectedTarget, async sftp => {
      await new Promise((resolve, reject) => {
        sftp.lstat(upload.target, (error, value) => {
          if (!error || value) reject(new Error('服务器校准实测暂存文件已存在，已拒绝覆盖。'));
          else if (error.code === 2 || error.code === 'ENOENT') resolve();
          else reject(new Error(`无法核验校准实测暂存路径：${error.message}`));
        });
      });
      try {
        await new Promise((resolve, reject) => {
          let settled = false;
          const finish = error => {
            if (settled) return;
            settled = true; clearTimeout(timer);
            if (error) reject(new Error(`校准实测上传失败：${error.message}`)); else resolve();
          };
          const timer = setTimeout(() => finish(new Error('传输超过 5 分钟。')), 5 * 60 * 1000);
          try { sftp.writeFile(upload.target, contents, {mode: 0o600, flag: 'wx'}, finish); }
          catch (error) { finish(error); }
        });
        const remote = await new Promise((resolve, reject) => sftp.lstat(upload.target,
          (error, value) => error ? reject(error) : resolve(value)));
        if (!remote || Number(remote.size) !== expectedSize)
          throw new Error('服务器校准实测暂存文件大小与本机不一致。');
      } catch (error) {
        await new Promise(resolve => sftp.unlink(upload.target, () => resolve()));
        throw error;
      }
      return {uploaded: true, uploadToken: upload.token,
        measurementSha256: expectedHash, measurementSizeBytes: expectedSize};
    }, {label: '校准实测'});
  }
  async discardCalibrationMeasurementUpload({uploadToken, remotePath, expectedTarget} = {}) {
    await this.ready;
    this.assertTraceTransferTarget(expectedTarget, '校准实测');
    const upload = this.validateCalibrationMeasurementUploadTarget({uploadToken, remotePath, expectedTarget});
    return this.withSftpTransfer(expectedTarget, sftp => new Promise((resolve, reject) => {
      sftp.unlink(upload.target, error => {
        if (error && error.code !== 2 && error.code !== 'ENOENT') reject(new Error(`清理服务器校准实测暂存文件失败：${error.message}`));
        else resolve({removed: !error, uploadToken: upload.token});
      });
    }), {label: '校准实测'});
  }
  async getIntegration() { await this.ready; return clone(this.integration); }
  async listAgentRuns(value = {}) { await this.ready; return this.agentRuns.list(value); }
  async getAgentRun(value = {}) { await this.ready; return this.agentRuns.get(value); }
  async listCodeAgentSessions(value = {}) { await this.ready; return this.codeAgentSessions.list(value); }
  async getCodeAgentSession(value = {}) { await this.ready; return this.codeAgentSessions.get(value); }
  async configureCodeAgentSessionRoot(value = {}) { await this.ready; return this.codeAgentSessions.configure(value); }
  closeRemoteWorkspaceConnection(record, reason = new Error('远端 Workspace 通道已关闭。')) {
    if (!record || record.state === 'closed') return;
    if (this.remoteWorkspaceConnections.get(record.role) === record)
      this.remoteWorkspaceConnections.delete(record.role);
    record.close(reason);
  }
  closeRemoteWorkspaceConnections(reason = new Error('SSH 连接已改变，远端 Workspace 通道已关闭。')) {
    this.remoteWorkspaceEpoch += 1;
    const records = [...this.remoteWorkspaceConnections.values()];
    this.remoteWorkspaceConnections.clear();
    this.remoteWorkspaceFailures.clear();
    for (const record of records) record.close(reason);
  }
  async acquireRemoteWorkspaceConnection({role = 'interactive', speculative = false} = {}) {
    await this.ready;
    if (!['interactive', 'prefetch', 'explorer-search', 'agent', 'platform'].includes(role))
      throw new Error('远端 Workspace 通道角色无效。');
    if (this.ssh.state !== 'connected' || !this.client) {
      const error = new Error('请先连接 SSH 服务器。');
      error.code = 'WORKSPACE_NOT_CONNECTED';
      error.workspaceTransportUnavailable = true;
      throw error;
    }
    const client = this.client;
    const generation = this.generation;
    const workspaceEpoch = this.remoteWorkspaceEpoch;
    const root = this.ssh.remoteRootPath;
    const target = this.currentSshTarget();
    const pythonExecutable = target ? this.serverProfileForTarget(target).pythonBin : 'python3';
    const existing = this.remoteWorkspaceConnections.get(role);
    if (existing && existing.client === client && existing.generation === generation
        && existing.root === root && existing.pythonExecutable === pythonExecutable
        && existing.state !== 'closed') {
      try { return await existing.workspaceReady; }
      catch (error) {
        // A real request that happened to join a speculative warmup must not
        // inherit that warmup's transient failure. Retry once through the
        // authoritative path, which may then install the normal cooldown.
        if (!speculative && existing.speculative
            && this.remoteWorkspaceConnections.get(role) !== existing)
          return this.acquireRemoteWorkspaceConnection({role, speculative: false});
        throw error;
      }
    }
    if (existing) this.closeRemoteWorkspaceConnection(existing, new Error('远端 Workspace 目标已更新。'));
    const recentFailure = this.remoteWorkspaceFailures.get(role);
    if (recentFailure?.generation === generation
        && recentFailure.client === client
        && Date.now() < recentFailure.retryAt) {
      const error = new Error(`远端 Workspace Worker 暂不可用：${recentFailure.error.message}`);
      error.code = 'WORKSPACE_RPC_UNAVAILABLE';
      error.cause = recentFailure.error;
      error.workspaceTransportUnavailable = true;
      throw error;
    }

    let launch;
    try { launch = createWorkspaceWorkerLaunch({workspaceRoot: root, pythonExecutable}); }
    catch (cause) {
      const error = new Error(`无法准备远端 Workspace Worker：${cause.message}`);
      error.code = 'WORKSPACE_RPC_UNAVAILABLE';
      error.cause = cause;
      error.workspaceTransportUnavailable = true;
      throw error;
    }
    let record;
    try {
      record = new WorkspaceRpcChannel({
        client, command: launch.command, bootstrapPayload: launch.bootstrapPayload,
        generation, key: `workspace:${role}`, readyTimeoutMs: 10000, requestTimeoutMs: 30000,
      });
    } catch (cause) {
      if (!speculative && workspaceEpoch === this.remoteWorkspaceEpoch) this.remoteWorkspaceFailures.set(role, {
        client, generation, error: cause instanceof Error ? cause : new Error(String(cause)),
        retryAt: Date.now() + WORKSPACE_WORKER_FAILURE_COOLDOWN_MS,
      });
      const error = cause instanceof Error ? cause : new Error(String(cause));
      error.workspaceTransportUnavailable = true;
      throw error;
    }
    Object.assign(record, {role, client, generation, root, pythonExecutable, workspaceEpoch, speculative,
      sourceSha256: launch.sourceSha256});
    this.remoteWorkspaceConnections.set(role, record);
    record.on('closed', () => {
      if (this.remoteWorkspaceConnections.get(role) === record)
        this.remoteWorkspaceConnections.delete(role);
    });
    record.workspaceReady = (async () => {
      try {
        await record.ready;
        const ping = await record.request({method: 'ping', timeoutMs: 5000, internal: true});
        if (ping.result?.protocol !== 'remote_workspace_rpc/v1' || ping.result?.root !== root)
          throw Object.assign(new Error('远端 Workspace Worker 返回了不匹配的协议或工作目录。'), {code: 'WORKSPACE_RPC_INVALID_RESPONSE'});
        if (record.state === 'closed' || client !== this.client || generation !== this.generation
            || this.ssh.state !== 'connected' || this.ssh.remoteRootPath !== root) {
          throw Object.assign(new Error('SSH 连接在 Workspace Worker 初始化期间发生变化。'), {code: 'WORKSPACE_CONNECTION_CHANGED'});
        }
        const failed = this.remoteWorkspaceFailures.get(role);
        if (failed?.client === client && failed?.generation === generation)
          this.remoteWorkspaceFailures.delete(role);
        record.workspaceReadyAt = Date.now();
        record.workspaceStartupDurationMs = Math.max(0, record.workspaceReadyAt - record.startedAt);
        return record;
      } catch (cause) {
        this.closeRemoteWorkspaceConnection(record, cause);
        if (!speculative && workspaceEpoch === this.remoteWorkspaceEpoch && client === this.client
            && generation === this.generation && root === this.ssh.remoteRootPath
            && pythonExecutable === this.serverProfileForTarget(this.currentSshTarget()).pythonBin) {
          this.remoteWorkspaceFailures.set(role, {
            client, generation, error: cause instanceof Error ? cause : new Error(String(cause)),
            retryAt: Date.now() + WORKSPACE_WORKER_FAILURE_COOLDOWN_MS,
          });
        }
        const error = cause instanceof Error ? cause : new Error(String(cause));
        error.workspaceTransportUnavailable = true;
        throw error;
      }
    })();
    record.workspaceReady.catch(() => {});
    return record.workspaceReady;
  }
  prewarmAgentWorkspaceConnection() {
    const currentTarget = this.currentSshTarget();
    // This is an optimization only. Never open an Agent lane against a target
    // that the current tool session is not already bound to.
    if (this.agent.state !== 'running' || !currentTarget || !this.toolSession.boundTarget
        || !this.sameSshTarget(this.toolSession.boundTarget, currentTarget)) return false;
    // A speculative warmup must never install the normal 30-second failure
    // cooldown. If it loses a transient race, the first real tool call gets a
    // fresh authoritative attempt immediately.
    void this.acquireRemoteWorkspaceConnection({role: 'agent', speculative: true}).catch(() => {});
    return true;
  }
  async workspaceRequest({role = 'interactive', method, params = {}, timeoutMs = 30000,
    requestId = null, isCancelled = null, cancellationError = null, expectedIdentity = null} = {}) {
    const identityMatchesCurrent = () => !expectedIdentity || (
      expectedIdentity.client === this.client
      && expectedIdentity.generation === this.generation
      && expectedIdentity.root === this.ssh.remoteRootPath
      && this.ssh.state === 'connected'
    );
    if (!identityMatchesCurrent()) {
      throw Object.assign(
        new Error('SSH 目标在 Workspace 请求前发生变化，已拒绝在新目标上执行。'),
        {code: 'WORKSPACE_CONNECTION_CHANGED'},
      );
    }
    let record;
    const acquireStartedAt = Date.now();
    try { record = await this.acquireRemoteWorkspaceConnection({role}); }
    catch (error) {
      error.workspaceTransportUnavailable = true;
      throw error;
    }
    const channelAcquireDurationMs = Math.max(0, Date.now() - acquireStartedAt);
    const client = record.client;
    const generation = record.generation;
    const root = record.root;
    if (!identityMatchesCurrent() || (expectedIdentity && (
      expectedIdentity.client !== client
      || expectedIdentity.generation !== generation
      || expectedIdentity.root !== root
    ))) {
      throw Object.assign(
        new Error('SSH 目标在 Workspace Worker 获取期间发生变化，已拒绝在新目标上执行。'),
        {code: 'WORKSPACE_CONNECTION_CHANGED'},
      );
    }
    if (isCancelled?.()) throw cancellationError?.() || Object.assign(new Error('远端 Workspace 请求已取消。'), {code: 'WORKSPACE_RPC_CANCELLED'});
    let response;
    try {
      // Once record.request() is entered, a mutation frame may have reached the
      // worker even when its response is later lost. Preserve this distinction
      // so multi-file mutations can report the current file as commit-unknown.
      response = await record.request({
        ...(requestId ? {requestId} : {}), method, params, timeoutMs,
      });
    } catch (error) {
      if (error && (typeof error === 'object' || typeof error === 'function'))
        error.workspaceRequestDispatched = true;
      // A timed-out handler may still hold a worker thread (for example an
      // uninterruptible library call). Retire only that role's lane; callers
      // decide separately whether their operation is safe to fall back.
      if (error?.code === 'WORKSPACE_RPC_REQUEST_TIMEOUT')
        this.closeRemoteWorkspaceConnection(record, error);
      throw error;
    }
    if (isCancelled?.()) throw cancellationError?.() || Object.assign(new Error('远端 Workspace 请求已取消。'), {code: 'WORKSPACE_RPC_CANCELLED'});
    if (client !== this.client || generation !== this.generation || this.ssh.state !== 'connected'
        || root !== this.ssh.remoteRootPath) {
      const error = new Error('SSH 目标在远端 Workspace 请求期间发生变化，已丢弃旧目标结果。');
      error.code = 'WORKSPACE_CONNECTION_CHANGED';
      error.workspaceRequestDispatched = true;
      error.workspaceResponseReceived = true;
      throw error;
    }
    return {
      ...response.result,
      transport: 'workspace-worker',
      channelReused: response.channelReused,
      transportDurationMs: response.durationMs,
      channelAcquireDurationMs,
      channelStartupDurationMs: Number.isFinite(record.workspaceStartupDurationMs)
        ? record.workspaceStartupDurationMs : null,
    };
  }
  closeRemoteFileConnection(record, {end = true} = {}) {
    if (!record || record.state === 'closed') return;
    record.state = 'closed';
    if (this.remoteFileConnections.get(record.role) === record) this.remoteFileConnections.delete(record.role);
    // Keep the installed error listener until the wrapper is collected. ssh2
    // may emit one final protocol error after end(), and removing it here would
    // turn an expected disconnect into an uncaught Main Process exception.
    if (end && record.sftp) try { record.sftp.end(); } catch {}
  }
  closeRemoteFileConnections() {
    const records = [...this.remoteFileConnections.values()];
    this.remoteFileConnections.clear();
    for (const record of records) this.closeRemoteFileConnection(record);
  }
  assertRemoteFileIdentity(expectedIdentity) {
    if (!expectedIdentity) return;
    if (expectedIdentity.client !== this.client || expectedIdentity.generation !== this.generation
        || expectedIdentity.root !== this.ssh.remoteRootPath)
      throw new RemoteFileError('REMOTE_CONNECTION_CHANGED', 'SSH 目标在远端文件操作期间发生变化，已拒绝在新目标上重放。');
  }
  captureRemoteFileIdentity(expectedConnection = null) {
    if (this.ssh.state !== 'connected' || !this.client)
      throw new RemoteFileError('REMOTE_NOT_CONNECTED', '请先连接 SSH 服务器。');
    if (expectedConnection !== null && expectedConnection !== undefined) {
      const expectedTarget = normalizeSshTarget(expectedConnection?.target);
      const expectedRoot = String(expectedConnection?.root || '');
      const expectedGeneration = Number(expectedConnection?.generation);
      const currentTarget = this.currentSshTarget();
      if (!hasSshTarget(expectedTarget) || !currentTarget
          || !sameSshTarget(expectedTarget, currentTarget)
          || !expectedRoot || expectedRoot !== this.ssh.remoteRootPath
          || !Number.isSafeInteger(expectedGeneration) || expectedGeneration !== this.generation) {
        throw new RemoteFileError(
          'REMOTE_CONNECTION_CHANGED',
          'SSH 目标与远程资源管理器显示的连接不一致，已拒绝在当前目标上执行。',
        );
      }
    }
    return {client: this.client, generation: this.generation, root: this.ssh.remoteRootPath};
  }
  async validateRemoteFileConnection(expectedConnection = null) {
    await this.ready;
    this.captureRemoteFileIdentity(expectedConnection);
    return {ok: true};
  }
  prepareRemoteFileRequest(value = {}) {
    const request = value && !Array.isArray(value) && typeof value === 'object' ? value : {};
    const {expectedConnection = null, ...params} = request;
    return {params, identity: this.captureRemoteFileIdentity(expectedConnection)};
  }
  async acquireRemoteFileConnection({role = 'interactive', operationTimeoutMs, expectedIdentity = null} = {}) {
    this.assertRemoteFileIdentity(expectedIdentity);
    await this.ready;
    this.assertRemoteFileIdentity(expectedIdentity);
    if (this.ssh.state !== 'connected' || !this.client)
      throw new RemoteFileError('REMOTE_NOT_CONNECTED', '请先连接 SSH 服务器。');
    const client = this.client;
    const generation = this.generation;
    const existing = this.remoteFileConnections.get(role);
    if (existing && existing.client === client && existing.generation === generation && existing.state !== 'closed')
      return existing.ready;
    if (existing) this.closeRemoteFileConnection(existing);

    const record = {role, client, generation, state: 'opening', sftp: null, service: null, ready: null,
      operationCount: 0};
    this.remoteFileConnections.set(role, record);
    record.ready = (async () => {
      const sftp = await new Promise((resolve, reject) => {
        let settled = false;
        const finish = (error, value) => {
          if (settled) {
            if (value) try { value.end(); } catch {}
            return;
          }
          settled = true;
          clearTimeout(timer);
          if (error) reject(error); else resolve(value);
        };
        const timer = setTimeout(() => finish(new RemoteFileError('REMOTE_SFTP_TIMEOUT', 'SSH 文件通道初始化超时。')), 10000);
        try {
          client.sftp((error, value) => finish(error
            ? new RemoteFileError('REMOTE_SFTP_UNAVAILABLE', `SSH 文件通道初始化失败：${error.message}`, error)
            : null, value));
        } catch (error) {
          finish(new RemoteFileError('REMOTE_SFTP_UNAVAILABLE', `SSH 文件通道初始化失败：${error.message}`, error));
        }
      });
      record.sftp = sftp;
      const invalidate = (error, {end = false} = {}) => {
        record.channelError ||= error || new Error('SSH 文件通道已关闭。');
        this.closeRemoteFileConnection(record, {end});
      };
      if (typeof sftp.on === 'function') {
        sftp.on('error', error => invalidate(error, {end: true}));
        sftp.on('close', () => invalidate());
        sftp.on('end', () => invalidate());
      }
      if (record.state === 'closed' || client !== this.client || generation !== this.generation || this.ssh.state !== 'connected') {
        // If disconnect closed the record before the asynchronous SFTP
        // callback arrived, the helper has no attached channel to end. Close
        // this late channel explicitly; otherwise retire the live record once.
        if (record.state === 'closed') { try { sftp.end(); } catch {} }
        else this.closeRemoteFileConnection(record);
        throw new RemoteFileError('REMOTE_CONNECTION_CHANGED', 'SSH 连接已变化，请重新加载远程文件。');
      }
      this.assertRemoteFileIdentity(expectedIdentity);
      const rootCandidate = expectedIdentity?.root || this.ssh.remoteRootPath || (this.ssh.cwd === '~' ? '.' : this.ssh.cwd);
      record.service = await RemoteFileService.create(sftp, rootCandidate, {operationTimeoutMs});
      this.assertRemoteFileIdentity(expectedIdentity);
      if (record.state === 'closed' || client !== this.client || generation !== this.generation || this.ssh.state !== 'connected') {
        this.closeRemoteFileConnection(record);
        throw new RemoteFileError('REMOTE_CONNECTION_CHANGED', 'SSH 连接已变化，请重新加载远程文件。');
      }
      record.state = 'ready';
      return record;
    })().catch(error => {
      this.closeRemoteFileConnection(record);
      throw error;
    });
    return record.ready;
  }
  async withRemoteFiles(operation, {operationTimeoutMs, role = 'interactive', retireConnectionWhen = null,
    expectedIdentity = null, transportMetadata = false} = {}) {
    this.assertRemoteFileIdentity(expectedIdentity);
    const record = await this.acquireRemoteFileConnection({role, operationTimeoutMs, expectedIdentity});
    this.assertRemoteFileIdentity(expectedIdentity);
    if (record.state !== 'ready' || record.channelError)
      throw new RemoteFileError('REMOTE_SFTP_UNAVAILABLE', `SSH 文件通道异常：${record.channelError?.message || '通道已关闭。'}`, record.channelError);
    const startedAt = Date.now();
    const channelReused = record.operationCount > 0;
    record.operationCount += 1;
    let result;
    try { result = await operation(record.service, record); }
    catch (error) {
      // A timed-out or connection-level request can leave an SFTP wrapper in
      // an indeterminate state. Do not make every later Explorer action wait
      // for the same dead channel; the next action will establish a new one.
      if (record.channelError || remoteFileConnectionFailed(error)) {
        record.channelError ||= error;
        this.closeRemoteFileConnection(record);
      }
      throw error;
    }
    this.assertRemoteFileIdentity(expectedIdentity);
    if (record.channelError)
      throw new RemoteFileError('REMOTE_SFTP_UNAVAILABLE', `SSH 文件通道异常：${record.channelError.message}`, record.channelError);
    if (record.client !== this.client || record.generation !== this.generation || this.ssh.state !== 'connected')
      throw new RemoteFileError('REMOTE_CONNECTION_CLOSED', 'SSH 连接已断开，远程文件操作未完成。');
    if (retireConnectionWhen?.(result)) this.closeRemoteFileConnection(record);
    return transportMetadata && result && typeof result === 'object' && !Array.isArray(result)
      ? {...result, transport: 'sftp-fallback', channelReused,
        transportDurationMs: Date.now() - startedAt}
      : result;
  }
  async listRemoteFiles(value = {}) {
    await this.ready;
    const {params, identity} = this.prepareRemoteFileRequest(value);
    try { return await this.workspaceRequest({method: 'fs.list', params, expectedIdentity: identity}); }
    catch (error) {
      if (!error.workspaceTransportUnavailable && !workspaceTransportFailed(error))
        throw remoteFileErrorFromWorkspace(error);
      if (identity.client !== this.client || identity.generation !== this.generation
          || identity.root !== this.ssh.remoteRootPath)
        throw new RemoteFileError('REMOTE_CONNECTION_CHANGED', 'SSH 目标在远端目录请求期间发生变化，已拒绝在新目标上重放。');
      return this.withRemoteFiles(service => service.list(params), {
        expectedIdentity: identity, transportMetadata: true,
      });
    }
  }
  async prefetchRemoteFiles(value = {}) {
    await this.ready;
    const {params, identity} = this.prepareRemoteFileRequest(value);
    try {
      return await this.workspaceRequest({role: 'prefetch', method: 'fs.prefetch', params,
        timeoutMs: Math.min(35000, Math.max(5000, Number(params.timeBudgetMs || 15000) + 5000)),
        expectedIdentity: identity});
    } catch (error) {
      if (!error.workspaceTransportUnavailable && !workspaceTransportFailed(error))
        throw remoteFileErrorFromWorkspace(error);
    }
    if (identity.client !== this.client || identity.generation !== this.generation
        || identity.root !== this.ssh.remoteRootPath)
      throw new RemoteFileError('REMOTE_CONNECTION_CHANGED', 'SSH 目标在远端预取请求期间发生变化，已拒绝在新目标上重放。');
    return this.withRemoteFiles((service, record) => service.prefetchTree(params, {
      shouldContinue: () => record.client === this.client && record.generation === this.generation
        && this.ssh.state === 'connected' && !this.disposed,
    }), {
      operationTimeoutMs: 5000,
      role: 'prefetch',
      expectedIdentity: identity,
      transportMetadata: true,
      // Prefetch reports per-directory failures instead of throwing so a
      // partial tree remains useful. Retire only transport-failed channels,
      // while still returning that partial result to the renderer.
      retireConnectionWhen: result => result?.failures?.some(failure => remoteFileConnectionFailed({code: failure.code})),
    });
  }
  async readRemoteFile(value = {}) {
    await this.ready;
    const {params, identity} = this.prepareRemoteFileRequest(value);
    try { return await this.workspaceRequest({method: 'fs.readText', params, expectedIdentity: identity}); }
    catch (error) {
      if (!error.workspaceTransportUnavailable && !workspaceTransportFailed(error))
        throw remoteFileErrorFromWorkspace(error);
      if (identity.client !== this.client || identity.generation !== this.generation
          || identity.root !== this.ssh.remoteRootPath)
        throw new RemoteFileError('REMOTE_CONNECTION_CHANGED', 'SSH 目标在远端文件请求期间发生变化，已拒绝在新目标上重放。');
      return this.withRemoteFiles(service => service.read(params), {
        expectedIdentity: identity, transportMetadata: true,
      });
    }
  }
  async saveRemoteFile(value = {}) {
    await this.ready;
    const prepared = this.prepareRemoteFileRequest(value);
    const identity = prepared.identity;
    const params = {...prepared.params, create: prepared.params.expectedRevision === undefined};
    try { return await this.workspaceRequest({method: 'fs.writeText', params, expectedIdentity: identity}); }
    catch (error) {
      // Never replay an ambiguous write. Falling back is permitted only when
      // the Worker could not be acquired, before a write frame was sent.
      if (!error.workspaceTransportUnavailable) {
        const mapped = remoteFileErrorFromWorkspace(error);
        if (error?.workspaceRequestDispatched && (
          workspaceTransportFailed(error)
          || ['WORKSPACE_CONNECTION_CHANGED', 'WORKSPACE_RPC_CANCELLED'].includes(error?.code)
        )) mapped.commitUnknown = true;
        throw mapped;
      }
      if (identity.client !== this.client || identity.generation !== this.generation
          || identity.root !== this.ssh.remoteRootPath)
        throw new RemoteFileError('REMOTE_CONNECTION_CHANGED', 'SSH 目标在远端保存请求期间发生变化，已拒绝在新目标上重放。');
      if (!this.allowLegacySftpWriteFallback)
        throw new RemoteFileError('REMOTE_WORKSPACE_WRITE_UNAVAILABLE',
          '远程保存需要 Workspace Worker；请检查服务器 Python 配置或重新连接后重试。', error);
      return this.withRemoteFiles(service => service.save(params), {
        expectedIdentity: identity, transportMetadata: true,
      });
    }
  }
  async renameRemoteFile(value = {}) {
    await this.ready;
    const {params, identity} = this.prepareRemoteFileRequest(value);
    try { return await this.workspaceRequest({method: 'fs.rename', params, expectedIdentity: identity}); }
    catch (error) {
      // Mutations are never replayed through another transport: the response
      // may have been lost after the rename committed on the remote host.
      throw remoteFileErrorFromWorkspace(error);
    }
  }
  async copyRemoteFile(value = {}) {
    await this.ready;
    const {params, identity} = this.prepareRemoteFileRequest(value);
    try { return await this.workspaceRequest({method: 'fs.copy', params, timeoutMs: 120000,
      expectedIdentity: identity}); }
    catch (error) {
      // Like save/rename, an ambiguous copy must not be issued a second time.
      throw remoteFileErrorFromWorkspace(error);
    }
  }
  async uploadRemoteFilesFromLocal({localPaths, directory = '', expectedConnection = null} = {}) {
    await this.ready;
    if (!Array.isArray(localPaths) || !localPaths.length || localPaths.length > MAX_REMOTE_UPLOAD_FILES)
      throw new RemoteFileError('REMOTE_UPLOAD_INVALID', `一次只能上传 1–${MAX_REMOTE_UPLOAD_FILES} 个本地文件。`);
    const normalizedDirectory = normalizeRelativePath(directory);
    const identity = this.captureRemoteFileIdentity(expectedConnection);
    const sources = [];
    const failures = [];
    let acceptedBytes = 0;
    for (const value of localPaths) {
      let fallbackName = '未命名文件';
      try {
        if (typeof value === 'string' && value && !value.includes('\0')) fallbackName = path.basename(value) || fallbackName;
        const source = await captureLocalUploadSource(value);
        if (acceptedBytes + source.size > MAX_REMOTE_UPLOAD_TOTAL_BYTES) {
          throw new RemoteFileError('REMOTE_UPLOAD_TOTAL_TOO_LARGE', '本次上传文件总大小超过 64 GiB 上限。');
        }
        acceptedBytes += source.size;
        sources.push(source);
      } catch (error) {
        failures.push(publicRemoteUploadFailure(error, fallbackName));
      }
    }
    const uploaded = [];
    const uploadedNames = new Set();
    for (const source of sources) {
      if (uploadedNames.has(source.name)) {
        failures.push({
          name: source.name,
          code: 'REMOTE_UPLOAD_DUPLICATE_NAME',
          message: `本次上传包含多个名为“${source.name}”的文件；只上传了第一个，未覆盖同名文件。`,
        });
        continue;
      }
      uploadedNames.add(source.name);
      let acknowledged = null;
      try {
        await assertLocalUploadSource(source);
        const result = await this.withRemoteFiles(async service => {
          const staged = await service.stageLocalUpload({
            localPath: source.filename,
            directory: normalizedDirectory,
            name: source.name,
            expectedSize: source.size,
          });
          let published = false;
          try {
            await assertLocalUploadSource(source);
            let committed;
            try {
              committed = await this.workspaceRequest({
                role: 'upload',
                method: 'fs.rename',
                params: {
                  path: staged.temporaryPath,
                  newName: source.name,
                  expectedSize: source.size,
                },
                timeoutMs: 120000,
                expectedIdentity: identity,
              });
            } catch (error) {
              if (error?.code === 'WORKSPACE_FILE_EXISTS') {
                throw new RemoteFileError('REMOTE_FILE_EXISTS', `远程目录中已存在“${source.name}”，未覆盖原文件。`, error);
              }
              if (error?.workspaceRequestDispatched && (
                workspaceTransportFailed(error)
                || ['WORKSPACE_CONNECTION_CHANGED', 'WORKSPACE_RPC_CANCELLED'].includes(error?.code)
              )) {
                throw new RemoteFileError(
                  'REMOTE_UPLOAD_COMMIT_UNKNOWN',
                  `“${source.name}”的发布请求已发出，但响应丢失或 SSH 目标变化；请刷新原目录核对上传结果。`,
                  error,
                );
              }
              throw remoteFileErrorFromWorkspace(error);
            }
            if (String(committed?.path || '') !== staged.path || Number(committed?.size) !== source.size)
              throw new RemoteFileError('REMOTE_UPLOAD_INVALID_RESPONSE', '远程上传发布结果与本地文件不一致。');
            published = true;
            acknowledged = {name: source.name, path: staged.path, size: source.size};
            return acknowledged;
          } finally {
            if (!published) {
              try { await service.removeStagedUpload(staged.temporaryPath); }
              catch (cleanupError) { console.error('Remote upload temporary cleanup:', cleanupError.message); }
            }
          }
        }, {
          role: 'upload',
          operationTimeoutMs: REMOTE_UPLOAD_TIMEOUT_MS,
          expectedIdentity: identity,
        });
        uploaded.push(result);
      } catch (error) {
        // The Worker acknowledgement is authoritative. A connection switch in
        // withRemoteFiles' final SFTP fence can happen just after the no-clobber
        // publish completed; do not turn that confirmed mutation into a false
        // failure or retry it on the next SSH target.
        if (acknowledged) uploaded.push(acknowledged);
        else failures.push(publicRemoteUploadFailure(error, source.name));
      }
    }
    return {
      directory: normalizedDirectory,
      uploaded,
      failures,
      totalFiles: localPaths.length,
    };
  }
  async searchRemoteFiles(value = {}) {
    const requestId = String(value?.requestId || '');
    if (!/^[A-Za-z0-9._:-]{1,128}$/.test(requestId))
      throw new RemoteFileError('REMOTE_SEARCH_INVALID', '远程搜索请求 ID 无效。');
    if (this.pendingRemoteFileSearches.has(requestId))
      throw new RemoteFileError('REMOTE_SEARCH_INVALID', '相同的远程搜索请求正在执行。');
    const pending = {requestId, cancelled: false, error: null};
    pending.cancel = () => {
      if (pending.cancelled) return;
      pending.cancelled = true;
      pending.error = Object.assign(new Error('远程文件搜索已取消。'), {code: 'WORKSPACE_RPC_CANCELLED'});
      this.remoteWorkspaceConnections.get('explorer-search')?.cancel(requestId, pending.error);
    };
    this.pendingRemoteFileSearches.set(requestId, pending);
    try {
      await this.ready;
      const {params, identity} = this.prepareRemoteFileRequest(value);
      if (String(params.requestId || '') !== requestId)
        throw new RemoteFileError('REMOTE_SEARCH_INVALID', '远程搜索请求 ID 在准备期间发生变化。');
      return await this.workspaceRequest({
        role: 'explorer-search', method: 'fs.search', params, requestId,
        timeoutMs: Math.min(35000, Math.max(5000, Number(params.timeBudgetMs || 10000) + 5000)),
        expectedIdentity: identity,
        isCancelled: () => pending.cancelled,
        cancellationError: () => pending.error,
      });
    } catch (error) {
      throw remoteFileErrorFromWorkspace(error);
    } finally {
      if (this.pendingRemoteFileSearches.get(requestId) === pending)
        this.pendingRemoteFileSearches.delete(requestId);
    }
  }
  async cancelRemoteFileSearch({requestId} = {}) {
    const normalized = String(requestId || '');
    const pending = this.pendingRemoteFileSearches.get(normalized);
    if (!pending) return {requestId: normalized, cancelled: false};
    pending.cancel();
    return {requestId: normalized, cancelled: true};
  }
  async replaceRemoteFiles(value = {}) {
    await this.ready;
    const expectedConnection = value?.expectedConnection;
    // Capture and freeze the Explorer target before reading any search snapshot.
    this.captureRemoteFileIdentity(expectedConnection);
    const replacement = String(value?.replacement ?? '');
    if (Buffer.byteLength(replacement, 'utf8') > 1024 * 1024)
      throw new RemoteFileError('REMOTE_REPLACE_INVALID', '替换文本超过 1 MiB。');
    const expression = workspaceReplacementExpression(value);
    const snapshots = Array.isArray(value?.files) ? value.files : [];
    if (!snapshots.length || snapshots.length > 200)
      throw new RemoteFileError('REMOTE_REPLACE_INVALID', '批量替换需要 1–200 个搜索快照。');
    const uniqueSnapshots = new Map();
    for (const snapshot of snapshots) {
      const snapshotPath = String(snapshot?.path || '');
      const snapshotRevision = String(snapshot?.revision || '');
      if (!snapshotPath || snapshotPath.startsWith('/') || snapshotPath.includes('\\')
          || !/^sha256:[0-9a-f]{64}$/.test(snapshotRevision))
        throw new RemoteFileError('REMOTE_REPLACE_INVALID', '搜索快照缺少有效的相对路径或 revision。');
      const existing = uniqueSnapshots.get(snapshotPath);
      if (existing && existing.revision !== snapshotRevision)
        throw new RemoteFileError('REMOTE_REPLACE_INVALID', `“${snapshotPath}”包含冲突的搜索快照。`);
      uniqueSnapshots.set(snapshotPath, {...snapshot, path: snapshotPath, revision: snapshotRevision});
    }
    const prepared = [];
    for (const snapshot of uniqueSnapshots.values()) {
      const opened = await this.readRemoteFile({path: snapshot?.path, expectedConnection});
      if (opened.revision !== snapshot?.revision)
        throw new RemoteFileError('REMOTE_FILE_CHANGED', `“${snapshot?.path || ''}”在搜索后发生变化，未执行批量替换。`);
      expression.lastIndex = 0;
      const content = opened.content.replace(expression, () => replacement);
      if (content !== opened.content) prepared.push({snapshot, opened, content});
    }
    const saved = [];
    for (const item of prepared) {
      try {
        const result = await this.saveRemoteFile({
          path: item.snapshot.path,
          content: item.content,
          expectedRevision: item.opened.revision,
          bom: item.opened.bom,
          lineEnding: item.opened.lineEnding,
          expectedConnection,
        });
        saved.push({path: item.snapshot.path, revision: result.revision});
      } catch (error) {
        const commitUnknown = Boolean(error?.commitUnknown);
        const partial = new RemoteFileError(error?.code || 'REMOTE_REPLACE_FAILED',
          commitUnknown
            ? `${saved.length ? `已确认替换 ${saved.length} 个文件；` : ''}“${item.snapshot.path}”的写入请求已发出，但响应丢失或 SSH 目标变化，提交状态未知。已停止后续替换；请在原目标刷新该文件后核对。原因：${String(error?.message || '未知错误')}`
            : saved.length
              ? `已替换 ${saved.length} 个文件，处理“${item.snapshot.path}”时失败并停止；已保存的文件不会自动回退。原因：${String(error?.message || '未知错误')}`
              : String(error?.message || '批量替换失败。'), error);
        partial.commitUnknown = commitUnknown;
        partial.details = {saved, ...(commitUnknown ? {commitUnknown: {path: item.snapshot.path}} : {})};
        throw partial;
      }
    }
    return {replacedFiles: saved.length, saved, atomic: false};
  }
  async downloadRemoteFileToLocal({
    path: remotePath,
    destinationPath,
    expectedSize: approvedRemoteSize,
    expectedMtimeMs: approvedRemoteMtimeMs,
    expectedRevision: approvedRemoteRevision,
    expectedDestinationFingerprint,
    expectedConnection = null,
  } = {}) {
    await this.ready;
    const identity = this.captureRemoteFileIdentity(expectedConnection);
    const assertConnectionIdentity = () => {
      if (!identity.client || identity.client !== this.client || identity.generation !== this.generation
          || identity.root !== this.ssh.remoteRootPath || this.ssh.state !== 'connected') {
        throw new RemoteFileError(
          'REMOTE_CONNECTION_CHANGED',
          'SSH 目标在远程下载期间发生变化，已停止下载且不会在新目标上继续。',
        );
      }
    };
    const readChunk = async params => {
      assertConnectionIdentity();
      try {
        const result = await this.workspaceRequest({
          method: 'fs.readChunk', params, timeoutMs: 120000, expectedIdentity: identity,
        });
        assertConnectionIdentity();
        return result;
      } catch (error) {
        // Prefer the target-fence error even when the old channel reports a
        // concurrent transport failure. Never replay a chunk on a new target.
        assertConnectionIdentity();
        throw remoteFileErrorFromWorkspace(error);
      }
    };
    assertConnectionIdentity();
    const destination = String(destinationPath || '');
    if (!destination || destination.includes('\0') || !path.isAbsolute(destination))
      throw new RemoteFileError('REMOTE_LOCAL_PATH_INVALID', '本地下载目标必须是绝对文件路径。');
    const parent = path.dirname(destination);
    const filename = path.basename(destination);
    if (!filename || filename === '.' || filename === '..')
      throw new RemoteFileError('REMOTE_LOCAL_PATH_INVALID', '本地下载目标无效。');
    const parentStat = await fs.promises.stat(parent).catch(error => {
      throw new RemoteFileError('REMOTE_LOCAL_PATH_INVALID', `本地下载目录不可用：${error.message}`, error);
    });
    if (!parentStat.isDirectory())
      throw new RemoteFileError('REMOTE_LOCAL_PATH_INVALID', '本地下载目标的父路径不是目录。');
    const approvedDestination = expectedDestinationFingerprint
      || await captureLocalDestinationFingerprint(destination);
    if (approvedDestination.exists && (!approvedDestination.regularFile || approvedDestination.symbolicLink))
      throw new RemoteFileError('REMOTE_LOCAL_PATH_INVALID', '本地下载目标必须是普通文件，不能覆盖目录或符号链接。');
    const temporary = path.join(parent, `.${filename}.hetero-download-${crypto.randomUUID()}.part`);
    let handle = null;
    let committed = false;
    try {
      handle = await fs.promises.open(temporary, 'wx', 0o600);
      const digest = crypto.createHash('sha256');
      let offset = 0;
      let snapshot = '';
      let expectedSize = null;
      while (true) {
        let result;
        try {
          result = await readChunk({
            path: remotePath,
            offset,
            length: REMOTE_DOWNLOAD_CHUNK_BYTES,
            ...(offset === 0 && Number.isSafeInteger(approvedRemoteSize)
              ? {expectedSize: approvedRemoteSize} : {}),
            ...(offset === 0 && Number.isSafeInteger(approvedRemoteMtimeMs)
              ? {expectedMtimeMs: approvedRemoteMtimeMs} : {}),
            ...(offset === 0 && typeof approvedRemoteRevision === 'string'
              ? {expectedRevision: approvedRemoteRevision} : {}),
            ...(snapshot ? {expectedSnapshot: snapshot} : {}),
          });
        } catch (error) {
          throw remoteFileErrorFromWorkspace(error);
        }
        const encoded = result?.contentBase64;
        const size = Number(result?.size);
        const bytesRead = Number(result?.bytesRead);
        if (!Number.isSafeInteger(size) || size < 0 || size > MAX_REMOTE_DOWNLOAD_BYTES)
          throw new RemoteFileError('REMOTE_DOWNLOAD_TOO_LARGE', '远程文件大小无效或超过 32 GiB 下载上限。');
        if (!Number.isSafeInteger(bytesRead) || bytesRead < 0 || bytesRead > REMOTE_DOWNLOAD_CHUNK_BYTES
            || Number(result?.offset) !== offset
            || typeof encoded !== 'string'
            || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(encoded)
            || !/^snapshot:[0-9a-f]{64}$/.test(String(result?.snapshot || '')))
          throw new RemoteFileError('REMOTE_WORKSPACE_INVALID_RESPONSE', '远端下载返回了无效的分块响应。');
        const chunk = Buffer.from(encoded, 'base64');
        if (chunk.length !== bytesRead)
          throw new RemoteFileError('REMOTE_WORKSPACE_INVALID_RESPONSE', '远端下载分块长度不匹配。');
        if (!snapshot) {
          snapshot = result.snapshot;
          expectedSize = size;
        } else if (result.snapshot !== snapshot || size !== expectedSize) {
          throw new RemoteFileError('REMOTE_FILE_CHANGED', '远程文件已在下载期间发生变化，请重新下载。');
        }
        let written = 0;
        while (written < chunk.length) {
          const write = await handle.write(chunk, written, chunk.length - written, offset + written);
          if (!Number.isInteger(write.bytesWritten) || write.bytesWritten <= 0)
            throw new RemoteFileError('REMOTE_LOCAL_WRITE_FAILED', '本地下载文件写入未取得进展。');
          written += write.bytesWritten;
        }
        digest.update(chunk);
        offset += chunk.length;
        if (result.eof) {
          if (offset !== expectedSize)
            throw new RemoteFileError('REMOTE_WORKSPACE_INVALID_RESPONSE', '远端下载提前结束，文件不完整。');
          break;
        }
        if (!chunk.length || offset >= expectedSize)
          throw new RemoteFileError('REMOTE_WORKSPACE_INVALID_RESPONSE', '远端下载分块没有前进。');
      }
      // Re-open and validate the same snapshot after the last data chunk. This
      // closes the mutation window between the final read and local commit.
      let verified;
      try {
        verified = await readChunk({
          path: remotePath, offset: expectedSize, length: 1, expectedSnapshot: snapshot,
        });
      } catch (error) {
        throw remoteFileErrorFromWorkspace(error);
      }
      if (verified?.snapshot !== snapshot || Number(verified?.size) !== expectedSize
          || Number(verified?.bytesRead) !== 0 || !verified?.eof)
        throw new RemoteFileError('REMOTE_FILE_CHANGED', '远程文件末次校验失败，请重新下载。');
      await handle.sync();
      await handle.close();
      handle = null;
      assertConnectionIdentity();
      await assertLocalDestinationFingerprint(destination, approvedDestination);
      await replaceDownloadedFile(temporary, destination, {
        expectedDestinationFingerprint: approvedDestination,
      });
      committed = true;
      return {downloaded: true, size: expectedSize, sha256: digest.digest('hex')};
    } catch (error) {
      if (handle) {
        try { await handle.close(); } catch {}
        handle = null;
      }
      throw error;
    } finally {
      if (!committed) {
        try { await fs.promises.unlink(temporary); } catch (error) {
          if (error?.code !== 'ENOENT') console.error('Remote download cleanup:', error.message);
        }
      }
    }
  }
  closePlatformRpcConnections(reason = new Error('SSH 连接已改变，服务器分析长驻通道已关闭。')) {
    const records = [...this.platformRpcConnections.values()];
    this.platformRpcConnections.clear();
    for (const record of records) record.close(reason);
  }
  async acquirePlatformRpcConnection({startupCommand, channelKey, expectedTarget} = {}) {
    await this.ready;
    if (this.ssh.state !== 'connected' || !this.client) throw new Error('请先连接 SSH 服务器。');
    const target = normalizeSshTarget(expectedTarget);
    if (!hasSshTarget(target) || !sameSshTarget(target, this.currentSshTarget()))
      throw new Error('SSH 目标已改变，已拒绝创建服务器分析长驻通道。');
    if (typeof startupCommand !== 'string' || !startupCommand.trim() || startupCommand.includes('\0')
        || Buffer.byteLength(startupCommand) > 32768)
      throw new Error('服务器分析长驻命令无效或超过 32 KiB。');
    const key = String(channelKey || crypto.createHash('sha256').update(startupCommand).digest('hex'));
    if (!/^[A-Za-z0-9._:-]{1,256}$/.test(key)) throw new Error('服务器分析长驻通道标识无效。');
    const existing = this.platformRpcConnections.get(key);
    if (existing && existing.client === this.client && existing.generation === this.generation && existing.state !== 'closed') {
      await existing.ready;
      return existing;
    }
    if (existing) existing.close(new Error('服务器分析长驻通道已更新。'));
    const client = this.client;
    const generation = this.generation;
    const record = new PlatformRpcChannel({
      client,
      generation,
      key,
      cwdPrefix: remotePrefix(this.ssh.cwd) + 'bash -c ',
      command: shellQuote(startupCommand),
    });
    this.platformRpcConnections.set(key, record);
    record.on('closed', () => {
      if (this.platformRpcConnections.get(key) === record) this.platformRpcConnections.delete(key);
    });
    await record.ready;
    if (client !== this.client || generation !== this.generation || this.ssh.state !== 'connected') {
      record.close(new Error('SSH 连接在服务器分析通道初始化期间发生变化。'));
      throw new Error('SSH 连接在服务器分析通道初始化期间发生变化。');
    }
    return record;
  }
  async platformRequest({startupCommand, channelKey, request, timeoutMs = 120000,
    expectedTarget = null, requestId = null, restartChannel = false, operationOrigin = 'platform',
    toolSessionId = null} = {}) {
    const normalizedRequestId = String(requestId || crypto.randomUUID());
    if (!/^[A-Za-z0-9._:-]{1,128}$/.test(normalizedRequestId)) throw new Error('平台请求 ID 无效。');
    if (!request || Array.isArray(request) || typeof request !== 'object') throw new Error('平台请求内容无效。');
    const requestJson = JSON.stringify(request);
    if (Buffer.byteLength(requestJson) > 12 * 1024 * 1024) throw new Error('平台请求超过 12 MiB。');
    if (this.pendingPlatformRequests.has(normalizedRequestId)) throw new Error('同一平台请求 ID 已在执行。');
    const baseChannelKey = String(channelKey || crypto.createHash('sha256').update(startupCommand).digest('hex'));
    if (!/^[A-Za-z0-9._:-]{1,220}$/.test(baseChannelKey)) throw new Error('服务器分析长驻通道标识无效。');
    const origin = operationOrigin === 'agent' ? 'agent' : 'platform';
    const effectiveChannelKey = `${baseChannelKey}:${origin === 'agent' ? 'agent' : 'page'}`;
    const pendingState = {requestId: normalizedRequestId, cancelled: false, record: null,
      toolSessionId: origin === 'agent' ? String(toolSessionId || '') : null};
    this.pendingPlatformRequests.set(normalizedRequestId, pendingState);
    const effectiveTimeoutMs = remoteExecutionTimeout(timeoutMs, false);
    const started = Date.now();
    const operationId = crypto.randomUUID();
    const operation = {kind: 'platform_rpc', origin, requestId: normalizedRequestId,
      path: String(request.path || ''), method: String(request.method || 'GET')};
    this.emit('event', {type: 'operation', target: 'platform', id: operationId, state: 'running', ...operation});
    try {
      await this.ready;
      if (pendingState.cancelled) throw new Error('服务器分析请求已取消。');
      if (restartChannel) this.closePlatformRpcConnections(new Error('服务器分析运行环境已更新，正在重建长驻通道。'));
      const record = await this.acquirePlatformRpcConnection({startupCommand, channelKey: effectiveChannelKey, expectedTarget});
      pendingState.record = record;
      if (pendingState.cancelled) throw new Error('服务器分析请求已取消。');
      if (origin === 'platform') this.activePlatformSpec = {
        startupCommand,
        channelKey: baseChannelKey,
        expectedTarget: normalizeSshTarget(expectedTarget),
      };
      const result = await record.request({
        requestId: normalizedRequestId,
        requestBase64: Buffer.from(requestJson, 'utf8').toString('base64'),
        timeoutMs: effectiveTimeoutMs,
      });
      if (!sameSshTarget(normalizeSshTarget(expectedTarget), this.currentSshTarget()))
        throw new Error('SSH 目标在平台请求期间发生变化，已丢弃旧目标返回结果。');
      this.emit('event', {type: 'operation', target: 'platform', id: operationId, state: 'completed',
        ...operation, ...result, durationMs: Date.now() - started});
      return {...result, cwd: this.ssh.cwd};
    } catch (error) {
      if (error?.remoteResult) {
        const result = {...error.remoteResult, cwd: this.ssh.cwd};
        this.emit('event', {type: 'operation', target: 'platform', id: operationId, state: 'failed',
          ...operation, ...result, durationMs: Date.now() - started});
        return result;
      }
      this.emit('event', {type: 'operation', target: 'platform', id: operationId, state: 'error',
        ...operation, message: error.message, durationMs: Date.now() - started});
      throw error;
    } finally {
      if (this.pendingPlatformRequests.get(normalizedRequestId) === pendingState)
        this.pendingPlatformRequests.delete(normalizedRequestId);
    }
  }
  async cancelPlatformRequest({requestId, expectedTarget = null} = {}) {
    const normalizedId = String(requestId || '');
    if (!/^[A-Za-z0-9._:-]{1,128}$/.test(normalizedId)) throw new Error('平台请求 ID 无效。');
    const earlyPending = this.pendingPlatformRequests.get(normalizedId);
    if (earlyPending) {
      earlyPending.cancelled = true;
      earlyPending.record?.cancel(normalizedId);
      return {requestId: normalizedId, cancelled: true};
    }
    await this.ready;
    if (expectedTarget !== null) {
      const target = normalizeSshTarget(expectedTarget);
      if (!hasSshTarget(target) || !sameSshTarget(target, this.currentSshTarget()))
        throw new Error('SSH 目标已改变，无法取消另一目标上的平台请求。');
    }
    const pending = this.pendingPlatformRequests.get(normalizedId);
    if (pending) {
      pending.cancelled = true;
      pending.record?.cancel(normalizedId);
      return {requestId: normalizedId, cancelled: true};
    }
    for (const record of this.platformRpcConnections.values()) {
      if (record.cancel(normalizedId)) return {requestId: normalizedId, cancelled: true};
    }
    return {requestId: normalizedId, cancelled: false};
  }
  async applicationFlowTool(method, params = {}) {
    const actionId = APPLICATION_FLOW_TOOLS[method];
    if (!actionId) throw new Error('未知的应用流程平台工具。');
    const status = this.toolStatus();
    const expectedTarget = normalizeSshTarget(status.boundTarget);
    const taskContext = clone(this.toolSession.taskContext || {});
    if (taskContext.task_kind !== 'application_flow_generation' || !taskContext.application_flow_analysis_id)
      throw new Error('当前 Code Agent 任务没有绑定应用流程分析上下文；请从“应用流程”页面发起流程重建或校准。');
    const trustedInput = {...params, application_flow_analysis_id: taskContext.application_flow_analysis_id};
    for (const key of ['source_connector_id', 'source_snapshot_id', 'subpath', 'graph_id',
      'trace_analysis_artifact_id', 'analysis_artifact_id']) {
      if (taskContext[key] !== undefined && taskContext[key] !== null && taskContext[key] !== '') trustedInput[key] = taskContext[key];
    }
    if (Array.isArray(taskContext.source_scopes)) trustedInput.source_scopes = taskContext.source_scopes;
    const spec = this.activePlatformSpec;
    if (!spec || !sameSshTarget(spec.expectedTarget, expectedTarget))
      throw new Error('应用流程平台通道尚未初始化；请先打开“应用流程”页面并等待代码仓与流程目录加载完成。');
    const requestId = `agent-flow-${crypto.randomUUID()}`;
    const result = await this.platformRequest({
      startupCommand: spec.startupCommand,
      channelKey: spec.channelKey,
      expectedTarget,
      requestId,
      timeoutMs: 10 * 60 * 1000,
      operationOrigin: 'agent',
      toolSessionId: this.toolSession.id,
      request: {
        schema_version: 'desktop_platform_rpc/v2',
        method: 'POST',
        path: '/v1/application-flows/agent-action',
        body: {action_id: actionId, input: trustedInput, context: taskContext},
      },
    });
    return decodePlatformRpcResult(result);
  }
  currentSshTarget() {
    if (this.ssh.state !== 'connected' || !this.client) return null;
    return normalizeSshTarget(this.ssh);
  }
  sameSshTarget(a, b) {
    return sameSshTarget(a, b);
  }
  rotateToolSession() {
    this.abandonOpenWorkbenchRecovery('Agent 工具会话已结束，本次自动恢复已中止。');
    const previousId = this.toolSession.id;
    this.brokerToken = crypto.randomBytes(32).toString('hex');
    this.toolSession = {id: crypto.randomUUID(), boundTarget: null, taskContext: null, taskContract: null};
    for (const cancel of [...this.pendingExecs]) if (cancel.toolSessionId === previousId) cancel(new Error('Agent 工具会话已结束，远程执行通道已关闭。'));
    for (const pending of [...this.pendingWorkspaceRequests]) {
      if (pending.toolSessionId !== previousId) continue;
      pending.cancel(Object.assign(new Error('Agent 工具会话已结束，远程文件操作已取消。'), {code: 'WORKSPACE_RPC_CANCELLED'}));
    }
    for (const pending of this.pendingPlatformRequests.values()) {
      if (pending.toolSessionId !== previousId) continue;
      pending.cancelled = true;
      pending.record?.cancel(pending.requestId, new Error('Agent 工具会话已结束，应用流程操作已取消。'));
    }
  }
  toolStatus() {
    const currentTarget = this.currentSshTarget();
    if (!this.toolSession.boundTarget && currentTarget) this.toolSession.boundTarget = currentTarget;
    const targetMismatch = Boolean(currentTarget && this.toolSession.boundTarget && !this.sameSshTarget(currentTarget, this.toolSession.boundTarget));
    if (!targetMismatch) this.prewarmAgentWorkspaceConnection();
    return {...clone(this.ssh), toolSessionId: this.toolSession.id, currentTarget, boundTarget: clone(this.toolSession.boundTarget), targetMismatch, ...(targetMismatch ? {message: 'SSH 目标已改变，请重启 Agent 以绑定新连接。'} : {})};
  }
  capturePlanRefinementContract() {
    const context = this.toolSession.taskContext;
    if (context?.task_kind !== 'capture_plan_refinement') return null;
    const identity = {
      planId: String(context.capture_plan_id || ''),
      requestId: String(context.capture_plan_request_id || ''),
      expectedUpdatedAt: String(context.capture_plan_expected_updated_at || ''),
    };
    const existing = this.toolSession.taskContract;
    if (existing?.kind === 'capture_plan_refinement'
      && existing.planId === identity.planId && existing.requestId === identity.requestId
      && existing.expectedUpdatedAt === identity.expectedUpdatedAt) return existing;
    const contract = {kind: 'capture_plan_refinement', ...identity, state: 'open'};
    this.toolSession.taskContract = contract;
    return contract;
  }
  beginCapturePlanRefinement(contract, params) {
    if (contract.state !== 'open') throw new Error('本次计划补全请求已经结束，不能再次调用工具；请从页面重新发起。');
    // Claim the single invocation before any validation or asynchronous work.
    // A malformed or stale invocation therefore cannot race a second update.
    contract.state = 'running';
    const missingContext = !contract.planId || !contract.requestId || !contract.expectedUpdatedAt;
    if (missingContext) throw new Error('计划补全任务上下文不完整，请从页面重新发起。');
    if (!params || typeof params !== 'object' || Array.isArray(params)
      || params.planId !== contract.planId || params.requestId !== contract.requestId
      || params.expectedUpdatedAt !== contract.expectedUpdatedAt)
      throw new Error('计划补全工具参数与页面请求不匹配；本次结果未写入，请从页面重新发起。');
    return contract;
  }
  workbenchRecoveryContract() {
    const context = this.toolSession.taskContext;
    if (context?.task_kind !== 'workbench_error_recovery') return null;
    const identity = {
      requestId: typeof context.repair_request_id === 'string' ? context.repair_request_id : '',
      operationKey: String(context.recovery_operation_key || '').trim(),
      operationAttemptId: String(context.operation_attempt_id || '').trim(),
      selectedDevice: String(context.selected_device || '').trim(),
      capturePlanId: String(context.capture_plan_id || '').trim(),
      capturePlanExpectedUpdatedAt: String(context.capture_plan_expected_updated_at || '').trim(),
      authorizationScopes: [...new Set((Array.isArray(context.recovery_authorization_scopes)
        ? context.recovery_authorization_scopes : [])
        .map(value => String(value || '').trim()).filter(value => RECOVERY_SCOPES.has(value)))].sort(),
    };
    const existing = this.toolSession.taskContract;
    if (existing?.kind === 'workbench_error_recovery'
      && existing.requestId === identity.requestId
      && existing.operationKey === identity.operationKey
      && existing.operationAttemptId === identity.operationAttemptId
      && existing.selectedDevice === identity.selectedDevice
      && existing.capturePlanId === identity.capturePlanId
      && existing.capturePlanExpectedUpdatedAt === identity.capturePlanExpectedUpdatedAt
      && JSON.stringify(existing.authorizationScopes) === JSON.stringify(identity.authorizationScopes)) return existing;
    if (!identity.requestId || identity.requestId !== identity.requestId.trim() || identity.requestId.length > 200)
      throw new Error('自动恢复任务缺少有效的 repairRequestId，请从错误通知重新发起。');
    if (!identity.operationKey || identity.operationKey.length > 200)
      throw new Error('自动恢复任务缺少失败操作标识，请从错误通知重新发起。');
    if (!identity.operationAttemptId || identity.operationAttemptId.length > 200)
      throw new Error('自动恢复任务缺少独立执行编号，不能安全重试。');
    const contract = {
      kind: 'workbench_error_recovery', ...identity,
      boundTarget: clone(this.currentSshTarget()),
      agentBoundTarget: clone(this.toolSession.boundTarget),
      state: 'open', completion: null, planUpdateState: 'open', updatedPlan: null,
    };
    this.toolSession.taskContract = contract;
    return contract;
  }
  recoveryScopeAllowed(contract, scope) {
    const automation = normalizeAutomationSettings(this.settings.automation);
    if (!automation.autoRecoverErrors || !contract.authorizationScopes.includes(scope)) return false;
    return scope === 'device' ? automation.allowDeviceOperations : automation.allowServerOperations;
  }
  assertRecoverySshTarget(contract) {
    const current = this.currentSshTarget();
    if (!contract.boundTarget || !current || !sameSshTarget(contract.boundTarget, current))
      throw new Error('自动恢复绑定的 SSH 目标已改变；本次恢复不能访问新的服务器，请重新发起原操作。');
    if (contract.agentBoundTarget && !sameSshTarget(contract.agentBoundTarget, current))
      throw new Error('Agent 会话绑定的 SSH 目标与自动恢复目标不一致，请重新启动 Agent 后重试。');
  }
  assertRecoveryDeviceTarget(contract, params = {}) {
    if (!contract.selectedDevice) throw new Error('自动恢复没有绑定用户选中的手机，不能执行手机变更。');
    const snapshot = this.capture.getState();
    if (snapshot.selectedDeviceSerial !== contract.selectedDevice)
      throw new Error('用户选中的手机已改变；本次自动恢复不能操作另一台设备。');
    if (params.runId) {
      const run = snapshot.runs.find(item => item.runId === params.runId);
      if (run && run.deviceSerial !== contract.selectedDevice)
        throw new Error('采集任务不属于本次自动恢复绑定的手机。');
    }
  }
  assertWorkbenchRecoveryTool(contract, method, params = {}) {
    method = String(method || '');
    if (contract.state === 'completed' && method !== 'desktop_recovery_complete')
      throw new Error('本次自动恢复请求已经结束，不能继续调用工具。');
    if (method === 'capture_start')
      throw new Error('自动恢复任务不能直接调用 capture_start 重放原步骤；请完成修复并调用 desktop_recovery_complete，由工作台按冻结参数重试一次。');
    if (method === 'capture_save_plan')
      throw new Error('自动恢复任务不能新建或替换采集计划；只能按冻结的 planId 与 revision 调用一次 capture_update_plan。');
    const serverTool = method === 'remote_status' || method.startsWith('remote_')
      || method.startsWith('workspace_') || method.startsWith('application_flow_');
    if (RECOVERY_SERVER_MUTATIONS.has(method) && !this.recoveryScopeAllowed(contract, 'server'))
      throw new Error('本次自动恢复未获服务器操作授权，不能变更服务器或远程工作区。');
    if (serverTool && method !== 'remote_status') this.assertRecoverySshTarget(contract);
    if (RECOVERY_DEVICE_MUTATIONS.has(method)) {
      if (!this.recoveryScopeAllowed(contract, 'device'))
        throw new Error('本次自动恢复未获手机操作授权，不能变更设备状态。');
      this.assertRecoveryDeviceTarget(contract, params);
    }
  }
  beginRecoveryCapturePlanUpdate(contract, params = {}) {
    if (contract.planUpdateState !== 'open')
      throw new Error('本次自动恢复的采集计划修复已经执行或结束，不能再次更新。');
    if (!contract.capturePlanId || !contract.capturePlanExpectedUpdatedAt)
      throw new Error('自动恢复没有冻结原采集计划及版本，不能修改计划。');
    if (!params || Array.isArray(params) || typeof params !== 'object'
        || params.planId !== contract.capturePlanId
        || params.expectedUpdatedAt !== contract.capturePlanExpectedUpdatedAt
        || params.requestId !== contract.requestId)
      throw new Error('capture_update_plan 必须使用自动恢复冻结的 planId、revision 和 repairRequestId。');
    contract.planUpdateState = 'running';
    return contract;
  }
  finishRecoveryCapturePlanUpdate(contract, result) {
    if (contract.state !== 'open' || contract.planUpdateState !== 'running')
      throw new Error('自动恢复任务已结束，采集计划修复结果不再用于重试。');
    const planId = String(result?.planId || '');
    const updatedAt = String(result?.updatedAt || '');
    if (planId !== contract.capturePlanId || !updatedAt)
      throw new Error('采集计划修复没有返回可审计的新版本。');
    contract.planUpdateState = 'completed';
    contract.updatedPlan = {planId, updatedAt};
  }
  completeWorkbenchRecovery(contract, params = {}) {
    if (!params || Array.isArray(params) || typeof params !== 'object')
      throw new Error('自动恢复完成握手参数无效。');
    const keys = Object.keys(params);
    if (keys.some(key => !['repairRequestId', 'outcome', 'summary'].includes(key)))
      throw new Error('自动恢复完成握手包含未知字段。');
    if (typeof params.repairRequestId !== 'string' || typeof params.outcome !== 'string'
        || typeof params.summary !== 'string') throw new Error('自动恢复完成握手字段类型无效。');
    const repairRequestId = params.repairRequestId;
    const outcome = params.outcome;
    const summary = params.summary.trim();
    if (repairRequestId !== contract.requestId)
      throw new Error('完成握手的 repairRequestId 与当前自动恢复请求不匹配。');
    if (!['repaired', 'unable'].includes(outcome))
      throw new Error('自动恢复 outcome 只能是 repaired 或 unable。');
    if (!summary || summary.length > 2000) throw new Error('自动恢复摘要需要是 1 至 2000 个字符。');
    if (contract.planUpdateState === 'running') throw new Error('采集计划修复仍在执行，不能提前完成自动恢复。');
    if (contract.completion) {
      if (contract.completion.outcome !== outcome || contract.completion.summary !== summary)
        throw new Error('本次自动恢复请求已经以另一结果结束，不能覆盖完成结果。');
      return {...contract.completion.receipt, duplicate: true};
    }
    const receipt = {
      repairRequestId: contract.requestId, outcome, summary,
      operationKey: contract.operationKey,
      operationAttemptId: contract.operationAttemptId,
      ...(contract.capturePlanId ? {
        retryPlanId: contract.updatedPlan?.planId || contract.capturePlanId,
        retryPlanRevision: contract.updatedPlan?.updatedAt || contract.capturePlanExpectedUpdatedAt,
      } : {}),
      completed: true, duplicate: false,
    };
    contract.state = 'completed';
    contract.completion = {outcome, summary, receipt};
    this.emit('event', {
      type: 'agent-recovery', requestId: contract.requestId, repairRequestId: contract.requestId,
      outcome, summary, operationKey: contract.operationKey,
      operationAttemptId: contract.operationAttemptId,
      ...(contract.capturePlanId ? {
        retryPlanId: contract.updatedPlan?.planId || contract.capturePlanId,
        retryPlanRevision: contract.updatedPlan?.updatedAt || contract.capturePlanExpectedUpdatedAt,
      } : {}),
    });
    return receipt;
  }
  abandonOpenWorkbenchRecovery(summary = '自动恢复任务已被新的 Agent 任务替换。') {
    const contract = this.toolSession.taskContract;
    if (contract?.kind !== 'workbench_error_recovery' || contract.state !== 'open') return false;
    if (contract.planUpdateState === 'running') contract.planUpdateState = 'abandoned';
    this.completeWorkbenchRecovery(contract, {
      repairRequestId: contract.requestId, outcome: 'unable', summary,
    });
    return true;
  }
  serverProfileForTarget(target) {
    const key = serverProfileTargetKey(target);
    return key && this.settings.serverProfiles[key]
      ? clone(this.settings.serverProfiles[key])
      : normalizeServerProfile();
  }
  serverProfileScopeId(target) {
    const key = serverProfileTargetKey(target);
    return key ? crypto.createHash('sha256').update(key, 'utf8').digest('hex').slice(0, 16) : 'unscoped';
  }
  syncActiveServerProfile() {
    const target = hasSshTarget(this.settings.ssh) ? normalizeSshTarget(this.settings.ssh) : null;
    this.settings.serverProfileTarget = target;
    this.settings.serverProfile = target ? this.serverProfileForTarget(target) : normalizeServerProfile(this.settings.serverProfile);
  }
  async saveSettings({ssh, agent, serverProfile, serverProfileTarget, hdcPath, automation} = {}) {
    await this.ready;
    const nextSsh = ssh ? publicSsh({...this.settings.ssh, ...ssh}) : this.settings.ssh;
    if (ssh) {
      const password = typeof ssh.password === 'string' ? ssh.password : '';
      if (nextSsh.auth === 'password' && password) {
        if (!this.credentialStore) throw new Error('当前运行环境未提供系统安全存储，无法安全保存 SSH 密码。');
        await this.credentialStore.setSshPassword(nextSsh, password);
      }
    }
    if (agent) {
      const normalized = normalizeAgent({...this.settings.agent, ...agent});
      // Credentials may be injected at launch through the environment, but are
      // never persisted with desktop preferences. Environment references remain.
      normalized.environment = Object.fromEntries(Object.entries(normalized.environment).filter(([key, val]) => !/(TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY|AUTHORIZATION|ACCESS_KEY|^AK$|^SK$)/i.test(key) || /^\{env:[A-Za-z_][A-Za-z0-9_]*\}$/.test(val)));
      this.settings.agent = normalized;
    }
    if (ssh && this.legacyServerProfilePending && hasSshTarget(nextSsh)) {
      const key = serverProfileTargetKey(nextSsh);
      if (!this.settings.serverProfiles[key]) this.settings.serverProfiles[key] = normalizeServerProfile(this.settings.serverProfile);
      this.legacyServerProfilePending = false;
    }
    if (serverProfile) {
      const explicitTarget = serverProfileTarget === undefined || serverProfileTarget === null
        ? null : normalizeSshTarget(serverProfileTarget);
      if (serverProfileTarget !== undefined && serverProfileTarget !== null && !hasSshTarget(explicitTarget)) throw new Error('服务器环境配置缺少有效的 SSH 目标作用域。');
      const target = explicitTarget || (hasSshTarget(nextSsh) ? normalizeSshTarget(nextSsh) : null);
      if (target) {
        const key = serverProfileTargetKey(target);
        this.settings.serverProfiles[key] = normalizeServerProfile({...this.serverProfileForTarget(target), ...serverProfile});
      } else {
        // Compatibility for a not-yet-configured first target. It is migrated
        // once that target is saved, and is never used as another target's base.
        this.settings.serverProfile = normalizeServerProfile({...this.settings.serverProfile, ...serverProfile});
        this.legacyServerProfilePending = true;
      }
    }
    if (ssh) this.settings.ssh = nextSsh;
    this.syncActiveServerProfile();
    if (hdcPath !== undefined) this.settings.hdcPath = String(hdcPath || 'hdc');
    if (automation !== undefined) this.settings.automation = normalizeAutomationSettings(automation);
    const filename = path.join(this.userDataDir, 'settings.json');
    const persistedSettings = {
      ...this.settings,
      ...(this.legacyServerProfilePending ? {serverProfileLegacyPending: true} : {}),
    };
    fs.writeFileSync(`${filename}.tmp`, `${JSON.stringify(persistedSettings, null, 2)}\n`, {mode: 0o600});
    fs.renameSync(`${filename}.tmp`, filename);
    if (serverProfile || ssh) {
      this.activePlatformSpec = null;
      // Always clear the per-role startup failure cache as well. A user who
      // fixes pythonBin must be able to retry immediately instead of waiting
      // for a stale cooldown recorded by the previous profile.
      this.closeRemoteWorkspaceConnections(new Error('SSH 或服务器环境配置已更新，远端 Workspace 通道已关闭。'));
      if (this.platformRpcConnections.size)
        this.closePlatformRpcConnections(new Error('SSH 或服务器环境配置已更新，分析长驻通道已关闭。'));
    }
    return this.getState();
  }
  serverProfileOperation(value = {}) {
    const wrapped = value && !Array.isArray(value) && typeof value === 'object' && (Object.hasOwn(value, 'profile') || Object.hasOwn(value, 'target'));
    const supplied = value && !Array.isArray(value) && typeof value === 'object' && value.profile
      ? value.profile : value;
    const requestedTarget = wrapped && value.target !== undefined && value.target !== null ? normalizeSshTarget(value.target) : null;
    if (wrapped && value.target !== undefined && value.target !== null && !hasSshTarget(requestedTarget)) throw new Error('服务器环境操作缺少有效的 SSH 目标。');
    const currentTarget = this.currentSshTarget();
    if (requestedTarget && currentTarget && !sameSshTarget(requestedTarget, currentTarget)) throw new Error('SSH 目标已改变，已拒绝在当前服务器使用其他目标的环境配置。');
    const settingsTarget = hasSshTarget(this.settings.serverProfileTarget) ? normalizeSshTarget(this.settings.serverProfileTarget)
      : hasSshTarget(this.settings.ssh) ? normalizeSshTarget(this.settings.ssh) : null;
    const target = currentTarget || requestedTarget || settingsTarget;
    const base = target ? this.serverProfileForTarget(target) : this.settings.serverProfile;
    return {profile: normalizeServerProfile({...base, ...(supplied || {})}), target, connectedTargetRequired: Boolean(currentTarget)};
  }
  assertServerProfileTarget(operation) {
    if (!operation.connectedTargetRequired) return;
    const current = this.currentSshTarget();
    if (!current || !sameSshTarget(operation.target, current)) throw new Error('SSH 目标在服务器环境操作期间发生变化，已停止后续写入。');
  }
  async inspectServerProfile(value = {}) {
    await this.ready;
    const operation = this.serverProfileOperation(value);
    const {profile} = operation;
    const scopeId = this.serverProfileScopeId(operation.target);
    const result = await this.executeRemote({command: buildServerProfileInspectScript(profile, {scopeId}), timeoutMs: 120000, expectedTarget: operation.target});
    if (result.exitCode !== 0) throw new Error(`服务器环境检查失败${result.stderr.trim() ? `：${result.stderr.trim()}` : `（exit ${result.exitCode}）`}`);
    this.assertServerProfileTarget(operation);
    return parseServerProfileInspect(result.stdout, profile);
  }
  async prepareServerProfile(value = {}) {
    await this.ready;
    const operation = this.serverProfileOperation(value);
    const {profile, target} = operation;
    const scopeId = this.serverProfileScopeId(target);
    const preparedResult = await this.executeRemote({command: buildServerProfilePrepareScript(profile, {scopeId}), timeoutMs: 120000, expectedTarget: target});
    if (preparedResult.exitCode !== 0) throw new Error(`服务器环境初始化失败${preparedResult.stderr.trim() ? `：${preparedResult.stderr.trim()}` : `（exit ${preparedResult.exitCode}）`}`);
    this.assertServerProfileTarget(operation);
    const preparation = parseServerProfilePrepare(preparedResult.stdout, profile);
    await this.saveSettings({serverProfile: profile, serverProfileTarget: target});
    this.assertServerProfileTarget(operation);
    const inspectedResult = await this.executeRemote({command: buildServerProfileInspectScript(profile, {scopeId}), timeoutMs: 120000, expectedTarget: target});
    if (inspectedResult.exitCode !== 0) return {
      schemaVersion: preparation.schemaVersion,
      profile: clone(profile),
      managedRoot: preparation.managedRoot,
      managedPaths: clone(preparation.managedPaths),
      checks: [],
      summary: null,
      preparation,
      inspectionError: `平台托管目录和 platform.env 已初始化并保存，但随后的只读能力复检失败${inspectedResult.stderr.trim() ? `：${inspectedResult.stderr.trim()}` : `（exit ${inspectedResult.exitCode}）`}。请点击“检查环境”重试；无需重复初始化。`,
    };
    this.assertServerProfileTarget(operation);
    return {...parseServerProfileInspect(inspectedResult.stdout, profile), preparation};
  }
  async ensureServerProfile(value = {}) {
    await this.ready;
    const operation = this.serverProfileOperation(value);
    const {profile, target} = operation;
    if (!profile.workerRepoRoot) {
      throw new Error('服务器分析环境尚未配置异构工作仓目录。请在“连接配置 → 服务器环境”填写异构工作仓目录并保存，工作台会在首次分析时自动初始化托管目录。');
    }
    const targetKey = serverProfileTargetKey(target) || 'unscoped';
    const profileKey = crypto.createHash('sha256').update(JSON.stringify(profile), 'utf8').digest('hex');
    const initializationKey = `${targetKey}\n${profileKey}`;
    const existing = this.pendingServerProfileInitializations.get(initializationKey);
    if (existing) return existing;
    const pending = this.prepareServerProfile({profile, target}).finally(() => {
      if (this.pendingServerProfileInitializations.get(initializationKey) === pending)
        this.pendingServerProfileInitializations.delete(initializationKey);
    });
    this.pendingServerProfileInitializations.set(initializationKey, pending);
    return pending;
  }
  async ensureRemoteAnalysisWorker(value = {}) {
    await this.ready;
    const operation = this.serverProfileOperation(value);
    const {profile, target} = operation;
    if (!target || !this.currentSshTarget() || !sameSshTarget(target, this.currentSshTarget()))
      throw new Error('自动部署分析 Worker 需要当前 SSH 连接。');
    const scopeId = this.serverProfileScopeId(target);
    const bundle = loadAnalysisWorkerBundle({resourcesDir: this.resourcesDir, desktopVersion: this.version});
    const profileFingerprint = crypto.createHash('sha256').update(JSON.stringify(profile), 'utf8').digest('hex');
    const deploymentKey = `${serverProfileTargetKey(target)}\n${scopeId}\n${profileFingerprint}\n${bundle.manifest.identity}`;
    const existing = this.pendingAnalysisWorkerDeployments.get(deploymentKey);
    if (existing) return existing;
    const pending = (async () => {
      const rootResult = await this.executeRemote({
        command: buildAnalysisWorkerRootScript({scopeId, profileId: profile.profileId}),
        timeoutMs: 30000, expectedTarget: target,
      });
      if (rootResult.exitCode !== 0)
        throw new Error(`无法准备分析 Worker 托管目录${rootResult.stderr.trim() ? `：${rootResult.stderr.trim()}` : ''}`);
      this.assertServerProfileTarget(operation);
      const baseRoot = parseAnalysisWorkerRoot(rootResult.stdout, {scopeId, profileId: profile.profileId});
      const finalRoot = `${baseRoot}/${bundle.manifest.identity}`;
      const inspection = await this.executeRemote({
        command: buildAnalysisWorkerInspectScript({root: finalRoot, identity: bundle.manifest.identity, pythonBin: profile.pythonBin}),
        timeoutMs: 120000, expectedTarget: target,
      });
      if (inspection.exitCode !== 0)
        throw new Error(`无法检查托管分析 Worker${inspection.stderr.trim() ? `：${inspection.stderr.trim()}` : ''}`);
      const workerStatus = parseAnalysisWorkerStatus(inspection.stdout);
      if (workerStatus.status === 'READY') {
        return {schemaVersion: bundle.manifest.schema_version, version: bundle.manifest.version,
          identity: bundle.manifest.identity, root: finalRoot, reused: true};
      }
      if (!['ABSENT', 'CORRUPT'].includes(workerStatus.status)) {
        const labels = {
          PYTHON_UNAVAILABLE: '配置的 Python 不可执行', PYTHON_UNSUPPORTED: 'Python 版本低于 3.10',
          DEPENDENCY_UNAVAILABLE: 'Python 依赖无法导入', SCHEMA_MISMATCH: 'Desktop RPC 契约版本不兼容',
        };
        const error = new Error(`分析 Worker 文件已就绪，但${labels[workerStatus.status] || '运行环境不可用'}${workerStatus.message ? `：${workerStatus.message}` : ''}`);
        error.code = `DESKTOP_ANALYSIS_WORKER_${workerStatus.status}`;
        throw error;
      }

      const token = crypto.randomBytes(12).toString('hex');
      const stagingRoot = `${baseRoot}/.partial-${bundle.manifest.identity.slice(0, 16)}-${token}`;
      let stagingCreated = false;
      try {
        const prepared = await this.executeRemote({
          command: buildAnalysisWorkerPrepareScript({baseRoot, stagingRoot, files: bundle.files}),
          timeoutMs: 30000, expectedTarget: target,
        });
        if (prepared.exitCode !== 0)
          throw new Error(`无法创建分析 Worker 暂存目录${prepared.stderr.trim() ? `：${prepared.stderr.trim()}` : ''}`);
        stagingCreated = true;
        const uploads = [...bundle.files, {
          path: 'analysis-worker-manifest.json', filename: bundle.manifestPath,
          size: fs.statSync(bundle.manifestPath).size,
        }];
        await this.withSftpTransfer(target, async sftp => {
          for (const file of uploads) {
            this.assertServerProfileTarget(operation);
            const remotePath = `${stagingRoot}/${file.path}`;
            await new Promise((resolve, reject) => {
              let settled = false;
              const finish = error => {
                if (settled) return;
                settled = true; clearTimeout(timer);
                if (error) reject(new Error(`上传分析 Worker 失败（${file.path}）：${error.message}`)); else resolve();
              };
              const timer = setTimeout(() => finish(new Error('单文件传输超过 5 分钟。')), 5 * 60 * 1000);
              try { sftp.fastPut(file.filename, remotePath, {concurrency: 8, chunkSize: 128 * 1024}, finish); }
              catch (error) { finish(error); }
            });
          }
        }, {label: '分析 Worker 部署'});
        this.assertServerProfileTarget(operation);
        const activated = await this.executeRemote({
          command: buildAnalysisWorkerActivateScript({baseRoot, stagingRoot, finalRoot,
            identity: bundle.manifest.identity, pythonBin: profile.pythonBin}),
          timeoutMs: 120000, expectedTarget: target,
        });
        if (activated.exitCode !== 0) {
          let readiness = null;
          try { readiness = parseAnalysisWorkerStatus(activated.stderr); } catch {}
          if (readiness && !['ABSENT', 'CORRUPT', 'READY'].includes(readiness.status)) {
            const error = new Error(`内置分析 Worker 已安全部署，但运行环境校验失败${readiness.message ? `：${readiness.message}` : ''}`);
            error.code = `DESKTOP_ANALYSIS_WORKER_${readiness.status}`;
            throw error;
          }
          const detail = activated.stderr.replace(/HETERO_[A-Z0-9_]+(?:\t[A-Z0-9_]+)?\t?/g, '').trim();
          throw new Error(`内置分析 Worker 激活失败${detail ? `：${detail}` : ''}`);
        }
        parseAnalysisWorkerReady(activated.stdout, finalRoot);
        stagingCreated = false;
        return {schemaVersion: bundle.manifest.schema_version, version: bundle.manifest.version,
          identity: bundle.manifest.identity, root: finalRoot, reused: false};
      } finally {
        if (stagingCreated) {
          const cleanup = `case ${shellQuote(stagingRoot)} in ${shellQuote(`${baseRoot}/.partial-`)}*) rm -rf -- ${shellQuote(stagingRoot)} ;; esac`;
          await this.executeRemote({command: cleanup, timeoutMs: 30000, expectedTarget: target}).catch(() => {});
        }
      }
    })().finally(() => {
      if (this.pendingAnalysisWorkerDeployments.get(deploymentKey) === pending)
        this.pendingAnalysisWorkerDeployments.delete(deploymentKey);
    });
    this.pendingAnalysisWorkerDeployments.set(deploymentKey, pending);
    return pending;
  }
  async ensureRemoteCodeAnalysisRuntime(value = {}) {
    await this.ready;
    const operation = this.serverProfileOperation(value);
    const {profile, target} = operation;
    if (!target || !this.currentSshTarget() || !sameSshTarget(target, this.currentSshTarget()))
      throw new Error('自动准备代码分析环境需要当前 SSH 连接。');
    const scopeId = this.serverProfileScopeId(target);
    const candidateRoots = [target.cwd, profile.workerRepoRoot].filter(Boolean);
    const preparationKey = `${serverProfileTargetKey(target)}\n${scopeId}\n${profile.profileId}\n${profile.pythonBin}\n${candidateRoots.join('\n')}`;
    const existing = this.pendingCodeAnalysisRuntimePreparations.get(preparationKey);
    if (existing) return existing;
    const pending = (async () => {
      const result = await this.executeRemote({
        command: buildCodeAnalysisRuntimePrepareScript({
          scopeId, profileId: profile.profileId, pythonBin: profile.pythonBin, candidateRoots,
        }),
        timeoutMs: 10 * 60 * 1000,
        expectedTarget: target,
      });
      if (result.exitCode !== 0) {
        const detail = result.stderr.trim().replace(/(https?:\/\/)[^\s/@]+:[^\s/@]+@/g, '$1');
        throw new Error(`服务器代码分析环境自动准备失败${detail ? `：${detail}` : `（exit ${result.exitCode}）`}`);
      }
      this.assertServerProfileTarget(operation);
      return parseCodeAnalysisRuntime(result.stdout, {scopeId, profileId: profile.profileId});
    })().finally(() => {
      if (this.pendingCodeAnalysisRuntimePreparations.get(preparationKey) === pending)
        this.pendingCodeAnalysisRuntimePreparations.delete(preparationKey);
    });
    this.pendingCodeAnalysisRuntimePreparations.set(preparationKey, pending);
    return pending;
  }
  verifyHost(host, port, hash) {
    const filename = path.join(this.userDataDir, 'known-hosts.json');
    let known = {};
    try { known = JSON.parse(fs.readFileSync(filename, 'utf8')); } catch (error) { if (error.code !== 'ENOENT') throw new Error('SSH 主机指纹文件无法读取，请检查 known-hosts.json。'); }
    const identity = `${host}:${port}`;
    if (known[identity] && known[identity].sha256 !== hash) throw new Error(`SSH 主机指纹发生变化：${identity}。请核对服务器身份后更新本地主机指纹记录。`);
    if (!known[identity]) { known[identity] = {sha256: hash, firstSeenAt: new Date().toISOString()}; fs.writeFileSync(`${filename}.tmp`, `${JSON.stringify(known, null, 2)}\n`, {mode: 0o600}); fs.renameSync(`${filename}.tmp`, filename); }
    return true;
  }
  async connectSsh(config = {}) {
    await this.ready;
    if (this.disposed) throw new Error('应用已关闭。');
    const {generation} = await this.disconnectSsh();
    const value = {...this.settings.ssh, ...config};
    const clean = publicSsh(value);
    const suppliedPassword = clean.auth === 'password' && typeof config.password === 'string' ? config.password : '';
    let passwordToPersist = '';
    if (!clean.host || !clean.username) throw new Error('请填写 SSH 主机和用户名。');
    await this.saveSettings({ssh: clean});
    if (generation !== this.generation || this.disposed) throw new Error('SSH 连接请求已被更新的操作取消。');
    const client = this.client = new Client();
    this.emitStatus('ssh', 'connecting', '正在连接 SSH…', {...clean, ...this.sshTerminalSnapshot()});
    const options = {host: clean.host, port: clean.port, username: clean.username, readyTimeout: 20000, keepaliveInterval: 15000, keepaliveCountMax: 3, hostHash: 'sha256'};
    let hostError;
    options.hostVerifier = hash => { try { return this.verifyHost(clean.host, clean.port, hash); } catch (error) { hostError = error; return false; } };
    try {
      if (clean.auth === 'password') {
        if (suppliedPassword) {
          if (this.credentialStore) {
            const credentialStatus = await this.sshPasswordCredentialStatus(clean);
            if (!credentialStatus.available || (credentialStatus.error && !credentialStatus.recoverable)) throw new Error(credentialStatus.error || '系统安全存储当前不可用，无法保存 SSH 密码。');
          }
          options.password = suppliedPassword;
          passwordToPersist = suppliedPassword;
        } else {
          if (!this.credentialStore) throw new Error('请输入 SSH 密码；当前运行环境没有可复用的安全凭据。');
          options.password = await this.credentialStore.getSshPassword(clean);
          if (!options.password) throw new Error('请输入 SSH 密码；当前服务器没有已保存的密码。');
        }
      }
      else if (clean.auth === 'key') { if (!clean.privateKeyPath) throw new Error('请填写 SSH 私钥路径。'); options.privateKey = fs.readFileSync(expandHome(clean.privateKeyPath)); if (value.passphrase) options.passphrase = value.passphrase; }
      else { options.agent = process.env.SSH_AUTH_SOCK || (process.platform === 'win32' ? '\\\\.\\pipe\\openssh-ssh-agent' : undefined); if (!options.agent) throw new Error('未找到本地 SSH Agent。'); }
      await new Promise((resolve, reject) => {
        let settled = false;
        const finish = (error) => { if (settled) return; settled = true; clearTimeout(timer); if (this.pendingConnect === cancel) this.pendingConnect = null; if (error) reject(error); else resolve(); };
        const cancel = () => finish(new Error('SSH 连接已取消。'));
        this.pendingConnect = cancel;
        const timer = setTimeout(() => { finish(new Error('SSH 连接或终端初始化超时。')); client.destroy(); }, 25000);
        client.on('error', error => { finish(hostError || error); if (this.client === client && this.ssh.state === 'connected') this.emitStatus('ssh', 'error', error.message, this.sshTerminalSnapshot()); });
        client.on('close', () => { finish(new Error('SSH 连接已关闭。')); if (this.client === client) { this.client = null; this.activePlatformSpec = null; this.closeRemoteWorkspaceConnections(new Error('SSH 连接已关闭，远端 Workspace 操作中断。')); this.closeRemoteFileConnections(); this.closePlatformRpcConnections(new Error('SSH 连接已关闭，服务器分析中断。')); this.closeAllSshTerminals('connection_closed'); for (const pending of [...this.pendingExecs]) pending(new Error('SSH 连接已关闭，远程执行中断。')); this.emitStatus('ssh', 'disconnected', 'SSH 连接已关闭。', this.sshTerminalSnapshot()); } });
        client.on('ready', () => {
          if (generation !== this.generation) return cancel();
          // Establish that the configured working directory is usable before
          // reporting connected. This uses another channel of the same session.
          client.exec(remotePrefix(clean.cwd) + 'pwd -P', (error, channel) => {
            if (error) return finish(error);
            if (generation !== this.generation) { channel.close(); return cancel(); }
            let diagnostic = ''; let resolvedRoot = '';
            channel.on('data', data => { resolvedRoot = (resolvedRoot + data.toString('utf8')).slice(0, 4096); });
            channel.stderr.on('data', data => { diagnostic = (diagnostic + data.toString('utf8')).slice(0, 4096); });
            channel.on('error', error => finish(error));
            channel.on('close', code => {
              if (settled) return;
              const remoteRootPath = resolvedRoot.replace(/\r?\n$/, '');
              if (code !== 0 || !remoteRootPath.startsWith('/') || /[\r\n]/.test(remoteRootPath) || remoteRootPath.includes('\0'))
                finish(new Error(`远程工作目录不可用：${clean.cwd}${diagnostic.trim() ? `；${diagnostic.trim()}` : ''}`));
              else this.openSshTerminal({client, generation, cwd: clean.cwd}).then(() => {
                this.emitStatus('ssh', 'connected', 'SSH 已连接。', {connectedAt: new Date().toISOString(), remoteRootPath, ...this.sshTerminalSnapshot()});
                // Prewarm the latency-sensitive Explorer lane. Worker startup
                // is optional; failure is cached and existing SFTP/direct exec
                // paths remain available without failing SSH connection.
                void this.acquireRemoteWorkspaceConnection({role: 'interactive'}).catch(() => {});
                this.prewarmAgentWorkspaceConnection();
                finish();
              }, finish);
            });
          });
        });
        try { client.connect(options); } catch (error) { finish(error); }
      });
      if (passwordToPersist && this.credentialStore) await this.credentialStore.setSshPassword(clean, passwordToPersist);
      return this.getState();
    } catch (error) {
      client.destroy();
      if (this.client === client) { this.client = null; this.closeAllSshTerminals('connect_failed'); this.emitStatus('ssh', 'error', error.message, this.sshTerminalSnapshot()); }
      throw error;
    }
  }
  async disconnectSsh() {
    this.generation++;
    if (this.pendingConnect) this.pendingConnect();
    for (const pending of this.pendingRemoteFileSearches.values()) pending.cancel();
    for (const pending of [...this.pendingExecs]) pending(new Error('SSH 已断开，远程执行中断。'));
    const client = this.client; this.client = null;
    this.activePlatformSpec = null;
    this.closeRemoteWorkspaceConnections(new Error('SSH 已断开，远端 Workspace 操作中断。'));
    this.closeRemoteFileConnections();
    this.closePlatformRpcConnections(new Error('SSH 已断开，服务器分析中断。'));
    this.closeAllSshTerminals('disconnected');
    if (client) { client.end(); const timer = setTimeout(() => client.destroy(), 500); timer.unref(); }
    this.emitStatus('ssh', 'disconnected', 'SSH 未连接。', this.sshTerminalSnapshot());
    return {state: 'disconnected', generation: this.generation};
  }
  sshTerminalRecord(terminalId, {allowPrimary = false} = {}) {
    const id = terminalId === undefined || terminalId === null || terminalId === ''
      ? (allowPrimary ? this.primarySshTerminalId : '') : String(terminalId);
    if (!id || !/^[A-Za-z0-9._:-]{1,128}$/.test(id)) throw new Error('SSH 终端 ID 无效。');
    const record = this.sshTerminals.get(id);
    if (!record || record.state !== 'open' || !record.stream) throw new Error('SSH 终端不存在或已关闭。');
    return record;
  }
  async createSshTerminal({cols = 100, rows = 28} = {}) {
    await this.ready;
    if (this.ssh.state !== 'connected' || !this.client) throw new Error('请先连接 SSH 服务器。');
    const terminal = await this.openSshTerminal({client: this.client, generation: this.generation, cwd: this.ssh.cwd, cols, rows});
    return {terminal, ...this.sshTerminalSnapshot()};
  }
  async listSshTerminals() {
    await this.ready;
    return clone(this.sshTerminalSnapshot());
  }
  inputSshTerminal({terminalId, data} = {}, {allowPrimary = false} = {}) {
    if (typeof data !== 'string' || data.length > 65536) throw new Error('终端输入无效。');
    const record = this.sshTerminalRecord(terminalId, {allowPrimary});
    record.stream.write(data);
    return {ok: true, terminalId: record.terminalId};
  }
  resizeSshTerminal({terminalId, cols, rows} = {}, {allowPrimary = false} = {}) {
    const record = this.sshTerminalRecord(terminalId, {allowPrimary});
    record.cols = boundedInt(cols, record.cols, 10, 500);
    record.rows = boundedInt(rows, record.rows, 3, 200);
    record.stream.setWindow(record.rows, record.cols, 0, 0);
    this.syncSshTerminalAliases();
    return {ok: true, terminalId: record.terminalId, cols: record.cols, rows: record.rows};
  }
  async closeSshTerminal({terminalId} = {}) {
    await this.ready;
    const record = this.sshTerminalRecord(terminalId);
    this.removeSshTerminal(record.terminalId, record.stream, 'local_closed');
    try { record.stream.close(); } catch {}
    return {closed: true, terminalId: record.terminalId, ...this.sshTerminalSnapshot()};
  }
  async execRemote(options = {}) { return this.executeRemote(options); }
  assertAgentWorkspaceContext(toolContext) {
    if (!toolContext || toolContext.id !== this.toolSession.id || toolContext.token !== this.brokerToken)
      throw new Error('Agent 工具会话已过期，请重新启动 Agent。');
    if (this.ssh.state !== 'connected' || !this.client) throw new Error('请先连接 SSH 服务器。');
    if (!this.sameSshTarget(toolContext.boundTarget, this.currentSshTarget()))
      throw new Error('SSH 目标已改变，当前 Agent 仍绑定原服务器或工作目录；请重启 Agent 以绑定新连接。');
  }
  async workspaceToolRequest(method, params, toolContext) {
    await this.ready;
    this.assertAgentWorkspaceContext(toolContext);
    const id = crypto.randomUUID();
    const started = Date.now();
    const operation = {type: 'operation', target: 'ssh', id, kind: 'workspace_rpc', origin: 'agent',
      method, path: String(params?.path || '')};
    const requestId = `agent-workspace-${crypto.randomUUID()}`;
    const pending = {
      requestId, toolSessionId: toolContext.id, cancelled: false, error: null,
      cancel: error => {
        if (pending.cancelled) return;
        pending.cancelled = true;
        pending.error = error || Object.assign(new Error('远端 Workspace 请求已取消。'), {code: 'WORKSPACE_RPC_CANCELLED'});
        this.remoteWorkspaceConnections.get('agent')?.cancel(requestId, pending.error);
      },
    };
    this.pendingWorkspaceRequests.add(pending);
    this.emit('event', {...operation, state: 'running'});
    try {
      const result = await this.workspaceRequest({role: 'agent', method, params,
        timeoutMs: method === 'fs.search' ? 35000 : 30000, requestId,
        isCancelled: () => pending.cancelled, cancellationError: () => pending.error});
      this.emit('event', {...operation, state: 'completed', durationMs: Date.now() - started,
        transport: result.transport, channelReused: result.channelReused,
        transportDurationMs: result.transportDurationMs,
        channelAcquireDurationMs: result.channelAcquireDurationMs,
        channelStartupDurationMs: result.channelStartupDurationMs});
      return result;
    } catch (error) {
      this.emit('event', {...operation, state: 'error', durationMs: Date.now() - started,
        message: error.message, transport: error.workspaceTransportUnavailable ? 'workspace-unavailable' : 'workspace-worker'});
      throw error;
    } finally {
      this.pendingWorkspaceRequests.delete(pending);
    }
  }
  async workspaceStat(params, toolContext) { return this.workspaceToolRequest('fs.stat', params, toolContext); }
  async workspaceList(params, toolContext) { return this.workspaceToolRequest('fs.list', params, toolContext); }
  async workspaceRead(params, toolContext) { return this.workspaceToolRequest('fs.readText', params, toolContext); }
  async workspaceSearch(params, toolContext) { return this.workspaceToolRequest('fs.search', params, toolContext); }
  async workspaceWrite(params, toolContext) { return this.workspaceToolRequest('fs.writeText', params, toolContext); }
  async cancelRemoteExec({requestId, expectedTarget = null} = {}) {
    await this.ready;
    const normalizedId = String(requestId || '');
    if (!/^[A-Za-z0-9._:-]{1,128}$/.test(normalizedId)) throw new Error('远程请求 ID 无效。');
    if (expectedTarget !== null) {
      const normalizedExpected = normalizeSshTarget(expectedTarget);
      if (!hasSshTarget(normalizedExpected) || !sameSshTarget(normalizedExpected, this.currentSshTarget()))
        throw new Error('SSH 目标已改变，无法取消另一目标上的请求。');
    }
    const pending = [...this.pendingExecs].find(cancel => cancel.requestId === normalizedId);
    if (!pending) return {requestId: normalizedId, cancelled: false};
    pending(new Error('远程请求已取消。'));
    return {requestId: normalizedId, cancelled: true};
  }
  executeRemoteDirect({client, command, effectiveTimeoutMs, started, pendingState}) {
    return new Promise((resolve, reject) => {
      let stream; let done = false; let total = 0; const stdout = []; const stderr = [];
      const finish = (error, code, signal) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        pendingState.directCancel = null;
        if (error) {
          try { stream?.signal('TERM'); stream?.close(); } catch {}
          reject(error);
        } else resolve({
          stdout: Buffer.concat(stdout).toString('utf8'), stderr: Buffer.concat(stderr).toString('utf8'),
          exitCode: Number.isInteger(code) ? code : null, signal: signal || null,
          durationMs: Date.now() - started, cwd: this.ssh.cwd,
          transport: 'ssh-exec-fallback', channelReused: false, queueWaitMs: 0,
        });
      };
      pendingState.directCancel = error => finish(error);
      const timer = setTimeout(() => finish(new Error(`远程执行超过 ${effectiveTimeoutMs} ms，已关闭执行通道。`)), effectiveTimeoutMs);
      if (pendingState.cancelled) return finish(pendingState.cancelError || new Error('远程请求已取消。'));
      client.exec(remotePrefix(this.ssh.cwd) + `bash -c ${shellQuote(command)}`, (error, channel) => {
        if (error) return finish(error);
        if (done || pendingState.cancelled) { channel.close(); return finish(pendingState.cancelError || new Error('远程请求已取消。')); }
        stream = channel;
        const append = (target, data) => {
          total += data.length;
          if (total > MAX_OUTPUT) finish(new Error('远程命令输出超过 1 MiB，已关闭执行通道。'));
          else target.push(Buffer.from(data));
        };
        stream.on('data', data => append(stdout, data));
        stream.stderr.on('data', data => append(stderr, data));
        stream.on('error', error => finish(error));
        stream.on('close', (code, signal) => finish(null, code, signal));
      });
    });
  }
  async executeRemote({command, timeoutMs = 30000, expectedTarget = null, requestId = null} = {}, toolContext = null) {
    await this.ready;
    if (toolContext) this.assertAgentWorkspaceContext(toolContext);
    if (this.ssh.state !== 'connected' || !this.client) throw new Error('请先连接 SSH 服务器。');
    let normalizedExpected = null;
    if (expectedTarget !== null) {
      normalizedExpected = normalizeSshTarget(expectedTarget);
      if (!hasSshTarget(normalizedExpected) || !sameSshTarget(normalizedExpected, this.currentSshTarget()))
        throw new Error('SSH 目标已改变，已拒绝在另一台服务器或工作目录执行请求。');
    }
    const normalizedRequestId = requestId === null ? null : String(requestId || '');
    if (normalizedRequestId !== null && !/^[A-Za-z0-9._:-]{1,128}$/.test(normalizedRequestId)) throw new Error('远程请求 ID 无效。');
    if (normalizedRequestId && [...this.pendingExecs].some(cancel => cancel.requestId === normalizedRequestId)) throw new Error('同一远程请求 ID 已在执行。');
    if (typeof command !== 'string' || !command.trim() || command.includes('\0') || Buffer.byteLength(command) > 32768) throw new Error('远程命令不能为空，且长度须小于 32 KiB。');
    if (this.pendingExecs.size >= 8) throw new Error('远程执行已有 8 个并发命令，请等待完成。');
    const client = this.client;
    const started = Date.now();
    const effectiveTimeoutMs = remoteExecutionTimeout(timeoutMs, Boolean(toolContext));
    const id = crypto.randomUUID();
    const operationIdentity = {kind: 'remote_exec', origin: toolContext ? 'agent' : 'platform'};
    this.emit('event', {type: 'operation', target: 'ssh', id, state: 'running', command, ...operationIdentity});
    const effectiveRequestId = normalizedRequestId || `remote-exec-${crypto.randomUUID()}`;
    const pendingState = {cancelled: false, cancelError: null, record: null, directCancel: null};
    const cancel = error => {
      if (pendingState.cancelled) return;
      pendingState.cancelled = true;
      pendingState.cancelError = error || new Error('远程请求已取消。');
      pendingState.record?.cancel(effectiveRequestId, pendingState.cancelError);
      pendingState.directCancel?.(pendingState.cancelError);
    };
    if (toolContext) cancel.toolSessionId = toolContext.id;
    if (normalizedRequestId) cancel.requestId = normalizedRequestId;
    this.pendingExecs.add(cancel);
    try {
      let record = null;
      try { record = await this.acquireRemoteWorkspaceConnection({role: toolContext ? 'agent' : 'platform'}); }
      catch (error) { if (!error.workspaceTransportUnavailable) throw error; }
      if (pendingState.cancelled) throw pendingState.cancelError;
      let result;
      if (!record) {
        result = await this.executeRemoteDirect({client, command, effectiveTimeoutMs, started, pendingState});
      } else {
        pendingState.record = record;
        try {
          const response = await record.request({
            requestId: effectiveRequestId, method: 'proc.exec',
            params: {command, timeoutMs: effectiveTimeoutMs, maxOutputBytes: MAX_OUTPUT},
            timeoutMs: Math.min(MAX_PLATFORM_REMOTE_TIMEOUT_MS + 10000, effectiveTimeoutMs + 5000),
          });
          const remote = response.result;
          result = {
            ...remote, cwd: this.ssh.cwd, transport: 'workspace-worker',
            channelReused: response.channelReused,
            transportDurationMs: response.durationMs,
            queueWaitMs: Math.max(0, Number(remote.queueWaitMs || 0)),
          };
        } catch (error) {
          // The Worker guarantees these errors occur before command launch, so
          // a direct-exec fallback cannot duplicate side effects.
          if (error.code === 'WORKSPACE_PATH_NOT_FOUND')
            result = await this.executeRemoteDirect({client, command, effectiveTimeoutMs, started, pendingState});
          else if (error.code === 'WORKSPACE_EXEC_OUTPUT_LIMIT')
            throw Object.assign(new Error('远程命令输出超过 1 MiB，已关闭执行通道。'), {code: error.code, cause: error});
          else throw error;
        }
      }
      if (normalizedExpected && !sameSshTarget(normalizedExpected, this.currentSshTarget()))
        throw new Error('SSH 目标在远程请求期间发生变化，已丢弃旧目标返回结果。');
      if (pendingState.cancelled) throw pendingState.cancelError;
      this.emit('event', {type: 'operation', target: 'ssh', id, state: result.exitCode === 0 ? 'completed' : 'failed', command, ...result, ...operationIdentity});
      return result;
    } catch (error) {
      this.emit('event', {type: 'operation', target: 'ssh', id, state: 'error', command, message: error.message, durationMs: Date.now() - started, ...operationIdentity});
      throw error;
    } finally { this.pendingExecs.delete(cancel); }
  }
  async startAgent(config = {}) {
    await this.ready;
    if (this.disposed) throw new Error('应用已关闭。');
    const {generation} = await this.stopAgent();
    const value = normalizeAgent({...this.settings.agent, ...config});
    const cwd = expandHome(value.cwd || os.homedir());
    if (!fs.statSync(cwd).isDirectory()) throw new Error('Agent 工作目录不存在。');
    const environment = {...process.env};
    for (const [key, val] of Object.entries(value.environment)) { const match = /^\{env:([A-Za-z_][A-Za-z0-9_]*)\}$/.exec(val); environment[key] = match ? process.env[match[1]] || '' : val; }
    // Electron's Node mode belongs only to helper/demo processes, not arbitrary Agents.
    delete environment.ELECTRON_RUN_AS_NODE;
    const sessionId = crypto.randomUUID();
    const skillRoot = this.agentSkillRoot;
    Object.assign(environment, {TERM: 'xterm-256color', HETERO_DESKTOP_BROKER_URL: this.integration.brokerUrl, HETERO_DESKTOP_BROKER_TOKEN: this.brokerToken, HETERO_DESKTOP_MCP_CONFIG: this.integration.mcpConfigPath, HETERO_AGENT_MCP_CONFIG: this.integration.mcpConfigPath, HETERO_AGENT_SESSION_ID: sessionId, HETERO_AGENT_SKILL_ROOT: skillRoot, HETERO_DESKTOP_TOOLS: this.integration.cliPath, HETERO_DESKTOP_NODE: this.executablePath, HETERO_DESKTOP_HELPER: this.helperPath});
    const executable = value.demo ? this.executablePath : value.executable;
    if (!executable) throw new Error('请填写 Agent 可执行文件。');
    const replacements = {workspace: cwd, mcp_config: this.integration.mcpConfigPath, mcp_descriptor: this.integration.mcpConfigPath, agent_session_id: sessionId, run_id: sessionId, skill_root: skillRoot};
    const args = value.demo ? [this.demoPath] : value.args.map(arg => arg.replace(/\{([a-z_]+)\}/g, (_, name) => replacements[name]));
    if (value.demo) environment.ELECTRON_RUN_AS_NODE = '1';
    const launch = createAgentLaunch(executable, args, environment);
    await this.saveSettings({agent: value});
    if (generation !== this.agentGeneration || this.disposed) throw new Error('Agent 启动请求已被更新的操作取消。');
    this.toolSession.boundTarget = this.currentSshTarget();
    this.emitStatus('agent', 'starting', value.demo ? '正在启动连接测试客户端…' : '正在启动本地 Agent…', {demo: value.demo, cwd, executable: executable, sessionId});
    try {
      const pty = (this.ptyModule || require('node-pty')).spawn(launch.file, launch.args, {name: 'xterm-256color', cols: 100, rows: 28, cwd, env: environment, useConpty: true});
      this.agentPty = pty;
      this.agentConfig = {...value, submitMode: value.demo ? 'plain_text_cr' : value.submitMode};
      this.agentReadyAt = Date.now() + value.startupDelayMs;
      this.lastAgentOutputAt = Date.now();
      this.agentOutputSequence = 0;
      this.agentRuns.start({sessionId, executable, cwd, demo: value.demo});
      pty.onData(data => { this.lastAgentOutputAt = Date.now(); this.agentOutputSequence++; this.agentRuns.agentOutput(data); this.emit('event', {type: 'terminal', target: 'agent', data}); });
      pty.onExit(({exitCode, signal}) => { if (this.agentPty === pty) { this.agentPty = null; this.agentGeneration++; this.agentBootstrap = {generation: this.agentGeneration, state: 'idle', enabled: false, message: ''}; this.agentRuns.finish({status: exitCode === 0 ? 'stopped' : 'error', exitCode, signal}); this.rotateToolSession(); this.emitStatus('agent', exitCode === 0 ? 'stopped' : 'error', `Agent 已退出（${exitCode}）。`, {exitCode, signal, bootstrapContextStatus: clone(this.agentBootstrap)}); } });
      // Agent knowledge is provisioned by its own persistent instructions.
      // Starting a PTY must never write hidden text into the interactive CLI.
      // Keep the status field as a compatibility surface for older renderers
      // and persisted configurations, while deliberately performing no
      // bootstrap operation. Legacy bootstrapContext fields are discarded by
      // normalizeAgent before the profile is saved again.
      this.agentBootstrap = {generation, state: 'disabled', enabled: false,
        message: '启动知识由 Agent 长期记忆提供。'};
      if (generation === this.agentGeneration) this.emitStatus('agent', 'running', value.demo ? '连接测试客户端已启动（不是 AI Agent）。' : '本地 Agent 已启动。', {pid: pty.pid, bootstrapContextStatus: clone(this.agentBootstrap)});
      this.prewarmAgentWorkspaceConnection();
      return this.getState();
    } catch (error) {
      this.emitStatus('agent', 'error', error.message); throw error;
    }
  }
  async stopAgent() {
    this.agentGeneration++;
    this.rotateToolSession();
    const pty = this.agentPty; this.agentPty = null;
    this.agentBootstrap = {generation: this.agentGeneration, state: 'idle', enabled: false, message: ''};
    if (pty) try { pty.kill(); } catch {}
    this.agentRuns.finish({status: 'stopped'});
    this.emitStatus('agent', 'stopped', 'Agent 未运行。', {bootstrapContextStatus: clone(this.agentBootstrap)});
    return {state: 'stopped', generation: this.agentGeneration};
  }
  async submitAgent({text, taskContext = null} = {}) {
    if (typeof text !== 'string' || !text.trim() || text.length > 65536 || text.includes('\0')) throw new Error('请输入要发送给 Agent 的内容（不超过 64 KiB）。');
    const pty = this.agentPty; const generation = this.agentGeneration;
    if (!pty || this.agent.state !== 'running') throw new Error('请先启动本地 Agent。');
    const normalizedTaskContext = normalizeAgentTaskContext(taskContext);
    const task = this.submitQueue.catch(() => {}).then(async () => {
      await delay(Math.max(0, this.agentReadyAt - Date.now()));
      if (pty !== this.agentPty || generation !== this.agentGeneration) throw new Error('Agent 会话已变更，本次消息未发送。');
      const {submitMode, submitDelayMs} = this.agentConfig;
      const bracketed = submitMode.startsWith('bracketed_paste');
      const outputSequenceBefore = this.agentOutputSequence;
      pty.write(bracketed ? `\x1b[200~${text}\x1b[201~` : text);
      const delivery = bracketed
        ? await waitForAgentPasteSettle({
          text, configuredDelayMs: submitDelayMs, outputSequenceBefore,
          readOutputSequence: () => this.agentOutputSequence,
          readLastOutputAt: () => this.lastAgentOutputAt,
          sessionCurrent: () => pty === this.agentPty && generation === this.agentGeneration,
        })
        : await (async () => {
          const startedAt = Date.now();
          await delay(submitDelayMs);
          return {
            strategy: 'plain_delay', actualWaitMs: Date.now() - startedAt,
            outputObserved: this.agentOutputSequence > outputSequenceBefore,
            waitReason: 'configured_delay_elapsed', bytes: Buffer.byteLength(text, 'utf8'),
          };
        })();
      if (pty !== this.agentPty || generation !== this.agentGeneration) throw new Error('Agent 会话已关闭。');
      this.abandonOpenWorkbenchRecovery('自动恢复任务已被新的 Agent 任务替换。');
      this.toolSession.taskContext = normalizedTaskContext;
      this.toolSession.taskContract = null;
      if (normalizedTaskContext?.task_kind === 'workbench_error_recovery') this.workbenchRecoveryContract();
      this.agentRuns.submit(text, normalizedTaskContext);
      const submitKey = submitMode.endsWith('_lf') ? '\n' : '\r';
      pty.write(submitKey);
      const diagnostics = {
        ...delivery, strategy: bracketed ? 'pty_output_settle_v2' : delivery.strategy,
        submitKey: submitKey === '\n' ? 'lf' : 'cr', submitKeyCount: 1,
      };
      this.emit('event', {type: 'operation', target: 'agent', state: 'submitted',
        message: '消息正文与提交键已分别写入 PTY。', delivery: diagnostics});
      return {submitted: true, delivery: diagnostics};
    });
    this.submitQueue = task;
    return task;
  }
  terminalInput({target, terminalId, data} = {}) {
    if (typeof data !== 'string' || data.length > 65536) throw new Error('终端输入无效。');
    if (target === 'ssh') return this.inputSshTerminal({terminalId, data}, {allowPrimary: true});
    const stream = target === 'agent' ? this.agentPty : null;
    if (!stream) throw new Error('终端尚未连接。');
    if (target === 'agent') {
      const generation = this.agentGeneration;
      if (isAgentControlInput(data)) {
        if (stream !== this.agentPty || generation !== this.agentGeneration) throw new Error('Agent 会话已变更。');
        stream.write(data);
        return {ok: true, bypassedQueue: true};
      }
      const task = this.submitQueue.catch(() => {}).then(() => { if (stream !== this.agentPty || generation !== this.agentGeneration) throw new Error('Agent 会话已变更。'); stream.write(data); return {ok: true}; });
      this.submitQueue = task;
      return task;
    }
    stream.write(data); return {ok: true};
  }
  terminalResize({target, terminalId, cols, rows} = {}) {
    cols = boundedInt(cols, 100, 10, 500); rows = boundedInt(rows, 28, 3, 200);
    if (target === 'agent') this.agentPty?.resize(cols, rows);
    else if (target === 'ssh') {
      if (!terminalId && !this.primarySshTerminalId) return {ok: true};
      return this.resizeSshTerminal({terminalId, cols, rows}, {allowPrimary: true});
    }
    return {ok: true};
  }
  async discoverDevices({hdcPath} = {}) {
    await this.ready;
    const executable = String(hdcPath || this.settings.hdcPath || 'hdc');
    const started = Date.now();
    return new Promise((resolve, reject) => {
      execFile(executable, ['list', 'targets'], {timeout: 10000, maxBuffer: 128 * 1024, windowsHide: true}, async (error, stdout, stderr) => {
        if (error) { const failure = new Error(`本地 HDC 设备发现失败：${error.code || error.message}${stderr ? `；${stderr.trim()}` : ''}`); this.emit('event', {type: 'operation', target: 'hdc', state: 'error', message: failure.message}); return reject(failure); }
        const devices = stdout.split(/\r?\n/).map(line => line.trim()).filter(line => line && !/^\[|empty|no device|targets?:/i.test(line)).map(serial => ({serial, deviceSerial: serial, connectionMode: 'local'}));
        const result = {devices, stdout, stderr, durationMs: Date.now() - started, transport: 'local'};
        try {
          if (hdcPath !== undefined && executable !== this.settings.hdcPath) await this.saveSettings({hdcPath: executable});
          await this.capture.updateDevices(result);
        } catch (error) { return reject(error); }
        this.emit('event', {type: 'operation', target: 'hdc', state: 'completed', ...result}); resolve(result);
      });
    });
  }
  async extractHardwareProfile({deviceSerial, hdcPath} = {}) {
    await this.ready;
    const executable = String(hdcPath || this.settings.hdcPath || 'hdc');
    try {
      const result = await extractLocalHardwareProfile({deviceSerial, hdcPath: executable});
      if (hdcPath !== undefined && executable !== this.settings.hdcPath) await this.saveSettings({hdcPath: executable});
      this.emit('event', {type: 'operation', target: 'hdc', kind: 'hardware_profile_extract', state: 'completed',
        durationMs: result.durationMs, deviceSerialSha256: result.profile.evidence.device_serial_sha256,
        domainCount: result.diagnostics.domain_count});
      return result;
    } catch (error) {
      this.emit('event', {type: 'operation', target: 'hdc', kind: 'hardware_profile_extract', state: 'error', message: error.message});
      throw error;
    }
  }
  async startCalibrationRun(value = {}) {
    await this.ready;
    const hdcPath = resolveExecutable(String(value.hdcPath || this.settings.hdcPath || 'hdc'), process.env);
    const extracted = await extractLocalHardwareProfile({deviceSerial: value.deviceSerial, hdcPath});
    validateDeviceHardwareProfile(value.hardwareProfile, extracted.profile);
    return this.calibration.start({...value, hdcPath});
  }
  async calibrationRunStatus(value = {}) {
    await this.ready;
    await this.calibration.initialReady;
    // Status polling also drives bounded, idempotent recovery for a completed
    // run whose cpufreq bounds could not be restored on the first attempt.
    // The reconcile runs in the background so a slow HDC command cannot stall
    // the renderer's progress channel.
    return this.calibration.status({...value, reconcile: true});
  }
  async cancelCalibrationRun(value = {}) {
    await this.ready;
    return this.calibration.cancel(value);
  }
  async getCalibrationMeasurements(value = {}) {
    await this.ready;
    await this.calibration.initialReady;
    return this.calibration.measurements(value);
  }
  handleBroker(req, res) {
    const reply = (code, value) => { if (!res.destroyed) { res.writeHead(code, {'Content-Type': 'application/json', 'Cache-Control': 'no-store'}); res.end(JSON.stringify(value)); } };
    const authorization = String(req.headers.authorization || '');
    const authorized = () => {
      const suppliedBuffer = Buffer.from(authorization); const expectedBuffer = Buffer.from(`Bearer ${this.brokerToken}`);
      return !req.headers.origin && suppliedBuffer.length === expectedBuffer.length && crypto.timingSafeEqual(suppliedBuffer, expectedBuffer);
    };
    if (req.method !== 'POST' || req.url !== '/tools') { reply(404, {error: 'Not found'}); return; }
    if (!authorized()) { reply(401, {error: 'Unauthorized'}); return; }
    let size = 0; const chunks = [];
    req.on('data', data => { size += data.length; if (size > MAX_BROKER_REQUEST_BYTES) { reply(413, {error: 'Request too large'}); req.destroy(); } else chunks.push(data); });
    req.on('error', () => {});
    req.on('end', async () => {
      let brokerMethod = ''; let brokerParams = {}; let operationId = ''; let operationStartedAt = 0;
      let capturePlanRefinement = null; let capturePlanRefinementAttempt = null;
      let workbenchRecovery = null; let recoveryCompletionOperationId = '';
      let recoveryPlanUpdateAttempt = null;
      try {
        if (this.disposed) return reply(503, {error: 'Desktop is closing'});
        if (!authorized()) return reply(401, {error: 'Agent 工具会话已过期，请重新启动 Agent。'});
        const {method, params} = JSON.parse(Buffer.concat(chunks).toString('utf8'));
        brokerMethod = method; brokerParams = params || {};
        let result;
        const captureTool = typeof method === 'string' && method.startsWith('capture_');
        operationId = captureTool ? `capture-tool-${crypto.randomUUID()}` : '';
        operationStartedAt = captureTool ? Date.now() : 0;
        capturePlanRefinement = this.capturePlanRefinementContract();
        if (capturePlanRefinement) {
          if (method !== 'capture_update_plan')
            throw new Error('计划补全任务只允许调用 capture_update_plan；远程、工作区、采集执行和查询工具均不可用。');
          capturePlanRefinementAttempt = capturePlanRefinement;
          this.beginCapturePlanRefinement(capturePlanRefinementAttempt, params);
        }
        workbenchRecovery = this.workbenchRecoveryContract();
        if (workbenchRecovery) this.assertWorkbenchRecoveryTool(workbenchRecovery, method, params);
        else if (method === 'desktop_recovery_complete')
          throw new Error('当前 Agent 任务不是类型化自动恢复任务，不能提交恢复完成握手。');
        if (method === 'desktop_recovery_complete') {
          recoveryCompletionOperationId = `agent-recovery-${crypto.randomUUID()}`;
          this.emit('event', {type: 'operation', target: 'automation', id: recoveryCompletionOperationId,
            kind: 'agent_recovery', origin: 'agent', method, requestId: workbenchRecovery.requestId,
            state: 'running'});
        }
        if (workbenchRecovery && method === 'capture_update_plan') {
          recoveryPlanUpdateAttempt = this.beginRecoveryCapturePlanUpdate(workbenchRecovery, params);
        }
        if (captureTool) this.emit('event', {type: 'operation', target: 'capture', id: operationId, kind: 'capture_tool', origin: 'agent', method, requestId: capturePlanRefinementAttempt?.requestId || params?.requestId || '', state: 'running'});
        if (method === 'remote_status') result = this.toolStatus();
        else if (method === 'remote_exec') { this.toolStatus(); result = await this.executeRemote(params, {id: this.toolSession.id, token: this.brokerToken, boundTarget: clone(this.toolSession.boundTarget)}); }
        else if (method === 'workspace_stat') { this.toolStatus(); result = await this.workspaceStat(params, {id: this.toolSession.id, token: this.brokerToken, boundTarget: clone(this.toolSession.boundTarget)}); }
        else if (method === 'workspace_list') { this.toolStatus(); result = await this.workspaceList(params, {id: this.toolSession.id, token: this.brokerToken, boundTarget: clone(this.toolSession.boundTarget)}); }
        else if (method === 'workspace_read') { this.toolStatus(); result = await this.workspaceRead(params, {id: this.toolSession.id, token: this.brokerToken, boundTarget: clone(this.toolSession.boundTarget)}); }
        else if (method === 'workspace_search') { this.toolStatus(); result = await this.workspaceSearch(params, {id: this.toolSession.id, token: this.brokerToken, boundTarget: clone(this.toolSession.boundTarget)}); }
        else if (method === 'workspace_write') { this.toolStatus(); result = await this.workspaceWrite(params, {id: this.toolSession.id, token: this.brokerToken, boundTarget: clone(this.toolSession.boundTarget)}); }
        else if (method === 'device_discover') result = await this.discoverDevices();
        else if (method === 'capture_catalog') result = await this.captureCatalog();
        else if (method === 'capture_status') result = await this.captureStatus(params);
        else if (method === 'capture_save_plan') result = await this.saveCapturePlan(params);
        else if (method === 'capture_update_plan') result = await this.updateCapturePlan(params);
        else if (method === 'capture_start') {
          if (!params || typeof params.planId !== 'string' || typeof params.deviceSerial !== 'string' || !params.deviceSerial) throw new Error('Agent 采集请求须明确给出 planId 和当前选择的 deviceSerial。');
          result = await this.startCapture(params);
        }
        else if (method === 'capture_cancel') result = await this.cancelCapture(params);
        else if (method === 'trace_list') result = {traces: await this.listTraces()};
        else if (method === 'trace_detail') result = await this.getTrace(params);
        else if (method === 'desktop_agent_run_list') result = await this.listAgentRuns(params);
        else if (method === 'desktop_agent_run_read') result = await this.getAgentRun(params);
        else if (method === 'desktop_recovery_complete') result = this.completeWorkbenchRecovery(workbenchRecovery, params);
        else if (Object.hasOwn(APPLICATION_FLOW_TOOLS, method)) result = await this.applicationFlowTool(method, params);
        else return reply(400, {error: 'Unknown tool'});
        if (recoveryPlanUpdateAttempt) this.finishRecoveryCapturePlanUpdate(recoveryPlanUpdateAttempt, result);
        if (capturePlanRefinementAttempt) capturePlanRefinementAttempt.state = 'completed';
        if (captureTool) this.emit('event', {type: 'operation', target: 'capture', id: operationId, kind: 'capture_tool', origin: 'agent', method, requestId: capturePlanRefinementAttempt?.requestId || params?.requestId || '', state: 'completed', durationMs: Date.now() - operationStartedAt});
        if (recoveryCompletionOperationId) this.emit('event', {type: 'operation', target: 'automation',
          id: recoveryCompletionOperationId, kind: 'agent_recovery', origin: 'agent', method,
          requestId: workbenchRecovery.requestId, state: 'completed'});
        reply(200, {result});
      } catch (error) {
        if (recoveryPlanUpdateAttempt?.planUpdateState === 'running') recoveryPlanUpdateAttempt.planUpdateState = 'failed';
        const firstRefinementFailure = capturePlanRefinementAttempt?.state === 'running';
        if (firstRefinementFailure) capturePlanRefinementAttempt.state = 'failed';
        const recordCaptureFailure = !capturePlanRefinement || firstRefinementFailure;
        if (recordCaptureFailure && typeof brokerMethod === 'string' && brokerMethod.startsWith('capture_') && operationId) this.emit('event', {type: 'operation', target: 'capture', id: operationId, kind: 'capture_tool', origin: 'agent', method: brokerMethod, requestId: capturePlanRefinementAttempt?.requestId || brokerParams.requestId || '', state: 'failed', durationMs: Date.now() - operationStartedAt, message: error.message});
        if (firstRefinementFailure) this.capture.notify('agent-plan-failed', {requestId: capturePlanRefinementAttempt.requestId, planId: capturePlanRefinementAttempt.planId, message: error.message});
        else if (!capturePlanRefinement && brokerMethod === 'capture_update_plan' && typeof brokerParams.requestId === 'string') this.capture.notify('agent-plan-failed', {requestId: brokerParams.requestId, planId: brokerParams.planId || '', message: error.message});
        if (recoveryCompletionOperationId) this.emit('event', {type: 'operation', target: 'automation',
          id: recoveryCompletionOperationId, kind: 'agent_recovery', origin: 'agent', method: brokerMethod,
          requestId: workbenchRecovery?.requestId || '', state: 'failed', message: error.message});
        reply(400, {error: error.message});
      }
    });
  }
  async dispose() {
    if (this.disposed) return;
    this.disposed = true;
    await this.ready.catch(() => {});
    await this.capture.dispose();
    await this.calibration.dispose();
    await this.stopAgent(); await this.disconnectSsh();
    if (this.broker) { this.broker.closeAllConnections?.(); await new Promise(resolve => this.broker.close(resolve)); this.broker = null; }
    this.brokerToken = '';
  }
}
module.exports = {
  DesktopRuntime, MAX_AGENT_REMOTE_TIMEOUT_MS, MAX_PLATFORM_REMOTE_TIMEOUT_MS,
  normalizeAgent, createAgentLaunch, remoteExecutionTimeout, remotePrefix, shellQuote,
  normalizeAgentTaskContext,
  agentPasteTiming, waitForAgentPasteSettle,
  captureLocalDestinationFingerprint, sameLocalDestinationFingerprint,
  replaceDownloadedFile,
};
