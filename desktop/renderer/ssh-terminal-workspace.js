'use strict';

const MAX_SSH_TERMINALS = 8;
const AGENT_EXECUTION_TAB_ID = 'agent-execution';
const MAX_PENDING_OUTPUT = 256 * 1024;
const MAX_AGENT_OPERATIONS = 100;

function asErrorMessage(error) {
  return String(error?.message || error || '终端操作失败。')
    .replace(/^Error invoking remote method '[^']+': Error: /, '');
}

function createElement(document, tagName, className = '', text = '') {
  const element = document.createElement(tagName);
  if (className) element.className = className;
  if (text) element.textContent = text;
  return element;
}

/**
 * Owns the SSH terminal tab strip and every xterm instance below it.
 *
 * Runtime contract:
 *   listSshTerminals() -> {terminals, primaryTerminalId}
 *   createSshTerminal({cols, rows}) -> {terminal, terminals}
 *   closeSshTerminal({terminalId}) -> {terminals}
 *   terminalInput/terminalResize always receive an explicit terminalId.
 *   ensureConnected() -> {reconnected}; reconnecting already creates the
 *   runtime primary terminal, which this controller adopts instead of cloning.
 *
 * Call handleEvent for every DesktopRuntime event, fitActive when its panel is
 * resized, applyTheme after the application theme changes, and dispose on
 * pagehide. Terminal/FitAddon are injected so this controller stays testable.
 */
