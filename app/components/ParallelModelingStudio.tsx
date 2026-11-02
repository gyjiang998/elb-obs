'use client'

import { Activity, ArrowDownToLine, ArrowRight, Boxes, ChartNoAxesCombined, Check, CircleAlert, Cpu, Database, FileUp, GitBranch, LoaderCircle, RefreshCw, Search, Sparkles, Square, X } from 'lucide-react'
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { formatBytes, platformApiFor } from '../lib/api'
import { agentAutonomousTaskStatusEventName } from '../lib/agent-command-bridge'
import type { JsonRecord } from '../lib/types'
import { focusWorkflowAction } from '../lib/workflow-focus'
import { restoreParallelSearchControls } from '../lib/parallel-search-controls'
import { buildParallelModelingReport, type ParallelModelingReportArtifact } from '../lib/parallel-modeling-report'
import { dispatchParallelAgent, downloadText, errorText, frequencyText, graphMermaid, isTerminal, jobPercent, metric, numeric, record, records, statusLabel, text, timestamp, useParallelJob, type CaptureRun, type CoreCombinationRecommendation, type JobReceipt, type ModelAnalysis, type ModelCandidate, type ModelingCatalog, type ModelingDataset, type ModelSearch, type Notice } from '../lib/parallel-modeling'
import { MermaidDiagram } from './MermaidDiagram'
import { useWorkbenchErrorNotification } from './WorkbenchErrorNotification'
import '../parallel-modeling.css'

export type ParallelModelingAssistantContext = {
  page_id: 'parallel_modeling'
  dataset_id: string | null
  analysis_id: string | null
  search_id: string | null
  job_id: string | null
  candidate_ids: string[]
  model_package_id: string | null
  feature_profile_id: string | null
  objective: string
  budget_ms: number | null
  search_space: JsonRecord
}
const api = platformApiFor('parallel-modeling')
const post = <T,>(path: string, body: unknown): Promise<T> => api<T>(path, { method: 'POST', body: JSON.stringify(body) })
const jobStorageKey = 'heterogeneous-parallel-modeling-active-job-v1'
const coreNames: Record<string, string> = { big: '大核', middle: '中核', small: '小核' }
function candidateName(candidate?: ModelCandidate | null): string {
  const config = record(candidate?.configuration)
  if (!Object.keys(config).length) return '候选配置'
  const main = coreNames[text(record(config.main).kind)] ?? text(record(config.main).kind, '主')
  const workers = records(config.workers)
  if (!workers.length) return `${main}主线程 · 串行对照`
  const worker = coreNames[text(workers[0].kind)] ?? text(workers[0].kind)
  const dispatch = config.dispatch === 'main_first' ? '主线程优先' : 'Worker 优先'
  return `${main}主线程 + ${workers.length} ${worker} Worker · ${config.main_participates ? '主线程参与' : '主线程调度'} · ${dispatch}`
}
function comparison(candidate: ModelCandidate | null | undefined, baseline: JsonRecord): JsonRecord { const existing = record(candidate?.comparison); const finish = numeric(record(candidate?.metrics).finish_ms); const original = numeric(baseline.duration_ms); const cpu = numeric(record(candidate?.metrics).app_core_ms); const originalCpu = numeric(baseline.app_cpu_ms); return { ...(finish !== null && original !== null ? { baseline_ms: original, latency_gain_ms: original - finish, latency_gain_pct: original > 0 ? 100 * (1 - finish / original) : null } : {}), ...(cpu !== null && originalCpu !== null ? { app_cpu_delta_ms: cpu - originalCpu } : {}), ...existing } }
function detailText(value: unknown): string { if (typeof value === 'string') return value; if (typeof value === 'number') return metric(value); if (value === null || value === undefined) return '—'; return JSON.stringify(value) }
function positiveInteger(value: string, label: string): number | undefined { if (!value.trim()) return undefined; const number = Number(value); if (!Number.isInteger(number) || number <= 0) throw new Error(`${label}必须为正整数。`); return number }
function pmuSummary(value: unknown): string { const pmu = record(value); if (pmu.bounds_valid === false) return `不可用 · ${text(pmu.reason, '缺少可对齐的计数边界')}`; if (pmu.feature_usable_under_25pct_instruction_boundary_rule === false) return '边界覆盖不足 · 仅保留计数范围'; const lower = record(pmu.lower); const upper = record(pmu.upper); const pairs = [numeric(pmu.interior_ipc) !== null ? `内部 IPC ${metric(pmu.interior_ipc, 3)}` : '', numeric(lower.instructions) !== null && numeric(upper.instructions) !== null ? `指令 ${metric(lower.instructions, 0)}–${metric(upper.instructions, 0)}` : ''].filter(Boolean); return pairs.length ? pairs.join(' · ') : '未关联可用 PMU 特征' }
function sameCandidate(a?: ModelCandidate | null, b?: ModelCandidate | null): boolean {
  if (!a || !b) return false
  return a === b || (numeric(a.configuration_index) !== null && a.configuration_index === b.configuration_index)
    || Boolean(a.id && a.id === b.id)
}
function sensitivityLabel(candidate: ModelCandidate, frequencyGoal = false): string {
  const count = numeric(record(candidate.metrics).scenario_count)
  const status = text(candidate.sensitivity_status)
  if (!frequencyGoal && count && count > 0) return `${count} 个扰动情景 · 最坏 ${metric(record(candidate.metrics).scenario_worst_ms)} ms`
  return ({ passed_budget: `${count ? `${count} 个` : ''}扰动情景内达标`, exceeds_budget: '扰动情景超出预算', not_evaluated: '稳健性未评估', not_requested: '未启用稳健性分析' } as Record<string, string>)[status] ?? text(status, '未返回稳健性状态')
}
function feasibilityLabel(candidate: ModelCandidate, frequencyGoal = false): string {
  if (candidate.selection_status === 'budget_infeasible') return '该组合无法达标'
  if (candidate.feasible === true) return '满足本次约束'
  if (frequencyGoal && candidate.meets_budget === false) return '超出时延预算'
  if (candidate.feasible === false) return '未满足稳健性约束'
  return '可行性待核验'
}
function datasetSourceLabel(dataset: ModelingDataset): string {
  return ({ capture_manifest: '负载采样', builtin_id: '历史归档', trace_text: 'Trace 导入', parsed: '解析结果' } as Record<string, string>)[text(dataset.source_kind)] ?? '已归档负载'
}
function archiveDatasetValue(id: string): string { return `dataset:${id}` }
function archiveBuiltinValue(id: string): string { return `builtin:${id}` }
function savedSearchLabel(search: ModelSearch): string {
  const objective = search.objective === 'minimum_latency' ? '最低时延' : '预算内最低频点'
  const finish = numeric(record(search.best?.metrics).finish_ms)
  return `${objective} · ${timestamp(search.created_at)}${finish === null ? '' : ` · ${metric(finish)} ms`}`
}
function OptimizationModelFormula({ objective, budget, robustness }: { objective: string; budget: string; robustness: boolean }) {
  const budgetValue = Number(budget)
  const budgetLabel = budgetValue > 0 ? `${metric(budgetValue)} ms` : 'B'
  const frequencyGoal = objective === 'minimum_frequency_with_budget'
  const priorities = frequencyGoal
    ? [
      ['01', '最高归一化频点', '先压低任一活动核组的最高频率'],
      ['02', '频点向量总和', '同等最高频点下继续降低整体频率'],
      ['03', '占用核槽', '频点相同时减少使用的物理核'],
      ['04', '预测时延', '资源相同时选择更短的回调耗时'],
      ['05', '稳定序号', '仅用于结果稳定决胜'],
    ]
    : [
      ['01', '预测时延', '最小化 UI 回调的 DAG 完成时间'],
      ['02', '占用核槽', '时延相同时减少使用的物理核'],
      ['03', '应用 CPU', '进一步降低应用 CPU core-ms'],
      ['04', '稳定序号', '仅用于结果稳定决胜'],
    ]
  return <div className="pm-optimization-model" aria-label={frequencyGoal ? '预算内最低频点优化模型' : '最短时延优化模型'}>
    <div className="pm-optimization-model-heading">
      <div><span className="pm-optimization-kicker">OPTIMIZATION MODEL</span><strong>本次优化模型</strong><small>目标、约束与求解顺序跟随当前搜索设置</small></div>
      <span className={`pm-optimization-kind ${frequencyGoal ? 'frequency' : 'latency'}`}>{frequencyGoal ? '预算内最低频点' : '最短 UI 回调耗时'} · 字典序</span>
    </div>
    <div className="pm-optimization-core">
      <section className="pm-optimization-target" aria-label="目标函数">
        <span className="pm-optimization-section-label">目标函数</span>
        {frequencyGoal
          ? <div className="pm-optimization-equation primary"><b>lex min</b><code>ₓ ( max<sub>g∈A(x)</sub> r<sub>g</sub>, Σ<sub>g∈A(x)</sub> r<sub>g</sub>, N<sub>slot</sub>(x), T̂<sub>UI</sub>(x), i(x) )</code></div>
          : <div className="pm-optimization-equation primary"><b>lex min</b><code>ₓ ( T̂<sub>UI</sub>(x), N<sub>slot</sub>(x), C<sub>app</sub>(x), i(x) )</code></div>}
        <p>{frequencyGoal ? '在满足回调预算的前提下，按字典序压低活动核组频点与占用资源。' : '以模型预测的 UI 回调 DAG 完成时间为首要目标，资源代价只参与同等时延方案的决胜。'}</p>
      </section>
      <section className="pm-optimization-constraints" aria-label="约束集合">
        <span className="pm-optimization-section-label">约束集合</span>
        <div className="pm-optimization-constraint-list">
          {frequencyGoal && <div className="pm-optimization-equation"><b>s.t.</b><code>T̂<sub>UI</sub>(x) ≤ {budgetLabel}</code></div>}
          {frequencyGoal && robustness && <div className="pm-optimization-equation"><b>and</b><code>max<sub>s∈S<sub>decl</sub></sub> T̂<sub>UI</sub>(x; s) ≤ {budgetLabel}</code></div>}
          <div className="pm-optimization-equation"><b>{frequencyGoal ? 'and' : 's.t.'}</b><code>x ∈ Ω<sub>current</sub></code></div>
        </div>
        <p>{frequencyGoal ? `候选配置与${robustness ? '声明扰动情景' : '名义情景'}都需通过预算门。` : '本目标不施加时延预算；候选必须属于本次冻结的可行域。'}</p>
      </section>
    </div>
    <section className="pm-optimization-priority" aria-label="字典序优先级">
      <span className="pm-optimization-section-label">字典序优先级</span>
      <ol>{priorities.map(([index, name, description]) => <li key={index}><b>{index}</b><span><strong>{name}</strong><small>{description}</small></span></li>)}</ol>
    </section>
    <section className="pm-optimization-solver" aria-label="两级求解流程">
      <span className="pm-optimization-section-label">两级求解</span>
      <div className="pm-optimization-flow">
        <div><b>组合内求解</b><span>枚举核组、Worker、调度与 OPP</span></div><i aria-hidden="true">→</i>
        <div><b>可行性筛选</b><span>{frequencyGoal ? '预算门与声明扰动检查' : '保留每个核心组合的名义 winner'}</span></div><i aria-hidden="true">→</i>
        <div><b>全局择优</b><span>按同一字典序选出全局 best</span></div>
      </div>
      <p className="pm-optimization-solver-note">求解顺序：先在每种物理核心组合内按上述字典序选择 winner，再从各组合的可行 winner 中按同一目标选出全局 best；{frequencyGoal ? '预算不可行的组合只保留名义最快配置用于诊断。' : '最低时延目标不施加时延预算。'}</p>
    </section>
    <section className="pm-optimization-symbols" aria-label="模型符号">
      <span className="pm-optimization-section-label">模型符号</span>
      <dl>
        <div><dt>x</dt><dd>核组、Worker、主线程参与、调度顺序与频点构成的决策向量</dd></div>
        <div><dt>T̂<sub>UI</sub></dt><dd>代价模型和依赖 DAG 仿真的 UI 回调完成时间</dd></div>
        <div><dt>N<sub>slot</sub></dt><dd>配置占用的物理核心槽位数</dd></div>
        {frequencyGoal ? <div><dt>r<sub>g</sub></dt><dd>核组频点相对该核组最高 OPP 的归一化比例</dd></div> : <div><dt>C<sub>app</sub></dt><dd>预测的应用 CPU core-ms 账目</dd></div>}
        <div><dt>Ω<sub>current</sub></dt><dd>由本次输入冻结形成的有限候选空间</dd></div>
      </dl>
    </section>
    <div className="pm-optimization-caveat">
      <strong>解释边界</strong>
      {frequencyGoal
        ? <p><code>r<sub>g</sub> = f<sub>g</sub> / f<sub>g,max</sub></code>；先降低活动核组中的最高归一化频点，再依次降低频点总和、占用核槽和预测时延。这里优化的是频点，不代表能耗最小。</p>
        : <p>先最小化模型预测的 UI 回调完成时间，再依次减少占用核槽和应用 CPU core-ms；<code>i(x)</code> 仅作稳定决胜。{robustness ? '声明扰动情景只对每个核心组合的名义 winner 做后验敏感性检查，不改变为最坏情景最小化。' : ''}</p>}
    </div>
    <p className="pm-optimization-scope"><code>x = (g<sub>main</sub>, g<sub>worker</sub>, n<sub>worker</sub>, p<sub>main</sub>, d, f)</code>；<code>Ω<sub>current</sub></code> 由当前候选任务、已观测依赖、同核组 Worker、物理核容量和所选硬件 OPP 共同限定。</p>
  </div>
}
function CoreCombinationTable({ search, rows, selectedId, onSelect }: { search: ModelSearch; rows: CoreCombinationRecommendation[]; selectedId: string; onSelect: (row: CoreCombinationRecommendation) => void }) {
  const frequencyGoal = search.objective === 'minimum_frequency_with_budget'
  return <div aria-label="核心组合推荐">
    <div className="pm-toolbar" style={{ justifyContent: 'space-between', marginBottom: 8 }}><h4>按核心组合比较</h4><span className="pm-badge active">{rows.length} 种组合 · {frequencyGoal ? '各组合独立寻低频' : '按预测时延升序'}</span></div>
    <p className="pm-table-caption">{frequencyGoal
      ? `在本次搜索范围内，每个组合独立选择满足 ${metric(search.budget_ms)} ms 预算的最低频点配置；可行组合按归一化频点排序，无法达标的组合列在末尾。`
      : '每个物理核心组合保留一项最低时延配置，可直接比较少核方案与最快方案的差距。'}选择一行查看该配置的预计收益与频点。</p>
    <div className="pm-table-scroll" style={{ maxHeight: 470 }}><table className="pm-table" aria-label="核心组合推荐表"><thead><tr><th>查看</th><th>核心组合</th><th>物理核数</th><th>预测时延 ms</th><th>距全局最快</th><th>{frequencyGoal ? '最低可行核组频点' : '该组合最优核组频点'}</th><th>预算 / 稳健性</th></tr></thead><tbody>{rows.map((row, index) => <tr key={row.combination_id} className={selectedId === row.combination_id ? 'selected' : ''} data-selectable="true" onClick={() => onSelect(row)}>
      <td><label className="pm-choice"><input type="radio" name="core-combination-rank" aria-label={`查看核心组合 ${row.combination_label}`} checked={selectedId === row.combination_id} onChange={() => onSelect(row)} /><span>{row.rank ?? index + 1}</span></label></td>
      <td className="wrap"><strong>{row.combination_label}</strong><small>{metric(row.candidate_count, 0)} 个配置中选出{row.selection_status === 'budget_infeasible' ? ' · 最快诊断配置' : ''}</small></td>
      <td>{metric(row.physical_core_count, 0)}</td><td>{metric(record(row.metrics).finish_ms)}</td>
      <td>{numeric(row.latency_gap_ms) === null ? '—' : `+${metric(row.latency_gap_ms)} ms`}<small>{numeric(row.latency_gap_pct) === null ? '—' : `+${metric(row.latency_gap_pct, 2)}%`}</small></td>
      <td className="wrap">{frequencyText(row.frequency_vector)}{row.selection_status === 'budget_infeasible' && <small>该频点仅作最快诊断，仍不满足预算</small>}</td>
      <td className="wrap"><span className={`pm-badge ${row.selection_status === 'budget_infeasible' || row.feasible === false ? 'warn' : row.feasible === true ? 'good' : ''}`}>{feasibilityLabel(row, frequencyGoal)}</span><small>{sensitivityLabel(row, frequencyGoal)}</small></td>
    </tr>)}</tbody></table></div>
    <p className="pm-table-caption">差距基准为本次全部配置的最快名义仿真时延：{metric(rows[0]?.global_fastest_finish_ms)} ms。物理核数包括主线程占用核；同核组共享频点。稳健性仅覆盖已声明的扰动情景。</p>
  </div>
}


