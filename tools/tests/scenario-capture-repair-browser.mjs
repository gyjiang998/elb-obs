/** Isolated browser regression: real UI -> current Agent /commands; no phone or external Agent. */
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const root = fileURLToPath(new URL('../../', import.meta.url))
const web = process.env.SCENARIO_REPAIR_QA_WEB
assert.ok(web, 'Set SCENARIO_REPAIR_QA_WEB to a local preview')
assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(new URL(web).hostname))
const origin = new URL(web).origin
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ? pathToFileURL(process.env.PLAYWRIGHT_MODULE).href : 'playwright')
const out = path.join(root, '.local-artifacts/local-preview/scenario-repair-browser')
await fs.mkdir(out, { recursive: true })
const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_EXECUTABLE ? { executablePath: process.env.CHROME_EXECUTABLE } : {}) })
const report = { pass: false, cases: [], pageErrors: [], blockedWrites: [], realAgentRequests: 0, realDeviceActions: 0 }
const delay = ms => new Promise(resolve => setTimeout(resolve, ms))
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
async function until(check, message, timeout = 20000) {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) { if (await check()) return; await delay(50) }
  throw Error('Timed out: ' + message)
}
const prefKey = 'heterogeneous-scenario-capture-auto-repair-v1'
const activeKey = 'heterogeneous-scenario-capture-active-job-v1'
const ids = { plan: `plan_${'a'.repeat(24)}`, job: `parallel_job_${'1'.repeat(24)}`, retryJob: `parallel_job_${'2'.repeat(24)}`, run: `capture_${'3'.repeat(24)}`, retryRun: `capture_${'4'.repeat(24)}`, oldRun: `capture_${'5'.repeat(24)}`, ticket: `capture_repair_${'6'.repeat(24)}` }
const device = { device_serial: 'QA_NO_REAL_PHONE', connection_mode: 'local', bridge_id: '', display_name: 'QA 隔离设备', connection_label: '隔离测试连接' }
const steps = [{ step_id: 'step_01', ordinal: 1, title: 'QA 采集步骤', operation: 'wait', slug: 'wait', seconds: 0, expected_targets: [] }]
const capture = { selected_step_ids: ['step_01'], collectors: ['trace', 'pmu'], preparation_mode: 'auto', pmu: { binary_path: '/data/local/tmp/thread_pmu_sampler', period_us: 10, duration_ms: 1000, group: 'auto', reader_cpu: 0 } }
const profile = { bundle: 'qa.fixture.only', ability: 'MainAbility', screen_size: [1280, 2832], targets: {}, trace_categories: ['sched'] }
const catalog = { templates: [{ template_id: 'qa', name: 'QA 场景', steps }], default_template_id: 'qa', default_profile: profile, default_capture: capture, operations: ['wait'], pmu_sampler: { period_us_range: [10, 10000] } }
const now = new Date().toISOString()
const usage = { schema_version: 'agent_cli_token_usage/v1', input_tokens: 0, output_tokens: 0, total_tokens: 0, cached_input_tokens: 0, cache_creation_input_tokens: 0, uncached_input_tokens: 0, cache_hit_rate: null, model_calls: 0, source: 'qa_fixture', updated_at: now }
const terminal = { schema_version: 'agent_cli_terminal/v1', terminal_session_id: 'qa_repair_terminal', run_id: 'qa_agent_run', adapter_id: 'qa_internal_agent', agent_display_name: 'QA Internal Agent', agent_session_id: 'qa_native_session', agent_workspace_id: 'qa_workspace', state: 'running', pid: null, exit_code: null, cols: 100, rows: 32, created_at: now, last_activity_at: now, earliest_sequence: 0, latest_sequence: 0, transport: 'pty', submit_mode: 'bracketed_paste_cr', startup_input_delay_ms: 0, submit_delay_ms: 35, effective_submit_delay_ms: 1000, submit_fallback_ms: 0, token_usage: usage }
const agentRun = { run_id: terminal.run_id, state: 'running', state_detail: 'QA fixture', current_turn: 0, event_cursor: 0, budget: { max_turns: 20, max_actions_per_turn: 5, max_output_tokens: 4000, context_window_tokens: 64000 }, pending_ui_actions: [] }
const adapter = { schema_version: 'agent_cli_adapter/v1', adapter_id: terminal.adapter_id, display_name: terminal.agent_display_name, available: true, session_strategy: 'native_session', submit_mode: terminal.submit_mode, startup_input_delay_ms: 0, submit_delay_ms: 35, submit_fallback_ms: 0, capabilities: {}, mcp_integration: { declared: false, configured: false, format: 'none', path: null, delivery: 'environment_and_workspace', warning: null }, browser_model_override_supported: false, usage_kind: 'unavailable', source: 'qa_fixture', default: true }

