#!/usr/bin/env node
'use strict';

// One stdio MCP / command-line adapter for the desktop's authenticated SSH session.
// It inherits the session token from its parent Agent; secrets are never in argv.
const http = require('node:http');
const readline = require('node:readline');

const MAX_MCP_MESSAGE_BYTES = 4 * 1024 * 1024;
const TOOL_CATALOG_SCHEMA_VERSION = 'heterogeneous_desktop_agent_tools/v2';

const TOOL_GROUP_PREFIXES = [
  ['workspace_', 'remote-workspace'],
  ['capture_', 'capture'],
  ['trace_', 'trace'],
  ['application_flow_', 'application-flow'],
  ['desktop_agent_run_', 'agent-observability'],
  ['remote_', 'remote'],
  ['device_', 'device'],
];

function toolGroup(name) {
  return TOOL_GROUP_PREFIXES.find(([prefix]) => String(name).startsWith(prefix))?.[1] || 'platform';
}

function shortDescription(description, maxLength = 88) {
  const source = String(description || '').replace(/\s+/g, ' ').trim();
  const firstSentence = source.split('。')[0] || source;
  if (firstSentence.length <= maxLength) return firstSentence;
  return `${firstSentence.slice(0, Math.max(1, maxLength - 1)).trimEnd()}…`;
}

