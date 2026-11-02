'use client'

import {
  ArrowDownToLine,
  BookOpenCheck,
  Check,
  Circle,
  CircleAlert,
  Clock3,
  Database,
  GitBranch,
  History,
  LoaderCircle,
  MessageSquareText,
  PencilLine,
  RefreshCw,
  Square,
  Trash2,
  TriangleAlert,
  X,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { agentAutonomousTaskStatusEventName } from '../lib/agent-command-bridge'
import { api, apiBase, formatBytes } from '../lib/api'
import { randomUuid } from '../lib/random-id'
import type { Connector, JsonRecord } from '../lib/types'
import { MarkdownReport } from './MarkdownReport'
import { useWorkbenchErrorNotification } from './WorkbenchErrorNotification'

type CodeGraph = {
  graph_id: string
  status: 'estimating' | 'awaiting_confirmation' | 'queued' | 'running' | 'cancelling' | 'cancelled' | 'ready' | 'failed' | 'interrupted' | string
  source_connector_id: string
  source_snapshot_id: string
  scope: string
  created_at: string
  updated_at: string
  confirmed_at?: string
  started_at?: string
  finished_at?: string
  duration_ms?: number
  artifact_id?: string
  build_series_id?: string
  parent_graph_id?: string | null
  estimate?: JsonRecord
  progress?: JsonRecord
  summary?: JsonRecord
  error?: JsonRecord
  semantic_analysis?: JsonRecord
  knowledge_engine?: JsonRecord
}

type CodeGraphCatalog = {
  schema_version: string
  extractor_available: boolean
  graphs: CodeGraph[]
  count: number
}

type CodeKnowledgeReport = JsonRecord & {
  artifact_id: string
  graph_id: string
  title: string
  created_at: string
  revision: number
  markdown?: string
  question?: string
  parent_report_id?: string | null
}

type ReportCatalog = {
  reports: CodeKnowledgeReport[]
  count: number
}

export type CodeKnowledgeAssistantContext = {
  page_id: 'code_knowledge'
  source_connector_id: string
  source_snapshot_id: string
  subpath: string
  graph_id: string | null
  graph_status: string | null
  knowledge_engine_id: string | null
  knowledge_engine_version: string | null
  current_report_id: string | null
  current_report_revision: number | null
  report_count: number
}

function record(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {}
}

function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' && value.trim() ? value.trim() : fallback
}

