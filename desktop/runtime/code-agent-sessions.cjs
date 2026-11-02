'use strict';

const fs = require('node:fs');
const fsp = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');

const MAX_SESSION_FILES = 500;
const MAX_SESSION_BYTES = 128 * 1024 * 1024;
const MAX_JSON_LINE_BYTES = 4 * 1024 * 1024;
const MAX_EVENTS = 100000;
const MAX_TIMELINE_EVENTS = 5000;
const MAX_TEXT_PREVIEW = 1600;
const MAX_TOOL_INPUT_PREVIEW = 1200;
const MAX_TOOL_RESULT_PREVIEW = 2000;
const REDACTED = '[REDACTED]';

const SECRET_ASSIGNMENT = /((?:password|passwd|passphrase|token|secret|api[_-]?key|private[_-]?key|authorization)[A-Za-z0-9_.-]*\s*[:=]\s*)("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;}&\]]+)/gi;
const PRIVATE_KEY_BLOCK = /-----BEGIN ((?:[A-Z0-9]+[ _-]+)*PRIVATE KEY(?: BLOCK)?)-----[\s\S]*?-----END \1-----/gi;
const UNFINISHED_PRIVATE_KEY_BLOCK = /-----BEGIN ((?:[A-Z0-9]+[ _-]+)*PRIVATE KEY(?: BLOCK)?)-----[\s\S]*$/gi;
const AUTHORIZATION_VALUE = /(\bAuthorization\b\s*[:=]?\s*["']?\s*)([A-Za-z][A-Za-z0-9._-]*)(\s+)([^"'\s,;]+)/gi;
const SECRET_KEY = /(?:password|passwd|passphrase|token|secret|api[_-]?key|private[_-]?key|authorization)/i;
const SECRET_CLI_ARGUMENT = /(^|\s)((?:--?|\/)[A-Za-z0-9_.-]*(?:password|passwd|passphrase|token|secret|api[_-]?key|private[_-]?key)[A-Za-z0-9_.-]*)(\s+)("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;}&\]]+)/gim;

function redact(value) {
  return String(value || '')
    .replace(PRIVATE_KEY_BLOCK, (_match, label) => '[REDACTED ' + label + ']')
    .replace(UNFINISHED_PRIVATE_KEY_BLOCK, (_match, label) => '[REDACTED ' + label + ']')
    .replace(AUTHORIZATION_VALUE, (_match, prefix, scheme, spacing) => prefix + scheme + spacing + REDACTED)
    .replace(SECRET_ASSIGNMENT, (_match, prefix, secret) => {
      const quote = ['"', "'", '`'].includes(secret[0]) && secret.at(-1) === secret[0] ? secret[0] : '';
      return prefix + quote + REDACTED + quote;
    })
    .replace(SECRET_CLI_ARGUMENT, (_match, leading, flag, spacing) => leading + flag + spacing + REDACTED);
}

function cleanPreview(value, limit) {
  return redact(String(value || '').replace(/[\x00-\x08\x0B-\x1F\x7F]/g, '').trim()).slice(0, limit);
}

function object(value) { return value && typeof value === 'object' && !Array.isArray(value) ? value : {}; }
function array(value) { return Array.isArray(value) ? value : []; }
function iso(value) {
  const time = Date.parse(String(value || ''));
  return Number.isFinite(time) ? new Date(time).toISOString() : null;
}
function sourceId(relativePath) {
  return Buffer.from(relativePath, 'utf8').toString('base64url');
}
function decodeSourceId(value) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_-]{1,2048}$/.test(value)) throw new Error('Code Agent Session 标识无效。');
  const decoded = Buffer.from(value, 'base64url').toString('utf8');
  if (!decoded || decoded.includes('\0') || path.isAbsolute(decoded)) throw new Error('Code Agent Session 标识无效。');
  return decoded;
}
function isWithin(root, candidate) {
  const relative = path.relative(root, candidate);
  return relative === '' || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative));
}
function safeSessionId(value, fallback) {
  const text = String(value || '').trim();
  return /^[A-Za-z0-9._:-]{1,160}$/.test(text) ? text : fallback;
}

