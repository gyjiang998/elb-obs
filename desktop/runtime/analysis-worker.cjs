'use strict';

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const SCHEMA = 'heterogeneous_desktop_analysis_worker/v1';
const ROOT_MARKER = 'HETERO_ANALYSIS_WORKER_ROOT\t';
const READY_MARKER = 'HETERO_ANALYSIS_WORKER_READY\t';
const STATUS_MARKER = 'HETERO_ANALYSIS_WORKER_STATUS\t';
const MAX_FILES = 256;
const MAX_BYTES = 64 * 1024 * 1024;
const IDENTITY = /^[a-f0-9]{64}$/;

function sha256File(filename) {
  const digest = crypto.createHash('sha256');
  const descriptor = fs.openSync(filename, 'r');
  const buffer = Buffer.allocUnsafe(128 * 1024);
  try {
    for (;;) {
      const count = fs.readSync(descriptor, buffer, 0, buffer.length, null);
      if (!count) break;
      digest.update(buffer.subarray(0, count));
    }
  } finally { fs.closeSync(descriptor); }
  return digest.digest('hex');
}

function safeRelative(value) {
  if (typeof value !== 'string' || !value || value.includes('\\') || value.includes('\0') || path.posix.isAbsolute(value)) return false;
  const parts = value.split('/');
  return parts.every(part => part && part !== '.' && part !== '..')
    && ['agent-resources', 'backend', 'data', 'tools'].includes(parts[0]);
}

function resolveAnalysisWorkerRoot({resourcesDir, resourcesPath = process.resourcesPath} = {}) {
  const candidates = [
    resourcesPath && path.join(resourcesPath, 'analysis-worker'),
    resourcesDir && path.join(resourcesDir, 'build/resources/analysis-worker'),
    resourcesDir && path.join(resourcesDir, 'analysis-worker'),
  ].filter(Boolean);
  for (const candidate of candidates) {
    try {
      if (fs.statSync(path.join(candidate, 'analysis-worker-manifest.json')).isFile()) return candidate;
    } catch {}
  }
  throw new Error('Desktop 安装中缺少内置分析 Worker；请更新或重新安装当前版本。');
}

