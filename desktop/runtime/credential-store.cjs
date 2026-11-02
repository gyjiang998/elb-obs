'use strict';

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const MAX_STORE_BYTES = 1024 * 1024;
const RECOVERABLE_CODES = new Set(['CREDENTIAL_STORE_INVALID', 'CREDENTIAL_STORE_DECRYPT_FAILED']);

class CredentialStoreError extends Error {
  constructor(code, message, cause) {
    super(message, cause ? {cause} : undefined);
    this.name = 'CredentialStoreError';
    this.code = code;
  }
}

function sshIdentity({host = '', port = 22, username = ''} = {}) {
  const value = `${String(username).trim()}\0${String(host).trim().toLowerCase()}\0${Number(port) || 22}`;
  return crypto.createHash('sha256').update(value).digest('hex');
}

class SafeStorageCredentialStore {
  constructor({userDataDir, safeStorage, fsImpl = fs} = {}) {
    if (!userDataDir) throw new Error('userDataDir is required');
    this.safeStorage = safeStorage;
    this.fs = fsImpl;
    this.filename = path.join(userDataDir, 'ssh-credentials.bin');
  }

  encryptionAvailable() {
    try { return Boolean(this.safeStorage?.isEncryptionAvailable?.()); } catch { return false; }
  }

  requireEncryption() {
    if (!this.encryptionAvailable()) {
      throw new CredentialStoreError('CREDENTIAL_STORE_UNAVAILABLE', '系统安全存储当前不可用，无法安全保存或读取 SSH 密码。请确认系统登录密钥环可用后重试。');
    }
  }

  readStore() {
    let encrypted;
    try { encrypted = this.fs.readFileSync(this.filename); }
    catch (error) {
      if (error.code === 'ENOENT') return {version: 1, sshPasswords: {}};
      throw new CredentialStoreError('CREDENTIAL_STORE_READ_FAILED', 'SSH 密码存储文件无法读取，请检查当前用户数据目录权限。', error);
    }
    if (!Buffer.isBuffer(encrypted) || encrypted.length === 0 || encrypted.length > MAX_STORE_BYTES) {
      throw new CredentialStoreError('CREDENTIAL_STORE_INVALID', 'SSH 密码存储文件无效。输入新密码后，工作台会保留损坏文件并重建安全凭据。');
    }
    this.requireEncryption();
    let decoded;
    try { decoded = this.safeStorage.decryptString(encrypted); }
    catch (error) {
      throw new CredentialStoreError('CREDENTIAL_STORE_DECRYPT_FAILED', '已保存的 SSH 密码无法由当前系统账户解密。输入新密码后，工作台会保留原文件并重建安全凭据。', error);
    }
    try {
      const value = JSON.parse(decoded);
      if (value?.version !== 1 || !value.sshPasswords || Array.isArray(value.sshPasswords) || typeof value.sshPasswords !== 'object') throw new Error('invalid schema');
      if (Object.values(value.sshPasswords).some(password => typeof password !== 'string')) throw new Error('invalid password');
      return value;
    } catch (error) {
      throw new CredentialStoreError('CREDENTIAL_STORE_INVALID', '已解密的 SSH 密码存储内容无效。输入新密码后，工作台会保留损坏文件并重建安全凭据。', error);
    }
  }

  writeStore(value) {
    this.requireEncryption();
    let encrypted;
    try { encrypted = this.safeStorage.encryptString(JSON.stringify(value)); }
    catch (error) {
      throw new CredentialStoreError('CREDENTIAL_STORE_ENCRYPT_FAILED', '系统安全存储无法加密 SSH 密码，请确认系统登录密钥环可用后重试。', error);
    }
    const temporary = `${this.filename}.${process.pid}.${crypto.randomUUID()}.tmp`;
    try {
      this.fs.mkdirSync(path.dirname(this.filename), {recursive: true, mode: 0o700});
      this.fs.writeFileSync(temporary, encrypted, {mode: 0o600});
      this.fs.renameSync(temporary, this.filename);
    } catch (error) {
      try { this.fs.rmSync(temporary, {force: true}); } catch {}
      throw new CredentialStoreError('CREDENTIAL_STORE_WRITE_FAILED', 'SSH 密码无法写入当前用户的安全凭据文件，请检查用户数据目录权限。', error);
    }
  }

  getSshPassword(target) {
    const value = this.readStore().sshPasswords[sshIdentity(target)];
    return typeof value === 'string' && value ? value : null;
  }

  setSshPassword(target, password) {
    if (typeof password !== 'string' || !password) throw new CredentialStoreError('CREDENTIAL_EMPTY', 'SSH 密码不能为空。');
    // Never quarantine a store merely because the OS keychain is unavailable.
    // Recovery is reserved for a user-supplied replacement password and a
    // store that was conclusively shown to be damaged or undecryptable.
    this.requireEncryption();
    let value;
    try { value = this.readStore(); }
    catch (error) {
      if (!RECOVERABLE_CODES.has(error.code)) throw error;
      this.quarantineDamagedStore();
      value = {version: 1, sshPasswords: {}};
    }
    value.sshPasswords[sshIdentity(target)] = password;
    this.writeStore(value);
  }

  quarantineDamagedStore() {
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    const backup = `${this.filename}.corrupt-${stamp}-${crypto.randomUUID()}`;
    try { this.fs.renameSync(this.filename, backup); }
    catch (error) {
      throw new CredentialStoreError('CREDENTIAL_STORE_WRITE_FAILED', '损坏的 SSH 密码存储文件无法隔离备份，请检查当前用户数据目录权限。', error);
    }
    return backup;
  }

  deleteSshPassword(target) {
    this.requireEncryption();
    const value = this.readStore();
    const identity = sshIdentity(target);
    if (!Object.prototype.hasOwnProperty.call(value.sshPasswords, identity)) return false;
    delete value.sshPasswords[identity];
    this.writeStore(value);
    return true;
  }

  getSshPasswordStatus(target) {
    if (!this.encryptionAvailable()) return {available: false, saved: false, error: '系统安全存储当前不可用，SSH 密码无法持久化。'};
    try { return {available: true, saved: Boolean(this.getSshPassword(target))}; }
    catch (error) { return {available: true, saved: false, error: error.message, recoverable: RECOVERABLE_CODES.has(error.code)}; }
  }
}

module.exports = {CredentialStoreError, SafeStorageCredentialStore, sshIdentity};
