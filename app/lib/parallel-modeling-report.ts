/**
 * Builds a portable, evidence-labelled HTML report for one parallel-modeling run.
 *
 * The report deliberately has no runtime dependency: all styles are embedded,
 * all values are HTML-escaped, and no script or remote asset is emitted.  This
 * keeps exported reports reviewable on an offline Windows workstation.
 */

type JsonObject = Record<string, unknown>

export type ParallelModelingReportInput = {
  title?: string
  dataset?: unknown
  features?: unknown
  hardware?: unknown
  calibrationCatalog?: unknown
  calibrationPlan?: unknown
  calibrationFit?: unknown
  calibrationBundle?: unknown
  analysis?: unknown
  search?: unknown
  warnings?: unknown[]
  generatedAt?: string | Date
}

export type ParallelModelingReportArtifact = {
  filename: string
  html: string
  mimeType: 'text/html;charset=utf-8'
}

export type ReportLineageIssue = {
  severity: 'error' | 'warning'
  code: string
  message: string
}

export type OptimizationModelPresentation = {
  objectiveId: string
  objectiveName: string
  objectiveFormula: string
  constraints: string[]
  explanation: string
  solveOrder: string
}

const EMPTY: JsonObject = Object.freeze({})

function object(value: unknown): JsonObject {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : EMPTY
}

function list(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function objects(value: unknown): JsonObject[] {
  return list(value).map(object).filter(item => item !== EMPTY)
}

function string(value: unknown, fallback = ''): string {
  if (typeof value === 'string' && value.trim()) return value.trim()
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  return fallback
}

function number(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function booleanLabel(value: unknown): string {
  return value === true ? '是' : value === false ? '否' : '未记录'
}

export function escapeHtml(value: unknown): string {
  return String(value ?? '').replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[character] ?? character))
}

function safeJson(value: unknown): string {
  const seen = new WeakSet<object>()
  try {
    return JSON.stringify(value, (_key, item) => {
      if (typeof item === 'bigint') return `${item.toString()}n`
      if (typeof item === 'function') return `[Function ${item.name || 'anonymous'}]`
      if (typeof item === 'symbol') return item.toString()
      if (item && typeof item === 'object') {
        if (seen.has(item)) return '[Circular]'
        seen.add(item)
      }
      return item
    }, 2) ?? 'null'
  } catch (error) {
    return `[无法序列化：${error instanceof Error ? error.message : '未知错误'}]`
  }
}

function isoTime(value: string | Date | undefined): string {
  const candidate = value instanceof Date ? value : value ? new Date(value) : new Date()
  return Number.isNaN(candidate.getTime()) ? new Date().toISOString() : candidate.toISOString()
}

function metric(value: unknown, digits = 3, unit = ''): string {
  const numeric = number(value)
  if (numeric === null) return '未记录'
  const formatted = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: digits }).format(numeric)
  return `${formatted}${unit ? ` ${unit}` : ''}`
}

function valueAt(value: unknown, ...path: string[]): unknown {
  let current: unknown = value
  for (const part of path) current = object(current)[part]
  return current
}

function first(...values: unknown[]): unknown {
  return values.find(value => value !== undefined && value !== null && value !== '')
}

function sourceKindLabel(value: unknown): string {
  const raw = string(value)
  return ({
    capture_manifest: '负载采样', builtin_id: '历史归档', trace_text: 'Trace 导入',
    parsed: '解析结果', imported: '导入数据', measured: '真机实测',
  } as Record<string, string>)[raw] ?? (raw || '未记录')
}

function statusLabel(value: unknown): string {
  const raw = string(value)
  return ({
    completed: '已完成', succeeded: '已完成', ready: '可用', available: '可用',
    partial: '部分可用', failed: '失败', planned: '已规划', proxy_calibrated: '代理负载已校准',
    partially_calibrated: '部分参数已校准', risk_calibrated: '风险校准模型',
    legacy_compat: '兼容参考模型', uncalibrated: '未校准',
  } as Record<string, string>)[raw] ?? (raw || '未记录')
}

function classLabel(value: unknown): string {
  const raw = string(value)
  return ({
    compute: '计算主导', memory: '访存主导', mixed: '混合型', service: '服务调用主导',
    scheduler: '调度主导', unknown: '证据不足',
  } as Record<string, string>)[raw] ?? (raw || '未分型')
}

function renderMissing(label: string, detail = '当前导出上下文未提供该项证据。'): string {
  return `<div class="evidence-missing"><strong>证据未提供 · ${escapeHtml(label)}</strong><span>${escapeHtml(detail)}</span></div>`
}

