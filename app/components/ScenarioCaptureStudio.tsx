'use client'

import { Activity, Archive, ArrowDownToLine, ArrowRight, Check, CircleAlert, ClipboardList, Code2, Cpu, FileJson2, LoaderCircle, Play, RefreshCw, Save, Smartphone, Sparkles, Square, X } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, apiBase, ApiError, apiErrorFromPayload } from '../lib/api'
import { agentAutonomousTaskStatusEventName } from '../lib/agent-command-bridge'
import { cancelAgentTaskSubmission } from '../lib/agent-task-submission'
import type { JsonRecord } from '../lib/types'
import { captureDeviceKey, captureDevices, captureFailure, dispatchParallelAgent, downloadText, errorText, isTerminal, jobPercent, post, record, records, statusLabel, text, timestamp, useParallelJob, type CaptureCatalog, type CaptureConnectionMode, type CaptureDevice, type CaptureFailure, type CapturePlan, type CapturePlanDraft, type CaptureRun, type CaptureSettings, type JobReceipt, type ModelingCatalog, type ModelingDataset, type Notice, type ParallelJob } from '../lib/parallel-modeling'
import { summarizePmuCapture, type PmuCaptureSummary } from '../lib/pmu-capture-summary'
import { captureAutoRepairKey, captureRepairEvidence, captureRepairIssue, captureRepairPrompt, type CaptureRepairTicket, type CaptureRepairView } from '../lib/scenario-capture-repair'
import { captureDeviceCommandEvent, captureDevicePreferenceKey, captureRoute, captureRouteKey, captureTarget, captureTargetKey, mergeCaptureDiscovery, type CaptureDeviceCommand, type CaptureDeviceSelectionView, type CaptureDeviceStatus, type CaptureRoute, type CaptureRouteOption, type CaptureTarget } from '../lib/scenario-capture-device'
import { focusWorkflowAction } from '../lib/workflow-focus'
import { CaptureDevicePicker } from './CaptureDevicePicker'
import { useWorkbenchErrorNotification } from './WorkbenchErrorNotification'
import '../parallel-modeling.css'

export type ScenarioCaptureAssistantContext = {
  page_id: 'scenario_capture'
  plan_id: string | null
  plan_draft: CapturePlanDraft | null
  selected_step_ids: string[]
  connection_mode: CaptureConnectionMode
  selected_device: CaptureDevice | null
  device_selection: CaptureDeviceSelectionView
  scenario_capture_target?: CaptureTarget
  bridge_id: string | null
  device_serial: string | null
  job_id: string | null
  run_id: string | null
}
const operationNames: Record<string, string> = { launch: '启动应用', click: '点击控件', date_mode: '日期视图', zoom: '缩放', albums: '进入相册', scroll: '滚动', home: '回桌面', key: '按键', wait: '等待', swipe: '滑动', unresolved: '待助手解析' }
const pmuGroups = [{ id: 'auto', label: '自动 · 尽可能采集全部可用事件' }, { id: 'cache', label: '通用缓存 · instructions / cycles / cache' }, { id: 'branch', label: '分支预测' }, { id: 'stall', label: '流水线停顿' }, { id: 'l1d', label: 'L1 数据缓存' }, { id: 'llc', label: '末级缓存' }, { id: 'dtlb', label: '数据 TLB' }]
const activeJobKey = 'heterogeneous-scenario-capture-active-job-v1'
type RepairAttempt = { jobId: string; plan: CapturePlan; device: CaptureDevice; used: boolean; stopped: boolean; started?: boolean; requestId?: string; ticket?: CaptureRepairTicket }
function clearStoredJob(id: string) {
  try { if (localStorage.getItem(activeJobKey) === id) localStorage.removeItem(activeJobKey) } catch { /* Storage is optional. */ }
}
const connectionModeNames: Record<CaptureConnectionMode, string> = { auto: '自动连接', local: '本机 HDC 直连', bridge: '远程采集端' }
function publicEndpointLabel(value: string, fallback = '远程采集端'): string {
  const label = (value || fallback).trim()
  if (/^windows hdc device bridge$/i.test(label) || /^windows device bridge$/i.test(label)) return 'Windows 采集端'
  if (/^windows bridge(?=\s*(?:·|$))/i.test(label)) return label.replace(/^windows bridge/i, 'Windows 采集端')
  if (/^device bridge$/i.test(label) || /^bridge$/i.test(label)) return '远程采集端'
  return label
}

function defaultDraft(catalog: CaptureCatalog): CapturePlanDraft {
  const template = catalog.templates.find((item) => item.template_id === catalog.default_template_id) ?? catalog.templates[0]
  return { template_id: template?.template_id ?? 'custom', title: template?.name ?? '负载采样', natural_language_steps: '', steps: template?.steps ?? [], profile: catalog.default_profile, capture: catalog.default_capture }
}
function draftFromPlan(plan: CapturePlan): CapturePlanDraft {
  return { template_id: plan.template_id, title: plan.title, natural_language_steps: plan.natural_language_steps, steps: plan.steps, profile: plan.profile, capture: plan.capture }
}
function collectorStatus(value: unknown): string { return typeof value === 'string' ? value : text(record(value).status, '未返回') }
function statusTone(status: string): string { return ['succeeded', 'completed', 'collected', 'ready'].includes(status) ? 'good' : ['partial', 'unavailable', 'needs_input'].includes(status) ? 'warn' : ['failed', 'invalid', 'exited_early'].includes(status) ? 'error' : 'active' }
function runConnectionName(run: CaptureRun): string { return connectionModeNames[run.connection_mode ?? (record(run.manifest).connection_mode === 'local' ? 'local' : 'bridge')] }
function CaptureFailureNotice({ failure }: { failure: CaptureFailure }) {
  return <div className="pm-note error" role="alert" style={{ marginTop: 10 }}><CircleAlert size={15} /><div><strong>失败位置：{failure.step}</strong><p style={{ marginTop: 4 }}>{failure.reason}</p><p style={{ marginTop: 4 }}>建议：{failure.suggestion}</p></div></div>
}
function CaptureFailureEvidence({ run }: { run: CaptureRun }) {
  const manifest = record(run.manifest)
  const files = records(manifest.files)
  return <>{records(manifest.steps).filter(step => Object.keys(record(step.screen_preparation_screenshot)).length > 0).map(step => {
    const diagnostic = record(step.screen_preparation_screenshot)
    const preparation = record(step.screen_preparation)
    const preparationTiming = record(step.screen_handoff_timing ?? step.gallery_preparation_timing ?? step.screen_preparation_timing)
    const actions = records(step.outside_capture_preparation)
    const stepId = text(step.step_id)
    const retained = diagnostic.status === 'retained' && files.some(file => file.kind === 'screen_preparation_screenshot' && file.step_id === stepId)
    const url = `${apiBase}/v1/scenario-capture/runs/${encodeURIComponent(run.run_id)}/steps/${encodeURIComponent(stepId)}/preparation-screenshot`
    const failedAction = actions.slice().reverse().find(action => action.status === 'failed' && text(action.reason))
    return <div className="pm-box" aria-label={`失败现场 · ${text(step.title, stepId)}`} key={stepId} style={{ marginTop: 12 }}>
      <h4>失败现场 · {text(step.title, stepId)}</h4>
      {retained ? <div className="pm-actions" style={{ marginTop: 0 }}><a className="quiet-button" href={url} target="_blank" rel="noopener noreferrer"><Smartphone size={14} />查看失败现场</a><a className="quiet-button" href={`${url}?download=true`}><ArrowDownToLine size={14} />下载截图</a></div> : <p>未能获取失败现场截图。</p>}
      {(actions.length > 0 || Object.keys(preparation).length > 0 || Object.keys(preparationTiming).length > 0 || !retained && text(diagnostic.reason)) && <details className="pm-details"><summary>查看设备操作诊断</summary>{text(preparationTiming.message) && <p>{text(preparationTiming.message)} · 已用 {String(preparationTiming.elapsed_seconds ?? '—')} 秒</p>}{failedAction && <p>操作错误：{text(failedAction.reason)}</p>}{!retained && text(diagnostic.reason) && <p>截图未返回：{text(diagnostic.reason)}</p>}<pre className="pm-code">{JSON.stringify({ initial_display_wakeup: step.initial_display_wakeup, screen_preparation: preparation, screen_preparation_timing: preparationTiming, screen_evidence_timing: step.screen_evidence_timing, screen_state_diagnostics: step.screen_state_diagnostics, capture_handoffs: step.capture_handoffs, screen_awake_guard: manifest.screen_awake_guard, actions }, null, 2)}</pre></details>}
    </div>
  })}</>
}
function PmuCaptureQuality({ title, summary }: { title: string; summary: PmuCaptureSummary }) {
  return <article className="pm-box pm-pmu-quality" aria-label={`${title} PMU采集质量`}>
    <h4>PMU 采集质量 · {title}</h4>
    <div className="pm-pmu-quality-grid">
      <div><div className="pm-inline"><strong>事件与计数完整性</strong><span className={`pm-badge ${summary.countTone}`}>{summary.countLabel}</span></div><p>{summary.countSummary}</p></div>
      <div><div className="pm-inline"><strong>实际采样质量</strong><span className={`pm-badge ${summary.timingTone}`}>{summary.timingLabel}</span></div><p>{summary.timingSummary}</p></div>
    </div>
    <p className="pm-hint pm-pmu-coverage">{summary.coverageSummary}</p>
    {summary.legacyNote && <p className="pm-pmu-legacy">{summary.legacyNote}</p>}
    <details className="pm-details"><summary>查看采样指标、原因与处理建议</summary>
      <dl className="pm-pmu-metrics">{summary.details.map(item => <div key={item.label}><dt>{item.label}</dt><dd>{item.value}</dd></div>)}</dl>
      {summary.groups.length > 0 && <div className="pm-table-scroll"><table className="pm-table" aria-label={`${title} PMU 事件组覆盖`}><thead><tr><th>事件组</th><th>状态</th><th>计数器运行覆盖</th><th>实际采集事件</th></tr></thead><tbody>{summary.groups.map(group => <tr key={group.id}><td>{group.id}</td><td><span className={`pm-badge ${group.tone}`}>{group.label}</span></td><td>{group.coverage}</td><td className="wrap">{group.events || '未取得事件'}{group.unavailable && <small>不支持 / 未打开：{group.unavailable}</small>}</td></tr>)}</tbody></table></div>}
      {summary.diagnostics.length > 0 && <div className="pm-pmu-diagnostics">{summary.diagnostics.map((diagnostic, index) => <div className={`pm-note ${diagnostic.severity === 'error' ? 'error' : diagnostic.severity === 'warning' ? 'warning' : ''}`} key={index}><CircleAlert size={14} /><div><p>{diagnostic.message}</p>{diagnostic.suggestion && <p>建议：{diagnostic.suggestion}</p>}</div></div>)}</div>}
      {!summary.diagnostics.length && <p className="pm-hint" style={{ marginTop: 10 }}>{summary.shortReason}</p>}
      {summary.rawReason && !summary.diagnostics.some(item => item.message === summary.rawReason) && <p className="pm-hint" style={{ marginTop: 8 }}>原始错误：{summary.rawReason}</p>}
      <p className="pm-hint" style={{ marginTop: 10 }}>零基线不是读取失败；采样时间覆盖完整不代表所有阶段均有可用差分。阶段分析还需结合实际间隔、计数器复用和阶段边界。</p>
    </details>
  </article>
}

