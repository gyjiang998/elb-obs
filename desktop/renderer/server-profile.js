import './server-profile.css';
import serverProfileTarget from './server-profile-target.cjs';
import { publishWorkbenchNotification } from '../../app/lib/workbench-notifications';

const { normalizeSshTarget, hasSshTarget, sameSshTarget, sshTargetLabel } = serverProfileTarget;

const $ = (id) => document.getElementById(id);
const PROFILE_SCHEMA = 'server_profile_report/v1';
const OPENHARMONY_SURFACES = ['EGL', 'OpenGLES', 'Vulkan Headers', 'musl', 'Graphic', 'Multimedia', 'AI'];
const MANAGED_PATHS = [
  ['state', '运行状态'], ['cache', '构建缓存'], ['assets', '运行资产'], ['tmp', '临时空间'],
  ['logs', '执行日志'], ['run', '运行目录'], ['envFile', '环境文件'],
];
const DEFAULT_CHECKS = [
  ['workerRepoRoot', '异构使能仓', '源码'], ['sourceRoots', '代码分析源码根', '源码'],
  ['openHarmonySdkRoot', 'OpenHarmony 头文件资源', 'SDK'], ['llvmBinRoot', 'LLVM 工具链', '工具链'],
  ['glslangBin', 'glslang 编译器', '工具链'], ['traceStreamer', 'TraceStreamer', '分析'],
  ['runtimeLibraryRoot', '真机运行库', '运行库'], ['pythonBin', 'Python 运行时', '工具链'],
  ['managedRoot', '平台托管目录', '工作台'],
];

const messageOf = (error) => String(error?.message || error || '操作失败').replace(/^Error invoking remote method '[^']+': Error: /, '');
const errorCodeOf = (error) => String(error?.code || error?.errorCode || error?.error_code || error?.details?.error_code || '');
const displayPath = (value) => String(value || '').trim() || '未配置';
const profileSignature = (profile) => JSON.stringify(profile);

function expectedManagedRoot(profileId, scopeId = 'unscoped') {
  return `$HOME/.local/share/heterogeneous-workbench/server-profiles/targets/${scopeId}/${profileId || 'default'}`;
}

function normalizeStatus(value) {
  if (value === true) return 'ready';
  if (value === false) return 'missing';
  const status = String(value || 'skipped').toLowerCase();
  if (['ready', 'ok', 'passed', 'present', 'success'].includes(status)) return 'ready';
  if (['missing', 'failed', 'error', 'invalid', 'blocked'].includes(status)) return 'missing';
  if (['warning', 'partial', 'degraded'].includes(status)) return 'warning';
  return 'skipped';
}

function statusText(status) {
  return ({ ready: '就绪', missing: '缺失', warning: '需关注', skipped: '待检查' })[normalizeStatus(status)];
}

function normalizeReport(value, profile, target, scopeId) {
  const report = value?.report && typeof value.report === 'object' ? value.report : value || {};
  const checks = Array.isArray(report.checks) ? report.checks.map((check, index) => ({
    id: String(check?.id || `check-${index}`),
    label: String(check?.label || check?.id || `能力 ${index + 1}`),
    category: String(check?.category || '环境'),
    status: normalizeStatus(check?.status),
    required: check?.required !== false,
    path: String(check?.path || ''),
    detail: String(check?.detail || ''),
  })) : [];
  return {
    schema: report.schema || report.schemaVersion || PROFILE_SCHEMA,
    profile: report.profile && typeof report.profile === 'object' ? report.profile : profile,
    managedRoot: String(report.managedRoot || expectedManagedRoot(profile.profileId, scopeId)),
    managedPaths: report.managedPaths && typeof report.managedPaths === 'object' ? report.managedPaths : {},
    checks,
    summary: report.summary && typeof report.summary === 'object' ? report.summary : null,
    inspectionError: String(report.inspectionError || ''),
    target: normalizeSshTarget(target),
  };
}

