import type { Artifact, JsonRecord } from './types'
import { candidateCodeScopes, normalizeCandidateCodeScopes } from './code-scope'

const MODEL_INPUT_CONTEXT_VERSION = 'model_input_context/v8'
const PREVIOUS_MODEL_INPUT_CONTEXT_VERSIONS = new Set(['model_input_context/v7', 'model_input_context/v6', 'model_input_context/v5', 'model_input_context/v4', 'model_input_context/v3', 'model_input_context/v2', 'model_input_context/v1'])
const MAX_MODEL_EVIDENCE_RECORDS = 4

function record(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {}
}

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

/** Historical projections remain readable, but must not be reused for a new run. */
export function hasCurrentModelInputContext(context: Artifact | JsonRecord): boolean {
  return text(record(context.model_input_context).schema_version) === MODEL_INPUT_CONTEXT_VERSION
}

function finiteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function boundedSemanticValue(value: unknown, depth = 0): unknown {
  if (value === null || typeof value === 'boolean' || typeof value === 'number') return value
  if (typeof value === 'string') {
    const normalized = value.trim()
    return normalized.length > 600 ? `${normalized.slice(0, 600)}...` : normalized
  }
  if (Array.isArray(value)) {
    if (depth >= 4) return []
    return value.slice(0, 8).map((item) => boundedSemanticValue(item, depth + 1))
  }
  if (value && typeof value === 'object') {
    if (depth >= 4) return {}
    return Object.fromEntries(
      Object.entries(value as JsonRecord)
        .filter(([, item]) => item !== undefined && item !== null)
        .slice(0, 16)
        .map(([key, item]) => [key, boundedSemanticValue(item, depth + 1)]),
    )
  }
  return String(value).slice(0, 600)
}

function selectFields(source: JsonRecord, names: string[]): JsonRecord {
  return Object.fromEntries(
    names
      .filter((name) => source[name] !== undefined && source[name] !== null)
      .map((name) => [name, boundedSemanticValue(source[name])]),
  )
}

function projectTraceMetrics(value: unknown): JsonRecord {
  const metrics = record(value)
  const projected = selectFields(metrics, [
    'self_time_estimate_total_duration_ms',
    'max_self_time_estimate_duration_ms',
    'inclusive_total_duration_ms',
    'max_span_duration_ms',
    'occurrence_count',
    'observation_window_ms',
    'self_time_share_of_window_percent',
    'inclusive_time_share_of_window_percent',
    'occurrences_per_second',
    'average_self_time_estimate_duration_ms',
    'average_inclusive_duration_ms',
    'self_to_inclusive_duration_percent',
  ])
  const firstStart = finiteNumber(metrics.first_start_ns)
  const lastEnd = finiteNumber(metrics.last_end_ns)
  const totalSelf = finiteNumber(metrics.self_time_estimate_total_duration_ms)
  const totalInclusive = finiteNumber(metrics.inclusive_total_duration_ms)
  const occurrenceCount = finiteNumber(metrics.occurrence_count)
  if (occurrenceCount !== null && occurrenceCount > 0) {
    if (totalSelf !== null) projected.average_self_time_estimate_duration_ms = Number((totalSelf / occurrenceCount).toFixed(6))
    if (totalInclusive !== null) projected.average_inclusive_duration_ms = Number((totalInclusive / occurrenceCount).toFixed(6))
  }
  if (totalSelf !== null && totalInclusive !== null && totalInclusive > 0) {
    projected.self_to_inclusive_duration_percent = Number(((totalSelf / totalInclusive) * 100).toFixed(6))
  }
  if (firstStart === null || lastEnd === null || lastEnd < firstStart) return projected
  const windowMs = (lastEnd - firstStart) / 1_000_000
  if (windowMs <= 0) return projected
  projected.observation_window_ms = Number(windowMs.toFixed(6))
  if (totalSelf !== null) projected.self_time_share_of_window_percent = Number(((totalSelf / windowMs) * 100).toFixed(6))
  if (totalInclusive !== null) projected.inclusive_time_share_of_window_percent = Number(((totalInclusive / windowMs) * 100).toFixed(6))
  if (occurrenceCount !== null) projected.occurrences_per_second = Number((occurrenceCount / (windowMs / 1000)).toFixed(6))
  return projected
}

function countMatches(value: string, expression: RegExp): number {
  return [...value.matchAll(expression)].length
}

function presentSymbols(code: string, symbols: string[]): string[] {
  return symbols.filter((symbol) => new RegExp(`\\b${symbol}\\b`).test(code))
}

