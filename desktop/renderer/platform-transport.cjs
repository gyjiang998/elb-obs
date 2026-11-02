'use strict';

const {hasSshTarget, normalizeSshTarget, sameSshTarget, serverProfileTargetKey} = require('./server-profile-target.cjs');

const RPC_SCHEMA = 'desktop_platform_rpc/v2';
const RPC_MARKER = 'HETERO_DESKTOP_RPC\t';
const RPC_GZIP_MARKER = 'HETERO_DESKTOP_RPC_GZIP\t';
const RPC_REPAIR_MARKER = 'HETERO_DESKTOP_REPAIRABLE\t';
const PROFILE_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const SCOPE_PATTERN = /^[0-9a-f]{16}$/;
const MAX_REMOTE_COMMAND_BYTES = 32768;
const SERVER_PROFILE_UNINITIALIZED_EXIT_CODE = 78;
const REMOTE_WORKER_UNAVAILABLE_EXIT_CODE = 79;
// The persistent Python page worker consumes its stdio requests synchronously.
// Opening more SSH requests here only creates renderer-side contention; keep one
// request in flight and let the queue prioritize the page the user is viewing.
const DEFAULT_PLATFORM_REQUEST_CONCURRENCY = 1;
const DEFAULT_PLATFORM_REQUEST_TIMEOUT_MS = 120000;
const SEARCH_PLATFORM_REQUEST_TIMEOUT_MS = 10 * 60 * 1000;
const CODE_GRAPH_REQUEST_TIMEOUT_MS = 30 * 60 * 1000;