async function *jsonLines(filename, {maxBytes = MAX_SESSION_BYTES, maxLineBytes = MAX_JSON_LINE_BYTES} = {}) {
  const stream = fs.createReadStream(filename, {encoding: 'utf8', highWaterMark: 64 * 1024});
  let pending = '';
  let consumed = 0;
  let lineNumber = 0;
  try {
    for await (const chunk of stream) {
      consumed += Buffer.byteLength(chunk, 'utf8');
      if (consumed > maxBytes) throw Object.assign(new Error('Session 文件超过 128 MiB 安全解析上限。'), {code: 'AGENT_SESSION_TOO_LARGE'});
      pending += chunk;
      if (Buffer.byteLength(pending, 'utf8') > maxLineBytes && !pending.includes('\n'))
        throw Object.assign(new Error('Session 中存在超过 4 MiB 的单行记录。'), {code: 'AGENT_SESSION_LINE_TOO_LARGE'});
      let newline;
      while ((newline = pending.indexOf('\n')) >= 0) {
        const line = pending.slice(0, newline).replace(/\r$/, '');
        pending = pending.slice(newline + 1);
        lineNumber += 1;
        if (Buffer.byteLength(line, 'utf8') > maxLineBytes)
          throw Object.assign(new Error(`Session 第 ${lineNumber} 行超过 4 MiB。`), {code: 'AGENT_SESSION_LINE_TOO_LARGE'});
        if (line.trim()) yield {line, lineNumber};
      }
    }
    if (pending.trim()) {
      lineNumber += 1;
      if (Buffer.byteLength(pending, 'utf8') > maxLineBytes)
        throw Object.assign(new Error(`Session 第 ${lineNumber} 行超过 4 MiB。`), {code: 'AGENT_SESSION_LINE_TOO_LARGE'});
      yield {line: pending.replace(/\r$/, ''), lineNumber};
    }
  } finally { stream.destroy(); }
}

function contentBlocks(event) {
  const blocks = [];
  const push = value => {
    if (typeof value === 'string') blocks.push({type: 'text', text: value});
    else if (value && typeof value === 'object' && !Array.isArray(value)) blocks.push(value);
  };
  const type = String(event?.type || '');
  if (['tool_use', 'tool_result', 'thinking', 'text'].includes(type)) {
    push(event);
    return blocks;
  }
  const message = object(event?.message);
  const content = Object.hasOwn(message, 'content') ? message.content : event?.content;
  for (const block of array(content)) push(block);
  if (typeof content === 'string') push(content);
  return blocks;
}

function resultText(content) {
  if (typeof content === 'string') return content;
  return array(content).map(item => typeof item === 'string' ? item : String(item?.text || item?.content || '')).filter(Boolean).join('\n');
}

function summarizeInput(value) {
  const input = object(value);
  const preferred = ['description', 'command', 'path', 'file_path', 'query', 'skill', 'name', 'method', 'url'];
  const result = {};
  for (const key of preferred) if (input[key] !== undefined) {
    const preview = cleanPreview(typeof input[key] === 'string' ? input[key] : JSON.stringify(input[key]), MAX_TOOL_INPUT_PREVIEW);
    if (preview) result[key] = preview;
  }
  if (!Object.keys(result).length) {
    for (const [key, item] of Object.entries(input).slice(0, 6)) {
      const preview = SECRET_KEY.test(key) ? REDACTED
        : cleanPreview(typeof item === 'string' ? item : JSON.stringify(item), 240);
      if (preview) result[key] = preview;
    }
  }
  return result;
}

