const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const assert = require('node:assert/strict');
const ts = require('typescript');

const repositoryRoot = path.resolve(__dirname, '../..');
const source = relative => fs.readFileSync(path.join(repositoryRoot, relative), 'utf8');

function loadTypeScriptModule(relative) {
  const output = ts.transpileModule(source(relative), {
    compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022},
  }).outputText;
  const loaded = {exports: {}};
  new Function('module', 'exports', 'require', output)(loaded, loaded.exports, require);
  return loaded.exports;
}

test('parallel modeling exposes the exact lexicographic optimization model as a full-width review card before search', () => {
  const page = source('app/components/ParallelModelingStudio.tsx');
  const css = source('app/parallel-modeling.css');

  assert.match(page, /function OptimizationModelFormula/);
  assert.match(page, /<OptimizationModelFormula objective=\{objective\} budget=\{budget\} robustness=\{robustness\} \/>/);
  assert.match(page, /<\/div><\/div><OptimizationModelFormula objective=\{objective\}/,
    'the model card should follow the closed two-column configuration grid');
  assert.ok(page.indexOf('<OptimizationModelFormula') < page.indexOf('搜索并行配置'),
    'the active model should be visible before execution');

  for (const section of ['目标函数', '约束集合', '字典序优先级', '两级求解', '模型符号', '解释边界']) {
    assert.match(page, new RegExp(section), `the review card should expose ${section}`);
  }
  assert.match(page, /组合内求解/);
  assert.match(page, /可行性筛选/);
  assert.match(page, /全局择优/);

  assert.match(page, /T̂<sub>UI<\/sub>\(x\), N<sub>slot<\/sub>\(x\), C<sub>app<\/sub>\(x\), i\(x\)/,
    'minimum latency must retain the backend lexicographic tie breakers');
  assert.match(page, /max<sub>g∈A\(x\)<\/sub> r<sub>g<\/sub>, Σ<sub>g∈A\(x\)<\/sub> r<sub>g<\/sub>, N<sub>slot<\/sub>\(x\), T̂<sub>UI<\/sub>\(x\), i\(x\)/,
    'minimum frequency must retain the normalized max, sum, slot, latency and stable-index order');
  assert.match(page, /T̂<sub>UI<\/sub>\(x\) ≤ \{budgetLabel\}/);
  assert.match(page, /max<sub>s∈S<sub>decl<\/sub><\/sub> T̂<sub>UI<\/sub>\(x; s\) ≤ \{budgetLabel\}/,
    'robust budget search must expose its declared-scenario budget constraint');
  assert.match(page, /只对每个核心组合的名义 winner 做后验敏感性检查，不改变为最坏情景最小化/,
    'minimum-latency robustness must not be presented as minimax');
  assert.match(page, /这里优化的是频点，不代表能耗最小/,
    'frequency and energy objectives must remain distinct');
  assert.match(page, /先在每种物理核心组合内按上述字典序选择 winner，再从各组合的可行 winner 中按同一目标选出全局 best/);
  assert.match(page, /当前候选任务、已观测依赖、同核组 Worker、物理核容量和所选硬件 OPP/);
  assert.match(page, /const controls = restoreParallelSearchControls\(value\)/);
  assert.match(page, /if \(controls\.objective\) setObjective\(controls\.objective\)/);
  assert.match(page, /if \(controls\.frequencyPolicy\) setFrequencyPolicy\(controls\.frequencyPolicy\)/);
  assert.match(page, /if \(controls\.robustness !== undefined\) setRobustness\(controls\.robustness\)/);
  assert.match(css, /\.pm-optimization-model \{/);
  assert.match(css, /\.pm-optimization-equation \{/);
  assert.match(css, /'Cambria Math', 'STIX Two Math'/);
  assert.match(css, /\.pm-optimization-core \{[^}]*grid-template-columns:/s);
  assert.match(css, /\.pm-optimization-flow \{/);
  assert.match(css, /@container \(max-width: 760px\)[^{]*\{[^}]*\.pm-optimization-core \{ grid-template-columns: 1fr;/s,
    'the formula card should collapse cleanly in narrow workbench panes');
});

test('historical search controls restore from frozen inputs without inferring missing fields', () => {
  const {restoreParallelSearchControls} = loadTypeScriptModule('app/lib/parallel-search-controls.ts');
  const restored = restoreParallelSearchControls({
    objective: 'minimum_latency',
    budget_ms: 99,
    candidate_ids: ['stale'],
    search_configuration: {
      objective: 'minimum_frequency_with_budget',
      budget_ms: 42.5,
      candidate_ids: ['build_02', 'build_01'],
      robustness: true,
      space: {
        available_core_group_ids: ['middle', 'big'],
        main_core_group_id: 'big',
        worker_counts: [3, 1, 3],
        main_participation: [false, true],
        frequency_policy: 'all_opps',
        frequencies_khz: {big: [2500000, 1325000, 2500000], middle: [1076000]},
      },
    },
    search_provenance: {
      robustness_requested: false,
      space: {frequency_policy: 'max_opps'},
    },
  });

  assert.deepEqual(restored, {
    objective: 'minimum_frequency_with_budget',
    budget: '42.5',
    availableCores: ['middle', 'big'],
    mainCore: 'big',
    workerCounts: [1, 3],
    mainParticipation: 'both',
    frequencyPolicy: 'all_opps',
    frequenciesKHz: {big: [1325000, 2500000], middle: [1076000]},
    robustness: true,
    candidateIds: ['build_02', 'build_01'],
  });

  assert.deepEqual(restoreParallelSearchControls({
    objective: 'minimum_latency',
    budget_ms: 80,
    candidate_ids: ['build_03'],
    search_provenance: {
      robustness_meaning: 'not_requested',
      space: {
        available_core_group_ids: ['big'],
        main_core_group_id: 'big',
        worker_counts: [0, 2],
        main_participation: false,
        frequency_policy: 'max_opps',
      },
    },
  }), {
    objective: 'minimum_latency',
    budget: '80',
    availableCores: ['big'],
    mainCore: 'big',
    workerCounts: [0, 2],
    mainParticipation: 'no',
    frequencyPolicy: 'max_opps',
    frequenciesKHz: null,
    robustness: false,
    candidateIds: ['build_03'],
  }, 'legacy records may use explicit provenance fields');

  assert.deepEqual(restoreParallelSearchControls({objective: 'unknown', search_provenance: {}}), {},
    'missing historical fields must not be reconstructed from the winner');
});
