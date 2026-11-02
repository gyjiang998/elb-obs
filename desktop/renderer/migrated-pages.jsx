import React, {useCallback, useEffect, useRef, useState} from 'react';
import {Activity, BookOpenCheck, ChevronRight, ClipboardPaste, Cpu, Database, FileCode2, FolderGit2, GitBranch, HardDrive, LoaderCircle, MonitorSmartphone, Network, RefreshCw, Route, Search, Send, Sparkles, Trash2} from 'lucide-react';
import {ApplicationFlowStudio} from '../../app/components/ApplicationFlowStudio';
import {CodeKnowledgeStudio} from '../../app/components/CodeKnowledgeStudio';
import {ExperienceEvolutionStudio} from '../../app/components/ExperienceEvolutionStudio';
import {SkillLibraryManager} from '../../app/components/SkillLibraryManager';
import {useWorkbenchErrorNotification} from '../../app/components/WorkbenchErrorNotification';
import {api} from '../../app/lib/api';
import {CodeAgentSessionExplorer} from './agent-session-view';
import './migrated-pages.css';

const text = (value, fallback = '') => typeof value === 'string' && value.trim() ? value.trim() : fallback;
const record = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
const list = value => Array.isArray(value) ? value : [];
const catalogUpdatedEvent = 'desktop-workspace-catalog-updated';

function runtimeStageIdsForFlowStage(stage, runtimeAlignment) {
  const stageId = text(stage?.id);
  if (!stageId) return [];
  const matches = list(record(runtimeAlignment).stage_matches);
  const match = record(matches.find(item => text(item?.stage_id) === stageId));
  const ids = new Set([stageId]);
  const addId = value => { const id = text(value); if (id) ids.add(id); };
  addId(stage?.runtime_stage_id);
  addId(match.runtime_stage_id);
  for (const value of [...list(stage?.runtime_stage_ids), ...list(match.runtime_stage_ids), ...list(match.reported_stage_ids)]) addId(value);
  const events = [match.start_event, match.end_event, ...list(match.supporting_events)];
  for (const value of events) {
    const event = record(value);
    addId(event.reported_stage_id);
    addId(record(event.stage_context).stage_id);
    for (const id of list(event.reported_stage_ids)) addId(id);
    for (const id of list(record(event.trace_context).reported_stage_ids)) addId(id);
  }
  return [...ids];
}

function ErrorNotice({error, source = 'migrated-page'}) {
  const notice = error ? {
    tone: 'error',
    text: text(error?.message, String(error)),
    errorCode: text(error?.code || error?.errorCode || error?.error_code),
    context: {
      status_code: error?.statusCode ?? error?.status_code ?? error?.status ?? undefined,
      repair_kind: error?.repairKind ?? error?.repair_kind ?? undefined,
    },
  } : null;
  const forwarded = useWorkbenchErrorNotification(notice, () => {}, source);
  if (!notice || forwarded) return null;
  return <div className="migrated-notice error" role="alert">{notice.text}</div>;
}

function MigratedNotice({notice, onDismiss = () => {}, source = 'migrated-page'}) {
  const forwarded = useWorkbenchErrorNotification(notice, onDismiss, source);
  if (!notice || forwarded) return null;
  return <div className={`migrated-notice ${notice.tone}`}>{notice.text}</div>;
}

function LoadingPage({label}) {
  return <div className="migrated-page-loading"><LoaderCircle className="spin" size={18}/><span>{label}</span></div>;
}

function usePlatformCatalog() {
  const [overview, setOverview] = useState(null);
  const [connectorId, setConnectorId] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const value = await api('/v1/overview', {cache: 'no-store'});
      setOverview(value);
      const connectors = list(value?.connectors);
      setConnectorId(current => connectors.some(item => item.connector_id === current)
        ? current
        : value?.default_connector?.connector_id || connectors[0]?.connector_id || '');
      setError(null);
      return value;
    } catch (nextError) {
      setError(nextError);
      throw nextError;
    } finally { setLoading(false); }
  }, []);
  useEffect(() => {
    const handleCatalogUpdate = () => { void refresh().catch(() => {}); };
    void refresh().catch(() => {});
    window.addEventListener(catalogUpdatedEvent, handleCatalogUpdate);
    return () => window.removeEventListener(catalogUpdatedEvent, handleCatalogUpdate);
  }, [refresh]);
  return {overview, connectorId, setConnectorId, loading, error, refresh};
}

export function DesktopApplicationFlowPage({onListCaptureTraceSources, onMaterializeCaptureTrace}) {
  const catalog = usePlatformCatalog();
  if (catalog.loading && !catalog.overview) return <LoadingPage label="正在读取应用流程资产…"/>;
  return <div className="migrated-web-page">
    <ErrorNotice error={catalog.error} source="application-flow-catalog"/>
    <ApplicationFlowStudio
      connectors={list(catalog.overview?.connectors)}
      selectedConnectorId={catalog.connectorId}
      loadedTrace={null}
      onSelectConnector={catalog.setConnectorId}
      onRefreshConnectors={catalog.refresh}
      onAssistantContextChange={() => {}}
      runtimeTraceUploadEnabled={false}
      artifactDownloadsEnabled={false}
      runtimeTraceConnectorFilterEnabled={false}
      onListCaptureTraceSources={onListCaptureTraceSources}
      onMaterializeCaptureTrace={onMaterializeCaptureTrace}
    />
  </div>;
}

export function DesktopCodeKnowledgePage() {
  const catalog = usePlatformCatalog();
  if (catalog.loading && !catalog.overview) return <LoadingPage label="正在读取代码知识资产…"/>;
  return <div className="migrated-web-page">
    <ErrorNotice error={catalog.error} source="code-knowledge-catalog"/>
    <CodeKnowledgeStudio
      connectors={list(catalog.overview?.connectors)}
      selectedConnectorId={catalog.connectorId}
      onSelectConnector={catalog.setConnectorId}
      onAssistantContextChange={() => {}}
      artifactDownloadsEnabled={false}
    />
  </div>;
}

function agentOperationTiming(operation) {
  if (operation.duration_ms == null) return operation.state || '未计时';
  const scope = operation.duration_scope === 'remote_execution' ? '远端执行'
    : operation.duration_scope === 'broker_wall' ? '平台墙钟' : '旧记录口径未标明';
  const parts = [`${scope} ${Math.round(operation.duration_ms)} ms`];
  if (Number.isFinite(operation.queue_wait_ms)) parts.push(`排队 ${Math.round(operation.queue_wait_ms)} ms`);
  if (Number.isFinite(operation.channel_acquire_duration_ms)) parts.push(`获取通道 ${Math.round(operation.channel_acquire_duration_ms)} ms`);
  if (Number.isFinite(operation.transport_duration_ms)) parts.push(`RPC 往返（含远端处理） ${Math.round(operation.transport_duration_ms)} ms`);
  if (operation.channel_reused === false && Number.isFinite(operation.channel_startup_duration_ms))
    parts.push(`通道冷启基线 ${Math.round(operation.channel_startup_duration_ms)} ms`);
  if (operation.channel_reused === true) parts.push('复用长驻通道');
  return parts.join(' · ');
}

