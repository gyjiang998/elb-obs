import { ApiError, api, apiBase } from './api'
import type { BackendAgentRun } from './backend-agent-runtime'
import type { BrowserModelConfig } from './model-config'

export type AgentCliAdapter = {
  schema_version: 'agent_cli_adapter/v1'
  adapter_id: string
  display_name: string
  description: string
  available: boolean
  diagnostic: string | null
  session_strategy: 'native_session' | 'workspace_continue' | 'stateless'
  submit_mode: 'bracketed_paste_cr' | 'bracketed_paste_lf' | 'plain_text_cr' | 'plain_text_lf'
  startup_input_delay_ms: number
  submit_delay_ms: number
  submit_fallback_ms: number
  capabilities: Record<string, boolean>
  mcp_integration: {
    declared: boolean
    configured: boolean
    format: 'none' | 'kimi' | 'opencode' | 'neutral' | string
    path: string | null
    delivery: 'argv_and_environment' | 'environment_and_workspace' | string
    warning: string | null
  }
  browser_model_override_supported: boolean
  usage_kind: 'unavailable' | 'kimi_wire' | 'standard_jsonl' | string
  source: string
  default: boolean
}

export type AgentCliAdapterCatalog = {
  schema_version: 'agent_cli_adapter_catalog/v1'
  default_adapter_id: string
  adapters: AgentCliAdapter[]
  manifest_error: {
    code: string
    message: string
    path: string
  } | null
}

export type AgentCliTokenUsage = {
  schema_version: string
  input_tokens: number
  output_tokens: number
  total_tokens: number
  cached_input_tokens: number
  cache_creation_input_tokens: number
  uncached_input_tokens: number
  cache_hit_rate: number | null
  model_calls: number
  source: string
  updated_at: string
}

/** PTY delivery diagnostics; these do not confirm that the Agent started work. */
export type AgentCliSubmissionTransport = {
  schema_version: 'agent_cli_submission_transport/v1'
  command_id: string
  strategy_version: 'pty_output_settle/v1' | 'plain_delay/v1'
  delivery_state: 'writing_content' | 'waiting_before_submit' | 'delivered' | 'failed'
  actual_wait_ms: number
  wait_reason: 'not_started' | 'waiting' | 'session_exited' | 'maximum_wait_reached' | 'output_settled' | 'no_output_wait_elapsed' | 'configured_delay_elapsed'
  output_observed: boolean
  submit_key: 'cr' | 'lf'
  submit_key_count: number
}

export type AgentCliTerminal = {
  schema_version: 'agent_cli_terminal/v1' | 'kimi_cli_terminal/v1'
  terminal_session_id: string
  run_id: string
  adapter_id: string
  agent_display_name: string
  agent_session_id: string
  agent_workspace_id: string
  kimi_session_id?: string
  state: 'running' | 'exited'
  pid: number | null
  exit_code: number | null
  cols: number
  rows: number
  created_at: string
  last_activity_at: string
  earliest_sequence: number
  latest_sequence: number
  transport: 'pty'
  idle_timeout_seconds?: null
  lifetime_policy?: 'until_explicit_stop_or_backend_shutdown'
  submit_mode?: AgentCliAdapter['submit_mode']
  startup_input_delay_ms?: number
  submit_delay_ms?: number
  effective_submit_delay_ms?: number
  submit_fallback_ms?: number
  last_submission?: AgentCliSubmissionTransport | null
  token_usage: AgentCliTokenUsage
}

export type AgentCliSessionResponse = {
  terminal: AgentCliTerminal
  run: BackendAgentRun
  recovery?: {
    status: 'durable_run_recovered' | 'fresh_session_started' | string
    reason: string
    failed_terminal_session_id?: string
  } | null
}

export type AgentCliOutputChunk = {
  sequence: number
  data_base64: string
  byte_count: number
  occurred_at: string
}

export type AgentCliOutputPollResponse = {
  schema_version: 'agent_cli_output_poll/v1' | 'kimi_cli_output_poll/v1'
  chunks: AgentCliOutputChunk[]
  truncated: boolean
  more_available: boolean
  terminal: AgentCliTerminal
}

function credential(config: BrowserModelConfig) {
  if (!config.apiKey.trim()) return undefined
  return {
    base_url: config.baseUrl,
    api_key: config.apiKey,
    model: config.model,
    context_window_tokens: config.contextWindowTokens,
    ttl_seconds: 30 * 60,
  }
}

