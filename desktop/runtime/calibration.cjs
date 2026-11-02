'use strict';

const {EventEmitter} = require('node:events');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const {execFile} = require('node:child_process');
const {TextDecoder} = require('node:util');

const ACTIVE = new Set(['preparing', 'running', 'cancelling']);
const FINAL = new Set(['succeeded', 'partial', 'failed', 'cancelled']);
const SERIAL = /^[A-Za-z0-9_.:-]{1,160}$/;
const PLAN_ID = /^cp_[a-f0-9]{24}$/;
const SHA256 = /^[a-f0-9]{64}$/;
const SAFE_GROUP = /^[A-Za-z0-9_.:-]{1,100}$/;
const POLICY_ID = /^(?:policy|cpu)(\d{1,4})$/;
const CPUFREQ_BASE = /^\/sys\/devices\/system\/cpu\/(?:cpufreq\/policy\d{1,4}|cpu\d{1,4}\/cpufreq)$/;
const MAX_PLAN_BYTES = 8 * 1024 * 1024;
const MAX_OUTPUT_BYTES = 2 * 1024 * 1024;
const REMOTE_ROOT = '/data/local/tmp';
const RECOVERY_SCHEMA = 'desktop_calibration_recovery/v1';
const MAX_RECOVERY_RUNS = 64;
const RECOVERY_RETRY_INTERVAL_MS = 5000;
const RUN_ID = /^cal_[a-f0-9]{32}$/;
const REMOTE_BINARY = /^\/data\/local\/tmp\/hetero_calibration_[a-f0-9]{16}(?:_(cal_[a-f0-9]{32}))?$/;

function clone(value) { return JSON.parse(JSON.stringify(value)); }
function now() { return new Date().toISOString(); }
function delay(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }
function sha256(value) { return crypto.createHash('sha256').update(value).digest('hex'); }
function redactDevice(value, serial) { return String(value || '').split(String(serial || '')).join('<device>'); }
function atomicJson(filename, value) {
  fs.mkdirSync(path.dirname(filename), {recursive: true, mode: 0o700});
  const temporary = `${filename}.${crypto.randomUUID()}.tmp`;
  try { fs.writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, {mode: 0o600}); fs.renameSync(temporary, filename); }
  finally { try { fs.unlinkSync(temporary); } catch {} }
}
function publicRun(run) {
  if (!run) return null;
  const result = clone(run);
  delete result.plan;
  delete result.hardwareProfile;
  delete result.measurementPath;
  result.log = (result.log || []).slice(-20);
  return result;
}
function calibrationError(code, message, details = {}) {
  const error = new Error(message); error.code = code; error.details = details; return error;
}
function strictInteger(value, label, minimum, maximum) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum)
    throw calibrationError('CALIBRATION_PLAN_INVALID', `${label} 无效。`);
  return value;
}
function safeGroup(value, label) {
  const result = String(value || '');
  if (!SAFE_GROUP.test(result)) throw calibrationError('CALIBRATION_PLAN_INVALID', `${label} 无效。`);
  return result;
}

function safeHdcPath(value) {
  const result = String(value || 'hdc');
  if (!result || result.length > 4096 || result.includes('\0') || /[\r\n]/.test(result))
    throw calibrationError('CALIBRATION_HDC_INVALID', 'HDC 可执行路径无效。');
  return result;
}

function safeProcessErrorCode(error) {
  const value = String(error?.code || 'UNKNOWN');
  if (/^\d{1,5}$/.test(value)) return value;
  return new Set(['EACCES', 'ECONNRESET', 'ENOENT', 'EPERM', 'ESRCH', 'ETIMEDOUT', 'SIGINT', 'SIGKILL', 'SIGTERM'])
    .has(value) ? value : 'UNKNOWN';
}

function abortRequested(control) {
  return control?.abortOnCancel === true && control?.cancelled === true;
}

function assertControlActive(control) {
  if (abortRequested(control))
    throw calibrationError('CALIBRATION_OPERATION_ABORTED', '校准后台操作已停止。');
}

function safeRecoveryRecord(value, expectedRunId = null) {
  if (!value || Array.isArray(value) || typeof value !== 'object'
      || value.schemaVersion !== RECOVERY_SCHEMA || !RUN_ID.test(String(value.runId || ''))
      || (expectedRunId && value.runId !== expectedRunId)
      || !SHA256.test(String(value.deviceSerialSha256 || '')))
    throw calibrationError('CALIBRATION_RECOVERY_INVALID', '遗留锁频恢复记录身份无效。');
  const remoteMatch = String(value.remoteBinary || '').match(REMOTE_BINARY);
  if (!remoteMatch || (remoteMatch[1] && remoteMatch[1] !== value.runId))
    throw calibrationError('CALIBRATION_RECOVERY_INVALID', '遗留锁频恢复记录的微基准路径无效。');
  if (value.frequencyRestorePending !== undefined && typeof value.frequencyRestorePending !== 'boolean')
    throw calibrationError('CALIBRATION_RECOVERY_INVALID', '遗留锁频恢复记录的恢复状态无效。');
  return {
    schemaVersion: RECOVERY_SCHEMA,
    runId: value.runId,
    deviceSerialSha256: value.deviceSerialSha256,
    hdcPath: safeHdcPath(value.hdcPath),
    remoteBinary: value.remoteBinary,
    frequencyState: safeFrequencyState(value.frequencyState),
    // v1 journals created before this field existed always represented an
    // armed frequency recovery.  Absence must therefore fail safe.
    frequencyRestorePending: value.frequencyRestorePending !== false,
    createdAt: typeof value.createdAt === 'string' ? value.createdAt : now(),
    frequencyRestoredAt: typeof value.frequencyRestoredAt === 'string' ? value.frequencyRestoredAt : null,
  };
}

function parseHdcTargetSerials(stdout) {
  const output = [];
  const seen = new Set();
  for (const rawLine of String(stdout || '').split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || /^\[|^(?:empty|no device|targets?\s*:)/i.test(line)) continue;
    // `hdc list targets` normally prints only the serial. Some HDC builds add
    // state/transport columns; recovery identity is still the first token.
    const serial = line.split(/\s+/)[0];
    if (!SERIAL.test(serial) || seen.has(serial)) continue;
    seen.add(serial); output.push(serial);
  }
  return output;
}

function safeFrequencyState(value) {
  if (!value || Array.isArray(value) || typeof value !== 'object')
    throw calibrationError('CALIBRATION_RECOVERY_INVALID', '频率恢复记录无效。');
  const output = {};
  for (const [rawId, raw] of Object.entries(value)) {
    const id = safeGroup(rawId, '恢复核组 ID');
    const cpu = strictInteger(raw?.cpu, `${id}.cpu`, 0, 4095);
    const base = String(raw?.base || '');
    if (!CPUFREQ_BASE.test(base)) throw calibrationError('CALIBRATION_RECOVERY_INVALID', `${id}.base 不是受支持的 cpufreq 路径。`);
    const minimum = strictInteger(raw?.minimum, `${id}.minimum`, 1, 100000000);
    const maximum = strictInteger(raw?.maximum, `${id}.maximum`, minimum, 100000000);
    output[id] = {cpu, base, minimum, maximum, writable: raw?.writable === true,
      sourcePolicyId: typeof raw?.sourcePolicyId === 'string' && POLICY_ID.test(raw.sourcePolicyId) ? raw.sourcePolicyId : null};
  }
  if (!Object.keys(output).length || Object.keys(output).length > 64)
    throw calibrationError('CALIBRATION_RECOVERY_INVALID', '频率恢复记录缺少有效频率域。');
  return output;
}

function cpuListGuard(baseExpression, cpu, {allowPerCpuIdentity = false} = {}) {
  // All interpolated values have already passed strict path/integer validation.
  const identity = allowPerCpuIdentity ? ` || [ "$base" = '/sys/devices/system/cpu/cpu${cpu}/cpufreq' ]` : '';
  return `cpulist_has_${cpu}() { for list_file in "$base/related_cpus" "$base/affected_cpus" "$base/cpus"; do [ -r "$list_file" ] || continue; list=$(cat "$list_file" 2>/dev/null) || continue; old_ifs=$IFS; IFS=', '; for token in $list; do case "$token" in *-*) first=${'${token%-*}'}; last=${'${token#*-}'}; [ "$first" -le ${cpu} ] 2>/dev/null && [ ${cpu} -le "$last" ] 2>/dev/null && { IFS=$old_ifs; return 0; } ;; *) [ "$token" = ${cpu} ] && { IFS=$old_ifs; return 0; } ;; esac; done; IFS=$old_ifs; done; return 1; }; base=${baseExpression}; [ -d "$base" ] && { cpulist_has_${cpu}${identity}; }`;
}

function cpufreqTokenFromBase(base) {
  const match = String(base || '').match(/^\/sys\/devices\/system\/cpu\/(?:cpufreq\/(policy\d{1,4})|(cpu\d{1,4})\/cpufreq)$/);
  if (!match) throw calibrationError('CALIBRATION_FREQUENCY_STATE_UNAVAILABLE', '已核验 cpufreq 路径无法转换为微基准频率源。');
  return match[1] || match[2];
}

function frequencyRecoverySummary(state, previous = {}) {
  return {
    domains: Object.entries(state || {}).map(([coreGroupId, item]) => ({coreGroupId, cpu: item.cpu, base: item.base,
      minimum: item.minimum, maximum: item.maximum, sourcePolicyId: item.sourcePolicyId || null})),
    cleanupPending: previous.cleanupPending !== false,
    cleanupErrors: Array.isArray(previous.cleanupErrors) ? previous.cleanupErrors : [],
    capturedAt: previous.capturedAt || now(), restoredAt: previous.restoredAt || null,
  };
}

function cleanupRows(errors) {
  return errors.map(error => ({phase: error.phase || 'cleanup', code: error.code || 'CALIBRATION_CLEANUP_FAILED',
    message: String(error.message || error).slice(0, 1000), coreGroupId: error.coreGroupId || null, at: now()}));
}

function blockingCleanupErrors(errors) {
  return errors.filter(error => error.phase === 'frequency_restore' || error.phase === 'recovery_checkpoint');
}

function validateDeviceHardwareProfile(expected, actual) {
  const expectedGroups = expected?.core_groups || expected?.cpu_groups;
  const actualGroups = actual?.core_groups || actual?.cpu_groups;
  if (!Array.isArray(expectedGroups) || !Array.isArray(actualGroups))
    throw calibrationError('CALIBRATION_DEVICE_PROFILE_MISMATCH', '无法核验所选设备与校准硬件画像。');
  const canonical = groups => groups.map(group => ({
    id: String(group?.id || group?.kind || ''),
    physical_core_count: Number(group?.physical_core_count),
    logical_cpu_ids: Array.isArray(group?.logical_cpu_ids) ? group.logical_cpu_ids.map(Number) : [],
    physical_core_representative_cpu_ids: Array.isArray(group?.physical_core_representative_cpu_ids)
      ? group.physical_core_representative_cpu_ids.map(Number) : (() => {
        const logical = Array.isArray(group?.logical_cpu_ids) ? group.logical_cpu_ids.map(Number) : [];
        const count = Number(group?.physical_core_count);
        return Number.isSafeInteger(count) && count > 0 ? Array.from({length: count}, (_unused, index) => logical[Math.floor(index * logical.length / count)]) : [];
      })(),
    opps_khz: Array.isArray(group?.opps_khz) ? [...group.opps_khz].map(Number).sort((a, b) => a - b) : [],
    source_policy_id: group?.source_policy_id == null ? null : String(group.source_policy_id),
  })).sort((left, right) => left.id.localeCompare(right.id));
  const expectedCanonical = canonical(expectedGroups);
  const actualCanonical = canonical(actualGroups);
  const expectedSerialHash = expected?.evidence?.device_serial_sha256;
  const actualSerialHash = actual?.evidence?.device_serial_sha256;
  if (expectedSerialHash && expectedSerialHash !== actualSerialHash)
    throw calibrationError('CALIBRATION_DEVICE_PROFILE_MISMATCH', '当前连接设备不是该硬件画像所登记的设备，请重新提取并生成校准方案。');
  const operatingDomain = expected?.source_metadata?.calibration_operating_domain;
  const fullOppCatalogSha256 = sha256(Buffer.from(JSON.stringify(actualCanonical.map(group => ({
    id: group.id, opps_khz: group.opps_khz,
  })))));
  const samePlacement = expectedCanonical.length === actualCanonical.length && expectedCanonical.every((group, index) => {
    const observed = actualCanonical[index];
    return group.id === observed.id
      && group.physical_core_count === observed.physical_core_count
      && JSON.stringify(group.logical_cpu_ids) === JSON.stringify(observed.logical_cpu_ids)
      && JSON.stringify(group.physical_core_representative_cpu_ids) === JSON.stringify(observed.physical_core_representative_cpu_ids)
      && group.source_policy_id === observed.source_policy_id;
  });
  const declaredSubsetDomain = operatingDomain?.schema_version === 'calibration_operating_domain/v1'
    && operatingDomain?.kind === 'verified_sustained_frequency_subset'
    && SHA256.test(String(operatingDomain?.full_opp_catalog_sha256 || ''))
    && operatingDomain.full_opp_catalog_sha256 === fullOppCatalogSha256;
  const validOppSubset = declaredSubsetDomain && samePlacement && expectedCanonical.every((group, index) => {
    const observed = actualCanonical[index].opps_khz;
    return group.opps_khz.length >= 1
      && group.opps_khz[0] === observed[0]
      && group.opps_khz.every(frequency => observed.includes(frequency));
  });
  if (JSON.stringify(expectedCanonical) !== JSON.stringify(actualCanonical) && !validOppSubset)
    throw calibrationError('CALIBRATION_DEVICE_PROFILE_MISMATCH', '当前设备的核组、物理核代表 CPU 或 OPP 与冻结硬件画像不一致，请重新提取并生成校准方案。',
      {expected_core_groups: expectedCanonical.map(group => group.id), actual_core_groups: actualCanonical.map(group => group.id)});
  return {verified: true, coreGroupCount: expectedCanonical.length, deviceSerialSha256: actualSerialHash || null,
    calibrationOperatingDomain: validOppSubset ? 'verified_sustained_frequency_subset' : 'full_hardware_opp_catalog'};
}

