import './capture.css';
import { publishWorkbenchNotification } from '../../app/lib/workbench-notifications';
import capturePlanAgentTaskModule from './capture-plan-agent-task.cjs';

const {buildCapturePlanAgentTask} = capturePlanAgentTaskModule;

const $ = (id) => document.getElementById(id);
const copy = (value) => JSON.parse(JSON.stringify(value));
const activeStatuses = new Set(['starting', 'running', 'cancelling']);
const statusLabels = { starting: '准备中', running: '采集中', cancelling: '正在取消', succeeded: '已完成', completed: '已完成', partial: '部分数据', failed: '失败', cancelled: '已取消', interrupted: '已中断', registered: '已登记' };
const operations = { launch: '启动应用', click: '点击控件', key: '按键', swipe: '滑动', scroll: '滚动列表', home: '返回桌面', wait: '等待', unresolved: '待补充', date_mode: '切换日期', zoom: '缩放', albums: '打开相册', back: '返回', save: '保存' };
const AGENT_PLAN_SLOW_MS = 90_000;
const AGENT_PLAN_DEADLINE_MS = 120_000;
const MAX_TRACKED_AGENT_PLAN_REQUESTS = 8;
const MAX_TRACKED_CAPTURE_ATTEMPTS = 24;
const messageOf = (error) => String(error?.message || error || '操作失败').replace(/^Error invoking remote method '[^']+': Error: /, '');
const errorCodeOf = (error) => String(error?.code || error?.errorCode || error?.error_code || error?.details?.error_code || '');
const timeText = (value) => { if (!value) return '—'; const time = new Date(value); return Number.isNaN(time.getTime()) ? String(value) : time.toLocaleString('zh-CN', { hour12: false }); };
function element(tag, className, text) { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = String(text); return node; }
function badge(status) { return element('span', `state-badge capture-status ${status === 'partial' ? 'partial' : ['failed', 'interrupted'].includes(status) ? 'error' : activeStatuses.has(status) ? 'pending' : ['succeeded', 'completed'].includes(status) ? 'good' : ''}`, statusLabels[status] || status || '已登记'); }

