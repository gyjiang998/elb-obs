import { api } from './api'
import type { BackendAgentRun } from './backend-agent-runtime'
import type { TraceAnalysisRecord } from './analysis-history'
import type { OptimizationWorkflowState } from './optimization-workflow'
import type { TargetPlatformProfile } from './target-platform-profile'

export type PersistedOptimizationWorkflow = OptimizationWorkflowState

export type AnalysisWorkspaceSnapshotPayload = {
  schema_version: 'analysis_workbench_state/v1'
  source_connector_id: string
  analysis_record: TraceAnalysisRecord
  workflow: PersistedOptimizationWorkflow
  reasoning: Record<string, unknown>
  target_platform_profile: TargetPlatformProfile
  device_validation_snapshot: Record<string, unknown> | null
  selected_skill_profile_id: string
  saved_by: 'browser'
}

export type AnalysisWorkspaceSnapshot = {
  schema_version: 'analysis_workspace_snapshot/v1'
  analysis_id: string
  revision: number
  created_at: string
  updated_at: string
  snapshot: AnalysisWorkspaceSnapshotPayload
  agent_session?: {
    bound: boolean
    run: BackendAgentRun | null
  }
}

export type AnalysisWorkspaceCatalogRecord = {
  analysis_id: string
  revision: number
  created_at: string
  updated_at: string
  analysis_record: TraceAnalysisRecord
  workflow_stage: string | null
}

export async function listAnalysisWorkspaces(limit = 40): Promise<AnalysisWorkspaceCatalogRecord[]> {
  const response = await api<{ records: AnalysisWorkspaceCatalogRecord[] }>(
    `/v1/projects/local-default/analysis-workspaces?limit=${encodeURIComponent(String(limit))}`,
    { cache: 'no-store' },
  )
  return Array.isArray(response.records) ? response.records : []
}

export async function saveAnalysisWorkspace(
  analysisId: string,
  snapshot: AnalysisWorkspaceSnapshotPayload,
): Promise<AnalysisWorkspaceSnapshot> {
  return api(`/v1/projects/local-default/analysis-workspaces/${encodeURIComponent(analysisId)}`, {
    method: 'PUT',
    body: JSON.stringify({ snapshot }),
  })
}

export async function readAnalysisWorkspace(analysisId: string): Promise<AnalysisWorkspaceSnapshot> {
  return api(`/v1/projects/local-default/analysis-workspaces/${encodeURIComponent(analysisId)}`, { cache: 'no-store' })
}