function deterministicExecutionOrder(planId, conditions) {
  return [...conditions].sort((left, right) => {
    const leftKey = sha256(Buffer.from(`${planId}\0${left.id}`));
    const rightKey = sha256(Buffer.from(`${planId}\0${right.id}`));
    return leftKey.localeCompare(rightKey) || left.id.localeCompare(right.id);
  });
}

function thermalRawRise(before, after) {
  const baseline = new Map((before || []).filter(row => Number.isFinite(row.temperature_raw))
    .map(row => [`${row.zone}\0${row.type}`, row.temperature_raw]));
  const rises = (after || []).filter(row => Number.isFinite(row.temperature_raw) && baseline.has(`${row.zone}\0${row.type}`))
    .map(row => row.temperature_raw - baseline.get(`${row.zone}\0${row.type}`));
  return rises.length ? Math.max(...rises) : null;
}

function normalizeCrlfBytes(contents) {
  let crlfCount = 0;
  for (let index = 0; index + 1 < contents.length; index++) {
    if (contents[index] === 13 && contents[index + 1] === 10) { crlfCount++; index++; }
  }
  if (!crlfCount) return {contents, crlfCount: 0};
  const normalized = Buffer.allocUnsafe(contents.length - crlfCount);
  let target = 0;
  for (let source = 0; source < contents.length; source++) {
    if (contents[source] === 13 && contents[source + 1] === 10) source++;
    normalized[target++] = contents[source];
  }
  return {contents: normalized, crlfCount};
}

function benchmarkBundleError(message, details) {
  return calibrationError('CALIBRATION_BENCHMARK_INVALID', message, details);
}

function decodeReceipt(contents, normalization) {
  if (contents.length >= 3 && contents[0] === 0xef && contents[1] === 0xbb && contents[2] === 0xbf)
    throw benchmarkBundleError('受管校准微基准构建回执不允许 UTF-8 BOM。',
      {item: 'receipt', check: 'encoding', encoding: 'utf-8-bom', normalization});
  try { return new TextDecoder('utf-8', {fatal: true}).decode(contents); }
  catch (error) { throw benchmarkBundleError('受管校准微基准构建回执不是有效 UTF-8。',
    {item: 'receipt', check: 'encoding', encoding: 'invalid-utf-8', normalization, cause: error.message}); }
}

