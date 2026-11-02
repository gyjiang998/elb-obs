const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const assert = require('node:assert/strict');

const desktopRoot = path.resolve(__dirname, '..');
const source = relative => fs.readFileSync(path.join(desktopRoot, relative), 'utf8');

test('activity rail exposes a non-layout hover flyout and configurable single-label fallback', () => {
  const html = source('renderer/index.html');
  const renderer = source('renderer/renderer.js');
  const controller = source('renderer/activity-rail.js');
  const theme = source('renderer/workbench-theme.css');
  const sidebar = html.match(/<aside id="sidebar-panel"[\s\S]*?<\/aside>/)?.[0] || '';

  assert.match(sidebar, /data-page="capture"[^>]+aria-label="负载采样"[^>]+title="负载采样"/);
  assert.match(sidebar, /<span class="nav-text">负载采样<\/span>/);
  assert.match(sidebar, /<div class="sidebar-bottom">[\s\S]*id="connection-settings-nav"[\s\S]*<span class="nav-text">连接配置<\/span>/);
  assert.match(sidebar, /class="activity-rail-flyout-surface"/);
  assert.match(html, /id="tab-interface"[\s\S]*id="activity-rail-expand-on-hover"/);
  assert.match(renderer, /\$\('connection-settings-nav'\)\.addEventListener\('click', \(\) => openSettings\('agent'\)\)/);
  assert.match(renderer, /createActivityRail\([\s\S]*sidebar: \$\('sidebar-panel'\)[\s\S]*enabled: activityRailPreference\.expandOnHover/);
  assert.match(renderer, /writeActivityRailPreferences\(localStorage/);
  assert.match(controller, /listen\(sidebar, 'pointerenter', scheduleOpen\)/);
  assert.match(controller, /listen\(sidebar, 'pointerleave'/);
  assert.match(controller, /closeUntilPointerLeaves/);
  assert.match(controller, /if \(event\.detail === 0\)[\s\S]*suppressedUntilPointerLeave = false/);
  assert.match(controller, /sidebar\.dataset\.labelMode = expandOnHover \? 'all' : 'single'/);
  assert.match(theme, /\.nav-item:not\(\.nav-tooltip-suppressed\):hover \.nav-text/);
  assert.match(theme, /\.sidebar\[data-label-mode="all"\]\[data-flyout-open="true"\] \.nav-item/);
  assert.match(theme, /--activity-rail-expanded-width: 164px/);
  assert.match(theme, /\.sidebar\[data-label-mode="all"\]\[data-flyout-open="true"\] \.nav-item \{[\s\S]*?gap: 20px/);
  assert.match(theme, /\.nav-items \{[\s\S]*overflow-y: auto/);
  assert.match(theme, /\.sidebar \{[\s\S]*z-index: 12/);
  assert.match(renderer, /button\.setAttribute\('aria-current', 'page'\)/);
  assert.doesNotMatch(sidebar, /class="local-label"[^>]+title=/);
  assert.doesNotMatch(html, /无需\s*Device Bridge/i);
});
