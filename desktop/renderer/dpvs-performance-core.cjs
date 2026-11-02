'use strict';

const SCHEMA_VERSION = 'dpvs-performance-analysis/v1';
const MAX_FILE_BYTES = 8 * 1024 * 1024;
const MAX_SESSION_BYTES = 32 * 1024 * 1024;
const MAX_EVIDENCE_FILES = 128;
const MAX_PROFILE_SYMBOLS = 5000;
const MAX_UNPARSED_EXCERPT_BYTES = 16 * 1024;

const number = value => {
  const source = String(value ?? '').trim();
  if (!source || source.includes(',')) return null;
  const parsed = Number(source);
  return Number.isFinite(parsed) ? parsed : null;
};

const countNumber = value => {
  const parsed = Number(String(value ?? '').replace(/,/g, ''));
  return Number.isFinite(parsed) ? parsed : null;
};

function sarNumber(value) {
  const source = String(value ?? '').trim();
  if (/^[+-]?\d{1,3}(?:,\d{3}){2,}(?:\.\d+)?$/.test(source)) return Number(source.replace(/,/g, ''));
  return number(source);
}

const mean = values => {
  const valid = values.filter(Number.isFinite);
  return valid.length ? valid.reduce((sum, value) => sum + value, 0) / valid.length : null;
};

function round(value, digits = 4) {
  if (!Number.isFinite(value)) return null;
  const scale = 10 ** digits;
  return Math.round(value * scale) / scale;
}

function normalizeEvent(value) {
  return String(value || 'unknown')
    .trim().toLowerCase().replace(/^perf[._ -]*/, '').replace(/[ _]+/g, '-').replace(/:+[ukhgp]+$/i, '') || 'unknown';
}

