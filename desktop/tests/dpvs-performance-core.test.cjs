'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const core = require('../renderer/dpvs-performance-core.cjs');

function evidence(id, role, filename, content, extra = {}) {
  const result = core.parseEvidenceText({filename, content});
  return {id, role, filename, bytes: Buffer.byteLength(content), sha256: id.repeat(8).slice(0, 64),
    detected_kind: result.detected_kind, parsed: result.parsed, imported_at: '2026-10-31T00:00:00.000Z', ...extra};
}

test('folded stacks compare normalized absolute sample rates and percentage changes', () => {
  const baseline = evidence('a', 'baseline', 'cycles.folded', 'main;worker;tcp_send_csum 40\nmain;worker;business 60\n', {duration_seconds: 10, sampling_config: 'freq=99', sampling_target: ' all-CPU '});
  const scaled = evidence('b', 'scaled', 'cycles.folded', 'main;worker;tcp_send_csum 120\nmain;worker;business 80\n', {duration_seconds: 10, sampling_config: ' FREQ = 99 ', sampling_target: 'all-cpu'});
  const analysis = core.buildAnalysis([baseline, scaled], {
    title: 'comparison', baseline: {node_count: 1, scope: 'per_node'}, scaled: {node_count: 16, scope: 'per_node'}, host: {},
  });
  const pair = analysis.perf_comparison[0];
  assert.equal(pair.status, 'paired');
  const checksum = pair.rows.find(row => row.symbol === 'tcp_send_csum');
  assert.equal(checksum.baseline_samples_per_second, 4);
  assert.equal(checksum.scaled_samples_per_second, 12);
  assert.equal(checksum.absolute_rate_delta, 8);
  assert.equal(checksum.percentage_point_delta, 20);
  assert.equal(checksum.relative_percent_change, 50);
});

test('cluster aggregate profile is divided by node count and absent symbols remain unknown', () => {
  const baseline = evidence('c', 'baseline', 'cycles.folded', 'main;base 16\n', {duration_seconds: 1, sampling_config: 'period=100000', sampling_target: 'all-CPU'});
  const scaled = evidence('d', 'scaled', 'cycles.folded', 'main;new_hotspot 160\n', {duration_seconds: 1, sampling_config: 'period=100000', sampling_target: 'all-CPU'});
  const pair = core.comparePerf([baseline, scaled], {
    baseline: {node_count: 1, scope: 'per_node'}, scaled: {node_count: 16, scope: 'cluster_total'},
  })[0];
  const added = pair.rows.find(row => row.symbol === 'new_hotspot');
  assert.equal(added.scaled_samples_per_second, 10);
  assert.equal(added.baseline_samples, null);
  assert.equal(added.absolute_rate_delta, null);
  assert.equal(added.presence, 'scaled_only');
});

test('sample rates remain observations until both sampling configuration tags match', () => {
  const baseline = evidence('m', 'baseline', 'cycles.folded', 'main;hot 10\n', {duration_seconds: 1, sampling_target: 'CPU=2'});
  const scaled = evidence('n', 'scaled', 'cycles.folded', 'main;hot 20\n', {duration_seconds: 1, sampling_target: 'cpu=2'});
  const pair = core.comparePerf([baseline, scaled], {baseline: {scope: 'per_node'}, scaled: {scope: 'per_node'}})[0];
  const hot = pair.rows.find(row => row.symbol === 'hot');
  assert.equal(hot.baseline_samples_per_second, 10);
  assert.equal(hot.scaled_samples_per_second, 20);
  assert.equal(hot.absolute_rate_delta, null);
  assert.equal(pair.sampling_config_comparable, false);
  assert.match(pair.warnings.join(' '), /缺少采样配置标签/);
});

