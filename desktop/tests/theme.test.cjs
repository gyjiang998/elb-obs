const test = require('node:test');
const assert = require('node:assert/strict');
const {
  LIGHT_TERMINAL_MINIMUM_CONTRAST_RATIO,
  THEMES,
  THEME_STORAGE_KEY,
  normalizeTheme,
  terminalMinimumContrastRatioFor,
  terminalThemeFor,
} = require('../renderer/theme.cjs');

test('theme identifiers and storage key remain stable', () => {
  assert.deepEqual(THEMES, ['dark', 'light', 'hybrid']);
  assert.equal(THEME_STORAGE_KEY, 'hetero.desktop.theme.v1');
  assert.equal(normalizeTheme('light'), 'light');
  assert.equal(normalizeTheme('hybrid'), 'hybrid');
  assert.equal(normalizeTheme('unknown'), 'dark');
  assert.equal(normalizeTheme(null), 'dark');
});

test('terminal palette follows the selected workbench region', () => {
  assert.equal(terminalThemeFor('dark', 'agent').background, '#181818');
  assert.equal(terminalThemeFor('dark', 'ssh').background, '#181818');
  assert.equal(terminalThemeFor('light', 'agent').background, '#ffffff');
  assert.equal(terminalThemeFor('light', 'ssh').background, '#ffffff');
  assert.equal(terminalThemeFor('hybrid', 'agent').background, '#181818');
  assert.equal(terminalThemeFor('hybrid', 'ssh').background, '#ffffff');
  assert.deepEqual(terminalThemeFor('hybrid', 'agent'), terminalThemeFor('dark', 'agent'));
  assert.notEqual(terminalThemeFor('light', 'ssh'), terminalThemeFor('light', 'ssh'));
});

test('xterm contrast correction covers every light terminal without changing dark Agent output', () => {
  assert.equal(LIGHT_TERMINAL_MINIMUM_CONTRAST_RATIO, 4.5);
  assert.equal(terminalMinimumContrastRatioFor('light', 'agent'), 4.5);
  assert.equal(terminalMinimumContrastRatioFor('light', 'ssh'), 4.5);
  assert.equal(terminalMinimumContrastRatioFor('hybrid', 'ssh'), 4.5);
  assert.equal(terminalMinimumContrastRatioFor('hybrid', 'agent'), 1);
  assert.equal(terminalMinimumContrastRatioFor('dark', 'agent'), 1);
  assert.equal(terminalMinimumContrastRatioFor('dark', 'ssh'), 1);
});

test('every light terminal ANSI foreground stays distinguishable on white', () => {
  const linear = value => {
    const channel = value / 255;
    return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
  };
  const luminance = value => {
    const channels = value.match(/[0-9a-f]{2}/gi).map(channel => Number.parseInt(channel, 16));
    return 0.2126 * linear(channels[0]) + 0.7152 * linear(channels[1]) + 0.0722 * linear(channels[2]);
  };
  for (const target of ['agent', 'ssh']) {
    const palette = terminalThemeFor('light', target);
    const background = luminance(palette.background);
    for (const key of ['foreground', 'black', 'red', 'green', 'yellow', 'blue', 'magenta', 'cyan', 'white', 'brightBlack', 'brightRed', 'brightGreen', 'brightYellow', 'brightBlue', 'brightMagenta', 'brightCyan', 'brightWhite']) {
      const foreground = luminance(palette[key]);
      const contrast = (Math.max(background, foreground) + 0.05) / (Math.min(background, foreground) + 0.05);
      assert(contrast >= 4.5, `${target} ${key} must remain readable on the light terminal background; contrast=${contrast.toFixed(2)}`);
    }
  }
});
