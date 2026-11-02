'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {once} = require('node:events');

const helper = path.resolve(__dirname, '../runtime/agent-tools.cjs');

function collectChild(child) {
  return new Promise((resolve, reject) => {
    let stdout = ''; let stderr = '';
    child.stdout.setEncoding('utf8'); child.stderr.setEncoding('utf8');
    child.stdout.on('data', value => stdout += value);
    child.stderr.on('data', value => stderr += value);
    child.on('error', reject);
    child.on('close', code => resolve({code, stdout, stderr}));
  });
}

async function fixture(t) {
  const requests = [];
  const token = 'workspace-test-token';
  const server = http.createServer((request, response) => {
    let body = '';
    request.setEncoding('utf8');
    request.on('data', value => body += value);
    request.on('end', () => {
      assert.equal(request.headers.authorization, `Bearer ${token}`);
      const payload = JSON.parse(body);
      requests.push(payload);
      const result = payload.method === 'workspace_read'
        ? {path: payload.params.path, content: 'int main() {}\n', revision: 'sha256:read', transport: 'worker'}
        : payload.method === 'workspace_write'
          ? {path: payload.params.path, revision: 'sha256:written', transport: 'worker'}
          : {transport: 'worker'};
      response.writeHead(200, {'Content-Type': 'application/json'});
      response.end(JSON.stringify({result}));
    });
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => new Promise(resolve => server.close(resolve)));
  const env = {...process.env,
    HETERO_DESKTOP_BROKER_URL: `http://127.0.0.1:${server.address().port}/tools`,
    HETERO_DESKTOP_BROKER_TOKEN: token};
  return {env, requests};
}

test('Agent CLI forwards structured workspace reads to the Desktop broker', async t => {
  const f = await fixture(t);
  const child = spawn(process.execPath, [helper, 'workspace_read', JSON.stringify({
    path: 'src/main.cpp', lineStart: 10, lineEnd: 24,
  })], {env: f.env, windowsHide: true});
  const result = await collectChild(child);
  assert.equal(result.code, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), {
    path: 'src/main.cpp', content: 'int main() {}\n', revision: 'sha256:read', transport: 'worker',
  });
  assert.deepEqual(f.requests, [{method: 'workspace_read', params: {
    path: 'src/main.cpp', lineStart: 10, lineEnd: 24,
  }}]);
});

test('Agent CLI exposes summaries first and one complete contract on demand without a broker connection', async () => {
  const child = spawn(process.execPath, [helper, '--list-tools'], {
    env: Object.fromEntries(Object.entries(process.env)
      .filter(([key]) => !['HETERO_DESKTOP_BROKER_URL', 'HETERO_DESKTOP_BROKER_TOKEN'].includes(key))),
    windowsHide: true,
  });
  const result = await collectChild(child);
  assert.equal(result.code, 0, result.stderr);
  const catalog = JSON.parse(result.stdout);
  assert.ok(Buffer.byteLength(result.stdout) < 8 * 1024, 'summary catalog must stay bounded and omit full schemas');
  assert.equal(catalog.schema_version, 'heterogeneous_desktop_agent_tools/v2');
  assert.equal(catalog.mcp_server, 'desktop-ssh');
  assert.equal(catalog.discovery_mode, 'summary_then_describe');
  assert.equal(catalog.contract_lookup.mcp_tool, 'desktop_tool_describe');
  const names = catalog.tools.map(tool => tool.name);
  for (const name of ['desktop_tool_describe', 'remote_status', 'capture_catalog', 'capture_save_plan', 'capture_update_plan', 'workspace_read'])
    assert.ok(names.includes(name), `missing ${name}`);
  assert.equal(catalog.tools.find(tool => tool.name === 'capture_save_plan').inputSchema, undefined);
  assert.match(catalog.tools.find(tool => tool.name === 'capture_save_plan').summary, /保存采集计划/);

  const described = await collectChild(spawn(process.execPath, [helper, '--describe-tool', 'capture_update_plan'], {
    env: Object.fromEntries(Object.entries(process.env)
      .filter(([key]) => !['HETERO_DESKTOP_BROKER_URL', 'HETERO_DESKTOP_BROKER_TOKEN'].includes(key))),
    windowsHide: true,
  }));
  assert.equal(described.code, 0, described.stderr);
  const contract = JSON.parse(described.stdout);
  assert.equal(contract.discovery_mode, 'on_demand_contract');
  assert.equal(contract.tool.name, 'capture_update_plan');
  const updateSchema = contract.tool.inputSchema;
  assert.deepEqual(updateSchema.required, ['planId', 'expectedUpdatedAt', 'requestId']);
  assert.equal(updateSchema.properties.app.minProperties, 1);
  assert.equal(updateSchema.properties.targetUpdates.minProperties, 1);
  assert.equal(updateSchema.properties.stepUpdates.minItems, 1);
  assert.equal(updateSchema.properties.stepUpdates.items.minProperties, 2);
  assert.equal(updateSchema.oneOf[1].properties.confirmUnchanged.const, true);
  assert.ok(updateSchema.oneOf[1].not.anyOf.some(value => value.required.includes('stepUpdates')));
});

