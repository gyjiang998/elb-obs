'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const {spawnSync} = require('node:child_process');
const {CalibrationManager, normalizeInputs, parseHdcTargetSerials, resolveBenchmarkBundle, validateDeviceHardwareProfile,
  validateFrequencyEvidenceMeasurement, validatePairedDispatchMeasurement,
  validatePairHelperPlacementMeasurement, validateTargetSiblingQuietnessMeasurement} = require('../runtime/calibration.cjs');

const CALIBRATION_RELATIVE = path.join('tools', 'parallel_calibration', 'prebuilt', 'aarch64-linux-ohos');
const SOURCE_BUNDLE = path.resolve(__dirname, '..', '..', CALIBRATION_RELATIVE);

const PROFILE = {
  schema_version: 'parallel_hardware_profile/v1', id: 'hp_fixture',
  core_groups: [
    {id: 'cluster_0', proxy_kind: 'small', physical_core_count: 2, logical_cpu_ids: [0, 1], opps_khz: [500000, 1000000]},
    {id: 'cluster_1', proxy_kind: 'big', physical_core_count: 2, logical_cpu_ids: [2, 3], opps_khz: [1000000, 2000000]},
  ],
};
const CONDITION = {
  id: 'condition_0123456789ab', benchmark: 'cpu_work', family: 'build', workload_id: 'object_tree_v1',
  core_kind: 'cluster_1', frequency_khz: 2000000, active_tasks: 1, work_units: 10000,
  split: 'train', repeats: 1,
};
const PLAN = {
  schema_version: 'parallel_calibration_plan/v1', id: 'cp_0123456789abcdef01234567',
  hardware_profile_id: PROFILE.id, conditions: [CONDITION],
};

const SIBLING_QUIETNESS = {
  target_sibling_cpus: [], target_sibling_activity: [],
  target_sibling_activity_verified: true, target_sibling_busy_fraction: 0,
  target_sibling_busy_threshold: 0.05, target_sibling_quietness_verified: true,
};

function frequencyEvidence({cpu, frequencyKhz, policyId, workUnits = 10000, benchmark = 'cpu_work',
    source = 'cpuinfo_cur_freq', windowKhz = frequencyKhz, warmupVerified = true} = {}) {
  const fallbackPath = String(policyId).startsWith('policy')
    ? `/sys/devices/system/cpu/cpufreq/${policyId}/scaling_cur_freq`
    : `/sys/devices/system/cpu/${policyId}/cpufreq/scaling_cur_freq`;
  const warmupCount = warmupVerified ? 3 : 12;
  const chunk = benchmark === 'empty_dispatch' ? 32 : Math.min(workUnits, 32768);
  return {
    workload_version: 'parallel-calibration-proxy-v6',
    frequency_source: source,
    frequency_source_cpu: cpu,
    frequency_source_path: source === 'cpuinfo_cur_freq'
      ? `/sys/devices/system/cpu/cpu${cpu}/cpufreq/cpuinfo_cur_freq` : fallbackPath,
    frequency_sample_count: 8,
    frequency_read_failures: 0,
    frequency_mean_khz: windowKhz,
    frequency_min_khz: windowKhz,
    frequency_max_khz: windowKhz,
    frequency_verified: source === 'cpuinfo_cur_freq' && warmupVerified
      && Math.abs(windowKhz / frequencyKhz - 1) <= 0.03,
    warmup_frequency_sample_count: warmupCount,
    warmup_frequency_in_range_consecutive: warmupVerified ? 3 : 0,
    warmup_frequency_required_consecutive: 3,
    warmup_frequency_max_samples: 12,
    warmup_frequency_min_khz: warmupVerified ? frequencyKhz : windowKhz,
    warmup_frequency_max_khz: warmupVerified ? frequencyKhz : windowKhz,
    warmup_frequency_last_khz: warmupVerified ? frequencyKhz : windowKhz,
    warmup_frequency_verified: warmupVerified,
    warmup_work_units: warmupCount * chunk,
  };
}

const PAIRED_DISPATCH_MEASUREMENT = {
  workload_version: 'parallel-calibration-proxy-v6', benchmark: 'empty_dispatch', work_units: 10000,
  dispatch_measurement_schema: 'paired_dispatch_delta/v1', dispatch_payload: 'mix64_v1',
  dispatch_payload_match: true, cold_start_included: false,
  direct_batch_worker_cpu_ms: 1.2, direct_batch_worker_wall_ms: 1.4,
  direct_batch_producer_cpu_ms: 0.02, direct_batch_roundtrip_wall_ms: 1.5,
  queue_dispatch_worker_cpu_ms: 7.2, queue_dispatch_worker_wall_ms: 18.0,
  queue_dispatch_producer_cpu_ms: 6.02, queue_dispatch_roundtrip_wall_ms: 18.5,
  dispatch_wall_delta_ms_per_task: (18.5 - 1.5) / 10000,
  dispatch_worker_cpu_delta_ms_per_task: (7.2 - 1.2) / 10000,
  dispatch_producer_cpu_delta_ms_per_task: (6.02 - 0.02) / 10000,
};

async function until(predicate, timeoutMs = 4000) {
  const started = Date.now();
  while (!predicate()) {
    if (Date.now() - started > timeoutMs) throw new Error('condition timed out');
    await new Promise(resolve => setTimeout(resolve, 10));
  }
}

function persistedText(root) {
  const contents = [];
  const visit = directory => {
    for (const entry of fs.readdirSync(directory, {withFileTypes: true})) {
      const filename = path.join(directory, entry.name);
      if (entry.isDirectory()) visit(filename);
      else if (entry.isFile()) contents.push(fs.readFileSync(filename));
    }
  };
  visit(root);
  return Buffer.concat(contents).toString('utf8');
}

