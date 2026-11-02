'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {
  AGENT_EXECUTION_TAB_ID,
  createSshTerminalWorkspace,
} = require('../renderer/ssh-terminal-workspace.js');

class FakeClassList {
  constructor(element) { this.element = element; }
  values() { return new Set(String(this.element.className || '').split(/\s+/).filter(Boolean)); }
  toggle(name, force) {
    const values = this.values();
    const add = force === undefined ? !values.has(name) : Boolean(force);
    if (add) values.add(name); else values.delete(name);
    this.element.className = [...values].join(' ');
    return add;
  }
  contains(name) { return this.values().has(name); }
}

class FakeElement {
  constructor(ownerDocument, tagName) {
    this.ownerDocument = ownerDocument;
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.attributes = {};
    this.listeners = new Map();
    this.className = '';
    this.classList = new FakeClassList(this);
    this.hidden = false;
    this.disabled = false;
    this.textContent = '';
    this.scrollTop = 0;
    this.scrollHeight = 100;
    this.style = {
      values: new Map(),
      setProperty: (name, value) => this.style.values.set(name, value),
      getPropertyValue: name => this.style.values.get(name) || '',
    };
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  append(...nodes) {
    for (const node of nodes) {
      if (node.parentNode) node.parentNode.children = node.parentNode.children.filter(item => item !== node);
      node.parentNode = this;
      this.children.push(node);
    }
  }
  insertBefore(node, before) {
    if (node.parentNode) node.parentNode.children = node.parentNode.children.filter(item => item !== node);
    node.parentNode = this;
    const index = this.children.indexOf(before);
    if (index < 0) this.children.push(node); else this.children.splice(index, 0, node);
  }
  remove() {
    if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(node => node !== this);
    this.parentNode = null;
  }
  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }
  removeEventListener(type, listener) {
    this.listeners.set(type, (this.listeners.get(type) || []).filter(item => item !== listener));
  }
  dispatch(type, overrides = {}) {
    const event = { preventDefault() {}, stopPropagation() {}, target: this, ...overrides };
    for (const listener of this.listeners.get(type) || []) listener(event);
  }
  getBoundingClientRect() { return { left: 0, top: 0, width: 1000, height: 300 }; }
}

class FakeDocument {
  constructor() { this.listeners = new Map(); this.body = new FakeElement(this, 'body'); }
  createElement(tagName) { return new FakeElement(this, tagName); }
  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }
  removeEventListener(type, listener) {
    this.listeners.set(type, (this.listeners.get(type) || []).filter(item => item !== listener));
  }
  dispatch(type, overrides = {}) {
    const event = { preventDefault() {}, stopPropagation() {}, ...overrides };
    for (const listener of this.listeners.get(type) || []) listener(event);
  }
}

class FakeFitAddon {
  fit() {
    this.fitCount = (this.fitCount || 0) + 1;
    this.terminal.cols = 121;
    this.terminal.rows = 37;
  }
}

class FakeTerminal {
  constructor(options) {
    this.options = options;
    this.cols = 80;
    this.rows = 24;
    this.writes = [];
    this.dataListeners = [];
    FakeTerminal.instances.push(this);
  }
  loadAddon(addon) { this.addon = addon; addon.terminal = this; }
  open(host) { this.host = host; }
  onData(listener) {
    this.dataListeners.push(listener);
    return { dispose: () => { this.dataListeners = this.dataListeners.filter(item => item !== listener); } };
  }
  emitData(data) { for (const listener of this.dataListeners) listener(data); }
  write(data) { this.writes.push(data); }
  attachCustomKeyEventHandler(handler) { this.keyHandler = handler; }
  hasSelection() { return false; }
  focus() { this.focused = true; }
  refresh(start, end) { this.refreshed = [start, end]; }
  dispose() { this.disposed = true; }
}
FakeTerminal.instances = [];

