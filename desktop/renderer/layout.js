import geometry from './layout-geometry.cjs';

const { STORAGE_KEY, DEFAULT_PREFERENCES, normalizePreferences, resolveLayout, clamp } = geometry;
const SIZE_KEYS = { sidebar: 'sidebarWidth', agent: 'agentWidth', terminal: 'terminalHeight' };

export function createWorkbenchLayout({ onResize = () => {}, onStateChange = () => {} } = {}) {
  const shell = document.querySelector('.app-shell');
  const sidebar = document.querySelector('.sidebar');
  const workspace = document.querySelector('.workspace-layout');
  const top = document.querySelector('.workspace-top');
  const panels = { agent: document.getElementById('agent-panel'), terminal: document.getElementById('terminal-panel') };
  const splitters = Object.fromEntries(['sidebar', 'agent', 'terminal'].map(kind => [kind, document.getElementById(`${kind}-splitter`)]));
  if (!shell || !sidebar || !workspace || !top || Object.values(panels).some(panel => !panel)) {
    throw new Error('Workbench layout containers are missing');
  }

  let preferences;
  try { preferences = normalizePreferences(JSON.parse(localStorage.getItem(STORAGE_KEY))); }
  catch { preferences = { ...DEFAULT_PREFERENCES }; }
  let resolved;
  let frame = 0;
  let drag = null;
  let disposed = false;
  let reportedState = '';
  const listeners = [];
  const listen = (target, event, handler, options) => {
    if (!target) return;
    target.addEventListener(event, handler, options);
    listeners.push(() => target.removeEventListener(event, handler, options));
  };
  const persist = () => {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(preferences)); } catch { /* Layout still works when storage is unavailable. */ }
  };
  const notifyResize = () => {
    if (frame || disposed) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      if (!disposed) onResize();
    });
  };
  const notifyState = () => {
    const state = { agentVisible: !preferences.agentCollapsed, terminalVisible: !preferences.terminalCollapsed };
    const serialized = JSON.stringify(state);
    if (reportedState === serialized) return;
    reportedState = serialized;
    onStateChange(state);
  };
  const metrics = () => ({
    width: workspace.clientWidth + sidebar.getBoundingClientRect().width + (splitters.sidebar?.getBoundingClientRect().width || 6),
    height: workspace.clientHeight,
    sidebarGap: splitters.sidebar?.getBoundingClientRect().width || 6,
    agentGap: splitters.agent?.getBoundingClientRect().width || 6,
    terminalGap: splitters.terminal?.getBoundingClientRect().height || 6,
  });
  const apply = () => {
    if (disposed) return;
    shell.classList.toggle('agent-collapsed', preferences.agentCollapsed);
    shell.classList.toggle('terminal-collapsed', preferences.terminalCollapsed);
    resolved = resolveLayout(preferences, metrics());
    for (const [kind, key] of Object.entries(SIZE_KEYS)) {
      const property = kind === 'terminal' ? '--terminal-height' : `--${kind}-width`;
      const value = `${resolved[key]}px`;
      if (shell.style.getPropertyValue(property) !== value) shell.style.setProperty(property, value);
      const separator = splitters[kind];
      if (separator) {
        separator.setAttribute('aria-valuemin', String(resolved.limits[kind][0]));
        separator.setAttribute('aria-valuemax', String(resolved.limits[kind][1]));
        separator.setAttribute('aria-valuenow', String(resolved[key]));
        separator.setAttribute('aria-valuetext', `${resolved[key]} 像素`);
      }
    }
    for (const kind of ['agent', 'terminal']) {
      const collapsed = preferences[`${kind}Collapsed`];
      panels[kind].setAttribute('aria-hidden', String(collapsed));
      if (splitters[kind]) {
        splitters[kind].tabIndex = collapsed ? -1 : 0;
        splitters[kind].setAttribute('aria-hidden', String(collapsed));
      }
    }
    notifyState();
    notifyResize();
  };
  const setCollapsed = (kind, collapsed) => {
    if (collapsed && panels[kind].contains(document.activeElement)) document.activeElement?.blur?.();
    preferences[`${kind}Collapsed`] = collapsed;
    apply();
    persist();
  };
  const setSize = (kind, value) => {
    preferences[SIZE_KEYS[kind]] = value === null ? null : clamp(value, ...resolved.limits[kind]);
    apply();
  };
  const stopDrag = (event) => {
    if (!drag || (event?.pointerId != null && drag.pointerId !== event.pointerId)) return;
    const finished = drag;
    drag = null;
    if (finished.element.hasPointerCapture?.(finished.pointerId)) finished.element.releasePointerCapture(finished.pointerId);
    document.body.classList.remove('layout-resizing');
    delete document.body.dataset.layoutDrag;
    persist();
    notifyResize();
  };

  for (const [kind, element] of Object.entries(splitters)) {
    if (!element) continue;
    if (kind === 'sidebar') {
      element.setAttribute('aria-hidden', 'true');
      element.removeAttribute('role');
      element.tabIndex = -1;
      continue;
    }
    element.setAttribute('aria-orientation', kind === 'terminal' ? 'horizontal' : 'vertical');
    listen(element, 'pointerdown', event => {
      if (event.button !== 0 || (kind !== 'sidebar' && preferences[`${kind}Collapsed`])) return;
      event.preventDefault();
      stopDrag();
      element.focus({ preventScroll: true });
      drag = { element, kind, pointerId: event.pointerId, origin: kind === 'terminal' ? event.clientY : event.clientX, size: resolved[SIZE_KEYS[kind]] };
      element.setPointerCapture(event.pointerId);
      document.body.classList.add('layout-resizing');
      document.body.dataset.layoutDrag = kind;
    });
    listen(element, 'pointermove', event => {
      if (!drag || drag.element !== element || drag.pointerId !== event.pointerId) return;
      const position = kind === 'terminal' ? event.clientY : event.clientX;
      setSize(kind, drag.size + (position - drag.origin) * (kind === 'sidebar' ? 1 : -1));
    });
    listen(element, 'pointerup', stopDrag);
    listen(element, 'pointercancel', stopDrag);
    listen(element, 'lostpointercapture', stopDrag);
    listen(element, 'dblclick', () => { setSize(kind, null); persist(); });
    listen(element, 'keydown', event => {
      const directions = kind === 'terminal' ? { ArrowUp: 1, ArrowDown: -1 }
        : kind === 'sidebar' ? { ArrowLeft: -1, ArrowRight: 1 } : { ArrowLeft: 1, ArrowRight: -1 };
      let value;
      if (event.key === 'Home') value = resolved.limits[kind][0];
      else if (event.key === 'End') value = resolved.limits[kind][1];
      else if (directions[event.key]) value = resolved[SIZE_KEYS[kind]] + directions[event.key] * (event.shiftKey ? 50 : 16);
      else return;
      event.preventDefault();
      setSize(kind, value);
      persist();
    });
  }
  listen(document.getElementById('collapse-terminal'), 'click', () => setCollapsed('terminal', true));
  const reset = () => {
    stopDrag();
    preferences = { ...DEFAULT_PREFERENCES };
    apply();
    persist();
  };
  listen(window, 'resize', apply);
  listen(window, 'blur', () => stopDrag());
  // Also account for the global notice changing the workspace's available height.
  const observer = new ResizeObserver(apply);
  observer.observe(workspace);
  apply();

  return {
    showAgent: () => setCollapsed('agent', false),
    showTerminal: () => setCollapsed('terminal', false),
    hideAgent: () => setCollapsed('agent', true),
    hideTerminal: () => setCollapsed('terminal', true),
    setAgentVisible: visible => setCollapsed('agent', !visible),
    setTerminalVisible: visible => setCollapsed('terminal', !visible),
    toggleAgent: () => setCollapsed('agent', !preferences.agentCollapsed),
    toggleTerminal: () => setCollapsed('terminal', !preferences.terminalCollapsed),
    state: () => ({ agentVisible: !preferences.agentCollapsed, terminalVisible: !preferences.terminalCollapsed }),
    reset,
    resize: apply,
    dispose() {
      stopDrag();
      disposed = true;
      if (frame) cancelAnimationFrame(frame);
      observer.disconnect();
      listeners.forEach(remove => remove());
    },
  };
}
