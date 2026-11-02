'use strict';

const NETWORK_SETTINGS = [
  {exported: ['npm_config_proxy'], target: 'HTTP_PROXY', explicit: ['HTTP_PROXY', 'http_proxy', 'GLOBAL_AGENT_HTTP_PROXY']},
  {exported: ['npm_config_https_proxy'], target: 'HTTPS_PROXY', explicit: ['HTTPS_PROXY', 'https_proxy', 'GLOBAL_AGENT_HTTPS_PROXY']},
  {exported: ['npm_config_noproxy', 'npm_config_no_proxy'], target: 'NO_PROXY', explicit: ['NO_PROXY', 'no_proxy', 'GLOBAL_AGENT_NO_PROXY'], normalize: noProxyValue},
  {exported: ['npm_config_cafile'], target: 'NODE_EXTRA_CA_CERTS', explicit: ['NODE_EXTRA_CA_CERTS']},
  {exported: ['npm_config_cafile'], target: 'CURL_CA_BUNDLE', explicit: ['CURL_CA_BUNDLE']},
];

const ELECTRON_SETTINGS = [
  {exported: ['npm_config_electron_mirror', 'NPM_CONFIG_ELECTRON_MIRROR', 'npm_package_config_electron_mirror'],
    target: 'ELECTRON_MIRROR', explicit: ['HETERO_ELECTRON_MIRROR', 'ELECTRON_MIRROR']},
  {exported: ['npm_config_electron_custom_dir', 'npm_config_electron_customdir', 'NPM_CONFIG_ELECTRON_CUSTOM_DIR',
    'npm_package_config_electron_customDir', 'npm_package_config_electron_custom_dir'],
    target: 'ELECTRON_CUSTOM_DIR', explicit: ['ELECTRON_CUSTOM_DIR']},
  {exported: ['npm_config_electron_custom_filename', 'npm_config_electron_customfilename', 'NPM_CONFIG_ELECTRON_CUSTOM_FILENAME',
    'npm_package_config_electron_customFilename', 'npm_package_config_electron_custom_filename'],
    target: 'ELECTRON_CUSTOM_FILENAME', explicit: ['ELECTRON_CUSTOM_FILENAME']},
  {exported: ['npm_config_electron_config_cache'], target: 'electron_config_cache', explicit: ['electron_config_cache']},
];

const MIRROR_ENV_NAMES = ['HETERO_ELECTRON_MIRROR', 'ELECTRON_MIRROR'];

function hasExplicitValue(env, names) {
  return names.some(name => Object.prototype.hasOwnProperty.call(env, name) && env[name] !== undefined && env[name] !== null);
}

function configValue(stdout) {
  if (typeof stdout !== 'string' && !Buffer.isBuffer(stdout)) return undefined;
  const value = String(stdout).trim();
  if (!value || /^(?:null|undefined)$/i.test(value) || /[\0\r\n]/.test(value)) return undefined;
  return value;
}

function noProxyValue(input) {
  if (typeof input !== 'string' && !Buffer.isBuffer(input)) return undefined;
  const value = String(input);
  if (value.includes('\0')) return undefined;
  const entries = value.split(/[\r\n]+/).map(entry => entry.trim()).filter(Boolean);
  if (!entries.length || entries.some(entry => /^(?:null|undefined)$/i.test(entry))) return undefined;
  return entries.join(',');
}

function defaultRegistry(value) {
  return value.replace(/\/+$/, '').toLowerCase() === 'https://registry.npmjs.org';
}

function importSettings(npmEnv, resolved, settings, log) {
  for (const setting of settings) {
    if (hasExplicitValue(resolved, setting.explicit)) continue;
    const normalize = setting.normalize || configValue;
    const exported = setting.exported.map(name => npmEnv[name]).find(value => normalize(value) !== undefined);
    if (exported === undefined) continue;
    const value = normalize(exported);
    if ((setting.target === 'HTTP_PROXY' || setting.target === 'HTTPS_PROXY') && /^false$/i.test(value)) {
      log(`npm lifecycle explicitly disables ${setting.target}; no proxy URL was imported.`);
      continue;
    }
    resolved[setting.target] = value;
    log(`Using npm lifecycle config for ${setting.target}; value hidden.`);
  }
}

function resolveNpmNetworkEnvironment(baseEnv = process.env, options = {}) {
  const npmEnv = {...baseEnv};
  const resolved = {...npmEnv};
  const log = options.log || console.log;
  importSettings(npmEnv, resolved, NETWORK_SETTINGS, log);
  if (options.archiveLabel && options.mirrorVariable) {
    const registry = configValue(npmEnv.npm_config_registry);
    if (registry !== undefined && !defaultRegistry(registry)) {
      log(`A custom npm registry is configured, but ${options.archiveLabel} are downloaded separately. Configure ${options.mirrorVariable} to change this archive source; registry was not used as a mirror.`);
    }
  }
  return resolved;
}

function resolveNpmDownloadEnvironment(baseEnv = process.env, options = {}) {
  const npmEnv = {...baseEnv};
  const log = options.log || console.log;
  const resolved = resolveNpmNetworkEnvironment(npmEnv, {log});
  importSettings(npmEnv, resolved, ELECTRON_SETTINGS, log);

  if (!hasExplicitValue(resolved, MIRROR_ENV_NAMES)) {
    const registry = configValue(npmEnv.npm_config_registry);
    if (registry !== undefined && !defaultRegistry(registry)) {
      log('A custom npm registry is configured, but Electron runtime archives are downloaded separately. Configure HETERO_ELECTRON_MIRROR or ELECTRON_MIRROR to change the Electron archive source; registry was not used as a mirror.');
    }
  }

  return resolved;
}

module.exports = {resolveNpmDownloadEnvironment, resolveNpmNetworkEnvironment};
