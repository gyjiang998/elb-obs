'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const {spawn, spawnSync} = require('node:child_process');
const {prepareAnalysisWorker} = require('../build/prepare-analysis-worker.cjs');
const desktopVersion = require('../package.json').version;
const {
  buildAnalysisWorkerActivateScript,
  buildAnalysisWorkerInspectScript,
  buildAnalysisWorkerPrepareScript,
  buildAnalysisWorkerRootScript,
  buildAnalysisWorkerVerifyScript,
  loadAnalysisWorkerBundle,
  parseAnalysisWorkerReady,
  parseAnalysisWorkerRoot,
  parseAnalysisWorkerStatus,
  resolveAnalysisWorkerRoot,
} = require('../runtime/analysis-worker.cjs');

test('analysis worker resource is reproducible, minimal and available through packaged and source layouts', () => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-analysis-worker-'));
  const packaged = path.join(temporary, 'resources', 'analysis-worker');
  prepareAnalysisWorker({output: packaged});
  const first = fs.readFileSync(path.join(packaged, 'analysis-worker-manifest.json'), 'utf8');
  prepareAnalysisWorker({output: packaged});
  assert.equal(fs.readFileSync(path.join(packaged, 'analysis-worker-manifest.json'), 'utf8'), first);
  assert.equal(resolveAnalysisWorkerRoot({resourcesDir: '/missing', resourcesPath: path.join(temporary, 'resources')}), packaged);
  const bundle = loadAnalysisWorkerBundle({resourcesDir: '/missing', resourcesPath: path.join(temporary, 'resources'), desktopVersion});
  assert.match(bundle.manifest.identity, /^[a-f0-9]{64}$/);
  assert.ok(bundle.totalBytes > 100000);
  assert.ok(bundle.totalBytes < 64 * 1024 * 1024);
  assert.ok(bundle.files.some(file => file.path === 'backend/tools/desktop_platform_rpc.py'));
  assert.ok(bundle.files.some(file => file.path === 'backend/desktop_workspace_rpc.py'));
  assert.ok(bundle.files.some(file => file.path === 'backend/htrace_analysis.py'));
  assert.ok(bundle.files.some(file => file.path === 'backend/hitrace_instrumentation.py'));
  assert.ok(bundle.files.some(file => file.path === 'backend/trace_modules.py'));
  assert.ok(bundle.files.some(file => file.path === 'backend/code_graph.py'));
  assert.ok(bundle.files.some(file => file.path === 'backend/parallel_modeling/search_executor.py'));
  assert.ok(bundle.files.some(file => file.path === 'backend/parallel_modeling/frequency_roofline.py'));
  assert.ok(bundle.files.some(file => file.path === 'data/knowledge-bases/optimization/optimization_knowledge_base.json'));
  assert.ok(bundle.files.some(file => file.path === 'data/knowledge-bases/hardware/hardware_knowledge_base.json'));
  assert.ok(bundle.files.some(file => file.path === 'data/hitrace_tag_source_scope_hints.json'));
  assert.ok(bundle.files.some(file => file.path.startsWith('agent-resources/skill-library/profiles/')));
  assert.ok(bundle.files.every(file => !file.path.includes('__pycache__') && !file.path.endsWith('.pyc')));
  assert.throws(() => loadAnalysisWorkerBundle({resourcesPath: path.join(temporary, 'resources'), desktopVersion: 'older-desktop'}), /版本.*不一致/);

  const development = path.join(temporary, 'source', 'build/resources/analysis-worker');
  fs.mkdirSync(path.dirname(development), {recursive: true});
  fs.renameSync(packaged, development);
  assert.equal(resolveAnalysisWorkerRoot({resourcesDir: path.join(temporary, 'source'), resourcesPath: '/missing'}), development);
  fs.rmSync(temporary, {recursive: true, force: true});
});

