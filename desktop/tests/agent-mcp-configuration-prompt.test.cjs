'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {TOOLS} = require('../runtime/agent-tools.cjs');
const {
  AGENT_MCP_CONFIGURATION_PROMPT,
} = require('../renderer/agent-mcp-configuration-prompt.cjs');

test('MCP configuration prompt discovers the real Agent loader instead of guessing flags', () => {
  for (const evidence of ['产品', '版本', '可执行', '--help', '配置 Schema', '已有配置文件'])
    assert.match(AGENT_MCP_CONFIGURATION_PROMPT, new RegExp(evidence));

  assert.match(AGENT_MCP_CONFIGURATION_PROMPT, /原生 MCP (?:配置|加载)机制/);
  assert.match(AGENT_MCP_CONFIGURATION_PROMPT, /不猜测参数名称/);
  assert.match(AGENT_MCP_CONFIGURATION_PROMPT, /官方(?:命令|支持)/);
});

test('MCP configuration prompt validates the generated desktop-ssh descriptor without editing it', () => {
  for (const variable of ['HETERO_AGENT_MCP_CONFIG', 'HETERO_DESKTOP_MCP_CONFIG'])
    assert.match(AGENT_MCP_CONFIGURATION_PROMPT, new RegExp(`\\b${variable}\\b`));

  for (const contract of [
    /文件存在/,
    /JSON (?:有效|合法)/,
    /mcpServers\.desktop-ssh/,
    /command、args (?:和|与) env/,
  ]) assert.match(AGENT_MCP_CONFIGURATION_PROMPT, contract);

  assert.match(AGENT_MCP_CONFIGURATION_PROMPT, /不修改.*(?:自动生成|生成).*integration\/mcp\.json/);
  assert.match(AGENT_MCP_CONFIGURATION_PROMPT, /(?:重新生成|覆盖)/);
});

test('MCP configuration prompt covers supported loader strategies in a deterministic order', () => {
  const externalConfig = AGENT_MCP_CONFIGURATION_PROMPT.search(/外部配置文件/);
  const registerCommand = AGENT_MCP_CONFIGURATION_PROMPT.search(/注册命令/);
  const fixedConfig = AGENT_MCP_CONFIGURATION_PROMPT.search(/固定的.*配置文件/);
  const launchArgs = AGENT_MCP_CONFIGURATION_PROMPT.search(/启动参数/);
  for (const [label, index] of Object.entries({externalConfig, registerCommand, fixedConfig, launchArgs}))
    assert.ok(index >= 0, `${label} loader strategy must be included`);
  assert.ok(externalConfig < registerCommand && registerCommand < fixedConfig && fixedConfig < launchArgs,
    'loader strategies must follow the documented priority order');

  assert.match(AGENT_MCP_CONFIGURATION_PROMPT, /保留原有 MCP Server/);
  assert.match(AGENT_MCP_CONFIGURATION_PROMPT, /(?:修改前|合并前).*备份/);
  assert.match(AGENT_MCP_CONFIGURATION_PROMPT, /\{mcp_config\}/);
});

test('MCP configuration prompt preserves dynamic broker credentials as inherited environment only', () => {
  for (const variable of [
    'HETERO_DESKTOP_BROKER_TOKEN',
    'HETERO_DESKTOP_BROKER_URL',
    'HETERO_AGENT_SESSION_ID',
  ]) assert.match(AGENT_MCP_CONFIGURATION_PROMPT, new RegExp(`\\b${variable}\\b`));

  assert.match(AGENT_MCP_CONFIGURATION_PROMPT, /继承.*(?:父 Agent|当前 Agent).*环境/);
  assert.match(AGENT_MCP_CONFIGURATION_PROMPT, /不要.*(?:写入|持久化).*(?:令牌|Token|TOKEN|Broker URL|会话)/);
  assert.match(AGENT_MCP_CONFIGURATION_PROMPT, /(?:动态注入|每次启动时.*注入)/);

  // A copyable contract may name secret variables, but must never embed a live endpoint,
  // credential, deployment identity, or host-specific path.
  assert.doesNotMatch(AGENT_MCP_CONFIGURATION_PROMPT, /(?:https?|wss?):\/\//i);
  assert.doesNotMatch(AGENT_MCP_CONFIGURATION_PROMPT, /(?:[A-Za-z]:\\|\/(?:home|Users|srv|opt|var)\/)/);
  assert.doesNotMatch(AGENT_MCP_CONFIGURATION_PROMPT, /\b(?:\d{1,3}\.){3}\d{1,3}\b/);
  assert.doesNotMatch(AGENT_MCP_CONFIGURATION_PROMPT, /(?:BEGIN [A-Z ]*PRIVATE KEY|Bearer\s+[A-Za-z0-9._-]+)/);
});

test('MCP configuration prompt proves native progressive discovery without accepting the CLI fallback', () => {
  const toolNames = new Set(TOOLS.map(tool => tool.name));
  for (const name of ['desktop_tool_describe', 'remote_status', 'capture_catalog']) {
    assert.ok(toolNames.has(name), `${name} must exist in the shipped Desktop tool contract`);
    assert.match(AGENT_MCP_CONFIGURATION_PROMPT, new RegExp(`\\b${name}\\b`));
  }

  assert.match(AGENT_MCP_CONFIGURATION_PROMPT, /原生 MCP.*工具(?:管理界面|列表)/s);
  assert.match(AGENT_MCP_CONFIGURATION_PROMPT, /本轮验证.*不要使用.*HETERO_DESKTOP_TOOLS.*CLI/s);
  assert.match(AGENT_MCP_CONFIGURATION_PROMPT, /只读/);
  assert.match(AGENT_MCP_CONFIGURATION_PROMPT, /不要执行会返回大型模板的 capture_catalog/);
  assert.doesNotMatch(AGENT_MCP_CONFIGURATION_PROMPT, /(?:调用|执行).*capture_save_plan/);
});

test('MCP configuration prompt treats restart as a validation boundary and requests an auditable report', () => {
  assert.match(AGENT_MCP_CONFIGURATION_PROMPT, /只能在 Agent 重启后加载/);
  assert.match(AGENT_MCP_CONFIGURATION_PROMPT, /先完成配置/);
  assert.match(AGENT_MCP_CONFIGURATION_PROMPT, /明确告诉.*需要重启/);
  assert.match(AGENT_MCP_CONFIGURATION_PROMPT, /重启后的验证步骤/);
  assert.match(AGENT_MCP_CONFIGURATION_PROMPT, /不把.*配置已写入.*误报.*原生 MCP 已验证/);

  for (const field of [
    '检测到的 Agent 与版本',
    '原生 MCP 加载机制',
    '实际修改的配置',
    '是否需要重启',
    'desktop-ssh 注册状态',
    '原生 remote_status 验证结果',
    '原生渐进式工具发现与 capture_catalog 合同读取结果',
    '仍需我在异构优化工作台中填写的启动参数',
  ]) assert.match(AGENT_MCP_CONFIGURATION_PROMPT, new RegExp(field.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));

  assert.ok(AGENT_MCP_CONFIGURATION_PROMPT.length < 6000,
    'the copyable MCP setup prompt should remain practical for an interactive Agent');
});
