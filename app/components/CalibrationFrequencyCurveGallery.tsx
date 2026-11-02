'use client'

import { useEffect, useMemo, useState } from 'react'
import { Activity, CircleAlert, Gauge } from 'lucide-react'
import type { JsonRecord } from '../lib/types'

type CalibrationFitRecord = JsonRecord & {
  id: string
  name?: string
  created_at?: string
  hardware_profile_id?: string
  model_preview?: JsonRecord
  measurements?: JsonRecord
  frequency_curve_observations?: JsonRecord
  quality?: JsonRecord
}

type CalibrationVersionRecord = JsonRecord & {
  id: string
  name?: string
  version?: string
  created_at?: string
  fit_id?: string
  model?: JsonRecord
}

type CurveSource = {
  id: string
  label: string
  status: 'published' | 'published-risk' | 'fit' | 'fit-risk' | 'diagnostic'
  fitStatus: string
  qualityPassed: boolean
  model: JsonRecord | null
  measurements: JsonRecord | null
  observations: JsonRecord | null
  rejectedSampleIds: Set<string>
  rejectedSampleIndices: Set<number>
}

type MeasuredPoint = {
  frequencyKHz: number
  unitCpuMs: number
  repeatCount: number
  splits: string[]
  workloadIds: string[]
  workloadVersions: string[]
  workingSetBytes: number[]
  instructionIntensity: number | null
  instructionIntensitySampleCount: number
  pmuStatus: string
  pmuIPC: number | null
  pmuCacheMisses: number | null
  pmuCacheMissesPerWorkUnit: number | null
  pmuCacheReferences: number | null
  pmuCacheMPKI: number | null
  pmuSampleCount: number
  pmuMetricSampleCounts: { ipc: number; cacheMisses: number; cacheReferences: number; cacheMPKI: number }
}

type CurveCard = {
  coreId: string
  coreLabel: string
  workloadClass: string
  workloadLabel: string
  profile: JsonRecord
  parameters: JsonRecord | null
  measurements: MeasuredPoint[]
}

const knownWorkloadLabels: Record<string, string> = {
  compute: '计算型',
  memory: '访存型',
  mixed: '混合型',
}

const knownCoreLabels: Record<string, string> = {
  big: '大核',
  middle: '中核',
  small: '小核',
}

const proxyIntensityFallback: Record<string, { level: string; basis: string; detail: string; sourceBytesPerUnit: number }> = {
  compute: {
    level: '源码强度未定义（无字节分母）',
    basis: '640 个源码整数操作/工作单元，不访问代理工作集',
    detail: '没有源码字节分母，强度保持为空；不能写成无限大，也不能换算为 FLOP/Byte。',
    sourceBytesPerUnit: 0,
  },
  memory: {
    level: '0.125 源码整数操作/最小源码 Byte',
    basis: '2052 个源码整数操作 ÷ 16 KiB 最小读写量/工作单元',
    detail: '字节数描述冻结源码访问，不等于缓存缺失或内存控制器观测到的 DRAM 流量。',
    sourceBytesPerUnit: 16384,
  },
  mixed: {
    level: '0.164 源码整数操作/最小源码 Byte',
    basis: '2692 个源码整数操作 ÷ 16 KiB 最小读写量/工作单元',
    detail: '它是单一混合档，不是连续强度扫描；数值强度需使用静态合同或同窗 PMU。',
    sourceBytesPerUnit: 16384,
  },
}

const accessPatternLabels: Record<string, string> = {
  register_integer_mix_dependency_chain: '寄存器整数混合依赖链',
  sequential_read_modify_write_8kib_blocks_with_wrap: '8 KiB 连续块顺序读改写并循环工作集',
  integer_mix_then_sequential_read_modify_write_8kib_blocks_with_wrap: '整数混合后顺序读改写 8 KiB 连续块',
}

function record(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {}
}

