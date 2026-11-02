'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { startupConnectionPlan } = require('../renderer/startup-connections.cjs');

function configured(overrides = {}) {
  return {
    settings: {
      ssh: {
        host: 'build.example',
        port: 22,
        username: 'builder',
        cwd: '/srv/workspace',
        auth: 'password',
        passwordCredential: { available: true, saved: true },
      },
      agent: { executable: 'code-agent.exe', args: [], cwd: 'C:\\workspace' },
    },
    ssh: { state: 'disconnected' },
    agent: { state: 'stopped' },
    ...overrides,
  };
}

test('complete saved configuration starts SSH and Code Agent', () => {
  assert.deepEqual(startupConnectionPlan(configured()), {
    missingSsh: [],
    missingAgent: [],
    settingsTab: null,
    connectSsh: true,
    startAgent: true,
  });
});

test('an existing live connection is not restarted during renderer reload', () => {
  const state = configured({ ssh: { state: 'connected' }, agent: { state: 'running' } });
  const plan = startupConnectionPlan(state);
  assert.equal(plan.connectSsh, false);
  assert.equal(plan.startAgent, false);
  assert.equal(plan.settingsTab, null);
});

test('connections already in progress are not replaced during renderer reload', () => {
  const state = configured({ ssh: { state: 'connecting' }, agent: { state: 'starting' } });
  const plan = startupConnectionPlan(state);
  assert.equal(plan.connectSsh, false);
  assert.equal(plan.startAgent, false);
});

test('missing SSH configuration opens the SSH tab while a configured Agent may still start', () => {
  const state = configured();
  state.settings.ssh.host = '';
  state.settings.ssh.passwordCredential.saved = false;
  const plan = startupConnectionPlan(state);
  assert.equal(plan.settingsTab, 'ssh');
  assert.deepEqual(plan.missingSsh, ['服务器地址', '已保存的 SSH 密码']);
  assert.equal(plan.connectSsh, false);
  assert.equal(plan.startAgent, true);
});

test('missing Agent executable opens the Agent tab', () => {
  const state = configured();
  state.settings.agent.executable = '';
  const plan = startupConnectionPlan(state);
  assert.equal(plan.settingsTab, 'agent');
  assert.deepEqual(plan.missingAgent, ['Code Agent 启动命令']);
  assert.equal(plan.connectSsh, true);
  assert.equal(plan.startAgent, false);
});

test('key and system Agent authentication do not require a stored password', () => {
  const key = configured();
  key.settings.ssh = { ...key.settings.ssh, auth: 'key', privateKeyPath: 'C:\\Users\\me\\.ssh\\id_ed25519', passwordCredential: { saved: false } };
  assert.equal(startupConnectionPlan(key).settingsTab, null);

  const agent = configured();
  agent.settings.ssh = { ...agent.settings.ssh, auth: 'agent', privateKeyPath: '', passwordCredential: { saved: false } };
  assert.equal(startupConnectionPlan(agent).settingsTab, null);
});

test('renderer attempts every configured connection before presenting missing configuration', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'renderer.js'), 'utf8');
  const begin = source.indexOf('async function autoStartConnections()');
  const end = source.indexOf('\nasync function submitAgentTask', begin);
  const autoStart = source.slice(begin, end);
  assert.ok(begin >= 0 && end > begin);
  assert.ok(autoStart.indexOf('if (plan.connectSsh)') < autoStart.indexOf('if (plan.startAgent)'));
  assert.ok(autoStart.indexOf('if (plan.startAgent)') < autoStart.indexOf('if (plan.settingsTab)'));
});
