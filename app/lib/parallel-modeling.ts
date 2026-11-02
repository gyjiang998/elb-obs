'use client'

import { useEffect, useState } from 'react'
import { dispatchDeferredAgentTask, parallelAgentTaskDetail, type ParallelAgentTaskKind } from './agent-task-submission'
import { api } from './api'
import { randomUuid } from './random-id'
import type { JsonRecord } from './types'

export type CaptureStep = { step_id: string; ordinal: number; title: string; operation: string; slug?: string; target?: string | null; expected_targets?: string[]; template_step?: number; selector?: JsonRecord; key?: string; seconds?: number; points?: number[]; velocity?: number }
export type CaptureSettings = { selected_step_ids: string[]; collectors: string[]; preparation_mode: 'auto' | 'already_prepared'; pmu: { binary_path: string; period_us: number; duration_ms: number; group: string; reader_cpu: number } }
export type CapturePlanDraft = { template_id: string; title: string; natural_language_steps: string; steps: CaptureStep[]; profile: JsonRecord; capture: CaptureSettings }
export type CapturePlan = CapturePlanDraft & { plan_id: string; artifact_id?: string; created_at?: string; readiness?: { status: string; blockers: JsonRecord[] }; execution_step_ids?: string[]; preparation_step_ids?: string[] }
export type DeviceBridge = JsonRecord & { bridge_id: string; display_name?: string; hostname?: string; connection_state?: string; capabilities?: JsonRecord }
export type CaptureConnectionMode = 'auto' | 'local' | 'bridge'
export type CaptureDevice = { device_serial: string; connection_mode: 'local' | 'bridge'; bridge_id: string; display_name: string; connection_label: string }
export type CaptureCatalog = { templates: { template_id: string; name: string; steps: CaptureStep[] }[]; default_template_id?: string; default_profile: JsonRecord; default_capture: CaptureSettings; operations: string[]; bridges?: DeviceBridge[]; pmu_sampler?: JsonRecord; connection_modes?: { id: CaptureConnectionMode; name: string }[]; default_connection_mode?: CaptureConnectionMode; local_hdc?: JsonRecord & { available?: boolean; binary_path?: string | null } }
export type CaptureRun = JsonRecord & { run_id: string; plan_id: string; status: string; manifest?: JsonRecord; manifest_artifact_id?: string; created_at?: string; connection_mode?: CaptureConnectionMode; bridge_id?: string; device_serial?: string }
export type ModelingDataset = JsonRecord & { id: string; name: string; status: string; source_kind?: string; hardware_profile_id?: string; summary?: JsonRecord; provenance?: JsonRecord; quality?: JsonRecord }
export type ModelComponent = JsonRecord & { id: string; name: string; wall_ms: number; cpu_ms: number; binder_blocked_ms?: number; request_ids?: string[]; pmu?: JsonRecord }
export type ModelAnalysis = JsonRecord & { id: string; analysis_id?: string; dataset_id: string; status: string; baseline: JsonRecord; replay_gate?: { passed: boolean; errors?: unknown }; components: ModelComponent[]; statistics?: JsonRecord; graphs?: { observed?: unknown; parallel?: unknown }; assumptions?: unknown[]; artifacts?: unknown }
export type ModelCandidate = JsonRecord & { id?: string; configuration?: JsonRecord; metrics?: JsonRecord; comparison?: JsonRecord; critical_chain?: unknown; timeline?: unknown; graph?: unknown; frequency_vector?: unknown }
export type CoreCombinationRecommendation = ModelCandidate & {
  combination_id: string; combination_label: string; physical_core_counts: Record<string, number>; physical_core_count: number
  candidate_count: number; nominal_feasible_count?: number; selection_status: 'objective_optimum' | 'budget_infeasible'
  recommendation_basis: 'nominal_objective' | 'declared_scenarios_objective' | 'fastest_nominal_fallback'
  fastest_configuration_index?: number; fastest_finish_ms?: number; global_fastest_finish_ms?: number
  latency_gap_ms?: number; latency_gap_pct?: number; rank?: number
}
export type ModelSearch = JsonRecord & { id: string; search_id?: string; analysis_id: string; status: string; objective: string; best?: ModelCandidate | null; candidates?: ModelCandidate[]; core_combination_recommendations?: CoreCombinationRecommendation[]; search_configuration?: JsonRecord; search_provenance?: JsonRecord; assumptions?: unknown[]; artifacts?: unknown }
export type ModelingCatalog = { builtins: { id: string; name: string; description: string; metrics?: JsonRecord }[]; hardware_profiles: (JsonRecord & { id: string; name: string; core_groups?: JsonRecord[]; frequency_domains?: unknown })[]; model_packages: (JsonRecord & { id: string; name: string; version?: string; status?: string; description?: string; hardware_profile_id?: string })[]; objectives: { id: string; name: string }[] }
export type ParallelJob = { job_id: string; kind: string; status: 'queued' | 'running' | 'succeeded' | 'partial' | 'failed' | 'cancelled'; progress: JsonRecord; result: JsonRecord | null; error: { code?: string; message?: string; details?: JsonRecord } | null; created_at?: string; updated_at?: string; run_id?: string }
export type JobReceipt = Partial<ParallelJob> & { job_id: string; run_id?: string }
export type Notice = { tone: 'info' | 'success' | 'error'; text: string }
export type WorkloadCaptureDescriptor = { runId?: string; stepId?: string; planTitle?: string; title?: string; stepOrdinal?: number | null; stepTitle?: string }