function renderKeyValues(rows: Array<[string, unknown]>, className = ''): string {
  return `<dl class="key-values ${className}">${rows.map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value === undefined || value === null || value === '' ? '未记录' : value)}</dd></div>`).join('')}</dl>`
}

function renderRawEvidence(title: string, value: unknown): string {
  if (value === undefined || value === null) return renderMissing(title)
  return `<details class="raw-evidence"><summary>${escapeHtml(title)} · 原始证据</summary><pre>${escapeHtml(safeJson(value))}</pre></details>`
}

function renderTransportProjection(label: string, value: unknown): string {
  const projection = object(value)
  if (projection === EMPTY) return ''
  const returned = number(projection.returned_candidates)
  const total = number(projection.total_candidates)
  const omitted = list(projection.omitted).map(item => string(item)).filter(Boolean)
  const coverage = returned !== null && total !== null
    ? `当前传输 ${metric(returned, 0)} / 共 ${metric(total, 0)} 个候选。`
    : '当前记录是面向页面传输的有界投影。'
  const persistence = projection.full_result_persisted === true ? '完整记录仍保存在服务端。' : ''
  const omissions = omitted.length ? `本报告未收到：${omitted.join('、')}。` : ''
  return `<div class="evidence-missing"><strong>传输覆盖边界 · ${escapeHtml(label)}</strong><span>${escapeHtml([coverage, persistence, omissions].filter(Boolean).join(' '))}</span></div>`
}

function idsDiffer(left: unknown, right: unknown): boolean {
  const a = string(left)
  const b = string(right)
  return Boolean(a && b && a !== b)
}

function addMismatch(issues: ReportLineageIssue[], code: string, label: string, left: unknown, right: unknown): void {
  if (!idsDiffer(left, right)) return
  issues.push({ severity: 'error', code, message: `${label}不一致：${string(left)} ≠ ${string(right)}` })
}

/** Validate cross-record identities before presenting the records as one evidence chain. */
export function validateParallelModelingReportLineage(input: ParallelModelingReportInput): ReportLineageIssue[] {
  const issues: ReportLineageIssue[] = []
  const dataset = object(input.dataset)
  const features = object(input.features)
  const hardware = object(input.hardware)
  const plan = object(input.calibrationPlan)
  const fit = object(input.calibrationFit)
  const bundle = object(input.calibrationBundle)
  const analysis = object(input.analysis)
  const search = object(input.search)
  const searchConfiguration = object(search.search_configuration)
  const searchHasFeatureContract = Object.prototype.hasOwnProperty.call(search, 'feature_profile_id')
    || Object.prototype.hasOwnProperty.call(searchConfiguration, 'feature_profile_id')
  const searchHasFeatureEvidence = searchHasFeatureContract || Boolean(string(valueAt(search, 'workload_profile', 'id')))
  const searchFeatureId = Object.prototype.hasOwnProperty.call(searchConfiguration, 'feature_profile_id')
    ? searchConfiguration.feature_profile_id
    : Object.prototype.hasOwnProperty.call(search, 'feature_profile_id')
      ? search.feature_profile_id
      : valueAt(search, 'workload_profile', 'id')
  const modelPackage = object(search.model_package)
  const calibrationProvenance = object(modelPackage.calibration_provenance)
  const analysisHardware = object(analysis.hardware_resolution)
  const searchHardware = object(search.hardware_resolution)
  const searchProvenance = object(search.search_provenance)

  addMismatch(issues, 'dataset_analysis', '数据集与分析记录的 dataset_id', dataset.id, analysis.dataset_id)
  addMismatch(issues, 'dataset_search', '数据集与寻优记录的 dataset_id', dataset.id, search.dataset_id)
  addMismatch(issues, 'dataset_features', '数据集与负载特征的 dataset_id', dataset.id, features.dataset_id)
  addMismatch(issues, 'analysis_search', '分析与寻优记录的 analysis_id', analysis.id, search.analysis_id)
  if (!searchHasFeatureEvidence) {
    addMismatch(issues, 'features_analysis', '负载特征与分析记录的 feature_profile_id', features.id, analysis.feature_profile_id)
    addMismatch(issues, 'features_analysis_snapshot', '负载特征与分析冻结快照的 feature_profile_id', features.id, valueAt(analysis, 'workload_profile', 'id'))
  }
  addMismatch(issues, 'features_search', '负载特征与寻优记录的 feature_profile_id', features.id, searchFeatureId)
  addMismatch(issues, 'features_search_snapshot', '负载特征与寻优冻结快照的 feature_profile_id', features.id, valueAt(search, 'workload_profile', 'id'))
  addMismatch(issues, 'search_feature_configuration', '寻优记录与搜索配置的 feature_profile_id', search.feature_profile_id, searchConfiguration.feature_profile_id)
  addMismatch(issues, 'search_feature_snapshot', '寻优记录与冻结快照的 feature_profile_id',
    searchFeatureId, valueAt(search, 'workload_profile', 'id'))
  addMismatch(issues, 'analysis_feature_snapshot', '分析记录与冻结快照的 feature_profile_id', analysis.feature_profile_id, valueAt(analysis, 'workload_profile', 'id'))
  if (searchHasFeatureContract && !string(searchFeatureId) && string(features.id)) {
    issues.push({ severity: 'error', code: 'features_search_contract_none', message: '寻优记录明确未使用负载特征，但报告输入仍提供了阶段级特征。' })
  }
  if (searchHasFeatureContract && !string(searchFeatureId) && string(valueAt(search, 'workload_profile', 'id'))) {
    issues.push({ severity: 'error', code: 'search_feature_contract_snapshot', message: '寻优记录声明未使用负载特征，但冻结快照仍包含 feature_profile_id。' })
  }

  const hardwareId = hardware.id
  for (const [code, label, linked] of [
    ['hardware_dataset', '硬件模型与数据集的 hardware_profile_id', dataset.hardware_profile_id],
    ['hardware_features', '硬件模型与负载特征的 hardware_profile_id', features.hardware_profile_id],
    ['hardware_analysis', '硬件模型与分析记录的 hardware_profile_id', analysisHardware.hardware_profile_id],
    ['hardware_search', '硬件模型与寻优记录的 hardware_profile_id', searchHardware.hardware_profile_id],
    ['hardware_plan', '硬件模型与校准方案的 hardware_profile_id', plan.hardware_profile_id],
    ['hardware_fit', '硬件模型与校准结果的 hardware_profile_id', fit.hardware_profile_id],
    ['hardware_model', '硬件模型与寻优成本模型的 hardware_profile_id', modelPackage.hardware_profile_id],
  ] as Array<[string, string, unknown]>) addMismatch(issues, code, label, hardwareId, linked)

  addMismatch(issues, 'plan_fit', '校准方案与拟合结果的 plan_id', plan.id, fit.plan_id)
  addMismatch(issues, 'plan_bundle', '校准方案与离线代码包的 plan_id', plan.id, bundle.plan_id)
  addMismatch(issues, 'plan_model', '校准方案与寻优成本模型来源的 plan_id', plan.id, calibrationProvenance.plan_id)
  addMismatch(issues, 'fit_model', '校准结果与寻优成本模型来源的 fit_id', fit.id, calibrationProvenance.fit_id)
  addMismatch(issues, 'model_search_configuration', '成本模型与搜索配置的 model_package_id', modelPackage.id, searchConfiguration.model_package_id)
  addMismatch(issues, 'hardware_hash_plan', '硬件画像摘要与校准方案', hardware.profile_sha256, plan.hardware_profile_sha256)
  addMismatch(issues, 'hardware_hash_features', '硬件画像摘要与负载特征', hardware.profile_sha256, features.hardware_profile_sha256)
  addMismatch(issues, 'hardware_hash_search', '硬件画像摘要与寻优记录', hardware.profile_sha256,
    first(searchProvenance.hardware_profile_sha256, searchHardware.hardware_profile_sha256))
  addMismatch(issues, 'measurements_hash_model', '校准拟合测量摘要与成本模型来源', fit.measurements_sha256,
    calibrationProvenance.measurements_sha256)
  addMismatch(issues, 'model_hash_search', '成本模型摘要与寻优记录',
    first(calibrationProvenance.model_sha256, modelPackage.model_sha256), searchProvenance.model_sha256)
  addMismatch(issues, 'calibration_version_model', '校准模型版本与寻优成本模型',
    calibrationProvenance.version_id, modelPackage.id)

  if (!string(dataset.id)) issues.push({ severity: 'warning', code: 'missing_dataset_id', message: '数据集未提供稳定 ID，无法完成跨阶段身份核验。' })
  if (!string(analysis.id)) issues.push({ severity: 'warning', code: 'missing_analysis_id', message: '分析记录未提供稳定 ID，无法核验寻优输入。' })
  if (!string(search.id)) issues.push({ severity: 'warning', code: 'missing_search_id', message: '寻优记录未提供稳定 ID，报告只能作为未绑定快照。' })
  return issues
}

/** Exact presentation of the two objectives currently implemented by the search backend. */
export function optimizationModelPresentation(searchValue: unknown): OptimizationModelPresentation {
  const search = object(searchValue)
  const configuration = object(search.search_configuration)
  const objectiveId = string(first(search.objective, configuration.objective), 'minimum_latency')
  const robust = first(configuration.robustness, valueAt(search, 'search_provenance', 'robustness_requested')) === true
  const budget = first(search.budget_ms, configuration.budget_ms)
  const budgetLabel = number(budget) === null ? 'B' : `${metric(budget)} ms`
  if (objectiveId === 'minimum_frequency_with_budget') {
    return {
      objectiveId,
      objectiveName: '预算内最低频点 · 字典序',
      objectiveFormula: 'lex minₓ ( max g∈A(x) r_g, Σ g∈A(x) r_g, N_slot(x), T̂_UI(x), i(x) )',
      constraints: [
        `T̂_UI(x) ≤ ${budgetLabel}`,
        ...(robust ? [`max s∈S_decl T̂_UI(x; s) ≤ ${budgetLabel}`] : []),
        'x ∈ Ω_current',
      ],
      explanation: 'r_g = f_g / f_g,max。先降低活动核组的最高归一化频点，再依次降低频点总和、占用核槽和预测时延；该目标不等同于能耗最小。',
      solveOrder: '先在每种物理核心组合内选择满足预算的字典序 winner，再从各组合的可行 winner 中按同一目标选出全局 best；预算不可行的组合仅保留名义最快配置用于诊断。',
    }
  }
  return {
    objectiveId: 'minimum_latency',
    objectiveName: '最短 UI 回调耗时 · 字典序',
    objectiveFormula: 'lex minₓ ( T̂_UI(x), N_slot(x), C_app(x), i(x) )',
    constraints: ['x ∈ Ω_current'],
    explanation: `先最小化模型预测的 UI 回调完成时间，再依次减少占用核槽和应用 CPU core-ms；i(x) 仅作稳定决胜。${robust ? '声明扰动情景只对每个核心组合的名义 winner 做后验敏感性检查，不改变为最坏情景最小化。' : ''}`,
    solveOrder: '先在每种物理核心组合内按字典序选择 winner，再从各组合 winner 中按同一目标选出全局 best；最低时延目标不施加时延预算。',
  }
}

function renderLineage(input: ParallelModelingReportInput): string {
  const issues = validateParallelModelingReportLineage(input)
  if (!issues.length) return '<div class="lineage passed"><strong>证据链核验通过</strong><span>数据集、特征、硬件、校准、分析与寻优记录的已提供身份一致。</span></div>'
  return `<div class="lineage ${issues.some(issue => issue.severity === 'error') ? 'failed' : 'warning'}"><strong>证据链核验发现 ${issues.length} 项问题</strong><ul>${issues.map(issue => `<li><b>${issue.severity === 'error' ? '不一致' : '待补证据'}</b> ${escapeHtml(issue.message)} <code>${escapeHtml(issue.code)}</code></li>`).join('')}</ul></div>`
}

function renderChapterIntroduction(text: string): string {
  return `<p class="chapter-introduction">${escapeHtml(text)}</p>`
}

function renderScenario(input: ParallelModelingReportInput): string {
  const dataset = object(input.dataset)
  const analysis = object(input.analysis)
  const baseline = object(analysis.baseline)
  const provenance = object(dataset.provenance)
  const capture = object(provenance.capture)
  const scenarioName = string(dataset.name, string(analysis.name, '未命名负载'))
  return `<section class="report-section" id="section-1"><div class="section-number">01</div><div class="section-body"><header><p>WORKLOAD SCENARIO</p><h2>负载场景</h2></header>
    ${renderChapterIntroduction('本章说明本次建模所对应的真实业务场景、采样步骤和实测回调基线，先回答“分析的是哪一次负载、原始耗时账目是什么”，为后续特征提取、硬件映射和寻优结论确定统一边界。')}
    <div class="hero-grid"><div><span class="eyebrow">当前数据集</span><h3>${escapeHtml(scenarioName)}</h3><p>${escapeHtml(sourceKindLabel(dataset.source_kind))} · ${escapeHtml(statusLabel(dataset.status))}</p></div>${renderKeyValues([
      ['数据集 ID', dataset.id], ['采样计划', first(capture.plan_title, capture.plan_id)],
      ['采样步骤', first(capture.step_title, capture.step_id)], ['硬件绑定', dataset.hardware_profile_id],
    ], 'compact')}</div>
    <h3 class="subheading">实测基线</h3>${analysis === EMPTY ? renderMissing('负载分析与实测基线') : renderKeyValues([
      ['UI 回调墙钟耗时', metric(baseline.duration_ms, 3, 'ms')], ['应用 CPU 时间', metric(baseline.app_cpu_ms, 3, 'core-ms')],
      ['Off-CPU 时间', metric(baseline.app_off_cpu_ms, 3, 'ms')], ['服务 CPU 时间', metric(baseline.service_cpu_ms, 3, 'core-ms')],
      ['BuildItem 数', metric(baseline.builditem_count, 0)], ['服务调用数', metric(baseline.service_call_count, 0)],
      ['回放耗时', metric(baseline.replay_ms, 3, 'ms')], ['数据语义', string(baseline.data_kind, '未记录')],
    ])}
    ${renderRawEvidence('数据集质量与来源', { quality: dataset.quality, provenance: dataset.provenance })}
  </div></section>`
}

function stageMetric(stage: JsonObject, domain: string, key: string): unknown {
  const source = object(stage[domain])
  const direct = source[key]
  if (direct !== undefined && direct !== null) return direct
  const evidence = object(valueAt(stage, 'metric_evidence', key))
  return first(evidence.exact, evidence.estimate, evidence.value)
}

function renderStageLedger(input: ParallelModelingReportInput, stages: JsonObject[]): string {
  const components = objects(valueAt(input.analysis, 'components'))
  const byId = new Map<string, JsonObject>()
  for (const component of components) {
    for (const candidate of [component.id, component.display_id]) {
      const key = string(candidate)
      if (key) byId.set(key, component)
    }
  }
  const entries = stages.length
    ? stages.map(stage => ({ stage, component: byId.get(string(stage.id)) ?? byId.get(string(stage.display_id)) ?? EMPTY }))
    : components.map(component => ({ stage: component, component }))
  if (!entries.length) return renderMissing('BuildItem 阶段账目', '分析与负载特征记录中没有可导出的阶段组件。')

  const durations = entries.map(({ stage, component }) => {
    const startNs = number(first(stage.start_ns, component.start_ns))
    const endNs = number(first(stage.end_ns, component.end_ns))
    const timestampDuration = startNs !== null && endNs !== null && endNs >= startNs ? (endNs - startNs) / 1e6 : null
    return Math.max(0, number(first(stage.wall_ms, component.wall_ms, component.duration_ms, timestampDuration)) ?? 0)
  })
  const absoluteStartsNs = entries.map(({ stage, component }) => number(first(stage.start_ns, component.start_ns)))
  const originNs = absoluteStartsNs.filter((value): value is number => value !== null && value >= 0).reduce<number | null>((minimum, value) => minimum === null ? value : Math.min(minimum, value), null)
  const measuredStarts = entries.map(({ stage }, index) => {
    const relative = number(stage.relative_start_ms)
    if (relative !== null && relative >= 0) return relative
    const absolute = absoluteStartsNs[index]
    return originNs !== null && absolute !== null && absolute >= originNs ? (absolute - originNs) / 1e6 : null
  })
  let fallbackStart = 0
  const starts = entries.map((_entry, index) => {
    const measured = measuredStarts[index]
    const start = measured !== null && measured >= 0 ? measured : fallbackStart
    fallbackStart = Math.max(fallbackStart, start + durations[index])
    return start
  })
  const measuredSpan = Math.max(0, ...starts.map((start, index) => start + durations[index]))
  const declaredSpan = number(first(valueAt(input.features, 'summary', 'duration_ms'), valueAt(input.analysis, 'baseline', 'duration_ms'), valueAt(input.dataset, 'summary', 'duration_ms')))
  const durationTotal = Math.max(1, measuredSpan, declaredSpan !== null && declaredSpan > 0 ? declaredSpan : 0)
  const selectorRules = entries.map((_entry, index) => `#stage-ledger-${index}:checked~.stage-detail-panels>.stage-detail:nth-child(${index + 1}){display:block}#stage-ledger-${index}:checked~.stage-timeline>label:nth-child(${index + 1}),#stage-ledger-${index}:focus-visible~.stage-timeline>label:nth-child(${index + 1}){outline:3px solid rgba(36,87,167,.28);outline-offset:2px;filter:saturate(1.18)}`).join('')
  const radios = entries.map((_entry, index) => `<input class="stage-selector" type="radio" name="stage-ledger-selection" id="stage-ledger-${index}"${index === 0 ? ' checked' : ''}>`).join('')
  const timeline = entries.map(({ stage }, index) => {
    const workloadClass = string(first(stage.workload_class, valueAt(stage, 'classification', 'cpu_candidate', 'id'), valueAt(stage, 'classification', 'id')), 'unknown')
    const safeClass = ['compute', 'memory', 'mixed', 'service', 'scheduler'].includes(workloadClass) ? workloadClass : 'unknown'
    const label = string(stage.display_id, string(stage.id, `B${String(index + 1).padStart(2, '0')}`))
    const title = `${label} · ${string(stage.name, '未命名阶段')} · ${metric(durations[index], 3, 'ms')}`
    const left = boundedCoordinate(starts[index] / durationTotal * 100, 0, 100)
    const width = boundedCoordinate(durations[index] / durationTotal * 100, .08, Math.max(.08, 100 - left))
    return `<label class="stage-segment ${safeClass}" for="stage-ledger-${index}" style="left:${left.toFixed(4)}%;width:${width.toFixed(4)}%" title="${escapeHtml(title)}"><b>${escapeHtml(label)}</b><span>${escapeHtml(metric(durations[index], 2, 'ms'))}</span></label>`
  }).join('')
  const panels = entries.map(({ stage, component }, index) => {
    const scheduling = object(stage.scheduling)
    const states = object(stage.states_ms)
    const placement = object(scheduling.cpu_placement)
    const classification = object(stage.classification)
    const cpuCandidate = object(classification.cpu_candidate)
    const label = string(stage.display_id, string(stage.id, `B${String(index + 1).padStart(2, '0')}`))
    const workloadClass = first(stage.workload_class, cpuCandidate.id, classification.id)
    return `<article class="stage-detail"><header><div><span>${escapeHtml(label)}</span><h4>${escapeHtml(string(stage.name, string(component.name, '未命名阶段')))}</h4></div><span class="tag">${escapeHtml(classLabel(workloadClass))}</span></header>${renderKeyValues([
      ['墙钟 / CPU', `${metric(first(stage.wall_ms, component.wall_ms, component.duration_ms), 3, 'ms')} / ${metric(first(stage.cpu_ms, component.cpu_ms), 3, 'ms')}`],
      ['落核', `${string(first(placement.primary_core_group_name, placement.primary_core_group_id), '核组未记录')} · CPU ${string(placement.primary_cpu, '—')}`],
      ['Running / Runnable', `${metric(first(states.running, stage.cpu_ms), 3, 'ms')} / ${metric(first(states.runnable, scheduling.runnable_ms), 3, 'ms')}`],
      ['Blocked / Interrupted', `${metric(first(states.blocked, scheduling.blocked_ms), 3, 'ms')} / ${metric(first(states.interrupted, scheduling.interrupted_ms), 3, 'ms')}`],
      ['IPC / Instructions', `${metric(stageMetric(stage, 'compute', 'ipc'), 3)} / ${metric(stageMetric(stage, 'compute', 'instructions'), 0)}`],
      ['Cycles / Branch MPKI', `${metric(stageMetric(stage, 'compute', 'cycles'), 0)} / ${metric(first(stageMetric(stage, 'compute', 'branch_mpki'), stageMetric(stage, 'compute', 'branch_miss_mpki')), 3)}`],
      ['Cache / LLC MPKI', `${metric(stageMetric(stage, 'memory', 'cache_mpki'), 3)} / ${metric(stageMetric(stage, 'memory', 'llc_mpki'), 3)}`],
      ['TLB MPKI / 证据质量', `${metric(first(stageMetric(stage, 'memory', 'tlb_mpki'), stageMetric(stage, 'memory', 'dtlb_mpki')), 3)} / ${string(first(cpuCandidate.evidence_quality, classification.evidence_quality), '未记录')}`],
      ['Binder 阻塞', metric(first(component.binder_blocked_ms, scheduling.binder_blocked_ms), 3, 'ms')],
      ['关联请求', list(component.request_ids).length ? list(component.request_ids).map(item => string(item)).filter(Boolean).join('、') : '未记录'],
    ])}</article>`
  }).join('')
  const rows = entries.map(({ stage, component }, index) => `<tr><td>${escapeHtml(string(stage.display_id, string(stage.id, String(index + 1))))}</td><td>${escapeHtml(string(stage.name, string(component.name, '未命名阶段')))}</td><td>${escapeHtml(metric(first(stage.wall_ms, component.wall_ms, component.duration_ms), 3, 'ms'))}</td><td>${escapeHtml(metric(first(stage.cpu_ms, component.cpu_ms), 3, 'core-ms'))}</td><td>${escapeHtml(metric(first(component.binder_blocked_ms, valueAt(stage, 'scheduling', 'binder_blocked_ms')), 3, 'ms'))}</td><td>${escapeHtml(String(list(component.request_ids).length))}</td></tr>`).join('')
  const measuredStartCount = measuredStarts.filter(value => value !== null).length
  const timingDescription = measuredStartCount === entries.length
    ? '色块保留阶段在 UI 回调中的真实起点和墙钟宽度，灰底留白为 BuildItem 外工作'
    : measuredStartCount
      ? '有时间戳的阶段保留真实位置，缺少起点的阶段按已有顺序补排'
      : '当前记录没有阶段起点，色块按组件顺序连续排布，仅用于选择详情'
  return `<div class="stage-ledger"><style>${selectorRules}</style>${radios}<div class="stage-timeline" aria-label="BuildItem 时间账目；选择阶段后在下方查看特征">${timeline}</div><p class="timeline-caption">0–${escapeHtml(metric(durationTotal, 3, 'ms'))} · ${timingDescription}；点击色块查看调度、落核与 PMU 特征。</p><div class="stage-detail-panels">${panels}</div><div class="table-wrap"><table><thead><tr><th>阶段 ID</th><th>阶段 / 组件</th><th>墙钟耗时</th><th>CPU 时间</th><th>Binder 阻塞</th><th>关联请求</th></tr></thead><tbody>${rows}</tbody></table></div></div>`
}