function finite(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function positive(value: unknown): number | null {
  const result = finite(value)
  return result != null && result > 0 ? result : null
}

function median(values: number[]): number {
  const ordered = [...values].sort((left, right) => left - right)
  const middle = Math.floor(ordered.length / 2)
  return ordered.length % 2 ? ordered[middle] : (ordered[middle - 1] + ordered[middle]) / 2
}

function formatFrequency(frequencyKHz: number): string {
  if (frequencyKHz >= 1_000_000) return `${(frequencyKHz / 1_000_000).toFixed(frequencyKHz % 1_000_000 ? 2 : 1)} GHz`
  return `${Math.round(frequencyKHz / 1000)} MHz`
}

function compactNumber(value: number): string {
  if (!Number.isFinite(value)) return '—'
  if (Math.abs(value) >= 100) return value.toFixed(0)
  if (Math.abs(value) >= 10) return value.toFixed(1)
  if (Math.abs(value) >= 1) return value.toFixed(2)
  return value.toPrecision(2)
}

function formatIntensity(value: number): string {
  if (!Number.isFinite(value)) return '—'
  if (Math.abs(value) >= 10) return value.toFixed(1).replace(/\.0$/, '')
  if (Math.abs(value) >= 1) return value.toFixed(2).replace(/0+$/, '').replace(/\.$/, '')
  if (Math.abs(value) >= .1) return value.toFixed(3).replace(/0+$/, '').replace(/\.$/, '')
  return value.toPrecision(3)
}

function uniqueStrings(value: unknown): string[] {
  return Array.isArray(value) ? [...new Set(value.filter((item): item is string => typeof item === 'string' && Boolean(item)))] : []
}

function uniquePositiveNumbers(value: unknown): number[] {
  return Array.isArray(value) ? [...new Set(value.map(positive).filter((item): item is number => item != null))].sort((left, right) => left - right) : []
}

function formatByteCount(value: number): string {
  if (value >= 1024 * 1024 && value % (1024 * 1024) === 0) return `${value / (1024 * 1024)} MiB`
  if (value >= 1024 && value % 1024 === 0) return `${value / 1024} KiB`
  return `${value} B`
}

function instructionIntensityFromSample(sample: JsonRecord): number | null {
  const explicit = positive(sample.instruction_intensity_instructions_per_dram_byte)
  if (explicit != null) return explicit
  if (sample.pmu_window_aligned !== true) return null
  const instructions = positive(sample.pmu_retired_instructions)
  const dramBytes = positive(sample.pmu_dram_bytes)
  return instructions != null && dramBytes != null ? instructions / dramBytes : null
}

function frequencyFactor(maxKHz: number, frequencyKHz: number, parameters: JsonRecord): number | null {
  const kneeKHz = positive(parameters.knee_khz)
  const highExponent = finite(parameters.above_knee_exponent)
  const lowExponent = finite(parameters.below_knee_exponent)
  if (!kneeKHz || highExponent == null || lowExponent == null || frequencyKHz <= 0 || maxKHz <= 0) return null
  if (frequencyKHz >= kneeKHz) return (maxKHz / frequencyKHz) ** highExponent
  return (maxKHz / kneeKHz) ** highExponent * (kneeKHz / frequencyKHz) ** lowExponent
}

function curveDomain(parameters: JsonRecord | null, measurements: MeasuredPoint[]): number[] {
  const configured = parameters && Array.isArray(parameters.frequency_domain_khz)
    ? parameters.frequency_domain_khz.map(finite).filter((value): value is number => value != null && value > 0)
    : []
  return [...new Set([...configured, ...measurements.map(point => point.frequencyKHz)])].sort((left, right) => left - right)
}

function hardwareGroupLabels(model: JsonRecord): Map<string, string> {
  const labels = new Map<string, string>()
  const hardwareProfile = record(model.hardware_profile)
  const groups = Array.isArray(hardwareProfile.core_groups) ? hardwareProfile.core_groups : []
  for (const value of groups) {
    const group = record(value)
    const id = typeof group.id === 'string' ? group.id : ''
    const name = typeof group.name === 'string' ? group.name : ''
    if (id && name) labels.set(id, name)
  }
  return labels
}

function measuredPoints(measurements: JsonRecord | null, rejectedSampleIds: Set<string>, rejectedSampleIndices: Set<number>, observations: JsonRecord | null = null): Map<string, MeasuredPoint[]> {
  const projected = observations && Array.isArray(observations.points) ? observations.points : []
  if (projected.length) {
    const grouped = new Map<string, MeasuredPoint[]>()
    for (const value of projected) {
      const point = record(value)
      const coreId = typeof point.core_kind === 'string' ? point.core_kind : ''
      const workloadClass = typeof point.workload_class === 'string' ? point.workload_class : ''
      const frequencyKHz = positive(point.frequency_khz)
      const unitCpuMs = positive(point.unit_cpu_ms)
      if (!coreId || !workloadClass || !frequencyKHz || !unitCpuMs) continue
      const key = `${coreId}\u0000${workloadClass}`
      const values = grouped.get(key) ?? []
      const metricCounts = record(point.pmu_metric_sample_counts)
      values.push({
        frequencyKHz,
        unitCpuMs,
        repeatCount: positive(point.repeat_count) ?? 0,
        splits: Array.isArray(point.splits) ? point.splits.filter((item): item is string => typeof item === 'string') : [],
        workloadIds: uniqueStrings(point.workload_ids),
        workloadVersions: uniqueStrings(point.workload_versions),
        workingSetBytes: uniquePositiveNumbers(point.working_set_bytes),
        instructionIntensity: positive(point.instruction_intensity_instructions_per_dram_byte),
        instructionIntensitySampleCount: positive(point.instruction_intensity_sample_count) ?? 0,
        pmuStatus: typeof point.pmu_status === 'string' ? point.pmu_status : 'unavailable',
        pmuIPC: finite(point.pmu_ipc),
        pmuCacheMisses: finite(point.pmu_cache_misses),
        pmuCacheMissesPerWorkUnit: finite(point.pmu_cache_misses_per_work_unit),
        pmuCacheReferences: finite(point.pmu_cache_references),
        pmuCacheMPKI: finite(point.pmu_cache_mpki),
        pmuSampleCount: positive(point.pmu_sample_count) ?? 0,
        pmuMetricSampleCounts: {
          ipc: positive(metricCounts.pmu_ipc) ?? 0,
          cacheMisses: positive(metricCounts.pmu_cache_misses) ?? 0,
          cacheReferences: positive(metricCounts.pmu_cache_references) ?? 0,
          cacheMPKI: positive(metricCounts.pmu_cache_mpki) ?? 0,
        },
      })
      grouped.set(key, values)
    }
    for (const values of grouped.values()) values.sort((left, right) => left.frequencyKHz - right.frequencyKHz)
    return grouped
  }
  const grouped = new Map<string, Map<number, {
    values: number[]
    splits: Set<string>
    workloadIds: Set<string>
    workloadVersions: Set<string>
    workingSetBytes: Set<number>
    instructionIntensities: number[]
    pmuIPC: number[]
    pmuCacheMisses: number[]
    pmuCacheMissesPerWorkUnit: number[]
    pmuCacheReferences: number[]
    pmuCacheMPKI: number[]
    pmuStatuses: Set<string>
    pmuCompleteSamples: number
  }>>()
  const samples = measurements && Array.isArray(measurements.samples) ? measurements.samples : []
  for (const [sampleIndex, value] of samples.entries()) {
    const sample = record(value)
    const sampleId = typeof sample.sample_id === 'string' ? sample.sample_id : ''
    const workloadId = typeof sample.workload_id === 'string' ? sample.workload_id : ''
    const coreId = typeof sample.core_kind === 'string' ? sample.core_kind : ''
    const workloadClass = typeof sample.workload_class === 'string' ? sample.workload_class : ''
    const frequencyKHz = positive(sample.frequency_khz)
    const cpuMs = positive(sample.cpu_ms)
    const workUnits = positive(sample.work_units)
    if (!coreId || !workloadClass || !frequencyKHz || !cpuMs || !workUnits) continue
    if (sample.benchmark !== 'pair_work' || Number(sample.active_tasks) !== 1 || !workloadId.includes('frequency_proxy')) continue
    if (rejectedSampleIndices.has(sampleIndex) || rejectedSampleIds.has(sampleId) || sample.affinity_verified === false || sample.frequency_verified === false || sample.thermal_throttled === true) continue
    const key = `${coreId}\u0000${workloadClass}`
    const frequencies = grouped.get(key) ?? new Map<number, {
      values: number[]
      splits: Set<string>
      workloadIds: Set<string>
      workloadVersions: Set<string>
      workingSetBytes: Set<number>
      instructionIntensities: number[]
      pmuIPC: number[]
      pmuCacheMisses: number[]
      pmuCacheMissesPerWorkUnit: number[]
      pmuCacheReferences: number[]
      pmuCacheMPKI: number[]
      pmuStatuses: Set<string>
      pmuCompleteSamples: number
    }>()
    const point = frequencies.get(frequencyKHz) ?? {
      values: [], splits: new Set<string>(), workloadIds: new Set<string>(), workloadVersions: new Set<string>(),
      workingSetBytes: new Set<number>(), instructionIntensities: [], pmuIPC: [], pmuCacheMisses: [], pmuCacheMissesPerWorkUnit: [],
      pmuCacheReferences: [], pmuCacheMPKI: [], pmuStatuses: new Set<string>(), pmuCompleteSamples: 0,
    }
    point.values.push(cpuMs / workUnits)
    if (typeof sample.split === 'string') point.splits.add(sample.split)
    if (workloadId) point.workloadIds.add(workloadId)
    if (typeof sample.workload_version === 'string' && sample.workload_version) point.workloadVersions.add(sample.workload_version)
    const workingSetBytes = positive(sample.working_set_bytes)
    if (workingSetBytes != null) point.workingSetBytes.add(workingSetBytes)
    const instructionIntensity = instructionIntensityFromSample(sample)
    if (instructionIntensity != null) point.instructionIntensities.push(instructionIntensity)
    if (typeof sample.pmu_collection_status === 'string') point.pmuStatuses.add(sample.pmu_collection_status)
    const cacheMisses = finite(sample.pmu_cache_misses)
    if (cacheMisses != null && cacheMisses >= 0) point.pmuCacheMissesPerWorkUnit.push(cacheMisses / workUnits)
    let completePmu = true
    for (const [field, target] of [
      ['pmu_ipc', point.pmuIPC], ['pmu_cache_misses', point.pmuCacheMisses],
      ['pmu_cache_references', point.pmuCacheReferences], ['pmu_cache_mpki', point.pmuCacheMPKI],
    ] as const) {
      const amount = finite(sample[field])
      if (amount != null && amount >= 0) target.push(amount)
      else completePmu = false
    }
    if (completePmu) point.pmuCompleteSamples += 1
    frequencies.set(frequencyKHz, point)
    grouped.set(key, frequencies)
  }
  return new Map([...grouped].map(([key, values]) => [key, [...values].map(([frequencyKHz, point]) => ({
    frequencyKHz,
    unitCpuMs: median(point.values),
    repeatCount: point.values.length,
    splits: [...point.splits].sort(),
    workloadIds: [...point.workloadIds].sort(),
    workloadVersions: [...point.workloadVersions].sort(),
    workingSetBytes: [...point.workingSetBytes].sort((left, right) => left - right),
    instructionIntensity: point.instructionIntensities.length ? median(point.instructionIntensities) : null,
    instructionIntensitySampleCount: point.instructionIntensities.length,
    pmuStatus: point.pmuStatuses.size === 1 && point.pmuStatuses.has('collected') ? 'collected'
      : point.pmuIPC.length || point.pmuCacheMisses.length || point.pmuCacheMPKI.length ? 'partial' : 'unavailable',
    pmuIPC: point.pmuIPC.length ? median(point.pmuIPC) : null,
    pmuCacheMisses: point.pmuCacheMisses.length ? median(point.pmuCacheMisses) : null,
    pmuCacheMissesPerWorkUnit: point.pmuCacheMissesPerWorkUnit.length ? median(point.pmuCacheMissesPerWorkUnit) : null,
    pmuCacheReferences: point.pmuCacheReferences.length ? median(point.pmuCacheReferences) : null,
    pmuCacheMPKI: point.pmuCacheMPKI.length ? median(point.pmuCacheMPKI) : null,
    pmuSampleCount: point.pmuCompleteSamples,
    pmuMetricSampleCounts: {
      ipc: point.pmuIPC.length,
      cacheMisses: point.pmuCacheMisses.length,
      cacheReferences: point.pmuCacheReferences.length,
      cacheMPKI: point.pmuCacheMPKI.length,
    },
  })).sort((left, right) => left.frequencyKHz - right.frequencyKHz)]))
}

function buildCards(source: CurveSource): CurveCard[] {
  const model = source.model ?? {}
  const profiles = record(model.profiles)
  const measurementsByCurve = measuredPoints(source.measurements, source.rejectedSampleIds, source.rejectedSampleIndices, source.observations)
  const coreIds = new Set<string>(Object.keys(profiles))
  const workloadClasses = new Set<string>()
  for (const [key] of measurementsByCurve) {
    const [coreId, workloadClass] = key.split('\u0000')
    coreIds.add(coreId)
    workloadClasses.add(workloadClass)
  }
  for (const value of Object.values(profiles)) {
    const classes = record(record(record(value).frequency_roofline).classes)
    Object.keys(classes).forEach(workloadClass => workloadClasses.add(workloadClass))
  }
  if (coreIds.size && !workloadClasses.size) ['compute', 'memory', 'mixed'].forEach(value => workloadClasses.add(value))
  const groupLabels = hardwareGroupLabels(model)
  return [...coreIds].flatMap(coreId => {
    const profile = record(profiles[coreId])
    const classes = record(record(profile.frequency_roofline).classes)
    return [...workloadClasses].map(workloadClass => ({
      coreId,
      coreLabel: groupLabels.get(coreId) ?? knownCoreLabels[coreId] ?? coreId,
      workloadClass,
      workloadLabel: knownWorkloadLabels[workloadClass] ?? workloadClass,
      profile,
      parameters: Object.prototype.hasOwnProperty.call(classes, workloadClass) ? record(classes[workloadClass]) : null,
      measurements: measurementsByCurve.get(`${coreId}\u0000${workloadClass}`) ?? [],
    }))
  })
}

function sourceIntensitySummary(card: CurveCard): { level: string; basis: string; detail: string; accessesWorkingSet: boolean } {
  const fallback = proxyIntensityFallback[card.workloadClass] ?? {
    level: '源码强度合同', basis: '当前代理没有可识别的静态强度定义', detail: '需要查看对应的冻结微基准合同。', sourceBytesPerUnit: 0,
  }
  const parameters = card.parameters ?? {}
  const nested = record(parameters.workload_intensity)
  const contract = Object.keys(nested).length ? nested : parameters
  const operations = positive(contract.algorithmic_integer_operations_per_work_unit)
  const sourceBytes = positive(contract.minimum_source_bytes_per_work_unit)
  const streamedBytes = positive(contract.streamed_footprint_bytes_per_work_unit)
  const staticIntensity = positive(contract.operational_intensity_integer_ops_per_byte)
  const rawAccessPattern = typeof contract.working_set_access_pattern === 'string' ? contract.working_set_access_pattern : ''
  const accessPattern = accessPatternLabels[rawAccessPattern] ?? rawAccessPattern
  if (operations == null && sourceBytes == null && staticIntensity == null && !accessPattern) return {
    ...fallback, accessesWorkingSet: fallback.sourceBytesPerUnit > 0,
  }
  const parts = [
    operations != null ? `${compactNumber(operations)} 源码整数操作/工作单元` : '',
    sourceBytes != null ? `${formatByteCount(sourceBytes)} 最小源码字节/工作单元` : '无源码字节分母',
    streamedBytes != null ? `${formatByteCount(streamedBytes)} 连续访问足迹/工作单元` : '',
  ].filter(Boolean)
  return {
    level: staticIntensity != null ? `${formatIntensity(staticIntensity)} 源码整数操作/最小源码 Byte` : fallback.level,
    basis: parts.join(' · '),
    detail: `${accessPattern ? `${accessPattern}。` : ''}${fallback.detail}`,
    accessesWorkingSet: sourceBytes != null || streamedBytes != null,
  }
}

function intensityEvidence(card: CurveCard): {
  source: { level: string; basis: string; detail: string; accessesWorkingSet: boolean }
  measured: { value: number; minimum: number; maximum: number; sampleCount: number } | null
  sourceThroughput: { minimum: number; maximum: number } | null
  workingSets: number[]
  workloadVersions: string[]
} {
  const nestedContract = record((card.parameters ?? {}).workload_intensity)
  const contract = Object.keys(nestedContract).length ? nestedContract : card.parameters ?? {}
  const hardwareCounterEvidence = record(contract.hardware_counter_evidence)
  const modelInstructionIntensity = positive(hardwareCounterEvidence.instruction_intensity_instructions_per_dram_byte)
  const sampledIntensityPoints = card.measurements
    .filter(point => point.instructionIntensity != null && point.instructionIntensitySampleCount > 0)
    .map(point => point.instructionIntensity as number)
  const intensityPoints = sampledIntensityPoints.length ? sampledIntensityPoints
    : modelInstructionIntensity != null ? [modelInstructionIntensity] : []
  const intensitySampleCount = card.measurements.reduce((sum, point) => sum + point.instructionIntensitySampleCount, 0)
    || (positive(hardwareCounterEvidence.sample_count) ?? 0)
  const sourceBytesPerUnit = positive(contract.minimum_source_bytes_per_work_unit)
    ?? positive(proxyIntensityFallback[card.workloadClass]?.sourceBytesPerUnit)
  const sourceThroughputs = sourceBytesPerUnit == null ? [] : card.measurements
    .filter(point => point.unitCpuMs > 0)
    .map(point => sourceBytesPerUnit / (point.unitCpuMs * 1e6))
  return {
    source: sourceIntensitySummary(card),
    measured: intensityPoints.length ? {
      value: median(intensityPoints), minimum: Math.min(...intensityPoints), maximum: Math.max(...intensityPoints),
      sampleCount: intensitySampleCount,
    } : null,
    sourceThroughput: sourceThroughputs.length ? {
      minimum: Math.min(...sourceThroughputs), maximum: Math.max(...sourceThroughputs),
    } : null,
    workingSets: [...new Set(card.measurements.flatMap(point => point.workingSetBytes))].sort((left, right) => left - right),
    workloadVersions: [...new Set(card.measurements.flatMap(point => point.workloadVersions))].sort(),
  }
}

function FrequencyCurveCard({ card, evidenceState }: { card: CurveCard; evidenceState: 'normal' | 'risk' | 'blocked' }) {
  const width = 320
  const height = 170
  const plot = { left: 43, right: 12, top: 13, bottom: 36 }
  const domain = curveDomain(card.parameters, card.measurements)
  const maxKHz = positive(card.profile.max_khz) ?? (domain.length ? Math.max(...domain) : null)
  const referenceCost = card.parameters ? positive(card.parameters.reference_cpu_ms_per_work_unit)
    ?? positive(card.parameters.max_opp_cpu_ms_per_work_unit) : null
  const absolute = referenceCost != null
  const fittedPoints: { frequencyKHz: number; value: number }[] = []
  if (card.parameters && maxKHz && domain.length >= 2) {
    const minimum = Math.min(...domain)
    const maximum = Math.max(...domain)
    for (let index = 0; index <= 48; index += 1) {
      const frequencyKHz = minimum + (maximum - minimum) * index / 48
      const factor = frequencyFactor(maxKHz, frequencyKHz, card.parameters)
      if (factor != null) fittedPoints.push({ frequencyKHz, value: factor * (referenceCost ?? 1) })
    }
  }
  let observed = card.measurements.map(point => ({ ...point, value: point.unitCpuMs }))
  if (!absolute && observed.length) {
    const highest = [...observed].sort((left, right) => right.frequencyKHz - left.frequencyKHz)[0]?.unitCpuMs
    if (highest > 0) observed = observed.map(point => ({ ...point, value: point.unitCpuMs / highest }))
  }
  const values = [...fittedPoints.map(point => point.value), ...observed.map(point => point.value)]
  const drawable = domain.length >= 2 && values.length > 0
  const xMinimum = drawable ? Math.min(...domain) : 0
  const xMaximum = drawable ? Math.max(...domain) : 1
  const rawMinimum = drawable ? Math.min(...values) : 0
  const rawMaximum = drawable ? Math.max(...values) : 1
  const padding = Math.max((rawMaximum - rawMinimum) * .12, rawMaximum * .04, 1e-9)
  const yMinimum = Math.max(0, rawMinimum - padding)
  const yMaximum = rawMaximum + padding
  const x = (value: number) => plot.left + (value - xMinimum) / Math.max(1, xMaximum - xMinimum) * (width - plot.left - plot.right)
  const y = (value: number) => plot.top + (yMaximum - value) / Math.max(1e-12, yMaximum - yMinimum) * (height - plot.top - plot.bottom)
  const displayScale = absolute ? 1000 : 1
  const tickFrequencies = domain.length <= 6
    ? domain
    : [...new Set([domain[0], domain[Math.floor((domain.length - 1) / 2)], domain[domain.length - 1]])]
  const kneeKHz = card.parameters ? positive(card.parameters.knee_khz) : null
  const intensity = intensityEvidence(card)
  const pmuIPC = card.measurements.map(point => point.pmuIPC).filter((value): value is number => value != null)
  const pmuCacheMissesPerThousandWorkUnits = card.measurements
    .map(point => point.pmuCacheMissesPerWorkUnit == null ? null : point.pmuCacheMissesPerWorkUnit * 1000)
    .filter((value): value is number => value != null)
  const pmuCacheMPKI = card.measurements.map(point => point.pmuCacheMPKI).filter((value): value is number => value != null)
  const pmuSampleCount = card.measurements.reduce((sum, point) => sum + point.pmuSampleCount, 0)
  const pmuMetricSampleCounts = card.measurements.reduce((sum, point) => ({
    ipc: sum.ipc + point.pmuMetricSampleCounts.ipc,
    cacheMisses: sum.cacheMisses + point.pmuMetricSampleCounts.cacheMisses,
    cacheReferences: sum.cacheReferences + point.pmuMetricSampleCounts.cacheReferences,
    cacheMPKI: sum.cacheMPKI + point.pmuMetricSampleCounts.cacheMPKI,
  }), { ipc: 0, cacheMisses: 0, cacheReferences: 0, cacheMPKI: 0 })
  const metricRange = (values: number[], suffix = '') => values.length
    ? `${compactNumber(Math.min(...values))}${values.length > 1 ? `–${compactNumber(Math.max(...values))}` : ''}${suffix}`
    : '不可用'
  const highExponent = card.parameters ? finite(card.parameters.above_knee_exponent) : null
  const lowExponent = card.parameters ? finite(card.parameters.below_knee_exponent) : null
  const formulaReady = maxKHz != null && kneeKHz != null && highExponent != null && lowExponent != null
  const formulaAnchor = referenceCost ?? 1
  const formulaAnchorLabel = referenceCost != null
    ? `${compactNumber(formulaAnchor)} ms/工作单元`
    : '1（相对时延）'

  return <article className={'cs-frequency-curve-card' + (drawable ? '' : ' empty')}>
    <header><div><strong>{card.coreLabel}</strong><span>{card.workloadLabel}</span></div>{card.parameters ? <span className={'pm-badge ' + (evidenceState === 'normal' ? 'good' : 'warn')}>{evidenceState === 'blocked' ? '仅诊断' : evidenceState === 'risk' ? '有风险拟合' : '已拟合'}</span> : <span className="pm-badge">无拟合</span>}</header>
    {drawable ? <>
      <svg className="cs-frequency-curve-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${card.coreLabel}${card.workloadLabel}频点性能曲线`}>
        <line className="cs-curve-axis" x1={plot.left} y1={plot.top} x2={plot.left} y2={height - plot.bottom} />
        <line className="cs-curve-axis" x1={plot.left} y1={height - plot.bottom} x2={width - plot.right} y2={height - plot.bottom} />
        {[yMinimum, (yMinimum + yMaximum) / 2, yMaximum].map(value => <g key={value}>
          <line className="cs-curve-grid" x1={plot.left} y1={y(value)} x2={width - plot.right} y2={y(value)} />
          <text className="cs-curve-tick y" x={plot.left - 5} y={y(value) + 3}>{compactNumber(value * displayScale)}</text>
        </g>)}
        {tickFrequencies.map(frequencyKHz => <g key={frequencyKHz}>
          <line className="cs-curve-tick-mark" x1={x(frequencyKHz)} y1={height - plot.bottom} x2={x(frequencyKHz)} y2={height - plot.bottom + 4} />
          <text className="cs-curve-tick" x={x(frequencyKHz)} y={height - plot.bottom + 14}>{Math.round(frequencyKHz / 1000)}</text>
        </g>)}
        {kneeKHz && kneeKHz >= xMinimum && kneeKHz <= xMaximum && <line className="cs-curve-knee" x1={x(kneeKHz)} y1={plot.top} x2={x(kneeKHz)} y2={height - plot.bottom} />}
        {fittedPoints.length > 1 && <polyline className="cs-curve-fit" points={fittedPoints.map(point => `${x(point.frequencyKHz)},${y(point.value)}`).join(' ')} />}
        {observed.map(point => <circle className="cs-curve-observed" key={point.frequencyKHz} cx={x(point.frequencyKHz)} cy={y(point.value)} r={3.4}>
          <title>{`${formatFrequency(point.frequencyKHz)} · ${compactNumber(point.value * displayScale)}${absolute ? ' ms/千工作量' : '×'} · ${point.repeatCount} 次${point.splits.length ? ` · ${point.splits.join('/')}` : ''} · IPC ${point.pmuIPC == null ? '不可用' : compactNumber(point.pmuIPC)} · Cache miss/千工作量 ${point.pmuCacheMissesPerWorkUnit == null ? '不可用' : compactNumber(point.pmuCacheMissesPerWorkUnit * 1000)} · Cache MPKI ${point.pmuCacheMPKI == null ? '不可用' : compactNumber(point.pmuCacheMPKI)}`}</title>
        </circle>)}
        <text className="cs-curve-axis-label x" x={(plot.left + width - plot.right) / 2} y={height - 3}>OPP（MHz）</text>
        <text className="cs-curve-axis-label y" transform={`translate(10 ${(plot.top + height - plot.bottom) / 2}) rotate(-90)`}>{absolute ? 'CPU ms / 千工作量' : '相对时延'}</text>
      </svg>
      <div className="cs-curve-legend"><span><i className="observed" />实测{observed.length ? ` ${observed.length} OPP` : '点未保留'}</span>{fittedPoints.length > 1 && <span><i className="fitted" />拟合曲线</span>}{kneeKHz && <span><i className="knee" />转折 {formatFrequency(kneeKHz)}</span>}</div>
      {formulaReady && <div className="cs-curve-formula"><span>分段拟合公式</span><code>{`T(f)=T(fmax)×(fmax/f)^${compactNumber(highExponent)}，f≥${formatFrequency(kneeKHz)}；T(f)=T(fmax)×(fmax/fk)^${compactNumber(highExponent)}×(fk/f)^${compactNumber(lowExponent)}，f<fk`}</code><small>{`T(fmax)=${formulaAnchorLabel}，fmax=${formatFrequency(maxKHz)}，fk=${formatFrequency(kneeKHz)}；只缩放 On-CPU 成本。`}</small></div>}
      <div className="cs-frequency-pmu">
        <div><span>IPC</span><strong>{metricRange(pmuIPC)}</strong><small>n={pmuMetricSampleCounts.ipc}</small></div>
        <div><span>Cache miss / 千工作量</span><strong>{metricRange(pmuCacheMissesPerThousandWorkUnits)}</strong><small>n={pmuMetricSampleCounts.cacheMisses}</small></div>
        <div><span>Cache MPKI</span><strong>{metricRange(pmuCacheMPKI)}</strong><small>n={pmuMetricSampleCounts.cacheMPKI}</small></div>
        <small>{pmuMetricSampleCounts.ipc || pmuMetricSampleCounts.cacheMisses || pmuMetricSampleCounts.cacheMPKI
          ? `四项齐全 ${pmuSampleCount} 个；各指标覆盖：IPC ${pmuMetricSampleCounts.ipc} · Cache miss ${pmuMetricSampleCounts.cacheMisses} · Cache references ${pmuMetricSampleCounts.cacheReferences} · Cache MPKI ${pmuMetricSampleCounts.cacheMPKI}。Cache miss 先按 work_units 归一，原始计数保留在校准证据中；PMU 计数按 enabled/running 缩放。`
          : '设备未提供或拒绝对应 perf_event；不影响时延采集，指标保持不可用。'}</small>
      </div>
      <div className="cs-curve-intensity">
        <div><span>代理设定</span><strong>{intensity.source.level}</strong><small>{intensity.source.basis}</small></div>
        <div><span>PMU 实测强度</span>{intensity.measured
          ? <><strong>{formatIntensity(intensity.measured.value)} Instruction/DRAM Byte</strong><small>{intensity.measured.sampleCount > 0 ? `${intensity.measured.sampleCount} 个同窗样本` : '模型保留的同窗 PMU 证据'}{intensity.measured.minimum !== intensity.measured.maximum ? ` · 范围 ${formatIntensity(intensity.measured.minimum)}–${formatIntensity(intensity.measured.maximum)}` : ''}</small></>
          : <><strong className="missing">未采集</strong><small>需要同一计时窗的 retired instructions 与已核验 DRAM bytes。</small></>}</div>
        {intensity.sourceThroughput && <div className="source-throughput"><span>代理源码吞吐</span><strong>{compactNumber(intensity.sourceThroughput.minimum)}–{compactNumber(intensity.sourceThroughput.maximum)} GB/s</strong><small>最小源码字节 ÷ 线程 CPU 时间；用于比较本代理各 OPP，不等于 DRAM 带宽。</small></div>}
        <p>{intensity.source.detail}{intensity.workingSets.length ? intensity.source.accessesWorkingSet
          ? ` 实际遍历的计划工作集 ${intensity.workingSets.map(formatByteCount).join(' / ')}。`
          : ` 统一计划字段为 ${intensity.workingSets.map(formatByteCount).join(' / ')}，该 compute 代理不分配或访问它。` : ''}{intensity.workloadVersions.length ? ` 代理版本 ${intensity.workloadVersions.join(' / ')}。` : ''}</p>
      </div>
      <footer><span>适用域 {formatFrequency(xMinimum)}–{formatFrequency(xMaximum)}</span><span>{domain.length} 档 OPP</span></footer>
    </> : <div className="cs-curve-empty"><CircleAlert size={20} /><strong>暂无可展示曲线</strong><span>{card.measurements.length ? '已有实测点，但缺少完整拟合参数或有效 OPP 适用域。' : '该核组与任务类型尚未形成有效实测和拟合结果。'}</span></div>}
  </article>
}

type ClassicRoof = {
  coreId: string
  frequencyKHz: number
  peakGflops: number
  bandwidthGbytes: number
  knee: number
  points: { intensity: number; measured: number; roof: number; ipc: number | null; cacheMisses: number | null; cacheMPKI: number | null; pmuStatus: string }[]
  instruction: JsonRecord
  quality: JsonRecord
  formula: JsonRecord
}

function classicRoofs(model: JsonRecord | null): Map<string, ClassicRoof[]> {
  const result = new Map<string, ClassicRoof[]>()
  const rows = Array.isArray(record(model?.classic_roofline).roofs) ? record(model?.classic_roofline).roofs as unknown[] : []
  for (const value of rows) {
    const row = record(value)
    const coreId = typeof row.core_kind === 'string' ? row.core_kind : ''
    const frequencyKHz = positive(row.frequency_khz)
    const peakGflops = positive(row.maximum_attainable_performance_gflops)
    const bandwidthGbytes = positive(row.sustainable_source_bandwidth_gbytes_s)
    const knee = positive(row.knee_flop_per_byte)
    if (!coreId || frequencyKHz == null || peakGflops == null || bandwidthGbytes == null || knee == null) continue
    const summaries = new Map((Array.isArray(row.intensity_summary) ? row.intensity_summary : []).map(record).map(summary => [finite(summary.operational_intensity_flop_per_byte), record(summary.pmu)]))
    const rawPoints = (Array.isArray(row.intensity_points) ? row.intensity_points : []).map(record).map(point => {
      const intensity = positive(point.operational_intensity_flop_per_byte)
      const pmu = intensity == null ? {} : summaries.get(intensity) ?? {}
      return {
      intensity: positive(point.operational_intensity_flop_per_byte),
      measured: positive(point.measured_gflops),
      roof: positive(point.roof_gflops),
      ipc: finite(pmu.ipc_median), cacheMisses: finite(pmu.cache_misses_median),
      cacheMPKI: finite(pmu.cache_mpki_median), pmuStatus: typeof pmu.status === 'string' ? pmu.status : 'unavailable',
      }
    }).filter((point): point is { intensity: number; measured: number; roof: number; ipc: number | null; cacheMisses: number | null; cacheMPKI: number | null; pmuStatus: string } => point.intensity != null && point.measured != null && point.roof != null)
    const pointsByIntensity = new Map<number, { measured: number[]; roofs: number[]; ipc: number[]; cacheMisses: number[]; cacheMPKI: number[]; statuses: Set<string> }>()
    for (const point of rawPoints) {
      const values = pointsByIntensity.get(point.intensity) ?? { measured: [], roofs: [], ipc: [], cacheMisses: [], cacheMPKI: [], statuses: new Set<string>() }
      values.measured.push(point.measured); values.roofs.push(point.roof)
      if (point.ipc != null) values.ipc.push(point.ipc)
      if (point.cacheMisses != null) values.cacheMisses.push(point.cacheMisses)
      if (point.cacheMPKI != null) values.cacheMPKI.push(point.cacheMPKI)
      values.statuses.add(point.pmuStatus)
      pointsByIntensity.set(point.intensity, values)
    }
    const points = [...pointsByIntensity].map(([intensity, values]) => ({
      intensity, measured: median(values.measured), roof: median(values.roofs),
      ipc: values.ipc.length ? median(values.ipc) : null,
      cacheMisses: values.cacheMisses.length ? median(values.cacheMisses) : null,
      cacheMPKI: values.cacheMPKI.length ? median(values.cacheMPKI) : null,
      pmuStatus: values.statuses.size === 1 && values.statuses.has('collected') ? 'collected' : values.ipc.length || values.cacheMPKI.length ? 'partial' : 'unavailable',
    })).sort((left, right) => left.intensity - right.intensity)
    const roofs = result.get(coreId) ?? []
    roofs.push({ coreId, frequencyKHz, peakGflops, bandwidthGbytes, knee, points,
      instruction: record(row.instruction_roofline), quality: record(row.quality), formula: record(row.formula) })
    result.set(coreId, roofs)
  }
  for (const roofs of result.values()) roofs.sort((left, right) => left.frequencyKHz - right.frequencyKHz)
  return result
}

function ClassicRooflineCard({ coreLabel, roofs, evidenceState }: { coreLabel: string; roofs: ClassicRoof[]; evidenceState: 'normal' | 'risk' | 'blocked' }) {
  const highestFrequency = roofs[roofs.length - 1]?.frequencyKHz ?? 0
  const [frequencyKHz, setFrequencyKHz] = useState(highestFrequency)
  useEffect(() => {
    if (!roofs.some(roof => roof.frequencyKHz === frequencyKHz)) setFrequencyKHz(highestFrequency)
  }, [frequencyKHz, highestFrequency, roofs])
  const roof = roofs.find(value => value.frequencyKHz === frequencyKHz) ?? roofs[roofs.length - 1]
  if (!roof) return null
  const width = 390
  const height = 205
  const plot = { left: 47, right: 15, top: 17, bottom: 38 }
  const intensities = [...roof.points.map(point => point.intensity), roof.knee].filter(value => value > 0)
  const xMinimum = Math.max(.01, Math.min(...intensities) / 2)
  const xMaximum = Math.max(xMinimum * 8, Math.max(...intensities) * 2)
  const yMaximum = Math.max(roof.peakGflops, ...roof.points.map(point => point.measured), 1) * 1.12
  const x = (value: number) => plot.left + (Math.log2(value) - Math.log2(xMinimum)) / Math.max(.001, Math.log2(xMaximum) - Math.log2(xMinimum)) * (width - plot.left - plot.right)
  const y = (value: number) => plot.top + (yMaximum - value) / yMaximum * (height - plot.top - plot.bottom)
  const roofPoints = [
    { intensity: xMinimum, performance: Math.min(roof.peakGflops, roof.bandwidthGbytes * xMinimum) },
    { intensity: roof.knee, performance: roof.peakGflops },
    { intensity: xMaximum, performance: roof.peakGflops },
  ]
  const intensityTicks = [...new Set([xMinimum, ...roof.points.map(point => point.intensity), xMaximum])]
    .sort((left, right) => left - right).filter((_, index, values) => values.length <= 6 || index === 0 || index === values.length - 1 || index === Math.floor(values.length / 2))
  const instructionIntensity = positive(roof.instruction.median_instruction_intensity_per_dram_byte)
  const instructionPublished = roof.instruction.status === 'publishable' && instructionIntensity != null

  return <article className="cs-classic-roofline-card">
    <header><div><strong>{coreLabel}</strong><span>经典浮点 Roofline</span>{evidenceState !== 'normal' && <em>{evidenceState === 'blocked' ? '仅诊断' : '质量风险'}</em>}</div><label><span>OPP</span><select value={roof.frequencyKHz} onChange={event => setFrequencyKHz(Number(event.target.value))}>{roofs.map(value => <option value={value.frequencyKHz} key={value.frequencyKHz}>{formatFrequency(value.frequencyKHz)}</option>)}</select></label></header>
    <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${coreLabel} ${formatFrequency(roof.frequencyKHz)} 的实测经典 Roofline；包含 ${roof.points.length} 个源码级计算强度档。`}>
      <line className="cs-curve-axis" x1={plot.left} y1={plot.top} x2={plot.left} y2={height - plot.bottom} />
      <line className="cs-curve-axis" x1={plot.left} y1={height - plot.bottom} x2={width - plot.right} y2={height - plot.bottom} />
      {[0, roof.peakGflops / 2, roof.peakGflops].map(value => <g key={value}><line className="cs-curve-grid" x1={plot.left} y1={y(value)} x2={width - plot.right} y2={y(value)} /><text className="cs-curve-tick y" x={plot.left - 5} y={y(value) + 3}>{compactNumber(value)}</text></g>)}
      {intensityTicks.map(value => <text className="cs-curve-tick" key={value} x={x(value)} y={height - plot.bottom + 14}>{formatIntensity(value)}</text>)}
      <polyline className="cs-classic-roof" points={roofPoints.map(point => `${x(point.intensity)},${y(point.performance)}`).join(' ')} />
      <line className="cs-curve-knee" x1={x(roof.knee)} y1={plot.top} x2={x(roof.knee)} y2={height - plot.bottom} />
      {roof.points.map(point => <circle className="cs-classic-intensity-point" key={`${point.intensity}:${point.measured}`} cx={x(point.intensity)} cy={y(point.measured)} r={3.7}><title>{`${formatIntensity(point.intensity)} FLOP/最小源码 Byte · 实测 ${compactNumber(point.measured)} GFLOP/s · 屋顶 ${compactNumber(point.roof)} GFLOP/s`}</title></circle>)}
      <text className="cs-curve-axis-label x" x={(plot.left + width - plot.right) / 2} y={height - 4}>算法 FLOP / 最小源码 Byte（log₂）</text>
      <text className="cs-curve-axis-label y" transform={`translate(11 ${(plot.top + height - plot.bottom) / 2}) rotate(-90)`}>实测 GFLOP/s</text>
    </svg>
    <div className="cs-classic-roofline-metrics"><div><span>计算屋顶</span><strong>{compactNumber(roof.peakGflops)} GFLOP/s</strong></div><div><span>持续源码带宽</span><strong>{compactNumber(roof.bandwidthGbytes)} GB/s</strong></div><div><span>交点</span><strong>{formatIntensity(roof.knee)} FLOP/Byte</strong></div></div>
    <div className="cs-curve-formula"><span>Roofline 公式</span><code>{typeof roof.formula.substituted === 'string' ? roof.formula.substituted : `P(I)=min(${compactNumber(roof.peakGflops)}, ${compactNumber(roof.bandwidthGbytes)}×I) GFLOP/s`}</code><small>{`Iknee=Ppeak/Bsource=${formatIntensity(roof.knee)} FLOP/Byte`}</small></div>
    <div className="cs-classic-intensity-levels"><span>实测强度档</span>{roof.points.length ? roof.points.map(point => <strong key={point.intensity}>{formatIntensity(point.intensity)}</strong>) : <em>未保留强度阶梯点</em>}</div>
    <div className="cs-classic-pmu-table"><div className="head"><span>强度</span><span>IPC</span><span>Cache misses</span><span>Cache MPKI</span></div>{roof.points.map(point => <div key={point.intensity}><strong>{formatIntensity(point.intensity)}</strong><span>{point.ipc == null ? '不可用' : compactNumber(point.ipc)}</span><span>{point.cacheMisses == null ? '不可用' : compactNumber(point.cacheMisses)}</span><span>{point.cacheMPKI == null ? '不可用' : compactNumber(point.cacheMPKI)}</span></div>)}</div>
    <div className={'cs-instruction-intensity ' + (instructionPublished ? 'measured' : 'missing')}><span>Instruction Roofline</span>{instructionPublished ? <strong>{formatIntensity(instructionIntensity)} Instruction/DRAM Byte · 同窗 PMU</strong> : <strong>缺少同窗 retired instructions + DRAM bytes</strong>}</div>
    <footer>{evidenceState === 'risk' && <strong>当前 Roofline 有统计质量门未通过；数值已保留并随版本标注风险，使用前应检查失败项。</strong>}{evidenceState === 'blocked' && <strong>当前 Roofline 仅供诊断，来源、结构或完整性门禁未通过，不能发布或用于寻优。</strong>}适用域：当前核组、OPP、冻结浮点内核与源码最小字节口径。它解释计算/带宽上限，不直接替代整数 BuildItem 或任务类型频点时延曲线。</footer>
  </article>
}

function ClassicRooflineEvidence({ model, evidenceState }: { model: JsonRecord | null; evidenceState: 'normal' | 'risk' | 'blocked' }) {
  const groups = useMemo(() => classicRoofs(model), [model])
  const labels = hardwareGroupLabels(model ?? {})
  return <div className="cs-intensity-evidence">
    <div className="pm-section-heading"><div><Activity size={16} /><h4>计算强度证据与 Roofline 扫描</h4></div><small>与任务类型频点时延曲线分开解释</small></div>
    {groups.size ? <>
      <p className="pm-hint">每个圆点是冻结浮点内核的一个源码计算强度档；折线由实测计算屋顶和持续源码带宽构成。只有同窗 PMU 同时具备 retired instructions 与经核验的 DRAM bytes，才显示 Instruction/DRAM Byte。</p>
      {evidenceState === 'risk' && <div className="pm-note warning cs-classic-diagnostic"><CircleAlert size={14} /><span>该次 Roofline 有统计质量门未通过。平台仍保留屋顶与强度点并允许带风险发布；证据等级和失败项会随模型保存。</span></div>}
      {evidenceState === 'blocked' && <div className="pm-note warning cs-classic-diagnostic"><CircleAlert size={14} /><span>该次 Roofline 的来源、结构或完整性门禁未通过，仅保留诊断证据，不可发布，也不可参与寻优。</span></div>}
      <div className="cs-classic-roofline-grid">{[...groups].map(([coreId, roofs]) => <ClassicRooflineCard key={coreId} coreLabel={labels.get(coreId) ?? knownCoreLabels[coreId] ?? coreId} roofs={roofs} evidenceState={evidenceState} />)}</div>
    </> : <div className="cs-intensity-empty"><CircleAlert size={18} /><div><strong>当前校准版本没有数值计算强度扫描</strong><p>上方 compute / memory / mixed 只表示冻结代理的源码结构档。要判断“强度选择是否过高或过低”，可在高级方案中勾选“经典硬件 Roofline”，采集多个 FLOP/最小源码 Byte 强度档；应用阶段的 Instruction/DRAM Byte 仍需同窗 PMU。</p><small>该辅助扫描会显著增加核组 × OPP 的实测数量，因此不默认加入时延寻优的一键主校准。</small></div></div>}
  </div>
}

export function CalibrationFrequencyCurveGallery({ selectedFit, fits, versions }: {
  selectedFit?: CalibrationFitRecord | null
  fits: CalibrationFitRecord[]
  versions: CalibrationVersionRecord[]
}) {
  const sources = useMemo<CurveSource[]>(() => {
    const fitRecords = selectedFit ? [...fits.filter(value => value.id !== selectedFit.id), selectedFit] : fits
    const fitById = new Map(fitRecords.map(value => [value.id, value]))
    const published = versions.map(version => {
      const sourceFit = version.fit_id ? fitById.get(version.fit_id) : undefined
      const rejected = Array.isArray(sourceFit?.quality?.rejected_sample_details)
        ? sourceFit.quality.rejected_sample_details.map(record).map(item => String(item.sample_id ?? '')).filter(Boolean)
        : []
      const rejectedIndices = Array.isArray(sourceFit?.quality?.rejected_sample_details)
        ? sourceFit.quality.rejected_sample_details.map(record).map(item => finite(item.index)).filter((item): item is number => item != null && Number.isInteger(item) && item >= 0)
        : []
      return {
        id: `version:${version.id}`,
        label: `${version.name ?? version.id}${version.version ? ` · ${version.version}` : ''}`,
        status: version.status === 'risk_calibrated' ? 'published-risk' as const : 'published' as const,
        fitStatus: String(sourceFit?.status ?? 'published'),
        qualityPassed: sourceFit?.quality?.passed === true && version.status !== 'risk_calibrated',
        model: record(version.model),
        measurements: sourceFit?.measurements ? record(sourceFit.measurements) : null,
        observations: sourceFit?.frequency_curve_observations ? record(sourceFit.frequency_curve_observations) : null,
        rejectedSampleIds: new Set(rejected),
        rejectedSampleIndices: new Set(rejectedIndices),
      }
    })
    const publishedFitIds = new Set(versions.map(version => version.fit_id).filter(Boolean))
    const previews = fitRecords.filter(value => !publishedFitIds.has(value.id) || selectedFit?.id === value.id).map(value => {
      const qualityPassed = value.quality?.passed === true
      const publishable = Boolean(value.quality?.publishable ?? value.quality?.passed)
      const rejected = Array.isArray(value.quality?.rejected_sample_details)
        ? value.quality.rejected_sample_details.map(record).map(item => String(item.sample_id ?? '')).filter(Boolean)
        : []
      const rejectedIndices = Array.isArray(value.quality?.rejected_sample_details)
        ? value.quality.rejected_sample_details.map(record).map(item => finite(item.index)).filter((item): item is number => item != null && Number.isInteger(item) && item >= 0)
        : []
      return {
        id: `fit:${value.id}`,
        label: value.name ?? value.id,
        status: qualityPassed ? 'fit' as const : publishable ? 'fit-risk' as const : 'diagnostic' as const,
        fitStatus: String(value.status ?? (qualityPassed ? 'passed' : 'unknown')),
        qualityPassed,
        model: value.model_preview ? record(value.model_preview) : null,
        measurements: value.measurements ? record(value.measurements) : null,
        observations: value.frequency_curve_observations ? record(value.frequency_curve_observations) : null,
        rejectedSampleIds: new Set(rejected),
        rejectedSampleIndices: new Set(rejectedIndices),
      }
    })
    return [...published, ...previews]
  }, [fits, selectedFit, versions])
  const selectedPublished = selectedFit ? versions.find(version => version.fit_id === selectedFit.id) : undefined
  const selectedPublishable = Boolean(selectedFit?.quality?.publishable ?? selectedFit?.quality?.passed)
  const preferredSourceId = selectedPublishable && selectedFit
    ? selectedPublished ? `version:${selectedPublished.id}` : `fit:${selectedFit.id}`
    : sources.find(source => source.status !== 'diagnostic')?.id ?? sources[0]?.id ?? ''
  const [sourceId, setSourceId] = useState(preferredSourceId)
  useEffect(() => {
    setSourceId(current => sources.some(source => source.id === preferredSourceId)
      ? preferredSourceId
      : sources.some(source => source.id === current) ? current : sources[0]?.id ?? '')
  }, [preferredSourceId, sources])
  const source = sources.find(value => value.id === sourceId) ?? null
  const cards = useMemo(() => source ? buildCards(source) : [], [source])
  const evidenceState = source?.status === 'diagnostic' ? 'blocked' as const
    : source?.status === 'published-risk' || source?.status === 'fit-risk' ? 'risk' as const : 'normal' as const

  return <section className="pm-section cs-frequency-curves">
    <div className="pm-section-heading"><div><Gauge size={17} /><h3>任务类型频点性能曲线</h3></div>{source && <span className={'pm-badge ' + (evidenceState === 'normal' ? 'good' : 'warn')}>{source.status === 'published' ? '已发布模型' : source.status === 'published-risk' ? '已发布 · 有质量风险' : source.status === 'fit' ? '质量门禁通过 · 拟合预览' : source.status === 'fit-risk' ? '可发布 · 有质量风险' : '仅诊断 · 不可发布'}</span>}</div>
    <div className="cs-frequency-curve-toolbar">
      <label className="pm-field"><span>曲线来源</span><select value={sourceId} disabled={!sources.length} onChange={event => setSourceId(event.target.value)}><option value="" disabled>{sources.length ? '请选择校准结果' : '尚无校准结果'}</option>{sources.map(value => <option key={value.id} value={value.id}>{value.status === 'published' ? '已发布 · ' : value.status === 'published-risk' ? '已发布（有风险） · ' : value.status === 'fit' ? '门禁通过 · ' : value.status === 'fit-risk' ? `可发布（有风险） · ${value.fitStatus} · ` : `仅诊断（不可发布） · ${value.fitStatus} · `}{value.label}</option>)}</select></label>
      <p><Activity size={14} />每张图对应一个“核组 × 任务类型”。实测点取各 OPP 重复样本中位数，拟合线来自已保存的分段频响参数。</p>
    </div>
    <div className="pm-note cs-intensity-boundary"><Activity size={15} /><span><strong>这里的横轴是 CPU 频率，不是计算强度。</strong>每张卡分别给出冻结代理的源码静态强度合同和同窗 PMU 强度证据；二者都缺失时明确显示“未采集”，不会根据 compute / memory 标签反推数值。</span></div>
    {evidenceState === 'risk' && <div className="pm-note warning cs-frequency-diagnostic"><CircleAlert size={15} /><span><strong>当前结果有统计质量或覆盖门未通过。</strong>状态：{source?.fitStatus}。曲线可随风险模型参与寻优，但结论必须保留证据等级和适用域，优先补测失败项。</span></div>}
    {evidenceState === 'blocked' && <div className="pm-note warning cs-frequency-diagnostic"><CircleAlert size={15} /><span><strong>当前结果仅供诊断，不可发布。</strong>状态：{source?.fitStatus}。来源、结构、完整性门禁未通过或有效数据不足，曲线不能参与寻优；请先修复阻断项并重新拟合。</span></div>}
    {cards.length ? <div className="cs-frequency-curve-grid">{cards.map(card => <FrequencyCurveCard key={`${card.coreId}:${card.workloadClass}`} card={card} evidenceState={evidenceState} />)}</div>
      : <div className="pm-empty"><Gauge size={25} />尚无频点性能曲线。完成包含“任务类型频点性能曲线”的真机校准后，这里会按实际核组和任务类型自动展开。</div>}
    <ClassicRooflineEvidence model={source?.model ?? null} evidenceState={evidenceState} />
  </section>
}
