'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.resolve(__dirname, '../renderer/capture.js'), 'utf8');

function bodyBetween(start, end) {
  const from = source.indexOf(start);
  const to = source.indexOf(end, from + start.length);
  assert(from >= 0 && to > from, `expected ${start} before ${end}`);
  return source.slice(from, to);
}

test('capture execution freezes one operation attempt before launching the selected plan', () => {
  const click = bodyBetween("$('capture-start').addEventListener('click'", "$('capture-device').addEventListener('change'");
  assert.match(click, /const operationAttemptId = nextCaptureAttemptId\(\)/);
  assert.match(click, /const deviceSerial = snapshot\.selectedDeviceSerial/);
  assert.match(click, /const hdcPath = \$\('hdc-path'\)\.value\.trim\(\) \|\| 'hdc'/);
  assert.match(click, /createCaptureAttempt\(\{operationAttemptId, record, deviceSerial, hdcPath\}\)/);

  const attempt = bodyBetween('function createCaptureAttempt', 'function rememberCaptureAttempt');
  assert.match(attempt, /planId: String\(record\.planId \|\| ''\)/);
  assert.match(attempt, /planRevision: String\(record\.updatedAt \|\| record\.createdAt \|\| ''\)/);
  assert.match(attempt, /planStamp: planStamp\(record\)/);
  assert.match(attempt, /deviceSerial: String\(deviceSerial \|\| ''\)/);
  assert.match(attempt, /hdcPath: String\(hdcPath \|\| 'hdc'\)/);
});

test('automatic capture retry is one-shot and refuses changed device, plan or active work', () => {
  const retry = bodyBetween('async function retryCaptureAttempt', 'function recoveryDetails');
  assert.match(retry, /original\.retryConsumed = true/);
  assert(retry.indexOf('original.retryConsumed = true') < retry.indexOf("invoke('getState')"), 'the only retry is consumed before validation');
  assert.match(retry, /if \(current\.activeRun\) throw new Error/);
  assert.match(retry, /current\.selectedDeviceSerial[\s\S]*original\.deviceSerial/);
  assert.match(retry, /!currentDevices\.includes\(original\.deviceSerial\)/);
  assert.match(retry, /item\.planId === original\.planId/);
  assert.match(retry, /retryPlanId !== original\.planId/);
  assert.match(retry, /record\.updatedAt \|\| record\.createdAt[\s\S]*retryPlanRevision/);
  assert.match(retry, /recoveryEligible: false/);
  assert.match(retry, /retryOf: original\.operationAttemptId/);
});

test('only a failed terminal capture receives recovery metadata and retries do not recurse', () => {
  const finished = bodyBetween('function notifyFinishedRunFailure', 'function acceptAgentPlanCandidate');
  assert.match(finished, /\['failed', 'interrupted'\]\.includes\(run\.status\)/);
  assert.match(finished, /run\.status === 'failed' \? recoveryDetails\(attempt, run\) : \{\}/,
    'interrupted runs remain diagnosable but are not automatically replayed');
  assert.match(finished, /reportedCaptureFailures\.has\(failureKey\)/);
  assert.match(finished, /if \(attempt\?\.failureNotified\) return/);

  const recovery = bodyBetween('function recoveryDetails', 'function clearAgentPlanTimers');
  assert.match(recovery, /if \(!attempt\?\.recoveryEligible\) return \{\}/);
  assert.match(recovery, /operationAttemptId: attempt\.operationAttemptId/);
  assert.match(recovery, /scopes: \['device'\]/);
  assert.match(recovery, /selectedDevice: attempt\.deviceSerial/);
  assert.match(recovery, /planId: attempt\.planId/);
  assert.match(recovery, /planRevision: attempt\.planRevision/);
  assert.match(recovery, /retry: \(recoveryResult\) => retryCaptureAttempt\(attempt, recoveryResult\)/);
  assert.match(recovery, /operation_attempt_id: attempt\.operationAttemptId/);
  assert.match(recovery, /function synchronousCaptureFailureDetails[\s\S]*attempt\.failureNotified = true/);
});

test('a launched retry exposes its run identity as pending for terminal tracking', () => {
  const launch = bodyBetween('async function launchCaptureAttempt', 'async function retryCaptureAttempt');
  assert.match(launch, /rememberCaptureAttempt\(run\.runId, attempt\)/);
  assert.match(launch, /runId: run\.runId/);
  assert.match(launch, /pending: activeStatuses\.has\(run\.status\)/);
  assert.match(launch, /operationAttemptId: attempt\.operationAttemptId/);
  assert.match(launch, /if \(trackCompletion\) result\.completion = captureCompletion\(run\)/);
  const completion = bodyBetween('function captureCompletion', 'function deviceSerials');
  assert.match(completion, /captureTerminalRuns\.get\(run\.runId\)/);
  assert.match(completion, /new Promise\(\(resolve, reject\) => captureRunCompletions\.set/);
});

