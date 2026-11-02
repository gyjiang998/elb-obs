'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const {
  MAX_GENERATED_HTML_REPORT_BYTES,
  normalizeGeneratedHtmlReportFilename,
  forceGeneratedHtmlReportExtension,
  normalizeGeneratedHtmlReportRequest,
  writeGeneratedHtmlReport,
} = require('../runtime/generated-html-report.cjs');

function fixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-html-report-'));
  t.after(() => fs.rmSync(directory, {recursive: true, force: true}));
  return directory;
}

test('generated HTML report request is bounded and its suggested name is portable', () => {
  assert.equal(normalizeGeneratedHtmlReportFilename('../bad\\CON:<report>?.htm'), 'CON report.html');
  assert.equal(normalizeGeneratedHtmlReportFilename('CON.html'), '_CON.html');
  assert.equal(normalizeGeneratedHtmlReportFilename(''), 'parallel-modeling-report.html');
  const normalized = normalizeGeneratedHtmlReportRequest({html: '<!doctype html><p>报告</p>', filename: '建模 / 结果.HTML'});
  assert.equal(normalized.filename, '结果.html');
  assert.equal(normalized.size, Buffer.byteLength(normalized.html));
  assert.match(normalized.sha256, /^[0-9a-f]{64}$/);
  assert.throws(() => normalizeGeneratedHtmlReportRequest({html: Buffer.alloc(1)}), /必须是字符串/);
  assert.throws(() => normalizeGeneratedHtmlReportRequest({html: ''}), /不能为空/);
  assert.throws(() => normalizeGeneratedHtmlReportRequest({html: 'x'.repeat(MAX_GENERATED_HTML_REPORT_BYTES + 1)}), /超过 16 MiB/);
});

test('generated HTML report writes a private sibling and atomically replaces an approved target', async t => {
  const directory = fixture(t);
  const selected = path.join(directory, 'parallel-result.txt');
  const destination = forceGeneratedHtmlReportExtension(selected);
  fs.writeFileSync(destination, 'old report');
  const html = '<!doctype html><html lang="zh-CN"><body>建模结果</body></html>';
  const result = await writeGeneratedHtmlReport({html, destinationPath: selected});
  assert.deepEqual(result, {
    canceled: false,
    size: Buffer.byteLength(html),
    sha256: require('node:crypto').createHash('sha256').update(html).digest('hex'),
  });
  assert.equal(fs.readFileSync(destination, 'utf8'), html);
  assert.equal(Object.hasOwn(result, 'path'), false);
  assert.deepEqual(fs.readdirSync(directory).filter(name => name.includes('.hetero-report-')), []);
});

test('failed generated report commit preserves the target and cleans its partial file', async t => {
  const directory = fixture(t);
  const destination = path.join(directory, 'report.html');
  fs.writeFileSync(destination, 'approved original');
  await assert.rejects(writeGeneratedHtmlReport({html: '<p>new</p>', destinationPath: destination}, {
    replaceFile: async () => { throw new Error('simulated commit failure'); },
    randomUUID: () => 'failed-commit',
  }), /simulated commit failure/);
  assert.equal(fs.readFileSync(destination, 'utf8'), 'approved original');
  assert.deepEqual(fs.readdirSync(directory).filter(name => name.includes('.hetero-report-')), []);
});

test('Desktop generated report IPC uses a native HTML Save dialog and returns no local path', () => {
  const source = fs.readFileSync(path.resolve(__dirname, '../main.cjs'), 'utf8');
  const start = source.indexOf("if (method === 'saveGeneratedHtmlReport')");
  const end = source.indexOf("if (method === 'downloadRemoteFile')", start);
  assert.ok(start >= 0 && end > start);
  const branch = source.slice(start, end);
  assert.match(branch, /normalizeGeneratedHtmlReportRequest\(payload\)/);
  assert.match(branch, /dialog\.showSaveDialog/);
  assert.match(branch, /app\.getPath\('downloads'\)/);
  assert.match(branch, /extensions: \['html'\]/);
  assert.match(branch, /if \(selection\.canceled \|\| !selection\.filePath\) return \{canceled: true\}/);
  assert.match(branch, /writeGeneratedHtmlReport/);
  assert.doesNotMatch(branch, /return \{[^}]*path:/);
});