function skillReferences(toolName, input, knownSkills) {
  const haystack = `${toolName}\n${JSON.stringify(input || {})}`.toLowerCase();
  const values = [];
  for (const skill of knownSkills) if (haystack.includes(String(skill.skill_id || '').toLowerCase())) values.push(skill.skill_id);
  if (/skill/i.test(toolName)) {
    for (const key of ['skill', 'skill_id', 'name']) {
      const value = String(input?.[key] || '').trim();
      if (value && /^[A-Za-z0-9._:-]{1,160}$/.test(value)) values.push(value);
    }
  }
  return [...new Set(values)];
}

function platformTiming(platformRun) {
  const operations = array(platformRun?.run?.operations)
    .filter(item => item && typeof item === 'object')
    .map(item => ({
      operation_id: cleanPreview(item.operation_id, 200),
      label: cleanPreview(item.method || item.kind || item.target || 'platform operation', 200),
      kind: cleanPreview(item.kind, 100),
      state: cleanPreview(item.state, 40),
      started_at: iso(item.started_at),
      updated_at: iso(item.updated_at),
      duration_ms: Number.isFinite(item.duration_ms) ? item.duration_ms : null,
      duration_scope: cleanPreview(item.duration_scope, 80)
        || (Number.isFinite(item.duration_ms) ? 'legacy_unspecified' : ''),
      queue_wait_ms: Number.isFinite(item.queue_wait_ms) ? item.queue_wait_ms : null,
      transport_duration_ms: Number.isFinite(item.transport_duration_ms) ? item.transport_duration_ms : null,
      channel_acquire_duration_ms: Number.isFinite(item.channel_acquire_duration_ms) ? item.channel_acquire_duration_ms : null,
      channel_startup_duration_ms: Number.isFinite(item.channel_startup_duration_ms) ? item.channel_startup_duration_ms : null,
      channel_reused: typeof item.channel_reused === 'boolean' ? item.channel_reused : null,
      transport: cleanPreview(item.transport, 100),
    }));
  if (!operations.length) return null;
  const timed = operations.filter(item => Number.isFinite(item.duration_ms));
  return {
    operation_count: operations.length,
    timed_operation_count: timed.length,
    total_recorded_duration_ms: timed.reduce((sum, item) => sum + item.duration_ms, 0),
    max_recorded_duration_ms: timed.reduce((maximum, item) => Math.max(maximum, item.duration_ms), 0),
    queue_wait_ms: operations.reduce((sum, item) => sum + (Number.isFinite(item.queue_wait_ms) ? item.queue_wait_ms : 0), 0),
    transport_duration_ms: operations.reduce((sum, item) => sum + (Number.isFinite(item.transport_duration_ms) ? item.transport_duration_ms : 0), 0),
    channel_acquire_duration_ms: operations.reduce((sum, item) => sum + (Number.isFinite(item.channel_acquire_duration_ms) ? item.channel_acquire_duration_ms : 0), 0),
    max_channel_startup_duration_ms: operations.reduce((maximum, item) => Math.max(maximum,
      Number.isFinite(item.channel_startup_duration_ms) ? item.channel_startup_duration_ms : 0), 0),
    operations: operations.slice(-500),
  };
}

async function readHeader(filename) {
  let count = 0;
  let metadata = {};
  let title = '';
  try {
    for await (const {line} of jsonLines(filename, {maxBytes: 256 * 1024})) {
      if (++count > 40) break;
      let event;
      try { event = JSON.parse(line); } catch { continue; }
      metadata = {...metadata, ...Object.fromEntries(['sessionId', 'cwd', 'entrypoint', 'permissionMode', 'version', 'gitBranch', 'timestamp']
        .filter(key => metadata[key] == null && event[key] != null).map(key => [key, event[key]]))};
      if (!title) {
        const role = String(event?.message?.role || event?.type || '').toLowerCase();
        if (role === 'user') title = contentBlocks(event).filter(block => block.type === 'text')
          .map(block => cleanPreview(block.text, 160)).find(Boolean) || '';
      }
      if (metadata.sessionId && title) break;
    }
  } catch (error) {
    if (error.code !== 'AGENT_SESSION_TOO_LARGE') throw error;
  }
  return {metadata, title};
}

