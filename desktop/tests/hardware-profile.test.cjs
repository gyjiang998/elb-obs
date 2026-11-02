'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const {
  HARDWARE_PROBE_SCRIPT,
  extractHardwareProfile,
  parseCpuList,
} = require('../runtime/hardware-profile.cjs');

function probeFixture({domainCount = 3, overlap = false} = {}) {
  const domains = [
    {id: 'policy0', cpus: '0-3', min: 400000, max: 1800000, available: '400000 900000 1800000'},
    {id: 'policy4', cpus: overlap ? '3-6' : '4-6', min: 600000, max: 2400000, time: [600000, 1600000, 2400000]},
    {id: 'policy7', cpus: '7', min: 800000, max: 3000000, available: '800000 2000000 3000000'},
    {id: 'policy8', cpus: '8', min: 900000, max: 3200000, available: '900000 3200000'},
  ].slice(0, domainCount);
  const cpuCount = Math.max(...domains.flatMap(domain => domain.cpus.match(/\d+/g).map(Number))) + 1;
  const lines = [
    'HWP\tBEGIN\tv1',
    'HWP\tMETA\tmodel\tMate Fixture',
    'HWP\tMETA\tmanufacturer\tHuawei',
    'HWP\tMETA\tbrand\tHUAWEI',
    'HWP\tMETA\tchipset\tKirin Fixture',
    'HWP\tMETA\tos_build\tOpenHarmony fixture build',
    'HWP\tSYSTEM\tcpu_present\t0-' + (cpuCount - 1),
    'HWP\tSYSTEM\tcpu_online\t0-' + Math.max(0, cpuCount - 2),
    'HWP\tPROBE\tcpufreq_root\tpresent',
    `HWP\tPROBE\tpolicy_candidate_count\t${domains.length}`,
  ];
  for (let cpu = 0; cpu < cpuCount; cpu++) {
    lines.push(`HWP\tCPU:${cpu}\tthread_siblings_list\t${cpu >= 4 && cpu <= 5 ? '4-5' : cpu}`);
    lines.push(`HWP\tCPU:${cpu}\tcore_id\t${cpu}`);
    lines.push(`HWP\tCPU:${cpu}\tphysical_package_id\t0`);
  }
  for (const domain of domains) {
    lines.push(`HWP\tPOLICY:${domain.id}\trelated_cpus\t${domain.cpus}`);
    lines.push(`HWP\tPOLICY:${domain.id}\tcpuinfo_min_freq\t${domain.min}`);
    lines.push(`HWP\tPOLICY:${domain.id}\tcpuinfo_max_freq\t${domain.max}`);
    lines.push(`HWP\tPOLICY:${domain.id}\tscaling_available_frequencies\t${domain.available || ''}`);
    for (const frequency of domain.time || []) lines.push(`HWP\tTIME:${domain.id}\trow\t${frequency} 123`);
  }
  lines.push('HWP\tEND\tv1');
  return `${lines.join('\n')}\n`;
}

test('local HDC extraction uses fixed argv, classifies three domains and never returns the raw serial', async () => {
  const calls = [];
  const serial = 'SECRET-DEVICE-SERIAL';
  const result = await extractHardwareProfile({
    deviceSerial: serial,
    hdcPath: 'C:\\Program Files\\hdc.exe',
    now: () => new Date('2026-10-28T01:02:03.000Z'),
    execFileImpl(executable, args, options, callback) {
      calls.push({executable, args, options});
      callback(null, probeFixture(), 'diagnostic without identifier');
    },
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].executable, 'C:\\Program Files\\hdc.exe');
  assert.deepEqual(calls[0].args.slice(0, 4), ['-t', serial, 'shell', HARDWARE_PROBE_SCRIPT]);
  assert.equal(calls[0].options.shell, undefined);
  assert.equal(calls[0].options.windowsHide, true);
  assert.equal(calls[0].options.timeout, 30000);
  assert.equal(calls[0].options.maxBuffer, 512 * 1024);
  assert.equal(result.profile.schema_version, 'parallel_hardware_profile/v1');
  assert.deepEqual(result.profile.cpu_groups.map(group => [group.kind, group.logical_cpu_ids, group.max_khz]), [
    ['small', [0, 1, 2, 3], 1800000],
    ['middle', [4, 5, 6], 2400000],
    ['big', [7], 3000000],
  ]);
  assert.deepEqual(result.profile.cpu_groups[1].opps_khz, [600000, 1600000, 2400000], 'time_in_state is the OPP fallback');
  assert.deepEqual(result.profile.frequency_domains.map(domain => domain.id), ['small', 'middle', 'big']);
  assert.equal(result.profile.evidence.device_serial_sha256, crypto.createHash('sha256').update(serial).digest('hex'));
  assert.equal(result.profile.evidence.extracted_at, '2026-10-28T01:02:03.000Z');
  assert.equal(result.profile.readiness.cost_model, 'not_calibrated');
  assert.equal(result.profile.cpu_groups[1].physical_core_count, 2, 'reciprocal thread siblings define physical cores even when Kirin core_id is unique per logical CPU');
  assert.equal(result.profile.cpu_groups[1].logical_cpu_count, 3);
  assert.equal(result.profile.cpu_groups[1].physical_core_count_source, 'thread_siblings_list');
  assert.deepEqual(result.profile.cpu_groups[1].physical_core_topology.map(core => core.logical_cpu_ids), [[4, 5], [6]]);
  assert.match(result.profile.warnings.join('；'), /CPU 7 处于离线状态/);
  assert.equal(result.diagnostics.domain_count, 3);
  assert.equal(JSON.stringify(result).includes(serial), false);
});

