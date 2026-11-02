import { api, apiBase, ApiError, apiErrorFromPayload } from './api'
import { analysisErrorPayload, appendAnalysisTraceEvent } from './analysis-observability'
import type { BrowserModelConfig } from './model-config'
import { randomUuid } from './random-id'
import { estimateMessageTokens, estimateTextTokens, normalizeModelTokenUsage, type ModelTokenUsage, type TokenMessage } from './token-budget'

export type ModelChatMessage = TokenMessage
export type { ModelTokenUsage } from './token-budget'

export type ModelChatCompletion = {
  choices?: Array<{
    finish_reason?: string | null
    message?: { content?: string; reasoning_content?: string; reasoning?: string }
  }>
  usage?: unknown
  tokenUsage: ModelTokenUsage
  /**
   * OpenAI-compatible reasoning providers sometimes put their deliberation in
   * a side channel. It is intentionally not rendered as a user conclusion;
   * structured callers may use it only as a bounded recovery source when the
   * final content channel is empty or malformed.
   */
  reasoningContent?: string
  agentEngine?: {
    engineId?: string
    sessionId?: string
    runId?: string
    conversationId?: string
  }
}

export type ModelStreamProgress = {
  reasoningDelta: string
  contentDelta: string
  reasoningChars: number
  contentChars: number
}

export type ModelObservabilityContext = {
  analysisId: string
  operation: string
  requestId?: string
  runId?: string
  turn?: number
}

export type ModelRetryProgress = {
  failedAttempt: number
  nextAttempt: number
  maxAttempts: number
  delayMs: number
  error: Error
}

type ModelRequestOptions = {
  maxTokens?: number
  outputBudgetPolicy?: 'configured' | 'task_specific'
  executionMode?: 'agent_engine' | 'direct_relay'
  operation?: string
  signal?: AbortSignal
  onProgress?: (progress: ModelStreamProgress) => void
  onRetry?: (progress: ModelRetryProgress) => void
  retryMaxAttempts?: number
  observability?: ModelObservabilityContext
}

type ModelRetryAttempt = {
  attempt: number
  maxAttempts: number
}

const DEFAULT_MODEL_RETRY_ATTEMPTS = 3
const MAX_MODEL_RETRY_ATTEMPTS = 5
const RETRYABLE_HTTP_STATUS = new Set([408, 425, 429, 500, 502, 503, 504])
const RETRYABLE_MODEL_ERROR_CODES = new Set([
  'MODEL_RELAY_UPSTREAM_TIMEOUT',
  'MODEL_RELAY_UPSTREAM_UNAVAILABLE',
  'MODEL_RELAY_STREAM_INCOMPLETE',
  'MODEL_RELAY_STREAM_EMPTY',
  'KIMI_PROMPT_IDLE_TIMEOUT',
])

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function agentEngineObservation(value: unknown): ModelChatCompletion['agentEngine'] | undefined {
  const item = asRecord(value)
  const engineId = stringValue(item.engine_id)
  const sessionId = stringValue(item.session_id)
  const runId = stringValue(item.run_id)
  const conversationId = stringValue(item.conversation_id)
  if (!engineId && !sessionId && !runId && !conversationId) return undefined
  return {
    ...(engineId ? { engineId } : {}),
    ...(sessionId ? { sessionId } : {}),
    ...(runId ? { runId } : {}),
    ...(conversationId ? { conversationId } : {}),
  }
}

function finiteInteger(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? Math.round(value) : null
}

export function isRetryableModelError(error: unknown): boolean {
  if (error instanceof ApiError) {
    if (error.code === 'MODEL_OUTPUT_TOKEN_LIMIT' || error.code === 'MODEL_RELAY_RESPONSE_TOO_LARGE') return false
    const upstreamStatus = finiteInteger(error.details.upstream_status)
    if (error.statusCode !== null && RETRYABLE_HTTP_STATUS.has(error.statusCode)) return true
    if (upstreamStatus !== null && RETRYABLE_HTTP_STATUS.has(upstreamStatus)) return true
    return RETRYABLE_MODEL_ERROR_CODES.has(error.code)
  }
  return error instanceof TypeError && /fetch|network|load failed/i.test(error.message)
}