async function scanSkillRoot(root) {
  const result = [];
  if (!root) return result;
  const visit = async (directory, depth) => {
    if (depth > 3 || result.length >= 200) return;
    let entries;
    try { entries = await fsp.readdir(directory, {withFileTypes: true}); } catch { return; }
    if (entries.some(entry => entry.isFile() && entry.name === 'SKILL.md')) {
      result.push({skill_id: path.basename(directory), path: directory});
      return;
    }
    for (const entry of entries) if (entry.isDirectory() && !entry.isSymbolicLink()) await visit(path.join(directory, entry.name), depth + 1);
  };
  await visit(root, 0);
  return result.sort((left, right) => left.skill_id.localeCompare(right.skill_id));
}

class CodeAgentSessionStore {
  constructor({userDataDir, integration = () => null, agentRuns = null, homeDir = os.homedir()} = {}) {
    if (!userDataDir) throw new Error('userDataDir is required');
    this.userDataDir = userDataDir;
    this.settingsPath = path.join(userDataDir, 'code-agent-sessions.json');
    this.integration = integration;
    this.agentRuns = agentRuns;
    this.homeDir = homeDir;
    this.configuredRoot = '';
    this.headerCache = new Map();
    try {
      const saved = JSON.parse(fs.readFileSync(this.settingsPath, 'utf8'));
      this.configuredRoot = typeof saved.root_path === 'string' ? saved.root_path : '';
    } catch {}
  }

  automaticRoots() {
    return [path.join(this.homeDir, '.cac', 'projects')];
  }

  async resolveRoot() {
    const candidates = this.configuredRoot ? [this.configuredRoot] : this.automaticRoots();
    for (const candidate of candidates) {
      const resolved = path.resolve(candidate);
      try {
        const stat = await fsp.stat(resolved);
        if (stat.isDirectory()) return {path: resolved, configured: Boolean(this.configuredRoot), available: true};
      } catch {}
    }
    return {path: this.configuredRoot ? path.resolve(this.configuredRoot) : path.resolve(this.automaticRoots()[0]), configured: Boolean(this.configuredRoot), available: false};
  }

  async configure({rootPath = ''} = {}) {
    const value = String(rootPath || '').trim();
    if (value) {
      if (!path.isAbsolute(value)) throw new Error('Code Agent Session 根目录必须是绝对路径。');
      const resolved = path.resolve(value);
      const stat = await fsp.stat(resolved).catch(() => null);
      if (!stat?.isDirectory()) throw new Error('Code Agent Session 根目录不存在或不是目录。');
      this.configuredRoot = resolved;
    } else this.configuredRoot = '';
    const temporary = `${this.settingsPath}.tmp`;
    await fsp.writeFile(temporary, `${JSON.stringify({root_path: this.configuredRoot}, null, 2)}\n`, {mode: 0o600});
    await fsp.rename(temporary, this.settingsPath);
    return this.list();
  }

  async files(root) {
    const values = [];
    const visit = async (directory, depth) => {
      if (depth > 2 || values.length >= MAX_SESSION_FILES) return;
      let entries;
      try { entries = await fsp.readdir(directory, {withFileTypes: true}); } catch { return; }
      for (const entry of entries) {
        if (values.length >= MAX_SESSION_FILES) break;
        if (entry.isSymbolicLink()) continue;
        const filename = path.join(directory, entry.name);
        if (entry.isDirectory()) await visit(filename, depth + 1);
        else if (entry.isFile() && entry.name.toLowerCase().endsWith('.jsonl')) values.push(filename);
      }
    };
    await visit(root, 0);
    return values;
  }

