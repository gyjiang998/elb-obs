/**
 * Scenario capture history/restoration checks with isolated device and job APIs.
 * SCENARIO_HISTORY_QA_WEB=http://127.0.0.1:4032 PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs node tools/tests/scenario-capture-history-browser.mjs
 * Only platform metadata and the two read-only KB searches may reach the server.
 */
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const root = fileURLToPath(new URL('../../', import.meta.url))
const expectedVersion = JSON.parse(await fs.readFile(path.join(root, 'package.json'), 'utf8')).version
const web = process.env.SCENARIO_HISTORY_QA_WEB
assert.ok(web, 'Set SCENARIO_HISTORY_QA_WEB explicitly')
const origin = new URL(web).origin
assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(new URL(web).hostname), 'QA must use a local preview')
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ? pathToFileURL(process.env.PLAYWRIGHT_MODULE).href : 'playwright')
const out = path.join(root, '.local-artifacts/local-preview/scenario-history-browser')
await fs.mkdir(out, { recursive: true })
const key = 'heterogeneous-scenario-capture-active-job-v1'
const ids = { old: `parallel_job_${'1'.repeat(24)}`, active: `parallel_job_${'2'.repeat(24)}`, current: `parallel_job_${'3'.repeat(24)}`, another: `parallel_job_${'4'.repeat(24)}` }
const oldReason = 'QA 历史失败：旧的锁屏错误'
const currentReason = 'QA 当前任务失败：本次设备状态校验'
const planId = `plan_${'a'.repeat(24)}`
const now = '2026-10-22T08:00:00Z'
const steps = [{ step_id: 'step_01', ordinal: 1, title: 'QA 等待步骤', slug: 'wait', operation: 'wait', seconds: 0, expected_targets: [] }]
const capture = { selected_step_ids: ['step_01'], collectors: ['trace'], preparation_mode: 'auto', pmu: { binary_path: '/data/local/tmp/thread_pmu_sampler', period_us: 100, duration_ms: 1000, group: 'cache', reader_cpu: 0 } }
const profile = { bundle: 'qa.fixture.only', ability: 'MainAbility', screen_size: [1280, 2832], targets: {}, trace_categories: ['sched'] }
const catalog = { templates: [{ template_id: 'qa_fixture', name: 'QA 采集场景', steps }], default_template_id: 'qa_fixture', default_profile: profile, default_capture: capture, operations: ['wait'], pmu_sampler: { period_us_range: [10, 10000] } }
const device = { device_serial: 'QA_FIXTURE_NO_REAL_DEVICE', connection_mode: 'local', bridge_id: '', display_name: 'QA 隔离设备', connection_label: '隔离测试连接' }
function savedPlan(id, title) {
  return { plan_id: id, title, template_id: 'qa_fixture', natural_language_steps: '', steps, profile, capture,
    created_at: now, readiness: { status: 'ready', blockers: [] } }
}
function failedRun(id, title, reason) {
  return { run_id: `capture_${id.repeat(24)}`, plan_id: planId, title, status: 'failed', created_at: now,
    device_serial: device.device_serial, connection_mode: 'local', manifest: {
      steps: [{ ...steps[0], status: 'failed', captured: true, collector_status: {}, screen_preparation_screenshot: { status: 'retained' } }],
      quality: { errors: [{ code: 'QA_FIXTURE_FAILURE', step_id: 'step_01', reason, suggestion: '隔离测试，无需操作设备。' }] },
      files: [{ kind: 'screen_preparation_screenshot', step_id: 'step_01', relative_path: 'fixture.png' }],
    } }
}
const oldRun = failedRun('5', 'QA 旧失败采集', oldReason)
const currentRun = failedRun('6', 'QA 当前采集', currentReason)
Object.assign(currentRun.manifest.steps[0], {
  initial_display_wakeup: { strategy_version: 'power_key_stateful/v2', power_awake: true },
  screen_preparation_timing: { status: 'failed', elapsed_seconds: 12.3, message: '屏幕准备失败，保留操作诊断；最后操作：回收界面布局' },
  screen_evidence_timing: { status: 'completed', elapsed_seconds: 2 },
  screen_state_diagnostics: { lock_service: { screen_locked_by_user: { '100': true } } },
})
function job(id, status, run = currentRun) {
  const terminal = ['succeeded', 'partial', 'failed', 'cancelled'].includes(status)
  return { schema_version: 'parallel_job/v1', job_id: id, kind: 'scenario_capture', status,
    run_id: run.run_id, created_at: now, updated_at: now, progress: { phase: 'qa_fixture', message: 'QA 隔离任务进度', percent: 20 },
    result: terminal ? run : null, error: status === 'failed' ? { code: 'QA_FIXTURE_FAILURE', message: run === oldRun ? oldReason : currentReason } : null,
    inputs: { plan_id: planId, connection_mode: 'local', device_serial: device.device_serial, bridge_id: '' } }
}
const delay = ms => new Promise(resolve => setTimeout(resolve, ms))
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
async function until(check, message, timeout = 20000) {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) { if (await check()) return; await delay(50) }
  throw Error(`Timed out: ${message}`)
}
const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_EXECUTABLE ? { executablePath: process.env.CHROME_EXECUTABLE } : {}) })
const report = { cases: [], pageErrors: [], blockedWrites: [], realAgentRequests: 0, realDeviceActions: 0 }