function renderFeatures(input: ParallelModelingReportInput): string {
  // The export assembler has already resolved the feature identity frozen by
  // the saved search. Do not revive an analysis snapshot when that search
  // explicitly selected no feature profile.
  const features = object(input.features)
  const summary = object(features.summary)
  const stages = objects(features.stages)
  const rows = stages.map((stage, index) => {
    const scheduling = object(stage.scheduling)
    const states = object(stage.states_ms)
    const classification = object(stage.classification)
    const cpuCandidate = object(classification.cpu_candidate)
    const placement = object(scheduling.cpu_placement)
    const workloadClass = first(stage.workload_class, cpuCandidate.id, classification.id)
    return `<tr><td><b>${escapeHtml(string(stage.display_id, string(stage.id, String(index + 1))))}</b><small>${escapeHtml(string(stage.name, '未命名阶段'))}</small></td><td>${escapeHtml(metric(stage.wall_ms, 3, 'ms'))}<small>CPU ${escapeHtml(metric(stage.cpu_ms, 3, 'ms'))}</small></td><td>${escapeHtml(string(first(placement.primary_core_group_name, placement.primary_core_group_id), '未记录'))}<small>CPU ${escapeHtml(string(placement.primary_cpu, '—'))}</small></td><td>${escapeHtml(metric(first(states.running, stage.cpu_ms), 3, 'ms'))}<small>Runnable ${escapeHtml(metric(first(states.runnable, scheduling.runnable_ms), 3))} · Blocked ${escapeHtml(metric(first(states.blocked, scheduling.blocked_ms), 3))}</small></td><td>${escapeHtml(metric(stageMetric(stage, 'compute', 'ipc'), 3))}<small>Instructions ${escapeHtml(metric(stageMetric(stage, 'compute', 'instructions'), 0))}</small></td><td>${escapeHtml(metric(stageMetric(stage, 'memory', 'cache_mpki'), 3))}<small>LLC ${escapeHtml(metric(stageMetric(stage, 'memory', 'llc_mpki'), 3))}</small></td><td><span class="tag">${escapeHtml(classLabel(workloadClass))}</span><small>${escapeHtml(string(first(cpuCandidate.evidence_quality, classification.evidence_quality), '证据质量未记录'))}</small></td></tr>`
  }).join('')
  return `<section class="report-section" id="section-2"><div class="section-number">02</div><div class="section-body"><header><p>WORKLOAD CHARACTERIZATION</p><h2>负载特征分析与提取</h2></header>
    ${renderChapterIntroduction('本章把 UI 回调拆成可独立建模的 BuildItem 阶段，并将每个阶段的墙钟账目、CPU 落核、调度状态和 PMU 计数对齐，用于区分计算、访存与混合负载，同时为单任务核频成本和共跑干扰模型提供阶段级输入。')}
    ${features === EMPTY ? renderMissing('负载特征版本', '未绑定阶段级负载特征；本节不推断 IPC、Cache 或任务类型。') : `${renderKeyValues([
      ['特征版本', features.id], ['引擎版本', features.engine_version], ['状态', statusLabel(features.status)],
      ['阶段数', metric(first(summary.stage_count, stages.length), 0)], ['阶段 CPU 时间', metric(summary.stage_cpu_ms, 3, 'core-ms')],
      ['PMU 可用阶段', metric(summary.pmu_usable_stages, 0)], ['已分型阶段', metric(summary.classified_cpu_stages, 0)],
      ['硬件身份已核验', booleanLabel(valueAt(features, 'quality', 'hardware_identity_verified'))],
    ])}
    <div class="table-wrap"><table><thead><tr><th>阶段 / 组件</th><th>耗时</th><th>CPU / 核组</th><th>调度拆解</th><th>IPC / 指令</th><th>Cache / LLC MPKI</th><th>Running 分型</th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="method-note"><strong>解释边界</strong><p>分型来自阶段 Trace、调度驻留与同窗 PMU 证据。缺失计数保持“未记录”；估算值与边界值不提升为精确实测，也不直接等同于具体硬件瓶颈。</p></div>`}
    <h3 class="subheading">BuildItem 组件阶段账目</h3>${renderStageLedger(input, stages)}
    ${renderRawEvidence('特征版本质量与来源', features === EMPTY ? undefined : { profile_sha256: features.profile_sha256, dataset_fingerprint: features.dataset_fingerprint, summary: features.summary, quality: features.quality, provenance: features.provenance })}
  </div></section>`
}

