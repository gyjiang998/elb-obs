const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const assert = require('node:assert/strict');
const ts = require('typescript');

const repositoryRoot = path.resolve(__dirname, '../..');

function loadReportModule() {
  const source = fs.readFileSync(path.join(repositoryRoot, 'app/lib/parallel-modeling-report.ts'), 'utf8');
  const output = ts.transpileModule(source, {
    compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022},
  }).outputText;
  const loaded = {exports: {}};
  new Function('module', 'exports', 'require', output)(loaded, loaded.exports, require);
  return loaded.exports;
}

const report = loadReportModule();

function fixture(overrides = {}) {
  const hardware = {
    schema_version: 'parallel_hardware_profile/v1', id: 'hw_01', name: 'Kirin test profile',
    profile_sha256: 'hardware-digest', physical_core_count: 9, logical_cpu_count: 14,
    core_groups: [
      {id: 'small', name: '小核组', physical_core_count: 4, logical_cpu_ids: [0, 1, 2, 3], opps_khz: [800000, 1800000]},
      {id: 'big', name: '大核组', physical_core_count: 2, logical_cpu_ids: [8, 9, 10, 11], opps_khz: [1200000, 2600000]},
    ],
  };
  const features = {
    schema_version: 'workload_feature_profile/v2', id: 'wf_01', name: '图库编辑 · 负载特征',
    dataset_id: 'ds_01', hardware_profile_id: 'hw_01', hardware_profile_sha256: 'hardware-digest',
    engine_version: '2.0', status: 'completed', profile_sha256: 'feature-digest', dataset_fingerprint: 'dataset-digest',
    summary: {stage_count: 2, stage_cpu_ms: 1.8, pmu_usable_stages: 2, classified_cpu_stages: 2},
    quality: {hardware_identity_verified: true}, provenance: {collector: 'test'},
    stages: [{
      id: 'build_00', display_id: 'B01', name: 'BuildItem', relative_start_ms: .4, wall_ms: 1.2, cpu_ms: 1,
      states_ms: {running: 1.2, runnable: .3, blocked: .5},
      compute: {instructions: 2400, cycles: 1200, ipc: 2}, memory: {cache_mpki: 1.2, llc_mpki: .2},
      scheduling: {cpu_placement: {primary_cpu: 8, primary_core_group_id: 'big', primary_core_group_name: '大核组'}},
      workload_class: 'compute', classification: {id: 'compute', evidence_quality: 'complete'},
    }, {
      id: 'build_01', display_id: 'B02', name: 'BuildItem memory', relative_start_ms: 2.2, wall_ms: 1.4, cpu_ms: .8,
      states_ms: {running: .8, runnable: .2, blocked: .4},
      compute: {instructions: 800, cycles: 1000, ipc: .8}, memory: {cache_mpki: 12.4, llc_mpki: 4.1},
      scheduling: {cpu_placement: {primary_cpu: 9, primary_core_group_id: 'big', primary_core_group_name: '大核组'}},
      workload_class: 'memory', classification: {id: 'memory', evidence_quality: 'complete'},
    }],
  };
  const dataset = {
    schema_version: 'parallel_modeling_dataset/v1', id: 'ds_01', name: '图库：编辑/保存*案例', status: 'ready',
    source_kind: 'capture_manifest', hardware_profile_id: 'hw_01', summary: {duration_ms: 4},
    provenance: {capture: {plan_title: '图库 17 步', step_id: 'step_07', step_title: '点击编辑'}},
  };
  const analysis = {
    schema_version: 'parallel_modeling_analysis/v1', id: 'an_01', dataset_id: 'ds_01', status: 'completed',
    feature_profile_id: 'wf_01', hardware_resolution: {hardware_profile_id: 'hw_01'}, workload_profile: features,
    baseline: {duration_ms: 4, app_cpu_ms: 2.2, app_off_cpu_ms: 1.8, service_cpu_ms: .2, builditem_count: 2, service_call_count: 1, replay_ms: 4, data_kind: 'measured'},
    analysis_provenance: {source_placement_cost_transfer: {status: 'verified', component_count: 2, verified_component_count: 2, fallback_component_count: 0}},
    components: [
      {id: 'build_00', display_id: 'B01', name: 'BuildItem', wall_ms: 2, cpu_ms: 1.2, binder_blocked_ms: .2, request_ids: ['r1']},
      {id: 'build_01', display_id: 'B02', name: 'BuildItem memory', wall_ms: 1.4, cpu_ms: .8, binder_blocked_ms: .3, request_ids: ['r2']},
    ],
  };
  const calibrationPlan = {
    schema_version: 'parallel_calibration_plan/v1', id: 'cp_01', name: 'Kirin test · 性能模型校准', status: 'planned',
    hardware_profile_id: 'hw_01', hardware_profile_sha256: 'hardware-digest', parameter_groups: ['frequency_roofline', 'pairwise_interference'],
    core_kinds: ['small', 'big'], repeats: 5, conditions: [{benchmark: 'cpu_work'}], expected_sample_count: 10,
  };
  const calibrationFit = {
    schema_version: 'parallel_calibration_fit/v1', id: 'cf_01', plan_id: 'cp_01', hardware_profile_id: 'hw_01', status: 'proxy_calibrated',
    measurements_sha256: 'measurement-digest', parameter_estimates: [{parameter: 'frequency_roofline', path: 'profiles.big.frequency_roofline', value: {compute: [1, 2]}, method: 'constrained_fit', sample_count: 10, status: 'calibrated'}],
    quality: {passed: true, publishable: true, accepted_samples: 10, rejected_samples: 0, train_samples: 8, validation_samples: 2, validation_median_ape: .04, validation_p90_ape: .07, evidence_grade: 'B', risk_level: 'low', risk_summary: '留出验证通过'},
    calibrated_parameters: ['profiles.big.frequency_roofline'], inherited_parameters: [], validation_predictions: [{actual: 1, predicted: 1.02}],
    provenance: {binary_sha256: 'measured-binary-digest'},
    frequency_curve_observations: {schema_version: 'calibration_frequency_curve_observations/v5', points: [
      {core_kind: 'big', workload_class: 'compute', frequency_khz: 1200000, unit_cpu_ms: .0032, repeat_count: 5},
      {core_kind: 'big', workload_class: 'compute', frequency_khz: 2600000, unit_cpu_ms: .0018, repeat_count: 5},
    ]},
    model_preview: {hardware_profile: hardware, profiles: {big: {max_khz: 2600000, frequency_roofline: {classes: {
      compute: {knee_khz: 2200000, above_knee_exponent: .18, below_knee_exponent: .92,
        frequency_domain_khz: [1200000, 1800000, 2200000, 2600000], reference_cpu_ms_per_work_unit: .0018},
    }}}}},
  };
  const search = {
    schema_version: 'parallel_modeling_search/v1', id: 'sr_01', analysis_id: 'an_01', dataset_id: 'ds_01',
    feature_profile_id: 'wf_01', status: 'completed', objective: 'minimum_latency',
    hardware_resolution: {hardware_profile_id: 'hw_01'},
    model_package: {id: 'cm_01', hardware_profile_id: 'hw_01', calibration_provenance: {version_id: 'cm_01', plan_id: 'cp_01', fit_id: 'cf_01', measurements_sha256: 'measurement-digest', model_sha256: 'model-digest'}},
    search_configuration: {objective: 'minimum_latency', model_package_id: 'cm_01', feature_profile_id: 'wf_01', candidate_ids: ['build_00'], robustness: true, dependency_policy: 'preserve_observed', space: {worker_counts: [1, 2]}},
    search_provenance: {evaluated_configurations: 4, feasible_configurations: 4, core_combination_count: 1, scope: 'Exhaustive within requested configurations', hardware_profile_sha256: 'hardware-digest', model_sha256: 'model-digest', source_placement_cost_transfer: {status: 'verified', component_count: 2, verified_component_count: 2, fallback_component_count: 0}},
    best: {configuration: {main: {kind: 'big'}, workers: [{kind: 'big'}], main_participates: true}, metrics: {finish_ms: 2.4, app_core_ms: 2.1}, comparison: {latency_gain_pct: 40, app_cpu_delta_ms: -.1}, frequency_vector: {big: 2600000}},
    core_combination_recommendations: [{rank: 1, combination_id: 'big_2', combination_label: '2 大核', physical_core_count: 2, metrics: {finish_ms: 2.4}, latency_gap_ms: 0, latency_gap_pct: 0, frequency_vector: {big: 2600000}, selection_status: 'objective_optimum'}],
    candidates: [{configuration: {main: {kind: 'big'}, workers: [{kind: 'big'}], main_participates: true}, metrics: {finish_ms: 2.4, app_core_ms: 2.1}, comparison: {latency_gain_pct: 40}, frequency_vector: {big: 2600000}, feasible: true}],
  };
  return {
    dataset, features, hardware,
    calibrationCatalog: {model_structure: {name: '并行成本模型', status: 'calibratable', formula: 'T = Ton + Toff'}, benchmark_catalog: [{id: 'frequency_roofline', name: '频点性能曲线', description: '扫描核组与 OPP', parameters: ['frequency_roofline']}], parameter_catalog: [{id: 'frequency_roofline'}], measurement_contract: {schema_version: 'parallel_calibration_measurement/v1'}},
    calibrationPlan, calibrationFit,
    calibrationBundle: {schema_version: 'parallel_calibration_bundle/v1', plan_id: 'cp_01', version: 'assets-7', bundle_sha256: 'bundle-digest', generated_at: '2026-10-30T00:00:00.000Z', manifest: {implementation_version: 'calibration-assets/7', sha256: 'manifest-digest'}, files: [{filename: 'benchmark.c', media_type: 'text/x-c', sha256: 'code-digest', content: 'int main(void) { return 0; }'}]},
    analysis, search, generatedAt: '2026-10-31T03:00:00.000Z', warnings: ['并行结果属于模型预测。'],
    ...overrides,
  };
}

