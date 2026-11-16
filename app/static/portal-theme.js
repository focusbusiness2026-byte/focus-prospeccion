'use strict';

(() => {
  const storageKey = 'focus-prospeccion-theme';
  const root = document.documentElement;
  let theme = 'dark';
  try {
    if (localStorage.getItem(storageKey) === 'light') theme = 'light';
  } catch {
    // The current page can still switch themes with storage disabled.
  }
  root.dataset.portalTheme = theme;

  document.addEventListener('DOMContentLoaded', () => {
    const button = document.getElementById('portal-theme-toggle');
    if (!button) return;

    const refreshLabel = () => {
      button.textContent = theme === 'light' ? 'Modo oscuro' : 'Modo claro';
    };

    refreshLabel();
    button.hidden = false;
    button.addEventListener('click', () => {
      theme = theme === 'light' ? 'dark' : 'light';
      root.dataset.portalTheme = theme;
      refreshLabel();
      try {
        localStorage.setItem(storageKey, theme);
      } catch {
        // Storage availability does not affect the current choice.
      }
    });
  });
})();
