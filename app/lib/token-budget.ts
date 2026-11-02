export type TokenMessage = {
  role: 'system' | 'user' | 'assistant'
  content: string
}

export type TokenSource = 'provider' | 'estimated'

export type ModelTokenUsage = {
  inputTokens: number
  outputTokens: number
  totalTokens: number
  inputSource: TokenSource
  outputSource: TokenSource
  cachedInputTokens?: number
  cacheHitRate?: number
}

export type PromptTokenBudget = {
  contextWindowTokens: number
  inputTokens: number
  outputReserveTokens: number
  occupiedTokens: number
  remainingTokens: number
  utilization: number
  nearLimit: boolean
  exceedsWindow: boolean
  compactionApplied: boolean
  compactedItems: number
}

export type ConversationMemory = {
  summary: string
  messages: TokenMessage[]
  compactedMessageCount: number
}

export type BoundedConversationRequest = {
  messages: TokenMessage[]
  memory: ConversationMemory
  budget: PromptTokenBudget
}

export const DEFAULT_CONTEXT_WINDOW_TOKENS = 128 * 1024
export const MIN_CONTEXT_WINDOW_TOKENS = 8 * 1024
export const MAX_CONTEXT_WINDOW_TOKENS = 1024 * 1024
export const DEFAULT_MODEL_OUTPUT_TOKENS = 16 * 1024
export const CONTEXT_COMPACTION_RATIO = 0.8
// Must stay aligned with ModelRelayChatCompletionRequest.messages.
export const MAX_MODEL_RELAY_MESSAGES = 24
// Compact in batches so the leading summary remains unchanged for several
// turns. Rewriting it on every call defeats provider prefix caching.
const MESSAGE_COMPACTION_HEADROOM = 6

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function finiteTokenCount(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? Math.round(value) : null
}

function firstTokenCount(...values: unknown[]): number | null {
  for (const value of values) {
    const parsed = finiteTokenCount(value)
    if (parsed !== null) return parsed
  }
  return null
}

export function normalizeContextWindowTokens(value: unknown, fallback = DEFAULT_CONTEXT_WINDOW_TOKENS): number {
  const parsed = typeof value === 'number' ? value : typeof value === 'string' ? Number(value) : Number.NaN
  if (!Number.isFinite(parsed)) return fallback
  return Math.min(MAX_CONTEXT_WINDOW_TOKENS, Math.max(MIN_CONTEXT_WINDOW_TOKENS, Math.round(parsed)))
}

export function outputReserveForContext(contextWindowTokens: number): number {
  const normalized = normalizeContextWindowTokens(contextWindowTokens)
  return Math.min(DEFAULT_MODEL_OUTPUT_TOKENS, Math.max(1024, Math.floor(normalized / 2)))
}

export function configuredOutputReserveForContext(
  contextWindowTokens: number,
  configuredOutputTokens: number,
): number {
  const normalized = normalizeContextWindowTokens(contextWindowTokens)
  const requested = Number.isFinite(configuredOutputTokens)
    ? Math.floor(configuredOutputTokens)
    : outputReserveForContext(normalized)
  return Math.min(normalized - 1, Math.max(1, requested))
}

export function estimateTextTokens(value: string): number {
  if (!value) return 0
  let cjk = 0
  let latin = 0
  let punctuation = 0
  for (const character of value) {
    if (/\s/u.test(character)) continue
    if (/[\u3400-\u9fff\uf900-\ufaff]/u.test(character)) {
      cjk += 1
    } else if (/[A-Za-z0-9_$]/u.test(character)) {
      latin += 1
    } else {
      punctuation += 1
    }
  }
  return Math.max(1, Math.ceil(cjk + latin / 3.6 + punctuation / 1.8))
}

export function estimateMessageTokens(messages: TokenMessage[]): number {
  return 2 + messages.reduce((total, message) => total + 4 + estimateTextTokens(message.content), 0)
}

export function formatTokenCount(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  if (value === 0) return '0'
  const kilo = value / 1000
  const decimals = kilo >= 100 ? 0 : 1
  return `${kilo.toFixed(decimals).replace(/\.0$/, '')}k`
}

