const LAYOUT_VERSION = 2;
const STORAGE_KEY = 'heterogeneous-workbench.layout.v2';
const DEFAULT_PREFERENCES = Object.freeze({
  version: LAYOUT_VERSION,
  sidebarWidth: null,
  agentWidth: null,
  terminalHeight: null,
  agentCollapsed: false,
  terminalCollapsed: false,
});

function dimension(value) {
  return typeof value === 'number' && Number.isFinite(value) && value > 0
    ? Math.min(value, 8192) : null;
}

function normalizePreferences(value) {
  if (!value || value.version !== LAYOUT_VERSION) return { ...DEFAULT_PREFERENCES };
  return {
    version: LAYOUT_VERSION,
    sidebarWidth: dimension(value.sidebarWidth),
    agentWidth: dimension(value.agentWidth),
    terminalHeight: dimension(value.terminalHeight),
    agentCollapsed: value.agentCollapsed === true,
    terminalCollapsed: value.terminalCollapsed === true,
  };
}

function clamp(value, min, max) {
  return Math.round(Math.max(min, Math.min(max, value)));
}

// Width includes the sidebar and both separators, but excludes main-shell padding.
// Resolve from the saved preference on every resize, so shrinking the window does
// not permanently overwrite a user's larger preferred panel size.
function resolveLayout(preferences, metrics) {
  const width = Math.max(0, Number(metrics.width) || 0);
  const height = Math.max(0, Number(metrics.height) || 0);
  const sidebarGap = Math.max(0, metrics.sidebarGap ?? 6);
  const agentGap = preferences.agentCollapsed ? 0 : Math.max(0, metrics.agentGap ?? 6);
  const terminalGap = Math.max(0, metrics.terminalGap ?? 6);
  const centerMin = Math.min(430, width * 0.45);
  const agentMin = preferences.agentCollapsed ? 0 : Math.min(340, width * 0.34);
  // The activity bar follows VS Code's fixed narrow rail. Labels are exposed as
  // hover/focus flyouts, so resizing the rail would only waste editor space.
  const sidebarMax = Math.min(50, width);
  const sidebarMin = sidebarMax;
  const sidebarWidth = sidebarMax;
  const agentMax = Math.max(0, Math.min(900, width - sidebarWidth - sidebarGap - agentGap - centerMin));
  const agentWidth = clamp(preferences.agentWidth ?? width * 0.4, Math.min(agentMin, agentMax), agentMax);
  const topMin = Math.min(280, height * 0.6);
  const terminalMax = Math.max(0, height - topMin - terminalGap);
  const terminalMin = Math.min(180, terminalMax);
  const terminalHeight = clamp(preferences.terminalHeight ?? height / 3, terminalMin, terminalMax);
  return {
    sidebarWidth, agentWidth, terminalHeight,
    limits: {
      sidebar: [Math.ceil(sidebarMin), Math.floor(sidebarMax)],
      agent: [Math.ceil(Math.min(agentMin, agentMax)), Math.floor(agentMax)],
      terminal: [Math.ceil(terminalMin), Math.floor(terminalMax)],
    },
  };
}

module.exports = { STORAGE_KEY, DEFAULT_PREFERENCES, normalizePreferences, resolveLayout, clamp };