test('Agent MCP advertises lightweight tools, describes one full contract locally and preserves revision-safe writes', async t => {
  const f = await fixture(t);
  const child = spawn(process.execPath, [helper, '--mcp'], {env: f.env, windowsHide: true});
  const pending = collectChild(child);
  child.stdin.end([
    JSON.stringify({jsonrpc: '2.0', id: 1, method: 'tools/list'}),
    JSON.stringify({jsonrpc: '2.0', id: 3, method: 'tools/call', params: {
      name: 'desktop_tool_describe', arguments: {name: 'workspace_write'},
    }}),
    JSON.stringify({jsonrpc: '2.0', id: 2, method: 'tools/call', params: {
      name: 'workspace_write', arguments: {
        path: 'src/main.cpp', content: 'updated\n', expectedRevision: 'sha256:read',
        bom: true, lineEnding: 'crlf',
      },
    }}),
    '',
  ].join('\n'));
  const result = await pending;
  assert.equal(result.code, 0, result.stderr);
  const messages = result.stdout.trim().split(/\r?\n/).map(line => JSON.parse(line));
  const tools = messages.find(value => value.id === 1).result.tools;
  assert.ok(Buffer.byteLength(JSON.stringify(tools)) < 8 * 1024, 'MCP discovery must not reintroduce every full schema');
  const names = tools.map(tool => tool.name);
  for (const name of ['workspace_stat', 'workspace_list', 'workspace_read', 'workspace_search', 'workspace_write'])
    assert.ok(names.includes(name), `missing ${name}`);
  assert.ok(names.includes('desktop_tool_describe'));
  assert.equal(tools.find(tool => tool.name === 'remote_exec').inputSchema.additionalProperties, true);
  const write = tools.find(tool => tool.name === 'workspace_write');
  assert.equal(write.inputSchema.required, undefined);
  const described = messages.find(value => value.id === 3).result.structuredContent.tool;
  assert.equal(described.name, 'workspace_write');
  assert.deepEqual(described.inputSchema.required, ['path', 'content']);
  assert.match(described.description, /expectedRevision/);
  assert.deepEqual(described.inputSchema.properties.lineEnding.enum, ['lf', 'crlf', 'cr']);
  assert.match(described.description, /bom.*lineEnding/);
  assert.deepEqual(messages.find(value => value.id === 2).result.structuredContent, {
    path: 'src/main.cpp', revision: 'sha256:written', transport: 'worker',
  });
  assert.deepEqual(f.requests, [{method: 'workspace_write', params: {
    path: 'src/main.cpp', content: 'updated\n', expectedRevision: 'sha256:read',
    bom: true, lineEnding: 'crlf',
  }}]);
});
