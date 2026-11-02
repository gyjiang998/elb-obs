'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ArrowDownToLine, ArrowRight, Check, CircleAlert, Cpu, Database,
  FileCode2, FlaskConical, Gauge, GitBranch, Layers, LoaderCircle, Network,
  Play, RefreshCw, Save, ShieldCheck, Smartphone, Square, Timer, Upload,
  Workflow, X,
} from 'lucide-react'
import { formatBytes, platformApiFor } from '../lib/api'
import { ParallelCostModelExplanation } from './ParallelCostModelExplanation'
import { InterferenceCalibrationTable, ParallelInterferenceExplanation } from './ParallelInterferenceExplanation'
import { downloadText, errorText, metric, timestamp, type Notice } from '../lib/parallel-modeling'
import type { JsonRecord } from '../lib/types'
import { focusWorkflowAction } from '../lib/workflow-focus'
import { useWorkbenchErrorNotification } from './WorkbenchErrorNotification'
import { CalibrationFrequencyCurveGallery } from './CalibrationFrequencyCurveGallery'
import '../parallel-modeling.css'
import '../calibration-storage.css'

type Parameter = { id: string; name: string; description: string; current_value: unknown; method: string; status: string; calibratable: boolean }
type CoreGroup = { id: string; name: string; opps_khz: number[]; max_khz: number; physical_core_count: number; logical_cpu_ids?: number[] }
type HardwareProfile = JsonRecord & {
  id: string
  name: string
  core_groups: CoreGroup[]
  builtin?: boolean
  source?: string | JsonRecord
  model_status?: string
  display_source?: string
  display_source_label?: string
  display_provenance?: JsonRecord
}
type CalibrationCatalog = {
  model_structure: { name: string; status: string; formula: string; reference_anchor: JsonRecord | string; stages: { id: string; name: string; description: string }[]; limitations: string[] }
  parameter_catalog: Parameter[]
  benchmark_catalog: { id: string; name: string; description: string; parameters: string[] }[]
  hardware_profiles: HardwareProfile[]
  defaults: { hardware_profile_id: string; parameter_groups: string[]; repeats: number }
  measurement_contract: JsonRecord
}
type CalibrationPlan = JsonRecord & { id: string; name: string; created_at?: string; hardware_profile_id: string; parameter_groups: string[]; core_kinds?: string[]; frequency_roofline_core_kinds?: string[]; repeats: number; conditions?: JsonRecord[]; condition_count?: number }
type CalibrationFit = JsonRecord & {
  id: string
  plan_id: string
  status: string
  parameter_estimates: { parameter: string; path: string; value: unknown; method: string; sample_count: number; status: string }[]
  quality: { passed: boolean; publishable?: boolean; risk_level?: 'low' | 'medium' | 'high'; risk_summary?: string; evidence_grade?: string; failed_gates?: { category?: string; message?: string }[]; publication_blockers?: string[]; errors: string[]; warnings: string[]; accepted_samples: number; rejected_samples: number; train_samples: number; validation_samples: number; validation_median_ape: number | null; validation_p90_ape: number | null; repeat_cv_max: number | null; parameter_coverage?: unknown }
}
type CalibrationVersion = JsonRecord & { id: string; name: string; status: string; version: string; fit_id: string; created_at?: string; calibrated_parameters: unknown; inherited_parameters: unknown; validation: JsonRecord; risk_level?: string; risk_summary?: string; evidence_grade?: string; failed_gates?: unknown[]; display_source?: string; display_source_label?: string; display_provenance?: JsonRecord }

export type CalibrationMeasurementFitRequest = { planId: string; file: File; text: string }
export type CalibrationMeasurementFitHandler = (request: CalibrationMeasurementFitRequest) => Promise<CalibrationFit>
export type CalibrationDevice = { serial?: string; deviceSerial?: string; device_serial?: string; name?: string; model?: string; state?: string }
export type CalibrationDeviceListResult = CalibrationDevice[] | { devices: CalibrationDevice[] }
export type CalibrationRunStatus = 'preparing' | 'running' | 'cancelling' | 'succeeded' | 'partial' | 'failed' | 'cancelled'
export type CalibrationDeviceRun = {
  runId: string
  status: CalibrationRunStatus
  stage?: string
  progress?: { completed?: number; total?: number; attempted?: number; retries?: number }
  planId?: string
  hardwareProfileId?: string
  deviceSerialSha256?: string
  startedAt?: string
  finishedAt?: string
  qualitySummary?: JsonRecord
  frequencyRecovery?: { cleanupPending?: boolean; cleanupErrors?: JsonRecord[]; restoredAt?: string | null }
  cleanupWarnings?: JsonRecord[]
  error?: string | { code?: string; message?: string; details?: JsonRecord } | null
}
export type CalibrationRunStartRequest = { plan: CalibrationPlan; hardwareProfile: HardwareProfile; deviceSerial: string }
export type CalibrationRunMeasurements = {
  runId: string
  filename: string
  measurementText: string
  measurementSha256: string
  measurementSizeBytes: number
  sampleCount: number
  invalidSampleCount: number
  errorCount: number
}
export type CalibrationRunStartHandler = (request: CalibrationRunStartRequest) => Promise<CalibrationDeviceRun>
export type CalibrationRunStatusHandler = (request: { runId: string }) => Promise<CalibrationDeviceRun>
export type CalibrationRunMeasurementsHandler = (request: { runId: string }) => Promise<CalibrationRunMeasurements>
type LocalDevice = { serial: string; name: string }
type AutomaticPhase = '' | 'fit' | 'validate' | 'publish' | 'complete' | 'error'
const api = platformApiFor('optimization-calibration')
const formatValue = (value: unknown) => typeof value === 'number' ? metric(value, 4) : typeof value === 'string' ? value : JSON.stringify(value)
const coreName: Record<string, string> = { big: '大核', middle: '中核', small: '小核', build_ratio: 'BuildItem', residual_ratio: '其他 CPU 工作' }
const fitCanPublish = (fit: CalibrationFit | null | undefined) => Boolean(fit?.quality?.publishable ?? fit?.quality?.passed)

const primaryCalibrationGroupIds = ['frequency_roofline', 'pairwise_interference', 'dispatch_overhead']
const fallbackCalibrationGroupIds = ['core_ratios', 'frequency_response', 'contention']
const auxiliaryCalibrationGroupIds = ['classic_roofline']
const parameterPresentation: Record<string, { name: string; description: string }> = {
  frequency_roofline: {
    name: '任务类型频点性能曲线',
    description: '按核组分别实测计算、访存、混合任务在不同 OPP 下的单位工作量时延响应。',
  },
  pairwise_interference: {
    name: '负载组合干扰矩阵',
    description: '用定向单跑与两两共跑识别计算、访存、混合负载组合的时延膨胀。',
  },
  dispatch_overhead: {
    name: 'Worker 任务派发开销',
    description: '同核、同 OPP、同 payload 对比直接批量执行与逐项队列派发，测量持久 Worker 的暖态派发增量。',
  },
  core_ratios: {
    name: '核型 CPU 倍率',
    description: '任务类型频点性能曲线无法命中时，用各核组最高 OPP 的配对单跑结果回退估算换核成本。',
  },
  frequency_response: {
    name: '线性频率弹性',
    description: '目标核组或任务类型缺少实测频点曲线时使用的 On-CPU 频率回退模型。',
  },
  contention: {
    name: '通用争用 alpha',
    description: '未命中负载组合干扰矩阵或活动任务超出实测范围时的并发膨胀回退参数。',
  },
  classic_roofline: {
    name: '经典硬件 Roofline',
    description: '实测计算吞吐上限、持续带宽与计算强度阶梯，作为理解平台上限的辅助表征。',
  },
}

