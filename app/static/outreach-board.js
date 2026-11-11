'use strict';
// Loaded exclusively by the sandbox entry point; never changes CRM state.
(() => {
  const selected = new Set();
  let controls, preparing = false;
  function refresh() {
    const board = document.querySelector('#crm-preview-board');
    if (!board) return;
    if (!controls || !controls.isConnected) {
      controls = document.createElement('div');
      controls.className = 'crm-campaign-controls';
      controls.innerHTML = '<div><strong>Campañas de contacto</strong><label><input type="checkbox" data-campaign-all> Seleccionar todos los aprobados visibles</label><small data-campaign-count></small></div><div class="crm-campaign-channels"><button class="primary" type="button" data-campaign-prepare="email">Preparar 5 correos</button><button class="secondary" type="button" data-campaign-prepare="social">LinkedIn y redes</button></div><p data-campaign-status role="status" hidden></p>';
      board.before(controls);
      controls.querySelector('[data-campaign-all]').onchange = event => {
        board.querySelectorAll('[data-campaign-select]').forEach(input => {input.checked = event.target.checked; if(input.checked)selected.add(input.value);else selected.delete(input.value);});
        count();
      };
      controls.querySelectorAll('[data-campaign-prepare]').forEach(button => button.onclick = () => {
        const ids = [...board.querySelectorAll('[data-campaign-select]:checked')].map(input => input.value);
        prepare(ids, button.dataset.campaignPrepare, controls.querySelector('[data-campaign-status]'));
      });
    }
    board.querySelectorAll('.crm-board-card[data-current-column="Aprobado para descarga"]').forEach(card => {
      if (card.querySelector('[data-campaign-select]')) return;
      const label = document.createElement('label');
      const input = document.createElement('input');
      input.type = 'checkbox'; input.value = card.dataset.dragProspect; input.dataset.campaignSelect = 'true';
      input.checked = selected.has(input.value); input.onchange = () => {if(input.checked)selected.add(input.value);else selected.delete(input.value);count();};
      const actions = document.createElement('div');
      actions.className = 'crm-card-campaign';
      const email = document.createElement('button');
      email.type = 'button';
      email.className = 'secondary';
      email.dataset.campaignLeadEmail = '';
      email.textContent = 'Preparar email';
      const status = document.createElement('small');
      status.setAttribute('role', 'status');
      status.hidden = true;
      email.onclick = () => prepare([input.value], 'email', status);
      label.append(input, document.createTextNode(' Seleccionar para campaña'));
      actions.append(label, email, status);
      card.append(actions);
    });
    count();
  }
  async function prepare(ids, channel, status) {
    if (!ids.length || preparing) return;
    status.hidden = false;
    if (ids.length > 100) {
      status.textContent = 'Selecciona hasta 100 leads por campaña.';
      return;
    }
    preparing = true;
    count();
    status.textContent = 'Cargando los destinatarios…';
    try {
      const csrf = document.cookie.split('; ').find(row => row.startsWith('focus_csrf='))?.slice('focus_csrf='.length) || '';
      const account = 'three-dots';
      const response = await fetch('/api/intelligence/' + account + '/sync-approved-selection', {
        method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': decodeURIComponent(csrf)},
        body: JSON.stringify({ids}),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok || !result.ok) {
        throw Error(typeof result.detail === 'string' ? result.detail : 'No se pudo cargar la selección. Revisa tu sesión y vuelve a intentarlo.');
      }
      const handoff = new URLSearchParams({account, channel, from: 'crm'});
      sessionStorage.setItem('focus-campaign-selection', JSON.stringify({ids, channel, account}));
      status.textContent = channel === 'email' ? 'Abriendo la preparación de correos…' : 'Abriendo la preparación de mensajes…';
      window.top.location.assign('/campaigns/outreach?' + handoff);
    } catch (error) {
      status.textContent = error.message || 'No se pudo preparar la selección.';
      preparing = false;
      count();
    }
  }

  function count() {
    const board = document.querySelector('#crm-preview-board');
    if (!controls || !board) return;
    const inputs = [...board.querySelectorAll('[data-campaign-select]')];
    const visible = inputs.filter(input => selected.has(input.value)).length;
    const label = controls.querySelector('[data-campaign-count]');
    const text = visible === 1 ? '1 lead aprobado seleccionado' : visible + ' leads aprobados seleccionados';
    if (label.textContent !== text) label.textContent = text;
    const all = controls.querySelector('[data-campaign-all]');
    all.checked = visible > 0 && visible === inputs.length;
    all.indeterminate = visible > 0 && visible < inputs.length;
    controls.querySelectorAll('[data-campaign-prepare]').forEach(button => button.disabled = preparing || !visible);
    board.querySelectorAll('[data-campaign-lead-email]').forEach(button => button.disabled = preparing);
  }
  new MutationObserver(refresh).observe(document.body,{childList:true,subtree:true});
  refresh();
})();