function retryDelayMs(error: unknown, failedAttempt: number): number {
  if (error instanceof ApiError) {
    const retryAfterSeconds = finiteInteger(error.details.retry_after_seconds)
    if (retryAfterSeconds !== null && retryAfterSeconds >= 0) {
      return Math.min(30_000, retryAfterSeconds * 1000)
    }
  }
  return Math.min(8_000, 1_500 * (2 ** Math.max(0, failedAttempt - 1)))
}

function normalizedRetryAttempts(options: ModelRequestOptions): number {
  const requested = finiteInteger(options.retryMaxAttempts)
  if (requested === null) return DEFAULT_MODEL_RETRY_ATTEMPTS
  return Math.max(1, Math.min(MAX_MODEL_RETRY_ATTEMPTS, requested))
}

async function requestWithRetry<T>(
  execute: (attempt: ModelRetryAttempt) => Promise<T>,
  options: ModelRequestOptions,
): Promise<T> {
  const maxAttempts = normalizedRetryAttempts(options)
  let lastError: unknown
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    options.signal?.throwIfAborted()
    try {
      return await execute({ attempt, maxAttempts })
    } catch (error) {
      lastError = error
      if (!isRetryableModelError(error) || attempt >= maxAttempts) throw error
      const delayMs = retryDelayMs(error, attempt)
      const retry: ModelRetryProgress = {
        failedAttempt: attempt,
        nextAttempt: attempt + 1,
        maxAttempts,
        delayMs,
        error: error instanceof Error ? error : new Error(String(error)),
      }
      options.onRetry?.(retry)
      const trace = options.observability
      await appendAnalysisTraceEvent(trace?.analysisId, {
        category: 'workflow',
        operation: `${trace?.operation ?? options.operation ?? 'model.chat_completion'}.retry`,
        phase: 'retry_backoff',
        status: 'scheduled',
        requestId: trace?.requestId,
        runId: trace?.runId,
        turn: trace?.turn,
        payload: {
          failed_attempt: retry.failedAttempt,
          next_attempt: retry.nextAttempt,
          max_attempts: retry.maxAttempts,
          delay_ms: retry.delayMs,
          error: analysisErrorPayload(error),
        },
      })
      await new Promise<void>((resolve, reject) => {
        const finish = () => {
          options.signal?.removeEventListener('abort', abort)
          resolve()
        }
        const timer = globalThis.setTimeout(finish, delayMs)
        const abort = () => {
          globalThis.clearTimeout(timer)
          options.signal?.removeEventListener('abort', abort)
          reject(options.signal?.reason ?? new DOMException('推理已由用户停止。', 'AbortError'))
        }
        if (options.signal?.aborted) {
          abort()
          return
        }
        options.signal?.addEventListener('abort', abort, { once: true })
      })
    }
  }
  throw lastError
}

function relayError(payload: unknown, fallback: string): ApiError {
  const error = asRecord(asRecord(payload).error)
  return new ApiError(
    stringValue(error.message) || fallback,
    stringValue(error.code) || 'MODEL_RELAY_STREAM_FAILED',
    asRecord(error.details),
  )
}

function outputLimitError(maxTokens: number | undefined): ApiError {
  const limit = typeof maxTokens === 'number' && Number.isFinite(maxTokens) ? Math.max(1, Math.round(maxTokens)) : null
  return new ApiError(
    `模型输出达到服务端${limit ? ` ${limit.toLocaleString('en-US')} Token` : ''}上限，返回内容不完整，平台已停止执行可能被截断的操作。可缩小本轮任务后继续。`,
    'MODEL_OUTPUT_TOKEN_LIMIT',
    limit ? { max_tokens: limit } : {},
  )
}

function reachedOutputLimit(reason: unknown): boolean {
  return reason === 'length' || reason === 'max_tokens'
}

