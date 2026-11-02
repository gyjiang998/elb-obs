'use strict';

const crypto = require('node:crypto');
const {execFile} = require('node:child_process');

const HDC_TIMEOUT_MS = 30000;
const HDC_MAX_BUFFER = 512 * 1024;
const MAX_CPU_ID = 4095;
const MAX_FREQUENCY_KHZ = 100 * 1000 * 1000;
const GROUP_LABELS = {small: '小核组', middle: '中核组', big: '大核组'};

// The script is constant and read-only. User-controlled values are passed only
// as execFile argv, never interpolated into this shell program.
const HARDWARE_PROBE_SCRIPT = String.raw`
emit_value() {
  printf 'HWP\t%s\t%s\t' "$1" "$2"
  shift 2
  emit_output=$("$@" 2>/dev/null)
  printf '%s' "$emit_output"
  printf '\n'
}
emit_file() {
  printf 'HWP\t%s\t%s\t' "$1" "$2"
  emit_output=''
  if [ -r "$3" ]; then IFS= read -r emit_output < "$3" || :; fi
  printf '%s' "$emit_output"
  printf '\n'
}
emit_param() {
  printf 'HWP\tMETA\t%s\t' "$1"
  if command -v param >/dev/null 2>&1; then
    emit_output=$(param get "$2" 2>/dev/null)
    printf '%s' "$emit_output"
  fi
  printf '\n'
}
emit_policy() {
  policy_path=$1
  policy_id=$2
  policy_source=$3
  fallback_cpu=$4
  policy_candidate_count=$((policy_candidate_count + 1))
  printf 'HWP\tPROBE\tpolicy_candidate\t%s:%s\n' "$policy_source" "$policy_id"
  printf 'HWP\tPOLICY:%s\tsource\t%s\n' "$policy_id" "$policy_source"
  printf 'HWP\tPOLICY:%s\tfallback_cpu\t%s\n' "$policy_id" "$fallback_cpu"
  emit_file "POLICY:$policy_id" related_cpus "$policy_path/related_cpus"
  emit_file "POLICY:$policy_id" affected_cpus "$policy_path/affected_cpus"
  emit_file "POLICY:$policy_id" cpus "$policy_path/cpus"
  emit_file "POLICY:$policy_id" cpu_list "$policy_path/cpu_list"
  emit_file "POLICY:$policy_id" cpuinfo_min_freq "$policy_path/cpuinfo_min_freq"
  emit_file "POLICY:$policy_id" cpuinfo_max_freq "$policy_path/cpuinfo_max_freq"
  emit_file "POLICY:$policy_id" scaling_min_freq "$policy_path/scaling_min_freq"
  emit_file "POLICY:$policy_id" scaling_max_freq "$policy_path/scaling_max_freq"
  emit_file "POLICY:$policy_id" scaling_available_frequencies "$policy_path/scaling_available_frequencies"
  if [ -r "$policy_path/stats/time_in_state" ]; then
    while IFS= read -r tis_line; do
      printf 'HWP\tTIME:%s\trow\t%s\n' "$policy_id" "$tis_line"
    done < "$policy_path/stats/time_in_state"
  fi
}

printf 'HWP\tBEGIN\tv1\n'
emit_param model const.product.model
emit_param product_name const.product.name
emit_param manufacturer const.product.manufacturer
emit_param brand const.product.brand
emit_param chipset const.product.chipset
emit_param board const.product.board
emit_param hardware_version const.product.hardwareversion
emit_param os_build const.ohos.fullname
emit_param software_version const.product.software.version
# Do not use uname -a: it includes the device node name and can leak a
# user-assigned hostname into the persisted hardware profile.  The system,
# release and machine fields are sufficient as a last-resort OS identifier.
emit_value META uname uname -s -r -m
if [ -r /proc/cpuinfo ]; then
  printf 'HWP\tMETA\tcpu_hardware\t'
  while IFS= read -r cpuinfo_line; do
    case "$cpuinfo_line" in
      Hardware:*|"model name":*|Processor:*)
        cpuinfo_value=${'$'}{cpuinfo_line#*:}
        printf '%s' "$cpuinfo_value"
        break
        ;;
    esac
  done < /proc/cpuinfo
  printf '\n'
fi
emit_file SYSTEM cpu_present /sys/devices/system/cpu/present
emit_file SYSTEM cpu_online /sys/devices/system/cpu/online

for cpu in /sys/devices/system/cpu/cpu[0-9]*; do
  [ -d "$cpu" ] || continue
  cpu_id=${'$'}{cpu##*cpu}
  emit_file "CPU:$cpu_id" core_cpus_list "$cpu/topology/core_cpus_list"
  emit_file "CPU:$cpu_id" thread_siblings_list "$cpu/topology/thread_siblings_list"
  emit_file "CPU:$cpu_id" core_id "$cpu/topology/core_id"
  emit_file "CPU:$cpu_id" physical_package_id "$cpu/topology/physical_package_id"
done

policy_candidate_count=0
if [ -d /sys/devices/system/cpu/cpufreq ]; then
  printf 'HWP\tPROBE\tcpufreq_root\tpresent\n'
else
  printf 'HWP\tPROBE\tcpufreq_root\tmissing\n'
fi
for policy in /sys/devices/system/cpu/cpufreq/policy*; do
  [ -d "$policy" ] || continue
  policy_id=${'$'}{policy##*/}
  emit_policy "$policy" "$policy_id" standard ''
done

# Some Harmony/Linux kernels expose cpufreq only below cpuN.  Probe that
# fallback only when the standard policy directory yielded no candidates;
# reading every per-CPU symlink is needlessly expensive on real devices.
if [ "$policy_candidate_count" -eq 0 ]; then
  for cpu in /sys/devices/system/cpu/cpu[0-9]*; do
    [ -d "$cpu/cpufreq" ] || continue
    cpu_id=${'$'}{cpu##*cpu}
    emit_policy "$cpu/cpufreq" "cpu$cpu_id" per_cpu "$cpu_id"
  done
fi
printf 'HWP\tPROBE\tpolicy_candidate_count\t%s\n' "$policy_candidate_count"
printf 'HWP\tEND\tv1\n'
`;

