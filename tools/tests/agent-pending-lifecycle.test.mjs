import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

const read = name => readFileSync(new URL(`../../${name}`, import.meta.url), 'utf8')
const compile = source => ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const helpers = await import(`data:text/javascript;base64,${Buffer.from(compile(read('app/lib/agent-task-submission.ts'))).toString('base64')}`)
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
const tick = () => new Promise(resolve => setTimeout(resolve, 0))

function findNode(file, predicate) {
  const source = ts.createSourceFile(file, read(file), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  let found
  function visit(node) { if (!found && predicate(node)) found = node; ts.forEachChild(node, visit) }
  visit(source)
  assert.ok(found, `production callback missing: ${file}`)
  return found
}
function callback(node, bindings) {
  const context = vm.createContext(bindings)
  vm.runInContext(compile(`globalThis.invoke = ${node.getText()}`), context)
  return context.invoke
}

const submitNode = findNode('app/components/KimiWebShell.tsx', node => ts.isVariableDeclaration(node) && node.name.getText() === 'submitCommand').initializer.arguments[0]
function fixture() {
  const session = { adapter_id: 'internal-a', terminal_session_id: 'pty-a', agent_display_name: 'Internal A', state: 'running' }
  const events = []
  const bindings = {
    ...helpers,
    randomUuid: () => 'fixture-request', adapterDisplayName: 'Internal A',
    submissionTargetRef: { current: { adapterId: 'internal-a', generation: 0, disposed: false } },
    sessionRef: { current: session }, inputChainRef: { current: Promise.resolve() },
    ensureActiveSession: async () => { events.push('ensure'); return bindings.sessionRef.current },
    syncContext: async (_id, options) => { options.assertCurrent(); events.push('context') },
    sendAgentCliCommand: async (id, command, commandId) => events.push({ id, command, commandId }),
    onCommandSubmitted: () => events.push('accepted'),
    onStateChange: state => events.push(state),
  }
  return { bindings, events, submit: callback(submitNode, bindings) }
}

test('pending target binds first initialization and rejects switch even if adapter switches back', async () => {
  let target = { generation: 0, adapterId: '', sessionId: '' }
  const guard = helpers.createAgentSubmissionGuard(() => target)
  guard()
  target = { ...target, adapterId: 'internal-a', sessionId: 'pty-a' }; guard()
  const pending = helpers.waitForAgentTarget(() => { guard(); return { target: null } }, { intervalMs: 1, timeoutMs: 100 })
  target = { ...target, generation: 2 }
  await assert.rejects(pending, /会话已切换/)
})

test('real submit callback rejects obsolete queued work before opening a session', async () => {
  for (const change of ['adapter', 'unmount', 'restart']) {
    const { bindings, events, submit } = fixture()
    const queued = deferred(); bindings.inputChainRef.current = queued.promise
    const pending = submit('未发出的中文任务')
    if (change === 'adapter') bindings.submissionTargetRef.current.adapterId = 'internal-b'
    if (change === 'unmount') bindings.submissionTargetRef.current.disposed = true
    if (change === 'restart') bindings.submissionTargetRef.current.generation++
    queued.resolve()
    await assert.rejects(pending, /未发送/)
    assert.ok(!events.includes('ensure'), change)
    assert.ok(!events.includes('accepted'), change)
    assert.ok(!events.some(event => typeof event === 'object'), change)
  }
})

test('target invalidation after session ensure or context sync prevents command delivery', async () => {
  for (const phase of ['ensure', 'context']) {
    const { bindings, events } = fixture()
    const reached = deferred(), finish = deferred()
    if (phase === 'ensure') bindings.ensureActiveSession = async () => { reached.resolve(); await finish.promise; return bindings.sessionRef.current }
    else bindings.syncContext = async () => { reached.resolve(); await finish.promise }
    const submit = callback(submitNode, bindings)
    const pending = submit('等待中的任务')
    await reached.promise
    bindings.sessionRef.current = { ...bindings.sessionRef.current, terminal_session_id: 'pty-replaced' }
    finish.resolve()
    await assert.rejects(pending, /会话已切换/)
    assert.ok(!events.includes('accepted'))
    assert.ok(!events.some(event => typeof event === 'object'))
  }
})

test('upstream page lifetime guard remains effective inside the actual input queue', async () => {
  const { bindings, events, submit } = fixture()
  let closed = false
  const assertCurrent = helpers.createAgentSubmissionGuard(() => ({ generation: 0, disposed: closed }))
  const queued = deferred(); bindings.inputChainRef.current = queued.promise
  const pending = submit('已关闭页面的排队任务', { assertCurrent })
  closed = true; queued.resolve()
  await assert.rejects(pending, /页面已关闭/)
  assert.ok(!events.includes('ensure'))
})

test('a delivered command is acknowledged even when adapter changes while awaiting its response', async () => {
  const { bindings, events } = fixture()
  const delivered = deferred(), receipt = deferred()
  bindings.sendAgentCliCommand = async (id, command, commandId) => {
    events.push({ id, command, commandId }); delivered.resolve(); await receipt.promise
  }
  const submit = callback(submitNode, bindings)
  const command = '第一行：任务上下文\n' + '中文、多行、🙂\n'.repeat(1000)
  const pending = submit(command, { commandId: 'once' })
  await delivered.promise
  assert.ok(!events.includes('accepted'))
  bindings.submissionTargetRef.current.generation++
  receipt.resolve(); await pending
  assert.deepEqual(events.filter(event => typeof event === 'object'), [{ id: 'pty-a', command, commandId: 'once' }])
  assert.equal(events.filter(event => event === 'accepted').length, 1)
})

test('device selection changed during CLI creation is synchronized before the first reused-session command', async () => {
  const file = 'app/components/KimiWebShell.tsx'
  const connectNode = findNode(file, node => ts.isVariableDeclaration(node) && node.name.getText() === 'connect').initializer.arguments[0]
  const syncNode = findNode(file, node => ts.isVariableDeclaration(node) && node.name.getText() === 'syncContext').initializer.arguments[0]
  const syncInputNode = findNode(file, node => ts.isFunctionDeclaration(node) && node.name?.getText() === 'contextSyncInput')
  const syncKeyNode = findNode(file, node => ts.isFunctionDeclaration(node) && node.name?.getText() === 'contextSyncKey')
  const { bindings } = fixture()
  const created = deferred(), finishCreate = deferred()
  const xterm = { cols: 100, rows: 32, reset() {}, scrollToBottom() {}, focus() {} }
  const session = bindings.sessionRef.current
  const contextA = { device_execution_mode: 'remote_bridge', device_selector: { bridge_id: 'bridge-a', device_serial: 'phone-a' } }
  const contextB = { device_execution_mode: 'local_hdc', device_selector: { bridge_id: '', device_serial: 'phone-b' } }
  const updates = [], commands = []
  let backendContext
  Object.assign(bindings, {
    terminalRef: { current: xterm }, connectedRef: { current: true },
    pageSessionIdRef: { current: 'page_fixture-context-race' }, connectPromiseRef: { current: null },
    sessionRef: { current: null }, requestedRunRestoreRef: { current: null },
    analysisIdRef: { current: undefined }, contextRef: { current: contextA }, configRef: { current: {} },
    disposedRef: { current: false }, outputPollControllerRef: { current: null },
    recoveryActiveRef: { current: false }, recoveryAttemptRef: { current: 0 }, recoveryTimerRef: { current: null },
    outputTailRef: { current: '' }, latestSequenceRef: { current: 0 }, lastBackendSizeRef: { current: '' },
    contextSyncChainRef: { current: Promise.resolve() }, lastContextSyncKeyRef: { current: '' },
    adapterId: 'internal-a',
    normalizedAnalysisId: value => value || undefined,
    contextSyncInput: callback(syncInputNode, {}), contextSyncKey: callback(syncKeyNode, {}),
    readStoredSession: () => null, readStoredPageRunBinding: () => null,
    emitTransportStatus() {}, onSessionReady() {}, onTokenUsage() {}, startUsagePolling() {},
    storeSession() {}, openOutputStream() {}, setReady() {},
    resizeAgentCliSession: async () => {},
    createAgentCliSession: async input => {
      backendContext = structuredClone(input.context)
      created.resolve()
      await finishCreate.promise
      return { terminal: { ...session, latest_sequence: 0 }, run: { run_id: 'run-context-race' } }
    },
    updateAgentCliContext: async (_id, input) => {
      backendContext = structuredClone(input.context)
      updates.push(backendContext)
      return { terminal: session, run: { run_id: 'run-context-race' } }
    },
    sendAgentCliCommand: async (_id, command, commandId) => commands.push({ command, commandId, context: backendContext }),
  })
  bindings.syncContext = callback(syncNode, bindings)
  const connect = callback(connectNode, bindings)
  const connecting = connect()
  await created.promise
  assert.deepEqual(backendContext, contextA)
  bindings.contextRef.current = contextB
  finishCreate.resolve()
  await connecting
  const submit = callback(submitNode, bindings)
  await submit('使用当前选中的设备', { commandId: 'first-after-create' })
  assert.deepEqual(commands[0].context, contextB, 'The command must not use the stale create-request device')
  assert.equal(updates.length, 1)
  await submit('继续本次设备分析', { commandId: 'same-context' })
  assert.equal(updates.length, 1, 'An acknowledged identical context can still be reused')
  assert.equal(commands.length, 2)
})

test('the workload page pending notice keeps the original request locked beyond 30 seconds', async () => {
  const call = findNode('app/components/WorkloadFeaturesStudio.tsx', node => ts.isCallExpression(node)
    && node.expression.getText() === 'window.setInterval' && node.getText().includes('pendingNoticeShown'))
  const watch = { accepted: false, startedAt: 0, requestId: 'original' }
  const notices = []
  const invoke = callback(call.arguments[0], { agentWatch: { current: watch }, Date: { now: () => 45000 },
    setNotice: value => notices.push(value), stopAgentWatch: () => assert.fail('pending request must not unlock'), checkAgentResult: async () => {} })
  invoke(); invoke(); await tick()
  assert.equal(watch.requestId, 'original')
  assert.equal(watch.pendingNoticeShown, true)
  assert.equal(notices.length, 1)
  assert.equal(notices[0].tone, 'info')
})

test('the device page pending notice does not clear its dispatch ID or unlock retry', () => {
  const call = findNode('app/components/DeviceVerificationPanel.tsx', node => ts.isCallExpression(node)
    && node.expression.getText() === 'window.setTimeout' && node.getText().includes('autonomousDispatchRequestRef'))
  const request = { current: 'original' }, timer = { current: 7 }, notices = []
  const invoke = callback(call.arguments[0], { autonomousDispatchRequestRef: request, autonomousDispatchTimeoutRef: timer,
    requestId: 'original', setNotice: value => notices.push(value), clearAutonomousDispatch: () => assert.fail('pending request must not unlock') })
  invoke()
  assert.equal(request.current, 'original')
  assert.equal(timer.current, null)
  assert.equal(notices[0].tone, 'info')
})
