const { app, BrowserWindow, ipcMain, dialog, shell, Menu, safeStorage, clipboard } = require('electron');
const fs = require('node:fs/promises');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { DesktopRuntime, captureLocalDestinationFingerprint } = require('./runtime/index.cjs');
const { SafeStorageCredentialStore } = require('./runtime/credential-store.cjs');
const { ZoomPreferenceStore, WindowZoomController } = require('./runtime/zoom-controller.cjs');
const { resolveTitlebarMenuPosition } = require('./runtime/titlebar-menu-position.cjs');
const { normalizeManifest } = require('./runtime-manifest.cjs');
const { LocalWorkspaceRegistry } = require('./runtime/local-workspace.cjs');
const {
  normalizeGeneratedHtmlReportRequest,
  writeGeneratedHtmlReport,
} = require('./runtime/generated-html-report.cjs');

let runtime, window, zoomController, applicationMenu, closing = false;
const localWorkspaces = new LocalWorkspaceRegistry();
const documentUrl = pathToFileURL(path.join(__dirname, 'dist/index.html')).href;
const TITLEBAR_BASE_HEIGHT = 35;
const nativeMenuState = { theme: 'dark', agentVisible: true, terminalVisible: true };
const nativeMenuThemes = new Set(['dark', 'light', 'hybrid']);
const titlebarMenuIds = new Map([
  ['edit', 'edit-menu'], ['view', 'view-menu'], ['theme', 'theme-menu'],
  ['layout', 'layout-menu'], ['connection', 'connection-menu'],
]);
const runtimeMethods = new Set([
  'getState', 'saveSettings', 'forgetSshPassword', 'connectSsh', 'disconnectSsh', 'startAgent', 'stopAgent',
  'submitAgent', 'terminalInput', 'terminalResize', 'createSshTerminal', 'listSshTerminals', 'inputSshTerminal',
  'resizeSshTerminal', 'closeSshTerminal',
  'execRemote', 'cancelRemoteExec', 'platformRequest', 'cancelPlatformRequest', 'discoverDevices', 'extractHardwareProfile', 'getIntegration',
  'startCalibrationRun', 'calibrationRunStatus', 'cancelCalibrationRun', 'getCalibrationMeasurements',
  'listRemoteFiles', 'prefetchRemoteFiles', 'readRemoteFile', 'saveRemoteFile',
  'searchRemoteFiles', 'cancelRemoteFileSearch', 'replaceRemoteFiles',
  'renameRemoteFile', 'copyRemoteFile',
  'listAgentRuns', 'getAgentRun', 'listCodeAgentSessions', 'getCodeAgentSession', 'configureCodeAgentSessionRoot',
  'inspectServerProfile', 'prepareServerProfile', 'ensureServerProfile', 'ensureRemoteAnalysisWorker', 'ensureRemoteCodeAnalysisRuntime',
  'captureCatalog', 'saveCapturePlan', 'selectCaptureDevice', 'startCapture', 'cancelCapture',
  'listTraces', 'registerTrace', 'getTrace', 'removeTrace', 'listTraceAnalysisSources',
  'prepareTraceArchive', 'uploadTraceArchive', 'discardTraceUpload',
  'uploadCalibrationMeasurements', 'discardCalibrationMeasurementUpload',
]);
const fencedRemoteExplorerMethods = new Set([
  'listRemoteFiles', 'prefetchRemoteFiles', 'readRemoteFile', 'saveRemoteFile',
  'renameRemoteFile', 'copyRemoteFile', 'searchRemoteFiles', 'replaceRemoteFiles',
]);
const openablePaths = new Set();

function sendMenuAction(action) {
  if (!window || window.isDestroyed()) return;
  window.webContents.send('desktop:menu-action', action);
  if (!window.isVisible()) window.show();
  window.focus();
}

function updateWindowTitlebar(factor = zoomController?.factor || 1) {
  if (process.platform === 'darwin' || !window || window.isDestroyed() || typeof window.setTitleBarOverlay !== 'function') return;
  const light = nativeMenuState.theme !== 'dark';
  window.setTitleBarOverlay({
    color: light ? '#f3f3f3' : '#181818',
    symbolColor: light ? '#1f1f1f' : '#cccccc',
    height: Math.round(TITLEBAR_BASE_HEIGHT * factor),
  });
}

