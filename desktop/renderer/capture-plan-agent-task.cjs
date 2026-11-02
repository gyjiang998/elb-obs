'use strict';

const CAPTURE_PLAN_AGENT_TASK_VERSION = 1;
const STEP_FIELDS = ['step_id', 'title', 'slug', 'operation', 'target', 'selector', 'expected_targets', 'key', 'seconds', 'points', 'velocity'];
const MAX_REQUEST_BYTES = 8 * 1024;
const MAX_CONTEXT_BYTES = 48 * 1024;
const MAX_PROMPT_BYTES = 60 * 1024;
const MAX_SELECTOR_BYTES = 2048;
const utf8Encoder = typeof TextEncoder === 'function' ? new TextEncoder() : null;

function byteLength(value) {
  const text = String(value);
  if (typeof Buffer !== 'undefined' && typeof Buffer.byteLength === 'function') return Buffer.byteLength(text);
  if (utf8Encoder) return utf8Encoder.encode(text).byteLength;
  return unescape(encodeURIComponent(text)).length;
}

function definedFields(value, fields) {
  return Object.fromEntries(fields.filter(key => value?.[key] !== undefined).map(key => [key, value[key]]));
}

function compactSelector(value) {
  if (value === undefined || value === null) return value;
  const serialized = JSON.stringify(value);
  if (byteLength(serialized) <= MAX_SELECTOR_BYTES) return value;
  return {_context_omitted: true, _byte_length: byteLength(serialized)};
}

function compactStep(value) {
  const step = definedFields(value, STEP_FIELDS);
  if (step.selector !== undefined) step.selector = compactSelector(step.selector);
  return step;
}

function compactBlocker(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {reason: String(value || '').slice(0, 500)};
  return Object.fromEntries(Object.entries(value).slice(0, 12).map(([key, item]) => [key,
    typeof item === 'string' ? item.slice(0, 500) : item]));
}

function compactCapturePlanContext({record, catalog, request, requestId} = {}) {
  if (!record?.planId || !record?.updatedAt || !record?.plan) throw new Error('计划补全上下文缺少已保存计划。');
  if (typeof request !== 'string' || !request.trim()) throw new Error('计划补全要求不能为空。');
  if (byteLength(request) > MAX_REQUEST_BYTES) throw new Error('补充给 Agent 的要求不能超过 8 KiB，请缩短后重试。');
  const plan = record.plan;
  const allSteps = Array.isArray(plan.steps) ? plan.steps : [];
  const blockers = (Array.isArray(plan.readiness?.blockers) ? plan.readiness.blockers : []).map(compactBlocker);
  const priorityIds = new Set(blockers.map(value => value?.step_id).filter(Boolean));
  for (const step of allSteps) if (step?.operation === 'unresolved') priorityIds.add(step.step_id);
  const orderedSteps = [...allSteps.filter(step => priorityIds.has(step?.step_id)), ...allSteps.filter(step => !priorityIds.has(step?.step_id))];
  const context = {
    schema_version: 'capture_plan_agent_task/v1',
    request_id: requestId,
    plan_id: record.planId,
    expected_updated_at: record.updatedAt,
    user_request: request.trim(),
    allowed_operations: (Array.isArray(catalog?.operations) ? catalog.operations : []).slice(0, 128),
    application: {bundle: plan.profile?.bundle || '', ability: plan.profile?.ability || ''},
    targets: {},
    steps: [],
    blockers,
    steps_total: allSteps.length,
    steps_omitted: 0,
  };
  if (byteLength(JSON.stringify(context)) > MAX_CONTEXT_BYTES) throw new Error('计划阻塞信息过多，无法在一次 Agent 任务中安全提交；请先缩小需要补全的步骤范围。');
  const availableTargets = plan.profile?.targets && typeof plan.profile.targets === 'object' ? plan.profile.targets : {};
  for (const original of orderedSteps) {
    const step = compactStep(original);
    const nextTargets = {...context.targets};
    const names = [step.target, ...(Array.isArray(step.expected_targets) ? step.expected_targets : [])].filter(Boolean);
    for (const name of names) if (Object.hasOwn(availableTargets, name)) nextTargets[name] = compactSelector(availableTargets[name]);
    const candidate = {...context, steps: [...context.steps, step], targets: nextTargets};
    if (byteLength(JSON.stringify(candidate)) <= MAX_CONTEXT_BYTES) {
      context.steps = candidate.steps;
      context.targets = nextTargets;
    } else context.steps_omitted += 1;
  }
  return context;
}

function buildCapturePlanAgentTask(value) {
  const context = compactCapturePlanContext(value);
  const prompt = `完成一次负载采样计划的局部补全。这是结构化数据任务，页面已经保存基线计划并提供完整的本次合同；不要分析代码仓、SSH 服务器、设备、Trace 或 Skill，也不要调用 capture_catalog、capture_status、capture_save_plan、capture_start、capture_cancel、remote_*、workspace_* 或 trace_*。

根据 user_request 只修改确有必要的步骤动作、控件定位、应用信息或标题。保留未列出的计划内容，尤其是采样步骤选择、采集器、PMU 参数和执行策略。没有证据的控件选择器不要臆造；可以保留 unresolved。完成时只调用一次 capture_update_plan：
- planId = plan_id
- expectedUpdatedAt = expected_updated_at
- requestId = request_id
- stepUpdates 只放变化的现有步骤
- targetUpdates 只放变化的控件 selector
- app 只在 bundle 或 ability 需要修改时提交
- summary 用一句话说明结果
- 如果现有计划已经满足要求，不提交空补丁，改传 confirmUnchanged: true

原生 MCP 中没有该工具时，把参数写入临时 JSON 文件，再使用环境变量 HETERO_DESKTOP_TOOLS 指向的 CLI 按“<入口> capture_update_plan @<JSON文件>”调用，完成后删除临时文件；本任务的参数合同已经给出，不要再次执行 --list-tools 或 desktop_tool_describe。工具成功返回 requestId 和 planId 后，用一句话说明结果并结束，不继续验证或执行采样。

任务上下文：
${JSON.stringify(context)}`;
  if (byteLength(prompt) > MAX_PROMPT_BYTES) throw new Error('计划补全任务超过 60 KiB，无法安全提交给 Agent；请缩小步骤范围后重试。');
  return prompt;
}

module.exports = {CAPTURE_PLAN_AGENT_TASK_VERSION, MAX_PROMPT_BYTES, compactCapturePlanContext, buildCapturePlanAgentTask};