  async assets() {
    const integration = object(typeof this.integration === 'function' ? this.integration() : this.integration);
    return {
      tools: array(integration.tools).map(name => ({name: String(name)})),
      skills: await scanSkillRoot(String(integration.skillRoot || '')),
    };
  }

  async list({limit = 200} = {}) {
    const root = await this.resolveRoot();
    const assets = await this.assets();
    if (!root.available) return {schema_version: 'code_agent_session_catalog/v1', root, sessions: [], assets};
    const filenames = await this.files(root.path);
    const entries = await Promise.all(filenames.map(async filename => {
      const stat = await fsp.stat(filename);
      const cached = this.headerCache.get(filename);
      const header = cached && cached.size === stat.size && cached.mtime_ms === stat.mtimeMs
        ? cached.header : await readHeader(filename);
      if (!cached || cached.header !== header)
        this.headerCache.set(filename, {size: stat.size, mtime_ms: stat.mtimeMs, header});
      const relative = path.relative(root.path, filename);
      const fallbackId = path.basename(filename, path.extname(filename));
      return {
        source_id: sourceId(relative),
        session_id: safeSessionId(header.metadata.sessionId, fallbackId),
        title: header.title || `Code Agent · ${fallbackId}`,
        project: path.dirname(relative) === '.' ? path.basename(root.path) : path.dirname(relative),
        cwd: cleanPreview(header.metadata.cwd, 500),
        version: cleanPreview(header.metadata.version, 80),
        entrypoint: cleanPreview(header.metadata.entrypoint, 80),
        created_at: iso(header.metadata.timestamp) || stat.birthtime.toISOString(),
        updated_at: stat.mtime.toISOString(),
        size_bytes: stat.size,
      };
    }));
    entries.sort((left, right) => right.updated_at.localeCompare(left.updated_at));
    const bounded = Math.max(1, Math.min(Number(limit) || 200, MAX_SESSION_FILES));
    return {schema_version: 'code_agent_session_catalog/v1', root, sessions: entries.slice(0, bounded), assets};
  }

  sessionPath(root, source) {
    const relative = decodeSourceId(source);
    const filename = path.resolve(root, relative);
    if (!isWithin(root, filename) || !filename.toLowerCase().endsWith('.jsonl')) throw new Error('Code Agent Session 路径越界或类型无效。');
    return filename;
  }

  correlatePlatformRun(session) {
    if (!this.agentRuns) return null;
    try { return {match: 'session_id', run: this.agentRuns.get({sessionId: session.session_id})}; } catch {}
    const catalog = this.agentRuns.list({limit: 500});
    const started = Date.parse(session.created_at || '');
    const finished = Date.parse(session.updated_at || '');
    const match = array(catalog.runs).find(item => {
      const at = Date.parse(item.created_at || '');
      if (!Number.isFinite(at) || !Number.isFinite(started) || !Number.isFinite(finished)) return false;
      if (at < started - 10 * 60 * 1000 || at > finished + 10 * 60 * 1000) return false;
      try { return !session.cwd || this.agentRuns.get({sessionId: item.session_id}).cwd === session.cwd; } catch { return false; }
    });
    if (!match) return null;
    try { return {match: 'cwd_time_overlap', run: this.agentRuns.get({sessionId: match.session_id})}; } catch { return null; }
  }

