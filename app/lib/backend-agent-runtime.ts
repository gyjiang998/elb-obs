import { api } from './api'
import type { BrowserModelConfig } from './model-config'
import { randomUuid } from './random-id'

export type BackendAgentRunState =
  | 'created'
  | 'waiting_for_model_credential'
  | 'running'
  | 'waiting_for_tool'
  | 'waiting_for_ui'
  | 'waiting_for_device'
  | 'compacting'
  | 'paused_model_overloaded'
  | 'paused_output_limit'
  | 'paused_turn_budget'
  | 'paused_bridge_offline'
  | 'completed'
  | 'failed'
  | 'cancelled'

export type BackendSkillBootstrap = {
  schema_version?: string
  bootstrap_id?: string
  status?: 'pending' | 'acknowledged' | string
  required_at?: string
  acknowledged_at?: string | null
  profile_id?: string
  adapter_id?: string
  agent_session_id?: string
  inventory_count?: number
  orchestrator?: {
    skill_id?: string
    version?: string
    content_sha256?: string
    source?: string
  }
  target_skills?: Record<string, {
    projection_status?: string
    projected_source?: string | null
    projected_sha256?: string | null
  }>
  acknowledgement?: {
    skill_discovery?: Array<{
      skill_id?: string
      status?: 'loaded' | 'available' | 'not_found' | 'load_failed' | string
      source?: string | null
      detail?: string | null
    }>
  }
}

export type BackendAgentRun = {
  run_id: string
  state: BackendAgentRunState
  state_detail: string
  current_turn: number
  event_cursor?: number
  budget: {
    max_turns: number
    max_actions_per_turn: number
    max_output_tokens: number
    context_window_tokens: number
  }
  pending_ui_actions: Array<{
    request_id: string
    action_id: string
    input: Record<string, unknown>
    action_key?: string
    turn?: number
  }>
  checkpoint?: Record<string, unknown>
  analysis_workflow?: {
    active?: boolean
    ready?: boolean
    stage?: string
    required_action?: string | null
    required_strategy_id?: string | null
  }
  skill_bootstrap?: BackendSkillBootstrap
  engine?: {
    engine_id?: string
    engine_version?: string
    workspace_id?: string
    session_id?: string
    prompt_id?: string
    status?: string
    usage?: Record<string, unknown>
  }
}

export type BackendAgentRunEvent = {
  event_id: string
  sequence: number
  run_id: string
  occurred_at: string
  event_type: string
  state: string
  payload: Record<string, unknown>
}

/**
 * Wire format of the backend-only BYOK lease request.  Keep this separate
 * from BrowserModelConfig: browser state intentionally uses camelCase while
 * FastAPI's durable-Run contract uses snake_case.
 */
export type BackendAgentCredential = {
  base_url: string
  api_key: string
  model: string
  context_window_tokens: number
  ttl_seconds?: number
}

export type BackendAgentEngineState = {
  status?: 'ready' | 'model_configuration_required' | 'unavailable' | string
  service_status?: 'ready' | 'unavailable' | string
  model_status?: 'ready' | 'unconfigured' | 'unavailable' | 'unknown' | string
  engine_id?: string
  model?: string
  provider_type?: 'kimi' | 'openai' | string
  model_probe?: 'passed' | 'retained' | string
  credential_mode?: 'browser_ephemeral' | 'kimi_managed' | 'none' | string
  expires_in_seconds?: number
  configuration_reloaded?: boolean
  browser_credential_applied?: boolean
  error?: { code?: string; message?: string; details?: Record<string, unknown> }
}

export type BackendAgentEngineResponse = {
  selected_engine: string
  engine: BackendAgentEngineState
}

export type BackendAgentBudget = {
  max_turns: number
  max_actions_per_turn: number
  max_output_tokens: number
  context_window_tokens: number
}

