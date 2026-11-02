'use client'

import {
  Activity,
  Check,
  Clock3,
  Download,
  FileChartColumnIncreasing,
  FileUp,
  FlaskConical,
  Gauge,
  GitBranch,
  LoaderCircle,
  Play,
  Plus,
  RefreshCw,
  Trash2,
  X,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { agentAutonomousTaskStatusEventName } from '../lib/agent-command-bridge'
import { api, apiBase, formatBytes, uploadWithProgress } from '../lib/api'
import { randomUuid } from '../lib/random-id'
import type { Connector, JsonRecord } from '../lib/types'
import { MarkdownReport } from './MarkdownReport'
import { MermaidDiagram } from './MermaidDiagram'
import { useWorkbenchErrorNotification } from './WorkbenchErrorNotification'

export type ExperimentalFlowMode = 'hiperf' | 'trace'

type ExperimentalEvidence = JsonRecord & {
  artifact_id: string
  analysis_kind: ExperimentalFlowMode
  created_at: string
  file: {
    name: string
    size_bytes: number
    sha256: string
    media_type: string
    managed_path: string
  }
}

type ExperimentalResultSummary = JsonRecord & {
  artifact_id: string
  analysis_kind: ExperimentalFlowMode
  source_connector_id: string
  title: string
  flow_goal: string
  created_at: string
  evidence_artifact_id: string
  revision: number
  parent_result_artifact_id?: string | null
  source_scopes?: ExperimentalSourceScope[]
  summary_excerpt?: string
  has_mermaid?: boolean
}

type ExperimentalSourceScope = {
  source_connector_id: string
  source_snapshot_id: string
  subpath: string
  role?: 'primary' | 'related'
}

type RelatedSourceScope = {
  sourceConnectorId: string
  subpath: string
}

type ExperimentalResult = ExperimentalResultSummary & {
  mermaid: string
  summary_markdown: string
}

type EvidenceCatalog = {
  evidence: ExperimentalEvidence[]
  count: number
}

type ResultCatalog = {
  results: ExperimentalResultSummary[]
  count: number
}

export type ExperimentalFlowAssistantContext = {
  page_id: 'experimental_flow_playground'
  analysis_kind: ExperimentalFlowMode
  source_connector_id: string
  source_snapshot_id: string
  subpath: string
  source_scopes: ExperimentalSourceScope[]
  flow_goal: string
  evidence_artifact_id: string | null
  result_artifact_id: string | null
  result_revision: number | null
}

type ResultTab = 'diagram' | 'summary' | 'source'

function record(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {}
}

function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' && value.trim() ? value.trim() : fallback
}