test('missing or mismatched sampling targets keep all perf deltas null while preserving observations', () => {
  const baseline = evidence('t1', 'baseline', 'cycles.folded', 'main;hot 10\n', {duration_seconds: 1, sampling_config: 'freq=99'});
  const scaled = evidence('t2', 'scaled', 'cycles.folded', 'main;hot 20\n', {duration_seconds: 1, sampling_config: 'freq=99'});
  const missing = core.comparePerf([baseline, scaled], {baseline: {scope: 'per_node'}, scaled: {scope: 'per_node'}})[0];
  const missingHot = missing.rows.find(row => row.symbol === 'hot');
  assert.equal(missing.status, 'not_comparable');
  assert.equal(missingHot.baseline_samples_per_second, 10);
  assert.equal(missingHot.scaled_samples_per_second, 20);
  assert.equal(missingHot.absolute_sample_delta, null);
  assert.equal(missingHot.absolute_rate_delta, null);
  assert.equal(missingHot.percentage_point_delta, null);
  assert.equal(missingHot.relative_percent_change, null);
  assert.match(missing.warnings.join(' '), /缺少采样目标\/范围/);

  const mismatched = core.comparePerf([
    {...baseline, sampling_target: 'PID=1234'},
    {...scaled, sampling_target: 'all-CPU'},
  ], {baseline: {scope: 'per_node'}, scaled: {scope: 'per_node'}})[0];
  assert.equal(mismatched.status, 'not_comparable');
  assert.equal(mismatched.rows.find(row => row.symbol === 'hot').percentage_point_delta, null);
  assert.match(mismatched.warnings.join(' '), /采样目标\/范围不一致/);
});

