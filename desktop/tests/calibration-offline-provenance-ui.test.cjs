'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.resolve(__dirname, '../..');
const source = relative => fs.readFileSync(path.join(root, relative), 'utf8');

test('bundled calibration provenance is distinct from local HDC extraction in both pages', () => {
  const hardware = source('app/components/HardwareModelsStudio.tsx');
  const calibration = source('app/components/OptimizationCalibrationStudio.tsx');

  for (const page of [hardware, calibration]) {
    assert.match(page, /display_source === 'bundled_offline_calibration'/);
    assert.match(page, /平台离线实测/);
    assert.match(page, /source === 'local_hdc' \? '本机 HDC 提取'/);
  }
  assert.match(calibration, /version\.display_source === 'bundled_offline_calibration'/);
  assert.match(calibration, /version\.display_source_label \|\| '随包校准'/);
});