function callDesktop(method, params = {}) {
  const endpoint = process.env.HETERO_DESKTOP_BROKER_URL;
  const token = process.env.HETERO_DESKTOP_BROKER_TOKEN;
  if (!endpoint || !token) return Promise.reject(new Error('请从桌面应用启动 Agent，以继承远程工具连接。'));
  const url = new URL(endpoint);
  if (url.hostname !== '127.0.0.1' || url.protocol !== 'http:') return Promise.reject(new Error('桌面工具地址必须为本机 loopback。'));
  const body = JSON.stringify({method, params});
  return new Promise((resolve, reject) => {
    const req = http.request(url, {method: 'POST', headers: {'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body), Authorization: `Bearer ${token}`}}, (res) => {
      const chunks = []; let size = 0;
      res.on('data', chunk => { size += chunk.length; if (size > 8 * 1024 * 1024) { req.destroy(new Error('桌面工具响应超过上限。')); } else chunks.push(chunk); });
      res.on('error', reject);
      res.on('end', () => {
        try { const value = JSON.parse(Buffer.concat(chunks).toString('utf8')); if (res.statusCode !== 200 || value.error) reject(new Error(value.error || `HTTP ${res.statusCode}`)); else resolve(value.result); }
        catch (error) { reject(error); }
      });
    });
    req.setTimeout(10 * 60 * 1000, () => req.destroy(new Error('桌面工具请求超时。')));
    req.on('error', reject); req.end(body);
  });
}

const BROKER_TOOLS = [
  {name: 'remote_status', description: '检查桌面应用当前 SSH 连接、Agent 绑定的服务器及工作目录，以及两者是否一致。', inputSchema: {type: 'object', properties: {}, additionalProperties: false}},
  {name: 'remote_exec', description: '通过桌面应用已认证的 SSH 会话，在本 Agent 绑定的 Linux 服务器与工作目录执行 shell 命令。返回 stdout、stderr、exitCode 和耗时。文件列举、读取、搜索、属性检查和修改优先使用 workspace_* 工具，remote_exec 用于编译、测试和其他命令执行。切换目标后重新启动 Agent 即可建立新绑定。', inputSchema: {type: 'object', properties: {command: {type: 'string'}, timeoutMs: {type: 'integer', minimum: 100, maximum: 120000}}, required: ['command'], additionalProperties: false}},
  {name: 'workspace_stat', description: '通过桌面应用的长驻远程工作区通道，读取绑定工作目录内文件或目录的类型、大小和修改时间。', inputSchema: {type: 'object', properties: {path: {type: 'string', description: '相对于 Agent 绑定的服务器工作目录的路径；空字符串表示根目录。'}}, required: ['path'], additionalProperties: false}},
  {name: 'workspace_list', description: '通过长驻远程工作区通道，列出绑定工作目录内的文件和目录。这是浏览远程源码树的默认工具。', inputSchema: {type: 'object', properties: {path: {type: 'string', description: '相对工作目录路径，默认为根目录。'}, recursive: {type: 'boolean', description: '是否递归列出子目录。'}, maxDepth: {type: 'integer', minimum: 1, maximum: 12}, maxEntries: {type: 'integer', minimum: 1, maximum: 5000}}, additionalProperties: false}},
  {name: 'workspace_read', description: '通过长驻远程工作区通道读取 UTF-8 文件或指定行区间。返回的 revision 始终覆盖完整文件，可用于后续原子写入的冲突检测。', inputSchema: {type: 'object', properties: {path: {type: 'string', description: '相对工作目录文件路径。'}, lineStart: {type: 'integer', minimum: 1, description: '可选的起始行，从 1 开始。'}, lineEnd: {type: 'integer', minimum: 1, description: '可选的结束行，包含该行。'}, maxBytes: {type: 'integer', minimum: 1, maximum: 1048576}}, required: ['path'], additionalProperties: false}},
  {name: 'workspace_search', description: '通过长驻远程工作区通道在绑定工作目录内搜索文本，返回带路径、行号和行内容的有界命中。', inputSchema: {type: 'object', properties: {query: {type: 'string', description: '搜索文本或正则表达式。'}, path: {type: 'string', description: '搜索起点的相对目录，默认为根目录。'}, glob: {type: 'string', description: '可选文件 glob，例如 **/*.cpp。'}, regex: {type: 'boolean'}, caseSensitive: {type: 'boolean'}, maxResults: {type: 'integer', minimum: 1, maximum: 500}}, required: ['query'], additionalProperties: false}},
  {name: 'workspace_write', description: '通过长驻远程工作区通道原子写入 UTF-8 文件。覆盖已有文件必须传 workspace_read 返回的 expectedRevision，并原样传回 bom 与 lineEnding 以保留文件格式；新建文件使用 create:true 且不传 expectedRevision。', inputSchema: {type: 'object', properties: {path: {type: 'string', description: '相对工作目录文件路径。'}, content: {type: 'string'}, expectedRevision: {type: 'string', description: '覆盖时必需的 sha256 revision。'}, create: {type: 'boolean', description: '仅用于新建文件。'}, bom: {type: 'boolean', description: '是否写入 UTF-8 BOM；覆盖时沿用 workspace_read 返回值。'}, lineEnding: {type: 'string', enum: ['lf', 'crlf', 'cr'], description: '换行格式；覆盖时沿用 workspace_read 返回值。'}}, required: ['path', 'content'], allOf: [{if: {properties: {create: {const: true}}, required: ['create']}, then: {}, else: {required: ['expectedRevision', 'bom', 'lineEnding']}}], additionalProperties: false}},
  {name: 'device_discover', description: '通过当前电脑的 HDC 发现手机，保留用户选中的设备。设备选择由 App 页面完成。', inputSchema: {type: 'object', properties: {}, additionalProperties: false}},
  {name: 'capture_catalog', description: '从空白构建采集计划时读取本地模板、操作类型、profile 和默认采集配置。页面局部补全任务已携带紧凑合同，不需要再次调用。', inputSchema: {type: 'object', properties: {}, additionalProperties: false}},
  {name: 'capture_status', description: '读取所选设备、当前采集状态和最近计划/任务/记录摘要；可给出 planId 读取该计划完整配置。历史返回最多30个计划、20个任务、30条Trace及总数。', inputSchema: {type: 'object', properties: {planId: {type: 'string'}}, additionalProperties: false}},
  {name: 'capture_save_plan', description: '校验并保存采集计划，同时生成固定 Runner 可执行的本地采集包。plan 使用 capture_catalog 的模板、steps、profile 和 capture 合同。返回 planId；生成计划后可回到 App 查看与执行。', inputSchema: {type: 'object', properties: {plan: {type: 'object', description: '计划，含title/template/steps/profile/capture，步骤使用固定操作及控件选择器。'}}, required: ['plan'], additionalProperties: false}},
  {name: 'capture_update_plan', description: '负载采样页面的低延迟计划补全工具。按 planId 和 expectedUpdatedAt 在本地合并差量、校验并保存；保留未提交的采样器、PMU 与执行配置。每个页面请求只调用一次；无需先读 catalog/status。若无需修改，使用 confirmUnchanged。', inputSchema: {type: 'object', properties: {
    planId: {type: 'string', minLength: 1, maxLength: 200},
    expectedUpdatedAt: {type: 'string', minLength: 1, maxLength: 200},
    requestId: {type: 'string', minLength: 1, maxLength: 200},
    title: {type: 'string', minLength: 1, maxLength: 200},
    app: {type: 'object', minProperties: 1, properties: {
      bundle: {type: 'string', minLength: 1, maxLength: 300},
      ability: {type: 'string', minLength: 1, maxLength: 300},
    }, additionalProperties: false},
    targetUpdates: {type: 'object', minProperties: 1, maxProperties: 256,
      propertyNames: {pattern: '^[A-Za-z][A-Za-z0-9_-]{0,127}$'},
      description: '按控件名称提交新增或修正后的 selector；值为非空对象或 null。',
      additionalProperties: {anyOf: [{type: 'object', minProperties: 1}, {type: 'null'}]}},
    stepUpdates: {type: 'array', minItems: 1, maxItems: 500,
      description: '只提交发生变化的现有步骤；每项除 step_id 外至少包含一个修改字段。',
      items: {type: 'object', minProperties: 2, properties: {
        step_id: {type: 'string', minLength: 1, maxLength: 200},
        title: {type: 'string', minLength: 1, maxLength: 500},
        slug: {type: 'string', minLength: 1, maxLength: 200},
        operation: {type: 'string', minLength: 1, maxLength: 100},
        target: {anyOf: [{type: 'string', minLength: 1, maxLength: 128}, {type: 'null'}]},
        selector: {anyOf: [{type: 'object', minProperties: 1}, {type: 'null'}]},
        expected_targets: {type: 'array', maxItems: 128, items: {type: 'string', minLength: 1, maxLength: 128}},
        key: {type: 'string', minLength: 1, maxLength: 100}, seconds: {type: 'number'},
        points: {type: 'array', minItems: 2, maxItems: 32, items: {type: 'number'}}, velocity: {type: 'number'},
      }, required: ['step_id'], additionalProperties: false}},
    confirmUnchanged: {type: 'boolean'}, summary: {type: 'string', maxLength: 1000},
  }, required: ['planId', 'expectedUpdatedAt', 'requestId'], additionalProperties: false,
  oneOf: [
    {anyOf: [{required: ['title']}, {required: ['app']}, {required: ['targetUpdates']}, {required: ['stepUpdates']}],
      properties: {confirmUnchanged: {const: false}}},
    {required: ['confirmUnchanged'], properties: {confirmUnchanged: {const: true}},
      not: {anyOf: [{required: ['title']}, {required: ['app']}, {required: ['targetUpdates']}, {required: ['stepUpdates']}]}},
  ]}},
  {name: 'capture_start', description: '在用户当前选中的手机上执行已保存计划；deviceSerial 与页面选择一致。立即返回任务，后续用 capture_status 跟踪 Trace/HiLog/PMU 采集。', inputSchema: {type: 'object', properties: {planId: {type: 'string'}, deviceSerial: {type: 'string'}}, required: ['planId', 'deviceSerial'], additionalProperties: false}},
  {name: 'capture_cancel', description: '取消当前本地采集，Runner 清理本轮采集器并保留已产生的数据。', inputSchema: {type: 'object', properties: {runId: {type: 'string'}}, required: ['runId'], additionalProperties: false}},
  {name: 'trace_list', description: '列出本地 Trace 记录、采集状态及归档位置，可找到供现有 Web 平台导入的 capture.zip。', inputSchema: {type: 'object', properties: {}, additionalProperties: false}},
  {name: 'trace_detail', description: '读取一个本地 Trace 记录的清单、步骤和文件，不读取二进制 Trace 内容。', inputSchema: {type: 'object', properties: {id: {type: 'string'}}, required: ['id'], additionalProperties: false}},
  {name: 'application_flow_list_assets', description: '通过 Desktop 长驻平台通道列出应用流程发现、场景和已登记代码仓；不依赖服务器 Web 服务。', inputSchema: {type: 'object', properties: {source_connector_id: {type: 'string'}, limit: {type: 'integer', minimum: 1, maximum: 500}}, additionalProperties: false}},
  {name: 'application_flow_inspect_repository', description: '在已登记代码仓内有界侦察操作入口和源码候选。', inputSchema: {type: 'object', properties: {source_connector_id: {type: 'string'}, subpath: {type: 'string'}}, required: ['source_connector_id'], additionalProperties: false}},
  {name: 'application_flow_search_source', description: '在已登记代码仓内按固定文本搜索源码并返回行级证据。', inputSchema: {type: 'object', properties: {source_connector_id: {type: 'string'}, query: {type: 'string'}, subpath: {type: 'string'}, max_results: {type: 'integer', minimum: 1, maximum: 100}}, required: ['source_connector_id', 'query'], additionalProperties: false}},
  {name: 'application_flow_read_source', description: '读取代码仓内一个相对路径的有界源码行区间。', inputSchema: {type: 'object', properties: {source_connector_id: {type: 'string'}, path: {type: 'string'}, line_start: {type: 'integer', minimum: 1}, line_end: {type: 'integer', minimum: 1}}, required: ['source_connector_id', 'path'], additionalProperties: false}},
  {name: 'application_flow_inspect_trace_instrumentation', description: '围绕实现位置识别直接 API、包装宏、RAII 与 ArkTS HiTrace 插桩。', inputSchema: {type: 'object', properties: {source_connector_id: {type: 'string'}, path: {type: 'string'}, line: {type: 'integer', minimum: 1}, symbol: {type: 'string'}, radius: {type: 'integer'}, max_results: {type: 'integer'}}, required: ['source_connector_id', 'path'], additionalProperties: false}},
  {name: 'application_flow_read_asset', description: '读取精确的应用流程 discovery 或 scenario 工件。', inputSchema: {type: 'object', properties: {artifact_type: {type: 'string', enum: ['application_flow_discovery', 'application_flow_scenario']}, artifact_id: {type: 'string'}, application_flow_analysis_id: {type: 'string'}}, required: ['artifact_type', 'artifact_id'], additionalProperties: false}},
  {name: 'application_flow_publish_candidates', description: '登记已理解的完整用户操作，建立源码范围、快照、代码底图和分析目标血缘。', inputSchema: {type: 'object', properties: {source_connector_id: {type: 'string'}, summary_zh: {type: 'string'}, candidates: {type: 'array', items: {type: 'object'}}, subpath: {type: 'string'}, unknowns: {type: 'array', items: {type: 'string'}}, analysis_goal: {type: 'string'}, code_graph_id: {type: 'string'}, source_scopes: {type: 'array', items: {type: 'object'}}, parent_discovery_artifact_id: {type: 'string'}, refinement_request: {type: 'string'}, application_flow_analysis_id: {type: 'string'}, trace_analysis_artifact_id: {type: 'string'}}, required: ['source_connector_id', 'summary_zh', 'candidates'], additionalProperties: false}},
  {name: 'application_flow_compile_mermaid', description: '把带应用流程元数据的 Mermaid 编译并保存为标准场景工件。', inputSchema: {type: 'object', properties: {source_connector_id: {type: 'string'}, discovery_artifact_id: {type: 'string'}, flow_id: {type: 'string'}, mermaid: {type: 'string'}, parent_scenario_artifact_id: {type: 'string'}, revision_request: {type: 'string'}, application_flow_analysis_id: {type: 'string'}}, required: ['source_connector_id', 'discovery_artifact_id', 'flow_id', 'mermaid'], additionalProperties: false}},
  {name: 'application_flow_enrich_trace_anchors', description: '提交逐阶段源码 HiTrace start/end 证据并生成增强场景版本。', inputSchema: {type: 'object', properties: {source_connector_id: {type: 'string'}, discovery_artifact_id: {type: 'string'}, scenario_artifact_id: {type: 'string'}, flow_id: {type: 'string'}, stage_results: {type: 'array', items: {type: 'object'}}, application_flow_analysis_id: {type: 'string'}}, required: ['source_connector_id', 'discovery_artifact_id', 'scenario_artifact_id', 'flow_id', 'stage_results'], additionalProperties: false}},
  {name: 'application_flow_inspect_runtime_trace', description: '读取真实 Trace 事件目录并返回流程阶段的有界运行时匹配候选。', inputSchema: {type: 'object', properties: {source_connector_id: {type: 'string'}, analysis_artifact_id: {type: 'string'}, scenario_artifact_id: {type: 'string'}, query: {type: 'string'}, global_limit: {type: 'integer'}, per_stage_limit: {type: 'integer'}, application_flow_analysis_id: {type: 'string'}}, required: ['source_connector_id', 'analysis_artifact_id', 'scenario_artifact_id'], additionalProperties: false}},
  {name: 'application_flow_apply_runtime_trace_alignment', description: '保存真实事件到阶段与活动的语义对齐，并生成带运行时证据的新场景版本。', inputSchema: {type: 'object', properties: {source_connector_id: {type: 'string'}, analysis_artifact_id: {type: 'string'}, event_catalog_artifact_id: {type: 'string'}, scenario_artifact_id: {type: 'string'}, flow_id: {type: 'string'}, mermaid: {type: 'string'}, stage_matches: {type: 'array', items: {type: 'object'}}, node_matches: {type: 'array', items: {type: 'object'}}, change_summary: {type: 'string'}, limitations: {type: 'array', items: {type: 'string'}}, application_flow_analysis_id: {type: 'string'}}, required: ['source_connector_id', 'analysis_artifact_id', 'event_catalog_artifact_id', 'scenario_artifact_id', 'flow_id', 'mermaid', 'stage_matches', 'change_summary'], additionalProperties: false}},
  {name: 'desktop_agent_run_list', description: '列出本机 Desktop Code Agent 的真实历史会话、任务数、首响应时间及各类工具等待耗时。用于经验演进和定位长任务卡点。', inputSchema: {type: 'object', properties: {limit: {type: 'integer', minimum: 1, maximum: 500}}, additionalProperties: false}},
  {name: 'desktop_agent_run_read', description: '读取一条本机 Desktop Code Agent 会话的任务、工具调用、远程等待和错误记录。', inputSchema: {type: 'object', properties: {sessionId: {type: 'string'}}, required: ['sessionId'], additionalProperties: false}},
  {name: 'desktop_recovery_complete', description: '结束当前类型化自动恢复任务。仅在修复和最小验证完成后调用；工作台收到 repaired 握手后才会按冻结参数重试原失败步骤一次。', inputSchema: {
    type: 'object',
    properties: {
      repairRequestId: {type: 'string', minLength: 1, maxLength: 200},
      outcome: {type: 'string', enum: ['repaired', 'unable']},
      summary: {type: 'string', minLength: 1, maxLength: 2000},
    },
    required: ['repairRequestId', 'outcome', 'summary'],
    additionalProperties: false,
  }},
];

const TOOL_DESCRIBE = {
  name: 'desktop_tool_describe',
  description: '普通工具默认只展示摘要；按工具名读取一个工作台工具的完整用途、参数 Schema 与调用方式。',
  inputSchema: {
    type: 'object',
    properties: {name: {type: 'string', minLength: 1, maxLength: 200}},
    required: ['name'],
    additionalProperties: false,
  },
};

const TOOLS = [TOOL_DESCRIBE, ...BROKER_TOOLS];

function findTool(name) {
  return TOOLS.find(tool => tool.name === String(name || '').trim()) || null;
}

function describeTool(name) {
  const tool = findTool(name);
  if (!tool) throw new Error(`未知工具：${String(name || '').trim() || '(空)'}`);
  return {
    schema_version: TOOL_CATALOG_SCHEMA_VERSION,
    discovery_mode: 'on_demand_contract',
    group: toolGroup(tool.name),
    tool,
    invocation: {
      mcp: {server: 'desktop-ssh', tool: tool.name},
      cli: `<HETERO_DESKTOP_TOOLS> ${tool.name} '<JSON参数>'`,
    },
  };
}

function toolCatalogSummary() {
  return {
    schema_version: TOOL_CATALOG_SCHEMA_VERSION,
    mcp_server: 'desktop-ssh',
    discovery_mode: 'summary_then_describe',
    contract_lookup: {
      mcp_tool: TOOL_DESCRIBE.name,
      cli: '--describe-tool <工具名>',
    },
    tools: TOOLS.map(tool => ({
      name: tool.name,
      group: toolGroup(tool.name),
      summary: shortDescription(tool.description),
    })),
  };
}

function mcpAdvertisedTools() {
  return TOOLS.map(tool => tool.name === TOOL_DESCRIBE.name ? tool : ({
    name: tool.name,
    description: shortDescription(tool.description),
    // MCP requires an inputSchema for every advertised tool. The broker remains
    // authoritative for validation; the full contract is fetched only when the
    // task actually needs this tool.
    inputSchema: {
      type: 'object',
      additionalProperties: true,
    },
  }));
}

async function handleRpc(message) {
  const {id, method, params = {}} = message;
  if (id === undefined) return null;
  const answer = result => ({jsonrpc: '2.0', id, result});
  if (method === 'initialize') return answer({protocolVersion: ['2024-11-05', '2025-03-26', '2025-06-18'].includes(params.protocolVersion) ? params.protocolVersion : '2024-11-05', capabilities: {tools: {}}, serverInfo: {name: 'heterogeneous-desktop-ssh', version: '0.1.0'}});
  if (method === 'ping') return answer({});
  if (method === 'tools/list') return answer({tools: mcpAdvertisedTools()});
  if (method === 'tools/call') {
    if (!TOOLS.some(tool => tool.name === params.name)) return {jsonrpc: '2.0', id, error: {code: -32602, message: 'Unknown tool'}};
    try {
      const result = params.name === TOOL_DESCRIBE.name
        ? describeTool(params.arguments?.name)
        : await callDesktop(params.name, params.arguments || {});
      return answer({content: [{type: 'text', text: JSON.stringify(result, null, 2)}], structuredContent: result, isError: params.name === 'remote_exec' && result.exitCode !== 0});
    } catch (error) { return answer({content: [{type: 'text', text: error.message}], isError: true}); }
  }
  return {jsonrpc: '2.0', id, error: {code: -32601, message: 'Method not found'}};
}

async function main() {
  if (process.argv[2] === '--mcp') {
    const lines = readline.createInterface({input: process.stdin, crlfDelay: Infinity});
    lines.on('line', line => {
      if (!line.trim()) return;
      if (Buffer.byteLength(line) > MAX_MCP_MESSAGE_BYTES) { process.stdout.write(`${JSON.stringify({jsonrpc: '2.0', id: null, error: {code: -32600, message: 'Message too large'}})}\n`); return; }
      Promise.resolve().then(() => handleRpc(JSON.parse(line))).then(reply => { if (reply) process.stdout.write(`${JSON.stringify(reply)}\n`); }).catch(() => process.stdout.write(`${JSON.stringify({jsonrpc: '2.0', id: null, error: {code: -32700, message: 'Parse error'}})}\n`));
    });
    return;
  }
  if (process.argv[2] === '--list-tools') {
    const value = process.argv[3] === '--full'
      ? {schema_version: TOOL_CATALOG_SCHEMA_VERSION, mcp_server: 'desktop-ssh', discovery_mode: 'full_compatibility', tools: TOOLS}
      : toolCatalogSummary();
    process.stdout.write(`${JSON.stringify(value, null, 2)}\n`);
    return;
  }
  if (process.argv[2] === '--describe-tool') {
    process.stdout.write(`${JSON.stringify(describeTool(process.argv[3]), null, 2)}\n`);
    return;
  }
  const method = process.argv[2];
  if (!TOOLS.some(tool => tool.name === method)) throw new Error('用法：agent-tools.cjs --list-tools；--describe-tool <工具名>；<工具名称> [JSON参数 | @JSON文件]；remote_exec "命令"；--mcp');
  let params = {};
  if (method === 'remote_exec') params = {command: process.argv[3], timeoutMs: Number(process.argv[4]) || 30000};
  else if (process.argv[3]) params = JSON.parse(process.argv[3].startsWith('@') ? require('node:fs').readFileSync(process.argv[3].slice(1), 'utf8').replace(/^\uFEFF/, '') : process.argv[3]);
  const result = method === TOOL_DESCRIBE.name ? describeTool(params.name) : await callDesktop(method, params);
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
  if (method === 'remote_exec' && result.exitCode !== 0) process.exitCode = 1;
}
if (require.main === module) main().catch(error => { process.stderr.write(`${error.message}\n`); process.exitCode = 1; });
module.exports = {
  callDesktop,
  describeTool,
  handleRpc,
  mcpAdvertisedTools,
  shortDescription,
  toolCatalogSummary,
  BROKER_TOOLS,
  MAX_MCP_MESSAGE_BYTES,
  TOOL_CATALOG_SCHEMA_VERSION,
  TOOL_DESCRIBE,
  TOOLS,
};