class HardwareProfileExtractionError extends Error {
  constructor(message, code, details = {}) {
    super(message);
    this.name = 'HardwareProfileExtractionError';
    this.code = code;
    this.details = details;
  }
}

function clean(value, maxLength = 512) {
  return String(value || '').replace(/[\u0000-\u001f\u007f]+/g, ' ').replace(/\s+/g, ' ').trim().slice(0, maxLength);
}

function integer(value, {minimum = 0, maximum = Number.MAX_SAFE_INTEGER} = {}) {
  const match = clean(value, 64).match(/^\d+$/);
  if (!match) return null;
  const parsed = Number(match[0]);
  return Number.isSafeInteger(parsed) && parsed >= minimum && parsed <= maximum ? parsed : null;
}

function parseCpuList(value) {
  const output = new Set();
  const source = clean(value, 8192);
  if (!source) return [];
  for (const token of source.split(/[\s,]+/).filter(Boolean)) {
    const range = token.match(/^(\d+)(?:-(\d+))?$/);
    if (!range) throw new HardwareProfileExtractionError(`设备返回了无法识别的 CPU 列表：${source}`, 'hardware_profile_cpu_list_invalid');
    const start = Number(range[1]);
    const end = Number(range[2] ?? range[1]);
    if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 0 || end < start || end > MAX_CPU_ID || end - start > MAX_CPU_ID)
      throw new HardwareProfileExtractionError(`设备返回了越界的 CPU 列表：${source}`, 'hardware_profile_cpu_list_invalid');
    for (let id = start; id <= end; id++) output.add(id);
  }
  return [...output].sort((a, b) => a - b);
}

function parseFrequencies(value) {
  const result = new Set();
  for (const token of clean(value, 65536).split(/\s+/).filter(Boolean)) {
    const parsed = integer(token, {minimum: 1, maximum: MAX_FREQUENCY_KHZ});
    if (parsed !== null) result.add(parsed);
  }
  return [...result].sort((a, b) => a - b);
}