test('analysis worker validates its inner calibration bundle first and emits a canonical receipt', () => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-analysis-worker-calibration-'));
  const source = path.resolve(__dirname, '..', '..', 'tools/parallel_calibration/prebuilt/aarch64-linux-ohos');
  const crlfBundle = path.join(temporary, 'crlf-bundle');
  fs.cpSync(source, crlfBundle, {recursive: true});
  const manifest = JSON.parse(fs.readFileSync(path.join(crlfBundle, 'manifest.json'), 'utf8'));
  const receipt = path.join(crlfBundle, ...manifest.receipt.split('/'));
  const canonical = fs.readFileSync(receipt);
  fs.writeFileSync(receipt, canonical.toString('utf8').replace(/\r?\n/g, '\r\n'));
  const output = path.join(temporary, 'worker');
  prepareAnalysisWorker({output, calibrationBundleRoot: crlfBundle});
  const packagedReceipt = path.join(output, 'tools/parallel_calibration/prebuilt/aarch64-linux-ohos', ...manifest.receipt.split('/'));
  assert.deepEqual(fs.readFileSync(packagedReceipt), canonical);

  const invalidBundle = path.join(temporary, 'invalid-bundle');
  fs.cpSync(crlfBundle, invalidBundle, {recursive: true});
  const invalidReceipt = path.join(invalidBundle, ...manifest.receipt.split('/'));
  fs.appendFileSync(invalidReceipt, 'tampered');
  const retainedOutput = path.join(temporary, 'retained-worker');
  fs.mkdirSync(retainedOutput); fs.writeFileSync(path.join(retainedOutput, 'known-good'), 'retained');
  assert.throws(() => prepareAnalysisWorker({output: retainedOutput, calibrationBundleRoot: invalidBundle}), error =>
    error.code === 'CALIBRATION_BENCHMARK_INVALID' && error.details?.item === 'receipt');
  assert.equal(fs.readFileSync(path.join(retainedOutput, 'known-good'), 'utf8'), 'retained',
    'invalid inner bundles must be rejected before replacing the previous worker');
  fs.rmSync(temporary, {recursive: true, force: true});
});

test('analysis worker executes all analysis catalogs with Python site-packages disabled', {
  skip: process.platform === 'win32' ? 'The managed analysis worker runs on a POSIX SSH target.' : false,
}, () => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-analysis-worker-stdlib-'));
  const worker = path.join(temporary, 'analysis-worker');
  prepareAnalysisWorker({output: worker});
  const probeProgram = `
import json, os, sys, tempfile
sys.path.insert(0, os.getcwd())
from backend.tools.desktop_platform_rpc import dispatch
from backend.htrace_analysis import list_htrace_self_time_events
from backend.hitrace_instrumentation import load_hitrace_tag_catalog
from backend.trace_modules import classify_trace_event
paths = [
    "/v1/workload-features/catalog", "/v1/parallel-modeling/catalog", "/v1/parallel-calibration/catalog",
    "/v1/skill-version-library", "/v1/user-skill-library", "/v1/experience-evolution",
    "/v1/experience-evolution/sources?limit=5",
]
with tempfile.TemporaryDirectory(prefix="hetero-worker-stdlib-") as state:
    os.environ["HETERO_STATE_ROOT"] = state
    print(json.dumps([dispatch({"method": "GET", "path": path})["value"]["schema_version"] for path in paths]))
`;
  const probe = spawnSync(process.env.PYTHON || 'python3', ['-I', '-S', '-c', probeProgram], {
    cwd: worker,
    encoding: 'utf8',
  });
  assert.equal(probe.status, 0, probe.stderr || probe.error?.message);
  assert.deepEqual(JSON.parse(probe.stdout), [
    'workload_features_catalog/v1',
    'parallel_modeling_catalog/v1',
    'parallel_calibration_catalog/v1',
    'skill_version_library/v1',
    'skill_management_library/v1',
    'experience_evolution_catalog/v1',
    'experience_source_catalog/v1',
  ]);
  fs.rmSync(temporary, {recursive: true, force: true});
});

test('analysis worker rejects local resource tampering before opening SSH', () => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-analysis-worker-tamper-'));
  const worker = path.join(temporary, 'analysis-worker');
  prepareAnalysisWorker({output: worker});
  fs.appendFileSync(path.join(worker, 'backend/tools/desktop_platform_rpc.py'), '\n# tampered\n');
  assert.throws(() => loadAnalysisWorkerBundle({resourcesPath: temporary, desktopVersion}), /文件校验失败/);
  fs.rmSync(temporary, {recursive: true, force: true});
});

