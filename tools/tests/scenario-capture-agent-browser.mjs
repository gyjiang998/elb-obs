/**
 * Browser integration with an isolated HTTP Agent. No real Agent or device writes.
 * Run against a built local preview:
 * SCENARIO_AGENT_QA_WEB=http://127.0.0.1:4032 PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs node tools/tests/scenario-capture-agent-browser.mjs
 */
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const root = fileURLToPath(new URL('../../', import.meta.url))
const web = process.env.SCENARIO_AGENT_QA_WEB
assert.ok(web, 'Set SCENARIO_AGENT_QA_WEB to a local preview URL explicitly')
assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(new URL(web).hostname), 'Only a local preview is permitted')
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ? pathToFileURL(process.env.PLAYWRIGHT_MODULE).href : 'playwright')
const out = path.join(root, '.local-artifacts/local-preview/scenario-agent-browser')
await fs.mkdir(out, { recursive: true })
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
async function bounded(promise, label) {
  let timer
  try { return await Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(Error(`Timed out: ${label}`)), 30000) })]) }
  finally { clearTimeout(timer) }
}
const usage = { schema_version: 'agent_cli_token_usage/v1', input_tokens: 0, output_tokens: 0, total_tokens: 0, cached_input_tokens: 0, cache_creation_input_tokens: 0, uncached_input_tokens: 0, cache_hit_rate: null, model_calls: 0, source: 'qa_fixture', updated_at: new Date().toISOString() }
const terminal = { schema_version: 'agent_cli_terminal/v1', terminal_session_id: 'qa_terminal_only', run_id: 'qa_run_only', adapter_id: 'qa_internal_agent', agent_display_name: 'QA Internal Agent', agent_session_id: 'qa_native_session', agent_workspace_id: 'qa_workspace', state: 'running', pid: null, exit_code: null, cols: 100, rows: 32, created_at: new Date().toISOString(), last_activity_at: new Date().toISOString(), earliest_sequence: 0, latest_sequence: 0, transport: 'pty', submit_mode: 'bracketed_paste_cr', startup_input_delay_ms: 0, submit_delay_ms: 35, effective_submit_delay_ms: 1000, submit_fallback_ms: 0, token_usage: usage }
const run = { run_id: terminal.run_id, state: 'running', state_detail: 'QA fixture only', current_turn: 0, event_cursor: 0, budget: { max_turns: 20, max_actions_per_turn: 5, max_output_tokens: 4000, context_window_tokens: 64000 }, pending_ui_actions: [] }
const adapter = { schema_version: 'agent_cli_adapter/v1', adapter_id: terminal.adapter_id, display_name: terminal.agent_display_name, description: 'Isolated browser fixture', available: true, diagnostic: null, session_strategy: 'native_session', submit_mode: terminal.submit_mode, startup_input_delay_ms: 0, submit_delay_ms: 35, submit_fallback_ms: 0, capabilities: {}, mcp_integration: { declared: false, configured: false, format: 'none', path: null, delivery: 'environment_and_workspace', warning: null }, browser_model_override_supported: false, usage_kind: 'unavailable', source: 'qa_fixture', default: true }
const browser = await chromium.launch({ ...(process.env.CHROME_EXECUTABLE ? { executablePath: process.env.CHROME_EXECUTABLE } : {}), headless: true })
const oldBridgeError = 'QA 旧 Windows HDC 路径不可用'
const bridgeA = { bridge_id: 'qa_old_windows', display_name: 'QA 旧 Windows', connection_state: 'connected', capabilities: { runtime_diagnostics: { status: 'failed', issues: [{ message_zh: oldBridgeError }] } } }
const bridgeB = { bridge_id: 'qa_new_windows', display_name: 'QA 新 Windows', connection_state: 'connected' }
const device = (bridge, serial = 'QA_SAME_SERIAL') => ({ connection_mode: bridge ? 'bridge' : 'local', bridge_id: bridge?.bridge_id ?? '', device_serial: serial, display_name: bridge?.display_name ?? 'QA 本机设备', connection_label: bridge ? 'Windows Bridge' : '本机 HDC' })
const deviceA = device(bridgeA), deviceB = device(bridgeB), localDevice = device(null)
const target = value => ({ connection_mode: value.connection_mode, bridge_id: value.bridge_id, device_serial: value.device_serial })
const deviceKey = value => JSON.stringify([value.connection_mode, value.bridge_id, value.device_serial])
const routeKey = value => JSON.stringify([value.connection_mode, value.bridge_id ?? ''])
const discoveries = []
let discoveryMode = 'empty'
const context = await browser.newContext({ viewport: { width: 1600, height: 1050 }, serviceWorkers: 'block', storageState: { cookies: [], origins: [{ origin: new URL(web).origin, localStorage: [{ name: 'heterogeneous-enable-device-bridge-selection-v1', value: JSON.stringify({ schemaVersion: 'device_bridge_selection/v1', bridgeId: bridgeA.bridge_id, savedAt: new Date().toISOString() }) }] }] } })
await context.routeWebSocket('**/v1/**', socket => socket.close())
const page = await context.newPage()
const commands = [], contexts = [], requests = [], pageErrors = [], blockedWrites = []
let createCount = 0, restoreCount = 0, closing = false, contextGate = null
const createGate = deferred(), createStarted = deferred()
page.on('pageerror', error => pageErrors.push(error.message))
if (process.env.SCENARIO_AGENT_QA_DEBUG) { page.on('console', item => console.error(item.type(), item.text())); page.on('requestfailed', item => console.error('requestfailed', item.url(), item.failure())); }

