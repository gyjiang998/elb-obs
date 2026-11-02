'use strict';

const LANGUAGE_BY_EXTENSION = Object.freeze({
  '.bat': 'bat',
  '.c': 'c',
  '.cc': 'cpp',
  '.cfg': 'ini',
  '.cmake': 'cmake',
  '.conf': 'ini',
  '.cpp': 'cpp',
  '.cs': 'csharp',
  '.css': 'css',
  '.csv': 'plaintext',
  '.cxx': 'cpp',
  '.dart': 'dart',
  '.dockerfile': 'dockerfile',
  '.env': 'ini',
  '.go': 'go',
  '.graphql': 'graphql',
  '.gql': 'graphql',
  '.h': 'c',
  '.hh': 'cpp',
  '.hlsl': 'hlsl',
  '.hpp': 'cpp',
  '.htm': 'html',
  '.html': 'html',
  '.ini': 'ini',
  '.java': 'java',
  '.js': 'javascript',
  '.json': 'json',
  '.json5': 'json',
  '.jsonc': 'json',
  '.jsx': 'javascript',
  '.kt': 'kotlin',
  '.kts': 'kotlin',
  '.less': 'less',
  '.lua': 'lua',
  '.m': 'objective-c',
  '.markdown': 'markdown',
  '.md': 'markdown',
  '.mmd': 'markdown',
  '.mm': 'objective-c',
  '.php': 'php',
  '.pl': 'perl',
  '.proto': 'protobuf',
  '.ps1': 'powershell',
  '.py': 'python',
  '.r': 'r',
  '.rb': 'ruby',
  '.rs': 'rust',
  '.scss': 'scss',
  '.sh': 'shell',
  '.sql': 'sql',
  '.swift': 'swift',
  '.toml': 'toml',
  '.ts': 'typescript',
  '.tsx': 'typescript',
  '.txt': 'plaintext',
  '.xml': 'xml',
  '.yaml': 'yaml',
  '.yml': 'yaml',
  '.zsh': 'shell',
});

const LANGUAGE_BY_FILENAME = Object.freeze({
  'cmakelists.txt': 'cmake',
  'containerfile': 'dockerfile',
  'dockerfile': 'dockerfile',
  'gemfile': 'ruby',
  'makefile': 'makefile',
  'procfile': 'shell',
});

function languageFromPath(value) {
  const normalized = String(value || '').replaceAll('\\', '/');
  const filename = (normalized.split('/').at(-1) || '').toLowerCase();
  if (LANGUAGE_BY_FILENAME[filename]) return LANGUAGE_BY_FILENAME[filename];
  if (filename.startsWith('dockerfile.')) return 'dockerfile';
  if (filename.startsWith('.env.')) return 'ini';
  const extensionIndex = filename.lastIndexOf('.');
  const extension = extensionIndex > 0 ? filename.slice(extensionIndex) : '';
  return LANGUAGE_BY_EXTENSION[extension] || 'plaintext';
}

function stableHash(value) {
  // FNV-1a keeps connection identities out of model URIs while preserving a
  // deterministic namespace for detached tabs from different SSH targets.
  let hash = 0x811c9dc5;
  for (const character of String(value || '')) {
    hash ^= character.codePointAt(0);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(36);
}

function encodePath(value) {
  const segments = String(value || 'untitled')
    .replaceAll('\\', '/')
    .split('/')
    .filter(Boolean)
    .map(segment => encodeURIComponent(segment).replaceAll('.', '%2E'));
  return segments.join('/') || 'untitled';
}

function modelUriFor({ id, identity, path: remotePath } = {}) {
  const modelId = encodeURIComponent(String(id || 'untitled'));
  return `remote-workspace://${stableHash(identity)}/${modelId}/${encodePath(remotePath)}`;
}

function monacoThemeFor(value) {
  return String(value || '').toLowerCase() === 'dark' ? 'vs-dark' : 'vs';
}

module.exports = { languageFromPath, modelUriFor, monacoThemeFor, stableHash };
