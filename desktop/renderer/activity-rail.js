export function createActivityRail({sidebar, enabled = true, openDelayMs = 110, closeDelayMs = 80} = {}) {
  if (!sidebar) throw new Error('Activity rail element is required');
  const items = [...sidebar.querySelectorAll('.nav-item')];
  const listeners = [];
  let expandOnHover = enabled !== false;
  let openTimer = 0;
  let closeTimer = 0;
  let suppressedUntilPointerLeave = false;
  let disposed = false;

  const listen = (target, type, handler) => {
    target.addEventListener(type, handler);
    listeners.push(() => target.removeEventListener(type, handler));
  };
  const clearTimers = () => {
    window.clearTimeout(openTimer);
    window.clearTimeout(closeTimer);
    openTimer = 0;
    closeTimer = 0;
  };
  const setOpen = (open) => {
    if (disposed) return;
    sidebar.dataset.flyoutOpen = String(expandOnHover && open);
  };
  const captureTitles = () => {
    for (const item of items) {
      if (!item.dataset.activityRailTitle) item.dataset.activityRailTitle = item.getAttribute('title') || item.getAttribute('aria-label') || '';
    }
  };
  const syncTitles = () => {
    captureTitles();
    for (const item of items) {
      if (expandOnHover) item.removeAttribute('title');
      else if (item.dataset.activityRailTitle) item.setAttribute('title', item.dataset.activityRailTitle);
    }
  };
  const clearSingleLabelState = () => {
    for (const item of items) {
      item.classList.remove('nav-tooltip-suppressed');
      delete item.dataset.tooltipAwaitingPointerReentry;
      delete item.dataset.suppressedTooltipTitle;
    }
  };
  const scheduleOpen = () => {
    window.clearTimeout(closeTimer);
    if (!expandOnHover || suppressedUntilPointerLeave) return;
    window.clearTimeout(openTimer);
    openTimer = window.setTimeout(() => setOpen(true), openDelayMs);
  };
  const scheduleClose = () => {
    window.clearTimeout(openTimer);
    window.clearTimeout(closeTimer);
    closeTimer = window.setTimeout(() => setOpen(false), closeDelayMs);
  };
  const suppressSingleLabel = (item) => {
    if (expandOnHover || item.classList.contains('nav-tooltip-suppressed')) return;
    item.dataset.suppressedTooltipTitle = item.getAttribute('title') || '';
    item.removeAttribute('title');
    item.classList.add('nav-tooltip-suppressed');
  };
  const prepareSingleLabelForReentry = (item) => {
    if (expandOnHover || !item.classList.contains('nav-tooltip-suppressed')) return;
    if (item.dataset.suppressedTooltipTitle) item.setAttribute('title', item.dataset.suppressedTooltipTitle);
    delete item.dataset.suppressedTooltipTitle;
    item.dataset.tooltipAwaitingPointerReentry = 'true';
  };
  const restoreSingleLabelOnReentry = (item) => {
    if (expandOnHover || item.dataset.tooltipAwaitingPointerReentry !== 'true') return;
    item.classList.remove('nav-tooltip-suppressed');
    delete item.dataset.tooltipAwaitingPointerReentry;
  };
  const closeUntilPointerLeaves = () => {
    if (!expandOnHover) return;
    clearTimers();
    suppressedUntilPointerLeave = true;
    setOpen(false);
  };
  const close = () => {
    clearTimers();
    suppressedUntilPointerLeave = false;
    setOpen(false);
  };
  const setEnabled = (value) => {
    expandOnHover = value !== false;
    clearTimers();
    suppressedUntilPointerLeave = false;
    sidebar.dataset.labelMode = expandOnHover ? 'all' : 'single';
    setOpen(false);
    clearSingleLabelState();
    syncTitles();
  };

  captureTitles();
  setEnabled(expandOnHover);
  listen(sidebar, 'pointerenter', scheduleOpen);
  listen(sidebar, 'pointerleave', () => {
    suppressedUntilPointerLeave = false;
    scheduleClose();
  });
  listen(sidebar, 'focusin', () => {
    if (!expandOnHover || suppressedUntilPointerLeave) return;
    clearTimers();
    setOpen(true);
  });
  listen(sidebar, 'focusout', (event) => {
    if (sidebar.contains(event.relatedTarget)) return;
    suppressedUntilPointerLeave = false;
    scheduleClose();
  });
  listen(sidebar, 'keydown', (event) => {
    if (event.key !== 'Escape' || sidebar.dataset.flyoutOpen !== 'true') return;
    event.preventDefault();
    closeUntilPointerLeaves();
  });
  for (const item of items) {
    listen(item, 'pointerdown', () => suppressSingleLabel(item));
    listen(item, 'click', (event) => {
      if (expandOnHover) {
        if (event.detail === 0) {
          clearTimers();
          suppressedUntilPointerLeave = false;
          setOpen(false);
        } else closeUntilPointerLeaves();
      } else if (event.detail !== 0) {
        suppressSingleLabel(item);
      }
    });
    listen(item, 'pointerleave', () => prepareSingleLabelForReentry(item));
    listen(item, 'pointerenter', () => restoreSingleLabelOnReentry(item));
  }

  return {
    close,
    closeUntilPointerLeaves,
    isEnabled: () => expandOnHover,
    setEnabled,
    dispose() {
      disposed = true;
      clearTimers();
      for (const remove of listeners.splice(0)) remove();
    },
  };
}
