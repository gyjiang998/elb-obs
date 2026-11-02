import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';
import './ssh-terminal-workspace.css';
import { createCaptureStudio } from './capture.js';
import { createWorkbenchLayout } from './layout.js';
import { createRemoteFileWorkspace } from './remote-files.js';
import { createActivityRail } from './activity-rail.js';
import { createServerProfileStudio } from './server-profile.js';
import { createSharedPages } from './shared-pages.jsx';
import { createDpvsPerformancePage } from './dpvs-performance.jsx';
import sshTerminalWorkspace from './ssh-terminal-workspace.js';
import sshConnectionProfile from './ssh-connection-profile.cjs';
import terminalShortcuts from './terminal-shortcuts.cjs';
import themePreferences from './theme.cjs';
import startupConnections from './startup-connections.cjs';
import notificationCenterModule from './notification-center.cjs';
import activityRailPreferencesModule from './activity-rail-preferences.cjs';
import agentLongTermMemoryPromptModule from './agent-long-term-memory-prompt.cjs';
import agentMcpConfigurationPromptModule from './agent-mcp-configuration-prompt.cjs';
import automationSettingsModule from '../runtime/automation-settings.cjs';
import { publishWorkbenchNotification } from '../../app/lib/workbench-notifications';

const { createSshTerminalWorkspace } = sshTerminalWorkspace;
const { sameSshConnectionProfile } = sshConnectionProfile;
const { shouldProcessTerminalKey } = terminalShortcuts;
const { THEME_STORAGE_KEY, normalizeTheme, terminalMinimumContrastRatioFor, terminalThemeFor } = themePreferences;
const { startupConnectionPlan } = startupConnections;
const { createNotificationCenter } = notificationCenterModule;
const { readActivityRailPreferences, writeActivityRailPreferences } = activityRailPreferencesModule;
const { AGENT_LONG_TERM_MEMORY_PROMPT, AGENT_INITIALIZATION_PROMPT } = agentLongTermMemoryPromptModule;
const { AGENT_MCP_CONFIGURATION_PROMPT } = agentMcpConfigurationPromptModule;
const { normalizeAutomationSettings } = automationSettingsModule;

const $ = (id) => document.getElementById(id);
const api = window.desktop;
const state = { agent: { state: 'stopped' }, ssh: { state: 'disconnected' }, settings: {}, integration: {} };
const pending = new Set();
const terminals = {};
const subscriptions = [];
let captureStudio;
let serverProfileStudio;
let sharedPages;
let dpvsPerformancePage;
let layout;
let remoteFiles;
let sshWorkspace;
let activityRail;
let lastFocus;
let resizeFrame;
let resizeTimer;
let lastFitAt = -Infinity;
let disposed = false;
let currentPage = 'capture';
let currentSettingsTab = 'agent';
let activeSshConnection = null;
let currentTheme = normalizeTheme(document.documentElement.dataset.theme);
let activityRailPreference = readActivityRailPreferences(localStorage);
const PAGE_LABELS = {
  'dpvs-performance': 'DPVS 性能分析', capture: '负载采样', 'workload-features': '负载特征', 'hardware-models': '硬件模型',
  'optimization-calibration': '寻优校准', 'parallel-modeling': '并行建模',
  'application-flow': '应用流程', 'opportunity-reasoning': '机会推理',
  'source-repositories': '代码仓', 'code-knowledge': '代码知识库',
  'optimization-knowledge': '优化知识库', skills: '技能列表', traces: 'Trace 管理',
  experience: '经验演进', workbench: '资源管理器', 'server-profile': '服务器环境', devices: '本地设备',
};

function elementLabel(node) {
  if (!node?.closest) return '';
  const control = node.closest('button, [role="button"], input, select, textarea, a');
  if (!control) return '';
  return String(control.getAttribute('aria-label') || control.getAttribute('title') || control.textContent || control.placeholder || '')
    .replace(/\s+/g, ' ').trim().slice(0, 160);
}

function notificationRuntimeContext(input = {}) {
  const active = document.activeElement;
  const control = elementLabel(active);
  const section = active?.closest?.('section, article, .card, .panel');
  const sectionLabel = String(section?.querySelector?.('h1, h2, h3, [role="heading"]')?.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 160);
  const visibleLocation = [sectionLabel, control].filter(Boolean).join(' / ');
  const technicalOperation = String(input.operation || '').trim();
  return {
    page: currentPage,
    pageLabel: PAGE_LABELS[currentPage] || currentPage || '工作台',
    operation: visibleLocation
      ? technicalOperation ? `${visibleLocation} [${technicalOperation}]` : visibleLocation
      : technicalOperation || String(input.source || '当前操作'),
    context: {
      notification_source: String(input.source || 'workbench'),
      ui_section: sectionLabel,
      ui_control: control,
      agent_state: String(state.agent?.state || ''),
      ssh_state: String(state.ssh?.state || ''),
      selected_device: String(state.capture?.selectedDeviceSerial || ''),
    },
  };
}

const notificationCenter = createNotificationCenter({
  document,
  window,
  clipboard: api?.invoke
    ? { writeText: (value) => api.invoke('writeClipboard', { text: value }) }
    : window.navigator.clipboard,
  getContext: notificationRuntimeContext,
  onSendToAgent: ({prompt, taskContext}) => submitAgentTask(prompt, taskContext),
  getAutomation: () => normalizeAutomationSettings(state.settings?.automation),
  onAutoRecover: ({prompt, taskContext}) => submitAgentTask(prompt, taskContext),
});
$('agent-long-term-memory-prompt').textContent = AGENT_LONG_TERM_MEMORY_PROMPT;
$('agent-mcp-configuration-prompt').textContent = AGENT_MCP_CONFIGURATION_PROMPT;
document.documentElement.dataset.theme = currentTheme;

