const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const assert = require('node:assert/strict');

const desktopRoot = path.resolve(__dirname, '..');
const source = relative => fs.readFileSync(path.join(desktopRoot, relative), 'utf8');

test('desktop uses Window Controls Overlay and keeps the native menu bar hidden', () => {
  const main = source('main.cjs');
  assert.match(main, /titleBarStyle\s*:\s*['"]hidden['"]/,
    'BrowserWindow must render the workbench into the native titlebar');
  assert.match(main, /titleBarOverlay\s*:\s*\{/,
    'BrowserWindow must retain native window controls through titleBarOverlay');
  assert.match(main, /autoHideMenuBar\s*:\s*true/,
    'The duplicate native menu row must remain hidden');
  assert.doesNotMatch(main, /window(?:Minimize|ToggleMaximize|Close)/,
    'Window controls must remain native so Windows Snap and system behavior keep working');
});

test('renderer titlebar has the requested menu and icon order without a visible product title', () => {
  const html = source('renderer/index.html');
  const match = html.match(/<header id="desktop-titlebar"[\s\S]*?<\/header>/);
  assert(match, 'The renderer must contain one desktop titlebar');
  const titlebar = match[0];
  const orderedIds = [
    'titlebar-app-icon',
    'titlebar-edit-menu',
    'titlebar-view-menu',
    'titlebar-theme-menu',
    'titlebar-layout-menu',
    'titlebar-connection-menu',
  ];
  const positions = orderedIds.map(id => titlebar.indexOf(`id="${id}"`));
  assert(positions.every(position => position >= 0), `Missing titlebar item: ${JSON.stringify({ orderedIds, positions })}`);
  assert.deepEqual([...positions].sort((left, right) => left - right), positions,
    'App icon, Edit, View, Theme, Layout and Connection must share the requested order');
  assert(!titlebar.includes('异构优化工作台'));
  assert(!/v\d+\.\d+\.\d+/i.test(titlebar));
  assert(!/id="window-(?:minimize|maximize|close)"/.test(titlebar),
    'The renderer must not replace native window controls');
});

test('all five renderer menu triggers delegate to the native submenu IPC', () => {
  const html = source('renderer/index.html');
  const renderer = source('renderer/renderer.js');
  for (const menu of ['edit', 'view', 'theme', 'layout', 'connection']) {
    assert.match(html, new RegExp(`data-titlebar-menu="${menu}"`), `${menu} titlebar trigger`);
  }
  assert.match(renderer, /showTitlebarMenu/,
    'Renderer menu triggers must request native submenu popup through the preload bridge');
  assert.match(renderer, /addEventListener\('mouseenter'/,
    'Hovering a titlebar menu must open the same native submenu without an extra click');
  assert.match(renderer, /setTimeout\(\(\) => void openTitlebarMenu\(button\), 140\)/,
    'Hover opening uses a short dwell to avoid accidental menu flashes while crossing the titlebar');
});