function parseProbe(stdout) {
  const meta = {};
  const system = {};
  const probe = {policy_candidates: []};
  const cpus = new Map();
  const policies = new Map();
  let began = false;
  let ended = false;
  for (const rawLine of String(stdout || '').split(/\r?\n/)) {
    if (!rawLine.startsWith('HWP\t')) continue;
    const columns = rawLine.split('\t');
    const section = columns[1] || '';
    const key = columns[2] || '';
    const value = columns.slice(3).join('\t').trim();
    if (section === 'BEGIN' && key === 'v1') { began = true; continue; }
    if (section === 'END' && key === 'v1') { ended = true; continue; }
    if (section === 'META') meta[key] = clean(value);
    else if (section === 'SYSTEM') system[key] = clean(value, 8192);
    else if (section === 'PROBE') {
      if (key === 'policy_candidate') probe.policy_candidates.push(clean(value, 160));
      else probe[key] = clean(value, 8192);
    }
    else if (section.startsWith('CPU:')) {
      const id = integer(section.slice(4), {maximum: MAX_CPU_ID});
      if (id !== null) { if (!cpus.has(id)) cpus.set(id, {}); cpus.get(id)[key] = clean(value, 8192); }
    } else if (section.startsWith('POLICY:')) {
      const id = clean(section.slice(7), 80);
      if (id) { if (!policies.has(id)) policies.set(id, {id, time_in_state: []}); policies.get(id)[key] = clean(value, 65536); }
    } else if (section.startsWith('TIME:')) {
      const id = clean(section.slice(5), 80);
      if (id) { if (!policies.has(id)) policies.set(id, {id, time_in_state: []}); policies.get(id).time_in_state.push(clean(value, 512)); }
    }
  }
  if (!began || !ended) throw new HardwareProfileExtractionError('HDC 返回的硬件探测结果不完整，请确认设备连接稳定后重试。', 'hardware_profile_probe_incomplete');
  return {meta, system, probe, cpus, policies: [...policies.values()]};
}

function topologyIdentity(cpuId, cpu) {
  const coreId = integer(cpu?.core_id, {maximum: MAX_CPU_ID});
  const packageId = integer(cpu?.physical_package_id, {maximum: MAX_CPU_ID});
  if (coreId !== null) return {identity: `package:${packageId ?? 0}:core:${coreId}`, source: 'core_id'};
  const siblings = parseCpuList(cpu?.thread_siblings_list || '');
  if (siblings.length) return {identity: `siblings:${siblings.join(',')}`, source: 'thread_siblings_list'};
  return {identity: `logical:${cpuId}`, source: 'logical_cpu_count_proxy'};
}

function reciprocalTopologyGroups(logicalCpuIds, cpus, field) {
  const domain = new Set(logicalCpuIds);
  const lists = new Map();
  let present = 0;
  for (const cpuId of logicalCpuIds) {
    const raw = String(cpus.get(cpuId)?.[field] || '').trim();
    if (!raw) continue;
    present += 1;
    const members = parseCpuList(raw);
    if (!members.length || !members.includes(cpuId) || members.some(member => !domain.has(member)))
      return {status: 'conflict', reason: 'invalid_membership'};
    lists.set(cpuId, members);
  }
  if (!present) return {status: 'unavailable'};
  if (present !== logicalCpuIds.length) return {status: 'conflict', reason: 'incomplete_map'};
  for (const [cpuId, members] of lists) {
    const canonical = members.join(',');
    if (members.some(member => lists.get(member)?.join(',') !== canonical))
      return {status: 'conflict', reason: 'non_reciprocal_map'};
    if (!members.includes(cpuId)) return {status: 'conflict', reason: 'missing_self'};
  }
  return {
    status: 'valid',
    groups: [...new Map([...lists.values()].map(members => [members.join(','), members])).values()]
      .sort((left, right) => left[0] - right[0]),
  };
}

