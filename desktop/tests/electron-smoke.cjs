// Runs the real Electron window, local PTY and SSH protocol against an isolated
// server. No user server, model credentials or device mutations are involved.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const { once } = require('node:events');
const { spawn } = require('node:child_process');
const { Server, utils: { sftp: { OPEN_MODE, STATUS_CODE } } } = require('ssh2');
const pty = require('node-pty');

async function clickNativeMenu(app, id) {
  await app.evaluate(({ BrowserWindow, Menu }, itemId) => {
    const item = Menu.getApplicationMenu()?.getMenuItemById(itemId);
    if (!item?.click) throw new Error(`Native menu item is not actionable: ${itemId}`);
    item.click(item, BrowserWindow.getAllWindows()[0], {});
  }, id);
}

async function disconnectSsh(page) {
  await page.evaluate(() => window.desktop.invoke('disconnectSsh'));
  await page.waitForFunction(() => document.getElementById('ssh-status').textContent === '未连接');
}

async function connectSshByCreatingTerminal(page) {
  await page.locator('#new-ssh-terminal').click();
  await page.waitForFunction(() => document.getElementById('ssh-status').textContent === '已连接');
}

async function waitForRemoteEditor(page) {
  await page.waitForFunction(() => {
    const host = document.getElementById('remote-file-editor');
    return Boolean(host?.__remoteEditor?.isMonaco && host.__remoteEditor.getActiveModelId());
  }, null, {timeout: 15000});
  await page.locator('#remote-file-editor .monaco-editor').waitFor({state: 'visible', timeout: 15000});
}

async function remoteEditorValue(page) {
  return page.evaluate(() => document.getElementById('remote-file-editor').__remoteEditor.getValue());
}

async function setRemoteEditorValue(page, value) {
  await page.evaluate((nextValue) => {
    const editor = document.getElementById('remote-file-editor')?.__remoteEditor;
    if (!editor) throw new Error('Monaco remote editor test contract is unavailable');
    editor.setValue(nextValue);
    editor.focus();
  }, value);
}

async function focusRemoteEditor(page) {
  await page.evaluate(() => {
    const editor = document.getElementById('remote-file-editor')?.__remoteEditor;
    if (!editor) throw new Error('Monaco remote editor test contract is unavailable');
    editor.focus();
  });
}

async function saveRemoteEditor(page) {
  await focusRemoteEditor(page);
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+S' : 'Control+S');
}

async function waitUntil(predicate, {timeoutMs = 15000, intervalMs = 25} = {}) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, intervalMs));
  }
  throw new Error('Timed out waiting for fixture evidence');
}

const WORKSPACE_REQUEST_HEADER = 'HETERO_WORKSPACE_RPC_REQUEST\t';

function observeWorkspaceRpc(command, connectionId, evidence) {
  const sourceLength = command.match(/\s(\d+)\s+--stdio-server(?:\s|$)/)?.[1];
  if (!sourceLength) return null;
  const record = {connectionId, command, requests: [], protocolErrors: []};
  evidence.sessions.push(record);
  let bootstrapBytes = Number(sourceLength);
  let buffer = Buffer.alloc(0);
  let expectedBodyBytes = null;
  return (chunk) => {
    let incoming = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    if (bootstrapBytes > 0) {
      const consumed = Math.min(bootstrapBytes, incoming.length);
      bootstrapBytes -= consumed;
      incoming = incoming.subarray(consumed);
    }
    if (!incoming.length) return;
    buffer = buffer.length ? Buffer.concat([buffer, incoming]) : Buffer.from(incoming);
    while (buffer.length) {
      if (expectedBodyBytes !== null) {
        if (buffer.length < expectedBodyBytes) return;
        const body = buffer.subarray(0, expectedBodyBytes);
        buffer = buffer.subarray(expectedBodyBytes);
        expectedBodyBytes = null;
        try {
          const request = JSON.parse(body.toString('utf8'));
          record.requests.push({method: request.method, params: request.params || {}});
        } catch (error) { record.protocolErrors.push(`invalid JSON: ${error.message}`); }
        continue;
      }
      const newline = buffer.indexOf(0x0a);
      if (newline < 0) return;
      const header = buffer.subarray(0, newline).toString('utf8').replace(/\r$/, '');
      buffer = buffer.subarray(newline + 1);
      if (!header.startsWith(WORKSPACE_REQUEST_HEADER)) {
        record.protocolErrors.push(`unexpected header: ${header}`);
        continue;
      }
      const length = header.slice(WORKSPACE_REQUEST_HEADER.length);
      if (!/^(?:0|[1-9][0-9]*)$/.test(length)) {
        record.protocolErrors.push(`invalid frame length: ${length}`);
        continue;
      }
      expectedBodyBytes = Number(length);
    }
  };
}

function workspaceRequests(evidence, {connectionId, method, path: requestPath} = {}) {
  return evidence.sessions
    .filter(session => connectionId === undefined || session.connectionId === connectionId)
    .flatMap(session => session.requests)
    .filter(request => (!method || request.method === method)
      && (requestPath === undefined || request.params?.path === requestPath));
}

function sftpAttrs(value) {
  return {
    mode: value.mode,
    uid: value.uid,
    gid: value.gid,
    size: value.size,
    atime: Math.floor(value.atimeMs / 1000),
    mtime: Math.floor(value.mtimeMs / 1000),
  };
}