function createFixture({writable = true, blockBenchmark = false, failRestore = false,
    failRestoreCount = null, failRemoteCleanup = false, failRemoteCleanupCount = null,
    blockThermal = false, blockRestore = false, osBuildError = null,
    siblingBusyCount = 0, frequencyInvalidCount = 0, frequencyFallbackCount = 0,
    directory = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-calibration-')),
    listedDevices = [], listTargetsError = null} = {}) {
  const calls = [];
  let remainingRestoreFailures = failRestoreCount == null ? (failRestore ? Number.POSITIVE_INFINITY : 0) : failRestoreCount;
  let remainingRemoteCleanupFailures = failRemoteCleanupCount == null
    ? (failRemoteCleanup ? Number.POSITIVE_INFINITY : 0) : failRemoteCleanupCount;
  let remainingSiblingBusy = siblingBusyCount;
  let remainingFrequencyInvalid = frequencyInvalidCount;
  let remainingFrequencyFallback = frequencyFallbackCount;
  const manager = new CalibrationManager({
    userDataDir: directory,
    resourcesDir: path.resolve(__dirname, '..'),
    sleepImpl: async () => {},
    execFileImpl(executable, args, options, callback) {
      let completed = false;
      const finish = (error, stdout = '', stderr = '') => {
        if (completed) return;
        completed = true;
        queueMicrotask(() => callback(error, stdout, stderr));
      };
      const child = {killed: false, kill() {
        this.killed = true;
        if ((blockBenchmark && command.includes('--benchmark'))
            || (blockThermal && command.includes('/sys/class/thermal/thermal_zone'))
            || (blockRestore && command.includes('current=$(cat "$base/scaling_max_freq" 2>/dev/null'))) {
          const error = new Error('terminated'); error.code = 'SIGTERM'; finish(error);
        }
      }};
      calls.push({executable, args: [...args], options, child});
      const command = args[2] === 'shell' ? args[3] : '';
      const isRestore = command.includes('current=$(cat "$base/scaling_max_freq" 2>/dev/null');
      const isRemoteCleanup = command.includes("pkill -f '(^|[[:space:]])/data/local/tmp/[h]etero_calibration_");
      const siblingQuietness = command.includes('--benchmark') && remainingSiblingBusy-- > 0
        ? {target_sibling_cpus: [4],
          target_sibling_activity: [{cpu: 4, busy_ticks: 2, total_ticks: 10, busy_fraction: 0.2, verified: true}],
          target_sibling_activity_verified: true, target_sibling_busy_fraction: 0.2,
          target_sibling_busy_threshold: 0.05, target_sibling_quietness_verified: false}
        : SIBLING_QUIETNESS;
      const frequencyState = (cpu, frequencyKhz, workUnits, benchmark) => {
        const policyId = command.match(/--frequency-policy-id ([A-Za-z0-9_-]+)/)?.[1];
        const fallback = command.includes('--benchmark') && remainingFrequencyFallback-- > 0;
        const invalid = !fallback && command.includes('--benchmark') && remainingFrequencyInvalid-- > 0;
        return frequencyEvidence({cpu, frequencyKhz, policyId, workUnits, benchmark,
          source: fallback ? 'scaling_cur_freq_fallback' : 'cpuinfo_cur_freq',
          windowKhz: invalid ? Math.round(frequencyKhz * 0.8) : frequencyKhz});
      };
      let stdout = '';
      if (args[0] === 'list' && args[1] === 'targets' && listTargetsError) {
        const error = new Error(listTargetsError.message || 'list targets failed'); error.code = listTargetsError.code || 1;
        finish(error, listTargetsError.stdout || '', listTargetsError.stderr || '');
        return child;
      }
      if (args[0] === 'list' && args[1] === 'targets') stdout = `${listedDevices.join('\n')}${listedDevices.length ? '\n' : ''}`;
      else if (command.includes('const.ohos.fullname') && osBuildError) {
        const error = new Error(osBuildError.message || 'os build probe failed'); error.code = osBuildError.code || 1;
        finish(error, osBuildError.stdout || '', osBuildError.stderr || '');
        return child;
      }
      else if (command.includes('const.ohos.fullname')) stdout = 'OpenHarmony fixture\n';
      else if (command.includes("printf 'min='")) {
        const policy = command.match(/\/sys\/devices\/system\/cpu\/cpufreq\/policy\d+/)?.[0];
        const perCpu = command.match(/\/sys\/devices\/system\/cpu\/cpu\d+\/cpufreq/)?.[0];
        stdout = `base=${policy || perCpu}\nmin=500000\nmax=2000000\nwritable=${writable ? 1 : 0}\n`;
      }
      else if (command.includes('/sys/class/thermal/thermal_zone')) stdout = 'thermal_zone0\tcpu\t41000\n';
      else if (command.includes('--benchmark roofline')) stdout = `${JSON.stringify({
        schema_version: 'parallel_calibration_measurement/v1',
        benchmark: 'roofline', family: 'roofline', workload_class: 'intensity_ladder',
        frequency_khz: 1000000, active_tasks: 1, work_units: 10000,
        working_set_bytes: 1048576, roofline_operations_per_element: 8,
        roofline_kernel_version: 'classic-roofline-blocked-v2',
        roofline_kernel_execution: 'blocked_fp64_16way_ilp', roofline_ilp_lanes: 16,
        roofline_generic_even_operations_supported: true, roofline_quality_gate_required: true,
        metric_basis: 'algorithmic_flop_per_minimum_source_byte', algorithmic_flops: 80000,
        algorithmic_bytes_minimum: 240000, operational_intensity_flop_per_byte: 1 / 3,
        attainable_gflops: 0.008, source_level_gbytes_per_s: 0.024,
        working_set_coverage_verified: true, cpu_ms: 9.8, wall_ms: 10, cpu: 0, helpers: [], observer_cpu: 3,
        affinity_verified: true, physical_cores_verified: true, physical_core_placement_verified: true,
        observer_independent_core: true, concurrency_verified: true,
        ...siblingQuietness,
        frequency_policy_id: command.match(/--frequency-policy-id ([A-Za-z0-9_-]+)/)?.[1],
        ...frequencyState(0, 1000000, 10000, 'roofline'),
      })}\n`;
      else if (command.includes('--benchmark cpu_work')) stdout = `${JSON.stringify({
        schema_version: 'parallel_calibration_measurement/v1',
        benchmark: 'cpu_work', family: 'build', frequency_khz: 2000000, active_tasks: 1,
        work_units: 10000, cpu_ms: 1.25, wall_ms: 1.4, cpu: 2, helpers: [], observer_cpu: 3,
        affinity_verified: true, physical_cores_verified: true, physical_core_placement_verified: true,
        observer_independent_core: true, concurrency_verified: true,
        ...siblingQuietness,
        frequency_policy_id: command.match(/--frequency-policy-id ([A-Za-z0-9_-]+)/)?.[1],
        ...frequencyState(2, 2000000, 10000, 'cpu_work'),
      })}\n`;
      if (remainingRestoreFailures > 0 && isRestore) {
        remainingRestoreFailures -= 1;
        const error = new Error('restore rejected'); error.code = 1; finish(error, '', 'restore rejected');
      } else if (remainingRemoteCleanupFailures > 0 && isRemoteCleanup) {
        remainingRemoteCleanupFailures -= 1;
        const error = new Error('cleanup rejected'); error.code = 1; finish(error, '', 'cleanup rejected');
      } else if (!((blockBenchmark && command.includes('--benchmark'))
          || (blockThermal && command.includes('/sys/class/thermal/thermal_zone'))
          || (blockRestore && isRestore))) finish(null, stdout, '');
      return child;
    },
  });
  return {manager, calls, directory};
}

function fixture(t, options = {}) {
  const result = createFixture(options);
  const {manager, directory} = result;
  t.after(async () => { await manager.dispose(); fs.rmSync(directory, {recursive: true, force: true}); });
  return result;
}

test('packaged benchmark is content-addressed and verified as an AArch64 ELF', () => {
  const bundle = resolveBenchmarkBundle({bundleRoot: SOURCE_BUNDLE});
  assert.match(bundle.manifest.binary_sha256, /^[a-f0-9]{64}$/);
  assert.equal(fs.readFileSync(bundle.binary).readUInt16LE(18), 183);
  const receipt = JSON.parse(bundle.receiptContents.toString('utf8'));
  const source = fs.readFileSync(path.resolve(__dirname, '..', '..', 'tools/parallel_calibration/benchmark.c'));
  assert.equal(receipt.source_sha256, crypto.createHash('sha256').update(source).digest('hex'),
    'managed AArch64 binary must be rebuilt from the checked-in benchmark source');
  assert.doesNotMatch(fs.readFileSync(bundle.receipt, 'utf8'), /(?:\/Users\/|[A-Za-z]:\\\\Users\\\\)/,
    'published build receipts must not expose workstation user paths');
});

test('paired Worker dispatch contract validates same-payload totals and per-task deltas', () => {
  assert.doesNotThrow(() => validatePairedDispatchMeasurement(PAIRED_DISPATCH_MEASUREMENT));
  assert.throws(() => validatePairedDispatchMeasurement({...PAIRED_DISPATCH_MEASUREMENT, dispatch_payload_match: false}),
    error => error.code === 'CALIBRATION_MEASUREMENT_MISMATCH' && /负载结果不一致/.test(error.message));
  assert.throws(() => validatePairedDispatchMeasurement({...PAIRED_DISPATCH_MEASUREMENT,
    dispatch_wall_delta_ms_per_task: PAIRED_DISPATCH_MEASUREMENT.dispatch_wall_delta_ms_per_task + 0.001}),
  error => error.code === 'CALIBRATION_MEASUREMENT_MISMATCH' && /配对总量不一致/.test(error.message));
  // Legacy and non-dispatch samples remain readable while new managed v4
  // dispatch samples are held to the stronger contract.
  assert.doesNotThrow(() => validatePairedDispatchMeasurement({benchmark: 'empty_dispatch', workload_version: 'parallel-calibration-proxy-v3'}));
  assert.doesNotThrow(() => validatePairedDispatchMeasurement({benchmark: 'cpu_work', workload_version: 'parallel-calibration-proxy-v4'}));
});

test('target physical-core sibling quietness contract is fail-closed and internally consistent', () => {
  assert.deepEqual(validateTargetSiblingQuietnessMeasurement({cpu: 4, ...SIBLING_QUIETNESS}), {
    activityVerified: true, quietnessVerified: true, busyFraction: 0, siblingCount: 0,
  });
  const smt = {cpu: 4, target_sibling_cpus: [5],
    target_sibling_activity: [{cpu: 5, busy_ticks: 1, total_ticks: 100, busy_fraction: 0.01, verified: true}],
    target_sibling_activity_verified: true, target_sibling_busy_fraction: 0.01,
    target_sibling_busy_threshold: 0.05, target_sibling_quietness_verified: true};
  assert.doesNotThrow(() => validateTargetSiblingQuietnessMeasurement(smt));
  assert.doesNotThrow(() => validateTargetSiblingQuietnessMeasurement({...smt,
    target_sibling_activity: [{cpu: 5, busy_ticks: 10, total_ticks: 100, busy_fraction: 0.1, verified: true}],
    target_sibling_busy_fraction: 0.1, target_sibling_quietness_verified: false}));
  assert.doesNotThrow(() => validateTargetSiblingQuietnessMeasurement({cpu: 4,
    target_sibling_cpus: [5], target_sibling_activity: [{cpu: 5, busy_ticks: 0, total_ticks: 0, busy_fraction: null, verified: false}],
    target_sibling_activity_verified: false, target_sibling_busy_fraction: null,
    target_sibling_busy_threshold: 0.05, target_sibling_quietness_verified: false}));
  assert.deepEqual(validateTargetSiblingQuietnessMeasurement({cpu: 4,
    target_sibling_cpus: [], target_sibling_activity: [], target_sibling_activity_verified: false,
    target_sibling_busy_fraction: null, target_sibling_busy_threshold: 0.05,
    target_sibling_quietness_verified: false}), {
    activityVerified: false, quietnessVerified: false, busyFraction: null, siblingCount: 0,
  }, 'an unavailable topology remains structurally readable but cannot claim a non-SMT core');
  for (const invalid of [
    {cpu: 4},
    {...smt, target_sibling_quietness_verified: false},
    {...smt, target_sibling_busy_fraction: 0},
    {...smt, target_sibling_activity_verified: false},
    {...smt, target_sibling_activity: [{cpu: 5, busy_ticks: 1, total_ticks: 0, busy_fraction: 0, verified: true}]},
    {...smt, target_sibling_cpus: [4]},
  ]) assert.throws(() => validateTargetSiblingQuietnessMeasurement(invalid),
    error => error.code === 'CALIBRATION_MEASUREMENT_MISMATCH');
});