async function fixture({ stored = '', readJob, bundleStatus, plans = [] } = {}) {
  const fixtureCatalog = bundleStatus ? { ...catalog, default_capture: { ...capture, collectors: ['trace', 'pmu'] }, pmu_sampler: { ...catalog.pmu_sampler, platform_bundle: { status: bundleStatus, message: bundleStatus === 'ready' ? 'QA 已校验，可自动安装。' : 'QA 平台缺少采样器资产。', remediation: bundleStatus === 'ready' ? {} : { next_action: 'QA 导入完整构建包后重试。', build_command: 'python -m tools.scenario_capture.build_pmu_sampler --native-sdk /QA/sdk --output /QA/bundle' } } } } : catalog
  const context = await browser.newContext({ viewport: { width: 1500, height: 1100 }, serviceWorkers: 'block',
    // Automatic repair has its own isolated Agent integration suite.
    storageState: { cookies: [], origins: [{ origin, localStorage: [{ name: 'heterogeneous-scenario-capture-auto-repair-v1', value: 'false' }, ...(stored ? [{ name: key, value: stored }] : [])] }] } })
  // No application WebSocket, including Agent terminals, may reach a real service.
  await context.routeWebSocket('**/*', socket => socket.close())
  const state = { runs: [oldRun], plans: [...plans], savedPlans: [], jobReads: [], executeCount: 0, newStatus: 'failed', requests: [] }
  await context.route('**/v1/**', async route => {
    const request = route.request(), pathname = new URL(request.url()).pathname, method = request.method()
    state.requests.push({ method, path: pathname })
    const reply = (json, status = 200) => route.fulfill({ json, status }).catch(() => {})
    if (pathname === '/v1/scenario-capture/catalog') return reply(fixtureCatalog)
    if (pathname === '/v1/scenario-capture/plans' && method === 'GET') return reply({ plans: state.plans })
    if (pathname === '/v1/scenario-capture/plans' && method === 'POST') { const payload = request.postDataJSON(); state.savedPlans.push(payload); const plan = { ...payload, plan_id: planId, created_at: now, readiness: { status: 'ready', blockers: [] } }; state.plans = [plan, ...state.plans.filter(item => item.plan_id !== plan.plan_id)]; return reply(plan) }
    if (pathname.startsWith('/v1/scenario-capture/plans/') && method === 'GET') {
      const id = decodeURIComponent(pathname.split('/').at(-1))
      const plan = state.plans.find(item => item.plan_id === id)
      return plan ? reply(plan) : reply({ error: { message: 'QA plan not found' } }, 404)
    }
    if (pathname === '/v1/scenario-capture/runs') return reply({ runs: state.runs })
    if (pathname === '/v1/scenario-capture/devices/discover') return reply({ devices: [device], selected_device: device, message: '隔离设备 fixture' })
    if (pathname === `/v1/scenario-capture/plans/${planId}/execute` && method === 'POST') {
      state.executeCount++
      state.runs = [currentRun, oldRun]
      return reply(job(ids.current, 'queued'))
    }
    if (pathname.startsWith('/v1/parallel-jobs/')) {
      const id = pathname.split('/')[3]
      if (method !== 'GET') return reply({ error: { message: 'QA does not cancel a real job' } }, 503)
      state.jobReads.push(id)
      if (id === ids.current) return reply(job(id, state.newStatus))
      const value = readJob ? await readJob(id, state) : { value: job(id, 'failed', oldRun) }
      return reply(value.value, value.status ?? 200)
    }
    if (pathname === '/v1/parallel-modeling/catalog') return reply({ builtins: [], hardware_profiles: [{ id: 'qa_hardware', name: 'QA 参考配置' }], model_packages: [], objectives: [] })
    if (pathname === '/v1/agent-cli/adapters') return reply({ schema_version: 'agent_cli_adapter_catalog/v1', default_adapter_id: '', adapters: [], manifest_error: null })
    if (/agent|device-bridge|model-relay/.test(pathname)) {
      if (method !== 'GET') report.blockedWrites.push({ method, path: pathname })
      return reply({ error: { message: 'QA isolates Agent and remote device services' } }, 503)
    }
    if (method === 'POST' && ['/v1/optimization-kb/search', '/v1/hardware-kb/search'].includes(pathname)) return route.continue()
    if (method !== 'GET') {
      report.blockedWrites.push({ method, path: pathname })
      return reply({ error: { message: 'Unmocked mutation blocked by QA' } }, 503)
    }
    // Capture/job routes should never escape to real state, even on fixture mistakes.
    if (/scenario-capture|parallel-jobs/.test(pathname)) return reply({ error: { message: 'Unknown fixture route' } }, 404)
    return route.continue()
  })
  const open = async () => {
    const page = await context.newPage()
    page.on('pageerror', error => report.pageErrors.push(error.message))
    await page.goto(web)
    await page.getByRole('button', { name: '负载采样', exact: true }).click()
    await page.getByRole('heading', { name: '负载采样', exact: true }).waitFor()
    await page.locator('.platform-version').waitFor()
    assert.ok((await page.locator('.platform-version').innerText()).includes(expectedVersion))
    assert.ok((await page.locator('.pm-page-title').getAttribute('title')).includes('按步骤准备目标操作'))
    return page
  }
  const page = await open()
  return { context, page, state, open }
}
const studio = page => page.locator('.parallel-studio')
const results = page => studio(page).locator('section.pm-section').filter({ has: page.getByRole('heading', { name: '采样结果', exact: true }) })
const storedValue = page => page.evaluate(storageKey => localStorage.getItem(storageKey), key)
async function unselected(page) {
  assert.equal(await results(page).locator('.pm-history button.selected').count(), 0)
  assert.equal(await studio(page).locator('.pm-note.error').count(), 0)
  assert.equal(await studio(page).locator('.pm-notice.error').count(), 0)
  assert.equal(await studio(page).locator('.pm-progress').count(), 0)
}