function DesktopAgentRunTimeline({invoke}) {
  const [runs, setRuns] = useState([]);
  const [selectedId, setSelectedId] = useState('');
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState(null);
  const refresh = useCallback(async () => {
    try {
      const value = await invoke('listAgentRuns', {limit: 100});
      const values = list(value?.runs);
      setRuns(values);
      setSelectedId(current => values.some(item => item.session_id === current) ? current : values[0]?.session_id || '');
      setError(null);
    } catch (nextError) { setError(nextError); }
  }, [invoke]);
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    if (!selectedId) { setDetail(null); return; }
    let active = true;
    void invoke('getAgentRun', {sessionId: selectedId}).then(value => { if (active) setDetail(value); }, nextError => { if (active) setError(nextError); });
    return () => { active = false; };
  }, [invoke, selectedId]);
  const selected = runs.find(item => item.session_id === selectedId);
  const slowestOperation = list(selected?.bottlenecks).reduce((slowest, item) => {
    if (!Number.isFinite(item?.max_duration_ms)) return slowest;
    return !slowest || item.max_duration_ms > slowest.max_duration_ms ? item : slowest;
  }, null);
  const tasks = list(detail?.tasks);
  const operations = list(detail?.operations).filter(item => item.state !== 'running').slice(-80);
  return <section className="work-section desktop-agent-history">
    <div className="section-heading"><div><span className="eyebrow">本机真实执行</span><h2>Code Agent 会话与等待分析</h2></div><button className="icon-button" title="刷新本机 Agent 历史" onClick={() => void refresh()}><RefreshCw size={15}/></button></div>
    <p>记录任务提交、首个输出、平台 RPC、SSH 命令与失败阶段；PTY 静默只记为未归因等待，不推断为模型思考。</p>
    <ErrorNotice error={error} source="agent-run-history"/>
    <div className="desktop-agent-history-layout">
      <div className="desktop-agent-run-list">{runs.map(run => <button key={run.session_id} className={run.session_id === selectedId ? 'selected' : ''} onClick={() => setSelectedId(run.session_id)}><div><strong>{run.title}</strong><small>{new Date(run.created_at).toLocaleString('zh-CN', {hour12: false})} · {run.status}</small></div><span>{run.task_count} 个任务</span></button>)}{runs.length === 0 && <div className="migrated-empty">还没有 Desktop Code Agent 执行记录。</div>}</div>
      <div className="desktop-agent-run-detail">{selected && <div className="desktop-agent-run-metrics"><span><small>任务</small><strong>{selected.task_count}</strong></span><span><small>工具操作</small><strong>{selected.operation_count}</strong></span><span><small>平均首响应</small><strong>{selected.first_output_latency_ms == null ? '—' : `${selected.first_output_latency_ms} ms`}</strong></span><span><small>最慢单次平台操作</small><strong>{slowestOperation ? `${slowestOperation.kind} · ${Math.round(slowestOperation.max_duration_ms)} ms${slowestOperation.count > 1 ? ` · ${slowestOperation.count} 次` : ''}` : '—'}</strong></span></div>}
        <small className="desktop-agent-timing-note">各项为重叠诊断口径，不可相加；RPC 往返包含远端处理。</small>
        <div className="desktop-agent-waterfall">{tasks.map(task => <article key={task.task_id}><span className="task">任务</span><div><strong>{task.prompt_preview?.split('\n')[0] || task.task_id}</strong><small>首响应 {task.first_output_latency_ms == null ? '未观察到' : `${task.first_output_latency_ms} ms`} · 输出 {task.output_bytes || 0} B</small>{task.output_preview && <details className="desktop-agent-exchange"><summary>查看交互片段</summary><pre><b>任务</b>{'\n'}{task.prompt_preview}{'\n\n'}<b>Agent 输出</b>{'\n'}{task.output_preview}</pre></details>}</div></article>)}{operations.map(operation => <article key={operation.operation_id}><span className={operation.state === 'error' || operation.state === 'failed' ? 'error' : 'tool'}>{operation.kind || operation.target || '工具'}</span><div><strong>{operation.method && operation.path ? `${operation.method} ${operation.path}` : operation.message || operation.target}</strong><small>{agentOperationTiming(operation)}</small></div></article>)}</div>
      </div>
    </div>
  </section>;
}

export function DesktopExperiencePage({setPage, invoke}) {
  return <div className="migrated-web-page workspace-scroll"><CodeAgentSessionExplorer invoke={invoke} ErrorNotice={ErrorNotice} LoadingPage={LoadingPage}/><DesktopAgentRunTimeline invoke={invoke}/><ExperienceEvolutionStudio
    onOpenView={view => setPage(view === 'reasoning' ? 'opportunity-reasoning' : view === 'sources' ? 'source-repositories' : 'skills')}
    onOpenSkill={skillId => {
      pendingSkillFocus = text(skillId);
      setPage('skills');
      window.dispatchEvent(new CustomEvent('desktop-skill-focus', {detail: {skill_id: pendingSkillFocus}}));
    }}
  /></div>;
}

let pendingSkillFocus = '';

function skillPresentation(manifest) {
  const input = record(manifest.input_schema);
  const output = record(manifest.output_schema);
  return {
    skill_id: text(manifest.skill_id),
    label_zh: text(manifest.label_zh, text(manifest.skill_id)),
    description_zh: text(manifest.description_zh, text(manifest.claim_boundary, '后端登记的可审计分析 Skill')),
    target_view: 'skills',
    prompt_slot: text(manifest.prompt_slot),
    fallback_content: '',
    execution: text(manifest.executor),
    input: list(input.required).join('、'),
    output: list(output.required).join('、'),
    boundary: text(manifest.claim_boundary),
  };
}

