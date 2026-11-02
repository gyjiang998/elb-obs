const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const assert = require('node:assert/strict');

const repositoryRoot = path.resolve(__dirname, '../..');
const source = relative => fs.readFileSync(path.join(repositoryRoot, relative), 'utf8');

test('calibration page renders a dynamic core-group by workload-class frequency-curve gallery at the bottom', () => {
  const page = source('app/components/OptimizationCalibrationStudio.tsx');
  const gallery = source('app/components/CalibrationFrequencyCurveGallery.tsx');
  const css = source('app/calibration-storage.css');

  assert.match(page, /<CalibrationFrequencyCurveGallery selectedFit=\{fit\} fits=\{fits\} versions=\{versions\} \/>/);
  assert.ok(page.lastIndexOf('<CalibrationFrequencyCurveGallery') > page.indexOf('id="calibration-workbench"'),
    'the curve matrix should stay visible below the calibration workbench tabs');
  assert.match(gallery, /Object\.keys\(profiles\)/,
    'core groups must come from the selected model instead of a three-core assumption');
  assert.match(gallery, /Object\.keys\(classes\)\.forEach/,
    'workload classes must be discovered from the fitted model');
  assert.match(gallery, /\[\.\.\.coreIds\]\.flatMap/);
  assert.doesNotMatch(gallery, /\['big',\s*'middle',\s*'small'\]/,
    'the gallery must not hard-code exactly three core groups');
  assert.match(gallery, /sample\.benchmark !== 'pair_work'/);
  assert.match(gallery, /rejectedSampleIndices\.has\(sampleIndex\)/,
    'rejected samples without a usable sample_id must still stay out of observed curves');
  assert.match(gallery, /cpuMs \/ workUnits/,
    'measured points should show unit-work CPU latency rather than incomparable total runtimes');
  assert.match(gallery, /<polyline className="cs-curve-fit"/);
  assert.match(gallery, /\{fittedPoints\.length > 1 && <span><i className="fitted" \/>拟合曲线<\/span>\}/,
    'cards with measured points only must not advertise a fitted curve');
  assert.match(gallery, /<circle className="cs-curve-observed"/);
  assert.match(gallery, /适用域 \{formatFrequency\(xMinimum\)\}–\{formatFrequency\(xMaximum\)\}/);
  assert.match(gallery, /源码强度未定义（无字节分母）/);
  assert.match(gallery, /0\.125 源码整数操作\/最小源码 Byte/);
  assert.match(gallery, /0\.164 源码整数操作\/最小源码 Byte/);
  assert.match(gallery, /该 compute 代理不分配或访问它/,
    'the uniform 64 MiB plan field must not be presented as a compute-proxy working set');
  assert.match(gallery, /operational_intensity_integer_ops_per_byte/,
    'frequency proxy cards must read the frozen source-intensity contract');
  assert.match(gallery, /instruction_intensity_instructions_per_dram_byte/,
    'the UI must keep aligned PMU instruction intensity separate from source counts');
  assert.match(gallery, /需要同一计时窗的 retired instructions 与已核验 DRAM bytes/,
    'missing PMU intensity must be explicit instead of inferred from a workload label');
  assert.match(gallery, /代理源码吞吐/);
  assert.match(gallery, /不等于 DRAM 带宽/);
  assert.match(gallery, /该核组与任务类型尚未形成有效实测和拟合结果/);
  assert.match(gallery, /version\.model/);
  assert.match(gallery, /sourceFit\?\.measurements/,
    'published models should reuse their retained fit samples when those records are available');
  assert.match(gallery, /sourceFit\?\.frequency_curve_observations/,
    'published models should receive bounded OPP medians from compact Desktop fit summaries');
  assert.match(gallery, /const fitRecords = selectedFit/,
    'a fully loaded selected fit must override its compact list record');
  assert.match(gallery, /const qualityPassed = value\.quality\?\.passed === true/,
    'fit sources must retain their quality-gate result');
  assert.match(gallery, /const publishable = Boolean\(value\.quality\?\.publishable \?\? value\.quality\?\.passed\)/,
    'publishability must remain separate from the statistical quality result');
  assert.match(gallery, /status: qualityPassed \? 'fit' as const : publishable \? 'fit-risk' as const : 'diagnostic' as const/,
    'risk-publishable and structurally blocked fits must have distinct states');
  assert.match(gallery, /sources\.find\(source => source\.status !== 'diagnostic'\)/,
    'the default source must prefer a published or quality-passed model over failed selected fits');
  assert.match(gallery, /可发布 · 有质量风险/);
  assert.match(gallery, /仅诊断 · 不可发布/);
  assert.match(gallery, /曲线可随风险模型参与寻优，但结论必须保留证据等级和适用域/);
  assert.match(gallery, /来源、结构、完整性门禁未通过或有效数据不足，曲线不能参与寻优/,
    'blocked diagnostics must never be described as publishable or usable for search');
  assert.match(gallery, /evidenceState === 'blocked' \? '仅诊断'/,
    'curve cards must visibly distinguish blocked evidence from publishable risk');
  assert.match(gallery, /pmuMetricSampleCounts/);
  assert.match(gallery, /四项齐全/,
    'partial PMU coverage must expose the true all-metric intersection');
  assert.match(gallery, /pmu_cache_misses_per_work_unit/,
    'projected PMU cache misses must be normalized before curves compare different work-unit counts');
  assert.match(gallery, /Cache miss \/ 千工作量/);
  assert.match(gallery, /cacheMisses \/ workUnits/,
    'the full-fit fallback path must use the same work-unit normalization as the backend projection');
  assert.match(gallery, /T\(fmax\)=\$\{formulaAnchorLabel\}/,
    'the fitted formula must include the measured value at fmax and its unit');
  assert.match(gallery, /function ClassicRooflineEvidence/);
  assert.match(gallery, /算法 FLOP \/ 最小源码 Byte（log₂）/);
  assert.match(gallery, /cs-classic-intensity-point/,
    'a fitted classic Roofline must render every measured intensity-ladder point');
  assert.match(gallery, /平台仍保留屋顶与强度点并允许带风险发布/,
    'a statistically weak Roofline must remain explicitly risk-labelled');
  assert.match(gallery, /当前校准版本没有数值计算强度扫描/);
  assert.match(css, /\.cs-frequency-curve-grid \{[^}]*repeat\(auto-fit,minmax\(270px,1fr\)\)/);
  assert.match(css, /\.cs-classic-roofline-grid \{[^}]*repeat\(auto-fit,minmax\(320px,1fr\)\)/);
});

