import React from 'react';
import { createRoot } from 'react-dom/client';
import { WorkloadFeaturesStudio } from '../../app/components/WorkloadFeaturesStudio';
import { OptimizationCalibrationStudio } from '../../app/components/OptimizationCalibrationStudio';
import { ParallelModelingStudio } from '../../app/components/ParallelModelingStudio';
import { HardwareModelsStudio } from '../../app/components/HardwareModelsStudio';
import { installPlatformTransport } from '../../app/lib/api';
import {
  agentCommandEventName,
  reportAgentAutonomousTaskStatus,
  reportAgentCommandStatus,
} from '../../app/lib/agent-command-bridge';
import agentErrorRepair from './agent-error-repair.cjs';
import platformTransportContract from './platform-transport.cjs';
import {
  DesktopApplicationFlowPage,
  DesktopCodeKnowledgePage,
  DesktopExperiencePage,
  DesktopOpportunityReasoningPage,
  DesktopOptimizationKnowledgePage,
  DesktopSkillsPage,
  DesktopSourceRepositoriesPage,
} from './migrated-pages.jsx';
import './shared-pages.css';

const {buildAgentRepairPrompt, repairableAgentError} = agentErrorRepair;
const {assertPlatformContextUnchanged, buildPlatformRequest, createConcurrentTaskQueue, createPlatformContextMonitor, parsePlatformResponse, platformRequestTimeout, platformResultError, recoverRemoteWorker, resolvePlatformContext, serverProfileInitializationRequired} = platformTransportContract;

function methodOf(init = {}) { return String(init.method || 'GET').toUpperCase(); }
function abortError() { return new DOMException('请求已取消', 'AbortError'); }
async function utf8Digest(value) {
  const bytes = new TextEncoder().encode(String(value));
  if (!globalThis.crypto?.subtle) throw new Error('当前工作台无法计算校准实测文件 SHA-256。');
  const digest = await globalThis.crypto.subtle.digest('SHA-256', bytes);
  return {bytes, sha256: [...new Uint8Array(digest)].map(item => item.toString(16).padStart(2, '0')).join('')};
}

