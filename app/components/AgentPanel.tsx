'use client'

import { CheckCircle2, ChevronDown, Circle, CircleAlert, Download, Eye, EyeOff, KeyRound, LoaderCircle, MonitorSmartphone, Paperclip, RefreshCw, Settings2, Square, Trash2, Upload, Wifi } from 'lucide-react'
import { ChangeEvent, DragEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { isRetryableModelError, requestModelChatCompletion, requestModelChatCompletionStream, type ModelRetryProgress, type ModelTokenUsage } from '../lib/model-client'
import { analysisErrorPayload, appendAnalysisTraceEvent } from '../lib/analysis-observability'
import { agentCommandEventName, reportAgentAutonomousTaskStatus, reportAgentCommandStatus } from '../lib/agent-command-bridge'
import { assertAgentTaskSubmissionActive, createAgentSubmissionGuard, waitForAgentTarget } from '../lib/agent-task-submission'
import { clearAnalysisRouteId, normalizeAnalysisRouteId, readAnalysisRouteId, writeAnalysisRouteId } from '../lib/analysis-route'
import { ApiError, apiBase } from '../lib/api'
import { ArtifactDeliveryNotifier, artifactDeliveryFromToolResult } from '../lib/artifact-delivery-notifications'
import { applyProviderPreset, BrowserModelConfig, clearBrowserModelConfig, DEFAULT_AGENT_MAX_OUTPUT_TOKENS, modelConfigChangedEvent, modelProviderPresets, normalizeAgentExecutionPolicy, normalizeAgentMaxActionsPerTurn, normalizeAgentMaxOutputTokens, normalizeAgentMaxTurns, providerPresetId, readBrowserModelConfig, writeBrowserModelConfig } from '../lib/model-config'
import { assistantPageContext } from '../lib/model-context'
import { captureSelectionView, dispatchCaptureDeviceCommand, scenarioCaptureRuntimeBinding } from '../lib/scenario-capture-device'
import { randomUuid } from '../lib/random-id'
import { applySkillPrompt, compactSkillProfile, type SkillVersionProfile } from '../lib/skill-version-library'
import {
  cancelBackendAgentRun,
  bindBackendAgentRunToAnalysis,
  clearBackendAgentEngineCredential,
  configureBackendAgentEngine,
  createBackendAgentRun,
  getAnalysisAgentSession,
  getBackendAgentEngineHealth,
  getBackendAgentRun,
  listBackendDeviceBridges,
  resumeBackendAgentRun,
  submitBackendAgentMessage,
  submitBackendUiActionResult,
  subscribeBackendAgentRun,
  type BackendAgentRun,
  type BackendSkillBootstrap,
  type BackendAgentEngineState,
  type BackendDeviceBridge,
  type BackendAgentRunEvent,
  type BackendAgentRunState,
} from '../lib/backend-agent-runtime'
import { deviceValidationControlSkill, pageAgentControlSkill, type OptimizationWorkflowState } from '../lib/optimization-workflow'
import { buildBoundedConversationRequest, compactTextToTokenLimit, createPromptTokenBudget, estimateMessageTokens, formatTokenCount, normalizeContextWindowTokens, type ConversationMemory, type PromptTokenBudget, type TokenMessage } from '../lib/token-budget'
import {
  deviceActionDependencyIssue,
  deviceValidationContinuationMessage,
  deviceValidationTerminal,
  normalizeDeviceValidationFacts,
  parseAgentResponse,
  updateDeviceValidationFacts,
  type AgentActionObservation,
  type DeviceValidationFacts,
} from '../lib/agent-runtime-contract'
import { readDeviceValidationSnapshot, storeDeviceValidationSnapshot } from '../lib/device-validation-snapshot'
import {
  AgentWebShell,
  type AgentWebShellCommandSubmission,
  type AgentWebShellHandle,
  type AgentWebShellState,
  type AgentWebShellTransportStatus,
} from './KimiWebShell'
import { listAgentCliAdapters, type AgentCliAdapter, type AgentCliTerminal, type AgentCliTokenUsage } from '../lib/kimi-cli-terminal'
import { CaptureDevicePicker } from './CaptureDevicePicker'
import type { PageAction } from '../lib/types'

type AgentMessage = {
  id: string
  role: 'assistant' | 'user' | 'system'
  content: string
  actionResults?: Array<{ actionId: string; result: string }>
  terminalKind?: 'conversation' | 'tool' | 'status'
  toolLabel?: string
  toolStatus?: 'running' | 'succeeded' | 'failed'
}

type TokenLedger = {
  inputTokens: number
  outputTokens: number
  cachedInputTokens: number
  cacheReportedInputTokens: number
  calls: number
  estimatedCalls: number
  cacheReportedCalls: number
}

type PendingAttachment = {
  id: string
  file: File
  name: string
  size: number
  mediaType: string
  preview: string
  previewAvailable: boolean
  previewTruncated: boolean
}

type PendingTextInput = {
  id: string
  text: string
}

type EngineConnectionState = 'checking' | 'ready' | 'model_configuration_required' | 'unavailable'
type AgentCliCatalogState = 'loading' | 'retrying' | 'ready' | 'error'

const SESSION_SKILL_STORAGE_PREFIX = 'heterogeneous-enable-web:loaded-session-skill:'

function sessionSkillStorageKey(agentSessionId: string, skillId: string): string {
  return `${SESSION_SKILL_STORAGE_PREFIX}${encodeURIComponent(agentSessionId)}:${encodeURIComponent(skillId)}`
}

function sessionHasLoadedSkill(agentSessionId: string, skillId: string): boolean {
  if (!agentSessionId || !skillId) return false
  try {
    return window.sessionStorage.getItem(sessionSkillStorageKey(agentSessionId, skillId)) === 'loaded'
  } catch {
    return false
  }
}

function rememberSessionSkill(agentSessionId: string, skillId: string): void {
  if (!agentSessionId || !skillId) return
  try {
    window.sessionStorage.setItem(sessionSkillStorageKey(agentSessionId, skillId), 'loaded')
  } catch {
    // The active Agent context still retains the Skill when browser storage is unavailable.
  }
}

function engineConnectionState(engine: BackendAgentEngineState | undefined): EngineConnectionState {
  if (engine?.status === 'ready' && engine.model_status !== 'unconfigured') return 'ready'
  if (engine?.service_status === 'ready' && (engine.status === 'model_configuration_required' || engine.model_status === 'unconfigured')) {
    return 'model_configuration_required'
  }
  return 'unavailable'
}

function browserModelConfigSignature(config: BrowserModelConfig): string {
  const value = `${config.baseUrl}\0${config.model}\0${config.contextWindowTokens}\0${config.apiKey}`
  let hash = 2166136261
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index)
    hash = Math.imul(hash, 16777619)
  }
  return (hash >>> 0).toString(16).padStart(8, '0')
}

function retryableModelConfigurationError(error: unknown): boolean {
  if (!(error instanceof ApiError) || error.statusCode === null) return true
  if (error.code === 'KIMI_MODEL_PROBE_FAILED') return false
  return error.statusCode === 409 || error.statusCode === 429 || error.statusCode >= 500
}

function agentCliCatalogDiagnostic(error: unknown): string {
  if (error instanceof ApiError) return `${error.code}：${error.message}`
  if (error instanceof Error && error.message.trim()) return error.message.trim()
  return '内部 Web/API 代理没有返回 Agent Adapter 目录。'
}

function usesUnencryptedModelTransport(baseUrl: string): boolean {
  return baseUrl.trim().toLowerCase().startsWith('http://')
}

type AgentContinuation = {
  schemaVersion: 'page_agent_checkpoint/v1'
  nextUserMessage: string
  memory: ConversationMemory
  origin: string
  analysisId?: string
  progress?: AgentTaskProgressContext
  reason: 'turn_budget' | 'model_error' | 'interrupted' | 'bridge_offline' | 'output_limit' | 'credential'
  resumeTurn: number
  savedAt: string
  failure?: {
    message: string
    transient: boolean
  }
  backendRunId?: string
  backendEventSequence?: number
}

type AgentRunState = {
  turn: number
  maxTurns: number
  maxActionsPerTurn: number
  state?: BackendAgentRunState
  detail?: string
  skillBootstrap?: BackendSkillBootstrap
}

type AgentLoopArgs = {
  initialUserMessage: string
  initialMemory: ConversationMemory
  origin: string
  analysisId?: string
  progress?: AgentTaskProgressContext
  focusedTask?: FocusedAgentTaskContext
  startingTurn?: number
}

type FocusedAgentTaskContext = {
  kind: 'custom_api_kb' | 'application_flow_discovery' | 'application_flow_generation' | 'application_flow_trace_anchors' | 'application_flow_runtime_trace_alignment' | 'code_knowledge_analysis' | 'experimental_flow_hiperf' | 'experimental_flow_trace' | 'scenario_capture' | 'parallel_modeling' | 'workload_features' | 'optimization_calibration'
  requestContext?: Record<string, unknown>
  sourceConnectorId?: string
  sourceSnapshotId?: string
  subpath?: string
  graphId?: string
  currentReportId?: string
  discoveryArtifactId?: string
  flowId?: string
  scenarioArtifactId?: string
  traceAnalysisArtifactId?: string
  evidenceArtifactId?: string
  resultArtifactId?: string
  analysisKind?: 'hiperf' | 'trace'
  applicationFlowAnalysisId?: string
  sourceScopes?: FocusedAgentSourceScope[]
}

type FocusedAgentSourceScope = {
  sourceConnectorId: string
  sourceSnapshotId?: string
  subpath?: string
  graphId?: string
}

type AgentTaskProgressContext = {
  requestId: string
  kind: 'device_validation'
  label: string
  startedAt: string
  facts: DeviceValidationFacts
  requestedUnit?: string
  recommendedProfileId?: string
  compatibleProfileIds?: string[]
}

type BackendAgentSession = {
  runId: string
  transport?: 'kimi-server' | 'agent-cli-pty'
  analysisId?: string
  origin: string
  progress?: AgentTaskProgressContext
  lastSequence: number
  reasoningPreview: string
  contentPreview: string
  liveProgressRequestId?: string
  handledUiRequests: Set<string>
  state: BackendAgentRunState
}

type StoredBackendAgentSession = Omit<BackendAgentSession, 'handledUiRequests'> & {
  schemaVersion: 'backend_agent_session/v1'
  savedAt: string
}

type StoredDeviceBridgeSelection = {
  schemaVersion: 'device_bridge_selection/v1'
  bridgeId: string
  savedAt: string
}

type StrategyProgressStatus = 'running' | 'succeeded' | 'warning' | 'failed'
type StrategyProgressStepStatus = 'pending' | 'running' | 'succeeded' | 'warning' | 'failed'
type StrategyProgressPhase = 'model_pending' | 'response_received' | 'contract_validating' | 'completed' | 'failed'
type StrategyProgressStep = {
  id: string
  title: string
  detail: string
  status: StrategyProgressStepStatus
}
type StrategyProgressDirection = {
  title: string
  detail: string
  evidenceStatus: 'observed' | 'needs_validation' | 'missing'
}
type StrategyProgress = {
  requestId: string
  strategyId: string
  strategyLabel: string
  phase: StrategyProgressPhase
  status: StrategyProgressStatus
  startedAt: string
  headline: string
  detail: string
  steps: StrategyProgressStep[]
  directions: StrategyProgressDirection[]
  streamPreview?: string
  streamedReasoningChars?: number
  streamedContentChars?: number
}

const initialMessage: AgentMessage = {
  id: 'agent-ready',
  role: 'assistant',
  content: '页面 Agent 已就绪。在这里输入命令或直接描述目标；页面按钮也会提交相应任务。',
}

const emptyConversationMemory: ConversationMemory = {
  summary: '',
  messages: [],
  compactedMessageCount: 0,
}

const emptyTokenLedger: TokenLedger = {
  inputTokens: 0,
  outputTokens: 0,
  cachedInputTokens: 0,
  cacheReportedInputTokens: 0,
  calls: 0,
  estimatedCalls: 0,
  cacheReportedCalls: 0,
}

const agentCheckpointStorageKey = 'heterogeneous-enable-page-agent-checkpoint-v1'
const agentCheckpointMaxAgeMs = 7 * 24 * 60 * 60 * 1000
const backendAgentSessionStorageKey = 'heterogeneous-enable-backend-agent-session-v1'
const backendUiResultStoragePrefix = 'heterogeneous-enable-backend-ui-result-v1'
const deviceBridgeSelectionStorageKey = 'heterogeneous-enable-device-bridge-selection-v1'
const agentCliAdapterStorageKey = 'heterogeneous-enable-agent-cli-adapter-v1'
const backendSessionMaxAgeMs = 14 * 24 * 60 * 60 * 1000
const terminalBackendRunStates = new Set<BackendAgentRunState>(['completed', 'failed', 'cancelled'])

function backendRunIsTerminal(state: BackendAgentRunState) {
  return terminalBackendRunStates.has(state)
}

function backendRunState(value: unknown): BackendAgentRunState | null {
  const states: BackendAgentRunState[] = [
    'created', 'waiting_for_model_credential', 'running', 'waiting_for_tool', 'waiting_for_ui', 'waiting_for_device', 'compacting',
    'paused_model_overloaded', 'paused_output_limit', 'paused_turn_budget', 'paused_bridge_offline', 'completed', 'failed', 'cancelled',
  ]
  return states.includes(value as BackendAgentRunState) ? value as BackendAgentRunState : null
}

function agentRunStateFromBackend(run: BackendAgentRun): AgentRunState {
  return {
    turn: run.current_turn,
    maxTurns: run.budget.max_turns,
    maxActionsPerTurn: run.budget.max_actions_per_turn,
    state: run.state,
    detail: run.state_detail,
    skillBootstrap: run.skill_bootstrap,
  }
}

function terminalToolLabel(name: string): string {
  if (!name) return '工具'
  if (!name.startsWith('mcp__')) return name
  const separator = name.lastIndexOf('__')
  const action = separator >= 0 ? name.slice(separator + 2) : name
  return `platform.${action}`
}

function storeBackendAgentSession(session: BackendAgentSession | null) {
  if (typeof window === 'undefined') return
  try {
    // Real Agent CLI sessions are owned by the browser page and persisted in
    // sessionStorage by AgentWebShell. Do not leak that Run into the legacy
    // analysis-scoped localStorage restoration path used by structured calls.
    if (session?.transport === 'agent-cli-pty') {
      window.localStorage.removeItem(backendAgentSessionStorageKey)
      return
    }
    const analysisId = normalizeAnalysisRouteId(session?.analysisId)
    if (!session || !analysisId || readAnalysisRouteId() !== analysisId) {
      window.localStorage.removeItem(backendAgentSessionStorageKey)
      return
    }
    const value: StoredBackendAgentSession = {
      schemaVersion: 'backend_agent_session/v1',
      runId: session.runId,
      analysisId,
      origin: session.origin,
      ...(session.progress ? { progress: session.progress } : {}),
      lastSequence: session.lastSequence,
      reasoningPreview: session.reasoningPreview.slice(-4_000),
      contentPreview: session.contentPreview.slice(-4_000),
      ...(session.liveProgressRequestId ? { liveProgressRequestId: session.liveProgressRequestId } : {}),
      state: session.state,
      savedAt: new Date().toISOString(),
    }
    window.localStorage.setItem(backendAgentSessionStorageKey, JSON.stringify(value))
  } catch {
    // The backend Run remains the durable source of truth when browser storage
    // is disabled or full.
  }
}

function readBackendAgentSession(expectedAnalysisId: string | null): BackendAgentSession | null {
  if (typeof window === 'undefined') return null
  try {
    if (!expectedAnalysisId) {
      window.localStorage.removeItem(backendAgentSessionStorageKey)
      return null
    }
    const value = asRecord(JSON.parse(window.localStorage.getItem(backendAgentSessionStorageKey) ?? 'null'))
    if (value.schemaVersion !== 'backend_agent_session/v1') return null
    const savedAt = typeof value.savedAt === 'string' ? value.savedAt : ''
    const savedAtMs = Date.parse(savedAt)
    if (!Number.isFinite(savedAtMs) || Date.now() - savedAtMs > backendSessionMaxAgeMs) {
      window.localStorage.removeItem(backendAgentSessionStorageKey)
      return null
    }
    const runId = typeof value.runId === 'string' ? value.runId : ''
    const origin = typeof value.origin === 'string' ? value.origin : ''
    const state = backendRunState(value.state)
    const analysisId = normalizeAnalysisRouteId(value.analysisId)
    if (!runId || !origin || !state || !analysisId || analysisId !== expectedAnalysisId) {
      window.localStorage.removeItem(backendAgentSessionStorageKey)
      return null
    }
    const progressRecord = asRecord(value.progress)
    const progress = progressRecord.kind === 'device_validation'
      && typeof progressRecord.requestId === 'string'
      && typeof progressRecord.label === 'string'
      && typeof progressRecord.startedAt === 'string'
      ? {
        requestId: progressRecord.requestId,
        kind: 'device_validation' as const,
        label: progressRecord.label,
        startedAt: progressRecord.startedAt,
        facts: normalizeDeviceValidationFacts(progressRecord.facts),
      }
      : undefined
    return {
      runId,
      analysisId,
      origin,
      ...(progress ? { progress } : {}),
      lastSequence: typeof value.lastSequence === 'number' ? Math.max(0, Math.round(value.lastSequence)) : 0,
      reasoningPreview: typeof value.reasoningPreview === 'string' ? value.reasoningPreview.slice(-4_000) : '',
      contentPreview: typeof value.contentPreview === 'string' ? value.contentPreview.slice(-4_000) : '',
      liveProgressRequestId: typeof value.liveProgressRequestId === 'string' ? value.liveProgressRequestId : undefined,
      handledUiRequests: new Set<string>(),
      state,
    }
  } catch {
    window.localStorage.removeItem(backendAgentSessionStorageKey)
    return null
  }
}

function readDeviceBridgeSelection(): string {
  if (typeof window === 'undefined') return ''
  try {
    const value = asRecord(JSON.parse(window.localStorage.getItem(deviceBridgeSelectionStorageKey) ?? 'null'))
    return value.schemaVersion === 'device_bridge_selection/v1' && typeof value.bridgeId === 'string'
      ? value.bridgeId
      : ''
  } catch {
    return ''
  }
}

function storeDeviceBridgeSelection(bridgeId: string) {
  if (typeof window === 'undefined') return
  try {
    if (!bridgeId) {
      window.localStorage.removeItem(deviceBridgeSelectionStorageKey)
      return
    }
    const value: StoredDeviceBridgeSelection = {
      schemaVersion: 'device_bridge_selection/v1',
      bridgeId,
      savedAt: new Date().toISOString(),
    }
    window.localStorage.setItem(deviceBridgeSelectionStorageKey, JSON.stringify(value))
  } catch {
    // A selected Bridge is a convenience, not the source of authorization.
    // The backend still validates bridge identity, capability and device lease.
  }
}

function remoteCaptureEndpointLabel(value: string): string {
  const label = value.trim()
  if (/^windows hdc device bridge$/i.test(label) || /^windows device bridge$/i.test(label)) return 'Windows 采集端'
  if (/^windows bridge(?=\s*(?:·|$))/i.test(label)) return label.replace(/^windows bridge/i, 'Windows 采集端')
  if (/^device bridge$/i.test(label) || /^bridge$/i.test(label)) return '远程采集端'
  return label
}

function backendUiResultStorageKey(runId: string, requestId: string) {
  return `${backendUiResultStoragePrefix}:${runId}:${requestId}`
}

function storedBackendUiResult(runId: string, requestId: string): { result?: Record<string, unknown>; error?: Record<string, unknown> } | null {
  if (typeof window === 'undefined') return null
  try {
    const value = asRecord(JSON.parse(window.localStorage.getItem(backendUiResultStorageKey(runId, requestId)) ?? 'null'))
    const result = asRecord(value.result)
    const error = asRecord(value.error)
    if (Object.keys(result).length === 0 && Object.keys(error).length === 0) return null
    return { ...(Object.keys(result).length > 0 ? { result } : {}), ...(Object.keys(error).length > 0 ? { error } : {}) }
  } catch {
    return null
  }
}

function persistBackendUiResult(runId: string, requestId: string, value: { result?: Record<string, unknown>; error?: Record<string, unknown> }) {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(backendUiResultStorageKey(runId, requestId), JSON.stringify(value))
  } catch {
    // A result can still be re-executed through the backend pending action if
    // the browser has no local persistence available.
  }
}

function clearStoredBackendUiResult(runId: string, requestId: string) {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.removeItem(backendUiResultStorageKey(runId, requestId))
  } catch {
    // Nothing to do.
  }
}

function shortResult(value: unknown): string {
  if (typeof value === 'string') return value.slice(0, 180)
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>
    if (typeof record.queued_action === 'string') return `已排队：${record.queued_action}`.slice(0, 180)
    if (Array.isArray(record.profiles)) {
      const profiles = record.profiles.map((item) => {
        const profile = asRecord(item)
        const headerPolicy = asRecord(profile.header_policy)
        const libcHeaderSurface = asRecord(profile.libc_header_surface)
        return {
          profile_id: profile.profile_id,
          unit: profile.unit,
          language: profile.language,
          summary_zh: profile.summary_zh,
          runtime_contract_zh: profile.runtime_contract_zh,
          entrypoint: profile.entrypoint,
          sdk_include_roots: profile.sdk_include_roots,
          repository_header_paths: profile.repository_header_paths,
          libc_header_surface: {
            available: libcHeaderSurface.available,
            family: libcHeaderSurface.family,
            standard_c_headers: libcHeaderSurface.standard_c_headers,
            linked_device_libc: libcHeaderSurface.linked_device_libc,
            guidance_zh: libcHeaderSurface.guidance_zh,
          },
          header_policy: {
            statement_zh: headerPolicy.statement_zh,
            matched_headers: headerPolicy.matched_headers,
            matched_header_count: headerPolicy.matched_header_count,
            total_header_count: headerPolicy.total_header_count,
            catalog_complete: headerPolicy.catalog_complete,
            query_hint_zh: headerPolicy.query_hint_zh,
          },
          linked_device_libraries: profile.linked_device_libraries,
        }
      })
      return JSON.stringify({ profiles }).slice(0, 8_000)
    }
    const factualKeys = [
      'status', 'error', 'error_code', 'error_details', 'message', 'artifact_id', 'input_kind', 'file_name', 'plan_id', 'profiles', 'device_count', 'devices', 'selected_device_serial',
      'probe_artifact_id', 'device_serial', 'observed_units', 'assessment_artifact_id', 'decision',
      'validation_status', 'validation_errors', 'attempt_count', 'summary', 'plan_artifact_id', 'plan_status',
      'planning_reason_zh', 'required_next_steps', 'confirmation_required',
      'run_artifact_id', 'execution_authorization', 'adapter_artifact_id', 'adapter_kind', 'materialization_status',
      'source_path', 'generated_adapter_run_artifact_id', 'next_recommended_actions', 'observed_evidence', 'compile',
      'remote_shell_run_artifact_id', 'command', 'command_sha256', 'return_code', 'timed_out', 'output_excerpt',
      'staged_source_artifact_id', 'experiment_run_artifact_id', 'profile_id', 'requested_unit', 'source_sha256',
      'source_relative_path', 'source_bytes', 'build', 'deployment', 'remote_binary_path', 'next_action_zh',
      'path', 'sha256', 'content', 'bytes', 'truncated',
    ]
    const factual = Object.fromEntries(factualKeys.flatMap((key) => key in record ? [[key, record[key]]] : []))
    const limit = typeof record.remote_shell_run_artifact_id === 'string' || typeof record.experiment_run_artifact_id === 'string' ? 8_000 : 1_800
    return JSON.stringify(Object.keys(factual).length > 0 ? factual : record).slice(0, limit)
  }
  return String(value)
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function focusedTaskFromPageContext(context: Record<string, unknown>): FocusedAgentTaskContext | undefined {
  if (context.active_view === 'scenario_capture' || context.active_view === 'parallel_modeling' || context.active_view === 'workload_features' || context.active_view === 'optimization_calibration') {
    return { kind: context.active_view }
  }
  if (context.active_view === 'code_knowledge') {
    const codeKnowledge = asRecord(context.code_knowledge)
    const sourceConnectorId = typeof codeKnowledge.source_connector_id === 'string' ? codeKnowledge.source_connector_id.trim() : ''
    const sourceSnapshotId = typeof codeKnowledge.source_snapshot_id === 'string' ? codeKnowledge.source_snapshot_id.trim() : ''
    if (!sourceConnectorId || !sourceSnapshotId) return undefined
    return {
      kind: 'code_knowledge_analysis',
      sourceConnectorId,
      sourceSnapshotId,
      subpath: typeof codeKnowledge.subpath === 'string' ? codeKnowledge.subpath : '',
      graphId: typeof codeKnowledge.graph_id === 'string' ? codeKnowledge.graph_id : undefined,
      currentReportId: typeof codeKnowledge.current_report_id === 'string' ? codeKnowledge.current_report_id : undefined,
    }
  }
  if (context.active_view === 'flows') {
    const applicationFlow = asRecord(context.application_flow)
    const sourceConnectorId = typeof applicationFlow.source_connector_id === 'string' ? applicationFlow.source_connector_id.trim() : ''
    const sourceSnapshotId = typeof applicationFlow.source_snapshot_id === 'string' ? applicationFlow.source_snapshot_id.trim() : ''
    if (!sourceConnectorId || !sourceSnapshotId) return undefined
    const scenarioArtifactId = typeof applicationFlow.scenario_artifact_id === 'string' ? applicationFlow.scenario_artifact_id : undefined
    const sourceScopes = Array.isArray(applicationFlow.source_scopes)
      ? applicationFlow.source_scopes.map(asRecord).flatMap((item) => {
        const connectorId = typeof item.source_connector_id === 'string' ? item.source_connector_id.trim() : ''
        if (!connectorId) return []
        return [{
          sourceConnectorId: connectorId,
          sourceSnapshotId: typeof item.source_snapshot_id === 'string' ? item.source_snapshot_id : undefined,
          subpath: typeof item.subpath === 'string' ? item.subpath : undefined,
          graphId: typeof item.code_graph_id === 'string'
            ? item.code_graph_id
            : typeof item.graph_id === 'string' ? item.graph_id : undefined,
        }]
      })
      : undefined
    return {
      kind: scenarioArtifactId ? 'application_flow_generation' : 'application_flow_discovery',
      sourceConnectorId,
      sourceSnapshotId,
      subpath: typeof applicationFlow.subpath === 'string' ? applicationFlow.subpath : '',
      graphId: typeof applicationFlow.graph_id === 'string' ? applicationFlow.graph_id : undefined,
      discoveryArtifactId: typeof applicationFlow.discovery_artifact_id === 'string' ? applicationFlow.discovery_artifact_id : undefined,
      flowId: typeof applicationFlow.flow_id === 'string' ? applicationFlow.flow_id : undefined,
      scenarioArtifactId,
      applicationFlowAnalysisId: typeof applicationFlow.application_flow_analysis_id === 'string'
        ? applicationFlow.application_flow_analysis_id
        : undefined,
      sourceScopes,
    }
  }
  if (context.active_view === 'playground_hiperf' || context.active_view === 'playground_trace') {
    const experimentalFlow = asRecord(context.experimental_flow)
    const sourceConnectorId = typeof experimentalFlow.source_connector_id === 'string' ? experimentalFlow.source_connector_id.trim() : ''
    const sourceSnapshotId = typeof experimentalFlow.source_snapshot_id === 'string' ? experimentalFlow.source_snapshot_id.trim() : ''
    const sourceScopes = Array.isArray(experimentalFlow.source_scopes)
      ? experimentalFlow.source_scopes.map(asRecord).flatMap((item) => {
        const connectorId = typeof item.source_connector_id === 'string' ? item.source_connector_id.trim() : ''
        if (!connectorId) return []
        return [{
          sourceConnectorId: connectorId,
          sourceSnapshotId: typeof item.source_snapshot_id === 'string' ? item.source_snapshot_id.trim() : undefined,
          subpath: typeof item.subpath === 'string' ? item.subpath : undefined,
        }]
      })
      : []
    const analysisKind = context.active_view === 'playground_hiperf' ? 'hiperf' : 'trace'
    if (!sourceConnectorId || !sourceSnapshotId) return undefined
    return {
      kind: analysisKind === 'hiperf' ? 'experimental_flow_hiperf' : 'experimental_flow_trace',
      analysisKind,
      sourceConnectorId,
      sourceSnapshotId,
      subpath: typeof experimentalFlow.subpath === 'string' ? experimentalFlow.subpath : '',
      sourceScopes,
      evidenceArtifactId: typeof experimentalFlow.evidence_artifact_id === 'string' ? experimentalFlow.evidence_artifact_id : undefined,
      resultArtifactId: typeof experimentalFlow.result_artifact_id === 'string' ? experimentalFlow.result_artifact_id : undefined,
    }
  }
  return undefined
}

