'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'capture.js'), 'utf8');

function functionSource(name, nextName) {
  const begin = source.indexOf(`  function ${name}(`);
  const end = nextName ? source.indexOf(`\n  function ${nextName}(`, begin + 1) : source.length;
  assert(begin >= 0 && end > begin, `missing renderer function ${name}`);
  return source.slice(begin, end);
}

test('Agent plan wait records the submitted plan revision and accepts only its unchanged UI baseline', () => {
  const begin = functionSource('beginAgentPlanWait', 'submissionMatchesCurrentPlan');
  const match = functionSource('submissionMatchesCurrentPlan', 'basePlan');
  const event = functionSource('event');
  assert.match(begin, /planId: record\.planId/);
  assert.match(begin, /expectedUpdatedAt: record\.updatedAt/);
  assert.match(begin, /baselineStamp: planStamp\(record\)/);
  assert.match(match, /activeAgentPlanSubmission\?\.requestId === submission\.requestId/,
    'a late result from an older request must be queued while a newer request is active');
  assert.match(match, /selectedPlanId === submission\.planId/);
  assert.match(match, /record\?\.updatedAt === submission\.expectedUpdatedAt/);
  assert.match(match, /loadedPlanStamp === submission\.baselineStamp && !dirty/);
  assert.ok(event.indexOf('const baselineCurrent = submissionMatchesCurrentPlan(submission)')
    < event.indexOf('applyState(event.state, {suppressPlanSync: agentPlanEvent})'),
  'the current baseline must be captured before the completion state changes the plan revision');
  assert.match(event, /agentPlanSubmissions\.get\(event\.requestId\)/);
  assert.doesNotMatch(source, /agentSubmittedAt|!agentPlanRequestId/,
    'timestamp guessing must not identify Agent plan completion');
});

test('switched or dirty plans queue an explicit update while readiness blockers remain non-executable', () => {
  const accept = functionSource('acceptAgentPlanCandidate', 'applyState');
  const queued = functionSource('queuePlanUpdate', 'acceptAgentPlanCandidate');
  assert.match(accept, /if \(!baselineCurrent \|\| dirty\) \{ queuePlanUpdate\(candidate, \{agent: true\}\); return; \}/);
  assert.match(queued, /capture-update/);
  assert.match(queued, /\u624b\u52a8\u8f7d\u5165|\u8f7d\u5165/);
  assert.match(accept, /candidate\.plan\?\.readiness\?\.blockers \|\| \[\]/);
  assert.ok(accept.indexOf("focusWorkflowAction('capture-agent-plan')")
    < accept.indexOf("focusWorkflowAction('capture-start')"),
  'an incomplete Agent result must retain the refinement action before the executable path');
});

test('Agent plan wait is bounded and every terminal path clears its timers', () => {
  const begin = functionSource('beginAgentPlanWait', 'submissionMatchesCurrentPlan');
  const finish = functionSource('finishAgentPlanWait', 'beginAgentPlanWait');
  const event = functionSource('event');
  assert.match(source, /const AGENT_PLAN_SLOW_MS = 90_000/);
  assert.match(source, /const AGENT_PLAN_DEADLINE_MS = 120_000/);
  assert.match(begin, /finishAgentPlanWait\(submission, 'timed_out'\)/);
  assert.match(begin, /\u5df2\u6062\u590d|\u91cd\u8bd5/);
  assert.match(finish, /clearAgentPlanTimers\(\)/);
  assert.match(event, /\['stopped', 'error'\]\.includes\(event\.state\)/);
  assert.match(event, /finishAgentPlanWait\(submission, event\.state === 'error' \? 'agent_error' : 'agent_stopped'\)/);
  assert.match(event, /finishAgentPlanWait\(submission, 'completed'\)/);
  assert.match(event, /finishAgentPlanWait\(submission, 'failed'\)/);
  assert.match(source, /catch \(error\) \{ finishAgentPlanWait\(submission, 'submit_failed'\); throw error; \}/);
  assert.match(source, /dispose: \(\) => \{ disposed = true; clearTimeout\(refreshTimer\); clearAgentPlanTimers\(\); \}/);
});
