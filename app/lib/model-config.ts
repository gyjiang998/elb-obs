import { DEFAULT_CONTEXT_WINDOW_TOKENS, DEFAULT_MODEL_OUTPUT_TOKENS, normalizeContextWindowTokens } from './token-budget'

export type BrowserModelConfig = {
  baseUrl: string
  apiKey: string
  model: string
  contextWindowTokens: number
  agentMaxOutputTokens: number
  agentMaxTurns: number
  agentMaxActionsPerTurn: number
  agentExecutionPolicy: 'full_access' | 'confirm_device_execution'
}

export type ModelProviderPresetId = 'kimi_coding' | 'moonshot_platform' | 'custom'

export type ModelProviderPreset = {
  id: Exclude<ModelProviderPresetId, 'custom'>
  label: string
  baseUrl: string
  model: string
}

export const modelConfigStorageKey = 'heterogeneous-enable-agent-model-v1'
export const modelConfigChangedEvent = 'heterogeneous-enable-model-config-changed'

export const DEFAULT_AGENT_MAX_TURNS = 10
export const DEFAULT_AGENT_MAX_ACTIONS_PER_TURN = 8
export const DEFAULT_AGENT_MAX_OUTPUT_TOKENS = DEFAULT_MODEL_OUTPUT_TOKENS
export const MIN_AGENT_MAX_OUTPUT_TOKENS = 1024
export const MAX_AGENT_MAX_OUTPUT_TOKENS = 64 * 1024

export function normalizeAgentMaxOutputTokens(
  value: unknown,
  contextWindowTokens: number,
  fallback = DEFAULT_AGENT_MAX_OUTPUT_TOKENS,
): number {
  const parsed = typeof value === 'number' ? value : Number(value)
  const fallbackValue = Number.isFinite(fallback) ? fallback : DEFAULT_AGENT_MAX_OUTPUT_TOKENS
  const requested = Number.isFinite(parsed) ? parsed : fallbackValue
  const contextBound = Math.max(MIN_AGENT_MAX_OUTPUT_TOKENS, Math.floor(normalizeContextWindowTokens(contextWindowTokens) / 2))
  return Math.min(MAX_AGENT_MAX_OUTPUT_TOKENS, contextBound, Math.max(MIN_AGENT_MAX_OUTPUT_TOKENS, Math.floor(requested)))
}

export function normalizeAgentMaxTurns(value: unknown, fallback = DEFAULT_AGENT_MAX_TURNS): number {
  const parsed = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(parsed)) return fallback
  return Math.min(30, Math.max(1, Math.floor(parsed)))
}

export function normalizeAgentMaxActionsPerTurn(value: unknown, fallback = DEFAULT_AGENT_MAX_ACTIONS_PER_TURN): number {
  const parsed = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(parsed)) return fallback
  return Math.min(8, Math.max(1, Math.floor(parsed)))
}

export function normalizeAgentExecutionPolicy(value: unknown): BrowserModelConfig['agentExecutionPolicy'] {
  return value === 'confirm_device_execution' ? 'confirm_device_execution' : 'full_access'
}

export const modelProviderPresets: ModelProviderPreset[] = [
  {
    id: 'kimi_coding',
    label: 'Kimi Coding',
    baseUrl: 'https://api.kimi.com/coding/v1',
    model: 'kimi-for-coding',
  },
  {
    id: 'moonshot_platform',
    label: 'Kimi 开放平台',
    baseUrl: 'https://api.moonshot.cn/v1',
    model: '',
  },
]

const defaults: BrowserModelConfig = {
  baseUrl: 'https://api.kimi.com/coding/v1',
  apiKey: '',
  model: 'kimi-for-coding',
  contextWindowTokens: DEFAULT_CONTEXT_WINDOW_TOKENS,
  agentMaxOutputTokens: DEFAULT_AGENT_MAX_OUTPUT_TOKENS,
  agentMaxTurns: DEFAULT_AGENT_MAX_TURNS,
  agentMaxActionsPerTurn: DEFAULT_AGENT_MAX_ACTIONS_PER_TURN,
  agentExecutionPolicy: 'full_access',
}

// Keep the server render and the browser's first render deterministic. Browser
// storage is synchronized from effects after hydration instead of during render.
export function createDefaultBrowserModelConfig(): BrowserModelConfig {
  return { ...defaults }
}

export function providerPresetId(config: BrowserModelConfig): ModelProviderPresetId {
  return modelProviderPresets.find((preset) => preset.baseUrl === config.baseUrl.trim().replace(/\/+$/, ''))?.id ?? 'custom'
}

export function applyProviderPreset(config: BrowserModelConfig, presetId: ModelProviderPresetId): BrowserModelConfig {
  const preset = modelProviderPresets.find((item) => item.id === presetId)
  if (!preset) return config
  return { ...config, baseUrl: preset.baseUrl, model: preset.model }
}

export function readBrowserModelConfig(): BrowserModelConfig {
  if (typeof window === 'undefined') return createDefaultBrowserModelConfig()
  try {
    const saved = JSON.parse(window.localStorage.getItem(modelConfigStorageKey) ?? '{}') as Partial<BrowserModelConfig>
    const contextWindowTokens = normalizeContextWindowTokens(saved.contextWindowTokens, defaults.contextWindowTokens)
    return {
      baseUrl: saved.baseUrl ?? defaults.baseUrl,
      apiKey: saved.apiKey ?? '',
      model: saved.model ?? '',
      contextWindowTokens,
      agentMaxOutputTokens: normalizeAgentMaxOutputTokens(saved.agentMaxOutputTokens, contextWindowTokens, defaults.agentMaxOutputTokens),
      agentMaxTurns: normalizeAgentMaxTurns(saved.agentMaxTurns, defaults.agentMaxTurns),
      agentMaxActionsPerTurn: normalizeAgentMaxActionsPerTurn(saved.agentMaxActionsPerTurn, defaults.agentMaxActionsPerTurn),
      agentExecutionPolicy: normalizeAgentExecutionPolicy(saved.agentExecutionPolicy),
    }
  } catch {
    return createDefaultBrowserModelConfig()
  }
}

export function writeBrowserModelConfig(config: BrowserModelConfig): void {
  const contextWindowTokens = normalizeContextWindowTokens(config.contextWindowTokens)
  const normalized = {
    ...config,
    contextWindowTokens,
    agentMaxOutputTokens: normalizeAgentMaxOutputTokens(config.agentMaxOutputTokens, contextWindowTokens),
    agentMaxTurns: normalizeAgentMaxTurns(config.agentMaxTurns),
    agentMaxActionsPerTurn: normalizeAgentMaxActionsPerTurn(config.agentMaxActionsPerTurn),
    agentExecutionPolicy: normalizeAgentExecutionPolicy(config.agentExecutionPolicy),
  }
  window.localStorage.setItem(modelConfigStorageKey, JSON.stringify(normalized))
  window.dispatchEvent(new CustomEvent(modelConfigChangedEvent, { detail: normalized }))
}

export function clearBrowserModelConfig(): void {
  window.localStorage.removeItem(modelConfigStorageKey)
  window.dispatchEvent(new CustomEvent(modelConfigChangedEvent, { detail: createDefaultBrowserModelConfig() }))
}