test('Kirin SMT topology keeps fourteen scheduling CPUs while exposing nine physical cores', async () => {
  const domains = [
    {id: 'policy0', cpus: '0-3', min: 400000, max: 1600000},
    {id: 'policy4', cpus: '4-11', min: 600000, max: 2400000},
    {id: 'policy12', cpus: '12-13', min: 900000, max: 3200000},
  ];
  const siblingList = cpu => cpu < 4 ? String(cpu) : cpu < 12 ? `${cpu - (cpu % 2)},${cpu - (cpu % 2) + 1}` : '12-13';
  const lines = [
    'HWP\tBEGIN\tv1', 'HWP\tMETA\tmodel\tKirin SMT Fixture',
    'HWP\tSYSTEM\tcpu_present\t0-13', 'HWP\tSYSTEM\tcpu_online\t0-13',
    'HWP\tPROBE\tcpufreq_root\tpresent', 'HWP\tPROBE\tpolicy_candidate_count\t3',
  ];
  for (let cpu = 0; cpu < 14; cpu++) {
    lines.push(`HWP\tCPU:${cpu}\tthread_siblings_list\t${siblingList(cpu)}`);
    lines.push(`HWP\tCPU:${cpu}\tcore_id\t${cpu}`); // Current Kirin kernels expose per-thread IDs here.
    lines.push(`HWP\tCPU:${cpu}\tphysical_package_id\t0`);
  }
  for (const domain of domains) {
    lines.push(`HWP\tPOLICY:${domain.id}\trelated_cpus\t${domain.cpus}`);
    lines.push(`HWP\tPOLICY:${domain.id}\tcpuinfo_min_freq\t${domain.min}`);
    lines.push(`HWP\tPOLICY:${domain.id}\tcpuinfo_max_freq\t${domain.max}`);
  }
  lines.push('HWP\tEND\tv1');
  const result = await extractHardwareProfile({deviceSerial: 'fixture', execFileImpl: (_file, _args, _options, callback) => callback(null, `${lines.join('\n')}\n`, '')});
  assert.equal(result.diagnostics.logical_cpu_count, 14);
  assert.equal(result.diagnostics.physical_core_count, 9);
  assert.deepEqual(result.profile.cpu_groups.map(group => [group.physical_core_count, group.logical_cpu_count]), [[4, 4], [4, 8], [1, 2]]);
  assert.deepEqual(result.profile.cpu_groups[1].physical_core_topology.map(core => core.logical_cpu_ids), [[4, 5], [6, 7], [8, 9], [10, 11]]);
  assert.equal(result.profile.cpu_groups[1].physical_core_representative_cpu_ids.join(','), '4,6,8,10');
});