try {
  const newestPlan = savedPlan(`plan_${'b'.repeat(24)}`, 'QA 最新计划')
  const olderPlan = savedPlan(`plan_${'c'.repeat(24)}`, 'QA 已明确选择计划')
  const selection = await fixture({ plans: [newestPlan, olderPlan] })
  try {
    const chooser = studio(selection.page).getByLabel('已保存计划')
    await until(async () => await chooser.inputValue() === newestPlan.plan_id, 'newest saved plan is selected on first open')
    assert.equal(await studio(selection.page).getByLabel('计划名称').inputValue(), newestPlan.title)
    await chooser.selectOption(olderPlan.plan_id)
    await until(async () => await studio(selection.page).getByLabel('计划名称').inputValue() === olderPlan.title, 'explicit saved-plan selection loads')
    selection.state.plans = [savedPlan(`plan_${'d'.repeat(24)}`, 'QA 外部新增计划'), newestPlan, olderPlan]
    await studio(selection.page).locator('.pm-heading').getByRole('button', { name: '刷新', exact: true }).click()
    await until(async () => !(await studio(selection.page).locator('.pm-heading button').isDisabled()), 'plan refresh completes')
    assert.equal(await chooser.inputValue(), olderPlan.plan_id)
    assert.equal(await studio(selection.page).getByLabel('计划名称').inputValue(), olderPlan.title)
    report.cases.push('first open selects the newest saved plan; later refresh preserves an explicit plan selection')
  } finally { await selection.context.close() }

  const stale = await fixture({ stored: ids.old })
  try {
    await until(async () => await storedValue(stale.page) === null, 'stale terminal pointer is cleared')
    await unselected(stale.page)
    await stale.page.screenshot({ path: path.join(out, 'fresh-window-no-old-error.png') })
    await stale.page.getByRole('button', { name: /QA 旧失败采集/ }).click()
    await until(async () => (await results(stale.page).innerText()).includes(oldReason), 'explicit history displays old error')
    assert.equal(await results(stale.page).locator('.pm-history button.selected').count(), 1)
    assert.equal(await results(stale.page).getByRole('link', { name: '查看失败现场', exact: true }).count(), 1)
    await studio(stale.page).locator('.pm-heading').getByRole('button', { name: '刷新', exact: true }).click()
    await until(async () => !(await studio(stale.page).locator('.pm-heading button').isDisabled()), 'history refresh completes')
    assert.ok((await results(stale.page).innerText()).includes(oldReason))
    const second = await stale.open()
    await unselected(second)
    await stale.page.screenshot({ path: path.join(out, 'explicit-history-failure.png') })
    report.cases.push('stale terminal storage is suppressed; manual history and screenshots remain; a new window is unselected')
  } finally { await stale.context.close() }

  let completed = false
  const active = await fixture({ stored: ids.active, readJob: async () => ({ value: job(ids.active, completed ? 'failed' : 'running') }) })
  try {
    await until(async () => (await studio(active.page).locator('.pm-progress').innerText().catch(() => '')).includes('执行中'), 'running job restores')
    // Simulate another browser window replacing the shared recovery pointer.
    await active.page.evaluate(({ storageKey, value }) => localStorage.setItem(storageKey, value), { storageKey: key, value: ids.another })
    completed = true
    active.state.runs = [currentRun, oldRun]
    await until(async () => (await studio(active.page).innerText()).includes(currentReason), 'recovered active completion remains visible')
    await until(async () => await results(active.page).locator('.pm-history button.selected').count() === 1, 'current completion selects result')
    assert.equal(await storedValue(active.page), ids.another, 'Old completion must not delete another window recovery pointer')
    await results(active.page).getByText('查看设备操作诊断', { exact: true }).click()
    const evidence = results(active.page).locator('[aria-label="失败现场 · QA 等待步骤"]')
    assert.ok((await evidence.innerText()).includes('已用 12.3 秒'))
    assert.ok((await evidence.locator('pre').innerText()).includes('screen_locked_by_user'))
    assert.ok((await evidence.locator('pre').innerText()).includes('power_key_stateful/v2'))
    await active.page.screenshot({ path: path.join(out, 'restored-current-completion.png') })
    report.cases.push('a running recovery completes normally; cleanup preserves a newer shared recovery pointer')
  } finally { await active.context.close() }

  const gate = deferred()
  const race = await fixture({ stored: ids.old, readJob: async () => { await gate.promise; return { value: job(ids.old, 'running', oldRun) } } })
  try {
    await until(async () => race.state.jobReads.includes(ids.old), 'old recovery lookup starts')
    const execute = studio(race.page).getByRole('button', { name: /^(保存并执行采样|在设备执行采样)$/ })
    await until(async () => (await studio(race.page).innerText()).includes('QA 隔离设备'), 'fixture device discovery completes')
    assert.ok(await execute.isEnabled(), 'A pending historical lookup must not overwrite or prevent this window submitting its explicit capture')
    await execute.click()
    await until(async () => race.state.executeCount === 1, 'current capture submits')
    gate.resolve()
    await until(async () => (await studio(race.page).innerText()).includes(currentReason), 'current terminal result is shown')
    await until(async () => await storedValue(race.page) === null, 'current terminal pointer is cleared')
    const selectedText = await results(race.page).locator('.pm-history button.selected').innerText()
    assert.ok(selectedText.includes('QA 当前采集'))
    assert.ok(!(await studio(race.page).locator('.pm-progress').innerText()).includes(oldReason))
    assert.equal(race.state.executeCount, 1)
    await race.page.screenshot({ path: path.join(out, 'late-recovery-keeps-current-task.png') })
    report.cases.push('new capture completes immediately; late historical recovery cannot replace it; terminal pointer is cleared')
  } finally { gate.resolve(); await race.context.close() }

  const missing = await fixture({ stored: ids.old, readJob: async () => ({ status: 404, value: { error: { code: 'PARALLEL_JOB_NOT_FOUND', message: 'QA historical job removed' } } }) })
  try {
    await until(async () => await storedValue(missing.page) === null, 'missing historical pointer is cleared')
    await unselected(missing.page)
    report.cases.push('deleted historical job does not block capture or resurrect an error')
  } finally { await missing.context.close() }

  for (const bundleStatus of ['ready', 'missing']) {
    const pmu = await fixture({ bundleStatus })
    try {
      await studio(pmu.page).locator('details.capture-pmu-advanced > summary').click()
      const asset = studio(pmu.page).locator('[aria-label="平台 PMU 采样器状态"]')
      const assetText = await asset.innerText()
      assert.ok(assetText.includes(bundleStatus === 'ready' ? '平台采样器已就绪' : '平台采样器待配置'), assetText)
      if (bundleStatus === 'ready') {
        const layout = await pmu.page.evaluate(() => {
          const rect = selector => { const value = document.querySelector(selector)?.getBoundingClientRect(); return value ? { x: value.x, y: value.y, width: value.width, height: value.height } : null }
          return { mode: rect('.capture-mode-row'), pmu: rect('.capture-pmu-panel'), save: rect('.capture-save-plan'), plans: rect('.capture-saved-plan-select'), execute: rect('.capture-execute-primary') }
        })
        assert.ok(layout.mode.x < layout.pmu.x && Math.abs(layout.mode.y + layout.mode.height - layout.pmu.y - layout.pmu.height) < 2, `Capture modes and PMU parameters should share one aligned row: ${JSON.stringify(layout)}`)
        const savedPlanToSaveRatio = layout.plans.width / layout.save.width
        assert.ok(savedPlanToSaveRatio > 2.8 && savedPlanToSaveRatio < 3.2 && Math.abs(layout.save.y - layout.execute.y) < 2, `Saved-plan selection should receive the space removed from the half-width Save action: ${JSON.stringify({ layout, savedPlanToSaveRatio })}`)
      }
      if (bundleStatus === 'missing') {
        await asset.getByText('查看部署方法', { exact: true }).click()
        assert.ok((await asset.innerText()).includes('build_pmu_sampler'))
      }
      const run = { ...oldRun, run_id: 'capture_' + '8'.repeat(24), title: 'QA PMU 准备结果', status: 'partial', manifest: {
        steps: [], quality: { warnings: [] }, pmu_preparation: {
          status: bundleStatus === 'ready' ? 'installed' : 'bundle_missing',
          message: bundleStatus === 'ready' ? 'QA 采样器自动安装并核验完成。' : 'QA 缺少采样器资产，Trace 和 HiLog 继续采集。',
          remediation: bundleStatus === 'ready' ? {} : { next_action: 'QA 配置完整构建包。' },
        },
      } }
      pmu.state.runs = [run, oldRun]
      await studio(pmu.page).locator('.pm-heading').getByRole('button', { name: '刷新', exact: true }).click()
      await pmu.page.getByRole('button', { name: /QA PMU 准备结果/ }).click()
      const preparation = results(pmu.page).locator('[aria-label="PMU 采样器准备"]')
      assert.ok((await preparation.innerText()).includes(bundleStatus === 'ready' ? '已自动安装' : '准备未完成'))
      if (bundleStatus === 'missing') assert.ok((await preparation.innerText()).includes('处理建议：QA 配置完整构建包。'))
      await pmu.page.screenshot({ path: path.join(out, `pmu-preparation-${bundleStatus}.png`), fullPage: true })
      report.cases.push(`PMU ${bundleStatus}: asset status and preparation result are visible without any device actions`)
    } finally { await pmu.context.close() }
  }

  const generic = await fixture({ bundleStatus: 'ready' })
  try {
    const page = generic.page
    await page.getByLabel('负载模板').selectOption('custom')
    assert.equal(await page.getByLabel('目标应用包名').inputValue(), '')
    assert.equal(await page.getByLabel('启动 Ability').inputValue(), '')
    await page.getByLabel('操作步骤 · 每行一步').fill('打开目标应用\n执行目标操作')
    await page.getByRole('button', { name: '全选采集', exact: true }).click()
    await page.getByRole('button', { name: /^保存计划/ }).click()
    await until(async () => (await studio(page).innerText()).includes('请填写目标应用包名和启动 Ability'), 'empty custom application is not silently defaulted')
    assert.equal(generic.state.savedPlans.length, 0)
    await page.getByLabel('目标应用包名').fill('com.example.notes')
    await page.getByLabel('启动 Ability').fill('EntryAbility')
    await page.getByRole('button', { name: /^保存计划/ }).click()
    await until(async () => generic.state.savedPlans.length === 1, 'generic application configuration is saved')
    await until(async () => await page.getByLabel('已保存计划').inputValue() === planId, 'newly saved plan is selected immediately')
    const payload = generic.state.savedPlans[0]
    assert.equal(payload.profile.bundle, 'com.example.notes')
    assert.equal(payload.profile.ability, 'EntryAbility')
    assert.deepEqual(payload.profile.targets, {})
    assert.equal(payload.capture.pmu.binary_path, '/data/local/tmp/thread_pmu_sampler')
    await page.getByLabel('负载模板').selectOption('qa_fixture')
    assert.equal(await page.getByLabel('目标应用包名').inputValue(), profile.bundle)
    await page.screenshot({ path: path.join(out, 'generic-application-configuration.png') })
    report.cases.push('custom scenarios clear application defaults; explicit package and Ability persist with the shared sampler')
  } finally { await generic.context.close() }

  const screen = await fixture()
  try {
    screen.state.runs = [{ ...oldRun, run_id: 'capture_' + '9'.repeat(24), title: 'QA 屏幕保护诊断', status: 'partial', manifest: {
      steps: [], quality: { warnings: [] }, screen_awake_guard: {
        protection_degraded: true, restoration_needed: true, restore_status: 'unverified',
      },
    } }, oldRun]
    await studio(screen.page).locator('.pm-heading').getByRole('button', { name: '刷新', exact: true }).click()
    await screen.page.getByRole('button', { name: /QA 屏幕保护诊断/ }).click()
    const output = await results(screen.page).innerText()
    assert.ok(output.includes('设备未确认临时延长熄屏时间'))
    assert.ok(output.includes('尚未确认恢复原熄屏时间'))
    assert.ok((await studio(screen.page).innerText()).includes('采样前再次核验，临时延长熄屏时间并在结束后恢复'))
    await screen.page.screenshot({ path: path.join(out, 'screen-timeout-diagnostics.png'), fullPage: true })
    report.cases.push('screen timeout protection and restoration uncertainty are visible without device actions')
  } finally { await screen.context.close() }

  assert.deepEqual(report.pageErrors, [])
  report.pass = true
  await fs.writeFile(path.join(out, 'report.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify(report))
} catch (error) {
  report.pass = false
  report.error = String(error)
  await fs.writeFile(path.join(out, 'report.json'), JSON.stringify(report, null, 2))
  throw error
} finally {
  await browser.close()
}
