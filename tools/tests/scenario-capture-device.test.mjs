import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

async function moduleUrl(name) {
  let source = await readFile(new URL(`../../app/lib/${name}.ts`, import.meta.url), 'utf8')
  if (name === 'model-context') source = source.replace("'./code-scope'", JSON.stringify(await moduleUrl('code-scope')))
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
  return `data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`
}
const { scenarioCaptureRuntimeBinding: bind, captureTargetKey: key, mergeCaptureDiscovery: merge } = await import(await moduleUrl('scenario-capture-device'))
const { assistantPageContext } = await import(await moduleUrl('model-context'))
const A = { connection_mode: 'bridge', bridge_id: 'old-windows', device_serial: 'SAME_SERIAL' }
const B = { ...A, bridge_id: 'new-windows' }
const local = { ...A, connection_mode: 'local', bridge_id: '' }
const page = target => ({ active_view: 'scenario_capture', scenario_capture: { page_id: 'scenario_capture', selected_device: target, scenario_capture_target: target } })
const ordinary = { device_execution_mode: 'remote_bridge', device_selector: { bridge_id: A.bridge_id }, selected_device_bridge: { bridge_id: A.bridge_id } }

test('selected scenario route overrides stale ordinary assistant Bridge and retains full identity', () => {
  const runtime = { ...ordinary, ...bind(page(B)) }
  assert.deepEqual(runtime.device_selector, B)
  assert.deepEqual(runtime.scenario_capture_target, B)
  assert.equal(runtime.selected_device_bridge.bridge_id, B.bridge_id)
  assert.equal(runtime.scenario_capture_selection_required, false)
  assert.notEqual(key(A), key(B))
  assert.deepEqual({ ...ordinary, ...bind(page(local)) }.device_selector, local)
  assert.equal(bind(page(local)).device_execution_mode, 'local_hdc')
  assert.equal(bind(page(local)).selected_device_bridge, null)
})

test('queued request snapshot wins over a later selection, including an explicit unselected snapshot', () => {
  assert.deepEqual(bind(page(A), { kind: 'scenario_capture', requestContext: page(B).scenario_capture }).device_selector, B)
  const runtime = bind(page(B), { kind: 'scenario_capture', requestContext: page(null).scenario_capture })
  assert.deepEqual(runtime.device_selector, {})
  assert.equal(runtime.scenario_capture_selection_required, true)
})

test('unselected capture page cannot inherit another workspace device; other pages retain ordinary binding', () => {
  const runtime = { ...ordinary, ...bind(page(null)) }
  assert.equal(runtime.device_execution_mode, 'unselected')
  assert.deepEqual(runtime.device_selector, {})
  assert.equal(runtime.selected_device_bridge, null)
  assert.equal('scenario_capture_target' in runtime, false)
  assert.deepEqual(bind({ active_view: 'parallel_modeling' }), {})
  assert.deepEqual(bind(page(B), { kind: 'parallel_modeling' }), {})
})

test('manual chat projection preserves active view and selected route, never resurrects old capture on another page', () => {
  const projected = assistantPageContext(page(B))
  assert.equal(projected.active_view, 'scenario_capture')
  assert.deepEqual(projected.scenario_capture.selected_device, B)
  assert.deepEqual(projected.scenario_capture.scenario_capture_target, B)
  assert.deepEqual(bind(projected).device_selector, B)
  assert.equal('scenario_capture' in assistantPageContext({ ...page(B), active_view: 'parallel_modeling' }), false)
})

test('scoped probe keeps other routes, timeout remains unconfirmed, and a successful empty route is offline', () => {
  const previous = [A, B, local]
  const initial = Object.fromEntries(previous.map(device => [key(device), 'connected']))
  const timeout = merge(previous, initial, [], { diagnostics: [{ ...B, status: 'timed_out' }] }, B)
  assert.equal(timeout.devices.length, 3)
  assert.equal(timeout.states[key(B)], 'unconfirmed')
  assert.equal(timeout.states[key(A)], 'connected')
  assert.equal(timeout.states[key(local)], 'connected')
  const empty = merge(previous, timeout.states, [], { diagnostics: [{ ...B, status: 'empty' }] }, B)
  assert.equal(empty.states[key(B)], 'offline')
  const partial = merge(previous, empty.states, [B], { diagnostics: [{ ...B, status: 'ready' }, { ...A, status: 'timed_out' }, { ...local, status: 'unavailable' }], discovery_complete: false }, null)
  assert.equal(partial.states[key(B)], 'connected')
  assert.equal(partial.states[key(A)], 'unconfirmed')
})
