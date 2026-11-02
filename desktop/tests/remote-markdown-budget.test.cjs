'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
  AUTO_PREVIEW_MAX_BYTES,
  RENDER_PREVIEW_MAX_BYTES,
  MERMAID_MAX_BYTES,
  MERMAID_MAX_NODES,
  MERMAID_MAX_RENDERED_DIAGRAMS,
  prepareRemoteMarkdownPreview,
} = require('../renderer/remote-markdown-budget.cjs');

test('large remote Markdown requires an explicit preview and is bounded before React parsing', () => {
  const source = `# remote\n${'untrusted content\n'.repeat(Math.ceil((RENDER_PREVIEW_MAX_BYTES * 2) / 18))}`;
  const prepared = prepareRemoteMarkdownPreview(source);
  assert.equal(prepared.autoPreview, false);
  assert.ok(prepared.originalBytes > AUTO_PREVIEW_MAX_BYTES);
  assert.equal(prepared.truncated, true);
  assert.match(prepared.source, /预览已按安全预算截断/);
  assert.ok(new TextEncoder().encode(prepared.source).length < RENDER_PREVIEW_MAX_BYTES + 1024,
    'the preview parser receives only a bounded prefix plus a short notice');
});

test('oversized and high-node Mermaid blocks degrade to inert code while small diagrams render', () => {
  const highNode = Array.from({length: MERMAID_MAX_NODES + 2}, (_, index) => `N${index}[node ${index}]`).join('\n');
  const oversized = `graph TD\nA[${'x'.repeat(MERMAID_MAX_BYTES)}]`;
  const prepared = prepareRemoteMarkdownPreview([
    '# diagrams',
    '```mermaid', 'graph TD', 'A --> B', '```',
    '```mermaid', highNode, '```',
    '```mermaid optional-directive', oversized, '```',
  ].join('\n'));
  assert.equal(prepared.renderedDiagrams, 1);
  assert.equal(prepared.degradedDiagrams, 2);
  assert.equal((prepared.source.match(/```mermaid/g) || []).length, 1);
  assert.equal((prepared.source.match(/```text/g) || []).length, 2);
  assert.match(prepared.source, /节点数量超出预算/);
  assert.match(prepared.source, /图源码过长/);
});

test('a Markdown document renders only a bounded number of Mermaid diagrams', () => {
  const source = Array.from({length: MERMAID_MAX_RENDERED_DIAGRAMS + 3}, (_, index) => [
    '```mermaid', 'graph TD', `A${index} --> B${index}`, '```',
  ].join('\n')).join('\n');
  const prepared = prepareRemoteMarkdownPreview(source);
  assert.equal(prepared.renderedDiagrams, MERMAID_MAX_RENDERED_DIAGRAMS);
  assert.equal(prepared.degradedDiagrams, 3);
  assert.match(prepared.source, /单页图数量超出预算/);
});