await page.route('**/v1/**', async route => {
  const request = route.request(), url = new URL(request.url()), method = request.method()
  const pathname = url.pathname
  const body = method === 'GET' ? null : request.postDataJSON()
  requests.push({ method, path: pathname })
  const reply = json => route.fulfill({ json })
  if (pathname === '/v1/agent-cli/adapters') return reply({ schema_version: 'agent_cli_adapter_catalog/v1', default_adapter_id: adapter.adapter_id, adapters: [adapter], manifest_error: null })
  if (pathname === '/v1/agent-cli/sessions' && method === 'POST') {
    createCount++
    createStarted.resolve()
    await createGate.promise
    return reply({ terminal, run })
  }
  if (pathname === `/v1/agent-cli/sessions/${terminal.terminal_session_id}` && method === 'GET') {
    restoreCount++
    return reply({ terminal, run })
  }
  if (pathname === `/v1/agent-cli/sessions/${terminal.terminal_session_id}/context`) {
    contexts.push(body)
    if (body.context?.focused_task && contextGate) await contextGate.promise
    return reply({ terminal, run })
  }
  if (pathname === `/v1/agent-cli/sessions/${terminal.terminal_session_id}/commands`) {
    commands.push(body)
    return reply({ accepted: true, terminal })
  }
  if (pathname.endsWith('/usage') && pathname.startsWith('/v1/agent-cli/')) return reply({ token_usage: usage })
  if (/^\/v1\/agent-cli\/sessions\/qa_terminal_only\/(resize|redraw)$/.test(pathname)) return reply({ terminal })
  if (pathname === `/v1/agent-cli/sessions/${terminal.terminal_session_id}/output`) {
    await sleep(120)
    if (closing) return route.abort()
    return reply({ schema_version: 'agent_cli_output_poll/v1', chunks: [], terminal, truncated: false, more_available: false })
  }
  if (pathname.startsWith('/v1/agent-runs/')) {
    if (method === 'GET' && pathname.includes('/events/poll')) return reply({ events: [], cursor: 0, more_available: false, run })
    if (method === 'GET') return reply(run)
    blockedWrites.push({ method, path: pathname })
    return route.fulfill({ status: 503, json: { error: { message: 'No real Agent action in browser QA' } } })
  }
  if (pathname === '/v1/scenario-capture/plans') return reply({ plans: [] })
  if (pathname === '/v1/scenario-capture/runs') return reply({ runs: [] })
  if (pathname === '/v1/device-bridges') return reply({ bridges: [bridgeA, bridgeB] })
  if (pathname === '/v1/scenario-capture/devices/discover') {
    discoveries.push(body)
    const all = body.connection_mode === 'auto'
    const found = all ? [deviceA, deviceB, localDevice] : [deviceA, deviceB, localDevice].filter(value => value.connection_mode === body.connection_mode && value.bridge_id === (body.bridge_id ?? ''))
    const timeout = discoveryMode === 'timeout' && !all
    const partial = discoveryMode === 'partial' && all
    const empty = discoveryMode === 'empty'
    const devices = empty || timeout ? [] : partial ? [deviceB, localDevice] : found
    const diagnostics = found.map(value => ({ ...target(value), status: empty ? 'empty' : timeout || partial && value.bridge_id === bridgeA.bridge_id ? 'timed_out' : 'ready', device_count: empty || timeout ? 0 : 1 }))
    return reply({ status: timeout ? 'discovery_incomplete' : empty ? 'no_devices' : all ? 'selection_required' : 'ready', devices, diagnostics, discovery_complete: !timeout && !partial, selected_device: all || timeout || empty ? null : devices[0], message: empty ? 'QA no device found; choose a connection route' : timeout ? 'QA selected route timed out' : partial ? 'QA unrelated old Windows failed' : 'QA choose one device' })
  }
  if (/agent|device-bridge/.test(pathname)) return route.fulfill({ status: 503, json: { error: { message: 'Isolated QA endpoint' } } })
  if (method !== 'GET') {
    blockedWrites.push({ method, path: pathname })
    return route.fulfill({ status: 503, json: { error: { message: 'QA blocks all unmocked mutations' } } })
  }
  return route.continue()
})