test('v6 target frequency evidence is bound to one logical CPU and audited through adaptive warmup', () => {
  const evidence = {benchmark: 'cpu_work', cpu: 4, work_units: 10000,
    frequency_khz: 1000000, frequency_policy_id: 'policy1',
    ...frequencyEvidence({cpu: 4, frequencyKhz: 1000000, policyId: 'policy1'})};
  assert.deepEqual(validateFrequencyEvidenceMeasurement(evidence), {
    target: {
      verified: true, source: 'cpuinfo_cur_freq', sourceCpu: 4,
      sourcePath: '/sys/devices/system/cpu/cpu4/cpufreq/cpuinfo_cur_freq',
      samples: 8, failures: 0, minimum: 1000000, maximum: 1000000,
      mean: 1000000, warmupVerified: true,
    }, peer: null,
  });
  const settling = {...evidence, warmup_frequency_sample_count: 5,
    warmup_frequency_min_khz: 800000, warmup_work_units: 50000};
  assert.doesNotThrow(() => validateFrequencyEvidenceMeasurement(settling),
    'early wrong-P-state samples remain auditable when the final three readings converged');
  const pair = {...evidence, benchmark: 'pair_work', peer_frequency_khz: 800000,
    peer_frequency_policy_id: 'policy0', peer_frequency_source: 'cpuinfo_cur_freq',
    peer_frequency_source_cpu: 1,
    peer_frequency_source_path: '/sys/devices/system/cpu/cpu1/cpufreq/cpuinfo_cur_freq',
    peer_frequency_sample_count: 8, peer_frequency_read_failures: 0,
    peer_observed_frequency_khz: 800000, peer_frequency_min_khz: 800000,
    peer_frequency_max_khz: 800000, peer_frequency_verified: true};
  assert.equal(validateFrequencyEvidenceMeasurement(pair, {peerCpu: 1, peerPolicyId: 'policy0'}).peer.verified, true);
  assert.throws(() => validateFrequencyEvidenceMeasurement({...pair,
    peer_frequency_source_path: '/sys/devices/system/cpu/cpu4/cpufreq/cpuinfo_cur_freq'},
  {peerCpu: 1, peerPolicyId: 'policy0'}), error => error.code === 'CALIBRATION_MEASUREMENT_MISMATCH');
  for (const invalid of [
    {...evidence, workload_version: 'parallel-calibration-proxy-v5'},
    {...evidence, frequency_source_cpu: 5},
    {...evidence, frequency_source_path: '/sys/devices/system/cpu/cpu5/cpufreq/cpuinfo_cur_freq'},
    {...evidence, warmup_frequency_sample_count: 2, warmup_work_units: 20000},
    {...evidence, warmup_work_units: 1},
    {...evidence, frequency_min_khz: 800000},
  ]) assert.throws(() => validateFrequencyEvidenceMeasurement(invalid),
    error => error.code === 'CALIBRATION_MEASUREMENT_MISMATCH');
});

test('active pair helper frequency evidence is anchored to its verified actual logical CPU', () => {
  const valid = {affinity_verified: true,
    helpers: [{cpu: 6, actual_cpu: 6, affinity_verified: true}]};
  assert.deepEqual(validatePairHelperPlacementMeasurement(valid, 6), {
    cpu: 6, actualCpu: 6, affinityVerified: true,
  });
  for (const invalid of [
    {...valid, helpers: []},
    {...valid, helpers: [{cpu: 7, actual_cpu: 7, affinity_verified: true}]},
    {...valid, helpers: [{cpu: 6, actual_cpu: 7, affinity_verified: true}]},
    {...valid, helpers: [{cpu: 6, actual_cpu: 6, affinity_verified: false}]},
  ]) assert.throws(() => validatePairHelperPlacementMeasurement(invalid, 6),
    error => error.code === 'CALIBRATION_MEASUREMENT_MISMATCH');
});

test('Git attributes keep the complete calibration prebuilt tree byte-exact', () => {
  const attributes = fs.readFileSync(path.resolve(__dirname, '..', '..', '.gitattributes'), 'utf8');
  assert.match(attributes, /^tools\/parallel_calibration\/prebuilt\/\*\* -text$/m);
});

test('package-like Windows resource path accepts only manifest-exact CRLF receipt normalization', t => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'Calibration App (portable)-'));
  t.after(() => fs.rmSync(temporary, {recursive: true, force: true}));
  const resourcesPath = path.join(temporary, 'Program Files', 'Heterogeneous Workbench', 'resources');
  const bundleRoot = path.join(resourcesPath, 'analysis-worker', CALIBRATION_RELATIVE);
  fs.mkdirSync(path.dirname(bundleRoot), {recursive: true});
  fs.cpSync(SOURCE_BUNDLE, bundleRoot, {recursive: true});
  const manifest = JSON.parse(fs.readFileSync(path.join(bundleRoot, 'manifest.json'), 'utf8'));
  const receipt = path.join(bundleRoot, ...manifest.receipt.split('/'));
  const canonicalReceipt = fs.readFileSync(receipt);
  const crlfReceipt = Buffer.from(canonicalReceipt.toString('utf8').replace(/\r?\n/g, '\r\n'));
  assert.notDeepEqual(crlfReceipt, canonicalReceipt);
  fs.writeFileSync(receipt, crlfReceipt);

  const bundle = resolveBenchmarkBundle({resourcesDir: '/missing', resourcesPath});
  assert.equal(bundle.root, bundleRoot);
  assert.equal(bundle.receiptNormalization, 'crlf-to-lf');
  assert.ok(bundle.receiptCrlfCount > 0);
  assert.deepEqual(bundle.receiptContents, canonicalReceipt);
  assert.deepEqual(fs.readFileSync(bundle.binary), fs.readFileSync(path.join(SOURCE_BUNDLE, ...manifest.binary.split('/'))),
    'binary bytes must pass through package validation without normalization');
});

test('an existing invalid packaged bundle never falls back to the source checkout', t => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'Calibration App invalid package-'));
  t.after(() => fs.rmSync(temporary, {recursive: true, force: true}));
  const resourcesPath = path.join(temporary, 'resources');
  const bundleRoot = path.join(resourcesPath, 'analysis-worker', CALIBRATION_RELATIVE);
  fs.mkdirSync(path.join(bundleRoot, 'manifest.json'), {recursive: true});
  assert.throws(() => resolveBenchmarkBundle({resourcesPath, resourcesDir: path.resolve(__dirname, '..')}), error =>
    error.code === 'CALIBRATION_BENCHMARK_INVALID'
    && error.details?.item === 'manifest' && error.details?.check === 'regular_file');
});