function fixture({ connected = true, ensureConnectedResult = { reconnected: false } } = {}) {
  FakeTerminal.instances = [];
  const document = new FakeDocument();
  const tabList = document.createElement('div');
  const paneContainer = document.createElement('div');
  const addButton = document.createElement('button');
  tabList.append(addButton);
  const calls = [];
  const notices = [];
  const connectionAttempts = [];
  const frameQueue = [];
  let connectionState = connected;
  let theme = 'dark';
  const responses = new Map();
  const invoke = async (method, payload) => {
    calls.push({ method, payload });
    const response = responses.get(method);
    return typeof response === 'function' ? response(payload) : response;
  };
  const workspace = createSshTerminalWorkspace({
    tabList, paneContainer, addButton, invoke,
    terminalMinimumContrastRatioFor: (value, target) => value === 'light' ? 4.5 : (value === 'hybrid' && target === 'ssh' ? 4.5 : 1),
    terminalThemeFor: (value, target) => ({ name: `${value}:${target}` }),
    getTheme: () => theme,
    notice: (...args) => notices.push(args),
    isConnected: () => connectionState,
    ensureConnected: async () => {
      connectionAttempts.push({ at: Date.now() });
      const result = typeof ensureConnectedResult === 'function'
        ? await ensureConnectedResult()
        : ensureConnectedResult;
      if (result === true || result?.reconnected) connectionState = true;
      return result;
    },
    Terminal: FakeTerminal,
    FitAddon: FakeFitAddon,
    scheduleFrame: callback => { frameQueue.push(callback); return frameQueue.length; },
    cancelFrame: () => {},
  });
  return {
    workspace, document, tabList, paneContainer, addButton, calls, notices, responses, connectionAttempts,
    setConnected(value) { connectionState = Boolean(value); },
    setTheme(value) { theme = value; },
    flushFrame() { const callback = frameQueue.shift(); callback?.(); },
  };
}

test('routes output, input and resize strictly by SSH terminalId', async () => {
  const f = fixture();
  await f.workspace.initialize({
    terminals: [{ terminalId: 'one' }, { terminalId: 'two' }],
    primaryTerminalId: 'one',
  });
  assert.deepEqual(f.workspace.getTerminalIds(), ['one', 'two']);
  assert.equal(f.workspace.getActiveId(), 'one');

  f.workspace.handleEvent({ type: 'terminal', target: 'ssh', terminalId: 'two', data: 'two-output' });
  assert.deepEqual(f.workspace.getTerminal('one').writes, []);
  assert.deepEqual(f.workspace.getTerminal('two').writes, ['two-output']);

  // Output may arrive just before the lifecycle event; it remains isolated by
  // ID and is flushed only when that exact terminal is materialized.
  f.workspace.handleEvent({ type: 'terminal', target: 'ssh', terminalId: 'three', data: 'early-three' });
  assert.equal(f.workspace.getTerminal('three'), null);
  f.workspace.handleEvent({ type: 'ssh-terminal', target: 'ssh', action: 'created', terminalId: 'three', terminal: { terminalId: 'three' } });
  assert.deepEqual(f.workspace.getTerminal('three').writes, ['early-three']);

  f.calls.length = 0;
  f.workspace.getTerminal('one').emitData('from-one');
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(f.calls, [{ method: 'inputSshTerminal', payload: { terminalId: 'one', data: 'from-one' } }]);

  f.calls.length = 0;
  f.workspace.activate('two', { focus: false });
  f.flushFrame();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(f.calls, [{ method: 'resizeSshTerminal', payload: { terminalId: 'two', cols: 121, rows: 37 } }]);
  assert.equal(f.workspace.getTerminal('one').addon.fitCount || 0, 0);
  assert.equal(f.workspace.getTerminal('two').addon.fitCount, 1);
});

test('keeps xterm instances and scrollback across tabs and selects a neighbor after close', async () => {
  const f = fixture();
  await f.workspace.initialize({ terminals: [
    { terminalId: 'one' }, { terminalId: 'two' }, { terminalId: 'three' },
  ], primaryTerminalId: 'one' });
  const firstInstance = f.workspace.getTerminal('two');
  firstInstance.write('persistent scrollback');
  f.workspace.activate('three', { focus: false });
  f.workspace.activate('two', { focus: false });
  assert.equal(f.workspace.getTerminal('two'), firstInstance);
  assert.deepEqual(firstInstance.writes, ['persistent scrollback']);

  f.responses.set('closeSshTerminal', {
    terminalId: 'two', closed: true,
    terminals: [{ terminalId: 'one' }, { terminalId: 'three' }],
  });
  await f.workspace.closeTerminal('two');
  assert.deepEqual(f.workspace.getTerminalIds(), ['one', 'three']);
  assert.equal(f.workspace.getActiveId(), 'three');
  assert.equal(firstInstance.disposed, true);
});

