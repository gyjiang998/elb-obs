const test = require('node:test');
const assert = require('node:assert/strict');
const { sameSshConnectionProfile } = require('../renderer/ssh-connection-profile.cjs');

const base = {
  host: 'Server.Example', port: 22, username: 'developer', cwd: '/srv/workspace/',
  auth: 'password', privateKeyPath: '',
};

test('SSH reconnect profile normalizes target spelling without hiding target changes', () => {
  assert.equal(sameSshConnectionProfile(base, {...base, host: ' server.example ', cwd: '/srv/workspace'}), true);
  assert.equal(sameSshConnectionProfile(base, {...base, port: 2200}), false);
  assert.equal(sameSshConnectionProfile(base, {...base, cwd: '/srv/other'}), false);
});

test('SSH reconnect profile includes authentication and active private key identity', () => {
  assert.equal(sameSshConnectionProfile(base, {...base, auth: 'agent'}), false);
  assert.equal(sameSshConnectionProfile(base, {...base, privateKeyPath: '/unused/key'}), true,
    'an inactive private-key field must not make a password connection look changed');
  const keyed = {...base, auth: 'key', privateKeyPath: '/home/developer/.ssh/id_ed25519'};
  assert.equal(sameSshConnectionProfile(keyed, {...keyed, privateKeyPath: ' /home/developer/.ssh/id_ed25519 '}), true);
  assert.equal(sameSshConnectionProfile(keyed, {...keyed, privateKeyPath: '/home/developer/.ssh/id_rsa'}), false);
});