function storeAgentCheckpoint(checkpoint: AgentContinuation | null) {
  if (typeof window === 'undefined') return
  try {
    const analysisId = normalizeAnalysisRouteId(checkpoint?.analysisId)
    if (!checkpoint || !analysisId || readAnalysisRouteId() !== analysisId) {
      window.localStorage.removeItem(agentCheckpointStorageKey)
      return
    }
    window.localStorage.setItem(agentCheckpointStorageKey, JSON.stringify({ ...checkpoint, analysisId }))
  } catch {
    // In-memory recovery still works when browser storage is unavailable or full.
  }
}

function readAgentCheckpoint(expectedAnalysisId: string | null): AgentContinuation | null {
  if (typeof window === 'undefined') return null
  try {
    if (!expectedAnalysisId) {
      storeAgentCheckpoint(null)
      return null
    }
    const value = asRecord(JSON.parse(window.localStorage.getItem(agentCheckpointStorageKey) ?? 'null'))
    if (value.schemaVersion !== 'page_agent_checkpoint/v1') return null
    const savedAt = typeof value.savedAt === 'string' ? value.savedAt : ''
    const savedAtMs = Date.parse(savedAt)
    if (!Number.isFinite(savedAtMs) || Date.now() - savedAtMs > agentCheckpointMaxAgeMs) {
      storeAgentCheckpoint(null)
      return null
    }
    const memory = asRecord(value.memory)
    const rawMessages = Array.isArray(memory.messages) ? memory.messages.map(asRecord) : []
    const messages = rawMessages.flatMap((message) => {
      const role = message.role
      const content = message.content
      return (role === 'system' || role === 'user' || role === 'assistant') && typeof content === 'string'
        ? [{ role, content } as TokenMessage]
        : []
    })
    const nextUserMessage = typeof value.nextUserMessage === 'string' ? value.nextUserMessage : ''
    const origin = typeof value.origin === 'string' ? value.origin : ''
    const analysisId = normalizeAnalysisRouteId(value.analysisId)
    if (!nextUserMessage || !origin || !analysisId || analysisId !== expectedAnalysisId) {
      storeAgentCheckpoint(null)
      return null
    }
    const reason = value.reason === 'turn_budget' || value.reason === 'model_error' || value.reason === 'interrupted'
      || value.reason === 'bridge_offline' || value.reason === 'output_limit' || value.reason === 'credential'
      ? value.reason
      : 'interrupted'
    const progressRecord = asRecord(value.progress)
    const progress = progressRecord.kind === 'device_validation'
      && typeof progressRecord.requestId === 'string'
      && typeof progressRecord.label === 'string'
      && typeof progressRecord.startedAt === 'string'
      ? {
        requestId: progressRecord.requestId,
        kind: 'device_validation' as const,
        label: progressRecord.label,
        startedAt: progressRecord.startedAt,
        facts: normalizeDeviceValidationFacts(progressRecord.facts),
      }
      : undefined
    const failureRecord = asRecord(value.failure)
    const failure = typeof failureRecord.message === 'string'
      ? { message: failureRecord.message, transient: failureRecord.transient === true }
      : undefined
    return {
      schemaVersion: 'page_agent_checkpoint/v1',
      nextUserMessage,
      memory: {
        summary: typeof memory.summary === 'string' ? memory.summary : '',
        messages,
        compactedMessageCount: typeof memory.compactedMessageCount === 'number' ? Math.max(0, Math.round(memory.compactedMessageCount)) : 0,
      },
      origin,
      analysisId,
      progress,
      reason,
      resumeTurn: typeof value.resumeTurn === 'number' ? Math.max(1, Math.round(value.resumeTurn)) : 1,
      savedAt,
      failure,
      backendRunId: typeof value.backendRunId === 'string' ? value.backendRunId : undefined,
      backendEventSequence: typeof value.backendEventSequence === 'number' ? Math.max(0, Math.round(value.backendEventSequence)) : undefined,
    }
  } catch {
    storeAgentCheckpoint(null)
    return null
  }
}

function numericValue(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null
}

function modelTokenUsage(value: unknown): ModelTokenUsage | null {
  const usage = asRecord(value)
  const inputTokens = numericValue(usage.inputTokens)
  const outputTokens = numericValue(usage.outputTokens)
  const totalTokens = numericValue(usage.totalTokens)
  if (inputTokens === null || outputTokens === null || totalTokens === null) return null
  const cachedInputTokens = numericValue(usage.cachedInputTokens)
  const cacheHitRate = numericValue(usage.cacheHitRate)
  return {
    inputTokens,
    outputTokens,
    totalTokens,
    inputSource: usage.inputSource === 'provider' ? 'provider' : 'estimated',
    outputSource: usage.outputSource === 'provider' ? 'provider' : 'estimated',
    ...(cachedInputTokens === null ? {} : { cachedInputTokens }),
    ...(cacheHitRate === null ? {} : { cacheHitRate: Math.min(1, cacheHitRate) }),
  }
}

function backendModelTokenUsage(value: unknown): ModelTokenUsage | null {
  const usage = asRecord(value)
  const inputTokens = numericValue(usage.prompt_tokens ?? usage.input_tokens ?? usage.inputTokens)
  const outputTokens = numericValue(usage.completion_tokens ?? usage.output_tokens ?? usage.outputTokens)
  if (inputTokens === null || outputTokens === null) return null
  const totalTokens = numericValue(usage.total_tokens ?? usage.totalTokens) ?? inputTokens + outputTokens
  const promptDetails = asRecord(usage.prompt_tokens_details ?? usage.input_tokens_details)
  const cachedInputTokens = numericValue(promptDetails.cached_tokens ?? usage.cached_input_tokens ?? usage.cachedInputTokens)
  return {
    inputTokens,
    outputTokens,
    totalTokens,
    inputSource: 'provider',
    outputSource: 'provider',
    ...(cachedInputTokens === null ? {} : { cachedInputTokens }),
  }
}

function strategyProgressStatus(value: unknown): StrategyProgressStatus | null {
  return value === 'running' || value === 'succeeded' || value === 'warning' || value === 'failed' ? value : null
}

function strategyProgressStepStatus(value: unknown): StrategyProgressStepStatus | null {
  return value === 'pending' || value === 'running' || value === 'succeeded' || value === 'warning' || value === 'failed' ? value : null
}

function strategyProgressPhase(value: unknown): StrategyProgressPhase | null {
  return value === 'model_pending' || value === 'response_received' || value === 'contract_validating' || value === 'completed' || value === 'failed' ? value : null
}

function parseStrategyProgress(value: unknown): StrategyProgress | null {
  const detail = asRecord(value)
  if (detail.schema_version !== 'strategy_progress/v1') return null
  const requestId = typeof detail.request_id === 'string' ? detail.request_id : ''
  const strategyId = typeof detail.strategy_id === 'string' ? detail.strategy_id : ''
  const strategyLabel = typeof detail.strategy_label === 'string' ? detail.strategy_label : ''
  const phase = strategyProgressPhase(detail.phase)
  const status = strategyProgressStatus(detail.status)
  const startedAt = typeof detail.started_at === 'string' ? detail.started_at : ''
  const headline = typeof detail.headline === 'string' ? detail.headline : ''
  const progressDetail = typeof detail.detail === 'string' ? detail.detail : ''
  if (!requestId || !strategyId || !strategyLabel || !phase || !status || !startedAt || !headline) return null
  const steps = Array.isArray(detail.steps) ? detail.steps.flatMap((item): StrategyProgressStep[] => {
    const step = asRecord(item)
    const id = typeof step.id === 'string' ? step.id : ''
    const title = typeof step.title === 'string' ? step.title : ''
    const stepDetail = typeof step.detail === 'string' ? step.detail : ''
    const stepStatus = strategyProgressStepStatus(step.status)
    return id && title && stepStatus ? [{ id, title, detail: stepDetail, status: stepStatus }] : []
  }) : []
  const directions = Array.isArray(detail.directions) ? detail.directions.flatMap((item): StrategyProgressDirection[] => {
    const direction = asRecord(item)
    const title = typeof direction.title === 'string' ? direction.title : ''
    const directionDetail = typeof direction.detail === 'string' ? direction.detail : ''
    const evidenceStatus = direction.evidenceStatus
    return title && directionDetail && (evidenceStatus === 'observed' || evidenceStatus === 'needs_validation' || evidenceStatus === 'missing')
      ? [{ title, detail: directionDetail, evidenceStatus }]
      : []
  }) : []
  return {
    requestId,
    strategyId,
    strategyLabel,
    phase,
    status,
    startedAt,
    headline,
    detail: progressDetail,
    steps,
    directions,
    streamPreview: typeof detail.streamPreview === 'string' ? detail.streamPreview : typeof detail.stream_preview === 'string' ? detail.stream_preview : undefined,
    streamedReasoningChars: numericValue(detail.streamedReasoningChars ?? detail.streamed_reasoning_chars) ?? undefined,
    streamedContentChars: numericValue(detail.streamedContentChars ?? detail.streamed_content_chars) ?? undefined,
  }
}

function createDeviceValidationProgress(context: AgentTaskProgressContext): StrategyProgress {
  return {
    requestId: context.requestId,
    strategyId: 'device_validation',
    strategyLabel: context.label,
    phase: 'model_pending',
    status: 'running',
    startedAt: context.startedAt,
    headline: '正在建立第一轮真机实验决策',
    detail: '热点源码、策略摘要、目标单元和已有设备 probe 已作为独立任务上下文注入。',
    steps: [
      { id: 'context', title: '载入真机验证上下文', detail: '使用已有热点、路由和机会事实，并补充紧凑设备任务包。', status: 'succeeded' },
      { id: 'model', title: '模型自主规划与修订', detail: '模型将根据每轮真实观察自行决定下一步。', status: 'running' },
      { id: 'actions', title: '工具与设备操作', detail: '等待模型选择 profile、暂存、构建上传或手机命令。', status: 'pending' },
      { id: 'verdict', title: '形成分层验证结论', detail: '分别判断编译、执行、等价性、硬件参与和性能证据。', status: 'pending' },
    ],
    directions: [],
  }
}

function createAgentRuntimeProgress(input: {
  requestId: string
  label?: string
  headline?: string
  detail?: string
  startedAt?: string
}): StrategyProgress {
  return {
    requestId: input.requestId,
    strategyId: 'page_agent',
    strategyLabel: input.label ?? '页面助手',
    phase: 'model_pending',
    status: 'running',
    startedAt: input.startedAt ?? new Date().toISOString(),
    headline: input.headline ?? '正在理解请求并决定下一步',
    detail: input.detail ?? '模型将根据当前消息和页面事实决定直接回复或调用分析工具。',
    steps: [
      { id: 'model', title: '模型推理', detail: '等待模型的实时生成内容。', status: 'running' },
      { id: 'actions', title: '页面与工具操作', detail: '模型会按真实依赖逐步调用已注册操作。', status: 'pending' },
      { id: 'verdict', title: '结果或阻塞说明', detail: '完成时给出结论；无法推进时说明具体原因。', status: 'pending' },
    ],
    directions: [],
  }
}

function advanceStrategyProgress(progress: StrategyProgress | null, phase: StrategyProgressPhase, detail: string): StrategyProgress | null {
  if (!progress) return progress
  const status: StrategyProgressStatus = phase === 'failed' ? 'failed' : phase === 'completed' ? 'succeeded' : 'running'
  const headline = phase === 'response_received'
    ? '模型文本已收到，正在进入结构化处理'
    : phase === 'contract_validating'
      ? '正在校验模型结论与证据门禁'
      : phase === 'completed'
        ? '策略结果已回填并完成证据校验'
        : phase === 'failed'
          ? '本次策略推理未完成'
          : progress.headline
  return {
    ...progress,
    phase,
    status,
    headline,
    detail,
    steps: progress.steps.map((step) => {
      if (step.id === 'model') return { ...step, status: phase === 'model_pending' ? 'running' : phase === 'failed' ? 'failed' : 'succeeded' }
      if (step.id === 'contract') {
        const nextStatus: StrategyProgressStepStatus = phase === 'model_pending' ? 'pending'
          : phase === 'response_received' || phase === 'contract_validating' ? 'running'
            : phase === 'completed' ? 'succeeded'
              : 'failed'
        return { ...step, status: nextStatus, detail: phase === 'completed' || phase === 'failed' ? detail : step.detail }
      }
      return step
    }),
  }
}

function elapsedLabel(seconds: number): string {
  if (seconds < 60) return `已用时 ${seconds}s`
  return `已用时 ${Math.floor(seconds / 60)}m ${seconds % 60}s`
}

function compactStreamPreview(value: string | undefined, limit = 260): string {
  const normalized = (value ?? '').replace(/\s+/g, ' ').trim()
  if (normalized.length <= limit) return normalized
  return `…${normalized.slice(-limit)}`
}

function StrategyProgressIcon({ status }: { status: StrategyProgressStepStatus }) {
  if (status === 'succeeded') return <CheckCircle2 size={15} aria-hidden="true" />
  if (status === 'warning' || status === 'failed') return <CircleAlert size={15} aria-hidden="true" />
  if (status === 'running') return <LoaderCircle size={15} className="spin" aria-hidden="true" />
  return <Circle size={15} aria-hidden="true" />
}

function StrategyProgressPanel({
  progress,
  elapsedSeconds,
  expanded,
  onToggle,
}: {
  progress: StrategyProgress
  elapsedSeconds: number
  expanded: boolean
  onToggle: () => void
}) {
  const isRunning = progress.status === 'running'
  const statusLabel = progress.status === 'succeeded' ? '已完成' : progress.status === 'warning' ? '已回填，需关注' : progress.status === 'failed' ? '未完成' : elapsedLabel(elapsedSeconds)
  const livePreview = isRunning ? compactStreamPreview(progress.streamPreview) : ''
  return <section className={`agent-reasoning-progress ${progress.status} ${expanded ? 'is-expanded' : 'is-collapsed'}`} aria-label={`${progress.strategyLabel}实时分析进度`}>
    <button className="agent-reasoning-progress-toggle" type="button" aria-expanded={expanded} onClick={onToggle} title={expanded ? '收起实时分析进度' : '展开实时分析进度'}>
      <div className="agent-reasoning-progress-toggle-content">
        <div className="agent-reasoning-progress-heading">
          <div><span>实时分析进度</span><strong>{progress.strategyLabel}</strong></div>
          <small>{isRunning && <LoaderCircle size={12} className="spin" aria-hidden="true" />}{statusLabel}</small>
        </div>
        <p className={`agent-reasoning-progress-preview ${livePreview ? 'has-live-stream' : ''}`}><strong>{progress.headline}</strong><span title={livePreview || progress.detail}>{livePreview ? `实时 · ${livePreview}` : progress.detail}</span></p>
      </div>
      <ChevronDown size={15} className="agent-reasoning-progress-chevron" aria-hidden="true" />
    </button>
    {expanded && <div className="agent-reasoning-progress-details">
      <ol className="agent-reasoning-progress-steps">
        {progress.steps.map((step) => <li className={`is-${step.status}`} key={step.id}>
          <StrategyProgressIcon status={step.status} />
          <div><strong>{step.title}</strong><span>{step.detail}</span></div>
        </li>)}
      </ol>
      {progress.directions.length > 0 && <div className="agent-reasoning-progress-directions">
        <span>本轮待验证方向</span>
        {progress.directions.map((direction, index) => <div className={`agent-progress-direction is-${direction.evidenceStatus}`} key={`${direction.title}-${index}`}>
          <strong>{direction.title}</strong><small>{direction.detail}</small>
        </div>)}
      </div>}
      {progress.streamPreview && <details className="agent-reasoning-stream" open>
        <summary>实时推理内容</summary>
        <pre>{progress.streamPreview}</pre>
      </details>}
    </div>}
  </section>
}

function boundedPageContext(context: Record<string, unknown>, contextWindowTokens: number): { text: string; compacted: boolean } {
  const serialized = JSON.stringify(assistantPageContext(context))
  const tokenLimit = Math.max(1200, Math.min(12000, Math.floor(contextWindowTokens * 0.14)))
  const compacted = compactTextToTokenLimit(serialized, tokenLimit, '[页面状态超过运行时传输范围；不可依据省略内容执行操作]')
  return { text: compacted.text, compacted: compacted.truncated }
}

function assistantSystemPrompt(
  actions: PageAction[],
  runtime: Pick<BrowserModelConfig, 'agentExecutionPolicy'>,
  skillProfile?: SkillVersionProfile | null,
): string {
  const toolDescription = [...actions]
    .sort((left, right) => left.action_id.localeCompare(right.action_id))
    .map(({ action_id, risk_level, input_schema }) => ({ action_id, risk_level, input_schema }))
  return [
    applySkillPrompt(pageAgentControlSkill, skillProfile, 'page_agent'),
    applySkillPrompt(deviceValidationControlSkill, skillProfile, 'device_validation'),
    '# 本轮运行时约束',
    '字段不足时不要调用操作；不存在、超出目录或含义不清时必须追问，绝不能编造 event_id。',
    '面向用户的回复通常跟随用户当前使用的语言；不要为了展示而强制改写原始思考流。标识符、路径和 evidence_id 保留原样。',
    '`art_*`、`artifact:*`、`trace:*`、`source:*` 都是引用标识，不是代码仓路径；只有页面明确给出的 repository-relative path 才能传给 workspace.read_file。真机任务已经注入的热点源码和策略摘要应直接使用，不得为了读取其工件 ID 额外调用文件 action。',
    '任务不设置产品级轮次、action、上下文或输出预算。根据真实观察持续决定下一步是回答、读取文件、修改文件、发起设备探测、暂存实验、后端构建、部署产物、执行设备命令或停止；只有任务完成、存在明确外部阻塞或用户主动停止时才结束。',
    runtime.agentExecutionPolicy === 'full_access'
      ? '当前执行策略为全授权：可调用已注册的 workspace.read_file、workspace.write_file、build.describe_profile、build.stage_source、build.create、device.discover、device.probe、device.deploy_artifact 与 device.execute_shell。写真机源码前用 build.describe_profile 查询固定 profile 的真实头文件和链接面；GPU/NPU/媒体 profile 若声明 libc_header_surface.available=true，可直接使用其 OpenHarmony musl 标准 C 头，不要逐轮删除 stdio.h、stdlib.h、string.h 或 unistd.h。不要从优化 KB、硬件 KB 或案例源码推断可编译头文件。随后暂存完整实验源码，先完成后端构建，再部署构建产物，取得真实 remote path 后由 device.execute_shell 决定手机侧运行、诊断与清理。device.execute_shell 的 command 仅作为固定 HDC 的单个远端 Shell 参数在当前已探测手机上执行；你可以依据 stdout/stderr 自主迭代，但绝不能构造或要求执行宿主机 shell、宿主机 HDC、宿主机路径或任意未注册 action。'
      : '当前执行策略要求确认模式：可做读取、分析和只读设备探测；暂存实验、后端构建、部署和远端 Shell 必须停止并提示用户切换到全授权。',
    `已注册操作：${JSON.stringify(toolDescription)}`,
  ].join('\n')
}

function assistantTurnMessage(
  context: Record<string, unknown>,
  contextWindowTokens: number,
  turnInput: string,
  _remainingTurns: number,
  pendingAttachments: PendingAttachment[],
  pendingTextInputs: PendingTextInput[],
): string {
  const pageContext = boundedPageContext(context, contextWindowTokens)
  const attachmentContext = pendingAttachments.map((attachment) => ({
    attachment_id: attachment.id,
    file_name: attachment.name,
    file_bytes: attachment.size,
    media_type: attachment.mediaType || 'unknown',
    text_preview_available: attachment.previewAvailable,
    preview_truncated: attachment.previewTruncated,
  }))
  const textInputContext = pendingTextInputs.map((input) => ({
    input_id: input.id,
    character_count: input.text.length,
  }))
  return [
    '# 本轮输入或工具观察',
    turnInput,
    '# 本轮动态状态',
    '当前任务由用户显式停止，不设置产品级轮次或工具次数上限。',
    attachmentContext.length > 0
      ? `当前待处理浏览器附件（尚未被平台判定用途或导入）：${JSON.stringify(attachmentContext)}`
      : '当前没有待处理浏览器附件。',
    textInputContext.length > 0
      ? `当前仍可按原文引用的用户输入：${JSON.stringify(textInputContext)}`
      : '当前没有可按原文引用的用户输入。',
    `当前页面上下文${pageContext.compacted ? '（已压缩）' : ''}：${pageContext.text}`,
  ].join('\n')
}

const attachmentPreviewByteLimit = 12 * 1024
const pendingInputLimit = 12

function inputReference(prefix: 'input' | 'attachment'): string {
  return `${prefix}_${randomUuid()}`
}

function readableTextPreview(bytes: Uint8Array): { available: boolean; text: string } {
  if (bytes.length === 0) return { available: true, text: '' }
  let controls = 0
  for (const byte of bytes) {
    if (byte === 0) return { available: false, text: '' }
    if (byte < 0x09 || (byte > 0x0d && byte < 0x20)) controls += 1
  }
  if (controls / bytes.length > 0.02) return { available: false, text: '' }
  return { available: true, text: new TextDecoder('utf-8', { fatal: false }).decode(bytes) }
}

function attachmentPromptRecord(attachment: PendingAttachment): Record<string, unknown> {
  return {
    attachment_id: attachment.id,
    file_name: attachment.name,
    media_type: attachment.mediaType || null,
    size_bytes: attachment.size,
    text_preview_available: attachment.previewAvailable,
    text_preview_truncated: attachment.previewTruncated,
    ...(attachment.previewAvailable ? { text_preview: attachment.preview } : {}),
  }
}

function userTurnEnvelope(input: PendingTextInput | null, attachments: PendingAttachment[]): string {
  return JSON.stringify({
    schema_version: 'page_agent_user_input/v1',
    input_id: input?.id ?? null,
    user_content: input?.text ?? '',
    pending_attachments: attachments.map(attachmentPromptRecord),
    intent_instruction: '请结合对话历史与当前页面状态判断这些内容的用途；含义不清时先追问，不要仅凭代码形状、文件名或扩展名强制执行页面操作。',
  })
}

function firstFencedCode(value: string): string | null {
  return value.match(/```[^\n]*\n([\s\S]*?)```/)?.[1]?.trim() || null
}

function lineRangeCode(value: string, startValue: unknown, endValue: unknown): { code: string; lineStart: number; lineEnd: number } | null {
  const lineStart = typeof startValue === 'number' && Number.isInteger(startValue) ? startValue : 0
  const lineEnd = typeof endValue === 'number' && Number.isInteger(endValue) ? endValue : 0
  const lines = value.split(/\r?\n/)
  if (lineStart < 1 || lineEnd < lineStart || lineEnd > lines.length) return null
  const code = lines.slice(lineStart - 1, lineEnd).join('\n').trim()
  return code ? { code, lineStart, lineEnd } : null
}

function promptBudgetFromEvent(value: unknown, fallbackWindowTokens: number, messages: TokenMessage[]): PromptTokenBudget {
  const detail = asRecord(value)
  const contextWindowTokens = normalizeContextWindowTokens(numericValue(detail.contextWindowTokens) ?? fallbackWindowTokens)
  return createPromptTokenBudget({
    contextWindowTokens,
    inputTokens: numericValue(detail.inputTokens) ?? estimateMessageTokens(messages),
    outputReserveTokens: numericValue(detail.outputReserveTokens) ?? undefined,
    compactionApplied: detail.compactionApplied === true,
    compactedItems: numericValue(detail.compactedItems) ?? 0,
  })
}

