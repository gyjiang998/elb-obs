'use strict';

/*
 * Remote server environment contract used by the Desktop application.
 *
 * The inspection script is deliberately read-only.  It only resolves names
 * and tests paths.  The preparation script owns the one mutable surface: a
 * profile-specific directory below the remote user's XDG data directory and
 * its permission-0600 platform.env file.
 */

const SCHEMA_VERSION = 'server_profile_report/v1';
const RECORD_SEPARATOR = '\x1e';
const FIELD_SEPARATOR = '\x1f';
const MAX_PATH_BYTES = 4096;
// Profile values are embedded into the read-only SSH preflight script.  Keep
// the full contract comfortably below DesktopRuntime's 32 KiB command limit
// so a profile can never be saved successfully and then fail only at inspect.
const MAX_PROFILE_BYTES = 12 * 1024;
const MAX_REMOTE_SCRIPT_BYTES = 32 * 1024;
const SCOPE_PATTERN = /^[a-f0-9]{16}$/;
const PYTHON_SHA256_HELPER = [
  'def file_sha256(path):',
  '    digest = hashlib.sha256()',
  '    with path.open("rb") as stream:',
  '        for chunk in iter(lambda: stream.read(1024 * 1024), b""):',
  '            digest.update(chunk)',
  '    return digest.hexdigest()',
].join('\n');

const SDK_SURFACE = Object.freeze([
  ['sdk.ai', 'AI 头文件', 'interface/sdk_c/ai', 'directory'],
  ['sdk.graphic', 'Graphic 2D 头文件', 'interface/sdk_c/graphic/graphic_2d', 'directory'],
  ['sdk.multimedia.avcodec', 'AVCodec 头文件', 'interface/sdk_c/multimedia/av_codec', 'directory'],
  ['sdk.multimedia.foundation', 'Media Foundation 头文件', 'interface/sdk_c/multimedia/media_foundation', 'directory'],
  ['sdk.egl', 'EGL 头文件', 'interface/sdk_c/third_party/egl', 'directory'],
  ['sdk.opengles', 'OpenGLES 头文件', 'interface/sdk_c/third_party/openGLES', 'directory'],
  ['sdk.vulkan', 'Vulkan 头文件', 'interface/sdk_c/third_party/vulkan-headers', 'directory'],
  ['sdk.musl.ndk', 'musl NDK 头文件', 'interface/sdk_c/third_party/musl/ndk_musl_include', 'directory'],
  ['sdk.musl.aarch64', 'musl AArch64 头文件', 'third_party/musl/arch/aarch64', 'directory'],
  ['sdk.musl.generic', 'musl 通用头文件', 'third_party/musl/arch/generic', 'directory'],
  ['sdk.musl.alltypes', 'musl alltypes 模板', 'third_party/musl/include/alltypes.h.in', 'file'],
]);

const RUNTIME_PROFILES = Object.freeze({
  gpu_gles3: ['libEGL.so', 'libGLESv3.so', 'libc.so'],
  gpu_vulkan: ['libvulkan.so', 'libc.so'],
  npu_nnrt: ['libneural_network_runtime.so', 'libneural_network_core.so', 'libc.so'],
  media_avcodec: ['libnative_media_vdec.so', 'libnative_media_venc.so', 'libnative_media_codecbase.so', 'libnative_media_core.so', 'libc.so'],
});

const DEFAULT_SERVER_PROFILE = Object.freeze({
  profileId: 'default',
  workerRepoRoot: '',
  sourceRoots: [],
  openHarmonySdkRoot: '',
  llvmBinRoot: '',
  glslangBin: '',
  traceStreamer: '',
  runtimeLibraryRoot: '',
  pythonBin: 'python3',
});

function clone(value) { return JSON.parse(JSON.stringify(value)); }

