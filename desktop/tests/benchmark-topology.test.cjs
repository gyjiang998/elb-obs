const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const test = require('node:test');
const assert = require('node:assert/strict');

const root = path.resolve(__dirname, '../..');
const benchmarkSource = path.join(root, 'tools/parallel_calibration/benchmark.c');

function findCompiler() {
  const candidates = [process.env.CC, 'cc', 'clang', 'gcc'].filter(Boolean);
  return candidates.find(candidate => spawnSync(candidate, ['--version'], { encoding: 'utf8' }).status === 0);
}

function writeTopology(rootPath, cpu, field, value) {
  const directory = path.join(rootPath, `cpu${cpu}`, 'topology');
  fs.mkdirSync(directory, { recursive: true });
  fs.writeFileSync(path.join(directory, field), `${value}\n`);
}

function writeMap(rootPath, field, entries) {
  for (const [cpu, value] of Object.entries(entries)) writeTopology(rootPath, cpu, field, value);
}

function writeLegacyIds(rootPath, entries) {
  for (const [cpu, { packageId = 0, coreId }] of Object.entries(entries)) {
    writeTopology(rootPath, cpu, 'physical_package_id', packageId);
    writeTopology(rootPath, cpu, 'core_id', coreId);
  }
}

test('benchmark executable classifies complete topology and rejects conflicting evidence', t => {
  const compiler = findCompiler();
  if (!compiler) {
    t.skip('a C compiler is required for the benchmark topology behavior test');
    return;
  }

  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'benchmark-topology-'));
  try {
    const executable = path.join(temporary, process.platform === 'win32' ? 'topology-test.exe' : 'topology-test');
    const compilation = spawnSync(compiler, [
      '-O0',
      '-std=c11',
      '-DPARALLEL_CALIBRATION_TOPOLOGY_TEST',
      benchmarkSource,
      '-o', executable,
      '-lm',
      '-pthread',
    ], { encoding: 'utf8' });
    assert.equal(compilation.status, 0, `topology test build failed:\n${compilation.stdout}\n${compilation.stderr}`);

    let caseIndex = 0;
    const classify = (prepare, left = 0, right = 1) => {
      const fixture = path.join(temporary, `case-${caseIndex++}`);
      fs.mkdirSync(fixture, { recursive: true });
      prepare(fixture);
      const result = spawnSync(executable, [fixture, String(left), String(right)], { encoding: 'utf8' });
      assert.equal(result.status, 0, `topology test failed:\n${result.stdout}\n${result.stderr}`);
      return result.stdout.trim();
    };

    assert.equal(classify(fixture => {
      writeMap(fixture, 'core_cpus_list', { 0: '0-1', 1: '0-1' });
      writeMap(fixture, 'thread_siblings_list', { 0: '0-1', 1: '0-1' });
      writeLegacyIds(fixture, { 0: { coreId: 0 }, 1: { coreId: 1 } });
    }), 'SAME', 'reciprocal sibling lists override Kirin-style per-thread core_id values');

    assert.equal(classify(fixture => {
      writeMap(fixture, 'core_cpus_list', { 0: '0', 1: '1' });
      writeMap(fixture, 'thread_siblings_list', { 0: '0', 1: '1' });
    }), 'DIFFERENT', 'two complete disjoint groups are independent physical cores');

    assert.equal(classify(fixture => {
      writeMap(fixture, 'thread_siblings_list', { 0: '0-1', 1: '0-1' });
      writeLegacyIds(fixture, { 0: { coreId: 0 }, 1: { coreId: 1 } });
    }), 'SAME', 'thread_siblings_list is authoritative when core_cpus_list is absent');

    assert.equal(classify(fixture => {
      writeLegacyIds(fixture, { 0: { coreId: 7 }, 1: { coreId: 7 } });
    }), 'SAME', 'core_id fallback is allowed when both list mechanisms are completely absent');

    assert.equal(classify(fixture => {
      writeLegacyIds(fixture, { 0: { coreId: 7 }, 1: { coreId: 8 } });
    }), 'UNKNOWN', 'different legacy IDs cannot prove independence on Kirin SMT systems');

    assert.equal(classify(fixture => {
      writeMap(fixture, 'core_cpus_list', { 0: '0-1', 1: '1' });
      writeMap(fixture, 'thread_siblings_list', { 0: '0', 1: '1' });
      writeLegacyIds(fixture, { 0: { coreId: 0 }, 1: { coreId: 1 } });
    }), 'CONFLICT', 'asymmetric maps must not fall through to another field or core_id');

    assert.equal(classify(fixture => {
      writeMap(fixture, 'core_cpus_list', { 0: '0' });
      writeMap(fixture, 'thread_siblings_list', { 0: '0', 1: '1' });
      writeLegacyIds(fixture, { 0: { coreId: 0 }, 1: { coreId: 1 } });
    }), 'CONFLICT', 'a map present for only one queried CPU is incomplete');

    assert.equal(classify(fixture => {
      writeMap(fixture, 'core_cpus_list', { 0: '0--1', 1: '0-1' });
      writeMap(fixture, 'thread_siblings_list', { 0: '0-1', 1: '0-1' });
    }), 'CONFLICT', 'malformed cpulist syntax is conflicting evidence');

    assert.equal(classify(fixture => {
      writeMap(fixture, 'core_cpus_list', { 0: '1', 1: '1' });
      writeMap(fixture, 'thread_siblings_list', { 0: '0-1', 1: '0-1' });
    }), 'CONFLICT', 'a sibling group that omits its own CPU is invalid');

    assert.equal(classify(fixture => {
      writeMap(fixture, 'core_cpus_list', { 0: '0,2', 1: '1' });
      writeMap(fixture, 'thread_siblings_list', { 0: '0', 1: '1' });
    }), 'CONFLICT', 'the complete referenced sibling group must be present and reciprocal');

    assert.equal(classify(fixture => {
      writeMap(fixture, 'core_cpus_list', { 0: '0-1', 1: '0-1' });
      writeMap(fixture, 'thread_siblings_list', { 0: '0', 1: '1' });
    }), 'CONFLICT', 'two valid list mechanisms may not disagree');

    assert.equal(classify(fixture => {
      writeMap(fixture, 'core_cpus_list', { 0: '0', 1: '0-1', 2: '2' });
      writeMap(fixture, 'thread_siblings_list', { 0: '0', 1: '0-1', 2: '2' });
    }, 0, 2), 'CONFLICT', 'a reverse-only edge from an unqueried CPU invalidates the physical-core graph');

    assert.equal(classify(fixture => {
      writeMap(fixture, 'core_cpus_list', { 1: '0-1' });
      writeMap(fixture, 'thread_siblings_list', { 0: '0', 1: '1', 2: '2' });
    }, 0, 2), 'CONFLICT', 'a reverse-only edge cannot hide behind an absent queried-CPU list and a valid secondary source');

    assert.equal(classify(fixture => {
      writeMap(fixture, 'core_cpus_list', { 0: '0', 1: '1' });
      writeMap(fixture, 'thread_siblings_list', { 0: '0 trailing', 1: '1' });
      writeLegacyIds(fixture, { 0: { coreId: 0 }, 1: { coreId: 1 } });
    }), 'CONFLICT', 'a malformed secondary list is not ignored after a valid primary list');

    assert.equal(classify(() => {}), 'UNKNOWN', 'missing list and legacy topology evidence stays unknown');
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
});

