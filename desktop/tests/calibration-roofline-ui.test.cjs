const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const assert = require('node:assert/strict');

const repositoryRoot = path.resolve(__dirname, '../..');
const source = relative => fs.readFileSync(path.join(repositoryRoot, relative), 'utf8');

test('calibration page separates the true Roofline from the frequency-time surrogate', () => {
  const explorer = source('app/components/RooflineFrequencyExplorer.tsx');
  const explanation = source('app/components/ParallelCostModelExplanation.tsx');
  const css = source('app/cost-model-explanation.css');

  assert.match(explorer, /Arithmetic intensity \(FLOP\/Byte\)/);
  assert.match(explorer, /Attainable performance \(GFLOP\/s\)/);
  assert.match(explorer, /const \[workloadIntensityExponent, setWorkloadIntensityExponent\] = useState\(/,
    'the workload arithmetic intensity must be interactive state');
  assert.match(explorer, /const workloadIntensity = 2 \*\* workloadIntensityExponent/,
    'the logarithmic slider position must map to arithmetic intensity');
  assert.match(explorer, /aria-label="[^"]*负载计算强度[^"]*"/,
    'the explorer must expose an accessible workload-intensity control');
  assert.match(explorer, /type="range"[^>]*value=\{workloadIntensityExponent\}/,
    'the workload-intensity control must be a range slider bound to the workload point');
  assert.match(explorer, /setWorkloadIntensityExponent\(Number\(event\.target\.value\)\)/,
    'moving the workload-intensity slider must update the workload point');
  assert.match(explorer, /bandwidthCeiling = memoryBandwidth \* workloadIntensity/,
    'the bandwidth roof at the workload point must be BW times arithmetic intensity');
  assert.match(explorer, /Math\.min\(computeCeiling, bandwidthCeiling\)/,
    'workload performance must be min(Ppeak, BW times arithmetic intensity)');
  assert.match(explorer, /workloadY = y\(attainablePerformance\)/,
    'the workload marker must be placed at the calculated attainable performance');
  assert.match(explorer, /workloadX = x\(workloadIntensity\)/,
    'the workload marker must move horizontally with arithmetic intensity');
  assert.match(explorer, /ridgeIntensity = computeCeiling \/ memoryBandwidth/);
  assert.match(explorer, /frequencyKnee = bandwidthCeiling \/ peakAtMaximumFrequency \* 100/);
  assert.match(explorer, /type="range" min=\{20\} max=\{100\}/,
    'the frequency control must move the active compute roof');
  assert.match(explorer, /currentNearMinimumReference[^\n]+peakAtMaximumFrequency \* \.2/,
    'the lowest compute-roof reference must track 20% OPP');
  assert.match(explorer, /className="cm-roofline-reference min"[^>]+y1=\{y\(peakAtMaximumFrequency \* \.2\)\}[^>]+y2=\{y\(peakAtMaximumFrequency \* \.2\)\}/,
    'the lowest compute-roof reference line must be drawn at 20% OPP');
  assert.match(explorer, /20% OPP 计算上限/,
    'the lowest compute-roof reference must be labelled as 20% OPP');
  assert.match(explorer, /Maximum attainable performance/);
  assert.match(explorer, /<line className="cm-roofline-boundary" x1=\{ridgeX\} y1=\{currentCeilingY\} x2=\{ridgeX\} y2=\{chart\.bottom\} \/>/,
    'the current Roofline intersection must project a dynamic vertical boundary to the x axis');
  assert.match(explorer, /<polygon className="cm-roofline-region memory" points=\{memoryBoundRegion\} \/>/,
    'the bandwidth roof and dynamic boundary must enclose the memory-bound region');
  assert.match(explorer, /<polygon className="cm-roofline-region compute" points=\{computeBoundRegion\} \/>/,
    'the area to the right of the dynamic boundary must be the compute-bound region');
  assert.match(explorer, /className="cm-roofline-region-label memory"[^>]*>Memory-bound<\/text>/,
    'the region left of the Roofline intersection must be labelled Memory-bound');
  assert.match(explorer, /className="cm-roofline-region-label compute"[^>]*>Compute-bound<\/text>/,
    'the region right of the Roofline intersection must be labelled Compute-bound');
  assert.doesNotMatch(explorer, /Roofline 拐点/,
    'the ridge marker must not carry a label that overlaps the active compute roof');
  assert.doesNotMatch(explorer, /固定负载 I = \{workloadIntensity\} FLOP\/Byte/,
    'the chart must not repeat the workload intensity in the marker label');
  assert.match(explorer, /cm-roofline-workload-label[^>]*>负载<\/text>/,
    'the workload point must use the concise label “负载”');
  assert.match(explorer, /currentNearMaximumReference[^\n]+Math\.abs/,
    'reference labels must yield when the active compute roof is close enough to overlap');
  assert.match(explorer, /frequencyKnee < 20[\s\S]*当前频段始终带宽受限[\s\S]*frequencyKnee > 100[\s\S]*当前频段始终计算受限/,
    'out-of-range workload boundaries must be explained as an always-limited state');
  assert.match(explorer, /computeBound \? '计算受限' : '带宽受限'/);
  assert.match(explorer, /本轮真机校准尚未生成 Instruction Roofline/);
  assert.match(explanation, /<RooflineFrequencyExplorer \/>/);
  assert.match(explanation, /G 是相对最高 OPP 的 On-CPU 时延倍率/,
    'the frequency response must not be mislabeled as attainable performance');
  assert.match(explanation, /低频段用较大的 e<sub>lo<\/sub>，高频段用较小的 e<sub>hi<\/sub>/);
  assert.match(explanation, /频率敏感区 · 升频收益高/);
  assert.match(explanation, /带宽受限区 · 收益趋缓/);
  assert.match(explanation, /相对 On-CPU 时延 G（越低越好）/);
  assert.match(css, /data-theme="dark"[^}]+cm-roofline-axis-title[^}]+fill: #d0d9de/);
  assert.match(css, /data-theme="dark"[^}]+cm-roofline-bandwidth-label[^}]+fill:/);
  assert.match(css, /\.cm-roofline-region\.memory[^}]+fill:/,
    'the memory-bound region must have a visible fill');
  assert.match(css, /\.cm-roofline-region\.compute[^}]+fill:/,
    'the compute-bound region must have a visible fill');
  assert.match(css, /\.cm-roofline-boundary[^}]+stroke:/,
    'the current intersection boundary must have a visible stroke');
  assert.match(css, /\.cm-roofline-control > input[^}]+width: 100%/,
    'both interactive sliders must retain the panel range-control styling');
});
