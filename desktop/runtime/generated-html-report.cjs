'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const path = require('node:path');
const {
  captureLocalDestinationFingerprint,
  replaceDownloadedFile,
} = require('./index.cjs');

const MAX_GENERATED_HTML_REPORT_BYTES = 16 * 1024 * 1024;
const DEFAULT_GENERATED_HTML_REPORT_FILENAME = 'parallel-modeling-report.html';
const MAX_REPORT_STEM_BYTES = 180;
const WINDOWS_RESERVED_BASENAME = /^(?:con|prn|aux|nul|clock\$|com[1-9]|lpt[1-9])$/i;

function trimUtf8(value, maximumBytes) {
  let result = '';
  for (const character of value) {
    if (Buffer.byteLength(result + character, 'utf8') > maximumBytes) break;
    result += character;
  }
  return result;
}

/**
 * A renderer-provided name is only a suggestion for the native Save dialog.
 * Strip path components and characters which are invalid on Windows so the
 * same report request behaves consistently on every supported desktop OS.
 */
function normalizeGeneratedHtmlReportFilename(value) {
  const candidate = typeof value === 'string' ? value : '';
  const leaf = candidate.replace(/\\/g, '/').split('/').pop() || '';
  let stem = leaf.replace(/\.html?$/i, '').replace(/[\u0000-\u001f\u007f<>:"/\\|?*]/g, ' ');
  stem = stem.replace(/\s+/g, ' ').replace(/^[ .]+|[ .]+$/g, '');
  stem = trimUtf8(stem, MAX_REPORT_STEM_BYTES).replace(/[ .]+$/g, '');
  if (!stem || stem === '.' || stem === '..') stem = DEFAULT_GENERATED_HTML_REPORT_FILENAME.slice(0, -5);
  if (WINDOWS_RESERVED_BASENAME.test(stem)) stem = `_${stem}`;
  return `${stem}.html`;
}

/** Force the user-confirmed destination to the report's only supported type. */
function forceGeneratedHtmlReportExtension(destinationPath) {
  if (typeof destinationPath !== 'string' || !destinationPath.trim())
    throw new TypeError('HTML 报告保存路径无效。');
  const absolute = path.resolve(destinationPath);
  const parsed = path.parse(absolute);
  const stem = parsed.ext ? parsed.name : parsed.base;
  if (!stem || stem === '.' || stem === '..') throw new TypeError('HTML 报告保存路径无效。');
  return path.join(parsed.dir, `${stem}.html`);
}

function normalizeGeneratedHtmlReportRequest(payload) {
  if (!payload || Array.isArray(payload) || typeof payload !== 'object')
    throw new TypeError('HTML 报告请求无效。');
  if (typeof payload.html !== 'string') throw new TypeError('HTML 报告内容必须是字符串。');
  const size = Buffer.byteLength(payload.html, 'utf8');
  if (!size) throw new TypeError('HTML 报告内容不能为空。');
  if (size > MAX_GENERATED_HTML_REPORT_BYTES)
    throw new RangeError('HTML 报告超过 16 MiB，无法保存。');
  return {
    html: payload.html,
    filename: normalizeGeneratedHtmlReportFilename(payload.filename),
    size,
    sha256: crypto.createHash('sha256').update(payload.html, 'utf8').digest('hex'),
  };
}

/**
 * Write a bounded generated report through a private same-directory temporary
 * file, then publish it with the local destination fingerprint protocol used
 * by remote downloads. The destination path never crosses back to renderer.
 */
async function writeGeneratedHtmlReport({
  html,
  destinationPath,
  size,
  sha256,
}, {
  fileSystem = fs,
  randomUUID = crypto.randomUUID,
  captureFingerprint = captureLocalDestinationFingerprint,
  replaceFile = replaceDownloadedFile,
} = {}) {
  const normalized = normalizeGeneratedHtmlReportRequest({html});
  if (size !== undefined && size !== normalized.size) throw new Error('HTML 报告大小校验失败。');
  if (sha256 !== undefined && sha256 !== normalized.sha256) throw new Error('HTML 报告摘要校验失败。');

  const destination = forceGeneratedHtmlReportExtension(destinationPath);
  const expectedDestinationFingerprint = await captureFingerprint(destination, {fileSystem});
  const parent = path.dirname(destination);
  const filename = path.basename(destination);
  const temporary = path.join(parent, `.${filename}.hetero-report-${randomUUID()}.part`);
  let handle = null;
  let committed = false;
  try {
    handle = await fileSystem.open(temporary, 'wx', 0o600);
    await handle.writeFile(html, 'utf8');
    await handle.sync();
    await handle.close();
    handle = null;
    await replaceFile(temporary, destination, {
      fileSystem,
      randomUUID,
      expectedDestinationFingerprint,
    });
    committed = true;
    return {canceled: false, size: normalized.size, sha256: normalized.sha256};
  } finally {
    if (handle) await handle.close().catch(() => {});
    if (!committed) await fileSystem.unlink(temporary).catch(error => {
      if (error?.code !== 'ENOENT') console.error('Generated report temporary cleanup:', error.message);
    });
  }
}

module.exports = {
  MAX_GENERATED_HTML_REPORT_BYTES,
  DEFAULT_GENERATED_HTML_REPORT_FILENAME,
  normalizeGeneratedHtmlReportFilename,
  forceGeneratedHtmlReportExtension,
  normalizeGeneratedHtmlReportRequest,
  writeGeneratedHtmlReport,
};
