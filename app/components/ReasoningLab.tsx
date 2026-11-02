'use client'

import { ArrowDown, BrainCircuit, Check, ClipboardPaste, Code2, Cpu, Database, FileCode2, FileUp, Hand, ListChecks, LoaderCircle, RefreshCw, RotateCcw, Search, ShieldAlert, WandSparkles, X } from 'lucide-react'
import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, upload } from '../lib/api'
import { loadHardwareApiRecords } from '../lib/analysis-catalog-cache'
import { agentCommandStatusEventName, dispatchAgentCommand } from '../lib/agent-command-bridge'
import { snapshotAnalysisContext, type AnalysisDeviceValidationRecord, type AnalysisHotspotSummary, type AnalysisInferenceRecord, type AnalysisInferenceStrategyId, type TraceAnalysisRecord, type TraceAnalysisStart } from '../lib/analysis-history'
import type { AnalysisWorkspaceSnapshot } from '../lib/analysis-workspace'
import { BrowserModelConfig, createDefaultBrowserModelConfig, modelConfigChangedEvent, readBrowserModelConfig } from '../lib/model-config'
import { DeviceVerificationPanel } from './DeviceVerificationPanel'
import { readDeviceValidationSnapshot } from '../lib/device-validation-snapshot'
import { HardwareSemanticRoutingExperiment } from './HardwareSemanticRoutingExperiment'
import { TraceStageTimeline } from './TraceStageTimeline'
import { hardwareSemanticRoutingSkill } from '../lib/hardware-semantic-routing'
import { rejectReasoningLabAction, respondReasoningLabAction } from '../lib/reasoning-action-bridge'
import { hasCurrentModelInputContext, reasoningModelInputContext } from '../lib/model-context'
import { fullOptimizationAnalysisSkill, strategySkillPrompts, strategyTitle, type OptimizationWorkflowEvent, type OptimizationWorkflowState, type StrategyId } from '../lib/optimization-workflow'
import { buildStrategyPromptPlan, ModelOutputIncompleteError } from '../lib/reasoning-prompt'
import { randomUuid } from '../lib/random-id'
import { applySkillPrompt, type SkillVersionProfile } from '../lib/skill-version-library'
import { mergeModelTokenUsage, parseOrRepairModelJson } from '../lib/structured-model-output'
import { chipsetOptions, createTargetPlatformProfile, operatingSystemOptions, targetPlatformProfileLabel, type TargetPlatformProfile } from '../lib/target-platform-profile'
import { createPromptTokenBudget, estimateMessageTokens, formatTokenCount, normalizeContextWindowTokens, type ModelTokenUsage, type PromptTokenBudget } from '../lib/token-budget'
import type { Artifact, DeploymentCapabilities, JsonRecord } from '../lib/types'
import { useWorkbenchErrorNotification } from './WorkbenchErrorNotification'

type Notice = { tone: 'error' | 'success' | 'info'; text: string } | null
type TraceSummary = Artifact & { hotspot_count?: number; cluster_count?: number; quality_reason?: string }

const strategySkillSlots: Record<StrategyId, string> = {
  kb_rag: 'strategy_kb_rag',
  hardware_kb: 'strategy_hardware_kb',
  direct_baseline: 'strategy_direct_baseline',
}

export type ReasoningAssistantContext = {
  analysis_record_id: string | null
  trace: {
    artifact_id: string
    label: string
    input_kind: string
  } | null
  hotspot_limit: number
  hotspot_module_count: number
  hotspots: Array<{
    rank: number
    module_rank: number | null
    event_id: string
    event_name: string
    module: string
    self_time_ms: number | null
    max_self_time_ms: number | null
    occurrence_count: number | null
  }>
  code_unit: {
    artifact_id: string
    event_name: string
    source_origin: string
    source_path: string
    source_binding: string
    code_sha256: string
    code_preview: string
  } | null
  hardware_routing: {
    status: 'pending' | 'completed' | 'skipped'
    result_artifact_id: string | null
    selection_origin: string | null
    decision: string | null
    selected_units: string[]
  } | null
  blind_baseline: {
    status: 'not_run' | 'completed'
    result_artifact_id: string | null
    comparison_artifact_id: string | null
    session_isolated: true
  }
  strategy_results: Array<{
    strategy_id: StrategyId
    status: 'context_ready' | 'completed'
    context_artifact_id: string | null
    result_artifact_id: string | null
    recommendation: string | null
    summary: string | null
  }>
  selected_api_record_id: string | null
}

export type ReasoningRecordRestoreRequest = {
  requestId: string
  analysis: TraceAnalysisRecord
  workspace?: AnalysisWorkspaceSnapshot | null
}

const REASONING_HOTSPOTS_PER_MODULE = 6
const DEFAULT_EXPANDED_HOTSPOT_MODULES = 3
const STRATEGY_RESPONSE_TIMEOUT_MS = 270_000
const agentStrategyOutputContract = {
  type: 'object',
  additionalProperties: false,
  required: ['schema_version', 'recommendation', 'confidence', 'evidence_status', 'summary', 'opportunities', 'missing_evidence', 'negative_evidence'],
  properties: {
    schema_version: { const: 'opportunity_reasoning/v1' },
    recommendation: { enum: ['candidate', 'not_recommended', 'evidence_required'] },
    confidence: { enum: ['low', 'medium', 'high'] },
    evidence_status: { enum: ['context_supported', 'model_hypothesis', 'insufficient_evidence'] },
    summary: { type: 'string', description: '中文总结' },
    opportunities: {
      type: 'array',
      maxItems: 3,
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['opportunity_kind', 'candidate_target', 'analysis_scope', 'rationale', 'required_conditions', 'risks', 'evidence_ids', 'verification_plan'],
        properties: {
          opportunity_kind: { enum: ['heterogeneous', 'parallel_cpu', 'simd', 'algorithmic'] },
          candidate_target: { type: 'string', description: '候选硬件、API、SIMD 或并行方向' },
          analysis_scope: {
            type: 'object',
            additionalProperties: false,
            required: ['level', 'status', 'description', 'source_evidence'],
            properties: {
              level: { enum: ['function', 'loop_region', 'data_pipeline', 'callee', 'unknown'] },
              status: { enum: ['observed', 'callee_unresolved', 'not_declared'] },
              description: { type: 'string', description: '实际分析的可见代码范围' },
              source_evidence: { type: 'string', description: '支持该范围的行号、scope_id 或可见源码事实' },
            },
          },
          rationale: { type: 'string', description: '代码事实到候选方向的因果理由' },
          required_conditions: { type: 'array', items: { type: 'string' } },
          risks: { type: 'array', items: { type: 'string' } },
          evidence_ids: { type: 'array', items: { type: 'string' }, description: '只能使用 model_input_context.allowed_evidence_ids' },
          verification_plan: { type: 'array', items: { type: 'string' } },
          implementation_sketch: { type: 'string' },
        },
      },
    },
    missing_evidence: { type: 'array', items: { type: 'string' } },
    negative_evidence: { type: 'array', items: { type: 'string' } },
  },
}
type ReasoningEventStatus = 'running' | 'succeeded' | 'warning' | 'failed'
type ReasoningEvent = {
  id: string
  runId: string
  strategyId: AnalysisInferenceStrategyId
  stage: 'context' | 'request' | 'response' | 'contract' | 'artifact'
  status: ReasoningEventStatus
  title: string
  detail: string
  timestamp: string
  payload?: string
  payloadLabel?: string
}

function inferenceStrategyLabel(strategyId: AnalysisInferenceStrategyId): string {
  if (strategyId === 'blind_baseline') return '独立盲推理'
  if (strategyId === 'blind_comparison') return '盲推理与流程对照'
  return strategyTitle(strategyId)
}

function inferenceStatusLabel(status: AnalysisInferenceRecord['status']): string {
  return ({ running: '推理中', succeeded: '已完成', failed: '失败', blocked: '未调用' })[status] ?? status
}

type TranscriptTimelineEntry = {
  stage: ReasoningEvent['stage']
  status: ReasoningEventStatus
  title: string
  detail: string
  timestamp: string
}

type ModelInferenceResponse = {
  rawOutput: string
  reasoningOutput?: string
  tokenUsage: ModelTokenUsage
  tokenBudget: PromptTokenBudget
}

type AgentResponseWaiter = {
  resolve: (response: ModelInferenceResponse) => void
  reject: (error: Error) => void
  timeoutId: number
  tokenBudget: PromptTokenBudget
}

type HardwareSemanticRoute = {
  codeArtifactId: string
  resultArtifact: Artifact
}

type AgentStrategyProgressStatus = 'running' | 'succeeded' | 'warning' | 'failed'
type AgentStrategyProgressPhase = 'model_pending' | 'response_received' | 'contract_validating' | 'completed' | 'failed'
type AgentStrategyProgressStep = {
  id: 'evidence' | 'scope' | 'model' | 'contract'
  title: string
  detail: string
  status: 'pending' | 'running' | 'succeeded' | 'warning' | 'failed'
}
type AgentStrategyProgressDirection = {
  title: string
  detail: string
  evidenceStatus: 'observed' | 'needs_validation' | 'missing'
}
type AgentStrategyProgress = {
  schema_version: 'strategy_progress/v1'
  request_id: string
  strategy_id: string
  strategy_label: string
  phase: AgentStrategyProgressPhase
  status: AgentStrategyProgressStatus
  started_at: string
  headline: string
  detail: string
  steps: AgentStrategyProgressStep[]
  directions: AgentStrategyProgressDirection[]
}

type ReasoningHotspotCatalog = {
  groups: JsonRecord[]
  stages: JsonRecord[]
  events: JsonRecord[]
  quality: JsonRecord
}

const strategies: Array<{ id: StrategyId; title: string; description: string; icon: typeof Database }> = [
  { id: 'kb_rag', title: '优化知识库上下文', description: '检索严格配对、CPU 回退实现与既有优化案例。', icon: Database },
  { id: 'hardware_kb', title: '硬件机会推理', description: '检索已选硬件目录的 API 候选，由模型判断热点核心与接口语义是否相容。', icon: Cpu },
  { id: 'direct_baseline', title: '直接代码推理', description: '保留目标设备/系统画像，但不提供知识库或能力记录。', icon: BrainCircuit },
]
const visibleStrategies = strategies.filter((strategy) => strategy.id === 'hardware_kb')

type ManualOpportunitySelectionResponse = { artifact: Artifact; context_artifact: Artifact; validation_errors: string[] }

function isStrategyId(value: string): value is StrategyId {
  return strategies.some((strategy) => strategy.id === value)
}

function record(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {}
}

function stringValue(value: unknown, fallback = '—'): string {
  return typeof value === 'string' && value.length > 0 ? value : fallback
}

function routedHardwareUnit(route: HardwareSemanticRoute | null, codeArtifactId: string): string {
  if (!route || route.codeArtifactId !== codeArtifactId) return ''
  const routeResult = record(route.resultArtifact.result)
  const candidates = Array.isArray(routeResult.candidates) ? routeResult.candidates.map(record) : []
  return candidates.map((candidate) => stringValue(candidate.unit_id, '')).find(Boolean) ?? ''
}

function numberValue(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function milliseconds(value: unknown): string {
  const parsed = numberValue(value)
  return parsed === null ? '—' : `${parsed.toFixed(parsed >= 10 ? 1 : 3)} ms`
}

function countValue(value: unknown): string {
  const parsed = numberValue(value)
  return parsed === null ? '—' : String(Math.trunc(parsed))
}

function eventMetrics(event: JsonRecord): JsonRecord {
  return record(event.metrics)
}

function eventModule(event: JsonRecord): string {
  const explicit = stringValue(event.module, '')
  if (explicit) return explicit
  const marker = record(event.hitrace_marker)
  const tags = Array.isArray(marker.module_tags) ? marker.module_tags.map(record) : []
  const modules = tags.map((tag) => stringValue(tag.display_name, '')).filter(Boolean)
  return modules.join(' · ') || '未分类'
}

function eventModuleKey(event: JsonRecord): string {
  const explicit = stringValue(event.module_key, '')
  if (explicit) return explicit
  const traceModule = record(event.trace_module)
  return stringValue(traceModule.module_key, stringValue(event.module, ''))
}

function localHotspotGroups(events: JsonRecord[]): JsonRecord[] {
  const grouped = new Map<string, JsonRecord[]>()
  for (const event of events) {
    const key = eventModuleKey(event) || eventModule(event)
    const current = grouped.get(key) ?? []
    current.push(event)
    grouped.set(key, current)
  }
  return [...grouped.entries()].map(([key, entries]) => ({
    module_key: key,
    module_name: eventModule(entries[0] ?? {}),
    system_collapsed: false,
    event_group_count: entries.length,
    returned_event_count: entries.length,
    per_module_limit: REASONING_HOTSPOTS_PER_MODULE,
    events: entries,
  }))
}

function groupEvents(group: JsonRecord): JsonRecord[] {
  return Array.isArray(group.events) ? group.events.map(record).filter((event) => Boolean(stringValue(event.event_id, ''))) : []
}

function traceLabel(trace: TraceSummary): string {
  const profile = stringValue(trace.analysis_profile, '')
  const input = trace.input_kind === 'raw_htrace' ? '.htrace' : trace.input_kind === 'external_trace_analysis_report' ? '追踪团队 JSON' : stringValue(trace.input_kind, 'trace')
  return `${input} · ${profile || trace.artifact_id}`
}

function historicalTargetPlatformProfile(analysis: TraceAnalysisRecord): TargetPlatformProfile | null {
  const ordered = [...analysis.inferences].sort((left, right) => right.startedAt.localeCompare(left.startedAt))
  for (const inference of ordered) {
    const candidate = inference.context.targetPlatformProfile
    if (!candidate || candidate.schema_version !== 'target_platform_profile/v1') continue
    if (!candidate.chipset?.model_id || !candidate.operating_system?.version_id) continue
    return candidate
  }
  return null
}

function tokenUsageFromAgentResponse(value: unknown, fallbackBudget: PromptTokenBudget): ModelTokenUsage {
  const usage = record(value)
  const inputTokens = numberValue(usage.inputTokens) ?? fallbackBudget.inputTokens
  const outputTokens = numberValue(usage.outputTokens) ?? 0
  const totalTokens = numberValue(usage.totalTokens) ?? inputTokens + outputTokens
  return {
    inputTokens,
    outputTokens,
    totalTokens,
    inputSource: usage.inputSource === 'provider' ? 'provider' : 'estimated',
    outputSource: usage.outputSource === 'provider' ? 'provider' : 'estimated',
    ...(numberValue(usage.cachedInputTokens) !== null ? { cachedInputTokens: numberValue(usage.cachedInputTokens) ?? undefined } : {}),
    ...(numberValue(usage.cacheHitRate) !== null ? { cacheHitRate: Math.min(1, numberValue(usage.cacheHitRate) ?? 0) } : {}),
  }
}

function tokenBudgetFromAgentResponse(value: unknown, fallback: PromptTokenBudget): PromptTokenBudget {
  const budget = record(value)
  return createPromptTokenBudget({
    contextWindowTokens: normalizeContextWindowTokens(numberValue(budget.contextWindowTokens) ?? fallback.contextWindowTokens),
    inputTokens: numberValue(budget.inputTokens) ?? fallback.inputTokens,
    outputReserveTokens: numberValue(budget.outputReserveTokens) ?? fallback.outputReserveTokens,
    compactionApplied: budget.compactionApplied === true || fallback.compactionApplied,
    compactedItems: numberValue(budget.compactedItems) ?? fallback.compactedItems,
  })
}

function resultSummary(artifact?: Artifact): JsonRecord {
  return record(artifact?.result)
}

function conciseAssistantText(value: unknown, fallback = ''): string {
  const text = stringValue(value, fallback).replace(/\s+/g, ' ').trim()
  return text.length > 560 ? `${text.slice(0, 557)}...` : text
}

function candidateScopeLevelLabel(level: string): string {
  return ({
    function: '函数范围',
    loop_region: '循环区',
    data_pipeline: '数据处理区',
    callee: '被调函数',
    unknown: '范围未声明',
  })[level] ?? level
}

function opportunityScopeSummary(opportunity: JsonRecord): string {
  const scope = record(opportunity.analysis_scope)
  const level = stringValue(scope.level)
  const status = stringValue(scope.status)
  const description = conciseAssistantText(scope.description)
  const sourceEvidence = conciseAssistantText(scope.source_evidence)
  if (!level && !description) return ''
  const statusLabel = status === 'callee_unresolved' ? '待展开' : status === 'not_declared' ? '模型未声明' : ''
  return [candidateScopeLevelLabel(level), statusLabel, description, sourceEvidence ? `依据：${sourceEvidence}` : ''].filter(Boolean).join(' · ')
}

function assistantConclusionMessage(input: {
  strategyLabel: string
  output: JsonRecord
  validationErrors: string[]
  adaptationActions: string[]
  calibrationActions: string[]
}): string {
  const opportunities = Array.isArray(input.output.opportunities) ? input.output.opportunities.map(record).slice(0, 3) : []
  const missingEvidence = Array.isArray(input.output.missing_evidence)
    ? input.output.missing_evidence.map((item) => conciseAssistantText(item)).filter(Boolean).slice(0, 6)
    : []
  const lines = [
    `“${input.strategyLabel}”已返回分析文本，并已回填策略结果与证据校验。`,
    '',
    '## 结论',
    conciseAssistantText(input.output.summary, '已生成受控分析结果，请结合下方策略面板查看完整证据。'),
    '',
    `- 建议：\`${conciseAssistantText(input.output.recommendation, 'evidence_required')}\``,
    `- 置信度：\`${conciseAssistantText(input.output.confidence, 'unknown')}\``,
    `- 证据状态：\`${conciseAssistantText(input.output.evidence_status, 'unknown')}\``,
  ]

  if (opportunities.length > 0) {
    lines.push('', '## 候选方向')
    opportunities.forEach((opportunity) => {
      const kind = conciseAssistantText(opportunity.opportunity_kind, '未分类候选')
      const target = conciseAssistantText(opportunity.candidate_target, '待确定目标')
      const rationale = conciseAssistantText(opportunity.rationale, '未给出可展示的理由。')
      const scope = opportunityScopeSummary(opportunity)
      lines.push(`- **${kind} / ${target}**${scope ? `（${scope}）` : ''}：${rationale}`)
    })
  }

  if (missingEvidence.length > 0) {
    lines.push('', '## 仍需证据')
    missingEvidence.forEach((item) => lines.push(`- ${item}`))
  }

  if (input.validationErrors.length > 0) {
    lines.push('', `**契约校验提示**：${input.validationErrors.length} 项问题需要保守核查：${input.validationErrors.map((item) => conciseAssistantText(item)).join('；')}`)
  } else if (input.calibrationActions.length > 0) {
    lines.push('', `**证据门禁**：${input.calibrationActions.map((item) => conciseAssistantText(item)).join('；')}`)
  } else if (input.adaptationActions.length > 0) {
    lines.push('', `**结构适配提示**：模型输出已按保守规则适配：${input.adaptationActions.map((item) => conciseAssistantText(item)).join('；')}`)
  }

  return lines.join('\n')
}

function progressList(value: unknown, limit = 3): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string' && item.trim().length > 0).slice(0, limit)
    : []
}

function progressEvidenceLabels(modelInput: JsonRecord): string[] {
  const records = Array.isArray(modelInput.context_records) ? modelInput.context_records.map(record) : []
  return records.slice(0, 3).map((item) => {
    const source = record(item.record)
    return stringValue(
      source.name,
      stringValue(source.record_id, stringValue(item.evidence_id, '上下文记录')),
    )
  }).filter(Boolean)
}

function hardwareRoutingProgress(modelInput: JsonRecord): { routeDetail: string; apiDetail: string; routeStatus: 'observed' | 'missing'; apiStatus: 'observed' | 'missing' } {
  const routing = record(modelInput.hardware_routing)
  const selectedUnits = Array.isArray(routing.selected_units) ? routing.selected_units.map(record) : []
  const apiLabels = (Array.isArray(modelInput.context_records) ? modelInput.context_records.map(record) : [])
    .filter((item) => {
      const evidenceId = stringValue(item.evidence_id, '')
      return evidenceId.startsWith('npu_api:') || evidenceId.startsWith('accelerator_api:')
    })
    .slice(0, 3)
    .map((item) => {
      const source = record(item.record)
      return stringValue(source.symbol, stringValue(source.name, stringValue(item.evidence_id, 'API 候选')))
    })
  if (selectedUnits.length === 0) {
    return {
      routeDetail: '硬件描述与可见代码信号没有选择出主单元；本轮不会扫描所有加速器 API 目录。',
      apiDetail: '未进入 API Top-N 精核；只能保留 CPU 并行或补充代码/数据形态证据的方向。',
      routeStatus: 'missing',
      apiStatus: 'missing',
    }
  }
  const selected = selectedUnits[0]
  const unit = stringValue(selected.unit_id, '主硬件单元')
  const name = stringValue(selected.hardware_name, stringValue(selected.hardware_id, '硬件知识库描述'))
  const reasons = progressList(selected.reasons, 2)
  return {
    routeDetail: `已由硬件描述选择 ${unit}：${name}${reasons.length > 0 ? `（${reasons.join('；')}）` : ''}。`,
    apiDetail: apiLabels.length > 0
      ? `只在 ${unit} 目录中召回并精核 ${apiLabels.length} 条 API Top-N：${apiLabels.join('、')}。`
      : `已选择 ${unit}，但该单元没有通过语义门槛的 API Top-N；不跨单元补充候选。`,
    routeStatus: 'observed',
    apiStatus: apiLabels.length > 0 ? 'observed' : 'missing',
  }
}

function progressScopeDetail(modelInput: JsonRecord): { count: number; detail: string } {
  const scopes = Array.isArray(modelInput.candidate_scopes) ? modelInput.candidate_scopes.map(record) : []
  if (scopes.length === 0) return { count: 0, detail: '当前函数未提取到词法候选片段，将以函数级结构保守分析。' }
  const labels = scopes.slice(0, 2).map((scope) => {
    const level = stringValue(scope.level, '代码片段')
    const lineStart = numberValue(scope.line_start)
    const lineEnd = numberValue(scope.line_end)
    const range = lineStart === null ? '' : lineEnd === null || lineEnd === lineStart ? `第 ${lineStart} 行` : `第 ${lineStart}-${lineEnd} 行`
    const callee = stringValue(scope.symbol, '')
    return [level === 'loop_region' ? '循环区' : level === 'data_pipeline' ? '数据处理区' : '被调函数', callee, range].filter(Boolean).join(' ')
  })
  return { count: scopes.length, detail: `已抽取 ${scopes.length} 个候选范围：${labels.join('；')}。` }
}

function progressTraceDetail(modelInput: JsonRecord): string {
  const metrics = record(record(modelInput.hotspot).trace_metrics)
  const selfTime = numberValue(metrics.self_time_estimate_total_duration_ms)
  const inclusiveTime = numberValue(metrics.inclusive_total_duration_ms)
  const occurrences = numberValue(metrics.occurrence_count)
  const parts = [
    selfTime === null ? '' : `self ${milliseconds(selfTime)}`,
    inclusiveTime === null ? '' : `inclusive ${milliseconds(inclusiveTime)}`,
    occurrences === null ? '' : `${Math.trunc(occurrences)} 次`,
  ].filter(Boolean)
  return parts.length > 0 ? parts.join('，') : 'Trace 指标有限；仅按可见代码与绑定信息分析。'
}

function progressCodeStructureDetail(modelInput: JsonRecord): string {
  const observations = record(modelInput.code_observations)
  const loops = numberValue(observations.loop_construct_count) ?? 0
  const ipc = progressList(observations.ipc_transaction_symbols)
  const locks = progressList(observations.lock_symbols)
  const parallel = progressList(observations.parallel_runtime_symbols)
  const math = progressList(observations.math_symbols)
  const parts = [
    loops > 0 ? `${loops} 个可见循环` : '',
    ipc.length > 0 ? `IPC：${ipc.join('、')}` : '',
    locks.length > 0 ? `同步：${locks.join('、')}` : '',
    parallel.length > 0 ? `并行运行时：${parallel.join('、')}` : '',
    math.length > 0 ? `数学调用：${math.join('、')}` : '',
  ].filter(Boolean)
  return parts.length > 0 ? parts.join('；') : '未见明确循环、IPC、锁或数学计算信号。'
}