const calibrationTargets = [
  { id: 'frequency-curve', icon: Gauge, title: '任务类型频点性能曲线', benchmark: '计算、混合、访存三类冻结代理按核组扫描可控 OPP', evidence: '单位工作量 CPU / 墙钟时延、源码静态强度合同，以及同窗 IPC、Cache miss、Cache MPKI', fit: '形成核组 × 任务类型的频点—时延曲线，并用独立 OPP 验证' },
  { id: 'interference-matrix', icon: Network, title: '负载组合干扰矩阵', benchmark: '计算 × 访存 × 混合负载执行定向单跑与两两共跑', evidence: '固定双方落核、OPP 与工作集后的目标任务 CPU 时延膨胀', fit: '拟合 A←B 有向 slowdown 及适用域，用更换工作量的样本留出验证' },
  { id: 'dispatch-ipc', icon: Timer, title: 'Worker 任务派发开销', benchmark: '同一常驻 Worker 分别执行 direct batch 与逐项 queue dispatch', evidence: '同 payload 校验、端到端 wall、Worker CPU、producer CPU 与实际频率', fit: '按 (queue − direct) / task_count 拟合暖态 Odispatch；冷启动不纳入本轮，后续单独标定' },
]
const auxiliaryRooflineTargets = [
  { id: 'compute-roof', icon: Cpu, title: '计算屋顶', benchmark: '固定浮点流水；逐核组、逐 OPP 执行相同算法工作量', evidence: '源码算法 FLOP、CPU / 墙钟时间、实际频率与落核证据', fit: '拟合源级 Ppeak(f)；Instruction Roofline 待同窗 PMU 计数接入' },
  { id: 'bandwidth-roof', icon: Database, title: '大工作集带宽屋顶', benchmark: '固定大工作集顺序流式读写，逐核组、逐 OPP 重复执行', evidence: '源码最小访存字节数、墙钟时间、完整工作集覆盖证据', fit: '拟合持续源级带宽；不宣称已拆出 L1 / L2 / LLC / DRAM 层级' },
  { id: 'intensity-ladder', icon: Gauge, title: '计算强度阶梯', benchmark: '在相同工作集上扫描 2 / 8 / 32 / 128 / 512 FLOP/元素五档源码结构', evidence: '算法 FLOP / 最小源码 Byte 与实测 GFLOP/s；PMU 证据单独标记', fit: '要求强度阶梯跨越实测拐点，并交叉核验带宽屋顶与计算屋顶' },
]
const automaticStages = [
  { id: 'capability', title: '核验模型', detail: '所选模型、真机拓扑与 OPP' },
  { id: 'plan', title: '生成微基准', detail: '训练、重复与留出矩阵' },
  { id: 'deploy', title: '部署', detail: '受管微基准与哈希核验' },
  { id: 'measure', title: '真机扫描', detail: '绑核、控频、共跑与频率核验' },
  { id: 'fit', title: '拟合', detail: '约束回归与稳健聚合' },
  { id: 'validate', title: '留出验证', detail: '未参与拟合的 OPP / 组合' },
  { id: 'publish', title: '发布', detail: '绑定硬件并记录可追溯版本' },
]

function ParameterValue({ value }: { value: unknown }) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return <span>{formatValue(value)}</span>
  return <div className="cs-parameter-values">{Object.entries(value).map(([key, entry]) => <div key={key}><b>{coreName[key] ?? key}</b><span>{entry && typeof entry === 'object' ? Object.entries(entry).map(([field, amount]) => (coreName[field] ?? field) + ' ' + formatValue(amount)).join(' · ') : formatValue(entry)}</span></div>)}</div>
}
const parameterStatus = (status: string) => ({
  requires_measurements: '待实测校准', legacy_compat: '参考参数', legacy_proxy_transfer: '参考参数', sensitivity_assumption: '敏感性假设',
  retained_assumption: '保留假设', assumed: '假设值', assumption: '假设值', uncalibrated: '待校准', measured: '已有测量',
  fitted: '实测拟合', calibrated: '已校准', inherited: '沿用原模型', default: '默认值', fixed: '固定参考',
  not_calibrated: '待校准', estimated_from_measurements: '实测拟合', validated_proxy_parameter: '已通过验证', provisional_estimate: '待验证估计',
})[status] ?? status
const parameterName = (parameter: Parameter) => parameterPresentation[parameter.id]?.name ?? parameter.name
const parameterDescription = (parameter: Parameter) => parameterPresentation[parameter.id]?.description ?? parameter.description

function ParameterRows({ parameters }: { parameters: Parameter[] }) {
  return <>{parameters.map(parameter => <tr key={parameter.id}>
    <td><strong>{parameterName(parameter)}</strong><small>{parameterDescription(parameter)}</small></td>
    <td><span className={'pm-badge ' + (parameter.calibratable ? 'warn' : '')}>{parameterStatus(parameter.status)}</span><ParameterValue value={parameter.current_value} /></td>
    <td>{parameter.method}</td>
  </tr>)}</>
}

function normalizeDevices(value: CalibrationDeviceListResult): LocalDevice[] {
  const rows = Array.isArray(value) ? value : Array.isArray(value?.devices) ? value.devices : []
  return rows.map(item => {
    const serial = String(item.serial ?? item.deviceSerial ?? item.device_serial ?? '').trim()
    return { serial, name: String(item.name ?? item.model ?? serial).trim() }
  }).filter(item => item.serial)
}
function hardwareSourceLabel(profile: HardwareProfile): string {
  if (profile.display_source === 'bundled_offline_calibration') return profile.display_source_label || '平台离线实测'
  const source = typeof profile.source === 'string' ? profile.source : String(profile.source?.kind ?? '')
  if (profile.builtin || source === 'builtin_archive') return '平台内置'
  return source === 'local_hdc' ? '本机 HDC 提取' : '用户添加'
}
function runErrorMessage(run: CalibrationDeviceRun): string {
  if (typeof run.error === 'string') return run.error
  if (run.error?.message) return run.error.message
  return run.status === 'cancelled' ? '真机校准已取消。' : '真机校准未完成，请查看设备能力检查与执行记录。'
}
function runStageIndex(run: CalibrationDeviceRun | null, phase: AutomaticPhase): number {
  if (phase === 'fit') return 4
  if (phase === 'validate') return 5
  if (phase === 'publish' || phase === 'complete') return 6
  if (!run) return -1
  const stage = String(run.stage ?? '').toLowerCase()
  if (/publish|version/.test(stage)) return 6
  if (/valid|quality|holdout/.test(stage)) return 5
  if (/fit|regress/.test(stage)) return 4
  if (/clean|recover|restore/.test(stage)) return 3
  if (/measure|scan|benchmark|condition|run/.test(stage)) return 3
  if (/deploy|binary|install|upload/.test(stage)) return 2
  if (/plan|matrix/.test(stage)) return 1
  return 0
}

function runStageLabel(run: CalibrationDeviceRun, phase: AutomaticPhase): string {
  if (phase === 'fit') return '拟合模型'
  if (phase === 'validate') return '留出验证'
  if (phase === 'publish') return '发布模型'
  if (phase === 'complete') return '校准完成'
  const stage = String(run.stage ?? '').toLowerCase()
  if (/cleanup_pending/.test(stage)) return '等待恢复频率边界'
  if (/clean|recover|restore/.test(stage)) return '测量完成 · 正在恢复频率边界'
  if (/measure|scan|benchmark|condition|run/.test(stage)) return '真机微基准扫描'
  if (/deploy|binary|install|upload/.test(stage)) return '部署受管微基准'
  if (/ready|capability|probe/.test(stage)) return '核验设备与硬件模型'
  if (/plan|matrix/.test(stage)) return '生成实验矩阵'
  if (run.status === 'partial') return '测量完成 · 存在未通过样本'
  if (run.status === 'succeeded') return '测量完成'
  if (run.status === 'cancelled') return '校准已取消'
  if (run.status === 'failed') return '校准失败'
  return run.stage || run.status
}