function shellQuote(value) { return `'${String(value).replace(/'/g, `'"'"'`)}'`; }

function abortError() {
  if (typeof DOMException === 'function') return new DOMException('请求已取消', 'AbortError');
  const error = new Error('请求已取消');
  error.name = 'AbortError';
  return error;
}

/**
 * Bound the number of SSH exec channels used by server-backed pages.  React
 * pages remain mounted while hidden, so one workspace update can otherwise
 * fan out enough refresh requests to exhaust ssh2's command channels.
 */
function createConcurrentTaskQueue(limit = DEFAULT_PLATFORM_REQUEST_CONCURRENCY) {
  if (!Number.isInteger(limit) || limit < 1) throw new Error('并发队列上限无效。');
  let active = 0;
  const pending = [];
  const priorityOf = item => {
    try {
      const value = typeof item.priority === 'function' ? item.priority() : item.priority;
      const number = Number(value ?? 0);
      return Number.isFinite(number) ? number : 0;
    } catch { return 0; }
  };
  const takeNext = () => {
    let selected = 0;
    let selectedPriority = priorityOf(pending[0]);
    // Replace only for a strictly higher priority so equal-priority requests
    // retain their insertion order.
    for (let index = 1; index < pending.length; index += 1) {
      const priority = priorityOf(pending[index]);
      if (priority > selectedPriority) { selected = index; selectedPriority = priority; }
    }
    return pending.splice(selected, 1)[0];
  };
  const pump = () => {
    while (active < limit && pending.length) {
      const item = takeNext();
      if (item.signal?.aborted) { item.cleanup(); item.reject(abortError()); continue; }
      item.cleanup();
      active += 1;
      Promise.resolve().then(item.task).then(item.resolve, item.reject).finally(() => { active -= 1; pump(); });
    }
  };
  return {
    run(task, {signal, priority = 0} = {}) {
      if (typeof task !== 'function') return Promise.reject(new Error('队列任务无效。'));
      if (signal?.aborted) return Promise.reject(abortError());
      return new Promise((resolve, reject) => {
        const item = {task, signal, priority, resolve, reject, cleanup: () => {}};
        if (signal) {
          const listener = () => {
            const index = pending.indexOf(item);
            if (index < 0) return;
            pending.splice(index, 1);
            item.cleanup();
            reject(abortError());
          };
          signal.addEventListener('abort', listener, {once: true});
          item.cleanup = () => signal.removeEventListener('abort', listener);
        }
        pending.push(item);
        pump();
      });
    },
    cancelPending() {
      for (const item of pending.splice(0)) { item.cleanup(); item.reject(abortError()); }
    },
    snapshot() { return {active, pending: pending.length, limit}; },
  };
}

function platformRequestTimeout(path, method = 'GET') {
  const verb = String(method).toUpperCase();
  const route = String(path);
  if (verb === 'POST' && route === '/v1/parallel-modeling/searches') return SEARCH_PLATFORM_REQUEST_TIMEOUT_MS;
  if (verb === 'POST' && route === '/v1/desktop-capture/import-runtime-trace') return SEARCH_PLATFORM_REQUEST_TIMEOUT_MS;
  if (verb === 'POST' && (/^\/v1\/code-graphs\/(?:estimates|builds)$/.test(route)
      || /^\/v1\/code-graphs\/[^/]+\/confirm$/.test(route))) return CODE_GRAPH_REQUEST_TIMEOUT_MS;
  return DEFAULT_PLATFORM_REQUEST_TIMEOUT_MS;
}

function utf8ToBase64(value) {
  const bytes = new TextEncoder().encode(String(value));
  let binary = '';
  for (let offset = 0; offset < bytes.length; offset += 0x8000)
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  return btoa(binary);
}

function base64ToUtf8(value) {
  const binary = atob(value);
  const bytes = Uint8Array.from(binary, character => character.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

function base64ToBytes(value) {
  const binary = atob(value);
  return Uint8Array.from(binary, character => character.charCodeAt(0));
}

async function gzipBase64ToUtf8(value) {
  if (typeof DecompressionStream !== 'function')
    throw new Error('当前工作台无法解压服务器响应，请更新客户端后重试。');
  const stream = new Blob([base64ToBytes(value)]).stream().pipeThrough(new DecompressionStream('gzip'));
  return new Response(stream).text();
}

function requestBody(body) {
  if (body === undefined || body === null || body === '') return {};
  if (typeof body !== 'string') throw new Error('当前操作只接受 JSON 请求。');
  let value;
  try { value = JSON.parse(body); }
  catch { throw new Error('请求内容不是有效 JSON。'); }
  if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error('请求内容必须是 JSON 对象。');
  return value;
}

function serverProfileInitializationRequired(result) {
  if (Number(result?.exitCode) !== SERVER_PROFILE_UNINITIALIZED_EXIT_CODE) return false;
  return /(?:服务器环境尚未初始化|服务器环境配置已更新|HETERO_REPO_ROOT 未配置|HETERO_PYTHON_BIN 未配置)/.test(String(result?.stderr || ''));
}

function serverProfileContextKey(value = {}) {
  const roots = Array.isArray(value?.sourceRoots) ? value.sourceRoots.map(item => String(item || '')) : [];
  return JSON.stringify([
    String(value?.profileId || 'default'), String(value?.workerRepoRoot || ''), roots,
    String(value?.openHarmonySdkRoot || ''), String(value?.llvmBinRoot || ''),
    String(value?.glslangBin || ''), String(value?.traceStreamer || ''),
    String(value?.runtimeLibraryRoot || ''), String(value?.pythonBin || 'python3'),
  ]);
}

function resolvePlatformContext(state) {
  if (state?.ssh?.state !== 'connected') throw new Error('请先连接 SSH 服务器，再读取服务器上的分析数据。');
  const expectedTarget = normalizeSshTarget(state.ssh);
  if (!hasSshTarget(expectedTarget)) throw new Error('当前 SSH 连接缺少完整目标信息，请重新连接。');
  if (!sameSshTarget(expectedTarget, state.settings?.serverProfileTarget))
    throw new Error('服务器环境与当前 SSH 目标不一致，请在当前连接上重新检查环境。');
  return {
    expectedTarget,
    profileId: state.settings?.serverProfile?.profileId || 'default',
    scopeId: state.settings?.serverProfileScopeId || '',
    profileKey: serverProfileContextKey(state.settings?.serverProfile),
  };
}

function platformContextKey(state) {
  const connectedTarget = state?.ssh?.state === 'connected' && hasSshTarget(state.ssh)
    ? serverProfileTargetKey(state.ssh) : '';
  const profileTarget = hasSshTarget(state?.settings?.serverProfileTarget)
    ? serverProfileTargetKey(state.settings.serverProfileTarget) : '';
  return JSON.stringify([
    connectedTarget,
    profileTarget,
    String(state?.settings?.serverProfileScopeId || ''),
    serverProfileContextKey(state?.settings?.serverProfile),
  ]);
}

function createPlatformContextMonitor(getState, onChange) {
  if (typeof getState !== 'function' || typeof onChange !== 'function') throw new Error('共享页面上下文监视器配置无效。');
  let current = platformContextKey(getState());
  return {
    sync() {
      const next = platformContextKey(getState());
      if (next === current) return false;
      const previous = current;
      current = next;
      onChange({previous, current: next});
      return true;
    },
    key() { return current; },
  };
}

function assertPlatformContextUnchanged(snapshot, state) {
  let current;
  try { current = resolvePlatformContext(state); }
  catch { throw new Error('请求期间服务器连接或环境配置已改变，已丢弃旧结果。'); }
  if (!sameSshTarget(snapshot?.expectedTarget, current.expectedTarget)
      || snapshot?.profileId !== current.profileId
      || snapshot?.scopeId !== current.scopeId
      || snapshot?.profileKey !== current.profileKey)
    throw new Error('请求期间服务器连接或环境配置已改变，已丢弃旧结果。');
  return current;
}

function buildPlatformRequest({profileId, scopeId, profile, platformWorkerRoot = '', path, method = 'GET', body, streaming = false} = {}) {
  const id = String(profileId || 'default').trim();
  if (!PROFILE_PATTERN.test(id)) throw new Error('服务器环境 Profile ID 无效。');
  const scope = String(scopeId || '').trim();
  if (!SCOPE_PATTERN.test(scope)) throw new Error('服务器环境目标作用域无效；请重新连接并初始化当前服务器环境。');
  const requestMethod = String(method || 'GET').toUpperCase();
  const requestPath = String(path || '');
  if (requestMethod === 'POST' && requestPath === '/v1/parallel-calibration/fits')
    throw new Error('Desktop 校准实测必须通过受管暂存上传，不允许将文件内容直接写入 SSH 请求。');
  const request = {schema_version: RPC_SCHEMA, method: requestMethod, path: requestPath, body: requestBody(body)};
  const encoded = utf8ToBase64(JSON.stringify(request));
  const expectedWorkerRepo = typeof profile?.workerRepoRoot === 'string' ? profile.workerRepoRoot.trim() : '';
  const expectedPython = typeof profile?.pythonBin === 'string' ? profile.pythonBin.trim() : '';
  const managedWorkerRoot = String(platformWorkerRoot || '');
  if (managedWorkerRoot && (!managedWorkerRoot.startsWith('/') || managedWorkerRoot.includes('\0') || /[\r\n]/.test(managedWorkerRoot)))
    throw new Error('托管分析 Worker 路径无效。');
  const command = [
    'set -eu',
    // executeRemote starts this shell in the SSH connection working directory.
    // Preserve it before sourcing the managed profile: the build-worker repo in
    // HETERO_REPO_ROOT and the Desktop platform repo are allowed to be different.
    'connection_root="$PWD"',
    `env_file="$HOME/.local/share/heterogeneous-workbench/server-profiles/targets/${scope}/${id}/platform.env"`,
    'test -f "$env_file" || { printf \'服务器环境尚未初始化：%s\\n\' "$env_file" >&2; exit 78; }',
    'set -a',
    '. "$env_file"',
    'set +a',
    'expand_rpc_path() { case "$1" in "~") printf \'%s\\n\' "$HOME" ;; "~/"*) printf \'%s/%s\\n\' "$HOME" "${1#~/}" ;; *) printf \'%s\\n\' "$1" ;; esac; }',
    'resolve_python() { case "$1" in */*) expand_rpc_path "$1" ;; *) command -v -- "$1" 2>/dev/null || printf \'%s\\n\' "$1" ;; esac; }',
    `expected_worker_repo=${shellQuote(expectedWorkerRepo)}`,
    `expected_python=${shellQuote(expectedPython)}`,
    'if [ -n "$expected_worker_repo" ] && [ "$(expand_rpc_path "${HETERO_REPO_ROOT:-}")" != "$(expand_rpc_path "$expected_worker_repo")" ]; then printf \'服务器环境配置已更新：异构工作仓目录需要重新初始化。\\n\' >&2; exit 78; fi',
    'if [ -n "$expected_python" ] && [ "$(resolve_python "${HETERO_PYTHON_BIN:-}")" != "$(resolve_python "$expected_python")" ]; then printf \'服务器环境配置已更新：Python 路径需要重新初始化。\\n\' >&2; exit 78; fi',
    'test -n "${HETERO_PYTHON_BIN:-}" || { printf \'HETERO_PYTHON_BIN 未配置。\\n\' >&2; exit 78; }',
    'rpc_roots=()',
    'rpc_root_count=0',
    'add_rpc_root() {',
    '  local candidate existing',
    '  candidate="$(expand_rpc_path "$1")"',
    '  [ -n "$candidate" ] && [ -f "$candidate/backend/tools/desktop_platform_rpc.py" ] || return 0',
    '  for existing in "${rpc_roots[@]-}"; do [ -n "$existing" ] && [ "$existing" = "$candidate" ] && return 0; done',
    '  rpc_roots+=("$candidate")',
    '  rpc_root_count=$((rpc_root_count + 1))',
    '}',
    // The SSH cwd is part of the fenced target identity and normally points at
    // the Desktop checkout. Prefer it over the independently configured build
    // worker repository unless an explicit platform root exists.
    `add_rpc_root ${shellQuote(managedWorkerRoot)}`,
    'add_rpc_root "${HETERO_PLATFORM_REPO_ROOT:-}"',
    'add_rpc_root "$connection_root"',
    'if command -v git >/dev/null 2>&1; then',
    '  git_root="$(git -C "$connection_root" rev-parse --show-toplevel 2>/dev/null || true)"',
    '  if [ -n "$git_root" ]; then add_rpc_root "$git_root"; fi',
    'fi',
    'add_rpc_root "${HETERO_REPO_ROOT:-}"',
    `if [ "$rpc_root_count" -eq 0 ]; then printf '${RPC_REPAIR_MARKER}REMOTE_WORKER_MISSING\\t未找到服务器分析服务：已检查当前工作目录与已配置的工作仓。\\n' >&2; exit ${REMOTE_WORKER_UNAVAILABLE_EXIT_CODE}; fi`,
    'rpc_root=""',
    'python_bin=""',
    `managed_analysis_python="$HOME/.local/share/heterogeneous-workbench/server-profiles/targets/${scope}/${id}/assets/code-analysis-runtime/tree-sitter-c-cpp-jsonschema-v2/bin/python"`,
    'for root_candidate in "${rpc_roots[@]}"; do',
    '  for python_candidate in "$managed_analysis_python" "$HETERO_PYTHON_BIN" "$root_candidate/.venv/bin/python" "$root_candidate/venv/bin/python"; do',
    '    python_candidate="$(resolve_python "$python_candidate")"',
    '    [ -n "$python_candidate" ] && [ -x "$python_candidate" ] || continue',
    '    if (cd -- "$root_candidate" && PYTHONPATH="$root_candidate${PYTHONPATH:+:$PYTHONPATH}" PYTHONDONTWRITEBYTECODE=1 "$python_candidate" -c \'import sys; from backend.tools.desktop_platform_rpc import SCHEMA_VERSION; raise SystemExit(0 if sys.version_info >= (3, 10) and SCHEMA_VERSION == "desktop_platform_rpc/v2" else 3)\') >/dev/null 2>&1; then rpc_root="$root_candidate"; python_bin="$python_candidate"; break 2; fi',
    '  done',
    'done',
    `if [ -z "$python_bin" ]; then printf '${RPC_REPAIR_MARKER}REMOTE_WORKER_PYTHON_UNAVAILABLE\\t服务器分析服务的 Python 环境不可用；已检查配置解释器与仓库虚拟环境。\\n' >&2; diagnostic_root="\${rpc_roots[0]}"; diagnostic_python="$(resolve_python "$HETERO_PYTHON_BIN")"; if [ -x "$diagnostic_python" ]; then (cd -- "$diagnostic_root" && PYTHONPATH="$diagnostic_root\${PYTHONPATH:+:$PYTHONPATH}" PYTHONDONTWRITEBYTECODE=1 "$diagnostic_python" -c 'from backend.tools.desktop_platform_rpc import SCHEMA_VERSION') >&2 || true; fi; exit ${REMOTE_WORKER_UNAVAILABLE_EXIT_CODE}; fi`,
    'cd -- "$rpc_root"',
    streaming
      ? 'PYTHONPATH="$rpc_root${PYTHONPATH:+:$PYTHONPATH}" PYTHONDONTWRITEBYTECODE=1 "$python_bin" -m backend.tools.desktop_platform_rpc --stdio-server'
      : `PYTHONPATH="$rpc_root\${PYTHONPATH:+:$PYTHONPATH}" PYTHONDONTWRITEBYTECODE=1 "$python_bin" -m backend.tools.desktop_platform_rpc --request-base64 ${shellQuote(encoded)}`,
  ].join('\n');
  if (new TextEncoder().encode(command).length >= MAX_REMOTE_COMMAND_BYTES)
    throw new Error('请求内容超过 32 KiB。请缩小补充数据，或改用文件导入。');
  return {request, command, streaming: Boolean(streaming)};
}

function remoteWorkerRepair(result) {
  if (Number(result?.exitCode) !== REMOTE_WORKER_UNAVAILABLE_EXIT_CODE) return null;
  const stderr = String(result?.stderr || '');
  const match = stderr.match(/(?:^|\n)HETERO_DESKTOP_REPAIRABLE(?:\t|_)(REMOTE_WORKER_(?:MISSING|PYTHON_UNAVAILABLE))(?:\t|[ :]+)([^\n]*)/);
  if (!match) return null;
  return {code: match[1], message: match[2].trim() || '服务器分析 Worker 不可用。'};
}

function remoteWorkerRecoveryRequired(result) {
  return Boolean(remoteWorkerRepair(result));
}

function platformResultError(result) {
  const repair = remoteWorkerRepair(result);
  if (repair) {
    const error = new Error(repair.message);
    error.code = repair.code;
    error.repairKind = repair.code === 'REMOTE_WORKER_MISSING' ? 'worker' : 'python_environment';
    return error;
  }
  return new Error(String(result?.stderr || '').trim() || `服务器分析请求失败（exit ${result?.exitCode}）。`);
}

async function recoverRemoteWorker(result, {deploy, retry} = {}) {
  if (!remoteWorkerRecoveryRequired(result)) return {result, root: ''};
  let deployed;
  try { deployed = await deploy(); }
  catch (error) {
    const detail = String(error?.message || error || '未知错误')
      .replace(/^Error invoking remote method '[^']+': Error: /, '')
      .replace(/HETERO_[A-Z0-9_]+(?:\t[A-Z0-9_]+)?\t?/g, '')
      .trim() || '未知错误';
    const failure = new Error(`平台未能自动准备内置分析 Worker：${detail}`);
    failure.code = 'DESKTOP_PLATFORM_WORKER_UNAVAILABLE';
    failure.repairKind = 'worker';
    throw failure;
  }
  if (!deployed?.root) {
    const failure = new Error('平台未能自动准备内置分析 Worker：部署结果缺少托管路径。');
    failure.code = 'DESKTOP_PLATFORM_WORKER_UNAVAILABLE';
    failure.repairKind = 'worker';
    throw failure;
  }
  return {result: await retry(deployed.root), root: deployed.root};
}

async function parsePlatformResponse(stdout) {
  const line = String(stdout || '').split(/\r?\n/).reverse().find(value => value.startsWith(RPC_MARKER) || value.startsWith(RPC_GZIP_MARKER));
  if (!line) throw new Error('服务器没有返回分析结果；请检查工作仓版本和 Python 环境。');
  let response;
  try {
    const compressed = line.startsWith(RPC_GZIP_MARKER);
    const marker = compressed ? RPC_GZIP_MARKER : RPC_MARKER;
    const json = compressed ? await gzipBase64ToUtf8(line.slice(marker.length)) : base64ToUtf8(line.slice(marker.length));
    response = JSON.parse(json);
  }
  catch { throw new Error('服务器返回的分析结果无效。'); }
  if (!response || response.schema_version !== RPC_SCHEMA || typeof response.ok !== 'boolean') throw new Error('服务器返回的分析结果版本不兼容，请更新工作台或服务器工作仓。');
  return response;
}

module.exports = {CODE_GRAPH_REQUEST_TIMEOUT_MS, DEFAULT_PLATFORM_REQUEST_CONCURRENCY, DEFAULT_PLATFORM_REQUEST_TIMEOUT_MS, MAX_REMOTE_COMMAND_BYTES, REMOTE_WORKER_UNAVAILABLE_EXIT_CODE, RPC_GZIP_MARKER, RPC_MARKER, RPC_REPAIR_MARKER, RPC_SCHEMA, SEARCH_PLATFORM_REQUEST_TIMEOUT_MS, SERVER_PROFILE_UNINITIALIZED_EXIT_CODE, assertPlatformContextUnchanged, buildPlatformRequest, createConcurrentTaskQueue, createPlatformContextMonitor, parsePlatformResponse, platformContextKey, platformRequestTimeout, platformResultError, recoverRemoteWorker, remoteWorkerRecoveryRequired, remoteWorkerRepair, requestBody, resolvePlatformContext, serverProfileContextKey, serverProfileInitializationRequired};