function strategyProgressDirections(modelInput: JsonRecord, strategyId: StrategyId, evidenceLabels: string[], scopeDetail: string): AgentStrategyProgressDirection[] {
  const recordsDetail = evidenceLabels.length > 0
    ? `本轮会逐条对照：${evidenceLabels.join('、')}。`
    : '本轮没有可用的策略知识记录，不能据此推断具体硬件或接口。'
  const codeDetail = progressCodeStructureDetail(modelInput)
  if (strategyId === 'kb_rag') {
    return [
      { title: '已有异构与 CPU fallback 模式', detail: recordsDetail, evidenceStatus: evidenceLabels.length > 0 ? 'observed' : 'missing' },
      { title: '热点计算核心的候选范围', detail: scopeDetail, evidenceStatus: 'observed' },
      { title: '结构类比与 CPU 并行边界', detail: codeDetail, evidenceStatus: 'needs_validation' },
    ]
  }
  if (strategyId === 'hardware_kb') {
    const routing = hardwareRoutingProgress(modelInput)
    return [
      { title: '可观察的计算/数据作用域', detail: scopeDetail, evidenceStatus: 'observed' },
      { title: '硬件单元路由', detail: routing.routeDetail, evidenceStatus: routing.routeStatus },
      { title: '所选单元的 API Top-N', detail: routing.apiDetail, evidenceStatus: routing.apiStatus },
      { title: 'CPU 并行与端到端门禁', detail: codeDetail, evidenceStatus: 'needs_validation' },
    ]
  }
  return [
    { title: 'Trace 时间关系', detail: progressTraceDetail(modelInput), evidenceStatus: 'observed' },
    { title: '函数本体的可见结构', detail: codeDetail, evidenceStatus: 'observed' },
    { title: '并行或算法方向的前提', detail: '仅根据本轮可见代码判断任务独立性、串行边界与粒度；不推断具体硬件单元或 API。', evidenceStatus: 'needs_validation' },
  ]
}

function createAgentStrategyProgress(input: {
  context: Artifact
  strategyId: StrategyId
  requestId: string
  tokenBudget: PromptTokenBudget
  phase: AgentStrategyProgressPhase
  startedAt: string
  detail?: string
  status?: AgentStrategyProgressStatus
}): AgentStrategyProgress {
  const modelInput = reasoningModelInputContext(input.context)
  const evidenceLabels = progressEvidenceLabels(modelInput)
  const scope = progressScopeDetail(modelInput)
  const evidenceDetail = evidenceLabels.length > 0
    ? `已装载 ${evidenceLabels.length} 条策略记录。`
    : '本轮没有策略知识记录；仅保留 Trace、源码与目标画像事实。'
  const strategyLabel = strategyTitle(input.strategyId)
  const phaseDefaults: Record<AgentStrategyProgressPhase, { headline: string; detail: string; status: AgentStrategyProgressStatus }> = {
    model_pending: {
      headline: '模型正在交叉验证本轮候选方向',
      detail: `已发送“${strategyLabel}”请求；将结合 Trace、代码作用域和受控上下文生成结构化结论。`,
      status: 'running',
    },
    response_received: {
      headline: '模型文本已收到，正在进入结构化处理',
      detail: '原始回答已保留；推理实验室正在解析 JSON 并检查证据引用。',
      status: 'running',
    },
    contract_validating: {
      headline: '正在校验模型结论与证据门禁',
      detail: '核验结果 schema、证据 ID、候选数量、作用域声明和保守降级规则。',
      status: 'running',
    },
    completed: {
      headline: '策略结果已回填并完成证据校验',
      detail: '最终结论已写入策略卡和本次推理记录。',
      status: 'succeeded',
    },
    failed: {
      headline: '本次策略推理未完成',
      detail: '模型请求或结果处理失败；完整错误会保留在推理记录中。',
      status: 'failed',
    },
  }
  const current = phaseDefaults[input.phase]
  const modelStatus = input.phase === 'model_pending' ? 'running' : input.phase === 'failed' ? 'failed' : 'succeeded'
  const contractStatus = input.phase === 'model_pending' ? 'pending'
    : input.phase === 'response_received' || input.phase === 'contract_validating' ? 'running'
      : input.phase === 'completed' ? (input.status === 'warning' ? 'warning' : 'succeeded')
        : input.phase === 'failed' ? 'failed' : 'pending'
  return {
    schema_version: 'strategy_progress/v1',
    request_id: input.requestId,
    strategy_id: input.strategyId,
    strategy_label: strategyLabel,
    phase: input.phase,
    status: input.status ?? current.status,
    started_at: input.startedAt,
    headline: current.headline,
    detail: input.detail ?? current.detail,
    steps: [
      { id: 'evidence', title: 'Trace 与源码证据已就绪', detail: `${progressTraceDetail(modelInput)}；${evidenceDetail}`, status: 'succeeded' },
      { id: 'scope', title: '候选代码范围已确定', detail: scope.detail, status: 'succeeded' },
      { id: 'model', title: '结构化模型推理', detail: input.phase === 'model_pending' ? `输入 ${formatTokenCount(input.tokenBudget.inputTokens)} / 窗口 ${formatTokenCount(input.tokenBudget.contextWindowTokens)}，等待模型响应。` : '模型请求已返回到本地推理实验室。', status: modelStatus },
      { id: 'contract', title: '结果契约与证据校验', detail: input.phase === 'completed' ? (input.detail ?? current.detail) : input.phase === 'failed' ? (input.detail ?? current.detail) : '等待模型输出后核验证据、作用域与保守门禁。', status: contractStatus },
    ],
    directions: strategyProgressDirections(modelInput, input.strategyId, evidenceLabels, scope.detail),
  }
}

function createIndependentReasoningProgress(input: {
  requestId: string
  strategyId: 'blind_baseline' | 'blind_comparison'
  label: string
  startedAt: string
  tokenBudget: PromptTokenBudget
  phase?: AgentStrategyProgressPhase
  detail?: string
}): AgentStrategyProgress {
  const phase = input.phase ?? 'model_pending'
  const failed = phase === 'failed'
  const completed = phase === 'completed'
  return {
    schema_version: 'strategy_progress/v1',
    request_id: input.requestId,
    strategy_id: input.strategyId,
    strategy_label: input.label,
    phase,
    status: failed ? 'failed' : completed ? 'succeeded' : 'running',
    started_at: input.startedAt,
    headline: failed ? `${input.label}未完成` : completed ? `${input.label}已保存` : `${input.label}正在运行`,
    detail: input.detail ?? (input.strategyId === 'blind_baseline'
      ? '盲推基线只接收代码、用户问题和目标平台画像，不接收知识库或流程路由。'
      : '正在比较冻结的盲推理结论与正式流程结果。'),
    steps: [
      { id: 'evidence', title: '输入隔离', detail: input.strategyId === 'blind_baseline' ? '未装载 Trace、路由、KB、API Top-N 或流程结论。' : '只新增经过校验的流程结果，不改写正式流程工件。', status: 'succeeded' },
      { id: 'scope', title: '对照范围', detail: input.strategyId === 'blind_baseline' ? '当前热点代码与用户选择的目标平台画像。' : '同一热点的盲推理与流程推理结果。', status: 'succeeded' },
      { id: 'model', title: '独立模型推理', detail: `输入 ${formatTokenCount(input.tokenBudget.inputTokens)} / 窗口 ${formatTokenCount(input.tokenBudget.contextWindowTokens)}。`, status: failed ? 'failed' : completed ? 'succeeded' : 'running' },
      { id: 'contract', title: '结构化结果存档', detail: completed ? '结果已保存为不可变对照工件。' : failed ? (input.detail ?? '结果未能保存。') : '等待模型结果并校验 JSON 结构。', status: failed ? 'failed' : completed ? 'succeeded' : 'pending' },
    ],
    directions: [],
  }
}

function notifyAgentStrategyProgress(detail: AgentStrategyProgress) {
  if (typeof window === 'undefined') return
  const dispatch = () => window.dispatchEvent(new CustomEvent('reasoning-lab-agent-analysis-progress', { detail }))
  if (typeof window.requestAnimationFrame === 'function') window.requestAnimationFrame(dispatch)
  else dispatch()
}

function notifyAgentStrategyCompletion(detail: Record<string, unknown>) {
  if (typeof window === 'undefined') return
  const dispatch = () => window.dispatchEvent(new CustomEvent('reasoning-lab-agent-analysis-completed', { detail }))
  if (typeof window.requestAnimationFrame === 'function') window.requestAnimationFrame(dispatch)
  else dispatch()
}

function reasoningStageLabel(stage: ReasoningEvent['stage']): string {
  return ({ context: '上下文', request: '模型请求', response: '模型回答', contract: '契约处理', artifact: '结果存档' })[stage]
}

function reasoningStatusLabel(status: ReasoningEventStatus): string {
  return ({ running: '进行中', succeeded: '完成', warning: '需关注', failed: '失败' })[status]
}

function WorkflowGuideCue({ text }: { text: string }) {
  return <span className="workflow-guide-cue"><Hand className="workflow-guide-hand" size={15} aria-hidden="true" /><span>{text}</span><ArrowDown className="workflow-guide-arrow" size={30} aria-hidden="true" /></span>
}