function resolvedOutputBudget(
  config: BrowserModelConfig,
  options: ModelRequestOptions,
): { maxTokens: number; observation: Record<string, unknown> } {
  const callerRequestedTokens = typeof options.maxTokens === 'number' && Number.isFinite(options.maxTokens)
    ? Math.max(1, Math.round(options.maxTokens))
    : null
  const configuredTokens = Math.max(1, Math.round(config.agentMaxOutputTokens))
  const taskSpecific = options.outputBudgetPolicy === 'task_specific'
  const maxTokens = taskSpecific ? callerRequestedTokens ?? configuredTokens : configuredTokens
  return {
    maxTokens,
    observation: {
      configured_tokens: configuredTokens,
      caller_requested_tokens: callerRequestedTokens,
      effective_tokens: maxTokens,
      source: taskSpecific ? 'explicit_task_specific_override' : 'browser_model_config',
    },
  }
}

type PrefixMessageSignature = {
  role: TokenMessage['role']
  contentHash: string
  contentLength: number
  estimatedTokens: number
}

const previousPromptByConversation = new Map<string, PrefixMessageSignature[]>()
const prefixConversationLimit = 64
const legacyModelRelayEnabled = process.env.NEXT_PUBLIC_HETERO_AGENT_ENGINE === 'legacy'

function modelRelayIdentity(config: BrowserModelConfig): { baseUrl: string; apiKey: string; model: string } {
  return legacyModelRelayEnabled
    ? { baseUrl: config.baseUrl, apiKey: config.apiKey, model: config.model }
    : { baseUrl: 'agent-engine://kimi-server', apiKey: 'engine-managed', model: 'engine-default' }
}

function promptContentHash(value: string): string {
  let first = 2166136261
  let second = 0x9e3779b9
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index)
    first = Math.imul(first ^ code, 16777619)
    second = Math.imul(second ^ (code + index), 2246822519)
  }
  return `${(first >>> 0).toString(16).padStart(8, '0')}${(second >>> 0).toString(16).padStart(8, '0')}`
}

function prefixConversationKey(config: BrowserModelConfig, trace?: ModelObservabilityContext): string {
  const identity = modelRelayIdentity(config)
  return [
    identity.baseUrl,
    identity.model,
    trace?.analysisId || 'no-analysis',
    trace?.operation || 'model.chat_completion',
    trace?.runId || 'no-run',
  ].join('|')
}

function prefixCacheObservation(
  config: BrowserModelConfig,
  messages: ModelChatMessage[],
  trace?: ModelObservabilityContext,
): Record<string, unknown> {
  const key = prefixConversationKey(config, trace)
  const current = messages.map((message) => ({
    role: message.role,
    contentHash: promptContentHash(message.content),
    contentLength: message.content.length,
    estimatedTokens: estimateMessageTokens([message]),
  }))
  const previous = previousPromptByConversation.get(key) ?? []
  let commonMessageCount = 0
  while (
    commonMessageCount < previous.length
    && commonMessageCount < current.length
    && previous[commonMessageCount].role === current[commonMessageCount].role
    && previous[commonMessageCount].contentLength === current[commonMessageCount].contentLength
    && previous[commonMessageCount].contentHash === current[commonMessageCount].contentHash
  ) {
    commonMessageCount += 1
  }
  const reusableTokens = previous
    .slice(0, commonMessageCount)
    .reduce((total, message) => total + message.estimatedTokens, 0)
  previousPromptByConversation.delete(key)
  previousPromptByConversation.set(key, current)
  while (previousPromptByConversation.size > prefixConversationLimit) {
    const oldest = previousPromptByConversation.keys().next().value
    if (typeof oldest !== 'string') break
    previousPromptByConversation.delete(oldest)
  }
  return {
    layout: 'stable_system_then_append_only_history_v2',
    system_prefix_estimated_tokens: messages[0]?.role === 'system' ? estimateMessageTokens([messages[0]]) : 0,
    previous_request_message_count: previous.length,
    reusable_prefix_message_count: commonMessageCount,
    reusable_prefix_estimated_tokens: reusableTokens,
    append_only_from_previous_request: previous.length > 0 && commonMessageCount === previous.length,
    prefix_rewrite_detected: previous.length > 0 && commonMessageCount < previous.length,
    current_suffix_role: messages.at(-1)?.role ?? null,
  }
}

