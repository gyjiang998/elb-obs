import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

const source = await readFile(new URL('../../app/lib/pmu-capture-summary.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const { summarizePmuCapture } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)

const current = () => ({
  collector_status: { pmu: 'collected' },
  pmu_validation: {
    validator_version: '2.0.0', valid: true, quality_complete: true,
    rows: 8000, usable_counter_rows: 6000, read_error_rows: 0, inactive_baseline_rows: 2000,
    invalid_counter_rows: 0, unscheduled_counter_rows: 0,
    coverage: { status: 'full', first_sample_ns: 1000000000, last_sample_ns: 2000000000, step_start_ns: 1200000000, step_end_ns: 1800000000, missing_start_ms: 0, missing_end_ms: 0 },
    metadata: { period_ns_requested: 100000, target_tid: 100, scope: 'target_thread_user_only_no_inherit', event_names: ['cycles', 'instructions'] },
    sampling_quality: { status: 'degraded', requested_period_us: 100, observed_median_interval_us: 150.5, observed_p95_interval_us: 250, maximum_interval_us: 1000, skipped_deadlines: 1000 },
    diagnostics: [{ code: 'PMU_INACTIVE_BASELINE', severity: 'info', message: '初始零累计样本保留为基线。' }, { code: 'PMU_SAMPLING_CADENCE_DEGRADED', severity: 'warning', message: '实际采样节奏未达到请求间隔。', suggestion: '依据实际间隔判断最小阶段。' }],
  },
})

test('complete counter capture and reduced temporal resolution remain separate', () => {
  const step = current()
  const snapshot = JSON.stringify(step)
  const summary = summarizePmuCapture(step)
  assert.equal(summary.countLabel, '完整')
  assert.equal(summary.countTone, 'good')
  assert.equal(summary.timingLabel, '分辨率降低')
  assert.equal(summary.timingTone, 'warn')
  assert.match(summary.countSummary, /真实读取失败 0 · 零基线 2,000/)
  assert.match(summary.timingSummary, /目标 100 μs · 实际中位数 150.5 μs · P95 250 μs/)
  assert.match(summary.coverageSummary, /覆盖完整操作窗口/)
  assert.equal(summary.shortReason, '实际采样节奏未达到请求间隔。')
  assert.equal(JSON.stringify(step), snapshot)
})

test('historical mixed read_error_rows is never reported as actual failure or baseline', () => {
  const step = { collector_status: { pmu: 'partial' }, pmu_validation: {
    valid: true, quality_complete: false, rows: 8764, usable_counter_rows: 6112, read_error_rows: 2652,
    coverage: { status: 'full', first_sample_ns: 1184227893786250, last_sample_ns: 1184229756321875 },
    metadata: { period_ns_requested: 100000, skipped_deadlines: 9865 },
  } }
  const snapshot = JSON.stringify(step)
  const summary = summarizePmuCapture(step)
  assert.equal(summary.status, 'partial')
  assert.equal(summary.countLabel, '旧记录需复核')
  assert.match(summary.countSummary, /有效计数 6,112 \/ 8,764 条 · 真实读取失败 未记录 · 零基线 未记录/)
  assert.match(summary.legacyNote, /2,652.*不能据此判断真实读取失败数/)
  assert.equal(summary.timingLabel, '分辨率降低')
  assert.match(summary.timingSummary, /实际中位数 未记录 · P95 未记录/)
  assert.match(summary.details.find(item => item.label.includes('旧记录平均')).value, /^212\.55 μs$/)
  assert.equal(JSON.stringify(step), snapshot)
})

test('real read failures and partial coverage have explicit diagnostics', () => {
  const step = current()
  step.collector_status.pmu = { status: 'partial', reason: '部分采集' }
  step.pmu_validation.quality_complete = false
  step.pmu_validation.read_error_rows = 3
  step.pmu_validation.coverage.status = 'partial'
  step.pmu_validation.diagnostics = [{ severity: 'warning', message: '3条样本发生实际计数读取错误。', suggestion: '检查采样器日志。' }]
  const summary = summarizePmuCapture(step)
  assert.equal(summary.countLabel, '不完整')
  assert.match(summary.countSummary, /真实读取失败 3/)
  assert.match(summary.coverageSummary, /未覆盖完整操作窗口/)
  assert.equal(summary.shortReason, '3条样本发生实际计数读取错误。')
})

test('permission failures and absent telemetry do not become fake zero counts', () => {
  const summary = summarizePmuCapture({ collector_status: { pmu: 'unavailable' }, pmu_validation: {
    validator_version: '2.0.0', valid: false, quality_complete: false,
    diagnostics: [{ severity: 'error', message: 'PMU计数器访问权限不足。', suggestion: '检查设备权限。' }],
  } })
  assert.equal(summary.countLabel, '不可用')
  assert.equal(summary.shortReason, 'PMU计数器访问权限不足。')
  assert.match(summary.countSummary, /真实读取失败 未记录 · 零基线 未记录/)
  assert.equal(summary.timingLabel, '未记录实际间隔')
})

test('historical invalid validation derives specific Chinese reasons without diagnostics', () => {
  const summary = summarizePmuCapture({ collector_status: { pmu: 'invalid' }, pmu_validation: { valid: false, metadata_valid: false, rows: 8, usable_counter_rows: 6 } })
  assert.match(summary.shortReason, /样本数、时钟或目标线程/)
  const error = summarizePmuCapture({ collector_status: { pmu: 'failed' }, pmu_validation: { valid: false, reason: 'No such file or directory' } })
  assert.match(error.shortReason, /未取得可用计数/)
  assert.equal(error.rawReason, 'No such file or directory')
})

test('only observed timing may report no reduction; missing data remains unknown', () => {
  const step = current()
  step.pmu_validation.sampling_quality.status = 'observed'
  step.pmu_validation.sampling_quality.skipped_deadlines = 0
  assert.equal(summarizePmuCapture(step).timingLabel, '未见降分辨率')
  delete step.pmu_validation.sampling_quality
  assert.equal(summarizePmuCapture(step).timingLabel, '未记录实际间隔')
  assert.equal(summarizePmuCapture({ captured: false, ...current() }), null)
  assert.equal(summarizePmuCapture({ collector_status: { trace: 'collected' } }), null)
  assert.equal(summarizePmuCapture({ collector_status: { pmu: 'not_requested' } }), null)
})

test('multi-group capture distinguishes multiplexed estimates, missing events and absent coverage', () => {
  const step = current()
  step.pmu_validation.validator_version = '3.0.0'
  step.pmu_validation.quality_complete = false
  step.pmu_validation.groups = [
    { id: 'base', status: 'estimated', running_ratio: .25, event_names: ['cycles', 'instructions'], unavailable_events: [] },
    { id: 'cache', status: 'collected', running_ratio: 1, event_names: ['generic_cache_misses'], unavailable_events: [{ name: 'generic_cache_references', errno: 95 }] },
    { id: 'llc', status: 'unavailable', running_ratio: null, event_names: [], unavailable_events: ['llc_read_misses'] },
  ]
  step.pmu_validation.sampling_quality.effective_period_us = 100
  const summary = summarizePmuCapture(step)
  assert.equal(summary.countLabel, '不完整')
  assert.deepEqual(summary.groups.map(item => [item.id, item.label, item.coverage]), [
    ['base', '复用 · 需估计', '25%'], ['cache', '已采集', '100%'], ['llc', '不可用', '未记录'],
  ])
  assert.equal(summary.groups[1].unavailable, 'generic_cache_references')
  assert.equal(summary.groups[2].unavailable, 'llc_read_misses')
  assert.equal(summary.details.find(item => item.label === '采样器配置的实际目标间隔').value, '100 μs')
  assert.deepEqual(summarizePmuCapture(current()).groups, [])
})