function installSftpFixture(session, workspace, evidence = {}) {
  session.on('sftp', (accept) => {
    const sftp = accept();
    const handles = new Map();
    let nextHandle = 1;

    const statusFor = (error) => {
      if (error?.code === 'ENOENT' || error?.code === 'ENOTDIR') return STATUS_CODE.NO_SUCH_FILE;
      if (error?.code === 'EACCES' || error?.code === 'EPERM') return STATUS_CODE.PERMISSION_DENIED;
      return STATUS_CODE.FAILURE;
    };
    const replyError = (requestId, error) => sftp.status(requestId, statusFor(error), String(error?.message || error));
    const resolvePath = (value) => {
      const resolved = path.resolve(String(value));
      const relative = path.relative(workspace, resolved);
      if (relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
        const error = new Error('Fixture SFTP access is outside the isolated workspace');
        error.code = 'EACCES';
        throw error;
      }
      return resolved;
    };
    const allocateHandle = (state) => {
      const id = nextHandle++;
      const handle = Buffer.alloc(4);
      handle.writeUInt32BE(id, 0);
      handles.set(id, state);
      return handle;
    };
    const findHandle = (requestId, handle, expectedKind) => {
      if (!Buffer.isBuffer(handle) || handle.length !== 4) {
        sftp.status(requestId, STATUS_CODE.FAILURE, 'Invalid fixture handle');
        return null;
      }
      const state = handles.get(handle.readUInt32BE(0));
      if (!state || (expectedKind && state.kind !== expectedKind)) {
        sftp.status(requestId, STATUS_CODE.FAILURE, 'Unknown fixture handle');
        return null;
      }
      return state;
    };
    const onStat = (followLinks) => (requestId, filename) => {
      try {
        const filenameOnDisk = resolvePath(filename);
        const value = followLinks ? fs.statSync(filenameOnDisk) : fs.lstatSync(filenameOnDisk);
        sftp.attrs(requestId, sftpAttrs(value));
      } catch (error) { replyError(requestId, error); }
    };

    sftp.on('REALPATH', (requestId, filename) => {
      try {
        const canonical = fs.realpathSync(resolvePath(filename));
        sftp.name(requestId, [{ filename: canonical, longname: canonical, attrs: {} }]);
      } catch (error) { replyError(requestId, error); }
    });
    sftp.on('STAT', onStat(true));
    sftp.on('LSTAT', onStat(false));
    sftp.on('OPENDIR', (requestId, dirname) => {
      try {
        const dirnameOnDisk = resolvePath(dirname);
        evidence.openDirectories?.push(dirnameOnDisk);
        if (evidence.failDirectories?.has(dirnameOnDisk)) {
          const error = new Error('Fixture directory is temporarily unavailable');
          error.code = 'EACCES';
          throw error;
        }
        const entries = fs.readdirSync(dirnameOnDisk, { withFileTypes: true }).map((entry) => {
          const value = fs.lstatSync(path.join(dirnameOnDisk, entry.name));
          return { filename: entry.name, longname: entry.name, attrs: sftpAttrs(value) };
        });
        const delays = evidence.directoryDelays?.get(dirnameOnDisk);
        const delayMs = Array.isArray(delays) && delays.length ? Number(delays.shift()) || 0 : 0;
        const respond = () => sftp.handle(requestId, allocateHandle({ kind: 'directory', entries, sent: false }));
        if (delayMs > 0) setTimeout(respond, delayMs); else respond();
      } catch (error) { replyError(requestId, error); }
    });
    sftp.on('READDIR', (requestId, handle) => {
      const state = findHandle(requestId, handle, 'directory');
      if (!state) return;
      if (state.sent || state.entries.length === 0) return sftp.status(requestId, STATUS_CODE.EOF);
      state.sent = true;
      sftp.name(requestId, state.entries);
    });
    sftp.on('OPEN', (requestId, filename, flags, attrs) => {
      try {
        const filenameOnDisk = resolvePath(filename);
        let openFlags = 'r';
        if (flags & OPEN_MODE.WRITE) {
          if (flags & OPEN_MODE.APPEND) openFlags = flags & OPEN_MODE.READ ? 'a+' : 'a';
          else if (flags & OPEN_MODE.EXCL) openFlags = flags & OPEN_MODE.READ ? 'wx+' : 'wx';
          else if (flags & OPEN_MODE.TRUNC) openFlags = flags & OPEN_MODE.READ ? 'w+' : 'w';
          else openFlags = flags & OPEN_MODE.READ ? 'r+' : (flags & OPEN_MODE.CREAT ? 'w' : 'r+');
        }
        const fd = fs.openSync(filenameOnDisk, openFlags, attrs?.mode || 0o644);
        sftp.handle(requestId, allocateHandle({ kind: 'file', fd }));
      } catch (error) { replyError(requestId, error); }
    });
    sftp.on('FSTAT', (requestId, handle) => {
      const state = findHandle(requestId, handle, 'file');
      if (!state) return;
      try { sftp.attrs(requestId, sftpAttrs(fs.fstatSync(state.fd))); }
      catch (error) { replyError(requestId, error); }
    });
    sftp.on('READ', (requestId, handle, offset, length) => {
      const state = findHandle(requestId, handle, 'file');
      if (!state) return;
      try {
        const buffer = Buffer.alloc(length);
        const bytesRead = fs.readSync(state.fd, buffer, 0, length, Number(offset));
        if (!bytesRead) sftp.status(requestId, STATUS_CODE.EOF);
        else sftp.data(requestId, buffer.subarray(0, bytesRead));
      } catch (error) { replyError(requestId, error); }
    });
    sftp.on('WRITE', (requestId, handle, offset, data) => {
      const state = findHandle(requestId, handle, 'file');
      if (!state) return;
      try {
        fs.writeSync(state.fd, data, 0, data.length, Number(offset));
        sftp.status(requestId, STATUS_CODE.OK);
      } catch (error) { replyError(requestId, error); }
    });
    sftp.on('RENAME', (requestId, oldPath, newPath) => {
      // Leave a small observable save window so the renderer smoke test can
      // prove keystrokes entered during an SFTP save stay dirty.
      setTimeout(() => {
        try {
          fs.renameSync(resolvePath(oldPath), resolvePath(newPath));
          sftp.status(requestId, STATUS_CODE.OK);
        } catch (error) { replyError(requestId, error); }
      }, 100);
    });
    sftp.on('REMOVE', (requestId, filename) => {
      try {
        fs.unlinkSync(resolvePath(filename));
        sftp.status(requestId, STATUS_CODE.OK);
      } catch (error) { replyError(requestId, error); }
    });
    sftp.on('CLOSE', (requestId, handle) => {
      const state = findHandle(requestId, handle);
      if (!state) return;
      // A real remote server may acknowledge file-handle closure later than
      // the readable stream's end event. Keep this delay to guard against
      // closing the entire SFTP channel while CLOSE is still in flight.
      setTimeout(() => {
        try {
          handles.delete(handle.readUInt32BE(0));
          if (state.kind === 'file') fs.closeSync(state.fd);
          sftp.status(requestId, STATUS_CODE.OK);
        } catch (error) { replyError(requestId, error); }
      }, 40);
    });
    sftp.on('end', () => {
      for (const state of handles.values()) if (state.kind === 'file') try { fs.closeSync(state.fd); } catch {}
      handles.clear();
    });
  });
}

