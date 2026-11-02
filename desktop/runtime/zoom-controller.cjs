'use strict';

const fs = require('node:fs/promises');
const path = require('node:path');

const DEFAULT_ZOOM_FACTOR = 1;
const MIN_ZOOM_FACTOR = 0.7;
const MAX_ZOOM_FACTOR = 2;
const ZOOM_STEP = 0.1;

function normalizeZoomFactor(value, fallback = DEFAULT_ZOOM_FACTOR) {
  if (value === null || value === '' || typeof value === 'boolean') return fallback;
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.round(Math.min(MAX_ZOOM_FACTOR, Math.max(MIN_ZOOM_FACTOR, number)) * 10) / 10;
}

function zoomCommandFromInput(input = {}) {
  if (input.type !== 'keyDown' || input.isComposing || input.alt || (!input.control && !input.meta)) return null;
  if (input.code === 'Equal' || input.code === 'NumpadAdd' || input.key === '+') return 'in';
  if (input.code === 'Minus' || input.code === 'NumpadSubtract' || input.key === '-') return 'out';
  if (input.code === 'Digit0' || input.code === 'Numpad0' || input.key === '0') return 'reset';
  return null;
}

function nextZoomFactor(current, command) {
  if (command === 'reset') return DEFAULT_ZOOM_FACTOR;
  const direction = command === 'in' ? 1 : command === 'out' ? -1 : 0;
  return normalizeZoomFactor(normalizeZoomFactor(current) + direction * ZOOM_STEP);
}

class ZoomPreferenceStore {
  constructor({ userDataDir, fileSystem = fs } = {}) {
    if (!userDataDir) throw new Error('ZoomPreferenceStore requires userDataDir');
    this.fileSystem = fileSystem;
    this.userDataDir = userDataDir;
    this.filePath = path.join(userDataDir, 'ui-preferences.json');
  }

  async load() {
    try {
      const value = JSON.parse(await this.fileSystem.readFile(this.filePath, 'utf8'));
      return normalizeZoomFactor(value?.zoomFactor);
    } catch (error) {
      if (error?.code !== 'ENOENT' && !(error instanceof SyntaxError)) throw error;
      return DEFAULT_ZOOM_FACTOR;
    }
  }

  async save(zoomFactor) {
    await this.fileSystem.mkdir(this.userDataDir, { recursive: true });
    const temporary = `${this.filePath}.tmp-${process.pid}-${Date.now()}`;
    await this.fileSystem.writeFile(temporary, `${JSON.stringify({ zoomFactor: normalizeZoomFactor(zoomFactor) }, null, 2)}\n`, { mode: 0o600 });
    try { await this.fileSystem.rename(temporary, this.filePath); }
    catch (error) {
      await this.fileSystem.rm(temporary, { force: true }).catch(() => {});
      throw error;
    }
  }
}

class WindowZoomController {
  constructor({ webContents, store, onError = console.error, onChange = () => {} } = {}) {
    if (!webContents || !store) throw new Error('WindowZoomController requires webContents and store');
    this.webContents = webContents;
    this.store = store;
    this.onError = onError;
    this.onChange = onChange;
    this.factor = DEFAULT_ZOOM_FACTOR;
    this.pendingSave = Promise.resolve();
  }

  applyFactor(value) {
    this.webContents.setZoomFactor(value);
    try { this.onChange(value); }
    catch (error) { this.onError(error); }
  }

  async initialize() {
    try { this.factor = await this.store.load(); }
    catch (error) { this.onError(error); this.factor = DEFAULT_ZOOM_FACTOR; }
    this.applyFactor(this.factor);
    return this.factor;
  }

  change(command) {
    if (!['in', 'out', 'reset'].includes(command)) return this.factor;
    const next = nextZoomFactor(this.factor, command);
    if (next === this.factor) return this.factor;
    this.factor = next;
    this.applyFactor(next);
    this.pendingSave = this.pendingSave
      .catch(() => {})
      .then(() => this.store.save(next))
      .catch(error => this.onError(error));
    return next;
  }

  handleInput(event, input) {
    const command = zoomCommandFromInput(input);
    if (!command) return false;
    event.preventDefault();
    this.change(command);
    return true;
  }

  flush() { return this.pendingSave; }
}

module.exports = {
  DEFAULT_ZOOM_FACTOR,
  MIN_ZOOM_FACTOR,
  MAX_ZOOM_FACTOR,
  normalizeZoomFactor,
  zoomCommandFromInput,
  nextZoomFactor,
  ZoomPreferenceStore,
  WindowZoomController,
};