test('benchmark measures target physical-core sibling activity and fails closed without evidence', t => {
  const compiler = findCompiler();
  if (!compiler) {
    t.skip('a C compiler is required for the sibling quietness behavior test');
    return;
  }

  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'benchmark-sibling-quietness-'));
  try {
    const executable = path.join(temporary, process.platform === 'win32' ? 'sibling-test.exe' : 'sibling-test');
    const compilation = spawnSync(compiler, [
      '-O0',
      '-std=c11',
      '-DPARALLEL_CALIBRATION_SIBLING_QUIETNESS_TEST',
      benchmarkSource,
      '-o', executable,
      '-lm',
      '-pthread',
    ], { encoding: 'utf8' });
    assert.equal(compilation.status, 0, `sibling test build failed:\n${compilation.stdout}\n${compilation.stderr}`);

    let caseIndex = 0;
    const measure = ({prepareTopology, before, after, target = 4}) => {
      const fixture = path.join(temporary, `quietness-${caseIndex++}`);
      const topology = path.join(fixture, 'topology');
      const beforePath = path.join(fixture, 'before.stat');
      const afterPath = path.join(fixture, 'after.stat');
      fs.mkdirSync(topology, {recursive: true});
      prepareTopology(topology);
      fs.writeFileSync(beforePath, before);
      fs.writeFileSync(afterPath, after);
      const result = spawnSync(executable, [topology, beforePath, afterPath, String(target)], {encoding: 'utf8'});
      assert.equal(result.status, 0, `sibling test failed:\n${result.stdout}\n${result.stderr}`);
      return JSON.parse(result.stdout);
    };
    const smtTopology = fixture => {
      writeMap(fixture, 'core_cpus_list', {4: '4-5', 5: '4-5'});
      writeMap(fixture, 'thread_siblings_list', {4: '4-5', 5: '4-5'});
    };
    const before = 'cpu  0 0 0 0 0 0 0 0\ncpu5 100 0 50 850 0 0 0 0\n';

    const quiet = measure({prepareTopology: smtTopology, before,
      after: 'cpu5 100 0 50 860 0 0 0 0\n'});
    assert.deepEqual(quiet.target_sibling_cpus, [5]);
    assert.equal(quiet.target_sibling_activity_verified, true);
    assert.equal(quiet.target_sibling_busy_fraction, 0);
    assert.equal(quiet.target_sibling_quietness_verified, true);
    assert.deepEqual(quiet.target_sibling_activity[0], {
      cpu: 5, busy_ticks: 0, total_ticks: 10, busy_fraction: 0, verified: true,
    });

    const contaminated = measure({prepareTopology: smtTopology, before,
      after: 'cpu5 102 0 50 858 0 0 0 0\n'});
    assert.equal(contaminated.target_sibling_activity_verified, true);
    assert.equal(contaminated.target_sibling_busy_fraction, 0.2);
    assert.equal(contaminated.target_sibling_quietness_verified, false);

    const threshold = measure({prepareTopology: smtTopology, before,
      after: 'cpu5 105 0 50 945 0 0 0 0\n'});
    assert.equal(threshold.target_sibling_busy_fraction, 0.05);
    assert.equal(threshold.target_sibling_quietness_verified, true, 'the declared 5% threshold is inclusive');

    const independent = measure({
      prepareTopology(fixture) {
        writeMap(fixture, 'core_cpus_list', {4: '4'});
        writeMap(fixture, 'thread_siblings_list', {4: '4'});
      },
      before: '', after: '',
    });
    assert.deepEqual(independent.target_sibling_cpus, []);
    assert.deepEqual(independent.target_sibling_activity, []);
    assert.equal(independent.target_sibling_busy_fraction, 0);
    assert.equal(independent.target_sibling_activity_verified, true);
    assert.equal(independent.target_sibling_quietness_verified, true);

    for (const invalid of [
      measure({prepareTopology: smtTopology, before, after: 'cpu4 0 0 0 10 0 0 0 0\n'}),
      measure({prepareTopology: smtTopology, before, after: before}),
      measure({
        prepareTopology(fixture) {
          writeMap(fixture, 'core_cpus_list', {4: '4-5', 5: '4-5'});
          writeMap(fixture, 'thread_siblings_list', {4: '4', 5: '5'});
        },
        before, after: 'cpu5 100 0 50 860 0 0 0 0\n',
      }),
    ]) {
      assert.equal(invalid.target_sibling_activity_verified, false);
      assert.equal(invalid.target_sibling_busy_fraction, null);
      assert.equal(invalid.target_sibling_quietness_verified, false);
    }
  } finally {
    fs.rmSync(temporary, {recursive: true, force: true});
  }
});

