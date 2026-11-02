import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

async function compile(path) {
  const source = await readFile(new URL(path, import.meta.url), 'utf8')
  return ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
}
const asModule = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const budget = asModule(await compile('../../app/lib/token-budget.ts'))
const compiled = (await compile('../../app/lib/device-validation-task.ts')).replace(/from ['"]\.\/token-budget['"]/, `from '${budget}'`)
const { buildDeviceValidationTask } = await import(asModule(compiled))

test('device task uses discovery/probe as the remote branch and delegates optional leases', () => {
  const task = buildDeviceValidationTask({
    codeUnit: { artifact_id: 'art_code', artifact_type: 'code_unit', code_language: 'cpp', code: 'int example() { return 1; }' },
    strategyArtifact: { artifact_id: 'art_strategy', artifact_type: 'strategy', result: {} },
    strategyLabel: 'CPU 并行', selectedUnit: 'parallel_cpu', selectedUnitLabel: 'CPU 并行', probe: null, selectedSerial: '',
  })
  const metadata = JSON.parse(task.modelMessage.match(/```json\n([\s\S]*?)\n```/)[1])
  assert.deepEqual(metadata.execution_graph.remote_device_branch, ['device.list_bridges', 'device.discover', 'device.probe'])
  assert.deepEqual(metadata.execution_graph.local_device_branch, ['device.discover', 'device.probe'])
  assert.match(metadata.execution_graph.remote_lease_policy, /确认 bridge_id 与 logical_device_id/)
  assert.match(metadata.execution_graph.remote_lease_policy, /device_lease_id 可省略/)
  assert.match(metadata.execution_graph.remote_lease_policy, /自动创建或复用/)
  assert.match(task.modelMessage, /device.create_lease.*可选兼容接口/)
  assert.match(task.modelMessage, /多个候选先确认目标/)
  assert.match(task.modelMessage, /任务状态未知.*先对账/)
  assert.match(task.modelMessage, /不循环换租/)
  assert.equal(metadata.device.serial, null)
  assert.match(task.modelMessage, /int example\(\) \{ return 1; \}/)
})