export function ReasoningLab({
  deployment,
  sourceConnectorId,
  workflow,
  onWorkflowEvent,
  onResetAnalysis,
  onTraceAnalysisStarted,
  onTraceAnalysisHotspotUpdated,
  onInferenceRecorded,
  onInferenceUpdated,
  onDeviceValidationRecorded,
  targetPlatformProfile,
  onTargetPlatformProfileChange,
  onAssistantStateChange,
  restoreRequest,
  skillProfile,
}: {
  deployment?: DeploymentCapabilities
  sourceConnectorId: string
  workflow: OptimizationWorkflowState
  onWorkflowEvent: (event: OptimizationWorkflowEvent) => void
  onResetAnalysis: () => void
  onTraceAnalysisStarted: (trace: TraceAnalysisStart) => string
  onTraceAnalysisHotspotUpdated: (analysisId: string, patch: Partial<AnalysisHotspotSummary>) => void
  onInferenceRecorded: (analysisId: string, inference: AnalysisInferenceRecord) => void
  onInferenceUpdated: (analysisId: string, inferenceId: string, patch: Partial<AnalysisInferenceRecord>) => void
  onDeviceValidationRecorded: (analysisId: string, validation: AnalysisDeviceValidationRecord) => void
  targetPlatformProfile: TargetPlatformProfile
  onTargetPlatformProfileChange: (profile: TargetPlatformProfile) => void
  onAssistantStateChange: (context: ReasoningAssistantContext) => void
  restoreRequest?: ReasoningRecordRestoreRequest | null
  skillProfile: SkillVersionProfile | null
}) {
  const [traces, setTraces] = useState<TraceSummary[]>([])
  const [trace, setTrace] = useState<TraceSummary | null>(null)
  const [hotspots, setHotspots] = useState<JsonRecord[]>([])
  const [hotspotQuality, setHotspotQuality] = useState<JsonRecord>({})
  const [hotspotGroups, setHotspotGroups] = useState<JsonRecord[]>([])
  const [hotspotStageGroups, setHotspotStageGroups] = useState<JsonRecord[]>([])
  const [expandedHotspotModuleKeys, setExpandedHotspotModuleKeys] = useState<string[]>([])
  const [codeUnit, setCodeUnit] = useState<Artifact | null>(null)
  const [hotspotSourceResolution, setHotspotSourceResolution] = useState<Artifact | null>(null)
  const [contexts, setContexts] = useState<Partial<Record<StrategyId, Artifact>>>({})
  const [results, setResults] = useState<Partial<Record<StrategyId, Artifact>>>({})
  const [, setComparison] = useState<Artifact | null>(null)
  const [pastedCode, setPastedCode] = useState('')
  const [functionHint, setFunctionHint] = useState('')
  const [eventName, setEventName] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [notice, setNotice] = useState<Notice>(null)
  const [modelConfig, setModelConfig] = useState<BrowserModelConfig>(() => createDefaultBrowserModelConfig())
  const [strategyFeedback, setStrategyFeedback] = useState<Partial<Record<StrategyId, string>>>({})
  const [strategyTokenStats, setStrategyTokenStats] = useState<Partial<Record<StrategyId, { budget: PromptTokenBudget; usage?: ModelTokenUsage }>>>({})
  const [reasoningEvents, setReasoningEvents] = useState<ReasoningEvent[]>([])
  const [analysisRecordId, setAnalysisRecordId] = useState<string | null>(null)
  const [hardwareSemanticRoute, setHardwareSemanticRoute] = useState<HardwareSemanticRoute | null>(null)
  const [blindContext, setBlindContext] = useState<Artifact | null>(null)
  const [blindResult, setBlindResult] = useState<Artifact | null>(null)
  const [blindComparison, setBlindComparison] = useState<Artifact | null>(null)
  const [blindFeedback, setBlindFeedback] = useState('')
  const [blindTokenStats, setBlindTokenStats] = useState<{ budget: PromptTokenBudget; usage?: ModelTokenUsage } | null>(null)
  const [hardwareRoutingSkipped, setHardwareRoutingSkipped] = useState(false)
  const [manualOpportunityResult, setManualOpportunityResult] = useState<Artifact | null>(null)
  const [apiSelectorOpen, setApiSelectorOpen] = useState(false)
  const [apiSelectorBusy, setApiSelectorBusy] = useState(false)
  const [apiSelectorRecords, setApiSelectorRecords] = useState<JsonRecord[]>([])
  const [apiSelectorQuery, setApiSelectorQuery] = useState('')
  const [selectedApiRecordId, setSelectedApiRecordId] = useState('')
  const strategyStepRef = useRef<HTMLElement | null>(null)
  const deviceStepRef = useRef<HTMLElement | null>(null)
  const agentResponseWaiters = useRef(new Map<string, AgentResponseWaiter>())
  const handledRestoreRequestRef = useRef<string | null>(null)
  const activeRestoreRequestRef = useRef<string | null>(null)
  const manualRecordCreationKeyRef = useRef<string | null>(null)
  const strategyAbortRef = useRef<AbortController | null>(null)
  const pasteSubmissionRef = useRef(false)
  const apiSelectorRequestRef = useRef(false)
  const apiSelectionSubmitRef = useRef(false)
  const [strategyCommandSubmission, setStrategyCommandSubmission] = useState<{ requestId: string; strategyId: StrategyId; accepted: boolean } | null>(null)
  const strategyCommandRequestRef = useRef('')
  const strategyCommandTimeoutRef = useRef<number | null>(null)

  const clearStrategyCommandSubmission = useCallback(() => {
    strategyCommandRequestRef.current = ''
    if (strategyCommandTimeoutRef.current !== null) {
      window.clearTimeout(strategyCommandTimeoutRef.current)
      strategyCommandTimeoutRef.current = null
    }
    setStrategyCommandSubmission(null)
  }, [])

  const submitStrategyCommand = useCallback((strategyId: StrategyId, title: string) => {
    if (strategyCommandRequestRef.current) return
    const requestId = randomUuid()
    strategyCommandRequestRef.current = requestId
    setStrategyCommandSubmission({ requestId, strategyId, accepted: false })
    setNotice(null)
    strategyCommandTimeoutRef.current = window.setTimeout(() => {
      if (strategyCommandRequestRef.current !== requestId) return
      clearStrategyCommandSubmission()
      setNotice({ tone: 'error', text: '页面助手在 60 秒内尚未开始机会推理，按钮已解锁；请查看右侧 Agent 状态后重试。' })
    }, 60_000)
    dispatchAgentCommand({
      requestId,
      analysisId: analysisRecordId ?? undefined,
      displayMessage: `运行${title}`,
      message: `执行“${title}”：直接调用平台 MCP 工具 reasoning_prepare_context，strategy_id 为 ${strategyId}，获取当前热点、目标平台、证据记录和输出契约；按契约形成判断后，调用 reasoning_run_strategy，传回 strategy_id、原始 context_artifact_id 和 agent_result。无需搜索平台源码或页面 Action ID。`,
    })
  }, [analysisRecordId, clearStrategyCommandSubmission])

  useEffect(() => {
    const listener = (event: Event) => {
      const detail = record((event as CustomEvent<unknown>).detail)
      const requestId = stringValue(detail.request_id, '')
      if (!requestId || requestId !== strategyCommandRequestRef.current) return
      if (stringValue(detail.status, '') === 'rejected') {
        clearStrategyCommandSubmission()
        setNotice({ tone: 'error', text: stringValue(detail.message, '页面助手未接收本次机会推理命令。') })
        return
      }
      setStrategyCommandSubmission((current) => current?.requestId === requestId ? { ...current, accepted: true } : current)
    }
    window.addEventListener(agentCommandStatusEventName, listener)
    return () => window.removeEventListener(agentCommandStatusEventName, listener)
  }, [clearStrategyCommandSubmission])

  useEffect(() => {
    if (!strategyCommandSubmission) return
    if (busy === `run:${strategyCommandSubmission.strategyId}`) clearStrategyCommandSubmission()
  }, [busy, clearStrategyCommandSubmission, strategyCommandSubmission])

  useEffect(() => {
    clearStrategyCommandSubmission()
  }, [clearStrategyCommandSubmission, codeUnit?.artifact_id, hardwareSemanticRoute?.resultArtifact.artifact_id])

  useEffect(() => () => {
    if (strategyCommandTimeoutRef.current !== null) window.clearTimeout(strategyCommandTimeoutRef.current)
  }, [])

  const appendReasoningEvent = useCallback((event: Omit<ReasoningEvent, 'id' | 'timestamp'>): string => {
    const id = randomUuid()
    const timestamp = new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date())
    setReasoningEvents((current) => [...current, { ...event, id, timestamp }].slice(-80))
    return id
  }, [])

  const updateReasoningEvent = useCallback((id: string, patch: Partial<Pick<ReasoningEvent, 'status' | 'title' | 'detail' | 'payload' | 'payloadLabel'>>) => {
    setReasoningEvents((current) => current.map((event) => event.id === id ? { ...event, ...patch } : event))
  }, [])

  const armAgentResponseTimeout = useCallback((requestId: string, waiter: AgentResponseWaiter) => {
    window.clearTimeout(waiter.timeoutId)
    waiter.timeoutId = window.setTimeout(() => {
      if (agentResponseWaiters.current.get(requestId) !== waiter) return
      agentResponseWaiters.current.delete(requestId)
      waiter.reject(new Error('页面助手连续 4 分 30 秒未收到模型流式输出或最终响应'))
    }, STRATEGY_RESPONSE_TIMEOUT_MS)
  }, [])

  const handleHardwareSemanticRouteResolved = useCallback((route: { requestId?: string; codeArtifactId: string; resultArtifact: Artifact }) => {
    if (!route.codeArtifactId) return
    setHardwareSemanticRoute({ codeArtifactId: route.codeArtifactId, resultArtifact: route.resultArtifact })
    setHardwareRoutingSkipped(false)
    setManualOpportunityResult(null)
    setBlindComparison(null)
    setContexts((current) => {
      const { hardware_kb: _hardwareContext, ...rest } = current
      return rest
    })
    setResults((current) => {
      const { hardware_kb: _hardwareResult, ...rest } = current
      return rest
    })
    setStrategyFeedback((current) => {
      const { hardware_kb: _hardwareFeedback, ...rest } = current
      return rest
    })
    setStrategyTokenStats((current) => {
      const { hardware_kb: _hardwareTokens, ...rest } = current
      return rest
    })
    const routeResult = record(route.resultArtifact.result)
    onWorkflowEvent({
      type: 'hardware_route_ready',
      routeOrigin: stringValue(routeResult.selection_origin, 'model') === 'user_manual' ? 'user_manual' : 'model',
    })
    window.setTimeout(() => respondReasoningLabAction(route.requestId, {
        status: 'succeeded',
        code_artifact_id: route.codeArtifactId,
        routing_result_artifact_id: route.resultArtifact.artifact_id,
        decision: stringValue(routeResult.decision, ''),
        summary: stringValue(routeResult.summary, ''),
        selected_units: (Array.isArray(routeResult.selected_units)
          ? routeResult.selected_units
          : Array.isArray(routeResult.candidates)
            ? routeResult.candidates
            : [])
          .map((item) => record(item))
          .slice(0, 2),
      }), 0)
    if (stringValue(routeResult.selection_origin, 'model') === 'user_manual') {
      window.requestAnimationFrame(() => strategyStepRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }))
    }
  }, [onWorkflowEvent])

  const handleHardwareSemanticRouteFailed = useCallback((route: { requestId?: string; message: string }) => {
    rejectReasoningLabAction(route.requestId, route.message)
  }, [])

  const skipHardwareSemanticRoute = useCallback(() => {
    if (!codeUnit) return
    setHardwareSemanticRoute(null)
    setHardwareRoutingSkipped(true)
    setManualOpportunityResult(null)
    setNotice({ tone: 'info', text: '已跳过硬件路由。当前界面只保留硬件机会推理，因此不会生成可进入真机验证的优化方向；可返回第 3 步直接选择硬件或并行优化。' })
    onWorkflowEvent({ type: 'hardware_route_skipped' })
  }, [codeUnit, onWorkflowEvent])

  const rerunHardwareSemanticRoute = useCallback(() => {
    if (!codeUnit) return
    setBlindComparison(null)
    setNotice({ tone: 'info', text: '正在根据当前代码和后续复核需求重新执行硬件单元路由；旧路由与策略保留在历史记录中。' })
    dispatchAgentCommand({
      analysisId: analysisRecordId ?? undefined,
      displayMessage: '重新评估硬件路由',
      message: '重新评估当前硬件方向：直接调用平台 MCP 工具 reasoning_prepare_hardware_semantic_route，重新比较本轮全部平台与用户自建能力卡，不机械继承上一轮结论；然后调用 reasoning_run_hardware_semantic_route，传回原始 context_artifact_id 和符合契约的 agent_result。无需搜索平台源码或页面 Action ID。',
    })
  }, [analysisRecordId, codeUnit])

  const adoptManualOpportunity = useCallback((response: ManualOpportunitySelectionResponse, label: string) => {
    const strategyId = isStrategyId(stringValue(response.artifact.strategy_id, ''))
      ? stringValue(response.artifact.strategy_id) as StrategyId
      : 'hardware_kb'
    setContexts((current) => ({ ...current, [strategyId]: response.context_artifact }))
    setResults((current) => ({ ...current, [strategyId]: response.artifact }))
    setManualOpportunityResult(response.artifact)
    setComparison(null)
    setStrategyFeedback((current) => ({ ...current, [strategyId]: `${label}已由用户直接选择；未调用机会推理模型。` }))
    const now = new Date().toISOString()
    if (analysisRecordId) {
      onInferenceRecorded(analysisRecordId, {
        inferenceId: randomUuid(),
        runId: randomUuid(),
        strategyId,
        strategyLabel: label,
        status: 'succeeded',
        startedAt: now,
        completedAt: now,
        model: { provider: 'user_manual', name: 'direct_selection' },
        context: snapshotAnalysisContext(response.context_artifact),
        resultArtifactId: response.artifact.artifact_id,
        summary: stringValue(record(response.artifact.result).summary, `${label}已保存。`),
      })
    }
    onWorkflowEvent({ type: 'manual_opportunity_selected', detail: `${label}已保存为用户直接选择的待验证方向。` })
    setNotice({ tone: 'success', text: `${label}已保存；已跳过模型机会推理，可直接开始真机验证。` })
    window.requestAnimationFrame(() => deviceStepRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }))
  }, [analysisRecordId, onInferenceRecorded, onWorkflowEvent])

  const selectParallelOptimization = useCallback(async () => {
    if (!codeUnit) throw new Error('请先关联或粘贴热点代码。')
    const response = await api<ManualOpportunitySelectionResponse>('/v1/projects/local-default/manual-opportunity-selections', {
      method: 'POST',
      body: JSON.stringify({
        code_artifact_id: codeUnit.artifact_id,
        selection_kind: 'parallel_cpu',
        target_platform_profile: targetPlatformProfile,
      }),
    })
    setHardwareSemanticRoute(null)
    setHardwareRoutingSkipped(false)
    adoptManualOpportunity(response, '并行优化')
  }, [adoptManualOpportunity, codeUnit, targetPlatformProfile])

  const openApiSelector = useCallback(async () => {
    if (apiSelectorRequestRef.current) return
    if (!codeUnit || hardwareSemanticRoute?.codeArtifactId !== codeUnit.artifact_id) {
      setNotice({ tone: 'info', text: '请先在第 3 步完成语义路由，或直接选择一个硬件单元。' })
      return
    }
    const unitId = routedHardwareUnit(hardwareSemanticRoute, codeUnit.artifact_id)
    if (!unitId) {
      setNotice({ tone: 'info', text: '当前路由没有可用硬件单元，无法打开对应 API 列表。' })
      return
    }
    apiSelectorRequestRef.current = true
    setApiSelectorRecords([])
    setSelectedApiRecordId('')
    setApiSelectorQuery('')
    setApiSelectorOpen(true)
    setApiSelectorBusy(true)
    setNotice(null)
    try {
      const response = await loadHardwareApiRecords<{ records: JsonRecord[] }>(unitId)
      setApiSelectorRecords(response.records)
      setSelectedApiRecordId(stringValue(response.records[0]?.record_id, ''))
    } catch (error) {
      setApiSelectorOpen(false)
      setNotice({ tone: 'error', text: error instanceof Error ? error.message : '无法读取所选硬件的 API 列表。' })
    } finally {
      apiSelectorRequestRef.current = false
      setApiSelectorBusy(false)
    }
  }, [codeUnit, hardwareSemanticRoute])

  const applyManualApiSelection = useCallback(async () => {
    if (apiSelectionSubmitRef.current) return
    if (!codeUnit || hardwareSemanticRoute?.codeArtifactId !== codeUnit.artifact_id || !selectedApiRecordId) return
    const routeResult = record(hardwareSemanticRoute.resultArtifact.result)
    const candidates = Array.isArray(routeResult.candidates) ? routeResult.candidates.map(record) : []
    const unitId = candidates.map((candidate) => stringValue(candidate.unit_id, '')).find(Boolean)
    if (!unitId) return
    const selectedRecord = apiSelectorRecords.find((item) => stringValue(item.record_id, '') === selectedApiRecordId)
    const label = stringValue(selectedRecord?.symbol, stringValue(selectedRecord?.name_zh, stringValue(selectedRecord?.name, selectedApiRecordId)))
    apiSelectionSubmitRef.current = true
    setApiSelectorBusy(true)
    try {
      const response = await api<ManualOpportunitySelectionResponse>('/v1/projects/local-default/manual-opportunity-selections', {
        method: 'POST',
        body: JSON.stringify({
          code_artifact_id: codeUnit.artifact_id,
          selection_kind: 'hardware_api',
          unit_id: unitId,
          api_record_id: selectedApiRecordId,
          hardware_semantic_routing_result_artifact_id: hardwareSemanticRoute.resultArtifact.artifact_id,
          target_platform_profile: targetPlatformProfile,
        }),
      })
      setApiSelectorOpen(false)
      adoptManualOpportunity(response, `直接选择：${label}`)
    } catch (error) {
      setNotice({ tone: 'error', text: error instanceof Error ? error.message : '直接优化方向保存失败。' })
    } finally {
      apiSelectionSubmitRef.current = false
      setApiSelectorBusy(false)
    }
  }, [adoptManualOpportunity, apiSelectorRecords, codeUnit, hardwareSemanticRoute, selectedApiRecordId, targetPlatformProfile])

  const resetDownstream = useCallback(() => {
    setCodeUnit(null)
    setHotspotSourceResolution(null)
    setContexts({})
    setResults({})
    setComparison(null)
    setStrategyFeedback({})
    setStrategyTokenStats({})
    setReasoningEvents([])
    setHardwareSemanticRoute(null)
    setHardwareRoutingSkipped(false)
    setManualOpportunityResult(null)
    setApiSelectorOpen(false)
    setApiSelectorRecords([])
    setApiSelectorQuery('')
    setSelectedApiRecordId('')
  }, [])

  useEffect(() => {
    if (trace || analysisRecordId || !codeUnit || codeUnit.source_connector_id !== sourceConnectorId) return
    if (manualRecordCreationKeyRef.current === codeUnit.artifact_id) return
    manualRecordCreationKeyRef.current = codeUnit.artifact_id
    const hotspot = record(codeUnit.hotspot)
    const label = stringValue(hotspot.event_name, stringValue(hotspot.function_hint, '用户粘贴代码'))
    const recordId = onTraceAnalysisStarted({
      artifactId: codeUnit.artifact_id,
      label: `手动代码：${label}`,
      inputKind: 'manual_code',
      sourceConnectorId,
    })
    setAnalysisRecordId(recordId)
    onTraceAnalysisHotspotUpdated(recordId, {
      eventName: label,
      sourceKind: 'pasted_code',
      codeArtifactId: codeUnit.artifact_id,
    })
  }, [analysisRecordId, codeUnit, onTraceAnalysisHotspotUpdated, onTraceAnalysisStarted, sourceConnectorId, trace])

  const refreshTraces = useCallback(async () => {
    if (!sourceConnectorId) return
    setBusy('traces')
    try {
      const response = await api<{ traces: TraceSummary[] }>(`/v1/projects/local-default/trace-analysis-results?source_connector_id=${encodeURIComponent(sourceConnectorId)}`)
      setTraces((previous) => {
        const refreshed = new Map(response.traces.map((item) => [item.artifact_id, item]))
        previous.filter((item) => item.source_connector_id === sourceConnectorId).forEach((item) => {
          if (!refreshed.has(item.artifact_id)) refreshed.set(item.artifact_id, item)
        })
        return [...refreshed.values()]
      })
      setTrace((previous) => {
        if (!previous) return null
        return response.traces.find((item) => item.artifact_id === previous.artifact_id)
          ?? (previous.source_connector_id === sourceConnectorId ? previous : null)
      })
    } catch (error) {
      setNotice({ tone: 'error', text: error instanceof Error ? error.message : '无法读取真实 trace 分析工件' })
    } finally {
      setBusy(null)
    }
  }, [sourceConnectorId])

  const readHotspotCatalog = useCallback(async (selectedTrace: TraceSummary): Promise<ReasoningHotspotCatalog> => {
    const response = await api<{ events: JsonRecord[]; quality: JsonRecord; module_catalog: JsonRecord; module_event_groups?: JsonRecord[]; stage_event_groups?: JsonRecord[] }>('/v1/projects/local-default/reasoning-trace-hotspots', {
      method: 'POST', body: JSON.stringify({
        analysis_artifact_id: selectedTrace.artifact_id,
        limit: 100,
        group_by_module: true,
        per_module_limit: REASONING_HOTSPOTS_PER_MODULE,
      }),
    })
    const groups = Array.isArray(response.module_event_groups) && response.module_event_groups.length > 0
      ? response.module_event_groups.map(record)
      : localHotspotGroups(response.events)
    const stages = Array.isArray(response.stage_event_groups) ? response.stage_event_groups.map(record) : []
    return { groups, stages, events: stages.length > 0 ? stages.flatMap(groupEvents) : groups.flatMap(groupEvents), quality: record(response.quality) }
  }, [])

  const selectTrace = useCallback((selected: TraceSummary | null): string | null => {
    setTrace(selected)
    setHotspots([])
    setHotspotQuality({})
    setHotspotGroups([])
    setHotspotStageGroups([])
    setExpandedHotspotModuleKeys([])
    resetDownstream()
    if (!selected) {
      setAnalysisRecordId(null)
      return null
    }
    const selectedTraceLabel = traceLabel(selected)
    const nextRecordId = onTraceAnalysisStarted({
      artifactId: selected.artifact_id,
      label: selectedTraceLabel,
      inputKind: stringValue(selected.input_kind, 'trace'),
      sourceConnectorId: selected.source_connector_id || sourceConnectorId,
    })
    setAnalysisRecordId(nextRecordId)
    onWorkflowEvent({ type: 'trace_selected', traceLabel: selectedTraceLabel })
    return nextRecordId
  }, [onTraceAnalysisStarted, onWorkflowEvent, resetDownstream, sourceConnectorId])

  const loadHotspotsForTrace = useCallback(async (
    selectedTrace: TraceSummary,
    recordId = analysisRecordId,
  ) => {
    setBusy('hotspots')
    try {
      const catalog = await readHotspotCatalog(selectedTrace)
      setHotspots(catalog.events)
      setHotspotQuality(catalog.quality)
      setHotspotGroups(catalog.groups)
      setHotspotStageGroups(catalog.stages)
      const nonSystemKeys = catalog.groups
        .filter((group) => !group.system_collapsed)
        .map((group) => stringValue(group.module_key, ''))
        .filter(Boolean)
      const fallbackKeys = catalog.groups.map((group) => stringValue(group.module_key, '')).filter(Boolean)
      setExpandedHotspotModuleKeys((nonSystemKeys.length > 0 ? nonSystemKeys : fallbackKeys).slice(0, DEFAULT_EXPANDED_HOTSPOT_MODULES))
      resetDownstream()
      const available = catalog.events.length > 0
      const anchorOnly = stringValue(catalog.quality.status, '') === 'degraded'
      const detail = !available
        ? stringValue(catalog.quality.reason, '该 Trace 没有可用的热点目录。')
        : catalog.stages.length > 0
          ? `已按 ${catalog.stages.length} 个阶段展示报告携带的全部热点记录；阶段报告计数与实际列出数分别保留。`
          : anchorOnly
            ? `已按 ${catalog.groups.length} 个模块提取可调查 Trace 锚点；当前报告未提供可归因的代表热点时长。`
            : `已按 ${catalog.groups.length} 个模块提取热点，每个模块最多展示 Top ${REASONING_HOTSPOTS_PER_MODULE}。`
      setNotice(catalog.events.length > 0
        ? catalog.stages.length > 0
          ? { tone: 'success', text: `已按 ${catalog.stages.length} 个阶段整理 ${catalog.events.length} 个可调查热点组；界面不再做模块 Top-N 裁剪。` }
          : anchorOnly
          ? { tone: 'info', text: `已提取 ${catalog.events.length} 个可调查 Trace 锚点，可继续定位源码与 callee；当前没有 CPU self-time 结论。` }
          : { tone: 'success', text: `已按 ${catalog.groups.length} 个模块整理热点，每个模块展示 Top ${REASONING_HOTSPOTS_PER_MODULE}。` }
        : { tone: 'info', text: stringValue(catalog.quality.reason, '该 trace 没有可用的热点目录。') })
      if (recordId) onTraceAnalysisHotspotUpdated(recordId, { candidateCount: catalog.events.length })
      onWorkflowEvent({ type: 'hotspots_extracted', count: catalog.events.length, available, detail })
    } catch (error) {
      setNotice({ tone: 'error', text: error instanceof Error ? error.message : '真实 trace 热点提取失败' })
    } finally {
      setBusy(null)
    }
  }, [analysisRecordId, onTraceAnalysisHotspotUpdated, onWorkflowEvent, readHotspotCatalog, resetDownstream])

  const loadHotspots = useCallback(async () => {
    if (!trace) {
      setNotice({ tone: 'info', text: '请先选择已分析的真实 trace，或导入 .htrace / trace 团队 JSON。' })
      return
    }
    await loadHotspotsForTrace(trace, analysisRecordId)
  }, [analysisRecordId, loadHotspotsForTrace, trace])

  const restoreHistoricalRecord = useCallback(async (analysis: TraceAnalysisRecord, requestId: string, workspace?: AnalysisWorkspaceSnapshot | null) => {
    const isCurrentRequest = () => activeRestoreRequestRef.current === requestId
    const isManualCodeRecord = analysis.trace.inputKind === 'manual_code'
    setBusy('restore')
    setNotice({ tone: 'info', text: `正在装载历史记录“${analysis.trace.label}”的不可变${isManualCodeRecord ? '代码与推理' : ' Trace 与推理'}工件…` })
    try {
      if (analysis.trace.sourceConnectorId && analysis.trace.sourceConnectorId !== sourceConnectorId) {
        throw new Error(`该记录绑定的${isManualCodeRecord ? '代码仓' : ' Trace'}与当前代码仓不一致，已拒绝装载`)
      }
      let catalog: ReasoningHotspotCatalog = { groups: [], stages: [], events: [], quality: {} }
      if (isManualCodeRecord) {
        // A pasted-code run has no trace catalog. Its immutable code artifact
        // is restored below and becomes the complete workbench input.
        setTrace(null)
        setHotspots([])
        setHotspotQuality({})
        setHotspotGroups([])
        setHotspotStageGroups([])
        setExpandedHotspotModuleKeys([])
      } else {
        const traceResponse = await api<{ trace: TraceSummary }>(`/v1/projects/local-default/trace-analysis-results/${encodeURIComponent(analysis.trace.artifactId)}`)
        if (!isCurrentRequest()) return
        const restoredTrace = traceResponse.trace
        if (!restoredTrace?.artifact_id || restoredTrace.source_connector_id !== sourceConnectorId) {
          throw new Error('该记录绑定的 Trace 与当前代码仓不一致，已拒绝装载')
        }
        catalog = await readHotspotCatalog(restoredTrace)
        if (!isCurrentRequest()) return
        setTraces((previous) => [restoredTrace, ...previous.filter((item) => item.artifact_id !== restoredTrace.artifact_id)])
        setTrace(restoredTrace)
        setHotspots(catalog.events)
        setHotspotQuality(catalog.quality)
        setHotspotGroups(catalog.groups)
        setHotspotStageGroups(catalog.stages)
        const recordedEventId = stringValue(analysis.hotspot?.eventId, '')
        const recordedEvent = catalog.events.find((item) => stringValue(item.event_id, '') === recordedEventId)
        const nonSystemKeys = catalog.groups
          .filter((group) => !group.system_collapsed)
          .map((group) => stringValue(group.module_key, ''))
          .filter(Boolean)
        const fallbackKeys = catalog.groups.map((group) => stringValue(group.module_key, '')).filter(Boolean)
        const defaultExpanded = (nonSystemKeys.length > 0 ? nonSystemKeys : fallbackKeys).slice(0, DEFAULT_EXPANDED_HOTSPOT_MODULES)
        setExpandedHotspotModuleKeys(Array.from(new Set([eventModuleKey(recordedEvent ?? {}), ...defaultExpanded].filter(Boolean))))
      }
      setAnalysisRecordId(analysis.analysisId)
      resetDownstream()
      setPastedCode('')
      setFunctionHint('')
      setEventName('')

      const unavailable: string[] = []
      let restoredCode: Artifact | null = null
      const codeArtifactId = stringValue(analysis.hotspot?.codeArtifactId, isManualCodeRecord ? analysis.trace.artifactId : '')
      if (codeArtifactId) {
        try {
          const codeResponse = await api<{ artifact: Artifact }>(`/v1/projects/local-default/hotspot-code/${encodeURIComponent(codeArtifactId)}`)
          if (!isCurrentRequest()) return
          if (codeResponse.artifact.source_connector_id !== sourceConnectorId) {
            throw new Error('历史热点代码与当前代码仓不一致')
          }
          restoredCode = codeResponse.artifact
          const restoredHotspot = record(restoredCode.hotspot)
          setCodeUnit(restoredCode)
          setPastedCode(stringValue(restoredCode.code, ''))
          setFunctionHint(stringValue(restoredHotspot.function_hint, ''))
          setEventName(stringValue(restoredHotspot.event_name, analysis.hotspot?.eventName ?? ''))
        } catch (error) {
          unavailable.push(`热点代码：${error instanceof Error ? error.message : '不可读取'}`)
        }
      } else {
        unavailable.push('此记录未保存热点代码工件')
      }

      const restoredContexts: Partial<Record<StrategyId, Artifact>> = {}
      const restoredResults: Partial<Record<StrategyId, Artifact>> = {}
      let restoredRoute: HardwareSemanticRoute | null = null
      let restoredBlindResult: Artifact | null = null
      let restoredBlindComparison: Artifact | null = null
      if (restoredCode) {
        const persistedReasoning = record(workspace?.snapshot.reasoning)
        const persistedRouting = record(persistedReasoning.hardware_routing)
        const routingResultArtifactId = stringValue(persistedRouting.result_artifact_id, '')
        if (routingResultArtifactId) {
          try {
            const routeResponse = await api<{ artifact: Artifact }>(`/v1/projects/local-default/hardware-semantic-routing/results/${encodeURIComponent(routingResultArtifactId)}`)
            const routeArtifact = routeResponse.artifact
            const routeContextId = stringValue(routeArtifact.context_artifact_id, '')
            const routeContextResponse = await api<{ artifact: Artifact }>(`/v1/projects/local-default/hardware-semantic-routing/${encodeURIComponent(routeContextId)}`)
            const routeInputs = Array.isArray(routeContextResponse.artifact.input_artifacts)
              ? routeContextResponse.artifact.input_artifacts.map((item) => stringValue(item, ''))
              : []
            if (!routeInputs.includes(`artifact:${restoredCode.artifact_id}`)) {
              throw new Error('历史硬件路由与热点代码的不可变关联校验失败')
            }
            restoredRoute = { codeArtifactId: restoredCode.artifact_id, resultArtifact: routeArtifact }
          } catch (error) {
            unavailable.push(`硬件单元路由：${error instanceof Error ? error.message : '不可读取'}`)
          }
        }
        const latestByStrategy = new Map<StrategyId, AnalysisInferenceRecord>()
        ;[...analysis.inferences]
          .sort((left, right) => right.startedAt.localeCompare(left.startedAt))
          .forEach((inference) => {
            if (isStrategyId(inference.strategyId) && inference.context.contextArtifactId && !latestByStrategy.has(inference.strategyId)) {
              latestByStrategy.set(inference.strategyId, inference)
            }
          })
        await Promise.all([...latestByStrategy.entries()].map(async ([strategyId, inference]) => {
          try {
            const contextResponse = await api<{ artifact: Artifact }>(`/v1/projects/local-default/reasoning-contexts/${encodeURIComponent(inference.context.contextArtifactId)}`)
            const contextArtifact = contextResponse.artifact
            const contextInputs = Array.isArray(contextArtifact.input_artifacts) ? contextArtifact.input_artifacts.map((item) => stringValue(item, '')) : []
            if (contextArtifact.strategy_id !== strategyId
              || contextArtifact.source_connector_id !== restoredCode.source_connector_id
              || !contextInputs.includes(`artifact:${restoredCode.artifact_id}`)) {
              throw new Error('历史策略上下文与热点代码的不可变关联校验失败')
            }
            restoredContexts[strategyId] = contextArtifact
            if (!inference.resultArtifactId) return
            const resultResponse = await api<{ artifact: Artifact }>(`/v1/projects/local-default/reasoning-results/${encodeURIComponent(inference.resultArtifactId)}`)
            const resultArtifact = resultResponse.artifact
            if (resultArtifact.strategy_id !== strategyId || resultArtifact.context_artifact_id !== contextArtifact.artifact_id) {
              throw new Error('历史策略结果与上下文的不可变关联校验失败')
            }
            restoredResults[strategyId] = resultArtifact
          } catch (error) {
            unavailable.push(`${strategyTitle(strategyId)}：${error instanceof Error ? error.message : '不可读取'}`)
          }
        }))
        const latestBlind = [...analysis.inferences]
          .filter((inference) => inference.strategyId === 'blind_baseline' && inference.resultArtifactId)
          .sort((left, right) => right.startedAt.localeCompare(left.startedAt))[0]
        if (latestBlind?.resultArtifactId) {
          try {
            const response = await api<{ artifact: Artifact }>(`/v1/projects/local-default/blind-reasoning/results/${encodeURIComponent(latestBlind.resultArtifactId)}`)
            if (stringValue(response.artifact.code_artifact_id, '') !== restoredCode.artifact_id) throw new Error('盲推理结果与历史热点代码不一致')
            restoredBlindResult = response.artifact
          } catch (error) {
            unavailable.push(`独立盲推理：${error instanceof Error ? error.message : '不可读取'}`)
          }
        }
        const latestBlindComparison = [...analysis.inferences]
          .filter((inference) => inference.strategyId === 'blind_comparison' && inference.resultArtifactId)
          .sort((left, right) => right.startedAt.localeCompare(left.startedAt))[0]
        if (latestBlindComparison?.resultArtifactId) {
          try {
            const response = await api<{ artifact: Artifact }>(`/v1/projects/local-default/blind-reasoning/comparisons/${encodeURIComponent(latestBlindComparison.resultArtifactId)}`)
            restoredBlindComparison = response.artifact
          } catch (error) {
            unavailable.push(`盲推理对照：${error instanceof Error ? error.message : '不可读取'}`)
          }
        }
      }
      if (!isCurrentRequest()) return

      setContexts(restoredContexts)
      setResults(restoredResults)
      setHardwareSemanticRoute(restoredRoute)
      const persistedReasoning = record(workspace?.snapshot.reasoning)
      const persistedRouting = record(persistedReasoning.hardware_routing)
      setHardwareRoutingSkipped(stringValue(persistedRouting.status, '') === 'skipped')
      setSelectedApiRecordId(stringValue(persistedReasoning.selected_api_record_id, ''))
      setBlindContext(null)
      setBlindResult(restoredBlindResult)
      setBlindComparison(restoredBlindComparison)
      setBlindFeedback(restoredBlindResult ? '已装载历史盲推理基线；不会重新调用模型。' : '')
      setManualOpportunityResult(
        Object.values(restoredResults).find((artifact) => stringValue(record(artifact?.manual_selection).origin, '') === 'user_manual') ?? null,
      )
      setComparison(null)
      setReasoningEvents([...analysis.inferences]
        .sort((left, right) => left.startedAt.localeCompare(right.startedAt))
        .slice(-24)
        .map((inference) => ({
          id: `restored:${inference.inferenceId}`,
          runId: inference.runId,
          strategyId: inference.strategyId,
          stage: 'artifact' as const,
          status: inference.status === 'failed' ? 'failed' as const : inference.status === 'running' ? 'running' as const : 'succeeded' as const,
          title: `${inferenceStrategyLabel(inference.strategyId)} · ${inferenceStatusLabel(inference.status)}`,
          detail: inference.summary ?? inference.error ?? '历史推理工件已恢复。',
          timestamp: new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date(inference.startedAt)),
        })))
      const restoredFeedback: Partial<Record<StrategyId, string>> = {}
      strategies.forEach((strategy) => {
        if (restoredResults[strategy.id]) restoredFeedback[strategy.id] = '已装载历史推理结果；不会重新调用模型。'
        else if (restoredContexts[strategy.id]) restoredFeedback[strategy.id] = '已装载历史策略上下文，可基于当前记录重新推理。'
      })
      setStrategyFeedback(restoredFeedback)
      const profile = historicalTargetPlatformProfile(analysis)
      if (profile) onTargetPlatformProfileChange(profile)
      const resultCount = Object.keys(restoredResults).length
      const inputDetail = isManualCodeRecord
        ? `已装载手动粘贴代码${restoredCode ? '' : '（热点代码不可读取）'}`
        : `已装载 ${catalog.groups.length} 个模块热点${restoredCode ? '、历史热点代码' : ''}`
      const detail = `${inputDetail}与 ${resultCount} 条历史策略结果；不会重新请求模型。${unavailable.length > 0 ? ` ${unavailable.length} 项工件未恢复。` : ''}`
      setNotice({ tone: unavailable.length > 0 ? 'info' : 'success', text: detail })
      onWorkflowEvent({
        type: 'analysis_record_restored',
        traceLabel: analysis.trace.label,
        hotspotLabel: stringValue(record(restoredCode?.hotspot).event_name, analysis.hotspot?.eventName ?? ''),
        hasCode: Boolean(restoredCode),
        restoredResultCount: resultCount,
        restoredRoute: Boolean(restoredRoute),
        restoredWorkflow: workspace?.snapshot.workflow,
        detail,
      })
    } catch (error) {
      if (!isCurrentRequest()) return
      const message = error instanceof Error ? error.message : '无法读取历史记录工件'
      setNotice({ tone: 'error', text: `未能装载此条历史记录：${message}` })
      onWorkflowEvent({ type: 'analysis_record_restore_failed', detail: message })
    } finally {
      if (isCurrentRequest()) {
        activeRestoreRequestRef.current = null
        setBusy(null)
      }
    }
  }, [onTargetPlatformProfileChange, onWorkflowEvent, readHotspotCatalog, resetDownstream, sourceConnectorId])

  useEffect(() => {
    if (!restoreRequest || !sourceConnectorId || handledRestoreRequestRef.current === restoreRequest.requestId) return
    if (restoreRequest.analysis.trace.sourceConnectorId && restoreRequest.analysis.trace.sourceConnectorId !== sourceConnectorId) return
    handledRestoreRequestRef.current = restoreRequest.requestId
    activeRestoreRequestRef.current = restoreRequest.requestId
    void restoreHistoricalRecord(restoreRequest.analysis, restoreRequest.requestId, restoreRequest.workspace)
  }, [restoreHistoricalRecord, restoreRequest, sourceConnectorId])

  const resolveHotspot = useCallback(async (eventId: string, actionRequestId = '') => {
    if (!trace) {
      rejectReasoningLabAction(actionRequestId, '请先选择一份 trace。')
      return
    }
    setBusy(`resolve:${eventId}`)
    try {
      const selectedHotspot = hotspots.find((item) => stringValue(item.event_id, '') === eventId)
      const resolution = await api<{ artifact: Artifact }>('/v1/projects/local-default/reasoning-trace-hotspots/resolve-source', {
        method: 'POST', body: JSON.stringify({ analysis_artifact_id: trace.artifact_id, event_id: eventId, module_key: eventModuleKey(selectedHotspot ?? {}) || null }),
      })
      setHotspotSourceResolution(resolution.artifact)
      let code: { artifact: Artifact }
      try {
        code = await api<{ artifact: Artifact }>('/v1/projects/local-default/hotspot-code/from-source-resolution', {
          method: 'POST', body: JSON.stringify({ source_resolution_artifact_id: resolution.artifact.artifact_id }),
        })
      } catch (codeError) {
        const resolutionDetail = record(resolution.artifact.resolution)
        const message = codeError instanceof Error ? codeError.message : '当前代码仓没有可验证的桩点函数。'
        setCodeUnit(null)
        setNotice({ tone: 'info', text: `${message} 已保留事件解析工件，可让页面助手继续检查外部运行时或其他源码范围。` })
        respondReasoningLabAction(actionRequestId, {
          status: 'source_anchor_unresolved',
          event_id: eventId,
          event_name: stringValue(record(resolution.artifact.event).event_name, stringValue(selectedHotspot?.event_name, eventId)),
          source_resolution_artifact_id: resolution.artifact.artifact_id,
          resolution_state: stringValue(resolutionDetail.state, 'unresolved'),
          reason: stringValue(resolutionDetail.reason, message),
          source_candidates: Array.isArray(resolution.artifact.source_candidates) ? resolution.artifact.source_candidates.slice(0, 6) : [],
        })
        return
      }
      setCodeUnit(code.artifact)
      setContexts({})
      setResults({})
      setComparison(null)
      setStrategyFeedback({})
      setStrategyTokenStats({})
      setHardwareSemanticRoute(null)
      setHardwareRoutingSkipped(false)
      setManualOpportunityResult(null)
      setNotice({ tone: 'success', text: '已定位真实事件的桩点包围函数。它是调用路径锚点，不等同于已证明的 CPU 热点；可继续深挖局部代码或 callee。' })
      const resolvedHotspot = record(code.artifact.hotspot)
      if (analysisRecordId) onTraceAnalysisHotspotUpdated(analysisRecordId, {
        eventId,
        eventName: stringValue(resolvedHotspot.event_name, stringValue(selectedHotspot?.event_name, eventId)),
        sourceKind: 'trace_source',
        codeArtifactId: code.artifact.artifact_id,
      })
      onWorkflowEvent({
        type: 'hotspot_code_ready',
        sourceKind: 'trace_source',
        hotspotLabel: stringValue(resolvedHotspot.event_name, stringValue(selectedHotspot?.event_name, eventId)),
      })
      respondReasoningLabAction(actionRequestId, {
        status: 'trace_anchor_ready',
        event_id: eventId,
        event_name: stringValue(resolvedHotspot.event_name, stringValue(selectedHotspot?.event_name, eventId)),
        source_resolution_artifact_id: resolution.artifact.artifact_id,
        code_artifact_id: code.artifact.artifact_id,
        hotspot_attribution_status: stringValue(record(code.artifact.hotspot_attribution).status, ''),
      })
    } catch (error) {
      const message = error instanceof Error ? error.message : '热点源码解析失败'
      setNotice({ tone: 'error', text: message })
      rejectReasoningLabAction(actionRequestId, message)
    } finally {
      setBusy(null)
    }
  }, [analysisRecordId, hotspots, onTraceAnalysisHotspotUpdated, onWorkflowEvent, trace])

  const requestHotspotInvestigation = useCallback(() => {
    const source = record(codeUnit?.source)
    const resolutionId = hotspotSourceResolution?.artifact_id
      || stringValue(source.source_resolution_artifact_id, '')
      || stringValue(source.trace_anchor_source_resolution_artifact_id, '')
    if (!resolutionId) {
      setNotice({ tone: 'info', text: '当前代码没有 trace 桩点解析工件；请先从热点事件定位源码。' })
      return
    }
    const event = record(hotspotSourceResolution?.event)
    const eventLabel = stringValue(event.event_name, stringValue(record(codeUnit?.hotspot).event_name, '当前 trace 事件'))
    const anchorPath = stringValue(source.path, stringValue(record(record(hotspotSourceResolution?.code_block).source).path, '源码未定位'))
    const anchorRange = source.line_start ? `${String(source.line_start)}-${String(source.line_end)}` : '范围未解析'
    dispatchAgentCommand({
      analysisId: analysisRecordId ?? undefined,
      displayMessage: `深挖 ${eventLabel} 对应的实际热点代码`,
      message: [
        '先调用平台工具 skills_read(skill_id="trace-hotspot-code-investigation") 读取当前热加载 Skill，并按返回的完整正文深挖当前 trace 桩点背后的真实热点代码。',
        `source_resolution_artifact_id=${resolutionId}`,
        `trace_event=${eventLabel}`,
        `anchor=${anchorPath}:${anchorRange}`,
        `调用 platform_read_artifact(artifact_type="trace_event_source_resolution", artifact_id="${resolutionId}") 读取桩点解析工件；工具名是单数 platform_read_artifact。若 Agent Adapter 未直接展示该工具，通过 platform_call_tool 转交同名工具和参数。`,
        '使用后端原生代码检索、Clang/编译上下文或调用关系工具，自适应检查桩点局部区域、调用方、直接与必要的传递 callee，以及外部运行时边界；不要固定调用深度，也不要把 inclusive span 当作函数 CPU self time。',
        '找到源码可观察的最小候选后，调用 ui_execute_registered_action(action_id="reasoning.select_hotspot_source_candidate", input={...})，回填精确 source_resolution_artifact_id、path、line_start、line_end、function_hint、candidate_kind、relation_to_anchor、rationale 和可复核的 call_path。若源码不在当前代码仓或只能定位到外部 runtime/model boundary，请明确停止在 evidence_required，不要把桩点 wrapper 送入硬件路由。',
      ].join('\n'),
    })
    setNotice({ tone: 'info', text: '正在深挖调用路径；结论会区分源码事实与无采样条件下的热点假设。' })
  }, [analysisRecordId, codeUnit, hotspotSourceResolution])

  const selectInvestigatedHotspot = useCallback(async (detail: JsonRecord, requestId: string) => {
    setBusy('select-investigated-hotspot')
    try {
      const response = await api<{ artifact: Artifact }>('/v1/projects/local-default/hotspot-code/from-investigated-source', {
        method: 'POST',
        body: JSON.stringify({
          source_resolution_artifact_id: stringValue(detail.source_resolution_artifact_id, ''),
          path: stringValue(detail.path, ''),
          line_start: numberValue(detail.line_start),
          line_end: numberValue(detail.line_end),
          function_hint: stringValue(detail.function_hint, ''),
          candidate_kind: stringValue(detail.candidate_kind, ''),
          relation_to_anchor: stringValue(detail.relation_to_anchor, ''),
          rationale: stringValue(detail.rationale, ''),
          expected_source_sha256: stringValue(detail.expected_source_sha256, '') || null,
          call_path: Array.isArray(detail.call_path) ? detail.call_path : [],
        }),
      })
      setCodeUnit(response.artifact)
      setContexts({})
      setResults({})
      setComparison(null)
      setStrategyFeedback({})
      setStrategyTokenStats({})
      setHardwareSemanticRoute(null)
      setHardwareRoutingSkipped(false)
      setManualOpportunityResult(null)
      const selectedHotspot = record(response.artifact.hotspot)
      const label = stringValue(response.artifact.function_hint, stringValue(selectedHotspot.event_name, '热点候选'))
      setNotice({ tone: 'success', text: '已回填调用路径深挖得到的源码候选；源码范围已验证，热点归因仍明确标记为无 CPU 采样的模型假设。' })
      if (analysisRecordId) onTraceAnalysisHotspotUpdated(analysisRecordId, {
        eventId: stringValue(selectedHotspot.event_id, ''),
        eventName: stringValue(selectedHotspot.event_name, label),
        sourceKind: 'trace_source',
        codeArtifactId: response.artifact.artifact_id,
      })
      onWorkflowEvent({ type: 'hotspot_code_ready', sourceKind: 'trace_source', hotspotLabel: label })
      respondReasoningLabAction(requestId, {
        status: 'succeeded',
        artifact_id: response.artifact.artifact_id,
        analysis_id: analysisRecordId ?? '',
        attribution_status: stringValue(record(response.artifact.hotspot_attribution).status, ''),
      })
    } catch (error) {
      const message = error instanceof Error ? error.message : '深挖热点候选未能回填。'
      setNotice({ tone: 'error', text: message })
      rejectReasoningLabAction(requestId, message)
    } finally {
      setBusy(null)
    }
  }, [analysisRecordId, onTraceAnalysisHotspotUpdated, onWorkflowEvent])

  const importTrace = useCallback(async (event: FormEvent<HTMLInputElement>) => {
    const file = event.currentTarget.files?.[0]
    if (!file) return
    const name = file.name.toLowerCase()
    if (!name.endsWith('.htrace') && !name.endsWith('.json')) {
      setNotice({ tone: 'error', text: '这里只接受原始 .htrace 或 trace 团队 JSON 分析报告。' })
      event.currentTarget.value = ''
      return
    }
    setBusy('upload')
    try {
      const formData = new FormData()
      formData.append('file', file)
      formData.append('source_connector_id', sourceConnectorId)
      const response = await upload<{ artifact: Artifact }>('/v1/projects/local-default/trace-imports/upload', formData)
      const imported = response.artifact as TraceSummary
      setTraces((previous) => [imported, ...previous.filter((item) => item.artifact_id !== imported.artifact_id)])
      selectTrace(imported)
      setNotice({ tone: 'success', text: `${file.name} 已完成导入与分析。现在可以提取真实热点。` })
    } catch (error) {
      setNotice({ tone: 'error', text: error instanceof Error ? error.message : 'trace 导入失败' })
    } finally {
      setBusy(null)
      event.currentTarget.value = ''
    }
  }, [selectTrace, sourceConnectorId])

  const adoptPastedCodeArtifact = useCallback((artifact: Artifact, hints: { functionHint?: string; eventName?: string; analysisId?: string } = {}) => {
    const artifactHotspot = record(artifact.hotspot)
    const label = hints.eventName?.trim()
      || hints.functionHint?.trim()
      || stringValue(artifactHotspot.event_name, '')
      || stringValue(artifactHotspot.function_hint, '')
      || '用户粘贴代码'
    setCodeUnit(artifact)
    setHotspotSourceResolution(null)
    setContexts({})
    setResults({})
    setComparison(null)
    setStrategyFeedback({})
    setStrategyTokenStats({})
    setHardwareSemanticRoute(null)
    setHardwareRoutingSkipped(false)
    setManualOpportunityResult(null)
    // Pasted code can be analyzed without a trace. Give that standalone run an
    // ordinary analysis-history entry so its contexts, transcript, and result
    // remain visible and reloadable instead of becoming an "unlinked" artifact.
    const recordId = hints.analysisId || (trace && analysisRecordId
      ? analysisRecordId
      : onTraceAnalysisStarted({
        artifactId: artifact.artifact_id,
        label: `手动代码：${label}`,
        inputKind: 'manual_code',
        sourceConnectorId,
      }))
    setAnalysisRecordId(recordId)
    setNotice({ tone: 'info', text: '已保存粘贴代码，并创建可恢复的优化分析记录。该输入未绑定到 Trace 源码，结论仍需补充运行时证据。' })
    onTraceAnalysisHotspotUpdated(recordId, {
      eventName: label,
      sourceKind: 'pasted_code',
      codeArtifactId: artifact.artifact_id,
    })
    onWorkflowEvent({ type: 'hotspot_code_ready', sourceKind: 'pasted_code', hotspotLabel: label })
  }, [analysisRecordId, onTraceAnalysisHotspotUpdated, onTraceAnalysisStarted, onWorkflowEvent, sourceConnectorId, trace])

  const savePastedCode = useCallback(async (input: { code: string; functionHint?: string; eventName?: string }): Promise<Artifact> => {
    const response = await api<{ artifact: Artifact }>('/v1/projects/local-default/hotspot-code/paste', {
      method: 'POST',
      body: JSON.stringify({
        code: input.code,
        function_hint: input.functionHint ?? '',
        event_name: input.eventName ?? '',
        source_connector_id: sourceConnectorId,
      }),
    })
    adoptPastedCodeArtifact(response.artifact, input)
    return response.artifact
  }, [adoptPastedCodeArtifact, sourceConnectorId])

  const submitPastedCode = useCallback(async (event: FormEvent) => {
    event.preventDefault()
    if (pasteSubmissionRef.current) return
    pasteSubmissionRef.current = true
    setBusy('paste')
    try {
      await savePastedCode({ code: pastedCode, functionHint, eventName })
    } catch (error) {
      setNotice({ tone: 'error', text: error instanceof Error ? error.message : '粘贴代码无效' })
    } finally {
      pasteSubmissionRef.current = false
      setBusy((current) => current === 'paste' ? null : current)
    }
  }, [eventName, functionHint, pastedCode, savePastedCode])

  const prepare = useCallback(async (strategyId: StrategyId, runId = randomUuid()): Promise<Artifact | null> => {
    if (!codeUnit) {
      setNotice({ tone: 'error', text: '请先从真实热点解析精确函数，或粘贴目标代码。' })
      return null
    }
    const routeArtifact = hardwareSemanticRoute?.codeArtifactId === codeUnit.artifact_id ? hardwareSemanticRoute.resultArtifact : null
    if (!routeArtifact && !hardwareRoutingSkipped) {
      setNotice({ tone: 'info', text: '热点代码已就绪。请先在第 3 步完成模型语义硬件路由，或明确跳过路由后再选择策略。独立盲推基线位于正式流程和真机验证之后。' })
      return null
    }
    if (strategyId === 'hardware_kb' && !routeArtifact) {
      setNotice({ tone: 'info', text: '硬件知识库策略需要一条已完成的模型语义路由，或在路由结果中人工选择硬件目录。' })
      return null
    }
    const eventId = appendReasoningEvent({
      runId,
      strategyId,
      stage: 'context',
      status: 'running',
      title: '正在准备策略上下文',
      detail: '读取热点代码、trace 证据和该策略允许的知识记录。',
    })
    setBusy(`prepare:${strategyId}`)
    try {
      const routeArtifactId = strategyId === 'hardware_kb' ? routeArtifact?.artifact_id : undefined
      if (strategyId === 'hardware_kb') {
        updateReasoningEvent(eventId, {
          title: '复用已确认的硬件目录',
          detail: `已保存路由工件 ${routeArtifactId?.slice(0, 16) ?? '—'}，现在仅在该目录中检索 API 简介。`,
        })
      }
      const response = await api<{ artifact: Artifact }>('/v1/projects/local-default/reasoning-contexts/prepare', {
        method: 'POST', body: JSON.stringify({
          code_artifact_id: codeUnit.artifact_id,
          strategy_id: strategyId,
          skill_profile_id: skillProfile?.profile_id,
          target_platform_profile: targetPlatformProfile,
          ...(routeArtifactId ? { hardware_semantic_routing_result_artifact_id: routeArtifactId } : {}),
        }),
      })
      setContexts((previous) => ({ ...previous, [strategyId]: response.artifact }))
      const records = Array.isArray(response.artifact.context_records) ? response.artifact.context_records.length : 0
      updateReasoningEvent(eventId, {
        status: 'succeeded',
        title: '策略上下文已准备',
        detail: `${records} 条上下文记录，context ${stringValue(response.artifact.context_sha256, '').slice(0, 12)}。`,
      })
      return response.artifact
    } catch (error) {
      const message = error instanceof Error ? error.message : '上下文准备失败'
      updateReasoningEvent(eventId, { status: 'failed', title: '策略上下文准备失败', detail: message })
      setNotice({ tone: 'error', text: message })
      return null
    } finally {
      setBusy(null)
    }
  }, [appendReasoningEvent, codeUnit, hardwareRoutingSkipped, hardwareSemanticRoute, skillProfile?.profile_id, targetPlatformProfile, updateReasoningEvent])

  const persistTranscript = useCallback(async (input: {
    analysisId: string
    inferenceId: string
    runId: string
    strategyId: AnalysisInferenceStrategyId
    context: Artifact
    model: { provider: string; name: string }
    systemPrompt: string
    userPrompt: string
    rawOutput: string
    tokenUsage?: ModelTokenUsage
    tokenBudget: PromptTokenBudget
    resultArtifactId?: string
    outcome: JsonRecord
    timeline: TranscriptTimelineEntry[]
  }): Promise<string | undefined> => {
    try {
      const response = await api<{ artifact: Artifact }>('/v1/projects/local-default/reasoning-transcripts', {
        method: 'POST',
        body: JSON.stringify({
          analysis_id: input.analysisId,
          inference_id: input.inferenceId,
          run_id: input.runId,
          strategy_id: input.strategyId,
          strategy_label: inferenceStrategyLabel(input.strategyId),
          context_artifact_id: input.context.artifact_id,
          result_artifact_id: input.resultArtifactId,
          model_provider: input.model.provider,
          model_name: input.model.name,
          system_prompt: input.systemPrompt,
          user_prompt: input.userPrompt,
          raw_output: input.rawOutput,
          model_usage: input.tokenUsage,
          token_budget: input.tokenBudget,
          outcome: input.outcome,
          timeline: input.timeline,
        }),
      })
      return response.artifact.artifact_id
    } catch (error) {
      appendReasoningEvent({
        runId: input.runId,
        strategyId: input.strategyId,
        stage: 'artifact',
        status: 'warning',
        title: '完整推理记录未能存档',
        detail: error instanceof Error ? error.message : '无法保存推理 transcript。当前页面结果不受影响。',
      })
      return undefined
    }
  }, [appendReasoningEvent])

  const requestAgentInference = useCallback((input: {
    requestId: string
    analysisId: string
    runId: string
    strategyId: string
    strategyLabel: string
    operation: string
    systemPrompt: string
    userPrompt: string
    tokenBudget: PromptTokenBudget
    progress: AgentStrategyProgress
  }): Promise<ModelInferenceResponse> => new Promise((resolve, reject) => {
    const waiter: AgentResponseWaiter = { resolve, reject, timeoutId: 0, tokenBudget: input.tokenBudget }
    agentResponseWaiters.current.set(input.requestId, waiter)
    armAgentResponseTimeout(input.requestId, waiter)
    window.dispatchEvent(new CustomEvent('reasoning-agent-analysis-request', {
      detail: {
        request_id: input.requestId,
        analysis_id: input.analysisId,
        run_id: input.runId,
        strategy_id: input.strategyId,
        strategy_label: input.strategyLabel,
        operation: input.operation,
        system_prompt: input.systemPrompt,
        user_prompt: input.userPrompt,
        token_budget: input.tokenBudget,
        analysis_progress: input.progress,
      },
    }))
  }), [armAgentResponseTimeout])

  const runBlindAnalysis = useCallback(async (actionRequestId?: string, question?: string) => {
    if (!codeUnit) {
      const message = '请先关联或粘贴热点代码，再运行独立盲推理。'
      setNotice({ tone: 'info', text: message })
      rejectReasoningLabAction(actionRequestId, message)
      return
    }
    const runId = randomUuid()
    const modelRequestId = randomUuid()
    const inferenceId = randomUuid()
    const startedAt = new Date().toISOString()
    const model = { provider: 'kimi_server_agent_engine', name: modelConfig.model || 'engine-default' }
    let transcriptContext: Artifact | null = null
    let transcriptSystemPrompt = ''
    let transcriptUserPrompt = ''
    let transcriptRawOutput = ''
    let transcriptTokenUsage: ModelTokenUsage | undefined
    let transcriptTokenBudget: PromptTokenBudget | null = null
    setBusy('blind-analysis')
    setBlindFeedback('正在准备只包含代码、用户问题和目标平台画像的独立上下文。')
    try {
      const prepared = await api<{ artifact: Artifact }>('/v1/projects/local-default/blind-reasoning/prepare', {
        method: 'POST',
        body: JSON.stringify({
          analysis_id: analysisRecordId ?? `unlinked-${codeUnit.artifact_id}`,
          code_artifact_id: codeUnit.artifact_id,
          user_question: question?.trim() || '请判断这段热点代码如何通过异构硬件、CPU 并行、SIMD 或算法方式优化，并尽量给出可验证的硬件 API 方向。',
          skill_profile_id: skillProfile?.profile_id,
          target_platform_profile: targetPlatformProfile,
        }),
      })
      setBlindContext(prepared.artifact)
      transcriptContext = prepared.artifact
      if (analysisRecordId) onInferenceRecorded(analysisRecordId, {
        inferenceId,
        runId,
        strategyId: 'blind_baseline',
        strategyLabel: '独立盲推理',
        status: 'running',
        startedAt,
        model,
        context: snapshotAnalysisContext(prepared.artifact),
      })
      const prompt = record(prepared.artifact.prompt)
      const systemPrompt = stringValue(prompt.system, '')
      const userPrompt = stringValue(prompt.user, '')
      transcriptSystemPrompt = systemPrompt
      transcriptUserPrompt = userPrompt
      if (!systemPrompt || !userPrompt) throw new Error('盲推理上下文没有生成完整提示词。')
      const messages = [{ role: 'system' as const, content: systemPrompt }, { role: 'user' as const, content: userPrompt }]
      const tokenBudget = createPromptTokenBudget({
        contextWindowTokens: modelConfig.contextWindowTokens,
        inputTokens: estimateMessageTokens(messages),
        outputReserveTokens: modelConfig.agentMaxOutputTokens,
      })
      transcriptTokenBudget = tokenBudget
      setBlindTokenStats({ budget: tokenBudget })
      const progress = createIndependentReasoningProgress({ requestId: modelRequestId, strategyId: 'blind_baseline', label: '独立盲推理', startedAt, tokenBudget })
      notifyAgentStrategyProgress(progress)
      const response = await requestAgentInference({
        requestId: modelRequestId,
        analysisId: stringValue(prepared.artifact.blind_session_analysis_id, ''),
        runId,
        strategyId: 'blind_baseline',
        strategyLabel: '独立盲推理',
        operation: 'blind_opportunity_analysis',
        systemPrompt,
        userPrompt,
        tokenBudget,
        progress,
      })
      transcriptRawOutput = response.rawOutput
      transcriptTokenUsage = response.tokenUsage
      transcriptTokenBudget = response.tokenBudget
      setBlindTokenStats({ budget: response.tokenBudget, usage: response.tokenUsage })
      const structured = await parseOrRepairModelJson({
        config: modelConfig,
        rawOutput: response.rawOutput,
        reasoningOutput: response.reasoningOutput,
        schemaName: 'blind_opportunity_reasoning/v1',
        contractHint: '必须包含 summary、confidence、workload_characterization、candidates、preferred_path 和 unknowns。',
        maxTokens: response.tokenBudget.outputReserveTokens,
        requireChineseSummary: true,
        observability: {
          analysisId: stringValue(prepared.artifact.blind_session_analysis_id, ''),
          operation: 'blind_opportunity_analysis.structure',
          runId,
        },
      })
      const saved = await api<{ artifact: Artifact; validation_errors: string[] }>('/v1/projects/local-default/blind-reasoning/results', {
        method: 'POST',
        body: JSON.stringify({
          context_artifact_id: prepared.artifact.artifact_id,
          model_provider: 'kimi_server_agent_engine',
          model_name: modelConfig.model || 'engine-default',
          raw_output: structured.auditOutput,
          result: structured.parsed,
        }),
      })
      setBlindResult(saved.artifact)
      setBlindComparison(null)
      const output = record(saved.artifact.result)
      const summary = stringValue(output.summary, '独立盲推理已保存。')
      const feedback = saved.validation_errors.length > 0
        ? `盲推理已保存，存在 ${saved.validation_errors.length} 项结构问题；它不会阻断正式流程。`
        : '盲推理已保存。该结果不会进入正式路由或知识库推理上下文。'
      setBlindFeedback(feedback)
      const transcriptArtifactId = await persistTranscript({
        analysisId: analysisRecordId ?? `unlinked-${runId}`,
        inferenceId,
        runId,
        strategyId: 'blind_baseline',
        context: prepared.artifact,
        model,
        systemPrompt,
        userPrompt,
        rawOutput: response.rawOutput,
        tokenUsage: response.tokenUsage,
        tokenBudget: response.tokenBudget,
        resultArtifactId: saved.artifact.artifact_id,
        outcome: {
          status: 'succeeded',
          summary,
          validation_errors: saved.validation_errors,
          claim_boundary_zh: stringValue(output.claim_boundary_zh, ''),
        },
        timeline: [
          { stage: 'context', status: 'succeeded', title: '独立上下文已准备', detail: `仅包含代码、用户问题和目标画像；context ${stringValue(prepared.artifact.context_sha256, '').slice(0, 12)}。`, timestamp: startedAt },
          { stage: 'request', status: 'succeeded', title: '盲推基线模型请求已返回', detail: `Analysis ${stringValue(prepared.artifact.blind_session_analysis_id, '').slice(0, 18)}。`, timestamp: new Date().toISOString() },
          { stage: 'contract', status: saved.validation_errors.length > 0 ? 'warning' : 'succeeded', title: '盲推理结果契约已校验', detail: feedback, timestamp: new Date().toISOString() },
          { stage: 'artifact', status: 'succeeded', title: '盲推理结果已保存', detail: saved.artifact.artifact_id, timestamp: new Date().toISOString() },
        ],
      })
      if (analysisRecordId) onInferenceUpdated(analysisRecordId, inferenceId, {
        status: 'succeeded',
        completedAt: new Date().toISOString(),
        resultArtifactId: saved.artifact.artifact_id,
        transcriptArtifactId,
        summary,
        tokenUsage: response.tokenUsage,
        tokenBudget: response.tokenBudget,
      })
      notifyAgentStrategyProgress(createIndependentReasoningProgress({ requestId: modelRequestId, strategyId: 'blind_baseline', label: '独立盲推理', startedAt, tokenBudget: response.tokenBudget, phase: 'completed', detail: feedback }))
      notifyAgentStrategyCompletion({ request_id: modelRequestId, strategy_id: 'blind_baseline', strategy_label: '独立盲推理', status: 'succeeded', conclusion: `独立盲推理已完成。\n\n**结论**\n${summary}` })
      respondReasoningLabAction(actionRequestId, {
        status: 'succeeded',
        result_artifact_id: saved.artifact.artifact_id,
        blind_session_analysis_id: stringValue(saved.artifact.blind_session_analysis_id, ''),
        summary,
        preferred_path: stringValue(output.preferred_path, ''),
        candidate_count: Array.isArray(output.candidates) ? output.candidates.length : 0,
        validation_errors: saved.validation_errors,
      })
    } catch (error) {
      const message = error instanceof Error ? error.message : '独立盲推理失败。'
      setBlindFeedback(`${message} 正式流程仍可继续。`)
      setNotice({ tone: 'error', text: message })
      const transcriptArtifactId = transcriptContext && transcriptTokenBudget
        ? await persistTranscript({
          analysisId: analysisRecordId ?? `unlinked-${runId}`,
          inferenceId,
          runId,
          strategyId: 'blind_baseline',
          context: transcriptContext,
          model,
          systemPrompt: transcriptSystemPrompt,
          userPrompt: transcriptUserPrompt,
          rawOutput: transcriptRawOutput,
          tokenUsage: transcriptTokenUsage,
          tokenBudget: transcriptTokenBudget,
          outcome: { status: 'failed', error: message },
          timeline: [
            { stage: 'context', status: 'succeeded', title: '独立上下文已准备', detail: `context ${stringValue(transcriptContext.context_sha256, '').slice(0, 12)}。`, timestamp: startedAt },
            { stage: 'artifact', status: 'failed', title: '盲推理未形成可保存结果', detail: message, timestamp: new Date().toISOString() },
          ],
        })
        : undefined
      if (analysisRecordId) onInferenceUpdated(analysisRecordId, inferenceId, { status: 'failed', completedAt: new Date().toISOString(), error: message, transcriptArtifactId, tokenUsage: transcriptTokenUsage, tokenBudget: transcriptTokenBudget ?? undefined })
      const fallbackBudget = blindTokenStats?.budget ?? createPromptTokenBudget({ contextWindowTokens: modelConfig.contextWindowTokens, inputTokens: 0, outputReserveTokens: modelConfig.agentMaxOutputTokens })
      notifyAgentStrategyProgress(createIndependentReasoningProgress({ requestId: modelRequestId, strategyId: 'blind_baseline', label: '独立盲推理', startedAt, tokenBudget: fallbackBudget, phase: 'failed', detail: message }))
      notifyAgentStrategyCompletion({ request_id: modelRequestId, strategy_id: 'blind_baseline', strategy_label: '独立盲推理', status: 'failed', error: `${message} 正式流程未被阻断。` })
      rejectReasoningLabAction(actionRequestId, message)
    } finally {
      setBusy(null)
    }
  }, [analysisRecordId, blindTokenStats?.budget, codeUnit, modelConfig, onInferenceRecorded, onInferenceUpdated, persistTranscript, requestAgentInference, skillProfile?.profile_id, targetPlatformProfile])

  const runBlindComparison = useCallback(async (actionRequestId?: string) => {
    const flowResult = strategies
      .map((strategy) => results[strategy.id])
      .find((artifact) => artifact && stringValue(record(artifact.manual_selection).origin, '') !== 'user_manual')
    if (!blindResult || !flowResult) {
      const message = '需要先完成独立盲推理和至少一次正式流程推理，才能进行对照。'
      setNotice({ tone: 'info', text: message })
      rejectReasoningLabAction(actionRequestId, message)
      return
    }
    const runId = randomUuid()
    const modelRequestId = randomUuid()
    const inferenceId = randomUuid()
    const startedAt = new Date().toISOString()
    const model = { provider: 'kimi_server_agent_engine', name: modelConfig.model || 'engine-default' }
    let transcriptContext: Artifact | null = null
    let transcriptSystemPrompt = ''
    let transcriptUserPrompt = ''
    let transcriptRawOutput = ''
    let transcriptTokenUsage: ModelTokenUsage | undefined
    let transcriptTokenBudget: PromptTokenBudget | null = null
    setBusy('blind-comparison')
    setBlindFeedback('正在对照冻结的盲推结果与正式流程结果。')
    try {
      const deviceSnapshot = readDeviceValidationSnapshot(analysisRecordId)
      const deviceRunArtifactId = deviceSnapshot?.facts.experimentExecution?.remoteShellRunArtifactId
      const prepared = await api<{ artifact: Artifact }>('/v1/projects/local-default/blind-reasoning/comparisons/prepare', {
        method: 'POST',
        body: JSON.stringify({
          blind_result_artifact_id: blindResult.artifact_id,
          flow_result_artifact_id: flowResult.artifact_id,
          ...(deviceRunArtifactId ? { device_run_artifact_id: deviceRunArtifactId } : {}),
          skill_profile_id: skillProfile?.profile_id,
        }),
      })
      transcriptContext = prepared.artifact
      const prompt = record(prepared.artifact.prompt)
      if (analysisRecordId) onInferenceRecorded(analysisRecordId, {
        inferenceId,
        runId,
        strategyId: 'blind_comparison',
        strategyLabel: '盲推理与流程对照',
        status: 'running',
        startedAt,
        model,
        context: snapshotAnalysisContext(prepared.artifact),
      })
      const systemPrompt = stringValue(prompt.system, '')
      const userPrompt = stringValue(prompt.user, '')
      transcriptSystemPrompt = systemPrompt
      transcriptUserPrompt = userPrompt
      if (!systemPrompt || !userPrompt) throw new Error('对照上下文没有生成完整提示词。')
      const messages = [{ role: 'system' as const, content: systemPrompt }, { role: 'user' as const, content: userPrompt }]
      const tokenBudget = createPromptTokenBudget({
        contextWindowTokens: modelConfig.contextWindowTokens,
        inputTokens: estimateMessageTokens(messages),
        outputReserveTokens: modelConfig.agentMaxOutputTokens,
      })
      transcriptTokenBudget = tokenBudget
      setBlindTokenStats({ budget: tokenBudget })
      const progress = createIndependentReasoningProgress({ requestId: modelRequestId, strategyId: 'blind_comparison', label: '盲推理与流程对照', startedAt, tokenBudget })
      notifyAgentStrategyProgress(progress)
      const response = await requestAgentInference({
        requestId: modelRequestId,
        analysisId: stringValue(prepared.artifact.blind_session_analysis_id, ''),
        runId,
        strategyId: 'blind_comparison',
        strategyLabel: '盲推理与流程对照',
        operation: 'blind_flow_comparison',
        systemPrompt,
        userPrompt,
        tokenBudget,
        progress,
      })
      transcriptRawOutput = response.rawOutput
      transcriptTokenUsage = response.tokenUsage
      transcriptTokenBudget = response.tokenBudget
      setBlindTokenStats({ budget: response.tokenBudget, usage: response.tokenUsage })
      const structured = await parseOrRepairModelJson({
        config: modelConfig,
        rawOutput: response.rawOutput,
        reasoningOutput: response.reasoningOutput,
        schemaName: 'blind_flow_reasoning_comparison/v1',
        contractHint: '必须包含 verdict、summary、agreements、disagreements、blind_strengths、blind_weaknesses、flow_strengths、flow_weaknesses、unsupported_claims 和 merged_recommendation。',
        maxTokens: response.tokenBudget.outputReserveTokens,
        requireChineseSummary: true,
        observability: {
          analysisId: stringValue(prepared.artifact.blind_session_analysis_id, ''),
          operation: 'blind_flow_comparison.structure',
          runId,
        },
      })
      const saved = await api<{ artifact: Artifact; validation_errors: string[] }>('/v1/projects/local-default/blind-reasoning/comparisons/results', {
        method: 'POST',
        body: JSON.stringify({
          context_artifact_id: prepared.artifact.artifact_id,
          model_provider: 'kimi_server_agent_engine',
          model_name: modelConfig.model || 'engine-default',
          raw_output: structured.auditOutput,
          result: structured.parsed,
        }),
      })
      setBlindComparison(saved.artifact)
      const output = record(saved.artifact.result)
      const summary = stringValue(output.summary, '对照结果已保存。')
      const feedback = '对照已完成。它用于判断流程资产的增益或束缚，不作为准确率真值。'
      setBlindFeedback(feedback)
      const transcriptArtifactId = await persistTranscript({
        analysisId: analysisRecordId ?? `unlinked-${runId}`,
        inferenceId,
        runId,
        strategyId: 'blind_comparison',
        context: prepared.artifact,
        model,
        systemPrompt,
        userPrompt,
        rawOutput: response.rawOutput,
        tokenUsage: response.tokenUsage,
        tokenBudget: response.tokenBudget,
        resultArtifactId: saved.artifact.artifact_id,
        outcome: {
          status: 'succeeded',
          summary,
          verdict: stringValue(output.verdict, 'inconclusive'),
          validation_errors: saved.validation_errors,
          evaluation_boundary_zh: stringValue(output.evaluation_boundary_zh, ''),
        },
        timeline: [
          { stage: 'context', status: 'succeeded', title: '对照上下文已准备', detail: `冻结盲结果与同代码流程结果；context ${stringValue(prepared.artifact.context_sha256, '').slice(0, 12)}。`, timestamp: startedAt },
          { stage: 'request', status: 'succeeded', title: '盲推结果对照已返回', detail: `Analysis ${stringValue(prepared.artifact.blind_session_analysis_id, '').slice(0, 18)}。`, timestamp: new Date().toISOString() },
          { stage: 'contract', status: saved.validation_errors.length > 0 ? 'warning' : 'succeeded', title: '对照结果契约已校验', detail: feedback, timestamp: new Date().toISOString() },
          { stage: 'artifact', status: 'succeeded', title: '对照结果已保存', detail: saved.artifact.artifact_id, timestamp: new Date().toISOString() },
        ],
      })
      if (analysisRecordId) onInferenceUpdated(analysisRecordId, inferenceId, {
        status: 'succeeded',
        completedAt: new Date().toISOString(),
        resultArtifactId: saved.artifact.artifact_id,
        transcriptArtifactId,
        summary,
        tokenUsage: response.tokenUsage,
        tokenBudget: response.tokenBudget,
      })
      notifyAgentStrategyProgress(createIndependentReasoningProgress({ requestId: modelRequestId, strategyId: 'blind_comparison', label: '盲推理与流程对照', startedAt, tokenBudget: response.tokenBudget, phase: 'completed', detail: feedback }))
      notifyAgentStrategyCompletion({ request_id: modelRequestId, strategy_id: 'blind_comparison', strategy_label: '盲推理与流程对照', status: 'succeeded', conclusion: `盲推理与流程推理对照已完成。\n\n**结论**\n${summary}` })
      respondReasoningLabAction(actionRequestId, {
        status: 'succeeded',
        comparison_artifact_id: saved.artifact.artifact_id,
        verdict: stringValue(output.verdict, 'inconclusive'),
        summary,
        merged_recommendation: stringValue(output.merged_recommendation, ''),
        validation_errors: saved.validation_errors,
      })
    } catch (error) {
      const message = error instanceof Error ? error.message : '盲推理与流程对照失败。'
      setBlindFeedback(message)
      setNotice({ tone: 'error', text: message })
      const transcriptArtifactId = transcriptContext && transcriptTokenBudget
        ? await persistTranscript({
          analysisId: analysisRecordId ?? `unlinked-${runId}`,
          inferenceId,
          runId,
          strategyId: 'blind_comparison',
          context: transcriptContext,
          model,
          systemPrompt: transcriptSystemPrompt,
          userPrompt: transcriptUserPrompt,
          rawOutput: transcriptRawOutput,
          tokenUsage: transcriptTokenUsage,
          tokenBudget: transcriptTokenBudget,
          outcome: { status: 'failed', error: message },
          timeline: [
            { stage: 'context', status: 'succeeded', title: '对照上下文已准备', detail: `context ${stringValue(transcriptContext.context_sha256, '').slice(0, 12)}。`, timestamp: startedAt },
            { stage: 'artifact', status: 'failed', title: '对照未形成可保存结果', detail: message, timestamp: new Date().toISOString() },
          ],
        })
        : undefined
      if (analysisRecordId) onInferenceUpdated(analysisRecordId, inferenceId, { status: 'failed', completedAt: new Date().toISOString(), error: message, transcriptArtifactId, tokenUsage: transcriptTokenUsage, tokenBudget: transcriptTokenBudget ?? undefined })
      const fallbackBudget = blindTokenStats?.budget ?? createPromptTokenBudget({ contextWindowTokens: modelConfig.contextWindowTokens, inputTokens: 0, outputReserveTokens: modelConfig.agentMaxOutputTokens })
      notifyAgentStrategyProgress(createIndependentReasoningProgress({ requestId: modelRequestId, strategyId: 'blind_comparison', label: '盲推理与流程对照', startedAt, tokenBudget: fallbackBudget, phase: 'failed', detail: message }))
      notifyAgentStrategyCompletion({ request_id: modelRequestId, strategy_id: 'blind_comparison', strategy_label: '盲推理与流程对照', status: 'failed', error: message })
      rejectReasoningLabAction(actionRequestId, message)
    } finally {
      setBusy(null)
    }
  }, [analysisRecordId, blindResult, blindTokenStats?.budget, modelConfig, onInferenceRecorded, onInferenceUpdated, persistTranscript, requestAgentInference, results, skillProfile?.profile_id])

  const runStrategy = useCallback(async (strategyId: StrategyId, actionRequestId?: string, rerunReason = '') => {
    const runId = randomUuid()
    const routeArtifact = codeUnit && hardwareSemanticRoute?.codeArtifactId === codeUnit.artifact_id
      ? hardwareSemanticRoute.resultArtifact
      : null
    if (!routeArtifact && !hardwareRoutingSkipped) {
      const message = '请先完成第 3 步模型语义硬件路由，或明确跳过路由后再选择策略。'
      setNotice({ tone: 'info', text: message })
      rejectReasoningLabAction(actionRequestId, message)
      return
    }
    if (strategyId === 'hardware_kb' && !routeArtifact) {
      const message = '硬件知识库策略需要一条已完成的模型语义路由，或在路由结果中人工选择硬件目录。'
      setNotice({ tone: 'info', text: message })
      rejectReasoningLabAction(actionRequestId, message)
      return
    }
    const existingContext = contexts[strategyId]
    const reusableContext = existingContext && hasCurrentModelInputContext(existingContext) ? existingContext : null
    const startedAt = new Date().toISOString()
    const timeline: TranscriptTimelineEntry[] = []
    if (existingContext && !reusableContext) {
      const detail = '已检测到旧版策略上下文；历史工件仍可审计，本次推理会自动重新准备当前版本上下文。'
      timeline.push({ stage: 'context', status: 'warning', title: '历史策略上下文版本已过期', detail, timestamp: startedAt })
      appendReasoningEvent({ runId, strategyId, stage: 'context', status: 'warning', title: '历史策略上下文版本已过期', detail })
    }
    if (reusableContext) {
      timeline.push({
        stage: 'context',
        status: 'succeeded',
        title: '复用已准备的策略上下文',
        detail: `context ${stringValue(reusableContext.context_sha256, '').slice(0, 12)}。`,
        timestamp: startedAt,
      })
      appendReasoningEvent({
        runId,
        strategyId,
        stage: 'context',
        status: 'succeeded',
        title: '复用已准备的策略上下文',
        detail: `context ${stringValue(reusableContext.context_sha256, '').slice(0, 12)}。`,
      })
    }
    const context = reusableContext ?? await prepare(strategyId, runId)
    if (!context) {
      rejectReasoningLabAction(actionRequestId, '策略上下文未能准备完成。')
      return
    }
    const config = modelConfig
    const inferenceId = randomUuid()
    const model = { provider: 'kimi_server_agent_engine', name: config.model || 'Kimi Server 默认模型' }
    const promptPlan = buildStrategyPromptPlan(
      context,
      strategyId,
      1024 * 1024,
      64 * 1024,
      {
        fullAnalysis: applySkillPrompt(fullOptimizationAnalysisSkill, skillProfile, 'full_analysis'),
        strategy: [
          applySkillPrompt(strategySkillPrompts[strategyId], skillProfile, strategySkillSlots[strategyId]),
          rerunReason.trim()
            ? `# 本轮复核原因\n${rerunReason.trim().slice(0, 800)}\n这是上一阶段提出的待核查原因；请根据当前代码和受控证据重新判断，不要机械继承旧结论。`
            : '',
        ].filter(Boolean).join('\n\n'),
      },
    )
    const { systemPrompt, userPrompt, tokenBudget } = promptPlan
    setStrategyTokenStats((previous) => ({ ...previous, [strategyId]: { budget: tokenBudget } }))
    if (!reusableContext) {
      timeline.push({
        stage: 'context',
        status: 'succeeded',
        title: '策略上下文已准备',
        detail: `context ${stringValue(context.context_sha256, '').slice(0, 12)}，已写入目标设备与系统画像。`,
        timestamp: new Date().toISOString(),
      })
    }
    if (analysisRecordId) {
      onInferenceRecorded(analysisRecordId, {
        inferenceId,
        runId,
        strategyId,
        strategyLabel: strategyTitle(strategyId),
        status: 'running',
        startedAt,
        model,
        context: snapshotAnalysisContext(context),
        tokenBudget,
      })
    }
    onWorkflowEvent({ type: 'strategy_selected', strategyId })
    setBusy(`run:${strategyId}`)
    setStrategyFeedback((previous) => ({ ...previous, [strategyId]: '页面助手正在携带该策略的受控上下文执行推理…' }))
    let rawOutput = ''
    let reasoningOutput = ''
    let modelUsage: ModelTokenUsage | undefined
    let actualTokenBudget = tokenBudget
    let requestTimelineIndex = -1
    let agentRequestId = ''
    let agentResponseReceived = false
    strategyAbortRef.current?.abort()
    const strategyAbortController = new AbortController()
    strategyAbortRef.current = strategyAbortController
    try {
      if (promptPlan.compactionDetail) {
        timeline.push({
          stage: 'context',
          status: 'warning',
          title: '策略上下文已按传输范围精简',
          detail: `${promptPlan.compactionDetail} 当前估算输入 ${formatTokenCount(tokenBudget.inputTokens)}。`,
          timestamp: new Date().toISOString(),
        })
        appendReasoningEvent({
          runId,
          strategyId,
          stage: 'context',
          status: 'warning',
          title: '策略上下文已按传输范围精简',
          detail: `${promptPlan.compactionDetail} 当前估算输入 ${formatTokenCount(tokenBudget.inputTokens)}。`,
        })
      }
      requestTimelineIndex = timeline.length
      timeline.push({
        stage: 'request',
        status: 'running',
        title: '正在调用模型',
        detail: `${config.model} 经本机中继执行结构化机会推理；估算输入 ${formatTokenCount(tokenBudget.inputTokens)}。`,
        timestamp: new Date().toISOString(),
      })
      const requestEventId = appendReasoningEvent({
        runId,
        strategyId,
        stage: 'request',
        status: 'running',
        title: '正在调用模型',
        detail: `${config.model} 经本机中继执行结构化机会推理；估算输入 ${formatTokenCount(tokenBudget.inputTokens)}。`,
        payload: `${systemPrompt}\n\n${userPrompt}`,
        payloadLabel: '查看发送给模型的技能、约束与语义上下文',
      })
      agentRequestId = randomUuid()
      const initialAgentProgress = createAgentStrategyProgress({
        context,
        strategyId,
        requestId: agentRequestId,
        tokenBudget,
        phase: 'model_pending',
        startedAt,
      })
      notifyAgentStrategyProgress(initialAgentProgress)
      const response = await requestAgentInference({
        requestId: agentRequestId,
        analysisId: analysisRecordId ?? `unlinked-${runId}`,
        runId,
        strategyId,
        strategyLabel: strategyTitle(strategyId),
        operation: `opportunity_strategy.${strategyId}`,
        systemPrompt,
        userPrompt,
        tokenBudget,
        progress: initialAgentProgress,
      })
      rawOutput = response.rawOutput
      reasoningOutput = response.reasoningOutput ?? ''
      modelUsage = response.tokenUsage
      actualTokenBudget = response.tokenBudget
      setStrategyTokenStats((previous) => ({ ...previous, [strategyId]: { budget: actualTokenBudget, usage: modelUsage } }))
      agentResponseReceived = true
      if (!rawOutput.trim() && !reasoningOutput.trim()) throw new Error('页面助手没有收到模型文本响应')
      timeline[requestTimelineIndex] = {
        ...timeline[requestTimelineIndex],
        status: 'succeeded',
        title: '模型调用完成',
        detail: `收到最终内容 ${rawOutput.length.toLocaleString()} 字、推理通道 ${reasoningOutput.length.toLocaleString()} 字；Input ${formatTokenCount(modelUsage.inputTokens)}，Output ${formatTokenCount(modelUsage.outputTokens)}${modelUsage.inputSource === 'estimated' || modelUsage.outputSource === 'estimated' ? '（含估算）' : ''}。`,
      }
      updateReasoningEvent(requestEventId, { status: 'succeeded', title: '模型调用完成', detail: `收到最终内容 ${rawOutput.length.toLocaleString()} 字、推理通道 ${reasoningOutput.length.toLocaleString()} 字；Input ${formatTokenCount(modelUsage.inputTokens)}，Output ${formatTokenCount(modelUsage.outputTokens)}${modelUsage.inputSource === 'estimated' || modelUsage.outputSource === 'estimated' ? '（含估算）' : ''}。` })
      timeline.push({ stage: 'response', status: 'succeeded', title: '已收到模型原始回答', detail: '原文已保留，尚未被视为已验证结论。', timestamp: new Date().toISOString() })
      appendReasoningEvent({
        runId,
        strategyId,
        stage: 'response',
        status: 'succeeded',
        title: '已收到模型原始回答',
        detail: '原文已保留，可展开查看；它尚未被视为已验证结论。',
        payload: rawOutput,
        payloadLabel: '查看模型原始回答',
      })
      notifyAgentStrategyProgress(createAgentStrategyProgress({
        context,
        strategyId,
        requestId: agentRequestId,
        tokenBudget: actualTokenBudget,
        phase: 'response_received',
        startedAt,
      }))
      let parsed: JsonRecord
      try {
        const structured = await parseOrRepairModelJson({
          config,
          rawOutput,
          reasoningOutput,
          schemaName: 'opportunity_reasoning/v1',
          contractHint: '必须包含 schema_version、recommendation、confidence、evidence_status、summary、opportunities、missing_evidence 和 negative_evidence。每个 opportunity 最多包含 opportunity_kind、candidate_target、analysis_scope、rationale、required_conditions、risks、evidence_ids、verification_plan、implementation_sketch。',
          maxTokens: actualTokenBudget.outputReserveTokens,
          signal: strategyAbortController.signal,
          requireChineseSummary: true,
          observability: {
            analysisId: analysisRecordId ?? '',
            operation: `opportunity_strategy.${strategyId}`,
            runId,
          },
        })
        parsed = structured.parsed
        if (structured.recovered) {
          modelUsage = mergeModelTokenUsage(modelUsage, structured.repairUsage)
          actualTokenBudget = createPromptTokenBudget({
            contextWindowTokens: actualTokenBudget.contextWindowTokens,
            inputTokens: modelUsage.inputTokens,
            outputReserveTokens: actualTokenBudget.outputReserveTokens,
            compactionApplied: actualTokenBudget.compactionApplied,
            compactedItems: actualTokenBudget.compactedItems,
          })
          setStrategyTokenStats((previous) => ({ ...previous, [strategyId]: { budget: actualTokenBudget, usage: modelUsage } }))
          window.dispatchEvent(new CustomEvent('reasoning-agent-extra-token-usage', { detail: { token_usage: structured.repairUsage, token_budget: actualTokenBudget } }))
          const recoveryDetail = structured.recoverySource === 'reasoning_channel'
            ? '上游模型只在思考流表达了结论，未写出最终 JSON；平台已基于同次响应完成一次不新增事实的结构恢复，现继续进行后端契约与证据校验。'
            : '上游模型未直接返回 JSON；平台已发起一次不新增事实的结构修复，现继续进行后端契约与证据校验。'
          timeline.push({ stage: 'contract', status: 'warning', title: '已完成 JSON 格式修复', detail: recoveryDetail, timestamp: new Date().toISOString() })
          appendReasoningEvent({ runId, strategyId, stage: 'contract', status: 'warning', title: '已完成 JSON 格式修复', detail: recoveryDetail })
          rawOutput = structured.auditOutput
        }
      } catch (error) {
        const reachedOutputLimit = Boolean(
          modelUsage && modelUsage.outputTokens >= actualTokenBudget.outputReserveTokens,
        )
        const message = error instanceof ModelOutputIncompleteError
          ? reachedOutputLimit
            ? `模型输出触及本轮 ${formatTokenCount(actualTokenBudget.outputReserveTokens)} Token 上限，JSON 在结束前被截断。原始回答已保留；请重新执行该策略。`
            : '模型返回的 JSON 在根对象结束前中断。原始回答已保留；请重新执行该策略。'
          : `模型输出不是可解析的 JSON，且一次结构修复未成功：${error instanceof Error ? error.message : '未知格式错误'}`
        timeline.push({ stage: 'contract', status: 'failed', title: '模型输出无法进入契约校验', detail: message, timestamp: new Date().toISOString() })
        appendReasoningEvent({ runId, strategyId, stage: 'contract', status: 'failed', title: '模型输出无法进入契约校验', detail: message })
        throw new Error(message)
      }
      notifyAgentStrategyProgress(createAgentStrategyProgress({
        context,
        strategyId,
        requestId: agentRequestId,
        tokenBudget: actualTokenBudget,
        phase: 'contract_validating',
        startedAt,
      }))
      const saved = await api<{ artifact: Artifact; validation_errors: string[]; format_adaptation?: JsonRecord | null }>('/v1/projects/local-default/reasoning-results', {
        method: 'POST',
        body: JSON.stringify({ context_artifact_id: context.artifact_id, model_provider: 'kimi_server_agent_engine', model_name: config.model || 'engine-default', raw_output: rawOutput, result: parsed }),
      })
      setResults((previous) => ({ ...previous, [strategyId]: saved.artifact }))
      setComparison(null)
      const output = resultSummary(saved.artifact)
      const adaptation = record(output.format_adaptation)
      const validation = record(output.validation)
      const calibration = record(output.evidence_calibration)
      const validationErrors = Array.isArray(validation.errors) ? validation.errors.map((item) => stringValue(item, '')).filter(Boolean) : saved.validation_errors
      const adaptationActions = Array.isArray(adaptation.actions) ? adaptation.actions.map((item) => stringValue(item, '')).filter(Boolean) : []
      const calibrationActions = Array.isArray(calibration.actions) ? calibration.actions.map((item) => stringValue(item, '')).filter(Boolean) : []
      const contractDetail = validationErrors.length > 0
        ? validationErrors.join('；')
        : calibrationActions.length > 0
          ? calibrationActions.join('；')
        : adaptationActions.length > 0
          ? adaptationActions.join('；')
          : '证据 ID、枚举和机会数量均符合本次上下文约束。'
      timeline.push({
        stage: 'contract',
        status: validationErrors.length > 0 || calibrationActions.length > 0 || adaptationActions.length > 0 ? 'warning' : 'succeeded',
        title: validationErrors.length > 0 ? '契约校验完成：存在需保守处理的问题' : calibrationActions.length > 0 ? '已按证据门禁校准模型结论' : adaptationActions.length > 0 ? '已适配模型的等价字段结构' : '模型输出通过契约校验',
        detail: contractDetail,
        timestamp: new Date().toISOString(),
      })
      appendReasoningEvent({
        runId,
        strategyId,
        stage: 'contract',
        status: validationErrors.length > 0 || calibrationActions.length > 0 || adaptationActions.length > 0 ? 'warning' : 'succeeded',
        title: validationErrors.length > 0 ? '契约校验完成：存在需保守处理的问题' : calibrationActions.length > 0 ? '已按证据门禁校准模型结论' : adaptationActions.length > 0 ? '已适配模型的等价字段结构' : '模型输出通过契约校验',
        detail: contractDetail,
      })
      timeline.push({ stage: 'artifact', status: 'succeeded', title: '推理结果已保存', detail: `${saved.artifact.artifact_id}。结果仍需设备与基准测试证据闭环。`, timestamp: new Date().toISOString() })
      appendReasoningEvent({
        runId,
        strategyId,
        stage: 'artifact',
        status: 'succeeded',
        title: '推理结果已保存',
        detail: `${saved.artifact.artifact_id}。结果仍需设备与基准测试证据闭环。`,
      })
      const feedback = validationErrors.length > 0
        ? `已保存；${validationErrors.length} 项契约问题已在下方展示。`
        : calibrationActions.length > 0
          ? '推理完成；模型结论已按证据门禁降为待验证，原始回答可在下方查看。'
        : adaptationActions.length > 0
          ? '推理完成；模型字段已按保守规则适配，原始回答可在下方查看。'
          : '推理完成。'
      const transcriptArtifactId = await persistTranscript({
        analysisId: analysisRecordId ?? `unlinked-${runId}`,
        inferenceId,
        runId,
        strategyId,
        context,
        model,
        systemPrompt,
        userPrompt,
        rawOutput,
        tokenUsage: modelUsage,
        tokenBudget: actualTokenBudget,
        resultArtifactId: saved.artifact.artifact_id,
        outcome: {
          status: 'succeeded',
          summary: validationErrors.length > 0 ? `结果已保存，${validationErrors.length} 项契约问题需要核查。` : stringValue(output.summary, '推理结果已保存。'),
          validation_errors: validationErrors,
          format_adaptation: adaptationActions,
          evidence_calibration: calibrationActions,
          token_usage: modelUsage,
          token_budget: actualTokenBudget,
        },
        timeline,
      })
      if (analysisRecordId) onInferenceUpdated(analysisRecordId, inferenceId, {
        status: 'succeeded',
        completedAt: new Date().toISOString(),
        resultArtifactId: saved.artifact.artifact_id,
        summary: validationErrors.length > 0
          ? `结果已保存，${validationErrors.length} 项契约问题需要核查。`
          : stringValue(output.summary, '推理结果已保存。'),
        transcriptArtifactId,
        tokenUsage: modelUsage,
        tokenBudget: actualTokenBudget,
      })
      setStrategyFeedback((previous) => ({ ...previous, [strategyId]: feedback }))
      setNotice(saved.validation_errors.length > 0
        ? { tone: 'info', text: `模型结果已保存，但存在 ${saved.validation_errors.length} 项契约校验问题。` }
        : calibrationActions.length > 0
          ? { tone: 'info', text: '模型结果已保存；由于缺少上下文支持，结论已按证据门禁降为待验证。' }
        : { tone: 'success', text: `${strategies.find((item) => item.id === strategyId)?.title} 已完成。` })
      onWorkflowEvent({
        type: 'strategy_completed',
        strategyId,
        detail: validationErrors.length > 0
          ? `结果已保存，但有 ${validationErrors.length} 项契约校验问题，需要在页面中核查。`
          : calibrationActions.length > 0
            ? '结果已保存；模型提出的方向缺少上下文支持，已按证据门禁降为待验证。'
          : '结果已保存。请查看机会、证据缺口和验证计划。',
      })
      notifyAgentStrategyProgress(createAgentStrategyProgress({
        context,
        strategyId,
        requestId: agentRequestId,
        tokenBudget: actualTokenBudget,
        phase: 'completed',
        startedAt,
        status: validationErrors.length > 0 || calibrationActions.length > 0 || adaptationActions.length > 0 ? 'warning' : 'succeeded',
        detail: validationErrors.length > 0
          ? `结果已保存，但有 ${validationErrors.length} 项契约问题需要核查。`
          : calibrationActions.length > 0
            ? '结果已回填；模型方向已按证据门禁降为待验证。'
            : adaptationActions.length > 0
              ? '结果已回填；模型字段已按保守规则适配。'
              : '结果已回填，证据与结构化契约校验通过。',
      }))
      notifyAgentStrategyCompletion({
        request_id: agentRequestId,
        strategy_id: strategyId,
        strategy_label: strategyTitle(strategyId),
        status: 'succeeded',
        conclusion: assistantConclusionMessage({
          strategyLabel: strategyTitle(strategyId),
          output,
          validationErrors,
          adaptationActions,
          calibrationActions,
        }),
      })
      respondReasoningLabAction(actionRequestId, {
        status: 'succeeded',
        strategy_id: strategyId,
        strategy_label: strategyTitle(strategyId),
        context_artifact_id: context.artifact_id,
        result_artifact_id: saved.artifact.artifact_id,
        recommendation: stringValue(output.recommendation, ''),
        confidence: stringValue(output.confidence, ''),
        evidence_status: stringValue(output.evidence_status, ''),
        summary: stringValue(output.summary, ''),
        opportunities: (Array.isArray(output.opportunities) ? output.opportunities : []).map(record).slice(0, 4),
        missing_evidence: Array.isArray(output.missing_evidence) ? output.missing_evidence.slice(0, 12) : [],
        validation_errors: validationErrors,
      })
    } catch (error) {
      const stopped = strategyAbortController.signal.aborted || (error instanceof DOMException && error.name === 'AbortError')
      const baseMessage = stopped ? '策略推理已由用户停止。' : error instanceof Error ? error.message : '模型推理失败'
      const message = agentResponseReceived
        ? baseMessage
        : `${baseMessage}。请检查 Kimi Agent Engine 与后端服务状态。`
      if (requestTimelineIndex >= 0 && timeline[requestTimelineIndex]?.status === 'running') {
        timeline[requestTimelineIndex] = {
          ...timeline[requestTimelineIndex],
          status: 'failed',
          title: '模型调用失败',
          detail: message,
        }
      }
      timeline.push({ stage: 'artifact', status: 'failed', title: '推理未形成可保存结果', detail: message, timestamp: new Date().toISOString() })
      appendReasoningEvent({ runId, strategyId, stage: 'artifact', status: 'failed', title: '推理未形成可保存结果', detail: message })
      const transcriptArtifactId = await persistTranscript({
        analysisId: analysisRecordId ?? `unlinked-${runId}`,
        inferenceId,
        runId,
        strategyId,
        context,
        model,
        systemPrompt,
        userPrompt,
        rawOutput,
        tokenUsage: modelUsage,
        tokenBudget: actualTokenBudget,
        outcome: { status: 'failed', error: message, token_usage: modelUsage, token_budget: actualTokenBudget },
        timeline,
      })
      if (analysisRecordId) onInferenceUpdated(analysisRecordId, inferenceId, { status: 'failed', completedAt: new Date().toISOString(), error: message, transcriptArtifactId, tokenUsage: modelUsage, tokenBudget: actualTokenBudget })
      setStrategyFeedback((previous) => ({ ...previous, [strategyId]: message }))
      setNotice({ tone: 'error', text: message })
      onWorkflowEvent({ type: 'strategy_failed', strategyId, detail: message })
      if (agentRequestId) {
        notifyAgentStrategyProgress(createAgentStrategyProgress({
          context,
          strategyId,
          requestId: agentRequestId,
          tokenBudget: actualTokenBudget,
          phase: 'failed',
          startedAt,
          detail: message,
        }))
      }
      if (agentResponseReceived && agentRequestId) {
        notifyAgentStrategyCompletion({
          request_id: agentRequestId,
          strategy_id: strategyId,
          strategy_label: strategyTitle(strategyId),
          status: 'failed',
          error: message,
        })
      }
      rejectReasoningLabAction(actionRequestId, message)
    } finally {
      if (strategyAbortRef.current === strategyAbortController) strategyAbortRef.current = null
      setBusy(null)
    }
  }, [analysisRecordId, appendReasoningEvent, codeUnit, contexts, hardwareRoutingSkipped, hardwareSemanticRoute, modelConfig, onInferenceRecorded, onInferenceUpdated, onWorkflowEvent, persistTranscript, prepare, requestAgentInference, skillProfile, updateReasoningEvent])

  const acceptAgentStrategyResult = useCallback(async (
    strategyId: StrategyId,
    actionRequestId: string | undefined,
    contextArtifactId: string,
    agentResult: JsonRecord,
  ) => {
    if (!codeUnit) {
      rejectReasoningLabAction(actionRequestId, '当前没有可回填的热点代码。')
      return
    }
    if (!contextArtifactId || Object.keys(agentResult).length === 0) {
      rejectReasoningLabAction(actionRequestId, '需要先调用 MCP 工具 reasoning_prepare_context，再提交对应上下文的 agent_result。')
      return
    }
    const inferenceId = randomUuid()
    const runId = randomUuid()
    const startedAt = new Date().toISOString()
    setBusy(`run:${strategyId}`)
    setStrategyFeedback((previous) => ({ ...previous, [strategyId]: '正在进行契约校验与页面回填…' }))
    try {
      const loaded = await api<{ artifact: Artifact }>(`/v1/projects/local-default/reasoning-contexts/${encodeURIComponent(contextArtifactId)}`)
      const context = loaded.artifact
      if (stringValue(context.strategy_id) !== strategyId) throw new Error('策略上下文与当前策略不一致，请重新准备。')
      const inputArtifacts = Array.isArray(context.input_artifacts) ? context.input_artifacts.map((item) => stringValue(item, '')) : []
      if (inputArtifacts.length > 0 && !inputArtifacts.includes(`artifact:${codeUnit.artifact_id}`)) {
        throw new Error('策略上下文与当前热点代码不一致，请重新准备。')
      }
      setContexts((previous) => ({ ...previous, [strategyId]: context }))
      if (analysisRecordId) {
        onInferenceRecorded(analysisRecordId, {
          inferenceId,
          runId,
          strategyId,
          strategyLabel: strategyTitle(strategyId),
          status: 'running',
          startedAt,
          model: { provider: 'kimi_agent_session', name: modelConfig.model || 'engine-default' },
          context: snapshotAnalysisContext(context),
        })
      }
      const rawOutput = JSON.stringify(agentResult)
      const saved = await api<{ artifact: Artifact; validation_errors: string[] }>('/v1/projects/local-default/reasoning-results', {
        method: 'POST',
        body: JSON.stringify({
          context_artifact_id: contextArtifactId,
          model_provider: 'kimi_agent_session',
          model_name: modelConfig.model || 'engine-default',
          raw_output: rawOutput,
          result: agentResult,
        }),
      })
      setResults((previous) => ({ ...previous, [strategyId]: saved.artifact }))
      setComparison(null)
      const output = resultSummary(saved.artifact)
      const validation = record(output.validation)
      const validationErrors = Array.isArray(validation.errors)
        ? validation.errors.map((item) => stringValue(item, '')).filter(Boolean)
        : saved.validation_errors
      const summary = stringValue(output.summary, '已形成结构化结论。')
      if (analysisRecordId) onInferenceUpdated(analysisRecordId, inferenceId, {
        status: 'succeeded',
        completedAt: new Date().toISOString(),
        resultArtifactId: saved.artifact.artifact_id,
        summary,
      })
      setStrategyFeedback((previous) => ({ ...previous, [strategyId]: validationErrors.length > 0
        ? `Agent 结果已回填，${validationErrors.length} 项契约问题已标记。`
        : 'Agent 结果已回填，结构与证据引用校验完成。' }))
      setNotice(validationErrors.length > 0
        ? { tone: 'info', text: `Agent 结果已保存，存在 ${validationErrors.length} 项契约问题。` }
        : { tone: 'success', text: `${strategyTitle(strategyId)} 已完成。` })
      onWorkflowEvent({ type: 'strategy_completed', strategyId, detail: '结果已保存并回填。' })
      respondReasoningLabAction(actionRequestId, {
        status: 'succeeded',
        strategy_id: strategyId,
        strategy_label: strategyTitle(strategyId),
        context_artifact_id: contextArtifactId,
        result_artifact_id: saved.artifact.artifact_id,
        recommendation: stringValue(output.recommendation, ''),
        confidence: stringValue(output.confidence, ''),
        evidence_status: stringValue(output.evidence_status, ''),
        summary,
        opportunities: (Array.isArray(output.opportunities) ? output.opportunities : []).map(record).slice(0, 4),
        missing_evidence: Array.isArray(output.missing_evidence) ? output.missing_evidence.slice(0, 12) : [],
        validation_errors: validationErrors,
      })
      window.requestAnimationFrame(() => deviceStepRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }))
    } catch (acceptError) {
      const message = acceptError instanceof Error ? acceptError.message : 'Agent 策略结果未能回填。'
      if (analysisRecordId) onInferenceUpdated(analysisRecordId, inferenceId, { status: 'failed', completedAt: new Date().toISOString(), error: message })
      setStrategyFeedback((previous) => ({ ...previous, [strategyId]: message }))
      setNotice({ tone: 'error', text: message })
      onWorkflowEvent({ type: 'strategy_failed', strategyId, detail: message })
      rejectReasoningLabAction(actionRequestId, message)
    } finally {
      setBusy(null)
    }
  }, [analysisRecordId, codeUnit, modelConfig.model, onInferenceRecorded, onInferenceUpdated, onWorkflowEvent])

  useEffect(() => { void refreshTraces() }, [refreshTraces])

  useEffect(() => {
    const listener = () => strategyAbortRef.current?.abort(new DOMException('策略推理已由用户停止。', 'AbortError'))
    window.addEventListener('page-agent-stop-current-inference', listener)
    return () => {
      window.removeEventListener('page-agent-stop-current-inference', listener)
      listener()
    }
  }, [])

  useEffect(() => {
    const sync = (event?: Event) => {
      const detail = event instanceof CustomEvent ? event.detail : undefined
      const next = detail && typeof detail === 'object' ? detail as BrowserModelConfig : readBrowserModelConfig()
      setModelConfig({ ...next, contextWindowTokens: normalizeContextWindowTokens(next.contextWindowTokens) })
    }
    sync()
    window.addEventListener(modelConfigChangedEvent, sync)
    return () => window.removeEventListener(modelConfigChangedEvent, sync)
  }, [])

  useEffect(() => {
    // A context pack is hashed with the selected target profile.  Reusing one
    // prepared for another SoC/OS selection would make the audit trail false.
    setContexts({})
    setResults({})
    setComparison(null)
    setStrategyFeedback({})
    setStrategyTokenStats({})
    setBlindContext(null)
    setBlindResult(null)
    setBlindComparison(null)
    setBlindFeedback('')
    setBlindTokenStats(null)
  }, [targetPlatformProfile.chipset.model_id, targetPlatformProfile.operating_system.version_id])

  useEffect(() => {
    if (codeUnit && blindResult && stringValue(blindResult.code_artifact_id, '') === codeUnit.artifact_id) return
    setBlindContext(null)
    setBlindResult(null)
    setBlindComparison(null)
    setBlindFeedback('')
    setBlindTokenStats(null)
  }, [blindResult, codeUnit])

  useEffect(() => {
    if (!skillProfile?.profile_id) return
    setContexts({})
    setResults({})
    setComparison(null)
    setHardwareSemanticRoute(null)
    setManualOpportunityResult(null)
    setBlindContext(null)
    setBlindResult(null)
    setBlindComparison(null)
    setBlindFeedback('')
    setBlindTokenStats(null)
  }, [skillProfile?.profile_id])

  useEffect(() => {
    const waiters = agentResponseWaiters.current
    const listener = (event: Event) => {
      const detail = record((event as CustomEvent<unknown>).detail)
      const requestId = stringValue(detail.request_id, '')
      const waiter = requestId ? waiters.get(requestId) : undefined
      if (!waiter) return
      window.clearTimeout(waiter.timeoutId)
      waiters.delete(requestId)
      const error = stringValue(detail.error, '')
      if (error) {
        waiter.reject(new Error(error))
        return
      }
      const tokenBudget = tokenBudgetFromAgentResponse(detail.token_budget, waiter.tokenBudget)
      waiter.resolve({
        rawOutput: stringValue(detail.raw_output, ''),
        reasoningOutput: stringValue(detail.reasoning_output, ''),
        tokenUsage: tokenUsageFromAgentResponse(detail.token_usage, tokenBudget),
        tokenBudget,
      })
    }
    const heartbeatListener = (event: Event) => {
      const detail = record((event as CustomEvent<unknown>).detail)
      const requestId = stringValue(detail.request_id, '')
      const waiter = requestId ? waiters.get(requestId) : undefined
      if (waiter) armAgentResponseTimeout(requestId, waiter)
    }
    window.addEventListener('reasoning-lab-agent-response', listener)
    window.addEventListener('reasoning-lab-agent-heartbeat', heartbeatListener)
    return () => {
      window.removeEventListener('reasoning-lab-agent-response', listener)
      window.removeEventListener('reasoning-lab-agent-heartbeat', heartbeatListener)
      for (const waiter of waiters.values()) {
        window.clearTimeout(waiter.timeoutId)
        waiter.reject(new Error('推理实验室已卸载，模型响应未被接收'))
      }
      waiters.clear()
    }
  }, [armAgentResponseTimeout])

  useEffect(() => {
    const listener = (event: Event) => {
      const detail = record((event as CustomEvent<unknown>).detail)
      const action = stringValue(detail.action, '')
      const requestId = stringValue(detail.request_id, '')
      if (action === 'refresh_traces') void refreshTraces()
      if (action === 'load_hotspots') void loadHotspots()
      if (action === 'resolve_hotspot') void resolveHotspot(stringValue(detail.event_id, ''), requestId)
      if (action === 'select_hotspot_source_candidate') void selectInvestigatedHotspot(detail, requestId)
      if (action === 'prepare_context') {
        void (async () => {
          const strategyId = stringValue(detail.strategy_id, 'kb_rag') as StrategyId
          const prepared = await prepare(strategyId)
          if (!prepared) {
            rejectReasoningLabAction(requestId, '策略上下文未能准备完成。')
            return
          }
          respondReasoningLabAction(requestId, {
            status: 'succeeded',
            strategy_id: strategyId,
            context_artifact_id: prepared.artifact_id,
            required_schema: 'opportunity_reasoning/v1',
            output_contract: agentStrategyOutputContract,
            model_input_context: reasoningModelInputContext(prepared),
          })
        })()
      }
      if (action === 'run_strategy') {
        const strategyId = stringValue(detail.strategy_id, 'kb_rag') as StrategyId
        const contextArtifactId = stringValue(detail.context_artifact_id, '')
        const agentResult = record(detail.agent_result)
        if (contextArtifactId && Object.keys(agentResult).length > 0) {
          void acceptAgentStrategyResult(strategyId, requestId || undefined, contextArtifactId, agentResult)
        } else {
          void runStrategy(strategyId, requestId || undefined, stringValue(detail.reason, ''))
        }
      }
      if (action === 'run_blind_analysis') void runBlindAnalysis(
        requestId || undefined,
        stringValue(detail.question, ''),
      )
      if (action === 'compare_blind_flow') void runBlindComparison(requestId || undefined)
      if (action === 'prepare_hardware_semantic_route') window.dispatchEvent(new CustomEvent('reasoning-lab-prepare-hardware-semantic-routing', {
        detail: { request_id: requestId },
      }))
      if (action === 'run_hardware_semantic_route') window.dispatchEvent(new CustomEvent('reasoning-lab-run-hardware-semantic-routing', {
        detail: {
          request_id: requestId,
          context_artifact_id: stringValue(detail.context_artifact_id, ''),
          agent_result: record(detail.agent_result),
          reason: stringValue(detail.reason, ''),
        },
      }))
      if (action === 'device_discover') window.dispatchEvent(new CustomEvent('device-verification-action', { detail: { action: 'discover', request_id: requestId } }))
      if (action === 'device_probe') window.dispatchEvent(new CustomEvent('device-verification-action', { detail: { action: 'probe', request_id: requestId, device_serial: stringValue(detail.device_serial, '') } }))
      if (action === 'device_stage_experiment_source') window.dispatchEvent(new CustomEvent('device-verification-action', { detail: { action: 'stage_experiment_source', request_id: requestId, profile_id: stringValue(detail.profile_id, ''), language: stringValue(detail.language, ''), title: stringValue(detail.title, ''), source: stringValue(detail.source, ''), shader_source: stringValue(detail.shader_source, '') } }))
      if (action === 'device_build_upload_experiment') window.dispatchEvent(new CustomEvent('device-verification-action', { detail: { action: 'build_upload_experiment', request_id: requestId, staged_source_artifact_id: stringValue(detail.staged_source_artifact_id, ''), timeout_seconds: numberValue(detail.timeout_seconds) ?? 240 } }))
      if (action === 'device_execute_shell') window.dispatchEvent(new CustomEvent('device-verification-action', { detail: { action: 'remote_shell', request_id: requestId, command: stringValue(detail.command, ''), timeout_seconds: numberValue(detail.timeout_seconds) ?? 240 } }))
      if (action === 'assistant_import_trace') {
        const imported = record(detail.artifact) as TraceSummary
        if (!imported.artifact_id) return
        setTraces((previous) => [imported, ...previous.filter((item) => item.artifact_id !== imported.artifact_id)])
        const recordId = selectTrace(imported)
        void loadHotspotsForTrace(imported, recordId)
      }
      if (action === 'assistant_paste_code') {
        const artifact = record(detail.artifact) as Artifact
        if (!artifact.artifact_id) {
          rejectReasoningLabAction(requestId, '热点代码工件无效。')
          return
        }
        const hints = record(detail.hints)
        setPastedCode(stringValue(artifact.code, ''))
        setFunctionHint(stringValue(hints.function_hint, stringValue(record(artifact.hotspot).function_hint, '')))
        setEventName(stringValue(hints.event_name, stringValue(record(artifact.hotspot).event_name, '')))
        adoptPastedCodeArtifact(artifact, {
          functionHint: stringValue(hints.function_hint, ''),
          eventName: stringValue(hints.event_name, ''),
          analysisId: stringValue(detail.analysis_id, ''),
        })
        window.setTimeout(() => respondReasoningLabAction(requestId, {
          status: 'succeeded',
          artifact_id: artifact.artifact_id,
          analysis_id: stringValue(detail.analysis_id, ''),
        }), 0)
      }
    }
    window.addEventListener('reasoning-lab-action', listener)
    return () => window.removeEventListener('reasoning-lab-action', listener)
  }, [acceptAgentStrategyResult, adoptPastedCodeArtifact, loadHotspots, loadHotspotsForTrace, prepare, refreshTraces, resolveHotspot, runBlindAnalysis, runBlindComparison, runStrategy, selectInvestigatedHotspot, selectTrace])

  const code = stringValue(codeUnit?.code, '')
  const source = record(codeUnit?.source)
  const hotspot = record(codeUnit?.hotspot)
  const hotspotAttribution = record(codeUnit?.hotspot_attribution)
  const hotspotSourceResolutionId = hotspotSourceResolution?.artifact_id
    || stringValue(source.source_resolution_artifact_id, '')
    || stringValue(source.trace_anchor_source_resolution_artifact_id, '')
  const hotspotNeedsInvestigation = Boolean(hotspotSourceResolutionId)
    && stringValue(hotspotAttribution.status, 'requires_code_path_investigation') !== 'model_selected_without_cpu_sampling'
  const blindOutput = record(blindResult?.result)
  const blindCandidates = Array.isArray(blindOutput.candidates) ? blindOutput.candidates.map(record) : []
  const blindComparisonOutput = record(blindComparison?.result)
  const flowResultAvailable = strategies.some((strategy) => {
    const artifact = results[strategy.id]
    return Boolean(artifact && stringValue(record(artifact.manual_selection).origin, '') !== 'user_manual')
  })
  const legacyConfigured = process.env.NEXT_PUBLIC_HETERO_AGENT_ENGINE === 'legacy'
    && Boolean(modelConfig.apiKey && modelConfig.model && modelConfig.baseUrl)
  const strategyPromptPreviews = useMemo(() => {
    const previews: Partial<Record<StrategyId, { budget: PromptTokenBudget; compactionDetail: string | null }>> = {}
    strategies.forEach((strategy) => {
      const context = contexts[strategy.id]
      if (!context) return
      const plan = buildStrategyPromptPlan(
        context,
        strategy.id,
        modelConfig.contextWindowTokens,
        modelConfig.agentMaxOutputTokens,
        {
          fullAnalysis: applySkillPrompt(fullOptimizationAnalysisSkill, skillProfile, 'full_analysis'),
          strategy: applySkillPrompt(strategySkillPrompts[strategy.id], skillProfile, strategySkillSlots[strategy.id]),
        },
      )
      previews[strategy.id] = { budget: plan.tokenBudget, compactionDetail: plan.compactionDetail }
    })
    return previews
  }, [contexts, modelConfig.agentMaxOutputTokens, modelConfig.contextWindowTokens, skillProfile])
  const deviceStrategyResults = strategies.flatMap((strategy) => {
    const artifact = results[strategy.id]
    if (!artifact) return []
    const manual = record(artifact.manual_selection)
    const manualLabel = stringValue(manual.label, '')
    return [{
      strategyId: strategy.id,
      strategyLabel: stringValue(manual.origin, '') === 'user_manual' && manualLabel ? `直接选择：${manualLabel}` : strategy.title,
      artifact,
    }]
  })
  const manualSelection = record(manualOpportunityResult?.manual_selection)
  const manualSelectedUnit = stringValue(manualSelection.selected_unit, '')
  const parallelOptimizationSelected = stringValue(manualSelection.selection_kind, '') === 'parallel_cpu'
  const routeResultForDeviceValidation = record(hardwareSemanticRoute?.resultArtifact.result)
  const routeCandidatesForDeviceValidation = Array.isArray(routeResultForDeviceValidation.candidates)
    ? routeResultForDeviceValidation.candidates.map(record)
    : []
  const suggestedDeviceValidationUnit = manualSelectedUnit
    || routeCandidatesForDeviceValidation
      .map((candidate) => stringValue(candidate.unit_id, ''))
      .find(Boolean)
    || null
  const hotspotQualityReason = stringValue(hotspotQuality.reason, '')
  const hotspotDurationKind = stringValue(hotspotQuality.duration_kind, '')
  const hotspotDurationsAreInclusive = hotspotQuality.cpu_self_time_available === false
    || hotspotDurationKind === 'inclusive_trace_span'
    || hotspotDurationKind === 'reported_trace_span_and_unmeasured_anchor'
    || hotspotDurationKind === 'unmeasured_trace_anchor'
  const guideTrace = workflow.stage === 'select_trace'
  const guideHotspots = workflow.stage === 'extract_hotspots'
  const guideCode = workflow.stage === 'select_hotspot_or_code'
  const guideHardwareRoute = workflow.stage === 'route_hardware'
  const guideStrategy = workflow.stage === 'choose_strategy'
  const hardwareRouteReady = Boolean(codeUnit && hardwareSemanticRoute?.codeArtifactId === codeUnit.artifact_id)
  const strategyStepReady = Boolean(codeUnit && (hardwareRouteReady || hardwareRoutingSkipped))
  const filteredApiSelectorRecords = useMemo(() => {
    const query = apiSelectorQuery.trim().toLocaleLowerCase()
    if (!query) return apiSelectorRecords
    return apiSelectorRecords.filter((item) => [
      item.record_id,
      item.symbol,
      item.name,
      item.name_zh,
      item.one_line_abstract_zh,
      item.detailed_description_zh,
      record(item.semantic_family).label_zh,
    ].some((value) => stringValue(value, '').toLocaleLowerCase().includes(query)))
  }, [apiSelectorQuery, apiSelectorRecords])
  const visibleHotspotGroups = useMemo(() => hotspotGroups.filter((group) => {
    const key = stringValue(group.module_key, '')
    return expandedHotspotModuleKeys.length === 0 || expandedHotspotModuleKeys.includes(key)
  }), [expandedHotspotModuleKeys, hotspotGroups])
  const assistantHotspots = useMemo(
    () => hotspotStageGroups.length > 0 ? hotspotStageGroups.flatMap(groupEvents) : visibleHotspotGroups.flatMap(groupEvents),
    [hotspotStageGroups, visibleHotspotGroups],
  )

  useEffect(() => {
    const codeSource = record(codeUnit?.source)
    const codeHotspot = record(codeUnit?.hotspot)
    const routeMatchesCode = Boolean(codeUnit && hardwareSemanticRoute?.codeArtifactId === codeUnit.artifact_id)
    const routeResult = routeMatchesCode ? record(hardwareSemanticRoute?.resultArtifact.result) : {}
    const routeSelectedUnits = (Array.isArray(routeResult.selected_units)
      ? routeResult.selected_units
      : Array.isArray(routeResult.candidates)
        ? routeResult.candidates
        : [])
      .map((item) => stringValue(record(item).unit_id, ''))
      .filter(Boolean)
      .slice(0, 1)
    const strategyResults = strategies.flatMap((strategy) => {
      const contextArtifact = contexts[strategy.id]
      const resultArtifact = results[strategy.id]
      if (!contextArtifact && !resultArtifact) return []
      const result = record(resultArtifact?.result)
      return [{
        strategy_id: strategy.id,
        status: resultArtifact ? 'completed' as const : 'context_ready' as const,
        context_artifact_id: contextArtifact?.artifact_id ?? null,
        result_artifact_id: resultArtifact?.artifact_id ?? null,
        recommendation: resultArtifact ? stringValue(result.recommendation, '') || null : null,
        summary: resultArtifact ? stringValue(result.summary, '') || null : null,
      }]
    })
    onAssistantStateChange({
      analysis_record_id: analysisRecordId,
      trace: trace ? {
        artifact_id: trace.artifact_id,
        label: traceLabel(trace),
        input_kind: stringValue(trace.input_kind, 'trace'),
      } : null,
      hotspot_limit: hotspotStageGroups.length > 0 ? assistantHotspots.length : REASONING_HOTSPOTS_PER_MODULE,
      hotspot_module_count: hotspotStageGroups.length > 0 ? hotspotStageGroups.length : hotspotGroups.length,
      hotspots: assistantHotspots.map((item, index) => {
        const metrics = eventMetrics(item)
        return {
          rank: numberValue(item.rank) ?? index + 1,
          module_rank: numberValue(item.module_rank),
          event_id: stringValue(item.event_id, ''),
          event_name: stringValue(item.event_name, ''),
          module: eventModule(item),
          self_time_ms: numberValue(metrics.self_time_estimate_total_duration_ms ?? metrics.total_duration_ms),
          max_self_time_ms: numberValue(metrics.max_self_time_estimate_duration_ms ?? metrics.max_span_duration_ms ?? metrics.max_duration_ms),
          occurrence_count: numberValue(metrics.occurrence_count ?? metrics.call_count),
        }
      }),
      code_unit: codeUnit ? {
        artifact_id: codeUnit.artifact_id,
        event_name: stringValue(codeHotspot.event_name, stringValue(codeHotspot.function_hint, '热点代码')),
        source_origin: stringValue(codeSource.origin, 'unknown'),
        source_path: stringValue(codeSource.path, ''),
        source_binding: stringValue(codeUnit.source_binding?.match_level, 'unknown'),
        code_sha256: stringValue(codeSource.sha256, codeUnit.content_sha256),
        code_preview: code.slice(0, 6000),
      } : null,
      hardware_routing: codeUnit ? {
        status: routeMatchesCode ? 'completed' : hardwareRoutingSkipped || parallelOptimizationSelected ? 'skipped' : 'pending',
        result_artifact_id: routeMatchesCode ? hardwareSemanticRoute?.resultArtifact.artifact_id ?? null : null,
        selection_origin: routeMatchesCode ? stringValue(routeResult.selection_origin, '') || null : parallelOptimizationSelected ? 'user_manual' : null,
        decision: routeMatchesCode ? stringValue(routeResult.decision, '') || null : parallelOptimizationSelected ? 'parallel_cpu' : null,
        selected_units: parallelOptimizationSelected ? ['parallel_cpu'] : routeSelectedUnits,
      } : null,
      blind_baseline: {
        status: blindResult ? 'completed' : 'not_run',
        result_artifact_id: blindResult?.artifact_id ?? null,
        comparison_artifact_id: blindComparison?.artifact_id ?? null,
        session_isolated: true,
      },
      strategy_results: strategyResults,
      selected_api_record_id: selectedApiRecordId || null,
    })
  }, [analysisRecordId, assistantHotspots, blindComparison, blindResult, code, codeUnit, contexts, hardwareRoutingSkipped, hardwareSemanticRoute, hotspotGroups.length, hotspotStageGroups.length, onAssistantStateChange, parallelOptimizationSelected, results, selectedApiRecordId, trace])

  const errorForwarded = useWorkbenchErrorNotification(notice, () => setNotice(null), 'opportunity-reasoning')
  return <div className="workspace-scroll reasoning-lab">
    <section className="work-section reasoning-heading">
      <div className="section-heading"><div><span className="eyebrow">机会推理</span><h2>热点异构与并行机会实验室</h2></div><div className="reasoning-heading-actions"><span className="reasoning-model-state ready">{legacyConfigured ? 'Legacy 模型中继' : 'Kimi Agent Engine'}</span><span className="icon-tooltip"><button className="icon-button" type="button" title="重置当前分析页面" aria-label="重置当前分析页面" onClick={onResetAnalysis}><RotateCcw size={15} /></button><span className="icon-tooltip-content" role="tooltip">重置当前分析页面</span></span></div></div>
      <p>从真实 Trace 定位热点代码，再通过语义路由、人工路由或并行优化快捷入口形成待验证方向。硬件机会推理和真机实验始终保留证据边界。</p>
      {workflow.stage !== 'idle' && <div className="workflow-progress"><Hand size={15} aria-hidden="true" /><span>页面助手引导中：{workflow.detail}</span></div>}
      {notice && !errorForwarded && <div className={`reasoning-notice ${notice.tone}`}>{notice.tone === 'error' ? <ShieldAlert size={15} /> : <Check size={15} />}<span>{notice.text}</span></div>}
    </section>

    <section className="work-section reasoning-target-profile">
      <div className="reasoning-target-profile-heading"><div><span className="eyebrow">分析环境</span><strong>目标设备与系统画像</strong><small>{targetPlatformProfileLabel(targetPlatformProfile)}</small></div><span>用户选择，非设备探测</span></div>
      <div className="reasoning-target-profile-controls table-shell">
        <label><span>设备 / SoC</span><select value={targetPlatformProfile.chipset.model_id} onChange={(event) => onTargetPlatformProfileChange(createTargetPlatformProfile(event.target.value, targetPlatformProfile.operating_system.version_id))}>{chipsetOptions.map((item) => <option value={item.id} key={item.id}>{item.label}</option>)}</select></label>
        <label><span>操作系统</span><select value={targetPlatformProfile.operating_system.version_id} onChange={(event) => onTargetPlatformProfileChange(createTargetPlatformProfile(targetPlatformProfile.chipset.model_id, event.target.value))}>{operatingSystemOptions.map((item) => <option value={item.id} key={item.id}>{item.label}</option>)}</select></label>
      </div>
      <p>此画像会写入每个策略的 context 和完整推理记录。它只限定分析目标，不能证明 NPU、GPU、DSP、驱动、接口或收益已经可用。</p>
    </section>

    <section className="work-section reasoning-step">
      <div className="reasoning-step-title"><span>1</span><div><strong>真实 Trace 热点</strong><small>选择已经分析的 trace，或直接导入原始 .htrace / trace 团队 JSON 报告。</small>{guideTrace && <WorkflowGuideCue text="选择或导入真实 Trace" />}{guideHotspots && <WorkflowGuideCue text="点击提取热点" />}</div><button className="quiet-button" type="button" disabled={busy === 'traces'} onClick={() => void refreshTraces()}>{busy === 'traces' ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}刷新列表</button></div>
      <div className="reasoning-trace-controls table-shell">
        <label className={guideTrace ? 'workflow-guided-target' : ''}>
          <span>分析工件</span>
          <select value={trace?.artifact_id ?? ''} onChange={(event) => selectTrace(traces.find((item) => item.artifact_id === event.target.value) ?? null)}>
            <option value="">选择真实 Trace 分析工件</option>
            {traces.map((item) => <option value={item.artifact_id} key={item.artifact_id}>{traceLabel(item)} · {item.artifact_id.slice(0, 12)}</option>)}
          </select>
        </label>
        <label className={`quiet-button reasoning-upload ${guideTrace ? 'workflow-guided-target' : ''}`}>
          <FileUp size={14} />导入真实 Trace
          <input className="visually-hidden" type="file" accept=".htrace,.json,application/json" onChange={(event) => void importTrace(event)} />
        </label>
        <button className={`primary-button ${guideHotspots ? 'workflow-guided-target' : ''}`} type="button" disabled={!trace || busy === 'hotspots'} onClick={() => void loadHotspots()}>
          {busy === 'hotspots' ? <LoaderCircle size={15} className="spin" /> : <FileCode2 size={15} />}提取热点
        </button>
      </div>
      {trace && <div className="reasoning-trace-meta"><span>{traceLabel(trace)}</span><span>{trace.hotspot_count ?? 0} 个归一化热点 · {trace.cluster_count ?? 0} 个簇</span><span>源码绑定 {stringValue(record(trace.source_binding).match_level, 'unknown')}</span></div>}
      {hotspotStageGroups.length > 0 && <div className={guideCode ? 'workflow-guided-target' : ''}>
        <TraceStageTimeline
          stages={hotspotStageGroups}
          selectedEventId={stringValue(hotspot.event_id, '')}
          busyEventId={busy?.startsWith('resolve:') ? busy.slice('resolve:'.length) : ''}
          onSelectEvent={(eventId) => void resolveHotspot(eventId)}
        />
      </div>}
      {hotspotStageGroups.length === 0 && hotspotGroups.length > 0 && <div className={`reasoning-hotspots ${guideCode ? 'workflow-guided-target' : ''}`}>
        <div className="reasoning-hotspot-list-note">按模块聚合，模块内按自耗时排序并展示 Top {REASONING_HOTSPOTS_PER_MODULE}。展开模块后可在会话中说“选择第 N 个热点”，或直接解析源码。</div>
        <div className="reasoning-hotspot-module-list">
          {hotspotGroups.map((group) => {
            const moduleKey = stringValue(group.module_key, '')
            const moduleName = stringValue(group.module_name, moduleKey || '未分类模块')
            const items = groupEvents(group)
            const expanded = expandedHotspotModuleKeys.includes(moduleKey)
            const moduleSelfTime = group.self_time_estimate_total_duration_ms
            const moduleMetricLabel = stringValue(group.metric_semantics, '') === 'inclusive_trace_span' ? '代表事件跨度' : '模块自耗时'
            return <details className={`reasoning-hotspot-module ${group.system_collapsed ? 'system' : ''}`} key={moduleKey || moduleName} open={expanded} onToggle={(event) => {
              const open = event.currentTarget.open
              setExpandedHotspotModuleKeys((current) => open
                ? current.includes(moduleKey) ? current : [...current, moduleKey]
                : current.filter((key) => key !== moduleKey))
            }}>
              <summary>
                <div><strong>{moduleName}</strong><span>{countValue(group.event_group_count)} 个事件组 · {moduleMetricLabel} {milliseconds(moduleSelfTime)}</span></div>
                <span className="reasoning-module-topn">Top {countValue(group.returned_event_count)} / {countValue(group.event_group_count)}</span>
              </summary>
              <div className="reasoning-module-events table-shell">
                <div className="table-header reasoning-hotspot"><span>热点 / Trace 锚点</span><span>{hotspotDurationsAreInclusive ? '代表跨度 / 最大单次' : '自耗时 / 最大单次'}</span><span>{hotspotDurationsAreInclusive ? '已列样本' : '调用'}</span><span /></div>
                {items.map((item) => {
                  const metrics = eventMetrics(item)
                  const selected = stringValue(hotspot.event_id, '') === stringValue(item.event_id, '')
                  const selfTime = metrics.self_time_estimate_total_duration_ms ?? metrics.total_duration_ms
                  const maxTime = metrics.max_self_time_estimate_duration_ms ?? metrics.max_span_duration_ms ?? metrics.max_duration_ms
                  const calls = metrics.occurrence_count ?? metrics.call_count
                  const eventId = stringValue(item.event_id)
                  return <div className={`table-row reasoning-hotspot ${selected ? 'selected' : ''}`} key={eventId}>
                    <div><strong><code>#{numberValue(item.rank) ?? '—'}</code>{stringValue(item.event_name)}</strong><span>{stringValue(item.summary, stringValue(item.source_resolution, ''))}</span></div>
                    <span>{milliseconds(selfTime)} / {milliseconds(maxTime)}</span>
                    <span>{countValue(calls)}</span>
                    <button className="icon-button" type="button" title="解析真实热点源码" aria-label="解析真实热点源码" disabled={busy === `resolve:${eventId}`} onClick={() => void resolveHotspot(eventId)}>{busy === `resolve:${eventId}` ? <LoaderCircle size={15} className="spin" /> : selected ? <Check size={15} /> : <FileCode2 size={15} />}</button>
                  </div>
                })}
              </div>
            </details>
          })}
        </div>
      </div>}
      {trace && hotspotStageGroups.length === 0 && hotspots.length === 0 && hotspotQualityReason && <div className="event-empty">{hotspotQualityReason}</div>}
    </section>

    <section className="work-section reasoning-step">
      <div className="reasoning-step-title"><span>2</span><div><strong>热点代码</strong><small>桩点包围函数只是调用路径锚点；沿局部区域、caller、callee 或外部运行时继续定位实际候选。</small>{guideCode && <WorkflowGuideCue text="选择热点或粘贴热点代码" />}</div>{hotspotNeedsInvestigation && <button className="quiet-button" type="button" disabled={busy !== null} onClick={requestHotspotInvestigation} title="沿调用路径深挖实际热点代码"><Search size={14} />深挖实际热点</button>}</div>
      <div className={`reasoning-code-grid ${guideCode ? 'workflow-guided-target' : ''}`}>
        <div className="reasoning-code-panel table-shell"><div className="reasoning-code-meta"><strong>{stringValue(hotspot.event_name, stringValue(record(hotspotSourceResolution?.event).event_name, '尚未选择热点'))}</strong><span>{stringValue(source.origin, hotspotSourceResolution ? 'trace_event_source_resolution' : '')} · {stringValue(source.path, '无文件绑定')} {source.line_start ? `:${String(source.line_start)}-${String(source.line_end)}` : ''}</span></div>{code ? <pre><code>{code}</code></pre> : <div className="event-empty">{hotspotSourceResolution ? '事件已解析，但当前代码仓尚未形成可验证的桩点函数；可让页面助手检查外部运行时边界。' : '选择步骤 1 的真实热点，或在右侧粘贴代码。'}</div>}</div>
        <form className={`reasoning-paste table-shell ${guideCode ? 'workflow-guided-target' : ''}`} onSubmit={(event) => void submitPastedCode(event)}><div><strong>粘贴代码</strong><span>仅用于辅助推理；不会被当作 trace 精确源码绑定。</span></div><input value={functionHint} onChange={(event) => setFunctionHint(event.target.value)} placeholder="函数名提示，例如 Foo::Run" /><input value={eventName} onChange={(event) => setEventName(event.target.value)} placeholder="关联热点事件名（可选）" /><textarea value={pastedCode} onChange={(event) => setPastedCode(event.target.value)} placeholder="粘贴 C/C++ 热点函数或代码片段" rows={8} /><button className={`quiet-button ${guideCode ? 'workflow-guided-target' : ''}`} type="submit" disabled={!pastedCode.trim() || busy === 'paste'}>{busy === 'paste' ? <LoaderCircle size={14} className="spin" /> : <ClipboardPaste size={14} />}作为热点代码</button></form>
      </div>
    </section>

    <section className="work-section reasoning-step">
      <div className="reasoning-step-title"><span>3</span><div><strong>硬件单元路由</strong><small>可运行模型语义路由，也可直接选择硬件目录或并行优化；后续证据否定当前方向时可重新路由。</small>{guideHardwareRoute && <WorkflowGuideCue text="运行语义路由，或直接选择路由" />}</div></div>
      <HardwareSemanticRoutingExperiment
        codeUnit={codeUnit}
        restoredRoute={hardwareSemanticRoute}
        analysisRecordId={analysisRecordId}
        modelConfig={modelConfig}
        targetPlatformProfile={targetPlatformProfile}
        skillPrompt={applySkillPrompt(hardwareSemanticRoutingSkill, skillProfile, 'hardware_route')}
        skillProfileId={skillProfile?.profile_id}
        onRouteResolved={handleHardwareSemanticRouteResolved}
        onRouteFailed={handleHardwareSemanticRouteFailed}
        onParallelSelected={selectParallelOptimization}
        onSkip={skipHardwareSemanticRoute}
      />
    </section>

    <section className="work-section reasoning-step" ref={strategyStepRef} id="hardware-opportunity-reasoning">
      <div className="reasoning-step-title"><span>4</span><div><strong>硬件机会推理</strong><small>让模型结合热点代码与所选硬件 API 上下文判断机会；若主方向不相容，可返回重新路由或在单元内重选 API。</small>{guideStrategy && <WorkflowGuideCue text="运行硬件机会推理，或直接选择优化方向" />}</div><div className="reasoning-step-actions"><button className="quiet-button" type="button" disabled={!hardwareRouteReady || apiSelectorBusy || Boolean(strategyCommandSubmission)} onClick={() => void openApiSelector()}>{apiSelectorBusy ? <LoaderCircle size={14} className="spin" /> : <ListChecks size={14} />}重选 API</button><button className="quiet-button" type="button" disabled={!codeUnit || busy !== null || Boolean(strategyCommandSubmission)} onClick={rerunHardwareSemanticRoute}><RefreshCw size={14} />重新路由</button></div></div>
      {parallelOptimizationSelected
        ? <div className="reasoning-bypass-state"><Check size={16} /><div><strong>已直接选择并行优化</strong><span>硬件机会推理已跳过；当前用户选择工件已绑定热点代码，可在第 5 步进行串行 reference 与 CPU 并行实现的真机对照。</span></div></div>
        : !strategyStepReady
          ? <div className="event-empty">热点代码已就绪。请先完成第 3 步语义路由，或直接选择一个硬件单元。</div>
          : hardwareRoutingSkipped
            ? <div className="event-empty">已跳过硬件路由，当前没有可供硬件机会推理使用的 API 目录。请返回第 3 步选择硬件或并行优化。</div>
            : <div className={`reasoning-strategy-grid single ${guideStrategy ? 'workflow-guided-target' : ''}`}>
        {visibleStrategies.map((strategy) => {
          const Icon = strategy.icon
          const context = contexts[strategy.id]
          const result = results[strategy.id]
          const contextRecords = Array.isArray(context?.context_records) ? context.context_records as JsonRecord[] : []
          const modelContext = context ? reasoningModelInputContext(context) : {}
          const modelContextRecords = Array.isArray(modelContext.context_records) ? modelContext.context_records as JsonRecord[] : []
          const candidateScopes = Array.isArray(modelContext.candidate_scopes) ? modelContext.candidate_scopes.map(record).slice(0, 4) : []
          const npuApiRecordCount = modelContextRecords.filter((item) => stringValue(item.evidence_id).startsWith('npu_api:')).length
          const hardwareRouting = record(modelContext.hardware_routing)
          const hardwareRoutingOrigin = stringValue(hardwareRouting.selection_origin, 'model')
          const selectedHardwareUnits = Array.isArray(hardwareRouting.selected_units) ? hardwareRouting.selected_units.map(record).slice(0, 2) : []
          const selectedHardwareRoute = selectedHardwareUnits[0] ?? {}
          const routedUnitLabel = stringValue(selectedHardwareRoute.unit_id, '')
          const routedHardwareLabel = stringValue(selectedHardwareRoute.hardware_name, stringValue(selectedHardwareRoute.hardware_id, ''))
          const routedApiRecordCount = modelContextRecords.filter((item) => {
            const evidenceId = stringValue(item.evidence_id, '')
            return evidenceId.startsWith('npu_api:') || evidenceId.startsWith('accelerator_api:')
          }).length
          const tokenStats = strategyTokenStats[strategy.id] ?? strategyPromptPreviews[strategy.id]
          const tokenBudget = tokenStats?.budget
          const tokenUsage = tokenStats && 'usage' in tokenStats ? tokenStats.usage : undefined
          const output = resultSummary(result)
          const validation = record(output.validation)
          const validationErrors = Array.isArray(validation.errors) ? validation.errors.map((item) => stringValue(item, '')).filter(Boolean) : []
          const adaptation = record(output.format_adaptation)
          const adaptationActions = Array.isArray(adaptation.actions) ? adaptation.actions.map((item) => stringValue(item, '')).filter(Boolean) : []
          const calibration = record(output.evidence_calibration)
          const calibrationActions = Array.isArray(calibration.actions) ? calibration.actions.map((item) => stringValue(item, '')).filter(Boolean) : []
          const opportunities = Array.isArray(output.opportunities) ? output.opportunities as JsonRecord[] : []
          const missingEvidence = Array.isArray(output.missing_evidence) ? output.missing_evidence.map((item) => stringValue(item, '')).filter(Boolean) : []
          const rawOutput = stringValue(result?.raw_output, '')
          return <article className="reasoning-strategy" key={strategy.id}>
            <div className="reasoning-strategy-head"><Icon size={17} /><div><strong>{strategy.title}</strong><span>{strategy.description}</span></div></div>
            <div className="reasoning-context-status"><span>上下文</span><strong>{context ? `模型 ${modelContextRecords.length} / 审计 ${contextRecords.length}` : '未准备'}</strong>{context && <small>{stringValue(context.context_sha256).slice(0, 12)}</small>}</div>
            {tokenBudget && <div className="reasoning-token-status"><div><span>估算 Input</span><strong>{formatTokenCount(tokenBudget.inputTokens)}</strong></div><div><span>实际 Input</span><strong>{tokenUsage ? formatTokenCount(tokenUsage.inputTokens) : '待调用'}</strong></div><div><span>实际 Output</span><strong>{tokenUsage ? formatTokenCount(tokenUsage.outputTokens) : '待调用'}</strong></div><small>{tokenBudget.compactionApplied ? '请求内容已按运行时传输范围精简；完整审计工件仍保留。' : '上下文管理由 Agent Engine 负责。'}{tokenUsage && (tokenUsage.inputSource === 'estimated' || tokenUsage.outputSource === 'estimated') ? ' Token 用量含本地估算。' : ''}</small></div>}
            <div className="reasoning-strategy-actions">
              <button className="quiet-button" type="button" disabled={!codeUnit || Boolean(strategyCommandSubmission) || busy === `prepare:${strategy.id}` || (strategy.id === 'hardware_kb' && !hardwareRouteReady)} onClick={() => void prepare(strategy.id)}>{busy === `prepare:${strategy.id}` ? <LoaderCircle size={14} className="spin" /> : <Code2 size={14} />}准备</button>
              <button className={`primary-button compact ${guideStrategy ? 'workflow-guided-target' : ''}`} type="button" disabled={!codeUnit || Boolean(strategyCommandSubmission) || busy === `run:${strategy.id}` || (strategy.id === 'hardware_kb' && !hardwareRouteReady)} onClick={() => submitStrategyCommand(strategy.id, strategy.title)}>
                {strategyCommandSubmission?.strategyId === strategy.id || busy === `run:${strategy.id}` ? <LoaderCircle size={14} className="spin" /> : <WandSparkles size={14} />}
                {strategyCommandSubmission?.strategyId === strategy.id ? strategyCommandSubmission.accepted ? '已提交，等待推理' : '正在提交' : '推理'}
              </button>
            </div>
            {strategyFeedback[strategy.id] && <div className="reasoning-strategy-feedback">{strategyFeedback[strategy.id]}</div>}
            {context && <details className="reasoning-context-details"><summary>查看模型可用证据</summary><p>模型只接收热点语义、精确源码、目标画像与下列受控证据；哈希、时间戳、原始检索词和工件追溯信息保留在审计工件中，不会发送给模型。</p><p>目标设备与系统：{targetPlatformProfileLabel(targetPlatformProfile)}（用户选择，非设备探测）</p>{strategy.id === 'hardware_kb' && <p>{routedUnitLabel ? hardwareRoutingOrigin === 'user_manual' ? `硬件单元目录：由用户指定 ${routedUnitLabel}${routedHardwareLabel ? `（${routedHardwareLabel}）` : ''} 作为 API 复核入口；当前阶段仅提供该目录的 ${routedApiRecordCount} 条 API Top-N。该选择不是模型或设备能力证据。` : `硬件单元目录：先由模型根据代码语义与能力卡选择 ${routedUnitLabel}${routedHardwareLabel ? `（${routedHardwareLabel}）` : ''}；当前阶段仅提供该目录的 ${routedApiRecordCount} 条 API Top-N，并可因语义不相容而拒绝。` : '硬件单元目录：模型未形成可用候选，因此不会横向扫描所有 API 目录。'}</p>}{candidateScopes.length > 0 && <p>源码候选作用域：{candidateScopes.map((scope) => `${candidateScopeLevelLabel(stringValue(scope.level))} 第 ${numberValue(scope.line_start) ?? '?'}-${numberValue(scope.line_end) ?? '?'} 行${stringValue(scope.resolution_status) === 'callee_unresolved' ? '（待展开）' : ''}`).join('；')}。它们是词法选择提示，不是 AST 证明。</p>}{npuApiRecordCount > 0 && <p>本轮含 {npuApiRecordCount} 条 NNRT 图算子 Top-N 候选：它们是 API 简介语义候选，不是当前设备支持结论，仍需完整图、`GetAvailableOperations`、编译和基准验证。</p>}{contextRecords.length > modelContextRecords.length && <p>审计工件保留 {contextRecords.length} 条检索记录；模型只使用最高相关的 {modelContextRecords.length} 条。</p>}{modelContextRecords.length > 0 ? <ul>{modelContextRecords.map((item) => { const evidence = record(item.record); const symbol = stringValue(evidence.symbol); const name = stringValue(evidence.name, stringValue(evidence.title, '结构化记录')); const abstract = stringValue(evidence.one_line_abstract_zh); return <li key={stringValue(item.evidence_id)}><code>{stringValue(item.evidence_id)}</code><span>{symbol ? `${symbol} · ` : ''}{name}{abstract ? `：${abstract}` : ''}</span></li> })}</ul> : <p>{strategy.id === 'direct_baseline' ? '此策略按设计不提供优化知识库或硬件知识库记录。' : '此策略没有通过相关性门槛的优化知识库或硬件知识库记录。'}</p>}</details>}
            {result && <div className="reasoning-result">
              <div><span className="eyebrow">结论</span><strong>{stringValue(output.recommendation)}</strong><span>{stringValue(output.evidence_status)} · {stringValue(output.confidence)}</span></div>
              <p>{stringValue(output.summary)}</p>
              {opportunities.map((item, index) => {
                const scope = opportunityScopeSummary(item)
                return <div className="reasoning-opportunity" key={index}><strong>{stringValue(item.opportunity_kind)} · {stringValue(item.candidate_target)}</strong><span>{stringValue(item.rationale)}</span>{scope && <small>{scope}</small>}</div>
              })}
              {missingEvidence.length > 0 && <div className="reasoning-missing-evidence"><strong>仍需证据</strong><span>{missingEvidence.join('；')}</span></div>}
              {calibrationActions.length > 0 && <div className="reasoning-adaptation"><strong>证据门禁</strong><span>{calibrationActions.join('；')}</span></div>}
              {adaptationActions.length > 0 && <div className="reasoning-adaptation"><strong>结构适配</strong><span>{adaptationActions.join('；')}</span></div>}
              {stringValue(validation.status, '') === 'invalid' && <div className="reasoning-validation"><strong>契约校验问题</strong><span>{validationErrors.join('；') || '模型输出未满足本次受控结论契约。'}</span></div>}
              <details className="reasoning-raw-output"><summary>查看模型原始回答</summary><pre>{rawOutput || '模型没有返回可展示的文本。'}</pre></details>
            </div>}
          </article>
        })}
      </div>}
    </section>

    {apiSelectorOpen && <div className="reasoning-api-modal-backdrop" role="presentation" onMouseDown={() => !apiSelectorBusy && setApiSelectorOpen(false)}>
      <section className="reasoning-api-modal" role="dialog" aria-modal="true" aria-labelledby="reasoning-api-modal-title" onMouseDown={(event) => event.stopPropagation()}>
        <header><div><span className="eyebrow">人工选择</span><h3 id="reasoning-api-modal-title">选择硬件 API 优化方向</h3><p>列表来自当前路由硬件的 API 知识库。选择只表示验证意图，不代表代码已经适配或设备已经支持。</p></div><button className="icon-button" type="button" title="关闭" aria-label="关闭 API 选择" disabled={apiSelectorBusy} onClick={() => setApiSelectorOpen(false)}><X size={16} /></button></header>
        <label className="reasoning-api-search"><Search size={15} /><input autoFocus value={apiSelectorQuery} onChange={(event) => setApiSelectorQuery(event.target.value)} placeholder="按 API、语义族或能力简介筛选" /></label>
        <div className="reasoning-api-list" role="radiogroup" aria-label="硬件 API 列表">
          {filteredApiSelectorRecords.map((item) => {
            const recordId = stringValue(item.record_id, '')
            const name = stringValue(item.symbol, stringValue(item.name_zh, stringValue(item.name, recordId)))
            const family = stringValue(record(item.semantic_family).label_zh, stringValue(record(item.semantic_family).id, '未分类'))
            return <label className={`reasoning-api-option ${selectedApiRecordId === recordId ? 'selected' : ''}`} key={recordId}>
              <input type="radio" name="manual-api-direction" value={recordId} checked={selectedApiRecordId === recordId} onChange={() => setSelectedApiRecordId(recordId)} />
              <span><strong>{name}</strong><small>{family} · {recordId}</small><em>{stringValue(item.one_line_abstract_zh, '暂无一句话简介。')}</em></span>
            </label>
          })}
          {apiSelectorBusy && apiSelectorRecords.length === 0
            ? <div className="event-empty"><LoaderCircle size={15} className="spin" />正在载入硬件 API 目录…</div>
            : filteredApiSelectorRecords.length === 0 && <div className="event-empty">没有符合当前筛选条件的 API。</div>}
        </div>
        <footer><span>共 {apiSelectorRecords.length} 条，当前显示 {filteredApiSelectorRecords.length} 条</span><div><button className="quiet-button" type="button" disabled={apiSelectorBusy} onClick={() => setApiSelectorOpen(false)}>取消</button><button className="primary-button" type="button" disabled={!selectedApiRecordId || apiSelectorBusy} onClick={() => void applyManualApiSelection()}>{apiSelectorBusy ? <LoaderCircle size={14} className="spin" /> : <Check size={14} />}采用此方向</button></div></footer>
      </section>
    </div>}

    <section className="work-section reasoning-step" ref={deviceStepRef} id="device-validation">
      <div className="reasoning-step-title"><span>5</span><div><strong>可选设备证据与真机验证</strong><small>一次启动后，页面助手会自行发现设备、写最小实验、构建上传并依据手机输出迭代；失败证据可触发 API 重选或重新路由。</small></div></div>
      <DeviceVerificationPanel
        deployment={deployment}
        sourceConnectorId={sourceConnectorId}
        analysisRecordId={analysisRecordId}
        codeUnit={codeUnit}
        strategyResults={deviceStrategyResults}
        suggestedUnit={suggestedDeviceValidationUnit}
        modelConfig={modelConfig}
        onValidationRecorded={(validation) => {
          if (!analysisRecordId) {
            setNotice({ tone: 'error', text: '当前热点尚未创建优化分析记录；请重新选择热点代码后再保存真机验证方案。' })
            return
          }
          onDeviceValidationRecorded(analysisRecordId, validation)
        }}
      />
    </section>

    <section className="work-section reasoning-step">
      <div className="reasoning-step-title"><span>6</span><div><strong>独立盲推理基线</strong><small>正式流程与真机验证之后运行，但仍只使用原始代码、用户问题和目标平台画像；不读取路由、知识库、API Top-N、流程结论或设备结果。</small></div><div className="reasoning-step-actions"><button className="quiet-button" type="button" disabled={!codeUnit || !flowResultAvailable || busy === 'blind-analysis'} onClick={() => void runBlindAnalysis()}>{busy === 'blind-analysis' ? <LoaderCircle size={14} className="spin" /> : <BrainCircuit size={14} />}运行盲推理</button><button className="quiet-button" type="button" disabled={!blindResult || !flowResultAvailable || busy === 'blind-comparison'} onClick={() => void runBlindComparison()}>{busy === 'blind-comparison' ? <LoaderCircle size={14} className="spin" /> : <ListChecks size={14} />}对照完整流程</button></div></div>
      {!blindResult && <div className="event-empty">先完成正式机会推理，并尽可能取得真机证据；随后用隔离输入生成不受流程上下文影响的盲推基线。</div>}
      {blindFeedback && <div className="reasoning-strategy-feedback">{blindFeedback}</div>}
      {blindTokenStats && <div className="reasoning-token-status"><div><span>Input</span><strong>{blindTokenStats.usage ? formatTokenCount(blindTokenStats.usage.inputTokens) : formatTokenCount(blindTokenStats.budget.inputTokens)}</strong></div><div><span>Output</span><strong>{blindTokenStats.usage ? formatTokenCount(blindTokenStats.usage.outputTokens) : '待调用'}</strong></div><small>盲推分析：{stringValue(blindResult?.blind_session_analysis_id, stringValue(blindContext?.blind_session_analysis_id, '尚未创建'))}</small></div>}
      {blindResult && <div className="reasoning-result blind-result">
        <div><span className="eyebrow">盲推理结论</span><strong>{stringValue(blindOutput.confidence, 'low')}</strong><span>独立基线 · 非设备证据</span></div>
        <p>{stringValue(blindOutput.summary)}</p>
        {blindCandidates.map((candidate, index) => <div className="reasoning-opportunity" key={`${stringValue(candidate.target)}-${index}`}><strong>{stringValue(candidate.optimization_kind)} · {stringValue(candidate.hardware_unit)} · {stringValue(candidate.target)}</strong><span>{stringValue(candidate.rationale)}</span>{Array.isArray(candidate.api_candidates) && candidate.api_candidates.length > 0 && <small>API 候选：{candidate.api_candidates.map((item) => stringValue(item, '')).filter(Boolean).join('、')}</small>}</div>)}
        <div className="reasoning-missing-evidence"><strong>首选路径</strong><span>{stringValue(blindOutput.preferred_path)}</span></div>
      </div>}
      {blindComparison && <div className="reasoning-result blind-comparison-result">
        <div><span className="eyebrow">盲推理 / 完整流程对照</span><strong>{stringValue(blindComparisonOutput.verdict, 'inconclusive')}</strong><span>同模型自评 · 非准确率真值</span></div>
        <p>{stringValue(blindComparisonOutput.summary)}</p>
        <div className="reasoning-missing-evidence"><strong>合并建议</strong><span>{stringValue(blindComparisonOutput.merged_recommendation)}</span></div>
        {Array.isArray(blindComparisonOutput.disagreements) && blindComparisonOutput.disagreements.length > 0 && <div className="reasoning-adaptation"><strong>关键分歧</strong><span>{blindComparisonOutput.disagreements.map((item) => stringValue(item, '')).filter(Boolean).join('；')}</span></div>}
      </div>}
    </section>

    <section className="work-section reasoning-step reasoning-transcript">
      <div className="reasoning-step-title"><span>7</span><div><strong>推理过程</strong><small>记录上下文、模型往返、结构适配和契约校验；不会展示 API 密钥。</small></div></div>
      {reasoningEvents.length === 0
        ? <div className="event-empty">尚未执行推理。选择一个策略后，这里会按时间显示完整过程。</div>
        : <ol className="reasoning-event-list">
          {reasoningEvents.map((event) => <li className={`reasoning-event ${event.status}`} key={event.id}>
            <div className="reasoning-event-meta"><span>{event.timestamp}</span><span>{inferenceStrategyLabel(event.strategyId)}</span><span>{reasoningStageLabel(event.stage)}</span><strong>{reasoningStatusLabel(event.status)}</strong></div>
            <div className="reasoning-event-body"><strong>{event.title}</strong><span>{event.detail}</span></div>
            {event.payload && <details className="reasoning-event-payload"><summary>{event.payloadLabel ?? '查看详细内容'}</summary><pre>{event.payload}</pre></details>}
          </li>)}
        </ol>}
    </section>
  </div>
}
