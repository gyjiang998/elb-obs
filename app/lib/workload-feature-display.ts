import { numeric, record, text } from './parallel-modeling'
import type { JsonRecord } from './types'

export type StageTimingSource = {
  cpu_ms: number
  states_ms: Record<string, number>
  scheduling: JsonRecord
  time_breakdown?: JsonRecord
}

/** Keep scheduler time exclusive; preemption is evidence about part of runnable. */
export function stageTiming(stage: StageTimingSource) {
  const breakdown = record(stage.time_breakdown)
  const partition = record(breakdown.partition_ms)
  const running = numeric(partition.running) ?? stage.cpu_ms
  const blocked = numeric(partition.blocked) ?? stage.states_ms.blocked ?? 0
  const runnable = numeric(breakdown.runnable_total_ms) ?? stage.states_ms.runnable ?? 0
  const unknown = numeric(partition.unknown) ?? stage.states_ms.unknown ?? 0
  const preemptionKnown = breakdown.preemption_evidence_available === true
  const preempted = preemptionKnown ? numeric(partition.preempted_wait) : null
  return {
    running, runnable, blocked, unknown, preempted, preemptionKnown,
    runnableOther: preemptionKnown ? numeric(partition.runnable_other) : null,
    preemptionEvents: numeric(breakdown.preemption_event_count),
    irq: numeric(breakdown.irq_overlap_ms), softirq: numeric(breakdown.softirq_overlap_ms),
    irqComplete: breakdown.irq_evidence_complete === true, softirqComplete: breakdown.softirq_evidence_complete === true,
    binder: numeric(stage.scheduling.binder_blocked_ms),
    notes: Array.isArray(breakdown.notes) ? breakdown.notes.filter((item): item is string => typeof item === 'string') : [],
  }
}

export function stageTimeSegments(stage: StageTimingSource) {
  const timing = stageTiming(stage)
  return [
    { id: 'cpu', label: 'Running 实际占 CPU', value: timing.running },
    ...(timing.preemptionKnown && timing.preempted !== null && timing.runnableOther !== null
      ? [{ id: 'preempted', label: 'Interrupted 抢占等待（Runnable 子集）', value: timing.preempted },
        { id: 'runnable', label: '其余 Runnable 等待', value: timing.runnableOther }]
      : [{ id: 'runnable', label: 'Runnable 等待（未区分抢占）', value: timing.runnable }]),
    { id: 'blocked', label: 'Blocked 阻塞', value: timing.blocked },
    { id: 'unknown', label: '状态未知', value: timing.unknown },
  ]
}

export const workloadMetrics = [
  { key: 'instructions', label: '指令数', area: 'compute', field: 'instructions', bounds: 'instruction_bounds', digits: 0 },
  { key: 'cycles', label: '周期数', area: 'compute', field: 'cycles', bounds: 'cycle_bounds', digits: 0 },
  { key: 'ipc', label: 'IPC', area: 'compute', field: 'ipc', bounds: 'ipc_bounds', digits: 2, note: 'instructions / cycles，同组计数' },
  { key: 'branch_instructions', label: '分支指令', area: 'compute', field: 'branch_instructions', digits: 0 },
  { key: 'branch_misses', label: '分支预测失败', area: 'compute', field: 'branch_misses', digits: 0 },
  { key: 'branch_mpki', label: 'Branch MPKI', area: 'compute', field: 'branch_mpki', digits: 2, note: '每千条指令的分支预测失败' },
  { key: 'cache_misses', label: '通用 Cache miss', area: 'memory', field: 'cache_misses', bounds: 'cache_miss_bounds', digits: 0 },
  { key: 'cache_references', label: '通用 Cache 访问', area: 'memory', field: 'cache_references', digits: 0 },
  { key: 'cache_mpki', label: 'Cache MPKI', area: 'memory', field: 'cache_mpki', bounds: 'cache_mpki_bounds', digits: 2, note: '通用缓存事件；不等同 LLC MPKI' },
  { key: 'llc_mpki', label: 'LLC MPKI', area: 'memory', field: 'llc_mpki', bounds: 'llc_mpki_bounds', digits: 2, note: '每千条指令的末级 Cache miss' },
  { key: 'llc_accesses', label: 'LLC 读访问', area: 'memory', field: 'llc_accesses', digits: 0 },
  { key: 'llc_misses', label: 'LLC miss', area: 'memory', field: 'llc_misses', digits: 0 },
  { key: 'l1d_mpki', label: 'L1D MPKI', area: 'memory', field: 'l1d_mpki', digits: 2 },
  { key: 'l1d_accesses', label: 'L1D 读访问', area: 'memory', field: 'l1d_accesses', digits: 0 },
  { key: 'l1d_misses', label: 'L1D miss', area: 'memory', field: 'l1d_misses', digits: 0 },
  { key: 'dtlb_mpki', label: 'DTLB MPKI', area: 'memory', field: 'dtlb_mpki', digits: 2 },
  { key: 'dtlb_accesses', label: 'DTLB 读访问', area: 'memory', field: 'dtlb_accesses', digits: 0 },
  { key: 'dtlb_misses', label: 'DTLB miss', area: 'memory', field: 'dtlb_misses', digits: 0 },
  { key: 'stalled_frontend', label: '前端停顿周期', area: 'compute', field: 'stalled_frontend', digits: 0 },
  { key: 'stalled_backend', label: '后端停顿周期', area: 'compute', field: 'stalled_backend', digits: 0 },
  { key: 'loads', label: 'Load', area: 'memory', field: 'loads', digits: 0 },
  { key: 'stores', label: 'Store', area: 'memory', field: 'stores', digits: 0 },
  { key: 'memory_stall_cycles', label: '访存停顿周期', area: 'memory', field: 'memory_stall_cycles', digits: 0 },
  { key: 'dram_bytes', label: 'DRAM 字节', area: 'memory', field: 'dram_bytes', digits: 0 },
] as const

