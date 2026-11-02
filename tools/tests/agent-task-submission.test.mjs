import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

const source = await readFile(new URL('../../app/lib/agent-task-submission.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const { waitForAgentTarget, enqueueAgentSubmission, parallelAgentTaskDetail, dispatchDeferredAgentTask, cancelAgentTaskSubmission, assertAgentTaskSubmissionActive, runGuardedAgentSubmission } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)
const delay = ms => new Promise(resolve => setTimeout(resolve, ms))

test('page task waits for initialization and preserves the full Chinese multiline payload', async () => {
  let target = null
  const text = '采集第七步，只分析当前操作。\n' + '中文场景 / BuildItem / PMU\n'.repeat(8000)
  const delivered = []
  const pending = waitForAgentTarget(() => ({ target }), { intervalMs: 1, timeoutMs: 1000 })
    .then(shell => shell.sendCommand(text))
  await delay(5)
  assert.deepEqual(delivered, [])
  target = { sendCommand: async command => delivered.push(command) }
  await pending
  assert.deepEqual(delivered, [text])
})

test('unavailable, timeout and disposed pages never submit', async () => {
  await assert.rejects(waitForAgentTarget(() => ({ target: null, error: 'adapter unavailable' })), /adapter unavailable/)
  await assert.rejects(waitForAgentTarget(() => ({ target: null }), { timeoutMs: 2, intervalMs: 1 }), /超时/)
  const controller = new AbortController()
  controller.abort()
  await assert.rejects(waitForAgentTarget(() => ({ target: {} }), { signal: controller.signal }), /未发送/)
})

test('context and command stay ordered; ack follows completed transport, not queue entry', async () => {
  const chain = { current: Promise.resolve() }
  const observed = []
  let finishFirst
  const delivered = new Promise(resolve => { finishFirst = resolve })
  const first = enqueueAgentSubmission(chain, async () => {
    observed.push('context:first')
    await delivered
    observed.push('submit:first', 'ack:first')
  })
  const second = enqueueAgentSubmission(chain, async () => {
    observed.push('context:second', 'submit:second', 'ack:second')
  })
  await delay(1)
  assert.deepEqual(observed, ['context:first'])
  finishFirst()
  await Promise.all([first, second])
  assert.deepEqual(observed, ['context:first', 'submit:first', 'ack:first', 'context:second', 'submit:second', 'ack:second'])
})

test('failed submission rejects the caller and does not poison the next input', async () => {
  const chain = { current: Promise.resolve() }
  const acks = []
  await assert.rejects(enqueueAgentSubmission(chain, async () => { throw Error('write failed') }), /write failed/)
  await enqueueAgentSubmission(chain, async () => acks.push('next'))
  assert.deepEqual(acks, ['next'])
})

test('parallel tasks use the shared skill contract and new views need no invented skill', () => {
  for (const [kind, expected] of [['scenario_capture', 'scenario-step-capture'], ['parallel_modeling', 'builditem-parallel-modeling'], ['workload_features', ''], ['optimization_calibration', '']]) {
    const detail = parallelAgentTaskDetail(kind, { dataset_id: 'ds_current' }, '任务\n第二行', 'request')
    assert.equal(detail.required_skill_id, expected)
    assert.equal(detail.task_kind, kind)
    assert.equal(detail.dataset_id, 'ds_current')
    assert.equal(detail.message, '任务\n第二行')
  }
  assert.equal(parallelAgentTaskDetail('scenario_capture', { required_skill_id: '' }, 'x', 'r').required_skill_id, '')
  assert.equal(parallelAgentTaskDetail('workload_features', { skill_id: 'team-skill' }, 'x', 'r').required_skill_id, 'team-skill')
})

test('caller can save request ID before immediate receiver rejection', async () => {
  const target = new EventTarget()
  let savedId = ''
  const matches = []
  target.addEventListener('page-agent-autonomous-task', event => matches.push(event.detail.request_id === savedId))
  dispatchDeferredAgentTask(target, { request_id: 'request' })
  savedId = 'request'
  await delay(0)
  assert.deepEqual(matches, [true])
})

test('revoked page task is dropped before event dispatch', async () => {
  const target = new EventTarget()
  let received = false
  target.addEventListener('page-agent-autonomous-task', () => { received = true })
  dispatchDeferredAgentTask(target, { request_id: 'revoked-before-dispatch' })
  cancelAgentTaskSubmission('revoked-before-dispatch')
  await delay(0)
  assert.equal(received, false)
})

test('revoked queued task cannot send after its context finishes syncing', async () => {
  let release
  const gate = new Promise(resolve => { release = resolve })
  let sent = false
  const pending = runGuardedAgentSubmission({
    assertCurrent: () => assertAgentTaskSubmissionActive('revoked-during-context'),
    ensureSession: async () => ({}),
    syncContext: async () => { await gate },
    sendCommand: async () => { sent = true },
  })
  await delay(0)
  cancelAgentTaskSubmission('revoked-during-context')
  release()
  await assert.rejects(pending, /已撤销/)
  assert.equal(sent, false)
  assert.doesNotThrow(() => assertAgentTaskSubmissionActive('unrelated-task'))
})
