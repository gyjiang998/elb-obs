'use client'

import { BrainCircuit, Check, Cpu, FlaskConical, ListChecks, LoaderCircle, ShieldAlert, Square, UserRoundCheck, WandSparkles } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../lib/api'
import { loadCustomApiCatalog } from '../lib/analysis-catalog-cache'
import { agentCommandStatusEventName, dispatchAgentCommand } from '../lib/agent-command-bridge'
import { analysisErrorPayload, appendAnalysisTraceEvent } from '../lib/analysis-observability'
import { buildHardwareSemanticRoutingPromptPlan } from '../lib/hardware-semantic-routing'
import { requestModelChatCompletionStream, type ModelStreamProgress } from '../lib/model-client'
import type { BrowserModelConfig } from '../lib/model-config'
import { randomUuid } from '../lib/random-id'
import { rejectReasoningLabAction, respondReasoningLabAction } from '../lib/reasoning-action-bridge'
import { mergeModelTokenUsage, parseOrRepairModelJson } from '../lib/structured-model-output'
import { formatTokenCount, type ModelTokenUsage, type PromptTokenBudget } from '../lib/token-budget'
import type { TargetPlatformProfile } from '../lib/target-platform-profile'
import type { Artifact, JsonRecord } from '../lib/types'

type RoutingContextResponse = { artifact: Artifact; context: JsonRecord }
type RoutingSaveResponse = { artifact: Artifact; validation_errors: string[] }
type RoutingManualSelectionResponse = { artifact: Artifact; validation_errors: string[] }
type RoutingDirectSelectionResponse = { artifact: Artifact; context_artifact: Artifact; validation_errors: string[] }

type RoutingState = 'idle' | 'preparing' | 'routing' | 'structuring' | 'saving' | 'succeeded' | 'failed'

function record(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {}
}

function stringValue(value: unknown, fallback = ''): string {
  return typeof value === 'string' && value ? value : fallback
}

function values(value: unknown): JsonRecord[] {
  return Array.isArray(value) ? value.map(record) : []
}

function stateLabel(state: RoutingState): string {
  return ({
    idle: '等待运行',
    preparing: '准备能力卡',
    routing: '模型语义路由中',
    structuring: '整理结构化结论',
    saving: '校验并存档',
    succeeded: '已完成',
    failed: '未完成',
  })[state]
}

function unitLabel(unitId: string): string {
  return ({ npu: 'NPU / NNRT', gpu: 'GPU', dsp: 'DSP 音频', media: '媒体硬件编解码' })[unitId] ?? unitId
}

function routeDecisionLabel(value: string): string {
  return ({ select_units: '已选择候选单元', needs_compute_core: '需要展开计算核心', no_accelerator_candidate: '当前无加速器候选' })[value] ?? '等待模型结论'
}

function renderLines(value: unknown, fallback = '—'): string {
  const items = Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string' && item.trim().length > 0) : []
  return items.length > 0 ? items.join('；') : fallback
}

function routingProgressText(state: RoutingState, reasoningChars: number, contentChars: number): string {
  if (state === 'preparing') return '正在准备硬件中立代码画像与当前全部能力卡。'
  if (state === 'structuring') return '正在校验模型响应，并将同次响应整理为结构化路由结论。'
  if (state === 'saving') return '正在校验路由结构、能力卡引用并写入审计工件。'
  if (state !== 'routing') return '等待下一次模型语义路由。'
  if (contentChars > 0) return '已收到结构化回答草稿，正在继续完成候选单元与证据引用。'
  if (reasoningChars > 0) return '模型正在比较可见代码、数据边界和当前全部能力卡；完成后会给出结构化结论。'
  return '已发送热点代码、硬件中立画像和当前全部能力卡，正在等待模型响应。'
}

function notifyRoutingProgress(input: {
  requestId: string
  startedAt: string
  state: RoutingState
  detail: string
  reasoningChars?: number
  contentChars?: number
  streamPreview?: string
  failedStage?: RoutingState
}) {
  if (typeof window === 'undefined') return
  const phase = input.state === 'failed'
    ? 'failed'
    : input.state === 'succeeded'
      ? 'completed'
      : input.state === 'saving'
        ? 'contract_validating'
        : input.state === 'structuring'
          ? 'response_received'
          : 'model_pending'
  const status = input.state === 'failed' ? 'failed' : input.state === 'succeeded' ? 'succeeded' : 'running'
  const effectiveState = input.state === 'failed' ? input.failedStage ?? 'preparing' : input.state
  const stepStatus = (runningStates: RoutingState[], succeededStates: RoutingState[]): 'pending' | 'running' | 'succeeded' | 'failed' => {
    if (input.state === 'failed' && runningStates.includes(effectiveState)) return 'failed'
    if (succeededStates.includes(effectiveState) || input.state === 'succeeded') return 'succeeded'
    if (runningStates.includes(effectiveState)) return 'running'
    return 'pending'
  }
  window.dispatchEvent(new CustomEvent('reasoning-lab-agent-analysis-progress', {
    detail: {
      schema_version: 'strategy_progress/v1',
      progress_kind: 'hardware_routing',
      request_id: input.requestId,
      strategy_id: 'hardware_routing',
      strategy_label: '硬件单元路由',
      phase,
      status,
      started_at: input.startedAt,
      headline: input.state === 'succeeded'
        ? '硬件单元路由已完成并存档'
        : input.state === 'failed'
          ? '硬件单元路由未完成'
          : stateLabel(input.state),
      detail: input.detail,
      steps: [
        { id: 'context', title: '准备硬件中立画像与能力卡', detail: '提供热点代码、中立画像、平台能力卡和已登记自建目录能力卡。', status: stepStatus(['preparing'], ['routing', 'structuring', 'saving', 'succeeded']) },
        { id: 'model', title: '模型语义路由', detail: input.streamPreview ? '正在展示模型实时生成的路由判断。' : '等待模型开始生成路由判断。', status: stepStatus(['routing'], ['structuring', 'saving', 'succeeded']) },
        { id: 'contract', title: '结构恢复与证据校验', detail: '校验候选单元、能力卡引用和结构契约。', status: stepStatus(['structuring'], ['saving', 'succeeded']) },
        { id: 'artifact', title: '保存路由工件', detail: '路由结果作为后续 API Top-N 的目录入口。', status: stepStatus(['saving'], ['succeeded']) },
      ],
      directions: [],
      streamPreview: input.streamPreview,
      streamedReasoningChars: input.reasoningChars ?? 0,
      streamedContentChars: input.contentChars ?? 0,
    },
  }))
}

