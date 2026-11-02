'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {
  DEFAULT_AUTOMATION_SETTINGS,
  normalizeAutomationSettings,
  authorizedAutomationScopes,
  automationScopeAllowed,
} = require('../runtime/automation-settings.cjs');

test('automatic recovery and broad operation grants are opt-in', () => {
  assert.deepEqual(normalizeAutomationSettings(), DEFAULT_AUTOMATION_SETTINGS);
  assert.deepEqual(normalizeAutomationSettings({
    autoRecoverErrors: true,
    allowDeviceOperations: true,
    allowServerOperations: false,
  }), {
    autoRecoverErrors: true,
    allowDeviceOperations: true,
    allowServerOperations: false,
  });
  assert.deepEqual(normalizeAutomationSettings({
    autoRecoverErrors: 1,
    allowDeviceOperations: 'true',
    allowServerOperations: null,
  }), DEFAULT_AUTOMATION_SETTINGS);
});

test('automatic recovery authorization is scoped independently to device and server operations', () => {
  const settings = normalizeAutomationSettings({allowDeviceOperations: true});
  assert.deepEqual(authorizedAutomationScopes(settings), ['device']);
  assert.equal(automationScopeAllowed(settings, 'device'), true);
  assert.equal(automationScopeAllowed(settings, 'server'), false);
  assert.equal(automationScopeAllowed(settings, 'unknown'), false);
});