test('receipt tampering and binary CRLF rewriting remain distinct, diagnostic integrity failures', t => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-calibration-integrity-'));
  t.after(() => fs.rmSync(temporary, {recursive: true, force: true}));
  const createBundle = name => {
    const root = path.join(temporary, name);
    fs.cpSync(SOURCE_BUNDLE, root, {recursive: true});
    return root;
  };

  const receiptRoot = createBundle('receipt-tampered');
  const receiptManifest = JSON.parse(fs.readFileSync(path.join(receiptRoot, 'manifest.json'), 'utf8'));
  const receiptPath = path.join(receiptRoot, ...receiptManifest.receipt.split('/'));
  const tampered = fs.readFileSync(receiptPath, 'utf8').replace('"target": "aarch64-linux-ohos"', '"target": "tampered"')
    .replace(/\r?\n/g, '\r\n');
  fs.writeFileSync(receiptPath, tampered);
  assert.throws(() => resolveBenchmarkBundle({bundleRoot: receiptRoot}), error =>
    error.code === 'CALIBRATION_BENCHMARK_INVALID'
    && error.details?.item === 'receipt' && error.details?.check === 'sha256'
    && error.details?.crlf_count > 0 && error.details?.crlf_normalized_sha256 !== receiptManifest.receipt_sha256);

  const crlfIdentityRoot = createBundle('crlf-is-not-canonical-identity');
  const crlfIdentityManifestPath = path.join(crlfIdentityRoot, 'manifest.json');
  const crlfIdentityManifest = JSON.parse(fs.readFileSync(crlfIdentityManifestPath, 'utf8'));
  const crlfIdentityReceipt = path.join(crlfIdentityRoot, ...crlfIdentityManifest.receipt.split('/'));
  const crlfIdentityBytes = Buffer.from(fs.readFileSync(crlfIdentityReceipt, 'utf8').replace(/\r?\n/g, '\r\n'));
  fs.writeFileSync(crlfIdentityReceipt, crlfIdentityBytes);
  crlfIdentityManifest.receipt_sha256 = crypto.createHash('sha256').update(crlfIdentityBytes).digest('hex');
  fs.writeFileSync(crlfIdentityManifestPath, `${JSON.stringify(crlfIdentityManifest, null, 2)}\n`);
  assert.throws(() => resolveBenchmarkBundle({bundleRoot: crlfIdentityRoot}), error =>
    error.code === 'CALIBRATION_BENCHMARK_INVALID'
    && error.details?.item === 'receipt' && error.details?.check === 'sha256'
    && error.details?.actual_sha256 === crlfIdentityManifest.receipt_sha256
    && error.details?.crlf_normalized_sha256 !== crlfIdentityManifest.receipt_sha256);

  for (const [name, mutate, encoding] of [
    ['receipt-bom', value => Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), value]), 'utf-8-bom'],
    ['receipt-invalid-utf8', value => {
      const next = Buffer.from(value);
      const marker = next.indexOf(Buffer.from('parallel_calibration'));
      assert.ok(marker >= 0);
      next[marker] = 0xff;
      return next;
    }, 'invalid-utf-8'],
  ]) {
    const encodingRoot = createBundle(name);
    const encodingManifestPath = path.join(encodingRoot, 'manifest.json');
    const encodingManifest = JSON.parse(fs.readFileSync(encodingManifestPath, 'utf8'));
    const encodingReceipt = path.join(encodingRoot, ...encodingManifest.receipt.split('/'));
    const changed = mutate(fs.readFileSync(encodingReceipt));
    fs.writeFileSync(encodingReceipt, changed);
    encodingManifest.receipt_sha256 = crypto.createHash('sha256').update(changed).digest('hex');
    fs.writeFileSync(encodingManifestPath, `${JSON.stringify(encodingManifest, null, 2)}\n`);
    assert.throws(() => resolveBenchmarkBundle({bundleRoot: encodingRoot}), error =>
      error.code === 'CALIBRATION_BENCHMARK_INVALID'
      && error.details?.item === 'receipt' && error.details?.check === 'encoding'
      && error.details?.encoding === encoding);
  }

  const binaryRoot = createBundle('binary-rewritten');
  const binaryManifestPath = path.join(binaryRoot, 'manifest.json');
  const binaryManifest = JSON.parse(fs.readFileSync(binaryManifestPath, 'utf8'));
  const binaryPath = path.join(binaryRoot, ...binaryManifest.binary.split('/'));
  const originalBinary = fs.readFileSync(binaryPath);
  const parts = [];
  let rewrittenCount = 0;
  for (const value of originalBinary) {
    if (value === 10) { parts.push(Buffer.from([13, 10])); rewrittenCount++; }
    else parts.push(Buffer.from([value]));
  }
  assert.ok(rewrittenCount > 0, 'fixture ELF must contain at least one LF byte');
  const rewrittenBinary = Buffer.concat(parts);
  fs.writeFileSync(binaryPath, rewrittenBinary);
  binaryManifest.binary_bytes = rewrittenBinary.length;
  fs.writeFileSync(binaryManifestPath, `${JSON.stringify(binaryManifest, null, 2)}\n`);
  assert.throws(() => resolveBenchmarkBundle({bundleRoot: binaryRoot}), error =>
    error.code === 'CALIBRATION_BENCHMARK_INVALID'
    && error.details?.item === 'binary' && error.details?.check === 'sha256'
    && error.details?.actual_sha256 !== binaryManifest.binary_sha256);
});

test('benchmark publisher repairs only manifest-backed CRLF and rejects other receipt changes', () => {
  const script = `
import hashlib
from tools.parallel_calibration.build_benchmark import normalize_existing_receipt,parse_receipt
canonical=b'{"schema_version":"fixture"}\\n'
expected=hashlib.sha256(canonical).hexdigest()
crlf=canonical.replace(b'\\n',b'\\r\\n')
assert normalize_existing_receipt(crlf,expected)==canonical
assert parse_receipt(canonical)=={"schema_version":"fixture"}
for value in (canonical+b' ', canonical.replace(b'fixture',b'tampered').replace(b'\\n',b'\\r\\n')):
    try: normalize_existing_receipt(value,expected)
    except RuntimeError: pass
    else: raise AssertionError('tampered receipt was accepted')
try: normalize_existing_receipt(crlf,hashlib.sha256(crlf).hexdigest())
except RuntimeError: pass
else: raise AssertionError('CRLF was accepted as a canonical receipt identity')
for value in (b'\\xef\\xbb\\xbf'+canonical, canonical.decode().encode('utf-16')):
    try: parse_receipt(value)
    except RuntimeError: pass
    else: raise AssertionError('non-canonical receipt encoding was accepted')
`;
  const probe = spawnSync(process.env.PYTHON || 'python3', ['-c', script], {
    cwd: path.resolve(__dirname, '..', '..'), encoding: 'utf8',
  });
  assert.equal(probe.status, 0, probe.stderr || probe.error?.message);
});

test('plan input is bound to the selected hardware profile and validates command fields', () => {
  const result = normalizeInputs({plan: PLAN, hardwareProfile: PROFILE, deviceSerial: 'PHONE-A'});
  assert.deepEqual([...result.groups.keys()], ['cluster_0', 'cluster_1']);
  assert.throws(() => normalizeInputs({plan: PLAN, hardwareProfile: {...PROFILE, id: 'hp_other'}, deviceSerial: 'PHONE-A'}),
    error => error.code === 'CALIBRATION_HARDWARE_MISMATCH');
  assert.throws(() => normalizeInputs({plan: {...PLAN, conditions: [{...CONDITION, family: '$(unsafe)'}]}, hardwareProfile: PROFILE, deviceSerial: 'PHONE-A'}),
    error => error.code === 'CALIBRATION_PLAN_INVALID');
});

test('current HDC topology must match the registered profile before execution', () => {
  const expected = {...PROFILE, evidence: {device_serial_sha256: 'a'.repeat(64)}};
  const actual = {cpu_groups: PROFILE.core_groups, evidence: {device_serial_sha256: 'a'.repeat(64)}};
  assert.equal(validateDeviceHardwareProfile(expected, actual).verified, true);
  assert.throws(() => validateDeviceHardwareProfile(expected, {
    ...actual, cpu_groups: PROFILE.core_groups.map((group, index) => index ? group : {...group, opps_khz: [500000, 900000]}),
  }), error => error.code === 'CALIBRATION_DEVICE_PROFILE_MISMATCH');
  assert.throws(() => validateDeviceHardwareProfile(expected, {...actual, evidence: {device_serial_sha256: 'b'.repeat(64)}}),
    error => error.code === 'CALIBRATION_DEVICE_PROFILE_MISMATCH');
  assert.throws(() => validateDeviceHardwareProfile(
    {...expected, core_groups: expected.core_groups.map((group, index) => ({...group, source_policy_id: `policy${index}`}))},
    {...actual, cpu_groups: actual.cpu_groups.map((group, index) => ({...group, source_policy_id: `policy${index + 1}`}))}),
  error => error.code === 'CALIBRATION_DEVICE_PROFILE_MISMATCH');

  const fullOppCatalogSha256 = crypto.createHash('sha256').update(JSON.stringify(
    PROFILE.core_groups.map(group => ({id: group.id, opps_khz: [...group.opps_khz].sort((a, b) => a - b)}))
      .sort((left, right) => left.id.localeCompare(right.id)),
  )).digest('hex');
  const subset = {
    ...expected,
    source_metadata: {kind: 'local_hdc', calibration_operating_domain: {
      schema_version: 'calibration_operating_domain/v1', kind: 'verified_sustained_frequency_subset',
      full_opp_catalog_sha256: fullOppCatalogSha256,
    }},
    core_groups: expected.core_groups.map((group, index) => index ? group : {...group, opps_khz: group.opps_khz.slice(0, -1)}),
  };
  assert.equal(validateDeviceHardwareProfile(subset, actual).calibrationOperatingDomain,
    'verified_sustained_frequency_subset');
  assert.throws(() => validateDeviceHardwareProfile({
    ...subset, source_metadata: {kind: 'local_hdc', calibration_operating_domain: {
      ...subset.source_metadata.calibration_operating_domain, full_opp_catalog_sha256: 'f'.repeat(64),
    }},
  }, actual), error => error.code === 'CALIBRATION_DEVICE_PROFILE_MISMATCH');
  assert.throws(() => validateDeviceHardwareProfile({
    ...subset,
    core_groups: subset.core_groups.map((group, index) => index ? group : {...group, opps_khz: [group.opps_khz[0], 1234567]}),
  }, actual), error => error.code === 'CALIBRATION_DEVICE_PROFILE_MISMATCH');
});

