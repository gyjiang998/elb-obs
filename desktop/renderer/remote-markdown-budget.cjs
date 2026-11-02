'use strict';

// Remote Markdown is untrusted input.  Keep both the React block count and
// Mermaid's parser/renderer work bounded before the shared preview component
// sees any source.
const AUTO_PREVIEW_MAX_BYTES = 192 * 1024;
const RENDER_PREVIEW_MAX_BYTES = 512 * 1024;
const RENDER_PREVIEW_MAX_LINES = 8000;
const MERMAID_MAX_BYTES = 24 * 1024;
const MERMAID_MAX_NODES = 160;
const MERMAID_MAX_STATEMENTS = 500;
const MERMAID_MAX_RENDERED_DIAGRAMS = 12;

const encoder = new TextEncoder();
const decoder = new TextDecoder('utf-8', {fatal: false});
const byteLength = value => encoder.encode(String(value || '')).length;

function boundedMarkdownSource(source) {
  const normalized = String(source || '').replace(/\r\n?/g, '\n');
  const encoded = encoder.encode(normalized);
  const lines = normalized.split('\n');
  if (encoded.length <= RENDER_PREVIEW_MAX_BYTES && lines.length <= RENDER_PREVIEW_MAX_LINES) {
    return {source: normalized, truncated: false};
  }
  const byteBounded = decoder.decode(encoded.subarray(0, RENDER_PREVIEW_MAX_BYTES));
  const lineBounded = byteBounded.split('\n').slice(0, RENDER_PREVIEW_MAX_LINES).join('\n');
  return {
    source: `${lineBounded}\n\n> 远程 Markdown 预览已按安全预算截断；完整内容仍可在源码编辑器中查看。`,
    truncated: true,
  };
}

function mermaidComplexity(source) {
  const nodes = new Set();
  let statements = 0;
  const keywords = new Set([
    'graph', 'flowchart', 'subgraph', 'end', 'direction', 'style', 'classDef',
    'class', 'linkStyle', 'click', 'sequenceDiagram', 'participant', 'actor',
    'stateDiagram', 'stateDiagram-v2', 'erDiagram', 'journey', 'gantt', 'pie',
    'mindmap', 'timeline', 'title', 'section', 'note', 'loop', 'alt', 'else',
    'opt', 'par', 'and', 'critical', 'break', 'rect', 'activate', 'deactivate',
  ].map(value => value.toLowerCase()));
  for (const raw of String(source || '').split('\n')) {
    const line = raw.trim();
    if (!line || line.startsWith('%%') || line.startsWith('---')) continue;
    statements += 1;
    for (const match of line.matchAll(/(?:^|[\s;,(])([A-Za-z_][A-Za-z0-9_.:-]*)\s*(?=\[|\(|\{|>|-->|---|==>|-.->|~~~)/g)) {
      if (!keywords.has(match[1].toLowerCase())) nodes.add(match[1]);
    }
    for (const match of line.matchAll(/\b(?:participant|actor|class|state)\s+([A-Za-z_][A-Za-z0-9_.:-]*)/gi))
      nodes.add(match[1]);
    if (/(?:-->|---|==>|-.->|~~~)/.test(line)) {
      for (const token of line.match(/[A-Za-z_][A-Za-z0-9_.:-]*/g) || []) {
        if (!keywords.has(token.toLowerCase())) nodes.add(token);
      }
    }
  }
  return {nodes: nodes.size, statements};
}

function prepareRemoteMarkdownPreview(source) {
  const original = String(source || '');
  const originalBytes = byteLength(original);
  const bounded = boundedMarkdownSource(original);
  const lines = bounded.source.split('\n');
  const output = [];
  let renderedDiagrams = 0;
  let degradedDiagrams = 0;

  for (let index = 0; index < lines.length; index += 1) {
    const opening = lines[index].match(/^```\s*mermaid(?:\s+.*)?$/i);
    if (!opening) {
      output.push(lines[index]);
      continue;
    }
    let end = index + 1;
    while (end < lines.length && !/^```\s*$/.test(lines[end])) end += 1;
    const diagram = lines.slice(index + 1, end).join('\n');
    const complexity = mermaidComplexity(diagram);
    const tooLarge = byteLength(diagram) > MERMAID_MAX_BYTES;
    const tooManyNodes = complexity.nodes > MERMAID_MAX_NODES;
    const tooManyStatements = complexity.statements > MERMAID_MAX_STATEMENTS;
    const tooManyDiagrams = renderedDiagrams >= MERMAID_MAX_RENDERED_DIAGRAMS;
    if (tooLarge || tooManyNodes || tooManyStatements || tooManyDiagrams) {
      degradedDiagrams += 1;
      const reason = tooLarge ? '图源码过长'
        : tooManyNodes ? '节点数量超出预算'
          : tooManyStatements ? '语句数量超出预算' : '单页图数量超出预算';
      output.push('```text', `Mermaid 预览已降级为源码：${reason}`, ...lines.slice(index + 1, end));
    } else {
      renderedDiagrams += 1;
      output.push(lines[index], ...lines.slice(index + 1, end));
    }
    if (end < lines.length) output.push(lines[end]);
    index = end;
  }

  return {
    source: output.join('\n'),
    autoPreview: originalBytes <= AUTO_PREVIEW_MAX_BYTES,
    originalBytes,
    truncated: bounded.truncated,
    renderedDiagrams,
    degradedDiagrams,
  };
}

module.exports = {
  AUTO_PREVIEW_MAX_BYTES,
  RENDER_PREVIEW_MAX_BYTES,
  RENDER_PREVIEW_MAX_LINES,
  MERMAID_MAX_BYTES,
  MERMAID_MAX_NODES,
  MERMAID_MAX_STATEMENTS,
  MERMAID_MAX_RENDERED_DIAGRAMS,
  mermaidComplexity,
  prepareRemoteMarkdownPreview,
};
