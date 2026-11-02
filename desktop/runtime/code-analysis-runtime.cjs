'use strict';

const path = require('node:path');

const CODE_ANALYSIS_RUNTIME_VERSION = 'tree-sitter-c-cpp-jsonschema-v2';
const CODE_ANALYSIS_RUNTIME_MARKER = 'HETERO_CODE_ANALYSIS_RUNTIME\t';
const PACKAGE_SPECS = [
  'tree-sitter==0.25.2',
  'tree-sitter-c==0.24.2',
  'tree-sitter-cpp==0.23.4',
  'jsonschema==4.25.1',
];

function shellQuote(value) { return `'${String(value).replace(/'/g, `'"'"'`)}'`; }

function managedRuntimeSuffix(scopeId, profileId) {
  if (!/^[a-f0-9]{16}$/.test(String(scopeId || ''))
      || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(String(profileId || '')))
    throw new Error('代码分析运行环境的服务器 Profile 标识无效。');
  return `/.local/share/heterogeneous-workbench/server-profiles/targets/${scopeId}/${profileId}/assets/code-analysis-runtime/${CODE_ANALYSIS_RUNTIME_VERSION}`;
}

const VERIFY_PROGRAM = String.raw`
import sys
from tree_sitter import Language, Parser
import tree_sitter_c
import tree_sitter_cpp
from jsonschema import Draft202012Validator
if sys.version_info < (3, 10): raise SystemExit(3)
for capsule in (tree_sitter_c.language, tree_sitter_cpp.language):
    parser=Parser(Language(capsule()))
    tree=parser.parse(b'int main(void) { return 0; }')
    if tree.root_node.has_error: raise SystemExit(4)
Draft202012Validator.check_schema({'type':'object','properties':{'id':{'type':'string'}}})
`.trim();

function buildCodeAnalysisRuntimePrepareScript({scopeId, profileId, pythonBin = 'python3', candidateRoots = []} = {}) {
  const suffix = managedRuntimeSuffix(scopeId, profileId);
  const profileRootSuffix = suffix.slice(0, suffix.indexOf('/assets/code-analysis-runtime/'));
  const packageArgs = PACKAGE_SPECS.map(shellQuote).join(' ');
  const roots = [...new Set((Array.isArray(candidateRoots) ? candidateRoots : [])
    .map(value => String(value || '').trim()).filter(Boolean))].slice(0, 4);
  return [
    'set -eu',
    'umask 077',
    `configured_python=${shellQuote(pythonBin || 'python3')}`,
    ...roots.map((root, index) => `candidate_root_${index}=${shellQuote(root)}`),
    'resolve_location() { case "$1" in "~") printf \'%s\\n\' "$HOME" ;; "~/"*) printf \'%s/%s\\n\' "$HOME" "${1#~/}" ;; *) printf \'%s\\n\' "$1" ;; esac; }',
    'resolve_python() { case "$1" in */*) resolve_location "$1" ;; *) command -v -- "$1" 2>/dev/null || true ;; esac; }',
    'python_bin=""',
    `for configured_candidate in "$configured_python" ${roots.flatMap((_root, index) => [
      `"$(resolve_location "$candidate_root_${index}")/.venv/bin/python"`,
      `"$(resolve_location "$candidate_root_${index}")/venv/bin/python"`,
    ]).join(' ')}; do`,
    '  candidate="$(resolve_python "$configured_candidate")"',
    '  [ -n "$candidate" ] && [ -x "$candidate" ] || continue',
    '  if "$candidate" -c \'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 3)\' >/dev/null 2>&1 \\',
    '      && "$candidate" -m pip --version >/dev/null 2>&1 \\',
    '      && "$candidate" -m venv --help >/dev/null 2>&1; then python_bin="$candidate"; break; fi',
    'done',
    'test -n "$python_bin" || { printf \'未找到同时支持 Python 3.10+、pip 与 venv 的服务器解释器，无法准备代码分析环境。\\n\' >&2; exit 4; }',
    `managed_root="$HOME${profileRootSuffix}"`,
    'test -d "$managed_root" && test ! -L "$managed_root" || { printf \'服务器 Profile 托管目录尚未初始化。\\n\' >&2; exit 5; }',
    'assets_root="$managed_root/assets"',
    'test -d "$assets_root" && test ! -L "$assets_root" || { printf \'服务器 Profile assets 目录不安全。\\n\' >&2; exit 5; }',
    'runtime_base="$assets_root/code-analysis-runtime"',
    `runtime_root="$HOME${suffix}"`,
    'lock_dir="$runtime_base/.install-lock"',
    'stale_lock="$runtime_base/.stale-lock-$$"',
    'log_file="$managed_root/logs/code-analysis-runtime-install.log"',
    'if [ -e "$runtime_base" ]; then test -d "$runtime_base" && test ! -L "$runtime_base" || { printf \'代码分析环境托管路径不安全。\\n\' >&2; exit 5; }; else mkdir -- "$runtime_base"; fi',
    'mkdir -p -- "$managed_root/logs" "$managed_root/cache/pip"',
    'chmod 0700 "$runtime_base" "$managed_root/logs" "$managed_root/cache" "$managed_root/cache/pip"',
    'verify_runtime() {',
    '  test -d "$1" && test ! -L "$1" || return 1',
    '  test -d "$1/bin" && test ! -L "$1/bin" || return 1',
    '  test -f "$1/pyvenv.cfg" && test ! -L "$1/pyvenv.cfg" || return 1',
    '  test -x "$1/bin/python" && test ! -L "$1/bin/python" || return 1',
    `  PYTHONDONTWRITEBYTECODE=1 "$1/bin/python" - <<'HETERO_CODE_ANALYSIS_VERIFY' >/dev/null 2>&1`,
    VERIFY_PROGRAM,
    'HETERO_CODE_ANALYSIS_VERIFY',
    '}',
    'if verify_runtime "$runtime_root"; then',
    `  printf '${CODE_ANALYSIS_RUNTIME_MARKER}READY\\t%s\\treused\\n' "$runtime_root"`,
    '  exit 0',
    'fi',
    'staging="$runtime_base/.partial-$$"',
    'locked=0',
    'release_lock() {',
    '  [ "$locked" -eq 1 ] || return 0',
    '  owner="$(cat "$lock_dir/owner.pid" 2>/dev/null || true)"',
    '  if [ "$owner" = "$$" ]; then rm -f -- "$lock_dir/owner.pid"; rmdir -- "$lock_dir" 2>/dev/null || true; fi',
    '  locked=0',
    '}',
    'cleanup() {',
    '  rm -rf -- "$staging"',
    '  release_lock',
    '}',
    'trap cleanup EXIT HUP INT TERM',
    'wait_count=0',
    'while ! mkdir -- "$lock_dir" 2>/dev/null; do',
    '  if verify_runtime "$runtime_root"; then',
    `    printf '${CODE_ANALYSIS_RUNTIME_MARKER}READY\\t%s\\treused\\n' "$runtime_root"`,
    '    exit 0',
    '  fi',
    '  wait_count=$((wait_count + 1))',
    '  if [ "$wait_count" -eq 1 ] || [ $((wait_count % 30)) -eq 0 ]; then',
    `    PYTHONDONTWRITEBYTECODE=1 "$python_bin" - "$lock_dir" "$stale_lock" <<'HETERO_CODE_ANALYSIS_STALE_LOCK' >/dev/null 2>&1 || true`,
    'import os,stat,sys,time',
    'lock,stale=sys.argv[1:3]',
    'try:',
    '    info=os.lstat(lock)',
    '    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode): raise SystemExit(1)',
    '    age=max(0.0,time.time()-info.st_mtime)',
    "    owner_path=os.path.join(lock,'owner.pid')",
    '    owner_alive=False',
    '    try:',
    "        owner=int(open(owner_path,encoding='ascii').read().strip())",
    '        if owner<=1: raise ValueError()',
    '        try: os.kill(owner,0); owner_alive=True',
    '        except ProcessLookupError: pass',
    '        except PermissionError: owner_alive=True',
    '    except (FileNotFoundError,ValueError,OSError):',
    '        if age<=30: raise SystemExit(1)',
    '    if owner_alive: raise SystemExit(1)',
    '    os.rename(lock,stale)',
    "    entries=os.listdir(stale)",
    "    if any(name!='owner.pid' for name in entries):",
    '        os.rename(stale,lock); raise SystemExit(1)',
    "    try: os.unlink(os.path.join(stale,'owner.pid'))",
    '    except FileNotFoundError: pass',
    '    os.rmdir(stale)',
    'except (FileNotFoundError,FileExistsError,PermissionError,OSError): raise SystemExit(1)',
    'HETERO_CODE_ANALYSIS_STALE_LOCK',
    '  fi',
    '  if [ "$wait_count" -ge 300 ]; then printf \'代码分析环境正在被另一工作台准备，等待超过 5 分钟。\\n\' >&2; exit 6; fi',
    '  sleep 1',
    'done',
    'printf \'%s\\n\' "$$" >"$lock_dir/owner.pid"',
    'chmod 0600 "$lock_dir/owner.pid"',
    'locked=1',
    'rm -rf -- "$staging"',
    ': >"$log_file"',
    'chmod 0600 "$log_file"',
    'if ! "$python_bin" -m venv --copies "$staging" >>"$log_file" 2>&1; then',
    '  printf \'无法创建服务器代码分析虚拟环境；详情已保存到 %s。\\n\' "$log_file" >&2',
    '  exit 7',
    'fi',
    `if ! PIP_CACHE_DIR="$managed_root/cache/pip" "$staging/bin/python" -m pip install --disable-pip-version-check --no-input --quiet ${packageArgs} >>"$log_file" 2>&1; then`,
    '  printf \'无法安装 Tree-sitter C/C++ 解析器；详情已保存到 %s。请检查服务器 pip 源或代理配置。\\n\' "$log_file" >&2',
    '  exit 8',
    'fi',
    'if ! verify_runtime "$staging"; then',
    '  printf \'Tree-sitter C/C++ 解析器安装后自检失败；详情已保存到 %s。\\n\' "$log_file" >&2',
    '  exit 9',
    'fi',
    'rm -rf -- "$runtime_root"',
    'mv -- "$staging" "$runtime_root"',
    'release_lock',
    'trap - EXIT HUP INT TERM',
    `printf '${CODE_ANALYSIS_RUNTIME_MARKER}READY\\t%s\\tinstalled\\n' "$runtime_root"`,
  ].join('\n');
}

function parseCodeAnalysisRuntime(stdout, {scopeId, profileId} = {}) {
  const line = String(stdout || '').split(/\r?\n/).reverse().find(value => value.startsWith(CODE_ANALYSIS_RUNTIME_MARKER));
  const [status, runtimeRoot, disposition] = line?.slice(CODE_ANALYSIS_RUNTIME_MARKER.length).split('\t') || [];
  const suffix = managedRuntimeSuffix(scopeId, profileId);
  if (status !== 'READY' || !['installed', 'reused'].includes(disposition)
      || !runtimeRoot?.startsWith('/') || runtimeRoot.includes('\0') || /[\r\n]/.test(runtimeRoot)
      || path.posix.normalize(runtimeRoot) !== runtimeRoot || !runtimeRoot.endsWith(suffix))
    throw new Error('服务器没有返回有效的代码分析运行环境。');
  return {schemaVersion: 'heterogeneous_code_analysis_runtime/v1', version: CODE_ANALYSIS_RUNTIME_VERSION,
    root: runtimeRoot, python: `${runtimeRoot}/bin/python`, reused: disposition === 'reused'};
}

module.exports = {
  CODE_ANALYSIS_RUNTIME_MARKER,
  CODE_ANALYSIS_RUNTIME_VERSION,
  PACKAGE_SPECS,
  buildCodeAnalysisRuntimePrepareScript,
  managedRuntimeSuffix,
  parseCodeAnalysisRuntime,
};