function physicalTopology(logicalCpuIds, cpus) {
  // Linux exposes the logical CPUs belonging to one physical core through
  // core_cpus_list.  Prefer a complete reciprocal map: some heterogeneous
  // ARM kernels assign a distinct core_id to every schedulable CPU even when
  // two CPUs are hardware threads of the same physical core.
  const coreCpuGroups = reciprocalTopologyGroups(logicalCpuIds, cpus, 'core_cpus_list');
  if (coreCpuGroups.status === 'conflict') throw new HardwareProfileExtractionError(
    '设备返回的 core_cpus_list 物理核映射不完整或不互证，已停止提取以避免将 SMT 线程误认为独立物理核。',
    'hardware_profile_topology_conflict', {field: 'core_cpus_list', reason: coreCpuGroups.reason, logical_cpu_ids: logicalCpuIds});

  // thread_siblings_list is the Linux scheduling-topology contract for SMT.
  // On current Kirin kernels core_id identifies the schedulable thread and is
  // different for siblings, so using package+core_id first overcounts cores.
  const siblingGroups = reciprocalTopologyGroups(logicalCpuIds, cpus, 'thread_siblings_list');
  if (siblingGroups.status === 'conflict') throw new HardwareProfileExtractionError(
    '设备返回的 thread_siblings_list 兄弟映射不完整或不互证，已停止提取以避免使用不可靠的 core_id 覆盖冲突证据。',
    'hardware_profile_topology_conflict', {field: 'thread_siblings_list', reason: siblingGroups.reason, logical_cpu_ids: logicalCpuIds});

  // When both kernel contracts are available they must describe the same
  // physical partition. Accepting only the preferred source here would let a
  // conflicting profile pass extraction and fail much later in the benchmark.
  if (coreCpuGroups.status === 'valid' && siblingGroups.status === 'valid') {
    if (JSON.stringify(coreCpuGroups.groups) !== JSON.stringify(siblingGroups.groups))
      throw new HardwareProfileExtractionError(
        '设备返回的 core_cpus_list 与 thread_siblings_list 对物理核边界的描述不一致，已停止提取。',
        'hardware_profile_topology_conflict', {
          field: 'core_cpus_list/thread_siblings_list', reason: 'source_disagreement', logical_cpu_ids: logicalCpuIds,
        });
    return {groups: coreCpuGroups.groups, source: 'core_cpus_list+thread_siblings_list'};
  }
  if (coreCpuGroups.status === 'valid') return {groups: coreCpuGroups.groups, source: 'core_cpus_list'};
  if (siblingGroups.status === 'valid') return {groups: siblingGroups.groups, source: 'thread_siblings_list'};

  const coreIdentities = logicalCpuIds.map(id => topologyIdentity(id, cpus.get(id)));
  const coreGroups = new Map();
  coreIdentities.forEach((item, index) => {
    const rows = coreGroups.get(item.identity) || [];
    rows.push(logicalCpuIds[index]);
    coreGroups.set(item.identity, rows);
  });
  return {
    groups: [...coreGroups.values()].sort((left, right) => left[0] - right[0]),
    source: coreIdentities.every(item => item.source === 'core_id') ? 'core_id_estimate' : 'logical_cpu_count_proxy',
  };
}

