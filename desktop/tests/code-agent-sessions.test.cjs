'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const {CodeAgentSessionStore, redact} = require('../runtime/code-agent-sessions.cjs');

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-code-agent-session-'));
  const home = path.join(root, 'home');
  const project = path.join(home, '.cac', 'projects', 'C--Users-demo');
  const userData = path.join(root, 'user-data');
  const skills = path.join(root, 'skills');
  fs.mkdirSync(project, {recursive: true});
  fs.mkdirSync(userData, {recursive: true});
  fs.mkdirSync(path.join(skills, 'heterogeneous-workbench-tools'), {recursive: true});
  fs.writeFileSync(path.join(skills, 'heterogeneous-workbench-tools', 'SKILL.md'), '# tools\n');
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  return {root, home, project, userData, skills};
}

test('Code Agent Session catalog auto-discovers CAC projects and parses a bounded linked timeline', async t => {
  const value = fixture(t);
  const events = [
    {type: 'user', uuid: 'u-1', timestamp: '2026-10-31T10:00:00+08:00', sessionId: 'session-1',
      cwd: 'C:\\Users\\demo', entrypoint: 'cli', permissionMode: 'auto', version: '1.2.3',
      message: {role: 'user', content: [{type: 'text', text: '分析采样计划'}]}},
    {type: 'assistant', uuid: 'a-1', parentUuid: 'u-1', timestamp: '2026-10-31T10:00:01+08:00',
      message: {role: 'assistant', content: [
        {type: 'thinking', thinking: 'private reasoning must not be returned'},
        {type: 'tool_use', id: 'tool-1', name: 'capture_update_plan',
          input: {description: '更新计划', token: 'secret-value'}},
      ]}},
    {type: 'user', uuid: 'u-2', parentUuid: 'a-1', timestamp: '2026-10-31T10:00:03+08:00',
      message: {role: 'user', content: [{type: 'tool_result', tool_use_id: 'tool-1',
        content: 'Authorization: Bearer hidden-value\nupdated', is_error: false}]}},
    {type: 'assistant', uuid: 'a-2', parentUuid: 'u-2', timestamp: '2026-10-31T10:00:04+08:00',
      message: {role: 'assistant', content: [{type: 'tool_use', id: 'tool-2', name: 'Bash',
        input: {command: 'type C:\\skills\\heterogeneous-workbench-tools\\SKILL.md'}}]}},
  ];
  fs.writeFileSync(path.join(value.project, 'session-1.jsonl'), events.map(event => JSON.stringify(event)).join('\n') + '\n');
  const run = {
    get({sessionId}) {
      if (sessionId !== 'session-1') throw new Error('missing');
      return {session_id: sessionId, cwd: 'C:\\Users\\demo', operations: [{
        operation_id: 'capture-1', kind: 'capture_tool', method: 'capture_update_plan',
        state: 'completed', duration_ms: 25, duration_scope: 'broker_wall', queue_wait_ms: 0,
        transport_duration_ms: 18, channel_acquire_duration_ms: 2,
        channel_startup_duration_ms: 400, channel_reused: false,
      }]};
    },
    list() { return {runs: []}; },
  };
  const store = new CodeAgentSessionStore({
    userDataDir: value.userData,
    homeDir: value.home,
    agentRuns: run,
    integration: () => ({skillRoot: value.skills, tools: ['capture_update_plan', 'remote_status']}),
  });
  const catalog = await store.list();
  assert.equal(catalog.root.available, true);
  assert.equal(catalog.root.configured, false);
  assert.equal(catalog.sessions.length, 1);
  assert.equal(catalog.sessions[0].title, '分析采样计划');
  assert.equal(catalog.assets.skills[0].skill_id, 'heterogeneous-workbench-tools');

  const detail = await store.get({sourceId: catalog.sessions[0].source_id});
  assert.equal(detail.metrics.event_count, 4);
  assert.equal(detail.metrics.tool_call_count, 2);
  assert.equal(detail.metrics.tool_error_count, 0);
  assert.equal(detail.metrics.thinking_block_count, 1);
  assert.equal(detail.metrics.duration_ms, 4000);
  assert.equal(detail.timeline[1].blocks[0].redacted, true);
  assert.equal(Object.hasOwn(detail.timeline[1].blocks[0], 'thinking'), false);
  const platformTool = detail.tools.find(tool => tool.name === 'capture_update_plan');
  assert.equal(platformTool.platform_tool, true);
  assert.equal(platformTool.call_count, 1);
  assert.equal(platformTool.total_duration_ms, 2000);
  assert.equal(platformTool.total_observed_duration_ms, 2000);
  const result = detail.timeline[1].blocks.find(block => block.type === 'tool_use').result;
  assert.equal(detail.timeline[1].blocks.find(block => block.type === 'tool_use').observed_duration_ms, 2000);
  assert.match(result.preview, /\[REDACTED\]/);
  assert.doesNotMatch(JSON.stringify(detail), /private reasoning must not be returned|hidden-value|secret-value/);
  assert.equal(detail.skills[0].loaded, true);
  assert.equal(detail.platform_run.match, 'session_id');
  assert.equal(detail.platform_timing.operation_count, 1);
  assert.equal(detail.platform_timing.operations[0].duration_scope, 'broker_wall');
  assert.equal(detail.platform_timing.operations[0].duration_ms, 25);
  assert.equal(detail.platform_timing.operations[0].transport_duration_ms, 18);
  assert.equal(detail.platform_timing.operations[0].channel_acquire_duration_ms, 2);
  assert.equal(detail.platform_timing.operations[0].channel_startup_duration_ms, 400);
  assert.equal(detail.platform_timing.max_channel_startup_duration_ms, 400);
  assert.equal(detail.timing_semantics.additive, false);
});

