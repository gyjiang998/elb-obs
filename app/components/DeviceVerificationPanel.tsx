'use client'

import { BrainCircuit, CheckCircle2, Circle, Code2, Cpu, FlaskConical, HardDrive, LoaderCircle, Play, RefreshCw, ShieldAlert, ShieldCheck, Terminal, TriangleAlert, WandSparkles } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, ApiError } from '../lib/api'
import { agentAutonomousTaskStatusEventName } from '../lib/agent-command-bridge'
import { appendAnalysisTraceEvent } from '../lib/analysis-observability'
import type { AnalysisDeviceValidationRecord } from '../lib/analysis-history'
import { buildDeviceDispatchPromptPlan } from '../lib/device-dispatch'
import { buildDeviceAdapterPromptPlan } from '../lib/device-adapter'
import { buildDeviceValidationTask } from '../lib/device-validation-task'
import { deviceValidationTerminal, type DeviceValidationFacts } from '../lib/agent-runtime-contract'
import {
  deviceValidationSnapshotEvent,
  deviceValidationSnapshotStorageKey,
  normalizeDeviceValidationSnapshot,
  readDeviceValidationSnapshot,
  type DeviceValidationSnapshot,
} from '../lib/device-validation-snapshot'
import type { BrowserModelConfig } from '../lib/model-config'
import { requestModelChatCompletionStream, type ModelStreamProgress } from '../lib/model-client'
import { randomUuid } from '../lib/random-id'
import { respondReasoningLabAction } from '../lib/reasoning-action-bridge'
import { mergeModelTokenUsage, parseOrRepairModelJson } from '../lib/structured-model-output'
import { formatTokenCount, type ModelTokenUsage, type PromptTokenBudget } from '../lib/token-budget'
import type { Artifact, DeploymentCapabilities, JsonRecord } from '../lib/types'
import { useWorkbenchErrorNotification } from './WorkbenchErrorNotification'

type DeviceUnit = 'npu' | 'gpu' | 'dsp' | 'media' | 'parallel_cpu'
type BusyAction = 'discover' | 'probe' | 'assess' | 'adapter' | 'adapter_run' | 'remote_shell' | 'plan' | 'run' | 'experiment_stage' | 'experiment_build' | 'experiment_agent' | null
type DispatchState = 'idle' | 'preparing' | 'reasoning' | 'structuring' | 'saving' | 'succeeded' | 'warning' | 'failed'

type DeviceDiscoveryResponse = {
  hdc: JsonRecord
  devices: Array<{ serial: string; connection_state?: string }>
}

type StrategyResultOption = {
  strategyId: string
  strategyLabel: string
  artifact: Artifact
}

type DispatchContextResponse = { artifact: Artifact }
type DispatchAssessmentResponse = { artifact: Artifact; validation_errors: string[] }
type AdapterContextResponse = { artifact: Artifact }
type AdapterResponse = { artifact: Artifact; validation_errors: string[] }

type DispatchAttempt = {
  index: number
  status: 'running' | 'succeeded' | 'warning' | 'failed'
  summary: string
  artifactId?: string
  decision?: string
  validationErrors?: string[]
}

type SafeActionResult = JsonRecord & {
  status: 'succeeded' | 'warning' | 'failed'
}

function record(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {}
}

function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' && value.trim() ? value.trim() : fallback
}

function values(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string' && item.trim().length > 0) : []
}

function unitLabel(unit: string): string {
  return ({ npu: 'NPU', gpu: 'GPU', dsp: 'DSP', media: '媒体硬件编解码', parallel_cpu: 'CPU 并行优化' })[unit] ?? unit
}

function observationLabel(status: string): string {
  return ({ observed: '已观测', not_observed: '未观测' })[status] ?? '待探测'
}

function planStatusLabel(status: string): string {
  return ({ ready_for_user_confirmation: '已登记可执行', evidence_required: '需登记目标配方' })[status] ?? (status || '尚未生成')
}

function runStatusLabel(status: string): string {
  return ({ passed: '功能验证通过', unsupported: '当前设备不支持该受控图', failed: '功能验证未通过' })[status] ?? (status || '尚未执行')
}

function remoteShellStatusLabel(status: string): string {
  return ({ succeeded: '远端命令完成', failed: '远端命令返回失败', timed_out: '远端命令等待超时' })[status] ?? (status || '尚未执行')
}

function summaryFromPlan(plan: Artifact): string {
  return text(plan.planning_reason_zh, '已生成真机验证方案。')
}

function referenceMatchLabel(level: string): string {
  return ({ byte_exact: '字节严格匹配', terminal_newline_normalized: '仅末尾换行规范化匹配' })[level] ?? '未返回'
}

function dispatchDecisionLabel(decision: string): string {
  return ({ semantic_candidate: '语义下发候选', needs_contract: '仍需补齐下发合同', not_suitable: '当前单元不适配' })[decision] ?? (decision || '未形成判断')
}

function recipeScopeLabel(scope: string): string {
  return ({ platform_baseline_preflight: '平台基线预检', target_reference_validation: '已登记参考实现验证' })[scope] ?? '受控配方'
}

function dispatchStateLabel(state: DispatchState): string {
  return ({
    idle: '等待判断',
    preparing: '准备上下文',
    reasoning: '模型推理中',
    structuring: '整理合同',
    saving: '保存判断',
    succeeded: '判断完成',
    warning: '需要补证',
    failed: '判断未完成',
  })[state]
}

function renderLines(value: unknown, fallback = '未返回'): string {
  const lines = values(value)
  return lines.length > 0 ? lines.join('；') : fallback
}

const activeAgentRunStates = new Set(['created', 'running', 'waiting_for_tool', 'waiting_for_ui', 'waiting_for_device', 'compacting'])

function autonomousValidationStatus(snapshot: DeviceValidationSnapshot): { label: string; tone: 'running' | 'success' | 'warning' | 'failed' } {
  if (activeAgentRunStates.has(snapshot.status)) return { label: '验证进行中', tone: 'running' }
  if (snapshot.status === 'completed') {
    return snapshot.facts.experimentExecution
      ? { label: '已取得真机执行结果', tone: 'success' }
      : { label: '验证已结束，证据未闭合', tone: 'warning' }
  }
  if (snapshot.status === 'failed') return { label: '验证未完成', tone: 'failed' }
  if (snapshot.status === 'cancelled') return { label: '验证已停止', tone: 'warning' }
  if (snapshot.status === 'waiting_for_model_credential') return { label: '等待模型连接', tone: 'warning' }
  return { label: '验证已暂停', tone: 'warning' }
}

function latestActionStatus(facts: DeviceValidationFacts, actionIds: string[]): 'warning' | 'pending' {
  const latest = [...facts.actionFacts].reverse().find((item) => actionIds.includes(item.actionId))
  return latest && (latest.status === 'failed' || latest.status === 'warning') ? 'warning' : 'pending'
}

function autonomousPipeline(snapshot: DeviceValidationSnapshot) {
  const facts = snapshot.facts
  const stages = [
    { id: 'device', label: '设备探测', branch: '设备分支', done: Boolean(facts.probeArtifactId), actions: ['device.list_bridges', 'device.discover', 'device.create_lease', 'device.probe'] },
    { id: 'profile', label: '构建面', branch: '构建分支', done: facts.profileIds.length > 0, actions: ['build.describe_profile', 'device.describe_experiment_profiles'] },
    { id: 'source', label: '实验源码', branch: '构建分支', done: Boolean(facts.stagedSourceArtifactId), actions: ['build.stage_source', 'device.stage_experiment_source'] },
    { id: 'build', label: '后端构建', branch: '构建分支', done: Boolean(facts.buildResultArtifactId), actions: ['build.create', 'device.build_upload_experiment'] },
    { id: 'deploy', label: '部署设备', branch: '汇合阶段', done: facts.deploymentVerified === true || Boolean(facts.experimentRunArtifactId && facts.remoteBinaryPath), actions: ['device.deploy_artifact', 'device.build_upload_experiment'] },
    { id: 'execute', label: '设备执行', branch: '汇合阶段', done: Boolean(facts.experimentExecution), actions: ['device.execute_shell'] },
  ]
  const active = activeAgentRunStates.has(snapshot.status)
  return stages.map((stage) => ({
    ...stage,
    status: latestActionStatus(facts, stage.actions) === 'warning'
      ? 'warning'
      : stage.done
        ? 'succeeded'
        : active && Boolean(snapshot.activeActionId) && stage.actions.includes(snapshot.activeActionId || '')
          ? 'running'
          : 'pending',
  }))
}

