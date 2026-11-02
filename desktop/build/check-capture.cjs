'use strict';

// Exercise the actual Python runtime on the host OS, including Windows CRT
// filesystem behavior. No HDC, phone, user settings or AppData writes are used.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const {captureEnvironment} = require('../runtime/capture-environment.cjs');

function checkCapture() {
  const appPath = path.resolve(__dirname, '..');
  const environment = captureEnvironment({appPath});
  const entry = path.join(environment.runtimeRoot, 'tools/scenario_capture/desktop_entry.py');
  if (!fs.existsSync(entry)) throw new Error('Capture runtime missing. Run npm run build first.');
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-capture-check-'));
  const outputRoot = path.join(temporary, '采集 self check', 'packages');
  const serial = 'SELF-CHECK-NO-DEVICE';

  function python(args, input = '') {
    const [executable, ...prefix] = environment.pythonCommand;
    const result = spawnSync(executable, [...prefix, ...args], {
      ...environment.launchOptions, cwd: appPath, input, encoding: 'utf8',
      windowsHide: true, timeout: 120000, maxBuffer: 8 * 1024 * 1024,
    });
    if (result.error) throw new Error(`Python self-check could not run: ${result.error.message}`);
    if (result.status !== 0) throw new Error(`Python self-check failed (exit ${result.status}):\n${result.stdout || ''}${result.stderr || ''}`);
    return result.stdout;
  }

  function invoke(args, input) {
    const records = python([entry, ...args], input).trim().split(/\r?\n/).filter(Boolean).map(line => JSON.parse(line));
    const result = records.find(record => record.type === 'result');
    assert.ok(result, 'Python entry returned no result');
    return result;
  }

  try {
    const catalog = invoke(['catalog']);
    assert.equal(catalog.schema_version, 'scenario_capture_catalog/v1');
    assert.equal(catalog.pmu_sampler.platform_bundle.status, 'ready');
    const payload = {
      template_id: 'custom', title: '本机保存自检',
      steps: [{step_id: 'step_01', title: '等待页面稳定', operation: 'wait', seconds: 1}],
      capture: {selected_step_ids: ['step_01'], collectors: ['trace']},
    };
    const save = value => invoke(['save-plan', '--input', '-', '--output-root', outputRoot,
      '--device-serial', serial], JSON.stringify(value));
    const first = save(payload);
    const repeated = save(payload);
    const changed = save({...payload, title: '修改后的采集计划'});
    assert.equal(first.plan_id, repeated.plan_id, 'Identical plan identity changed');
    assert.notEqual(first.package_directory, repeated.package_directory, 'Repeat save overwrote an immutable package');
    assert.notEqual(first.plan_id, changed.plan_id, 'Changed plan retained old identity');

    // Validate from the generated package's own module closure, not the repo.
    const validation = python(['-c', [
      'import json, sys',
      'from pathlib import Path',
      'records = json.load(sys.stdin)',
      'sys.path.insert(0, str(Path(records[0]["package_directory"]) / "runtime"))',
      'from tools.scenario_capture.run_local_package import load_package',
      'for record in records:',
      '    root, package, plan = load_package(Path(record["package_directory"]))',
      '    assert plan["artifact_id"] == record["plan_id"]',
      '    assert plan["title"] == record["plan"]["title"]',
      '    assert package["selected_target"]["device_serial"] == "SELF-CHECK-NO-DEVICE"',
      '    assert Path(record["package_zip"]).is_file()',
      'print(json.dumps({"validated_packages": len(records)}))',
    ].join('\n')], JSON.stringify([first, repeated, changed]));
    assert.equal(JSON.parse(validation).validated_packages, 3);
    console.log(`PASS: native ${process.platform} capture catalog, repeated/changed plan saves and 3 package validations (no device required).`);
  } finally {
    fs.rmSync(temporary, {recursive: true, force: true, maxRetries: 3, retryDelay: 150});
  }
}

if (require.main === module) {
  try { checkCapture(); }
  catch (error) { console.error(`FAIL: ${error.message}`); process.exitCode = 1; }
}

module.exports = {checkCapture};