export function record(value: unknown): JsonRecord { return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {} }
export function records(value: unknown): JsonRecord[] { return Array.isArray(value) ? value.map(record).filter((item) => Object.keys(item).length > 0) : [] }
export function text(value: unknown, fallback = ''): string { return typeof value === 'string' && value.trim() ? value.trim() : fallback }
export function numeric(value: unknown): number | null { return typeof value === 'number' && Number.isFinite(value) ? value : null }
export function metric(value: unknown, digits = 3, unit = ''): string { const n = numeric(value); return n === null ? '—' : `${n.toLocaleString('zh-CN', { maximumFractionDigits: digits, minimumFractionDigits: digits })}${unit ? ` ${unit}` : ''}` }
export function timestamp(value: unknown): string { if (!text(value)) return '—'; const date = new Date(text(value)); return Number.isNaN(date.getTime()) ? text(value) : date.toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) }
export function errorText(error: unknown): string { return error instanceof Error ? error.message : '请求未完成，请重试。' }
export function workloadCaptureKey(value: WorkloadCaptureDescriptor | JsonRecord | unknown): string {
  const source = record(value)
  const runId = text(source.runId ?? source.run_id)
  const stepId = text(source.stepId ?? source.step_id)
  return runId && stepId ? `${runId}\u0000${stepId}` : ''
}
export function workloadCaptureLabel(value: WorkloadCaptureDescriptor | JsonRecord | unknown, fallback = ''): string {
  const source = record(value)
  const planTitle = text(source.planTitle ?? source.plan_title)
  const stepTitle = text(source.stepTitle ?? source.step_title)
  const explicit = numeric(source.stepOrdinal ?? source.step_ordinal)
  const matched = text(source.stepId ?? source.step_id).match(/^step[_-]0*([1-9][0-9]*)$/i)
  const ordinal = explicit !== null && Number.isInteger(explicit) && explicit > 0 ? explicit : matched ? Number(matched[1]) : null
  const step = ordinal !== null ? `Step ${ordinal}${stepTitle ? `: ${stepTitle}` : ''}` : stepTitle
  const label = planTitle ? `[${planTitle}]${step ? ` ${step}` : ''}` : step
  return label || fallback
}
export function workloadDatasetLabel(dataset: ModelingDataset, sources: WorkloadCaptureDescriptor[] = []): string {
  const capture = record(dataset.provenance).capture
  const provenance = record(capture)
  const key = workloadCaptureKey(provenance)
  const local = key ? sources.find(source => workloadCaptureKey(source) === key) : undefined
  return workloadCaptureLabel(local ?? provenance, text(dataset.name, '未命名负载'))
}
export function captureDeviceKey(device: CaptureDevice): string { return JSON.stringify([device.connection_mode, device.bridge_id, device.device_serial]) }
export function captureDevices(value: unknown): CaptureDevice[] {
  const source = record(value)
  const devices = new Map<string, CaptureDevice>()
  for (const item of records(source.devices)) {
    const serial = text(item.device_serial ?? item.serial)
    const mode = item.connection_mode
    const bridgeId = mode === 'bridge' ? text(item.bridge_id) : ''
    if (!serial || (mode !== 'local' && mode !== 'bridge') || (mode === 'bridge' && !bridgeId)) continue
    const device: CaptureDevice = { device_serial: serial, connection_mode: mode, bridge_id: bridgeId, display_name: text(item.display_name, serial), connection_label: text(item.connection_label, mode === 'local' ? '本机 HDC 直连' : '远程采集端') }
    devices.set(captureDeviceKey(device), device)
  }
  return [...devices.values()]
}
export type CaptureFailure = { step: string; reason: string; suggestion: string }

