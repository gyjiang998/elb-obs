// Exercises the actual Electron layout and persisted pane geometry without
// touching a physical device, an external SSH server or a model service.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');

async function settle(page) {
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await page.waitForTimeout(120);
}

async function clickNativeMenu(app, id) {
  await app.evaluate(({ BrowserWindow, Menu }, itemId) => {
    const item = Menu.getApplicationMenu()?.getMenuItemById(itemId);
    if (!item?.click) throw new Error(`Native menu item is not actionable: ${itemId}`);
    item.click(item, BrowserWindow.getAllWindows()[0], {});
  }, id);
}

async function sendDesktopEvent(app, event) {
  await app.evaluate(({ BrowserWindow }, value) => {
    const window = BrowserWindow.getAllWindows()[0];
    if (!window || window.isDestroyed()) throw new Error('Desktop renderer is unavailable');
    window.webContents.send('desktop:event', value);
  }, event);
}

async function terminalMarkerColor(page, marker) {
  await page.waitForFunction(value => [...document.querySelectorAll('#agent-terminal .xterm-rows span')]
    .some(element => element.textContent.includes(value)), marker);
  return page.evaluate(value => {
    const element = [...document.querySelectorAll('#agent-terminal .xterm-rows span')]
      .find(candidate => candidate.textContent.includes(value));
    return getComputedStyle(element).color;
  }, marker);
}

async function titlebarSnapshot(page) {
  return page.evaluate(() => {
    const rect = (id) => {
      const value = document.getElementById(id).getBoundingClientRect();
      return { left: value.left, right: value.right, top: value.top, bottom: value.bottom, width: value.width, height: value.height };
    };
    const menuIds = ['titlebar-edit-menu', 'titlebar-view-menu', 'titlebar-theme-menu', 'titlebar-layout-menu', 'titlebar-connection-menu'];
    const titlebar = document.getElementById('desktop-titlebar');
    const icon = document.getElementById('titlebar-app-icon');
    return {
      text: titlebar.innerText,
      menuLabels: menuIds.map(id => document.getElementById(id).textContent.trim()),
      menuRects: Object.fromEntries(menuIds.map(id => [id, rect(id)])),
      titlebar: rect('desktop-titlebar'),
      icon: rect('titlebar-app-icon'),
      iconTag: icon.tagName,
      iconSource: icon.getAttribute('src') || '',
      menuFontSizes: menuIds.map(id => Number.parseFloat(getComputedStyle(document.getElementById(id)).fontSize)),
      bodyFontSize: Number.parseFloat(getComputedStyle(document.body).fontSize),
      cssZoom: getComputedStyle(titlebar).zoom,
      transform: getComputedStyle(titlebar).transform,
    };
  });
}

async function settingsDrawerSnapshot(page) {
  return page.evaluate(() => {
    const rect = (selector) => {
      const value = document.querySelector(selector).getBoundingClientRect();
      return { left: value.left, right: value.right, top: value.top, bottom: value.bottom, width: value.width, height: value.height };
    };
    return {
      viewportHeight: innerHeight,
      titlebar: rect('#desktop-titlebar'),
      backdrop: rect('#settings-backdrop'),
      drawer: rect('#settings-drawer'),
      title: rect('#settings-title'),
      close: rect('#close-settings'),
    };
  });
}

function assertSettingsDrawerGeometry(snapshot, label) {
  assertClose(snapshot.drawer.top, snapshot.titlebar.bottom, `${label}: drawer starts below the custom titlebar`, 1);
  assertClose(snapshot.backdrop.top, snapshot.titlebar.bottom, `${label}: backdrop starts below the custom titlebar`, 1);
  assert(snapshot.title.top >= snapshot.titlebar.bottom,
    `${label}: settings title must not be covered by the custom titlebar`);
  assert(snapshot.close.top >= snapshot.titlebar.bottom,
    `${label}: settings close action must not be covered by the custom titlebar`);
  assertClose(snapshot.drawer.bottom, snapshot.viewportHeight, `${label}: drawer reaches the viewport bottom`, 1);
  assertClose(snapshot.backdrop.bottom, snapshot.viewportHeight, `${label}: backdrop reaches the viewport bottom`, 1);
}

async function nativeMenuSnapshot(app) {
  return app.evaluate(({ Menu }) => {
    const menu = Menu.getApplicationMenu();
    return {
      theme: ['dark', 'light', 'hybrid'].find(value => menu?.getMenuItemById(`theme-${value}`)?.checked),
      agentVisible: menu?.getMenuItemById('layout-agent')?.checked,
      terminalVisible: menu?.getMenuItemById('layout-terminal')?.checked,
      themeLabels: menu?.getMenuItemById('theme-menu')?.submenu?.items.map(item => item.label) || [],
      layoutLabels: menu?.getMenuItemById('layout-menu')?.submenu?.items.filter(item => item.type !== 'separator').map(item => item.label) || [],
      connectionLabels: menu?.getMenuItemById('connection-menu')?.submenu?.items.map(item => item.label) || [],
    };
  });
}

async function geometry(page) {
  return page.evaluate(() => {
    const rect = (selector) => {
      const { x, y, width, height } = document.querySelector(selector).getBoundingClientRect();
      return { x, y, width, height };
    };
    return {
      viewport: { width: innerWidth, height: innerHeight },
      document: { width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight },
      sidebar: rect('.sidebar'), navItems: rect('.nav-items'), firstNavItem: rect('.nav-item'), workspace: rect('.workspace-layout'),
      content: rect('#workspace-content'), agent: rect('#agent-panel'), terminal: rect('#terminal-panel'),
    };
  });
}

async function explorerGeometry(page) {
  return page.evaluate(() => {
    const rect = (selector) => {
      const {x, y, width, height, right, bottom} = document.querySelector(selector).getBoundingClientRect();
      return {x, y, width, height, right, bottom};
    };
    const pageNode = document.getElementById('workbench-page');
    return {
      page: rect('#workbench-page'),
      workspace: rect('#remote-workspace'),
      explorer: rect('#remote-explorer'),
      splitter: rect('#remote-explorer-splitter'),
      editor: rect('.remote-editor'),
      visibleChildren: [...pageNode.children].filter((node) => !node.hidden && getComputedStyle(node).display !== 'none').map((node) => node.id),
      monacoReady: Boolean(document.getElementById('remote-file-editor')?.__remoteEditor?.isMonaco),
      operationsCount: document.querySelectorAll('#operations').length,
      headingCount: pageNode.querySelectorAll('.page-heading').length,
    };
  });
}

async function themeSnapshot(page) {
  return page.evaluate(() => {
    const color = (selector, property = 'backgroundColor') => getComputedStyle(document.querySelector(selector))[property];
    const activeActivity = document.querySelector('.nav-item.active');
    const activeActivityStyle = getComputedStyle(activeActivity);
    const activeActivityIndicator = getComputedStyle(activeActivity, '::before');
    return {
      theme: document.documentElement.dataset.theme,
      stored: localStorage.getItem('hetero.desktop.theme.v1'),
      bodyBackground: color('body'),
      workspaceBackground: color('#workspace-content'),
      agentBackground: color('#agent-panel'),
      terminalBackground: color('#terminal-panel'),
      agentViewportBackground: color('#agent-terminal .xterm-viewport'),
      sshViewportBackground: color('.agent-execution-pane'),
      agentForeground: color('#agent-terminal .xterm-rows', 'color'),
      sshForeground: color('.agent-execution-empty', 'color'),
      agentAnsiForeground: color('#agent-terminal .xterm-rows span', 'color'),
      sshAnsiForeground: color('.agent-execution-empty', 'color'),
      activity: {
        railBackground: color('.sidebar'),
        activeBackground: activeActivityStyle.backgroundColor,
        activeForeground: activeActivityStyle.color,
        activeIconColor: color('.nav-item.active .nav-icon', 'color'),
        indicatorBackground: activeActivityIndicator.backgroundColor,
        indicatorOpacity: activeActivityIndicator.opacity,
        indicatorWidth: activeActivityIndicator.width,
      },
    };
  });
}