export function createCaptureStudio({ invoke, getAppState, setPage, openSettings, submitTask, analyzeTrace }) {
  let snapshot = { plans: [], traces: [], runs: [], activeRun: null, selectedDeviceSerial: '' };
  let catalog;
  let draft;
  let selectedPlanId = '';
  let loadedPlanStamp = '';
  let devices = [];
  let dirty = false;
  let initialized = false;
  let initialization;
  let selectedTraceId = '';
  let displayedRunId = '';
  let awaitingAgent = false;
  let activeAgentPlanSubmission = null;
  const agentPlanSubmissions = new Map();
  let agentPlanSlow = false;
  let agentPlanSlowTimer;
  let agentPlanDeadlineTimer;
  let refreshTimer;
  let refreshRunning = false;
  let activePage = '';
  let deviceDiscovery = null;
  let disposed = false;
  let traceDetailRevision = 0;
  let launchingCaptureAttempt = null;
  const captureAttemptsByRunId = new Map();
  const captureRunCompletions = new Map();
  const captureTerminalRuns = new Map();
  const reportedCaptureFailures = new Set();
  const pending = new Set();
  const stepErrors = new Map();

  $('capture-page').innerHTML = `
    <div class="page-heading capture-heading"><h1 class="capture-page-title page-title-tip" tabindex="0" title="按步骤准备目标操作，同步采集 Trace、HiLog 与主线程 PMU，并将结果用于后续分析。">负载采样</h1><button id="capture-refresh" class="button"><span aria-hidden="true">↻</span>刷新</button></div>
    <div id="capture-notice" class="notice" role="status" hidden></div>
    <div id="capture-update" class="notice capture-update-note" hidden><span>Agent 已更新计划，可载入查看新的步骤和采集配置。</span><button id="capture-apply-update" class="button">载入更新</button></div>
    <div class="capture-body">
      <section class="capture-web-section capture-device-section" aria-labelledby="capture-device-heading">
        <div class="capture-web-heading"><div><span class="capture-heading-icon" aria-hidden="true">▯</span><h2 id="capture-device-heading">1. 设备连接</h2></div></div>
        <div class="capture-device-bar"><label for="capture-device" class="sr-only">采样设备</label><select id="capture-device"><option value="">请选择设备</option></select><div class="capture-device-actions"><button id="capture-discover" class="button small-button">刷新设备</button><button id="capture-device-settings" class="text-button">HDC 设置</button></div><span id="capture-device-meta" class="capture-device-meta">当前采样设备</span></div>
      </section>

      <section class="capture-web-section" aria-labelledby="capture-scene-heading">
        <div class="capture-web-heading"><div><span class="capture-heading-icon" aria-hidden="true">▤</span><h2 id="capture-scene-heading">2. 负载与模板</h2></div><span id="capture-step-summary" class="capture-count-badge">正在读取模板…</span></div>
        <div class="capture-field-grid capture-template-primary-fields">
          <label class="capture-field">负载模板<select id="capture-template" aria-label="负载模板"><option value="gallery_17_steps_v1">图库 17 步</option><option value="custom">自定义负载</option></select></label>
          <label class="capture-field">计划名称<input id="capture-title" placeholder="采样计划名称" aria-label="计划名称"></label>
        </div>
        <details id="capture-template-details" class="capture-template-details">
          <summary><span>模板详情</span><span class="capture-template-details-hint">目标应用、操作步骤与执行范围</span></summary>
          <div class="capture-template-details-body">
            <div class="capture-field-grid">
              <label class="capture-field">目标应用包名<input id="capture-app-bundle" placeholder="例如 com.huawei.hmos.photos" aria-label="目标应用包名" required><span class="capture-help">用于定位目标应用与主线程。</span></label>
              <label class="capture-field">启动 Ability<input id="capture-app-ability" placeholder="例如 com.huawei.photos.MainAbility" aria-label="启动 Ability" required><span class="capture-help">用于启动步骤；Agent 可根据应用信息补充。</span></label>
            </div>
            <label id="capture-natural-steps-wrap" class="capture-field capture-natural-steps">操作步骤 · 每行一步<textarea id="capture-instructions" rows="4" placeholder="启动图库&#10;打开一张图片&#10;点击编辑&#10;打开滤镜面板"></textarea><span class="capture-help">先保存自然语言草稿，再由 Agent 补充操作类型与控件定位。</span></label>
            <div class="capture-step-layout">
              <div class="capture-step-panel">
                <div class="capture-list-toolbar"><button id="capture-select-all" class="text-button" type="button">全选采集</button><button id="capture-clear-selection" class="text-button" type="button">清空</button><span>仅勾选步骤启动采集器</span></div>
                <div class="capture-step-scroll"><table class="capture-step-table"><thead><tr><th>采集</th><th>#</th><th>操作步骤</th><th>执行动作</th><th>参数</th><th></th></tr></thead><tbody id="capture-steps"></tbody></table></div>
                <div class="capture-step-footer"><button id="capture-add-step" class="text-button">＋ 添加步骤</button><span>勾选步骤记录数据，其余步骤只准备界面</span></div>
              </div>
              <aside class="capture-plan-side">
                <section class="capture-info-box"><h3>界面准备方式</h3><label class="capture-field"><select id="capture-preparation"><option value="auto">自动准备</option><option value="already_prepared">界面已准备</option></select></label><p id="capture-preparation-help" class="capture-help">依次执行到最后一个勾选步骤，只记录选定动作的采集窗口。</p></section>
                <section class="capture-info-box capture-plan-action-box"><h3>执行范围</h3><p id="capture-execution-summary" class="capture-help">尚未选择采集步骤。</p><details class="capture-agent-details"><summary>补充给 Agent 的要求</summary><label class="capture-field"><textarea id="capture-agent-request" rows="3" placeholder="例如：只采集点击编辑后的窗口，并补齐控件定位。"></textarea></label></details></section>
              </aside>
            </div>
            <details class="capture-toggle"><summary>应用、控件选择器与操作参数</summary><textarea id="capture-profile" aria-label="应用与控件配置 JSON"></textarea><p class="capture-help">应用包、Ability、控件 ID 与停留时间来自模板；运行时自动检测屏幕尺寸并换算操作坐标。</p></details>
          </div>
        </details>
        <div id="capture-agent-state" class="capture-agent-state" role="status" hidden></div>
        <button id="capture-agent-plan" class="button primary-button capture-step-primary" title="已有历史计划可以直接从“已保存计划”中选择并执行，不必每次都让助手重新生成。">✦ 让助手生成计划</button>
      </section>

      <section class="capture-web-section" aria-labelledby="capture-collector-heading">
        <div class="capture-web-heading"><div><span class="capture-heading-icon" aria-hidden="true">⌁</span><h2 id="capture-collector-heading">3. 采样配置</h2></div></div>
        <div class="capture-config-stack">
          <div class="capture-mode-row" role="group" aria-label="采集模式"><strong>采集模式：</strong><div class="capture-checks"><label class="capture-checkbox"><input type="checkbox" id="capture-trace" checked>HiTrace</label><label class="capture-checkbox"><input type="checkbox" id="capture-hilog" checked>HiLog</label><label class="capture-checkbox"><input type="checkbox" id="capture-pmu" checked>PMU 计数</label></div></div>
          <div id="capture-pmu-fields" class="capture-pmu-panel"><strong>PMU 采样参数：</strong><div class="capture-pmu-grid"><label class="capture-field">PMU 模式<select id="capture-pmu-group"><option value="auto">自动 · 尽可能采集全部可用事件</option><option value="cache">通用缓存</option><option value="branch">分支预测</option><option value="stall">流水线停顿</option><option value="l1d">L1 数据缓存</option><option value="llc">末级缓存</option><option value="dtlb">数据 TLB</option></select></label><label class="capture-field">目标采样间隔（μs）<input id="capture-period" type="number" min="10" max="10000" value="10"></label><label class="capture-field">采样窗口（ms）<input id="capture-duration" type="number" min="100" max="10000" value="10000"></label></div></div>
        </div>
        <p id="capture-readiness" class="capture-readiness-note" role="status" aria-live="polite">正在检查计划是否可以执行…</p>
      </section>
    </div>

    <section class="capture-web-section capture-history-section"><div class="capture-plan-controls"><button id="capture-save" class="button">保存计划</button><select id="capture-saved-plan" aria-label="已保存计划"><option value="">选择已保存计划</option></select><button id="capture-start" class="button primary-button">▷ 在设备执行采样</button></div><div class="capture-recovery-policy" aria-live="polite"><span id="capture-auto-recovery-status" class="state-badge">失败自动重试：未开启</span><span id="capture-auto-recovery-detail">采样失败后仅显示错误通知。</span><button id="capture-auto-recovery-settings" class="text-button" type="button">配置失败自动重试 →</button></div><span id="capture-dirty" class="capture-pending-changes"></span><section id="capture-run" class="capture-run capture-inline-run" hidden><div class="capture-run-header"><div><div class="capture-run-title"><h3 id="capture-run-title"></h3><span id="capture-run-status"></span></div><div id="capture-run-id" class="run-id"></div></div><button id="capture-run-details" class="text-button">查看采样记录 →</button></div><div class="capture-progress-track"><div id="capture-run-progress" class="capture-progress-fill"></div></div><div class="capture-run-body"><div id="capture-run-message" class="capture-run-message"></div><details><summary>任务详情</summary><pre id="capture-run-log" class="capture-run-log"></pre></details></div></section></section>
    <section class="capture-web-section capture-history-section"><div class="capture-web-heading"><div><span class="capture-heading-icon" aria-hidden="true">▧</span><h2>采样结果</h2></div><div><button id="capture-go-traces" class="text-button">管理原始 Trace →</button><span id="capture-run-count" class="capture-section-count">0 次运行</span></div></div><div id="capture-recent" class="capture-history"></div></section>`;
  $('traces-page').innerHTML = `
    <div class="page-heading"><h1 class="trace-page-title page-title-tip" tabindex="0" title="统一登记本机采样目录与归档，保留原始 Trace、HiLog 与 PMU 数据供后续分析。">Trace 管理</h1><div class="trace-import-bar"><button id="trace-register-directory" class="button">登记采样目录</button><button id="trace-register-archive" class="button primary-button">导入 capture.zip</button></div></div>
    <div id="trace-notice" class="notice" role="status" hidden></div>
    <div class="trace-split"><section class="capture-card trace-list-card"><header class="capture-card-header"><h2>采样记录 <span id="trace-count"></span></h2><button id="trace-refresh" class="text-button">刷新</button></header><div class="trace-search"><input id="trace-search" placeholder="搜索负载、设备或记录 ID" aria-label="搜索采样记录"></div><div id="trace-list" class="trace-record-list"></div></section><section id="trace-detail" class="capture-card trace-detail"><div class="capture-empty">选择一条采样记录，查看各步骤的数据文件与状态。</div></section></div>`;

  function notify(message, tone = '', target = 'capture-notice', context = {}) {
    const node = $(target);
    if (tone === 'error' && message && publishWorkbenchNotification({tone: 'error', text: message, source: target, ...context})) {
      node.textContent = ''; node.hidden = true; return;
    }
    node.textContent = message; node.className = `notice ${tone}`.trim(); node.hidden = !message;
  }
  async function perform(key, task, target = 'capture-notice', {errorContext} = {}) { if (pending.has(key)) return; pending.add(key); updateControls(); notify('', '', target); try { return await task(); } catch (error) { let details = {}; try { details = typeof errorContext === 'function' ? errorContext(error) || {} : {}; } catch { details = {}; } if (details.suppressNotification !== true) { const notificationDetails = {...details}; delete notificationDetails.suppressNotification; notify(messageOf(error), 'error', target, {operation: key, errorCode: errorCodeOf(error), context: {operation_key: key}, ...notificationDetails}); } } finally { pending.delete(key); updateControls(); } }
  function focusWorkflowAction(id) {
    window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
      const target = $(id);
      if (!target) return;
      target.scrollIntoView({ block: 'center', behavior: 'smooth' });
      if (target.matches('button') && !target.disabled) target.focus({ preventScroll: true });
    }));
  }
  function revealTemplateDetails(targetId = '') {
    const details = $('capture-template-details');
    if (details) details.open = true;
    const target = targetId ? $(targetId) : null;
    for (let parent = target?.closest?.('details'); parent; parent = parent.parentElement?.closest?.('details')) parent.open = true;
    if (target) window.requestAnimationFrame(() => {
      target.scrollIntoView?.({block: 'center', behavior: 'smooth'});
      target.focus?.({preventScroll: true});
    });
  }
  function currentRun() { return snapshot.activeRun && activeStatuses.has(snapshot.activeRun.status) ? snapshot.activeRun : snapshot.runs?.find((run) => run.runId === displayedRunId); }
  function executionReadiness() {
    if (!draft) return { ready: false, messages: ['采集模板尚未加载。'] };
    const steps = draft.steps || [];
    const selectedIds = new Set(draft.capture?.selected_step_ids || []);
    const selectedIndexes = steps.map((step, index) => selectedIds.has(step.step_id) ? index : -1).filter((index) => index >= 0);
    const collectors = ['trace', 'hilog', 'pmu'].filter((name) => $(`capture-${name}`).checked);
    const messages = [];
    if (!selectedIndexes.length) messages.push('请至少勾选一个采集步骤。');
    if (!collectors.length) messages.push('请至少选择一种采集数据。');
    if (selectedIndexes.length) {
      const first = $('capture-preparation').value === 'already_prepared' ? selectedIndexes[0] : 0;
      const last = selectedIndexes.at(-1);
      const executionSteps = steps.slice(first, last + 1);
      const unresolved = executionSteps.filter((step) => step.operation === 'unresolved');
      if (unresolved.length) messages.push(`执行范围内有 ${unresolved.length} 个步骤待解析（${unresolved.map((step) => step.title || step.step_id).join('、')}）。`);
      let targets = {};
      try {
        const profile = JSON.parse($('capture-profile').value || '{}');
        if (profile && typeof profile === 'object' && !Array.isArray(profile) && profile.targets && typeof profile.targets === 'object' && !Array.isArray(profile.targets)) targets = profile.targets;
      } catch { /* readDraft reports malformed JSON; readiness below reports selector blockers only when targets are readable. */ }
      const missing = [];
      for (const step of executionSteps) {
        const required = new Set(step.expected_targets || []);
        if (step.target && !step.selector) required.add(step.target);
        if (step.operation === 'date_mode') required.add('date_mode_button');
        if (step.template_step === 10) required.add('save_menu');
        for (const target of required) if (!targets[target]) missing.push({ step, target });
      }
      if (missing.length) {
        const details = missing.slice(0, 4).map(({ step, target }) => `${step.title || step.step_id}：${target}`).join('、');
        messages.push(`执行范围内有 ${missing.length} 项控件定位待补充（${details}${missing.length > 4 ? '等' : ''}）。`);
      }
      const executionIds = new Set(executionSteps.map((step) => step.step_id));
      const extraBlockers = (draft.readiness?.blockers || []).filter((blocker) => executionIds.has(blocker?.step_id) && !['instruction_needs_structured_operation', 'selector_missing'].includes(blocker?.reason));
      if (extraBlockers.length) messages.push(`执行范围内还有 ${extraBlockers.length} 项计划校验问题，请保存计划并按提示补充。`);
    }
    return { ready: messages.length === 0, messages };
  }
  function renderReadiness(readiness) {
    const note = $('capture-readiness');
    if (!readiness.ready) {
      note.dataset.state = 'blocked';
      note.textContent = `尚不可执行：${readiness.messages.join(' ')}`;
      return;
    }
    if (!snapshot.selectedDeviceSerial) {
      note.dataset.state = 'waiting';
      note.textContent = '计划内容已就绪；请选择采样设备后执行。';
      return;
    }
    note.dataset.state = 'ready';
    note.textContent = `计划已就绪，可在设备 ${snapshot.selectedDeviceSerial} 上执行；启动前仍会检查设备与目标应用状态。`;
  }
  function blockerText(blocker) {
    const step = blocker?.step_id || '执行步骤';
    if (blocker?.reason === 'instruction_needs_structured_operation') return `${step} 的操作待解析`;
    if (blocker?.reason === 'selector_missing') return `${step} 缺少控件定位 ${blocker.target || ''}`.trim();
    return `${step}：${blocker?.reason || '计划校验未通过'}`;
  }
  function updateControls() {
    const active = snapshot.activeRun && activeStatuses.has(snapshot.activeRun.status);
    const readiness = executionReadiness();
    const cancelling = Boolean(active && (pending.has('cancel') || snapshot.activeRun?.status === 'cancelling'));
    $('capture-start').disabled = active ? cancelling : !readiness.ready || !snapshot.selectedDeviceSerial || pending.has('start') || pending.has('save');
    $('capture-start').title = active ? '取消当前负载采样。' : readiness.ready ? snapshot.selectedDeviceSerial ? '' : '请先选择采样设备。' : readiness.messages.join(' ');
    $('capture-start').classList.toggle('capture-cancel', Boolean(active));
    $('capture-start').textContent = active ? cancelling ? '正在取消…' : '■ 取消采集' : pending.has('start') ? '正在启动采样…' : '▷ 在设备执行采样';
    $('capture-save').disabled = !draft || Boolean(active) || pending.has('save') || pending.has('start');
    $('capture-device').disabled = Boolean(active) || pending.has('device') || pending.has('start') || pending.has('agent');
    $('capture-discover').disabled = pending.has('discover');
    $('capture-discover').textContent = pending.has('discover') ? '正在读取…' : '刷新设备';
    $('capture-refresh').disabled = pending.has('refresh') || pending.has('discover');
    $('capture-refresh').innerHTML = pending.has('refresh') ? '<span aria-hidden="true">↻</span>正在刷新…' : '<span aria-hidden="true">↻</span>刷新';
    const agentBusy = pending.has('agent') || awaitingAgent;
    $('capture-agent-plan').disabled = !draft || agentBusy || pending.has('start');
    $('capture-agent-plan').textContent = pending.has('agent') ? '正在提交给 Agent…' : awaitingAgent ? '等待助手生成计划…' : '✦ 让助手生成计划';
    $('capture-agent-plan').setAttribute('aria-busy', String(agentBusy));
    const editorLocked = Boolean(active) || pending.has('save') || pending.has('start') || pending.has('agent');
    for (const input of $('capture-page').querySelectorAll('.capture-body input,.capture-body select,.capture-body textarea,#capture-saved-plan,#capture-add-step,#capture-select-all,#capture-clear-selection,.step-delete-button,.capture-history-card')) input.disabled = editorLocked || !catalog;
    $('capture-pmu-fields').hidden = !$('capture-pmu').checked;
    $('capture-natural-steps-wrap').hidden = $('capture-template').value !== 'custom';
    $('capture-dirty').textContent = dirty ? '有未保存的修改' : selectedPlanId ? '计划已保存' : '';
    const automation = getAppState()?.settings?.automation || {};
    const recoveryEnabled = automation.autoRecoverErrors === true;
    const deviceAuthorized = automation.allowDeviceOperations === true;
    const recoveryStatus = $('capture-auto-recovery-status');
    recoveryStatus.className = `state-badge ${recoveryEnabled && deviceAuthorized ? 'good' : recoveryEnabled ? 'pending' : ''}`.trim();
    recoveryStatus.textContent = recoveryEnabled && deviceAuthorized
      ? '失败自动重试：已开启'
      : recoveryEnabled ? '失败自动重试：待授权手机操作' : '失败自动重试：未开启';
    $('capture-auto-recovery-detail').textContent = recoveryEnabled && deviceAuthorized
      ? '已接入的采样步骤失败时，工作台会发送给 Code Agent 修复并仅重试一次。'
      : recoveryEnabled ? '开启“授权全部手机操作”后，采样失败才能无人值守重试。' : '采样失败后仅显示错误通知。';
    $('capture-preparation-help').textContent = $('capture-preparation').value === 'already_prepared' ? '设备已停在首个勾选动作前，直接从该动作执行至最后一个采集步骤。' : '依次执行到最后一个勾选步骤，只记录选定动作的采集窗口。';
    renderReadiness(readiness);
    $('capture-agent-state').classList.toggle('running', agentBusy);
    $('capture-agent-state').hidden = !agentBusy;
    if (awaitingAgent) $('capture-agent-state').textContent = agentPlanSlow
      ? '助手已处理超过 90 秒；将再等待 30 秒，超时后自动恢复重试。'
      : '助手正在生成并回填计划，请留在当前步骤。';
    else if (pending.has('agent')) $('capture-agent-state').textContent = '正在将计划交给助手。';
  }
  function markDirty() { dirty = true; updateControls(); }
  function planStamp(record) { return record ? `${record.planId}:${record.updatedAt || record.createdAt || JSON.stringify(record.plan)}` : ''; }
  function nextCaptureAttemptId(prefix = 'capture_attempt') {
    return `${prefix}_${globalThis.crypto?.randomUUID?.().replaceAll('-', '') || `${Date.now()}_${Math.random().toString(16).slice(2)}`}`;
  }
  function createCaptureAttempt({operationAttemptId, record, deviceSerial, hdcPath, recoveryEligible = true, retryOf = ''}) {
    return {
      operationAttemptId,
      planId: String(record.planId || ''),
      planRevision: String(record.updatedAt || record.createdAt || ''),
      planStamp: planStamp(record),
      deviceSerial: String(deviceSerial || ''),
      hdcPath: String(hdcPath || 'hdc'),
      recoveryEligible,
      retryOf,
      retryConsumed: false,
    };
  }
  function rememberCaptureAttempt(runId, attempt) {
    const id = String(runId || '');
    if (!id || !attempt) return;
    captureAttemptsByRunId.set(id, attempt);
    while (captureAttemptsByRunId.size > MAX_TRACKED_CAPTURE_ATTEMPTS) captureAttemptsByRunId.delete(captureAttemptsByRunId.keys().next().value);
  }
  function captureTerminalError(run) {
    const status = String(run?.status || 'unknown').toLowerCase();
    if (!['failed', 'cancelled', 'interrupted', 'unknown'].includes(status)) return null;
    const error = new Error(String(run?.error?.reason || run?.error?.message || run?.message || `采样重试结果为 ${status}`));
    error.code = String(run?.error?.code || run?.error?.error_code || `capture_retry_${status}`);
    return error;
  }
  function rememberCaptureTerminal(run) {
    if (!run?.runId || activeStatuses.has(run.status)) return;
    captureTerminalRuns.set(run.runId, run);
    while (captureTerminalRuns.size > MAX_TRACKED_CAPTURE_ATTEMPTS) captureTerminalRuns.delete(captureTerminalRuns.keys().next().value);
    const waiter = captureRunCompletions.get(run.runId);
    if (!waiter) return;
    captureRunCompletions.delete(run.runId);
    const error = captureTerminalError(run);
    if (error) waiter.reject(error);
    else waiter.resolve({runId: run.runId, status: run.status, message: run.message || ''});
  }
  function captureCompletion(run) {
    const terminal = captureTerminalRuns.get(run.runId) || (!activeStatuses.has(run.status) ? run : null);
    if (terminal) {
      const error = captureTerminalError(terminal);
      return error ? Promise.reject(error) : Promise.resolve({runId: terminal.runId, status: terminal.status, message: terminal.message || ''});
    }
    return new Promise((resolve, reject) => captureRunCompletions.set(run.runId, {resolve, reject}));
  }
  function deviceSerials(value) {
    return (Array.isArray(value) ? value : []).map((device) => typeof device === 'string' ? device : device?.serial || device?.deviceSerial || device?.device_serial).filter(Boolean);
  }
  async function launchCaptureAttempt(attempt, {trackCompletion = false} = {}) {
    launchingCaptureAttempt = attempt;
    try {
      const run = await invoke('startCapture', {planId: attempt.planId, deviceSerial: attempt.deviceSerial, hdcPath: attempt.hdcPath});
      if (!run?.runId) throw new Error('采样启动结果不完整，未返回运行 ID。');
      rememberCaptureAttempt(run.runId, attempt);
      displayedRunId = run.runId;
      snapshot.activeRun = run;
      snapshot.runs = [run, ...snapshot.runs.filter((item) => item.runId !== run.runId)];
      renderRun(); focusWorkflowAction('capture-run');
      const result = {
        runId: run.runId,
        status: run.status,
        pending: activeStatuses.has(run.status),
        operationAttemptId: attempt.operationAttemptId,
        ...(attempt.retryOf ? {retryOf: attempt.retryOf} : {}),
      };
      if (trackCompletion) result.completion = captureCompletion(run);
      return result;
    } finally {
      if (launchingCaptureAttempt === attempt) launchingCaptureAttempt = null;
    }
  }
  async function retryCaptureAttempt(original, recoveryResult = {}) {
    if (!original?.recoveryEligible || original.retryConsumed) throw new Error('本次失败已执行过自动重试，不会再次运行。');
    // Consume the only retry before validation. A stale plan/device must not be
    // turned into a retry loop if the same notification is delivered twice.
    original.retryConsumed = true;
    const state = await invoke('getState');
    const current = state?.capture || {};
    if (current.activeRun) throw new Error('已有采样任务正在运行，未重试原步骤。');
    if (String(current.selectedDeviceSerial || '') !== original.deviceSerial) throw new Error('当前选择的设备已变化，未在其他手机上重试。');
    const currentDevices = deviceSerials(current.devices);
    if (currentDevices.length && !currentDevices.includes(original.deviceSerial)) throw new Error('原采样设备当前不可用，未切换到其他手机重试。');
    const record = (current.plans || []).find((item) => item.planId === original.planId);
    if (!record) throw new Error('原采样计划已不存在，未重试。');
    const retryPlanId = String(recoveryResult.retryPlanId || original.planId);
    const retryPlanRevision = String(recoveryResult.retryPlanRevision || original.planRevision);
    if (retryPlanId !== original.planId || String(record.updatedAt || record.createdAt || '') !== retryPlanRevision) {
      throw new Error('采样计划版本不属于本次已审计修复，未自动重试。');
    }
    applyState(current);
    const retry = createCaptureAttempt({
      operationAttemptId: nextCaptureAttemptId('capture_retry'),
      record,
      deviceSerial: original.deviceSerial,
      hdcPath: original.hdcPath,
      recoveryEligible: false,
      retryOf: original.operationAttemptId,
    });
    return launchCaptureAttempt(retry, {trackCompletion: true});
  }
  function recoveryDetails(attempt, run = null) {
    if (!attempt?.recoveryEligible) return {};
    const runId = String(run?.runId || '');
    const recoveryKey = `capture-start:${attempt.operationAttemptId}`;
    return {
      dedupeKey: runId ? `capture-run-failed:${runId}` : `capture-start-failed:${attempt.operationAttemptId}`,
      recovery: {
        key: recoveryKey,
        operationAttemptId: attempt.operationAttemptId,
        label: '在设备执行采样',
        operationKey: 'capture-start',
        scopes: ['device'],
        selectedDevice: attempt.deviceSerial,
        planId: attempt.planId,
        planRevision: attempt.planRevision,
        retry: (recoveryResult) => retryCaptureAttempt(attempt, recoveryResult),
      },
      context: {
        operation_key: 'capture-start',
        operation_attempt_id: attempt.operationAttemptId,
        plan_id: attempt.planId,
        plan_revision: attempt.planRevision,
        device_serial: attempt.deviceSerial,
        desktop_run_id: runId,
      },
    };
  }
  function synchronousCaptureFailureDetails(attempt) {
    if (!attempt) return {};
    if (attempt.failureNotified) return {suppressNotification: true};
    attempt.failureNotified = true;
    return {operation: '在设备执行采样', ...recoveryDetails(attempt)};
  }
  function clearAgentPlanTimers() {
    clearTimeout(agentPlanSlowTimer); clearTimeout(agentPlanDeadlineTimer);
    agentPlanSlowTimer = undefined; agentPlanDeadlineTimer = undefined;
  }
  function trimAgentPlanSubmissions() {
    while (agentPlanSubmissions.size > MAX_TRACKED_AGENT_PLAN_REQUESTS) {
      const requestId = [...agentPlanSubmissions.keys()].find(value => value !== activeAgentPlanSubmission?.requestId);
      if (!requestId) break;
      agentPlanSubmissions.delete(requestId);
    }
  }
  function finishAgentPlanWait(submission, status) {
    if (!submission) return false;
    submission.status = status;
    if (activeAgentPlanSubmission?.requestId !== submission.requestId) return false;
    clearAgentPlanTimers();
    activeAgentPlanSubmission = null; awaitingAgent = false; agentPlanSlow = false;
    updateControls();
    return true;
  }
  function beginAgentPlanWait(record, requestId) {
    clearAgentPlanTimers();
    const submission = {
      requestId,
      planId: record.planId,
      expectedUpdatedAt: record.updatedAt,
      baselineStamp: planStamp(record),
      submittedAt: Date.now(),
      status: 'waiting',
    };
    activeAgentPlanSubmission = submission;
    agentPlanSubmissions.set(requestId, submission); trimAgentPlanSubmissions();
    awaitingAgent = true; agentPlanSlow = false;
    agentPlanSlowTimer = setTimeout(() => {
      if (disposed || activeAgentPlanSubmission?.requestId !== requestId) return;
      agentPlanSlow = true; updateControls();
    }, AGENT_PLAN_SLOW_MS);
    agentPlanDeadlineTimer = setTimeout(() => {
      if (disposed || activeAgentPlanSubmission?.requestId !== requestId) return;
      finishAgentPlanWait(submission, 'timed_out');
      notify('助手在 120 秒内未提交计划更新，已恢复“让助手生成计划”，可直接重试。迟到结果仍会按原计划版本校验，不会覆盖新修改。');
    }, AGENT_PLAN_DEADLINE_MS);
    updateControls();
    return submission;
  }
  function submissionMatchesCurrentPlan(submission) {
    const record = snapshot.plans?.find((value) => value.planId === selectedPlanId);
    return Boolean(submission && activeAgentPlanSubmission?.requestId === submission.requestId
      && selectedPlanId === submission.planId
      && record?.updatedAt === submission.expectedUpdatedAt
      && loadedPlanStamp === submission.baselineStamp && !dirty);
  }
  function basePlan(templateId = catalog?.default_template_id || 'gallery_17_steps_v1') {
    const template = catalog?.templates?.find((item) => item.template_id === templateId);
    const custom = templateId === 'custom';
    const steps = custom ? [] : copy(template?.steps || []);
    const profile = copy(catalog?.default_profile || {});
    if (custom) Object.assign(profile, { bundle: '', ability: '', targets: {} });
    return { title: custom ? '自定义负载采样' : template?.name || '图库 17 步', template_id: templateId, natural_language_steps: '', steps, profile, capture: { ...(copy(catalog?.default_capture || {})), selected_step_ids: custom ? [] : catalog?.default_capture?.selected_step_ids || [steps[0]?.step_id].filter(Boolean), collectors: ['trace', 'hilog', 'pmu'], preparation_mode: 'auto', pmu: { ...(catalog?.default_capture?.pmu || {}), period_us: 10, duration_ms: 10000, group: 'auto' } }, execution_policy: 'through_last_selected' };
  }
  function loadPlan(plan, record = null) {
    draft = copy(plan); selectedPlanId = record?.planId || ''; loadedPlanStamp = planStamp(record); dirty = false; stepErrors.clear();
    $('capture-title').value = draft.title || '';
    $('capture-template').value = draft.template_id || 'custom';
    $('capture-instructions').value = draft.natural_language_steps || '';
    $('capture-profile').value = JSON.stringify(draft.profile || {}, null, 2);
    $('capture-app-bundle').value = draft.profile?.bundle || '';
    $('capture-app-ability').value = draft.profile?.ability || '';
    $('capture-app-bundle').setAttribute('aria-invalid', 'false');
    $('capture-app-ability').setAttribute('aria-invalid', 'false');
    const capture = draft.capture || {};
    for (const name of ['trace', 'hilog', 'pmu']) $(`capture-${name}`).checked = (capture.collectors || ['trace', 'hilog', 'pmu']).includes(name);
    $('capture-preparation').value = capture.preparation_mode || 'auto';
    $('capture-period').value = capture.pmu?.period_us ?? 10;
    $('capture-duration').value = capture.pmu?.duration_ms ?? 10000;
    $('capture-pmu-group').value = capture.pmu?.group === 'all' ? 'auto' : capture.pmu?.group || 'auto';
    $('capture-update').hidden = !queuedAgentPlan;
    renderPlans(); renderSteps(); updateControls();
  }
  function readDraft({fork = false} = {}) {
    if (!draft) throw new Error('采集模板尚未加载。');
    if (stepErrors.size) { revealTemplateDetails(); throw new Error([...stepErrors.values()][0]); }
    if (!draft.steps?.length) { revealTemplateDetails('capture-instructions'); throw new Error('请至少填写一个步骤。'); }
    let profile;
    try { profile = JSON.parse($('capture-profile').value || '{}'); } catch { revealTemplateDetails('capture-profile'); throw new Error('应用与控件配置不是有效的 JSON。'); }
    if (!profile || typeof profile !== 'object' || Array.isArray(profile)) { revealTemplateDetails('capture-profile'); throw new Error('应用与控件配置需要 JSON 对象。'); }
    profile.bundle = $('capture-app-bundle').value.trim();
    profile.ability = $('capture-app-ability').value.trim();
    $('capture-app-bundle').setAttribute('aria-invalid', String(!profile.bundle));
    $('capture-app-ability').setAttribute('aria-invalid', String(!profile.ability));
    if (!profile.bundle && !profile.ability) { revealTemplateDetails('capture-app-bundle'); throw new Error('请填写目标应用包名和启动 Ability；也可以让页面助手补充。'); }
    if (!profile.bundle) { revealTemplateDetails('capture-app-bundle'); throw new Error('请填写目标应用包名；也可以让页面助手补充。'); }
    if (!profile.ability) { revealTemplateDetails('capture-app-ability'); throw new Error('请填写启动 Ability；也可以让页面助手补充。'); }
    const title = $('capture-title').value.trim();
    if (!title) { $('capture-title').focus(); throw new Error('请填写采样计划名称。'); }
    const selected = [...$('capture-steps').querySelectorAll('[data-step-selected]:checked')].map((input) => input.dataset.stepSelected);
    const collectors = ['trace', 'hilog', 'pmu'].filter((name) => $(`capture-${name}`).checked);
    if (!selected.length) { revealTemplateDetails('capture-select-all'); throw new Error('请至少勾选一个需要采集的步骤。'); }
    if (!collectors.length) throw new Error('请至少选择一种采集数据。');
    const period = Number($('capture-period').value); const duration = Number($('capture-duration').value);
    if (!Number.isInteger(period) || period < 10 || period > 10000) throw new Error('PMU 采样间隔需要为 10–10000 μs 的整数。');
    if (!Number.isInteger(duration) || duration < 100 || duration > 10000) throw new Error('PMU 窗口上限需要为 100–10000 ms 的整数。');
    const value = { ...copy(draft), ...(!fork && selectedPlanId ? { planId: selectedPlanId } : {}), title, template_id: $('capture-template').value, natural_language_steps: $('capture-instructions').value, profile, capture: { ...copy(draft.capture || {}), selected_step_ids: selected, collectors, preparation_mode: $('capture-preparation').value, pmu: { ...copy(draft.capture?.pmu || {}), period_us: period, duration_ms: duration, group: $('capture-pmu-group').value } } };
    if (fork) for (const key of ['planId', 'plan_id', 'artifact_id', 'id']) delete value[key];
    return value;
  }
  function renderPlans() {
    const select = $('capture-saved-plan'); select.replaceChildren(new Option(snapshot.plans?.length ? '选择已保存计划' : '尚无已保存计划', ''));
    for (const record of snapshot.plans || []) select.append(new Option(record.title || record.plan?.title || record.planId, record.planId));
    select.value = selectedPlanId;
  }
  function syncSelected() { if (!draft) return; draft.capture = { ...draft.capture, selected_step_ids: [...$('capture-steps').querySelectorAll('[data-step-selected]:checked')].map((input) => input.dataset.stepSelected) }; renderStepSummary(); }
  function updateCustomSteps(value) {
    if (!draft || $('capture-template').value !== 'custom') return;
    const steps = value.split(/\r?\n/).map((line) => line.trim().replace(/^\d+[.、)）]\s*/, '')).filter(Boolean).map((title, index) => ({ step_id: `step_${String(index + 1).padStart(2, '0')}`, ordinal: index + 1, title, operation: 'unresolved' }));
    const ids = new Set(steps.map((step) => step.step_id));
    draft.natural_language_steps = value;
    draft.steps = steps;
    draft.capture = { ...draft.capture, selected_step_ids: (draft.capture?.selected_step_ids || []).filter((id) => ids.has(id)) };
    stepErrors.clear();
    renderSteps();
    markDirty();
  }
  function renderStepSummary() {
    const ids = draft?.capture?.selected_step_ids || []; const count = ids.length; const steps = draft?.steps || [];
    $('capture-step-summary').textContent = `${steps.length} 个步骤 · ${count} 个采集点`;
    const indexes = steps.map((step, index) => ids.includes(step.step_id) ? index : -1).filter((index) => index >= 0);
    const start = $('capture-preparation').value === 'already_prepared' ? indexes[0] : 0; const end = indexes.at(-1);
    $('capture-execution-summary').textContent = end === undefined ? '尚未选择采集步骤。' : `将执行第 ${start + 1} 至 ${end + 1} 步，其中 ${count} 步记录运行数据。`;
  }
  function renderSteps() {
    const body = $('capture-steps'); body.replaceChildren();
    if (!draft?.steps?.length) { const row = element('tr'); const cell = element('td', 'capture-step-empty', '添加一个步骤，或选择图库模板开始。'); cell.colSpan = 6; row.append(cell); body.append(row); renderStepSummary(); return; }
    const selected = new Set(draft.capture?.selected_step_ids || []);
    draft.steps.forEach((step, index) => {
      const row = element('tr', selected.has(step.step_id) ? 'selected-step' : '');
      const checkCell = element('td'); const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = selected.has(step.step_id); checkbox.dataset.stepSelected = step.step_id; checkbox.setAttribute('aria-label', `采集第 ${index + 1} 步 ${step.title}`); checkbox.addEventListener('change', () => { row.classList.toggle('selected-step', checkbox.checked); syncSelected(); markDirty(); }); checkCell.append(checkbox);
      const titleCell = element('td'); const title = document.createElement('input'); title.type = 'text'; title.value = step.title || ''; title.setAttribute('aria-label', `第 ${index + 1} 步名称`); title.addEventListener('input', () => { step.title = title.value; markDirty(); }); titleCell.append(title);
      const operationCell = element('td'); const select = document.createElement('select'); select.setAttribute('aria-label', `第 ${index + 1} 步动作`);
      const choices = [...new Set([...(catalog?.operations || Object.keys(operations)), step.operation].filter(Boolean))];
      for (const operation of choices) select.append(new Option(operations[operation] || operation, operation));
      select.value = step.operation || 'unresolved'; select.addEventListener('change', () => { step.operation = select.value; if (step.operation === 'key') step.key ||= 'Back'; if (step.operation === 'wait') step.seconds ??= 1; if (step.operation === 'swipe') { step.points ||= [.5, .8, .5, .2]; step.velocity ||= 1000; } markDirty(); renderSteps(); }); operationCell.append(select);
      const detailCell = element('td'); const detailButton = element('button', 'step-config-button', '配置'); detailButton.type = 'button'; detailCell.append(detailButton);
      const deleteCell = element('td'); const remove = element('button', 'step-delete-button', '×'); remove.setAttribute('aria-label', `删除第 ${index + 1} 步`); remove.addEventListener('click', () => { syncSelected(); draft.steps.splice(index, 1); draft.capture.selected_step_ids = draft.capture.selected_step_ids.filter((id) => id !== step.step_id); markDirty(); renderSteps(); }); deleteCell.append(remove);
      row.append(checkCell, element('td', '', index + 1), titleCell, operationCell, detailCell, deleteCell); body.append(row);
      const details = element('tr', 'step-detail-row'); details.hidden = true; const cell = element('td'); cell.colSpan = 6; const grid = element('div', 'step-detail-grid');
      function field(label, value, onChange, type = 'text', full = false) { const holder = element('label', full ? 'span-full' : '', label); const input = document.createElement('input'); input.type = type; input.value = value ?? ''; input.addEventListener('change', () => { onChange(input.value); markDirty(); }); holder.append(input); grid.append(holder); return input; }
      field('步骤 ID', step.step_id, (value) => { const errorKey = `id_${index}`; if (!/^[a-zA-Z][a-zA-Z0-9_-]{0,63}$/.test(value) || draft.steps.some((other) => other !== step && other.step_id === value)) { const message = '步骤 ID 需要唯一，以字母开头，只使用字母、数字、下划线或连字符。'; stepErrors.set(errorKey, message); notify(message, 'error'); return; } stepErrors.delete(errorKey); const old = step.step_id; step.step_id = value; checkbox.dataset.stepSelected = value; draft.capture.selected_step_ids = draft.capture.selected_step_ids.map((id) => id === old ? value : id); });
      field('控件选择器名称（target）', step.target || '', (value) => { step.target = value.trim() || null; });
      if (step.operation === 'key') field('按键：Home / Back / Enter / Power', step.key || 'Back', (value) => { step.key = value; });
      if (step.operation === 'wait') field('等待时间（秒）', step.seconds ?? 1, (value) => { step.seconds = Number(value); }, 'number');
      if (step.operation === 'swipe') { field('滑动坐标比例：x1,y1,x2,y2', (step.points || []).join(', '), (value) => { step.points = value.split(',').map(Number); }, 'text', true); field('滑动速度', step.velocity || 1000, (value) => { step.velocity = Number(value); }, 'number'); }
      field('执行后预期控件（逗号分隔）', (step.expected_targets || []).join(', '), (value) => { step.expected_targets = value.split(',').map((item) => item.trim()).filter(Boolean); }, 'text', true);
      const selectorLabel = element('label', 'span-full', '内联控件定位 JSON（可选）'); const selector = document.createElement('textarea'); selector.rows = 2; selector.value = step.selector ? JSON.stringify(step.selector, null, 2) : ''; selector.addEventListener('change', () => { try { if (selector.value.trim()) step.selector = JSON.parse(selector.value); else delete step.selector; stepErrors.delete(`selector_${index}`); markDirty(); notify(''); } catch { const message = `第 ${index + 1} 步控件定位 JSON 格式不正确。`; stepErrors.set(`selector_${index}`, message); notify(message, 'error'); } }); selectorLabel.append(selector); grid.append(selectorLabel);
      cell.append(grid); details.append(cell); body.append(details); detailButton.addEventListener('click', () => { details.hidden = !details.hidden; detailButton.textContent = details.hidden ? '配置' : '收起'; });
    });
    renderStepSummary();
  }
  function updateDevices(value) {
    devices = Array.isArray(value) ? value : value?.devices || [];
    const select = $('capture-device'); select.replaceChildren(new Option(devices.length ? '请选择采样设备' : '尚未读取到设备', ''));
    const serials = [];
    for (const device of devices) { const serial = typeof device === 'string' ? device : device.serial || device.deviceSerial || device.device_serial; if (serial) { serials.push(serial); select.append(new Option(serial, serial)); } }
    const previous = snapshot.selectedDeviceSerial || '';
    const captureActive = snapshot.activeRun && activeStatuses.has(snapshot.activeRun.status);
    let selected = previous;
    // Automatic discovery may observe a transiently incomplete HDC list. Keep
    // an explicit selection instead of silently moving the capture to another
    // phone; only a page with no prior choice may select its sole candidate.
    if (!captureActive && !previous && serials.length === 1) selected = serials[0];
    if (selected !== previous) {
      snapshot.selectedDeviceSerial = selected;
      void invoke('selectCaptureDevice', { serial: selected }).catch((error) => notify(messageOf(error), 'error', 'capture-notice', {operation: '选择采样设备', errorCode: errorCodeOf(error)}));
    }
    if (selected && ![...select.options].some((option) => option.value === selected)) select.append(new Option(`${selected}（待确认）`, selected));
    select.value = selected;
    $('capture-device-meta').textContent = devices.length ? `${devices.length} 台设备${selected ? ' · 已选择当前设备' : ''}` : '等待发现设备';
    updateControls();
  }
  async function discoverDevices({ reportEmpty = false } = {}) {
    const hdcPath = $('hdc-path').value.trim() || 'hdc';
    if (deviceDiscovery?.hdcPath === hdcPath) return deviceDiscovery.promise;
    let task;
    task = (async () => {
      pending.add('discover');
      updateControls();
      try {
        const result = await invoke('discoverDevices', { hdcPath });
        if (disposed || deviceDiscovery?.promise !== task) return result;
        updateDevices(result);
        if (reportEmpty && !devices.length) notify('HDC 已返回，当前没有已连接设备。请检查 USB 连接与设备授权。');
        return result;
      } catch (error) {
        if (!disposed && deviceDiscovery?.promise === task) notify(messageOf(error), 'error', 'capture-notice', {operation: '发现本机 HDC 设备', errorCode: errorCodeOf(error)});
      } finally {
        if (deviceDiscovery?.promise === task) {
          deviceDiscovery = null;
          pending.delete('discover');
          updateControls();
        }
      }
    })();
    deviceDiscovery = { hdcPath, promise: task };
    return task;
  }
  function renderRun() {
    const run = currentRun(); $('capture-run').hidden = !run;
    if (!run) return;
    $('capture-run-title').textContent = run.title || '负载采样';
    $('capture-run-status').replaceChildren(badge(run.status));
    $('capture-run-id').textContent = `${run.deviceSerial || '—'} · ${run.runId}`;
    $('capture-run-message').textContent = [run.message || run.stage || statusLabels[run.status] || '等待执行信息', run.registrationError ? `归档登记失败：${run.registrationError}` : '', run.resultDiagnostic || ''].filter(Boolean).join('\n');
    $('capture-run-message').classList.toggle('error-text', ['failed', 'interrupted'].includes(run.status));
    const explicitProgress = Number(run.progress?.percent ?? run.progressPercent);
    const ratio = Number.isFinite(explicitProgress) ? explicitProgress : ['succeeded', 'partial'].includes(run.status) ? 100 : 0;
    $('capture-run-progress').classList.toggle('indeterminate', !Number.isFinite(explicitProgress) && activeStatuses.has(run.status));
    $('capture-run-progress').style.width = `${Math.max(0, Math.min(100, ratio))}%`;
    const log = (run.log || []).slice(-150).map((line) => typeof line === 'string' ? line : line.message || line.text || JSON.stringify(line));
    $('capture-run-log').textContent = log.join('\n');
    $('capture-run-details').disabled = !run.traceId;
  }
  function recordRow(record, { active = false } = {}) {
    const row = element('button', `capture-result-row ${active ? 'active' : ''}`); const content = element('div'); content.append(element('strong', '', record.title || record.id || record.runId), element('small', '', `${timeText(record.registeredAt || record.updatedAt || record.createdAt)} · ${record.deviceSerial || '设备未记录'}`)); row.append(content, badge(record.status)); row.addEventListener('click', () => { setPage('traces'); void perform('trace-detail', () => showTrace(record.id), 'trace-notice'); }); return row;
  }
  function renderTraces() {
    const traces = snapshot.traces || []; const recent = $('capture-recent'); recent.replaceChildren();
    const runs = snapshot.runs || [];
    for (const run of runs.slice(0, 4)) { const row = element('button', 'capture-result-row'); const content = element('div'); content.append(element('strong', '', run.title || run.runId), element('small', '', `${timeText(run.createdAt || run.updatedAt)} · ${run.deviceSerial || '设备未记录'}`)); row.append(content, badge(run.status)); row.addEventListener('click', () => { displayedRunId = run.runId; renderRun(); $('capture-run').scrollIntoView({ block: 'nearest', behavior: 'smooth' }); }); recent.append(row); }
    if (!runs.length) for (const record of traces.slice(0, 4)) recent.append(recordRow(record));
    if (!traces.length && !runs.length) recent.append(element('div', 'capture-empty', '采集完成后会自动登记到本机 Trace 管理。'));
    const query = $('trace-search').value.trim().toLowerCase(); const list = $('trace-list'); list.replaceChildren();
    const filtered = traces.filter((record) => !query || [record.title, record.id, record.runId, record.deviceSerial].some((value) => String(value || '').toLowerCase().includes(query)));
    for (const record of filtered) list.append(recordRow(record, { active: record.id === selectedTraceId }));
    if (!filtered.length) list.append(element('div', 'capture-empty', query ? '没有匹配的采样记录。' : '还没有采样记录。可以执行负载采样，或登记已有数据。'));
    $('trace-count').textContent = `· ${traces.length}`;
    $('capture-run-count').textContent = `${runs.length} 次运行`;
  }
  function queuePlanUpdate(candidate, {agent = false} = {}) {
    if (!candidate) return;
    queuedAgentPlan = candidate;
    const blockers = candidate.plan?.readiness?.blockers || [];
    const description = $('capture-update').querySelector('span');
    description.textContent = blockers.length
      ? `${agent ? '助手已提交' : '计划已有'}更新，但仍有 ${blockers.length} 项未解析内容；可载入后继续补充。`
      : `${agent ? '助手已更新计划' : '计划已有更新'}；当前页面已切换计划或有未保存修改，请手动载入。`;
    $('capture-update').hidden = false;
    focusWorkflowAction('capture-apply-update');
  }
  function notifyFinishedRunFailure(run) {
    if (!run?.runId) return;
    if (!run || !['failed', 'interrupted'].includes(run.status)) return;
    const error = run.error && typeof run.error === 'object' ? run.error : {};
    const stepTitle = String(error.step_title || error.stepTitle || run.progress?.step_title || '').trim();
    const stepId = String(error.step_id || error.stepId || run.progress?.step_id || '').trim();
    const reason = String(error.reason || error.message || run.message || '采样任务未完成。').trim();
    const suggestion = String(error.suggestion || '').trim();
    const lines = [stepTitle ? `失败位置：${stepTitle}` : '', reason, suggestion ? `建议：${suggestion}` : ''].filter(Boolean);
    const failureKey = `${run.runId}:${run.status}:${errorCodeOf(error)}:${stepId}:${reason}`;
    if (reportedCaptureFailures.has(failureKey)) return;
    const attempt = captureAttemptsByRunId.get(run.runId);
    if (attempt?.failureNotified) return;
    if (attempt) attempt.failureNotified = true;
    reportedCaptureFailures.add(failureKey);
    while (reportedCaptureFailures.size > MAX_TRACKED_CAPTURE_ATTEMPTS * 2) reportedCaptureFailures.delete(reportedCaptureFailures.values().next().value);
    const recoverable = run.status === 'failed' ? recoveryDetails(attempt, run) : {};
    notify(lines.join('\n'), 'error', 'capture-notice', {
      operation: stepTitle ? `在设备执行采样 / ${stepTitle}` : '在设备执行采样',
      errorCode: errorCodeOf(error),
      ...recoverable,
      context: {
        ...(recoverable.context || {}),
        operation_key: 'capture-start',
        desktop_run_id: String(run.runId || ''),
        capture_run_id: String(run.captureRunId || ''),
        device_serial: String(run.deviceSerial || ''),
        stage: String(run.stage || ''),
        step_id: stepId,
        step_title: stepTitle,
      },
    });
  }
  function acceptAgentPlanCandidate(candidate, event, baselineCurrent) {
    if (!candidate) { notify('助手已完成计划补全，请刷新后查看。', 'success'); return; }
    if (!baselineCurrent || dirty) { queuePlanUpdate(candidate, {agent: true}); return; }
    loadPlan(candidate.plan, candidate);
    const blockers = candidate.plan?.readiness?.blockers || [];
    if (blockers.length) {
      revealTemplateDetails();
      notify(`助手已更新计划，但仍有 ${blockers.length} 项未解析内容；请继续补充后再执行采样。`);
      focusWorkflowAction('capture-agent-plan');
      return;
    }
    notify(event.changed === false ? '当前计划已经满足要求，可以直接执行采样。' : '助手已完成计划补全，可以继续执行采样。', 'success');
    focusWorkflowAction('capture-start');
  }
  function applyState(next, {suppressPlanSync = false} = {}) {
    if (!next) return;
    snapshot = { ...snapshot, ...next };
    if (Array.isArray(next.devices)) devices = next.devices;
    const updated = snapshot.plans?.find((record) => record.planId === selectedPlanId);
    if (!suppressPlanSync && updated && loadedPlanStamp && planStamp(updated) !== loadedPlanStamp) {
      if (!dirty) loadPlan(updated.plan, updated);
      else queuePlanUpdate(updated);
    }
    renderPlans(); updateDevices(devices); renderRun(); renderTraces(); updateControls();
  }
  let queuedAgentPlan;
  async function refresh() { if (refreshRunning || disposed) return; refreshRunning = true; try { const state = await invoke('getState'); applyState(state.capture); } finally { refreshRunning = false; } }
  function event(event) {
    if (event?.type === 'status' && event.target === 'agent' && ['stopped', 'error'].includes(event.state) && activeAgentPlanSubmission) {
      const submission = activeAgentPlanSubmission;
      finishAgentPlanWait(submission, event.state === 'error' ? 'agent_error' : 'agent_stopped');
      notify(event.state === 'error'
        ? `助手运行异常，计划未更新：${event.message || '未知错误'}。可重试生成计划。`
        : '助手已停止，计划未更新。重新启动助手后可重试。', 'error');
      return;
    }
    if (event?.type !== 'capture') return;
    const agentPlanEvent = ['agent-plan-completed', 'agent-plan-failed'].includes(event.kind);
    const submission = agentPlanEvent ? agentPlanSubmissions.get(event.requestId) : null;
    const baselineCurrent = submissionMatchesCurrentPlan(submission);
    const previousActive = snapshot.activeRun && activeStatuses.has(snapshot.activeRun.status) ? snapshot.activeRun : null;
    let finishedRun = null;
    if (event.state) {
      applyState(event.state, {suppressPlanSync: agentPlanEvent});
      const nextActive = event.state.activeRun;
      if (launchingCaptureAttempt && nextActive?.runId) rememberCaptureAttempt(nextActive.runId, launchingCaptureAttempt);
      if (previousActive?.runId) finishedRun = snapshot.runs?.find((item) => item.runId === previousActive.runId) || null;
    }
    if (event.kind === 'agent-plan-completed' && submission) {
      const candidate = snapshot.plans?.find((record) => record.planId === event.planId);
      finishAgentPlanWait(submission, 'completed');
      acceptAgentPlanCandidate(candidate, event, baselineCurrent);
    } else if (event.kind === 'agent-plan-completed') {
      const candidate = snapshot.plans?.find((record) => record.planId === event.planId);
      if (candidate) queuePlanUpdate(candidate, {agent: true});
    } else if (event.kind === 'agent-plan-failed' && submission) {
      finishAgentPlanWait(submission, 'failed');
      notify(event.message || '助手提交的计划补丁未通过校验。', 'error');
    }
    if (event.run) {
      if (launchingCaptureAttempt && event.run.runId) rememberCaptureAttempt(event.run.runId, launchingCaptureAttempt);
      snapshot.runs = [event.run, ...(snapshot.runs || []).filter((run) => run.runId !== event.run.runId)];
      if (activeStatuses.has(event.run.status)) snapshot.activeRun = event.run;
      else if (snapshot.activeRun?.runId === event.run.runId) snapshot.activeRun = null;
      if (displayedRunId === event.run.runId || activeStatuses.has(event.run.status)) displayedRunId = event.run.runId;
      renderRun(); updateControls();
      if (['succeeded', 'partial'].includes(event.run.status) && event.run.traceId) focusWorkflowAction('capture-run-details');
      if (!activeStatuses.has(event.run.status)) finishedRun = event.run;
    }
    notifyFinishedRunFailure(finishedRun);
    rememberCaptureTerminal(finishedRun);
    clearTimeout(refreshTimer); refreshTimer = setTimeout(() => void refresh().catch((error) => notify(messageOf(error), 'error', 'capture-notice', {operation: '刷新采样状态', errorCode: errorCodeOf(error)})), 150);
  }
  async function save() {
    const existing = snapshot.plans.find((item) => item.planId === selectedPlanId);
    if (!dirty && existing) return existing;
    const record = await invoke('saveCapturePlan', {plan: readDraft({fork: Boolean(selectedPlanId)})});
    if (!record?.planId || !record.plan) throw new Error('计划保存结果不完整。');
    snapshot.plans = [record, ...snapshot.plans.filter((item) => item.planId !== record.planId)];
    loadPlan(record.plan, record);
    return record;
  }
  async function showTrace(id) {
    if (!id) return;
    selectedTraceId = id; renderTraces(); const revision = ++traceDetailRevision;
    const detail = await invoke('getTrace', { id }); if (revision !== traceDetailRevision || disposed) return;
    const record = { ...snapshot.traces.find((item) => item.id === id), ...(detail.record || detail.trace || detail) };
    const container = $('trace-detail'); container.replaceChildren();
    const header = element('header', 'capture-card-header'); header.append(element('h2', '', record.title || id), badge(record.status)); container.append(header);
    const body = element('div', 'trace-detail-content'); const grid = element('dl', 'trace-summary-grid');
    for (const [label, value] of [['采样设备', record.deviceSerial], ['登记时间', timeText(record.registeredAt)], ['采样记录', record.runId || record.id], ['文件数量', record.fileCount ?? '—']]) { const cell = element('div'); cell.append(element('dt', '', label), element('dd', '', value || '—')); grid.append(cell); } body.append(grid);
    const available = detail.availability?.exists !== false;
    const directoryAvailable = detail.availability?.runDirectoryExists ?? Boolean(record.runDirectory && available);
    const archiveAvailable = detail.availability?.archiveExists ?? Boolean(record.archivePath && available);
    if (!available && !directoryAvailable && !archiveAvailable) body.append(element('div', 'notice error', detail.availability?.diagnostic || '原始数据已移动或删除，登记信息仍然保留。'));
    const manifest = detail.manifest || record.manifest; const files = detail.files || record.files || [];
    if (record.message || manifest?.error || manifest?.message) body.append(element('div', 'notice', record.message || manifest.error?.message || manifest.error || manifest.message));
    const diagnostics = [...(manifest?.quality?.errors || []), ...(manifest?.quality?.warnings || [])].slice(0, 5);
    for (const diagnostic of diagnostics) { const text = [diagnostic.step_title || diagnostic.step_id, diagnostic.message || diagnostic.reason || diagnostic.code, diagnostic.suggestion].filter(Boolean).map((value) => typeof value === 'string' ? value : JSON.stringify(value)).join(' · '); if (text) body.append(element('div', 'notice trace-diagnostic', text)); }
    if (manifest?.steps?.length) {
      body.append(element('div', 'capture-section-heading', '步骤数据')); const table = element('table', 'trace-files'); const head = element('thead'); const headerRow = element('tr'); for (const label of ['步骤', '状态', 'Trace / HiLog / PMU']) headerRow.append(element('th', '', label)); head.append(headerRow); table.append(head); const rows = element('tbody');
      for (const step of manifest.steps) { const row = element('tr'); const names = step.captured === false ? '准备步骤 · 未启用采集' : ['trace', 'hilog', 'pmu'].map((name) => `${name}: ${dataStatus(step.collector_status?.[name] || step[name] || step.collectors?.[name] || step.artifacts?.[name])}`).join(' · '); row.append(element('td', '', step.title || step.step_id), element('td', '', statusLabels[step.status] || step.status || '—'), element('td', '', names)); rows.append(row); } table.append(rows); body.append(table);
    } else if (files.length) {
      const table = element('table', 'trace-files'); const rows = element('tbody'); for (const file of files.slice(0, 100)) { const row = element('tr'); const size = file.size_bytes ?? file.size; row.append(element('td', '', typeof file === 'string' ? file : file.relative_path || file.relativePath || file.path || file.name), element('td', '', typeof file === 'object' && size !== undefined ? `${(size / 1024).toFixed(1)} KB` : '')); rows.append(row); } table.append(rows); body.append(table);
    } else body.append(element('div', 'capture-empty', '详细文件清单保存在采集目录中。'));
    const actions = element('div', 'trace-actions');
    const directory = record.runDirectory || (record.archivePath ? '' : record.sourcePath);
    const analyzableSteps = (manifest?.steps || []).filter(step => step?.captured && step.trace_validation?.valid === true
      && step.collector_status?.trace !== 'invalid' && files.filter(file => file?.step_id === step.step_id && file?.kind === 'trace').length === 1);
    if (analyzeTrace && record.archivePath && analyzableSteps.length) {
      let selectedStepId = analyzableSteps[0].step_id;
      if (analyzableSteps.length > 1) {
        const chooser = element('label', 'trace-analysis-step'); chooser.append(element('span', '', '分析步骤'));
        const select = element('select');
        for (const step of analyzableSteps) { const option = element('option', '', step.title || step.step_id); option.value = step.step_id; select.append(option); }
        select.addEventListener('change', () => { selectedStepId = select.value; }); chooser.append(select); actions.append(chooser);
      }
      const analyze = element('button', 'button primary-button', '负载特征分析');
      analyze.id = 'trace-analyze-features'; analyze.disabled = !archiveAvailable;
      analyze.title = '自动同步完整 capture.zip 到当前 SSH 服务器环境，创建数据集并进入负载特征页';
      analyze.addEventListener('click', () => void perform('analyze-trace', () => analyzeTrace(id, selectedStepId), 'trace-notice'));
      actions.append(analyze);
    }
    if (directory) { const open = element('button', 'button', '打开采集目录'); open.disabled = !directoryAvailable; open.addEventListener('click', () => void perform('open-trace', () => invoke('openPath', { path: directory }), 'trace-notice')); actions.append(open); }
    if (record.archivePath) { const archive = element('button', 'button primary-button', '定位采集包'); archive.disabled = !archiveAvailable; archive.addEventListener('click', () => void perform('open-archive', () => invoke('openPath', { path: record.archivePath }), 'trace-notice')); actions.append(archive); }
    const remove = element('button', 'button', '移除登记'); remove.id = 'trace-remove'; remove.title = '只移除本机列表登记，保留原始目录和采集包'; remove.addEventListener('click', () => void perform('remove-trace', async () => { await invoke('removeTrace', { id }); selectedTraceId = ''; traceDetailRevision++; await refresh(); $('trace-detail').replaceChildren(element('div', 'capture-empty', '登记已移除，原始采集目录和文件保持不变。')); notify('已移除登记，原始文件仍保留在本地。', 'success', 'trace-notice'); }, 'trace-notice')); actions.append(remove);
    body.append(actions); if (directory) body.append(element('div', 'trace-path', directory)); body.append(element('p', 'capture-format-note', '原始数据保留在本地；负载特征分析会自动同步完整 capture.zip，并保留 Trace、PMU 与采集元数据的对应关系。')); container.append(body);
  }
  function dataStatus(value) { if (!value) return '未记录'; if (typeof value === 'string') return ({ collected: '已返回', partial: '部分数据', invalid: '校验未通过', missing: '缺失', unavailable: '不可用' })[value] || value; return statusLabels[value.status] || value.status || (value.path || value.file ? '已返回' : '见目录'); }

  for (const id of ['capture-title', 'capture-app-bundle', 'capture-app-ability', 'capture-profile', 'capture-preparation', 'capture-period', 'capture-duration', 'capture-pmu-group', 'capture-trace', 'capture-hilog', 'capture-pmu']) $(id).addEventListener('input', markDirty);
  $('capture-instructions').addEventListener('input', () => updateCustomSteps($('capture-instructions').value));
  for (const id of ['capture-app-bundle', 'capture-app-ability']) $(id).addEventListener('input', () => { $(id).setAttribute('aria-invalid', 'false'); try { const profile = JSON.parse($('capture-profile').value || '{}'); profile.bundle = $('capture-app-bundle').value; profile.ability = $('capture-app-ability').value; $('capture-profile').value = JSON.stringify(profile, null, 2); } catch { /* Raw JSON validation is reported when saving. */ } });
  $('capture-profile').addEventListener('input', () => { try { const profile = JSON.parse($('capture-profile').value || '{}'); $('capture-app-bundle').value = profile.bundle || ''; $('capture-app-ability').value = profile.ability || ''; $('capture-app-bundle').setAttribute('aria-invalid', 'false'); $('capture-app-ability').setAttribute('aria-invalid', 'false'); } catch { /* Let the user finish editing JSON before synchronizing fields. */ } });
  $('capture-preparation').addEventListener('change', renderStepSummary);
  $('capture-refresh').addEventListener('click', () => void perform('refresh', refresh));
  $('capture-select-all').addEventListener('click', () => { for (const input of $('capture-steps').querySelectorAll('[data-step-selected]')) { input.checked = true; input.closest('tr')?.classList.add('selected-step'); } syncSelected(); markDirty(); });
  $('capture-clear-selection').addEventListener('click', () => { for (const input of $('capture-steps').querySelectorAll('[data-step-selected]')) { input.checked = false; input.closest('tr')?.classList.remove('selected-step'); } syncSelected(); markDirty(); });
  $('capture-template').addEventListener('change', () => { const templateId = $('capture-template').value; displayedRunId = ''; loadPlan(basePlan(templateId)); dirty = true; renderRun(); updateControls(); });
  $('capture-saved-plan').addEventListener('change', () => { const record = snapshot.plans.find((item) => item.planId === $('capture-saved-plan').value); if (!record) return; displayedRunId = ''; loadPlan(record.plan, record); renderRun(); notify(''); focusWorkflowAction('capture-start'); });
  $('capture-add-step').addEventListener('click', () => { if (!draft) return; syncSelected(); let index = draft.steps.length + 1; while (draft.steps.some((step) => step.step_id === `step_${String(index).padStart(2, '0')}`)) index++; draft.steps.push({ step_id: `step_${String(index).padStart(2, '0')}`, slug: `action_${index}`, title: '新的操作步骤', operation: 'unresolved', expected_targets: [] }); markDirty(); renderSteps(); });
  $('capture-save').addEventListener('click', () => void perform('save', async () => { await save(); notify('采样计划已保存，可直接执行或稍后从已保存计划中复用。', 'success'); focusWorkflowAction('capture-start'); }));
  $('capture-start').addEventListener('click', () => {
    const active = snapshot.activeRun && activeStatuses.has(snapshot.activeRun.status);
    if (active) {
      void perform('cancel', async () => { const run = snapshot.activeRun; if (!run) return; const updated = await invoke('cancelCapture', { runId: run.runId }); snapshot.activeRun = updated; renderRun(); });
      return;
    }
    const operationAttemptId = nextCaptureAttemptId();
    let attempt = null;
    void perform('start', async () => {
      if (!snapshot.selectedDeviceSerial) throw new Error('请先选择要采样的设备。');
      const deviceSerial = snapshot.selectedDeviceSerial;
      const hdcPath = $('hdc-path').value.trim() || 'hdc';
      const record = await save();
      const blockers = record.plan?.readiness?.blockers || [];
      if (blockers.length) { revealTemplateDetails(); throw new Error(`计划尚不可执行：${blockers.slice(0, 5).map(blockerText).join('；')}${blockers.length > 5 ? '；请继续补充其他计划信息' : ''}`); }
      attempt = createCaptureAttempt({operationAttemptId, record, deviceSerial, hdcPath});
      return launchCaptureAttempt(attempt);
    }, 'capture-notice', {
      errorContext: () => synchronousCaptureFailureDetails(attempt),
    });
  });
  $('capture-device').addEventListener('change', () => void perform('device', async () => { const serial = $('capture-device').value; await invoke('selectCaptureDevice', { serial }); snapshot.selectedDeviceSerial = serial; updateDevices(devices); }));
  $('capture-discover').addEventListener('click', () => void discoverDevices({ reportEmpty: true }));
  $('capture-device-settings').addEventListener('click', () => setPage('devices'));
  $('capture-auto-recovery-settings').addEventListener('click', () => openSettings?.('automation'));
  $('capture-go-traces').addEventListener('click', () => setPage('traces'));
  $('capture-run-details').addEventListener('click', () => { const run = currentRun(); if (run?.traceId) { setPage('traces'); void perform('trace-detail', () => showTrace(run.traceId), 'trace-notice'); } });
  $('capture-agent-plan').addEventListener('click', () => void perform('agent', async () => {
    const record = await save(); const request = $('capture-agent-request').value.trim();
    const blockers = record.plan?.readiness?.blockers || [];
    if (!request && blockers.length === 0) {
      notify('当前计划已经完整，无需重复生成；可以直接在设备执行采样。', 'success');
      focusWorkflowAction('capture-start');
      return;
    }
    const requestId = `capture_plan_${globalThis.crypto?.randomUUID?.().replaceAll('-', '') || `${Date.now()}_${Math.random().toString(16).slice(2)}`}`;
    const prompt = buildCapturePlanAgentTask({record, catalog, request: request || '补全当前 blockers 中尚未解析的动作与控件定位。', requestId});
    const submission = beginAgentPlanWait(record, requestId);
    try {
      await submitTask(prompt, {task_kind: 'capture_plan_refinement', capture_plan_id: record.planId, capture_plan_request_id: requestId, capture_plan_expected_updated_at: record.updatedAt});
      if (activeAgentPlanSubmission?.requestId === requestId) notify('计划补全任务已自动发送；助手只需提交一次差量更新。', 'success');
    } catch (error) { finishAgentPlanWait(submission, 'submit_failed'); throw error; }
  }));
  $('capture-apply-update').addEventListener('click', () => {
    if (!queuedAgentPlan) return;
    const candidate = queuedAgentPlan; queuedAgentPlan = null;
    loadPlan(candidate.plan, candidate);
    const blockers = candidate.plan?.readiness?.blockers || [];
    if (blockers.length) {
      revealTemplateDetails();
      notify(`已载入更新，计划仍有 ${blockers.length} 项未解析内容；请继续补充。`);
      focusWorkflowAction('capture-agent-plan');
    } else {
      notify('已载入助手更新的计划。', 'success');
      focusWorkflowAction('capture-start');
    }
  });
  $('trace-search').addEventListener('input', renderTraces);
  $('trace-refresh').addEventListener('click', () => void perform('refresh', async () => { await refresh(); if (selectedTraceId) await showTrace(selectedTraceId); }, 'trace-notice'));
  async function register(kind) { const path = await invoke('choosePath', { kind, filters: kind === 'file' ? [{ name: '采集归档', extensions: ['zip'] }] : undefined }); if (!path) return; const record = await invoke('registerTrace', { path }); await refresh(); const id = record.id || record.trace?.id; if (id) await showTrace(id); notify('采集数据已登记。', 'success', 'trace-notice'); }
  $('trace-register-directory').addEventListener('click', () => void perform('register', () => register('directory'), 'trace-notice'));
  $('trace-register-archive').addEventListener('click', () => void perform('register', () => register('file'), 'trace-notice'));

  async function initialize(next) {
    applyState(next);
    if (initialized) return initialization;
    initialized = true;
    initialization = (async () => {
      await perform('catalog', async () => { catalog = await invoke('captureCatalog'); if (!catalog?.templates) throw new Error('采集模板不可用，请检查本地采集运行环境。'); const select = $('capture-template'); select.replaceChildren(); for (const template of catalog.templates) select.append(new Option(template.name || template.template_id, template.template_id)); if (![...select.options].some((option) => option.value === 'custom')) select.append(new Option('自定义场景', 'custom')); if (!draft) { const saved = snapshot.plans?.[0]; loadPlan(saved?.plan || basePlan(), saved || null); } });
      if (activePage === 'capture') await discoverDevices();
    })();
    return initialization;
  }
  renderTraces(); updateDevices([]); updateControls();
  return { initialize, applyState, onEvent: event, updateDevices, onPage: (page) => {
    const enteredCapture = page === 'capture' && activePage !== 'capture';
    activePage = page;
    if (initialized && page === 'capture') void (async () => {
      try {
        await refresh();
        if (enteredCapture) await discoverDevices();
      } catch (error) { notify(messageOf(error), 'error', 'capture-notice', {operation: '进入负载采样页面', errorCode: errorCodeOf(error)}); }
    })();
    else if (initialized && page === 'traces') void refresh().catch((error) => notify(messageOf(error), 'error', 'trace-notice', {operation: '进入 Trace 管理页面', errorCode: errorCodeOf(error)}));
  }, updateAgentState: updateControls, dispose: () => { disposed = true; clearTimeout(refreshTimer); clearAgentPlanTimers(); } };
}