test('analysis worker manifest rejects traversal and absolute paths', () => {
  for (const malicious of ['../outside.py', 'backend/../../outside.py', '/tmp/outside.py', 'backend\\outside.py']) {
    const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-analysis-worker-path-'));
    const worker = path.join(temporary, 'analysis-worker');
    prepareAnalysisWorker({output: worker});
    const filename = path.join(worker, 'analysis-worker-manifest.json');
    const manifest = JSON.parse(fs.readFileSync(filename, 'utf8'));
    manifest.files.push({path: malicious, size: 0, sha256: '0'.repeat(64)});
    fs.writeFileSync(filename, JSON.stringify(manifest));
    assert.throws(() => loadAnalysisWorkerBundle({resourcesPath: temporary, desktopVersion}), /无效文件记录/);
    fs.rmSync(temporary, {recursive: true, force: true});
  }
});

test('managed analysis worker scripts stay within the selected profile and require Python/schema verification', () => {
  const profile = {scopeId: '1234567890abcdef', profileId: 'project-a'};
  const rootScript = buildAnalysisWorkerRootScript(profile);
  const suffix = '/.local/share/heterogeneous-workbench/server-profiles/targets/1234567890abcdef/project-a/assets/analysis-worker';
  for (const segment of suffix.slice(1).split('/')) assert.match(rootScript, new RegExp(`current="\\$current/${segment.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}"`));
  const root = `/home/user${suffix}`;
  assert.equal(parseAnalysisWorkerRoot(`noise\nHETERO_ANALYSIS_WORKER_ROOT\t${root}\n`, profile), root);
  assert.throws(() => parseAnalysisWorkerRoot('HETERO_ANALYSIS_WORKER_ROOT\t/tmp/outside\n', profile), /托管路径/);

  const identity = 'a'.repeat(64);
  const finalRoot = `${root}/${identity}`;
  const stagingRoot = `${root}/.partial-${identity.slice(0, 16)}-${'b'.repeat(24)}`;
  const verify = buildAnalysisWorkerVerifyScript({root: finalRoot, identity, pythonBin: '/usr/bin/python3.10'});
  assert.match(verify, /sys\.version_info < \(3,10\)/);
  assert.match(verify, /SCHEMA_VERSION!='desktop_platform_rpc\/v2'/);
  assert.match(verify, /replace\('\\t',' '\)\.replace\('\\r',' '\)\.replace\('\\n',' '\)/);
  assert.doesNotMatch(verify, /replace\('\\\\t',' '\)/);
  const prepare = buildAnalysisWorkerPrepareScript({baseRoot: root, stagingRoot,
    files: [{path: 'backend/tools/desktop_platform_rpc.py'}]});
  assert.match(prepare, /\.partial-/);
  const activate = buildAnalysisWorkerActivateScript({baseRoot: root, stagingRoot, finalRoot, identity,
    pythonBin: '/usr/bin/python3.10'});
  assert.match(activate, /mv -- "\$staging" "\$final"/);
  assert.match(activate, /analysis worker file path is invalid/);
  assert.match(activate, /\.deploy-lock-/);
  assert.match(activate, /time\.time\(\)-info\.st_mtime<=600/);
  assert.match(activate, /attempt.*-ge 300/);
  assert.ok(activate.lastIndexOf('HETERO_FINAL_INTEGRITY') < activate.lastIndexOf('HETERO_ANALYSIS_WORKER_READY'));
  const inspect = buildAnalysisWorkerInspectScript({root: finalRoot, identity, pythonBin: '/usr/bin/python3.10'});
  assert.match(inspect, /PYTHON_UNAVAILABLE/);
  assert.deepEqual(parseAnalysisWorkerStatus('noise\nHETERO_ANALYSIS_WORKER_STATUS\tDEPENDENCY_UNAVAILABLE\tmissing fastapi\n'),
    {status: 'DEPENDENCY_UNAVAILABLE', message: 'missing fastapi'});
  assert.equal(parseAnalysisWorkerReady(`HETERO_ANALYSIS_WORKER_READY\t${finalRoot}\n`, finalRoot), finalRoot);
});