function relativeLuminance(cssColor) {
  const channels = String(cssColor).match(/[\d.]+/g)?.slice(0, 3).map(Number);
  assert(channels?.length === 3, `Expected an RGB color, got ${cssColor}`);
  const [red, green, blue] = channels.map(value => {
    const normalized = value / 255;
    return normalized <= 0.04045 ? normalized / 12.92 : ((normalized + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
}

function contrastRatio(foreground, background) {
  const lighter = Math.max(relativeLuminance(foreground), relativeLuminance(background));
  const darker = Math.min(relativeLuminance(foreground), relativeLuminance(background));
  return (lighter + 0.05) / (darker + 0.05);
}

function assertThemeIdentity(snapshot, expected, { persisted = true } = {}) {
  assert.equal(snapshot.theme, expected, `${expected}: root theme marker`);
  if (persisted) assert.equal(snapshot.stored, expected, `${expected}: persisted theme preference`);
}

function assertThemeColors(snapshot, expected) {
  const minimumTextContrast = 4.5;
  const minimumAnsiContrast = 4.5;
  if (expected === 'dark') {
    for (const [name, value] of Object.entries({
      workspace: snapshot.workspaceBackground,
      agent: snapshot.agentBackground,
      terminal: snapshot.terminalBackground,
      agentViewport: snapshot.agentViewportBackground,
      sshViewport: snapshot.sshViewportBackground,
    })) assert(relativeLuminance(value) < 0.08, `dark: ${name} must use a dark surface, got ${value}`);
  } else if (expected === 'light') {
    for (const [name, value] of Object.entries({
      workspace: snapshot.workspaceBackground,
      agent: snapshot.agentBackground,
      terminal: snapshot.terminalBackground,
      agentViewport: snapshot.agentViewportBackground,
      sshViewport: snapshot.sshViewportBackground,
    })) assert(relativeLuminance(value) > 0.75, `light: ${name} must use a light surface, got ${value}`);
  } else {
    assert(relativeLuminance(snapshot.workspaceBackground) > 0.75, `hybrid: workspace must be light, got ${snapshot.workspaceBackground}`);
    assert(relativeLuminance(snapshot.terminalBackground) > 0.75, `hybrid: SSH panel must be light, got ${snapshot.terminalBackground}`);
    assert(relativeLuminance(snapshot.sshViewportBackground) > 0.75, `hybrid: SSH xterm must be light, got ${snapshot.sshViewportBackground}`);
    assert(relativeLuminance(snapshot.agentBackground) < 0.08, `hybrid: Agent panel must be dark, got ${snapshot.agentBackground}`);
    assert(relativeLuminance(snapshot.agentViewportBackground) < 0.08, `hybrid: Agent xterm must be dark, got ${snapshot.agentViewportBackground}`);
  }
  assert(contrastRatio(snapshot.agentForeground, snapshot.agentViewportBackground) >= minimumTextContrast,
    `${expected}: Agent xterm foreground must remain legible`);
  assert(contrastRatio(snapshot.sshForeground, snapshot.sshViewportBackground) >= minimumTextContrast,
    `${expected}: SSH xterm foreground must remain legible`);
  assert(contrastRatio(snapshot.agentAnsiForeground, snapshot.agentViewportBackground) >= minimumAnsiContrast,
    `${expected}: Agent ANSI text must remain legible`);
  assert(contrastRatio(snapshot.sshAnsiForeground, snapshot.sshViewportBackground) >= minimumAnsiContrast,
    `${expected}: SSH ANSI text must remain legible`);
  assert(Math.abs(relativeLuminance(snapshot.activity.activeBackground) - relativeLuminance(snapshot.workspaceBackground)) < 0.01,
    `${expected}: selected activity must visually share the current page surface`);
  assert.equal(snapshot.activity.activeIconColor, snapshot.activity.activeForeground,
    `${expected}: selected activity icon must inherit the blue selection color`);
  assert.notEqual(snapshot.activity.activeForeground, snapshot.activity.railBackground,
    `${expected}: selected activity icon must remain visible on the activity rail`);
  assert.equal(snapshot.activity.indicatorOpacity, '1', `${expected}: selected activity indicator must be visible`);
  assert.equal(snapshot.activity.indicatorWidth, '4px', `${expected}: selected activity indicator must be prominent`);
  assert.equal(snapshot.activity.indicatorBackground, expected === 'dark' ? 'rgb(0, 122, 204)' : 'rgb(0, 120, 212)',
    `${expected}: selected activity indicator must use the theme accent`);
  assert.equal(snapshot.activity.activeForeground, expected === 'dark' ? 'rgb(77, 170, 252)' : 'rgb(0, 120, 212)',
    `${expected}: selected activity icon must use the theme blue`);
}

function assertGeometryUnchanged(actual, expected, label) {
  for (const name of ['sidebar', 'workspace', 'content', 'agent', 'terminal']) {
    for (const dimension of ['x', 'y', 'width', 'height']) {
      assertClose(actual[name][dimension], expected[name][dimension], `${label}: ${name}.${dimension}`, 1);
    }
  }
}

async function drag(page, selector, dx, dy) {
  const box = await page.locator(selector).boundingBox();
  assert(box && box.width && box.height, `${selector} must be an operable resize handle`);
  const x = box.x + box.width / 2, y = box.y + box.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + dx, y + dy, { steps: 12 });
  await page.mouse.up();
  await settle(page);
}

function assertClose(actual, expected, message, tolerance = 3) {
  assert(Math.abs(actual - expected) <= tolerance, `${message}: expected ${expected}, got ${actual}`);
}

function assertContained(value, label) {
  assert(value.document.width <= value.viewport.width + 1, `${label}: document horizontally overflows`);
  assert(value.document.height <= value.viewport.height + 1, `${label}: application frame vertically overflows`);
  for (const name of ['sidebar', 'content', 'agent', 'terminal']) {
    const pane = value[name];
    assert(pane.width > 0 && pane.height > 0, `${label}: ${name} must remain usable`);
    assert(pane.x >= -1 && pane.y >= -1, `${label}: ${name} escapes the viewport origin`);
    assert(pane.x + pane.width <= value.viewport.width + 1, `${label}: ${name} escapes viewport right edge`);
    assert(pane.y + pane.height <= value.viewport.height + 1, `${label}: ${name} escapes viewport bottom edge`);
  }
}

async function main() {
  const { _electron } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
  const output = path.resolve(__dirname, '../test-results');
  fs.mkdirSync(output, { recursive: true });
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-desktop-layout-ui-'));
  const env = { ...process.env, HETERO_DESKTOP_USER_DATA: path.join(temp, 'user-data') };
  delete env.ELECTRON_RUN_AS_NODE;
  const report = { checks: [], errors: [] };
  let app, page;
  async function launch() {
    app = await _electron.launch({
      executablePath: process.env.DESKTOP_ELECTRON_BIN || require('electron'),
      args: process.env.DESKTOP_PACKAGED ? [] : [path.resolve(__dirname, '..')],
      env, timeout: 60000,
    });
    page = await app.firstWindow();
    page.on('pageerror', (error) => report.errors.push(error.message));
    await page.waitForSelector('.app-shell');
    await page.waitForSelector('#desktop-titlebar');
    await page.waitForSelector('#agent-terminal .xterm');
    await page.waitForSelector('.agent-execution-pane');
    await page.waitForFunction(() => Boolean(document.documentElement.dataset.platform));
    await page.waitForSelector('#settings-drawer:not([hidden])');
    await page.locator('#close-settings').click();
    await settle(page);
  }
  try {
    await launch();
    const initial = await geometry(page);
    assertContained(initial, 'default layout');
    assert(initial.agent.x >= initial.content.x + initial.content.width, 'Agent must dock to the right of the current page');
    assertClose(initial.sidebar.width, 50, 'Default navigation uses the VS Code-style activity rail width');
    assertClose(initial.agent.width / initial.viewport.width, 0.4, 'Default Agent occupies forty percent of the window', 0.025);
    assertClose(initial.agent.y, initial.workspace.y, 'Agent starts at the top of the workspace');
    assertClose(initial.agent.height, initial.workspace.height, 'Agent spans the full workbench height');
    assert(initial.terminal.y >= initial.content.y + initial.content.height, 'SSH terminal must dock below the main page');
    assertClose(initial.terminal.x, initial.content.x, 'SSH terminal starts at the page left edge');
    assertClose(initial.terminal.width, initial.content.width, 'SSH terminal stays below the page column');
    assertClose(initial.terminal.height / initial.workspace.height, 1 / 3, 'Default SSH terminal occupies one third of the workspace', 0.025);
    const visualContract = await page.evaluate(() => {
      const root = getComputedStyle(document.documentElement);
      const shell = document.querySelector('.app-shell').getBoundingClientRect();
      return {
        shellTop: shell.top,
        shellHeight: shell.height,
        viewportHeight: innerHeight,
        hasGlobalHeader: Boolean(document.querySelector('.global-header')),
        hasAgentComposer: Boolean(document.querySelector('.agent-composer')),
        hasSshFooter: Boolean(document.querySelector('.ssh-footer')),
        hasDisconnectButton: Boolean(document.getElementById('disconnect-ssh')),
        canvas: root.getPropertyValue('--canvas').trim(),
        surface: root.getPropertyValue('--surface').trim(),
        ink: root.getPropertyValue('--ink').trim(),
        faint: root.getPropertyValue('--faint').trim(),
        line: root.getPropertyValue('--line').trim(),
        blue: root.getPropertyValue('--blue').trim(),
        bodyBackground: getComputedStyle(document.body).backgroundColor,
        sidebarBackground: getComputedStyle(document.querySelector('.sidebar')).backgroundColor,
        activeNavBackground: getComputedStyle(document.querySelector('.nav-item.active')).backgroundColor,
      };
    });
    const titlebar = await titlebarSnapshot(page);
    assertClose(visualContract.shellTop, titlebar.titlebar.bottom, 'Workbench starts immediately below the custom titlebar');
    assertClose(initial.sidebar.y, titlebar.titlebar.bottom, 'Activity rail touches the custom titlebar', 1);
    assertClose(initial.navItems.y, initial.sidebar.y, 'Activity rail navigation has no top inset', 1);
    assertClose(initial.firstNavItem.y, initial.navItems.y, 'First activity begins at the top of the navigation rail', 1);
    assertClose(visualContract.shellHeight + titlebar.titlebar.height, visualContract.viewportHeight,
      'Custom titlebar and workbench fill the renderer');
    const { activeNavBackground, shellHeight, viewportHeight, ...stableVisualContract } = visualContract;
    assert.deepEqual(stableVisualContract, {
      shellTop: titlebar.titlebar.bottom,
      hasGlobalHeader: false, hasAgentComposer: false, hasSshFooter: false, hasDisconnectButton: false,
      canvas: '#181818', surface: '#1e1e1e', ink: '#cccccc', faint: '#8c8c8c', line: '#3c3c3c', blue: '#007acc',
      bodyBackground: 'rgb(30, 30, 30)', sidebarBackground: 'rgb(24, 24, 24)',
    });
    assert.equal(activeNavBackground, 'rgb(30, 30, 30)',
      'Dark selected activity must use the same surface as the current page');
    const remoteAuxiliarySizes = await page.evaluate(() => ['.remote-section-label', '.remote-editor-kind', '.remote-editor-actions > span', '.remote-editor-path', '.remote-editor-empty p', '.remote-editor-footer']
      .map(selector => Number.parseFloat(getComputedStyle(document.querySelector(selector)).fontSize)));
    assert(remoteAuxiliarySizes.every(size => size >= 10), `Remote Explorer auxiliary text is too small: ${remoteAuxiliarySizes.join(', ')}`);
    assert.deepEqual(titlebar.menuLabels, ['编辑', '视图', '主题', '布局', '连接配置']);
    assert.equal(new Set(titlebar.menuFontSizes).size, 1, `Titlebar menus must share one font size: ${titlebar.menuFontSizes.join(', ')}`);
    assert(['1', 'normal'].includes(titlebar.cssZoom), `Titlebar must not counteract page zoom: ${titlebar.cssZoom}`);
    assert.equal(titlebar.transform, 'none', 'Titlebar must not counter-scale itself with a transform');
    assert(!titlebar.text.includes('异构优化工作台'), 'Titlebar must omit the redundant product title');
    assert(!/v\d+\.\d+\.\d+/i.test(titlebar.text), 'Titlebar must omit the product version');
    assert.equal(titlebar.iconTag, 'IMG', 'Application mark must be an image');
    assert(titlebar.iconSource, 'Application mark must have an image source');
    assert(titlebar.icon.right <= titlebar.menuRects['titlebar-edit-menu'].left,
      'Application mark must precede Edit');
    const titlebarCenter = (titlebar.titlebar.top + titlebar.titlebar.bottom) / 2;
    for (const [id, rect] of Object.entries({ ...titlebar.menuRects, 'titlebar-app-icon': titlebar.icon })) {
      assertClose((rect.top + rect.bottom) / 2, titlebarCenter, `${id} shares the single titlebar row`, 1.5);
    }
    assert(titlebar.menuRects['titlebar-connection-menu'].right < titlebar.titlebar.right,
      'Application menus must leave the Window Controls Overlay area unobstructed');
    const nativeMenus = await nativeMenuSnapshot(app);
    assert.deepEqual(nativeMenus.layoutLabels, ['打开 / 关闭助手', '打开 / 关闭终端', '重置布局']);
    assert.deepEqual(nativeMenus.connectionLabels, ['Code Agent…', '失败自动重试与授权…', 'SSH 服务器…', '服务器环境…', '工具集成…', '界面…']);
    assert.deepEqual(nativeMenus.themeLabels, ['深色', '浅色', '白 + 黑']);
    assert.deepEqual({ theme: nativeMenus.theme, agentVisible: nativeMenus.agentVisible, terminalVisible: nativeMenus.terminalVisible },
      { theme: 'dark', agentVisible: true, terminalVisible: true });
    const nativeChrome = await app.evaluate(({ BrowserWindow }) => {
      const target = BrowserWindow.getAllWindows()[0];
      return { platform: process.platform, menuBarVisible: target.isMenuBarVisible(), menuBarAutoHide: target.isMenuBarAutoHide() };
    });
    if (nativeChrome.platform !== 'darwin') {
      assert.equal(nativeChrome.menuBarVisible, false, 'Window Controls Overlay must replace the visible native menu bar');
      assert.equal(nativeChrome.menuBarAutoHide, true, 'Native menu bar must stay auto-hidden below the custom menu triggers');
    }
    report.checks.push('Single Window Controls Overlay titlebar places the app mark, Edit, View, Theme, Layout and Connection in one row without product/version text or a duplicate native menu bar');

    const cleanTheme = await themeSnapshot(page);
    assertThemeIdentity(cleanTheme, 'dark', { persisted: false });
    assert([null, 'dark'].includes(cleanTheme.stored), `Fresh profile must begin with the dark theme, got stored value ${cleanTheme.stored}`);
    assertThemeColors(cleanTheme, 'dark');
    await page.locator('#capture-template-details > summary').click();
    await settle(page);
    const stepDetailsButton = page.locator('.step-config-button').first();
    await stepDetailsButton.evaluate(element => element.click());
    await settle(page);
    const expandedStepDetails = page.locator('.step-detail-row').first();
    assert(await expandedStepDetails.isVisible(), 'Step configuration must expose its detail row');
    const stepDetailsBackground = await expandedStepDetails.locator('td').evaluate(element => getComputedStyle(element).backgroundColor);
    assert(relativeLuminance(stepDetailsBackground) < 0.08, `Dark step details must not leak a light row, got ${stepDetailsBackground}`);
    await stepDetailsButton.evaluate(element => element.click());
    await settle(page);
    const themeGeometry = await geometry(page);
    await page.evaluate(() => {
      window.__themeTerminalNodes = {
        agent: document.querySelector('#agent-terminal .xterm'),
        ssh: document.querySelector('.agent-execution-pane'),
      };
    });
    const switchTheme = async (theme) => {
      await clickNativeMenu(app, `theme-${theme}`);
      await page.waitForFunction(value => document.documentElement.dataset.theme === value, theme);
      await settle(page);
      await page.waitForTimeout(60);
      const snapshot = await themeSnapshot(page);
      assertThemeIdentity(snapshot, theme);
      assertThemeColors(snapshot, theme);
      assert.equal((await nativeMenuSnapshot(app)).theme, theme, `${theme}: native submenu selection`);
      assertGeometryUnchanged(await geometry(page), themeGeometry, `${theme} theme switch`);
      assert(await page.evaluate(() => window.__themeTerminalNodes.agent === document.querySelector('#agent-terminal .xterm')
        && window.__themeTerminalNodes.ssh === document.querySelector('.agent-execution-pane')),
      `${theme}: changing theme must preserve the Agent terminal and bottom execution view`);
      return snapshot;
    };
    await page.screenshot({ path: path.join(output, 'desktop-theme-dark.png'), fullPage: true });
    await switchTheme('light');
    await sendDesktopEvent(app, {
      type: 'terminal', target: 'agent',
      data: '\r\n\u001b[38;5;231mLIGHT_ANSI_256_WHITE\u001b[0m\r\n\u001b[38;2;255;255;255mLIGHT_TRUECOLOR_WHITE\u001b[0m\r\n',
    });
    const lightTerminalBackground = (await themeSnapshot(page)).agentViewportBackground;
    const ansi256White = await terminalMarkerColor(page, 'LIGHT_ANSI_256_WHITE');
    const truecolorWhite = await terminalMarkerColor(page, 'LIGHT_TRUECOLOR_WHITE');
    assert(contrastRatio(ansi256White, lightTerminalBackground) >= 4.5,
      `light: xterm must correct ANSI 256 white, got ${ansi256White} on ${lightTerminalBackground}`);
    assert(contrastRatio(truecolorWhite, lightTerminalBackground) >= 4.5,
      `light: xterm must correct 24-bit truecolor white, got ${truecolorWhite} on ${lightTerminalBackground}`);
    await page.screenshot({ path: path.join(output, 'desktop-theme-light.png'), fullPage: true });
    await switchTheme('hybrid');
    await page.screenshot({ path: path.join(output, 'desktop-theme-hybrid.png'), fullPage: true });
    await switchTheme('dark');
    report.checks.push('Dark, light and hybrid themes recolor live terminal surfaces; light xterm output corrects ANSI 256 and 24-bit white to WCAG AA contrast');

    await switchTheme('hybrid');
    await page.reload();
    await page.waitForSelector('#agent-terminal .xterm');
    await page.waitForSelector('.agent-execution-pane');
    await page.waitForSelector('#settings-drawer:not([hidden])');
    await page.locator('#close-settings').click();
    await settle(page);
    const reloadedTheme = await themeSnapshot(page);
    assertThemeIdentity(reloadedTheme, 'hybrid');
    assertThemeColors(reloadedTheme, 'hybrid');
    assertGeometryUnchanged(await geometry(page), themeGeometry, 'hybrid theme renderer reload');
    await app.close(); app = null;
    await launch();
    const restartedTheme = await themeSnapshot(page);
    assertThemeIdentity(restartedTheme, 'hybrid');
    assertThemeColors(restartedTheme, 'hybrid');
    assertGeometryUnchanged(await geometry(page), themeGeometry, 'hybrid theme app restart');
    await clickNativeMenu(app, 'theme-dark');
    await page.waitForFunction(() => document.documentElement.dataset.theme === 'dark');
    await settle(page);
    assertThemeIdentity(await themeSnapshot(page), 'dark');
    report.checks.push('Titlebar theme menu updates live xterms and hybrid theme survives renderer reload and complete app restart');

    await clickNativeMenu(app, 'connection-ssh');
    await page.waitForFunction(() => !document.getElementById('settings-drawer').hidden && !document.getElementById('settings-ssh').hidden);
    assertSettingsDrawerGeometry(await settingsDrawerSnapshot(page), 'default zoom settings drawer');
    await page.locator('#close-settings').click();
    await clickNativeMenu(app, 'connection-automation');
    await page.waitForFunction(() => !document.getElementById('settings-drawer').hidden && !document.getElementById('settings-automation').hidden);
    assert.equal(await page.locator('#automation-auto-recover-errors').isChecked(), false);
    assert.equal(await page.locator('#automation-allow-device-operations').isChecked(), false);
    assert.equal(await page.locator('#automation-allow-server-operations').isChecked(), false);
    await page.locator('#close-settings').click();
    report.checks.push('Titlebar connection menu opens SSH and opt-in automation configuration below the Window Controls Overlay row');

    const captureActivity = page.locator('[data-page="capture"]');
    const activityLabel = captureActivity.locator('.nav-text');
    const activityGeometry = await geometry(page);
    assert.equal(await activityLabel.evaluate((element) => getComputedStyle(element).opacity), '0');
    await page.locator('.local-label').hover();
    await page.waitForFunction(() => document.getElementById('sidebar-panel').dataset.flyoutOpen === 'true');
    await page.waitForFunction(() => [...document.querySelectorAll('#sidebar-panel .nav-text')].every(label => getComputedStyle(label).opacity === '1'));
    const railBox = await page.locator('#sidebar-panel').boundingBox();
    const expandedActivityBox = await captureActivity.boundingBox();
    await page.mouse.move(expandedActivityBox.x + expandedActivityBox.width - 12, railBox.y + 100);
    await page.waitForTimeout(180);
    assert.equal(await page.locator('#sidebar-panel').getAttribute('data-flyout-open'), 'true',
      'Moving from the rail into the floating label area must keep every label open');
    assert.equal(await activityLabel.textContent(), '负载采样');
    assert.equal(await page.locator('[data-page="hardware-models"] .nav-text').evaluate((element) => getComputedStyle(element).opacity), '1');
    assert.equal(await page.locator('[data-page="traces"] .nav-text').evaluate((element) => getComputedStyle(element).opacity), '1');
    assert.equal(await page.locator('#connection-settings-nav .nav-text').evaluate((element) => getComputedStyle(element).opacity), '1');
    const activityRailSpacing = await page.locator('[data-page="optimization-knowledge"]').evaluate((element) => {
      const icon = element.querySelector('.nav-icon').getBoundingClientRect();
      const label = element.querySelector('.nav-text').getBoundingClientRect();
      const item = element.getBoundingClientRect();
      return {
        left: item.left,
        width: item.width,
        iconToLabel: label.left - icon.right,
        labelToRight: item.right - label.right,
      };
    });
    assert.equal(activityRailSpacing.width, 164, 'Expanded activity rail should stay compact');
    assert(activityRailSpacing.iconToLabel >= 18,
      'Expanded activity labels must keep a readable gap after the icon');
    assert(activityRailSpacing.labelToRight <= 50,
      'Expanded activity labels must not leave an oversized empty right gutter');
    const settingsRailSpacing = await page.locator('#connection-settings-nav').evaluate((element) => {
      const icon = element.querySelector('.nav-icon').getBoundingClientRect();
      const label = element.querySelector('.nav-text').getBoundingClientRect();
      const item = element.getBoundingClientRect();
      return {left: item.left, width: item.width, iconToLabel: label.left - icon.right};
    });
    assert.equal(settingsRailSpacing.left, activityRailSpacing.left,
      'Bottom settings row must align with the feature rows instead of centering around the 50px rail');
    assert.equal(settingsRailSpacing.width, activityRailSpacing.width);
    assert(settingsRailSpacing.iconToLabel >= 18);
    assertGeometryUnchanged(await geometry(page), activityGeometry, 'activity rail overlay');
    assert.equal(await captureActivity.getAttribute('title'), null, 'Expanded labels replace native title tooltips');
    await captureActivity.click();
    await page.waitForFunction(() => document.getElementById('sidebar-panel').dataset.flyoutOpen === 'false');
    await page.waitForFunction(() => getComputedStyle(document.querySelector('[data-page="capture"] .nav-text')).opacity === '0');
    assert.equal(await activityLabel.evaluate((element) => getComputedStyle(element).opacity), '0',
      'Clicking an activity must dismiss the complete flyout');
    await page.mouse.move(400, 400);
    await page.locator('#sidebar-panel').hover({position: {x: 24, y: 8}});
    await page.waitForFunction(() => document.getElementById('sidebar-panel').dataset.flyoutOpen === 'true');
    const inactiveActivity = page.locator('.nav-items [data-page]:not(.active)').first();
    await inactiveActivity.hover();
    await page.waitForTimeout(160);
    assert.equal(await inactiveActivity.evaluate((element) => getComputedStyle(element).backgroundColor), 'rgb(42, 45, 46)',
      'Dark activity hover must be visible without competing with the selected page');
    await page.mouse.move(400, 400);
    await captureActivity.focus();
    await page.keyboard.press('Tab');
    assert.equal(await inactiveActivity.evaluate((element) => document.activeElement === element), true,
      'Keyboard traversal must reach the next activity');
    const activityFocus = await inactiveActivity.evaluate((element) => {
      const style = getComputedStyle(element);
      return { color: style.outlineColor, style: style.outlineStyle, width: style.outlineWidth };
    });
    assert.deepEqual(activityFocus, { color: 'rgb(0, 122, 204)', style: 'solid', width: '2px' },
      'Keyboard focus on the activity rail must remain prominent');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => document.getElementById('sidebar-panel').dataset.flyoutOpen === 'false');
    await page.keyboard.press('Tab');
    await page.waitForFunction(() => document.getElementById('sidebar-panel').dataset.flyoutOpen === 'true');
    const connectionSettingsActivity = page.locator('#connection-settings-nav');
    assert.equal(await connectionSettingsActivity.locator('.nav-text').textContent(), '连接配置');
    await connectionSettingsActivity.click();
    await page.waitForFunction(() => !document.getElementById('settings-drawer').hidden && !document.getElementById('settings-agent').hidden);
    await page.locator('#tab-interface').click();
    await page.locator('#activity-rail-expand-on-hover').uncheck();
    await page.locator('#save-settings').click();
    await page.waitForFunction(() => document.getElementById('sidebar-panel').dataset.labelMode === 'single');
    await page.locator('#close-settings').click();
    await page.reload();
    await page.waitForSelector('#agent-terminal .xterm');
    await page.waitForFunction(() => document.getElementById('sidebar-panel').dataset.labelMode === 'single');
    await page.waitForSelector('#settings-drawer:not([hidden])');
    await page.locator('#close-settings').click();
    await captureActivity.hover();
    await page.waitForFunction(() => getComputedStyle(document.querySelector('[data-page="capture"] .nav-text')).opacity === '1');
    assert.equal(await page.locator('[data-page="hardware-models"] .nav-text').evaluate((element) => getComputedStyle(element).opacity), '0',
      'Disabled expansion must retain the previous single-label tooltip');
    await connectionSettingsActivity.click();
    await page.locator('#tab-interface').click();
    await page.locator('#activity-rail-expand-on-hover').check();
    await page.locator('#save-settings').click();
    await page.locator('#close-settings').click();
    report.checks.push('Activity rail shows every label in a non-layout overlay, supports single-label fallback in Interface settings, and preserves keyboard focus');
    await page.evaluate(() => {
      window.__layoutNodes = {
        agent: document.querySelector('#agent-terminal .xterm'),
        ssh: document.querySelector('.agent-execution-pane'),
      };
    });
    const analysisPages = ['capture', 'workload-features', 'hardware-models', 'optimization-calibration', 'parallel-modeling'];
    assert.deepEqual(await page.locator('.nav-items [data-page]').evaluateAll(elements => elements.slice(0, 5).map(element => element.dataset.page)), analysisPages,
      'Analysis navigation must follow capture, workload features, hardware models, calibration, modeling');
    for (const name of ['workload-features', 'hardware-models', 'optimization-calibration', 'parallel-modeling', 'traces', 'workbench', 'server-profile', 'devices', 'capture']) {
      await page.locator(`[data-page="${name}"]`).click();
      assert(await page.locator(`#${name}-page`).isVisible());
      assert(await page.locator('#agent-panel').isVisible(), `${name}: global Agent must remain visible`);
      assert(await page.locator('#terminal-panel').isVisible(), `${name}: global SSH terminal must remain visible`);
    }
    await page.locator('[data-page="workload-features"]').click();
    await page.waitForSelector('#workload-features-root .workload-features-studio');
    await settle(page);
    assert(await page.locator('#workload-features-root h2', {hasText: '负载特征'}).isVisible(), 'Shared React workload page must mount in the Desktop shell');
    await page.screenshot({ path: path.join(output, 'desktop-workload-features.png'), fullPage: true });
    report.checks.push('Shared Web workload-features React page mounts inside Desktop without replacing Agent or SSH panes');

    const hardwareAgentWasVisible = await page.locator('#agent-panel').isVisible();
    const hardwareTerminalWasVisible = await page.locator('#terminal-panel').isVisible();
    if (hardwareAgentWasVisible) await clickNativeMenu(app, 'layout-agent');
    if (hardwareTerminalWasVisible) await clickNativeMenu(app, 'layout-terminal');
    await settle(page);
    await page.locator('[data-page="hardware-models"]').click();
    await page.waitForSelector('#hardware-models-root .hardware-models-studio');
    await settle(page);
    const hardwareExtractGeometry = await page.evaluate(() => {
      const rect = (element) => {
        const {top, bottom, height} = element.getBoundingClientRect();
        return {top, bottom, height};
      };
      const row = document.querySelector('.hardware-extract-row');
      const select = row?.querySelector('select');
      return {select: select && rect(select), buttons: [...(row?.querySelectorAll('button') || [])].map(rect)};
    });
    assert(hardwareExtractGeometry.select && hardwareExtractGeometry.buttons.length === 2, 'Hardware extraction should show one device selector and two actions');
    assert(hardwareExtractGeometry.buttons.every((button) =>
      Math.abs(button.top - hardwareExtractGeometry.select.top) < 1
      && Math.abs(button.bottom - hardwareExtractGeometry.select.bottom) < 1
      && Math.abs(button.height - hardwareExtractGeometry.select.height) < 1),
    `Device selector and read/extract actions should have exactly aligned 38 px controls: ${JSON.stringify(hardwareExtractGeometry)}`);
    const hardwareGroupGeometry = await page.locator('.hardware-group-grid > .hardware-group-card').evaluateAll((cards) => cards.map((card) => {
      const {top, bottom, height} = card.getBoundingClientRect();
      return {top, bottom, height, marginTop: getComputedStyle(card).marginTop};
    }));
    assert.equal(hardwareGroupGeometry.length, 3, 'The default editor should expose big, middle and small core-group cards');
    assert(hardwareGroupGeometry.every((card) =>
      Math.abs(card.top - hardwareGroupGeometry[0].top) < 1
      && Math.abs(card.bottom - hardwareGroupGeometry[0].bottom) < 1
      && Math.abs(card.height - hardwareGroupGeometry[0].height) < 1
      && card.marginTop === '0px'),
    `Core-group cards should share one aligned, equal-height grid row: ${JSON.stringify(hardwareGroupGeometry)}`);
    const hardwareProfileGeometry = await page.evaluate(async () => {
      const host = document.createElement('div');
      host.className = 'hardware-profile-list';
      host.dataset.layoutFixture = 'hardware-profile-cards';
      host.innerHTML = `
        <article class="pm-box hardware-profile-card"><div class="hardware-card-heading"><strong>模型 A</strong></div><div class="hardware-topology"><div><span>大核</span><strong>1 核</strong></div></div><p class="pm-hint">较短说明。</p><div class="hardware-profile-footer"><code>model-a</code><span>参考模型</span></div></article>
        <article class="pm-box hardware-profile-card"><div class="hardware-card-heading"><strong>模型 B</strong></div><div class="hardware-topology"><div><span>大核</span><strong>1 核</strong></div></div><p class="pm-hint">这段说明刻意更长，用于确认同一网格行里的模型卡会拉伸到一致高度，同时保持底部信息对齐。</p><div class="hardware-profile-footer"><code>model-b</code><span>拓扑迁移模型</span></div></article>`;
      document.querySelector('.hardware-models-studio').append(host);
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      const cards = [...host.querySelectorAll('.hardware-profile-card')].map((card) => {
        const {top, bottom, height} = card.getBoundingClientRect();
        return {top, bottom, height, marginTop: getComputedStyle(card).marginTop};
      });
      host.remove();
      return cards;
    });
    assert.equal(hardwareProfileGeometry.length, 2);
    assert(hardwareProfileGeometry.every((card) =>
      Math.abs(card.top - hardwareProfileGeometry[0].top) < 1
      && Math.abs(card.bottom - hardwareProfileGeometry[0].bottom) < 1
      && Math.abs(card.height - hardwareProfileGeometry[0].height) < 1
      && card.marginTop === '0px'),
    `Registered hardware model cards should share one aligned, equal-height grid row: ${JSON.stringify(hardwareProfileGeometry)}`);
    await page.screenshot({ path: path.join(output, 'desktop-hardware-models.png'), fullPage: true });
    report.checks.push('Hardware extraction controls, core-group cards and registered-model cards use aligned equal-height rows');
    if (hardwareAgentWasVisible) await clickNativeMenu(app, 'layout-agent');
    if (hardwareTerminalWasVisible) await clickNativeMenu(app, 'layout-terminal');
    await settle(page);

    await page.locator('[data-page="optimization-calibration"]').click();
    await page.waitForSelector('#optimization-calibration-root .calibration-studio');
    await page.waitForFunction(() => !document.querySelector('#optimization-calibration-root .pm-skeleton'));
    await settle(page);
    assert(await page.locator('#optimization-calibration-root h2', {hasText: '寻优校准'}).isVisible(), 'Shared React calibration page must mount in the Desktop shell');
    await page.screenshot({ path: path.join(output, 'desktop-optimization-calibration.png'), fullPage: true });

    await page.locator('[data-page="parallel-modeling"]').click();
    await page.waitForSelector('#parallel-modeling-root .parallel-studio');
    await settle(page);
    assert(await page.locator('#parallel-modeling-root h2', {hasText: '并行建模'}).isVisible(), 'Shared React modeling page must mount in the Desktop shell');
    await page.screenshot({ path: path.join(output, 'desktop-parallel-modeling.png'), fullPage: true });
    assert(await page.evaluate(() => window.__layoutNodes.agent === document.querySelector('#agent-terminal .xterm') && window.__layoutNodes.ssh === document.querySelector('.agent-execution-pane')),
      'Mounting shared analysis pages must preserve the original terminal DOM');
    report.checks.push('Shared Web calibration and parallel-modeling pages mount in the requested order and preserve both terminals');
    report.checks.push('Page navigation preserves the full-height right-side Agent and collapsible page-column SSH terminal');

    await page.locator('[data-page="workbench"]').click();
    await settle(page);
    const explorerInitial = await explorerGeometry(page);
    assert.deepEqual(explorerInitial.visibleChildren, ['remote-workspace'],
      'Explorer page should dedicate its complete canvas to the remote workspace');
    assert.equal(explorerInitial.headingCount, 0, 'Dedicated Explorer should not retain a generic workbench heading');
    assert.equal(explorerInitial.operationsCount, 0, 'Remote execution history belongs to the fixed terminal, not the Explorer page');
    assert.equal(explorerInitial.monacoReady, true, 'Explorer should mount the Monaco editor adapter even before a file is opened');
    assert(await page.locator('#workspace-source-remote').isVisible(), 'Explorer exposes the remote workspace source');
    assert(await page.locator('#workspace-source-local').isVisible(), 'Explorer exposes the local workspace source in the same activity');
    assert(await page.locator('#workspace-search-query').isVisible(), 'Search stays inline above the Explorer tree');
    assert.equal(await page.locator('[data-page="search"]').count(), 0, 'Search does not add another activity item');
    assertClose(explorerInitial.workspace.x, explorerInitial.page.x, 'Remote workspace fills Explorer page from the left', 1);
    assertClose(explorerInitial.workspace.y, explorerInitial.page.y, 'Remote workspace fills Explorer page from the top', 1);
    assertClose(explorerInitial.workspace.width, explorerInitial.page.width, 'Remote workspace fills Explorer page width, allowing only the page scrollbar gutter', 16);
    assertClose(explorerInitial.workspace.height, explorerInitial.page.height, 'Remote workspace fills Explorer page height', 1);
    assertClose(explorerInitial.explorer.right, explorerInitial.splitter.x, 'File tree meets the resize gutter', 1);
    assertClose(explorerInitial.splitter.right, explorerInitial.editor.x, 'Editor starts directly after the resize gutter', 1);

    await page.locator('[data-page="workbench"]').click();
    await settle(page);
    const explorerRepeated = await explorerGeometry(page);
    assert(await page.locator('#remote-explorer').isVisible(), 'Repeated clicks on the active Explorer icon keep the server file tree visible');
    assertClose(explorerRepeated.explorer.width, explorerInitial.explorer.width,
      'Repeated clicks on the active Explorer icon do not collapse the server file tree', 1);
    assertClose(explorerRepeated.editor.width, explorerInitial.editor.width,
      'Repeated clicks on the active Explorer icon leave the editor canvas stable', 1);

    await drag(page, '#remote-explorer-splitter', 72, 0);
    const explorerResized = await explorerGeometry(page);
    assert(explorerResized.explorer.width > explorerInitial.explorer.width + 50,
      'Dragging the Explorer divider right must enlarge the remote file tree');
    assert(explorerResized.editor.width < explorerInitial.editor.width - 50,
      'Dragging the Explorer divider right must give the corresponding width back from the editor');
    const explorerDivider = page.locator('#remote-explorer-splitter');
    assert.equal(await explorerDivider.getAttribute('role'), 'separator');
    assert.notEqual(await explorerDivider.getAttribute('aria-valuenow'), null);
    await explorerDivider.focus();
    await page.keyboard.press('ArrowLeft');
    await settle(page);
    const explorerKeyboardResized = await explorerGeometry(page);
    assert(explorerKeyboardResized.explorer.width < explorerResized.explorer.width,
      'Explorer divider supports keyboard resizing');
    report.checks.push('Explorer owns the complete page, stays open on repeated activity clicks, and provides a draggable accessible tree/editor divider');
    await page.screenshot({ path: path.join(output, 'desktop-layout-default.png'), fullPage: true });

    await page.locator('#collapse-terminal').click();
    assert.equal(await page.locator('#terminal-panel').isVisible(), false);
    await settle(page);
    assert.equal((await nativeMenuSnapshot(app)).terminalVisible, false);
    await clickNativeMenu(app, 'layout-terminal');
    await settle(page);
    assert(await page.locator('#terminal-panel').isVisible());
    assert.equal((await nativeMenuSnapshot(app)).terminalVisible, true);
    report.checks.push('SSH terminal can be folded from its panel header and restored from the titlebar layout menu');

    await drag(page, '#agent-splitter', -90, 0);
    const agentResized = await geometry(page);
    assert(agentResized.agent.width > initial.agent.width + 50, 'Dragging Agent divider left must enlarge Agent pane');
    await drag(page, '#terminal-splitter', 0, -65);
    const terminalResized = await geometry(page);
    assert(terminalResized.terminal.height > initial.terminal.height + 40, 'Dragging terminal divider up must enlarge terminal pane');
    assertClose(terminalResized.agent.height, initial.agent.height, 'Resizing the terminal leaves the full-height Agent unchanged');
    assert(terminalResized.content.height < initial.content.height - 40, 'Dragging the terminal upward reduces the page row');
    report.checks.push('Pointer dragging resizes Agent width and SSH terminal height independently');

    for (const [selector, key, pane, axis] of [
      ['#agent-splitter', 'ArrowLeft', 'agent', 'width'],
      ['#terminal-splitter', 'ArrowUp', 'terminal', 'height'],
    ]) {
      const handle = page.locator(selector);
      assert.equal(await handle.getAttribute('role'), 'separator');
      assert.notEqual(await handle.getAttribute('aria-valuenow'), null);
      const before = await geometry(page);
      await handle.focus();
      await page.keyboard.press(key);
      await settle(page);
      const after = await geometry(page);
      assert(after[pane][axis] > before[pane][axis], `${selector} must support ${key} keyboard resizing`);
    }
    report.checks.push('Agent and terminal splitters expose accessible separator semantics and keyboard resizing');

    const resized = await geometry(page);
    await page.screenshot({ path: path.join(output, 'desktop-layout-resized.png'), fullPage: true });
    await clickNativeMenu(app, 'layout-agent');
    await clickNativeMenu(app, 'layout-terminal');
    assert.equal(await page.locator('#agent-panel').isVisible(), false);
    assert.equal(await page.locator('#terminal-panel').isVisible(), false);
    await page.locator('[data-page="traces"]').click();
    await clickNativeMenu(app, 'layout-agent');
    await clickNativeMenu(app, 'layout-terminal');
    await settle(page);
    assert(await page.locator('#agent-panel').isVisible());
    assert(await page.locator('#terminal-panel').isVisible());
    assert(await page.evaluate(() => window.__layoutNodes.agent === document.querySelector('#agent-terminal .xterm') && window.__layoutNodes.ssh === document.querySelector('.agent-execution-pane')), 'Collapsing or changing pages must preserve the original terminal DOM');
    const restored = await geometry(page);
    assertClose(restored.agent.width, resized.agent.width, 'Restored Agent width');
    assertClose(restored.terminal.height, resized.terminal.height, 'Restored terminal height');
    report.checks.push('Collapsing and restoring panes keeps both original xterm instances and restores pane sizes');

    await page.reload();
    await page.waitForSelector('#agent-terminal .xterm');
    await page.waitForSelector('#settings-drawer:not([hidden])');
    await page.locator('#close-settings').click();
    await settle(page);
    const reloaded = await geometry(page);
    assertClose(reloaded.sidebar.width, 50, 'Activity rail remains fixed on reload');
    assertClose(reloaded.agent.width, resized.agent.width, 'Agent width persists on reload');
    assertClose(reloaded.terminal.height, resized.terminal.height, 'Terminal height persists on reload');
    await app.close(); app = null;
    await launch();
    const reopened = await geometry(page);
    assertClose(reopened.sidebar.width, 50, 'Activity rail remains fixed on restart');
    assertClose(reopened.agent.width, resized.agent.width, 'Agent width persists on restart');
    assertClose(reopened.terminal.height, resized.terminal.height, 'Terminal height persists on restart');
    report.checks.push('Adjusted pane sizes survive renderer reload and complete app restart');

    await clickNativeMenu(app, 'layout-agent');
    await clickNativeMenu(app, 'layout-terminal');
    await page.reload();
    await page.waitForSelector('#agent-terminal .xterm', { state: 'attached' });
    await page.waitForSelector('#settings-drawer:not([hidden])');
    await page.locator('#close-settings').click();
    await settle(page);
    assert.equal(await page.locator('#agent-panel').isVisible(), false);
    assert.equal(await page.locator('#terminal-panel').isVisible(), false);
    await clickNativeMenu(app, 'layout-agent');
    await clickNativeMenu(app, 'layout-terminal');
    await settle(page);
    report.checks.push('Collapsed pane preferences survive reload and remain recoverable from the titlebar layout menu');

    await app.browserWindow(page).then((window) => window.evaluate((browserWindow) => browserWindow.setSize(1020, 720)));
    await settle(page);
    for (const name of ['capture', 'workload-features', 'hardware-models', 'optimization-calibration', 'parallel-modeling', 'traces', 'workbench', 'server-profile', 'devices']) {
      await page.locator(`[data-page="${name}"]`).click();
      await settle(page);
      assertContained(await geometry(page), `compact ${name} page`);
    }
    await page.locator('[data-page="capture"]').click();
    await settle(page);
    await page.screenshot({ path: path.join(output, 'desktop-layout-compact.png'), fullPage: true });
    report.checks.push('All nine pages fit a 1020 × 720 window with Agent and SSH panes still accessible');

    await app.browserWindow(page).then(window => window.evaluate(browserWindow => browserWindow.webContents.setZoomFactor(1.1)));
    await settle(page);
    await page.mouse.move(400, 400);
    await page.locator('#sidebar-panel').hover({position: {x: 24, y: 8}});
    await page.waitForFunction(() => document.getElementById('sidebar-panel').dataset.flyoutOpen === 'true');
    await page.locator('.nav-items').evaluate(element => { element.scrollTop = element.scrollHeight; });
    await settle(page);
    const compactRail = await page.evaluate(() => {
      const rect = selector => document.querySelector(selector).getBoundingClientRect();
      return {nav: rect('.nav-items'), last: rect('[data-page="experience"]'), settings: rect('#connection-settings-nav'), sidebar: rect('#sidebar-panel')};
    });
    assert(compactRail.last.top >= compactRail.nav.top - 1 && compactRail.last.bottom <= compactRail.nav.bottom + 1,
      `Zoomed activity rail must scroll the final feature into view: ${JSON.stringify(compactRail)}`);
    assert(compactRail.settings.top >= compactRail.sidebar.top - 1 && compactRail.settings.bottom <= compactRail.sidebar.bottom + 1,
      'Connection settings must remain fixed and operable below the scrolling feature list');
    await app.browserWindow(page).then(window => window.evaluate(browserWindow => browserWindow.webContents.setZoomFactor(1)));
    await settle(page);
    report.checks.push('Compact zoom keeps the complete activity list scrollable and connection settings fixed at the bottom');

    await page.locator('#agent-splitter').focus();
    await page.keyboard.press('Home');
    await settle(page);
    const minimumHeader = await page.evaluate(() => {
      const panel = document.querySelector('#agent-panel').getBoundingClientRect();
      const title = document.querySelector('#agent-panel h2');
      const actions = document.querySelector('#agent-panel .panel-actions').getBoundingClientRect();
      const initialize = document.querySelector('#agent-initialize').getBoundingClientRect();
      const settings = document.querySelector('#agent-settings').getBoundingClientRect();
      return {
        width: panel.width,
        titleRight: title.getBoundingClientRect().left + title.scrollWidth,
        actionsLeft: actions.left,
        actionsRight: actions.right,
        panelRight: panel.right,
        initializeLeft: initialize.left,
        initializeRight: initialize.right,
        settingsLeft: settings.left,
      };
    });
    await page.screenshot({ path: path.join(output, 'desktop-layout-minimum-agent.png'), fullPage: true });
    assert(minimumHeader.titleRight <= minimumHeader.actionsLeft, `Minimum Agent width overlaps title and actions: ${JSON.stringify(minimumHeader)}`);
    assert(minimumHeader.actionsRight <= minimumHeader.panelRight, 'Minimum Agent width clips header actions');
    assert(minimumHeader.initializeLeft >= minimumHeader.actionsLeft && minimumHeader.initializeRight <= minimumHeader.settingsLeft,
      'Agent initialization action must remain visible immediately left of settings');
    report.checks.push('Minimum Agent width keeps title, status, initialization and settings actions separated and visible');

    await app.browserWindow(page).then((window) => window.evaluate((browserWindow) => browserWindow.setSize(1500, 990)));
    await settle(page);
    await clickNativeMenu(app, 'layout-agent');
    await clickNativeMenu(app, 'layout-terminal');
    await clickNativeMenu(app, 'layout-reset');
    await settle(page);
    assert(await page.locator('#agent-panel').isVisible());
    assert(await page.locator('#terminal-panel').isVisible());
    const reset = await geometry(page);
    assertClose(reset.agent.width, initial.agent.width, 'Reset restores default Agent width');
    assertClose(reset.sidebar.width, 50, 'Reset keeps the fixed activity rail width');
    assertClose(reset.terminal.height / reset.workspace.height, 1 / 3, 'Reset restores one-third-height terminal', 0.025);
    assertClose(reset.agent.height, reset.workspace.height, 'Reset restores the full-height Agent');
    assertClose(reset.terminal.x, reset.content.x, 'Reset aligns terminal with the page left edge');
    assertClose(reset.terminal.width, reset.content.width, 'Reset keeps terminal within the page column');
    assertContained(reset, 'reset layout');
    report.checks.push('Reset restores default widths, visible panels, a full-height Agent and page-column terminal');

    const zoomFactor = async () => app.browserWindow(page).then(browserWindow => browserWindow.evaluate(value => value.webContents.getZoomFactor()));
    const pressZoom = async (keyCode, modifiers = ['control']) => {
      await app.browserWindow(page).then(browserWindow => browserWindow.evaluate((value, input) => {
        value.webContents.sendInputEvent({ type: 'keyDown', keyCode: input.keyCode, modifiers: input.modifiers });
        value.webContents.sendInputEvent({ type: 'keyUp', keyCode: input.keyCode, modifiers: input.modifiers });
      }, { keyCode, modifiers }));
      await settle(page);
    };
    const titlebarTypography = async () => {
      const snapshot = await titlebarSnapshot(page);
      const factor = await zoomFactor();
      return {
        factor,
        cssSizes: snapshot.menuFontSizes,
        renderedSizes: snapshot.menuFontSizes.map(size => size * factor),
        cssZoom: snapshot.cssZoom,
        transform: snapshot.transform,
      };
    };
    await page.locator('#agent-terminal .xterm-helper-textarea').focus();
    const defaultTitlebarTypography = await titlebarTypography();
    await pressZoom('+', ['control', 'shift']);
    assertClose(await zoomFactor(), 1.1, 'Ctrl++ enlarges the entire workbench', 0.01);
    await page.locator('#connection-settings-nav').click();
    await page.waitForFunction(() => !document.getElementById('settings-drawer').hidden);
    assertSettingsDrawerGeometry(await settingsDrawerSnapshot(page), 'enlarged settings drawer');
    await page.locator('#close-settings').click();
    const enlargedTitlebarTypography = await titlebarTypography();
    assert.deepEqual(enlargedTitlebarTypography.cssSizes, defaultTitlebarTypography.cssSizes,
      'Titlebar menu typography must remain in the renderer CSS coordinate system');
    for (let index = 0; index < enlargedTitlebarTypography.renderedSizes.length; index += 1) {
      assert(enlargedTitlebarTypography.renderedSizes[index] > defaultTitlebarTypography.renderedSizes[index] * 1.09,
        `Titlebar menu ${index} must grow with workbench zoom`);
    }
    assert(['1', 'normal'].includes(enlargedTitlebarTypography.cssZoom) && enlargedTitlebarTypography.transform === 'none',
      'Titlebar must not counter-scale the workbench zoom');
    await pressZoom('-');
    assertClose(await zoomFactor(), 1, 'Ctrl+- reduces the entire workbench', 0.01);
    const restoredTitlebarTypography = await titlebarTypography();
    assert.deepEqual(restoredTitlebarTypography.renderedSizes, defaultTitlebarTypography.renderedSizes,
      'Ctrl+- must return titlebar menus to the same visual size as the page');
    await pressZoom('=');
    assertClose(await zoomFactor(), 1.1, 'Ctrl+= is accepted as the unshifted zoom-in shortcut', 0.01);
    await app.close(); app = null;
    await launch();
    assertClose(await zoomFactor(), 1.1, 'Workbench zoom persists after a complete app restart', 0.01);
    await pressZoom('0');
    assertClose(await zoomFactor(), 1, 'Ctrl+0 restores the default workbench zoom', 0.01);
    report.checks.push('Ctrl++ / Ctrl+- zoom the custom titlebar menus together with the workbench; Ctrl+0 resets it and the chosen zoom survives restart');

    assert.deepEqual(report.errors, []);
    report.initial = initial;
    report.resized = resized;
    report.reset = reset;
    fs.writeFileSync(path.join(output, 'electron-layout-smoke.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report, null, 2));
  } catch (error) {
    report.failure = error.message;
    fs.writeFileSync(path.join(output, 'electron-layout-smoke.json'), JSON.stringify(report, null, 2));
    if (page && !page.isClosed()) await page.screenshot({ path: path.join(output, 'desktop-layout-smoke-failure.png'), fullPage: true }).catch(() => {});
    throw error;
  } finally {
    if (app) await app.close();
    fs.rmSync(temp, { recursive: true, force: true });
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