test('Kirin 9020-style topology exposes eight physical cores over twelve logical CPUs', async () => {
  const domains = [
    {id: 'policy0', cpus: '0-3', min: 418000, max: 1600000},
    {id: 'policy4', cpus: '4-9', min: 418000, max: 1997000},
    {id: 'policy10', cpus: '10-11', min: 1210000, max: 2363000},
  ];
  const siblings = cpu => cpu < 4 ? String(cpu) : cpu < 10 ? `${cpu - (cpu % 2)},${cpu - (cpu % 2) + 1}` : '10-11';
  const lines = [
    'HWP\tBEGIN\tv1', 'HWP\tMETA\tmodel\tKirin 9020 topology fixture',
    'HWP\tSYSTEM\tcpu_present\t0-11', 'HWP\tSYSTEM\tcpu_online\t0-11',
    'HWP\tPROBE\tcpufreq_root\tpresent', 'HWP\tPROBE\tpolicy_candidate_count\t3',
  ];
  for (let cpu = 0; cpu < 12; cpu++) {
    lines.push(`HWP\tCPU:${cpu}\tcore_cpus_list\t${siblings(cpu)}`);
    lines.push(`HWP\tCPU:${cpu}\tthread_siblings_list\t${siblings(cpu)}`);
    lines.push(`HWP\tCPU:${cpu}\tcore_id\t${cpu}`);
    lines.push(`HWP\tCPU:${cpu}\tphysical_package_id\t0`);
  }
  for (const domain of domains) {
    lines.push(`HWP\tPOLICY:${domain.id}\trelated_cpus\t${domain.cpus}`);
    lines.push(`HWP\tPOLICY:${domain.id}\tcpuinfo_min_freq\t${domain.min}`);
    lines.push(`HWP\tPOLICY:${domain.id}\tcpuinfo_max_freq\t${domain.max}`);
  }
  lines.push('HWP\tEND\tv1');
  const result = await extractHardwareProfile({
    deviceSerial: 'fixture',
    execFileImpl: (_file, _args, _options, callback) => callback(null, `${lines.join('\n')}\n`, ''),
  });
  assert.equal(result.diagnostics.physical_core_count, 8);
  assert.equal(result.diagnostics.logical_cpu_count, 12);
  assert.deepEqual(result.profile.cpu_groups.map(group => [group.physical_core_count, group.logical_cpu_count]), [[4, 4], [3, 6], [1, 2]]);
  assert.deepEqual(result.profile.cpu_groups[1].physical_core_topology.map(core => core.logical_cpu_ids), [[4, 5], [6, 7], [8, 9]]);
  assert.deepEqual(result.profile.cpu_groups[2].physical_core_topology.map(core => core.logical_cpu_ids), [[10, 11]]);
  assert.ok(result.profile.cpu_groups.every(group => group.physical_core_count_source === 'core_cpus_list+thread_siblings_list'));
});

test('partial or asymmetric sibling evidence blocks extraction instead of falling back to Kirin core_id', async () => {
  const run = stdout => extractHardwareProfile({deviceSerial: 'fixture', execFileImpl: (_file, _args, _options, callback) => callback(null, stdout, '')});
  const asymmetric = probeFixture().replace(
    'HWP\tCPU:5\tthread_siblings_list\t4-5',
    'HWP\tCPU:5\tthread_siblings_list\t5',
  );
  await assert.rejects(run(asymmetric), error => {
    assert.equal(error.code, 'hardware_profile_topology_conflict');
    assert.equal(error.details.field, 'thread_siblings_list');
    assert.equal(error.details.reason, 'non_reciprocal_map');
    return true;
  });

  const partialCoreMap = probeFixture().replace(
    'HWP\tCPU:4\tthread_siblings_list\t4-5',
    'HWP\tCPU:4\tcore_cpus_list\t4-5\nHWP\tCPU:4\tthread_siblings_list\t4-5',
  );
  await assert.rejects(run(partialCoreMap), error => {
    assert.equal(error.code, 'hardware_profile_topology_conflict');
    assert.equal(error.details.field, 'core_cpus_list');
    assert.equal(error.details.reason, 'incomplete_map');
    return true;
  });

  const disagreeingSources = probeFixture().split('\n').flatMap(line => {
    const match = line.match(/^HWP\tCPU:(\d+)\tthread_siblings_list\t/);
    return match ? [`HWP\tCPU:${match[1]}\tcore_cpus_list\t${match[1]}`, line] : [line];
  }).join('\n');
  await assert.rejects(run(disagreeingSources), error => {
    assert.equal(error.code, 'hardware_profile_topology_conflict');
    assert.equal(error.details.field, 'core_cpus_list/thread_siblings_list');
    assert.equal(error.details.reason, 'source_disagreement');
    return true;
  });
});

test('core_id-only topology remains explicitly estimated and does not claim verified sibling evidence', async () => {
  const stdout = probeFixture().split(/\r?\n/)
    .filter(line => !line.includes('\tthread_siblings_list\t'))
    .join('\n');
  const result = await extractHardwareProfile({
    deviceSerial: 'fixture',
    execFileImpl: (_file, _args, _options, callback) => callback(null, stdout, ''),
  });
  assert.equal(result.profile.cpu_groups[1].physical_core_count, 3);
  assert.equal(result.profile.cpu_groups[1].physical_core_count_source, 'core_id_estimate');
  assert.ok(result.profile.cpu_groups[1].physical_core_topology.every(core => core.topology_source === 'core_id_estimate'));
  assert.match(result.profile.warnings.join('；'), /仅按 package\/core_id 估计/);
});

test('CPU list parser handles ranges and refuses malformed or unbounded device output', () => {
  assert.deepEqual(parseCpuList('0-3, 6 8-9'), [0, 1, 2, 3, 6, 8, 9]);
  assert.throws(() => parseCpuList('0-99999'), error => error.code === 'hardware_profile_cpu_list_invalid');
  assert.throws(() => parseCpuList('0-2;rm'), error => error.code === 'hardware_profile_cpu_list_invalid');
});