function boundedTerminalDimension(value: number, fallback: number, minimum: number, maximum: number): number {
  const normalized = Number.isFinite(value) ? Math.trunc(value) : fallback
  return Math.max(minimum, Math.min(maximum, normalized || fallback))
}

async function withRequestTimeout<T>(input: {
  timeoutMs: number
  code: string
  message: string
  signal?: AbortSignal
  request: (signal: AbortSignal) => Promise<T>
}): Promise<T> {
  const controller = new AbortController()
  let timedOut = false
  const abortFromCaller = () => controller.abort()
  if (input.signal?.aborted) controller.abort()
  else input.signal?.addEventListener('abort', abortFromCaller, { once: true })
  const timer = setTimeout(() => {
    timedOut = true
    controller.abort()
  }, input.timeoutMs)
  try {
    return await input.request(controller.signal)
  } catch (error) {
    if (timedOut) {
      throw new ApiError(input.message, input.code, { timeout_ms: input.timeoutMs }, 504)
    }
    throw error
  } finally {
    clearTimeout(timer)
    input.signal?.removeEventListener('abort', abortFromCaller)
  }
}

export async function listAgentCliAdapters(): Promise<AgentCliAdapterCatalog> {
  return withRequestTimeout({
    timeoutMs: 8_000,
    code: 'AGENT_CLI_ADAPTER_CATALOG_TIMEOUT',
    message: '读取 Agent Adapter 目录超过 8 秒，内部 Web/API 代理可能尚未就绪。',
    request: (signal) => api<AgentCliAdapterCatalog>('/v1/agent-cli/adapters', { cache: 'no-store', signal }),
  })
}

export async function createAgentCliSession(input: {
  adapterId: string
  runId?: string
  adoptPageSession?: boolean
  pageSessionId: string
  analysisId?: string
  goal: string
  mode?: 'page_agent' | 'device_validation'
  context: Record<string, unknown>
  config: BrowserModelConfig
  cols: number
  rows: number
}): Promise<AgentCliSessionResponse> {
  const cols = boundedTerminalDimension(input.cols, 100, 40, 320)
  const rows = boundedTerminalDimension(input.rows, 32, 12, 160)
  return withRequestTimeout({
    timeoutMs: 90_000,
    code: 'AGENT_CLI_SESSION_CREATE_TIMEOUT',
    message: '后端创建 Agent CLI PTY 超过 90 秒，连接将自动重试。',
    request: (signal) => api<AgentCliSessionResponse>('/v1/agent-cli/sessions', {
      method: 'POST',
      signal,
      body: JSON.stringify({
        adapter_id: input.adapterId,
        run_id: input.runId || undefined,
        adopt_page_session: input.adoptPageSession === true,
        page_session_id: input.pageSessionId,
        analysis_id: input.analysisId || undefined,
        goal: input.goal,
        mode: input.mode ?? 'page_agent',
        context: input.context,
        credential: credential(input.config),
        cols,
        rows,
        budget: {
          max_turns: input.config.agentMaxTurns,
          max_actions_per_turn: input.config.agentMaxActionsPerTurn,
          max_output_tokens: input.config.agentMaxOutputTokens,
          context_window_tokens: input.config.contextWindowTokens,
        },
      }),
    }),
  })
}

export async function getAgentCliSession(sessionId: string, pageSessionId: string): Promise<AgentCliSessionResponse> {
  return withRequestTimeout({
    timeoutMs: 15_000,
    code: 'AGENT_CLI_SESSION_RESTORE_TIMEOUT',
    message: '恢复已保存的 Agent CLI PTY 超过 15 秒，连接将自动重试。',
    request: (signal) => api<AgentCliSessionResponse>(
      `/v1/agent-cli/sessions/${encodeURIComponent(sessionId)}?page_session_id=${encodeURIComponent(pageSessionId)}`,
      { cache: 'no-store', signal },
    ),
  })
}

export async function getAgentCliSessionUsage(sessionId: string, pageSessionId: string): Promise<AgentCliTokenUsage> {
  const response = await api<{ token_usage: AgentCliTokenUsage }>(`/v1/agent-cli/sessions/${encodeURIComponent(sessionId)}/usage?page_session_id=${encodeURIComponent(pageSessionId)}`)
  return response.token_usage
}

