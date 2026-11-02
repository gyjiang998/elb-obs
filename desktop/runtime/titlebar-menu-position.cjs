'use strict';

function finiteNumber(value, label) {
  const number = Number(value);
  if (!Number.isFinite(number)) throw new TypeError(`${label} must be a finite number`);
  return number;
}

function clamp(value, minimum, maximum) {
  return Math.min(maximum, Math.max(minimum, value));
}

/**
 * Electron's Menu.popup x/y are relative to the BrowserWindow content origin.
 * Renderer DOMRects use CSS pixels, so only the webContents zoom factor is
 * applied here. BrowserWindow screen coordinates must never be added again:
 * Electron converts the content-relative point to screen coordinates itself.
 */
function resolveTitlebarMenuPosition({ anchorX, anchorY, zoomFactor, contentBounds } = {}) {
  const cssX = finiteNumber(anchorX, 'anchorX');
  const cssY = finiteNumber(anchorY, 'anchorY');
  const factor = finiteNumber(zoomFactor, 'zoomFactor');
  const width = finiteNumber(contentBounds?.width, 'contentBounds.width');
  const height = finiteNumber(contentBounds?.height, 'contentBounds.height');
  if (cssX < 0 || cssY < 0) throw new RangeError('menu anchor must be inside the renderer');
  if (factor <= 0) throw new RangeError('zoomFactor must be greater than zero');
  if (width <= 0 || height <= 0) throw new RangeError('content bounds must be non-empty');

  return {
    x: clamp(Math.round(cssX * factor), 0, Math.max(0, Math.round(width) - 1)),
    y: clamp(Math.round(cssY * factor), 0, Math.max(0, Math.round(height) - 1)),
  };
}

module.exports = { resolveTitlebarMenuPosition };