/** Public, non-secret view of a registered outbound HDC Device Bridge. */
export type BackendDeviceBridge = {
  bridge_id: string
  display_name: string
  connection_state: 'connected' | 'offline' | string
  registration_mode?: 'trusted_single_user' | 'pairing_code' | string
  created_at?: string
  last_seen_at?: string | null
  heartbeat_age_seconds?: number
  capabilities?: {
    protocol_version?: string
    implementation_version?: string
    deployment_contract_version?: string
    actions?: string[]
    platform?: string
    hdc_path_configured?: boolean
    runtime_diagnostics?: {
      schema_version?: string
      checked_at?: string
      status?: 'ready' | 'attention' | 'failed' | string
      hdc?: {
        configured_path_exists?: boolean
        executable_name?: string
        configured_parent_hint?: string
        target_count?: number
        version?: { return_code?: number | null; stdout?: string; stderr?: string }
      }
      spool?: { writable?: boolean; path_hint?: string; error?: string | null }
      issues?: Array<{ code?: string; failure_domain?: string; message_zh?: string }>
    }
  }
}

export function runtimeIdempotencyKey(scope: string): string {
  const random = randomUuid()
  return `${scope}:${random}`.replace(/[^A-Za-z0-9._:-]/g, '').slice(0, 180)
}

function credential(config: BrowserModelConfig): BackendAgentCredential {
  return {
    base_url: config.baseUrl,
    api_key: config.apiKey,
    model: config.model,
    context_window_tokens: config.contextWindowTokens,
    ttl_seconds: 30 * 60,
  }
}

function optionalCredential(config: BrowserModelConfig): BackendAgentCredential | undefined {
  return config.apiKey.trim() && config.baseUrl.trim() && config.model.trim()
    ? credential(config)
    : undefined
}

export async function getBackendAgentEngineHealth(): Promise<BackendAgentEngineResponse> {
  return api('/v1/agent-engine/health')
}

export async function configureBackendAgentEngine(config: BrowserModelConfig): Promise<BackendAgentEngineResponse> {
  return api('/v1/agent-engine/model-credential', {
    method: 'POST',
    body: JSON.stringify(credential(config)),
  })
}

export async function clearBackendAgentEngineCredential(): Promise<BackendAgentEngineResponse> {
  return api('/v1/agent-engine/model-credential', { method: 'DELETE' })
}

async function write<T>(path: string, body: Record<string, unknown>, scope: string): Promise<T> {
  return api<T>(path, {
    method: 'POST',
    headers: { 'Idempotency-Key': runtimeIdempotencyKey(scope) },
    body: JSON.stringify(body),
  })
}

export async function createBackendAgentRun(input: {
  goal: string
  initialInput: string
  mode: 'page_agent' | 'device_validation'
  context: Record<string, unknown>
  analysisId?: string
  /** Legacy engine compatibility only. Kimi Runs are controlled by Stop. */
  budget?: BackendAgentBudget
  config: BrowserModelConfig
}): Promise<BackendAgentRun> {
  const modelCredential = optionalCredential(input.config)
  const response = await write<{ run: BackendAgentRun }>('/v1/agent-runs', {
    goal: input.goal,
    initial_input: input.initialInput,
    mode: input.mode,
    context: input.context,
    ...(input.analysisId ? { analysis_id: input.analysisId } : {}),
    ...(input.budget ? { budget: input.budget } : {}),
    ...(modelCredential ? { credential: modelCredential } : {}),
  }, 'agent-run-create')
  return response.run
}

export async function submitBackendAgentMessage(
  runId: string,
  message: string,
  config: BrowserModelConfig,
  task?: { mode: 'page_agent' | 'device_validation'; goal: string; context: Record<string, unknown> },
): Promise<BackendAgentRun> {
  const modelCredential = optionalCredential(config)
  const response = await write<{ run: BackendAgentRun }>(`/v1/agent-runs/${encodeURIComponent(runId)}/messages`, {
    message,
    ...(task ? { mode: task.mode, goal: task.goal, context: task.context } : {}),
    ...(modelCredential ? { credential: modelCredential } : {}),
  }, `agent-run-message:${runId}`)
  return response.run
}

export async function getAnalysisAgentSession(analysisId: string): Promise<BackendAgentRun | null> {
  try {
    const response = await api<{ run: BackendAgentRun }>(`/v1/analysis-agent-sessions/${encodeURIComponent(analysisId)}`)
    return response.run
  } catch (error) {
    if (error instanceof Error && /尚未创建|not found/i.test(error.message)) return null
    return null
  }
}

export async function bindBackendAgentRunToAnalysis(runId: string, analysisId: string): Promise<BackendAgentRun> {
  const response = await write<{ run: BackendAgentRun }>(`/v1/agent-runs/${encodeURIComponent(runId)}/analysis-binding`, {
    analysis_id: analysisId,
  }, `agent-run-analysis-bind:${runId}:${analysisId}`)
  return response.run
}