test('builds one offline HTML file with the five required evidence sections', () => {
  const artifact = report.buildParallelModelingReport(fixture());
  assert.equal(artifact.mimeType, 'text/html;charset=utf-8');
  assert.match(artifact.filename, /^并行建模报告-/);
  assert.ok(!/[<>:"/\\|?*]/.test(artifact.filename), 'filename must be safe on Windows');

  const html = artifact.html;
  assert.match(html, /01[\s\S]*负载场景/);
  assert.match(html, /02[\s\S]*负载特征分析与提取/);
  assert.match(html, /03[\s\S]*硬件模型/);
  assert.match(html, /04[\s\S]*寻优校准方案、代码与结果/);
  assert.match(html, /05[\s\S]*并行建模寻优方案、公式与结果/);
  assert.match(html, /int main\(void\) \{ return 0; \}/, 'calibration source should be embedded');
  assert.match(html, /lex minₓ \( T̂_UI\(x\), N_slot\(x\), C_app\(x\), i\(x\) \)/);
  assert.match(html, /2\.4 ms/);
  assert.equal((html.match(/class="chapter-introduction"/g) ?? []).length, 5,
    'every report chapter should explain its purpose in prose');
  const firstSection = html.slice(html.indexOf('id="section-1"'), html.indexOf('id="section-2"'));
  const secondSection = html.slice(html.indexOf('id="section-2"'), html.indexOf('id="section-3"'));
  assert.doesNotMatch(firstSection, /BuildItem 组件阶段账目/);
  assert.match(secondSection, /BuildItem 组件阶段账目/);
  assert.match(secondSection, /type="radio" name="stage-ledger-selection"/);
  assert.match(secondSection, /for="stage-ledger-1"/);
  assert.match(secondSection, /B02/);
  assert.match(secondSection, /left:10\.0000%;width:30\.0000%/,
    'the timeline must preserve the first BuildItem start and duration inside the 4 ms callback');
  assert.match(secondSection, /灰底留白为 BuildItem 外工作/);
  assert.match(html, /<svg[^>]+role="img"/);
  assert.match(html, /实测 2 OPP/);
  assert.doesNotMatch(html, /(?:NaN|Infinity)/, 'generated SVG coordinates must stay finite');
  assert.match(html, /证据链核验通过/);
  assert.match(html, /Content-Security-Policy/);
  assert.doesNotMatch(html, /<script\b/i);
  assert.doesNotMatch(html, /(?:src|href)=["']https?:/i);
});

test('escapes scenario, warning, raw JSON and calibration code without executable markup', () => {
  const input = fixture();
  input.dataset.name = '<img src=x onerror=alert(1)>';
  input.features.provenance = {hostile: '</pre><script>alert(2)</script>'};
  input.calibrationBundle.files[0].content = '</code></pre><script>alert(3)</script>';
  input.warnings = ['<svg onload=alert(4)>'];
  input.hardware.core_groups[1].name = '"><svg onload=alert(5)>';
  const html = report.buildParallelModelingHtmlReport(input);

  assert.doesNotMatch(html, /<script\b/i);
  assert.doesNotMatch(html, /<img\b/i);
  assert.doesNotMatch(html, /<svg\s+onload/i);
  assert.match(html, /<svg[^>]+role="img"/, 'the report should retain its own static SVG chart');
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.match(html, /&lt;script&gt;alert\(3\)&lt;\/script&gt;/);
  assert.match(html, /&lt;svg onload=alert\(4\)&gt;/);
  assert.match(html, /&quot;&gt;&lt;svg onload=alert\(5\)&gt;/);
});

test('orders calibration and optimization chapters for decision-first reading', () => {
  const html = report.buildParallelModelingHtmlReport(fixture());
  const calibration = html.slice(html.indexOf('id="section-4"'), html.indexOf('id="section-5"'));
  const curve = calibration.indexOf('任务类型频点性能曲线');
  const calibrationDetails = calibration.indexOf('<details class="report-details calibration-details">');
  assert.ok(curve >= 0 && calibrationDetails > curve, 'curves should precede collapsed calibration details');
  assert.doesNotMatch(calibration.slice(calibrationDetails, calibrationDetails + 90), /\sopen(?:\s|>)/,
    'calibration details should be collapsed by default');

  const optimization = html.slice(html.indexOf('id="section-5"'));
  const results = optimization.indexOf('寻优结果');
  const taskFormula = optimization.indexOf('单任务核频执行时间估算');
  const objectiveFormula = optimization.indexOf('并行配置寻优函数');
  const searchEvidence = optimization.indexOf('<details class="report-details search-details">');
  assert.ok(results >= 0 && taskFormula > results && objectiveFormula > taskFormula && searchEvidence > objectiveFormula,
    'parallel modeling should read as explanation, results, task cost, objective, then folded evidence');
  assert.match(optimization, /R<sub>src→ref<\/sub>/);
  assert.match(optimization, /T̂<sub>on<\/sub>\(k,c,f\)/);
  assert.match(optimization, /makespan\(DAG/);
  assert.match(optimization, /lex minₓ \( T̂_UI\(x\), N_slot\(x\), C_app\(x\), i\(x\) \)/);
});

test('renders the recorded source-cost branch instead of claiming verified normalization for user-bound hardware', () => {
  const input = fixture();
  input.analysis.analysis_provenance.source_placement_cost_transfer = {
    status: 'fallback_reference_anchor', component_count: 2, verified_component_count: 0,
    fallback_component_count: 2, fallback_reasons: {source_hardware_identity_unverified: 2},
  };
  input.search.search_provenance.source_placement_cost_transfer = input.analysis.analysis_provenance.source_placement_cost_transfer;
  const html = report.buildParallelModelingHtmlReport(input);
  const optimization = html.slice(html.indexOf('id="section-5"'));
  assert.match(optimization, /阶段核型与频点时延估算 · 观测参考锚回退/);
  assert.match(optimization, /W<sub>ref<\/sub> = T<sub>on,obs<\/sub>（观测参考锚回退）/);
  assert.match(optimization, /source_hardware_identity_unverified/);
  assert.match(optimization, /手工绑定的硬件画像只用于目标拓扑与 OPP/);
});

test('prefers the winning candidate transfer branch over a verified source-side summary', () => {
  const input = fixture();
  input.search.best.source_placement_cost_transfer = {
    status: 'fallback_reference_anchor', verified_components: [], fallback_components: ['build_00'],
    fallback_reasons: {target_transfer_reference_mismatch: 1.2},
  };
  const html = report.buildParallelModelingHtmlReport(input);
  const optimization = html.slice(html.indexOf('id="section-5"'));
  assert.match(optimization, /阶段核型与频点时延估算 · 观测参考锚回退/);
  assert.match(optimization, /W<sub>ref<\/sub> = T<sub>on,obs<\/sub>（观测参考锚回退）/);
  assert.match(optimization, /target_transfer_reference_mismatch/);
  assert.doesNotMatch(optimization, /本次记录已用阶段实际 CPU 驻留和对应源曲线归一到参考工作量/);
});

test('reports lineage mismatches and labels absent evidence instead of inventing it', () => {
  const input = fixture({
    features: undefined,
    hardware: undefined,
    calibrationPlan: undefined,
    calibrationFit: undefined,
    calibrationBundle: undefined,
  });
  delete input.analysis.workload_profile;
  delete input.search.workload_profile;
  input.search.analysis_id = 'an_other';
  const issues = report.validateParallelModelingReportLineage(input);
  assert.ok(issues.some(issue => issue.code === 'analysis_search' && issue.severity === 'error'));
  const html = report.buildParallelModelingHtmlReport(input);
  assert.match(html, /证据链核验发现/);
  assert.match(html, /证据未提供 · 负载特征版本/);
  assert.match(html, /证据未提供 · 硬件模型/);
  assert.match(html, /证据未提供 · 校准方案/);
  assert.match(html, /证据未提供 · 校准拟合结果/);
});

test('presents the budget objective with exact lexicographic semantics and robust constraint', () => {
  const input = fixture();
  input.search.objective = 'minimum_frequency_with_budget';
  input.search.budget_ms = 3;
  input.search.search_configuration.objective = 'minimum_frequency_with_budget';
  input.search.search_configuration.budget_ms = 3;
  const model = report.optimizationModelPresentation(input.search);
  assert.equal(model.objectiveName, '预算内最低频点 · 字典序');
  assert.match(model.objectiveFormula, /max g∈A\(x\) r_g/);
  assert.deepEqual(model.constraints, ['T̂_UI(x) ≤ 3 ms', 'max s∈S_decl T̂_UI(x; s) ≤ 3 ms', 'x ∈ Ω_current']);
  assert.match(model.explanation, /不等同于能耗最小/);
});

test('labels bounded search and fit projections without claiming a complete transported result', () => {
  const input = fixture();
  input.search.transport_projection = {
    schema_version: 'desktop_search_projection/v1',
    full_result_persisted: true,
    returned_candidates: 100,
    total_candidates: 420,
    omitted: ['non_winner.graph', 'candidate.node_times'],
  };
  input.calibrationFit.transport_projection = {
    schema_version: 'desktop_calibration_fit_projection/v1',
    full_result_persisted: true,
    returned_candidates: 1,
    total_candidates: 12,
    omitted: ['measurements', 'validation_predictions'],
  };
  delete input.calibrationFit.validation_predictions;

  const html = report.buildParallelModelingHtmlReport(input);
  assert.match(html, /传输覆盖边界 · 并行寻优/);
  assert.match(html, /当前传输 100 \/ 共 420 个候选/);
  assert.match(html, /non_winner\.graph、candidate\.node_times/);
  assert.match(html, /传输覆盖边界 · 校准拟合/);
  assert.match(html, /当前传输 1 \/ 共 12 个候选/);
  assert.match(html, /measurements、validation_predictions/);
  assert.match(html, /<h3 class="subheading">当前传输候选<\/h3>/);
  assert.match(html, /校准质量与模型预览 · 原始证据/);
  assert.doesNotMatch(html, /寻优来源、假设与完整候选/);
  assert.doesNotMatch(html, /校准质量、验证预测与模型预览/);
});

test('identifies calibration code as rebuilt from current deployment assets and exposes bundle identity', () => {
  const html = report.buildParallelModelingHtmlReport(fixture());
  assert.match(html, /代码来源 · 按历史方案由当前部署资产重建/);
  assert.match(html, /不等同于实际测量时冻结的源码/);
  assert.match(html, /assets-7/);
  assert.match(html, /bundle-digest/);
  assert.match(html, /calibration-assets\/7/);
  assert.match(html, /manifest-digest/);
  assert.match(html, /measured-binary-digest/);
  assert.doesNotMatch(html, /冻结实测源码/);
});

test('validates hardware, measurement and model hashes across the frozen lineage', () => {
  const input = fixture();
  input.search.search_provenance.hardware_profile_sha256 = 'other-hardware-digest';
  input.search.model_package.calibration_provenance.measurements_sha256 = 'other-measurement-digest';
  input.search.search_provenance.model_sha256 = 'other-model-digest';
  input.search.model_package.calibration_provenance.version_id = 'cm_other';

  const issues = report.validateParallelModelingReportLineage(input);
  for (const code of [
    'hardware_hash_search',
    'measurements_hash_model',
    'model_hash_search',
    'calibration_version_model',
  ]) {
    assert.ok(issues.some(issue => issue.code === code && issue.severity === 'error'), `missing ${code}`);
  }
});

test('validates feature identities frozen at top level, configuration and embedded snapshots', () => {
  const input = fixture();
  input.search.workload_profile = {...input.features, id: 'wf_other'};
  input.analysis.workload_profile = {...input.features, id: 'wf_analysis_other'};
  const issues = report.validateParallelModelingReportLineage(input);
  assert.ok(issues.some(issue => issue.code === 'features_search_snapshot' && issue.severity === 'error'));
  assert.ok(issues.some(issue => issue.code === 'search_feature_snapshot' && issue.severity === 'error'));
  assert.ok(issues.some(issue => issue.code === 'analysis_feature_snapshot' && issue.severity === 'error'));
});

test('treats the search feature as authoritative when it legitimately overrides the analysis feature', () => {
  const input = fixture();
  const searchFeature = {...input.features, id: 'wf_search'};
  input.features = searchFeature;
  input.search.feature_profile_id = 'wf_search';
  input.search.search_configuration.feature_profile_id = 'wf_search';
  input.search.workload_profile = searchFeature;
  const issues = report.validateParallelModelingReportLineage(input);
  assert.ok(!issues.some(issue => ['features_analysis', 'features_analysis_snapshot'].includes(issue.code)),
    'analysis may retain the feature used for baseline analysis while search freezes a newer same-dataset feature');
  assert.ok(!issues.some(issue => issue.code.startsWith('features_search') || issue.code.startsWith('search_feature')));

  input.search.feature_profile_id = null;
  input.search.search_configuration.feature_profile_id = null;
  input.search.workload_profile = null;
  const leakedFeatureIssues = report.validateParallelModelingReportLineage(input);
  assert.ok(leakedFeatureIssues.some(issue => issue.code === 'features_search_contract_none' && issue.severity === 'error'));
  input.features = undefined;
  const noFeatureIssues = report.validateParallelModelingReportLineage(input);
  assert.ok(!noFeatureIssues.some(issue => issue.code === 'features_analysis'),
    'an explicit no-feature search must not fall back to the analysis feature');
  const html = report.buildParallelModelingHtmlReport(input);
  const featureSection = html.slice(html.indexOf('id="section-2"'), html.indexOf('id="section-3"'));
  assert.match(featureSection, /证据未提供 · 负载特征版本/);
  assert.doesNotMatch(featureSection, /wf_01|阶段 CPU 时间|Running 分型/,
    'the report must not revive the parent analysis feature after an explicit no-feature search');
});

test('normalizes Windows reserved names, illegal characters and trailing dots', () => {
  assert.equal(report.windowsSafeFilename('CON.html'), '_CON.html');
  assert.equal(report.windowsSafeFilename('a<b>:c/?.html'), 'a-b--c--.html');
  assert.equal(report.windowsSafeFilename('name. .html'), 'name.html');
  assert.ok(report.windowsSafeFilename('x'.repeat(300) + '.html').length <= 120);
});

test('parallel modeling exports the frozen saved lineage and Desktop uses native save', () => {
  const studio = fs.readFileSync(path.join(repositoryRoot, 'app/components/ParallelModelingStudio.tsx'), 'utf8');
  const desktop = fs.readFileSync(path.join(repositoryRoot, 'desktop/renderer/shared-pages.jsx'), 'utf8');
  assert.match(studio, /function exportHtmlReport\(\)/);
  assert.match(studio, /parallel-modeling\/searches\/\$\{encodeURIComponent\(search\.id\)\}/,
    'the report must begin from the selected persisted search');
  assert.match(studio, /parallel-modeling\/analyses\/\$\{encodeURIComponent\(frozenSearch\.analysis_id\)\}/);
  assert.match(studio, /parallel-modeling\/datasets\/\$\{encodeURIComponent\(frozenAnalysis\.dataset_id\)\}/);
  assert.match(studio, /workload-features\/profiles\/\$\{encodeURIComponent\(featureId\)\}/);
  assert.match(studio, /text\(searchConfiguration\.feature_profile_id\)/,
    'report export must resolve the feature version frozen inside search_configuration');
  assert.match(studio, /const featureSnapshot = record\(frozenSearch\.workload_profile\)/,
    'embedded frozen feature snapshots must remain a valid identity fallback');
  assert.match(studio, /new Set\(searchFeatureIds\)\.size > 1/,
    'conflicting frozen feature identities must stop export instead of mixing records');
  const searchSnapshot = studio.indexOf('text(featureSnapshot.id)');
  const analysisTopLevel = studio.indexOf('text(frozenAnalysis.feature_profile_id)');
  assert.ok(searchSnapshot >= 0 && analysisTopLevel > searchSnapshot,
    'the search snapshot must take precedence over analysis-only feature fallbacks');
  assert.match(studio, /const featureId = searchHasFeatureContract \? explicitSearchFeatureId/,
    'an explicit no-feature search must not inherit the analysis feature');
  assert.match(studio, /features: featureId \? featureResult \?\? embeddedFeatureSnapshot : undefined/,
    'report input must omit analysis snapshots when the saved search explicitly used no feature profile');
  const hardwareHandler = studio.slice(studio.indexOf('function selectAnalysisHardware'), studio.indexOf('const errorForwarded'));
  assert.doesNotMatch(hardwareHandler, /setFeatureProfileId\(''\)/,
    'changing target hardware must preserve the current feature as the source for immutable re-extraction');
  assert.match(studio, /parallel-calibration\/plans\/\$\{encodeURIComponent\(planId\)\}\/bundle/);
  assert.match(studio, /一键导出 HTML 报告/);
  assert.match(desktop, /onSaveReport=\{\(report\) => invoke\('saveGeneratedHtmlReport', \{html: report\.html, filename: report\.filename\}\)\}/);
  assert.doesNotMatch(desktop.slice(desktop.indexOf('onSaveReport='), desktop.indexOf('initialDatasetId=', desktop.indexOf('onSaveReport='))), /fullResultExportEnabled/,
    'HTML reporting must stay available when raw JSON export is disabled');
});
