const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const assert = require('node:assert/strict');

const root = path.resolve(__dirname, '../..');
const source = relative => fs.readFileSync(path.join(root, relative), 'utf8');

test('hardware model UI distinguishes physical cores from logical scheduling CPUs', () => {
  const studio = source('app/components/HardwareModelsStudio.tsx');
  assert.match(studio, /physical_core_topology/);
  assert.match(studio, /物理核 → 逻辑 CPU/);
  assert.match(studio, /个物理核/);
  assert.match(studio, /个逻辑 CPU/);
  assert.match(studio, /物理核用于计算独立并行资源/);
  assert.match(studio, /physical_core_representative_cpu_ids/);
  assert.match(studio, /physicalTopology: undefined/,
    'editing CPU membership must invalidate a stale extracted sibling map');
  assert.match(studio, /physicalCoreCountSource: 'manual'/,
    'editing a topology must not keep claiming that its physical-core count came from sysfs');

  const calibration = source('app/components/OptimizationCalibrationStudio.tsx');
  assert.match(calibration, /个物理核 · \{selectedHardware\.core_groups\.reduce/);
  assert.match(calibration, /个逻辑 CPU/);

  const css = source('app/parallel-modeling.css');
  assert.match(css, /\.hardware-count-summary strong \{ color: var\(--ink\)/);
  assert.match(css, /\.hardware-core-map code \{[^}]*color: var\(--ink\)/);
  assert.doesNotMatch(css, /\.hardware-(?:count-summary|core-map)[^{]*\{[^}]*var\(--text\)/);
});

