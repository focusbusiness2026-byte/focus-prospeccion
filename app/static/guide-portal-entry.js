'use strict';
// Keep the existing search execution and history, with the guide as the only editor.
(() => {
  const list = document.querySelector('#source-list');
  if (!list) return;
  let guidedApplied = false;
  const hide = (node) => {
    node.hidden = true;
    node.style.setProperty('display', 'none', 'important');
  };
  document.querySelectorAll('#recommended-config,#save-config').forEach(hide);
  const intro = document.querySelector('#sources .intro');
  if (intro) intro.textContent = 'Define tu cliente ideal con la guía o vuelve a usar una búsqueda guardada.';
  const info = document.querySelector('#sources [data-info-title="Prospección"]');
  if (info) info.dataset.infoCopy = 'Selecciona tu empresa y abre Prospección guiada para configurar una búsqueda. Las búsquedas guardadas siguen disponibles para investigar.';

  function refresh() {
    list.querySelectorAll('.research-workspace:not([data-guide-entry])').forEach((workspace) => {
      workspace.dataset.guideEntry = 'true';
      const form = workspace.querySelector('.research-builder');
      if (!form) return;
      const savedIntro = workspace.querySelector('.saved-search-dashboard p:not(.eyebrow)');
      if (savedIntro) savedIntro.textContent = 'Selecciona una búsqueda existente y aplícala para investigar.';
      form.querySelectorAll('.builder-heading,.builder-tabs,[data-builder-step],[data-prospecting-improvements],.automation-panel,.research-option-select').forEach(hide);
      // The legacy scheduling name is required for saving a schedule, not for starting a search.
      const scheduleName = form.querySelector('[data-schedule-name]');
      if (scheduleName) scheduleName.required = false;
      const entry = document.createElement('div');
      entry.className = 'builder-card active';
      const link = document.createElement('a');
      link.className = 'primary button';
      link.href = '/prospecting-guide?source=' + encodeURIComponent(workspace.dataset.source);
      link.textContent = 'Configurar con Prospección guiada →';
      entry.append(link);
      workspace.prepend(entry);
    });
    const params = new URLSearchParams(location.search);
    const saved = params.get('guided_search'), source = params.get('guided_source');
    if (!saved || !source || guidedApplied) return;
    const workspace = list.querySelector('.research-workspace[data-source="' + CSS.escape(source) + '"]');
    const selector = workspace?.querySelector('[data-saved-search-selector]');
    if (selector && [...selector.options].some((option) => option.value === saved)) {
      guidedApplied = true;
      selector.value = saved;
      workspace.querySelector('[data-apply-saved-search]')?.click();
    } else {
      const company = document.querySelector('#source-selector');
      if (company && company.value !== source && [...company.options].some((option) => option.value === source)) {
        company.value = source;
        company.dispatchEvent(new Event('change', {bubbles:true}));
      }
    }
  }
  new MutationObserver(refresh).observe(list, {childList:true, subtree:true});
  refresh();
})();