test('Code Agent Session root configuration is persisted and source ids cannot escape it', async t => {
  const value = fixture(t);
  const custom = path.join(value.root, 'custom-sessions');
  fs.mkdirSync(custom);
  fs.writeFileSync(path.join(custom, 'custom.jsonl'), JSON.stringify({
    type: 'text', uuid: 'text-1', timestamp: '2026-10-31T01:00:00Z', text: 'hello',
  }) + '\n');
  const first = new CodeAgentSessionStore({userDataDir: value.userData, homeDir: value.home});
  const configured = await first.configure({rootPath: custom});
  assert.equal(configured.root.configured, true);
  assert.equal(configured.sessions.length, 1);
  const restored = new CodeAgentSessionStore({userDataDir: value.userData, homeDir: value.home});
  assert.equal((await restored.list()).root.path, custom);
  await assert.rejects(() => restored.get({sourceId: Buffer.from('../outside.jsonl').toString('base64url')}), /不存在|越界/);
  await assert.rejects(() => first.configure({rootPath: 'relative/path'}), /绝对路径/);
});

test('Code Agent Session parser reports malformed lines without exposing unbounded records', async t => {
  const value = fixture(t);
  fs.writeFileSync(path.join(value.project, 'broken.jsonl'), [
    '{not json',
    JSON.stringify({type: 'thinking', uuid: 'thinking-1', timestamp: '2026-10-31T01:00:00Z', thinking: 'hidden'}),
    JSON.stringify({type: 'tool_result', uuid: 'result-1', timestamp: '2026-10-31T01:00:01Z',
      tool_use_id: 'unknown', content: 'orphan', is_error: true}),
  ].join('\n') + '\n');
  const store = new CodeAgentSessionStore({userDataDir: value.userData, homeDir: value.home});
  const catalog = await store.list();
  const detail = await store.get({sourceId: catalog.sessions[0].source_id});
  assert.equal(detail.metrics.parse_error_count, 1);
  assert.equal(detail.metrics.thinking_character_count, 6);
  assert.equal(detail.timeline.find(event => event.event_id === 'result-1').blocks[0].type, 'tool_result');
  assert.equal(detail.privacy.thinking_text_exposed, false);
});

test('Code Agent Session parser accepts top-level content arrays as well as message.content blocks', async t => {
  const value = fixture(t);
  fs.writeFileSync(path.join(value.project, 'content-shapes.jsonl'), [
    JSON.stringify({type: 'assistant', uuid: 'top-1', timestamp: '2026-10-31T01:00:00Z',
      content: [{type: 'text', text: 'top-level text'}, {type: 'tool_use', id: 'top-tool', name: 'Bash',
        input: {description: 'top-level tool'}}]}),
    JSON.stringify({type: 'user', uuid: 'nested-1', parentUuid: 'top-1', timestamp: '2026-10-31T01:00:01Z',
      message: {role: 'user', content: [{type: 'tool_result', tool_use_id: 'top-tool', content: 'done'}]}}),
  ].join('\n') + '\n');
  const store = new CodeAgentSessionStore({userDataDir: value.userData, homeDir: value.home});
  const catalog = await store.list();
  const source = catalog.sessions.find(item => item.session_id === 'content-shapes');
  const detail = await store.get({sourceId: source.source_id});
  assert.equal(detail.timeline[0].blocks[0].preview, 'top-level text');
  assert.equal(detail.timeline[0].blocks[1].name, 'Bash');
  assert.equal(detail.timeline[0].blocks[1].result.preview, 'done');
});

test('Code Agent Session redaction covers CLI credentials and unfinished key blocks', () => {
  const value = redact('run --api-key secret-value\n-----BEGIN PRIVATE KEY-----\nraw-key-material');
  assert.doesNotMatch(value, /secret-value|raw-key-material/);
  assert.match(value, /--api-key \[REDACTED\]/);
  assert.match(value, /\[REDACTED PRIVATE KEY\]/);
});
