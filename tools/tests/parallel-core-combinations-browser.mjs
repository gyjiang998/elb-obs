/**
 * Isolated browser checks for per-core-combination search results.
 * PARALLEL_MODELING_QA_WEB=http://127.0.0.1:4032 node tools/tests/parallel-core-combinations-browser.mjs
 * Modeling, device, job and Agent APIs are mocked; no real searches or device actions run.
 */
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const root = fileURLToPath(new URL('../../', import.meta.url))
const web = process.env.PARALLEL_MODELING_QA_WEB ?? 'http://127.0.0.1:4032'
assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(new URL(web).hostname), 'Use a local preview')
const modulePath = process.env.PLAYWRIGHT_MODULE ?? '/Users/j00922218/.npm/_npx/e41f203b7505f1fb/node_modules/playwright/index.mjs'
const { chromium } = await import(pathToFileURL(modulePath).href)
const out = path.join(root, '.local-artifacts/local-preview/parallel-core-combinations-browser')
await fs.mkdir(out, { recursive: true })
const expectedVersion = JSON.parse(await fs.readFile(path.join(root, 'package.json'), 'utf8')).version
const report = { cases: [], pageErrors: [], blockedWrites: [], autoBindingRequests: [], realModelSearches: 0, realDeviceActions: 0 }
const now = '2026-10-22T08:00:00Z'
const hardware = { id: 'qa_hw', name: 'QA 1 大 3 中 4 小', core_groups: [
  { id: 'big', kind: 'big', name: '大核', physical_core_count: 1, max_khz: 2400000, opps_khz: [1200000, 1600000, 2400000] },
  { id: 'middle', kind: 'middle', name: '中核', physical_core_count: 3, max_khz: 2200000, opps_khz: [1000000, 1200000, 2200000] },
  { id: 'small', kind: 'small', name: '小核', physical_core_count: 4, max_khz: 1600000, opps_khz: [800000, 1200000, 1600000] },
] }
const alternateHardware = { ...hardware, id: 'qa_hw_alternate', name: 'QA 新硬件版本' }
const dataset = { id: 'ds_qa_core_combinations', name: 'QA 核组合负载', status: 'ready', hardware_profile_id: hardware.id,
  source_kind: 'capture_manifest', created_at: now, summary: { duration_ms: 20, builditem_count: 1 } }
const historicalDataset = { id: 'ds_qa_historical_archive', name: 'QA 历史归档负载', status: 'ready', hardware_profile_id: hardware.id,
  source_kind: 'builtin_id', created_at: '2026-10-20T08:00:00Z', summary: { duration_ms: 24, builditem_count: 2 }, provenance: { builtin_id: 'qa_materialized_builtin' } }
const importedDataset = { id: 'ds_qa_imported_trace', name: 'QA 文件导入负载', status: 'ready', hardware_profile_id: hardware.id,
  source_kind: 'trace_text', created_at: '2026-10-21T08:00:00Z', summary: { duration_ms: 18, builditem_count: 3 } }
const derivedDataset = { ...importedDataset, id: 'ds_qa_imported_trace_alternate', name: 'QA 文件导入负载 · QA 新硬件版本',
  hardware_profile_id: alternateHardware.id, source_kind: 'hardware_binding', created_at: '2026-10-22T09:00:00Z',
  provenance: { derived_from_dataset_id: importedDataset.id } }
const importedFeature = { id: 'wf_qa_imported', name: 'QA 原硬件负载特征', dataset_id: importedDataset.id, created_at: now }
const structure = { nodes: [{ id: 'phase', label: 'QA BuildItem', duration_ms: 20 }], edges: [] }
const analysis = { id: 'an_qa_core_combinations', dataset_id: dataset.id, status: 'completed', created_at: now,
  baseline: { duration_ms: 20, app_cpu_ms: 24, service_cpu_ms: 3, replay_ms: 20, service_call_count: 1 },
  replay_gate: { passed: true }, graphs: { observed: structure, parallel: structure },
  components: [{ id: 'phase', name: 'QA BuildItem', wall_ms: 20, cpu_ms: 24, binder_blocked_ms: 1, parallelizable: true }], assumptions: [] }