export function createPromptTokenBudget(input: {
  contextWindowTokens: number
  inputTokens: number
  outputReserveTokens?: number
  compactionApplied?: boolean
  compactedItems?: number
}): PromptTokenBudget {
  const contextWindowTokens = normalizeContextWindowTokens(input.contextWindowTokens)
  const inputTokens = Math.max(0, Math.round(input.inputTokens))
  const outputReserveTokens = Math.min(
    contextWindowTokens - 1,
    Math.max(1, Math.round(input.outputReserveTokens ?? outputReserveForContext(contextWindowTokens))),
  )
  const occupiedTokens = inputTokens + outputReserveTokens
  return {
    contextWindowTokens,
    inputTokens,
    outputReserveTokens,
    occupiedTokens,
    remainingTokens: Math.max(0, contextWindowTokens - occupiedTokens),
    utilization: Math.min(1, occupiedTokens / contextWindowTokens),
    nearLimit: occupiedTokens >= Math.floor(contextWindowTokens * CONTEXT_COMPACTION_RATIO),
    exceedsWindow: occupiedTokens > contextWindowTokens,
    compactionApplied: Boolean(input.compactionApplied),
    compactedItems: Math.max(0, Math.round(input.compactedItems ?? 0)),
  }
}

export function compactTextToTokenLimit(value: string, tokenLimit: number, marker = '[已按上下文预算压缩]'): { text: string; truncated: boolean } {
  const limit = Math.max(1, Math.round(tokenLimit))
  if (estimateTextTokens(value) <= limit) return { text: value, truncated: false }
  const markerTokens = estimateTextTokens(marker)
  const target = Math.max(1, limit - markerTokens)
  let characters = Math.max(8, Math.floor(value.length * target / Math.max(estimateTextTokens(value), 1)))
  let compacted = value
  for (let attempt = 0; attempt < 12; attempt += 1) {
    const prefixLength = Math.max(1, Math.floor(characters * 0.7))
    const suffixLength = Math.max(1, characters - prefixLength)
    compacted = `${value.slice(0, prefixLength)}\n${marker}\n${value.slice(-suffixLength)}`
    if (estimateTextTokens(compacted) <= limit) break
    characters = Math.max(2, Math.floor(characters * 0.78))
  }
  return { text: compacted, truncated: true }
}

function conversationDigest(previousSummary: string, discarded: TokenMessage[], tokenLimit: number): string {
  const rows = [
    previousSummary ? `既有压缩摘要：${previousSummary}` : '',
    ...discarded.map((message) => {
      const excerpt = compactTextToTokenLimit(message.content.replace(/\s+/g, ' ').trim(), 120, '[省略]').text
      return `${message.role === 'user' ? '用户' : '助手'}：${excerpt}`
    }),
  ].filter(Boolean)
  return compactTextToTokenLimit(rows.join('\n'), tokenLimit, '[更早会话已压缩]').text
}