function number(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function normalizeScope(value: string): string {
  return value.trim().replaceAll('\\', '/').replace(/^\.\//, '').replace(/\/$/, '')
}

function formatDuration(seconds: number): string {
  const bounded = Math.max(0, Math.round(seconds))
  if (bounded < 60) return `${bounded} 秒`
  const hours = Math.floor(bounded / 3600)
  const minutes = Math.ceil((bounded % 3600) / 60)
  return hours > 0 ? `${hours} 小时 ${minutes} 分钟` : `${minutes} 分钟`
}

function formatElapsedMilliseconds(milliseconds: number): string {
  const bounded = Math.max(0, milliseconds)
  if (bounded < 1000) return `${Math.max(1, Math.round(bounded))} 毫秒`
  if (bounded < 60_000) return `${(bounded / 1000).toFixed(bounded < 10_000 ? 1 : 0)} 秒`
  return formatDuration(bounded / 1000)
}

function formatTimestamp(value: string): string {
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function statusLabel(status: string): string {
  return ({
    estimating: '正在预估',
    awaiting_confirmation: '等待确认',
    queued: '等待执行',
    running: '正在构建',
    cancelling: '正在中止',
    cancelled: '已中止',
    ready: '底图已完成',
    failed: '分析失败',
    interrupted: '被服务重启中断',
  } as Record<string, string>)[status] ?? status
}

function phaseLabel(phase: string): string {
  return ({
    estimating: '统计源码规模与本机历史吞吐',
    awaiting_confirmation: '估时完成，等待用户确认',
    queued: '任务已入队',
    discovering: '发现 C / C++ 源码文件',
    parsing: '提取文件、符号、调用与 HiTrace 结构',
    resolving_edges: '解析调用与包含关系候选',
    cancelling: '正在安全停止并清理临时数据库',
    cancelled: '任务已由用户中止',
    completed: '静态代码知识底图构建完成',
    failed: '构建未完成',
  } as Record<string, string>)[phase] ?? phase
}

function remainingSeconds(graph: CodeGraph, now: number): number | null {
  const progress = record(graph.progress)
  const observed = number(progress.estimated_remaining_seconds, -1)
  const updatedAt = Date.parse(text(progress.eta_updated_at))
  if (observed >= 0) return Math.max(0, observed - (Number.isFinite(updatedAt) ? (now - updatedAt) / 1000 : 0))
  const estimate = record(graph.estimate)
  const initial = number(estimate.estimated_duration_seconds, -1)
  const startedAt = Date.parse(text(graph.started_at, text(graph.confirmed_at, graph.created_at)))
  if (initial >= 0 && graph.status !== 'awaiting_confirmation') {
    return Math.max(0, initial - (Number.isFinite(startedAt) ? (now - startedAt) / 1000 : 0))
  }
  return initial >= 0 ? initial : null
}

function elapsedMilliseconds(graph: CodeGraph, now: number): number | null {
  const recorded = number(graph.duration_ms, -1)
  if (recorded >= 0) return recorded
  const startedAt = Date.parse(text(graph.started_at, graph.confirmed_at ?? ''))
  if (!Number.isFinite(startedAt)) return null
  const terminal = ['ready', 'failed', 'cancelled', 'interrupted'].includes(graph.status)
  const finishedAt = terminal ? Date.parse(text(graph.finished_at, graph.updated_at)) : now
  return Number.isFinite(finishedAt) ? Math.max(0, finishedAt - startedAt) : null
}

function metricText(value: number, available: boolean): string {
  return available ? String(Math.max(0, value)) : '—'
}

const scopeStorageKey = 'heterogeneous-enable-code-knowledge-scope-v1'

export function CodeKnowledgeStudio({
  connectors,
  selectedConnectorId,
  onSelectConnector,
  onAssistantContextChange,
  artifactDownloadsEnabled = true,
}: {
  connectors: Connector[]
  selectedConnectorId: string
  onSelectConnector: (connectorId: string) => void
  onAssistantContextChange: (context: CodeKnowledgeAssistantContext | null) => void
  artifactDownloadsEnabled?: boolean
}) {
  const [catalog, setCatalog] = useState<CodeGraphCatalog | null>(null)
  const [selectedGraphId, setSelectedGraphId] = useState('')
  const [subpath, setSubpath] = useState('')
  const [maxFiles, setMaxFiles] = useState(50_000)
  const [reports, setReports] = useState<CodeKnowledgeReport[]>([])
  const [selectedReportId, setSelectedReportId] = useState('')
  const [selectedReport, setSelectedReport] = useState<CodeKnowledgeReport | null>(null)
  const [reportsLoadedForGraph, setReportsLoadedForGraph] = useState('')
  const [busy, setBusy] = useState<'estimate' | 'confirm' | 'cancel' | 'report' | null>(null)
  const [notice, setNotice] = useState<{ tone: 'info' | 'success' | 'error'; message: string } | null>(null)
  const [showEstimateConfirmation, setShowEstimateConfirmation] = useState(false)
  const [showCancelConfirmation, setShowCancelConfirmation] = useState(false)
  const [reportRevisionRequest, setReportRevisionRequest] = useState('')
  const [pendingDelete, setPendingDelete] = useState<{ kind: 'graph' | 'report'; id: string; label: string } | null>(null)
  const [now, setNow] = useState(() => Date.now())
  const automaticAnalysisRef = useRef(new Set<string>())
  const agentDispatchRef = useRef<string | null>(null)

  const activeConnector = connectors.find((item) => item.connector_id === selectedConnectorId) ?? null
  const connectorGraphs = useMemo(
    () => (catalog?.graphs ?? []).filter((item) => item.source_connector_id === selectedConnectorId),
    [catalog?.graphs, selectedConnectorId],
  )
  const activeGraph = connectorGraphs.find((item) => item.graph_id === selectedGraphId)
    ?? connectorGraphs.find((item) => item.source_snapshot_id === activeConnector?.snapshot.snapshot_id && normalizeScope(item.scope) === normalizeScope(subpath))
    ?? null
  const estimate = record(activeGraph?.estimate)
  const progress = record(activeGraph?.progress)
  const summary = record(activeGraph?.summary)
  const graphError = record(activeGraph?.error)
  const semanticAnalysis = record(activeGraph?.semantic_analysis)
  const knowledgeEngine = record(activeGraph?.knowledge_engine)
  const processed = number(progress.files_processed, number(progress.files_parsed) + number(progress.files_failed))
  const discovered = number(progress.files_discovered, number(estimate.source_file_count))
  const countdown = activeGraph ? remainingSeconds(activeGraph, now) : null
  const elapsed = activeGraph ? elapsedMilliseconds(activeGraph, now) : null
  const graphBusy = Boolean(activeGraph && ['estimating', 'queued', 'running', 'cancelling'].includes(activeGraph.status))
  const progressHasCounters = ['parsing', 'resolving_edges', 'completed', 'cancelled', 'failed'].includes(text(progress.phase))
  const terminalSummary = activeGraph?.status === 'ready'
  const parsedFiles = terminalSummary ? number(summary.files_parsed) : processed
  const totalFiles = terminalSummary ? number(summary.files_discovered) : discovered
  const symbolCount = terminalSummary ? number(summary.symbol_count) : number(progress.symbol_count)
  const callCandidateCount = terminalSummary ? number(summary.call_candidate_count) : number(progress.call_candidate_count)
  const tracepointCount = terminalSummary ? number(summary.tracepoint_count) : number(progress.tracepoint_count)
  const assistantContext = useMemo<CodeKnowledgeAssistantContext>(() => ({
    page_id: 'code_knowledge',
    source_connector_id: selectedConnectorId,
    source_snapshot_id: activeConnector?.snapshot.snapshot_id ?? '',
    subpath: normalizeScope(activeGraph?.scope ?? subpath),
    graph_id: activeGraph?.graph_id ?? null,
    graph_status: activeGraph?.status ?? null,
    knowledge_engine_id: text(knowledgeEngine.engine_id) || null,
    knowledge_engine_version: text(knowledgeEngine.graph_version) || null,
    current_report_id: selectedReport?.artifact_id ?? null,
    current_report_revision: selectedReport ? number(selectedReport.revision) : null,
    report_count: reports.length,
  }), [activeConnector?.snapshot.snapshot_id, activeGraph?.graph_id, activeGraph?.scope, activeGraph?.status, knowledgeEngine.engine_id, knowledgeEngine.graph_version, reports.length, selectedConnectorId, selectedReport, subpath])

  useEffect(() => {
    onAssistantContextChange(assistantContext)
  }, [assistantContext, onAssistantContextChange])

  useEffect(() => () => onAssistantContextChange(null), [onAssistantContextChange])

  const refreshCatalog = useCallback(async () => {
    const suffix = selectedConnectorId ? `?source_connector_id=${encodeURIComponent(selectedConnectorId)}&limit=100` : '?limit=100'
    const value = await api<CodeGraphCatalog>(`/v1/code-graphs${suffix}`)
    setCatalog(value)
    return value
  }, [selectedConnectorId])

  const openReport = useCallback(async (reportId: string) => {
    if (!reportId) {
      setSelectedReportId('')
      setSelectedReport(null)
      return
    }
    const value = await api<{ report: CodeKnowledgeReport }>(`/v1/code-knowledge-reports/${encodeURIComponent(reportId)}`)
    setSelectedReportId(reportId)
    setSelectedReport(value.report)
    setReportRevisionRequest('')
  }, [])

  const refreshReports = useCallback(async (graphId: string) => {
    if (!graphId) {
      setReports([])
      setReportsLoadedForGraph('')
      setSelectedReportId('')
      setSelectedReport(null)
      return []
    }
    const value = await api<ReportCatalog>(`/v1/code-knowledge-reports?graph_id=${encodeURIComponent(graphId)}&limit=100`)
    setReports(value.reports)
    setReportsLoadedForGraph(graphId)
    const target = value.reports.find((item) => item.artifact_id === selectedReportId)?.artifact_id ?? value.reports[0]?.artifact_id ?? ''
    if (target) await openReport(target)
    else {
      setSelectedReportId('')
      setSelectedReport(null)
    }
    return value.reports
  }, [openReport, selectedReportId])

  useEffect(() => {
    try {
      const stored = JSON.parse(window.localStorage.getItem(scopeStorageKey) ?? '{}') as Record<string, string>
      setSubpath(stored[selectedConnectorId] ?? '')
    } catch {
      setSubpath('')
    }
    setSelectedGraphId('')
    setSelectedReportId('')
    setSelectedReport(null)
  }, [selectedConnectorId])

  useEffect(() => {
    const normalized = normalizeScope(subpath)
    try {
      const stored = JSON.parse(window.localStorage.getItem(scopeStorageKey) ?? '{}') as Record<string, string>
      window.localStorage.setItem(scopeStorageKey, JSON.stringify({ ...stored, [selectedConnectorId]: normalized }))
    } catch {
      // Backend graph state remains authoritative when browser storage is unavailable.
    }
  }, [selectedConnectorId, subpath])

  useEffect(() => {
    void refreshCatalog().catch((error: unknown) => setNotice({ tone: 'error', message: error instanceof Error ? error.message : '无法读取代码知识任务' }))
  }, [refreshCatalog])

  useEffect(() => {
    if (!activeGraph) return
    setSelectedGraphId(activeGraph.graph_id)
    if (activeGraph.status === 'awaiting_confirmation') setShowEstimateConfirmation(true)
  }, [activeGraph])

  useEffect(() => {
    if (!activeGraph) return
    const shouldPoll = ['estimating', 'queued', 'running', 'cancelling'].includes(activeGraph.status)
      || busy === 'report'
    if (!shouldPoll) return
    const timer = window.setTimeout(() => {
      void Promise.all([
        refreshCatalog(),
        activeGraph.status === 'ready' ? refreshReports(activeGraph.graph_id) : Promise.resolve([]),
      ]).catch((error: unknown) => setNotice({ tone: 'error', message: error instanceof Error ? error.message : '代码知识任务状态刷新失败' }))
    }, 1500)
    return () => window.clearTimeout(timer)
  }, [activeGraph, busy, refreshCatalog, refreshReports])

  useEffect(() => {
    if (!graphBusy) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [graphBusy])

  useEffect(() => {
    if (!activeGraph || graphBusy) return
    setNow(Date.now())
  }, [activeGraph, graphBusy])

  useEffect(() => {
    if (!activeGraph || activeGraph.status !== 'ready') return
    void refreshReports(activeGraph.graph_id).catch((error: unknown) => setNotice({ tone: 'error', message: error instanceof Error ? error.message : '无法读取代码知识报告' }))
  }, [activeGraph, refreshReports])

  const dispatchAgentAnalysis = useCallback((graph: CodeGraph, options: { requestId?: string; parentReport?: CodeKnowledgeReport | null; revisionRequest?: string } = {}) => {
    const requestId = options.requestId || randomUuid()
    const parentReport = options.parentReport ?? null
    const revisionRequest = options.revisionRequest?.trim() ?? ''
    agentDispatchRef.current = requestId
    setBusy('report')
    window.dispatchEvent(new CustomEvent('page-agent-autonomous-task', {
      detail: {
        request_id: requestId,
        task_kind: 'code_knowledge_analysis',
        source_connector_id: graph.source_connector_id,
        source_snapshot_id: graph.source_snapshot_id,
        subpath: graph.scope,
        graph_id: graph.graph_id,
        current_report_id: parentReport?.artifact_id ?? '',
        message: parentReport
          ? `请加载 code-knowledge-analysis Skill，继续修改当前代码知识报告。graph_id=${graph.graph_id}；parent_report_id=${parentReport.artifact_id}；当前版本=v${number(parentReport.revision, 1)}；用户修改要求=${revisionRequest}。先读取父报告并按要求执行有界底图查询和必要源码复核，保留未受影响且证据仍成立的内容；完成后调用 code_graph_publish_report，明确传 parent_report_id=${parentReport.artifact_id} 和 question=${revisionRequest}，生成一个新版本并保留父报告。`
          : `请加载 code-knowledge-analysis Skill，为当前模块生成首份代码知识 Markdown 报告。graph_id=${graph.graph_id}；connector=${graph.source_connector_id}；snapshot=${graph.source_snapshot_id}；scope=${graph.scope || '.'}。使用 code_graph_query_knowledge 对平台静态代码底图做有界查询，并用原生源码工具复核模块职责、关键接口、控制流、数据流和调用关系。报告可包含 Mermaid，完成后调用 code_graph_publish_report。`,
        display_message: parentReport ? `继续修改代码知识报告 v${number(parentReport.revision, 1)}` : `分析代码模块：${graph.scope || '仓根目录'}`,
      },
    }))
  }, [])

  useEffect(() => {
    const listener = (event: Event) => {
      const detail = record((event as CustomEvent<unknown>).detail)
      const requestId = text(detail.request_id)
      if (!requestId || requestId !== agentDispatchRef.current) return
      if (text(detail.status) === 'rejected') {
        agentDispatchRef.current = null
        setBusy(null)
        setNotice({ tone: 'error', message: text(detail.message, '页面助手未接收本次代码知识分析任务。') })
        return
      }
      setNotice({ tone: 'info', message: '页面助手已接收任务；可在右侧实时分析进度查看检索、复核与报告生成过程。' })
    }
    window.addEventListener(agentAutonomousTaskStatusEventName, listener)
    return () => window.removeEventListener(agentAutonomousTaskStatusEventName, listener)
  }, [])

  useEffect(() => {
    if (!activeGraph || activeGraph.status !== 'ready' || reportsLoadedForGraph !== activeGraph.graph_id || reports.length > 0) return
    if (automaticAnalysisRef.current.has(activeGraph.graph_id)) return
    automaticAnalysisRef.current.add(activeGraph.graph_id)
    const requestId = randomUuid()
    void api<{ dispatch_required: boolean }>(`/v1/code-graphs/${encodeURIComponent(activeGraph.graph_id)}/semantic-analysis`, {
      method: 'POST',
      body: JSON.stringify({ request_id: requestId, force: false }),
    }).then((result) => {
      if (result.dispatch_required) dispatchAgentAnalysis(activeGraph, { requestId })
    }).catch((error: unknown) => {
      automaticAnalysisRef.current.delete(activeGraph.graph_id)
      setNotice({ tone: 'error', message: error instanceof Error ? error.message : '无法提交代码语义分析' })
    })
  }, [activeGraph, dispatchAgentAnalysis, reports.length, reportsLoadedForGraph])

  useEffect(() => {
    const listener = (event: Event) => {
      const detail = record((event as CustomEvent<unknown>).detail)
      const result = record(detail.result)
      const report = record(result.report)
      const reportId = text(report.artifact_id)
      const graphId = text(report.graph_id, activeGraph?.graph_id ?? '')
      agentDispatchRef.current = null
      setBusy(null)
      setNotice({ tone: 'success', message: text(result.summary_zh, '代码知识报告已生成并装载') })
      void Promise.all([refreshCatalog(), refreshReports(graphId)]).then(() => reportId ? openReport(reportId) : undefined)
    }
    window.addEventListener('code-knowledge-updated', listener)
    return () => window.removeEventListener('code-knowledge-updated', listener)
  }, [activeGraph?.graph_id, openReport, refreshCatalog, refreshReports])

  async function requestEstimate() {
    if (!activeConnector) {
      setNotice({ tone: 'error', message: '请先选择一个已登记代码仓' })
      return
    }
    setBusy('estimate')
    setNotice({ tone: 'info', message: '后端正在统计源码规模并校准预计分析时间' })
    try {
      const result = await api<{ graph: CodeGraph }>('/v1/code-graphs/estimates', {
        method: 'POST',
        body: JSON.stringify({ source_connector_id: activeConnector.connector_id, subpath: normalizeScope(subpath), max_files: maxFiles, force_new: true }),
      })
      setSelectedGraphId(result.graph.graph_id)
      await refreshCatalog()
      if (result.graph.status === 'ready') {
        setNotice({ tone: 'success', message: '当前源码快照已有可复用的代码知识底图' })
      }
    } catch (error) {
      setNotice({ tone: 'error', message: error instanceof Error ? error.message : '分析时间预估失败' })
    } finally {
      setBusy(null)
    }
  }

  async function confirmAnalysis() {
    if (!activeGraph) return
    setBusy('confirm')
    try {
      await api(`/v1/code-graphs/${encodeURIComponent(activeGraph.graph_id)}/confirm`, { method: 'POST' })
      setShowEstimateConfirmation(false)
      setNotice({ tone: 'info', message: '代码知识底图已进入后台构建；离开页面不会中断任务' })
      await refreshCatalog()
    } catch (error) {
      setNotice({ tone: 'error', message: error instanceof Error ? error.message : '代码分析启动失败' })
    } finally {
      setBusy(null)
    }
  }

  async function cancelAnalysis() {
    if (!activeGraph) return
    setBusy('cancel')
    try {
      await api(`/v1/code-graphs/${encodeURIComponent(activeGraph.graph_id)}/cancel`, { method: 'POST' })
      setShowCancelConfirmation(false)
      setShowEstimateConfirmation(false)
      setNotice({ tone: 'info', message: '停止请求已提交；后端会在安全边界清理未完成数据' })
      await refreshCatalog()
    } catch (error) {
      setNotice({ tone: 'error', message: error instanceof Error ? error.message : '停止代码分析失败' })
    } finally {
      setBusy(null)
    }
  }

  async function regenerateReport() {
    if (!activeGraph) return
    const requestId = randomUuid()
    setBusy('report')
    try {
      const result = await api<{ dispatch_required: boolean }>(`/v1/code-graphs/${encodeURIComponent(activeGraph.graph_id)}/semantic-analysis`, {
        method: 'POST',
        body: JSON.stringify({ request_id: requestId, force: true }),
      })
      if (result.dispatch_required) dispatchAgentAnalysis(activeGraph, { requestId })
    } catch (error) {
      setBusy(null)
      setNotice({ tone: 'error', message: error instanceof Error ? error.message : '无法重新提交语义分析' })
    }
  }

  async function continueReport() {
    if (!activeGraph || !selectedReport || !reportRevisionRequest.trim()) {
      setNotice({ tone: 'error', message: '请先选择一份代码知识报告，并填写修改要求' })
      return
    }
    const requestId = randomUuid()
    setBusy('report')
    try {
      const result = await api<{ dispatch_required: boolean }>(`/v1/code-graphs/${encodeURIComponent(activeGraph.graph_id)}/semantic-analysis`, {
        method: 'POST',
        body: JSON.stringify({ request_id: requestId, force: true }),
      })
      if (result.dispatch_required) dispatchAgentAnalysis(activeGraph, { requestId, parentReport: selectedReport, revisionRequest: reportRevisionRequest })
    } catch (error) {
      setBusy(null)
      setNotice({ tone: 'error', message: error instanceof Error ? error.message : '无法提交报告修改任务' })
    }
  }

  async function deleteSelectedAsset() {
    if (!pendingDelete) return
    setBusy('cancel')
    try {
      const deletedActiveGraph = pendingDelete.kind === 'graph' && activeGraph?.graph_id === pendingDelete.id
      if (pendingDelete.kind === 'graph') {
        await api(`/v1/code-graphs/${encodeURIComponent(pendingDelete.id)}`, { method: 'DELETE' })
        if (selectedGraphId === pendingDelete.id) {
          setSelectedGraphId('')
          setReports([])
          setSelectedReportId('')
          setSelectedReport(null)
        }
      } else {
        await api(`/v1/code-knowledge-reports/${encodeURIComponent(pendingDelete.id)}`, { method: 'DELETE' })
        if (selectedReportId === pendingDelete.id) {
          setSelectedReportId('')
          setSelectedReport(null)
        }
      }
      const kind = pendingDelete.kind
      setPendingDelete(null)
      const updated = await refreshCatalog()
      const graphId = kind === 'report' ? activeGraph?.graph_id ?? '' : ''
      if (graphId) await refreshReports(graphId)
      if (kind === 'graph' && deletedActiveGraph && updated.graphs[0]) {
        setSelectedGraphId(updated.graphs[0].graph_id)
        setSubpath(updated.graphs[0].scope)
      }
      setNotice({ tone: 'success', message: kind === 'graph' ? '代码知识构建及其报告已从列表删除' : '代码知识报告版本已从列表删除' })
    } catch (error) {
      setNotice({ tone: 'error', message: error instanceof Error ? error.message : '删除代码知识资产失败' })
    } finally {
      setBusy(null)
    }
  }

  const errorForwarded = useWorkbenchErrorNotification(notice, () => setNotice(null), 'code-knowledge')
  return <div className="workspace-scroll code-knowledge-scroll">
    <section className="work-section code-knowledge-controls">
      <div className="section-heading">
        <div><span className="eyebrow">代码仓 · 可恢复长任务</span><h2>代码知识库构建</h2></div>
        <div className="heading-actions"><button className="icon-button" type="button" title="刷新代码知识任务与报告" aria-label="刷新代码知识任务与报告" onClick={() => void Promise.all([refreshCatalog(), activeGraph ? refreshReports(activeGraph.graph_id) : Promise.resolve([])])}><RefreshCw size={15} /></button></div>
      </div>
      <div className="code-knowledge-source-controls">
        <label><span>代码仓</span><select value={selectedConnectorId} onChange={(event) => onSelectConnector(event.target.value)}>{connectors.map((connector) => <option key={connector.connector_id} value={connector.connector_id}>{connector.display_name} · {connector.source_root}</option>)}</select></label>
        <label><span>分析目录</span><input value={subpath} onChange={(event) => setSubpath(event.target.value)} placeholder="代码仓内相对目录；留空表示仓根目录" /></label>
        <label className="code-knowledge-max-files"><span>文件上限</span><input type="number" min={1} max={500000} step={1000} value={maxFiles} onChange={(event) => setMaxFiles(Math.max(1, Math.min(500000, Number(event.target.value) || 1)))} /></label>
        <button className="primary-button" type="button" disabled={!activeConnector || busy === 'estimate' || graphBusy} onClick={() => void requestEstimate()}>{busy === 'estimate' || activeGraph?.status === 'estimating' ? <LoaderCircle size={15} className="spin" /> : <Database size={15} />}新建分析</button>
      </div>
      {activeConnector && <div className="code-knowledge-source-fact"><GitBranch size={14} /><strong>{activeConnector.display_name}</strong><span>{activeConnector.snapshot.snapshot_id}</span><em>{activeConnector.snapshot.clean ? '干净快照' : '包含本地修改'}</em></div>}
      {notice && !errorForwarded && <button type="button" className={`application-flow-notice ${notice.tone}`} onClick={() => setNotice(null)}><span>{notice.message}</span><X size={14} /></button>}
    </section>

    <section className="work-section code-knowledge-history" aria-label="代码知识库构建记录">
      <div className="code-knowledge-history-heading"><div><History size={17} /><span><strong>知识库构建记录</strong><small>同一目录可以多次构建；每轮底图和报告独立保留</small></span></div><span>{connectorGraphs.length} 次构建</span></div>
      {connectorGraphs.length > 0 ? <div className="code-knowledge-run-list">{connectorGraphs.map((graph) => {
        const graphSummaryValue = record(graph.summary)
        const isActive = graph.graph_id === activeGraph?.graph_id
        const canDelete = !['estimating', 'queued', 'running', 'cancelling'].includes(graph.status)
        return <article className={isActive ? 'selected' : ''} key={graph.graph_id}>
          <button type="button" className="code-knowledge-run-open" onClick={() => { setSelectedGraphId(graph.graph_id); setSubpath(graph.scope); setNotice({ tone: 'info', message: graph.status === 'ready' ? '知识库构建已装载；选择报告后可在右侧页面助手继续修改' : `已装载${statusLabel(graph.status)}的构建记录` }) }}>
            <i className={`is-${graph.status}`} />
            <span><strong>{graph.scope || '仓根目录'}</strong><small>{formatTimestamp(graph.created_at)} · {statusLabel(graph.status)}{graph.parent_graph_id ? ' · 重建版本' : ''}</small></span>
            <span className="code-knowledge-run-metrics"><small>{number(graphSummaryValue.files_parsed)} 文件</small><small>{number(graphSummaryValue.symbol_count)} 符号</small><small>{number(graphSummaryValue.tracepoint_count)} HiTrace</small></span>
          </button>
          <button className="icon-button danger" type="button" title={canDelete ? '删除该构建及其报告' : '请先中止正在执行的构建'} aria-label="删除该构建及其报告" disabled={!canDelete || busy !== null} onClick={() => setPendingDelete({ kind: 'graph', id: graph.graph_id, label: graph.scope || '仓根目录' })}><Trash2 size={14} /></button>
        </article>
      })}</div> : <div className="code-knowledge-history-empty"><Database size={20} /><span>尚无构建记录。点击“新建分析”后会在这里保留每一轮结果。</span></div>}
    </section>

    {activeGraph && <section className={`work-section code-knowledge-progress is-${activeGraph.status}`} aria-live="polite">
      <div className="code-knowledge-progress-heading">
        <div>{graphBusy ? <LoaderCircle size={17} className="spin" /> : activeGraph.status === 'ready' ? <Check size={17} /> : activeGraph.status === 'failed' ? <CircleAlert size={17} /> : <Circle size={17} />}<span><strong>静态代码知识底图构建</strong><small>{statusLabel(activeGraph.status)} · {phaseLabel(text(progress.phase, activeGraph.status))}</small></span></div>
        <div className="code-knowledge-countdown"><Clock3 size={15} /><span>{graphBusy ? `${elapsed !== null ? `已用时 ${formatElapsedMilliseconds(elapsed)}` : '正在启动'}${countdown !== null ? ` · 预计剩余 ${formatDuration(countdown)}` : ''}` : elapsed !== null ? `实际用时 ${formatElapsedMilliseconds(elapsed)}` : activeGraph.status === 'awaiting_confirmation' && countdown !== null ? `预计用时 ${formatDuration(countdown)}` : '尚未开始'}</span></div>
      </div>
      <div className="code-knowledge-progress-metrics">
        <div><span>文件数量</span><strong>{parsedFiles} / {totalFiles || '—'}</strong></div>
        <div><span>符号</span><strong>{metricText(symbolCount, terminalSummary || progressHasCounters)}</strong></div>
        <div><span>调用候选</span><strong>{metricText(callCandidateCount, terminalSummary || progressHasCounters)}</strong></div>
        <div><span>HiTrace</span><strong>{metricText(tracepointCount, terminalSummary || progressHasCounters)}</strong></div>
        <div><span>当前文件</span><strong title={text(progress.current_path)}>{text(progress.current_path, activeGraph.status === 'ready' ? '已完成' : '等待返回')}</strong></div>
      </div>
      {text(graphError.message) && <div className="code-knowledge-error"><TriangleAlert size={15} /><span>{text(graphError.message)}</span></div>}
      {['estimating', 'queued', 'running'].includes(activeGraph.status) && <button className="quiet-button danger" type="button" disabled={busy === 'cancel'} onClick={() => setShowCancelConfirmation(true)}><Square size={13} />中止分析</button>}
    </section>}

    <div className="code-knowledge-workspace">
      <section className="code-knowledge-report-pane">
        <header><div><BookOpenCheck size={17} /><span><strong>代码知识报告</strong><small>{activeGraph?.status === 'ready' ? `${reports.length} 个版本 · Tree-sitter/SQLite 底图 + Agent 源码分析 · 由右侧页面助手继续追问` : '底图完成后由右侧页面助手生成'}</small></span></div>{artifactDownloadsEnabled && selectedReport && <a className="quiet-button" href={`${apiBase}/v1/code-knowledge-reports/${encodeURIComponent(selectedReport.artifact_id)}/markdown`}><ArrowDownToLine size={14} />Markdown</a>}</header>
        {reports.length > 0 && <div className="code-knowledge-report-versions">{reports.map((report) => <div className={selectedReportId === report.artifact_id ? 'selected' : ''} key={report.artifact_id}><button type="button" onClick={() => void openReport(report.artifact_id)}><strong>v{report.revision} · {report.title}</strong><small>{formatTimestamp(report.created_at)}{report.question ? ` · ${report.question}` : ''}</small></button><button className="icon-button danger" type="button" title="删除该报告版本" aria-label="删除该报告版本" disabled={busy !== null} onClick={() => setPendingDelete({ kind: 'report', id: report.artifact_id, label: `v${report.revision} · ${report.title}` })}><Trash2 size={13} /></button></div>)}</div>}
        <div className="code-knowledge-markdown">
          {selectedReport?.markdown ? <MarkdownReport source={selectedReport.markdown} /> : activeGraph?.status === 'ready' ? <div className="code-knowledge-empty"><MessageSquareText size={24} /><strong>{busy === 'report' ? '页面助手正在分析代码' : '尚无语义报告'}</strong><span>{busy === 'report' ? '可在右侧实时分析进度查看 Agent 的搜索与推理。' : semanticAnalysis.status === 'requested' ? '上一轮任务没有发布报告，可以重新提交分析。' : '静态底图已经完成，可以提交 Agent 语义分析。'}</span><button className="quiet-button" type="button" disabled={busy !== null} onClick={() => void regenerateReport()}>{busy === 'report' ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}生成报告</button></div> : <div className="code-knowledge-empty"><Database size={24} /><strong>等待代码知识底图</strong><span>先选择代码仓和目录，完成估时确认与后台构建。</span></div>}
        </div>
        {selectedReport && <div className="code-knowledge-report-revision">
          <div><PencilLine size={16} /><span><strong>继续修改当前报告</strong><small>基于 v{number(selectedReport.revision, 1)} 生成新版本，并保留当前报告。</small></span></div>
          <textarea rows={3} value={reportRevisionRequest} onChange={(event) => setReportRevisionRequest(event.target.value)} placeholder="例如：补充相机启动主调用链，并为每个阶段添加源码引用和 Mermaid 图。" />
          <button className="primary-button" type="button" disabled={busy !== null || !reportRevisionRequest.trim()} onClick={() => void continueReport()}>{busy === 'report' ? <LoaderCircle size={14} className="spin" /> : <MessageSquareText size={14} />}交给页面助手修改</button>
        </div>}
      </section>

    </div>

    {showEstimateConfirmation && activeGraph?.status === 'awaiting_confirmation' && <div className="code-knowledge-modal-backdrop" role="presentation"><section className="code-knowledge-modal" role="dialog" aria-modal="true" aria-labelledby="code-estimate-title">
      <header><div><Clock3 size={18} /><span><strong id="code-estimate-title">确认开始代码分析</strong><small>估时基于当前源码规模与本机历史吞吐，运行中会持续校准。</small></span></div><button className="icon-button" type="button" title="关闭" onClick={() => setShowEstimateConfirmation(false)}><X size={15} /></button></header>
      <dl><div><dt>预计时间</dt><dd>{formatDuration(number(estimate.estimated_duration_seconds))}<small>{text(estimate.confidence) === 'low' ? '首次构建，置信度较低' : `${text(estimate.confidence)} 置信度`}</small></dd></div><div><dt>源码规模</dt><dd>{number(estimate.source_file_count)} 个文件<small>{formatBytes(number(estimate.source_bytes))}</small></dd></div><div><dt>预计存储</dt><dd>{formatBytes(number(estimate.estimated_database_bytes))}<small>{formatBytes(number(estimate.filesystem_free_bytes))} 可用</small></dd></div><div><dt>分析范围</dt><dd>{activeGraph.scope || '仓根目录'}<small>最多 {number(estimate.max_files)} 个文件</small></dd></div></dl>
      {estimate.truncated_by_max_files === true && <p className="code-knowledge-modal-warning"><TriangleAlert size={15} />当前目录超过文件上限，报告只覆盖本轮已登记范围。</p>}
      <footer><button className="quiet-button" type="button" disabled={busy !== null} onClick={() => void cancelAnalysis()}>暂不分析</button><button className="primary-button" type="button" disabled={busy !== null || estimate.can_start !== true} onClick={() => void confirmAnalysis()}>{busy === 'confirm' ? <LoaderCircle size={15} className="spin" /> : <Check size={15} />}确认开始</button></footer>
    </section></div>}

    {showCancelConfirmation && activeGraph && <div className="code-knowledge-modal-backdrop" role="presentation"><section className="code-knowledge-modal compact" role="dialog" aria-modal="true" aria-labelledby="code-cancel-title">
      <header><div><TriangleAlert size={18} /><span><strong id="code-cancel-title">中止本次代码分析？</strong><small>已完成的历史报告不会删除；本轮临时数据库会在安全边界清理。</small></span></div></header>
      <footer><button className="quiet-button" type="button" onClick={() => setShowCancelConfirmation(false)}>继续分析</button><button className="primary-button danger" type="button" disabled={busy === 'cancel'} onClick={() => void cancelAnalysis()}>{busy === 'cancel' ? <LoaderCircle size={15} className="spin" /> : <Square size={13} />}确认中止</button></footer>
    </section></div>}

    {pendingDelete && <div className="code-knowledge-modal-backdrop" role="presentation"><section className="code-knowledge-modal compact" role="dialog" aria-modal="true" aria-labelledby="code-delete-title">
      <header><div><TriangleAlert size={18} /><span><strong id="code-delete-title">删除“{pendingDelete.label}”？</strong><small>{pendingDelete.kind === 'graph' ? '该构建下的报告会一并从列表删除，SQLite 查询索引会被清理；不可变审计血缘仍保留。' : '仅删除当前报告版本，不影响底图和其他报告。'}</small></span></div><button className="icon-button" type="button" title="关闭" onClick={() => setPendingDelete(null)}><X size={15} /></button></header>
      <footer><button className="quiet-button" type="button" onClick={() => setPendingDelete(null)}>取消</button><button className="primary-button danger" type="button" disabled={busy !== null} onClick={() => void deleteSelectedAsset()}>{busy === 'cancel' ? <LoaderCircle size={15} className="spin" /> : <Trash2 size={14} />}确认删除</button></footer>
    </section></div>}
  </div>
}
