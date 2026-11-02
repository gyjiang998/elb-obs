export type ParallelObjective = 'minimum_latency' | 'minimum_frequency_with_budget'
export type MainParticipationControl = 'both' | 'yes' | 'no'

export type RestoredParallelSearchControls = {
  objective?: ParallelObjective
  budget?: string
  availableCores?: string[]
  mainCore?: string
  workerCounts?: number[]
  mainParticipation?: MainParticipationControl
  frequencyPolicy?: 'all_opps' | 'max_opps'
  frequenciesKHz?: Record<string, number[]> | null
  robustness?: boolean
  candidateIds?: string[]
}

type UnknownRecord = Record<string, unknown>

function object(value: unknown): UnknownRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as UnknownRecord : {}
}

function first<T>(values: unknown[], parse: (value: unknown) => T | undefined): T | undefined {
  for (const value of values) {
    const parsed = parse(value)
    if (parsed !== undefined) return parsed
  }
  return undefined
}

function objective(value: unknown): ParallelObjective | undefined {
  return value === 'minimum_latency' || value === 'minimum_frequency_with_budget' ? value : undefined
}

function positiveNumberText(value: unknown): string | undefined {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? String(value) : undefined
}

function stringValue(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined
}

function stringArray(value: unknown): string[] | undefined {
  if (!Array.isArray(value) || !value.length || value.some(item => typeof item !== 'string' || !item.trim())) return undefined
  return [...new Set(value.map(item => String(item).trim()))]
}

function counts(value: unknown): number[] | undefined {
  const source = typeof value === 'number' ? [value] : value
  if (!Array.isArray(source) || !source.length || source.some(item => !Number.isInteger(item) || Number(item) < 0)) return undefined
  return [...new Set(source.map(Number))].sort((left, right) => left - right)
}

function participation(value: unknown): MainParticipationControl | undefined {
  const source = typeof value === 'boolean' ? [value] : value
  if (!Array.isArray(source) || !source.length || source.some(item => typeof item !== 'boolean')) return undefined
  const values = new Set(source)
  if (values.has(true) && values.has(false)) return 'both'
  return values.has(true) ? 'yes' : 'no'
}

function frequencyPolicy(value: unknown): 'all_opps' | 'max_opps' | undefined {
  return value === 'all_opps' || value === 'max_opps' ? value : undefined
}

function frequencyMap(value: unknown): Record<string, number[]> | null | undefined {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return undefined
  const entries = Object.entries(value as UnknownRecord)
  if (!entries.length) return null
  const result: Record<string, number[]> = {}
  for (const [core, frequencies] of entries) {
    if (!core.trim() || !Array.isArray(frequencies) || !frequencies.length
        || frequencies.some(item => !Number.isInteger(item) || Number(item) <= 0)) return undefined
    result[core] = [...new Set(frequencies.map(Number))].sort((left, right) => left - right)
  }
  return result
}

/**
 * Restore only controls that are explicitly frozen in a search record.
 * Missing or malformed historical fields are omitted so callers preserve the
 * current control instead of inferring an input from the selected winner.
 */
export function restoreParallelSearchControls(value: unknown): RestoredParallelSearchControls {
  const search = object(value)
  const frozen = object(search.search_configuration)
  const frozenSpace = object(frozen.space)
  const provenance = object(search.search_provenance)
  const provenanceSpace = object(provenance.space)
  const restored: RestoredParallelSearchControls = {}

  const restoredObjective = first([frozen.objective, search.objective], objective)
  const restoredBudget = first([frozen.budget_ms, search.budget_ms], positiveNumberText)
  const restoredCores = first([frozenSpace.available_core_group_ids, provenanceSpace.available_core_group_ids], stringArray)
  const restoredMainCore = first([frozenSpace.main_core_group_id, provenanceSpace.main_core_group_id], stringValue)
  const restoredCounts = first([frozenSpace.worker_counts, provenanceSpace.worker_counts], counts)
  const restoredParticipation = first([frozenSpace.main_participation, provenanceSpace.main_participation], participation)
  const restoredFrequencyPolicy = first([frozenSpace.frequency_policy, provenanceSpace.frequency_policy], frequencyPolicy)
  const restoredCandidates = first([frozen.candidate_ids, search.candidate_ids], stringArray)
  if (restoredObjective !== undefined) restored.objective = restoredObjective
  if (restoredBudget !== undefined) restored.budget = restoredBudget
  if (restoredCores !== undefined) restored.availableCores = restoredCores
  if (restoredMainCore !== undefined) restored.mainCore = restoredMainCore
  if (restoredCounts !== undefined) restored.workerCounts = restoredCounts
  if (restoredParticipation !== undefined) restored.mainParticipation = restoredParticipation
  if (restoredFrequencyPolicy !== undefined) restored.frequencyPolicy = restoredFrequencyPolicy
  if (restoredCandidates !== undefined) restored.candidateIds = restoredCandidates
  const exactSpace = Object.keys(frozenSpace).length ? frozenSpace : provenanceSpace
  if (Object.keys(exactSpace).length) {
    if (Object.prototype.hasOwnProperty.call(exactSpace, 'frequencies_khz')) {
      const restoredFrequencies = frequencyMap(exactSpace.frequencies_khz)
      if (restoredFrequencies !== undefined) restored.frequenciesKHz = restoredFrequencies
    } else restored.frequenciesKHz = null
  }

  if (typeof frozen.robustness === 'boolean') restored.robustness = frozen.robustness
  else if (typeof provenance.robustness_requested === 'boolean') restored.robustness = provenance.robustness_requested
  else if (provenance.robustness_meaning === 'not_requested') restored.robustness = false
  else if (typeof provenance.robustness_meaning === 'string' && provenance.robustness_meaning.trim()) restored.robustness = true

  return restored
}
