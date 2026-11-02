// Actual Electron IPC, CaptureManager, Python catalog/validation/capture/archive
// and Trace registry. Only HDC is replaced by a local executable fixture, so no
// physical device is touched. This POSIX fixture runs on macOS/Linux.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');

async function buttonVisual(page, selector) {
  return page.locator(selector).evaluate((button) => {
    const style = getComputedStyle(button);
    return {
      background: style.backgroundColor,
      color: style.color,
      border: style.borderColor,
      radius: style.borderRadius,
      shadow: style.boxShadow,
    };
  });
}

function relativeLuminance(cssColor) {
  const channels = String(cssColor).match(/[\d.]+/g)?.slice(0, 3).map(Number);
  assert.equal(channels?.length, 3, `Expected an RGB color, got ${cssColor}`);
  const [red, green, blue] = channels.map((value) => {
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

async function main() {
  if (process.platform === 'win32') { console.log(JSON.stringify({ skipped: true, reason: 'POSIX executable HDC fixture; Windows runtime tests run separately.' })); return; }
  const { _electron } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
  const output = path.resolve(__dirname, '../test-results');
  fs.mkdirSync(output, { recursive: true });
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-desktop-capture-ui-'));
  const callsPath = path.join(temp, 'hdc-calls.jsonl');
  const fakeHdc = path.join(temp, 'fixture-hdc');
  const python = execFileSync(process.env.HETERO_DESKTOP_PYTHON || 'python3', ['-c', 'import sys; print(sys.executable)'], { encoding: 'utf8' }).trim();
  fs.writeFileSync(fakeHdc, `#!${python}\nimport json,sys,time\nwith open(${JSON.stringify(callsPath)},'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\nif sys.argv[1:] == ['list','targets']:\n print('DESKTOP-QA-A\\nDESKTOP-QA-B')\n sys.exit(0)\ntime.sleep(1.5)\nprint('DESKTOP_QA_DEVICE_SHELL_FAILED: isolated fixture; no physical device',file=sys.stderr)\nsys.exit(3)\n`, { mode: 0o755 });
  const userData = path.join(temp, 'user-data');
  fs.mkdirSync(userData, { recursive: true });
  fs.writeFileSync(path.join(userData, 'settings.json'), `${JSON.stringify({ hdcPath: fakeHdc }, null, 2)}\n`);
  const report = { checks: [], errors: [] };
  let app;
  try {
    const env = { ...process.env, HETERO_DESKTOP_USER_DATA: userData, HETERO_DESKTOP_PYTHON: python };
    delete env.ELECTRON_RUN_AS_NODE;
    app = await _electron.launch({ executablePath: process.env.DESKTOP_ELECTRON_BIN || require('electron'), args: process.env.DESKTOP_PACKAGED ? [] : [path.resolve(__dirname, '..')], env, timeout: 60000 });
    const page = await app.firstWindow();
    page.on('pageerror', (error) => report.errors.push(error.message));
    await page.waitForSelector('#capture-steps [data-step-selected]', { state: 'attached', timeout: 60000 });
    await page.waitForSelector('#settings-drawer:not([hidden])');
    await page.locator('#close-settings').click();
    await page.waitForFunction(() => [...document.querySelector('#capture-device').options].some((option) => option.value === 'DESKTOP-QA-B'));
    fs.writeFileSync(callsPath, '');
    assert.equal(await page.locator('#capture-steps [data-step-selected]').count(), 17);
    assert.equal(await page.locator('#capture-period').inputValue(), '10');
    assert(await page.locator('#capture-page').isVisible());
    assert.equal(await page.locator('.capture-page-title').textContent(), '负载采样');
    assert((await page.locator('.capture-page-title').getAttribute('title')).includes('Trace'));
    assert.equal(await page.locator('#capture-scene-heading').textContent(), '2. 负载与模板');
    assert.equal(await page.locator('#capture-template-details').getAttribute('open'), null, 'Template details should start collapsed');
    assert(await page.locator('#capture-template').isVisible());
    assert(await page.locator('#capture-title').isVisible());
    assert.equal(await page.locator('#capture-app-bundle').isVisible(), false);
    assert.equal(await page.locator('.capture-step-panel').isVisible(), false);
    await page.locator('#capture-template-details > summary').focus();
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => document.querySelector('#capture-template-details').open);
    assert(await page.locator('#capture-app-bundle').isVisible());
    assert(await page.locator('.capture-step-panel').isVisible());
    const captureLayout = await page.evaluate(() => {
      const rectOf = (node) => { const value = node?.getBoundingClientRect(); return value && { x: value.x, y: value.y, width: value.width, height: value.height }; };
      const rect = (selector) => rectOf(document.querySelector(selector));
      return { stepScroll: rect('.capture-step-scroll'), stepPanel: rect('.capture-step-panel'), planSide: rect('.capture-plan-side'), agentAction: rect('#capture-agent-plan'), agentBox: rect('.capture-plan-action-box'), deviceSection: rectOf(document.querySelector('#capture-device-heading')?.closest('.capture-web-section')), sceneSection: rectOf(document.querySelector('#capture-scene-heading')?.closest('.capture-web-section')), configSection: rectOf(document.querySelector('#capture-collector-heading')?.closest('.capture-web-section')), modeRow: rect('.capture-mode-row'), pmuPanel: rect('.capture-pmu-panel'), pmuFields: [...document.querySelectorAll('.capture-pmu-grid > .capture-field')].map(rectOf), planControls: rect('.capture-plan-controls'), saveAction: rect('#capture-save'), savedPlan: rect('#capture-saved-plan'), captureAction: rect('#capture-start') };
    });
    assert(captureLayout.stepScroll.height <= 321, `Step list should be compact and scrollable: ${JSON.stringify(captureLayout.stepScroll)}`);
    if (Math.abs(captureLayout.stepPanel.y - captureLayout.planSide.y) < 2) assert(Math.abs(captureLayout.stepPanel.height - captureLayout.planSide.height) < 2, 'Step list and preparation column should align when displayed side by side');
    assert(captureLayout.deviceSection.y < captureLayout.sceneSection.y && captureLayout.sceneSection.y < captureLayout.configSection.y, 'Device connection should be the first numbered workflow step');
    assert(Math.abs(captureLayout.deviceSection.width - captureLayout.configSection.width) < 2, `Device and capture configuration sections should have equal width: ${JSON.stringify(captureLayout)}`);
    assert(captureLayout.pmuFields.length === 3 && captureLayout.pmuFields.every((field) => Math.abs(field.y - captureLayout.pmuFields[0].y) < 2), 'PMU mode, target interval and window should stay on one row');
    assert(captureLayout.modeRow.x < captureLayout.pmuPanel.x && Math.abs(captureLayout.modeRow.y + captureLayout.modeRow.height - captureLayout.pmuPanel.y - captureLayout.pmuPanel.height) < 2, 'Capture modes and PMU parameters should share one aligned configuration row');
    assert.equal(await page.locator('.capture-mode-row').evaluate((node) => node.closest('.capture-info-box') === null), true, 'Capture mode should be a flat row without a surrounding card');
    assert(captureLayout.agentAction.width >= captureLayout.agentBox.width - 30, 'Plan action should span the available step width');
    assert(Math.abs(captureLayout.saveAction.y - captureLayout.savedPlan.y) < 2 && Math.abs(captureLayout.savedPlan.y - captureLayout.captureAction.y) < 2, 'Save, saved-plan selection and execution should share one row');
    assert(captureLayout.saveAction.x < captureLayout.savedPlan.x && captureLayout.savedPlan.x < captureLayout.captureAction.x, 'Plan actions should follow save, select, execute order');
    const savedPlanToSaveRatio = captureLayout.savedPlan.width / captureLayout.saveAction.width;
    assert(savedPlanToSaveRatio > 2.8 && savedPlanToSaveRatio < 3.2, `Saved-plan selection should receive the space removed from the half-width Save action: ${savedPlanToSaveRatio}`);
    const flexibleShare = captureLayout.savedPlan.width / (captureLayout.savedPlan.width + captureLayout.captureAction.width);
    assert(flexibleShare > .40 && flexibleShare < .46, `Saved-plan selector should take about three sevenths of the flexible row width: ${flexibleShare}`);
    assert.equal(await page.locator('.capture-history-section').first().locator('.capture-web-heading').count(), 0, 'Saved plans should not have a separate heading row');
    assert.equal(await page.locator('#capture-saved-plan').getAttribute('aria-label'), '已保存计划');
    assert.equal(await page.locator('#capture-cancel').count(), 0, 'Cancel must replace the execution action instead of appearing beside Save');
    assert.equal(await page.locator('#capture-new-plan').count(), 0, 'A separate new-plan action should not be shown');
    assert.equal(await page.locator('#capture-plan-history').count(), 0, 'Saved plans should not be repeated as cards below the selector');
    assert((await page.locator('#capture-agent-plan').getAttribute('title')).includes('历史计划'), 'Plan action should explain that saved plans can be reused');
    assert(await page.locator('#capture-run').evaluate((node) => node.closest('.capture-history-section') !== null), 'Long-running progress should stay beside saved-plan execution');
    await page.locator('#capture-template-details > summary').click();
    assert.equal(await page.locator('#capture-template-details').getAttribute('open'), null);
    report.checks.push('Default capture page loads real Python Gallery 17-step catalog with requested 10 us PMU interval');
    report.checks.push('Template and plan name stay visible while application identity, steps and execution details start collapsed and remain keyboard accessible');
    report.checks.push('Device connection leads the workflow; flat capture modes and one-line PMU settings feed a compact save, plan selection and execution row');
    await page.locator('#capture-agent-plan').click();
    await page.waitForFunction(() => document.querySelector('#capture-notice').textContent.includes('当前计划已经完整'));
    assert.notEqual(await page.locator('#capture-saved-plan').inputValue(), '', 'The deterministic fast path should persist and select the executable plan');
    assert.equal(await page.locator('#capture-agent-plan').getAttribute('aria-busy'), 'false');
    report.checks.push('A complete template uses the local deterministic plan path without starting or waiting for Code Agent');
    await page.locator('#capture-template').selectOption('custom');
    assert.equal(await page.locator('#capture-template-details').getAttribute('open'), null, 'Changing templates must preserve the collapsed detail preference');
    await page.locator('#capture-template-details > summary').click();
    assert.equal(await page.locator('#capture-app-bundle').inputValue(), '');
    assert.equal(await page.locator('#capture-app-ability').inputValue(), '');
    assert.deepEqual(JSON.parse(await page.locator('#capture-profile').inputValue()).targets, {});
    assert.equal(await page.locator('#capture-steps [data-step-selected]').count(), 0);
    await page.locator('#capture-instructions').fill('1. 启动图库\n2） 打开一张图片');
    assert.equal(await page.locator('#capture-steps [data-step-selected]').count(), 2);
    assert.deepEqual(await page.locator('#capture-steps input[aria-label$="步名称"]').evaluateAll((inputs) => inputs.map((input) => input.value)), ['启动图库', '打开一张图片']);
    assert.equal(await page.locator('#capture-steps [data-step-selected]:checked').count(), 0);
    await page.locator('#capture-select-all').click();
    assert.equal(await page.locator('#capture-steps [data-step-selected]:checked').count(), 2);
    await page.locator('#capture-template-details > summary').click();
    assert.equal(await page.locator('#capture-template-details').getAttribute('open'), null);
    await page.locator('#capture-save').click();
    await page.waitForFunction(() => document.querySelector('#workbench-notification-root').textContent.includes('目标应用包名和启动 Ability'));
    assert.equal(await page.locator('#capture-template-details').getAttribute('open'), '', 'Validation should reveal hidden template controls');
    await page.waitForFunction(() => document.activeElement?.id === 'capture-app-bundle');
    const applicationIdentityNotification = page.locator('#workbench-notification-root .workbench-notification', {hasText: '目标应用包名和启动 Ability'});
    await applicationIdentityNotification.locator('.workbench-notification-content').click();
    await page.waitForFunction(() => document.querySelector('#workbench-notification-root .workbench-notification[data-copy-state="copied"]'));
    assert((await app.evaluate(({clipboard}) => clipboard.readText())).includes('目标应用包名和启动 Ability'), 'Clicking notification content should copy the complete error');
    await applicationIdentityNotification.locator('.workbench-notification-close').click();
    assert.equal(await applicationIdentityNotification.count(), 0, 'Viewport error should close without blocking the page');
    assert.equal(await page.locator('#capture-app-bundle').getAttribute('aria-invalid'), 'true');
    assert.equal(await page.locator('#capture-app-ability').getAttribute('aria-invalid'), 'true');
    await page.locator('#capture-app-bundle').fill('com.example.gallery');
    await page.locator('#capture-template-details > summary').click();
    await page.locator('#capture-save').click();
    await page.waitForFunction(() => document.querySelector('#workbench-notification-root').textContent.includes('请填写启动 Ability'));
    assert.equal(await page.locator('#capture-template-details').getAttribute('open'), '');
    await page.waitForFunction(() => document.activeElement?.id === 'capture-app-ability');
    assert.equal(await page.locator('#capture-app-bundle').getAttribute('aria-invalid'), 'false');
    assert.equal(await page.locator('#capture-app-ability').getAttribute('aria-invalid'), 'true');
    await page.locator('#workbench-notification-root .workbench-notification', {hasText: '请填写启动 Ability'}).locator('.workbench-notification-close').click();
    await page.locator('#capture-template').selectOption('gallery_17_steps_v1');
    assert.equal(await page.locator('#capture-steps [data-step-selected]').count(), 17);
    report.checks.push('Custom template clears application identity and targets, converts numbered lines into synchronized steps, and blocks save with field-specific application errors');
    await page.locator('[data-page="devices"]').click();
    await page.locator('#hdc-path').fill(fakeHdc);
    await page.locator('[data-page="capture"]').click();
    await page.waitForFunction(() => [...document.querySelector('#capture-device').options].some((option) => option.value === 'DESKTOP-QA-B'));
    await page.waitForTimeout(200);
    let discoveryCalls = fs.readFileSync(callsPath, 'utf8').trim().split('\n').map(JSON.parse).filter((args) => args.join(' ') === 'list targets');
    assert.equal(discoveryCalls.length, 1, 'Entering capture should issue one automatic HDC discovery');
    assert.equal(await page.locator('#capture-device').inputValue(), '');
    assert(await page.locator('#capture-start').isDisabled());
    await page.locator('#capture-device').selectOption('DESKTOP-QA-B');
    await page.waitForFunction(() => !document.querySelector('#capture-start').disabled);
    await page.locator('[data-page="devices"]').click();
    await page.locator('[data-page="capture"]').click();
    await page.waitForFunction(() => document.querySelector('#capture-device').value === 'DESKTOP-QA-B');
    await page.waitForTimeout(200);
    discoveryCalls = fs.readFileSync(callsPath, 'utf8').trim().split('\n').map(JSON.parse).filter((args) => args.join(' ') === 'list targets');
    assert.equal(discoveryCalls.length, 2, 'Returning to capture should refresh once without duplicate HDC discovery');
    assert.equal(await page.locator('#capture-device').inputValue(), 'DESKTOP-QA-B', 'Automatic refresh must retain the explicit device selection');
    await page.locator('#capture-clear-selection').click();
    assert(await page.locator('#capture-start').isDisabled());
    assert((await page.locator('#capture-readiness').textContent()).includes('至少勾选一个采集步骤'));
    await page.locator('[data-step-selected="step_07"]').check();
    for (const collector of ['trace', 'hilog', 'pmu']) await page.locator(`#capture-${collector}`).uncheck();
    assert(await page.locator('#capture-start').isDisabled());
    assert((await page.locator('#capture-readiness').textContent()).includes('至少选择一种采集数据'));
    await page.locator('#capture-trace').check();
    const firstOperation = page.locator('select[aria-label="第 1 步动作"]');
    const originalOperation = await firstOperation.inputValue();
    await firstOperation.selectOption('unresolved');
    assert(await page.locator('#capture-start').isDisabled());
    assert((await page.locator('#capture-readiness').textContent()).includes('执行范围内有 1 个步骤待解析'));
    await firstOperation.selectOption(originalOperation);
    await page.locator('.capture-toggle > summary').click();
    const profile = JSON.parse(await page.locator('#capture-profile').inputValue());
    const originalProfile = JSON.stringify(profile, null, 2);
    profile.targets.first_photo = null;
    await page.locator('#capture-profile').fill(JSON.stringify(profile, null, 2));
    assert(await page.locator('#capture-start').isDisabled());
    assert((await page.locator('#capture-readiness').textContent()).includes('控件定位待补充'));
    await page.locator('#capture-start').evaluate((button) => button.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    await page.waitForFunction(() => document.querySelector('#workbench-notification-root').textContent.includes('计划尚不可执行'), null, { timeout: 60000 });
    assert.equal((await page.evaluate(() => window.desktop.invoke('getState'))).capture.activeRun, null);
    await page.locator('#workbench-notification-root .workbench-notification', {hasText: '计划尚不可执行'}).locator('.workbench-notification-close').click();
    await page.locator('#capture-profile').fill(originalProfile);
    assert.equal(await page.locator('#capture-start').isDisabled(), false);
    assert((await page.locator('#capture-readiness').textContent()).includes('计划已就绪'));
    report.checks.push('Execution readiness disables capture for empty selection, empty collectors, unresolved execution steps and selector blockers, with actionable status text');
    await page.locator('#capture-title').fill('桌面端 D1 · 隔离失败采集验证');
    await page.locator('#capture-save').click();
    await page.waitForFunction(() => document.querySelector('#capture-notice').textContent.includes('已保存'), null, { timeout: 60000 });
    const planId = await page.locator('#capture-saved-plan').inputValue();
    assert(planId);
    const planCountAfterFirstSave = (await page.evaluate(() => window.desktop.invoke('getState'))).capture.plans.length;
    await page.locator('#capture-title').fill('桌面端 D1 · 隔离失败采集验证 · 新版本');
    await page.locator('#capture-save').click();
    await page.waitForFunction(() => !document.querySelector('#capture-save').disabled);
    const versionedPlanId = await page.locator('#capture-saved-plan').inputValue();
    assert(versionedPlanId && versionedPlanId !== planId, 'Editing a saved plan should create a reusable new plan instead of overwriting history');
    assert.equal((await page.evaluate(() => window.desktop.invoke('getState'))).capture.plans.length, planCountAfterFirstSave + 1);
    await page.locator('#capture-saved-plan').selectOption(planId);
    await page.waitForFunction((expected) => document.querySelector('#capture-title').value === expected, '桌面端 D1 · 隔离失败采集验证');
    report.checks.push('Capture entry automatically discovers local HDC devices once and later refreshes retain the explicit phone selection');
    report.checks.push('Actual local HDC discovery presents two devices without auto-switch; edited plans create history while a selected plan remains directly executable');
    assert.equal(await page.locator('#capture-agent-plan').evaluate((button) => button.classList.contains('capture-agent-button')), false,
      'Agent plan action must use the shared primary-button visual contract without a private color override');
    for (const theme of ['dark', 'light', 'hybrid']) {
      await page.evaluate((value) => { document.documentElement.dataset.theme = value; }, theme);
      await page.mouse.move(2, 2);
      await page.waitForTimeout(180);
      const agentDefault = await buttonVisual(page, '#capture-agent-plan');
      assert.deepEqual(agentDefault, await buttonVisual(page, '#capture-start'),
        `${theme}: Agent plan and capture execution actions must share their default primary-button visual`);
      assert(contrastRatio(agentDefault.color, agentDefault.background) >= 4.5,
        `${theme}: Agent plan default text must meet WCAG AA contrast`);
      await page.locator('#capture-agent-plan').hover();
      await page.waitForTimeout(180);
      const agentHover = await buttonVisual(page, '#capture-agent-plan');
      await page.locator('#capture-start').hover();
      await page.waitForTimeout(180);
      const captureHover = await buttonVisual(page, '#capture-start');
      assert.deepEqual(agentHover, captureHover,
        `${theme}: Agent plan hover must remain legible and match capture execution hover`);
      assert(contrastRatio(agentHover.color, agentHover.background) >= 4.5,
        `${theme}: Agent plan hover text must meet WCAG AA contrast`);
    }
    await page.evaluate(() => { document.documentElement.dataset.theme = 'dark'; });
    report.checks.push('Agent planning uses the same readable primary-button default and hover colors as capture execution in every theme');
    await page.screenshot({ path: path.join(output, 'desktop-capture-plan.png'), fullPage: true });
    const captureActionHandle = await page.locator('#capture-start').elementHandle();
    await page.locator('#capture-start').focus();
    assert.equal(await page.locator('#capture-start').evaluate((button) => button === document.activeElement), true);
    const planCountBeforeExecute = (await page.evaluate(() => window.desktop.invoke('getState'))).capture.plans.length;
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => document.querySelector('#capture-start').textContent.includes('取消采集'), null, { timeout: 60000 });
    assert.equal(await captureActionHandle.evaluate((button) => button === document.querySelector('#capture-start')), true, 'The execution button should switch in place instead of being replaced');
    assert.equal(await page.locator('#capture-start').isEnabled(), true, 'Active capture must keep the in-place cancel action available');
    await page.waitForFunction(() => document.querySelector('#capture-run').hidden === false, null, { timeout: 60000 });
    assert.equal((await page.evaluate(() => window.desktop.invoke('getState'))).capture.plans.length, planCountBeforeExecute, 'Direct execution of a selected saved plan should not save another copy');
    let state;
    const deadline = Date.now() + 120000;
    while (Date.now() < deadline) {
      state = await page.evaluate(() => window.desktop.invoke('getState'));
      if (!state.capture.activeRun && state.capture.runs[0]?.status === 'failed') break;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    const run = state.capture.runs[0];
    assert.equal(run.deviceSerial, 'DESKTOP-QA-B');
    assert.equal(run.status, 'failed');
    assert(run.traceId, `Failed capture did not register retained data: ${JSON.stringify({ status: run.status, message: run.message, registrationError: run.registrationError })}`);
    await page.waitForFunction(() => {
      const root = document.querySelector('#workbench-notification-root');
      return root?.textContent.includes('DESKTOP_QA_DEVICE_SHELL_FAILED') && root.querySelector('.workbench-notification-agent');
    });
    // This fixture fails during the device-session preflight, before a scenario
    // step starts.  The notification must preserve that real failure instead
    // of fabricating a step title, while still exposing Agent diagnosis.
    const failureNotification = page.locator('#workbench-notification-root .workbench-notification', {hasText: 'DESKTOP_QA_DEVICE_SHELL_FAILED'});
    assert.equal(await failureNotification.locator('.workbench-notification-agent').getAttribute('aria-label'), '发送错误给助手');
    const trace = await page.evaluate((id) => window.desktop.invoke('getTrace', { id }), run.traceId);
    assert(fs.existsSync(trace.runDirectory));
    assert(fs.existsSync(trace.archivePath));
    const calls = fs.readFileSync(callsPath, 'utf8').trim().split('\n').map(JSON.parse);
    assert(calls.some((args) => args.includes('shell')));
    for (const args of calls.filter((args) => args.includes('shell'))) assert.equal(args[args.indexOf('-t') + 1], 'DESKTOP-QA-B');
    report.checks.push('The execution action switches in place to Cancel while capture runs; capture remains bound to device B, preserves failed-run evidence and exposes one-click Agent diagnosis with failure context');
    await page.waitForFunction(() => document.querySelector('#capture-run-status').textContent.includes('失败'));
    assert((await page.locator('#capture-run').boundingBox()).height > 60, 'Run status collapsed in flex layout');
    await page.screenshot({ path: path.join(output, 'desktop-capture-failure.png'), fullPage: true });
    await page.locator('[data-page="traces"]').click();
    await page.locator('#trace-list .capture-result-row').first().click();
    await page.waitForSelector('#trace-remove');
    assert((await page.locator('#trace-detail').textContent()).includes('失败'));
    await page.screenshot({ path: path.join(output, 'desktop-capture-trace.png'), fullPage: true });
    await page.locator('#trace-remove').click();
    await page.waitForFunction(() => document.querySelector('#trace-notice').textContent.includes('已移除登记'));
    assert(fs.existsSync(trace.runDirectory));
    assert(fs.existsSync(trace.archivePath));
    assert.equal((await page.evaluate(() => window.desktop.invoke('listTraces'))).length, 0);
    report.checks.push('Trace detail displays retained failure; removing registry entry preserves original files');
    const registered = await page.evaluate((archive) => window.desktop.invoke('registerTrace', { path: archive }), trace.archivePath);
    await page.locator('#trace-refresh').click();
    await page.waitForSelector('#trace-list .capture-result-row');
    assert(registered.id);
    report.checks.push('Actual capture.zip can be registered back through the production IPC/import validator');
    await app.browserWindow(page).then((window) => window.evaluate((browserWindow) => browserWindow.setSize(1020, 720)));
    await page.locator('[data-page="capture"]').click();
    await page.waitForTimeout(250);
    const geometry = await page.evaluate(() => ({ viewport: innerWidth, body: document.body.scrollWidth, runHeight: document.getElementById('capture-run').getBoundingClientRect().height }));
    assert.equal(geometry.viewport, geometry.body);
    assert(geometry.runHeight > 60);
    await page.screenshot({ path: path.join(output, 'desktop-capture-compact.png'), fullPage: true });
    report.checks.push('1020 px compact layout has no horizontal overflow and keeps failure details visible');
    assert.deepEqual(report.errors, []);
    report.planId = planId; report.runStatus = run.status; report.shellCalls = calls.filter((args) => args.includes('shell')).length;
    fs.writeFileSync(path.join(output, 'electron-capture-smoke.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report, null, 2));
  } catch (error) {
    report.failure = error.message;
    fs.writeFileSync(path.join(output, 'electron-capture-smoke.json'), JSON.stringify(report, null, 2));
    if (app) { const pages = app.windows(); if (pages[0]) await pages[0].screenshot({ path: path.join(output, 'desktop-capture-smoke-failure.png') }).catch(() => {}); }
    throw error;
  } finally {
    if (app) await app.close();
    fs.rmSync(temp, { recursive: true, force: true });
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