export async function pollAgentCliOutput(
  sessionId: string,
  afterSequence: number,
  signal: AbortSignal,
  waitMs = 0,
): Promise<AgentCliOutputPollResponse> {
  return withRequestTimeout({
    timeoutMs: Math.max(10_000, waitMs + 5_000),
    code: 'AGENT_CLI_OUTPUT_POLL_TIMEOUT',
    message: 'Agent CLI PTY 输出连接超时，页面将从最后输出序号继续恢复。',
    signal,
    request: (requestSignal) => api<AgentCliOutputPollResponse>(
      `/v1/agent-cli/sessions/${encodeURIComponent(sessionId)}/output?after=${afterSequence}&wait_ms=${waitMs}`,
      { signal: requestSignal, cache: 'no-store' },
    ),
  })
}

export async function writeAgentCliInput(sessionId: string, data: Uint8Array): Promise<void> {
  let binary = ''
  for (let index = 0; index < data.length; index += 1) binary += String.fromCharCode(data[index])
  await api(`/v1/agent-cli/sessions/${encodeURIComponent(sessionId)}/input`, {
    method: 'POST',
    body: JSON.stringify({ data_base64: btoa(binary) }),
  })
}

export async function sendAgentCliCommand(sessionId: string, command: string, commandId: string): Promise<void> {
  await api(`/v1/agent-cli/sessions/${encodeURIComponent(sessionId)}/commands`, {
    method: 'POST',
    body: JSON.stringify({ command, command_id: commandId }),
  })
}

export async function updateAgentCliContext(sessionId: string, input: {
  goal: string
  pageSessionId: string
  mode?: 'page_agent' | 'device_validation'
  context: Record<string, unknown>
  analysisId?: string
}): Promise<AgentCliSessionResponse> {
  return api<AgentCliSessionResponse>(`/v1/agent-cli/sessions/${encodeURIComponent(sessionId)}/context`, {
    method: 'POST',
    body: JSON.stringify({
      goal: input.goal,
      page_session_id: input.pageSessionId,
      mode: input.mode ?? 'page_agent',
      context: input.context,
      analysis_id: input.analysisId || undefined,
    }),
  })
}

export async function resizeAgentCliSession(sessionId: string, cols: number, rows: number): Promise<void> {
  const nextCols = boundedTerminalDimension(cols, 100, 40, 320)
  const nextRows = boundedTerminalDimension(rows, 32, 12, 160)
  await api(`/v1/agent-cli/sessions/${encodeURIComponent(sessionId)}/resize`, {
    method: 'POST',
    body: JSON.stringify({ cols: nextCols, rows: nextRows }),
  })
}

export async function redrawAgentCliSession(sessionId: string, cols: number, rows: number): Promise<void> {
  // Ink/TUI applications commonly ignore SIGWINCH when the kernel window size
  // did not actually change. Pulse one row, then restore the fitted size so
  // Kimi emits a fresh screen that a reconnecting browser can replay.
  const nextCols = boundedTerminalDimension(cols, 100, 40, 320)
  const nextRows = boundedTerminalDimension(rows, 32, 12, 160)
  const pulseRows = nextRows > 12 ? nextRows - 1 : nextRows + 1
  await resizeAgentCliSession(sessionId, nextCols, pulseRows)
  await new Promise((resolve) => window.setTimeout(resolve, 40))
  await resizeAgentCliSession(sessionId, nextCols, nextRows)
}

export async function stopAgentCliSession(sessionId: string): Promise<void> {
  await api(`/v1/agent-cli/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' })
}

export function agentCliStreamUrl(sessionId: string, afterSequence: number): string {
  if (apiBase) {
    return `${apiBase}/v1/agent-cli/sessions/${encodeURIComponent(sessionId)}/events?after=${afterSequence}`
  }
  return `/agent-cli-stream/${encodeURIComponent(sessionId)}?after=${afterSequence}`
}

export function decodeTerminalChunk(dataBase64: string): Uint8Array {
  const binary = atob(dataBase64)
  const data = new Uint8Array(binary.length)
  for (let index = 0; index < binary.length; index += 1) data[index] = binary.charCodeAt(index)
  return data
}

// Source-level compatibility aliases for extensions compiled against v1.
export type KimiCliTokenUsage = AgentCliTokenUsage
export type KimiCliTerminal = AgentCliTerminal
export type KimiCliSessionResponse = AgentCliSessionResponse
export type KimiCliOutputChunk = AgentCliOutputChunk
export type KimiCliOutputPollResponse = AgentCliOutputPollResponse
