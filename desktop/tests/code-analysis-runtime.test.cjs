'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {
  CODE_ANALYSIS_RUNTIME_MARKER,
  PACKAGE_SPECS,
  buildCodeAnalysisRuntimePrepareScript,
  managedRuntimeSuffix,
  parseCodeAnalysisRuntime,
} = require('../runtime/code-analysis-runtime.cjs');

const scopeId = '0123456789abcdef';
const profileId = 'default';

test('managed code analysis runtime installs pinned C/C++ parsers in a profile-owned venv', () => {
  const script = buildCodeAnalysisRuntimePrepareScript({
    scopeId, profileId, pythonBin: '/usr/bin/python3', candidateRoots: ['/srv/platform', '/srv/build worker'],
  });
  for (const dependency of PACKAGE_SPECS) assert.ok(script.includes(`'${dependency}'`));
  assert.match(script, /-m venv --copies/);
  assert.match(script, /candidate_root_0='\/srv\/platform'/);
  assert.match(script, /candidate_root_1='\/srv\/build worker'/);
  assert.match(script, /candidate_root_0"\)\/\.venv\/bin\/python/);
  assert.match(script, /candidate_root_1"\)\/venv\/bin\/python/);
  assert.match(script, /-m pip --version/);
  assert.match(script, /-m pip install --disable-pip-version-check --no-input --quiet/);
  assert.match(script, /tree_sitter_c\.language/);
  assert.match(script, /tree_sitter_cpp\.language/);
  assert.match(script, /code-analysis-runtime-install\.log/);
  assert.match(script, /owner\.pid/);
  assert.match(script, /os\.kill\(owner,0\)/);
  assert.match(script, /if owner_alive: raise SystemExit\(1\)/);
  assert.match(script, /os\.rename\(lock,stale\)/);
  assert.match(script, /if \[ "\$owner" = "\$\$" \]/);
  assert.match(script, /test ! -L "\$1\/bin\/python"/);
  assert.doesNotMatch(script, /sudo |pip install --user|HETERO_REPO_ROOT/);
});

test('runtime paths are target/profile scoped and response paths are validated', () => {
  const suffix = managedRuntimeSuffix(scopeId, profileId);
  const root = `/home/qa${suffix}`;
  assert.deepEqual(parseCodeAnalysisRuntime(
    `${CODE_ANALYSIS_RUNTIME_MARKER}READY\t${root}\tinstalled\n`, {scopeId, profileId},
  ), {
    schemaVersion: 'heterogeneous_code_analysis_runtime/v1',
    version: 'tree-sitter-c-cpp-jsonschema-v2',
    root,
    python: `${root}/bin/python`,
    reused: false,
  });
  assert.throws(() => parseCodeAnalysisRuntime(
    `${CODE_ANALYSIS_RUNTIME_MARKER}READY\t/tmp/unmanaged\treused\n`, {scopeId, profileId},
  ), /有效的代码分析运行环境/);
  assert.throws(() => managedRuntimeSuffix('../escape', profileId), /Profile 标识无效/);
});

test('Desktop allowlist and transport recover parser absence through the managed runtime', () => {
  const main = fs.readFileSync(path.join(__dirname, '..', 'main.cjs'), 'utf8');
  const shared = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'shared-pages.jsx'), 'utf8');
  const transport = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'platform-transport.cjs'), 'utf8');
  assert.match(main, /'ensureRemoteCodeAnalysisRuntime'/);
  assert.match(shared, /CODE_GRAPH_EXTRACTOR_UNAVAILABLE/);
  assert.match(shared, /invoke\('ensureRemoteCodeAnalysisRuntime'/);
  assert.match(transport, /managed_analysis_python/);
  assert.ok(transport.indexOf('"$managed_analysis_python"') < transport.indexOf('"$HETERO_PYTHON_BIN"'));
});