function renderHardware(input: ParallelModelingReportInput): string {
  const hardware = object(input.hardware)
  const groups = objects(hardware.core_groups)
  const rows = groups.map(group => {
    const opps = list(first(group.opps_khz, group.frequencies_khz)).map(number).filter((item): item is number => item !== null).sort((a, b) => a - b)
    return `<tr><td><b>${escapeHtml(string(group.name, string(group.id, '未命名核组')))}</b><small>${escapeHtml(string(group.id))}</small></td><td>${escapeHtml(metric(group.physical_core_count, 0))}</td><td>${escapeHtml(list(group.logical_cpu_ids).join(', ') || '未记录')}</td><td>${escapeHtml(opps.length ? `${metric(opps[0] / 1000, 0)}–${metric(opps[opps.length - 1] / 1000, 0)} MHz` : metric(first(group.min_khz, group.max_khz), 0, 'kHz'))}</td><td>${escapeHtml(String(opps.length || '未记录'))}</td></tr>`
  }).join('')
  return `<section class="report-section" id="section-3"><div class="section-number">03</div><div class="section-body"><header><p>HARDWARE PROFILE</p><h2>硬件模型</h2></header>
    ${renderChapterIntroduction('本章冻结寻优所使用的目标硬件画像，包括物理核组、逻辑 CPU 映射和可用 OPP。它定义任务可以落在哪些资源上、每种核心组合是否可行，以及频点性能曲线的适用硬件边界。')}
    ${hardware === EMPTY ? renderMissing('硬件模型', '未提供硬件画像；配置可行域与 OPP 结论无法独立复核。') : `${renderKeyValues([
      ['硬件模型', string(hardware.name, '未命名硬件')], ['硬件 ID', hardware.id], ['画像摘要', hardware.profile_sha256],
      ['来源', first(hardware.display_source_label, hardware.source)], ['模型状态', statusLabel(hardware.model_status)],
      ['物理核心总数', first(hardware.physical_core_count, groups.reduce((sum, group) => sum + (number(group.physical_core_count) ?? 0), 0))],
      ['逻辑核心数', first(hardware.logical_cpu_count, groups.reduce((sum, group) => sum + list(group.logical_cpu_ids).length, 0))],
      ['频率域数', first(hardware.frequency_domain_count, list(hardware.frequency_domains).length || groups.length)],
    ])}<div class="table-wrap"><table><thead><tr><th>核组</th><th>物理核心</th><th>逻辑 CPU</th><th>OPP 范围</th><th>OPP 数</th></tr></thead><tbody>${rows}</tbody></table></div>`}
    ${renderRawEvidence('硬件画像', hardware === EMPTY ? undefined : hardware)}
  </div></section>`
}

function positiveNumber(value: unknown): number | null {
  const numeric = number(value)
  return numeric !== null && numeric > 0 ? numeric : null
}

function boundedCoordinate(value: number, minimum: number, maximum: number): number {
  if (!Number.isFinite(value)) return minimum
  return Math.max(minimum, Math.min(maximum, value))
}

function curveFrequencyFactor(maximumKHz: number, frequencyKHz: number, parameters: JsonObject): number | null {
  const kneeKHz = positiveNumber(parameters.knee_khz)
  const highExponent = number(parameters.above_knee_exponent)
  const lowExponent = number(parameters.below_knee_exponent)
  if (!kneeKHz || highExponent === null || lowExponent === null || maximumKHz <= 0 || frequencyKHz <= 0
    || kneeKHz > maximumKHz || frequencyKHz > maximumKHz || highExponent < 0 || lowExponent < highExponent) return null
  const result = frequencyKHz >= kneeKHz
    ? (maximumKHz / frequencyKHz) ** highExponent
    : (maximumKHz / kneeKHz) ** highExponent * (kneeKHz / frequencyKHz) ** lowExponent
  return Number.isFinite(result) && result > 0 ? result : null
}

function renderCalibrationCurves(fit: JsonObject, hardware: JsonObject): string {
  const observations = objects(valueAt(fit, 'frequency_curve_observations', 'points'))
  const preview = object(fit.model_preview)
  const profiles = object(preview.profiles)
  const hardwareGroups = [...objects(hardware.core_groups), ...objects(valueAt(preview, 'hardware_profile', 'core_groups'))]
  const coreLabels = new Map<string, string>()
  for (const group of hardwareGroups) {
    const id = string(group.id)
    if (id && !coreLabels.has(id)) coreLabels.set(id, string(group.name, id))
  }
  const workloadLabels: Record<string, string> = { compute: '计算型', memory: '访存型', mixed: '混合型' }
  const cards = new Map<string, { coreId: string; workloadClass: string; profile: JsonObject; parameters: JsonObject; observations: JsonObject[] }>()
  for (const [coreId, value] of Object.entries(profiles)) {
    const profile = object(value)
    const classes = object(valueAt(profile, 'frequency_roofline', 'classes'))
    for (const [workloadClass, rawParameters] of Object.entries(classes)) {
      const key = `${coreId}\u0000${workloadClass}`
      cards.set(key, { coreId, workloadClass, profile, parameters: object(rawParameters), observations: [] })
    }
  }
  for (const row of observations) {
    const coreId = string(row.core_kind)
    const workloadClass = string(row.workload_class)
    const frequencyKHz = positiveNumber(row.frequency_khz)
    const unitCpuMs = positiveNumber(row.unit_cpu_ms)
    if (!coreId || !workloadClass || !frequencyKHz || !unitCpuMs) continue
    const key = `${coreId}\u0000${workloadClass}`
    const existing = cards.get(key)
    if (existing) existing.observations.push(row)
    else cards.set(key, { coreId, workloadClass, profile: object(profiles[coreId]), parameters: EMPTY, observations: [row] })
  }
  if (!cards.size) return renderMissing('任务类型频点性能曲线', '校准结果未保留 frequency_curve_observations，也没有可绘制的 model_preview 频响参数。')

  const chartCards = [...cards.values()].map(card => {
    const observed = card.observations.map(row => ({
      frequency: positiveNumber(row.frequency_khz) as number,
      value: positiveNumber(row.unit_cpu_ms) as number,
      repeats: positiveNumber(row.repeat_count) ?? 0,
    })).filter(point => Number.isFinite(point.frequency) && Number.isFinite(point.value)).sort((left, right) => left.frequency - right.frequency)
    const modelDomain = list(card.parameters.frequency_domain_khz).map(positiveNumber).filter((value): value is number => value !== null)
    const frequencies = [...new Set([...modelDomain, ...observed.map(point => point.frequency)])].sort((left, right) => left - right)
    const maximumKHz = positiveNumber(card.profile.max_khz) ?? (frequencies.length ? Math.max(...frequencies) : null)
    const referenceCost = positiveNumber(first(card.parameters.reference_cpu_ms_per_work_unit, card.parameters.max_opp_cpu_ms_per_work_unit))
    const absolute = referenceCost !== null
    let normalizedObserved = observed
    if (!absolute && observed.length) {
      const anchor = [...observed].sort((left, right) => right.frequency - left.frequency)[0]?.value
      if (anchor && Number.isFinite(anchor)) normalizedObserved = observed.map(point => ({ ...point, value: point.value / anchor }))
    }
    const fitted: Array<{ frequency: number; value: number }> = []
    if (maximumKHz && frequencies.length >= 2 && card.parameters !== EMPTY) {
      const minimumKHz = Math.min(...frequencies)
      const maximumDomainKHz = Math.max(...frequencies)
      for (let index = 0; index <= 36; index += 1) {
        const frequency = minimumKHz + (maximumDomainKHz - minimumKHz) * index / 36
        const factor = curveFrequencyFactor(maximumKHz, frequency, card.parameters)
        if (factor !== null) fitted.push({ frequency, value: factor * (referenceCost ?? 1) })
      }
    }
    const values = [...normalizedObserved.map(point => point.value), ...fitted.map(point => point.value)].filter(Number.isFinite)
    const coreLabel = coreLabels.get(card.coreId) ?? card.coreId
    const classLabelValue = workloadLabels[card.workloadClass] ?? card.workloadClass
    if (frequencies.length < 2 || !values.length) return `<article class="curve-card empty"><header><strong>${escapeHtml(coreLabel)}</strong><span>${escapeHtml(classLabelValue)}</span></header>${renderMissing('曲线数据', '有效 OPP 少于两个，无法绘制。')}</article>`

    const width = 360
    const height = 196
    const plot = { left: 50, right: 14, top: 18, bottom: 40 }
    const xMinimum = Math.min(...frequencies)
    const xMaximum = Math.max(...frequencies)
    const rawMinimum = Math.min(...values)
    const rawMaximum = Math.max(...values)
    const padding = Math.max((rawMaximum - rawMinimum) * .12, rawMaximum * .04, 1e-9)
    const yMinimum = Math.max(0, rawMinimum - padding)
    const yMaximum = rawMaximum + padding
    const x = (value: number) => boundedCoordinate(plot.left + (value - xMinimum) / Math.max(1, xMaximum - xMinimum) * (width - plot.left - plot.right), plot.left, width - plot.right)
    const y = (value: number) => boundedCoordinate(plot.top + (yMaximum - value) / Math.max(1e-12, yMaximum - yMinimum) * (height - plot.top - plot.bottom), plot.top, height - plot.bottom)
    const ticks = frequencies.length <= 4 ? frequencies : [frequencies[0], frequencies[Math.floor((frequencies.length - 1) / 2)], frequencies[frequencies.length - 1]]
    const displayScale = absolute ? 1000 : 1
    const kneeKHz = positiveNumber(card.parameters.knee_khz)
    const highExponent = number(card.parameters.above_knee_exponent)
    const lowExponent = number(card.parameters.below_knee_exponent)
    const fittedPoints = fitted.map(point => `${x(point.frequency).toFixed(2)},${y(point.value).toFixed(2)}`).join(' ')
    const circles = normalizedObserved.map(point => `<circle class="curve-observed" cx="${x(point.frequency).toFixed(2)}" cy="${y(point.value).toFixed(2)}" r="3.8"><title>${escapeHtml(`${metric(point.frequency / 1000, 0)} MHz · ${metric(point.value * displayScale, 3)}${absolute ? ' ms/千工作量' : '×'} · ${metric(point.repeats, 0)} 次`)}</title></circle>`).join('')
    const yTicks = [yMinimum, (yMinimum + yMaximum) / 2, yMaximum].map(value => `<g><line class="curve-chart-grid" x1="${plot.left}" y1="${y(value).toFixed(2)}" x2="${width - plot.right}" y2="${y(value).toFixed(2)}"/><text class="curve-tick y" x="${plot.left - 5}" y="${(y(value) + 3).toFixed(2)}">${escapeHtml(metric(value * displayScale, 2))}</text></g>`).join('')
    const xTicks = ticks.map(value => `<g><line class="curve-mark" x1="${x(value).toFixed(2)}" y1="${height - plot.bottom}" x2="${x(value).toFixed(2)}" y2="${height - plot.bottom + 4}"/><text class="curve-tick" x="${x(value).toFixed(2)}" y="${height - plot.bottom + 16}">${escapeHtml(metric(value / 1000, 0))}</text></g>`).join('')
    const knee = kneeKHz && kneeKHz >= xMinimum && kneeKHz <= xMaximum ? `<line class="curve-knee" x1="${x(kneeKHz).toFixed(2)}" y1="${plot.top}" x2="${x(kneeKHz).toFixed(2)}" y2="${height - plot.bottom}"/>` : ''
    const formula = maximumKHz && kneeKHz && highExponent !== null && lowExponent !== null
      ? `<p class="curve-formula"><code>T(f)=T(fmax)×(fmax/f)<sup>${escapeHtml(metric(highExponent, 3))}</sup></code>（f≥fk）；<code>T(f)=T(fmax)×(fmax/fk)<sup>${escapeHtml(metric(highExponent, 3))}</sup>×(fk/f)<sup>${escapeHtml(metric(lowExponent, 3))}</sup></code>（f&lt;fk）</p>` : ''
    return `<article class="curve-card"><header><strong>${escapeHtml(coreLabel)}</strong><span>${escapeHtml(classLabelValue)}</span></header><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(`${coreLabel}${classLabelValue}任务类型频点性能曲线`)}"><title>${escapeHtml(`${coreLabel} · ${classLabelValue} · 静态校准曲线`)}</title><line class="curve-axis" x1="${plot.left}" y1="${plot.top}" x2="${plot.left}" y2="${height - plot.bottom}"/><line class="curve-axis" x1="${plot.left}" y1="${height - plot.bottom}" x2="${width - plot.right}" y2="${height - plot.bottom}"/>${yTicks}${xTicks}${knee}${fittedPoints ? `<polyline class="curve-fit" points="${fittedPoints}"/>` : ''}${circles}<text class="curve-axis-label" x="${(plot.left + width - plot.right) / 2}" y="${height - 4}">OPP（MHz）</text><text class="curve-axis-label y" transform="translate(11 ${(plot.top + height - plot.bottom) / 2}) rotate(-90)">${absolute ? 'CPU ms / 千工作量' : '相对时延'}</text></svg><div class="curve-legend"><span><i class="observed"></i>实测 ${observed.length} OPP</span>${fitted.length ? '<span><i class="fitted"></i>拟合曲线</span>' : ''}${kneeKHz ? `<span><i class="knee"></i>转折 ${escapeHtml(metric(kneeKHz / 1000, 0, 'MHz'))}</span>` : ''}</div>${formula}</article>`
  }).join('')
  return `<div class="curve-grid">${chartCards}</div><p class="table-footnote">每张图对应一个“核组 × 任务类型”。圆点来自已接受样本在同一 OPP 的单位工作量时延聚合，折线来自 model_preview 的分段频率响应；曲线只缩放 On-CPU 成本。</p>`
}