function executeBash(script) {
  return new Promise(resolve => {
    const child = spawn('bash', ['-c', script], {stdio: ['ignore', 'pipe', 'pipe']});
    let stdout = ''; let stderr = '';
    child.stdout.setEncoding('utf8'); child.stderr.setEncoding('utf8');
    child.stdout.on('data', value => { stdout += value; });
    child.stderr.on('data', value => { stderr += value; });
    child.on('close', status => resolve({status, stdout, stderr}));
  });
}

test('first-use managed root creation tolerates concurrent Desktop clients', {
  skip: process.platform === 'win32' ? 'Remote analysis workers run on a POSIX SSH target.' : false,
}, async () => {
  const temporary = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-worker-root-race-')));
  const home = path.join(temporary, 'home');
  const assets = path.join(home, '.local/share/heterogeneous-workbench/server-profiles/targets/1234567890abcdef/project-a/assets');
  const wrapperBin = path.join(temporary, 'bin');
  fs.mkdirSync(assets, {recursive: true});
  fs.mkdirSync(wrapperBin);
  const mkdirWrapper = path.join(wrapperBin, 'mkdir');
  fs.writeFileSync(mkdirWrapper, '#!/bin/sh\ncase "$*" in *analysis-worker*) sleep 0.2 ;; esac\nexec /bin/mkdir "$@"\n', {mode: 0o755});
  const script = buildAnalysisWorkerRootScript({scopeId: '1234567890abcdef', profileId: 'project-a'});
  const run = () => new Promise(resolve => {
    const child = spawn('bash', ['-c', script], {
      env: {...process.env, HOME: home, PATH: `${wrapperBin}:${process.env.PATH || '/usr/bin:/bin'}`},
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = ''; let stderr = '';
    child.stdout.setEncoding('utf8'); child.stderr.setEncoding('utf8');
    child.stdout.on('data', value => { stdout += value; });
    child.stderr.on('data', value => { stderr += value; });
    child.on('close', status => resolve({status, stdout, stderr}));
  });
  const results = await Promise.all([run(), run()]);
  const expected = path.join(assets, 'analysis-worker');
  for (const result of results) {
    assert.equal(result.status, 0, result.stderr);
    assert.equal(parseAnalysisWorkerRoot(result.stdout, {scopeId: '1234567890abcdef', profileId: 'project-a'}), expected);
  }
  assert.ok(fs.statSync(expected).isDirectory());
  fs.rmSync(temporary, {recursive: true, force: true});
});

test('cross-client activation serializes install, reuses the final tree and recovers an abandoned lock', {
  skip: process.platform === 'win32' ? 'Remote analysis workers run on a POSIX SSH target.' : false,
  timeout: 120000,
}, async () => {
  const temporary = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-worker-activation-race-')));
  const resourceParent = path.join(temporary, 'resources');
  const resourceRoot = path.join(resourceParent, 'analysis-worker');
  prepareAnalysisWorker({output: resourceRoot});
  const currentBundle = loadAnalysisWorkerBundle({resourcesPath: resourceParent, desktopVersion});
  const baseRoot = path.join(temporary, 'managed'); fs.mkdirSync(baseRoot);
  const identity = currentBundle.manifest.identity;
  const finalRoot = path.join(baseRoot, identity);
  const stages = ['1'.repeat(24), '2'.repeat(24)].map(token =>
    path.join(baseRoot, `.partial-${identity.slice(0, 16)}-${token}`));
  for (const stage of stages) fs.cpSync(resourceRoot, stage, {recursive: true});
  const lock = path.join(baseRoot, `.deploy-lock-${identity}`);
  fs.mkdirSync(lock);
  const old = new Date(Date.now() - 11 * 60 * 1000);
  fs.utimesSync(lock, old, old);
  const scripts = stages.map(stagingRoot => buildAnalysisWorkerActivateScript({
    baseRoot, stagingRoot, finalRoot, identity, pythonBin: process.env.PYTHON || 'python3',
  }));
  const values = await Promise.all(scripts.map(executeBash));
  for (const value of values) {
    assert.equal(value.status, 0, value.stderr);
    assert.equal(parseAnalysisWorkerReady(value.stdout, finalRoot), finalRoot);
  }
  assert.ok(fs.statSync(finalRoot).isDirectory());
  assert.deepEqual(fs.readdirSync(baseRoot), [identity]);
  assert.ok(!fs.readdirSync(finalRoot).some(name => name.startsWith('.partial-')));
  fs.rmSync(temporary, {recursive: true, force: true});
});

test('activation retains an integrity-valid worker when readiness fails and reports a single-line status', {
  skip: process.platform === 'win32' ? 'Remote analysis workers run on a POSIX SSH target.' : false,
  timeout: 120000,
}, async () => {
  const temporary = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-worker-readiness-')));
  const resourceRoot = path.join(temporary, 'resource');
  prepareAnalysisWorker({output: resourceRoot});
  const dependency = path.join(resourceRoot, 'backend/core.py');
  fs.writeFileSync(dependency, 'raise ModuleNotFoundError("fixture\\tline\\nnext")\n');
  const manifestPath = path.join(resourceRoot, 'analysis-worker-manifest.json');
  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  const record = manifest.files.find(file => file.path === 'backend/core.py');
  record.size = fs.statSync(dependency).size;
  record.sha256 = crypto.createHash('sha256').update(fs.readFileSync(dependency)).digest('hex');
  manifest.identity = crypto.createHash('sha256').update(JSON.stringify({
    version: manifest.version, files: manifest.files,
  })).digest('hex');
  fs.writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);

  const baseRoot = path.join(temporary, 'managed'); fs.mkdirSync(baseRoot);
  const stagingRoot = path.join(baseRoot, `.partial-${manifest.identity.slice(0, 16)}-${'3'.repeat(24)}`);
  const finalRoot = path.join(baseRoot, manifest.identity);
  fs.cpSync(resourceRoot, stagingRoot, {recursive: true});
  const activated = await executeBash(buildAnalysisWorkerActivateScript({
    baseRoot, stagingRoot, finalRoot, identity: manifest.identity, pythonBin: process.env.PYTHON || 'python3',
  }));
  assert.equal(activated.status, 7, activated.stderr);
  assert.deepEqual(parseAnalysisWorkerStatus(activated.stderr), {
    status: 'DEPENDENCY_UNAVAILABLE', message: 'ModuleNotFoundError: fixture line next',
  });
  assert.equal(activated.stderr.trim().split(/\r?\n/).length, 1);
  assert.ok(fs.statSync(finalRoot).isDirectory());
  assert.equal(fs.existsSync(stagingRoot), false);

  const inspected = await executeBash(buildAnalysisWorkerInspectScript({
    root: finalRoot, identity: manifest.identity, pythonBin: process.env.PYTHON || 'python3',
  }));
  assert.equal(inspected.status, 0, inspected.stderr);
  assert.equal(parseAnalysisWorkerStatus(inspected.stdout).status, 'DEPENDENCY_UNAVAILABLE');
  assert.ok(fs.statSync(finalRoot).isDirectory());
  fs.rmSync(temporary, {recursive: true, force: true});
});

test('symlinked home remains valid while managed path and worker-root symlinks are rejected', {
  skip: process.platform === 'win32' ? 'Remote analysis workers run on a POSIX SSH target.' : false,
}, () => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-analysis-worker-link-'));
  const home = path.join(temporary, 'home');
  const linkedHome = path.join(temporary, 'linked-home');
  const outside = path.join(temporary, 'outside');
  const assets = path.join(home, '.local/share/heterogeneous-workbench/server-profiles/targets/1234567890abcdef/project-a/assets');
  fs.mkdirSync(assets, {recursive: true}); fs.mkdirSync(outside);
  fs.symlinkSync(home, linkedHome, 'dir');
  const profile = {scopeId: '1234567890abcdef', profileId: 'project-a'};
  const linkedHomeAttempt = spawnSync('bash', ['-c', buildAnalysisWorkerRootScript(profile)], {
    env: {...process.env, HOME: linkedHome}, encoding: 'utf8',
  });
  assert.equal(linkedHomeAttempt.status, 0, linkedHomeAttempt.stderr);
  assert.match(linkedHomeAttempt.stdout, new RegExp(`${fs.realpathSync(home).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}/\\.local/`));
  fs.rmSync(assets, {recursive: true, force: true});
  fs.symlinkSync(outside, assets, 'dir');
  const rootAttempt = spawnSync('bash', ['-c', buildAnalysisWorkerRootScript(profile)], {
    env: {...process.env, HOME: home}, encoding: 'utf8',
  });
  assert.notEqual(rootAttempt.status, 0);
  assert.match(rootAttempt.stderr, /不能包含符号链接/);

  const realWorker = path.join(temporary, 'real-worker');
  prepareAnalysisWorker({output: realWorker});
  const manifest = JSON.parse(fs.readFileSync(path.join(realWorker, 'analysis-worker-manifest.json')));
  const linkedWorker = path.join(temporary, 'linked-worker');
  fs.symlinkSync(realWorker, linkedWorker, 'dir');
  const verifyAttempt = spawnSync('bash', ['-c', buildAnalysisWorkerVerifyScript({
    root: linkedWorker, identity: manifest.identity, pythonBin: process.env.PYTHON || 'python3',
  })], {encoding: 'utf8'});
  assert.notEqual(verifyAttempt.status, 0);

  fs.rmSync(assets, {force: true});
  fs.mkdirSync(assets, {recursive: true});
  fs.symlinkSync(outside, path.join(assets, 'analysis-worker'), 'dir');
  const workerRootAttempt = spawnSync('bash', ['-c', buildAnalysisWorkerRootScript(profile)], {
    env: {...process.env, HOME: home}, encoding: 'utf8',
  });
  assert.notEqual(workerRootAttempt.status, 0);
  assert.match(workerRootAttempt.stderr, /不能包含符号链接/);
  fs.rmSync(temporary, {recursive: true, force: true});
});