test('splits two existing terminals, fits both panes and keeps tab activation inside the split', async () => {
  const f = fixture();
  await f.workspace.initialize({
    terminals: [{ terminalId: 'one' }, { terminalId: 'two' }],
    primaryTerminalId: 'one',
  });
  assert.equal(await f.workspace.splitTerminal('two'), true);
  assert.deepEqual(f.workspace.getSplitState().terminalIds, ['two', 'one']);
  assert.equal(f.workspace.getActiveId(), 'two');
  assert.equal(f.paneContainer.classList.contains('split'), true);
  const visible = f.paneContainer.children
    .filter(node => node.classList.contains('ssh-terminal-pane') && !node.hidden)
    .map(node => node.dataset.terminalId);
  assert.deepEqual(visible.sort(), ['one', 'two']);

  f.flushFrame();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(f.calls.filter(call => call.method === 'resizeSshTerminal').map(call => call.payload.terminalId).sort(), ['one', 'two']);

  f.workspace.activate('one', { focus: false });
  assert.equal(f.workspace.getActiveId(), 'one');
  assert.deepEqual(f.workspace.getSplitState().terminalIds, ['two', 'one']);
  assert.equal(f.paneContainer.children.find(node => node.dataset.terminalId === 'two').hidden, false);
  assert.equal(f.paneContainer.children.find(node => node.dataset.terminalId === 'one').hidden, false);

  assert.equal(f.workspace.clearSplit({ keepId: 'one', focus: false }), true);
  assert.deepEqual(f.workspace.getSplitState().terminalIds, []);
  assert.equal(f.paneContainer.classList.contains('split'), false);
  assert.equal(f.paneContainer.children.find(node => node.dataset.terminalId === 'two').hidden, true);
});

test('terminal tab context menu toggles split and draggable separator changes the pane ratio', async () => {
  const f = fixture();
  await f.workspace.initialize({
    terminals: [{ terminalId: 'one' }, { terminalId: 'two' }],
    primaryTerminalId: 'one',
  });
  const secondTabShell = f.tabList.children.find(node => node.dataset.terminalId === 'two');
  secondTabShell.dispatch('contextmenu', { clientX: 120, clientY: 80 });
  const menu = f.document.body.children.find(node => node.classList.contains('ssh-terminal-context-menu'));
  assert(menu && !menu.hidden);
  assert.equal(menu.children[0].textContent, '拆分终端');
  menu.children[0].dispatch('click');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.paneContainer.classList.contains('split'), true);

  const splitter = f.paneContainer.children.find(node => node.classList.contains('ssh-terminal-pane-splitter'));
  splitter.dispatch('pointerdown', { button: 0, pointerId: 7, clientX: 500 });
  f.document.dispatch('pointermove', { pointerId: 7, clientX: 680 });
  f.document.dispatch('pointerup', { pointerId: 7, clientX: 680 });
  assert.equal(f.workspace.getSplitState().ratio, 0.68);
  assert.equal(f.paneContainer.style.getPropertyValue('--ssh-split-ratio'), '68%');
  assert.equal(splitter.attributes['aria-valuenow'], '68');
  assert.equal(f.workspace.setSplitRatio(0, {fit: false}), 0.2);
  assert.equal(f.workspace.setSplitRatio('invalid', {fit: false}), 0.5);

  secondTabShell.dispatch('contextmenu', { clientX: 120, clientY: 80 });
  assert.equal(menu.children[0].textContent, '取消拆分');
  menu.children[0].dispatch('click');
  assert.deepEqual(f.workspace.getSplitState().terminalIds, []);
});

test('selects the primary terminal only during initialization and preserves the active tab during state reconciliation', async () => {
  const f = fixture();
  const descriptors = [{ terminalId: 'one' }, { terminalId: 'two' }];
  await f.workspace.initialize({ terminals: descriptors, primaryTerminalId: 'one' });
  assert.equal(f.workspace.getActiveId(), 'one');

  f.workspace.activate('two', { focus: false });
  f.workspace.reconcile(descriptors, { preferredId: 'one' });
  assert.equal(f.workspace.getActiveId(), 'two', 'applyState reconciliation must not return to the runtime primary');

  f.workspace.handleEvent({
    type: 'status', target: 'ssh', state: 'connected',
    terminals: descriptors, primaryTerminalId: 'one',
  });
  assert.equal(f.workspace.getActiveId(), 'two', 'status reconciliation must preserve the selected SSH terminal');

  f.workspace.activate(AGENT_EXECUTION_TAB_ID, { focus: false });
  f.workspace.reconcile(descriptors, { preferredId: 'one' });
  assert.equal(f.workspace.getActiveId(), AGENT_EXECUTION_TAB_ID, 'state reconciliation must preserve Agent execution');

  f.workspace.handleEvent({
    type: 'status', target: 'ssh', state: 'connected',
    terminals: descriptors, primaryTerminalId: 'one',
  });
  assert.equal(f.workspace.getActiveId(), AGENT_EXECUTION_TAB_ID, 'status reconciliation must preserve Agent execution');

  await f.workspace.initialize({ terminals: descriptors, primaryTerminalId: 'one' });
  assert.equal(f.workspace.getActiveId(), 'one', 'explicit initialization/connect selects the primary terminal');
});