function loadAnalysisWorkerBundle(options = {}) {
  const root = resolveAnalysisWorkerRoot(options);
  const desktopVersion = String(options.desktopVersion || '');
  if (!desktopVersion) throw new Error('校验内置分析 Worker 时缺少 Desktop 版本。');
  const rootAttributes = fs.lstatSync(root);
  const manifestPath = path.join(root, 'analysis-worker-manifest.json');
  const manifestAttributes = fs.lstatSync(manifestPath);
  if (!rootAttributes.isDirectory() || rootAttributes.isSymbolicLink()
      || !manifestAttributes.isFile() || manifestAttributes.isSymbolicLink()
      || manifestAttributes.size > 1024 * 1024)
    throw new Error('内置分析 Worker 资源路径不安全。');
  let manifest;
  try { manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8')); }
  catch (error) { throw new Error(`内置分析 Worker 清单无法读取：${error.message}`); }
  if (manifest?.schema_version !== SCHEMA || !IDENTITY.test(String(manifest?.identity || ''))
      || typeof manifest?.version !== 'string' || !manifest.version
      || !Array.isArray(manifest?.files) || manifest.files.length < 1 || manifest.files.length > MAX_FILES)
    throw new Error('内置分析 Worker 清单无效。');
  if (manifest.version !== desktopVersion)
    throw new Error(`内置分析 Worker 版本 ${manifest.version} 与 Desktop ${desktopVersion} 不一致。`);
  const seen = new Set();
  let total = 0;
  const files = manifest.files.map(record => {
    const relative = record?.path;
    const size = Number(record?.size);
    const sha256 = String(record?.sha256 || '');
    if (!safeRelative(relative) || seen.has(relative) || !Number.isSafeInteger(size) || size < 0
        || !IDENTITY.test(sha256)) throw new Error('内置分析 Worker 清单包含无效文件记录。');
    seen.add(relative); total += size;
    if (total > MAX_BYTES) throw new Error('内置分析 Worker 超过 64 MiB 安全上限。');
    const parts = relative.split('/');
    let parent = root;
    for (const part of parts.slice(0, -1)) {
      parent = path.join(parent, part);
      const attributes = fs.lstatSync(parent);
      if (!attributes.isDirectory() || attributes.isSymbolicLink())
        throw new Error(`内置分析 Worker 目录不安全：${relative}`);
    }
    const filename = path.join(root, ...parts);
    const attributes = fs.lstatSync(filename);
    if (!attributes.isFile() || attributes.isSymbolicLink() || attributes.size !== size || sha256File(filename) !== sha256)
      throw new Error(`内置分析 Worker 文件校验失败：${relative}`);
    return {path: relative, size, sha256, filename};
  });
  for (const required of ['backend/__init__.py', 'backend/tools/desktop_platform_rpc.py',
    'backend/desktop_workspace_rpc.py', 'backend/capture_runtime_trace.py',
    'backend/htrace_analysis.py', 'backend/hitrace_instrumentation.py', 'backend/trace_modules.py',
    'backend/custom_api_kb.py', 'backend/code_graph.py', 'backend/skill_versions.py',
    'backend/application_flow.py', 'backend/application_flow_trace.py',
    'backend/experience_evolution.py', 'backend/parallel_modeling/manager.py',
    'data/hitrace_tag_source_scope_hints.json',
    'data/parallel-modeling/provenance.json',
    'data/knowledge-bases/optimization/optimization_knowledge_base.json',
    'data/knowledge-bases/hardware/hardware_knowledge_base.json',
    'data/knowledge-bases/hardware/npu_api_knowledge_base.json',
    'data/knowledge-bases/hardware/gpu_api_knowledge_base.json',
    'agent-resources/skill-library/skills/custom-accelerator-api-kb-builder/2.0.0.md',
    'agent-resources/skill-library/profiles/agent-centric-page-device-selection-2026-10-26.json',
    'agent-resources/skills/application-execution-flow-reconstruction/SKILL.md',
    'agent-resources/skills/application-execution-flow-reconstruction/references/application-flow-scenario-v2.schema.json'])
    if (!seen.has(required)) throw new Error(`内置分析 Worker 缺少必要文件：${required}`);
  const identityPayload = JSON.stringify({version: manifest.version,
    files: files.map(({path: relative, size, sha256}) => ({path: relative, size, sha256}))});
  if (crypto.createHash('sha256').update(identityPayload).digest('hex') !== manifest.identity)
    throw new Error('内置分析 Worker 清单身份校验失败。');
  return {root, manifest: {...manifest, files: files.map(({filename: _filename, ...record}) => record)}, files,
    manifestPath, totalBytes: total};
}

// The integrity verifier never follows links and accepts exactly the
// manifest-declared files. Readiness is checked separately so a valid bundle
// is retained when the selected Python environment is temporarily unusable.
const REMOTE_INTEGRITY_PROGRAM = String.raw`
import hashlib,json,os,pathlib,stat,sys
root=pathlib.Path(sys.argv[1])
identity=sys.argv[2]
manifest_path=root/'analysis-worker-manifest.json'
root_info=root.lstat()
if not stat.S_ISDIR(root_info.st_mode) or root.is_symlink(): raise SystemExit('analysis worker root is not a real directory')
manifest_info=manifest_path.lstat()
if not stat.S_ISREG(manifest_info.st_mode) or manifest_path.is_symlink() or manifest_info.st_size>1048576: raise SystemExit('analysis worker manifest is unsafe')
manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
records=manifest.get('files')
if manifest.get('schema_version')!='heterogeneous_desktop_analysis_worker/v1' or manifest.get('identity')!=identity or not isinstance(manifest.get('version'),str) or not manifest.get('version') or len(manifest.get('version'))>64 or not isinstance(records,list) or not 1<=len(records)<=256: raise SystemExit('analysis worker manifest contract is invalid')
declared=set()
total=0
validated=[]
for record in records:
    if not isinstance(record,dict): raise SystemExit('analysis worker file record is invalid')
    relative=record.get('path')
    size=record.get('size')
    digest_text=record.get('sha256')
    parts=relative.split('/') if isinstance(relative,str) else []
    if not parts or parts[0] not in ('agent-resources','backend','data','tools') or any(part in ('','.', '..') for part in parts) or '\\' in relative or relative.startswith('/') or relative in declared: raise SystemExit('analysis worker file path is invalid')
    if isinstance(size,bool) or not isinstance(size,int) or size<0 or size>67108864: raise SystemExit('analysis worker file size is invalid')
    if not isinstance(digest_text,str) or len(digest_text)!=64 or any(character not in '0123456789abcdef' for character in digest_text): raise SystemExit('analysis worker file hash is invalid')
    declared.add(relative); total+=size
    if total>67108864: raise SystemExit('analysis worker bundle is too large')
    validated.append((record,parts))
payload=json.dumps({'version':manifest.get('version'),'files':records},ensure_ascii=False,separators=(',',':')).encode()
if hashlib.sha256(payload).hexdigest()!=identity: raise SystemExit('analysis worker identity is invalid')
expected_children={():{'analysis-worker-manifest.json'}}
for record,parts in validated:
    for index,part in enumerate(parts): expected_children.setdefault(tuple(parts[:index]),set()).add(part)
for directory_parts,expected in expected_children.items():
    directory=root.joinpath(*directory_parts)
    directory_info=directory.lstat()
    if not stat.S_ISDIR(directory_info.st_mode) or directory.is_symlink(): raise SystemExit('analysis worker directory is unsafe')
    actual=set()
    with os.scandir(directory) as entries:
        for entry in entries:
            actual.add(entry.name)
            if len(actual)>257: raise SystemExit('analysis worker directory has too many entries')
    if actual!=expected: raise SystemExit('analysis worker directory contents do not match manifest')
for record,parts in validated:
    target=root.joinpath(*parts)
    parent=root
    for part in parts[:-1]:
        parent=parent/part
        parent_info=parent.lstat()
        if not stat.S_ISDIR(parent_info.st_mode) or parent.is_symlink(): raise SystemExit('analysis worker parent directory is unsafe')
    info=target.lstat()
    if not stat.S_ISREG(info.st_mode) or target.is_symlink() or info.st_size!=record.get('size'): raise SystemExit('analysis worker file is unsafe')
    digest=hashlib.sha256()
    with target.open('rb') as source:
        for chunk in iter(lambda: source.read(131072),b''): digest.update(chunk)
    if digest.hexdigest()!=record.get('sha256'): raise SystemExit('analysis worker file hash does not match manifest')
`.trim();

const REMOTE_READINESS_PROGRAM = String.raw`
import pathlib,sys
root=pathlib.Path(sys.argv[1])
marker='HETERO_ANALYSIS_WORKER_STATUS\t'
if sys.version_info < (3,10):
    print(marker+'PYTHON_UNSUPPORTED\t需要 Python 3.10 或更高版本。')
    raise SystemExit(0)
sys.path.insert(0,str(root))
try:
    from backend.tools.desktop_platform_rpc import SCHEMA_VERSION
    from backend.desktop_workspace_rpc import _knowledge_payload
    from backend.skill_versions import skill_version_library
    optimization=_knowledge_payload('optimization')
    hardware=_knowledge_payload('hardware')
    skills=skill_version_library()
    if not optimization.get('records') or not hardware.get('records') or not skills.get('profiles'):
        raise RuntimeError('workspace catalogs are empty')
except Exception as error:
    detail=(type(error).__name__+': '+str(error)).replace('\t',' ').replace('\r',' ').replace('\n',' ')[:500]
    print(marker+'DEPENDENCY_UNAVAILABLE\t'+detail)
    raise SystemExit(0)
if SCHEMA_VERSION!='desktop_platform_rpc/v2':
    print(marker+'SCHEMA_MISMATCH\tDesktop RPC 契约版本不兼容。')
else:
    print(marker+'READY\t分析 Worker 已就绪。')
`.trim();

function shellQuote(value) { return `'${String(value).replace(/'/g, `'"'"'`)}'`; }

function pythonResolver(configured) {
  return [
    `configured_python=${shellQuote(configured || 'python3')}`,
    'case "$configured_python" in',
    '  "~/"*) python_bin="$HOME/${configured_python#~/}" ;;',
    '  /*) python_bin="$configured_python" ;;',
    '  *) python_bin="$(command -v -- "$configured_python" 2>/dev/null || true)" ;;',
    'esac',
    'test -n "$python_bin" && test -x "$python_bin" || { printf \'配置的 Python 不可执行：%s\\n\' "$configured_python" >&2; exit 4; }',
  ].join('\n');
}

function managedWorkerSuffix(scopeId, profileId) {
  if (!/^[a-f0-9]{16}$/.test(scopeId) || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(profileId))
    throw new Error('分析 Worker 的服务器 Profile 标识无效。');
  return `/.local/share/heterogeneous-workbench/server-profiles/targets/${scopeId}/${profileId}/assets/analysis-worker`;
}

function buildAnalysisWorkerRootScript({scopeId, profileId} = {}) {
  const suffix = managedWorkerSuffix(scopeId, profileId);
  const parts = suffix.slice(1).split('/');
  return [
    'set -eu', 'umask 077', 'home_root="$(cd -P -- "$HOME" && pwd -P)"', 'current="$home_root"',
    ...parts.map((part, index) => index === parts.length - 1
      ? `current="$current/${part}"; if [ -L "$current" ]; then printf '分析 Worker 托管路径不能包含符号链接：%s\\n' "$current" >&2; exit 6; elif [ -e "$current" ]; then test -d "$current" || { printf '分析 Worker 托管路径不是目录：%s\\n' "$current" >&2; exit 6; }; elif ! mkdir -- "$current" 2>/dev/null; then if [ -L "$current" ]; then printf '分析 Worker 托管路径不能包含符号链接：%s\\n' "$current" >&2; exit 6; else test -d "$current" || { printf '无法创建分析 Worker 托管目录：%s\\n' "$current" >&2; exit 6; }; fi; fi`
      : `current="$current/${part}"; if [ -L "$current" ]; then printf '分析 Worker 托管路径不能包含符号链接：%s\\n' "$current" >&2; exit 6; else test -d "$current" || { printf '服务器 Profile 托管目录尚未初始化：%s\\n' "$current" >&2; exit 6; }; fi`),
    'root="$current"', 'chmod 0700 "$root"',
    `printf '${ROOT_MARKER}%s\\n' "$root"`,
  ].join('\n');
}

function parseAnalysisWorkerRoot(stdout, {scopeId, profileId} = {}) {
  const line = String(stdout || '').split(/\r?\n/).find(value => value.startsWith(ROOT_MARKER));
  const value = line?.slice(ROOT_MARKER.length) || '';
  const suffix = managedWorkerSuffix(scopeId, profileId);
  if (!value.startsWith('/') || value.includes('\0') || /[\r\n]/.test(value)
      || path.posix.normalize(value) !== value || !value.endsWith(suffix))
    throw new Error('服务器返回的分析 Worker 托管路径无效。');
  return value;
}

function buildAnalysisWorkerVerifyScript({root, identity, pythonBin} = {}) {
  if (!IDENTITY.test(String(identity || ''))) throw new Error('分析 Worker 身份无效。');
  return [
    'set -eu', pythonResolver(pythonBin),
    `worker_root=${shellQuote(root)}`,
    'test -d "$worker_root" || exit 3',
    `PYTHONDONTWRITEBYTECODE=1 "$python_bin" - "$worker_root" ${shellQuote(identity)} <<'HETERO_WORKER_INTEGRITY'`,
    REMOTE_INTEGRITY_PROGRAM,
    'HETERO_WORKER_INTEGRITY',
    `readiness="$(PYTHONDONTWRITEBYTECODE=1 "$python_bin" - "$worker_root" <<'HETERO_WORKER_READINESS'`,
    REMOTE_READINESS_PROGRAM,
    'HETERO_WORKER_READINESS',
    ')"',
    `case "$readiness" in ${shellQuote(`${STATUS_MARKER}READY`)}$'\t'*) ;; *) printf '%s\\n' "$readiness" >&2; exit 7 ;; esac`,
    `printf '${READY_MARKER}%s\\n' "$worker_root"`,
  ].join('\n');
}

function buildAnalysisWorkerInspectScript({root, identity, pythonBin} = {}) {
  if (!IDENTITY.test(String(identity || ''))) throw new Error('分析 Worker 身份无效。');
  return [
    'set -u',
    `configured_python=${shellQuote(pythonBin || 'python3')}`,
    'case "$configured_python" in',
    '  "~/"*) python_bin="$HOME/${configured_python#~/}" ;;',
    '  /*) python_bin="$configured_python" ;;',
    '  *) python_bin="$(command -v -- "$configured_python" 2>/dev/null || true)" ;;',
    'esac',
    `worker_root=${shellQuote(root)}`,
    `if [ -z "$python_bin" ] || [ ! -x "$python_bin" ]; then printf '${STATUS_MARKER}PYTHON_UNAVAILABLE\\t配置的 Python 不可执行。\\n'; exit 0; fi`,
    `if [ ! -e "$worker_root" ]; then printf '${STATUS_MARKER}ABSENT\\t尚未部署当前版本。\\n'; exit 0; fi`,
    `if [ -L "$worker_root" ] || [ ! -d "$worker_root" ]; then printf '${STATUS_MARKER}CORRUPT\\t托管路径不是安全目录。\\n'; exit 0; fi`,
    `if ! PYTHONDONTWRITEBYTECODE=1 "$python_bin" - "$worker_root" ${shellQuote(identity)} >/dev/null 2>&1 <<'HETERO_WORKER_INTEGRITY'`,
    REMOTE_INTEGRITY_PROGRAM,
    'HETERO_WORKER_INTEGRITY',
    'then',
    `  printf '${STATUS_MARKER}CORRUPT\\t已部署文件未通过完整性校验。\\n'`,
    '  exit 0',
    'fi',
    `PYTHONDONTWRITEBYTECODE=1 "$python_bin" - "$worker_root" <<'HETERO_WORKER_READINESS'`,
    REMOTE_READINESS_PROGRAM,
    'HETERO_WORKER_READINESS',
  ].join('\n');
}

function parseAnalysisWorkerStatus(stdout) {
  const line = String(stdout || '').split(/\r?\n/).reverse().find(value => value.startsWith(STATUS_MARKER));
  const fields = line?.slice(STATUS_MARKER.length).split('\t') || [];
  const status = fields.shift() || '';
  if (!['ABSENT', 'CORRUPT', 'PYTHON_UNAVAILABLE', 'PYTHON_UNSUPPORTED', 'DEPENDENCY_UNAVAILABLE', 'SCHEMA_MISMATCH', 'READY'].includes(status))
    throw new Error('服务器没有返回有效的分析 Worker 状态。');
  return {status, message: fields.join(' ').trim()};
}

function buildAnalysisWorkerPrepareScript({baseRoot, stagingRoot, files} = {}) {
  const directories = new Set([stagingRoot]);
  for (const record of files || []) {
    if (!safeRelative(record.path)) throw new Error('分析 Worker 文件路径无效。');
    let relative = path.posix.dirname(record.path);
    while (relative && relative !== '.') {
      directories.add(`${stagingRoot}/${relative}`);
      relative = path.posix.dirname(relative);
    }
  }
  const expectedPrefix = `${baseRoot}/.partial-`;
  if (!stagingRoot.startsWith(expectedPrefix) || path.posix.dirname(stagingRoot) !== baseRoot)
    throw new Error('分析 Worker 暂存路径无效。');
  const values = [...directories].sort().map(shellQuote).join(' ');
  return ['set -eu', 'umask 077', `test -d ${shellQuote(baseRoot)} && test ! -L ${shellQuote(baseRoot)}`,
    `test "$(cd -P -- ${shellQuote(baseRoot)} && pwd -P)" = ${shellQuote(baseRoot)}`,
    `mkdir -p -- ${values}`, `chmod 0700 -- ${values}`].join('\n');
}

function buildAnalysisWorkerActivateScript({baseRoot, stagingRoot, finalRoot, identity, pythonBin} = {}) {
  if (!IDENTITY.test(String(identity || '')) || finalRoot !== `${baseRoot}/${identity}`
      || !stagingRoot.startsWith(`${baseRoot}/.partial-`) || path.posix.dirname(stagingRoot) !== baseRoot)
    throw new Error('分析 Worker 激活路径无效。');
  const quarantine = `${baseRoot}/.invalid-${identity.slice(0, 16)}-${path.posix.basename(stagingRoot).slice(-12)}`;
  const lock = `${baseRoot}/.deploy-lock-${identity}`;
  const staleLock = `${baseRoot}/.stale-lock-${identity.slice(0, 16)}-${path.posix.basename(stagingRoot).slice(-12)}`;
  return [
    'set -eu', 'umask 077', pythonResolver(pythonBin),
    `base=${shellQuote(baseRoot)}`,
    'test -d "$base" && test ! -L "$base"',
    'test "$(cd -P -- "$base" && pwd -P)" = "$base"',
    `staging=${shellQuote(stagingRoot)}`, `final=${shellQuote(finalRoot)}`, `quarantine=${shellQuote(quarantine)}`,
    `lock=${shellQuote(lock)}`, `stale_lock=${shellQuote(staleLock)}`,
    `PYTHONDONTWRITEBYTECODE=1 "$python_bin" - "$staging" ${shellQuote(identity)} <<'HETERO_STAGING_INTEGRITY'`,
    REMOTE_INTEGRITY_PROGRAM,
    'HETERO_STAGING_INTEGRITY',
    'locked=0',
    'cleanup_lock() { if [ "$locked" -eq 1 ]; then rmdir -- "$lock" 2>/dev/null || true; fi; }',
    'trap cleanup_lock EXIT HUP INT TERM',
    'attempt=0',
    'until mkdir -- "$lock" 2>/dev/null; do',
    '  attempt=$((attempt + 1))',
    // Recover only an old, empty, real directory left by a killed client.
    // Rename stays inside baseRoot, so no client removes a newly acquired lock.
    '  if [ "$attempt" -eq 1 ] || [ $((attempt % 50)) -eq 0 ]; then',
    `    PYTHONDONTWRITEBYTECODE=1 "$python_bin" - "$lock" "$stale_lock" <<'HETERO_STALE_LOCK' >/dev/null 2>&1 || true`,
    'import os,stat,sys,time',
    'lock,stale=sys.argv[1:3]',
    'try:',
    '    info=os.lstat(lock)',
    '    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or time.time()-info.st_mtime<=600: raise SystemExit(1)',
    '    os.rename(lock,stale)',
    '    try: os.rmdir(stale)',
    '    except OSError:',
    '        try: os.rename(stale,lock)',
    '        except OSError: pass',
    '        raise SystemExit(1)',
    'except (FileNotFoundError,FileExistsError,PermissionError,OSError): raise SystemExit(1)',
    'HETERO_STALE_LOCK',
    '  fi',
    '  if [ "$attempt" -ge 300 ]; then printf \'等待其他 Desktop 完成分析 Worker 部署超时（30 秒）；请稍后重试。\\n\' >&2; exit 8; fi',
    '  sleep 0.1',
    'done',
    'locked=1',
    // Recheck under the lock. Another Desktop may have installed the same
    // content-addressed version while this client uploaded its staging tree.
    'final_ok=0',
    `if [ -d "$final" ] && [ ! -L "$final" ] && PYTHONDONTWRITEBYTECODE=1 "$python_bin" - "$final" ${shellQuote(identity)} >/dev/null 2>&1 <<'HETERO_EXISTING_INTEGRITY'`,
    REMOTE_INTEGRITY_PROGRAM,
    'HETERO_EXISTING_INTEGRITY',
    'then final_ok=1; fi',
    'if [ "$final_ok" -eq 1 ]; then rm -rf -- "$staging"',
    'else',
    '  rm -rf -- "$quarantine"',
    '  if [ -e "$final" ] || [ -L "$final" ]; then mv -- "$final" "$quarantine"; fi',
    '  if ! mv -- "$staging" "$final"; then if [ -e "$quarantine" ] && [ ! -e "$final" ] && [ ! -L "$final" ]; then mv -- "$quarantine" "$final"; fi; exit 5; fi',
    'fi',
    // READY is emitted only after a complete verification of the exact final
    // tree selected under the deployment lock.
    `PYTHONDONTWRITEBYTECODE=1 "$python_bin" - "$final" ${shellQuote(identity)} <<'HETERO_FINAL_INTEGRITY'`,
    REMOTE_INTEGRITY_PROGRAM,
    'HETERO_FINAL_INTEGRITY',
    'rm -rf -- "$quarantine"',
    `readiness="$(PYTHONDONTWRITEBYTECODE=1 "$python_bin" - "$final" <<'HETERO_FINAL_READINESS'`,
    REMOTE_READINESS_PROGRAM,
    'HETERO_FINAL_READINESS',
    ')"',
    `case "$readiness" in ${shellQuote(`${STATUS_MARKER}READY`)}$'\t'*) ;; *) printf '%s\\n' "$readiness" >&2; exit 7 ;; esac`,
    'rmdir -- "$lock"', 'locked=0', 'trap - EXIT HUP INT TERM',
    `printf '${READY_MARKER}%s\\n' "$final"`,
  ].join('\n');
}

function parseAnalysisWorkerReady(stdout, expectedRoot) {
  const line = String(stdout || '').split(/\r?\n/).find(value => value.startsWith(READY_MARKER));
  if (line?.slice(READY_MARKER.length) !== expectedRoot) throw new Error('服务器没有确认分析 Worker 已就绪。');
  return expectedRoot;
}

module.exports = {
  READY_MARKER, ROOT_MARKER, SCHEMA, STATUS_MARKER, buildAnalysisWorkerActivateScript,
  buildAnalysisWorkerInspectScript, buildAnalysisWorkerPrepareScript, buildAnalysisWorkerRootScript,
  buildAnalysisWorkerVerifyScript, loadAnalysisWorkerBundle, managedWorkerSuffix,
  parseAnalysisWorkerReady, parseAnalysisWorkerRoot, parseAnalysisWorkerStatus, resolveAnalysisWorkerRoot,
};
