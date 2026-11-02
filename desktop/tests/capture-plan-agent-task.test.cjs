'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {MAX_PROMPT_BYTES, compactCapturePlanContext, buildCapturePlanAgentTask} = require('../renderer/capture-plan-agent-task.cjs');

function fixture() {
  return {
    record: {
      planId: 'plan_fixture', updatedAt: '2026-10-31T10:00:00.000Z',
      plan: {
        title: '图库计划', template_id: 'gallery', natural_language_steps: '',
        steps: [
          {step_id: 'step_01', ordinal: 1, title: '启动图库', operation: 'launch', expected_targets: ['photos'], immutable_metadata: 'omit'},
          {step_id: 'step_02', ordinal: 2, title: '点击照片', operation: 'unresolved', target: 'photos'},
        ],
        profile: {bundle: 'com.example.photos', ability: 'MainAbility', targets: {photos: {id: 'TabPhotos'}, unused: {id: 'Unused'}}, trace_categories: ['sched']},
        capture: {selected_step_ids: ['step_02'], collectors: ['trace', 'pmu'], pmu: {period_us: 10}},
        provenance: {large: 'must-not-enter-agent-context'},
        readiness: {blockers: [{step_id: 'step_02', reason: 'instruction_needs_structured_operation'}]},
      },
    },
    catalog: {operations: ['launch', 'click', 'unresolved'], provenance: {large: 'must-not-enter-agent-context'}},
    request: '把点击照片转换为结构化动作。', requestId: 'capture_plan_request_fixture',
  };
}

test('capture plan Agent task carries only editable fields and a one-tool completion contract', () => {
  const value = fixture();
  const context = compactCapturePlanContext(value);
  assert.equal(context.plan_id, value.record.planId);
  assert.equal(context.expected_updated_at, value.record.updatedAt);
  assert.deepEqual(context.targets, {photos: {id: 'TabPhotos'}});
  assert.equal(context.steps[0].ordinal, undefined);
  assert.equal(context.steps[0].immutable_metadata, undefined);
  assert.equal(context.application.bundle, 'com.example.photos');

  const prompt = buildCapturePlanAgentTask(value);
  assert.match(prompt, /只调用一次 capture_update_plan/);
  assert.match(prompt, /confirmUnchanged/);
  assert.match(prompt, /不要再次执行 --list-tools/);
  assert.match(prompt, /不要.*desktop_tool_describe/);
  assert.doesNotMatch(prompt, /must-not-enter-agent-context|selected_step_ids|period_us|trace_categories/);
  assert.ok(Buffer.byteLength(prompt) < 5000, `prompt is unexpectedly large: ${Buffer.byteLength(prompt)}`);
});

test('capture plan Agent task rejects an unsaved plan without a revision', () => {
  const value = fixture(); delete value.record.updatedAt;
  assert.throws(() => buildCapturePlanAgentTask(value), /缺少已保存计划/);
});

test('capture plan Agent task remains below the submit limit for a large valid plan', () => {
  const value = fixture();
  value.record.plan.steps = Array.from({length: 300}, (_, index) => ({
    step_id: `step_${String(index + 1).padStart(3, '0')}`,
    title: `步骤 ${index + 1} ${'说明'.repeat(80)}`,
    operation: 'unresolved', target: `target_${index + 1}`,
    selector: {text: '内联定位'.repeat(800)},
  }));
  value.record.plan.profile.targets = Object.fromEntries(value.record.plan.steps.map((step, index) => [step.target, {id: `id_${index}`, text: '控件'.repeat(1000)}]));
  value.record.plan.readiness.blockers = value.record.plan.steps.map(step => ({step_id: step.step_id, reason: 'instruction_needs_structured_operation'}));
  const context = compactCapturePlanContext(value);
  const prompt = buildCapturePlanAgentTask(value);
  assert.ok(context.steps_omitted > 0);
  assert.ok(Buffer.byteLength(prompt) <= MAX_PROMPT_BYTES);
  assert.ok(context.steps.length > 0);
});

test('capture plan Agent task rejects an unbounded user request before submit', () => {
  const value = fixture(); value.request = 'a'.repeat(9 * 1024);
  assert.throws(() => buildCapturePlanAgentTask(value), /不能超过 8 KiB/);
});