function PmuSamplerPreparation({ preparation }: { preparation: JsonRecord }) {
  const status = text(preparation.status)
  if (!status || status === 'not_requested') return null
  const ready = ['existing', 'installed'].includes(status)
  const pending = ['checking', 'preparing', 'verifying_bundle', 'checking_managed', 'deploying', 'probing_capabilities'].includes(status)
  const remediation = record(preparation.remediation)
  const shellDiagnostics = [text(preparation.shell_diagnostics), text(record(preparation.previous_sampler).shell_diagnostics)].filter(Boolean).join('\n')
  return <article className="pm-box" aria-label="PMU 采样器准备" style={{ marginTop: 12 }}>
    <div className="pm-inline"><h4 style={{ margin: 0 }}>PMU 采样器准备</h4><span className={`pm-badge ${ready ? 'good' : pending ? 'active' : 'warn'}`}>{ready ? status === 'installed' ? '已自动安装' : '复用设备采样器' : status === 'existing_legacy_unverified' ? '旧版采样器' : pending ? '准备中' : '准备未完成'}</span></div>
    <p style={{ marginTop: 8 }}>{text(preparation.message, text(preparation.reason, status))}</p>
    {text(preparation.reason) && preparation.reason !== preparation.message && <p>{text(preparation.reason)}</p>}
    {text(remediation.next_action) && <p style={{ marginTop: 6 }}>处理建议：{text(remediation.next_action)}</p>}
    <details className="pm-details"><summary>查看采样器准备记录</summary>{shellDiagnostics && <><p>设备命令诊断</p><pre className="pm-code">{shellDiagnostics}</pre></>}{text(remediation.build_command) && <pre className="pm-code">{text(remediation.build_command)}</pre>}<pre className="pm-code">{JSON.stringify(preparation, null, 2)}</pre></details>
  </article>
}