try {
  await page.goto(web)
  await page.getByRole('button', { name: '负载采样', exact: true }).click()
  const selector = () => page.getByLabel('采样设备', { exact: true })
  const assistantSelector = () => page.getByLabel('助手采样设备', { exact: true })
  const routeSelector = () => page.getByLabel('设备连接路径', { exact: true })
  const assistantRouteSelector = () => page.getByLabel('助手设备连接路径', { exact: true })
  const leftPicker = () => page.locator('.capture-device-picker:not(.compact)')
  const assistantPicker = () => page.locator('.capture-device-picker.compact')
  async function showCaptureSettings(open = true) {
    const shown = await assistantSelector().count() > 0
    if (shown !== open) await page.getByRole('button', { name: '页面助手设置', exact: true }).click()
    if (open) await assistantSelector().waitFor()
  }
  async function checkCaptureSettings(expected) {
    await showCaptureSettings()
    assert.equal(await page.getByLabel('选择远程设备桥', { exact: true }).count(), 0, 'Capture page uses the shared capture picker rather than a stale generic Bridge selector')
    assert.equal(await page.getByText(oldBridgeError, { exact: true }).count(), 0, 'Stale A diagnostic must not be shown for the capture page')
    const content = await assistantPicker().innerText()
    assert.equal(await assistantSelector().inputValue(), expected ? deviceKey(expected) : '')
    assert.equal(await assistantSelector().inputValue(), await selector().inputValue(), 'Both entry points share one selected device')
    assert.equal(await assistantRouteSelector().inputValue(), await routeSelector().inputValue(), 'Both entry points share one connection route')
    if (expected) {
      assert.ok(content.includes(expected.display_name))
      assert.ok(content.includes(expected.device_serial))
      assert.equal(await assistantSelector().isEnabled(), true, 'The assistant capture device selector is interactive')
    } else assert.ok(content.includes('请选择本次使用的设备') || content.includes('可先选择连接路径'))
    await showCaptureSettings(false)
  }

  // Empty discovery must not trap users: registered routes remain selectable in
  // either entry point, and a route refresh can recover without any Agent call.
  await page.waitForFunction(() => document.querySelector('.capture-device-picker:not(.compact)')?.textContent.includes('QA no device found'))
  assert.deepEqual(discoveries[0], { connection_mode: 'auto' }, 'Opening capture automatically discovers every connection')
  const initialDiscoveryCount = discoveries.length
  await sleep(100)
  assert.equal(discoveries.length, initialDiscoveryCount, 'Initial discovery runs once rather than repeating after its own state update')
  assert.equal(await selector().isDisabled(), true)
  assert.equal(await routeSelector().isEnabled(), true)
  await showCaptureSettings()
  assert.equal(await assistantSelector().isDisabled(), true)
  assert.equal(await assistantRouteSelector().isEnabled(), true)
  assert.equal(await assistantRouteSelector().locator('option').count(), 4, 'Registered Windows routes remain available before a phone is discovered')
  await page.screenshot({ path: path.join(out, 'empty-device-recovery.png') })
  discoveryMode = 'healthy'
  await assistantRouteSelector().selectOption(routeKey(deviceB))
  await page.waitForFunction(key => document.querySelector('select[aria-label="采样设备"]')?.value === key, deviceKey(deviceB))
  assert.equal(await routeSelector().inputValue(), routeKey(deviceB), 'Assistant route changes update the capture page')
  let beforeRefresh = discoveries.length
  await assistantPicker().getByRole('button', { name: '刷新设备', exact: true }).click()
  for (let i = 0; i < 100 && discoveries.length === beforeRefresh; i++) await sleep(20)
  assert.deepEqual(discoveries.at(-1), target(deviceB), 'Assistant refresh probes the selected route and serial')
  beforeRefresh = discoveries.length
  await assistantPicker().getByRole('button', { name: '全部连接', exact: true }).click()
  for (let i = 0; i < 100 && discoveries.length === beforeRefresh; i++) await sleep(20)
  assert.deepEqual(discoveries.at(-1), { connection_mode: 'auto' }, 'Assistant all-connections refresh discovers every route')
  await assistantSelector().locator('option').filter({ hasText: 'QA 本机设备' }).waitFor({ state: 'attached' })
  await assistantSelector().selectOption(deviceKey(localDevice))
  await page.waitForFunction(key => document.querySelector('select[aria-label="采样设备"]')?.value === key, deviceKey(localDevice))
  await assistantSelector().selectOption(deviceKey(deviceB))
  await page.screenshot({ path: path.join(out, 'shared-device-picker.png'), fullPage: true })
  await showCaptureSettings(false)
  await checkCaptureSettings(deviceB)
  await page.getByLabel('负载模板').selectOption('custom')
  const steps = ['启动图库并保持当前图片未修改', '打开第二张图片，保留中文“示例🙂”', ...Array.from({ length: 12 }, (_, i) => `操作阶段 ${i + 1}：展开编辑面板并记录主线程、内存和 PMU 指标`)].join('\n')
  await page.getByLabel('操作步骤 · 每行一步').fill(steps)
  await page.getByLabel('计划名称', { exact: true }).fill('中文多行采集计划 · 点击时草稿')
  await bounded(createStarted.promise, 'Agent fixture initialization')
  await page.getByRole('button', { name: '让助手生成计划', exact: true }).click()
  await page.getByLabel('计划名称', { exact: true }).fill('提交后继续编辑的草稿')
  await selector().selectOption(deviceKey(localDevice))
  await sleep(150)
  assert.equal(commands.length, 0, 'Page tasks must wait for the session to initialize')
  createGate.resolve()
  await page.waitForFunction(() => document.querySelector('.pm-notice')?.textContent.includes('请求已发往助手终端'))
  assert.equal(commands.length, 1, 'One button click sends one command')
  const first = commands[0]
  assert.ok(first.command.includes('\n'), 'The command preserves the skill prefix newline')
  const parseDraft = command => JSON.parse(command.split('读取当前场景草稿 ')[1].split('，将自然语言步骤转换为')[0])
  const firstDraft = parseDraft(first.command)
  assert.equal(firstDraft.title, '中文多行采集计划 · 点击时草稿')
  assert.equal(firstDraft.natural_language_steps, steps)
  assert.equal(firstDraft.steps.length, 14)
  assert.ok(Buffer.byteLength(first.command) > 4500)
  const focusedFirst = contexts.find(item => item.context?.focused_task?.request_context?.plan_draft?.title === firstDraft.title).context
  assert.deepEqual(focusedFirst.scenario_capture_target, target(deviceB), 'Queued task retains selected B while current page moves to local')
  assert.deepEqual(focusedFirst.device_selector, target(deviceB))
  assert.ok(first.command.includes('connection_mode=bridge，bridge_id=qa_new_windows'))
  await page.screenshot({ path: path.join(out, 'initialized-submission.png') })

  // Reload reattaches the same native session. Hold the focused context write
  // while the page generates several ordinary context updates.
  await page.reload()
  await page.getByRole('button', { name: '负载采样', exact: true }).click()
  await page.waitForFunction(() => document.querySelector('.kimi-webshell.is-ready'))
  await selector().waitFor()
  assert.equal(await selector().inputValue(), deviceKey(localDevice), 'Reload preserves the last explicit capture device and route')
  await page.getByRole('button', { name: '刷新全部设备', exact: true }).click()
  await selector().locator(`option[value='${deviceKey(deviceB)}']`).waitFor({ state: 'attached' })
  await selector().selectOption(deviceKey(deviceB))
  assert.equal(createCount, 1, 'Reload must reuse the stored session')
  assert.ok(restoreCount >= 1)
  await page.getByLabel('计划名称', { exact: true }).fill('复用会话提交 · 原始快照')
  contextGate = deferred()
  const priorContexts = contexts.length
  await page.getByRole('button', { name: '让助手生成计划', exact: true }).click()
  for (let i = 0; i < 3; i++) await page.getByLabel('计划名称', { exact: true }).fill(`排队期间编辑 ${i}`)
  for (let i = 0; i < 100 && !contexts.slice(priorContexts).some(item => item.context?.focused_task); i++) await sleep(20)
  assert.ok(contexts.slice(priorContexts).some(item => item.context?.focused_task), 'Focused context must be synchronized before the command')
  assert.equal(commands.length, 1, 'Command waits for its context update')
  await selector().selectOption(deviceKey(localDevice))
  contextGate.resolve()
  contextGate = null
  await page.waitForFunction(() => document.querySelector('.pm-notice')?.textContent.includes('请求已发往助手终端'))
  assert.equal(commands.length, 2)
  assert.notEqual(commands[1].command_id, first.command_id)
  assert.equal(parseDraft(commands[1].command).title, '复用会话提交 · 原始快照')
  assert.ok(!commands[1].command.includes('排队期间编辑'))
  assert.equal(createCount, 1)
  const focusedSecond = contexts.slice(priorContexts).find(item => item.context?.focused_task).context
  assert.deepEqual(focusedSecond.device_selector, target(deviceB), 'Submitted context snapshot wins over subsequent page selection')

  async function manual(message, expected) {
    const before = commands.length
    await page.locator('.agent-local-composer textarea').fill(message)
    await page.locator('.agent-local-composer-send').click()
    for (let i = 0; i < 100 && commands.length === before; i++) await sleep(30)
    assert.equal(commands.length, before + 1, 'Manual chat sends one command')
    const runtime = contexts.at(-1).context
    assert.deepEqual(runtime.device_selector, expected ?? {})
    assert.equal(runtime.device_execution_mode, expected ? expected.connection_mode === 'local' ? 'local_hdc' : 'remote_bridge' : 'unselected')
    assert.equal(runtime.scenario_capture_selection_required, !expected)
    const projected = typeof runtime.page_context === 'string' ? JSON.parse(runtime.page_context) : runtime.page_context
    assert.equal(projected.active_view, 'scenario_capture')
    if (expected) assert.deepEqual(target(projected.scenario_capture.selected_device), expected)
  }
  await checkCaptureSettings(localDevice)
  await manual('QA 手动消息使用本机选定设备', target(localDevice))
  await selector().selectOption(deviceKey(deviceB))
  await manual('QA 手动消息使用新 Windows 设备', target(deviceB))
  const probeStart = discoveries.length
  await sleep(100)
  await page.evaluate(() => window.dispatchEvent(new Event('focus')))
  for (let i = 0; i < 100 && discoveries.length === probeStart; i++) await sleep(20)
  assert.deepEqual(discoveries.at(-1), target(deviceB), 'Periodic/focus refresh probes selected B only')
  discoveryMode = 'partial'
  await page.getByRole('button', { name: '刷新全部设备', exact: true }).click()
  await sleep(200)
  assert.deepEqual(discoveries.at(-1), { connection_mode: 'auto' })
  assert.equal(await selector().inputValue(), deviceKey(deviceB), 'All refresh retains B despite local device and broken A')
  assert.ok((await leftPicker().locator('.pm-device-card').innerText()).includes('已连接'))
  assert.equal(await selector().locator('option').count(), 4, 'Same serial on three routes remains three distinct choices')
  discoveryMode = 'timeout'
  await page.evaluate(() => window.dispatchEvent(new Event('focus')))
  await page.waitForFunction(() => document.querySelector('.pm-device-card')?.textContent.includes('待确认'))
  assert.equal(await selector().inputValue(), deviceKey(deviceB))
  assert.ok(!(await page.locator('.pm-device-card').innerText()).includes('等待重连'), 'Timeout is uncertainty rather than offline')
  discoveryMode = 'healthy'
  await page.reload()
  await page.getByRole('button', { name: '负载采样', exact: true }).click()
  await page.waitForFunction(() => document.querySelector('.kimi-webshell.is-ready'))
  await selector().waitFor()
  assert.equal(await selector().inputValue(), deviceKey(deviceB), 'Reload preserves the selected Windows device after a transient discovery timeout')
  await checkCaptureSettings(deviceB)
  await manual('QA 重载保留所选 Windows 设备', target(deviceB))
  await showCaptureSettings()
  await assistantRouteSelector().selectOption(routeKey({ connection_mode: 'auto' }))
  await page.waitForFunction(() => document.querySelector('select[aria-label="采样设备"]')?.value === '')
  await showCaptureSettings(false)
  await checkCaptureSettings(null)
  await manual('QA 未选择场景设备不可继承旧 Bridge', null)
  await page.getByRole('button', { name: '负载特征', exact: true }).click()
  await page.getByRole('button', { name: '页面助手设置', exact: true }).click()
  await page.getByLabel('选择远程采集端', { exact: true }).waitFor()
  assert.equal(await page.getByLabel('选择远程采集端', { exact: true }).inputValue(), bridgeA.bridge_id, 'Other workspaces retain their configured Bridge')
  assert.equal(await page.getByText(oldBridgeError, { exact: true }).count(), 1, 'Other workspace diagnostics remain unchanged')
  assert.deepEqual(pageErrors, [])
  assert.ok(!requests.some(item => item.path.endsWith('/input')), 'No raw Enter retries or pasted text through a second path')
  await page.screenshot({ path: path.join(out, 'reattached-submission.png') })
  const report = { pass: true, cases: ['capture entry automatically discovers all connections once', 'empty discovery keeps registered connection paths selectable', 'assistant route refresh recovers from empty discovery', 'assistant all-connections refresh discovers every route', 'both pickers share one selected device and route', 'capture selection and route survive reload', 'not-ready queues exact Chinese multiline draft', 'context updates preserve submitted snapshot', 'reload reuses native session', 'one HTTP command per click', 'button snapshot overrides stale assistant Bridge and later page selection', 'manual chat binds selected Bridge or local route', 'unselected page clears old Bridge', 'selected refresh only probes selected route', 'partial route failure preserves healthy selected device', 'same serial remains distinct by route', 'timeout means unconfirmed', 'capture settings select B or local device without stale A diagnostics', 'other workspaces retain generic Bridge selection and diagnostics'], createCount, restoreCount, commands: commands.map(command => ({ command_id: command.command_id, bytes: Buffer.byteLength(command.command) })), pageErrors, blockedWrites, realAgentRequests: 0, realDeviceActions: 0 }
  await fs.writeFile(path.join(out, 'report.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify(report))
} finally {
  await fs.writeFile(path.join(out, 'last-state.json'), JSON.stringify({requests, discoveries, pageErrors, body: await page.locator('body').innerText()}, null, 2)).catch(() => {})
  closing = true
  createGate.resolve()
  contextGate?.resolve()
  await browser.close()
}
