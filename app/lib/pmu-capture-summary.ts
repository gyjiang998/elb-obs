type RecordValue = Record<string, unknown>
export type PmuTone = 'good' | 'warn' | 'error' | ''
export type PmuDetail = { label: string; value: string }
export type PmuDiagnostic = { message: string; suggestion: string; severity: string }
export type PmuGroupSummary = { id: string; label: string; tone: PmuTone; events: string; unavailable: string; coverage: string }
export type PmuCaptureSummary = {
  status: string
  legacy: boolean
  countLabel: string
  countTone: PmuTone
  timingLabel: string
  timingTone: PmuTone
  shortReason: string
  countSummary: string
  timingSummary: string
  coverageSummary: string
  legacyNote: string
  details: PmuDetail[]
  diagnostics: PmuDiagnostic[]
  rawReason: string
  groups: PmuGroupSummary[]
}

const object = (value: unknown): RecordValue => value && typeof value === 'object' && !Array.isArray(value) ? value as RecordValue : {}
const numeric = (value: unknown): number | null => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null
const string = (value: unknown): string => typeof value === 'string' ? value.trim() : ''
const numberText = (value: number | null): string => value === null ? '未记录' : value.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
const duration = (value: number | null, unit: string): string => value === null ? '未记录' : `${numberText(value)} ${unit}`
const chinese = (value: string): boolean => /[\u3400-\u9fff]/u.test(value)