function renderCalibration(input: ParallelModelingReportInput): string {
  const catalog = object(input.calibrationCatalog)
  const plan = object(input.calibrationPlan)
  const fit = object(input.calibrationFit)
  const bundle = object(input.calibrationBundle)
  const modelStructure = object(catalog.model_structure)
  const quality = object(fit.quality)
  const fitProvenance = object(fit.provenance)
  const bundleManifest = object(bundle.manifest)
  const parameterRows = objects(fit.parameter_estimates).map(parameter => `<tr><td>${escapeHtml(string(parameter.parameter, string(parameter.path, '未命名参数')))}<small>${escapeHtml(string(parameter.path))}</small></td><td>${escapeHtml(typeof parameter.value === 'object' ? safeJson(parameter.value) : String(parameter.value ?? '未记录'))}</td><td>${escapeHtml(string(parameter.method, '未记录'))}</td><td>${escapeHtml(metric(parameter.sample_count, 0))}</td><td>${escapeHtml(statusLabel(parameter.status))}</td></tr>`).join('')
  const benchmarkRows = objects(catalog.benchmark_catalog).map(item => `<tr><td>${escapeHtml(string(item.name, string(item.id)))}</td><td>${escapeHtml(string(item.description, '未记录'))}</td><td>${escapeHtml(list(item.parameters).join('、') || '未记录')}</td></tr>`).join('')
  const codeFiles = objects(bundle.files)
  const code = codeFiles.length ? codeFiles.map(file => `<details class="code-file"><summary><b>${escapeHtml(string(file.filename, '未命名文件'))}</b><span>${escapeHtml(string(file.media_type, 'text/plain'))} · SHA-256 ${escapeHtml(string(file.sha256, '未记录'))}</span></summary><pre><code>${escapeHtml(file.content ?? '')}</code></pre></details>`).join('') : renderMissing('校准代码包', '未获取微基准、采集脚本或测量合同。')
  const measuredBinarySha256 = first(fitProvenance.binary_sha256, valueAt(fit, 'measurements', 'provenance', 'binary_sha256'))
  const bundleVersion = first(bundle.asset_version, bundle.version, bundleManifest.asset_version, bundleManifest.version, bundleManifest.implementation_version)
  const bundleSha256 = first(bundle.bundle_sha256, bundle.sha256, bundleManifest.bundle_sha256, bundleManifest.sha256, bundleManifest.asset_sha256)
  const bundleGeneratedAt = first(bundle.generated_at, bundle.created_at, bundleManifest.generated_at, bundleManifest.created_at)
  const codeSource = codeFiles.length ? `<div class="evidence-missing"><strong>代码来源 · 按历史方案由当前部署资产重建</strong><span>该代码包依据历史 plan_id 由当前部署中的校准资产重新生成，用于复现和审阅；它不等同于实际测量时冻结的源码。实际运行身份应以拟合来源中的二进制 SHA-256 和独立工件为准。</span></div>${renderKeyValues([
    ['Bundle schema', bundle.schema_version], ['历史方案 ID', bundle.plan_id],
    ['部署资产版本', bundleVersion], ['Bundle 摘要', bundleSha256],
    ['重建时间', bundleGeneratedAt], ['实测二进制 SHA-256', measuredBinarySha256],
  ])}${renderRawEvidence('当前部署重建代码包元数据', {
    schema_version: bundle.schema_version, plan_id: bundle.plan_id, version: bundleVersion,
    bundle_sha256: bundleSha256, generated_at: bundleGeneratedAt, manifest: bundle.manifest,
    files: codeFiles.map(file => ({ filename: file.filename, media_type: file.media_type, sha256: file.sha256 })),
  })}` : ''
  const fitProjection = renderTransportProjection('校准拟合', fit.transport_projection)
  const fitEvidenceTitle = fit.validation_predictions === undefined
    ? '校准质量与模型预览'
    : '校准质量、验证预测与模型预览'
  return `<section class="report-section" id="section-4"><div class="section-number">04</div><div class="section-body"><header><p>MEASUREMENT &amp; CALIBRATION</p><h2>寻优校准方案、代码与结果</h2></header>
    ${renderChapterIntroduction('本章用真机微基准把“核组、任务类型与 OPP”转换为可复核的单位工作量时延曲线，并记录拟合质量与适用域。寻优阶段使用这些曲线估算阶段 On-CPU 成本；等待状态和跨服务依赖仍由 Trace 证据保留。')}
    <h3 class="subheading">任务类型频点性能曲线</h3>${fit === EMPTY ? renderMissing('校准拟合结果', '没有拟合结果，无法绘制频点性能曲线。') : renderCalibrationCurves(fit, object(input.hardware))}
    ${fit === EMPTY ? '' : renderKeyValues([
      ['拟合 ID', fit.id], ['状态', statusLabel(fit.status)], ['证据等级', string(quality.evidence_grade, '未记录')],
      ['总体风险', string(quality.risk_level, '未记录')], ['接受 / 拒绝样本', `${metric(quality.accepted_samples, 0)} / ${metric(quality.rejected_samples, 0)}`],
      ['留出 Median / P90 APE', `${metric(quality.validation_median_ape, 3)} / ${metric(quality.validation_p90_ape, 3)}`],
      ['可发布', booleanLabel(first(quality.publishable, quality.passed))], ['风险摘要', string(quality.risk_summary, '未记录')],
    ])}
    <details class="report-details calibration-details"><summary><span>校准方案、代码与参数详情</span><small>展开查看微基准设计、方案条件、代码身份、参数估计与质量证据</small></summary><div class="details-body">
    <h3 class="subheading">校准方法与方案</h3>${catalog === EMPTY ? renderMissing('校准方法目录') : `${renderKeyValues([
      ['模型结构', string(modelStructure.name, '未记录')], ['模型状态', statusLabel(modelStructure.status)],
      ['模型公式', string(modelStructure.formula, '未记录')], ['基准类型数', metric(list(catalog.benchmark_catalog).length, 0)],
      ['可校准参数数', metric(list(catalog.parameter_catalog).length, 0)], ['测量合同', string(valueAt(catalog, 'measurement_contract', 'schema_version'), '未记录')],
    ])}${benchmarkRows ? `<div class="table-wrap"><table><thead><tr><th>微基准</th><th>测量目的</th><th>校准参数</th></tr></thead><tbody>${benchmarkRows}</tbody></table></div>` : ''}`}
    ${plan === EMPTY ? renderMissing('校准方案', '没有与本次硬件和寻优模型绑定的校准方案。') : `${renderKeyValues([
      ['方案', string(plan.name, '未命名方案')], ['方案 ID', plan.id], ['状态', statusLabel(plan.status)],
      ['硬件模型', plan.hardware_profile_id], ['参数组', list(plan.parameter_groups).join('、') || '未记录'],
      ['核组', list(plan.core_kinds).join('、') || '未记录'], ['重复次数', metric(plan.repeats, 0)],
      ['条件数', metric(first(plan.condition_count, list(plan.conditions).length), 0)], ['预计样本数', metric(plan.expected_sample_count, 0)],
    ])}${renderRawEvidence('校准方案条件与测量合同', { conditions: plan.conditions, measurement_contract: plan.measurement_contract, quality_thresholds: plan.quality_thresholds, cpu_map: plan.cpu_map, frequency_policy_ids: plan.frequency_policy_ids })}`}
    <h3 class="subheading">校准代码包（当前部署资产重建）</h3>${codeSource}${code}
    <h3 class="subheading">拟合与质量结果</h3>${fit === EMPTY ? renderMissing('校准拟合结果', '未提供参数估计、留出验证和质量门结果。') : `${renderKeyValues([
      ['拟合 ID', fit.id], ['状态', statusLabel(fit.status)], ['测量摘要', fit.measurements_sha256],
      ['接受样本', metric(quality.accepted_samples, 0)], ['拒绝样本', metric(quality.rejected_samples, 0)],
      ['训练 / 留出', `${metric(quality.train_samples, 0)} / ${metric(quality.validation_samples, 0)}`],
      ['留出 Median APE', metric(quality.validation_median_ape, 3)], ['留出 P90 APE', metric(quality.validation_p90_ape, 3)],
      ['证据等级', string(quality.evidence_grade, '未记录')], ['总体风险', string(quality.risk_level, '未记录')],
      ['可发布', booleanLabel(first(quality.publishable, quality.passed))], ['风险摘要', string(quality.risk_summary, '未记录')],
    ])}${fitProjection}${parameterRows ? `<div class="table-wrap"><table><thead><tr><th>参数</th><th>估计值</th><th>方法</th><th>样本</th><th>状态</th></tr></thead><tbody>${parameterRows}</tbody></table></div>` : renderMissing('参数估计')}${renderRawEvidence(fitEvidenceTitle, { quality: fit.quality, calibrated_parameters: fit.calibrated_parameters, inherited_parameters: fit.inherited_parameters, validation_predictions: fit.validation_predictions, model_preview: fit.model_preview, provenance: fit.provenance, transport_projection: fit.transport_projection })}`}
    </div></details>
  </div></section>`
}

