'use strict';
// The embedded view uses the existing board, dialogs, and CRM write queue.
(() => {
  if (!embeddedCrmSource || window.parent === window) return;
  const tenant = new URLSearchParams(location.search).get('tenant');
  if (!tenant) return;
  const synced = new Set(), pending = new Set();
  const status = document.createElement('p');
  status.hidden = true;
  status.setAttribute('role', 'status');
  document.querySelector('#crm-preview-board').before(status);
  let loading = false;

  async function sync() {
    if (loading || !pending.size) return;
    loading = true;
    const ids = [...pending].slice(0, 5);
    ids.forEach((id) => pending.delete(id));
    try {
      const params = new URLSearchParams({ids:ids.join(',')});
      const response = await fetch('/api/intelligence/' + encodeURIComponent(tenant) + '/guide/leads?' + params, {
        credentials:'same-origin', signal:AbortSignal.timeout(15000),
      });
      if (!response.ok) throw Error('No se pudieron actualizar las tarjetas.');
      const rows = (await response.json()).filter((lead) => lead.onboarding_id === embeddedCrmSource && ids.includes(lead.execution_id));
      const current = new Set(dashboardData.prospects.map((lead) => lead.execution_id));
      const additions = rows.filter((lead) => !current.has(lead.execution_id));
      rows.forEach((lead) => synced.add(lead.execution_id));
      if (additions.length) {
        dashboardData.prospects.unshift(...additions);
        renderProspects();
        renderCrmBoard();
        for (const lead of additions) {
          document.querySelector('[data-drag-prospect="' + CSS.escape(lead.execution_id) + '"][data-current-column="Nuevo"]')
            ?.classList.add('guide-lead-arrived');
        }
      }
      status.hidden = true;
    } catch {
      status.textContent = 'No se pudo actualizar el tablero en este momento. Recarga Prospección para ver los leads guardados.';
      status.hidden = false;
    } finally {
      loading = false;
      if (pending.size) sync();
    }
  }

  window.addEventListener('message', async (event) => {
    if (event.source !== window.parent || event.origin !== location.origin ||
        event.data?.type !== 'focus-guide-leads' || !Array.isArray(event.data.ids)) return;
    await portalInitialLoad;
    for (const id of event.data.ids.slice(0, 5)) {
      if (typeof id === 'string' && id.length <= 80 && !synced.has(id)) pending.add(id);
    }
    sync();
  });

  portalInitialLoad.then(() => {
    if (!dashboardData.sources.length && !dashboardData.demo) {
      status.textContent = 'No se pudo cargar el tablero. Recarga Prospección para volver a intentarlo.';
      status.hidden = false;
    }
    window.parent.postMessage({type:'focus-guide-crm-ready'}, location.origin);
  });
})();
