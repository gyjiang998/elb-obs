'use client'

import { Activity, ArrowDownToLine, ArrowRight, BarChart3, CircleAlert, Cpu, Database, FileJson, GitBranch, Layers, LoaderCircle, RefreshCw, Search, Sparkles, X } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { platformApiFor, platformTextApiFor } from '../lib/api'
import { agentAutonomousTaskStatusEventName } from '../lib/agent-command-bridge'
import { dispatchParallelAgent, downloadText, errorText, metric, numeric, record, text, timestamp, workloadCaptureKey, workloadCaptureLabel, workloadDatasetLabel, type ModelingCatalog, type ModelingDataset, type Notice } from '../lib/parallel-modeling'
import type { JsonRecord } from '../lib/types'
import { focusWorkflowAction } from '../lib/workflow-focus'
import { stageMetric, stageTimeSegments, stageTiming, workloadMetrics } from '../lib/workload-feature-display'
import { useWorkbenchErrorNotification } from './WorkbenchErrorNotification'
import '../parallel-modeling.css'
import '../workload-features.css'

type FeatureClass = 'compute' | 'memory' | 'mixed' | 'service' | 'scheduler' | 'unknown'
type FeatureClassification = { id: FeatureClass; name: string; evidence: string[]; parallel_implication: string; evidence_quality?: string
  confidence?: { level?: string; score?: number }; available_metrics?: string[]; missing_evidence?: string[]; cpu_candidate?: FeatureClassification }
type Stage = {
  id: string; name: string; display_id: string; start_ns: number; end_ns: number; relative_start_ms: number
  wall_ms: number; cpu_ms: number; states_ms: Record<string, number>; compute: JsonRecord; memory: JsonRecord; scheduling: JsonRecord
  classification: FeatureClassification
  workload_class: string; quality: { pmu_usable: boolean; pmu_source: string; pmu_scope: string | null; missing_counters: string[]; reasons: string[] }
  evidence: JsonRecord; time_breakdown?: JsonRecord; metric_evidence?: JsonRecord
}
type ProfileSummary = { id: string; name: string; created_at: string; dataset_id: string; hardware_profile_id: string | null; summary: JsonRecord; quality: JsonRecord }
type FeatureProfile = ProfileSummary & { schema_version?: string; engine_version?: string; hardware_profile_sha256?: string | null; stages: Stage[]; dataset_fingerprint: string; profile_sha256: string; component_classes: Record<string, string>; provenance: JsonRecord }
type Catalog = { classes: { id: FeatureClass; name: string }[]; classification_rules: JsonRecord; measurement_contract: JsonRecord; notes: string[] }
type ArchivedBuiltin = { id: string; name: string; description?: string }
type HardwareProfileSummary = ModelingCatalog['hardware_profiles'][number]
type AgentWatch = { requestId: string; datasetId: string; initialProfileId: string | null; knownProfileIds: Set<string>; startedAt: number; accepted: boolean; pendingNoticeShown?: boolean; resultId?: string }
export type WorkloadCaptureSource = { traceId: string; runId: string; stepId: string; title: string; planTitle?: string; stepOrdinal?: number | null; stepTitle: string; status: string; registeredAt?: string; ready: boolean; reason?: string }

const api = platformApiFor('workload-features')
const apiText = platformTextApiFor('workload-features')

function range(value: unknown, bounds: unknown, digits = 2): string {
  if (numeric(value) !== null) return metric(value, digits)
  if (Array.isArray(bounds) && bounds.length === 2 && bounds.every(item => numeric(item) !== null)) return `${metric(bounds[0], digits)}–${metric(bounds[1], digits)}`
  return '未采集'
}
function MetricRow({ name, value, note }: { name: string; value: ReactNode; note?: string }) {
  return <div className="wf-metric-row"><span>{name}{note && <small>{note}</small>}</span><strong>{value}</strong></div>
}

function runningClassification(stage: Stage) {
  return stage.classification.cpu_candidate ?? stage.classification
}

function classificationSummary(classification: FeatureClassification) {
  const level = classification.confidence?.level
  const quality = classification.evidence_quality
  const levelLabel = ({ high: '高', medium: '中', low: '低', none: '无' } as Record<string, string>)[level ?? ''] ?? '未记录'
  const qualityLabel = ({ complete: '完整证据', partial: '部分证据', minimal: '最小证据', none: '无有效证据' } as Record<string, string>)[quality ?? ''] ?? '历史规则'
  return `候选置信度 ${levelLabel} · ${qualityLabel}`
}

function PmuValue({ stage, metricKey }: { stage: Stage; metricKey: string }) {
  const item = stageMetric(stage, metricKey)
  const weighted = item.estimateScope === 'covered_stage_window'
  const mappingNote = item.mappingVerified === false && metricKey.startsWith('llc_') ? <small className="wf-counter-range">硬件映射待核验</small> : null
  const bounds = item.bounds ? `${metric(item.bounds[0], item.digits)}–${metric(item.bounds[1], item.digits)}` : ''
  const title = [item.label,
    item.boundaryUncertain ? '采样读取窗口跨越阶段边界，无法精确归属到本阶段；下界为 0 不表示阶段没有执行工作。' : '',
    item.estimate !== null ? weighted ? '按相邻读取中点确定采样窗口，计数增量按阶段与窗口的墙钟重叠比例分配；假设窗口内均匀分布，并非按 Running 时间精确归属。复用先按同组 enabled/running 校正；未覆盖时间不外推。' : '仅阶段内部完整采样窗口的估计，不是全阶段计数。' : '',
    bounds ? `阶段保守范围 ${bounds}` : '',
    item.timeCoverage !== null ? `${weighted ? '有效窗口' : '内部时间'}覆盖 ${metric(item.timeCoverage * 100, 1)}%` : '',
    item.runtimeCoverage !== null ? `计数器运行覆盖 ${metric(item.runtimeCoverage * 100, 1)}%` : '',
    item.status !== 'exact' && item.maxBoundarySpanUs !== null ? `跨边界读取窗口最大 ${metric(item.maxBoundarySpanUs / 1000)} ms` : '',
    item.mappingVerified === false && metricKey.startsWith('llc_') ? 'LLC 事件名称已记录，当前硬件层级映射待核验，不直接推断 DRAM 压力。' : '',
    item.scope,
  ].filter(Boolean).join('；')
  if (item.status === 'exact' && item.exact !== null) return <span className="wf-counter" title={title}>{metric(item.exact, item.digits)}{mappingNote}</span>
  if (item.estimate !== null) return <span className="wf-counter" title={title}><span className="wf-counter-status wf-counter-estimate">{weighted ? '估算' : '内部估计'}</span><span>≈ {metric(item.estimate, item.digits)}</span>{!weighted ? <small className="wf-counter-range">仅覆盖的内部窗口</small> : item.timeCoverage !== null && item.timeCoverage < .999999 ? <small className="wf-counter-range">仅覆盖 {metric(item.timeCoverage * 100, 1)}%</small> : null}{mappingNote}</span>
  if (item.bounds || item.boundaryUncertain) return <span className="wf-counter" title={title}><span className="wf-counter-status">{item.boundaryUncertain ? '边界不确定' : '区间'}</span><small className="wf-counter-range">{bounds ? `范围 ${bounds}` : ['ipc', 'cache_mpki', 'llc_mpki', 'branch_mpki', 'l1d_mpki', 'dtlb_mpki'].includes(metricKey) ? '缺少可确定的比率范围' : '缺少可归属的阶段计数'}</small>{mappingNote}</span>
  return <span className="wf-counter-missing" title={title}>{item.hasEvidence ? '不可用 / 未采集' : '未采集'}</span>
}

