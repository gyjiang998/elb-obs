import { captureFailure, record, records, text, type CaptureDevice, type CapturePlan, type CaptureRun, type ParallelJob } from './parallel-modeling'
import type { JsonRecord } from './types'

export const captureAutoRepairKey = 'heterogeneous-scenario-capture-auto-repair-v1'

export type CaptureRepairIssue = { reason: string; code: string; step: string }
export type CaptureRepairTicket = {
  repair_request_id: string; source_job_id: string; source_run_id: string; plan_id: string
  device_serial: string; bridge_id: string; connection_mode: string
  status: 'pending' | 'submitted' | 'cancelled' | 'expired' | 'succeeded' | 'partial' | 'failed'
  retry_job_id?: string; retry_run_id?: string; retry_status?: string; expires_at?: string
}
export type CaptureRepairView = { status: 'creating' | 'submitting' | 'repairing' | 'retrying' | 'succeeded' | 'partial' | 'failed' | 'cancelled'; message: string; ticket?: CaptureRepairTicket }

export function captureRepairPrompt(ticket: CaptureRepairTicket, evidence: JsonRecord): string {
  return `本次用户主动采集失败，已启用“失败后自动修复并重试”。请读取以下运行清单、错误和日志引用，分析根因并修复采集计划、设备准备或采样器配置，然后重新采集一次并报告结果。\n${JSON.stringify(evidence, null, 2)}\n` +
    `本次修复请求 repair_request_id=${ticket.repair_request_id}。修复后调用 scenario_capture_execute，传入 repair_request_id=${ticket.repair_request_id}、plan_id（原计划 ${ticket.plan_id} 或修复后的新计划）、connection_mode=${ticket.connection_mode}、bridge_id=${ticket.bridge_id || '空字符串'}、device_serial=${ticket.device_serial}。保留用户选中的步骤与采集器。本次请求仅包含一次修复重采；通过该修复请求执行，平台会关联新任务并核验请求仍然有效。读取新任务最终结果并说明修复内容和采集质量。`
}

/** Partial PMU coverage alone is not a failed capture or a reason to replay it. */
export function captureRepairIssue(job: ParallelJob, run: CaptureRun): CaptureRepairIssue | null {
  if (job.status === 'cancelled' || run.status === 'cancelled') return null
  if (job.status === 'failed' || run.status === 'failed') {
    const failure = captureFailure(run, job.error)
    return { reason: failure?.reason ?? text(job.error?.message, '本次采集执行失败。'), code: text(job.error?.code), step: failure?.step ?? '采集任务' }
  }
  if (run.status !== 'partial' && job.status !== 'partial') return null
  const manifest = record(run.manifest)
  const preparation = record(manifest.pmu_preparation ?? run.pmu_preparation)
  const failedProbe = ['probe_failed', 'probe_timed_out', 'deployment_failed', 'bundle_missing', 'bundle_invalid', 'incompatible', 'not_executable', 'custom_path_missing'].includes(text(preparation.status))
    || /^PMU_(?:SAMPLER_(?:PROBE|CAPABILITIES)|CAPABILITY)_/.test(text(preparation.code))
  if (preparation.allow_execution === false || failedProbe) return {
    reason: text(preparation.message, text(preparation.reason, 'PMU 采样器准备失败，本次未执行 PMU 采样。')),
    code: text(preparation.code), step: 'PMU 采样器准备',
  }
  for (const step of records(manifest.steps)) {
    if (step.captured === false) continue
    const validation = record(step.pmu_validation)
    const gate = record(validation.sampler_preparation)
    if (gate.allow_execution === false) return {
      reason: text(gate.message, text(validation.reason, 'PMU 采样器准备失败，本次未执行 PMU 采样。')),
      code: text(gate.code), step: text(step.title, text(step.step_id)),
    }
    const pmuStatus = record(step.collector_status).pmu
    if (['failed', 'unavailable', 'invalid', 'missing'].includes(text(pmuStatus, text(record(pmuStatus).status)))) {
      const diagnostic = records(validation.diagnostics).find(item => item.severity === 'error') ?? records(validation.diagnostics)[0]
      return { reason: text(validation.reason, text(diagnostic?.message, '本次 PMU 采集未获得有效数据。')),
        code: text(diagnostic?.code), step: text(step.title, text(step.step_id)) }
    }
  }
  return null
}

export function captureRepairEvidence(job: ParallelJob, run: CaptureRun, plan: CapturePlan, device: CaptureDevice, issue: CaptureRepairIssue): JsonRecord {
  const manifest = record(run.manifest)
  return {
    source_job_id: job.job_id, source_run_id: run.run_id, source_plan_id: plan.plan_id,
    connection_mode: device.connection_mode, bridge_id: device.bridge_id, device_serial: device.device_serial,
    failure: issue, job_error: job.error, manifest_artifact_id: run.manifest_artifact_id ?? null,
    run_api: `/v1/scenario-capture/runs/${encodeURIComponent(run.run_id)}`,
    plan_api: `/v1/scenario-capture/plans/${encodeURIComponent(plan.plan_id)}`,
    remote_root: manifest.remote_root ?? null, pmu_preparation: manifest.pmu_preparation ?? run.pmu_preparation ?? null,
    errors: record(manifest.quality).errors ?? [],
    steps: records(manifest.steps).filter(step => step.status === 'failed' || step.captured !== false).map(step => ({
      step_id: step.step_id, title: step.title, status: step.status, collector_status: step.collector_status,
      error: step.error, pmu_diagnostics: record(step.pmu_validation).diagnostics,
      pmu_preparation: record(step.pmu_validation).sampler_preparation,
    })),
    log_references: records(manifest.files).filter(file => /log|preparation|diagnostic|status|script/.test(text(file.kind)) || /\.log$/.test(text(file.name)))
      .map(file => ({ step_id: file.step_id, kind: file.kind, relative_path: file.relative_path, storage_locations: file.storage_locations })),
  }
}
