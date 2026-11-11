'use strict';
// The embedded view uses the existing board, dialogs, and CRM write queue.
(() => {
  if (!embeddedCrmSource || window.parent === window) return;
  const tenant = new URLSearchParams(location.search).get('tenant');
  const frame = window.frameElement;
  if (!tenant || !frame) return;
  const content = document.querySelector('.portal-app > .content');
  const setViewport = () => {
    const bounds = frame.getBoundingClientRect();
    const top = Math.max(0, -bounds.top);
    const available = Math.max(0, Math.min(window.parent.innerHeight, bounds.bottom) - Math.max(0, bounds.top));
    document.documentElement.style.setProperty('--guide-dialog-top', (top + 12) + 'px');
    document.documentElement.style.setProperty('--guide-dialog-height', Math.max(96, available - 24) + 'px');
  };
  const sizing = new ResizeObserver(() => {
    const height = Math.ceil(content.getBoundingClientRect().height);
    if (height > 0) window.parent.postMessage({type:'focus-guide-crm-height', height}, location.origin);
    setViewport();
  });
  sizing.observe(content);
  const view = new AbortController();
  const board = document.querySelector('#crm-preview-board');
  const relativeTime = new Intl.RelativeTimeFormat('es-ES', {numeric:'always'});
  const fullDate = new Intl.DateTimeFormat('es-ES', {dateStyle:'full', timeStyle:'short'});
  let completedIds = new Set();
  const pulsedIds = new Set();
  const pulseObserver = new IntersectionObserver(entries => {
    for (const {target, isIntersecting} of entries) {
      const id = target.dataset.dragProspect;
      if (!isIntersecting || !target.isConnected || !completedIds.has(id) || pulsedIds.has(id)) continue;
      pulsedIds.add(id);
      target.classList.add('guide-lead-pulse');
      pulseObserver.unobserve(target);
    }
  });

  function refreshScrapeAges() {
    const now = Date.now();
    for (const label of board.querySelectorAll('.crm-scrape-age')) {
      const minutes = Math.floor((now - Date.parse(label.dateTime)) / 60000);
      const age = minutes < 1 ? 'Ahora mismo' : minutes < 60 ? relativeTime.format(-minutes, 'minute') :
        minutes < 1440 ? relativeTime.format(-Math.floor(minutes / 60), 'hour') :
          relativeTime.format(-Math.floor(minutes / 1440), 'day');
      if (label.textContent !== age) label.textContent = age;
    }
  }

  function labelScrapeDates() {
    const dates = new Map(dashboardData.prospects.map(lead => [lead.execution_id, lead.created_at]));
    for (const card of board.querySelectorAll('[data-drag-prospect]')) {
      const heading = card.querySelector('.crm-card-heading');
      if (!heading || heading.querySelector('.crm-scrape-age')) continue;
      const stamp = Date.parse(dates.get(card.dataset.dragProspect));
      if (!Number.isFinite(stamp)) continue;
      const date = new Date(stamp);
      const label = document.createElement('time');
      label.className = 'crm-scrape-age';
      label.dateTime = date.toISOString();
      label.title = 'Raspado: ' + fullDate.format(date);
      heading.append(label);
    }
    refreshScrapeAges();
  }

  function highlightNewLeads() {
    pulseObserver.disconnect();
    for (const card of board.querySelectorAll('[data-drag-prospect]')) {
      const id = card.dataset.dragProspect;
      const isNew = completedIds.has(id) && card.dataset.currentColumn === 'Nuevo';
      card.classList.toggle('guide-lead-highlight', isNew);
      if (isNew && !pulsedIds.has(id)) pulseObserver.observe(card);
    }
  }

  const cardObserver = new MutationObserver(() => {
    labelScrapeDates();
    highlightNewLeads();
  });
  cardObserver.observe(board, {childList:true});
  labelScrapeDates();
  const ageTimer = window.setInterval(refreshScrapeAges, 15000);
  window.parent.addEventListener('scroll', setViewport, {passive:true, signal:view.signal});
  window.parent.addEventListener('resize', setViewport, {passive:true, signal:view.signal});
  window.addEventListener('pagehide', () => {
    view.abort();
    sizing.disconnect();
    cardObserver.disconnect();
    pulseObserver.disconnect();
    window.clearInterval(ageTimer);
  }, {once:true});
  const synced = new Set(), pending = new Set();
  const status = document.createElement('p');
  status.hidden = true;
  status.setAttribute('role', 'status');
  board.before(status);
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
    if (event.source !== window.parent || event.origin !== location.origin) return;
    if (event.data?.type === 'focus-guide-show-leads') {
      board.querySelector('.guide-lead-highlight')?.scrollIntoView({block:'center'});
      return;
    }
    if (event.data?.type !== 'focus-guide-leads' || !Array.isArray(event.data.ids)) return;
    await portalInitialLoad;
    const completed = Array.isArray(event.data.completed) ? event.data.completed : [];
    const ids = new Set(completed.slice(0, 5).filter(id => typeof id === 'string' && id.length <= 80));
    if (ids.size !== completedIds.size || [...ids].some(id => !completedIds.has(id))) {
      completedIds = ids;
      pulsedIds.clear();
      highlightNewLeads();
    }
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