async function requestModelChatCompletionOnce(
  config: BrowserModelConfig,
  messages: ModelChatMessage[],
  options: ModelRequestOptions,
  retryAttempt: ModelRetryAttempt,
): Promise<ModelChatCompletion> {
  const trace = options.observability
  const requestId = trace?.requestId || randomUuid() || `model-${Date.now()}`
  const outputBudget = resolvedOutputBudget(config, options)
  const relayIdentity = modelRelayIdentity(config)
  const operation = trace?.operation ?? options.operation ?? 'model.chat_completion'
  await appendAnalysisTraceEvent(trace?.analysisId, {
    category: 'model_request',
    operation,
    phase: 'request',
    status: 'started',
    requestId,
    runId: trace?.runId,
    turn: trace?.turn,
    payload: {
      transport: 'non_streaming',
      execution_mode: options.executionMode ?? 'agent_engine',
      provider_base_url: relayIdentity.baseUrl,
      model: relayIdentity.model,
      max_tokens: outputBudget.maxTokens,
      output_budget: outputBudget.observation,
      retry_attempt: retryAttempt.attempt,
      retry_max_attempts: retryAttempt.maxAttempts,
      message_count: messages.length,
      prefix_cache: prefixCacheObservation(config, messages, trace),
      messages,
    },
  })
  try {
    const response = await api<Omit<ModelChatCompletion, 'tokenUsage'> & { agent_engine?: unknown }>('/v1/model-relay/chat-completions', {
      method: 'POST',
      signal: options.signal,
      body: JSON.stringify({
        base_url: relayIdentity.baseUrl,
        api_key: relayIdentity.apiKey,
        model: relayIdentity.model,
        messages,
        max_tokens: outputBudget.maxTokens,
        ...(trace?.analysisId ? { analysis_id: trace.analysisId } : {}),
        operation,
        request_id: requestId,
        execution_mode: options.executionMode ?? 'agent_engine',
      }),
    })
    const message = response.choices?.[0]?.message
    const content = message?.content ?? ''
    const reasoningContent = stringValue(message?.reasoning_content) || stringValue(message?.reasoning)
    if (response.choices?.some((choice) => reachedOutputLimit(choice.finish_reason))) {
      throw outputLimitError(outputBudget.maxTokens)
    }
    const tokenUsage = normalizeModelTokenUsage(response.usage, {
      inputTokens: estimateMessageTokens(messages),
      outputTokens: estimateTextTokens(content),
    })
    const agentEngine = agentEngineObservation(response.agent_engine)
    await appendAnalysisTraceEvent(trace?.analysisId, {
      category: 'model_response',
      operation,
      phase: 'response',
      status: 'succeeded',
      requestId,
      runId: trace?.runId,
      turn: trace?.turn,
      payload: {
        finish_reasons: response.choices?.map((choice) => choice.finish_reason ?? null) ?? [],
        content,
        reasoning_content: reasoningContent,
        provider_usage: response.usage ?? null,
        token_usage: tokenUsage,
        prompt_cache: {
          reported: tokenUsage.cachedInputTokens !== undefined,
          cached_input_tokens: tokenUsage.cachedInputTokens ?? null,
          hit_rate: tokenUsage.cacheHitRate ?? null,
        },
        retry_attempt: retryAttempt.attempt,
        agent_engine: response.agent_engine ?? null,
      },
    })
    return {
      ...response,
      tokenUsage,
      ...(reasoningContent ? { reasoningContent } : {}),
      ...(agentEngine ? { agentEngine } : {}),
    }
  } catch (error) {
    await appendAnalysisTraceEvent(trace?.analysisId, {
      category: 'model_error',
      operation,
      phase: 'response',
      status: 'failed',
      requestId,
      runId: trace?.runId,
      turn: trace?.turn,
      payload: {
        ...analysisErrorPayload(error),
        output_budget: outputBudget.observation,
        retry_attempt: retryAttempt.attempt,
        retry_max_attempts: retryAttempt.maxAttempts,
      },
    })
    throw error
  }
}