export async function resumeBackendAgentRun(runId: string, config: BrowserModelConfig): Promise<BackendAgentRun> {
  const modelCredential = optionalCredential(config)
  const response = await write<{ run: BackendAgentRun }>(`/v1/agent-runs/${encodeURIComponent(runId)}/resume`, {
    additional_turns: 0,
    ...(modelCredential ? { credential: modelCredential } : {}),
  }, `agent-run-resume:${runId}`)
  return response.run
}

export async function cancelBackendAgentRun(runId: string): Promise<BackendAgentRun> {
  const response = await write<{ run: BackendAgentRun }>(`/v1/agent-runs/${encodeURIComponent(runId)}/cancel`, {}, `agent-run-cancel:${runId}`)
  return response.run
}

export async function submitBackendUiActionResult(input: {
  runId: string
  requestId: string
  result?: Record<string, unknown>
  error?: Record<string, unknown>
  /** Kept for call-site compatibility; UI observations never reconfigure the model. */
  config: BrowserModelConfig
}): Promise<BackendAgentRun> {
  const response = await write<{ run: BackendAgentRun }>(`/v1/agent-runs/${encodeURIComponent(input.runId)}/ui-action-results`, {
    request_id: input.requestId,
    result: input.result ?? {},
    error: input.error,
  }, `agent-run-ui:${input.runId}:${input.requestId}`)
  return response.run
}

export async function getBackendAgentRun(runId: string): Promise<BackendAgentRun> {
  const response = await api<{ run: BackendAgentRun }>(`/v1/agent-runs/${encodeURIComponent(runId)}`)
  return response.run
}

export async function listBackendDeviceBridges(): Promise<BackendDeviceBridge[]> {
  const response = await api<{ bridges: BackendDeviceBridge[] }>('/v1/device-bridges')
  return Array.isArray(response.bridges) ? response.bridges : []
}

export function subscribeBackendAgentRun(
  runId: string,
  afterSequence: number,
  onEvent: (event: BackendAgentRunEvent) => void,
  onTransportState?: (state: 'connected' | 'reconnecting') => void,
): () => void {
  let closed = false
  let lastSequence = Math.max(0, afterSequence)
  let controller: AbortController | null = null

  const waitBeforeRetry = (delayMs: number) => new Promise<void>((resolve) => {
    window.setTimeout(resolve, delayMs)
  })

  const poll = async () => {
    let consecutiveFailures = 0
    let connected = false
    while (!closed) {
      controller = new AbortController()
      const currentController = controller
      let timedOut = false
      const timer = window.setTimeout(() => {
        timedOut = true
        currentController.abort()
      }, 25_000)
      try {
        const response = await api<{
          events: BackendAgentRunEvent[]
          latest_sequence: number
          more_available: boolean
        }>(`/v1/agent-runs/${encodeURIComponent(runId)}/events/poll?after=${lastSequence}&wait_ms=0&limit=500`, {
          cache: 'no-store',
          signal: currentController.signal,
        })
        if (closed || controller !== currentController) return
        consecutiveFailures = 0
        if (!connected) {
          connected = true
          onTransportState?.('connected')
        }
        for (const event of Array.isArray(response.events) ? response.events : []) {
          if (!event || typeof event.sequence !== 'number' || event.sequence <= lastSequence) continue
          lastSequence = event.sequence
          onEvent(event)
        }
        if (!response.more_available) {
          const interval = response.events.length > 0
            ? 40
            : document.visibilityState === 'hidden' ? 1500 : 300
          await waitBeforeRetry(interval)
        }
      } catch {
        if (closed || (currentController.signal.aborted && !timedOut)) return
        consecutiveFailures += 1
        if (connected) {
          connected = false
          onTransportState?.('reconnecting')
        } else {
          onTransportState?.('reconnecting')
        }
        await waitBeforeRetry(Math.min(5_000, 500 * (2 ** Math.min(consecutiveFailures - 1, 4))))
      } finally {
        window.clearTimeout(timer)
        if (controller === currentController) controller = null
      }
    }
  }

  void poll()
  return () => {
    closed = true
    controller?.abort()
    controller = null
  }
}
