'use strict';
(() => {
  const $ = (selector) => document.querySelector(selector);
  const text = (selector, value) => { $(selector).textContent = value; };
  let busy = false, loginConfirmed = false, currentStep = null, current = null;

  function highlight(step, moveFocus = false) {
    for (const node of $('#ghl-steps').children) {
      node.classList.toggle('ghl-current', node.id === step);
      if (node.id === step) node.setAttribute('aria-current', 'step');
      else node.removeAttribute('aria-current');
    }
    if (moveFocus) {
      const target = $('#' + step + ' h2');
      target.tabIndex = -1;
      target.focus();
    }
  }

  function render(s) {
    const tenant = $('#ghl-account').value;
    const complete = s.domain_verified && s.workflow_verified && s.unsubscribe_verified;
    const pending = [['dominio y remitente', s.domain_verified], ['secuencia', s.workflow_verified],
      ['baja y reglas de parada', s.unsubscribe_verified]].filter(([,ok]) => !ok).map(([label]) => label);
    const company = s.company || $('#ghl-account').selectedOptions[0]?.textContent || 'tu empresa';
    text('#ghl-company', company);
    text('#ghl-location-name', company);
    text('#ghl-status', s.connected ? 'Subcuenta conectada.' : 'Subcuenta sin conectar.');
    text('#ghl-verify-status', complete ? 'Comprobaciones confirmadas.' : 'Pendiente: ' + pending.join(', ') + '.');
    text('#ghl-sender', s.sender_email ? 'Remitente configurado · ' + s.sender_email +
      (s.domain_verified ? ' · Verificación administrativa registrada' : ' · Verificación pendiente') : 'Remitente pendiente de configurar.');
    text('#ghl-assets-status', (s.workflow_linked ? 'Flujo vinculado' : 'Flujo pendiente de vincular') +
      ' · Plantillas vinculadas: ' + s.template_count + (s.asset_check_enabled ? '' : ' · Comprobación en GHL pendiente de autorización'));
    $('#ghl-check-assets').hidden = !(s.admin && s.connected && s.asset_check_enabled);
    $('#ghl-asset-results').hidden = true;
    $('#ghl-asset-results').replaceChildren();
    text('#ghl-auth-help', s.setup_permissions_missing ? 'Renueva la autorización para conceder permisos de plantillas y workflows.' :
      s.connected ? 'La autorización de esta empresa está guardada.' :
      s.state === 'wrong_location' ? 'La autorización no coincide con esta empresa. El administrador debe revisar la subcuenta antes de continuar.' :
      s.can_connect ? 'Pulsa Autorizar GoHighLevel y concede acceso solo a la subcuenta que abriste.' :
      !s.admin ? 'Pide a tu administrador que vincule y autorice esta subcuenta. No necesitas compartir claves.' :
      !s.oauth_ready ? 'Focus Business debe preparar OAuth en el servidor antes de poder autorizar. Puedes revisar el ID de la subcuenta mientras tanto.' :
      'Pega el enlace de la subcuenta abierta en GoHighLevel y guarda. Después autoriza esa subcuenta.');
    text('#ghl-send-help', s.connected && complete && s.sending_enabled ? 'La conexión y sus comprobaciones están listas para revisar una campaña.' :
      !s.sending_enabled ? 'Envíos deshabilitados. La administración debe revisar los requisitos antes de habilitarlos.' :
      'Todavía faltan comprobaciones de la conexión o del envío.');
    const admin = $('#ghl-admin-link'), connect = $('#ghl-connect-action');
    $('#ghl-location-form').hidden = !s.admin || s.location_configured;
    admin.href = '/campaigns/outreach?account=' + encodeURIComponent(tenant) + '#ghl-setup';
    admin.hidden = !s.admin || s.connected;
    connect.href = '/api/intelligence/' + encodeURIComponent(tenant) + '/outreach/ghl/connect';
    connect.hidden = !s.can_connect || (s.connected && !s.setup_permissions_missing);
    $('#ghl-next-step').hidden = s.connected || loginConfirmed;
    $('#ghl-steps').hidden = $('#ghl-recheck').hidden = false;
    currentStep = !s.connected ? (loginConfirmed ? 'ghl-authorize' : 'ghl-login') :
      !complete ? 'ghl-verify' : 'ghl-send';
    highlight(currentStep);
  }

  async function load() {
    const tenant = $('#ghl-account').value;
    const r = await fetch('/api/intelligence/' + encodeURIComponent(tenant) + '/outreach/ghl/status', {credentials:'same-origin'});
    const s = await r.json();
    if (!r.ok) throw Error(typeof s.detail === 'string' ? s.detail : 'No se pudo comprobar la conexión.');
    current = s;
    render(s);
    const capability = $('#ghl-capability');
    capability.hidden = !(s.admin && s.connected && s.asset_check_enabled);
    if (capability.hidden) return;
    capability.textContent = 'Comprobando la API de workflows…';
    try {
      const response = await fetch('/api/intelligence/' + encodeURIComponent(tenant) + '/outreach/ghl/workflow-capability', {credentials:'same-origin'});
      const result = await response.json();
      if (!response.ok) throw Error(typeof result.detail === 'string' ? result.detail : 'No se pudo comprobar la API de workflows.');
      const states = {can_prepare_draft:'La API de workflows responde y hay permisos de preparación. El primer borrador debe validarse con GoHighLevel.',
        base_workflow:'La API de workflows está limitada para esta subcuenta. Usa un flujo base instalado en GoHighLevel.',
        permission_pending:'La API responde. Falta renovar la autorización con permisos de plantillas y workflows.'};
      capability.textContent = states[result.state] || 'Capacidad de workflows sin confirmar.';
    } catch (error) {
      capability.textContent = error.message;
    }
  }

  $('#ghl-location-form').onsubmit = event => {
    event.preventDefault();
    const form = event.currentTarget;
    work(async () => {
      const token = document.cookie.split('; ').find(row => row.startsWith('focus_csrf='))?.slice('focus_csrf='.length) || '';
      const tenant = $('#ghl-account').value;
      const response = await fetch('/api/intelligence/' + encodeURIComponent(tenant) + '/outreach/ghl/location', {
        method:'PUT', credentials:'same-origin', headers:{'Content-Type':'application/json', 'X-CSRF-Token':decodeURIComponent(token)},
        body:JSON.stringify({url:new FormData(form).get('url')}),
      });
      const result = await response.json();
      if (!response.ok) throw Error(typeof result.detail === 'string' ? result.detail : 'Revisa el enlace de GoHighLevel.');
      form.reset();
      loginConfirmed = true;
      await load();
    });
  };

  async function work(action) {
    if (busy) return;
    busy = true;
    $('#ghl-account').disabled = $('#ghl-recheck').disabled = true;
    $('#ghl-error').hidden = true;
    text('#ghl-status', 'Comprobando la conexión…');
    try { await action(); }
    catch (error) { text('#ghl-error', error.message); $('#ghl-error').hidden = false; text('#ghl-status', 'Comprobación pendiente.'); }
    finally { busy = false; $('#ghl-account').disabled = $('#ghl-recheck').disabled = false; }
  }

  $('#ghl-check-assets').onclick = async () => {
    if (busy) return;
    busy = true;
    const button = $('#ghl-check-assets'), result = $('#ghl-asset-results');
    const account = $('#ghl-account');
    account.disabled = button.disabled = true;
    $('#ghl-error').hidden = true;
    result.hidden = true;
    text('#ghl-assets-status', 'Consultando los recursos instalados en GoHighLevel…');
    try {
      const response = await fetch('/api/intelligence/' + encodeURIComponent(account.value) + '/outreach/ghl/assets', {credentials:'same-origin'});
      const report = await response.json();
      if (!response.ok) throw Error(typeof report.detail === 'string' ? report.detail : 'No se pudieron consultar los recursos.');
      const states = {draft:'Flujo encontrado en borrador', published:'Flujo encontrado y publicado',
        missing:'Falta instalar el flujo Focus', ambiguous:'Hay varios flujos Focus. La administración debe resolver el duplicado', unknown:'Estado del flujo sin confirmar'};
      const rows = [states[report.workflow_state], report.templates_found + ' de 5 plantillas encontradas',
        report.template_senders_match + ' de 5 plantillas con el remitente configurado',
        report.fields_found + ' de ' + report.fields_expected + ' campos de contacto encontrados',
        report.workflow_sender_matches === null ? 'Remitente del flujo sin comprobar' :
          report.workflow_sender_matches ? 'El remitente del flujo coincide con el configurado' : 'El remitente del flujo requiere revisión'];
      result.replaceChildren(...rows.map(value => {
        const item = document.createElement('li');
        item.textContent = value;
        return item;
      }));
      result.hidden = false;
      text('#ghl-assets-status', 'Consulta terminada. Los recursos encontrados están pendientes de revisión administrativa.');
    } catch (error) {
      text('#ghl-assets-status', error.message || 'No se pudieron consultar los recursos.');
    } finally {
      account.disabled = button.disabled = false;
      busy = false;
    }
  };
  $('#ghl-recheck').onclick = () => work(load);
  $('#ghl-next-step').onclick = () => {
    if (!current || busy) return;
    loginConfirmed = true; render(current); highlight(currentStep, true);
  };
  work(async () => {
    const r = await fetch('/api/intelligence/accounts', {credentials:'same-origin'}), data = await r.json();
    if (!r.ok || !data.accounts?.length) throw Error('No hay empresas autorizadas para esta sesión.');
    const select = $('#ghl-account');
    for (const account of data.accounts) select.add(new Option(account.name, account.id));
    $('#ghl-account-label').hidden = data.accounts.length < 2;
    const requested = new URLSearchParams(location.search).get('account');
    if (data.accounts.some((account) => account.id === requested)) select.value = requested;
    select.onchange = () => work(async () => {
      loginConfirmed = false; current = null; $('#ghl-steps').hidden = true;
      await load();
    });
    await load();
  });
})();
