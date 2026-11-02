const test = require('node:test');
const assert = require('node:assert/strict');
const {
  TERMINAL_KEY,
  BROWSER_COPY_KEY,
  BROWSER_PASTE_KEY,
  classifyTerminalShortcut,
  shouldProcessTerminalKey,
} = require('../renderer/terminal-shortcuts.cjs');

function key(overrides = {}) {
  return {
    type: 'keydown',
    code: '',
    key: '',
    ctrlKey: false,
    metaKey: false,
    altKey: false,
    shiftKey: false,
    ...overrides,
  };
}

test('Ctrl+C copies a terminal selection but remains interrupt without one', () => {
  const ctrlC = key({ code: 'KeyC', key: 'c', ctrlKey: true });
  assert.equal(classifyTerminalShortcut(ctrlC, true), BROWSER_COPY_KEY);
  assert.equal(shouldProcessTerminalKey(ctrlC, true), false);
  assert.equal(classifyTerminalShortcut(ctrlC, false), TERMINAL_KEY);
  assert.equal(shouldProcessTerminalKey(ctrlC, false), true);
});

test('Cmd+C copies a selection and never becomes terminal input on macOS', () => {
  const commandC = key({ code: 'KeyC', key: 'c', metaKey: true });
  assert.equal(classifyTerminalShortcut(commandC, true), BROWSER_COPY_KEY);
  assert.equal(classifyTerminalShortcut(commandC, false), BROWSER_COPY_KEY);
  assert.equal(shouldProcessTerminalKey(commandC, false), false);
});

test('Ctrl+V and Cmd+V use the browser paste event', () => {
  for (const modifier of [{ ctrlKey: true }, { metaKey: true }]) {
    const paste = key({ code: 'KeyV', key: 'v', ...modifier });
    assert.equal(classifyTerminalShortcut(paste), BROWSER_PASTE_KEY);
    assert.equal(shouldProcessTerminalKey(paste), false);
  }
});

test('shifted clipboard shortcuts remain available', () => {
  assert.equal(classifyTerminalShortcut(key({ code: 'KeyC', ctrlKey: true, shiftKey: true }), true), BROWSER_COPY_KEY);
  assert.equal(classifyTerminalShortcut(key({ code: 'KeyV', ctrlKey: true, shiftKey: true })), BROWSER_PASTE_KEY);
});

test('Alt combinations, unrelated keys and non-keydown events stay with xterm', () => {
  assert.equal(classifyTerminalShortcut(key({ code: 'KeyC', ctrlKey: true, altKey: true }), true), TERMINAL_KEY);
  assert.equal(classifyTerminalShortcut(key({ code: 'KeyV', ctrlKey: true, altKey: true })), TERMINAL_KEY);
  assert.equal(classifyTerminalShortcut(key({ code: 'KeyA', key: 'a', ctrlKey: true })), TERMINAL_KEY);
  assert.equal(classifyTerminalShortcut(key({ type: 'keyup', code: 'KeyV', ctrlKey: true })), TERMINAL_KEY);
  assert.equal(classifyTerminalShortcut(key({ code: '', key: 'V', ctrlKey: true })), BROWSER_PASTE_KEY);
});
