import { api, apiBase, ApiError } from './api'
import { randomUuid } from './random-id'

export type AnalysisTraceCategory =
  | 'workflow'
  | 'model_request'
  | 'model_response'
  | 'model_error'
  | 'tool_call'
  | 'tool_result'
  | 'page_action'
  | 'contract'
  | 'progress'

export type AnalysisTraceEventInput = {
  category: AnalysisTraceCategory
  operation: string
  phase?: string
  status?: string
  requestId?: string
  runId?: string
  turn?: number
  payload?: Record<string, unknown>
}

export type AnalysisExecutionTraceEvent = {
  schema_version: 'analysis_execution_trace_event/v1'
  analysis_id: string
  event_id: string
  occurred_at: string
  received_at: string
  category: AnalysisTraceCategory
  operation: string
  phase: string
  status: string
  request_id: string
  run_id: string
  turn: number | null
  client_sequence: number | null
  payload: Record<string, unknown>
  payload_sha256: string
}

export type AnalysisExecutionTrace = {
  analysis_id: string
  trace_id: string
  relative_path: string
  event_count: number
  returned_event_count?: number
  file_bytes?: number
  events: AnalysisExecutionTraceEvent[]
}

export type AnalysisModelToolCall = {
  tool_call_id: string
  name: string
  status: 'running' | 'succeeded' | 'failed' | string
  started_at: string
  ended_at: string
  arguments: Record<string, unknown>
  result: unknown
}

export type AnalysisModelStep = {
  step_id: string
  agent_id: string
  turn_id: string
  step: number
  started_at: string
  ended_at: string
  status: string
  finish_reason: string
  input: string
  reasoning: string
  output: string
  model_request: Record<string, unknown>
  usage: Record<string, unknown>
  latency: Record<string, unknown>
  tools: AnalysisModelToolCall[]
}

export type AnalysisVisualEvent = {
  event_id: string
  occurred_at: string
  source: 'browser' | 'agent_run' | string
  kind: string
  operation: string
  phase: string
  status: string
  request_id: string
  run_id: string
  payload: Record<string, unknown>
}

export type AnalysisExecutionView = {
  schema_version: 'analysis_execution_view/v1'
  analysis_id: string
  generated_at: string
  coverage: {
    workspace_snapshot: boolean
    browser_events: boolean
    agent_run: boolean
    native_model_transcript: boolean
    native_model_source: string
  }
  workspace: Record<string, unknown> | null
  agent_session: {
    bound: boolean
    run: Record<string, unknown> | null
    run_id: string | null
    adapter_id: string | null
    native_session_id: string | null
  }
  summary: {
    started_at: string
    ended_at: string
    duration_ms: number | null
    record_span_ms: number | null
    model_step_count: number
    tool_call_count: number
    tool_success_count: number
    tool_failure_count: number
    browser_event_count: number
    agent_event_count: number
    input_tokens: number
    output_tokens: number
    cached_input_tokens: number
    cache_hit_rate: number | null
  }
  model_transcript: {
    available: boolean
    source: string
    system_prompt: string
    turn_prompt: string
    steps: AnalysisModelStep[]
    wire_files: string[]
    row_count?: number
    truncated?: boolean
  }
  events: AnalysisVisualEvent[]
  raw_sources: Record<string, unknown>
}

const sequenceByAnalysis = new Map<string, number>()

function nextSequence(analysisId: string): number {
  const next = (sequenceByAnalysis.get(analysisId) ?? 0) + 1
  sequenceByAnalysis.set(analysisId, next)
  return next
}

function eventId(): string {
  return randomUuid()
}

export function analysisErrorPayload(error: unknown): Record<string, unknown> {
  if (error instanceof ApiError) {
    return {
      name: error.name,
      message: error.message,
      code: error.code,
      status_code: error.statusCode,
      details: error.details,
    }
  }
  if (error instanceof Error) return { name: error.name, message: error.message }
  return { message: String(error) }
}

/**
 * Observability is best-effort and must never become a new failure mode for
 * analysis. The backend recursively redacts credential-shaped field names.
 */
export async function appendAnalysisTraceEvent(
  analysisId: string | null | undefined,
  input: AnalysisTraceEventInput,
): Promise<void> {
  if (!analysisId?.trim()) return
  await api(`/v1/projects/local-default/analysis-traces/${encodeURIComponent(analysisId)}/events`, {
    method: 'POST',
    body: JSON.stringify({
      event_id: eventId(),
      occurred_at: new Date().toISOString(),
      category: input.category,
      operation: input.operation,
      phase: input.phase ?? '',
      status: input.status ?? 'observed',
      request_id: input.requestId ?? '',
      run_id: input.runId ?? '',
      turn: input.turn,
      client_sequence: nextSequence(analysisId),
      payload: input.payload ?? {},
    }),
  }).catch(() => undefined)
}

export function analysisTraceDownloadUrl(analysisId: string): string {
  return `${apiBase}/v1/projects/local-default/analysis-traces/${encodeURIComponent(analysisId)}/download`
}