function showTitlebarMenu(payload = {}) {
  const item = applicationMenu?.getMenuItemById(titlebarMenuIds.get(String(payload.menu || '')));
  if (!item?.submenu) throw new Error('标题栏菜单不存在。');
  const anchorX = Number(payload.x);
  const anchorY = Number(payload.y);
  if (!Number.isFinite(anchorX) || !Number.isFinite(anchorY) || anchorX < 0 || anchorY < 0)
    throw new Error('标题栏菜单位置无效。');
  const bounds = window.getContentBounds();
  const factor = zoomController?.factor || window.webContents.getZoomFactor() || 1;
  const position = resolveTitlebarMenuPosition({
    anchorX,
    anchorY,
    zoomFactor: factor,
    contentBounds: bounds,
  });
  item.submenu.popup({
    window,
    ...position,
  });
  return { ok: true, position };
}

function updateNativeMenuState(value = {}) {
  if (nativeMenuThemes.has(value.theme)) nativeMenuState.theme = value.theme;
  if (typeof value.agentVisible === 'boolean') nativeMenuState.agentVisible = value.agentVisible;
  if (typeof value.terminalVisible === 'boolean') nativeMenuState.terminalVisible = value.terminalVisible;
  const menu = applicationMenu;
  const selectedTheme = menu?.getMenuItemById(`theme-${nativeMenuState.theme}`);
  if (selectedTheme) selectedTheme.checked = true;
  const agent = menu?.getMenuItemById('layout-agent');
  const terminal = menu?.getMenuItemById('layout-terminal');
  if (agent) agent.checked = nativeMenuState.agentVisible;
  if (terminal) terminal.checked = nativeMenuState.terminalVisible;
  updateWindowTitlebar();
}

