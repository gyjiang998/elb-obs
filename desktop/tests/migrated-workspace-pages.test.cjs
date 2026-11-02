const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const assert = require('node:assert/strict');

const desktopRoot = path.resolve(__dirname, '..');
const repositoryRoot = path.resolve(desktopRoot, '..');
const source = relative => fs.readFileSync(path.join(repositoryRoot, relative), 'utf8');

test('migrated workspace pages keep the product workflow order and real React hosts', () => {
  const html = source('desktop/renderer/index.html');
  const pages = [...html.matchAll(/class="nav-item" data-page="([^"]+)"/g)].map(match => match[1]);
  const workflow = [
    'parallel-modeling', 'application-flow', 'opportunity-reasoning', 'source-repositories',
    'code-knowledge', 'optimization-knowledge', 'skills', 'traces', 'experience',
  ];
  assert.deepEqual(pages.filter(page => workflow.includes(page)), workflow);
  assert.deepEqual(pages.slice(-3), ['skills', 'traces', 'experience'],
    'Skill、Trace 与经验演进必须保持倒数第三、倒数第二和最后一项');
  for (const page of workflow.filter(page => page !== 'traces')) {
    assert.match(html, new RegExp(`id="${page}-page"[\\s\\S]*?id="${page}-root"`));
  }
});