export function buildBoundedConversationRequest(input: {
  systemPrompt: string
  memory: ConversationMemory
  userMessage: string
  contextWindowTokens: number
  outputReserveTokens?: number
}): BoundedConversationRequest {
  const contextWindowTokens = normalizeContextWindowTokens(input.contextWindowTokens)
  const outputReserveTokens = input.outputReserveTokens ?? outputReserveForContext(contextWindowTokens)
  const inputLimit = Math.max(1, Math.min(
    contextWindowTokens - outputReserveTokens,
    Math.floor(contextWindowTokens * CONTEXT_COMPACTION_RATIO) - outputReserveTokens,
  ))
  const base = [{ role: 'system' as const, content: input.systemPrompt }]
  const priorSummary = input.memory.summary.trim()
  const currentUser = { role: 'user' as const, content: input.userMessage }
  const fullMessages = [
    ...base,
    ...(priorSummary ? [{ role: 'system' as const, content: `更早会话摘要（只保留事实与未完成意图）：\n${priorSummary}` }] : []),
    ...input.memory.messages,
    currentUser,
  ]
  const fullTokens = estimateMessageTokens(fullMessages)
  if (fullTokens <= inputLimit && fullMessages.length <= MAX_MODEL_RELAY_MESSAGES) {
    return {
      messages: fullMessages,
      memory: input.memory,
      budget: createPromptTokenBudget({ contextWindowTokens, inputTokens: fullTokens, outputReserveTokens, compactionApplied: Boolean(priorSummary), compactedItems: input.memory.compactedMessageCount }),
    }
  }

  const summaryLimit = Math.max(192, Math.min(960, Math.floor(inputLimit * 0.16)))
  const systemAndUserTokens = estimateMessageTokens([...base, currentUser])
  const retained: TokenMessage[] = []
  let retainedTokens = 0
  const retainedBudget = Math.max(0, inputLimit - systemAndUserTokens - summaryLimit - 8)
  // Reserve system + summary + current user, plus several future append-only
  // turns before another summary rewrite is necessary.
  const retainedMessageLimit = MAX_MODEL_RELAY_MESSAGES - 3 - MESSAGE_COMPACTION_HEADROOM
  for (let index = input.memory.messages.length - 1; index >= 0; index -= 1) {
    if (retained.length >= retainedMessageLimit) break
    const message = input.memory.messages[index]
    const messageTokens = estimateMessageTokens([message])
    if (retainedTokens + messageTokens > retainedBudget) continue
    retained.unshift(message)
    retainedTokens += messageTokens
  }
  const retainedMessages = new Set(retained)
  const discarded = input.memory.messages.filter((message) => !retainedMessages.has(message))
  let summary = conversationDigest(priorSummary, discarded, summaryLimit)
  let messages = [
    ...base,
    ...(summary ? [{ role: 'system' as const, content: `更早会话摘要（只保留事实与未完成意图）：\n${summary}` }] : []),
    ...retained,
    currentUser,
  ]
  let inputTokens = estimateMessageTokens(messages)
  if (inputTokens > inputLimit && summary) {
    summary = compactTextToTokenLimit(summary, Math.max(48, summaryLimit / 2), '[摘要继续压缩]').text
    messages = [
      ...base,
      { role: 'system', content: `更早会话摘要（只保留事实与未完成意图）：\n${summary}` },
      ...retained,
      currentUser,
    ]
    inputTokens = estimateMessageTokens(messages)
  }
  return {
    messages,
    memory: {
      summary,
      messages: retained,
      compactedMessageCount: input.memory.compactedMessageCount + discarded.length,
    },
    budget: createPromptTokenBudget({
      contextWindowTokens,
      inputTokens,
      outputReserveTokens,
      compactionApplied: true,
      compactedItems: input.memory.compactedMessageCount + discarded.length,
    }),
  }
}

export function normalizeModelTokenUsage(rawUsage: unknown, fallback: { inputTokens: number; outputTokens: number }): ModelTokenUsage {
  const usage = asRecord(rawUsage)
  const promptDetails = asRecord(usage.prompt_tokens_details)
  const inputDetails = asRecord(usage.input_tokens_details)
  const reportedCacheHit = firstTokenCount(
    promptDetails.cached_tokens,
    promptDetails.cache_read_tokens,
    inputDetails.cached_tokens,
    inputDetails.cache_read_input_tokens,
    usage.prompt_cache_hit_tokens,
    usage.cached_input_tokens,
    usage.cache_read_input_tokens,
    usage.cached_tokens,
  )
  const reportedCacheMiss = firstTokenCount(
    usage.prompt_cache_miss_tokens,
    usage.cache_creation_input_tokens,
  )
  const derivedInput = reportedCacheHit !== null && reportedCacheMiss !== null
    ? reportedCacheHit + reportedCacheMiss
    : null
  const actualInput = firstTokenCount(
    usage.prompt_tokens,
    usage.input_tokens,
    usage.prompt_token_count,
    usage.input_token_count,
    derivedInput,
  )
  const actualOutput = firstTokenCount(usage.completion_tokens, usage.output_tokens, usage.completion_token_count, usage.output_token_count)
  const actualTotal = firstTokenCount(usage.total_tokens, usage.total_token_count)
  const inputTokens = actualInput ?? Math.max(0, Math.round(fallback.inputTokens))
  const outputTokens = actualOutput ?? Math.max(0, Math.round(fallback.outputTokens))
  const cachedInputTokens = reportedCacheHit === null ? null : Math.min(inputTokens, reportedCacheHit)
  return {
    inputTokens,
    outputTokens,
    totalTokens: actualTotal ?? inputTokens + outputTokens,
    inputSource: actualInput === null ? 'estimated' : 'provider',
    outputSource: actualOutput === null ? 'estimated' : 'provider',
    ...(cachedInputTokens === null
      ? {}
      : {
          cachedInputTokens,
          cacheHitRate: inputTokens > 0 ? cachedInputTokens / inputTokens : 0,
        }),
  }
}