function reasoningDigest(result: JsonRecord, candidates: JsonRecord[], cards: JsonRecord[] = []): Array<{ label: string; detail: string }> {
  const profile = record(result.semantic_profile)
  const labels = new Map(cards.map((card) => [stringValue(card.unit_id), stringValue(card.label_zh)]))
  const digest: Array<{ label: string; detail: string }> = []
  const data = renderLines(profile.data_representation, '')
  const operation = renderLines(profile.operation_pattern, '')
  const dependency = renderLines(profile.dependency_and_state, '')
  const boundaries = renderLines(profile.boundary_conditions, '')
  const uncertainty = renderLines(profile.uncertainty, '')
  if (data) digest.push({ label: '数据形态', detail: data })
  if (operation) digest.push({ label: '计算结构', detail: operation })
  if (dependency) digest.push({ label: '依赖与状态', detail: dependency })
  if (boundaries) digest.push({ label: '运行边界', detail: boundaries })
  if (candidates.length > 0) {
    digest.push({
      label: '候选单元',
      detail: candidates.map((candidate) => {
        const unitId = stringValue(candidate.unit_id)
        return `${labels.get(unitId) || unitLabel(unitId)}（${stringValue(candidate.confidence, 'low')}）：${stringValue(candidate.rationale, '待后续 API 与设备验证')}`
      }).join('；'),
    })
  }
  if (uncertainty) digest.push({ label: '仍需验证', detail: uncertainty })
  const negative = renderLines(result.negative_evidence, '')
  if (negative) digest.push({ label: '反证与排除项', detail: negative })
  return digest
}

const agentRouteOutputContract = {
  schema_version: 'hardware_semantic_routing/v2',
  decision: 'select_units | needs_compute_core | no_accelerator_candidate',
  summary: '中文总结',
  semantic_profile: {
    data_representation: ['只写可见事实'],
    operation_pattern: ['计算结构'],
    dependency_and_state: ['依赖与状态'],
    boundary_conditions: ['边界与未知项'],
    uncertainty: ['不能由当前代码确认的事项'],
  },
  candidates: [{
    unit_id: '必须逐字使用本轮 hardware_capability_cards 中已有的 unit_id',
    confidence: 'low | medium | high',
    analysis_scope: '候选代码范围',
    observed_code_evidence: ['代码事实'],
    rationale: '代码语义与能力卡的因果匹配',
    missing_evidence: ['待验证事项'],
    evidence_ids: ['只能使用 model_input_context.allowed_evidence_ids'],
  }],
  negative_evidence: ['反证或排除项'],
}

