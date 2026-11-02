/**
 * Browser checks for scheduler decomposition, PMU evidence, CSV exports and automatic event groups.
 * WORKLOAD_FEATURES_QA_WEB=http://127.0.0.1:4032 node tools/tests/workload-features-evidence-browser.mjs
 * Uses backend-generated profiles plus an unchanged real legacy-stage excerpt.
 * Device, Agent, jobs and mutations are isolated; no collection or real analysis runs.
 */
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const root = fileURLToPath(new URL('../../', import.meta.url))
const web = process.env.WORKLOAD_FEATURES_QA_WEB ?? 'http://127.0.0.1:4032'
assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(new URL(web).hostname), 'Use a local preview')
const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE ?? '/Users/j00922218/.npm/_npx/e41f203b7505f1fb/node_modules/playwright/index.mjs').href)
const generated = JSON.parse(await fs.readFile(path.join(root, 'tools/tests/fixtures/workload-feature-evidence.json'), 'utf8'))
const legacy = JSON.parse(await fs.readFile(path.join(root, 'tools/tests/fixtures/workload-feature-legacy-boundary.json'), 'utf8'))
const expectedVersion = JSON.parse(await fs.readFile(path.join(root, 'package.json'), 'utf8')).version
const out = path.join(root, '.local-artifacts/local-preview/workload-features-evidence-browser')
await fs.mkdir(out, { recursive: true })
const report = { cases: [], pageErrors: [], blockedWrites: [], savedModes: [], csvRequests: [], downloads: [], realDeviceActions: 0, realAgentRequests: 0 }
const estimated = generated.profiles.find(profile => profile.stages[0].metric_evidence.instructions.status === 'estimated')
const weightedShort = generated.profiles.find(profile => profile.stages[0].metric_evidence.instructions.estimate_scope === 'covered_stage_window' && profile.stages[0].metric_evidence.instructions.bounds?.[0] === 0)
const exact = generated.profiles.find(profile => profile.stages[0].metric_evidence.instructions.status === 'exact')
assert.ok(estimated && weightedShort && exact, 'Backend fixtures must include multiplexed, short-window weighted and exact evidence')
// Deliberately synthetic scheduling display case: keep each partition disjoint.
const scheduling = structuredClone(exact)
scheduling.id = 'wf_qa_scheduling_partition'
Object.assign(scheduling.stages[0], { name: 'QA 调度拆解', wall_ms: 10, cpu_ms: 4, states_ms: { on_cpu: 4, runnable: 3, blocked: 2, unknown: 1 },
  time_breakdown: { partition_ms: { running: 4, preempted_wait: 1, runnable_other: 2, blocked: 2, unknown: 1 }, runnable_total_ms: 3, preemption_evidence_available: true, preemption_event_count: 1, irq_overlap_ms: .2, softirq_overlap_ms: .1, notes: ['QA R+ 来源已确认；IRQ 重叠不属于线程抢占等待。'] } })
Object.assign(scheduling.stages[0].scheduling, { binder_blocked_ms: 1.5, service_cpu_ms: .5, blocked_ms: 2, runnable_ms: 3 })
scheduling.summary.duration_ms = 10
const profiles = [...generated.profiles, legacy, scheduling]
// Backend tests cover column semantics; these bytes exercise browser transport and scope.
const csvBytes = (value, view) => Buffer.from('\ufeff' + (view === 'stages'
  ? '特征版本,阶段,墙钟耗时(ms),Running(ms)\r\n' + value.stages.map(stage => `${value.id},${stage.display_id},${stage.wall_ms},${stage.cpu_ms}`).join('\r\n')
  : '特征版本,阶段,指标,数值,状态\r\n' + value.stages.map(stage => `${value.id},${stage.display_id},instructions,2000,exact`).join('\r\n')) + '\r\n', 'utf8')
