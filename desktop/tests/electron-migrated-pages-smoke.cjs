// Exercises every Web-to-Desktop migrated workspace in the real Electron
// renderer. It intentionally runs without SSH or Agent configuration so the
// pages must remain usable while their remote data source is unavailable.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');

const pages = [
  ['application-flow', 'application-flow-root'],
  ['opportunity-reasoning', 'opportunity-reasoning-root'],
  ['source-repositories', 'source-repositories-root'],
  ['code-knowledge', 'code-knowledge-root'],
  ['optimization-knowledge', 'optimization-knowledge-root'],
  ['skills', 'skills-root'],
  ['experience', 'experience-root'],
];

async function settle(page) {
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await page.waitForTimeout(100);
}

async function main() {
  const {_electron} = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-desktop-migrated-pages-'));
  const output = path.resolve(__dirname, '../test-results');
  fs.mkdirSync(output, {recursive: true});
  const env = {...process.env, HETERO_DESKTOP_USER_DATA: path.join(temp, 'user-data')};
  delete env.ELECTRON_RUN_AS_NODE;
  const errors = [];
  let app;
  try {
    app = await _electron.launch({
      executablePath: process.env.DESKTOP_ELECTRON_BIN || require('electron'),
      args: process.env.DESKTOP_PACKAGED ? [] : [path.resolve(__dirname, '..')],
      env,
      timeout: 60000,
    });
    const page = await app.firstWindow();
    page.on('pageerror', error => errors.push(error.message));
    await page.waitForSelector('.app-shell');
    await page.waitForFunction(() => Boolean(document.documentElement.dataset.platform));
    await page.waitForSelector('#settings-drawer:not([hidden])');
    await page.locator('#close-settings').click();
    await settle(page);

    const navOrder = await page.locator('.nav-items [data-page]').evaluateAll(items => items.map(item => item.dataset.page));
    assert.deepEqual(navOrder.slice(-3), ['skills', 'traces', 'experience']);
    const initialRootChildren = await page.evaluate(ids => Object.fromEntries(ids.map(id => [id, document.getElementById(id).childElementCount])), pages.map(item => item[1]));
    assert(Object.values(initialRootChildren).every(count => count === 0),
      `migrated pages must lazy-mount after selection: ${JSON.stringify(initialRootChildren)}`);

    async function visitMigratedPages(theme, {agentWidth = '33.333vw', layout = 'default'} = {}) {
      await page.evaluate(width => {
        document.documentElement.style.setProperty('--agent-width', width);
      }, agentWidth);
      await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
      await settle(page);
      for (const [pageId, rootId] of pages) {
        await page.locator(`[data-page="${pageId}"]`).click();
        await page.waitForFunction(({pageId, rootId}) => {
          const section = document.getElementById(`${pageId}-page`);
          const root = document.getElementById(rootId);
          return section && !section.hidden && root && root.childElementCount > 0;
        }, {pageId, rootId});
        const snapshot = await page.evaluate(({pageId, rootId}) => {
          const section = document.getElementById(`${pageId}-page`);
          const root = document.getElementById(rootId);
          const rect = section.getBoundingClientRect();
          return {
            active: document.querySelector('.nav-item.active')?.dataset.page,
            hidden: section.hidden,
            rootChildren: root.childElementCount,
            rect: {width: rect.width, height: rect.height},
            documentWidth: document.documentElement.scrollWidth,
            viewportWidth: innerWidth,
            workspaceWidth: document.getElementById('workspace-content')?.clientWidth ?? 0,
            workspaceScrollWidth: document.getElementById('workspace-content')?.scrollWidth ?? 0,
          };
        }, {pageId, rootId});
        const context = `${theme}/${layout}/${pageId}`;
        assert.equal(snapshot.active, pageId, `${context}: activity rail selection`);
        assert.equal(snapshot.hidden, false, `${context}: page visible`);
        assert(snapshot.rootChildren > 0, `${context}: React root mounted`);
        assert(snapshot.rect.width > 200 && snapshot.rect.height > 200, `${context}: usable page geometry`);
        assert(snapshot.documentWidth <= snapshot.viewportWidth + 1, `${context}: no frame-level horizontal overflow`);
        assert(snapshot.workspaceScrollWidth <= snapshot.workspaceWidth + 1, `${context}: no workspace-level horizontal overflow`);
      }
    }
    await visitMigratedPages('dark');
    await visitMigratedPages('light');
    await visitMigratedPages('dark', {agentWidth: '55vw', layout: 'narrow-workspace'});
    await visitMigratedPages('light', {agentWidth: '55vw', layout: 'narrow-workspace'});
    await page.evaluate(() => { document.documentElement.style.removeProperty('--agent-width'); });

    await page.locator('[data-page="source-repositories"]').click();
    await page.waitForSelector('#source-repositories-root .migrated-web-page');

    const autonomousStatus = page.evaluate(() => new Promise(resolve => {
      const timeout = setTimeout(() => resolve({status: 'timeout'}), 5000);
      window.addEventListener('page-agent-autonomous-task-status', event => {
        if (event.detail?.request_id !== 'desktop-smoke-autonomous') return;
        clearTimeout(timeout);
        resolve(event.detail);
      }, {once: false});
      window.dispatchEvent(new CustomEvent('page-agent-autonomous-task', {detail: {
        request_id: 'desktop-smoke-autonomous',
        required_skill_id: 'desktop-smoke-skill',
        message: 'DESKTOP_MIGRATED_PAGE_AGENT_BRIDGE',
      }}));
    }));
    const status = await autonomousStatus;
    assert.equal(status.status, 'rejected', 'unconfigured Agent rejects through the autonomous status channel');
    assert.notEqual(status.status, 'timeout', 'autonomous task bridge responds');

    if (await page.locator('#settings-drawer').isVisible()) {
      await page.locator('#close-settings').click();
      await settle(page);
    }

    await page.evaluate(() => { document.documentElement.dataset.theme = 'light'; });
    await settle(page);
    const light = await page.evaluate(() => {
      const migrated = document.querySelector('#source-repositories-root .migrated-web-page');
      return {
        pageBackground: getComputedStyle(migrated).backgroundColor,
        bodyBackground: getComputedStyle(document.body).backgroundColor,
      };
    });
    assert.match(light.pageBackground, /^rgb\((24[0-9]|25[0-5]),/, 'light migrated page keeps a light surface');

    await page.evaluate(() => { document.documentElement.dataset.theme = 'dark'; });
    await settle(page);
    const dark = await page.evaluate(() => {
      const migrated = document.querySelector('#source-repositories-root .migrated-web-page');
      return {
        pageBackground: getComputedStyle(migrated).backgroundColor,
        bodyBackground: getComputedStyle(document.body).backgroundColor,
      };
    });
    assert.equal(dark.pageBackground, 'rgb(24, 24, 24)', 'dark migrated page uses the Desktop dark surface');
    assert.equal(errors.length, 0, `renderer errors: ${errors.join(' | ')}`);
    await page.screenshot({path: path.join(output, 'electron-migrated-pages-smoke.png')});
    console.log(JSON.stringify({pages: pages.map(item => item[0]), autonomousStatus: status.status, themes: {light, dark}}, null, 2));
  } finally {
    if (app) await app.close();
    fs.rmSync(temp, {recursive: true, force: true});
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