export function HardwareSemanticRoutingExperiment({
  codeUnit,
  restoredRoute,
  analysisRecordId,
  modelConfig,
  targetPlatformProfile,
  skillPrompt = '',
  skillProfileId,
  onRouteResolved,
  onRouteFailed,
  onParallelSelected,
  onSkip,
}: {
  codeUnit: Artifact | null
  restoredRoute?: { codeArtifactId: string; resultArtifact: Artifact } | null
  analysisRecordId?: string | null
  modelConfig: BrowserModelConfig
  targetPlatformProfile: TargetPlatformProfile
  skillPrompt?: string
  skillProfileId?: string
  onRouteResolved?: (route: { requestId?: string; codeArtifactId: string; resultArtifact: Artifact }) => void
  onRouteFailed?: (route: { requestId?: string; codeArtifactId: string; message: string }) => void
  onParallelSelected?: () => Promise<void>
  onSkip?: () => void
}) {
  const [state, setState] = useState<RoutingState>('idle')
  const routingAbortRef = useRef<AbortController | null>(null)
  const [context, setContext] = useState<Artifact | null>(null)
  const [resultArtifact, setResultArtifact] = useState<Artifact | null>(null)
  const [formatRecovery, setFormatRecovery] = useState('')
  const [error, setError] = useState('')
  const [tokenUsage, setTokenUsage] = useState<ModelTokenUsage | null>(null)
  const [tokenBudget, setTokenBudget] = useState<PromptTokenBudget | null>(null)
  const [manualUnitId, setManualUnitId] = useState('')
  const [manualNote, setManualNote] = useState('')
  const [manualSelectionBusy, setManualSelectionBusy] = useState(false)
  const [directSelectionOpen, setDirectSelectionOpen] = useState(false)
  const [directSelectionUnit, setDirectSelectionUnit] = useState('gpu')
  const [directSelectionBusy, setDirectSelectionBusy] = useState(false)
  const [directCatalogBusy, setDirectCatalogBusy] = useState(false)
  const [directCustomCards, setDirectCustomCards] = useState<JsonRecord[]>([])
  const directCatalogRequestRef = useRef<Promise<void> | null>(null)
  const directSelectionRequestRef = useRef(false)
  const [routeCommandSubmission, setRouteCommandSubmission] = useState<{ requestId: string; accepted: boolean } | null>(null)
  const routeCommandRequestRef = useRef('')
  const routeCommandTimeoutRef = useRef<number | null>(null)

  const modelInput = record(context?.model_input_context)
  const cards = values(modelInput.hardware_capability_cards)
  const genericProfile = record(modelInput.generic_code_profile)
  const result = record(resultArtifact?.result)
  const semanticProfile = record(result.semantic_profile)
  const validation = record(result.validation)
  const candidates = values(result.candidates)
  const routeDigest = reasoningDigest(result, candidates, cards)
  const selectionOrigin = stringValue(result.selection_origin, 'model')
  const sourceKey = codeUnit?.artifact_id ?? ''
  const directRouteCards = [
    ...cards.filter((card) => stringValue(card.unit_id).startsWith('custom:')),
    ...directCustomCards,
  ].filter((card, index, all) => {
    const unitId = stringValue(card.unit_id)
    return unitId.startsWith('custom:') && all.findIndex((item) => stringValue(item.unit_id) === unitId) === index
  })

  const clearRouteCommandSubmission = useCallback(() => {
    routeCommandRequestRef.current = ''
    if (routeCommandTimeoutRef.current !== null) {
      window.clearTimeout(routeCommandTimeoutRef.current)
      routeCommandTimeoutRef.current = null
    }
    setRouteCommandSubmission(null)
  }, [])

  const submitRouteCommand = useCallback(() => {
    if (!codeUnit || routeCommandRequestRef.current) return
    const requestId = randomUuid()
    routeCommandRequestRef.current = requestId
    setRouteCommandSubmission({ requestId, accepted: false })
    setError('')
    routeCommandTimeoutRef.current = window.setTimeout(() => {
      if (routeCommandRequestRef.current !== requestId) return
      clearRouteCommandSubmission()
      setError('页面助手在 60 秒内尚未开始硬件路由，按钮已解锁；请查看右侧 Agent 状态后重试。')
    }, 60_000)
    dispatchAgentCommand({
      requestId,
      analysisId: analysisRecordId ?? undefined,
      displayMessage: '运行硬件单元路由',
      message: '执行硬件单元路由：直接调用平台 MCP 工具 reasoning_prepare_hardware_semantic_route 获取当前热点的中立代码画像、平台能力卡、自建目录能力卡和输出契约；比较全部能力卡后，调用 reasoning_run_hardware_semantic_route，传回原始 context_artifact_id 和符合契约的 agent_result。无需搜索平台源码或页面 Action ID。',
    })
  }, [analysisRecordId, clearRouteCommandSubmission, codeUnit])

  useEffect(() => {
    const listener = (event: Event) => {
      const detail = record((event as CustomEvent<unknown>).detail)
      const requestId = stringValue(detail.request_id)
      if (!requestId || requestId !== routeCommandRequestRef.current) return
      if (stringValue(detail.status) === 'rejected') {
        clearRouteCommandSubmission()
        setError(stringValue(detail.message, '页面助手未接收本次硬件路由命令。'))
        return
      }
      setRouteCommandSubmission({ requestId, accepted: true })
    }
    window.addEventListener(agentCommandStatusEventName, listener)
    return () => window.removeEventListener(agentCommandStatusEventName, listener)
  }, [clearRouteCommandSubmission])

  useEffect(() => {
    if (!routeCommandRequestRef.current) return
    if (state === 'preparing' || state === 'routing' || state === 'structuring' || state === 'saving') {
      clearRouteCommandSubmission()
    }
  }, [clearRouteCommandSubmission, state])

  useEffect(() => () => {
    if (routeCommandTimeoutRef.current !== null) window.clearTimeout(routeCommandTimeoutRef.current)
  }, [])

  useEffect(() => {
    routingAbortRef.current?.abort()
    clearRouteCommandSubmission()
    setState('idle')
    setContext(null)
    setResultArtifact(null)
    setFormatRecovery('')
    setError('')
    setTokenUsage(null)
    setTokenBudget(null)
    setManualUnitId('')
    setManualNote('')
    setManualSelectionBusy(false)
    setDirectSelectionOpen(false)
    setDirectSelectionUnit('gpu')
    setDirectSelectionBusy(false)
  }, [clearRouteCommandSubmission, sourceKey])

  useEffect(() => {
    if (!sourceKey || restoredRoute?.codeArtifactId !== sourceKey) return
    if (resultArtifact?.artifact_id === restoredRoute.resultArtifact.artifact_id) return
    let cancelled = false
    const contextArtifactId = stringValue(restoredRoute.resultArtifact.context_artifact_id, '')
    if (!contextArtifactId) {
      setError('历史硬件路由结果缺少不可变上下文关联。')
      return
    }
    setState('preparing')
    void api<{ artifact: Artifact }>(`/v1/projects/local-default/hardware-semantic-routing/${encodeURIComponent(contextArtifactId)}`)
      .then((response) => {
        if (cancelled) return
        setContext(response.artifact)
        setResultArtifact(restoredRoute.resultArtifact)
        setState('succeeded')
        setError('')
        const candidates = values(record(restoredRoute.resultArtifact.result).candidates)
        setManualUnitId(stringValue(candidates[0]?.unit_id, ''))
      })
      .catch((reason) => {
        if (cancelled) return
        setState('failed')
        setError(reason instanceof Error ? reason.message : '无法读取历史硬件路由上下文。')
      })
    return () => { cancelled = true }
  }, [restoredRoute, resultArtifact?.artifact_id, sourceKey])

  const loadDirectCustomCards = useCallback((): Promise<void> => {
    if (directCatalogRequestRef.current) return directCatalogRequestRef.current
    setDirectCatalogBusy(true)
    const request = loadCustomApiCatalog<{ knowledge_bases: JsonRecord[] }>()
      .then((catalog) => {
        setDirectCustomCards((catalog.knowledge_bases ?? []).map((item) => ({
          unit_id: `custom:${stringValue(item.kb_id)}`,
          label_zh: stringValue(item.display_name, `${stringValue(item.accelerator_unit, '自建硬件')} API（自建）`),
        })).filter((item) => /^custom:custom_api_kb_[a-f0-9]{24}$/.test(stringValue(item.unit_id))))
      })
    directCatalogRequestRef.current = request
    const clear = () => {
      if (directCatalogRequestRef.current === request) directCatalogRequestRef.current = null
      setDirectCatalogBusy(false)
    }
    request.then(clear, clear)
    return request
  }, [])

  const toggleDirectSelection = useCallback(() => {
    if (directSelectionOpen) {
      setDirectSelectionOpen(false)
      return
    }
    setDirectSelectionOpen(true)
    void loadDirectCustomCards().catch((catalogError) => {
      setError(catalogError instanceof Error ? catalogError.message : '自建 API 目录加载失败。')
    })
  }, [directSelectionOpen, loadDirectCustomCards])

  useEffect(() => {
    if (!resultArtifact) return
    const modelUnit = stringValue(candidates[0]?.unit_id, '')
    setManualUnitId((current) => current || modelUnit || stringValue(cards[0]?.unit_id, ''))
  }, [cards, candidates, resultArtifact])

  const prepareForAgent = useCallback(async (requestId?: string) => {
    if (!codeUnit) {
      rejectReasoningLabAction(requestId, '请先从热点关联代码，或在上方粘贴待分析代码。')
      return
    }
    setState('preparing')
    setError('')
    try {
      const prepared = await api<RoutingContextResponse>('/v1/projects/local-default/hardware-semantic-routing/prepare', {
        method: 'POST',
        body: JSON.stringify({
          code_artifact_id: codeUnit.artifact_id,
          skill_profile_id: skillProfileId,
          target_platform_profile: targetPlatformProfile,
        }),
      })
      setContext(prepared.artifact)
      setState('idle')
      respondReasoningLabAction(requestId, {
        status: 'succeeded',
        code_artifact_id: codeUnit.artifact_id,
        context_artifact_id: prepared.artifact.artifact_id,
        required_schema: 'hardware_semantic_routing/v2',
        output_contract: agentRouteOutputContract,
        model_input_context: record(prepared.artifact.model_input_context),
      })
    } catch (prepareError) {
      const message = prepareError instanceof Error ? prepareError.message : '硬件路由上下文准备失败。'
      setState('failed')
      setError(message)
      rejectReasoningLabAction(requestId, message)
    }
  }, [codeUnit, skillProfileId, targetPlatformProfile])

  const acceptAgentRoute = useCallback(async (requestId: string | undefined, contextArtifactId: string, agentResult: JsonRecord) => {
    if (!codeUnit) {
      rejectReasoningLabAction(requestId, '当前没有可回填的热点代码。')
      return
    }
    if (!contextArtifactId || Object.keys(agentResult).length === 0) {
      rejectReasoningLabAction(requestId, '需要先准备路由上下文，再提交结构化 agent_result。')
      return
    }
    setState('saving')
    setError('')
    try {
      const prepared = await api<{ artifact: Artifact }>(`/v1/projects/local-default/hardware-semantic-routing/${encodeURIComponent(contextArtifactId)}`)
      if (stringValue(prepared.artifact.code_artifact_id) !== codeUnit.artifact_id) {
        throw new Error('路由上下文与当前热点代码不一致，请重新准备。')
      }
      setContext(prepared.artifact)
      const rawOutput = JSON.stringify(agentResult)
      const saved = await api<RoutingSaveResponse>('/v1/projects/local-default/hardware-semantic-routing/results', {
        method: 'POST',
        body: JSON.stringify({
          context_artifact_id: contextArtifactId,
          model_provider: 'kimi_agent_session',
          model_name: modelConfig.model || 'engine-default',
          raw_output: rawOutput,
          result: agentResult,
        }),
      })
      setResultArtifact(saved.artifact)
      setState('succeeded')
      setFormatRecovery('路由结果已通过结构与证据引用校验并完成工件回填。')
      onRouteResolved?.({ requestId, codeArtifactId: codeUnit.artifact_id, resultArtifact: saved.artifact })
      const normalized = record(saved.artifact.result)
      respondReasoningLabAction(requestId, {
        status: 'succeeded',
        routing_result_artifact_id: saved.artifact.artifact_id,
        decision: stringValue(normalized.decision),
        summary: stringValue(normalized.summary),
        selected_units: values(normalized.candidates).slice(0, 2),
        validation_errors: saved.validation_errors,
      })
    } catch (saveError) {
      const message = saveError instanceof Error ? saveError.message : 'Agent 路由结果未能回填。'
      setState('failed')
      setError(message)
      onRouteFailed?.({ requestId, codeArtifactId: codeUnit.artifact_id, message })
      rejectReasoningLabAction(requestId, message)
    }
  }, [codeUnit, modelConfig.model, onRouteFailed, onRouteResolved])

  const run = useCallback(async (requestId?: string, rerouteReason = '') => {
    if (!codeUnit) {
      const message = '请先从热点关联代码，或在上方粘贴待分析代码。'
      setError(message)
      onRouteFailed?.({ requestId, codeArtifactId: '', message })
      return
    }
    const activeRequestId = requestId || randomUuid()
    routingAbortRef.current?.abort()
    const abortController = new AbortController()
    routingAbortRef.current = abortController
    const startedAt = new Date().toISOString()
    let latestReasoningChars = 0
    let latestContentChars = 0
    let latestReasoningPreview = ''
    let latestContentPreview = ''
    let activeRoutingState: RoutingState = 'preparing'
    setState('preparing')
    setContext(null)
    setResultArtifact(null)
    setFormatRecovery('')
    setError('')
    setTokenUsage(null)
    setTokenBudget(null)
    setManualUnitId('')
    setManualNote('')
    notifyRoutingProgress({
      requestId: activeRequestId,
      startedAt,
      state: 'preparing',
      detail: '正在由后端准备硬件中立代码画像、允许证据 ID 与当前全部能力卡。',
    })
    await appendAnalysisTraceEvent(analysisRecordId, {
      category: 'page_action',
      operation: 'hardware_semantic_routing.prepare',
      phase: 'hardware_routing',
      status: 'started',
      requestId: activeRequestId,
      payload: { code_artifact_id: codeUnit.artifact_id, target_platform_profile: targetPlatformProfile },
    })
    try {
      const prepared = await api<RoutingContextResponse>('/v1/projects/local-default/hardware-semantic-routing/prepare', {
        method: 'POST',
        body: JSON.stringify({ code_artifact_id: codeUnit.artifact_id, skill_profile_id: skillProfileId, target_platform_profile: targetPlatformProfile }),
      })
      setContext(prepared.artifact)
      await appendAnalysisTraceEvent(analysisRecordId, {
        category: 'tool_result',
        operation: 'hardware_semantic_routing.prepare',
        phase: 'hardware_routing',
        status: 'succeeded',
        requestId: activeRequestId,
        payload: { context_artifact: prepared.artifact },
      })
      const plan = buildHardwareSemanticRoutingPromptPlan(
        prepared.artifact,
        1024 * 1024,
        64 * 1024,
        [
          skillPrompt,
          rerouteReason.trim()
            ? `# 本轮重新路由事实\n上一阶段请求重新路由，原因如下：${rerouteReason.trim().slice(0, 800)}\n请重新比较本轮全部能力卡；该原因是待核查观察，不是新硬件已被证明的结论。`
            : '',
        ].filter(Boolean).join('\n\n'),
      )
      setTokenBudget(plan.tokenBudget)
      const declaredEvidenceIds = record(prepared.artifact.model_input_context).allowed_evidence_ids
      const allowedEvidenceIds = Array.isArray(declaredEvidenceIds)
        ? declaredEvidenceIds.filter((value): value is string => typeof value === 'string')
        : []
      const routingContractHint = [
        '必须包含 decision、summary、semantic_profile、candidates 和 negative_evidence。',
        'candidates 最多两个，每项含 unit_id、confidence、analysis_scope、observed_code_evidence、rationale、missing_evidence、evidence_ids。',
        `evidence_ids 只能使用本轮允许的值：${allowedEvidenceIds.join(', ') || '（本轮未声明，使用空数组）'}。`,
      ].join(' ')
      activeRoutingState = 'routing'
      setState('routing')
      notifyRoutingProgress({
        requestId: activeRequestId,
        startedAt,
        state: 'routing',
        detail: '模型正在根据代码语义和能力卡选择最多两个待验证硬件单元。',
      })
      const completion = await requestModelChatCompletionStream(modelConfig, plan.messages, {
        maxTokens: plan.tokenBudget.outputReserveTokens,
        signal: abortController.signal,
        observability: {
          analysisId: analysisRecordId ?? '',
          operation: 'hardware_semantic_routing.model',
          requestId: activeRequestId,
          runId: activeRequestId,
        },
        onProgress: (progress: ModelStreamProgress) => {
          latestReasoningChars = progress.reasoningChars
          latestContentChars = progress.contentChars
          latestReasoningPreview = `${latestReasoningPreview}${progress.reasoningDelta}`.slice(-1600)
          latestContentPreview = `${latestContentPreview}${progress.contentDelta}`.slice(-1600)
          const latestStreamPreview = [
            latestReasoningPreview ? `[思考]\n${latestReasoningPreview}` : '',
            latestContentPreview ? `[输出]\n${latestContentPreview}` : '',
          ].filter(Boolean).join('\n\n')
          notifyRoutingProgress({
            requestId: activeRequestId,
            startedAt,
            state: 'routing',
            detail: routingProgressText('routing', progress.reasoningChars, progress.contentChars),
            reasoningChars: progress.reasoningChars,
            contentChars: progress.contentChars,
            streamPreview: latestStreamPreview,
          })
        },
      })
      const completedOutput = completion.choices?.[0]?.message?.content ?? ''
      activeRoutingState = 'structuring'
      setState('structuring')
      notifyRoutingProgress({
        requestId: activeRequestId,
        startedAt,
        state: 'structuring',
        detail: '模型文本已收到，正在恢复并校验 hardware_semantic_routing/v2 结构。',
        reasoningChars: latestReasoningChars,
        contentChars: latestContentChars,
      })
      const structured = await parseOrRepairModelJson({
        config: modelConfig,
        rawOutput: completedOutput,
        reasoningOutput: completion.reasoningContent,
        schemaName: 'hardware_semantic_routing/v2',
        contractHint: routingContractHint,
        maxTokens: plan.tokenBudget.outputReserveTokens,
        signal: abortController.signal,
        requireChineseSummary: true,
        observability: {
          analysisId: analysisRecordId ?? '',
          operation: 'hardware_semantic_routing.model',
          runId: activeRequestId,
        },
      })
      setFormatRecovery(structured.recovered
        ? structured.recoverySource === 'reasoning_channel'
          ? '上游模型只在思考流表达了候选判断，未写出最终 JSON；平台已基于同次响应完成一次不新增事实的结构恢复，并会继续进行后端证据校验。'
          : '上游模型没有直接返回可用 JSON；平台已完成一次不新增事实的结构修复，并会继续进行后端证据校验。'
        : '')
      const combinedUsage = mergeModelTokenUsage(completion.tokenUsage, structured.repairUsage)
      setTokenUsage(combinedUsage)
      window.dispatchEvent(new CustomEvent('reasoning-agent-extra-token-usage', { detail: { token_usage: combinedUsage, token_budget: plan.tokenBudget } }))
      activeRoutingState = 'saving'
      setState('saving')
      notifyRoutingProgress({
        requestId: activeRequestId,
        startedAt,
        state: 'saving',
        detail: '结构化路由已形成，正在执行后端证据引用校验并保存审计工件。',
        reasoningChars: latestReasoningChars,
        contentChars: latestContentChars,
      })
      const saved = await api<RoutingSaveResponse>('/v1/projects/local-default/hardware-semantic-routing/results', {
        method: 'POST',
        body: JSON.stringify({
          context_artifact_id: prepared.artifact.artifact_id,
          model_provider: 'kimi_server_agent_engine',
          model_name: modelConfig.model,
          raw_output: structured.auditOutput,
          result: structured.parsed,
        }),
      })
      setResultArtifact(saved.artifact)
      activeRoutingState = 'succeeded'
      setState('succeeded')
      await appendAnalysisTraceEvent(analysisRecordId, {
        category: 'contract',
        operation: 'hardware_semantic_routing.persist',
        phase: 'hardware_routing',
        status: 'succeeded',
        requestId: activeRequestId,
        payload: {
          result_artifact: saved.artifact,
          validation_errors: saved.validation_errors,
          token_usage: combinedUsage,
          token_budget: plan.tokenBudget,
        },
      })
      notifyRoutingProgress({
        requestId: activeRequestId,
        startedAt,
        state: 'succeeded',
        detail: '路由结果已完成结构与证据校验，并作为后续 API 目录入口保存。',
        reasoningChars: latestReasoningChars,
        contentChars: latestContentChars,
      })
      onRouteResolved?.({ requestId: activeRequestId, codeArtifactId: codeUnit.artifact_id, resultArtifact: saved.artifact })
    } catch (runError) {
      setState('failed')
      const stopped = abortController.signal.aborted || (runError instanceof DOMException && runError.name === 'AbortError')
      const message = stopped ? '硬件单元路由已由用户停止。' : runError instanceof Error ? runError.message : '模型语义路由失败。'
      setError(message)
      await appendAnalysisTraceEvent(analysisRecordId, {
        category: 'model_error',
        operation: 'hardware_semantic_routing',
        phase: 'hardware_routing',
        status: 'failed',
        requestId: activeRequestId,
        payload: analysisErrorPayload(runError),
      })
      notifyRoutingProgress({ requestId: activeRequestId, startedAt, state: 'failed', failedStage: activeRoutingState, detail: message })
      onRouteFailed?.({ requestId: activeRequestId, codeArtifactId: codeUnit.artifact_id, message })
    } finally {
      if (routingAbortRef.current === abortController) routingAbortRef.current = null
    }
  }, [analysisRecordId, codeUnit, modelConfig, onRouteFailed, onRouteResolved, skillProfileId, skillPrompt, targetPlatformProfile])

  const stopRouting = useCallback(() => {
    routingAbortRef.current?.abort(new DOMException('硬件单元路由已由用户停止。', 'AbortError'))
  }, [])

  const applyManualSelection = useCallback(async () => {
    if (!codeUnit || !resultArtifact || !manualUnitId) {
      setError('请先完成一次模型语义路由，再选择需要人工复核的硬件目录。')
      return
    }
    setManualSelectionBusy(true)
    setError('')
    try {
      const saved = await api<RoutingManualSelectionResponse>('/v1/projects/local-default/hardware-semantic-routing/manual-selection', {
        method: 'POST',
        body: JSON.stringify({
          result_artifact_id: resultArtifact.artifact_id,
          unit_id: manualUnitId,
          note: manualNote.trim(),
        }),
      })
      setResultArtifact(saved.artifact)
      setFormatRecovery('用户已指定硬件目录；该选择只用于打开该目录的 API 复核，不构成模型或设备能力证据。')
      onRouteResolved?.({ codeArtifactId: codeUnit.artifact_id, resultArtifact: saved.artifact })
    } catch (selectionError) {
      setError(selectionError instanceof Error ? selectionError.message : '人工硬件目录选择失败。')
    } finally {
      setManualSelectionBusy(false)
    }
  }, [codeUnit, manualNote, manualUnitId, onRouteResolved, resultArtifact])

  const applyDirectSelection = useCallback(async () => {
    if (directSelectionRequestRef.current) return
    if (!codeUnit) {
      setError('请先关联或粘贴热点代码。')
      return
    }
    directSelectionRequestRef.current = true
    setDirectSelectionBusy(true)
    setError('')
    try {
      if (directSelectionUnit === 'parallel_cpu') {
        if (!onParallelSelected) throw new Error('当前页面没有注册并行优化快捷入口。')
        await onParallelSelected()
        setFormatRecovery('已将“并行优化”保存为用户直接选择的待验证方向，并跳过硬件机会推理。')
      } else {
        const saved = await api<RoutingDirectSelectionResponse>('/v1/projects/local-default/hardware-semantic-routing/direct-selection', {
          method: 'POST',
          body: JSON.stringify({
            code_artifact_id: codeUnit.artifact_id,
            unit_id: directSelectionUnit,
            note: '用户在路由阶段直接选择硬件目录。',
            target_platform_profile: targetPlatformProfile,
          }),
        })
        setContext(saved.context_artifact)
        setResultArtifact(saved.artifact)
        setState('succeeded')
        setFormatRecovery('已直接采用用户选择的硬件目录；未调用模型，也未将该选择写成设备能力证据。')
        onRouteResolved?.({ codeArtifactId: codeUnit.artifact_id, resultArtifact: saved.artifact })
      }
      setDirectSelectionOpen(false)
    } catch (selectionError) {
      setError(selectionError instanceof Error ? selectionError.message : '直接选择路由失败。')
    } finally {
      directSelectionRequestRef.current = false
      setDirectSelectionBusy(false)
    }
  }, [codeUnit, directSelectionUnit, onParallelSelected, onRouteResolved, targetPlatformProfile])

  useEffect(() => {
    const listener = (event: Event) => {
      const detail = record((event as CustomEvent<unknown>).detail)
      const requestId = stringValue(detail.request_id, '') || undefined
      const agentResult = record(detail.agent_result)
      const contextArtifactId = stringValue(detail.context_artifact_id, '')
      if (contextArtifactId && Object.keys(agentResult).length > 0) {
        void acceptAgentRoute(requestId, contextArtifactId, agentResult)
        return
      }
      void run(requestId, stringValue(detail.reason, ''))
    }
    window.addEventListener('reasoning-lab-run-hardware-semantic-routing', listener)
    return () => window.removeEventListener('reasoning-lab-run-hardware-semantic-routing', listener)
  }, [acceptAgentRoute, run])

  useEffect(() => {
    const listener = (event: Event) => {
      const detail = record((event as CustomEvent<unknown>).detail)
      void prepareForAgent(stringValue(detail.request_id, '') || undefined)
    }
    window.addEventListener('reasoning-lab-prepare-hardware-semantic-routing', listener)
    return () => window.removeEventListener('reasoning-lab-prepare-hardware-semantic-routing', listener)
  }, [prepareForAgent])

  useEffect(() => {
    const listener = () => stopRouting()
    window.addEventListener('page-agent-stop-current-inference', listener)
    return () => {
      window.removeEventListener('page-agent-stop-current-inference', listener)
      stopRouting()
    }
  }, [stopRouting])

  return <div className="hardware-semantic-routing-experiment">
    <div className="hardware-semantic-routing-heading">
      <div>
        <span className="eyebrow">第一阶段</span>
        <h3>模型语义硬件路由</h3>
        <p>直接向模型提供热点代码、硬件中立静态画像、平台能力卡和已登记自建目录能力卡，先判断最可能的单元，再进入该目录的 API Top-N 精核。</p>
      </div>
      <div className="hardware-semantic-routing-actions">
        <span className="hardware-semantic-routing-state ready">Agent Engine 已就绪</span>
        <button className="primary-button compact" type="button" onClick={submitRouteCommand} disabled={!codeUnit || Boolean(routeCommandSubmission) || state === 'preparing' || state === 'routing' || state === 'structuring' || state === 'saving'} title="运行硬件单元路由">
          {routeCommandSubmission || state === 'preparing' || state === 'routing' || state === 'structuring' || state === 'saving' ? <LoaderCircle size={14} className="spin" /> : <WandSparkles size={14} />}
          {routeCommandSubmission ? routeCommandSubmission.accepted ? '已提交，等待路由' : '正在提交' : state === 'idle' || state === 'succeeded' || state === 'failed' ? '运行语义路由' : stateLabel(state)}
        </button>
        {(state === 'preparing' || state === 'routing' || state === 'structuring' || state === 'saving') && <button className="quiet-button compact" type="button" onClick={stopRouting} title="停止当前硬件单元路由"><Square size={13} />停止</button>}
        <button className="quiet-button compact" type="button" onClick={toggleDirectSelection} disabled={!codeUnit || Boolean(routeCommandSubmission) || directSelectionBusy || state === 'preparing' || state === 'routing' || state === 'structuring' || state === 'saving'} title="不调用模型，直接选择硬件目录或 CPU 并行方向"><ListChecks size={14} />直接选择路由</button>
        <button className="quiet-button compact" type="button" onClick={onSkip} disabled={!codeUnit || Boolean(routeCommandSubmission) || directSelectionBusy || state === 'preparing' || state === 'routing' || state === 'structuring' || state === 'saving'} title="跳过硬件路由且不创建优化方向">跳过路由</button>
      </div>
    </div>
    {directSelectionOpen && <div className="hardware-direct-route-picker table-shell">
      <div><ListChecks size={15} /><span><strong>直接选择路由</strong><small>硬件单元进入“硬件机会推理”；并行优化直接进入真机验证。所有选择都会保存为用户选择，不伪装成模型结论。</small></span></div>
      <label><span>路由方向</span><select value={directSelectionUnit} onChange={(event) => setDirectSelectionUnit(event.target.value)} disabled={directSelectionBusy}>
        <option value="npu">NPU / NNRT</option>
        <option value="gpu">GPU</option>
        <option value="dsp">DSP 音频</option>
        <option value="media">媒体硬件编解码</option>
        {directRouteCards.map((card) => <option value={stringValue(card.unit_id)} key={stringValue(card.unit_id)}>{stringValue(card.label_zh, unitLabel(stringValue(card.unit_id)))}</option>)}
        <option value="parallel_cpu">并行优化</option>
      </select>{directCatalogBusy && <small className="hardware-direct-route-loading"><LoaderCircle size={13} className="spin" />正在载入自建硬件目录…</small>}</label>
      <button className="primary-button compact" type="button" disabled={directSelectionBusy} onClick={() => void applyDirectSelection()}>{directSelectionBusy ? <LoaderCircle size={14} className="spin" /> : <Check size={14} />}确认选择</button>
    </div>}
    <div className="hardware-semantic-routing-note"><FlaskConical size={15} /><span>生产链不执行规则路由：模型读取热点代码、硬件中立画像、平台能力卡和自建目录能力卡。自建目录保持独立，本结果仍不会直接声明 API 或设备可用。</span></div>
    {!codeUnit && <div className="hardware-semantic-routing-empty">先在“热点代码”中关联或粘贴代码，才能运行语义路由。</div>}
    {error && <div className="hardware-semantic-routing-feedback error"><ShieldAlert size={15} /><span>{error}</span></div>}
    {formatRecovery && <div className="hardware-semantic-routing-feedback"><Check size={15} /><span>{formatRecovery}</span></div>}
    {context && <>
      <div className="hardware-semantic-routing-summary">
        <div><span>输入隔离</span><strong>不含规则路由、API 目录、trace 名、模块名、源码路径或额外函数提示</strong></div>
        <div><span>模型状态</span><strong>{stateLabel(state)}</strong></div>
        <div><span>估算输入</span><strong>{tokenBudget ? formatTokenCount(tokenBudget.inputTokens) : '—'}</strong></div>
        <div><span>模型用量</span><strong>{tokenUsage ? `输入 ${formatTokenCount(tokenUsage.inputTokens)} · 输出 ${formatTokenCount(tokenUsage.outputTokens)}` : '等待响应'}</strong></div>
      </div>
      <div className="hardware-capability-card-grid">
        {cards.map((card) => <article className="hardware-capability-card" key={stringValue(card.unit_id)}>
          <header><Cpu size={15} /><strong>{stringValue(card.label_zh, unitLabel(stringValue(card.unit_id)))}</strong></header>
          <p>{stringValue(card.one_line_abstract_zh)}</p>
          <small>适配信号：{renderLines(card.fit_signals_zh)}</small>
          <small>边界：{renderLines(card.boundaries_zh)}</small>
        </article>)}
      </div>
      <details className="hardware-semantic-routing-profile"><summary>查看模型可见的硬件中立代码画像</summary><pre>{JSON.stringify(genericProfile, null, 2)}</pre></details>
    </>}
    {resultArtifact && <div className="hardware-semantic-routing-result">
      <div className="hardware-semantic-routing-result-heading"><Check size={16} /><div><span>{selectionOrigin === 'user_manual' ? '人工指定的路由目录' : '模型路由结论'}</span><strong>{routeDecisionLabel(stringValue(result.decision))}</strong></div><small>{stringValue(validation.status) === 'valid' ? '结构与证据引用已校验' : '存在结构校验问题'}</small></div>
      <p>{stringValue(result.summary, '模型没有提供可展示的摘要。')}</p>
      {routeDigest.length > 0 && <section className="hardware-semantic-routing-digest" aria-label="推理摘要">
        <header><BrainCircuit size={15} /><strong>推理摘要</strong><span>基于已校验的结构化结论生成；原始生成流统一保留在页面助手的实时分析进度中。</span></header>
        <dl>{routeDigest.map((item) => <div key={item.label}><dt>{item.label}</dt><dd>{item.detail}</dd></div>)}</dl>
      </section>}
      {Object.values(semanticProfile).some((value) => Array.isArray(value) && value.length > 0) && <details className="hardware-semantic-routing-profile" open><summary>模型形成的语义画像</summary><pre>{JSON.stringify(semanticProfile, null, 2)}</pre></details>}
      {candidates.length > 0 ? <div className="hardware-semantic-route-candidates">{candidates.map((candidate) => <article key={stringValue(candidate.unit_id)}>
        <header><strong>{stringValue(cards.find((card) => stringValue(card.unit_id) === stringValue(candidate.unit_id))?.label_zh, unitLabel(stringValue(candidate.unit_id)))}</strong><span>{stringValue(candidate.confidence, 'low')}</span></header>
        <p>{stringValue(candidate.rationale)}</p>
        <small>作用域：{stringValue(candidate.analysis_scope, '未声明')}</small>
        <small>代码依据：{renderLines(candidate.observed_code_evidence, '未列出')}</small>
        <small>待验证：{renderLines(candidate.missing_evidence, '仍需 API、设备与端到端验证')}</small>
      </article>)}</div> : <div className="hardware-semantic-routing-empty">{stringValue(result.decision) === 'needs_compute_core' ? '模型认为需要补充被调函数或更小的计算核心后再路由。' : '模型没有给出加速器候选。'}</div>}
      <div className="hardware-semantic-routing-manual">
        <div><UserRoundCheck size={15} /><strong>人工复核硬件目录</strong><span>模型路由完成后，可将任一目录作为后续 API 精核入口；这不会被写成设备能力或模型代码证据。</span></div>
        <label><span>硬件目录</span><select value={manualUnitId} onChange={(event) => setManualUnitId(event.target.value)} disabled={manualSelectionBusy}>{cards.map((card) => <option key={stringValue(card.unit_id)} value={stringValue(card.unit_id)}>{stringValue(card.label_zh, unitLabel(stringValue(card.unit_id)))}</option>)}</select></label>
        <label><span>复核备注（可选）</span><input value={manualNote} maxLength={600} onChange={(event) => setManualNote(event.target.value)} placeholder="例如：希望核对 DSP API 是否存在可复用路径" disabled={manualSelectionBusy} /></label>
        <button className="quiet-button" type="button" disabled={!manualUnitId || manualSelectionBusy} onClick={() => void applyManualSelection()}>{manualSelectionBusy ? <LoaderCircle size={14} className="spin" /> : <UserRoundCheck size={14} />}{selectionOrigin === 'user_manual' ? '更新人工选择' : '采用人工选择'}</button>
      </div>
      {Array.isArray(validation.errors) && validation.errors.length > 0 && <div className="hardware-semantic-routing-feedback error"><ShieldAlert size={15} /><span>{renderLines(validation.errors)}</span></div>}
      <details><summary>查看路由审计信息</summary><pre>{JSON.stringify({ context_artifact_id: context?.artifact_id, result_artifact_id: resultArtifact.artifact_id, model_input_constraints: modelInput.constraints, normalized_result: result }, null, 2)}</pre></details>
    </div>}
  </div>
}