export function createDesktopPlatformTransport({invoke, getAppState}) {
  const requestControllers = new Set();
  const requestQueue = createConcurrentTaskQueue();
  const managedWorkerRoots = new Map();
  async function runPlatform(spec, expectedTarget, signal, timeoutMs, restartChannel = false) {
    if (signal?.aborted) throw abortError();
    const requestId = globalThis.crypto.randomUUID();
    const pending = invoke('platformRequest', {
      startupCommand: spec.command,
      request: spec.request,
      timeoutMs,
      expectedTarget,
      requestId,
      restartChannel,
    });
    let abortListener;
    const abortPromise = signal && new Promise((_, reject) => {
      abortListener = () => {
        void invoke('cancelPlatformRequest', {requestId, expectedTarget}).catch(() => {});
        reject(abortError());
      };
      signal.addEventListener('abort', abortListener, {once: true});
    });
    try { return abortPromise ? await Promise.race([pending, abortPromise]) : await pending; }
    finally {
      if (abortListener) signal.removeEventListener('abort', abortListener);
      if (signal?.aborted) void pending.catch(() => {});
    }
  }
  async function execute(path, init = {}) {
    if (init.signal?.aborted) throw abortError();
    const state = getAppState();
    const requestContext = resolvePlatformContext(state);
    const {expectedTarget, profileId, scopeId} = requestContext;
    const timeoutMs = platformRequestTimeout(path, methodOf(init));
    const workerKey = JSON.stringify([expectedTarget, profileId, scopeId, requestContext.profileKey]);
    const requestSpec = platformWorkerRoot => buildPlatformRequest({profileId, scopeId,
      profile: state.settings?.serverProfile || {}, platformWorkerRoot, path, method: methodOf(init), body: init.body,
      streaming: true});
    let spec = requestSpec(managedWorkerRoots.get(workerKey) || '');
    let result = await runPlatform(spec, expectedTarget, init.signal, timeoutMs);
    if (init.signal?.aborted) throw abortError();
    assertPlatformContextUnchanged(requestContext, getAppState());
    if (serverProfileInitializationRequired(result)) {
      const current = getAppState();
      try {
        await invoke('ensureServerProfile', {profile: current.settings?.serverProfile || {}, target: expectedTarget});
      } catch (error) {
        throw new Error(`服务器分析环境自动初始化失败：${String(error?.message || error || '未知错误').replace(/^Error invoking remote method '[^']+': Error: /, '')}`);
      }
      if (init.signal?.aborted) throw abortError();
      assertPlatformContextUnchanged(requestContext, getAppState());
      result = await runPlatform(spec, expectedTarget, init.signal, timeoutMs);
      if (init.signal?.aborted) throw abortError();
      assertPlatformContextUnchanged(requestContext, getAppState());
    }
    const recovered = await recoverRemoteWorker(result, {
      deploy: () => invoke('ensureRemoteAnalysisWorker', {profile: state.settings?.serverProfile || {}, target: expectedTarget}),
      retry: async root => {
        if (init.signal?.aborted) throw abortError();
        assertPlatformContextUnchanged(requestContext, getAppState());
        managedWorkerRoots.set(workerKey, root);
        spec = requestSpec(root);
        const retried = await runPlatform(spec, expectedTarget, init.signal, timeoutMs);
        if (init.signal?.aborted) throw abortError();
        assertPlatformContextUnchanged(requestContext, getAppState());
        return retried;
      },
    });
    result = recovered.result;
    if (result.exitCode !== 0) throw platformResultError(result);
    let response = await parsePlatformResponse(result.stdout);
    if (init.signal?.aborted) throw abortError();
    assertPlatformContextUnchanged(requestContext, getAppState());
    if (!response.ok && ['CODE_GRAPH_EXTRACTOR_UNAVAILABLE', 'DESKTOP_ANALYSIS_RUNTIME_UNAVAILABLE'].includes(response.error?.code)) {
      try {
        await invoke('ensureRemoteCodeAnalysisRuntime', {
          profile: state.settings?.serverProfile || {}, target: expectedTarget,
        });
      } catch (error) {
        throw new Error(String(error?.message || error || '服务器代码分析环境自动准备失败。')
          .replace(/^Error invoking remote method '[^']+': Error: /, ''));
      }
      if (init.signal?.aborted) throw abortError();
      assertPlatformContextUnchanged(requestContext, getAppState());
      result = await runPlatform(spec, expectedTarget, init.signal, timeoutMs, true);
      if (result.exitCode !== 0) throw platformResultError(result);
      response = await parsePlatformResponse(result.stdout);
      if (init.signal?.aborted) throw abortError();
      assertPlatformContextUnchanged(requestContext, getAppState());
    }
    if (!response.ok) {
      const error = new Error(response.error?.message || '服务器分析请求失败。');
      error.code = response.error?.code || 'DESKTOP_PLATFORM_REQUEST_FAILED';
      error.details = response.error?.details || {};
      error.statusCode = response.error?.status_code || null;
      throw error;
    }
    return response.result;
  }
  async function schedule(path, init = {}, scheduling = {}) {
    const controller = new AbortController();
    const externalSignal = init.signal;
    const abort = () => controller.abort();
    if (externalSignal?.aborted) controller.abort();
    else externalSignal?.addEventListener('abort', abort, {once: true});
    requestControllers.add(controller);
    try {
      return await requestQueue.run(() => execute(path, {...init, signal: controller.signal}), {
        signal: controller.signal,
        priority: scheduling.priority,
      });
    } finally {
      externalSignal?.removeEventListener('abort', abort);
      requestControllers.delete(controller);
    }
  }
  return {
    cancelAll() {
      for (const controller of requestControllers) controller.abort();
      requestQueue.cancelPending();
    },
    async request(path, init, scheduling) {
      const result = await schedule(path, init, scheduling);
      if (result?.kind !== 'json') throw new Error('服务器没有返回 JSON 数据。');
      return result.value;
    },
    async requestText(path, init, scheduling) {
      const result = await schedule(path, init, scheduling);
      if (result?.kind !== 'text' || typeof result.content !== 'string') throw new Error('服务器没有返回文本数据。');
      return {text: result.content, contentType: result.content_type, filename: result.filename};
    },
  };
}

export function createSharedPages({invoke, getAppState, setPage, submitTask}) {
  const analysisPages = new Set([
    'workload-features', 'hardware-models', 'optimization-calibration', 'parallel-modeling',
    'application-flow', 'opportunity-reasoning', 'source-repositories', 'code-knowledge',
    'optimization-knowledge', 'skills', 'experience',
  ]);
  const captureTraceRefreshPages = new Set(['application-flow', 'opportunity-reasoning']);
  const pageLabels = {
    'workload-features': '负载特征', 'hardware-models': '硬件模型',
    'optimization-calibration': '寻优校准', 'parallel-modeling': '并行建模',
    'application-flow': '应用流程', 'opportunity-reasoning': '机会推理',
    'source-repositories': '代码仓', 'code-knowledge': '代码知识库',
    'optimization-knowledge': '优化知识库', 'skills': '技能列表', 'experience': '经验演进',
  };
  const hosts = {
    'workload-features': document.getElementById('workload-features-root'),
    'hardware-models': document.getElementById('hardware-models-root'),
    'optimization-calibration': document.getElementById('optimization-calibration-root'),
    'parallel-modeling': document.getElementById('parallel-modeling-root'),
    'application-flow': document.getElementById('application-flow-root'),
    'opportunity-reasoning': document.getElementById('opportunity-reasoning-root'),
    'source-repositories': document.getElementById('source-repositories-root'),
    'code-knowledge': document.getElementById('code-knowledge-root'),
    'optimization-knowledge': document.getElementById('optimization-knowledge-root'),
    'skills': document.getElementById('skills-root'),
    'experience': document.getElementById('experience-root'),
  };
  const roots = {};
  const mounted = new Set();
  let activePage = '';
  let featureDatasetId;
  let featureProfileId;
  let modelingDatasetId;
  let modelingModelId;
  let modelingFeatureProfileId;
  let modelingHandoffRevision = 0;
  let hardwareDraft;
  let hardwareDraftRevision = 0;
  const repairCandidates = new Map();
  const repairPanels = new Map();

  function repairPanel(page) {
    if (repairPanels.has(page)) return repairPanels.get(page);
    const panel = document.createElement('aside');
    panel.className = 'desktop-agent-repair';
    panel.hidden = true;
    panel.setAttribute('role', 'alert');
    const copy = document.createElement('div');
    const title = document.createElement('strong');
    title.textContent = '分析环境或依赖需要修复';
    const message = document.createElement('span');
    message.textContent = '可使用 Code Agent 检查当前 SSH 服务器，修复后验证同一分析请求。';
    copy.append(title, message);
    const action = document.createElement('button');
    action.type = 'button';
    action.className = 'button primary-button desktop-agent-repair-action';
    action.textContent = '使用 Code Agent 修复';
    const dismiss = document.createElement('button');
    dismiss.type = 'button';
    dismiss.className = 'desktop-agent-repair-dismiss';
    dismiss.setAttribute('aria-label', '关闭修复提示');
    dismiss.textContent = '×';
    dismiss.addEventListener('click', () => { panel.hidden = true; });
    action.addEventListener('click', async () => {
      const candidate = repairCandidates.get(page);
      if (!candidate || typeof submitTask !== 'function') return;
      action.disabled = true;
      action.textContent = '正在提交…';
      try {
        await submitTask(buildAgentRepairPrompt({...candidate, state: getAppState()}));
        panel.classList.add('submitted');
        title.textContent = '修复任务已提交给 Code Agent';
        message.textContent = 'Agent 将通过当前 SSH 连接诊断、修复并验证；执行过程显示在右侧。';
        action.textContent = '已提交';
      } catch (error) {
        panel.classList.remove('submitted');
        title.textContent = 'Code Agent 修复任务提交失败';
        message.textContent = String(error?.message || error || '请检查 Code Agent 配置后重试。');
        action.disabled = false;
        action.textContent = '重新提交';
      }
    });
    panel.append(copy, action, dismiss);
    hosts[page].before(panel);
    const value = {panel, title, message, action};
    repairPanels.set(page, value);
    return value;
  }

  function clearAgentRepair(page, path, init = {}) {
    const candidate = repairCandidates.get(page);
    if (!candidate) return;
    if (path && (candidate.path !== path || candidate.method !== methodOf(init))) return;
    repairCandidates.delete(page);
    const value = repairPanels.get(page);
    if (!value) return;
    value.panel.hidden = true;
    value.panel.classList.remove('submitted');
  }

  function offerAgentRepair(error, path, init = {}, requestPage = activePage) {
    if (!repairableAgentError(error) || typeof submitTask !== 'function') return;
    const page = analysisPages.has(requestPage) ? requestPage : 'workload-features';
    repairCandidates.set(page, {error, page, pageLabel: pageLabels[page], path, method: methodOf(init)});
    const {panel, title, message, action} = repairPanel(page);
    panel.classList.remove('submitted');
    panel.hidden = false;
    title.textContent = '分析环境或依赖需要修复';
    message.textContent = '可使用 Code Agent 检查当前 SSH 服务器，修复后验证同一分析请求。';
    action.disabled = false;
    action.textContent = '使用 Code Agent 修复';
  }

  const transport = createDesktopPlatformTransport({invoke, getAppState});
  const pageScheduling = requestPage => ({
    // Read this value when the queued request is selected, not when it is
    // enqueued. Navigating therefore promotes the new page's pending work while
    // preserving hidden pages and their state.
    priority: () => requestPage === activePage ? 1 : 0,
  });
  const platform = {
    cancelAll: () => transport.cancelAll(),
    async request(path, init, context = {}) {
      const requestPage = analysisPages.has(context.owner) ? context.owner : activePage;
      try {
        const result = await transport.request(path, init, pageScheduling(requestPage));
        clearAgentRepair(requestPage, path, init);
        return result;
      }
      catch (error) { offerAgentRepair(error, path, init, requestPage); throw error; }
    },
    async requestText(path, init, context = {}) {
      const requestPage = analysisPages.has(context.owner) ? context.owner : activePage;
      try {
        const result = await transport.requestText(path, init, pageScheduling(requestPage));
        clearAgentRepair(requestPage, path, init);
        return result;
      }
      catch (error) { offerAgentRepair(error, path, init, requestPage); throw error; }
    },
  };
  const uninstallTransport = installPlatformTransport(platform);

  function resetPages() {
    platform.cancelAll();
    for (const [page, root] of Object.entries(roots)) {
      root.unmount();
      delete roots[page];
    }
    mounted.clear();
    featureDatasetId = undefined;
    featureProfileId = undefined;
    modelingDatasetId = undefined;
    modelingModelId = undefined;
    modelingFeatureProfileId = undefined;
    modelingHandoffRevision = 0;
    hardwareDraft = undefined;
    hardwareDraftRevision = 0;
    for (const {panel} of repairPanels.values()) panel.hidden = true;
    repairCandidates.clear();
  }

  function rootFor(page) {
    if (!roots[page]) roots[page] = createRoot(hosts[page]);
    mounted.add(page);
    return roots[page];
  }
  async function listCaptureSources() {
    const value = await invoke('listTraceAnalysisSources');
    const sources = [];
    for (const trace of Array.isArray(value?.sources) ? value.sources : []) {
      for (const step of Array.isArray(trace?.steps) ? trace.steps : []) sources.push({
        traceId: trace.traceId, runId: trace.runId, stepId: step.stepId,
        title: trace.title || trace.runId || trace.traceId, planTitle: trace.planTitle || '',
        stepOrdinal: step.stepOrdinal, stepTitle: step.title || step.stepId,
        status: trace.status || '', registeredAt: trace.registeredAt,
        ready: Boolean(step.ready), reason: step.reason || '',
      });
    }
    return sources;
  }
  function captureDataset(datasets, source) {
    return (Array.isArray(datasets) ? datasets : []).find(dataset => {
      const capture = dataset?.provenance?.capture;
      return capture?.run_id === source.runId && capture?.step_id === source.stepId;
    });
  }
  async function prepareCaptureDataset(source) {
    if (!source?.traceId || !source?.stepId) throw new Error('请选择可分析的本机采集步骤。');
    const requestContext = resolvePlatformContext(getAppState());
    const descriptor = await invoke('prepareTraceArchive', {traceId: source.traceId});
    assertPlatformContextUnchanged(requestContext, getAppState());
    const contract = {archive_sha256: descriptor.archiveSha256, archive_size_bytes: descriptor.archiveSizeBytes};
    const staging = await platform.request('/v1/desktop-capture/staging', {method: 'POST', body: JSON.stringify(contract)});
    if (!staging?.upload_token || !staging?.remote_path) throw new Error('服务器没有返回采集归档托管暂存位置。');
    const transfer = {traceId: source.traceId, uploadToken: staging.upload_token, remotePath: staging.remote_path,
      archiveSha256: descriptor.archiveSha256, archiveSizeBytes: descriptor.archiveSizeBytes,
      expectedTarget: requestContext.expectedTarget};
    let uploaded = false;
    let importRequested = false;
    try {
      await invoke('uploadTraceArchive', transfer); uploaded = true;
      assertPlatformContextUnchanged(requestContext, getAppState());
      importRequested = true;
      const imported = await platform.request('/v1/desktop-capture/import-package', {method: 'POST', body: JSON.stringify({
        upload_token: staging.upload_token, ...contract,
      })});
      const selected = captureDataset(imported?.datasets, source);
      if (!selected) {
        const failure = (Array.isArray(imported?.dataset_imports) ? imported.dataset_imports : []).find(item => item?.step_id === source.stepId);
        throw new Error(failure?.message || failure?.error?.message || `采集包已同步，但步骤「${source.stepTitle}」没有生成可分析的数据集。`);
      }
      return selected;
    } catch (error) {
      if (!uploaded || !importRequested) await invoke('discardTraceUpload', transfer).catch(() => {});
      // Cleanup has its own idempotent contract. Never retry import as cleanup:
      // an interrupted response may still have completed dataset creation.
      await platform.request('/v1/desktop-capture/discard-staging', {method: 'POST', body: JSON.stringify({
        upload_token: staging.upload_token, ...contract,
      })}).catch(() => {});
      throw error;
    }
  }
  async function prepareCaptureRuntimeTrace(source, sourceConnectorId) {
    if (!source?.traceId || !source?.stepId) throw new Error('请选择可分析的本机采集步骤。');
    if (!sourceConnectorId) throw new Error('请选择应用流程对应的代码仓。');
    const requestContext = resolvePlatformContext(getAppState());
    const descriptor = await invoke('prepareTraceArchive', {traceId: source.traceId});
    assertPlatformContextUnchanged(requestContext, getAppState());
    const contract = {archive_sha256: descriptor.archiveSha256, archive_size_bytes: descriptor.archiveSizeBytes};
    const staging = await platform.request('/v1/desktop-capture/staging', {method: 'POST', body: JSON.stringify(contract)});
    if (!staging?.upload_token || !staging?.remote_path) throw new Error('服务器没有返回采集归档托管暂存位置。');
    const transfer = {traceId: source.traceId, uploadToken: staging.upload_token, remotePath: staging.remote_path,
      archiveSha256: descriptor.archiveSha256, archiveSizeBytes: descriptor.archiveSizeBytes,
      expectedTarget: requestContext.expectedTarget};
    let uploaded = false;
    let importRequested = false;
    try {
      await invoke('uploadTraceArchive', transfer); uploaded = true;
      assertPlatformContextUnchanged(requestContext, getAppState());
      importRequested = true;
      const imported = await platform.request('/v1/desktop-capture/import-runtime-trace', {method: 'POST', body: JSON.stringify({
        upload_token: staging.upload_token, ...contract,
        step_id: source.stepId, source_connector_id: sourceConnectorId,
      })});
      if (!imported?.trace?.artifact_id) throw new Error('采集包已同步，但没有生成可供应用流程使用的真实 Trace 工件。');
      return imported.trace;
    } catch (error) {
      if (!uploaded || !importRequested) await invoke('discardTraceUpload', transfer).catch(() => {});
      await platform.request('/v1/desktop-capture/discard-staging', {method: 'POST', body: JSON.stringify({
        upload_token: staging.upload_token, ...contract,
      })}).catch(() => {});
      throw error;
    }
  }
  async function fitCalibrationMeasurements({planId, file, text}) {
    if (!planId) throw new Error('请选择校准方案。');
    const {bytes, sha256} = await utf8Digest(text);
    if (!bytes.byteLength || bytes.byteLength > 20 * 1024 * 1024)
      throw new Error('校准实测 JSON 必须大于 0 且不超过 20 MiB。');
    const requestContext = resolvePlatformContext(getAppState());
    const contract = {
      plan_id: planId,
      measurement_size_bytes: bytes.byteLength,
      measurement_sha256: sha256,
      filename: String(file?.name || 'measurements.json'),
    };
    const staging = await platform.request('/v1/parallel-calibration/measurement-staging', {
      method: 'POST', body: JSON.stringify(contract),
    });
    if (!staging?.upload_token || !staging?.remote_path)
      throw new Error('服务器没有返回校准实测托管暂存位置。');
    const transfer = {
      uploadToken: staging.upload_token,
      remotePath: staging.remote_path,
      measurementText: text,
      measurementSha256: sha256,
      measurementSizeBytes: bytes.byteLength,
      expectedTarget: requestContext.expectedTarget,
    };
    const stagedContract = {
      plan_id: planId,
      upload_token: staging.upload_token,
      measurement_size_bytes: bytes.byteLength,
      measurement_sha256: sha256,
    };
    try {
      assertPlatformContextUnchanged(requestContext, getAppState());
      await invoke('uploadCalibrationMeasurements', transfer);
      assertPlatformContextUnchanged(requestContext, getAppState());
      return await platform.request('/v1/parallel-calibration/fits/from-staging', {
        method: 'POST', body: JSON.stringify(stagedContract),
      });
    } catch (error) {
      await invoke('discardCalibrationMeasurementUpload', transfer).catch(() => {});
      await platform.request('/v1/parallel-calibration/measurement-staging/discard', {
        method: 'POST', body: JSON.stringify(stagedContract),
      }).catch(() => {});
      throw error;
    }
  }
  function renderFeatures() {
    rootFor('workload-features').render(<WorkloadFeaturesStudio
      initialDatasetId={featureDatasetId}
      initialFeatureProfileId={featureProfileId}
      assistantEnabled={false}
      measurementByteLimit={16 * 1024}
      onListCaptureSources={listCaptureSources}
      onPrepareCaptureDataset={prepareCaptureDataset}
      onCreateExample={async () => {
        const catalog = await platform.request('/v1/parallel-modeling/catalog');
        const builtinId = catalog?.builtins?.[0]?.id;
        if (!builtinId) throw new Error('服务器没有可用的内置负载示例。');
        const dataset = await platform.request('/v1/parallel-modeling/datasets', {method: 'POST', body: JSON.stringify({builtin_id: builtinId})});
        return dataset?.id;
      }}
      onNavigateCapture={() => setPage('capture')}
      onNavigateCalibration={() => setPage('optimization-calibration')}
      onNavigateHardwareModels={() => setPage('hardware-models')}
      onNavigateModeling={(datasetId, featureProfileId) => {
        modelingDatasetId = datasetId;
        modelingFeatureProfileId = featureProfileId;
        modelingHandoffRevision += 1;
        renderModeling();
        setPage('parallel-modeling');
      }}
    />);
  }
  function renderCalibration() {
    rootFor('optimization-calibration').render(<OptimizationCalibrationStudio
      externalLinksEnabled={false}
      measurementImportEnabled={false}
      measurementByteLimit={20 * 1024 * 1024}
      onFitMeasurements={fitCalibrationMeasurements}
      onListCalibrationDevices={() => invoke('discoverDevices', {hdcPath: localHdcPath()})}
      onStartCalibrationRun={(request) => invoke('startCalibrationRun', {...request, hdcPath: localHdcPath()})}
      onGetCalibrationRun={(request) => invoke('calibrationRunStatus', request)}
      onCancelCalibrationRun={(request) => invoke('cancelCalibrationRun', request)}
      onGetCalibrationMeasurements={(request) => invoke('getCalibrationMeasurements', request)}
      onNavigateModeling={(modelId) => {
        modelingModelId = modelId;
        renderModeling();
        setPage('parallel-modeling');
      }}
      onNavigateHardwareModels={() => setPage('hardware-models')}
    />);
  }
  function localHdcPath() {
    return getAppState()?.settings?.hdcPath || 'hdc';
  }
  function renderHardwareModels() {
    rootFor('hardware-models').render(<HardwareModelsStudio
      initialDraft={hardwareDraft}
      draftRevision={hardwareDraftRevision}
      onListLocalDevices={() => invoke('discoverDevices', {hdcPath: localHdcPath()})}
      onExtractLocalHardware={(deviceSerial) => invoke('extractHardwareProfile', {deviceSerial, hdcPath: localHdcPath()})}
      onNavigateModeling={() => setPage('parallel-modeling')}
    />);
  }
  function renderModeling() {
    rootFor('parallel-modeling').render(<ParallelModelingStudio
      assistantEnabled={false}
      executionMode="inline"
      fileImportByteLimit={16 * 1024}
      captureSourcesEnabled={false}
      fullResultExportEnabled={false}
      onSaveReport={(report) => invoke('saveGeneratedHtmlReport', {html: report.html, filename: report.filename})}
      initialDatasetId={modelingDatasetId}
      initialModelId={modelingModelId}
      initialFeatureProfileId={modelingFeatureProfileId}
      handoffRevision={modelingHandoffRevision}
      onNavigateCapture={() => setPage('capture')}
      onNavigateCalibration={() => setPage('optimization-calibration')}
      onNavigateHardwareModels={() => setPage('hardware-models')}
      onNavigateFeatures={(datasetId) => {
        featureDatasetId = datasetId;
        featureProfileId = undefined;
        renderFeatures();
        setPage('workload-features');
      }}
    />);
  }

  function renderMigratedPage(page) {
    // These pages remain mounted while navigating. A fresh callback identity makes their
    // capture-source effects run again when the user returns, without discarding page state.
    const listFreshCaptureTraceSources = () => listCaptureSources();
    const discoverLocalDeviceCatalog = async () => {
      const state = await invoke('getState');
      const hdcPath = state?.settings?.hdcPath || localHdcPath();
      const discovered = await invoke('discoverDevices', {hdcPath});
      const devices = Array.isArray(discovered?.devices) ? discovered.devices : [];
      const serials = devices.map(item => String(item?.serial || item?.deviceSerial || item?.device_serial || '')).filter(Boolean);
      const previous = String(state?.capture?.selectedDeviceSerial || '');
      const selectedDeviceSerial = previous && serials.includes(previous) ? previous : serials.length === 1 ? serials[0] : '';
      return {devices, serials, selectedDeviceSerial, persistedSelectedDeviceSerial: previous, hdcPath};
    };
    const resolveLocalDevice = async (requestedDeviceSerial = '') => {
      const discovered = await discoverLocalDeviceCatalog();
      const requested = String(requestedDeviceSerial || '');
      if (requested && !discovered.serials.includes(requested)) throw new Error('所选本地 HDC 设备已离线，请刷新设备列表。');
      const deviceSerial = requested || discovered.selectedDeviceSerial;
      if (!deviceSerial) {
        if (discovered.serials.length > 1) throw new Error(`发现 ${discovered.serials.length} 台本地设备，请在此页选择目标设备。`);
        throw new Error('没有发现可用的本地 HDC 设备。');
      }
      if (deviceSerial !== discovered.persistedSelectedDeviceSerial) await invoke('selectCaptureDevice', {serial: deviceSerial});
      const extracted = await invoke('extractHardwareProfile', {deviceSerial, hdcPath: discovered.hdcPath});
      return {deviceSerial, deviceCount: discovered.serials.length, transport: 'local_hdc', profile: extracted?.profile || extracted};
    };
    const props = {invoke, setPage, submitTask, resolveLocalDevice, listLocalDevices: discoverLocalDeviceCatalog};
    const component = {
      'application-flow': <DesktopApplicationFlowPage {...props}
        onListCaptureTraceSources={listFreshCaptureTraceSources}
        onMaterializeCaptureTrace={prepareCaptureRuntimeTrace}/>,
      'opportunity-reasoning': <DesktopOpportunityReasoningPage {...props}
        onListCaptureTraceSources={listFreshCaptureTraceSources}
        onMaterializeCaptureTrace={prepareCaptureRuntimeTrace}/>,
      'source-repositories': <DesktopSourceRepositoriesPage {...props}/>,
      'code-knowledge': <DesktopCodeKnowledgePage {...props}/>,
      'optimization-knowledge': <DesktopOptimizationKnowledgePage {...props}/>,
      'skills': <DesktopSkillsPage {...props}/>,
      'experience': <DesktopExperiencePage {...props}/>,
    }[page];
    if (component) rootFor(page).render(component);
  }

  function mountPage(page) {
    if (page === 'workload-features' && !mounted.has(page)) renderFeatures();
    else if (page === 'hardware-models' && !mounted.has(page)) renderHardwareModels();
    else if (page === 'optimization-calibration' && !mounted.has(page)) renderCalibration();
    else if (page === 'parallel-modeling' && !mounted.has(page)) renderModeling();
    else if (analysisPages.has(page) && !mounted.has(page)) renderMigratedPage(page);
  }

  function syncContext() {
    return contextMonitor.sync();
  }

  const contextMonitor = createPlatformContextMonitor(getAppState, () => {
    resetPages();
    if (analysisPages.has(activePage)) mountPage(activePage);
  });

  async function handlePageAgentCommand(event) {
    const detail = event?.detail || {};
    const requestId = String(detail.request_id || '').trim();
    if (!requestId) return;
    try {
      if (typeof submitTask !== 'function' || !String(detail.message || '').trim())
        throw new Error('当前 Desktop 未连接可用的 Code Agent。');
      await submitTask(String(detail.message));
      const status = {requestId, status: 'accepted', message: '任务已发送到右侧 Code Agent。'};
      reportAgentCommandStatus(status);
    } catch (error) {
      const status = {requestId, status: 'rejected', message: String(error?.message || error || '任务提交失败。')};
      reportAgentCommandStatus(status);
    }
  }

  async function handlePageAutonomousTask(event) {
    const detail = event?.detail || {};
    const requestId = String(detail.request_id || globalThis.crypto?.randomUUID?.() || `desktop-agent-${Date.now()}`).trim();
    try {
      const message = String(detail.message || '').trim();
      if (typeof submitTask !== 'function' || !message)
        throw new Error('当前 Desktop 未连接可用的 Code Agent。');
      const skillId = String(detail.required_skill_id || detail.skill_id || '').trim();
      const contextFields = [
        'task_kind', 'application_flow_analysis_id', 'source_connector_id', 'source_snapshot_id',
        'subpath', 'graph_id', 'discovery_artifact_id', 'flow_id', 'scenario_artifact_id',
        'trace_analysis_artifact_id', 'analysis_artifact_id', 'evidence_artifact_id',
        'result_artifact_id', 'analysis_kind', 'source_scopes', 'selected_device',
        'scenario_capture_target', 'repair_request_id',
      ];
      const taskContext = Object.fromEntries(contextFields
        .filter(key => detail[key] !== undefined && detail[key] !== null && detail[key] !== '')
        .map(key => [key, detail[key]]));
      const contextBlock = Object.keys(taskContext).length
        ? `\n\n平台已绑定的本轮任务上下文（JSON；调用平台工具时保持这些 ID 和范围）：\n${JSON.stringify(taskContext, null, 2)}`
        : '';
      const task = skillId
        ? `请从 $HETERO_AGENT_SKILL_ROOT/${skillId}/SKILL.md 加载 ${skillId} Skill。加载完成后执行当前阶段任务：\n${message}${contextBlock}`
        : `${message}${contextBlock}`;
      await submitTask(task, {required_skill_id: skillId || undefined, ...taskContext});
      reportAgentAutonomousTaskStatus({requestId, status: 'accepted', message: '任务已发送到右侧 Code Agent。'});
    } catch (error) {
      reportAgentAutonomousTaskStatus({
        requestId,
        status: 'rejected',
        message: String(error?.message || error || '任务提交失败。'),
      });
    }
  }
  window.addEventListener(agentCommandEventName, handlePageAgentCommand);
  window.addEventListener('page-agent-autonomous-task', handlePageAutonomousTask);

  return {
    async extractHardwareFromDevice(deviceSerial, hdcPath = localHdcPath()) {
      const result = await invoke('extractHardwareProfile', {deviceSerial, hdcPath});
      hardwareDraft = result?.profile || result;
      hardwareDraftRevision += 1;
      renderHardwareModels();
      setPage('hardware-models');
      return result;
    },
    async openTraceInFeatures(traceId, stepId = '') {
      const sources = await listCaptureSources();
      const source = sources.find(item => item.traceId === traceId && (!stepId || item.stepId === stepId) && item.ready);
      if (!source) throw new Error('该采集记录没有通过质量校验且带标准 capture.zip 的采集步骤。');
      const dataset = await prepareCaptureDataset(source);
      const profile = await platform.request('/v1/workload-features/analyze', {method: 'POST', body: JSON.stringify({dataset_id: dataset.id})});
      featureDatasetId = dataset.id;
      featureProfileId = profile.id;
      renderFeatures();
      setPage('workload-features');
      return {dataset, profile, source};
    },
    onPage(page) {
      activePage = page;
      const contextChanged = syncContext();
      const alreadyMounted = mounted.has(page);
      mountPage(page);
      if (!contextChanged && alreadyMounted && captureTraceRefreshPages.has(page)) renderMigratedPage(page);
    },
    syncContext,
    dispose() {
      platform.cancelAll();
      uninstallTransport();
      window.removeEventListener(agentCommandEventName, handlePageAgentCommand);
      window.removeEventListener('page-agent-autonomous-task', handlePageAutonomousTask);
      Object.values(roots).forEach(root => root.unmount());
    },
  };
}