const catalog = { builtins: [
  { id: 'qa_materialized_builtin', name: 'QA 已物化模板不应重复', description: '已由数据集承载', metrics: { duration_ms: 24, builditem_count: 2 } },
  { id: 'qa_pending_builtin', name: 'QA 待载入历史样例', description: '尚未物化的随版本样例', metrics: { duration_ms: 30, builditem_count: 4 } },
], hardware_profiles: [hardware, alternateHardware], model_packages: [
  { id: 'qa_model', name: 'QA 模型', status: 'ready', hardware_profile_id: hardware.id },
  { id: 'qa_model_alternate', name: 'QA 新硬件模型', status: 'ready', hardware_profile_id: alternateHardware.id },
], objectives: [] }

function candidate(index, workers, finish, big, middle, options = {}) {
  return { configuration_index: index, configuration: { main: { kind: 'big' }, workers: Array.from({ length: workers }, () => ({ kind: 'middle' })), main_participates: true, dispatch: 'main_first' },
    metrics: { finish_ms: finish, app_core_ms: 24 + workers, service_core_ms: 3, peak_app_cpu_concurrency: 1 + workers, scenario_count: 18, scenario_worst_ms: finish + .5 },
    frequency_vector: { big, ...(workers ? { middle } : {}) }, feasible: true, meets_budget: true, sensitivity_status: 'passed_budget', ...options }
}
function combination(row, workers, label, options = {}) {
  return { ...row, combination_id: `big1-middle${workers}-small0`, combination_label: label,
    physical_core_counts: { big: 1, middle: workers, small: 0 }, physical_core_count: 1 + workers,
    candidate_count: workers ? 40 : 3, nominal_feasible_count: 2, selection_status: 'objective_optimum',
    recommendation_basis: 'declared_scenarios_objective', fastest_configuration_index: row.configuration_index,
    fastest_finish_ms: row.metrics.finish_ms, global_fastest_finish_ms: 10,
    latency_gap_ms: row.metrics.finish_ms - 10, latency_gap_pct: (row.metrics.finish_ms / 10 - 1) * 100, ...options }
}
function detailed(row, label) { return { ...row, graph: { nodes: [{ id: 'qa-winner', label, duration_ms: row.metrics.finish_ms }], edges: [] }, critical_chain: ['qa-winner'], timeline: [{ node: label, resource: 'big', start_ms: 0, end_ms: row.metrics.finish_ms }] } }
const fastest = candidate(0, 3, 10, 2400000, 2200000)
const near = candidate(900, 1, 10.12, 2200000, 1800000)
const serial = candidate(901, 0, 18, 2400000, 0)
const latency = { id: 'sr_qa_latency', analysis_id: analysis.id, dataset_id: dataset.id, status: 'completed', created_at: now,
  objective: 'minimum_latency', budget_ms: 20, best: detailed(fastest, 'QA 全局最快配置'),
  // Deliberately omit the near-best two-core row from the raw list: the UI must use the backend matrix.
  candidates: [fastest, ...Array.from({ length: 39 }, (_, i) => candidate(i + 1, 3, 10.2 + i, 2400000, 2200000))],
  core_combination_recommendations: [combination(fastest, 3, '1 大 + 3 中', { rank: 1 }), combination(near, 1, '1 大 + 1 中', { rank: 2 }), combination(serial, 0, '1 大', { rank: 3 })], assumptions: [] }