function resolveBenchmarkBundle({resourcesDir, resourcesPath = process.resourcesPath, bundleRoot} = {}) {
  const relative = path.join('tools', 'parallel_calibration', 'prebuilt', 'aarch64-linux-ohos');
  const candidates = bundleRoot ? [bundleRoot] : [
    resourcesPath && path.join(resourcesPath, 'analysis-worker', relative),
    resourcesDir && path.join(resourcesDir, 'build', 'resources', 'analysis-worker', relative),
    resourcesDir && path.join(resourcesDir, 'analysis-worker', relative),
    path.resolve(__dirname, '..', '..', relative),
  ].filter(Boolean);
  let root = null;
  for (const candidate of candidates) {
    try { fs.lstatSync(candidate); root = candidate; break; }
    catch (error) {
      if (error.code !== 'ENOENT') throw benchmarkBundleError('受管校准微基准目录无法访问。',
        {item: 'bundle', check: 'accessible', cause: error.code || error.message});
    }
  }
  if (!root) throw calibrationError('CALIBRATION_BENCHMARK_UNAVAILABLE', 'Desktop 安装中缺少受管校准微基准，请更新或重新安装当前版本。',
    {item: 'bundle', check: 'manifest_present', candidates_checked: candidates.length});
  const rootStat = fs.lstatSync(root);
  if (!rootStat.isDirectory() || rootStat.isSymbolicLink())
    throw benchmarkBundleError('受管校准微基准目录不安全。',
      {item: 'bundle', check: 'regular_directory', symbolic_link: rootStat.isSymbolicLink()});
  const manifestPath = path.join(root, 'manifest.json');
  let manifestStat;
  try { manifestStat = fs.lstatSync(manifestPath); }
  catch (error) { throw benchmarkBundleError('受管校准微基准清单不存在或无法访问。',
    {item: 'manifest', check: 'accessible', cause: error.code || error.message}); }
  if (!manifestStat.isFile() || manifestStat.isSymbolicLink() || manifestStat.size > 128 * 1024)
    throw benchmarkBundleError('受管校准微基准清单不安全。',
      {item: 'manifest', check: 'regular_file', size_bytes: manifestStat.size, symbolic_link: manifestStat.isSymbolicLink()});
  let manifest;
  try { manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8')); }
  catch (error) { throw benchmarkBundleError('受管校准微基准清单无法读取。',
    {item: 'manifest', check: 'json', cause: error.message}); }
  if (manifest?.schema_version !== 'parallel_calibration_benchmark_bundle/v1'
      || !SHA256.test(String(manifest.binary_sha256 || ''))
      || !SHA256.test(String(manifest.receipt_sha256 || ''))
      || !Number.isSafeInteger(manifest.binary_bytes) || manifest.binary_bytes < 64 || manifest.binary_bytes > 4 * 1024 * 1024)
    throw benchmarkBundleError('受管校准微基准清单格式无效。',
      {item: 'manifest', check: 'contract', schema_version: manifest?.schema_version || null});
  const safeRelative = value => typeof value === 'string' && !value.includes('\\') && !path.posix.isAbsolute(value)
    && value.split('/').every(part => part && part !== '.' && part !== '..');
  if (!safeRelative(manifest.binary) || !safeRelative(manifest.receipt))
    throw benchmarkBundleError('受管校准微基准路径无效。',
      {item: 'manifest', check: 'relative_paths'});
  const binary = path.join(root, ...manifest.binary.split('/'));
  const receipt = path.join(root, ...manifest.receipt.split('/'));
  for (const [item, filename] of [['binary', binary], ['receipt', receipt]]) {
    let attributes;
    try { attributes = fs.lstatSync(filename); }
    catch (error) { throw benchmarkBundleError(`受管校准微基准${item === 'binary' ? '可执行文件' : '构建回执'}缺失或无法访问。`,
      {item, check: 'accessible', cause: error.code || error.message}); }
    if (!attributes.isFile() || attributes.isSymbolicLink())
      throw benchmarkBundleError(`受管校准微基准${item === 'binary' ? '可执行文件' : '构建回执'}不是安全的普通文件。`,
        {item, check: 'regular_file', symbolic_link: attributes.isSymbolicLink()});
  }
  const contents = fs.readFileSync(binary);
  if (contents.length !== manifest.binary_bytes)
    throw benchmarkBundleError('受管校准微基准可执行文件大小与清单不一致。',
      {item: 'binary', check: 'size', expected_bytes: manifest.binary_bytes, actual_bytes: contents.length});
  const binaryDigest = sha256(contents);
  if (binaryDigest !== manifest.binary_sha256)
    throw benchmarkBundleError('受管校准微基准可执行文件哈希校验失败。',
      {item: 'binary', check: 'sha256', expected_sha256: manifest.binary_sha256, actual_sha256: binaryDigest});
  const elfIdentity = {magic: contents.subarray(0, 4).toString('hex'), elf_class: contents[4], endianness: contents[5], machine: contents.readUInt16LE(18)};
  if (elfIdentity.magic !== '7f454c46' || elfIdentity.elf_class !== 2 || elfIdentity.endianness !== 1 || elfIdentity.machine !== 183)
    throw benchmarkBundleError('受管校准微基准可执行文件不是 AArch64 little-endian ELF64。',
      {item: 'binary', check: 'aarch64_elf64', ...elfIdentity});

  const receiptOnDisk = fs.readFileSync(receipt);
  const receiptDigest = sha256(receiptOnDisk);
  const normalizedReceipt = normalizeCrlfBytes(receiptOnDisk);
  const receiptContents = normalizedReceipt.contents;
  let receiptNormalization = 'none';
  const receiptCrlfCount = normalizedReceipt.crlfCount;
  const normalizedReceiptDigest = sha256(normalizedReceipt.contents);
  if (normalizedReceipt.crlfCount) {
    if (normalizedReceiptDigest !== manifest.receipt_sha256)
      throw benchmarkBundleError('受管校准微基准构建回执哈希校验失败。', {
        item: 'receipt', check: 'sha256', expected_sha256: manifest.receipt_sha256,
        actual_sha256: receiptDigest, crlf_normalized_sha256: normalizedReceiptDigest,
        crlf_count: normalizedReceipt.crlfCount,
      });
    receiptNormalization = 'crlf-to-lf';
  } else if (receiptDigest !== manifest.receipt_sha256)
    throw benchmarkBundleError('受管校准微基准构建回执哈希校验失败。', {
      item: 'receipt', check: 'sha256', expected_sha256: manifest.receipt_sha256,
      actual_sha256: receiptDigest, crlf_normalized_sha256: null, crlf_count: 0,
    });
  const receiptText = decodeReceipt(receiptContents, receiptNormalization);
  let receiptRecord;
  try { receiptRecord = JSON.parse(receiptText); }
  catch (error) { throw benchmarkBundleError('受管校准微基准构建回执不是有效 JSON。',
    {item: 'receipt', check: 'json', cause: error.message, normalization: receiptNormalization}); }
  if (receiptRecord?.schema_version !== 'parallel_calibration_benchmark_build/v1'
      || receiptRecord.target !== 'aarch64-linux-ohos'
      || receiptRecord.binary_sha256 !== manifest.binary_sha256
      || receiptRecord.binary_bytes !== manifest.binary_bytes)
    throw benchmarkBundleError('受管校准微基准构建回执与清单或二进制身份不一致。',
      {item: 'receipt', check: 'contract', normalization: receiptNormalization,
        schema_version: receiptRecord?.schema_version || null, target: receiptRecord?.target || null});
  return {root, binary, receipt, manifest, receiptContents, receiptNormalization, receiptCrlfCount};
}

function normalizeInputs({plan, hardwareProfile, deviceSerial}) {
  if (!plan || Array.isArray(plan) || typeof plan !== 'object'
      || plan.schema_version !== 'parallel_calibration_plan/v1' || !PLAN_ID.test(String(plan.id || ''))
      || !Array.isArray(plan.conditions) || !plan.conditions.length || plan.conditions.length > 2000
      || Buffer.byteLength(JSON.stringify(plan)) > MAX_PLAN_BYTES)
    throw calibrationError('CALIBRATION_PLAN_INVALID', '校准方案无效或条件数量超限。');
  if (!hardwareProfile || Array.isArray(hardwareProfile) || typeof hardwareProfile !== 'object'
      || String(hardwareProfile.id || '') !== String(plan.hardware_profile_id || ''))
    throw calibrationError('CALIBRATION_HARDWARE_MISMATCH', '校准方案与所选硬件画像不一致，请为当前设备重新生成方案。');
  if (plan.hardware_profile_sha256 && String(hardwareProfile.profile_sha256 || '') !== String(plan.hardware_profile_sha256))
    throw calibrationError('CALIBRATION_HARDWARE_MISMATCH', '校准方案绑定的硬件画像内容哈希与当前画像不一致，请重新生成方案。');
  if (!SERIAL.test(String(deviceSerial || '')))
    throw calibrationError('CALIBRATION_DEVICE_INVALID', '请选择当前连接的本地 HDC 设备。');
  const rawGroups = hardwareProfile.core_groups || hardwareProfile.cpu_groups;
  if (!Array.isArray(rawGroups) || !rawGroups.length || rawGroups.length > 64)
    throw calibrationError('CALIBRATION_HARDWARE_INVALID', '硬件画像缺少 CPU 核组。');
  const groups = new Map();
  const used = new Set();
  for (const raw of rawGroups) {
    const id = safeGroup(raw?.id || raw?.kind, '核组 ID');
    const physical = strictInteger(raw?.physical_core_count, `${id}.physical_core_count`, 1, 256);
    const logical = raw?.logical_cpu_ids;
    if (!Array.isArray(logical) || logical.length < physical || logical.length > 512)
      throw calibrationError('CALIBRATION_HARDWARE_INVALID', `${id} 缺少物理核代表 CPU。`);
    const representatives = raw.physical_core_representative_cpu_ids
      || Array.from({length: physical}, (_unused, index) => logical[Math.floor(index * logical.length / physical)]);
    if (!Array.isArray(representatives) || representatives.length !== physical || representatives.some(cpu => !logical.includes(cpu)))
      throw calibrationError('CALIBRATION_HARDWARE_INVALID', `${id} 缺少每个物理核各一个的代表 CPU。`);
    const cpus = representatives.map((cpu, index) => strictInteger(cpu, `${id}.physical_core_representative_cpu_ids[${index}]`, 0, 4095));
    if (cpus.some(cpu => used.has(cpu))) throw calibrationError('CALIBRATION_HARDWARE_INVALID', '不同核组的物理核代表 CPU 发生重叠。');
    cpus.forEach(cpu => used.add(cpu));
    const opps = raw.opps_khz;
    if (!Array.isArray(opps) || !opps.length || opps.some(value => !Number.isSafeInteger(value) || value < 1 || value > 100000000))
      throw calibrationError('CALIBRATION_HARDWARE_INVALID', `${id} 缺少有效 OPP。`);
    const sourcePolicyId = raw.source_policy_id == null || raw.source_policy_id === '' ? null : String(raw.source_policy_id);
    if (sourcePolicyId !== null && !POLICY_ID.test(sourcePolicyId))
      throw calibrationError('CALIBRATION_HARDWARE_INVALID', `${id}.source_policy_id 无效。`);
    groups.set(id, {id, cpus, logicalCpus: logical.map((cpu, index) => strictInteger(cpu, `${id}.logical_cpu_ids[${index}]`, 0, 4095)),
      sourcePolicyId, opps: [...new Set(opps)].sort((a, b) => a - b)});
  }
  const normalizedConditions = plan.conditions.map((raw, index) => {
    if (!raw || typeof raw !== 'object') throw calibrationError('CALIBRATION_PLAN_INVALID', `条件 ${index + 1} 无效。`);
    const core = safeGroup(raw.core_kind, `条件 ${index + 1} 核组`);
    const group = groups.get(core);
    if (!group) throw calibrationError('CALIBRATION_HARDWARE_MISMATCH', `方案核组 ${core} 不在所选硬件画像中。`);
    const frequency = strictInteger(raw.frequency_khz, 'frequency_khz', 1, 100000000);
    if (!group.opps.includes(frequency))
      throw calibrationError('CALIBRATION_HARDWARE_MISMATCH', `方案频点 ${frequency} kHz 不属于 ${core} 的当前 OPP。`);
    const benchmark = String(raw.benchmark || '');
    if (!['cpu_work', 'empty_dispatch', 'pair_work', 'roofline'].includes(benchmark)) throw calibrationError('CALIBRATION_PLAN_INVALID', '方案包含未知微基准。');
    const family = String(raw.family || '');
    if (!['build', 'residual', 'roofline'].includes(family)) throw calibrationError('CALIBRATION_PLAN_INVALID', '方案包含未知任务族。');
    const activeTasks = strictInteger(raw.active_tasks, 'active_tasks', 1, 8);
    const condition = {...clone(raw), core_kind: core, family, frequency_khz: frequency, active_tasks: activeTasks,
      work_units: strictInteger(raw.work_units, 'work_units', 1, 2000000000),
      repeats: strictInteger(raw.repeats, 'repeats', 1, 30)};
    if (!/^condition_[a-f0-9]{12}$/.test(String(condition.id || ''))) throw calibrationError('CALIBRATION_PLAN_INVALID', '方案条件 ID 无效。');
    if (!['train', 'validation'].includes(condition.split)) throw calibrationError('CALIBRATION_PLAN_INVALID', '方案条件 split 无效。');
    if (benchmark === 'pair_work') {
      if (!['compute', 'memory', 'mixed'].includes(condition.workload_class)) throw calibrationError('CALIBRATION_PLAN_INVALID', 'pair_work 负载类型无效。');
      if (!['none', 'compute', 'memory', 'mixed'].includes(condition.peer_workload_class)) throw calibrationError('CALIBRATION_PLAN_INVALID', 'pair_work 干扰负载类型无效。');
      condition.working_set_bytes = strictInteger(condition.working_set_bytes, 'working_set_bytes', 4096, 2 * 1024 * 1024 * 1024);
      if (activeTasks === 2) {
        if (condition.peer_workload_class === 'none') throw calibrationError('CALIBRATION_PLAN_INVALID', '双任务共跑必须指定干扰负载类型。');
        condition.peer_core_kind = safeGroup(condition.peer_core_kind, '干扰核组');
        const peer = groups.get(condition.peer_core_kind);
        condition.peer_frequency_khz = strictInteger(condition.peer_frequency_khz, 'peer_frequency_khz', 1, 100000000);
        if (!peer || !peer.opps.includes(condition.peer_frequency_khz))
          throw calibrationError('CALIBRATION_HARDWARE_MISMATCH', '共跑干扰核组或频点与硬件画像不一致。');
      } else if (condition.peer_workload_class !== 'none' || Number(condition.peer_frequency_khz || 0) !== 0) {
        throw calibrationError('CALIBRATION_PLAN_INVALID', '单任务 pair_work 的干扰负载必须为 none 且干扰频点为 0。');
      }
    }
    if (benchmark === 'roofline') {
      if (family !== 'roofline' || activeTasks !== 1
          || !['compute_ceiling', 'bandwidth_ceiling', 'intensity_ladder', 'instruction_proxy'].includes(condition.workload_class))
        throw calibrationError('CALIBRATION_PLAN_INVALID', '经典 Roofline 条件的任务族、角色或并发数无效。');
      condition.working_set_bytes = strictInteger(condition.working_set_bytes, 'working_set_bytes', 1024 * 1024, 512 * 1024 * 1024);
      condition.roofline_operations_per_element = strictInteger(condition.roofline_operations_per_element, 'roofline_operations_per_element', 2, 4096);
      if (condition.roofline_operations_per_element % 2)
        throw calibrationError('CALIBRATION_PLAN_INVALID', '经典 Roofline 每元素操作数必须为偶数。');
      if (condition.workload_class === 'compute_ceiling' && condition.roofline_operations_per_element !== 32)
        throw calibrationError('CALIBRATION_PLAN_INVALID', '计算屋顶使用冻结的 32 FLOP/工作单元内核。');
      if (condition.roofline_kernel_version !== 'classic-roofline-blocked-v2')
        throw calibrationError('CALIBRATION_PLAN_INVALID', '经典 Roofline 方案必须使用分块多路 ILP v2 内核。');
    }
    return condition;
  });
  return {plan: clone(plan), hardwareProfile: clone(hardwareProfile), deviceSerial: String(deviceSerial), groups, conditions: normalizedConditions};
}

function placementsFor(condition, groups, observer) {
  const targetGroup = groups.get(condition.core_kind);
  const target = targetGroup.cpus.find(cpu => cpu !== observer);
  if (target === undefined) throw calibrationError('CALIBRATION_PLACEMENT_UNAVAILABLE', `${condition.core_kind} 没有可用的目标物理核。`);
  const candidates = [...targetGroup.cpus, ...[...groups.values()].filter(group => group.id !== targetGroup.id).flatMap(group => group.cpus)]
    .filter(cpu => cpu !== observer && cpu !== target);
  let helpers = candidates.slice(0, condition.active_tasks - 1);
  if (condition.benchmark === 'pair_work' && condition.active_tasks === 2) {
    const peer = groups.get(condition.peer_core_kind);
    helpers = [peer.cpus.find(cpu => cpu !== observer && cpu !== target)].filter(cpu => cpu !== undefined);
  }
  if (helpers.length !== condition.active_tasks - 1)
    throw calibrationError('CALIBRATION_PLACEMENT_UNAVAILABLE', `条件 ${condition.id} 没有足够的独立物理核。`);
  return {target, helpers, observer};
}

function chooseObserver(conditions, groups) {
  const cpus = [...groups.values()].flatMap(group => group.cpus);
  for (const observer of [...cpus].reverse()) {
    try { for (const condition of conditions) placementsFor(condition, groups, observer); return observer; } catch {}
  }
  throw calibrationError('CALIBRATION_PLACEMENT_UNAVAILABLE', '当前拓扑无法同时保留独立观测核；请减少共跑任务数或使用更多物理核。');
}

function benchmarkArguments(remoteBinary, condition, placement, frequencyState) {
  const targetFrequency = frequencyState?.[condition.core_kind];
  if (!targetFrequency?.sourcePolicyId || !POLICY_ID.test(targetFrequency.sourcePolicyId))
    throw calibrationError('CALIBRATION_FREQUENCY_STATE_UNAVAILABLE', `缺少 ${condition.core_kind} 已核验的频率采样路径。`);
  const args = [remoteBinary, '--benchmark', condition.benchmark, '--family', condition.family,
    '--cpu', String(placement.target), '--observer-cpu', String(placement.observer),
    '--frequency-khz', String(condition.frequency_khz), '--work-units', String(condition.work_units),
    '--frequency-policy-id', targetFrequency.sourcePolicyId,
    '--active-tasks', String(condition.active_tasks), '--sample-period-us', '1000', '--frequency-tolerance', '0.03'];
  if (placement.helpers.length) args.push('--helper-cpus', placement.helpers.join(','));
  if (condition.benchmark === 'pair_work') args.push(
    '--workload-class', condition.workload_class,
    '--peer-workload-class', condition.peer_workload_class,
    '--peer-frequency-khz', String(condition.peer_frequency_khz || 0),
    '--working-set-bytes', String(condition.working_set_bytes),
  );
  if (condition.benchmark === 'pair_work' && condition.active_tasks === 2) {
    const peerFrequency = frequencyState?.[condition.peer_core_kind];
    if (!peerFrequency?.sourcePolicyId || !POLICY_ID.test(peerFrequency.sourcePolicyId))
      throw calibrationError('CALIBRATION_FREQUENCY_STATE_UNAVAILABLE', `缺少 ${condition.peer_core_kind} 已核验的频率采样路径。`);
    args.push('--peer-frequency-policy-id', peerFrequency.sourcePolicyId);
  }
  if (condition.benchmark === 'roofline') args.push(
    '--workload-class', condition.workload_class,
    '--working-set-bytes', String(condition.working_set_bytes),
    '--roofline-operations-per-element', String(condition.roofline_operations_per_element),
  );
  return args;
}

const PAIRED_DISPATCH_TOTAL_FIELDS = [
  'direct_batch_worker_cpu_ms', 'direct_batch_worker_wall_ms',
  'direct_batch_producer_cpu_ms', 'direct_batch_roundtrip_wall_ms',
  'queue_dispatch_worker_cpu_ms', 'queue_dispatch_worker_wall_ms',
  'queue_dispatch_producer_cpu_ms', 'queue_dispatch_roundtrip_wall_ms',
];
const PAIRED_DISPATCH_DELTA_FIELDS = [
  'dispatch_wall_delta_ms_per_task', 'dispatch_worker_cpu_delta_ms_per_task',
  'dispatch_producer_cpu_delta_ms_per_task',
];
const TARGET_SIBLING_BUSY_THRESHOLD = 0.05;
const MANAGED_WORKLOAD_VERSION = 'parallel-calibration-proxy-v6';
const WARMUP_FREQUENCY_REQUIRED_CONSECUTIVE = 3;
const WARMUP_FREQUENCY_MAX_SAMPLES = 12;
const FREQUENCY_TOLERANCE = 0.03;

function validatePairedDispatchMeasurement(measured) {
  if (measured?.benchmark !== 'empty_dispatch'
      || !['parallel-calibration-proxy-v4', 'parallel-calibration-proxy-v5', MANAGED_WORKLOAD_VERSION]
        .includes(measured?.workload_version)) return;
  if (measured.dispatch_measurement_schema !== 'paired_dispatch_delta/v1'
      || measured.dispatch_payload !== 'mix64_v1' || measured.dispatch_payload_match !== true
      || measured.cold_start_included !== false)
    throw calibrationError('CALIBRATION_MEASUREMENT_MISMATCH', 'Worker 投递实测缺少同负载配对差分合同或负载结果不一致。');
  for (const field of PAIRED_DISPATCH_TOTAL_FIELDS)
    if (!Number.isFinite(measured[field]) || measured[field] < 0)
      throw calibrationError('CALIBRATION_MEASUREMENT_MISMATCH', `Worker 投递实测 ${field} 无效。`);
  for (const field of PAIRED_DISPATCH_DELTA_FIELDS)
    if (!Number.isFinite(measured[field]))
      throw calibrationError('CALIBRATION_MEASUREMENT_MISMATCH', `Worker 投递实测 ${field} 无效。`);
  if (!Number.isSafeInteger(measured.work_units) || measured.work_units < 1)
    throw calibrationError('CALIBRATION_MEASUREMENT_MISMATCH', 'Worker 投递实测 task_count 无效。');
  const expected = {
    dispatch_wall_delta_ms_per_task:
      (measured.queue_dispatch_roundtrip_wall_ms - measured.direct_batch_roundtrip_wall_ms) / measured.work_units,
    dispatch_worker_cpu_delta_ms_per_task:
      (measured.queue_dispatch_worker_cpu_ms - measured.direct_batch_worker_cpu_ms) / measured.work_units,
    dispatch_producer_cpu_delta_ms_per_task:
      (measured.queue_dispatch_producer_cpu_ms - measured.direct_batch_producer_cpu_ms) / measured.work_units,
  };
  for (const [field, value] of Object.entries(expected)) {
    const tolerance = Math.max(1e-9, Math.abs(value) * 1e-6);
    if (Math.abs(measured[field] - value) > tolerance)
      throw calibrationError('CALIBRATION_MEASUREMENT_MISMATCH', `Worker 投递实测 ${field} 与配对总量不一致。`);
  }
}

function managedFrequencyPath({source, cpu, policyId}) {
  if (source === 'cpuinfo_cur_freq')
    return `/sys/devices/system/cpu/cpu${cpu}/cpufreq/cpuinfo_cur_freq`;
  if (source !== 'scaling_cur_freq_fallback' || !POLICY_ID.test(String(policyId || ''))) return null;
  return String(policyId).startsWith('policy')
    ? `/sys/devices/system/cpu/cpufreq/${policyId}/scaling_cur_freq`
    : `/sys/devices/system/cpu/${policyId}/cpufreq/scaling_cur_freq`;
}

function validateFrequencyEvidenceMeasurement(measured, {
  expectedCpu = measured?.cpu, expectedPolicyId = measured?.frequency_policy_id,
  peerCpu = null, peerPolicyId = measured?.peer_frequency_policy_id,
} = {}) {
  const mismatch = message => { throw calibrationError('CALIBRATION_MEASUREMENT_MISMATCH', message); };
  if (!measured || typeof measured !== 'object' || Array.isArray(measured)) mismatch('微基准缺少目标逻辑 CPU 实际频率证据。');
  if (measured.workload_version !== MANAGED_WORKLOAD_VERSION)
    mismatch(`受管微基准必须返回 ${MANAGED_WORKLOAD_VERSION} 实际频率合同。`);
  const integer = (value, label, minimum = 0, maximum = Number.MAX_SAFE_INTEGER) => {
    if (!Number.isSafeInteger(value) || value < minimum || value > maximum) mismatch(`${label} 无效。`);
    return value;
  };
  const finite = (value, label, minimum = 0) => {
    if (!Number.isFinite(value) || value < minimum) mismatch(`${label} 无效。`);
    return value;
  };
  const validateRole = ({prefix = '', cpu, policyId, requestedKhz, requireWarmup = false}) => {
    integer(cpu, `${prefix || 'target_'}frequency_source_cpu`, 0, 4095);
    const source = measured[`${prefix}frequency_source`];
    if (!['cpuinfo_cur_freq', 'scaling_cur_freq_fallback'].includes(source))
      mismatch(`${prefix || 'target_'}frequency_source 无效。`);
    const sourceCpu = integer(measured[`${prefix}frequency_source_cpu`], `${prefix}frequency_source_cpu`, 0, 4095);
    if (sourceCpu !== cpu) mismatch(`${prefix || '目标'}频率采样 CPU 与实际工作线程 CPU 不一致。`);
    const expectedPath = managedFrequencyPath({source, cpu, policyId});
    if (!expectedPath || measured[`${prefix}frequency_source_path`] !== expectedPath)
      mismatch(`${prefix || '目标'}频率采样路径没有冻结到实际工作线程。`);
    const samples = integer(measured[`${prefix}frequency_sample_count`], `${prefix}frequency_sample_count`, 0);
    const failures = integer(measured[`${prefix}frequency_read_failures`], `${prefix}frequency_read_failures`, 0);
    const mean = measured[prefix ? 'peer_observed_frequency_khz' : 'frequency_mean_khz'];
    const minimum = measured[`${prefix}frequency_min_khz`];
    const maximum = measured[`${prefix}frequency_max_khz`];
    let windowInRange = false;
    if (samples > 0) {
      finite(mean, `${prefix}frequency_mean_khz`, 1);
      finite(minimum, `${prefix}frequency_min_khz`, 1);
      finite(maximum, `${prefix}frequency_max_khz`, 1);
      if (minimum > mean || mean > maximum) mismatch(`${prefix || '目标'}频率区间与均值不一致。`);
      const low = requestedKhz * (1 - FREQUENCY_TOLERANCE);
      const high = requestedKhz * (1 + FREQUENCY_TOLERANCE);
      windowInRange = minimum >= low && maximum <= high;
    } else if (mean !== null || minimum !== null || maximum !== null) {
      mismatch(`${prefix || '目标'}频率没有有效采样时不能携带区间结论。`);
    }
    let warmupVerified = true;
    if (requireWarmup) {
      const count = integer(measured.warmup_frequency_sample_count, 'warmup_frequency_sample_count', 1,
        WARMUP_FREQUENCY_MAX_SAMPLES);
      const consecutive = integer(measured.warmup_frequency_in_range_consecutive,
        'warmup_frequency_in_range_consecutive', 0, WARMUP_FREQUENCY_REQUIRED_CONSECUTIVE);
      if (measured.warmup_frequency_required_consecutive !== WARMUP_FREQUENCY_REQUIRED_CONSECUTIVE
          || measured.warmup_frequency_max_samples !== WARMUP_FREQUENCY_MAX_SAMPLES)
        mismatch('驻留预热频率门限与受管 v6 合同不一致。');
      if (consecutive > count) mismatch('驻留预热连续命中数超过采样数。');
      const warmupMinimum = measured.warmup_frequency_min_khz;
      const warmupMaximum = measured.warmup_frequency_max_khz;
      const warmupLast = measured.warmup_frequency_last_khz;
      if (warmupMinimum !== null || warmupMaximum !== null || warmupLast !== null) {
        finite(warmupMinimum, 'warmup_frequency_min_khz', 1);
        finite(warmupMaximum, 'warmup_frequency_max_khz', 1);
        finite(warmupLast, 'warmup_frequency_last_khz', 1);
        if (warmupMinimum > warmupLast || warmupLast > warmupMaximum)
          mismatch('驻留预热频率区间与末次读数不一致。');
      }
      warmupVerified = consecutive >= WARMUP_FREQUENCY_REQUIRED_CONSECUTIVE;
      if (measured.warmup_frequency_verified !== warmupVerified)
        mismatch('驻留预热核验结论与连续频率证据不一致。');
      if (warmupVerified) {
        const low = requestedKhz * (1 - FREQUENCY_TOLERANCE);
        const high = requestedKhz * (1 + FREQUENCY_TOLERANCE);
        if (!Number.isFinite(warmupLast) || warmupLast < low || warmupLast > high)
          mismatch('驻留预热末次频率不在目标 OPP ±3% 内。');
      }
      const warmupUnits = integer(measured.warmup_work_units, 'warmup_work_units', 1);
      const chunk = measured.benchmark === 'empty_dispatch' ? 32 : Math.min(measured.work_units, 32768);
      if (warmupUnits !== count * chunk) mismatch('驻留预热工作量与逐轮频率采样数不一致。');
    }
    const expectedVerified = source === 'cpuinfo_cur_freq' && warmupVerified
      && samples >= 5 && failures === 0 && windowInRange;
    if (typeof measured[`${prefix}frequency_verified`] !== 'boolean'
        || measured[`${prefix}frequency_verified`] !== expectedVerified)
      mismatch(`${prefix || '目标'}频率核验结论与冻结采样证据不一致。`);
    return {verified: expectedVerified, source, sourceCpu, sourcePath: expectedPath,
      samples, failures, minimum, maximum, mean, warmupVerified};
  };
  const target = validateRole({cpu: expectedCpu, policyId: expectedPolicyId,
    requestedKhz: measured.frequency_khz, requireWarmup: true});
  let peer = null;
  if (peerCpu !== null) peer = validateRole({prefix: 'peer_', cpu: peerCpu, policyId: peerPolicyId,
    requestedKhz: measured.peer_frequency_khz, requireWarmup: false});
  return {target, peer};
}

function validatePairHelperPlacementMeasurement(measured, expectedCpu) {
  const mismatch = message => { throw calibrationError('CALIBRATION_MEASUREMENT_MISMATCH', message); };
  const helper = Array.isArray(measured?.helpers) && measured.helpers.length === 1
    && measured.helpers[0] && typeof measured.helpers[0] === 'object'
    ? measured.helpers[0] : null;
  if (!helper || helper.cpu !== expectedCpu)
    mismatch('共跑微基准没有返回冻结 helper CPU 的放置证据。');
  if (measured.affinity_verified === true
      && (helper.actual_cpu !== helper.cpu || helper.affinity_verified !== true))
    mismatch('共跑 helper 的逐线程实际落核证据与汇总结论不一致。');
  return {cpu: helper.cpu, actualCpu: helper.actual_cpu,
    affinityVerified: helper.affinity_verified === true && helper.actual_cpu === helper.cpu};
}

function validateTargetSiblingQuietnessMeasurement(measured) {
  const mismatch = message => { throw calibrationError('CALIBRATION_MEASUREMENT_MISMATCH', message); };
  if (!measured || typeof measured !== 'object' || Array.isArray(measured)) mismatch('微基准缺少目标物理核同胞线程静默度证据。');
  const targetCpu = measured.cpu;
  if (!Number.isSafeInteger(targetCpu) || targetCpu < 0 || targetCpu > 4095) mismatch('目标 CPU 编号无效。');
  const siblingCpus = measured.target_sibling_cpus;
  const activity = measured.target_sibling_activity;
  if (!Array.isArray(siblingCpus) || siblingCpus.length > 64
      || siblingCpus.some(cpu => !Number.isSafeInteger(cpu) || cpu < 0 || cpu > 4095 || cpu === targetCpu)
      || new Set(siblingCpus).size !== siblingCpus.length)
    mismatch('目标物理核同胞逻辑 CPU 列表无效。');
  if (!Array.isArray(activity) || activity.length !== siblingCpus.length)
    mismatch('目标物理核同胞线程活动明细不完整。');
  if (!Number.isFinite(measured.target_sibling_busy_threshold)
      || Math.abs(measured.target_sibling_busy_threshold - TARGET_SIBLING_BUSY_THRESHOLD) > 1e-12)
    mismatch('目标物理核同胞线程静默阈值与受管合同不一致。');
  if (typeof measured.target_sibling_activity_verified !== 'boolean'
      || typeof measured.target_sibling_quietness_verified !== 'boolean')
    mismatch('目标物理核同胞线程核验状态无效。');
  let maximum = 0;
  let everyVerified = true;
  activity.forEach((row, index) => {
    if (!row || typeof row !== 'object' || Array.isArray(row) || row.cpu !== siblingCpus[index]
        || typeof row.verified !== 'boolean') mismatch('目标物理核同胞线程活动明细与 CPU 列表不一致。');
    for (const field of ['busy_ticks', 'total_ticks'])
      if (!Number.isSafeInteger(row[field]) || row[field] < 0) mismatch(`同胞线程 ${field} 无效。`);
    if (row.verified) {
      if (row.total_ticks < 1 || row.busy_ticks > row.total_ticks || !Number.isFinite(row.busy_fraction)
          || row.busy_fraction < 0 || row.busy_fraction > 1
          || Math.abs(row.busy_fraction - row.busy_ticks / row.total_ticks) > 1e-8)
        mismatch('目标物理核同胞线程忙碌占比与计数器差分不一致。');
      maximum = Math.max(maximum, row.busy_fraction);
    } else {
      everyVerified = false;
      if (row.busy_fraction !== null || row.busy_ticks !== 0 || row.total_ticks !== 0)
        mismatch('未核验的同胞线程活动不能携带部分计数结论。');
    }
  });
  const expectedEvidence = siblingCpus.length === 0
    ? measured.target_sibling_activity_verified
    : everyVerified;
  if (siblingCpus.length > 0 && measured.target_sibling_activity_verified !== expectedEvidence)
    mismatch('目标物理核同胞线程活动核验状态与逐线程证据不一致。');
  if (expectedEvidence) {
    if (!Number.isFinite(measured.target_sibling_busy_fraction)
        || Math.abs(measured.target_sibling_busy_fraction - maximum) > 1e-8)
      mismatch('目标物理核同胞线程最坏忙碌占比与逐线程证据不一致。');
    const expectedQuietness = maximum <= TARGET_SIBLING_BUSY_THRESHOLD;
    if (measured.target_sibling_quietness_verified !== expectedQuietness)
      mismatch('目标物理核同胞线程静默结论与忙碌占比不一致。');
  } else if (measured.target_sibling_busy_fraction !== null || measured.target_sibling_quietness_verified !== false) {
    mismatch('缺少同胞线程活动证据时不能声明静默。');
  }
  return {activityVerified: expectedEvidence, quietnessVerified: measured.target_sibling_quietness_verified,
    busyFraction: measured.target_sibling_busy_fraction, siblingCount: siblingCpus.length};
}

class CalibrationManager extends EventEmitter {
  constructor({userDataDir, resourcesDir, resourcesPath = process.resourcesPath, execFileImpl = execFile, sleepImpl = delay} = {}) {
    super();
    if (!userDataDir) throw new Error('userDataDir is required');
    this.root = path.join(userDataDir, 'calibration');
    this.runsRoot = path.join(this.root, 'runs');
    this.resourcesDir = resourcesDir;
    this.resourcesPath = resourcesPath;
    this.execFile = execFileImpl;
    this.sleep = sleepImpl;
    this.active = new Map();
    fs.mkdirSync(this.runsRoot, {recursive: true, mode: 0o700});
    this.recoveryQueue = Promise.resolve();
    this.recoveryReconcileRequest = null;
    this.lastRecoveryReconcileAt = 0;
    // Recovery I/O is different from an active run's cleanup I/O: cancelling
    // a measurement must still restore its frequency bounds, while disposing
    // the manager must stop a background recovery from starting more HDC
    // commands. `abortOnCancel` makes that distinction explicit.
    this.recoveryControl = {cancelled: false, child: null, abortOnCancel: true};
    this.disposed = false;
    // `initialReady` is the one startup fence. Later background retries stay
    // on `recoveryQueue`; they must never move the public readiness fence and
    // thereby delay cancel/dispose of a live calibration.
    this.initialReady = this.queueRecoveryReconcile();
    this.ready = this.initialReady;
  }
  runPath(id) { return path.join(this.runsRoot, id, 'run.json'); }
  measurementsPath(id) { return path.join(this.runsRoot, id, 'measurements.json'); }
  recoveryPath(id) { return path.join(this.runsRoot, id, 'recovery.json'); }
  save(run) { atomicJson(this.runPath(run.runId), run); this.emit('event', {type: 'calibration', target: 'device', run: publicRun(run)}); }
  load(runId) {
    const id = String(runId || '');
    if (!/^cal_[a-f0-9]{32}$/.test(id)) throw calibrationError('CALIBRATION_RUN_NOT_FOUND', '校准运行 ID 无效。');
    try { return JSON.parse(fs.readFileSync(this.runPath(id), 'utf8')); }
    catch { throw calibrationError('CALIBRATION_RUN_NOT_FOUND', '校准运行不存在。'); }
  }
  saveRecovery(run, {hdcPath, remoteBinary, frequencyState}) {
    const state = safeFrequencyState(frequencyState);
    const record = {schemaVersion: RECOVERY_SCHEMA, runId: run.runId,
      deviceSerialSha256: run.deviceSerialSha256, hdcPath: safeHdcPath(hdcPath), remoteBinary,
      frequencyState: state, frequencyRestorePending: true, createdAt: now(), frequencyRestoredAt: null};
    safeRecoveryRecord(record, run.runId);
    atomicJson(this.recoveryPath(run.runId), record);
    run.frequencyRecovery = frequencyRecoverySummary(state, run.frequencyRecovery);
    run.frequencyRecovery.cleanupPending = true;
    run.updatedAt = now();
    this.save(run);
    return record;
  }
  markRecoveryFrequencyRestored(runId) {
    const current = safeRecoveryRecord(JSON.parse(fs.readFileSync(this.recoveryPath(runId), 'utf8')), runId);
    const updated = {...current, frequencyRestorePending: false, frequencyRestoredAt: now()};
    atomicJson(this.recoveryPath(runId), updated);
    return updated;
  }
  clearRecovery(run) {
    try { fs.unlinkSync(this.recoveryPath(run.runId)); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
  queueRecoveryReconcile() {
    const task = this.recoveryQueue.catch(() => {}).then(() => this.reconcileRecoveries());
    this.recoveryQueue = task;
    return task;
  }
  requestRecoveryReconcile() {
    if (this.disposed) return Promise.resolve();
    if (this.recoveryReconcileRequest) return this.recoveryReconcileRequest;
    const waitMs = Math.max(0, RECOVERY_RETRY_INTERVAL_MS - (Date.now() - this.lastRecoveryReconcileAt));
    const request = (async () => {
      if (waitMs) await this.sleep(waitMs);
      this.lastRecoveryReconcileAt = Date.now();
      await this.queueRecoveryReconcile();
    })();
    let wrapped;
    wrapped = request.finally(() => {
      if (this.recoveryReconcileRequest === wrapped) this.recoveryReconcileRequest = null;
    });
    this.recoveryReconcileRequest = wrapped;
    return wrapped;
  }
  pendingRecoveryFor(deviceSerialSha256) {
    let entries = [];
    try { entries = fs.readdirSync(this.runsRoot, {withFileTypes: true}); } catch { return null; }
    for (const entry of entries) {
      if (!entry.isDirectory() || !/^cal_[a-f0-9]{32}$/.test(entry.name)) continue;
      const recoveryExists = fs.existsSync(this.recoveryPath(entry.name));
      let trustedRecovery = false;
      try {
        const record = JSON.parse(fs.readFileSync(this.recoveryPath(entry.name), 'utf8'));
        // This gate intentionally needs only the journal identity. If the
        // restore payload itself is damaged, allowing another run on the same
        // device would be less safe than keeping it blocked for diagnosis.
        const safe = safeRecoveryRecord(record, entry.name);
        trustedRecovery = true;
        if (safe.deviceSerialSha256 === deviceSerialSha256 && safe.frequencyRestorePending) return entry.name;
        if (!safe.frequencyRestorePending) continue;
      } catch {}
      try {
        const run = this.load(entry.name);
        if (run.deviceSerialSha256 === deviceSerialSha256
            && (run.frequencyRecovery?.cleanupPending === true || ACTIVE.has(run.status))) return entry.name;
        // A valid run identity scopes a damaged/missing journal to that device.
        if (SHA256.test(String(run.deviceSerialSha256 || ''))) continue;
      } catch {}
      // A recovery journal proves that a frequency transaction was armed. If
      // both it and run.json are unreadable, its device identity is unknown;
      // fail closed globally rather than risk stacking another lock operation.
      if (recoveryExists && !trustedRecovery) return entry.name;
    }
    return null;
  }
  async discoverRecoveryDevices(hdcPath, control = this.recoveryControl) {
    assertControlActive(control);
    return new Promise((resolve, reject) => {
      let child; let settled = false;
      const finish = (error, stdout = '') => {
        if (settled) return; settled = true;
        if (control?.child === child) control.child = null;
        if (abortRequested(control))
          return reject(calibrationError('CALIBRATION_OPERATION_ABORTED', '校准后台设备发现已停止。'));
        if (error) return reject(calibrationError('CALIBRATION_RECOVERY_DEVICE_DISCOVERY_FAILED',
          `遗留锁频恢复无法发现本地设备（HDC ${safeProcessErrorCode(error)}）。`));
        resolve(parseHdcTargetSerials(stdout));
      };
      try {
        child = this.execFile(safeHdcPath(hdcPath), ['list', 'targets'], {timeout: 10000, maxBuffer: 128 * 1024, windowsHide: true},
          finish);
        if (control && !settled) control.child = child;
      } catch (error) { finish(error); }
    });
  }
  recoveryFailure(run, errors, {interrupted = false} = {}) {
    const rows = cleanupRows(errors);
    const state = run.frequencyRecovery || {domains: [], capturedAt: null, restoredAt: null};
    run.frequencyRecovery = {...state, cleanupPending: true, cleanupErrors: rows};
    run.cleanupErrors = rows;
    if (run.status === 'succeeded') run.status = 'partial';
    if (interrupted || ACTIVE.has(run.status)) {
      run.status = 'failed';
      run.error = {code: 'CALIBRATION_RUN_INTERRUPTED_RECOVERY_PENDING',
        message: '上次校准在完成设备恢复前中断；平台尚未恢复全部频率边界，将在设备再次连接时重试。', details: {cleanupErrors: rows}};
      run.completedAt = run.completedAt || now();
    }
    run.stage = 'cleanup_pending'; run.updatedAt = now(); this.save(run);
  }
  recoveryRun(record) {
    let run = null;
    try { run = this.load(record.runId); } catch {}
    if (run?.runId === record.runId && run.deviceSerialSha256 === record.deviceSerialSha256) return run;
    return {
      schemaVersion: 'desktop_calibration_run/v1', runId: record.runId, status: 'preparing', stage: 'cleanup_pending',
      planId: null, hardwareProfileId: null, deviceSerialSha256: record.deviceSerialSha256,
      startedAt: record.createdAt, updatedAt: now(), completedAt: null,
      progress: {completed: 0, total: 0}, qualitySummary: {sampleCount: 0, invalidSampleCount: 0, errorCount: 0},
      log: [], error: null, frequencyRecovery: frequencyRecoverySummary(record.frequencyState),
    };
  }
  async reconcileRecoveries() {
    if (this.disposed || abortRequested(this.recoveryControl)) return;
    let entries;
    try { entries = fs.readdirSync(this.runsRoot, {withFileTypes: true}); } catch { return; }
    const recoveries = [];
    for (const entry of entries) {
      if (!entry.isDirectory() || !RUN_ID.test(entry.name) || this.active.has(entry.name)) continue;
      const recoveryPath = this.recoveryPath(entry.name);
      if (!fs.existsSync(recoveryPath)) {
        try {
          const run = this.load(entry.name);
          if (ACTIVE.has(run.status)) this.recoveryFailure(run, [{phase: 'recovery', code: 'CALIBRATION_RECOVERY_RECORD_MISSING',
            message: '上次校准异常中断且没有可用的频率恢复记录。'}], {interrupted: true});
        } catch {}
        continue;
      }
      try {
        const record = safeRecoveryRecord(JSON.parse(fs.readFileSync(recoveryPath, 'utf8')), entry.name);
        let modifiedMs = 0;
        try { modifiedMs = fs.statSync(recoveryPath).mtimeMs; } catch {}
        recoveries.push({record, modifiedMs});
      } catch (error) {
        try {
          const run = this.load(entry.name);
          this.recoveryFailure(run, [{phase: 'recovery', code: error.code || 'CALIBRATION_RECOVERY_INVALID',
            message: error.message || '遗留锁频恢复记录无效。'}], {interrupted: ACTIVE.has(run.status)});
        } catch {}
      }
    }
    // Every armed frequency recovery is safety-critical and is processed even
    // when more than MAX_RECOVERY_RUNS artifact-only retries have accumulated.
    const byNewest = (left, right) => right.modifiedMs - left.modifiedMs;
    const pending = recoveries.filter(item => item.record.frequencyRestorePending).sort(byNewest);
    const artifactOnly = recoveries.filter(item => !item.record.frequencyRestorePending).sort(byNewest).slice(0, MAX_RECOVERY_RUNS);
    const work = [...pending, ...artifactOnly];
    const deviceLists = new Map();
    for (const item of work) {
      if (this.disposed || abortRequested(this.recoveryControl)) return;
      let recovery = item.record;
      const run = this.recoveryRun(recovery);
      try {
        const frequencyRecoveryRequired = recovery.frequencyRestorePending;
        run.frequencyRecovery = frequencyRecoverySummary(recovery.frequencyState, run.frequencyRecovery);
        if (!deviceLists.has(recovery.hdcPath))
          deviceLists.set(recovery.hdcPath, this.discoverRecoveryDevices(recovery.hdcPath, this.recoveryControl));
        const devices = await deviceLists.get(recovery.hdcPath);
        if (this.disposed || abortRequested(this.recoveryControl)) return;
        const serial = devices.find(value => sha256(value) === recovery.deviceSerialSha256);
        if (!serial) throw calibrationError('CALIBRATION_RECOVERY_DEVICE_OFFLINE', '遗留锁频对应设备当前未连接；平台将在下次启动时继续恢复。');
        if (frequencyRecoveryRequired) {
          const errors = await this.restoreFrequencies(recovery.hdcPath, serial, recovery.frequencyState, this.recoveryControl);
          if (this.disposed || abortRequested(this.recoveryControl)) return;
          if (errors.length) {
            await this.cleanupRemoteBinary(recovery.hdcPath, serial, recovery.remoteBinary, this.recoveryControl);
            if (this.disposed || abortRequested(this.recoveryControl)) return;
            this.recoveryFailure(run, errors, {interrupted: ACTIVE.has(run.status)});
            continue;
          }
          try { recovery = this.markRecoveryFrequencyRestored(recovery.runId); }
          catch (error) {
            this.recoveryFailure(run, [{phase: 'recovery_checkpoint', code: error.code || 'CALIBRATION_RECOVERY_CHECKPOINT_FAILED',
              message: '频率边界已恢复，但恢复日志无法持久化安全状态；平台将重试核验。'}],
            {interrupted: ACTIVE.has(run.status)});
            continue;
          }
        }
        const cleanupResults = await this.cleanupRemoteBinary(recovery.hdcPath, serial, recovery.remoteBinary, this.recoveryControl);
        if (this.disposed || abortRequested(this.recoveryControl)) return;
        const warnings = cleanupRows(cleanupResults);
        run.frequencyRecovery = {...frequencyRecoverySummary(recovery.frequencyState, run.frequencyRecovery), cleanupPending: false,
          cleanupErrors: [], restoredAt: recovery.frequencyRestoredAt || now()};
        run.cleanupErrors = [];
        if (ACTIVE.has(run.status)) {
          run.status = 'failed'; run.stage = 'recovered'; run.completedAt = now();
          run.error = {code: 'CALIBRATION_RUN_INTERRUPTED_RECOVERED',
            message: '上次校准异常中断；平台已在本次启动时恢复设备频率边界，请重新执行校准。', details: {}};
        } else run.stage = run.status === 'cancelled' ? 'cancelled' : 'completed';
        run.updatedAt = now();
        run.cleanupWarnings = warnings;
        this.save(run);
        if (!warnings.length) {
          try { this.clearRecovery(run); }
          catch (error) {
            run.cleanupWarnings = cleanupRows([{phase: 'recovery_record_cleanup',
              code: error.code || 'CALIBRATION_RECOVERY_RECORD_CLEANUP_FAILED', message: error.message || String(error)}]);
            run.updatedAt = now(); this.save(run);
          }
        }
      } catch (error) {
        if (this.disposed || abortRequested(this.recoveryControl)) return;
        if (!recovery.frequencyRestorePending) {
          run.frequencyRecovery = {...frequencyRecoverySummary(recovery.frequencyState, run.frequencyRecovery), cleanupPending: false,
            cleanupErrors: [], restoredAt: recovery.frequencyRestoredAt || run.frequencyRecovery?.restoredAt || now()};
          run.cleanupWarnings = cleanupRows([{phase: 'artifact_cleanup_retry',
            code: error.code || 'CALIBRATION_ARTIFACT_CLEANUP_RETRY_FAILED', message: error.message || String(error)}]);
          run.updatedAt = now(); this.save(run);
        } else this.recoveryFailure(run,
          [{phase: 'recovery', code: error.code || 'CALIBRATION_RECOVERY_FAILED', message: error.message || String(error)}],
          {interrupted: ACTIVE.has(run.status)});
      }
    }
  }
  async hdc(hdcPath, serial, args, {timeoutMs = 30000, control = null, allowFailure = false} = {}) {
    if (!Array.isArray(args) || args.some(value => typeof value !== 'string' || value.includes('\0')))
      throw calibrationError('CALIBRATION_HDC_INVALID', '校准 HDC 参数无效。');
    assertControlActive(control);
    return new Promise((resolve, reject) => {
      let settled = false; let child;
      const finish = (error, stdout = '', stderr = '') => {
        if (settled) return; settled = true;
        if (control?.child === child) control.child = null;
        if (abortRequested(control))
          return reject(calibrationError('CALIBRATION_OPERATION_ABORTED', '校准后台真机命令已停止。'));
        const processCode = error ? safeProcessErrorCode(error) : '0';
        if (error && !allowFailure) reject(calibrationError('CALIBRATION_HDC_FAILED',
          `真机校准命令失败（HDC ${processCode}）${String(stderr).trim() ? `；${redactDevice(String(stderr).trim(), serial).slice(-1000)}` : ''}`));
        else resolve({exitCode: error ? processCode : 0, stdout: String(stdout), stderr: String(stderr)});
      };
      try {
        child = this.execFile(hdcPath, ['-t', serial, ...args], {timeout: timeoutMs, maxBuffer: MAX_OUTPUT_BYTES, windowsHide: true}, finish);
        if (control && !settled) control.child = child;
        if (abortRequested(control) && !settled) { try { child?.kill(); } catch {} }
      } catch (error) { finish(error); }
    });
  }
  async shell(hdcPath, serial, command, options = {}) {
    return this.hdc(hdcPath, serial, ['shell', command], options);
  }
  async frequencyState(hdcPath, serial, groups, control) {
    const result = {};
    for (const group of groups.values()) {
      const cpu = group.cpus[0];
      const policy = group.sourcePolicyId;
      const preferred = policy?.startsWith('policy')
        ? `/sys/devices/system/cpu/cpufreq/${policy}`
        : policy?.startsWith('cpu') ? `/sys/devices/system/cpu/${policy}/cpufreq` : null;
      const fallback = `/sys/devices/system/cpu/cpu${cpu}/cpufreq`;
      const candidates = [...new Set([preferred, fallback].filter(Boolean))];
      const selection = candidates.map((candidate, index) => `${index ? 'elif' : 'if'} { ${cpuListGuard(`'${candidate}'`, cpu,
        {allowPerCpuIdentity: candidate === fallback})}; }; then selected="$base"`).join('; ') + '; else exit 41; fi';
      const command = `${selection}; base=$selected; printf 'base=%s\\n' "$base"; printf 'min='; cat "$base/scaling_min_freq" 2>/dev/null; printf 'max='; cat "$base/scaling_max_freq" 2>/dev/null; if [ -w "$base/scaling_min_freq" ] && [ -w "$base/scaling_max_freq" ]; then printf 'writable=1\\n'; else printf 'writable=0\\n'; fi`;
      const response = await this.shell(hdcPath, serial, command, {control});
      const base = response.stdout.match(/base=(\/sys\/devices\/system\/cpu\/(?:cpufreq\/policy\d{1,4}|cpu\d{1,4}\/cpufreq))/)?.[1];
      const minimum = Number(response.stdout.match(/min=(\d+)/)?.[1]);
      const maximum = Number(response.stdout.match(/max=(\d+)/)?.[1]);
      const writable = response.stdout.match(/writable=(\d+)/)?.[1] === '1';
      if (!base || !CPUFREQ_BASE.test(base) || !Number.isSafeInteger(minimum) || !Number.isSafeInteger(maximum) || minimum < 1 || maximum < minimum)
        throw calibrationError('CALIBRATION_FREQUENCY_STATE_UNAVAILABLE', `无法读取 ${group.id} 的 cpufreq 边界。`);
      result[group.id] = {cpu, base, minimum, maximum, writable, sourcePolicyId: cpufreqTokenFromBase(base)};
    }
    const blocked = Object.entries(result).filter(([, value]) => !value.writable).map(([id]) => id);
    if (blocked.length) throw calibrationError('CALIBRATION_FREQUENCY_CONTROL_UNAVAILABLE',
      `当前设备不允许平台锁定 ${blocked.join('、')} 的 OPP；频率扫描已阻断，未执行或伪造实测。`, {blocked_core_groups: blocked});
    return result;
  }
  async setFrequency(hdcPath, serial, state, groupId, frequency, control) {
    const item = state[groupId];
    if (!item) throw calibrationError('CALIBRATION_FREQUENCY_STATE_UNAVAILABLE', `缺少 ${groupId} 频率域状态。`);
    const value = strictInteger(frequency, '目标频点', 1, 100000000);
    const guard = cpuListGuard(`'${item.base}'`, item.cpu, {allowPerCpuIdentity: item.base === `/sys/devices/system/cpu/cpu${item.cpu}/cpufreq`});
    const command = `${guard} || exit 41; current=$(cat "$base/scaling_max_freq") || exit 20; if [ "$current" -lt ${value} ]; then printf '%s' ${value} > "$base/scaling_max_freq" && printf '%s' ${value} > "$base/scaling_min_freq"; else printf '%s' ${value} > "$base/scaling_min_freq" && printf '%s' ${value} > "$base/scaling_max_freq"; fi; test "$(cat "$base/scaling_min_freq")" = ${value} && test "$(cat "$base/scaling_max_freq")" = ${value}`;
    await this.shell(hdcPath, serial, command, {control});
  }
  async restoreFrequencies(hdcPath, serial, state, control) {
    const errors = [];
    for (const [coreGroupId, item] of Object.entries(state || {})) {
      assertControlActive(control);
      const guard = cpuListGuard(`'${item.base}'`, item.cpu, {allowPerCpuIdentity: item.base === `/sys/devices/system/cpu/cpu${item.cpu}/cpufreq`});
      const command = `${guard} || exit 41; current=$(cat "$base/scaling_max_freq" 2>/dev/null || printf 0); if [ "$current" -lt ${item.maximum} ]; then printf '%s' ${item.maximum} > "$base/scaling_max_freq" && printf '%s' ${item.minimum} > "$base/scaling_min_freq"; else printf '%s' ${item.minimum} > "$base/scaling_min_freq" && printf '%s' ${item.maximum} > "$base/scaling_max_freq"; fi; test "$(cat "$base/scaling_min_freq")" = ${item.minimum} && test "$(cat "$base/scaling_max_freq")" = ${item.maximum}`;
      try { await this.shell(hdcPath, serial, command, {control}); }
      catch (error) {
        if (abortRequested(control)) throw error;
        errors.push({phase: 'frequency_restore', coreGroupId, code: error.code || 'CALIBRATION_FREQUENCY_RESTORE_FAILED', message: error.message || String(error)});
      }
    }
    return errors;
  }
  async cleanupRemoteBinary(hdcPath, serial, remoteBinary, control) {
    const errors = [];
    if (remoteBinary) {
      assertControlActive(control);
      const match = String(remoteBinary).match(REMOTE_BINARY);
      if (!match) return [{phase: 'remote_cleanup', code: 'CALIBRATION_REMOTE_PATH_INVALID', message: '微基准远端路径无效。'}];
      // Anchor the process command line at an exact executable token. A legacy
      // hash-only retry must not match a newer run-specific path sharing the
      // same binary hash prefix.
      const remotePattern = `(^|[[:space:]])${REMOTE_ROOT}/[h]${path.posix.basename(remoteBinary).slice(1)}([[:space:]]|$)`;
      try { await this.shell(hdcPath, serial, `pkill -f '${remotePattern}' >/dev/null 2>&1 || true; rm -f '${remoteBinary}'`, {control}); }
      catch (error) {
        if (abortRequested(control)) throw error;
        errors.push({phase: 'remote_cleanup', code: error.code || 'CALIBRATION_REMOTE_CLEANUP_FAILED', message: error.message || String(error)});
      }
    }
    return errors;
  }
  async thermalSnapshot(hdcPath, serial, control) {
    // HarmonyOS recovery/user shells are often BusyBox-like and may not ship
    // `tr`. Each sysfs attribute is one line, so shell built-in `read` gives a
    // deterministic record without adding an external command dependency.
    const command = "for zone in /sys/class/thermal/thermal_zone*; do [ -d \"$zone\" ] || continue; type=''; temperature=''; IFS= read -r type < \"$zone/type\" 2>/dev/null || true; IFS= read -r temperature < \"$zone/temp\" 2>/dev/null || true; printf '%s\\t%s\\t%s\\n' \"${zone##*/}\" \"$type\" \"$temperature\"; done";
    const response = await this.shell(hdcPath, serial, command, {control, allowFailure: true}).catch(() => null);
    if (!response || response.exitCode !== 0) return [];
    return response.stdout.split(/\r?\n/).slice(0, 128).map(line => line.trim()).filter(Boolean).map(line => {
      const [zone, type, rawTemperature] = line.split('\t');
      const temperature = Number(String(rawTemperature || '').trim());
      return {zone: String(zone || '').slice(0, 80), type: String(type || '').trim().slice(0, 120),
        temperature_raw: Number.isFinite(temperature) ? temperature : null};
    }).filter(row => /^thermal_zone\d+$/.test(row.zone) && row.type && Number.isFinite(row.temperature_raw));
  }
  async start({plan, hardwareProfile, deviceSerial, hdcPath = 'hdc'} = {}) {
    await this.initialReady;
    if (this.disposed) throw calibrationError('CALIBRATION_MANAGER_DISPOSED', '校准执行器已经关闭，请重新打开工作台后再试。');
    const input = normalizeInputs({plan, hardwareProfile, deviceSerial});
    await this.queueRecoveryReconcile();
    if (this.disposed) throw calibrationError('CALIBRATION_MANAGER_DISPOSED', '校准执行器已经关闭，请重新打开工作台后再试。');
    const pendingRecovery = this.pendingRecoveryFor(sha256(input.deviceSerial));
    if (pendingRecovery) throw calibrationError('CALIBRATION_RECOVERY_PENDING',
      '该设备仍有未完成的频率恢复；平台已尝试恢复但尚未成功，请保持设备连接并重试。', {runId: pendingRecovery});
    if ([...this.active.values()].some(record => record.deviceSerial === input.deviceSerial))
      throw calibrationError('CALIBRATION_DEVICE_BUSY', '所选设备已有校准任务在执行。');
    const bundle = resolveBenchmarkBundle({resourcesDir: this.resourcesDir, resourcesPath: this.resourcesPath});
    const runId = `cal_${crypto.randomBytes(16).toString('hex')}`;
    const run = {schemaVersion: 'desktop_calibration_run/v1', runId, status: 'preparing', stage: 'readiness',
      planId: input.plan.id, hardwareProfileId: input.hardwareProfile.id,
      deviceSerialSha256: sha256(input.deviceSerial), startedAt: now(), updatedAt: now(), completedAt: null,
      progress: {completed: 0, total: input.conditions.reduce((sum, condition) => sum + condition.repeats, 0), attempted: 0, retries: 0},
      qualitySummary: {sampleCount: 0, invalidSampleCount: 0, errorCount: 0}, log: [], error: null,
      plan: input.plan, hardwareProfile: input.hardwareProfile, measurementPath: this.measurementsPath(runId)};
    this.save(run);
    const control = {cancelled: false, child: null, deviceSerial: input.deviceSerial, run};
    this.active.set(runId, control);
    control.promise = this.execute(run, input, bundle, safeHdcPath(hdcPath), control)
      .finally(() => {
        this.active.delete(runId);
        if (control.run.cleanupWarnings?.length) void this.requestRecoveryReconcile().catch(() => {});
      });
    return publicRun(run);
  }
  async execute(run, input, bundle, hdcPath, control) {
    const remoteBinary = `${REMOTE_ROOT}/hetero_calibration_${bundle.manifest.binary_sha256.slice(0, 16)}_${run.runId}`;
    let frequencyState = null;
    let finalStatus = null;
    let finalError = null;
    const measurements = {schema_version: 'parallel_calibration_measurements/v1', plan_id: input.plan.id,
      hardware_profile_id: input.hardwareProfile.id,
      provenance: {device_id: run.deviceSerialSha256, os_build: 'unknown', collector: 'desktop_calibration_runner/1.0.0',
        measured_at: now(), source_kind: 'measured', binary_sha256: bundle.manifest.binary_sha256,
        plan_sha256: sha256(Buffer.from(JSON.stringify(input.plan))), cpu_map: {}, observer_cpu: null,
        thermal_evidence: '平台保存 thermal zone 原始读数，并锁定 scaling_min_freq/scaling_max_freq；只有目标逻辑 CPU 的 cpuinfo_cur_freq 在驻留预热和正式窗口均通过 ±3% 核验时，才记录“未观测到有效热降频”，不据此声称设备温度正常。'},
      samples: [], collection_errors: [], invalid_samples: [], collection_status: 'running',
      retry_policy: {schema_version: 'calibration_bounded_retry/v1', trigger: 'sibling_or_target_frequency_evidence', max_attempts_per_repeat: 3},
      retry_summary: {attempt_count: 0, retry_count: 0, recovered_repeat_count: 0, exhausted_repeat_count: 0}};
    try {
      const executionConditions = deterministicExecutionOrder(input.plan.id, input.conditions);
      measurements.provenance.execution_order = executionConditions.map(condition => condition.id);
      measurements.provenance.execution_order_policy = 'deterministic_sha256_plan_condition_interleave_v1';
      const observer = chooseObserver(input.conditions, input.groups);
      measurements.provenance.observer_cpu = observer;
      for (const [id, group] of input.groups) measurements.provenance.cpu_map[id] = group.cpus;
      // The full uname output contains the node name on some devices. Keep provenance to
      // OS/kernel/architecture fields and never persist a device hostname.
      const osBuild = await this.shell(hdcPath, input.deviceSerial,
        'param get const.ohos.fullname 2>/dev/null || uname -s -r -m', {control});
      measurements.provenance.os_build = osBuild.stdout.trim().split(/\r?\n/).filter(Boolean).at(-1)?.slice(0, 500) || 'unknown';
      frequencyState = await this.frequencyState(hdcPath, input.deviceSerial, input.groups, control);
      if (control.cancelled) throw calibrationError('CALIBRATION_CANCELLED', '校准已取消。');
      this.saveRecovery(run, {hdcPath, remoteBinary, frequencyState});
      run.stage = 'deploying'; run.updatedAt = now(); run.log.push({at: now(), message: '频率控制能力已核验，正在部署受管微基准。'}); this.save(run);
      await this.hdc(hdcPath, input.deviceSerial, ['file', 'send', bundle.binary, remoteBinary], {timeoutMs: 120000, control});
      await this.shell(hdcPath, input.deviceSerial, `chmod 700 ${remoteBinary} && ${remoteBinary} --help >/dev/null 2>&1 || test $? -eq 2`, {control});
      run.status = 'running'; run.stage = 'measuring'; run.updatedAt = now(); this.save(run);
      let executionSampleIndex = 0;
      let unfulfilledRepeatCount = 0;
      const executionSampleCount = input.conditions.reduce((sum, condition) => sum + condition.repeats, 0);
      for (const condition of executionConditions) {
        if (control.cancelled) throw calibrationError('CALIBRATION_CANCELLED', '校准已取消。');
        const placement = placementsFor(condition, input.groups, observer);
        await this.setFrequency(hdcPath, input.deviceSerial, frequencyState, condition.core_kind, condition.frequency_khz, control);
        if (condition.benchmark === 'pair_work' && condition.active_tasks === 2 && condition.peer_core_kind !== condition.core_kind)
          await this.setFrequency(hdcPath, input.deviceSerial, frequencyState, condition.peer_core_kind, condition.peer_frequency_khz, control);
        for (let repeat = 0; repeat < condition.repeats; repeat += 1) {
          let repeatSatisfied = false;
          let repeatRetried = false;
          let lastCooldownMs = 500;
          for (let attempt = 0; attempt < measurements.retry_policy.max_attempts_per_repeat; attempt += 1) {
            if (control.cancelled) throw calibrationError('CALIBRATION_CANCELLED', '校准已取消。');
            const args = benchmarkArguments(remoteBinary, condition, placement, frequencyState);
            const command = args.join(' ');
            const runIdentity = `run_${crypto.randomUUID().replaceAll('-', '')}`;
            const sampleIdentity = `sample_${crypto.randomUUID().replaceAll('-', '')}`;
            let retryScheduled = false;
            try {
              const thermalBefore = await this.thermalSnapshot(hdcPath, input.deviceSerial, control);
              if (control.cancelled) throw calibrationError('CALIBRATION_CANCELLED', '校准已取消。');
              const response = await this.shell(hdcPath, input.deviceSerial, command, {timeoutMs: 5 * 60 * 1000, control});
              if (control.cancelled) throw calibrationError('CALIBRATION_CANCELLED', '校准已取消。');
              const thermalAfter = await this.thermalSnapshot(hdcPath, input.deviceSerial, control);
              if (control.cancelled) throw calibrationError('CALIBRATION_CANCELLED', '校准已取消。');
              const line = response.stdout.split(/\r?\n/).find(value => value.trim().startsWith('{') && value.includes('parallel_calibration_measurement/v1'));
              if (!line) throw calibrationError('CALIBRATION_MEASUREMENT_INVALID', '微基准没有返回可识别的测量 JSON。');
              const measured = JSON.parse(line);
              validatePairedDispatchMeasurement(measured);
              const siblingQuietness = validateTargetSiblingQuietnessMeasurement(measured);
              for (const field of ['benchmark', 'family', 'frequency_khz', 'active_tasks', 'work_units'])
                if (measured[field] !== condition[field]) throw calibrationError('CALIBRATION_MEASUREMENT_MISMATCH', `实测 ${field} 与冻结方案不一致。`);
              if (measured.cpu !== placement.target || measured.observer_cpu !== placement.observer)
                throw calibrationError('CALIBRATION_MEASUREMENT_MISMATCH', '微基准实际目标 CPU 或观测 CPU 与冻结放置不一致。');
              if (measured.frequency_policy_id !== frequencyState[condition.core_kind].sourcePolicyId)
                throw calibrationError('CALIBRATION_MEASUREMENT_MISMATCH', '微基准频率采样源与执行器核验的 cpufreq 频率域不一致。');
              if (condition.benchmark === 'pair_work' && condition.active_tasks === 2
                  && measured.peer_frequency_policy_id !== frequencyState[condition.peer_core_kind].sourcePolicyId)
                throw calibrationError('CALIBRATION_MEASUREMENT_MISMATCH', '共跑微基准的频率采样源与执行器核验的 cpufreq 频率域不一致。');
              if (condition.benchmark === 'pair_work' && condition.active_tasks === 2)
                validatePairHelperPlacementMeasurement(measured, placement.helpers[0]);
              const frequencyEvidence = validateFrequencyEvidenceMeasurement(measured, {
                expectedCpu: placement.target,
                expectedPolicyId: frequencyState[condition.core_kind].sourcePolicyId,
                peerCpu: condition.benchmark === 'pair_work' && condition.active_tasks === 2
                  ? placement.helpers[0] : null,
                peerPolicyId: condition.benchmark === 'pair_work' && condition.active_tasks === 2
                  ? frequencyState[condition.peer_core_kind].sourcePolicyId : null,
              });
              if (condition.benchmark === 'roofline') {
                for (const field of ['workload_class', 'working_set_bytes', 'roofline_operations_per_element', 'roofline_kernel_version'])
                  if (measured[field] !== condition[field]) throw calibrationError('CALIBRATION_MEASUREMENT_MISMATCH', `实测 ${field} 与冻结 Roofline 方案不一致。`);
              }
              const sample = {...measured,
                ...Object.fromEntries(['benchmark', 'family', 'workload_id', 'core_kind', 'frequency_khz', 'active_tasks', 'work_units', 'split'].map(key => [key, condition[key]])),
                sample_id: sampleIdentity, run_id: runIdentity, condition_id: condition.id, repeat,
                collection_attempt: attempt + 1, max_collection_attempts: measurements.retry_policy.max_attempts_per_repeat,
                data_kind: 'measured', observed_frequency_khz: measured.frequency_mean_khz,
                observed_frequency_min_khz: measured.frequency_min_khz, observed_frequency_max_khz: measured.frequency_max_khz,
                thermal_throttled: measured.frequency_verified === true ? false : null,
                thermal_note: measured.frequency_verified === true
                  ? '目标窗口频率锁定且观测频率通过 ±3% 校验，未观测到有效热降频；该结论不代表温度正常。'
                  : '目标窗口频率未通过核验，无法判断是否发生有效热降频。',
                thermal_snapshot_before: thermalBefore, thermal_snapshot_after: thermalAfter,
                raw_sha256: sha256(Buffer.from(line)), binary_sha256: bundle.manifest.binary_sha256,
                measured_at: now(), command: args.slice(1)};
              if (condition.benchmark === 'pair_work') {
                sample.peer_core_kind = condition.peer_core_kind;
                // These are source-level integer/byte semantics frozen by the
                // plan, not hardware FLOPs, retired instructions or DRAM
                // traffic. Preserve them beside timing evidence for audit.
                for (const field of ['workload_intensity', 'peer_workload_intensity'])
                  if (condition[field]) sample[field] = JSON.parse(JSON.stringify(condition[field]));
              }
              const rawRise = thermalRawRise(thermalBefore, thermalAfter);
              const rawScaleThreshold = [...thermalBefore, ...thermalAfter].some(row => Math.abs(row.temperature_raw || 0) >= 1000) ? 2000 : 2;
              lastCooldownMs = rawRise !== null && rawRise >= rawScaleThreshold ? 1500 : 500;
              sample.thermal_raw_max_rise = rawRise;
              sample.cooldown_after_ms = lastCooldownMs;
              measurements.samples.push(sample);
              const reasons = [];
              for (const field of ['affinity_verified', 'physical_cores_verified', 'observer_independent_core', 'frequency_verified'])
                if (sample[field] !== true) reasons.push(`${field} 未核验`);
              const siblingInvalid = !siblingQuietness.activityVerified || !siblingQuietness.quietnessVerified;
              const frequencyInvalid = !frequencyEvidence.target.verified
                || (frequencyEvidence.peer !== null && !frequencyEvidence.peer.verified);
              if (!siblingQuietness.activityVerified) reasons.push('目标物理核同胞逻辑线程活动证据未核验');
              else if (!siblingQuietness.quietnessVerified)
                reasons.push(`目标物理核同胞逻辑线程忙碌占比 ${(siblingQuietness.busyFraction * 100).toFixed(2)}% 超过 5%`);
              if (frequencyEvidence.target.source !== 'cpuinfo_cur_freq')
                reasons.push('目标频率仅有 scaling_cur_freq fallback 诊断证据');
              else if (!frequencyEvidence.target.warmupVerified)
                reasons.push('目标 CPU 在有界预热内未连续 3 次驻留目标 OPP ±3%');
              else if (!frequencyEvidence.target.verified)
                reasons.push(`目标 CPU 实际频率窗口未通过 ±3% 核验（${frequencyEvidence.target.minimum ?? '无'}–${frequencyEvidence.target.maximum ?? '无'} kHz）`);
              if (frequencyEvidence.peer !== null && !frequencyEvidence.peer.verified)
                reasons.push('共跑 helper 的实际 CPU 频率窗口未通过 per-CPU ±3% 核验');
              if (sample.active_tasks > 1 && sample.concurrency_verified !== true) reasons.push('共跑覆盖未核验');
              if (sample.benchmark === 'roofline' && sample.working_set_coverage_verified !== true) reasons.push('Roofline 工作集覆盖未核验');
              if (sample.thermal_throttled !== false) reasons.push('目标窗口频率不稳定');
              retryScheduled = reasons.length > 0 && (siblingInvalid || frequencyInvalid)
                && attempt + 1 < measurements.retry_policy.max_attempts_per_repeat;
              if (reasons.length) measurements.invalid_samples.push({sample_id: sampleIdentity, condition_id: condition.id,
                repeat, collection_attempt: attempt + 1, retry_scheduled: retryScheduled,
                target_sibling_busy_fraction: siblingQuietness.busyFraction,
                frequency_source: frequencyEvidence.target.source,
                frequency_source_cpu: frequencyEvidence.target.sourceCpu,
                frequency_source_path: frequencyEvidence.target.sourcePath,
                warmup_frequency_verified: frequencyEvidence.target.warmupVerified,
                warmup_frequency_sample_count: sample.warmup_frequency_sample_count,
                warmup_frequency_in_range_consecutive: sample.warmup_frequency_in_range_consecutive,
                observed_frequency_min_khz: frequencyEvidence.target.minimum,
                observed_frequency_max_khz: frequencyEvidence.target.maximum,
                ...(frequencyEvidence.peer ? {
                  peer_frequency_source: frequencyEvidence.peer.source,
                  peer_frequency_source_cpu: frequencyEvidence.peer.sourceCpu,
                  peer_frequency_source_path: frequencyEvidence.peer.sourcePath,
                  peer_observed_frequency_min_khz: frequencyEvidence.peer.minimum,
                  peer_observed_frequency_max_khz: frequencyEvidence.peer.maximum,
                } : {}),
                reasons});
              else repeatSatisfied = true;
            } catch (error) {
              if (control.cancelled) throw calibrationError('CALIBRATION_CANCELLED', '校准已取消。');
              measurements.collection_errors.push({condition_id: condition.id, run_id: runIdentity, repeat,
                collection_attempt: attempt + 1, code: error.code || 'CALIBRATION_MEASUREMENT_FAILED',
                error: String(error.message || error).slice(0, 2000)});
            }
            run.progress.attempted += 1;
            measurements.retry_summary.attempt_count += 1;
            if (retryScheduled) {
              repeatRetried = true;
              run.progress.retries += 1;
              measurements.retry_summary.retry_count += 1;
              run.log.push({at: now(), message: `条件 ${condition.id} 第 ${repeat + 1} 个重复未通过同胞线程或实际频率质量门，正在执行第 ${attempt + 2} 次有界补测。`});
            }
            run.qualitySummary = {sampleCount: measurements.samples.length,
              invalidSampleCount: measurements.invalid_samples.length, errorCount: measurements.collection_errors.length};
            run.updatedAt = now();
            atomicJson(run.measurementPath, measurements); this.save(run);
            if (retryScheduled && !control.cancelled) {
              await this.sleep(lastCooldownMs);
              continue;
            }
            break;
          }
          if (repeatSatisfied && repeatRetried) measurements.retry_summary.recovered_repeat_count += 1;
          if (!repeatSatisfied) {
            unfulfilledRepeatCount += 1;
            measurements.retry_summary.exhausted_repeat_count += 1;
          }
          run.progress.completed += 1;
          executionSampleIndex += 1;
          run.qualitySummary = {sampleCount: measurements.samples.length,
            invalidSampleCount: measurements.invalid_samples.length, errorCount: measurements.collection_errors.length};
          run.updatedAt = now();
          atomicJson(run.measurementPath, measurements); this.save(run);
          if (executionSampleIndex < executionSampleCount && !control.cancelled) await this.sleep(lastCooldownMs);
        }
      }
      measurements.unfulfilled_repeat_count = unfulfilledRepeatCount;
      measurements.collection_status = measurements.collection_errors.length || unfulfilledRepeatCount ? 'partial' : 'completed';
      finalStatus = measurements.collection_status === 'completed' ? 'succeeded' : 'partial';
      atomicJson(run.measurementPath, measurements);
    } catch (error) {
      finalStatus = error.code === 'CALIBRATION_CANCELLED' || control.cancelled ? 'cancelled' : 'failed';
      finalError = {code: error.code || 'CALIBRATION_RUN_FAILED', message: error.message || String(error), details: error.details || {}};
      measurements.collection_status = finalStatus;
      atomicJson(run.measurementPath, measurements);
    } finally {
      if (!finalStatus) {
        finalStatus = control.cancelled ? 'cancelled' : 'failed';
        finalError = {code: control.cancelled ? 'CALIBRATION_CANCELLED' : 'CALIBRATION_RUN_FAILED',
          message: control.cancelled ? '校准已取消。' : '校准执行未生成最终状态。', details: {}};
        measurements.collection_status = finalStatus;
      }
      const applyTerminalState = () => {
        // A cancellation accepted while safety cleanup is in progress remains
        // visible as cancelled once cleanup finishes. A prior execution failure
        // retains its more useful diagnostic.
        if (control.cancelled && run.status === 'cancelling' && finalStatus !== 'failed') {
          finalStatus = 'cancelled';
          finalError = {code: 'CALIBRATION_CANCELLED', message: '校准已取消。', details: {}};
          measurements.collection_status = 'cancelled';
        }
        run.status = finalStatus;
        run.stage = finalStatus === 'succeeded' || finalStatus === 'partial' ? 'completed' : finalStatus;
        run.completedAt = now(); run.updatedAt = run.completedAt; run.error = finalError;
      };
      if (frequencyState) {
        run.stage = 'cleaning'; run.updatedAt = now(); this.save(run);
        const cleanupResults = await this.restoreFrequencies(hdcPath, input.deviceSerial, frequencyState, control);
        if (!blockingCleanupErrors(cleanupResults).length) {
          try { this.markRecoveryFrequencyRestored(run.runId); }
          catch (error) { cleanupResults.push({phase: 'recovery_checkpoint', code: error.code || 'CALIBRATION_RECOVERY_CHECKPOINT_FAILED',
            message: '频率边界已恢复，但恢复日志无法持久化安全状态；平台将重试核验。'}); }
        }
        cleanupResults.push(...await this.cleanupRemoteBinary(hdcPath, input.deviceSerial, remoteBinary, control));
        const cleanupErrors = blockingCleanupErrors(cleanupResults);
        const cleanupWarnings = cleanupRows(cleanupResults.filter(error => !blockingCleanupErrors([error]).length));
        applyTerminalState();
        if (cleanupErrors.length) {
          this.recoveryFailure(run, cleanupErrors);
          for (const error of run.cleanupErrors) measurements.collection_errors.push({condition_id: null, run_id: run.runId,
            repeat: null, code: error.code, error: `${error.phase}: ${error.message}`});
          measurements.collection_status = run.status === 'failed' || run.status === 'cancelled' ? run.status : 'partial';
        } else {
          run.frequencyRecovery = {...frequencyRecoverySummary(frequencyState, run.frequencyRecovery), cleanupPending: false,
            cleanupErrors: [], restoredAt: now()};
          run.cleanupErrors = [];
          run.cleanupWarnings = cleanupWarnings;
          if (cleanupWarnings.length) measurements.collection_warnings = cleanupWarnings.map(error => ({
            run_id: run.runId, code: error.code, warning: `${error.phase}: ${error.message}`,
          }));
        }
        run.qualitySummary = {sampleCount: measurements.samples.length, invalidSampleCount: measurements.invalid_samples.length,
          errorCount: measurements.collection_errors.length};
        atomicJson(run.measurementPath, measurements); this.save(run);
        if (!cleanupErrors.length && !cleanupWarnings.length) {
          try { this.clearRecovery(run); }
          catch (error) {
            run.cleanupWarnings = cleanupRows([{phase: 'recovery_record_cleanup',
              code: error.code || 'CALIBRATION_RECOVERY_RECORD_CLEANUP_FAILED', message: error.message || String(error)}]);
            run.updatedAt = now(); this.save(run);
          }
        }
      } else {
        applyTerminalState();
        run.qualitySummary = {sampleCount: measurements.samples.length, invalidSampleCount: measurements.invalid_samples.length,
          errorCount: measurements.collection_errors.length};
        atomicJson(run.measurementPath, measurements); this.save(run);
      }
    }
  }
  status({runId, reconcile = false} = {}) {
    const run = this.load(runId);
    if (reconcile && (run.frequencyRecovery?.cleanupPending || run.cleanupWarnings?.length) && !this.active.has(run.runId))
      void this.requestRecoveryReconcile().catch(() => {});
    return publicRun(run);
  }
  async cancel({runId} = {}) {
    const run = this.load(runId);
    if (FINAL.has(run.status)) return publicRun(run);
    const control = this.active.get(run.runId);
    if (!control) throw calibrationError('CALIBRATION_RUN_NOT_ACTIVE', '校准任务不在当前 Desktop 进程中运行。');
    control.cancelled = true;
    try { control.child?.kill(); } catch {}
    control.run.status = 'cancelling'; control.run.stage = 'cleaning'; control.run.updatedAt = now(); this.save(control.run);
    return publicRun(control.run);
  }
  measurements({runId} = {}) {
    const run = this.load(runId);
    if (ACTIVE.has(run.status)) throw calibrationError('CALIBRATION_RUN_ACTIVE', '校准仍在执行，尚未生成最终实测数据。');
    if (run.frequencyRecovery?.cleanupPending) throw calibrationError('CALIBRATION_CLEANUP_PENDING',
      '设备频率边界或微基准清理尚未完成；平台会在设备保持连接或下次启动时继续恢复，恢复前不会拟合或发布该数据。',
      {cleanupErrors: run.frequencyRecovery.cleanupErrors || []});
    let text;
    try { text = fs.readFileSync(this.measurementsPath(run.runId), 'utf8'); }
    catch { throw calibrationError('CALIBRATION_MEASUREMENTS_NOT_FOUND', '校准实测数据不存在。'); }
    const contents = Buffer.from(text);
    if (!contents.length || contents.length > 20 * 1024 * 1024) throw calibrationError('CALIBRATION_MEASUREMENTS_INVALID', '校准实测数据大小无效。');
    const value = JSON.parse(text);
    return {runId: run.runId, filename: `${run.runId}.measurements.json`, measurementText: text,
      measurementSha256: sha256(contents), measurementSizeBytes: contents.length,
      sampleCount: value.samples?.length || 0, invalidSampleCount: value.invalid_samples?.length || 0,
      errorCount: value.collection_errors?.length || 0};
  }
  async dispose() {
    if (this.disposed) return;
    this.disposed = true;
    const controls = [...this.active.values()];
    for (const control of controls) { control.cancelled = true; try { control.child?.kill(); } catch {} }
    this.recoveryControl.cancelled = true;
    try { this.recoveryControl.child?.kill(); } catch {}
    await Promise.allSettled(controls.map(control => control.promise).filter(Boolean));
    await Promise.allSettled([this.initialReady]);
  }
}

module.exports = {CalibrationManager, calibrationError, chooseObserver, deterministicExecutionOrder, normalizeInputs, parseHdcTargetSerials,
  placementsFor, resolveBenchmarkBundle, thermalRawRise, validateDeviceHardwareProfile, validatePairedDispatchMeasurement,
  validateFrequencyEvidenceMeasurement, validatePairHelperPlacementMeasurement,
  validateTargetSiblingQuietnessMeasurement};
