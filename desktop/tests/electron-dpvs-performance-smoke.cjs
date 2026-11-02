'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');

async function settle(page) {
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await page.waitForTimeout(100);
}

async function pasteEvidence(page, role, text) {
  const panel = page.locator(`.dpvs-scenario-card[data-role="${role}"]`);
  await panel.getByRole('button', {name: '粘贴文本'}).click();
  await panel.locator('textarea').fill(text);
  await panel.getByRole('button', {name: '导入粘贴内容'}).click();
  await panel.locator('.dpvs-evidence-row').last().waitFor({state: 'visible'});
}

async function main() {
  const {_electron} = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-desktop-dpvs-'));
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
    await page.waitForFunction(() => document.getElementById('settings-drawer')?.hidden === true);
    await page.locator('[data-page="dpvs-performance"]').click();
    await page.waitForSelector('#dpvs-performance-page:not([hidden]) .dpvs-performance-page');

    await pasteEvidence(page, 'baseline', 'main;worker;tcp_send_csum 40\nmain;worker;business 60\n');
    await pasteEvidence(page, 'scaled', 'main;worker;tcp_send_csum 120\nmain;worker;business 80\n');
    await page.locator('.dpvs-scenario-card[data-role="baseline"] .dpvs-evidence-options input').first().fill('cycles');
    await page.locator('.dpvs-scenario-card[data-role="scaled"] .dpvs-evidence-options input').first().fill('cycles');
    await page.locator('.dpvs-scenario-card[data-role="baseline"] .dpvs-evidence-options input').nth(3).fill('all-CPU');
    await page.locator('.dpvs-scenario-card[data-role="scaled"] .dpvs-evidence-options input').nth(3).fill('all-CPU');
    await pasteEvidence(page, 'host', 'node0 node1\nnuma_hit 100000 100000\nnuma_miss 10 20\nlocal_node 99000 99000\nother_node 1000 1000\n');
    await settle(page);

    await page.getByRole('button', {name: /Perf 函数差异/}).click();
    await page.getByRole('row', {name: /tcp_send_csum/}).waitFor({state: 'visible'});
    const perfText = await page.locator('.dpvs-table.perf').textContent();
    assert.match(perfText, /tcp_send_csum/);
    assert.match(perfText, /20(?:\.0+)? pp/);

    await page.locator('[data-page="capture"]').click();
    await page.locator('[data-page="dpvs-performance"]').click();
    assert.equal(await page.locator('.dpvs-evidence-row').count(), 3, 'evidence survives page switches');
    assert(await page.locator('#agent-panel').isVisible(), 'Code Agent remains beside the analysis center');
    assert(await page.locator('#terminal-panel').isVisible(), 'SSH terminal remains below the analysis center');

    const geometry = await page.evaluate(() => ({
      documentWidth: document.documentElement.scrollWidth,
      viewportWidth: innerWidth,
      workspaceWidth: document.getElementById('workspace-content').clientWidth,
      workspaceScrollWidth: document.getElementById('workspace-content').scrollWidth,
    }));
    assert(geometry.documentWidth <= geometry.viewportWidth + 1, `frame overflow: ${JSON.stringify(geometry)}`);
    assert(geometry.workspaceScrollWidth <= geometry.workspaceWidth + 1, `workspace overflow: ${JSON.stringify(geometry)}`);
    assert.equal(errors.length, 0, `renderer errors: ${errors.join(' | ')}`);
    await page.screenshot({path: path.join(output, 'desktop-dpvs-performance.png'), fullPage: true});
    console.log(JSON.stringify({evidenceCount: 3, geometry, screenshot: 'test-results/desktop-dpvs-performance.png'}, null, 2));
  } finally {
    if (app) await app.close();
    fs.rmSync(temp, {recursive: true, force: true});
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
