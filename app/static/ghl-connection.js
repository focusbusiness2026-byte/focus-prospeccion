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
    text('#ghl-auth-help', s.connected ? 'La autorización de esta empresa está guardada.' :
      s.state === 'wrong_location' ? 'La autorización no coincide con esta empresa. El administrador debe revisar la subcuenta antes de continuar.' :
      s.can_connect ? 'Pulsa Autorizar GoHighLevel y concede acceso solo a la subcuenta que abriste.' :
      !s.admin ? 'Pide a tu administrador que vincule y autorice esta subcuenta. No necesitas compartir claves.' :
      !s.oauth_ready ? 'Focus Business debe preparar OAuth en el servidor antes de poder autorizar. Puedes revisar el ID de la subcuenta mientras tanto.' :
      'Abre Configuración de la subcuenta, pega su ID y guarda. Después vuelve y pulsa Volver a comprobar.');
    text('#ghl-send-help', s.connected && complete && s.sending_enabled ? 'La conexión y sus comprobaciones están listas para revisar una campaña.' :
      !s.sending_enabled ? 'Envíos deshabilitados. La administración debe revisar los requisitos antes de habilitarlos.' :
      'Todavía faltan comprobaciones de la conexión o del envío.');
    const admin = $('#ghl-admin-link'), connect = $('#ghl-connect-action');
    admin.href = '/campaigns/outreach?account=' + encodeURIComponent(tenant) + '#ghl-setup';
    admin.hidden = !s.admin || s.connected;
    connect.href = '/api/intelligence/' + encodeURIComponent(tenant) + '/outreach/ghl/connect';
    connect.hidden = !s.can_connect || s.connected;
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
  }

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