test('Agent execution tab accepts only agent remote_exec operations and safely upserts by id', () => {
  const f = fixture();
  const malicious = '<img src=x onerror=globalThis.compromised=true>';
  assert.equal(f.workspace.handleEvent({ type: 'operation', id: 'ignored-platform', origin: 'platform', kind: 'remote_exec', command: 'pwd' }), false);
  assert.equal(f.workspace.handleEvent({ type: 'operation', id: 'ignored-kind', origin: 'agent', kind: 'file_read', command: 'cat' }), false);
  assert.equal(f.workspace.getAgentOperationCount(), 0);

  assert.equal(f.workspace.handleEvent({ type: 'operation', id: 'op-1', origin: 'agent', kind: 'remote_exec', state: 'running', command: malicious }), true);
  assert.equal(f.workspace.handleEvent({ type: 'operation', id: 'op-1', origin: 'agent', kind: 'remote_exec', state: 'completed', stdout: 'done', exitCode: 0 }), true);
  assert.equal(f.workspace.getAgentOperationCount(), 1);

  const agentPane = f.paneContainer.children.find(node => node.dataset.terminalId === AGENT_EXECUTION_TAB_ID);
  const log = agentPane.children[1];
  assert.equal(log.children.length, 1);
  const output = log.children[0].children[1];
  assert.match(output.textContent, /<img src=x/);
  assert.match(output.textContent, /done/);
  assert.match(output.textContent, /退出码 0/);
  assert.equal(globalThis.compromised, undefined);
});

test('applies theme to every xterm and enforces the eight-terminal UI limit', async () => {
  const f = fixture();
  const descriptors = Array.from({ length: 8 }, (_, index) => ({ terminalId: `terminal-${index + 1}` }));
  await f.workspace.initialize({ terminals: descriptors, primaryTerminalId: 'terminal-1' });
  assert.equal(f.addButton.disabled, true);
  f.setTheme('light');
  f.workspace.applyTheme();
  for (const descriptor of descriptors) {
    const terminal = f.workspace.getTerminal(descriptor.terminalId);
    assert.deepEqual(terminal.options.theme, { name: 'light:ssh' });
    assert.equal(terminal.options.minimumContrastRatio, 4.5);
    assert.deepEqual(terminal.refreshed, [0, 23]);
  }
});

test('disconnected workspace keeps new-terminal available, reconnects before creation and retains Agent execution', async () => {
  const f = fixture({ connected: false, ensureConnectedResult: { reconnected: true } });
  await f.workspace.initialize({ terminals: [{ terminalId: 'stale' }] });
  assert.deepEqual(f.workspace.getTerminalIds(), ['stale']);
  assert.equal(f.addButton.disabled, false);
  f.workspace.handleEvent({ type: 'status', target: 'ssh', state: 'disconnected' });
  assert.deepEqual(f.workspace.getTerminalIds(), []);
  assert.equal(f.workspace.getActiveId(), AGENT_EXECUTION_TAB_ID);

  f.responses.set('listSshTerminals', {
    terminals: [{ terminalId: 'reconnected-terminal' }],
    primaryTerminalId: 'reconnected-terminal',
  });
  // A reconnect creates the runtime's primary terminal. The workspace must
  // adopt that shell instead of creating an unnecessary second one.
  await f.workspace.createTerminal();
  assert.equal(f.connectionAttempts.length, 1);
  assert.deepEqual(f.calls, [{ method: 'listSshTerminals', payload: undefined }]);
  assert.deepEqual(f.workspace.getTerminalIds(), ['reconnected-terminal']);
  assert.equal(f.workspace.getActiveId(), 'reconnected-terminal');
});

test('failed automatic SSH connection does not create a terminal and leaves retry available', async () => {
  const f = fixture({ connected: false, ensureConnectedResult: false });
  const result = await f.workspace.createTerminal();
  assert.equal(result, null);
  assert.equal(f.connectionAttempts.length, 1);
  assert.deepEqual(f.calls, []);
  assert.equal(f.addButton.disabled, false);
  assert.deepEqual(f.workspace.getTerminalIds(), []);
});

test('Main Process IPC allowlist exposes only the typed multi-terminal operations used by the renderer', () => {
  const source = fs.readFileSync(path.resolve(__dirname, '../main.cjs'), 'utf8');
  for (const method of ['createSshTerminal', 'listSshTerminals', 'inputSshTerminal', 'resizeSshTerminal', 'closeSshTerminal']) {
    assert.match(source, new RegExp(`['"]${method}['"]`), `${method} must be explicitly allowlisted`);
  }
  for (const method of ['uploadCalibrationMeasurements', 'discardCalibrationMeasurementUpload']) {
    assert.match(source, new RegExp(`['"]${method}['"]`), `${method} must be explicitly allowlisted`);
  }
});
