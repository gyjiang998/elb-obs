'use strict';

const fs = require('node:fs');
const path = require('node:path');

function now() { return new Date().toISOString(); }
function clone(value) { return JSON.parse(JSON.stringify(value)); }

function operationDurationScope(event) {
  if (typeof event?.durationScope === 'string' && event.durationScope) return event.durationScope;
  // A successful proc.exec response carries the command's duration from the
  // remote worker. Other broker operations currently report elapsed wall
  // time around the Desktop operation, which can include worker acquisition,
  // queueing and transport. Persist that distinction instead of presenting
  // every duration as the time spent inside the named tool.
  if (event?.kind === 'remote_exec' && Number.isFinite(event?.transportDurationMs)
      && ['completed', 'failed'].includes(event?.state)) return 'remote_execution';
  return Number.isFinite(event?.durationMs) ? 'broker_wall' : null;
}

const REDACTED = '[REDACTED]';
const RAW_OUTPUT_PREVIEW_LIMIT = 64 * 1024;
// Agent output and tool lifecycle events are latency-sensitive: they are
// emitted on the Electron main-process path that also serves the loopback
// tool broker.  Persisting the entire run synchronously for every event can
// hold that path behind Windows antivirus/indexing for seconds.  Keep the
// in-memory model authoritative while a session is live and coalesce durable
// snapshots outside the tool call's critical path.
const LIVE_PERSIST_DEBOUNCE_MS = 250;
const SECRET_KEY_PATTERN = String.raw`[A-Za-z0-9_.-]*(?:password|passwd|passphrase|token|secret|api[_-]?key|private[_-]?key)[A-Za-z0-9_.-]*`;
const SECRET_VALUE_PATTERN = String.raw`"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\`(?:\\.|[^\`\\])*\`|Bearer\s+(?:\[REDACTED\]|[^\s,;}&\]]+)|\[REDACTED\]|[^\s,;}&\]]+`;
const SECRET_ASSIGNMENT = new RegExp(
  `((?:["']?)${SECRET_KEY_PATTERN}(?:["']?)\\s*[:=]\\s*)(${SECRET_VALUE_PATTERN})`,
  'gi',
);
const SECRET_CLI_ARGUMENT = new RegExp(
  `(^|\\s)((?:--?|/)${SECRET_KEY_PATTERN})(\\s+)(${SECRET_VALUE_PATTERN})`,
  'gim',
);
const AUTHORIZATION_VALUE = /(\bAuthorization\b\s*[:=]?\s*["']?\s*)([A-Za-z][A-Za-z0-9._-]*)(\s+)([^"'\s,;]+)/gi;
const PRIVATE_KEY_BLOCK = /-----BEGIN ((?:[A-Z0-9]+[ _-]+)*PRIVATE KEY(?: BLOCK)?)-----[\s\S]*?-----END \1-----/gi;
const UNFINISHED_PRIVATE_KEY_BLOCK = /-----BEGIN ((?:[A-Z0-9]+[ _-]+)*PRIVATE KEY(?: BLOCK)?)-----[\s\S]*$/gi;

function redactedValue(value) {
  const text = String(value || '');
  if (/^Bearer\s+/i.test(text)) return `Bearer ${REDACTED}`;
  const quote = text[0];
  if (['"', "'", '`'].includes(quote) && text.at(-1) === quote) return `${quote}${REDACTED}${quote}`;
  return REDACTED;
}

function redactSensitiveText(value) {
  return String(value || '')
    // Replace complete blocks first. The second expression protects a block
    // while a PTY is still streaming it and the END marker has not arrived.
    .replace(PRIVATE_KEY_BLOCK, (_match, label) => `[REDACTED ${label}]`)
    .replace(UNFINISHED_PRIVATE_KEY_BLOCK, (_match, label) => `[REDACTED ${label}]`)
    .replace(AUTHORIZATION_VALUE, (_match, prefix, scheme, spacing) => `${prefix}${scheme}${spacing}${REDACTED}`)
    .replace(SECRET_ASSIGNMENT, (_match, prefix, secret) => `${prefix}${redactedValue(secret)}`)
    .replace(SECRET_CLI_ARGUMENT, (_match, leading, flag, spacing, secret) => `${leading}${flag}${spacing}${redactedValue(secret)}`);
}

class DesktopAgentRunStore {
  constructor(root) {
    this.root = path.join(root, 'agent-runs');
    fs.mkdirSync(this.root, {recursive: true, mode: 0o700});
    this.current = null;
    this.outputFlushTimer = null;
    this.persistRevision = 0;
    this.persistChain = Promise.resolve();
    this.pendingPersistSnapshot = null;
    this.persistLoopActive = false;
    // Raw PTY fragments stay in memory only so a credential split across
    // chunks can be recognized when the next fragment arrives. They are never
    // copied into the persisted run document.
    this.outputPreviewBuffers = new Map();
    this.persistOrder = Date.now() * 1000;
  }

  filename(sessionId) { return path.join(this.root, `${sessionId}.json`); }

  schedulePersist(delayMs = LIVE_PERSIST_DEBOUNCE_MS) {
    if (!this.current || this.outputFlushTimer) return;
    this.outputFlushTimer = setTimeout(() => {
      this.outputFlushTimer = null;
      this.queuePersist();
    }, Math.max(0, Number(delayMs) || 0));
    this.outputFlushTimer.unref?.();
  }

  cancelScheduledPersist() {
    if (!this.outputFlushTimer) return;
    clearTimeout(this.outputFlushTimer);
    this.outputFlushTimer = null;
  }

  persistedSnapshot(run = this.current) {
    if (!run) return null;
    const persisted = clone(run);
    for (const task of Array.isArray(persisted.tasks) ? persisted.tasks : []) {
      task.prompt_preview = redactSensitiveText(task.prompt_preview).slice(0, 8000);
      task.output_preview = redactSensitiveText(task.output_preview).slice(-8000);
    }
    for (const operation of Array.isArray(persisted.operations) ? persisted.operations : [])
      operation.message = redactSensitiveText(operation.message).slice(0, 2000);
    return persisted;
  }

  preparePersistedSnapshot() {
    if (!this.current) return null;
    this.current.updated_at = now();
    this.persistOrder = Math.max(Date.now() * 1000, this.persistOrder + 1);
    this.current.updated_order = this.persistOrder;
    return {
      revision: ++this.persistRevision,
      sessionId: this.current.session_id,
      value: this.persistedSnapshot(),
    };
  }

  queuePersist() {
    const snapshot = this.preparePersistedSnapshot();
    if (!snapshot) return;
    // Keep at most one queued snapshot behind the write currently in flight.
    // Replacing this slot prevents a slow roaming profile or antivirus scan
    // from turning every PTY/tool event into an ever-growing disk backlog.
    this.pendingPersistSnapshot = snapshot;
    if (this.persistLoopActive) return;
    this.persistLoopActive = true;
    this.persistChain = this.drainPersistQueue();
  }

  async drainPersistQueue() {
    try {
      while (this.pendingPersistSnapshot) {
        const snapshot = this.pendingPersistSnapshot;
        this.pendingPersistSnapshot = null;
        const filename = this.filename(snapshot.sessionId);
        const temporary = `${filename}.tmp-${process.pid}-${snapshot.revision}`;
        try {
          await fs.promises.writeFile(temporary, `${JSON.stringify(snapshot.value, null, 2)}\n`, {mode: 0o600});
          if (snapshot.revision !== this.persistRevision) {
            await fs.promises.rm(temporary, {force: true});
            continue;
          }
          // There is deliberately no await between the final revision check
          // and renameSync. finish() runs on the same JS thread, so it cannot
          // commit the terminal snapshot in this interval and then be
          // overwritten by an older asynchronous rename.
          fs.renameSync(temporary, filename);
        } catch (error) {
          await fs.promises.rm(temporary, {force: true}).catch(() => {});
          // Telemetry persistence must never delay or fail the Agent tool
          // path. Keep the in-memory run and report a bounded diagnostic.
          console.error(`Agent run snapshot persist failed: ${String(error?.message || error).slice(0, 500)}`);
        }
      }
    } finally {
      this.persistLoopActive = false;
      // queuePersist cannot interleave between the loop's final check and this
      // assignment, but a future implementation may enqueue from a callback;
      // retain the guard so that work can never be stranded.
      if (this.pendingPersistSnapshot) {
        this.persistLoopActive = true;
        this.persistChain = this.drainPersistQueue();
        await this.persistChain;
      }
    }
  }

  persist() {
    const snapshot = this.preparePersistedSnapshot();
    if (!snapshot) return;
    const filename = this.filename(snapshot.sessionId);
    const persisted = snapshot.value;
    fs.writeFileSync(`${filename}.tmp`, `${JSON.stringify(persisted, null, 2)}\n`, {mode: 0o600});
    fs.renameSync(`${filename}.tmp`, filename);
  }

  start({sessionId, executable, cwd, demo = false} = {}) {
    if (this.current && this.current.status === 'running') this.finish({status: 'restarted'});
    this.outputPreviewBuffers.clear();
    this.current = {
      schema_version: 'desktop_agent_run/v1',
      session_id: sessionId,
      title: `Code Agent · ${path.basename(executable || 'agent')}`,
      executable: executable || '', cwd: cwd || '', demo: Boolean(demo),
      status: 'running', created_at: now(), updated_at: now(), finished_at: null,
      tasks: [], operations: [], output: {bytes: 0, chunks: 0, first_at: null, last_at: null},
    };
    // Starting and submitting are immediately followed by latency-sensitive
    // PTY work.  A coalesced snapshot is sufficient; finish() still performs
    // a synchronous final commit.
    this.schedulePersist();
    return clone(this.current);
  }

  finish({status = 'stopped', exitCode = null, signal = null} = {}) {
    if (!this.current) return;
    this.cancelScheduledPersist();
    // A snapshot that has not started I/O is obsolete once the terminal state
    // is synchronously committed. An in-flight write is fenced by the revision
    // increment performed in persist().
    this.pendingPersistSnapshot = null;
    const completedAt = now();
    for (const task of this.current.tasks || []) {
      if (task.completed_at) continue;
      task.status = status === 'error' ? 'failed' : 'interrupted';
      task.completed_at = completedAt;
      task.completion_latency_ms = Math.max(0, Date.parse(completedAt) - Date.parse(task.submitted_at));
    }
    this.current.status = status;
    this.current.exit_code = exitCode;
    this.current.signal = signal;
    this.current.finished_at = completedAt;
    this.persist();
    this.current = null;
    this.outputPreviewBuffers.clear();
  }

  submit(text, taskContext = null) {
    if (!this.current) return;
    const submittedAt = now();
    const taskId = `task-${this.current.tasks.length + 1}`;
    this.current.tasks.push({
      task_id: taskId,
      submitted_at: submittedAt,
      prompt_preview: redactSensitiveText(String(text || '')).slice(0, 8000),
      prompt_bytes: Buffer.byteLength(String(text || '')),
      task_context: taskContext && typeof taskContext === 'object' ? Object.fromEntries([
        'task_kind', 'capture_plan_id', 'capture_plan_request_id', 'capture_plan_expected_updated_at',
        'repair_request_id', 'recovery_operation_key', 'recovery_authorization_scopes',
        'operation_attempt_id', 'selected_device', 'page', 'page_label', 'operation', 'error_code',
      ].filter(key => taskContext[key] !== undefined).map(key => [key, clone(taskContext[key])])) : null,
      status: 'running', completed_at: null, completion_latency_ms: null,
      first_tool_at: null, first_tool_latency_ms: null,
      first_output_at: null,
      first_output_latency_ms: null,
      last_output_at: null,
      output_bytes: 0,
      output_preview: '',
    });
    this.current.tasks = this.current.tasks.slice(-100);
    this.outputPreviewBuffers.set(taskId, '');
    const retainedTaskIds = new Set(this.current.tasks.map(item => item.task_id));
    for (const id of this.outputPreviewBuffers.keys()) if (!retainedTaskIds.has(id)) this.outputPreviewBuffers.delete(id);
    this.schedulePersist();
  }

  agentOutput(data) {
    if (!this.current) return;
    const value = String(data || '');
    const at = now();
    const bytes = Buffer.byteLength(value);
    this.current.output.bytes += bytes;
    this.current.output.chunks += 1;
    this.current.output.first_at ||= at;
    this.current.output.last_at = at;
    const task = this.current.tasks[this.current.tasks.length - 1];
    if (task) {
      task.last_output_at = at;
      task.output_bytes += bytes;
      // PTYs echo Enter and may emit cursor/control sequences immediately
      // after input. Count the first visible payload, not that terminal echo,
      // as the Agent's first observable response.
      const cleaned = value
        .replace(/\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])/g, '')
        .replace(/\r/g, '')
        .replace(/[\x00-\x08\x0B-\x1F\x7F]/g, '');
      const visible = cleaned.trim();
      if (visible) {
        const raw = `${this.outputPreviewBuffers.get(task.task_id) || ''}${cleaned}`.slice(-RAW_OUTPUT_PREVIEW_LIMIT);
        this.outputPreviewBuffers.set(task.task_id, raw);
        task.output_preview = redactSensitiveText(raw).trim().slice(-8000);
      }
      if (!task.first_output_at && visible) {
        task.first_output_at = at;
        task.first_output_latency_ms = Math.max(0, Date.parse(at) - Date.parse(task.submitted_at));
      }
    }
    this.schedulePersist(1000);
  }

  operation(event) {
    // Page refreshes and other platform-owned work can happen while an Agent
    // is running. Keep only operations that actually entered through the
    // Agent broker so Experience Evolution does not misattribute UI latency.
    if (!this.current || event?.type !== 'operation' || event?.origin !== 'agent') return;
    const id = String(event.id || `${event.target || 'operation'}-${Date.now()}`);
    const index = this.current.operations.findIndex(item => item.operation_id === id);
    const value = {
      operation_id: id,
      target: event.target || '', kind: event.kind || '', origin: event.origin || '',
      method: event.method || '', path: event.path || '', state: event.state || '',
      request_id: event.requestId || '',
      duration_ms: Number.isFinite(event.durationMs) ? event.durationMs : null,
      duration_scope: operationDurationScope(event),
      transport_duration_ms: Number.isFinite(event.transportDurationMs) ? event.transportDurationMs : null,
      channel_acquire_duration_ms: Number.isFinite(event.channelAcquireDurationMs) ? event.channelAcquireDurationMs : null,
      channel_startup_duration_ms: Number.isFinite(event.channelStartupDurationMs) ? event.channelStartupDurationMs : null,
      queue_wait_ms: Number.isFinite(event.queueWaitMs) ? event.queueWaitMs : null,
      transport: event.transport || '',
      exit_code: Number.isInteger(event.exitCode) ? event.exitCode : null,
      channel_reused: typeof event.channelReused === 'boolean' ? event.channelReused : null,
      message: redactSensitiveText(event.message || event.stderr || '').slice(0, 2000),
      updated_at: now(),
    };
    if (index >= 0) this.current.operations[index] = {...this.current.operations[index], ...value};
    else this.current.operations.push({...value, started_at: now()});
    if (['capture_update_plan', 'desktop_recovery_complete'].includes(event.method)
        && ['running', 'completed', 'failed'].includes(event.state)) {
      const tasks = this.current.tasks || [];
      const task = event.requestId
        ? [...tasks].reverse().find(item => (event.method === 'desktop_recovery_complete'
          ? item.task_context?.repair_request_id : item.task_context?.capture_plan_request_id) === event.requestId)
        : tasks.at(-1);
      if (task && !task.first_tool_at) {
        task.first_tool_at = now();
        task.first_tool_latency_ms = Math.max(0, Date.parse(task.first_tool_at) - Date.parse(task.submitted_at));
      }
      const terminalState = event.method === 'desktop_recovery_complete'
        ? event.state === 'completed' : ['completed', 'failed'].includes(event.state);
      if (task && terminalState && !task.completed_at) {
        task.status = event.state === 'completed' ? 'completed' : 'failed';
        task.completed_at = now();
        task.completion_latency_ms = Math.max(0, Date.parse(task.completed_at) - Date.parse(task.submitted_at));
      }
    }
    this.current.operations = this.current.operations.slice(-500);
    this.schedulePersist();
  }

  summarize(run) {
    const operations = Array.isArray(run.operations) ? run.operations : [];
    const completed = operations.filter(item => Number.isFinite(item.duration_ms));
    const byKind = {};
    for (const item of completed) {
      const key = item.kind || item.target || 'other';
      byKind[key] ||= {count: 0, duration_ms: 0, max_duration_ms: 0,
        queue_wait_ms: 0, transport_duration_ms: 0, channel_acquire_duration_ms: 0,
        max_channel_startup_duration_ms: 0};
      byKind[key].count += 1;
      byKind[key].duration_ms += item.duration_ms;
      byKind[key].max_duration_ms = Math.max(byKind[key].max_duration_ms, item.duration_ms);
      if (Number.isFinite(item.queue_wait_ms)) byKind[key].queue_wait_ms += item.queue_wait_ms;
      if (Number.isFinite(item.transport_duration_ms)) byKind[key].transport_duration_ms += item.transport_duration_ms;
      if (Number.isFinite(item.channel_acquire_duration_ms)) byKind[key].channel_acquire_duration_ms += item.channel_acquire_duration_ms;
      if (Number.isFinite(item.channel_startup_duration_ms))
        byKind[key].max_channel_startup_duration_ms = Math.max(byKind[key].max_channel_startup_duration_ms, item.channel_startup_duration_ms);
    }
    const firstOutput = (run.tasks || []).map(item => item.first_output_latency_ms).filter(Number.isFinite);
    const bottlenecks = Object.entries(byKind)
      .map(([kind, value]) => ({kind, ...value}))
      .sort((left, right) => right.duration_ms - left.duration_ms);
    return {
      session_id: run.session_id, title: run.title, status: run.status,
      created_at: run.created_at, updated_at: run.updated_at, finished_at: run.finished_at,
      task_count: (run.tasks || []).length, operation_count: operations.length,
      first_output_latency_ms: firstOutput.length ? Math.round(firstOutput.reduce((a, b) => a + b, 0) / firstOutput.length) : null,
      bottlenecks, output_bytes: run.output?.bytes || 0,
    };
  }

  list({limit = 100} = {}) {
    const values = [];
    for (const entry of fs.readdirSync(this.root, {withFileTypes: true})) {
      if (!entry.isFile() || !entry.name.endsWith('.json')) continue;
      try { values.push(JSON.parse(fs.readFileSync(path.join(this.root, entry.name), 'utf8'))); } catch {}
    }
    // The durable snapshot is intentionally debounced while an Agent is
    // active.  Replace its stale on-disk copy with the authoritative in-memory
    // state so observability never trades correctness for transport latency.
    if (this.current) {
      const live = this.persistedSnapshot();
      const index = values.findIndex(value => value.session_id === live.session_id);
      if (index >= 0) values[index] = live; else values.push(live);
    }
    values.sort((left, right) => (Number(right.updated_order) || 0) - (Number(left.updated_order) || 0)
      || String(right.updated_at || '').localeCompare(String(left.updated_at || ''))
      || String(right.session_id || '').localeCompare(String(left.session_id || '')));
    return {schema_version: 'desktop_agent_run_catalog/v1', runs: values.slice(0, Math.max(1, Math.min(Number(limit) || 100, 500))).map(value => this.summarize(value))};
  }

  get({sessionId} = {}) {
    const id = String(sessionId || '');
    if (!/^[A-Za-z0-9._:-]{1,128}$/.test(id)) throw new Error('Agent 会话 ID 无效。');
    if (this.current?.session_id === id) return this.persistedSnapshot();
    try { return JSON.parse(fs.readFileSync(this.filename(id), 'utf8')); }
    catch (error) { if (error.code === 'ENOENT') throw new Error('Agent 历史会话不存在。'); throw error; }
  }
}

module.exports = {DesktopAgentRunStore};