export function ParallelModelingStudio({ onAssistantContextChange, initialDatasetId, initialModelId, initialFeatureProfileId, handoffRevision = 0, onNavigateFeatures, onNavigateCapture, onNavigateCalibration, onNavigateHardwareModels, onSaveReport, assistantEnabled = true, executionMode = 'jobs', fileImportByteLimit = 128 * 1024 * 1024, captureSourcesEnabled = true, fullResultExportEnabled = true }: {
  onAssistantContextChange?: (context: ParallelModelingAssistantContext | null) => void
  initialDatasetId?: string
  initialModelId?: string
  initialFeatureProfileId?: string
  handoffRevision?: number
  onNavigateFeatures?: (datasetId?: string) => void
  onNavigateCapture?: () => void
  onNavigateCalibration?: () => void
  onNavigateHardwareModels?: () => void
  onSaveReport?: (report: ParallelModelingReportArtifact) => Promise<{ canceled?: boolean; size?: number; sha256?: string } | void>
  assistantEnabled?: boolean
  executionMode?: 'jobs' | 'inline'
  fileImportByteLimit?: number
  captureSourcesEnabled?: boolean
  fullResultExportEnabled?: boolean
}) {
  const [catalog, setCatalog] = useState<ModelingCatalog | null>(null)
  const [datasets, setDatasets] = useState<ModelingDataset[]>([])
  const [runs, setRuns] = useState<CaptureRun[]>([])
  const [analyses, setAnalyses] = useState<ModelAnalysis[]>([])
  const [searches, setSearches] = useState<ModelSearch[]>([])
  const [dataset, setDataset] = useState<ModelingDataset | null>(null)
  const [analysis, setAnalysis] = useState<ModelAnalysis | null>(null)
  const [search, setSearch] = useState<ModelSearch | null>(null)
  const [sourceTab, setSourceTab] = useState<'archive' | 'capture' | 'file'>('archive')
  const [archiveSelection, setArchiveSelection] = useState(initialDatasetId ? archiveDatasetValue(initialDatasetId) : '')
  const [builtinId, setBuiltinId] = useState('')
  const [captureRunId, setCaptureRunId] = useState('')
  const [stepId, setStepId] = useState('')
  const [importName, setImportName] = useState('')
  const [targetTid, setTargetTid] = useState('')
  const [targetVsync, setTargetVsync] = useState('')
  const [sourceFile, setSourceFile] = useState<File | null>(null)
  const [pmuFile, setPmuFile] = useState<File | null>(null)
  const [pmuMetadataFile, setPmuMetadataFile] = useState<File | null>(null)
  const [fileKind, setFileKind] = useState<'trace' | 'parsed'>('trace')
  const [hardwareId, setHardwareId] = useState('')
  const [bindingHardwareId, setBindingHardwareId] = useState('')
  const [modelId, setModelId] = useState(initialModelId ?? '')
  const [featureProfiles, setFeatureProfiles] = useState<(JsonRecord & { id: string; name: string; dataset_id: string })[]>([])
  const [featureProfileId, setFeatureProfileId] = useState(initialFeatureProfileId ?? '')
  const [candidateIds, setCandidateIds] = useState<string[]>([])
  const [availableCores, setAvailableCores] = useState<string[]>([])
  const [mainCore, setMainCore] = useState('')
  const [workerCounts, setWorkerCounts] = useState<number[]>([0, 1, 2, 3, 4])
  const [mainParticipation, setMainParticipation] = useState<'both' | 'yes' | 'no'>('both')
  const [frequencyPolicy, setFrequencyPolicy] = useState('max_opps')
  const [frozenFrequenciesKHz, setFrozenFrequenciesKHz] = useState<Record<string, number[]> | null>(null)
  const [objective, setObjective] = useState('minimum_latency')
  const [budget, setBudget] = useState('')
  const [robustness, setRobustness] = useState(true)
  const [selectedCandidate, setSelectedCandidate] = useState(0)
  const [selectedCombinationId, setSelectedCombinationId] = useState('')
  const [candidatePage, setCandidatePage] = useState(0)
  const [graphTab, setGraphTab] = useState<'observed' | 'parallel' | 'predicted'>('observed')
  const [busy, setBusy] = useState('')
  const [loading, setLoading] = useState(true)
  const [notice, setNotice] = useState<Notice | null>(null)
  const [jobId, setJobId] = useState('')
  const handledJob = useRef('')
  const datasetIntent = useRef(0)
  const analyzeAction = useRef<HTMLButtonElement | null>(null)
  const searchAction = useRef<HTMLButtonElement | null>(null)
  const resultsRegion = useRef<HTMLElement | null>(null)
  const agentRequest = useRef('')
  const [agentWatching, setAgentWatching] = useState(false)
  const [reportBusy, setReportBusy] = useState(false)
  const agentKnownSearches = useRef<Set<string>>(new Set())
  const agentKnownAnalyses = useRef<Set<string>>(new Set())
  const { job, pollError } = useParallelJob(executionMode === 'jobs' ? jobId : '', 'parallel-modeling')
  const jobActive = Boolean(executionMode === 'jobs' && jobId && (!job || !isTerminal(job.status)))
  const percent = jobPercent(job)
  const activeRun = runs.find((item) => item.run_id === captureRunId)
  const captureSteps = records(record(activeRun?.manifest).steps)
  const resolvedHardwareId = text(record(analysis?.hardware_resolution).hardware_profile_id, text(dataset?.hardware_profile_id))
  const hardware = catalog?.hardware_profiles.find((item) => item.id === resolvedHardwareId)
  const compatibleModels = useMemo(() => (catalog?.model_packages ?? []).filter(item => !resolvedHardwareId || item.hardware_profile_id === resolvedHardwareId), [catalog?.model_packages, resolvedHardwareId])
  const selectedCostModel = compatibleModels.find(item => item.id === modelId)
  const cores = hardware?.core_groups ?? []
  const coreProfileKey = JSON.stringify(cores)
  const physicalCores = cores.filter((core) => availableCores.includes(text(core.id))).reduce((total, core) => total + (numeric(core.physical_core_count) ?? 0), 0)
  const maxWorkers = Math.max(0, ...cores.filter((core) => availableCores.includes(text(core.id))).map((core) => (numeric(core.physical_core_count) ?? 0) - (core.id === mainCore ? 1 : 0)))
  const workerOptions = Array.from({ length: maxWorkers + 1 }, (_, index) => index)
  const currentCandidates = useMemo(() => { const best = search?.best; const rows = search?.candidates ?? []; return best ? [best, ...rows.filter((item) => !sameCandidate(item, best))] : rows }, [search])
  const combinations = search?.core_combination_recommendations
  const selectedCombination = combinations?.find((item) => item.combination_id === selectedCombinationId)
  const selectedSummary = selectedCombination ?? currentCandidates[selectedCandidate] ?? search?.best
  // Only the identical saved configuration may reuse the detailed winner's DAG and timing.
  const candidate = sameCandidate(selectedSummary, search?.best) ? { ...search?.best, ...selectedSummary } : selectedSummary
  const candidateMetrics = record(candidate?.metrics)
  const interference = record(candidate?.interference)
  const activeFeatureProfile = featureProfiles.find(item => item.id === featureProfileId && item.dataset_id === dataset?.id)
  const activeFeatureId = activeFeatureProfile?.id ?? ''
  const hardwareBindingDirty = Boolean(dataset && bindingHardwareId && bindingHardwareId !== dataset.hardware_profile_id)
  const materializedBuiltinIds = useMemo(() => new Set(datasets.map(item => text(record(item.provenance).builtin_id)).filter(Boolean)), [datasets])
  const availableBuiltins = useMemo(() => (catalog?.builtins ?? []).filter(item => !materializedBuiltinIds.has(item.id)), [catalog?.builtins, materializedBuiltinIds])
  const analysisSearches = useMemo(() => analysis ? searches.filter((item) => item.analysis_id === analysis.id) : [], [analysis, searches])
  const selectedSearchId = search && analysisSearches.some((item) => item.id === search.id) ? search.id : ''
  const baseline = record(analysis?.baseline)
  const candidateComparison = comparison(candidate, baseline)
  const currentGraph = graphTab === 'observed' ? analysis?.graphs?.observed : graphTab === 'parallel' ? analysis?.graphs?.parallel : candidate?.graph
  const diagram = useMemo(() => graphMermaid(currentGraph), [currentGraph])
  const criticalIds = Array.isArray(candidate?.critical_chain) ? candidate.critical_chain.map(String) : []
  const criticalNodes = records(record(candidate?.graph).nodes)
  const searchSpace = useMemo(() => ({ available_core_group_ids: availableCores, main_core_group_id: mainCore, worker_counts: workerCounts, main_participation: mainParticipation === 'both' ? [true, false] : [mainParticipation === 'yes'], frequency_policy: frequencyPolicy, ...(frozenFrequenciesKHz ? { frequencies_khz: frozenFrequenciesKHz } : {}) }), [availableCores, mainCore, workerCounts, mainParticipation, frequencyPolicy, frozenFrequenciesKHz])
  const context = useMemo<ParallelModelingAssistantContext>(() => ({ page_id: 'parallel_modeling', dataset_id: dataset?.id ?? null, analysis_id: analysis?.id ?? null, search_id: search?.id ?? null, job_id: jobId || null, candidate_ids: candidateIds, model_package_id: modelId || null, feature_profile_id: activeFeatureId || null, objective, budget_ms: objective === 'minimum_frequency_with_budget' && Number(budget) > 0 ? Number(budget) : null, search_space: searchSpace }), [dataset, analysis, search, jobId, candidateIds, modelId, activeFeatureId, objective, budget, searchSpace])

  const refresh = useCallback(async () => {
    setLoading(true)
    const results = await Promise.allSettled([api<ModelingCatalog>('/v1/parallel-modeling/catalog'), api<{ datasets: ModelingDataset[] }>('/v1/parallel-modeling/datasets'), captureSourcesEnabled ? api<{ runs: CaptureRun[] }>('/v1/scenario-capture/runs') : Promise.resolve({ runs: [] as CaptureRun[] }), api<{ analyses: ModelAnalysis[] }>('/v1/parallel-modeling/analyses'), api<{ searches: ModelSearch[] }>('/v1/parallel-modeling/searches'), api<{ profiles: (JsonRecord & { id: string; name: string; dataset_id: string })[] }>('/v1/workload-features/profiles')])
    const [catalogResult, datasetResult, runResult, analysisResult, searchResult, featureResult] = results
    if (catalogResult.status === 'fulfilled') { const next = catalogResult.value; setCatalog(next); setBuiltinId((current) => current || next.builtins[0]?.id || ''); setHardwareId((current) => current || (next.hardware_profiles.length === 1 ? next.hardware_profiles[0]?.id : '')); setModelId((current) => current || next.model_packages[0]?.id || '') }
    if (datasetResult.status === 'fulfilled') setDatasets(datasetResult.value.datasets ?? [])
    if (runResult.status === 'fulfilled') setRuns(runResult.value.runs ?? [])
    if (analysisResult.status === 'fulfilled') setAnalyses(analysisResult.value.analyses ?? [])
    if (searchResult.status === 'fulfilled') setSearches(searchResult.value.searches ?? [])
    if (featureResult.status === 'fulfilled') setFeatureProfiles(featureResult.value.profiles ?? [])
    const failures = results.filter((item): item is PromiseRejectedResult => item.status === 'rejected')
    if (failures.length) setNotice({ tone: 'error', text: [...new Set(failures.map((item) => errorText(item.reason)))].join('；') })
    else setNotice(current => current?.tone === 'error' ? null : current)
    setLoading(false)
  }, [captureSourcesEnabled])
  const selectDataset = useCallback(async (id: string, nextFeatureProfileId = '', handoff = false) => {
    const intent = ++datasetIntent.current
    if (!id) { setDataset(null); setBindingHardwareId(''); setAnalysis(null); setSearch(null); setFeatureProfileId(''); setArchiveSelection(''); setBusy(''); return }
    setBusy('dataset')
    try {
      const next = await api<ModelingDataset>(`/v1/parallel-modeling/datasets/${encodeURIComponent(id)}`)
      if (intent !== datasetIntent.current) return
      setDataset(next); setBindingHardwareId(text(next.hardware_profile_id)); setAnalysis(null); setSearch(null); setFeatureProfileId(nextFeatureProfileId); setCandidateIds([]); setFrozenFrequenciesKHz(null); setGraphTab('observed')
      setDatasets((current) => [next, ...current.filter((item) => item.id !== next.id)])
      setSourceTab('archive'); setArchiveSelection(archiveDatasetValue(next.id))
      if (handoff) setNotice({ tone: 'success', text: `已带入负载「${next.name}」${nextFeatureProfileId ? '及所选特征版本' : ''}。` })
      focusWorkflowAction(analyzeAction)
    } catch (error) { if (intent === datasetIntent.current) setNotice({ tone: 'error', text: errorText(error) }) } finally { if (intent === datasetIntent.current) setBusy('') }
  }, [])
  const acceptAnalysis = useCallback((value: ModelAnalysis) => { setAnalysis(value); setFeatureProfileId(text(value.feature_profile_id, text(record(value.workload_profile).id))); const selectedModel = text(record(value.cost_model).id); if (selectedModel) setModelId(selectedModel); setSearch(null); setCandidateIds(value.components.filter((item) => item.parallelizable !== false).map((item) => item.id)); setBudget(numeric(value.baseline.duration_ms)?.toString() ?? ''); setFrozenFrequenciesKHz(null); setGraphTab('observed'); setAnalyses((current) => [value, ...current.filter((item) => item.id !== value.id)]); focusWorkflowAction(searchAction) }, [])
  const acceptSearch = useCallback((value: ModelSearch) => {
    const controls = restoreParallelSearchControls(value)
    setSearch(value)
    setFeatureProfileId(text(value.feature_profile_id,
      text(record(value.search_configuration).feature_profile_id, text(record(value.workload_profile).id))))
    const selectedModel = text(record(value.model_package).id)
    if (selectedModel) setModelId(selectedModel)
    if (controls.objective) setObjective(controls.objective)
    if (controls.budget !== undefined) setBudget(controls.budget)
    if (controls.availableCores) setAvailableCores(controls.availableCores)
    if (controls.mainCore) setMainCore(controls.mainCore)
    if (controls.workerCounts) setWorkerCounts(controls.workerCounts)
    if (controls.mainParticipation) setMainParticipation(controls.mainParticipation)
    if (controls.frequencyPolicy) setFrequencyPolicy(controls.frequencyPolicy)
    if (controls.frequenciesKHz !== undefined) setFrozenFrequenciesKHz(controls.frequenciesKHz)
    if (controls.robustness !== undefined) setRobustness(controls.robustness)
    if (controls.candidateIds) setCandidateIds(controls.candidateIds)
    setSelectedCandidate(0)
    setSelectedCombinationId(value.core_combination_recommendations?.[0]?.combination_id ?? '')
    setCandidatePage(0)
    const first = value.core_combination_recommendations?.[0] ?? value.best
    setGraphTab(value.best?.graph && sameCandidate(first, value.best) ? 'predicted' : 'parallel')
    setSearches((current) => [value, ...current.filter((item) => item.id !== value.id)])
    focusWorkflowAction(resultsRegion)
  }, [])
  useEffect(() => { void refresh(); if (executionMode === 'jobs') { try { setJobId(localStorage.getItem(jobStorageKey) ?? '') } catch { /* Storage is optional. */ } } }, [executionMode, refresh])
  useEffect(() => {
    if (initialDatasetId) void selectDataset(initialDatasetId, initialFeatureProfileId ?? '', handoffRevision > 0)
  }, [initialDatasetId, initialFeatureProfileId, handoffRevision, selectDataset])
  useEffect(() => { if (initialModelId) setModelId(initialModelId) }, [initialModelId])
  useEffect(() => {
    if (!dataset) return
    setBindingHardwareId(text(dataset.hardware_profile_id, catalog?.hardware_profiles.length === 1 ? catalog.hardware_profiles[0]?.id : ''))
  }, [dataset, catalog?.hardware_profiles])
  useEffect(() => {
    if (!resolvedHardwareId || !compatibleModels.length) return
    if (!compatibleModels.some(item => item.id === modelId)) setModelId(compatibleModels[0].id)
  }, [compatibleModels, modelId, resolvedHardwareId])
  useEffect(() => { if (executionMode === 'jobs' && jobId) { try { localStorage.setItem(jobStorageKey, jobId) } catch { /* Continue without storage. */ } } }, [executionMode, jobId])
  useEffect(() => {
    const next = JSON.parse(coreProfileKey) as JsonRecord[]
    setAvailableCores(next.map((core) => text(core.id)).filter(Boolean))
    const preferred = next.reduce<JsonRecord | undefined>((best, core) => !best || (numeric(core.max_khz) ?? 0) > (numeric(best.max_khz) ?? 0) ? core : best, undefined)
    const main = text(preferred?.id, text(next[0]?.id))
    setMainCore(main)
    const count = Math.max(0, ...next.map((core) => (numeric(core.physical_core_count) ?? 0) - (core.id === main ? 1 : 0)))
    setWorkerCounts(Array.from({ length: count + 1 }, (_, index) => index))
  }, [coreProfileKey])
  useEffect(() => {
    if (!job || !isTerminal(job.status) || handledJob.current === job.job_id) return
    handledJob.current = job.job_id
    if (job.status === 'failed') { setNotice({ tone: 'error', text: job.error?.message ?? '任务失败，请检查输入与任务详情。' }); return }
    if (job.status === 'cancelled') { setNotice({ tone: 'info', text: '任务已取消。' }); return }
    const result = job.result
    if (!result) { setNotice({ tone: 'error', text: '任务已结束，但服务未返回结果。' }); return }
    let active = true
    const intent = datasetIntent.current
    const load = async () => { try {
      if (job.kind === 'parallel_analysis') { const value = Array.isArray(result.components) ? result as ModelAnalysis : await api<ModelAnalysis>(`/v1/parallel-modeling/analyses/${encodeURIComponent(text(result.analysis_id ?? result.id))}`); if (active && intent === datasetIntent.current) { if (value.dataset_id) { const target = await api<ModelingDataset>(`/v1/parallel-modeling/datasets/${encodeURIComponent(value.dataset_id)}`); if (active && intent === datasetIntent.current) { setDataset(target); setBindingHardwareId(text(target.hardware_profile_id)); setSourceTab('archive'); setArchiveSelection(archiveDatasetValue(target.id)) } } if (active && intent === datasetIntent.current) acceptAnalysis(value) } }
      else if (job.kind === 'parallel_search') { const value = result.best !== undefined || result.candidates !== undefined ? result as ModelSearch : await api<ModelSearch>(`/v1/parallel-modeling/searches/${encodeURIComponent(text(result.search_id ?? result.id))}`); if (active && intent === datasetIntent.current) { const target = await api<ModelAnalysis>(`/v1/parallel-modeling/analyses/${encodeURIComponent(value.analysis_id)}`); if (active && intent === datasetIntent.current) { setAnalysis(target); const source = await api<ModelingDataset>(`/v1/parallel-modeling/datasets/${encodeURIComponent(target.dataset_id)}`); if (active && intent === datasetIntent.current) { setDataset(source); setBindingHardwareId(text(source.hardware_profile_id)); setSourceTab('archive'); setArchiveSelection(archiveDatasetValue(source.id)) } } if (active && intent === datasetIntent.current) acceptSearch(value) } }
      if (active) void refresh()
    } catch (error) { if (active) setNotice({ tone: 'error', text: errorText(error) }) } }
    void load()
    return () => { active = false }
  }, [job, acceptAnalysis, acceptSearch, refresh])
  useEffect(() => { if (assistantEnabled) onAssistantContextChange?.(context) }, [assistantEnabled, context, onAssistantContextChange])
  useEffect(() => () => onAssistantContextChange?.(null), [onAssistantContextChange])
  useEffect(() => {
    const updated = () => { void refresh() }
    window.addEventListener('parallel-workspace-updated', updated)
    return () => window.removeEventListener('parallel-workspace-updated', updated)
  }, [refresh])
  useEffect(() => {
    if (!assistantEnabled) return
    const listener = (event: Event) => { const detail = record((event as CustomEvent).detail); if (text(detail.request_id) !== agentRequest.current) return; if (detail.status === 'rejected') setAgentWatching(false); setNotice({ tone: detail.status === 'rejected' ? 'error' : 'info', text: text(detail.message, detail.status === 'rejected' ? '页面助手未接受请求。' : '助手已接收请求，完成的分析和搜索会自动刷新。') }); agentRequest.current = '' }
    window.addEventListener(agentAutonomousTaskStatusEventName, listener)
    return () => window.removeEventListener(agentAutonomousTaskStatusEventName, listener)
  }, [assistantEnabled])
  useEffect(() => {
    if (!agentWatching || !dataset?.id) return
    let active = true
    const sourceId = dataset.id
    const poll = async () => { try { const a = await api<{ analyses: ModelAnalysis[] }>('/v1/parallel-modeling/analyses'); const relevant = a.analyses.filter((item) => item.dataset_id === sourceId); if (active) setAnalyses(a.analyses); const newAnalysis = relevant.find((item) => !agentKnownAnalyses.current.has(item.id)); if (newAnalysis && active) { agentKnownAnalyses.current.add(newAnalysis.id); acceptAnalysis(await api<ModelAnalysis>(`/v1/parallel-modeling/analyses/${encodeURIComponent(newAnalysis.id)}`)) } const s = await api<{ searches: ModelSearch[] }>('/v1/parallel-modeling/searches'); if (active) setSearches(s.searches); const ids = new Set(relevant.map((item) => item.id)); const result = s.searches.find((item) => ids.has(item.analysis_id) && !agentKnownSearches.current.has(item.id)); if (result && active) { acceptSearch(await api<ModelSearch>(`/v1/parallel-modeling/searches/${encodeURIComponent(result.id)}`)); setAgentWatching(false); setNotice({ tone: 'success', text: '已载入助手完成的并行建模推荐。' }) } } catch { /* Main refresh surfaces connection failures; keep watching real results. */ } }
    const timer = setInterval(() => void poll(), 5000)
    return () => { active = false; clearInterval(timer) }
  }, [agentWatching, dataset?.id, acceptAnalysis, acceptSearch])

  function chooseArchive(value: string) {
    setArchiveSelection(value)
    if (value.startsWith('dataset:')) {
      const id = value.slice('dataset:'.length)
      if (dataset?.id === id) return
      void selectDataset(id)
      return
    }
    if (value.startsWith('builtin:')) {
      datasetIntent.current += 1
      setBuiltinId(value.slice('builtin:'.length))
      setDataset(null); setBindingHardwareId(''); setAnalysis(null); setSearch(null); setFeatureProfileId(''); setCandidateIds([]); setBusy('')
    }
  }
  async function importData() { const intent = ++datasetIntent.current; setBusy('import'); setNotice(null); try {
    let payload: JsonRecord = {}
    if (sourceTab === 'archive') { if (!builtinId || archiveSelection !== archiveBuiltinValue(builtinId)) throw new Error('请选择尚未载入的历史样例。'); payload = { builtin_id: builtinId } }
    else {
      const tid = positiveInteger(targetTid, '目标线程 TID'); const vsync = positiveInteger(targetVsync, 'VSync ID')
      payload = { ...(importName.trim() ? { name: importName.trim() } : {}), ...(tid ? { target_tid: tid } : {}), ...(vsync ? { target_vsync_id: vsync } : {}), ...(hardwareId ? { hardware_profile_id: hardwareId } : {}) }
      if (sourceTab === 'capture') { if (!captureRunId) throw new Error('请选择已完成的采集记录。'); payload = { ...payload, capture_run_id: captureRunId, ...(stepId ? { step_id: stepId } : {}) } }
      else { if (!sourceFile) throw new Error('请选择 Trace 文本或解析结果 JSON。'); const totalBytes = sourceFile.size + (pmuFile?.size ?? 0) + (pmuMetadataFile?.size ?? 0); if (totalBytes > fileImportByteLimit) throw new Error(`当前传输通道支持最大 ${formatBytes(fileImportByteLimit)} 的导入数据。请压缩、裁剪或拆分后重试。`); const content = await sourceFile.text(); if (fileKind === 'parsed') { const parsed: unknown = JSON.parse(content); if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('解析结果应为包含 frame、binder_chains、wait_ledger 的 JSON 对象。'); payload = { ...payload, parsed: record(parsed).parsed ?? parsed } } else payload = { ...payload, trace_text: content }; if (pmuFile) payload.pmu_text = await pmuFile.text(); if (pmuMetadataFile) { const metadata: unknown = JSON.parse(await pmuMetadataFile.text()); if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)) throw new Error('PMU 元数据必须为 JSON 对象。'); payload.pmu_metadata = metadata } }
    }
    if (intent !== datasetIntent.current) return
    const value = await post<ModelingDataset>('/v1/parallel-modeling/datasets', payload)
    if (intent !== datasetIntent.current) return
    if (!value.id) throw new Error('服务未返回数据集编号。')
    setDataset(value); setBindingHardwareId(text(value.hardware_profile_id)); setAnalysis(null); setSearch(null); setFeatureProfileId(''); setSourceTab('archive'); setArchiveSelection(archiveDatasetValue(value.id)); setDatasets((current) => [value, ...current.filter((item) => item.id !== value.id)]); setNotice({ tone: 'success', text: `已导入「${value.name}」，可开始账目拆解与基线回放。` }); focusWorkflowAction(analyzeAction)
  } catch (error) { if (intent === datasetIntent.current) setNotice({ tone: 'error', text: errorText(error) }) } finally { if (intent === datasetIntent.current) setBusy('') } }
  async function analyze() {
    if (!dataset || loading) return
    const intent = ++datasetIntent.current
    setBusy('analysis'); setNotice(null)
    try {
      let targetDataset = dataset
      let targetFeatureId = activeFeatureId
      let rebuiltFeature = false
      if (bindingHardwareId && bindingHardwareId !== dataset.hardware_profile_id) {
        const next = await post<ModelingDataset>(`/v1/parallel-modeling/datasets/${encodeURIComponent(dataset.id)}/hardware-binding`, { hardware_profile_id: bindingHardwareId })
        if (intent !== datasetIntent.current) return
        targetDataset = next
        if (targetFeatureId) {
          const nextFeature = await post<JsonRecord & { id: string; name: string; dataset_id: string }>('/v1/workload-features/analyze', { dataset_id: next.id })
          if (intent !== datasetIntent.current) return
          targetFeatureId = nextFeature.id
          rebuiltFeature = true
          setFeatureProfiles(current => [nextFeature, ...current.filter(item => item.id !== nextFeature.id)])
        }
        setDataset(next); setAnalysis(null); setSearch(null); setFeatureProfileId(targetFeatureId); setCandidateIds([]); setGraphTab('observed')
        setDatasets((current) => [next, ...current.filter((item) => item.id !== next.id)])
        setSourceTab('archive'); setArchiveSelection(archiveDatasetValue(next.id)); setBindingHardwareId(text(next.hardware_profile_id))
      }
      const targetHardwareId = text(targetDataset.hardware_profile_id)
      const targetModels = (catalog?.model_packages ?? []).filter((item) => !targetHardwareId || item.hardware_profile_id === targetHardwareId)
      const targetModelId = targetHardwareId ? (targetModels.some((item) => item.id === modelId) ? modelId : targetModels[0]?.id) : modelId
      if (targetModelId && targetModelId !== modelId) setModelId(targetModelId)
      const payload = { dataset_id: targetDataset.id, feature_profile_id: targetFeatureId, ...(targetModelId ? { model_package_id: targetModelId } : {}) }
      if (executionMode === 'inline') {
        const value = await post<ModelAnalysis>('/v1/parallel-modeling/analyses', payload)
        if (intent !== datasetIntent.current) return
        acceptAnalysis(value); setNotice({ tone: 'success', text: rebuiltFeature ? '已创建硬件绑定的不可变负载版本、重新提取阶段特征，并完成账目拆解与基线回放。' : '账目拆解与基线回放已完成。' }); void refresh()
      } else {
        const receipt = await post<JobReceipt>('/v1/parallel-modeling/analyses', payload)
        if (intent !== datasetIntent.current) return
        if (!receipt.job_id) throw new Error('服务未返回分析任务编号。')
        setJobId(receipt.job_id); setNotice(null)
      }
    } catch (error) {
      if (intent === datasetIntent.current) setNotice({ tone: 'error', text: errorText(error) })
    } finally {
      if (intent === datasetIntent.current) setBusy('')
    }
  }
  async function loadAnalysis(id: string) { setBusy('analysis-load'); try { acceptAnalysis(await api<ModelAnalysis>(`/v1/parallel-modeling/analyses/${encodeURIComponent(id)}`)) } catch (error) { setNotice({ tone: 'error', text: errorText(error) }) } finally { setBusy('') } }
  async function loadSearch(id: string) { setBusy('search-load'); try { acceptSearch(await api<ModelSearch>(`/v1/parallel-modeling/searches/${encodeURIComponent(id)}`)) } catch (error) { setNotice({ tone: 'error', text: errorText(error) }) } finally { setBusy('') } }
  async function exportHtmlReport() {
    if (!search?.id || reportBusy) return
    setReportBusy(true)
    setNotice(null)
    try {
      // Re-read the saved result and follow its frozen identities. The report
      // must never mix a historical search with controls edited after it ran.
      const frozenSearch = await api<ModelSearch>(`/v1/parallel-modeling/searches/${encodeURIComponent(search.id)}`)
      const frozenAnalysis = await api<ModelAnalysis>(`/v1/parallel-modeling/analyses/${encodeURIComponent(frozenSearch.analysis_id)}`)
      const frozenDataset = await api<ModelingDataset>(`/v1/parallel-modeling/datasets/${encodeURIComponent(frozenAnalysis.dataset_id)}`)
      if (frozenSearch.analysis_id !== frozenAnalysis.id || frozenAnalysis.dataset_id !== frozenDataset.id) throw new Error('报告证据链不一致，请刷新并重新选择本次寻优记录。')

      const warnings: string[] = []
      const optional = async (label: string, path: string): Promise<unknown> => {
        if (!path) return undefined
        try { return await api<unknown>(path) }
        catch (error) { warnings.push(`${label}读取失败：${errorText(error)}`); return undefined }
      }
      const modelPackage = record(frozenSearch.model_package)
      const calibration = record(modelPackage.calibration_provenance)
      const featureSnapshot = record(frozenSearch.workload_profile)
      const analysisFeatureSnapshot = record(frozenAnalysis.workload_profile)
      // Historical records may carry the same frozen workload-feature identity
      // at different schema locations. Follow only identities frozen into this
      // search/analysis; never splice in a profile created after the search.
      const searchConfiguration = record(frozenSearch.search_configuration)
      const searchFeatureIds = [
        text(frozenSearch.feature_profile_id),
        text(searchConfiguration.feature_profile_id),
        text(featureSnapshot.id),
      ].filter(Boolean)
      if (new Set(searchFeatureIds).size > 1) throw new Error('寻优记录内部的负载特征证据不一致，请重新选择该寻优记录。')
      const searchHasFeatureContract = Object.prototype.hasOwnProperty.call(frozenSearch, 'feature_profile_id')
        || Object.prototype.hasOwnProperty.call(searchConfiguration, 'feature_profile_id')
      const explicitSearchFeatureId = Object.prototype.hasOwnProperty.call(searchConfiguration, 'feature_profile_id')
        ? text(searchConfiguration.feature_profile_id)
        : Object.prototype.hasOwnProperty.call(frozenSearch, 'feature_profile_id')
          ? text(frozenSearch.feature_profile_id)
          : text(featureSnapshot.id)
      if (searchHasFeatureContract && !explicitSearchFeatureId && searchFeatureIds.length) {
        throw new Error('寻优记录声明未使用负载特征，但仍包含非空特征快照，证据链不一致。')
      }
      const analysisFeatureIds = [text(frozenAnalysis.feature_profile_id), text(analysisFeatureSnapshot.id)].filter(Boolean)
      if (!searchHasFeatureContract && !searchFeatureIds.length && new Set(analysisFeatureIds).size > 1) {
        throw new Error('分析记录内部的负载特征证据不一致，无法作为旧版寻优记录的回退。')
      }
      // A search may legitimately override the feature version selected by its
      // parent analysis. Search evidence is authoritative; analysis is used
      // only for historical records with no search-level feature contract.
      const featureId = searchHasFeatureContract ? explicitSearchFeatureId
        : searchFeatureIds[0] ?? analysisFeatureIds[0] ?? ''
      const hardwareId = text(record(frozenSearch.hardware_resolution).hardware_profile_id,
        text(record(frozenAnalysis.hardware_resolution).hardware_profile_id, text(frozenDataset.hardware_profile_id)))
      const planId = text(calibration.plan_id)
      const fitId = text(calibration.fit_id)
      const [featureResult, hardwareResult, calibrationCatalog, calibrationPlan, calibrationFit, calibrationBundle] = await Promise.all([
        optional('负载特征版本', featureId ? `/v1/workload-features/profiles/${encodeURIComponent(featureId)}` : ''),
        optional('硬件模型', hardwareId ? `/v1/parallel-modeling/hardware-profiles/${encodeURIComponent(hardwareId)}` : ''),
        optional('寻优校准方法目录', '/v1/parallel-calibration/catalog'),
        optional('寻优校准方案', planId ? `/v1/parallel-calibration/plans/${encodeURIComponent(planId)}` : ''),
        optional('寻优校准结果', fitId ? `/v1/parallel-calibration/fits/${encodeURIComponent(fitId)}` : ''),
        optional('寻优校准代码包', planId ? `/v1/parallel-calibration/plans/${encodeURIComponent(planId)}/bundle` : ''),
      ])
      const embeddedFeatureSnapshot = featureId && text(featureSnapshot.id) === featureId ? featureSnapshot
        : featureId && text(analysisFeatureSnapshot.id) === featureId ? analysisFeatureSnapshot
          : undefined
      const report = buildParallelModelingReport({
        dataset: frozenDataset,
        features: featureId ? featureResult ?? embeddedFeatureSnapshot : undefined,
        hardware: hardwareResult,
        calibrationCatalog,
        calibrationPlan,
        calibrationFit,
        calibrationBundle,
        analysis: frozenAnalysis,
        search: frozenSearch,
        warnings,
      })
      if (onSaveReport) {
        const saved = await onSaveReport(report)
        if (saved?.canceled) { setNotice({ tone: 'info', text: '已取消导出并行建模报告。' }); return }
      } else downloadText(report.filename, report.html, report.mimeType)
      setNotice({ tone: 'success', text: `HTML 报告已生成：${report.filename}` })
    } catch (error) {
      setNotice({ tone: 'error', text: errorText(error) })
    } finally {
      setReportBusy(false)
    }
  }
  async function startSearch() { if (!analysis || loading) return; setBusy('search'); try { if (hardwareBindingDirty) throw new Error('硬件配置已改变，请先重新拆解账目与回放基线。'); if (!candidateIds.length) throw new Error('请至少选择一个并行候选任务。'); if (!availableCores.length || !availableCores.includes(mainCore)) throw new Error('请选择可用核组与主线程所在核组。'); if (!workerCounts.length || workerCounts.some((value) => value > maxWorkers)) throw new Error('当前模型的 Worker 位于同一核组，数量应不超过所选核组的可用物理核心数。'); if (objective === 'minimum_frequency_with_budget' && !(Number(budget) > 0)) throw new Error('请填写大于 0 的UI 回调耗时预算。'); const payload = { analysis_id: analysis.id, candidate_ids: candidateIds, model_package_id: modelId, feature_profile_id: activeFeatureId, objective, ...(objective === 'minimum_frequency_with_budget' ? { budget_ms: Number(budget) } : {}), space: searchSpace, robustness }; if (executionMode === 'inline') { const value = await post<ModelSearch>('/v1/parallel-modeling/searches', payload); acceptSearch(value); setNotice({ tone: 'success', text: '并行配置搜索已完成。' }); void refresh() } else { const receipt = await post<JobReceipt>('/v1/parallel-modeling/searches', payload); if (!receipt.job_id) throw new Error('服务未返回搜索任务编号。'); setJobId(receipt.job_id); setNotice(null) } } catch (error) { setNotice({ tone: 'error', text: errorText(error) }) } finally { setBusy('') } }
  async function cancel() { setBusy('cancel'); try { await post(`/v1/parallel-jobs/${encodeURIComponent(jobId)}/cancel`, {}); setNotice({ tone: 'info', text: '已请求取消，等待后台任务停止。' }) } catch (error) { setNotice({ tone: 'error', text: errorText(error) }) } finally { setBusy('') } }
  function toggleCore(id: string) { const next = availableCores.includes(id) ? availableCores.filter((value) => value !== id) : [...availableCores, id]; setAvailableCores(next); if (!next.includes(mainCore)) setMainCore(next[0] ?? ''); const main = next.includes(mainCore) ? mainCore : next[0]; const max = Math.max(0, ...cores.filter((core) => next.includes(text(core.id))).map((core) => (numeric(core.physical_core_count) ?? 0) - (core.id === main ? 1 : 0))); setWorkerCounts((current) => current.filter((count) => count <= max)) }
  function askAgent() { if (!dataset || loading) return; agentKnownAnalyses.current = new Set(analyses.map((item) => item.id)); agentKnownSearches.current = new Set(searches.map((item) => item.id)); setAgentWatching(true); agentRequest.current = dispatchParallelAgent('parallel_modeling', context as unknown as JsonRecord, `请加载 builditem-parallel-modeling Skill，根据当前数据集、候选任务与搜索空间完成账目拆解、基线回放和并行推荐：${JSON.stringify(context)}。保留跨组件独立性等假设，说明频点向量、core-ms、关键路径和预计收益。输出可审阅的模型仿真结果与实施建议，并回填当前页面。`); setNotice({ tone: 'info', text: '正在向页面助手提交建模请求。' }) }
  function selectCombination(row: CoreCombinationRecommendation) { setSelectedCombinationId(row.combination_id); if (graphTab === 'predicted' && !row.graph && !sameCandidate(row, search?.best)) setGraphTab('parallel') }
  function selectConfiguration(index: number) { setSelectedCombinationId(''); setSelectedCandidate(index); const row = currentCandidates[index]; if (graphTab === 'predicted' && !row?.graph && !sameCandidate(row, search?.best)) setGraphTab('parallel') }
  function selectAnalysisHardware(id: string) {
    if (id === bindingHardwareId) return
    datasetIntent.current += 1
    setBindingHardwareId(id)
    if (dataset && id !== dataset.hardware_profile_id) {
      // Preserve the current feature ID as the source signal for analyze().
      // The derived dataset receives a newly extracted immutable feature
      // version; the old profile is never sent directly with new hardware.
      setAnalysis(null); setSearch(null); setCandidateIds([]); setGraphTab('observed')
      setNotice({ tone: 'info', text: '硬件配置已改变。点击“拆解账目与回放基线”将自动生成新负载版本并继续分析。' })
    }
  }

  const errorForwarded = useWorkbenchErrorNotification(notice, () => setNotice(null), 'parallel-modeling')
  return <div className="workspace-scroll parallel-studio">
    <header className="pm-heading"><div><h2 className="pm-page-title" tabIndex={0} title="还原 BuildItem 与同步服务依赖，搜索可用核心、Worker 和共享频点，比较时延收益与 CPU 代价。">并行建模</h2></div><button className="quiet-button" type="button" onClick={() => void refresh()} disabled={loading}><RefreshCw size={14} className={loading ? 'spin' : ''} />刷新</button></header>
    {notice && !errorForwarded && <div className={`pm-notice ${notice.tone}`} role={notice.tone === 'error' ? 'alert' : 'status'}><CircleAlert size={15} /><span>{notice.text}</span><button type="button" onClick={() => setNotice(null)} aria-label="关闭提示"><X size={14} /></button></div>}
    {!catalog && loading && <div className="pm-skeleton"><LoaderCircle size={18} className="spin" />正在读取负载、分析与寻优历史…</div>}
    <section className="pm-section"><div className="pm-section-heading"><div><Database size={17} /><h3>1. 数据与目标场景</h3></div>{onNavigateCapture && <button className="quiet-button" type="button" onClick={onNavigateCapture}>前往负载采样<ArrowRight size={13} /></button>}</div>
      <div className="pm-grid"><div><div className="pm-tabs" role="tablist" aria-label="数据来源">{([{ id: 'archive', name: '负载归档' }, ...(captureSourcesEnabled ? [{ id: 'capture' as const, name: '负载采样结果' }] : []), { id: 'file', name: '导入文件' }] as const).map((tab) => <button type="button" role="tab" aria-selected={sourceTab === tab.id} key={tab.id} onClick={() => setSourceTab(tab.id)}>{tab.name}</button>)}</div>
        {!captureSourcesEnabled && <div className="pm-note" style={{ marginTop: 12 }}><CircleAlert size={14} /><span>这里展示服务器中已经形成的采集负载与历史归档；新的本机 capture.zip 可先在 Trace 管理中导入。</span></div>}
        <div style={{ marginTop: 12 }}>{sourceTab === 'archive' && <><div className="pm-archive-heading"><strong>已采集与历史归档负载</strong><span>{datasets.length + availableBuiltins.length} 项</span></div><div className="pm-archive-list" role="listbox" aria-label="已采集与历史归档负载">
          {datasets.map((item) => <button type="button" role="option" aria-selected={archiveSelection === archiveDatasetValue(item.id)} className={archiveSelection === archiveDatasetValue(item.id) ? 'selected' : ''} disabled={Boolean(busy) || jobActive} key={item.id} onClick={() => chooseArchive(archiveDatasetValue(item.id))}><span><strong>{item.name}</strong><small>{datasetSourceLabel(item)} · {timestamp(item.created_at)}</small></span><span><b>{metric(item.summary?.duration_ms)} ms</b><small>{metric(item.summary?.builditem_count, 0)} 个 BuildItem</small></span></button>)}
          {availableBuiltins.map((item) => <button type="button" role="option" aria-selected={archiveSelection === archiveBuiltinValue(item.id)} className={archiveSelection === archiveBuiltinValue(item.id) ? 'selected' : ''} disabled={Boolean(busy) || jobActive} key={item.id} onClick={() => chooseArchive(archiveBuiltinValue(item.id))}><span><strong>{item.name}</strong><small>随版本提供的历史样例 · 尚未载入</small></span><span><b>{metric(item.metrics?.duration_ms)} ms</b><small>{metric(item.metrics?.builditem_count, 0)} 个 BuildItem</small></span></button>)}
          {!datasets.length && !availableBuiltins.length && <div className="pm-empty">暂无可用负载。请先完成负载采样，或从文件导入数据。</div>}
        </div><p className="pm-hint" style={{ marginTop: 8 }}>采集、Trace 导入和历史样例形成的可分析数据统一保存在这里；已载入负载可直接选择，无需重复导入。</p>{availableBuiltins.filter((item) => archiveSelection === archiveBuiltinValue(item.id)).map((item) => <div className="pm-source-card" style={{ marginTop: 12 }} key={item.id}><p>{item.description}</p><div className="pm-inline" style={{ flexWrap: 'wrap' }}><span className="pm-badge">{metric(item.metrics?.duration_ms)} ms</span><span className="pm-badge">{metric(item.metrics?.builditem_count, 0)} 个 BuildItem</span><span className="pm-badge">{metric(item.metrics?.service_call_count, 0)} 次服务调用</span></div></div>)}</>}
        {sourceTab === 'capture' && <div className="pm-fields"><label className="pm-field full"><span>已回收数据的采集批次</span><select value={captureRunId} onChange={(event) => { setCaptureRunId(event.target.value); setStepId('') }}><option value="">选择采集批次</option>{runs.filter((run) => ['completed', 'succeeded', 'partial'].includes(run.status)).map((run) => <option value={run.run_id} key={run.run_id}>{timestamp(run.created_at)} · {statusLabel(run.status)} · {run.run_id}</option>)}</select></label><label className="pm-field full"><span>目标操作</span><select value={stepId} onChange={(event) => setStepId(event.target.value)}><option value="">由解析器选择</option>{captureSteps.map((step) => <option key={text(step.step_id)} value={text(step.step_id)}>{text(step.title, text(step.step_id))}</option>)}</select></label>{activeRun?.status === 'partial' && <div className="pm-note warning pm-full"><CircleAlert size={14} /><span>该批次存在采集缺口。模型将使用实际可用数据；缺失的 PMU 或 Trace 不会被填充为测量值。</span></div>}</div>}
        {sourceTab === 'file' && <div className="pm-fields"><label className="pm-field full"><span>文件类型</span><select value={fileKind} onChange={(event) => { setFileKind(event.target.value as 'trace' | 'parsed'); setSourceFile(null) }}><option value="trace">Trace 文本 + 可选 PMU 文本</option><option value="parsed">解析结果 JSON（frame / binder_chains / wait_ledger）</option></select></label><label className="pm-file pm-full">{fileKind === 'trace' ? '选择 Trace 文本' : '选择解析结果 JSON'}<input key={fileKind} type="file" accept={fileKind === 'trace' ? '.txt,.trace,.html,.ftrace' : '.json'} onChange={(event) => setSourceFile(event.target.files?.[0] ?? null)} />{sourceFile && <small>{sourceFile.name} · {formatBytes(sourceFile.size)}</small>}</label><label className="pm-file pm-full">PMU 数据（可选）<input type="file" accept=".txt,.csv,.json,.log" onChange={(event) => setPmuFile(event.target.files?.[0] ?? null)} />{pmuFile && <small>{pmuFile.name} · {formatBytes(pmuFile.size)}</small>}</label><label className="pm-file pm-full">PMU 元数据 JSON（与 PMU 数据成对提供）<input type="file" accept=".json" onChange={(event) => setPmuMetadataFile(event.target.files?.[0] ?? null)} />{pmuMetadataFile && <small>{pmuMetadataFile.name}</small>}</label><p className="pm-hint pm-full">PMU 元数据用于目标线程与时钟对齐。仅上传计数文件时，仍可分析 Trace，PMU 特征会标为不可用。当前传输通道总上限 {formatBytes(fileImportByteLimit)}；超出上限时请先裁剪或拆分文件。</p></div>}
        {sourceTab !== 'archive' && <div className="pm-fields" style={{ marginTop: 12 }}><label className="pm-field"><span>目标线程 TID（可选）</span><input type="number" min={1} value={targetTid} onChange={(event) => setTargetTid(event.target.value)} placeholder="自动识别" /></label><label className="pm-field"><span>目标 VSync ID（可选）</span><input type="number" min={1} value={targetVsync} onChange={(event) => setTargetVsync(event.target.value)} placeholder="自动选择目标帧" /></label><label className="pm-field"><span>数据集名称（可选）</span><input value={importName} onChange={(event) => setImportName(event.target.value)} placeholder="由数据来源命名" /></label><label className="pm-field"><span>硬件配置</span><select value={hardwareId} onChange={(event) => setHardwareId(event.target.value)}><option value="">请选择硬件配置</option>{catalog?.hardware_profiles.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label></div>}
        {(sourceTab !== 'archive' || archiveSelection.startsWith('builtin:')) && <div className="pm-actions"><button className="primary-button" type="button" disabled={Boolean(busy) || jobActive || !catalog} onClick={() => void importData()}>{busy === 'import' ? <LoaderCircle size={14} className="spin" /> : <FileUp size={14} />}{sourceTab === 'archive' ? '载入历史样例' : '导入数据集'}</button></div>}</div></div>
      <div className="pm-box"><h4>当前负载</h4>{dataset ? <><div className="pm-source-card"><strong>{dataset.name}</strong><p>{statusLabel(dataset.status)} · {datasetSourceLabel(dataset)} · {text(dataset.source_kind, '已归档数据')}</p><p>目标帧 {metric(dataset.summary?.duration_ms)} ms · {metric(dataset.summary?.builditem_count, 0)} 个 BuildItem</p></div>
        <div className={`pm-note ${dataset.hardware_profile_id ? '' : 'warning'}`} style={{ marginBottom: 12 }}><Cpu size={15} /><span>{dataset.hardware_profile_id ? `已绑定硬件配置：${catalog?.hardware_profiles.find(item => item.id === dataset.hardware_profile_id)?.name ?? dataset.hardware_profile_id}` : '该负载尚未绑定硬件配置。可先分析带可信归档证据的数据；其他负载需要绑定配置后再寻优。'}</span></div>
        <label className="pm-field"><span>分析使用的硬件配置</span><select value={bindingHardwareId} disabled={loading || Boolean(busy) || jobActive} onChange={event => selectAnalysisHardware(event.target.value)}><option value="">选择硬件配置</option>{catalog?.hardware_profiles.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select><small>点击拆解时自动生成不可变的新负载版本，保留原负载及既有分析记录。</small></label>
        {onNavigateHardwareModels && <div className="pm-actions"><button className="quiet-button" type="button" onClick={onNavigateHardwareModels}>管理硬件模型<ArrowRight size={13} /></button></div>}
        <button ref={analyzeAction} className="primary-button pm-step-action" style={{ marginTop: 12 }} type="button" disabled={loading || Boolean(busy) || jobActive} onClick={() => void analyze()}>{busy === 'analysis' ? <LoaderCircle size={14} className="spin" /> : <Activity size={14} />}拆解账目与回放基线</button><details className="pm-details"><summary>来源、数据质量与目标定位</summary><pre className="pm-code">{JSON.stringify({ summary: dataset.summary, quality: dataset.quality, provenance: dataset.provenance }, null, 2)}</pre></details></> : <p className="pm-hint">从左侧选择已采集或历史归档负载，也可以导入新的 Trace 文件。</p>}</div></div>
    </section>
    {dataset && <section className="pm-section"><div className="pm-section-heading"><div><Activity size={17} /><h3>负载特征输入</h3></div>{onNavigateFeatures && <button className="quiet-button" onClick={() => onNavigateFeatures(dataset.id)}>分析阶段特征<ArrowRight size={13} /></button>}</div><label className="pm-field"><span>本数据集的负载特征</span><select aria-label="本数据集的负载特征" disabled={loading} value={activeFeatureId} onChange={event => setFeatureProfileId(event.target.value)}><option value="">未使用阶段分型 · 使用通用争用模型</option>{featureProfiles.filter(item => item.dataset_id === dataset.id).map(item => <option key={item.id} value={item.id}>{item.name} · {timestamp(item.created_at)}</option>)}</select><small>阶段特征提供计算 / 访存等候选类型；寻优模型中的共跑校准矩阵决定同跑干扰。缺少匹配数据的区间使用通用争用系数，并在结果中列出。</small></label>{activeFeatureProfile && <p className="pm-hint" style={{ marginTop: 8 }}>所选特征模型：{activeFeatureProfile.id}。分析与搜索结果会保存本次使用的版本。</p>}</section>}
    {executionMode === 'jobs' && jobId && <div className="pm-progress" role="status" aria-live="polite"><div className="pm-progress-head">{jobActive ? <LoaderCircle size={17} className="spin" /> : job?.status === 'succeeded' ? <Check size={17} /> : <CircleAlert size={17} />}<div><strong>{job ? `${job.kind === 'parallel_analysis' ? '账目分析' : '配置搜索'} · ${statusLabel(job.status)}` : '正在读取后台任务'}</strong><small>{text(job?.progress.message ?? job?.progress.stage, jobId)}{percent !== null ? ` · ${Math.round(percent)}%` : ''}</small></div>{jobActive && <button type="button" className="quiet-button" disabled={busy === 'cancel'} onClick={() => void cancel()}><Square size={12} />取消</button>}</div>{jobActive && <progress max={100} value={percent ?? undefined} aria-label="后台任务进度" />}{pollError && <p className="pm-hint" style={{ marginTop: 8 }}>连接暂时中断，正在重试：{pollError}</p>}<details className="pm-details"><summary>任务进度与错误详情</summary><pre className="pm-code">{JSON.stringify({ progress: job?.progress, error: job?.error }, null, 2)}</pre></details></div>}
    {dataset && <section className="pm-section"><div className="pm-section-heading"><div><Boxes size={17} /><h3>2. 实测账目与并行候选</h3></div>{analysis && <span className={`pm-badge ${analysis.replay_gate?.passed ? 'good' : 'warn'}`}>基线回放 {analysis.replay_gate?.passed ? '通过' : '未通过'}</span>}</div>
      {analyses.some((item) => item.dataset_id === dataset.id) && <label className="pm-field" style={{ marginBottom: 12 }}><span>已保存的分析</span><select value={analysis?.id ?? ''} disabled={Boolean(busy) || jobActive || hardwareBindingDirty} onChange={(event) => { if (event.target.value) void loadAnalysis(event.target.value) }}><option value="">选择分析记录</option>{analyses.filter((item) => item.dataset_id === dataset.id).map((item) => <option key={item.id} value={item.id}>{timestamp(item.created_at)} · {item.id}</option>)}</select></label>}
      {!analysis ? <div className="pm-empty"><Activity size={22} />点击“拆解账目与回放基线”，读取 BuildItem、Binder 等待与服务 CPU。</div> : <><div className="pm-metrics"><div><span>原始UI 回调耗时</span><strong>{metric(baseline.duration_ms)}</strong><small>ms · 测量值</small></div><div><span>应用 CPU</span><strong>{metric(baseline.app_cpu_ms)}</strong><small>core-ms · 测量值</small></div><div><span>服务 CPU</span><strong>{metric(baseline.service_cpu_ms)}</strong><small>core-ms · 测量值</small></div><div><span>基线模型回放</span><strong>{metric(baseline.replay_ms)}</strong><small>ms · {metric(baseline.service_call_count, 0)} 次服务调用</small></div></div>
      {analysis.replay_gate?.passed === false && <div className="pm-note error" style={{ marginTop: 12 }}><CircleAlert size={15} /><span>基线回放校验未通过，暂不能给出可靠搜索推荐。{detailText(analysis.replay_gate.errors)}</span></div>}
      <div className="pm-note warning" style={{ marginTop: 12 }}><CircleAlert size={15} /><span>并行候选基于已观测的调用链。缺少应用源码，跨 BuildItem 独立性是条件假设；仿真保留组件内部与服务请求返回依赖。勾选仅表示纳入模型，不代表已证明线程安全。</span></div>
      <div className="pm-toolbar" style={{ margin: '12px 0' }}><button className="quiet-button" type="button" onClick={() => setCandidateIds(analysis.components.filter((item) => item.parallelizable !== false).map((item) => item.id))}>选择全部候选</button><button className="quiet-button" type="button" onClick={() => setCandidateIds([])}>清空</button><small className="pm-hint">已选 {candidateIds.length} / {analysis.components.length} 个 · 时延按实测降序</small></div>
      <div className="pm-table-scroll" style={{ maxHeight: 410 }}><table className="pm-table"><thead><tr><th>参与并行</th><th>BuildItem / 组件</th><th>Wall ms</th><th>CPU ms</th><th>Binder 等待 ms</th><th>请求数</th><th>PMU 特征</th></tr></thead><tbody>{[...analysis.components].sort((a, b) => b.wall_ms - a.wall_ms).map((item) => <tr key={item.id} className={candidateIds.includes(item.id) ? 'selected' : ''}><td><input type="checkbox" aria-label={`选择 ${item.name}`} checked={candidateIds.includes(item.id)} disabled={item.parallelizable === false} onChange={() => setCandidateIds((current) => current.includes(item.id) ? current.filter((id) => id !== item.id) : [...current, item.id])} /></td><td className="wrap"><strong>{item.name}</strong><small>{item.id} · {item.parallelizable === false ? '保留串行' : '条件候选'}</small></td><td>{metric(item.wall_ms)}</td><td>{metric(item.cpu_ms)}</td><td>{metric(item.binder_blocked_ms)}</td><td>{item.request_ids?.length ?? '—'}</td><td className="wrap">{pmuSummary(item.pmu)}</td></tr>)}</tbody></table></div>
      <details className="pm-details"><summary>展开完整账目、PMU 特征与分析假设</summary><pre className="pm-code">{JSON.stringify({ statistics: analysis.statistics, components: analysis.components, assumptions: analysis.assumptions }, null, 2)}</pre></details></>}
    </section>}
    {analysis && <section className="pm-section"><div className="pm-section-heading"><div><Cpu size={17} /><h3>3. 模型、资源与优化目标</h3></div><span className="pm-badge">{hardware?.name ?? '硬件配置待加载'}</span></div>
      {text(record(analysis.hardware_resolution).note) && <div className="pm-note" style={{ marginBottom: 12 }}><Cpu size={15} /><span>{text(record(analysis.hardware_resolution).note)}</span></div>}
      {!hardware && <div className="pm-note warning" style={{ marginBottom: 12 }}><CircleAlert size={15} /><span>当前分析没有解析出硬件配置。请在“当前负载”中绑定已有配置，或前往硬件模型管理从本地设备提取。</span>{onNavigateHardwareModels && <button className="quiet-button" type="button" onClick={onNavigateHardwareModels}>管理硬件模型</button>}</div>}
      <div className="pm-grid equal"><div><label className="pm-field"><span>并行代价模型</span><select value={modelId} disabled={!hardware || !compatibleModels.length} onChange={(event) => setModelId(event.target.value)}>{!compatibleModels.length && <option value="">当前硬件没有兼容模型</option>}{compatibleModels.map((item) => <option key={item.id} value={item.id}>{item.name} {item.version ? `· ${item.version}` : ''}{item.status === 'topology_proxy_uncalibrated' ? ' · 待校准' : item.status === 'risk_calibrated' ? ' · 有风险' : ''}</option>)}</select><small>{selectedCostModel?.description}</small></label>{selectedCostModel?.status === 'risk_calibrated' && <div className="pm-note warning" style={{ marginTop: 8 }}><CircleAlert size={14} /><span><strong>风险校准模型 · 总体风险 {text(selectedCostModel.risk_level, 'high')} · 证据等级 {text(selectedCostModel.evidence_grade, '—')}</strong><br />{text(selectedCostModel.risk_summary, '部分统计质量或覆盖门未通过；模型仍可用于搜索，结果需保留适用风险。')}</span></div>}<div className="pm-toolbar" style={{ marginTop: 8 }}>{onNavigateCalibration && <button className="quiet-button" onClick={onNavigateCalibration}>查看模型参数与校准方法<ArrowRight size={13} /></button>}{onNavigateHardwareModels && <button className="quiet-button" type="button" onClick={onNavigateHardwareModels}>管理硬件模型<ArrowRight size={13} /></button>}</div><div className="pm-box" style={{ marginTop: 12 }}><h4>可用物理核心</h4><div className="pm-choice-list">{cores.map((core) => <label key={text(core.id)} className="pm-choice"><input type="checkbox" checked={availableCores.includes(text(core.id))} onChange={() => toggleCore(text(core.id))} /><span>{text(core.name, text(core.id))} · {metric(core.physical_core_count, 0)} 个物理核<small>最高 {numeric(core.max_khz) !== null ? metric(Number(core.max_khz) / 1000, 0) : '—'} MHz · {Array.isArray(core.opps_khz) ? core.opps_khz.length : '—'} 档频点</small></span></label>)}</div><p className="pm-hint" style={{ marginTop: 10 }}>同核组共享频点。SMT 逻辑 CPU 不计为额外物理核心；当前共 {physicalCores} 个可用物理核。</p></div><div className="pm-fields" style={{ marginTop: 12 }}><label className="pm-field"><span>主线程核组</span><select value={mainCore} onChange={(event) => { const id = event.target.value; setMainCore(id); const max = Math.max(0, ...cores.filter((core) => availableCores.includes(text(core.id))).map((core) => (numeric(core.physical_core_count) ?? 0) - (core.id === id ? 1 : 0))); setWorkerCounts((current) => current.filter((count) => count <= max)) }}>{!availableCores.length && <option value="">请先选择可用核组</option>}{cores.filter((core) => availableCores.includes(text(core.id))).map((core) => <option value={text(core.id)} key={text(core.id)}>{text(core.name, text(core.id))}</option>)}</select></label><label className="pm-field"><span>主线程参与候选任务</span><select value={mainParticipation} onChange={(event) => setMainParticipation(event.target.value as 'both' | 'yes' | 'no')}><option value="both">同时搜索参与 / 不参与</option><option value="yes">参与</option><option value="no">只负责调度</option></select></label></div><div className="pm-box" style={{ marginTop: 12 }}><h4>搜索 Worker 数量</h4><div className="pm-checks">{workerOptions.map((count) => <label key={count} className="pm-choice"><input type="checkbox" checked={workerCounts.includes(count)} onChange={() => setWorkerCounts((current) => current.includes(count) ? current.filter((value) => value !== count) : [...current, count].sort((a, b) => a - b))} /><span>{count}</span></label>)}</div><p className="pm-hint" style={{ marginTop: 8 }}>当前模型的 Worker 使用同一核组；主线程与 Worker 同组时扣除一个物理核心。0 Worker 是串行对照。</p></div></div>
      <div><div className="pm-box"><h4>搜索目标</h4><div className="pm-choice-list"><label className="pm-choice"><input type="radio" name="parallel-objective" checked={objective === 'minimum_latency'} onChange={() => { setObjective('minimum_latency'); setFrequencyPolicy('max_opps'); setFrozenFrequenciesKHz(null) }} /><span>最短 UI 回调耗时<small>分别保留每个核心组合的最低时延方案，比较少核配置与最快配置。</small></span></label><label className="pm-choice"><input type="radio" name="parallel-objective" checked={objective === 'minimum_frequency_with_budget'} onChange={() => { setObjective('minimum_frequency_with_budget'); setFrequencyPolicy('all_opps'); setFrozenFrequenciesKHz(null) }} /><span>预算内最低频点<small>每个核心组合独立满足时延预算，再选择最低可行的共享频点。</small></span></label></div>{objective === 'minimum_frequency_with_budget' && <label className="pm-field" style={{ marginTop: 12 }}><span>UI 回调耗时预算（ms）</span><input type="number" min={0.001} step="any" value={budget} onChange={(event) => setBudget(event.target.value)} /><small>基线时延 {metric(baseline.duration_ms)} ms；结果仍属于模型预测。</small></label>}</div><label className="pm-field" style={{ marginTop: 12 }}><span>频点搜索范围</span><select value={frozenFrequenciesKHz ? 'frozen_custom_opps' : frequencyPolicy} onChange={(event) => { setFrozenFrequenciesKHz(null); setFrequencyPolicy(event.target.value) }}>{frozenFrequenciesKHz && <option value="frozen_custom_opps">已载入搜索的自定义 OPP 子集</option>}<option value="all_opps">全部硬件频点</option><option value="max_opps">仅各核组最高频点</option></select>{frozenFrequenciesKHz && <small>{Object.entries(frozenFrequenciesKHz).map(([core, values]) => `${core}: ${values.map(value => `${value / 1000} MHz`).join(' / ')}`).join('；')}。切换范围后改用标准硬件 OPP。</small>}</label><label className="pm-choice" style={{ marginTop: 12 }}><input type="checkbox" checked={robustness} onChange={(event) => setRobustness(event.target.checked)} /><span>包含稳健性分析<small>使用模型支持的扰动与敏感性检查，并随推荐保存。</small></span></label></div></div><OptimizationModelFormula objective={objective} budget={budget} robustness={robustness} /><div className="pm-note" style={{ marginTop: 14 }}><GitBranch size={15} /><span>输出仿真配置、频点与收益建议。应用代码改动及设备效果验证由后续工作另行完成。</span></div><div className="pm-actions"><button ref={searchAction} className="primary-button pm-step-action" type="button" disabled={loading || Boolean(busy) || jobActive || analysis.replay_gate?.passed === false || !candidateIds.length || !hardware || !compatibleModels.some(item => item.id === modelId) || !workerCounts.length} onClick={() => void startSearch()}>{busy === 'search' ? <LoaderCircle size={14} className="spin" /> : <Search size={14} />}搜索并行配置</button>{assistantEnabled && <button className="quiet-button" type="button" disabled={loading || jobActive || agentWatching} onClick={askAgent}><Sparkles size={14} />{agentWatching ? '等待助手结果' : '交给页面助手'}</button>}{assistantEnabled && agentWatching && <button type="button" className="quiet-button" onClick={() => setAgentWatching(false)}>停止自动刷新</button>}</div>
    </section>}
    {analysis && <section ref={resultsRegion} tabIndex={-1} className="pm-section"><div className="pm-section-heading"><div><ChartNoAxesCombined size={17} /><h3>4. 候选排名与预计收益</h3></div>{search && <div className="pm-toolbar pm-report-actions"><button className="primary-button" type="button" disabled={reportBusy} onClick={() => void exportHtmlReport()}>{reportBusy ? <LoaderCircle size={14} className="spin" /> : <ArrowDownToLine size={14} />}{reportBusy ? '正在汇总报告' : '一键导出 HTML 报告'}</button>{fullResultExportEnabled && <button className="quiet-button" type="button" onClick={() => downloadText(`${search.id}.json`, JSON.stringify(search, null, 2))}><ArrowDownToLine size={14} />导出原始推荐 JSON</button>}</div>}</div>
      {analysisSearches.length > 0 && <label className="pm-field pm-saved-search-field" style={{ marginBottom: 12 }}><span>已保存的搜索</span><select aria-label="已保存的搜索" value={selectedSearchId} disabled={Boolean(busy) || jobActive} onChange={(event) => { if (event.target.value) void loadSearch(event.target.value) }}><option value="">选择搜索记录</option>{analysisSearches.map((item) => <option key={item.id} value={item.id}>{savedSearchLabel(item)}</option>)}</select></label>}
      {!search ? <div className="pm-empty"><Search size={22} />配置搜索完成后显示各核心组合的推荐、频点和关键路径。结果由后台模型计算。</div> : <>
        {Boolean(search.transport_projection) && <div className="pm-note" style={{ marginBottom: 12 }}><CircleAlert size={15} /><span>当前页面展示核心组合、最优配置及前 {metric(record(search.transport_projection).returned_candidates, 0)} / {metric(record(search.transport_projection).total_candidates, 0)} 个候选配置；完整结果已随本次搜索保存。</span></div>}
        {text(record(search.hardware_resolution).note) && <div className="pm-note" aria-label="寻优硬件匹配依据" style={{ marginBottom: 12 }}><Cpu size={15} /><span>{text(record(search.hardware_resolution).note)}</span></div>}
        {!search.best && <div className="pm-note warning" style={{ marginBottom: 12 }}><CircleAlert size={16} /><span>本次搜索没有满足约束的配置。下方如有配置，仅供诊断，不能作为可行推荐。</span></div>}
        {combinations?.length ? <CoreCombinationTable search={search} rows={combinations} selectedId={selectedCombinationId} onSelect={selectCombination} /> : <div className="pm-note" style={{ marginBottom: 12 }}><CircleAlert size={15} /><span>{combinations ? '本次搜索未返回核心组合推荐。' : '此历史搜索未保存按核心组合的推荐，继续展示原配置列表。重新搜索可比较每个组合的最优方案。'}</span></div>}
        {candidate && <div aria-label="当前配置详情" style={{ marginTop: 14 }}>
          <div className="pm-toolbar" style={{ justifyContent: 'space-between', marginBottom: 10 }}><h4>{selectedCombination ? `当前组合 · ${selectedCombination.combination_label}` : '当前配置详情'}</h4><span className={`pm-badge ${candidate.feasible === false || selectedCombination?.selection_status === 'budget_infeasible' ? 'warn' : 'active'}`}>{selectedCombination?.selection_status === 'budget_infeasible' ? '无法达标 · 最快诊断配置' : search.objective === 'minimum_latency' ? '最短时延目标' : '预算内最低频点目标'}</span></div>
          {selectedCombination?.selection_status === 'budget_infeasible' && <div className="pm-note warning" style={{ marginBottom: 10 }}><CircleAlert size={15} /><span>该核心组合在本次搜索范围内无法满足预算，当前展示其最快名义配置用于分析差距。</span></div>}
          <div className="pm-metrics"><div><span>预计 UI 回调耗时</span><strong>{metric(candidateMetrics.finish_ms)}</strong><small>ms · 模型预测</small></div><div><span>预计时延收益</span><strong>{metric(candidateComparison.latency_gain_pct, 1)}%</strong><small>减少 {metric(candidateComparison.latency_gain_ms)} ms</small></div><div><span>应用 CPU 代价</span><strong>{metric(candidateMetrics.app_core_ms)}</strong><small>core-ms · 增量 {metric(candidateComparison.app_cpu_delta_ms)}</small></div><div><span>服务 CPU 代价</span><strong>{metric(candidateMetrics.service_core_ms)}</strong><small>core-ms · 应用峰值并发 {metric(candidateMetrics.peak_app_cpu_concurrency, 0)}</small></div></div>
          <div className="pm-box" style={{ marginTop: 12 }}><div className="pm-source-card"><strong>{candidateName(candidate)}</strong><p>{frequencyText(candidate.frequency_vector)}</p><p>主线程 {coreNames[text(record(record(candidate.configuration).main).kind)] ?? text(record(record(candidate.configuration).main).kind, '—')} · {records(record(candidate.configuration).workers).length} Worker · 主线程{record(candidate.configuration).main_participates ? '参与任务' : '仅调度'}</p></div><p className="pm-hint" style={{ marginTop: 8 }}>{feasibilityLabel(candidate, search.objective === 'minimum_frequency_with_budget')} · {sensitivityLabel(candidate, search.objective === 'minimum_frequency_with_budget')}。核频按 MHz 展示，core-ms 是 CPU 时间账目，不等同于能量或功耗。</p></div>
          {Object.keys(interference).length > 0 && <div className="pm-box" style={{ marginTop: 12 }}><h4>共跑干扰依据</h4><p className="pm-hint" style={{ marginTop: 8 }}>特征模型：{text(search.feature_profile_id, '未关联')} · 双任务校准覆盖 {metric(interference.calibrated_cpu_ms)} core-ms · 通用模型回退 {metric(interference.fallback_cpu_ms)} core-ms。</p><p className="pm-hint" style={{ marginTop: 6 }}>覆盖值是受相应干扰模型影响的应用 CPU 占用，单独执行的区间不计入。核组迁移、未知阶段类型、未测频点及更高并发的适用范围见下方记录。</p><details className="pm-details"><summary>覆盖范围与回退原因</summary><pre className="pm-code">{JSON.stringify(interference, null, 2)}</pre></details></div>}
          {criticalIds.length > 0 && <div style={{ marginTop: 14 }}><h4>当前配置的关键路径</h4><div className="pm-chain">{criticalIds.map((id, index) => <Fragment key={`${id}-${index}`}>{index > 0 && <ArrowRight size={13} />}<span title={id}>{text(criticalNodes.find((node) => String(node.id) === id)?.label, id)}</span></Fragment>)}</div></div>}
        </div>}
        {currentCandidates.length > 0 && <details className="pm-details" open={!combinations?.length} key={`${search.id}-configurations`}>
          <summary>全部配置明细 · {currentCandidates.length} 个配置</summary>
          <p className="pm-table-caption">保留服务端配置排名：全局推荐优先，其余按服务端优化目标排序。此表未按核心组合合并。</p>
          <div className="pm-table-scroll" style={{ maxHeight: 360 }}><table className="pm-table" aria-label="全部配置明细表"><thead><tr><th>查看</th><th>候选配置</th><th>预计时延 ms</th><th>收益 %</th><th>预算 / 稳健性</th><th>应用 core-ms</th><th>核组频点</th></tr></thead><tbody>{currentCandidates.slice(candidatePage * 25, (candidatePage + 1) * 25).map((item, offset) => { const index = candidatePage * 25 + offset; return <tr key={item.id ?? item.configuration_index as number ?? index} className={!selectedCombinationId && selectedCandidate === index ? 'selected' : ''}><td><label className="pm-choice"><input type="radio" name="candidate-rank" aria-label={`查看第 ${index + 1} 个配置 ${candidateName(item)}`} checked={!selectedCombinationId && selectedCandidate === index} onChange={() => selectConfiguration(index)} /><span>{index + 1}</span></label></td><td className="wrap">{candidateName(item)}</td><td>{metric(record(item.metrics).finish_ms)}</td><td>{metric(comparison(item, baseline).latency_gain_pct, 1)}</td><td className="wrap"><span className={`pm-badge ${item.feasible === true ? 'good' : item.feasible === false || search.objective === 'minimum_frequency_with_budget' && item.meets_budget === false ? 'warn' : ''}`}>{feasibilityLabel(item, search.objective === 'minimum_frequency_with_budget')}</span><small>{sensitivityLabel(item, search.objective === 'minimum_frequency_with_budget')}</small></td><td>{metric(record(item.metrics).app_core_ms)}</td><td className="wrap">{frequencyText(item.frequency_vector)}</td></tr> })}</tbody></table></div>
          <div className="pm-toolbar" style={{ marginTop: 10 }}><button type="button" className="quiet-button" disabled={candidatePage === 0} onClick={() => setCandidatePage((value) => value - 1)}>上一页</button><span className="pm-hint">第 {candidatePage + 1} / {Math.max(1, Math.ceil(currentCandidates.length / 25))} 页 · 共 {currentCandidates.length} 个配置</span><button type="button" className="quiet-button" disabled={(candidatePage + 1) * 25 >= currentCandidates.length} onClick={() => setCandidatePage((value) => value + 1)}>下一页</button></div>
        </details>}
        {(search.assumptions?.length ?? 0) > 0 && <div className="pm-box" style={{ marginTop: 12 }}><h4>推荐成立的前提</h4><ul className="pm-assumptions">{search.assumptions?.map((item, index) => <li key={index}>{detailText(item)}</li>)}</ul></div>}
        <details className="pm-details"><summary>当前配置、搜索覆盖与稳健性结果</summary><pre className="pm-code">{JSON.stringify({ combination: selectedCombination, configuration: candidate?.configuration, metrics: candidate?.metrics, comparison: candidate?.comparison, search_provenance: search.search_provenance, component_costs: candidate?.component_costs, resource_costs: candidate?.resource_costs, sensitivity_status: candidate?.sensitivity_status, robustness: sameCandidate(candidate, search.best) ? search.robustness : undefined, assumptions: search.assumptions }, null, 2)}</pre></details>
      </>}
    </section>}
    {analysis && <section className="pm-section"><div className="pm-section-heading"><div><GitBranch size={17} /><h3>5. 依赖图与前后关键路径</h3></div><span className="pm-badge">实测依赖 / 模型预测</span></div>{Array.isArray(record(analysis.transport_projection).omitted) && (record(analysis.transport_projection).omitted as unknown[]).some(item => String(item).startsWith('graphs')) && <div className="pm-note" style={{ marginBottom: 12 }}><CircleAlert size={15} /><span>依赖图已保存在服务器完整结果中，当前 SSH 响应未返回图形明细；可继续查看组件账目和寻优摘要。</span></div>}<div className="pm-tabs" role="tablist" aria-label="前后依赖图"><button type="button" role="tab" aria-selected={graphTab === 'observed'} onClick={() => setGraphTab('observed')}>实测串行 DAG</button><button type="button" role="tab" aria-selected={graphTab === 'parallel'} onClick={() => setGraphTab('parallel')}>候选并行 DAG</button><button type="button" role="tab" aria-selected={graphTab === 'predicted'} onClick={() => setGraphTab('predicted')} disabled={!candidate?.graph}>当前配置预测 DAG</button></div>{search && candidate && !candidate.graph && <p className="pm-table-caption">当前配置未保存预测 DAG 与执行时序，可查看实测依赖和候选并行结构；此处不复用其他配置的预测结果。</p>}{diagram ? <div className="pm-graph"><MermaidDiagram source={diagram} /></div> : <div className="pm-empty" style={{ marginTop: 12 }}>当前结果尚未返回该依赖图。</div>}{Boolean(candidate?.timeline) && <details className="pm-details"><summary>当前配置的模拟执行时序</summary><div className="pm-table-scroll"><table className="pm-table"><thead><tr><th>节点</th><th>资源 / 通道</th><th>开始 ms</th><th>结束 ms</th></tr></thead><tbody>{records(candidate?.timeline).map((item, index) => <tr key={`${text(item.node)}-${index}`}><td className="wrap">{text(item.node)}</td><td>{text(item.resource ?? item.lane, String(item.lane ?? '—'))}</td><td>{metric(item.start_ms)}</td><td>{metric(item.end_ms)}</td></tr>)}</tbody></table></div></details>}</section>}
  </div>
}
