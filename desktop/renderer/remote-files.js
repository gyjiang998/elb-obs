import './remote-files.css';
import remoteTreeRefresh from './remote-tree-refresh.cjs';
import remoteTreeCache from './remote-tree-cache.cjs';
import remoteEditorTabs from './remote-editor-tabs.cjs';
import workspaceAsyncState from './workspace-async-state.cjs';
import { createRemoteCodeEditor } from './remote-code-editor.js';
import { createRemoteMarkdownPreview } from './remote-markdown-preview.jsx';

const {RecursiveRefreshTimer} = remoteTreeRefresh;
const {RemoteTreeCacheMetadata, isPathWithin, normalizeConnectionIdentity} = remoteTreeCache;
const {RemoteEditorTabStore, tabIsDirty, tabNeedsRead} = remoteEditorTabs;
const {WorkspaceReadTokens, WorkspaceSearchSession, sameWorkspaceBinding} = workspaceAsyncState;

const byId = (id) => document.getElementById(id);
const REMOTE_TREE_REFRESH_INTERVAL_MS = 10 * 60 * 1000;
const REMOTE_EXPLORER_WIDTH_KEY = 'heterogeneous-workbench.remote-explorer-width';
const REMOTE_EXPLORER_DEFAULT_WIDTH = 248;
const REMOTE_TREE_PREFETCH_OPTIONS = Object.freeze({
  maxDepth: 5,
  maxDirectories: 192,
  maxEntries: 5000,
  concurrency: 4,
  timeBudgetMs: 15000,
});

function joinPath(parent, name) {
  return parent ? `${parent}/${name}` : name;
}

