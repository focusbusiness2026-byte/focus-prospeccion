'use strict';
(() => {
  const $ = (selector) => document.querySelector(selector);
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
  const labels = {sectors:'Sectores', target_countries:'Países', target_city:'Ciudad', target_region:'Región', client_types:'Tipo de cliente', ideal_company_size:'Perfil o tamaño', minimum_budget:'Presupuesto', decision_roles:'Decisores', keywords:'Contexto', excluded_sectors:'Sectores excluidos', exclusions:'Exclusiones', preferences:'Preferencias'};
  const statuses = {review_required:'No pudimos confirmar el último paso. Consulta su estado antes de volver a empezar.', applied:'Búsqueda guardada.'};
  let tenant = '', session = null, busy = false, aiReady = false, startId = null, shownQuestion = '';
  let answerRequest = null, operation = null, waitTimer = null, intakeLoaded = false;
  let stateTimer = null, stateChecks = 0;
  let historyOpen = false, resetOpen = false, editIndex = null, revisionRequest = null;
  let saved = new Map();
  const isWaiting = () => ['generating', 'applying'].includes(session?.state);
  const dialog = $('#guide-busy');
  dialog.addEventListener('cancel', (event) => event.preventDefault());

  function reveal(selector, block = 'start') {
    const element = $(selector);
    element.focus({preventScroll:true});
    element.scrollIntoView({block, behavior:matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'});
  }

  function stage() {
    return session && [session.id, session.version, session.state].join(':');
  }

  function revealStep() {
    const target = historyOpen ? '#guide-history-label' : session?.state === 'checkpoint' ? '#guide-checkpoint-title' :
      ['ready','applied'].includes(session?.state) ? '#guide-review-title' : !$('#guide-answer').hidden ? '#guide-question' : null;
    if (target) reveal(target);
  }

  function remember() {
    if (!session) return;
    saved.set(session.id, {id:session.id, created_at:session.created_at, count:session.history.length,
      state:session.state, revision:Boolean(session.revision_of)});
  }

  async function api(path = '', method = 'GET', data) {
    const csrf = decodeURIComponent(document.cookie.split('; ').find((x) => x.startsWith('focus_csrf='))?.split('=').slice(1).join('=') || '');
    const response = await fetch('/api/intelligence/' + encodeURIComponent(tenant) + '/guide' + path, {
      method, credentials:'same-origin', headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},
      body: data === undefined ? undefined : JSON.stringify(data),
    });
    let body;
    try { body = await response.json(); } catch { throw Error('No se confirmó la operación. Consulta el estado guardado.'); }
    if (!response.ok) throw Error(typeof body.detail === 'string' ? body.detail : 'Revisa la solicitud.');
    return body;
  }

  async function work(action, title = 'Cargando tu empresa…') {
    if (busy) return;
    const before = stage(), scrollOnChange = intakeLoaded;
    let failed = false;
    clearTimeout(stateTimer);
    busy = true; operation = {title, long: false}; $('#guide-error').hidden = true; render();
    waitTimer = setTimeout(() => { operation.long = true; render(); }, 10000);
    try { await action(); }
    catch (error) { failed = true; $('#guide-error').textContent = error.message; $('#guide-error').hidden = false; }
    finally {
      clearTimeout(waitTimer); operation = null; busy = false; remember(); render(); followSavedState();
      if (failed && !dialog.open) reveal('#guide-error', 'nearest');
      else if (scrollOnChange && before !== stage() && !dialog.open) revealStep();
    }
  }

  function followSavedState() {
    if (!isWaiting()) { stateChecks = 0; return; }
    if (stateChecks >= 16) return;
    stateTimer = setTimeout(() => work(async () => {
      stateChecks += 1;
      try { session = await api('/sessions/' + session.id); }
      catch (error) { stateChecks = 16; throw error; }
    }, session.state === 'applying' ? 'Comprobando si se guardó tu búsqueda…' : 'Comprobando si tu pregunta está lista…'), 6000);
  }

  function learning(s, n, limit) {
    const feedback = s?.feedback;
    const context = s?.turn ? ['onboarding',
      s.web_status === 'consulted' && 'web',
      (feedback?.approved?.count || feedback?.discarded?.count) && 'decisiones de leads',
      s.prior_approved && 'enfoque previo',
    ].filter(Boolean) : [];
    const sources = context.length;
    const remaining = Math.max(0, limit - n);
    const legacy = Boolean(s && !s.round_limit);
    const label = legacy ? n + (n === 1 ? ' respuesta anterior' : ' respuestas anteriores') : n + ' de ' + limit + ' respuestas';
    const note = s?.state === 'applied' ? 'Enfoque guardado. Puedes abrirlo para investigar.' :
      legacy ? 'Empieza con opciones para seguir afinando tu búsqueda.' :
      !remaining ? 'Ya puedes revisar el enfoque.' :
      remaining === 1 ? 'Falta 1 pregunta para revisar el enfoque.' : 'Faltan ' + remaining + ' preguntas para revisar el enfoque.';
    const progress = $('#guide-progress');
    progress.max = limit + sources; progress.value = n + sources;
    progress.setAttribute('aria-valuetext', label + (sources ? ', ' + sources + ' aportes de contexto' : ''));
    $('#guide-progress-label').textContent = label;
    $('#guide-learning-note').textContent = (sources ? 'Contexto: ' + context.join(' · ') + '. ' : '') + note;
    return note;
  }

  function liveText(selector, text) {
    const el = $(selector);
    if (el.textContent !== text) el.textContent = text;
  }

  function showIntake(intake) {
    $('#guide-intake').innerHTML = intake.map((section, index) =>
      '<details' + (index === 0 ? ' open' : '') + '><summary>' + esc(section.title) +
      '</summary><dl>' + section.fields.map(({label, value}) =>
        '<div><dt>' + esc(label) + '</dt><dd>' + esc(value) + '</dd></div>').join('') + '</dl></details>').join('');
  }

  function render() {
    const s = session, n = s?.history.length || 0, limit = s?.round_limit || 10;
    const legacy = Boolean(s && !s.round_limit), isApplied = s?.state === 'applied';
    const showFlow = !historyOpen && !resetOpen;
    const canAsk = showFlow && s?.state === 'draft' && !legacy && Boolean(s.turn);
    const waiting = isWaiting();
    const ready = s?.state === 'ready', checkpoint = s?.state === 'checkpoint';
    const canExtend = !legacy && limit < 20;
    const criteriaComplete = ['sectors','target_countries'].every((key) => s?.criteria[key]?.some((value) => value.trim()));
    $('#guide-account').disabled = busy;
    $('#guide-start').hidden = Boolean(s) || !intakeLoaded || !showFlow; $('#guide-start').disabled = busy || !aiReady;
    $('#guide-new').hidden = !s || !showFlow;
    $('#guide-new').disabled = busy || waiting || !aiReady;
    $('#guide-history-open').hidden = !showFlow || ![...saved.values()].some((item) => item.count);
    $('#guide-refresh').hidden = !tenant || (!waiting && s?.state !== 'review_required' && $('#guide-error').hidden);
    $('#guide-refresh').disabled = busy;
    const note = learning(s, n, limit);
    const showWait = busy || (waiting && stateChecks < 16);
    const waitTitle = operation?.title || (s?.state === 'applying' ? 'Guardando tu búsqueda…' : 'Preparando la siguiente pregunta…');
    liveText('#guide-busy-title', showWait ? waitTitle : '');
    liveText('#guide-busy-detail', showWait
      ? (operation?.long ? 'La consulta sigue en curso. ' : '') + (intakeLoaded ? note : 'Estamos leyendo tu ficha de onboarding.')
      : '');
    if (showWait && !dialog.open) {
      dialog.showModal();
      document.documentElement.classList.add('guide-locked');
    } else if (!showWait && dialog.open) {
      document.documentElement.classList.remove('guide-locked');
      dialog.close();
    }
    liveText('#guide-status', !showFlow ? '' : waiting && !showWait ? 'Aún no se confirmó el resultado. Usa «Consultar estado» para comprobarlo.' : statuses[s?.state] ||
      (!s ? (intakeLoaded && !aiReady ? 'La administración debe habilitar el asistente para esta cuenta.' : '') :
        legacy && s.state === 'draft' ? 'Tu conversación anterior se conserva.' : canAsk ? 'Pregunta ' + (n + 1) + ' de ' + limit : ''));
    $('#guide-answer').hidden = !canAsk;
    $('#guide-question-fieldset').disabled = busy;
    $('#guide-next').disabled = busy;
    if (canAsk && shownQuestion !== s.id + ':' + s.version) {
      shownQuestion = s.id + ':' + s.version;
      $('#guide-question').textContent = s.turn.question;
      $('#guide-options').innerHTML = s.turn.options.map((option, index) =>
        '<label class="guide-option"><input type="radio" name="choice" value="' + index + '" required> ' + esc(option) + '</label>').join('') +
        '<label class="guide-option"><input type="radio" name="choice" value="unknown" required> No lo sé todavía</label>' +
        '<label class="guide-option"><input type="radio" name="choice" value="other" required> Otra respuesta</label>';
      $('#guide-custom').hidden = true; $('#answer').required = false; $('#answer').value = '';
    }
    $('#answer').disabled = busy;
    $('#guide-checkpoint').hidden = !showFlow || !checkpoint;
    $('#guide-checkpoint-title').textContent = canExtend ? '¿Afinamos un poco más?' : 'Ya tienes tu enfoque';
    $('#guide-checkpoint-summary').textContent = s?.turn?.summary || '';
    $('#guide-review-choice').disabled = busy;
    $('#guide-more').hidden = !canExtend; $('#guide-more').disabled = busy;
    $('#guide-review').hidden = !showFlow || (!ready && !isApplied);
    $('#guide-summary').textContent = s?.turn?.summary || '';
    $('#guide-criteria').innerHTML = Object.entries(s?.criteria || {}).map(([key, value]) =>
      '<div><dt>' + esc(labels[key] || key) + '</dt><dd>' + esc((Array.isArray(value) ? value.join(', ') : value) || 'Por confirmar') + '</dd></div>').join('');
    $('#guide-assumptions').innerHTML = (s?.turn?.assumptions || []).map((value) => '<p>' + esc(value) + '</p>').join('');
    $('#guide-confirm-label').hidden = !ready || !criteriaComplete;
    $('#guide-missing').hidden = !ready || criteriaComplete;
    $('#guide-confirm').disabled = busy;
    $('#guide-apply').hidden = !ready || !criteriaComplete; $('#guide-apply').disabled = busy || !$('#guide-confirm').checked;
    $('#guide-more-review').hidden = !ready || !canExtend; $('#guide-more-review').disabled = busy;
    $('#guide-research').hidden = !isApplied;
    if (s?.saved_search) $('#guide-research').href = '/portal?guided_source=' + encodeURIComponent(s.profile.onboarding_id) + '&guided_search=' + encodeURIComponent(s.saved_search.saved_search_id) + '#sources';
    $('#guide-reset').hidden = !resetOpen;
    $('#guide-history').hidden = !historyOpen;
    if (historyOpen) {
      const items = [...saved.values()].sort((a,b) => b.created_at - a.created_at);
      const selector = $('#guide-history-account');
      selector.innerHTML = items.map((item) => {
        const date = new Date(item.created_at * 1000).toLocaleString('es-ES', {dateStyle:'short', timeStyle:'short', timeZone:'Europe/Madrid'});
        return '<option value="' + esc(item.id) + '">' + esc(date + ' · ' + item.count +
          (item.count === 1 ? ' respuesta' : ' respuestas') + (item.revision ? ' · corregida' : '')) + '</option>';
      }).join('');
      selector.value = s?.id || '';
      $('#guide-history-account-label').hidden = items.length < 2;
      $('#guide-history-empty').hidden = Boolean(n);
      const canEdit = aiReady && ['draft','checkpoint','ready','applied'].includes(s?.state);
      $('#guide-history-list').innerHTML = (s?.history || []).map((h, index) => '<li><b>' + esc(h.question) + '</b><p>' +
        esc(h.answer) + '</p>' + (canEdit ? '<button type="button" data-edit-index="' + index + '" aria-label="Editar respuesta ' +
          (index + 1) + '">Editar</button>' : '') + '</li>').join('');
    }
    $('#guide-edit').hidden = !historyOpen || editIndex === null;
    $('#guide-analysis').textContent = s?.analysis_summary || (s ? 'Se conserva el contexto de tu empresa.' : 'El asistente usa estos datos y tu web para preparar las preguntas.');
    const discarded = s?.feedback?.discarded?.count || 0, approved = s?.feedback?.approved?.count || 0;
    $('#guide-feedback').hidden = !discarded && !approved;
    $('#guide-feedback').textContent = 'Tu tablero orienta las propuestas: ' + approved + ' aprobados y ' + discarded + ' descartados revisados.';
    $('#guide-source-details').hidden = !s;
    $('#guide-web-status').textContent = s?.web_status === 'consulted' ? 'Web oficial consultada.' : 'Sin evidencia web; se usó el onboarding.';
    $('#guide-sources').innerHTML = (s?.sources || []).map((x) => '<li><a href="' + esc(x.url) + '" target="_blank" rel="noopener noreferrer">' + esc(x.title) + '</a></li>').join('');
  }

  async function load() {
    const result = await api();
    if (session?.id !== result.session?.id) editIndex = null;
    session = result.session; aiReady = result.ai_ready;
    saved = new Map((result.saved || []).map((item) => [item.id, item]));
    intakeLoaded = true;
    $('#guide-environment').textContent = result.mode === 'local_demo' ? 'Prueba local · Respuestas simuladas' : 'Guardar tu enfoque no inicia una búsqueda.';
    $('#guide-company').textContent = result.profile.productora;
    showIntake(result.intake || []);
  }
  function newSession() {
    return work(async () => {
      startId ||= crypto.randomUUID();
      session = await api('/sessions', 'POST', {request_id:startId, fresh:resetOpen});
      startId = null; answerRequest = null; $('#guide-confirm').checked = false;
      historyOpen = resetOpen = false; editIndex = null;
    }, 'Analizando tu empresa y preparando la primera pregunta…');
  }
  function decision(choice) {
    return work(async () => {
      session = await api('/sessions/' + session.id + '/checkpoint', 'POST', {version:session.version, choice});
      $('#guide-confirm').checked = false;
    }, choice === 'review' ? 'Preparando el resumen de tu búsqueda…' : 'Abriendo la siguiente pregunta…');
  }
  $('#guide-start').onclick = $('#guide-reset-confirm').onclick = newSession;
  $('#guide-new').onclick = () => {
    resetOpen = true; render(); reveal('#guide-reset-title');
  };
  $('#guide-reset-cancel').onclick = () => {
    resetOpen = false; render(); revealStep();
    if ($('#guide-answer').hidden && $('#guide-checkpoint').hidden && $('#guide-review').hidden) $('#guide-new').focus();
  };
  $('#guide-history-open').onclick = () => {
    historyOpen = true; render(); reveal('#guide-history-label');
  };
  $('#guide-history-close').onclick = () => {
    historyOpen = false; editIndex = null; render(); revealStep();
    if ($('#guide-answer').hidden && $('#guide-checkpoint').hidden && $('#guide-review').hidden) $('#guide-history-open').focus();
  };
  $('#guide-history-account').onchange = (event) => {
    const id = event.target.value;
    work(async () => {
      session = await api('/sessions/' + id);
      editIndex = null; answerRequest = null; $('#guide-confirm').checked = false;
    }, 'Abriendo tus respuestas guardadas…');
  };
  $('#guide-history-list').onclick = (event) => {
    const button = event.target.closest('[data-edit-index]');
    if (!button) return;
    editIndex = Number(button.dataset.editIndex);
    const response = session.history[editIndex];
    $('#guide-edit-question').textContent = '· ' + response.question;
    $('#guide-edit-answer').value = response.answer;
    render(); reveal('#guide-edit-answer', 'center');
  };
  $('#guide-edit-cancel').onclick = () => {
    const index = editIndex;
    editIndex = null; render(); reveal('[data-edit-index="' + index + '"]', 'nearest');
  };
  $('#guide-edit').onsubmit = (event) => {
    event.preventDefault();
    const answer = $('#guide-edit-answer').value.trim(), index = editIndex;
    if (index === null || !answer) return;
    work(async () => {
      const {id, version} = session;
      const key = JSON.stringify([id, version, index, answer]);
      if (revisionRequest?.key !== key) revisionRequest = {key, id:crypto.randomUUID()};
      session = await api('/sessions/' + id + '/revisions', 'POST', {
        request_id:revisionRequest.id, version, index, answer,
      });
      revisionRequest = null; answerRequest = null; historyOpen = false; editIndex = null;
      $('#guide-confirm').checked = false;
    }, 'Guardando tu corrección y actualizando el enfoque…');
  };
  $('#guide-refresh').onclick = () => work(async () => { stateChecks = 0; await load(); });
  $('#guide-review-choice').onclick = () => decision('review');
  $('#guide-more').onclick = $('#guide-more-review').onclick = () => decision('more');
  $('#guide-confirm').onchange = render;
  $('#guide-options').onchange = (event) => {
    const custom = event.target.value === 'other';
    $('#guide-custom').hidden = !custom; $('#answer').required = custom;
    if (custom) $('#answer').focus();
  };
  $('#guide-answer').onsubmit = (event) => {
    event.preventDefault();
    const choice = new FormData(event.target).get('choice');
    if (choice === null) return;
    const mode = ['unknown','other'].includes(choice) ? 'custom' : 'option';
    const answer = choice === 'other' ? $('#answer').value.trim() : choice === 'unknown' ? 'No lo sé todavía' : session.turn.options[Number(choice)];
    if (!answer) return;
    work(async () => {
      const version = session.version;
      const key = JSON.stringify([session.id, version, mode, answer]);
      if (answerRequest?.key !== key) answerRequest = {key, id:crypto.randomUUID()};
      session = await api('/sessions/' + session.id + '/answers', 'POST', {request_id:answerRequest.id, version, answer, mode});
      answerRequest = null; $('#guide-confirm').checked = false;
    }, 'Procesando tu respuesta y preparando el siguiente paso…');
  };
  $('#guide-apply').onclick = () => work(async () => {
    if (!$('#guide-confirm').checked) return;
    session = await api('/sessions/' + session.id + '/apply', 'POST', {version:session.version, confirmed:true});
  }, 'Guardando tu búsqueda…');
  work(async () => {
    const response = await fetch('/api/intelligence/accounts', {credentials:'same-origin'}), data = await response.json();
    if (!response.ok || !data.accounts?.length) throw Error('No hay una empresa autorizada para esta sesión.');
    $('#guide-account').innerHTML = data.accounts.map((x) => '<option value="' + esc(x.id) + '">' + esc(x.name) + '</option>').join('');
    $('#guide-account-label').hidden = data.accounts.length < 2;
    const source = new URLSearchParams(location.search).get('source');
    const account = source ? data.accounts.find((item) => item.source_id === source) : data.accounts[0];
    if (!account) throw Error('Esta productora no tiene la guía disponible. Vuelve a Prospección para elegir una cuenta.');
    tenant = account.id; $('#guide-account').value = tenant;
    $('#guide-account').onchange = () => work(async () => {
      tenant = $('#guide-account').value; session = null; startId = answerRequest = null; aiReady = intakeLoaded = false;
      historyOpen = resetOpen = false; editIndex = null; saved.clear();
      $('#guide-company').textContent = 'Tu empresa'; showIntake([]);
      $('#guide-confirm').checked = false; await load();
    });
    await load();
  });
})();