const lowTwo = candidate(80, 1, 11, 1200000, 1000000)
const lowFour = candidate(81, 3, 10.4, 1600000, 1200000)
const impossible = candidate(82, 0, 14, 2400000, 0, { feasible: false, meets_budget: false, sensitivity_status: 'exceeds_budget' })
const frequency = { ...latency, id: 'sr_qa_frequency', objective: 'minimum_frequency_with_budget', budget_ms: 12,
  best: detailed(lowTwo, 'QA 全局低频配置'), candidates: [lowTwo, lowFour, impossible],
  core_combination_recommendations: [combination(lowTwo, 1, '1 大 + 1 中', { rank: 1 }), combination(lowFour, 3, '1 大 + 3 中', { rank: 2 }),
    combination(impossible, 0, '1 大', { rank: 3, selection_status: 'budget_infeasible', recommendation_basis: 'fastest_nominal_fallback', nominal_feasible_count: 0 })] }
const legacy = { ...latency, id: 'sr_qa_legacy', candidates: [fastest, near] }
delete legacy.core_combination_recommendations
const searches = [latency, frequency, legacy]
for (const search of searches) search.assumptions = [`QA_SEARCH_${search.id}`]
latency.hardware_resolution = { source: "verified_archived_sample", note: "QA 已按归档指纹核对硬件，收益仍为条件模型预测。" }

const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_EXECUTABLE ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' })
const context = await browser.newContext({ viewport: { width: 1600, height: 1100 }, serviceWorkers: 'block' })
await context.routeWebSocket('**/*', socket => socket.close())
await context.route('**/v1/**', async route => {
  const request = route.request(), pathname = new URL(request.url()).pathname, method = request.method()
  const reply = (json, status = 200) => route.fulfill({ json, status })
  if (pathname === '/v1/parallel-modeling/catalog') return reply(catalog)
  if (pathname === '/v1/parallel-modeling/datasets') return reply({ datasets: [dataset, historicalDataset, importedDataset] })
  if (pathname === `/v1/parallel-modeling/datasets/${dataset.id}`) return reply(dataset)
  if (pathname === `/v1/parallel-modeling/datasets/${importedDataset.id}`) return reply(importedDataset)
  if (pathname === `/v1/parallel-modeling/datasets/${derivedDataset.id}`) return reply(derivedDataset)
  if (pathname === `/v1/parallel-modeling/datasets/${importedDataset.id}/hardware-binding` && method === 'POST') {
    report.autoBindingRequests.push({ kind: 'binding', body: request.postDataJSON() })
    return reply(derivedDataset)
  }
  if (pathname === '/v1/parallel-modeling/analyses' && method === 'POST') {
    report.autoBindingRequests.push({ kind: 'analysis', body: request.postDataJSON() })
    return reply({ job_id: 'job_qa_auto_hardware_binding', kind: 'parallel_analysis', status: 'queued' })
  }
  if (pathname === '/v1/parallel-modeling/analyses' && method === 'GET') return reply({ analyses: [analysis] })
  if (pathname === `/v1/parallel-modeling/analyses/${analysis.id}`) return reply(analysis)
  if (pathname === '/v1/parallel-jobs/job_qa_auto_hardware_binding') return reply({ job_id: 'job_qa_auto_hardware_binding', kind: 'parallel_analysis', status: 'running', progress: {}, result: null, error: null })
  if (pathname === '/v1/parallel-modeling/searches' && method === 'GET') return reply({ searches })
  const saved = searches.find(item => pathname === `/v1/parallel-modeling/searches/${item.id}`)
  if (saved && method === 'GET') return reply(saved)
  if (pathname === '/v1/workload-features/profiles') return reply({ profiles: [importedFeature] })
  if (pathname === '/v1/scenario-capture/runs') return reply({ runs: [] })
  if (pathname === '/v1/scenario-capture/plans') return reply({ plans: [] })
  if (pathname === '/v1/scenario-capture/devices/discover') return reply({ devices: [], message: 'QA 隔离设备服务' })
  if (pathname === '/v1/agent-cli/adapters') return reply({ adapters: [], default_adapter_id: '' })
  if (/agent|device-bridge|model-relay|scenario-capture|parallel-jobs|parallel-modeling/.test(pathname)) {
    if (method !== 'GET') report.blockedWrites.push({ method, path: pathname })
    return reply({ error: { message: 'QA isolates real modeling, devices and agents' } }, 503)
  }
  if (method === 'POST' && ['/v1/optimization-kb/search', '/v1/hardware-kb/search'].includes(pathname)) return route.continue()
  if (method !== 'GET') { report.blockedWrites.push({ method, path: pathname }); return reply({ error: { message: 'Unmocked mutation blocked' } }, 503) }
  return route.continue()
})

