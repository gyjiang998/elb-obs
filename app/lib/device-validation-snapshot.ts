import type { BackendAgentRunState } from './backend-agent-runtime'
import { normalizeDeviceValidationFacts, type DeviceValidationFacts } from './agent-runtime-contract'

export const deviceValidationSnapshotStorageKey = 'heterogeneous-enable-device-validation-snapshot-v1'
export const deviceValidationSnapshotEvent = 'page-agent-device-validation-snapshot'

export type DeviceValidationSnapshot = {
  schemaVersion: 'device_validation_snapshot/v1'
  analysisId: string
  requestId: string
  runId: string
  status: BackendAgentRunState
  facts: DeviceValidationFacts
  requestedUnit?: string
  recommendedProfileId?: string
  compatibleProfileIds?: string[]
  activeActionId?: string
  activeActionDetail?: string
  finalResponse?: string
  updatedAt: string
}

const runStates = new Set<BackendAgentRunState>([
  'created',
  'waiting_for_model_credential',
  'running',
  'waiting_for_tool',
  'waiting_for_ui',
  'waiting_for_device',
  'compacting',
  'paused_model_overloaded',
  'paused_output_limit',
  'paused_turn_budget',
  'paused_bridge_offline',
  'completed',
  'failed',
  'cancelled',
])

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function runState(value: unknown): BackendAgentRunState | null {
  return typeof value === 'string' && runStates.has(value as BackendAgentRunState)
    ? value as BackendAgentRunState
    : null
}

export function normalizeDeviceValidationSnapshot(value: unknown, expectedAnalysisId?: string | null): DeviceValidationSnapshot | null {
  const raw = record(value)
  const analysisId = text(raw.analysisId)
  const status = runState(raw.status)
  if (
    raw.schemaVersion !== 'device_validation_snapshot/v1'
    || !analysisId
    || (expectedAnalysisId && analysisId !== expectedAnalysisId)
    || !text(raw.requestId)
    || !text(raw.runId)
    || !status
  ) return null
  const compatibleProfileIds = Array.isArray(raw.compatibleProfileIds)
    ? raw.compatibleProfileIds.filter((item): item is string => typeof item === 'string' && item.trim().length > 0).slice(0, 8)
    : []
  return {
    schemaVersion: 'device_validation_snapshot/v1',
    analysisId,
    requestId: text(raw.requestId),
    runId: text(raw.runId),
    status,
    facts: normalizeDeviceValidationFacts(raw.facts),
    ...(text(raw.requestedUnit) ? { requestedUnit: text(raw.requestedUnit) } : {}),
    ...(text(raw.recommendedProfileId) ? { recommendedProfileId: text(raw.recommendedProfileId) } : {}),
    ...(compatibleProfileIds.length > 0 ? { compatibleProfileIds } : {}),
    ...(text(raw.activeActionId) ? { activeActionId: text(raw.activeActionId).slice(0, 160) } : {}),
    ...(text(raw.activeActionDetail) ? { activeActionDetail: text(raw.activeActionDetail).slice(0, 1000) } : {}),
    ...(text(raw.finalResponse) ? { finalResponse: text(raw.finalResponse).slice(-12_000) } : {}),
    updatedAt: text(raw.updatedAt) || new Date().toISOString(),
  }
}

export function storeDeviceValidationSnapshot(snapshot: DeviceValidationSnapshot | null) {
  if (typeof window === 'undefined') return
  try {
    if (!snapshot) {
      window.localStorage.removeItem(deviceValidationSnapshotStorageKey)
      return
    }
    const stored = normalizeDeviceValidationSnapshot({
      ...snapshot,
      facts: normalizeDeviceValidationFacts(snapshot.facts),
      finalResponse: snapshot.finalResponse?.slice(-12_000),
      updatedAt: new Date().toISOString(),
    })
    if (!stored) return
    window.localStorage.setItem(deviceValidationSnapshotStorageKey, JSON.stringify(stored))
    window.dispatchEvent(new CustomEvent(deviceValidationSnapshotEvent, { detail: stored }))
  } catch {
    // The backend Run execution trace remains authoritative when browser
    // persistence is disabled.
  }
}

export function readDeviceValidationSnapshot(expectedAnalysisId: string | null): DeviceValidationSnapshot | null {
  if (typeof window === 'undefined' || !expectedAnalysisId) return null
  try {
    return normalizeDeviceValidationSnapshot(
      JSON.parse(window.localStorage.getItem(deviceValidationSnapshotStorageKey) ?? 'null'),
      expectedAnalysisId,
    )
  } catch {
    return null
  }
}