let failCsvExport = false
const datasets = [...generated.datasets, { id: legacy.dataset_id, name: 'QA 历史短阶段', status: 'ready', summary: { duration_ms: legacy.summary.duration_ms, builditem_count: 1 } }]
const steps = [{ step_id: 'step_01', ordinal: 1, title: 'QA 等待', operation: 'wait', seconds: 0, expected_targets: [] }]
const capture = { selected_step_ids: ['step_01'], collectors: ['trace', 'pmu'], preparation_mode: 'auto', pmu: { binary_path: '/data/local/tmp/thread_pmu_sampler', period_us: 10, duration_ms: 1000, group: 'auto', reader_cpu: 0 } }
const profile = { bundle: 'qa.fixture.only', ability: 'MainAbility', screen_size: [1280, 2832], targets: {}, trace_categories: ['sched'] }
const captureCatalog = { templates: [{ template_id: 'qa_fixture', name: 'QA 采集场景', steps }], default_template_id: 'qa_fixture', default_profile: profile, default_capture: capture, operations: ['wait'], pmu_sampler: { period_us_range: [10, 10000], supported_modes: ['auto', 'cache', 'branch', 'stall', 'l1d', 'llc', 'dtlb'], auto_event_count: 14 } }
const legacyPlan = { plan_id: 'plan_qa_legacy_cache', template_id: 'qa_fixture', title: 'QA 历史单组计划', natural_language_steps: '等待', steps, profile, capture: { ...capture, pmu: { ...capture.pmu, group: 'cache' } }, created_at: '2026-10-23T08:00:00Z' }
const allPlan = { ...legacyPlan, plan_id: 'plan_qa_all_alias', title: 'QA all 别名计划', capture: { ...capture, pmu: { ...capture.pmu, group: 'all' } } }
const run = { run_id: 'capture_qa_multigroup', plan_id: legacyPlan.plan_id, title: 'QA 多组采集', status: 'partial', created_at: '2026-10-23T09:00:00Z', connection_mode: 'local', device_serial: 'QA_NO_REAL_DEVICE', manifest: { steps: [{ ...steps[0], captured: true, collector_status: { pmu: 'partial' }, pmu_validation: {
  validator_version: '3.0.0', schema_version: 'pmu_capture_validation/v3', valid: true, quality_complete: false, rows: 100, usable_counter_rows: 90,
  read_error_rows: 0, inactive_baseline_rows: 10, coverage: { status: 'full' }, metadata: { target_tid: 100, scope: 'target_thread_user_only_no_inherit' },
  sampling_quality: { status: 'degraded', requested_period_us: 10, effective_period_us: 100, observed_median_interval_us: 110, observed_p95_interval_us: 200 },
  groups: [{ id: 'base', status: 'estimated', running_ratio: .25, event_names: ['cycles', 'instructions'] }, { id: 'llc', status: 'unavailable', running_ratio: null, event_names: [], unavailable_events: [{ name: 'llc_read_misses', errno: 95 }] }],
  diagnostics: [{ severity: 'warning', message: 'QA 多组计数器复用，运行覆盖 25%。', suggestion: '查看各组覆盖与内部估计。' }],
} }], files: [], quality: {} } }
const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_EXECUTABLE ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' })
const context = await browser.newContext({ viewport: { width: 1600, height: 1100 }, serviceWorkers: 'block' })
await context.routeWebSocket('**/*', socket => socket.close())
await context.route('**/v1/**', async route => {
  const request = route.request(), url = new URL(request.url()), pathname = url.pathname, method = request.method()
  const reply = (json, status = 200) => route.fulfill({ json, status })
  if (pathname === '/v1/workload-features/catalog') return reply(generated.catalog)
  if (pathname === '/v1/workload-features/profiles') return reply({ profiles })
  const csvProfile = profiles.find(item => pathname === `/v1/workload-features/profiles/${item.id}/export.csv`)
  if (csvProfile) {
    const view = url.searchParams.get('view')
    report.csvRequests.push({ method, profileId: csvProfile.id, view, query: url.search })
    if (method !== 'GET') { report.blockedWrites.push({ method, pathname }); return reply({ error: { message: 'CSV export must use GET' } }, 405) }
    if (failCsvExport) return reply({ error: { code: 'QA_CSV_EXPORT_FAILED', message: 'QA CSV 导出失败，请重试。' } }, 503)
    if (!['stages', 'pmu'].includes(view)) return reply({ error: { message: 'Invalid CSV view' } }, 422)
    return route.fulfill({ status: 200, body: csvBytes(csvProfile, view), headers: { 'Content-Type': 'text/csv; charset=utf-8', 'Content-Disposition': `attachment; filename="qa-export-${csvProfile.id}-${view}.csv"` } })
  }
  const feature = profiles.find(item => pathname === `/v1/workload-features/profiles/${item.id}`)
  if (feature) return reply(feature)
  if (pathname === '/v1/parallel-modeling/datasets') return reply({ datasets })
  if (pathname === '/v1/parallel-modeling/catalog') return reply({ builtins: [], hardware_profiles: [], model_packages: [], objectives: [] })
  if (pathname === '/v1/scenario-capture/catalog') return reply(captureCatalog)
  if (pathname === '/v1/scenario-capture/plans' && method === 'GET') return reply({ plans: [legacyPlan, allPlan] })
  if (pathname === '/v1/scenario-capture/plans' && method === 'POST') { const payload = request.postDataJSON(); report.savedModes.push(payload.capture.pmu.group); return reply({ ...payload, plan_id: 'plan_qa_saved', readiness: { status: 'ready', blockers: [] } }) }
  const plan = [legacyPlan, allPlan].find(item => pathname === `/v1/scenario-capture/plans/${item.plan_id}`)
  if (plan) return reply(plan)
  if (pathname === '/v1/scenario-capture/runs') return reply({ runs: [run] })
  if (pathname === `/v1/scenario-capture/runs/${run.run_id}`) return reply(run)
  if (pathname === '/v1/scenario-capture/devices/discover') return reply({ devices: [], message: 'QA 隔离设备服务' })
  if (pathname === '/v1/agent-cli/adapters') return reply({ adapters: [], default_adapter_id: '' })
  if (/agent|device-bridge|model-relay|scenario-capture|parallel-jobs|parallel-modeling|workload-features/.test(pathname)) {
    if (method !== 'GET') report.blockedWrites.push({ method, pathname })
    return reply({ error: { message: 'QA isolates devices, agents and analysis' } }, 503)
  }
  if (method === 'POST' && ['/v1/optimization-kb/search', '/v1/hardware-kb/search'].includes(pathname)) return route.continue()
  if (method !== 'GET') { report.blockedWrites.push({ method, pathname }); return reply({ error: { message: 'Unmocked mutation blocked' } }, 503) }
  return route.continue()
})