test('real-device runner deploys the managed binary, measures, persists and never exposes the raw serial', async t => {
  const {manager, calls} = fixture(t);
  const started = await manager.start({plan: PLAN, hardwareProfile: PROFILE, deviceSerial: 'PHONE-PRIVATE', hdcPath: '/opt/hdc'});
  await until(() => ['succeeded', 'partial', 'failed', 'cancelled'].includes(manager.status({runId: started.runId}).status));
  const run = manager.status({runId: started.runId});
  assert.equal(run.status, 'succeeded', JSON.stringify(run.error));
  assert.equal(JSON.stringify(run).includes('PHONE-PRIVATE'), false);
  const measurements = manager.measurements({runId: started.runId});
  const parsed = JSON.parse(measurements.measurementText);
  assert.equal(parsed.samples.length, 1);
  assert.deepEqual(parsed.provenance.cpu_map, {cluster_0: [0, 1], cluster_1: [2, 3]});
  assert.equal(parsed.samples[0].thermal_throttled, false);
  assert.equal(parsed.samples[0].thermal_snapshot_before[0].temperature_raw, 41000);
  assert.ok(calls.some(call => call.args[2] === 'file' && call.args[3] === 'send'));
  assert.ok(calls.some(call => call.args[2] === 'shell' && call.args[3].includes('--benchmark cpu_work')));
  assert.ok(calls.every(call => call.executable === '/opt/hdc'));
});

test('sibling activity contamination receives bounded auditable retry and a recovered repeat completes', async t => {
  const {manager, calls} = fixture(t, {siblingBusyCount: 1});
  const started = await manager.start({plan: PLAN, hardwareProfile: PROFILE, deviceSerial: 'PHONE-A'});
  await until(() => ['succeeded', 'partial', 'failed', 'cancelled'].includes(manager.status({runId: started.runId}).status));
  const run = manager.status({runId: started.runId});
  assert.equal(run.status, 'succeeded', JSON.stringify(run.error));
  assert.deepEqual(run.progress, {completed: 1, total: 1, attempted: 2, retries: 1});
  const parsed = JSON.parse(manager.measurements({runId: started.runId}).measurementText);
  assert.equal(parsed.samples.length, 2, 'the contaminated raw attempt remains auditable');
  assert.equal(parsed.invalid_samples.length, 1);
  assert.equal(parsed.invalid_samples[0].retry_scheduled, true);
  assert.match(parsed.invalid_samples[0].reasons.join(' '), /同胞逻辑线程忙碌占比/);
  assert.deepEqual(parsed.retry_summary, {
    attempt_count: 2, retry_count: 1, recovered_repeat_count: 1, exhausted_repeat_count: 0,
  });
  assert.equal(parsed.unfulfilled_repeat_count, 0);
  assert.equal(parsed.collection_status, 'completed');
  assert.equal(calls.filter(call => call.args[2] === 'shell' && call.args[3].includes('--benchmark cpu_work')).length, 2);
});

test('persistent sibling activity stops after three attempts and cannot produce a complete run', async t => {
  const {manager, calls} = fixture(t, {siblingBusyCount: 10});
  const started = await manager.start({plan: PLAN, hardwareProfile: PROFILE, deviceSerial: 'PHONE-A'});
  await until(() => ['succeeded', 'partial', 'failed', 'cancelled'].includes(manager.status({runId: started.runId}).status));
  const run = manager.status({runId: started.runId});
  assert.equal(run.status, 'partial');
  assert.deepEqual(run.progress, {completed: 1, total: 1, attempted: 3, retries: 2});
  const parsed = JSON.parse(manager.measurements({runId: started.runId}).measurementText);
  assert.equal(parsed.retry_summary.exhausted_repeat_count, 1);
  assert.equal(parsed.unfulfilled_repeat_count, 1);
  assert.equal(parsed.invalid_samples.length, 3);
  assert.equal(parsed.invalid_samples.at(-1).retry_scheduled, false);
  assert.equal(calls.filter(call => call.args[2] === 'shell' && call.args[3].includes('--benchmark cpu_work')).length, 3);
});

test('actual per-CPU frequency outside the three-percent band receives a bounded retry', async t => {
  const {manager, calls} = fixture(t, {frequencyInvalidCount: 1});
  const started = await manager.start({plan: PLAN, hardwareProfile: PROFILE, deviceSerial: 'PHONE-A'});
  await until(() => ['succeeded', 'partial', 'failed', 'cancelled'].includes(manager.status({runId: started.runId}).status));
  const run = manager.status({runId: started.runId});
  assert.equal(run.status, 'succeeded', JSON.stringify(run.error));
  assert.deepEqual(run.progress, {completed: 1, total: 1, attempted: 2, retries: 1});
  const parsed = JSON.parse(manager.measurements({runId: started.runId}).measurementText);
  assert.equal(parsed.retry_policy.trigger, 'sibling_or_target_frequency_evidence');
  assert.equal(parsed.invalid_samples.length, 1);
  assert.equal(parsed.invalid_samples[0].retry_scheduled, true);
  assert.match(parsed.invalid_samples[0].reasons.join(' '), /实际频率窗口未通过/);
  assert.equal(calls.filter(call => call.args[2] === 'shell' && call.args[3].includes('--benchmark cpu_work')).length, 2);
});

test('persistent actual-frequency mismatch exhausts exactly three attempts and blocks completion', async t => {
  const {manager, calls} = fixture(t, {frequencyInvalidCount: 10});
  const started = await manager.start({plan: PLAN, hardwareProfile: PROFILE, deviceSerial: 'PHONE-A'});
  await until(() => ['succeeded', 'partial', 'failed', 'cancelled'].includes(manager.status({runId: started.runId}).status));
  const run = manager.status({runId: started.runId});
  assert.equal(run.status, 'partial');
  const parsed = JSON.parse(manager.measurements({runId: started.runId}).measurementText);
  assert.equal(parsed.invalid_samples.length, 3);
  assert.equal(parsed.retry_summary.retry_count, 2);
  assert.equal(parsed.retry_summary.exhausted_repeat_count, 1);
  assert.equal(parsed.unfulfilled_repeat_count, 1);
  assert.equal(calls.filter(call => call.args[2] === 'shell' && call.args[3].includes('--benchmark cpu_work')).length, 3);
});

test('scaling_cur_freq fallback remains diagnostic and cannot satisfy a production repeat', async t => {
  const {manager} = fixture(t, {frequencyFallbackCount: 10});
  const started = await manager.start({plan: PLAN, hardwareProfile: PROFILE, deviceSerial: 'PHONE-A'});
  await until(() => ['succeeded', 'partial', 'failed', 'cancelled'].includes(manager.status({runId: started.runId}).status));
  assert.equal(manager.status({runId: started.runId}).status, 'partial');
  const parsed = JSON.parse(manager.measurements({runId: started.runId}).measurementText);
  assert.equal(parsed.invalid_samples.length, 3);
  assert.equal(parsed.invalid_samples.every(row => row.frequency_source === 'scaling_cur_freq_fallback'), true);
  assert.match(parsed.invalid_samples[0].reasons.join(' '), /fallback/);
});

test('thermal snapshots use shell built-ins on minimal HarmonyOS shells', async t => {
  const {manager, calls} = fixture(t);
  const rows = await manager.thermalSnapshot('hdc', 'PHONE-A', null);
  assert.deepEqual(rows, [{zone: 'thermal_zone0', type: 'cpu', temperature_raw: 41000}]);
  const command = calls.at(-1).args[3];
  assert.match(command, /IFS= read -r type/);
  assert.doesNotMatch(command, /\|\s*tr\b|\btr\s/,
    'the device shell must not require the optional tr executable');
});

test('measurement provenance fallback omits the device node name from uname', () => {
  const source = fs.readFileSync(path.join(__dirname, '../runtime/calibration.cjs'), 'utf8');
  assert.match(source, /uname -s -r -m/);
  assert.doesNotMatch(source, /uname -a/);
});

test('HDC process diagnostics cannot persist a raw device identity through error.code or stderr', async t => {
  const rawSerial = 'PHONE-PRIVATE-HDC-ERROR';
  const {manager, directory} = fixture(t, {osBuildError: {
    code: rawSerial, message: `probe failed for ${rawSerial}`, stderr: `transport ${rawSerial} rejected`,
  }});
  const started = await manager.start({plan: PLAN, hardwareProfile: PROFILE, deviceSerial: rawSerial});
  await until(() => manager.status({runId: started.runId}).status === 'failed');
  const failed = manager.status({runId: started.runId});
  assert.equal(failed.error.code, 'CALIBRATION_HDC_FAILED');
  assert.match(failed.error.message, /HDC UNKNOWN/);
  assert.equal(JSON.stringify(failed).includes(rawSerial), false);
  assert.equal(persistedText(directory).includes(rawSerial), false);
});

test('frequency sweep is blocked when cpufreq control is unavailable and no benchmark is executed', async t => {
  const {manager, calls} = fixture(t, {writable: false});
  const started = await manager.start({plan: PLAN, hardwareProfile: PROFILE, deviceSerial: 'PHONE-A'});
  await until(() => manager.status({runId: started.runId}).status === 'failed');
  const failed = manager.status({runId: started.runId});
  assert.equal(failed.error.code, 'CALIBRATION_FREQUENCY_CONTROL_UNAVAILABLE');
  assert.match(failed.error.message, /未执行或伪造实测/);
  assert.equal(calls.some(call => call.args[2] === 'shell' && call.args[3].includes('--benchmark cpu_work')), false);
});

