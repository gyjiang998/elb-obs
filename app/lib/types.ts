export type JsonRecord = Record<string, unknown>

export interface SourceBinding {
  declared_revision: string | null
  resolved_snapshot_id: string | null
  match_level: 'exact' | 'compatible' | 'unknown' | 'mismatch' | string
  resolver_version: string
  snapshot_state?: 'clean' | 'dirty' | string
  worktree_fingerprint_algorithm?: string
  worktree_fingerprint_sha256?: string
  worktree_changed_path_count?: number
  worktree_dirty_repository_count?: number
  source_snapshot_artifact_id?: string
}

export interface Artifact extends JsonRecord {
  artifact_id: string
  artifact_type: string
  content_sha256: string
  source_connector_id: string
  source_binding: SourceBinding
  clusters?: TraceCluster[]
  hotspot_candidates?: HotspotCandidate[]
  quality?: JsonRecord
  input_kind?: 'external_trace_analysis_report' | 'raw_htrace' | 'sys_trace_experimental' | string
  analysis_profile?: string
  raw_htrace?: JsonRecord
  selected_cluster?: TraceCluster
  state?: string
}

export interface TraceCluster extends JsonRecord {
  cluster_id: string
  selection_state: 'pending' | 'selected' | 'rejected' | string
  edge_summary: JsonRecord
  time_range_ns: JsonRecord
  module_summary?: JsonRecord
  thread_refs?: JsonRecord[]
  experimental?: boolean
}

export interface HotspotCandidate extends JsonRecord {
  hotspot_id: string
  cluster_id: string
  temporal_pressure: string
  dominance_label: string
  function_ref?: JsonRecord
  active_time_metrics: JsonRecord
}

export interface TraceEventCandidate extends JsonRecord {
  event_id: string
  event_name: string
  event_kind: string
  rank: number
  metrics: JsonRecord
  source_location?: JsonRecord | null
  source_resolution?: string
  hitrace_marker?: JsonRecord
}

export interface Connector extends JsonRecord {
  connector_id: string
  display_name: string
  source_root: string
  kind: string
  mode: string
  snapshot: { snapshot_id: string; clean: boolean; repositories: JsonRecord[] }
  worktree_fingerprint?: {
    algorithm: string
    sha256: string
    dirty_repository_count: number
    changed_path_count: number
  }
  remote_url?: string | null
  pinned_commit?: string | null
}

export interface Fixture extends JsonRecord {
  fixture_id: string
  label: string
  description: string
  kind: string
  bytes: number
  sha256: string
}

export interface CaseRecord extends JsonRecord {
  case_id: string
  title: string
  algorithm_hypothesis: string
  target_hypothesis: string
  lifecycle: string
  source_evidence: Array<{ path: string; line: number; sha256?: string | null }>
}

export interface PageAction extends JsonRecord {
  action_id: string
  risk_level: 'read' | 'write_local' | 'resource_heavy' | 'destructive' | string
  input_schema: JsonRecord
}

export interface KnowledgeBaseMetadata {
  kb_id: string
  version: string
  source_revision: string
  content_sha256: string
  summary: JsonRecord
}

export interface DeploymentCapabilities {
  mode: 'device' | 'analysis'
  device_validation_enabled: boolean
  device_validation_message: string
}

export interface Overview {
  project: { project_id: string; mode: string }
  deployment?: DeploymentCapabilities
  default_connector: Connector | null
  default_connector_status?: JsonRecord
  connectors: Connector[]
  optimization_kb: KnowledgeBaseMetadata
  hardware_kb: KnowledgeBaseMetadata
  custom_api_knowledge_bases?: JsonRecord[]
  application_flow_assets?: JsonRecord
  // Legacy compatibility field; new UI paths use optimization_kb directly.
  kb: { kb_id: string; version: string; source_revision: string; manifest_sha256: string }
  case_catalog: { catalog_id: string; case_count: number; content_sha256: string }
  fixtures: Fixture[]
  artifact_store: { artifact_count: number; filesystem_free_bytes: number }
  agent_mode: {
    runtime_location: string
    selected_engine: string
    session_owner: string
    task_and_evidence_owner: string
    browser_role: string
    model_key_storage: string
    server_receives_model_key: boolean | string
    legacy_model_relay_allowed_base_urls: string[]
  }
}