function lexicalCodeObservations(code: string): JsonRecord {
  return {
    analysis_method: 'lexical_static_scan/v1',
    loop_construct_count: countMatches(code, /\b(?:for|while)\s*\(/g),
    branch_construct_count: countMatches(code, /\b(?:if|switch)\s*\(/g),
    ipc_transaction_symbols: presentSymbols(code, ['SendTransactCmd', 'SendRequest', 'OnRemoteRequest']),
    parcel_write_call_count: countMatches(code, /\bWrite[A-Za-z0-9_]*\s*\(/g),
    parcel_read_call_count: countMatches(code, /\bRead[A-Za-z0-9_]*\s*\(/g),
    lock_symbols: presentSymbols(code, ['shared_lock', 'unique_lock', 'lock_guard', 'mutex', 'shared_mutex']),
    parallel_runtime_symbols: presentSymbols(code, ['ffrt_submit', 'ffrt_submit_h', 'parallel_for', 'async', 'thread', 'TaskGroup', 'omp']),
    bulk_memory_symbols: presentSymbols(code, ['memcpy', 'memmove', 'memset']),
    math_symbols: presentSymbols(code, ['sin', 'cos', 'sqrt', 'exp', 'log', 'fft']),
  }
}

function projectTargetPlatformProfile(value: unknown): JsonRecord {
  const profile = record(value)
  return {
    ...selectFields(profile, ['schema_version', 'profile_source', 'capability_status']),
    chipset: selectFields(record(profile.chipset), ['family_id', 'family_label', 'model_id', 'model_label']),
    operating_system: selectFields(record(profile.operating_system), ['family_id', 'family_label', 'version_id', 'version_label']),
  }
}

function projectHardwareRouting(value: unknown): JsonRecord {
  const routing = record(value)
  const selectedUnits = Array.isArray(routing.selected_units) ? routing.selected_units : []
  return {
    ...selectFields(routing, ['selection_origin', 'primary_unit_id']),
    selected_units: selectedUnits
      .slice(0, 1)
      .map((item) => selectFields(record(item), ['unit_id']))
      .filter((item) => Object.keys(item).length > 0),
  }
}

function projectHardwareOpportunityEvidence(value: unknown): JsonRecord | null {
  const item = record(value)
  const evidenceId = text(item.evidence_id)
  if (!evidenceId) return null
  return {
    evidence_id: evidenceId,
    ...(typeof item.retrieval_rank === 'number' ? { retrieval_rank: item.retrieval_rank } : {}),
    record: record(item.record),
  }
}

function projectLegacyCpuAstBinding(value: unknown): JsonRecord {
  const profile = record(value)
  const astFeatures = record(profile.ast_features)
  return {
    ...selectFields(profile, ['binding_status', 'matchability']),
    compute_core: selectFields(record(profile.compute_core), ['matching_status', 'status', 'reason']),
    function: selectFields(record(profile.function), ['qualified_name', 'signature', 'source_reference']),
    ast_summary: selectFields(astFeatures, ['parse_status', 'code_metrics', 'loop_count', 'branch_count', 'max_loop_nesting']),
  }
}

function projectLegacyCpuSignature(value: unknown, astValue: unknown): JsonRecord {
  const features = record(value)
  const structured = record(features.structured_features)
  const astFeatures = record(record(astValue).ast_features)
  const memory = record(structured.memory_access)
  const control = record(structured.control_flow)
  const computation = record(structured.computation)
  const parallelism = record(structured.parallelism)
  const algorithmTags = structured.algorithm_tags ?? astFeatures.algorithm_tags ?? []
  const dataTypes = structured.data_type_evidence ?? astFeatures.types_used ?? []
  const projected: JsonRecord = {
    ...selectFields(features, ['availability']),
    ...selectFields(structured, ['binding_status', 'compute_core_status']),
    algorithm_tags: boundedSemanticValue(algorithmTags),
    data_type_evidence: boundedSemanticValue(dataTypes),
    memory_access: Object.keys(memory).length > 0
      ? selectFields(memory, ['classification', 'pointer_expression_count', 'array_subscript_count', 'assignment_count', 'continuity'])
      : boundedSemanticValue(astFeatures.memory_access ?? {}),
    control_flow: Object.keys(control).length > 0
      ? selectFields(control, ['loop_count', 'branch_count', 'max_loop_nesting'])
      : selectFields(astFeatures, ['loop_count', 'branch_count', 'max_loop_nesting']),
    computation: selectFields(computation, ['math_calls', 'notable_callees', 'operators']),
    parallelism: selectFields(parallelism, ['openmp_pragma_count', 'classification']),
  }
  return Object.fromEntries(Object.entries(projected).filter(([, item]) => {
    if (item === null || item === undefined || item === '') return false
    if (Array.isArray(item)) return item.length > 0
    if (typeof item === 'object') return Object.keys(record(item)).length > 0
    return true
  }))
}

function projectEvidenceRecord(value: unknown, rank: number): JsonRecord | null {
  const item = record(value)
  const evidenceId = text(item.evidence_id)
  if (!evidenceId) return null
  const source = record(item.record)
  const astProfile = record(source.cpu_fallback_ast)
  const cpuFeatures = record(source.cpu_code_features)
  const semanticRecord: JsonRecord = selectFields(source, [
    'record_id',
    'record_type',
    'name',
    'algorithm',
    'target_unit',
    'hardware_id',
    'unit_type',
    'confidence',
    'source_group',
    'module',
    'pair_shape',
    'software_interface',
    'capability_scope',
    'device_availability',
    'performance_envelope',
    'supported_algorithms',
    'strict_case_count',
    'strict_case_ids',
    'implementation_paths',
    'constraints',
    'accelerator',
    'cpu_software_fallback',
    'optimization_details',
    'source_capability_summary',
  ])
  if (Object.keys(astProfile).length > 0) semanticRecord.cpu_ast_binding = projectLegacyCpuAstBinding(astProfile)
  const cpuSignature = projectLegacyCpuSignature(cpuFeatures, astProfile)
  if (Object.keys(cpuSignature).length > 0) semanticRecord.cpu_signature = cpuSignature
  return {
    evidence_id: evidenceId,
    retrieval_rank: rank,
    retrieval_match_terms: Array.isArray(item.match_terms) ? item.match_terms.slice(0, 8).map(text).filter(Boolean) : [],
    record: semanticRecord,
  }
}

function enrichModelInputContext(value: JsonRecord): JsonRecord {
  const hotspot = record(value.hotspot)
  const code = text(value.code)
  const hardwareOpportunity = text(value.strategy_id) === 'hardware_kb'
  const contextRecords = Array.isArray(value.context_records) ? value.context_records : []
  return {
    ...value,
    hotspot: {
      ...hotspot,
      trace_metrics: projectTraceMetrics(hotspot.trace_metrics),
    },
    code_observations: lexicalCodeObservations(code),
    candidate_scope_method: 'lexical_scope_selection/v1',
    candidate_scopes: normalizeCandidateCodeScopes(value.candidate_scopes, code),
    ...(hardwareOpportunity ? {
      context_records: contextRecords
        .map(projectHardwareOpportunityEvidence)
        .filter((item): item is JsonRecord => Boolean(item)),
    } : {}),
    ...(Object.keys(record(value.hardware_routing)).length > 0 ? { hardware_routing: projectHardwareRouting(value.hardware_routing) } : {}),
  }
}

/**
 * Return the versioned semantic projection intended for model inference.
 *
 * New contexts are generated by the backend. The fallback keeps historical
 * contexts usable without accidentally serializing the audit artifact envelope
 * into a provider request.
 */
export function reasoningModelInputContext(context: Artifact | JsonRecord): JsonRecord {
  const supplied = record(context.model_input_context)
  if (
    (text(supplied.schema_version) === MODEL_INPUT_CONTEXT_VERSION || PREVIOUS_MODEL_INPUT_CONTEXT_VERSIONS.has(text(supplied.schema_version)))
    && typeof supplied.code === 'string'
  ) return enrichModelInputContext(supplied)

  const hotspot = record(context.hotspot)
  const source = record(context.source)
  const allRecords = Array.isArray(context.context_records) ? context.context_records : []
  const contextRecords = allRecords
    .slice(0, MAX_MODEL_EVIDENCE_RECORDS)
    .map((item, index) => projectEvidenceRecord(item, index + 1))
    .filter((item): item is JsonRecord => Boolean(item))
  const traceEventId = text(hotspot.event_id)
  const sourceSha = text(source.sha256)
  const allowedEvidenceIds = [
    ...contextRecords.map((item) => text(item.evidence_id)).filter(Boolean),
    ...(traceEventId ? [`trace:${traceEventId}`] : []),
    ...(sourceSha ? [`source:${sourceSha}`] : []),
  ]
  const retrieval = record(context.retrieval)
  return enrichModelInputContext({
    schema_version: 'model_input_context/legacy_projection_v1',
    strategy_id: text(context.strategy_id),
    hotspot: {
      ...selectFields(hotspot, ['event_name', 'module', 'critical_path_status']),
      trace_metrics: projectTraceMetrics(hotspot.trace_metrics),
    },
    source: selectFields(source, ['origin', 'path', 'line_start', 'line_end', 'binding_level']),
    function_hint: text(context.function_hint),
    code_language: text(context.code_language) || 'cpp',
    code: text(context.code),
    candidate_scope_method: 'lexical_scope_selection/v1',
    candidate_scopes: candidateCodeScopes(text(context.code)),
    target_platform_profile: projectTargetPlatformProfile(context.target_platform_profile),
    retrieval: {
      method: text(retrieval.method) || 'none',
      matched_evidence_count: allRecords.length,
      provided_evidence_count: contextRecords.length,
      truncated: retrieval.truncated === true,
    },
    ...(Object.keys(record(context.hardware_routing)).length > 0 ? { hardware_routing: projectHardwareRouting(context.hardware_routing) } : {}),
    context_records: contextRecords,
    allowed_evidence_ids: allowedEvidenceIds,
  })
}

/** Keep the page assistant focused on actionable state rather than audit data. */
export function assistantPageContext(context: Record<string, unknown>): JsonRecord {
  const reasoningLab = record(context.reasoning_lab)
  const hotspots = Array.isArray(reasoningLab.hotspots) ? reasoningLab.hotspots : []
  const compactHotspots = hotspots.slice(0, 30).map((item) => selectFields(record(item), [
    'rank',
    'module_rank',
    'event_id',
    'event_name',
    'module',
    'self_time_ms',
    'max_self_time_ms',
    'occurrence_count',
  ]))
  const trace = record(reasoningLab.trace)
  const codeUnit = record(reasoningLab.code_unit)
  const hardwareRouting = record(reasoningLab.hardware_routing)
  const strategyResults = Array.isArray(reasoningLab.strategy_results) ? reasoningLab.strategy_results : []
  const workflow = record(context.guided_optimization_workflow)
  const activeTrace = record(context.active_trace)
  const sourceConnectors = Array.isArray(context.source_connectors) ? context.source_connectors : []
  const customApiKnowledgeBases = Array.isArray(context.custom_api_knowledge_bases) ? context.custom_api_knowledge_bases : []
  return {
    schema_version: 'page_assistant_context/v2',
    active_view: text(context.active_view),
    ...(context.active_view === 'scenario_capture' ? {
      scenario_capture: selectFields(record(context.scenario_capture), [
        'page_id', 'plan_id', 'plan_draft', 'selected_step_ids', 'selected_device',
        'connection_mode', 'bridge_id', 'device_serial', 'scenario_capture_target', 'job_id', 'run_id',
      ]),
    } : {}),
    project: selectFields(record(context), ['project_id', 'source_connector_id']),
    source_connectors: sourceConnectors.slice(0, 20).map((item) => selectFields(record(item), [
      'source_connector_id',
      'display_name',
      'source_root',
      'source_snapshot_id',
    ])),
    custom_api_knowledge_bases: customApiKnowledgeBases.slice(0, 30).map((item) => selectFields(record(item), [
      'kb_id',
      'display_name',
      'accelerator_unit',
      'record_count',
    ])),
    target_platform_profile: projectTargetPlatformProfile(context.target_platform_profile),
    workflow: selectFields(workflow, ['stage', 'detail', 'revision']),
    active_trace: selectFields(activeTrace, ['artifact_id', 'binding']),
    selected_trace_state: selectFields(record(context), ['selected_cluster_id', 'selected_event_id']),
    reasoning_lab: {
      analysis_record_id: text(reasoningLab.analysis_record_id) || null,
      trace: selectFields(trace, ['artifact_id', 'label', 'input_kind']),
      ...selectFields(reasoningLab, ['hotspot_limit', 'hotspot_module_count']),
      hotspots: compactHotspots,
      code_unit: selectFields(codeUnit, ['artifact_id', 'event_name', 'source_origin', 'source_path', 'source_binding', 'code_preview']),
      hardware_routing: selectFields(hardwareRouting, ['status', 'result_artifact_id', 'selection_origin', 'decision', 'selected_units']),
      strategy_results: strategyResults.slice(0, 3).map((item) => selectFields(record(item), [
        'strategy_id',
        'status',
        'context_artifact_id',
        'result_artifact_id',
        'recommendation',
        'summary',
      ])),
    },
  }
}
