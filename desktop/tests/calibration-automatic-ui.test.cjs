const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const assert = require('node:assert/strict');

const repositoryRoot = path.resolve(__dirname, '../..');
const source = relative => fs.readFileSync(path.join(repositoryRoot, relative), 'utf8');

function functionBody(contents, name, nextName) {
  const match = contents.match(new RegExp(`async function ${name}\\(\\) \\{([\\s\\S]*?)\\n  \\}\\n  async function ${nextName}\\(`));
  assert.ok(match, `expected to find ${name} implementation`);
  return match[1];
}

test('calibration page runs the selected registered hardware model through the platform-owned measurement loop', () => {
  const calibration = source('app/components/OptimizationCalibrationStudio.tsx');
  const automaticStart = functionBody(calibration, 'startAutomaticCalibration', 'completeAutomaticCalibration');

  for (const contract of [
    'onListCalibrationDevices',
    'onStartCalibrationRun',
    'onGetCalibrationRun',
    'onCancelCalibrationRun',
    'onGetCalibrationMeasurements',
  ]) assert.match(calibration, new RegExp(contract));

  assert.match(calibration, /const selectedHardware = catalog\?\.hardware_profiles\.find\(item => item\.id === hardwareId\) \?\? null/);
  assert.match(automaticStart, /const hardwareProfile = selectedHardware/);
  assert.match(automaticStart, /const nextPlan = await persistPlan\(hardwareProfile, effectiveGroups\)/);
  assert.match(automaticStart, /await onStartCalibrationRun\(\{ plan: nextPlan, hardwareProfile, deviceSerial \}\)/);
  assert.doesNotMatch(automaticStart, /onExtractCalibrationHardware/,
    'starting calibration must not extract a new hardware profile behind the user selection');
  assert.doesNotMatch(automaticStart, /\/v1\/parallel-modeling\/hardware-profiles/,
    'starting calibration must not register a duplicate hardware profile');
  assert.match(calibration, /await onGetCalibrationMeasurements\(\{ runId: completedRun\.runId \}\)/);
  assert.match(calibration, /new File\(\[output\.measurementText\]/);
  assert.match(calibration, /await publishFit\(result, false\)/);
  assert.match(calibration, /'cancelling'/);
  assert.match(calibration, /run\.frequencyRecovery\?\.cleanupPending/,
    'a run with pending frequency restoration must not enter fitting or publishing');
  assert.match(calibration, /const runRecoveryPending = run\?\.frequencyRecovery\?\.cleanupPending === true[\s\S]*!runExecutionActive \|\| \/clean\|recover\|restore\//,
    'an armed recovery journal during measurement is not itself a cleanup phase');
  assert.match(calibration, /const activeRunId = runActive \? run\?\.runId/,
    'polling must continue through the cleanup/recovery phase instead of freezing at the terminal measurement count');
  assert.match(calibration, /正在恢复设备频率边界/);
  assert.match(calibration, /测量完成 · 正在恢复频率边界/,
    '153\/153 is the end of measurement, while frequency restoration remains a visible independent stage');
  assert.match(calibration, /个未通过实际频率、落核或同胞线程静默核验/,
    'a partial scan must expose rejected sample counts instead of looking like a cleanup failure');
  assert.match(calibration, /频率或同胞线程质量补测/,
    'bounded frequency/sibling retries must be visible separately from completed plan repeats');
  assert.match(calibration, /首次恢复设备频率边界未成功；平台正在通过当前 HDC 连接自动重试/);
  assert.match(calibration, /设备原始频率边界已经恢复；临时微基准或本地恢复记录仍在后台清理，不影响拟合与发布/,
    'non-critical artifact cleanup must be visible without blocking the model pipeline');
  assert.doesNotMatch(calibration, /实测已停止，但设备原始频率边界尚未恢复/,
    'normal in-progress cleanup must not be reported as a failed operation');
});

test('calibration primary workflow lists every registered hardware model and requires an explicit selection', () => {
  const calibration = source('app/components/OptimizationCalibrationStudio.tsx');
  const automaticAvailability = calibration.match(/const automaticCalibrationAvailable = ([^\n]+)/)?.[1] ?? '';
  const workbench = calibration.match(/<section id="calibration-workbench"[\s\S]*?<details className="pm-details cs-advanced-plan">/)?.[0] ?? '';

  assert.match(workbench, /<span>硬件模型<\/span>/);
  assert.match(workbench, /<select value=\{hardwareId\}[^>]*onChange=\{event => selectHardwareModel\(event\.target\.value\)\}/);
  assert.match(workbench, /catalog\.hardware_profiles\.map\(hardware => <option key=\{hardware\.id\} value=\{hardware\.id\}>\{hardware\.name\} · \{hardwareSourceLabel\(hardware\)\}<\/option>\)/,
    'the primary selector must be sourced from the complete active platform hardware-model catalog');
  assert.match(calibration, /profile\.builtin \|\| source === 'builtin_archive'/,
    'the bundled archive profile must be labeled as platform-owned even though the calibration catalog omits builtin=true');
  assert.match(calibration, /const hardwareSelectionExplicit = useRef\(false\)/);
  assert.match(calibration, /hardwareSelectionExplicit\.current && c\.value\.hardware_profiles\.some\(item => item\.id === current\)/,
    'an explicit selection must survive catalog refresh while it remains active');
  assert.match(calibration, /hardwareSelectionExplicit\.current = false[\s\S]*c\.value\.hardware_profiles\.length === 1 \? c\.value\.hardware_profiles\[0\]\.id : ''/,
    'an implicit single-model default must return to no selection once the catalog becomes ambiguous');
  assert.match(workbench, /管理硬件模型/,
    'users need a direct route to extract or manually add a model when the catalog is incomplete');
  assert.match(calibration, /disabled=\{[^}]*!selectedHardware[^}]*\}/,
    'one-click calibration must remain unavailable until a registered model is selected');
  assert.doesNotMatch(automaticAvailability, /onExtractCalibrationHardware/,
    'automatic calibration availability must not depend on implicit profile extraction');
  assert.match(calibration, /一键生成微基准并校准/);
});