export async function requestModelChatCompletion(
  config: BrowserModelConfig,
  messages: ModelChatMessage[],
  options: ModelRequestOptions = {},
): Promise<ModelChatCompletion> {
  return requestWithRetry(
    (attempt) => requestModelChatCompletionOnce(config, messages, options, attempt),
    options,
  )
}

/** Consume a relayed OpenAI-compatible SSE response without exposing the key. */
async function requestModelChatCompletionStreamOnce(
  config: BrowserModelConfig,
  messages: ModelChatMessage[],
  options: ModelRequestOptions,
  retryAttempt: ModelRetryAttempt,
): Promise<ModelChatCompletion> {
  const trace = options.observability
  const requestId = trace?.requestId || randomUuid() || `model-${Date.now()}`
  const outputBudget = resolvedOutputBudget(config, options)
  const relayIdentity = modelRelayIdentity(config)
  let content = ''
  let reasoning = ''
  await appendAnalysisTraceEvent(trace?.analysisId, {
    category: 'model_request',
    operation: trace?.operation ?? 'model.chat_completion_stream',
    phase: 'request',
    status: 'started',
    requestId,
    runId: trace?.runId,
    turn: trace?.turn,
    payload: {
      transport: 'streaming',
      execution_mode: options.executionMode ?? 'agent_engine',
      provider_base_url: relayIdentity.baseUrl,
      model: relayIdentity.model,
      max_tokens: outputBudget.maxTokens,
      output_budget: outputBudget.observation,
      retry_attempt: retryAttempt.attempt,
      retry_max_attempts: retryAttempt.maxAttempts,
      message_count: messages.length,
      prefix_cache: prefixCacheObservation(config, messages, trace),
      messages,
    },
  })
  try {
    const response = await fetch(`${apiBase}/v1/model-relay/chat-completions/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: options.signal,
      body: JSON.stringify({
        base_url: relayIdentity.baseUrl,
        api_key: relayIdentity.apiKey,
        model: relayIdentity.model,
        messages,
        max_tokens: outputBudget.maxTokens,
        ...(trace?.analysisId ? { analysis_id: trace.analysisId } : {}),
        operation: trace?.operation ?? 'model.chat_completion_stream',
        request_id: requestId,
        execution_mode: options.executionMode ?? 'agent_engine',
      }),
    })
    if (!response.ok) {
      const payload: unknown = await response.json().catch(() => ({}))
      throw apiErrorFromPayload(payload, response.status)
    }
    if (!response.body) throw new ApiError('本机中继没有返回可读取的流式响应。', 'MODEL_RELAY_STREAM_EMPTY')

    let buffer = ''
    let eventName = 'message'
    let dataLines: string[] = []
    let completed = false
    let usage: unknown
    let agentEngine: ModelChatCompletion['agentEngine'] | undefined
    let finishReason: string | null = null

    const applyEvent = () => {
      const data = dataLines.join('\n').trim()
      const receivedEvent = eventName
      eventName = 'message'
      dataLines = []
      if (!data) return
      if (data === '[DONE]') {
        completed = true
        return
      }
      let payload: unknown
      try {
        payload = JSON.parse(data)
      } catch {
        throw new ApiError('上游模型服务返回了无法解析的流式事件。', 'MODEL_RELAY_STREAM_INVALID')
      }
      if (receivedEvent === 'error' || asRecord(payload).error) throw relayError(payload, '上游模型流式请求失败。')
      const record = asRecord(payload)
      const choices = Array.isArray(record.choices) ? record.choices.map(asRecord) : []
      const choice = choices[0] ?? {}
      if (typeof choice.finish_reason === 'string') finishReason = choice.finish_reason
      const delta = asRecord(choice.delta)
      const message = asRecord(choice.message)
      let contentDelta = stringValue(delta.content)
      let reasoningDelta = stringValue(delta.reasoning_content) || stringValue(delta.reasoning)
      if (!contentDelta && stringValue(message.content)) {
        const completedContent = stringValue(message.content)
        contentDelta = completedContent.startsWith(content) ? completedContent.slice(content.length) : completedContent
      }
      if (!reasoningDelta && stringValue(message.reasoning_content)) {
        const completedReasoning = stringValue(message.reasoning_content)
        reasoningDelta = completedReasoning.startsWith(reasoning) ? completedReasoning.slice(reasoning.length) : completedReasoning
      }
      content += contentDelta
      reasoning += reasoningDelta
      if (record.usage !== undefined) usage = record.usage
      if (record.agent_engine !== undefined) agentEngine = agentEngineObservation(record.agent_engine)
      if (contentDelta || reasoningDelta) options.onProgress?.({
        contentDelta,
        reasoningDelta,
        contentChars: content.length,
        reasoningChars: reasoning.length,
      })
    }

    const processLine = (rawLine: string) => {
      const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine
      if (!line) {
        applyEvent()
        return
      }
      if (line.startsWith('event:')) {
        eventName = line.slice('event:'.length).trim() || 'message'
        return
      }
      if (line.startsWith('data:')) dataLines.push(line.slice('data:'.length).trimStart())
    }

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    try {
      while (true) {
        const { done, value } = await reader.read()
        buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done })
        const lines = buffer.split('\n')
        buffer = lines.pop() ?? ''
        for (const line of lines) processLine(line)
        if (done) break
      }
      buffer += decoder.decode()
      if (buffer) processLine(buffer)
      if (dataLines.length > 0) applyEvent()
    } finally {
      reader.releaseLock()
    }
    if (!completed) throw new ApiError('模型流式连接在完成标记前关闭。', 'MODEL_RELAY_STREAM_INCOMPLETE')
    if (reachedOutputLimit(finishReason)) throw outputLimitError(outputBudget.maxTokens)
    const tokenUsage = normalizeModelTokenUsage(usage, {
      inputTokens: estimateMessageTokens(messages),
      outputTokens: estimateTextTokens(content),
    })
    await appendAnalysisTraceEvent(trace?.analysisId, {
      category: 'model_response',
      operation: trace?.operation ?? 'model.chat_completion_stream',
      phase: 'response',
      status: 'succeeded',
      requestId,
      runId: trace?.runId,
      turn: trace?.turn,
      payload: {
        finish_reason: finishReason,
        content,
        reasoning_content: reasoning,
        provider_usage: usage ?? null,
        token_usage: tokenUsage,
        prompt_cache: {
          reported: tokenUsage.cachedInputTokens !== undefined,
          cached_input_tokens: tokenUsage.cachedInputTokens ?? null,
          hit_rate: tokenUsage.cacheHitRate ?? null,
        },
        retry_attempt: retryAttempt.attempt,
        agent_engine: agentEngine ?? null,
      },
    })
    return {
      choices: [{ finish_reason: finishReason, message: { content, ...(reasoning ? { reasoning_content: reasoning } : {}) } }],
      usage,
      tokenUsage,
      ...(reasoning ? { reasoningContent: reasoning } : {}),
      ...(agentEngine ? { agentEngine } : {}),
    }
  } catch (error) {
    const errorPayload = analysisErrorPayload(error)
    await appendAnalysisTraceEvent(trace?.analysisId, {
      category: 'model_error',
      operation: trace?.operation ?? 'model.chat_completion_stream',
      phase: 'response',
      status: 'failed',
      requestId,
      runId: trace?.runId,
      turn: trace?.turn,
      payload: {
        ...errorPayload,
        output_budget: outputBudget.observation,
        partial_stream: {
          content_chars: content.length,
          reasoning_chars: reasoning.length,
          content_tail: content.slice(-2000),
          reasoning_tail: reasoning.slice(-2000),
        },
        retry_attempt: retryAttempt.attempt,
        retry_max_attempts: retryAttempt.maxAttempts,
      },
    })
    throw error
  }
}

export async function requestModelChatCompletionStream(
  config: BrowserModelConfig,
  messages: ModelChatMessage[],
  options: ModelRequestOptions = {},
): Promise<ModelChatCompletion> {
  return requestWithRetry(
    (attempt) => requestModelChatCompletionStreamOnce(config, messages, options, attempt),
    options,
  )
}
