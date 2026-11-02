const THEME_STORAGE_KEY = 'hetero.desktop.theme.v1';
const THEMES = Object.freeze(['dark', 'light', 'hybrid']);
const LIGHT_TERMINAL_MINIMUM_CONTRAST_RATIO = 4.5;

const DARK_TERMINAL_THEME = Object.freeze({
  background: '#181818', foreground: '#cccccc', cursor: '#aeafad', selectionBackground: '#264f78',
  black: '#1e1e1e', red: '#f48771', green: '#89d185', yellow: '#cca700', blue: '#75beff',
  magenta: '#c586c0', cyan: '#4ec9b0', white: '#d4d4d4', brightBlack: '#858585',
  brightRed: '#f48771', brightGreen: '#b5cea8', brightYellow: '#dcdcaa', brightBlue: '#9cdcfe',
  brightMagenta: '#d7a0d7', brightCyan: '#4ec9b0', brightWhite: '#ffffff',
});

const LIGHT_TERMINAL_THEME = Object.freeze({
  background: '#ffffff', foreground: '#1f1f1f', cursor: '#1f1f1f', selectionBackground: '#add6ff',
  black: '#000000', red: '#cd3131', green: '#008000', yellow: '#795e26', blue: '#0451a5',
  magenta: '#bc05bc', cyan: '#00758f', white: '#555555', brightBlack: '#666666',
  brightRed: '#cd3131', brightGreen: '#16813e', brightYellow: '#7a6900', brightBlue: '#0451a5',
  brightMagenta: '#bc05bc', brightCyan: '#00758f', brightWhite: '#767676',
});

function normalizeTheme(value) {
  return THEMES.includes(value) ? value : 'dark';
}

function usesDarkTerminal(theme, target) {
  const normalized = normalizeTheme(theme);
  return normalized === 'dark' || (normalized === 'hybrid' && target === 'agent');
}

function terminalThemeFor(theme, target) {
  return { ...(usesDarkTerminal(theme, target) ? DARK_TERMINAL_THEME : LIGHT_TERMINAL_THEME) };
}

function terminalMinimumContrastRatioFor(theme, target) {
  // xterm applies this to all foreground sources, including ANSI 256-color
  // and 24-bit truecolor output. Dark terminals keep their original colors.
  return usesDarkTerminal(theme, target) ? 1 : LIGHT_TERMINAL_MINIMUM_CONTRAST_RATIO;
}

module.exports = {
  DARK_TERMINAL_THEME,
  LIGHT_TERMINAL_MINIMUM_CONTRAST_RATIO,
  LIGHT_TERMINAL_THEME,
  THEMES,
  THEME_STORAGE_KEY,
  normalizeTheme,
  terminalMinimumContrastRatioFor,
  terminalThemeFor,
};