try {
  const page = await context.newPage()
  page.on('pageerror', error => report.pageErrors.push(error.message))
  await page.goto(web)
  await page.getByRole('button', { name: '并行建模', exact: true }).click()
  await page.getByRole('heading', { name: '并行建模', exact: true }).waitFor()
  await page.locator('.platform-version').waitFor()
  assert.ok((await page.locator('.platform-version').innerText()).includes(expectedVersion))
  await page.getByRole('tab', { name: '负载归档', exact: true }).waitFor()
  const archive = page.getByRole('listbox', { name: '已采集与历史归档负载' })
  assert.equal(await archive.getByRole('option').count(), 4)
  for (const name of ['QA 核组合负载', 'QA 历史归档负载', 'QA 文件导入负载', 'QA 待载入历史样例']) assert.equal(await archive.getByText(name, { exact: true }).count(), 1)
  assert.equal(await archive.getByText('QA 已物化模板不应重复', { exact: true }).count(), 0)
  await archive.getByRole('option', { name: /QA 核组合负载/ }).click()
  await page.getByRole('heading', { name: '当前负载', exact: true }).locator('..').getByText('QA 核组合负载', { exact: true }).waitFor()
  report.cases.push('Unified workload archive lists captured, imported and historical datasets, retains pending built-ins and de-duplicates materialized built-ins')
  await page.locator('label.pm-field').filter({ has: page.getByText('已保存的分析', { exact: true }) }).locator('select').selectOption(analysis.id)
  const frequencyPolicy = page.locator('label.pm-field').filter({ has: page.getByText('频点搜索范围', { exact: true }) }).locator('select')
  assert.equal(await frequencyPolicy.inputValue(), 'max_opps')
  await page.getByRole('radio', { name: /预算内最低频点/ }).check()
  assert.equal(await frequencyPolicy.inputValue(), 'all_opps')
  await page.getByRole('radio', { name: /最短 UI 回调耗时/ }).check()
  assert.equal(await frequencyPolicy.inputValue(), 'max_opps')
  report.cases.push('Latency search defaults to maximum OPPs while the frequency-budget objective restores the full OPP space')
  const results = page.locator('section.pm-section').filter({ has: page.getByRole('heading', { name: '4. 候选排名与预计收益', exact: true }) })
  const details = results.locator('[aria-label="当前配置详情"]')
  const table = results.getByRole('table', { name: '核心组合推荐表' })
  const savedSearch = page.getByLabel('已保存的搜索', { exact: true })
  await savedSearch.waitFor()
  const savedSearchLayout = await savedSearch.evaluate((node) => {
    const bounds = node.getBoundingClientRect()
    return { width: bounds.width, parentWidth: node.parentElement?.parentElement?.getBoundingClientRect().width ?? 0, scrollWidth: node.scrollWidth }
  })
  assert.ok(savedSearchLayout.width <= 622 && savedSearchLayout.width <= savedSearchLayout.parentWidth && savedSearchLayout.scrollWidth <= Math.ceil(savedSearchLayout.width), `Saved-search selector should remain compact without horizontal overflow: ${JSON.stringify(savedSearchLayout)}`)
  const savedSearchLabels = await savedSearch.locator('option').allTextContents()
  assert.ok(savedSearchLabels.slice(1).every(label => !label.includes('sr_qa_')), `Saved-search labels should not expose long storage IDs: ${JSON.stringify(savedSearchLabels)}`)
  assert.ok(savedSearchLabels.some(label => label.includes('最低时延') && label.includes('10.000 ms')), `Saved-search labels should identify the objective and modeled latency: ${JSON.stringify(savedSearchLabels)}`)
  const selectSearch = async id => { await savedSearch.selectOption(id); await results.getByText(`QA_SEARCH_${id}`, { exact: true }).waitFor() }

  await selectSearch(latency.id)
  assert.equal(await results.getByLabel("寻优硬件匹配依据").innerText(), latency.hardware_resolution.note)
  await table.waitFor()
  assert.equal(await table.locator('tbody tr').count(), 3)
  const latencyRows = await table.locator('tbody tr').allTextContents()
  assert.ok(latencyRows[0].includes('1 大 + 3 中') && latencyRows[0].includes('10.000'))
  assert.ok(latencyRows[1].includes('1 大 + 1 中') && latencyRows[1].includes('+0.120 ms') && latencyRows[1].includes('+1.20%'))
  assert.ok(latencyRows[2].includes('18.000'))
  assert.equal(await results.locator('details').filter({ has: page.getByText('全部配置明细 · 40 个配置', { exact: true }) }).getAttribute('open'), null)
  await table.getByRole('radio', { name: '查看核心组合 1 大 + 1 中', exact: true }).check()
  await details.getByRole('heading', { name: '当前组合 · 1 大 + 1 中', exact: true }).waitFor()
  assert.ok((await details.innerText()).includes('10.120'))
  assert.ok((await details.innerText()).includes('2,200 MHz'))
  assert.ok(await page.getByRole('tab', { name: '当前配置预测 DAG', exact: true }).isDisabled())
  assert.equal(await page.getByText('当前配置的模拟执行时序', { exact: true }).count(), 0)
  await results.screenshot({ path: path.join(out, 'minimum-latency-two-core-comparison.png') })
  report.cases.push('Backend combination matrix preserves the near-fastest two-core row absent from 40 raw candidates; correct latency order, core count, MHz and deltas; selection does not borrow winner timing')

  await selectSearch(frequency.id)
  assert.equal(await results.getByLabel("寻优硬件匹配依据").count(), 0, "Old searches do not borrow another result hardware evidence")
  await table.getByRole('row').filter({ hasText: '1,200 MHz' }).first().waitFor()
  const frequencyRows = await table.locator('tbody tr').allTextContents()
  assert.ok(frequencyRows[0].includes('1 大 + 1 中') && frequencyRows[0].includes('11.000'))
  assert.ok(frequencyRows[1].includes('1 大 + 3 中') && frequencyRows[1].includes('10.400'))
  assert.ok(frequencyRows[0].includes('1,200 MHz') && frequencyRows[0].includes('1,000 MHz'))
  assert.ok(frequencyRows[1].includes('1,600 MHz') && frequencyRows[1].includes('1,200 MHz'))
  assert.ok(frequencyRows[2].includes('该组合无法达标'))
  assert.ok((await results.locator('[aria-label="核心组合推荐"]').innerText()).includes('最快名义仿真时延：10.000 ms'))
  await table.getByRole('radio', { name: '查看核心组合 1 大', exact: true }).check()
  await details.getByRole('heading', { name: '当前组合 · 1 大', exact: true }).waitFor()
  assert.ok((await details.innerText()).includes('无法达标 · 最快诊断配置'))
  assert.ok((await details.innerText()).includes('14.000'))
  assert.ok(await page.getByRole('tab', { name: '当前配置预测 DAG', exact: true }).isDisabled())
  await results.screenshot({ path: path.join(out, 'minimum-frequency-infeasible-diagnostic.png') })
  report.cases.push('Frequency goal preserves backend frequency order rather than resorting by latency; each group shows its own feasible MHz; infeasible group remains selectable as a diagnostic')

  await selectSearch(legacy.id)
  await table.waitFor({ state: 'detached' })
  assert.ok((await results.innerText()).includes('此历史搜索未保存按核心组合的推荐'))
  const rawTable = results.getByRole('table', { name: '全部配置明细表' })
  await rawTable.waitFor()
  assert.equal(await rawTable.locator('tbody tr').count(), 2)
  assert.ok(await page.getByRole('tab', { name: '当前配置预测 DAG', exact: true }).isEnabled())
  await rawTable.getByRole('radio').nth(1).check()
  assert.ok((await details.innerText()).includes('10.120'))
  assert.ok(await page.getByRole('tab', { name: '当前配置预测 DAG', exact: true }).isDisabled())
  await results.screenshot({ path: path.join(out, 'legacy-configurations-fallback.png') })
  report.cases.push('Historical search without new fields opens the original configuration list; raw row selection updates metrics and avoids unrelated prediction DAG')

  const hardwareSelect = page.locator('label.pm-field').filter({ has: page.getByText('分析使用的硬件配置', { exact: true }) }).locator('select')
  await hardwareSelect.selectOption(alternateHardware.id)
  assert.equal(await page.getByRole('button', { name: '搜索并行配置', exact: true }).count(), 0,
    'Changing hardware must invalidate the old analysis before another search can start')
  const savedAnalysisSelect = page.locator('label.pm-field').filter({ has: page.getByText('已保存的分析', { exact: true }) }).locator('select')
  assert.equal(await savedAnalysisSelect.isDisabled(), true,
    'An old-hardware saved analysis must stay unavailable until the new hardware revision is analyzed')
  report.cases.push('Changing analysis hardware immediately fences stale analysis and search state')

  await archive.getByRole('option', { name: /QA 文件导入负载/ }).click()
  const currentWorkload = page.locator('.pm-box').filter({ has: page.getByRole('heading', { name: '当前负载', exact: true }) })
  await currentWorkload.getByText('QA 文件导入负载', { exact: true }).waitFor()
  const featureSelect = page.getByRole('combobox', { name: '本数据集的负载特征' })
  await featureSelect.selectOption(importedFeature.id)
  await hardwareSelect.selectOption(alternateHardware.id)
  assert.equal(await page.getByRole('button', { name: /生成新硬件版本|绑定并生成新版本/ }).count(), 0)
  await page.getByRole('button', { name: '拆解账目与回放基线', exact: true }).click()
  await currentWorkload.getByText(derivedDataset.name, { exact: true }).waitFor()
  assert.deepEqual(report.autoBindingRequests.map(item => item.kind), ['binding', 'analysis'])
  assert.deepEqual(report.autoBindingRequests[0].body, { hardware_profile_id: alternateHardware.id })
  assert.equal(report.autoBindingRequests[1].body.dataset_id, derivedDataset.id)
  assert.equal(report.autoBindingRequests[1].body.model_package_id, 'qa_model_alternate')
  assert.equal(report.autoBindingRequests[1].body.feature_profile_id, '', 'A feature profile bound to the source dataset must not leak into the derived dataset analysis')
  assert.equal(await archive.getByRole('option', { name: new RegExp(derivedDataset.name) }).getAttribute('aria-selected'), 'true')
  report.cases.push('Changing hardware is one action: analysis automatically creates and selects the immutable dataset revision, chooses its model and drops source-only feature state')

  assert.deepEqual(report.pageErrors, [])
  await fs.writeFile(path.join(out, 'report.json'), JSON.stringify(report, null, 2) + '\n')
  console.log(JSON.stringify({ status: 'passed', cases: report.cases.length, output: out }))
} finally { await context.close(); await browser.close() }