/** Read-only presentation of both current validation and historical manifests. */
export function summarizePmuCapture(stepValue: unknown): PmuCaptureSummary | null {
  const step = object(stepValue)
  if (step.captured === false) return null
  const collector = object(step.collector_status).pmu
  const collectorObject = object(collector)
  const status = typeof collector === 'string' ? collector : string(collectorObject.status)
  const validation = object(step.pmu_validation)
  const hasValidation = Object.keys(validation).length > 0
  if (!hasValidation && (!status || ['not_requested', 'disabled', 'skipped'].includes(status))) return null
  const metadata = object(validation.metadata)
  const coverage = object(validation.coverage)
  const sampling = object(validation.sampling_quality)
  const stop = object(validation.stop)
  const legacy = hasValidation && !string(validation.validator_version)
  const rows = numeric(validation.rows)
  const good = numeric(validation.usable_counter_rows)
  // Earlier validators counted every non-active row as a read error. Do not
  // silently turn that aggregate into either actual failures or zero baselines.
  const readErrors = legacy ? null : numeric(validation.read_error_rows)
  const baseline = legacy ? null : numeric(validation.inactive_baseline_rows)
  const oldUnusable = legacy ? numeric(validation.read_error_rows) : null
  const requestedNs = numeric(metadata.period_ns_requested)
  const requested = numeric(sampling.requested_period_us) ?? (requestedNs !== null && requestedNs > 0 ? requestedNs / 1000 : null)
  const median = numeric(sampling.observed_median_interval_us)
  const p95 = numeric(sampling.observed_p95_interval_us)
  const maximum = numeric(sampling.maximum_interval_us)
  const skipped = numeric(sampling.skipped_deadlines) ?? numeric(metadata.skipped_deadlines)
  const first = numeric(coverage.first_sample_ns)
  const last = numeric(coverage.last_sample_ns)
  const span = first !== null && last !== null && last >= first ? (last - first) / 1e6 : null
  const start = numeric(coverage.step_start_ns)
  const end = numeric(coverage.step_end_ns)
  const window = start !== null && end !== null && end >= start ? (end - start) / 1e6 : null
  const estimatedMean = legacy && span !== null && rows !== null && rows > 1 ? span * 1000 / (rows - 1) : null
  const diagnostics: PmuDiagnostic[] = (Array.isArray(validation.diagnostics) ? validation.diagnostics : []).map(object).map(item => ({
    message: string(item.message), suggestion: string(item.suggestion), severity: string(item.severity),
  })).filter(item => item.message)
  const groups: PmuGroupSummary[] = (Array.isArray(validation.groups) ? validation.groups : []).map(object).map(item => {
    const status = string(item.status)
    const labels: Record<string, string> = { collected: '已采集', estimated: '复用 · 需估计', not_running: '未运行', unavailable: '不可用', invalid: '校验失败' }
    const unavailable = Array.isArray(item.unavailable_events) ? item.unavailable_events.map(event => typeof event === 'string' ? event : string(object(event).name) || string(object(event).event)).filter(Boolean).join(' / ') : ''
    const coverage = numeric(item.running_ratio)
    return { id: string(item.id), label: labels[status] ?? status, tone: status === 'collected' ? 'good' : status === 'invalid' ? 'error' : 'warn',
      events: Array.isArray(item.event_names) ? item.event_names.filter(event => typeof event === 'string').join(' / ') : '', unavailable,
      coverage: coverage === null ? '未记录' : `${numberText(coverage * 100)}%` }
  })
  const rawReason = string(validation.reason) || string(collectorObject.reason) || string(collectorObject.message)
  const invalid = validation.valid === false || ['invalid', 'failed', 'unavailable', 'exited_early'].includes(status)
  const complete = validation.quality_complete === true && !invalid
  const countLabel = invalid ? '不可用' : complete ? '完整' : legacy ? '旧记录需复核' : validation.quality_complete === false || status === 'partial' ? '不完整' : '未校验'
  const countTone: PmuTone = invalid ? 'error' : complete ? 'good' : hasValidation || status === 'partial' ? 'warn' : ''
  const degraded = sampling.status === 'degraded' || (skipped !== null && skipped > 0)
  const timingLabel = degraded ? '分辨率降低' : sampling.status === 'observed' ? '未见降分辨率' : '未记录实际间隔'
  const timingTone: PmuTone = degraded ? 'warn' : sampling.status === 'observed' ? 'good' : ''
  const countSummary = `有效计数 ${numberText(good)} / ${numberText(rows)} 条 · 真实读取失败 ${numberText(readErrors)} · 零基线 ${numberText(baseline)}`
  const timingSummary = `目标 ${duration(requested, 'μs')} · 实际中位数 ${duration(median, 'μs')} · P95 ${duration(p95, 'μs')}`
  const coverageLabel = coverage.status === 'full' ? '采样时间覆盖完整操作窗口' : coverage.status === 'partial' ? '采样时间未覆盖完整操作窗口' : '操作窗口覆盖情况未记录'
  const coverageSummary = `${coverageLabel}${span !== null ? ` · 记录跨度 ${duration(span, 'ms')}` : ''}${window !== null ? ` / 操作窗口 ${duration(window, 'ms')}` : ''}`
  const legacyNote = legacy ? oldUnusable !== null && oldUnusable > 0
    ? `历史记录将 ${numberText(oldUnusable)} 条未计入有效计数的样本统一归入异常，可能包含零基线，不能据此判断真实读取失败数。`
    : '历史记录未区分零基线与真实读取失败，沿用原始状态展示；未记录的指标不作为零值。' : ''
  const important = diagnostics.find(item => item.severity === 'error') ?? diagnostics.find(item => item.severity === 'warning')
  let shortReason = important && chinese(important.message) ? important.message : ''
  if (!shortReason && rawReason && chinese(rawReason)) shortReason = rawReason
  if (!shortReason) {
    if (validation.metadata_valid === false) shortReason = '样本数、时钟或目标线程与采集记录不一致。'
    else if (validation.ordered_samples === false) shortReason = '采样时间戳顺序或读取区间异常。'
    else if (good !== null && good < 2) shortReason = '有效计数不足两条，无法形成可靠差分。'
    else if (invalid) shortReason = '未取得可用计数；请查看下方诊断与采样记录。'
    else if (legacy && oldUnusable !== null && oldUnusable > 0) shortReason = '旧校验混合统计零基线与异常样本，需复核；详见下方。'
    else if (degraded) shortReason = complete ? '计数完整，但实际采样节奏降分辨率。' : '实际采样节奏降分辨率；详见下方。'
    else if (complete) shortReason = '计数与采集窗口校验通过。'
    else if (coverage.status === 'partial') shortReason = '采样时间未完整覆盖操作窗口。'
    else shortReason = '缺少完整校验信息，请查看下方已返回的采样指标。'
  }
  const details: PmuDetail[] = [
    { label: '有效活动计数 / 总样本', value: `${numberText(good)} / ${numberText(rows)}` },
    { label: '真实读取失败', value: numberText(readErrors) },
    { label: '初始零基线', value: numberText(baseline) },
    { label: '异常计数 / 事件未运行', value: `${numberText(numeric(validation.invalid_counter_rows))} / ${numberText(numeric(validation.unscheduled_counter_rows))}` },
    { label: '目标采样间隔', value: duration(requested, 'μs') },
    { label: '实际间隔：中位数 / P95', value: `${duration(median, 'μs')} / ${duration(p95, 'μs')}` },
    { label: '最大采样间隔', value: duration(maximum, 'μs') },
    { label: '跳过的采样时点', value: numberText(skipped) },
    { label: '采样时间跨度 / 操作窗口', value: `${duration(span, 'ms')} / ${duration(window, 'ms')}` },
    { label: '窗口缺口：开始 / 结束', value: `${duration(numeric(coverage.missing_start_ms), 'ms')} / ${duration(numeric(coverage.missing_end_ms), 'ms')}` },
    { label: '目标线程', value: metadata.target_tid == null ? '未记录' : String(metadata.target_tid) },
    { label: '采集范围', value: metadata.scope === 'target_thread_user_only_no_inherit' ? '目标线程用户态，不含子线程' : validation.scope === 'target_main_thread' ? '目标主线程' : '未记录' },
  ]
  if (estimatedMean !== null) details.push({ label: '旧记录平均间隔（按时间跨度估算）', value: duration(estimatedMean, 'μs') })
  if (numeric(sampling.effective_period_us) !== null) details.push({ label: '采样器配置的实际目标间隔', value: duration(numeric(sampling.effective_period_us), 'μs') })
  if (Array.isArray(metadata.event_names)) details.push({ label: '采集事件', value: metadata.event_names.filter(item => typeof item === 'string').join(' / ') || '未记录' })
  if (stop.expected_stop !== undefined) details.push({ label: '采样器结束', value: stop.expected_stop === true ? '正常结束或按计划停止' : stop.exit_code == null ? '退出状态未记录' : '非预期结束' })
  return { status, legacy, countLabel, countTone, timingLabel, timingTone, shortReason, countSummary, timingSummary, coverageSummary, legacyNote, details, diagnostics, rawReason, groups }
}