type MetricSource = {
  compute: JsonRecord; memory: JsonRecord; evidence: JsonRecord
  metric_evidence?: JsonRecord
}

function interval(value: unknown): [number, number] | null {
  if (!Array.isArray(value) || value.length !== 2) return null
  const [lower, upper] = value.map(numeric)
  return lower !== null && upper !== null && lower <= upper ? [lower, upper] : null
}

/** Estimates and exact counts stay distinct; old ranges never become midpoint estimates. */
export function stageMetric(stage: MetricSource, key: string) {
  const definition = workloadMetrics.find(item => item.key === key)
  const declared = record(stage.metric_evidence?.[key])
  const legacy = definition ? stage[definition.area] : {}
  const legacyValue = definition ? numeric(legacy[definition.field]) : null
  const legacyBounds = definition && 'bounds' in definition ? interval(legacy[definition.bounds]) : null
  const pmu = record(stage.evidence.pmu)
  const hasEvidence = Object.keys(declared).length > 0
  const exact = hasEvidence ? numeric(declared.exact) : legacyValue
  const bounds = hasEvidence ? interval(declared.bounds) : legacyBounds
  const status = hasEvidence ? text(declared.status, 'unavailable') : exact !== null ? 'exact' : bounds ? 'bounded' : 'unavailable'
  const interiorBins = hasEvidence ? numeric(declared.interior_bins) : numeric(pmu.interior_bins)
  const reasons = Array.isArray(declared.reason_codes) ? declared.reason_codes.filter((item): item is string => typeof item === 'string') : []
  // Ratios cannot have finite bounds when the denominator's interior count is zero.
  const legacyRatioUncertain = !hasEvidence && ['ipc', 'cache_mpki'].includes(key)
    && interval(stage.compute.instruction_bounds) !== null && interiorBins === 0
  return {
    key, label: definition?.label ?? key, digits: definition?.digits ?? 2, status, exact, bounds,
    estimate: numeric(declared.estimate), observed: interval(declared.observed_counter_bounds),
    estimateScope: text(declared.estimate_scope, numeric(declared.estimate) !== null ? 'interior_only' : ''),
    estimateMethod: text(declared.estimate_method), windowConvention: text(declared.sample_window_convention),
    interiorEstimate: numeric(declared.interior_estimate), weightedBins: numeric(declared.weighted_bins),
    interiorTimeCoverage: numeric(declared.interior_time_coverage),
    boundaryUncertain: (status !== 'exact' && bounds !== null && (interiorBins === 0 || bounds[0] === 0))
      || reasons.some(reason => ['short_stage_no_interior_samples', 'boundary_dominated'].includes(reason)) || legacyRatioUncertain,
    interiorBins, boundaryBins: numeric(declared.boundary_bins),
    timeCoverage: hasEvidence ? numeric(declared.time_coverage) : numeric(pmu.interior_time_fraction),
    runtimeCoverage: numeric(declared.runtime_coverage),
    maxBoundarySpanUs: hasEvidence ? numeric(declared.max_boundary_span_us) : numeric(pmu.max_intersecting_snapshot_span_us),
    group: text(declared.group_id), scope: text(declared.scope, text(pmu.scope)), reasons,
    mappingVerified: declared.hardware_mapping_verified ?? (key.startsWith('llc_') ? stage.memory.llc_mapping_verified : undefined),
    hasEvidence,
  }
}
