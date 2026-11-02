// Invoke the real button handler with local fixture state, without mounting a UI or Agent.
import ts from 'typescript'
import vm from 'node:vm'
import { readFileSync } from 'node:fs'
const input = JSON.parse(readFileSync(0, 'utf8'))
const filename = new URL('../../app/components/ScenarioCaptureStudio.tsx', import.meta.url)
const source = ts.createSourceFile(filename.pathname, readFileSync(filename, 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
let handler
function find(node) {
  if (ts.isFunctionDeclaration(node) && node.name?.text === 'askAgent') handler = node
  ts.forEachChild(node, find)
}
find(source)
if (!handler) throw new Error('Scenario capture button handler missing')
let captured
const code = ts.transpileModule(handler.getText(source), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
vm.runInNewContext(code + '\naskAgent()', {
  draft: input.draft, profileJson: JSON.stringify(input.draft.profile), plans: [],
  agentKnownPlans: {}, agentDraftSnapshot: {}, liveDraftSnapshot: { current: 'fixture' }, agentRequest: {},
  setAgentWatching() {}, setNotice() {}, record: value => value,
  context: { page_id: 'scenario_capture', connection_mode: 'auto' },
  deviceChoice: { current: input.selected_device ?? null }, captureTarget: value => value, errorText: String,
  dispatchParallelAgent(kind, context, message) { captured = { kind, context, message }; return 'fixture-request' },
})
if (!captured) throw new Error('Button did not dispatch')
process.stdout.write(JSON.stringify(captured))