function validateScalar(value, label, {allowEmpty = true} = {}) {
  if (value === undefined || value === null) value = '';
  if (typeof value !== 'string') throw new Error(`${label} 必须为字符串。`);
  value = value.trim();
  if (!allowEmpty && !value) throw new Error(`${label} 不能为空。`);
  if (Buffer.byteLength(value) > MAX_PATH_BYTES) throw new Error(`${label} 不能超过 ${MAX_PATH_BYTES} 字节。`);
  // platform.env is sourced by bash and also parsed as KEY="VALUE".  Reject
  // control bytes and expansion/escape characters instead of attempting a
  // second, subtly different escaping language.  Spaces and apostrophes are
  // valid and are handled by the generated shell script.
  if (/[\u0000-\u001f\u007f-\u009f\u2028\u2029]/u.test(value) || /["`$\\]/.test(value))
    throw new Error(`${label} 包含不能安全写入服务器环境文件的字符。`);
  return value;
}

function validateRemotePath(value, label) {
  value = validateScalar(value, label);
  if (value && !value.startsWith('/') && !value.startsWith('~/'))
    throw new Error(`${label} 必须使用以 / 或 ~/ 开头的远端路径。`);
  return value;
}

function validatePython(value) {
  value = validateScalar(value, 'Python 可执行文件');
  if (!value) return DEFAULT_SERVER_PROFILE.pythonBin;
  if (value.includes('/')) return validateRemotePath(value, 'Python 可执行文件');
  if (!/^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$/.test(value))
    throw new Error('Python PATH 命令只能包含字母、数字、点、下划线、加号和连字符。');
  return value;
}

function normalizeServerProfile(value = {}) {
  if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error('服务器环境 Profile 必须为对象。');
  const profileId = validateScalar(value.profileId ?? DEFAULT_SERVER_PROFILE.profileId, 'Profile ID', {allowEmpty: false});
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(profileId))
    throw new Error('Profile ID 只能包含字母、数字、点、下划线和连字符，且不超过 64 个字符。');
  const sourceRoots = value.sourceRoots ?? DEFAULT_SERVER_PROFILE.sourceRoots;
  if (!Array.isArray(sourceRoots)) throw new Error('源码目录必须为字符串数组。');
  const normalizedRoots = [];
  for (const [index, source] of sourceRoots.entries()) {
    const normalized = validateRemotePath(source, `源码目录 ${index + 1}`);
    if (normalized && !normalizedRoots.includes(normalized)) normalizedRoots.push(normalized);
  }
  if (normalizedRoots.length > 32) throw new Error('源码目录最多配置 32 个。');
  const normalized = {
    profileId,
    workerRepoRoot: validateRemotePath(value.workerRepoRoot ?? DEFAULT_SERVER_PROFILE.workerRepoRoot, '异构工作仓目录'),
    sourceRoots: normalizedRoots,
    openHarmonySdkRoot: validateRemotePath(value.openHarmonySdkRoot ?? DEFAULT_SERVER_PROFILE.openHarmonySdkRoot, 'OpenHarmony SDK 目录'),
    llvmBinRoot: validateRemotePath(value.llvmBinRoot ?? DEFAULT_SERVER_PROFILE.llvmBinRoot, 'LLVM bin 目录'),
    glslangBin: validateRemotePath(value.glslangBin ?? DEFAULT_SERVER_PROFILE.glslangBin, 'glslang 可执行文件'),
    traceStreamer: validateRemotePath(value.traceStreamer ?? DEFAULT_SERVER_PROFILE.traceStreamer, 'TraceStreamer 可执行文件'),
    runtimeLibraryRoot: validateRemotePath(value.runtimeLibraryRoot ?? DEFAULT_SERVER_PROFILE.runtimeLibraryRoot, '运行库目录'),
    pythonBin: validatePython(value.pythonBin ?? DEFAULT_SERVER_PROFILE.pythonBin),
  };
  if (Buffer.byteLength(JSON.stringify(normalized), 'utf8') > MAX_PROFILE_BYTES)
    throw new Error(`服务器环境 Profile 不能超过 ${MAX_PROFILE_BYTES / 1024} KiB；请减少源码目录数量或缩短路径。`);
  const embeddedValues = [
    normalized.profileId, normalized.workerRepoRoot, ...normalized.sourceRoots,
    normalized.openHarmonySdkRoot, normalized.llvmBinRoot, normalized.glslangBin,
    normalized.traceStreamer, normalized.runtimeLibraryRoot, normalized.pythonBin,
  ];
  if (embeddedValues.reduce((total, item) => total + Buffer.byteLength(shellQuote(item), 'utf8'), 0) > MAX_PROFILE_BYTES)
    throw new Error(`服务器环境 Profile 的 Shell 编码不能超过 ${MAX_PROFILE_BYTES / 1024} KiB；请减少过长路径或路径中的单引号。`);
  return normalized;
}

function shellQuote(value) { return `'${String(value).replace(/'/g, `'"'"'`)}'`; }

function boundedRemoteScript(script) {
  if (Buffer.byteLength(script, 'utf8') >= MAX_REMOTE_SCRIPT_BYTES)
    throw new Error('服务器环境检查命令超过 32 KiB；请减少源码目录数量或缩短路径。');
  return script;
}

function normalizeScopeId(value) {
  const scopeId = String(value || 'unscoped');
  if (scopeId !== 'unscoped' && !SCOPE_PATTERN.test(scopeId)) throw new Error('服务器环境目标作用域无效。');
  return scopeId;
}

function symbolicManagedPaths(profileId, scopeId = 'unscoped') {
  scopeId = normalizeScopeId(scopeId);
  const managedRoot = `$HOME/.local/share/heterogeneous-workbench/server-profiles/targets/${scopeId}/${profileId}`;
  return {
    managedRoot,
    managedPaths: {
      state: `${managedRoot}/state`, cache: `${managedRoot}/cache`, assets: `${managedRoot}/assets`,
      tmp: `${managedRoot}/tmp`, logs: `${managedRoot}/logs`, run: `${managedRoot}/run`,
      envFile: `${managedRoot}/platform.env`,
    },
  };
}

const SCRIPT_PREAMBLE = String.raw`set -u
emit_record() {
  local first=1 value
  printf '\036'
  for value in "$@"; do
    if [ "$first" -eq 0 ]; then printf '\037'; fi
    first=0
    printf '%s' "$value"
  done
}
expand_location() {
  case "$1" in
    '~') printf '%s' "$HOME" ;;
    '~/'*) printf '%s/%s' "$HOME" "\${1#\~/}" ;;
    *) printf '%s' "$1" ;;
  esac
}
resolve_executable() {
  local requested resolved
  requested="$1"
  [ -n "$requested" ] || return 0
  resolved="$(expand_location "$requested")"
  case "$resolved" in
    */*) printf '%s' "$resolved" ;;
    *) command -v -- "$resolved" || printf '%s' "$resolved" ;;
  esac
}
emit_path_check() {
  local id="$1" label="$2" category="$3" requirement="$4" configured="$5" kind="$6" detail="$7" resolved status
  resolved="$(expand_location "$configured")"
  if [ -z "$configured" ]; then
    status='missing'
    [ "$requirement" = 'optional' ] && status='skipped'
  elif [ "$kind" = 'directory' ] && [ -d "$resolved" ]; then status='ready'
  elif [ "$kind" = 'file' ] && [ -f "$resolved" ]; then status='ready'
  elif [ "$kind" = 'executable' ] && [ -f "$resolved" ] && [ -x "$resolved" ]; then status='ready'
  else status='missing'
  fi
  emit_record CHECK "$id" "$label" "$category" "$status" "$requirement" "$resolved" "$detail"
}
emit_executable_check() {
  local id="$1" label="$2" category="$3" requirement="$4" configured="$5" detail="$6" resolved status
  if [ -z "$configured" ]; then
    resolved=''; status='missing'
    [ "$requirement" = 'optional' ] && status='skipped'
  else
    resolved="$(resolve_executable "$configured")"
    if [ -f "$resolved" ] && [ -x "$resolved" ]; then status='ready'; else status='missing'; fi
  fi
  emit_record CHECK "$id" "$label" "$category" "$status" "$requirement" "$resolved" "$detail"
}
emit_python_check() {
  local configured="$1" repository="$2" resolved status='missing' detail='需要可执行的 Python 3.10+ 与平台 Worker 依赖。'
  resolved="$(resolve_executable "$configured")"
  if [ -n "$configured" ] && [ -f "$resolved" ] && [ -x "$resolved" ] && [ -d "$repository" ] && (cd -- "$repository" && PYTHONDONTWRITEBYTECODE=1 "$resolved" -c 'import sys; import backend.core; from backend.parallel_modeling.manager import ParallelModelingManager; from backend.tools.desktop_platform_rpc import SCHEMA_VERSION; raise SystemExit(0 if sys.version_info >= (3, 10) and SCHEMA_VERSION == "desktop_platform_rpc/v2" else 3)' 2>/dev/null); then
    status='ready'; detail='Python 3.10+、平台依赖与 Desktop RPC v2 均可导入。'
  fi
  emit_record CHECK python 'Python + Worker 依赖' toolchain "$status" required "$resolved" "$detail"
}
emit_runtime_check() {
  local profile="$1" root="$2"; shift 2
  local resolved status='ready' library missing=''
  resolved="$(expand_location "$root")"
  if [ -z "$root" ]; then status='missing'; else
    for library in "$@"; do
      if [ ! -f "$resolved/$library" ]; then
        status='missing'
        if [ -z "$missing" ]; then missing="$library"; else missing="$missing, $library"; fi
      fi
    done
  fi
  emit_record CHECK "runtime.$profile" "$profile 运行库" runtime "$status" conditional "$resolved" "缺失时：$missing"
}`.replaceAll('\\${', '${');

function buildServerProfileInspectScript(value = {}, {scopeId = 'unscoped'} = {}) {
  const profile = normalizeServerProfile(value);
  scopeId = normalizeScopeId(scopeId);
  const registryVerifier = [
    'import hashlib,json,pathlib,sys',
    `exec(${JSON.stringify(PYTHON_SHA256_HELPER)})`,
    'manifest=pathlib.Path(sys.argv[1])',
    'root=pathlib.Path(sys.argv[2]).expanduser().resolve()',
    'data=json.loads(manifest.read_text(encoding="utf-8"))',
    'records=data.get("libraries",[])',
    'valid=data.get("schema_version")=="build_runtime_library_source/v1" and pathlib.Path(data.get("library_root","")).expanduser().resolve()==root and bool(records)',
    'valid=valid and all(path.is_file() and file_sha256(path)==item.get("sha256") for item in records for path in [root/item.get("name","")])',
    'raise SystemExit(0 if valid else 3)',
  ].join(';');
  const registryProfileVerifier = [
    'import hashlib,json,pathlib,sys',
    `exec(${JSON.stringify(PYTHON_SHA256_HELPER)})`,
    'manifest=pathlib.Path(sys.argv[1])',
    'root=pathlib.Path(sys.argv[2]).expanduser().resolve()',
    'data=json.loads(manifest.read_text(encoding="utf-8"))',
    'declared={item.get("name"):item for item in data.get("libraries",[]) if isinstance(item,dict) and item.get("name")}',
    'valid=data.get("schema_version")=="build_runtime_library_source/v1" and pathlib.Path(data.get("library_root","")).expanduser().resolve()==root',
    'valid=valid and all(name in declared and (root/name).is_file() and file_sha256(root/name)==declared[name].get("sha256") for name in sys.argv[3:])',
    'raise SystemExit(0 if valid else 3)',
  ].join(';');
  const lines = [SCRIPT_PREAMBLE, String.raw`
managed_root="$HOME/.local/share/heterogeneous-workbench/server-profiles/targets/` + scopeId + '/' + profile.profileId + String.raw`"
remote_identity="$(id -un)@$(hostname)"
emit_record META "$managed_root" "$managed_root/state" "$managed_root/cache" "$managed_root/assets" "$managed_root/tmp" "$managed_root/logs" "$managed_root/run" "$managed_root/platform.env"
emit_record CHECK ssh.connection 'SSH 连接' connection ready required "$remote_identity" '当前只读检查运行于已认证 SSH 会话。'`];

  lines.push(`emit_path_check repo.worker '异构工作仓' repository required ${shellQuote(profile.workerRepoRoot)} directory '目录应包含 backend/build_worker.py。'`);
  lines.push(`worker_root="$(expand_location ${shellQuote(profile.workerRepoRoot)})"`);
  lines.push(`emit_path_check repo.build_worker 'Build Worker' repository required "\${worker_root}/backend/build_worker.py" file '服务器侧真机测试案例编译入口。'`);
  if (profile.sourceRoots.length === 0) {
    lines.push(`emit_record CHECK sources.external '附加源码目录' source skipped optional '' '未配置附加源码目录；代码分析仍可使用异构工作仓。'`);
  } else {
    profile.sourceRoots.forEach((source, index) => lines.push(`emit_path_check source.${index} '源码目录 ${index + 1}' source required ${shellQuote(source)} directory '供 Code Agent 检索与流程分析。'`));
  }
  lines.push(`emit_python_check ${shellQuote(profile.pythonBin)} "$worker_root"`);
  lines.push(`llvm_root="$(expand_location ${shellQuote(profile.llvmBinRoot)})"`);
  lines.push(`emit_path_check llvm.root 'LLVM bin 目录' toolchain required ${shellQuote(profile.llvmBinRoot)} directory '固定 OpenHarmony AArch64 编译工具链。'`);
  for (const tool of ['clang', 'clang++', 'ld.lld'])
    lines.push(`emit_path_check llvm.${tool.replace(/[^a-z]/g, match => match === '+' ? 'x' : '-')} '${tool}' toolchain required "\${llvm_root}/${tool}" executable 'LLVM 工具链能力。'`);
  lines.push(`clang_headers_status=missing`);
  lines.push(`clang_headers_path="\${llvm_root%/*}/lib/clang"`);
  lines.push(`for candidate in "$clang_headers_path"/*/include; do if [ -d "$candidate" ] && [ -f "$candidate/stddef.h" ] && [ -f "$candidate/stdint.h" ]; then clang_headers_status=ready; clang_headers_path="$(cd -- "$candidate" && pwd -P)"; break; fi; done`);
  lines.push(`emit_record CHECK llvm.resource-headers 'Clang 内建头文件' toolchain "$clang_headers_status" required "$clang_headers_path" 'LLVM bin 相邻的 lib/clang/<version>/include 需包含 stddef.h 与 stdint.h。'`);
  lines.push(`emit_executable_check glslang 'glslang' toolchain required ${shellQuote(profile.glslangBin)} 'Vulkan shader 到 SPIR-V 的编译器。'`);
  lines.push(`emit_executable_check trace.streamer 'TraceStreamer' trace required ${shellQuote(profile.traceStreamer)} '原始 HiTrace 到可查询数据库的转换器。'`);
  lines.push(`sdk_root="$(expand_location ${shellQuote(profile.openHarmonySdkRoot)})"`);
  lines.push(`emit_path_check sdk.root 'OpenHarmony SDK 头文件根' sdk required ${shellQuote(profile.openHarmonySdkRoot)} directory '根目录同时包含 interface/sdk_c 与 third_party。'`);
  for (const [id, label, relative, kind] of SDK_SURFACE)
    lines.push(`emit_path_check ${shellQuote(id)} ${shellQuote(label)} sdk required "\${sdk_root}/${relative}" ${kind} ${shellQuote(`能力面：${relative}`)}`);
  lines.push(`emit_path_check runtime.root 'AArch64 运行库根' runtime required ${shellQuote(profile.runtimeLibraryRoot)} directory '所有动态实验 profile 共用的目标设备运行库目录。'`);
  for (const [profileId, libraries] of Object.entries(RUNTIME_PROFILES))
    lines.push(`emit_runtime_check ${shellQuote(profileId)} ${shellQuote(profile.runtimeLibraryRoot)} ${libraries.map(shellQuote).join(' ')}`);
  lines.push(`registry_file="$managed_root/state/build-worker/runtime-library-source.json"`);
  lines.push(`python_exec="$(resolve_executable ${shellQuote(profile.pythonBin)})"`);
  lines.push(`runtime_root="$(expand_location ${shellQuote(profile.runtimeLibraryRoot)})"`);
  lines.push(`registry_status=missing`);
  lines.push(`registry_detail='运行库目录须由 Build Worker 按文件哈希登记。'`);
  lines.push(`if [ -f "$registry_file" ] && [ -x "$python_exec" ] && PYTHONDONTWRITEBYTECODE=1 "$python_exec" -c ${shellQuote(registryVerifier)} "$registry_file" "$runtime_root" 2>/dev/null; then registry_status=ready; registry_detail='登记根目录与当前运行库文件哈希一致。'; fi`);
  lines.push(`emit_record CHECK runtime.registry 'Build Worker 运行库登记' runtime "$registry_status" required "$registry_file" "$registry_detail"`);
  for (const [profileId, libraries] of Object.entries(RUNTIME_PROFILES)) {
    lines.push(`${profileId}_registry_status=missing`);
    lines.push(`if [ -f "$registry_file" ] && [ -x "$python_exec" ] && PYTHONDONTWRITEBYTECODE=1 "$python_exec" -c ${shellQuote(registryProfileVerifier)} "$registry_file" "$runtime_root" ${libraries.map(shellQuote).join(' ')} 2>/dev/null; then ${profileId}_registry_status=ready; fi`);
    lines.push(`emit_record CHECK ${shellQuote(`runtime.registry.${profileId}`)} ${shellQuote(`${profileId} 哈希登记`)} runtime "$${profileId}_registry_status" conditional "$registry_file" ${shellQuote(`登记须覆盖：${libraries.join(', ')}`)}`);
  }
  return boundedRemoteScript(`${lines.join('\n')}\n`);
}

function buildServerProfilePrepareScript(value = {}, {scopeId = 'unscoped'} = {}) {
  const profile = normalizeServerProfile(value);
  scopeId = normalizeScopeId(scopeId);
  const knownRuntimeLibraries = [...new Set(Object.values(RUNTIME_PROFILES).flat())].sort();
  const registryVerifier = [
    'import hashlib,json,pathlib,sys',
    `exec(${JSON.stringify(PYTHON_SHA256_HELPER)})`,
    'manifest=pathlib.Path(sys.argv[1])',
    'root=pathlib.Path(sys.argv[2]).expanduser().resolve()',
    'known=set(sys.argv[3:])',
    'data=json.loads(manifest.read_text(encoding="utf-8"))',
    'records=data.get("libraries",[])',
    'declared={item.get("name") for item in records if isinstance(item,dict)}',
    'present={name for name in known if (root/name).is_file()}',
    'valid=data.get("schema_version")=="build_runtime_library_source/v1" and pathlib.Path(data.get("library_root","")).expanduser().resolve()==root and bool(records) and declared==present',
    'valid=valid and all(path.is_file() and file_sha256(path)==item.get("sha256") for item in records for path in [root/item.get("name","")])',
    'raise SystemExit(0 if valid else 3)',
  ].join(';');
  const firstSource = profile.sourceRoots[0] || '';
  const locations = {
    workerRepoRoot: profile.workerRepoRoot,
    openHarmonySdkRoot: profile.openHarmonySdkRoot,
    llvmBinRoot: profile.llvmBinRoot,
    glslangBin: profile.glslangBin,
    traceStreamer: profile.traceStreamer,
    runtimeLibraryRoot: profile.runtimeLibraryRoot,
    pythonBin: profile.pythonBin,
    firstSource,
  };
  const assignments = Object.entries(locations).map(([key, value]) => `${key}=${shellQuote(value)}`).join('\n');
  return boundedRemoteScript(`${SCRIPT_PREAMBLE}
set -e
umask 077
profile_id=${shellQuote(profile.profileId)}
target_scope=${shellQuote(scopeId)}
managed_root="$HOME/.local/share/heterogeneous-workbench/server-profiles/targets/$target_scope/$profile_id"
${assignments}
workerRepoRoot="$(expand_location "$workerRepoRoot")"
openHarmonySdkRoot="$(expand_location "$openHarmonySdkRoot")"
llvmBinRoot="$(expand_location "$llvmBinRoot")"
glslangBin="$(resolve_executable "$glslangBin")"
traceStreamer="$(resolve_executable "$traceStreamer")"
runtimeLibraryRoot="$(expand_location "$runtimeLibraryRoot")"
pythonBin="$(resolve_executable "$pythonBin")"
firstSource="$(expand_location "$firstSource")"
validate_env_value() {
  case "$2" in
    *'"'*|*'\`'*|*'$'*|*'\\'*|*$'\\n'*|*$'\\r'*)
      printf '服务器返回的 %s 路径不能安全写入 platform.env。\\n' "$1" >&2
      exit 2
      ;;
  esac
}
validate_env_value HETERO_REPO_ROOT "$workerRepoRoot"
validate_env_value HETERO_OPENHARMONY_SDK_ROOT "$openHarmonySdkRoot"
validate_env_value HETERO_LLVM_BIN_ROOT "$llvmBinRoot"
validate_env_value HETERO_GLSLANG_BIN "$glslangBin"
validate_env_value HETERO_TRACE_STREAMER "$traceStreamer"
validate_env_value HETERO_RUNTIME_LIBRARY_ROOT "$runtimeLibraryRoot"
validate_env_value HETERO_PYTHON_BIN "$pythonBin"
validate_env_value HETERO_DEFAULT_SOURCE_ROOT "$firstSource"
mkdir -p -- "$managed_root/state" "$managed_root/cache/xdg" "$managed_root/assets" "$managed_root/tmp" "$managed_root/logs" "$managed_root/run"
chmod 0700 "$managed_root" "$managed_root/state" "$managed_root/cache" "$managed_root/cache/xdg" "$managed_root/assets" "$managed_root/tmp" "$managed_root/logs" "$managed_root/run"
env_file="$managed_root/platform.env"
temporary="$env_file.tmp.$$"
trap 'rm -f -- "$temporary"' EXIT HUP INT TERM
{
  printf '# Generated by Heterogeneous Workbench Desktop.\\n'
  printf 'HETERO_USER_HOME="%s"\\n' "$managed_root"
  printf 'HETERO_ROOTLESS_RUNTIME_ROOT="%s"\\n' "$managed_root"
  printf 'HETERO_REPO_ROOT="%s"\\n' "$workerRepoRoot"
  printf 'HETERO_STATE_ROOT="%s"\\n' "$managed_root/state"
  printf 'HETERO_RUNTIME_ASSET_ROOT="%s"\\n' "$managed_root/assets"
  printf 'HETERO_CACHE_ROOT="%s"\\n' "$managed_root/cache"
  printf 'HETERO_PYTHON_BIN="%s"\\n' "$pythonBin"
  printf 'HETERO_TRACE_STREAMER="%s"\\n' "$traceStreamer"
  printf 'HETERO_LLVM_BIN_ROOT="%s"\\n' "$llvmBinRoot"
  printf 'HETERO_GLSLANG_BIN="%s"\\n' "$glslangBin"
  printf 'HETERO_OPENHARMONY_SDK_ROOT="%s"\\n' "$openHarmonySdkRoot"
  printf 'HETERO_RUNTIME_LIBRARY_ROOT="%s"\\n' "$runtimeLibraryRoot"
  printf 'HETERO_DEFAULT_SOURCE_ROOT="%s"\\n' "$firstSource"
  printf 'HETERO_DEPLOYMENT_MODE="worker"\\n'
  printf 'HETERO_AGENT_ENGINE="legacy"\\n'
  printf 'TMPDIR="%s"\\n' "$managed_root/tmp"
  printf 'XDG_CACHE_HOME="%s"\\n' "$managed_root/cache/xdg"
} >"$temporary"
chmod 0600 "$temporary"
mv -f -- "$temporary" "$env_file"
trap - EXIT HUP INT TERM
registry_file="$managed_root/state/build-worker/runtime-library-source.json"
registry_status=skipped
registry_detail='运行库目录、Python 或工作仓尚未就绪，未执行哈希登记。'
if [ -n "$runtimeLibraryRoot" ] && [ -f "$registry_file" ] && [ -x "$pythonBin" ] && PYTHONDONTWRITEBYTECODE=1 "$pythonBin" -c ${shellQuote(registryVerifier)} "$registry_file" "$runtimeLibraryRoot" ${knownRuntimeLibraries.map(shellQuote).join(' ')} 2>/dev/null; then
  registry_status=ready
  registry_detail='已有登记与当前运行库文件哈希一致；无需重复写入。'
elif [ -n "$runtimeLibraryRoot" ] && [ -d "$runtimeLibraryRoot" ] && [ -x "$pythonBin" ] && [ -f "$workerRepoRoot/backend/tools/configure_runtime_library_source.py" ]; then
  if registry_output="$(cd -- "$workerRepoRoot" && PYTHONDONTWRITEBYTECODE=1 "$pythonBin" -m backend.tools.configure_runtime_library_source --library-root "$runtimeLibraryRoot" --state-root "$managed_root/state" 2>&1)"; then
    registry_status=ready
    registry_detail='Build Worker 运行库根目录与文件哈希已登记。'
  else
    registry_status=warning
    registry_detail="运行库登记失败：$registry_output"
  fi
fi
emit_record PREPARE_CHECK runtime.registry "$registry_status" "$registry_file" "$registry_detail"
emit_record PREPARED "$managed_root" "$managed_root/state" "$managed_root/cache" "$managed_root/assets" "$managed_root/tmp" "$managed_root/logs" "$managed_root/run" "$env_file"
`);
}

function splitRecords(stdout) {
  if (typeof stdout !== 'string') throw new Error('服务器 Profile 脚本输出必须为字符串。');
  return stdout.split(RECORD_SEPARATOR).slice(1).map(record => record.split(FIELD_SEPARATOR));
}

function checkStatus(checks, ids) {
  const selected = ids.map(id => checks.find(check => check.id === id)).filter(Boolean);
  const missing = selected.filter(check => check.status !== 'ready').map(check => check.id);
  return {ready: selected.length === ids.length && missing.length === 0, checks: ids, missing};
}

function reportFromRecords(profile, records) {
  const meta = records.find(record => record[0] === 'META');
  if (!meta || meta.length !== 9) throw new Error('服务器 Profile 检查没有返回有效 META 记录。');
  const checks = records.filter(record => record[0] === 'CHECK').map(record => {
    if (record.length !== 8) throw new Error('服务器 Profile 检查返回了损坏的 CHECK 记录。');
    const [, id, label, category, status, requirement, path, detail] = record;
    if (!['ready', 'missing', 'warning', 'skipped'].includes(status)) throw new Error(`服务器 Profile 返回了未知状态：${status}`);
    return {id, label, category, status, required: requirement === 'required', requirement, path, detail};
  });
  if (!checks.length) throw new Error('服务器 Profile 检查没有返回能力项。');
  const managedRoot = meta[1];
  const managedPaths = {state: meta[2], cache: meta[3], assets: meta[4], tmp: meta[5], logs: meta[6], run: meta[7], envFile: meta[8]};
  const workerIds = ['ssh.connection', 'repo.worker', 'repo.build_worker', 'python'];
  const sourceIds = profile.sourceRoots.map((_, index) => `source.${index}`);
  const codeAnalysisIds = [...workerIds, ...sourceIds];
  const traceAnalysisIds = [...workerIds, 'trace.streamer'];
  const sdkIds = ['sdk.root', ...SDK_SURFACE.map(([id]) => id)];
  const runtimeProfileIds = Object.keys(RUNTIME_PROFILES);
  const commonLlvmIds = ['llvm.root', 'llvm.resource-headers', 'llvm.ld-lld'];
  const deviceBuildIds = [...workerIds, ...commonLlvmIds, 'llvm.clang', 'llvm.clangxx', 'glslang', ...sdkIds, 'runtime.root', ...runtimeProfileIds.flatMap(id => [`runtime.${id}`, `runtime.registry.${id}`])];
  const buildProfiles = {
    native_c: checkStatus(checks, [...workerIds, ...commonLlvmIds, 'llvm.clang']),
    native_cpp: checkStatus(checks, [...workerIds, ...commonLlvmIds, 'llvm.clangxx']),
    gpu_gles3: checkStatus(checks, [...workerIds, ...commonLlvmIds, 'llvm.clang', 'sdk.root', 'sdk.egl', 'sdk.opengles', 'sdk.musl.ndk', 'sdk.musl.aarch64', 'sdk.musl.generic', 'sdk.musl.alltypes', 'runtime.registry.gpu_gles3', 'runtime.gpu_gles3']),
    gpu_vulkan: checkStatus(checks, [...workerIds, ...commonLlvmIds, 'llvm.clang', 'glslang', 'sdk.root', 'sdk.vulkan', 'sdk.musl.ndk', 'sdk.musl.aarch64', 'sdk.musl.generic', 'sdk.musl.alltypes', 'runtime.registry.gpu_vulkan', 'runtime.gpu_vulkan']),
    npu_nnrt: checkStatus(checks, [...workerIds, ...commonLlvmIds, 'llvm.clang', 'sdk.root', 'sdk.ai', 'sdk.musl.ndk', 'sdk.musl.aarch64', 'sdk.musl.generic', 'sdk.musl.alltypes', 'runtime.registry.npu_nnrt', 'runtime.npu_nnrt']),
    media_avcodec: checkStatus(checks, [...workerIds, ...commonLlvmIds, 'llvm.clang', 'sdk.root', 'sdk.graphic', 'sdk.multimedia.avcodec', 'sdk.multimedia.foundation', 'sdk.musl.ndk', 'sdk.musl.aarch64', 'sdk.musl.generic', 'sdk.musl.alltypes', 'runtime.registry.media_avcodec', 'runtime.media_avcodec']),
  };
  const readyFor = {
    codeAnalysis: checkStatus(checks, codeAnalysisIds).ready,
    traceAnalysis: checkStatus(checks, traceAnalysisIds).ready,
    deviceBuild: checkStatus(checks, deviceBuildIds).ready,
  };
  return {
    schemaVersion: SCHEMA_VERSION,
    profile: clone(profile),
    managedRoot,
    managedPaths,
    checks,
    capabilityMatrix: {
      codeAnalysis: checkStatus(checks, codeAnalysisIds),
      traceAnalysis: checkStatus(checks, traceAnalysisIds),
      deviceBuild: checkStatus(checks, deviceBuildIds),
      buildProfiles,
    },
    summary: {
      ready: checks.every(check => check.status === 'ready' || check.status === 'skipped'),
      total: checks.length,
      missing: checks.filter(check => check.status === 'missing').length,
      readyCount: checks.filter(check => check.status === 'ready').length,
      readyFor,
    },
  };
}

function parseServerProfileInspect(stdout, value = {}) {
  return reportFromRecords(normalizeServerProfile(value), splitRecords(stdout));
}

function parseServerProfilePrepare(stdout, value = {}) {
  const profile = normalizeServerProfile(value);
  const record = splitRecords(stdout).find(fields => fields[0] === 'PREPARED');
  if (!record || record.length !== 9) throw new Error('服务器 Profile 初始化没有返回有效结果。');
  const checks = splitRecords(stdout).filter(fields => fields[0] === 'PREPARE_CHECK').map(fields => ({
    id: fields[1], status: fields[2], path: fields[3], detail: fields[4],
  }));
  return {
    schemaVersion: SCHEMA_VERSION,
    profile,
    managedRoot: record[1],
    managedPaths: {state: record[2], cache: record[3], assets: record[4], tmp: record[5], logs: record[6], run: record[7], envFile: record[8]},
    prepared: true,
    checks,
  };
}

module.exports = {
  DEFAULT_SERVER_PROFILE,
  RUNTIME_PROFILES,
  SCHEMA_VERSION,
  SDK_SURFACE,
  buildServerProfileInspectScript,
  buildServerProfilePrepareScript,
  normalizeServerProfile,
  parseServerProfileInspect,
  parseServerProfilePrepare,
  symbolicManagedPaths,
};
