const path = require('node:path');
const MIN_SUBMIT_DELAY_MS = 100;
const DEFAULT_SUBMIT_DELAY_MS = 1000;

function submitDelay(value) {
  if (value === undefined || value === null || value === '') return DEFAULT_SUBMIT_DELAY_MS;
  const parsed = Number(value);
  return Number.isInteger(parsed)
    ? Math.max(MIN_SUBMIT_DELAY_MS, Math.min(5000, parsed))
    : MIN_SUBMIT_DELAY_MS;
}

function normalizeManifest(document, baseDir) {
  if (!document || typeof document !== 'object') throw new Error('Agent 配置必须是 JSON 对象。');
  const entries = Array.isArray(document) ? document : document.adapters || document.agents;
  const candidates = entries || [document];
  if (!Array.isArray(candidates) || candidates.length === 0) throw new Error('配置没有 Agent 条目。');
  const selected = candidates.find((item) => item.adapter_id === document.default_adapter) || candidates[0];
  if (!selected || typeof selected.executable !== 'string' || !selected.executable.trim()) throw new Error('Agent 配置缺少 executable。');
  if (selected.args && (!Array.isArray(selected.args) || selected.args.some((item) => typeof item !== 'string'))) throw new Error('args 必须是字符串数组。');
  // CLI argv stays argv. A Linux path is intentionally retained for the user to replace on Windows.
  return {
    executable: selected.executable,
    args: selected.args || [],
    cwd: selected.cwd ? path.resolve(baseDir, selected.cwd) : '',
    environment: selected.environment || {},
    submitMode: selected.submitMode || selected.submit_mode || 'bracketed_paste_cr',
    submitDelayMs: submitDelay(selected.submitDelayMs ?? selected.submit_delay_ms),
    startupDelayMs: selected.startupDelayMs ?? selected.startup_input_delay_ms ?? 600,
    importedName: selected.display_name || selected.adapter_id || '',
    importNote: candidates.length > 1 ? `配置包含 ${candidates.length} 个 Agent，已载入 ${selected.display_name || selected.adapter_id || '第一项'}，可在界面修改。` : '',
  };
}
module.exports = { normalizeManifest };