function number(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function formatTime(value: string): string {
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function normalizeSubpath(value: string): string {
  return value.trim().replaceAll('\\', '/').replace(/^\.\//, '').replace(/\/$/, '')
}

const modeDefinition = {
  hiperf: {
    title: '函数采样流程实验',
    eyebrow: 'Playground · HiPerf',
    evidenceLabel: 'HiPerf 数据',
    uploadLabel: '导入 HiPerf',
    skillId: 'experimental-flow-hiperf-reconstruction',
    taskKind: 'experimental_flow_hiperf',
    accept: '.data,.perf,.report,.txt,.json,.csv,application/octet-stream,text/plain,application/json,text/csv',
    empty: '等待 HiPerf 流程图',
    Icon: Gauge,
  },
  trace: {
    title: '运行轨迹流程实验',
    eyebrow: 'Playground 2 · Trace',
    evidenceLabel: 'Trace 数据',
    uploadLabel: '导入 Trace',
    skillId: 'experimental-flow-trace-reconstruction',
    taskKind: 'experimental_flow_trace',
    accept: '.htrace,.trace,.sys,.txt,.json,application/octet-stream,text/plain,application/json',
    empty: '等待 Trace 流程图',
    Icon: Activity,
  },
} as const

export function ExperimentalFlowPlayground({
  mode,
  connectors,
  selectedConnectorId,
  onSelectConnector,
  onAssistantContextChange,
}: {
  mode: ExperimentalFlowMode
  connectors: Connector[]
  selectedConnectorId: string
  onSelectConnector: (connectorId: string) => void
  onAssistantContextChange: (context: ExperimentalFlowAssistantContext | null) => void
}) {
  const definition = modeDefinition[mode]
  const [subpath, setSubpath] = useState('')
  const [relatedSourceScopes, setRelatedSourceScopes] = useState<RelatedSourceScope[]>([])
  const [flowGoal, setFlowGoal] = useState('')
  const [evidence, setEvidence] = useState<ExperimentalEvidence[]>([])
  const [selectedEvidenceId, setSelectedEvidenceId] = useState('')
  const [results, setResults] = useState<ExperimentalResultSummary[]>([])
  const [selectedResultId, setSelectedResultId] = useState('')
  const [selectedResult, setSelectedResult] = useState<ExperimentalResult | null>(null)
  const [resultTab, setResultTab] = useState<ResultTab>('diagram')
  const [busy, setBusy] = useState<'upload' | 'analysis' | 'delete' | null>(null)
  const [uploadProgress, setUploadProgress] = useState<number | null>(null)
  const [notice, setNotice] = useState<{ tone: 'info' | 'success' | 'error'; text: string } | null>(null)
  const fileRef = useRef<HTMLInputElement | null>(null)
  const requestRef = useRef('')
  const activeConnector = connectors.find((item) => item.connector_id === selectedConnectorId) ?? null
  const selectedEvidence = evidence.find((item) => item.artifact_id === selectedEvidenceId) ?? null
  const storageKey = `heterogeneous-enable-experimental-flow-${mode}-${selectedConnectorId}`
  const sourceScopeViews = useMemo(() => {
    const rawScopes: RelatedSourceScope[] = [
      { sourceConnectorId: selectedConnectorId, subpath },
      ...relatedSourceScopes.filter((item) => item.sourceConnectorId !== selectedConnectorId),
    ].filter((item) => item.sourceConnectorId)
    return rawScopes.flatMap((item, index) => {
      const connector = connectors.find((candidate) => candidate.connector_id === item.sourceConnectorId)
      if (!connector) return []
      return [{
        connector,
        subpath: normalizeSubpath(item.subpath),
        role: (index === 0 ? 'primary' : 'related') as 'primary' | 'related',
      }]
    })
  }, [connectors, relatedSourceScopes, selectedConnectorId, subpath])
  const sourceScopeConnectorIds = useMemo(
    () => new Set(sourceScopeViews.map((item) => item.connector.connector_id)),
    [sourceScopeViews],
  )
  const sourceScopesPayload = useMemo<ExperimentalSourceScope[]>(() => sourceScopeViews.map((item) => ({
    source_connector_id: item.connector.connector_id,
    source_snapshot_id: item.connector.snapshot.snapshot_id,
    subpath: item.subpath,
    role: item.role,
  })), [sourceScopeViews])

  const assistantContext = useMemo<ExperimentalFlowAssistantContext>(() => ({
    page_id: 'experimental_flow_playground',
    analysis_kind: mode,
    source_connector_id: selectedConnectorId,
    source_snapshot_id: activeConnector?.snapshot.snapshot_id ?? '',
    subpath: normalizeSubpath(subpath),
    source_scopes: sourceScopesPayload,
    flow_goal: flowGoal.trim(),
    evidence_artifact_id: selectedEvidenceId || null,
    result_artifact_id: selectedResult?.artifact_id ?? null,
    result_revision: selectedResult ? number(selectedResult.revision, 1) : null,
  }), [activeConnector?.snapshot.snapshot_id, flowGoal, mode, selectedConnectorId, selectedEvidenceId, selectedResult, sourceScopesPayload, subpath])

  useEffect(() => {
    onAssistantContextChange(assistantContext)
  }, [assistantContext, onAssistantContextChange])

  useEffect(() => () => onAssistantContextChange(null), [onAssistantContextChange])

  const openResult = useCallback(async (artifactId: string) => {
    if (!artifactId) {
      setSelectedResultId('')
      setSelectedResult(null)
      return
    }
    const response = await api<{ result: ExperimentalResult }>(`/v1/experimental-flows/results/${encodeURIComponent(artifactId)}`)
    setSelectedResultId(artifactId)
    setSelectedResult(response.result)
    setFlowGoal(response.result.flow_goal)
    const storedScopes = Array.isArray(response.result.source_scopes)
      ? response.result.source_scopes.map(record)
      : []
    const primaryConnectorId = response.result.source_connector_id
    const primaryScope = storedScopes.find((item) => text(item.source_connector_id) === primaryConnectorId)
    if (primaryConnectorId && primaryConnectorId !== selectedConnectorId) onSelectConnector(primaryConnectorId)
    setSubpath(text(primaryScope?.subpath, text(record(response.result.source_scope).subpath)))
    setRelatedSourceScopes(storedScopes.flatMap((item) => {
      const connectorId = text(item.source_connector_id)
      if (!connectorId || connectorId === primaryConnectorId || !connectors.some((connector) => connector.connector_id === connectorId)) return []
      return [{ sourceConnectorId: connectorId, subpath: text(item.subpath) }]
    }))
    setSelectedEvidenceId(response.result.evidence_artifact_id)
    setResultTab('diagram')
  }, [connectors, onSelectConnector, selectedConnectorId])

  const refreshCatalogs = useCallback(async (preferredResultId = '') => {
    if (!selectedConnectorId) {
      setEvidence([])
      setResults([])
      return
    }
    const connector = encodeURIComponent(selectedConnectorId)
    const [evidenceCatalog, resultCatalog] = await Promise.all([
      api<EvidenceCatalog>(`/v1/experimental-flows/evidence?analysis_kind=${mode}&source_connector_id=${connector}&limit=100`),
      api<ResultCatalog>(`/v1/experimental-flows/results?analysis_kind=${mode}&source_connector_id=${connector}&limit=100`),
    ])
    setEvidence(evidenceCatalog.evidence)
    setResults(resultCatalog.results)
    const nextEvidence = evidenceCatalog.evidence.some((item) => item.artifact_id === selectedEvidenceId)
      ? selectedEvidenceId
      : evidenceCatalog.evidence[0]?.artifact_id ?? ''
    setSelectedEvidenceId(nextEvidence)
    const nextResult = preferredResultId
      || (resultCatalog.results.some((item) => item.artifact_id === selectedResultId) ? selectedResultId : resultCatalog.results[0]?.artifact_id ?? '')
    if (nextResult) await openResult(nextResult)
    else {
      setSelectedResultId('')
      setSelectedResult(null)
    }
  }, [mode, openResult, selectedConnectorId, selectedEvidenceId, selectedResultId])

  useEffect(() => {
    try {
      const stored = record(JSON.parse(window.localStorage.getItem(storageKey) ?? '{}'))
      setSubpath(text(stored.subpath))
      setFlowGoal(text(stored.flow_goal))
      setRelatedSourceScopes(Array.isArray(stored.related_source_scopes) ? stored.related_source_scopes.map(record).flatMap((item) => {
        const connectorId = text(item.source_connector_id)
        if (!connectorId || connectorId === selectedConnectorId || !connectors.some((connector) => connector.connector_id === connectorId)) return []
        return [{ sourceConnectorId: connectorId, subpath: text(item.subpath) }]
      }) : [])
    } catch {
      setSubpath('')
      setFlowGoal('')
      setRelatedSourceScopes([])
    }
  }, [connectors, selectedConnectorId, storageKey])

  useEffect(() => {
    try {
      window.localStorage.setItem(storageKey, JSON.stringify({
        subpath: normalizeSubpath(subpath),
        flow_goal: flowGoal,
        related_source_scopes: relatedSourceScopes.map((item) => ({
          source_connector_id: item.sourceConnectorId,
          subpath: normalizeSubpath(item.subpath),
        })),
      }))
    } catch {
      // Server artifacts remain authoritative when browser storage is unavailable.
    }
  }, [flowGoal, relatedSourceScopes, storageKey, subpath])

  useEffect(() => {
    setRelatedSourceScopes((current) => current.filter((item) => item.sourceConnectorId !== selectedConnectorId))
  }, [selectedConnectorId])

  useEffect(() => {
    void refreshCatalogs().catch((error: unknown) => {
      setNotice({ tone: 'error', text: error instanceof Error ? error.message : 'Playground 记录读取失败' })
    })
  // Catalog selection changes are reconciled inside refreshCatalogs.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, selectedConnectorId])

  useEffect(() => {
    const listener = (event: Event) => {
      const detail = record((event as CustomEvent<unknown>).detail)
      const actionResult = record(detail.result)
      const result = record(actionResult.result)
      if (text(result.analysis_kind) !== mode) return
      const artifactId = text(result.artifact_id)
      requestRef.current = ''
      setBusy(null)
      setNotice({ tone: 'success', text: text(actionResult.summary_zh, '实验流程图已生成') })
      void refreshCatalogs(artifactId).catch((error: unknown) => {
        setNotice({ tone: 'error', text: error instanceof Error ? error.message : '结果已保存，但页面刷新失败' })
      })
    }
    window.addEventListener('experimental-flow-updated', listener)
    return () => window.removeEventListener('experimental-flow-updated', listener)
  }, [mode, refreshCatalogs])

  useEffect(() => {
    const listener = (event: Event) => {
      const detail = record((event as CustomEvent<unknown>).detail)
      if (text(detail.request_id) !== requestRef.current) return
      if (text(detail.status) === 'rejected') {
        requestRef.current = ''
        setBusy(null)
        setNotice({ tone: 'error', text: text(detail.message, '页面助手未接收本次实验任务') })
      } else {
        setNotice({ tone: 'info', text: '页面助手已接收任务，分析过程显示在右侧实时进度中。' })
      }
    }
    window.addEventListener(agentAutonomousTaskStatusEventName, listener)
    return () => window.removeEventListener(agentAutonomousTaskStatusEventName, listener)
  }, [])

  async function uploadEvidence(file: File) {
    if (!selectedConnectorId) {
      setNotice({ tone: 'error', text: '请先选择代码仓' })
      return
    }
    setBusy('upload')
    setUploadProgress(0)
    setNotice({ tone: 'info', text: `正在导入${definition.evidenceLabel}` })
    const form = new FormData()
    form.append('file', file)
    form.append('analysis_kind', mode)
    form.append('source_connector_id', selectedConnectorId)
    try {
      const response = await uploadWithProgress<{ evidence: ExperimentalEvidence }>(
        '/v1/experimental-flows/evidence',
        form,
        (progress) => setUploadProgress(progress.ratio),
      )
      setSelectedEvidenceId(response.evidence.artifact_id)
      await refreshCatalogs()
      setSelectedEvidenceId(response.evidence.artifact_id)
      setNotice({ tone: 'success', text: `${response.evidence.file.name} 已导入` })
    } catch (error) {
      setNotice({ tone: 'error', text: error instanceof Error ? error.message : '运行数据导入失败' })
    } finally {
      setBusy(null)
      setUploadProgress(null)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  function addRelatedSourceScope() {
    const next = connectors.find((connector) => !sourceScopeConnectorIds.has(connector.connector_id))
    if (!next) {
      setNotice({ tone: 'info', text: '没有更多可添加的已登记代码仓' })
      return
    }
    setRelatedSourceScopes((current) => [...current, { sourceConnectorId: next.connector_id, subpath: '' }])
  }

  function updateRelatedSourceScope(index: number, patch: Partial<RelatedSourceScope>) {
    setRelatedSourceScopes((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item))
  }

  function startAnalysis(continueFromCurrent = false) {
    if (!activeConnector || !selectedEvidence || !flowGoal.trim()) {
      setNotice({ tone: 'error', text: '请完整填写流程描述并选择运行数据' })
      return
    }
    const requestId = randomUuid()
    requestRef.current = requestId
    setBusy('analysis')
    const parentResultId = continueFromCurrent ? selectedResult?.artifact_id ?? '' : ''
    const evidenceName = selectedEvidence.file.name
    const runtimeDirection = mode === 'hiperf'
      ? '把 HiPerf 中可核对的函数 Self 百分比、样本数或可推导时间映射到图中；说明每项指标口径。'
      : '把 Trace 中可核对的实际事件、时序、并发和 wall/span 持续时间映射到图中；说明每项指标口径。'
    const message = [
      `执行 ${definition.title}。`,
      `用户流程：${flowGoal.trim()}`,
      `源码范围（${sourceScopesPayload.length} 个代码仓）：${JSON.stringify(sourceScopesPayload)}。`,
      `运行数据：evidence_artifact_id=${selectedEvidence.artifact_id}；文件=${evidenceName}。`,
      runtimeDirection,
      '自主理解代码与调用关系，生成尽量落实到真实函数的细粒度自由 Mermaid，并给出证据与缺口说明。完成后调用 experimental_flow_publish_result 回填页面。',
      parentResultId ? `本轮基于 result_artifact_id=${parentResultId} 继续修订，发布时作为 parent_result_artifact_id。` : '',
    ].filter(Boolean).join('\n')
    window.dispatchEvent(new CustomEvent('page-agent-autonomous-task', {
      detail: {
        request_id: requestId,
        task_kind: definition.taskKind,
        required_skill_id: definition.skillId,
        analysis_kind: mode,
        source_connector_id: activeConnector.connector_id,
        source_snapshot_id: activeConnector.snapshot.snapshot_id,
        subpath: normalizeSubpath(subpath),
        source_scopes: sourceScopesPayload,
        evidence_artifact_id: selectedEvidence.artifact_id,
        result_artifact_id: parentResultId,
        message,
        display_message: `${definition.title}：${flowGoal.trim().slice(0, 80)}`,
      },
    }))
  }

  async function deleteResult() {
    if (!selectedResult) return
    if (!window.confirm(`删除“${selectedResult.title}”第 ${selectedResult.revision} 版？`)) return
    setBusy('delete')
    try {
      await api(`/v1/experimental-flows/results/${encodeURIComponent(selectedResult.artifact_id)}`, { method: 'DELETE' })
      setSelectedResult(null)
      setSelectedResultId('')
      await refreshCatalogs()
      setNotice({ tone: 'success', text: '实验结果已删除' })
    } catch (error) {
      setNotice({ tone: 'error', text: error instanceof Error ? error.message : '实验结果删除失败' })
    } finally {
      setBusy(null)
    }
  }

  const DefinitionIcon = definition.Icon
  const errorForwarded = useWorkbenchErrorNotification(notice, () => setNotice(null), `experimental-flow-${mode}`)

  return <div className="workspace-scroll experimental-flow-playground">
    <section className="work-section experimental-flow-control">
      <div className="section-heading">
        <div><span className="eyebrow">{definition.eyebrow}</span><h2>{definition.title}</h2></div>
        <span className="experimental-badge"><FlaskConical size={14} />实验性</span>
      </div>

      {notice && !errorForwarded && <button className={`experimental-flow-notice ${notice.tone}`} type="button" onClick={() => setNotice(null)}><span>{notice.text}</span><X size={14} /></button>}

      <div className="experimental-flow-source-heading">
        <div><span className="eyebrow">源码范围</span><strong>{sourceScopeViews.length} 个代码仓</strong></div>
        <button className="quiet-button" type="button" disabled={Boolean(busy) || sourceScopeViews.length >= Math.min(connectors.length, 12)} onClick={addRelatedSourceScope}><Plus size={15} />添加代码仓</button>
      </div>
      <div className="experimental-flow-source-list">
        {sourceScopeViews.map((scopeView, scopeIndex) => <article className={`experimental-flow-source ${scopeView.role}`} key={scopeView.connector.connector_id}>
          <label><span>{scopeIndex === 0 ? '主代码仓' : `关联代码仓 ${scopeIndex}`}</span><select value={scopeView.connector.connector_id} onChange={(event) => scopeIndex === 0 ? onSelectConnector(event.target.value) : updateRelatedSourceScope(scopeIndex - 1, { sourceConnectorId: event.target.value })}>{connectors.map((connector) => <option key={connector.connector_id} value={connector.connector_id} disabled={sourceScopeConnectorIds.has(connector.connector_id) && connector.connector_id !== scopeView.connector.connector_id}>{connector.display_name} · {connector.source_root}</option>)}</select></label>
          <label><span>分析目录</span><input value={scopeView.subpath} onChange={(event) => scopeIndex === 0 ? setSubpath(event.target.value) : updateRelatedSourceScope(scopeIndex - 1, { subpath: event.target.value })} placeholder="代码仓内相对目录，可留空" /></label>
          {scopeIndex > 0 && <button className="icon-button danger" type="button" title="移除该关联代码仓" aria-label="移除该关联代码仓" disabled={Boolean(busy)} onClick={() => setRelatedSourceScopes((current) => current.filter((_, index) => index !== scopeIndex - 1))}><X size={14} /></button>}
          <small><GitBranch size={13} />{scopeView.connector.snapshot.snapshot_id}</small>
        </article>)}
      </div>

      <div className="experimental-flow-form">
        <label className="experimental-flow-goal"><span>用户流程</span><textarea rows={4} value={flowGoal} onChange={(event) => setFlowGoal(event.target.value)} placeholder="描述操作起点、动作、结束状态和关注范围" /></label>
        <label><span>{definition.evidenceLabel}</span><select value={selectedEvidenceId} onChange={(event) => setSelectedEvidenceId(event.target.value)}><option value="">选择已导入数据</option>{evidence.map((item) => <option key={item.artifact_id} value={item.artifact_id}>{item.file.name} · {formatBytes(item.file.size_bytes)}</option>)}</select></label>
        <div className="experimental-flow-actions">
          <input ref={fileRef} className="visually-hidden" type="file" accept={definition.accept} onChange={(event) => { const file = event.target.files?.[0]; if (file) void uploadEvidence(file) }} />
          <button className="quiet-button" type="button" disabled={Boolean(busy)} onClick={() => fileRef.current?.click()}>{busy === 'upload' ? <LoaderCircle size={15} className="spin" /> : <FileUp size={15} />}{busy === 'upload' && uploadProgress !== null ? `${Math.round(uploadProgress * 100)}%` : definition.uploadLabel}</button>
          <button className="primary-button" type="button" disabled={Boolean(busy) || !selectedEvidenceId || !flowGoal.trim()} onClick={() => startAnalysis(false)}>{busy === 'analysis' ? <LoaderCircle size={15} className="spin" /> : <Play size={15} />}{busy === 'analysis' ? '分析中' : '新建分析'}</button>
          {busy === 'analysis' && <button className="quiet-button" type="button" onClick={() => { requestRef.current = ''; setBusy(null); setNotice({ tone: 'info', text: '页面已解除等待；Agent Session 中的任务状态保持不变。' }) }}><X size={14} />解除等待</button>}
        </div>
      </div>
    </section>

    <section className="work-section experimental-flow-output">
      <div className="experimental-flow-history">
        <div className="experimental-flow-history-heading"><div><span className="eyebrow">分析记录</span><strong>{results.length} 条</strong></div><button className="icon-button" type="button" title="刷新实验记录" aria-label="刷新实验记录" onClick={() => void refreshCatalogs()}><RefreshCw size={15} /></button></div>
        <div className="experimental-flow-history-list">
          {results.map((item) => <button key={item.artifact_id} className={item.artifact_id === selectedResultId ? 'selected' : ''} type="button" onClick={() => void openResult(item.artifact_id)}><DefinitionIcon size={16} /><span><strong>{item.title}</strong><small>v{number(item.revision, 1)} · {formatTime(item.created_at)}</small></span>{item.artifact_id === selectedResultId && <Check size={14} />}</button>)}
          {results.length === 0 && <div className="experimental-flow-empty-list"><Clock3 size={17} /><span>尚无实验结果</span></div>}
        </div>
      </div>

      <div className="experimental-flow-result">
        {!selectedResult ? <div className="experimental-flow-empty"><FileChartColumnIncreasing size={26} /><strong>{definition.empty}</strong><span>{selectedEvidence ? selectedEvidence.file.name : `先导入${definition.evidenceLabel}`}</span></div> : <>
          <div className="experimental-flow-result-heading">
            <div><span className="eyebrow">v{number(selectedResult.revision, 1)} · {formatTime(selectedResult.created_at)}</span><h3>{selectedResult.title}</h3><p>{selectedResult.flow_goal}</p></div>
            <div><button className="quiet-button" type="button" disabled={Boolean(busy) || !selectedEvidenceId || !flowGoal.trim()} onClick={() => startAnalysis(true)}><RefreshCw size={14} />继续修改</button><a className="icon-button" href={`${apiBase}/v1/experimental-flows/results/${selectedResult.artifact_id}/mermaid`} title="下载 Mermaid" aria-label="下载 Mermaid"><Download size={15} /></a><button className="icon-button" type="button" title="删除当前结果" aria-label="删除当前结果" disabled={busy === 'delete'} onClick={() => void deleteResult()}>{busy === 'delete' ? <LoaderCircle size={15} className="spin" /> : <Trash2 size={15} />}</button></div>
          </div>
          <div className="experimental-flow-tabs" role="tablist" aria-label="实验流程结果">
            <button className={resultTab === 'diagram' ? 'selected' : ''} type="button" role="tab" aria-selected={resultTab === 'diagram'} onClick={() => setResultTab('diagram')}>流程图</button>
            <button className={resultTab === 'summary' ? 'selected' : ''} type="button" role="tab" aria-selected={resultTab === 'summary'} onClick={() => setResultTab('summary')}>分析说明</button>
            <button className={resultTab === 'source' ? 'selected' : ''} type="button" role="tab" aria-selected={resultTab === 'source'} onClick={() => setResultTab('source')}>Mermaid</button>
          </div>
          <div className="experimental-flow-result-body">
            {resultTab === 'diagram' && <MermaidDiagram source={selectedResult.mermaid} />}
            {resultTab === 'summary' && <MarkdownReport source={selectedResult.summary_markdown} />}
            {resultTab === 'source' && <pre className="experimental-flow-mermaid-source">{selectedResult.mermaid}</pre>}
          </div>
        </>}
      </div>
    </section>
  </div>
}
