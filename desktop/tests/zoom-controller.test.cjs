'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {
  DEFAULT_ZOOM_FACTOR,
  normalizeZoomFactor,
  zoomCommandFromInput,
  nextZoomFactor,
  ZoomPreferenceStore,
  WindowZoomController,
} = require('../runtime/zoom-controller.cjs');

test('zoom shortcuts accept Ctrl or Command with keyboard and numpad keys', () => {
  assert.equal(zoomCommandFromInput({ type: 'keyDown', control: true, code: 'Equal', key: '=' }), 'in');
  assert.equal(zoomCommandFromInput({ type: 'keyDown', control: true, shift: true, code: 'Equal', key: '+' }), 'in');
  assert.equal(zoomCommandFromInput({ type: 'keyDown', meta: true, code: 'NumpadAdd', key: '+' }), 'in');
  assert.equal(zoomCommandFromInput({ type: 'keyDown', control: true, code: 'Minus', key: '-' }), 'out');
  assert.equal(zoomCommandFromInput({ type: 'keyDown', control: true, code: 'NumpadSubtract', key: '-' }), 'out');
  assert.equal(zoomCommandFromInput({ type: 'keyDown', control: true, code: 'Digit0', key: '0' }), 'reset');
  assert.equal(zoomCommandFromInput({ type: 'keyDown', code: 'Equal', key: '+' }), null);
  assert.equal(zoomCommandFromInput({ type: 'keyDown', control: true, alt: true, code: 'Equal', key: '+' }), null);
  assert.equal(zoomCommandFromInput({ type: 'keyUp', control: true, code: 'Equal', key: '+' }), null);
});

test('zoom factors move in readable steps, reset, and remain within safe bounds', () => {
  assert.equal(nextZoomFactor(1, 'in'), 1.1);
  assert.equal(nextZoomFactor(1, 'out'), 0.9);
  assert.equal(nextZoomFactor(1.6, 'reset'), DEFAULT_ZOOM_FACTOR);
  assert.equal(nextZoomFactor(2, 'in'), 2);
  assert.equal(nextZoomFactor(0.7, 'out'), 0.7);
  assert.equal(normalizeZoomFactor(null), DEFAULT_ZOOM_FACTOR);
  assert.equal(normalizeZoomFactor('not-a-number'), DEFAULT_ZOOM_FACTOR);
});

test('zoom preference store persists the factor and recovers from absent or malformed state', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hetero-zoom-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const store = new ZoomPreferenceStore({ userDataDir: directory });
  assert.equal(await store.load(), DEFAULT_ZOOM_FACTOR);
  await store.save(1.4);
  assert.equal(await store.load(), 1.4);
  if (process.platform !== 'win32') assert.equal(fs.statSync(store.filePath).mode & 0o777, 0o600);
  fs.writeFileSync(store.filePath, '{broken');
  assert.equal(await store.load(), DEFAULT_ZOOM_FACTOR);
});

test('window zoom controller consumes shortcuts, applies zoom, and serializes persistence', async () => {
  const applied = [];
  const changed = [];
  const saved = [];
  const webContents = { setZoomFactor: value => applied.push(value) };
  const store = {
    load: async () => 1.2,
    save: async value => { saved.push(value); },
  };
  const controller = new WindowZoomController({ webContents, store, onChange: value => changed.push(value) });
  await controller.initialize();
  let prevented = false;
  assert.equal(controller.handleInput({ preventDefault: () => { prevented = true; } }, { type: 'keyDown', control: true, code: 'Equal' }), true);
  assert.equal(prevented, true);
  controller.change('out');
  await controller.flush();
  assert.deepEqual(applied, [1.2, 1.3, 1.2]);
  assert.deepEqual(changed, applied, 'renderer zoom and Window Controls Overlay geometry must update together');
  assert.deepEqual(saved, [1.3, 1.2]);
});
