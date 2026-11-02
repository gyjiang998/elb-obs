const TERMINAL_KEY = 'terminal';
const BROWSER_COPY_KEY = 'browser-copy';
const BROWSER_PASTE_KEY = 'browser-paste';

function normalizedKey(event) {
  if (event?.code === 'KeyC') return 'c';
  if (event?.code === 'KeyV') return 'v';
  return typeof event?.key === 'string' ? event.key.toLowerCase() : '';
}

function classifyTerminalShortcut(event, hasSelection = false) {
  if (!event || event.type !== 'keydown' || event.altKey) return TERMINAL_KEY;
  const modified = event.ctrlKey === true || event.metaKey === true;
  if (!modified) return TERMINAL_KEY;

  const key = normalizedKey(event);
  if (key === 'v') return BROWSER_PASTE_KEY;
  if (key !== 'c') return TERMINAL_KEY;
  if (hasSelection) return BROWSER_COPY_KEY;

  // Ctrl+C without a selection remains the terminal interrupt sequence. On
  // macOS, Cmd+C has no terminal meaning and can stay with the browser.
  return event.ctrlKey === true && event.metaKey !== true ? TERMINAL_KEY : BROWSER_COPY_KEY;
}

function shouldProcessTerminalKey(event, hasSelection = false) {
  return classifyTerminalShortcut(event, hasSelection) === TERMINAL_KEY;
}

module.exports = {
  TERMINAL_KEY,
  BROWSER_COPY_KEY,
  BROWSER_PASTE_KEY,
  classifyTerminalShortcut,
  shouldProcessTerminalKey,
};