function PmuQuality({ stage }: { stage: Stage }) {
  const items = workloadMetrics.map(item => stageMetric(stage, item.key)).filter(item => item.hasEvidence || item.status !== 'unavailable' || item.boundaryUncertain)
  const primary = items.find(item => item.key === 'instructions') ?? items[0]
  const weighted = primary?.estimateScope === 'covered_stage_window'
  return <div className="wf-boundary-note" aria-label="PMU 边界与精度">
    <h4>{weighted ? '采样窗口加权估算 · 保留边界与精度' : primary?.boundaryUncertain ? '边界不确定：当前采样无法精确归属阶段计数' : 'PMU 覆盖与采样精度'}</h4>
    {primary?.status === 'exact' ? <p>阶段耗时 <strong>{metric(stage.wall_ms)} ms</strong>；使用阶段对齐计数{primary.timeCoverage !== null && <>，时间覆盖 <strong>{metric(primary.timeCoverage * 100, 1)}%</strong></>}。</p> : primary && <p>阶段耗时 <strong>{metric(stage.wall_ms)} ms</strong>{primary.maxBoundarySpanUs !== null && <>；跨边界读取窗口最大 <strong>{metric(primary.maxBoundarySpanUs / 1000)} ms</strong></>}{primary.interiorBins !== null && <>；阶段内完整采样区间 <strong>{metric(primary.interiorBins, 0)}</strong> 个</>}{primary.timeCoverage !== null && <>；{weighted ? '有效窗口' : '内部时间'}覆盖 <strong>{metric(primary.timeCoverage * 100, 1)}%</strong></>}。</p>}
    {primary?.status !== 'exact' && (weighted ? <p>主数值按采样窗口与阶段的墙钟时间重叠比例估算：计数增量 × 重叠时长 / 窗口时长。每次读取取起止中点，相邻中点组成窗口；复用计数先按同组 enabled/running 校正。IPC / MPKI 使用同组、同一批窗口的加权计数。</p> : <p>内部估计仅代表已覆盖的完整内部窗口；历史版本若只有区间，保留原值，可点击“提取负载特征”从留存的原始采样重新计算。</p>)}
    {weighted && <p>该方法假设窗口内计数均匀分布，不是按 Running 时间精确归属；100% 窗口覆盖也不等于精确测量。未覆盖时段不外推；估值可用于带置信度的降级候选，不会升级为校准实测或已证实的硬件瓶颈。</p>}
    {primary?.boundaryUncertain && <p>原始范围表示无法精确归属的计数边界，下界为 0 不表示没有执行指令。目标采样间隔不等于实际精度，读取窗口过长会使估算更依赖均匀分布假设。</p>}
    <p>复用后的已观测贡献不作为全阶段上界。通用 Cache 事件与 LLC 事件分别解释。</p>
    {items.length > 0 && <details className="pm-details"><summary>逐指标覆盖、精度与来源</summary><div className="pm-table-scroll"><table className="pm-table wf-quality-table" aria-label="PMU 指标证据表"><thead><tr><th>指标 / 采集组</th><th>数据状态与原始边界</th><th>有效时间 / 计数器运行覆盖</th><th>采样边界</th></tr></thead><tbody>{items.map(item => <tr key={item.key}><td>{item.label}<small>{item.group || '原记录未分组'} · {item.scope || '作用域未记录'}</small></td><td><PmuValue stage={stage} metricKey={item.key} />{item.bounds && item.status !== 'exact' && <small>阶段保守范围：{metric(item.bounds[0], item.digits)}–{metric(item.bounds[1], item.digits)}</small>}{item.observed && <small>已观测贡献：{metric(item.observed[0], item.digits)}–{metric(item.observed[1], item.digits)}<br />不作为全阶段上下界</small>}</td><td>{item.timeCoverage === null ? '未记录' : `${metric(item.timeCoverage * 100, 1)}%`} / {item.runtimeCoverage === null ? '未记录' : `${metric(item.runtimeCoverage * 100, 1)}%`}<small>{item.status === 'exact' ? '阶段对齐计数' : item.estimateScope === 'covered_stage_window' ? '墙钟重叠加权' : '仅内部完整窗口'}{item.estimateScope === 'covered_stage_window' && item.interiorTimeCoverage !== null && <> · 完整内部覆盖 {metric(item.interiorTimeCoverage * 100, 1)}%</>}</small></td><td>{item.status === 'exact' ? '阶段对齐计数，不依赖采样分桶' : <>内部 {item.interiorBins === null ? '未记录' : metric(item.interiorBins, 0)} 个{item.boundaryBins !== null && <> · 跨边界 {metric(item.boundaryBins, 0)} 个</>}<small>最大读取跨度 {item.maxBoundarySpanUs === null ? '未记录' : `${metric(item.maxBoundarySpanUs / 1000)} ms`}</small></>}</td></tr>)}</tbody></table></div></details>}
  </div>
}

function RunningMetrics({ stage }: { stage: Stage }) {
  const sections = [
    { id: 'compute', name: '计算', keys: ['instructions', 'cycles', 'ipc'], extra: [] },
    { id: 'branch', name: '分支', keys: ['branch_instructions', 'branch_misses', 'branch_mpki'], extra: [] },
    { id: 'cache', name: '缓存与 TLB', keys: ['cache_mpki', 'llc_mpki', 'l1d_mpki', 'dtlb_mpki'], extra: ['cache_references', 'cache_misses', 'llc_accesses', 'llc_misses', 'l1d_accesses', 'l1d_misses', 'dtlb_accesses', 'dtlb_misses'] },
    { id: 'pipeline', name: '流水线与访存', keys: ['stalled_frontend', 'stalled_backend', 'memory_stall_cycles'], extra: ['loads', 'stores', 'dram_bytes'] },
  ]
  const row = (key: string) => {
    const item = workloadMetrics.find(definition => definition.key === key)
    return item ? <MetricRow key={key} name={item.label} note={'note' in item ? item.note : undefined} value={<PmuValue stage={stage} metricKey={key} />} /> : null
  }
  return <div className="wf-domain-grid wf-pmu-grid">{sections.map(section => <article key={section.id} aria-label={`${section.name}特征`}><h4>{section.id === 'cache' ? <Layers size={16} /> : <Cpu size={16} />}{section.name}</h4>{section.keys.map(row)}{section.id === 'compute' && numeric(stage.compute.interior_ipc) !== null && <MetricRow name="内部窗口 IPC" note="仅完全落在阶段内的采样区间，不替代全阶段 IPC" value={metric(stage.compute.interior_ipc)} />}{section.extra.length > 0 && <details className="pm-details"><summary>{section.id === 'cache' ? '查看各级缓存 / TLB 事件计数' : '查看 Load / Store / DRAM 数据'}</summary>{section.extra.map(row)}</details>}</article>)}</div>
}
function StateBar({ stage }: { stage: Stage }) {
  const states = stageTimeSegments(stage)
  return <div className="wf-state-bar" aria-label={states.map(item => `${item.label} ${metric(item.value)} ms`).join('，')}>{states.filter(item => item.value > 0).map(item => <span key={item.id} className={item.id} style={{ width: `${stage.wall_ms > 0 ? 100 * item.value / stage.wall_ms : 0}%` }} title={`${item.label} ${metric(item.value)} ms`} />)}</div>
}

type CpuResidence = { cpu: number; cpuMs: number; groupId: string; groupName: string; proxyKind: string; frequencies: number[]; frequencyUnknown: boolean }
type CpuResidencyEvidence = { status: 'complete' | 'partial' | 'insufficient' | 'unavailable'; observed: number; expected: number | null; ratio: number | null; sourceAnchorEligible: boolean; legacyDerived: boolean }

function cpuResidence(stage: Stage): CpuResidence[] {
  const values = Array.isArray(stage.scheduling.cpu_residency) ? stage.scheduling.cpu_residency : []
  const byCpu = new Map<number, CpuResidence>()
  values.forEach(value => {
    const item = record(value)
    const cpu = numeric(item.cpu)
    const cpuMs = numeric(item.cpu_ms)
    if (cpu === null || cpuMs === null) return
    const current = byCpu.get(cpu) ?? { cpu, cpuMs: 0, groupId: text(item.core_group_id), groupName: text(item.core_group_name), proxyKind: text(item.proxy_kind), frequencies: [], frequencyUnknown: false }
    current.cpuMs += cpuMs
    if (!current.groupId) current.groupId = text(item.core_group_id)
    if (!current.groupName) current.groupName = text(item.core_group_name)
    if (!current.proxyKind) current.proxyKind = text(item.proxy_kind)
    const frequency = numeric(item.frequency_khz)
    if (frequency !== null && frequency > 0 && !current.frequencies.includes(frequency)) current.frequencies.push(frequency)
    else if (frequency === null || frequency <= 0) current.frequencyUnknown = true
    byCpu.set(cpu, current)
  })
  return [...byCpu.values()].sort((left, right) => right.cpuMs - left.cpuMs || left.cpu - right.cpu)
}

function frequencyResidence(values: number[], frequencyUnknown = false) {
  if (!values.length) return '频率未记录'
  const frequencies = [...values].sort((left, right) => left - right).map(value => value / 1000)
  const label = frequencies.length === 1 ? `${metric(frequencies[0], 0)} MHz` : `${metric(frequencies[0], 0)}–${metric(frequencies.at(-1), 0)} MHz`
  return frequencyUnknown ? `${label} · 部分频率未记录` : label
}

function cpuResidencyEvidence(stage: Stage, rows: CpuResidence[]): CpuResidencyEvidence {
  const placement = record(stage.scheduling.cpu_placement)
  const declared = record(placement.residency_evidence ?? stage.scheduling.cpu_residency_evidence)
  const observedFromRows = rows.reduce((total, row) => total + row.cpuMs, 0)
  const observed = numeric(declared.observed_on_cpu_ms) ?? numeric(placement.observed_on_cpu_ms) ?? observedFromRows
  const expected = numeric(declared.expected_on_cpu_ms) ?? numeric(placement.expected_on_cpu_ms) ?? numeric(stage.cpu_ms)
  const ratio = numeric(declared.coverage_ratio) ?? numeric(declared.coverage) ?? numeric(placement.coverage_ratio)
    ?? (expected !== null && expected > 0 ? observed / expected : null)
  const declaredStatus = text(declared.status || placement.coverage_status)
  const legacyConserved = !declaredStatus && expected !== null && expected > 0
    && Math.abs(observed - expected) <= Math.max(.001, expected * .001)
  const status = (['complete', 'partial', 'insufficient', 'unavailable'].includes(declaredStatus)
    ? declaredStatus
    : expected === null || expected <= 0 || observed <= 0 ? 'unavailable'
    : ratio !== null && ratio > 1.05 ? 'insufficient'
    : legacyConserved ? 'complete'
    : ratio !== null && ratio >= .70 ? 'partial'
    : 'insufficient') as CpuResidencyEvidence['status']
  return { status, observed, expected, ratio,
    sourceAnchorEligible: declared.source_anchor_eligible === true || placement.source_anchor_eligible === true
      || (!declaredStatus && status === 'complete'),
    legacyDerived: !declaredStatus }
}

function sourceAnchorProfileEligible(profile: FeatureProfile) {
  return profile.schema_version === 'workload_feature_profile/v2'
    && profile.engine_version === '1.6.0'
    && Boolean(profile.hardware_profile_sha256?.trim())
}