async function main() {
  if (process.platform === 'win32') throw new Error('This isolated SSH shell fixture requires POSIX; Windows native PTY tests run under npm test.');
  const { _electron } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
  const output = path.resolve(__dirname, '../test-results');
  fs.mkdirSync(output, { recursive: true });
  // macOS exposes /var through /private/var. Use the canonical fixture root so
  // the sandboxed SFTP path check and `pwd -P` agree on the same directory.
  const temp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-desktop-electron-')));
  const workspace = path.join(temp, 'remote workspace'); fs.mkdirSync(workspace);
  const alternateWorkspace = path.join(workspace, 'alternate'); fs.mkdirSync(alternateWorkspace);
  fs.writeFileSync(path.join(alternateWorkspace, 'nested.txt'), 'NESTED_TREE_ITEM\n');
  const removedWorkspace = path.join(alternateWorkspace, 'removed-during-prefetch');
  fs.mkdirSync(removedWorkspace);
  fs.writeFileSync(path.join(removedWorkspace, 'stale-prefetch-result.txt'), 'MUST_NOT_REAPPEAR\n');
  fs.writeFileSync(path.join(workspace, 'server-evidence.txt'), Buffer.concat([
    Buffer.from([0xef, 0xbb, 0xbf]),
    Buffer.from('SSH_REMOTE_EVIDENCE\r\n'),
  ]));
  const hostKey = crypto.generateKeyPairSync('rsa', { modulusLength: 2048 }).privateKey.export({ type: 'pkcs1', format: 'pem' });
  const clients = new Set(), processes = new Set();
  const commands = [];
  const sftpEvidence = {openDirectories: [], failDirectories: new Set(), directoryDelays: new Map()};
  const workspaceRpcEvidence = {sessions: []};
  const smokeStartedAt = Date.now();
  let smokeStage = 'fixture startup';
  const checkpoint = (value) => {
    smokeStage = value;
    console.error(`[electron-smoke +${Date.now() - smokeStartedAt}ms] ${value}`);
  };
  let connections = 0, app, watchdog;
  const server = new Server({ hostKeys: [hostKey] }, (client) => {
    connections++; const connectionId = connections; clients.add(client);
    client.on('error', () => {});
    client.on('close', () => clients.delete(client));
    client.on('authentication', (context) => {
      if (context.method === 'password' && context.username === 'desktop-test' && context.password === 'isolated-test') context.accept();
      else context.reject(['password']);
    });
    client.on('ready', () => client.on('session', (accept) => {
      const session = accept();
      installSftpFixture(session, workspace, sftpEvidence);
      session.on('pty', (accept) => accept());
      session.on('window-change', (accept) => accept?.());
      session.on('shell', (accept) => {
        const channel = accept();
        const terminal = pty.spawn('/bin/bash', ['--noprofile', '--norc'], { name: 'xterm-256color', cwd: workspace, cols: 100, rows: 28, env: { ...process.env, PS1: 'remote-test$ ' } });
        processes.add(terminal);
        terminal.onData((data) => { if (!channel.destroyed) channel.write(data); });
        terminal.onExit(({ exitCode }) => { processes.delete(terminal); if (!channel.destroyed) { channel.exit(exitCode); channel.end(); } });
        channel.on('data', (data) => terminal.write(data.toString()));
        channel.on('close', () => terminal.kill()); channel.on('error', () => terminal.kill());
      });
      session.on('exec', (accept, _reject, info) => {
        commands.push(info.command);
        const observeRpcData = observeWorkspaceRpc(info.command, connectionId, workspaceRpcEvidence);
        const channel = accept();
        const child = spawn('/bin/sh', ['-c', info.command], { stdio: ['pipe', 'pipe', 'pipe'] });
        processes.add(child);
        child.stdin.on('error', () => {});
        channel.on('data', data => {
          observeRpcData?.(data);
          if (!child.stdin.destroyed && !child.stdin.writableEnded) try { child.stdin.write(data); } catch {}
        });
        channel.on('end', () => { if (!child.stdin.destroyed && !child.stdin.writableEnded) child.stdin.end(); });
        child.stdout.on('data', (data) => { if (!channel.destroyed) channel.write(data); });
        child.stderr.on('data', (data) => { if (!channel.destroyed) channel.stderr.write(data); });
        child.on('close', (code) => { processes.delete(child); if (!channel.destroyed) { channel.exit(code ?? 1); channel.end(); } });
        channel.on('close', () => child.kill()); channel.on('error', () => child.kill());
      });
    }));
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  watchdog = setTimeout(() => {
    console.error(`[electron-smoke watchdog] exceeded 170000ms; last stage: ${smokeStage}`);
    for (const child of processes) try { child.kill('SIGKILL'); } catch {}
    for (const client of clients) try { client.destroy(); } catch {}
    try { app?.process()?.kill('SIGKILL'); } catch {}
    try { server.close(); } catch {}
    try { fs.rmSync(temp, {recursive: true, force: true}); } catch {}
    process.exit(124);
  }, 170000);
  const errors = [];
  const mainProcessStderr = [];
  const report = { checks: [], commands, errors };
  try {
    const userData = path.join(temp, 'user-data');
    const env = { ...process.env, HETERO_DESKTOP_USER_DATA: userData };
    delete env.ELECTRON_RUN_AS_NODE;
    app = await _electron.launch({ executablePath: process.env.DESKTOP_ELECTRON_BIN || require('electron'), args: process.env.DESKTOP_PACKAGED ? [] : [path.resolve(__dirname, '..')], env, timeout: 60000 });
    checkpoint('first Electron window launched');
    app.process().stderr?.on('data', data => mainProcessStderr.push(data.toString('utf8')));
    const page = await app.firstWindow();
    page.on('pageerror', (error) => errors.push(error.message));
    await page.waitForFunction(() => document.getElementById('platform-label').textContent.includes('macOS'));
    await page.waitForSelector('#settings-drawer:not([hidden])');
    assert.equal(await page.locator('#tab-ssh').getAttribute('aria-selected'), 'true');
    report.checks.push('Missing startup configuration opens the corresponding connection settings tab');
    await page.locator('#ssh-host').fill('127.0.0.1');
    await page.locator('#ssh-port').fill(String(server.address().port));
    await page.locator('#ssh-username').fill('desktop-test');
    await page.locator('#ssh-cwd').fill(workspace);
    await page.locator('#ssh-password').fill('isolated-test');
    await page.locator('#save-settings').click();
    await page.waitForFunction(() => document.getElementById('settings-save-status').textContent.includes('已保存'));
    assert.equal(await page.locator('#ssh-password').inputValue(), '');
    assert.match(await page.locator('#ssh-password-status').textContent(), /已为当前服务器和用户名安全保存/);
    assert.ok(!fs.readFileSync(path.join(userData, 'settings.json'), 'utf8').includes('isolated-test'));
    assert.ok(!fs.readFileSync(path.join(userData, 'ssh-credentials.bin')).includes(Buffer.from('isolated-test')));
    report.checks.push('SSH password is encrypted by OS safe storage, omitted from settings/renderer, and reusable when input stays blank');
    assert.equal(await page.locator('#forget-ssh-password').isEnabled(), true);
    await page.locator('#forget-ssh-password').click();
    await page.waitForFunction(() => document.getElementById('settings-save-status').textContent.includes('已删除当前服务器'));
    assert.equal(await page.locator('#forget-ssh-password').isDisabled(), true);
    assert.doesNotMatch(await page.locator('#ssh-password-status').textContent(), /已为当前服务器和用户名安全保存/);
    await page.locator('#ssh-password').fill('isolated-test');
    await page.locator('#save-settings').click();
    await page.waitForFunction(() => document.getElementById('settings-save-status').textContent.includes('已保存'));
    assert.equal(await page.locator('#ssh-password').inputValue(), '');
    report.checks.push('Settings can forget only the current SSH target password and accept a fresh replacement without exposing it over IPC');
    await page.locator('#close-settings').click();
    await page.locator('[data-page="workbench"]').click();
    assert.equal(await page.locator('#connect-ssh').count(), 0);
    assert.equal(await page.locator('#start-agent').count(), 0);
    assert.equal(await page.locator('#stop-agent').count(), 0);
    await connectSshByCreatingTerminal(page);
    checkpoint('initial SSH connection established');
    report.checks.push('Creating a terminal automatically establishes authenticated SSH without a panel reconnect control');
    const evidenceFile = page.locator('#remote-file-tree .remote-tree-row', { hasText: 'server-evidence.txt' });
    await evidenceFile.waitFor({ timeout: 15000 });
    await waitUntil(() => workspaceRequests(workspaceRpcEvidence,
      {connectionId: 1, method: 'fs.list', path: ''}).length > 0);
    await waitUntil(() => workspaceRequests(workspaceRpcEvidence,
      {connectionId: 1, method: 'fs.prefetch'}).some(request => request.params.paths?.includes('alternate')));
    assert(workspaceRpcEvidence.sessions.some(session => session.connectionId === 1
      && session.command.includes('--stdio-server') && session.command.includes('--workspace-root')),
    'normal Explorer traffic must bootstrap the portless Workspace Worker through SSH exec');
    assert.equal(sftpEvidence.openDirectories.length, 0,
      'normal Explorer list and prefetch must stay on Workspace RPC instead of opening SFTP directories');
    const firstConnectedAt = await page.evaluate(async () => (await window.desktop.invoke('getState')).ssh.connectedAt);
    await disconnectSsh(page);
    await connectSshByCreatingTerminal(page);
    const reconnectedAt = await page.evaluate(async () => (await window.desktop.invoke('getState')).ssh.connectedAt);
    assert.notEqual(reconnectedAt, firstConnectedAt);
    await evidenceFile.waitFor({ timeout: 15000 });
    await waitUntil(() => workspaceRequests(workspaceRpcEvidence,
      {connectionId: 2, method: 'fs.list', path: ''}).length > 0);
    await waitUntil(() => workspaceRequests(workspaceRpcEvidence,
      {connectionId: 2, method: 'fs.prefetch'}).some(request => request.params.paths?.includes('alternate')));
    const rootListsBeforeRefresh = workspaceRequests(workspaceRpcEvidence,
      {connectionId: 2, method: 'fs.list', path: ''}).length;
    await page.locator('#remote-files-refresh').click();
    await waitUntil(() => workspaceRequests(workspaceRpcEvidence,
      {connectionId: 2, method: 'fs.list', path: ''}).length > rootListsBeforeRefresh);
    await page.waitForFunction(() => !document.getElementById('remote-files-refresh').disabled, null, {timeout: 10000});
    const sharedConnectionCount = connections;
    assert.equal(sharedConnectionCount, 2, `new-terminal reconnect must establish exactly one replacement connection; got ${connections}`);

    assert.equal(sftpEvidence.openDirectories.length, 0);
    assert.deepEqual(workspaceRpcEvidence.sessions.flatMap(session => session.protocolErrors), []);
    report.checks.push('SSH reconnect bootstraps fresh Workspace Worker lanes; list, prefetch and manual refresh remain on framed RPC without SFTP fallback');
    checkpoint('Workspace Worker list, prefetch, refresh and reconnect verified');
    await evidenceFile.waitFor({ timeout: 15000 });
    const folder = page.locator('#remote-file-tree .remote-tree-row', { hasText: 'alternate' });
    await folder.focus();
    assert.equal(await folder.getAttribute('tabindex'), '0');
    assert.equal(await folder.getAttribute('aria-level'), '1');
    assert.equal(await page.locator('#remote-file-tree .remote-tree-row[tabindex="0"]').count(), 1);
    const collapsedChevron = await folder.locator('.remote-tree-chevron').evaluate((element) => {
      const rowRect = element.closest('.remote-tree-row').getBoundingClientRect();
      const rect = element.getBoundingClientRect();
      const marker = getComputedStyle(element, '::before');
      return {
        text: element.textContent,
        expanded: element.classList.contains('expanded'),
        width: rect.width,
        height: rect.height,
        rowCenter: rowRect.top + rowRect.height / 2,
        center: rect.top + rect.height / 2,
        markerTransform: marker.transform,
        markerBorderRight: marker.borderRightWidth,
        markerBorderBottom: marker.borderBottomWidth,
      };
    });
    assert.equal(collapsedChevron.text, '', 'tree chevrons must not depend on font-glyph baselines');
    assert.equal(collapsedChevron.expanded, false);
    assert.equal(collapsedChevron.width, 14);
    assert.equal(collapsedChevron.height, 18);
    assert(Math.abs(collapsedChevron.rowCenter - collapsedChevron.center) <= 0.5,
      `collapsed chevron must be vertically centered in its row: ${JSON.stringify(collapsedChevron)}`);
    assert.notEqual(collapsedChevron.markerTransform, 'none');
    assert.notEqual(collapsedChevron.markerBorderRight, '0px');
    assert.notEqual(collapsedChevron.markerBorderBottom, '0px');
    const alternateListsBeforeExpand = workspaceRequests(workspaceRpcEvidence,
      {connectionId: 2, method: 'fs.list', path: 'alternate'}).length;
    await folder.press('ArrowRight');
    const nestedFile = page.locator('#remote-file-tree .remote-tree-row', { hasText: 'nested.txt' });
    await nestedFile.waitFor();
    assert.equal(await folder.getAttribute('aria-expanded'), 'true');
    const expandedChevron = await folder.locator('.remote-tree-chevron').evaluate((element) => {
      const rowRect = element.closest('.remote-tree-row').getBoundingClientRect();
      const rect = element.getBoundingClientRect();
      return {
        text: element.textContent,
        expanded: element.classList.contains('expanded'),
        rowCenter: rowRect.top + rowRect.height / 2,
        center: rect.top + rect.height / 2,
        markerTransform: getComputedStyle(element, '::before').transform,
      };
    });
    assert.equal(expandedChevron.text, '');
    assert.equal(expandedChevron.expanded, true);
    assert(Math.abs(expandedChevron.rowCenter - expandedChevron.center) <= 0.5,
      `expanded chevron must be vertically centered in its row: ${JSON.stringify(expandedChevron)}`);
    assert.notEqual(expandedChevron.markerTransform, collapsedChevron.markerTransform,
      'expanded and collapsed folders must use distinct CSS chevron directions');
    await new Promise(resolve => setTimeout(resolve, 100));
    assert.equal(workspaceRequests(workspaceRpcEvidence,
      {connectionId: 2, method: 'fs.list', path: 'alternate'}).length,
      alternateListsBeforeExpand, 'expanding a prefetched directory must not issue another remote list');
    await folder.press('ArrowRight');
    const firstNestedRow = page.locator('#remote-file-tree .remote-tree-row[data-parent-path="alternate"]').first();
    assert.equal(await firstNestedRow.evaluate((row) => row === document.activeElement), true);
    assert.equal(await nestedFile.getAttribute('aria-level'), '2');
    await firstNestedRow.press('ArrowLeft');
    assert.equal(await folder.evaluate((row) => row === document.activeElement), true);
    await folder.press('End');
    assert.equal(await evidenceFile.evaluate((row) => row === document.activeElement), true);
    await nestedFile.click();
    const remoteEditor = page.locator('#remote-file-editor');
    await waitForRemoteEditor(page);
    await waitUntil(() => workspaceRequests(workspaceRpcEvidence,
      {connectionId: 2, method: 'fs.readText', path: 'alternate/nested.txt'}).length > 0);
    const alternateListsBeforeRemoval = workspaceRequests(workspaceRpcEvidence,
      {connectionId: 2, method: 'fs.list', path: 'alternate'}).length;
    fs.rmSync(removedWorkspace, {recursive: true, force: true});
    await page.locator('#remote-files-refresh').click();
    await waitUntil(() => workspaceRequests(workspaceRpcEvidence,
      {connectionId: 2, method: 'fs.list', path: 'alternate'}).length > alternateListsBeforeRemoval);
    await page.waitForFunction(() => !document.getElementById('remote-files-refresh').disabled, null, {timeout: 10000});
    assert.equal(await page.locator('#remote-file-tree .remote-tree-row', {hasText: 'removed-during-prefetch'}).count(), 0);

    fs.mkdirSync(removedWorkspace);
    const alternateListsBeforeSave = workspaceRequests(workspaceRpcEvidence,
      {connectionId: 2, method: 'fs.list', path: 'alternate'}).length;
    const nestedWritesBeforeSave = workspaceRequests(workspaceRpcEvidence,
      {connectionId: 2, method: 'fs.writeText', path: 'alternate/nested.txt'}).length;
    await setRemoteEditorValue(page, 'NESTED_TREE_ITEM\nSAVE_REINTRODUCES_EMPTY_CHILD\n');
    await saveRemoteEditor(page);
    await page.waitForFunction(() => ['已保存', '已同步'].includes(document.getElementById('remote-editor-state').textContent));
    await waitUntil(() => workspaceRequests(workspaceRpcEvidence,
      {connectionId: 2, method: 'fs.writeText', path: 'alternate/nested.txt'}).length > nestedWritesBeforeSave);
    await waitUntil(() => workspaceRequests(workspaceRpcEvidence,
      {connectionId: 2, method: 'fs.list', path: 'alternate'}).length > alternateListsBeforeSave);
    assert.equal(fs.readFileSync(path.join(alternateWorkspace, 'nested.txt'), 'utf8'),
      'NESTED_TREE_ITEM\nSAVE_REINTRODUCES_EMPTY_CHILD\n');
    const reintroducedFolder = page.locator('#remote-file-tree .remote-tree-row', {hasText: 'removed-during-prefetch'});
    await reintroducedFolder.waitFor();
    const removedListsBeforeExpand = workspaceRequests(workspaceRpcEvidence,
      {connectionId: 2, method: 'fs.list', path: 'alternate/removed-during-prefetch'}).length;
    await reintroducedFolder.click();
    await waitUntil(() => workspaceRequests(workspaceRpcEvidence,
      {connectionId: 2, method: 'fs.list', path: 'alternate/removed-during-prefetch'}).length > removedListsBeforeExpand);
    assert.equal(await page.locator('#remote-file-tree .remote-tree-row', {hasText: 'stale-prefetch-result.txt'}).count(), 0,
      'refreshing a removed and recreated subtree must not resurrect stale prefetched children');
    report.checks.push('Worker-backed refresh invalidates removed subtree caches; read and write RPC update the editor and actual fixture filesystem');

    const unavailableWorkspace = `${workspace}.temporarily-unavailable`;
    fs.renameSync(workspace, unavailableWorkspace);
    try {
      await page.locator('#remote-files-refresh').click();
      await page.waitForFunction(() => !document.getElementById('remote-files-refresh').disabled
        && document.querySelector('#workbench-notification-root .workbench-notification'));
      const remoteErrorNotification = page.locator('#workbench-notification-root .workbench-notification').first();
      const notificationText = await remoteErrorNotification.innerText();
      assert.match(notificationText, /Workspace|远程|目录|路径/);
      const notificationBounds = await remoteErrorNotification.boundingBox();
      const viewport = page.viewportSize();
      assert(notificationBounds && notificationBounds.y >= 0 && (!viewport || notificationBounds.y < viewport.height),
        'Remote errors should remain visible at the top of the current viewport');
      await remoteErrorNotification.locator('.workbench-notification-copy').click();
      await page.waitForFunction(() => document.querySelector('#workbench-notification-root .workbench-notification[data-copy-state="copied"]'));
      assert.match(await app.evaluate(({clipboard}) => clipboard.readText()), /Workspace|远程|目录|路径/,
        'Copy action should write the complete remote error');
      await remoteErrorNotification.locator('.workbench-notification-close').click();
    } finally {
      if (fs.existsSync(unavailableWorkspace) && !fs.existsSync(workspace)) fs.renameSync(unavailableWorkspace, workspace);
    }
    assert.equal(await folder.isVisible(), true);
    assert.equal(await nestedFile.isVisible(), true);
    assert.equal(await folder.getAttribute('aria-expanded'), 'true');
    assert.equal(sftpEvidence.openDirectories.length, 0);
    report.checks.push('Remote Explorer expands prefetched Worker cache without another list, preserves keyboard navigation, and surfaces copyable non-blocking errors while retaining the last good tree');
    checkpoint('Explorer cache and error notification verified');
    await evidenceFile.click();
    await waitForRemoteEditor(page);
    assert.equal(await page.locator('#remote-editor-state').textContent(), '已同步');
    assert.match(await page.locator('#remote-editor-meta').textContent(), /UTF-8 BOM · CRLF/);
    await setRemoteEditorValue(page, 'SSH_REMOTE_EVIDENCE\nREMOTE_EDITOR_SAVED\n');
    await saveRemoteEditor(page);
    await setRemoteEditorValue(page, 'SSH_REMOTE_EVIDENCE\nREMOTE_EDITOR_SAVED\nEDITED_DURING_SAVE\n');
    await page.waitForFunction(() => document.getElementById('remote-editor-state').textContent === '尚未保存'
      && document.getElementById('remote-file-save').textContent === '保存'
      && !document.getElementById('remote-file-save').disabled);
    assert.deepEqual(fs.readFileSync(path.join(workspace, 'server-evidence.txt')), Buffer.concat([
      Buffer.from([0xef, 0xbb, 0xbf]),
      Buffer.from('SSH_REMOTE_EVIDENCE\r\nREMOTE_EDITOR_SAVED\r\n'),
    ]));
    await saveRemoteEditor(page);
    await page.waitForFunction(() => ['已保存', '已同步'].includes(document.getElementById('remote-editor-state').textContent));
    assert.deepEqual(fs.readFileSync(path.join(workspace, 'server-evidence.txt')), Buffer.concat([
      Buffer.from([0xef, 0xbb, 0xbf]),
      Buffer.from('SSH_REMOTE_EVIDENCE\r\nREMOTE_EDITOR_SAVED\r\nEDITED_DURING_SAVE\r\n'),
    ]));
    report.checks.push('Remote editor preserves BOM/CRLF, saves with Ctrl/Cmd+S, and keeps edits typed during a save dirty');

    const editorTabs = page.locator('#remote-editor-tabs .remote-editor-tab-shell');
    assert.equal(await editorTabs.count(), 2, 'opening another remote file must keep the previous editor tab');
    const evidenceTab = editorTabs.filter({hasText: 'server-evidence.txt'});
    const nestedTab = editorTabs.filter({hasText: 'nested.txt'});
    await setRemoteEditorValue(page, 'SSH_REMOTE_EVIDENCE\nREMOTE_EDITOR_SAVED\nEDITED_DURING_SAVE\nFIRST_TAB_DIRTY\n');
    page.once('dialog', dialog => dialog.dismiss());
    await evidenceTab.locator('.remote-editor-tab-close').click();
    assert.equal(await editorTabs.count(), 2, 'dismissing the dirty-tab prompt must retain every editor buffer');
    await nestedTab.locator('.remote-editor-tab').click();
    assert.equal(await remoteEditorValue(page), 'NESTED_TREE_ITEM\nSAVE_REINTRODUCES_EMPTY_CHILD\n');
    await setRemoteEditorValue(page, 'NESTED_TREE_ITEM\nSECOND_TAB_DIRTY\n');
    await evidenceTab.locator('.remote-editor-tab').click();
    assert.equal(await remoteEditorValue(page), 'SSH_REMOTE_EVIDENCE\nREMOTE_EDITOR_SAVED\nEDITED_DURING_SAVE\nFIRST_TAB_DIRTY\n');
    await saveRemoteEditor(page);
    await page.waitForFunction(() => ['已保存', '已同步'].includes(document.getElementById('remote-editor-state').textContent));
    assert.equal(await nestedTab.locator('.remote-editor-tab-dirty').getAttribute('hidden'), null,
      'saving the active tab must not clear another tab dirty state');
    await nestedTab.locator('.remote-editor-tab').click();
    assert.equal(await page.locator('#remote-editor-state').textContent(), '尚未保存');
    await saveRemoteEditor(page);
    await page.waitForFunction(() => ['已保存', '已同步'].includes(document.getElementById('remote-editor-state').textContent));
    await nestedTab.locator('.remote-editor-tab-close').click();
    assert.equal(await editorTabs.count(), 1);
    assert.equal(await remoteEditorValue(page), 'SSH_REMOTE_EVIDENCE\nREMOTE_EDITOR_SAVED\nEDITED_DURING_SAVE\nFIRST_TAB_DIRTY\n');
    report.checks.push('Remote Explorer keeps multiple file tabs with independent buffers and dirty state, switches without closing, saves the active tab, and closes a selected tab');
    checkpoint('Worker-backed editor read, write and tabs verified');

    await page.locator('#agent-settings').click();
    await page.locator('#tab-integration').click();
    assert(await page.locator('#run-demo').isVisible(), 'The connection demo belongs to Settings > 工具集成, outside the dedicated Explorer canvas');
    await page.locator('#run-demo').click();
    await page.waitForFunction(() => document.querySelector('.agent-execution-log')?.textContent.includes('server-evidence.txt'), { timeout: 15000 });
    await waitUntil(() => workspaceRequests(workspaceRpcEvidence,
      {connectionId: 2, method: 'proc.exec'}).some(request => request.params.command?.includes('ls -la')));
    assert(workspaceRpcEvidence.sessions.some(session => session.connectionId === 2
      && session.requests.some(request => request.method === 'proc.exec')),
    'Code Agent remote commands must run through a persistent Workspace Worker lane');
    assert.equal(sftpEvidence.openDirectories.length, 0);
    report.checks.push('Native PTY demo and Code Agent remote_exec use a persistent Workspace Worker lane and return the actual remote file listing');
    assert.equal(connections, sharedConnectionCount);
    await page.locator('#close-settings').click();
    await page.waitForFunction(() => document.querySelector('.agent-execution-log').textContent.includes('server-evidence.txt'));
    await page.locator('[data-terminal-id="agent-execution"] .ssh-terminal-tab').click();
    assert(await page.locator('.agent-execution-pane').isVisible());
    assert.equal(await page.locator('.agent-execution-pane .xterm-helper-textarea').count(), 0);
    assert.match(await page.locator('.agent-execution-log').textContent(), /\$ pwd && uname -a && ls -la/);
    const firstTerminalId = await page.locator('.ssh-terminal-tab-shell:not([data-terminal-id="agent-execution"])').first().getAttribute('data-terminal-id');
    await page.locator(`.ssh-terminal-tab-shell[data-terminal-id="${firstTerminalId}"] .ssh-terminal-tab`).click();
    report.checks.push('Agent remote_exec commands, output and status appear in the fixed read-only execution tab without replacing the interactive shell');
    await page.evaluate(() => { window.__sshOutput = ''; window.desktop.onEvent((event) => { if (event.type === 'terminal' && event.target === 'ssh') window.__sshOutput += event.data; }); });
    await page.locator('#ssh-terminal .xterm-helper-textarea').focus();
    await page.keyboard.type("printf 'TERMINAL_%s\\n' ROUNDTRIP"); await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.__sshOutput.includes('TERMINAL_ROUNDTRIP'));
    report.checks.push('Interactive SSH terminal keyboard roundtrip');
    await page.evaluate((terminalId) => {
      window.__firstSshTerminal = document.querySelector(`.ssh-terminal-pane[data-terminal-id="${terminalId}"] .xterm`);
    }, firstTerminalId);
    await page.locator('#new-ssh-terminal').click();
    await page.waitForFunction(() => document.querySelectorAll('.ssh-terminal-tab-shell:not([data-terminal-id="agent-execution"])').length === 2);
    const terminalIds = await page.locator('.ssh-terminal-tab-shell:not([data-terminal-id="agent-execution"])').evaluateAll(elements => elements.map(element => element.dataset.terminalId));
    const secondTerminalId = terminalIds.find(value => value !== firstTerminalId);
    assert(secondTerminalId);
    assert.equal(connections, sharedConnectionCount);
    await page.locator('.ssh-terminal-pane:not([hidden]) .xterm-helper-textarea').focus();
    await page.keyboard.type("printf 'SECOND_%s\\n' TERMINAL"); await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.__sshOutput.includes('SECOND_TERMINAL'));
    await page.locator(`.ssh-terminal-tab-shell[data-terminal-id="${firstTerminalId}"]`).click({ button: 'right' });
    await page.locator('.ssh-terminal-context-item', { hasText: '拆分终端' }).click();
    await page.waitForFunction(() => document.querySelectorAll('.ssh-terminal-pane:not([hidden])').length === 2
      && document.querySelector('.ssh-terminal-panes')?.classList.contains('split'));
    const splitWidthsBefore = await page.locator('.ssh-terminal-pane:not([hidden])').evaluateAll(elements => elements.map(element => element.getBoundingClientRect().width));
    const splitDivider = await page.locator('.ssh-terminal-pane-splitter').boundingBox();
    assert(splitDivider);
    await page.mouse.move(splitDivider.x + splitDivider.width / 2, splitDivider.y + splitDivider.height / 2);
    await page.mouse.down();
    await page.mouse.move(splitDivider.x + 72, splitDivider.y + splitDivider.height / 2, { steps: 5 });
    await page.mouse.up();
    const splitWidthsAfter = await page.locator('.ssh-terminal-pane:not([hidden])').evaluateAll(elements => elements.map(element => element.getBoundingClientRect().width));
    assert(splitWidthsAfter[0] > splitWidthsBefore[0] + 40, 'dragging the split divider must enlarge the left terminal');
    assert(splitWidthsAfter[1] < splitWidthsBefore[1] - 40, 'dragging the split divider must shrink the right terminal');
    assert.equal(await page.locator('.ssh-terminal-pane:not([hidden]) .xterm').count(), 2);
    await page.locator(`.ssh-terminal-tab-shell[data-terminal-id="${firstTerminalId}"]`).click({ button: 'right' });
    await page.locator('.ssh-terminal-context-item', { hasText: '取消拆分' }).click();
    await page.waitForFunction(() => document.querySelectorAll('.ssh-terminal-pane:not([hidden])').length === 1);
    report.checks.push('Terminal tab context menu splits two live SSH shells side by side and the draggable divider resizes both panes');
    await page.locator(`.ssh-terminal-tab-shell[data-terminal-id="${firstTerminalId}"] .ssh-terminal-tab`).click();
    assert(await page.evaluate((terminalId) => window.__firstSshTerminal === document.querySelector(`.ssh-terminal-pane[data-terminal-id="${terminalId}"] .xterm`), firstTerminalId));
    await page.locator(`.ssh-terminal-tab-shell[data-terminal-id="${secondTerminalId}"] .ssh-terminal-tab`).click();
    await page.locator(`.ssh-terminal-tab-shell[data-terminal-id="${secondTerminalId}"] .ssh-terminal-tab-close`).click();
    await page.waitForFunction(() => document.querySelectorAll('.ssh-terminal-tab-shell:not([data-terminal-id="agent-execution"])').length === 1);
    assert(await page.locator(`.ssh-terminal-pane[data-terminal-id="${firstTerminalId}"]`).isVisible());
    assert.equal(connections, sharedConnectionCount);
    report.checks.push('VS Code-style terminal tabs create and close independent shells on one SSH connection while preserving the original xterm');
    const pasteCommand = "printf 'CLIPBOARD_%s\\n' ROUNDTRIP";
    await app.evaluate(({ clipboard }, value) => clipboard.writeText(value), pasteCommand);
    await page.locator('#ssh-terminal .xterm-helper-textarea').focus();
    await page.keyboard.press(process.platform === 'darwin' ? 'Meta+V' : 'Control+V');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.__sshOutput.includes('CLIPBOARD_ROUNDTRIP'));
    report.checks.push('Native clipboard paste reaches the focused xterm without clipboard-read permission');
    await page.keyboard.type('sleep 30'); await page.keyboard.press('Enter');
    await page.waitForTimeout(150);
    await page.keyboard.press('Control+C');
    await page.keyboard.type("printf 'INTERRUPT_%s\\n' PRESERVED"); await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.__sshOutput.includes('INTERRUPT_PRESERVED'), null, { timeout: 5000 });
    report.checks.push('Ctrl+C without a selection still interrupts the terminal process');
    await page.screenshot({ path: path.join(output, 'desktop-connected.png'), fullPage: true });
    checkpoint('interactive terminal and split layout verified');

    // Exercise the generic custom executable integration, distinct from demo mode.
    const cli = path.join(temp, 'test-code-agent.cjs');
    const helperPath = path.resolve(__dirname, '../runtime/agent-tools.cjs');
    fs.writeFileSync(cli, `const {callDesktop}=require(${JSON.stringify(helperPath)});
if(process.stdin.isTTY)process.stdin.setRawMode(true);
console.log('CUSTOM_AGENT_READY');
let data='';
process.stdin.on('data',async chunk=>{
  data+=chunk;if(!data.endsWith('\\r'))return;
  const task=data;data='';const clean=task.replace(/\\x1b\\[20[01]~/g,'');
  console.log('TASK_RECEIVED:'+clean);
  if(clean.includes('异构使能优化工作台”快速初始化')&&clean.includes('初始化只做一件事'))console.log('AGENT_INITIALIZATION_SUBMITTED');
  if(clean.includes('"schema_version":"capture_plan_agent_task/v1"')){
    try{
      const marker='任务上下文：\\n';
      const context=JSON.parse(clean.slice(clean.lastIndexOf(marker)+marker.length).trim());
      await new Promise(resolve=>setTimeout(resolve,6500));
      const updated=await callDesktop('capture_update_plan',{planId:context.plan_id,expectedUpdatedAt:context.expected_updated_at,requestId:context.request_id,confirmUnchanged:true,summary:'Electron fixture confirmed the plan baseline.'});
      console.log('CAPTURE_PLAN_UPDATED:'+updated.requestId);
    }catch(e){console.log('CAPTURE_PLAN_UPDATE_FAILED:'+e.message)}
    return;
  }
  try{const result=await callDesktop('remote_exec',{command:'cat server-evidence.txt'});console.log(result.stdout)}catch(e){console.log(e.message)}
});`);
    await page.locator('#agent-settings').click();
    assert.equal(await page.locator('#agent-bootstrap-context').count(), 0,
      'Code Agent settings must not expose the removed automatic bootstrap input');
    await page.locator('#copy-agent-memory-prompt').click();
    await page.waitForFunction(() => document.getElementById('settings-error').textContent.includes('长期记忆系统提示已复制'));
    const memoryPrompt = await app.evaluate(({clipboard}) => clipboard.readText());
    assert.match(memoryPrompt, /写入你的用户级长期记忆或全局持久规则/);
    assert.match(memoryPrompt, /remote_status/);
    assert.match(memoryPrompt, /workspace_(?:stat|list|read|search|write)/);
    assert.doesNotMatch(memoryPrompt, /127\.0\.0\.1|desktop-test|HETERO_DESKTOP_BROKER_TOKEN/);
    report.checks.push('Code Agent settings copy a stable long-term-memory prompt without writing deployment identity into the conversation');
    await page.locator('#copy-agent-mcp-configuration-prompt').click();
    await page.waitForFunction(() => document.getElementById('settings-error').textContent.includes('原生 MCP 配置提示已复制'));
    const mcpConfigurationPrompt = await app.evaluate(({clipboard}) => clipboard.readText());
    assert.equal(await page.locator('#agent-mcp-configuration-prompt').textContent(), mcpConfigurationPrompt,
      'native MCP prompt preview must match the copied task exactly');
    assert.equal(await page.locator('#agent-mcp-configuration-prompt').getAttribute('tabindex'), '0',
      'native MCP prompt preview must support keyboard scrolling');
    assert.match(mcpConfigurationPrompt, /配置.*原生 MCP/);
    assert.match(mcpConfigurationPrompt, /mcpServers\.desktop-ssh/);
    assert.match(mcpConfigurationPrompt, /原生 MCP 调用只读工具 remote_status/);
    assert.match(mcpConfigurationPrompt, /desktop_tool_describe 读取 capture_catalog 的单工具合同/);
    assert.match(mcpConfigurationPrompt, /\{mcp_config\}/);
    assert.doesNotMatch(mcpConfigurationPrompt, /127\.0\.0\.1|desktop-test|Bearer\s+[A-Za-z0-9._-]+/);
    report.checks.push('Code Agent settings preview and copy a reusable native MCP configuration prompt');
    await page.locator('#agent-executable').fill(process.execPath);
    await page.locator('#agent-args').fill(JSON.stringify([cli]));
    await page.locator('#agent-cwd').fill(temp);
    assert.equal(await page.locator('#settings-start-agent').count(), 1);
    await page.locator('#save-settings').click();
    await page.waitForFunction(() => document.getElementById('agent-status').textContent === '运行中');
    const firstAgentSession = await page.evaluate(async () => (await window.desktop.invoke('getState')).agent.sessionId);
    await page.locator('#save-settings').click();
    await page.waitForFunction(() => !document.getElementById('save-settings').disabled);
    assert.equal(await page.evaluate(async () => (await window.desktop.invoke('getState')).agent.sessionId), firstAgentSession,
      'saving an unchanged running Agent configuration must preserve the live session');
    assert.equal(await page.locator('#settings-start-agent').textContent(), '重新启动 Agent');
    await page.locator('#settings-start-agent').click();
    await page.waitForFunction((previous) => {
      const badge = document.getElementById('agent-status');
      return badge.textContent === '运行中' && !document.getElementById('settings-start-agent').disabled
        && window.desktop.invoke('getState').then((value) => value.agent.sessionId !== previous);
    }, firstAgentSession);
    await page.locator('#close-settings').click();
    assert.equal(connections, sharedConnectionCount);
    report.checks.push('Saving Code Agent configuration auto-starts without restarting an unchanged session; the only manual restart action lives in settings and starts a fresh session');

    const agentHeaderOrder = await page.evaluate(() => {
      const initialize = document.getElementById('agent-initialize');
      const settings = document.getElementById('agent-settings');
      return Boolean(initialize.compareDocumentPosition(settings) & Node.DOCUMENT_POSITION_FOLLOWING);
    });
    assert(agentHeaderOrder, 'Code Agent initialization must sit immediately before Agent settings');
    await page.locator('#agent-initialize').click();
    await page.waitForFunction(() => document.getElementById('agent-initialize').getAttribute('aria-busy') === 'true');
    assert.equal(await page.locator('#agent-initialize').isDisabled(), true, 'initialization action must stay disabled while the prompt is being submitted');
    await page.waitForFunction(() => document.getElementById('agent-terminal').textContent.includes('AGENT_INITIALIZATION_SUBMITTED'), null, { timeout: 15000 });
    await page.waitForFunction(() => document.getElementById('workbench-notification-root').textContent.includes('初始化任务已发送'));
    await page.waitForFunction(() => !document.getElementById('agent-initialize').disabled
      && document.getElementById('agent-initialize').getAttribute('aria-busy') === 'false');
    report.checks.push('Agent header initialization explicitly submits the memory-and-tool discovery prompt through the live PTY and reports pending and success states');

    // Submit the real capture-page plan task without navigating away from the
    // page. The fixture Agent accepts the task through the actual PTY, then
    // makes a second remote tool call over the already connected SSH session.
    await page.evaluate(() => { window.__liveTerminalNodes = { agent: document.querySelector('#agent-terminal .xterm'), ssh: document.querySelector('#ssh-terminal .xterm') }; });
    await page.locator('[data-page="capture"]').click();
    await page.waitForSelector('#capture-steps [data-step-selected]', { state: 'attached', timeout: 60000 });
    await clickNativeMenu(app, 'layout-agent');
    assert.equal(await page.locator('#agent-panel').isVisible(), false);
    await page.locator('#capture-save').click();
    await page.waitForFunction(() => document.querySelector('#capture-saved-plan').value !== '' && !document.querySelector('#capture-save').disabled);
    const agentBaselinePlanId = await page.locator('#capture-saved-plan').inputValue();
    const agentBaselineTitle = await page.locator('#capture-title').inputValue();
    const alternateTitle = `${agentBaselineTitle} · 切换保护`;
    await page.locator('#capture-title').fill(alternateTitle);
    await page.locator('#capture-save').click();
    await page.waitForFunction((baseline) => document.querySelector('#capture-saved-plan').value !== baseline
      && !document.querySelector('#capture-save').disabled, agentBaselinePlanId);
    const alternatePlanId = await page.locator('#capture-saved-plan').inputValue();
    await page.locator('#capture-saved-plan').selectOption(agentBaselinePlanId);
    await page.waitForFunction((title) => document.querySelector('#capture-title').value === title, agentBaselineTitle);
    if ((await page.locator('#capture-template-details').getAttribute('open')) === null) await page.locator('#capture-template-details > summary').click();
    await page.locator('.capture-agent-details > summary').click();
    await page.locator('#capture-agent-request').fill('完善布局冒烟验证的采集计划。');
    await page.locator('#capture-agent-plan').click();
    try {
      await page.waitForFunction(() => document.querySelector('#capture-agent-plan').getAttribute('aria-busy') === 'true'
        && !document.querySelector('#capture-saved-plan').disabled, null, { timeout: 15000 });
    } catch (error) {
      const diagnostic = await page.evaluate(() => ({
        agentBusy: document.querySelector('#capture-agent-plan')?.getAttribute('aria-busy'),
        agentDisabled: document.querySelector('#capture-agent-plan')?.disabled,
        savedPlanDisabled: document.querySelector('#capture-saved-plan')?.disabled,
        notice: document.querySelector('#capture-notice')?.textContent,
        notifications: document.querySelector('#workbench-notification-root')?.textContent,
        agentState: document.querySelector('#capture-agent-state')?.textContent,
        agentTerminal: document.querySelector('#agent-terminal')?.textContent?.slice(-2000),
      }));
      throw new Error(`capture Agent did not enter submitted wait state: ${JSON.stringify(diagnostic)}`, {cause: error});
    }
    await page.locator('#capture-saved-plan').selectOption(alternatePlanId);
    await page.waitForFunction((title) => document.querySelector('#capture-title').value === title, alternateTitle);
    await page.waitForFunction(() => !document.querySelector('#capture-update').hidden, null, { timeout: 15000 });
    assert.equal(await page.locator('#capture-saved-plan').inputValue(), alternatePlanId,
      'an Agent result for another baseline must not replace the plan selected while it was running');
    assert((await page.locator('#capture-update').textContent()).includes('手动载入'));
    assert.equal(await page.locator('#capture-agent-plan').getAttribute('aria-busy'), 'false');
    await page.locator('#capture-apply-update').click();
    await page.waitForFunction((planId) => document.querySelector('#capture-saved-plan').value === planId, agentBaselinePlanId);
    assert(await page.locator('#capture-page').isVisible(), 'Plan submission must preserve the capture page');
    assert(await page.locator('#agent-panel').isVisible(), 'Plan submission must reveal the Agent pane');
    assert(await page.locator('#terminal-panel').isVisible());
    assert.equal(connections, sharedConnectionCount);
    report.checks.push('Capture plan completion stays correlated to its submitted plan revision and becomes an explicit apply action after the user switches plans');

    const blockerOperation = page.locator('select[aria-label="第 1 步动作"]');
    await blockerOperation.selectOption('unresolved');
    await page.locator('#capture-template-details > summary').click();
    assert.equal(await page.locator('#capture-template-details').getAttribute('open'), null);
    await page.locator('#capture-agent-plan').click();
    await page.waitForFunction(() => document.querySelector('#capture-agent-plan').getAttribute('aria-busy') === 'true');
    await page.waitForFunction(() => document.querySelector('#capture-notice').textContent.includes('仍有')
      && document.querySelector('#capture-agent-plan').getAttribute('aria-busy') === 'false', null, { timeout: 20000 });
    assert.equal(await page.locator('#capture-template-details').getAttribute('open'), '', 'Agent blockers should reveal the template details that need correction');
    assert(await page.locator('#capture-start').isDisabled(), 'an Agent completion with remaining blockers must not focus an executable plan');
    report.checks.push('Agent completion with unresolved readiness blockers restores Retry and keeps capture disabled instead of claiming the plan is executable');
    checkpoint('custom Code Agent and capture submission verified');

    await clickNativeMenu(app, 'layout-agent');
    await clickNativeMenu(app, 'layout-terminal');
    await page.locator('[data-page="traces"]').click();
    await clickNativeMenu(app, 'layout-agent');
    await clickNativeMenu(app, 'layout-terminal');
    assert(await page.evaluate(() => window.__liveTerminalNodes.agent === document.querySelector('#agent-terminal .xterm') && window.__liveTerminalNodes.ssh === document.querySelector('#ssh-terminal .xterm')));
    await page.locator('#ssh-terminal .xterm-helper-textarea').focus();
    await page.keyboard.type("printf 'AFTER_LAYOUT_%s\\n' ROUNDTRIP"); await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.__sshOutput.includes('AFTER_LAYOUT_ROUNDTRIP'));
    assert.equal(await page.locator('#agent-status').textContent(), '运行中');
    assert.equal(connections, sharedConnectionCount);
    report.checks.push('Collapsing panes and changing pages keeps the running Agent process, original terminal DOM and live SSH keyboard roundtrip');
    await page.locator('[data-page="capture"]').click();
    await page.screenshot({ path: path.join(output, 'desktop-global-agent-capture.png'), fullPage: true });
    checkpoint('global pane layout verified');
    if (process.env.DESKTOP_TEST_HDC) {
      await page.locator('[data-page="devices"]').click();
      await page.locator('#hdc-path').fill(process.env.DESKTOP_TEST_HDC);
      await page.locator('#discover-devices').click();
      await page.waitForFunction(() => !document.getElementById('discover-devices').disabled);
      assert.equal(await page.locator('#device-notice').getAttribute('hidden'), '');
      report.deviceList = await page.locator('#device-list').innerText();
      await page.screenshot({ path: path.join(output, 'desktop-local-hdc.png'), fullPage: true });
      report.checks.push('Read-only local HDC discovery through desktop IPC');
    }
    await page.locator('[data-page="workbench"]').click();
    await evidenceFile.click();
    await page.waitForFunction(() => document.getElementById('remote-editor-state').textContent === '已同步');
    await disconnectSsh(page);
    fs.writeFileSync(path.join(workspace, 'server-evidence.txt'), Buffer.concat([
      Buffer.from([0xef, 0xbb, 0xbf]),
      Buffer.from('SERVER_REFRESHED_AFTER_RECONNECT\r\n'),
    ]));
    await connectSshByCreatingTerminal(page);
    await page.waitForFunction(() => document.getElementById('remote-file-editor')?.__remoteEditor?.getValue() === 'SERVER_REFRESHED_AFTER_RECONNECT\n');
    assert.equal(await page.locator('#remote-editor-state').textContent(), '已同步');
    report.checks.push('A clean active file is automatically re-read after reconnecting to the same SSH target');
    checkpoint('clean editor reconnect verified');

    checkpoint('dirty reconnect: creating local edit');
    await setRemoteEditorValue(page, 'LOCAL_UNSAVED_RECONNECT_EDIT\n');
    checkpoint('dirty reconnect: disconnecting SSH');
    await disconnectSsh(page);
    checkpoint('dirty reconnect: SSH disconnected');
    fs.writeFileSync(path.join(workspace, 'server-evidence.txt'), Buffer.concat([
      Buffer.from([0xef, 0xbb, 0xbf]),
      Buffer.from('SERVER_CHANGED_WHILE_DISCONNECTED\r\n'),
    ]));
    checkpoint('dirty reconnect: reconnecting SSH');
    await connectSshByCreatingTerminal(page);
    checkpoint('dirty reconnect: SSH reconnected; awaiting revision state');
    await page.waitForFunction(() => document.getElementById('remote-editor-state').textContent.includes('待重新校验'));
    checkpoint('dirty reconnect: revision state ready; submitting fenced save');
    assert.equal(await remoteEditorValue(page), 'LOCAL_UNSAVED_RECONNECT_EDIT\n');
    await saveRemoteEditor(page);
    checkpoint('dirty reconnect: save submitted; awaiting conflict notification');
    await page.waitForFunction(() => document.getElementById('workbench-notification-root').textContent.includes('发生变化'));
    checkpoint('dirty reconnect: conflict notification received');
    await page.locator('#workbench-notification-root .workbench-notification', {hasText: '发生变化'}).locator('.workbench-notification-close').click();
    assert.deepEqual(fs.readFileSync(path.join(workspace, 'server-evidence.txt')), Buffer.concat([
      Buffer.from([0xef, 0xbb, 0xbf]),
      Buffer.from('SERVER_CHANGED_WHILE_DISCONNECTED\r\n'),
    ]));
    report.checks.push('A dirty reconnect buffer is retained, marked for revalidation, and revision fencing rejects a conflicting save');
    checkpoint('dirty editor reconnect conflict verified');

    await setRemoteEditorValue(page, 'UNSAVED_REMOTE_EDIT\n');
    await page.locator('#ssh-settings').click();
    await page.locator('#ssh-cwd').fill(alternateWorkspace);
    await page.locator('#save-settings').click();
    await page.waitForFunction(() => document.getElementById('settings-save-status').textContent.includes('已保存')
      && document.getElementById('ssh-status').textContent === '已连接'
      && !document.getElementById('save-settings').disabled);
    await page.locator('#close-settings').click();
    await focusRemoteEditor(page);
    await page.keyboard.press(process.platform === 'darwin' ? 'Meta+A' : 'Control+A');
    const retained = await page.evaluate(() => {
      const editor = document.getElementById('remote-file-editor').__remoteEditor;
      const view = editor.captureViewState();
      return {
        value: editor.getValue(),
        readOnly: editor.getReadOnly(),
        selectionLength: view.selectionEnd - view.selectionStart,
      };
    });
    assert.deepEqual(retained, {
      value: 'UNSAVED_REMOTE_EDIT\n',
      readOnly: true,
      selectionLength: 'UNSAVED_REMOTE_EDIT\n'.length,
    });
    report.checks.push('Saving a changed SSH root switches the connection and preserves unsaved content as selectable read-only text');
    checkpoint('SSH target switch verified');
    await app.evaluate(({ dialog }) => {
      globalThis.__desktopOriginalMessageBox = dialog.showMessageBoxSync.bind(dialog);
      globalThis.__desktopUnsavedPrompt = null;
      dialog.showMessageBoxSync = (_window, options) => {
        globalThis.__desktopUnsavedPrompt = options;
        return 1;
      };
    });
    // Electron emits the renderer beforeunload through CDP as a JavaScript
    // dialog while the main-process will-prevent-unload handler owns the
    // actual decision. Registering a listener keeps Playwright from racing an
    // automatic dismiss against the native decision stub above.
    page.once('dialog', () => {});
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => Boolean(document.documentElement.dataset.platform));
    assert.equal(await page.title(), 'Heterogeneous Workbench');
    const unsavedPrompt = await app.evaluate(() => globalThis.__desktopUnsavedPrompt);
    assert.match(unsavedPrompt?.message || '', /未保存/);
    assert.deepEqual(fs.readFileSync(path.join(workspace, 'server-evidence.txt')), Buffer.concat([
      Buffer.from([0xef, 0xbb, 0xbf]),
      Buffer.from('SERVER_CHANGED_WHILE_DISCONNECTED\r\n'),
    ]));
    await app.evaluate(({ dialog }) => { dialog.showMessageBoxSync = globalThis.__desktopOriginalMessageBox; });
    report.checks.push('Unsaved remote edits trigger the native close/reload decision and explicit discard never writes the server file');
    checkpoint('unsaved reload decision verified; closing first app');
    await app.close(); app = null;
    checkpoint('first app closed; relaunching');
    app = await _electron.launch({ executablePath: process.env.DESKTOP_ELECTRON_BIN || require('electron'), args: process.env.DESKTOP_PACKAGED ? [] : [path.resolve(__dirname, '..')], env, timeout: 60000 });
    const restartedPage = await app.firstWindow();
    restartedPage.on('pageerror', (error) => errors.push(error.message));
    await restartedPage.waitForFunction(() => document.getElementById('ssh-status').textContent === '已连接'
      && document.getElementById('agent-status').textContent === '运行中', null, { timeout: 30000 });
    assert.equal(await restartedPage.locator('#settings-drawer').getAttribute('hidden'), '');
    report.checks.push('A complete saved configuration automatically reconnects SSH and starts Code Agent after a full app restart');
    checkpoint('application restart verified');
    const settings = fs.readFileSync(path.join(temp, 'user-data/settings.json'), 'utf8');
    assert.ok(!settings.includes('isolated-test'));
    assert.deepEqual(errors, []);
    assert.doesNotMatch(mainProcessStderr.join(''), /No response from server|A JavaScript error occurred in the main process/i);
    report.connections = connections;
    report.checks.push('Secrets absent from preferences; no renderer or uncaught SFTP Main Process errors');
    fs.writeFileSync(path.join(output, 'electron-smoke.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report, null, 2));
  } finally {
    clearTimeout(watchdog);
    if (app) await app.close();
    for (const child of processes) try { child.kill(); } catch {}
    for (const client of clients) client.destroy();
    await new Promise((resolve) => server.close(resolve));
    fs.rmSync(temp, { recursive: true, force: true });
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
