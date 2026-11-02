import type { StrategyId } from './optimization-workflow'
import type { TargetPlatformProfile } from './target-platform-profile'
import type { ModelTokenUsage, PromptTokenBudget } from './token-budget'
import type { Artifact, JsonRecord } from './types'

export const analysisHistoryStorageKey = 'heterogeneous-enable-analysis-history-v1'

export type AnalysisInferenceStatus = 'running' | 'succeeded' | 'failed' | 'blocked'
export type AnalysisInferenceStrategyId = StrategyId | 'blind_baseline' | 'blind_comparison'

export type AnalysisSkillProfileSnapshot = {
  profileId: string
  profileSha256: string
  skills: Record<string, { version: string; contentSha256: string }>
}

export type AnalysisContextSnapshot = {
  contextArtifactId: string
  contextSha256: string
  promptContractVersion: string
  source: {
    origin: string
    path: string
    lineStart: number | null
    lineEnd: number | null
    sha256: string
    bindingLevel: string
  }
  hotspot: {
    eventId: string
    eventName: string
  }
  retrieval: {
    method: string
    terms: string[]
    candidateCount: number | null
    truncated: boolean | null
  }
  targetPlatformProfile?: TargetPlatformProfile
  skillProfile?: AnalysisSkillProfileSnapshot
  evidence: Array<{ evidenceId: string; label: string; score: number | null }>
}

export type AnalysisInferenceRecord = {
  inferenceId: string
  runId: string
  strategyId: AnalysisInferenceStrategyId
  strategyLabel: string
  status: AnalysisInferenceStatus
  startedAt: string
  completedAt?: string
  model: { provider: string; name: string }
  context: AnalysisContextSnapshot
  tokenUsage?: ModelTokenUsage
  tokenBudget?: PromptTokenBudget
  resultArtifactId?: string
  transcriptArtifactId?: string
  summary?: string
  error?: string
}

export type AnalysisHotspotSummary = {
  eventId?: string
  eventName?: string
  sourceKind?: 'trace_source' | 'pasted_code'
  codeArtifactId?: string
  candidateCount?: number
  moduleKey?: string
}

export type AnalysisDeviceValidationStatus = 'planned' | 'evidence_required' | 'passed' | 'unsupported' | 'failed'

export type AnalysisDeviceValidationRecord = {
  validationId: string
  status: AnalysisDeviceValidationStatus
  createdAt: string
  updatedAt: string
  deviceSerial: string
  requestedUnit: 'npu' | 'gpu' | 'dsp' | 'media' | string
  probeArtifactId: string
  planArtifactId: string
  reasoningResultArtifactId: string
  runArtifactId?: string
  adapterArtifactId?: string
  summary: string
}

export type TraceAnalysisRecord = {
  analysisId: string
  trace: {
    artifactId: string
    label: string
    inputKind: string
    sourceConnectorId: string
  }
  createdAt: string
  updatedAt: string
  hotspot?: AnalysisHotspotSummary
  inferences: AnalysisInferenceRecord[]
  // Optional so records created before real-device validation remain readable.
  deviceValidations?: AnalysisDeviceValidationRecord[]
}

export type TraceAnalysisStart = TraceAnalysisRecord['trace']

function asRecord(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {}
}

function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' && value.trim() ? value.trim() : fallback
}

function nullableNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function contextRecordLabel(value: JsonRecord): string {
  const item = asRecord(value.record)
  return text(item.name)
    || text(item.title)
    || text(item.hardware_id)
    || text(item.record_id)
    || text(item.case_id)
    || text(value.evidence_id, '结构化上下文记录')
}