function inferEvent(filename = '', content = '') {
  const header = String(content).match(/(?:of\s+event|event(?:\s+name)?)\s*[:=]?\s*['"]?([^'"\n#]+)/i);
  if (header) return normalizeEvent(header[1].trim().split(/\s{2,}/)[0]);
  const source = String(filename).toLowerCase();
  const known = ['cache-misses', 'cache-references', 'branch-misses', 'branch-instructions', 'instructions', 'cycles', 'cpu-clock', 'task-clock', 'llc-load-misses', 'l1-dcache-load-misses'];
  return known.find(event => source.includes(event)) || 'unknown';
}

function parseScaledNumber(value) {
  const match = String(value || '').trim().replace(/,/g, '').match(/^([0-9]+(?:\.[0-9]+)?)\s*([kmg])?$/i);
  if (!match) return null;
  const scale = {k: 1e3, m: 1e6, g: 1e9}[String(match[2] || '').toLowerCase()] || 1;
  return Number(match[1]) * scale;
}

function decodeXml(value) {
  const entities = {amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", '#39': "'"};
  return String(value || '').replace(/&(#x[0-9a-f]+|#[0-9]+|amp|lt|gt|quot|apos|#39);/gi, (whole, entity) => {
    if (entity[0] === '#') {
      const radix = entity[1].toLowerCase() === 'x' ? 16 : 10;
      const code = Number.parseInt(entity.slice(radix === 16 ? 2 : 1), radix);
      return Number.isFinite(code) ? String.fromCodePoint(code) : whole;
    }
    return entities[entity.toLowerCase()] || whole;
  });
}

function cleanSymbol(value) {
  return decodeXml(String(value || '')).replace(/\s+/g, ' ').trim();
}

function inspectText(content) {
  const text = String(content ?? '');
  if (text.includes('\0')) return {ok: false, reason: '检测到 NUL 字节，文件像是二进制数据。sar 的 saXX 文件请先用 sadf 或 sar -f 导出为文本。'};
  const controls = (text.match(/[\x01-\x08\x0B\x0C\x0E-\x1F]/g) || []).length;
  if (text.length && controls / text.length > 0.01)
    return {ok: false, reason: '控制字符比例过高，无法作为文本证据解析。二进制 perf.data/saXX 需要先在采集机导出。'};
  return {ok: true};
}

function detectArtifactKind(filename, content, hint = 'auto') {
  if (hint && hint !== 'auto') return hint;
  const name = String(filename || '').toLowerCase();
  const sample = String(content || '').slice(0, 300000);
  if (/\bnuma_(?:hit|miss|foreign)\b|per-node process memory usage|\bother_node\b|\blocal_node\b/i.test(sample)) return 'numastat';
  if (/\bN\d+=\d+/.test(sample) && /\b(?:default|interleave|bind|preferred)(?::\S+)?\b/.test(sample)) return 'numa_maps';
  if (/<svg\b/i.test(sample) && /<title>/i.test(sample)) return 'perf';
  if (/^\s*[^#\n;]+(?:;[^\n;]+)+\s+\d+(?:\.\d+)?\s*$/m.test(sample)) return 'perf';
  if (/^\s*#?\s*(?:Samples:|Overhead\s+|Children\s+Self\s+)/mi.test(sample) || /\[[.kKu]\]\s+\S+/m.test(sample)) return 'perf';
  if (/%(?:user|usr|system|sys|idle|iowait)|\bkbmemfree\b|\bIFACE\b|\brxpck\/s\b|\bDEV\b.*\bawait\b/mi.test(sample)) return 'sar';
  if (/numa[_-]?maps/.test(name)) return 'numa_maps';
  if (/numastat|numa/.test(name)) return 'numastat';
  if (/flame|folded|perf|cycles|cache-miss/.test(name)) return 'perf';
  if (/sar|sadf/.test(name)) return 'sar';
  return 'unknown';
}

function profileFromMap({format, event, basis, totalSamples, symbols, warnings = [], sourceHasAbsolute = true}) {
  const rows = [...symbols.values()]
    .filter(row => row.symbol && row.symbol !== 'all')
    .sort((left, right) => (right.samples ?? -1) - (left.samples ?? -1) || (right.percent ?? -1) - (left.percent ?? -1))
    .slice(0, MAX_PROFILE_SYMBOLS)
    .map(row => ({...row, samples: round(row.samples, 3), self_samples: round(row.self_samples, 3), percent: round(row.percent, 5)}));
  if (symbols.size > MAX_PROFILE_SYMBOLS) warnings.push(`函数数超过 ${MAX_PROFILE_SYMBOLS}，仅保留样本最多的 ${MAX_PROFILE_SYMBOLS} 项。`);
  return {kind: 'perf', format, basis, event: normalizeEvent(event), total_samples: round(totalSamples, 3), source_has_absolute: sourceHasAbsolute, symbols: rows, warnings};
}

function parseFlamegraphSvg(content, filename) {
  const titles = [...String(content).matchAll(/<title>([\s\S]*?)<\/title>/gi)].map(match => cleanSymbol(match[1]));
  const parsed = [];
  for (const title of titles) {
    const match = title.match(/^(.*?)\s+\(([0-9][0-9,]*(?:\.[0-9]+)?)\s+(?:samples?|counts?)(?:,\s*([0-9.]+)%)?\)\s*$/i)
      || title.match(/^(.*?)\s+\(([0-9][0-9,]*(?:\.[0-9]+)?)(?:,\s*([0-9.]+)%)?\)\s*$/);
    if (!match) continue;
    parsed.push({symbol: cleanSymbol(match[1]), samples: countNumber(match[2]), declaredPercent: number(match[3])});
  }
  if (!parsed.length) throw new Error('SVG 中没有识别到 FlameGraph 的“函数 (samples, %)”标题。');
  const total = Math.max(...parsed.map(row => row.samples || 0));
  const symbols = new Map();
  for (const row of parsed) {
    if (!row.symbol || /^(all|root|total)$/i.test(row.symbol)) continue;
    const current = symbols.get(row.symbol) || {symbol: row.symbol, samples: 0, self_samples: null, percent: 0, absolute_source: 'svg_title'};
    current.samples += row.samples || 0;
    symbols.set(row.symbol, current);
  }
  for (const row of symbols.values()) row.percent = total > 0 ? row.samples / total * 100 : null;
  return profileFromMap({
    format: 'flamegraph-svg', basis: 'inclusive', event: inferEvent(filename, content), totalSamples: total, symbols,
    warnings: ['SVG 火焰图提供包含子调用的宽度，不能等同于函数自身 CPU 时间；同名递归帧可能使聚合占比超过 100%。'],
  });
}

function parseFoldedStacks(content, filename) {
  const symbols = new Map();
  let total = 0;
  let accepted = 0;
  for (const sourceLine of String(content).split(/\r?\n/)) {
    const line = sourceLine.trim();
    if (!line || line.startsWith('#')) continue;
    const match = line.match(/^(.*?)\s+([0-9]+(?:\.[0-9]+)?)$/);
    if (!match || !match[1].includes(';')) continue;
    const count = Number(match[2]);
    const stack = match[1].split(';').map(cleanSymbol).filter(Boolean);
    if (!stack.length || !Number.isFinite(count)) continue;
    total += count; accepted += 1;
    for (const symbol of new Set(stack)) {
      const current = symbols.get(symbol) || {symbol, samples: 0, self_samples: 0, percent: 0, absolute_source: 'folded_count'};
      current.samples += count;
      symbols.set(symbol, current);
    }
    const leaf = stack[stack.length - 1];
    symbols.get(leaf).self_samples += count;
  }
  if (!accepted) throw new Error('没有识别到“frame;frame count”格式的 folded stacks。');
  for (const row of symbols.values()) row.percent = total > 0 ? row.samples / total * 100 : null;
  return profileFromMap({format: 'perf-folded', basis: 'inclusive', event: inferEvent(filename, content), totalSamples: total, symbols,
    warnings: ['folded stacks 的 samples 是采样计数，不是纳秒；只有采样事件、频率和时长可比时，计数差才可用于绝对对比。']});
}

function parsePerfReport(content, filename) {
  const text = String(content);
  const sampleBlocks = [...text.matchAll(/^\s*#\s*Samples:\s*[^\n]*?\bof\s+event\s+['"]?([^'"\n#]+?)['"]?\s*$/gmi)];
  const explicitEvents = [...text.matchAll(/^\s*#\s*event(?:\s+name)?\s*[:=]\s*['"]?([^'"\n#]+?)['"]?\s*$/gmi)];
  const declaredEvents = new Set([...sampleBlocks, ...explicitEvents].map(match => normalizeEvent(match[1])));
  if (sampleBlocks.length > 1 || explicitEvents.length > 1 || declaredEvents.size > 1) {
    throw new Error(`检测到单个 perf report 包含 ${Math.max(sampleBlocks.length, explicitEvents.length, declaredEvents.size)} 个事件块（${[...declaredEvents].join('、') || '事件名未识别'}）；不同事件或重复采集的分母不能合并，请按事件和采集轮次分别导出文件。`);
  }
  const event = inferEvent(filename, text);
  const totalMatch = text.match(/#\s*Samples:\s*([0-9.,]+\s*[KMG]?)\s+of\s+event/i);
  const total = totalMatch ? parseScaledNumber(totalMatch[1].replace(/\s/g, '')) : null;
  const hasChildrenSelf = /\bChildren\s+Self\b/i.test(text);
  const hasSamplesColumn = text.split(/\r?\n/).some(line => /(?:Overhead|Children)[^\n]*\bSamples\b/i.test(line));
  const symbols = new Map();
  let rows = 0;
  for (const sourceLine of text.split(/\r?\n/)) {
    const line = sourceLine.trim();
    if (!/^\d+(?:\.\d+)?%/.test(line)) continue;
    const percentages = [...line.matchAll(/([0-9]+(?:\.[0-9]+)?)%/g)];
    if (!percentages.length) continue;
    const selected = percentages[hasChildrenSelf && percentages.length > 1 ? 1 : 0];
    const percent = Number(selected[1]);
    const afterPercentages = line.slice((percentages[percentages.length - 1].index || 0) + percentages[percentages.length - 1][0].length).trim();
    const samplesMatch = hasSamplesColumn ? afterPercentages.match(/^([0-9][0-9,]*(?:\.[0-9]+)?\s*[KMG]?)\s+/i) : null;
    const reportedSamples = samplesMatch ? parseScaledNumber(samplesMatch[1].replace(/[\s,]/g, '')) : null;
    const marker = line.match(/\[[.kKu]\]\s+(.+)$/);
    let symbol = marker ? marker[1] : line.slice((percentages[percentages.length - 1].index || 0) + percentages[percentages.length - 1][0].length).trim().split(/\s{2,}/).pop();
    symbol = cleanSymbol(symbol).replace(/\s+\(inlined\)$/i, '');
    if (!symbol || !Number.isFinite(percent)) continue;
    const current = symbols.get(symbol) || {symbol, samples: reportedSamples !== null ? 0 : null, self_samples: null, percent: 0,
      absolute_source: reportedSamples !== null ? 'report_samples_column' : null};
    current.percent += percent;
    if (reportedSamples !== null) current.samples += reportedSamples;
    symbols.set(symbol, current); rows += 1;
  }
  if (!rows) throw new Error('perf report 中没有识别到以百分比开头的函数行。');
  const warnings = hasSamplesColumn
    ? ['函数绝对样本优先取自 perf report 的 Samples 列。']
    : total
      ? ['perf report 仅提供事件总样本数和函数 Overhead；Overhead 可能按 period 加权，未用总数 × 百分比反推函数 samples。函数绝对样本保持为空。']
      : ['该 perf report 只有占比，没有函数样本计数；绝对样本差将保持为空。'];
  if (hasChildrenSelf) warnings.push('检测到 Children/Self 双列，当前按 Self 列比较。');
  return profileFromMap({format: 'perf-report', basis: 'self', event, totalSamples: total, symbols, warnings, sourceHasAbsolute: hasSamplesColumn});
}

function parsePerf(content, filename) {
  if (/<svg\b/i.test(content)) return parseFlamegraphSvg(content, filename);
  if (/^\s*[^#\n;]+(?:;[^\n;]+)+\s+\d+(?:\.\d+)?\s*$/m.test(content)) return parseFoldedStacks(content, filename);
  return parsePerfReport(content, filename);
}

const CPU_METRICS = new Set(['%user', '%usr', '%nice', '%system', '%sys', '%iowait', '%steal', '%irq', '%soft', '%guest', '%gnice', '%idle']);
function sarCategory(metric) {
  const key = String(metric || '').toLowerCase();
  if (CPU_METRICS.has(key)) return 'cpu';
  if (/^(?:rx|tx)|(?:rx|tx).+\/s$|ifutil|iface/.test(key)) return 'network';
  if (/^(?:tps|rtps|wtps|dtps|bread\/s|bwrtn\/s|rk?b\/s|wk?b\/s|dk?b\/s|areq-sz|aqu-sz|await|r_await|w_await|d_await|%util)$/.test(key)) return 'io';
  if (/mem|swap|commit|active|inact|dirty|anon|slab|avail|pgpg|fault|majflt|free|buffer|cache/i.test(key)) return 'memory';
  return '';
}

function sarUnit(metric) {
  const key = String(metric || '');
  if (key.startsWith('%') || /util$/i.test(key)) return '%';
  if (/kb\/s/i.test(key)) return 'kB/s';
  if (/mb\/s/i.test(key)) return 'MB/s';
  if (/\/s$/i.test(key) || /tps$/i.test(key)) return '/s';
  if (/^kb/i.test(key)) return 'kB';
  return '';
}

function finishSarSamples(samples, format, warnings = []) {
  const grouped = new Map();
  for (const sample of samples) {
    const key = `${sample.category}\0${sample.device || ''}\0${sample.metric}`;
    const group = grouped.get(key) || {...sample, all: [], averages: []};
    (sample.average ? group.averages : group.all).push(sample.value);
    grouped.set(key, group);
  }
  const metrics = [...grouped.values()].map(group => ({
    category: group.category, device: group.device || '', metric: group.metric, unit: sarUnit(group.metric),
    value: round(mean(group.averages.length ? group.averages : group.all), 5),
    sample_count: (group.averages.length ? group.averages : group.all).length,
    source: group.averages.length ? 'sar_average_row' : 'mean_of_rows',
  })).filter(metric => metric.value !== null).sort((a, b) => `${a.category}${a.device}${a.metric}`.localeCompare(`${b.category}${b.device}${b.metric}`));
  if (!metrics.length) throw new Error('没有识别到 sar CPU、内存、网络或 IO 指标。请导出带表头的 sar/sadf 文本。');
  return {kind: 'sar', format, metrics, warnings};
}

function parseSadfDelimited(content) {
  const lines = String(content).split(/\r?\n/).filter(line => line.trim());
  for (let headerIndex = 0; headerIndex < lines.length; headerIndex += 1) {
    const rawHeader = lines[headerIndex].replace(/^\s*#\s*/, '');
    const delimiter = rawHeader.includes(';') ? ';' : rawHeader.includes(',') ? ',' : null;
    if (!delimiter) continue;
    const headers = rawHeader.split(delimiter).map(value => value.trim());
    const metricIndexes = headers.map((metric, index) => ({metric, index, category: sarCategory(metric)})).filter(item => item.category);
    if (!metricIndexes.length) continue;
    const deviceIndex = headers.findIndex(header => /^(CPU|IFACE|DEV)$/i.test(header));
    const samples = [];
    for (let index = headerIndex + 1; index < lines.length; index += 1) {
      if (/^\s*#/.test(lines[index])) break;
      const values = lines[index].split(delimiter).map(value => value.trim());
      if (values.length < headers.length - 1) continue;
      let device = deviceIndex >= 0 ? values[deviceIndex] : '';
      if (/^CPU$/i.test(headers[deviceIndex]) && device === '-1') device = 'all';
      for (const item of metricIndexes) {
        const value = sarNumber(values[item.index]);
        if (value !== null) samples.push({category: item.category, metric: item.metric, device, value, average: false});
      }
    }
    if (samples.length) return finishSarSamples(samples, `sadf-${delimiter === ';' ? 'semicolon' : 'csv'}`);
  }
  return null;
}

function sarClockSeconds(line) {
  const match = String(line).match(/\b(\d{1,2}):(\d{2}):(\d{2})(?:\.\d+)?\s*(AM|PM)?\b/i);
  if (!match) return null;
  let hour = Number(match[1]);
  const marker = String(match[4] || '').toUpperCase();
  if (marker === 'PM' && hour < 12) hour += 12;
  if (marker === 'AM' && hour === 12) hour = 0;
  return hour * 3600 + Number(match[2]) * 60 + Number(match[3]);
}

function inspectSarWindow(content) {
  const text = String(content);
  const restartMarkers = [];
  const measurementLines = [];
  const sections = [];
  let current = null;
  const finish = () => { if (current?.length) sections.push(current); current = null; };
  for (const [lineIndex, raw] of text.split(/\r?\n/).entries()) {
    const line = raw.trim();
    if (!line) continue;
    if (/LINUX\s+RESTART/i.test(line)) {
      restartMarkers.push(lineIndex);
      finish();
      continue;
    }
    const delimiter = line.includes(';') ? ';' : line.includes(',') ? ',' : null;
    const tokens = delimiter ? line.replace(/^\s*#\s*/, '').split(delimiter).map(value => value.trim()) : line.split(/\s+/);
    const metricCount = tokens.filter(token => sarCategory(token)).length;
    const hasEntityHeader = tokens.some(token => /^(CPU|IFACE|DEV)$/i.test(token));
    if (metricCount >= 2 || (metricCount && hasEntityHeader)) { finish(); current = []; continue; }
    if (!current) continue;
    const seconds = sarClockSeconds(line);
    const isAverage = /^(?:Average|平均)/i.test(line);
    if (seconds !== null || isAverage) measurementLines.push(lineIndex);
    if (seconds !== null && current.at(-1) !== seconds) current.push(seconds);
  }
  finish();
  const firstMeasurement = measurementLines.length ? measurementLines[0] : null;
  const lastMeasurement = measurementLines.length ? measurementLines.at(-1) : null;
  const restartBoundaries = firstMeasurement === null ? 0
    : restartMarkers.filter(index => index > firstMeasurement && index < lastMeasurement).length;
  const intervals = [];
  for (const times of sections) {
    for (let index = 1; index < times.length; index += 1) {
      let delta = times[index] - times[index - 1];
      if (delta < 0) delta += 24 * 3600;
      if (delta > 0) intervals.push(round(delta, 6));
    }
  }
  const uniqueIntervals = [...new Set(intervals)];
  const variableIntervals = uniqueIntervals.length > 1;
  const status = restartBoundaries ? 'restart_boundary' : variableIntervals ? 'variable_intervals' : 'regular_or_average';
  return {status, comparable: !restartBoundaries && !variableIntervals, restart_boundaries: restartBoundaries,
    restart_markers: restartMarkers.length, ignored_restart_markers: restartMarkers.length - restartBoundaries,
    observed_intervals_seconds: uniqueIntervals, section_count: sections.length};
}

function attachSarWindow(parsed, window) {
  parsed.window = window;
  if (!window.comparable) parsed.metrics = parsed.metrics.map(metric => ({...metric,
    observed_value: metric.value, value: null, source: 'incomparable_source_window'}));
  if (window.restart_boundaries) parsed.warnings.push(`检测到 ${window.restart_boundaries} 个测量前后均有样本的 LINUX RESTART 边界；跨启动普通均值已置空，不参与场景比较。`);
  if (window.ignored_restart_markers) parsed.warnings.push(`另有 ${window.ignored_restart_markers} 个 LINUX RESTART 标记位于首个测量样本之前或末个样本之后，不视为跨启动采样边界。`);
  if (window.status === 'variable_intervals') parsed.warnings.push(`检测到可变 SAR 采样间隔（${window.observed_intervals_seconds.join('、')} 秒）；普通算术均值已置空，不参与场景比较。`);
  return parsed;
}

function parseSarText(content) {
  const sarText = String(content);
  const window = inspectSarWindow(sarText);
  const explicitCommaCsv = sarText.split(/\r?\n/).some(line => {
    if (!line.includes(',')) return false;
    const headers = line.replace(/^\s*#\s*/, '').split(',').map(value => value.trim());
    return headers.filter(header => sarCategory(header)).length >= 2;
  });
  const ambiguousComma = explicitCommaCsv ? null : sarText.split(/\r?\n/).flatMap(line => line.trim().split(/[\s;]+/)).find(token => {
    if (!token.includes(',')) return false;
    if (/^[+-]?\d{1,3}(?:,\d{3}){2,}(?:\.\d+)?$/.test(token)) return false;
    return /^[+-]?\d[\d,]*$/.test(token);
  });
  if (ambiguousComma) throw new Error(`SAR 数值“${ambiguousComma}”包含 locale 歧义逗号，无法判断是小数还是千分位。请用 LC_ALL=C sar 导出，或使用带明确列分隔的 sadf CSV。`);
  const delimited = parseSadfDelimited(sarText);
  if (delimited) return attachSarWindow(delimited, window);
  const lines = sarText.split(/\r?\n/);
  let section = null;
  const samples = [];
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) continue;
    const tokens = line.split(/\s+/);
    const labelIndex = tokens.findIndex(token => /^(CPU|IFACE|DEV)$/i.test(token));
    const metricTokens = tokens.map((metric, index) => ({metric, index, category: sarCategory(metric)})).filter(item => item.category);
    if (metricTokens.length >= 2 || (metricTokens.length && labelIndex >= 0)) {
      const label = labelIndex >= 0 ? tokens[labelIndex] : '';
      const metrics = metricTokens.filter(item => labelIndex < 0 || item.index > labelIndex);
      section = {label, metrics: metrics.map(item => ({metric: item.metric, category: item.category}))};
      continue;
    }
    if (!section || !section.metrics.length) continue;
    const average = /^(?:Average|平均)/i.test(line);
    let device = '';
    let values = [];
    if (section.label) {
      const metricCount = section.metrics.length;
      const deviceIndex = tokens.length - metricCount - 1;
      if (deviceIndex < 0 || deviceIndex >= tokens.length) continue;
      device = tokens[deviceIndex];
      if (section.label === 'CPU' && device === '-1') device = 'all';
      values = tokens.slice(deviceIndex + 1, deviceIndex + 1 + section.metrics.length);
    } else values = tokens.slice(-section.metrics.length);
    if (values.length < section.metrics.length) continue;
    section.metrics.forEach((item, index) => {
      const value = sarNumber(values[index]);
      if (value !== null) samples.push({...item, device, value, average});
    });
  }
  return attachSarWindow(finishSarSamples(samples, 'sar-text'), window);
}

function parseNumastat(content) {
  const text = String(content);
  const lines = text.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  const system = {};
  for (const line of lines) {
    const match = line.match(/^(numa_hit|numa_miss|numa_foreign|interleave_hit|local_node|other_node)\s+(.+)$/i);
    if (!match) continue;
    const values = match[2].trim().split(/\s+/).map(countNumber).filter(Number.isFinite);
    if (values.length) system[match[1].toLowerCase()] = {nodes: values, total: values.reduce((sum, value) => sum + value, 0)};
  }
  const processRows = {};
  const nodeHeader = lines.find(line => /Node\s+\d+/i.test(line) && /Total/i.test(line));
  if (nodeHeader) {
    const nodeCount = (nodeHeader.match(/Node\s+\d+/gi) || []).length;
    for (const line of lines) {
      const match = line.match(/^([A-Za-z][\w .-]*?)\s+((?:[0-9]+(?:\.[0-9]+)?\s+){1,}[0-9]+(?:\.[0-9]+)?)$/);
      if (!match) continue;
      const values = match[2].trim().split(/\s+/).map(number);
      if (values.length >= nodeCount + 1) processRows[match[1].trim()] = {nodes: values.slice(0, nodeCount), total: values[nodeCount], unit: 'MiB'};
    }
  }
  if (!Object.keys(system).length && !Object.keys(processRows).length) throw new Error('没有识别到 numastat 系统计数或 numastat -p 的进程内存表。');
  const warnings = [];
  if (Object.keys(system).length) warnings.push('numastat 系统计数通常是自启动以来的累计值；单次快照不能说明压测窗口内的增量。');
  if (Object.keys(processRows).length) warnings.push('numastat -p 只显示进程页面分布，不测量 CPU 到远端 NUMA 页的实际访问次数。');
  return {kind: 'numa', format: 'numastat', system, process_rows: processRows, warnings};
}

function parseNumaMaps(content) {
  const pages = {};
  const policies = {};
  let rows = 0;
  for (const line of String(content).split(/\r?\n/)) {
    if (!/\bN\d+=\d+/.test(line)) continue;
    rows += 1;
    for (const match of line.matchAll(/\bN(\d+)=(\d+)/g)) pages[`N${match[1]}`] = (pages[`N${match[1]}`] || 0) + Number(match[2]);
    const policy = line.match(/\b(default|interleave|bind|preferred)(?::\S+)?\b/);
    if (policy) policies[policy[1]] = (policies[policy[1]] || 0) + 1;
  }
  if (!rows) throw new Error('没有识别到包含 N0=… 的 /proc/PID/numa_maps 行。');
  return {kind: 'numa', format: 'numa_maps', pages, policies, mapped_rows: rows,
    warnings: ['numa_maps 显示虚拟内存页的放置和策略，不记录这些页由哪个 CPU 访问，也不能直接计算跨 NUMA 访问率。']};
}

function boundedExcerpt(content) {
  const bytes = new TextEncoder().encode(String(content));
  if (bytes.byteLength <= MAX_UNPARSED_EXCERPT_BYTES) return {text: new TextDecoder().decode(bytes), truncated: false, bytes: bytes.byteLength};
  const side = Math.floor((MAX_UNPARSED_EXCERPT_BYTES - 96) / 2);
  const head = new TextDecoder().decode(bytes.slice(0, side));
  const tail = new TextDecoder().decode(bytes.slice(bytes.byteLength - side));
  return {text: `${head}\n\n…[中间内容已裁剪，仅保留安全头尾摘录]…\n\n${tail}`, truncated: true, bytes: bytes.byteLength};
}

function parseEvidenceText({filename = 'pasted.txt', content, kind = 'auto'}) {
  const inspection = inspectText(content);
  if (!inspection.ok) throw new Error(inspection.reason);
  const detected = detectArtifactKind(filename, content, kind);
  let parsed;
  try {
    if (detected === 'perf') parsed = parsePerf(content, filename);
    else if (detected === 'sar') parsed = parseSarText(content);
    else if (detected === 'numastat') parsed = parseNumastat(content);
    else if (detected === 'numa_maps') parsed = parseNumaMaps(content);
    else throw new Error('无法识别确定性格式。支持 FlameGraph SVG、folded stacks、perf report、sar/sadf 文本、numastat 和 numa_maps。');
    return {detected_kind: detected, parsed};
  } catch (error) {
    const excerpt = boundedExcerpt(content);
    const parserError = String(error?.message || error);
    return {detected_kind: 'unparsed_text', parsed: {kind: 'unparsed_text', format: 'unparsed-text', candidate_kind: detected,
      excerpt: excerpt.text, excerpt_truncated: excerpt.truncated, source_bytes: excerpt.bytes, parser_error: parserError,
      warnings: [`确定性解析未成功：${parserError}`, '该文件作为有界文本候选保留给 Code Agent；覆盖矩阵不会把它算作已解析指标。']}};
  }
}

function perfArtifactMap(evidences, role) {
  const map = new Map();
  for (const evidence of evidences.filter(item => item.role === role && item.parsed?.kind === 'perf')) {
    const event = normalizeEvent(evidence.event_override || evidence.parsed.event);
    const group = map.get(event) || [];
    group.push(evidence);
    map.set(event, group);
  }
  return map;
}

function scopeDivisor(metadata, role) {
  const scenario = metadata?.[role] || {};
  return scenario.scope === 'cluster_total' ? Math.max(1, number(scenario.node_count) || (role === 'scaled' ? 16 : 1)) : 1;
}

function normalizeSamplingConfig(value) {
  return String(value || '').trim().toLowerCase().replace(/\s+/g, '');
}

function comparePerf(evidences, metadata) {
  const baseline = perfArtifactMap(evidences, 'baseline');
  const scaled = perfArtifactMap(evidences, 'scaled');
  const events = [...new Set([...baseline.keys(), ...scaled.keys()])].sort();
  return events.map(event => {
    const leftGroup = baseline.get(event) || [];
    const rightGroup = scaled.get(event) || [];
    const left = leftGroup[0] || null;
    const right = rightGroup[0] || null;
    const selectionWarnings = [];
    if (leftGroup.length > 1) selectionWarnings.push(`单节点场景的 ${event} 有 ${leftGroup.length} 份证据；当前只使用最早导入的“${left.filename}”，其余文件未合并：${leftGroup.slice(1).map(item => item.filename).join('、')}。请修改事件名以分别比较，或移除重复文件。`);
    if (rightGroup.length > 1) selectionWarnings.push(`16 节点场景的 ${event} 有 ${rightGroup.length} 份证据；当前只使用最早导入的“${right.filename}”，其余文件未合并：${rightGroup.slice(1).map(item => item.filename).join('、')}。请修改事件名以分别比较，或移除重复文件。`);
    if (!left || !right) return {event, status: 'unpaired', baseline_evidence_id: left?.id || null, scaled_evidence_id: right?.id || null, rows: [], warnings: [...selectionWarnings, `${event} 只在${left ? '单节点' : '16 节点'}场景出现，未把缺失场景当作 0。`]};
    const leftDuration = number(left.duration_seconds);
    const rightDuration = number(right.duration_seconds);
    const leftSamplingConfig = normalizeSamplingConfig(left.sampling_config);
    const rightSamplingConfig = normalizeSamplingConfig(right.sampling_config);
    const samplingConfigComparable = Boolean(leftSamplingConfig && rightSamplingConfig && leftSamplingConfig === rightSamplingConfig);
    const eventComparable = event !== 'unknown';
    const basisComparable = Boolean(left.parsed.basis && right.parsed.basis && left.parsed.basis === right.parsed.basis);
    const leftSamplingTarget = normalizeSamplingConfig(left.sampling_target);
    const rightSamplingTarget = normalizeSamplingConfig(right.sampling_target);
    const samplingTargetComparable = Boolean(leftSamplingTarget && rightSamplingTarget && leftSamplingTarget === rightSamplingTarget);
    const absoluteRateComparable = eventComparable && basisComparable && samplingConfigComparable && samplingTargetComparable;
    const distributionComparable = eventComparable && basisComparable && samplingTargetComparable;
    const leftDivisor = scopeDivisor(metadata, 'baseline');
    const rightDivisor = scopeDivisor(metadata, 'scaled');
    const leftRows = new Map(left.parsed.symbols.map(row => [row.symbol, row]));
    const rightRows = new Map(right.parsed.symbols.map(row => [row.symbol, row]));
    const symbols = [...new Set([...leftRows.keys(), ...rightRows.keys()])];
    const rows = symbols.map(symbol => {
      const before = leftRows.get(symbol) || null;
      const after = rightRows.get(symbol) || null;
      const baselineSamples = Number.isFinite(before?.samples) ? before.samples / leftDivisor : null;
      const scaledSamples = Number.isFinite(after?.samples) ? after.samples / rightDivisor : null;
      const baselineRate = baselineSamples !== null && leftDuration > 0 ? baselineSamples / leftDuration : null;
      const scaledRate = scaledSamples !== null && rightDuration > 0 ? scaledSamples / rightDuration : null;
      const baselinePercent = Number.isFinite(before?.percent) ? before.percent : null;
      const scaledPercent = Number.isFinite(after?.percent) ? after.percent : null;
      return {
        symbol,
        presence: before && after ? 'both' : before ? 'baseline_only' : 'scaled_only',
        baseline_samples: round(baselineSamples, 3), scaled_samples: round(scaledSamples, 3),
        absolute_sample_delta: absoluteRateComparable && baselineSamples !== null && scaledSamples !== null ? round(scaledSamples - baselineSamples, 3) : null,
        baseline_samples_per_second: round(baselineRate, 4), scaled_samples_per_second: round(scaledRate, 4),
        absolute_rate_delta: absoluteRateComparable && baselineRate !== null && scaledRate !== null ? round(scaledRate - baselineRate, 4) : null,
        baseline_percent: round(baselinePercent, 5), scaled_percent: round(scaledPercent, 5),
        percentage_point_delta: distributionComparable && baselinePercent !== null && scaledPercent !== null ? round(scaledPercent - baselinePercent, 5) : null,
        relative_percent_change: distributionComparable && baselinePercent > 0 && scaledPercent !== null ? round((scaledPercent - baselinePercent) / baselinePercent * 100, 3) : null,
        baseline_absolute_source: before?.absolute_source || null,
        scaled_absolute_source: after?.absolute_source || null,
      };
    }).sort((a, b) => {
      const score = row => Math.abs(row.absolute_rate_delta ?? row.absolute_sample_delta ?? row.percentage_point_delta ?? -1);
      return score(b) - score(a);
    });
    const warnings = [...selectionWarnings, ...left.parsed.warnings, ...right.parsed.warnings];
    if (!eventComparable) warnings.push('两侧 perf event 均为 unknown，不能确认采样语义一致；只保留各侧观测值，所有差值为空。请在证据行填写明确事件名。');
    if (!basisComparable) warnings.push(`两侧函数统计口径不一致（单节点 ${left.parsed.basis || 'unknown'}，16 节点 ${right.parsed.basis || 'unknown'}）；Self 与 Inclusive 不可混比，所有差值为空。`);
    if (!(leftDuration > 0 && rightDuration > 0)) warnings.push('至少一侧缺少采集时长，无法比较 samples/s；原始样本差仍受采样时长影响。');
    else if (!samplingConfigComparable) warnings.push(leftSamplingConfig && rightSamplingConfig
      ? `两侧采样配置标签不一致（单节点“${left.sampling_config}”，16 节点“${right.sampling_config}”）；samples/s 只作为各自观测值展示，不计算差值。`
      : '至少一侧缺少采样配置标签（例如 freq=99 或 period=100000）；samples/s 只作为各自观测值展示，不计算差值。');
    if (!samplingTargetComparable) warnings.push(leftSamplingTarget && rightSamplingTarget
      ? `两侧采样目标/范围不一致（单节点“${left.sampling_target}”，16 节点“${right.sampling_target}”）；样本计数、samples/s 与函数占比只作为各自观测值展示，不计算任何差值。`
      : '至少一侧缺少采样目标/范围（例如 PID=1234、TID=5678、CPU=2、per-worker 或 all-CPU）；无法确认函数占比的分母一致，样本计数、samples/s 与占比只作为各自观测值展示，不计算任何差值。');
    if ((metadata?.baseline?.scope || 'per_node') !== (metadata?.scaled?.scope || 'per_node')) warnings.push('两侧采集范围不同；cluster_total 已按节点数折算为每节点，但节点间不均衡可能被平均值掩盖。');
    return {event, status: distributionComparable ? 'paired' : 'not_comparable', baseline_evidence_id: left.id, scaled_evidence_id: right.id,
      baseline_duration_seconds: leftDuration, scaled_duration_seconds: rightDuration,
      baseline_sampling_config: left.sampling_config || null, scaled_sampling_config: right.sampling_config || null,
      sampling_config_comparable: samplingConfigComparable,
      baseline_sampling_target: left.sampling_target || null, scaled_sampling_target: right.sampling_target || null,
      sampling_target_comparable: samplingTargetComparable, distribution_comparable: distributionComparable, basis_comparable: basisComparable,
      baseline_basis: left.parsed.basis || null, scaled_basis: right.parsed.basis || null,
      baseline_divisor: leftDivisor, scaled_divisor: rightDivisor, rows, warnings: [...new Set(warnings)]};
  });
}

function compareSar(evidences, metadata = {}) {
  const baselineWindow = normalizeSamplingConfig(metadata.baseline?.sar_window_signature);
  const scaledWindow = normalizeSamplingConfig(metadata.scaled?.sar_window_signature);
  const windowsAligned = Boolean(baselineWindow && scaledWindow && baselineWindow === scaledWindow);
  const collect = role => {
    const map = new Map();
    for (const evidence of evidences.filter(item => item.role === role && item.parsed?.kind === 'sar')) {
      for (const metric of evidence.parsed.metrics) {
        const key = `${metric.category}\0${metric.device}\0${metric.metric}`;
        const sources = map.get(key) || [];
        const window = evidence.parsed.window || {status: 'unknown', comparable: true};
        sources.push({evidence_id: evidence.id, filename: evidence.filename,
          value: window.comparable ? metric.value : null, observed_value: metric.observed_value ?? metric.value,
          window_status: window.status, window_comparable: window.comparable});
        map.set(key, sources);
      }
    }
    return map;
  };
  const left = collect('baseline');
  const right = collect('scaled');
  const host = collect('host');
  return [...new Set([...left.keys(), ...right.keys(), ...host.keys()])].map(key => {
    const [category, device, metric] = key.split('\0');
    const baselineSources = left.get(key) || [];
    const scaledSources = right.get(key) || [];
    const hostSources = host.get(key) || [];
    const ambiguous = baselineSources.length > 1 || scaledSources.length > 1;
    const invalidSourceWindow = [...baselineSources, ...scaledSources].some(item => !item.window_comparable);
    const ambiguousHost = hostSources.length > 1;
    const invalidHostWindow = hostSources.some(item => !item.window_comparable);
    const baseline = !ambiguous && baselineSources.length === 1 ? baselineSources[0].value : null;
    const scaled = !ambiguous && scaledSources.length === 1 ? scaledSources[0].value : null;
    const hostValue = !ambiguousHost && !invalidHostWindow && hostSources.length === 1 ? hostSources[0].value : null;
    const bothSides = baselineSources.length === 1 && scaledSources.length === 1;
    const status = ambiguous ? 'multiple_sources_not_merged'
      : invalidSourceWindow ? 'source_window_not_comparable'
        : !bothSides ? baselineSources.length ? 'baseline_only' : scaledSources.length ? 'scaled_only' : 'guest_missing'
          : !windowsAligned ? 'windows_not_aligned' : 'paired';
    const warning = ambiguous
      ? `同一场景/实体/指标存在多份 SAR 来源，未自动平均：${[...baselineSources, ...scaledSources].map(item => item.filename).join('、')}。请移除重复来源或在报告外明确聚合口径。`
      : invalidSourceWindow
        ? `SAR 来源跨 LINUX RESTART 或包含可变采样间隔，未计算普通均值或场景差值：${[...baselineSources, ...scaledSources].filter(item => !item.window_comparable).map(item => `${item.filename}(${item.window_status})`).join('、')}。`
        : bothSides && !windowsAligned
          ? baselineWindow && scaledWindow
            ? `两侧 SAR 可比窗口标签不一致（单节点“${metadata.baseline?.sar_window_signature}”，扩展场景“${metadata.scaled?.sar_window_signature}”）；只展示观测值，不计算差值。`
            : '至少一侧缺少 SAR 可比窗口标签；平台不会自动假设 baseline/scaled 时间窗口、稳态阶段或采样时长对齐，只展示观测值。'
          : null;
    const hostStatus = !hostSources.length ? 'missing'
      : ambiguousHost ? 'multiple_sources_not_merged'
        : invalidHostWindow ? 'source_window_not_comparable' : 'observation';
    const hostWarning = ambiguousHost
      ? `Host 同一实体/指标存在多份 SAR 来源，未自动平均：${hostSources.map(item => item.filename).join('、')}。`
      : invalidHostWindow
        ? `Host SAR 来源跨 LINUX RESTART 或包含可变采样间隔，观测值保持为空：${hostSources.filter(item => !item.window_comparable).map(item => `${item.filename}(${item.window_status})`).join('、')}。`
        : null;
    return {category, device, metric, unit: sarUnit(metric), baseline: round(baseline, 5), scaled: round(scaled, 5), host: round(hostValue, 5),
      absolute_delta: status === 'paired' && baseline !== null && scaled !== null ? round(scaled - baseline, 5) : null,
      relative_change_percent: status === 'paired' && baseline !== null && baseline !== 0 && scaled !== null ? round((scaled - baseline) / Math.abs(baseline) * 100, 3) : null,
      baseline_sources: baselineSources, scaled_sources: scaledSources, host_sources: hostSources, warning, host_warning: hostWarning, host_status: hostStatus,
      baseline_window_signature: metadata.baseline?.sar_window_signature || null,
      scaled_window_signature: metadata.scaled?.sar_window_signature || null, status};
  }).sort((a, b) => a.category.localeCompare(b.category) || Math.abs(b.relative_change_percent ?? -1) - Math.abs(a.relative_change_percent ?? -1));
}

function analyzeNuma(evidences) {
  const artifacts = evidences.filter(item => item.role === 'host' && item.parsed?.kind === 'numa');
  const warnings = [...new Set(artifacts.flatMap(item => item.parsed.warnings || []))];
  const systemSnapshots = artifacts.filter(item => item.parsed.format === 'numastat' && Object.keys(item.parsed.system || {}).length);
  const ambiguousSystemSnapshots = systemSnapshots.length > 1;
  if (ambiguousSystemSnapshots) warnings.unshift(`发现 ${systemSnapshots.length} 份 numastat 系统累计快照，未求和或自动相减；请标明压测前/后并在显式差值流程中处理。`);
  let numaHit = 0; let numaMiss = 0; let local = 0; let other = 0;
  let hasHitMiss = false; let hasLocalOther = false;
  const processDistributions = []; const pageDistributions = [];
  for (const artifact of artifacts) {
    const parsed = artifact.parsed;
    if (parsed.format === 'numastat') {
      if (!ambiguousSystemSnapshots && (parsed.system.numa_hit || parsed.system.numa_miss)) {
        hasHitMiss = true; numaHit += parsed.system.numa_hit?.total || 0; numaMiss += parsed.system.numa_miss?.total || 0;
      }
      if (!ambiguousSystemSnapshots && (parsed.system.local_node || parsed.system.other_node)) {
        hasLocalOther = true; local += parsed.system.local_node?.total || 0; other += parsed.system.other_node?.total || 0;
      }
      for (const [name, row] of Object.entries(parsed.process_rows || {})) processDistributions.push({evidence_id: artifact.id, name, ...row});
    } else pageDistributions.push({evidence_id: artifact.id, pages: parsed.pages, policies: parsed.policies, mapped_rows: parsed.mapped_rows});
  }
  const missRatio = hasHitMiss && numaHit + numaMiss > 0 ? numaMiss / (numaHit + numaMiss) * 100 : null;
  const otherRatio = hasLocalOther && local + other > 0 ? other / (local + other) * 100 : null;
  let assessment = '缺少可用于判断 NUMA 放置的 host 证据。';
  let confidence = 'low';
  if (ambiguousSystemSnapshots) {
    assessment = '存在多份未标注前后关系的 numastat 累计快照，不能求和或直接比较；NUMA 比例保持未知。请显式选择单个快照，或提供压测前后差值。';
  } else if (missRatio !== null || otherRatio !== null) {
    const high = (missRatio ?? 0) >= 5 || (otherRatio ?? 0) >= 10;
    assessment = high
      ? '累计 NUMA 放置计数中存在较高的 miss/other_node 比例，需要采集压测前后增量并结合 uncore/访问延迟验证。'
      : '当前累计 NUMA 放置计数没有显示大量放置偏离；这不能单独证明运行时没有跨 NUMA 内存访问。';
    confidence = 'medium';
  } else if (processDistributions.length || pageDistributions.length) {
    assessment = '证据只说明页面分布或内存策略，无法据此计算跨 NUMA 访问率。需要 numastat 压测前后差值或硬件事件。';
  }
  return {status: !artifacts.length ? 'missing' : ambiguousSystemSnapshots ? 'ambiguous_multiple_snapshots' : 'available',
    ambiguous_multiple_snapshots: ambiguousSystemSnapshots, evidence_count: artifacts.length, numa_miss_ratio_percent: round(missRatio, 5),
    other_node_ratio_percent: round(otherRatio, 5), process_distributions: processDistributions, page_distributions: pageDistributions,
    assessment, confidence, warnings};
}

function coverageFor(evidences, role) {
  const roleEvidence = evidences.filter(item => item.role === role);
  const sarCategories = new Set(roleEvidence.filter(item => item.parsed?.kind === 'sar').flatMap(item => item.parsed.metrics.map(metric => metric.category)));
  return {
    perf: roleEvidence.some(item => item.parsed?.kind === 'perf'),
    sar_cpu: sarCategories.has('cpu'), sar_memory: sarCategories.has('memory'), sar_network: sarCategories.has('network'), sar_io: sarCategories.has('io'),
    numa: roleEvidence.some(item => item.parsed?.kind === 'numa'), evidence_count: roleEvidence.length,
  };
}

function buildScalingAnalysis(metadata = {}) {
  const baselineNodes = number(metadata.baseline?.node_count);
  const scaledNodes = number(metadata.scaled?.node_count);
  const nodeRatio = baselineNodes > 0 && scaledNodes > 0 ? scaledNodes / baselineNodes : null;
  const metric = (key, label, unit) => {
    const baseline = number(metadata.baseline?.[key]);
    const scaled = number(metadata.scaled?.[key]);
    const comparable = baseline > 0 && scaled !== null && scaled >= 0 && nodeRatio > 0;
    const speedup = comparable ? scaled / baseline : null;
    return {
      metric: key, label, unit, scope: 'cluster_total', baseline: round(baseline, 4), scaled: round(scaled, 4),
      baseline_per_node: baseline !== null && baselineNodes > 0 ? round(baseline / baselineNodes, 4) : null,
      scaled_per_node: scaled !== null && scaledNodes > 0 ? round(scaled / scaledNodes, 4) : null,
      speedup: round(speedup, 5), efficiency: comparable ? round(speedup / nodeRatio, 7) : null,
      efficiency_percent: comparable ? round(speedup / nodeRatio * 100, 4) : null,
      status: comparable ? 'comparable' : baseline === 0 ? 'invalid_zero_baseline' : 'missing',
    };
  };
  return {
    baseline_nodes: round(baselineNodes, 4), scaled_nodes: round(scaledNodes, 4), node_ratio: round(nodeRatio, 5),
    metrics: [metric('pps', '实测集群 PPS', 'packets/s'), metric('cps', '实测集群 CPS', 'connections/s')],
  };
}

function provenanceParser(format) {
  const parsers = {
    'flamegraph-svg': ['flamegraph_svg_titles_v1', 'high'],
    'perf-folded': ['perf_folded_stacks_v1', 'high'],
    'perf-report': ['perf_report_stdio_v1', 'medium'],
    'sar-text': ['sar_text_sections_v1', 'medium'],
    'sadf-semicolon': ['sadf_delimited_v1', 'high'],
    'sadf-csv': ['sadf_delimited_v1', 'high'],
    numastat: ['numastat_text_v1', 'high'],
    numa_maps: ['proc_numa_maps_v1', 'high'],
    'unparsed-text': ['unparsed_text_passthrough_v1', 'low'],
  };
  const [parser, confidence] = parsers[format] || ['local_text_autodetect_v1', 'low'];
  return {parser, confidence};
}

function freshLocalAnalysisId() {
  const uuid = globalThis.crypto?.randomUUID?.();
  return `local-${uuid || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
}

function buildAnalysis(evidences = [], metadata = {}) {
  const safeEvidence = evidences.filter(item => item && item.parsed);
  const coverage = {baseline: coverageFor(safeEvidence, 'baseline'), scaled: coverageFor(safeEvidence, 'scaled'), host: coverageFor(safeEvidence, 'host')};
  const perf = comparePerf(safeEvidence, metadata);
  const sar = compareSar(safeEvidence, metadata);
  const hostSarObservations = sar.filter(row => row.host_sources?.length).map(row => ({
    category: row.category, device: row.device, metric: row.metric, unit: row.unit,
    value: row.host, status: row.host_status, sources: row.host_sources, warning: row.host_warning,
  }));
  const numa = analyzeNuma(safeEvidence);
  const scaling = buildScalingAnalysis(metadata);
  const unparsedEvidence = safeEvidence.filter(item => item.parsed?.kind === 'unparsed_text').map(item => ({
    id: item.id, role: item.role, filename: item.filename, sha256: item.sha256 || null,
    candidate_kind: item.parsed.candidate_kind, excerpt: item.parsed.excerpt,
    excerpt_truncated: item.parsed.excerpt_truncated, source_bytes: item.parsed.source_bytes,
    parser_error: item.parsed.parser_error,
  }));
  const findings = [];
  const limitations = [];
  for (const warning of perf.flatMap(item => item.warnings || [])) {
    if (/只使用最早导入|perf event 均为 unknown/.test(warning)) limitations.push(warning);
  }
  for (const row of sar.filter(item => ['multiple_sources_not_merged', 'source_window_not_comparable', 'windows_not_aligned'].includes(item.status))) limitations.push(row.warning);
  for (const row of hostSarObservations.filter(item => item.warning)) limitations.push(row.warning);
  if (numa.ambiguous_multiple_snapshots) limitations.push(numa.assessment);
  for (const item of unparsedEvidence) limitations.push(`${item.filename} 未通过确定性解析（候选类型 ${item.candidate_kind}）：${item.parser_error}。它未计入覆盖矩阵，仅作为 Agent 候选文本。`);
  for (const metric of scaling.metrics.filter(item => item.status === 'comparable')) findings.push({
    severity: metric.efficiency_percent < 70 ? 'warning' : 'info', title: `${metric.label} 线性扩展效率 ${metric.efficiency_percent}%`,
    detail: `${scaling.baseline_nodes} → ${scaling.scaled_nodes} 节点，集群总量 ${metric.baseline} → ${metric.scaled} ${metric.unit}，加速比 ${metric.speedup}×。`,
  });
  const pairedPerf = perf.filter(item => item.status === 'paired');
  if (pairedPerf.length) {
    const event = pairedPerf[0];
    const top = event.rows.find(row => row.presence === 'both' && (row.absolute_rate_delta !== null || row.absolute_sample_delta !== null || row.percentage_point_delta !== null));
    if (top) findings.push({severity: 'info', title: `${event.event} 变化最大的函数：${top.symbol}`,
      detail: top.absolute_rate_delta !== null ? `每节点样本率变化 ${top.absolute_rate_delta >= 0 ? '+' : ''}${top.absolute_rate_delta} samples/s，占比变化 ${top.percentage_point_delta ?? '未知'} 个百分点。`
        : top.absolute_sample_delta !== null ? `归一化原始样本计数变化 ${top.absolute_sample_delta >= 0 ? '+' : ''}${top.absolute_sample_delta}；采样配置或时长未形成可比口径，不能解释为 CPU 时间。`
          : `占比变化 ${top.percentage_point_delta >= 0 ? '+' : ''}${top.percentage_point_delta} 个百分点；源数据没有绝对样本。`});
  } else limitations.push('没有同事件的单节点与 16 节点 perf 证据，未生成函数差异结论。');
  const pairedSar = sar.filter(item => item.status === 'paired');
  if (pairedSar.length) {
    const top = [...pairedSar].filter(item => item.relative_change_percent !== null).sort((a, b) => Math.abs(b.relative_change_percent) - Math.abs(a.relative_change_percent))[0];
    if (top) findings.push({severity: 'info', title: `SAR 变化：${top.device ? `${top.device} ` : ''}${top.metric}`,
      detail: `单节点 ${top.baseline}${top.unit}，16 节点场景 ${top.scaled}${top.unit}，绝对变化 ${top.absolute_delta}${top.unit}（${top.relative_change_percent}%）。`});
  } else limitations.push('没有可配对的 guest SAR 指标，内存、网络和 IO 差异保持未知。');
  findings.push({severity: numa.confidence === 'medium' ? 'info' : 'warning', title: 'Host NUMA 评估', detail: numa.assessment});
  for (const [role, label] of [['baseline', '单节点 guest'], ['scaled', '16 节点 guest']]) {
    for (const key of ['perf', 'sar_cpu', 'sar_memory', 'sar_network', 'sar_io']) if (!coverage[role][key]) limitations.push(`${label} 缺少 ${key.replace('sar_', 'SAR ')} 证据。`);
  }
  limitations.push('perf 样本计数不是执行时间；只有事件、Self/Inclusive 口径、采样配置、采样目标/范围、采集范围和时长一致时，samples/s 才能用于绝对对比。采样目标缺失或不一致时，函数占比的分母也不能假定一致。');
  limitations.push('baseline/scaled SAR 时间窗口不会自动对齐；只有两侧提供相同的可比窗口标签，且来源不存在跨启动边界或可变间隔时，才计算指标差值。');
  limitations.push('函数未出现在一侧样本中记为“未采到”，不会自动当作 0。');
  return {
    schema_version: SCHEMA_VERSION,
    analysis_id: String(metadata.analysis_id || metadata.session_id || freshLocalAnalysisId()),
    manifest: {
      source: 'desktop_local_import',
      scenarios: {
        baseline: {...metadata.baseline, role: 'single_node_guest', evidence_ids: safeEvidence.filter(item => item.role === 'baseline').map(item => item.id)},
        scaled: {...metadata.scaled, role: 'sixteen_node_guest', evidence_ids: safeEvidence.filter(item => item.role === 'scaled').map(item => item.id)},
        host: {...metadata.host, role: 'host_single_node', evidence_ids: safeEvidence.filter(item => item.role === 'host').map(item => item.id)},
      },
    },
    generated_at: new Date().toISOString(), metadata, coverage, scaling_analysis: scaling, unparsed_evidence: unparsedEvidence,
    perf_comparison: perf, sar_comparison: sar, host_sar_observations: hostSarObservations, host_numa_analysis: numa,
    findings, recommendations: [
      '先补齐同一 perf event、Self/Inclusive 口径、采样频率、采样目标/范围和明确时长的两侧数据，再按 samples/s 与占比百分点共同排序。',
      '对网络、IO 和 NUMA 累计计数采集压测前后两个快照，以差值除以压测时长；同时保存 PPS/CPS 与连接数作为工作量分母。',
      '对疑似多核共享瓶颈补采 perf c2c、队列满/重试计数以及每个 RX/TX 队列 PPS，避免只凭火焰图归因。',
    ], limitations: [...new Set(limitations)],
    provenance: safeEvidence.map(item => ({id: item.id, role: item.role, filename: item.filename, bytes: item.bytes, sha256: item.sha256 || null,
      detected_kind: item.detected_kind, format: item.parsed.format, ...provenanceParser(item.parsed.format), event: item.event_override || item.parsed.event || null,
      basis: item.parsed.basis || null, total_samples: item.parsed.total_samples ?? null,
      duration_seconds: number(item.duration_seconds), sampling_config: item.sampling_config || null,
      sampling_target: item.sampling_target || null, sar_window: item.parsed.window || null, parser_error: item.parsed.parser_error || null,
      imported_at: item.imported_at, warnings: item.parsed.warnings || []})),
  };
}

function jsonBytes(value) {
  return new TextEncoder().encode(JSON.stringify(value)).byteLength;
}

function compactStrings(value, limit = 512) {
  if (typeof value === 'string') return value.length > limit ? `${value.slice(0, limit)}…[truncated]` : value;
  if (Array.isArray(value)) return value.map(item => compactStrings(item, limit));
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, compactStrings(item, limit)]));
  return value;
}

function buildAgentPayload(analysis, maxBytes = 240 * 1024) {
  const perfSource = analysis.perf_comparison || [];
  const sarSource = analysis.sar_comparison || [];
  const hostSarSource = analysis.host_sar_observations || [];
  let eventLimit = 8;
  let rowLimit = 50;
  let sarLimit = 100;
  let hostSarLimit = 100;
  const truncatedInitially = perfSource.length > eventLimit || perfSource.some(item => (item.rows || []).length > rowLimit)
    || sarSource.length > sarLimit || hostSarSource.length > hostSarLimit;
  const make = (compactProvenance = false) => {
    const truncation = truncatedInitially || perfSource.length > eventLimit || perfSource.some(item => (item.rows || []).length > rowLimit)
      || sarSource.length > sarLimit || hostSarSource.length > hostSarLimit;
    const limitation = `Agent 证据摘要已限制为最多 ${eventLimit} 个 perf events × 每个 ${rowLimit} 行、guest SAR ${sarLimit} 行、Host SAR ${hostSarLimit} 行；完整确定性结果仍保留在本地报告。`;
    return {
      ...analysis,
      perf_comparison: perfSource.slice(0, eventLimit).map(item => ({...item, rows: (item.rows || []).slice(0, rowLimit)})),
      sar_comparison: sarSource.slice(0, sarLimit),
      host_sar_observations: hostSarSource.slice(0, hostSarLimit),
      provenance: compactProvenance ? (analysis.provenance || []).map(item => ({
        id: item.id, role: item.role, filename: String(item.filename || '').slice(0, 256), bytes: item.bytes, sha256: item.sha256,
        detected_kind: item.detected_kind, format: item.format, parser: item.parser, confidence: item.confidence,
        event: item.event, basis: item.basis, total_samples: item.total_samples,
        duration_seconds: item.duration_seconds, sampling_config: item.sampling_config,
        sampling_target: item.sampling_target, sar_window: item.sar_window, imported_at: item.imported_at,
      })) : analysis.provenance,
      limitations: truncation ? [...new Set([...(analysis.limitations || []), limitation])] : analysis.limitations,
      agent_payload_truncated: truncation,
    };
  };
  let payload = make(false);
  if (jsonBytes(payload) > maxBytes) { eventLimit = 4; rowLimit = 25; sarLimit = 50; hostSarLimit = 50; payload = make(true); }
  if (jsonBytes(payload) > maxBytes) { eventLimit = 2; rowLimit = 10; sarLimit = 20; hostSarLimit = 20; payload = make(true); }
  if (jsonBytes(payload) > maxBytes) {
    eventLimit = 0; rowLimit = 0; sarLimit = 0; hostSarLimit = 0; payload = make(true);
    payload.limitations = [...new Set([...(payload.limitations || []), 'Agent payload 达到字节上限，函数行与 guest/Host SAR 行已全部省略；请以本地 HTML/Markdown 报告为准。'])];
    payload.agent_payload_truncated = true;
  }
  if (jsonBytes(payload) > maxBytes) {
    payload = compactStrings(payload, 512);
    payload.limitations = [...new Set([...(payload.limitations || []), 'Agent payload 中过长的文本字段已裁剪到 512 字符；证据条目和哈希清单保持完整。'])];
    payload.agent_payload_truncated = true;
  }
  return {payload, bytes: jsonBytes(payload), truncated: Boolean(payload.agent_payload_truncated), max_bytes: maxBytes};
}

function mdCell(value) {
  if (value === null || value === undefined || value === '') return '—';
  return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/([\\`*_\[\]])/g, '\\$1').replace(/\|/g, '\\|').replace(/\r?\n/g, ' ');
}

function fmtSigned(value, suffix = '') {
  return Number.isFinite(value) ? `${value > 0 ? '+' : ''}${value}${suffix}` : '—';
}

function buildMarkdownReport(analysis) {
  const title = analysis.metadata?.title || 'DPVS / CVS 线性扩展性能分析';
  const lines = [`# ${mdCell(String(title).replace(/[\r\n#]+/g, ' ').trim())}`, '', `分析 ID：${mdCell(analysis.analysis_id)}`, `生成时间：${mdCell(analysis.generated_at)}`, '', '## 证据覆盖', '',
    '| 场景 | Perf | SAR CPU | SAR 内存 | SAR 网络 | SAR IO | NUMA |', '| --- | --- | --- | --- | --- | --- | --- |'];
  for (const [role, label] of [['baseline', '单节点 guest'], ['scaled', '16 节点 guest'], ['host', 'Host 单机']]) {
    const row = analysis.coverage[role];
    lines.push(`| ${label} | ${row.perf ? '有' : '缺失'} | ${row.sar_cpu ? '有' : '缺失'} | ${row.sar_memory ? '有' : '缺失'} | ${row.sar_network ? '有' : '缺失'} | ${row.sar_io ? '有' : '缺失'} | ${row.numa ? '有' : '缺失'} |`);
  }
  lines.push('', '## 线性扩展效率（集群总量）', '', `节点倍数：${mdCell(analysis.scaling_analysis.node_ratio)}×`, '',
    '| 指标 | 单节点场景集群总量 | 扩展场景集群总量 | 加速比 | 扩展效率 |', '| --- | ---: | ---: | ---: | ---: |');
  for (const metric of analysis.scaling_analysis.metrics) lines.push(`| ${metric.label} | ${mdCell(metric.baseline)} ${metric.unit} | ${mdCell(metric.scaled)} ${metric.unit} | ${mdCell(metric.speedup)}× | ${mdCell(metric.efficiency_percent)}% |`);
  lines.push('', '## 主要发现', '');
  for (const finding of analysis.findings) lines.push(`- **${mdCell(finding.title)}**：${mdCell(finding.detail)}`);
  for (const comparison of analysis.perf_comparison) {
    lines.push('', `## Perf 函数差异：${mdCell(comparison.event)}`, '');
    if (comparison.status === 'unpaired') { lines.push(`无法配对：${mdCell(comparison.warnings.join('；'))}`); continue; }
    lines.push('| 函数 | 单节点观测值 | 16节点观测值 | 可比绝对变化 | 单节点占比 | 16节点占比 | 百分点变化 | 占比相对变化 |', '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |');
    for (const row of comparison.rows.slice(0, 30)) {
      const hasRateObservations = row.baseline_samples_per_second !== null && row.scaled_samples_per_second !== null;
      const comparableDelta = row.absolute_rate_delta !== null ? row.absolute_rate_delta : hasRateObservations ? null : row.absolute_sample_delta;
      lines.push(`| ${mdCell(row.symbol)} | ${mdCell(row.baseline_samples_per_second ?? row.baseline_samples)} | ${mdCell(row.scaled_samples_per_second ?? row.scaled_samples)} | ${mdCell(comparableDelta)} | ${mdCell(row.baseline_percent)}% | ${mdCell(row.scaled_percent)}% | ${fmtSigned(row.percentage_point_delta, ' pp')} | ${fmtSigned(row.relative_percent_change, '%')} |`);
    }
    if (comparison.warnings.length) lines.push('', ...comparison.warnings.map(value => `> ${mdCell(value)}`));
  }
  lines.push('', '## SAR 对比与 Host 观测', '', '| 类别 | 设备 | 指标 | 单节点 | 16节点 | Host 单机 | 绝对变化 | 相对变化 | Guest 状态 | Host 状态 |', '| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |');
  for (const row of analysis.sar_comparison.slice(0, 60)) lines.push(`| ${mdCell(row.category)} | ${mdCell(row.device)} | ${mdCell(row.metric)} | ${mdCell(row.baseline)} ${mdCell(row.unit)} | ${mdCell(row.scaled)} ${mdCell(row.unit)} | ${mdCell(row.host)} ${mdCell(row.unit)} | ${fmtSigned(row.absolute_delta, row.unit)} | ${fmtSigned(row.relative_change_percent, '%')} | ${mdCell(row.status)} | ${mdCell(row.host_status)} |`);
  lines.push('', '## Host NUMA', '', analysis.host_numa_analysis.assessment, '', `置信度：${analysis.host_numa_analysis.confidence}`,
    '', `- numa_miss 比例：${mdCell(analysis.host_numa_analysis.numa_miss_ratio_percent)}%`, `- other_node 比例：${mdCell(analysis.host_numa_analysis.other_node_ratio_percent)}%`);
  lines.push('', '## 建议', '', ...analysis.recommendations.map(value => `- ${mdCell(value)}`), '', '## 限制', '', ...analysis.limitations.map(value => `- ${mdCell(value)}`), '', '## 证据清单', '',
    '| 场景 | 文件 | SHA-256 | 类型/格式/口径 | 事件 | 时长 | 采样配置 | 采样目标 | SAR窗口状态 |', '| --- | --- | --- | --- | --- | ---: | --- | --- | --- |');
  for (const row of analysis.provenance) lines.push(`| ${mdCell(row.role)} | ${mdCell(row.filename)} | ${mdCell(row.sha256)} | ${mdCell(`${row.detected_kind}/${row.format}/${row.basis || '—'}`)} | ${mdCell(row.event)} | ${mdCell(row.duration_seconds)} | ${mdCell(row.sampling_config)} | ${mdCell(row.sampling_target)} | ${mdCell(row.sar_window?.status)} |`);
  return `${lines.join('\n')}\n`;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, character => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[character]));
}

function buildHtmlReport(analysis) {
  const title = analysis.metadata?.title || 'DPVS / CVS 线性扩展性能分析';
  const coverageRows = [['baseline', '单节点 guest'], ['scaled', '16 节点 guest'], ['host', 'Host 单机']].map(([role, label]) => {
    const row = analysis.coverage[role];
    return `<tr><th>${escapeHtml(label)}</th>${['perf', 'sar_cpu', 'sar_memory', 'sar_network', 'sar_io', 'numa'].map(key => `<td class="${row[key] ? 'ok' : 'missing'}">${row[key] ? '有' : '缺失'}</td>`).join('')}</tr>`;
  }).join('');
  const findings = analysis.findings.map(item => `<article class="finding ${escapeHtml(item.severity)}"><h3>${escapeHtml(item.title)}</h3><p>${escapeHtml(item.detail)}</p></article>`).join('');
  const perfSections = analysis.perf_comparison.map(comparison => {
    if (comparison.status === 'unpaired') return `<section><h2>Perf：${escapeHtml(comparison.event)}</h2><p>${escapeHtml(comparison.warnings.join('；'))}</p></section>`;
    const rows = comparison.rows.slice(0, 50).map(row => {
      const hasRateObservations = row.baseline_samples_per_second !== null && row.scaled_samples_per_second !== null;
      const comparableDelta = row.absolute_rate_delta !== null ? row.absolute_rate_delta : hasRateObservations ? null : row.absolute_sample_delta;
      return `<tr><th>${escapeHtml(row.symbol)}</th><td>${escapeHtml(row.baseline_samples_per_second ?? row.baseline_samples ?? '—')}</td><td>${escapeHtml(row.scaled_samples_per_second ?? row.scaled_samples ?? '—')}</td><td>${escapeHtml(fmtSigned(comparableDelta))}</td><td>${escapeHtml(row.baseline_percent ?? '—')}%</td><td>${escapeHtml(row.scaled_percent ?? '—')}%</td><td>${escapeHtml(fmtSigned(row.percentage_point_delta, ' pp'))}</td><td>${escapeHtml(fmtSigned(row.relative_percent_change, '%'))}</td></tr>`;
    }).join('');
    return `<section><h2>Perf：${escapeHtml(comparison.event)}</h2><table><thead><tr><th>函数</th><th>单节点观测值</th><th>16节点观测值</th><th>可比绝对变化</th><th>单节点占比</th><th>16节点占比</th><th>百分点变化</th><th>占比相对变化</th></tr></thead><tbody>${rows}</tbody></table>${comparison.warnings.map(value => `<p class="note">${escapeHtml(value)}</p>`).join('')}</section>`;
  }).join('');
  const sarRows = analysis.sar_comparison.slice(0, 100).map(row => `<tr><td>${escapeHtml(row.category)}</td><td>${escapeHtml(row.device || '—')}</td><th>${escapeHtml(row.metric)}</th><td>${escapeHtml(row.baseline ?? '—')} ${escapeHtml(row.unit)}</td><td>${escapeHtml(row.scaled ?? '—')} ${escapeHtml(row.unit)}</td><td>${escapeHtml(row.host ?? '—')} ${escapeHtml(row.unit)}</td><td>${escapeHtml(fmtSigned(row.absolute_delta, row.unit))}</td><td>${escapeHtml(fmtSigned(row.relative_change_percent, '%'))}</td><td>${escapeHtml(row.status)}</td><td>${escapeHtml(row.host_status)}</td></tr>`).join('');
  const provenance = analysis.provenance.map(row => `<tr><td>${escapeHtml(row.role)}</td><th>${escapeHtml(row.filename)}</th><td><code>${escapeHtml(row.sha256 || '—')}</code></td><td>${escapeHtml(`${row.detected_kind}/${row.format}/${row.basis || '—'}`)}</td><td>${escapeHtml(row.event || '—')}</td><td>${escapeHtml(row.duration_seconds ?? '—')}</td><td>${escapeHtml(row.sampling_config || '—')}</td><td>${escapeHtml(row.sampling_target || '—')}</td><td>${escapeHtml(row.sar_window?.status || '—')}</td></tr>`).join('');
  const scalingRows = analysis.scaling_analysis.metrics.map(metric => `<tr><th>${escapeHtml(metric.label)}</th><td>${escapeHtml(metric.baseline ?? '—')} ${escapeHtml(metric.unit)}</td><td>${escapeHtml(metric.scaled ?? '—')} ${escapeHtml(metric.unit)}</td><td>${escapeHtml(metric.speedup ?? '—')}×</td><td>${escapeHtml(metric.efficiency_percent ?? '—')}%</td></tr>`).join('');
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:"><title>${escapeHtml(title)}</title><style>body{margin:0;background:#f5f5f3;color:#202124;font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}main{max-width:1180px;margin:auto;padding:40px 28px}h1{font-size:28px;margin:0 0 4px}h2{margin:34px 0 12px;font-size:19px;border-bottom:1px solid #ddd;padding-bottom:7px}h3{margin:0 0 5px;font-size:14px}p{margin:4px 0 12px}.meta,.note{color:#666}.finding{border-left:4px solid #3975d6;background:white;padding:12px 16px;margin:10px 0}.finding.warning{border-color:#c17a12}table{width:100%;border-collapse:collapse;background:#fff;font-size:12px}th,td{padding:7px 9px;border:1px solid #ddd;text-align:left;vertical-align:top}thead{background:#eceff3}.ok{color:#237a3c}.missing{color:#a1443c}code{font-size:10px;word-break:break-all}ul{padding-left:21px}@media print{body{background:#fff}main{max-width:none;padding:0}.finding{break-inside:avoid}table{break-inside:auto}tr{break-inside:avoid}}</style></head><body><main><h1>${escapeHtml(title)}</h1><p class="meta">分析 ID：${escapeHtml(analysis.analysis_id)} · 生成时间：${escapeHtml(analysis.generated_at)} · Schema ${escapeHtml(analysis.schema_version)}</p><h2>证据覆盖</h2><table><thead><tr><th>场景</th><th>Perf</th><th>SAR CPU</th><th>SAR 内存</th><th>SAR 网络</th><th>SAR IO</th><th>NUMA</th></tr></thead><tbody>${coverageRows}</tbody></table><h2>线性扩展效率（集群总量）</h2><p class="meta">节点倍数：${escapeHtml(analysis.scaling_analysis.node_ratio ?? '—')}×</p><table><thead><tr><th>指标</th><th>单节点场景集群总量</th><th>扩展场景集群总量</th><th>加速比</th><th>扩展效率</th></tr></thead><tbody>${scalingRows}</tbody></table><h2>主要发现</h2>${findings}${perfSections}<section><h2>SAR 对比与 Host 观测</h2><table><thead><tr><th>类别</th><th>设备</th><th>指标</th><th>单节点</th><th>16节点</th><th>Host 单机</th><th>绝对变化</th><th>相对变化</th><th>Guest 状态</th><th>Host 状态</th></tr></thead><tbody>${sarRows}</tbody></table></section><section><h2>Host NUMA</h2><p>${escapeHtml(analysis.host_numa_analysis.assessment)}</p><p class="meta">置信度：${escapeHtml(analysis.host_numa_analysis.confidence)} · numa_miss ${escapeHtml(analysis.host_numa_analysis.numa_miss_ratio_percent ?? '—')}% · other_node ${escapeHtml(analysis.host_numa_analysis.other_node_ratio_percent ?? '—')}%</p></section><section><h2>建议</h2><ul>${analysis.recommendations.map(value => `<li>${escapeHtml(value)}</li>`).join('')}</ul></section><section><h2>限制</h2><ul>${analysis.limitations.map(value => `<li>${escapeHtml(value)}</li>`).join('')}</ul></section><section><h2>证据清单</h2><table><thead><tr><th>场景</th><th>文件</th><th>SHA-256</th><th>类型/格式/口径</th><th>事件</th><th>时长（秒）</th><th>采样配置</th><th>采样目标</th><th>SAR窗口状态</th></tr></thead><tbody>${provenance}</tbody></table></section></main></body></html>`;
}

module.exports = {
  SCHEMA_VERSION, MAX_FILE_BYTES, MAX_SESSION_BYTES, MAX_EVIDENCE_FILES, detectArtifactKind, inspectText, parseEvidenceText,
  parsePerf, parseSarText, parseNumastat, parseNumaMaps, comparePerf, compareSar, analyzeNuma, buildAnalysis,
  buildMarkdownReport, buildHtmlReport, buildScalingAnalysis, buildAgentPayload, escapeHtml, normalizeEvent,
};