test('classic Roofline conditions are executed by the same managed true-device path', async t => {
  const {manager, calls} = fixture(t);
  const condition = {...CONDITION, id: 'condition_abcdef012345', benchmark: 'roofline', family: 'roofline',
    workload_id: 'classic_intensity_ladder_8_v1', core_kind: 'cluster_0', frequency_khz: 1000000,
    workload_class: 'intensity_ladder', working_set_bytes: 1048576,
    roofline_operations_per_element: 8,
    roofline_kernel_version: 'classic-roofline-blocked-v2'};
  const plan = {...PLAN, id: 'cp_abcdef0123456789abcdef01', conditions: [condition]};
  const started = await manager.start({plan, hardwareProfile: PROFILE, deviceSerial: 'PHONE-A'});
  await until(() => ['succeeded', 'partial', 'failed'].includes(manager.status({runId: started.runId}).status));
  const run = manager.status({runId: started.runId});
  assert.equal(run.status, 'succeeded', JSON.stringify(run.error));
  const sample = JSON.parse(manager.measurements({runId: started.runId}).measurementText).samples[0];
  assert.equal(sample.workload_class, 'intensity_ladder');
  assert.equal(sample.operational_intensity_flop_per_byte, 1 / 3);
  const invocation = calls.find(call => call.args[2] === 'shell' && call.args[3].includes('--benchmark roofline'));
  assert.match(invocation.args[3], /--roofline-operations-per-element 8/);
});

test('dispose waits for active calibration cleanup and restores original frequency bounds', async t => {
  const {manager, calls} = fixture(t, {blockBenchmark: true});
  const started = await manager.start({plan: PLAN, hardwareProfile: PROFILE, deviceSerial: 'PHONE-A'});
  await until(() => calls.some(call => call.args[2] === 'shell' && call.args[3].includes('--benchmark cpu_work')));
  await manager.dispose();
  const run = manager.status({runId: started.runId});
  assert.equal(run.status, 'cancelled');
  assert.ok(calls.some(call => call.args[2] === 'shell' && call.args[3].includes("pkill -f '(^|[[:space:]])/data/local/tmp/[h]etero_calibration_")));
  assert.ok(calls.filter(call => call.args[2] === 'shell' && call.args[3].includes('scaling_min_freq')).length >= 4,
    'each frequency domain should be read, locked and restored before dispose resolves');
});

test('start rejects when dispose wins the initial readiness race', async t => {
  const {manager, directory} = fixture(t);
  await manager.initialReady;
  let release;
  manager.initialReady = new Promise(resolve => { release = resolve; });
  const starting = manager.start({plan: PLAN, hardwareProfile: PROFILE, deviceSerial: 'PHONE-A'});
  await Promise.resolve();
  const disposing = manager.dispose();
  release();
  await disposing;
  await assert.rejects(starting, error => error.code === 'CALIBRATION_MANAGER_DISPOSED');
  assert.deepEqual(fs.readdirSync(path.join(directory, 'calibration', 'runs')), []);
});

test('start rejects when dispose wins the recovery reconciliation race', async t => {
  const {manager, directory} = fixture(t);
  await manager.initialReady;
  let release; let entered;
  const gate = new Promise(resolve => { release = resolve; });
  const called = new Promise(resolve => { entered = resolve; });
  manager.queueRecoveryReconcile = () => { entered(); return gate; };
  const starting = manager.start({plan: PLAN, hardwareProfile: PROFILE, deviceSerial: 'PHONE-A'});
  await called;
  await manager.dispose();
  release();
  await assert.rejects(starting, error => error.code === 'CALIBRATION_MANAGER_DISPOSED');
  assert.deepEqual(fs.readdirSync(path.join(directory, 'calibration', 'runs')), []);
});

test('cancelling a blocked pre-measurement thermal snapshot never starts the benchmark', async t => {
  const {manager, calls} = fixture(t, {blockThermal: true});
  const started = await manager.start({plan: PLAN, hardwareProfile: PROFILE, deviceSerial: 'PHONE-A'});
  await until(() => calls.some(call => call.args[2] === 'shell'
    && call.args[3].includes('/sys/class/thermal/thermal_zone')));
  await manager.cancel({runId: started.runId});
  await until(() => {
    const run = manager.status({runId: started.runId});
    return run.status === 'cancelled' && run.frequencyRecovery?.cleanupPending === false;
  });
  assert.equal(calls.some(call => call.args[2] === 'shell' && call.args[3].includes('--benchmark')), false);
  assert.ok(calls.some(call => call.args[2] === 'shell'
    && call.args[3].includes('current=$(cat "$base/scaling_max_freq" 2>/dev/null')),
  'cancellation must still restore the captured frequency domains');
});

test('normal completion remains active through cleanup and emits terminal status only after restoration', async t => {
  const {manager} = fixture(t);
  const events = [];
  manager.on('event', event => { if (event.type === 'calibration') events.push(event.run); });
  const started = await manager.start({plan: PLAN, hardwareProfile: PROFILE, deviceSerial: 'PHONE-A'});
  await until(() => manager.status({runId: started.runId}).status === 'succeeded');
  const runEvents = events.filter(run => run.runId === started.runId);
  assert.ok(runEvents.some(run => run.stage === 'cleaning' && ['preparing', 'running', 'cancelling'].includes(run.status)));
  assert.equal(runEvents.some(run => ['succeeded', 'partial', 'failed', 'cancelled'].includes(run.status)
    && run.frequencyRecovery?.cleanupPending === true), false,
  'a terminal status must not be observable while normal restoration is still pending');
  const terminal = runEvents.find(run => ['succeeded', 'partial', 'failed', 'cancelled'].includes(run.status));
  assert.equal(terminal.status, 'succeeded');
  assert.equal(terminal.frequencyRecovery.cleanupPending, false);
});

test('background recovery retries do not replace initial readiness or block active-run disposal', async t => {
  const {manager, calls} = fixture(t, {blockBenchmark: true});
  const started = await manager.start({plan: PLAN, hardwareProfile: PROFILE, deviceSerial: 'PHONE-A'});
  await until(() => calls.some(call => call.args[2] === 'shell' && call.args[3].includes('--benchmark cpu_work')));
  const initialReady = manager.initialReady;
  manager.lastRecoveryReconcileAt = Date.now();
  manager.sleep = async () => new Promise(() => {});
  void manager.requestRecoveryReconcile();
  assert.equal(manager.ready, initialReady, 'background retries must not move the startup readiness fence');
  await Promise.race([
    manager.dispose(),
    new Promise((_, reject) => setTimeout(() => reject(new Error('dispose waited on background recovery')), 500)),
  ]);
  assert.equal(manager.status({runId: started.runId}).status, 'cancelled');
});

test('dispose aborts background recovery without issuing another HDC command', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-calibration-dispose-recovery-'));
  const first = createFixture({directory, failRestore: true});
  let second = null;
  t.after(async () => {
    await first.manager.dispose();
    await second?.manager.dispose();
    fs.rmSync(directory, {recursive: true, force: true});
  });
  const rawSerial = 'PHONE-BACKGROUND-RECOVERY';
  const started = await first.manager.start({plan: PLAN, hardwareProfile: PROFILE, deviceSerial: rawSerial});
  await until(() => first.manager.status({runId: started.runId}).frequencyRecovery?.cleanupPending === true
    && !first.manager.active.has(started.runId));
  await first.manager.dispose();

  second = createFixture({directory, listedDevices: [rawSerial], blockRestore: true});
  await until(() => second.calls.some(call => call.args[2] === 'shell'
    && call.args[3].includes('current=$(cat "$base/scaling_max_freq" 2>/dev/null')));
  await second.manager.dispose();
  const callCount = second.calls.length;
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(second.calls.length, callCount, 'dispose must prevent recovery from starting another HDC request');
  assert.equal(second.calls.some(call => call.args[2] === 'shell'
    && call.args[3].includes("pkill -f '(^|[[:space:]])/data/local/tmp/[h]etero_calibration_")), false,
  'artifact cleanup must not start after disposal interrupts frequency restoration');
});

test('original cpufreq paths and bounds are persisted with the run and normal cleanup restores them', async t => {
  const policyProfile = {...PROFILE, core_groups: PROFILE.core_groups.map((group, index) => ({
    ...group, source_policy_id: index === 0 ? 'policy0' : 'policy2',
  }))};
  const {manager, calls, directory} = fixture(t);
  const started = await manager.start({plan: PLAN, hardwareProfile: policyProfile, deviceSerial: 'PHONE-A'});
  await until(() => manager.status({runId: started.runId}).frequencyRecovery?.cleanupPending === false);

  const run = manager.status({runId: started.runId});
  assert.equal(run.status, 'succeeded', JSON.stringify(run.error));
  assert.deepEqual(run.frequencyRecovery.domains.map(domain => ({
    coreGroupId: domain.coreGroupId, base: domain.base, minimum: domain.minimum, maximum: domain.maximum,
  })), [
    {coreGroupId: 'cluster_0', base: '/sys/devices/system/cpu/cpufreq/policy0', minimum: 500000, maximum: 2000000},
    {coreGroupId: 'cluster_1', base: '/sys/devices/system/cpu/cpufreq/policy2', minimum: 500000, maximum: 2000000},
  ]);
  assert.deepEqual(run.frequencyRecovery.cleanupErrors, []);
  assert.match(run.frequencyRecovery.restoredAt, /^\d{4}-\d{2}-\d{2}T/);
  assert.equal(fs.existsSync(path.join(directory, 'calibration', 'runs', started.runId, 'recovery.json')), false);
  const stored = JSON.parse(fs.readFileSync(path.join(directory, 'calibration', 'runs', started.runId, 'run.json')));
  assert.deepEqual(stored.frequencyRecovery, run.frequencyRecovery);
  const restores = calls.filter(call => call.args[2] === 'shell'
    && call.args[3].includes('current=$(cat "$base/scaling_max_freq" 2>/dev/null'));
  assert.equal(restores.length, 2);
  assert.ok(restores.every(call => call.args[3].includes("test \"$(cat \"$base/scaling_min_freq\")\" = 500000")));
  assert.ok(restores.every(call => call.args[3].includes("test \"$(cat \"$base/scaling_max_freq\")\" = 2000000")));
});