/** Older runs kept the failure only in the manifest; use the same summary for live and historical results. */
export function captureFailure(value: unknown, fallback?: unknown): CaptureFailure | null {
  const run = record(value)
  const manifest = record(run.manifest)
  const errors = record(manifest.quality).errors
  const manifestError = Array.isArray(errors) ? errors.find((item) => text(item) || text(record(item).reason ?? record(item).message)) : undefined
  const error = record(manifestError ?? run.error ?? fallback)
  const details = record(error.details)
  const cleanup = record(manifest.cleanup)
  const rawReason = text(manifestError, text(error.reason ?? error.message, text(run.error, text(fallback, cleanup.status === 'pending' ? text(cleanup.reason) : ''))))
  if (!rawReason) return null
  const steps = records(manifest.steps)
  const stepId = text(error.step_id ?? details.step_id)
  const failedStep = steps.find((step) => stepId && step.step_id === stepId) ?? [...steps].reverse().find((step) => step.status === 'failed')
  const stepTitle = text(failedStep?.title, text(error.step_title ?? details.step_title, stepId))
  const ordinal = numeric(failedStep?.ordinal)
  const step = stepTitle ? `${ordinal !== null ? `第 ${ordinal} 步 · ` : ''}${stepTitle}` : cleanup.status === 'pending' ? '采集结束清理' : '设备与采集环境检查'
  let reason = rawReason
  let suggestion = text(error.suggestion ?? error.recovery_hint ?? details.suggestion ?? details.recovery_hint)
  if (/Launch step requires.*Gallery is already visible/i.test(rawReason)) {
    reason = '当前已在图库内，“启动图库”步骤要求从解锁后的桌面开始。'
    suggestion ||= '回到桌面后重新执行。若只采集后续操作，可勾选对应步骤并选择“界面已准备”。'
  } else if (/Gallery is not visible/i.test(rawReason)) {
    reason = '未识别到当前界面中的图库应用，步骤前置或完成状态校验未通过。'
    suggestion ||= '确认手机已解锁且图库位于前台。采集后续操作时，可准备目标界面并选择“界面已准备”后重试。'
  } else if (/Trace state unavailable|another trace is active/i.test(rawReason)) {
    reason = '无法确认 Trace 可用状态，或设备上已有其他 Trace 采集正在运行。'
    suggestion ||= '结束设备上其他 Trace 采集后重试，并确认设备支持 HiTrace 采集。'
  } else if (/No visible gallery control matches|Ambiguous selector/i.test(rawReason)) {
    reason = '当前界面未能唯一定位该步骤需要操作的控件。'
    suggestion ||= '准备该步骤对应的图库界面后重试，或让页面助手依据当前操作步骤重新生成计划。'
  } else if (/UI layout could not be recovered|UI dump does not expose/i.test(rawReason)) {
    reason = '未能读取用于确认操作界面的 UI 布局。'
    suggestion ||= '保持手机解锁，重新发现设备后重试。'
  } else if (/Screen\/layout size.*does not match profile/i.test(rawReason)) {
    reason = '当前设备的界面尺寸与采集计划不匹配。'
    suggestion ||= '确认设备方向与计划一致，或让页面助手为当前设备重新生成采集计划。'
  }
  suggestion ||= '确认设备连接及当前操作界面后重试；下载采集清单可保留此次失败记录。'
  return { step, reason, suggestion }
}
export function statusLabel(status: string): string { return ({ queued: '已排队', running: '执行中', succeeded: '已完成', completed: '已完成', ready: '可用', ready_for_device_preflight: '待设备预检', needs_input: '需补充操作', partial: '部分采集', failed: '失败', cancelled: '已取消', connected: '在线', offline: '离线', collected: '已采集', unavailable: '不可用', invalid: '数据无效', exited_early: '提前退出', imported: '已导入', unresolved: '待解析', cancelled_by_user: '已取消' } as Record<string, string>)[status] ?? status }
export function isTerminal(status?: string): boolean { return ['succeeded', 'partial', 'failed', 'cancelled'].includes(status ?? '') }
export function jobPercent(job: ParallelJob | null): number | null { const p = job?.progress ?? {}; const direct = numeric(p.percent ?? p.percentage ?? p.progress_percent ?? p.progress); if (direct !== null) return Math.max(0, Math.min(100, direct)); const done = numeric(p.completed ?? p.completed_count ?? p.configurations_evaluated ?? p.current); const total = numeric(p.total ?? p.total_count ?? p.configurations_total); return done !== null && total && total > 0 ? Math.min(100, done / total * 100) : null }
export function downloadText(filename: string, content: string, mime = 'application/json'): void { const url = URL.createObjectURL(new Blob([content], { type: mime })); const anchor = document.createElement('a'); anchor.href = url; anchor.download = filename; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000) }
export function post<T>(path: string, body: unknown): Promise<T> { return api<T>(path, { method: 'POST', body: JSON.stringify(body) }) }