  async get({sourceId: requestedSourceId} = {}) {
    const catalog = await this.list({limit: MAX_SESSION_FILES});
    if (!catalog.root.available) throw new Error('Code Agent Session 根目录不可用。');
    const summary = catalog.sessions.find(item => item.source_id === requestedSourceId);
    if (!summary) throw new Error('Code Agent Session 不存在或已移出配置目录。');
    const filename = this.sessionPath(catalog.root.path, requestedSourceId);
    const timeline = [];
    const byUuid = new Map();
    const toolUses = new Map();
    const toolStats = new Map();
    const loadedSkills = new Set();
    const parseErrors = [];
    let firstAt = null;
    let lastAt = null;
    let thinkingBlocks = 0;
    let thinkingCharacters = 0;
    let lineCount = 0;
    let normalizedEventCount = 0;
    let sessionMetadata = {};
    for await (const {line, lineNumber} of jsonLines(filename)) {
      lineCount += 1;
      if (lineCount > MAX_EVENTS) throw Object.assign(new Error('Session 记录超过 100,000 条安全解析上限。'), {code: 'AGENT_SESSION_EVENT_LIMIT'});
      let event;
      try { event = JSON.parse(line); } catch {
        if (parseErrors.length < 20) parseErrors.push({line: lineNumber, error: 'JSON 无法解析'});
        continue;
      }
      sessionMetadata = {...sessionMetadata, ...Object.fromEntries(['sessionId', 'cwd', 'entrypoint', 'permissionMode', 'version', 'gitBranch']
        .filter(key => sessionMetadata[key] == null && event[key] != null).map(key => [key, cleanPreview(event[key], 500)]))};
      const timestamp = iso(event.timestamp) || null;
      if (timestamp && (!firstAt || timestamp < firstAt)) firstAt = timestamp;
      if (timestamp && (!lastAt || timestamp > lastAt)) lastAt = timestamp;
      const eventId = safeSessionId(event.uuid, `line-${lineNumber}`);
      const role = cleanPreview(event?.message?.role || (['user', 'assistant', 'system'].includes(event.type) ? event.type : 'event'), 40).toLowerCase();
      const normalized = {
        event_id: eventId,
        parent_id: safeSessionId(event.parentUuid, ''),
        role,
        timestamp,
        sidechain: event.isSidechain === true,
        blocks: [],
      };
      for (const block of contentBlocks(event)) {
        const type = String(block.type || '');
        if (type === 'text') {
          const preview = cleanPreview(block.text, MAX_TEXT_PREVIEW);
          if (preview) normalized.blocks.push({type: 'text', preview, truncated: String(block.text || '').length > MAX_TEXT_PREVIEW});
        } else if (type === 'thinking') {
          const length = String(block.thinking || '').length;
          thinkingBlocks += 1;
          thinkingCharacters += length;
          normalized.blocks.push({type: 'thinking', character_count: length, redacted: true});
        } else if (type === 'tool_use') {
          const toolUseId = safeSessionId(block.id, `tool-${lineNumber}-${normalized.blocks.length + 1}`);
          const name = cleanPreview(block.name, 200) || 'unknown';
          const input = summarizeInput(block.input);
          const value = {type: 'tool_use', tool_use_id: toolUseId, name, input, timestamp, result: null,
            // This is filled from the Agent JSONL timestamps. It is an
            // end-to-end observed span, not proof of time spent inside the
            // tool implementation: Agent scheduling, permission handling,
            // MCP/CLI adaptation and result serialization can all be inside.
            observed_duration_ms: null, duration_ms: null};
          normalized.blocks.push(value);
          toolUses.set(toolUseId, value);
          const stats = toolStats.get(name) || {name, call_count: 0, error_count: 0,
            total_observed_duration_ms: 0, total_duration_ms: 0};
          stats.call_count += 1;
          toolStats.set(name, stats);
          for (const skill of skillReferences(name, block.input, catalog.assets.skills)) loadedSkills.add(skill);
        } else if (type === 'tool_result') {
          const toolUseId = safeSessionId(block.tool_use_id, '');
          const preview = cleanPreview(resultText(block.content), MAX_TOOL_RESULT_PREVIEW);
          const result = {type: 'tool_result', tool_use_id: toolUseId, preview, is_error: block.is_error === true, timestamp,
            truncated: resultText(block.content).length > MAX_TOOL_RESULT_PREVIEW};
          const use = toolUses.get(toolUseId);
          if (use) {
            use.result = result;
            if (timestamp && use.timestamp) {
              use.observed_duration_ms = Math.max(0, Date.parse(timestamp) - Date.parse(use.timestamp));
              // duration_ms remains as a compatibility alias for existing
              // clients; new UI and analysis must use observed_duration_ms.
              use.duration_ms = use.observed_duration_ms;
            }
            const stats = toolStats.get(use.name);
            if (stats) {
              if (result.is_error) stats.error_count += 1;
              if (Number.isFinite(use.observed_duration_ms)) {
                stats.total_observed_duration_ms += use.observed_duration_ms;
                stats.total_duration_ms += use.observed_duration_ms;
              }
            }
          } else normalized.blocks.push(result);
        }
      }
      if (normalized.blocks.length || ['user', 'assistant', 'system'].includes(normalized.role)) {
        normalizedEventCount += 1;
        if (timeline.length < MAX_TIMELINE_EVENTS) {
          timeline.push(normalized);
          byUuid.set(eventId, normalized);
        }
      }
    }
    const platformToolNames = new Set(catalog.assets.tools.map(item => item.name));
    const tools = [...toolStats.values()].map(item => ({...item,
      platform_tool: platformToolNames.has(item.name) || [...platformToolNames].some(name => item.name.endsWith(`__${name}`)),
    })).sort((left, right) => right.call_count - left.call_count || left.name.localeCompare(right.name));
    const rootEvents = timeline.filter(item => !item.parent_id || !byUuid.has(item.parent_id)).length;
    const session = {...summary,
      session_id: safeSessionId(sessionMetadata.sessionId, summary.session_id),
      cwd: sessionMetadata.cwd || summary.cwd,
      version: sessionMetadata.version || summary.version,
      entrypoint: sessionMetadata.entrypoint || summary.entrypoint,
      permission_mode: sessionMetadata.permissionMode || '',
      git_branch: sessionMetadata.gitBranch || '',
      created_at: firstAt || summary.created_at,
      updated_at: lastAt || summary.updated_at,
    };
    const platformRun = this.correlatePlatformRun(session);
    const timing = platformTiming(platformRun);
    return {
      schema_version: 'code_agent_session_detail/v1',
      session,
      metrics: {
        event_count: normalizedEventCount,
        rendered_event_count: timeline.length,
        timeline_truncated: normalizedEventCount > timeline.length,
        root_count: rootEvents,
        tool_call_count: tools.reduce((sum, item) => sum + item.call_count, 0),
        tool_error_count: tools.reduce((sum, item) => sum + item.error_count, 0),
        thinking_block_count: thinkingBlocks,
        thinking_character_count: thinkingCharacters,
        duration_ms: firstAt && lastAt ? Math.max(0, Date.parse(lastAt) - Date.parse(firstAt)) : null,
        parse_error_count: parseErrors.length,
      },
      timeline,
      tools,
      skills: catalog.assets.skills.map(skill => ({...skill, loaded: loadedSkills.has(skill.skill_id)})),
      available_tools: catalog.assets.tools.map(tool => ({...tool, call_count: tools.find(item => item.name === tool.name || item.name.endsWith(`__${tool.name}`))?.call_count || 0})),
      parse_errors: parseErrors,
      platform_run: platformRun,
      platform_timing: timing,
      timing_semantics: {
        agent_jsonl: 'tool_use 到匹配 tool_result 的观察跨度；可能包含 Agent 排队、权限处理、MCP/CLI 适配、平台执行与结果序列化。',
        platform: 'Desktop broker 记录的平台操作计时；duration_scope 标明远端执行时间、broker 墙钟或旧记录未标明口径。RPC 往返包含远端处理，通道冷启是参考基线。',
        additive: false,
      },
      privacy: {thinking_text_exposed: false, text_preview_limit: MAX_TEXT_PREVIEW, tool_result_preview_limit: MAX_TOOL_RESULT_PREVIEW},
    };
  }
}

module.exports = {
  CodeAgentSessionStore,
  jsonLines,
  redact,
};