test('frequency restoration failures downgrade a successful measurement and leave auditable recovery pending', async t => {
  for (const scenario of [
    {name: 'frequency restore', options: {failRestore: true}, phase: 'frequency_restore'},
  ]) await t.test(scenario.name, async t => {
    const {manager, directory} = fixture(t, scenario.options);
    const started = await manager.start({plan: PLAN, hardwareProfile: PROFILE, deviceSerial: 'PHONE-A'});
    await until(() => manager.status({runId: started.runId}).frequencyRecovery?.cleanupPending === true
      && manager.status({runId: started.runId}).status === 'partial');
    const run = manager.status({runId: started.runId});
    assert.equal(run.stage, 'cleanup_pending');
    assert.equal(run.frequencyRecovery.cleanupPending, true);
    assert.ok(run.frequencyRecovery.cleanupErrors.some(row => row.phase === scenario.phase), JSON.stringify(run.frequencyRecovery));
    assert.ok(run.cleanupErrors.some(row => row.phase === scenario.phase), JSON.stringify(run.cleanupErrors));
    assert.equal(fs.existsSync(path.join(directory, 'calibration', 'runs', started.runId, 'recovery.json')), true);
    assert.throws(() => manager.measurements({runId: started.runId}),
      error => error.code === 'CALIBRATION_CLEANUP_PENDING');
  });
});

test('temporary benchmark deletion failure is a warning and never masquerades as frequency recovery failure', async t => {
  const {manager, directory} = fixture(t, {failRemoteCleanup: true, listedDevices: ['PHONE-A']});
  const started = await manager.start({plan: PLAN, hardwareProfile: PROFILE, deviceSerial: 'PHONE-A'});
  await until(() => {
    const current = manager.status({runId: started.runId});
    return current.status === 'succeeded' && current.frequencyRecovery?.cleanupPending === false
      && current.cleanupWarnings?.some(row => row.phase === 'remote_cleanup');
  });
  const run = manager.status({runId: started.runId});
  assert.equal(run.stage, 'completed');
  assert.deepEqual(run.frequencyRecovery.cleanupErrors, []);
  assert.doesNotThrow(() => manager.measurements({runId: started.runId}));
  assert.equal(fs.existsSync(path.join(directory, 'calibration', 'runs', started.runId, 'recovery.json')), true,
    'the private recovery record is retained only to retry artifact cleanup in the background');
  const recovery = JSON.parse(fs.readFileSync(path.join(directory, 'calibration', 'runs', started.runId, 'recovery.json')));
  assert.equal(recovery.frequencyRestorePending, false,
    'the journal must atomically record that only artifact cleanup remains');
});

test('run-specific benchmark paths and exact cleanup patterns isolate an old artifact retry from a new run', async t => {
  const {manager, calls} = fixture(t, {failRemoteCleanupCount: 2, listedDevices: ['PHONE-A']});
  const first = await manager.start({plan: PLAN, hardwareProfile: PROFILE, deviceSerial: 'PHONE-A'});
  await until(() => manager.status({runId: first.runId}).cleanupWarnings?.some(row => row.phase === 'remote_cleanup'));
  const second = await manager.start({plan: PLAN, hardwareProfile: PROFILE, deviceSerial: 'PHONE-A'});
  await until(() => manager.status({runId: second.runId}).frequencyRecovery?.cleanupPending === false);

  const destinations = calls.filter(call => call.args[2] === 'file' && call.args[3] === 'send').map(call => call.args[5]);
  assert.equal(destinations.length, 2);
  assert.notEqual(destinations[0], destinations[1]);
  assert.ok(destinations[0].endsWith(`_${first.runId}`));
  assert.ok(destinations[1].endsWith(`_${second.runId}`));
  const oldCleanup = calls.find(call => call.args[2] === 'shell'
    && call.args[3].includes(`[h]etero_calibration_${path.posix.basename(destinations[0]).split('_')[2]}_${first.runId}`));
  assert.ok(oldCleanup, 'the old run must clean only its own content-addressed path');
  assert.match(oldCleanup.args[3], /\(\[\[:space:\]\]\|\$\)/,
    'the process match must stop at the executable token rather than matching a newer path prefix');
  assert.equal(oldCleanup.args[3].includes(second.runId), false);
});

test('a new manager matches a stranded run by serial hash, restores it and removes the private recovery record', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-calibration-recovery-'));
  const first = createFixture({directory, failRestore: true});
  let second = null;
  t.after(async () => {
    await first.manager.dispose();
    await second?.manager.dispose();
    fs.rmSync(directory, {recursive: true, force: true});
  });
  const rawSerial = 'PHONE-PRIVATE';
  const started = await first.manager.start({plan: PLAN, hardwareProfile: PROFILE, deviceSerial: rawSerial, hdcPath: '/opt/hdc'});
  await until(() => first.manager.status({runId: started.runId}).frequencyRecovery?.cleanupPending === true
    && first.manager.status({runId: started.runId}).status === 'partial');
  await first.manager.dispose();
  const recoveryPath = path.join(directory, 'calibration', 'runs', started.runId, 'recovery.json');
  assert.equal(fs.existsSync(recoveryPath), true);
  const pendingRecovery = JSON.parse(fs.readFileSync(recoveryPath));
  assert.equal(pendingRecovery.deviceSerialSha256, first.manager.status({runId: started.runId}).deviceSerialSha256);
  assert.equal(Object.hasOwn(pendingRecovery, 'deviceSerial'), false);
  assert.equal(persistedText(directory).includes(rawSerial), false);

  second = createFixture({directory, listedDevices: ['UNRELATED-PHONE', rawSerial]});
  await second.manager.ready;
  const recovered = second.manager.status({runId: started.runId});
  assert.equal(recovered.frequencyRecovery.cleanupPending, false);
  assert.deepEqual(recovered.frequencyRecovery.cleanupErrors, []);
  assert.match(recovered.frequencyRecovery.restoredAt, /^\d{4}-\d{2}-\d{2}T/);
  assert.equal(fs.existsSync(recoveryPath), false);
  assert.ok(second.calls.some(call => call.args[0] === 'list' && call.args[1] === 'targets'));
  assert.ok(second.calls.some(call => call.args[0] === '-t' && call.args[1] === rawSerial && call.args[2] === 'shell'));
  assert.equal(second.calls.some(call => call.args[0] === '-t' && call.args[1] === 'UNRELATED-PHONE'), false);
  assert.equal(persistedText(directory).includes(rawSerial), false);
});

test('a valid recovery journal restores frequency bounds even when run.json is missing or corrupt', async t => {
  for (const mode of ['missing', 'corrupt']) await t.test(mode, async t => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), `desktop-calibration-orphan-${mode}-`));
    const first = createFixture({directory, failRestore: true});
    let second = null;
    t.after(async () => {
      await first.manager.dispose();
      await second?.manager.dispose();
      fs.rmSync(directory, {recursive: true, force: true});
    });
    const rawSerial = `PHONE-ORPHAN-${mode.toUpperCase()}`;
    const started = await first.manager.start({plan: PLAN, hardwareProfile: PROFILE, deviceSerial: rawSerial});
    await until(() => first.manager.status({runId: started.runId}).frequencyRecovery?.cleanupPending === true
      && !first.manager.active.has(started.runId));
    if (mode === 'missing') {
      const recoveryPath = path.join(directory, 'calibration', 'runs', started.runId, 'recovery.json');
      const legacyRecovery = JSON.parse(fs.readFileSync(recoveryPath));
      delete legacyRecovery.frequencyRestorePending;
      fs.writeFileSync(recoveryPath, `${JSON.stringify(legacyRecovery)}\n`, {mode: 0o600});
    }
    const runPath = path.join(directory, 'calibration', 'runs', started.runId, 'run.json');
    if (mode === 'missing') fs.unlinkSync(runPath);
    else fs.writeFileSync(runPath, '{not-json', {mode: 0o600});
    const journal = JSON.parse(fs.readFileSync(path.join(directory, 'calibration', 'runs', started.runId, 'recovery.json')));
    assert.equal(first.manager.pendingRecoveryFor(journal.deviceSerialSha256), started.runId,
      'the journal itself must block a new run even when run.json cannot be loaded');

    second = createFixture({directory, listedDevices: [rawSerial]});
    await second.manager.initialReady;
    const recovered = second.manager.status({runId: started.runId});
    assert.equal(recovered.frequencyRecovery.cleanupPending, false);
    assert.equal(recovered.stage, 'recovered');
    assert.equal(recovered.error.code, 'CALIBRATION_RUN_INTERRUPTED_RECOVERED');
    assert.equal(fs.existsSync(path.join(directory, 'calibration', 'runs', started.runId, 'recovery.json')), false);
    assert.ok(second.calls.some(call => call.args[2] === 'shell' && call.args[3].includes('scaling_min_freq')));
    assert.equal(persistedText(directory).includes(rawSerial), false);
  });
});

