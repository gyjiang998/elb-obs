'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {TOOLS} = require('../runtime/agent-tools.cjs');
const {
  AGENT_LONG_TERM_MEMORY_PROMPT_VERSION,
  AGENT_LONG_TERM_MEMORY_RULES,
  AGENT_LONG_TERM_MEMORY_PROMPT,
  AGENT_INITIALIZATION_PROMPT,
} = require('../renderer/agent-long-term-memory-prompt.cjs');

test('long-term memory prompt is a complete one-time installation request', () => {
  assert.equal(AGENT_LONG_TERM_MEMORY_PROMPT_VERSION, 4);
  assert.match(AGENT_LONG_TERM_MEMORY_PROMPT, /写入你的用户级长期记忆或全局持久规则/);
  assert.match(AGENT_LONG_TERM_MEMORY_PROMPT, /写入后请回复保存位置和生效范围/);
  assert.match(AGENT_LONG_TERM_MEMORY_PROMPT, /如果你不支持长期记忆/);
  assert.ok(AGENT_LONG_TERM_MEMORY_PROMPT.includes(AGENT_LONG_TERM_MEMORY_RULES));
  assert.ok(AGENT_LONG_TERM_MEMORY_PROMPT.length < 3200, 'copyable prompt should remain concise');
});

test('persistent rules match the shipped Desktop SSH and Workspace tool contract', () => {
  const toolNames = new Set(TOOLS.map(tool => tool.name));
  for (const name of [
    'remote_status',
    'remote_exec',
    'workspace_stat',
    'workspace_list',
    'workspace_read',
    'workspace_search',
    'workspace_write',
  ]) {
    assert.ok(toolNames.has(name), `${name} must exist in the shipped Desktop tool contract`);
    assert.match(AGENT_LONG_TERM_MEMORY_RULES, new RegExp(`\\b${name}\\b`));
  }
  assert.match(AGENT_LONG_TERM_MEMORY_RULES, /最长 120 秒/);
  assert.match(AGENT_LONG_TERM_MEMORY_RULES, /revision、bom、lineEnding/);
  assert.match(AGENT_LONG_TERM_MEMORY_RULES, /create:true/);
  assert.match(AGENT_LONG_TERM_MEMORY_RULES, /不自动重放/);
});

test('initialization is a one-write fast path and defers discovery to real tasks', () => {
  assert.match(AGENT_INITIALIZATION_PROMPT, /快速初始化/);
  assert.match(AGENT_INITIALIZATION_PROMPT, /只做一件事/);
  assert.match(AGENT_INITIALIZATION_PROMPT, /不要在初始化阶段读取 Skill、枚举文件、检查环境变量、发现 MCP\/CLI、读取工具合同或调用任何平台工具/);
  assert.match(AGENT_INITIALIZATION_PROMPT, /首个真实任务中按需验证/);
  assert.match(AGENT_INITIALIZATION_PROMPT, /不能把“尚未验证”表述为“缺失”/);
  assert.match(AGENT_INITIALIZATION_PROMPT, /仅回复：保存位置、生效范围/);
  assert.ok(AGENT_INITIALIZATION_PROMPT.includes(AGENT_LONG_TERM_MEMORY_RULES));
  assert.doesNotMatch(AGENT_INITIALIZATION_PROMPT, /请立即完成.*实际检查结果/);
  assert.doesNotMatch(AGENT_INITIALIZATION_PROMPT, /用实际可用的 MCP 或 CLI 路径读取|确认本机采样能力已登记|完成后按“工具接入方式/);
  assert.ok(AGENT_INITIALIZATION_PROMPT.length < 2400, 'fast initialization should avoid a broad diagnostic task');
});

test('persistent and initialization prompts contain no deployment identity, path, or credential material', () => {
  for (const prompt of [AGENT_LONG_TERM_MEMORY_PROMPT, AGENT_INITIALIZATION_PROMPT]) {
    assert.doesNotMatch(prompt, /HETERO_DESKTOP_BROKER_(?:URL|TOKEN)/);
    assert.doesNotMatch(prompt, /(?:https?|wss?):\/\//i);
    assert.doesNotMatch(prompt, /(?:[A-Za-z]:\\|\/(?:home|Users|srv|opt|var)\/)/);
    assert.doesNotMatch(prompt, /\b(?:\d{1,3}\.){3}\d{1,3}\b/);
    assert.doesNotMatch(prompt, /(?:BEGIN [A-Z ]*PRIVATE KEY|Bearer\s+[A-Za-z0-9._-]+)/);
  }
});

test('Code Agent settings expose an explicit copy action without an automatic startup-context control', () => {
  const html = fs.readFileSync(path.join(__dirname, '../renderer/index.html'), 'utf8');
  const renderer = fs.readFileSync(path.join(__dirname, '../renderer/renderer.js'), 'utf8');
  assert.match(html, /id="copy-agent-memory-prompt"[^>]*>复制系统提示</);
  assert.match(html, /工作台不会向 Agent 对话自动写入准备提示/);
  assert.doesNotMatch(html, /id="agent-bootstrap-context"/);
  assert.match(renderer, /writeClipboard', \{text: AGENT_LONG_TERM_MEMORY_PROMPT\}/);
  assert.doesNotMatch(renderer, /\$\('agent-bootstrap-context'\)/);
});

test('explicit initialization is tagged for Agent session auditing', () => {
  const renderer = fs.readFileSync(path.join(__dirname, '../renderer/renderer.js'), 'utf8');
  assert.match(renderer, /submitAgentTask\(AGENT_INITIALIZATION_PROMPT, \{task_kind: 'agent_initialization'\}\)/);
  assert.match(renderer, /初始化任务已发送（快速模式）/);
});
