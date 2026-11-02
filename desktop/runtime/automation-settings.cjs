'use strict';

const AUTOMATION_SCOPES = Object.freeze(['device', 'server']);
const DEFAULT_AUTOMATION_SETTINGS = Object.freeze({
  autoRecoverErrors: false,
  allowDeviceOperations: false,
  allowServerOperations: false,
});

function normalizeAutomationSettings(value = {}) {
  const source = value && !Array.isArray(value) && typeof value === 'object' ? value : {};
  return {
    autoRecoverErrors: source.autoRecoverErrors === true,
    allowDeviceOperations: source.allowDeviceOperations === true,
    allowServerOperations: source.allowServerOperations === true,
  };
}

function authorizedAutomationScopes(value = {}) {
  const settings = normalizeAutomationSettings(value);
  return [
    ...(settings.allowDeviceOperations ? ['device'] : []),
    ...(settings.allowServerOperations ? ['server'] : []),
  ];
}

function automationScopeAllowed(value, scope) {
  if (!AUTOMATION_SCOPES.includes(scope)) return false;
  return authorizedAutomationScopes(value).includes(scope);
}

module.exports = {
  AUTOMATION_SCOPES,
  DEFAULT_AUTOMATION_SETTINGS,
  normalizeAutomationSettings,
  authorizedAutomationScopes,
  automationScopeAllowed,
};