function run(id, status, pmuGate = false) {
  const gate = { allow_execution: false, code: 'PMU_SAMPLER_CAPABILITIES_TIMEOUT', message: 'QA PMU 能力探测超时，本轮未执行采样器。' }
  return { run_id: id, plan_id: ids.plan, status, title: id === ids.oldRun ? 'QA 历史失败' : 'QA 当前采集', created_at: now, ...device, manifest_artifact_id: 'qa_manifest', manifest: {
    remote_root: '/data/local/tmp/hetero_scenarios/' + id,
    ...(pmuGate ? { pmu_preparation: { ...gate, status: 'probe_timed_out' } } : {}),
    steps: [{ ...steps[0], captured: true, status: status === 'failed' ? 'failed' : 'succeeded', collector_status: { trace: 'collected', pmu: status === 'partial' ? 'partial' : 'collected' }, pmu_validation: pmuGate ? { sampler_preparation: gate, diagnostics: [{ code: gate.code, message: gate.message }] } : { diagnostics: [{ code: 'PMU_GROUP_LOW_COVERAGE', message: '普通质量提示' }] } }],
    quality: { errors: status === 'failed' ? [{ code: 'QA_CAPTURE_FAILED', step_id: 'step_01', reason: 'QA 当前采集失败，需要修复选择器。' }] : [], warnings: [] },
    files: [{ kind: 'action_log', step_id: 'step_01', relative_path: '01_wait/actions.log' }],
  } }
}
async function fixture({ initialStatus = 'failed', pmuGate = false, preference, restored = false, agentGate, ticketGate, autoRetry = false, retryStatus = 'failed', delayedRetryCancel = false, cancelFailures = 0 } = {}) {
  const entries = [...(preference !== undefined ? [{ name: prefKey, value: String(preference) }] : []), ...(restored ? [{ name: activeKey, value: ids.job }] : [])]
  const context = await browser.newContext({ viewport: { width: 1560, height: 1080 }, serviceWorkers: 'block', storageState: { cookies: [], origins: [{ origin, localStorage: entries }] } })
  await context.routeWebSocket('**/*', socket => socket.close())
  const state = { initialStatus, commands: [], contexts: [], creates: 0, cancels: 0, executes: 0, ticket: null, plans: [], reads: 0, retryStatus, closed: false, runVisible: restored }
  const currentRun = () => run(ids.run, state.initialStatus, pmuGate)
  const getJob = (id, status) => ({ job_id: id, kind: 'scenario_capture', status, progress: { message: 'QA 采集进度' }, run_id: id === ids.retryJob ? ids.retryRun : ids.run, result: ['queued', 'running'].includes(status) ? null : run(id === ids.retryJob ? ids.retryRun : ids.run, status, id === ids.job && pmuGate), error: status === 'failed' ? { code: 'QA_CAPTURE_FAILED', message: 'QA 采集失败' } : null })
  const reply = (route, json, status = 200) => route.fulfill({ json, status }).catch(() => {})
  await context.route('**/v1/**', async route => {
    const request = route.request(), pathname = new URL(request.url()).pathname, method = request.method()
    const body = method === 'GET' ? null : request.postDataJSON()
    if (pathname === '/v1/scenario-capture/catalog') return reply(route, catalog)
    if (pathname === '/v1/scenario-capture/plans') {
      if (method === 'GET') return reply(route, { plans: state.plans })
      const plan = { ...body, plan_id: ids.plan, readiness: { status: 'ready', blockers: [] } }; state.plans = [plan]; return reply(route, plan)
    }
    if (pathname === '/v1/scenario-capture/runs') return reply(route, { runs: [...(state.runVisible ? [currentRun()] : []), run(ids.oldRun, 'failed')] })
    if (pathname === `/v1/scenario-capture/runs/${ids.run}`) return reply(route, currentRun())
    if (pathname === '/v1/scenario-capture/devices/discover') return reply(route, { devices: [device], selected_device: device })
    if (pathname === `/v1/scenario-capture/plans/${ids.plan}/execute`) { state.executes++; state.runVisible = true; return reply(route, getJob(ids.job, 'queued')) }
    if (pathname === `/v1/parallel-jobs/${ids.job}/cancel`) { state.initialStatus = 'failed'; return reply(route, getJob(ids.job, 'cancelled')) }
    if (pathname === `/v1/parallel-jobs/${ids.job}`) {
      state.reads++
      return reply(route, getJob(ids.job, restored && state.reads === 1 ? 'running' : state.initialStatus))
    }
    if (pathname === `/v1/parallel-jobs/${ids.retryJob}`) return reply(route, getJob(ids.retryJob, state.retryStatus))
    if (pathname === `/v1/scenario-capture/runs/${ids.run}/repair-request`) {
      assert.equal(body.job_id, ids.job)
      state.creates++
      state.ticket = { repair_request_id: ids.ticket, source_job_id: ids.job, source_run_id: ids.run, plan_id: ids.plan, ...device, status: 'pending', expires_at: '2099-01-01T00:00:00Z' }
      if (ticketGate) await ticketGate.promise
      return reply(route, state.ticket)
    }
    if (pathname === `/v1/scenario-capture/repair-requests/${ids.ticket}/cancel`) { state.cancels++; if (state.cancels <= cancelFailures) return reply(route, { error: { message: 'QA cancel connection failed once' } }, 503); if (state.ticket) { state.ticket.status = 'cancelled'; if (state.ticket.retry_job_id) { state.ticket.retry_status = state.retryStatus; if (!delayedRetryCancel) state.retryStatus = 'cancelled' } } return reply(route, state.ticket) }
    if (pathname === `/v1/scenario-capture/repair-requests/${ids.ticket}`) {
      if (state.ticket?.retry_job_id) { state.ticket.retry_status = state.retryStatus; if (state.ticket.status !== 'cancelled') state.ticket.status = ['queued', 'running'].includes(state.retryStatus) ? 'submitted' : state.retryStatus }
      return reply(route, state.ticket)
    }
    if (pathname === '/v1/parallel-modeling/catalog') return reply(route, { builtins: [], hardware_profiles: [], model_packages: [], objectives: [] })
    if (pathname === '/v1/agent-cli/adapters') return reply(route, { schema_version: 'agent_cli_adapter_catalog/v1', default_adapter_id: adapter.adapter_id, adapters: [adapter], manifest_error: null })
    if (pathname === '/v1/agent-cli/sessions' && method === 'POST') { if (agentGate) await agentGate.promise; return reply(route, { terminal, run: agentRun }) }
    if (pathname === `/v1/agent-cli/sessions/${terminal.terminal_session_id}`) return reply(route, { terminal, run: agentRun })
    if (pathname === `/v1/agent-cli/sessions/${terminal.terminal_session_id}/context`) { state.contexts.push(body); return reply(route, { terminal, run: agentRun }) }
    if (pathname === `/v1/agent-cli/sessions/${terminal.terminal_session_id}/commands`) {
      state.commands.push(body)
      if (autoRetry && state.ticket?.status === 'pending') Object.assign(state.ticket, { status: 'submitted', retry_job_id: ids.retryJob, retry_run_id: ids.retryRun })
      return reply(route, { accepted: true, terminal })
    }
    if (pathname.endsWith('/usage') && pathname.startsWith('/v1/agent-cli/')) return reply(route, { token_usage: usage })
    if (pathname.endsWith('/output') && pathname.startsWith('/v1/agent-cli/')) { await delay(80); return state.closed ? route.abort() : reply(route, { schema_version: 'agent_cli_output_poll/v1', chunks: [], terminal, truncated: false, more_available: false }) }
    if (/\/(resize|redraw)$/.test(pathname) && pathname.startsWith('/v1/agent-cli/')) return reply(route, { terminal })
    if (pathname.startsWith('/v1/agent-runs/') && method === 'GET') return reply(route, pathname.includes('/events/poll') ? { events: [], cursor: 0, more_available: false, run: agentRun } : agentRun)
    if (/scenario-capture|parallel-jobs|agent|device-bridge|model-relay/.test(pathname)) {
      if (method !== 'GET') report.blockedWrites.push({ method, pathname })
      return reply(route, { error: { message: 'QA isolated endpoint' } }, 503)
    }
    if (method === 'POST' && ['/v1/optimization-kb/search', '/v1/hardware-kb/search'].includes(pathname)) return route.continue()
    if (method !== 'GET') { report.blockedWrites.push({ method, pathname }); return reply(route, { error: { message: 'QA blocks unmocked write' } }, 503) }
    return route.continue()
  })
  const open = async () => {
    const page = await context.newPage()
    page.on('pageerror', error => report.pageErrors.push(error.message))
    await page.goto(web)
    await page.getByRole('button', { name: '场景采集', exact: true }).click()
    await page.getByRole('heading', { name: '按步骤采集真实运行数据' }).waitFor()
    await page.getByRole('checkbox', { name: '失败后自动修复并重试', exact: true }).waitFor()
    return page
  }
  return { state, context, open, page: await open(), close: async () => { state.closed = true; agentGate?.resolve(); ticketGate?.resolve(); await context.close() } }
}
const execute = async page => { const button = page.getByRole('button', { name: /^(保存并执行采集|在设备执行采集)$/ }); await until(() => button.isEnabled(), 'execute enabled'); await button.click() }
const toggle = page => page.getByRole('checkbox', { name: '失败后自动修复并重试', exact: true })
async function check(name, options, test) { const f = await fixture(options); try { await test(f); report.cases.push(name) } finally { await f.close() } }
try {
  await check('default enabled: actual Agent command, exact retry tracking, one repair maximum', { autoRetry: true }, async ({ page, state, open }) => {
    assert.equal(await toggle(page).isChecked(), true)
    await execute(page)
    await until(() => state.commands.length === 1, 'automatic Agent command')
    const command = state.commands[0].command
    for (const required of [ids.run, ids.plan, ids.ticket, device.device_serial, 'QA 当前采集失败', '01_wait/actions.log', 'scenario_capture_execute']) assert.ok(command.includes(required), required)
    assert.ok(!command.includes('请加载'), 'repair task is a platform feature, not an invented Skill')
    await page.getByText('修复后的重采仍然失败，本次自动修复已结束。', { exact: true }).waitFor()
    await page.locator('.pm-progress').getByText('QA 采集进度', { exact: true }).waitFor()
    assert.equal(state.creates, 1)
    await page.screenshot({ path: path.join(out, 'repair-retry-failed.png') })
    const another = await open(); await delay(1800)
    assert.equal(state.commands.length, 1, 'new window must not replay repair')
    assert.equal(state.creates, 1)
    await another.close()
  })
  await check('partial PMU capability failure starts repair', { initialStatus: 'partial', pmuGate: true }, async ({ page, state }) => {
    await execute(page); await until(() => state.commands.length === 1, 'PMU repair command')
    assert.ok(state.commands[0].command.includes('PMU_SAMPLER_CAPABILITIES_TIMEOUT'))
    await page.screenshot({ path: path.join(out, 'pmu-repair.png') })
  })
  await check('ordinary partial PMU coverage does not replay user actions', { initialStatus: 'partial' }, async ({ page, state }) => {
    await execute(page); await until(() => state.reads > 0, 'job observed'); await delay(1800)
    assert.equal(state.creates, 0); assert.equal(state.commands.length, 0)
  })
  await check('disabled preference persists and suppresses repair', { preference: false }, async ({ page, state, open }) => {
    assert.equal(await toggle(page).isChecked(), false)
    await execute(page); await until(() => state.reads > 0, 'failed job observed'); await delay(500)
    assert.equal(state.creates, 0)
    const another = await open(); assert.equal(await toggle(another).isChecked(), false); await another.close()
  })
  await check('history selection and restored active job failure do not repair', { restored: true }, async ({ page, state }) => {
    await until(() => state.reads >= 2, 'restored terminal job')
    await page.getByRole('button', { name: /QA 历史失败/ }).click(); await delay(500)
    assert.equal(state.creates, 0); assert.equal(state.commands.length, 0)
  })
  await check('manual cancellation suppresses repair even if terminal result is failed', { initialStatus: 'running' }, async ({ page, state }) => {
    await execute(page); await page.locator('.pm-progress').getByRole('button', { name: '取消', exact: true }).click()
    await until(() => state.reads >= 2, 'cancelled task final poll'); await delay(500)
    assert.equal(state.creates, 0); assert.equal(state.commands.length, 0)
  })
  await check('disable during automatic retry stops the retry job', { autoRetry: true, retryStatus: 'running', delayedRetryCancel: true }, async ({ page, state }) => {
    await execute(page)
    await page.getByText('助手已发起修复后的重采，正在跟踪新的采集任务。', { exact: true }).waitFor()
    await toggle(page).uncheck()
    await until(() => state.cancels > 0, 'retry stop requested')
    await delay(250)
    assert.equal(await page.getByText('本次重采已取消。', { exact: true }).count(), 0, 'Cancellation request is not completed job cancellation')
    state.retryStatus = 'cancelled'
    await page.getByText('本次重采已取消。', { exact: true }).waitFor()
    assert.equal(state.retryStatus, 'cancelled'); assert.equal(state.commands.length, 1)
  })
  const agentGate = deferred()
  await check('disable while Agent initializes revokes queued command and retry ticket', { agentGate }, async ({ page, state }) => {
    await execute(page); await until(() => state.creates === 1, 'repair ticket')
    await page.getByText('正在向当前助手发送失败记录与修复请求。', { exact: true }).waitFor()
    await toggle(page).uncheck(); await until(() => state.cancels > 0, 'ticket cancelled')
    agentGate.resolve(); await delay(2200)
    assert.equal(state.commands.length, 0); assert.equal(state.ticket.status, 'cancelled')
  })
  const ticketGate = deferred()
  await check('disable during ticket creation cancels receipt before Agent dispatch', { ticketGate, cancelFailures: 1 }, async ({ page, state }) => {
    await execute(page); await until(() => state.creates === 1, 'inflight ticket creation')
    await toggle(page).uncheck(); ticketGate.resolve()
    await until(() => state.cancels === 1, 'first cancellation attempt')
    assert.equal(await page.getByText('本次自动修复已停止。', { exact: true }).count(), 0, 'Failed cancellation must remain unconfirmed')
    await until(() => state.cancels >= 2, 'late ticket cancellation retried'); await delay(500)
    assert.equal(state.commands.length, 0); assert.equal(state.ticket.status, 'cancelled')
  })
  assert.deepEqual(report.pageErrors, [])
  assert.deepEqual(report.blockedWrites, [])
  report.pass = true
} finally {
  await fs.writeFile(path.join(out, 'report.json'), JSON.stringify(report, null, 2))
  await browser.close()
}
console.log(JSON.stringify(report))