export function ScenarioCaptureStudio({ onAssistantContextChange, onNavigateModeling, onNavigateTraceLibrary }: { onAssistantContextChange: (context: ScenarioCaptureAssistantContext | null) => void; onNavigateModeling?: (datasetId?: string) => void; onNavigateTraceLibrary?: () => void }) {
  const [catalog, setCatalog] = useState<CaptureCatalog | null>(null)
  const [plans, setPlans] = useState<CapturePlan[]>([])
  const [runs, setRuns] = useState<CaptureRun[]>([])
  const [draft, setDraft] = useState<CapturePlanDraft | null>(null)
  const [savedPlan, setSavedPlan] = useState<CapturePlan | null>(null)
  const [dirty, setDirty] = useState(false)
  const [profileJson, setProfileJson] = useState('')
  const applicationProfile = useMemo(() => {
    try { const value: unknown = JSON.parse(profileJson); return value && typeof value === 'object' && !Array.isArray(value) ? record(value) : null } catch { return null }
  }, [profileJson])
  const [devices, setDevices] = useState<CaptureDevice[]>([])
  const [selectedDevice, setSelectedDevice] = useState<CaptureDevice | null>(null)
  const [discoveryRoute, setDiscoveryRoute] = useState<CaptureRoute>({ connection_mode: 'auto', bridge_id: '' })
  const [registeredBridges, setRegisteredBridges] = useState<JsonRecord[]>([])
  const [discoveryDiagnostics, setDiscoveryDiagnostics] = useState<JsonRecord[]>([])
  const [deviceStates, setDeviceStates] = useState<Record<string, CaptureDeviceStatus>>({})
  const [discovering, setDiscovering] = useState(false)
  const [discoveryNote, setDiscoveryNote] = useState('正在自动识别已连接设备…')
  const [discoveryError, setDiscoveryError] = useState('')
  const [busy, setBusy] = useState('')
  const [loading, setLoading] = useState(true)
  const [notice, setNotice] = useState<Notice | null>(null)
  const [jobId, setJobId] = useState('')
  const [runId, setRunId] = useState('')
  const [terminalRun, setTerminalRun] = useState<CaptureRun | null>(null)
  const [script, setScript] = useState<{ filename: string; content: string; sha256?: string } | null>(null)
  const [localPackage, setLocalPackage] = useState<{ filename: string; deviceSerial: string } | null>(null)
  const [resultStepId, setResultStepId] = useState('')
  const [targetTid, setTargetTid] = useState('')
  const [modelingCatalog, setModelingCatalog] = useState<ModelingCatalog | null>(null)
  const [hardwareId, setHardwareId] = useState('')
  const [agentWatching, setAgentWatching] = useState(false)
  const [autoRepair, setAutoRepair] = useState(true)
  const [repair, setRepair] = useState<CaptureRepairView | null>(null)
  const autoRepairEnabled = useRef(true)
  const repairAttempt = useRef<RepairAttempt | null>(null)
  const followedRepairJob = useRef('')
  const handledJob = useRef('')
  const jobSelectionRevision = useRef(0)
  const discoveryRequest = useRef(0)
  const discoveryController = useRef<AbortController | null>(null)
  const discoveryPromise = useRef<Promise<void> | null>(null)
  const initialDiscoveryStarted = useRef(false)
  const mounted = useRef(false)
  const captureStarting = useRef(false)
  const activeCapture = useRef(false)
  const deviceList = useRef<CaptureDevice[]>([])
  const deviceStatusList = useRef<Record<string, CaptureDeviceStatus>>({})
  const deviceChoice = useRef<CaptureDevice | null>(null)
  const discoveryRouteChoice = useRef<CaptureRoute>({ connection_mode: 'auto', bridge_id: '' })
  const agentRequest = useRef('')
  const agentKnownPlans = useRef<Set<string>>(new Set())
  const agentDraftSnapshot = useRef('')
  const liveDraftSnapshot = useRef('')
  const initialPlanSelection = useRef(false)
  const captureAction = useRef<HTMLButtonElement>(null)
  const importAction = useRef<HTMLButtonElement>(null)
  liveDraftSnapshot.current = JSON.stringify({ draft, profileJson })
  const { job: polledJob, pollError } = useParallelJob(jobId)
  const job = polledJob?.job_id === jobId ? polledJob : null
  const jobActive = Boolean(jobId && (!job || !isTerminal(job.status)))
  const repairActive = Boolean(repair && ['creating', 'submitting', 'repairing', 'retrying'].includes(repair.status))
  const discoveryPaused = jobActive || busy === 'execute' || repairActive
  activeCapture.current = jobActive
  const catalogReady = Boolean(catalog)
  const pmuPeriodRange = catalog?.pmu_sampler?.period_us_range
  const pmuBundle = record(catalog?.pmu_sampler?.platform_bundle)
  const pmuModes = Array.isArray(catalog?.pmu_sampler?.supported_modes) ? catalog.pmu_sampler.supported_modes : null
  const availablePmuGroups = pmuGroups.filter(group => !pmuModes || pmuModes.includes(group.id) || group.id === draft?.capture.pmu.group)
  const automaticPmu = ['auto', 'all'].includes(draft?.capture.pmu.group ?? '')
  const pmuPeriodMin = Array.isArray(pmuPeriodRange) && Number.isInteger(pmuPeriodRange[0]) && pmuPeriodRange[0] > 0 ? Number(pmuPeriodRange[0]) : 10
  const pmuPeriodMax = Array.isArray(pmuPeriodRange) && Number.isInteger(pmuPeriodRange[1]) && pmuPeriodRange[1] >= pmuPeriodMin ? Number(pmuPeriodRange[1]) : Math.max(pmuPeriodMin, 10000)
  const connectionMode: CaptureConnectionMode = selectedDevice?.connection_mode ?? 'auto'
  const deviceSerial = selectedDevice?.device_serial ?? ''
  const bridgeId = selectedDevice?.bridge_id ?? ''
  const selectedDeviceKey = selectedDevice ? captureDeviceKey(selectedDevice) : ''
  const selectedDeviceState = selectedDeviceKey ? deviceStates[selectedDeviceKey] ?? 'unconfirmed' : null
  const connectionUnavailable = !selectedDevice || selectedDeviceState === 'offline'
  const activeRun = runs.find((item) => item.run_id === runId) ?? (terminalRun?.run_id === runId ? terminalRun : null)
  const manifest = record(activeRun?.manifest)
  const screenAwakeGuard = record(manifest.screen_awake_guard)
  const pmuPreparation = record(manifest.pmu_preparation ?? record(activeRun).pmu_preparation)
  const resultSteps = records(manifest.steps)
  const pmuSummaries = resultSteps.map((step, index) => ({ key: text(step.step_id, String(index)), title: text(step.title, text(step.step_id)), summary: summarizePmuCapture(step) }))
  const runFailure = activeRun?.status === 'failed' ? captureFailure(activeRun) : null
  const jobFailure = job?.status === 'failed' ? captureFailure(job.result ?? runs.find((item) => item.run_id === job.run_id), job.error) : null
  const selected = new Set(draft?.capture.selected_step_ids ?? [])
  const lastSelected = draft?.steps.reduce((last, step, index) => selected.has(step.step_id) ? index : last, -1) ?? -1
  const firstSelected = draft?.steps.findIndex((step) => selected.has(step.step_id)) ?? -1
  const executionStart = draft?.capture.preparation_mode === 'already_prepared' ? firstSelected : 0
  const unresolvedSteps = draft?.steps.filter((step, index) => index >= executionStart && index <= lastSelected && step.operation === 'unresolved') ?? []
  const percent = jobPercent(job)
  const refresh = useCallback(async () => {
    setLoading(true)
    const results = await Promise.allSettled([
      api<CaptureCatalog>('/v1/scenario-capture/catalog'),
      api<{ plans: CapturePlan[] }>('/v1/scenario-capture/plans'),
      api<{ runs: CaptureRun[] }>('/v1/scenario-capture/runs'),
      api<ModelingCatalog>('/v1/parallel-modeling/catalog'),
    ])
    const [catalogResult, plansResult, runsResult, modelResult] = results
    if (catalogResult.status === 'fulfilled') { const next = catalogResult.value; setCatalog(next); setRegisteredBridges(next.bridges ?? []); setDraft((current) => current ?? defaultDraft(next)); setProfileJson((current) => current || JSON.stringify(next.default_profile, null, 2)) }
    if (plansResult.status === 'fulfilled') {
      const availablePlans = plansResult.value.plans ?? []
      setPlans(availablePlans)
      // Establish a useful default once. Subsequent refreshes preserve the
      // plan or draft that the user explicitly chose, even if another window
      // creates a newer plan.
      if (!initialPlanSelection.current) {
        initialPlanSelection.current = true
        const plan = availablePlans[0]
        if (plan) {
          setSavedPlan(plan)
          setDraft(draftFromPlan(plan))
          setProfileJson(JSON.stringify(plan.profile, null, 2))
          setDirty(false)
        }
      }
    }
    if (runsResult.status === 'fulfilled') setRuns(runsResult.value.runs ?? [])
    if (modelResult.status === 'fulfilled') {
      setModelingCatalog(modelResult.value)
      setHardwareId((current) => current || (modelResult.value.hardware_profiles.length === 1 ? modelResult.value.hardware_profiles[0]?.id ?? '' : ''))
    }
    const failures = results.filter((item): item is PromiseRejectedResult => item.status === 'rejected')
    if (failures.length) setNotice({ tone: 'error', text: failures.map((item) => errorText(item.reason)).join('；') })
    setLoading(false)
  }, [])
  useEffect(() => { void refresh() }, [refresh])
  const stopRepair = useCallback(async (message = '已关闭自动修复，后续失败不会自动提交给助手。') => {
    const attempt = repairAttempt.current
    if (!attempt) return
    attempt.stopped = true
    if (attempt.requestId) cancelAgentTaskSubmission(attempt.requestId)
    if (!attempt.started) return
    if (!attempt.ticket) { if (mounted.current) setRepair({ status: 'repairing', message: '已停止向助手提交，正在等待修复请求返回后确认撤销。' }); return }
    if (['succeeded', 'partial', 'failed', 'cancelled', 'expired'].includes(attempt.ticket.status)) return
    try {
      const ticket = await post<CaptureRepairTicket>(`/v1/scenario-capture/repair-requests/${encodeURIComponent(attempt.ticket.repair_request_id)}/cancel`, {})
      attempt.ticket = ticket
      if (mounted.current && repairAttempt.current === attempt) setRepair({ ticket, status: ticket.retry_job_id ? 'retrying' : 'cancelled', message: ticket.retry_job_id ? '已关闭自动修复，正在等待本次重采停止。' : message })
    } catch (error) {
      if (mounted.current && repairAttempt.current === attempt) setRepair({ ticket: attempt.ticket, status: 'repairing', message: `撤销修复请求尚未确认，正在重试：${errorText(error)}` })
    }
  }, [])
  useEffect(() => {
    const applyPreference = () => {
      let enabled = true
      try { enabled = localStorage.getItem(captureAutoRepairKey) !== 'false' } catch { /* Storage is optional. */ }
      autoRepairEnabled.current = enabled
      setAutoRepair(enabled)
      if (!enabled) void stopRepair()
    }
    applyPreference()
    const listener = (event: StorageEvent) => { if (event.key === captureAutoRepairKey) applyPreference() }
    window.addEventListener('storage', listener)
    return () => window.removeEventListener('storage', listener)
  }, [stopRepair])
  const beginRepair = useCallback(async (failedJob: ParallelJob) => {
    const attempt = repairAttempt.current
    if (!attempt || attempt.jobId !== failedJob.job_id || attempt.used || attempt.stopped || !autoRepairEnabled.current || failedJob.status === 'cancelled') return
    // Claim once before any await. Restored or historical jobs have no attempt.
    attempt.used = true
    try {
      const run = text(failedJob.result?.run_id) ? failedJob.result as CaptureRun
        : failedJob.run_id ? await api<CaptureRun>(`/v1/scenario-capture/runs/${encodeURIComponent(failedJob.run_id)}`) : null
      if (!run || !captureRepairIssue(failedJob, run) || attempt.stopped || !autoRepairEnabled.current || repairAttempt.current !== attempt || !mounted.current) return
      const issue = captureRepairIssue(failedJob, run)!
      attempt.started = true
      setRepair({ status: 'creating', message: '正在关联本次失败记录，准备交给当前助手分析。' })
      const ticket = await post<CaptureRepairTicket>(`/v1/scenario-capture/runs/${encodeURIComponent(run.run_id)}/repair-request`, { job_id: failedJob.job_id })
      attempt.ticket = ticket
      if (attempt.stopped || !autoRepairEnabled.current || repairAttempt.current !== attempt || !mounted.current) {
        if (mounted.current && repairAttempt.current === attempt) setRepair({ ticket, status: 'repairing', message: '已停止向助手提交，正在确认修复请求撤销。' })
        else await post(`/v1/scenario-capture/repair-requests/${encodeURIComponent(ticket.repair_request_id)}/cancel`, {})
        return
      }
      if (ticket.status !== 'pending') { setRepair({ ticket, status: ticket.retry_job_id ? 'retrying' : 'cancelled', message: '本次失败已有修复记录，平台会保留关联结果。' }); return }
      const evidence = captureRepairEvidence(failedJob, run, attempt.plan, attempt.device, issue)
      attempt.requestId = dispatchParallelAgent('scenario_capture', { ...evidence, selected_device: attempt.device, scenario_capture_target: captureTarget(attempt.device), repair_request_id: ticket.repair_request_id, required_skill_id: '', display_message: '修复本次采集失败并重采一次' }, captureRepairPrompt(ticket, evidence))
      setRepair({ ticket, status: 'submitting', message: '正在向当前助手发送失败记录与修复请求。' })
    } catch (error) {
      if (mounted.current && repairAttempt.current === attempt) setRepair({ ticket: attempt.ticket, status: 'failed', message: `${attempt.stopped ? '未向助手发送修复请求，撤销结果暂时无法确认' : '自动修复未能开始'}：${errorText(error)}` })
    }
  }, [])
  useEffect(() => {
    let storedId = ''
    try { storedId = localStorage.getItem(activeJobKey) ?? '' } catch { /* Storage is optional. */ }
    if (!storedId) return
    const controller = new AbortController()
    const revision = jobSelectionRevision.current
    let retry: ReturnType<typeof setTimeout> | undefined
    const current = () => !controller.signal.aborted && revision === jobSelectionRevision.current
    const restore = async () => {
      if (!current()) return
      try {
        const previous = await api<ParallelJob>(`/v1/parallel-jobs/${encodeURIComponent(storedId)}`, { signal: controller.signal })
        if (!current()) return
        // A completed job belongs to history, not to a newly opened page.
        if (previous.job_id !== storedId || previous.kind !== 'scenario_capture' || !['queued', 'running'].includes(previous.status)) {
          clearStoredJob(storedId)
          return
        }
        try { if (localStorage.getItem(activeJobKey) !== storedId) return } catch { /* Continue without persistence. */ }
        setJobId((selectedId) => selectedId || storedId)
      } catch (error) {
        if (!current()) return
        if (error instanceof ApiError && error.statusCode === 404) clearStoredJob(storedId)
        else retry = setTimeout(() => void restore(), 5000)
      }
    }
    void restore()
    return () => { controller.abort(); if (retry) clearTimeout(retry) }
  }, [])
  useEffect(() => { if (jobId) { try { localStorage.setItem(activeJobKey, jobId) } catch { /* Continue without persistence. */ } } }, [jobId])
  useEffect(() => {
    if (!job || !isTerminal(job.status) || handledJob.current === job.job_id) return
    handledJob.current = job.job_id
    clearStoredJob(job.job_id)
    if (job.result && text(job.result.run_id)) {
      const result = job.result as CaptureRun
      setTerminalRun(result); setRunId(result.run_id)
      if (['succeeded', 'partial'].includes(job.status)) focusWorkflowAction(importAction)
    }
    if (job.status === 'failed') { const failure = captureFailure(job.result, job.error); setNotice({ tone: 'error', text: failure ? `${failure.step}：${failure.reason}` : '负载采样失败，可在下方采样结果中查看原因。' }) }
    if (job.status === 'cancelled') setNotice({ tone: 'info', text: '负载采样已取消；已返回的数据可在运行记录中查看。' })
    if (job.status !== 'cancelled') void beginRepair(job)
    void refresh()
  }, [job, refresh, beginRepair])
  const repairTicketId = repair?.ticket?.repair_request_id ?? ''
  useEffect(() => {
    if (!repairTicketId) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    const poll = async () => {
      const attempt = repairAttempt.current
      if (!attempt || attempt.ticket?.repair_request_id !== repairTicketId || controller.signal.aborted) return
      try {
        const ticket = attempt.stopped && ['pending', 'submitted'].includes(attempt.ticket.status)
          ? await post<CaptureRepairTicket>(`/v1/scenario-capture/repair-requests/${encodeURIComponent(repairTicketId)}/cancel`, {})
          : await api<CaptureRepairTicket>(`/v1/scenario-capture/repair-requests/${encodeURIComponent(repairTicketId)}`, { signal: controller.signal })
        if (controller.signal.aborted || repairAttempt.current !== attempt) return
        attempt.ticket = ticket
        if (ticket.retry_job_id && followedRepairJob.current !== ticket.retry_job_id) {
          followedRepairJob.current = ticket.retry_job_id
          jobSelectionRevision.current += 1
          setJobId(ticket.retry_job_id); setRunId(ticket.retry_run_id ?? ''); setTerminalRun(null)
          void refresh()
        }
        const retryStatus = ticket.retry_status ?? ticket.status
        if (ticket.retry_job_id) setRepair({ ticket, status: ['succeeded', 'partial', 'failed', 'cancelled'].includes(retryStatus) ? retryStatus as CaptureRepairView['status'] : 'retrying', message: ({ succeeded: '修复后的重采已完成。', partial: '修复后的重采已结束，仍有数据质量提示，请查看采样结果。', failed: '修复后的重采仍然失败，本次自动修复已结束。', cancelled: '本次重采已取消。' } as Record<string, string>)[retryStatus] ?? (attempt.stopped ? '已请求停止本次自动重采，正在等待任务结束。' : '助手已发起修复后的重采，正在跟踪新的采样任务。') })
        else if (['cancelled', 'expired', 'failed'].includes(ticket.status)) setRepair({ ticket, status: ticket.status === 'cancelled' ? 'cancelled' : 'failed', message: ticket.status === 'expired' ? '本次修复请求已过期，可检查助手结果后重新执行负载采样。' : ticket.status === 'cancelled' ? '本次自动修复已停止。' : '本次自动修复未完成，请查看助手分析。' })
        if (['pending', 'submitted'].includes(ticket.status) || ticket.retry_job_id && !isTerminal(retryStatus)) timer = setTimeout(() => void poll(), 1500)
      } catch (error) {
        if (controller.signal.aborted) return
        setRepair(current => current ? { ...current, message: `暂时无法确认修复进度，正在重连：${errorText(error)}` } : current)
        timer = setTimeout(() => void poll(), 3000)
      }
    }
    void poll()
    return () => { controller.abort(); if (timer) clearTimeout(timer) }
  }, [repairTicketId, refresh])
  useEffect(() => { setResultStepId(''); setTargetTid('') }, [runId])
  const chooseRoute = useCallback((route: CaptureRoute) => {
    const normalized = captureRoute(route)
    if (!normalized) return
    discoveryRouteChoice.current = normalized
    setDiscoveryRoute(current => captureRouteKey(current) === captureRouteKey(normalized) ? current : normalized)
  }, [])
  const persistDeviceChoice = useCallback((device: CaptureDevice | null, route: CaptureRoute) => {
    try { localStorage.setItem(captureDevicePreferenceKey, JSON.stringify({ route, selected_device: device })) } catch { /* Storage is optional. */ }
  }, [])
  const chooseDevice = useCallback((device: CaptureDevice | null) => {
    deviceChoice.current = device
    setSelectedDevice(device)
    if (device) chooseRoute({ connection_mode: device.connection_mode, bridge_id: device.bridge_id })
    persistDeviceChoice(device, discoveryRouteChoice.current)
  }, [chooseRoute, persistDeviceChoice])
  const selectDevice = useCallback((device: CaptureDevice | null) => {
    if (device && deviceChoice.current && captureDeviceKey(device) === captureDeviceKey(deviceChoice.current)) return
    // A probe already in flight belongs to the old route, not the new selection.
    discoveryRequest.current += 1
    discoveryController.current?.abort()
    discoveryController.current = null
    discoveryPromise.current = null
    setDiscovering(false)
    setDiscoveryError('')
    setDiscoveryDiagnostics([])
    chooseDevice(device)
    setLocalPackage(null)
  }, [chooseDevice])
  const selectRoute = useCallback((route: CaptureRoute) => {
    const normalized = captureRoute(route)
    if (!normalized) return
    discoveryRequest.current += 1
    discoveryController.current?.abort()
    discoveryController.current = null
    discoveryPromise.current = null
    setDiscovering(false)
    setDiscoveryError('')
    setDiscoveryDiagnostics([])
    chooseRoute(normalized)
    const current = deviceChoice.current
    if (current && (normalized.connection_mode === 'auto' || current.connection_mode !== normalized.connection_mode || current.bridge_id !== normalized.bridge_id)) chooseDevice(null)
    persistDeviceChoice(deviceChoice.current, normalized)
    setLocalPackage(null)
  }, [chooseDevice, chooseRoute, persistDeviceChoice])
  const refreshBridgeRegistry = useCallback(async () => {
    try {
      const result = await api<{ bridges: JsonRecord[] }>('/v1/device-bridges')
      if (mounted.current) setRegisteredBridges(result.bridges ?? [])
    } catch { /* Route discovery reports connectivity errors; keep known Bridge choices. */ }
  }, [])
  const discover = useCallback((allDevices = false): Promise<void> => {
    if (!mounted.current || !catalogReady || activeCapture.current || captureStarting.current) return Promise.resolve()
    if (discoveryPromise.current && !allDevices) return discoveryPromise.current
    if (allDevices) discoveryController.current?.abort()
    const requestId = ++discoveryRequest.current
    const route = discoveryRouteChoice.current
    const requested = allDevices ? null : captureTarget(deviceChoice.current) ?? (route.connection_mode === 'auto' ? null : route)
    const controller = new AbortController()
    discoveryController.current = controller
    setDiscovering(true)
    void refreshBridgeRegistry()
    const timeout = setTimeout(() => controller.abort(), 20000)
    const pending = (async () => {
      try {
        const result = await api<JsonRecord>('/v1/scenario-capture/devices/discover', { method: 'POST', body: JSON.stringify(requested ?? { connection_mode: 'auto' }), signal: controller.signal })
        if (!mounted.current || requestId !== discoveryRequest.current || activeCapture.current) return
        const incoming = captureDevices(result)
        const merged = mergeCaptureDiscovery(deviceList.current, deviceStatusList.current, incoming, result, requested)
        const next = merged.devices
        deviceList.current = next
        deviceStatusList.current = merged.states
        setDevices(next)
        setDeviceStates(merged.states)
        setDiscoveryDiagnostics(records(result.diagnostics))
        setDiscoveryError('')
        // Keep the same device and route even if it disconnects; never replace a previous selection silently.
        const current = deviceChoice.current
        const automatic = captureDevices({ devices: [result.selected_device] })[0]
        const resolved = current ? next.find((device) => captureDeviceKey(device) === captureDeviceKey(current)) ?? current : automatic ? next.find((device) => captureDeviceKey(device) === captureDeviceKey(automatic)) ?? null : null
        chooseDevice(resolved)
        const selectedStatus = resolved ? merged.states[captureDeviceKey(resolved)] : null
        setDiscoveryNote(selectedStatus === 'connected' ? '所选设备已连接；定期检测仅检查当前连接路径。'
          : selectedStatus === 'offline' ? '所选设备暂时离线，连接恢复后可继续使用。'
          : resolved ? '所选设备连接状态待确认，可刷新或直接执行设备预检。'
          : text(result.message, next.length ? `发现 ${next.length} 台设备，请选择本次采集使用的设备。` : '连接手机并完成 USB 调试授权后刷新设备。'))
      } catch (error) {
        if (!mounted.current || requestId !== discoveryRequest.current || activeCapture.current) return
        const states = { ...deviceStatusList.current }
        for (const device of deviceList.current) {
          if (!requested || (device.connection_mode === requested.connection_mode && device.bridge_id === requested.bridge_id)) states[captureDeviceKey(device)] = 'unconfirmed'
        }
        deviceStatusList.current = states
        setDeviceStates(states)
        const current = deviceChoice.current
        if (!requested || !current || (current.connection_mode === requested.connection_mode && current.bridge_id === requested.bridge_id)) {
          setDiscoveryError(controller.signal.aborted ? '设备检测超时，当前连接状态待确认；已保留设备选择。' : `暂时无法确认所选连接状态，已保留设备选择。${errorText(error)}`)
        }
      } finally {
        clearTimeout(timeout)
        if (discoveryController.current === controller) {
          discoveryController.current = null
          discoveryPromise.current = null
        }
        if (mounted.current && requestId === discoveryRequest.current) setDiscovering(false)
      }
    })()
    discoveryPromise.current = pending
    return pending
  }, [catalogReady, chooseDevice, refreshBridgeRegistry])
  useEffect(() => {
    mounted.current = true
    try {
      const saved = record(JSON.parse(localStorage.getItem(captureDevicePreferenceKey) ?? 'null'))
      const route = captureRoute(saved.route)
      const device = captureDevices({ devices: [saved.selected_device] })[0]
      if (route) chooseRoute(route)
      if (device) {
        deviceList.current = [device]
        deviceStatusList.current = { [captureDeviceKey(device)]: 'unconfirmed' }
        setDevices([device]); setDeviceStates(deviceStatusList.current)
        chooseDevice(device)
      }
    } catch { /* A missing or older preference falls back to automatic discovery. */ }
    return () => {
      mounted.current = false
      discoveryRequest.current += 1
      discoveryController.current?.abort()
      discoveryController.current = null
      discoveryPromise.current = null
      const attempt = repairAttempt.current
      if (attempt) {
        attempt.stopped = true
        if (attempt.requestId) cancelAgentTaskSubmission(attempt.requestId)
        if (attempt.ticket && !attempt.ticket.retry_job_id) void post(`/v1/scenario-capture/repair-requests/${encodeURIComponent(attempt.ticket.repair_request_id)}/cancel`, {}).catch(() => {})
      }
    }
  }, [chooseDevice, chooseRoute])
  useEffect(() => {
    if (!catalogReady || discoveryPaused) return
    // Opening the page must discover every connection once. Later health checks
    // stay scoped to the user's selected route and never replace that choice.
    const firstDiscovery = !initialDiscoveryStarted.current
    initialDiscoveryStarted.current = true
    void discover(firstDiscovery)
    const onFocus = () => { if (document.visibilityState === 'visible') void discover() }
    const timer = setInterval(onFocus, 12000)
    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onFocus)
    return () => { clearInterval(timer); window.removeEventListener('focus', onFocus); document.removeEventListener('visibilitychange', onFocus) }
  }, [catalogReady, discoveryPaused, discover])
  const handleDeviceCommand = useCallback((command: CaptureDeviceCommand) => {
    if (discoveryPaused) return
    if (command.action === 'select') {
      const target = captureTarget(command.target)
      selectDevice(target ? deviceList.current.find(device => captureDeviceKey(device) === captureTargetKey(target)) ?? null : null)
      void discover()
    } else if (command.action === 'route') {
      selectRoute(command.route)
      void discover()
    }
    else if (command.action === 'refresh') void discover(command.all === true)
  }, [discoveryPaused, selectDevice, selectRoute, discover])
  useEffect(() => {
    const listener = (event: Event) => {
      const detail = record((event as CustomEvent<unknown>).detail)
      if (['select', 'route', 'refresh'].includes(text(detail.action))) handleDeviceCommand(detail as CaptureDeviceCommand)
    }
    window.addEventListener(captureDeviceCommandEvent, listener)
    return () => window.removeEventListener(captureDeviceCommandEvent, listener)
  }, [handleDeviceCommand])
  useEffect(() => {
    const listener = (event: Event) => {
      const detail = record((event as CustomEvent).detail)
      const attempt = repairAttempt.current
      if (attempt?.requestId && text(detail.request_id) === attempt.requestId) {
        if (attempt.stopped) return
        if (detail.status === 'rejected') void stopRepair(`助手未接收修复请求：${text(detail.message, '请检查当前助手连接。')}`)
        else if (!attempt.ticket?.retry_job_id) setRepair({ ticket: attempt.ticket, status: 'repairing', message: '失败记录已发送到当前助手，正在分析并修复；完成后将重采一次。' })
        return
      }
      if (text(detail.request_id) !== agentRequest.current) return
      if (detail.status === 'rejected') setAgentWatching(false)
      setNotice({ tone: detail.status === 'rejected' ? 'error' : 'info', text: text(detail.message, detail.status === 'rejected' ? '页面助手未接受请求。' : '请求已发往助手终端，计划生成并保存后会自动回填。') }); agentRequest.current = ''
    }
    const updated = () => { void refresh() }
    window.addEventListener(agentAutonomousTaskStatusEventName, listener); window.addEventListener('parallel-workspace-updated', updated)
    return () => { window.removeEventListener(agentAutonomousTaskStatusEventName, listener); window.removeEventListener('parallel-workspace-updated', updated) }
  }, [refresh, stopRepair])
  useEffect(() => {
    if (!agentWatching) return
    let active = true
    const poll = async () => {
      try {
        const value = await api<{ plans: CapturePlan[] }>('/v1/scenario-capture/plans')
        if (!active) return
        setPlans(value.plans)
        const next = value.plans.find((plan) => !agentKnownPlans.current.has(plan.plan_id))
        if (!next) return
        const plan = await api<CapturePlan>(`/v1/scenario-capture/plans/${encodeURIComponent(next.plan_id)}`)
        if (!active) return
        setAgentWatching(false)
        if (agentDraftSnapshot.current === liveDraftSnapshot.current) {
          setSavedPlan(plan)
          setDraft(draftFromPlan(plan))
          setProfileJson(JSON.stringify(plan.profile, null, 2)); setDirty(false); setScript(null); setLocalPackage(null)
          setNotice({ tone: 'success', text: '助手生成的计划已回填，可以继续执行采样。' })
          focusWorkflowAction(captureAction)
        } else setNotice({ tone: 'info', text: '助手已生成新计划。当前草稿有后续修改，已保留；可在已保存计划中加载新版本。' })
      } catch { /* Keep polling actual plans; manual refresh reports connection errors. */ }
    }
    const timer = setInterval(() => void poll(), 4000)
    return () => { active = false; clearInterval(timer) }
  }, [agentWatching])
  const deviceRoutes = useMemo(() => {
    const routes = new Map<string, CaptureRouteOption>()
    const add = (route: CaptureRouteOption) => routes.set(captureRouteKey(route), route)
    add({ connection_mode: 'auto', bridge_id: '', display_name: '自动发现全部连接' })
    add({ connection_mode: 'local', bridge_id: '', display_name: '本机 HDC 直连' })
    for (const device of devices) if (device.connection_mode === 'bridge') add({ connection_mode: 'bridge', bridge_id: device.bridge_id, display_name: publicEndpointLabel(device.connection_label, '远程采集端') })
    for (const bridge of registeredBridges) if (text(bridge.bridge_id)) add({ connection_mode: 'bridge', bridge_id: text(bridge.bridge_id), display_name: publicEndpointLabel(text(bridge.display_name), '远程采集端'), connection_state: text(bridge.connection_state) })
    if (!routes.has(captureRouteKey(discoveryRoute))) add({ ...discoveryRoute, display_name: discoveryRoute.bridge_id })
    return [...routes.values()]
  }, [devices, registeredBridges, discoveryRoute])
  const deviceSelection = useMemo<CaptureDeviceSelectionView>(() => ({ devices, states: deviceStates, selected: captureTarget(selectedDevice), route: discoveryRoute, routes: deviceRoutes,
    discovering, locked: discoveryPaused, note: discoveryNote, error: discoveryError, diagnostics: discoveryDiagnostics }),
  [devices, deviceStates, selectedDevice, discoveryRoute, deviceRoutes, discovering, discoveryPaused, discoveryNote, discoveryError, discoveryDiagnostics])
  const context = useMemo<ScenarioCaptureAssistantContext>(() => ({ page_id: 'scenario_capture', plan_id: savedPlan?.plan_id ?? null, plan_draft: draft, selected_step_ids: draft?.capture.selected_step_ids ?? [], connection_mode: connectionMode, selected_device: selectedDevice, device_selection: deviceSelection, ...(selectedDevice ? { scenario_capture_target: captureTarget(selectedDevice)! } : {}), bridge_id: bridgeId || null, device_serial: deviceSerial || null, job_id: jobId || null, run_id: runId || null }), [savedPlan, draft, connectionMode, selectedDevice, deviceSelection, bridgeId, deviceSerial, jobId, runId])
  useEffect(() => { onAssistantContextChange(context) }, [context, onAssistantContextChange])
  useEffect(() => () => onAssistantContextChange(null), [onAssistantContextChange])

  function change(patch: Partial<CapturePlanDraft>) { setDraft((current) => current ? { ...current, ...patch } : current); setDirty(true); setScript(null); setLocalPackage(null) }
  function changeCapture(patch: Partial<CaptureSettings>) { if (draft) change({ capture: { ...draft.capture, ...patch } }) }
  function changeApplication(field: 'bundle' | 'ability', value: string) {
    if (!applicationProfile) return
    const profile = { ...applicationProfile, [field]: value }
    setProfileJson(JSON.stringify(profile, null, 2))
    change({ profile })
  }
  function selectTemplate(id: string) {
    if (!catalog || !draft) return
    const template = catalog.templates.find((item) => item.template_id === id)
    const profile = id === 'custom' ? { ...catalog.default_profile, bundle: '', ability: '', targets: {} } : catalog.default_profile
    change({ template_id: id, title: template?.name ?? '自定义负载采样', steps: template?.steps ?? [], profile,
      natural_language_steps: '', capture: { ...draft.capture, selected_step_ids: template ? catalog.default_capture.selected_step_ids : [] } })
    setProfileJson(JSON.stringify(profile, null, 2))
    setSavedPlan(null)
  }
  function customSteps(value: string) { if (!draft) return; const steps = value.split(/\r?\n/).map((line) => line.trim().replace(/^\d+[.、)）]\s*/, '')).filter(Boolean).map((title, index) => ({ step_id: `step_${String(index + 1).padStart(2, '0')}`, ordinal: index + 1, title, operation: 'unresolved' })); const ids = new Set(steps.map((step) => step.step_id)); change({ natural_language_steps: value, steps, capture: { ...draft.capture, selected_step_ids: draft.capture.selected_step_ids.filter((id) => ids.has(id)) } }) }
  async function loadPlan(id: string) { setBusy('load'); try { const plan = await api<CapturePlan>(`/v1/scenario-capture/plans/${encodeURIComponent(id)}`); setSavedPlan(plan); setDraft(draftFromPlan(plan)); setProfileJson(JSON.stringify(plan.profile, null, 2)); setDirty(false); setScript(null); setLocalPackage(null); focusWorkflowAction(captureAction) } catch (error) { setNotice({ tone: 'error', text: errorText(error) }) } finally { setBusy('') } }
  async function ensurePlan(): Promise<CapturePlan> { if (!draft) throw new Error('请先加载场景目录。'); if (!draft.steps.length) throw new Error('请至少填写一个步骤。'); if (!draft.capture.selected_step_ids.length) throw new Error('请选择需要采集的步骤。'); if (!draft.capture.collectors.length) throw new Error('请至少开启一种采集器。'); if (!dirty && savedPlan) return savedPlan; const profile: unknown = JSON.parse(profileJson); if (!profile || typeof profile !== 'object' || Array.isArray(profile)) throw new Error('应用与控件配置必须是 JSON 对象。'); if (!text(record(profile).bundle).trim() || !text(record(profile).ability).trim()) throw new Error('请填写目标应用包名和启动 Ability；也可以让页面助手补充。'); const plan = await post<CapturePlan>('/v1/scenario-capture/plans', { ...draft, profile }); setSavedPlan(plan); setDraft({ ...draft, profile: record(profile) }); setDirty(false); setPlans((current) => [plan, ...current.filter((item) => item.plan_id !== plan.plan_id)]); return plan }
  async function savePlan() { setBusy('save'); try { const plan = await ensurePlan(); setNotice({ tone: plan.readiness?.status === 'needs_input' ? 'info' : 'success', text: plan.readiness?.status === 'needs_input' ? '计划已保存。仍有步骤需要补充结构化操作或控件信息。' : '计划已保存，可直接执行负载采样。' }); if (plan.readiness?.status !== 'needs_input') focusWorkflowAction(captureAction) } catch (error) { setNotice({ tone: 'error', text: errorText(error) }) } finally { setBusy('') } }
  async function generateScript() { setBusy('script'); try { const plan = await ensurePlan(); const response = await fetch(`${apiBase}/v1/scenario-capture/plans/${encodeURIComponent(plan.plan_id)}/script`); if (!response.ok) throw apiErrorFromPayload(await response.json().catch(() => ({})), response.status); const content = await response.text(); const filename = response.headers.get('Content-Disposition')?.match(/filename="?([^";]+)/)?.[1] ?? `${plan.plan_id}.py`; const generated = { filename, content }; setScript(generated); downloadText(filename, content, 'text/x-python'); setNotice({ tone: 'success', text: 'Python 采集脚本已生成并下载，内容与当前保存计划一致。' }) } catch (error) { setNotice({ tone: 'error', text: errorText(error) }) } finally { setBusy('') } }
  async function generateLocalPackage() {
    setBusy('local-package')
    try {
      const plan = await ensurePlan()
      const device = deviceChoice.current
      const query = new URLSearchParams({ connection_mode: device?.connection_mode ?? 'auto' })
      if (device?.device_serial) query.set('device_serial', device.device_serial)
      if (device?.bridge_id) query.set('bridge_id', device.bridge_id)
      const response = await fetch(`${apiBase}/v1/scenario-capture/plans/${encodeURIComponent(plan.plan_id)}/local-package?${query}`)
      if (!response.ok) throw apiErrorFromPayload(await response.json().catch(() => ({})), response.status)
      const blob = await response.blob()
      const filename = response.headers.get('Content-Disposition')?.match(/filename="?([^";]+)/)?.[1] ?? `${plan.plan_id}-windows-local.zip`
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url; link.download = filename; link.click()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
      setLocalPackage({ filename, deviceSerial: device?.device_serial ?? '' })
      setNotice({ tone: 'success', text: device ? 'Windows 本地采集包已下载，已绑定当前设备。解压后运行 run-capture.ps1 即可。' : 'Windows 本地采集包已下载。解压后运行 run-capture.ps1，脚本会在 Windows 本机识别手机。' })
    } catch (error) { setNotice({ tone: 'error', text: errorText(error) }) }
    finally { setBusy('') }
  }
  async function execute() {
    if (captureStarting.current || activeCapture.current || repairActive) return
    jobSelectionRevision.current += 1
    captureStarting.current = true
    setBusy('execute')
    try {
      if (repairAttempt.current) await stopRepair('已开始新的手动采集，上一轮自动修复已结束。')
      if (repairAttempt.current?.ticket && ['pending', 'submitted'].includes(repairAttempt.current.ticket.status)) throw new Error('上一轮自动修复尚未确认停止，请等待状态返回后重新采集。')
      repairAttempt.current = null
      setRepair(null)
      await discoveryPromise.current
      if (!mounted.current) return
      const device = deviceChoice.current
      if (!device || deviceStatusList.current[captureDeviceKey(device)] === 'offline') throw new Error('正在等待所选设备连接，请保持手机连接并完成 USB 调试授权。')
      const plan = await ensurePlan()
      if (plan.readiness?.status === 'needs_input') throw new Error('请先补齐当前计划的操作与控件信息。')
      const receipt = await post<JobReceipt>(`/v1/scenario-capture/plans/${encodeURIComponent(plan.plan_id)}/execute`, { connection_mode: device.connection_mode, device_serial: device.device_serial, bridge_id: device.bridge_id })
      if (!receipt.job_id) throw new Error('服务未返回采样任务编号。')
      repairAttempt.current = { jobId: receipt.job_id, plan, device, used: false, stopped: !autoRepairEnabled.current }
      setJobId(receipt.job_id)
      setRunId(receipt.run_id ?? '')
      setTerminalRun(null)
      setNotice(null)
    } catch (error) { setNotice({ tone: 'error', text: errorText(error) }) } finally { captureStarting.current = false; if (mounted.current) setBusy('') }
  }
  async function cancel() { setBusy('cancel'); if (repairAttempt.current) repairAttempt.current.stopped = true; try { await post(`/v1/parallel-jobs/${encodeURIComponent(jobId)}/cancel`, {}); setNotice({ tone: 'info', text: '已请求取消，等待任务返回最终状态。' }) } catch (error) { setNotice({ tone: 'error', text: errorText(error) }) } finally { setBusy('') } }
  function changeAutoRepair(enabled: boolean) {
    autoRepairEnabled.current = enabled; setAutoRepair(enabled)
    try { localStorage.setItem(captureAutoRepairKey, String(enabled)) } catch { /* Storage is optional. */ }
    if (!enabled) void stopRepair()
  }
  async function importRun() { if (!activeRun) return; setBusy('import'); try { if (!hardwareId) throw new Error('请选择套用的参考硬件模型。'); const tid = targetTid.trim() ? Number(targetTid) : undefined; if (tid !== undefined && (!Number.isInteger(tid) || tid <= 0)) throw new Error('目标线程 TID 必须为正整数。'); const dataset = await post<ModelingDataset>('/v1/parallel-modeling/datasets', { capture_run_id: activeRun.run_id, hardware_profile_id: hardwareId, ...(resultStepId ? { step_id: resultStepId } : {}), ...(tid ? { target_tid: tid } : {}), name: `${draft?.title ?? '负载采样'} · ${resultStepId || activeRun.run_id}` }); if (!dataset.id) throw new Error('服务未返回建模数据集编号。'); onNavigateModeling?.(dataset.id); setNotice({ tone: 'success', text: `采样结果已导入建模数据集：${dataset.name}` }) } catch (error) { setNotice({ tone: 'error', text: errorText(error) }) } finally { setBusy('') } }
  function askAgent() {
    if (!draft) return
    try {
      const profile: unknown = JSON.parse(profileJson)
      if (!profile || typeof profile !== 'object' || Array.isArray(profile)) throw new Error('请将应用与控件配置填写为 JSON 对象。')
      const currentDraft = { ...draft, profile: record(profile) }
      const device = deviceChoice.current
      const target = captureTarget(device)
      const requestContext = { ...context, plan_draft: currentDraft, selected_device: device,
        scenario_capture_target: target, connection_mode: target?.connection_mode ?? 'auto',
        bridge_id: target?.bridge_id ?? null, device_serial: target?.device_serial ?? null }
      agentKnownPlans.current = new Set(plans.map((plan) => plan.plan_id))
      agentDraftSnapshot.current = liveDraftSnapshot.current
      setAgentWatching(true)
      const deviceMessage = target
        ? `本次采集设备已选定：connection_mode=${target.connection_mode}，bridge_id=${target.bridge_id || '空字符串'}，device_serial=${target.device_serial}。通过这一连接路径读取设备与控件证据。`
        : '当前尚未选择采集设备。先根据草稿生成计划，设备操作等待页面选择设备。'
      agentRequest.current = dispatchParallelAgent('scenario_capture', requestContext as unknown as JsonRecord,
        `请加载 scenario-step-capture Skill。${deviceMessage}读取当前场景草稿 ${JSON.stringify(currentDraft)}，将自然语言步骤转换为已注册的结构化操作与控件选择器。保留选中步骤、采集器与 PMU 参数，结合已有控件证据补充定位信息，输出计划与脚本并回填页面，供后续执行采集使用。`)
      setNotice({ tone: 'info', text: '正在向页面助手提交计划请求，保存的结果会自动回填。' })
    } catch (error) { setNotice({ tone: 'error', text: errorText(error) }) }
  }


  const errorForwarded = useWorkbenchErrorNotification(notice, () => setNotice(null), 'scenario-capture')
  return <div className="workspace-scroll parallel-studio">
    <header className="pm-heading capture-page-heading"><h2 className="pm-page-title" tabIndex={0} title="按步骤准备目标操作，同步采集 Trace、HiLog 与主线程 PMU，并将结果用于后续分析。">负载采样</h2><button className="quiet-button" type="button" onClick={() => void refresh()} disabled={loading}><RefreshCw size={14} className={loading ? 'spin' : ''} />刷新</button></header>
    {notice && !errorForwarded && <div className={`pm-notice ${notice.tone}`} role={notice.tone === 'error' ? 'alert' : 'status'}><CircleAlert size={15} /><span>{notice.text}</span><button type="button" onClick={() => setNotice(null)} aria-label="关闭提示"><X size={14} /></button></div>}
    {!draft && loading && <div className="pm-skeleton"><LoaderCircle size={18} className="spin" />正在读取负载模板与采样参数…</div>}
    {!draft && !loading && <div className="pm-empty">负载模板暂不可用。请检查服务连接后刷新。</div>}
    {draft && <>
      <section className="pm-section capture-device-step"><div className="pm-section-heading"><div><Smartphone size={17} /><h3>1. 设备连接</h3></div></div>
        <CaptureDevicePicker view={deviceSelection} onCommand={handleDeviceCommand} />
      </section>
      <section className="pm-section"><div className="pm-section-heading"><div><ClipboardList size={17} /><h3>2. 负载与步骤</h3></div><span className="pm-badge">{draft.steps.length} 个步骤 · {selected.size} 个采集点</span></div>
        <div className="pm-fields"><label className="pm-field"><span>负载模板</span><select value={draft.template_id} onChange={(event) => selectTemplate(event.target.value)} disabled={jobActive}>{catalog?.templates.map((item) => <option key={item.template_id} value={item.template_id}>{item.name} · {item.steps.length} 步</option>)}<option value="custom">自定义操作流程</option></select></label><label className="pm-field"><span>计划名称</span><input value={draft.title} onChange={(event) => change({ title: event.target.value })} maxLength={300} /></label></div>
        <div className="pm-fields" style={{ marginTop: 12 }}><label className="pm-field"><span>目标应用包名</span><input value={text(applicationProfile?.bundle)} onChange={(event) => changeApplication('bundle', event.target.value)} disabled={jobActive || !applicationProfile} placeholder="例如 com.example.notes" /><small>按当前应用定位主线程，所有场景共用同一 PMU 采样器。</small></label><label className="pm-field"><span>启动 Ability</span><input value={text(applicationProfile?.ability)} onChange={(event) => changeApplication('ability', event.target.value)} disabled={jobActive || !applicationProfile} placeholder="例如 EntryAbility" /><small>用于启动步骤；页面助手可根据应用信息补充。</small></label></div>
        {!applicationProfile && <p className="pm-hint">请先修复下方“应用、控件选择器与操作参数”中的 JSON，再编辑目标应用。</p>}
        {draft.template_id === 'custom' && <label className="pm-field" style={{ marginTop: 12 }}><span>操作步骤 · 每行一步</span><textarea rows={5} value={draft.natural_language_steps} onChange={(event) => customSteps(event.target.value)} placeholder={'启动图库\n打开一张图片\n点击编辑\n打开滤镜面板'} /><small>先保存自然语言草稿，再由页面助手补充操作类型与控件；未解析步骤不会进入设备执行。</small></label>}
        <div className="pm-grid capture-step-grid"><div className="capture-step-column"><div className="pm-toolbar" style={{ marginBottom: 9 }}><button className="quiet-button" type="button" onClick={() => changeCapture({ selected_step_ids: draft.steps.map((step) => step.step_id) })}>全选采集</button><button className="quiet-button" type="button" onClick={() => changeCapture({ selected_step_ids: [] })}>清空</button><small className="pm-hint">仅勾选步骤启动采集器</small></div><div className="pm-step-list" role="group" aria-label="需要采集的场景步骤">{draft.steps.map((step, index) => { const capture = selected.has(step.step_id); const prepares = !capture && index >= executionStart && index <= lastSelected; return <label className={`pm-step ${capture ? 'selected' : prepares ? 'preparation' : ''}`} key={step.step_id}><input type="checkbox" checked={capture} onChange={() => changeCapture({ selected_step_ids: capture ? draft.capture.selected_step_ids.filter((id) => id !== step.step_id) : [...draft.capture.selected_step_ids, step.step_id] })} /><span>{String(index + 1).padStart(2, '0')}</span><div><strong>{step.title}</strong><small>{operationNames[step.operation] ?? step.operation}{step.target ? ` · ${step.target}` : ''}</small></div><span className={`pm-badge ${step.operation === 'unresolved' ? 'warn' : capture ? 'active' : ''}`}>{step.operation === 'unresolved' ? '待解析' : capture ? '采集' : prepares ? '准备' : '不执行'}</span></label> })}{!draft.steps.length && <div className="pm-empty">输入操作步骤后，可在这里勾选采集点。</div>}</div></div>
          <div className="capture-step-settings"><div className="pm-box"><h4>界面准备方式</h4><div className="pm-choice-list"><label className="pm-choice"><input type="radio" name="capture-preparation" checked={draft.capture.preparation_mode === 'auto'} onChange={() => changeCapture({ preparation_mode: 'auto' })} /><span>自动准备<small>从第1步操作至最后一个采集点，未勾选步骤只准备界面。</small></span></label><label className="pm-choice"><input type="radio" name="capture-preparation" checked={draft.capture.preparation_mode === 'already_prepared'} onChange={() => changeCapture({ preparation_mode: 'already_prepared' })} /><span>界面已准备<small>从首个采集点开始；保留采集点之间的操作，并核验当前控件。</small></span></label></div></div><div className="pm-box capture-plan-action-box"><h4>执行范围</h4><p className="pm-hint">{lastSelected < 0 ? '尚未选择采集步骤。' : `将执行第 ${executionStart + 1} 至 ${lastSelected + 1} 步，其中 ${selected.size} 步记录运行数据。`}</p>{unresolvedSteps.length > 0 && <p className="pm-hint" style={{ marginTop: 9 }}>{unresolvedSteps.length} 个执行步骤需要解析；可让页面助手生成结构化计划。</p>}</div></div></div>
        {agentWatching && <div className="capture-action-progress" role="status"><LoaderCircle size={14} className="spin" /><span>助手正在生成并回填计划</span><button type="button" className="quiet-button" onClick={() => setAgentWatching(false)}>停止</button></div>}<button className="primary-button capture-step-primary" type="button" title="已保存的计划可以直接选择并执行，无需每次重新生成。" onClick={askAgent} disabled={!draft.steps.length || agentWatching}><Sparkles size={15} />{agentWatching ? '等待助手生成计划…' : '让助手生成计划'}</button>
      </section>
      <section className="pm-section capture-config-section">
        <div className="pm-section-heading"><div><Activity size={17} /><h3>3. 采样配置</h3></div></div>
        <div className="capture-config-primary-row">
          <div className="capture-mode-row" role="group" aria-label="采集模式"><strong>采集模式：</strong><div className="pm-collectors">
            {[{ id: 'trace', name: 'HiTrace' }, { id: 'hilog', name: 'HiLog' }, { id: 'pmu', name: 'PMU 计数' }].map((item) => <label className="pm-choice" key={item.id}><input type="checkbox" checked={draft.capture.collectors.includes(item.id)} onChange={(event) => changeCapture({ collectors: event.target.checked ? [...draft.capture.collectors, item.id] : draft.capture.collectors.filter((id) => id !== item.id) })} /><span>{item.name}</span></label>)}
          </div></div>
          {draft.capture.collectors.includes('pmu') && <div className="capture-pmu-panel">
            <strong>PMU 采样参数：</strong>
            <div className="pm-fields three capture-pmu-primary-fields">
              <label className="pm-field"><span>PMU 采集模式</span><select aria-label="PMU 采集模式" value={draft.capture.pmu.group} onChange={(event) => changeCapture({ pmu: { ...draft.capture.pmu, group: event.target.value } })}>{draft.capture.pmu.group === 'all' && <option value="all">自动 · 尽可能采集全部可用事件</option>}{availablePmuGroups.map((group) => <option key={group.id} value={group.id}>{group.label}</option>)}</select></label>
              <label className="pm-field"><span>目标采样间隔（μs）</span><input type="number" min={pmuPeriodMin} max={pmuPeriodMax} step={1} value={draft.capture.pmu.period_us} onChange={(event) => changeCapture({ pmu: { ...draft.capture.pmu, period_us: Number(event.target.value) } })} /></label>
              <label className="pm-field"><span>采样窗口（ms）</span><input type="number" min={100} max={10000} step={100} value={draft.capture.pmu.duration_ms} onChange={(event) => changeCapture({ pmu: { ...draft.capture.pmu, duration_ms: Number(event.target.value) } })} /></label>
            </div>
          </div>}
        </div>
        {draft.capture.collectors.includes('pmu') && <details className="pm-details capture-pmu-advanced"><summary>PMU 高级参数与采样器状态</summary>
            <div className="pm-fields" style={{ marginTop: 12 }}><label className="pm-field"><span>采样器所在 CPU</span><input type="number" min={0} max={255} value={draft.capture.pmu.reader_cpu} onChange={(event) => changeCapture({ pmu: { ...draft.capture.pmu, reader_cpu: Number(event.target.value) } })} /></label><label className="pm-field"><span>设备采样器路径</span><input value={draft.capture.pmu.binary_path} onChange={(event) => changeCapture({ pmu: { ...draft.capture.pmu, binary_path: event.target.value } })} /></label></div>
            <p className="pm-hint" style={{ marginTop: 10 }}>{automaticPmu ? '自动模式会尽可能采集多组事件；硬件资源不足时复用计数器，结果会分别报告运行覆盖与实际采样间隔。' : '单事件组可减少计数器竞争，适合复查关键指标。'}</p>
            {text(pmuBundle.status) && <div className={`pm-note ${pmuBundle.status === 'ready' ? '' : 'warning'}`} style={{ marginTop: 10 }} aria-label="平台 PMU 采样器状态"><Cpu size={15} /><div><strong>{pmuBundle.status === 'ready' ? '平台采样器已就绪' : pmuBundle.status === 'invalid' ? '平台采样器待修复' : '平台采样器待配置'}</strong><p>{text(pmuBundle.message)}</p>{pmuBundle.status !== 'ready' && <><p>设备已有可用采样器时仍可采集。正常部署会自动准备仓库随附采样器，请查看部署日志。</p><details className="pm-details"><summary>查看部署方法</summary><p>{text(record(pmuBundle.remediation).next_action)}</p><pre className="pm-code">{text(record(pmuBundle.remediation).build_command)}</pre></details></>}</div></div>}
            <div className="pm-note" style={{ marginTop: 10 }}><Cpu size={15} /><span>当前采集目标应用主线程的用户态计数。工作线程和内核态不计入；计数器复用和阶段边界可能产生估计值或覆盖缺口。</span></div>
          </details>}
        <details className="pm-details"><summary>应用、控件选择器与操作参数</summary><label className="pm-field"><span>应用与控件配置（JSON）</span><textarea className="pm-code" rows={12} spellCheck={false} value={profileJson} onChange={(event) => { setProfileJson(event.target.value); setDirty(true); setScript(null); setLocalPackage(null) }} /><small>应用包、Ability、控件 ID 与停留时间来自模板，可按场景调整。运行时自动检测屏幕尺寸并换算操作坐标。</small></label><details className="pm-details"><summary>当前结构化步骤</summary><pre className="pm-code">{JSON.stringify(draft.steps, null, 2)}</pre></details></details>
        {!dirty && savedPlan?.readiness?.blockers?.length ? <div className="pm-note warning" style={{ marginTop: 12 }}><CircleAlert size={15} /><span>{savedPlan.readiness.blockers.map((item) => `${text(item.step_id)}：${text(item.reason)}${item.target ? `（${item.target}）` : ''}`).join('；')}</span></div> : null}
        <p className="pm-hint" style={{ marginTop: 12 }}>执行前自动检查屏幕，按需拉起目标应用、解锁并关闭 USB 弹窗；采样前再次核验，临时延长熄屏时间并在结束后恢复。</p>
        <div className="pm-actions capture-secondary-actions"><button className="quiet-button" type="button" onClick={() => void generateScript()} disabled={Boolean(busy) || jobActive}><Code2 size={14} />生成 Python</button><button className="quiet-button" type="button" onClick={() => void generateLocalPackage()} disabled={Boolean(busy) || jobActive}>{busy === 'local-package' ? <LoaderCircle size={14} className="spin" /> : <Archive size={14} />}Windows 本地采集包</button><label className="pm-inline" style={{ cursor: 'pointer', gap: 6 }}><input type="checkbox" checked={autoRepair} onChange={(event) => changeAutoRepair(event.target.checked)} />失败后自动修复并重试</label></div>
        <div className="capture-plan-run-row">
          <button className="quiet-button capture-save-plan" type="button" onClick={() => void savePlan()} disabled={Boolean(busy) || jobActive}>{busy === 'save' ? <LoaderCircle size={14} className="spin" /> : <Save size={14} />}保存计划{dirty ? ' *' : ''}</button>
          <label className="capture-saved-plan-select"><span className="visually-hidden">已保存计划</span><select aria-label="已保存计划" value={!dirty ? savedPlan?.plan_id ?? '' : ''} onChange={(event) => { if (event.target.value) void loadPlan(event.target.value) }} disabled={Boolean(busy) || jobActive}><option value="">{dirty ? '当前配置尚未保存' : plans.length ? '请选择已保存计划' : '尚无已保存计划'}</option>{plans.map((plan) => <option key={plan.plan_id ?? plan.artifact_id} value={plan.plan_id ?? plan.artifact_id ?? ''}>{plan.title} · {timestamp(plan.created_at)} · {plan.capture?.selected_step_ids?.length ?? '—'} 个采集点</option>)}</select></label>
          <button ref={captureAction} className={`primary-button capture-execute-primary${jobActive ? ' danger' : ''}`} type="button" onClick={() => { if (jobActive) void cancel(); else void execute() }} disabled={jobActive ? busy === 'cancel' : Boolean(busy) || repairActive || connectionUnavailable || !deviceSerial.trim() || !selected.size || !draft.capture.collectors.length || unresolvedSteps.length > 0}>{jobActive ? busy === 'cancel' ? <LoaderCircle size={15} className="spin" /> : <Square size={15} /> : busy === 'execute' ? <LoaderCircle size={15} className="spin" /> : <Play size={15} />}{jobActive ? busy === 'cancel' ? '正在取消…' : '取消采集' : dirty || !savedPlan ? '保存并执行采样' : '在设备执行采样'}</button>
        </div>
        {savedPlan && <p className="pm-hint capture-plan-status">计划 {savedPlan.plan_id.slice(-12)} · {dirty ? '有未保存修改' : statusLabel(savedPlan.readiness?.status ?? 'ready')}</p>}
        {jobId && <section className="pm-progress capture-inline-job" aria-live="polite"><div className="pm-progress-head">{jobActive ? <LoaderCircle size={17} className="spin" /> : job?.status === 'succeeded' ? <Check size={17} /> : <CircleAlert size={17} />}<div><strong>设备采样 · {job ? statusLabel(job.status) : '正在读取任务'}</strong><small>{text(job?.progress.message ?? job?.progress.phase, jobId)}</small></div></div>{jobActive && <progress max={100} value={percent ?? undefined} aria-label="采样任务进度" />}{pollError && <p className="pm-hint" role="alert">状态连接中断，正在重试：{pollError}</p>}{jobFailure && <CaptureFailureNotice failure={jobFailure} />}<details className="pm-details"><summary>任务详情</summary><pre className="pm-code">{JSON.stringify(job?.progress ?? {}, null, 2)}</pre></details></section>}
        {script && <details className="pm-details"><summary>查看生成的 Python · {script.filename}</summary><pre className="pm-code">{script.content}</pre>{script.sha256 && <small className="pm-hint">SHA-256：{script.sha256}</small>}</details>}
        {localPackage && <details className="pm-details" open><summary>Windows 本地采样方法 · {localPackage.filename}</summary><ol className="pm-hint"><li>在连接手机的 Windows 电脑上解压采样包。</li><li>在 PowerShell 中进入解压目录，执行 <code>Set-ExecutionPolicy -Scope Process Bypass</code>，再执行 <code>.\run-capture.ps1</code>。</li><li>{localPackage.deviceSerial ? <>脚本默认使用设备 <code>{localPackage.deviceSerial}</code>。</> : <>脚本自动选择唯一连接的手机；有多台时会列出序列号，按提示用 <code>-DeviceSerial</code> 指定。</>}完成后会输出 <code>capture_….capture.zip</code>。</li><li>进入 Trace 管理导入采样包，即可继续负载特征与并行建模。</li></ol><p className="pm-hint">完整命令、HDC 路径和输出目录参数已写入包内 README。</p></details>}
      </section>
    </>}
    {repair && <section className="pm-box" aria-label="采集自动修复" aria-live="polite">
      <div className="pm-inline"><Sparkles size={16} /><strong>失败修复与重采</strong><span className={`pm-badge ${repair.status === 'failed' ? 'error' : repair.status === 'succeeded' ? 'good' : 'active'}`}>{({ creating: '准备修复', submitting: '发送中', repairing: '分析修复中', retrying: '正在重采', succeeded: '已完成', partial: '部分采集', failed: '未完成', cancelled: '已停止' })[repair.status]}</span></div>
      <p style={{ marginTop: 8 }}>{repair.message}</p><p className="pm-hint" style={{ marginTop: 5 }}>每次手动采集最多自动修复并重采一次；关闭选项可停止本次自动修复与重采。</p>
      {repair.ticket && <details className="pm-details"><summary>关联采集记录</summary><p>原采集：<code>{repair.ticket.source_run_id}</code></p>{repair.ticket.retry_run_id && <p>重采：<code>{repair.ticket.retry_run_id}</code></p>}</details>}
    </section>}
    <section className="pm-section"><div className="pm-section-heading"><div><FileJson2 size={17} /><h3>采样结果</h3></div><div>{onNavigateTraceLibrary && <button className="quiet-button" onClick={onNavigateTraceLibrary}>管理原始 Trace<ArrowRight size={13} /></button>}<small>{runs.length} 次运行</small></div></div>{runs.length ? <div className="pm-history">{runs.map((run) => <button type="button" className={runId === run.run_id ? 'selected' : ''} key={run.run_id} onClick={() => setRunId(run.run_id)}><strong>{text(run.title, run.run_id.slice(-14))}</strong><small>{timestamp(run.created_at)} · {runConnectionName(run)} · {run.device_serial}</small><span className={`pm-badge ${statusTone(run.status)}`}>{statusLabel(run.status)}</span></button>)}</div> : <div className="pm-empty"><Activity size={25} />完成负载采样后，这里会展示每个步骤的真实数据与完整性。</div>}
      {activeRun && <div style={{ marginTop: 13 }}><div className="pm-inline" style={{ marginBottom: 10 }}><span className={`pm-badge ${statusTone(activeRun.status)}`}>{statusLabel(activeRun.status)}</span><span className="pm-hint">{runConnectionName(activeRun)} · {activeRun.device_serial} · {activeRun.run_id}</span></div>{runFailure && <CaptureFailureNotice failure={runFailure} />}{activeRun.status === 'partial' && <div className="pm-note warning"><CircleAlert size={15} /><span>本次采集存在数据质量提示。已返回的数据已保留，后续分析需结合各项完整性状态。</span></div>}{resultSteps.length ? <div className="pm-table-scroll" style={{ marginTop: 10 }}><table className="pm-table"><thead><tr><th>步骤</th><th>Trace</th><th>HiLog</th><th>PMU</th><th>记录</th></tr></thead><tbody>{resultSteps.map((step, index) => { const status = record(step.collector_status); return <tr key={text(step.step_id, String(index))}><td>{text(step.title, text(step.step_id))}{step.status === 'failed' && <small><span className="pm-badge error">失败</span></small>}</td>{['trace', 'hilog', 'pmu'].map((kind) => <td key={kind} className={kind === 'pmu' ? 'wrap' : undefined}><span className={`pm-badge ${step.captured === false ? '' : statusTone(collectorStatus(status[kind]))}`}>{step.captured === false ? '准备步骤 · 未采集' : statusLabel(collectorStatus(status[kind]))}</span>{kind === 'pmu' && pmuSummaries[index]?.summary ? <small>{pmuSummaries[index].summary?.shortReason}</small> : text(record(status[kind]).reason ?? record(status[kind]).message) && <small>{text(record(status[kind]).reason ?? record(status[kind]).message)}</small>}</td>)}<td><code>{text(step.step_id)}</code></td></tr> })}</tbody></table></div> : <p className="pm-hint">本次运行尚未返回逐步采集清单。</p>}
        <PmuSamplerPreparation preparation={pmuPreparation} />
        {activeRun.status === 'failed' && <CaptureFailureEvidence run={activeRun} />}
        {screenAwakeGuard.protection_degraded === true && <div className="pm-note warning" style={{ marginTop: 10 }}><CircleAlert size={15} /><span>设备未确认临时延长熄屏时间，本轮采用采集前屏幕复核；长时间无操作时仍可能熄屏。具体原因已保存在完整清单中。</span></div>}
        {screenAwakeGuard.restoration_needed === true && !['running', 'queued'].includes(activeRun.status) && <div className="pm-note warning" style={{ marginTop: 10 }}><CircleAlert size={15} /><span>尚未确认恢复原熄屏时间，请查看完整清单中的屏幕设置恢复记录。</span></div>}
        {pmuSummaries.some(item => item.summary) && <div className="pm-pmu-results">{pmuSummaries.map(item => item.summary && <PmuCaptureQuality key={item.key} title={item.title} summary={item.summary} />)}</div>}
        <div className="pm-fields" style={{ marginTop: 12 }}><label className="pm-field"><span>导入建模的步骤</span><select value={resultStepId} onChange={(event) => setResultStepId(event.target.value)}><option value="">由数据集解析器选择</option>{resultSteps.map((step) => <option key={text(step.step_id)} value={text(step.step_id)}>{text(step.title, text(step.step_id))}</option>)}</select></label><label className="pm-field"><span>目标线程 TID（可选）</span><input type="number" min={1} value={targetTid} onChange={(event) => setTargetTid(event.target.value)} placeholder="留空由 Trace 识别" /></label><label className="pm-field full"><span>套用的参考硬件模型</span><select value={hardwareId} onChange={(event) => setHardwareId(event.target.value)}><option value="">请选择参考配置</option>{modelingCatalog?.hardware_profiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.name}</option>)}</select><small>按所选参考硬件的核型、物理核心和 CPU 频点做条件仿真，请确认与采样设备匹配。</small></label></div><div className="pm-actions capture-secondary-actions"><button className="quiet-button" type="button" onClick={() => downloadText(`${activeRun.run_id}.json`, JSON.stringify(activeRun.manifest ?? activeRun, null, 2))}><ArrowDownToLine size={14} />下载采样清单</button></div><button ref={importAction} className="primary-button capture-step-primary" type="button" disabled={Boolean(busy) || !hardwareId || !resultSteps.length || !['completed', 'succeeded', 'partial'].includes(activeRun.status)} onClick={() => void importRun()}>{busy === 'import' ? <LoaderCircle size={15} className="spin" /> : <ArrowRight size={15} />}导入并行建模</button><details className="pm-details"><summary>完整清单与采样缺口</summary><pre className="pm-code">{JSON.stringify(activeRun.manifest ?? activeRun, null, 2)}</pre></details></div>}
    </section>
  </div>
}
