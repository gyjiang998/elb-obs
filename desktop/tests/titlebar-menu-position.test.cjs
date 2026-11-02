'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { resolveTitlebarMenuPosition } = require('../runtime/titlebar-menu-position.cjs');

test('native menu anchor stays content-relative when the window is away from the screen origin', () => {
  const position = resolveTitlebarMenuPosition({
    anchorX: 126,
    anchorY: 35,
    zoomFactor: 1,
    contentBounds: { x: 840, y: 420, width: 1500, height: 990 },
  });
  assert.deepEqual(position, { x: 126, y: 35 });
});

test('native menu anchor converts renderer CSS pixels using the workbench zoom', () => {
  assert.deepEqual(resolveTitlebarMenuPosition({
    anchorX: 126.4,
    anchorY: 35,
    zoomFactor: 1.4,
    contentBounds: { x: 200, y: 100, width: 1500, height: 990 },
  }), { x: 177, y: 49 });
});

test('native menu anchor remains inside content near window and overlay edges', () => {
  assert.deepEqual(resolveTitlebarMenuPosition({
    anchorX: 1200,
    anchorY: 900,
    zoomFactor: 2,
    contentBounds: { x: -1920, y: 0, width: 1020, height: 720 },
  }), { x: 1019, y: 719 });
});

test('native menu anchor rejects invalid renderer geometry', () => {
  assert.throws(() => resolveTitlebarMenuPosition({
    anchorX: -1,
    anchorY: 35,
    zoomFactor: 1,
    contentBounds: { width: 1020, height: 720 },
  }), /inside the renderer/);
  assert.throws(() => resolveTitlebarMenuPosition({
    anchorX: 0,
    anchorY: 35,
    zoomFactor: 0,
    contentBounds: { width: 1020, height: 720 },
  }), /zoomFactor/);
});