test('migrated pages mount shared Web studios and submit page actions to Desktop Code Agent', () => {
  const pages = source('desktop/renderer/migrated-pages.jsx');
  const shared = source('desktop/renderer/shared-pages.jsx');
  const applicationFlow = source('app/components/ApplicationFlowStudio.tsx');
  for (const component of ['ApplicationFlowStudio', 'CodeKnowledgeStudio', 'ExperienceEvolutionStudio', 'SkillLibraryManager'])
    assert.match(pages, new RegExp(`import \\{${component}\\}`));
  assert.match(shared, /window\.addEventListener\(agentCommandEventName, handlePageAgentCommand\)/);
  assert.match(shared, /window\.addEventListener\('page-agent-autonomous-task', handlePageAutonomousTask\)/);
  assert.match(shared, /await submitTask\(String\(detail\.message\)\)/);
  assert.match(shared, /await submitTask\(task, \{required_skill_id: skillId \|\| undefined, \.\.\.taskContext\}\)/,
    'autonomous page tasks must bind trusted structured context to the Agent tool session');
  assert.match(shared, /reportAgentAutonomousTaskStatus\(\{requestId, status: 'accepted'/);
  assert.match(shared, /window\.removeEventListener\('page-agent-autonomous-task', handlePageAutonomousTask\)/);
  assert.match(pages, /runtimeTraceUploadEnabled=\{false\}/);
  assert.match(pages, /artifactDownloadsEnabled=\{false\}/);
  assert.match(pages, /runtimeTraceConnectorFilterEnabled=\{false\}/,
    'Desktop application flow must discover all server runtime traces, including traces created under older connector bindings');
  assert.match(shared, /onListCaptureTraceSources=\{listFreshCaptureTraceSources\}/);
  assert.match(shared, /'opportunity-reasoning': <DesktopOpportunityReasoningPage[\s\S]*onMaterializeCaptureTrace=\{prepareCaptureRuntimeTrace\}/);
  assert.match(shared, /\/v1\/desktop-capture\/import-runtime-trace/,
    'Desktop application flow must materialize registered local captures through the audited server import');
  assert.match(shared, /captureTraceRefreshPages = new Set\(\['application-flow', 'opportunity-reasoning'\]\)/);
  assert.match(shared, /const listFreshCaptureTraceSources = \(\) => listCaptureSources\(\)/);
  assert.match(shared, /alreadyMounted && captureTraceRefreshPages\.has\(page\)/,
    'returning to a mounted Trace consumer must refresh its local capture catalog');
  assert.match(applicationFlow, /limit=500/,
    'the application-flow Trace catalog must include the full managed catalog page');
  assert.match(applicationFlow, /connectorCatalogKey\.split\('\\u0000'\)\.includes\(traceConnectorId\)/);
  assert.match(applicationFlow, /loadedRuntimeTrace\?\.source_connector_id, onSelectConnector, selectedConnectorId/,
    'Trace provenance must be reapplied when the connector catalog arrives after Trace detail');
  assert.match(applicationFlow, /\[onListCaptureTraceSources, refreshRuntimeTraces\]/,
    'returning to the page must refresh the server Trace catalog together with local capture sources');
  const sessionView = source('desktop/renderer/agent-session-view.jsx');
  for (const method of ['listCodeAgentSessions', 'getCodeAgentSession', 'chooseCodeAgentSessionRoot', 'configureCodeAgentSessionRoot'])
    assert.match(sessionView, new RegExp(method));
  assert.match(pages, /<CodeAgentSessionExplorer[\s\S]*<DesktopAgentRunTimeline/,
    'experience evolution must show native Agent JSONL evidence before platform run summaries');
  assert.match(sessionView, /观察跨度/,
    'Agent JSONL timing must be labelled as an observed span rather than tool execution time');
  assert.match(sessionView, /value === null \|\| value === undefined \|\| value === ''/,
    'missing Agent timing must render as unknown instead of coercing null to zero');
  assert.match(sessionView, /累计观察跨度/);
  assert.match(sessionView, /各时间项口径不同、不可相加/);
  assert.match(sessionView, /RPC 往返（含远端处理）/);
  assert.match(sessionView, /启发式：目录与时间/);
  for (const timing of ['duration_scope', 'queue_wait_ms', 'transport_duration_ms',
    'channel_acquire_duration_ms', 'channel_startup_duration_ms'])
    assert.match(pages, new RegExp(timing), `platform Agent waterfall must expose ${timing}`);
  assert.match(pages, /平台墙钟/);
  assert.match(pages, /远端执行/);
  assert.match(pages, /RPC 往返（含远端处理）/);
  assert.match(pages, /各项为重叠诊断口径，不可相加/);
  assert.match(pages, /最慢单次平台操作/);
  assert.match(pages, /slowestOperation\.max_duration_ms/,
    '等待分析必须展示单次最慢操作，不能把同kind累计时间误当单次耗时');
});

test('managed analysis worker packages the workspace RPC, knowledge bases and Skill assets', () => {
  const prepare = source('desktop/build/prepare-analysis-worker.cjs');
  const runtime = source('desktop/runtime/analysis-worker.cjs');
  assert.match(prepare, /'desktop_workspace_rpc\.py'/);
  assert.match(prepare, /'capture_runtime_trace\.py'/);
  assert.match(prepare, /'custom_api_kb\.py'/);
  assert.match(prepare, /data\/knowledge-bases/);
  assert.match(prepare, /agent-resources\/skill-library/);
  assert.match(runtime, /'agent-resources','backend','data','tools'/);
  assert.match(runtime, /data\/knowledge-bases\/hardware\/npu_api_knowledge_base\.json/);
  assert.match(runtime, /data\/knowledge-bases\/hardware\/gpu_api_knowledge_base\.json/);
  assert.match(runtime, /custom-accelerator-api-kb-builder\/2\.0\.0\.md/);
});

test('Desktop optimization knowledge includes NPU, GPU and custom API catalogs', () => {
  const pages = source('desktop/renderer/migrated-pages.jsx');
  for (const label of ['NPU API', 'GPU API', '自建 API']) assert.match(pages, new RegExp(label));
  assert.match(pages, /\/v1\/hardware-kb\/npu-api\/records/);
  assert.match(pages, /\/v1\/hardware-kb\/accelerator-api\/gpu\/records/);
  assert.match(pages, /\/v1\/custom-api-kbs\/build/);
  assert.match(pages, /服务器代码目录/);
  assert.match(pages, /自建 API Skill 的合同扫描规则/);
});

test('Desktop opportunity reasoning preserves the real-trace Web methodology and local device profile', () => {
  const pages = source('desktop/renderer/migrated-pages.jsx');
  const shared = source('desktop/renderer/shared-pages.jsx');
  for (const contract of [
    '/v1/desktop-opportunity/traces',
    '/v1/desktop-opportunity/source-match',
    '/v1/desktop-opportunity/manual-code',
    '/v1/desktop-opportunity/hardware-route',
  ]) assert.ok(pages.includes(contract), `missing Desktop opportunity contract ${contract}`);
  assert.match(pages, /\/v1\/application-flows\?limit=500/,
    'opportunity reasoning must expose application-flow results with persisted Trace alignment');
  for (const input of ['应用流程结果', '独立 Trace', '粘贴热点代码'])
    assert.match(pages, new RegExp(input));
  assert.match(pages, /runtime_trace_analysis_artifact_id/,
    'application-flow input must bind its existing runtime Trace analysis artifact');
  assert.match(pages, /\/v1\/application-flows\/scenarios\/\$\{encodeURIComponent\(flowScenarioId\)\}/,
    'a selected application-flow result must load its immutable scenario detail');
  assert.match(pages, /record\(flowScenarioDetail\?\.scenario_json\)\.stages/,
    'the stage selector must be sourced from the selected scenario JSON');
  assert.match(pages, /runtimeStageIds\.includes\(text\(record\(item\?\.stage_context\)\.stage_id\)\)/,
    'application-flow hotspots must be filtered by their explicit Trace stage context');
  assert.match(pages, /flowStageOptions\.find\(item => item\.hotspotCount > 0\)\?\.stageId/,
    'the first stage with observed hotspots must be selected by default');
  assert.match(pages, /setFlowStageId\(event\.target\.value\); setHotspotId\(''\); setSourceMatchState\(null\); setRouteResultState\(null\)/,
    'switching application-flow stage must invalidate hotspot, source and route evidence');
  for (const field of ['scenario_stage_id', 'scenario_stage_name', 'trace_stage_ids', 'selected_hotspot_stage_context'])
    assert.match(pages, new RegExp(field), `Agent payload must include explicit ${field} stage context`);
  assert.match(pages, /stage_context/,
    'flow-derived hotspots must retain their stage context in both the UI and Agent payload');
  assert.match(pages, /manual_code_artifact_id: manualArtifactId/,
    'manual code must be persisted before hardware routing and referenced by immutable artifact id');
  assert.match(pages, /function chooseInputMode\(nextMode\)[\s\S]*clearDerivedEvidence\(\)[\s\S]*setManualCodeState\(null\)/,
    'switching opportunity input modes must clear derived evidence from the previous input');
  for (const step of ['目标设备与系统画像', '真实 Trace 与热点提取', '热点代码匹配', '模型语义路由与机会推理'])
    assert.match(pages, new RegExp(step));
  assert.match(pages, /heterogeneous-opportunity-orchestrator/);
  assert.match(pages, /平台没有按关键词预选硬件/);
  assert.match(pages, /desktop-opportunity\/traces\?limit=500/,
    'the opportunity selector must expose the complete bounded Trace catalog');
  assert.match(pages, /sourceSelectionKeyRef\.current !== requestInputKey/,
    'a slow source-match response must not cross Trace, hotspot or connector selections');
  assert.match(pages, /text\(connector\?\.snapshot\?\.snapshot_id\)/,
    'source and route evidence must be invalidated when the selected repository snapshot changes');
  assert.match(pages, /routeInputKeyRef\.current !== requestInputKey/,
    'a slow route response must remain bound to its source and device inputs');
  assert.match(pages, /setTraceDetail\(null\); setHotspotId\(''\)/,
    'switching Trace must clear the previous hotspot catalog before loading the next detail');
  assert.match(pages, /selection_owner|model_input_context/);
  assert.match(pages, /第一阶段只能读取 model_routing_context\.model_input_context/);
  assert.match(pages, /!text\(selectedSource\.code\)\.trim\(\)/);
  assert.match(pages, /已找到候选源码/);
  assert.doesNotMatch(pages, /matched_terms/);
  assert.doesNotMatch(pages, /routeResult\?\.selected_units/);
  assert.match(shared, /invoke\('discoverDevices', \{hdcPath\}\)/);
  assert.match(shared, /invoke\('extractHardwareProfile', \{deviceSerial, hdcPath\}\)/);
  assert.match(shared, /state\?\.capture\?\.selectedDeviceSerial/);
  assert.match(shared, /listLocalDevices: discoverLocalDeviceCatalog/);
  assert.match(pages, /发现.*台本地设备，请选择本轮分析设备/);
  assert.match(pages, /maskedDeviceLabel/);
  assert.match(pages, /deviceRequestRef = useRef\(\{generation: 0, inputKey: ''\}\)/,
    'device discovery must keep an explicit request generation and input identity');
  assert.match(pages, /generation: deviceRequestRef\.current\.generation \+ 1/);
  assert.match(pages, /if \(deviceRequestRef\.current !== request\) return/,
    'a slow device response must not overwrite a newer device selection');
  assert.match(pages, /const interactionBusy = deviceBusy \|\| Boolean\(busy\)/,
    'device controls must remain locked independently of concurrent Trace detail work');
  assert.doesNotMatch(pages, /busy === 'device'/,
    'device discovery must not share the single-value activity state with Trace loading');
  assert.match(pages, /device_serial_sha256: profile\?\.evidence\?\.device_serial_sha256/,
    'opportunity prompts must correlate a device without disclosing its raw HDC serial');
  assert.match(pages, /本机 Trace 管理/);
  assert.match(pages, /onMaterializeCaptureTrace\(source, catalog\.connectorId\)/,
    'opportunity reasoning must materialize registered local captures on demand');
  assert.match(pages, /converted_callstack_direct_child_subtraction/,
    'raw HTrace UI must recognize the shared Web direct-child subtraction profile');
  assert.match(pages, /插桩 Span 排他时间估计，不是采样 CPU self-time/,
    'raw HTrace self-time must remain an instrumentation-span estimate');
  assert.match(pages, /Trace Span 指标，不能解读为 CPU self-time/,
    'fallback hotspots must not be presented as sampled CPU self-time');
  assert.match(pages, /migrated-trace-module-summary/,
    'raw HTrace UI must summarize the Web module groups without expanding every event');
  assert.match(shared, /resolveLocalDevice/);
});
