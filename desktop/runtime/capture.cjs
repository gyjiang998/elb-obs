'use strict';

const {EventEmitter} = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {spawn} = require('node:child_process');

const ACTIVE = new Set(['starting', 'running', 'cancelling']);
const TERMINAL = new Set(['succeeded', 'partial', 'failed', 'cancelled', 'interrupted']);
const SERIAL = /^[A-Za-z0-9_.:-]{1,160}$/;
const PLAN_ID = /^[A-Za-z0-9_.:-]{1,200}$/;
const CAPTURE_RUN_ID = /^capture_[a-f0-9]{24}$/;
const DESKTOP_RUN_ID = /^desktop_run_[a-f0-9]{32}$/;
const STEP_PATCH_FIELDS = new Set(['title', 'slug', 'operation', 'target', 'selector', 'expected_targets', 'key', 'seconds', 'points', 'velocity']);
const PLAN_UPDATE_HISTORY_LIMIT = 256;
const now = () => new Date().toISOString();
const copy = value => JSON.parse(JSON.stringify(value));
const id = prefix => `${prefix}_${crypto.randomUUID().replaceAll('-', '')}`;
function nextRevision(previous = '') {
  const current = Date.now();
  const prior = Date.parse(previous);
  return new Date(Number.isFinite(prior) && current <= prior ? prior + 1 : current).toISOString();
}
function canonicalJson(value) {
  const ancestors = new Set();
  const normalize = (item, arrayItem = false) => {
    if (item === undefined || typeof item === 'function' || typeof item === 'symbol') return arrayItem ? null : undefined;
    if (typeof item === 'bigint') throw new Error('计划补全参数包含不支持的值。');
    if (!item || typeof item !== 'object') return item;
    if (ancestors.has(item)) throw new Error('计划补全参数不能循环引用。');
    ancestors.add(item);
    let result;
    if (Array.isArray(item)) result = item.map(entry => normalize(entry, true));
    else {
      result = {};
      for (const key of Object.keys(item).sort()) {
        const normalized = normalize(item[key], false);
        if (normalized !== undefined) result[key] = normalized;
      }
    }
    ancestors.delete(item);
    return result;
  };
  return JSON.stringify(normalize(value));
}
function planUpdateFailureCode(error) {
  const message = String(error?.message || '');
  if (message.includes('迟到结果')) return 'stale_revision';
  if (message.includes('正在关闭')) return 'manager_closing';
  if (message.includes('不存在')) return 'not_found';
  if (message.includes('补丁') || message.includes('字段') || message.includes('字符串') || message.includes('字符')) return 'invalid_patch';
  return 'update_failed';
}
function planUpdateFailureMessage(code) {
  if (code === 'stale_revision') return '计划已在助手处理期间更新；迟到结果未覆盖新版本，请重新发起计划补全。';
  if (code === 'manager_closing') return '采集管理器正在关闭。';
  if (code === 'not_found') return '采集计划不存在，请从当前页面重新发起计划补全。';
  if (code === 'invalid_patch') return '计划补全补丁无效；该 requestId 已消费，请使用新的 requestId 修正后重试。';
  return '计划差量更新未能完成；该 requestId 已消费，请使用新的 requestId 重试。';
}
function inside(root, target) { const relative = path.relative(root, target); return relative === '' || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative)); }
function atomicJson(filename, value) {
  fs.mkdirSync(path.dirname(filename), {recursive: true});
  const temporary = `${filename}.${crypto.randomBytes(4).toString('hex')}.tmp`;
  try { fs.writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, {mode: 0o600}); fs.renameSync(temporary, filename); }
  finally { try { fs.unlinkSync(temporary); } catch {} }
}
function safeRelative(value) {
  if (typeof value !== 'string' || !value || value.includes('\\') || /[\x00-\x1f:]/.test(value) || value.startsWith('/') || value.split('/').some(part => !part || part === '.' || part === '..')) throw new Error('Trace 清单包含不安全的相对路径。');
  return value;
}
function fileIdentity(manifest) {
  return crypto.createHash('sha256').update(JSON.stringify({run_id: manifest.run_id, plan_id: manifest.plan_id, project_id: manifest.project_id || '', status: manifest.status, device_serial: manifest.device_serial || '', files: manifest.files.map(file => ({relative_path: file.relative_path, size_bytes: file.size_bytes, sha256: file.sha256})).sort((a, b) => a.relative_path.localeCompare(b.relative_path))})).digest('hex');
}
function describeError(value) {
  if (typeof value === 'string') return value.slice(0, 4000);
  if (!value || typeof value !== 'object') return '';
  return [value.step_title ? `失败位置：${value.step_title}` : '', value.reason || value.message || '', value.suggestion ? `建议：${value.suggestion}` : ''].filter(Boolean).join('；').slice(0, 4000);
}

