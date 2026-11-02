'use strict';

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const MAX_FILES = 128;
const MAX_BYTES = 2 * 1024 * 1024;
const ALLOWED = new Set(['.md', '.json', '.yaml', '.yml', '.mermaid']);
// Only expose Skills whose complete tool contract is available through the
// Desktop broker. Experimental flow Skills still reference legacy tools and
// must not be advertised to the local Agent as executable capabilities.
const MANAGED_SKILLS = new Set([
  'heterogeneous-workbench-tools',
  'application-execution-flow-reconstruction',
  'dpvs-cvs-performance-analysis',
]);

function sourceRoot({resourcesDir, resourcesPath = process.resourcesPath} = {}) {
  const candidates = [
    resourcesPath && path.join(resourcesPath, 'analysis-worker', 'agent-resources', 'skills'),
    resourcesDir && path.resolve(resourcesDir, '..', 'agent-resources', 'skills'),
    resourcesDir && path.join(resourcesDir, 'build', 'resources', 'analysis-worker', 'agent-resources', 'skills'),
    resourcesDir && path.join(resourcesDir, 'analysis-worker', 'agent-resources', 'skills'),
  ].filter(Boolean);
  for (const candidate of candidates) {
    try {
      if (fs.statSync(path.join(candidate, 'application-execution-flow-reconstruction', 'SKILL.md')).isFile()) return candidate;
    } catch {}
  }
  throw new Error('Desktop 安装中缺少本地 Agent Skills；请更新或重新安装当前版本。');
}

function collect(root) {
  const files = [];
  let total = 0;
  function walk(directory, prefix = '') {
    for (const entry of fs.readdirSync(directory, {withFileTypes: true})) {
      if (!prefix && entry.isDirectory() && !MANAGED_SKILLS.has(entry.name)) continue;
      const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
      const filename = path.join(directory, entry.name);
      const attributes = fs.lstatSync(filename);
      if (attributes.isSymbolicLink()) throw new Error(`Agent Skill 资源不能包含符号链接：${relative}`);
      if (entry.isDirectory()) walk(filename, relative);
      else if (entry.isFile()) {
        if (!ALLOWED.has(path.extname(entry.name).toLowerCase())) throw new Error(`Agent Skill 资源类型不受支持：${relative}`);
        if (files.length + 1 > MAX_FILES) throw new Error('Agent Skill 资源文件过多。');
        total += attributes.size;
        if (total > MAX_BYTES) throw new Error('Agent Skill 资源超过 2 MiB。');
        files.push({relative, filename, size: attributes.size,
          sha256: crypto.createHash('sha256').update(fs.readFileSync(filename)).digest('hex')});
      }
    }
  }
  walk(root);
  files.sort((left, right) => left.relative.localeCompare(right.relative));
  return files;
}

function managedManifest(files, version) {
  const records = files.map(({relative, size, sha256}) => ({path: relative, size, sha256}));
  const identity = crypto.createHash('sha256').update(JSON.stringify({version: String(version || ''),
    files: files.map(({relative, size, sha256}) => ({relative, size, sha256}))})).digest('hex');
  return {schema_version: 'heterogeneous_desktop_agent_skills/v1', identity,
    version: String(version || ''), files: records};
}

function validManagedRoot(destination, expected) {
  try {
    const root = fs.lstatSync(destination);
    const manifestPath = path.join(destination, 'manifest.json');
    const manifestInfo = fs.lstatSync(manifestPath);
    if (!root.isDirectory() || root.isSymbolicLink() || !manifestInfo.isFile() || manifestInfo.isSymbolicLink()) return false;
    const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
    if (manifest.schema_version !== expected.schema_version || manifest.identity !== expected.identity
        || manifest.version !== expected.version || JSON.stringify(manifest.files) !== JSON.stringify(expected.files)) return false;
    const actual = [];
    function walk(directory, prefix = '') {
      for (const entry of fs.readdirSync(directory, {withFileTypes: true})) {
        const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
        const filename = path.join(directory, entry.name);
        const attributes = fs.lstatSync(filename);
        if (attributes.isSymbolicLink()) throw new Error('symlink');
        if (entry.isDirectory()) walk(filename, relative);
        else if (entry.isFile()) actual.push(relative);
        else throw new Error('unsupported entry');
      }
    }
    walk(destination);
    const expectedPaths = ['manifest.json', ...expected.files.map(item => item.path)].sort();
    if (JSON.stringify(actual.sort()) !== JSON.stringify(expectedPaths)) return false;
    for (const record of expected.files) {
      const filename = path.join(destination, ...record.path.split('/'));
      const attributes = fs.lstatSync(filename);
      if (!attributes.isFile() || attributes.isSymbolicLink() || attributes.size !== record.size
          || crypto.createHash('sha256').update(fs.readFileSync(filename)).digest('hex') !== record.sha256) return false;
    }
    return true;
  } catch { return false; }
}

function prepareManagedAgentSkills({userDataDir, resourcesDir, version, resourcesPath = process.resourcesPath} = {}) {
  if (!userDataDir) throw new Error('Agent Skill 目录缺少 userDataDir。');
  const source = sourceRoot({resourcesDir, resourcesPath});
  const files = collect(source);
  const manifest = managedManifest(files, version);
  const {identity} = manifest;
  const base = path.join(userDataDir, 'agent-skills');
  const destination = path.join(base, identity);
  if (validManagedRoot(destination, manifest)) return destination;

  fs.mkdirSync(base, {recursive: true, mode: 0o700});
  fs.rmSync(destination, {recursive: true, force: true});
  const temporary = path.join(base, `.install-${process.pid}-${crypto.randomUUID()}`);
  fs.mkdirSync(temporary, {recursive: true, mode: 0o700});
  try {
    for (const file of files) {
      const target = path.join(temporary, ...file.relative.split('/'));
      fs.mkdirSync(path.dirname(target), {recursive: true, mode: 0o700});
      fs.copyFileSync(file.filename, target);
      fs.chmodSync(target, 0o400);
    }
    fs.writeFileSync(path.join(temporary, 'manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`, {mode: 0o400});
    try { fs.renameSync(temporary, destination); }
    catch (error) {
      if (error.code !== 'EEXIST' && error.code !== 'ENOTEMPTY') throw error;
      fs.rmSync(temporary, {recursive: true, force: true});
    }
  } catch (error) {
    fs.rmSync(temporary, {recursive: true, force: true});
    throw error;
  }
  if (!validManagedRoot(destination, manifest)) throw new Error('本地 Agent Skill 安装后完整性校验失败。');
  return destination;
}

module.exports = {MANAGED_SKILLS, prepareManagedAgentSkills, sourceRoot, validManagedRoot};