export function DeviceVerificationPanel({
  deployment,
  sourceConnectorId,
  analysisRecordId,
  codeUnit,
  strategyResults,
  suggestedUnit,
  modelConfig,
  onValidationRecorded,
}: {
  deployment?: DeploymentCapabilities
  sourceConnectorId: string
  analysisRecordId?: string | null
  codeUnit: Artifact | null
  strategyResults: StrategyResultOption[]
  suggestedUnit: string | null
  modelConfig: BrowserModelConfig
  onValidationRecorded: (record: AnalysisDeviceValidationRecord) => void
}) {
  const [discovery, setDiscovery] = useState<DeviceDiscoveryResponse | null>(null)
  const [selectedSerial, setSelectedSerial] = useState('')
  const [probe, setProbe] = useState<Artifact | null>(null)
  const [selectedUnit, setSelectedUnit] = useState<DeviceUnit>('gpu')
  const [customRouteProfileOverridden, setCustomRouteProfileOverridden] = useState(false)
  const [selectedResultId, setSelectedResultId] = useState('')
  const [plan, setPlan] = useState<Artifact | null>(null)
  const [run, setRun] = useState<Artifact | null>(null)
  const [dispatchContext, setDispatchContext] = useState<Artifact | null>(null)
  const [dispatchAssessment, setDispatchAssessment] = useState<Artifact | null>(null)
  const [dispatchState, setDispatchState] = useState<DispatchState>('idle')
  const [dispatchThought, setDispatchThought] = useState('')
  const [dispatchOutput, setDispatchOutput] = useState('')
  const [dispatchFormatRecovery, setDispatchFormatRecovery] = useState('')
  const [dispatchTokenUsage, setDispatchTokenUsage] = useState<ModelTokenUsage | null>(null)
  const [dispatchTokenBudget, setDispatchTokenBudget] = useState<PromptTokenBudget | null>(null)
  const [dispatchStreamCounts, setDispatchStreamCounts] = useState({ reasoningChars: 0, contentChars: 0 })
  const [dispatchAttempts, setDispatchAttempts] = useState<DispatchAttempt[]>([])
  const [adapterContext, setAdapterContext] = useState<Artifact | null>(null)
  const [generatedAdapter, setGeneratedAdapter] = useState<Artifact | null>(null)
  const [adapterRun, setAdapterRun] = useState<Artifact | null>(null)
  const [adapterState, setAdapterState] = useState<DispatchState>('idle')
  const [adapterThought, setAdapterThought] = useState('')
  const [adapterOutput, setAdapterOutput] = useState('')
  const [adapterFormatRecovery, setAdapterFormatRecovery] = useState('')
  const [adapterTokenUsage, setAdapterTokenUsage] = useState<ModelTokenUsage | null>(null)
  const [adapterTokenBudget, setAdapterTokenBudget] = useState<PromptTokenBudget | null>(null)
  const [adapterStreamCounts, setAdapterStreamCounts] = useState({ reasoningChars: 0, contentChars: 0 })
  const [adapterAttempts, setAdapterAttempts] = useState<DispatchAttempt[]>([])
  const [remoteShellRuns, setRemoteShellRuns] = useState<Artifact[]>([])
  const [experimentSources, setExperimentSources] = useState<Artifact[]>([])
  const [experimentRuns, setExperimentRuns] = useState<Artifact[]>([])
  const [autonomousSnapshot, setAutonomousSnapshot] = useState<DeviceValidationSnapshot | null>(null)
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState<BusyAction>(null)
  const [notice, setNotice] = useState<{ tone: 'error' | 'success' | 'info'; text: string } | null>(null)
  const autonomousDispatchRequestRef = useRef<string | null>(null)
  const autonomousDispatchTimeoutRef = useRef<number | null>(null)

  const selectedResult = useMemo(
    () => strategyResults.find((item) => item.artifact.artifact_id === selectedResultId) ?? strategyResults[0] ?? null,
    [selectedResultId, strategyResults],
  )
  const probeDevice = record(probe?.device)
  const observedUnits = record(probe?.observed_units)
  const selectedObservation = record(observedUnits[selectedUnit])
  const planStatus = text(plan?.plan_status)
  const planRecipe = record(plan?.recipe_contract)
  const planConfirmation = record(plan?.confirmation)
  const planDispatch = record(plan?.dispatch_assessment)
  const recipeScope = text(planRecipe.recipe_scope)
  const runData = record(run?.execution)
  const runEvidence = record(run?.observed_evidence)
  const runUnit = text(run?.requested_unit)
  const runEvidenceLabel = runUnit === 'gpu' ? 'GPU' : runUnit === 'npu' ? 'NNRT 加速器' : '运行单元'
  const runEvidenceValue = runUnit === 'gpu'
    ? text(runEvidence.renderer, '未返回')
    : runUnit === 'npu'
      ? text(runEvidence.accelerator_name, '未返回')
      : '未返回'
  const dispatchResult = record(dispatchAssessment?.result)
  const dispatchValidation = record(dispatchResult.validation)
  const dispatchContract = record(dispatchResult.dispatch_contract)
  const adapterResult = record(generatedAdapter?.result)
  const adapterValidation = record(adapterResult.validation)
  const adapterMaterialization = record(generatedAdapter?.materialization)
  const adapterRunExecution = record(adapterRun?.execution)
  const autonomousState = autonomousSnapshot ? autonomousValidationStatus(autonomousSnapshot) : null
  const autonomousTerminal = autonomousSnapshot ? deviceValidationTerminal(autonomousSnapshot.facts) : null
  const autonomousStages = autonomousSnapshot ? autonomousPipeline(autonomousSnapshot) : []
  const autonomousRunActive = autonomousSnapshot ? activeAgentRunStates.has(autonomousSnapshot.status) : false
  const customSuggestedUnit = suggestedUnit?.startsWith('custom:') ? suggestedUnit : ''

  const clearAutonomousDispatch = useCallback(() => {
    autonomousDispatchRequestRef.current = null
    if (autonomousDispatchTimeoutRef.current !== null) {
      window.clearTimeout(autonomousDispatchTimeoutRef.current)
      autonomousDispatchTimeoutRef.current = null
    }
    setBusy((current) => current === 'experiment_agent' ? null : current)
  }, [])

  useEffect(() => {
    const listener = (event: Event) => {
      const detail = record((event as CustomEvent<unknown>).detail)
      const requestId = text(detail.request_id)
      if (!requestId || requestId !== autonomousDispatchRequestRef.current) return
      if (text(detail.status) === 'rejected') {
        clearAutonomousDispatch()
        setNotice({ tone: 'error', text: text(detail.message, '页面助手未接收本次真机验证任务。') })
        return
      }
      if (autonomousDispatchTimeoutRef.current !== null) {
        window.clearTimeout(autonomousDispatchTimeoutRef.current)
        autonomousDispatchTimeoutRef.current = null
      }
      setNotice({ tone: 'info', text: '页面助手已接收真机验证任务，正在创建可观测的 Agent Run。' })
    }
    window.addEventListener(agentAutonomousTaskStatusEventName, listener)
    return () => window.removeEventListener(agentAutonomousTaskStatusEventName, listener)
  }, [clearAutonomousDispatch])

  useEffect(() => {
    if (!autonomousSnapshot || autonomousSnapshot.requestId !== autonomousDispatchRequestRef.current) return
    clearAutonomousDispatch()
  }, [autonomousSnapshot, clearAutonomousDispatch])

  useEffect(() => () => {
    if (autonomousDispatchTimeoutRef.current !== null) window.clearTimeout(autonomousDispatchTimeoutRef.current)
  }, [])

  useEffect(() => {
    if (suggestedUnit && ['npu', 'gpu', 'dsp', 'media', 'parallel_cpu'].includes(suggestedUnit)) {
      setSelectedUnit(suggestedUnit as DeviceUnit)
      setCustomRouteProfileOverridden(false)
    } else if (suggestedUnit?.startsWith('custom:')) {
      setCustomRouteProfileOverridden(false)
    }
  }, [suggestedUnit])

  useEffect(() => {
    const analysisId = analysisRecordId?.trim() ?? ''
    if (!analysisId) {
      setAutonomousSnapshot(null)
      return
    }
    setAutonomousSnapshot(readDeviceValidationSnapshot(analysisId))
    const adopt = (value: unknown) => {
      const snapshot = normalizeDeviceValidationSnapshot(value, analysisId)
      if (snapshot) setAutonomousSnapshot(snapshot)
    }
    const snapshotListener = (event: Event) => adopt((event as CustomEvent<unknown>).detail)
    const storageListener = (event: StorageEvent) => {
      if (event.key !== deviceValidationSnapshotStorageKey || !event.newValue) return
      try {
        adopt(JSON.parse(event.newValue))
      } catch {
        // Ignore a partially written value from another tab.
      }
    }
    window.addEventListener(deviceValidationSnapshotEvent, snapshotListener)
    window.addEventListener('storage', storageListener)
    return () => {
      window.removeEventListener(deviceValidationSnapshotEvent, snapshotListener)
      window.removeEventListener('storage', storageListener)
    }
  }, [analysisRecordId])

  useEffect(() => {
    setPlan(null)
    setRun(null)
    setConfirmed(false)
    setNotice(null)
  }, [codeUnit?.artifact_id, selectedResultId, selectedUnit])

  useEffect(() => {
    setDispatchContext(null)
    setDispatchAssessment(null)
    setDispatchState('idle')
    setDispatchThought('')
    setDispatchOutput('')
    setDispatchFormatRecovery('')
    setDispatchTokenUsage(null)
    setDispatchTokenBudget(null)
    setDispatchStreamCounts({ reasoningChars: 0, contentChars: 0 })
    setDispatchAttempts([])
    setAdapterContext(null)
    setGeneratedAdapter(null)
    setAdapterRun(null)
    setAdapterState('idle')
    setAdapterThought('')
    setAdapterOutput('')
    setAdapterFormatRecovery('')
    setAdapterTokenUsage(null)
    setAdapterTokenBudget(null)
    setAdapterStreamCounts({ reasoningChars: 0, contentChars: 0 })
    setAdapterAttempts([])
  }, [codeUnit?.artifact_id, probe?.artifact_id, selectedResultId, selectedUnit])

  useEffect(() => {
    if (!selectedResultId && strategyResults[0]) setSelectedResultId(strategyResults[0].artifact.artifact_id)
  }, [selectedResultId, strategyResults])

  const discover = useCallback(async (): Promise<SafeActionResult> => {
    setBusy('discover')
    setNotice(null)
    try {
      const response = await api<DeviceDiscoveryResponse>('/v1/device-verification/devices')
      setDiscovery(response)
      const first = response.devices[0]?.serial ?? ''
      setSelectedSerial((current) => response.devices.some((item) => item.serial === current) ? current : first)
      setProbe(null)
      setRemoteShellRuns([])
      setExperimentSources([])
      setExperimentRuns([])
      if (response.devices.length === 0) {
        setNotice({ tone: 'info', text: text(response.hdc.diagnostic, '未发现已连接的 HDC 设备。') })
      } else {
        setNotice({ tone: 'success', text: `发现 ${response.devices.length} 台已连接设备；尚未读取设备能力。` })
      }
      return {
        status: response.devices.length > 0 ? 'succeeded' : 'warning',
        device_count: response.devices.length,
        devices: response.devices.map((item) => ({ serial: item.serial, connection_state: item.connection_state ?? '' })),
        selected_device_serial: first,
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : '无法发现真机。'
      setNotice({ tone: 'error', text: message })
      return { status: 'failed', error: message }
    } finally {
      setBusy(null)
    }
  }, [])

  const probeDeviceCapabilities = useCallback(async (requestedSerial = selectedSerial): Promise<SafeActionResult> => {
    if (!requestedSerial) {
      setNotice({ tone: 'error', text: '请先发现并选择一台真机。' })
      return { status: 'failed', error: '请先发现并选择一台真机。' }
    }
    setBusy('probe')
    setNotice(null)
    try {
      const response = await api<{ artifact: Artifact }>('/v1/projects/local-default/device-probes', {
        method: 'POST',
        body: JSON.stringify({ device_serial: requestedSerial, source_connector_id: sourceConnectorId }),
      })
      setSelectedSerial(requestedSerial)
      setProbe(response.artifact)
      setPlan(null)
      setRun(null)
      setRemoteShellRuns([])
      setExperimentSources([])
      setExperimentRuns([])
      setConfirmed(false)
      setNotice({ tone: 'success', text: '已完成固定 HDC 只读探测；观测结果不会自动等价为设备支持。' })
      return {
        status: 'succeeded',
        probe_artifact_id: response.artifact.artifact_id,
        device_serial: requestedSerial,
        observed_units: record(response.artifact.observed_units),
        claim_boundary_zh: text(response.artifact.claim_boundary_zh),
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : '设备探测失败。'
      setNotice({ tone: 'error', text: message })
      return { status: 'failed', error: message, device_serial: requestedSerial }
    } finally {
      setBusy(null)
    }
  }, [selectedSerial, sourceConnectorId])

  const assessDispatch = useCallback(async (
    requestedUnit: DeviceUnit = selectedUnit,
    agentNote = '',
  ): Promise<SafeActionResult> => {
    if (!probe || !codeUnit || !selectedResult) {
      const message = '需要已完成的设备探测、热点代码和一条策略推理结果，才能请求模型判断下发可行性。'
      setNotice({ tone: 'error', text: message })
      return { status: 'failed', error: message }
    }
    setBusy('assess')
    setNotice(null)
    setDispatchState('preparing')
    setDispatchContext(null)
    setDispatchThought('')
    setDispatchOutput('')
    setDispatchFormatRecovery('')
    setDispatchTokenUsage(null)
    setDispatchTokenBudget(null)
    setDispatchStreamCounts({ reasoningChars: 0, contentChars: 0 })
    const attemptIndex = dispatchAttempts.length + 1
    const modelRequestId = randomUuid()
    const priorAssessment = dispatchAssessment ? record(dispatchAssessment.result) : undefined
    const parentAssessmentArtifactId = dispatchAssessment?.artifact_id
    const priorValidationErrors = values(record(priorAssessment?.validation).errors)
    setDispatchAttempts((current) => [...current, {
      index: attemptIndex,
      status: 'running',
      summary: agentNote ? '页面助手请求结合新增观察继续判断。' : '正在形成新的语义下发判断。',
    }])
    try {
      const prepared = await api<DispatchContextResponse>('/v1/projects/local-default/device-dispatch-contexts/prepare', {
        method: 'POST',
        body: JSON.stringify({
          device_probe_artifact_id: probe.artifact_id,
          hotspot_code_artifact_id: codeUnit.artifact_id,
          reasoning_result_artifact_id: selectedResult.artifact.artifact_id,
          requested_unit: requestedUnit,
        }),
      })
      setDispatchContext(prepared.artifact)
      const modelInput = record(prepared.artifact.model_input_context)
      const allowedEvidenceIds = values(modelInput.allowed_evidence_ids)
      const contractHint = [
        '必须包含 decision、target_unit、execution_mode、summary、reasoning、candidate_scope、observed_code_evidence、dispatch_contract、required_device_checks、risks、alternative_units 和 evidence_ids。',
        'decision 只能为 semantic_candidate、needs_contract 或 not_suitable；semantic_candidate 的 target_unit 必须等于 requested_unit。',
        'semantic_candidate 的 dispatch_contract 必须至少包含 semantic_operations、input_assumptions、required_adapter_work、cpu_fallback 和 result_comparison。',
        `evidence_ids 只能使用本轮允许的值：${allowedEvidenceIds.join(', ') || '（本轮未声明，使用空数组）'}。`,
      ].join(' ')
      const promptPlan = buildDeviceDispatchPromptPlan(
        prepared.artifact,
        modelConfig.contextWindowTokens,
        modelConfig.agentMaxOutputTokens,
        {
          attemptIndex,
          parentAssessmentArtifactId,
          priorAssessment,
          validationErrors: priorValidationErrors,
          agentNote,
        },
      )
      setDispatchTokenBudget(promptPlan.tokenBudget)
      if (promptPlan.tokenBudget.exceedsWindow) {
        throw new Error(`下发判断上下文在压缩后仍超过已配置的 ${formatTokenCount(promptPlan.tokenBudget.contextWindowTokens)} 窗口。`)
      }
      setDispatchState('reasoning')
      const completion = await requestModelChatCompletionStream(modelConfig, promptPlan.messages, {
        maxTokens: promptPlan.tokenBudget.outputReserveTokens,
        observability: {
          analysisId: analysisRecordId ?? '',
          operation: 'device_dispatch_assessment.model',
          requestId: modelRequestId,
          runId: modelRequestId,
          turn: attemptIndex,
        },
        onProgress: (progress: ModelStreamProgress) => {
          if (progress.reasoningDelta) setDispatchThought((current) => `${current}${progress.reasoningDelta}`.slice(-16000))
          if (progress.contentDelta) setDispatchOutput((current) => `${current}${progress.contentDelta}`.slice(-16000))
          setDispatchStreamCounts({ reasoningChars: progress.reasoningChars, contentChars: progress.contentChars })
        },
      })
      const rawOutput = completion.choices?.[0]?.message?.content ?? ''
      setDispatchState('structuring')
      const structured = await parseOrRepairModelJson({
        config: modelConfig,
        rawOutput,
        reasoningOutput: completion.reasoningContent,
        schemaName: 'device_dispatch_assessment/v1',
        contractHint,
        maxTokens: promptPlan.tokenBudget.outputReserveTokens,
        requireChineseSummary: true,
        observability: {
          analysisId: analysisRecordId ?? '',
          operation: 'device_dispatch_assessment.model',
          runId: modelRequestId,
          turn: attemptIndex,
        },
      })
      setDispatchOutput(structured.effectiveOutput)
      setDispatchFormatRecovery(structured.recovered
        ? structured.recoverySource === 'reasoning_channel'
          ? '上游响应只在思考流中表达了结论；平台已基于同次响应完成结构恢复，并将继续校验合同。'
          : '上游响应未直接形成可用 JSON；平台已完成一次不新增事实的结构修复，并将继续校验合同。'
        : '')
      const combinedUsage = mergeModelTokenUsage(completion.tokenUsage, structured.repairUsage)
      setDispatchTokenUsage(combinedUsage)
      window.dispatchEvent(new CustomEvent('reasoning-agent-extra-token-usage', { detail: { token_usage: combinedUsage, token_budget: promptPlan.tokenBudget } }))
      setDispatchState('saving')
      const saved = await api<DispatchAssessmentResponse>('/v1/projects/local-default/device-dispatch-assessments', {
        method: 'POST',
        body: JSON.stringify({
          context_artifact_id: prepared.artifact.artifact_id,
          model_provider: 'kimi_server_agent_engine',
          model_name: modelConfig.model || 'engine-default',
          raw_output: structured.auditOutput,
          attempt_index: attemptIndex,
          ...(parentAssessmentArtifactId ? { parent_assessment_artifact_id: parentAssessmentArtifactId } : {}),
          ...(agentNote ? { agent_note: agentNote.slice(0, 1200) } : {}),
          result: structured.parsed,
        }),
      })
      const assessment = record(saved.artifact.result)
      const validation = record(assessment.validation)
      const validationErrors = values(validation.errors)
      const validationStatus = text(validation.status)
      const decisionRaw = text(assessment.decision)
      const summary = text(assessment.summary, '模型未返回摘要。')
      setDispatchAttempts((current) => current.map((item) => item.index === attemptIndex ? {
        ...item,
        status: validationStatus === 'valid' ? 'succeeded' : 'warning',
        summary,
        artifactId: saved.artifact.artifact_id,
        decision: decisionRaw,
        validationErrors,
      } : item))
      setDispatchAssessment(saved.artifact)
      setDispatchState(validationStatus === 'valid' ? 'succeeded' : 'warning')
      const decision = dispatchDecisionLabel(decisionRaw)
      setNotice({
        tone: validationStatus === 'valid' ? 'success' : 'info',
        text: validationStatus === 'valid'
          ? `模型下发判断已完成：${decision}。页面助手可根据这次真实结果自行决定是否继续收集证据。`
          : `模型下发判断已保存，但仍有 ${validationErrors.length} 项合同校验问题；应补充事实或由页面助手选择下一步，不会自动重复猜测。`,
      })
      return {
        status: validationStatus === 'valid' ? 'succeeded' : 'warning',
        assessment_artifact_id: saved.artifact.artifact_id,
        decision: text(assessment.decision),
        validation_status: validationStatus,
        validation_errors: validationErrors,
        attempt_count: attemptIndex,
        summary: text(assessment.summary),
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : '模型下发判断未完成。'
      setDispatchState('failed')
      setDispatchAttempts((current) => current.map((item) => item.index === attemptIndex ? { ...item, status: 'failed', summary: message } : item))
      setNotice({ tone: 'error', text: message })
      return { status: 'failed', error: message }
    } finally {
      setBusy(null)
    }
  }, [analysisRecordId, codeUnit, dispatchAssessment, dispatchAttempts.length, modelConfig, probe, selectedResult, selectedUnit])

  const synthesizeAdapter = useCallback(async (
    requestedUnit: DeviceUnit = selectedUnit,
    agentNote = '',
  ): Promise<SafeActionResult> => {
    if (!probe || !codeUnit || !dispatchAssessment) {
      const message = '需要设备探测、热点代码和已保存的模型下发合同，才能生成受控 Adapter。'
      setNotice({ tone: 'error', text: message })
      return { status: 'failed', error: message }
    }
    const currentDecision = text(record(dispatchAssessment.result).decision)
    const currentValidation = text(record(record(dispatchAssessment.result).validation).status)
    if (currentDecision !== 'semantic_candidate' || currentValidation !== 'valid') {
      const message = '当前下发判断尚未形成通过校验的语义候选，不能生成 Adapter。'
      setNotice({ tone: 'info', text: message })
      return { status: 'warning', error: message, decision: currentDecision, validation_status: currentValidation }
    }
    setBusy('adapter')
    setNotice(null)
    setAdapterState('preparing')
    setAdapterContext(null)
    setAdapterThought('')
    setAdapterOutput('')
    setAdapterFormatRecovery('')
    setAdapterTokenUsage(null)
    setAdapterTokenBudget(null)
    setAdapterStreamCounts({ reasoningChars: 0, contentChars: 0 })
    const attemptIndex = adapterAttempts.length + 1
    const modelRequestId = randomUuid()
    const parentAdapterArtifactId = generatedAdapter?.artifact_id
    setAdapterAttempts((current) => [...current, {
      index: attemptIndex,
      status: 'running',
      summary: agentNote ? '页面助手请求结合真实运行结果生成下一版 Adapter。' : '正在综合受控 Adapter 规格。',
    }])
    try {
      const prepared = await api<AdapterContextResponse>('/v1/projects/local-default/device-adapter-contexts/prepare', {
        method: 'POST',
        body: JSON.stringify({
          device_probe_artifact_id: probe.artifact_id,
          hotspot_code_artifact_id: codeUnit.artifact_id,
          dispatch_assessment_artifact_id: dispatchAssessment.artifact_id,
          requested_unit: requestedUnit,
        }),
      })
      setAdapterContext(prepared.artifact)
      const modelInput = record(prepared.artifact.model_input_context)
      const allowedEvidenceIds = values(modelInput.allowed_evidence_ids)
      const contractHint = [
        '必须包含 decision、target_unit、adapter_kind、summary、rationale、source_scope、implementation、verification_plan、risks 和 evidence_ids。',
        'decision 只能为 ready_for_materialization、needs_contract 或 not_supported；ready_for_materialization 的 target_unit 必须等于 requested_unit。',
        'GPU 仅可填写 gpu_gles3_rgba_expression_2px 的受限表达式与两像素向量；NPU 仅可填写 npu_nnrt_matmul_f32 的二维 FLOAT32 MatMul 参数。',
        `evidence_ids 只能使用本轮允许的值：${allowedEvidenceIds.join(', ') || '（本轮未声明，使用空数组）'}。`,
      ].join(' ')
      const promptPlan = buildDeviceAdapterPromptPlan(
        prepared.artifact,
        modelConfig.contextWindowTokens,
        modelConfig.agentMaxOutputTokens,
        {
          attemptIndex,
          parentAdapterArtifactId,
          priorRun: adapterRun ? record(adapterRun) : undefined,
          agentNote,
        },
      )
      setAdapterTokenBudget(promptPlan.tokenBudget)
      if (promptPlan.tokenBudget.exceedsWindow) {
        throw new Error(`Adapter 综合上下文在压缩后仍超过已配置的 ${formatTokenCount(promptPlan.tokenBudget.contextWindowTokens)} 窗口。`)
      }
      setAdapterState('reasoning')
      const completion = await requestModelChatCompletionStream(modelConfig, promptPlan.messages, {
        maxTokens: promptPlan.tokenBudget.outputReserveTokens,
        observability: {
          analysisId: analysisRecordId ?? '',
          operation: 'device_adapter_generation.model',
          requestId: modelRequestId,
          runId: modelRequestId,
          turn: attemptIndex,
        },
        onProgress: (progress: ModelStreamProgress) => {
          if (progress.reasoningDelta) setAdapterThought((current) => `${current}${progress.reasoningDelta}`.slice(-16000))
          if (progress.contentDelta) setAdapterOutput((current) => `${current}${progress.contentDelta}`.slice(-16000))
          setAdapterStreamCounts({ reasoningChars: progress.reasoningChars, contentChars: progress.contentChars })
        },
      })
      const rawOutput = completion.choices?.[0]?.message?.content ?? ''
      setAdapterState('structuring')
      const structured = await parseOrRepairModelJson({
        config: modelConfig,
        rawOutput,
        reasoningOutput: completion.reasoningContent,
        schemaName: 'device_adapter_spec/v1',
        contractHint,
        maxTokens: promptPlan.tokenBudget.outputReserveTokens,
        requireChineseSummary: true,
        observability: {
          analysisId: analysisRecordId ?? '',
          operation: 'device_adapter_generation.model',
          runId: modelRequestId,
          turn: attemptIndex,
        },
      })
      setAdapterOutput(structured.effectiveOutput)
      setAdapterFormatRecovery(structured.recovered
        ? structured.recoverySource === 'reasoning_channel'
          ? '上游响应只在思考流中表达了规格；平台已基于同次响应完成结构恢复，并将继续校验模板。'
          : '上游响应未直接形成可用 JSON；平台已完成一次不新增事实的结构修复，并将继续校验模板。'
        : '')
      const combinedUsage = mergeModelTokenUsage(completion.tokenUsage, structured.repairUsage)
      setAdapterTokenUsage(combinedUsage)
      window.dispatchEvent(new CustomEvent('reasoning-agent-extra-token-usage', { detail: { token_usage: combinedUsage, token_budget: promptPlan.tokenBudget } }))
      setAdapterState('saving')
      const saved = await api<AdapterResponse>('/v1/projects/local-default/device-adapters', {
        method: 'POST',
        body: JSON.stringify({
          context_artifact_id: prepared.artifact.artifact_id,
          model_provider: 'kimi_server_agent_engine',
          model_name: modelConfig.model || 'engine-default',
          raw_output: structured.auditOutput,
          attempt_index: attemptIndex,
          ...(parentAdapterArtifactId ? { parent_adapter_artifact_id: parentAdapterArtifactId } : {}),
          ...(agentNote ? { agent_note: agentNote.slice(0, 1200) } : {}),
          result: structured.parsed,
        }),
      })
      const result = record(saved.artifact.result)
      const validation = record(result.validation)
      const validationErrors = values(validation.errors)
      const materialization = record(saved.artifact.materialization)
      const materialized = text(materialization.status) === 'ready'
      const summary = text(result.summary, text(materialization.reason_zh, '模型未返回 Adapter 摘要。'))
      setAdapterAttempts((current) => current.map((item) => item.index === attemptIndex ? {
        ...item,
        status: text(validation.status) === 'valid' && materialized ? 'succeeded' : 'warning',
        summary,
        artifactId: saved.artifact.artifact_id,
        decision: text(result.decision),
        validationErrors,
      } : item))
      setGeneratedAdapter(saved.artifact)
      setAdapterRun(null)
      setAdapterState(text(validation.status) === 'valid' && materialized ? 'succeeded' : 'warning')
      setNotice({
        tone: text(validation.status) === 'valid' && materialized ? 'success' : 'info',
        text: text(validation.status) === 'valid' && materialized
          ? `受控 Adapter 已生成：${text(result.adapter_kind)}。页面助手可直接运行真机功能验证。`
          : `${summary} 未生成可运行代码。`,
      })
      return {
        status: text(validation.status) === 'valid' && materialized ? 'succeeded' : 'warning',
        adapter_artifact_id: saved.artifact.artifact_id,
        adapter_kind: text(result.adapter_kind),
        decision: text(result.decision),
        validation_status: text(validation.status),
        validation_errors: validationErrors,
        materialization_status: text(materialization.status),
        source_path: text(materialization.source_relative_path),
        summary,
        next_recommended_actions: text(validation.status) === 'valid' && materialized ? ['device.run_generated_adapter'] : [],
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : '受控 Adapter 综合未完成。'
      setAdapterState('failed')
      setAdapterAttempts((current) => current.map((item) => item.index === attemptIndex ? { ...item, status: 'failed', summary: message } : item))
      setNotice({ tone: 'error', text: message })
      return { status: 'failed', error: message }
    } finally {
      setBusy(null)
    }
  }, [adapterAttempts.length, adapterRun, analysisRecordId, codeUnit, dispatchAssessment, generatedAdapter?.artifact_id, modelConfig, probe, selectedUnit])

  const runGeneratedAdapter = useCallback(async (requestedAdapterArtifactId = ''): Promise<SafeActionResult> => {
    if (!generatedAdapter || (requestedAdapterArtifactId && requestedAdapterArtifactId !== generatedAdapter.artifact_id)) {
      const message = '页面助手请求的 Adapter 不是当前已生成的受控 Adapter。'
      setNotice({ tone: 'error', text: message })
      return { status: 'failed', error: message }
    }
    if (modelConfig.agentExecutionPolicy !== 'full_access') {
      const message = '当前执行策略要求确认模式，不能自动运行生成的 Adapter。'
      setNotice({ tone: 'error', text: message })
      return { status: 'failed', error: message }
    }
    if (text(record(generatedAdapter.materialization).status) !== 'ready') {
      const message = '当前 Adapter 没有已物化的受控源码，不能运行。'
      setNotice({ tone: 'info', text: message })
      return { status: 'warning', error: message }
    }
    setBusy('adapter_run')
    setNotice(null)
    try {
      const response = await api<{ artifact: Artifact }>('/v1/projects/local-default/device-adapter-runs', {
        method: 'POST',
        body: JSON.stringify({
          adapter_artifact_id: generatedAdapter.artifact_id,
          execution_authorization: 'project_default_authorized',
        }),
      })
      setAdapterRun(response.artifact)
      const status = text(response.artifact.status)
      const summary = text(response.artifact.summary_zh, '生成的 Adapter 已完成真机验证。')
      onValidationRecorded({
        validationId: generatedAdapter.artifact_id,
        status: status === 'passed' ? 'passed' : status === 'unsupported' ? 'unsupported' : 'failed',
        createdAt: text(generatedAdapter.created_at, new Date().toISOString()),
        updatedAt: text(response.artifact.created_at, new Date().toISOString()),
        deviceSerial: text(record(response.artifact.device).serial, selectedSerial),
        requestedUnit: selectedUnit,
        probeArtifactId: text(response.artifact.probe_artifact_id, probe?.artifact_id ?? ''),
        planArtifactId: generatedAdapter.artifact_id,
        adapterArtifactId: generatedAdapter.artifact_id,
        reasoningResultArtifactId: selectedResult?.artifact.artifact_id ?? '',
        runArtifactId: response.artifact.artifact_id,
        summary,
      })
      setNotice({ tone: status === 'passed' ? 'success' : status === 'unsupported' ? 'info' : 'error', text: summary })
      return {
        status: status === 'passed' ? 'succeeded' : status === 'unsupported' ? 'warning' : 'failed',
        adapter_artifact_id: generatedAdapter.artifact_id,
        generated_adapter_run_artifact_id: response.artifact.artifact_id,
        validation_status: status,
        summary,
        observed_evidence: record(response.artifact.observed_evidence),
        compile: record(response.artifact.compile),
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : '生成的 Adapter 真机验证未完成。'
      setNotice({ tone: 'error', text: message })
      return { status: 'failed', error: message, adapter_artifact_id: generatedAdapter.artifact_id }
    } finally {
      setBusy(null)
    }
  }, [generatedAdapter, modelConfig.agentExecutionPolicy, onValidationRecorded, probe?.artifact_id, selectedResult?.artifact.artifact_id, selectedSerial, selectedUnit])

  const stageExperimentSource = useCallback(async ({
    profileId,
    language,
    title,
    source,
    shaderSource = '',
  }: {
    profileId: string
    language: string
    title: string
    source: string
    shaderSource?: string
  }): Promise<SafeActionResult> => {
    if (!probe) {
      const message = '需要先完成当前设备的只读探测，才能暂存与这台真机关联的实验源码。'
      setNotice({ tone: 'error', text: message })
      return { status: 'failed', error: message }
    }
    if (modelConfig.agentExecutionPolicy !== 'full_access') {
      const message = '当前执行策略为确认模式，页面助手不能暂存真机实验源码。'
      setNotice({ tone: 'error', text: message })
      return { status: 'failed', error: message }
    }
    if (!['native_c', 'native_cpp', 'gpu_gles3', 'gpu_vulkan', 'npu_nnrt', 'media_avcodec'].includes(profileId) || !['c', 'cpp'].includes(language) || !source.trim() || (profileId === 'gpu_vulkan' && !shaderSource.trim())) {
      const message = '实验 profile、语言或完整源码无效。'
      setNotice({ tone: 'error', text: message })
      return { status: 'failed', error: message }
    }
    setBusy('experiment_stage')
    setNotice(null)
    try {
      const response = await api<{ artifact: Artifact }>('/v1/projects/local-default/device-experiment-sources', {
        method: 'POST',
        body: JSON.stringify({
          device_probe_artifact_id: probe.artifact_id,
          profile_id: profileId,
          language,
          title,
          source,
          shader_source: shaderSource,
          execution_authorization: 'agent_full_access',
        }),
      })
      const artifact = response.artifact
      setExperimentSources((current) => [artifact, ...current.filter((item) => item.artifact_id !== artifact.artifact_id)].slice(0, 12))
      const summary = text(artifact.summary_zh, '完整实验源码已暂存。')
      setNotice({ tone: 'success', text: summary })
      return {
        status: 'succeeded',
        staged_source_artifact_id: artifact.artifact_id,
        profile_id: text(artifact.profile_id),
        requested_unit: text(artifact.requested_unit),
        source_sha256: text(artifact.source_sha256),
        source_relative_path: text(artifact.source_relative_path),
        source_bytes: artifact.source_bytes,
        summary,
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : '真机实验源码暂存失败。'
      setNotice({ tone: 'error', text: message })
      return {
        status: 'failed',
        error: message,
        ...(error instanceof ApiError ? { error_code: error.code, error_details: error.details } : {}),
      }
    } finally {
      setBusy(null)
    }
  }, [modelConfig.agentExecutionPolicy, probe])

  const buildUploadExperiment = useCallback(async (
    stagedSourceArtifactId: string,
    timeoutSeconds = 240,
  ): Promise<SafeActionResult> => {
    if (!probe) {
      const message = '需要先完成当前设备的只读探测，才能构建并上传实验。'
      setNotice({ tone: 'error', text: message })
      return { status: 'failed', error: message }
    }
    if (modelConfig.agentExecutionPolicy !== 'full_access') {
      const message = '当前执行策略为确认模式，页面助手不能构建或上传真机实验。'
      setNotice({ tone: 'error', text: message })
      return { status: 'failed', error: message }
    }
    if (!/^art_[a-f0-9]{24}$/.test(stagedSourceArtifactId)) {
      const message = '页面助手没有提供已暂存实验源码的 artifact_id。'
      setNotice({ tone: 'error', text: message })
      return { status: 'failed', error: message }
    }
    setBusy('experiment_build')
    setNotice(null)
    try {
      const response = await api<{ artifact: Artifact }>('/v1/projects/local-default/device-experiment-runs', {
        method: 'POST',
        body: JSON.stringify({
          device_probe_artifact_id: probe.artifact_id,
          staged_source_artifact_id: stagedSourceArtifactId,
          timeout_seconds: Number.isInteger(timeoutSeconds) ? timeoutSeconds : 240,
          execution_authorization: 'agent_full_access',
        }),
      })
      const artifact = response.artifact
      const deployment = record(artifact.deployment)
      const status = text(artifact.status)
      const summary = text(artifact.summary_zh, '真机实验构建与上传已结束。')
      setExperimentRuns((current) => [artifact, ...current.filter((item) => item.artifact_id !== artifact.artifact_id)].slice(0, 12))
      setNotice({ tone: status === 'uploaded' ? 'success' : 'info', text: summary })
      return {
        status: status === 'uploaded' ? 'succeeded' : 'warning',
        experiment_status: status,
        experiment_run_artifact_id: artifact.artifact_id,
        staged_source_artifact_id: text(artifact.staged_source_artifact_id, stagedSourceArtifactId),
        profile_id: text(artifact.profile_id),
        requested_unit: text(artifact.requested_unit),
        device_serial: text(record(artifact.device).serial, selectedSerial),
        remote_binary_path: text(deployment.remote_binary_path),
        build: record(artifact.build),
        deployment,
        next_action_zh: text(artifact.next_action_zh),
        summary,
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : '真机实验构建或上传失败。'
      setNotice({ tone: 'error', text: message })
      return { status: 'failed', error: message, staged_source_artifact_id: stagedSourceArtifactId }
    } finally {
      setBusy(null)
    }
  }, [modelConfig.agentExecutionPolicy, probe, selectedSerial])

  const startAutonomousExperiment = useCallback(() => {
    if (deployment?.device_validation_enabled === false) {
      setNotice({ tone: 'info', text: deployment.device_validation_message })
      return
    }
    if (autonomousDispatchRequestRef.current || autonomousRunActive) return
    if (!codeUnit || !selectedResult) {
      setNotice({ tone: 'error', text: '需要先准备热点代码并完成至少一种策略推理，才能启动真机验证。' })
      return
    }
    if (modelConfig.agentExecutionPolicy !== 'full_access') {
      setNotice({ tone: 'error', text: '请在页面助手设置中切换为全授权，才能启动可自主迭代的真机验证。' })
      return
    }
    const requestId = randomUuid()
    autonomousDispatchRequestRef.current = requestId
    setBusy('experiment_agent')
    setNotice({ tone: 'info', text: '正在把真机验证任务提交给页面助手…' })
    autonomousDispatchTimeoutRef.current = window.setTimeout(() => {
      if (autonomousDispatchRequestRef.current !== requestId) return
      autonomousDispatchTimeoutRef.current = null
      setNotice({ tone: 'info', text: '仍在等待页面助手连接与提交回执；请求保持排队，收到明确结果后更新。' })
    }, 30_000)
    const task = buildDeviceValidationTask({
      codeUnit,
      strategyArtifact: selectedResult.artifact,
      strategyLabel: selectedResult.strategyLabel,
      selectedUnit,
      selectedUnitLabel: unitLabel(selectedUnit),
      probe,
      selectedSerial,
    })
    window.dispatchEvent(new CustomEvent('page-agent-autonomous-task', {
      detail: {
        analysis_id: analysisRecordId ?? '',
        request_id: requestId,
        message: task.modelMessage,
        display_message: task.displayMessage,
        requested_unit: task.selectedUnit,
        recommended_profile_id: task.recommendedProfileId,
        compatible_profile_ids: task.compatibleProfileIds,
        device_facts: probe ? {
          schemaVersion: 'device_validation_facts/v1',
          deviceSerial: text(record(probe.device).serial, selectedSerial),
          probeArtifactId: probe.artifact_id,
          profileIds: [],
          actionFacts: [],
        } : undefined,
      },
    }))
    void appendAnalysisTraceEvent(analysisRecordId, {
      category: 'workflow',
      operation: 'device_validation.task.dispatch',
      phase: 'device_validation',
      status: 'started',
      requestId,
      runId: requestId,
      payload: {
        selected_unit: selectedUnit,
        selected_strategy_artifact_id: selectedResult.artifact.artifact_id,
        code_artifact_id: codeUnit.artifact_id,
        probe_artifact_id: probe?.artifact_id ?? null,
        display_message: task.displayMessage,
        model_message: task.modelMessage,
        code_truncated: task.codeTruncated,
      },
    })
    if (task.codeTruncated) {
      setNotice({ tone: 'info', text: '热点源码超过真机任务输入预算，已明确标记压缩；页面助手不得猜测被省略的实现。' })
    }
  }, [analysisRecordId, autonomousRunActive, codeUnit, deployment, modelConfig.agentExecutionPolicy, probe, selectedResult, selectedSerial, selectedUnit])

  const executeRemoteShell = useCallback(async (
    command: string,
    timeoutSeconds = 240,
  ): Promise<SafeActionResult> => {
    if (!probe) {
      const message = '需要先完成当前设备的只读探测，才能把 Shell 命令绑定到这台真机。'
      setNotice({ tone: 'error', text: message })
      return { status: 'failed', error: message }
    }
    if (modelConfig.agentExecutionPolicy !== 'full_access') {
      const message = '当前执行策略为确认模式，页面助手不能直接执行远端 Shell 命令。'
      setNotice({ tone: 'error', text: message })
      return { status: 'failed', error: message }
    }
    if (!command.trim()) {
      const message = '页面助手没有提供可执行的远端 Shell 命令。'
      setNotice({ tone: 'error', text: message })
      return { status: 'failed', error: message }
    }
    setBusy('remote_shell')
    setNotice(null)
    try {
      const response = await api<{ artifact: Artifact }>('/v1/projects/local-default/device-shell-runs', {
        method: 'POST',
        body: JSON.stringify({
          device_probe_artifact_id: probe.artifact_id,
          command,
          timeout_seconds: Number.isInteger(timeoutSeconds) ? timeoutSeconds : 240,
          execution_authorization: 'agent_full_access',
        }),
      })
      const artifact = response.artifact
      const execution = record(artifact.execution)
      const status = text(artifact.status)
      const summary = text(artifact.summary_zh, '远端 Shell 命令已完成。')
      const stdout = text(execution.stdout)
      const stderr = text(execution.stderr)
      setRemoteShellRuns((current) => [artifact, ...current.filter((item) => item.artifact_id !== artifact.artifact_id)].slice(0, 12))
      setNotice({ tone: status === 'succeeded' ? 'success' : 'error', text: summary })
      return {
        status: status === 'succeeded' ? 'succeeded' : 'failed',
        remote_shell_run_artifact_id: artifact.artifact_id,
        device_serial: text(record(artifact.device).serial, selectedSerial),
        command_sha256: text(artifact.command_sha256),
        command,
        return_code: execution.return_code,
        timed_out: execution.timed_out === true,
        output_excerpt: `${stdout}${stderr ? `\n[stderr]\n${stderr}` : ''}`.slice(-12_000),
        summary,
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : '远端 Shell 命令未完成。'
      setNotice({ tone: 'error', text: message })
      return { status: 'failed', error: message, command }
    } finally {
      setBusy(null)
    }
  }, [modelConfig.agentExecutionPolicy, probe, selectedSerial])

  const createPlan = useCallback(async (requestedUnit: DeviceUnit = selectedUnit): Promise<SafeActionResult> => {
    if (!probe || !codeUnit || !selectedResult) {
      const message = '需要已完成的设备探测、热点代码和一条策略推理结果，才能生成实测方案。'
      setNotice({ tone: 'error', text: message })
      return { status: 'failed', error: message }
    }
    setBusy('plan')
    setNotice(null)
    try {
      const response = await api<{ artifact: Artifact }>('/v1/projects/local-default/device-validation-plans', {
        method: 'POST',
        body: JSON.stringify({
          device_probe_artifact_id: probe.artifact_id,
          hotspot_code_artifact_id: codeUnit.artifact_id,
          reasoning_result_artifact_id: selectedResult.artifact.artifact_id,
          requested_unit: requestedUnit,
          ...(dispatchAssessment ? { dispatch_assessment_artifact_id: dispatchAssessment.artifact_id } : {}),
        }),
      })
      setPlan(response.artifact)
      setRun(null)
      setConfirmed(false)
      const status = text(response.artifact.plan_status)
      onValidationRecorded({
        validationId: response.artifact.artifact_id,
        status: status === 'ready_for_user_confirmation' ? 'planned' : 'evidence_required',
        createdAt: text(response.artifact.created_at, new Date().toISOString()),
        updatedAt: text(response.artifact.created_at, new Date().toISOString()),
        deviceSerial: text(record(response.artifact.device).serial, selectedSerial),
        requestedUnit,
        probeArtifactId: probe.artifact_id,
        planArtifactId: response.artifact.artifact_id,
        reasoningResultArtifactId: selectedResult.artifact.artifact_id,
        summary: summaryFromPlan(response.artifact),
      })
      setNotice({ tone: status === 'ready_for_user_confirmation' ? 'success' : 'info', text: summaryFromPlan(response.artifact) })
      return {
        status: status === 'ready_for_user_confirmation' ? 'succeeded' : 'warning',
        plan_artifact_id: response.artifact.artifact_id,
        plan_status: status,
        planning_reason_zh: summaryFromPlan(response.artifact),
        required_next_steps: values(response.artifact.required_next_steps),
        confirmation_required: Boolean(record(response.artifact.confirmation).required),
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : '无法生成真机验证方案。'
      setNotice({ tone: 'error', text: message })
      return { status: 'failed', error: message }
    } finally {
      setBusy(null)
    }
  }, [codeUnit, dispatchAssessment, onValidationRecorded, probe, selectedResult, selectedSerial, selectedUnit])

  const executePlan = useCallback(async ({
    agentAuthorized: _agentAuthorized = false,
    requestedPlanArtifactId = '',
  }: {
    agentAuthorized?: boolean
    requestedPlanArtifactId?: string
  } = {}): Promise<SafeActionResult> => {
    if (!plan || (requestedPlanArtifactId && requestedPlanArtifactId !== plan.artifact_id)) {
      const message = '页面助手请求的实测方案不是当前已显示的已登记方案。'
      setNotice({ tone: 'error', text: message })
      return { status: 'failed', error: message }
    }
    const nonce = text(planConfirmation.nonce)
    // The local single-user workbench treats full access as default consent for
    // this bounded recipe. Confirmation mode retains the explicit checkbox.
    const fullAccessRun = modelConfig.agentExecutionPolicy === 'full_access'
    if (!fullAccessRun && (!confirmed || !nonce)) {
      setNotice({ tone: 'error', text: '请先阅读方案并明确勾选确认，再执行受控真机配方。' })
      return { status: 'failed', error: '当前执行策略要求页面确认，尚未完成确认。' }
    }
    setBusy('run')
    setNotice(null)
    try {
      const response = await api<{ artifact: Artifact }>('/v1/projects/local-default/device-validation-runs', {
        method: 'POST',
        body: JSON.stringify({
          plan_artifact_id: plan.artifact_id,
          user_confirmed: fullAccessRun ? false : true,
          confirmation_nonce: fullAccessRun ? '' : nonce,
          execution_authorization: fullAccessRun ? 'agent_full_access' : 'user_confirmation',
        }),
      })
      setRun(response.artifact)
      setConfirmed(false)
      const status = text(response.artifact.status)
      onValidationRecorded({
        validationId: plan.artifact_id,
        status: status === 'passed' ? 'passed' : status === 'unsupported' ? 'unsupported' : 'failed',
        createdAt: text(plan.created_at, new Date().toISOString()),
        updatedAt: text(response.artifact.created_at, new Date().toISOString()),
        deviceSerial: text(record(response.artifact.device).serial, selectedSerial),
        requestedUnit: selectedUnit,
        probeArtifactId: text(plan.probe_artifact_id, probe?.artifact_id ?? ''),
        planArtifactId: plan.artifact_id,
        reasoningResultArtifactId: text(record(plan.analysis_link).reasoning_result_artifact_id, selectedResult?.artifact.artifact_id ?? ''),
        runArtifactId: response.artifact.artifact_id,
        summary: text(response.artifact.summary_zh, '真机配方已完成。'),
      })
      const summary = text(response.artifact.summary_zh, '真机配方已完成。')
      setNotice({ tone: status === 'passed' ? 'success' : status === 'unsupported' ? 'info' : 'error', text: summary })
      return {
        status: status === 'passed' ? 'succeeded' : status === 'unsupported' ? 'warning' : 'failed',
        run_artifact_id: response.artifact.artifact_id,
        plan_artifact_id: plan.artifact_id,
        validation_status: status,
        summary,
        execution_authorization: text(response.artifact.execution_authorization),
        observed_evidence: record(response.artifact.observed_evidence),
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : '真机配方未完成。'
      setNotice({ tone: 'error', text: message })
      return { status: 'failed', error: message, plan_artifact_id: plan.artifact_id }
    } finally {
      setBusy(null)
    }
  }, [confirmed, modelConfig.agentExecutionPolicy, onValidationRecorded, plan, planConfirmation.nonce, probe?.artifact_id, selectedResult?.artifact.artifact_id, selectedSerial, selectedUnit])

  useEffect(() => {
    const listener = (event: Event) => {
      const detail = record((event as CustomEvent<unknown>).detail)
      const action = text(detail.action)
      const respond = (result: SafeActionResult) => respondReasoningLabAction(detail.request_id, result)
      if (deployment?.device_validation_enabled === false) {
        respond({ status: 'failed', error: deployment.device_validation_message, code: 'DEPLOYMENT_DEVICE_VALIDATION_DISABLED' })
        return
      }
      const settleUnit = async (unit: DeviceUnit) => {
        if (unit === selectedUnit) return
        setSelectedUnit(unit)
        await new Promise<void>((resolve) => window.setTimeout(resolve, 0))
      }
      if (action === 'discover') {
        void discover().then(respond)
        return
      }
      if (action === 'probe') {
        const requestedSerial = text(detail.device_serial)
        if (!requestedSerial) {
          const message = '页面助手需要指定已发现的设备序列号，才能发起只读探测。'
          setNotice({ tone: 'error', text: message })
          respond({ status: 'failed', error: message })
          return
        }
        void probeDeviceCapabilities(requestedSerial).then(respond)
        return
      }
      if (action === 'prepare' || action === 'assess') {
        const requestedUnit = text(detail.requested_unit)
        if (!['npu', 'gpu', 'dsp', 'media'].includes(requestedUnit)) {
          const message = action === 'assess' ? '页面助手指定的下发判断单元无效。' : '页面助手指定的真机验证单元无效。'
          setNotice({ tone: 'error', text: message })
          respond({ status: 'failed', error: message })
          return
        }
        void (async () => {
          const unit = requestedUnit as DeviceUnit
          await settleUnit(unit)
          const result = action === 'assess'
            ? await assessDispatch(unit, text(detail.follow_up_note))
            : await createPlan(unit)
          respond(result)
        })()
        return
      }
      if (action === 'synthesize_adapter') {
        const requestedUnit = text(detail.requested_unit)
        if (!['npu', 'gpu', 'dsp', 'media'].includes(requestedUnit)) {
          const message = '页面助手指定的 Adapter 目标单元无效。'
          setNotice({ tone: 'error', text: message })
          respond({ status: 'failed', error: message })
          return
        }
        void (async () => {
          const unit = requestedUnit as DeviceUnit
          await settleUnit(unit)
          respond(await synthesizeAdapter(unit, text(detail.follow_up_note)))
        })()
        return
      }
      if (action === 'run_adapter') {
        void runGeneratedAdapter(text(detail.adapter_artifact_id)).then(respond)
        return
      }
      if (action === 'remote_shell') {
        const timeoutSeconds = typeof detail.timeout_seconds === 'number' && Number.isInteger(detail.timeout_seconds)
          ? detail.timeout_seconds
          : 240
        void executeRemoteShell(text(detail.command), timeoutSeconds).then(respond)
        return
      }
      if (action === 'stage_experiment_source') {
        const source = typeof detail.source === 'string' ? detail.source : ''
        void stageExperimentSource({
          profileId: text(detail.profile_id),
          language: text(detail.language),
          title: text(detail.title),
          source,
          shaderSource: text(detail.shader_source),
        }).then(respond)
        return
      }
      if (action === 'build_upload_experiment') {
        const timeoutSeconds = typeof detail.timeout_seconds === 'number' && Number.isInteger(detail.timeout_seconds)
          ? detail.timeout_seconds
          : 240
        void buildUploadExperiment(text(detail.staged_source_artifact_id), timeoutSeconds).then(respond)
        return
      }
      if (action === 'run') {
        void executePlan({
          agentAuthorized: true,
          requestedPlanArtifactId: text(detail.plan_artifact_id),
        }).then(respond)
      }
    }
    window.addEventListener('device-verification-action', listener)
    return () => window.removeEventListener('device-verification-action', listener)
  }, [assessDispatch, buildUploadExperiment, createPlan, deployment, discover, executePlan, executeRemoteShell, probeDeviceCapabilities, runGeneratedAdapter, selectedSerial, selectedUnit, stageExperimentSource, synthesizeAdapter])

  const errorForwarded = useWorkbenchErrorNotification(notice, () => setNotice(null), 'device-verification')

  if (deployment?.device_validation_enabled === false) {
    return <div className="device-verification">
      <div className="device-verification-intro"><strong>当前部署：分析模式</strong><button className="quiet-button" type="button" onClick={startAutonomousExperiment}><HardDrive size={14} />真机验证</button></div>
      <div className="device-verification-notice info" role="status"><TriangleAlert size={14} /><span>{notice?.text || deployment.device_validation_message}</span></div>
    </div>
  }

  if (!codeUnit || strategyResults.length === 0) {
    return <div className="event-empty">先完成一次策略推理。真机验证会绑定一台设备、当前热点代码和该策略结果，避免把无关联的实测写入分析记录。</div>
  }

  return <div className="device-verification">
    <div className="device-verification-intro">
      <div><HardDrive size={17} /><div><strong>设备证据与真机验证</strong><span>一次启动后，页面助手会根据真实编译、上传和手机输出自主迭代。平台固定工作站侧工具链、HDC、目标绑定、超时与审计；手机侧命令由全授权页面助手决定。</span></div></div>
      <small>{modelConfig.agentExecutionPolicy === 'full_access' ? '全授权：可自主真机实验' : '确认模式：仅可分析与探测'}</small>
    </div>
    {customSuggestedUnit && !customRouteProfileOverridden && <div className="device-verification-notice info"><TriangleAlert size={14} /><span>本轮机会推理使用了自建 API 目录 <code>{customSuggestedUnit}</code>。它已进入路由与 API 精核，但尚未绑定真机构建 profile；平台不会悄悄把它替换为 GPU 或 NPU。请在“可选实验偏好”中选择兼容方向，并明确确认后再启动。</span><button className="quiet-button compact" type="button" onClick={() => setCustomRouteProfileOverridden(true)}>采用当前 {unitLabel(selectedUnit)} 验证方向</button></div>}
    {notice && !errorForwarded && <div className={`device-verification-notice ${notice.tone}`}>{notice.tone === 'error' ? <ShieldAlert size={14} /> : notice.tone === 'success' ? <CheckCircle2 size={14} /> : <TriangleAlert size={14} />}<span>{notice.text}</span></div>}

    <section className="device-agent-experiment">
      <header>
        <div><BrainCircuit size={16} /><span><strong>自主真机验证</strong><small>不再生成固定 Adapter。页面助手可自行读代码、写最小实验、固定 profile 编译上传，并按手机真实输出继续修订。</small></span></div>
        <span className="device-dispatch-state">轮次上限由助手设置决定</span>
      </header>
      <div className="device-agent-experiment-start table-shell">
        <button className="primary-button" type="button" disabled={busy === 'experiment_agent' || autonomousRunActive || modelConfig.agentExecutionPolicy !== 'full_access' || Boolean(customSuggestedUnit && !customRouteProfileOverridden)} onClick={startAutonomousExperiment} title="启动 Agent Engine 的多轮真机实验；助手会自行发现和探测手机，编写、构建、上传并在手机侧迭代验证。">{busy === 'experiment_agent' || autonomousRunActive ? <LoaderCircle size={14} className="spin" /> : <Play size={14} />}{autonomousRunActive ? '真机验证进行中' : busy === 'experiment_agent' ? '正在提交验证' : '开始真机验证'}</button>
        <span>{autonomousRunActive ? '正在推进构建、部署或设备实验。' : modelConfig.agentExecutionPolicy === 'full_access' ? '页面助手会自行发现设备、探测能力并按真实结果迭代。' : '请先在页面助手中启用全授权。'}</span>
      </div>
      <details className="device-experiment-preferences">
        <summary>可选实验偏好</summary>
        <div className="device-verification-controls table-shell">
          <label><span>优先验证方向</span><select value={selectedUnit} onChange={(event) => { setSelectedUnit(event.target.value as DeviceUnit); setCustomRouteProfileOverridden(true) }}>{(['npu', 'gpu', 'dsp', 'media', 'parallel_cpu'] as DeviceUnit[]).map((unit) => <option value={unit} key={unit}>{unitLabel(unit)}</option>)}</select></label>
          <label><span>关联策略结果</span><select value={selectedResult?.artifact.artifact_id ?? ''} onChange={(event) => setSelectedResultId(event.target.value)}>{strategyResults.map((item) => <option value={item.artifact.artifact_id} key={item.artifact.artifact_id}>{item.strategyLabel} · {item.artifact.artifact_id.slice(0, 12)}</option>)}</select></label>
        </div>
      </details>
      {autonomousSnapshot && autonomousState && <section className={`device-agent-result is-${autonomousState.tone}`} aria-live="polite">
        <header>
          <div>{autonomousState.tone === 'running' ? <LoaderCircle size={16} className="spin" /> : autonomousState.tone === 'success' ? <CheckCircle2 size={16} /> : autonomousState.tone === 'failed' ? <ShieldAlert size={16} /> : <TriangleAlert size={16} />}<span><strong>{autonomousState.label}</strong><small>{unitLabel(autonomousSnapshot.requestedUnit || selectedUnit)} · Run <code>{autonomousSnapshot.runId.slice(0, 12)}</code> · {new Date(autonomousSnapshot.updatedAt).toLocaleString('zh-CN', { hour12: false })}</small></span></div>
          <a className="quiet-link" href={`/v1/agent-runs/${encodeURIComponent(autonomousSnapshot.runId)}/execution-trace`} download>执行记录</a>
        </header>
        <ol className="device-agent-pipeline" aria-label="真机验证阶段">
          {autonomousStages.map((stage) => <li className={`is-${stage.status}`} key={stage.id}>
            {stage.status === 'running' ? <LoaderCircle size={13} className="spin" /> : stage.status === 'succeeded' ? <CheckCircle2 size={13} /> : stage.status === 'warning' ? <TriangleAlert size={13} /> : <Circle size={13} />}
            <span><strong>{stage.label}</strong><small>{stage.branch}</small></span>
          </li>)}
        </ol>
        {autonomousSnapshot.activeActionDetail && <p className="device-agent-active-action"><code>{autonomousSnapshot.activeActionId}</code><span>{autonomousSnapshot.activeActionDetail}</span></p>}
        <dl className="device-agent-result-facts">
          <div><dt>设备</dt><dd>{autonomousSnapshot.facts.deviceSerial || '待探测'}</dd></div>
          <div><dt>Profile</dt><dd>{autonomousSnapshot.facts.profileIds.join('、') || autonomousSnapshot.recommendedProfileId || '待确认'}</dd></div>
          <div><dt>构建工件</dt><dd><code>{autonomousSnapshot.facts.buildResultArtifactId?.slice(0, 16) || '—'}</code></dd></div>
          <div><dt>部署工件</dt><dd><code>{autonomousSnapshot.facts.deploymentArtifactId?.slice(0, 16) || '—'}</code>{autonomousSnapshot.facts.deploymentVerified === true ? ' · 已校验' : autonomousSnapshot.facts.deploymentVerified === false ? ` · 未校验${autonomousSnapshot.facts.deploymentFailureStage ? `（${autonomousSnapshot.facts.deploymentFailureStage}）` : ''}` : ''}</dd></div>
          <div><dt>设备返回码</dt><dd>{autonomousSnapshot.facts.experimentExecution?.returnCode ?? '—'}</dd></div>
          <div><dt>证据边界</dt><dd>{autonomousTerminal?.detail || '尚未取得已部署实验的设备执行返回。'}</dd></div>
        </dl>
        {autonomousSnapshot.facts.experimentExecution?.outputExcerpt && <details className="device-agent-result-output" open={autonomousSnapshot.status === 'completed'}><summary>设备程序输出</summary><pre>{autonomousSnapshot.facts.experimentExecution.outputExcerpt}</pre></details>}
        {autonomousSnapshot.facts.actionFacts.length > 0 && <details className="device-agent-result-actions"><summary>工具结果（{autonomousSnapshot.facts.actionFacts.length}）</summary><ol>{autonomousSnapshot.facts.actionFacts.map((fact, index) => <li className={`is-${fact.status}`} key={`${fact.actionId}-${index}`}><code>{fact.actionId}</code><span>{fact.summary}</span></li>)}</ol></details>}
        {autonomousSnapshot.finalResponse && <details className="device-agent-result-conclusion" open={autonomousSnapshot.status === 'completed'}><summary>页面助手结论</summary><pre>{autonomousSnapshot.finalResponse}</pre></details>}
      </section>}
      {discovery && <div className="device-discovery-meta"><span>HDC：{discovery.hdc.available === true ? text(discovery.hdc.version, '可用') : '不可用'}</span><span>{discovery.devices.length > 0 ? `${discovery.devices.length} 台设备已连接` : text(discovery.hdc.diagnostic, '未发现设备')}</span></div>}
      {probe && <div className="device-probe-facts table-shell">
        <div><span>设备</span><strong>{text(probeDevice.model, '型号未返回')}</strong><small>{text(probeDevice.serial)}</small></div>
        <div><span>芯片</span><strong>{text(probeDevice.chipset, '未返回')}</strong><small>{text(probeDevice.hardware, '硬件标识未返回')}</small></div>
        <div><span>系统</span><strong>{text(probeDevice.os_full_name, '未返回')}</strong><small>API {text(probeDevice.api_version, '—')}</small></div>
        <div><span>探测工件</span><strong><code>{probe.artifact_id.slice(0, 12)}</code></strong><small>只读 HDC 白名单</small></div>
      </div>}
      {(experimentSources.length > 0 || experimentRuns.length > 0) && <section className="device-experiment-records">
        <header><Code2 size={16} /><span><strong>页面助手实验记录</strong><small>每个源码、构建和上传结果都会成为后续自主推理的真实输入。</small></span></header>
        <ol>
          {experimentRuns.map((runArtifact) => {
            const deployment = record(runArtifact.deployment)
            const runStatus = text(runArtifact.status)
            return <li className={`is-${runStatus}`} key={runArtifact.artifact_id}>
              {runStatus === 'uploaded' ? <CheckCircle2 size={15} /> : <TriangleAlert size={15} />}
              <div><strong>{runStatus === 'uploaded' ? '已构建并上传' : '构建或上传需要处理'}</strong><span>{text(runArtifact.summary_zh)}</span><small>{unitLabel(text(runArtifact.requested_unit))} · <code>{text(runArtifact.profile_id)}</code> · <code>{runArtifact.artifact_id.slice(0, 12)}</code>{text(deployment.remote_binary_path) ? ` · ${text(deployment.remote_binary_path)}` : ''}</small></div>
              <details><summary>编译与部署输出</summary><pre>{JSON.stringify({ build: record(runArtifact.build), deployment }, null, 2)}</pre></details>
            </li>
          })}
          {experimentSources.filter((source) => !experimentRuns.some((runArtifact) => text(runArtifact.staged_source_artifact_id) === source.artifact_id)).map((source) => <li className="is-staged" key={source.artifact_id}><Code2 size={15} /><div><strong>源码已暂存</strong><span>{text(source.summary_zh)}</span><small><code>{text(source.profile_id)}</code> · {text(source.source_sha256).slice(0, 16)} · <code>{source.artifact_id.slice(0, 12)}</code></small></div></li>)}
        </ol>
      </section>}
      {remoteShellRuns.length > 0 && <section className="device-remote-shells">
        <header><Terminal size={16} /><span><strong>页面助手远端 Shell 记录</strong><small>命令由页面助手自主选择并在已绑定手机上执行；本机不启动宿主 Shell。</small></span></header>
        <ol>{remoteShellRuns.map((shellRun) => {
          const execution = record(shellRun.execution)
          const shellStatus = text(shellRun.status)
          return <li className={`is-${shellStatus}`} key={shellRun.artifact_id}>{shellStatus === 'succeeded' ? <CheckCircle2 size={15} /> : shellStatus === 'timed_out' ? <TriangleAlert size={15} /> : <ShieldAlert size={15} />}<div><strong>{remoteShellStatusLabel(shellStatus)}</strong><code>{text(shellRun.command, '命令内容未返回')}</code><small>设备 {text(record(shellRun.device).serial, '—')} · 返回码 {typeof execution.return_code === 'number' ? execution.return_code : '—'} · <code>{shellRun.artifact_id.slice(0, 12)}</code></small></div><details><summary>输出</summary><pre>{text(execution.stdout, '无标准输出。')}{text(execution.stderr) ? `\n\n[stderr]\n${text(execution.stderr)}` : ''}</pre></details></li>
        })}</ol>
      </section>}
    </section>

    <div hidden>
    <div className="device-verification-controls table-shell">
      <button className="quiet-button" type="button" disabled={busy === 'discover'} onClick={() => void discover()}>{busy === 'discover' ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}发现真机</button>
      <label><span>HDC 设备</span><select value={selectedSerial} disabled={!discovery || discovery.devices.length === 0} onChange={(event) => { setSelectedSerial(event.target.value); setProbe(null) }}><option value="">选择设备</option>{discovery?.devices.map((item) => <option key={item.serial} value={item.serial}>{item.serial}</option>)}</select></label>
      <button className="quiet-button" type="button" disabled={!selectedSerial || busy === 'probe'} onClick={() => void probeDeviceCapabilities()}>{busy === 'probe' ? <LoaderCircle size={14} className="spin" /> : <ShieldCheck size={14} />}读取设备能力</button>
    </div>

    {discovery && <div className="device-discovery-meta"><span>HDC：{discovery.hdc.available === true ? text(discovery.hdc.version, '可用') : '不可用'}</span><span>{discovery.devices.length > 0 ? `${discovery.devices.length} 台设备已连接` : text(discovery.hdc.diagnostic, '未发现设备')}</span></div>}

    {probe && <>
      <div className="device-probe-facts table-shell">
        <div><span>设备</span><strong>{text(probeDevice.model, '型号未返回')}</strong><small>{text(probeDevice.serial)}</small></div>
        <div><span>芯片</span><strong>{text(probeDevice.chipset, '未返回')}</strong><small>{text(probeDevice.hardware, '硬件标识未返回')}</small></div>
        <div><span>系统</span><strong>{text(probeDevice.os_full_name, '未返回')}</strong><small>API {text(probeDevice.api_version, '—')}</small></div>
        <div><span>探测工件</span><strong><code>{probe.artifact_id.slice(0, 12)}</code></strong><small>只读 HDC 白名单</small></div>
      </div>
      <div className="device-unit-observations">
        {(['npu', 'gpu', 'dsp', 'media'] as DeviceUnit[]).map((unit) => {
          const observation = record(observedUnits[unit])
          const active = unit === selectedUnit
          return <button className={`device-unit-observation ${active ? 'active' : ''}`} type="button" key={unit} onClick={() => setSelectedUnit(unit)}>
            <Cpu size={14} /><span><strong>{unitLabel(unit)}</strong><small>{observationLabel(text(observation.observation_status))} · {values(observation.observed_capabilities).join('、') || '无运行时正向观察'}</small></span>
          </button>
        })}
      </div>
      <section className={`device-dispatch-assessment ${dispatchState === 'succeeded' ? 'succeeded' : dispatchState === 'warning' ? 'warning' : dispatchState === 'failed' ? 'failed' : ''}`}>
        <header>
          <div><BrainCircuit size={16} /><span><strong>模型下发判断</strong><small>模型依据当前热点、策略证据、单元 API 上下文与设备观察形成语义合同。是否继续判断由页面助手根据真实结果决定，不使用参考源码匹配决定资格。</small></span></div>
          <span className={`device-dispatch-state ${dispatchState === 'succeeded' ? 'ready' : ''}`}>{dispatchStateLabel(dispatchState)}</span>
        </header>
        <div className="device-dispatch-actions">
          <button className="quiet-button" type="button" disabled={busy === 'assess' || dispatchState === 'preparing' || dispatchState === 'reasoning' || dispatchState === 'structuring' || dispatchState === 'saving'} onClick={() => void assessDispatch()} title="形成一次可审计的模型下发判断；页面助手可在后续依据新事实决定是否再次判断。">
            {busy === 'assess' ? <LoaderCircle size={14} className="spin" /> : <WandSparkles size={14} />}
            {dispatchState === 'idle' || dispatchState === 'succeeded' || dispatchState === 'warning' || dispatchState === 'failed' ? '模型判断下发可行性' : dispatchStateLabel(dispatchState)}
          </button>
          <span>Kimi Agent Engine 已接管</span>
          {dispatchTokenBudget && <span>上下文 {formatTokenCount(dispatchTokenBudget.inputTokens)} / {formatTokenCount(dispatchTokenBudget.contextWindowTokens)}</span>}
          {dispatchTokenUsage && <span>Input {formatTokenCount(dispatchTokenUsage.inputTokens)} · Output {formatTokenCount(dispatchTokenUsage.outputTokens)}</span>}
        </div>
        {dispatchContext && <p className="device-dispatch-audit">下发判断上下文 <code>{dispatchContext.artifact_id.slice(0, 12)}</code> · 当前选择 {unitLabel(selectedUnit)} · {dispatchTokenBudget?.compactionApplied ? '源码已按 Token 预算压缩' : '使用完整热点代码'}</p>}
        {dispatchAttempts.length > 0 && <ol className="device-dispatch-attempts" aria-label="模型下发判断尝试记录">
          {dispatchAttempts.map((attempt) => <li className={`is-${attempt.status}`} key={attempt.index}>
            {attempt.status === 'running' ? <LoaderCircle size={14} className="spin" /> : attempt.status === 'failed' ? <ShieldAlert size={14} /> : attempt.status === 'warning' ? <TriangleAlert size={14} /> : <CheckCircle2 size={14} />}
            <div><strong>第 {attempt.index} 次判断</strong><span>{attempt.summary}</span>{attempt.validationErrors && attempt.validationErrors.length > 0 && <small>{attempt.validationErrors.join('；')}</small>}</div>
            {attempt.artifactId && <code>{attempt.artifactId.slice(0, 12)}</code>}
          </li>)}
        </ol>}
        {(dispatchState === 'reasoning' || dispatchState === 'structuring' || dispatchState === 'saving' || dispatchThought || dispatchOutput) && <div className="device-dispatch-live">
          <p>{dispatchState === 'reasoning' ? '模型正在实时生成下发判断。' : dispatchState === 'structuring' ? '模型响应已收到，正在整理严格下发合同。' : dispatchState === 'saving' ? '正在校验合同字段、证据引用并保存审计工件。' : '本次模型下发判断的流式记录。'}</p>
          {dispatchFormatRecovery && <div className="device-verification-notice"><TriangleAlert size={14} /><span>{dispatchFormatRecovery}</span></div>}
          {dispatchThought && <details open><summary>思考流{dispatchStreamCounts.reasoningChars ? `（已接收 ${dispatchStreamCounts.reasoningChars} 字）` : ''}</summary><pre>{dispatchThought}</pre></details>}
          {dispatchOutput && <details><summary>结构化输出{dispatchStreamCounts.contentChars ? `（已接收 ${dispatchStreamCounts.contentChars} 字）` : ''}</summary><pre>{dispatchOutput}</pre></details>}
        </div>}
        {dispatchAssessment && <div className="device-dispatch-result">
          <div><CheckCircle2 size={15} /><span><strong>{dispatchDecisionLabel(text(dispatchResult.decision))}</strong><small>{text(dispatchResult.execution_mode, '未声明候选路径')} · {text(dispatchValidation.status) === 'valid' ? '合同结构与证据引用已校验' : '合同存在需保守处理的问题'}</small></span></div>
          <p>{text(dispatchResult.summary, '模型没有提供可展示的下发判断摘要。')}</p>
          <dl>
            <div><dt>判断范围</dt><dd>{text(record(dispatchResult.candidate_scope).description, '未声明')}</dd></div>
            <div><dt>源码依据</dt><dd>{renderLines(dispatchResult.observed_code_evidence)}</dd></div>
            <div><dt>候选操作</dt><dd>{renderLines(dispatchContract.semantic_operations)}</dd></div>
            <div><dt>需补齐工作</dt><dd>{renderLines(dispatchContract.required_adapter_work)}</dd></div>
            <div><dt>CPU 回退</dt><dd>{renderLines(dispatchContract.cpu_fallback)}</dd></div>
            <div><dt>设备检查</dt><dd>{renderLines(dispatchResult.required_device_checks)}</dd></div>
          </dl>
          {values(dispatchValidation.errors).length > 0 && <div className="device-verification-notice error"><ShieldAlert size={14} /><span>{renderLines(dispatchValidation.errors)}</span></div>}
          <details><summary>查看完整下发合同</summary><pre>{JSON.stringify({ assessment_artifact_id: dispatchAssessment.artifact_id, result: dispatchResult }, null, 2)}</pre></details>
        </div>}
      </section>
      <section className={`device-dispatch-assessment device-adapter-assessment ${adapterState === 'succeeded' ? 'succeeded' : adapterState === 'warning' ? 'warning' : adapterState === 'failed' ? 'failed' : ''}`}>
        <header>
          <div><Code2 size={16} /><span><strong>生成 Adapter 与真机迭代</strong><small>模型把通过校验的语义合同映射为可审计的 GPU/NPU Adapter 参数；平台生成实际 C 源码，以固定工具链编译、传输、执行并清理。页面助手可读取真实输出后决定下一轮，不需逐次确认。</small></span></div>
          <span className={`device-dispatch-state ${adapterState === 'succeeded' ? 'ready' : ''}`}>{dispatchStateLabel(adapterState)}</span>
        </header>
        <div className="device-dispatch-actions">
          <button className="quiet-button" type="button" disabled={busy === 'adapter' || !dispatchAssessment || adapterState === 'preparing' || adapterState === 'reasoning' || adapterState === 'structuring' || adapterState === 'saving'} onClick={() => void synthesizeAdapter()} title="由模型形成受控 Adapter 规格，平台物化为本地 C 源码。">
            {busy === 'adapter' ? <LoaderCircle size={14} className="spin" /> : <Code2 size={14} />}
            {adapterState === 'preparing' || adapterState === 'reasoning' || adapterState === 'structuring' || adapterState === 'saving' ? dispatchStateLabel(adapterState) : '生成受控 Adapter'}
          </button>
          <button className="primary-button compact" type="button" disabled={busy === 'adapter_run' || text(adapterMaterialization.status) !== 'ready' || modelConfig.agentExecutionPolicy !== 'full_access'} onClick={() => void runGeneratedAdapter()} title="编译、传输并运行当前不可变 Adapter；运行日志将作为下一轮页面助手的新证据。">
            {busy === 'adapter_run' ? <LoaderCircle size={14} className="spin" /> : <Play size={14} />}运行生成 Adapter
          </button>
          <span>Kimi Agent Engine 已接管</span>
          {adapterTokenBudget && <span>上下文 {formatTokenCount(adapterTokenBudget.inputTokens)} / {formatTokenCount(adapterTokenBudget.contextWindowTokens)}</span>}
          {adapterTokenUsage && <span>Input {formatTokenCount(adapterTokenUsage.inputTokens)} · Output {formatTokenCount(adapterTokenUsage.outputTokens)}</span>}
        </div>
        {adapterContext && <p className="device-dispatch-audit">Adapter 上下文 <code>{adapterContext.artifact_id.slice(0, 12)}</code> · 当前选择 {unitLabel(selectedUnit)} · {adapterTokenBudget?.compactionApplied ? '源码已按 Token 预算压缩' : '使用完整热点代码'}</p>}
        {adapterAttempts.length > 0 && <ol className="device-dispatch-attempts" aria-label="受控 Adapter 综合尝试记录">
          {adapterAttempts.map((attempt) => <li className={`is-${attempt.status}`} key={attempt.index}>
            {attempt.status === 'running' ? <LoaderCircle size={14} className="spin" /> : attempt.status === 'failed' ? <ShieldAlert size={14} /> : attempt.status === 'warning' ? <TriangleAlert size={14} /> : <CheckCircle2 size={14} />}
            <div><strong>第 {attempt.index} 次 Adapter 综合</strong><span>{attempt.summary}</span>{attempt.validationErrors && attempt.validationErrors.length > 0 && <small>{attempt.validationErrors.join('；')}</small>}</div>
            {attempt.artifactId && <code>{attempt.artifactId.slice(0, 12)}</code>}
          </li>)}
        </ol>}
        {(adapterState === 'reasoning' || adapterState === 'structuring' || adapterState === 'saving' || adapterThought || adapterOutput) && <div className="device-dispatch-live">
          <p>{adapterState === 'reasoning' ? '模型正在实时生成 Adapter 方案。' : adapterState === 'structuring' ? '模型响应已收到，正在整理受控 Adapter 规格。' : adapterState === 'saving' ? '正在校验模板参数、证据引用并物化不可变源码。' : '本次 Adapter 综合的流式记录。'}</p>
          {adapterFormatRecovery && <div className="device-verification-notice"><TriangleAlert size={14} /><span>{adapterFormatRecovery}</span></div>}
          {adapterThought && <details open><summary>思考流{adapterStreamCounts.reasoningChars ? `（已接收 ${adapterStreamCounts.reasoningChars} 字）` : ''}</summary><pre>{adapterThought}</pre></details>}
          {adapterOutput && <details><summary>结构化输出{adapterStreamCounts.contentChars ? `（已接收 ${adapterStreamCounts.contentChars} 字）` : ''}</summary><pre>{adapterOutput}</pre></details>}
        </div>}
        {generatedAdapter && <div className="device-dispatch-result">
          <div><CheckCircle2 size={15} /><span><strong>{text(adapterValidation.status) === 'valid' && text(adapterMaterialization.status) === 'ready' ? 'Adapter 已物化' : 'Adapter 尚未物化'}</strong><small>{text(adapterResult.adapter_kind, '无可运行模板')} · {text(adapterValidation.status) === 'valid' ? '规格与证据引用已校验' : '规格存在需保守处理的问题'}</small></span></div>
          <p>{text(adapterResult.summary, text(adapterMaterialization.reason_zh, '模型没有提供可展示的 Adapter 摘要。'))}</p>
          <dl>
            <div><dt>代码范围</dt><dd>{text(record(adapterResult.source_scope).description, '未声明')}</dd></div>
            <div><dt>物化源码</dt><dd>{text(adapterMaterialization.source_relative_path, '未生成')}</dd></div>
            <div><dt>源码哈希</dt><dd>{text(adapterMaterialization.source_sha256, '—').slice(0, 16)}</dd></div>
            <div><dt>验证计划</dt><dd>{renderLines(adapterResult.verification_plan)}</dd></div>
          </dl>
          {values(adapterValidation.errors).length > 0 && <div className="device-verification-notice error"><ShieldAlert size={14} /><span>{renderLines(adapterValidation.errors)}</span></div>}
          <details><summary>查看完整 Adapter 工件</summary><pre>{JSON.stringify({ adapter_artifact_id: generatedAdapter.artifact_id, result: adapterResult, materialization: adapterMaterialization }, null, 2)}</pre></details>
        </div>}
        {adapterRun && <div className={`device-validation-run ${text(adapterRun.status)}`}>
          <div>{text(adapterRun.status) === 'passed' ? <CheckCircle2 size={16} /> : <TriangleAlert size={16} />}<span><strong>{runStatusLabel(text(adapterRun.status))}</strong><small>{text(adapterRun.summary_zh, '生成 Adapter 已完成真机验证。')}</small></span></div>
          <dl className="device-run-facts"><div><dt>设备</dt><dd>{text(record(adapterRun.device).serial)}</dd></div><div><dt>时长</dt><dd>{typeof adapterRunExecution.duration_ms === 'number' ? `${adapterRunExecution.duration_ms.toFixed(1)} ms` : '—'}</dd></div><div><dt>Adapter</dt><dd>{text(record(adapterRun.observed_evidence).adapter_kind, '—')}</dd></div><div><dt>清理</dt><dd>{record(adapterRun.cleanup).succeeded === true ? '已完成' : '需检查'}</dd></div></dl>
          <details className="device-run-output"><summary>查看生成 Adapter 的编译与运行输出</summary><pre>{JSON.stringify(record(adapterRun.compile), null, 2)}{text(adapterRunExecution.stdout) ? `\n\n[stdout]\n${text(adapterRunExecution.stdout)}` : ''}{text(adapterRunExecution.stderr) ? `\n\n[stderr]\n${text(adapterRunExecution.stderr)}` : ''}</pre></details>
        </div>}
      </section>
      <div className="device-plan-controls table-shell">
        <label><span>验证单元</span><select value={selectedUnit} onChange={(event) => setSelectedUnit(event.target.value as DeviceUnit)}>{(['npu', 'gpu', 'dsp', 'media'] as DeviceUnit[]).map((unit) => <option value={unit} key={unit}>{unitLabel(unit)}</option>)}</select></label>
        <label><span>关联策略结果</span><select value={selectedResult?.artifact.artifact_id ?? ''} onChange={(event) => setSelectedResultId(event.target.value)}>{strategyResults.map((item) => <option value={item.artifact.artifact_id} key={item.artifact.artifact_id}>{item.strategyLabel} · {item.artifact.artifact_id.slice(0, 12)}</option>)}</select></label>
        <button className="primary-button" type="button" disabled={busy === 'plan'} onClick={() => void createPlan()}>{busy === 'plan' ? <LoaderCircle size={14} className="spin" /> : <FlaskConical size={14} />}生成实测方案</button>
      </div>
      <p className="device-observation-boundary">{text(selectedObservation.claim_boundary_zh, '当前未返回该单元的设备观察。')}</p>
      {remoteShellRuns.length > 0 && <section className="device-remote-shells">
        <header><Terminal size={16} /><span><strong>页面助手远端 Shell 记录</strong><small>命令由页面助手自主选择并在已绑定手机上执行。本机不启动宿主 Shell；每次命令、输出、超时与返回码均保留为审计工件。</small></span></header>
        <ol>
          {remoteShellRuns.map((shellRun) => {
            const execution = record(shellRun.execution)
            const shellStatus = text(shellRun.status)
            return <li className={`is-${shellStatus}`} key={shellRun.artifact_id}>
              {shellStatus === 'succeeded' ? <CheckCircle2 size={15} /> : shellStatus === 'timed_out' ? <TriangleAlert size={15} /> : <ShieldAlert size={15} />}
              <div><strong>{remoteShellStatusLabel(shellStatus)}</strong><code>{text(shellRun.command, '命令内容未返回')}</code><small>设备 {text(record(shellRun.device).serial, '—')} · 返回码 {typeof execution.return_code === 'number' ? execution.return_code : '—'} · {typeof execution.duration_ms === 'number' ? `${execution.duration_ms.toFixed(1)} ms` : '—'} · <code>{shellRun.artifact_id.slice(0, 12)}</code></small></div>
              <details><summary>输出</summary><pre>{text(execution.stdout, '无标准输出。')}{text(execution.stderr) ? `\n\n[stderr]\n${text(execution.stderr)}` : ''}</pre></details>
            </li>
          })}
        </ol>
      </section>}
    </>}

    {plan && <div className={`device-validation-plan ${planStatus === 'ready_for_user_confirmation' ? 'ready' : 'blocked'}`}>
      <div className="device-validation-plan-head"><div><FlaskConical size={16} /><span><strong>{planStatusLabel(planStatus)}</strong><small>{summaryFromPlan(plan)}{planStatus === 'ready_for_user_confirmation' && modelConfig.agentExecutionPolicy === 'full_access' ? ' 当前为全授权模式，可直接运行这条受控配方。' : ''}</small></span></div><code>{plan.artifact_id.slice(0, 12)}</code></div>
      {Object.keys(planRecipe).length > 0 ? <>
        <dl className="device-plan-details">
          <div><dt>配方</dt><dd><code>{text(planRecipe.recipe_id)}</code></dd></div>
          <div><dt>验证范围</dt><dd>{recipeScopeLabel(recipeScope)}</dd></div>
          {text(planRecipe.reference_match_level) && <div><dt>源码匹配</dt><dd>{referenceMatchLabel(text(planRecipe.reference_match_level))}</dd></div>}
          {Object.keys(planDispatch).length > 0 && <div><dt>模型判断</dt><dd>{dispatchDecisionLabel(text(planDispatch.decision))} · {text(planDispatch.execution_mode, '未声明路径')}</dd></div>}
          <div><dt>范围</dt><dd>{text(planRecipe.execution_scope_zh)}</dd></div>
          <div><dt>不证明</dt><dd>{values(planRecipe.does_not_prove_zh).join('；')}</dd></div>
        </dl>
        {modelConfig.agentExecutionPolicy !== 'full_access' && <label className="device-confirmation"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} /><span>我确认在 <code>{text(record(plan.device).serial)}</code> 上运行这条{recipeScope === 'platform_baseline_preflight' ? '平台基线' : '已登记参考实现'}配方。它会编译、传输并执行固定测试二进制，完成后清理固定远端临时路径；{recipeScope === 'platform_baseline_preflight' ? '不会编译、改写或下发当前热点代码。' : '验证范围仅限当前已登记参考实现。'}</span></label>}
        <button className="primary-button device-run-button" type="button" disabled={(modelConfig.agentExecutionPolicy !== 'full_access' && !confirmed) || busy === 'run'} onClick={() => void executePlan()}>{busy === 'run' ? <LoaderCircle size={14} className="spin" /> : <Play size={14} />}{modelConfig.agentExecutionPolicy === 'full_access' ? '直接执行受控实测' : '执行受控实测'}</button>
      </> : <div className="device-plan-blocked"><TriangleAlert size={15} /><div><strong>{Object.keys(planDispatch).length > 0 ? '模型判断尚未形成可执行受控路径' : '需要先完成模型下发判断'}</strong><span>{values(plan.required_next_steps).join('；') || '需要补齐下发合同、adapter、软件回退和结果比较器；这不是参考源码或模型请求错误。'}</span></div></div>}
      <p className="device-plan-boundary">{text(plan.claim_boundary_zh)}</p>
    </div>}

    {run && <div className={`device-validation-run ${text(run.status)}`}>
      <div>{text(run.status) === 'passed' ? <CheckCircle2 size={16} /> : <TriangleAlert size={16} />}<span><strong>{runStatusLabel(text(run.status))}</strong><small>{text(run.summary_zh)}</small></span></div>
      <dl className="device-run-facts"><div><dt>设备</dt><dd>{text(record(run.device).serial)}</dd></div><div><dt>时长</dt><dd>{typeof runData.duration_ms === 'number' ? `${runData.duration_ms.toFixed(1)} ms` : '—'}</dd></div><div><dt>{runEvidenceLabel}</dt><dd>{runEvidenceValue}</dd></div><div><dt>清理</dt><dd>{record(run.cleanup).succeeded === true ? '已完成' : '需检查'}</dd></div></dl>
      <details className="device-run-output"><summary>查看受控运行输出</summary><pre>{text(runData.stdout, '无标准输出。')}{text(runData.stderr) ? `\n\n[stderr]\n${text(runData.stderr)}` : ''}</pre></details>
      <p>{text(record(run.evidence_calibration).recommendation_effect)}</p>
    </div>}
    </div>
  </div>
}