test('benchmark freezes per-CPU frequency sources and requires bounded stable warmup', t => {
  const compiler = findCompiler();
  if (!compiler) {
    t.skip('a C compiler is required for the frequency-source behavior test');
    return;
  }

  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'benchmark-frequency-source-'));
  try {
    const executable = path.join(temporary, process.platform === 'win32' ? 'frequency-test.exe' : 'frequency-test');
    const compilation = spawnSync(compiler, [
      '-O0',
      '-std=c11',
      '-DPARALLEL_CALIBRATION_FREQUENCY_TEST',
      benchmarkSource,
      '-o', executable,
      '-lm',
      '-pthread',
    ], { encoding: 'utf8' });
    assert.equal(compilation.status, 0, `frequency test build failed:\n${compilation.stdout}\n${compilation.stderr}`);

    let caseIndex = 0;
    const sourceCase = ({cpu, policy, cpuinfo, scaling, afterCpuinfo, afterScaling}) => {
      const fixture = path.join(temporary, `source-${caseIndex++}`);
      const cpuDirectory = path.join(fixture, `cpu${cpu}`, 'cpufreq');
      const policyDirectory = path.join(fixture, 'cpufreq', policy);
      fs.mkdirSync(cpuDirectory, {recursive: true});
      fs.mkdirSync(policyDirectory, {recursive: true});
      if (cpuinfo !== undefined)
        fs.writeFileSync(path.join(cpuDirectory, 'cpuinfo_cur_freq'), `${cpuinfo}\n`);
      if (scaling !== undefined)
        fs.writeFileSync(path.join(policyDirectory, 'scaling_cur_freq'), `${scaling}\n`);
      const result = spawnSync(executable, [
        'source', fixture, String(cpu), policy,
        afterCpuinfo === undefined ? 'missing' : String(afterCpuinfo),
        afterScaling === undefined ? 'missing' : String(afterScaling),
      ], {encoding: 'utf8'});
      assert.equal(result.status, 0, `frequency source test failed:\n${result.stdout}\n${result.stderr}`);
      return JSON.parse(result.stdout);
    };

    const targetCpuinfo = sourceCase({
      cpu: 4, policy: 'policy7', cpuinfo: 1000, scaling: 800,
      afterCpuinfo: undefined, afterScaling: 900,
    });
    assert.equal(targetCpuinfo.frequency_source, 'cpuinfo_cur_freq');
    assert.equal(targetCpuinfo.frequency_source_cpu, 4);
    assert.equal(targetCpuinfo.frequency_source_path,
      path.join(temporary, 'source-0', 'cpu4', 'cpufreq', 'cpuinfo_cur_freq'));
    assert.equal(targetCpuinfo.initial_value, 1000);
    assert.equal(targetCpuinfo.post_mutation_value, -1,
      'a frozen cpuinfo source must not switch to a readable scaling fallback');
    assert.equal(targetCpuinfo.stable_window_frequency_verified, true);

    const targetFallback = sourceCase({
      cpu: 4, policy: 'policy7', cpuinfo: undefined, scaling: 800,
      afterCpuinfo: 1100, afterScaling: 900,
    });
    assert.equal(targetFallback.frequency_source, 'scaling_cur_freq_fallback');
    assert.equal(targetFallback.frequency_source_cpu, 4);
    assert.equal(targetFallback.frequency_source_path,
      path.join(temporary, 'source-1', 'cpufreq', 'policy7', 'scaling_cur_freq'));
    assert.equal(targetFallback.initial_value, 800);
    assert.equal(targetFallback.post_mutation_value, 900,
      'a frozen fallback must not switch when cpuinfo appears later');
    assert.equal(targetFallback.stable_window_frequency_verified, false,
      'fallback evidence remains diagnostic and cannot verify a production sample');

    const helperCpuinfo = sourceCase({
      cpu: 6, policy: 'policy9', cpuinfo: 1500, scaling: 1200,
      afterCpuinfo: 1510, afterScaling: 1250,
    });
    assert.equal(helperCpuinfo.frequency_source, 'cpuinfo_cur_freq');
    assert.equal(helperCpuinfo.frequency_source_cpu, 6);
    assert.equal(helperCpuinfo.frequency_source_path,
      path.join(temporary, 'source-2', 'cpu6', 'cpufreq', 'cpuinfo_cur_freq'),
      'the peer helper source is tied to its own logical CPU, not a policy representative');
    assert.equal(helperCpuinfo.post_mutation_value, 1510);

    const warmup = samples => {
      const result = spawnSync(executable, ['warmup', '1000', ...samples.map(String)], {encoding: 'utf8'});
      assert.equal(result.status, 0, `warmup test failed:\n${result.stdout}\n${result.stderr}`);
      return JSON.parse(result.stdout);
    };

    const stabilized = warmup([800, 970, 1000, 1030, 2000]);
    assert.deepEqual(stabilized, {
      warmup_frequency_sample_count: 4,
      warmup_frequency_in_range_consecutive: 3,
      warmup_frequency_required_consecutive: 3,
      warmup_frequency_max_samples: 12,
      warmup_frequency_min_khz: 800,
      warmup_frequency_max_khz: 1030,
      warmup_frequency_last_khz: 1030,
      warmup_frequency_verified: true,
      frequency_verified: true,
    }, 'warmup stops after three consecutive samples within the inclusive +/-3% band');

    const resetByReadFailure = warmup([1000, 1000, 'missing', 1000, 1000, 1000]);
    assert.equal(resetByReadFailure.warmup_frequency_sample_count, 6);
    assert.equal(resetByReadFailure.warmup_frequency_in_range_consecutive, 3);
    assert.equal(resetByReadFailure.warmup_frequency_verified, true,
      'a missing read resets the consecutive stability streak');
    assert.equal(resetByReadFailure.frequency_verified, true);

    const unstable = warmup(Array.from({length: 16}, (_, index) => index % 2 ? 1040 : 960));
    assert.equal(unstable.warmup_frequency_sample_count, 12,
      'an unreachable OPP is bounded by the fixed maximum sample count');
    assert.equal(unstable.warmup_frequency_in_range_consecutive, 0);
    assert.equal(unstable.warmup_frequency_verified, false);
    assert.equal(unstable.frequency_verified, false,
      'an unstable warmup must force the production frequency verdict false');
    assert.equal(unstable.warmup_frequency_min_khz, 960);
    assert.equal(unstable.warmup_frequency_max_khz, 1040);
  } finally {
    fs.rmSync(temporary, {recursive: true, force: true});
  }
});