export function OptimizationCalibrationStudio({
  onNavigateModeling,
  onNavigateHardwareModels,
  onFitMeasurements,
  onListCalibrationDevices,
  onStartCalibrationRun,
  onGetCalibrationRun,
  onCancelCalibrationRun,
  onGetCalibrationMeasurements,
  measurementByteLimit = 20 * 1024 * 1024,
  measurementImportEnabled = true,
  externalLinksEnabled = true,
}: {
  onNavigateModeling: (modelId?: string) => void
  onNavigateHardwareModels?: () => void
  onFitMeasurements?: CalibrationMeasurementFitHandler
  onListCalibrationDevices?: () => Promise<CalibrationDeviceListResult>
  onStartCalibrationRun?: CalibrationRunStartHandler
  onGetCalibrationRun?: CalibrationRunStatusHandler
  onCancelCalibrationRun?: CalibrationRunStatusHandler
  onGetCalibrationMeasurements?: CalibrationRunMeasurementsHandler
  measurementByteLimit?: number
  measurementImportEnabled?: boolean
  externalLinksEnabled?: boolean
}) {
  const [catalog, setCatalog] = useState<CalibrationCatalog | null>(null)
  const [plans, setPlans] = useState<CalibrationPlan[]>([])
  const [versions, setVersions] = useState<CalibrationVersion[]>([])
  const [fits, setFits] = useState<CalibrationFit[]>([])
  const [plan, setPlan] = useState<CalibrationPlan | null>(null)
  const [fit, setFit] = useState<CalibrationFit | null>(null)
  const [bundle, setBundle] = useState<{ files: { filename: string; media_type: string; content: string }[] } | null>(null)
  const [tab, setTab] = useState<'plan' | 'fit' | 'versions'>('plan')
  const [name, setName] = useState('任务类型真机频点与并发校准')
  const [versionName, setVersionName] = useState('')
  const [hardwareId, setHardwareId] = useState('')
  const [groups, setGroups] = useState<string[]>([])
  const [repeats, setRepeats] = useState(5)
  const [victimKind, setVictimKind] = useState('middle')
  const [peerKind, setPeerKind] = useState('middle')
  const [victimFrequency, setVictimFrequency] = useState(0)
  const [peerFrequency, setPeerFrequency] = useState(0)
  const [workingSetMiB, setWorkingSetMiB] = useState(64)
  const [measurementText, setMeasurementText] = useState('')
  const [measurementName, setMeasurementName] = useState('')
  const [measurementFile, setMeasurementFile] = useState<File | null>(null)
  const [devices, setDevices] = useState<LocalDevice[]>([])
  const [deviceSerial, setDeviceSerial] = useState('')
  const [deviceMessage, setDeviceMessage] = useState('')
  const [run, setRun] = useState<CalibrationDeviceRun | null>(null)
  const [automaticPhase, setAutomaticPhase] = useState<AutomaticPhase>('')
  const [autoPublish, setAutoPublish] = useState(true)
  const [busy, setBusy] = useState('')
  const [notice, setNotice] = useState<Notice | null>(null)
  const nextCalibrationAction = useRef<HTMLButtonElement | null>(null)
  const fitAction = useRef<HTMLButtonElement | null>(null)
  const publishAction = useRef<HTMLButtonElement | null>(null)
  const finalizedRuns = useRef(new Set<string>())
  const hardwareSelectionExplicit = useRef(false)
  const selectedHardware = catalog?.hardware_profiles.find(item => item.id === hardwareId) ?? null
  const coreGroups = selectedHardware?.core_groups ?? []
  const victimCore = coreGroups.find(item => item.id === victimKind)
  const peerCore = coreGroups.find(item => item.id === peerKind)
  const automaticCalibrationAvailable = Boolean(onListCalibrationDevices && onStartCalibrationRun && onGetCalibrationRun && onGetCalibrationMeasurements)
  const runExecutionActive = run?.status === 'preparing' || run?.status === 'running' || run?.status === 'cancelling'
  const runRecoveryPending = run?.frequencyRecovery?.cleanupPending === true
    && (!runExecutionActive || /clean|recover|restore/.test(String(run?.stage ?? '').toLowerCase()))
  const runActive = runExecutionActive || runRecoveryPending
  const measurementImportAvailable = measurementImportEnabled || Boolean(onFitMeasurements)
  const frequencyRooflineEligibleGroups = coreGroups.filter(group => new Set(group.opps_khz).size >= 6)
  const frequencyRooflineSkippedGroups = coreGroups.filter(group => new Set(group.opps_khz).size < 6)
  const classicRooflineSelected = groups.includes('classic_roofline')
  const classicRooflineOppCount = coreGroups.reduce((sum, group) => {
    const available = new Set(group.opps_khz).size
    return sum + Math.min(available, groups.includes('frequency_roofline') && available >= 6 ? 6 : 3)
  }, 0)
  const classicRooflineEstimatedRepeats = classicRooflineOppCount * (3 * Math.max(repeats, 3) + 10)

  const refresh = useCallback(async () => {
    setBusy('refresh')
    const results = await Promise.allSettled([
      api<CalibrationCatalog>('/v1/parallel-calibration/catalog'),
      api<{ plans: CalibrationPlan[] }>('/v1/parallel-calibration/plans'),
      api<{ fits: CalibrationFit[] }>('/v1/parallel-calibration/fits'),
      api<{ versions: CalibrationVersion[] }>('/v1/parallel-calibration/versions'),
    ])
    const [c, p, f, v] = results
    if (c.status === 'fulfilled') {
      setCatalog(c.value)
      setHardwareId(current => {
        if (hardwareSelectionExplicit.current && c.value.hardware_profiles.some(item => item.id === current)) return current
        hardwareSelectionExplicit.current = false
        return c.value.hardware_profiles.length === 1 ? c.value.hardware_profiles[0].id : ''
      })
      setGroups(current => current.length ? current : primaryCalibrationGroupIds.filter(id => c.value.parameter_catalog.some(parameter => parameter.id === id)))
      setRepeats(current => current || c.value.defaults.repeats)
    }
    if (p.status === 'fulfilled') setPlans(p.value.plans)
    if (f.status === 'fulfilled') setFits(f.value.fits)
    if (v.status === 'fulfilled') setVersions(v.value.versions)
    const errors = results.filter((result): result is PromiseRejectedResult => result.status === 'rejected')
    if (errors.length) setNotice({ tone: 'error', text: [...new Set(errors.map(result => errorText(result.reason)))].join('；') })
    else setNotice(current => current?.tone === 'error' ? null : current)
    setBusy('')
  }, [])

  const refreshDevices = useCallback(async (announce = false) => {
    if (!onListCalibrationDevices) return
    setBusy(current => current || 'devices')
    setDeviceMessage('正在读取本机 HDC 设备…')
    try {
      const rows = normalizeDevices(await onListCalibrationDevices())
      setDevices(rows)
      setDeviceSerial(current => rows.some(item => item.serial === current) ? current : rows.length ? rows[0].serial : '')
      setDeviceMessage(rows.length ? '已发现 ' + rows.length + ' 台本机直连设备。' : '尚未发现本机 HDC 设备。连接设备后刷新。')
      if (announce) setNotice({ tone: rows.length ? 'success' : 'info', text: rows.length ? '设备列表已刷新。' : '当前没有可用的本机 HDC 设备。' })
    } catch (error) {
      setDeviceMessage(errorText(error))
      if (announce) setNotice({ tone: 'error', text: errorText(error) })
    } finally {
      setBusy(current => current === 'devices' ? '' : current)
    }
  }, [onListCalibrationDevices])

  useEffect(() => { void refresh() }, [refresh])
  useEffect(() => { if (onListCalibrationDevices) void refreshDevices(false) }, [onListCalibrationDevices, refreshDevices])
  useEffect(() => {
    const updated = (event: Event) => {
      if ((event as CustomEvent<{ source?: string }>).detail?.source === 'hardware-profiles') void refresh()
    }
    window.addEventListener('parallel-workspace-updated', updated)
    return () => window.removeEventListener('parallel-workspace-updated', updated)
  }, [refresh])

  function selectHardwareModel(identity: string) {
    hardwareSelectionExplicit.current = true
    setHardwareId(identity)
  }

  async function loadPlan(id: string) {
    setBusy('plan')
    try {
      setPlan(await api<CalibrationPlan>('/v1/parallel-calibration/plans/' + encodeURIComponent(id)))
      setBundle(null); setFit(null); setMeasurementText(''); setMeasurementName(''); setMeasurementFile(null)
    } catch (error) { setNotice({ tone: 'error', text: errorText(error) }) }
    finally { setBusy('') }
  }
  function planRequestBody(parameterGroups = groups, hardwareProfile?: HardwareProfile) {
    const planningGroups = hardwareProfile?.core_groups ?? coreGroups
    const frequencyRooflineCoreKinds = planningGroups
      .filter(group => new Set(group.opps_khz).size >= 6)
      .map(group => group.id)
    const requestedVictim = planningGroups.find(item => item.id === victimKind)
    const selectedVictim = requestedVictim ?? planningGroups.find(item => item.physical_core_count >= 2) ?? planningGroups[0]
    const requestedPeer = planningGroups.find(item => item.id === peerKind)
    const selectedPeer = requestedPeer && (requestedPeer.id !== selectedVictim?.id || selectedVictim.physical_core_count >= 2)
      ? requestedPeer
      : selectedVictim?.physical_core_count >= 2 ? selectedVictim : planningGroups.find(item => item.id !== selectedVictim?.id)
    const selectedVictimFrequency = selectedVictim?.id === victimKind && selectedVictim.opps_khz.includes(victimFrequency)
      ? victimFrequency : selectedVictim?.max_khz
    const selectedPeerFrequency = selectedPeer?.id === peerKind && selectedPeer.opps_khz.includes(peerFrequency)
      ? peerFrequency : selectedPeer?.max_khz
    return {
      name, hardware_profile_id: hardwareId, parameter_groups: parameterGroups, repeats,
      ...(parameterGroups.includes('frequency_roofline') ? {
        frequency_roofline_core_kinds: frequencyRooflineCoreKinds,
      } : {}),
      ...(parameterGroups.includes('pairwise_interference') && selectedVictim && selectedPeer ? {
        interference_placements: [{
          victim_core_kind: selectedVictim.id, peer_core_kind: selectedPeer.id,
          victim_frequency_khz: selectedVictimFrequency,
          peer_frequency_khz: selectedVictim.id === selectedPeer.id ? selectedVictimFrequency : selectedPeerFrequency,
        }],
        working_set_bytes: workingSetMiB * 1024 * 1024,
      } : {}),
    }
  }
  async function persistPlan(hardwareProfile?: HardwareProfile, parameterGroups = groups): Promise<CalibrationPlan> {
    const result = await api<CalibrationPlan>('/v1/parallel-calibration/plans', {
      method: 'POST',
      body: JSON.stringify({ ...planRequestBody(parameterGroups, hardwareProfile), ...(hardwareProfile ? { hardware_profile_id: hardwareProfile.id } : {}) }),
    })
    setPlan(result); setPlans(current => [result, ...current.filter(item => item.id !== result.id)])
    setFit(null); setBundle(null); setMeasurementText(''); setMeasurementName(''); setMeasurementFile(null)
    return result
  }
  async function createPlan() {
    setBusy('plan')
    try {
      const result = await persistPlan()
      setNotice({ tone: 'success', text: '校准方案已保存。可直接在本机连接的真机上执行，也可在高级入口下载校准包。' })
      focusWorkflowAction(nextCalibrationAction)
      return result
    } catch (error) { setNotice({ tone: 'error', text: errorText(error) }); return null }
    finally { setBusy('') }
  }
  async function loadBundle() {
    if (!plan) return
    setBusy('bundle')
    try { setBundle(await api('/v1/parallel-calibration/plans/' + encodeURIComponent(plan.id) + '/bundle')) }
    catch (error) { setNotice({ tone: 'error', text: errorText(error) }) }
    finally { setBusy('') }
  }
  async function loadFit(id: string) {
    setBusy('fit-load')
    try {
      const result = await api<CalibrationFit>('/v1/parallel-calibration/fits/' + encodeURIComponent(id))
      setFit(result); setPlan(plans.find(value => value.id === result.plan_id) ?? null)
    } catch (error) { setNotice({ tone: 'error', text: errorText(error) }) }
    finally { setBusy('') }
  }
  async function readMeasurements(file: File | undefined) {
    if (!file) return
    try {
      if (file.size > measurementByteLimit) throw new Error('当前传输通道支持最大 ' + formatBytes(measurementByteLimit) + ' 的校准 JSON。请压缩或拆分后重试。')
      const contents = await file.text()
      const data = JSON.parse(contents)
      if (!data || Array.isArray(data) || typeof data !== 'object') throw new Error('请选择包含 provenance 和 samples 的校准 JSON 文件。')
      setMeasurementText(contents); setMeasurementName(file.name); setMeasurementFile(file); setFit(null); setNotice(null)
      focusWorkflowAction(fitAction)
    } catch (error) {
      setMeasurementText(''); setMeasurementName(''); setMeasurementFile(null); setNotice({ tone: 'error', text: errorText(error) })
    }
  }
  async function fitMeasurements() {
    if (!plan || !measurementText || !measurementFile) return
    setBusy('fit')
    try {
      const result = onFitMeasurements
        ? await onFitMeasurements({ planId: plan.id, file: measurementFile as File, text: measurementText })
        : await api<CalibrationFit>('/v1/parallel-calibration/fits', { method: 'POST', body: JSON.stringify({ plan_id: plan.id, measurements: JSON.parse(measurementText) }) })
      setFit(result); setFits(current => [result, ...current.filter(item => item.id !== result.id)])
      setNotice({ tone: result.quality.passed ? 'success' : 'info', text: result.quality.passed ? '拟合与质量检查通过，可以保存为寻优模型版本。' : fitCanPublish(result) ? '拟合结果已生成并可带风险发布；版本会保留失败门禁与证据等级。' : '拟合结果缺少可用实测、估计参数或来源完整性，当前不能发布。' })
      if (fitCanPublish(result)) focusWorkflowAction(publishAction)
    } catch (error) { setNotice({ tone: 'error', text: errorText(error) }) }
    finally { setBusy('') }
  }
  async function publishFit(result: CalibrationFit, navigateToVersions: boolean): Promise<CalibrationVersion> {
    const version = await api<CalibrationVersion>('/v1/parallel-calibration/versions', {
      method: 'POST', body: JSON.stringify({ fit_id: result.id, ...(versionName.trim() ? { name: versionName } : {}) }),
    })
    setVersions(current => [version, ...current.filter(item => item.id !== version.id)])
    if (navigateToVersions) setTab('versions')
    window.dispatchEvent(new Event('parallel-workspace-updated'))
    return version
  }
  async function publish() {
    if (!fit || !fitCanPublish(fit)) return
    setBusy('publish')
    try { await publishFit(fit, true); setNotice({ tone: 'success', text: '校准版本已保存，已加入并行建模的代价模型列表。' }) }
    catch (error) { setNotice({ tone: 'error', text: errorText(error) }) }
    finally { setBusy('') }
  }
  async function startAutomaticCalibration() {
    if (!catalog || !deviceSerial || !selectedHardware || !onStartCalibrationRun || !onGetCalibrationRun || !onGetCalibrationMeasurements) return
    setBusy('auto-start'); setAutomaticPhase(''); setRun(null)
    try {
      const hardwareProfile = selectedHardware
      setNotice({ tone: 'info', text: `正在用「${hardwareProfile.name}」生成微基准计划；启动前会自动核对真机核组、物理核与 OPP。` })
      const lowOppKneeGroups = hardwareProfile.core_groups.filter(group => new Set(group.opps_khz).size < 6)
      const rooflineCoreGroups = hardwareProfile.core_groups.filter(group => new Set(group.opps_khz).size >= 6)
      const lowOppLinearGroups = hardwareProfile.core_groups.filter(group => new Set(group.opps_khz).size < 3)
      let effectiveGroups = [...groups]
      const capabilityNotes: string[] = []
      if (effectiveGroups.includes('frequency_roofline') && lowOppKneeGroups.length) {
        if (!rooflineCoreGroups.length) effectiveGroups = effectiveGroups.filter(group => group !== 'frequency_roofline')
        capabilityNotes.push('任务类型频点性能曲线按核组要求至少 6 个 OPP；'
          + (rooflineCoreGroups.length ? rooflineCoreGroups.map(group => group.name).join('、') + ' 继续生成曲线，' : '')
          + lowOppKneeGroups.map(group => group.name).join('、') + ' 仅跳过该曲线，其他已选参数组仍执行')
      }
      if (effectiveGroups.includes('frequency_response') && lowOppLinearGroups.length) {
        effectiveGroups = effectiveGroups.filter(group => group !== 'frequency_response')
        capabilityNotes.push('兼容频率弹性需要每个核组至少 3 个 OPP；已跳过该回退参数')
      }
      if (!effectiveGroups.length) throw new Error('设备公开的 OPP 数量不足，当前没有可执行的校准参数组。')
      setGroups(effectiveGroups)
      const nextPlan = await persistPlan(hardwareProfile, effectiveGroups)
      const started = await onStartCalibrationRun({ plan: nextPlan, hardwareProfile, deviceSerial })
      finalizedRuns.current.delete(started.runId)
      setRun(started)
      setNotice({ tone: 'info', text: `已按「${hardwareProfile.name}」生成实验矩阵，正在准备并部署受管微基准。` + (capabilityNotes.length ? '能力降级：' + capabilityNotes.join('；') + '。' : '') })
    } catch (error) { setNotice({ tone: 'error', text: errorText(error) }) }
    finally { setBusy('') }
  }
  async function completeAutomaticCalibration(completedRun: CalibrationDeviceRun) {
    if (!onGetCalibrationMeasurements || finalizedRuns.current.has(completedRun.runId)) return
    finalizedRuns.current.add(completedRun.runId)
    setBusy('auto-finalize'); setAutomaticPhase('fit')
    try {
      const output = await onGetCalibrationMeasurements({ runId: completedRun.runId })
      const completedPlan = plan?.id === completedRun.planId ? plan : await api<CalibrationPlan>('/v1/parallel-calibration/plans/' + encodeURIComponent(completedRun.planId ?? ''))
      if (!completedPlan) throw new Error('真机校准完成，但对应的校准方案已不存在。')
      if (!output.measurementText) throw new Error('真机校准完成，但没有返回可拟合的测量数据。')
      const file = new File([output.measurementText], output.filename || 'measurements.json', { type: 'application/json' })
      const result = onFitMeasurements
        ? await onFitMeasurements({ planId: completedPlan.id, file, text: output.measurementText })
        : await api<CalibrationFit>('/v1/parallel-calibration/fits', { method: 'POST', body: JSON.stringify({ plan_id: completedPlan.id, measurements: JSON.parse(output.measurementText) }) })
      setPlan(completedPlan); setFit(result); setFits(current => [result, ...current.filter(item => item.id !== result.id)])
      setAutomaticPhase('validate'); setTab('fit')
      if (!fitCanPublish(result)) {
        setNotice({ tone: 'error', text: '真机扫描已完成，但没有形成可用实测参数，或来源完整性检查失败；平台已保留诊断结果。' })
        return
      }
      if (autoPublish) {
        setAutomaticPhase('publish')
        await publishFit(result, false)
        setAutomaticPhase('complete')
        setNotice({ tone: result.quality.passed ? 'success' : 'info', text: result.quality.passed ? '真机扫描、参数拟合、留出验证与模型发布已全部完成。' : '真机扫描与参数拟合已完成；模型已带总体风险、证据等级和失败门禁发布。' })
      } else {
        setAutomaticPhase('complete')
        setNotice({ tone: result.quality.passed ? 'success' : 'info', text: result.quality.passed ? '真机扫描、参数拟合与留出验证已完成，可以保存模型版本。' : '拟合已完成并可带风险保存；请查看总体风险与失败门禁。' })
        focusWorkflowAction(publishAction)
      }
    } catch (error) {
      finalizedRuns.current.delete(completedRun.runId); setAutomaticPhase('error'); setNotice({ tone: 'error', text: errorText(error) })
    } finally { setBusy('') }
  }
  async function cancelAutomaticCalibration() {
    if (!run || !onCancelCalibrationRun) return
    setBusy('auto-cancel')
    try {
      const result = await onCancelCalibrationRun({ runId: run.runId })
      setRun(result); setNotice({ tone: 'info', text: '已请求停止真机校准。已完成的测量仍保留在本机运行记录中。' })
    } catch (error) { setNotice({ tone: 'error', text: errorText(error) }) }
    finally { setBusy('') }
  }

  const activeRunId = runActive ? run?.runId ?? '' : ''
  useEffect(() => {
    if (!activeRunId || !onGetCalibrationRun) return
    let disposed = false
    let timer = 0
    const poll = async () => {
      try {
        const latest = await onGetCalibrationRun({ runId: activeRunId })
        if (!disposed) setRun(latest)
      } catch (error) {
        if (!disposed) setNotice({ tone: 'error', text: '读取真机校准进度失败：' + errorText(error) })
      } finally {
        if (!disposed) timer = window.setTimeout(poll, 1200)
      }
    }
    timer = window.setTimeout(poll, 500)
    return () => { disposed = true; window.clearTimeout(timer) }
  }, [activeRunId, onGetCalibrationRun])
  useEffect(() => {
    if (!run) return
    if (runRecoveryPending) {
      setAutomaticPhase('')
      setNotice({
        tone: 'info',
        text: run.stage === 'cleanup_pending'
          ? '实测已完成，首次恢复设备频率边界未成功；平台正在通过当前 HDC 连接自动重试，恢复完成后继续拟合。'
          : '实测已完成，正在恢复设备原始频率边界并清理微基准；完成后将自动继续拟合。',
      })
    } else if (run.status === 'succeeded' || run.status === 'partial') void completeAutomaticCalibration(run)
    else if (run.status === 'failed' || run.status === 'cancelled') {
      setAutomaticPhase('error')
      setNotice({ tone: run.status === 'cancelled' ? 'info' : 'error', text: runErrorMessage(run) })
    }
    // A terminal transition is the trigger. Progress updates must not repeat fitting.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.runId, run?.status, run?.stage, runRecoveryPending])

  const modelIcons = [FileCode2, Cpu, GitBranch, Layers]
  const currentRunStage = runStageIndex(run, automaticPhase)
  const progressCompleted = Number(run?.progress?.completed ?? 0)
  const progressTotal = Number(run?.progress?.total ?? 0)
  const progressAttempts = Number(run?.progress?.attempted ?? progressCompleted)
  const progressRetries = Number(run?.progress?.retries ?? 0)
  const progressPercent = progressTotal > 0 ? Math.min(100, Math.round(progressCompleted / progressTotal * 100)) : 0
  const acceptedSamples = Number(run?.qualitySummary?.sampleCount ?? 0)
  const invalidSamples = Number(run?.qualitySummary?.invalidSampleCount ?? 0)
  const collectionErrors = Number(run?.qualitySummary?.errorCount ?? 0)
  const errorForwarded = useWorkbenchErrorNotification(notice, () => setNotice(null), 'optimization-calibration')

  return <div className="parallel-studio calibration-studio">
    <div className="pm-heading"><div><h2 className="pm-page-title" tabIndex={0} title="平台自动在目标设备上构建实验矩阵、执行微基准、拟合并验证并行代价模型。">寻优校准</h2></div><div className="pm-toolbar"><button className="quiet-button" disabled={Boolean(busy)} onClick={() => void refresh()}><RefreshCw size={14} />刷新</button><button className="quiet-button" onClick={() => onNavigateModeling()}>前往并行建模<ArrowRight size={14} /></button></div></div>
    {notice && !errorForwarded && <div className={'pm-notice ' + notice.tone} role="status"><CircleAlert size={15} /><span>{notice.text}</span><button aria-label="关闭提示" onClick={() => setNotice(null)}><X size={14} /></button></div>}
    {!catalog ? busy === 'refresh'
      ? <div className="pm-skeleton"><LoaderCircle size={18} className="spin" />正在读取校准方法、方案与历史拟合…</div>
      : <div className="pm-empty"><CircleAlert size={24} />尚未读取到校准目录。连接并初始化服务器环境后重试。</div>
      : <>
        <section className="pm-section">
          <div className="pm-section-heading"><div><GitBranch size={17} /><h3>寻优模型如何工作</h3></div><span className="pm-badge">关键路径 · 离散事件仿真</span></div>
          <div className="cs-model-map">{catalog.model_structure.stages.map((stage, index) => {
            const Icon = modelIcons[index % modelIcons.length]
            return <div key={stage.id}><Icon size={21} /><strong>{stage.name}</strong><p>{stage.description}</p></div>
          })}</div>
          <ParallelCostModelExplanation />
          <ParallelInterferenceExplanation onPreparePlan={() => { setGroups(['pairwise_interference']); setName('负载组合共跑干扰校准'); setTab('plan'); document.getElementById('calibration-workbench')?.scrollIntoView({ behavior: 'smooth', block: 'start' }) }} />
          <div className="pm-note" style={{ marginTop: 12 }}><Cpu size={16} /><span><strong>CPU 频点（OPP）是什么？</strong> OPP 是芯片支持的频率与电压工作点。平台扫描可控频率档位并核验实测频率；同一频率域内的核共享 OPP。当前目标是时延模型，不估计电压、功耗或能量。{externalLinksEnabled && <a href="https://docs.kernel.org/power/opp.html" target="_blank" rel="noreferrer"> OPP 定义</a>}</span></div>
          <details className="pm-details"><summary>模型适用范围与当前假设</summary>{catalog.model_structure.limitations.map((item, index) => <p className="pm-hint" style={{ marginTop: 7 }} key={index}>{item}</p>)}</details>
        </section>

        <section className="pm-section cs-design-section">
          <div className="pm-section-heading"><div><FlaskConical size={17} /><h3>自动真机校准设计</h3></div><small>参数 → 微基准 → 观测量 → 拟合结果</small></div>
          <p className="cs-design-intro">一键校准以任务类型频点性能曲线、负载组合干扰矩阵和 Worker 任务派发开销为主校准项。平台从设备自动提取核组与 OPP，生成受管微基准实验矩阵；训练样本用于拟合，未参与拟合的 OPP、工作规模与负载组合用于留出验证。来源、结构或测量完整性失败时阻断发布；重复抖动、覆盖或留出误差等统计质量门未通过时仍生成风险模型，并保存失败项与证据等级。</p>
          <div className="cs-calibration-targets">{calibrationTargets.map(item => {
            const Icon = item.icon
            return <article key={item.id}><div className="cs-target-title"><span><Icon size={16} /></span><strong>{item.title}</strong></div><dl><div><dt>微基准</dt><dd>{item.benchmark}</dd></div><div><dt>采集</dt><dd>{item.evidence}</dd></div><div><dt>校准</dt><dd>{item.fit}</dd></div></dl></article>
          })}</div>
          <div className="pm-section-heading" style={{ marginTop: 18 }}><div><Cpu size={16} /><h4>辅助硬件表征 · 经典 Roofline</h4></div><small>用于理解平台计算与带宽上限，不与三项主校准参数混合</small></div>
          <p className="pm-hint">经典 Roofline 当前使用冻结源码的算法 FLOP 与最小字节计数；Instruction Roofline 只有在 retired instructions 与 DRAM bytes 完成同窗 PMU 对齐后才可发布。</p>
          <div className="cs-calibration-targets">{auxiliaryRooflineTargets.map(item => {
            const Icon = item.icon
            return <article key={item.id}><div className="cs-target-title"><span><Icon size={16} /></span><strong>{item.title}</strong></div><dl><div><dt>微基准</dt><dd>{item.benchmark}</dd></div><div><dt>采集</dt><dd>{item.evidence}</dd></div><div><dt>拟合</dt><dd>{item.fit}</dd></div></dl></article>
          })}</div>
          <div className="cs-automatic-flow" aria-label="自动校准流程">{automaticStages.map((stage, index) => <div key={stage.id}><span>{index + 1}</span><strong>{stage.title}</strong><small>{stage.detail}</small></div>)}</div>
          <details className="pm-details"><summary>查看当前模型参数与微基准目录</summary>
            <h4 style={{ marginTop: 14 }}>主校准参数</h4>
            <div className="pm-table-scroll" style={{ marginTop: 8 }}><table className="pm-table cs-parameter-table"><thead><tr><th>参数 / 作用</th><th>当前值与来源</th><th>校准方法</th></tr></thead><tbody><ParameterRows parameters={catalog.parameter_catalog.filter(parameter => primaryCalibrationGroupIds.includes(parameter.id))} /></tbody></table></div>
            <h4 style={{ marginTop: 16 }}>辅助硬件表征</h4>
            <div className="pm-table-scroll" style={{ marginTop: 8 }}><table className="pm-table cs-parameter-table"><thead><tr><th>参数 / 作用</th><th>当前值与来源</th><th>校准方法</th></tr></thead><tbody><ParameterRows parameters={catalog.parameter_catalog.filter(parameter => auxiliaryCalibrationGroupIds.includes(parameter.id))} /></tbody></table></div>
            <div className="pm-note" style={{ marginTop: 16 }}><CircleAlert size={14} /><span><strong>兼容回退</strong>：仅当主校准曲线或干扰矩阵无法命中时使用，并在寻优结果中标明回退范围。</span></div>
            <div className="pm-table-scroll" style={{ marginTop: 8 }}><table className="pm-table cs-parameter-table"><thead><tr><th>兼容回退参数</th><th>当前值与来源</th><th>校准方法</th></tr></thead><tbody><ParameterRows parameters={catalog.parameter_catalog.filter(parameter => fallbackCalibrationGroupIds.includes(parameter.id))} /></tbody></table></div>
            <div className="pm-table-scroll" style={{ marginTop: 8 }}><table className="pm-table cs-parameter-table"><tbody><ParameterRows parameters={catalog.parameter_catalog.filter(parameter => ![...primaryCalibrationGroupIds, ...auxiliaryCalibrationGroupIds, ...fallbackCalibrationGroupIds].includes(parameter.id))} /></tbody></table></div>
            <div className="cs-methods" style={{ marginTop: 12 }}>{catalog.benchmark_catalog.map(method => <article key={method.id}><h4>{method.name}</h4><p>{method.description}</p><div className="pm-inline" style={{ flexWrap: 'wrap' }}>{method.parameters.map(id => { const parameter = catalog.parameter_catalog.find(item => item.id === id); return <span className="pm-badge" key={id}>{parameter ? parameterName(parameter) : id}</span> })}</div></article>)}</div>
          </details>
        </section>

        <section id="calibration-workbench" className="pm-section">
          <div className="pm-section-heading"><div><Layers size={17} /><h3>校准工作台</h3></div><small>默认一键完成真机测量、拟合、验证与发布</small></div>
          <div className="cs-workflow-tabs" role="tablist" aria-label="校准工作台"><button role="tab" aria-selected={tab === 'plan'} className={tab === 'plan' ? 'selected' : ''} onClick={() => setTab('plan')}>真机校准</button><button role="tab" aria-selected={tab === 'fit'} className={tab === 'fit' ? 'selected' : ''} onClick={() => setTab('fit')}>拟合结果 · {fits.length}</button><button role="tab" aria-selected={tab === 'versions'} className={tab === 'versions' ? 'selected' : ''} onClick={() => setTab('versions')}>模型版本 · {versions.length}</button></div>

          {tab === 'plan' && <>
            <div className="cs-auto-run-grid">
              <div className="pm-box cs-auto-device">
                <div className="pm-section-heading"><div><Smartphone size={16} /><h3>校准对象</h3></div><button className="quiet-button" disabled={!onListCalibrationDevices || Boolean(busy)} onClick={() => void refreshDevices(true)}><RefreshCw size={13} />刷新</button></div>
                <label className="pm-field cs-hardware-model-select"><span>硬件模型</span><select value={hardwareId} disabled={!catalog.hardware_profiles.length || runActive} onChange={event => selectHardwareModel(event.target.value)}><option value="" disabled>{catalog.hardware_profiles.length ? '请选择硬件模型' : '尚未登记硬件模型'}</option>{catalog.hardware_profiles.map(hardware => <option key={hardware.id} value={hardware.id}>{hardware.name} · {hardwareSourceLabel(hardware)}</option>)}</select><small>列出“硬件模型”页面中所有未归档配置。平台按所选模型生成微基准，并在执行前自动核对真机。</small></label>
                {selectedHardware && <div className="cs-selected-hardware"><div><Cpu size={15} /><span><strong>{selectedHardware.name}</strong><small>{hardwareSourceLabel(selectedHardware)} · {selectedHardware.core_groups.length} 个核组 · {selectedHardware.core_groups.reduce((sum, group) => sum + Number(group.physical_core_count || 0), 0)} 个物理核 · {selectedHardware.core_groups.reduce((sum, group) => sum + (Array.isArray(group.logical_cpu_ids) ? group.logical_cpu_ids.length : Number(group.physical_core_count || 0)), 0)} 个逻辑 CPU</small></span></div><div>{selectedHardware.core_groups.map(group => <span key={group.id}>{group.name} · {group.physical_core_count} 物理核 / {Array.isArray(group.logical_cpu_ids) ? group.logical_cpu_ids.length : group.physical_core_count} 逻辑 CPU · {group.opps_khz.length} 档 OPP</span>)}</div></div>}
                {selectedHardware && groups.includes('frequency_roofline') && frequencyRooflineSkippedGroups.length > 0 && <div className="pm-note"><Gauge size={15} /><span>频点性能曲线按核组生成：{frequencyRooflineEligibleGroups.length ? frequencyRooflineEligibleGroups.map(group => group.name).join('、') + ' 满足至少 6 档 OPP；' : ''}{frequencyRooflineSkippedGroups.map(group => group.name).join('、')} 仅跳过该曲线，负载组合、Worker 开销等已选校准仍保留。</span></div>}
                {!catalog.hardware_profiles.length && <div className="pm-note warning"><CircleAlert size={15} /><span>尚未登记硬件模型。请先从当前设备提取，或在硬件模型页面手动添加。</span></div>}
                {onNavigateHardwareModels && <button className="quiet-button cs-manage-hardware" type="button" disabled={runActive} onClick={onNavigateHardwareModels}><Cpu size={13} />管理硬件模型</button>}
                <label className="pm-field"><span>执行校准的本机 HDC 设备</span><select value={deviceSerial} disabled={!devices.length || runActive} onChange={event => setDeviceSerial(event.target.value)}><option value="" disabled>{devices.length ? '请选择设备' : '尚未发现设备'}</option>{devices.map(device => <option key={device.serial} value={device.serial}>{device.name === device.serial ? device.serial : device.name + ' · ' + device.serial}</option>)}</select></label>
                <p className="pm-hint">{deviceMessage || '页面打开后自动发现本机直连设备。'}</p>
                <div className="cs-auto-summary"><div><span>硬件模型</span><strong>{selectedHardware?.name ?? '等待选择'}</strong></div><div><span>微基准计划</span><strong>平台自动生成 · 每条件 {repeats} 次</strong></div><div><span>完成策略</span><strong>{autoPublish ? '完整性通过后自动发布' : '拟合后等待确认'}</strong></div></div>
                <label className="pm-choice cs-auto-intensity"><input type="checkbox" checked={classicRooflineSelected} disabled={runActive} onChange={event => setGroups(current => event.target.checked ? [...new Set([...current, 'classic_roofline'])] : current.filter(id => id !== 'classic_roofline'))} /><span><strong>同时采集计算强度与经典 Roofline（可选）</strong><small>增加浮点计算屋顶、持续源码带宽及 5 档 FLOP/最小源码 Byte 扫描{classicRooflineEstimatedRepeats > 0 ? `；当前硬件约增加 ${classicRooflineEstimatedRepeats} 次实测` : ''}。应用阶段 Instruction/DRAM Byte 仍需同窗 PMU。</small></span></label>
                <label className="pm-choice cs-auto-publish"><input type="checkbox" checked={autoPublish} disabled={runActive} onChange={event => setAutoPublish(event.target.checked)} /><span>来源与结构完整性通过后自动发布；统计质量门失败时标记总体风险</span></label>
                {!automaticCalibrationAvailable && <div className="pm-note warning"><CircleAlert size={15} /><span>自动真机校准需要桌面工作台的本机 HDC 执行能力。当前仍可在下方高级入口生成方案或导入已有测量。</span></div>}
                <div className="pm-actions">{runRecoveryPending
                  ? <button className="primary-button pm-step-action" disabled><LoaderCircle size={15} className="spin" />正在恢复设备频率边界</button>
                  : runExecutionActive
                  ? <button className="primary-button pm-step-action cs-cancel-action" disabled={!onCancelCalibrationRun || busy === 'auto-cancel' || run?.status === 'cancelling'} onClick={() => void cancelAutomaticCalibration()}>{busy === 'auto-cancel' || run?.status === 'cancelling' ? <LoaderCircle size={15} className="spin" /> : <Square size={15} />}{run?.status === 'cancelling' ? '正在停止真机校准' : '停止真机校准'}</button>
                  : <button ref={nextCalibrationAction} className="primary-button pm-step-action" disabled={!automaticCalibrationAvailable || !deviceSerial || !selectedHardware || !groups.length || Boolean(busy)} onClick={() => void startAutomaticCalibration()}>{busy === 'auto-start' || busy === 'auto-finalize' ? <LoaderCircle size={15} className="spin" /> : <Play size={15} />}一键生成微基准并校准</button>}</div>
              </div>
              <div className="pm-box cs-run-progress">
                <div className="pm-section-heading"><div><Workflow size={16} /><h3>执行闭环</h3></div><span className={'pm-badge ' + (automaticPhase === 'complete' ? 'good' : run?.status === 'failed' && !runRecoveryPending ? 'warn' : '')}>{automaticPhase === 'complete' ? '已发布' : runRecoveryPending ? '正在恢复频率' : runExecutionActive ? '执行中' : run?.status === 'partial' ? '部分完成' : run?.status === 'failed' ? '失败' : '等待执行'}</span></div>
                <div className="cs-run-stage-list">{automaticStages.map((stage, index) => {
                  const state = index < currentRunStage ? 'done' : index === currentRunStage ? 'active' : 'pending'
                  return <div className={state} key={stage.id}><span>{state === 'done' ? <Check size={12} /> : index + 1}</span><div><strong>{stage.title}</strong><small>{stage.detail}</small></div></div>
                })}</div>
                {run && <div className="cs-run-detail"><div><span>{runStageLabel(run, automaticPhase)}</span><strong>{progressTotal > 0 ? progressCompleted + ' / ' + progressTotal + ' 个计划重复' : run.status}</strong></div><div className="cs-run-track"><span style={{ width: progressPercent + '%' }} /></div>{progressAttempts > 0 && <small>已执行 {progressAttempts} 次实测{progressRetries > 0 ? ` · 频率或同胞线程质量补测 ${progressRetries} 次` : ''} · 保存 {acceptedSamples} 个原始样本{invalidSamples > 0 ? ` · ${invalidSamples} 个未通过实际频率、落核或同胞线程静默核验` : ''}{collectionErrors > 0 ? ` · ${collectionErrors} 个执行错误` : ''}</small>}<small>运行记录 {run.runId}{run.deviceSerialSha256 ? ' · 设备 ' + run.deviceSerialSha256.slice(0, 12) : ''}</small></div>}
                {run?.cleanupWarnings?.length ? <div className="pm-note warning"><CircleAlert size={14} /><span>设备原始频率边界已经恢复；临时微基准或本地恢复记录仍在后台清理，不影响拟合与发布。</span></div> : null}
                {!run && <div className="cs-run-placeholder"><ShieldCheck size={22} /><p>平台会先核验频率控制与核绑定，并保存热区原始快照；测量窗口只有实际频率稳定时才会被接纳。失败会停在具体步骤并保留诊断，不要求用户准备 JSON。</p></div>}
                {run && !run.frequencyRecovery?.cleanupPending && (run.status === 'succeeded' || run.status === 'partial') && automaticPhase === 'error' && <button className="quiet-button" disabled={Boolean(busy)} onClick={() => { finalizedRuns.current.delete(run.runId); void completeAutomaticCalibration(run) }}><RefreshCw size={13} />重试拟合与发布</button>}
              </div>
            </div>

            <details className="pm-details cs-advanced-plan"><summary>高级方案设置与离线兼容方式</summary>
              <div className="pm-grid equal" style={{ marginTop: 14 }}>
                <div className="pm-box"><h4>实验矩阵设置</h4>
                  <div className="pm-fields"><label className="pm-field full"><span>方案名称</span><input value={name} onChange={event => setName(event.target.value)} /></label><label className="pm-field"><span>参考硬件</span><select value={hardwareId} onChange={event => selectHardwareModel(event.target.value)}><option value="" disabled>请选择硬件模型</option>{catalog.hardware_profiles.map(hardware => <option key={hardware.id} value={hardware.id}>{hardware.name} · {hardwareSourceLabel(hardware)}</option>)}</select></label><label className="pm-field"><span>每个条件重复次数</span><input type="number" min={3} max={30} value={repeats} onChange={event => setRepeats(Number(event.target.value))} /></label></div>
                  <h4 style={{ marginTop: 14 }}>主校准项</h4>
                  <div className="pm-choice-list" style={{ marginTop: 8 }}>{catalog.parameter_catalog.filter(parameter => primaryCalibrationGroupIds.includes(parameter.id)).map(parameter => <label className="pm-choice" key={parameter.id}><input type="checkbox" checked={groups.includes(parameter.id)} onChange={() => setGroups(current => current.includes(parameter.id) ? current.filter(id => id !== parameter.id) : [...current, parameter.id])} /><span>{parameterName(parameter)}</span></label>)}</div>
                  <h4 style={{ marginTop: 14 }}>辅助硬件表征</h4>
                  <div className="pm-choice-list" style={{ marginTop: 8 }}>{catalog.parameter_catalog.filter(parameter => auxiliaryCalibrationGroupIds.includes(parameter.id)).map(parameter => <label className="pm-choice" key={parameter.id}><input type="checkbox" checked={groups.includes(parameter.id)} onChange={() => setGroups(current => current.includes(parameter.id) ? current.filter(id => id !== parameter.id) : [...current, parameter.id])} /><span>{parameterName(parameter)}</span></label>)}</div>
                  <details className="pm-details" style={{ marginTop: 12 }}><summary>兼容回退参数</summary><p className="pm-hint">主校准数据缺失或超出实测适用域时才使用。</p><div className="pm-choice-list" style={{ marginTop: 8 }}>{catalog.parameter_catalog.filter(parameter => fallbackCalibrationGroupIds.includes(parameter.id)).map(parameter => <label className="pm-choice" key={parameter.id}><input type="checkbox" checked={groups.includes(parameter.id)} onChange={() => setGroups(current => current.includes(parameter.id) ? current.filter(id => id !== parameter.id) : [...current, parameter.id])} /><span>{parameterName(parameter)}</span></label>)}</div></details>
                  {groups.includes('pairwise_interference') && <div className="ci-plan-controls"><h4>共跑组合适用域</h4><div className="pm-fields">
                    <label className="pm-field"><span>目标任务核组</span><select value={victimKind} onChange={event => { setVictimKind(event.target.value); setVictimFrequency(0); if (event.target.value === 'big' && peerKind === 'big') setPeerKind('middle') }}>{coreGroups.map(group => <option key={group.id} value={group.id}>{group.name}</option>)}</select></label>
                    <label className="pm-field"><span>干扰任务核组</span><select value={peerKind} onChange={event => { setPeerKind(event.target.value); setPeerFrequency(0) }}>{coreGroups.map(group => <option key={group.id} value={group.id} disabled={victimKind === group.id && group.physical_core_count < 2}>{group.name}</option>)}</select></label>
                    <label className="pm-field"><span>目标频率（kHz）</span><select value={victimFrequency || victimCore?.max_khz || ''} onChange={event => setVictimFrequency(Number(event.target.value))}>{victimCore?.opps_khz.map(value => <option key={value} value={value}>{value}</option>)}</select></label>
                    <label className="pm-field"><span>干扰频率（kHz）</span><select disabled={victimKind === peerKind} value={victimKind === peerKind ? (victimFrequency || victimCore?.max_khz || '') : (peerFrequency || peerCore?.max_khz || '')} onChange={event => setPeerFrequency(Number(event.target.value))}>{peerCore?.opps_khz.map(value => <option key={value} value={value}>{value}</option>)}</select></label>
                    <label className="pm-field full"><span>每线程工作集（MiB）</span><select value={workingSetMiB} onChange={event => setWorkingSetMiB(Number(event.target.value))}>{[8, 32, 64, 128, 256].map(value => <option key={value} value={value}>{value} MiB</option>)}</select></label>
                  </div></div>}
                  <div className="pm-actions"><button className="quiet-button" disabled={Boolean(busy) || !groups.length || !name.trim() || !hardwareId || repeats < 3 || repeats > 30} onClick={() => void createPlan()}>{busy === 'plan' ? <LoaderCircle size={14} className="spin" /> : <FlaskConical size={14} />}仅生成并保存方案</button></div>
                </div>
                <div className="pm-box"><h4>{plan ? plan.name : '离线校准包'}</h4><p className="pm-hint">用于无法由桌面工作台直接控制的设备或既有自动化实验室。校准包包含受管源码、条件清单与测量契约；通常无需使用此入口。</p>
                  {plan && <div className="cs-result-line"><span className="pm-badge">{plan.conditions?.length ?? plan.condition_count ?? '—'} 个实验条件</span><span className="pm-badge">训练 {plan.repeats} 次重复 · 独立留出验证</span></div>}
                  {plan && !bundle && <div className="pm-actions"><button className="quiet-button" disabled={Boolean(busy)} onClick={() => void loadBundle()}><ArrowDownToLine size={14} />获取离线校准包</button></div>}
                  {bundle && <div className="pm-choice-list" style={{ marginTop: 12 }}>{bundle.files.map(file => <button className="quiet-button" key={file.filename} onClick={() => downloadText(file.filename, file.content, file.media_type)}><ArrowDownToLine size={13} />{file.filename}</button>)}</div>}
                </div>
              </div>
              {plans.length > 0 && <><h4 style={{ marginTop: 18 }}>已保存方案</h4><div className="cs-history-grid">{plans.map(item => <button disabled={Boolean(busy)} className={plan?.id === item.id ? 'selected' : ''} key={item.id} onClick={() => void loadPlan(item.id)}><strong>{item.name}</strong><small>{timestamp(item.created_at)} · {item.conditions?.length ?? item.condition_count ?? '—'} 个条件</small></button>)}</div></>}
            </details>
          </>}

          {tab === 'fit' && <>
            {fit ? <div className="pm-box">
              <div className="pm-section-heading"><div><FlaskConical size={17} /><h3>拟合与留出验证结果</h3></div><span className={'pm-badge ' + (fit.quality.passed ? 'good' : 'warn')}>{fit.quality.passed ? '质量检查通过' : fitCanPublish(fit) ? '可发布 · 有风险' : fit.status === 'needs_data' ? '需要补充数据' : '不可发布'}</span></div>
              <div className="cs-summary"><div><strong>{fit.quality.accepted_samples}</strong><span>可用样本</span></div><div><strong>{fit.quality.validation_samples}</strong><span>留出验证样本</span></div><div><strong>{fit.quality.validation_median_ape == null ? '—' : metric(fit.quality.validation_median_ape * 100, 1) + '%'}</strong><span>验证误差中位数</span></div><div><strong>{fit.quality.validation_p90_ape == null ? '—' : metric(fit.quality.validation_p90_ape * 100, 1) + '%'}</strong><span>验证误差 P90</span></div></div>
              <div className={'pm-note ' + (fit.quality.passed ? '' : 'warning')}><CircleAlert size={14} /><span><strong>总体风险 {fit.quality.risk_level ?? (fit.quality.passed ? 'low' : 'high')} · 证据等级 {fit.quality.evidence_grade ?? '—'}</strong><br />{fit.quality.risk_summary ?? (fit.quality.passed ? '全部质量门通过。' : '请根据失败质量门判断适用域。')}</span></div>
              {Array.isArray(fit.quality.failed_gates) && fit.quality.failed_gates.length > 0 && <details className="pm-details"><summary>质量门失败项（{fit.quality.failed_gates.length}）</summary><ul className="pm-hint">{fit.quality.failed_gates.map((gate, index) => <li key={index}>{gate.message ?? JSON.stringify(gate)}</li>)}</ul></details>}
              {[...fit.quality.errors, ...fit.quality.warnings].map((item, index) => <p className="pm-hint" key={index} style={{ marginBottom: 7 }}>{item}</p>)}
              <div className="pm-table-scroll"><table className="pm-table cs-fit-table"><thead><tr><th>参数</th><th>估计值</th><th>方法</th><th>样本</th></tr></thead><tbody>{fit.parameter_estimates.map((parameter, index) => <tr key={parameter.path + '-' + index}><td>{parameter.path || parameter.parameter}<small>{parameterStatus(parameter.status)}</small></td><td>{formatValue(parameter.value)}</td><td>{parameter.method}</td><td>{parameter.sample_count}</td></tr>)}</tbody></table></div>
              {fitCanPublish(fit) && !versions.some(version => version.fit_id === fit.id) && <div className="pm-fields" style={{ marginTop: 14 }}><label className="pm-field"><span>模型版本名称（可选）</span><input value={versionName} onChange={event => setVersionName(event.target.value)} placeholder="设备 / 场景 / 校准日期" /></label><div className="pm-actions"><button ref={publishAction} className="primary-button pm-step-action" disabled={Boolean(busy)} onClick={() => void publish()}><Save size={14} />{fit.quality.passed ? '保存为寻优模型' : '带风险保存为寻优模型'}</button></div></div>}
              {versions.some(version => version.fit_id === fit.id) && <div className="pm-note" style={{ marginTop: 14 }}><Check size={14} /><span>该拟合结果已发布为硬件绑定模型，可直接用于并行建模。</span></div>}
              <InterferenceCalibrationTable model={fit.model_preview} /><details className="pm-details"><summary>完整拟合记录</summary><pre className="pm-code">{JSON.stringify(fit, null, 2)}</pre></details>
            </div> : <div className="pm-empty"><FlaskConical size={27} />尚无拟合结果。返回“真机校准”并执行一次自动校准。</div>}

            <details className="pm-details cs-manual-import"><summary>高级 / 兼容：导入已有微基准测量 JSON</summary>
              <label className="pm-field" style={{ marginTop: 14 }}><span>对应校准方案</span><select value={plan?.id ?? ''} disabled={Boolean(busy)} onChange={event => void loadPlan(event.target.value)}><option value="" disabled>请选择已保存的校准方案</option>{plans.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
              {!measurementImportAvailable && <div className="pm-note warning" style={{ marginTop: 14 }}><CircleAlert size={15} /><span>当前环境不支持手工文件导入。请使用桌面工作台自动真机校准。</span></div>}
              {measurementImportAvailable && <div className="pm-grid equal" style={{ marginTop: 14 }}><div><label className="pm-field cs-file-input"><span>选择测量 JSON</span><small>兼容自动化实验室或旧版本导出的 provenance + samples 契约。通道上限 {formatBytes(measurementByteLimit)}。</small><input type="file" accept=".json,application/json" disabled={!plan || Boolean(busy)} onChange={event => void readMeasurements(event.target.files?.[0])} /></label>{measurementName && <p className="pm-hint" style={{ marginTop: 8 }}>{measurementName}</p>}<div className="pm-actions"><button ref={fitAction} className="primary-button pm-step-action" disabled={!plan || !measurementText || !measurementFile || Boolean(busy)} onClick={() => void fitMeasurements()}>{busy === 'fit' ? <LoaderCircle size={14} className="spin" /> : <Upload size={14} />}拟合并检查质量</button></div></div><div className="pm-box"><h4>导入质量门禁</h4><p className="pm-hint">核绑定与实测频率必须符合条件；CPU 时间对应相同工作量；重复测量稳定；训练与留出样本覆盖所选参数。未测参数沿用原模型并明确记录来源。</p><details className="pm-details"><summary>查看测量数据字段</summary><pre className="pm-code">{JSON.stringify(catalog.measurement_contract, null, 2)}</pre></details></div></div>}
            </details>
            {fits.length > 0 && <><h4 style={{ marginTop: 18 }}>历史拟合</h4><div className="cs-history-grid">{fits.map(item => <button key={item.id} disabled={Boolean(busy)} className={fit?.id === item.id ? 'selected' : ''} onClick={() => void loadFit(item.id)}><strong>{plans.find(value => value.id === item.plan_id)?.name ?? item.plan_id}</strong><small>{item.quality?.passed ? '检查通过' : fitCanPublish(item) ? `可发布 · ${item.quality.risk_level ?? '有风险'}` : '不可发布'} · {timestamp(item.created_at)}</small></button>)}</div></>}
          </>}

          {tab === 'versions' && <>{versions.length ? <div className="pm-choice-list">{versions.map(version => <article className="pm-box" key={version.id}><div className="pm-section-heading"><div><Check size={16} /><h3>{version.name}</h3></div><div className="pm-inline"><span className={'pm-badge ' + (version.status === 'risk_calibrated' ? 'warn' : 'good')}>{version.status === 'risk_calibrated' ? '风险校准模型' : version.status === 'partially_calibrated' ? '部分参数已校准' : '真机代理负载已校准'}</span>{version.display_source === 'bundled_offline_calibration' && <span className="pm-badge">{version.display_source_label || '随包校准'}</span>}</div></div><p className="pm-hint">{timestamp(version.created_at)} · {version.version}</p><p className="pm-hint" style={{ marginTop: 8 }}>已校准参数：{formatValue(version.calibrated_parameters)}</p><p className="pm-hint">继承参数：{formatValue(version.inherited_parameters)}</p><InterferenceCalibrationTable model={version.model} /><div className={'pm-note ' + (version.status === 'risk_calibrated' ? 'warning' : '')} style={{ marginTop: 10 }}><CircleAlert size={14} /><span>{version.status === 'risk_calibrated' ? <><strong>总体风险 {version.risk_level ?? 'high'} · 证据等级 {version.evidence_grade ?? '—'}</strong><br />{version.risk_summary ?? '部分质量门未通过；请查看版本失败项并限制适用域。'}</> : <>此版本已通过微基准拟合检查。应用并行收益仍是条件预测，需结合目标场景验证适用性。</>}</span></div><div className="pm-actions"><button className="primary-button" onClick={() => onNavigateModeling(version.id)}>使用此模型寻优<ArrowRight size={14} /></button><button className="quiet-button" onClick={() => downloadText(version.id + '.json', JSON.stringify(version, null, 2), 'application/json')}><ArrowDownToLine size={14} />导出版本</button></div></article>)}</div> : <div className="pm-empty"><FlaskConical size={27} />暂无校准版本。完成真机校准与留出验证后，平台会生成硬件绑定模型。</div>}</>}
        </section>
        <CalibrationFrequencyCurveGallery selectedFit={fit} fits={fits} versions={versions} />
      </>}
  </div>
}
