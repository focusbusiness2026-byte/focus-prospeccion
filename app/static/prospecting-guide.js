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
  let profileHash = '', legacyProfileHash = '', intakeFields = new Map();
  let reanalysisRequest = null, shownCheckpoint = '';
  let researchAvailable = false;
  let job = null, jobTimer = null;
  let timing = null;
  let jobFailures = 0, jobDisconnected = false;
  let liveLeadIds = new Set();
  let saved = new Map();
  const isWaiting = () => ['generating', 'applying'].includes(session?.state);
  const dialog = $('#guide-busy');
  dialog.addEventListener('cancel', (event) => event.preventDefault());

  function notifyCrm() {
    if (liveLeadIds.size) document.dispatchEvent(new CustomEvent('portal:guide-leads', {
      detail: {ids:[...liveLeadIds]},
    }));
  }

  $('#guide-view-leads').addEventListener('click', (event) => {
    event.preventDefault();
    showView('results', true);
    $('#crm-board-heading').focus({preventScroll:true});
  });

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

  async function requestJSON(path, method = 'GET', data) {
    const csrf = decodeURIComponent(document.cookie.split('; ').find((x) => x.startsWith('focus_csrf='))?.split('=').slice(1).join('=') || '');
    const response = await fetch(path, {
      method, credentials:'same-origin', headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},
      body: data === undefined ? undefined : JSON.stringify(data),
      signal: method === 'GET' ? AbortSignal.timeout(20000) : undefined,
    });
    let body;
    try { body = await response.json(); } catch { throw Error('No se confirmó la operación. Consulta el estado guardado.'); }
    if (!response.ok) throw Error(typeof body.detail === 'string' ? body.detail : 'Revisa la solicitud.');
    return body;
  }

  function api(path = '', method = 'GET', data) {
    return requestJSON('/api/intelligence/' + encodeURIComponent(tenant) + '/guide' + path, method, data);
  }

  async function work(action, title = 'Cargando tu empresa…', returnToHeader = false) {
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
      else if (scrollOnChange && before !== stage() && !dialog.open) {
        if (returnToHeader) {
          if (!$('#guide-answer').hidden) $('#guide-question').focus({preventScroll:true});
          $('#guide-header').scrollIntoView({block:'start', behavior:'instant'});
        } else revealStep();
      }
      applyPortalScope();
    }
  }

  function followSavedState() {
    if (!isWaiting()) { stateChecks = 0; return; }
    if (stateChecks >= 16) return;
    stateTimer = setTimeout(() => work(async () => {
      stateChecks += 1;
      try { session = await api('/sessions/' + session.id); }
      catch (error) { stateChecks = 16; throw error; }
    }, session.state === 'applying' ? 'Comprobando si se guardó tu búsqueda…' : 'Comprobando si tu pregunta está lista…',
    session.state === 'generating'), 6000);
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
    const note = s?.state === 'applied' ? 'Enfoque guardado. Ya puedes iniciar o programar el raspado.' :
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
    const opened = new Set([...$('#guide-intake').querySelectorAll('details[open]')].map((group) => group.dataset.group));
    intakeFields = new Map(intake.flatMap((group) => group.fields.map((field) => [field.label, field])));
    $('#guide-intake').innerHTML = intake.map((section) =>
      '<details data-group="' + esc(section.title) + '"' + (opened.has(section.title) ? ' open' : '') +
      '><summary>' + esc(section.title) + '</summary><dl>' + section.fields.map(({label, value, editable}) =>
        '<div class="guide-field"><dt><span>' + esc(label) + '</span>' + (editable ?
          '<button type="button" data-intake-field="' + esc(label) + '" aria-label="Editar ' + esc(label) + '">Editar</button>' : '') +
          '</dt><dd class="' + (value ? '' : 'muted') + '">' + esc(value || 'Sin responder') + '</dd></div>'
      ).join('') + '</dl></details>').join('');
  }

  function updateIntakeView(result) {
    profileHash = result.profile_hash;
    legacyProfileHash = result.legacy_profile_hash;
    $('#guide-company').textContent = result.profile.productora;
    showIntake(result.intake || []);
  }

  function currentProfile(s) {
    if (!s) return true;
    const expected = Object.hasOwn(s.profile, 'business_context') ? profileHash : legacyProfileHash;
    return s.profile_hash === expected;
  }

  function jobActive() {
    return ['queued', 'running'].includes(job?.status);
  }

  function acceptJob(updated, animate = true) {
    const changedJob = job?.job_id !== updated?.job_id;
    if (changedJob) {
      liveLeadIds = new Set();
      $('#guide-busy-leads').replaceChildren();
    }
    job = updated;
    if (updated?.timing) timing = updated.timing;
    jobFailures = 0;
    jobDisconnected = false;
    if (animate) {
      for (const lead of job?.saved_leads || []) liveLeadIds.add(lead.execution_id);
    }
    notifyCrm();
    const result = $('#guide-result');
    result.hidden = job?.status !== 'completed' || !job.leads_found;
    $('#guide-result-leads').replaceChildren(...(result.hidden ? [] : (job.highlights || []).map(lead => {
      const item = document.createElement('li'), name = document.createElement('strong');
      name.textContent = lead.company;
      item.append(name);
      if (lead.facts.length) {
        const facts = document.createElement('p');
        facts.className = 'muted';
        facts.textContent = lead.facts.join(' · ');
        item.append(facts);
      }
      if (lead.finding) {
        const finding = document.createElement('p');
        finding.textContent = lead.finding;
        item.append(finding);
      }
      const potential = document.createElement('p');
      potential.textContent = 'Potencial: ' + lead.potential;
      item.append(potential);
      return item;
    })));
  }

  function duration(seconds) {
    const mins = Math.floor(seconds / 60);
    return mins ? mins + ' min ' + (seconds % 60) + ' s' : seconds + ' s';
  }

  function renderElapsed() {
    if (jobActive() && !jobDisconnected) {
      const seconds = Math.max(0, Math.floor((Date.now() - Date.parse(job.created_at)) / 1000)) || 0;
      const phaseSeconds = Math.max(0, Math.floor((Date.now() - Date.parse(job.updated_at)) / 1000));
      liveText('#guide-busy-elapsed', duration(seconds) + ' en curso' +
        (phaseSeconds >= 20 && !jobFailures ? ' · ' + duration(phaseSeconds) + ' en este paso' : ''));
      if (!timing) {
        liveText('#guide-busy-estimate', 'El tiempo estimado se calculará con las primeras ejecuciones.');
      } else {
        const remaining = timing.average_seconds - seconds;
        liveText('#guide-busy-estimate', remaining > 0 ? 'Tiempo restante orientativo: ' + duration(remaining) :
          'Esta búsqueda está tardando más que la referencia. El servidor sigue trabajando.');
      }
    }
  }

  function renderJobDialog() {
    $('#guide-busy-progress').value = job.progress || 0;
    const activity = $('#guide-busy-activity');
    const latest = (job.activities || []).slice(-4);
    activity.hidden = !latest.length;
    if ([...activity.children].map((item) => item.textContent).join('\n') !== latest.join('\n')) {
      activity.replaceChildren(...latest.map((message) => {
        const item = document.createElement('li');
        item.textContent = message;
        return item;
      }));
    }
    const list = $('#guide-busy-leads');
    for (const lead of job.saved_leads || []) {
      if ([...list.children].some((item) => item.dataset.lead === lead.execution_id)) continue;
      const item = document.createElement('li'), name = document.createElement('strong'), badge = document.createElement('span');
      item.dataset.lead = lead.execution_id;
      name.textContent = lead.company;
      badge.textContent = 'Nuevo';
      item.append(name, badge);
      list.append(item);
    }
  }

  function renderExecution(reviewed) {
    $('#guide-execution').hidden = !reviewed && !job && !jobDisconnected;
    $('#guide-run-actions').hidden = !reviewed;
    $('#guide-launch').disabled = busy || !researchAvailable || jobActive() || jobDisconnected;
    $('#guide-program').disabled = busy;
    $('#guide-job-status').hidden = !job && !jobDisconnected;
    $('#guide-job-progress').hidden = !jobActive();
    $('#guide-job-progress').value = job?.progress || 0;
    const reference = timing?.sample_count === 1 ? 'Referencia de 1 raspado' : 'Promedio de ' + timing?.sample_count + ' raspados';
    liveText('#guide-research-average', timing ? reference + ': ' + duration(timing.average_seconds) : 'Tiempo promedio pendiente de medir.');
    liveText('#guide-job-status', jobDisconnected ?
      'Se perdió la conexión. Consulta el estado del raspado antes de iniciar otro.' :
      job?.status === 'completed' ? (job.leads_found ? 'Raspado completado. ' + job.leads_found +
        (job.leads_found === 1 ? ' lead guardado.' : ' leads guardados.') : 'Raspado completado.') : job?.message || '');
    $('#guide-reconnect-job').hidden = !jobDisconnected;
    $('#guide-reconnect-job').disabled = busy;
    renderElapsed();
  }

  function pollJob() {
    clearTimeout(jobTimer);
    if (!jobActive() || jobDisconnected) return;
    const jobId = job.job_id;
    const account = tenant;
    jobTimer = setTimeout(async () => {
      try {
        const updated = await api('/job');
        if (tenant !== account || job?.job_id !== jobId) return;
        if (!updated) throw Error('El servidor no confirmó el estado.');
        acceptJob(updated);
      } catch {
        if (tenant !== account || job?.job_id !== jobId) return;
        jobFailures += 1;
        jobDisconnected = jobFailures >= 5;
      }
      render();
      if (!$('#guide-result').hidden && job?.status === 'completed') reveal('#guide-result-title');
      pollJob();
    }, jobFailures ? Math.min(3000 * (2 ** jobFailures), 18000) : 3000);
  }

  function render() {
    const s = session, n = s?.history.length || 0, limit = s?.round_limit || 10;
    const legacy = Boolean(s && !s.round_limit), isApplied = s?.state === 'applied';
    const showFlow = !historyOpen && !resetOpen;
    const matches = currentProfile(s);
    const canAsk = showFlow && s?.state === 'draft' && !legacy && Boolean(s.turn);
    const waiting = isWaiting();
    const ready = s?.state === 'ready', checkpoint = s?.state === 'checkpoint';
    const canExtend = !legacy && limit < 20;
    const criteriaComplete = ['sectors','target_countries'].every((key) => s?.criteria[key]?.some((value) => value.trim()));
    $('#guide-reanalyse').hidden = !s || !aiReady;
    $('#guide-reanalyse').disabled = busy || waiting || s?.version < 1;
    $('#guide-account').disabled = busy;
    $('#guide-start').hidden = Boolean(s) || !intakeLoaded || !showFlow; $('#guide-start').disabled = busy || !aiReady;
    $('#guide-new').hidden = !s || !showFlow;
    $('#guide-new').disabled = busy || waiting || !aiReady;
    $('#guide-history-open').hidden = !showFlow || ![...saved.values()].some((item) => item.count);
    $('#guide-refresh').hidden = !tenant || (!waiting && s?.state !== 'review_required' && $('#guide-error').hidden);
    $('#guide-refresh').disabled = busy;
    const note = learning(s, n, limit);
    const trackJob = jobActive() && !jobDisconnected;
    const showWait = busy || (waiting && stateChecks < 16) || trackJob;
    const waitTitle = trackJob ? 'Raspado en curso' : operation?.title ||
      (s?.state === 'applying' ? 'Guardando tu búsqueda…' : 'Preparando la siguiente pregunta…');
    liveText('#guide-busy-title', showWait ? waitTitle : '');
    liveText('#guide-busy-detail', trackJob ?
      (jobFailures ? 'Estamos recuperando la conexión para confirmar el avance…' : job.message) :
      (showWait ? (operation?.long ? 'La consulta sigue en curso. ' : '') +
        (intakeLoaded ? note : 'Estamos leyendo tu ficha de onboarding.') : ''));
    $('#guide-busy-job').hidden = !trackJob;
    if (trackJob) renderJobDialog();
    if (showWait && !dialog.open) {
      dialog.showModal();
      document.documentElement.classList.add('guide-locked');
    } else if (!showWait && dialog.open) {
      document.documentElement.classList.remove('guide-locked');
      dialog.close();
    }
    liveText('#guide-status', !showFlow ? '' : waiting && !showWait ? 'Aún no se confirmó el resultado. Usa «Consultar estado» para comprobarlo.' : statuses[s?.state] ||
      (!s ? (intakeLoaded && !aiReady ? 'La administración debe habilitar el asistente para esta cuenta.' : '') :
        !matches ? 'La ficha cambió. Vuelve a analizar el perfil para actualizar la propuesta.' :
        legacy && s.state === 'draft' ? 'Tu conversación anterior se conserva.' : canAsk ? 'Pregunta ' + (n + 1) + ' de ' + limit : ''));
    $('#guide-answer').hidden = !canAsk;
    $('#guide-question-fieldset').disabled = busy;
    $('#guide-next').disabled = busy || !matches;
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
    if (checkpoint && shownCheckpoint !== stage()) {
      shownCheckpoint = stage();
      $('#guide-additional').value = s.additional_context || '';
    }
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
    $('#guide-apply').hidden = !ready || !criteriaComplete; $('#guide-apply').disabled = busy || !matches || !$('#guide-confirm').checked;
    $('#guide-more-review').hidden = !ready || !canExtend; $('#guide-more-review').disabled = busy;
    renderExecution(showFlow && (ready || isApplied) && criteriaComplete && matches);
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
    $('#guide-analysis').textContent =
      s?.analysis_summary || (s ? 'Se conserva el contexto de tu empresa.' : 'El asistente usa estos datos y tu web para preparar las preguntas.');
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
    researchAvailable = result.research_available;
    timing = result.timing;
    acceptJob(result.job, false);
    pollJob();
    saved = new Map((result.saved || []).map((item) => [item.id, item]));
    intakeLoaded = true;
    $('#guide-environment').textContent = 'Guardar tu enfoque no inicia una búsqueda.';
    updateIntakeView(result);
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
    const note = session.state === 'checkpoint' ? $('#guide-additional').value.trim() : null;
    if (!currentProfile(session) || (note !== null && note !== (session.additional_context || ''))) {
      return reanalyse(note, choice);
    }
    return work(async () => {
      session = await api('/sessions/' + session.id + '/checkpoint', 'POST', {version:session.version, choice});
      $('#guide-confirm').checked = false;
    }, choice === 'review' ? 'Preparando el resumen de tu búsqueda…' : 'Abriendo la siguiente pregunta…');
  }
  function reanalyse(note = null, choice = null) {
    return work(async () => {
      const {id, version} = session;
      const key = JSON.stringify([id, version, profileHash, note, choice]);
      if (reanalysisRequest?.key !== key) reanalysisRequest = {key, id:crypto.randomUUID()};
      session = await api('/sessions/' + id + '/reanalysis', 'POST', {
        request_id:reanalysisRequest.id, version, additional_context:note, choice,
      });
      reanalysisRequest = null; answerRequest = null;
      historyOpen = resetOpen = false; editIndex = null;
      $('#guide-intake-status').hidden = true;
      $('#guide-confirm').checked = false;
    }, 'Reanalizando el perfil y todas tus respuestas…', true);
  }
  $('#guide-reanalyse').onclick = () => reanalyse(session.state === 'checkpoint' ? $('#guide-additional').value.trim() : null);
  $('#guide-intake').onclick = (event) => {
    const button = event.target.closest('[data-intake-field]');
    if (!button || busy) return;
    const field = intakeFields.get(button.dataset.intakeField);
    $('#guide-intake form')?.remove();
    const form = document.createElement('form');
    form.innerHTML = '<label>Editar ' + esc(field.label) + '<textarea maxlength="4000" rows="2"></textarea></label>' +
      '<div class="actions"><button class="primary" type="submit">Guardar</button><button type="button">Cancelar</button></div>';
    const input = form.querySelector('textarea');
    input.value = field.value;
    form.querySelector('[type="button"]').onclick = () => {form.remove(); button.focus();};
    form.onsubmit = (submitEvent) => {
      submitEvent.preventDefault();
      if (input.value.trim() === field.value) { form.remove(); button.focus(); return; }
      work(async () => {
        const result = await api('/intake', 'PATCH', {field:field.label, before:field.value, value:input.value});
        updateIntakeView(result);
        liveText('#guide-intake-status', 'Campo guardado.' + (session ? ' Puedes volver a analizar el perfil.' : ''));
        $('#guide-intake-status').hidden = false;
      }, 'Guardando el campo de onboarding…');
    };
    button.closest('.guide-field').append(form);
    input.focus();
  };
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
    }, 'Procesando tu respuesta y preparando el siguiente paso…', true);
  };
  $('#guide-apply').onclick = () => work(async () => {
    if (!$('#guide-confirm').checked) return;
    session = await api('/sessions/' + session.id + '/apply', 'POST', {version:session.version, confirmed:true});
    document.dispatchEvent(new Event('portal:guide-search-saved'));
  }, 'Guardando tu búsqueda…');
  $('#guide-launch').onclick = () => work(async () => {
    try {
      acceptJob(await api('/sessions/' + session.id + '/launch', 'POST', {version:session.version}));
    } catch (error) {
      let confirmed;
      try { confirmed = await api('/job'); }
      catch { jobDisconnected = true; }
      if (confirmed?.job_id && confirmed.job_id !== job?.job_id) acceptJob(confirmed);
      else throw error;
    }
    pollJob();
  }, 'Iniciando el raspado…');
  $('#guide-reconnect-job').onclick = () => work(async () => {
    acceptJob(await api('/job'));
    pollJob();
  }, 'Consultando el raspado…');
  $('#guide-program').onclick = () => {
    const settings = $('#legacy-prospecting');
    if (tenant) document.dispatchEvent(new CustomEvent('portal:guide-account', {detail:tenant}));
    settings.open = true;
    settings.scrollIntoView({block:'start'});
    settings.querySelector('summary').focus({preventScroll:true});
  };
  setInterval(renderElapsed, 1000);
  const accountSelect = $('#guide-account');
  let accounts = [];

  async function selectAccount(account) {
    tenant = account?.id || '';
    accountSelect.value = tenant;
    session = null; startId = answerRequest = null; aiReady = intakeLoaded = false;
    historyOpen = resetOpen = false; editIndex = null; saved.clear();
    clearTimeout(jobTimer); job = timing = null;
    liveLeadIds.clear(); jobFailures = 0; jobDisconnected = false;
    acceptJob(null, false);
    $('#guide-busy-leads').replaceChildren();
    $('#guide-intake-status').hidden = true;
    $('#guide-company').textContent = 'Tu empresa'; showIntake([]);
    $('#guide-confirm').checked = false;
    if (tenant) await load();
  }

  async function loadAccounts({sources, selectedSourceId} = {}) {
    accounts = sources.map(source => ({
      id:source.id, source_id:source.id, name:source.name,
    }));
    accountSelect.innerHTML = accounts.map((x) => '<option value="' + esc(x.id) + '">' + esc(x.name) + '</option>').join('');
    $('#guide-account-label').hidden = accounts.length < 2;
    const requested = accounts.find(item => item.id === selectedSourceId);
    const account = requested || accounts.find(item => item.id === tenant) || accounts[0];
    if (!account) {
      await selectAccount(null);
      throw Error('La guía no está disponible para esta empresa.');
    }
    if (account.id !== tenant || !intakeLoaded) await selectAccount(account);
    else accountSelect.value = tenant;
  }

  accountSelect.onchange = () => {
    const account = accounts.find(item => item.id === accountSelect.value);
    if (account) work(async () => {
      await selectAccount(account);
      document.dispatchEvent(new CustomEvent('portal:guide-account', {detail:account.id}));
    });
  };
  let scopeKey, pendingScope;
  function applyPortalScope() {
    if (busy || !pendingScope) return;
    const detail = pendingScope;
    pendingScope = undefined;
    const key = JSON.stringify(detail);
    if (key === scopeKey) return;
    scopeKey = key;
    work(async () => {
      try { await loadAccounts(detail); }
      catch (error) { scopeKey = undefined; throw error; }
    });
  }
  document.addEventListener('portal:prospecting', ({detail}) => {
    pendingScope = detail;
    applyPortalScope();
  });
  notifyProspectingGuide();
})();