test('one to many frequency domains are preserved and overlapping policies still fail', async () => {
  const run = stdout => extractHardwareProfile({deviceSerial: 'fixture', execFileImpl: (_file, _args, _options, callback) => callback(null, stdout, '')});
  const one = await run(probeFixture({domainCount: 1}));
  assert.deepEqual(one.profile.cpu_groups.map(group => [group.id, group.proxy_kind]), [['cluster_0', 'big']]);
  const two = await run(probeFixture({domainCount: 2}));
  assert.deepEqual(two.profile.cpu_groups.map(group => [group.id, group.proxy_kind]), [['cluster_0', 'small'], ['cluster_1', 'big']]);
  const four = await run(probeFixture({domainCount: 4}));
  assert.deepEqual(four.profile.cpu_groups.map(group => group.id), ['cluster_0', 'cluster_1', 'cluster_2', 'cluster_3']);
  assert.deepEqual(four.profile.cpu_groups.map(group => group.proxy_kind), ['small', 'middle', 'middle', 'big']);
  assert.equal(four.diagnostics.logical_cpu_count, 9);
  await assert.rejects(run(probeFixture({overlap: true})), error => {
    assert.equal(error.code, 'hardware_profile_cpu_domain_overlap');
    assert.match(error.message, /逻辑 CPU 3/);
    return true;
  });
});

test('zero usable domains reports concrete probe evidence instead of a three-domain requirement', async () => {
  const stdout = 'HWP\tBEGIN\tv1\nHWP\tSYSTEM\tcpu_present\t0-7\nHWP\tPROBE\tcpufreq_root\tpresent\nHWP\tPROBE\tpolicy_candidate_count\t0\nHWP\tEND\tv1\n';
  await assert.rejects(extractHardwareProfile({deviceSerial: 'fixture', execFileImpl: (_file, _args, _options, callback) => callback(null, stdout, '')}), error => {
    assert.equal(error.code, 'hardware_profile_no_frequency_domains');
    assert.match(error.message, /cpufreq 根目录已存在/);
    assert.equal(error.details.policy_candidate_count, 0);
    assert.doesNotMatch(error.message, /恰好 3/);
    return true;
  });
});

test('failed param output is not stored as chipset metadata', async () => {
  const stdout = probeFixture()
    .replace('HWP\tMETA\tchipset\tKirin Fixture', 'HWP\tMETA\tchipset\tGet parameter "const.product.chipset" fail! errNum is:106!\nHWP\tMETA\tboard\tFixture Board');
  const result = await extractHardwareProfile({deviceSerial: 'fixture', execFileImpl: (_file, _args, _options, callback) => callback(null, stdout, '')});
  assert.equal(result.profile.chipset, 'Fixture Board');
});

test('probe uses shell builtins, omits the hostname from uname and includes per-CPU fallback', () => {
  const commands = HARDWARE_PROBE_SCRIPT.split(/\r?\n/).filter(line => !line.trimStart().startsWith('#')).join('\n');
  assert.doesNotMatch(HARDWARE_PROBE_SCRIPT, /\btr\b/);
  assert.doesNotMatch(commands, /uname\s+(?:-[^\n]*a\b|--all\b)/);
  assert.doesNotMatch(commands, /uname\s+(?:-[^\n]*n\b|--nodename\b)/);
  assert.match(commands, /emit_value META uname uname -s -r -m/);
  assert.match(HARDWARE_PROBE_SCRIPT, /\$cpu\/cpufreq/);
  assert.match(HARDWARE_PROBE_SCRIPT, /policy_candidate_count/);
});

test('HDC failures and timeouts redact the selected serial', async () => {
  const serial = 'PRIVATE-SERIAL-42';
  await assert.rejects(extractHardwareProfile({deviceSerial: serial, execFileImpl: (_file, _args, _options, callback) => {
    const error = new Error(`Command failed for ${serial}`); error.code = 'EIO'; callback(error, '', `device ${serial} failed`);
  }}), error => {
    assert.equal(error.code, 'hardware_profile_probe_failed');
    assert.equal(error.message.includes(serial), false);
    return true;
  });
  await assert.rejects(extractHardwareProfile({deviceSerial: serial, execFileImpl: (_file, _args, _options, callback) => {
    const error = new Error(`timeout ${serial}`); error.code = 'ETIMEDOUT'; error.killed = true; callback(error, '', '');
  }}), error => error.code === 'hardware_profile_probe_timeout' && !error.message.includes(serial));
});

test('truncated HDC output is rejected instead of creating a partial profile', async () => {
  await assert.rejects(extractHardwareProfile({deviceSerial: 'fixture', execFileImpl: (_file, _args, _options, callback) => {
    callback(null, probeFixture().replace('HWP\tEND\tv1\n', ''), '');
  }}), error => error.code === 'hardware_profile_probe_incomplete');
});