export function snapshotAnalysisContext(context: Artifact): AnalysisContextSnapshot {
  const source = asRecord(context.source)
  const hotspot = asRecord(context.hotspot)
  const retrieval = asRecord(context.retrieval)
  const evidence = Array.isArray(context.context_records)
    ? context.context_records.slice(0, 12).map(asRecord).map((item) => ({
      evidenceId: text(item.evidence_id, 'unknown'),
      label: contextRecordLabel(item),
      score: nullableNumber(item.score),
    }))
    : []
  const terms = Array.isArray(retrieval.terms)
    ? retrieval.terms.filter((item): item is string => typeof item === 'string' && item.trim().length > 0).slice(0, 16)
    : []
  const targetPlatformProfile = asRecord(context.target_platform_profile)
  const chipset = asRecord(targetPlatformProfile.chipset)
  const operatingSystem = asRecord(targetPlatformProfile.operating_system)
  const hasTargetPlatformProfile = text(targetPlatformProfile.schema_version) === 'target_platform_profile/v1'
    && text(chipset.model_id)
    && text(operatingSystem.version_id)
  const skillProfile = asRecord(context.skill_profile)
  const profileSkills = asRecord(skillProfile.skills)
  const profileId = text(skillProfile.profile_id)
  const profileSha256 = text(skillProfile.profile_sha256)
  const skillVersions = Object.fromEntries(Object.entries(profileSkills).flatMap(([skillId, raw]) => {
    const item = asRecord(raw)
    const version = text(item.version)
    const contentSha256 = text(item.content_sha256)
    return version && contentSha256 ? [[skillId, { version, contentSha256 }]] : []
  }))
  return {
    contextArtifactId: context.artifact_id,
    contextSha256: text(context.context_sha256),
    promptContractVersion: text(context.prompt_contract_version),
    source: {
      origin: text(source.origin),
      path: text(source.path),
      lineStart: nullableNumber(source.line_start),
      lineEnd: nullableNumber(source.line_end),
      sha256: text(source.sha256),
      bindingLevel: text(source.binding_level),
    },
    hotspot: {
      eventId: text(hotspot.event_id),
      eventName: text(hotspot.event_name),
    },
    retrieval: {
      method: text(retrieval.method, 'none'),
      terms,
      candidateCount: nullableNumber(retrieval.candidate_count),
      truncated: typeof retrieval.truncated === 'boolean' ? retrieval.truncated : null,
    },
    ...(hasTargetPlatformProfile ? {
      targetPlatformProfile: {
        schema_version: 'target_platform_profile/v1',
        profile_source: 'user_selected',
        capability_status: 'not_device_probe',
        chipset: {
          family_id: text(chipset.family_id),
          family_label: text(chipset.family_label),
          model_id: text(chipset.model_id),
          model_label: text(chipset.model_label),
        },
        operating_system: {
          family_id: text(operatingSystem.family_id),
          family_label: text(operatingSystem.family_label),
          version_id: text(operatingSystem.version_id),
          version_label: text(operatingSystem.version_label),
        },
      } satisfies TargetPlatformProfile,
    } : {}),
    ...(profileId && profileSha256 ? {
      skillProfile: {
        profileId,
        profileSha256,
        skills: skillVersions,
      },
    } : {}),
    evidence,
  }
}

export function createTraceAnalysisRecord(analysisId: string, trace: TraceAnalysisStart, now = new Date().toISOString()): TraceAnalysisRecord {
  return {
    analysisId,
    trace,
    createdAt: now,
    updatedAt: now,
    inferences: [],
    deviceValidations: [],
  }
}

export function readAnalysisHistory(): TraceAnalysisRecord[] {
  if (typeof window === 'undefined') return []
  try {
    const value: unknown = JSON.parse(window.localStorage.getItem(analysisHistoryStorageKey) ?? '[]')
    if (!Array.isArray(value)) return []
    return value.filter((item): item is TraceAnalysisRecord => {
      const record = asRecord(item)
      return Boolean(text(record.analysisId) && text(asRecord(record.trace).artifactId) && Array.isArray(record.inferences))
    }).slice(0, 40)
  } catch {
    return []
  }
}

export function writeAnalysisHistory(records: TraceAnalysisRecord[]): void {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(analysisHistoryStorageKey, JSON.stringify(records.slice(0, 40)))
  } catch {
    // The immutable backend context artifacts remain available even if browser storage is full.
  }
}

export function sortAnalysisHistory(records: TraceAnalysisRecord[]): TraceAnalysisRecord[] {
  return [...records].sort((left, right) => right.updatedAt.localeCompare(left.updatedAt)).slice(0, 40)
}