test('automatic calibration exposes the expensive intensity sweep as an explicit optional add-on', () => {
  const page = source('app/components/OptimizationCalibrationStudio.tsx');
  assert.match(page, /同时采集计算强度与经典 Roofline（可选）/);
  assert.match(page, /classicRooflineEstimatedRepeats/,
    'the user should see the additional measurement cost before enabling the sweep');
  assert.match(page, /3 \* Math\.max\(repeats, 3\) \+ 10/,
    'the estimate must include at least three ceiling repeats and five two-repeat intensity levels');
  assert.doesNotMatch(page, /const primaryCalibrationGroupIds = \[[^\]]*classic_roofline/,
    'the costly auxiliary sweep must not silently join the latency-calibration default');
  assert.match(page, /2 \/ 8 \/ 32 \/ 128 \/ 512 FLOP\/元素五档源码结构/);
});

test('risk-calibrated models remain selectable without being presented as quality-passed', () => {
  const calibration = source('app/components/OptimizationCalibrationStudio.tsx');
  const modeling = source('app/components/ParallelModelingStudio.tsx');
  assert.match(calibration, /fit\?\.quality\?\.publishable \?\? fit\?\.quality\?\.passed/,
    'historical passed fits without the new publishable field must stay compatible');
  assert.match(calibration, /来源与结构完整性通过后自动发布；统计质量门失败时标记总体风险/);
  assert.doesNotMatch(calibration, /if \(!result\.quality\.passed\)/,
    'automatic calibration must not stop solely because a statistical gate failed');
  assert.match(modeling, /item\.status === 'risk_calibrated' \? ' · 有风险'/);
  assert.match(modeling, /风险校准模型 · 总体风险/);
  assert.match(modeling, /模型仍可用于搜索，结果需保留适用风险/);
});