function frequencyLabel(value: unknown): string {
  const entries = Object.entries(object(value))
  if (!entries.length) return '未记录'
  return entries.map(([group, frequency]) => `${group} ${number(frequency) === null ? '未使用' : `${metric((number(frequency) as number) / 1000, 0)} MHz`}`).join(' · ')
}

function configurationLabel(value: unknown): string {
  const configuration = object(value)
  const main = object(configuration.main)
  const workers = objects(configuration.workers)
  return `${string(first(main.kind, configuration.main_core_group_id), '主线程核组未记录')} 主线程 · ${workers.length || number(configuration.worker_count) || 0} Worker · 主线程${configuration.main_participates === true ? '参与任务' : '仅调度'}`
}

function renderOptimizationModel(search: JsonObject): string {
  const model = optimizationModelPresentation(search)
  return `<div class="optimization-model"><div class="model-title"><span>OPTIMIZATION MODEL</span><strong>${escapeHtml(model.objectiveName)}</strong></div><div class="equation"><span>目标函数</span><code>${escapeHtml(model.objectiveFormula)}</code></div><div class="constraints"><strong>约束条件</strong>${model.constraints.map(item => `<code>${escapeHtml(item)}</code>`).join('')}</div><div class="model-notes"><p>${escapeHtml(model.explanation)}</p><p>${escapeHtml(model.solveOrder)}</p><p><code>x = (g_main, g_worker, n_worker, p_main, d, f)</code>；<code>Ω_current</code> 由候选任务、已观测依赖、同核组 Worker、物理核容量与所选硬件 OPP 共同限定。</p></div></div>`
}

function renderTaskFrequencyCostModel(input: ParallelModelingReportInput): string {
  const transferCandidates = [
    object(valueAt(input.search, 'best', 'source_placement_cost_transfer')),
    object(valueAt(input.search, 'search_provenance', 'source_target_curve_compatibility', 'best')),
    object(valueAt(input.search, 'search_provenance', 'source_placement_cost_transfer')),
    object(valueAt(input.analysis, 'analysis_provenance', 'source_placement_cost_transfer')),
  ]
  // Prefer what the winning candidate actually consumed. A verified source
  // summary can still fall back when the target curve/class/reference is not
  // compatible with that source anchor.
  const sourceTransfer = transferCandidates.find(candidate => Object.keys(candidate).length) ?? EMPTY
  const status = string(sourceTransfer.status)
  const verified = status === 'verified' || status === 'verified_with_evidence_risk'
  const mixed = status === 'mixed_verified_and_fallback'
  const fallback = status === 'fallback_reference_anchor'
  const sourceFormula = verified
    ? 'R<sub>src→ref</sub> = Σ<sub>i</sub>(t<sub>i</sub>/F<sub>src,i</sub>) / Σ<sub>i</sub>t<sub>i</sub><br>W<sub>ref</sub> = T<sub>on,obs</sub> · R<sub>src→ref</sub>'
    : mixed
      ? 'W<sub>ref</sub> = { T<sub>on,obs</sub> · R<sub>src→ref</sub>，源驻留已核验<br><span class="formula-indent">T<sub>on,obs</sub>，证据不足阶段回退</span> }'
      : fallback
        ? 'W<sub>ref</sub> = T<sub>on,obs</sub>（观测参考锚回退）'
        : 'W<sub>ref</sub> = { T<sub>on,obs</sub> · R<sub>src→ref</sub>，证据核验通过<br><span class="formula-indent">T<sub>on,obs</sub>，证据不足时回退</span> }'
  const statusLabel = verified ? '源驻留已核验' : mixed ? '部分阶段回退' : fallback ? '观测参考锚回退' : '历史记录未声明分支'
  const fallbackReasons = Object.keys(object(sourceTransfer.fallback_reasons))
  const evidenceNote = fallbackReasons.length ? ` 回退原因：${fallbackReasons.join('、')}。` : ''
  const manuallyBound = fallbackReasons.includes('source_hardware_identity_unverified')
  const sourceExplanation = verified
    ? '本次记录已用阶段实际 CPU 驻留和对应源曲线归一到参考工作量。'
    : mixed
      ? '本次记录仅对证据完整的阶段执行源驻留归一，其余阶段保留观测 On-CPU 时间作为参考工作量。'
      : fallback
        ? manuallyBound
          ? '最优配置没有形成可核验的源成本锚，搜索按后端记录的观测参考锚回退；手工绑定的硬件画像只用于目标拓扑与 OPP，不会被写成已核验源设备。'
          : '最优配置在源驻留、任务类别、目标绝对曲线或迁移参考系上没有形成完整兼容链，相关阶段按后端记录的观测参考锚回退。'
        : '历史记录未提供源成本迁移状态，公式同时列出核验路径与保守回退路径，不将任一分支伪装为已执行事实。'
  return `<div class="task-cost-model"><div class="model-title"><span>SINGLE-TASK FREQUENCY COST</span><strong>阶段核型与频点时延估算 · ${statusLabel}</strong></div><div class="task-equations"><div><span>源成本锚</span><code>${sourceFormula}</code></div><div><span>目标核频估算</span><code>T̂<sub>on</sub>(k,c,f) = W<sub>ref</sub> · F<sub>k,c</sub>(f)</code></div><div><span>整条回调</span><code>T̂<sub>UI</sub>(x) = makespan(DAG{T̂<sub>on</sub>, D<sub>non-binder</sub>, E<sub>binder</sub>})</code></div></div><p>${escapeHtml(sourceExplanation + evidenceNote)} <code>F<sub>k,c</sub>(f)</code> 是任务类型 <code>c</code> 在核组 <code>k</code>、频率 <code>f</code> 下由微基准实测并拟合的相对单位工作量时延。非 Binder delay 保留 Trace 实测时长，同步 Binder 等待沿 send → service → reply → wake 依赖链进入 DAG，避免把 Off-CPU 账目重复相加。</p></div>`
}

function renderParallelModeling(input: ParallelModelingReportInput): string {
  const analysis = object(input.analysis)
  const search = object(input.search)
  const configuration = object(search.search_configuration)
  const provenance = object(search.search_provenance)
  const best = object(search.best)
  const bestMetrics = object(best.metrics)
  const bestComparison = object(best.comparison)
  const recommendations = objects(search.core_combination_recommendations)
  const candidates = objects(search.candidates)
  const searchProjection = renderTransportProjection('并行寻优', search.transport_projection)
  const recommendationRows = recommendations.map((row, index) => `<tr><td>${escapeHtml(String(first(row.rank, index + 1)))}</td><td>${escapeHtml(string(row.combination_label, string(row.combination_id, '未命名组合')))}<small>${escapeHtml(metric(row.physical_core_count, 0))} 个物理核</small></td><td>${escapeHtml(metric(valueAt(row, 'metrics', 'finish_ms'), 3, 'ms'))}</td><td>${escapeHtml(number(row.latency_gap_ms) === null ? '未记录' : `+${metric(row.latency_gap_ms, 3)} ms`)}<small>${escapeHtml(number(row.latency_gap_pct) === null ? '' : `+${metric(row.latency_gap_pct, 2)}%`)}</small></td><td>${escapeHtml(frequencyLabel(row.frequency_vector))}</td><td>${escapeHtml(statusLabel(row.selection_status))}</td></tr>`).join('')
  const candidateRows = candidates.slice(0, 200).map((row, index) => `<tr><td>${escapeHtml(String(index + 1))}</td><td>${escapeHtml(configurationLabel(row.configuration))}</td><td>${escapeHtml(metric(valueAt(row, 'metrics', 'finish_ms'), 3, 'ms'))}</td><td>${escapeHtml(metric(valueAt(row, 'comparison', 'latency_gain_pct'), 2, '%'))}</td><td>${escapeHtml(metric(valueAt(row, 'metrics', 'app_core_ms'), 3, 'core-ms'))}</td><td>${escapeHtml(frequencyLabel(row.frequency_vector))}</td><td>${escapeHtml(booleanLabel(row.feasible))}</td></tr>`).join('')
  const bestResult = best === EMPTY
    ? renderMissing('最优配置', '搜索记录没有 best；请检查搜索状态或可行域。')
    : `<div class="result-highlight"><div><span>模型预测 UI 回调耗时</span><strong>${escapeHtml(metric(bestMetrics.finish_ms, 3, 'ms'))}</strong><small>相对基线 ${escapeHtml(metric(bestComparison.latency_gain_pct, 2, '%'))}</small></div><div><span>应用 CPU 成本</span><strong>${escapeHtml(metric(bestMetrics.app_core_ms, 3, 'core-ms'))}</strong><small>变化 ${escapeHtml(metric(bestComparison.app_cpu_delta_ms, 3, 'core-ms'))}</small></div><div><span>核心与 Worker</span><strong>${escapeHtml(configurationLabel(best.configuration))}</strong><small>${escapeHtml(frequencyLabel(best.frequency_vector))}</small></div></div>`
  const recommendationTable = recommendations.length
    ? `<div class="table-wrap"><table><thead><tr><th>排名</th><th>核心组合</th><th>预测时延</th><th>距最快</th><th>核组频点</th><th>选择状态</th></tr></thead><tbody>${recommendationRows}</tbody></table></div>`
    : renderMissing('核心组合推荐')
  const candidateTable = candidates.length
    ? `<div class="table-wrap"><table><thead><tr><th>#</th><th>配置</th><th>预测时延</th><th>收益</th><th>应用 CPU</th><th>频点</th><th>可行</th></tr></thead><tbody>${candidateRows}</tbody></table></div>${candidates.length > 200 ? `<p class="table-footnote">表格展示前 200 / 当前传输 ${escapeHtml(String(candidates.length))} 个候选；当前传输数组保存在下方原始证据中。</p>` : ''}`
    : renderMissing('当前传输候选')
  const searchEvidence = `<details class="report-details search-details"><summary><span>搜索范围、候选配置与求解证据</span><small>展开查看搜索身份、可行域、当前传输候选和原始证据</small></summary><div class="details-body">${renderKeyValues([
    ['寻优 ID', search.id], ['分析 ID', search.analysis_id], ['状态', statusLabel(search.status)],
    ['成本模型', first(valueAt(search, 'model_package', 'id'), configuration.model_package_id)],
    ['负载特征版本', first(search.feature_profile_id, configuration.feature_profile_id)],
    ['候选任务数', metric(list(configuration.candidate_ids).length, 0)],
    ['已评估配置数', metric(provenance.evaluated_configurations, 0)], ['可行配置数', metric(provenance.feasible_configurations, 0)],
    ['核心组合数', metric(first(provenance.core_combination_count, recommendations.length), 0)],
    ['依赖策略', first(configuration.dependency_policy, search.dependency_policy)],
    ['稳健性', configuration.robustness === true ? '已启用 · 声明情景敏感性检查' : '未启用'],
    ['搜索完备性', string(provenance.scope, '未记录')],
  ])}${searchProjection}${renderRawEvidence('搜索配置', configuration)}${renderRawEvidence('最优配置完整结果', best === EMPTY ? undefined : best)}<h3 class="subheading">当前传输候选</h3>${candidateTable}${renderRawEvidence('寻优来源、假设与当前传输候选', {
    analysis_id: analysis.id, search_provenance: search.search_provenance, model_package: search.model_package,
    assumptions: search.assumptions, candidates: search.candidates, transport_projection: search.transport_projection,
    artifacts: search.artifacts,
  })}</div></details>`
  const content = search === EMPTY
    ? renderMissing('并行寻优记录', '没有可导出的优化目标、搜索空间、最优配置或候选结果。')
    : `<h3 class="subheading">寻优结果</h3><h4 class="result-heading">全局最优并行配置</h4>${bestResult}<h4 class="result-heading">按物理核心组合推荐</h4>${recommendationTable}<h3 class="subheading">单任务核频执行时间估算</h3>${renderTaskFrequencyCostModel(input)}<h3 class="subheading">并行配置寻优函数</h3>${renderOptimizationModel(search)}${searchEvidence}`
  return `<section class="report-section" id="section-5"><div class="section-number">05</div><div class="section-body"><header><p>PARALLEL OPTIMIZATION</p><h2>并行建模寻优方案、公式与结果</h2></header>
    ${renderChapterIntroduction('本章将阶段级负载特征、目标硬件和已校准成本模型组合成资源约束 DAG，在候选核心、Worker 数和 OPP 空间中搜索配置。先呈现可直接决策的最优结果，再给出单任务时延估算与全局优化函数，最后保留完整搜索证据供复核。')}
    ${content}
  </div></section>`
}

