'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {SafeStorageCredentialStore, sshIdentity} = require('../runtime/credential-store.cjs');

function fixture(t, available = true) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-credentials-'));
  t.after(() => fs.rmSync(directory, {recursive: true, force: true}));
  const safeStorage = {
    isEncryptionAvailable: () => available,
    encryptString: value => Buffer.from(`sealed:${Buffer.from(value).toString('base64')}`),
    decryptString: value => {
      const text = value.toString();
      if (!text.startsWith('sealed:')) throw new Error('cannot decrypt');
      return Buffer.from(text.slice(7), 'base64').toString();
    },
  };
  return {directory, store: new SafeStorageCredentialStore({userDataDir: directory, safeStorage})};
}

test('safe credential store encrypts SSH passwords outside settings and scopes them by target', t => {
  const {directory, store} = fixture(t);
  const first = {host: 'Server.Example', port: 22, username: 'alice'};
  const second = {...first, username: 'bob'};
  store.setSshPassword(first, 'secret-first');
  store.setSshPassword(second, 'secret-second');
  assert.equal(store.getSshPassword(first), 'secret-first');
  assert.equal(store.getSshPassword(second), 'secret-second');
  assert.deepEqual(store.getSshPasswordStatus(first), {available: true, saved: true});
  const onDisk = fs.readFileSync(path.join(directory, 'ssh-credentials.bin'));
  assert.ok(!onDisk.includes(Buffer.from('secret-first')));
  assert.ok(!onDisk.includes(Buffer.from('alice')));
  assert.equal(fs.existsSync(path.join(directory, 'settings.json')), false);
  assert.equal(sshIdentity(first), sshIdentity({...first, host: 'server.example'}));
});

test('unavailable and damaged system credential stores return actionable diagnostics', t => {
  const unavailableFixture = fixture(t, false);
  const unavailable = unavailableFixture.store;
  assert.deepEqual(unavailable.getSshPasswordStatus({host: 'server', username: 'alice'}), {
    available: false, saved: false, error: '系统安全存储当前不可用，SSH 密码无法持久化。',
  });
  fs.writeFileSync(path.join(unavailableFixture.directory, 'ssh-credentials.bin'), 'damaged-but-preserved');
  assert.throws(() => unavailable.setSshPassword({host: 'server', username: 'alice'}, 'secret'), /系统安全存储当前不可用/);
  assert.equal(fs.readFileSync(path.join(unavailableFixture.directory, 'ssh-credentials.bin'), 'utf8'), 'damaged-but-preserved');
  assert.equal(fs.readdirSync(unavailableFixture.directory).some(name => name.includes('.corrupt-')), false);

  const damaged = fixture(t);
  fs.writeFileSync(path.join(damaged.directory, 'ssh-credentials.bin'), 'not-encrypted');
  assert.throws(() => damaged.store.getSshPassword({host: 'server', username: 'alice'}), /无法由当前系统账户解密/);
  assert.match(damaged.store.getSshPasswordStatus({host: 'server', username: 'alice'}).error, /输入新密码/);
  assert.equal(damaged.store.getSshPasswordStatus({host: 'server', username: 'alice'}).recoverable, true);
});

test('an explicit replacement password quarantines damaged stores without overwriting backups', t => {
  const {directory, store} = fixture(t);
  const target = {host: 'server', port: 22, username: 'alice'};
  const filename = path.join(directory, 'ssh-credentials.bin');
  fs.writeFileSync(filename, 'first-damaged-value');
  store.setSshPassword(target, 'replacement-one');
  assert.equal(store.getSshPassword(target), 'replacement-one');
  let backups = fs.readdirSync(directory).filter(name => name.startsWith('ssh-credentials.bin.corrupt-'));
  assert.equal(backups.length, 1);
  assert.equal(fs.readFileSync(path.join(directory, backups[0]), 'utf8'), 'first-damaged-value');

  fs.writeFileSync(filename, Buffer.alloc(0));
  store.setSshPassword(target, 'replacement-two');
  assert.equal(store.getSshPassword(target), 'replacement-two');
  backups = fs.readdirSync(directory).filter(name => name.startsWith('ssh-credentials.bin.corrupt-'));
  assert.equal(backups.length, 2);
  assert.equal(new Set(backups).size, 2);
  assert.ok(backups.some(name => fs.readFileSync(path.join(directory, name), 'utf8') === 'first-damaged-value'));
});

test('forgetting one SSH password preserves credentials for other targets', t => {
  const {store} = fixture(t);
  const first = {host: 'server', port: 22, username: 'alice'};
  const second = {...first, username: 'bob'};
  store.setSshPassword(first, 'first-secret');
  store.setSshPassword(second, 'second-secret');
  assert.equal(store.deleteSshPassword(first), true);
  assert.equal(store.deleteSshPassword(first), false);
  assert.equal(store.getSshPassword(first), null);
  assert.equal(store.getSshPassword(second), 'second-secret');
});