function normalizeDomains(parsed) {
  const domains = [];
  const seenCpuSets = new Set();
  for (const policy of parsed.policies) {
    const logicalCpuIds = parseCpuList(policy.related_cpus || policy.affected_cpus || policy.cpus || policy.cpu_list || policy.fallback_cpu || '');
    if (!logicalCpuIds.length) continue;
    if (logicalCpuIds.length > 512) throw new HardwareProfileExtractionError(`频率域 ${policy.id} 的 CPU 数量超过 512，当前硬件画像格式无法保存。`, 'hardware_profile_cpu_count_unsupported', {policy_id: policy.id});
    const cpuSet = logicalCpuIds.join(',');
    if (seenCpuSets.has(cpuSet)) continue;
    seenCpuSets.add(cpuSet);
    const available = parseFrequencies(policy.scaling_available_frequencies);
    const timeInState = parseFrequencies(policy.time_in_state.map(row => row.split(/\s+/)[0]).join(' '));
    const minimum = integer(policy.cpuinfo_min_freq, {minimum: 1, maximum: MAX_FREQUENCY_KHZ})
      ?? integer(policy.scaling_min_freq, {minimum: 1, maximum: MAX_FREQUENCY_KHZ});
    const maximum = integer(policy.cpuinfo_max_freq, {minimum: 1, maximum: MAX_FREQUENCY_KHZ})
      ?? integer(policy.scaling_max_freq, {minimum: 1, maximum: MAX_FREQUENCY_KHZ});
    const opps = new Set(available.length ? available : timeInState);
    if (minimum !== null) opps.add(minimum);
    if (maximum !== null) opps.add(maximum);
    const oppsKhz = [...opps].sort((a, b) => a - b);
    if (!oppsKhz.length) throw new HardwareProfileExtractionError(`频率域 ${policy.id} 未提供可用 OPP，请检查设备 cpufreq sysfs 权限。`, 'hardware_profile_opps_unavailable', {policy_id: policy.id});
    if (oppsKhz.length > 256) throw new HardwareProfileExtractionError(`频率域 ${policy.id} 返回超过 256 个 OPP，当前硬件画像格式无法保存。`, 'hardware_profile_opps_unsupported', {policy_id: policy.id});
    const minKhz = minimum ?? oppsKhz[0];
    const maxKhz = maximum ?? oppsKhz.at(-1);
    if (minKhz > maxKhz) throw new HardwareProfileExtractionError(`频率域 ${policy.id} 的最小/最大频率无效。`, 'hardware_profile_frequency_invalid', {policy_id: policy.id});
    const topology = physicalTopology(logicalCpuIds, parsed.cpus);
    const physicalCoreCount = topology.groups.length;
    if (physicalCoreCount > 256) throw new HardwareProfileExtractionError(`频率域 ${policy.id} 的物理核数量超过 256，当前硬件画像格式无法保存。`, 'hardware_profile_core_count_unsupported', {policy_id: policy.id});
    const physicalCoreRepresentativeCpuIds = topology.groups.map(group => group[0]);
    const physicalCoreTopology = topology.groups.map((members, index) => ({
      id: `core_${index}`,
      logical_cpu_ids: members,
      representative_logical_cpu_id: members[0],
      topology_source: topology.source,
    }));
    domains.push({sourcePolicyId: policy.id, logicalCpuIds, physicalCoreRepresentativeCpuIds, oppsKhz, minKhz, maxKhz,
      physicalCoreCount, physicalCoreTopology, physicalCoreCountSource: topology.source});
  }
  if (!domains.length) {
    const candidates = parsed.policies.map(policy => ({
      id: policy.id,
      source: policy.source || 'unknown',
      cpu_list_available: Boolean(policy.related_cpus || policy.affected_cpus || policy.cpus || policy.cpu_list || policy.fallback_cpu),
      frequency_available: Boolean(policy.scaling_available_frequencies || policy.cpuinfo_min_freq || policy.cpuinfo_max_freq
        || policy.scaling_min_freq || policy.scaling_max_freq || policy.time_in_state.length),
    }));
    const root = parsed.probe?.cpufreq_root || 'unknown';
    const candidateCount = integer(parsed.probe?.policy_candidate_count, {maximum: 4096}) ?? candidates.length;
    throw new HardwareProfileExtractionError(
      `未从设备读取到可用的 CPU 频率域：cpufreq 根目录${root === 'present' ? '已存在' : root === 'missing' ? '不存在' : '状态未知'}，`
        + `发现 ${candidateCount} 个策略候选、解析 ${candidates.length} 个策略记录，但没有任何记录同时提供可用 CPU 列表和频率信息。`
        + '请检查该系统用户对 /sys/devices/system/cpu/cpufreq 或 cpuN/cpufreq 的只读权限。',
      'hardware_profile_no_frequency_domains', {
        detected_domain_count: 0,
        cpufreq_root: root,
        policy_candidate_count: candidateCount,
        policy_candidates: candidates,
      },
    );
  }
  domains.sort((left, right) => left.maxKhz - right.maxKhz || left.minKhz - right.minKhz || left.logicalCpuIds[0] - right.logicalCpuIds[0]);
  const occupied = new Set();
  for (const domain of domains) for (const cpu of domain.logicalCpuIds) {
    if (occupied.has(cpu)) throw new HardwareProfileExtractionError(`逻辑 CPU ${cpu} 同时属于多个频率域。`, 'hardware_profile_cpu_domain_overlap');
    occupied.add(cpu);
  }
  return domains;
}