function renderWarnings(input: ParallelModelingReportInput): string {
  const warnings = list(input.warnings).map(item => typeof item === 'string' ? item : safeJson(item))
  if (!warnings.length) return ''
  return `<aside class="report-warnings"><h2>导出提示与已知限制</h2><ul>${warnings.map(item => `<li>${escapeHtml(item)}</li>`).join('')}</ul></aside>`
}

const REPORT_CSS = `
:root{color-scheme:light;--ink:#172033;--muted:#657086;--line:#dce2eb;--soft:#f5f7fa;--paper:#fff;--blue:#2457a7;--blue-soft:#eef4ff;--red:#a3302b;--red-soft:#fff1ef;--green:#187356;--green-soft:#eaf8f2;--amber:#8a5a05;--amber-soft:#fff7df;font-family:"Segoe UI","Microsoft YaHei UI","Microsoft YaHei",Arial,sans-serif}
*{box-sizing:border-box}body{margin:0;background:#e9edf3;color:var(--ink);font-size:14px;line-height:1.62}main{width:min(1180px,calc(100% - 32px));margin:24px auto 56px;background:var(--paper);box-shadow:0 18px 60px rgba(26,42,68,.12)}.report-cover{padding:54px 64px 42px;border-top:7px solid var(--red);background:linear-gradient(135deg,#fff 0,#fff 58%,#f2f6fb 100%)}.report-cover .kicker{margin:0 0 14px;color:var(--red);font-size:12px;font-weight:800;letter-spacing:.18em}.report-cover h1{margin:0;max-width:850px;font-size:34px;line-height:1.25;letter-spacing:-.02em}.report-cover .subtitle{max-width:850px;margin:14px 0 28px;color:var(--muted);font-size:16px}.cover-meta{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.cover-meta div{padding:12px 14px;border-left:2px solid var(--blue);background:rgba(255,255,255,.78)}.cover-meta span,.result-highlight span{display:block;color:var(--muted);font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.05em}.cover-meta strong{display:block;margin-top:3px;overflow-wrap:anywhere}.lineage{margin:0 64px 30px;padding:14px 16px;border:1px solid;border-radius:8px}.lineage strong,.lineage span{display:block}.lineage span{margin-top:3px;color:var(--muted)}.lineage.passed{border-color:#b7dfd0;background:var(--green-soft)}.lineage.warning{border-color:#efda9d;background:var(--amber-soft)}.lineage.failed{border-color:#e9b4af;background:var(--red-soft)}.lineage ul{margin:8px 0 0;padding-left:20px}.lineage code{margin-left:6px;color:var(--muted)}.report-nav{display:flex;gap:8px;flex-wrap:wrap;padding:0 64px 24px;border-bottom:1px solid var(--line)}.report-nav a{color:var(--blue);text-decoration:none;padding:6px 10px;background:var(--blue-soft);border-radius:4px;font-weight:600}.report-section{display:grid;grid-template-columns:62px 1fr;gap:24px;padding:44px 64px;border-bottom:1px solid var(--line)}.section-number{color:#a9b4c4;font:700 25px/1 Georgia,serif;padding-top:7px}.section-body{min-width:0}.section-body>header{margin-bottom:24px}.section-body>header p{margin:0 0 2px;color:var(--blue);font-size:10px;font-weight:800;letter-spacing:.16em}.section-body>header h2{margin:0;font-size:25px;line-height:1.25}.hero-grid{display:grid;grid-template-columns:1fr 1.35fr;gap:22px;align-items:start;padding:22px;background:linear-gradient(120deg,#f8fafc,#eef4ff);border:1px solid #d8e3f3;border-radius:8px}.hero-grid h3{margin:5px 0 3px;font-size:21px}.hero-grid p{margin:0;color:var(--muted)}.eyebrow{font-size:11px;color:var(--blue);font-weight:700}.subheading{margin:28px 0 10px;padding-left:10px;border-left:3px solid var(--red);font-size:16px}.key-values{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:1px;background:var(--line);border:1px solid var(--line);border-radius:6px;overflow:hidden}.key-values.compact{grid-template-columns:repeat(2,minmax(0,1fr));border:0}.key-values div{min-width:0;padding:10px 12px;background:#fff}.key-values dt{color:var(--muted);font-size:11px}.key-values dd{margin:2px 0 0;font-weight:650;overflow-wrap:anywhere}.table-wrap{margin-top:10px;overflow:auto;border:1px solid var(--line);border-radius:6px}table{width:100%;border-collapse:collapse;font-size:12px}th,td{padding:9px 10px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line)}th{position:sticky;top:0;background:#f0f3f7;color:#3c485c;font-size:11px;white-space:nowrap}tr:last-child td{border-bottom:0}td small{display:block;margin-top:2px;color:var(--muted)}.tag{display:inline-block;padding:2px 7px;border:1px solid #cbd8ed;border-radius:10px;color:var(--blue);background:var(--blue-soft);white-space:nowrap}.evidence-missing{margin:10px 0;padding:13px 15px;border:1px dashed #d9b35b;border-radius:6px;background:var(--amber-soft)}.evidence-missing strong,.evidence-missing span{display:block}.evidence-missing span{margin-top:2px;color:var(--muted)}.raw-evidence,.code-file{margin-top:12px;border:1px solid var(--line);border-radius:6px;background:#fafbfc}.raw-evidence summary,.code-file summary{cursor:pointer;padding:10px 12px;font-weight:650}.raw-evidence pre,.code-file pre{max-height:540px;margin:0;padding:14px;overflow:auto;border-top:1px solid var(--line);background:#172033;color:#dce6f5;font:12px/1.55 Consolas,"Cascadia Mono",monospace;white-space:pre-wrap;overflow-wrap:anywhere}.code-file summary{display:flex;justify-content:space-between;gap:16px}.code-file summary span{color:var(--muted);font-size:11px;font-weight:400}.method-note{margin-top:14px;padding:12px 14px;border-left:3px solid var(--blue);background:var(--soft)}.method-note p{margin:3px 0 0;color:var(--muted)}.optimization-model{margin-bottom:24px;border:1px solid #cdd9ea;border-radius:9px;overflow:hidden;background:#fff}.model-title{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:13px 16px;background:#1d3559;color:#fff}.model-title span{font-size:10px;letter-spacing:.15em;color:#cdd9ee}.equation{display:grid;grid-template-columns:90px 1fr;align-items:center;gap:12px;padding:22px 18px;background:linear-gradient(90deg,#f8fbff,#eef4ff)}.equation span,.constraints>strong{color:var(--muted);font-size:11px}.equation code{font:600 19px/1.4 "Cambria Math","Times New Roman",serif;color:#162d50;overflow-wrap:anywhere}.constraints{display:grid;grid-template-columns:90px 1fr;gap:7px 12px;padding:14px 18px;border-top:1px solid #dce5f1}.constraints>strong{grid-row:1/span 6}.constraints code{font:15px/1.4 "Cambria Math","Times New Roman",serif}.model-notes{padding:12px 18px;border-top:1px solid var(--line);background:#fbfcfe;color:var(--muted);font-size:12px}.model-notes p{margin:4px 0}.model-notes code{color:#273a57}.result-highlight{display:grid;grid-template-columns:.8fr .8fr 1.4fr;gap:10px}.result-highlight>div{padding:16px;border:1px solid #cdd9ea;border-radius:7px;background:#f7faff}.result-highlight strong{display:block;margin-top:5px;font-size:19px;line-height:1.3}.result-highlight small{display:block;margin-top:4px;color:var(--muted)}.table-footnote{color:var(--muted);font-size:11px}.report-warnings{margin:34px 64px;padding:18px 20px;border:1px solid #e9b4af;border-radius:8px;background:var(--red-soft)}.report-warnings h2{margin:0 0 8px;font-size:16px}.report-warnings ul{margin:0;padding-left:20px}.report-footer{padding:26px 64px 42px;color:var(--muted);font-size:11px}.report-footer strong{color:var(--ink)}
.chapter-introduction{max-width:920px;margin:-7px 0 22px;padding:13px 16px;border-left:3px solid #b8c5d8;background:#f8fafc;color:#49566b;font-size:13px;line-height:1.75}.stage-ledger{position:relative}.stage-selector{position:absolute;width:1px;height:1px;opacity:0}.stage-timeline{position:relative;height:66px;border:1px solid var(--line);border-radius:7px;background:#f0f2f5;overflow:hidden}.stage-segment{position:absolute;top:10px;bottom:10px;display:flex;min-width:2px;flex-direction:column;justify-content:center;align-items:center;overflow:hidden;padding:5px 2px;border:1px solid rgba(255,255,255,.8);border-radius:4px;cursor:pointer;color:#17304f;background:#bed2ee;transition:filter .15s,outline .15s}.stage-selector:focus-visible+label{outline:3px solid rgba(36,87,167,.35);outline-offset:2px}.stage-segment.compute{background:#bad9f2}.stage-segment.memory{background:#cce3d4}.stage-segment.mixed{background:#eadcb9}.stage-segment.service,.stage-segment.scheduler{background:#d8cceb}.stage-segment.unknown{background:#dfe3e9}.stage-segment:hover{filter:saturate(1.18)}.stage-segment b,.stage-segment span{max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.stage-segment span{font-size:10px}.timeline-caption{margin:7px 2px 10px;color:var(--muted);font-size:11px}.stage-detail{display:none;padding:15px;border:1px solid #cfd9e8;border-radius:7px;background:#fbfdff}.stage-detail>header{display:flex;align-items:start;justify-content:space-between;gap:12px;margin-bottom:11px}.stage-detail>header span:first-child{color:var(--blue);font-size:11px;font-weight:700}.stage-detail h4{margin:1px 0 0;font-size:16px}.report-details{margin-top:18px;border:1px solid var(--line);border-radius:8px;background:#fafbfc;overflow:hidden}.report-details>summary{display:flex;align-items:center;justify-content:space-between;gap:20px;padding:14px 16px;cursor:pointer;color:#26364f;font-weight:700}.report-details>summary small{color:var(--muted);font-size:11px;font-weight:400;text-align:right}.report-details[open]>summary{border-bottom:1px solid var(--line);background:#f2f5f9}.details-body{padding:4px 16px 18px}.curve-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin:10px 0 12px}.curve-card{min-width:0;padding:13px;border:1px solid #d4deeb;border-radius:8px;background:#fff}.curve-card>header{display:flex;align-items:center;justify-content:space-between;gap:12px}.curve-card>header strong{font-size:14px}.curve-card>header span{color:var(--muted);font-size:11px}.curve-card svg{display:block;width:100%;height:auto;margin-top:7px;overflow:visible}.curve-axis{stroke:#728198;stroke-width:1}.curve-chart-grid{stroke:#e4e9f0;stroke-width:1}.curve-mark{stroke:#8d99a9;stroke-width:1}.curve-tick{fill:#68758a;font:9px "Segoe UI","Microsoft YaHei UI",sans-serif;text-anchor:middle}.curve-tick.y{text-anchor:end}.curve-axis-label{fill:#566379;font:10px "Segoe UI","Microsoft YaHei UI",sans-serif;text-anchor:middle}.curve-fit{fill:none;stroke:#2457a7;stroke-width:2.2;stroke-linecap:round;stroke-linejoin:round}.curve-observed{fill:#fff;stroke:#a3302b;stroke-width:2}.curve-knee{stroke:#9a6a16;stroke-width:1.2;stroke-dasharray:4 3}.curve-legend{display:flex;gap:12px;flex-wrap:wrap;color:var(--muted);font-size:10px}.curve-legend span{display:flex;align-items:center;gap:4px}.curve-legend i{display:inline-block;width:12px;height:3px;background:#2457a7}.curve-legend i.observed{width:7px;height:7px;border:2px solid #a3302b;border-radius:50%;background:#fff}.curve-legend i.knee{height:8px;width:1px;border-left:1px dashed #9a6a16;background:none}.curve-formula{margin:8px 0 0;color:#5b6678;font-size:10px;line-height:1.55}.curve-formula code{color:#243b60;font-family:"Cambria Math","Times New Roman",serif}.result-heading{margin:14px 0 8px;font-size:13px}.task-cost-model{border:1px solid #cdd9ea;border-radius:9px;overflow:hidden;background:#fff}.task-equations{display:grid;grid-template-columns:1.35fr .9fr 1.35fr}.task-equations>div{min-width:0;padding:18px;background:linear-gradient(90deg,#fafcff,#f1f6fd)}.task-equations>div+div{border-left:1px solid #dce5f1}.task-equations span{display:block;margin-bottom:7px;color:var(--muted);font-size:10px;font-weight:700;letter-spacing:.06em}.task-equations code{font:600 14px/1.55 "Cambria Math","Times New Roman",serif;color:#162d50;overflow-wrap:anywhere}.task-cost-model>p{margin:0;padding:12px 16px;border-top:1px solid var(--line);background:#fbfcfe;color:var(--muted);font-size:12px}.task-cost-model>p code{color:#273a57}.calibration-details{margin-top:24px}
@media(max-width:760px){main{width:100%;margin:0;box-shadow:none}.report-cover,.report-section{padding-left:20px;padding-right:20px}.lineage,.report-warnings{margin-left:20px;margin-right:20px}.report-nav{padding-left:20px;padding-right:20px}.cover-meta,.key-values{grid-template-columns:repeat(2,minmax(0,1fr))}.report-section{grid-template-columns:1fr;gap:8px}.section-number{font-size:17px}.hero-grid,.result-highlight,.curve-grid,.task-equations{grid-template-columns:1fr}.equation,.constraints{grid-template-columns:1fr}.constraints>strong{grid-row:auto}.task-equations>div+div{border-left:0;border-top:1px solid #dce5f1}.report-details>summary{align-items:start;flex-direction:column;gap:2px}.report-details>summary small{text-align:left}}
@media print{body{background:#fff;font-size:10pt}main{width:100%;margin:0;box-shadow:none}.report-cover{padding-top:20mm}.report-nav{display:none}.report-section{break-before:page;padding:16mm 14mm}.report-section:first-of-type{break-before:auto}.table-wrap{overflow:visible}.raw-evidence,.code-file,.curve-card{break-inside:avoid}.raw-evidence:not([open]) pre,.code-file:not([open]) pre{display:none}.stage-detail{display:block!important;margin-top:6px}.stage-selector{display:none}th{position:static}.report-footer{padding:8mm 14mm}}
`