function applyTheme(value, { persist = true } = {}) {
  currentTheme = normalizeTheme(value);
  document.documentElement.dataset.theme = currentTheme;
  if (persist) {
    try { localStorage.setItem(THEME_STORAGE_KEY, currentTheme); } catch { /* Keep the in-memory preference. */ }
  }
  for (const [target, pane] of Object.entries(terminals)) {
    pane.terminal.options.theme = terminalThemeFor(currentTheme, target);
    pane.terminal.options.minimumContrastRatio = terminalMinimumContrastRatioFor(currentTheme, target);
    pane.terminal.refresh(0, Math.max(0, pane.terminal.rows - 1));
  }
  sshWorkspace?.applyTheme();
  remoteFiles?.setTheme(currentTheme);
  syncNativeMenuState({ theme: currentTheme });
  scheduleFit();
}

const errorMessage = (error) => String(error?.message || error || '操作失败').replace(/^Error invoking remote method '[^']+': Error: /, '');
const errorCode = (error) => String(error?.code || error?.errorCode || error?.error_code || error?.details?.error_code || '');
function notice(message, tone = '', target = 'notice', context = {}) {
  const element = $(target);
  if (tone === 'error' && message && publishWorkbenchNotification({ tone: 'error', text: message, source: target, ...context })) {
    element.textContent = '';
    element.hidden = true;
    return;
  }
  element.textContent = message;
  element.className = `notice ${tone}`.trim();
  element.hidden = !message;
}
function invoke(method, payload) {
  if (!api) return Promise.reject(new Error('请通过桌面应用打开工作台。'));
  return api.invoke(method, payload);
}
function syncNativeMenuState(value) {
  if (!api) return;
  void invoke('setNativeMenuState', value).catch(() => {});
}
async function action(key, operation, errorTarget = 'notice') {
  if (pending.has(key)) return;
  pending.add(key);
  updateControls();
  notice('', '', errorTarget);
  try { return await operation(); }
  catch (error) {
    notice(errorMessage(error), 'error', errorTarget === 'notice' && !$('settings-drawer').hidden ? 'settings-error' : errorTarget, {
      operation: key,
      errorCode: errorCode(error),
      context: {operation_key: key},
    });
  }
  finally { pending.delete(key); updateControls(); }
}
function stateLabel(target, value) {
  const labels = { connected: '已连接', disconnected: '未连接', connecting: '连接中', running: '运行中', starting: '启动中', stopped: '未启动', error: '连接失败', closed: '已退出' };
  if (value === 'error' && target === 'agent') return '启动失败';
  return labels[value] || value || '未连接';
}
function stateTone(value) {
  if (['connected', 'running'].includes(value)) return 'good';
  if (['connecting', 'starting'].includes(value)) return 'pending';
  return value === 'error' ? 'error' : '';
}
function updateControls() {
  const agentLive = ['running', 'starting'].includes(state.agent.state);
  const sshLive = state.ssh.state === 'connected';
  const realAgentLive = agentLive && !state.agent.demo;
  const sshShellClosed = sshLive && state.ssh.shellState === 'closed';
  const savedSsh = state.settings.ssh || {};
  $('run-demo').disabled = realAgentLive || pending.has('demo') || pending.has('ssh-connect') || pending.has('agent-start');
  $('run-demo').title = realAgentLive ? 'Code Agent 已运行，无需再启动连接演示' : '启动连接测试客户端，验证远程工具执行';
  $('discover-devices').disabled = pending.has('discover');
  $('discover-devices').textContent = pending.has('discover') ? '正在读取…' : '读取设备';
  $('save-settings').disabled = pending.has('settings') || pending.has('agent-start');
  $('settings-start-agent').disabled = pending.has('settings') || pending.has('agent-start');
  $('settings-start-agent').textContent = agentLive ? '重新启动 Agent' : '启动 Agent';
  const initializingAgent = pending.has('agent-initialize');
  $('agent-initialize').disabled = initializingAgent || pending.has('agent-start') || pending.has('settings');
  $('agent-initialize').setAttribute('aria-busy', String(initializingAgent));
  $('agent-initialize').title = initializingAgent ? '正在初始化 Code Agent…' : '初始化 Code Agent';
  updateSshPasswordStatus();
  for (const target of ['agent', 'ssh']) {
    const badge = $(`${target}-status`);
    badge.textContent = stateLabel(target, state[target].state);
    badge.className = `state-badge ${stateTone(state[target].state)}`.trim();
  }
  if (sshShellClosed) $('ssh-status').textContent = '工具在线';
  const agentConfig = state.settings.agent || {};
  const sshConfig = savedSsh;
  const activeAgentExecutable = agentLive ? state.agent.executable || agentConfig.executable : agentConfig.executable;
  const activeSsh = sshLive && state.ssh.host ? state.ssh : sshConfig;
  if ($('agent-summary')) $('agent-summary').textContent = state.agent.state === 'running' && state.agent.demo ? '连接测试客户端' : activeAgentExecutable || '未配置';
  $('agent-executable-label').textContent = state.agent.state === 'running' && state.agent.demo ? '连接测试客户端 · 不是 AI Agent' : activeAgentExecutable || '本地可执行程序 / CLI';
  if ($('ssh-summary')) $('ssh-summary').textContent = activeSsh.host ? `${activeSsh.username || 'user'}@${activeSsh.host}:${activeSsh.port || 22}` : '未配置';
  $('ssh-location-label').textContent = state.ssh.state === 'connected' ? state.ssh.cwd || sshConfig.cwd || '~' : '远程服务器 / 交互式终端';
  sshWorkspace?.refreshControls();
  remoteFiles?.updateConnection(state.ssh);
  captureStudio?.updateAgentState();
  serverProfileStudio?.updateConnection();
}
function hydrateAgent(config = {}) {
  $('agent-executable').value = config.executable || '';
  $('agent-args').value = JSON.stringify(config.args || [], null, 2);
  $('agent-cwd').value = config.cwd || '';
  $('agent-environment').value = JSON.stringify(config.environment || {}, null, 2);
  $('agent-submit-mode').value = config.submitMode || config.submit_mode || 'bracketed_paste_cr';
  $('agent-submit-delay').value = config.submitDelayMs ?? config.submit_delay_ms ?? 1000;
  $('agent-startup-delay').value = config.startupDelayMs ?? config.startup_input_delay_ms ?? 800;
  updateSubmitDelay();
}
function hydrateSettings(settings = {}) {
  hydrateAgent(settings.agent);
  const ssh = settings.ssh || {};
  $('ssh-host').value = ssh.host || '';
  $('ssh-port').value = ssh.port || 22;
  $('ssh-username').value = ssh.username || '';
  $('ssh-cwd').value = ssh.cwd || '/srv/workspace';
  $('ssh-auth').value = ssh.auth || 'password';
  $('ssh-password').value = '';
  $('ssh-key-path').value = ssh.privateKeyPath || '';
  $('hdc-path').value = settings.hdcPath || 'hdc';
  const automation = normalizeAutomationSettings(settings.automation);
  $('automation-auto-recover-errors').checked = automation.autoRecoverErrors;
  $('automation-allow-device-operations').checked = automation.allowDeviceOperations;
  $('automation-allow-server-operations').checked = automation.allowServerOperations;
  $('activity-rail-expand-on-hover').checked = activityRail?.isEnabled() ?? activityRailPreference.expandOnHover;
  serverProfileStudio?.hydrate(settings.serverProfile, settings.serverProfileTarget || settings.ssh, settings.serverProfileScopeId);
  updateAuthFields();
}
function automationConfig() {
  return normalizeAutomationSettings({
    autoRecoverErrors: $('automation-auto-recover-errors').checked,
    allowDeviceOperations: $('automation-allow-device-operations').checked,
    allowServerOperations: $('automation-allow-server-operations').checked,
  });
}
function updateSshPasswordStatus() {
  const ssh = state.settings.ssh || {};
  const credential = ssh.passwordCredential || {};
  const input = $('ssh-password');
  const status = $('ssh-password-status');
  const forget = $('forget-ssh-password');
  input.placeholder = credential.saved ? '已安全保存；留空继续使用' : '输入密码';
  forget.disabled = !credential.saved || pending.has('credential-forget');
  forget.hidden = $('ssh-auth').value !== 'password';
  if (credential.error) status.textContent = credential.error;
  else if (credential.saved) status.textContent = '已为当前服务器和用户名安全保存密码；留空即可复用，输入新密码将更新。';
  else status.textContent = '密码将由操作系统安全存储加密保存在本机，页面不会回显。';
}
function applyState(value, { hydrate = false } = {}) {
  if (!value) return;
  for (const key of ['agent', 'ssh', 'settings', 'integration']) if (value[key]) state[key] = value[key];
  if (value.platform) document.documentElement.dataset.platform = value.platform;
  if (value.platform) $('platform-label').textContent = `${({ win32: 'Windows', darwin: 'macOS', linux: 'Linux' })[value.platform] || value.platform} · HDC / PTY / SSH`;
  if (hydrate) hydrateSettings(state.settings);
  else if (value.settings) serverProfileStudio?.syncSettings(state.settings);
  sharedPages?.syncContext();
  updateSshPasswordStatus();
  updateIntegration();
  updateControls();
  if (Array.isArray(state.ssh.terminals)) sshWorkspace?.reconcile(state.ssh.terminals, { preferredId: state.ssh.primaryTerminalId });
  scheduleFit();
  if (value.capture) void captureStudio?.initialize(value.capture);
}
function parseJson(id, expected, name) {
  let value;
  try { value = JSON.parse($(id).value || (expected === 'array' ? '[]' : '{}')); }
  catch { throw new Error(`${name}不是有效的 JSON。`); }
  if (expected === 'array' ? !Array.isArray(value) || value.some((v) => typeof v !== 'string') : value === null || Array.isArray(value) || typeof value !== 'object' || Object.values(value).some((v) => typeof v !== 'string')) throw new Error(`${name}需要${expected === 'array' ? '字符串数组' : '字符串键值对象'}。`);
  return value;
}
function agentConfig({ demo = false } = {}) {
  return { executable: $('agent-executable').value.trim(), args: parseJson('agent-args', 'array', '启动参数'), cwd: $('agent-cwd').value.trim(), environment: parseJson('agent-environment', 'object', '环境变量'), submitMode: $('agent-submit-mode').value, submitDelayMs: Number($('agent-submit-delay').value), startupDelayMs: Number($('agent-startup-delay').value), demo };
}
function normalizedAgentConfiguration(config = {}) {
  return {
    executable: String(config.executable || '').trim(),
    args: Array.isArray(config.args) ? config.args.map(String) : [],
    cwd: String(config.cwd || '').trim(),
    environment: Object.fromEntries(Object.entries(config.environment || {}).sort(([left], [right]) => left.localeCompare(right))),
    submitMode: config.submitMode || config.submit_mode || 'bracketed_paste_cr',
    submitDelayMs: Number(config.submitDelayMs ?? config.submit_delay_ms ?? 1000),
    startupDelayMs: Number(config.startupDelayMs ?? config.startup_input_delay_ms ?? 800),
  };
}
function sameAgentConfiguration(left, right) {
  return JSON.stringify(normalizedAgentConfiguration(left)) === JSON.stringify(normalizedAgentConfiguration(right));
}
function sshConfig() {
  const port = Number($('ssh-port').value);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('SSH 端口需要在 1–65535 之间。');
  return { host: $('ssh-host').value.trim(), port, username: $('ssh-username').value.trim(), cwd: $('ssh-cwd').value.trim() || '~', auth: $('ssh-auth').value, password: $('ssh-password').value, privateKeyPath: $('ssh-key-path').value.trim(), passphrase: $('ssh-passphrase').value };
}
function updateAuthFields() {
  $('ssh-password-fields').hidden = $('ssh-auth').value !== 'password';
  $('ssh-key-fields').hidden = $('ssh-auth').value !== 'key';
  updateSshPasswordStatus();
}
function updateSubmitDelay() {
  const minimum = 100;
  $('agent-submit-delay').min = String(minimum);
  if (Number($('agent-submit-delay').value) < minimum) $('agent-submit-delay').value = String(minimum);
}
function setPage(page) {
  for (const name of ['dpvs-performance', 'capture', 'workload-features', 'hardware-models', 'optimization-calibration', 'parallel-modeling', 'application-flow', 'opportunity-reasoning', 'source-repositories', 'code-knowledge', 'optimization-knowledge', 'skills', 'traces', 'experience', 'workbench', 'server-profile', 'devices']) $(`${name}-page`).hidden = page !== name;
  document.querySelectorAll('[data-page]').forEach((button) => {
    const active = button.dataset.page === page;
    button.classList.toggle('active', active);
    if (active) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  currentPage = page;
  scheduleFit();
  if (page === 'workbench') remoteFiles?.layout();
  captureStudio?.onPage(page);
  serverProfileStudio?.onPage(page);
  sharedPages?.onPage(page);
  dpvsPerformancePage?.onPage(page);
}
function setTab(tab) {
  currentSettingsTab = tab;
  for (const name of ['agent', 'ssh', 'server-profile', 'integration', 'automation', 'interface']) {
    $(`settings-${name}`).hidden = name !== tab;
    $(`tab-${name}`).classList.toggle('active', name === tab);
    $(`tab-${name}`).setAttribute('aria-selected', String(name === tab));
  }
  $('settings-start-agent').hidden = tab !== 'agent';
  if (tab === 'interface') $('activity-rail-expand-on-hover').checked = activityRail?.isEnabled() ?? activityRailPreference.expandOnHover;
  if (tab === 'automation') {
    const automation = normalizeAutomationSettings(state.settings?.automation);
    $('automation-auto-recover-errors').checked = automation.autoRecoverErrors;
    $('automation-allow-device-operations').checked = automation.allowDeviceOperations;
    $('automation-allow-server-operations').checked = automation.allowServerOperations;
  }
}
function openSettings(tab = 'agent') {
  lastFocus = document.activeElement;
  activityRail?.closeUntilPointerLeaves();
  setTab(tab);
  $('settings-backdrop').hidden = false;
  $('settings-drawer').hidden = false;
  $('close-settings').focus();
}
function closeSettings() {
  $('settings-backdrop').hidden = true;
  $('settings-drawer').hidden = true;
  lastFocus?.focus?.();
  activityRail?.close();
  scheduleFit();
}
function updateIntegration() {
  const integration = state.integration || {};
  $('integration-config-path').value = integration.mcpConfigPath || '';
  $('integration-cli-path').value = integration.cliPath || '';
  $('integration-details').textContent = JSON.stringify(integration, null, 2);
  $('open-mcp-config').disabled = !integration.mcpConfigPath;
  $('open-integration-folder').disabled = !integration.cliPath;
}
function receiveEvent(event) {
  if (disposed || !event) return;
  if (event.type === 'agent-recovery') void notificationCenter.handleRecoveryEvent(event);
  captureStudio?.onEvent(event);
  if (event.type === 'terminal' && event.target === 'agent') terminals.agent.terminal.write(String(event.data || ''));
  if (event.type === 'status' && ['agent', 'ssh'].includes(event.target)) {
    state[event.target] = { ...state[event.target], ...event };
    if (event.target === 'ssh') sharedPages?.syncContext();
    updateControls();
    if (event.state === 'error' && event.message) notice(event.message, 'error');
    if (event.target === 'agent' && ['error', 'stopped'].includes(event.state)) {
      notificationCenter.cancelActiveRecovery(event.message || 'Code Agent 已停止，本次自动恢复未完成。');
    }
    if (['connected', 'running'].includes(event.state)) {
      if (terminals[event.target]) terminals[event.target].lastSize = null;
      scheduleFit();
    }
  }
  if (event.type === 'ssh-terminal' && event.target === 'ssh') {
    state.ssh = {
      ...state.ssh,
      ...(Array.isArray(event.terminals) ? { terminals: event.terminals } : {}),
      ...(event.terminalCount !== undefined ? { terminalCount: event.terminalCount } : {}),
      ...(event.primaryTerminalId !== undefined ? { primaryTerminalId: event.primaryTerminalId } : {}),
      ...(event.shellState !== undefined ? { shellState: event.shellState } : {}),
    };
    updateControls();
  }
  sshWorkspace?.handleEvent(event);
}
function scheduleFit() {
  if (disposed || resizeFrame || resizeTimer) return;
  const wait = Math.max(0, 60 - (performance.now() - lastFitAt));
  if (wait) {
    resizeTimer = setTimeout(() => { resizeTimer = undefined; scheduleFit(); }, wait);
    return;
  }
  resizeFrame = requestAnimationFrame(() => {
    resizeFrame = undefined;
    if (disposed) return;
    lastFitAt = performance.now();
    for (const pane of Object.values(terminals)) {
      const { fit, terminal, target } = pane;
      const host = $(`${target}-terminal`);
      if (!host.clientWidth || !host.clientHeight || !host.getClientRects().length) continue;
      try {
        fit.fit();
        const size = `${terminal.cols}:${terminal.rows}`;
        if (terminal.cols > 0 && terminal.rows > 0 && pane.lastSize !== size) {
          pane.lastSize = size;
          void invoke('terminalResize', { target, cols: terminal.cols, rows: terminal.rows }).catch(() => { pane.lastSize = null; });
        }
      } catch { /* A collapsed host is fitted again when restored. */ }
    }
    void sshWorkspace?.fitActive();
  });
}
function createTerminal(target) {
  const terminal = new Terminal({ cursorBlink: true, cursorStyle: 'bar', fontFamily: '"Cascadia Code", "Cascadia Mono", Consolas, "Microsoft YaHei UI", monospace', fontSize: 12, lineHeight: 1.35, scrollback: 6000, allowProposedApi: false, minimumContrastRatio: terminalMinimumContrastRatioFor(currentTheme, target), theme: terminalThemeFor(currentTheme, target) });
  const fit = new FitAddon(); terminal.loadAddon(fit); terminal.open($(`${target}-terminal`));
  // Returning false keeps xterm from translating the shortcut into terminal
  // input. Chromium then emits the native copy/paste event, which xterm's own
  // clipboard handlers process without requesting clipboard-read permission.
  terminal.attachCustomKeyEventHandler((event) => shouldProcessTerminalKey(event, terminal.hasSelection()));
  terminals[target] = { terminal, fit, target };
  terminal.writeln('\x1b[90m本地 Agent 终端\r\n\r\n配置并保存你的 CLI 后，Agent 会自动启动。\r\n首次体验可点击「运行连接演示」。\x1b[0m\r\n');
  subscriptions.push(terminal.onData((data) => {
    if (state.agent.state !== 'running') return;
    void invoke('terminalInput', { target, data }).catch((error) => notice(errorMessage(error), 'error'));
  }));
}
async function browse(id, kind = 'file') {
  const selected = await invoke('choosePath', { kind });
  if (selected) $(id).value = typeof selected === 'string' ? selected : selected.path || selected.filePaths?.[0] || '';
}
function savedSshConfig() {
  const config = state.settings.ssh || {};
  return {
    host: String(config.host || '').trim(),
    port: Number(config.port) || 22,
    username: String(config.username || '').trim(),
    cwd: String(config.cwd || '~').trim() || '~',
    auth: config.auth || 'password',
    password: '',
    privateKeyPath: String(config.privateKeyPath || '').trim(),
    passphrase: '',
  };
}
async function connect(config = sshConfig()) {
  layout?.showTerminal();
  if (!config.host || !config.username) { openSettings('ssh'); throw new Error('请填写服务器地址和用户名。'); }
  if (activeSshConnection && sameSshConnectionProfile(activeSshConnection.config, config)) return activeSshConnection.promise;
  const operation = { config, promise: null };
  operation.promise = (async () => {
    const result = await invoke('connectSsh', config);
    applyState(result);
    await sshWorkspace?.initialize(result?.ssh || state.ssh);
    $('ssh-password').value = '';
    if (state.ssh.primaryTerminalId) sshWorkspace?.activate(state.ssh.primaryTerminalId);
    return result;
  })();
  activeSshConnection = operation;
  try { return await operation.promise; }
  finally { if (activeSshConnection === operation) activeSshConnection = null; }
}
async function ensureSshConnected() {
  const config = savedSshConfig();
  if (!config.host || !config.username) {
    openSettings('ssh');
    throw new Error('请先在连接配置中填写 SSH 服务器地址和用户名。');
  }
  if (state.ssh.state === 'connected' && sameSshConnectionProfile(state.ssh, config)) return { reconnected: false };
  await connect(config);
  return { reconnected: true };
}
async function startConfiguredAgent(config) {
  if (!config.executable) throw new Error('请填写你的 Code Agent 启动命令，或使用“运行连接演示”验证链路。');
  layout?.showAgent();
  terminals.agent.terminal.clear();
  applyState(await invoke('startAgent', config));
  terminals.agent.terminal.focus();
}
async function saveConnectionSettings({ forceAgentStart = false } = {}) {
  if (currentSettingsTab === 'interface') {
    activityRailPreference = writeActivityRailPreferences(localStorage, {
      expandOnHover: $('activity-rail-expand-on-hover').checked,
    });
    activityRail?.setEnabled(activityRailPreference.expandOnHover);
    $('settings-save-status').textContent = '界面偏好已保存到当前电脑';
    notice('界面偏好已保存。', 'success', 'settings-error');
    return;
  }
  const savesAgent = currentSettingsTab === 'agent' || forceAgentStart;
  const savesSsh = currentSettingsTab === 'ssh';
  const nextAgent = savesAgent ? agentConfig() : state.settings.agent || {};
  const nextSsh = savesSsh ? sshConfig() : savedSshConfig();
  const previousAgent = state.settings.agent || {};
  const agentLive = ['running', 'starting'].includes(state.agent.state);
  const shouldSwitchSsh = savesSsh
    && state.ssh.state === 'connected'
    && !sameSshConnectionProfile(state.ssh, nextSsh);
  const shouldAutoStart = savesAgent
    && Boolean(nextAgent.executable)
    && (!agentLive || state.agent.demo || !sameAgentConfiguration(previousAgent, nextAgent));
  let payload = {};
  if (savesAgent) payload = { agent: nextAgent };
  else if (savesSsh) payload = { ssh: nextSsh };
  else if (currentSettingsTab === 'server-profile') payload = { serverProfile: serverProfileStudio.readProfile(), serverProfileTarget: serverProfileStudio.readTarget() };
  else if (currentSettingsTab === 'automation') payload = { automation: automationConfig() };
  const result = await invoke('saveSettings', payload);
  applyState(result);
  if (currentSettingsTab === 'automation') notificationCenter.revalidateAutomation();
  serverProfileStudio.syncSettings(result?.settings || state.settings, { force: true });
  if (savesSsh) $('ssh-password').value = '';
  $('settings-save-status').textContent = savesSsh ? '已保存 · SSH 密码由系统安全存储保护' : '配置已保存';
  if (shouldSwitchSsh) {
    try { await connect(nextSsh); }
    catch (error) {
      try { applyState(await invoke('getState')); } catch { /* Preserve the connection error. */ }
      throw new Error(`配置已保存，但 SSH 切换失败：${errorMessage(error)}`);
    }
  }
  const launchAgent = forceAgentStart || shouldAutoStart;
  if (launchAgent) {
    try { await startConfiguredAgent(nextAgent); }
    catch (error) {
      try { applyState(await invoke('getState')); } catch { /* Preserve the launch error. */ }
      throw new Error(`配置已保存，但 Code Agent 启动失败：${errorMessage(error)}`);
    }
  }
  let message = currentSettingsTab === 'automation' ? '自动恢复与授权配置已保存。' : '连接配置已保存。';
  if (launchAgent) message = `Code Agent 配置已保存并${agentLive ? '重新启动' : '启动'}。`;
  else if (shouldSwitchSsh) message = 'SSH 配置已保存并切换连接。';
  else if (savesAgent && !nextAgent.executable) message = 'Code Agent 配置已保存；填写启动命令后可启动。';
  notice(message, 'success', 'settings-error');
}
async function autoStartConnections() {
  const plan = startupConnectionPlan(state);
  const failures = [];
  if (plan.connectSsh) {
    pending.add('ssh-connect');
    updateControls();
    try { await connect(); }
    catch (error) { failures.push(`SSH：${errorMessage(error)}`); }
    finally { pending.delete('ssh-connect'); updateControls(); }
  }

  if (plan.startAgent) {
    pending.add('agent-start');
    updateControls();
    try {
      terminals.agent.terminal.clear();
      applyState(await invoke('startAgent', agentConfig()));
    } catch (error) { failures.push(`Code Agent：${errorMessage(error)}`); }
    finally { pending.delete('agent-start'); updateControls(); }
  }

  if (plan.settingsTab) {
    openSettings(plan.settingsTab);
    const missing = [...plan.missingSsh, ...plan.missingAgent].join('、');
    const failed = failures.length ? `；自动连接结果：${failures.join('；')}` : '';
    notice(`请先完成连接配置：${missing}${failed}`, failures.length ? 'error' : '', 'settings-error');
  } else if (failures.length) notice(`自动连接未完成。${failures.join('；')}`, 'error');
}
async function submitAgentTask(text, taskContext = null) {
  layout?.showAgent();
  if (state.agent.state !== 'running' || state.agent.demo) {
    const config = agentConfig();
    if (!config.executable) { openSettings('agent'); throw new Error('请先配置自己的 Code Agent 启动命令，再提交任务。'); }
    applyState(await invoke('startAgent', config));
  }
  await invoke('submitAgent', { text, taskContext });
}
function renderDevices(result) {
  const devices = Array.isArray(result) ? result : result?.devices || [];
  captureStudio?.updateDevices(result);
  const list = $('device-list'); list.replaceChildren();
  $('device-count').textContent = String(devices.length);
  $('device-scan-time').textContent = `${new Date().toLocaleTimeString('zh-CN', { hour12: false })} 更新${result.durationMs !== undefined ? ` · ${result.durationMs} ms` : ''}`;
  if (!devices.length) {
    const empty = document.createElement('div'); empty.className = 'device-empty';
    const heading = document.createElement('h3'); heading.textContent = 'HDC 已返回，当前没有已连接设备';
    const detail = document.createElement('p'); detail.textContent = '检查 USB 连接和设备调试授权后，再次读取设备。';
    empty.append(heading, detail); list.append(empty); return;
  }
  for (const device of devices) {
    const serial = typeof device === 'string' ? device : device.serial || device.deviceSerial || device.device_serial || device.id || '未知序列号';
    const row = document.createElement('div'); row.className = 'device-row';
    const icon = document.createElement('span'); icon.className = 'device-glyph'; icon.textContent = '▯';
    const details = document.createElement('div'); const heading = document.createElement('h3'); heading.textContent = serial;
    const label = document.createElement('p'); label.textContent = `${typeof device === 'object' && device.name ? `${device.name} · ` : ''}本机 HDC 直连`;
    details.append(heading, label);
    const status = document.createElement('span'); status.className = 'state-badge good'; status.textContent = '已发现';
    const extract = document.createElement('button'); extract.type = 'button'; extract.className = 'button small-button'; extract.textContent = '提取硬件模型';
    extract.addEventListener('click', () => action(`hardware-extract:${serial}`, async () => {
      const hdcPath = $('hdc-path').value.trim() || 'hdc';
      notice('正在读取设备 CPU 拓扑、频率域与 OPP…', '', 'device-notice');
      await sharedPages.extractHardwareFromDevice(serial, hdcPath);
    }, 'device-notice'));
    row.append(icon, details, status, extract); list.append(row);
  }
}

createTerminal('agent');
layout = createWorkbenchLayout({ onResize: scheduleFit, onStateChange: syncNativeMenuState });
sshWorkspace = createSshTerminalWorkspace({
  tabList: $('ssh-terminal-tabs'),
  paneContainer: $('ssh-terminal'),
  addButton: $('new-ssh-terminal'),
  invoke,
  Terminal,
  FitAddon,
  terminalMinimumContrastRatioFor,
  terminalThemeFor,
  getTheme: () => currentTheme,
  notice,
  isConnected: () => state.ssh.state === 'connected',
  ensureConnected: ensureSshConnected,
  shouldProcessTerminalKey,
});
applyTheme(currentTheme, { persist: false });
remoteFiles = createRemoteFileWorkspace({
  invoke,
  uploadDroppedFiles: api?.uploadDroppedFiles,
  onError: (error) => notice(errorMessage(error), 'error'),
});
remoteFiles.setTheme(currentTheme);
serverProfileStudio = createServerProfileStudio({ invoke, getAppState: () => state, openSettings, submitTask: submitAgentTask });
sharedPages = createSharedPages({ invoke, getAppState: () => state, setPage, submitTask: submitAgentTask });
dpvsPerformancePage = createDpvsPerformancePage({container: $('dpvs-performance-root'), invoke, submitTask: submitAgentTask});
activityRail = createActivityRail({
  sidebar: $('sidebar-panel'),
  enabled: activityRailPreference.expandOnHover,
});
const observer = new ResizeObserver(scheduleFit);
observer.observe($('agent-terminal'));
observer.observe($('ssh-terminal'));
if (api) {
  subscriptions.push({ dispose: api.onEvent(receiveEvent) });
  subscriptions.push({ dispose: api.onMenuAction((command) => {
    if (command?.type === 'theme') applyTheme(command.theme);
    else if (command?.type === 'layout-pane' && command.pane === 'agent') layout?.setAgentVisible(Boolean(command.visible));
    else if (command?.type === 'layout-pane' && command.pane === 'terminal') layout?.setTerminalVisible(Boolean(command.visible));
    else if (command?.type === 'layout-reset') layout?.reset();
    else if (command?.type === 'open-settings') openSettings(command.tab);
  }) });
}
document.querySelectorAll('[data-page]').forEach((button) => button.addEventListener('click', () => {
  const nextPage = button.dataset.page;
  setPage(nextPage);
}));
document.querySelectorAll('[data-tab]').forEach((button) => button.addEventListener('click', () => setTab(button.dataset.tab)));
let titlebarMenuHoverTimer;
async function openTitlebarMenu(button) {
  window.clearTimeout(titlebarMenuHoverTimer);
  if (button.getAttribute('aria-expanded') === 'true') return;
  const bounds = button.getBoundingClientRect();
  button.setAttribute('aria-expanded', 'true');
  try { await invoke('showTitlebarMenu', { menu: button.dataset.titlebarMenu, x: bounds.left, y: bounds.bottom }); }
  catch (error) { notice(errorMessage(error), 'error'); }
  finally { button.setAttribute('aria-expanded', 'false'); }
}
document.querySelectorAll('[data-titlebar-menu]').forEach((button) => {
  button.addEventListener('click', () => void openTitlebarMenu(button));
  button.addEventListener('mouseenter', () => {
    window.clearTimeout(titlebarMenuHoverTimer);
    titlebarMenuHoverTimer = window.setTimeout(() => void openTitlebarMenu(button), 140);
  });
  button.addEventListener('mouseleave', () => window.clearTimeout(titlebarMenuHoverTimer));
});
$('agent-initialize').addEventListener('click', () => action('agent-initialize', async () => {
  await submitAgentTask(AGENT_INITIALIZATION_PROMPT, {task_kind: 'agent_initialization'});
  notificationCenter.show({
    tone: 'success',
    source: 'agent-initialize',
    dedupeKey: 'agent-initialize-submitted',
    text: 'Code Agent 初始化任务已发送（快速模式）；工具和 Skill 将在真实任务中按需验证。',
  });
}));
$('agent-settings').addEventListener('click', () => openSettings('agent'));
$('ssh-settings').addEventListener('click', () => openSettings('ssh'));
$('connection-settings-nav').addEventListener('click', () => openSettings('agent'));
$('close-settings').addEventListener('click', closeSettings);
$('settings-backdrop').addEventListener('click', closeSettings);
$('ssh-auth').addEventListener('change', updateAuthFields);
$('agent-submit-mode').addEventListener('change', updateSubmitDelay);
$('agent-submit-delay').addEventListener('change', updateSubmitDelay);
$('browse-agent').addEventListener('click', () => action('browse', () => browse('agent-executable'), 'settings-error'));
$('browse-agent-cwd').addEventListener('click', () => action('browse', () => browse('agent-cwd', 'directory'), 'settings-error'));
$('browse-ssh-key').addEventListener('click', () => action('browse', () => browse('ssh-key-path'), 'settings-error'));
$('browse-hdc').addEventListener('click', () => action('browse', () => browse('hdc-path'), 'device-notice'));
$('import-manifest').addEventListener('click', () => action('manifest', async () => {
  const imported = await invoke('importManifest');
  if (!imported) return;
  hydrateAgent(imported.agent || imported);
  notice(imported.importNote || `已导入${imported.importedName ? ` ${imported.importedName}` : ' Agent 配置'}。确认本机可执行文件和工作目录后保存。`, 'success', 'settings-error');
}, 'settings-error'));
$('save-settings').addEventListener('click', () => action('settings', saveConnectionSettings, 'settings-error'));
$('settings-start-agent').addEventListener('click', () => action('agent-start', () => saveConnectionSettings({ forceAgentStart: true }), 'settings-error'));
$('forget-ssh-password').addEventListener('click', () => action('credential-forget', async () => {
  const result = await invoke('forgetSshPassword');
  applyState(result);
  $('ssh-password').value = '';
  $('settings-save-status').textContent = '已删除当前服务器的已保存密码';
  notice('已忘记当前 SSH 密码；下次连接需要重新输入。', 'success', 'settings-error');
}, 'settings-error'));
$('run-demo').addEventListener('click', () => action('demo', async () => {
  layout?.showAgent();
  layout?.showTerminal();
  if (['running', 'starting'].includes(state.agent.state) && !state.agent.demo) throw new Error('Code Agent 已运行，无需再启动连接演示。');
  if (state.ssh.state !== 'connected') await connect();
  terminals.agent.terminal.clear();
  applyState(await invoke('startAgent', { ...state.settings.agent, demo: true }));
  await invoke('submitAgent', { text: '验证远程工作目录、系统信息和目录内容。' });
  notice('连接测试已提交：客户端将执行 pwd、uname -a 和 ls -la，输出显示在右侧助手与 Agent 执行终端。', 'success');
}));
$('discover-devices').addEventListener('click', () => action('discover', async () => {
  const hdcPath = $('hdc-path').value.trim() || 'hdc';
  await invoke('saveSettings', { hdcPath });
  const result = await invoke('discoverDevices', { hdcPath });
  renderDevices(result);
}, 'device-notice'));
$('open-mcp-config').addEventListener('click', () => action('open-config', () => invoke('openPath', { path: state.integration.mcpConfigPath }), 'settings-error'));
$('open-integration-folder').addEventListener('click', () => action('open-cli', () => invoke('openPath', { path: state.integration.cliPath }), 'settings-error'));
$('copy-integration').addEventListener('click', () => action('copy-integration', async () => { await invoke('writeClipboard', {text: JSON.stringify(state.integration, null, 2)}); notice('已复制工具集成信息。', 'success', 'settings-error'); }, 'settings-error'));
$('copy-agent-memory-prompt').addEventListener('click', () => action('copy-agent-memory-prompt', async () => {
  await invoke('writeClipboard', {text: AGENT_LONG_TERM_MEMORY_PROMPT});
  notice('长期记忆系统提示已复制；请交给你的 Agent 保存一次。', 'success', 'settings-error');
}, 'settings-error'));
$('copy-agent-mcp-configuration-prompt').addEventListener('click', () => action('copy-agent-mcp-configuration-prompt', async () => {
  await invoke('writeClipboard', {text: AGENT_MCP_CONFIGURATION_PROMPT});
  notice('原生 MCP 配置提示已复制；请交给你的 Code Agent 执行。', 'success', 'settings-error');
}, 'settings-error'));
document.addEventListener('keydown', (event) => {
  if ($('settings-drawer').hidden) return;
  if (event.key === 'Escape') { event.preventDefault(); closeSettings(); }
  if (event.key === 'Tab') {
    const nodes = [...$('settings-drawer').querySelectorAll('button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled)')].filter((node) => node.getClientRects().length);
    const first = nodes[0]; const last = nodes.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
});
let rendererDisposed = false;
function disposeRenderer() {
  if (rendererDisposed) return;
  rendererDisposed = true;
  disposed = true; cancelAnimationFrame(resizeFrame); clearTimeout(resizeTimer); observer.disconnect();
  layout?.dispose();
  activityRail?.dispose();
  remoteFiles?.dispose();
  captureStudio?.dispose();
  sharedPages?.dispose();
  dpvsPerformancePage?.dispose();
  sshWorkspace?.dispose();
  notificationCenter.dispose();
  for (const subscription of subscriptions) subscription.dispose?.();
  for (const { terminal } of Object.values(terminals)) terminal.dispose();
}
window.addEventListener('beforeunload', (event) => {
  if (!remoteFiles?.hasUnsavedChanges()) return;
  event.preventDefault();
  event.returnValue = '';
});
window.addEventListener('pagehide', disposeRenderer);
captureStudio = createCaptureStudio({ invoke, getAppState: () => state, setPage, openSettings, submitTask: async (text, taskContext) => {
  await submitAgentTask(text, taskContext);
}, analyzeTrace: (traceId, stepId) => sharedPages.openTraceInFeatures(traceId, stepId) });
setPage('capture');
void action('initialize', async () => {
  const initial = await invoke('getState');
  applyState(initial, { hydrate: true });
  await sshWorkspace.initialize(initial.ssh);
  await captureStudio.initialize(initial.capture);
  await autoStartConnections();
});