test('a damaged recovery payload still blocks the matching device by journal identity', async t => {
  const {manager, directory} = fixture(t);
  await manager.initialReady;
  const runId = `cal_${'d'.repeat(32)}`;
  const deviceSerialSha256 = crypto.createHash('sha256').update('PHONE-DAMAGED-JOURNAL').digest('hex');
  const runDirectory = path.join(directory, 'calibration', 'runs', runId);
  fs.mkdirSync(runDirectory, {recursive: true, mode: 0o700});
  fs.writeFileSync(path.join(runDirectory, 'recovery.json'), `${JSON.stringify({schemaVersion: 'desktop_calibration_recovery/v1',
    runId, deviceSerialSha256, frequencyRestorePending: true, frequencyState: 'damaged'})}\n`, {mode: 0o600});
  assert.equal(manager.pendingRecoveryFor(deviceSerialSha256), runId);
});

test('armed frequency recovery is never starved by the artifact-only retry limit', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-calibration-priority-'));
  const first = createFixture({directory, failRestore: true});
  let second = null;
  t.after(async () => {
    await first.manager.dispose();
    await second?.manager.dispose();
    fs.rmSync(directory, {recursive: true, force: true});
  });
  const rawSerial = 'PHONE-PRIORITY';
  const started = await first.manager.start({plan: PLAN, hardwareProfile: PROFILE, deviceSerial: rawSerial});
  await until(() => first.manager.status({runId: started.runId}).frequencyRecovery?.cleanupPending === true
    && !first.manager.active.has(started.runId));
  const template = JSON.parse(fs.readFileSync(path.join(directory, 'calibration', 'runs', started.runId, 'recovery.json')));
  for (let index = 0; index < 65; index += 1) {
    const runId = `cal_${index.toString(16).padStart(32, '0')}`;
    const runDirectory = path.join(directory, 'calibration', 'runs', runId);
    fs.mkdirSync(runDirectory, {recursive: true, mode: 0o700});
    fs.writeFileSync(path.join(runDirectory, 'recovery.json'), `${JSON.stringify({...template, runId,
      remoteBinary: `/data/local/tmp/hetero_calibration_${'a'.repeat(16)}_${runId}`,
      frequencyRestorePending: false, frequencyRestoredAt: new Date().toISOString()})}\n`, {mode: 0o600});
  }

  second = createFixture({directory, listedDevices: [rawSerial]});
  await second.manager.initialReady;
  assert.equal(fs.existsSync(path.join(directory, 'calibration', 'runs', started.runId, 'recovery.json')), false,
    'the safety-critical journal must be restored before the bounded artifact-only set');
  assert.equal(second.manager.status({runId: started.runId}).frequencyRecovery.cleanupPending, false);
});

test('recovery discovery failures never persist raw device identifiers from process diagnostics', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-calibration-private-discovery-'));
  const first = createFixture({directory, failRestore: true});
  let second = null;
  t.after(async () => {
    await first.manager.dispose();
    await second?.manager.dispose();
    fs.rmSync(directory, {recursive: true, force: true});
  });
  const rawSerial = 'PRIVATE-SERIAL-DISCOVERY';
  const started = await first.manager.start({plan: PLAN, hardwareProfile: PROFILE, deviceSerial: rawSerial});
  await until(() => first.manager.status({runId: started.runId}).frequencyRecovery?.cleanupPending === true
    && !first.manager.active.has(started.runId));
  second = createFixture({directory, listTargetsError: {
    code: rawSerial, message: `cannot list ${rawSerial}`, stderr: `transport ${rawSerial} disconnected`,
  }});
  await second.manager.initialReady;
  const recovered = second.manager.status({runId: started.runId});
  assert.equal(recovered.frequencyRecovery.cleanupPending, true);
  assert.match(recovered.cleanupErrors[0].message, /HDC UNKNOWN/);
  assert.equal(JSON.stringify(recovered).includes(rawSerial), false);
  assert.equal(persistedText(directory).includes(rawSerial), false);
});

test('starting another run retries a stranded recovery in the same Desktop session', async t => {
  const {manager, directory} = fixture(t, {failRestoreCount: 2, listedDevices: ['PHONE-A']});
  const first = await manager.start({plan: PLAN, hardwareProfile: PROFILE, deviceSerial: 'PHONE-A'});
  await until(() => manager.status({runId: first.runId}).frequencyRecovery?.cleanupPending === true
    && manager.status({runId: first.runId}).status === 'partial');
  const second = await manager.start({plan: PLAN, hardwareProfile: PROFILE, deviceSerial: 'PHONE-A'});
  await until(() => manager.status({runId: second.runId}).frequencyRecovery?.cleanupPending === false);
  assert.equal(manager.status({runId: first.runId}).frequencyRecovery.cleanupPending, false);
  assert.equal(fs.existsSync(path.join(directory, 'calibration', 'runs', first.runId, 'recovery.json')), false);
  assert.equal(manager.status({runId: second.runId}).status, 'succeeded');
});

test('status polling retries a stranded cleanup while the same device remains connected', async t => {
  const {manager, directory, calls} = fixture(t, {failRestoreCount: 2, listedDevices: ['PHONE-A']});
  const started = await manager.start({plan: PLAN, hardwareProfile: PROFILE, deviceSerial: 'PHONE-A'});
  await until(() => manager.status({runId: started.runId}).frequencyRecovery?.cleanupPending === true
    && manager.status({runId: started.runId}).stage === 'cleanup_pending'
    && !manager.active.has(started.runId));

  const pending = manager.status({runId: started.runId, reconcile: true});
  for (let index = 0; index < 8; index++) manager.status({runId: started.runId, reconcile: true});
  assert.equal(pending.frequencyRecovery.cleanupPending, true);
  await until(() => manager.status({runId: started.runId}).frequencyRecovery?.cleanupPending === false);

  const recovered = manager.status({runId: started.runId});
  assert.equal(recovered.stage, 'completed');
  assert.equal(recovered.status, 'partial', 'measurement result remains auditable after cleanup recovery');
  assert.equal(fs.existsSync(path.join(directory, 'calibration', 'runs', started.runId, 'recovery.json')), false);
  assert.equal(calls.filter(call => call.args[0] === 'list' && call.args[1] === 'targets').length, 1,
    'rapid 1.2s UI polling must coalesce into one recovery discovery instead of queueing retries');
});

test('recovery discovery accepts HDC builds that append state columns without weakening serial identity', () => {
  assert.deepEqual(parseHdcTargetSerials([
    'Targets:',
    'PHONE-A\tConnected\tUSB',
    'PHONE-B',
    '[I] daemon diagnostic',
    'PHONE-A Connected',
    'no device',
  ].join('\r\n')), ['PHONE-A', 'PHONE-B']);
});

test('cpufreq discovery prefers source policy, falls back to cpuN and guards both with the related CPU list', async t => {
  const mixedProfile = {...PROFILE, core_groups: [
    {...PROFILE.core_groups[0], source_policy_id: 'policy0'},
    {...PROFILE.core_groups[1]},
  ]};
  const {manager, calls} = fixture(t);
  const started = await manager.start({plan: PLAN, hardwareProfile: mixedProfile, deviceSerial: 'PHONE-A'});
  await until(() => manager.status({runId: started.runId}).frequencyRecovery?.cleanupPending === false);
  const probes = calls.filter(call => call.args[2] === 'shell' && call.args[3].includes("printf 'base=%s"));
  assert.equal(probes.length, 2);
  assert.match(probes[0].args[3], /\/sys\/devices\/system\/cpu\/cpufreq\/policy0/);
  assert.ok(probes[0].args[3].indexOf('/cpufreq/policy0') < probes[0].args[3].indexOf('/cpu0/cpufreq'),
    'the extracted policy path must be evaluated before the cpuN compatibility fallback');
  assert.match(probes[0].args[3], /related_cpus/);
  assert.match(probes[1].args[3], /\/sys\/devices\/system\/cpu\/cpu2\/cpufreq/);
  assert.match(probes[1].args[3], /related_cpus/);
  const run = manager.status({runId: started.runId});
  assert.deepEqual(run.frequencyRecovery.domains.map(domain => domain.base), [
    '/sys/devices/system/cpu/cpufreq/policy0',
    '/sys/devices/system/cpu/cpu2/cpufreq',
  ]);
});
