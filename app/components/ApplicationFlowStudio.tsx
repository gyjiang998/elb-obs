'use client'

import { ArrowDownToLine, BarChart3, Check, CircleAlert, Database, FileJson2, GitBranch, History, LoaderCircle, MessageSquareText, Network, PencilLine, Plus, RefreshCw, Route, Search, Sparkles, Trash2, Upload, X } from 'lucide-react'
import { type ChangeEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, apiBase, formatBytes, uploadWithProgress } from '../lib/api'
import { agentAutonomousTaskStatusEventName } from '../lib/agent-command-bridge'
import { randomUuid } from '../lib/random-id'
import type { Artifact, Connector, JsonRecord } from '../lib/types'
import { MermaidDiagram } from './MermaidDiagram'
import { TraceStageTimeline } from './TraceStageTimeline'
import { useWorkbenchErrorNotification } from './WorkbenchErrorNotification'

type FlowSummary = {
  id: string
  name: string
  order: number
  description: string
  confidence: 'high' | 'medium' | 'low' | string
}

type DiscoverySummary = {
  artifact_id: string
  application_flow_analysis_id: string
  created_at: string
  source_connector_id: string
  source_snapshot_id: string
  scope: string
  source_scopes?: JsonRecord[]
  analysis_goal?: string
  code_graph_artifact_id?: string
  parent_discovery_artifact_id?: string | null
  refinement_request?: string
  interaction_iteration?: number
  trace_analysis_artifact_id?: string | null
  flow_count: number
  flows: FlowSummary[]
  quality: JsonRecord
}

type ScenarioSummary = {
  artifact_id: string
  application_flow_analysis_id: string
  created_at: string
  source_connector_id: string
  source_scopes?: JsonRecord[]
  discovery_artifact_id: string
  flow_id: string
  flow_name: string
  scenario: string
  stage_count: number
  revision: number
  parent_scenario_artifact_id?: string | null
  revision_request?: string
  has_runtime_trace_alignment?: boolean
  has_stage_analysis?: boolean
  runtime_trace_analysis_artifact_id?: string | null
  quality: JsonRecord
}

type FlowCatalog = {
  schema_version: string
  schemas: JsonRecord
  preferences?: {
    schema_version: string
    confirm_flow_before_trace: boolean
    updated_at?: string | null
  }
  discoveries: DiscoverySummary[]
  scenarios: ScenarioSummary[]
}

type CodeGraph = {
  graph_id: string
  status: 'queued' | 'running' | 'ready' | 'failed' | 'interrupted' | string
  source_connector_id: string
  source_snapshot_id: string
  scope: string
  created_at: string
  artifact_id?: string
  summary?: JsonRecord
  progress?: JsonRecord
  error?: JsonRecord
}

type CodeGraphCatalog = {
  schema_version: string
  extractor_available: boolean
  graphs: CodeGraph[]
  count: number
}

type SourceEvidence = {
  id: string
  path: string
  line_start: number
  line_end: number
  symbol: string
  evidence_kind: string
  claim: string
}

type DiscoveredFlow = FlowSummary & {
  trigger: string
  completion: string
  participants: string[]
  entry_points: JsonRecord[]
  exit_points: JsonRecord[]
  source_evidence: SourceEvidence[]
  trace_anchor_candidates: JsonRecord[]
  open_questions: string[]
}

type DiscoveryArtifact = {
  artifact_id: string
  application_flow_analysis_id: string
  source_connector_id: string
  analysis_context?: JsonRecord
  discovery: {
    source_snapshot_id: string
    scope: string
    analysis_summary: JsonRecord
    flows: DiscoveredFlow[]
    unknowns: string[]
  }
  quality: JsonRecord
}

type ScenarioArtifact = {
  artifact_id: string
  application_flow_analysis_id: string
  created_at?: string
  source_connector_id?: string
  source_scopes?: JsonRecord[]
  discovery_artifact_id: string
  flow_id: string
  flow_name: string
  revision?: number
  parent_scenario_artifact_id?: string | null
  revision_request?: string
  scenario_json: JsonRecord & { stages?: JsonRecord[]; nodes?: JsonRecord[] }
  mermaid: string
  stage_hierarchy?: JsonRecord
  stage_evidence: JsonRecord[]
  boundary_analysis?: JsonRecord[]
  runtime_trace_alignment?: JsonRecord
  stage_analysis?: JsonRecord
  quality: JsonRecord
  unknowns: string[]
}

type RuntimeTraceSummary = {
  artifact_id: string
  created_at: string
  uploaded_at?: string
  import_instance_id?: string
  trace_name?: string
  input_kind: string
  analysis_profile?: string
  source_connector_id: string
  stage_count: number
  hotspot_count: number
  quality_status?: string
}

type RuntimeTraceCatalog = {
  schema_version: string
  traces: RuntimeTraceSummary[]
  count: number
}

export type CaptureRuntimeTraceSource = {
  traceId: string
  runId: string
  stepId: string
  title: string
  planTitle?: string
  stepOrdinal?: number | null
  stepTitle: string
  status?: string
  registeredAt?: string
  ready: boolean
  reason?: string
}

function runtimeTraceSummaryFromArtifact(artifact: Artifact | null | undefined): RuntimeTraceSummary | null {
  if (!artifact || artifact.artifact_type !== 'trace_analysis_result') return null
  const inputKind = text(artifact.input_kind)
  if (!['raw_htrace', 'external_trace_analysis_report'].includes(inputKind)) return null
  const quality = record(artifact.quality)
  const traceImport = record(artifact.trace_import)
  return {
    artifact_id: artifact.artifact_id,
    created_at: text(artifact.created_at),
    uploaded_at: text(traceImport.uploaded_at, text(artifact.created_at)),
    import_instance_id: text(traceImport.import_instance_id),
    trace_name: text(artifact.trace_name),
    input_kind: inputKind,
    analysis_profile: text(artifact.analysis_profile),
    source_connector_id: artifact.source_connector_id,
    stage_count: Array.isArray(artifact.stage_catalog) ? artifact.stage_catalog.length : 0,
    hotspot_count: Array.isArray(artifact.hotspot_candidates) ? artifact.hotspot_candidates.length : 0,
    quality_status: text(quality.status),
  }
}

function runtimeTraceTaskInstruction(trace: RuntimeTraceSummary | undefined, artifactId: string): string {
  if (!artifactId) {
    return '本轮未选择真实 Trace；交付件基于源码证据生成，并把运行时边界保留为待校准状态。'
  }
  const traceName = trace?.trace_name || artifactId
  const traceKind = trace?.input_kind === 'raw_htrace' ? '原始 .htrace' : '阶段分析 JSON'
  return `后端已确认并把真实 Trace“${traceName}”（analysis_artifact_id=${artifactId}，类型=${traceKind}）绑定到本次应用流程分析。生成场景后直接使用 application_flow_inspect_runtime_trace 读取真实事件目录与时序；该工具可按本次分析自动解析已绑定 Trace。以完整 event_name 校准 stage 与可观察 activity 边界，再用 application_flow_apply_runtime_trace_alignment 的 stage_matches 和 node_matches 保存匹配结果；平台按 parent_id 聚合父级时间包络。最终结论说明该 Trace 的使用情况与各层节点匹配状态。`
}

function flowStructureTaskInstruction(): string {
  return '本轮以流程结构确认为交付边界：基于用户操作和源码完成覆盖完整操作的细粒度分层 Mermaid，并通过平台工具发布 Mermaid 与标准 application-flow-scenario/v2 JSON。发布后概括动作边界、阶段拆分、关键调用和源码缺口，请用户确认流程结构；HiTrace 边界核对、真实 Trace 校准和分析预览留待用户确认后的下一轮。'
}

type BusyPhase = 'building' | 'generating' | 'importing-trace' | 'loading' | null

type PendingFlowAnalysis = {
  requestId: string
  applicationFlowAnalysisId: string
  sourceConnectorId: string
  analysisGoal: string
  createdAt: string
  scope: string
  expectsRuntime: boolean
  status: 'submitting' | 'running' | 'failed'
  detail: string
}

type RelatedSourceScope = {
  sourceConnectorId: string
  subpath: string
}

export type ApplicationFlowAssistantContext = {
  page_id: 'application_flow'
  source_connector_id: string
  source_snapshot_id: string
  subpath: string
  source_scopes: JsonRecord[]
  graph_id: string | null
  application_flow_analysis_id: string | null
  discovery_artifact_id: string | null
  flow_id: string | null
  flow_name: string | null
  scenario_artifact_id: string | null
  scenario_revision: number | null
  trace_analysis_artifact_id: string | null
}

function record(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {}
}

function records(value: unknown): JsonRecord[] {
  return Array.isArray(value) ? value.map(record).filter((item) => Object.keys(item).length > 0) : []
}

function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' && value.trim() ? value : fallback
}

function number(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function durationLabel(value: unknown): string {
  const milliseconds = number(value, Number.NaN)
  if (!Number.isFinite(milliseconds)) return '未形成时间包络'
  if (milliseconds >= 1000) return `${(milliseconds / 1000).toFixed(2)} s`
  return `${milliseconds.toFixed(milliseconds >= 10 ? 2 : 3)} ms`
}

function list(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string' && item.length > 0) : []
}

function scenarioLineage(scenarios: ScenarioSummary[], discoveryArtifactId: string, flowId: string): ScenarioSummary[] {
  if (!discoveryArtifactId || !flowId) return []
  const analysisId = scenarios.find((item) => item.discovery_artifact_id === discoveryArtifactId)?.application_flow_analysis_id ?? ''
  const flowScenarios = scenarios.filter((item) => (
    (!flowId || item.flow_id === flowId)
    && (!analysisId || item.application_flow_analysis_id === analysisId)
  ))
  const lineageIds = new Set(
    flowScenarios
      .filter((item) => item.discovery_artifact_id === discoveryArtifactId)
      .map((item) => item.artifact_id),
  )
  if (lineageIds.size === 0) return []

  let changed = true
  while (changed) {
    changed = false
    for (const item of flowScenarios) {
      const parentId = item.parent_scenario_artifact_id ?? ''
      if (lineageIds.has(item.artifact_id) && parentId && !lineageIds.has(parentId)) {
        lineageIds.add(parentId)
        changed = true
      }
      if (parentId && lineageIds.has(parentId) && !lineageIds.has(item.artifact_id)) {
        lineageIds.add(item.artifact_id)
        changed = true
      }
    }
  }

  return flowScenarios
    .filter((item) => lineageIds.has(item.artifact_id))
    .sort((left, right) => {
      const createdDifference = Date.parse(right.created_at) - Date.parse(left.created_at)
      return Number.isFinite(createdDifference) && createdDifference !== 0
        ? createdDifference
        : right.artifact_id.localeCompare(left.artifact_id)
    })
}

function scenarioArtifactLineage(scenarios: ScenarioSummary[], artifactId: string): ScenarioSummary[] {
  const seed = scenarios.find((item) => item.artifact_id === artifactId)
  if (!seed) return []
  const lineageIds = new Set([artifactId])
  let changed = true
  while (changed) {
    changed = false
    for (const item of scenarios) {
      if (item.flow_id !== seed.flow_id) continue
      if (item.application_flow_analysis_id !== seed.application_flow_analysis_id) continue
      const parentId = item.parent_scenario_artifact_id ?? ''
      if (lineageIds.has(item.artifact_id) && parentId && !lineageIds.has(parentId)) {
        lineageIds.add(parentId)
        changed = true
      }
      if (parentId && lineageIds.has(parentId) && !lineageIds.has(item.artifact_id)) {
        lineageIds.add(item.artifact_id)
        changed = true
      }
    }
  }
  return scenarios
    .filter((item) => lineageIds.has(item.artifact_id))
    .sort((left, right) => {
      const createdDifference = Date.parse(right.created_at) - Date.parse(left.created_at)
      return Number.isFinite(createdDifference) && createdDifference !== 0
        ? createdDifference
        : right.artifact_id.localeCompare(left.artifact_id)
    })
}

function discoveryScenarioLineage(scenarios: ScenarioSummary[], discovery: DiscoverySummary): ScenarioSummary[] {
  const flowIds = new Set(discovery.flows.map((flow) => flow.id))
  const analysisScenarios = scenarios.filter((scenario) => (
    scenario.application_flow_analysis_id === discovery.application_flow_analysis_id
    && flowIds.has(scenario.flow_id)
  ))
  if (analysisScenarios.length > 0) {
    return analysisScenarios.sort((left, right) => {
      const createdDifference = Date.parse(right.created_at) - Date.parse(left.created_at)
      return Number.isFinite(createdDifference) && createdDifference !== 0
        ? createdDifference
        : right.artifact_id.localeCompare(left.artifact_id)
    })
  }
  const byArtifactId = new Map<string, ScenarioSummary>()
  for (const flow of discovery.flows) {
    for (const scenario of scenarioLineage(scenarios, discovery.artifact_id, flow.id)) {
      byArtifactId.set(scenario.artifact_id, scenario)
    }
  }
  return [...byArtifactId.values()].sort((left, right) => {
    const createdDifference = Date.parse(right.created_at) - Date.parse(left.created_at)
    return Number.isFinite(createdDifference) && createdDifference !== 0
      ? createdDifference
      : right.artifact_id.localeCompare(left.artifact_id)
  })
}

function qualityLabel(value: unknown, evidenceScope?: unknown): string {
  if (value === 'ready' && evidenceScope === 'source_declared_only') return '源码锚点已闭合'
  if (value === 'ready' && evidenceScope === 'runtime_observed') return '真实 Trace 已闭合'
  if (value === 'ready' && evidenceScope === 'mixed_source_runtime') return '源码与 Trace 已闭合'
  if (value === 'ready') return '旧版锚点状态'
  if (value === 'draft_needs_trace_evidence') return '存在未闭合锚点'
  if (value === 'pass') return '契约通过'
  if (value === 'partial') return '部分就绪'
  return text(value, '待分析')
}

function anchorStatusLabel(value: unknown, sourceCalibrated = true): string {
  if (value === 'observed_in_source' && sourceCalibrated) return '源码已确认'
  if (value === 'observed_in_source') return '旧版未逐行校验'
  if (value === 'inferred_needs_trace') return '源码推断'
  if (value === 'user_provided') return '用户提供'
  if (value === 'unavailable') return '未找到'
  return text(value, '未校准')
}

function locatorLabel(stage: JsonRecord, boundary: 'start' | 'end'): string {
  const locator = record(stage[boundary])
  if (locator.type === 'trace_point') return text(locator.name, '未命名事件')
  if (locator.type === 'relation') return `${text(locator.relation, 'relation')} · ${text(locator.direction)}`
  return stage.matcher ? `matcher · ${text(record(stage.matcher).type)}` : '未绑定'
}

function boundaryAnchor(evidence: JsonRecord, boundary: 'start' | 'end'): JsonRecord {
  const anchors = Array.isArray(evidence.boundary_anchors) ? evidence.boundary_anchors.map(record) : []
  return anchors.find((item) => item.boundary === boundary) ?? {}
}

function sourceAnchorDetail(anchor: JsonRecord): string {
  const location = record(anchor.source_location)
  const instrumentation = record(anchor.instrumentation)
  const path = text(location.path)
  const line = number(location.line)
  const macro = text(instrumentation.macro)
  const matchKind = text(instrumentation.event_label_match)
  return [macro, matchKind, path && line ? `${path}:${line}` : path].filter(Boolean).join(' · ')
}

function anchorAnalysisLabel(value: unknown): string {
  if (value === 'complete') return 'Start / End 已闭合'
  if (value === 'partial') return '部分锚点'
  if (value === 'unavailable') return '未找到'
  return '待识别'
}

function runtimeMatchLabel(value: unknown): string {
  if (value === 'matched') return '真实 Trace 已匹配'
  if (value === 'partial') return '真实 Trace 部分匹配'
  if (value === 'unmatched') return '真实 Trace 未匹配'
  return '尚未进行 Trace 校准'
}

function runtimeEventDetail(value: JsonRecord): string {
  const context = record(value.trace_context)
  const metrics = record(value.metrics)
  const process = text(context.process_name)
  const thread = text(context.thread_name)
  const count = number(metrics.occurrence_count)
  return [process, thread, count ? `${count} 次` : ''].filter((item) => item && item !== 'unknown').join(' · ')
}

function formatTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(date)
}

function formatTraceUploadTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(date)
}

function normalizeScope(value: string): string {
  return value.trim().replaceAll('\\', '/').replace(/^\.\//, '').replace(/\/$/, '')
}

function graphStatusLabel(value: string): string {
  if (value === 'ready') return '可用于分析'
  if (value === 'queued') return '等待构建'
  if (value === 'running') return '正在构建'
  if (value === 'failed') return '构建失败'
  if (value === 'interrupted') return '构建已中断'
  return value || '尚未构建'
}

function candidateBlueprint(context: JsonRecord | undefined, flowId: string): JsonRecord {
  const values = Array.isArray(context?.candidate_blueprints) ? context.candidate_blueprints.map(record) : []
  return values.find((item) => text(item.flow_id) === flowId) ?? {}
}

function normalizedStageHierarchy(value: JsonRecord, stages: JsonRecord[], flowName: string): JsonRecord {
  if (Array.isArray(value.actions) && value.actions.length > 0) return value
  if (!stages.length) return value
  const actionId = 'action_legacy'
  const phases = stages.map((stage, index) => ({
    id: `phase_${String(index + 1).padStart(2, '0')}`,
    action_id: actionId,
    order: index + 1,
    name: text(stage.name, `阶段 ${index + 1}`),
    description: '旧版工件未保存业务阶段说明。',
    stage_ids: [text(stage.id)],
  }))
  return {
    schema_version: 'application_flow_stage_hierarchy/v1',
    representation: 'legacy_flat_projection',
    actions: [{ id: actionId, order: 1, name: flowName || '既有流程', description: '由旧版有序 Stage 兼容投影。', phase_ids: phases.map((item) => item.id) }],
    phases,
    stage_bindings: phases.map((phase) => ({ stage_id: phase.stage_ids[0], action_id: actionId, phase_id: phase.id })),
    activities: [],
    activity_relations: [],
    implementations: [],
    quality: { status: 'legacy_flat', action_count: 1, phase_count: phases.length, anchorable_stage_count: stages.length, activity_count: 0, activity_relation_count: 0, implementation_count: 0 },
  }
}

function activityRelationLabel(value: unknown): string {
  const labels: Record<string, string> = {
    sequence: '串行',
    data_dependency: '数据依赖',
    control_dependency: '控制依赖',
    async_dispatch: '异步派发',
    parallel: '并行',
    parallel_candidate: '可并行候选',
    join_all: '全部汇合',
    join_any: '任一汇合',
    fallback: '回退路径',
  }
  const key = text(value)
  return labels[key] ?? key
}

function StageHierarchyView({ hierarchy, stages }: { hierarchy: JsonRecord; stages: JsonRecord[] }) {
  const actions = Array.isArray(hierarchy.actions) ? hierarchy.actions.map(record).sort((a, b) => number(a.order) - number(b.order)) : []
  const phases = Array.isArray(hierarchy.phases) ? hierarchy.phases.map(record).sort((a, b) => number(a.order) - number(b.order)) : []
  const bindings = Array.isArray(hierarchy.stage_bindings) ? hierarchy.stage_bindings.map(record) : []
  const activities = Array.isArray(hierarchy.activities) ? hierarchy.activities.map(record) : []
  const activityRelations = Array.isArray(hierarchy.activity_relations) ? hierarchy.activity_relations.map(record) : []
  const implementations = Array.isArray(hierarchy.implementations) ? hierarchy.implementations.map(record) : []
  const bindingByStage = new Map(bindings.map((item) => [text(item.stage_id), item]))
  const activityById = new Map(activities.map((item) => [text(item.id), item]))

  if (!actions.length) return <div className="application-flow-empty output"><Network size={24} /><strong>当前工件没有分层信息</strong><span>旧版流程仍可在流程图与 JSON 中查看；重新生成后会保存动作、阶段和函数实现层。</span></div>

  return <div className="application-flow-hierarchy" data-representation={text(hierarchy.representation)}>
    {actions.map((action) => {
      const actionId = text(action.id)
      const actionPhases = phases.filter((phase) => text(phase.action_id) === actionId)
      return <section className="application-flow-hierarchy-action" key={actionId}>
        <header><span>{number(action.order)}</span><div><strong>{text(action.name)}</strong><p>{text(action.description)}</p></div></header>
        <div className="application-flow-hierarchy-phases">
          {actionPhases.map((phase) => {
            const phaseId = text(phase.id)
            const phaseStages = stages.filter((stage) => text(bindingByStage.get(text(stage.id))?.phase_id) === phaseId)
            return <div className="application-flow-hierarchy-phase" key={phaseId}>
              <div className="application-flow-hierarchy-phase-title"><span>业务阶段 {number(phase.order)}</span><strong>{text(phase.name)}</strong><p>{text(phase.description)}</p></div>
              <div className="application-flow-hierarchy-stages">
                {phaseStages.map((stage) => {
                  const stageId = text(stage.id)
                  const stageImplementations = implementations
                    .filter((item) => text(item.stage_id) === stageId)
                    .sort((a, b) => number(a.order) - number(b.order))
                  const stageActivities = activities
                    .filter((item) => text(item.stage_id) === stageId)
                    .sort((a, b) => number(a.order) - number(b.order))
                  const stageActivityIds = new Set(stageActivities.map((item) => text(item.id)))
                  const stageRelations = activityRelations.filter((item) => (
                    stageActivityIds.has(text(item.from)) && stageActivityIds.has(text(item.to))
                  ))
                  const stageLevelImplementations = stageImplementations.filter((item) => !text(item.activity_id))
                  return <div className="application-flow-hierarchy-stage" key={stageId}>
                    <div className="application-flow-hierarchy-stage-title"><span>可锚定子阶段 {number(stage.order)}</span><strong>{text(stage.name)}</strong><code>{stageId} · {text(stage.process_name)}</code></div>
                    <div className="application-flow-hierarchy-stage-body">
                      {stageActivities.length > 0 && <div className="application-flow-activity-graph">
                        <header><strong>细粒度流程</strong><span>{stageActivities.length} 个活动 · {stageRelations.length} 条关系</span></header>
                        <div className="application-flow-activity-list">
                          {stageActivities.map((activity) => {
                            const activityId = text(activity.id)
                            const activityImplementations = stageImplementations.filter((item) => text(item.activity_id) === activityId)
                            const outgoing = stageRelations.filter((item) => text(item.from) === activityId)
                            return <section className="application-flow-activity" key={activityId}>
                              <div className="application-flow-activity-heading"><span>{number(activity.order)}</span><div><strong>{text(activity.name)}</strong><p>{text(activity.description)}</p><code>{activityId}</code></div></div>
                              {outgoing.length > 0 && <div className="application-flow-activity-relations">{outgoing.map((relation, index) => <span key={`${activityId}-${text(relation.to)}-${text(relation.type)}-${index}`}><b>{activityRelationLabel(relation.type)}</b><code>→ {text(activityById.get(text(relation.to))?.name, text(relation.to))}</code>{text(relation.description) && <small>{text(relation.description)}</small>}</span>)}</div>}
                              {activityImplementations.length > 0 && <ol>{activityImplementations.map((item) => <li key={text(item.id, `${stageId}-${activityId}-${number(item.order)}`)}><span>{text(item.relation)}</span><strong>{text(item.symbol)}</strong><code>{text(item.path)}:{number(item.line)}</code></li>)}</ol>}
                              {!activityImplementations.length && <p className="application-flow-hierarchy-unresolved">尚未绑定到已核对的源码实现</p>}
                            </section>
                          })}
                        </div>
                      </div>}
                      {stageLevelImplementations.length > 0 && <div className="application-flow-stage-implementations"><strong>{stageActivities.length > 0 ? '子阶段级实现' : '函数实现'}</strong><ol>{stageLevelImplementations.map((item) => <li key={text(item.id, `${stageId}-${number(item.order)}`)}><span>{text(item.relation)}</span><strong>{text(item.symbol)}</strong><code>{text(item.path)}:{number(item.line)}</code></li>)}</ol></div>}
                      {!stageImplementations.length && <p className="application-flow-hierarchy-unresolved">当前分析范围尚未关联到已核对的源码实现</p>}
                    </div>
                  </div>
                })}
              </div>
            </div>
          })}
        </div>
      </section>
    })}
    {text(hierarchy.representation) === 'legacy_flat_projection' && <div className="application-flow-hierarchy-legacy"><CircleAlert size={15} /><span>这是旧版扁平 Stage 的兼容投影；重新生成该流程可获得真实动作、业务阶段与函数调用层。</span></div>}
  </div>
}

function FlowEvidenceView({
  scenario,
  stages,
  selectedRuntimeTrace,
  selectedRuntimeTraceId,
}: {
  scenario: ScenarioArtifact
  stages: JsonRecord[]
  selectedRuntimeTrace?: RuntimeTraceSummary
  selectedRuntimeTraceId: string
}) {
  const scenarioQuality = record(scenario.quality)
  const stageEvidenceById = new Map((scenario.stage_evidence ?? []).map((item) => [text(item.stage_id), record(item)]))
  const boundaryAnalysisById = new Map((scenario.boundary_analysis ?? []).map((item) => [text(item.stage_id), record(item)]))
  const runtimeTraceAlignment = record(scenario.runtime_trace_alignment)
  const alignedRuntimeTraceId = text(runtimeTraceAlignment.trace_analysis_artifact_id)
  const alignedRuntimeTrace = record(runtimeTraceAlignment.trace)
  const runtimeAlignmentQuality = record(runtimeTraceAlignment.quality)
  const runtimeStageMatches = Array.isArray(runtimeTraceAlignment.stage_matches)
    ? runtimeTraceAlignment.stage_matches.map(record)
    : []
  const hasRuntimeAlignment = Boolean(alignedRuntimeTraceId || runtimeStageMatches.length > 0)
  const selectedTraceDiffers = Boolean(selectedRuntimeTraceId && alignedRuntimeTraceId && selectedRuntimeTraceId !== alignedRuntimeTraceId)
  const runtimeMatchByStageId = new Map(runtimeStageMatches.map((item) => [text(item.stage_id), item]))

  return <div className="application-flow-evidence-view">
    <section className="application-flow-evidence-section" aria-label="源码插桩证据">
      <div className="application-flow-evidence-heading"><div><strong>源码 HiTrace 插桩</strong><small>定位声明位置与静态事件表达；它本身不证明事件在本次运行中发生。</small></div><span>{number(scenarioQuality.source_declared_stage_count)} / {stages.length} 阶段闭合</span></div>
      <div className="application-flow-stage-table table-shell">
        <div className="table-header"><span>顺序</span><span>阶段 / 进程</span><span>源码 Start 候选</span><span>源码 End 候选</span><span>识别状态</span></div>
        {stages.map((stage) => {
          const stageId = text(stage.id)
          const evidence = stageEvidenceById.get(stageId) ?? {}
          const analysis = boundaryAnalysisById.get(stageId) ?? {}
          const startEvidence = boundaryAnchor(evidence, 'start')
          const endEvidence = boundaryAnchor(evidence, 'end')
          const startAnalysis = record(analysis.start)
          const endAnalysis = record(analysis.end)
          const startAnchor = startAnalysis.status === 'observed_in_source' ? startAnalysis : startEvidence
          const endAnchor = endAnalysis.status === 'observed_in_source' ? endAnalysis : endEvidence
          const calibrated = Boolean(text(startAnchor.anchor_id) && text(endAnchor.anchor_id))
          const startName = text(startAnalysis.event_name, text(startEvidence.event_name, locatorLabel(stage, 'start')))
          const endName = text(endAnalysis.event_name, text(endEvidence.event_name, locatorLabel(stage, 'end')))
          const startDetail = sourceAnchorDetail(startAnchor) || '未找到源码 Start 锚点'
          const endDetail = sourceAnchorDetail(endAnchor) || '未找到源码 End 锚点'
          return <div className="table-row" key={stageId}><span>{number(stage.order)}</span><div><strong>{text(stage.name)}</strong><small title={`${stageId} · ${text(stage.process_name)}`}>{stageId} · {text(stage.process_name)}</small></div><div className="application-flow-boundary"><code title={startName}>{startName}</code><small title={startDetail}>{startDetail}</small></div><div className="application-flow-boundary"><code title={endName}>{endName}</code><small title={endDetail}>{endDetail}</small></div><span title={text(analysis.notes, text(evidence.notes))}>{analysis.status ? anchorAnalysisLabel(analysis.status) : anchorStatusLabel(evidence.trace_anchor_status, calibrated)}</span></div>
        })}
      </div>
      {list(scenario.unknowns).length > 0 && <div className="application-flow-unknowns"><CircleAlert size={16} /><div><strong>仍需证据</strong>{list(scenario.unknowns).map((item) => <span key={item}>{item}</span>)}</div></div>}
    </section>

    <section className="application-flow-evidence-section" aria-label="真实 Trace 边界证据">
      <div className="application-flow-evidence-heading"><div><strong>真实 Trace 边界</strong><small>校准后的 start/end 采用事件目录中的完整 event name，并以 exact 方式匹配。</small></div><span>{hasRuntimeAlignment ? `${number(runtimeAlignmentQuality.matched_stage_count)} / ${stages.length} 精确闭合` : '等待运行时校准'}</span></div>
      {hasRuntimeAlignment
        ? <div className="application-flow-runtime-trace-selection"><FileJson2 size={15} /><span><strong>{text(alignedRuntimeTrace.trace_name, alignedRuntimeTraceId)}</strong><small>{alignedRuntimeTraceId} · {text(alignedRuntimeTrace.input_kind) === 'raw_htrace' ? '从 TraceStreamer 事件目录读取' : '从阶段报告记录读取'}{selectedTraceDiffers ? ' · 顶部当前另选了一份输入 Trace' : ''}</small></span></div>
        : selectedRuntimeTrace
          ? <div className="application-flow-runtime-trace-selection"><FileJson2 size={15} /><span><strong>{selectedRuntimeTrace.trace_name || selectedRuntimeTrace.artifact_id}</strong><small>{selectedRuntimeTrace.artifact_id} · 等待本轮证据校准</small></span></div>
          : <div className="application-flow-empty compact"><Upload size={22} /><strong>本轮未选择真实 Trace</strong><span>当前 Mermaid/JSON 保留源码锚点候选；在第 1 步选择 Trace 后重新生成，即可校准实际 event name。</span></div>}
      {runtimeStageMatches.length > 0 && <>
        <div className="application-flow-runtime-summary"><div><Check size={16} /><span><strong>运行时校准结果</strong><small>{text(record(runtimeTraceAlignment.trace).trace_name, alignedRuntimeTraceId)}</small></span></div><div><span><strong>{number(runtimeAlignmentQuality.matched_stage_count)}</strong> 完整匹配</span><span><strong>{number(runtimeAlignmentQuality.partial_stage_count)}</strong> 部分匹配</span><span><strong>{number(runtimeAlignmentQuality.unmatched_stage_count)}</strong> 未匹配</span></div></div>
        <div className="application-flow-stage-table table-shell">
          <div className="table-header"><span>顺序</span><span>阶段 / 进程</span><span>真实 Start Event</span><span>真实 End Event</span><span>匹配状态</span></div>
          {stages.map((stage) => {
            const stageId = text(stage.id)
            const runtimeMatch = runtimeMatchByStageId.get(stageId) ?? {}
            const runtimeStart = record(runtimeMatch.start_event)
            const runtimeEnd = record(runtimeMatch.end_event)
            const startName = text(runtimeStart.event_name, '未匹配')
            const endName = text(runtimeEnd.event_name, '未匹配')
            const startDetail = runtimeEventDetail(runtimeStart) || 'Trace 中未确认 Start Event'
            const endDetail = runtimeEventDetail(runtimeEnd) || 'Trace 中未确认 End Event'
            return <div className="table-row" key={stageId}><span>{number(stage.order)}</span><div><strong>{text(stage.name)}</strong><small title={`${stageId} · ${text(stage.process_name)}`}>{stageId} · {text(stage.process_name)}</small></div><div className="application-flow-boundary"><code title={startName}>{startName}</code><small title={startDetail}>{startDetail}</small></div><div className="application-flow-boundary"><code title={endName}>{endName}</code><small title={endDetail}>{endDetail}</small></div><span title={text(runtimeMatch.rationale)}>{runtimeMatchLabel(runtimeMatch.match_status)}</span></div>
          })}
        </div>
      </>}
      {hasRuntimeAlignment && runtimeStageMatches.length === 0 && <div className="application-flow-empty compact"><Sparkles size={22} /><strong>当前 Trace 未闭合阶段边界</strong><span>候选事件与限制已保存在场景证据中，请查看页面助手诊断。</span></div>}
      {!hasRuntimeAlignment && selectedRuntimeTrace && <div className="application-flow-empty compact"><Sparkles size={22} /><strong>等待本轮证据校准</strong><span>重新生成阶段图时，页面助手会在同一任务中完成源码插桩核对与真实 Trace 对齐。</span></div>}
    </section>
  </div>
}

function FlowAnalysisSummary({
  scenario,
  hierarchy,
  stages,
  actionCoverage,
}: {
  scenario: ScenarioArtifact
  hierarchy: JsonRecord
  stages: JsonRecord[]
  actionCoverage: JsonRecord[]
}) {
  const actions = Array.isArray(hierarchy.actions) ? hierarchy.actions.map(record) : []
  const phases = Array.isArray(hierarchy.phases) ? hierarchy.phases.map(record) : []
  const activities = Array.isArray(hierarchy.activities) ? hierarchy.activities.map(record) : []
  const implementations = Array.isArray(hierarchy.implementations) ? hierarchy.implementations.map(record) : []
  const implementedStageIds = new Set(implementations.map((item) => text(item.stage_id)).filter(Boolean))
  const unresolvedStages = stages.filter((stage) => !implementedStageIds.has(text(stage.id)))
  const activityIds = new Set(activities.map((item) => text(item.id)).filter(Boolean))
  const activityImplementationCounts = new Map<string, number>()
  for (const implementation of implementations) {
    const activityId = text(implementation.activity_id)
    if (activityId) activityImplementationCounts.set(activityId, (activityImplementationCounts.get(activityId) ?? 0) + 1)
  }
  const stagesWithActivities = new Set(activities.map((item) => text(item.stage_id)).filter(Boolean))
  const stagesWithoutActivities = stages.filter((stage) => !stagesWithActivities.has(text(stage.id))).length
  const activitiesWithoutImplementations = [...activityIds].filter((activityId) => !activityImplementationCounts.has(activityId)).length
  const aggregateActivities = [...activityImplementationCounts.values()].filter((count) => count > 1).length
  const granularityObservation = record(hierarchy.granularity_observation)
  const granularityReviewCount = number(
    granularityObservation.review_candidate_count,
    stagesWithoutActivities + activitiesWithoutImplementations + aggregateActivities,
  )
  const coverageGaps = actionCoverage.filter((item) => item.mapping_status === 'partial' || item.mapping_status === 'unmapped')
  const unknowns = list(scenario.unknowns)
  const sourceUnknowns = unknowns.filter((item) => /代码仓|源码.{0,12}(?:缺失|未关联|无法定位|范围)|缺少.{0,12}源码|source.{0,12}(?:missing|unavailable|unresolved)|repository/i.test(item))
  const gapDetails = Array.from(new Set([
    ...coverageGaps.map((item) => text(item.missing_reason)).filter(Boolean),
    ...unresolvedStages.map((stage) => `“${text(stage.name, text(stage.id))}”尚未关联源码实现`),
    ...sourceUnknowns,
  ])).slice(0, 3)
  const runtimeQuality = record(record(scenario.runtime_trace_alignment).quality)
  const runtimeMatched = number(runtimeQuality.matched_stage_count)
  const hasRuntimeEvidence = Boolean(scenario.runtime_trace_alignment)
  const hasSourceGap = gapDetails.length > 0
  const hasGranularityReview = granularityReviewCount > 0
  const reviewDetails = [
    ...gapDetails,
    ...(stagesWithoutActivities > 0 ? [`${stagesWithoutActivities} 个子阶段尚未展开细粒度活动`] : []),
    ...(activitiesWithoutImplementations > 0 ? [`${activitiesWithoutImplementations} 个细粒度活动尚未绑定源码实现`] : []),
    ...(aggregateActivities > 0 ? [`${aggregateActivities} 个活动聚合了多个源码实现，可按职责或异步边界复核`] : []),
  ].slice(0, 4)

  return <section className={`application-flow-analysis-summary ${hasSourceGap || hasGranularityReview ? 'warning' : 'ready'}`} aria-label="当前流程分析小结">
    <header>
      {hasSourceGap || hasGranularityReview ? <CircleAlert size={17} /> : <Check size={17} />}
      <div><strong>当前分析小结</strong><span>{actions.length} 个用户动作 · {phases.length} 个业务阶段 · {stages.length} 个子阶段{activities.length > 0 ? ` · ${activities.length} 个细粒度活动` : ''} · {implementations.length} 个源码实现点</span></div>
    </header>
    <div className="application-flow-analysis-summary-metrics">
      <span><small>源码已关联阶段</small><strong>{implementedStageIds.size} / {stages.length}</strong></span>
      <span><small>运行时精确闭合</small><strong>{hasRuntimeEvidence ? `${runtimeMatched} / ${stages.length}` : '未校准'}</strong></span>
      <span><small>未决证据</small><strong>{unknowns.length}</strong></span>
      <span><small>粒度复核候选</small><strong>{granularityReviewCount}</strong></span>
    </div>
    <p>{hasSourceGap
      ? '当前存在明确的源码关联缺口，相关阶段结论仍需补充代码仓或实现线索。'
      : hasGranularityReview
        ? '结构契约已经通过，但仍有聚合或未绑定节点值得结合源码职责继续细化。'
      : '当前分析范围内未发现明确的必要源码缺口；尚未闭合的运行时边界和其他证据仍保留在边界证据中。'}</p>
    {reviewDetails.length > 0 && <ul>{reviewDetails.map((item) => <li key={item}>{item}</li>)}</ul>}
  </section>
}

export function ApplicationFlowStudio({
  connectors,
  selectedConnectorId,
  loadedTrace,
  onSelectConnector,
  onRefreshConnectors,
  onAssistantContextChange,
  runtimeTraceUploadEnabled = true,
  artifactDownloadsEnabled = true,
  runtimeTraceConnectorFilterEnabled = true,
  onListCaptureTraceSources,
  onMaterializeCaptureTrace,
}: {
  connectors: Connector[]
  selectedConnectorId: string
  loadedTrace?: Artifact | null
  onSelectConnector: (connectorId: string) => void
  onRefreshConnectors: () => Promise<unknown>
  onAssistantContextChange: (context: ApplicationFlowAssistantContext | null) => void
  runtimeTraceUploadEnabled?: boolean
  artifactDownloadsEnabled?: boolean
  runtimeTraceConnectorFilterEnabled?: boolean
  onListCaptureTraceSources?: () => Promise<CaptureRuntimeTraceSource[]>
  onMaterializeCaptureTrace?: (source: CaptureRuntimeTraceSource, sourceConnectorId: string) => Promise<RuntimeTraceSummary>
}) {
  const [catalog, setCatalog] = useState<FlowCatalog | null>(null)
  const [codeGraphCatalog, setCodeGraphCatalog] = useState<CodeGraphCatalog | null>(null)
  const [selectedCodeGraphId, setSelectedCodeGraphId] = useState('')
  const [selectedDiscoveryId, setSelectedDiscoveryId] = useState('')
  const [selectedFlowId, setSelectedFlowId] = useState('')
  const [selectedScenarioId, setSelectedScenarioId] = useState('')
  const [discovery, setDiscovery] = useState<DiscoveryArtifact | null>(null)
  const [scenario, setScenario] = useState<ScenarioArtifact | null>(null)
  const [runtimeTraceCatalog, setRuntimeTraceCatalog] = useState<RuntimeTraceCatalog | null>(null)
  const [captureTraceSources, setCaptureTraceSources] = useState<CaptureRuntimeTraceSource[]>([])
  const [selectedRuntimeTraceId, setSelectedRuntimeTraceId] = useState('')
  const [subpath, setSubpath] = useState('')
  const [relatedSourceScopes, setRelatedSourceScopes] = useState<RelatedSourceScope[]>([])
  const [analysisGoal, setAnalysisGoal] = useState('')
  const [revisionRequest, setRevisionRequest] = useState('')
  const [confirmFlowBeforeTrace, setConfirmFlowBeforeTrace] = useState(true)
  const [savingFlowPreference, setSavingFlowPreference] = useState(false)
  const [busy, setBusy] = useState<BusyPhase>(null)
  const [notice, setNotice] = useState<{ tone: 'info' | 'error' | 'success'; message: string } | null>(null)
  const [pendingFlowAnalysis, setPendingFlowAnalysis] = useState<PendingFlowAnalysis | null>(null)
  const [detailTab, setDetailTab] = useState<'hierarchy' | 'diagram' | 'evidence' | 'json' | 'analysis-preview' | 'analysis-json'>('hierarchy')
  const [pendingDelete, setPendingDelete] = useState<{ artifactType: 'application_flow_discovery' | 'application_flow_scenario'; artifactId: string; label: string; cascade: boolean } | null>(null)
  const [pendingTraceDelete, setPendingTraceDelete] = useState<RuntimeTraceSummary | null>(null)
  const runtimeTraceInputRef = useRef<HTMLInputElement>(null)
  const flowCalibrationRunRef = useRef<{ expectsRuntime: boolean; completesAfterCompile: boolean } | null>(null)
  const pendingFlowAnalysisRef = useRef<PendingFlowAnalysis | null>(null)
  const activeFlowAnalysisIdRef = useRef('')

  const activeConnector = connectors.find((connector) => connector.connector_id === selectedConnectorId)
  const loadedRuntimeTrace = useMemo(() => runtimeTraceSummaryFromArtifact(loadedTrace), [loadedTrace])
  const loadedRuntimeTraceId = loadedRuntimeTrace?.artifact_id ?? ''
  const connectorCatalogKey = useMemo(
    () => connectors.map((connector) => connector.connector_id).sort().join('\u0000'),
    [connectors],
  )
  const normalizedSubpath = normalizeScope(subpath)
  const matchingCodeGraphs = useMemo(
    () => (codeGraphCatalog?.graphs ?? []).filter((item) => (
      item.source_connector_id === selectedConnectorId
      && item.source_snapshot_id === activeConnector?.snapshot.snapshot_id
      && normalizeScope(item.scope) === normalizedSubpath
    )),
    [activeConnector?.snapshot.snapshot_id, codeGraphCatalog?.graphs, normalizedSubpath, selectedConnectorId],
  )
  const activeCodeGraph = matchingCodeGraphs.find((item) => item.graph_id === selectedCodeGraphId)
    ?? matchingCodeGraphs.find((item) => item.status === 'ready')
    ?? matchingCodeGraphs[0]
  const sourceScopeViews = useMemo(() => {
    const rawScopes: RelatedSourceScope[] = [
      { sourceConnectorId: selectedConnectorId, subpath },
      ...relatedSourceScopes.filter((item) => item.sourceConnectorId !== selectedConnectorId),
    ].filter((item) => item.sourceConnectorId)
    return rawScopes.flatMap((item, index) => {
      const connector = connectors.find((candidate) => candidate.connector_id === item.sourceConnectorId)
      if (!connector) return []
      const normalizedScope = normalizeScope(item.subpath)
      const graphs = (codeGraphCatalog?.graphs ?? []).filter((graph) => (
        graph.source_connector_id === connector.connector_id
        && graph.source_snapshot_id === connector.snapshot.snapshot_id
        && normalizeScope(graph.scope) === normalizedScope
      ))
      const graph = index === 0
        ? graphs.find((candidate) => candidate.graph_id === selectedCodeGraphId) ?? graphs.find((candidate) => candidate.status === 'ready') ?? graphs[0]
        : graphs.find((candidate) => candidate.status === 'ready') ?? graphs[0]
      return [{ connector, subpath: normalizedScope, graph, role: (index === 0 ? 'primary' : 'related') as 'primary' | 'related' }]
    })
  }, [codeGraphCatalog?.graphs, connectors, relatedSourceScopes, selectedCodeGraphId, selectedConnectorId, subpath])
  const sourceScopeConnectorIds = useMemo(() => new Set(sourceScopeViews.map((item) => item.connector.connector_id)), [sourceScopeViews])
  const sourceScopesPayload = useMemo(() => sourceScopeViews.map((item) => ({
    source_connector_id: item.connector.connector_id,
    source_snapshot_id: item.connector.snapshot.snapshot_id,
    subpath: item.subpath,
    code_graph_id: item.graph?.graph_id ?? '',
    role: item.role,
  })), [sourceScopeViews])
  const runtimeTraceConnectorIds = useMemo(() => sourceScopeViews.map((item) => item.connector.connector_id).join(','), [sourceScopeViews])
  const historyDiscoveries = useMemo(() => {
    const latestByAnalysis = new Map<string, DiscoverySummary>()
    for (const item of catalog?.discoveries ?? []) {
      const current = latestByAnalysis.get(item.application_flow_analysis_id)
      if (!current || Date.parse(item.created_at) > Date.parse(current.created_at)) {
        latestByAnalysis.set(item.application_flow_analysis_id, item)
      }
    }
    return [...latestByAnalysis.values()].sort((left, right) => Date.parse(right.created_at) - Date.parse(left.created_at))
  }, [catalog?.discoveries])
  const historyScenarios = useMemo(() => catalog?.scenarios ?? [], [catalog?.scenarios])
  const connectorDiscoveries = useMemo(
    () => (catalog?.discoveries ?? []).filter((item) => {
      const scopes = Array.isArray(item.source_scopes) ? item.source_scopes.map(record) : []
      return item.source_connector_id === selectedConnectorId || scopes.some((scope) => text(scope.source_connector_id) === selectedConnectorId)
    }),
    [catalog?.discoveries, selectedConnectorId],
  )
  const visiblePendingFlowAnalysis = pendingFlowAnalysis
  const selectedFlow = discovery?.discovery.flows.find((item) => item.id === selectedFlowId) ?? null
  const selectedBlueprint = candidateBlueprint(discovery?.analysis_context, selectedFlowId)
  const selectedActionCoverage = Array.isArray(selectedBlueprint.action_coverage) ? selectedBlueprint.action_coverage.map(record) : []
  const matchingScenarios = useMemo(
    () => scenarioLineage(historyScenarios, selectedDiscoveryId, selectedFlowId),
    [historyScenarios, selectedDiscoveryId, selectedFlowId],
  )
  const availableRuntimeTraces = useMemo(() => {
    const byArtifactId = new Map<string, RuntimeTraceSummary>()
    for (const item of runtimeTraceCatalog?.traces ?? []) {
      if (item?.artifact_id) byArtifactId.set(item.artifact_id, item)
    }
    return [...byArtifactId.values()]
  }, [runtimeTraceCatalog?.traces])
  const selectedRuntimeTrace = availableRuntimeTraces.find((item) => item.artifact_id === selectedRuntimeTraceId)
  const activeRuntimeTraceId = selectedRuntimeTrace?.artifact_id ?? ''
  const assistantContext = useMemo<ApplicationFlowAssistantContext>(() => ({
    page_id: 'application_flow',
    source_connector_id: selectedConnectorId,
    source_snapshot_id: activeConnector?.snapshot.snapshot_id ?? '',
    subpath: discovery?.discovery.scope ?? normalizedSubpath,
    source_scopes: sourceScopesPayload,
    graph_id: activeCodeGraph?.graph_id ?? null,
    application_flow_analysis_id: pendingFlowAnalysis?.applicationFlowAnalysisId
      ?? discovery?.application_flow_analysis_id
      ?? scenario?.application_flow_analysis_id
      ?? null,
    discovery_artifact_id: discovery?.artifact_id ?? null,
    flow_id: selectedFlow?.id ?? null,
    flow_name: selectedFlow?.name ?? null,
    scenario_artifact_id: scenario?.artifact_id ?? null,
    scenario_revision: scenario ? number(scenario.revision, 1) : null,
    trace_analysis_artifact_id: activeRuntimeTraceId || null,
  }), [activeCodeGraph?.graph_id, activeConnector?.snapshot.snapshot_id, activeRuntimeTraceId, discovery?.application_flow_analysis_id, discovery?.artifact_id, discovery?.discovery.scope, normalizedSubpath, pendingFlowAnalysis?.applicationFlowAnalysisId, scenario, selectedConnectorId, selectedFlow?.id, selectedFlow?.name, sourceScopesPayload])

  useEffect(() => {
    onAssistantContextChange(assistantContext)
  }, [assistantContext, onAssistantContextChange])

  useEffect(() => () => onAssistantContextChange(null), [onAssistantContextChange])

  useEffect(() => {
    setRelatedSourceScopes((current) => {
      const next = current.filter((item) => item.sourceConnectorId !== selectedConnectorId)
      return next.length === current.length ? current : next
    })
  }, [selectedConnectorId])

  const refreshCatalog = useCallback(async () => {
    const value = await api<FlowCatalog>('/v1/application-flows', { cache: 'no-store' })
    setCatalog(value)
    setConfirmFlowBeforeTrace(value.preferences?.confirm_flow_before_trace ?? true)
    return value
  }, [])

  const refreshCodeGraphs = useCallback(async () => {
    const value = await api<CodeGraphCatalog>('/v1/code-graphs?limit=500')
    setCodeGraphCatalog(value)
    return value
  }, [])

  const refreshRuntimeTraces = useCallback(async (preferredTraceId = '') => {
    const query = runtimeTraceConnectorFilterEnabled && runtimeTraceConnectorIds
      ? `?source_connector_ids=${encodeURIComponent(runtimeTraceConnectorIds)}&limit=500`
      : '?limit=500'
    const value = await api<RuntimeTraceCatalog>(`/v1/application-flows/runtime-traces${query}`)
    setRuntimeTraceCatalog(value)
    setSelectedRuntimeTraceId((current) => {
      const availableIds = new Set(value.traces.map((item) => item.artifact_id))
      if (preferredTraceId && availableIds.has(preferredTraceId)) return preferredTraceId
      if (availableIds.has(current)) return current
      if (loadedRuntimeTraceId && availableIds.has(loadedRuntimeTraceId)) return loadedRuntimeTraceId
      return value.traces[0]?.artifact_id || ''
    })
    return value
  }, [loadedRuntimeTraceId, runtimeTraceConnectorIds, runtimeTraceConnectorFilterEnabled])

  const refreshCaptureTraceSources = useCallback(async () => {
    if (!onListCaptureTraceSources) return []
    const sources = await onListCaptureTraceSources()
    setCaptureTraceSources(sources)
    return sources
  }, [onListCaptureTraceSources])

  async function selectRuntimeTrace(value: string) {
    if (!value.startsWith('capture:')) {
      setSelectedRuntimeTraceId(value)
      return
    }
    const source = captureTraceSources.find((item) => `capture:${item.traceId}:${item.stepId}` === value)
    if (!source || !onMaterializeCaptureTrace || !activeConnector) return
    setBusy('importing-trace')
    setNotice({tone: 'info', message: `正在物化“${source.stepTitle}”的真实 Trace 事件与热点…`})
    try {
      const materialized = await onMaterializeCaptureTrace(source, activeConnector.connector_id)
      await refreshRuntimeTraces(materialized.artifact_id)
      setNotice({tone: 'success', message: `已将“${source.stepTitle}”纳入真实 Trace 目录，并绑定到当前代码仓。`})
    } catch (error) {
      setNotice({tone: 'error', message: error instanceof Error ? error.message : '本机采集 Trace 物化失败'})
    } finally {
      setBusy(null)
    }
  }

  async function bindRuntimeTraceForAnalysis(applicationFlowAnalysisId: string): Promise<RuntimeTraceSummary | null> {
    const response = await api<{ status: string; trace: RuntimeTraceSummary | null }>('/v1/application-flows/runtime-traces/bind', {
      method: 'POST',
      body: JSON.stringify({
        application_flow_analysis_id: applicationFlowAnalysisId,
        analysis_artifact_id: activeRuntimeTraceId || null,
      }),
    })
    return response.trace
  }

  async function updateFlowAnalysisMode(enabled: boolean) {
    const previous = confirmFlowBeforeTrace
    setConfirmFlowBeforeTrace(enabled)
    setSavingFlowPreference(true)
    try {
      const saved = await api<{ confirm_flow_before_trace: boolean }>('/v1/application-flows/preferences', {
        method: 'PUT',
        body: JSON.stringify({ confirm_flow_before_trace: enabled }),
      })
      setConfirmFlowBeforeTrace(saved.confirm_flow_before_trace)
    } catch (error) {
      setConfirmFlowBeforeTrace(previous)
      setNotice({ tone: 'error', message: error instanceof Error ? error.message : '流程分析偏好保存失败' })
    } finally {
      setSavingFlowPreference(false)
    }
  }

  const openDiscovery = useCallback(async (artifactId: string) => {
    if (!artifactId) {
      setSelectedDiscoveryId('')
      setSelectedFlowId('')
      setDiscovery(null)
      return
    }
    setBusy((current) => current ?? 'loading')
    try {
      const value = await api<DiscoveryArtifact>(`/v1/application-flows/discoveries/${encodeURIComponent(artifactId)}`)
      const runningAnalysisId = flowCalibrationRunRef.current ? activeFlowAnalysisIdRef.current : ''
      if (runningAnalysisId && value.application_flow_analysis_id !== runningAnalysisId) return
      activeFlowAnalysisIdRef.current = value.application_flow_analysis_id
      setDiscovery(value)
      const savedGoal = text(record(value.analysis_context).analysis_goal)
      if (savedGoal) setAnalysisGoal(savedGoal)
      const storedScopes = Array.isArray(record(value.analysis_context).source_scopes)
        ? (record(value.analysis_context).source_scopes as unknown[]).map(record)
        : []
      const primaryScope = storedScopes.find((item) => item.role === 'primary') ?? storedScopes[0]
      const primaryConnectorId = text(primaryScope?.source_connector_id, value.source_connector_id)
      if (primaryConnectorId && primaryConnectorId !== selectedConnectorId) onSelectConnector(primaryConnectorId)
      setSubpath(normalizeScope(text(primaryScope?.subpath, value.discovery.scope)))
      setRelatedSourceScopes(storedScopes
        .filter((item) => text(item.source_connector_id) && text(item.source_connector_id) !== primaryConnectorId)
        .map((item) => ({ sourceConnectorId: text(item.source_connector_id), subpath: normalizeScope(text(item.subpath)) })))
      setSelectedDiscoveryId(artifactId)
      setSelectedFlowId((current) => value.discovery.flows.some((flow) => flow.id === current) ? current : value.discovery.flows[0]?.id ?? '')
    } finally {
      setBusy((current) => current === 'loading' ? null : current)
    }
  }, [onSelectConnector, selectedConnectorId])

  const openScenario = useCallback(async (
    artifactId: string,
    targetTab: 'hierarchy' | 'diagram' | 'evidence' | 'json' | 'analysis-preview' | 'analysis-json' = 'hierarchy',
  ) => {
    if (!artifactId) {
      setSelectedScenarioId('')
      setScenario(null)
      return
    }
    setBusy((current) => current ?? 'loading')
    try {
      const value = await api<ScenarioArtifact>(`/v1/application-flows/scenarios/${encodeURIComponent(artifactId)}`, { cache: 'no-store' })
      const runningAnalysisId = flowCalibrationRunRef.current ? activeFlowAnalysisIdRef.current : ''
      if (runningAnalysisId && value.application_flow_analysis_id !== runningAnalysisId) return
      activeFlowAnalysisIdRef.current = value.application_flow_analysis_id
      setScenario(value)
      setSelectedScenarioId(artifactId)
      const alignedTraceId = text(record(value.runtime_trace_alignment).trace_analysis_artifact_id)
      if (alignedTraceId) setSelectedRuntimeTraceId(alignedTraceId)
      setDetailTab(targetTab)
      setRevisionRequest('')
    } finally {
      setBusy((current) => current === 'loading' ? null : current)
    }
  }, [])

  const loadAnalysisRecord = useCallback(async (
    discoverySummary: DiscoverySummary,
    requestedScenario?: ScenarioSummary,
  ) => {
    const lineage = discoveryScenarioLineage(historyScenarios, discoverySummary)
    const scenarioSummary = requestedScenario ?? lineage[0]
    const associatedTraceId = text(
      scenarioSummary?.runtime_trace_analysis_artifact_id,
      text(
        lineage.find((item) => item.runtime_trace_analysis_artifact_id)?.runtime_trace_analysis_artifact_id,
        text(discoverySummary.trace_analysis_artifact_id),
      ),
    )
    activeFlowAnalysisIdRef.current = discoverySummary.application_flow_analysis_id
    flowCalibrationRunRef.current = null
    setBusy('loading')
    try {
      const [discoveryValue, scenarioValue] = await Promise.all([
        api<DiscoveryArtifact>(`/v1/application-flows/discoveries/${encodeURIComponent(discoverySummary.artifact_id)}`, { cache: 'no-store' }),
        scenarioSummary
          ? api<ScenarioArtifact>(`/v1/application-flows/scenarios/${encodeURIComponent(scenarioSummary.artifact_id)}`, { cache: 'no-store' })
          : Promise.resolve(null),
      ])
      const analysisContext = record(discoveryValue.analysis_context)
      const storedScopes = Array.isArray(analysisContext.source_scopes)
        ? (analysisContext.source_scopes as unknown[]).map(record)
        : (discoverySummary.source_scopes ?? []).map(record)
      const primaryScope = storedScopes.find((item) => item.role === 'primary') ?? storedScopes[0] ?? {
        source_connector_id: discoveryValue.source_connector_id,
        subpath: discoveryValue.discovery.scope,
      }
      const primaryConnectorId = text(primaryScope.source_connector_id, discoveryValue.source_connector_id)
      const connectorAvailable = connectors.some((item) => item.connector_id === primaryConnectorId)
      if (connectorAvailable && primaryConnectorId !== selectedConnectorId) onSelectConnector(primaryConnectorId)
      setSubpath(normalizeScope(text(primaryScope.subpath, discoveryValue.discovery.scope)))
      setRelatedSourceScopes(storedScopes
        .filter((item) => text(item.source_connector_id) && text(item.source_connector_id) !== primaryConnectorId)
        .map((item) => ({ sourceConnectorId: text(item.source_connector_id), subpath: normalizeScope(text(item.subpath)) })))
      const graphArtifactId = text(primaryScope.code_graph_artifact_id, text(analysisContext.code_graph_artifact_id))
      const graphId = text(primaryScope.code_graph_id)
        || codeGraphCatalog?.graphs.find((item) => item.artifact_id === graphArtifactId)?.graph_id
        || ''
      setSelectedCodeGraphId(graphId)
      setAnalysisGoal(text(analysisContext.analysis_goal, discoverySummary.analysis_goal ?? ''))
      setDiscovery(discoveryValue)
      setSelectedDiscoveryId(discoverySummary.artifact_id)
      const flowId = scenarioValue?.flow_id ?? scenarioSummary?.flow_id ?? discoveryValue.discovery.flows[0]?.id ?? ''
      setSelectedFlowId(flowId)
      setScenario(scenarioValue)
      setSelectedScenarioId(scenarioValue?.artifact_id ?? '')
      const restoredTraceId = text(
        record(scenarioValue?.runtime_trace_alignment).trace_analysis_artifact_id,
        text(analysisContext.trace_analysis_artifact_id, associatedTraceId),
      )
      setSelectedRuntimeTraceId(restoredTraceId)
      setDetailTab('hierarchy')
      setRevisionRequest('')
      setNotice({
        tone: 'success',
        message: `${scenarioValue ? '分析记录、阶段图' : '分析记录'}${restoredTraceId ? '、真实 Trace' : ''}${connectorAvailable ? '与代码仓' : ''}已装载${connectorAvailable ? '' : '；原代码仓当前未登记，请重新登记后再继续源码分析'}`,
      })
    } catch (error) {
      setNotice({ tone: 'error', message: error instanceof Error ? error.message : '应用流程分析记录装载失败' })
    } finally {
      setBusy(null)
    }
  }, [codeGraphCatalog?.graphs, connectors, historyScenarios, onSelectConnector, selectedConnectorId])

  const openStageAnalysis = useCallback(async (targetTab: 'analysis-preview' | 'analysis-json') => {
    if (records(record(scenario?.stage_analysis).stages).length > 0) {
      setDetailTab(targetTab)
      return
    }
    try {
      const latestCatalog = await refreshCatalog()
      const currentLineage = selectedScenarioId
        ? scenarioArtifactLineage(latestCatalog.scenarios, selectedScenarioId)
        : scenarioLineage(latestCatalog.scenarios, selectedDiscoveryId, selectedFlowId)
      const analyzedScenario = currentLineage.find((item) => item.has_stage_analysis)
        ?? matchingScenarios.find((item) => item.has_stage_analysis)
      if (!analyzedScenario) {
        setDetailTab(targetTab)
        setNotice({ tone: 'info', message: '当前流程谱系尚未生成阶段分析侧车；场景版本号不会阻止预览加载。' })
        return
      }
      await openScenario(analyzedScenario.artifact_id, targetTab)
      setNotice({
        tone: 'info',
        message: `当前版本没有分析侧车，已装载同一流程中包含分析预览的 v${number(analyzedScenario.revision, 1)}。`,
      })
    } catch (error) {
      setNotice({ tone: 'error', message: error instanceof Error ? error.message : '分析预览装载失败' })
    }
  }, [matchingScenarios, openScenario, refreshCatalog, scenario?.stage_analysis, selectedDiscoveryId, selectedFlowId, selectedScenarioId])

  useEffect(() => {
    void refreshCatalog().catch((error: unknown) => setNotice({ tone: 'error', message: error instanceof Error ? error.message : '无法读取应用流程资产' }))
  }, [refreshCatalog])

  useEffect(() => {
    void refreshCodeGraphs().catch((error: unknown) => setNotice({ tone: 'error', message: error instanceof Error ? error.message : '无法读取代码知识底图' }))
  }, [refreshCodeGraphs])

  useEffect(() => {
    void refreshRuntimeTraces().catch((error: unknown) => setNotice({ tone: 'error', message: error instanceof Error ? error.message : '无法读取真实 Trace 列表' }))
  }, [onListCaptureTraceSources, refreshRuntimeTraces])

  useEffect(() => {
    void refreshCaptureTraceSources().catch((error: unknown) => setNotice({ tone: 'error', message: error instanceof Error ? error.message : '无法读取本机 Trace 管理记录' }))
  }, [refreshCaptureTraceSources])

  useEffect(() => {
    const traceConnectorId = loadedRuntimeTrace?.source_connector_id?.trim() ?? ''
    if (!traceConnectorId || traceConnectorId === selectedConnectorId) return
    if (!connectorCatalogKey.split('\u0000').includes(traceConnectorId)) return
    onSelectConnector(traceConnectorId)
  }, [connectorCatalogKey, loadedRuntimeTrace?.source_connector_id, onSelectConnector, selectedConnectorId])

  useEffect(() => {
    if (loadedRuntimeTraceId && runtimeTraceCatalog?.traces.some((item) => item.artifact_id === loadedRuntimeTraceId)) {
      setSelectedRuntimeTraceId(loadedRuntimeTraceId)
    }
  }, [loadedRuntimeTraceId, runtimeTraceCatalog?.traces])

  useEffect(() => {
    const pending = sourceScopeViews.some((item) => item.graph?.status === 'queued' || item.graph?.status === 'running')
    if (!pending) return
    const timer = window.setTimeout(() => {
      void refreshCodeGraphs().catch((error: unknown) => setNotice({ tone: 'error', message: error instanceof Error ? error.message : '代码知识底图状态刷新失败' }))
    }, 1200)
    return () => window.clearTimeout(timer)
  }, [refreshCodeGraphs, sourceScopeViews])

  useEffect(() => {
    if (!selectedConnectorId) return
    if (selectedDiscoveryId && historyDiscoveries.some((item) => item.artifact_id === selectedDiscoveryId)) return
    const currentDiscoveryMatches = connectorDiscoveries.some((item) => item.artifact_id === selectedDiscoveryId)
    if (visiblePendingFlowAnalysis && !selectedDiscoveryId) return
    if (!currentDiscoveryMatches && connectorDiscoveries[0]) void openDiscovery(connectorDiscoveries[0].artifact_id)
    if (!connectorDiscoveries[0]) {
      setSelectedDiscoveryId('')
      setSelectedFlowId('')
      setDiscovery(null)
    }
  }, [connectorDiscoveries, historyDiscoveries, openDiscovery, selectedConnectorId, selectedDiscoveryId, visiblePendingFlowAnalysis])

  useEffect(() => {
    if (!selectedFlowId) return
    const currentScenarioMatches = matchingScenarios.some((item) => item.artifact_id === selectedScenarioId)
    if (!currentScenarioMatches && matchingScenarios[0]) void openScenario(matchingScenarios[0].artifact_id)
    if (!matchingScenarios[0]) {
      setSelectedScenarioId('')
      setScenario(null)
    }
  }, [matchingScenarios, openScenario, selectedFlowId, selectedScenarioId])

  useEffect(() => {
    const listener = (event: Event) => {
      const detail = record((event as CustomEvent<unknown>).detail)
      const actionId = text(detail.action_id)
      const result = record(detail.result)
      const artifact = record(result.artifact)
      const artifactType = text(artifact.artifact_type)
      const artifactId = text(artifact.artifact_id)
      const enrichedDiscovery = record(result.discovery_artifact)
      const enrichedDiscoveryId = text(enrichedDiscovery.artifact_id)
      if (!artifactId || !['application_flow_discovery', 'application_flow_scenario'].includes(artifactType)) return
      const resultAnalysisId = text(artifact.application_flow_analysis_id, text(enrichedDiscovery.application_flow_analysis_id))
      const activeAnalysisId = activeFlowAnalysisIdRef.current
      if (activeAnalysisId && resultAnalysisId !== activeAnalysisId) return
      pendingFlowAnalysisRef.current = null
      setPendingFlowAnalysis(null)
      const pendingFlow = flowCalibrationRunRef.current
      const intermediate = Boolean(
        pendingFlow
        && (
          actionId === 'application_flow.publish_candidates'
          || actionId === 'application_flow.save_discovery'
          || (actionId === 'application_flow.compile_mermaid' && !pendingFlow.completesAfterCompile)
          || (pendingFlow.expectsRuntime && actionId === 'application_flow.enrich_trace_anchors')
        )
      )
      void refreshCatalog().then(() => Promise.all([
        artifactType === 'application_flow_discovery'
          ? openDiscovery(artifactId)
          : openScenario(
            artifactId,
            actionId === 'application_flow.apply_runtime_trace_alignment'
              ? 'analysis-preview'
              : actionId === 'application_flow.compile_mermaid' && pendingFlow?.completesAfterCompile
                ? 'diagram'
                : 'hierarchy',
          ),
        enrichedDiscoveryId ? openDiscovery(enrichedDiscoveryId) : Promise.resolve(),
      ])).then(() => {
        if (intermediate) {
          setNotice({
            tone: 'info',
            message: actionId === 'application_flow.publish_candidates' || actionId === 'application_flow.save_discovery'
              ? '完整操作边界已登记，正在生成细粒度阶段图与证据'
              : actionId === 'application_flow.compile_mermaid'
                ? '分层阶段图已编译，正在复用本轮代码理解核对源码 HiTrace 插桩'
                : '源码插桩已核对，正在用所选真实 Trace 校准实际 event name',
          })
          return
        }
        setBusy(null)
        flowCalibrationRunRef.current = null
        if (actionId === 'application_flow.enrich_trace_anchors') setDetailTab('evidence')
        if (actionId === 'application_flow.apply_runtime_trace_alignment') setDetailTab('analysis-preview')
        setNotice({
          tone: 'success',
          message: actionId === 'application_flow.enrich_trace_anchors'
            ? text(result.summary_zh, '分层阶段图与源码 HiTrace 证据已生成，增强工件已装载')
            : actionId === 'application_flow.apply_runtime_trace_alignment'
              ? text(result.summary_zh, '阶段图、源码插桩与真实 Trace event name 已完成联合校准')
            : actionId === 'application_flow.compile_mermaid' && pendingFlow?.completesAfterCompile
              ? '流程图与标准 JSON 已生成，请先确认动作边界和阶段结构；确认后可继续进行 Trace 校准与分析预览'
            : artifactType === 'application_flow_discovery'
              ? '完整操作边界已登记'
              : '流程图已生成，标准 JSON 已由工具编译',
        })
      }).catch((error: unknown) => {
        setBusy(null)
        flowCalibrationRunRef.current = null
        setNotice({ tone: 'error', message: error instanceof Error ? error.message : '流程工件已生成，但页面刷新失败' })
      })
    }
    window.addEventListener('application-flow-updated', listener)
    return () => window.removeEventListener('application-flow-updated', listener)
  }, [openDiscovery, openScenario, refreshCatalog])

  useEffect(() => {
    const listener = (event: Event) => {
      const detail = record((event as CustomEvent<unknown>).detail)
      const requestId = text(detail.request_id)
      const status = text(detail.status)
      const current = pendingFlowAnalysisRef.current
      if (!current || current.requestId !== requestId) return
      if (status === 'accepted') {
        const next: PendingFlowAnalysis = { ...current, status: 'running', detail: '已提交到当前页面 Agent Session，正在分析' }
        pendingFlowAnalysisRef.current = next
        setPendingFlowAnalysis(next)
        return
      }
      if (status === 'rejected') {
        const message = text(detail.message, '页面助手未接受本次分析任务')
        flowCalibrationRunRef.current = null
        setBusy(null)
        setNotice({ tone: 'error', message })
        const next: PendingFlowAnalysis = { ...current, status: 'failed', detail: message }
        pendingFlowAnalysisRef.current = next
        setPendingFlowAnalysis(next)
      }
    }
    window.addEventListener(agentAutonomousTaskStatusEventName, listener)
    return () => window.removeEventListener(agentAutonomousTaskStatusEventName, listener)
  }, [])

  async function requestCodeGraphBuild(scopeView = sourceScopeViews[0]) {
    if (!scopeView) {
      setNotice({ tone: 'error', message: '请先选择一个已登记代码仓' })
      return
    }
    const { connector, subpath: scopeSubpath, graph } = scopeView
    setBusy('building')
    setNotice({ tone: 'info', message: `正在为“${connector.display_name}”启动模块代码知识底图构建` })
    try {
      const result = await api<{ status: string; graph: CodeGraph }>('/v1/code-graphs/builds', {
        method: 'POST',
        body: JSON.stringify({
          source_connector_id: connector.connector_id,
          subpath: scopeSubpath,
          max_files: 50_000,
          force_new: graph?.status === 'ready',
        }),
      })
      if (connector.connector_id === selectedConnectorId) setSelectedCodeGraphId(result.graph.graph_id)
      await refreshCodeGraphs()
      setNotice({
        tone: result.graph.status === 'ready' ? 'success' : 'info',
        message: result.graph.status === 'ready' ? `已复用“${connector.display_name}”当前源码快照的代码知识底图` : `“${connector.display_name}”底图已进入后台构建`,
      })
    } catch (error) {
      setNotice({ tone: 'error', message: error instanceof Error ? error.message : '代码知识底图构建启动失败' })
    } finally {
      setBusy(null)
    }
  }

  function addRelatedSourceScope() {
    const next = connectors.find((connector) => !sourceScopeConnectorIds.has(connector.connector_id))
    if (!next) {
      setNotice({ tone: 'info', message: '没有更多可添加的已登记代码仓' })
      return
    }
    setRelatedSourceScopes((current) => [...current, { sourceConnectorId: next.connector_id, subpath: '' }])
  }

  function updateRelatedSourceScope(index: number, patch: Partial<RelatedSourceScope>) {
    setRelatedSourceScopes((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item))
  }

  async function refreshSourceSnapshot() {
    if (!activeConnector || activeConnector.kind !== 'local') {
      setNotice({ tone: 'error', message: '当前代码仓不支持刷新本地工作树快照' })
      return
    }
    setBusy('loading')
    try {
      await api(`/v1/source-connectors/${encodeURIComponent(activeConnector.connector_id)}/refresh`, { method: 'POST' })
      await onRefreshConnectors()
      await refreshCodeGraphs()
      setNotice({ tone: 'success', message: '源码快照已刷新；内容变化后请为当前模块重新构建底图' })
    } catch (error) {
      setNotice({ tone: 'error', message: error instanceof Error ? error.message : '源码快照刷新失败' })
    } finally {
      setBusy(null)
    }
  }

  async function requestFlowAnalysis() {
    if (!activeConnector) {
      setNotice({ tone: 'error', message: '请先选择一个已登记代码仓' })
      return
    }
    if (!analysisGoal.trim()) {
      setNotice({ tone: 'error', message: '请先描述想分析的用户操作、触发点或完成边界' })
      return
    }
    const requestId = `application-flow:${randomUuid()}`
    const applicationFlowAnalysisId = `application-flow:${randomUuid()}`
    const submittedAt = new Date().toISOString()
    const structureOnly = confirmFlowBeforeTrace
    const expectsRuntime = !structureOnly && Boolean(activeRuntimeTraceId)
    activeFlowAnalysisIdRef.current = applicationFlowAnalysisId
    const pending: PendingFlowAnalysis = {
      requestId,
      applicationFlowAnalysisId,
      sourceConnectorId: activeConnector.connector_id,
      analysisGoal: analysisGoal.trim(),
      createdAt: submittedAt,
      scope: normalizedSubpath,
      expectsRuntime,
      status: 'submitting',
      detail: '正在提交到当前页面 Agent Session',
    }
    pendingFlowAnalysisRef.current = pending
    setPendingFlowAnalysis(pending)
    setSelectedDiscoveryId('')
    setSelectedFlowId('')
    setDiscovery(null)
    setSelectedScenarioId('')
    setScenario(null)
    setBusy('generating')
    setNotice({ tone: 'info', message: structureOnly ? '正在准备流程结构分析' : expectsRuntime ? '正在确认所选真实 Trace，并准备本次流程分析' : '正在准备本次流程分析' })
    let boundTrace: RuntimeTraceSummary | null = null
    if (!structureOnly) {
      try {
        boundTrace = await bindRuntimeTraceForAnalysis(applicationFlowAnalysisId)
      } catch (error) {
        const message = error instanceof Error ? error.message : '所选真实 Trace 无法在后端读取'
        const failed = { ...pending, status: 'failed' as const, detail: message }
        pendingFlowAnalysisRef.current = failed
        setPendingFlowAnalysis(failed)
        setBusy(null)
        setNotice({ tone: 'error', message: `${message}。请重新选择或上传 Trace 后再分析。` })
        await refreshRuntimeTraces().catch(() => undefined)
        return
      }
    }
    const boundTraceId = boundTrace?.artifact_id ?? ''
    const usesRuntimeTrace = Boolean(boundTraceId)
    const deliveryInstruction = structureOnly
      ? flowStructureTaskInstruction()
      : runtimeTraceTaskInstruction(boundTrace ?? undefined, boundTraceId)
    const analysisMethods = structureOnly
      ? '源码理解和调用关系追踪'
      : '源码理解、调用关系追踪和插桩识别'
    flowCalibrationRunRef.current = { expectsRuntime: usesRuntimeTrace, completesAfterCompile: structureOnly }
    setNotice({ tone: 'info', message: structureOnly ? '页面助手正在理解完整操作并生成待确认流程图' : usesRuntimeTrace ? '页面助手正在理解完整操作，并生成带真实 Trace 校准的阶段图' : '页面助手正在理解完整操作并生成阶段图' })
    window.dispatchEvent(new CustomEvent('page-agent-autonomous-task', {
      detail: {
        request_id: requestId,
        application_flow_analysis_id: applicationFlowAnalysisId,
        task_kind: 'application_flow_generation',
        source_connector_id: activeConnector.connector_id,
        source_snapshot_id: activeConnector.snapshot.snapshot_id,
        subpath: normalizedSubpath,
        graph_id: activeCodeGraph?.graph_id,
        trace_analysis_artifact_id: boundTraceId || undefined,
        source_scopes: sourceScopesPayload,
        required_skill_id: 'application-execution-flow-reconstruction',
        message: `直接分析并重建完整用户操作：${analysisGoal.trim()}。输入包括本轮 source_scopes 和可选代码底图。请自主选择${analysisMethods}方法，产出细粒度分层 Mermaid、标准 application-flow-scenario/v2 JSON，以及源码覆盖与缺口说明。完整性要求：用户描述中的每个明确动作都进入 action/phase/stage，源码范围缺失的动作以 coverage gap 保留，入口覆盖第一个动作，出口覆盖最终可观察结果。粒度要求：每个 stage 表达一个可独立定位的运行区间，每个 activity 表达一个代码级责任；当源码显示 UI 触发、跨层或异步派发、核心处理、完成回调、持久化属于不同边界时，用 @activity、@activity_relation 和必要的多个 stage 表达，并用 @implementation.activity_id 关联源码实现。节点数量由真实职责边界决定。首次编译后结合 compiler.granularity_observation 复核聚合节点，按语义决定是否修订。为兼容现有工件链，在形成整体判断后，用 application_flow_publish_candidates 登记唯一选定的完整操作并立即使用返回的 discovery_artifact_id、flow_id 编译 Mermaid。${deliveryInstruction}`,
        display_message: structureOnly ? `分析“${analysisGoal.trim()}”并生成待确认流程图` : usesRuntimeTrace ? `分析“${analysisGoal.trim()}”并用“${boundTrace?.trace_name || boundTraceId}”校准` : `分析“${analysisGoal.trim()}”并生成阶段图`,
      },
    }))
  }

  async function requestScenarioRevision() {
    if (!activeConnector || !discovery || !selectedFlow || !scenario || !revisionRequest.trim()) {
      setNotice({ tone: 'error', message: '请先选择一份流程阶段图，并填写希望修改的内容' })
      return
    }
    const applicationFlowAnalysisId = scenario.application_flow_analysis_id || discovery.application_flow_analysis_id
    activeFlowAnalysisIdRef.current = applicationFlowAnalysisId
    setBusy('generating')
    const structureOnly = confirmFlowBeforeTrace
    setNotice({ tone: 'info', message: structureOnly ? '正在准备流程结构修订' : activeRuntimeTraceId ? '正在确认所选真实 Trace，并准备修订阶段图' : '正在准备修订阶段图' })
    let boundTrace: RuntimeTraceSummary | null = null
    if (!structureOnly) {
      try {
        boundTrace = await bindRuntimeTraceForAnalysis(applicationFlowAnalysisId)
      } catch (error) {
        const message = error instanceof Error ? error.message : '所选真实 Trace 无法在后端读取'
        setBusy(null)
        setNotice({ tone: 'error', message: `${message}。请重新选择或上传 Trace 后再修订。` })
        await refreshRuntimeTraces().catch(() => undefined)
        return
      }
    }
    const boundTraceId = boundTrace?.artifact_id ?? ''
    const expectsRuntime = Boolean(boundTraceId)
    const deliveryInstruction = structureOnly
      ? flowStructureTaskInstruction()
      : runtimeTraceTaskInstruction(boundTrace ?? undefined, boundTraceId)
    flowCalibrationRunRef.current = { expectsRuntime, completesAfterCompile: structureOnly }
    setNotice({ tone: 'info', message: structureOnly ? '页面助手正在修订待确认流程图；旧版本会保留' : '页面助手正在修订阶段图，并连续更新源码与运行时边界证据；旧版本会保留' })
    window.dispatchEvent(new CustomEvent('page-agent-autonomous-task', {
      detail: {
        application_flow_analysis_id: applicationFlowAnalysisId,
        task_kind: 'application_flow_generation',
        source_connector_id: activeConnector.connector_id,
        source_snapshot_id: activeConnector.snapshot.snapshot_id,
        subpath: discovery.discovery.scope,
        graph_id: activeCodeGraph?.graph_id,
        discovery_artifact_id: discovery.artifact_id,
        flow_id: selectedFlow.id,
        scenario_artifact_id: scenario.artifact_id,
        trace_analysis_artifact_id: boundTraceId || undefined,
        source_scopes: sourceScopesPayload,
        required_skill_id: 'application-execution-flow-reconstruction',
        message: `根据用户反馈修订应用流程阶段图。scenario_artifact_id=${scenario.artifact_id}；discovery_artifact_id=${discovery.artifact_id}；flow_id=${selectedFlow.id}；修改要求=${revisionRequest.trim()}。请自主判断需要重读的源码范围、阶段粒度和证据工具，更新平台约定的 Mermaid；需要细化子阶段时，用 @activity、@activity_relation 和 @implementation.activity_id 保留细粒度活动及其执行关系。由 application_flow_compile_mermaid 生成新的 application-flow-scenario/v2 版本；保留 parent_scenario_artifact_id 与 revision_request。${deliveryInstruction}`,
        display_message: structureOnly ? `修订“${selectedFlow.name}”第 ${number(scenario.revision, 1)} 版流程图` : `修订并校准“${selectedFlow.name}”第 ${number(scenario.revision, 1)} 版`,
      },
    }))
  }

  async function requestRuntimeCalibration() {
    if (!activeConnector || !discovery || !selectedFlow || !scenario) {
      setNotice({ tone: 'error', message: '请先生成或选择一份流程阶段图' })
      return
    }
    if (!activeRuntimeTraceId) {
      setNotice({ tone: 'error', message: '请先选择或上传一份真实 Trace' })
      return
    }
    const applicationFlowAnalysisId = scenario.application_flow_analysis_id || discovery.application_flow_analysis_id
    activeFlowAnalysisIdRef.current = applicationFlowAnalysisId
    setBusy('generating')
    setNotice({ tone: 'info', message: '流程结构已确认，正在绑定所选 Trace 并准备运行时校准' })
    let boundTrace: RuntimeTraceSummary | null
    try {
      boundTrace = await bindRuntimeTraceForAnalysis(applicationFlowAnalysisId)
    } catch (error) {
      const message = error instanceof Error ? error.message : '所选真实 Trace 无法在后端读取'
      setBusy(null)
      setNotice({ tone: 'error', message: `${message}。请重新选择或上传 Trace 后再校准。` })
      await refreshRuntimeTraces().catch(() => undefined)
      return
    }
    const boundTraceId = boundTrace?.artifact_id ?? ''
    if (!boundTrace || !boundTraceId) {
      setBusy(null)
      setNotice({ tone: 'error', message: '所选真实 Trace 未能绑定到当前分析，请重新选择后再试' })
      return
    }
    flowCalibrationRunRef.current = { expectsRuntime: true, completesAfterCompile: false }
    setNotice({ tone: 'info', message: '页面助手正在核对源码插桩、校准真实事件并生成分析预览' })
    window.dispatchEvent(new CustomEvent('page-agent-autonomous-task', {
      detail: {
        request_id: `application-flow-calibration:${randomUuid()}`,
        application_flow_analysis_id: applicationFlowAnalysisId,
        task_kind: 'application_flow_generation',
        source_connector_id: activeConnector.connector_id,
        source_snapshot_id: activeConnector.snapshot.snapshot_id,
        subpath: discovery.discovery.scope,
        graph_id: activeCodeGraph?.graph_id,
        discovery_artifact_id: discovery.artifact_id,
        flow_id: selectedFlow.id,
        scenario_artifact_id: scenario.artifact_id,
        trace_analysis_artifact_id: boundTraceId,
        source_scopes: sourceScopesPayload,
        required_skill_id: 'application-execution-flow-reconstruction',
        message: `用户已确认当前流程结构，继续完成运行时证据校准与分析预览。scenario_artifact_id=${scenario.artifact_id}；discovery_artifact_id=${discovery.artifact_id}；flow_id=${selectedFlow.id}。以当前 Mermaid 和标准 JSON 为基线，自主核对源码 HiTrace 插桩与完整 event_name，形成 stage 与可观察 activity 的运行时边界、各层时长、热点分析预览和证据缺口；运行时证据揭示结构矛盾时，说明原因并做有证据的最小修订。${runtimeTraceTaskInstruction(boundTrace, boundTraceId)}`,
        display_message: `确认“${selectedFlow.name}”流程并用“${boundTrace.trace_name || boundTraceId}”校准`,
      },
    }))
  }

  async function deleteFlowAsset() {
    if (!pendingDelete) return
    setBusy('loading')
    try {
      await api(`/v1/application-flows/${encodeURIComponent(pendingDelete.artifactType)}/${encodeURIComponent(pendingDelete.artifactId)}?cascade=${pendingDelete.cascade ? 'true' : 'false'}`, { method: 'DELETE' })
      const deletedScenario = pendingDelete.artifactType === 'application_flow_scenario'
      setPendingDelete(null)
      if (deletedScenario && selectedScenarioId === pendingDelete.artifactId) {
        setSelectedScenarioId('')
        setScenario(null)
      }
      if (!deletedScenario && selectedDiscoveryId === pendingDelete.artifactId) {
        setSelectedDiscoveryId('')
        setSelectedFlowId('')
        setDiscovery(null)
        setSelectedScenarioId('')
        setScenario(null)
      }
      await refreshCatalog()
      setNotice({ tone: 'success', message: deletedScenario ? '流程阶段图版本已从资产列表删除' : '流程发现及其关联场景已从资产列表删除' })
    } catch (error) {
      setNotice({ tone: 'error', message: error instanceof Error ? error.message : '删除应用流程工件失败' })
    } finally {
      setBusy(null)
    }
  }

  async function importRuntimeTrace(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    if (!file) return
    const lowerName = file.name.toLowerCase()
    if (!lowerName.endsWith('.htrace') && !lowerName.endsWith('.json')) {
      setNotice({ tone: 'error', message: '运行时校准只接受原始 .htrace 或阶段分析 JSON 文件' })
      event.target.value = ''
      return
    }
    if (!activeConnector) {
      setNotice({ tone: 'error', message: '请先选择主代码仓，再导入真实 Trace' })
      event.target.value = ''
      return
    }
    setBusy('importing-trace')
    setNotice({ tone: 'info', message: `正在上传 ${file.name} · ${formatBytes(file.size)}` })
    try {
      const formData = new FormData()
      formData.append('file', file)
      formData.append('source_connector_id', activeConnector.connector_id)
      const response = await uploadWithProgress<{ artifact: { artifact_id: string } }>(
        '/v1/projects/local-default/trace-imports/upload',
        formData,
        (progress) => {
          if (progress.phase === 'processing') {
            setNotice({ tone: 'info', message: lowerName.endsWith('.htrace')
              ? '上传完成，TraceStreamer 正在转换并建立事件索引；大型 Trace 首次处理通常需要数分钟'
              : '上传完成，正在规范化阶段与事件数据' })
            return
          }
          const percent = progress.ratio === null ? '' : ` · ${Math.min(100, Math.round(progress.ratio * 100))}%`
          const bytes = progress.total > 0 ? `（${formatBytes(progress.loaded)} / ${formatBytes(progress.total)}）` : ` · ${formatBytes(progress.loaded)}`
          setNotice({ tone: 'info', message: `正在上传 ${file.name}${percent}${bytes}` })
        },
      )
      await refreshRuntimeTraces(response.artifact.artifact_id)
      setNotice({ tone: 'success', message: '真实 Trace 已导入并自动选中；同名历史上传仍保留在列表中' })
    } catch (error) {
      setNotice({ tone: 'error', message: error instanceof Error ? error.message : '真实 Trace 导入失败' })
    } finally {
      setBusy(null)
      event.target.value = ''
    }
  }

  async function deleteRuntimeTrace() {
    if (!pendingTraceDelete) return
    setBusy('loading')
    try {
      await api(`/v1/application-flows/runtime-traces/${encodeURIComponent(pendingTraceDelete.artifact_id)}`, { method: 'DELETE' })
      if (selectedRuntimeTraceId === pendingTraceDelete.artifact_id) setSelectedRuntimeTraceId('')
      setPendingTraceDelete(null)
      await refreshRuntimeTraces()
      setNotice({ tone: 'success', message: '该 Trace 已从应用流程输入列表删除；既有审计工件与历史分析结果仍保留' })
    } catch (error) {
      setNotice({ tone: 'error', message: error instanceof Error ? error.message : '删除真实 Trace 失败' })
    } finally {
      setBusy(null)
    }
  }

  const scenarioQuality = record(scenario?.quality)
  const stages = Array.isArray(scenario?.scenario_json.stages) ? scenario.scenario_json.stages.map(record) : []
  const stageHierarchy = normalizedStageHierarchy(record(scenario?.stage_hierarchy), stages, text(scenario?.flow_name, scenario?.flow_id ?? ''))
  const activityCount = records(stageHierarchy.activities).length
  const stageAnalysis = record(scenario?.stage_analysis)
  const stageAnalysisSummary = record(stageAnalysis.summary)
  const stageAnalysisStages = records(stageAnalysis.stages)
  const stageAnalysisNodes = records(stageAnalysis.nodes)
  const errorForwarded = useWorkbenchErrorNotification(notice, () => setNotice(null), 'application-flow')
  return <div className="workspace-scroll application-flow-scroll">
    <section className="work-section application-flow-controls">
      <div className="section-heading">
        <div><span className="eyebrow">Agent 驱动的应用流程重建</span><h2>从用户操作直接生成阶段图与运行时证据</h2></div>
        <button className="icon-button" type="button" title="刷新底图、应用流程与真实 Trace 资产" aria-label="刷新底图、应用流程与真实 Trace 资产" onClick={() => void Promise.all([refreshCatalog(), refreshCodeGraphs(), refreshRuntimeTraces(), refreshCaptureTraceSources()])}><RefreshCw size={15} /></button>
      </div>
      <div className="application-flow-source-scope-heading">
        <div><span className="eyebrow">第 1 步 · 分析输入与知识底图</span><strong>{sourceScopeViews.length} 个代码仓范围</strong></div>
        <div className="application-flow-source-scope-actions">
          <button className="quiet-button" type="button" disabled={busy !== null || sourceScopeViews.length >= Math.min(connectors.length, 12)} onClick={addRelatedSourceScope}><Plus size={15} />添加代码仓</button>
          {runtimeTraceUploadEnabled && <>
            <input ref={runtimeTraceInputRef} className="visually-hidden" type="file" accept=".htrace,.json,application/json" onChange={(event) => void importRuntimeTrace(event)} />
            <button className="quiet-button" type="button" disabled={busy !== null || !activeConnector} onClick={() => runtimeTraceInputRef.current?.click()}>{busy === 'importing-trace' ? <LoaderCircle size={15} className="spin" /> : <Upload size={15} />}添加 Trace</button>
          </>}
        </div>
      </div>
      <div className="application-flow-source-scope-list">
        {sourceScopeViews.map((scopeView, scopeIndex) => {
          const scopeGraph = scopeView.graph
          const scopeSummary = record(scopeGraph?.summary)
          const scopeProgress = record(scopeGraph?.progress)
          const scopeError = record(scopeGraph?.error)
          return <article className={`application-flow-source-scope ${scopeView.role}`} key={scopeView.connector.connector_id}>
            <div className="application-flow-source-scope-controls">
              <label><span>{scopeIndex === 0 ? '主代码仓' : `关联代码仓 ${scopeIndex}`}</span><select value={scopeView.connector.connector_id} onChange={(event) => scopeIndex === 0 ? onSelectConnector(event.target.value) : updateRelatedSourceScope(scopeIndex - 1, { sourceConnectorId: event.target.value })}>{connectors.map((connector) => <option key={connector.connector_id} value={connector.connector_id} disabled={sourceScopeConnectorIds.has(connector.connector_id) && connector.connector_id !== scopeView.connector.connector_id}>{connector.display_name} · {connector.source_root}</option>)}</select></label>
              <label><span>分析目录</span><input value={scopeView.subpath} onChange={(event) => scopeIndex === 0 ? setSubpath(event.target.value) : updateRelatedSourceScope(scopeIndex - 1, { subpath: event.target.value })} placeholder="代码仓内相对目录，可留空" /></label>
              <button className="quiet-button" type="button" disabled={busy !== null || ['queued', 'running'].includes(scopeGraph?.status ?? '')} onClick={() => void requestCodeGraphBuild(scopeView)}>{busy === 'building' || ['queued', 'running'].includes(scopeGraph?.status ?? '') ? <LoaderCircle size={15} className="spin" /> : <Database size={15} />}{scopeGraph?.status === 'ready' ? '重建底图' : '构建底图'}</button>
              {scopeIndex > 0 && <button className="icon-button danger" type="button" title="移除该关联代码仓" aria-label="移除该关联代码仓" disabled={busy !== null} onClick={() => setRelatedSourceScopes((current) => current.filter((_, index) => index !== scopeIndex - 1))}><X size={14} /></button>}
            </div>
            <div className="application-flow-source-scope-status">
              <GitBranch size={14} /><span title={scopeView.connector.snapshot.snapshot_id}>{scopeView.connector.snapshot.snapshot_id}</span><strong>{scopeGraph ? graphStatusLabel(scopeGraph.status) : '尚无底图'}</strong>
              {scopeGraph && <div className="application-flow-code-graph-metrics"><span><strong>{number(scopeSummary.files_parsed, number(scopeProgress.files_parsed))}</strong> 文件</span><span><strong>{number(scopeSummary.symbol_count)}</strong> 符号</span><span><strong>{number(scopeSummary.call_candidate_count)}</strong> 调用候选</span><span><strong>{number(scopeSummary.tracepoint_count)}</strong> 插桩</span></div>}
              {scopeIndex === 0 && scopeView.connector.kind === 'local' && <button className="icon-button" type="button" title="重新计算主代码仓工作树源码快照" aria-label="重新计算主代码仓工作树源码快照" disabled={busy !== null} onClick={() => void refreshSourceSnapshot()}><RefreshCw size={14} /></button>}
            </div>
            {text(scopeError.message) && <div className="application-flow-graph-error"><CircleAlert size={14} /><span>{text(scopeError.message)}</span></div>}
          </article>
        })}
      </div>
      <div className="application-flow-trace-input">
        <FileJson2 size={16} />
        <label><span>真实 Trace</span><select value={selectedRuntimeTraceId} disabled={busy === 'importing-trace'} onChange={(event) => void selectRuntimeTrace(event.target.value)}><option value="">暂不选择 Trace</option>{availableRuntimeTraces.length > 0 && <optgroup label="服务器真实 Trace">{availableRuntimeTraces.map((item) => <option key={item.artifact_id} value={item.artifact_id}>{formatTraceUploadTime(item.uploaded_at || item.created_at)} · {item.trace_name || item.artifact_id} · {item.artifact_id.slice(-6)} · {item.input_kind === 'raw_htrace' ? '原始 Trace' : `${item.stage_count} 个报告阶段`}</option>)}</optgroup>}{captureTraceSources.length > 0 && <optgroup label="本机 Trace 管理">{captureTraceSources.map((item) => <option key={`${item.traceId}:${item.stepId}`} value={`capture:${item.traceId}:${item.stepId}`} disabled={!item.ready}>{item.planTitle ? `[${item.planTitle}] ` : ''}{item.stepOrdinal ? `Step ${item.stepOrdinal}: ` : ''}{item.stepTitle}{item.ready ? ' · 选择后物化事件与热点' : ` · ${item.reason || '不可分析'}`}</option>)}</optgroup>}</select></label>
        <span>{availableRuntimeTraces.length} 份已物化{captureTraceSources.length ? ` · ${captureTraceSources.filter(item => item.ready).length} 份本机采集` : ''}</span>
      </div>
      {selectedRuntimeTrace && <div className="application-flow-runtime-trace-selection application-flow-input-trace-selection"><FileJson2 size={15} /><span><strong>{selectedRuntimeTrace.trace_name || selectedRuntimeTrace.artifact_id}</strong><small>{formatTraceUploadTime(selectedRuntimeTrace.uploaded_at || selectedRuntimeTrace.created_at)} · {selectedRuntimeTrace.artifact_id} · {selectedRuntimeTrace.input_kind === 'raw_htrace' ? '原始 .htrace' : '阶段分析 JSON'} · {confirmFlowBeforeTrace ? '流程确认后用于精确事件名校准' : '生成阶段图时用于精确事件名校准'}</small></span><button className="icon-button danger" type="button" disabled={busy !== null} title="从应用流程 Trace 列表删除" aria-label={`删除 Trace ${selectedRuntimeTrace.trace_name || selectedRuntimeTrace.artifact_id}`} onClick={() => setPendingTraceDelete(selectedRuntimeTrace)}><Trash2 size={14} /></button></div>}
      <label className="application-flow-analysis-mode">
        <input type="checkbox" checked={confirmFlowBeforeTrace} disabled={savingFlowPreference || busy !== null} onChange={(event) => void updateFlowAnalysisMode(event.target.checked)} />
        <span><strong>先确认流程图，再校准 Trace</strong><small>首次仅生成 Mermaid 与标准 JSON；确认流程结构后，再生成运行时校准和分析预览。{savingFlowPreference ? ' 正在保存设置...' : ''}</small></span>
      </label>
      <div className="application-flow-intent-controls">
        <label><span>想分析的完整用户操作 / 流程边界</span><textarea rows={2} value={analysisGoal} onChange={(event) => setAnalysisGoal(event.target.value)} placeholder="例如：用户打开相机、切换人像模式、点击拍照，直到照片保存并可在图库查看。" /></label>
        <button className="primary-button" type="button" title={confirmFlowBeforeTrace ? '先生成流程图，确认后再校准 Trace' : selectedRuntimeTrace ? `使用“${selectedRuntimeTrace.trace_name || selectedRuntimeTrace.artifact_id}”分析并生成阶段图` : '分析并生成阶段图'} disabled={!activeConnector || !analysisGoal.trim() || busy !== null} onClick={() => void requestFlowAnalysis()}>{busy === 'generating' ? <LoaderCircle size={15} className="spin" /> : <Sparkles size={15} />}{confirmFlowBeforeTrace ? '生成待确认流程图' : selectedRuntimeTrace ? '使用所选 Trace 分析并生成阶段图' : '分析并生成阶段图'}</button>
      </div>
      {notice && !errorForwarded && <button type="button" className={`application-flow-notice ${notice.tone}`} title={busy ? '关闭提示并解除当前页面等待状态' : '关闭提示'} onClick={() => { flowCalibrationRunRef.current = null; setNotice(null); setBusy(null) }}>{notice.tone === 'error' ? <CircleAlert size={15} /> : notice.tone === 'success' ? <Check size={15} /> : <LoaderCircle size={15} className={busy ? 'spin' : ''} />}<span>{notice.message}</span></button>}
    </section>

    <section className="work-section application-flow-history" aria-label="应用流程分析记录">
      <div className="application-flow-history-heading">
        <div><History size={17} /><span><strong>应用流程分析记录</strong><small>完整分析独立保存；后续修改作为子版本保留</small></span></div>
        <span>{historyDiscoveries.length + (visiblePendingFlowAnalysis ? 1 : 0)} 次分析 · {historyScenarios.length} 个结果版本</span>
      </div>
      {visiblePendingFlowAnalysis || historyDiscoveries.length > 0 ? <div className="application-flow-history-list">
        {visiblePendingFlowAnalysis && <article className={`application-flow-history-pending${!selectedDiscoveryId ? ' selected' : ''} is-${visiblePendingFlowAnalysis.status}`}>
          <div className="application-flow-history-record">
            <button type="button" className="application-flow-history-open" onClick={() => {
              setSelectedDiscoveryId('')
              setSelectedFlowId('')
              setDiscovery(null)
              setSelectedScenarioId('')
              setScenario(null)
            }}>
              <span className="application-flow-history-icon">{visiblePendingFlowAnalysis.status === 'failed' ? <CircleAlert size={16} /> : <LoaderCircle size={16} className="spin" />}</span>
              <span><strong>{visiblePendingFlowAnalysis.analysisGoal}</strong><small>{formatTime(visiblePendingFlowAnalysis.createdAt)} · {visiblePendingFlowAnalysis.detail} · {visiblePendingFlowAnalysis.scope || '仓根目录'}{visiblePendingFlowAnalysis.expectsRuntime ? ' · 使用真实 Trace' : ''}</small></span>
            </button>
            <span className="application-flow-history-pending-state" title={visiblePendingFlowAnalysis.detail}>{visiblePendingFlowAnalysis.status === 'failed' ? '失败' : visiblePendingFlowAnalysis.status === 'running' ? '分析中' : '提交中'}</span>
          </div>
        </article>}
        {historyDiscoveries.map((item) => {
          const childScenarios = discoveryScenarioLineage(historyScenarios, item)
          const latestScenario = childScenarios[0]
          const selected = selectedDiscoveryId === item.artifact_id
          const recordConnector = connectors.find((connector) => connector.connector_id === item.source_connector_id)
          const historicalSnapshot = Boolean(recordConnector && item.source_snapshot_id !== recordConnector.snapshot.snapshot_id)
          const sourceScopeCount = Math.max(1, item.source_scopes?.length ?? 0)
          return <article className={selected ? 'selected' : ''} key={item.artifact_id}>
            <div className="application-flow-history-record">
              <button type="button" className="application-flow-history-open" disabled={busy !== null} onClick={() => void loadAnalysisRecord(item, latestScenario)}>
                <span className="application-flow-history-icon"><Route size={16} /></span>
                <span><strong>{item.analysis_goal || item.flows[0]?.name || item.scope || '未命名流程分析'}</strong><small>{formatTime(item.created_at)} · {sourceScopeCount} 个代码仓 · {childScenarios.length} 个结果版本 · {normalizeScope(item.scope) || '仓根目录'} · {recordConnector ? historicalSnapshot ? '历史快照' : '当前快照' : '代码仓未登记'}{latestScenario?.runtime_trace_analysis_artifact_id ? ' · 含真实 Trace' : ''}</small></span>
              </button>
              <button className="icon-button danger" type="button" title="删除该分析记录及其阶段图版本" aria-label="删除该分析记录及其阶段图版本" disabled={busy !== null} onClick={() => setPendingDelete({ artifactType: 'application_flow_discovery', artifactId: item.artifact_id, label: item.analysis_goal || item.flows[0]?.name || '该流程分析', cascade: true })}><Trash2 size={14} /></button>
            </div>
            {childScenarios.length > 0 && <div className="application-flow-history-versions">
              {childScenarios.map((child) => <div className={selectedScenarioId === child.artifact_id ? 'selected' : ''} key={child.artifact_id}>
                <button type="button" disabled={busy !== null} onClick={() => void loadAnalysisRecord(item, child)}><Network size={13} /><span><strong>v{number(child.revision, 1)} · {child.flow_name || child.scenario}</strong><small>{child.stage_count} 阶段 · {formatTime(child.created_at)}{child.runtime_trace_analysis_artifact_id ? ' · 含真实 Trace' : ''}{child.has_stage_analysis ? ' · 含分析预览' : ''}</small></span></button>
                <button className="icon-button danger" type="button" title="删除该场景版本" aria-label="删除该场景版本" disabled={busy !== null} onClick={() => setPendingDelete({ artifactType: 'application_flow_scenario', artifactId: child.artifact_id, label: `${child.flow_name || child.scenario} v${number(child.revision, 1)}`, cascade: false })}><Trash2 size={13} /></button>
              </div>)}
            </div>}
          </article>
        })}
      </div> : <div className="application-flow-history-empty"><History size={19} /><span>完成一次流程分析后，阶段图及其版本会保存在这里。</span></div>}
    </section>

    <section className="application-flow-output" aria-label="应用流程产物">
        <div className="application-flow-pane-heading"><div><span className="eyebrow">第 2 步 · 阶段图与证据校准</span><h3>流程阶段图、源码插桩与真实 Trace</h3></div>{scenario && <span>{number(record(stageHierarchy.quality).action_count)} 动作 · {number(record(stageHierarchy.quality).phase_count)} 大阶段 · {stages.length} 子阶段{activityCount > 0 ? ` · ${activityCount} 细粒度活动` : ''}</span>}</div>
        {matchingScenarios.length > 0 && <label className="application-flow-artifact-select"><span>场景版本</span><select value={selectedScenarioId} onChange={(event) => void openScenario(event.target.value)}>{matchingScenarios.map((item) => <option key={item.artifact_id} value={item.artifact_id}>v{number(item.revision, 1)} · {formatTime(item.created_at)} · {item.scenario} · {item.stage_count} 阶段{item.has_stage_analysis ? ' · 含分析预览' : ''}</option>)}</select></label>}
        {scenario ? <>
          <div className="application-flow-output-summary">
            <div><Network size={17} /><span><strong>{text(scenario.flow_name, scenario.flow_id)}</strong><small>{scenario.artifact_id}</small></span></div>
            <span className={`application-flow-quality ${text(scenarioQuality.executable_status) === 'ready' ? 'ready' : ''}`}>{qualityLabel(scenarioQuality.executable_status, scenarioQuality.anchor_evidence_scope)}</span>
          </div>
          {stageAnalysisStages.length === 0 && <div className="application-flow-calibration-gate">
            <Check size={17} />
            <span><strong>请先确认流程结构</strong><small>核对动作边界、阶段拆分与关键调用。确认无误后，使用所选 Trace 生成边界校准和分析预览。</small></span>
            <button className="primary-button" type="button" disabled={busy !== null || !activeRuntimeTraceId} title={activeRuntimeTraceId ? '确认当前流程结构并开始真实 Trace 校准' : '请先在第 1 步选择或上传真实 Trace'} onClick={() => void requestRuntimeCalibration()}>{busy === 'generating' ? <LoaderCircle size={15} className="spin" /> : <BarChart3 size={15} />}{activeRuntimeTraceId ? '确认并校准 Trace' : '请先选择 Trace'}</button>
          </div>}
          <div className="trace-explorer-tabs application-flow-tabs" role="tablist" aria-label="流程产物视图">
            <button type="button" className={detailTab === 'hierarchy' ? 'selected' : ''} onClick={() => setDetailTab('hierarchy')}><Network size={15} />分层结构</button>
            <button type="button" className={detailTab === 'diagram' ? 'selected' : ''} onClick={() => setDetailTab('diagram')}><Route size={15} />流程图</button>
            <button type="button" className={detailTab === 'evidence' ? 'selected' : ''} onClick={() => setDetailTab('evidence')}><Search size={15} />边界证据</button>
            <button type="button" className={detailTab === 'json' ? 'selected' : ''} onClick={() => setDetailTab('json')}><FileJson2 size={15} />JSON</button>
            <button type="button" className={detailTab === 'analysis-preview' ? 'selected' : ''} onClick={() => void openStageAnalysis('analysis-preview')}><BarChart3 size={15} />分析预览</button>
            <button type="button" className={detailTab === 'analysis-json' ? 'selected' : ''} onClick={() => void openStageAnalysis('analysis-json')}><FileJson2 size={15} />分析预览 JSON</button>
          </div>
          {detailTab === 'hierarchy'
            ? <StageHierarchyView hierarchy={stageHierarchy} stages={stages} />
            : detailTab === 'diagram'
              ? <MermaidDiagram source={scenario.mermaid} />
              : detailTab === 'evidence'
                ? <FlowEvidenceView scenario={scenario} stages={stages} selectedRuntimeTrace={selectedRuntimeTrace} selectedRuntimeTraceId={activeRuntimeTraceId} />
                : detailTab === 'json'
                  ? <pre className="application-flow-json">{JSON.stringify(scenario.scenario_json, null, 2)}</pre>
                  : detailTab === 'analysis-preview'
                    ? stageAnalysisStages.length > 0
                      ? <div className="application-flow-stage-analysis">
                        <div className="application-flow-runtime-summary">
                          <div><BarChart3 size={16} /><span><strong>{text(stageAnalysisSummary.trace_name, '真实 Trace 阶段分析')}</strong><small>{text(stageAnalysisSummary.trace_analysis_artifact_id)}</small></span></div>
                          <div><span>已计时节点 <strong>{number(stageAnalysisSummary.timed_nodes, number(stageAnalysisSummary.timed_stages))} / {number(stageAnalysisSummary.total_nodes, number(stageAnalysisSummary.total_stages))}</strong></span><span>全流程包络 <strong>{durationLabel(stageAnalysisSummary.trace_window_duration_ms)}</strong></span><span>事件 <strong>{number(stageAnalysisSummary.total_event_count)}</strong></span></div>
                        </div>
                        <TraceStageTimeline stages={stageAnalysisStages} nodes={stageAnalysisNodes} />
                      </div>
                      : <div className="application-flow-empty output"><BarChart3 size={24} /><strong>尚无阶段负载分析</strong><span>选择真实 Trace 并完成运行时证据校准后，平台会生成阶段时长、相对时间轴和阶段内热点。</span></div>
                    : stageAnalysisStages.length > 0
                      ? <pre className="application-flow-json">{JSON.stringify(stageAnalysis, null, 2)}</pre>
                      : <div className="application-flow-empty output"><FileJson2 size={24} /><strong>尚无分析预览 JSON</strong><span>该 JSON 是真实 Trace 校准后的独立侧车，不会改变正式 application-flow-scenario/v2 JSON。</span></div>}
          {artifactDownloadsEnabled && <div className="application-flow-downloads">
            <a className="quiet-button" href={`${apiBase}/v1/application-flows/scenarios/${encodeURIComponent(scenario.artifact_id)}/mermaid`}><ArrowDownToLine size={14} />Mermaid</a>
            <a className="quiet-button" href={`${apiBase}/v1/application-flows/scenarios/${encodeURIComponent(scenario.artifact_id)}/json`}><ArrowDownToLine size={14} />JSON</a>
            {stageAnalysisStages.length > 0 && <a className="quiet-button" href={`${apiBase}/v1/application-flows/scenarios/${encodeURIComponent(scenario.artifact_id)}/stage-analysis`}><ArrowDownToLine size={14} />分析 JSON</a>}
          </div>}
          <FlowAnalysisSummary scenario={scenario} hierarchy={stageHierarchy} stages={stages} actionCoverage={selectedActionCoverage} />
          <div className="application-flow-revision-box">
            <div><PencilLine size={16} /><span><strong>继续修改当前版本</strong><small>基于 v{number(scenario.revision, 1)} 创建新版本，当前结果不会被覆盖。</small></span></div>
            <textarea rows={3} value={revisionRequest} onChange={(event) => setRevisionRequest(event.target.value)} placeholder="例如：把预览启动拆成会话配置、流提交和首帧显示三个子阶段，并补充对应函数调用。" />
            <button className="primary-button" type="button" disabled={busy !== null || !revisionRequest.trim()} onClick={() => void requestScenarioRevision()}>{busy === 'generating' ? <LoaderCircle size={15} className="spin" /> : <MessageSquareText size={15} />}交给页面助手修改</button>
          </div>
        </> : <div className="application-flow-empty output"><Network size={24} /><strong>等待阶段图与证据产物</strong><span>Agent 在一次任务中生成分层 Mermaid、核对源码插桩，并在选择 Trace 时校准真实 event name；平台同步生成标准场景 JSON。</span></div>}
    </section>

    {pendingDelete && <div className="code-knowledge-modal-backdrop" role="presentation"><section className="code-knowledge-modal compact" role="dialog" aria-modal="true" aria-labelledby="application-flow-delete-title">
      <header><div><CircleAlert size={18} /><span><strong id="application-flow-delete-title">删除“{pendingDelete.label}”？</strong><small>{pendingDelete.cascade ? '该发现下的场景版本也会从列表删除；不可变审计工件仍会保留。' : '仅删除当前场景版本，不影响候选发现和其他版本。'}</small></span></div><button className="icon-button" type="button" title="关闭" onClick={() => setPendingDelete(null)}><X size={15} /></button></header>
      <footer><button className="quiet-button" type="button" onClick={() => setPendingDelete(null)}>取消</button><button className="primary-button danger" type="button" disabled={busy !== null} onClick={() => void deleteFlowAsset()}>{busy === 'loading' ? <LoaderCircle size={15} className="spin" /> : <Trash2 size={14} />}确认删除</button></footer>
    </section></div>}
    {pendingTraceDelete && <div className="code-knowledge-modal-backdrop" role="presentation"><section className="code-knowledge-modal compact" role="dialog" aria-modal="true" aria-labelledby="application-flow-trace-delete-title">
      <header><div><CircleAlert size={18} /><span><strong id="application-flow-trace-delete-title">删除“{pendingTraceDelete.trace_name || pendingTraceDelete.artifact_id}”？</strong><small>该上传会从应用流程 Trace 列表移除；既有流程结果和不可变审计工件仍会保留。</small></span></div><button className="icon-button" type="button" title="关闭" onClick={() => setPendingTraceDelete(null)}><X size={15} /></button></header>
      <footer><button className="quiet-button" type="button" onClick={() => setPendingTraceDelete(null)}>取消</button><button className="primary-button danger" type="button" disabled={busy !== null} onClick={() => void deleteRuntimeTrace()}>{busy === 'loading' ? <LoaderCircle size={15} className="spin" /> : <Trash2 size={14} />}确认删除</button></footer>
    </section></div>}
  </div>
}