export function windowsSafeFilename(value: string, fallback = 'parallel-modeling-report.html'): string {
  const requested = string(value, fallback)
  const extensionMatch = requested.match(/(\.[A-Za-z0-9]{1,10})$/)
  const extension = extensionMatch?.[1] ?? ''
  let base = extension ? requested.slice(0, -extension.length) : requested
  base = Array.from(base, character => character.charCodeAt(0) < 32 || character.charCodeAt(0) === 127 ? '-' : character)
    .join('').replace(/[<>:"/\\|?*]/g, '-').replace(/\s+/g, ' ').replace(/[. ]+$/g, '').trim()
  if (!base) base = extension ? fallback.replace(/\.[^.]+$/, '') : fallback
  if (/^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(base)) base = `_${base}`
  base = base.slice(0, Math.max(1, 120 - extension.length)).replace(/[. ]+$/g, '') || 'report'
  return `${base}${extension}`
}

export function parallelModelingReportFilename(input: ParallelModelingReportInput): string {
  const dataset = object(input.dataset)
  const search = object(input.search)
  const scenario = string(dataset.name, '未命名负载')
  const searchId = string(search.id, '未寻优')
  const day = isoTime(input.generatedAt).slice(0, 10)
  return windowsSafeFilename(`并行建模报告-${scenario}-${searchId}-${day}.html`)
}

export function buildParallelModelingHtmlReport(input: ParallelModelingReportInput): string {
  const dataset = object(input.dataset)
  const analysis = object(input.analysis)
  const search = object(input.search)
  const generatedAt = isoTime(input.generatedAt)
  const title = string(input.title, '并行建模与寻优分析报告')
  const scenario = string(dataset.name, string(analysis.name, '未命名负载'))
  const modelId = string(first(valueAt(search, 'model_package', 'id'), valueAt(search, 'search_configuration', 'model_package_id')), '未绑定')
  return `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"><title>${escapeHtml(title)} · ${escapeHtml(scenario)}</title><style>${REPORT_CSS}</style></head>
<body><main data-report-schema="parallel-modeling-html-report/v1"><header class="report-cover"><p class="kicker">HETEROGENEOUS PARALLEL MODELING · EVIDENCE REPORT</p><h1>${escapeHtml(title)}</h1><p class="subtitle">汇总负载场景、阶段特征、硬件画像、微基准校准证据以及并行配置搜索结果。所有“实测”“估算”和“模型预测”按原始记录语义分别呈现。</p><div class="cover-meta"><div><span>负载场景</span><strong>${escapeHtml(scenario)}</strong></div><div><span>数据集 / 分析</span><strong>${escapeHtml(string(dataset.id, '未绑定'))} / ${escapeHtml(string(analysis.id, '未绑定'))}</strong></div><div><span>成本模型 / 寻优</span><strong>${escapeHtml(modelId)} / ${escapeHtml(string(search.id, '未绑定'))}</strong></div><div><span>生成时间（UTC）</span><strong>${escapeHtml(generatedAt)}</strong></div></div></header>
${renderLineage(input)}<nav class="report-nav" aria-label="报告目录"><a href="#section-1">01 负载场景</a><a href="#section-2">02 负载特征</a><a href="#section-3">03 硬件模型</a><a href="#section-4">04 寻优校准</a><a href="#section-5">05 并行寻优</a></nav>
${renderScenario(input)}${renderFeatures(input)}${renderHardware(input)}${renderCalibration(input)}${renderParallelModeling(input)}${renderWarnings(input)}
<footer class="report-footer"><strong>证据声明：</strong>报告是所选记录在 ${escapeHtml(generatedAt)} 的离线快照。Trace / PMU 数据按记录标为实测、估算或缺失；并行时延与收益为成本模型在已声明搜索空间内的条件预测，不等同于应用改造后的真机验证结果。</footer></main></body></html>`
}

export function buildParallelModelingReport(input: ParallelModelingReportInput): ParallelModelingReportArtifact {
  return {
    filename: parallelModelingReportFilename(input),
    html: buildParallelModelingHtmlReport(input),
    mimeType: 'text/html;charset=utf-8',
  }
}