test('calibration page explains parameters, measurement evidence and validation before execution', () => {
  const calibration = source('app/components/OptimizationCalibrationStudio.tsx');
  const css = source('app/calibration-storage.css');

  for (const text of [
    '任务类型频点性能曲线',
    '负载组合干扰矩阵',
    'Worker 任务派发开销',
    '兼容回退',
    '核型 CPU 倍率',
    '线性频率弹性',
    '通用争用 alpha',
    '辅助硬件表征',
    '计算屋顶',
    '大工作集带宽屋顶',
    '核验模型',
    '真机扫描',
    '留出验证',
    '一键生成微基准并校准',
  ]) assert.match(calibration, new RegExp(text.replace('/', '\\/')));

  assert.match(calibration, /Instruction Roofline 只有在 retired instructions 与 DRAM bytes 完成同窗 PMU 对齐后才可发布/);
  assert.match(calibration, /不宣称已拆出 L1 \/ L2 \/ LLC \/ DRAM 层级/);
  assert.match(calibration, /\(queue − direct\) \/ task_count/);
  assert.match(calibration, /冷启动不纳入本轮，后续单独标定/);
  assert.match(calibration, /const primaryCalibrationGroupIds = \['frequency_roofline', 'pairwise_interference', 'dispatch_overhead'\]/,
    'the default UI path must use the three primary calibration groups without changing their stable IDs');
  assert.match(calibration, /const fallbackCalibrationGroupIds = \['core_ratios', 'frequency_response', 'contention'\]/,
    'legacy core ratio, linear elasticity and alpha must be grouped as compatibility fallbacks');
  assert.match(calibration, /const auxiliaryCalibrationGroupIds = \['classic_roofline'\]/,
    'classic Roofline must remain an auxiliary hardware characterization');
  assert.match(calibration, /setGroups\(current => current\.length \? current : primaryCalibrationGroupIds\.filter/,
    'the primary one-click workflow must initialize from the UI primary groups');
  assert.doesNotMatch(calibration, /保留主线/,
    'the ambiguous residual-work label must not remain visible in the calibration page');
  assert.match(calibration, /lowOppKneeGroups/);
  assert.match(calibration, /frequencyRooflineCoreKinds/,
    'the UI must send a per-core frequency-curve capability subset');
  assert.match(calibration, /frequency_roofline_core_kinds: frequencyRooflineCoreKinds/);
  assert.match(calibration, /rooflineCoreGroups\.length \? rooflineCoreGroups\.map/,
    'mixed-OPP hardware must report which core groups continue curve calibration');
  assert.doesNotMatch(calibration,
    /lowOppKneeGroups\.length\) \{\s*effectiveGroups = effectiveGroups\.filter\(group => group !== 'frequency_roofline'\)/,
    'one low-OPP core group must not remove frequency curves from every core group');
  assert.match(calibration, /lowOppLinearGroups/);
  assert.doesNotMatch(calibration, /经典 Roofline 频率扫描需要每个核组至少 3 个 OPP/,
    'classic Roofline remains measurable on hardware exposing fewer than three OPPs');
  assert.match(calibration, /高级 \/ 兼容：导入已有微基准测量 JSON/);
  assert.match(css, /\.cs-calibration-targets/);
  assert.match(css, /\.cs-run-stage-list/);
});

test('desktop calibration page wires the local HDC runner and keeps hardware extraction in model management', () => {
  const desktop = source('desktop/renderer/shared-pages.jsx');
  const runtime = source('desktop/runtime/index.cjs');

  assert.match(desktop, /onListCalibrationDevices=\{\(\) => invoke\('discoverDevices', \{hdcPath: localHdcPath\(\)\}\)\}/);
  assert.match(desktop, /onStartCalibrationRun=\{\(request\) => invoke\('startCalibrationRun', \{\.\.\.request, hdcPath: localHdcPath\(\)\}\)\}/);
  assert.match(desktop, /onGetCalibrationRun=\{\(request\) => invoke\('calibrationRunStatus', request\)\}/);
  assert.match(desktop, /onCancelCalibrationRun=\{\(request\) => invoke\('cancelCalibrationRun', request\)\}/);
  assert.match(desktop, /onGetCalibrationMeasurements=\{\(request\) => invoke\('getCalibrationMeasurements', request\)\}/);
  assert.doesNotMatch(desktop, /onExtractCalibrationHardware=/,
    'calibration must not expose a second profile-registration path');
  assert.match(desktop, /onExtractLocalHardware=\{\(deviceSerial\) => invoke\('extractHardwareProfile', \{deviceSerial, hdcPath: localHdcPath\(\)\}\)\}/,
    'device extraction remains available on the dedicated hardware-model page');
  assert.match(desktop, /onNavigateHardwareModels=\{\(\) => setPage\('hardware-models'\)\}/);
  assert.match(runtime, /this\.calibration\.status\(\{\.\.\.value, reconcile: true\}\)/,
    'Desktop progress polling must trigger bounded recovery for an online device after a transient cleanup failure');
  assert.doesNotMatch(runtime, /async cancelCalibrationRun[\s\S]{0,180}await this\.calibration\.ready/,
    'cancel must reach the active child immediately instead of waiting for a background recovery retry');
});
