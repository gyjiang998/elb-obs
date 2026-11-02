(() => {
  const allowed = new Set(['dark', 'light', 'hybrid']);
  try {
    const saved = localStorage.getItem('hetero.desktop.theme.v1');
    document.documentElement.dataset.theme = allowed.has(saved) ? saved : 'dark';
  } catch {
    document.documentElement.dataset.theme = 'dark';
  }
})();