export function DesktopSkillsPage({setPage}) {
  const [library, setLibrary] = useState(null);
  const [manifests, setManifests] = useState([]);
  const [profileId, setProfileId] = useState('');
  const [error, setError] = useState(null);
  const [focusSkillId, setFocusSkillId] = useState(pendingSkillFocus);
  const refresh = useCallback(async () => {
    try {
      const [nextLibrary, actions] = await Promise.all([
        api('/v1/skill-version-library', {cache: 'no-store'}),
        api('/v1/page-actions', {cache: 'no-store'}),
      ]);
      setLibrary(nextLibrary);
      setManifests(list(actions?.skills));
      setProfileId(current => list(nextLibrary?.profiles).some(item => item.profile_id === current)
        ? current : text(nextLibrary?.default_profile_id));
      setError(null);
    } catch (nextError) { setError(nextError); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    const listener = event => {
      const skillId = text(event?.detail?.skill_id);
      if (!skillId) return;
      pendingSkillFocus = skillId;
      setFocusSkillId(skillId);
    };
    window.addEventListener('desktop-skill-focus', listener);
    return () => window.removeEventListener('desktop-skill-focus', listener);
  }, []);
  if (!library && !error) return <LoadingPage label="正在读取 Skill 版本库…"/>;
  return <div className="migrated-web-page workspace-scroll">
    <ErrorNotice error={error} source="skills-catalog"/>
    <section className="work-section skills-work-section">
      <div className="section-heading"><div><span className="eyebrow">可审计执行单元</span><h2>技能列表</h2></div></div>
      {library && <div className="skill-version-selector table-shell">
        <label className="skill-version-control"><span>运行时 Skill 配置</span><select value={profileId} onChange={event => setProfileId(event.target.value)}>{list(library.profiles).map(profile => <option key={profile.profile_id} value={profile.profile_id}>{profile.label_zh} · {profile.profile_id}</option>)}</select></label>
        <div className="skill-version-summary"><strong>{list(library.profiles).find(item => item.profile_id === profileId)?.label_zh}</strong><p>{list(library.profiles).find(item => item.profile_id === profileId)?.description_zh}</p><small>{library.selection_scope_zh}</small></div>
      </div>}
      <SkillLibraryManager
        profileId={profileId}
        presentations={manifests.map(skillPresentation).filter(item => item.skill_id)}
        focusSkillId={focusSkillId}
        onLibraryChanged={refresh}
        onOpenWorkspace={target => setPage(target === 'flows' ? 'application-flow' : target === 'reasoning' ? 'opportunity-reasoning' : target === 'sources' ? 'source-repositories' : target === 'knowledge' ? 'optimization-knowledge' : 'skills')}
      />
    </section>
  </div>;
}

export function DesktopSourceRepositoriesPage() {
  const catalog = usePlatformCatalog();
  const [path, setPath] = useState('');
  const [name, setName] = useState('');
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState(null);
  const connectors = list(catalog.overview?.connectors);
  async function refreshCatalog() {
    try {
      await catalog.refresh();
      setNotice(null);
    } catch (error) {
      setNotice({tone: 'error', text: text(error?.message, String(error))});
    }
  }
  async function register() {
    if (!path.trim()) return;
    setBusy('register');
    try {
      const result = await api('/v1/source-connectors/local', {method: 'POST', body: JSON.stringify({source_root: path.trim(), display_name: name.trim()})});
      await catalog.refresh();
      catalog.setConnectorId(result?.connector?.connector_id || '');
      window.dispatchEvent(new Event(catalogUpdatedEvent));
      setNotice({tone: 'success', text: '代码仓已登记并生成源码快照。'});
      setPath(''); setName('');
    } catch (error) { setNotice({tone: 'error', text: text(error?.message, String(error))}); }
    finally { setBusy(''); }
  }
  async function refreshConnector(connector) {
    setBusy(connector.connector_id);
    try {
      await api(`/v1/source-connectors/${encodeURIComponent(connector.connector_id)}/refresh`, {method: 'POST'});
      await catalog.refresh();
      window.dispatchEvent(new Event(catalogUpdatedEvent));
      setNotice({tone: 'success', text: `已刷新“${connector.display_name}”的源码快照。`});
    } catch (error) { setNotice({tone: 'error', text: text(error?.message, String(error))}); }
    finally { setBusy(''); }
  }
  return <div className="migrated-web-page workspace-scroll">
    <section className="work-section migrated-page-heading"><div className="section-heading"><div><span className="eyebrow">源码连接器</span><h2>代码仓</h2></div><button className="icon-button" title="刷新代码仓" onClick={() => void refreshCatalog()}><RefreshCw size={15}/></button></div><p>登记服务器上已有的 Git 工作树，后续应用流程、代码知识和机会分析都绑定到可追溯源码快照。</p></section>
    <ErrorNotice error={catalog.error} source="source-repositories-catalog"/><MigratedNotice notice={notice} onDismiss={() => setNotice(null)} source="source-repositories"/>
    <section className="work-section"><div className="section-heading"><div><span className="eyebrow">已登记</span><h2>源码工作树</h2></div><span className="migrated-count">{connectors.length} 个</span></div>
      <div className="migrated-connector-list">{connectors.map(connector => <article className={connector.connector_id === catalog.connectorId ? 'selected' : ''} key={connector.connector_id} onClick={() => catalog.setConnectorId(connector.connector_id)}>
        <GitBranch size={18}/><div><strong>{connector.display_name}</strong><span title={connector.source_root}>{connector.source_root}</span><small>{connector.snapshot?.snapshot_id || '尚无快照'} · {connector.snapshot?.clean ? '工作树干净' : '包含本地修改'}</small></div><button className="quiet-button" disabled={busy === connector.connector_id || connector.kind !== 'local'} onClick={event => {event.stopPropagation(); void refreshConnector(connector);}}>{busy === connector.connector_id ? <LoaderCircle className="spin" size={14}/> : <RefreshCw size={14}/>}刷新快照</button>
      </article>)}</div>
      {!catalog.loading && connectors.length === 0 && <div className="migrated-empty"><FolderGit2 size={28}/><strong>尚未登记代码仓</strong><span>在下方填写服务器上的 Git 工作树绝对路径。</span></div>}
    </section>
    <section className="work-section"><div className="section-heading"><div><span className="eyebrow">服务器路径</span><h2>登记代码仓</h2></div></div><div className="migrated-register-grid"><label><span>代码仓绝对路径</span><input value={path} onChange={event => setPath(event.target.value)} placeholder="/srv/source/openharmony"/></label><label><span>显示名称</span><input value={name} onChange={event => setName(event.target.value)} placeholder="例如 OpenHarmony 主仓"/></label><button className="primary-button" disabled={!path.trim() || Boolean(busy)} onClick={() => void register()}>{busy === 'register' ? <LoaderCircle className="spin" size={14}/> : <FolderGit2 size={14}/>}登记并生成快照</button></div></section>
  </div>;
}

function knowledgeTitle(item) {
  const value = record(item.record);
  return text(item.name, text(item.name_zh, text(item.symbol,
    text(value.name, text(value.name_zh, text(value.symbol, text(value.title, text(item.record_id, '知识记录'))))))));
}

export function DesktopOptimizationKnowledgePage() {
  const catalog = usePlatformCatalog();
  const [kind, setKind] = useState('optimization');
  const [query, setQuery] = useState('');
  const [result, setResult] = useState(null);
  const [selected, setSelected] = useState(null);
  const [customCatalog, setCustomCatalog] = useState(null);
  const [customKnowledgeBase, setCustomKnowledgeBase] = useState(null);
  const [customKbId, setCustomKbId] = useState('');
  const [sourcePath, setSourcePath] = useState('');
  const [unitName, setUnitName] = useState('');
  const [notice, setNotice] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  useEffect(() => {
    const connector = list(catalog.overview?.connectors).find(item => item.connector_id === catalog.connectorId);
    if (connector && !sourcePath) setSourcePath(text(connector.source_root));
  }, [catalog.connectorId, catalog.overview, sourcePath]);
  const loadCustomCatalog = useCallback(async (preferredKbId = '') => {
    const value = await api('/v1/custom-api-kbs', {cache: 'no-store'});
    setCustomCatalog(value);
    const candidates = list(value?.knowledge_bases);
    const requestedId = preferredKbId || customKbId;
    const nextId = candidates.some(item => item.kb_id === requestedId) ? requestedId : candidates[0]?.kb_id || '';
    if (nextId) {
      const detail = await api(`/v1/custom-api-kbs/${encodeURIComponent(nextId)}`, {cache: 'no-store'});
      setCustomKbId(nextId); setCustomKnowledgeBase(detail); setResult({records: list(detail?.records)});
    } else {
      setCustomKbId(''); setCustomKnowledgeBase(null); setResult({records: []});
    }
    setSelected(null);
    return value;
  }, [customKbId]);
  const search = useCallback(async (nextKind = kind, nextQuery = query) => {
    setBusy(true);
    try {
      let value;
      if (nextKind === 'npu') {
        const suffix = nextQuery.trim() ? `?query=${encodeURIComponent(nextQuery.trim())}` : '';
        value = await api(`/v1/hardware-kb/npu-api/records${suffix}`, {cache: 'no-store'});
      } else if (nextKind === 'gpu') {
        const suffix = nextQuery.trim() ? `?query=${encodeURIComponent(nextQuery.trim())}` : '';
        value = await api(`/v1/hardware-kb/accelerator-api/gpu/records${suffix}`, {cache: 'no-store'});
      } else if (nextKind === 'custom') {
        if (!customKbId) {
          await loadCustomCatalog(); setError(null); return;
        }
        else {
          const suffix = nextQuery.trim() ? `?query=${encodeURIComponent(nextQuery.trim())}` : '';
          const detail = await api(`/v1/custom-api-kbs/${encodeURIComponent(customKbId)}${suffix}`, {cache: 'no-store'});
          setCustomKnowledgeBase(detail); value = {records: list(detail?.records)};
        }
      } else {
        value = await api(`/v1/${nextKind}-kb/search`, {method: 'POST', body: JSON.stringify({query: nextQuery, include_unverified: nextKind === 'hardware', include_cases: false})});
      }
      setResult(value); setSelected(null); setError(null);
    } catch (nextError) { setError(nextError); }
    finally { setBusy(false); }
  }, [customKbId, kind, loadCustomCatalog, query]);
  useEffect(() => { void search('optimization', ''); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  async function open(item) {
    try {
      if (kind === 'npu') setSelected(await api(`/v1/hardware-kb/npu-api/records/${encodeURIComponent(item.record_id)}`));
      else if (kind === 'gpu') setSelected(await api(`/v1/hardware-kb/accelerator-api/gpu/records/${encodeURIComponent(item.record_id)}`));
      else if (kind === 'custom') setSelected(await api(`/v1/custom-api-kbs/${encodeURIComponent(customKbId)}/records/${encodeURIComponent(item.record_id)}`));
      else setSelected(await api(`/v1/${kind}-kb/records/${encodeURIComponent(item.record_type)}/${encodeURIComponent(item.record_id)}`));
    } catch (nextError) { setError(nextError); }
  }
  async function selectKind(nextKind) {
    setKind(nextKind); setSelected(null); setNotice(null);
    await search(nextKind, query);
  }
  async function selectCustomKnowledgeBase(kbId) {
    setBusy(true);
    try {
      const detail = await api(`/v1/custom-api-kbs/${encodeURIComponent(kbId)}`, {cache: 'no-store'});
      setCustomKbId(kbId); setCustomKnowledgeBase(detail); setResult({records: list(detail?.records)}); setSelected(null); setError(null);
    } catch (nextError) { setError(nextError); }
    finally { setBusy(false); }
  }
  async function buildCustomKnowledgeBase() {
    const requestedPath = sourcePath.trim(); const requestedUnit = unitName.trim();
    if (!requestedPath || !requestedUnit) return;
    setBusy(true); setNotice(null);
    try {
      let connector = list(catalog.overview?.connectors).find(item => text(item.source_root) === requestedPath);
      if (!connector) {
        const registered = await api('/v1/source-connectors/local', {method: 'POST', body: JSON.stringify({source_root: requestedPath, display_name: requestedPath.split(/[\\/]/).filter(Boolean).at(-1) || requestedUnit})});
        connector = registered?.connector;
        await catalog.refresh(); window.dispatchEvent(new Event(catalogUpdatedEvent));
      }
      const built = await api('/v1/custom-api-kbs/build', {method: 'POST', body: JSON.stringify({unit_name: requestedUnit, source_connector_id: connector?.connector_id || '', source_root: requestedPath})});
      const kbId = text(built?.knowledge_base?.kb_id);
      await loadCustomCatalog(kbId);
      const quality = text(built?.knowledge_base?.quality?.status, 'warning');
      const count = Number(built?.knowledge_base?.summary?.record_count || 0);
      setNotice({tone: quality === 'pass' ? 'success' : 'warning', text: `已按自建 API Skill 的合同扫描规则构建 ${requestedUnit} API 目录，共 ${count} 条候选；质量状态：${quality}。`});
      setUnitName(''); setError(null);
    } catch (nextError) { setError(nextError); }
    finally { setBusy(false); }
  }
  async function deleteCustomKnowledgeBase(kbId) {
    if (!window.confirm('删除这份自建 API 知识库？')) return;
    setBusy(true);
    try { await api(`/v1/custom-api-kbs/${encodeURIComponent(kbId)}`, {method: 'DELETE'}); await loadCustomCatalog(); setError(null); }
    catch (nextError) { setError(nextError); }
    finally { setBusy(false); }
  }
  const records = list(result?.records);
  const selectedRecord = selected?.record || selected;
  const customMetadata = customKnowledgeBase?.knowledge_base;
  return <div className="migrated-web-page workspace-scroll">
    <section className="work-section migrated-page-heading"><div className="section-heading"><div><span className="eyebrow">优化证据与接口合同</span><h2>优化知识库</h2></div></div><p>统一检索优化案例、硬件能力以及 NPU、GPU 和用户自建 API；API 记录描述接口合同，设备支持和收益仍需实测。</p></section>
    <section className="work-section"><div className="migrated-tabs migrated-knowledge-tabs"><button className={kind === 'optimization' ? 'selected' : ''} onClick={() => void selectKind('optimization')}><Database size={15}/>优化案例</button><button className={kind === 'hardware' ? 'selected' : ''} onClick={() => void selectKind('hardware')}><Sparkles size={15}/>硬件能力</button><button className={kind === 'npu' ? 'selected' : ''} onClick={() => void selectKind('npu')}><Cpu size={15}/>NPU API</button><button className={kind === 'gpu' ? 'selected' : ''} onClick={() => void selectKind('gpu')}><Network size={15}/>GPU API</button><button className={kind === 'custom' ? 'selected' : ''} onClick={() => void selectKind('custom')}><FolderGit2 size={15}/>自建 API</button></div>
      {kind === 'custom' && <><div className="migrated-api-builder"><label><span>服务器代码目录</span><input value={sourcePath} onChange={event => setSourcePath(event.target.value)} placeholder="/srv/source/vendor-accelerator"/></label><label><span>异构单元名称</span><input value={unitName} onChange={event => setUnitName(event.target.value)} placeholder="例如 NPU、AAE、ISP" maxLength={80}/></label><button className="primary-button" disabled={busy || !sourcePath.trim() || !unitName.trim()} onClick={() => void buildCustomKnowledgeBase()}>{busy ? <LoaderCircle className="spin" size={14}/> : <Sparkles size={14}/>}分析目录并构建 API 库</button></div><MigratedNotice notice={notice} onDismiss={() => setNotice(null)} source="optimization-knowledge"/><div className="migrated-custom-kb-strip">{list(customCatalog?.knowledge_bases).map(item => <div className={item.kb_id === customKbId ? 'selected' : ''} key={item.kb_id}><button onClick={() => void selectCustomKnowledgeBase(item.kb_id)}><strong>{item.display_name}</strong><small>{item.summary?.record_count || 0} 条 · {item.quality?.status || '待复核'}</small></button><button className="icon-button" title={`删除 ${item.display_name}`} onClick={() => void deleteCustomKnowledgeBase(item.kb_id)}><Trash2 size={13}/></button></div>)}{customCatalog && list(customCatalog.knowledge_bases).length === 0 && <span>尚无自建 API 目录</span>}</div>{customMetadata && <div className="migrated-api-summary"><strong>{customMetadata.display_name}</strong><span>{customMetadata.source?.source_root}</span><span>公开/契约 {customMetadata.summary?.public_contract_record_count || 0} · 算子 {customMetadata.summary?.operator_record_count || 0} · 内部实现 {customMetadata.summary?.internal_implementation_record_count || 0}</span></div>}</>}
      <form className="migrated-search" onSubmit={event => {event.preventDefault(); void search();}}><Search size={16}/><input value={query} onChange={event => setQuery(event.target.value)} placeholder="算法、代码结构、CPU fallback、硬件单元或接口"/><button className="primary-button" disabled={busy}>{busy ? <LoaderCircle className="spin" size={14}/> : <Search size={14}/>}检索</button></form>
      <ErrorNotice error={catalog.error || error} source="optimization-knowledge-catalog"/><div className="migrated-knowledge-grid"><div className="migrated-knowledge-list">{records.map(item => <button className={selectedRecord?.record_id === item.record_id ? 'selected' : ''} key={`${item.record_type}:${item.record_id}`} onClick={() => void open(item)}>{kind === 'npu' ? <Cpu size={16}/> : kind === 'gpu' ? <Network size={16}/> : kind === 'custom' ? <FolderGit2 size={16}/> : <Database size={16}/>}<span><strong>{knowledgeTitle(item)}</strong><small>{item.record_type} · {item.target_unit || item.accelerator_unit || item.hardware_id || item.exposure || item.confidence || '结构化记录'}</small></span></button>)}{!busy && records.length === 0 && <div className="migrated-empty"><Database size={26}/><span>没有命中记录</span></div>}</div><div className="migrated-knowledge-detail">{selected ? <><span className="eyebrow">{selectedRecord?.record_type || '知识记录'}</span><h3>{knowledgeTitle(selectedRecord)}</h3><pre>{JSON.stringify(selectedRecord, null, 2)}</pre></> : <div className="migrated-empty"><BookOpenCheck size={28}/><strong>选择记录查看接口合同与证据</strong></div>}</div></div>
    </section>
  </div>;
}

function maskedDeviceLabel(value) {
  const serial = String(value || '');
  return serial.length > 6 ? `设备 …${serial.slice(-6)}` : '本地 HDC 设备';
}

export function DesktopOpportunityReasoningPage({
  submitTask, resolveLocalDevice, listLocalDevices,
  onListCaptureTraceSources, onMaterializeCaptureTrace,
}) {
  const catalog = usePlatformCatalog();
  const [inputMode, setInputMode] = useState('flow');
  const [flowCatalog, setFlowCatalog] = useState(null);
  const [flowScenarioId, setFlowScenarioId] = useState('');
  const [flowScenarioDetailState, setFlowScenarioDetailState] = useState(null);
  const [flowStageId, setFlowStageId] = useState('');
  const [flowCatalogRevision, setFlowCatalogRevision] = useState(0);
  const [device, setDevice] = useState(null);
  const [devices, setDevices] = useState([]);
  const [selectedDeviceSerial, setSelectedDeviceSerial] = useState('');
  const [traces, setTraces] = useState([]);
  const [captureTraceSources, setCaptureTraceSources] = useState([]);
  const [independentTraceId, setIndependentTraceId] = useState('');
  const [traceDetail, setTraceDetail] = useState(null);
  const [hotspotId, setHotspotId] = useState('');
  const [sourceMatchState, setSourceMatchState] = useState(null);
  const [manualCode, setManualCode] = useState('');
  const [manualFunctionHint, setManualFunctionHint] = useState('');
  const [manualEventName, setManualEventName] = useState('');
  const [manualCodeState, setManualCodeState] = useState(null);
  const [routeResultState, setRouteResultState] = useState(null);
  const [busy, setBusy] = useState('');
  const [deviceBusy, setDeviceBusy] = useState(false);
  const [notice, setNotice] = useState(null);
  const deviceRequestRef = useRef({generation: 0, inputKey: ''});
  const connector = list(catalog.overview?.connectors).find(item => item.connector_id === catalog.connectorId);
  const flowScenarios = list(flowCatalog?.scenarios).filter(item => text(item.runtime_trace_analysis_artifact_id));
  const selectedFlowScenario = flowScenarios.find(item => item.artifact_id === flowScenarioId) || null;
  const flowScenarioDetail = flowScenarioDetailState?.scenarioId === flowScenarioId
    ? flowScenarioDetailState.value
    : null;
  const flowScenarioLoading = flowScenarioDetailState?.scenarioId === flowScenarioId
    && flowScenarioDetailState.loading;
  const traceId = inputMode === 'flow'
    ? text(selectedFlowScenario?.runtime_trace_analysis_artifact_id)
    : inputMode === 'trace' ? independentTraceId : '';
  const selectedTrace = traces.find(item => item.artifact_id === traceId);
  const hotspots = list(traceDetail?.hotspots);
  const scenarioStages = list(record(flowScenarioDetail?.scenario_json).stages).map(record);
  const stageAnalysisRows = list(record(flowScenarioDetail?.stage_analysis).stages).map(record);
  const flowStageOptions = scenarioStages.map((stage, index) => {
    const stageId = text(stage.id);
    const runtimeStageIds = runtimeStageIdsForFlowStage(stage, flowScenarioDetail?.runtime_trace_alignment);
    const stageHotspots = hotspots.filter(item => runtimeStageIds.includes(text(record(item?.stage_context).stage_id)));
    const analysis = stageAnalysisRows.find(item => text(item.id) === stageId) || {};
    return {
      stage,
      stageId,
      stageName: text(stage.name, stageId),
      stageOrder: Number(stage.order) || index + 1,
      runtimeStageIds,
      hotspotCount: stageHotspots.length,
      durationMs: analysis.duration_ms,
    };
  }).filter(item => item.stageId).sort((left, right) => left.stageOrder - right.stageOrder);
  const selectedFlowStage = flowStageOptions.find(item => item.stageId === flowStageId) || null;
  const visibleHotspots = inputMode === 'flow'
    ? flowStageId
      ? hotspots.filter(item => selectedFlowStage?.runtimeStageIds.includes(text(record(item?.stage_context).stage_id)))
      : []
    : hotspots;
  const selectedHotspot = visibleHotspots.find(item => item.hotspot_id === hotspotId);
  const connectorCatalogKey = list(catalog.overview?.connectors)
    .map(item => item.connector_id).sort().join('\u0000');
  const sourceSelectionKey = JSON.stringify([
    inputMode,
    flowScenarioId,
    flowStageId,
    traceId,
    hotspotId,
    catalog.connectorId,
    text(connector?.snapshot?.snapshot_id),
    text(connector?.snapshot?.content_sha256),
  ]);
  const sourceSelectionKeyRef = useRef(sourceSelectionKey);
  sourceSelectionKeyRef.current = sourceSelectionKey;
  const sourceMatch = sourceMatchState?.inputKey === sourceSelectionKey ? sourceMatchState.value : null;
  const manualInputKey = JSON.stringify([
    manualCode,
    manualFunctionHint,
    manualEventName,
    catalog.connectorId,
    text(connector?.snapshot?.snapshot_id),
  ]);
  const manualInputKeyRef = useRef(manualInputKey);
  manualInputKeyRef.current = manualInputKey;
  const manualCodeArtifact = manualCodeState?.inputKey === manualInputKey ? manualCodeState.value : null;
  const deviceProfileKey = JSON.stringify(record(device?.profile));
  const routeInputKey = JSON.stringify([
    inputMode,
    sourceSelectionKey,
    text(sourceMatch?.artifact?.artifact_id),
    text(manualCodeArtifact?.artifact?.artifact_id),
    deviceProfileKey,
  ]);
  const routeInputKeyRef = useRef(routeInputKey);
  routeInputKeyRef.current = routeInputKey;
  const routeResult = routeResultState?.inputKey === routeInputKey ? routeResultState.value : null;

  const clearDerivedEvidence = useCallback(() => {
    setSourceMatchState(null);
    setRouteResultState(null);
    setHotspotId('');
    setNotice(null);
  }, []);

  function chooseInputMode(nextMode) {
    if (nextMode === inputMode) return;
    clearDerivedEvidence();
    setTraceDetail(null);
    setFlowStageId('');
    setManualCodeState(null);
    setInputMode(nextMode);
  }

  const beginDeviceRequest = useCallback(inputKey => {
    const request = {
      generation: deviceRequestRef.current.generation + 1,
      inputKey,
    };
    deviceRequestRef.current = request;
    setDeviceBusy(true);
    setRouteResultState(null);
    return request;
  }, []);

  const continueDeviceRequest = useCallback((request, inputKey) => {
    if (deviceRequestRef.current !== request) return null;
    const continued = {generation: request.generation, inputKey};
    deviceRequestRef.current = continued;
    return continued;
  }, []);

  const finishDeviceRequest = useCallback(request => {
    if (deviceRequestRef.current === request) setDeviceBusy(false);
  }, []);

  const detectDevice = useCallback(async (requestedDeviceSerial = '', continuedRequest = null) => {
    const requestedSerial = text(requestedDeviceSerial);
    const request = continuedRequest || beginDeviceRequest(`resolve:${requestedSerial || 'auto'}`);
    if (typeof resolveLocalDevice !== 'function') {
      if (deviceRequestRef.current === request)
        setDevice({error: '当前工作台没有提供本地设备画像能力。'});
      finishDeviceRequest(request);
      return;
    }
    try {
      const value = await resolveLocalDevice(requestedSerial);
      if (deviceRequestRef.current !== request) return;
      setSelectedDeviceSerial(value?.deviceSerial || requestedSerial);
      setDevice(value); setNotice(null);
    } catch (error) {
      if (deviceRequestRef.current === request)
        setDevice({error: text(error?.message, String(error))});
    } finally { finishDeviceRequest(request); }
  }, [beginDeviceRequest, finishDeviceRequest, resolveLocalDevice]);

  const refreshDevice = useCallback(async () => {
    let request = beginDeviceRequest('discover');
    if (typeof listLocalDevices !== 'function') {
      request = continueDeviceRequest(request, 'resolve:auto');
      if (request) await detectDevice('', request);
      return;
    }
    try {
      const value = await listLocalDevices();
      if (deviceRequestRef.current !== request) return;
      const available = list(value?.devices);
      setDevices(available);
      const selected = text(value?.selectedDeviceSerial);
      setSelectedDeviceSerial(selected);
      if (selected) {
        request = continueDeviceRequest(request, `resolve:${selected}`);
        if (request) await detectDevice(selected, request);
      }
      else {
        setDevice({error: available.length > 1
          ? `发现 ${available.length} 台本地设备，请选择本轮分析设备。`
          : '没有发现可用的本地 HDC 设备。'});
      }
    } catch (error) {
      if (deviceRequestRef.current === request)
        setDevice({error: text(error?.message, String(error))});
    } finally { finishDeviceRequest(request); }
  }, [beginDeviceRequest, continueDeviceRequest, detectDevice, finishDeviceRequest, listLocalDevices]);

  const loadTraces = useCallback(async () => {
    setBusy(current => current || 'traces');
    try {
      const value = await api('/v1/desktop-opportunity/traces?limit=500', {cache: 'no-store'});
      const values = list(value?.traces);
      setTraces(values);
      setIndependentTraceId(current => values.some(item => item.artifact_id === current) ? current : values[0]?.artifact_id || '');
      setNotice(null);
      return values;
    } catch (error) { setNotice({tone: 'error', text: text(error?.message, String(error))}); return []; }
    finally { setBusy(current => current === 'traces' ? '' : current); }
  }, []);

  const loadApplicationFlows = useCallback(async () => {
    try {
      const value = await api('/v1/application-flows?limit=500', {cache: 'no-store'});
      const values = list(value?.scenarios).filter(item => text(item.runtime_trace_analysis_artifact_id));
      setFlowCatalog(value);
      setFlowScenarioId(current => values.some(item => item.artifact_id === current) ? current : values[0]?.artifact_id || '');
      setFlowCatalogRevision(current => current + 1);
      return values;
    } catch (error) {
      setNotice({tone: 'error', text: text(error?.message, String(error))});
      return [];
    }
  }, []);

  const loadCaptureTraces = useCallback(async () => {
    if (typeof onListCaptureTraceSources !== 'function') return [];
    const values = await onListCaptureTraceSources();
    setCaptureTraceSources(list(values));
    return values;
  }, [onListCaptureTraceSources]);

  async function selectTrace(value) {
    clearDerivedEvidence();
    if (!value.startsWith('capture:')) {
      setIndependentTraceId(value);
      return;
    }
    const source = captureTraceSources.find(item => `capture:${item.traceId}:${item.stepId}` === value);
    if (!source || typeof onMaterializeCaptureTrace !== 'function') return;
    if (!catalog.connectorId) {
      setNotice({tone: 'error', text: '请先登记并选择热点源码对应的代码仓。'});
      return;
    }
    setBusy('importing-trace');
    setNotice({tone: 'info', text: `正在同步“${source.stepTitle}”并提取真实 Trace 热点…`});
    try {
      const materialized = await onMaterializeCaptureTrace(source, catalog.connectorId);
      await loadTraces();
      setIndependentTraceId(materialized.artifact_id);
      setNotice({tone: 'success', text: `已将“${source.stepTitle}”纳入机会推理 Trace 目录。`});
    } catch (error) {
      setNotice({tone: 'error', text: text(error?.message, String(error))});
    } finally { setBusy(''); }
  }

  useEffect(() => {
    void Promise.allSettled([refreshDevice(), loadTraces(), loadCaptureTraces(), loadApplicationFlows()]);
  }, [refreshDevice, loadTraces, loadCaptureTraces, loadApplicationFlows]);

  useEffect(() => {
    if (inputMode !== 'flow' || !flowScenarioId) {
      setFlowScenarioDetailState(null);
      setFlowStageId('');
      return;
    }
    let active = true;
    setFlowScenarioDetailState({scenarioId: flowScenarioId, loading: true, value: null});
    setFlowStageId('');
    void api(`/v1/application-flows/scenarios/${encodeURIComponent(flowScenarioId)}`, {cache: 'no-store'})
      .then(value => {
        if (!active) return;
        setFlowScenarioDetailState({scenarioId: flowScenarioId, loading: false, value});
      })
      .catch(error => {
        if (!active) return;
        setFlowScenarioDetailState({scenarioId: flowScenarioId, loading: false, value: null});
        setNotice({tone: 'error', text: text(error?.message, String(error))});
      });
    return () => { active = false; };
  }, [flowCatalogRevision, flowScenarioId, inputMode]);

  useEffect(() => {
    setSourceMatchState(null); setRouteResultState(null); setHotspotId('');
    if (!traceId || inputMode === 'manual') { setTraceDetail(null); return; }
    let active = true;
    setTraceDetail(null); setHotspotId('');
    setBusy('trace-detail');
    void api(`/v1/desktop-opportunity/traces/${encodeURIComponent(traceId)}`, {cache: 'no-store'})
      .then(value => {
        if (!active) return;
        setTraceDetail(value);
        setNotice(null);
      })
      .catch(error => { if (active) setNotice({tone: 'error', text: text(error?.message, String(error))}); })
      .finally(() => { if (active) setBusy(current => current === 'trace-detail' ? '' : current); });
    return () => { active = false; };
  }, [flowScenarioId, independentTraceId, inputMode, traceId]);

  useEffect(() => {
    if (inputMode !== 'flow') return;
    if (!flowScenarioDetail || !traceDetail) {
      setFlowStageId('');
      return;
    }
    setFlowStageId(current => {
      if (flowStageOptions.some(item => item.stageId === current)) return current;
      return flowStageOptions.find(item => item.hotspotCount > 0)?.stageId
        || flowStageOptions[0]?.stageId
        || '';
    });
  }, [flowScenarioDetail, inputMode, traceDetail]);

  useEffect(() => {
    if (inputMode === 'manual') return;
    const values = inputMode === 'flow' ? visibleHotspots : hotspots;
    setHotspotId(current => values.some(item => item.hotspot_id === current)
      ? current
      : values[0]?.hotspot_id || '');
    setSourceMatchState(null);
    setRouteResultState(null);
  }, [flowScenarioDetail, flowStageId, inputMode, traceDetail]);

  useEffect(() => {
    const traceConnector = text(traceDetail?.trace?.source_connector_id);
    if (traceConnector && connectorCatalogKey.split('\u0000').includes(traceConnector))
      catalog.setConnectorId(traceConnector);
  }, [connectorCatalogKey, traceDetail?.trace?.source_connector_id, catalog.setConnectorId]);

  function updateManualInput(setter, value) {
    setter(value);
    setManualCodeState(null);
    setRouteResultState(null);
    setNotice(null);
  }

  async function saveManualCode() {
    if (!manualCode.trim()) return;
    const requestInputKey = manualInputKeyRef.current;
    setBusy('manual-code'); setManualCodeState(null); setRouteResultState(null);
    try {
      const value = await api('/v1/desktop-opportunity/manual-code', {method: 'POST', body: JSON.stringify({
        code: manualCode,
        function_hint: manualFunctionHint,
        event_name: manualEventName,
        source_connector_id: catalog.connectorId || null,
      })});
      if (manualInputKeyRef.current !== requestInputKey || inputMode !== 'manual') return;
      setManualCodeState({inputKey: requestInputKey, value});
      setNotice({tone: 'success', text: '热点代码已保存为不可变分析输入，可以准备模型路由证据。'});
    } catch (error) {
      if (manualInputKeyRef.current === requestInputKey)
        setNotice({tone: 'error', text: text(error?.message, String(error))});
    } finally { setBusy(''); }
  }

  async function matchSource() {
    if (inputMode === 'manual' || !traceId || !hotspotId || !catalog.connectorId) return;
    const requestInputKey = sourceSelectionKeyRef.current;
    setBusy('source'); setSourceMatchState(null); setRouteResultState(null);
    try {
      const value = await api('/v1/desktop-opportunity/source-match', {method: 'POST', body: JSON.stringify({
        analysis_artifact_id: traceId, hotspot_id: hotspotId, source_connector_id: catalog.connectorId,
      })});
      if (sourceSelectionKeyRef.current !== requestInputKey) return;
      setSourceMatchState({inputKey: requestInputKey, value});
      const selected = record(value?.selected_match);
      const bindingLevel = text(value?.source_binding?.match_level, 'unknown');
      const confirmed = selected.match_level === 'declared_source_location' && ['exact', 'compatible'].includes(bindingLevel);
      setNotice(selected.code
        ? confirmed
          ? {tone: 'success', text: '已核验 Trace 声明的源码位置与当前源码快照。'}
          : {tone: 'info', text: '已找到候选源码；符号搜索或源码版本关系尚未形成精确绑定，模型将保留该不确定性。'}
        : {tone: 'info', text: value?.quality?.reason || '当前热点尚未找到可核验源码。'});
    } catch (error) {
      if (sourceSelectionKeyRef.current === requestInputKey)
        setNotice({tone: 'error', text: text(error?.message, String(error))});
    }
    finally { setBusy(''); }
  }

  async function routeHardware() {
    const manualArtifactId = text(manualCodeArtifact?.artifact?.artifact_id);
    if (inputMode === 'manual' ? !manualArtifactId : (!traceId || !hotspotId || !sourceMatch)) return;
    const requestInputKey = routeInputKeyRef.current;
    setBusy('route'); setRouteResultState(null);
    try {
      const body = inputMode === 'manual'
        ? {manual_code_artifact_id: manualArtifactId, device_profile: record(device?.profile)}
        : {
            analysis_artifact_id: traceId,
            hotspot_id: hotspotId,
            source_match: sourceMatch?.artifact?.artifact_id
              ? {artifact: {artifact_id: sourceMatch.artifact.artifact_id}}
              : {},
            device_profile: record(device?.profile),
          };
      const value = await api('/v1/desktop-opportunity/hardware-route', {method: 'POST', body: JSON.stringify(body)});
      if (routeInputKeyRef.current !== requestInputKey) return;
      setRouteResultState({inputKey: requestInputKey, value});
      setNotice({tone: 'success', text: '模型路由证据已准备；平台没有按关键词预选硬件，下一步由右侧 Code Agent 完成路由与机会推理。'});
    } catch (error) {
      if (routeInputKeyRef.current === requestInputKey)
        setNotice({tone: 'error', text: text(error?.message, String(error))});
    }
    finally { setBusy(''); }
  }

  async function submit() {
    if (!routeResult) return;
    const sourceEvidence = inputMode === 'manual' ? manualCodeArtifact : sourceMatch;
    const payload = {
      method: inputMode === 'flow'
        ? '应用流程结果 → 阶段热点 → 热点源码 → 模型语义路由 → 机会推理'
        : inputMode === 'manual'
          ? '用户热点代码 → 模型语义路由 → 机会推理'
          : '真实 Trace → 热点提取 → 热点源码匹配 → 模型语义路由 → 机会推理',
      input_kind: inputMode,
      application_flow: inputMode === 'flow' && selectedFlowScenario ? {
        scenario_artifact_id: selectedFlowScenario.artifact_id,
        analysis_id: selectedFlowScenario.application_flow_analysis_id,
        flow_id: selectedFlowScenario.flow_id,
        flow_name: selectedFlowScenario.flow_name,
        scenario: selectedFlowScenario.scenario,
        revision: selectedFlowScenario.revision,
        runtime_trace_analysis_artifact_id: selectedFlowScenario.runtime_trace_analysis_artifact_id,
        stage_context: selectedFlowStage ? {
          scenario_stage_id: selectedFlowStage.stageId,
          scenario_stage_name: selectedFlowStage.stageName,
          scenario_stage_order: selectedFlowStage.stageOrder,
          scenario_stage_definition: selectedFlowStage.stage,
          trace_stage_ids: selectedFlowStage.runtimeStageIds,
          selected_hotspot_stage_context: selectedHotspot?.stage_context || null,
          stage_hotspot_count: selectedFlowStage.hotspotCount,
        } : null,
      } : null,
      target_device: device ? {
        transport: device.transport,
        device_count: device.deviceCount,
        device_serial_sha256: profile?.evidence?.device_serial_sha256 || null,
        profile,
      } : null,
      trace: selectedTrace || traceDetail?.trace || null,
      hotspot: selectedHotspot || (inputMode === 'manual' ? {
        event_name: manualEventName || manualFunctionHint || '用户粘贴热点代码',
        function_hint: manualFunctionHint || null,
      } : null),
      source_match: sourceEvidence ? {
        artifact_id: sourceEvidence?.artifact?.artifact_id,
        source_connector_id: sourceEvidence.source_connector_id,
        source_binding: sourceEvidence.source_binding,
        match_level: sourceEvidence?.selected_match?.match_level,
        quality: sourceEvidence.quality,
      } : null,
      model_routing_context: routeResult,
      source_connector: connector ? {connector_id: connector.connector_id, display_name: connector.display_name, source_root: connector.source_root, snapshot_id: connector.snapshot?.snapshot_id} : null,
    };
    try {
      await submitTask(`请按 heterogeneous-opportunity-orchestrator 方法执行模型语义路由和机会推理。平台没有按关键词打分或预选硬件。第一阶段只能读取 model_routing_context.model_input_context，依据其中可见代码、硬件中立画像和全部能力卡选择 0 至 2 个 unit_id，并引用对应 evidence_ids；不要用 trace_name、event_name、源码路径、API 目录或软件方向卡影响硬件单元路由。形成 route 后，第二阶段才读取 downstream_context、api_directories 与 software_opportunity_cards，只沿主路由目录核验具体 API，同时独立评估 CPU 并行、SIMD 与算法/数据流方向。请输出结构化的 route 与 opportunities，逐项写明源码依据、缺失证据、适用条件、风险和真机验证方案。不得把目录命中、路由候选或目标画像当成设备支持与性能收益。\n\n${JSON.stringify(payload, null, 2)}`);
      setNotice({tone: 'success', text: '模型路由与机会推理任务已发送到右侧 Code Agent。'});
    } catch (error) {
      setNotice({tone: 'error', text: text(error?.message, String(error))});
    }
  }

  const profile = record(device?.profile);
  const sourceEvidence = inputMode === 'manual' ? manualCodeArtifact : sourceMatch;
  const selectedSource = record(sourceEvidence?.selected_match);
  const sourceConfirmed = inputMode !== 'manual' && selectedSource.match_level === 'declared_source_location'
    && ['exact', 'compatible'].includes(text(sourceMatch?.source_binding?.match_level, 'unknown'));
  const capabilityCards = list(routeResult?.model_input_context?.hardware_capability_cards);
  const apiDirectories = list(routeResult?.api_directories);
  const traceKind = text(traceDetail?.trace?.input_kind || selectedTrace?.input_kind);
  const hotspotQuality = record(traceDetail?.hotspot_quality);
  const moduleEventGroups = list(traceDetail?.module_event_groups);
  const isRawHtrace = traceKind === 'raw_htrace';
  const hasSpanSelfTimeEstimate = isRawHtrace
    && hotspotQuality.source === 'converted_callstack_direct_child_subtraction'
    && hotspotQuality.status !== 'unavailable'
    && hotspots.some(item => item.metric_semantics === 'instrumentation_span_direct_child_self_time_estimate');
  const hotspotMetricLabel = hasSpanSelfTimeEstimate
    ? '插桩自耗时估计'
    : isRawHtrace ? 'Trace Span 时长' : '累计时长';
  const rawHtraceQualityLabel = hasSpanSelfTimeEstimate
    ? hotspotQuality.status === 'partial' ? '部分可用' : '可用'
    : '自耗时不可用';
  const interactionBusy = deviceBusy || Boolean(busy) || flowScenarioLoading;
  const manualCodeBytes = new TextEncoder().encode(manualCode).length;
  const missingSourceCode = !text(selectedSource.code).trim();
  const canPrepareRoute = inputMode === 'manual'
    ? Boolean(text(manualCodeArtifact?.artifact?.artifact_id))
    : Boolean(traceId && hotspotId && !missingSourceCode);
  const inputModeLabel = inputMode === 'flow' ? '应用流程结果' : inputMode === 'trace' ? '独立 Trace' : '粘贴热点代码';

  return <div className="migrated-web-page workspace-scroll">
    <section className="work-section migrated-page-heading"><div className="section-heading"><div><span className="eyebrow">真实运行证据驱动</span><h2>机会推理</h2></div><button className="icon-button" title="刷新设备、应用流程与 Trace" disabled={interactionBusy} onClick={() => void Promise.allSettled([refreshDevice(), loadTraces(), loadCaptureTraces(), loadApplicationFlows()])}><RefreshCw size={15}/></button></div><p>从应用流程阶段结果、独立真实 Trace 或用户提供的热点代码进入同一套源码证据、硬件语义路由与机会推理链路。</p></section>
    <ErrorNotice error={catalog.error} source="opportunity-reasoning-catalog"/><MigratedNotice notice={notice} onDismiss={() => setNotice(null)} source="opportunity-reasoning"/>
    <div className="migrated-opportunity-pipeline">
      <section className="work-section migrated-opportunity-stage"><header><span>1</span><div><strong>目标设备与系统画像</strong><small>本地 HDC 自动发现并沿用负载采样页所选设备</small></div><MonitorSmartphone size={18}/></header>{devices.length > 1 && <label className="migrated-stage-select"><span>分析设备</span><select value={selectedDeviceSerial} disabled={interactionBusy} onChange={event => {const serial = event.target.value; setSelectedDeviceSerial(serial); void detectDevice(serial);}}><option value="">请选择本轮设备</option>{devices.map(item => {const serial = text(item?.serial || item?.deviceSerial || item?.device_serial); return <option key={serial} value={serial}>{maskedDeviceLabel(serial)}</option>;})}</select></label>}{deviceBusy ? <div className="migrated-stage-loading"><LoaderCircle className="spin" size={15}/>正在识别设备与操作系统…</div> : profile.model ? <div className="migrated-device-summary"><div><span>设备</span><strong>{profile.vendor} {profile.model}</strong></div><div><span>操作系统</span><strong>{profile.os_build || '版本未返回'}</strong></div><div><span>芯片</span><strong>{profile.chipset || '未识别'}</strong></div><div><span>连接</span><strong>{maskedDeviceLabel(device.deviceSerial)} · 本地 HDC</strong></div></div> : <div className="migrated-empty"><HardDrive size={24}/><span>{device?.error || '没有发现本地 HDC 设备；仍可分析输入，硬件路由将标记设备证据缺失。'}</span></div>}</section>

      <section className="work-section migrated-opportunity-stage"><header><span>2</span><div><strong>分析输入与热点</strong><small>{inputModeLabel}进入统一机会推理链路；Trace 输入执行真实 Trace 与热点提取</small></div>{inputMode === 'manual' ? <ClipboardPaste size={18}/> : <Activity size={18}/>}</header>
        <div className="migrated-opportunity-input-tabs" role="tablist" aria-label="机会推理输入类型">
          <button type="button" role="tab" aria-selected={inputMode === 'flow'} className={inputMode === 'flow' ? 'selected' : ''} disabled={interactionBusy} onClick={() => chooseInputMode('flow')}><Route size={14}/>应用流程结果</button>
          <button type="button" role="tab" aria-selected={inputMode === 'trace'} className={inputMode === 'trace' ? 'selected' : ''} disabled={interactionBusy} onClick={() => chooseInputMode('trace')}><Activity size={14}/>独立 Trace</button>
          <button type="button" role="tab" aria-selected={inputMode === 'manual'} className={inputMode === 'manual' ? 'selected' : ''} disabled={interactionBusy} onClick={() => chooseInputMode('manual')}><ClipboardPaste size={14}/>粘贴热点代码</button>
        </div>
        {inputMode === 'flow' && <>
          <label className="migrated-stage-select"><span>流程结果</span><select value={flowScenarioId} disabled={interactionBusy} onChange={event => {clearDerivedEvidence(); setFlowStageId(''); setFlowScenarioDetailState(null); setFlowScenarioId(event.target.value);}}><option value="">请选择已完成 Trace 校准的应用流程</option>{flowScenarios.map(item => <option value={item.artifact_id} key={item.artifact_id}>{item.flow_name || item.scenario || item.artifact_id} · v{item.revision || 1} · {item.stage_count || 0} 个阶段</option>)}</select></label>
          {selectedFlowScenario && <label className="migrated-stage-select migrated-flow-stage-select"><span>流程阶段</span><select value={flowStageId} disabled={interactionBusy || flowStageOptions.length === 0} onChange={event => {setFlowStageId(event.target.value); setHotspotId(''); setSourceMatchState(null); setRouteResultState(null); setNotice(null);}}><option value="">请选择流程阶段</option>{flowStageOptions.map(item => <option value={item.stageId} key={item.stageId}>{item.stageOrder}. {item.stageName} · {item.hotspotCount} 个热点{item.durationMs != null ? ` · ${item.durationMs} ms` : ''}</option>)}</select></label>}
          {flowScenarioLoading ? <div className="migrated-stage-loading"><LoaderCircle className="spin" size={15}/>正在读取流程阶段…</div> : selectedFlowScenario ? <div className="migrated-flow-binding"><Route size={16}/><div><strong>{selectedFlowScenario.flow_name || selectedFlowScenario.scenario || '应用流程'}{selectedFlowStage ? ` · ${selectedFlowStage.stageOrder}. ${selectedFlowStage.stageName}` : ''}</strong><small>场景 v{selectedFlowScenario.revision || 1} · 已绑定真实 Trace {selectedFlowScenario.runtime_trace_analysis_artifact_id}</small>{selectedFlowStage && <small>Trace 阶段 {selectedFlowStage.runtimeStageIds.join('、')} · 当前 {selectedFlowStage.hotspotCount} 个热点</small>}</div><span>{selectedFlowStage ? `${selectedFlowStage.hotspotCount} 热点` : selectedFlowScenario.has_stage_analysis ? '含阶段分析' : '已校准'}</span></div> : <div className="migrated-empty"><Route size={24}/><span>暂无带真实 Trace 校准结果的应用流程。请先在“应用流程”完成校准。</span></div>}
        </>}
        {inputMode === 'trace' && <label className="migrated-stage-select"><span>真实 Trace</span><select value={independentTraceId} disabled={interactionBusy} onChange={event => void selectTrace(event.target.value)}><option value="">请选择真实 Trace</option>{traces.length > 0 && <optgroup label="服务器真实 Trace">{traces.map(item => <option value={item.artifact_id} key={item.artifact_id}>{item.trace_name || item.artifact_id} · {item.input_kind === 'raw_htrace' ? '选择后提取 self-time 热点' : `${item.hotspot_count || 0} 个热点`}</option>)}</optgroup>}{captureTraceSources.length > 0 && <optgroup label="本机 Trace 管理">{captureTraceSources.map(item => <option key={`${item.traceId}:${item.stepId}`} value={`capture:${item.traceId}:${item.stepId}`} disabled={!item.ready}>{item.planTitle ? `[${item.planTitle}] ` : ''}{item.stepOrdinal ? `Step ${item.stepOrdinal}: ` : ''}{item.stepTitle}{item.ready ? ' · 选择后提取热点' : ` · ${item.reason || '不可分析'}`}</option>)}</optgroup>}</select></label>}
        {inputMode === 'manual' && <div className="migrated-manual-code-input"><div className="migrated-manual-code-fields"><label><span>函数名提示</span><input value={manualFunctionHint} onChange={event => updateManualInput(setManualFunctionHint, event.target.value)} placeholder="例如 Foo::Run" maxLength={240}/></label><label><span>关联事件名</span><input value={manualEventName} onChange={event => updateManualInput(setManualEventName, event.target.value)} placeholder="可选" maxLength={240}/></label></div><label><span>热点代码</span><textarea value={manualCode} onChange={event => updateManualInput(setManualCode, event.target.value)} placeholder="粘贴 C/C++ 热点函数或最小可分析代码片段" rows={10} maxLength={16 * 1024}/><small className={manualCodeBytes > 16 * 1024 ? 'limit-exceeded' : ''}>{manualCodeBytes.toLocaleString()} / 16,384 bytes · 用户输入不会被声明为 Trace 精确源码绑定</small></label><button className="primary-button migrated-primary-action" type="button" disabled={!manualCode.trim() || manualCodeBytes > 16 * 1024 || interactionBusy} onClick={() => void saveManualCode()}>{busy === 'manual-code' ? <LoaderCircle className="spin" size={15}/> : <ClipboardPaste size={15}/>}保存为热点代码输入</button></div>}
        {inputMode !== 'manual' && (busy === 'importing-trace' ? <div className="migrated-stage-loading"><LoaderCircle className="spin" size={15}/>正在同步采样归档并提取热点…</div> : busy === 'trace-detail' ? <div className="migrated-stage-loading"><LoaderCircle className="spin" size={15}/>正在读取热点目录…</div> : <>{isRawHtrace && <div className={`migrated-trace-quality ${hasSpanSelfTimeEstimate ? 'estimate' : 'degraded'}`}><div><strong>{hasSpanSelfTimeEstimate ? '插桩 Span 自耗时估计' : '已降级为 Trace Span 指标'} · {rawHtraceQualityLabel}</strong><small>{hasSpanSelfTimeEstimate ? '直接子区间扣减得到的插桩 Span 排他时间估计，不是采样 CPU self-time。' : '转换调用栈不可用；当前热点保留原 Trace Span 指标，不能解读为 CPU self-time。'}</small></div>{moduleEventGroups.length > 0 ? <div className="migrated-trace-module-summary" aria-label="Trace 模块分组摘要">{moduleEventGroups.slice(0, 8).map(item => <span key={item.module_key || item.module_name}><strong>{item.module_name || item.module_key || '未分类'}</strong><small>{item.returned_event_count || 0} 个热点 · {item.self_time_estimate_total_duration_ms ?? '—'} ms</small></span>)}</div> : <small className="migrated-trace-module-unavailable">模块分组不可用</small>}</div>}<div className="migrated-hotspot-list">{visibleHotspots.map(item => {const stage = record(item.stage_context); const stageLabel = text(stage.stage_name, text(stage.stage_id)); return <button className={hotspotId === item.hotspot_id ? 'selected' : ''} key={item.hotspot_id} disabled={interactionBusy} onClick={() => {setHotspotId(item.hotspot_id); setSourceMatchState(null); setRouteResultState(null);}}><span>{item.rank}</span><div><strong>{item.event_name}</strong>{stageLabel && <em className="migrated-hotspot-stage">阶段 {stage.stage_order ? `${stage.stage_order} · ` : ''}{stageLabel}{stage.duration_ms != null ? ` · ${stage.duration_ms} ms` : ''}</em>}<small>{item.module} · {hotspotMetricLabel} {item.total_duration_ms ?? item.max_duration_ms ?? '—'} ms · {item.call_count ?? '—'} 次</small></div><ChevronRight size={15}/></button>;})}{traceId && !interactionBusy && visibleHotspots.length === 0 && <div className="migrated-empty"><span>{inputMode === 'flow' && selectedFlowStage ? '当前流程阶段没有带 stage_context 的可用热点。' : '该输入没有可用热点目录。'}</span></div>}</div></>)}
      </section>

      <section className="work-section migrated-opportunity-stage"><header><span>3</span><div><strong>{inputMode === 'manual' ? '热点代码确认' : '热点代码匹配'}</strong><small>{inputMode === 'manual' ? '使用已保存的用户代码作为本轮代码证据' : '优先核验 Trace 声明位置，否则在选定源码快照中做有界符号搜索'}</small></div><FileCode2 size={18}/></header><label className="migrated-stage-select"><span>代码仓</span><select value={catalog.connectorId} disabled={interactionBusy} onChange={event => {catalog.setConnectorId(event.target.value); setSourceMatchState(null); setManualCodeState(null); setRouteResultState(null);}}>{list(catalog.overview?.connectors).map(item => <option value={item.connector_id} key={item.connector_id}>{item.display_name} · {item.source_root}</option>)}</select></label>{sourceEvidence && <div className={`migrated-source-match ${inputMode === 'manual' || sourceConfirmed ? 'available' : selectedSource.code ? 'candidate' : 'unavailable'}`}>{selectedSource.code ? <><strong>{inputMode === 'manual' ? manualFunctionHint || manualEventName || '用户粘贴热点代码' : `${selectedSource.path}:${selectedSource.line}`}</strong><small>{inputMode === 'manual' ? '用户输入 · 未绑定源码快照' : `${sourceConfirmed ? '已核验源码' : '候选源码'} · ${selectedSource.match_level} · ${sourceMatch?.source_binding?.match_level || 'unknown'} source binding`}</small><pre>{selectedSource.code}</pre></> : <span>{sourceEvidence?.quality?.reason}</span>}</div>}{inputMode !== 'manual' && <button className="primary-button migrated-primary-action" disabled={!traceId || !hotspotId || !catalog.connectorId || interactionBusy} onClick={() => void matchSource()}>{busy === 'source' ? <LoaderCircle className="spin" size={15}/> : <Search size={15}/>}匹配热点代码</button>}{inputMode === 'manual' && !manualCodeArtifact && <div className="migrated-empty compact"><span>请先在上一步保存热点代码。</span></div>}</section>

      <section className="work-section migrated-opportunity-stage"><header><span>4</span><div><strong>模型语义路由与机会推理</strong><small>平台准备中立画像与证据，右侧 Code Agent 选择单元并判断机会</small></div><Route size={18}/></header>{routeResult && <><div className="migrated-route-evidence-summary"><div><span>硬件中立画像</span><strong>{list(routeResult?.model_input_context?.candidate_scopes).length} 个候选计算范围</strong></div><div><span>能力卡</span><strong>{capabilityCards.length} 个单元</strong></div><div><span>API 目录</span><strong>{apiDirectories.length} 个目录 · {routeResult?.catalog_summary?.api_record_count || 0} 条记录</strong></div></div><div className="migrated-route-list">{capabilityCards.map(item => {const directory = apiDirectories.find(value => value.unit_id === item.unit_id); return <article key={item.unit_id}><Cpu size={16}/><div><strong>{item.label_zh || item.unit_id}</strong><small>{directory ? `${directory.record_count || 0} 条 API · ${list(directory.semantic_families).length} 个语义族` : '能力卡证据已就绪'}</small></div><span>待模型判断</span></article>;})}</div></>}<div className="migrated-opportunity-actions"><button className="primary-button" disabled={!canPrepareRoute || interactionBusy} onClick={() => void routeHardware()}>{busy === 'route' ? <LoaderCircle className="spin" size={15}/> : <Route size={15}/>}准备模型路由证据</button><button className="primary-button" disabled={!routeResult || interactionBusy} onClick={() => void submit()}><Send size={15}/>交给右侧 Code Agent 路由与推理</button></div>{routeResult && <small className="migrated-route-boundary">{routeResult.boundary}</small>}</section>
    </div>
  </div>;
}
