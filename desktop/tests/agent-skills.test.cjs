'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const {TOOLS} = require('../runtime/agent-tools.cjs');
const {MANAGED_SKILLS, prepareManagedAgentSkills} = require('../runtime/agent-skills.cjs');

test('current executable Skills are materialized locally and reused by identity', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-agent-skills-'));
  try {
    const options = {userDataDir: directory, resourcesDir: path.resolve(__dirname, '..'),
      resourcesPath: path.join(directory, 'missing-resources'), version: 'test'};
    const first = prepareManagedAgentSkills(options);
    const second = prepareManagedAgentSkills(options);
    assert.equal(second, first);
    assert.equal(fs.statSync(path.join(first, 'application-execution-flow-reconstruction', 'SKILL.md')).isFile(), true);
    assert.equal(fs.statSync(path.join(first, 'heterogeneous-workbench-tools', 'SKILL.md')).isFile(), true);
    assert.equal(fs.statSync(path.join(first, 'dpvs-cvs-performance-analysis', 'SKILL.md')).isFile(), true);
    assert.equal(fs.existsSync(path.join(first, 'experimental-flow-trace-reconstruction')), false);
    assert.equal(fs.existsSync(path.join(first, 'experimental-flow-hiperf-reconstruction')), false);
    const manifest = JSON.parse(fs.readFileSync(path.join(first, 'manifest.json'), 'utf8'));
    assert.equal(manifest.schema_version, 'heterogeneous_desktop_agent_skills/v1');
    assert.ok(manifest.files.some(item => item.path.endsWith('application-flow-scenario-v2.schema.json')));
    assert.ok(manifest.files.some(item => item.path === 'heterogeneous-workbench-tools/SKILL.md'));
    assert.equal(manifest.files.some(item => item.path.includes('experimental-flow-')), false);
    const skillFile = path.join(first, 'application-execution-flow-reconstruction', 'SKILL.md');
    fs.chmodSync(skillFile, 0o600);
    fs.writeFileSync(skillFile, 'tampered skill');
    assert.equal(prepareManagedAgentSkills(options), first);
    assert.notEqual(fs.readFileSync(skillFile, 'utf8'), 'tampered skill');
  } finally { fs.rmSync(directory, {recursive: true, force: true}); }
});

test('workbench tool Skill documents every shipped top-level capability and discovery entry', () => {
  assert.deepEqual([...MANAGED_SKILLS], [
    'heterogeneous-workbench-tools',
    'application-execution-flow-reconstruction',
    'dpvs-cvs-performance-analysis',
  ]);
  const source = fs.readFileSync(path.resolve(__dirname,
    '../../agent-resources/skills/heterogeneous-workbench-tools/SKILL.md'), 'utf8');
  for (const name of [
    'remote_status', 'remote_exec', 'workspace_stat', 'workspace_list', 'workspace_read',
    'workspace_search', 'workspace_write', 'device_discover', 'capture_catalog',
    'capture_status', 'capture_save_plan', 'capture_update_plan', 'capture_start', 'capture_cancel',
    'trace_list', 'trace_detail', 'desktop_agent_run_list', 'desktop_agent_run_read',
    'desktop_recovery_complete',
  ]) {
    assert.ok(TOOLS.some(tool => tool.name === name), `${name} is shipped`);
    assert.match(source, new RegExp(`\\b${name}\\b`), `${name} is documented`);
  }
  for (const variable of [
    'HETERO_DESKTOP_MCP_CONFIG', 'HETERO_AGENT_MCP_CONFIG',
    'HETERO_DESKTOP_TOOLS', 'HETERO_AGENT_SKILL_ROOT',
  ]) assert.match(source, new RegExp(`\\b${variable}\\b`));
  assert.match(source, /--list-tools/);
  assert.match(source, /--describe-tool/);
  assert.match(source, /desktop_tool_describe/);
  assert.match(source, /不批量读取所有 Schema/);
});
