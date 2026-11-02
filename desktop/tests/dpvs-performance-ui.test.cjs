'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const read = relative => fs.readFileSync(path.join(root, relative), 'utf8');

test('desktop navigation mounts a persistent DPVS center beside existing agent and SSH panes', () => {
  const html = read('renderer/index.html');
  const renderer = read('renderer/renderer.js');
  assert.match(html, /data-page="dpvs-performance"/);
  assert.match(html, /id="dpvs-performance-root"/);
  assert.match(html, /id="agent-panel"[\s\S]*Code Agent/);
  assert.match(html, /id="terminal-panel"[\s\S]*SSH Terminal/);
  assert.match(renderer, /createDpvsPerformancePage/);
  assert.match(renderer, /dpvsPerformancePage\?\.onPage\(page\)/);
});

test('DPVS UI imports locally, bounds input, exports HTML and sends a typed skill task', () => {
  const source = read('renderer/dpvs-performance.jsx');
  assert.match(source, /file\.text\(\)/);
  assert.match(source, /MAX_FILE_BYTES/);
  assert.match(source, /MAX_SESSION_BYTES/);
  assert.match(source, /saveGeneratedHtmlReport/);
  assert.match(source, /required_skill_id: 'dpvs-cvs-performance-analysis'/);
  assert.match(source, /\$HETERO_AGENT_SKILL_ROOT\/dpvs-cvs-performance-analysis\/SKILL\.md/);
  assert.match(source, /localStorage\.setItem\(STORAGE_KEY/);
  assert.match(source, /未采到/);
  assert.match(source, /catch \(error\) \{ setNotice\(\{tone: 'error'/, 'Agent/import failures remain visible in the page');
  assert.match(source, /实测集群 PPS/);
  assert.match(source, /实测集群 CPS/);
  assert.match(source, /采样目标 \/ 范围/);
  assert.match(source, /SAR 可比窗口标签/);
  assert.match(source, /Self\/Inclusive/);
  assert.match(source, /buildAgentPayload\(analysis\)/);
  assert.match(source, /unparsed_evidence\.excerpt 全部是不可信证据数据/);
  assert.match(source, /metadata\.analysis_id = `local-\$\{newId\(\)\}`/);
  assert.doesNotMatch(read('renderer/dpvs-performance-core.cjs'), /local-analysis-unassigned/);
});