test('FlameGraph SVG parser accepts title samples and report HTML escapes user-controlled fields', () => {
  const svg = `<svg><g class="func_g"><title>all (100 samples, 100.00%)</title></g><g><title>tcp_send_csum&lt;bad&gt; (20 samples, 20.00%)</title></g></svg>`;
  const parsed = core.parseEvidenceText({filename: 'cycles.svg', content: svg});
  assert.equal(parsed.detected_kind, 'perf');
  assert.equal(parsed.parsed.format, 'flamegraph-svg');
  assert.equal(parsed.parsed.symbols[0].symbol, 'tcp_send_csum<bad>');
  const analysis = core.buildAnalysis([{id: 'x', role: 'baseline', filename: '<img src=x onerror=1>.svg', bytes: svg.length, sha256: '<hash>', imported_at: 'now', ...parsed}], {title: '<script>alert(1)</script>', baseline: {}, scaled: {}, host: {}});
  const html = core.buildHtmlReport(analysis);
  const markdown = core.buildMarkdownReport(analysis);
  assert.doesNotMatch(html, /<script>alert/);
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.match(html, /default-src 'none'/);
  assert.doesNotMatch(markdown, /<script>|<img/);
  assert.match(markdown, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.match(markdown, /&lt;img src=x onerror=1&gt;\.svg/);
});

test('a FlameGraph profile made entirely of unknown symbols is retained', () => {
  const parsed = core.parseEvidenceText({filename: 'cycles.svg', content: `<svg><g><title>[unknown] (80 samples, 100.00%)</title></g></svg>`}).parsed;
  assert.equal(parsed.symbols.length, 1);
  assert.equal(parsed.symbols[0].symbol, '[unknown]');
  assert.equal(parsed.symbols[0].percent, 100);
});

test('perf report without total samples retains percentages and leaves absolute values null', () => {
  const report = `# event: cycles\n# Overhead  Command  Shared Object  Symbol\n  20.00%  dpvs  dpvs  [.] tcp_send_csum\n  10.00%  dpvs  dpvs  [.] business\n`;
  const parsed = core.parseEvidenceText({filename: 'perf-cycles.report', content: report}).parsed;
  assert.equal(parsed.format, 'perf-report');
  const checksum = parsed.symbols.find(row => row.symbol === 'tcp_send_csum');
  assert.equal(checksum.percent, 20);
  assert.equal(checksum.samples, null);
  assert.match(parsed.warnings.join(' '), /绝对样本差/);
});

test('perf report event totals are not multiplied by Overhead percentages to invent function samples', () => {
  const report = `# Samples: 1K of event 'cycles'\n# Overhead Command Shared Object Symbol\n  20.04% dpvs dpvs [.] tcp_send_csum\n`;
  const parsed = core.parseEvidenceText({filename: 'perf-cycles.report', content: report}).parsed;
  const checksum = parsed.symbols.find(row => row.symbol === 'tcp_send_csum');
  assert.equal(parsed.total_samples, 1000);
  assert.equal(parsed.source_has_absolute, false);
  assert.equal(checksum.samples, null);
  assert.equal(checksum.absolute_source, null);
  assert.match(parsed.warnings.join(' '), /Overhead 可能按 period 加权/);
  const artifact = evidence('pt', 'baseline', 'perf-cycles.report', report, {sampling_target: 'all-CPU'});
  const analysis = core.buildAnalysis([artifact], {analysis_id: 'perf-total', baseline: {}, scaled: {}, host: {}});
  assert.equal(analysis.provenance[0].total_samples, 1000);
});

test('perf report Samples column takes precedence over percentage-derived estimates', () => {
  const report = `# Samples: 1K of event 'cycles'\n# Overhead  Samples  Command  Shared Object  Symbol\n  20.04%      200  dpvs  dpvs  [.] tcp_send_csum\n`;
  const parsed = core.parseEvidenceText({filename: 'perf-cycles.report', content: report}).parsed;
  const checksum = parsed.symbols.find(row => row.symbol === 'tcp_send_csum');
  assert.equal(checksum.samples, 200);
  assert.equal(checksum.absolute_source, 'report_samples_column');
  assert.match(parsed.warnings.join(' '), /Samples 列/);
});

test('multi-event perf reports are retained only as unparsed evidence instead of merging denominators', () => {
  const report = `# Samples: 100 of event 'cycles'\n# Overhead Samples Command Shared Object Symbol\n  20.00% 20 dpvs dpvs [.] cpu_hot\n# Samples: 100 of event 'cache-misses'\n# Overhead Samples Command Shared Object Symbol\n  80.00% 80 dpvs dpvs [.] miss_hot\n`;
  const result = core.parseEvidenceText({filename: 'perf-multiple.report', content: report});
  assert.equal(result.detected_kind, 'unparsed_text');
  assert.equal(result.parsed.candidate_kind, 'perf');
  assert.match(result.parsed.parser_error, /包含 2 个事件块/);
  assert.match(result.parsed.parser_error, /分别导出文件/);
});

test('Self and Inclusive perf profiles are not compared as one metric', () => {
  const report = `# Samples: 100 of event 'cycles'\n# Overhead Samples Command Shared Object Symbol\n  20.00% 20 dpvs dpvs [.] hot\n`;
  const baseline = evidence('b1', 'baseline', 'cycles.report', report, {duration_seconds: 1, sampling_config: 'freq=99', sampling_target: 'all-CPU'});
  const scaled = evidence('b2', 'scaled', 'cycles.folded', 'main;hot 20\nmain;cold 80\n', {duration_seconds: 1, sampling_config: 'freq=99', sampling_target: 'all-CPU'});
  const pair = core.comparePerf([baseline, scaled], {baseline: {scope: 'per_node'}, scaled: {scope: 'per_node'}})[0];
  const hot = pair.rows.find(row => row.symbol === 'hot');
  assert.equal(pair.status, 'not_comparable');
  assert.equal(pair.baseline_basis, 'self');
  assert.equal(pair.scaled_basis, 'inclusive');
  assert.equal(hot.absolute_sample_delta, null);
  assert.equal(hot.absolute_rate_delta, null);
  assert.equal(hot.percentage_point_delta, null);
  assert.equal(hot.relative_percent_change, null);
  assert.match(pair.warnings.join(' '), /Self 与 Inclusive 不可混比/);
});

test('unknown symbols are retained and unknown events never produce deltas', () => {
  const report = `# event: cycles\n# Overhead Command Shared Object Symbol\n  80.00% dpvs dpvs [.] [unknown]\n  20.00% dpvs dpvs [.] known\n`;
  const profile = core.parseEvidenceText({filename: 'cycles.report', content: report}).parsed;
  assert(profile.symbols.some(row => row.symbol === '[unknown]'));
  const baseline = evidence('u1', 'baseline', 'baseline.folded', 'main;hot 10\n', {duration_seconds: 1, sampling_config: 'freq=99'});
  const scaled = evidence('u2', 'scaled', 'scaled.folded', 'main;hot 20\n', {duration_seconds: 1, sampling_config: 'freq=99'});
  const pair = core.comparePerf([baseline, scaled], {baseline: {scope: 'per_node'}, scaled: {scope: 'per_node'}})[0];
  assert.equal(pair.event, 'unknown');
  assert.equal(pair.status, 'not_comparable');
  assert.equal(pair.rows[0].absolute_sample_delta, null);
  assert.equal(pair.rows[0].absolute_rate_delta, null);
  assert.equal(pair.rows[0].percentage_point_delta, null);
  assert.match(pair.warnings.join(' '), /填写明确事件名/);
});

test('sar parser compares CPU and network Average rows while preserving a missing side', () => {
  const baselineSar = `Linux host\n12:00:00 CPU %user %nice %system %iowait %steal %idle\n12:00:01 all 10 0 20 1 0 69\n12:00:01 3 40 0 30 1 0 29\nAverage: all 12 0 18 1 0 69\nAverage: 3 42 0 31 1 0 26\n\n12:00:00 IFACE rxpck/s txpck/s rxkB/s txkB/s\nAverage: eth0 100 80 50 40\n`;
  const scaledSar = `12:00:00 CPU %user %nice %system %iowait %steal %idle\nAverage: all 20 0 30 2 0 48\nAverage: 3 50 0 35 2 0 13\n`;
  const left = evidence('e', 'baseline', 'sar.txt', baselineSar);
  const right = evidence('f', 'scaled', 'sar.txt', scaledSar);
  const rows = core.compareSar([left, right], {
    baseline: {sar_window_signature: 'steady-60s'}, scaled: {sar_window_signature: 'steady-60s'},
  });
  const system = rows.find(row => row.category === 'cpu' && row.device === 'all' && row.metric === '%system');
  assert.equal(system.baseline, 18);
  assert.equal(system.scaled, 30);
  assert.equal(system.absolute_delta, 12);
  const cpu3 = rows.find(row => row.category === 'cpu' && row.device === '3' && row.metric === '%system');
  assert.equal(cpu3.baseline, 31);
  assert.equal(cpu3.scaled, 35);
  const network = rows.find(row => row.category === 'network' && row.metric === 'rxpck/s');
  assert.equal(network.status, 'baseline_only');
  assert.equal(network.scaled, null);
});

test('SAR windows are not auto-aligned without explicit matching scenario metadata', () => {
  const baseline = evidence('w1', 'baseline', 'sar-baseline.txt', `12:00:00 CPU %user %system %idle\nAverage: all 10 20 70\n`);
  const scaled = evidence('w2', 'scaled', 'sar-scaled.txt', `12:00:00 CPU %user %system %idle\nAverage: all 20 30 50\n`);
  const row = core.compareSar([baseline, scaled]).find(item => item.metric === '%system');
  assert.equal(row.status, 'windows_not_aligned');
  assert.equal(row.baseline, 20);
  assert.equal(row.scaled, 30);
  assert.equal(row.absolute_delta, null);
  assert.match(row.warning, /不会自动假设/);
});

test('SAR restart boundaries and variable intervals invalidate aggregation, but a pre-sample restart marker does not', () => {
  const crossRestart = evidence('r1', 'baseline', 'sar-restart.txt', `12:00:00 CPU %user %system %idle\n12:00:01 all 10 20 70\n12:00:02 all 11 21 68\n12:00:03 LINUX RESTART (8 CPU)\n12:00:04 CPU %user %system %idle\n12:00:05 all 12 22 66\n12:00:06 all 13 23 64\n`);
  assert.equal(crossRestart.parsed.window.status, 'restart_boundary');
  assert.equal(crossRestart.parsed.window.restart_boundaries, 1);
  assert(crossRestart.parsed.metrics.every(metric => metric.value === null));

  const variable = evidence('r2', 'scaled', 'sar-variable.txt', `12:00:00 CPU %user %system %idle\n12:00:01 all 10 20 70\n12:00:02 all 11 21 68\n12:00:04 all 12 22 66\n`);
  assert.equal(variable.parsed.window.status, 'variable_intervals');
  assert.deepEqual(variable.parsed.window.observed_intervals_seconds, [1, 2]);
  assert(variable.parsed.metrics.every(metric => metric.value === null));
  const invalid = core.compareSar([crossRestart, variable], {
    baseline: {sar_window_signature: 'steady'}, scaled: {sar_window_signature: 'steady'},
  }).find(item => item.metric === '%system');
  assert.equal(invalid.status, 'source_window_not_comparable');
  assert.equal(invalid.baseline, null);
  assert.equal(invalid.scaled, null);
  assert.equal(invalid.absolute_delta, null);

  const preSampleRestart = evidence('r3', 'baseline', 'sar-boot-header.txt', `11:59:59 LINUX RESTART (8 CPU)\n12:00:00 CPU %user %system %idle\n12:00:01 all 10 20 70\n12:00:02 all 12 22 66\n`);
  assert.equal(preSampleRestart.parsed.window.status, 'regular_or_average');
  assert.equal(preSampleRestart.parsed.window.restart_boundaries, 0);
  assert.equal(preSampleRestart.parsed.window.ignored_restart_markers, 1);
  assert.equal(preSampleRestart.parsed.metrics.find(metric => metric.metric === '%system').value, 21);
});

test('duplicate SAR sources for one role/entity/metric are not averaged silently', () => {
  const one = evidence('s1', 'baseline', 'sar-one.txt', `12:00 CPU %user %system %idle\nAverage: all 10 20 70\n`);
  const two = evidence('s2', 'baseline', 'sar-two.txt', `12:00 CPU %user %system %idle\nAverage: all 30 40 30\n`);
  const scaled = evidence('s3', 'scaled', 'sar-scaled.txt', `12:00 CPU %user %system %idle\nAverage: all 20 30 50\n`);
  const row = core.compareSar([one, two, scaled]).find(item => item.metric === '%system');
  assert.equal(row.status, 'multiple_sources_not_merged');
  assert.equal(row.baseline, null);
  assert.equal(row.scaled, null);
  assert.equal(row.absolute_delta, null);
  assert.match(row.warning, /sar-one\.txt/);
  assert.match(row.warning, /sar-two\.txt/);
});

test('Host SAR remains a first-class bounded observation in analysis, reports, and Agent payload', () => {
  const host = evidence('hs', 'host', 'host-sar.txt', `12:00:00 IFACE rxpck/s txpck/s rxkB/s txkB/s\nAverage: eth0 100 80 50 40\n`);
  const analysis = core.buildAnalysis([host], {
    analysis_id: 'host-sar-analysis', baseline: {node_count: 1}, scaled: {node_count: 16}, host: {node_count: 1},
  });
  const row = analysis.sar_comparison.find(item => item.category === 'network' && item.metric === 'rxpck/s');
  assert.equal(analysis.coverage.host.sar_network, true);
  assert.equal(row.status, 'guest_missing');
  assert.equal(row.host, 100);
  assert.equal(row.host_status, 'observation');
  assert.equal(analysis.host_sar_observations.length, 4);
  assert.equal(analysis.host_sar_observations.find(item => item.metric === 'rxpck/s').value, 100);
  assert.match(core.buildMarkdownReport(analysis), /Host 单机/);
  assert.match(core.buildHtmlReport(analysis), /SAR 对比与 Host 观测/);
  const payload = core.buildAgentPayload(analysis).payload;
  assert.equal(payload.host_sar_observations.find(item => item.metric === 'rxpck/s').value, 100);

  const duplicate = evidence('hs2', 'host', 'host-sar-two.txt', `12:00:00 IFACE rxpck/s txpck/s\nAverage: eth0 200 160\n`);
  const duplicateRow = core.compareSar([host, duplicate]).find(item => item.metric === 'rxpck/s');
  assert.equal(duplicateRow.host_status, 'multiple_sources_not_merged');
  assert.equal(duplicateRow.host, null);
  assert.match(duplicateRow.host_warning, /未自动平均/);
});

test('multiple perf files for one role and event are never ignored silently', () => {
  const first = evidence('h', 'baseline', 'first-cycles.folded', 'main;one 10\n');
  const duplicate = evidence('i', 'baseline', 'second-cycles.folded', 'main;two 20\n');
  const scaled = evidence('j', 'scaled', 'scaled-cycles.folded', 'main;one 30\n');
  const pair = core.comparePerf([first, duplicate, scaled], {baseline: {scope: 'per_node'}, scaled: {scope: 'per_node'}})[0];
  assert.equal(pair.baseline_evidence_id, 'h');
  assert.match(pair.warnings.join(' '), /只使用最早导入/);
  assert.match(pair.warnings.join(' '), /second-cycles\.folded/);
  assert.equal(pair.rows.some(row => row.symbol === 'two'), false);
});

test('linear scaling uses cluster totals and reports 7x / 43.75 percent efficiency for 1 to 16 nodes', () => {
  const scaling = core.buildScalingAnalysis({
    baseline: {node_count: 1, cps: 100000},
    scaled: {node_count: 16, cps: 700000},
  });
  const cps = scaling.metrics.find(metric => metric.metric === 'cps');
  assert.equal(scaling.node_ratio, 16);
  assert.equal(cps.scope, 'cluster_total');
  assert.equal(cps.speedup, 7);
  assert.equal(cps.efficiency_percent, 43.75);
});

test('analysis follows the skill contract and bounds the Agent evidence summary', () => {
  const baseline = evidence('k', 'baseline', 'cycles.folded', 'main;hot 10\n');
  const scaled = evidence('l', 'scaled', 'cycles.folded', 'main;hot 20\n');
  const analysis = core.buildAnalysis([baseline, scaled], {
    analysis_id: 'local-session-1', title: 'contract',
    baseline: {node_count: 1, scope: 'per_node'}, scaled: {node_count: 16, scope: 'per_node'}, host: {},
  });
  assert.equal(analysis.analysis_id, 'local-session-1');
  assert.equal(analysis.manifest.scenarios.baseline.role, 'single_node_guest');
  assert.equal(analysis.manifest.scenarios.scaled.role, 'sixteen_node_guest');
  assert.equal(analysis.provenance[0].parser, 'perf_folded_stacks_v1');
  assert.equal(analysis.provenance[0].confidence, 'high');
  analysis.perf_comparison = Array.from({length: 20}, (_, index) => ({event: `event-${index}`, status: 'paired', warnings: [], rows: Array.from({length: 100}, (__, row) => ({symbol: `function-${index}-${row}`, absolute_sample_delta: row}))}));
  analysis.sar_comparison = Array.from({length: 300}, (_, index) => ({metric: `metric-${index}`, baseline: index, scaled: index + 1}));
  analysis.host_sar_observations = Array.from({length: 300}, (_, index) => ({metric: `host-metric-${index}`, value: index}));
  const prepared = core.buildAgentPayload(analysis);
  assert(prepared.bytes < 256 * 1024);
  assert(prepared.payload.perf_comparison.length <= 8);
  assert(prepared.payload.perf_comparison.every(item => item.rows.length <= 50));
  assert(prepared.payload.sar_comparison.length <= 100);
  assert(prepared.payload.host_sar_observations.length <= 100);
  assert.equal(prepared.payload.provenance.length, analysis.provenance.length);
  assert.equal(prepared.truncated, true);
  assert.match(prepared.payload.limitations.join(' '), /Agent 证据摘要/);
});

test('numastat assessment qualifies cumulative allocation counters', () => {
  const input = `                           node0           node1\nnuma_hit                 100000          100000\nnuma_miss                    10              20\nnuma_foreign                 20              10\ninterleave_hit             1000            1000\nlocal_node                99000           99000\nother_node                 1000            1000\n`;
  const host = evidence('g', 'host', 'numastat.txt', input);
  const result = core.analyzeNuma([host]);
  assert.equal(result.confidence, 'medium');
  assert.ok(result.numa_miss_ratio_percent < 0.1);
  assert.match(result.assessment, /不能单独证明/);
  assert.match(result.warnings.join(' '), /累计值/);
});

test('multiple system numastat snapshots remain ambiguous instead of being summed', () => {
  const first = evidence('n1', 'host', 'numastat-before.txt', `node0 node1\nnuma_hit 100 100\nnuma_miss 1 1\nlocal_node 90 90\nother_node 10 10\n`);
  const second = evidence('n2', 'host', 'numastat-after.txt', `node0 node1\nnuma_hit 200 200\nnuma_miss 2 2\nlocal_node 180 180\nother_node 20 20\n`);
  const result = core.analyzeNuma([first, second]);
  assert.equal(result.status, 'ambiguous_multiple_snapshots');
  assert.equal(result.ambiguous_multiple_snapshots, true);
  assert.equal(result.numa_miss_ratio_percent, null);
  assert.equal(result.other_node_ratio_percent, null);
  assert.match(result.assessment, /不能求和/);
});

test('binary input is rejected while unknown text and locale-ambiguous SAR are retained as bounded candidates', () => {
  assert.throws(() => core.parseEvidenceText({filename: 'sa31', content: `Linux\0binary`}), /sadf|sar -f/);
  const unknown = core.parseEvidenceText({filename: 'vendor-platform.txt', content: `vendor header\n${'新格式数据'.repeat(5000)}`});
  assert.equal(unknown.detected_kind, 'unparsed_text');
  assert.equal(unknown.parsed.kind, 'unparsed_text');
  assert(new TextEncoder().encode(unknown.parsed.excerpt).byteLength <= 16 * 1024);
  assert.equal(unknown.parsed.excerpt_truncated, true);
  const locale = core.parseEvidenceText({filename: 'sar.txt', content: `12:00 CPU %user %system %idle\nAverage: all 1,23 2,50 96,27\n`});
  assert.equal(locale.detected_kind, 'unparsed_text');
  assert.match(locale.parsed.parser_error, /locale 歧义/);
  const item = {id: 'raw', role: 'baseline', filename: 'vendor-platform.txt', bytes: 10, sha256: 'hash', imported_at: 'now', ...unknown};
  const analysis = core.buildAnalysis([item], {analysis_id: 'local-raw', baseline: {node_count: 1}, scaled: {node_count: 16}, host: {}});
  assert.equal(analysis.coverage.baseline.perf, false);
  assert.equal(analysis.unparsed_evidence.length, 1);
  const payload = core.buildAgentPayload(analysis).payload;
  assert.equal(payload.unparsed_evidence[0].filename, 'vendor-platform.txt');
  assert.match(payload.limitations.join(' '), /未通过确定性解析/);
});