function formatSize(value) {
  const size = Number(value);
  if (!Number.isFinite(size) || size < 0) return '';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(size < 10240 ? 1 : 0)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function textFormatLabel(file = {}) {
  const encoding = file.bom ? 'UTF-8 BOM' : 'UTF-8';
  const endings = { lf: 'LF', crlf: 'CRLF', cr: 'CR', mixed: '混合换行' };
  return `${encoding} · ${endings[file.lineEnding] || 'LF'}`;
}

function isMarkdownPath(value) {
  return /\.(?:md|markdown|mdown|mkd|mmd)$/i.test(String(value || ''));
}

function parentPath(value) {
  const normalized = String(value || '');
  return normalized.includes('/') ? normalized.slice(0, normalized.lastIndexOf('/')) : '';
}

function basename(value) {
  return String(value || '').split('/').at(-1) || '';
}

function remoteConnectionExpectation(ssh = {}) {
  const generation = Number(ssh.connectionGeneration);
  if (ssh.state !== 'connected' || !ssh.host || !ssh.username
      || !Number.isSafeInteger(generation)) return null;
  return {
    target: {
      host: String(ssh.host),
      port: Number(ssh.port) || 22,
      username: String(ssh.username),
      cwd: String(ssh.cwd || '~'),
    },
    root: String(ssh.remoteRootPath || ssh.cwd || '~'),
    generation,
  };
}

export function createRemoteFileWorkspace({
  invoke,
  uploadDroppedFiles = window.desktop?.uploadDroppedFiles,
  onError = () => {},
} = {}) {
  const elements = {
    workspace: byId('remote-workspace'), explorer: byId('remote-explorer'), splitter: byId('remote-explorer-splitter'),
    tree: byId('remote-file-tree'), root: byId('remote-root'), rootLabel: byId('remote-root-label'), refresh: byId('remote-files-refresh'),
    editor: byId('remote-file-editor'), empty: byId('remote-editor-empty'), tabs: byId('remote-editor-tabs'),
    state: byId('remote-editor-state'), path: byId('remote-editor-path'), save: byId('remote-file-save'),
    position: byId('remote-editor-position'), meta: byId('remote-editor-meta'),
    preview: byId('remote-markdown-preview'), previewToggle: byId('remote-markdown-toggle'),
    sourceTitle: byId('workspace-source-title'), sourceRemote: byId('workspace-source-remote'),
    sourceLocal: byId('workspace-source-local'), chooseLocal: byId('local-workspace-select'),
    searchQuery: byId('workspace-search-query'), searchCase: byId('workspace-search-case'),
    searchReplaceToggle: byId('workspace-search-replace-toggle'), searchCancel: byId('workspace-search-cancel'),
    replaceRow: byId('workspace-replace-row'), replaceValue: byId('workspace-replace-value'),
    replaceAll: byId('workspace-replace-all'), searchStatus: byId('workspace-search-status'),
    searchResults: byId('workspace-search-results'),
    uploadStatus: byId('remote-upload-status'),
  };
  if (Object.values(elements).some((element) => !element)) throw new Error('Remote file workspace containers are missing');

  const cache = new Map();
  const cacheMetadata = new RemoteTreeCacheMetadata();
  const directoryLoadTokens = new Map();
  const expanded = new Set(['']);
  const listeners = [];
  let connected = false;
  let identity = '';
  let cwd = '';
  let expectedConnection = null;
  let sourceMode = 'remote';
  let remoteSsh = {};
  let remoteIdentity = 'remote:disconnected';
  let localGrant = null;
  let searchTimer = null;
  let searchMatches = [];
  let searchTruncated = false;
  const searchSession = new WorkspaceSearchSession();
  const readTokens = new WorkspaceReadTokens();
  const tabs = new RemoteEditorTabStore();
  let focusedTreePath = '';
  let requestGeneration = 0;
  let treeLoadCount = 0;
  let treeRefreshTask = null;
  let queuedTreeRefresh = null;
  let disposed = false;
  let explorerWidth = REMOTE_EXPLORER_DEFAULT_WIDTH;
  let copiedFile = null;
  let contextMenu = null;
  let dropTargetElement = null;
  let uploadInProgress = false;
  let uploadStatusTimer = null;
  const periodicTreeRefresh = new RecursiveRefreshTimer({
    intervalMs: REMOTE_TREE_REFRESH_INTERVAL_MS,
    setTimer: (callback, delay) => window.setTimeout(callback, delay),
    clearTimer: timer => window.clearTimeout(timer),
    run: () => queueTreeRefresh({reportError: false, required: false}),
  });

  try {
    const storedWidth = Number(window.localStorage.getItem(REMOTE_EXPLORER_WIDTH_KEY));
    if (Number.isFinite(storedWidth) && storedWidth > 0) explorerWidth = storedWidth;
  } catch { /* A restricted renderer can still use the in-memory default. */ }

  const listen = (target, event, handler) => {
    target.addEventListener(event, handler);
    listeners.push(() => target.removeEventListener(event, handler));
  };
  const activeTab = () => tabs.active;
  const connectionFence = () => {
    if (sourceMode === 'local') {
      if (!localGrant?.grantId) throw new Error('请先选择本地工作区。');
      return {grantId: localGrant.grantId};
    }
    if (!expectedConnection) {
      if (connected) throw new Error('当前 SSH 连接身份尚未同步，请等待连接状态刷新后重试。');
      return {};
    }
    return {expectedConnection: {
      target: {...expectedConnection.target},
      root: expectedConnection.root,
      generation: expectedConnection.generation,
    }};
  };
  const sourceInvoke = (remoteMethod, localMethod, payload = {}) => invoke(
    sourceMode === 'local' ? localMethod : remoteMethod,
    {...payload, ...connectionFence()},
  );
  const sourceLabel = (mode = sourceMode) => mode === 'local' ? '本地' : 'SSH';
  const workspaceBinding = ({query = '', caseSensitive = false} = {}) => ({
    query,
    caseSensitive,
    source: sourceMode,
    identity,
    connection: sourceMode === 'remote' && expectedConnection ? {
      target: {...expectedConnection.target},
      root: expectedConnection.root,
      generation: expectedConnection.generation,
    } : null,
    grantId: sourceMode === 'local' ? String(localGrant?.grantId || '') : '',
  });
  const currentSearchBinding = () => workspaceBinding({
    query: elements.searchQuery.value,
    caseSensitive: elements.searchCase.getAttribute('aria-pressed') === 'true',
  });
  const currentReadBinding = () => workspaceBinding();
  const bindingFence = binding => binding.source === 'local'
    ? {grantId: binding.grantId}
    : {expectedConnection: binding.connection};
  const invokeForBinding = (binding, remoteMethod, localMethod, payload = {}) => invoke(
    binding.source === 'local' ? localMethod : remoteMethod,
    {...payload, ...bindingFence(binding)},
  );
  const isDirty = (tab = activeTab()) => tabIsDirty(tab);
  const clampExplorerWidth = (value) => {
    const available = Math.max(180, elements.workspace.clientWidth - 280);
    return Math.round(Math.min(Math.max(180, Number(value) || REMOTE_EXPLORER_DEFAULT_WIDTH), Math.min(420, available)));
  };
  const applyExplorerWidth = (value, {persist = false} = {}) => {
    explorerWidth = clampExplorerWidth(value);
    elements.workspace.style.setProperty('--remote-explorer-width', `${explorerWidth}px`);
    elements.splitter.setAttribute('aria-valuenow', String(explorerWidth));
    if (persist) {
      try { window.localStorage.setItem(REMOTE_EXPLORER_WIDTH_KEY, String(explorerWidth)); }
      catch { /* Layout persistence is optional. */ }
    }
    editorController?.layout();
  };
  const setEditorMessage = (message) => { elements.state.textContent = message; };
  const showUploadStatus = (message, {tone = '', hideAfterMs = 0} = {}) => {
    if (uploadStatusTimer) clearTimeout(uploadStatusTimer);
    uploadStatusTimer = null;
    elements.uploadStatus.textContent = message;
    elements.uploadStatus.dataset.tone = tone;
    elements.uploadStatus.hidden = !message;
    if (message && hideAfterMs > 0) uploadStatusTimer = window.setTimeout(() => {
      uploadStatusTimer = null;
      elements.uploadStatus.hidden = true;
      elements.uploadStatus.textContent = '';
      delete elements.uploadStatus.dataset.tone;
    }, hideAfterMs);
  };
  const clearDropTarget = ({keepStatus = false} = {}) => {
    dropTargetElement?.classList.remove('drop-target');
    dropTargetElement = null;
    elements.explorer.classList.remove('upload-dragging');
    if (!keepStatus && !uploadInProgress) showUploadStatus('');
  };
  const isFileDrag = event => Array.from(event.dataTransfer?.types || []).includes('Files');
  const uploadTargetForEvent = event => {
    if (sourceMode !== 'remote' || !connected || uploadInProgress || !isFileDrag(event)) return null;
    const row = event.target.closest?.('.remote-tree-row');
    if (row) {
      if (row.dataset.directory !== 'true' || row.dataset.dropEnabled !== 'true') return null;
      return {directory: row.dataset.path, element: row, label: row.dataset.path};
    }
    if (elements.root.contains(event.target) || elements.tree.contains(event.target))
      return {directory: '', element: elements.root, label: '远程工作目录'};
    return null;
  };
  const displayUploadTarget = target => {
    if (dropTargetElement !== target?.element) {
      dropTargetElement?.classList.remove('drop-target');
      dropTargetElement = target?.element || null;
      dropTargetElement?.classList.add('drop-target');
    }
    elements.explorer.classList.toggle('upload-dragging', Boolean(target));
    if (target) showUploadStatus(`释放以上传到 ${target.label}`);
    else if (!uploadInProgress) showUploadStatus('');
  };
  const setTreeEmpty = (message, tone = '') => {
    const node = document.createElement('div');
    node.className = `remote-tree-empty ${tone}`.trim();
    node.textContent = message;
    elements.tree.replaceChildren(node);
  };
  const positionFromOffset = (value, offset) => {
    const before = String(value || '').slice(0, Math.max(0, Number(offset) || 0));
    const rows = before.split('\n');
    return {line: rows.length, column: (rows.at(-1)?.length || 0) + 1};
  };
  const updatePosition = (view) => {
    const tab = activeTab();
    if (!tab) {
      elements.position.textContent = 'Ln 1, Col 1';
      return;
    }
    if (tab.previewMode && isMarkdownPath(tab.path)) {
      elements.position.textContent = 'Markdown 预览';
      return;
    }
    const current = view || (editorController.hasModel(tab.id) ? editorController.captureViewState(tab.id) : {}) || {};
    const position = current.line && current.column
      ? current
      : positionFromOffset(tab?.content || '', current.selectionStart ?? tab?.selectionStart ?? 0);
    elements.position.textContent = `Ln ${position.line || 1}, Col ${position.column || 1}`;
  };
  const captureActiveEditorView = () => {
    const tab = activeTab();
    if (!tab || tab.status !== 'ready') return;
    tab.content = editorController.getValue(tab.id);
    const view = editorController.captureViewState(tab.id) || {};
    tab.selectionStart = view.selectionStart || 0;
    tab.selectionEnd = view.selectionEnd ?? tab.selectionStart;
    tab.scrollTop = view.scrollTop || 0;
  };
  const editorController = createRemoteCodeEditor({
    host: elements.editor,
    onChange: ({id, value}) => {
      const tab = tabs.get(id);
      if (!tab || tab.status !== 'ready') return;
      tab.content = value;
      if (tab.id === tabs.activeId) {
        updatePosition();
        updateEditorControls();
      } else renderTabs();
    },
    onCursorChange: ({id, selectionStart = 0, selectionEnd = selectionStart, scrollTop = 0, line, column}) => {
      const tab = tabs.get(id);
      if (!tab) return;
      Object.assign(tab, {selectionStart, selectionEnd, scrollTop});
      if (tab.id === tabs.activeId) updatePosition({line, column, selectionStart});
    },
    onSave: () => { void saveFile(); },
  });
  const markdownPreview = createRemoteMarkdownPreview(elements.preview);
  const extensionLabel = (tab) => tab?.name?.includes('.') ? tab.name.split('.').at(-1).slice(0, 3).toUpperCase() : 'TXT';
  const renderTabs = () => {
    if (!tabs.all.length) {
      const placeholder = document.createElement('div');
      placeholder.className = 'remote-editor-tab-placeholder';
      const kind = document.createElement('span');
      kind.className = 'remote-editor-kind'; kind.textContent = '◇'; kind.setAttribute('aria-hidden', 'true');
      const label = document.createElement('span'); label.textContent = '未打开文件';
      placeholder.append(kind, label);
      elements.tabs.replaceChildren(placeholder);
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const tab of tabs.all) {
      const shell = document.createElement('div');
      shell.className = `remote-editor-tab-shell${tab.id === tabs.activeId ? ' active' : ''}${tab.detached ? ' detached' : ''}`;
      shell.dataset.tabId = tab.id;
      const select = document.createElement('button');
      select.type = 'button';
      select.className = 'remote-editor-tab';
      select.setAttribute('role', 'tab');
      select.setAttribute('aria-selected', String(tab.id === tabs.activeId));
      select.title = `${tab.rootPath || '~'}/${tab.path}${tab.detached ? ' · 上一 SSH 目标' : ''}`;
      const kind = document.createElement('span');
      kind.className = 'remote-editor-kind'; kind.textContent = tab.status === 'error' ? '!' : extensionLabel(tab);
      kind.setAttribute('aria-hidden', 'true');
      const name = document.createElement('span');
      name.className = 'remote-editor-tab-name'; name.textContent = tab.name;
      const dirty = document.createElement('i');
      dirty.className = 'remote-editor-tab-dirty'; dirty.hidden = !tabIsDirty(tab);
      dirty.title = '有未保存的修改';
      select.append(kind, name, dirty);
      select.addEventListener('click', () => activateTab(tab.id));
      const close = document.createElement('button');
      close.type = 'button'; close.className = 'remote-editor-tab-close'; close.textContent = '×';
      close.setAttribute('aria-label', `关闭 ${tab.name}`); close.title = `关闭 ${tab.name}`;
      close.addEventListener('click', () => closeTab(tab.id));
      shell.append(select, close); fragment.append(shell);
    }
    elements.tabs.replaceChildren(fragment);
    elements.tabs.querySelector('.remote-editor-tab-shell.active')?.scrollIntoView({block: 'nearest', inline: 'nearest'});
  };
  const renderActiveEditor = ({focus = false} = {}) => {
    const tab = activeTab();
    renderTabs();
    if (!tab) {
      elements.editor.hidden = true;
      elements.preview.hidden = true;
      elements.previewToggle.hidden = true;
      markdownPreview.clear();
      elements.empty.hidden = false;
      elements.empty.querySelector('strong').textContent = '打开文件开始编辑';
      elements.empty.querySelector('p').textContent = sourceMode === 'local'
        ? '文件直接从已授权的本地目录读取和保存。'
        : '文件通过当前 SSH 会话读取和保存。';
      elements.path.textContent = `${sourceMode === 'local' ? '本地' : '远程'}工作目录`;
      elements.meta.textContent = `UTF-8 · ${sourceLabel()}`;
      setEditorMessage('请选择左侧文件');
      updatePosition();
      updateEditorControls();
      renderTree();
      return;
    }
    elements.path.textContent = `${tab.rootPath || '~'}${tab.path ? `/${tab.path}` : ''}`;
    const markdown = isMarkdownPath(tab.path);
    elements.previewToggle.hidden = !markdown || tab.status !== 'ready';
    elements.previewToggle.textContent = tab.previewMode ? '编辑' : tab.previewRequiresExplicit ? '打开预览' : '预览';
    elements.previewToggle.title = tab.previewMode ? '切换到 Markdown 源码编辑'
      : tab.previewRequiresExplicit
        ? '文件超过自动预览预算；点击后按安全预算渲染，复杂 Mermaid 将显示源码'
        : '安全渲染 Markdown 预览';
    if (tab.status === 'loading' || tab.status === 'error') {
      elements.editor.hidden = true;
      elements.preview.hidden = true;
      elements.empty.hidden = false;
      elements.empty.querySelector('strong').textContent = tab.status === 'loading' ? '正在读取远程文件…' : '无法打开这个文件';
      elements.empty.querySelector('p').textContent = tab.status === 'loading' ? tab.path : tab.error;
    } else {
      elements.empty.hidden = true;
      const existingModel = editorController.hasModel(tab.id);
      const modelOptions = {
        id: tab.id,
        identity: tab.identity,
        path: tab.path,
        value: tab.content,
        forceValue: true,
        readOnly: tab.detached || tab.identity !== identity || !connected || Boolean(tab.readOnly),
      };
      if (!existingModel) Object.assign(modelOptions, {
        selectionStart: tab.selectionStart || 0,
        selectionEnd: tab.selectionEnd ?? tab.selectionStart ?? 0,
        scrollTop: tab.scrollTop || 0,
      });
      editorController.openModel(modelOptions);
      const showPreview = markdown && Boolean(tab.previewMode);
      elements.editor.hidden = showPreview;
      elements.preview.hidden = !showPreview;
      if (showPreview) markdownPreview.render(tab.content);
      else markdownPreview.clear();
      elements.meta.textContent = `${textFormatLabel(tab)} · ${formatSize(tab.size ?? new TextEncoder().encode(tab.content).length) || '0 B'} · ${sourceLabel(tab.source)}`;
    }
    updatePosition();
    updateEditorControls();
    if (focus && !elements.editor.hidden) editorController.focus();
    renderTree();
  };
  function activateTab(id) {
    const target = tabs.get(id);
    if (!target) return;
    if (target.source && target.source !== sourceMode && !target.detached) {
      void switchSource(target.source).then(() => {
        if (!tabs.get(id)) return;
        tabs.activate(id);
        renderActiveEditor({focus: true});
      });
      return;
    }
    const wasActive = id === tabs.activeId;
    if (!wasActive) {
      captureActiveEditorView();
      tabs.activate(id);
    }
    if (target.status === 'error' && !target.busy && !target.detached
        && target.identity === identity && connected) {
      void openFile(target.path, target.entry);
      return;
    }
    if (wasActive && !elements.editor.hidden) return;
    renderActiveEditor({focus: true});
  }
  function closeTab(id) {
    const tab = tabs.get(id);
    if (!tab) return;
    if (tabIsDirty(tab) && !window.confirm(`“${tab.name}”还有未保存的修改，仍要关闭吗？`)) return;
    captureActiveEditorView();
    tabs.close(id);
    editorController.closeModel(id);
    renderActiveEditor({focus: true});
  }
  const updateEditorControls = () => {
    const tab = activeTab();
    const dirty = isDirty(tab);
    const viewable = Boolean(tab) && tab.status === 'ready';
    const attached = Boolean(tab) && !tab.detached && tab.identity === identity;
    // Keep disconnected, detached and format-protected content selectable so
    // the user can always copy work out of the editor. Only an empty editor or
    // an in-flight read is disabled; every other non-editable state is readonly.
    if (tab && editorController.hasModel(tab.id)) {
      editorController.setReadOnly(!viewable || !attached || !connected || Boolean(tab.readOnly), tab.id);
    }
    elements.save.disabled = !tab || !attached || !connected || !dirty || Boolean(tab.busy) || tab.readOnly;
    elements.save.textContent = tab?.busy === 'save' ? '保存中…' : '保存';
    if (tab && !tab.busy) {
      const state = tab.status === 'error' ? '读取失败'
        : tab.detached ? '上一工作区目标 · 未保存内容仅供复制'
        : !attached ? '其他工作区 · 只读'
        : !connected ? (sourceMode === 'local' ? '本地工作区未授权' : 'SSH 已断开')
        : tab.readOnly ? tab.readOnlyReason
          : tab.revisionNeedsVerification ? (dirty ? '待重新校验 · 尚未保存' : '待重新校验')
            : dirty ? '尚未保存' : '已同步';
      setEditorMessage(state);
    } else if (tab?.busy === 'read') setEditorMessage('读取中…');
    renderTabs();
  };
  const resetEditorTabsForTarget = (nextIdentity, source = sourceMode) => {
    captureActiveEditorView();
    const transitioned = tabs.transitionSourceIdentity(nextIdentity, source);
    for (const tab of transitioned.removed) editorController.closeModel(tab.id);
    renderActiveEditor();
    return transitioned;
  };
  const clearEditor = () => {
    for (const tab of [...tabs.all]) {
      tabs.close(tab.id);
      editorController.closeModel(tab.id);
    }
    renderActiveEditor();
  };
  const removeCachedSubtree = (root) => {
    const knownPaths = new Set([
      ...cache.keys(),
      ...cacheMetadata.versions.keys(),
      ...cacheMetadata.refreshedAt.keys(),
      ...expanded,
      ...directoryLoadTokens.keys(),
    ]);
    cacheMetadata.markSubtreeRemoved(root, knownPaths);
    for (const key of knownPaths) {
      if (!isPathWithin(key, root)) continue;
      cache.delete(key);
      expanded.delete(key);
      directoryLoadTokens.delete(key);
    }
  };
  const replaceCachedDirectory = (path, entries, { versioned = false, refreshedAt } = {}) => {
    const previous = cache.get(path) || [];
    const next = Array.isArray(entries) ? entries : [];
    const nextDirectories = new Set(next
      .filter(entry => (entry.type === 'directory' || entry.type === 'dir') && entry.accessible !== false)
      .map(entry => entry.path || joinPath(path, entry.name)));
    for (const entry of previous) {
      if ((entry.type !== 'directory' && entry.type !== 'dir') || entry.accessible === false) continue;
      const child = entry.path || joinPath(path, entry.name);
      if (!nextDirectories.has(child)) removeCachedSubtree(child);
    }
    cache.set(path, next);
    cacheMetadata.markRefreshed(path, {versioned, refreshedAt});
  };
  const resetTreeCache = () => {
    cache.clear();
    cacheMetadata.reset();
    directoryLoadTokens.clear();
    expanded.clear();
    expanded.add('');
  };

  const treeRows = () => [...elements.tree.querySelectorAll('.remote-tree-row:not(:disabled)')];
  const focusTreePath = (path) => {
    const rows = treeRows();
    const target = rows.find(row => row.dataset.path === path) || rows[0];
    if (!target) return;
    focusedTreePath = target.dataset.path;
    for (const row of rows) row.tabIndex = row === target ? 0 : -1;
    target.focus();
  };
  const syncTreeTabStops = () => {
    const rows = treeRows();
    if (!rows.length) return;
    let target = rows.find(row => row.dataset.path === focusedTreePath);
    const active = activeTab();
    if (!target && active && !active.detached && active.identity === identity) target = rows.find(row => row.dataset.path === active.path);
    target ||= rows[0];
    focusedTreePath = target.dataset.path;
    for (const row of rows) row.tabIndex = row === target ? 0 : -1;
  };

  function appendEntries(container, parent = '', depth = 0) {
    const entries = cache.get(parent) || [];
    for (const [position, entry] of entries.entries()) {
      const path = entry.path || joinPath(parent, entry.name);
      const directory = entry.type === 'directory' || entry.type === 'dir';
      const active = activeTab();
      const rowActive = active?.path === path && !active.detached && active.identity === identity;
      const row = document.createElement('button');
      row.type = 'button';
      row.className = `remote-tree-row${rowActive ? ' active' : ''}`;
      row.style.setProperty('--tree-depth', String(depth));
      row.dataset.path = path;
      row.dataset.parentPath = parent;
      row.dataset.directory = String(directory);
      row.dataset.dropEnabled = String(directory && !entry.symlink && entry.accessible !== false);
      row.tabIndex = -1;
      row.setAttribute('role', 'treeitem');
      row.setAttribute('aria-label', `${directory ? '文件夹' : '文件'} ${entry.name}`);
      row.setAttribute('aria-level', String(depth + 1));
      row.setAttribute('aria-posinset', String(position + 1));
      row.setAttribute('aria-setsize', String(entries.length));
      row.setAttribute('aria-selected', String(rowActive));
      if (entry.accessible === false) {
        row.disabled = true;
        row.setAttribute('aria-disabled', 'true');
        row.classList.add('inaccessible');
        row.title = '符号链接位于当前 SSH 工作目录之外';
      } else if (entry.symlink) row.title = '符号链接 · 可查看，不直接覆盖链接目标';
      if (directory) row.setAttribute('aria-expanded', String(expanded.has(path)));
      const chevron = document.createElement('span');
      chevron.className = `remote-tree-chevron${directory ? (expanded.has(path) ? ' expanded' : '') : ' placeholder'}`;
      chevron.setAttribute('aria-hidden', 'true');
      const icon = document.createElement('span');
      icon.className = `remote-tree-icon ${directory ? 'folder' : 'file'}`;
      icon.setAttribute('aria-hidden', 'true');
      const label = document.createElement('span');
      label.className = 'remote-tree-name'; label.textContent = entry.name;
      const meta = document.createElement('span');
      meta.className = 'remote-tree-meta'; meta.textContent = directory ? '' : formatSize(entry.size);
      row.append(chevron, icon, label, meta);
      row.addEventListener('focus', () => {
        focusedTreePath = path;
        for (const candidate of treeRows()) candidate.tabIndex = candidate === row ? 0 : -1;
      });
      row.addEventListener('click', () => {
        focusedTreePath = path;
        void (directory ? toggleDirectory(path) : openFile(path, entry));
      });
      row.addEventListener('contextmenu', (event) => showContextMenu(event, path, entry, directory));
      container.append(row);
      if (directory && expanded.has(path)) {
        if (cache.has(path)) appendEntries(container, path, depth + 1);
        else {
          const loading = document.createElement('div');
          loading.className = 'remote-tree-loading'; loading.style.setProperty('--tree-depth', String(depth + 1)); loading.textContent = '读取中…';
          container.append(loading);
        }
      }
    }
  }
  const renderTree = () => {
    const restoreKeyboardFocus = elements.tree.contains(document.activeElement);
    if (!connected) {
      setTreeEmpty(sourceMode === 'local' ? '选择本地工作区后显示文件。' : '连接 SSH 后显示工作目录中的文件。');
      return;
    }
    if (!cache.has('')) { setTreeEmpty(sourceMode === 'local' ? '正在读取本地工作目录…' : '正在读取远程工作目录…'); return; }
    const root = cache.get('');
    if (!root.length) { setTreeEmpty(sourceMode === 'local' ? '本地工作目录为空。' : '远程工作目录为空。'); return; }
    const fragment = document.createDocumentFragment();
    appendEntries(fragment);
    elements.tree.replaceChildren(fragment);
    syncTreeTabStops();
    if (restoreKeyboardFocus) focusTreePath(focusedTreePath);
  };

  async function loadDirectory(path = '', { force = false, reportError = true, showBusy = true } = {}) {
    if (!connected || disposed) return;
    if (!force && cache.has(path)) return true;
    const generation = requestGeneration;
    const token = Symbol(path || 'root');
    directoryLoadTokens.set(path, token);
    if (showBusy) {
      treeLoadCount += 1;
      elements.tree.setAttribute('aria-busy', 'true');
    }
    if (path === '') renderTree();
    try {
      const result = await sourceInvoke('listRemoteFiles', 'listLocalFiles', {path});
      if (disposed || !connected || generation !== requestGeneration || directoryLoadTokens.get(path) !== token) return false;
      const entries = Array.isArray(result) ? result : result?.entries;
      replaceCachedDirectory(path, entries, {versioned: true});
      renderTree();
      return true;
    } catch (error) {
      if (generation !== requestGeneration || disposed || directoryLoadTokens.get(path) !== token) return false;
      // Keep the last-known-good tree during transient SSH/SFTP failures.
      if (path === '' && !cache.has('')) setTreeEmpty(sourceMode === 'local' ? '无法读取本地工作目录。' : '无法读取远程工作目录。', 'error');
      else if (path !== '' && !cache.has(path)) { expanded.delete(path); renderTree(); }
      if (reportError) onError(error);
      return false;
    } finally {
      if (directoryLoadTokens.get(path) === token) directoryLoadTokens.delete(path);
      if (showBusy) {
        treeLoadCount = Math.max(0, treeLoadCount - 1);
        if (!treeLoadCount) elements.tree.removeAttribute('aria-busy');
      }
    }
  }

  async function toggleDirectory(path) {
    focusedTreePath = path;
    if (expanded.has(path)) {
      expanded.delete(path);
      renderTree();
      return;
    }
    expanded.add(path);
    renderTree();
    const cached = cache.has(path);
    if (!cached) await loadDirectory(path);
    else if (cacheMetadata.isStale(path, REMOTE_TREE_REFRESH_INTERVAL_MS)) {
      // Stale-while-revalidate: keep the cached children visible and refresh
      // this directory on its own SFTP operation without blocking navigation.
      await loadDirectory(path, {force: true, reportError: false, showBusy: false});
    }
  }

  async function finishDroppedUpload(uploadTask, {directory, fileCount, generation}) {
    try {
      const result = await uploadTask;
      if (disposed) return;
      if (generation !== requestGeneration) {
        showUploadStatus('工作区已切换，已停止显示原上传结果', {tone: 'error', hideAfterMs: 4000});
        return;
      }
      const uploaded = Array.isArray(result?.uploaded) ? result.uploaded : [];
      const failures = Array.isArray(result?.failures) ? result.failures : [];
      if (uploaded.length + failures.length !== fileCount)
        throw new Error('文件上传结果不完整，请刷新目标目录核对。');
      if (uploaded.length) {
        if (directory) expanded.add(directory);
        await loadDirectory(directory, {force: true, reportError: false});
        if (disposed) return;
        if (generation !== requestGeneration) {
          showUploadStatus('工作区已切换，已停止显示原上传结果', {tone: 'error', hideAfterMs: 4000});
          return;
        }
      }
      const target = directory || '远程工作目录';
      if (!failures.length) {
        const message = `已上传 ${uploaded.length} 个文件到 ${target}`;
        setEditorMessage(message);
        showUploadStatus(message, {hideAfterMs: 2800});
      } else {
        const message = `已上传 ${uploaded.length} 个文件，${failures.length} 个失败`;
        setEditorMessage(message);
        showUploadStatus(message, {tone: 'error', hideAfterMs: 6000});
        const details = failures.slice(0, 3)
          .map(item => `${item.name || '文件'}：${item.message || item.code || '上传失败'}`).join('；');
        onError(new Error(`${message}${details ? `。${details}` : ''}`));
      }
    } catch (error) {
      if (!disposed && generation === requestGeneration) {
        setEditorMessage('上传失败');
        showUploadStatus('文件上传失败', {tone: 'error', hideAfterMs: 6000});
        onError(error);
      }
    } finally {
      uploadInProgress = false;
      clearDropTarget({keepStatus: true});
    }
  }

  function applyFileResult(tab, result, loadedBinding = currentReadBinding()) {
    const entry = tab.entry || {};
    const content = String(result?.content ?? '').replace(/\r\n?/g, '\n');
    const previewPolicy = isMarkdownPath(tab.path) ? markdownPreview.policy(content) : null;
    Object.assign(tab, {
      bom: Boolean(result?.bom),
      lineEnding: result?.lineEnding || 'lf',
      readOnly: result?.lineEnding === 'mixed' || Boolean(entry.symlink),
      readOnlyReason: result?.lineEnding === 'mixed' ? '混合换行 · 仅查看' : entry.symlink ? '符号链接 · 仅查看' : '',
      content,
      pristine: content,
      revision: String(result?.revision || ''),
      loadedBinding,
      revisionNeedsVerification: false,
      ...(tab.previewMode === undefined ? {previewMode: Boolean(previewPolicy?.autoPreview)} : {}),
      previewRequiresExplicit: Boolean(previewPolicy && !previewPolicy.autoPreview),
      previewTruncated: Boolean(previewPolicy?.truncated),
      degradedMermaidDiagrams: Number(previewPolicy?.degradedDiagrams || 0),
      size: Number(result?.size ?? new TextEncoder().encode(content).length),
      mtimeMs: Number.isInteger(result?.mtimeMs) ? result.mtimeMs : entry.mtimeMs,
      busy: '',
      status: 'ready',
      error: '',
    });
    if (tab.id === tabs.activeId) renderActiveEditor();
    else renderTabs();
  }

  async function reloadTabAfterReconnect(tab, generation, reconnectIdentity) {
    if (!tab || tab.detached || tab.identity !== reconnectIdentity) return;
    if (tabIsDirty(tab)) {
      tab.revisionNeedsVerification = true;
      if (tab.id === tabs.activeId) updateEditorControls(); else renderTabs();
      return;
    }
    const binding = currentReadBinding();
    const readToken = readTokens.begin(binding, tab.id);
    tab.busy = 'read';
    tab.status = 'loading';
    if (tab.id === tabs.activeId) updateEditorControls(); else renderTabs();
    try {
      const result = await invokeForBinding(binding, 'readRemoteFile', 'readLocalFile', {path: tab.path});
      if (disposed || !connected || identity !== reconnectIdentity || generation !== requestGeneration
          || !tabs.get(tab.id) || !readTokens.accept(readToken, result, currentReadBinding())) return;
      applyFileResult(tab, result, binding);
    } catch (error) {
      if (disposed || identity !== reconnectIdentity || generation !== requestGeneration || !tabs.get(tab.id)
          || readTokens.pendingFor(tab.id) !== readToken) return;
      // Keep the known buffer and revision. Any later save still carries the
      // previous expectedRevision, so a server-side change cannot be lost.
      tab.revisionNeedsVerification = true;
      tab.status = tab.content || tab.pristine ? 'ready' : 'error';
      tab.error = String(error?.message || error || '读取失败');
      onError(new Error(`SSH 已重连，但“${tab.name}”重新读取失败；缓冲区已保留并等待 revision 校验。${error?.message ? ` ${error.message}` : ''}`));
    } finally {
      if (readTokens.finish(readToken) && tabs.get(tab.id)) {
        tab.busy = '';
        if (tab.status === 'loading') {
          tab.status = 'error';
          tab.error = '工作区在读取期间已变化，请重试。';
        }
        if (tab.id === tabs.activeId) renderActiveEditor(); else renderTabs();
      }
    }
  }

  function reloadTabsAfterReconnect() {
    const generation = requestGeneration;
    const reconnectIdentity = identity;
    for (const tab of tabs.all.filter(candidate => !candidate.detached && candidate.identity === reconnectIdentity)) {
      void reloadTabAfterReconnect(tab, generation, reconnectIdentity);
    }
  }

  async function openFile(path, entry = {}, {preview = false} = {}) {
    if (!connected) return;
    captureActiveEditorView();
    const opened = tabs.open({identity, path, entry, rootPath: cwd || '~'});
    opened.tab.source = sourceMode;
    if (preview && isMarkdownPath(path)) {
      opened.tab.previewMode = true;
      opened.tab.previewExplicit = true;
    }
    if (!tabNeedsRead(opened)) {
      renderActiveEditor({focus: true});
      return;
    }
    const tab = opened.tab;
    if (!opened.created) Object.assign(tab, {busy: 'read', status: 'loading', error: ''});
    const generation = requestGeneration;
    const binding = currentReadBinding();
    const readToken = readTokens.begin(binding, tab.id);
    renderActiveEditor();
    try {
      const result = await invokeForBinding(binding, 'readRemoteFile', 'readLocalFile', {path});
      if (disposed || !connected || identity !== tab.identity || generation !== requestGeneration || !tabs.get(tab.id)
          || !readTokens.accept(readToken, result, currentReadBinding())) return;
      applyFileResult(tab, result, binding);
      if (preview && isMarkdownPath(path)) {
        tab.previewMode = true;
        tab.previewExplicit = true;
        if (tab.id === tabs.activeId) renderActiveEditor();
      }
      if (tab.id === tabs.activeId && !tab.readOnly) editorController.focus();
    } catch (error) {
      if (!tabs.get(tab.id) || generation !== requestGeneration || readTokens.pendingFor(tab.id) !== readToken) return;
      tab.busy = '';
      tab.status = 'error';
      tab.error = String(error?.message || error || '读取失败');
      if (tab.id === tabs.activeId) renderActiveEditor(); else renderTabs();
      onError(error);
    } finally {
      if (readTokens.finish(readToken) && tabs.get(tab.id)) {
        tab.busy = '';
        if (tab.status === 'loading') {
          tab.status = 'error';
          tab.error = '工作区在读取期间已变化，请重试。';
        }
        if (tab.id === tabs.activeId) renderActiveEditor(); else renderTabs();
      }
    }
  }

  async function saveFile() {
    captureActiveEditorView();
    const tab = activeTab();
    if (!tab || tab.detached || tab.identity !== identity || !connected || !tabIsDirty(tab) || tab.busy || tab.readOnly) return;
    tab.busy = 'save'; updateEditorControls();
    const generation = requestGeneration;
    try {
      // Keep a snapshot of exactly what is sent. The editor intentionally
      // stays editable while SFTP is saving; keystrokes entered during the
      // request must remain dirty instead of being silently marked saved.
      const submittedContent = tab.content;
      const result = await sourceInvoke('saveRemoteFile', 'saveLocalFile', {
        path: tab.path,
        content: submittedContent,
        expectedRevision: tab.revision || undefined,
        bom: tab.bom,
        lineEnding: tab.lineEnding,
      });
      if (!tabs.get(tab.id)) return;
      if (generation !== requestGeneration || tab.detached || tab.identity !== identity) {
        tab.revisionNeedsVerification = true;
        return;
      }
      tab.pristine = submittedContent;
      tab.revision = String(result?.revision || tab.revision);
      tab.revisionNeedsVerification = false;
      tab.bom = Boolean(result?.bom ?? tab.bom);
      tab.lineEnding = result?.lineEnding || tab.lineEnding;
      tab.size = Number(result?.size ?? new TextEncoder().encode(submittedContent).length);
      if (Number.isInteger(result?.mtimeMs)) tab.mtimeMs = result.mtimeMs;
      if (tab.id === tabs.activeId) setEditorMessage('已保存');
      const parent = tab.path.includes('/') ? tab.path.slice(0, tab.path.lastIndexOf('/')) : '';
      await loadDirectory(parent, { force: true });
    } catch (error) {
      if (tab.id === tabs.activeId) setEditorMessage('保存失败');
      onError(error);
    } finally {
      if (tabs.get(tab.id)) tab.busy = '';
      if (tab.id === tabs.activeId) updateEditorControls(); else renderTabs();
    }
  }

  function mutationSnapshot(path, entry = {}) {
    const tab = tabs.find(identity, path);
    if (tab?.revision) return {expectedRevision: tab.revision};
    const snapshot = {};
    if (Number.isInteger(entry.size)) snapshot.expectedSize = entry.size;
    if (Number.isInteger(entry.mtimeMs)) snapshot.expectedMtimeMs = entry.mtimeMs;
    return snapshot;
  }

  function downloadSnapshot(path, entry = {}) {
    const tab = tabs.find(identity, path);
    const snapshot = {};
    const size = Number.isInteger(tab?.size) ? tab.size : entry.size;
    const mtimeMs = Number.isInteger(tab?.mtimeMs) ? tab.mtimeMs : entry.mtimeMs;
    if (Number.isInteger(size)) snapshot.expectedSize = size;
    if (Number.isInteger(mtimeMs)) snapshot.expectedMtimeMs = mtimeMs;
    if (typeof tab?.revision === 'string' && /^sha256:[0-9a-f]{64}$/.test(tab.revision))
      snapshot.expectedRevision = tab.revision;
    return snapshot;
  }

  function rewriteOpenTabPaths(previousPath, nextPath) {
    for (const tab of tabs.all) {
      if (tab.identity !== identity || tab.detached) continue;
      if (tab.path !== previousPath && !tab.path.startsWith(`${previousPath}/`)) continue;
      tab.path = `${nextPath}${tab.path.slice(previousPath.length)}`;
      tab.name = basename(tab.path);
      tab.entry = {...tab.entry, path: tab.path, name: tab.name};
    }
  }

  async function refreshAfterMutation() {
    resetTreeCache();
    focusedTreePath = '';
    renderTree();
    await queueTreeRefresh({reportError: true, required: true});
    renderActiveEditor();
  }

  async function renameEntry(path, entry, directory) {
    const fence = connectionFence();
    const currentName = basename(path);
    const nextName = window.prompt(`重命名“${currentName}”`, currentName)?.trim();
    if (!nextName || nextName === currentName) return;
    const result = await invoke('renameRemoteFile', {
      path, newName: nextName, ...mutationSnapshot(path, entry), ...fence,
    });
    const nextPath = String(result?.path || joinPath(parentPath(path), nextName));
    rewriteOpenTabPaths(path, nextPath);
    if (copiedFile?.identity === identity
        && (copiedFile.path === path || copiedFile.path.startsWith(`${path}/`))) copiedFile = null;
    await refreshAfterMutation();
    setEditorMessage(`${directory ? '目录' : '文件'}已重命名`);
  }

  function copiedDestination(folder, sourceName) {
    const entries = cache.get(folder) || [];
    const names = new Set(entries.map(entry => entry.name));
    if (!names.has(sourceName) && joinPath(folder, sourceName) !== copiedFile?.path) return joinPath(folder, sourceName);
    const dot = sourceName.lastIndexOf('.');
    const stem = dot > 0 ? sourceName.slice(0, dot) : sourceName;
    const extension = dot > 0 ? sourceName.slice(dot) : '';
    for (let index = 1; index < 1000; index += 1) {
      const suffix = index === 1 ? ' copy' : ` copy ${index}`;
      const candidate = `${stem}${suffix}${extension}`;
      if (!names.has(candidate)) return joinPath(folder, candidate);
    }
    throw new Error('当前目录中同名副本过多，请先重命名。');
  }

  async function pasteCopiedFile(folder) {
    if (!copiedFile || copiedFile.identity !== identity || !connected) return;
    const destinationPath = copiedDestination(folder, basename(copiedFile.path));
    await invoke('copyRemoteFile', {
      path: copiedFile.path,
      destinationPath,
      ...mutationSnapshot(copiedFile.path, copiedFile.entry),
      ...connectionFence(),
    });
    await refreshAfterMutation();
    setEditorMessage(`已复制到 ${destinationPath}`);
  }

  function absoluteRemotePath(relative) {
    const root = String(cwd || '~').replace(/[\\/]$/, '');
    if (!relative) return root;
    return sourceMode === 'local' && root.includes('\\')
      ? `${root}\\${relative.replaceAll('/', '\\')}`
      : `${root}/${relative}`;
  }

  function dismissContextMenu() {
    contextMenu?.remove();
    contextMenu = null;
  }

  function contextMenuItem(label, action, {disabled = false} = {}) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'remote-context-menu-item';
    button.setAttribute('role', 'menuitem');
    button.textContent = label;
    button.disabled = disabled;
    button.addEventListener('click', () => {
      dismissContextMenu();
      Promise.resolve().then(action).catch(onError);
    });
    return button;
  }

  function showContextMenu(event, path, entry, directory) {
    event.preventDefault();
    event.stopPropagation();
    dismissContextMenu();
    focusedTreePath = path;
    const menu = document.createElement('div');
    menu.className = 'remote-context-menu';
    menu.setAttribute('role', 'menu');
    if (sourceMode === 'remote') menu.append(contextMenuItem('重命名', () => renameEntry(path, entry, directory)));
    menu.append(contextMenuItem('复制文件路径', () => invoke('writeClipboard', {text: absoluteRemotePath(path)})));
    if (!directory) {
      if (sourceMode === 'remote') menu.append(
        contextMenuItem('复制', () => {
          copiedFile = {identity, path, entry: {...entry}};
          setEditorMessage(`已复制 ${entry.name || basename(path)}，可在目录上右键粘贴`);
        }),
        contextMenuItem('下载', () => invoke('downloadRemoteFile', {
          path,
          ...downloadSnapshot(path, entry),
          ...connectionFence(),
        })),
      );
      menu.append(contextMenuItem('预览', () => openFile(path, entry, {preview: true})));
    } else if (sourceMode === 'remote' && copiedFile?.identity === identity) {
      menu.append(contextMenuItem('粘贴', () => pasteCopiedFile(path)));
    }
    document.body.append(menu);
    const width = menu.offsetWidth;
    const height = menu.offsetHeight;
    menu.style.left = `${Math.max(4, Math.min(event.clientX, window.innerWidth - width - 4))}px`;
    menu.style.top = `${Math.max(4, Math.min(event.clientY, window.innerHeight - height - 4))}px`;
    contextMenu = menu;
    menu.querySelector('button:not(:disabled)')?.focus();
  }

  async function prefetchDirectoryTree(generation) {
    if (sourceMode !== 'remote' || !connected || disposed || generation !== requestGeneration) return;
    const roots = (cache.get('') || [])
      .filter(entry => (entry.type === 'directory' || entry.type === 'dir')
        && entry.accessible !== false && !entry.symlink)
      .map(entry => entry.path || entry.name);
    if (!roots.length) return;
    const cacheSnapshot = cacheMetadata.snapshot();
    let result;
    try {
      result = await invoke('prefetchRemoteFiles', {
        paths: roots, ...REMOTE_TREE_PREFETCH_OPTIONS, ...connectionFence(),
      });
    } catch {
      // This is a best-effort background optimization. Direct folder access
      // still reports its own actionable error and keeps the last good tree.
      return;
    }
    if (!connected || disposed || generation !== requestGeneration || result?.cancelled) return;
    const refreshedAt = Date.now();
    for (const directory of Array.isArray(result?.directories) ? result.directories : []) {
      const path = String(directory?.path || '');
      if (!path || !cacheMetadata.canApplyPrefetch(path, cacheSnapshot)) continue;
      replaceCachedDirectory(path, directory.entries, {versioned: false, refreshedAt});
    }
    renderTree();
  }

  async function refreshExpandedDirectories(generation) {
    // Root is refreshed separately. A small sequential loop avoids a burst of
    // SFTP channels when a user has many folders open, while still forcing all
    // visible branches beyond the bounded prefetch frontier to converge.
    const paths = [...expanded].filter(path => path);
    for (const path of paths) {
      if (!connected || disposed || generation !== requestGeneration) return;
      if (!expanded.has(path)) continue;
      await loadDirectory(path, {force: true, reportError: false, showBusy: false});
    }
  }

  async function performTreeRefresh({reportError = false} = {}) {
    if (!connected || disposed) return;
    const generation = requestGeneration;
    const loaded = await loadDirectory('', {
      force: true,
      reportError,
      showBusy: reportError || !cache.has(''),
    });
    if (!loaded || !connected || disposed || generation !== requestGeneration) return;
    // Prefetch and explicit refreshes use independent SFTP operations. Direct
    // expanded-path results advance their versions, so a slower prefetch result
    // cannot overwrite them.
    await Promise.all([
      prefetchDirectoryTree(generation),
      refreshExpandedDirectories(generation),
    ]);
  }

  const resolveQueuedTreeRefresh = () => {
    const queued = queuedTreeRefresh;
    queuedTreeRefresh = null;
    queued?.resolve();
  };

  function queueTreeRefresh({reportError = false, required = false} = {}) {
    if (!connected || disposed) return Promise.resolve();
    const generation = requestGeneration;
    if (treeRefreshTask?.generation === generation) {
      if (!required) return treeRefreshTask.promise;
      if (!queuedTreeRefresh || queuedTreeRefresh.generation !== generation) {
        let resolve;
        const promise = new Promise(done => { resolve = done; });
        queuedTreeRefresh = {generation, reportError: Boolean(reportError), promise, resolve};
      } else queuedTreeRefresh.reportError ||= Boolean(reportError);
      // A required/manual request waits for the follow-up round, rather than
      // merely waiting for the already-running periodic/background refresh.
      return queuedTreeRefresh.promise;
    }
    const task = {generation, promise: null};
    task.promise = (async () => {
      try { await performTreeRefresh({reportError}); }
      finally {
        if (treeRefreshTask !== task) return;
        treeRefreshTask = null;
        const queued = queuedTreeRefresh;
        queuedTreeRefresh = null;
        if (queued && connected && !disposed && queued.generation === requestGeneration) {
          queueTreeRefresh({reportError: queued.reportError, required: false}).then(queued.resolve, queued.resolve);
        } else queued?.resolve();
      }
    })();
    treeRefreshTask = task;
    return task.promise;
  }

  async function refresh() {
    if (!connected || disposed) return;
    elements.refresh.disabled = true;
    try { await queueTreeRefresh({reportError: true, required: true}); }
    finally { elements.refresh.disabled = !connected; }
  }

  function invalidatePendingReads(message = '工作区已变化，请重试读取。') {
    const invalidated = readTokens.invalidate();
    for (const token of invalidated) {
      const tab = tabs.get(token.key);
      if (!tab) continue;
      tab.busy = '';
      if (tab.status === 'loading') {
        tab.status = 'error';
        tab.error = message;
      }
    }
    if (invalidated.length) renderActiveEditor();
  }

  async function cancelSearchToken(token) {
    if (!token?.requestId) return;
    try {
      await invokeForBinding(token.binding, 'cancelRemoteFileSearch', 'cancelLocalFileSearch', {
        requestId: token.requestId,
      });
    } catch { /* Cancellation is best effort; the session token drops late results. */ }
  }

  function invalidateSearchView({clearQuery = false, status = ''} = {}) {
    const token = searchSession.invalidate();
    if (token) void cancelSearchToken(token);
    if (searchTimer) { clearTimeout(searchTimer); searchTimer = null; }
    searchMatches = [];
    searchTruncated = false;
    if (clearQuery) elements.searchQuery.value = '';
    elements.searchStatus.textContent = status;
    elements.searchCancel.hidden = true;
    renderSearchResults();
  }

  function applySourceState({preserveTabs = false} = {}) {
    const previousIdentity = identity;
    const previousConnected = connected;
    const nextConnected = sourceMode === 'local' ? Boolean(localGrant?.grantId) : remoteSsh.state === 'connected';
    const nextIdentity = sourceMode === 'local'
      ? (localGrant?.grantId ? `local:${localGrant.grantId}` : 'local:unselected')
      : remoteIdentity;
    const nextExpectedConnection = sourceMode === 'remote' && nextConnected ? remoteConnectionExpectation(remoteSsh) : null;
    const targetChanged = Boolean(previousIdentity) && nextIdentity !== previousIdentity;
    const generationChanged = sourceMode === 'remote' && nextConnected && previousConnected
      && previousIdentity === nextIdentity && expectedConnection && nextExpectedConnection
      && expectedConnection.generation !== nextExpectedConnection.generation;
    const becameConnected = nextConnected && (!previousConnected || targetChanged);
    const sameTargetReconnect = becameConnected && Boolean(previousIdentity) && nextIdentity === previousIdentity;
    const sameTargetGenerationChanged = generationChanged && nextIdentity === previousIdentity;
    const workspaceContextChanged = targetChanged || generationChanged || previousConnected !== nextConnected;
    if (workspaceContextChanged) {
      clearDropTarget();
      if (uploadInProgress)
        showUploadStatus('工作区已切换，正在结束原上传…', {tone: 'error'});
      invalidateSearchView({clearQuery: true});
      invalidatePendingReads();
    }
    if (targetChanged || generationChanged) {
      dismissContextMenu();
      periodicTreeRefresh.stop();
      requestGeneration += 1; resetTreeCache();
      resolveQueuedTreeRefresh();
      focusedTreePath = '';
      if (targetChanged && !preserveTabs) {
        const {retained} = resetEditorTabsForTarget(nextIdentity);
        if (retained.length) onError(new Error(`工作区已切换；之前的 ${retained.length} 个未保存文件已保留为只读标签，可复制后再关闭。`));
      }
    }
    connected = nextConnected;
    identity = nextIdentity;
    expectedConnection = nextExpectedConnection;
    elements.sourceTitle.textContent = sourceMode === 'local' ? '本地工作区' : '远程工作区';
    elements.sourceRemote.classList.toggle('active', sourceMode === 'remote');
    elements.sourceRemote.setAttribute('aria-selected', String(sourceMode === 'remote'));
    elements.sourceLocal.classList.toggle('active', sourceMode === 'local');
    elements.sourceLocal.setAttribute('aria-selected', String(sourceMode === 'local'));
    elements.chooseLocal.hidden = sourceMode !== 'local';
    if (nextConnected) {
      cwd = sourceMode === 'local' ? localGrant.root : (remoteSsh.remoteRootPath || remoteSsh.cwd || '~');
      elements.rootLabel.textContent = cwd;
      elements.root.title = cwd;
    } else {
      dismissContextMenu();
      cwd = '';
      elements.rootLabel.textContent = sourceMode === 'local' ? '请选择本地工作区' : '尚未连接 SSH';
      elements.root.title = elements.rootLabel.textContent;
      requestGeneration += 1;
      resetTreeCache();
      resolveQueuedTreeRefresh();
      periodicTreeRefresh.stop();
    }
    elements.refresh.disabled = !connected;
    renderTree(); renderActiveEditor();
    if (becameConnected || targetChanged || generationChanged) {
      periodicTreeRefresh.start();
      void queueTreeRefresh({reportError: true, required: true});
    }
    const readBinding = currentReadBinding();
    const needsRemoteRevalidation = sourceMode === 'remote' && nextConnected
      && tabs.all.some(tab => tab.source === 'remote' && !tab.detached && tab.identity === nextIdentity
        && !readTokens.pendingFor(tab.id)
        && (!tab.loadedBinding || !sameWorkspaceBinding(tab.loadedBinding, readBinding)));
    if (sameTargetReconnect || sameTargetGenerationChanged || needsRemoteRevalidation) reloadTabsAfterReconnect();
  }

  function updateConnection(ssh = {}) {
    const previousRemoteIdentity = remoteIdentity;
    remoteSsh = {...ssh};
    if (ssh.state === 'connected') remoteIdentity = `remote:${normalizeConnectionIdentity(ssh)}`;
    if (sourceMode === 'remote') applySourceState();
    else if (previousRemoteIdentity !== 'remote:disconnected' && remoteIdentity !== previousRemoteIdentity) {
      const transitioned = tabs.transitionSourceIdentity(remoteIdentity, 'remote');
      for (const tab of transitioned.removed) editorController.closeModel(tab.id);
      if (transitioned.retained.length) onError(new Error(`远程 SSH 目标已切换；${transitioned.retained.length} 个未保存文件保留为只读标签。`));
      renderActiveEditor();
    }
  }

  async function initializeLocalWorkspace({choose = false, apply = true} = {}) {
    const grant = await invoke(choose ? 'chooseLocalWorkspace' : 'getDefaultLocalWorkspace', {});
    if (grant?.grantId) localGrant = grant;
    if (apply && sourceMode === 'local') applySourceState();
    return localGrant;
  }

  async function switchSource(nextMode) {
    if (!['remote', 'local'].includes(nextMode) || nextMode === sourceMode) return;
    captureActiveEditorView();
    invalidateSearchView({clearQuery: true});
    invalidatePendingReads();
    sourceMode = nextMode;
    if (sourceMode === 'local' && !localGrant) {
      try { await initializeLocalWorkspace({apply: false}); }
      catch (error) { onError(error); }
    }
    applySourceState({preserveTabs: true});
    const sourceTab = tabs.all.find(tab => !tab.detached && tab.identity === identity);
    if (sourceTab) { tabs.activate(sourceTab.id); renderActiveEditor(); }
  }

  function renderSearchResults() {
    elements.searchResults.replaceChildren();
    if (!searchMatches.length) {
      elements.searchResults.hidden = true;
      elements.replaceAll.disabled = true;
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const match of searchMatches) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'workspace-search-result';
      button.title = `${match.path}:${match.line}:${match.column}`;
      const pathLabel = document.createElement('span');
      pathLabel.className = 'workspace-search-result-path'; pathLabel.textContent = match.path;
      const location = document.createElement('span');
      location.className = 'workspace-search-result-location'; location.textContent = `${match.line}:${match.column}`;
      const preview = document.createElement('span');
      preview.className = 'workspace-search-result-preview'; preview.textContent = match.preview || '';
      button.append(pathLabel, location, preview);
      button.addEventListener('click', async () => {
        try {
          await openFile(match.path, {name: basename(match.path), size: match.size, mtimeMs: match.mtimeMs});
          const tab = tabs.find(identity, match.path);
          if (tab?.status === 'ready') editorController.revealPosition({line: match.line, column: match.column}, tab.id);
        } catch (error) { onError(error); }
      });
      fragment.append(button);
    }
    elements.searchResults.replaceChildren(fragment);
    elements.searchResults.hidden = false;
    elements.replaceAll.disabled = searchTruncated
      || !searchMatches.some(match => /^sha256:[0-9a-f]{64}$/.test(String(match.revision || '')));
  }

  async function runSearch() {
    if (searchTimer) { clearTimeout(searchTimer); searchTimer = null; }
    const previous = searchSession.invalidate();
    if (previous) await cancelSearchToken(previous);
    searchMatches = [];
    searchTruncated = false;
    renderSearchResults();
    const binding = currentSearchBinding();
    const {query, caseSensitive} = binding;
    if (!query) { elements.searchStatus.textContent = ''; return; }
    if (!connected) {
      elements.searchStatus.textContent = sourceMode === 'local' ? '请先选择本地工作区。' : '请先连接 SSH。';
      return;
    }
    // Cancellation itself is asynchronous. Do not launch a request captured
    // before an input/source/connection change that happened while awaiting it.
    if (!sameWorkspaceBinding(binding, currentSearchBinding())) return;
    const requestId = `explorer-search-${window.crypto.randomUUID()}`;
    const token = searchSession.begin(binding, requestId);
    elements.searchCancel.hidden = false;
    elements.searchStatus.textContent = '搜索中…';
    try {
      const result = await invokeForBinding(binding, 'searchRemoteFiles', 'searchLocalFiles', {
        requestId,
        query,
        caseSensitive,
        regex: false,
        maxResults: 300,
        maxFiles: 10000,
        maxFileBytes: 1024 * 1024,
        timeBudgetMs: 10000,
      });
      if (disposed || !searchSession.accept(token, result, currentSearchBinding())) return;
      searchMatches = Array.isArray(result?.matches) ? result.matches : [];
      searchTruncated = Boolean(result?.truncated);
      renderSearchResults();
      const suffix = result?.truncated ? ` · 已在${({result_limit: '结果上限', file_limit: '文件上限', time_budget: '时间预算'})[result.reason] || '安全上限'}停止` : '';
      elements.searchStatus.textContent = `${searchMatches.length} 个结果 · 扫描 ${Number(result?.stats?.scannedFiles || 0)} 个文件${suffix}`;
    } catch (error) {
      if (searchSession.current === token && !/取消/.test(String(error?.message || ''))) {
        elements.searchStatus.textContent = '搜索失败';
        onError(error);
      }
    } finally {
      if (searchSession.finish(token)) elements.searchCancel.hidden = true;
    }
  }

  function scheduleSearch() {
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => { searchTimer = null; void runSearch(); }, 260);
  }

  async function replaceAllSearchMatches() {
    const binding = currentSearchBinding();
    const snapshot = searchSession.snapshotFor(binding);
    if (!snapshot || !connected) {
      onError(new Error('搜索条件或工作区已变化，请重新搜索后再替换。'));
      return;
    }
    const resultSnapshot = snapshot.result || {};
    const matches = Array.isArray(resultSnapshot.matches) ? resultSnapshot.matches : [];
    if (!binding.query || !matches.length) return;
    if (resultSnapshot.truncated) { onError(new Error('搜索结果已达到安全上限，请缩小搜索范围后再全部替换。')); return; }
    const files = new Map();
    for (const match of matches) {
      const matchRevision = String(match.revision || '');
      if (!/^sha256:[0-9a-f]{64}$/.test(matchRevision)) continue;
      const existing = files.get(match.path);
      if (existing && existing.revision !== matchRevision) {
        onError(new Error(`“${match.path}”的搜索快照不一致，请重新搜索。`));
        return;
      }
      files.set(match.path, {path: match.path, revision: matchRevision});
    }
    if (!files.size) { onError(new Error('搜索结果缺少可校验的文件快照，请重新搜索。')); return; }
    const dirty = [...files.keys()].find(path => {
      const tab = tabs.find(identity, path);
      return tab && tabIsDirty(tab);
    });
    if (dirty) { onError(new Error(`“${dirty}”有未保存修改，请先保存或关闭后再批量替换。`)); return; }
    elements.replaceAll.disabled = true;
    elements.searchStatus.textContent = '正在校验快照并替换…';
    try {
      const result = await invokeForBinding(snapshot.binding, 'replaceRemoteFiles', 'replaceLocalFiles', {
        query: snapshot.binding.query,
        replacement: elements.replaceValue.value,
        caseSensitive: snapshot.binding.caseSensitive,
        regex: false,
        files: [...files.values()],
      });
      if (!sameWorkspaceBinding(snapshot.binding, currentSearchBinding())) {
        onError(new Error('替换已在原工作区完成；当前工作区已变化，请返回原目标刷新核对。'));
        return;
      }
      const changed = new Set((result?.saved || []).map(item => item.path));
      for (const tab of tabs.all) {
        if (tab.identity !== identity || !changed.has(tab.path)) continue;
        tab.status = 'error'; tab.error = '文件已由批量替换更新，正在重新读取。';
        void openFile(tab.path, tab.entry);
      }
      resetTreeCache();
      await queueTreeRefresh({reportError: false, required: true});
      elements.searchStatus.textContent = `已替换 ${Number(result?.replacedFiles || 0)} 个文件`;
      await runSearch();
    } catch (error) {
      elements.searchStatus.textContent = '替换未完成';
      onError(error);
    } finally {
      const current = searchSession.snapshotFor(currentSearchBinding());
      elements.replaceAll.disabled = !current || Boolean(current.result?.truncated)
        || !(current.result?.matches || []).length;
    }
  }

  listen(elements.refresh, 'click', () => { void refresh(); });
  listen(elements.sourceRemote, 'click', () => { void switchSource('remote'); });
  listen(elements.sourceLocal, 'click', () => { void switchSource('local'); });
  listen(elements.chooseLocal, 'click', async () => {
    try { await initializeLocalWorkspace({choose: true}); }
    catch (error) { onError(error); }
  });
  listen(elements.searchQuery, 'input', () => {
    invalidateSearchView();
    if (elements.searchQuery.value) scheduleSearch();
  });
  listen(elements.searchQuery, 'keydown', (event) => {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    void runSearch();
  });
  listen(elements.searchCase, 'click', () => {
    const next = elements.searchCase.getAttribute('aria-pressed') !== 'true';
    elements.searchCase.setAttribute('aria-pressed', String(next));
    invalidateSearchView();
    if (elements.searchQuery.value) void runSearch();
  });
  listen(elements.searchReplaceToggle, 'click', () => {
    const expanded = elements.replaceRow.hidden;
    elements.replaceRow.hidden = !expanded;
    elements.searchReplaceToggle.setAttribute('aria-expanded', String(expanded));
    if (expanded) elements.replaceValue.focus();
  });
  listen(elements.searchCancel, 'click', () => {
    invalidateSearchView({status: '搜索已取消'});
  });
  listen(elements.replaceAll, 'click', () => { void replaceAllSearchMatches(); });
  listen(elements.previewToggle, 'click', () => {
    captureActiveEditorView();
    const tab = activeTab();
    if (!tab || !isMarkdownPath(tab.path) || tab.status !== 'ready') return;
    tab.previewMode = !tab.previewMode;
    if (tab.previewMode) tab.previewExplicit = true;
    renderActiveEditor({focus: !tab.previewMode});
  });
  listen(document, 'pointerdown', (event) => {
    if (contextMenu && !contextMenu.contains(event.target)) dismissContextMenu();
  });
  listen(window, 'blur', dismissContextMenu);
  listen(document, 'keydown', (event) => {
    if (event.key === 'Escape' && contextMenu) {
      event.preventDefault();
      dismissContextMenu();
    }
  });
  listen(elements.explorer, 'dragenter', (event) => {
    if (!isFileDrag(event)) return;
    event.preventDefault();
    const target = uploadTargetForEvent(event);
    displayUploadTarget(target);
  });
  listen(elements.explorer, 'dragover', (event) => {
    if (!isFileDrag(event)) return;
    event.preventDefault();
    const target = uploadTargetForEvent(event);
    displayUploadTarget(target);
    if (event.dataTransfer) event.dataTransfer.dropEffect = target ? 'copy' : 'none';
  });
  listen(elements.explorer, 'dragleave', (event) => {
    if (event.relatedTarget && elements.explorer.contains(event.relatedTarget)) return;
    clearDropTarget();
  });
  listen(elements.explorer, 'drop', (event) => {
    if (!isFileDrag(event)) return;
    event.preventDefault();
    event.stopPropagation();
    const target = uploadTargetForEvent(event);
    if (!target) { clearDropTarget(); return; }
    // FileList itself does not retain its entries across Electron's isolated
    // context bridge. Snapshot native File objects into a real array while the
    // drop event still owns the DataTransfer.
    const files = Array.from(event.dataTransfer?.files || []);
    const fileCount = files.length;
    if (!fileCount) { clearDropTarget(); return; }
    if (typeof uploadDroppedFiles !== 'function') {
      clearDropTarget();
      onError(new Error('当前桌面版本不支持拖拽上传，请更新后重试。'));
      return;
    }
    let uploadTask;
    const generation = requestGeneration;
    try {
      // The preload resolves native File objects synchronously with
      // webUtils.getPathForFile. Start it inside the drop callback because the
      // browser may invalidate DataTransfer after this event returns.
      uploadTask = uploadDroppedFiles(files, {
        directory: target.directory,
        expectedConnection: connectionFence().expectedConnection,
      });
    } catch (error) {
      clearDropTarget();
      onError(error);
      return;
    }
    uploadInProgress = true;
    clearDropTarget({keepStatus: true});
    showUploadStatus(`正在上传 ${fileCount} 个文件到 ${target.label}…`);
    void finishDroppedUpload(uploadTask, {directory: target.directory, fileCount, generation});
  });
  listen(document, 'dragend', () => clearDropTarget());
  listen(elements.tree, 'keydown', (event) => {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    const row = event.target.closest?.('.remote-tree-row');
    if (!row || row.disabled) return;
    const rows = treeRows();
    const index = rows.indexOf(row);
    const path = row.dataset.path;
    const directory = row.dataset.directory === 'true';
    let destination;
    switch (event.key) {
      case 'ArrowDown': destination = rows[index + 1]; break;
      case 'ArrowUp': destination = rows[index - 1]; break;
      case 'Home': destination = rows[0]; break;
      case 'End': destination = rows.at(-1); break;
      case 'ArrowRight':
        if (directory && !expanded.has(path)) {
          event.preventDefault();
          void toggleDirectory(path);
          return;
        }
        if (directory) destination = rows.find(candidate => candidate.dataset.parentPath === path);
        break;
      case 'ArrowLeft':
        if (directory && expanded.has(path)) {
          event.preventDefault();
          void toggleDirectory(path);
          return;
        }
        destination = rows.find(candidate => candidate.dataset.path === row.dataset.parentPath);
        break;
      default: return;
    }
    event.preventDefault();
    if (destination) focusTreePath(destination.dataset.path);
  });
  listen(elements.save, 'click', () => { void saveFile(); });
  listen(elements.splitter, 'pointerdown', (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = explorerWidth;
    elements.splitter.setPointerCapture?.(event.pointerId);
    document.body.dataset.layoutDrag = 'explorer';
    const move = (moveEvent) => applyExplorerWidth(startWidth + moveEvent.clientX - startX);
    const finish = () => {
      elements.splitter.removeEventListener('pointermove', move);
      elements.splitter.removeEventListener('pointerup', finish);
      elements.splitter.removeEventListener('pointercancel', finish);
      delete document.body.dataset.layoutDrag;
      applyExplorerWidth(explorerWidth, {persist: true});
    };
    elements.splitter.addEventListener('pointermove', move);
    elements.splitter.addEventListener('pointerup', finish);
    elements.splitter.addEventListener('pointercancel', finish);
  });
  listen(elements.splitter, 'keydown', (event) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home'].includes(event.key)) return;
    event.preventDefault();
    const step = event.shiftKey ? 48 : 16;
    const next = event.key === 'Home' ? REMOTE_EXPLORER_DEFAULT_WIDTH : explorerWidth + (event.key === 'ArrowLeft' ? -step : step);
    applyExplorerWidth(next, {persist: true});
  });
  listen(elements.splitter, 'dblclick', () => applyExplorerWidth(REMOTE_EXPLORER_DEFAULT_WIDTH, {persist: true}));
  clearEditor();
  applySourceState();
  elements.refresh.disabled = true;
  applyExplorerWidth(explorerWidth);

  return {
    updateConnection,
    refresh,
    setTheme: (theme) => editorController.setTheme(theme),
    layout: () => {
      applyExplorerWidth(explorerWidth);
      editorController.layout();
    },
    hasUnsavedChanges: () => {
      captureActiveEditorView();
      return tabs.hasDirty();
    },
    dispose() {
      const pendingSearch = searchSession.invalidate();
      if (pendingSearch) void cancelSearchToken(pendingSearch);
      readTokens.invalidate();
      disposed = true; requestGeneration += 1;
      if (searchTimer) clearTimeout(searchTimer);
      if (uploadStatusTimer) clearTimeout(uploadStatusTimer);
      resolveQueuedTreeRefresh();
      periodicTreeRefresh.stop();
      clearDropTarget({keepStatus: true});
      listeners.forEach(remove => remove());
      dismissContextMenu();
      markdownPreview.dispose();
      editorController.dispose();
    },
  };
}

