import React from 'react';
import { createRoot } from 'react-dom/client';
import { MarkdownReport } from '../../app/components/MarkdownReport';
import remoteMarkdownBudget from './remote-markdown-budget.cjs';

const {prepareRemoteMarkdownPreview} = remoteMarkdownBudget;

/**
 * Mount the same safe Markdown/Mermaid renderer used by the web workbench.
 * MarkdownReport builds React text nodes instead of injecting Markdown HTML;
 * links are protocol-whitelisted and Mermaid runs with securityLevel=strict.
 */
export function createRemoteMarkdownPreview(host) {
  if (!host) throw new Error('Remote Markdown preview host is missing');
  const root = createRoot(host);
  let lastSource = null;
  return {
    policy(source) {
      return prepareRemoteMarkdownPreview(String(source ?? ''));
    },
    render(source) {
      const next = String(source ?? '');
      if (next === lastSource) return;
      lastSource = next;
      const prepared = prepareRemoteMarkdownPreview(next);
      root.render(<MarkdownReport source={prepared.source} />);
      return prepared;
    },
    clear() {
      lastSource = null;
      root.render(null);
    },
    dispose() { root.unmount(); },
  };
}