function CpuPlacement({ stage, detailed = false, hardwareVerified = false, profileContractEligible = false }: { stage: Stage; detailed?: boolean; hardwareVerified?: boolean; profileContractEligible?: boolean }) {
  const rows = cpuResidence(stage)
  const coverage = cpuResidencyEvidence(stage, rows)
  if (!rows.length) return <span className="wf-cpu-placement missing">CPU 未记录 · 驻留证据不可用</span>
  const primary = rows[0]
  const groups = new Set(rows.map(row => row.groupId).filter(Boolean))
  const migrations = numeric(stage.scheduling.cpu_migrations)
  const coverageLabel = coverage.ratio === null ? '覆盖率未知' : `覆盖 ${metric(coverage.ratio * 100, 1)}%（${metric(coverage.observed)} / ${coverage.expected === null ? '未知' : metric(coverage.expected)} ms）`
  const title = `${rows.map(row => `CPU ${row.cpu} · ${row.groupName || '核型未知'} · ${metric(row.cpuMs)} ms · ${frequencyResidence(row.frequencies, row.frequencyUnknown)}`).join('；')}；${coverageLabel}`
  const placementNote = groups.size > 1 ? `跨 ${groups.size} 个核组驻留` : migrations !== null && migrations > 0 ? `迁核 ${metric(migrations, 0)} 次` : '未观测到迁核'
  const groupsComplete = rows.every(row => Boolean(row.groupId))
  const frequenciesComplete = rows.every(row => !row.frequencyUnknown && row.frequencies.length > 0)
  const anchorEligible = profileContractEligible && coverage.status === 'complete' && coverage.sourceAnchorEligible && hardwareVerified && groupsComplete && frequenciesComplete
  const anchorGap = coverage.status !== 'complete' ? '驻留时长覆盖不完整'
    : !profileContractEligible ? '历史特征版本缺少 v2 硬件画像身份合同'
    : !hardwareVerified ? '硬件身份未核验'
    : !groupsComplete ? '部分逻辑 CPU 未映射核组'
    : !frequenciesComplete ? '部分驻留频率未记录'
    : !coverage.sourceAnchorEligible ? '源执行锚前置条件未满足'
    : ''
  const evidenceLabel = coverage.status === 'partial' ? '部分观测' : coverage.status === 'unavailable' ? '证据不可用' : !anchorEligible ? '证据不足' : ''
  return <span className={`wf-cpu-placement ${!anchorEligible ? 'wf-residency-incomplete' : ''}`} title={title}>
    <strong>{evidenceLabel && <>{evidenceLabel} · </>}CPU {primary.cpu} · {primary.groupName ? `${primary.groupName}${hardwareVerified ? '' : '（用户绑定画像）'}` : '核型未知'}</strong>
    {primary.groupName && !hardwareVerified && <small className="wf-hardware-unverified">画像未核验</small>}
    <small>{anchorEligible ? '主驻留' : '已观测最长驻留'} {metric(primary.cpuMs)} ms · {frequencyResidence(primary.frequencies, primary.frequencyUnknown)}</small>
    {!anchorEligible && <small className="wf-residency-warning">{coverageLabel} · {anchorGap}，不作为确定源执行时间锚点</small>}
    {anchorEligible && <small>{coverageLabel}{coverage.legacyDerived ? ' · 历史版本按时长守恒兼容评估' : coverage.ratio !== null && Math.abs(coverage.ratio - 1) > .001 ? ' · 容差内外推，建模保留风险' : ''}</small>}
    {(detailed || rows.length > 1 || (migrations ?? 0) > 0) && <small>{placementNote}{rows.length > 1 ? ` · 共 ${rows.length} 个逻辑 CPU` : ''}</small>}
  </span>
}

function SchedulingBreakdown({ stage }: { stage: Stage }) {
  const timing = stageTiming(stage)
  return <div className="wf-scheduling" aria-label="阶段调度时间拆解">
    <h4><GitBranch size={16} />先拆调度时间<span>互斥时间合计 {metric(stage.wall_ms)} ms</span></h4>
    <StateBar stage={stage} />
    <div className="wf-scheduling-grid">
      <article className="wf-time-running"><h5>Running · 实际占 CPU</h5><strong>{metric(timing.running)} <small>ms</small></strong><p>下面的 PMU 用于刻画这部分运行工作。</p>{(timing.irq !== null || timing.softirq !== null) && <p className="wf-time-sub">{timing.irq !== null && <>已观测硬件 IRQ 重叠 {metric(timing.irq)} ms{!timing.irqComplete && '（证据不完整）'}<br /></>}{timing.softirq !== null && <>已观测 SoftIRQ 重叠 {metric(timing.softirq)} ms{!timing.softirqComplete && '（证据不完整）'}<br /></>}重叠注释，不另加到阶段耗时。</p>}</article>
      <article className="wf-time-runnable"><h5>Runnable · 等待 CPU</h5><strong>{metric(timing.runnable)} <small>ms</small></strong><div className="wf-time-sub"><span>其中 Interrupted · 抢占等待</span><b>{timing.preempted === null ? '未区分' : `${metric(timing.preempted)} ms`}</b><span>{timing.runnableOther === null ? '当前记录缺少可核验的抢占来源' : `其余等待 ${metric(timing.runnableOther)} ms`}</span></div></article>
      <article className="wf-time-blocked"><h5>Blocked · 阻塞</h5><strong>{metric(timing.blocked)} <small>ms</small></strong><div className="wf-time-sub"><span>已关联 Binder 同步阻塞</span><b>{timing.binder === null ? '未采集' : `${metric(timing.binder)} ms`}</b><span>Binder 属于 Blocked；其余原因由调用 / I/O 证据确认。</span></div></article>
      <article className="wf-time-unknown"><h5>状态未知</h5><strong>{metric(timing.unknown)} <small>ms</small></strong><p>调度记录缺口保留为未知。</p></article>
    </div>
    <p className="pm-hint">Interrupted 是 Runnable 的子集，不能与 Runnable 总等待重复相加；硬件 IRQ / SoftIRQ 不等同于线程被抢占。</p>
    {timing.notes.length > 0 && <details className="pm-details"><summary>调度状态证据</summary>{timing.notes.map((note, index) => <p className="pm-hint" key={index}>{note}</p>)}</details>}
  </div>
}