function proxyKind(index, count) {
  if (count === 1) return 'big';
  if (count === 2) return index === 0 ? 'small' : 'big';
  const ratio = index / (count - 1);
  return ratio < 1 / 3 ? 'small' : ratio > 2 / 3 ? 'big' : 'middle';
}

function firstValue(...values) {
  return values.map(value => clean(value)).find(value => value && !/^Get parameter .+ fail!|errNum\s+is:/i.test(value)) || '';
}

function buildProfile(parsed, deviceSerial, now = () => new Date()) {
  const domains = normalizeDomains(parsed);
  const cpuPresent = parseCpuList(parsed.system.cpu_present || '');
  const cpuOnline = parseCpuList(parsed.system.cpu_online || '');
  const warnings = [];
  const covered = new Set(domains.flatMap(item => item.logicalCpuIds));
  const missing = cpuPresent.filter(id => !covered.has(id));
  if (missing.length) warnings.push(`cpufreq 频率域未覆盖 CPU ${missing.join(', ')}，保存前请核对核组。`);
  const offline = cpuPresent.filter(id => !cpuOnline.includes(id));
  if (offline.length) warnings.push(`提取时 CPU ${offline.join(', ')} 处于离线状态；核组使用 related_cpus 保留这些 CPU。`);
  if (domains.some(item => item.physicalCoreCountSource === 'logical_cpu_count_proxy'))
    warnings.push('部分 CPU 未提供拓扑 sibling/core_id，物理核数暂按逻辑 CPU 数估计。');
  if (domains.some(item => item.physicalCoreCountSource === 'core_id_estimate'))
    warnings.push('设备未提供 sibling 列表，物理核数仅按 package/core_id 估计；该来源不能证明逻辑 CPU 位于独立物理核。');
  const model = firstValue(parsed.meta.model, parsed.meta.product_name, '未命名设备').slice(0, 120);
  const brand = firstValue(parsed.meta.brand, parsed.meta.manufacturer).slice(0, 60);
  const chipset = firstValue(parsed.meta.chipset, parsed.meta.board, parsed.meta.hardware_version, parsed.meta.cpu_hardware, '未知芯片').slice(0, 200);
  const hasUnambiguousThreeClassOrder = domains.length === 3 && new Set(domains.map(domain => domain.maxKhz)).size === 3;
  const cpuGroups = domains.map((domain, index) => {
    const proxy = proxyKind(index, domains.length);
    const id = hasUnambiguousThreeClassOrder ? proxy : `cluster_${index}`;
    const name = hasUnambiguousThreeClassOrder ? GROUP_LABELS[proxy] : `CPU 核组 ${index + 1}`;
    return {id, name, kind: id, proxy_kind: proxy,
      physical_core_count: domain.physicalCoreCount,
      physical_core_representative_cpu_ids: domain.physicalCoreRepresentativeCpuIds,
      physical_core_topology: domain.physicalCoreTopology,
      physical_core_count_source: domain.physicalCoreCountSource,
      logical_cpu_count: domain.logicalCpuIds.length,
      logical_cpu_ids: domain.logicalCpuIds,
      frequency_domain_id: id,
      opps_khz: domain.oppsKhz,
      min_khz: domain.minKhz,
      max_khz: domain.maxKhz,
      source_policy_id: domain.sourcePolicyId};
  });
  return {
    schema_version: 'parallel_hardware_profile/v1',
    name: clean(`${brand ? `${brand} ` : ''}${model} · 本机提取`, 200),
    vendor: firstValue(parsed.meta.manufacturer, brand, '未知厂商').slice(0, 200),
    model,
    chipset,
    os_build: firstValue(parsed.meta.os_build, parsed.meta.software_version, parsed.meta.uname, '未知版本').slice(0, 200),
    source: 'local_hdc',
    profile_source: 'local_hdc',
    cpu_groups: cpuGroups,
    frequency_domains: cpuGroups.map(group => ({id: group.frequency_domain_id, core_group_ids: [group.id], shared_opp: true,
      opps_khz: group.opps_khz, min_khz: group.min_khz, max_khz: group.max_khz, source_policy_id: group.source_policy_id})),
    evidence: {
      device_serial_sha256: crypto.createHash('sha256').update(deviceSerial, 'utf8').digest('hex'),
      extracted_at: now().toISOString(),
      cpu_present: cpuPresent,
      cpu_online: cpuOnline,
      source: 'sysfs_cpufreq',
      transport: 'local_hdc',
    },
    readiness: {status: 'ready', topology: `${domains.length}_frequency_domains`, cost_model: 'not_calibrated'},
    warnings,
  };
}