class CaptureManager extends EventEmitter {
  constructor({userDataDir, runtimeRoot, pythonCommand = ['python3'], entryPath, launchOptions = {}, disposeGraceMs = 15000, commandTimeoutMs = 120000} = {}) {
    super();
    if (!userDataDir || !runtimeRoot) throw new Error('CaptureManager requires userDataDir and runtimeRoot');
    this.root = path.resolve(userDataDir, 'capture');
    this.runtimeRoot = path.resolve(runtimeRoot);
    this.entryPath = entryPath ? path.resolve(entryPath) : path.join(this.runtimeRoot, 'tools', 'scenario_capture', 'desktop_entry.py');
    this.pythonCommand = Array.isArray(pythonCommand) ? [...pythonCommand] : [pythonCommand];
    if (!this.pythonCommand.length || this.pythonCommand.some(value => typeof value !== 'string' || !value || value.includes('\0'))) throw new Error('pythonCommand 必须是可执行文件与参数组成的数组。');
    this.launchOptions = launchOptions;
    this.disposeGraceMs = Math.max(100, Number(disposeGraceMs) || 15000);
    this.commandTimeoutMs = Math.max(100, Number(commandTimeoutMs) || 120000);
    this.statePath = path.join(this.root, 'state.json');
    this.packagesRoot = path.join(this.root, 'packages');
    this.outputRoot = path.join(this.root, 'captures');
    this.jobsRoot = path.join(this.root, 'jobs');
    for (const directory of [this.root, this.packagesRoot, this.outputRoot, this.jobsRoot]) fs.mkdirSync(directory, {recursive: true});
    // macOS /var aliases /private/var; Python Path.resolve also canonicalizes
    // Windows junctions. Keep persisted paths and containment on the same basis.
    for (const key of ['root', 'packagesRoot', 'outputRoot', 'jobsRoot']) this[key] = fs.realpathSync(this[key]);
    this.statePath = path.join(this.root, 'state.json');
    this.state = {schemaVersion: 'desktop_capture/v1', selectedDeviceSerial: '', plans: [], runs: [], traces: [], planUpdateRequests: [], recoveryNotice: ''};
    this.devices = [];
    this.active = null;
    this.disposed = false;
    this.closing = false;
    this.children = new Set();
    this.persistTimer = null;
    this.catalogPromise = null;
    this.planUpdateQueues = new Map();
    this.planUpdateInFlight = new Map();
    // This set is intentionally process-local.  Membership proves that this
    // manager, during this startup, changed the corresponding Desktop journal
    // entry from active to interrupted.  A pre-existing interrupted/history
    // entry must never become collector-recovery authority merely by existing.
    this.startupInterruptedRunIds = new Set();
    this.runtime = {available: fs.existsSync(this.entryPath), entryPath: this.entryPath, diagnostic: fs.existsSync(this.entryPath) ? '' : '本地采集运行时尚未安装。'};
    this.load();
  }
  load() {
    try {
      const saved = JSON.parse(fs.readFileSync(this.statePath, 'utf8'));
      if (saved.schemaVersion !== 'desktop_capture/v1' || !Array.isArray(saved.plans) || !Array.isArray(saved.runs) || !Array.isArray(saved.traces)) throw new Error('采集索引格式无效');
      this.state = {...this.state, ...saved};
    } catch (error) {
      if (error.code !== 'ENOENT') {
        const backup = `${this.statePath}.unreadable-${Date.now()}`;
        try { fs.renameSync(this.statePath, backup); } catch {}
        this.state.recoveryNotice = `采集索引无法读取，原文件已保留：${backup}`;
      }
    }
    this.state.planUpdateRequests = (Array.isArray(this.state.planUpdateRequests) ? this.state.planUpdateRequests : [])
      .filter(entry => entry && typeof entry === 'object' && PLAN_ID.test(entry.requestId || '') && /^[a-f0-9]{64}$/.test(entry.fingerprint || '') && ['succeeded', 'failed'].includes(entry.status))
      .slice(0, PLAN_UPDATE_HISTORY_LIMIT)
      .map(entry => entry.status === 'succeeded'
        ? {requestId: entry.requestId, fingerprint: entry.fingerprint, planId: String(entry.planId || ''), status: 'succeeded', changed: entry.changed === true, updatedAt: String(entry.updatedAt || ''), readiness: copy(entry.readiness ?? null), completedAt: String(entry.completedAt || '')}
        : {requestId: entry.requestId, fingerprint: entry.fingerprint, planId: String(entry.planId || ''), status: 'failed', failureCode: String(entry.failureCode || 'update_failed'), completedAt: String(entry.completedAt || '')});
    const interrupted = this.state.runs.filter(run => ACTIVE.has(run.status));
    for (const run of interrupted) {
      if (this.exactInterruptedCaptureRunId(run, {requireActive: true})) this.startupInterruptedRunIds.add(run.runId);
      Object.assign(run, {status: 'interrupted', stage: 'recovered', finishedAt: now(), updatedAt: now(), message: '应用上次在采集期间退出。此记录已恢复为中断，保留原始目录，可登记已经生成的归档。'});
    }
    if (interrupted.length) this.state.recoveryNotice = `恢复了 ${interrupted.length} 条中断采集记录；未自动重跑或切换设备。`;
    this.persist();
  }
  persist() { if (this.persistTimer) { clearTimeout(this.persistTimer); this.persistTimer = null; } atomicJson(this.statePath, this.state); }
  schedulePersist() { if (!this.persistTimer) { this.persistTimer = setTimeout(() => { this.persistTimer = null; this.persist(); }, 150); this.persistTimer.unref(); } }
  notify(kind = 'state', extra = {}) { this.emit('event', {type: 'capture', kind, ...extra, ...(kind === 'state' ? {state: this.getState()} : {})}); }
  getState() {
    return copy({plans: this.state.plans, selectedDeviceSerial: this.state.selectedDeviceSerial, devices: this.devices, activeRun: this.active ? this.state.runs.find(run => run.runId === this.active.runId) || null : null, runs: this.state.runs, traces: this.state.traces.map(trace => this.traceSummary(trace)), runtime: this.runtime, recoveryNotice: this.state.recoveryNotice});
  }
  traceSummary(trace) { const {manifest, ...summary} = trace; return summary; }
  updateDevices({devices = []} = {}) {
    if (!Array.isArray(devices)) throw new Error('设备发现结果必须是数组。');
    this.devices = devices.map(device => ({...device, serial: String(device.serial || device.deviceSerial || device.device_serial || '')})).filter(device => SERIAL.test(device.serial));
    this.notify(); return this.getState();
  }
  selectDevice({serial = ''} = {}) {
    if (this.closing || this.disposed) throw new Error('采集管理器正在关闭。');
    if (serial && (typeof serial !== 'string' || !SERIAL.test(serial))) throw new Error('设备序列号无效。');
    if (this.active && serial !== this.state.selectedDeviceSerial) throw new Error('采集执行期间目标设备已冻结，请等待当前任务结束再切换。');
    if (serial && serial !== this.state.selectedDeviceSerial && !this.devices.some(device => device.serial === serial)) throw new Error('请选择本机设备发现列表中的手机。');
    this.state.selectedDeviceSerial = serial; this.persist(); this.notify(); return this.getState();
  }
  assertReady({allowClosing = false} = {}) {
    if (this.disposed || (this.closing && !allowClosing)) throw new Error('采集管理器正在关闭。');
    if (!fs.existsSync(this.entryPath)) throw new Error('本地采集运行时不存在，请重新安装应用。');
  }
  exactInterruptedCaptureRunId(run, {requireActive = false} = {}) {
    if (!run || typeof run !== 'object' || !DESKTOP_RUN_ID.test(run.runId || '')
        || (requireActive ? !ACTIVE.has(run.status) : run.status !== 'interrupted')
        || !CAPTURE_RUN_ID.test(run.captureRunId || '') || !SERIAL.test(run.deviceSerial || '')) return '';
    if (typeof run.outputRoot !== 'string' || path.resolve(run.outputRoot) !== this.outputRoot) return '';
    const expectedDirectory = path.join(this.outputRoot, 'scenario-capture', 'runs', run.captureRunId);
    if (typeof run.runDirectory !== 'string' || path.resolve(run.runDirectory) !== expectedDirectory) return '';
    const plan = run.planSnapshot;
    if (!plan || typeof plan !== 'object' || Array.isArray(plan)
        || typeof plan.project_id !== 'string' || !plan.project_id
        || typeof plan.artifact_id !== 'string' || !plan.artifact_id) return '';
    try {
      if (fs.realpathSync(run.outputRoot) !== this.outputRoot
          || fs.realpathSync(run.runDirectory) !== expectedDirectory
          || fs.lstatSync(run.runDirectory).isSymbolicLink()
          || !fs.statSync(run.runDirectory).isDirectory()) return '';
      const metadataPath = path.join(expectedDirectory, 'run.json');
      const metadataStat = fs.lstatSync(metadataPath);
      if (metadataStat.isSymbolicLink() || !metadataStat.isFile() || metadataStat.size > 8 * 1024 * 1024
          || fs.realpathSync(metadataPath) !== metadataPath) return '';
      const metadata = JSON.parse(fs.readFileSync(metadataPath, 'utf8'));
      if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)
          || metadata.schema_version !== 'scenario_capture_run/v1'
          || metadata.status !== 'running'
          || metadata.connection_mode !== 'local'
          || metadata.run_id !== run.captureRunId
          || metadata.device_serial !== run.deviceSerial
          || metadata.project_id !== plan.project_id
          || metadata.plan_id !== plan.artifact_id) return '';
      return run.captureRunId;
    } catch { return ''; }
  }
  recoverableDeviceRunIds(serial, projectId) {
    if (typeof projectId !== 'string' || !projectId) return [];
    const result = new Set();
    for (const run of this.state.runs) {
      if (!run || !this.startupInterruptedRunIds.has(run.runId) || run.deviceSerial !== serial
          || run.planSnapshot?.project_id !== projectId) continue;
      const candidate = this.exactInterruptedCaptureRunId(run);
      if (candidate) result.add(candidate);
    }
    return [...result];
  }
  spawnCommand(args, onMessage, {timeoutMs = this.commandTimeoutMs, allowClosing = false} = {}) {
    this.assertReady({allowClosing});
    const child = spawn(this.pythonCommand[0], [...this.pythonCommand.slice(1), this.entryPath, ...args], {...this.launchOptions, cwd: this.launchOptions.cwd || this.runtimeRoot, env: {...process.env, ...this.launchOptions.env, PYTHONUNBUFFERED: '1', PYTHONDONTWRITEBYTECODE: '1'}, windowsHide: true, shell: false, stdio: ['ignore', 'pipe', 'pipe']});
    this.children.add(child);
    let stdout = ''; let stderr = ''; let result = null; let commandError = null; let timer;
    const consume = line => {
      if (!line.trim()) return;
      if (Buffer.byteLength(line) > 8 * 1024 * 1024) { commandError = new Error('本地采集运行时单条消息超过大小限制。'); child.kill(); return; }
      let message;
      try { message = JSON.parse(line); } catch { onMessage?.({type: 'log', stream: 'stdout', message: line.slice(0, 4000)}); return; }
      if (!message || typeof message !== 'object' || Array.isArray(message)) return;
      if (message.type === 'result') result = message;
      if (message.type === 'error') commandError = new Error(String(message.message || message.error || '本地采集运行时报告错误。').slice(0, 4000));
      onMessage?.(message);
    };
    child.stdout.setEncoding('utf8'); child.stderr.setEncoding('utf8');
    child.stdout.on('data', chunk => {
      stdout += chunk;
      if (Buffer.byteLength(stdout) > 8 * 1024 * 1024) { commandError = new Error('本地采集运行时输出超过缓冲限制。'); child.kill(); return; }
      let split; while ((split = stdout.indexOf('\n')) >= 0) { consume(stdout.slice(0, split)); stdout = stdout.slice(split + 1); }
    });
    child.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-16000); onMessage?.({type: 'log', stream: 'stderr', message: chunk.slice(-4000)}); });
    const completion = new Promise(resolve => {
      child.on('error', error => { commandError = new Error(`无法启动本地 Python 采集运行时：${error.message}`); this.runtime = {...this.runtime, available: false, diagnostic: commandError.message}; });
      child.on('close', (exitCode, signal) => {
        clearTimeout(timer); this.children.delete(child); if (stdout.trim()) consume(stdout);
        resolve({result, error: commandError, exitCode, signal, stderr});
      });
      if (timeoutMs > 0) timer = setTimeout(() => { commandError = new Error(`本地采集辅助操作超过 ${timeoutMs} ms。`); child.kill(); }, timeoutMs);
    });
    return {child, completion};
  }
  async command(args, {allowClosing = false} = {}) {
    const {completion} = this.spawnCommand(args, null, {allowClosing});
    const outcome = await completion;
    if (outcome.error) throw outcome.error;
    if (outcome.exitCode !== 0) throw new Error(`本地采集辅助操作失败（${outcome.exitCode ?? outcome.signal}）：${outcome.stderr.trim() || outcome.result?.message || '未返回成功结果'}`);
    if (!outcome.result) throw new Error('本地采集运行时退出但未返回结果。');
    this.runtime = {...this.runtime, available: true, diagnostic: ''};
    return outcome.result;
  }
  async catalog() {
    if (!this.catalogPromise) this.catalogPromise = this.command(['catalog']).then(value => value.catalog || Object.fromEntries(Object.entries(value).filter(([key]) => key !== 'type'))).catch(error => { this.catalogPromise = null; throw error; });
    return copy(await this.catalogPromise);
  }
  async savePlan({plan} = {}, {notify = true, persist = true, expectedUpdatedAt = null} = {}) {
    this.assertReady();
    if (!plan || typeof plan !== 'object' || Array.isArray(plan) || Buffer.byteLength(JSON.stringify(plan)) > 4 * 1024 * 1024) throw new Error('采集计划必须为大小小于 4 MiB 的对象。');
    const previousId = plan.planId || plan.plan_id || plan.artifact_id || plan.id;
    const previous = this.state.plans.find(value => value.planId === previousId || value.plan.artifact_id === previousId);
    if (expectedUpdatedAt !== null && previous?.updatedAt !== expectedUpdatedAt) throw new Error('计划已在助手处理期间更新；迟到结果未覆盖新版本，请重新发起计划补全。');
    const requestId = id('plan_request');
    const inputPath = path.join(this.jobsRoot, `${requestId}.json`);
    atomicJson(inputPath, plan);
    let value;
    try { value = await this.command(['save-plan', '--input', inputPath, '--output-root', this.packagesRoot]); }
    finally { try { fs.unlinkSync(inputPath); } catch {} }
    this.assertReady();
    if (!value.plan || typeof value.plan !== 'object' || !value.plan_id || !value.package_directory) throw new Error('计划校验器未返回完整的计划和运行包。');
    const packageDirectory = fs.realpathSync(value.package_directory);
    if (!inside(fs.realpathSync(this.packagesRoot), packageDirectory) || !fs.statSync(packageDirectory).isDirectory()) throw new Error('计划运行包不在本机计划目录中。');
    if (!fs.existsSync(path.join(packageDirectory, 'package.json')) || !fs.existsSync(path.join(packageDirectory, 'plan.json'))) throw new Error('计划运行包缺少 package.json 或 plan.json。');
    const current = this.state.plans.find(item => item.planId === previousId || item.plan.artifact_id === previousId);
    if (expectedUpdatedAt !== null && current?.updatedAt !== expectedUpdatedAt) throw new Error('计划已在助手处理期间更新；迟到结果未覆盖新版本，请重新发起计划补全。');
    const baseline = current || previous;
    const record = {planId: baseline?.planId || String(value.plan_id), title: String(value.plan.title || plan.title || '场景采集计划'), plan: value.plan, packageDirectory, packageZip: value.package_zip || '', createdAt: baseline?.createdAt || now(), updatedAt: nextRevision(baseline?.updatedAt)};
    const index = this.state.plans.findIndex(item => item.planId === record.planId);
    if (index >= 0) this.state.plans.splice(index, 1);
    this.state.plans.unshift(record); if (persist) this.persist(); if (notify) this.notify(); return copy(record);
  }
  findPlanUpdateRequest(requestId) { return this.state.planUpdateRequests.find(entry => entry.requestId === requestId) || null; }
  completedPlanUpdateResult(entry, summary) {
    return {requestId: entry.requestId, planId: entry.planId, updatedAt: entry.updatedAt, changed: entry.changed, readiness: copy(entry.readiness ?? null), summary: summary.trim()};
  }
  rememberPlanUpdate(entry) {
    this.state.planUpdateRequests = [entry, ...this.state.planUpdateRequests.filter(value => value.requestId !== entry.requestId)].slice(0, PLAN_UPDATE_HISTORY_LIMIT);
    this.persist();
  }
  enqueuePlanUpdate(planId, operation) {
    const previous = this.planUpdateQueues.get(planId) || Promise.resolve();
    const run = previous.catch(() => {}).then(operation);
    const tail = run.catch(() => {});
    this.planUpdateQueues.set(planId, tail);
    return run.finally(() => { if (this.planUpdateQueues.get(planId) === tail) this.planUpdateQueues.delete(planId); });
  }
  async updatePlan({planId, expectedUpdatedAt, requestId, title, app, targetUpdates, stepUpdates, confirmUnchanged = false, summary = ''} = {}) {
    this.assertReady();
    if (typeof planId !== 'string' || !PLAN_ID.test(planId)) throw new Error('采集计划 ID 无效。');
    if (typeof requestId !== 'string' || !PLAN_ID.test(requestId)) throw new Error('计划补全请求 ID 无效。');
    if (typeof summary !== 'string' || summary.length > 1000) throw new Error('计划补全摘要不能超过 1000 个字符。');
    const request = {planId, expectedUpdatedAt, title, app, targetUpdates, stepUpdates, confirmUnchanged, summary};
    const canonical = canonicalJson(request);
    if (Buffer.byteLength(canonical) > 256 * 1024) throw new Error('采集计划补丁超过 256 KiB。');
    const fingerprint = crypto.createHash('sha256').update(canonical).digest('hex');
    const replay = this.findPlanUpdateRequest(requestId);
    if (replay) {
      if (replay.fingerprint !== fingerprint) throw new Error('计划补全请求 ID 已用于不同参数，请生成新的 requestId。');
      if (replay.status === 'failed') throw new Error(planUpdateFailureMessage(replay.failureCode));
      return this.completedPlanUpdateResult(replay, summary);
    }
    const active = this.planUpdateInFlight.get(requestId);
    if (active) {
      if (active.fingerprint !== fingerprint) throw new Error('计划补全请求 ID 正在处理不同参数，请生成新的 requestId。');
      return active.promise;
    }
    const knownPlan = this.state.plans.find(value => value.planId === planId || value.plan?.artifact_id === planId);
    const logicalPlanId = knownPlan?.planId || planId;
    const operation = this.enqueuePlanUpdate(logicalPlanId, async () => {
      try {
        const record = this.state.plans.find(value => value.planId === logicalPlanId || value.plan?.artifact_id === planId);
        if (!record) throw new Error('采集计划不存在，请从当前页面重新发起计划补全。');
        if (typeof expectedUpdatedAt !== 'string' || expectedUpdatedAt !== record.updatedAt) throw new Error('计划已在助手处理期间更新；迟到结果未覆盖新版本，请重新发起计划补全。');
        const changed = title !== undefined || app !== undefined || targetUpdates !== undefined || stepUpdates !== undefined;
        if (confirmUnchanged === true && changed) throw new Error('confirmUnchanged 不能与计划补丁同时提交。');
        if (app !== undefined && (!app || typeof app !== 'object' || Array.isArray(app) || Object.keys(app).length === 0)) throw new Error('应用补丁需要是非空对象。');
        if (targetUpdates !== undefined && (!targetUpdates || typeof targetUpdates !== 'object' || Array.isArray(targetUpdates) || Object.keys(targetUpdates).length === 0)) throw new Error('控件定位补丁需要是非空对象。');
        if (stepUpdates !== undefined && (!Array.isArray(stepUpdates) || stepUpdates.length === 0)) throw new Error('步骤补丁需要是非空数组。');
        if (!changed && confirmUnchanged !== true) throw new Error('没有计划修改时请使用 confirmUnchanged 完成请求。');
        if (!changed) {
          const terminal = {requestId, fingerprint, planId: record.planId, status: 'succeeded', changed: false, updatedAt: record.updatedAt, readiness: copy(record.plan?.readiness || null), completedAt: now()};
          this.rememberPlanUpdate(terminal);
          const result = this.completedPlanUpdateResult(terminal, summary);
          this.notify('agent-plan-completed', {requestId, planId: record.planId, changed: false, state: this.getState()});
          return result;
        }
        const next = copy(record.plan);
        next.planId = record.planId;
        if (title !== undefined) {
          if (typeof title !== 'string' || !title.trim() || title.length > 200) throw new Error('计划名称需要是 1–200 个字符。');
          next.title = title.trim();
        }
        if (app !== undefined) {
          const keys = Object.keys(app);
          if (keys.some(key => !['bundle', 'ability'].includes(key))) throw new Error('应用补丁只支持 bundle 和 ability。');
          next.profile = {...(next.profile || {})};
          for (const key of keys) {
            if (typeof app[key] !== 'string' || !app[key].trim() || app[key].length > 300) throw new Error(`${key} 需要是非空字符串。`);
            next.profile[key] = app[key].trim();
          }
        }
        if (targetUpdates !== undefined) {
          next.profile = {...(next.profile || {}), targets: {...(next.profile?.targets || {})}};
          for (const [name, selector] of Object.entries(targetUpdates)) {
            if (!/^[A-Za-z][A-Za-z0-9_-]{0,127}$/.test(name)) throw new Error('控件名称无效。');
            if (selector === null) { next.profile.targets[name] = null; continue; }
            if (!selector || typeof selector !== 'object' || Array.isArray(selector) || Object.keys(selector).length === 0) throw new Error('控件定位信息需要是非空对象或 null。');
            next.profile.targets[name] = copy(selector);
          }
        }
        if (stepUpdates !== undefined) {
          if (stepUpdates.length > 500) throw new Error('步骤补丁需要是最多 500 项的数组。');
          const steps = Array.isArray(next.steps) ? next.steps : [];
          const seen = new Set();
          for (const update of stepUpdates) {
            if (!update || typeof update !== 'object' || Array.isArray(update) || typeof update.step_id !== 'string') throw new Error('每个步骤补丁都需要 step_id。');
            if (seen.has(update.step_id)) throw new Error('步骤补丁重复。');
            seen.add(update.step_id);
            const step = steps.find(value => value.step_id === update.step_id);
            if (!step) throw new Error('计划步骤不存在。');
            const keys = Object.keys(update).filter(key => key !== 'step_id');
            if (!keys.length) throw new Error('每个步骤补丁至少需要一个修改字段。');
            if (keys.some(key => !STEP_PATCH_FIELDS.has(key))) throw new Error('步骤补丁包含不支持的字段。');
            if (Object.hasOwn(update, 'selector') && update.selector !== null
              && (!update.selector || typeof update.selector !== 'object' || Array.isArray(update.selector) || Object.keys(update.selector).length === 0))
              throw new Error('步骤内联定位需要是非空对象或 null。');
            for (const key of keys) step[key] = copy(update[key]);
          }
        }
        const saved = await this.savePlan({plan: next}, {notify: false, persist: false, expectedUpdatedAt: record.updatedAt});
        const terminal = {requestId, fingerprint, planId: saved.planId, status: 'succeeded', changed: true, updatedAt: saved.updatedAt, readiness: copy(saved.plan?.readiness || null), completedAt: now()};
        this.rememberPlanUpdate(terminal);
        const result = this.completedPlanUpdateResult(terminal, summary);
        this.notify('agent-plan-completed', {requestId, planId: saved.planId, changed: true, state: this.getState()});
        return result;
      } catch (error) {
        if (!this.findPlanUpdateRequest(requestId)) this.rememberPlanUpdate({requestId, fingerprint, planId, status: 'failed', failureCode: planUpdateFailureCode(error), completedAt: now()});
        throw error;
      }
    });
    this.planUpdateInFlight.set(requestId, {fingerprint, promise: operation});
    try { return await operation; }
    finally { if (this.planUpdateInFlight.get(requestId)?.promise === operation) this.planUpdateInFlight.delete(requestId); }
  }
  startCapture({planId, deviceSerial, hdcPath = 'hdc'} = {}) {
    this.assertReady();
    if (this.active) throw new Error('已有采集任务正在执行，请等待完成或取消后重试。');
    const plan = this.state.plans.find(value => value.planId === planId);
    if (!plan) throw new Error('请先保存并选择采集计划。');
    const selected = this.state.selectedDeviceSerial;
    if (!selected || !SERIAL.test(selected)) throw new Error('请先从设备发现列表明确选择一台手机。');
    if (deviceSerial && deviceSerial !== selected) throw new Error('执行请求的设备与用户当前选择不一致，请重新选择设备后执行。');
    if (typeof hdcPath !== 'string' || !hdcPath.trim() || hdcPath.includes('\0')) throw new Error('本地 HDC 路径无效。');
    const recoverableDeviceRunIds = this.recoverableDeviceRunIds(selected, plan.plan?.project_id);
    const runId = id('desktop_run');
    const jobDirectory = path.join(this.jobsRoot, runId); fs.mkdirSync(jobDirectory);
    const cancelFile = path.join(jobDirectory, 'cancel.request');
    const record = {runId, planId, title: plan.title, deviceSerial: selected, status: 'starting', stage: 'starting', message: '正在本机启动采集…', createdAt: now(), updatedAt: now(), outputRoot: this.outputRoot, jobDirectory, packageDirectory: plan.packageDirectory, hdcPath, cancelFile, log: [], planSnapshot: copy(plan.plan), captureRunId: '', traceId: '', recoverableDeviceRunIds};
    this.state.runs.unshift(record);
    this.state.runs = this.state.runs.slice(0, 200);
    this.active = {runId, child: null, completion: null};
    this.persist(); this.notify();
    try {
      const processTask = this.spawnCommand(['run', '--package', plan.packageDirectory, '--hdc', hdcPath, '--device-serial', selected, '--output-root', this.outputRoot, '--cancel-file', cancelFile, '--parent-pid', String(process.pid), '--recover-run-ids', recoverableDeviceRunIds.join(',')], message => this.captureMessage(record, message), {timeoutMs: 0});
      this.active.child = processTask.child; record.pid = processTask.child.pid || null;
      this.active.completion = processTask.completion.then(outcome => this.finishCapture(record, outcome)).catch(error => {
        Object.assign(record, {status: 'failed', stage: 'failed', message: `采集结果处理失败：${error.message}`, finishedAt: now(), updatedAt: now()});
        if (this.active?.runId === record.runId) this.active = null;
        this.persist(); this.notify(); return copy(record);
      });
      this.persist();
    } catch (error) {
      Object.assign(record, {status: 'failed', stage: 'failed', message: error.message, finishedAt: now(), updatedAt: now()}); this.active = null; this.persist(); this.notify(); throw error;
    }
    return copy(record);
  }
  captureMessage(record, message) {
    if (!ACTIVE.has(record.status)) return;
    if (message.type === 'result') return;
    if (message.error) record.error = copy(message.error);
    if (typeof message.run_id === 'string') record.captureRunId = message.run_id;
    if (typeof message.run_directory === 'string' && inside(this.outputRoot, path.resolve(message.run_directory))) record.runDirectory = path.resolve(message.run_directory);
    if (message.type === 'progress') {
      if (record.status === 'starting') record.status = 'running';
      record.stage = String(message.stage || record.stage).slice(0, 200);
      record.message = String(message.message || message.status || record.message).slice(0, 4000);
      record.progress = copy(message);
    }
    const log = {at: now(), type: message.type || 'log', message: String(message.message || message.stage || '').slice(0, 4000)};
    if (log.message) { record.log.push(log); record.log = record.log.slice(-120); }
    record.updatedAt = now(); this.schedulePersist();
    this.notify(message.type === 'progress' ? 'progress' : 'log', {run: copy(record)});
  }
  async finishCapture(record, outcome) {
    const result = outcome.result;
    let status = result && TERMINAL.has(result.status) ? result.status : 'failed';
    let message = result?.message || '';
    if (result?.error) record.error = copy(result.error);
    if (result?.run_path) {
      try {
        const runPath = fs.realpathSync(result.run_path);
        if (inside(fs.realpathSync(this.outputRoot), runPath) && fs.statSync(runPath).size < 8 * 1024 * 1024) {
          const runMetadata = JSON.parse(fs.readFileSync(runPath, 'utf8'));
          const detailed = runMetadata.manifest?.quality?.errors?.at(-1) || runMetadata.manifest?.steps?.findLast(step => step.error)?.error || runMetadata.error;
          if (detailed) record.error = copy(detailed);
        }
      } catch (error) { record.resultDiagnostic = `任务详情未能读取：${error.message}`; }
    }
    if (outcome.error) { status = 'failed'; message = outcome.error.message; }
    else if (!result) { status = record.forceInterrupted ? 'interrupted' : 'failed'; message = record.forceInterrupted ? '应用关闭时采集未完成收尾，保留现场供恢复。' : `采集进程已退出（${outcome.exitCode ?? outcome.signal}），但没有返回完整结果。${outcome.stderr ? ` ${outcome.stderr.trim().slice(-2000)}` : ''}`; }
    else if (['succeeded', 'partial'].includes(status) && outcome.exitCode !== 0) { status = 'failed'; message = `采集报告成功但进程退出码为 ${outcome.exitCode ?? outcome.signal}，请查看日志。`; }
    record.exitCode = outcome.exitCode; record.signal = outcome.signal || '';
    if (result) {
      record.captureRunId = String(result.run_id || record.captureRunId);
      record.result = copy(result);
      const source = result.run_directory || result.archive;
      if (source) {
        try {
          if (!inside(fs.realpathSync(this.outputRoot), fs.realpathSync(source))) throw new Error('采集结果路径不在本机采集库中。');
          const trace = await this.registerTrace({path: source}, {internal: true});
          record.traceId = trace.id; record.runDirectory = trace.runDirectory; record.archivePath = trace.archivePath;
          const manifest = this.state.traces.find(item => item.id === trace.id).manifest;
          const manifestError = manifest.quality?.errors?.at(-1) || manifest.steps?.findLast(step => step.error)?.error;
          if (manifestError) record.error = copy(manifestError);
          if (result.archive) {
            const archive = fs.realpathSync(result.archive);
            if (!inside(fs.realpathSync(this.outputRoot), archive) || !archive.endsWith('.capture.zip') || !fs.statSync(archive).isFile()) throw new Error('标准采集包路径无效。');
            const stored = this.state.traces.find(item => item.id === trace.id);
            stored.archivePath = archive; stored.generatedArchive = true; record.archivePath = archive;
          }
        } catch (error) {
          record.registrationError = error.message;
          if (['succeeded', 'partial'].includes(status)) { status = 'failed'; message = `采集结果校验失败：${error.message}`; }
        }
      } else if (['succeeded', 'partial'].includes(status)) { status = 'failed'; message = '采集报告完成，但未返回 Trace 目录或标准归档。'; }
    }
    if (record.forceInterrupted) { status = 'interrupted'; message = '应用关闭时采集未完成收尾，已保留现场；设备采集器清理状态需核对。'; }
    if (!message && ['failed', 'cancelled', 'interrupted'].includes(status)) message = describeError(record.error);
    Object.assign(record, {status, stage: 'finished', message: message || ({succeeded: '采集完成，数据已登记到 Trace 管理。', partial: '采集完成，部分采集能力不可用；已保留实际数据和诊断。', cancelled: '采集已取消，现场数据与清理记录已保留。', failed: '采集失败，请查看进度和诊断。', interrupted: '采集中断，已保留执行记录。'}[status]), updatedAt: now(), finishedAt: now()});
    if (this.active?.runId === record.runId) this.active = null;
    this.persist(); this.notify(); return copy(record);
  }
  cancelCapture({runId} = {}) {
    const record = this.state.runs.find(run => run.runId === runId);
    if (!record) throw new Error('采集任务不存在。');
    if (!ACTIVE.has(record.status)) return copy(record);
    if (this.active?.runId !== runId) throw new Error('采集任务已不在当前进程执行。');
    atomicJson(record.cancelFile, {requestedAt: now(), runId});
    Object.assign(record, {status: 'cancelling', stage: 'cancelling', message: '已请求停止采集，等待设备采集器清理和归档。', updatedAt: now()});
    this.persist(); this.notify(); return copy(record);
  }
  async registerTrace({path: inputPath} = {}, {internal = false} = {}) {
    if (!internal) this.assertReady();
    if (typeof inputPath !== 'string' || !inputPath.trim() || inputPath.includes('\0')) throw new Error('请选择标准采集目录或 .capture.zip 文件。');
    const requestedPath = path.resolve(inputPath);
    if (fs.lstatSync(requestedPath).isSymbolicLink()) throw new Error('采集源不能是符号链接。');
    const sourcePath = fs.realpathSync(requestedPath);
    const sourceStat = fs.statSync(sourcePath);
    if (!sourceStat.isDirectory() && (!sourceStat.isFile() || !sourcePath.toLowerCase().endsWith('.capture.zip'))) throw new Error('请选择标准采集目录或 .capture.zip 文件。');
    const inspected = await this.command(['inspect-trace', '--path', requestedPath], {allowClosing: internal});
    if (!internal) this.assertReady();
    const manifest = inspected.manifest;
    if (!manifest || manifest.schema_version !== 'scenario_capture_manifest/v1' || !Array.isArray(manifest.files) || !manifest.files.length || !TERMINAL.has(manifest.status)) throw new Error('Trace 检查器未返回有效的终态清单。');
    for (const file of manifest.files) safeRelative(file.relative_path);
    const identity = inspected.identity_sha256 || fileIdentity(manifest);
    let record = this.state.traces.find(trace => trace.runId === manifest.run_id);
    if (record && record.identity !== identity) throw new Error('同一采集 run_id 已登记了不同内容，不能覆盖。');
    if (!record) {
      record = {id: id('trace'), runId: manifest.run_id, title: String(manifest.title || manifest.run_id), status: manifest.status, deviceSerial: String(manifest.device_serial || manifest.device?.serial || ''), registeredAt: now(), sourcePath, runDirectory: '', archivePath: '', identity, manifest, fileCount: manifest.files.length, totalBytes: manifest.files.reduce((sum, file) => sum + Number(file.size_bytes || 0), 0), verifiedAt: now()};
      this.state.traces.unshift(record);
    }
    if (sourceStat.isDirectory()) record.runDirectory = sourcePath; else record.archivePath = sourcePath;
    record.sourcePath = record.runDirectory || record.archivePath; record.updatedAt = now();
    this.persist(); this.notify('trace', {trace: this.traceSummary(record)}); this.notify(); return copy(this.traceSummary(record));
  }
  listTraces() { return copy(this.state.traces.map(trace => ({...this.traceSummary(trace), available: fs.existsSync(trace.sourcePath)}))); }
  listTraceAnalysisSources() {
    return copy(this.state.traces.map(trace => {
      const matchingRun = this.state.runs.find(run => run.traceId === trace.id || run.captureRunId === trace.runId);
      const manifestPlanId = String(trace.manifest?.plan_id || '');
      const matchingPlan = this.state.plans.find(plan => plan.planId === matchingRun?.planId
        || (manifestPlanId && (plan.planId === manifestPlanId || plan.plan?.artifact_id === manifestPlanId || plan.plan?.plan_id === manifestPlanId)));
      const archivePath = trace.archivePath || (String(trace.sourcePath || '').toLowerCase().endsWith('.capture.zip') ? trace.sourcePath : '');
      let archiveAvailable = false;
      try {
        const attributes = archivePath ? fs.lstatSync(archivePath) : null;
        archiveAvailable = Boolean(attributes && !attributes.isSymbolicLink() && attributes.isFile());
      } catch {}
      const files = Array.isArray(trace.manifest?.files) ? trace.manifest.files : [];
      const steps = (Array.isArray(trace.manifest?.steps) ? trace.manifest.steps : []).filter(step => step?.captured).map(step => {
        const stepFiles = files.filter(file => file?.step_id === step.step_id);
        const traceFiles = stepFiles.filter(file => file?.kind === 'trace');
        const traceValid = step.trace_validation?.valid === true && step.collector_status?.trace !== 'invalid';
        const ready = archiveAvailable && traceValid && traceFiles.length === 1;
        const matchedOrdinal = String(step.step_id || '').match(/^step[_-]0*([1-9][0-9]*)$/i);
        const stepOrdinal = Number.isInteger(step.ordinal) && step.ordinal > 0 ? step.ordinal : matchedOrdinal ? Number(matchedOrdinal[1]) : null;
        const reason = ready ? '' : !archiveAvailable ? '缺少标准 capture.zip 归档。'
          : !traceValid ? '该步骤的 Trace 未通过采集质量校验。'
          : traceFiles.length !== 1 ? '该步骤需要且只能包含一个 Trace 文件。' : '该步骤暂不可分析。';
        return {stepId: String(step.step_id || ''), stepOrdinal, title: String(step.title || step.step_id || '采集步骤'), ready, reason,
          collectors: copy(step.collector_status || {}), traceValidation: copy(step.trace_validation || {})};
      }).filter(step => step.stepId);
      const planTitle = [trace.manifest?.title, matchingRun?.title, matchingPlan?.title, trace.title !== trace.runId ? trace.title : '']
        .find(value => typeof value === 'string' && value.trim())?.trim().slice(0, 300) || '';
      return {traceId: trace.id, runId: trace.runId, title: trace.title, planTitle, status: trace.status, deviceSerial: trace.deviceSerial,
        registeredAt: trace.registeredAt, archiveAvailable, available: archiveAvailable && fs.existsSync(trace.sourcePath), steps};
    }));
  }
  traceArchiveForAnalysis({id: traceId} = {}) {
    const trace = this.state.traces.find(item => item.id === traceId);
    if (!trace) throw new Error('Trace 记录不存在。');
    const requested = trace.archivePath || (String(trace.sourcePath || '').toLowerCase().endsWith('.capture.zip') ? trace.sourcePath : '');
    if (!requested) throw new Error('该采集记录没有标准 capture.zip；请登记采集生成的完整归档。');
    let attributes;
    try { attributes = fs.lstatSync(requested); }
    catch { throw new Error('采集归档已移动或删除，请在 Trace 管理中重新登记。'); }
    if (attributes.isSymbolicLink() || !attributes.isFile()) throw new Error('采集归档必须是已登记的普通文件。');
    const archivePath = fs.realpathSync(requested);
    if (!archivePath.toLowerCase().endsWith('.capture.zip') || !this.isAllowedPath(archivePath)) throw new Error('采集归档不属于当前 Trace 登记。');
    return {trace, archivePath, attributes: fs.statSync(archivePath)};
  }
  getTrace({id: traceId} = {}) {
    const trace = this.state.traces.find(item => item.id === traceId);
    if (!trace) throw new Error('Trace 记录不存在。');
    const files = trace.manifest.files.map(file => ({...file, ...(trace.runDirectory ? {absolutePath: path.join(trace.runDirectory, ...safeRelative(file.relative_path).split('/'))} : {})}));
    const regularFileAvailable = filename => {
      if (!filename) return false;
      try { const current = fs.lstatSync(filename); return !current.isSymbolicLink() && current.isFile(); }
      catch { return false; }
    };
    const directoryAvailable = filename => {
      if (!filename) return false;
      try { const current = fs.lstatSync(filename); return !current.isSymbolicLink() && current.isDirectory(); }
      catch { return false; }
    };
    const sourceExists = fs.existsSync(trace.sourcePath);
    return copy({...trace, files, availability: {
      exists: sourceExists,
      runDirectoryExists: directoryAvailable(trace.runDirectory),
      archiveExists: regularFileAvailable(trace.archivePath),
      ...(!sourceExists ? {diagnostic: '原始采集路径已被移动或删除，登记记录仍保留。'} : {}),
    }});
  }
  removeTrace({id: traceId} = {}) {
    if (this.closing || this.disposed) throw new Error('采集管理器正在关闭。');
    const index = this.state.traces.findIndex(trace => trace.id === traceId);
    if (index < 0) throw new Error('Trace 记录不存在。');
    const [removed] = this.state.traces.splice(index, 1); this.persist(); this.notify(); return {removed: true, filesDeleted: false, id: removed.id};
  }
  isAllowedPath(target) {
    if (typeof target !== 'string' || !target || target.includes('\0')) return false;
    let canonical; try { canonical = fs.realpathSync(target); } catch { return false; }
    for (const trace of this.state.traces) {
      for (const item of [trace.sourcePath, trace.runDirectory, trace.archivePath].filter(Boolean)) {
        try { if (!fs.lstatSync(item).isSymbolicLink() && fs.realpathSync(item) === canonical) return true; } catch {}
      }
      if (trace.runDirectory) {
        let directory; try { directory = fs.realpathSync(trace.runDirectory); } catch { continue; }
        if (!inside(directory, canonical)) continue;
        for (const file of trace.manifest.files) { try { if (fs.realpathSync(path.join(directory, ...safeRelative(file.relative_path).split('/'))) === canonical && fs.statSync(canonical).isFile()) return true; } catch {} }
      }
    }
    return false;
  }
  async dispose() {
    if (this.disposed || this.closing) return;
    this.closing = true;
    const current = this.active;
    if (current) {
      this.cancelCapture({runId: current.runId});
      let timedOut = false;
      let timer;
      await Promise.race([current.completion, new Promise(resolve => { timer = setTimeout(() => { timedOut = true; resolve(); }, this.disposeGraceMs); })]);
      clearTimeout(timer);
      if (timedOut && this.active?.runId === current.runId) {
        const record = this.state.runs.find(run => run.runId === current.runId); record.forceInterrupted = true;
        current.child?.kill();
        await current.completion;
      }
    }
    this.disposed = true;
    for (const child of this.children) child.kill();
    this.persist();
  }
}

module.exports = {CaptureManager, safeRelative, fileIdentity};