export function WorkloadFeaturesStudio({ initialDatasetId, initialFeatureProfileId, onAssistantContextChange, onNavigateModeling, onNavigateCapture, onNavigateCalibration, onNavigateHardwareModels, onCreateExample, onListCaptureSources, onPrepareCaptureDataset, assistantEnabled = true, measurementByteLimit = 10 * 1024 * 1024 }: {
  initialDatasetId?: string
  initialFeatureProfileId?: string
  onAssistantContextChange?: (context: JsonRecord | null) => void
  onNavigateModeling?: (datasetId?: string, featureProfileId?: string) => void
  onNavigateCapture?: () => void
  onNavigateCalibration?: () => void
  onNavigateHardwareModels?: () => void
  onCreateExample?: () => Promise<string | void>
  onListCaptureSources?: () => Promise<WorkloadCaptureSource[]>
  onPrepareCaptureDataset?: (source: WorkloadCaptureSource) => Promise<ModelingDataset>
  assistantEnabled?: boolean
  measurementByteLimit?: number
}) {
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [builtins, setBuiltins] = useState<ArchivedBuiltin[]>([])
  const [hardwareProfiles, setHardwareProfiles] = useState<HardwareProfileSummary[]>([])
  const [datasets, setDatasets] = useState<ModelingDataset[]>([])
  const [profiles, setProfiles] = useState<ProfileSummary[]>([])
  const [captureSources, setCaptureSources] = useState<WorkloadCaptureSource[]>([])
  const [captureSourceId, setCaptureSourceId] = useState('')
  const [builtinId, setBuiltinId] = useState('')
  const [datasetId, setDatasetId] = useState(initialDatasetId ?? '')
  const [bindingHardwareId, setBindingHardwareId] = useState('')
  const [profile, setProfile] = useState<FeatureProfile | null>(null)
  const [stageId, setStageId] = useState('')
  const [busy, setBusy] = useState('')
  const [exporting, setExporting] = useState<'stages' | 'pmu' | ''>('')
  const [loaded, setLoaded] = useState(false)
  const [notice, setNotice] = useState<Notice | null>(null)
  const [query, setQuery] = useState('')
  const [classFilter, setClassFilter] = useState('all')
  const [sort, setSort] = useState('order')
  const [measurement, setMeasurement] = useState<JsonRecord | null>(null)
  const [measurementName, setMeasurementName] = useState('')
  const agentRequest = useRef('')
  const [agentPending, setAgentPending] = useState(false)
  const [agentWatching, setAgentWatching] = useState(false)
  const agentWatch = useRef<AgentWatch | null>(null)
  const agentGeneration = useRef(0)
  const agentPollInFlight = useRef(false)
  const agentAbort = useRef<AbortController | null>(null)
  const analyzeAction = useRef<HTMLButtonElement | null>(null)
  const modelingAction = useRef<HTMLButtonElement | null>(null)
  const latest = useRef({ datasetId, profileId: profile?.id ?? null, busy })
  latest.current = { datasetId, profileId: profile?.id ?? null, busy }
  const dataset = datasets.find(item => item.id === datasetId)
  const hardwareBindingDirty = Boolean(dataset && bindingHardwareId && bindingHardwareId !== dataset.hardware_profile_id)
  const archivedCaptureKeys = useMemo(() => new Set(datasets.filter(item => Boolean(item.hardware_profile_id)).map(item => workloadCaptureKey(record(item.provenance).capture as JsonRecord)).filter(Boolean)), [datasets])
  const unboundArchivedCaptureKeys = useMemo(() => new Set(datasets.filter(item => !item.hardware_profile_id).map(item => workloadCaptureKey(record(item.provenance).capture as JsonRecord)).filter(Boolean)), [datasets])
  const materializedBuiltinIds = useMemo(() => new Set(datasets.map(item => text(record(item.provenance).builtin_id)).filter(Boolean)), [datasets])
  const localCaptureSources = useMemo(() => captureSources.filter(item => !archivedCaptureKeys.has(workloadCaptureKey(item))), [captureSources, archivedCaptureKeys])
  const archivedBuiltins = useMemo(() => builtins.filter(item => !materializedBuiltinIds.has(item.id)), [builtins, materializedBuiltinIds])
  const selectedCaptureSource = captureSources.find(item => `${item.traceId}:${item.stepId}` === captureSourceId)
  const selectedCaptureNeedsReimport = Boolean(selectedCaptureSource && unboundArchivedCaptureKeys.has(workloadCaptureKey(selectedCaptureSource)))
  const selectedWorkload = datasetId ? `dataset:${datasetId}` : captureSourceId ? `capture:${captureSourceId}` : builtinId ? `builtin:${builtinId}` : ''
  const hasWorkloads = datasets.length > 0 || localCaptureSources.length > 0 || archivedBuiltins.length > 0
  const selected = profile?.stages.find(item => item.id === stageId) ?? profile?.stages[0]
  const selectedRunning = selected ? runningClassification(selected) : null
  const context = useMemo(() => ({ page_id: 'workload_features', dataset_id: datasetId || null, feature_profile_id: profile?.id ?? null, selected_stage_id: selected?.id ?? null }), [datasetId, profile?.id, selected?.id])
  const visibleStages = useMemo(() => (profile?.stages ?? []).filter(item => {
    const running = runningClassification(item)
    return (classFilter === 'all' || item.classification.id === classFilter || running.id === classFilter)
      && `${item.name} ${item.id} ${item.display_id}`.toLowerCase().includes(query.trim().toLowerCase())
  }).sort((a, b) => sort === 'wall' ? b.wall_ms - a.wall_ms : sort === 'cpu' ? b.cpu_ms - a.cpu_ms : a.start_ns - b.start_ns), [profile, query, classFilter, sort])
  const refresh = useCallback(async () => {
    setBusy('refresh')
    const results = await Promise.allSettled([api<Catalog>('/v1/workload-features/catalog'), api<{ datasets: ModelingDataset[] }>('/v1/parallel-modeling/datasets'), api<{ profiles: ProfileSummary[] }>('/v1/workload-features/profiles'), onListCaptureSources ? onListCaptureSources() : Promise.resolve([] as WorkloadCaptureSource[]), api<ModelingCatalog>('/v1/parallel-modeling/catalog')])
    if (results[0].status === 'fulfilled') setCatalog(results[0].value)
    if (results[1].status === 'fulfilled') setDatasets(results[1].value.datasets)
    if (results[2].status === 'fulfilled') setProfiles(results[2].value.profiles)
    if (results[3].status === 'fulfilled') {
      const sources = results[3].value
      setCaptureSources(sources)
      setCaptureSourceId(current => sources.some(item => `${item.traceId}:${item.stepId}` === current) ? current : '')
    }
    if (results[4].status === 'fulfilled') {
      const items = results[4].value.builtins
      setBuiltins(items)
      setHardwareProfiles(results[4].value.hardware_profiles ?? [])
      setBuiltinId(current => items.some(item => item.id === current) ? current : '')
    }
    const errors = results.filter((result): result is PromiseRejectedResult => result.status === 'rejected')
    if (errors.length) setNotice({ tone: 'error', text: [...new Set(errors.map(item => errorText(item.reason)))].join('；') })
    else setNotice(current => current?.tone === 'error' ? null : current)
    setBusy(''); setLoaded(true)
  }, [onListCaptureSources])
  const stopAgentWatch = useCallback(() => {
    agentGeneration.current++; agentWatch.current = null; agentRequest.current = ''
    agentAbort.current?.abort(); agentAbort.current = null
    setAgentPending(false); setAgentWatching(false)
  }, [])
  const loadProfile = useCallback(async (id: string) => {
    stopAgentWatch()
    if (!id) { setProfile(null); setStageId(''); return }
    setBusy('profile')
    try { const value = await api<FeatureProfile>(`/v1/workload-features/profiles/${encodeURIComponent(id)}`); setProfile(value); setStageId(value.stages[0]?.id ?? ''); setDatasetId(value.dataset_id); setCaptureSourceId(''); setBuiltinId(''); setMeasurement(null); setMeasurementName('') }
    catch (error) { setNotice({ tone: 'error', text: errorText(error) }) }
    finally { setBusy('') }
  }, [stopAgentWatch])
  const checkAgentResult = useCallback(async () => {
    const watch = agentWatch.current
    if (!watch || !watch.accepted) return
    if (latest.current.datasetId !== watch.datasetId || latest.current.profileId !== watch.initialProfileId) { stopAgentWatch(); return }
    if (Date.now() - watch.startedAt > 5 * 60 * 1000) {
      stopAgentWatch(); setNotice({ tone: 'info', text: '已停止自动等待。尚未发现新的特征版本，可在助手完成后刷新“已保存的特征版本”。' }); return
    }
    if (latest.current.busy || agentPollInFlight.current) return
    agentPollInFlight.current = true
    const controller = new AbortController(); agentAbort.current = controller
    const timeout = window.setTimeout(() => controller.abort(), 15000)
    try {
      const result = await api<{ profiles: ProfileSummary[] }>('/v1/workload-features/profiles', { signal: controller.signal })
      if (agentWatch.current !== watch || latest.current.datasetId !== watch.datasetId || latest.current.profileId !== watch.initialProfileId) return
      setProfiles(result.profiles)
      const candidate = result.profiles.find(item => item.dataset_id === watch.datasetId && (watch.resultId
        ? item.id === watch.resultId
        : !watch.knownProfileIds.has(item.id)))
      if (!candidate || latest.current.busy) return
      if (watch.knownProfileIds.has(candidate.id)) {
        stopAgentWatch(); setNotice({ tone: 'info', text: '助手已完成分析，结果复用了已有特征版本；当前页面选择保持不变。可在版本列表查看。' }); return
      }
      const value = await api<FeatureProfile>(`/v1/workload-features/profiles/${encodeURIComponent(candidate.id)}`, { signal: controller.signal })
      if (agentWatch.current !== watch || latest.current.busy || latest.current.datasetId !== watch.datasetId || latest.current.profileId !== watch.initialProfileId || value.dataset_id !== watch.datasetId) return
      stopAgentWatch(); setProfile(value)
      setStageId(current => value.stages.some(item => item.id === current) ? current : value.stages[0]?.id ?? '')
      setNotice({ tone: 'success', text: `已载入助手生成的特征版本，共 ${value.stages.length} 个阶段。` })
    } catch { /* Bounded polling retries temporary failures; no fabricated result is shown. */ }
    finally { window.clearTimeout(timeout); agentPollInFlight.current = false; if (agentAbort.current === controller) agentAbort.current = null }
  }, [stopAgentWatch])
  useEffect(() => { void refresh() }, [refresh])
  useEffect(() => {
    setBindingHardwareId(dataset?.hardware_profile_id ?? '')
  }, [dataset?.id, dataset?.hardware_profile_id])
  useEffect(() => { if (initialDatasetId) { stopAgentWatch(); setDatasetId(initialDatasetId); setCaptureSourceId(''); setBuiltinId(''); setProfile(null); setStageId(''); setMeasurement(null); setMeasurementName(''); void refresh() } }, [initialDatasetId, refresh, stopAgentWatch])
  useEffect(() => { if (initialFeatureProfileId) void loadProfile(initialFeatureProfileId) }, [initialFeatureProfileId, loadProfile])
  useEffect(() => { onAssistantContextChange?.(context) }, [context, onAssistantContextChange])
  useEffect(() => () => onAssistantContextChange?.(null), [onAssistantContextChange])
  useEffect(() => {
    const status = (event: Event) => {
      const detail = record((event as CustomEvent).detail)
      if (!agentRequest.current || text(detail.request_id) !== agentRequest.current) return
      if (['rejected', 'cancelled', 'stopped', 'failed'].includes(text(detail.status))) {
        stopAgentWatch()
        setNotice({ tone: 'error', text: text(detail.message, '助手请求未完成，已停止自动载入。') })
      } else if (detail.status === 'accepted') {
        setAgentPending(false)
        if (agentWatch.current) agentWatch.current.accepted = true
        setNotice({ tone: 'info', text: text(detail.message, '助手已接收负载特征分析请求，完成后自动载入新版本。') })
        void checkAgentResult()
      }
    }
    const updated = (event: Event) => {
      const detail = record((event as CustomEvent).detail)
      const result = record(detail.result)
      const watch = agentWatch.current
      if (watch && /workload_features[._]analyze$/.test(text(detail.action_id)) && result.dataset_id === watch.datasetId && text(result.id)) watch.resultId = text(result.id)
      if (watch) void checkAgentResult()
      else if (!latest.current.busy) void refresh()
    }
    window.addEventListener(agentAutonomousTaskStatusEventName, status)
    window.addEventListener('parallel-workspace-updated', updated)
    return () => { window.removeEventListener(agentAutonomousTaskStatusEventName, status); window.removeEventListener('parallel-workspace-updated', updated) }
  }, [refresh, checkAgentResult, stopAgentWatch])
  useEffect(() => {
    if (!agentWatching) return
    const timer = window.setInterval(() => {
      const watch = agentWatch.current
      if (watch && !watch.accepted && !watch.pendingNoticeShown && Date.now() - watch.startedAt > 30000) {
        watch.pendingNoticeShown = true
        setNotice({ tone: 'info', text: '仍在等待助手连接与提交回执；请求保持排队，收到明确结果后更新。' }); return
      }
      void checkAgentResult()
    }, 3000)
    return () => window.clearInterval(timer)
  }, [agentWatching, checkAgentResult, stopAgentWatch])
  useEffect(() => () => { agentWatch.current = null; agentGeneration.current++; agentAbort.current?.abort() }, [])
  async function bindDatasetForAnalysis(source: ModelingDataset): Promise<ModelingDataset> {
    if (!bindingHardwareId || source.hardware_profile_id === bindingHardwareId) return source
    const bound = await api<ModelingDataset>(`/v1/parallel-modeling/datasets/${encodeURIComponent(source.id)}/hardware-binding`, {
      method: 'POST',
      body: JSON.stringify({ hardware_profile_id: bindingHardwareId }),
    })
    setDatasets(current => [bound, ...current.filter(item => item.id !== bound.id)])
    setDatasetId(bound.id)
    setBindingHardwareId(text(bound.hardware_profile_id))
    return bound
  }
  function acceptAnalyzedProfile(target: ModelingDataset, value: FeatureProfile) {
    setDatasets(current => [target, ...current.filter(item => item.id !== target.id)])
    setDatasetId(target.id); setCaptureSourceId(''); setBuiltinId('')
    setProfile(value); setStageId(value.stages[0]?.id ?? '')
    setProfiles(current => [value, ...current.filter(item => item.id !== value.id)])
  }
  async function analyze() {
    if (!dataset) return
    stopAgentWatch()
    setBusy('analyze'); setNotice(null)
    try {
      const target = await bindDatasetForAnalysis(dataset)
      const value = await api<FeatureProfile>('/v1/workload-features/analyze', { method: 'POST', body: JSON.stringify({ dataset_id: target.id, ...(measurement && target.id === dataset.id ? { measurements: measurement } : {}) }) })
      acceptAnalyzedProfile(target, value)
      if (measurement && target.id !== dataset.id) { setMeasurement(null); setMeasurementName('') }
      window.dispatchEvent(new Event('parallel-workspace-updated'))
      setNotice({ tone: 'success', text: `已保存 ${value.stages.length} 个阶段的负载特征${target.id !== dataset.id ? '，并创建硬件绑定的不可变负载版本' : ''}。分型依据与缺失指标已逐阶段保留。` })
      focusWorkflowAction(modelingAction)
    } catch (error) { setNotice({ tone: 'error', text: errorText(error) }) }
    finally { setBusy('') }
  }
  async function createExample() {
    if (!onCreateExample || busy) return
    setBusy('example'); setNotice(null); stopAgentWatch()
    try {
      const createdId = await onCreateExample()
      await refresh()
      if (createdId) { setDatasetId(createdId); setCaptureSourceId(''); setBuiltinId(''); setProfile(null); setStageId('') }
      setNotice({ tone: 'success', text: '已载入内置归档示例，可提取负载特征。' })
      focusWorkflowAction(analyzeAction)
    } catch (error) { setNotice({ tone: 'error', text: errorText(error) }) }
    finally { setBusy('') }
  }
  async function analyzeCaptureSource() {
    if (!onPrepareCaptureDataset || !captureSourceId || busy) return
    const source = captureSources.find(item => `${item.traceId}:${item.stepId}` === captureSourceId)
    if (!source) return
    if (!source.ready) { setNotice({ tone: 'error', text: source.reason || '该采集步骤暂不可分析。' }); return }
    stopAgentWatch(); setBusy('capture-import'); setNotice(null)
    try {
      const imported = await onPrepareCaptureDataset(source)
      if (!imported?.id) throw new Error('采集归档已同步，但服务器没有返回负载数据集。')
      const target = await bindDatasetForAnalysis(imported)
      setProfile(null); setStageId(''); setMeasurement(null); setMeasurementName(''); setQuery(''); setClassFilter('all')
      const value = await api<FeatureProfile>('/v1/workload-features/analyze', { method: 'POST', body: JSON.stringify({ dataset_id: target.id }) })
      acceptAnalyzedProfile(target, value)
      window.dispatchEvent(new Event('parallel-workspace-updated'))
      setNotice({ tone: 'success', text: `已从本机采样「${workloadCaptureLabel(source, source.stepTitle)}」同步完整 capture.zip${target.id !== imported.id ? '、关联所选硬件模型' : ''}，并提取 ${value.stages.length} 个阶段的负载特征。` })
      focusWorkflowAction(modelingAction)
    } catch (error) { setNotice({ tone: 'error', text: errorText(error) }) }
    finally { setBusy('') }
  }
  async function analyzeArchivedBuiltin() {
    if (!builtinId || busy) return
    stopAgentWatch(); setBusy('builtin-import'); setNotice(null)
    try {
      const imported = await api<ModelingDataset>('/v1/parallel-modeling/datasets', { method: 'POST', body: JSON.stringify({ builtin_id: builtinId }) })
      if (!imported?.id) throw new Error('历史归档已载入，但服务器没有返回负载数据集。')
      const target = await bindDatasetForAnalysis(imported)
      setProfile(null); setStageId(''); setMeasurement(null); setMeasurementName(''); setQuery(''); setClassFilter('all')
      const value = await api<FeatureProfile>('/v1/workload-features/analyze', { method: 'POST', body: JSON.stringify({ dataset_id: target.id }) })
      acceptAnalyzedProfile(target, value)
      window.dispatchEvent(new Event('parallel-workspace-updated'))
      setNotice({ tone: 'success', text: `已载入历史归档「${imported.name}」${target.id !== imported.id ? '并关联所选硬件模型' : ''}，并提取 ${value.stages.length} 个阶段的负载特征。` })
      focusWorkflowAction(modelingAction)
    } catch (error) { setNotice({ tone: 'error', text: errorText(error) }) }
    finally { setBusy('') }
  }
  function selectWorkload(value: string) {
    stopAgentWatch(); setProfile(null); setStageId(''); setMeasurement(null); setMeasurementName(''); setQuery(''); setClassFilter('all')
    if (value.startsWith('dataset:')) { setDatasetId(value.slice('dataset:'.length)); setCaptureSourceId(''); setBuiltinId('') }
    else if (value.startsWith('capture:')) { setDatasetId(''); setCaptureSourceId(value.slice('capture:'.length)); setBuiltinId('') }
    else if (value.startsWith('builtin:')) { setDatasetId(''); setCaptureSourceId(''); setBuiltinId(value.slice('builtin:'.length)) }
    else { setDatasetId(''); setCaptureSourceId(''); setBuiltinId('') }
    focusWorkflowAction(analyzeAction)
  }
  async function analyzeSelectedWorkload() {
    if (captureSourceId) await analyzeCaptureSource()
    else if (builtinId) await analyzeArchivedBuiltin()
    else await analyze()
  }
  async function upload(file: File | undefined) {
    if (!file) return
    stopAgentWatch()
    try {
      if (file.size > measurementByteLimit) throw new Error(`阶段特征 JSON 请控制在 ${Math.max(1, Math.floor(measurementByteLimit / 1024))} KiB 内。`)
      const value = JSON.parse(await file.text())
      if (!value || Array.isArray(value) || value.schema_version !== 'workload_stage_measurements/v1') throw new Error('请选择 workload_stage_measurements/v1 格式的逐阶段实测数据。')
      if (value.dataset_id !== datasetId) throw new Error('文件中的数据集 ID 与当前选择不一致。')
      setMeasurement(value); setMeasurementName(file.name); setNotice(null)
    } catch (error) { setMeasurement(null); setMeasurementName(''); setNotice({ tone: 'error', text: errorText(error) }) }
  }
  function downloadTemplate() {
    if (!profile || !dataset) return
    const template = { schema_version: 'workload_stage_measurements/v1', dataset_id: datasetId, hardware_profile_id: dataset.hardware_profile_id ?? null, clock_domain: 'trace_monotonic', clock_alignment_evidence: '', provenance: { source: '', run_id: '', collector: '' }, stages: profile.stages.map(stage => ({ stage_id: stage.id, start_ns: stage.start_ns, end_ns: stage.end_ns, tid: dataset.summary?.tid, measurement_scope: 'thread_user', counters: { instructions: { value: null, unit: 'count', event: '' }, cycles: { value: null, unit: 'count', event: '' }, llc_misses: { value: null, unit: 'count', event: '' } }, event_mapping: { llc_misses: { verified: false, cache_level: 'LLC', event: '' } } })) }
    downloadText(`${profile.id}-measurement-template.json`, JSON.stringify(template, null, 2))
  }
  async function exportCsv(view: 'stages' | 'pmu') {
    if (!profile || exporting) return
    const exportedProfile = profile
    setExporting(view); setNotice(null)
    const controller = new AbortController()
    const timeout = window.setTimeout(() => controller.abort(), 30000)
    try {
      const response = await apiText(`/v1/workload-features/profiles/${encodeURIComponent(exportedProfile.id)}/export.csv?view=${view}`, { signal: controller.signal })
      if (!response.contentType?.includes('text/csv')) throw new Error('未收到 CSV 文件，请确认平台服务已更新后重试。')
      const filename = response.filename ?? `${exportedProfile.id}-${view}.csv`
      const url = URL.createObjectURL(new Blob([response.text], { type: response.contentType }))
      const anchor = document.createElement('a')
      anchor.href = url; anchor.download = filename
      document.body.appendChild(anchor); anchor.click(); anchor.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
      setNotice({ tone: 'success', text: `已导出当前特征版本的全部 ${exportedProfile.stages.length} 个阶段${view === 'pmu' ? ' PMU 明细' : '的耗时、调度与 PMU 特征'}。` })
    } catch (error) { setNotice({ tone: 'error', text: controller.signal.aborted ? '特征 CSV 导出超时，请稍后重试。' : errorText(error) }) }
    finally { window.clearTimeout(timeout); setExporting('') }
  }
  async function askAgent() {
    stopAgentWatch()
    const generation = agentGeneration.current
    const requestedDatasetId = datasetId
    setAgentPending(true)
    const controller = new AbortController(); agentAbort.current = controller
    const timeout = window.setTimeout(() => controller.abort(), 15000)
    try {
      const snapshot = await api<{ profiles: ProfileSummary[] }>('/v1/workload-features/profiles', { signal: controller.signal })
      if (agentGeneration.current !== generation) return
      if (latest.current.datasetId !== requestedDatasetId || latest.current.busy) { stopAgentWatch(); return }
      setProfiles(snapshot.profiles)
      const requestId = dispatchParallelAgent('workload_features', context, `请分析当前数据集 ${requestedDatasetId} 的 BuildItem 阶段负载特征。先读取 workload_features_catalog，使用 workload_features_analyze 保存阶段特征版本，先解释 Running、Runnable、Blocked 调度账目及 Runnable 内有证据的抢占等待，再解释 Running 部分的计算与访存特征、PMU 窗口重叠估算单值及其原始边界、计数器覆盖与实际采样精度。已选版本 ${profile?.id ?? '无'}，当前阶段 ${selected?.id ?? '全部'}。对候选类型给出观测依据，并说明如何与共跑校准衔接。`)
      agentRequest.current = requestId
      agentWatch.current = { requestId, datasetId: requestedDatasetId, initialProfileId: profile?.id ?? null, knownProfileIds: new Set(snapshot.profiles.map(item => item.id)), startedAt: Date.now(), accepted: false }
      setAgentWatching(true)
    } catch (error) { if (agentGeneration.current === generation) { stopAgentWatch(); setNotice({ tone: 'error', text: errorText(error) }) } }
    finally { window.clearTimeout(timeout); if (agentAbort.current === controller) agentAbort.current = null }
  }
  const duration = numeric(profile?.summary.duration_ms) ?? 1
  const errorForwarded = useWorkbenchErrorNotification(notice, () => setNotice(null), 'workload-features')
  return <div className="parallel-studio workload-features-studio">
    <div className="pm-heading"><div><h2 className="pm-page-title" tabIndex={0} title="拆解每个 BuildItem 的运行、等待与阻塞时间，并分析运行期间的计算和访存特征。">负载特征</h2></div><div className="pm-toolbar"><button className="quiet-button" disabled={Boolean(busy)} onClick={() => void refresh()}><RefreshCw size={14} />刷新</button>{onNavigateModeling && <button className="quiet-button" onClick={() => onNavigateModeling(datasetId || undefined)}>并行建模<ArrowRight size={14} /></button>}</div></div>
    {notice && !errorForwarded && <div className={`pm-notice ${notice.tone}`} role="status"><CircleAlert size={15} /><span>{notice.text}</span><button aria-label="关闭提示" onClick={() => setNotice(null)}><X size={14} /></button></div>}
    {!loaded ? <div className="pm-skeleton"><LoaderCircle size={18} className="spin" />正在读取负载数据、特征版本与分析配置…</div> : <>
      <section className="pm-section"><div className="pm-section-heading"><div><Database size={17} /><h3>负载与特征版本</h3></div><span className="pm-badge">BuildItem 粒度</span></div>
        {!hasWorkloads ? <div className="pm-empty"><Database size={25} /><p>还没有可分析的数据集。可从负载归档、Trace 导入或负载采样创建数据集；随版本提供的图库样例无需连接手机即可分析。</p>{(onCreateExample || onNavigateModeling || onNavigateCapture) && <div className="pm-actions">{onCreateExample && <button className="primary-button" disabled={Boolean(busy)} onClick={() => void createExample()}>{busy === 'example' ? <LoaderCircle size={14} className="spin" /> : <Database size={14} />}载入图库历史样例</button>}{onNavigateModeling && <button className={onCreateExample ? 'quiet-button' : 'primary-button'} onClick={() => onNavigateModeling()}>打开负载归档或导入 Trace</button>}{onNavigateCapture && <button className={(onCreateExample || onNavigateModeling) ? 'quiet-button' : 'primary-button'} onClick={onNavigateCapture}>前往负载采样</button>}</div>}</div> : <><div className="pm-fields three"><label className="pm-field"><span>选择负载数据集</span><select value={selectedWorkload} disabled={Boolean(busy)} onChange={event => selectWorkload(event.target.value)}><option value="">选择负载数据</option>{localCaptureSources.length > 0 && <optgroup label="本机采样结果">{localCaptureSources.map(item => <option key={`${item.traceId}:${item.stepId}`} value={`capture:${item.traceId}:${item.stepId}`} disabled={!item.ready}>{workloadCaptureLabel(item, item.stepTitle)}{unboundArchivedCaptureKeys.has(workloadCaptureKey(item)) ? ' · 已归档但未绑定画像，可重新导入匹配' : ''}{item.ready ? '' : ` · ${item.reason || '不可分析'}`}</option>)}</optgroup>}{archivedBuiltins.length > 0 && <optgroup label="内置历史归档">{archivedBuiltins.map(item => <option key={item.id} value={`builtin:${item.id}`}>{item.name}</option>)}</optgroup>}{datasets.length > 0 && <optgroup label="已归档负载">{datasets.map(item => <option key={item.id} value={`dataset:${item.id}`}>{workloadDatasetLabel(item, captureSources)}</option>)}</optgroup>}</select></label><label className="pm-field"><span>关联硬件模型</span><select aria-label="关联硬件模型" value={bindingHardwareId} disabled={Boolean(busy) || !hardwareProfiles.length} onChange={event => { setBindingHardwareId(event.target.value); if (event.target.value !== dataset?.hardware_profile_id) { setProfile(null); setStageId(''); setMeasurement(null); setMeasurementName('') } }}><option value="" disabled={Boolean(dataset?.hardware_profile_id)}>暂不关联硬件模型</option>{hardwareProfiles.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select><small>{dataset?.hardware_profile_id === bindingHardwareId ? record(dataset.quality).hardware_identity_verified === true ? '当前负载与该硬件身份已核验。' : '当前负载已绑定该画像，设备身份尚未核验。' : bindingHardwareId ? '提取时创建硬件绑定的不可变负载版本，并重新分析阶段特征。' : '未关联时保留逻辑 CPU，核型显示为未知。'}</small></label><label className="pm-field"><span>已保存的特征版本</span><select value={profile?.id ?? ''} disabled={Boolean(busy) || !datasetId || hardwareBindingDirty} onChange={event => void loadProfile(event.target.value)}><option value="">选择特征版本</option>{profiles.filter(item => !datasetId || item.dataset_id === datasetId).map(item => <option key={item.id} value={item.id}>{timestamp(item.created_at)} · {metric(item.summary.stage_count, 0)} 个阶段 · {item.id.slice(-6)}</option>)}</select></label></div>
          {!hardwareProfiles.length && <div className="pm-note warning wf-hardware-warning" role="status"><CircleAlert size={15} /><span><strong>当前没有可关联的硬件模型。</strong>请先从本地设备提取硬件配置，或在硬件模型页手动添加；完成后返回本页重新提取负载特征。</span>{onNavigateHardwareModels && <button className="quiet-button" type="button" onClick={onNavigateHardwareModels}>前往硬件模型<ArrowRight size={13} /></button>}</div>}
          {dataset && <p className="pm-hint" style={{ marginTop: 10 }} title={text(record(dataset.quality).hardware_binding_note)}>UI 回调 {metric(dataset.summary?.duration_ms)} ms · {metric(dataset.summary?.builditem_count, 0)} 个 BuildItem · {dataset.hardware_profile_id ? record(dataset.quality).hardware_identity_verified === true ? '硬件身份已核验' : '已绑定用户画像，设备身份未核验' : '未绑定参考硬件，仍可进行实测特征分析'}</p>}
          {selectedCaptureSource && <p className="pm-hint" style={{ marginTop: 10 }}>{selectedCaptureNeedsReimport ? '该采样曾归档但未绑定硬件画像；重新提取会按采样设备身份尝试匹配当前画像。' : '本机采样结果将在提取时同步完整 capture.zip，并一起读取 Trace、PMU 与采样元数据。'}</p>}
          <div className="pm-actions"><button ref={analyzeAction} className="primary-button pm-step-action" disabled={Boolean(busy) || (!datasetId && !captureSourceId && !builtinId) || Boolean(selectedCaptureSource && !selectedCaptureSource.ready) || !catalog} onClick={() => void analyzeSelectedWorkload()}>{busy === 'analyze' || busy === 'capture-import' || busy === 'builtin-import' ? <LoaderCircle size={14} className="spin" /> : <Activity size={14} />}提取负载特征</button>{assistantEnabled && <button className="quiet-button" disabled={!datasetId || hardwareBindingDirty || Boolean(busy) || agentPending || agentWatching} onClick={() => void askAgent()}><Sparkles size={14} />{agentPending ? '正在发送给助手…' : agentWatching ? '等待助手分析结果…' : '让助手分析特征'}</button>}{assistantEnabled && agentWatching && !agentPending && <button className="quiet-button" onClick={() => { stopAgentWatch(); setNotice({ tone: 'info', text: '已停止自动载入，助手仍可继续完成分析。' }) }}>停止自动载入</button>}</div>
          <details className="pm-details"><summary>补充逐阶段 PMU / I/O 实测数据</summary><p className="pm-hint">已有 Trace 对齐 PMU 会自动读取。补充数据需与当前阶段的线程、起止时间、硬件和时钟对应；整帧、整进程统计不会按耗时分摊。</p><label className="pm-field wf-file"><span>逐阶段实测 JSON</span><input type="file" accept=".json,application/json" disabled={!datasetId || Boolean(busy)} onChange={event => void upload(event.target.files?.[0])} /></label>{measurementName && <p className="pm-hint">已选择 {measurementName}，下一次提取时进行来源、边界和单位校验。<button className="quiet-button" onClick={() => { setMeasurement(null); setMeasurementName('') }}>移除</button></p>}<div className="pm-actions">{profile && <button className="quiet-button" onClick={downloadTemplate}><FileJson size={14} />下载本负载的实测模板</button>}</div><details className="pm-details"><summary>字段与事件语义要求</summary><pre className="pm-code">{JSON.stringify(catalog?.measurement_contract, null, 2)}</pre></details></details>
        </>}
      </section>
      {!profile ? <section className="pm-section"><div className="wf-empty-domains"><article><GitBranch size={22} /><h3>先拆调度时间</h3><p>运行 / 等待 / 阻塞，关联抢占与 Binder 证据</p></article><article><Cpu size={22} /><h3>再看运行特征</h3><p>指令与周期、IPC、Cache / LLC MPKI、分支事件</p></article><article><Activity size={22} /><h3>同时核对精度</h3><p>采样边界、覆盖与计数器复用，区分区间与估计</p></article></div><p className="pm-hint">点击“提取负载特征”或选择已有版本，查看完整阶段列表。采集缺失的指标明确保留为“未采集”。</p></section> : <>
        {profile.hardware_profile_id && profile.quality.hardware_identity_verified !== true && <div className="pm-note warning wf-hardware-warning" role="status"><CircleAlert size={15} /><span><strong>硬件画像未核验 / 用户绑定。</strong>逻辑 CPU 驻留来自 Trace 实测；下方核组名称来自用户绑定画像，只能作为建模候选，不能视为已核验的设备实际核型。可重新导入原采样，让平台按设备身份匹配画像。</span></div>}
        <section className="pm-section"><div className="pm-section-heading"><div><BarChart3 size={17} /><h3>阶段总览</h3></div><div className="pm-toolbar" role="group" aria-label="导出特征版本"><button className="quiet-button" disabled={Boolean(busy) || Boolean(exporting)} title="每阶段一行，包含耗时、调度拆解与 PMU 特征；导出当前版本全部阶段" onClick={() => void exportCsv('stages')}>{exporting === 'stages' ? <LoaderCircle size={14} className="spin" /> : <ArrowDownToLine size={14} />}导出阶段 CSV</button><button className="quiet-button" disabled={Boolean(busy) || Boolean(exporting)} title="每阶段、每指标一行，包含计数、估算、边界与覆盖率；导出当前版本全部阶段" onClick={() => void exportCsv('pmu')}>{exporting === 'pmu' ? <LoaderCircle size={14} className="spin" /> : <ArrowDownToLine size={14} />}导出 PMU 明细</button><button className="quiet-button" disabled={Boolean(busy)} onClick={() => downloadText(`${profile.id}.json`, JSON.stringify(profile, null, 2))}><FileJson size={14} />导出 JSON</button></div></div><div className="pm-metrics"><div><span>BuildItem 阶段</span><strong>{metric(profile.summary.stage_count, 0)}</strong><small>顺序与并行建模保持一致</small></div><div><span>阶段 CPU 时间</span><strong>{metric(profile.summary.stage_cpu_ms)}</strong><small>core-ms · Trace 实测</small></div><div><span>PMU 边界可用</span><strong>{metric(profile.summary.pmu_usable_stages, 0)} / {profile.stages.length}</strong><small>可用于完整证据判断的阶段</small></div><div><span>Running 候选分型</span><strong>{metric(profile.summary.classified_cpu_stages, 0)}</strong><small>包含标注置信度的降级候选</small></div></div>
        </section>
        <section className="pm-section"><div className="pm-section-heading"><div><Layers size={17} /><h3>逐阶段负载特征</h3></div><span className="pm-badge">{visibleStages.length} / {profile.stages.length} 个阶段</span></div><div className="wf-filters"><label className="pm-field"><span>搜索阶段</span><div className="wf-search"><Search size={14} /><input placeholder="组件名 / 阶段 ID" value={query} onChange={event => setQuery(event.target.value)} /></div></label><label className="pm-field"><span>特征分型</span><select value={classFilter} onChange={event => setClassFilter(event.target.value)}><option value="all">全部类型</option>{catalog?.classes.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><label className="pm-field"><span>排序方式</span><select value={sort} onChange={event => setSort(event.target.value)}><option value="order">Trace 执行顺序</option><option value="wall">阶段耗时降序</option><option value="cpu">CPU 时间降序</option></select></label></div>
          <div className="pm-table-scroll wf-table-scroll"><table className="pm-table wf-stage-table" aria-label="BuildItem 阶段特征表"><thead><tr><th>阶段 / 组件</th><th>阶段耗时 ms</th><th>运行 CPU / 核组</th><th>调度时间 ms</th><th>Running · IPC</th><th>指令数 / Cycles</th><th>Cache / LLC MPKI</th><th>特征分型</th></tr></thead><tbody>{visibleStages.map(stage => {
            const timing = stageTiming(stage)
            const running = runningClassification(stage)
            return <tr key={stage.id} className={selected?.id === stage.id ? 'selected' : ''}><td className="wrap"><button className="wf-stage-button" aria-pressed={selected?.id === stage.id} aria-controls="selected-builditem-detail" onClick={() => setStageId(stage.id)}><small>{stage.display_id}</small><strong>{stage.name}</strong></button></td><td>{metric(stage.wall_ms)}</td><td><CpuPlacement stage={stage} hardwareVerified={profile.quality.hardware_identity_verified === true} profileContractEligible={sourceAnchorProfileEligible(profile)} /></td><td><StateBar stage={stage} /><small>运行 {metric(timing.running)} · 等待 {metric(timing.runnable)}</small><small>阻塞 {metric(timing.blocked)} · 抢占 {timing.preempted === null ? '未区分' : metric(timing.preempted)}</small></td><td><PmuValue stage={stage} metricKey="ipc" /></td><td><PmuValue stage={stage} metricKey="instructions" /><span className="wf-counter-caption">Cycles</span><PmuValue stage={stage} metricKey="cycles" /></td><td><PmuValue stage={stage} metricKey="cache_mpki" /><span className="wf-counter-caption">LLC</span><PmuValue stage={stage} metricKey="llc_mpki" /></td><td><span className={`wf-type-pill wf-class-${stage.classification.id}`}>{stage.classification.name}</span><small>{running === stage.classification ? classificationSummary(running) : `Running：${running.name} · ${classificationSummary(running)}`}</small></td></tr>
          })}</tbody></table>{!visibleStages.length && <div className="pm-empty">没有匹配的阶段，请修改搜索或类型筛选。</div>}</div>
          <div className="wf-state-legend"><span><i className="cpu" />Running</span><span><i className="preempted" />Interrupted · 抢占等待</span><span><i className="runnable" />其余 Runnable</span><span><i className="blocked" />Blocked</span><span><i className="unknown" />未知</span></div><p className="pm-hint">抢占等待已包含在 Runnable 总等待中；未记录抢占来源时不强行区分。点击阶段后，可在下方时间条与阶段详情中继续查看。</p>
          <div className="wf-method-details">
            <details className="pm-details"><summary>特征分型说明 · 判据、降级候选与证据边界</summary><div className="wf-classification-method">
              <p>先判断整个阶段是否由等待主导，再对 Running 部分做计算 / 访存候选分型。同步 Binder 阻塞或 Runnable 等核达到阶段耗时的 30% 时，阶段主特征分别标记为“系统服务等待”或“调度等待”，但仍单独保留 Running 候选供性能曲线匹配。</p>
              <div className="wf-method-grid">
                <article><span className="wf-type-pill wf-class-compute">计算主导候选</span><p><strong>完整证据：</strong>Running 占比 ≥ 70%，IPC 下界 ≥ 1.0，且已核验 LLC MPKI 上界 ≤ 5。</p><p><strong>部分证据：</strong>IPC ≥ 1.2 或 Cache / LLC MPKI ≤ 5 提供计算方向投票。</p></article>
                <article><span className="wf-type-pill wf-class-memory">访存压力候选</span><p><strong>完整证据：</strong>Running 占比 ≥ 70%，IPC 上界 ≤ 1.0，且已核验 LLC MPKI 下界 ≥ 20。</p><p><strong>部分证据：</strong>IPC ≤ 0.8 仅是弱证据；Cache / LLC MPKI ≥ 20 才提供更强访存方向证据。</p></article>
                <article><span className="wf-type-pill wf-class-mixed">混合特征</span><p>指标落在阈值中间、计算与缓存信号冲突，或只有基础计数而不能确认单一主导因素时，保守标记为混合候选。</p><p>缺少有效 IPC、Cache / LLC MPKI 和基础计数时保留“证据不足”。</p></article>
              </div>
              <p className="pm-hint">这些阈值用于同设备、同采集口径下的候选分组，不是芯片通用瓶颈定律。通用 Cache miss 或未核验 LLC 事件不能直接证明 DRAM 带宽瓶颈；真实并行收益仍由核组、频点与共跑微基准校准。</p>
            </div></details>
            <details className="pm-details"><summary>校准微基准设计 · 如何对应三类 Running 负载</summary><div className="wf-benchmark-method">
              <div className="wf-method-grid">
                <article><h4>冻结任务结构</h4><p><strong>计算：</strong>寄存器整数混合依赖链，每 work unit 计 640 个源码整数操作，不虚构字节分母。</p><p><strong>访存：</strong>顺序读改写 1024 个 uint64，每 unit 至少读 8 KiB、写 8 KiB。</p><p><strong>混合：</strong>在相同 16 KiB 最小源码读写量上叠加整数混合。</p></article>
                <article><h4>固定放置与扫描</h4><p>每个核组选择一个代表物理核，将目标线程绑定到对应逻辑 CPU；逐 OPP 锁频并核验实际 CPU、实际频率和同胞线程静默度。</p><p>同一任务结构在训练 OPP 重复实测，另留未参与拟合的 OPP 做验证。</p></article>
                <article><h4>同窗观测与用途</h4><p>正式计时窗采集单位工作量 Wall / CPU 时延，并同步记录 instructions、cycles、IPC、Cache miss 与 Cache MPKI；硬件支持时补充 LLC MPKI。</p><p>PMU 缺失保持 unavailable，不按任务标签推断；实测曲线用于把真实阶段的 Running 时间迁移到目标核组和频点。</p></article>
              </div>
              <p className="pm-hint">三类代理用于建立“核组 × 任务类型 × OPP”的时延响应，不声称代理与真实 BuildItem 指令流完全相同，也不保证单线程访存代理一定打满 DRAM。经典 Roofline 的计算强度扫描属于独立辅助实验。</p>
            </div></details>
          </div>
        </section>
        <section className="pm-section wf-position-section"><div className="pm-section-heading"><div><GitBranch size={17} /><h3>BuildItem 在 UI 回调中的位置</h3></div><span className="pm-badge">0 — {metric(duration)} ms</span></div>
          <div className="wf-class-legend">{catalog?.classes.map(item => <span key={item.id}><i className={`wf-class-${item.id}`} />{item.name} · {metric(record(profile.summary.class_counts)[item.id] ?? 0, 0)}</span>)}</div>
          <div className="wf-timeline" aria-label="BuildItem 阶段时间分布">{profile.stages.map(stage => <button key={stage.id} type="button" aria-label={`${stage.display_id} ${stage.name} ${metric(stage.wall_ms)} ms`} aria-pressed={selected?.id === stage.id} aria-controls="selected-builditem-detail" title={`${stage.display_id} ${stage.name} · ${metric(stage.wall_ms)} ms · ${stage.classification.name}`} className={`wf-class-${stage.classification.id} ${selected?.id === stage.id ? 'selected' : ''}`} style={{ left: `${stage.relative_start_ms / duration * 100}%`, width: `${Math.max(.08, stage.wall_ms / duration * 100)}%` }} onClick={() => setStageId(stage.id)} />)}</div><p className="pm-hint">灰底留白为 BuildItem 外的工作，共 {metric(profile.summary.non_builditem_ms)} ms；选择任一色块，下方立即显示对应阶段。</p>
          {selected && <div id="selected-builditem-detail" className="wf-detail wf-position-detail" aria-label="阶段详情"><div className="pm-section-heading"><div><Activity size={17} /><h3>{selected.display_id} · {selected.name}</h3></div><span className={`wf-type-pill wf-class-${selected.classification.id}`}>{selected.classification.name}</span></div><p className="pm-hint">相对回调起点 {metric(selected.relative_start_ms)} ms · 阶段耗时 {metric(selected.wall_ms)} ms · {selected.id}</p>
          <SchedulingBreakdown stage={selected} />
          <h4 className="wf-running-heading"><Cpu size={16} />再看 Running 的计算与访存特征</h4><p className="pm-hint">PMU 来自目标线程的已采集作用域；下方区分精确计数与估算，详情保留原始范围和覆盖。</p>
          <RunningMetrics stage={selected} />
          <PmuQuality stage={selected} />
          <div className="wf-service-summary" aria-label="关联服务、CPU 放置与 I/O 证据"><MetricRow name="阶段运行 CPU / 核组" note={profile.quality.hardware_identity_verified === true ? '逻辑 CPU 来自 Trace 调度驻留；核组来自已核验硬件画像' : '逻辑 CPU 来自 Trace 调度驻留；核组来自未核验的用户绑定画像'} value={<CpuPlacement stage={selected} detailed hardwareVerified={profile.quality.hardware_identity_verified === true} profileContractEligible={sourceAnchorProfileEligible(profile)} />} /><MetricRow name="关联服务端 CPU" note="其他线程开销，不能加到本阶段耗时" value={`${metric(selected.scheduling.service_cpu_ms)} ms`} /><MetricRow name="I/O 阻塞" note="需要 I/O 证据，普通 Blocked 不直接归因" value={numeric(selected.scheduling.io_wait_ms) === null ? '未采集' : `${metric(selected.scheduling.io_wait_ms)} ms`} /><MetricRow name="I/O 读 / 写字节" value={`${range(selected.scheduling.io_read_bytes, null, 0)} / ${range(selected.scheduling.io_write_bytes, null, 0)}`} /><MetricRow name="CPU 迁移" value={range(selected.scheduling.cpu_migrations, null, 0)} /></div>
          <div className="wf-evidence"><h4>阶段主特征与 Running 负载候选</h4><p><strong>阶段主特征：{selected.classification.name}</strong> · {classificationSummary(selected.classification)}{numeric(selected.classification.confidence?.score) !== null && <> · {metric(Number(selected.classification.confidence?.score) * 100, 0)}%</>}</p>{selected.classification.evidence.map((item, index) => <p key={`primary-${index}`}>{item}</p>)}{selectedRunning && selectedRunning !== selected.classification && <><p><strong>Running 负载候选：{selectedRunning.name}</strong> · {classificationSummary(selectedRunning)}{numeric(selectedRunning.confidence?.score) !== null && <> · {metric(Number(selectedRunning.confidence?.score) * 100, 0)}%</>}</p>{selectedRunning.evidence.map((item, index) => <p key={`running-${index}`}>{item}</p>)}</>}{selectedRunning && Array.isArray(selectedRunning.missing_evidence) && selectedRunning.missing_evidence.length > 0 && <p className="pm-hint">Running 候选待补证据：{selectedRunning.missing_evidence.join('、')}。</p>}<p>{selectedRunning?.parallel_implication ?? selected.classification.parallel_implication}</p>{selected.quality.reasons.filter(item => !selected.classification.evidence.includes(item) && !selectedRunning?.evidence.includes(item)).map((item, index) => <p className="pm-hint" key={index}>{item}</p>)}<p className="pm-hint">PMU 作用域：{selected.quality.pmu_scope ?? '未采集'}。服务调用：{Array.isArray(selected.scheduling.services) && selected.scheduling.services.length ? selected.scheduling.services.join('、') : '未匹配到同步服务调用'}。</p></div>
          <details className="pm-details"><summary>查看原始证据、核心驻留与缺失指标</summary><pre className="pm-code">{JSON.stringify({ quality: selected.quality, time_breakdown: selected.time_breakdown, metric_evidence: selected.metric_evidence, cpu_placement: selected.scheduling.cpu_placement, cpu_residency_evidence: selected.scheduling.cpu_residency_evidence, cpu_residency: selected.scheduling.cpu_residency, evidence: selected.evidence }, null, 2)}</pre></details>
          </div>}
        </section>
        {(onNavigateModeling || onNavigateCalibration) && <section className="pm-section"><div className="pm-section-heading"><div><GitBranch size={17} /><h3>接入并行模型</h3></div><span className="pm-badge">版本绑定 · 可追溯</span></div><div className="wf-handoff"><div><h4>阶段特征 → 共跑校准 → 并行配置</h4><p>阶段类型用于选择对应的共跑干扰参数，依赖关系和硬件资源仍由并行模型约束。部分证据阶段使用明确标注置信度的降级候选；完全没有有效指标时才保留 unknown 并回退兼容代价参数。</p></div><div className="pm-actions">{onNavigateModeling && <button ref={modelingAction} className="primary-button pm-step-action" onClick={() => onNavigateModeling(profile.dataset_id, profile.id)}>带入并行建模<ArrowRight size={14} /></button>}{onNavigateCalibration && <button className="quiet-button" onClick={onNavigateCalibration}>查看共跑校准</button>}</div></div><details className="pm-details"><summary>版本来源与分型规则</summary><pre className="pm-code">{JSON.stringify({ id: profile.id, dataset_fingerprint: profile.dataset_fingerprint, rules: catalog?.classification_rules, provenance: profile.provenance }, null, 2)}</pre></details></section>}
      </>}
    </>}
  </div>
}