function createNode(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

export function createServerProfileStudio({ invoke, getAppState, openSettings, submitTask }) {
  let report = null;
  let pending = '';
  let savedSignature = '';
  let profileTarget = null;
  let profileScopeId = 'unscoped';

  $('server-profile-page').innerHTML = `
    <div class="page-heading server-profile-heading">
      <div><h1 class="page-title-tip" tabindex="0" title="配置源码分析、工具链编译和异构验证构建所需的服务器资源。">服务器环境</h1></div>
      <button id="server-profile-configure" class="button">配置资源目录</button>
    </div>
    <div id="server-profile-notice" class="notice server-profile-notice" role="status"></div>
    <div class="server-profile-actions" aria-label="服务器环境操作">
      <div class="server-profile-connection"><span id="server-profile-connection-dot" class="status-dot"></span><div><strong id="server-profile-connection-title">SSH 未连接</strong><span id="server-profile-connection-detail">连接服务器后才能执行远程预检与初始化。</span></div></div>
      <div class="server-profile-action-buttons"><button id="server-profile-inspect" class="button">检查环境</button><button id="server-profile-prepare" class="button primary-button">初始化托管目录</button><button id="server-profile-agent" class="button server-profile-agent-button">✦ 交给 Agent 补齐</button></div>
    </div>
    <div class="server-profile-grid">
      <section class="server-profile-panel" aria-labelledby="server-resources-title">
        <header><div><span class="server-profile-index">A</span><div><h2 id="server-resources-title">用户提供资源</h2><p>已有源码、SDK、编译与分析工具</p></div></div><span id="server-profile-name" class="server-profile-chip">default</span></header>
        <div id="server-profile-resources" class="server-profile-resource-list"></div>
      </section>
      <section class="server-profile-panel" aria-labelledby="server-managed-title">
        <header><div><span class="server-profile-index">B</span><div><h2 id="server-managed-title">平台自动管理目录</h2><p>无需逐项填写，按环境隔离创建</p></div></div><span class="server-profile-chip auto">AUTO</span></header>
        <div class="server-managed-root"><span>托管根目录</span><code id="server-managed-root"></code></div>
        <div id="server-managed-paths" class="server-managed-paths"></div>
      </section>
      <section class="server-profile-panel server-profile-matrix-panel" aria-labelledby="server-matrix-title">
        <header><div><span class="server-profile-index">C</span><div><h2 id="server-matrix-title">能力预检矩阵</h2><p>核对三类工作所需资源与工具是否完整</p></div></div><span id="server-profile-schema" class="server-profile-schema">${PROFILE_SCHEMA}</span></header>
        <div id="server-profile-report-target" class="server-profile-report-target">尚未执行服务器预检</div>
        <div id="server-profile-readiness" class="server-profile-readiness"></div>
        <div class="server-profile-table-wrap"><table class="server-profile-table"><thead><tr><th>类别</th><th>能力</th><th>状态</th><th>位置与诊断</th></tr></thead><tbody id="server-profile-checks"></tbody></table></div>
      </section>
    </div>`;

  function readProfile() {
    const roots = $('server-source-roots').value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
    return {
      profileId: $('server-profile-id').value.trim() || 'default',
      workerRepoRoot: $('server-worker-repo-root').value.trim(),
      sourceRoots: [...new Set(roots)],
      openHarmonySdkRoot: $('server-openharmony-sdk-root').value.trim(),
      llvmBinRoot: $('server-llvm-bin-root').value.trim(),
      glslangBin: $('server-glslang-bin').value.trim(),
      traceStreamer: $('server-trace-streamer').value.trim(),
      runtimeLibraryRoot: $('server-runtime-library-root').value.trim(),
      pythonBin: $('server-python-bin').value.trim() || 'python3',
    };
  }

  function hydrate(profile = {}, target = selectedTarget(), scopeId = 'unscoped') {
    $('server-profile-id').value = profile.profileId || 'default';
    $('server-worker-repo-root').value = profile.workerRepoRoot || '';
    $('server-source-roots').value = Array.isArray(profile.sourceRoots) ? profile.sourceRoots.join('\n') : '';
    $('server-openharmony-sdk-root').value = profile.openHarmonySdkRoot || '';
    $('server-llvm-bin-root').value = profile.llvmBinRoot || '';
    $('server-glslang-bin').value = profile.glslangBin || '';
    $('server-trace-streamer').value = profile.traceStreamer || '';
    $('server-runtime-library-root').value = profile.runtimeLibraryRoot || '';
    $('server-python-bin').value = profile.pythonBin || 'python3';
    profileTarget = hasSshTarget(target) ? normalizeSshTarget(target) : null;
    profileScopeId = /^[a-f0-9]{16}$/.test(String(scopeId || '')) ? String(scopeId) : 'unscoped';
    savedSignature = profileSignature(readProfile());
    report = null;
    render();
  }

  function syncSettings(settings = {}, { force = false } = {}) {
    const target = hasSshTarget(settings.serverProfileTarget)
      ? normalizeSshTarget(settings.serverProfileTarget)
      : hasSshTarget(settings.ssh) ? normalizeSshTarget(settings.ssh) : null;
    const changed = Boolean(profileTarget || target) && !sameSshTarget(profileTarget, target);
    if (force || changed) hydrate(settings.serverProfile || {}, target, settings.serverProfileScopeId);
  }

  function connected() { return getAppState()?.ssh?.state === 'connected'; }

  function selectedTarget() {
    const appState = getAppState() || {};
    const ssh = appState.ssh || {};
    const configured = appState.settings?.ssh || {};
    if (['connected', 'connecting'].includes(ssh.state) && hasSshTarget(ssh)) return normalizeSshTarget(ssh);
    if (hasSshTarget(configured)) return normalizeSshTarget(configured);
    return hasSshTarget(ssh) ? normalizeSshTarget(ssh) : null;
  }

  function executionTarget() {
    const ssh = getAppState()?.ssh || {};
    return connected() && hasSshTarget(ssh) ? normalizeSshTarget(ssh) : null;
  }

  function checkedExecutionTarget() {
    const target = executionTarget();
    if (!target) throw new Error('当前没有可执行服务器环境操作的 SSH 目标。');
    if (profileTarget && !sameSshTarget(profileTarget, target)) throw new Error('当前服务器配置属于另一个 SSH 目标，请先重新加载当前目标的配置。');
    return target;
  }

  function notify(message, tone = '', context = {}) {
    const node = $('server-profile-notice');
    if (tone === 'error' && message && publishWorkbenchNotification({tone: 'error', text: message, source: 'server-profile', ...context})) {
      node.textContent = '';
      node.hidden = true;
      return;
    }
    node.textContent = message;
    node.className = `notice server-profile-notice ${tone}`.trim();
    node.hidden = !message;
  }

  function renderConnection() {
    const ssh = getAppState()?.ssh || {};
    const configured = getAppState()?.settings?.ssh || {};
    const isConnected = connected();
    const targetChanged = Boolean(report?.target && !sameSshTarget(report.target, selectedTarget()));
    if (targetChanged) {
      report = null;
      renderManaged(readProfile());
      renderReportOrigin();
      renderReadiness();
      renderChecks();
    }
    $('server-profile-connection-dot').className = `status-dot ${isConnected ? 'good' : ''}`;
    $('server-profile-connection-title').textContent = isConnected ? 'SSH 工具通道在线' : ssh.state === 'connecting' ? '正在连接 SSH' : 'SSH 未连接';
    $('server-profile-connection-detail').textContent = isConnected
      ? `${ssh.username || configured.username || 'user'}@${ssh.host || configured.host || 'server'} · ${ssh.cwd || configured.cwd || '~'}`
      : '连接服务器后才能执行远程预检与初始化。';
    for (const id of ['server-profile-inspect', 'server-profile-prepare', 'server-profile-agent']) $(id).disabled = !isConnected || Boolean(pending);
    $('server-profile-inspect').textContent = pending === 'inspect' ? '正在检查…' : '检查环境';
    $('server-profile-prepare').textContent = pending === 'prepare' ? '正在初始化…' : '初始化托管目录';
    $('server-profile-agent').textContent = pending === 'agent' ? '正在提交…' : '✦ 交给 Agent 补齐';
    if (targetChanged) notify('SSH 目标已改变：已清除上一台服务器的预检结果，请连接后重新检查。', '');
    else if (!isConnected && !pending && report) notify('当前 SSH 离线：下方仅保留该目标的上次检查结果，不能作为当前可执行结论。', '');
    else if (!isConnected && !pending) notify('未连接 SSH：请先连接远程服务器，再检查资源目录或初始化平台托管目录。', '');
  }

  function resourceRow(label, value, kind, detail = '') {
    const row = createNode('div', 'server-profile-resource');
    const icon = createNode('span', 'server-profile-resource-icon', kind);
    const content = createNode('div', 'server-profile-resource-content');
    const heading = createNode('div', 'server-profile-resource-heading');
    heading.append(createNode('strong', '', label), createNode('code', value ? '' : 'empty', displayPath(value)));
    content.append(heading);
    if (detail) content.append(createNode('p', '', detail));
    row.append(icon, content);
    return row;
  }

  function renderResources(profile) {
    const list = $('server-profile-resources');
    list.replaceChildren();
    list.append(
      resourceRow('异构使能仓', profile.workerRepoRoot, '</>'),
      resourceRow('代码分析源码根', profile.sourceRoots.join(' · '), '{}', profile.sourceRoots.length ? `${profile.sourceRoots.length} 个源码目录` : '可配置一个或多个待分析代码树'),
      resourceRow('OpenHarmony SDK / Third-party 资源', profile.openHarmonySdkRoot, 'SDK', OPENHARMONY_SURFACES.join(' · ')),
      resourceRow('LLVM / glslang', [profile.llvmBinRoot, profile.glslangBin].filter(Boolean).join(' · '), 'CC'),
      resourceRow('TraceStreamer', profile.traceStreamer, 'TS'),
      resourceRow('真机运行库', profile.runtimeLibraryRoot, 'LIB'),
      resourceRow('Python', profile.pythonBin, 'PY'),
    );
  }

  function renderManaged(profile) {
    const root = report?.managedRoot || expectedManagedRoot(profile.profileId, profileScopeId);
    $('server-managed-root').textContent = root;
    const list = $('server-managed-paths');
    list.replaceChildren();
    for (const [key, label] of MANAGED_PATHS) {
      const row = createNode('div', 'server-managed-path');
      const mark = createNode('span', 'server-managed-path-mark', '◇');
      const content = createNode('div');
      content.append(createNode('strong', '', label));
      const value = report?.managedPaths?.[key];
      content.append(createNode('code', value ? '' : 'logical', value || `由平台在 ${root} 下管理`));
      row.append(mark, content); list.append(row);
    }
  }

  function fallbackChecks() {
    return DEFAULT_CHECKS.map(([id, label, category]) => ({ id, label, category, status: 'skipped', required: true, path: '', detail: '尚未在服务器执行预检' }));
  }

  function renderReadiness() {
    const area = $('server-profile-readiness');
    area.replaceChildren();
    const readyFor = report?.summary?.readyFor || {};
    const online = connected();
    for (const [key, label] of [['codeAnalysis', '代码分析'], ['traceAnalysis', 'Trace 分析'], ['deviceBuild', '真机构建']]) {
      const value = readyFor[key];
      const historical = Boolean(report && !online);
      const tone = historical ? 'previous' : value === true ? 'ready' : value === false ? 'blocked' : '';
      const card = createNode('div', `server-readiness-card ${tone}`.trim());
      card.append(createNode('span', 'server-readiness-mark', historical ? '◷' : value === true ? '✓' : value === false ? '!' : '·'));
      const description = historical
        ? value === true ? '上次检查就绪 · 当前离线' : value === false ? '上次检查未就绪 · 当前离线' : '上次检查状态未知 · 当前离线'
        : value === true ? '所需资源完整' : value === false ? '仍有必需项缺失' : '等待环境预检';
      const body = createNode('div'); body.append(createNode('strong', '', label), createNode('span', '', description));
      card.append(body); area.append(card);
    }
  }

  function renderReportOrigin() {
    const node = $('server-profile-report-target');
    if (!report?.target) {
      node.textContent = '尚未执行服务器预检';
      node.className = 'server-profile-report-target';
      return;
    }
    const online = connected() && sameSshTarget(report.target, executionTarget());
    node.textContent = `${online ? '当前检查目标' : '上次检查目标'} · ${sshTargetLabel(report.target)}${online ? '' : ' · 当前离线'}`;
    node.className = `server-profile-report-target ${online ? 'current' : 'offline'}`;
  }

  function renderChecks() {
    const body = $('server-profile-checks');
    body.replaceChildren();
    const historical = Boolean(report && !connected());
    for (const check of report?.checks?.length ? report.checks : fallbackChecks()) {
      const tr = document.createElement('tr');
      const category = createNode('td', 'server-check-category', check.category);
      const label = createNode('td'); label.append(createNode('strong', '', check.label));
      if (!check.required) label.append(createNode('span', 'server-check-optional', '可选'));
      const status = createNode('td');
      const normalized = normalizeStatus(check.status);
      status.append(createNode('span', `server-check-status ${historical ? 'previous' : normalized}`, historical ? `上次${statusText(normalized)}` : statusText(normalized)));
      const diagnostic = createNode('td', 'server-check-diagnostic');
      if (check.path) diagnostic.append(createNode('code', '', check.path));
      diagnostic.append(createNode('span', '', check.detail || (normalizeStatus(check.status) === 'ready' ? '能力已验证' : '尚无诊断信息')));
      tr.append(category, label, status, diagnostic); body.append(tr);
    }
  }

  function render() {
    const profile = readProfile();
    $('server-profile-name').textContent = profile.profileId;
    $('server-profile-schema').textContent = report?.schema || PROFILE_SCHEMA;
    renderResources(profile); renderManaged(profile); renderReportOrigin(); renderReadiness(); renderChecks(); renderConnection();
  }

  async function perform(kind, task) {
    if (pending) return;
    if (!connected()) { notify('未连接 SSH：请先连接远程服务器，再执行这项操作。', 'error', {operation: kind}); renderConnection(); return; }
    pending = kind; renderConnection();
    try { await task(); }
    catch (error) { notify(messageOf(error), 'error', {operation: kind, errorCode: errorCodeOf(error), context: {operation_key: kind}}); }
    finally { pending = ''; renderConnection(); }
  }

  function applyReport(value, profile, target) {
    const current = executionTarget();
    if (!current || !sameSshTarget(target, current)) throw new Error('SSH 目标在预检期间发生变化，已丢弃该结果；请在当前服务器重新检查。');
    report = normalizeReport(value, profile, target, profileScopeId);
    render();
  }

  async function inspect() {
    const profile = readProfile();
    const target = checkedExecutionTarget();
    const value = await invoke('inspectServerProfile', { profile, target });
    applyReport(value, profile, target);
    const missing = report.checks.filter((check) => check.required && check.status === 'missing').length;
    notify(missing ? `环境预检完成：仍有 ${missing} 个必需能力缺失。` : '环境预检完成，结果来自当前 SSH 服务器。', missing ? 'error' : 'success');
  }

  async function prepare() {
    const profile = readProfile();
    const target = checkedExecutionTarget();
    const value = await invoke('prepareServerProfile', { profile, target });
    applyReport(value, profile, target);
    notify(report.inspectionError || '托管目录初始化完成；能力矩阵已按服务器返回结果更新。', report.inspectionError ? 'error' : 'success');
  }

  function agentPrompt() {
    const profile = readProfile();
    const unresolved = (report?.checks || fallbackChecks()).filter((check) => ['missing', 'warning', 'skipped'].includes(check.status));
    return [
      '请补齐异构使能工作台的远程服务器环境，并在完成后运行环境预检。',
      '工作台已经建立当前 SSH 工具通道。请基于下面的目录契约检查实际文件，创建缺失的平台托管目录，并给出仍需用户提供的资源清单。',
      `服务器环境配置：\n${JSON.stringify(profile, null, 2)}`,
      `OpenHarmony 头文件资源根应覆盖：${OPENHARMONY_SURFACES.join('、')}。`,
      `当前未就绪能力：\n${unresolved.map((check) => `- ${check.label}：${check.detail || statusText(check.status)}`).join('\n') || '- 无；请复核环境契约。'}`,
      '请记录实际执行的检查、创建的目录和验证结论，便于工作台再次预检。',
    ].join('\n\n');
  }

  $('server-profile-configure').addEventListener('click', () => openSettings('server-profile'));
  $('server-profile-inspect').addEventListener('click', () => void perform('inspect', inspect));
  $('server-profile-prepare').addEventListener('click', () => void perform('prepare', prepare));
  $('server-profile-agent').addEventListener('click', () => void perform('agent', async () => {
    await submitTask(agentPrompt());
    notify('环境补齐任务已提交给右侧 Code Agent。', 'success');
  }));
  for (const id of ['server-profile-id', 'server-worker-repo-root', 'server-source-roots', 'server-openharmony-sdk-root', 'server-llvm-bin-root', 'server-glslang-bin', 'server-trace-streamer', 'server-runtime-library-root', 'server-python-bin']) {
    $(id).addEventListener('input', () => {
      if (profileSignature(readProfile()) !== savedSignature) report = null;
      render();
    });
  }

  hydrate({});
  return {
    readProfile,
    readTarget: () => profileTarget ? { ...profileTarget } : null,
    hydrate,
    syncSettings,
    applyReport,
    updateConnection: renderConnection,
    onPage(page) { if (page === 'server-profile') render(); },
  };
}
