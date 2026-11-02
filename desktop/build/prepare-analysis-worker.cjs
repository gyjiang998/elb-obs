'use strict';

// Package the allow-listed Desktop analysis worker with the application.  A
// Linux checkout may intentionally contain only product sources or may lag the
// Windows Desktop build; analysis must not depend on mutating that checkout.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {resolveBenchmarkBundle} = require('../runtime/calibration.cjs');

const root = path.resolve(__dirname, '../..');
const defaultOutput = path.join(__dirname, 'resources/analysis-worker');
const calibrationRelative = path.join('tools', 'parallel_calibration', 'prebuilt', 'aarch64-linux-ohos');
const defaultCalibrationBundleRoot = path.join(root, calibrationRelative);
const backendFiles = [
  '__init__.py',
  'core.py',
  'capture_runtime_trace.py',
  'application_flow.py',
  'application_flow_trace.py',
  'htrace_analysis.py',
  'hitrace_instrumentation.py',
  'trace_modules.py',
  'generic_code_semantic_profile.py',
  'hardware_semantic_routing.py',
  'hardware_unit_routing.py',
  'accelerator_api_retrieval.py',
  'npu_api_retrieval.py',
  'deployment_mode.py',
  'local_capture_package.py',
  'source_revision_snapshot.py',
  'trace_library.py',
  'desktop_workspace_rpc.py',
  'dpvs_analysis.py',
  'custom_api_kb.py',
  'code_graph.py',
  'platform_paths.py',
  'skills.py',
  'skill_versions.py',
  'user_skills.py',
  'analysis_observability.py',
  'experience_evolution.py',
];

function copyTree(source, destination, filter = () => true) {
  fs.cpSync(source, destination, {
    recursive: true,
    filter(filename) {
      const name = path.basename(filename);
      return !name.startsWith('test_')
        && !['__pycache__', '.DS_Store'].includes(name)
        && !name.endsWith('.pyc')
        && filter(filename);
    },
  });
}

function digest(filename) {
  return crypto.createHash('sha256').update(fs.readFileSync(filename)).digest('hex');
}

function prepareAnalysisWorker({output = defaultOutput, calibrationBundleRoot = defaultCalibrationBundleRoot} = {}) {
  // Validate the inner content-addressed benchmark before deleting a previous
  // known-good worker or computing the outer analysis-worker identity.
  const calibrationBundle = resolveBenchmarkBundle({bundleRoot: calibrationBundleRoot});
  fs.rmSync(output, {recursive: true, force: true});
  fs.mkdirSync(path.join(output, 'backend/tools'), {recursive: true});
  for (const file of backendFiles)
    fs.copyFileSync(path.join(root, 'backend', file), path.join(output, 'backend', file));
  fs.copyFileSync(path.join(root, 'backend/tools/__init__.py'), path.join(output, 'backend/tools/__init__.py'));
  fs.copyFileSync(path.join(root, 'backend/tools/desktop_platform_rpc.py'), path.join(output, 'backend/tools/desktop_platform_rpc.py'));
  copyTree(path.join(root, 'backend/parallel_modeling'), path.join(output, 'backend/parallel_modeling'),
    filename => !path.basename(filename).startsWith('test_'));
  copyTree(path.join(root, 'data/parallel-modeling'), path.join(output, 'data/parallel-modeling'));
  copyTree(path.join(root, 'data/knowledge-bases'), path.join(output, 'data/knowledge-bases'));
  fs.copyFileSync(path.join(root, 'data/hitrace_tag_source_scope_hints.json'),
    path.join(output, 'data/hitrace_tag_source_scope_hints.json'));
  copyTree(path.join(root, 'agent-resources/skill-library'), path.join(output, 'agent-resources/skill-library'));
  // Current executable Skills belong in the Desktop bundle as well.  They are
  // materialized on the PC for the local Code Agent and their reference
  // schemas are also consumed by the server-side, transport-neutral manager.
  copyTree(path.join(root, 'agent-resources/skills'), path.join(output, 'agent-resources/skills'));
  copyTree(path.join(root, 'tools/parallel_calibration'), path.join(output, 'tools/parallel_calibration'));
  const packagedCalibrationRoot = path.join(output, calibrationRelative);
  if (path.resolve(calibrationBundleRoot) !== path.resolve(defaultCalibrationBundleRoot)) {
    fs.rmSync(packagedCalibrationRoot, {recursive: true, force: true});
    copyTree(calibrationBundleRoot, packagedCalibrationRoot);
  }
  // Existing Windows worktrees may already contain a receipt rewritten to
  // CRLF.  The verifier accepts it only when CRLF -> LF exactly recovers the
  // manifest hash; the packaged worker always emits those canonical LF bytes.
  fs.writeFileSync(path.join(packagedCalibrationRoot, ...calibrationBundle.manifest.receipt.split('/')),
    calibrationBundle.receiptContents);
  const packagedCalibration = resolveBenchmarkBundle({bundleRoot: packagedCalibrationRoot});
  if (packagedCalibration.receiptNormalization !== 'none')
    throw new Error('Packaged calibration receipt was not written with canonical LF bytes.');

  const files = [];
  function walk(directory) {
    for (const entry of fs.readdirSync(directory, {withFileTypes: true})) {
      const filename = path.join(directory, entry.name);
      if (entry.isSymbolicLink()) throw new Error(`Analysis Worker resource cannot contain a symbolic link: ${filename}`);
      if (entry.isDirectory()) walk(filename);
      else if (entry.isFile()) files.push({
        path: path.relative(output, filename).split(path.sep).join('/'),
        size: fs.statSync(filename).size,
        sha256: digest(filename),
      });
    }
  }
  walk(output);
  files.sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0);
  const version = JSON.parse(fs.readFileSync(path.join(root, 'desktop/package.json'), 'utf8')).version;
  const identity = crypto.createHash('sha256').update(JSON.stringify({version, files})).digest('hex');
  fs.writeFileSync(path.join(output, 'analysis-worker-manifest.json'), `${JSON.stringify({
    schema_version: 'heterogeneous_desktop_analysis_worker/v1',
    version,
    identity,
    files,
  }, null, 2)}\n`);
  return output;
}

if (require.main === module) console.log(prepareAnalysisWorker());
module.exports = {prepareAnalysisWorker};
