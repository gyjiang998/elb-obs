const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const assert = require('node:assert/strict');

const root = path.resolve(__dirname, '../..');
const source = relative => fs.readFileSync(path.join(root, relative), 'utf8');

test('workload features exposes measured CPU placement and explainable classification methods', () => {
  const page = source('app/components/WorkloadFeaturesStudio.tsx');

  assert.match(page, /运行 CPU \/ 核组/);
  assert.match(page, /function CpuPlacement/);
  assert.match(page, /stage\.scheduling\.cpu_residency/);
  assert.match(page, /CPU 未记录 · 驻留证据不可用/);
  assert.match(page, /部分观测/);
  assert.match(page, /证据不足/);
  assert.match(page, /不作为确定源执行时间锚点/);
  assert.match(page, /硬件身份未核验/);
  assert.match(page, /部分驻留频率未记录/);
  assert.match(page, /frequencyUnknown/);
  assert.match(page, /容差内外推，建模保留风险/);
  assert.match(page, /coverage_ratio/);
  assert.match(page, /cpu_residency_evidence/);
  assert.match(page, /逻辑 CPU 来自 Trace 调度驻留；核组来自已核验硬件画像/);
  assert.match(page, /硬件画像未核验 \/ 用户绑定/,
    'an unverified explicit binding must produce a visible warning, not only a hover title');
  assert.match(page, /画像未核验/);
  assert.match(page, /用户绑定画像/);
  assert.match(page, /hardwareVerified=\{profile\.quality\.hardware_identity_verified === true\}/,
    'table and detail CPU placement must receive the profile identity confidence');
  assert.match(page, /function sourceAnchorProfileEligible/);
  assert.match(page, /profile\.schema_version === 'workload_feature_profile\/v2'/);
  assert.match(page, /profile\.engine_version === '1\.6\.0'/);
  assert.match(page, /profile\.hardware_profile_sha256/);
  assert.match(page, /profileContractEligible=\{sourceAnchorProfileEligible\(profile\)\}/,
    'legacy profiles may show observed residency but must not display it as a source cost anchor');
  assert.match(page, /历史特征版本缺少 v2 硬件画像身份合同/);
  assert.match(page, /datasets\.filter\(item => Boolean\(item\.hardware_profile_id\)\)/,
    'only a capture that already produced a hardware-bound dataset may hide its re-import source');
  assert.match(page, /已归档但未绑定画像，可重新导入匹配/);
  assert.match(page, /重新提取会按采样设备身份尝试匹配当前画像/);
  assert.match(page, /<details className="pm-details"><summary>特征分型说明/,
    'classification rules should be available below the table but collapsed by default');
  assert.match(page, /IPC 下界 ≥ 1\.0/);
  assert.match(page, /已核验 LLC MPKI 下界 ≥ 20/);
  assert.match(page, /<details className="pm-details"><summary>校准微基准设计/,
    'benchmark design should be explainable without making the main table noisy');
  assert.match(page, /将目标线程绑定到对应逻辑 CPU/);
  assert.match(page, /instructions、cycles、IPC、Cache miss 与 Cache MPKI/);
  assert.match(page, /PMU 缺失保持 unavailable，不按任务标签推断/);
});

test('workload features binds an explicit hardware model before extracting a new immutable feature version', () => {
  const page = source('app/components/WorkloadFeaturesStudio.tsx');
  const web = source('app/page.tsx');
  const desktop = source('desktop/renderer/shared-pages.jsx');

  assert.match(page, /type ModelingCatalog/);
  assert.match(page, /setHardwareProfiles\(results\[4\]\.value\.hardware_profiles \?\? \[\]\)/);
  assert.match(page, /aria-label="关联硬件模型"/);
  assert.match(page, /当前没有可关联的硬件模型/);
  assert.match(page, /前往硬件模型/);
  assert.match(page, /async function bindDatasetForAnalysis/);
  assert.match(page, /parallel-modeling\/datasets\/\$\{encodeURIComponent\(source\.id\)\}\/hardware-binding/);
  assert.match(page, /hardware_profile_id: bindingHardwareId/);
  assert.match(page, /dataset_id: target\.id/,
    'feature extraction must use the immutable hardware-bound dataset revision returned by the server');
  assert.match(page, /创建硬件绑定的不可变负载版本/);
  assert.match(web, /onNavigateHardwareModels=\{\(\) => setView\('hardware_models'\)\}/);
  assert.match(desktop, /onNavigateHardwareModels=\{\(\) => setPage\('hardware-models'\)\}/);
});