function redact(value, secret) {
  return String(value || '').split(secret).join('<device>');
}

function invokeHdc(execFileImpl, executable, args) {
  return new Promise((resolve, reject) => {
    execFileImpl(executable, args, {timeout: HDC_TIMEOUT_MS, maxBuffer: HDC_MAX_BUFFER, windowsHide: true}, (error, stdout = '', stderr = '') => {
      if (!error) return resolve({stdout: String(stdout), stderr: String(stderr)});
      reject({error, stdout: String(stdout), stderr: String(stderr)});
    });
  });
}

async function extractHardwareProfile({deviceSerial, hdcPath = 'hdc', execFileImpl = execFile, now} = {}) {
  const serial = String(deviceSerial || '').trim();
  const executable = String(hdcPath || 'hdc').trim();
  if (!serial || serial.length > 512 || serial.includes('\0')) throw new HardwareProfileExtractionError('请选择要提取硬件模型的本地设备。', 'hardware_profile_device_required');
  if (!executable || executable.length > 32768 || executable.includes('\0')) throw new HardwareProfileExtractionError('HDC 可执行文件路径无效。', 'hardware_profile_hdc_path_invalid');
  const started = Date.now();
  let response;
  try {
    response = await invokeHdc(execFileImpl, executable, ['-t', serial, 'shell', HARDWARE_PROBE_SCRIPT]);
  } catch (failure) {
    const reason = redact(failure.error?.code || failure.error?.message || 'unknown error', serial);
    const stderr = clean(redact(failure.stderr, serial), 1024);
    const timeout = failure.error?.killed || failure.error?.code === 'ETIMEDOUT';
    throw new HardwareProfileExtractionError(
      timeout ? '本地 HDC 硬件探测超过 30 秒，请确认设备仍在线后重试。' : `本地 HDC 硬件探测失败：${reason}${stderr ? `；${stderr}` : ''}`,
      timeout ? 'hardware_profile_probe_timeout' : 'hardware_profile_probe_failed',
    );
  }
  const profile = buildProfile(parseProbe(response.stdout), serial, now);
  return {
    profile,
    diagnostics: {
      domain_count: profile.cpu_groups.length,
      logical_cpu_count: profile.cpu_groups.reduce((sum, group) => sum + group.logical_cpu_ids.length, 0),
      physical_core_count: profile.cpu_groups.reduce((sum, group) => sum + group.physical_core_count, 0),
      stderr: clean(redact(response.stderr, serial), 1024),
    },
    durationMs: Date.now() - started,
    transport: 'local',
  };
}

module.exports = {
  HARDWARE_PROBE_SCRIPT,
  HardwareProfileExtractionError,
  buildProfile,
  extractHardwareProfile,
  parseCpuList,
  parseProbe,
};