test('remote verifier rejects hostile manifest metadata before touching declared paths', {
  skip: process.platform === 'win32' ? 'Remote analysis workers run on a POSIX SSH target.' : false,
}, () => {
  const cases = [
    {name: 'traversal', files: [{path: '../outside', size: 0, sha256: '0'.repeat(64)}], expected: /file path is invalid/},
    {name: 'too-many', files: Array.from({length: 257}, (_, index) => ({path: `backend/${index}.py`, size: 0, sha256: '0'.repeat(64)})), expected: /manifest contract is invalid/},
    {name: 'oversize', files: [{path: 'backend/a.py', size: 67108865, sha256: '0'.repeat(64)}], expected: /file size is invalid/},
    {name: 'bad-hash', files: [{path: 'backend/a.py', size: 0, sha256: 'z'.repeat(64)}], expected: /file hash is invalid/},
  ];
  for (const current of cases) {
    const temporary = fs.mkdtempSync(path.join(os.tmpdir(), `desktop-worker-hostile-${current.name}-`));
    const root = path.join(temporary, 'worker'); fs.mkdirSync(root);
    const identity = 'a'.repeat(64);
    fs.writeFileSync(path.join(root, 'analysis-worker-manifest.json'), JSON.stringify({
      schema_version: 'heterogeneous_desktop_analysis_worker/v1', version: 'test', identity, files: current.files,
    }));
    fs.writeFileSync(path.join(temporary, 'outside'), 'must not be opened');
    const attempt = spawnSync('bash', ['-c', buildAnalysisWorkerVerifyScript({
      root, identity, pythonBin: process.env.PYTHON || 'python3',
    })], {encoding: 'utf8'});
    assert.notEqual(attempt.status, 0);
    assert.match(attempt.stderr, current.expected);
    fs.rmSync(temporary, {recursive: true, force: true});
  }
});