function createSshTerminalWorkspace(options = {}) {
  const {
    tabList,
    paneContainer,
    addButton,
    invoke,
    terminalMinimumContrastRatioFor,
    terminalThemeFor,
    notice = () => {},
    isConnected = () => false,
    ensureConnected,
    getTheme = options.theme || (() => 'dark'),
    Terminal: TerminalClass,
    FitAddon: FitAddonClass,
    shouldProcessTerminalKey,
    scheduleFrame = callback => {
      if (typeof globalThis.requestAnimationFrame === 'function') return globalThis.requestAnimationFrame(callback);
      return globalThis.setTimeout(callback, 0);
    },
    cancelFrame = handle => {
      if (typeof globalThis.cancelAnimationFrame === 'function') globalThis.cancelAnimationFrame(handle);
      else globalThis.clearTimeout(handle);
    },
  } = options;

  if (!tabList || !paneContainer || !addButton) throw new TypeError('SSH 终端工作区缺少标签栏、面板容器或新建按钮。');
  if (typeof invoke !== 'function') throw new TypeError('SSH 终端工作区需要 invoke(method, payload)。');
  if (typeof terminalMinimumContrastRatioFor !== 'function') throw new TypeError('SSH 终端工作区需要 terminalMinimumContrastRatioFor(theme, target)。');
  if (typeof terminalThemeFor !== 'function') throw new TypeError('SSH 终端工作区需要 terminalThemeFor(theme, target)。');
  if (ensureConnected !== undefined && typeof ensureConnected !== 'function') throw new TypeError('SSH 终端工作区的 ensureConnected 必须是函数。');
  if (typeof TerminalClass !== 'function' || typeof FitAddonClass !== 'function') throw new TypeError('SSH 终端工作区需要 xterm Terminal 与 FitAddon。');

  const document = tabList.ownerDocument || paneContainer.ownerDocument || globalThis.document;
  if (!document?.createElement) throw new TypeError('SSH 终端工作区需要 DOM document。');

  const terminals = new Map();
  const pendingOutput = new Map();
  const agentOperations = new Map();
  let terminalOrder = [];
  let activeId = AGENT_EXECUTION_TAB_ID;
  let splitPair = [];
  let splitRatio = 0.5;
  let nextLabelNumber = 1;
  let creating = false;
  let disposed = false;
  let fitFrame;
  let contextTargetId = '';

  tabList.setAttribute('role', 'tablist');
  tabList.setAttribute('aria-label', 'SSH 终端');

  function insertTab(element) {
    if (addButton.parentNode === tabList) tabList.insertBefore(element, addButton);
    else tabList.append(element);
  }

  function makeTabShell(id, label, { closable = false } = {}) {
    const shell = createElement(document, 'div', 'ssh-terminal-tab-shell');
    shell.dataset.terminalId = id;
    const tab = createElement(document, 'button', 'ssh-terminal-tab', label);
    tab.type = 'button';
    tab.setAttribute('role', 'tab');
    tab.setAttribute('aria-selected', 'false');
    tab.dataset.terminalId = id;
    if (closable) tab.title = `${label}（右键可拆分）`;
    tab.addEventListener('click', () => activate(id));
    if (closable) {
      shell.addEventListener('contextmenu', event => {
        event.preventDefault();
        showContextMenu(event, id);
      });
    }
    shell.append(tab);
    let close;
    if (closable) {
      close = createElement(document, 'button', 'ssh-terminal-tab-close', '×');
      close.type = 'button';
      close.title = `关闭${label}`;
      close.setAttribute('aria-label', `关闭${label}`);
      close.addEventListener('click', event => {
        event.preventDefault();
        event.stopPropagation();
        void closeTerminal(id).catch(() => {});
      });
      shell.append(close);
    }
    insertTab(shell);
    return { shell, tab, close };
  }

  const agentTab = makeTabShell(AGENT_EXECUTION_TAB_ID, 'Agent 执行');
  const agentPane = createElement(document, 'section', 'ssh-terminal-pane agent-execution-pane');
  agentPane.dataset.terminalId = AGENT_EXECUTION_TAB_ID;
  agentPane.setAttribute('role', 'tabpanel');
  agentPane.setAttribute('aria-label', 'Agent 执行');
  const agentEmpty = createElement(document, 'p', 'agent-execution-empty', 'Code Agent 发起的 SSH 命令会显示在这里。');
  const agentLog = createElement(document, 'div', 'agent-execution-log');
  agentLog.setAttribute('aria-live', 'polite');
  agentPane.append(agentEmpty, agentLog);
  paneContainer.append(agentPane);

  const paneSplitter = createElement(document, 'div', 'ssh-terminal-pane-splitter');
  paneSplitter.hidden = true;
  paneSplitter.setAttribute('role', 'separator');
  paneSplitter.setAttribute('aria-label', '调整拆分终端宽度');
  paneSplitter.setAttribute('aria-orientation', 'vertical');
  paneSplitter.setAttribute('aria-valuemin', '20');
  paneSplitter.setAttribute('aria-valuemax', '80');
  paneSplitter.setAttribute('tabindex', '0');
  paneContainer.append(paneSplitter);

  const contextMenu = createElement(document, 'div', 'ssh-terminal-context-menu');
  contextMenu.hidden = true;
  contextMenu.setAttribute('role', 'menu');
  const splitMenuItem = createElement(document, 'button', 'ssh-terminal-context-item', '拆分终端');
  splitMenuItem.type = 'button';
  splitMenuItem.setAttribute('role', 'menuitem');
  contextMenu.append(splitMenuItem);
  (document.body || paneContainer).append(contextMenu);

  function refreshControls() {
    const canConnect = isConnected() || typeof ensureConnected === 'function';
    addButton.disabled = disposed || creating || !canConnect || terminals.size >= MAX_SSH_TERMINALS;
    addButton.title = terminals.size >= MAX_SSH_TERMINALS
      ? `最多可同时打开 ${MAX_SSH_TERMINALS} 个 SSH 终端`
      : (isConnected() ? '新建 SSH 终端' : '连接并新建 SSH 终端');
  }

  function currentTheme() {
    try { return typeof getTheme === 'function' ? getTheme() : getTheme; }
    catch { return 'dark'; }
  }

  function visibleTerminalIds() {
    if (activeId === AGENT_EXECUTION_TAB_ID) return [];
    if (splitPair.length === 2) return splitPair.filter(id => terminals.has(id));
    return terminals.has(activeId) ? [activeId] : [];
  }

  function renderTerminalLayout() {
    const visibleIds = visibleTerminalIds();
    const split = visibleIds.length === 2;
    paneContainer.classList.toggle('split', split);
    if (paneContainer.style?.setProperty) paneContainer.style.setProperty('--ssh-split-ratio', `${Math.round(splitRatio * 10000) / 100}%`);
    paneSplitter.hidden = !split;
    paneSplitter.setAttribute('aria-valuenow', String(Math.round(splitRatio * 100)));

    const agentSelected = activeId === AGENT_EXECUTION_TAB_ID;
    agentTab.tab.classList.toggle('active', agentSelected);
    agentTab.tab.setAttribute('aria-selected', String(agentSelected));
    agentTab.shell.classList.toggle('active', agentSelected);
    agentPane.hidden = !agentSelected;

    for (const record of terminals.values()) {
      const selected = record.terminalId === activeId;
      const visibleIndex = visibleIds.indexOf(record.terminalId);
      record.tab.classList.toggle('active', selected);
      record.tab.setAttribute('aria-selected', String(selected));
      record.tabShell.classList.toggle('active', selected);
      record.tabShell.classList.toggle('split-visible', visibleIndex >= 0);
      record.pane.hidden = visibleIndex < 0;
      record.pane.classList.toggle('split-primary', split && visibleIndex === 0);
      record.pane.classList.toggle('split-secondary', split && visibleIndex === 1);
    }
  }

  function scheduleActiveFit({ focus = false } = {}) {
    if (disposed || fitFrame !== undefined) return;
    fitFrame = scheduleFrame(() => {
      fitFrame = undefined;
      if (disposed) return;
      void fitActive({ focus });
    });
  }

  function activate(id, { focus = true } = {}) {
    if (id !== AGENT_EXECUTION_TAB_ID && !terminals.has(id)) return false;
    if (id === AGENT_EXECUTION_TAB_ID) splitPair = [];
    else if (splitPair.length === 2 && !splitPair.includes(id)) {
      const replacedIndex = Math.max(0, splitPair.indexOf(activeId));
      splitPair[replacedIndex] = id;
    }
    activeId = id;
    renderTerminalLayout();
    if (id !== AGENT_EXECUTION_TAB_ID) scheduleActiveFit({ focus });
    return true;
  }

  function setSplitRatio(value, { fit = true } = {}) {
    const numeric = Number(value);
    const next = Math.min(0.8, Math.max(0.2, Number.isFinite(numeric) ? numeric : 0.5));
    splitRatio = next;
    if (paneContainer.style?.setProperty) paneContainer.style.setProperty('--ssh-split-ratio', `${Math.round(next * 10000) / 100}%`);
    paneSplitter.setAttribute('aria-valuenow', String(Math.round(next * 100)));
    if (fit && splitPair.length === 2) scheduleActiveFit();
    return splitRatio;
  }

  function applySplit(firstId, secondId, { focusId = firstId } = {}) {
    const first = String(firstId || '');
    const second = String(secondId || '');
    if (!first || !second || first === second || !terminals.has(first) || !terminals.has(second)) return false;
    splitPair = [first, second];
    activeId = terminals.has(focusId) && splitPair.includes(focusId) ? focusId : first;
    renderTerminalLayout();
    scheduleActiveFit({ focus: true });
    return true;
  }

  function clearSplit({ keepId = activeId, focus = true } = {}) {
    if (!splitPair.length) return false;
    const next = terminals.has(keepId) ? keepId : splitPair.find(id => terminals.has(id)) || terminalOrder[0] || AGENT_EXECUTION_TAB_ID;
    splitPair = [];
    activeId = next;
    renderTerminalLayout();
    if (next !== AGENT_EXECUTION_TAB_ID) scheduleActiveFit({ focus });
    return true;
  }

  async function splitTerminal(terminalId) {
    const id = String(terminalId || '');
    if (!terminals.has(id)) return false;
    if (splitPair.length === 2) {
      activate(id);
      return true;
    }
    let companion = activeId !== AGENT_EXECUTION_TAB_ID && activeId !== id && terminals.has(activeId)
      ? activeId
      : terminalOrder.find(candidate => candidate !== id && terminals.has(candidate));
    if (!companion) {
      const result = await createTerminal();
      companion = String(result?.terminal?.terminalId || '');
      if (!terminals.has(companion)) companion = terminalOrder.find(candidate => candidate !== id && terminals.has(candidate));
    }
    if (!companion) return false;
    return applySplit(id, companion, { focusId: id });
  }

  function hideContextMenu() {
    contextMenu.hidden = true;
    contextTargetId = '';
  }

  function showContextMenu(event, terminalId) {
    const id = String(terminalId || '');
    if (!terminals.has(id)) return;
    contextTargetId = id;
    const alreadySplit = splitPair.length === 2 && splitPair.includes(id);
    splitMenuItem.textContent = alreadySplit ? '取消拆分' : '拆分终端';
    contextMenu.hidden = false;
    if (contextMenu.style) {
      contextMenu.style.left = `${Math.max(4, Number(event?.clientX) || 0)}px`;
      contextMenu.style.top = `${Math.max(4, Number(event?.clientY) || 0)}px`;
    }
    splitMenuItem.focus?.();
  }

  function onSplitMenuClick(event) {
    event?.preventDefault?.();
    event?.stopPropagation?.();
    const id = contextTargetId;
    const alreadySplit = splitPair.length === 2 && splitPair.includes(id);
    hideContextMenu();
    if (alreadySplit) clearSplit({ keepId: id });
    else void splitTerminal(id).catch(error => notice(asErrorMessage(error), 'error'));
  }

  function onDocumentPointerDown(event) {
    if (!contextMenu.hidden && event?.target !== contextMenu && event?.target !== splitMenuItem) hideContextMenu();
  }

  function onDocumentKeyDown(event) {
    if (event?.key === 'Escape') hideContextMenu();
  }

  function ratioFromClientX(clientX) {
    const bounds = paneContainer.getBoundingClientRect?.();
    if (!bounds || !bounds.width) return splitRatio;
    return (Number(clientX) - bounds.left) / bounds.width;
  }

  function onSplitterPointerDown(event) {
    if (splitPair.length !== 2 || event?.button > 0) return;
    event.preventDefault?.();
    paneSplitter.setPointerCapture?.(event.pointerId);
    const onMove = moveEvent => {
      moveEvent.preventDefault?.();
      setSplitRatio(ratioFromClientX(moveEvent.clientX));
    };
    const onEnd = endEvent => {
      paneSplitter.releasePointerCapture?.(endEvent?.pointerId);
      document.removeEventListener?.('pointermove', onMove);
      document.removeEventListener?.('pointerup', onEnd);
      document.removeEventListener?.('pointercancel', onEnd);
      scheduleActiveFit();
    };
    document.addEventListener?.('pointermove', onMove);
    document.addEventListener?.('pointerup', onEnd);
    document.addEventListener?.('pointercancel', onEnd);
  }

  function onSplitterKeyDown(event) {
    if (!['ArrowLeft', 'ArrowRight', 'Home'].includes(event?.key)) return;
    event.preventDefault?.();
    if (event.key === 'Home') setSplitRatio(0.5);
    else setSplitRatio(splitRatio + (event.key === 'ArrowRight' ? 0.05 : -0.05));
  }

  function onSplitterDoubleClick() {
    setSplitRatio(0.5);
  }

  function terminalOptions() {
    return {
      cursorBlink: true,
      cursorStyle: 'bar',
      fontFamily: '"Cascadia Code", "Cascadia Mono", Consolas, "Microsoft YaHei UI", monospace',
      fontSize: 12,
      lineHeight: 1.35,
      scrollback: 6000,
      allowProposedApi: false,
      minimumContrastRatio: terminalMinimumContrastRatioFor(currentTheme(), 'ssh'),
      theme: terminalThemeFor(currentTheme(), 'ssh'),
    };
  }

  function flushPendingOutput(record) {
    const buffered = pendingOutput.get(record.terminalId);
    pendingOutput.delete(record.terminalId);
    if (buffered) record.terminal.write(buffered);
  }

  function ensureTerminal(descriptor = {}) {
    const terminalId = String(descriptor.terminalId || '');
    if (!terminalId) return null;
    const existing = terminals.get(terminalId);
    if (existing) {
      existing.descriptor = { ...existing.descriptor, ...descriptor };
      return existing;
    }
    const labelNumber = nextLabelNumber++;
    const label = `终端 ${labelNumber}`;
    const { shell: tabShell, tab, close } = makeTabShell(terminalId, label, { closable: true });
    const pane = createElement(document, 'section', 'ssh-terminal-pane');
    pane.hidden = true;
    pane.dataset.terminalId = terminalId;
    pane.setAttribute('role', 'tabpanel');
    pane.setAttribute('aria-label', label);
    const host = createElement(document, 'div', 'ssh-terminal-host');
    host.dataset.terminalId = terminalId;
    pane.append(host);
    paneContainer.append(pane);

    const terminal = new TerminalClass(terminalOptions());
    const fit = new FitAddonClass();
    terminal.loadAddon(fit);
    terminal.open(host);
    if (typeof shouldProcessTerminalKey === 'function' && typeof terminal.attachCustomKeyEventHandler === 'function') {
      terminal.attachCustomKeyEventHandler(event => shouldProcessTerminalKey(event, terminal.hasSelection?.() || false));
    }
    const record = {
      terminalId,
      descriptor: { ...descriptor, terminalId },
      label,
      tabShell,
      tab,
      close,
      pane,
      host,
      terminal,
      fit,
      lastSize: null,
      subscriptions: [],
    };
    record.subscriptions.push(terminal.onData(data => {
      if (disposed || !isConnected() || !terminals.has(terminalId)) return;
      void invoke('inputSshTerminal', { terminalId, data }).catch(error => notice(asErrorMessage(error), 'error'));
    }));
    terminals.set(terminalId, record);
    terminalOrder.push(terminalId);
    flushPendingOutput(record);
    refreshControls();
    return record;
  }

  function removeTerminal(terminalId, { selectNeighbor = true } = {}) {
    const record = terminals.get(terminalId);
    if (!record) {
      pendingOutput.delete(terminalId);
      return false;
    }
    const removedIndex = terminalOrder.indexOf(terminalId);
    const wasActive = activeId === terminalId;
    const wasSplit = splitPair.includes(terminalId);
    const splitNeighbor = wasSplit ? splitPair.find(id => id !== terminalId && terminals.has(id)) : '';
    terminals.delete(terminalId);
    if (contextTargetId === terminalId) hideContextMenu();
    terminalOrder = terminalOrder.filter(id => id !== terminalId);
    if (wasSplit) splitPair = [];
    pendingOutput.delete(terminalId);
    for (const subscription of record.subscriptions) subscription?.dispose?.();
    record.terminal.dispose?.();
    record.tabShell.remove();
    record.pane.remove();
    if (wasActive && selectNeighbor) {
      const neighborId = splitNeighbor || terminalOrder[Math.min(removedIndex, terminalOrder.length - 1)] || AGENT_EXECUTION_TAB_ID;
      activate(neighborId, { focus: neighborId !== AGENT_EXECUTION_TAB_ID });
    } else renderTerminalLayout();
    refreshControls();
    return true;
  }

  function reconcile(descriptors = [], { preferredId, activatePreferred = false } = {}) {
    const normalized = Array.isArray(descriptors)
      ? descriptors.filter(item => item && item.terminalId).slice(0, MAX_SSH_TERMINALS)
      : [];
    const ids = new Set(normalized.map(item => String(item.terminalId)));
    for (const id of [...terminals.keys()]) if (!ids.has(id)) removeTerminal(id);
    const nextOrder = [];
    for (const descriptor of normalized) {
      const record = ensureTerminal(descriptor);
      if (record) nextOrder.push(record.terminalId);
    }
    terminalOrder = nextOrder;
    // Runtime snapshots always carry the primary shell as connection metadata.
    // Selecting it is opt-in so routine applyState/status reconciliation cannot
    // override the tab the user is currently reading.
    const preferred = String(preferredId || '');
    if (activatePreferred && preferred && terminals.has(preferred)) activate(preferred, { focus: false });
    else if (activeId !== AGENT_EXECUTION_TAB_ID && !terminals.has(activeId)) activate(terminalOrder[0] || AGENT_EXECUTION_TAB_ID, { focus: false });
    refreshControls();
  }

  async function listTerminals({ activatePrimary = false } = {}) {
    if (disposed) return { terminals: [] };
    if (!isConnected()) {
      reconcile([]);
      return { terminals: [] };
    }
    try {
      const result = await invoke('listSshTerminals');
      reconcile(result?.terminals, {
        preferredId: result?.primaryTerminalId,
        activatePreferred: activatePrimary,
      });
      return result || { terminals: [] };
    } catch (error) {
      notice(asErrorMessage(error), 'error');
      throw error;
    }
  }

  async function createTerminal() {
    if (disposed || creating) return null;
    if (terminals.size >= MAX_SSH_TERMINALS) {
      notice(`最多可同时打开 ${MAX_SSH_TERMINALS} 个 SSH 终端。`, 'error');
      return null;
    }
    creating = true;
    refreshControls();
    try {
      const connection = typeof ensureConnected === 'function'
        ? await ensureConnected()
        : { reconnected: false };
      if (!isConnected()) {
        notice('SSH 服务器尚未连接，请先完成连接配置。', 'error');
        return null;
      }
      // connectSsh establishes the primary PTY itself. Reconcile and reuse it
      // so one click while offline yields one usable terminal instead of two.
      if (connection?.reconnected) return listTerminals({ activatePrimary: true });
      const result = await invoke('createSshTerminal', { cols: 100, rows: 28 });
      if (result?.terminals) reconcile(result.terminals);
      const record = ensureTerminal(result?.terminal);
      if (record) activate(record.terminalId);
      return result;
    } catch (error) {
      notice(asErrorMessage(error), 'error');
      throw error;
    } finally {
      creating = false;
      refreshControls();
    }
  }

  async function closeTerminal(terminalId) {
    const id = String(terminalId || '');
    if (!terminals.has(id)) return null;
    try {
      const result = await invoke('closeSshTerminal', { terminalId: id });
      // The lifecycle event normally removes the pane before the IPC promise
      // resolves. This fallback keeps the UI correct when events are delayed.
      removeTerminal(id);
      if (result?.terminals) reconcile(result.terminals);
      return result;
    } catch (error) {
      notice(asErrorMessage(error), 'error');
      throw error;
    }
  }

  async function fitActive({ focus = false } = {}) {
    if (disposed || activeId === AGENT_EXECUTION_TAB_ID) return null;
    const results = [];
    for (const terminalId of visibleTerminalIds()) {
      const record = terminals.get(terminalId);
      if (!record || record.pane.hidden) continue;
      try { record.fit.fit(); } catch { continue; }
      if (focus && terminalId === activeId) record.terminal.focus?.();
      const cols = Number(record.terminal.cols);
      const rows = Number(record.terminal.rows);
      if (!Number.isInteger(cols) || !Number.isInteger(rows) || cols < 1 || rows < 1) continue;
      const size = `${cols}:${rows}`;
      if (record.lastSize === size || !isConnected()) {
        results.push({ terminalId: record.terminalId, cols, rows });
        continue;
      }
      record.lastSize = size;
      try {
        results.push(await invoke('resizeSshTerminal', { terminalId: record.terminalId, cols, rows }));
      } catch (error) {
        record.lastSize = null;
        notice(asErrorMessage(error), 'error');
      }
    }
    return results.length <= 1 ? (results[0] || null) : results;
  }

  function applyTheme() {
    const theme = terminalThemeFor(currentTheme(), 'ssh');
    const minimumContrastRatio = terminalMinimumContrastRatioFor(currentTheme(), 'ssh');
    for (const record of terminals.values()) {
      record.terminal.options.theme = { ...theme };
      record.terminal.options.minimumContrastRatio = minimumContrastRatio;
      record.terminal.refresh?.(0, Math.max(0, Number(record.terminal.rows || 1) - 1));
    }
    scheduleActiveFit();
  }

  function bufferOutput(terminalId, data) {
    const prior = pendingOutput.get(terminalId) || '';
    pendingOutput.set(terminalId, (prior + data).slice(-MAX_PENDING_OUTPUT));
  }

  function operationText(event) {
    const parts = [];
    if (event.command) parts.push(`$ ${event.command}`);
    if (event.stdout) parts.push(String(event.stdout));
    if (event.stderr) parts.push(String(event.stderr));
    if (event.message) parts.push(String(event.message));
    if (event.durationMs !== undefined || event.exitCode !== undefined) {
      parts.push([
        event.durationMs !== undefined ? `${event.durationMs} ms` : '',
        event.exitCode !== undefined ? `退出码 ${event.exitCode}` : '',
      ].filter(Boolean).join(' · '));
    }
    return parts.join('\n') || '等待远程执行结果…';
  }

  function upsertAgentOperation(event) {
    if (event?.type !== 'operation' || event.origin !== 'agent' || event.kind !== 'remote_exec' || !event.id) return false;
    const id = String(event.id);
    let record = agentOperations.get(id);
    if (!record) {
      const article = createElement(document, 'article', 'agent-execution-record');
      article.dataset.operationId = id;
      const header = createElement(document, 'div', 'agent-execution-record-header');
      const state = createElement(document, 'span', 'agent-execution-state');
      const time = createElement(document, 'time', 'agent-execution-time');
      const output = createElement(document, 'pre', 'agent-execution-output');
      header.append(state, time);
      article.append(header, output);
      agentLog.append(article);
      record = { article, state, time, output, event: {} };
      agentOperations.set(id, record);
      while (agentOperations.size > MAX_AGENT_OPERATIONS) {
        const [oldestId, oldest] = agentOperations.entries().next().value;
        oldest.article.remove();
        agentOperations.delete(oldestId);
      }
    }
    record.event = { ...record.event, ...event };
    const labels = { running: '执行中', completed: '已完成', failed: '执行失败', error: '错误' };
    record.state.textContent = labels[record.event.state] || String(record.event.state || '记录');
    record.state.className = `agent-execution-state ${['failed', 'error'].includes(record.event.state) ? 'error' : ''}`.trim();
    record.time.textContent = new Date().toLocaleTimeString('zh-CN', { hour12: false });
    // textContent is deliberate: command output is untrusted remote text.
    record.output.textContent = operationText(record.event).slice(0, 256 * 1024);
    agentEmpty.hidden = true;
    agentLog.scrollTop = agentLog.scrollHeight;
    return true;
  }

  function handleEvent(event) {
    if (disposed || !event) return false;
    if (upsertAgentOperation(event)) return true;
    if (event.type === 'terminal' && event.target === 'ssh') {
      const terminalId = String(event.terminalId || '');
      if (!terminalId) return false;
      const data = String(event.data || '');
      const record = terminals.get(terminalId);
      if (record) record.terminal.write(data);
      else bufferOutput(terminalId, data);
      return true;
    }
    if (event.type === 'ssh-terminal' && event.target === 'ssh') {
      const terminalId = String(event.terminalId || event.terminal?.terminalId || '');
      if (event.action === 'closed') removeTerminal(terminalId);
      else if (event.action === 'created') {
        const hadTerminals = terminals.size > 0;
        const record = ensureTerminal(event.terminal || { terminalId });
        if (record && !hadTerminals && activeId === AGENT_EXECUTION_TAB_ID && !agentOperations.size) activate(record.terminalId, { focus: false });
      }
      if (Array.isArray(event.terminals)) reconcile(event.terminals);
      return true;
    }
    if (event.type === 'status' && event.target === 'ssh') {
      if (Array.isArray(event.terminals)) reconcile(event.terminals);
      else if (['disconnected', 'error'].includes(event.state)) reconcile([]);
      refreshControls();
      return true;
    }
    return false;
  }

  function onAddClick() {
    void createTerminal().catch(() => {});
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    if (fitFrame !== undefined) cancelFrame(fitFrame);
    fitFrame = undefined;
    addButton.removeEventListener('click', onAddClick);
    splitMenuItem.removeEventListener('click', onSplitMenuClick);
    paneSplitter.removeEventListener('pointerdown', onSplitterPointerDown);
    paneSplitter.removeEventListener('keydown', onSplitterKeyDown);
    paneSplitter.removeEventListener('dblclick', onSplitterDoubleClick);
    document.removeEventListener?.('pointerdown', onDocumentPointerDown);
    document.removeEventListener?.('keydown', onDocumentKeyDown);
    for (const id of [...terminals.keys()]) removeTerminal(id, { selectNeighbor: false });
    pendingOutput.clear();
    agentOperations.clear();
    agentTab.shell.remove();
    agentPane.remove();
    paneSplitter.remove();
    contextMenu.remove();
  }

  addButton.addEventListener('click', onAddClick);
  splitMenuItem.addEventListener('click', onSplitMenuClick);
  paneSplitter.addEventListener('pointerdown', onSplitterPointerDown);
  paneSplitter.addEventListener('keydown', onSplitterKeyDown);
  paneSplitter.addEventListener('dblclick', onSplitterDoubleClick);
  document.addEventListener?.('pointerdown', onDocumentPointerDown);
  document.addEventListener?.('keydown', onDocumentKeyDown);
  activate(AGENT_EXECUTION_TAB_ID, { focus: false });
  refreshControls();

  return {
    initialize: snapshot => snapshot?.terminals
      ? (reconcile(snapshot.terminals, {
        preferredId: snapshot.primaryTerminalId,
        activatePreferred: true,
      }), Promise.resolve(snapshot))
      : listTerminals({ activatePrimary: true }),
    listTerminals,
    createTerminal,
    closeTerminal,
    splitTerminal,
    clearSplit,
    setSplitRatio,
    activate,
    reconcile,
    handleEvent,
    fitActive,
    applyTheme,
    refreshControls,
    dispose,
    getActiveId: () => activeId,
    getSplitState: () => ({ terminalIds: [...splitPair], ratio: splitRatio }),
    getTerminalIds: () => [...terminalOrder],
    getTerminal: terminalId => terminals.get(String(terminalId || ''))?.terminal || null,
    getAgentOperationCount: () => agentOperations.size,
    constants: { AGENT_EXECUTION_TAB_ID, MAX_SSH_TERMINALS },
  };
}

module.exports = {
  AGENT_EXECUTION_TAB_ID,
  MAX_SSH_TERMINALS,
  createSshTerminalWorkspace,
};