/** Poll only server-reported state. A network failure preserves the job and retries. */
export function useParallelJob(jobId: string, requestOwner = '') {
  const [job, setJob] = useState<ParallelJob | null>(null)
  const [pollError, setPollError] = useState('')
  useEffect(() => {
    setJob(null); setPollError('')
    if (!jobId) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    let attempts = 0
    const poll = async () => {
      try {
        const next = await api<ParallelJob>(`/v1/parallel-jobs/${encodeURIComponent(jobId)}`, { signal: controller.signal }, { owner: requestOwner })
        if (controller.signal.aborted) return
        setJob(next); setPollError(''); attempts = 0
        if (!isTerminal(next.status)) timer = setTimeout(poll, 1500)
      } catch (error) {
        if (controller.signal.aborted) return
        setPollError(errorText(error)); attempts += 1
        timer = setTimeout(poll, Math.min(15000, 1500 * 2 ** Math.min(attempts, 3)))
      }
    }
    void poll()
    return () => { controller.abort(); if (timer) clearTimeout(timer) }
  }, [jobId, requestOwner])
  return { job, pollError }
}

export function dispatchParallelAgent(kind: ParallelAgentTaskKind, context: JsonRecord, message: string): string {
  const requestId = randomUuid()
  dispatchDeferredAgentTask(window, parallelAgentTaskDetail(kind, context, message, requestId))
  return requestId
}

export function bridgeSerials(bridge?: DeviceBridge): string[] {
  const diagnostic = record(record(bridge?.capabilities).runtime_diagnostics)
  const stdout = text(record(record(diagnostic.hdc).targets).stdout)
  return Array.from(new Set(stdout.split(/\r?\n/).map((line) => line.trim().split(/\s+/)[0]).filter((line) => line && !line.startsWith('[') && line !== 'Empty' && line !== 'List')))
}

/** Build a diagram from actual backend graph nodes/edges; no synthetic timings. */
export function graphMermaid(graph: unknown): string {
  if (typeof graph === 'string') return graph
  const source = record(graph)
  if (typeof source.mermaid === 'string') return source.mermaid
  const nodes = records(source.nodes)
  if (!nodes.length) return ''
  const ids = new Map(nodes.map((node, i) => [String(node.id ?? node.node_id ?? i), `n${i}`]))
  const label = (value: unknown) => String(value ?? '').replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replace(/[\r\n]/g, ' ')
  const lines = ['flowchart LR', ...nodes.map((node, i) => `n${i}["${label(node.label ?? node.name ?? node.id ?? node.node_id)}${numeric(node.duration_ms) !== null ? ` · ${metric(node.duration_ms)} ms` : ''}"]`)]
  const rawEdges = Array.isArray(source.edges) ? source.edges : Array.isArray(source.links) ? source.links : []
  for (const value of rawEdges) {
    const edge = record(value)
    const from = ids.get(String(Array.isArray(value) ? value[0] : edge.source ?? edge.from ?? edge.src))
    const to = ids.get(String(Array.isArray(value) ? value[1] : edge.target ?? edge.to ?? edge.dst))
    if (from && to) lines.push(`${from} --> ${to}`)
  }
  return lines.join('\n')
}
export function frequencyText(value: unknown): string {
  const entries = Object.entries(record(value))
  return entries.length ? entries.map(([key, item]) => `${({big:'大核',mid:'中核',middle:'中核',little:'小核',small:'小核'} as Record<string,string>)[key] ?? key} ${numeric(item) !== null ? `${metric(Number(item) / 1000, 0)} MHz` : '未使用'}`).join(' · ') : '—'
}