function buildApplicationMenu() {
  const themeItem = (label, theme) => ({
    id: `theme-${theme}`, label, type: 'radio', checked: nativeMenuState.theme === theme,
    click: () => sendMenuAction({ type: 'theme', theme }),
  });
  const settingsItem = (label, tab) => ({
    id: `connection-${tab}`, label, click: () => sendMenuAction({ type: 'open-settings', tab }),
  });
  return Menu.buildFromTemplate([
    ...(process.platform === 'darwin' ? [{ label: '工作台', submenu: [{ role: 'about' }, { type: 'separator' }, { role: 'quit' }] }] : []),
    { id: 'edit-menu', label: '编辑', submenu: [{ role: 'undo' }, { role: 'redo' }, { type: 'separator' }, { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' }] },
    { id: 'view-menu', label: '视图', submenu: [
      { label: '恢复默认缩放', accelerator: 'CommandOrControl+0', click: () => zoomController.change('reset') },
      { label: '放大', accelerator: 'CommandOrControl+Plus', click: () => zoomController.change('in') },
      { label: '缩小', accelerator: 'CommandOrControl+-', click: () => zoomController.change('out') },
      { type: 'separator' }, { role: 'togglefullscreen' }, { role: 'toggleDevTools' },
    ] },
    { id: 'theme-menu', label: '主题', submenu: [themeItem('深色', 'dark'), themeItem('浅色', 'light'), themeItem('白 + 黑', 'hybrid')] },
    { id: 'layout-menu', label: '布局', submenu: [
      { id: 'layout-agent', label: '打开 / 关闭助手', type: 'checkbox', checked: nativeMenuState.agentVisible,
        click: item => sendMenuAction({ type: 'layout-pane', pane: 'agent', visible: item.checked }) },
      { id: 'layout-terminal', label: '打开 / 关闭终端', type: 'checkbox', checked: nativeMenuState.terminalVisible,
        click: item => sendMenuAction({ type: 'layout-pane', pane: 'terminal', visible: item.checked }) },
      { type: 'separator' },
      { id: 'layout-reset', label: '重置布局', click: () => sendMenuAction({ type: 'layout-reset' }) },
    ] },
    { id: 'connection-menu', label: '连接配置', submenu: [
      settingsItem('Code Agent…', 'agent'), settingsItem('失败自动重试与授权…', 'automation'),
      settingsItem('SSH 服务器…', 'ssh'),
      settingsItem('服务器环境…', 'server-profile'), settingsItem('工具集成…', 'integration'),
      settingsItem('界面…', 'interface'),
    ] },
  ]);
}

app.setPath('userData', process.env.HETERO_DESKTOP_USER_DATA
  ? path.resolve(process.env.HETERO_DESKTOP_USER_DATA)
  : path.join(app.getPath('appData'), 'HeterogeneousWorkbench'));
if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', () => { if (window) { window.restore(); window.focus(); } });
  app.whenReady().then(async () => {
    const userDataDir = app.getPath('userData');
    runtime = new DesktopRuntime({
      userDataDir, resourcesDir: __dirname,
      executablePath: process.execPath, version: app.getVersion(),
      credentialStore: new SafeStorageCredentialStore({userDataDir, safeStorage}),
    });
    ipcMain.handle('desktop:invoke', async (event, method, payload) => {
      if (event.sender !== window?.webContents || event.senderFrame?.url !== documentUrl)
        throw new Error('请求不是来自工作台主窗口。');
      if (runtimeMethods.has(method)) {
        if (fencedRemoteExplorerMethods.has(method)
            && (!payload?.expectedConnection || Array.isArray(payload.expectedConnection)
              || typeof payload.expectedConnection !== 'object'))
          throw new Error('远程资源管理器连接身份缺失，请刷新文件列表后重试。');
        return runtime[method](payload);
      }
      const ownerId = event.sender.id;
      if (method === 'getDefaultLocalWorkspace') {
        const state = await runtime.getState();
        const configured = String(state?.settings?.agent?.cwd || '').trim();
        if (!configured) return null;
        try { return await localWorkspaces.authorize(ownerId, configured, {source: 'agent-cwd'}); }
        catch { return null; }
      }
      if (method === 'chooseLocalWorkspace') {
        const result = await dialog.showOpenDialog(window, {
          title: '选择本地工作区', properties: ['openDirectory'],
        });
        if (result.canceled || !result.filePaths[0]) return null;
        return localWorkspaces.authorize(ownerId, result.filePaths[0], {source: 'selected'});
      }
      if (method === 'chooseCodeAgentSessionRoot') {
        const result = await dialog.showOpenDialog(window, {
          title: '选择 Code Agent Session 根目录', properties: ['openDirectory'],
        });
        if (result.canceled || !result.filePaths[0]) return null;
        return runtime.configureCodeAgentSessionRoot({rootPath: result.filePaths[0]});
      }
      if (method === 'listLocalFiles') return localWorkspaces.list(ownerId, payload);
      if (method === 'readLocalFile') return localWorkspaces.read(ownerId, payload);
      if (method === 'saveLocalFile') return localWorkspaces.save(ownerId, payload);
      if (method === 'searchLocalFiles') return localWorkspaces.search(ownerId, payload);
      if (method === 'cancelLocalFileSearch') return localWorkspaces.cancelSearch(ownerId, payload?.requestId);
      if (method === 'replaceLocalFiles') return localWorkspaces.replace(ownerId, payload);
      if (method === 'setNativeMenuState') {
        updateNativeMenuState(payload);
        return { ok: true };
      }
      if (method === 'showTitlebarMenu') return showTitlebarMenu(payload);
      if (method === 'writeClipboard') {
        const text = String(payload?.text ?? '');
        if (Buffer.byteLength(text, 'utf8') > 1024 * 1024) throw new Error('复制内容超过 1 MB。');
        clipboard.writeText(text);
        return {ok: true};
      }
      if (method === 'saveGeneratedHtmlReport') {
        const report = normalizeGeneratedHtmlReportRequest(payload);
        const selection = await dialog.showSaveDialog(window, {
          title: '导出分析报告',
          defaultPath: path.join(app.getPath('downloads'), report.filename),
          filters: [{name: 'HTML 报告', extensions: ['html']}],
          properties: ['showOverwriteConfirmation'],
        });
        if (selection.canceled || !selection.filePath) return {canceled: true};
        // The native dialog authorizes the destination. A private sibling
        // temporary file and destination fingerprint keep the final publish
        // atomic without exposing the chosen local path to the renderer.
        return writeGeneratedHtmlReport({
          html: report.html,
          destinationPath: selection.filePath,
          size: report.size,
          sha256: report.sha256,
        });
      }
      if (method === 'downloadRemoteFile') {
        const remotePath = String(payload?.path || '');
        const filename = path.posix.basename(remotePath);
        if (!filename || filename === '.' || filename === '..') throw new Error('请选择要下载的远程文件。');
        const expectedConnection = payload?.expectedConnection;
        if (!expectedConnection || Array.isArray(expectedConnection) || typeof expectedConnection !== 'object')
          throw new Error('远程资源管理器连接身份缺失，请刷新文件列表后重试。');
        // Fence the renderer-visible SSH target before opening a modal dialog.
        // A stale Explorer row must not open a download flow for the Runtime's
        // newly connected target.
        await runtime.validateRemoteFileConnection(expectedConnection);
        const selection = await dialog.showSaveDialog(window, {
          title: '下载远程文件',
          defaultPath: path.join(app.getPath('downloads'), filename),
        });
        if (selection.canceled || !selection.filePath) return {canceled: true};
        // The connection may change while the native Save dialog owns the UI.
        // Revalidate before creating a local partial file or reading chunk 0.
        await runtime.validateRemoteFileConnection(expectedConnection);
        // Freeze the local namespace entry at the moment the user confirms
        // the Save dialog.  A long transfer may otherwise overwrite a file
        // that another application creates or edits before commit.
        const expectedDestinationFingerprint = await captureLocalDestinationFingerprint(selection.filePath);
        const expectedSize = Number.isSafeInteger(payload?.expectedSize) ? payload.expectedSize : undefined;
        const expectedMtimeMs = Number.isSafeInteger(payload?.expectedMtimeMs) ? payload.expectedMtimeMs : undefined;
        const expectedRevision = typeof payload?.expectedRevision === 'string'
          && /^sha256:[0-9a-f]{64}$/.test(payload.expectedRevision)
          ? payload.expectedRevision : undefined;
        // Only after the user selects a destination do we read the remote file.
        // The Runtime streams fixed chunks through its target-fenced Workspace
        // lane into a sibling temporary file and atomically commits it. The
        // sandboxed renderer never receives the chosen local path.
        const remote = await runtime.downloadRemoteFileToLocal({
          path: remotePath,
          destinationPath: selection.filePath,
          ...(Number.isSafeInteger(expectedSize) && expectedSize >= 0 ? {expectedSize} : {}),
          ...(Number.isSafeInteger(expectedMtimeMs) && expectedMtimeMs >= 0 ? {expectedMtimeMs} : {}),
          ...(expectedRevision ? {expectedRevision} : {}),
          expectedConnection,
          expectedDestinationFingerprint,
        });
        openablePaths.add(path.resolve(selection.filePath));
        return {canceled: false, size: remote.size, sha256: remote.sha256};
      }
      if (method === 'choosePath') {
        const result = await dialog.showOpenDialog(window, {
          title: payload?.kind === 'directory' ? '选择工作目录' : '选择文件',
          properties: [payload?.kind === 'directory' ? 'openDirectory' : 'openFile'],
          ...(payload?.kind === 'file' && Array.isArray(payload.filters) ? {filters: payload.filters} : {}),
        });
        const selected = result.canceled ? null : result.filePaths[0];
        if (selected) openablePaths.add(path.resolve(selected));
        return selected;
      }
      if (method === 'importManifest') {
        const result = await dialog.showOpenDialog(window, {
          title: '导入 Agent 配置', properties: ['openFile'], filters: [{ name: 'JSON', extensions: ['json'] }],
        });
        if (result.canceled) return null;
        const source = result.filePaths[0];
        const stat = await fs.stat(source);
        if (stat.size > 1024 * 1024) throw new Error('Agent 配置文件超过 1 MB。');
        return normalizeManifest(JSON.parse(await fs.readFile(source, 'utf8')), path.dirname(source));
      }
      if (method === 'openPath') {
        const target = path.resolve(String(payload?.path || ''));
        const integration = await runtime.getIntegration();
        const allowed = [app.getPath('userData'), integration.mcpConfigPath, integration.cliPath,
          integration.cliPath && path.dirname(integration.cliPath), integration.workspacePath].filter(Boolean);
        if (!allowed.some((p) => target === path.resolve(p)) && !openablePaths.has(target) && !await runtime.capture.isAllowedPath(target))
          throw new Error('请使用文件选择器选择需要打开的位置。');
        // Open folders in the native file manager; reveal files without executing them.
        const stat = await fs.stat(target);
        if (stat.isDirectory()) { const error = await shell.openPath(target); if (error) throw new Error(error); }
        else shell.showItemInFolder(target);
        return { ok: true };
      }
      throw new Error(`未知的桌面操作：${method}`);
    });
    ipcMain.handle('desktop:upload-dropped-files', async (event, payload = {}) => {
      if (event.sender !== window?.webContents || event.senderFrame?.url !== documentUrl)
        throw new Error('请求不是来自工作台主窗口。');
      const localPaths = payload?.localPaths;
      if (!Array.isArray(localPaths) || !localPaths.length || localPaths.length > 64
          || localPaths.some(value => typeof value !== 'string' || !value
            || value.length > 32767 || value.includes('\0')))
        throw new Error('拖拽上传文件列表无效。');
      const expectedConnection = payload?.expectedConnection;
      if (!expectedConnection || Array.isArray(expectedConnection) || typeof expectedConnection !== 'object')
        throw new Error('远程资源管理器连接身份缺失，请刷新文件列表后重试。');
      // Paths arrive only from webUtils.getPathForFile in the isolated preload.
      // They are never accepted through the renderer's generic invoke method
      // and are never returned to the sandboxed page.
      return runtime.uploadRemoteFilesFromLocal({
        localPaths,
        directory: payload?.directory,
        expectedConnection,
      });
    });
    window = new BrowserWindow({
      width: 1500, height: 990, minWidth: 1020, minHeight: 720,
      title: 'Heterogeneous Workbench', backgroundColor: '#181818', show: false,
      icon: path.join(__dirname, 'assets/icon.png'),
      titleBarStyle: 'hidden',
      ...(process.platform === 'darwin'
        ? { trafficLightPosition: { x: 12, y: 11 } }
        : { titleBarOverlay: { color: '#181818', symbolColor: '#cccccc', height: TITLEBAR_BASE_HEIGHT } }),
      autoHideMenuBar: true,
      webPreferences: { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true, nodeIntegration: false, sandbox: true },
    });
    window.once('ready-to-show', () => { if (!window.isDestroyed()) window.show(); });
    const rendererOwnerId = window.webContents.id;
    window.webContents.once('destroyed', () => { void localWorkspaces.revokeOwner(rendererOwnerId); });
    window.webContents.setWindowOpenHandler(({url}) => {
      try {
        const target = new URL(url);
        if (target.protocol === 'https:' || target.protocol === 'http:') {
          void shell.openExternal(target.href).catch(error => console.error('Open external link:', error.message));
        }
      } catch {}
      return { action: 'deny' };
    });
    window.webContents.on('will-navigate', (event, url) => { if (url !== documentUrl) event.preventDefault(); });
    zoomController = new WindowZoomController({
      webContents: window.webContents,
      store: new ZoomPreferenceStore({ userDataDir }),
      onError: error => console.error('Desktop zoom preference:', error.message),
      onChange: factor => updateWindowTitlebar(factor),
    });
    await zoomController.initialize();
    window.webContents.on('before-input-event', (event, input) => zoomController.handleInput(event, input));
    window.webContents.on('will-prevent-unload', (event) => {
      const choice = dialog.showMessageBoxSync(window, {
        type: 'warning',
        title: '工作区文件尚未保存',
        message: '文件编辑器中还有未保存的修改。',
        detail: '关闭工作台将丢失这些修改。',
        buttons: ['继续编辑', '放弃修改'],
        defaultId: 0,
        cancelId: 0,
        noLink: true,
      });
      // Electron uses preventDefault here to ignore the renderer's unload
      // cancellation after the user explicitly chooses to discard edits.
      if (choice === 1) event.preventDefault();
    });
    window.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
    runtime.on('event', (event) => { if (window && !window.isDestroyed()) window.webContents.send('desktop:event', event); });
    applicationMenu = buildApplicationMenu();
    Menu.setApplicationMenu(applicationMenu);
    if (process.platform !== 'darwin') {
      window.removeMenu();
      window.setMenuBarVisibility(false);
    }
    await window.loadURL(documentUrl);
  }).catch((error) => { dialog.showErrorBox('工作台启动失败', error.message); app.exit(1); });
  app.on('window-all-closed', () => app.quit());
  app.on('before-quit', (event) => {
    if (closing) return;
    event.preventDefault();
    // Give the renderer a chance to protect unsaved workspace edits before
    // tearing down SSH, Agent and capture processes. If the user keeps
    // editing, BrowserWindow.close() is cancelled and the runtime stays live.
    if (window && !window.isDestroyed()) { window.close(); return; }
    closing = true;
    Promise.all([runtime?.dispose(), zoomController?.flush()])
      .catch((error) => console.error('Desktop shutdown:', error.message))
      .finally(() => app.quit());
  });
}