export function AgentPanel({
  actions,
  context,
  onExecuteAction,
  workflow,
  onImportTraceFile,
  skillProfile,
}: {
  actions: PageAction[]
  context: Record<string, unknown>
  onExecuteAction: (actionId: string, input: Record<string, unknown>) => Promise<unknown>
  workflow: OptimizationWorkflowState
  onImportTraceFile: (file: File) => Promise<{ artifact: { artifact_id: string; input_kind?: string } }>
  skillProfile: SkillVersionProfile | null
}) {
  const [, setMessages] = useState<AgentMessage[]>([initialMessage])
  const [draft, setDraft] = useState('')
  const [config, setConfig] = useState<BrowserModelConfig>({
    baseUrl: 'https://api.kimi.com/coding/v1',
    apiKey: '',
    model: 'kimi-for-coding',
    contextWindowTokens: 128 * 1024,
    agentMaxOutputTokens: DEFAULT_AGENT_MAX_OUTPUT_TOKENS,
    agentMaxTurns: 10,
    agentMaxActionsPerTurn: 8,
    agentExecutionPolicy: 'full_access',
  })
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [agentCliAdapters, setAgentCliAdapters] = useState<AgentCliAdapter[]>([])
  const [selectedAgentAdapterId, setSelectedAgentAdapterId] = useState('kimi')
  const [agentCliCatalogState, setAgentCliCatalogState] = useState<AgentCliCatalogState>('loading')
  const [agentCliCatalogDetail, setAgentCliCatalogDetail] = useState('正在读取 Agent Adapter 目录；PTY 尚未启动。')
  const [agentCliCatalogReloadRevision, setAgentCliCatalogReloadRevision] = useState(0)
  const [showKey, setShowKey] = useState(false)
  const [busy, setBusy] = useState(false)
  const [strategyRequestBusy, setStrategyRequestBusy] = useState(false)
  const [strategyProgress, setStrategyProgress] = useState<StrategyProgress | null>(null)
  const [strategyProgressExpanded, setStrategyProgressExpanded] = useState(false)
  const [strategyProgressElapsedSeconds, setStrategyProgressElapsedSeconds] = useState(0)
  const [attachmentBusy, setAttachmentBusy] = useState(false)
  const [dragActive, setDragActive] = useState(false)
  const [connectionState, setConnectionState] = useState<'idle' | 'ok' | 'error'>('idle')
  const [modelConfigBusy, setModelConfigBusy] = useState<'save' | 'clear' | 'probe' | null>(null)
  const [engineConnection, setEngineConnection] = useState<EngineConnectionState>('checking')
  const [conversationMemory, setConversationMemory] = useState<ConversationMemory>(emptyConversationMemory)
  const [agentContinuation, setAgentContinuation] = useState<AgentContinuation | null>(null)
  const [agentRun, setAgentRun] = useState<AgentRunState | null>(null)
  const [lastBackendRunId, setLastBackendRunId] = useState<string | null>(null)
  const setAgentStatusText = useCallback((value: string) => {
    const detail = value.trim()
    if (!detail) return
    setStrategyProgress((current) => current?.status === 'running'
      ? { ...current, detail }
      : createAgentRuntimeProgress({
        requestId: `page-status:${Date.now()}`,
        headline: '页面助手正在处理',
        detail,
      }))
  }, [])
  const [deviceBridges, setDeviceBridges] = useState<BackendDeviceBridge[]>([])
  const [selectedDeviceBridgeId, setSelectedDeviceBridgeId] = useState('')
  const [deviceBridgeBusy, setDeviceBridgeBusy] = useState<'refresh' | null>(null)
  const [deviceBridgeNotice, setDeviceBridgeNotice] = useState('')
  const [tokenLedger, setTokenLedger] = useState<TokenLedger>(emptyTokenLedger)
  const [, setLastTokenBudget] = useState<PromptTokenBudget | null>(null)
  const [, setPendingAttachments] = useState<PendingAttachment[]>([])
  const [, setPendingTextInputs] = useState<PendingTextInput[]>([])
  const handledResetRevision = useRef(0)
  const restoredCheckpointRef = useRef(false)
  const modelConfigRef = useRef(config)
  const engineSyncInFlightRef = useRef(false)
  const modelConfigRecoveryPendingRef = useRef(false)
  const rejectedBrowserModelConfigRef = useRef<string | null>(null)
  const contextRef = useRef(context)
  const activeStrategyRequestRef = useRef<string | null>(null)
  const activeStrategyAbortRef = useRef<AbortController | null>(null)
  const runAgentLoopRef = useRef<((args: AgentLoopArgs) => Promise<void>) | null>(null)
  const dispatchUserTurnRef = useRef<((content: string, attachments: PendingAttachment[], displayContent: string, focusedTask?: FocusedAgentTaskContext) => Promise<void>) | null>(null)
  const backendAgentSessionRef = useRef<BackendAgentSession | null>(null)
  const backendAgentSubscriptionRef = useRef<(() => void) | null>(null)
  const attachBackendAgentSessionRef = useRef<(session: BackendAgentSession) => void>(() => undefined)
  const executeBackendUiActionRef = useRef<(session: BackendAgentSession, payload: Record<string, unknown>) => Promise<void>>(async () => undefined)
  const webShellRef = useRef<AgentWebShellHandle | null>(null)
  const agentTaskAbortRef = useRef<AbortController | null>(null)
  const agentTaskGenerationRef = useRef(0)
  const agentTaskTargetStateRef = useRef({ available: false, error: '', adapterId: '', sessionId: '' })
  const pendingAnalysisSessionRestoreRef = useRef<{ analysisId: string; runId: string } | null>(null)
  const artifactDeliveryNotifierRef = useRef<ArtifactDeliveryNotifier | null>(null)
  const webShellProgressRef = useRef<AgentTaskProgressContext | undefined>(undefined)
  const [webShellTerminal, setWebShellTerminal] = useState<AgentCliTerminal | null>(null)
  const [webShellState, setWebShellState] = useState<AgentWebShellState>('connecting')
  const [webShellTransport, setWebShellTransport] = useState<AgentWebShellTransportStatus>({
    state: 'initializing',
    detail: '正在初始化浏览器到 PTY 的输出链路。',
    lastConnectedAt: null,
    disconnectedAt: null,
    retryAttempt: 0,
  })
  const [webShellDetail, setWebShellDetail] = useState('正在读取 Agent Adapter 目录；PTY 尚未启动。')
  const [webShellTokenUsage, setWebShellTokenUsage] = useState<AgentCliTokenUsage | null>(null)
  const credentialRecoveryRunIdsRef = useRef(new Set<string>())
  const pendingAttachmentsRef = useRef(new Map<string, PendingAttachment>())
  const pendingAttachmentListRef = useRef<PendingAttachment[]>([])
  const pendingTextInputsRef = useRef(new Map<string, PendingTextInput>())
  const pendingTextInputListRef = useRef<PendingTextInput[]>([])
  const traceInputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    const notifier = new ArtifactDeliveryNotifier()
    artifactDeliveryNotifierRef.current = notifier
    const detach = notifier.attach()
    return () => {
      detach()
      if (artifactDeliveryNotifierRef.current === notifier) artifactDeliveryNotifierRef.current = null
    }
  }, [])

  useEffect(() => {
    const sync = (event?: Event) => {
      const detail = event instanceof CustomEvent ? event.detail : undefined
      const next = detail && typeof detail === 'object' ? detail as BrowserModelConfig : readBrowserModelConfig()
      const normalized = {
        ...next,
        contextWindowTokens: normalizeContextWindowTokens(next.contextWindowTokens),
        agentMaxOutputTokens: normalizeAgentMaxOutputTokens(next.agentMaxOutputTokens, next.contextWindowTokens),
        agentMaxTurns: normalizeAgentMaxTurns(next.agentMaxTurns),
        agentMaxActionsPerTurn: normalizeAgentMaxActionsPerTurn(next.agentMaxActionsPerTurn),
        agentExecutionPolicy: normalizeAgentExecutionPolicy(next.agentExecutionPolicy),
      }
      setConfig(normalized)
    }
    sync()
    window.addEventListener(modelConfigChangedEvent, sync)
    return () => {
      window.removeEventListener(modelConfigChangedEvent, sync)
    }
  }, [])

  const actionIds = useMemo(() => new Set(actions.map((action) => action.action_id)), [actions])
  const connected = engineConnection === 'ready'
  const selectedProvider = providerPresetId(config)
  const selectedAgentAdapter = useMemo(
    () => agentCliAdapters.find((adapter) => adapter.adapter_id === selectedAgentAdapterId) ?? null,
    [agentCliAdapters, selectedAgentAdapterId],
  )
  const selectedAgentDisplayName = selectedAgentAdapter?.display_name ?? webShellTerminal?.agent_display_name ?? 'Agent CLI'
  const selectedAgentMcpState = !selectedAgentAdapter
    ? agentCliCatalogState === 'error'
      ? 'error'
      : agentCliCatalogState === 'retrying'
        ? 'retrying'
        : 'loading'
    : selectedAgentAdapter.mcp_integration.warning
      ? 'warning'
      : selectedAgentAdapter.mcp_integration.configured
        ? 'ready'
        : 'missing'
  const selectedAgentMcpTitle = selectedAgentAdapter
    ? selectedAgentAdapter.mcp_integration.warning
      || (selectedAgentAdapter.mcp_integration.configured
        ? `MCP ${selectedAgentAdapter.mcp_integration.format} 配置已投影到 ${selectedAgentAdapter.mcp_integration.path ?? 'Agent 工作区'}；${selectedAgentAdapter.mcp_integration.delivery}。新配置需要新建 PTY Session 才会装载。`
        : '当前 Agent Adapter 没有配置平台 MCP。')
    : agentCliCatalogDetail
  const selectedAgentMcpLabel = !selectedAgentAdapter
    ? agentCliCatalogState === 'error'
      ? 'Agent 目录不可用'
      : agentCliCatalogState === 'retrying'
        ? 'Agent 目录重试中'
        : 'Agent 目录加载中'
    : selectedAgentMcpState === 'ready'
      ? 'MCP 已投影'
      : selectedAgentMcpState === 'warning'
        ? 'MCP 待确认'
        : 'MCP 未配置'
  const agentCliConnected = Boolean(selectedAgentAdapter?.available) && (selectedAgentAdapterId !== 'kimi' || connected)
  const agentCliBlockedDetail = !selectedAgentAdapter
    ? agentCliCatalogDetail
    : !selectedAgentAdapter.available
      ? `${selectedAgentAdapter.display_name} 当前不可用：${selectedAgentAdapter.diagnostic ?? '后端未提供可执行入口。'}`
      : selectedAgentAdapterId === 'kimi' && !connected
        ? engineConnection === 'checking'
          ? 'Agent Adapter 目录已加载，正在检查 Agent Engine 与模型配置；PTY 尚未启动。'
          : engineConnection === 'model_configuration_required'
            ? 'Agent Engine 已启动，但模型尚未配置；PTY 将在模型可用后启动。'
            : 'Agent Engine 当前不可用；主工作台仍可使用，请检查 API 与 Kimi 服务日志。'
        : ''
  agentTaskTargetStateRef.current = {
    available: agentCliConnected,
    adapterId: selectedAgentAdapter?.adapter_id ?? '',
    sessionId: webShellTerminal?.adapter_id === selectedAgentAdapterId ? webShellTerminal.terminal_session_id : '',
    error: selectedAgentAdapter && (!selectedAgentAdapter.available || (selectedAgentAdapterId === 'kimi' && engineConnection === 'model_configuration_required'))
      ? agentCliBlockedDetail : '',
  }
  useEffect(() => {
    const controller = new AbortController()
    agentTaskAbortRef.current = controller
    return () => controller.abort()
  }, [])
  const pageAgentSubmissionGuard = useCallback(() => {
    const signal = agentTaskAbortRef.current?.signal
    return createAgentSubmissionGuard(() => ({
      ...agentTaskTargetStateRef.current,
      generation: agentTaskGenerationRef.current,
      disposed: signal?.aborted,
    }))
  }, [])
  const waitForPageAgent = useCallback((assertCurrent: () => void) => waitForAgentTarget(() => {
    assertCurrent()
    return {
      target: agentTaskTargetStateRef.current.available ? webShellRef.current : null,
      error: agentTaskTargetStateRef.current.error,
    }
  }, { signal: agentTaskAbortRef.current?.signal }), [])
  const selectedDeviceBridge = useMemo(
    () => deviceBridges.find((bridge) => bridge.bridge_id === selectedDeviceBridgeId) ?? null,
    [deviceBridges, selectedDeviceBridgeId],
  )
  const isScenarioCapturePage = context.active_view === 'scenario_capture'
  const captureDeviceSelection = captureSelectionView(asRecord(context.scenario_capture).device_selection)
  const selectedBridgeDiagnostics = selectedDeviceBridge?.capabilities?.runtime_diagnostics
  const selectedBridgeHdc = selectedBridgeDiagnostics?.hdc
  const selectedBridgeHealth = selectedDeviceBridge
    ? selectedDeviceBridge.connection_state !== 'connected'
      ? '远程采集端当前离线；请确认 Windows 采集进程仍在运行。'
      : selectedBridgeDiagnostics?.status === 'ready'
        ? `采集端 ${selectedDeviceBridge.capabilities?.implementation_version ?? '版本未知'} · HDC ${selectedBridgeHdc?.version?.stdout?.trim().split(/\r?\n/, 1)[0] || '可执行'} · ${selectedBridgeHdc?.target_count ?? 0} 台设备 · spool 可写`
        : selectedBridgeDiagnostics?.issues?.[0]?.message_zh
          ?? `远程采集端已连接；${selectedBridgeDiagnostics ? '诊断需要关注' : '等待采集端上报运行诊断'}。`
    : ''
  const totalInputTokens = (webShellTokenUsage?.input_tokens ?? 0) + tokenLedger.inputTokens
  const totalOutputTokens = (webShellTokenUsage?.output_tokens ?? 0) + tokenLedger.outputTokens
  const totalConsumedTokens = totalInputTokens + totalOutputTokens
  const totalCachedInputTokens = (webShellTokenUsage?.cached_input_tokens ?? 0) + tokenLedger.cachedInputTokens
  const cacheReportedInputTokens = (webShellTokenUsage?.input_tokens ?? 0) + tokenLedger.cacheReportedInputTokens
  const cumulativeCacheHitRate = cacheReportedInputTokens > 0
    ? totalCachedInputTokens / cacheReportedInputTokens
    : null
  const totalModelCalls = (webShellTokenUsage?.model_calls ?? 0) + tokenLedger.calls
  const cliTokenUsageAvailable = Boolean(webShellTokenUsage && webShellTokenUsage.source !== 'unavailable')
  const cliTokenUsageConfigured = Boolean(selectedAgentAdapter?.capabilities.token_usage && selectedAgentAdapter.usage_kind !== 'unavailable')
  const platformTokenUsageAvailable = tokenLedger.calls > 0
  const tokenUsageAvailable = cliTokenUsageAvailable || platformTokenUsageAvailable
  const tokenUsageStatus = totalModelCalls > 0
    ? `${totalModelCalls} 次模型调用${tokenLedger.estimatedCalls > 0 ? ` · ${tokenLedger.estimatedCalls} 次含估算` : ''}${!cliTokenUsageAvailable && platformTokenUsageAvailable ? ' · 仅平台调用' : ''}`
    : cliTokenUsageAvailable
      ? '等待首轮模型返回'
      : cliTokenUsageConfigured
      ? webShellTokenUsage ? '等待首轮模型返回' : '正在读取 Agent 用量'
      : '当前 Agent 未提供 Token 用量'

  useEffect(() => {
    modelConfigRef.current = config
    const session = backendAgentSessionRef.current
    if (session?.state === 'waiting_for_model_credential' && config.apiKey.trim() && config.baseUrl.trim() && config.model.trim()) {
      void recoverBackendCredential(session)
    }
    // Credential recovery is keyed by the persisted Run and current config;
    // the in-flight set prevents duplicate resume calls from SSE and effects.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [config])

  // The terminal's rendered runtime context must include the current selection,
  // including a change immediately followed by a manual message.
  contextRef.current = context

  useEffect(() => {
    const savedSelection = readDeviceBridgeSelection()
    if (savedSelection) setSelectedDeviceBridgeId(savedSelection)
    void refreshDeviceBridges()
    const refreshTimer = window.setInterval(() => void refreshDeviceBridges(true), 15_000)
    return () => window.clearInterval(refreshTimer)
  }, [])

  useEffect(() => {
    let active = true
    let retryTimer: ReturnType<typeof setTimeout> | null = null
    let attempt = 0

    const load = async () => {
      if (!active) return
      setAgentCliCatalogState(attempt === 0 ? 'loading' : 'retrying')
      if (attempt === 0) setAgentCliCatalogDetail('正在读取 Agent Adapter 目录；PTY 尚未启动。')
      try {
        const catalog = await listAgentCliAdapters()
        if (!active) return
        setAgentCliAdapters(catalog.adapters)
        const persisted = window.localStorage.getItem(agentCliAdapterStorageKey)?.trim()
        const persistedAdapter = catalog.adapters.find((adapter) => adapter.adapter_id === persisted && adapter.available)
        const defaultAdapter = catalog.adapters.find((adapter) => adapter.adapter_id === catalog.default_adapter_id && adapter.available)
        const selected = persistedAdapter
          ?? defaultAdapter
          ?? catalog.adapters.find((adapter) => adapter.available)
        if (!selected) {
          setAgentCliCatalogState('error')
          setAgentCliCatalogDetail('Agent Adapter 目录已返回，但没有可用的 Agent CLI；请检查后端 Adapter 配置。')
          return
        }
        setSelectedAgentAdapterId(selected.adapter_id)
        setAgentCliCatalogState('ready')
        const selectionDetail = persistedAdapter && persistedAdapter.adapter_id !== catalog.default_adapter_id
          ? `浏览器保留了 ${persistedAdapter.display_name}；后端默认项为 ${defaultAdapter?.display_name ?? catalog.default_adapter_id}。`
          : `已选择 ${selected.display_name}。`
        const manifestDetail = catalog.manifest_error
          ? ` 自定义 Adapter 清单加载失败：${catalog.manifest_error.code}，${catalog.manifest_error.message}（${catalog.manifest_error.path}）。`
          : ''
        setAgentCliCatalogDetail(`Agent Adapter 目录已加载，${selectionDetail}${manifestDetail}`)
      } catch (error) {
        if (!active) return
        attempt += 1
        const delayMs = Math.min(10_000, attempt === 1 ? 500 : 1_000 * (2 ** Math.min(attempt - 2, 3)))
        setAgentCliCatalogState('retrying')
        setAgentCliCatalogDetail(`Agent Adapter 目录请求失败：${agentCliCatalogDiagnostic(error)} ${Math.round(delayMs / 100) / 10} 秒后自动重试；PTY 尚未启动，主工作台不受影响。`)
        retryTimer = setTimeout(() => void load(), delayMs)
      }
    }

    void load()
    return () => {
      active = false
      if (retryTimer) clearTimeout(retryTimer)
    }
  }, [agentCliCatalogReloadRevision])

  useEffect(() => {
    let active = true
    let retryTimer: ReturnType<typeof setTimeout> | null = null
    const schedule = (delayMs: number) => {
      if (!active) return
      retryTimer = setTimeout(check, delayMs)
    }
    const check = async () => {
      if (engineSyncInFlightRef.current) {
        schedule(1000)
        return
      }
      engineSyncInFlightRef.current = true
      try {
        let value = await getBackendAgentEngineHealth()
        if (!active) return
        const browserConfig = readBrowserModelConfig()
        const browserConfigured = Boolean(
          browserConfig.apiKey.trim() && browserConfig.baseUrl.trim() && browserConfig.model.trim(),
        )
        const browserConfigSignature = browserModelConfigSignature(browserConfig)
        const browserConfigRejected = rejectedBrowserModelConfigRef.current === browserConfigSignature
        const leaseNearExpiry = value.engine.credential_mode === 'browser_ephemeral'
          && typeof value.engine.expires_in_seconds === 'number'
          && value.engine.expires_in_seconds <= 5 * 60
        const browserOverrideMissing = browserConfigured
          && !browserConfigRejected
          && value.engine.credential_mode !== 'browser_ephemeral'
        if (browserConfigured && (leaseNearExpiry || browserOverrideMissing)) {
          try {
            value = await configureBackendAgentEngine(browserConfig)
            rejectedBrowserModelConfigRef.current = null
            if (!active) return
          } catch (error) {
            if (retryableModelConfigurationError(error)) throw error
            rejectedBrowserModelConfigRef.current = browserConfigSignature
            modelConfigRecoveryPendingRef.current = false
            const state = engineConnectionState(value.engine)
            setEngineConnection(state)
            if (state === 'ready') setConnectionState('ok')
            schedule(30_000)
            return
          }
        }
        const state = engineConnectionState(value.engine)
        setEngineConnection(state)
        if (state === 'ready') {
          setConnectionState('ok')
          if (value.engine.credential_mode === 'browser_ephemeral' && modelConfigRecoveryPendingRef.current) {
            modelConfigRecoveryPendingRef.current = false
            setMessages((current) => [...current, {
              id: randomUuid(),
              role: 'system',
              content: '后端服务已经恢复，浏览器模型配置现已在 Kimi Agent Engine 中生效。',
            }])
          }
        }
        schedule(state === 'unavailable' ? 2500 : 30_000)
      } catch {
        if (!active) return
        setEngineConnection('unavailable')
        schedule(2500)
      } finally {
        engineSyncInFlightRef.current = false
      }
    }
    void check()
    return () => {
      active = false
      if (retryTimer) clearTimeout(retryTimer)
    }
  }, [])

  useEffect(() => {
    storeDeviceBridgeSelection(selectedDeviceBridgeId)
  }, [selectedDeviceBridgeId])

  // Compatibility restoration for legacy structured Agent Runs. The real
  // Agent CLI has its own page-scoped sessionStorage lifecycle in AgentWebShell.
  /* oxlint-disable react-hooks/exhaustive-deps */
  useEffect(() => {
    if (restoredCheckpointRef.current) return
    const routeAnalysisId = readAnalysisRouteId()
    if (!routeAnalysisId) {
      // A bare workbench URL deliberately starts clean. Durable Runs remain
      // auditable on the backend, but only an explicit analysis_id may attach
      // one to a freshly loaded browser page.
      restoredCheckpointRef.current = true
      storeAgentCheckpoint(null)
      storeBackendAgentSession(null)
      return
    }
    const reasoningLab = asRecord(context.reasoning_lab)
    const activeAnalysisId = normalizeAnalysisRouteId(reasoningLab.analysis_record_id)
    if (activeAnalysisId !== routeAnalysisId) return
    restoredCheckpointRef.current = true
    const checkpoint = readAgentCheckpoint(routeAnalysisId)
    const persistedSession = readBackendAgentSession(routeAnalysisId)
    if (!checkpoint && !persistedSession) {
      void getAnalysisAgentSession(routeAnalysisId).then((run) => {
        if (!run) return
        const session: BackendAgentSession = {
          runId: run.run_id,
          analysisId: routeAnalysisId,
          origin: '已恢复的优化分析会话',
          lastSequence: 0,
          reasoningPreview: '',
          contentPreview: '',
          handledUiRequests: new Set(),
          state: run.state,
        }
        backendAgentSessionRef.current = session
        setLastBackendRunId(session.runId)
        storeBackendAgentSession(session)
        if (!backendRunIsTerminal(run.state)) {
          attachBackendAgentSession(session)
          setAgentRun(agentRunStateFromBackend(run))
          setBusy(run.state === 'running' || run.state === 'waiting_for_tool' || run.state === 'waiting_for_device' || run.state === 'compacting')
        }
      })
      return
    }
    const session = persistedSession ?? (checkpoint?.backendRunId
      ? {
          runId: checkpoint.backendRunId,
          analysisId: checkpoint.analysisId,
          origin: checkpoint.origin,
          progress: checkpoint.progress,
          lastSequence: checkpoint.backendEventSequence ?? 0,
          reasoningPreview: '',
          contentPreview: '',
          handledUiRequests: new Set<string>(),
          state: 'created' as const,
        }
      : null)
    const effectiveCheckpoint = checkpoint ?? (session
      ? backendContinuation(session, 'interrupted', 1)
      : null)
    if (!effectiveCheckpoint) return
    setAgentContinuation(effectiveCheckpoint)
    setConversationMemory(effectiveCheckpoint.memory)
    setMessages((current) => [...current, {
      id: randomUuid(),
      role: 'system',
      content: effectiveCheckpoint.reason === 'model_error'
        ? '检测到上次因模型服务失败暂停的任务。真实工具结果和下一轮输入仍在；可点击继续按钮，或发送新消息让模型判断如何处理该检查点。'
        : effectiveCheckpoint.reason === 'turn_budget'
          ? '检测到尚未完成的自主任务。已恢复上一轮真实工具结果，可以继续推理。'
          : '检测到浏览器中断前保存的自主任务检查点，可以从原位置继续。',
    }])
    if (!session) return
    backendAgentSessionRef.current = session
    setLastBackendRunId(session.runId)
    void getBackendAgentRun(session.runId)
      .then((run) => {
        session.state = run.state
        storeBackendAgentSession(session)
        if (backendRunIsTerminal(run.state)) {
          backendAgentSessionRef.current = session
          storeBackendAgentSession(session)
          setAgentContinuation(null)
          storeAgentCheckpoint(null)
          setAgentRun(null)
          setBusy(false)
          return
        }
        attachBackendAgentSession(session)
        setAgentRun(agentRunStateFromBackend(run))
        setAgentStatusText(run.state_detail)
        if (run.state === 'waiting_for_model_credential' && modelConfigRef.current.apiKey.trim() && modelConfigRef.current.baseUrl.trim() && modelConfigRef.current.model.trim()) {
          setAgentContinuation(null)
          storeAgentCheckpoint(null)
          void recoverBackendCredential(session)
          return
        }
        if (run.state === 'paused_model_overloaded' || run.state === 'paused_output_limit' || run.state === 'paused_turn_budget' || run.state === 'paused_bridge_offline' || run.state === 'waiting_for_model_credential') {
          const reason: AgentContinuation['reason'] = run.state === 'paused_turn_budget'
            ? 'turn_budget'
            : run.state === 'paused_bridge_offline'
              ? 'bridge_offline'
              : run.state === 'paused_output_limit'
                ? 'output_limit'
                : run.state === 'waiting_for_model_credential'
                  ? 'credential'
                  : 'model_error'
          const resumed = backendContinuation(session, reason, Math.max(1, run.current_turn || effectiveCheckpoint.resumeTurn))
          setAgentContinuation(resumed)
          storeAgentCheckpoint(resumed)
          setBusy(false)
        } else {
          setAgentContinuation(null)
          storeAgentCheckpoint(null)
        }
        if (run.state === 'running' || run.state === 'waiting_for_tool' || run.state === 'waiting_for_device' || run.state === 'compacting') {
          setBusy(true)
        }
        if (run.state === 'waiting_for_ui') {
          setBusy(run.pending_ui_actions.length > 0)
          for (const pending of run.pending_ui_actions) {
            void executeBackendUiAction(session, pending)
          }
        }
      })
      .catch(() => {
        // A backend retention policy may have removed an old run. Keep the
        // browser checkpoint visible; the user can still resume it as a new
        // run without losing the original user intent.
        setMessages((current) => [...current, {
          id: randomUuid(),
          role: 'system',
          content: '未能重新连接已保存的后端 Agent Run；已保留本地检查点，可继续创建新的后端任务。',
        }])
      })
  }, [context])
  /* oxlint-enable react-hooks/exhaustive-deps */

  useEffect(() => {
    setStrategyProgressExpanded(false)
  }, [strategyProgress?.requestId])

  useEffect(() => () => {
    backendAgentSubscriptionRef.current?.()
    backendAgentSubscriptionRef.current = null
  }, [])

  useEffect(() => {
    if (!strategyProgress || strategyProgress.status !== 'running') {
      setStrategyProgressElapsedSeconds(0)
      return
    }
    const updateElapsed = () => {
      const started = Date.parse(strategyProgress.startedAt)
      setStrategyProgressElapsedSeconds(Number.isFinite(started) ? Math.max(0, Math.floor((Date.now() - started) / 1000)) : 0)
    }
    updateElapsed()
    const intervalId = window.setInterval(updateElapsed, 1000)
    return () => window.clearInterval(intervalId)
  }, [strategyProgress])

  useEffect(() => {
    if (workflow.stage !== 'idle' || workflow.revision === 0 || workflow.revision <= handledResetRevision.current) return
    handledResetRevision.current = workflow.revision
    activeStrategyRequestRef.current = null
    backendAgentSubscriptionRef.current?.()
    backendAgentSubscriptionRef.current = null
    backendAgentSessionRef.current = null
    clearAnalysisRouteId()
    storeBackendAgentSession(null)
    setStrategyRequestBusy(false)
    setStrategyProgress(null)
    setStrategyProgressExpanded(false)
    setMessages([initialMessage])
    setConversationMemory(emptyConversationMemory)
    setAgentContinuation(null)
    setAgentRun(null)
    storeAgentCheckpoint(null)
    pendingAttachmentsRef.current.clear()
    pendingAttachmentListRef.current = []
    setPendingAttachments([])
    pendingTextInputsRef.current.clear()
    pendingTextInputListRef.current = []
    setPendingTextInputs([])
    setLastTokenBudget(null)
  }, [workflow.revision, workflow.stage])

  function recordModelUsage(usage: ModelTokenUsage, budget: PromptTokenBudget) {
    const actualBudget = createPromptTokenBudget({
      contextWindowTokens: budget.contextWindowTokens,
      inputTokens: usage.inputTokens,
      outputReserveTokens: budget.outputReserveTokens,
      compactionApplied: budget.compactionApplied,
      compactedItems: budget.compactedItems,
    })
    setLastTokenBudget(actualBudget)
    setTokenLedger((current) => ({
      inputTokens: current.inputTokens + usage.inputTokens,
      outputTokens: current.outputTokens + usage.outputTokens,
      cachedInputTokens: current.cachedInputTokens + (usage.cachedInputTokens ?? 0),
      cacheReportedInputTokens: current.cacheReportedInputTokens + (usage.cachedInputTokens === undefined ? 0 : usage.inputTokens),
      calls: current.calls + 1,
      estimatedCalls: current.estimatedCalls + (usage.inputSource === 'estimated' || usage.outputSource === 'estimated' ? 1 : 0),
      cacheReportedCalls: current.cacheReportedCalls + (usage.cachedInputTokens === undefined ? 0 : 1),
    }))
    return actualBudget
  }

  async function refreshDeviceBridges(silent = false) {
    if (!silent) {
      setDeviceBridgeBusy('refresh')
      setDeviceBridgeNotice('')
    }
    try {
      const bridges = await listBackendDeviceBridges()
      setDeviceBridges(bridges)
      setSelectedDeviceBridgeId((current) => {
        if (current && bridges.some((bridge) => bridge.bridge_id === current && bridge.connection_state === 'connected')) return current
        const persisted = readDeviceBridgeSelection()
        if (persisted && bridges.some((bridge) => bridge.bridge_id === persisted && bridge.connection_state === 'connected')) return persisted
        const connected = bridges.filter((bridge) => bridge.connection_state === 'connected')
        return connected.length === 1 ? connected[0].bridge_id : ''
      })
      const connectedCount = bridges.filter((bridge) => bridge.connection_state === 'connected').length
      if (!silent) {
        setDeviceBridgeNotice(
          bridges.length === 0
            ? '当前没有可用的远程采集端。'
            : connectedCount === 1
              ? '已自动选择唯一在线的远程采集端。'
              : `已读取 ${bridges.length} 个远程采集端，其中 ${connectedCount} 个在线。`,
        )
      }
    } catch (error) {
      if (!silent) setDeviceBridgeNotice(`读取远程采集端失败：${error instanceof Error ? error.message : '未知错误'}。`)
    } finally {
      if (!silent) setDeviceBridgeBusy(null)
    }
  }

  useEffect(() => {
    let disposed = false
    const listener = (event: Event) => {
      const detail = asRecord((event as CustomEvent<unknown>).detail)
      const requestId = typeof detail.request_id === 'string' ? detail.request_id : ''
      const analysisId = typeof detail.analysis_id === 'string' ? detail.analysis_id : ''
      const runId = typeof detail.run_id === 'string' ? detail.run_id : requestId
      const strategyLabel = typeof detail.strategy_label === 'string' ? detail.strategy_label : '所选策略'
      const operation = typeof detail.operation === 'string' && detail.operation.trim()
        ? detail.operation.trim()
        : `opportunity_strategy.${typeof detail.strategy_id === 'string' ? detail.strategy_id : 'unknown'}`
      const systemPrompt = typeof detail.system_prompt === 'string' ? detail.system_prompt : ''
      const userPrompt = typeof detail.user_prompt === 'string' ? detail.user_prompt : ''
      if (!requestId || !systemPrompt || !userPrompt) return
      const activeConfig = modelConfigRef.current
      const requestMessages: TokenMessage[] = [
        { role: 'system', content: systemPrompt },
        { role: 'user', content: userPrompt },
      ]
      const requestBudget = promptBudgetFromEvent(detail.token_budget, activeConfig.contextWindowTokens, requestMessages)
      const initialProgress = parseStrategyProgress(detail.analysis_progress)

      const respond = (payload: Record<string, unknown>) => {
        window.dispatchEvent(new CustomEvent('reasoning-lab-agent-response', {
          detail: { request_id: requestId, ...payload },
        }))
      }

      activeStrategyAbortRef.current?.abort()
      const abortController = new AbortController()
      activeStrategyAbortRef.current = abortController
      activeStrategyRequestRef.current = requestId
      setStrategyRequestBusy(true)
      if (initialProgress) setStrategyProgress(initialProgress)
      setLastTokenBudget(requestBudget)
      setMessages((current) => [...current, {
        id: randomUuid(),
        role: 'assistant',
        content: `正在使用“${strategyLabel}”分析已选热点代码，并在返回后校验结构化结论。`,
      }])
      void (async () => {
        let handedOffForResultProcessing = false
        let reasoningPreview = ''
        let contentPreview = ''
        let lastProgressUpdate = 0
        try {
          const payload = await requestModelChatCompletionStream(activeConfig, requestMessages, {
            maxTokens: requestBudget.outputReserveTokens,
            signal: abortController.signal,
            observability: {
              analysisId,
              operation,
              requestId,
              runId,
            },
            onRetry: (retry) => {
              if (disposed) return
              reasoningPreview = ''
              contentPreview = ''
              window.dispatchEvent(new CustomEvent('reasoning-lab-agent-heartbeat', { detail: { request_id: requestId } }))
              setStrategyProgress((current) => current?.requestId === requestId
                ? {
                  ...current,
                  status: 'running',
                  headline: '上游模型服务繁忙，正在自动恢复',
                  detail: `第 ${retry.failedAttempt} 次请求未成功，${Math.ceil(retry.delayMs / 1000)} 秒后自动进行第 ${retry.nextAttempt}/${retry.maxAttempts} 次尝试；本轮上下文不会丢失。`,
                }
                : current)
            },
            onProgress: (progress) => {
              if (disposed) return
              reasoningPreview = `${reasoningPreview}${progress.reasoningDelta}`.slice(-1200)
              contentPreview = `${contentPreview}${progress.contentDelta}`.slice(-1200)
              window.dispatchEvent(new CustomEvent('reasoning-lab-agent-heartbeat', { detail: { request_id: requestId } }))
              const now = Date.now()
              if (now - lastProgressUpdate < 120) return
              lastProgressUpdate = now
              const preview = [
                reasoningPreview ? `[思考]\n${reasoningPreview}` : '',
                contentPreview ? `[输出]\n${contentPreview}` : '',
                !reasoningPreview && !contentPreview ? '正在等待模型的首个流式片段。' : '',
              ].filter(Boolean).join('\n\n')
              setStrategyProgress((current) => current?.requestId === requestId
                ? {
                  ...current,
                  detail: '以下内容由机会推理模型实时生成。',
                  streamPreview: preview,
                  streamedReasoningChars: progress.reasoningChars,
                  streamedContentChars: progress.contentChars,
                  steps: current.steps.map((step) => step.id === 'model'
                    ? { ...step, detail: '正在接收机会推理模型的实时生成内容。' }
                    : step),
                }
                : current)
            },
          })
          const rawOutput = payload.choices?.[0]?.message?.content ?? ''
          if (disposed) return
          const actualBudget = recordModelUsage(payload.tokenUsage, requestBudget)
          setStrategyProgress((current) => current?.requestId === requestId
            ? advanceStrategyProgress(current, 'response_received', '模型文本已收到，正在由推理实验室解析 JSON、核验证据引用并回填策略结果。')
            : current)
          respond({
            raw_output: rawOutput,
            // A reasoning-model provider can finish its side channel before
            // it emits final content. The lab treats this as a transient,
            // bounded JSON-recovery source rather than a user-visible answer.
            ...(payload.reasoningContent ? { reasoning_output: payload.reasoningContent } : {}),
            token_usage: payload.tokenUsage,
            token_budget: actualBudget,
          })
          handedOffForResultProcessing = true
        } catch (error) {
          if (disposed) return
          const stopped = abortController.signal.aborted || (error instanceof DOMException && error.name === 'AbortError')
          const message = stopped ? '推理已由用户停止。' : error instanceof Error ? error.message : '模型请求失败'
          respond({ error: message })
          activeStrategyRequestRef.current = null
          setStrategyProgress((current) => {
            if (!current || current.requestId !== requestId) return current
            const next = advanceStrategyProgress(current, 'failed', message)
            if (!next) return current
            return {
              ...next,
              status: stopped ? 'warning' : 'failed',
              headline: stopped ? '推理已停止' : '推理未完成',
            }
          })
          setMessages((current) => [...current, {
            id: randomUuid(),
            role: 'system',
            content: `“${strategyLabel}”未完成：${message}`,
          }])
        } finally {
          if (activeStrategyAbortRef.current === abortController) activeStrategyAbortRef.current = null
          if (!disposed && !handedOffForResultProcessing) setStrategyRequestBusy(false)
        }
      })()
    }
    const completionListener = (event: Event) => {
      const detail = asRecord((event as CustomEvent<unknown>).detail)
      const requestId = typeof detail.request_id === 'string' ? detail.request_id : ''
      if (!requestId || activeStrategyRequestRef.current !== requestId) return
      activeStrategyRequestRef.current = null
      activeStrategyAbortRef.current = null
      setStrategyRequestBusy(false)
      const strategyLabel = typeof detail.strategy_label === 'string' ? detail.strategy_label : '所选策略'
      if (detail.status === 'succeeded') {
        const conclusion = typeof detail.conclusion === 'string' && detail.conclusion.trim()
          ? detail.conclusion
          : `“${strategyLabel}”已返回分析文本，并已回填策略结果与证据校验。`
        setMessages((current) => [...current, {
          id: randomUuid(),
          role: 'assistant',
          content: conclusion,
        }])
        return
      }
      const message = typeof detail.error === 'string' && detail.error ? detail.error : '策略结果未能完成回填。'
      setMessages((current) => [...current, {
        id: randomUuid(),
        role: 'system',
        content: `“${strategyLabel}”已返回分析文本，但未能完成结果回填：${message}`,
      }])
    }
    const progressListener = (event: Event) => {
      const next = parseStrategyProgress((event as CustomEvent<unknown>).detail)
      if (!next) return
      setStrategyProgress((current) => {
        if (!current || current.requestId === next.requestId) return next
        const currentStartedAt = Date.parse(current.startedAt)
        const nextStartedAt = Date.parse(next.startedAt)
        if (!Number.isFinite(currentStartedAt) || !Number.isFinite(nextStartedAt)) {
          return current.status === 'running' ? current : next
        }
        return nextStartedAt >= currentStartedAt ? next : current
      })
    }
    const extraTokenUsageListener = (event: Event) => {
      const detail = asRecord((event as CustomEvent<unknown>).detail)
      const usage = modelTokenUsage(detail.token_usage)
      if (!usage) return
      const budget = promptBudgetFromEvent(
        detail.token_budget,
        modelConfigRef.current.contextWindowTokens,
        [{ role: 'system', content: '额外结构化处理调用' }],
      )
      recordModelUsage(usage, budget)
    }
    window.addEventListener('reasoning-agent-analysis-request', listener)
    window.addEventListener('reasoning-lab-agent-analysis-completed', completionListener)
    window.addEventListener('reasoning-lab-agent-analysis-progress', progressListener)
    window.addEventListener('reasoning-agent-extra-token-usage', extraTokenUsageListener)
    return () => {
      disposed = true
      activeStrategyAbortRef.current?.abort()
      activeStrategyAbortRef.current = null
      window.removeEventListener('reasoning-agent-analysis-request', listener)
      window.removeEventListener('reasoning-lab-agent-analysis-completed', completionListener)
      window.removeEventListener('reasoning-lab-agent-analysis-progress', progressListener)
      window.removeEventListener('reasoning-agent-extra-token-usage', extraTokenUsageListener)
    }
  }, [])

  function updateConfig(next: BrowserModelConfig) {
    const contextWindowTokens = normalizeContextWindowTokens(next.contextWindowTokens)
    const normalized = {
      ...next,
      contextWindowTokens,
      agentMaxOutputTokens: normalizeAgentMaxOutputTokens(next.agentMaxOutputTokens, contextWindowTokens),
      agentMaxTurns: normalizeAgentMaxTurns(next.agentMaxTurns),
      agentMaxActionsPerTurn: normalizeAgentMaxActionsPerTurn(next.agentMaxActionsPerTurn),
      agentExecutionPolicy: normalizeAgentExecutionPolicy(next.agentExecutionPolicy),
    }
    setConfig(normalized)
    setConnectionState('idle')
  }

  function updateExecutionPolicy(value: unknown) {
    const policy = normalizeAgentExecutionPolicy(value)
    setConfig((current) => ({ ...current, agentExecutionPolicy: policy }))
    writeBrowserModelConfig({ ...readBrowserModelConfig(), agentExecutionPolicy: policy })
  }

  function selectAgentAdapter(adapterId: string) {
    const adapter = agentCliAdapters.find((item) => item.adapter_id === adapterId)
    if (!adapter?.available) return
    if (adapterId !== selectedAgentAdapterId) agentTaskGenerationRef.current++
    window.localStorage.setItem(agentCliAdapterStorageKey, adapter.adapter_id)
    setSelectedAgentAdapterId(adapter.adapter_id)
    setWebShellTokenUsage(null)
    setWebShellState('connecting')
    setWebShellDetail(`正在切换到 ${adapter.display_name}，当前分析 Run 与页面状态会保留。`)
  }

  function retryAgentCliCatalog() {
    setAgentCliCatalogState('loading')
    setAgentCliCatalogDetail('正在重新读取 Agent Adapter 目录；PTY 尚未启动。')
    setAgentCliCatalogReloadRevision((revision) => revision + 1)
  }

  async function saveConfig() {
    if (!config.baseUrl.trim() || !config.model.trim() || !config.apiKey.trim()) {
      setConnectionState('error')
      setMessages((current) => [...current, { id: randomUuid(), role: 'system', content: '请完整填写模型服务地址、模型名称和 API 密钥。' }])
      return
    }
    writeBrowserModelConfig(config)
    const configSignature = browserModelConfigSignature(config)
    rejectedBrowserModelConfigRef.current = null
    setModelConfigBusy('save')
    setConnectionState('idle')
    if (selectedAgentAdapterId !== 'kimi') {
      try {
        setConnectionState('ok')
        await webShellRef.current?.restart()
        setSettingsOpen(false)
        setMessages((current) => [...current, {
          id: randomUuid(),
          role: 'system',
          content: selectedAgentAdapter?.browser_model_override_supported
            ? `模型配置已保存于当前浏览器，并已临时注入 ${selectedAgentDisplayName} 进程；API 密钥不会写入 Run、工件或 Agent 配置文件。`
            : `${selectedAgentDisplayName} 使用自身模型配置；浏览器配置已保存，但不会覆盖该 Agent。`,
        }])
      } catch (error) {
        setConnectionState('error')
        setMessages((current) => [...current, { id: randomUuid(), role: 'system', content: `${selectedAgentDisplayName} 重启失败：${error instanceof Error ? error.message : '未知错误'}` }])
      } finally {
        setModelConfigBusy(null)
      }
      return
    }
    setEngineConnection('checking')
    try {
      const value = await configureBackendAgentEngine(config)
      const state = engineConnectionState(value.engine)
      const ready = state === 'ready'
      setEngineConnection(state)
      setConnectionState(ready ? 'ok' : 'error')
      if (!ready) throw new Error('Kimi Agent Engine 未返回就绪状态。')
      modelConfigRecoveryPendingRef.current = false
      rejectedBrowserModelConfigRef.current = null
      await webShellRef.current?.restart()
      setSettingsOpen(false)
      const credentialMode = value.engine.credential_mode === 'browser_ephemeral'
        ? value.engine.configuration_reloaded === false
          ? '浏览器模型租约已刷新'
          : '后端已使用新模型配置重载 Agent Engine'
        : '检测到后端已有可用 Kimi 配置，继续使用后端模型'
      const transportNotice = usesUnencryptedModelTransport(config.baseUrl)
        ? ' 当前使用 HTTP，后端到模型服务之间的密钥与请求内容不会被 TLS 加密，请仅用于可信本机或内网。'
        : ''
      const protocolNotice = value.engine.provider_type === 'openai'
        ? '已按 OpenAI 兼容协议完成真实模型调用。'
        : value.engine.provider_type === 'kimi'
          ? '已按 Kimi 原生协议完成真实模型调用。'
          : '已完成真实模型调用。'
      setMessages((current) => [...current, { id: randomUuid(), role: 'system', content: `模型配置已保存于当前浏览器；${credentialMode}，${protocolNotice}API 密钥不会进入分析记录或工件。${transportNotice}` }])
    } catch (error) {
      const retryable = retryableModelConfigurationError(error)
      modelConfigRecoveryPendingRef.current = retryable
      rejectedBrowserModelConfigRef.current = retryable ? null : configSignature
      try {
        const health = await getBackendAgentEngineHealth()
        setEngineConnection(engineConnectionState(health.engine))
      } catch {
        setEngineConnection('unavailable')
      }
      setConnectionState('error')
      const diagnostic = error instanceof ApiError
        ? `${error.code}：${error.message}`
        : error instanceof Error
          ? error.message
          : '未知错误'
      setMessages((current) => [...current, {
        id: randomUuid(),
        role: 'system',
        content: retryable
          ? `本次模型配置请求未完成：${diagnostic}。若真实探针失败，后端会自动撤销临时配置并保留原有可用模型。`
          : `模型配置未生效：${diagnostic}。请修改模型服务地址或配置后重新保存；平台不会重复提交同一无效配置。`,
      }])
    } finally {
      setModelConfigBusy(null)
    }
  }

  async function clearConfig() {
    modelConfigRecoveryPendingRef.current = false
    rejectedBrowserModelConfigRef.current = null
    clearBrowserModelConfig()
    const next = readBrowserModelConfig()
    setConfig(next)
    setConnectionState('idle')
    setModelConfigBusy('clear')
    if (selectedAgentAdapterId !== 'kimi') {
      try {
        await webShellRef.current?.restart()
        setMessages((current) => [...current, { id: randomUuid(), role: 'system', content: `浏览器模型覆盖已清除；${selectedAgentDisplayName} 将回落到后端环境或自身配置。` }])
      } finally {
        setModelConfigBusy(null)
      }
      return
    }
    try {
      const value = await clearBackendAgentEngineCredential()
      const state = engineConnectionState(value.engine)
      setEngineConnection(state)
      setConnectionState(state === 'ready' ? 'ok' : 'idle')
      await webShellRef.current?.restart()
      const fallbackMessage = state === 'ready'
        ? '已自动回落到后端现有模型配置，Agent Engine 继续可用。'
        : state === 'model_configuration_required'
          ? 'Agent Engine 仍由后端运行，目前没有可用模型。'
          : '后端 Agent Engine 当前不可用。'
      setMessages((current) => [...current, { id: randomUuid(), role: 'system', content: `当前浏览器模型配置和后端短期凭证均已清除。${fallbackMessage}` }])
    } catch (error) {
      setMessages((current) => [...current, { id: randomUuid(), role: 'system', content: `浏览器配置已清除，但后端凭证暂未撤销：${error instanceof Error ? error.message : '未知错误'}` }])
    } finally {
      setModelConfigBusy(null)
    }
  }

  async function probeConnection() {
    if (!config.apiKey.trim() || !config.baseUrl.trim() || !config.model.trim()) return
    setModelConfigBusy('probe')
    setConnectionState('idle')
    try {
      await configureBackendAgentEngine(config)
      const probeMessages: TokenMessage[] = [{ role: 'user', content: '仅输出 OK。' }]
      const budget = createPromptTokenBudget({ contextWindowTokens: config.contextWindowTokens, inputTokens: estimateMessageTokens(probeMessages), outputReserveTokens: 8 })
      const payload = await requestModelChatCompletion(config, probeMessages, {
        maxTokens: 8,
        outputBudgetPolicy: 'task_specific',
        retryMaxAttempts: 1,
        operation: 'model_configuration_probe',
      })
      recordModelUsage(payload.tokenUsage, budget)
      setEngineConnection('ready')
      setConnectionState('ok')
    } catch (error) {
      setEngineConnection('unavailable')
      setConnectionState('error')
      setMessages((current) => [...current, { id: randomUuid(), role: 'system', content: `模型连接探针失败：${error instanceof Error ? error.message : '跨域或网络错误'}` }])
    } finally {
      setModelConfigBusy(null)
    }
  }

  function replacePendingAttachments(next: PendingAttachment[]) {
    pendingAttachmentListRef.current = next
    setPendingAttachments(next)
  }

  function replacePendingTextInputs(next: PendingTextInput[]) {
    pendingTextInputListRef.current = next
    setPendingTextInputs(next)
  }

  function removePendingAttachment(attachmentId: string) {
    pendingAttachmentsRef.current.delete(attachmentId)
    replacePendingAttachments(pendingAttachmentListRef.current.filter((item) => item.id !== attachmentId))
  }

  function removePendingTextInput(inputId: string) {
    pendingTextInputsRef.current.delete(inputId)
    replacePendingTextInputs(pendingTextInputListRef.current.filter((item) => item.id !== inputId))
  }

  function createPendingTextInput(text: string): PendingTextInput {
    const input = { id: inputReference('input'), text }
    pendingTextInputsRef.current.set(input.id, input)
    const next = [...pendingTextInputListRef.current, input].slice(-pendingInputLimit)
    const retained = new Set(next.map((item) => item.id))
    for (const inputId of pendingTextInputsRef.current.keys()) {
      if (!retained.has(inputId)) pendingTextInputsRef.current.delete(inputId)
    }
    replacePendingTextInputs(next)
    return input
  }

  async function createPendingAttachment(file: File): Promise<PendingAttachment> {
    const bytes = new Uint8Array(await file.slice(0, attachmentPreviewByteLimit).arrayBuffer())
    const readable = readableTextPreview(bytes)
    const compacted = readable.available
      ? compactTextToTokenLimit(readable.text, 4_000, '[附件文本预览已截断]')
      : { text: '', truncated: false }
    const attachment: PendingAttachment = {
      id: inputReference('attachment'),
      file,
      name: file.name,
      size: file.size,
      mediaType: file.type,
      preview: compacted.text,
      previewAvailable: readable.available,
      previewTruncated: file.size > bytes.length || compacted.truncated,
    }
    pendingAttachmentsRef.current.set(attachment.id, attachment)
    const next = [...pendingAttachmentListRef.current, attachment].slice(-pendingInputLimit)
    const retained = new Set(next.map((item) => item.id))
    for (const attachmentId of pendingAttachmentsRef.current.keys()) {
      if (!retained.has(attachmentId)) pendingAttachmentsRef.current.delete(attachmentId)
    }
    replacePendingAttachments(next)
    return attachment
  }

  async function dispatchUserTurn(content: string, attachments: PendingAttachment[], displayContent: string, focusedTask?: FocusedAgentTaskContext) {
    artifactDeliveryNotifierRef.current?.preparePermission()
    const textInput = content ? createPendingTextInput(content) : null
    const visibleAttachments = attachments.length > 0 ? attachments : pendingAttachmentListRef.current
    const envelope = userTurnEnvelope(textInput, visibleAttachments)
    const entryRequestId = textInput?.id ?? inputReference('input')
    const activeBackendSession = backendAgentSessionRef.current
    if (activeBackendSession) {
      activeBackendSession.liveProgressRequestId = entryRequestId
      activeBackendSession.reasoningPreview = ''
      activeBackendSession.contentPreview = ''
      storeBackendAgentSession(activeBackendSession)
    }
    const currentPageContext = contextRef.current
    const reasoningLab = asRecord(currentPageContext.reasoning_lab)
    const currentAnalysisId = currentPageContext.active_view === 'code_knowledge'
      ? undefined
      : typeof reasoningLab.analysis_record_id === 'string' ? reasoningLab.analysis_record_id : undefined
    const resolvedFocusedTask = focusedTask ?? focusedTaskFromPageContext(currentPageContext)
    setStrategyProgress(createAgentRuntimeProgress({
      requestId: entryRequestId,
      headline: `正在向 ${selectedAgentDisplayName} 提交任务`,
      detail: '正在准备页面上下文并等待终端接收。',
    }))
    setStrategyProgressExpanded(false)
    setMessages((current) => [...current, {
      id: randomUuid(),
      role: 'user',
      content: displayContent,
    }])
    if (!agentCliConnected) {
      setMessages((current) => [...current, {
        id: randomUuid(),
        role: 'system',
        content: engineConnection === 'model_configuration_required'
          ? `输入已保留在当前页面。${selectedAgentDisplayName} 已由后端启动，请先配置模型，或让管理员提供后端默认模型。`
          : `输入已保留在当前页面，但后端 ${selectedAgentDisplayName} 当前不可用。请检查 adapter 与平台服务状态。`,
      }])
      return
    }

    if (webShellRef.current) {
      try {
        await webShellRef.current.sendCommand(envelope, {
          commandId: entryRequestId,
          analysisId: currentAnalysisId,
          context: pageAgentRuntimeContext(currentAnalysisId, resolvedFocusedTask),
        })
      } catch (error) {
        setWebShellState('error')
        setWebShellDetail(error instanceof Error ? error.message : `输入未能写入 ${selectedAgentDisplayName}。`)
      }
      return
    }

    const checkpoint = agentContinuation
    setAgentContinuation(null)
    storeAgentCheckpoint(null)
    if (checkpoint?.backendRunId) {
      const session: BackendAgentSession = backendAgentSessionRef.current?.runId === checkpoint.backendRunId
        ? backendAgentSessionRef.current
        : {
            runId: checkpoint.backendRunId,
            analysisId: checkpoint.analysisId ?? currentAnalysisId,
            origin: checkpoint.origin || displayContent,
            progress: checkpoint.progress,
            lastSequence: checkpoint.backendEventSequence ?? 0,
            reasoningPreview: '',
            contentPreview: '',
            handledUiRequests: new Set(),
            state: 'created',
          }
      attachBackendAgentSession(session)
      setBusy(true)
      setAgentStatusText('正在把新消息交给后端 Agent Run。')
      try {
        // The backend appends this input to its persisted checkpoint itself.
        // Do not reconstruct a faux observation in the browser: that used to
        // hide real device/build results behind a generic local checkpoint.
        const run = await submitBackendAgentMessage(
          checkpoint.backendRunId,
          envelope,
          modelConfigRef.current,
          resolvedFocusedTask ? {
            mode: 'page_agent',
            goal: displayContent || '继续当前代码知识任务。',
            context: pageAgentRuntimeContext(currentAnalysisId, resolvedFocusedTask),
          } : undefined,
        )
        session.state = run.state
        storeBackendAgentSession(session)
        setAgentRun(agentRunStateFromBackend(run))
        setAgentContinuation(null)
        storeAgentCheckpoint(null)
      } catch (error) {
        setBusy(false)
        const restored = backendContinuation(session, 'model_error', checkpoint.resumeTurn, {
          message: error instanceof Error ? error.message : '后端 Agent Run 未能接收新消息。',
          transient: true,
        })
        setAgentContinuation(restored)
        storeAgentCheckpoint(restored)
        setMessages((current) => [...current, {
          id: randomUuid(),
          role: 'system',
          content: `后端 Agent Run 未能接收新消息：${error instanceof Error ? error.message : '未知错误'}。检查点已保留。`,
        }])
      } finally {
        setAgentStatusText('')
      }
      return
    }
    const activeSession = backendAgentSessionRef.current
    const activeSessionMatchesAnalysis = activeSession
      && (!currentAnalysisId || normalizeAnalysisRouteId(activeSession.analysisId) === normalizeAnalysisRouteId(currentAnalysisId))
    if (activeSession && activeSessionMatchesAnalysis) {
      setBusy(true)
      const reopenConversation = backendRunIsTerminal(activeSession.state)
      setAgentStatusText(reopenConversation ? '正在继续处理新消息。' : '正在把新消息追加到后端 Agent Run 的当前检查点。')
      try {
        if (reopenConversation) attachBackendAgentSession(activeSession)
        const taskSwitch = reopenConversation || resolvedFocusedTask ? {
          mode: 'page_agent' as const,
          goal: displayContent || '根据用户目标继续操作鸿蒙异构优化工作台。',
          context: pageAgentRuntimeContext(currentAnalysisId, resolvedFocusedTask),
        } : undefined
        const run = await submitBackendAgentMessage(
          activeSession.runId,
          envelope,
          modelConfigRef.current,
          taskSwitch,
        )
        activeSession.state = run.state
        activeSession.progress = undefined
        activeSession.origin = displayContent
        storeBackendAgentSession(activeSession)
        setAgentRun(agentRunStateFromBackend(run))
        setAgentContinuation(null)
        storeAgentCheckpoint(null)
      } catch (error) {
        setBusy(false)
        setMessages((current) => [...current, {
          id: randomUuid(),
          role: 'system',
          content: `后端 Agent Run 未能接收新消息：${error instanceof Error ? error.message : '未知错误'}。`,
        }])
      } finally {
        setAgentStatusText('')
      }
      return
    }
    await runBackendAgentRun({
      initialUserMessage: envelope,
      initialMemory: conversationMemory,
      origin: displayContent,
      analysisId: currentAnalysisId,
      focusedTask: resolvedFocusedTask,
    })
  }

  async function submitAttachmentsToAgent(files: File[]) {
    if (attachmentBusy || busy || strategyRequestBusy) return
    setAttachmentBusy(true)
    setStrategyProgress(createAgentRuntimeProgress({
      requestId: `attachment:${Date.now()}`,
      headline: '正在读取附件',
      detail: '页面正在提取附件元数据和受限文本预览，随后判断附件用途。',
    }))
    let attachments: PendingAttachment[] = []
    try {
      attachments = await Promise.all(files.slice(0, 4).map(createPendingAttachment))
    } catch (error) {
      setMessages((current) => [...current, {
        id: randomUuid(),
        role: 'system',
        content: `附件读取失败：${error instanceof Error ? error.message : '未知错误'}。`,
      }])
    } finally {
      setAttachmentBusy(false)
    }
    if (attachments.length === 0) return
    const instruction = draft.trim()
    setDraft('')
    const names = attachments.map((attachment) => `“${attachment.name}”`).join('、')
    const display = instruction ? `${instruction}\n\n已附加：${names}` : `已附加：${names}`
    await dispatchUserTurn(instruction, attachments, display)
  }

  function handleTraceInput(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.currentTarget.files ?? [])
    event.currentTarget.value = ''
    if (files.length > 0) void submitAttachmentsToAgent(files)
  }

  function handleComposerDrop(event: DragEvent<HTMLElement>) {
    event.preventDefault()
    setDragActive(false)
    const files = Array.from(event.dataTransfer.files ?? [])
    if (files.length > 0) void submitAttachmentsToAgent(files)
  }

  async function executeRequestedAction(
    actionId: string,
    input: Record<string, unknown>,
    analysisId: string | undefined,
    turn: number,
  ): Promise<unknown> {
    if (!actionId.startsWith('assistant.')) return onExecuteAction(actionId, input)
    const requestId = randomUuid()
    await appendAnalysisTraceEvent(analysisId, {
      category: 'page_action',
      operation: actionId,
      phase: 'input_intent',
      status: 'started',
      requestId,
      turn,
      payload: { input },
    })
    try {
      let output: unknown
      if (actionId === 'assistant.use_pending_text_as_hotspot') {
        const inputId = typeof input.input_id === 'string' ? input.input_id : ''
        const pending = pendingTextInputsRef.current.get(inputId)
        if (!pending) throw new Error('用户输入引用不存在或已经过期，请让用户重新提交目标代码。')
        if (input.content_mode !== 'entire_input' && input.content_mode !== 'first_code_fence' && input.content_mode !== 'line_range') {
          throw new Error('需要明确选择 entire_input、first_code_fence 或 line_range，页面不会替模型猜测代码范围。')
        }
        const contentMode = input.content_mode
        const selectedRange = contentMode === 'line_range'
          ? lineRangeCode(pending.text, input.line_start, input.line_end)
          : null
        if (contentMode === 'line_range' && !selectedRange) {
          throw new Error('line_range 需要覆盖有效代码的一基 line_start 与 line_end。')
        }
        const code = contentMode === 'first_code_fence'
          ? firstFencedCode(pending.text)
          : selectedRange?.code ?? pending.text
        if (!code) throw new Error('所选用户输入中没有可用代码；请检查 Markdown 代码块或一基行号范围。')
        const result = await onExecuteAction('reasoning.paste_hotspot_code', {
          code,
          function_hint: typeof input.function_hint === 'string' ? input.function_hint : '',
          event_name: typeof input.event_name === 'string' ? input.event_name : '',
        })
        removePendingTextInput(inputId)
        output = {
          input_id: inputId,
          content_mode: contentMode,
          ...(selectedRange ? { line_start: selectedRange.lineStart, line_end: selectedRange.lineEnd } : {}),
          ...asRecord(result),
        }
      } else if (actionId === 'assistant.import_pending_trace') {
        const attachmentId = typeof input.attachment_id === 'string' ? input.attachment_id : ''
        const attachment = pendingAttachmentsRef.current.get(attachmentId)
        if (!attachment) throw new Error('待处理附件不存在或浏览器文件句柄已经失效，请让用户重新附加文件。')
        setAttachmentBusy(true)
        try {
          const response = await onImportTraceFile(attachment.file)
          removePendingAttachment(attachmentId)
          output = {
            artifact_id: response.artifact.artifact_id,
            input_kind: response.artifact.input_kind ?? 'unknown',
            file_name: attachment.name,
          }
        } finally {
          setAttachmentBusy(false)
        }
      } else {
        throw new Error(`未实现的浏览器输入 action：${actionId}`)
      }
      await appendAnalysisTraceEvent(analysisId, {
        category: 'tool_result',
        operation: actionId,
        phase: 'input_intent',
        status: 'succeeded',
        requestId,
        turn,
        payload: { input, output: output ?? null },
      })
      return output
    } catch (error) {
      await appendAnalysisTraceEvent(analysisId, {
        category: 'tool_result',
        operation: actionId,
        phase: 'input_intent',
        status: 'failed',
        requestId,
        turn,
        payload: { input, error: analysisErrorPayload(error) },
      })
      throw error
    }
  }

  function backendContinuation(
    session: BackendAgentSession,
    reason: AgentContinuation['reason'],
    resumeTurn: number,
    failure?: AgentContinuation['failure'],
  ): AgentContinuation {
    return {
      schemaVersion: 'page_agent_checkpoint/v1',
      nextUserMessage: '后端 Agent Run 已保存精确检查点；恢复时请从未完成的真实 action 继续。',
      memory: emptyConversationMemory,
      origin: session.origin,
      analysisId: session.analysisId,
      progress: session.progress,
      reason,
      resumeTurn: Math.max(1, resumeTurn),
      savedAt: new Date().toISOString(),
      failure,
      backendRunId: session.runId,
      backendEventSequence: session.lastSequence,
    }
  }

  function updateBackendProgress(session: BackendAgentSession, event: BackendAgentRunEvent) {
    const payload = event.payload
    const turn = typeof payload.turn === 'number' ? payload.turn : undefined
    const taskLabel = session.progress?.label ?? '页面助手'
    const updateLiveProgress = (update: (current: StrategyProgress) => StrategyProgress) => {
      const requestId = session.liveProgressRequestId || `${session.runId}:active`
      setStrategyProgress((current) => update(
        current?.requestId === requestId
          ? current
          : createAgentRuntimeProgress({ requestId, label: taskLabel }),
      ))
    }
    if (event.event_type === 'model.request') {
      const phase = typeof payload.phase === 'string' ? payload.phase : 'full_execution'
      const requestId = session.liveProgressRequestId || `${session.runId}:task:${event.sequence}`
      session.liveProgressRequestId = requestId
      session.reasoningPreview = ''
      session.contentPreview = ''
      const headline = phase === 'entry_triage' ? '正在理解请求' : turn ? `第 ${turn} 轮：模型正在决定下一步` : '模型正在决定下一步'
      const detail = phase === 'entry_triage'
        ? '正在根据当前消息与页面状态判断直接回复或进入分析流程。'
        : '模型正在结合前序真实操作结果生成下一步判断。'
      setStrategyProgress((current) => current?.requestId === requestId
        ? {
            ...current,
            phase: 'model_pending',
            status: 'running',
            headline,
            detail,
            streamPreview: undefined,
            steps: current.steps.map((step) => step.id === 'model'
              ? { ...step, status: 'running', detail: '等待模型的实时生成内容。' }
              : step),
          }
        : createAgentRuntimeProgress({
            requestId,
            label: taskLabel,
            headline,
            detail,
            startedAt: event.occurred_at,
          }))
      return
    }
    if (event.event_type === 'model.stream') {
      const reasoning = typeof payload.reasoning_delta === 'string' ? payload.reasoning_delta : ''
      const content = typeof payload.content_delta === 'string' ? payload.content_delta : ''
      session.reasoningPreview = `${session.reasoningPreview}${reasoning}`.slice(-4_000)
      session.contentPreview = `${session.contentPreview}${content}`.slice(-4_000)
      updateLiveProgress((current) => ({
        ...current,
        status: 'running',
        headline: turn ? `第 ${turn} 轮：模型正在推理` : '模型正在推理',
        detail: '以下内容由模型实时生成。',
        streamPreview: [
          session.reasoningPreview ? `[思考]\n${session.reasoningPreview}` : '',
          session.contentPreview ? `[输出]\n${session.contentPreview}` : '',
        ].filter(Boolean).join('\n\n'),
        streamedReasoningChars: typeof payload.reasoning_chars === 'number' ? payload.reasoning_chars : undefined,
        streamedContentChars: typeof payload.content_chars === 'number' ? payload.content_chars : undefined,
        steps: current.steps.map((step) => step.id === 'model'
          ? { ...step, status: 'running', detail: '正在接收模型实时生成内容。' }
          : step),
      }))
      return
    }
    if (event.event_type === 'agent.entry_decision') {
      const scope = typeof payload.execution_scope === 'string' ? payload.execution_scope : ''
      updateLiveProgress((current) => ({
        ...current,
        headline: payload.decision === 'reply'
          ? '模型选择直接回复'
          : scope === 'full_optimization_analysis'
            ? '已进入优化分析流程'
            : '正在执行当前任务',
        detail: typeof payload.reason === 'string' ? payload.reason : '模型已完成本轮入口判断。',
      }))
      return
    }
    if (event.event_type === 'run.incomplete_page_analysis') {
      const workflowState = asRecord(payload.workflow)
      const requiredAction = typeof workflowState.required_action === 'string' ? workflowState.required_action : '下一项页面分析操作'
      updateLiveProgress((current) => ({
        ...current,
        status: 'running',
        headline: '正在补齐左侧分析工件',
        detail: `当前需要 ${requiredAction}；真实结果返回后将继续处理。`,
      }))
      return
    }
    if (event.event_type === 'tool.call' || event.event_type === 'ui.action_requested') {
      const actionId = typeof payload.action_id === 'string' ? payload.action_id : '已注册操作'
      updateLiveProgress((current) => ({
        ...current,
        status: 'running',
        headline: turn ? `第 ${turn} 轮：正在执行 ${actionId}` : `正在执行 ${actionId}`,
        detail: '该操作的真实返回将作为后续判断依据。',
        steps: current.steps.map((step) => step.id === 'model'
          ? { ...step, status: 'succeeded' }
          : step.id === 'actions'
            ? { ...step, status: 'running', detail: `正在执行 ${actionId}。` }
            : step),
      }))
      return
    }
    if (event.event_type === 'device.job_progress') {
      const actionId = typeof payload.action_id === 'string' ? payload.action_id : '设备操作'
      const deviceProgress = asRecord(payload.progress)
      const detail = typeof deviceProgress.detail_zh === 'string' && deviceProgress.detail_zh
        ? deviceProgress.detail_zh
        : typeof deviceProgress.stage === 'string' && deviceProgress.stage
          ? `设备执行阶段：${deviceProgress.stage}`
          : '设备端已返回执行进度，正在等待结果。'
      updateLiveProgress((current) => ({
        ...current,
        status: 'running',
        headline: `真机验证：${actionId}`,
        detail,
        steps: current.steps.map((step) => step.id === 'actions'
          ? { ...step, status: 'running', detail }
          : step),
      }))
      return
    }
    if (event.event_type === 'tool.result' || event.event_type === 'tool.result_reused') {
      const failed = payload.status === 'failed'
      updateLiveProgress((current) => ({
        ...current,
        status: failed ? 'warning' : 'running',
        headline: failed ? '操作返回了可诊断错误' : '操作结果已返回，正在继续判断',
        detail: shortResult(payload.result ?? payload.error),
        steps: current.steps.map((step) => step.id === 'actions'
          ? { ...step, status: failed ? 'warning' : 'succeeded', detail: failed ? '模型会基于结构化错误决定修订或停止。' : '真实操作结果已保存。' }
          : step),
      }))
      return
    }
    if (event.event_type === 'model.response' || event.event_type === 'model.decision') {
      updateLiveProgress((current) => ({
        ...current,
        phase: 'response_received',
        headline: '本轮模型响应已返回',
        detail: '正在处理模型结论或衔接下一项已注册操作。',
        steps: current.steps.map((step) => step.id === 'model' ? { ...step, status: 'succeeded' } : step),
      }))
      return
    }
    if (event.event_type === 'run.state_changed') {
      const state = event.state as BackendAgentRunState
      if (state === 'completed' || state === 'failed' || state === 'cancelled' || state === 'waiting_for_model_credential' || state.startsWith('paused_') || state === 'waiting_for_ui') {
        const waitingForUser = state === 'waiting_for_ui' && payload.waiting_reason === 'model_requested_user_input'
        updateLiveProgress((current) => ({
          ...current,
          phase: state === 'completed' ? 'completed' : state === 'failed' ? 'failed' : current.phase,
          status: state === 'completed' ? 'succeeded' : state === 'failed' ? 'failed' : state === 'cancelled' || state === 'waiting_for_model_credential' || state.startsWith('paused_') || waitingForUser ? 'warning' : 'running',
          headline: state === 'completed'
            ? '本次任务已完成'
            : state === 'failed'
              ? '本次任务未完成'
              : waitingForUser
                ? '需要用户补充或判断'
                : state === 'waiting_for_model_credential'
                  ? '需要恢复模型连接'
                  : state === 'cancelled'
                    ? '本次任务已停止'
                    : current.headline,
          detail: typeof payload.detail === 'string' ? payload.detail : '当前上下文与真实观察已经保留。',
          steps: current.steps.map((step) => step.id === 'verdict' && (state === 'completed' || state === 'failed' || waitingForUser)
            ? { ...step, status: state === 'completed' ? 'succeeded' : state === 'failed' ? 'failed' : 'warning', detail: typeof payload.detail === 'string' ? payload.detail : step.detail }
            : step),
        }))
      }
    }
  }

  function backendSessionWasCancelled(session: BackendAgentSession) {
    const current = backendAgentSessionRef.current
    return session.state === 'cancelled' || current?.runId !== session.runId || current.state === 'cancelled'
  }

  async function executeBackendUiAction(session: BackendAgentSession, payload: Record<string, unknown>) {
    const requestId = typeof payload.request_id === 'string' ? payload.request_id : ''
    const actionId = typeof payload.action_id === 'string' ? payload.action_id : ''
    const input = asRecord(payload.input)
    const turn = typeof payload.turn === 'number' ? payload.turn : 0
    if (!requestId || !actionId || backendSessionWasCancelled(session) || session.handledUiRequests.has(requestId)) return
    session.handledUiRequests.add(requestId)
    setAgentStatusText(`正在执行页面操作：${actionId}`)
    const replay = storedBackendUiResult(session.runId, requestId)
    if (replay) {
      try {
        const run = await submitBackendUiActionResult({
          runId: session.runId,
          requestId,
          ...(replay.result ? { result: replay.result } : {}),
          ...(replay.error ? { error: replay.error } : {}),
          config: modelConfigRef.current,
        })
        session.state = run.state
        storeBackendAgentSession(session)
        setAgentRun(agentRunStateFromBackend(run))
        clearStoredBackendUiResult(session.runId, requestId)
        return
      } catch (error) {
        // Keep the local result for a later SSE reconnect.  The server may
        // have restarted before it could accept this exact request ID.
        setMessages((current) => [...current, {
          id: randomUuid(),
          role: 'system',
          content: `已完成的页面操作尚未回传后端：${error instanceof Error ? error.message : '未知错误'}。`,
        }])
        session.handledUiRequests.delete(requestId)
        return
      } finally {
        setAgentStatusText('')
      }
    }
    try {
      const output = await executeRequestedAction(actionId, input, session.analysisId, turn)
      const outputRecord = asRecord(output)
      const producedAnalysisId = normalizeAnalysisRouteId(outputRecord.analysis_id)
      if (producedAnalysisId && normalizeAnalysisRouteId(session.analysisId) !== producedAnalysisId) {
        if (session.analysisId) throw new Error('当前 Agent Run 已绑定到另一条优化分析记录。')
        const boundRun = await bindBackendAgentRunToAnalysis(session.runId, producedAnalysisId)
        session.analysisId = producedAnalysisId
        session.state = boundRun.state
        storeBackendAgentSession(session)
      }
      const result = Object.keys(outputRecord).length > 0
        ? outputRecord
        : output === undefined || output === null
          ? { status: 'succeeded' }
          : { value: output }
      if (backendSessionWasCancelled(session)) return
      persistBackendUiResult(session.runId, requestId, { result })
      const run = await submitBackendUiActionResult({
        runId: session.runId,
        requestId,
        result,
        config: modelConfigRef.current,
      })
      session.state = run.state
      storeBackendAgentSession(session)
      setAgentRun(agentRunStateFromBackend(run))
      clearStoredBackendUiResult(session.runId, requestId)
    } catch (error) {
      if (backendSessionWasCancelled(session)) return
      const payloadError = analysisErrorPayload(error)
      persistBackendUiResult(session.runId, requestId, { error: payloadError })
      try {
        const run = await submitBackendUiActionResult({
          runId: session.runId,
          requestId,
          error: payloadError,
          config: modelConfigRef.current,
        })
        session.state = run.state
        storeBackendAgentSession(session)
        setAgentRun(agentRunStateFromBackend(run))
        clearStoredBackendUiResult(session.runId, requestId)
      } catch (submitError) {
        session.handledUiRequests.delete(requestId)
        setMessages((current) => [...current, {
          id: randomUuid(),
          role: 'system',
          content: `页面操作结果未能回传后端：${submitError instanceof Error ? submitError.message : '未知错误'}。`,
        }])
      }
    } finally {
      setAgentStatusText('')
    }
  }
  executeBackendUiActionRef.current = executeBackendUiAction

  function recordEngineTerminalEvent(session: BackendAgentSession, event: BackendAgentRunEvent): boolean {
    if (event.event_type !== 'engine.event') return false
    const engineEventType = typeof event.payload.engine_event_type === 'string' ? event.payload.engine_event_type : ''
    const enginePayload = asRecord(event.payload.payload)
    if (engineEventType === 'tool.call.started') {
      const toolCallId = typeof enginePayload.toolCallId === 'string' ? enginePayload.toolCallId : `${event.sequence}`
      const rawName = typeof enginePayload.name === 'string' ? enginePayload.name : '工具'
      const description = typeof enginePayload.description === 'string'
        ? enginePayload.description
        : typeof enginePayload.display === 'string'
          ? enginePayload.display
          : '正在执行'
      const messageId = `engine-tool-${session.runId}-${toolCallId}`
      setMessages((current) => current.some((message) => message.id === messageId)
        ? current
        : [...current, {
            id: messageId,
            role: 'system',
            terminalKind: 'tool',
            toolLabel: terminalToolLabel(rawName),
            toolStatus: 'running',
            content: description,
          }])
      return true
    }
    if (engineEventType === 'tool.result') {
      const toolCallId = typeof enginePayload.toolCallId === 'string' ? enginePayload.toolCallId : `${event.sequence}`
      const messageId = `engine-tool-${session.runId}-${toolCallId}`
      const failed = enginePayload.isError === true
      setMessages((current) => current.map((message) => message.id === messageId
        ? {
            ...message,
            toolStatus: failed ? 'failed' : 'succeeded',
            content: failed ? '执行失败，诊断已返回 Agent' : '执行完成',
          }
        : message))
      return true
    }
    if (engineEventType === 'compaction.started' || engineEventType === 'compaction.completed' || engineEventType === 'compaction.blocked') {
      const content = engineEventType === 'compaction.started'
        ? '上下文接近上限，Agent 正在压缩会话记忆'
        : engineEventType === 'compaction.completed'
          ? '上下文压缩完成，任务继续运行'
          : '上下文压缩未完成，诊断已保留'
      setMessages((current) => [...current, {
        id: `engine-status-${session.runId}-${event.sequence}`,
        role: 'system',
        terminalKind: 'status',
        content,
      }])
      return true
    }
    return true
  }

  function handleBackendAgentEvent(session: BackendAgentSession, event: BackendAgentRunEvent) {
    if (session.state === 'cancelled') return
    if (event.sequence <= session.lastSequence) return
    session.lastSequence = Math.max(session.lastSequence, event.sequence)
    if (event.event_type === 'run.state_changed') {
      const nextState = backendRunState(event.state)
      if (nextState) session.state = nextState
    }
    const payload = event.payload
    if (session.progress && (event.event_type === 'tool.result' || event.event_type === 'tool.result_reused')) {
      const actionId = typeof payload.action_id === 'string' ? payload.action_id : ''
      if (actionId) {
        session.progress.facts = updateDeviceValidationFacts(session.progress.facts, [{
          actionId,
          input: asRecord(payload.input),
          output: asRecord(payload.result ?? payload.error),
          result: shortResult(payload.result ?? payload.error),
          failed: payload.status === 'failed',
        }])
        if (session.analysisId) {
          const existing = readDeviceValidationSnapshot(session.analysisId)
          storeDeviceValidationSnapshot({
            schemaVersion: 'device_validation_snapshot/v1',
            analysisId: session.analysisId,
            requestId: session.progress.requestId,
            runId: session.runId,
            status: session.state,
            facts: session.progress.facts,
            ...(session.progress.requestedUnit ? { requestedUnit: session.progress.requestedUnit } : {}),
            ...(session.progress.recommendedProfileId ? { recommendedProfileId: session.progress.recommendedProfileId } : {}),
            ...(session.progress.compatibleProfileIds ? { compatibleProfileIds: session.progress.compatibleProfileIds } : {}),
            ...(existing?.finalResponse ? { finalResponse: existing.finalResponse } : {}),
            updatedAt: new Date().toISOString(),
          })
        }
      }
    }
    if (session.progress && session.analysisId && event.event_type === 'tool.call') {
      const actionId = typeof payload.action_id === 'string' ? payload.action_id : ''
      if (actionId) {
        const existing = readDeviceValidationSnapshot(session.analysisId)
        storeDeviceValidationSnapshot({
          schemaVersion: 'device_validation_snapshot/v1',
          analysisId: session.analysisId,
          requestId: session.progress.requestId,
          runId: session.runId,
          status: session.state,
          facts: session.progress.facts,
          ...(session.progress.requestedUnit ? { requestedUnit: session.progress.requestedUnit } : {}),
          ...(session.progress.recommendedProfileId ? { recommendedProfileId: session.progress.recommendedProfileId } : {}),
          ...(session.progress.compatibleProfileIds ? { compatibleProfileIds: session.progress.compatibleProfileIds } : {}),
          activeActionId: actionId,
          activeActionDetail: `${actionId} 已提交，正在等待真实返回。`,
          ...(existing?.finalResponse ? { finalResponse: existing.finalResponse } : {}),
          updatedAt: new Date().toISOString(),
        })
      }
    }
    storeBackendAgentSession(session)
    updateBackendProgress(session, event)
    if (event.event_type === 'skill.bootstrap_required' || event.event_type === 'skill.bootstrap_acknowledged') {
      const skillBootstrap = asRecord(event.payload.skill_bootstrap) as BackendSkillBootstrap
      setAgentRun((current) => ({
        turn: current?.turn ?? 0,
        maxTurns: current?.maxTurns ?? modelConfigRef.current.agentMaxTurns,
        maxActionsPerTurn: current?.maxActionsPerTurn ?? modelConfigRef.current.agentMaxActionsPerTurn,
        state: current?.state ?? session.state,
        detail: current?.detail,
        skillBootstrap,
      }))
    }
    if (recordEngineTerminalEvent(session, event)) return
    if (event.event_type === 'agent.entry_decision') {
      return
    }
    if (event.event_type === 'model.stream') {
      const retry = asRecord(payload.retry)
      if (Object.keys(retry).length > 0) {
        setStrategyProgress((current) => current ? {
          ...current,
          status: 'running',
          headline: '上游模型服务繁忙，正在自动恢复',
          detail: '后端会从当前检查点重试，已经取得的工具事实不会丢失。',
        } : current)
      }
      return
    }
    if (event.event_type === 'device.job_progress') {
      if (session.progress && session.analysisId) {
        const actionId = typeof payload.action_id === 'string' ? payload.action_id : 'device.operation'
        const progress = asRecord(payload.progress)
        const detail = typeof progress.detail_zh === 'string' && progress.detail_zh
          ? progress.detail_zh
          : typeof progress.stage === 'string' && progress.stage
            ? `设备执行阶段：${progress.stage}`
            : '设备端已返回进度，正在等待结果。'
        const existing = readDeviceValidationSnapshot(session.analysisId)
        storeDeviceValidationSnapshot({
          schemaVersion: 'device_validation_snapshot/v1',
          analysisId: session.analysisId,
          requestId: session.progress.requestId,
          runId: session.runId,
          status: session.state,
          facts: session.progress.facts,
          ...(session.progress.requestedUnit ? { requestedUnit: session.progress.requestedUnit } : {}),
          ...(session.progress.recommendedProfileId ? { recommendedProfileId: session.progress.recommendedProfileId } : {}),
          ...(session.progress.compatibleProfileIds ? { compatibleProfileIds: session.progress.compatibleProfileIds } : {}),
          activeActionId: actionId,
          activeActionDetail: detail,
          ...(existing?.finalResponse ? { finalResponse: existing.finalResponse } : {}),
          updatedAt: new Date().toISOString(),
        })
      }
      return
    }
    if (event.event_type === 'model.response') {
      const usage = backendModelTokenUsage(payload.usage)
      if (usage) {
        const budget = createPromptTokenBudget({
          contextWindowTokens: modelConfigRef.current.contextWindowTokens,
          inputTokens: usage.inputTokens,
          outputReserveTokens: modelConfigRef.current.agentMaxOutputTokens,
        })
        recordModelUsage(usage, budget)
      }
      const content = typeof payload.content === 'string' ? payload.content.trim() : ''
      if (session.progress && session.analysisId && content) {
        storeDeviceValidationSnapshot({
          schemaVersion: 'device_validation_snapshot/v1',
          analysisId: session.analysisId,
          requestId: session.progress.requestId,
          runId: session.runId,
          status: session.state,
          facts: session.progress.facts,
          ...(session.progress.requestedUnit ? { requestedUnit: session.progress.requestedUnit } : {}),
          ...(session.progress.recommendedProfileId ? { recommendedProfileId: session.progress.recommendedProfileId } : {}),
          ...(session.progress.compatibleProfileIds ? { compatibleProfileIds: session.progress.compatibleProfileIds } : {}),
          finalResponse: content,
          updatedAt: new Date().toISOString(),
        })
      }
      if (payload.engine_id === 'kimi-server' && content) {
        const messageId = `backend-engine-response-${session.runId}-${event.sequence}`
        setMessages((current) => current.some((message) => message.id === messageId)
          ? current
          : [...current, { id: messageId, role: 'assistant', content }])
      }
      return
    }
    if (event.event_type === 'model.decision') {
      const reply = typeof payload.reply === 'string' ? payload.reply : ''
      if (reply) {
        const messageId = `backend-decision-${session.runId}-${event.sequence}`
        setMessages((current) => current.some((message) => message.id === messageId)
          ? current
          : [...current, { id: messageId, role: 'assistant', content: reply }])
      }
      return
    }
    if (event.event_type === 'tool.call') {
      const actionId = typeof payload.action_id === 'string' ? payload.action_id : '已注册工具'
      const requestId = typeof payload.request_id === 'string' ? payload.request_id : `${event.sequence}`
      const messageId = `platform-tool-${session.runId}-${requestId}`
      setMessages((current) => current.some((message) => message.id === messageId)
        ? current
        : [...current, {
            id: messageId,
            role: 'system',
            terminalKind: 'tool',
            toolLabel: actionId,
            toolStatus: 'running',
            content: '正在执行并等待真实返回',
          }])
      return
    }
    if (event.event_type === 'tool.result' || event.event_type === 'tool.result_reused') {
      const actionId = typeof payload.action_id === 'string' ? payload.action_id : '已注册操作'
      const requestId = typeof payload.request_id === 'string' ? payload.request_id : `${event.sequence}`
      const messageId = `platform-tool-${session.runId}-${requestId}`
      const result = shortResult(payload.result ?? payload.error)
      const failed = payload.status === 'failed'
      if (!failed && event.event_type === 'tool.result') {
        const delivery = artifactDeliveryFromToolResult(actionId, payload.result)
        if (delivery) artifactDeliveryNotifierRef.current?.announce(delivery)
      }
      if (actionId === 'knowledge.build_custom_api_kb' && !failed) {
        window.dispatchEvent(new CustomEvent('custom-api-kb-updated', { detail: { result: payload.result } }))
      }
      if ((actionId === 'skills.save_user_version' || actionId === 'skills.activate_version') && !failed) {
        window.dispatchEvent(new CustomEvent('skill-library-updated', { detail: { action_id: actionId, result: payload.result } }))
      }
      if ((actionId === 'experience.save_review' || actionId === 'experience.triage_case') && !failed) {
        window.dispatchEvent(new CustomEvent('experience-library-updated', { detail: { action_id: actionId, result: payload.result } }))
      }
      if ((actionId === 'application_flow.publish_candidates' || actionId === 'application_flow.save_discovery' || actionId === 'application_flow.save_scenario' || actionId === 'application_flow.compile_mermaid' || actionId === 'application_flow.enrich_trace_anchors' || actionId === 'application_flow.apply_runtime_trace_alignment') && !failed) {
        window.dispatchEvent(new CustomEvent('application-flow-updated', { detail: { action_id: actionId, result: payload.result } }))
      }
      if (actionId === 'code_graph.publish_report' && !failed) {
        window.dispatchEvent(new CustomEvent('code-knowledge-updated', { detail: { action_id: actionId, result: payload.result } }))
      }
      if (actionId === 'experimental_flow.publish_result' && !failed) {
        window.dispatchEvent(new CustomEvent('experimental-flow-updated', { detail: { action_id: actionId, result: payload.result } }))
      }
      if (!failed && /^(scenario_capture|parallel_modeling|parallel_job|workload_features)\./.test(actionId)) {
        window.dispatchEvent(new CustomEvent('parallel-workspace-updated', { detail: { action_id: actionId, result: payload.result } }))
      }
      setMessages((current) => {
        const content = failed
          ? `本轮尝试未通过，诊断已保存。${result ? ` ${result}` : ''}`
          : result || '已返回真实结果。'
        if (current.some((message) => message.id === messageId)) {
          return current.map((message) => message.id === messageId
            ? { ...message, toolStatus: failed ? 'failed' : 'succeeded', content }
            : message)
        }
        return [...current, {
          id: messageId,
          role: 'system',
          terminalKind: 'tool',
          toolLabel: actionId,
          toolStatus: failed ? 'failed' : 'succeeded',
          content,
        }]
      })
      return
    }
    if (event.event_type === 'ui.action_requested') {
      void executeBackendUiAction(session, payload)
      return
    }
    if (event.event_type !== 'run.state_changed') return
    const state = event.state as BackendAgentRunState
    const detail = typeof payload.detail === 'string' ? payload.detail : ''
    const turn = typeof payload.turn === 'number' ? payload.turn : 0
    setAgentRun((current) => ({
      turn: turn || current?.turn || 0,
      maxTurns: current?.maxTurns ?? modelConfigRef.current.agentMaxTurns,
      maxActionsPerTurn: current?.maxActionsPerTurn ?? modelConfigRef.current.agentMaxActionsPerTurn,
      state,
      detail,
      skillBootstrap: current?.skillBootstrap,
    }))
    if (session.transport === 'agent-cli-pty') {
      session.state = state
      storeBackendAgentSession(session)
      if (state === 'waiting_for_ui' || state === 'waiting_for_tool' || state === 'waiting_for_device' || state === 'compacting') {
        setBusy(true)
      } else if (state === 'running' || state === 'created') {
        setBusy(false)
      }
      return
    }
    if (session.progress && session.analysisId) {
      const existing = readDeviceValidationSnapshot(session.analysisId)
      storeDeviceValidationSnapshot({
        schemaVersion: 'device_validation_snapshot/v1',
        analysisId: session.analysisId,
        requestId: session.progress.requestId,
        runId: session.runId,
        status: state,
        facts: session.progress.facts,
        ...(session.progress.requestedUnit ? { requestedUnit: session.progress.requestedUnit } : {}),
        ...(session.progress.recommendedProfileId ? { recommendedProfileId: session.progress.recommendedProfileId } : {}),
        ...(session.progress.compatibleProfileIds ? { compatibleProfileIds: session.progress.compatibleProfileIds } : {}),
        ...(existing?.finalResponse ? { finalResponse: existing.finalResponse } : {}),
        updatedAt: new Date().toISOString(),
      })
    }
    if (state === 'waiting_for_model_credential' && modelConfigRef.current.apiKey.trim() && modelConfigRef.current.baseUrl.trim() && modelConfigRef.current.model.trim()) {
      setBusy(true)
      setAgentContinuation(null)
      storeAgentCheckpoint(null)
      void recoverBackendCredential(session)
      return
    }
    if (state === 'running' || state === 'waiting_for_tool' || state === 'waiting_for_device' || state === 'compacting') {
      setBusy(true)
      return
    }
    if (state === 'waiting_for_ui') {
      const waitingForUser = payload.waiting_reason === 'model_requested_user_input'
      setBusy(!waitingForUser)
      return
    }
    if (state === 'completed' || state === 'cancelled') {
      session.state = state
      backendAgentSubscriptionRef.current?.()
      backendAgentSubscriptionRef.current = null
      backendAgentSessionRef.current = session
      storeBackendAgentSession(session)
      setBusy(false)
      setAgentRun(null)
      setAgentContinuation(null)
      storeAgentCheckpoint(null)
      if (state === 'cancelled') {
        setMessages((current) => [...current, {
          id: randomUuid(),
          role: 'system',
          content: detail || '后端 Agent Run 已取消；已经发生的动作和结果仍保留在执行记录中。',
        }])
      }
      return
    }
    if (state === 'failed' || state === 'waiting_for_model_credential' || state.startsWith('paused_')) {
      session.state = state
      storeBackendAgentSession(session)
      const reason: AgentContinuation['reason'] = state === 'paused_turn_budget'
        ? 'turn_budget'
        : state === 'paused_bridge_offline'
          ? 'bridge_offline'
          : state === 'paused_output_limit'
            ? 'output_limit'
            : state === 'waiting_for_model_credential'
              ? 'credential'
              : state === 'failed'
                ? 'model_error'
                : 'interrupted'
      const continuation = backendContinuation(session, reason, Math.max(1, turn || 1), state === 'failed' ? { message: detail || '后端 Agent Run 失败', transient: false } : undefined)
      setAgentContinuation(continuation)
      storeAgentCheckpoint(continuation)
      setBusy(false)
      setAgentRun(null)
      setMessages((current) => [...current, {
        id: randomUuid(),
        role: state === 'failed' ? 'system' : 'assistant',
        content: state === 'waiting_for_model_credential'
          ? '后端任务检查点已恢复，但当前浏览器没有可用的模型配置。请在页面助手设置中恢复模型连接后继续。'
          : detail || '后端 Agent Run 已暂停，真实操作结果和恢复检查点均已保留。',
      }])
    }
  }

  async function recoverBackendCredential(session: BackendAgentSession): Promise<boolean> {
    const activeConfig = modelConfigRef.current
    if (!activeConfig.apiKey.trim() || !activeConfig.baseUrl.trim() || !activeConfig.model.trim()) return false
    if (credentialRecoveryRunIdsRef.current.has(session.runId)) return true
    credentialRecoveryRunIdsRef.current.add(session.runId)
    setBusy(true)
    setAgentStatusText('后端任务已恢复，正在重新连接模型并从检查点继续。')
    try {
      const run = await resumeBackendAgentRun(session.runId, activeConfig)
      session.state = run.state
      storeBackendAgentSession(session)
      setAgentRun(agentRunStateFromBackend(run))
      if (run.state === 'waiting_for_model_credential') return false
      setAgentContinuation(null)
      storeAgentCheckpoint(null)
      setBusy(!backendRunIsTerminal(run.state) && run.state !== 'waiting_for_ui')
      setAgentStatusText(backendRunIsTerminal(run.state) ? '' : '模型已重新连接，正在从保存的检查点继续。')
      return true
    } catch (error) {
      setBusy(false)
      setAgentStatusText('模型自动重连未完成，检查点仍已保留。')
      setMessages((current) => [...current, {
        id: randomUuid(),
        role: 'system',
        content: `模型自动重连未完成：${error instanceof Error ? error.message : '未知错误'}。检查点和真实工具结果没有丢失。`,
      }])
      return false
    } finally {
      credentialRecoveryRunIdsRef.current.delete(session.runId)
    }
  }

  function attachBackendAgentSession(session: BackendAgentSession) {
    backendAgentSubscriptionRef.current?.()
    backendAgentSessionRef.current = session
    setLastBackendRunId(session.runId)
    storeBackendAgentSession(session)
    backendAgentSubscriptionRef.current = subscribeBackendAgentRun(
      session.runId,
      session.lastSequence,
      (event) => handleBackendAgentEvent(session, event),
      (state) => setAgentStatusText(state === 'connected' ? '' : '后端 Agent 事件流正在重连，任务仍在后端继续。'),
    )
  }
  attachBackendAgentSessionRef.current = attachBackendAgentSession

  const pageAgentRuntimeContext = useCallback((analysisId?: string | null, focusedTask?: FocusedAgentTaskContext): Record<string, unknown> => {
    const scopedAnalysisId = normalizeAnalysisRouteId(analysisId)
    const runtimePageContext = focusedTask?.kind === 'scenario_capture' && focusedTask.requestContext
      ? { ...contextRef.current, active_view: 'scenario_capture', scenario_capture: focusedTask.requestContext }
      : contextRef.current
    const pageContext = boundedPageContext(runtimePageContext, 1024 * 1024)
    const retainedDeviceValidation = readDeviceValidationSnapshot(scopedAnalysisId)
    return {
      schema_version: 'page_agent_runtime_context/v1',
      agent_entry_policy: 'agent_direct',
      analysis_record_id: scopedAnalysisId,
      page_context: pageContext.text,
      page_context_compacted: pageContext.compacted,
      device_validation_facts: retainedDeviceValidation ? {
        run_id: retainedDeviceValidation.runId,
        request_id: retainedDeviceValidation.requestId,
        status: retainedDeviceValidation.status,
        facts: retainedDeviceValidation.facts,
        final_response: retainedDeviceValidation.finalResponse ?? null,
        updated_at: retainedDeviceValidation.updatedAt,
      } : null,
      device_execution_mode: selectedDeviceBridge ? 'remote_bridge' : 'local_hdc',
      skill_profile: compactSkillProfile(skillProfile),
      ...(focusedTask ? {
        focused_task: {
          kind: focusedTask.kind,
          ...(focusedTask.requestContext ? { request_context: focusedTask.requestContext } : {}),
          source_connector_id: focusedTask.sourceConnectorId ?? '',
          source_snapshot_id: focusedTask.sourceSnapshotId ?? '',
          subpath: focusedTask.subpath ?? '',
          graph_id: focusedTask.graphId ?? '',
          current_report_id: focusedTask.currentReportId ?? '',
          discovery_artifact_id: focusedTask.discoveryArtifactId ?? '',
          flow_id: focusedTask.flowId ?? '',
          scenario_artifact_id: focusedTask.scenarioArtifactId ?? '',
          trace_analysis_artifact_id: focusedTask.traceAnalysisArtifactId ?? '',
          evidence_artifact_id: focusedTask.evidenceArtifactId ?? '',
          result_artifact_id: focusedTask.resultArtifactId ?? '',
          analysis_kind: focusedTask.analysisKind ?? '',
          application_flow_analysis_id: focusedTask.applicationFlowAnalysisId ?? '',
          source_scopes: (focusedTask.sourceScopes ?? []).map((item) => ({
            source_connector_id: item.sourceConnectorId,
            source_snapshot_id: item.sourceSnapshotId ?? '',
            subpath: item.subpath ?? '',
            graph_id: item.graphId ?? '',
          })),
        },
      } : {}),
      ...(selectedDeviceBridge ? {
        device_selector: { bridge_id: selectedDeviceBridge.bridge_id },
        selected_device_bridge: {
          bridge_id: selectedDeviceBridge.bridge_id,
          display_name: selectedDeviceBridge.display_name,
          connection_state: selectedDeviceBridge.connection_state,
        },
      } : {}),
      ...scenarioCaptureRuntimeBinding(contextRef.current, focusedTask),
    }
  }, [selectedDeviceBridge, skillProfile])

  const handleWebShellSessionReady = useCallback((terminal: AgentCliTerminal, run: BackendAgentRun) => {
    const previous = backendAgentSessionRef.current?.runId === run.run_id
      ? backendAgentSessionRef.current
      : null
    const scopedAnalysisId = normalizeAnalysisRouteId(readAnalysisRouteId())
    const session: BackendAgentSession = {
      runId: run.run_id,
      transport: 'agent-cli-pty',
      ...(scopedAnalysisId ? { analysisId: scopedAnalysisId } : {}),
      origin: `真实 ${terminal.agent_display_name} WebShell`,
      progress: webShellProgressRef.current,
      lastSequence: Math.max(previous?.lastSequence ?? 0, run.event_cursor ?? 0),
      reasoningPreview: previous?.reasoningPreview ?? '',
      contentPreview: previous?.contentPreview ?? '',
      liveProgressRequestId: previous?.liveProgressRequestId,
      handledUiRequests: previous?.handledUiRequests ?? new Set(),
      state: run.state,
    }
    setWebShellTerminal(terminal)
    setLastBackendRunId(run.run_id)
    setAgentRun(agentRunStateFromBackend(run))
    attachBackendAgentSessionRef.current(session)
    for (const pending of run.pending_ui_actions) {
      if (!session.handledUiRequests.has(pending.request_id)) void executeBackendUiActionRef.current(session, pending)
    }
  }, [])

  const handleWebShellStateChange = useCallback((state: AgentWebShellState, detail: string) => {
    setWebShellState(state)
    setWebShellDetail(detail)
    if (state === 'working') {
      setBusy(true)
      setAgentStatusText(detail)
      return
    }
    if (state === 'ready') {
      setBusy(false)
      if (!activeStrategyRequestRef.current) {
        setStrategyProgress((current) => current?.status === 'running'
          ? {
              ...current,
              phase: 'completed',
              status: 'succeeded',
              headline: '本轮 Agent 已返回',
              detail: `${selectedAgentDisplayName} 已回到输入状态；业务结论、页面工件与工具证据均已保留。`,
              steps: current.steps.map((step) => step.status === 'running'
                ? { ...step, status: 'succeeded', detail: '本轮 CLI 已完成并回到输入状态。' }
                : step),
            }
          : current)
      }
      return
    }
    if (state === 'connecting') {
      setAgentStatusText(detail)
      return
    }
    setBusy(false)
    setStrategyProgress((current) => current?.status === 'running'
      ? {
          ...current,
          phase: 'failed',
          status: 'failed',
          headline: state === 'exited' ? `${selectedAgentDisplayName} 已退出` : '本轮 Agent 未完成',
          detail,
          steps: current.steps.map((step) => step.status === 'running'
            ? { ...step, status: 'failed', detail }
            : step),
        }
      : current)
  }, [selectedAgentDisplayName, setAgentStatusText])

  const handleWebShellCommandSubmitted = useCallback((submission: AgentWebShellCommandSubmission) => {
    const session = backendAgentSessionRef.current
    if (session) {
      session.liveProgressRequestId = submission.commandId
      session.reasoningPreview = ''
      session.contentPreview = ''
      storeBackendAgentSession(session)
    }
    setStrategyProgress((current) => current?.requestId === submission.commandId && current.status === 'running'
      ? current
      : createAgentRuntimeProgress({
          requestId: submission.commandId,
          startedAt: submission.submittedAt,
          headline: `已发往 ${selectedAgentDisplayName} 终端`,
          detail: '任务正文和提交键已写入终端，正在等待 Agent 响应。',
        }))
    setStrategyProgressExpanded(false)
  }, [selectedAgentDisplayName])

  async function runBackendAgentRun({ initialUserMessage, initialMemory, origin, analysisId, progress, focusedTask, startingTurn = 1 }: AgentLoopArgs) {
    const activeConfig = modelConfigRef.current
    const scopedAnalysisId = normalizeAnalysisRouteId(analysisId)
    if (scopedAnalysisId) writeAnalysisRouteId(scopedAnalysisId)
    setBusy(true)
    setAgentStatusText('正在创建后端 Agent Run。')
    if (progress) {
      setStrategyProgress(createDeviceValidationProgress(progress))
      setStrategyProgressExpanded(false)
    }
    const projectedPageContext = assistantPageContext(contextRef.current)
    const task = compactTextToTokenLimit(initialUserMessage, 20_000, '[用户输入超过请求传输范围，尾部已截断]').text.slice(0, 90 * 1024)
    const recentMemory = initialMemory.messages.slice(-6).map((item) => ({ role: item.role, content: item.content.slice(-8_000) }))
    try {
      const run = await createBackendAgentRun({
        goal: progress ? '在真实设备上形成最小可证伪的异构实验闭环，并区分构建、部署、执行、等价性与收益证据。' : origin || '根据用户目标操作鸿蒙异构优化工作台。',
        initialInput: JSON.stringify({
          schema_version: 'page_agent_backend_start/v1',
          task,
          execution_sequence: startingTurn,
          prior_memory: recentMemory,
          instruction: '结合当前任务与已保存的真实事实自主决定下一步；不要假定浏览器仍会同步等待。任务由用户显式停止，不设置产品级轮次或工具次数上限。',
        }),
        mode: progress ? 'device_validation' : 'page_agent',
        ...(scopedAnalysisId ? { analysisId: scopedAnalysisId } : {}),
        context: progress ? {
          schema_version: 'device_validation_runtime_context/v1',
          analysis_record_id: scopedAnalysisId,
          target_platform_profile: asRecord(projectedPageContext.target_platform_profile),
          device_validation_facts: progress.facts,
          device_validation_target: {
            requested_unit: progress.requestedUnit ?? null,
            recommended_profile_id: progress.recommendedProfileId ?? null,
            compatible_profile_ids: progress.compatibleProfileIds ?? [],
          },
          device_execution_mode: selectedDeviceBridge ? 'remote_bridge' : 'local_hdc',
          skill_profile: compactSkillProfile(skillProfile),
          ...(selectedDeviceBridge ? { device_selector: { bridge_id: selectedDeviceBridge.bridge_id } } : {}),
        } : pageAgentRuntimeContext(scopedAnalysisId, focusedTask),
        config: activeConfig,
      })
      const previousSession = backendAgentSessionRef.current?.runId === run.run_id
        ? backendAgentSessionRef.current
        : null
      const session: BackendAgentSession = {
        runId: run.run_id,
        ...(scopedAnalysisId ? { analysisId: scopedAnalysisId } : {}),
        origin,
        progress,
        lastSequence: previousSession?.lastSequence ?? 0,
        reasoningPreview: previousSession?.reasoningPreview ?? '',
        contentPreview: previousSession?.contentPreview ?? '',
        liveProgressRequestId: previousSession?.liveProgressRequestId,
        handledUiRequests: previousSession?.handledUiRequests ?? new Set(),
        state: run.state,
      }
      if (progress && scopedAnalysisId) {
        storeDeviceValidationSnapshot({
          schemaVersion: 'device_validation_snapshot/v1',
          analysisId: scopedAnalysisId,
          requestId: progress.requestId,
          runId: run.run_id,
          status: run.state,
          facts: progress.facts,
          ...(progress.requestedUnit ? { requestedUnit: progress.requestedUnit } : {}),
          ...(progress.recommendedProfileId ? { recommendedProfileId: progress.recommendedProfileId } : {}),
          ...(progress.compatibleProfileIds ? { compatibleProfileIds: progress.compatibleProfileIds } : {}),
          updatedAt: new Date().toISOString(),
        })
      }
      setLastBackendRunId(run.run_id)
      attachBackendAgentSession(session)
      setAgentRun(agentRunStateFromBackend(run))
      // A running backend Run is already durable.  Only paused/recoverable
      // states get a visible "继续" checkpoint; otherwise the old UI made it
      // look as if users should resume a task that was still running.
      setAgentContinuation(null)
      storeAgentCheckpoint(null)
    } catch (error) {
      setBusy(false)
      setAgentRun(null)
      setMessages((current) => [...current, {
        id: randomUuid(),
        role: 'system',
        content: `后端 Agent Run 创建失败：${error instanceof Error ? error.message : '未知错误'}。`,
      }])
    } finally {
      setAgentStatusText('')
    }
  }

  function actionObservation(results: AgentActionObservation[], turn: number): string {
    const evidence = results.map((result) => ({ action_id: result.actionId, result: result.result }))
    const observationTokenLimit = results.some((result) => result.actionId === 'build.create' || result.actionId === 'device.deploy_artifact' || result.actionId === 'device.execute_shell')
      ? 5_000
      : results.some((result) => result.actionId === 'build.describe_profile')
        ? 3_000
        : results.some((result) => result.actionId.startsWith('device.'))
          ? 1_200
          : 420
    const bounded = compactTextToTokenLimit(JSON.stringify(evidence), observationTokenLimit, '[页面操作结果已按预算压缩]')
    return `第 ${turn} 轮页面操作已执行。以下是唯一可作为后续事实的操作结果：${bounded.text}\n资源类型提醒：所有 *_artifact_id 只是后续专用 action 的引用，不是文件路径；remote_binary_path 是手机路径，也不能交给 workspace.read_file。只有结果明确返回的代码仓相对 path 才能用于 workspace.read_file。\n请基于这些结果自行判断下一步：可以回答、读取或写入允许范围内的文件、继续分析、暂存实验、构建上传、执行设备操作，或停止。只调用当前已经具备完整输入的 action；若下一步依赖本轮 action 的返回值，应留到下一轮。若实际结果要求用户选择或补充信息，停止调用 action 并用中文说明。`
  }

  // Compatibility implementation retained while the page-action surface is
  // being migrated. New work always enters the durable backend Runtime below;
  // this function is deliberately not selected by normal UI flows.
  async function runBrowserAgentLoop({ initialUserMessage, initialMemory, origin, analysisId, progress, startingTurn = 1 }: AgentLoopArgs) {
    if (progress) progress.facts = normalizeDeviceValidationFacts(progress.facts)
    let workingMemory = initialMemory
    let nextUserMessage = initialUserMessage
    let completed = false
    let budgetExhausted = false
    let logicalTurn = Math.max(1, startingTurn)
    const maxTurns = config.agentMaxTurns
    const maxActionsPerTurn = config.agentMaxActionsPerTurn
    let lastCheckpoint: AgentContinuation = {
      schemaVersion: 'page_agent_checkpoint/v1',
      nextUserMessage,
      memory: workingMemory,
      origin,
      analysisId,
      progress,
      reason: 'interrupted',
      resumeTurn: logicalTurn,
      savedAt: new Date().toISOString(),
    }

    setBusy(true)
    setAgentContinuation(null)
    storeAgentCheckpoint(null)
    setAgentStatusText('')
    setAgentRun({ turn: 0, maxTurns, maxActionsPerTurn })
    if (progress) {
      setStrategyProgress(createDeviceValidationProgress(progress))
      setStrategyProgressExpanded(false)
    }
    await appendAnalysisTraceEvent(analysisId, {
      category: 'workflow',
      operation: progress ? 'device_validation.agent.start' : 'page_agent.run.start',
      phase: progress?.kind ?? 'assistant',
      status: 'started',
      requestId: progress?.requestId,
      runId: progress?.requestId,
      payload: {
        origin,
        max_turns: maxTurns,
        max_actions_per_turn: maxActionsPerTurn,
        starting_turn: logicalTurn,
        initial_memory_message_count: initialMemory.messages.length,
        memory_isolated: Boolean(progress),
      },
    })
    try {
      for (let turn = 1; turn <= maxTurns; turn += 1) {
        logicalTurn = Math.max(1, startingTurn) + turn - 1
        lastCheckpoint = {
          ...lastCheckpoint,
          nextUserMessage,
          memory: workingMemory,
          reason: 'interrupted',
          resumeTurn: logicalTurn,
          savedAt: new Date().toISOString(),
          failure: undefined,
        }
        storeAgentCheckpoint(lastCheckpoint)
        setAgentRun({ turn, maxTurns, maxActionsPerTurn })
        if (progress) {
          setStrategyProgress((current) => current?.requestId === progress.requestId ? {
            ...current,
            phase: 'model_pending',
            status: 'running',
            headline: `第 ${logicalTurn} 轮：模型正在决定下一步`,
            detail: '正在根据已注入任务和上一轮真实工具结果选择下一项最小可证伪操作。',
            streamPreview: '',
            streamedReasoningChars: 0,
            streamedContentChars: 0,
            steps: current.steps.map((step) => step.id === 'model'
              ? { ...step, status: 'running', detail: `正在执行第 ${logicalTurn} 轮自主决策。` }
              : step),
          } : current)
        }
        const systemPrompt = assistantSystemPrompt(actions, config, skillProfile)
        const turnMessage = assistantTurnMessage(
          contextRef.current,
          config.contextWindowTokens,
          nextUserMessage,
          maxTurns - turn + 1,
          pendingAttachmentListRef.current,
          pendingTextInputListRef.current,
        )
        const request = buildBoundedConversationRequest({
          systemPrompt,
          memory: workingMemory,
          userMessage: turnMessage,
          contextWindowTokens: config.contextWindowTokens,
          outputReserveTokens: config.agentMaxOutputTokens,
        })
        if (request.budget.exceedsWindow) {
          throw new Error(`当前页面上下文与本条输入超过已配置的 ${formatTokenCount(request.budget.contextWindowTokens)} 窗口；请提高“上下文窗口”配置，或缩小粘贴内容。`)
        }
        setLastTokenBudget(request.budget)
        let reasoningPreview = ''
        let contentPreview = ''
        const payload = await requestModelChatCompletionStream(config, request.messages, {
          maxTokens: request.budget.outputReserveTokens,
          observability: {
            analysisId: analysisId ?? '',
            operation: progress ? 'device_validation.agent_decision' : 'page_agent.decision',
            requestId: `${progress?.requestId ?? randomUuid()}:turn:${logicalTurn}`,
            runId: progress?.requestId,
            turn: logicalTurn,
          },
          onRetry: (retry: ModelRetryProgress) => {
            reasoningPreview = ''
            contentPreview = ''
            const status = `上游模型服务繁忙，${Math.ceil(retry.delayMs / 1000)} 秒后自动进行第 ${retry.nextAttempt}/${retry.maxAttempts} 次尝试；当前任务检查点已保留。`
            setAgentStatusText(status)
            if (progress) {
              setStrategyProgress((current) => current?.requestId === progress.requestId ? {
                ...current,
                status: 'running',
                headline: `第 ${logicalTurn} 轮：模型服务繁忙，正在自动恢复`,
                detail: status,
              } : current)
            }
          },
          onProgress: (stream) => {
            setAgentStatusText('')
            if (!progress) return
            reasoningPreview = `${reasoningPreview}${stream.reasoningDelta}`.slice(-1400)
            contentPreview = `${contentPreview}${stream.contentDelta}`.slice(-1400)
            setStrategyProgress((current) => current?.requestId === progress.requestId ? {
              ...current,
              detail: '以下内容由模型实时生成。',
              streamPreview: [
                reasoningPreview ? `[思考]\n${reasoningPreview}` : '',
                contentPreview ? `[输出]\n${contentPreview}` : '',
              ].filter(Boolean).join('\n\n'),
              streamedReasoningChars: stream.reasoningChars,
              streamedContentChars: stream.contentChars,
            } : current)
          },
        })
        setAgentStatusText('')
        const content = payload.choices?.[0]?.message?.content ?? ''
        recordModelUsage(payload.tokenUsage, request.budget)
        const modelResponse = parseAgentResponse(content)
        const requestedActions = modelResponse.actions
        await appendAnalysisTraceEvent(analysisId, {
          category: 'contract',
          operation: progress ? 'device_validation.agent_decision' : 'page_agent.decision',
          phase: progress?.kind ?? 'assistant',
          status: modelResponse.parseStatus === 'strict' ? 'parsed' : modelResponse.parseStatus,
          requestId: progress?.requestId,
          runId: progress?.requestId,
          turn: logicalTurn,
          payload: {
            reply: modelResponse.reply,
            actions: requestedActions,
            requested_action_count: requestedActions.length,
            parse_status: modelResponse.parseStatus,
            parse_error: modelResponse.parseError ?? null,
          },
        })
        const actionResults: AgentActionObservation[] = []
        const executableActions = requestedActions.slice(0, maxActionsPerTurn)
        if (requestedActions.length > maxActionsPerTurn) {
          actionResults.push({
            actionId: 'agent.action_budget',
            input: {},
            result: `本轮操作预算为 ${maxActionsPerTurn}；其余 action 未执行，下一轮可根据当前结果重新决定。`,
          })
        }
        if (progress && executableActions.length > 0) {
          setStrategyProgress((current) => current?.requestId === progress.requestId ? {
            ...current,
            phase: 'contract_validating',
            headline: `第 ${logicalTurn} 轮：执行 ${executableActions.length} 项工具操作`,
            detail: executableActions.map((action) => action.action_id || '未知操作').join('、'),
            steps: current.steps.map((step) => step.id === 'model'
              ? { ...step, status: 'succeeded', detail: `第 ${logicalTurn} 轮已形成可执行决策。` }
              : step.id === 'actions'
                ? { ...step, status: 'running', detail: `正在执行：${executableActions.map((action) => action.action_id || '未知操作').join('、')}` }
                : step),
          } : current)
        }
        for (const action of executableActions) {
          if (!action.action_id || !actionIds.has(action.action_id)) {
            actionResults.push({ actionId: action.action_id ?? '未知操作', input: action.input ?? {}, result: '操作未注册', failed: true })
            continue
          }
          const input = action.input ?? {}
          const dependencyIssue = progress ? deviceActionDependencyIssue(action, progress.facts) : null
          if (dependencyIssue) {
            await appendAnalysisTraceEvent(analysisId, {
              category: 'contract',
              operation: 'device_validation.action_dependency',
              phase: 'device_validation',
              status: 'deferred',
              requestId: progress?.requestId,
              runId: progress?.requestId,
              turn: logicalTurn,
              payload: { action_id: action.action_id, input, reason: dependencyIssue },
            })
            actionResults.push({ actionId: action.action_id, input, result: JSON.stringify({ status: 'deferred', reason: dependencyIssue }) })
            continue
          }
          try {
            const result = await executeRequestedAction(action.action_id, input, analysisId, logicalTurn)
            actionResults.push({ actionId: action.action_id, input, output: result, result: shortResult(result) })
          } catch (error) {
            const output = { status: 'failed', ...analysisErrorPayload(error) }
            actionResults.push({ actionId: action.action_id, input, output, result: JSON.stringify(output), failed: true })
          }
        }
        if (progress && actionResults.length > 0) progress.facts = updateDeviceValidationFacts(progress.facts, actionResults)
        workingMemory = {
          ...request.memory,
          messages: [
            ...request.memory.messages,
            { role: 'user', content: turnMessage },
            {
              role: 'assistant',
              content: modelResponse.parseStatus === 'strict'
                ? content
                : JSON.stringify({ reply: modelResponse.reply, actions: requestedActions, parse_status: modelResponse.parseStatus }),
            },
          ],
        }
        setConversationMemory(workingMemory)
        setAgentStatusText('')
        setMessages((current) => [
          ...current,
          ...(modelResponse.parseStatus === 'recovered' ? [{
            id: randomUuid(),
            role: 'system' as const,
            content: `模型的外层 JSON 存在轻微格式错误，平台已恢复 ${requestedActions.length} 个结构完整 action 并继续执行。`,
          }] : []),
          {
            id: randomUuid(),
            role: 'assistant',
            content: modelResponse.reply ?? (actionResults.length > 0 ? '已执行页面操作，正在根据真实结果决定下一步。' : '本轮未返回可展示的回答。'),
            actionResults,
          },
        ])
        const terminal = progress ? deviceValidationTerminal(progress.facts) : null
        if (actionResults.length === 0 && progress && !terminal) {
          const continuation = deviceValidationContinuationMessage(progress.facts, modelResponse.parseStatus)
          await appendAnalysisTraceEvent(analysisId, {
            category: 'workflow',
            operation: 'device_validation.completion_gate',
            phase: 'device_validation',
            status: 'continued',
            requestId: progress.requestId,
            runId: progress.requestId,
            turn: logicalTurn,
            payload: {
              parse_status: modelResponse.parseStatus,
              reason: 'no_terminal_device_execution_or_environment_blocker',
              facts: progress.facts,
            },
          })
          setMessages((current) => [...current, {
            id: randomUuid(),
            role: 'system',
            content: '真机任务尚未取得已上传实验的手机执行结果，也没有真实环境阻塞证据；运行时已保留当前事实并自动进入下一轮，不会把“等待返回”当成完成。',
          }])
          setStrategyProgress((current) => current?.requestId === progress.requestId ? {
            ...current,
            phase: 'model_pending',
            status: 'running',
            headline: '真机验证尚未形成终态，正在自动继续',
            detail: '本轮没有可执行 action，但尚未取得手机执行结果；运行时已把真实事实作为下一轮输入。',
            steps: current.steps.map((step) => step.id === 'model'
              ? { ...step, status: 'running', detail: '运行时完成门禁要求继续决策。' }
              : step.id === 'actions'
                ? { ...step, status: 'warning', detail: '本轮未执行工具；等待下一轮选择可满足依赖的 action。' }
                : step),
          } : current)
          lastCheckpoint = {
            schemaVersion: 'page_agent_checkpoint/v1',
            nextUserMessage: continuation,
            memory: workingMemory,
            origin,
            analysisId,
            progress,
            reason: 'interrupted',
            resumeTurn: logicalTurn + 1,
            savedAt: new Date().toISOString(),
          }
          storeAgentCheckpoint(lastCheckpoint)
          if (turn === maxTurns) {
            budgetExhausted = true
            lastCheckpoint = { ...lastCheckpoint, reason: 'turn_budget', savedAt: new Date().toISOString() }
            setAgentContinuation(lastCheckpoint)
            storeAgentCheckpoint(lastCheckpoint)
            setStrategyProgress((current) => current?.requestId === progress.requestId ? {
              ...current,
              status: 'warning',
              headline: '本次真机验证已到达轮次预算',
              detail: '尚未得到手机执行结果；当前真实工具结果和下一步恢复点已保存。',
            } : current)
            break
          }
          nextUserMessage = continuation
          await new Promise<void>((resolve) => window.setTimeout(resolve, 0))
          continue
        }
        if (actionResults.length === 0) {
          completed = true
          if (progress) {
            const terminalDetail = terminal?.detail ?? modelResponse.reply ?? '模型已停止调用工具，并形成当前证据范围内的结论。'
            setStrategyProgress((current) => current?.requestId === progress.requestId ? {
              ...current,
              phase: 'completed',
              status: terminal?.warning ? 'warning' : 'succeeded',
              headline: terminal?.kind === 'remote_execution_observed' ? '已取得真机实验执行返回' : '真机验证自主任务已形成当前结论',
              detail: terminalDetail,
              steps: current.steps.map((step) => step.id === 'model' || step.id === 'verdict'
                ? { ...step, status: terminal?.warning ? 'warning' : 'succeeded' }
                : step.id === 'actions' && step.status === 'running'
                  ? { ...step, status: terminal?.warning ? 'warning' : 'succeeded' }
                  : step),
            } : current)
          }
          break
        }
        if (progress) {
          setStrategyProgress((current) => current?.requestId === progress.requestId ? {
            ...current,
            headline: `第 ${logicalTurn} 轮工具结果已返回`,
            detail: actionResults.map((result) => `${result.actionId}：${result.result.slice(0, 120)}`).join('；'),
            steps: current.steps.map((step) => step.id === 'actions'
              ? { ...step, status: actionResults.some((result) => result.result.includes('"status_code":422') || result.result.includes('"status":"failed"')) ? 'warning' : 'succeeded', detail: `已取得 ${actionResults.length} 项真实返回，下一轮将据此修订。` }
              : step),
          } : current)
        }
        const observation = actionObservation(actionResults, logicalTurn)
        lastCheckpoint = {
          schemaVersion: 'page_agent_checkpoint/v1',
          nextUserMessage: observation,
          memory: workingMemory,
          origin,
          analysisId,
          progress,
          reason: 'interrupted',
          resumeTurn: logicalTurn + 1,
          savedAt: new Date().toISOString(),
        }
        storeAgentCheckpoint(lastCheckpoint)
        if (turn === maxTurns) {
          budgetExhausted = true
          lastCheckpoint = {
            ...lastCheckpoint,
            reason: 'turn_budget',
            savedAt: new Date().toISOString(),
          }
          setAgentContinuation(lastCheckpoint)
          storeAgentCheckpoint(lastCheckpoint)
          if (progress) {
            setStrategyProgress((current) => current?.requestId === progress.requestId ? {
              ...current,
              status: 'warning',
              headline: '本次真机验证已到达轮次预算',
              detail: '真实观察和任务上下文已保存，可继续更多轮次而无需重新开始。',
            } : current)
          }
          break
        }
        nextUserMessage = observation
        await new Promise<void>((resolve) => window.setTimeout(resolve, 0))
      }
      setConversationMemory(workingMemory)
      if (completed) {
        setAgentContinuation(null)
        storeAgentCheckpoint(null)
      }
      await appendAnalysisTraceEvent(analysisId, {
        category: 'workflow',
        operation: progress ? 'device_validation.agent.complete' : 'page_agent.run.complete',
        phase: progress?.kind ?? 'assistant',
        status: budgetExhausted && !completed ? 'budget_exhausted' : 'succeeded',
        requestId: progress?.requestId,
        runId: progress?.requestId,
        payload: {
          completed,
          budget_exhausted: budgetExhausted,
          next_resume_turn: budgetExhausted ? lastCheckpoint.resumeTurn : null,
          retained_message_count: workingMemory.messages.length,
          compacted_message_count: workingMemory.compactedMessageCount,
        },
      })
      if (budgetExhausted && !completed) {
        setMessages((current) => [...current, {
          id: randomUuid(),
          role: 'system',
          content: `本次自主任务已使用 ${maxTurns} 轮预算，真实观察与上下文已保留。可以继续推理，让模型自行决定下一步。`,
        }])
      }
    } catch (error) {
      const transient = isRetryableModelError(error)
      const pausedCheckpoint: AgentContinuation = {
        ...lastCheckpoint,
        reason: 'model_error',
        savedAt: new Date().toISOString(),
        failure: {
          message: error instanceof Error ? error.message : '未知模型错误',
          transient,
        },
      }
      setConversationMemory(pausedCheckpoint.memory)
      setAgentContinuation(pausedCheckpoint)
      storeAgentCheckpoint(pausedCheckpoint)
      await appendAnalysisTraceEvent(analysisId, {
        category: 'workflow',
        operation: progress ? 'device_validation.agent.paused' : 'page_agent.run.paused',
        phase: progress?.kind ?? 'assistant',
        status: 'paused',
        requestId: progress?.requestId,
        runId: progress?.requestId,
        turn: pausedCheckpoint.resumeTurn,
        payload: {
          ...analysisErrorPayload(error),
          transient,
          checkpoint: {
            schema_version: pausedCheckpoint.schemaVersion,
            resume_turn: pausedCheckpoint.resumeTurn,
            retained_message_count: pausedCheckpoint.memory.messages.length,
            compacted_message_count: pausedCheckpoint.memory.compactedMessageCount,
          },
        },
      })
      if (progress) {
        setStrategyProgress((current) => current?.requestId === progress.requestId
          ? {
            ...current,
            status: 'warning',
            headline: transient ? '模型服务暂时不可用，真机任务已暂停' : '本轮模型请求未完成，真机任务已暂停',
            detail: `已保存到第 ${pausedCheckpoint.resumeTurn} 轮的输入与真实工具结果；恢复时不会重新执行硬件路由或已完成操作。`,
          }
          : current)
      }
      setMessages((current) => [...current, {
        id: randomUuid(),
        role: 'system',
        content: `${transient ? '上游模型服务在自动重试后仍不可用' : '本轮助手请求未完成'}：${error instanceof Error ? error.message : '未知错误'}。任务已停在第 ${pausedCheckpoint.resumeTurn} 轮之前；可点击继续按钮，或发送新消息让模型结合保存的工具结果判断下一步。`,
      }])
    } finally {
      setAgentRun(null)
      setAgentStatusText('')
      setBusy(false)
    }
  }

  // Kept only for decoding a pre-migration browser checkpoint. New work never
  // dispatches through this loop, but keeping the symbol reachable avoids
  // silently dropping an older in-memory recovery path during an upgrade.
  void runBrowserAgentLoop

  // All new autonomous work is backend-owned.  The old browser loop remains
  // only as a compatibility implementation while saved pre-migration
  // checkpoints expire; neither a page task nor device validation dispatches
  // through it any more.
  runAgentLoopRef.current = runBackendAgentRun
  dispatchUserTurnRef.current = dispatchUserTurn

  useEffect(() => {
    const listener = (event: Event) => {
      const detail = asRecord((event as CustomEvent<unknown>).detail)
      const task = typeof detail.message === 'string' ? detail.message.trim() : ''
      const commandId = typeof detail.request_id === 'string' && detail.request_id ? detail.request_id : randomUuid()
      if (!task) {
        reportAgentCommandStatus({ requestId: commandId, status: 'rejected', message: '页面命令内容为空。' })
        return
      }
      artifactDeliveryNotifierRef.current?.preparePermission()
      const analysisId = typeof detail.analysis_id === 'string' ? detail.analysis_id : ''
      const assertCurrent = pageAgentSubmissionGuard()
      void waitForPageAgent(assertCurrent).then((shell) => shell.sendCommand(task, { commandId, analysisId, assertCurrent })).then(() => {
        reportAgentCommandStatus({ requestId: commandId, status: 'accepted' })
      }).catch((error) => {
        const message = error instanceof Error ? error.message : `页面命令未能写入 ${selectedAgentDisplayName}。`
        setWebShellState('error')
        setWebShellDetail(message)
        reportAgentCommandStatus({ requestId: commandId, status: 'rejected', message })
      })
    }
    window.addEventListener(agentCommandEventName, listener)
    return () => window.removeEventListener(agentCommandEventName, listener)
  }, [waitForPageAgent, pageAgentSubmissionGuard, selectedAgentDisplayName])

  const restoreAnalysisAgentSession = useCallback(async (request: { analysisId: string; runId: string }) => {
    const shell = webShellRef.current
    if (!shell || !agentCliConnected) return false
    let runId = request.runId
    if (!runId) {
      const run = await getAnalysisAgentSession(request.analysisId)
      runId = run?.run_id ?? ''
    }
    if (!runId) return true
    setAgentStatusText('正在恢复该分析记录绑定的 Agent Session。')
    await shell.restoreRun(runId, request.analysisId)
    setMessages((current) => [...current, {
      id: randomUuid(),
      role: 'system',
      content: `已恢复分析记录 ${request.analysisId.slice(0, 8)} 对应的 Agent Run；后续页面操作与对话将继续使用该原生 Session。`,
    }])
    return true
  }, [agentCliConnected, setAgentStatusText])

  useEffect(() => {
    const listener = (event: Event) => {
      const detail = asRecord((event as CustomEvent<unknown>).detail)
      const analysisId = typeof detail.analysis_id === 'string' ? detail.analysis_id.trim() : ''
      const runId = typeof detail.run_id === 'string' ? detail.run_id.trim() : ''
      if (!analysisId) return
      const request = { analysisId, runId }
      pendingAnalysisSessionRestoreRef.current = request
      void restoreAnalysisAgentSession(request).then((restored) => {
        if (restored && pendingAnalysisSessionRestoreRef.current?.analysisId === analysisId) {
          pendingAnalysisSessionRestoreRef.current = null
        }
      }).catch((error) => {
        setAgentStatusText(error instanceof Error ? error.message : 'Agent Session 恢复失败。')
      })
    }
    window.addEventListener('analysis-agent-session-restore-request', listener)
    return () => window.removeEventListener('analysis-agent-session-restore-request', listener)
  }, [restoreAnalysisAgentSession, setAgentStatusText])

  useEffect(() => {
    const pending = pendingAnalysisSessionRestoreRef.current
    if (!pending || !agentCliConnected || !webShellRef.current) return
    void restoreAnalysisAgentSession(pending).then((restored) => {
      if (restored && pendingAnalysisSessionRestoreRef.current === pending) pendingAnalysisSessionRestoreRef.current = null
    }).catch(() => undefined)
  }, [agentCliConnected, restoreAnalysisAgentSession, webShellState])

  async function cancelCurrentBackendRun() {
    const session = backendAgentSessionRef.current
    if (!session || backendRunIsTerminal(session.state)) return
    const priorState = session.state
    const priorProgress = strategyProgress
    session.state = 'cancelled'
    backendAgentSubscriptionRef.current?.()
    backendAgentSubscriptionRef.current = null
    backendAgentSessionRef.current = null
    storeBackendAgentSession(null)
    setBusy(false)
    setAgentRun(null)
    setAgentContinuation(null)
    storeAgentCheckpoint(null)
    setAgentStatusText('')
    setStrategyProgress((current) => current?.status === 'running'
      ? {
          ...current,
          phase: 'failed',
          status: 'warning',
          headline: '真机验证已停止',
          detail: '已停止后续模型轮次和新操作；已经返回的构建与设备事实仍保留在执行记录中。',
          steps: current.steps.map((step) => step.status === 'running'
            ? { ...step, status: 'warning', detail: '用户已停止当前 Agent Run。' }
            : step),
        }
      : current)
    try {
      const run = await cancelBackendAgentRun(session.runId)
      session.state = run.state
      setMessages((current) => [...current, {
        id: randomUuid(),
        role: 'system',
        content: '当前后端 Agent Run 已停止。已发生的模型、构建和设备事实仍保留在执行记录中，停止后的模型流与动作不会继续回填页面。',
      }])
    } catch (error) {
      session.state = priorState
      backendAgentSessionRef.current = session
      storeBackendAgentSession(session)
      attachBackendAgentSession(session)
      setAgentRun({
        turn: agentRun?.turn ?? 0,
        maxTurns: agentRun?.maxTurns ?? config.agentMaxTurns,
        maxActionsPerTurn: agentRun?.maxActionsPerTurn ?? config.agentMaxActionsPerTurn,
        state: priorState,
        detail: '取消请求未送达，已重新连接原 Agent Run。',
      })
      setStrategyProgress(priorProgress)
      setBusy(priorState === 'running' || priorState === 'waiting_for_tool' || priorState === 'waiting_for_device' || priorState === 'compacting')
      setMessages((current) => [...current, {
        id: randomUuid(),
        role: 'system',
        content: `停止后端 Agent Run 失败：${error instanceof Error ? error.message : '未知错误'}。已重新连接原任务，避免把仍在运行的任务误显示为已停止。`,
      }])
    }
  }

  function stopCurrentInference() {
    window.dispatchEvent(new CustomEvent('page-agent-stop-current-inference'))
    const strategyController = activeStrategyAbortRef.current
    if (strategyController && !strategyController.signal.aborted) {
      strategyController.abort(new DOMException('推理已由用户停止。', 'AbortError'))
      setAgentStatusText('正在停止当前策略推理。')
    }
    if (webShellTerminal?.state === 'running') {
      void webShellRef.current?.interrupt()
      return
    }
    if (backendAgentSessionRef.current) {
      void cancelCurrentBackendRun()
    }
  }

  useEffect(() => {
    const listener = (event: Event) => {
      const detail = asRecord((event as CustomEvent<unknown>).detail)
      const task = typeof detail.message === 'string' ? detail.message.trim() : ''
      const analysisId = typeof detail.analysis_id === 'string' ? detail.analysis_id : ''
      const requestId = typeof detail.request_id === 'string' && detail.request_id ? detail.request_id : randomUuid()
      const displayMessage = typeof detail.display_message === 'string' && detail.display_message.trim()
        ? detail.display_message.trim()
        : task
      const requiredSkillId = typeof detail.required_skill_id === 'string' ? detail.required_skill_id.trim()
        : typeof detail.skill_id === 'string' ? detail.skill_id.trim() : ''
      const taskKind = typeof detail.task_kind === 'string' ? detail.task_kind : 'device_validation'
      const focusedTaskKind: FocusedAgentTaskContext['kind'] | undefined = taskKind === 'custom_api_kb'
        || taskKind === 'application_flow_discovery'
        || taskKind === 'application_flow_generation'
        || taskKind === 'application_flow_trace_anchors'
        || taskKind === 'application_flow_runtime_trace_alignment'
        || taskKind === 'code_knowledge_analysis'
        || taskKind === 'experimental_flow_hiperf'
        || taskKind === 'experimental_flow_trace'
        || taskKind === 'scenario_capture'
        || taskKind === 'parallel_modeling'
        || taskKind === 'workload_features'
        || taskKind === 'optimization_calibration'
        ? taskKind
        : undefined
      const focusedTask = Boolean(focusedTaskKind)
      const focusedTaskContext: FocusedAgentTaskContext | undefined = focusedTaskKind ? {
        kind: focusedTaskKind,
        requestContext: Object.fromEntries(Object.entries(detail).filter(([key]) => !['message', 'display_message'].includes(key))),
        sourceConnectorId: typeof detail.source_connector_id === 'string' ? detail.source_connector_id : undefined,
        sourceSnapshotId: typeof detail.source_snapshot_id === 'string' ? detail.source_snapshot_id : undefined,
        subpath: typeof detail.subpath === 'string' ? detail.subpath : undefined,
        graphId: typeof detail.graph_id === 'string' ? detail.graph_id : undefined,
        currentReportId: typeof detail.current_report_id === 'string' ? detail.current_report_id : undefined,
        discoveryArtifactId: typeof detail.discovery_artifact_id === 'string' ? detail.discovery_artifact_id : undefined,
        flowId: typeof detail.flow_id === 'string' ? detail.flow_id : undefined,
        scenarioArtifactId: typeof detail.scenario_artifact_id === 'string' ? detail.scenario_artifact_id : undefined,
        traceAnalysisArtifactId: typeof detail.trace_analysis_artifact_id === 'string' ? detail.trace_analysis_artifact_id : undefined,
        evidenceArtifactId: typeof detail.evidence_artifact_id === 'string' ? detail.evidence_artifact_id : undefined,
        resultArtifactId: typeof detail.result_artifact_id === 'string' ? detail.result_artifact_id : undefined,
        analysisKind: detail.analysis_kind === 'hiperf' || detail.analysis_kind === 'trace' ? detail.analysis_kind : undefined,
        applicationFlowAnalysisId: typeof detail.application_flow_analysis_id === 'string'
          ? detail.application_flow_analysis_id
          : undefined,
        sourceScopes: Array.isArray(detail.source_scopes) ? detail.source_scopes.map(asRecord).flatMap((item) => {
          const connectorId = typeof item.source_connector_id === 'string' ? item.source_connector_id : ''
          if (!connectorId) return []
          return [{
            sourceConnectorId: connectorId,
            sourceSnapshotId: typeof item.source_snapshot_id === 'string' ? item.source_snapshot_id : undefined,
            subpath: typeof item.subpath === 'string' ? item.subpath : undefined,
            graphId: typeof item.code_graph_id === 'string'
              ? item.code_graph_id
              : typeof item.graph_id === 'string' ? item.graph_id : undefined,
          }]
        }) : undefined,
      } : undefined
      const initialDeviceFacts = normalizeDeviceValidationFacts(detail.device_facts)
      const requestedUnit = typeof detail.requested_unit === 'string' ? detail.requested_unit : ''
      const recommendedProfileId = typeof detail.recommended_profile_id === 'string' ? detail.recommended_profile_id : ''
      const compatibleProfileIds = Array.isArray(detail.compatible_profile_ids)
        ? detail.compatible_profile_ids.filter((item): item is string => typeof item === 'string' && item.trim().length > 0).slice(0, 8)
        : []
      if (!task) {
        reportAgentAutonomousTaskStatus({ requestId, status: 'rejected', message: '自主任务内容为空。' })
        return
      }
      artifactDeliveryNotifierRef.current?.preparePermission()
      const progress: AgentTaskProgressContext | undefined = focusedTask ? undefined : {
          requestId,
          kind: 'device_validation',
          label: '真机验证',
          startedAt: new Date().toISOString(),
          facts: initialDeviceFacts,
          ...(requestedUnit ? { requestedUnit } : {}),
          ...(recommendedProfileId ? { recommendedProfileId } : {}),
          ...(compatibleProfileIds.length > 0 ? { compatibleProfileIds } : {}),
        }
      webShellProgressRef.current = progress
      if (progress) {
        setStrategyProgress(createDeviceValidationProgress(progress))
        setStrategyProgressExpanded(false)
      }
      const agentSessionId = webShellTerminal?.agent_session_id || webShellTerminal?.terminal_session_id || ''
      const shouldLoadSkill = Boolean(requiredSkillId && (!agentSessionId || !sessionHasLoadedSkill(agentSessionId, requiredSkillId)))
      const command = shouldLoadSkill
        ? `请加载 ${requiredSkillId} Skill。加载完成后执行当前阶段任务：\n${task}`
        : task
      const runtimeContext = pageAgentRuntimeContext(analysisId, focusedTaskContext)
      const assertSessionCurrent = pageAgentSubmissionGuard()
      const assertCurrent = () => { assertAgentTaskSubmissionActive(requestId); assertSessionCurrent() }
      void waitForPageAgent(assertCurrent).then((shell) => shell.sendCommand(command, {
        commandId: requestId,
        assertCurrent,
        analysisId,
        goal: focusedTask
          ? displayMessage
          : '在真实设备上形成最小可证伪的异构实验闭环，并区分构建、部署、执行、等价性与收益证据。',
        mode: focusedTask ? 'page_agent' : 'device_validation',
        context: runtimeContext,
      })).then(() => {
        if (shouldLoadSkill && agentSessionId) rememberSessionSkill(agentSessionId, requiredSkillId)
        reportAgentAutonomousTaskStatus({ requestId, status: 'accepted' })
      }).catch((error) => {
        const message = error instanceof Error ? error.message : `自主任务未能写入 ${selectedAgentDisplayName}。`
        setWebShellState('error')
        setWebShellDetail(message)
        reportAgentAutonomousTaskStatus({ requestId, status: 'rejected', message })
      })
    }
    window.addEventListener('page-agent-autonomous-task', listener)
    return () => window.removeEventListener('page-agent-autonomous-task', listener)
  }, [waitForPageAgent, pageAgentSubmissionGuard, pageAgentRuntimeContext, selectedAgentDisplayName, webShellTerminal?.agent_session_id, webShellTerminal?.terminal_session_id])

  const activeRunState = webShellState === 'working'
    ? 'running'
    : webShellState === 'error'
      ? 'failed'
      : webShellState === 'exited'
        ? 'cancelled'
        : agentRun?.state ?? backendAgentSessionRef.current?.state
  const activeSessionLabel = webShellTerminal?.agent_session_id
    ? webShellTerminal.agent_session_id.replace(/^session_/, '').slice(0, 10)
    : lastBackendRunId
      ? lastBackendRunId.replace(/^run_/, '').slice(0, 10)
      : '未创建'
  const currentAnalysisId = normalizeAnalysisRouteId(readAnalysisRouteId())
  const transportOnline = webShellTransport.state === 'online'
  const transportRecovering = ['initializing', 'recovering', 'restoring'].includes(webShellTransport.state)
  const transportStatusLabel = transportOnline
    ? webShellState === 'working' ? '运行中 · PTY 在线' : '空闲 · PTY 在线'
    : webShellTransport.state === 'initializing'
      ? '正在建立 PTY 链路'
        : webShellTransport.state === 'restoring'
          ? '正在恢复 Agent Session'
        : webShellTransport.state === 'recovering'
          ? `输出链路恢复中 · 第 ${Math.max(1, webShellTransport.retryAttempt)} 次`
          : webShellTransport.state === 'offline'
            ? webShellTerminal ? '浏览器链路中断 · PTY 待确认' : 'PTY 尚未建立'
            : webShellTransport.state === 'closed'
              ? 'PTY 已关闭'
              : 'PTY 状态未知'
  const transportStatusTitle = transportOnline
    ? `${selectedAgentDisplayName} PTY 链路在线；后端 Session 不设置空闲超时。`
    : webShellTransport.detail
  const visibleWebShellDetail = transportOnline
    ? webShellDetail
    : webShellTransport.detail || webShellDetail
  return (
    <aside className="agent-panel" aria-label="页面助手">
      <div className="agent-heading">
        <div className="agent-heading-title">
          <h2>页面助手</h2>
          <span className={`agent-mcp-state ${selectedAgentMcpState}`} title={selectedAgentMcpTitle}>{selectedAgentMcpLabel}</span>
        </div>
        <div className="agent-heading-actions">
          <span
            className={`connection-dot ${transportOnline ? 'is-connected' : transportRecovering ? 'is-recovering' : webShellTransport.state === 'offline' ? 'is-offline' : agentCliConnected || engineConnection === 'model_configuration_required' ? 'is-service-ready' : ''}`}
            title={webShellTerminal
              ? transportStatusTitle
              : agentCliConnected
                ? `${selectedAgentDisplayName} Agent Engine 已就绪，PTY 尚未连接。`
              : !selectedAgentAdapter
                ? agentCliCatalogDetail
              : engineConnection === 'checking'
                ? '正在检查 Agent Engine'
                : engineConnection === 'model_configuration_required'
                  ? '后端 Agent Engine 已启动，模型待配置'
                  : `${selectedAgentDisplayName} 不可用`}
          />
          {(busy || strategyRequestBusy || strategyProgress?.status === 'running') && <button className="icon-button agent-stop-button" type="button" title="停止当前推理及后续工具操作" aria-label="停止当前推理" onClick={stopCurrentInference}><Square size={14} /></button>}
          {lastBackendRunId && <a className="icon-button" href={`${apiBase}/v1/agent-runs/${encodeURIComponent(lastBackendRunId)}/execution-trace`} download title="下载当前或最近一次后端 Agent Run 的脱敏执行记录" aria-label="下载后端 Agent Run 执行记录"><Download size={15} /></a>}
          <button className="icon-button" type="button" title="页面助手设置" aria-label="页面助手设置" onClick={() => setSettingsOpen((open) => !open)}>
            <Settings2 size={16} />
          </button>
        </div>
      </div>

      {settingsOpen && (
        <section className="agent-settings" aria-label="页面助手设置">
          <div className="form-field">
            <label htmlFor="agent-cli-adapter">页面 Agent</label>
            <select id="agent-cli-adapter" value={selectedAgentAdapterId} disabled={agentCliAdapters.length === 0} onChange={(event) => selectAgentAdapter(event.target.value)}>
              {agentCliAdapters.length === 0 && <option value={selectedAgentAdapterId}>Agent Adapter 目录尚未加载</option>}
              {agentCliAdapters.map((adapter) => <option key={adapter.adapter_id} value={adapter.adapter_id} disabled={!adapter.available}>{adapter.display_name}{adapter.available ? '' : '（不可用）'}</option>)}
            </select>
            <small className="form-field-note">{selectedAgentAdapter?.description ? `${selectedAgentAdapter.description} ${agentCliCatalogDetail}` : agentCliCatalogDetail}{selectedAgentAdapter?.diagnostic ? ` ${selectedAgentAdapter.diagnostic}` : ''}{selectedAgentAdapter?.mcp_integration.warning ? ` ${selectedAgentAdapter.mcp_integration.warning}` : selectedAgentAdapter?.mcp_integration.configured ? ` 平台 MCP 已按 ${selectedAgentAdapter.mcp_integration.format} 格式投影，并通过 ${selectedAgentAdapter.mcp_integration.delivery === 'argv_and_environment' ? '启动参数和环境变量' : '工作区和环境变量'}交付。` : ''}</small>
            {agentCliCatalogState !== 'ready' && <button className="quiet-button compact agent-catalog-retry" type="button" disabled={agentCliCatalogState === 'loading'} onClick={retryAgentCliCatalog}>{agentCliCatalogState === 'loading' ? <LoaderCircle size={13} className="spin" /> : <RefreshCw size={13} />}重新加载 Agent 目录</button>}
          </div>
          <p className={`model-relay-note ${engineConnection === 'unavailable' ? 'warning' : ''}`}>
            {selectedAgentAdapterId !== 'kimi'
              ? `${selectedAgentDisplayName} 由后端 PTY 托管；页面按钮、终端输入与平台 MCP 共用当前页面 Run。`
              : connected
              ? 'Kimi Agent Engine 与模型均已就绪。后端负责 Engine 生命周期；浏览器配置会以短期内存租约生效并自动续期。'
              : engineConnection === 'model_configuration_required'
                ? 'Kimi Agent Engine 已由后端启动，但尚无可用模型。可在下方保存个人模型配置，或使用管理员配置的后端默认模型。'
                : engineConnection === 'checking'
                  ? '正在检查后端 Kimi Agent Engine 与模型配置。'
                  : '后端 Kimi Agent Engine 服务不可用，请检查平台启动日志。'}
          </p>
          <section className="agent-model-settings" aria-label="模型服务设置">
            <h3>模型服务</h3>
            <p className="model-relay-note">{selectedAgentAdapter?.browser_model_override_supported ? `${selectedAgentDisplayName} 支持浏览器模型覆盖。` : `${selectedAgentDisplayName} 使用后端或自身模型配置。`} API 密钥不会写入分析记录、工件或执行 Trace。</p>
            <div className="form-field"><label htmlFor="model-provider">模型服务预设</label><select id="model-provider" value={selectedProvider} onChange={(event) => updateConfig(applyProviderPreset(config, event.target.value as typeof selectedProvider))}>{modelProviderPresets.map((preset) => <option value={preset.id} key={preset.id}>{preset.label}</option>)}<option value="custom">自定义</option></select></div>
            <div className="form-field"><label htmlFor="model-base-url">模型服务地址</label><input id="model-base-url" value={config.baseUrl} onChange={(event) => updateConfig({ ...config, baseUrl: event.target.value })} onInput={(event) => updateConfig({ ...config, baseUrl: event.currentTarget.value })} placeholder="https://api.kimi.com/coding/v1 或 http://内网地址/v1" />{usesUnencryptedModelTransport(config.baseUrl) && <p className="model-relay-note warning">HTTP 可用于可信本机或内网服务；后端到模型服务的密钥与请求内容不会被 TLS 加密。</p>}</div>
            <div className="form-field"><label htmlFor="model-name">模型名称</label><input id="model-name" value={config.model} onChange={(event) => updateConfig({ ...config, model: event.target.value })} onInput={(event) => updateConfig({ ...config, model: event.currentTarget.value })} placeholder="kimi-for-coding" /></div>
            <div className="form-field"><label htmlFor="model-context-window">上下文长度（Token）</label><input id="model-context-window" type="number" min={8192} max={1048576} step={8192} value={config.contextWindowTokens} onChange={(event) => updateConfig({ ...config, contextWindowTokens: Number(event.target.value) })} /></div>
            <div className="form-field key-field"><label htmlFor="model-key">API 密钥</label><input id="model-key" type={showKey ? 'text' : 'password'} value={config.apiKey} onChange={(event) => updateConfig({ ...config, apiKey: event.target.value })} onInput={(event) => updateConfig({ ...config, apiKey: event.currentTarget.value })} placeholder="sk-..." autoComplete="off" /><button className="field-icon" type="button" title={showKey ? '隐藏 API 密钥' : '显示 API 密钥'} aria-label={showKey ? '隐藏 API 密钥' : '显示 API 密钥'} onClick={() => setShowKey((visible) => !visible)}>{showKey ? <EyeOff size={15} /> : <Eye size={15} />}</button></div>
            <div className="settings-actions"><button className="quiet-button" type="button" onClick={() => void clearConfig()} disabled={modelConfigBusy !== null} title="清除当前浏览器配置并撤销后端短期凭证">{modelConfigBusy === 'clear' ? <LoaderCircle size={14} className="spin" /> : <Trash2 size={14} />}清除</button><button className="quiet-button" type="button" onClick={() => void probeConnection()} disabled={modelConfigBusy !== null || !config.apiKey.trim() || !config.baseUrl.trim() || !config.model.trim()} title="使用当前配置执行一次真实模型探针">{modelConfigBusy === 'probe' ? <LoaderCircle size={14} className="spin" /> : <Wifi size={14} />}探针</button><button className="primary-button compact" type="button" onClick={() => void saveConfig()} disabled={modelConfigBusy !== null}>{modelConfigBusy === 'save' ? <LoaderCircle size={14} className="spin" /> : <KeyRound size={14} />}保存</button></div>
          </section>
          <div className="form-field">
            <label htmlFor="agent-execution-policy">Agent 执行策略</label>
            <select id="agent-execution-policy" value={config.agentExecutionPolicy} onChange={(event) => updateExecutionPolicy(event.target.value)}>
              <option value="full_access">全授权（已注册工作流与远端设备 Shell）</option>
              <option value="confirm_device_execution">真机执行需要页面确认</option>
            </select>
            <small className="form-field-note">全授权模式下，{selectedAgentDisplayName} 可在后端工作区使用原生代码导航、文件编辑、Shell、编译和同机 HDC；页面操作与远程客户端设备通过平台工具通道。WebSearch 与 FetchURL 保持禁用。</small>
          </div>
          {isScenarioCapturePage ? <CaptureDevicePicker view={captureDeviceSelection} onCommand={dispatchCaptureDeviceCommand} compact /> : <section className="agent-device-bridge" aria-label="远程采集端">
            <div className="agent-device-bridge-heading"><span><MonitorSmartphone size={14} />远程采集端</span><div><button className="icon-button compact" type="button" title="刷新远程采集端" aria-label="刷新远程采集端" disabled={deviceBridgeBusy !== null} onClick={() => void refreshDeviceBridges()}>{deviceBridgeBusy === 'refresh' ? <LoaderCircle size={13} className="spin" /> : <RefreshCw size={13} />}</button></div></div>
            <select value={selectedDeviceBridgeId} onChange={(event) => { setSelectedDeviceBridgeId(event.target.value); setDeviceBridgeNotice(event.target.value ? '其他页面的助手使用当前远程设备；负载采样跟随页面所选设备。' : '其他页面的助手使用当前本机设备；负载采样跟随页面所选设备。') }} aria-label="选择远程采集端">
              <option value="">后端同机 HDC</option>
              {deviceBridges.map((bridge) => <option key={bridge.bridge_id} value={bridge.bridge_id}>{remoteCaptureEndpointLabel(bridge.display_name || bridge.bridge_id)} · {bridge.connection_state === 'connected' ? '已连接' : '离线'}</option>)}
            </select>
            <small className="agent-device-bridge-note">负载采样以页面所选设备为准；可用远程设备会自动显示在列表中。</small>
            {selectedBridgeHealth && <small className={`agent-device-bridge-note ${selectedBridgeDiagnostics?.status === 'failed' ? 'is-error' : selectedBridgeDiagnostics?.status === 'ready' ? 'is-ready' : 'is-warning'}`}>{selectedBridgeHealth}</small>}
            {deviceBridgeNotice && <small className="agent-device-bridge-note">{deviceBridgeNotice}</small>}
          </section>}
          {connectionState !== 'idle' && <span className={`settings-status ${connectionState}`}>{connectionState === 'ok' ? '连接可用' : '连接失败'}</span>}
        </section>
      )}

      <section className="agent-token-budget" aria-label="模型 Token 用量">
        <div className="agent-token-heading"><span>当前页面 Session Token</span><small>{tokenUsageStatus}</small></div>
        <div className="agent-token-values">
          <div><span>总消耗</span><strong>{tokenUsageAvailable ? formatTokenCount(totalConsumedTokens) : '—'}</strong></div>
          <div title={tokenUsageAvailable ? `Input ${totalInputTokens} / Output ${totalOutputTokens}` : '当前 Agent Adapter 没有提供可汇总的 Token 用量。'}><span>Input / Output</span><strong>{tokenUsageAvailable ? `${formatTokenCount(totalInputTokens)} / ${formatTokenCount(totalOutputTokens)}` : '— / —'}</strong></div>
          <div title="缓存命中率 = 模型服务返回的 Cache Read Token / Input Token"><span>缓存命中率</span><strong>{!tokenUsageAvailable ? 'Agent 未提供' : cumulativeCacheHitRate === null ? '服务未返回' : `${(cumulativeCacheHitRate * 100).toFixed(1)}%`}</strong></div>
        </div>
      </section>
      {strategyProgress && <StrategyProgressPanel progress={strategyProgress} elapsedSeconds={strategyProgressElapsedSeconds} expanded={strategyProgressExpanded} onToggle={() => setStrategyProgressExpanded((expanded) => !expanded)} />}
      <section className={`agent-cli ${dragActive ? 'is-drag-active' : ''}`} aria-label={selectedAgentDisplayName} onDragEnter={(event) => { event.preventDefault(); setDragActive(true) }} onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; setDragActive(true) }} onDragLeave={(event) => { if (event.currentTarget === event.target) setDragActive(false) }} onDrop={handleComposerDrop}>
        <header className="agent-cli-statusbar">
          <div><span className={`agent-cli-state ${activeRunState ?? 'idle'}`} /> <strong>{selectedAgentDisplayName} · PTY</strong><code>session:{activeSessionLabel}</code></div>
          <div className="agent-cli-status-actions"><button className="icon-button compact" type="button" title={`重绘终端画面，不重启当前 ${selectedAgentDisplayName} Session`} aria-label={`重绘 ${selectedAgentDisplayName} 终端`} disabled={!webShellTerminal || webShellTerminal.state !== 'running'} onClick={() => void webShellRef.current?.redraw()}><RefreshCw size={12} /></button><span className={`agent-cli-transport is-${webShellTransport.state}`} title={transportStatusTitle}>{transportStatusLabel}</span></div>
        </header>
        <div className={`agent-webshell-status is-${webShellTransport.state}`}><span>{!agentCliConnected && !webShellTerminal ? agentCliBlockedDetail : visibleWebShellDetail}</span><div><input ref={traceInputRef} className="visually-hidden" type="file" multiple onChange={handleTraceInput} /><button className="icon-button compact" type="button" title={`把 Trace、源码或文本附件交给当前 ${selectedAgentDisplayName} Session`} aria-label={`附加文件到 ${selectedAgentDisplayName}`} disabled={attachmentBusy} onClick={() => traceInputRef.current?.click()}>{attachmentBusy ? <LoaderCircle size={13} className="spin" /> : <Paperclip size={13} />}</button>{webShellTerminal?.pid && <code>pid:{webShellTerminal.pid}</code>}</div></div>
        <AgentWebShell
          ref={webShellRef}
          connected={agentCliConnected}
          adapterId={selectedAgentAdapterId}
          adapterDisplayName={selectedAgentDisplayName}
          waitingDetail={agentCliConnected ? webShellDetail : agentCliBlockedDetail}
          config={config}
          analysisId={currentAnalysisId || undefined}
          runtimeContext={pageAgentRuntimeContext(currentAnalysisId)}
          onSessionReady={handleWebShellSessionReady}
          onStateChange={handleWebShellStateChange}
          onTransportChange={setWebShellTransport}
          onTokenUsage={setWebShellTokenUsage}
          onCommandSubmitted={handleWebShellCommandSubmitted}
        />
        {dragActive && <div className="kimi-webshell-drop-overlay"><Upload size={18} /><strong>松开以交给当前 {selectedAgentDisplayName} Session</strong><span>浏览器保留附件，Agent 通过页面 MCP 按需导入</span></div>}
      </section>
    </aside>
  )
}