try {
  const page = await context.newPage()
  page.on('pageerror', error => report.pageErrors.push(error.message))
  page.on('download', download => report.downloads.push(download.suggestedFilename()))
  await page.goto(web)
  await page.getByRole('button', { name: '负载特征', exact: true }).click()
  await page.getByRole('heading', { name: '负载与特征版本', exact: true }).waitFor()
  await page.locator('.platform-version.matched').waitFor()
  assert.ok((await page.locator('.platform-version.matched').innerText()).includes(expectedVersion))
  const field = label => page.locator('label.pm-field').filter({ has: page.getByText(label, { exact: true }) }).locator('select')
  const detail = page.locator('[aria-label="阶段详情"]')
  const metricRow = (section, label) => detail.locator(`article[aria-label="${section}特征"] .wf-metric-row`).filter({ hasText: new RegExp('^' + label) })
  const selectProfile = async value => {
    await field('选择负载数据集').selectOption('')
    await field('已保存的特征版本').selectOption(value.id)
    await detail.getByRole('heading', { name: `${value.stages[0].display_id} · ${value.stages[0].name}`, exact: true }).waitFor()
    // Different profiles can have the same stage title, so wait for the selected option too.
    await page.waitForFunction(id => document.querySelector('section.wf-detail') && [...document.querySelectorAll('select')].some(select => select.value === id), value.id)
  }

  await selectProfile(legacy)
  assert.ok((await metricRow('计算', '指令数').innerText()).includes('边界不确定'))
  assert.ok((await metricRow('计算', '指令数').innerText()).includes('范围 0–50,391,782'))
  assert.ok((await metricRow('计算', '指令数').locator('.wf-counter').getAttribute('title')).includes('下界为 0 不表示阶段没有执行工作'))
  const quality = detail.locator('[aria-label="PMU 边界与精度"]')
  assert.ok((await quality.innerText()).includes('阶段耗时 0.176 ms；跨边界读取窗口最大 30.928 ms'))
  assert.ok((await quality.innerText()).includes('阶段内完整采样区间 0 个'))
  assert.ok((await detail.locator('.wf-time-runnable').innerText()).includes('未区分'))
  assert.ok((await metricRow('计算', 'IPC').innerText()).includes('缺少可确定的比率范围'))
  await detail.screenshot({ style: '.global-header { visibility: hidden !important; }', path: path.join(out, 'legacy-boundary-uncertainty.png') })
  report.cases.push('Real legacy 0–50,391,782 interval remains conservative, explains .176 ms versus 30.928 ms, and does not invent IPC or preemption')

  await selectProfile(weightedShort)
  await metricRow('计算', '指令数').getByText('估算', { exact: true }).waitFor()
  assert.ok((await metricRow('计算', '指令数').innerText()).includes('≈ 286,774'))
  assert.equal(await metricRow('计算', '指令数').getByText(/^范围 /).count(), 0)
  assert.ok((await quality.innerText()).includes('30.928 ms'))
  assert.ok((await quality.innerText()).includes('有效窗口覆盖 100.0%'))
  assert.ok((await quality.innerText()).includes('假设窗口内计数均匀分布'))
  await quality.getByText('逐指标覆盖、精度与来源', { exact: true }).click()
  const shortEvidence = quality.getByRole('table', { name: 'PMU 指标证据表' }).locator('tbody tr').filter({ hasText: /^指令数/ })
  assert.ok((await shortEvidence.innerText()).includes('阶段保守范围：0–50,391,782'))
  assert.ok((await shortEvidence.innerText()).includes('完整内部覆盖 0.0%'))
  await quality.getByText('逐指标覆盖、精度与来源', { exact: true }).click()
  await detail.screenshot({ style: '.global-header { visibility: hidden !important; }', path: path.join(out, 'short-stage-weighted-estimate.png') })
  report.cases.push('Short stage shows 286,774 weighted estimate, while full boundary range and uniform-wall assumption remain in details')

  await selectProfile(estimated)
  await metricRow('计算', '指令数').getByText('估算', { exact: true }).waitFor()
  assert.ok((await metricRow('计算', '指令数').innerText()).includes('≈ 16,000'))
  assert.equal(await metricRow('计算', '指令数').getByText(/^范围 /).count(), 0)
  for (const label of ['Cache MPKI', 'LLC MPKI', 'L1D MPKI', 'DTLB MPKI']) assert.ok((await metricRow('缓存与 TLB', label).innerText()).includes('估算'))
  for (const label of ['前端停顿周期', '后端停顿周期']) assert.ok((await metricRow('流水线与访存', label).innerText()).includes('估算'))
  assert.ok((await metricRow('缓存与 TLB', 'LLC MPKI').innerText()).includes('硬件映射待核验'))
  await detail.getByText('查看各级缓存 / TLB 事件计数', { exact: true }).click()
  assert.ok((await metricRow('缓存与 TLB', 'LLC miss').innerText()).includes('估算'))
  await detail.getByText('查看 Load / Store / DRAM 数据', { exact: true }).click()
  assert.ok((await metricRow('流水线与访存', 'Load').innerText()).includes('不可用 / 未采集'))
  await quality.getByText('逐指标覆盖、精度与来源', { exact: true }).click()
  const qualityInstructions = quality.getByRole('table', { name: 'PMU 指标证据表' }).locator('tbody tr').filter({ hasText: /^指令数/ })
  assert.ok((await qualityInstructions.innerText()).includes('100.0% / 25.0%'))
  assert.ok((await qualityInstructions.innerText()).includes('已观测贡献：3,000–5,000'))
  await quality.getByText('逐指标覆盖、精度与来源', { exact: true }).click()
  await detail.getByText('查看各级缓存 / TLB 事件计数', { exact: true }).click()
  await detail.getByText('查看 Load / Store / DRAM 数据', { exact: true }).click()
  await detail.screenshot({ style: '.global-header { visibility: hidden !important; }', path: path.join(out, 'multigroup-weighted-estimates.png') })
  report.cases.push('All groups use weighted numeric estimates and local MPKI denominators; raw contributions and full bounds stay distinct, with mapping and missing data explicit')

  await selectProfile(exact)
  await metricRow('计算', '指令数').getByText('2,000', { exact: true }).waitFor()
  assert.equal(await metricRow('计算', '指令数').getByText('内部估计', { exact: true }).count(), 0)
  assert.ok((await metricRow('计算', 'IPC').innerText()).includes('2.00'))
  assert.ok((await quality.innerText()).includes('使用阶段对齐计数'))
  assert.ok(!(await quality.innerText()).includes('阶段内完整采样区间 0 个'))
  report.cases.push('Exact stage measurements remain numeric and are not relabeled as estimates or zero-sample uncertainty')

  await selectProfile(scheduling)
  const timing = detail.locator('[aria-label="阶段调度时间拆解"]')
  assert.ok((await timing.locator('.wf-time-runnable').innerText()).includes('3.000'))
  assert.ok((await timing.locator('.wf-time-runnable').innerText()).includes('1.000 ms'))
  assert.ok((await timing.locator('.wf-time-runnable').innerText()).includes('其余等待 2.000 ms'))
  assert.ok((await timing.locator('.wf-time-running').innerText()).includes('硬件 IRQ 重叠 0.200 ms'))
  assert.ok((await timing.locator('.wf-time-blocked').innerText()).includes('1.500 ms'))
  const widths = await timing.locator('.wf-state-bar span').evaluateAll(nodes => nodes.map(node => Number.parseFloat(node.style.width)))
  assert.deepEqual(widths, [40, 10, 20, 20, 10])
  await timing.screenshot({ style: '.global-header { visibility: hidden !important; }', path: path.join(out, 'exclusive-scheduler-partition.png') })
  report.cases.push('Running + preempted wait + other runnable + blocked + unknown sum to 100%; Binder and IRQ annotations never add duplicate wall time')

  const exportCsv = async view => {
    const label = view === 'stages' ? '导出阶段 CSV' : '导出 PMU 明细'
    const [download] = await Promise.all([page.waitForEvent('download'), page.getByRole('button', { name: label, exact: true }).click()])
    const filename = `qa-export-${scheduling.id}-${view}.csv`
    assert.equal(download.suggestedFilename(), filename, 'Use the server Content-Disposition filename')
    assert.equal(await download.failure(), null)
    const target = path.join(out, filename)
    await download.saveAs(target)
    const bytes = await fs.readFile(target)
    assert.deepEqual([...bytes.subarray(0, 3)], [0xef, 0xbb, 0xbf], 'Preserve the UTF-8 BOM for Excel')
    assert.deepEqual(bytes, csvBytes(scheduling, view), 'Download server CSV bytes without client filtering or transformation')
    assert.deepEqual(report.csvRequests.at(-1), { method: 'GET', profileId: scheduling.id, view, query: `?view=${view}` })
    await page.getByRole('button', { name: label, exact: true }).waitFor({ state: 'visible' })
    await page.waitForFunction(buttonText => [...document.querySelectorAll('button')].some(button => button.textContent === buttonText && !button.disabled), label)
  }
  await exportCsv('stages')
  await page.locator('section.pm-section').filter({ has: page.getByRole('heading', { name: '阶段总览', exact: true }) }).screenshot({ style: '.global-header { visibility: hidden !important; }', path: path.join(out, 'feature-export-toolbar.png') })
  await page.getByPlaceholder('组件名 / 阶段 ID').fill('QA_NO_STAGE_MATCHES')
  await page.getByText('没有匹配的阶段，请修改搜索或类型筛选。', { exact: true }).waitFor()
  assert.equal(await page.getByRole('table', { name: 'BuildItem 阶段特征表' }).locator('tbody tr').count(), 0)
  await exportCsv('stages')
  await exportCsv('pmu')
  report.cases.push('Stage and PMU CSV exports use the selected profile GET endpoint and server filename, retain UTF-8 BOM byte-for-byte, and export the complete version even when the stage filter hides all rows')

  const [jsonDownload] = await Promise.all([page.waitForEvent('download'), page.getByRole('button', { name: '导出 JSON', exact: true }).click()])
  assert.equal(jsonDownload.suggestedFilename(), `${scheduling.id}.json`)
  const jsonTarget = path.join(out, jsonDownload.suggestedFilename())
  await jsonDownload.saveAs(jsonTarget)
  assert.deepEqual(JSON.parse(await fs.readFile(jsonTarget, 'utf8')), scheduling, 'JSON export retains the complete selected profile')
  report.cases.push('Original JSON export remains available and includes all stages despite the display filter')

  failCsvExport = true
  const downloadsBeforeFailure = report.downloads.length
  await page.getByRole('button', { name: '导出阶段 CSV', exact: true }).click()
  await page.locator('.pm-notice.error').filter({ hasText: 'QA CSV 导出失败，请重试。' }).waitFor()
  await page.waitForFunction(() => [...document.querySelectorAll('button')].some(button => button.textContent === '导出阶段 CSV' && !button.disabled))
  assert.equal(report.downloads.length, downloadsBeforeFailure, 'HTTP errors must not produce a misleading downloaded file')
  assert.deepEqual(report.csvRequests.at(-1), { method: 'GET', profileId: scheduling.id, view: 'stages', query: '?view=stages' })
  failCsvExport = false
  await exportCsv('stages')
  await page.getByPlaceholder('组件名 / 阶段 ID').fill('')
  report.cases.push('Non-200 CSV responses surface the backend error without a download; the export button recovers and a subsequent export succeeds')

  await page.getByRole('button', { name: '场景采集', exact: true }).click()
  await page.getByRole('heading', { name: '按步骤采集真实运行数据', exact: true }).waitFor()
  const mode = page.getByRole('combobox', { name: 'PMU 采集模式', exact: true })
  await mode.waitFor()
  assert.equal(await mode.inputValue(), 'auto')
  assert.ok((await page.locator('.parallel-studio').innerText()).includes('同一次操作采集多组事件'))
  await page.getByRole('button', { name: /QA 历史单组计划/ }).click()
  await page.waitForFunction(() => document.querySelector('select[aria-label="PMU 采集模式"]')?.value === 'cache')
  assert.equal(await mode.inputValue(), 'cache')
  await page.getByRole('button', { name: /QA all 别名计划/ }).click()
  await page.waitForFunction(() => document.querySelector('select[aria-label="PMU 采集模式"]')?.value === 'all')
  assert.equal(await mode.inputValue(), 'all')
  await mode.selectOption('auto')
  await page.getByRole('button', { name: /^保存计划/ }).click()
  await page.getByRole('status').filter({ hasText: '已保存' }).waitFor()
  assert.deepEqual(report.savedModes, ['auto'])
  await page.getByRole('button', { name: /QA 多组采集/ }).click()
  const captureQuality = page.locator('[aria-label="QA 等待 PMU采集质量"]')
  await captureQuality.getByText('查看采样指标、原因与处理建议', { exact: true }).click()
  const groups = captureQuality.getByRole('table', { name: 'QA 等待 PMU 事件组覆盖' })
  assert.ok((await groups.innerText()).includes('复用 · 需估计'))
  assert.ok((await groups.innerText()).includes('25%'))
  assert.ok((await groups.innerText()).includes('llc_read_misses'))
  await captureQuality.screenshot({ style: '.global-header { visibility: hidden !important; }', path: path.join(out, 'capture-auto-group-coverage.png') })
  report.cases.push('New plans default to automatic events; old cache and all alias plans retain modes; saved auto request and per-group coverage are correctly displayed without starting collection')

  assert.deepEqual(report.pageErrors, [])
  assert.deepEqual(report.blockedWrites, [])
  await fs.writeFile(path.join(out, 'report.json'), JSON.stringify(report, null, 2) + '\n')
  console.log(JSON.stringify({ status: 'passed', cases: report.cases.length, output: out }))
} catch (error) {
  report.failure = String(error)
  await fs.writeFile(path.join(out, 'report.json'), JSON.stringify(report, null, 2) + '\n')
  const page = context.pages()[0]
  if (page) await page.screenshot({ path: path.join(out, 'failure.png'), fullPage: true }).catch(() => {})
  throw error
} finally { await context.close(); await browser.close() }
