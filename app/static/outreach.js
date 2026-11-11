'use strict';
(() => {
const el=s=>document.querySelector(s),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let tenant='',catalog,current,ghlConnection,isAdmin=false,busy=false,selected=new Map(),reviewRecipient='',libraryEdit=null;
const states={draft:'Borrador',active:'Activa',paused:'Pausada',pause_pending:'Pausa pendiente de confirmación',activation_uncertain:'Revisión de GHL pendiente',activating:'Incorporando contactos',resuming:'Reanudando',archived:'Archivada',completed:'Completada'};
async function api(path='',method='GET',data){const token=document.cookie.split('; ').find(x=>x.startsWith('focus_csrf='))?.split('=')[1]||'';const response=await fetch('/api/intelligence/'+encodeURIComponent(tenant)+'/outreach'+path,{method,credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':decodeURIComponent(token)},body:data===undefined?undefined:JSON.stringify(data)});let body;try{body=await response.json();}catch{throw Error('El servidor no confirmó la operación. Revisa el estado antes de repetir.');}if (!response.ok) {
  if (typeof body.detail === 'string') throw Error(body.detail);
  const errors = Array.isArray(body.detail) ? body.detail : [];
  if (errors.some(error => error.loc?.includes('steps'))) throw Error('Elige una secuencia de cinco correos.');
  if (errors.some(error => error.loc?.includes('recipients'))) throw Error('Revisa los destinatarios de la campaña.');
  throw Error('No se pudo completar la solicitud. Vuelve a abrir la campaña.');
}return body;}
async function work(fn){if(busy)return;busy=true;el('#error').hidden=true;const disabled=new Map([...document.querySelectorAll('button')].map(b=>[b,b.disabled]));disabled.forEach((_,b)=>b.disabled=true);try{await fn();}catch(e){el('#error').hidden=false;el('#error').textContent=e.message;el('#error').scrollIntoView({block:'nearest'});}finally{busy=false;disabled.forEach((value,b)=>{if(b.isConnected)b.disabled=value;});}}
function channel(){return [el('#channel').value];}
function setChannel(value, reconcile=false){
  const next = value === 'social' ? 'social' : 'email';
  const changed = el('#channel').value !== next;
  el('#channel').value = next;
  if (reconcile && changed) {
    for (const contact of catalog.contacts) {
      if (!selected.has(contact.id)) continue;
      if (eligible(contact)) selected.set(contact.id, defaultEmail(contact));
      else selected.delete(contact.id);
    }
    clearSelectionNotice();
  }
  const social=channel()[0]==='social';
  document.querySelectorAll('[data-channel]').forEach(button=>{
    const active=button.dataset.channel===channel()[0];
    button.setAttribute('aria-pressed',String(active));button.classList.toggle('primary',active);
  });
  el('#prepare-selected').textContent=social?'Preparar mensajes personalizados':'Preparar 5 correos';
  el('#channel-help').textContent=social?'Prepara textos para los perfiles públicos de LinkedIn, Instagram y Facebook que tenga el lead. Tú decides cuándo enviarlos.':'Se crean cinco correos por destinatario para revisar antes de comenzar la campaña en GoHighLevel.';
}
function eligible(c){return channel().every(k=>c[k+'_ready']);}
function filtered(){const q=el('#recipient-filter').value.toLowerCase();return catalog.contacts.filter(c=>c.approved&&[c.company,c.sector,c.city].join(' ').toLowerCase().includes(q));}
const networkNames = {linkedin: 'LinkedIn', instagram: 'Instagram', facebook: 'Facebook'};
function defaultEmail(contact) {
  return channel()[0] === 'email' && contact.email_options.length === 1 ? contact.email_options[0].email : '';
}
function exclusion(contact) {
  const isEmail = channel()[0] === 'email';
  const reasons = contact.reasons.filter(reason => !reason.startsWith('Sin perfil personal') && (isEmail || reason !== 'Sin email público disponible'));
  return reasons.join(' · ') || 'Sin perfiles públicos de LinkedIn, Instagram o Facebook';
}
function renderRecipients() {
  const isEmail = channel()[0] === 'email';
  const contacts = filtered();
  el('#recipient-list').innerHTML = contacts.map(contact => {
    const id = esc(contact.id), checked = selected.has(contact.id);
    const email = selected.get(contact.id) || '';
    const emails = contact.email_options;
    const emailChoice = emails.length === 1
      ? '<small>' + esc(emails[0].email) + '</small>'
      : `<label>Correo destinatario<select data-email="${id}" required ${checked?'':'disabled'}>
        <option value="">Elige un correo público…</option>
        ${emails.map(option=>`<option value="${esc(option.email)}" ${email===option.email?'selected':''}>${esc(option.email)}</option>`).join('')}
      </select></label>`;
    const info = !eligible(contact) ? '<small>' + esc(exclusion(contact)) + '</small>'
      : isEmail ? emailChoice : '<small>' + esc(Object.keys(contact.social_profiles||{}).map(network=>networkNames[network]).join(' · ')) + '</small>';
    return `<article class="recipient-option ${eligible(contact)?'':'disabled'}">
      <input type="checkbox" aria-label="Seleccionar ${esc(contact.company)}" data-select="${id}" ${checked?'checked':''} ${eligible(contact)?'':'disabled'}>
      <div><h3>${esc(contact.company)}</h3>${info}
        ${isEmail && eligible(contact) ? `<button class="recipient-prepare" type="button" data-prepare-email="${id}">Preparar email</button>` : ''}
      </div></article>`;
  }).join('') || '<p class="muted">No hay aprobados con estos filtros.</p>';
  el('#selection-count').textContent=selected.size+' seleccionados';
  const available = contacts.filter(eligible);
  const selectedCount = available.filter(contact => selected.has(contact.id)).length;
  const selectAll = el('#select-visible');
  selectAll.checked = available.length > 0 && selectedCount === available.length;
  selectAll.indeterminate = selectedCount > 0 && selectedCount < available.length;
  el('#recipient-list').querySelectorAll('[data-select]').forEach(input => input.onchange = () => {
    const contact = catalog.contacts.find(item => item.id === input.dataset.select);
    if (input.checked) selected.set(contact.id, defaultEmail(contact));
    else selected.delete(contact.id);
    renderRecipients();
  });
  el('#recipient-list').querySelectorAll('[data-email]').forEach(input => input.onchange = () => {
    if (selected.has(input.dataset.email)) selected.set(input.dataset.email, input.value);
  });
  el('#recipient-list').querySelectorAll('[data-prepare-email]').forEach(button => button.onclick = () => work(async () => {
    const contact = catalog.contacts.find(item => item.id === button.dataset.prepareEmail);
    const email = selected.get(contact.id) || defaultEmail(contact);
    if (!email) {
      selected.set(contact.id, '');
      renderRecipients();
      el('#recipient-list select:enabled:invalid')?.focus();
      throw Error('Elige uno de los correos públicos de esta tarjeta.');
    }
    await createAndPrepare([{lead_id: contact.id, email}]);
  }));
}
function clearSelectionNotice() {
  el('#selection-notice').hidden = true;
  el('#selection-notice').replaceChildren();
}
async function createAndPrepare(recipients = [...selected].map(([lead_id,email]) => ({lead_id,email}))) {
  if (!catalog.generation_ready) throw Error('La generación con IA está pendiente de habilitación por el administrador.');
  if (!recipients.length) throw Error('Selecciona al menos un destinatario aprobado.');
  if (recipients.length > 100) throw Error('Selecciona hasta 100 leads por campaña.');
  if (channel()[0] === 'email' && recipients.some(row => !row.email)) {
    el('#recipient-list select:enabled:invalid')?.focus();
    throw Error('Elige un correo público para cada destinatario seleccionado.');
  }
  const c = await api('/campaigns','POST',{channels:channel(),steps:5,recipients});
  await open(c.id);
  await prepareDraft(c.id);
}
async function prepareCrmSelection(params) {
  if (params.get('from') !== 'crm') return;
  const raw = sessionStorage.getItem('focus-campaign-selection');
  sessionStorage.removeItem('focus-campaign-selection');
  params.delete('from');
  history.replaceState(null,'','?' + params);
  let handoff;
  try { handoff = JSON.parse(raw || 'null'); } catch { /* Offer manual selection. */ }
  if (!Array.isArray(handoff?.ids) || !handoff.ids.length || handoff.account !== tenant ||
      handoff.account !== params.get('account') || handoff.channel !== channel()[0]) {
    throw Error('No se recuperó la selección del tablero. Vuelve al CRM o selecciona los destinatarios aquí.');
  }
  const byId = new Map(catalog.contacts.map(contact => [contact.id, contact]));
  const excluded = [];
  for (const id of new Set(handoff.ids)) {
    const contact = byId.get(id);
    if (contact && eligible(contact)) selected.set(id, defaultEmail(contact));
    else excluded.push(contact ? contact.company + ' · ' + exclusion(contact) : 'Lead ya no disponible');
  }
  renderRecipients();
  const requiresChoice = channel()[0] === 'email' && [...selected.values()].some(email => !email);
  if (excluded.length || requiresChoice) {
    const notice = el('#selection-notice');
    notice.hidden = false;
    notice.textContent = !selected.size ? 'Ningún lead seleccionado está disponible para este canal.'
      : requiresChoice ? 'Elige un correo público por destinatario y continúa.'
      : 'Revisa los leads excluidos y confirma la preparación de los demás.';
    if (excluded.length) {
      const list = document.createElement('ul');
      for (const reason of excluded) {
        const item = document.createElement('li');
        item.textContent = reason;
        list.append(item);
      }
      notice.append(list);
    }
    notice.scrollIntoView({block:'center'});
    return;
  }
  await createAndPrepare();
}
function show(view){['create','detail','library'].forEach(v=>el('#'+v+'-view').hidden=v!==view);}
async function load(){catalog=await api();ghlConnection=catalog.mode!=='local_demo'?await api('/ghl/status').catch(()=>null):null;el('#ghl-setup-link').href='/integrations/ghl?account='+encodeURIComponent(tenant);el('#environment').textContent=catalog.mode==='local_demo'?'VISTA LOCAL · Datos de ejemplo y generación simulada · Cero envíos y cero llamadas de pago':'COPIA PRIVADA · '+(catalog.generation_ready?'IA habilitada para preparación.':'Preparación de IA pendiente del administrador.')+' '+(catalog.sending_ready?'Conexión GHL configurada; revisa antes de activar.':'Envíos GHL sin habilitar.');el('#campaigns-list').innerHTML=catalog.campaigns.map(c=>`<button class="campaign-item" data-open="${esc(c.id)}">${esc(c.name)}<small>${states[c.state]||esc(c.state)} · ${c.recipients.length} destinatarios</small></button>`).join('')||'<p class="muted">Todavía no hay nuevas campañas.</p>';el('#campaigns-list').querySelectorAll('[data-open]').forEach(b=>b.onclick=()=>work(()=>open(b.dataset.open)));for(const input of el('#brand-form').elements){if(!input.name)continue;const value=catalog.brand[input.name];if(input.type==='checkbox')input.checked=value===true;else input.value=input.name==='template_ids'?(value||[]).join('\n'):value|| (input.name==='color'?'#d9af3c':'');}el('#demo-brand').hidden=catalog.mode!=='local_demo';el('#ghl-connect').hidden=!ghlConnection?.can_connect;el('#ghl-connect').href='/api/intelligence/'+encodeURIComponent(tenant)+'/outreach/ghl/connect';el('#ghl-setup-status').textContent=ghlStatus();renderRecipients();}
function ghlStatus() {
  if (catalog.mode === 'local_demo') return 'Simulación local';
  if (!ghlConnection) return 'No se pudo comprobar la conexión';
  if (!ghlConnection.connected) return 'Subcuenta sin conectar';
  if (!ghlConnection.sending_enabled) return 'Subcuenta conectada · Envío pendiente de habilitación';
  return 'Subcuenta conectada';
}
async function open(id){current=await api('/campaigns/'+id);show('detail');renderDetail();history.replaceState(null,'','?campaign='+encodeURIComponent(id));}
function renderDetail(){const d=current,c=d.campaign,m=d.metrics,frozen=c.state!=='draft';if(c.channels.includes('social'))return renderSocial();const ready=c.approval?.digest===d.digest;const canStart=ready&&!d.issues.length&&(catalog.mode==='local_demo'||Boolean(ghlConnection?.connected&&ghlConnection.sending_enabled));el('#detail-view').innerHTML=`<p class="eyebrow">2 · TEXTOS Y DISEÑO</p><h2>${esc(c.name)}</h2><span class="badge">${states[c.state]||esc(c.state)} · versión ${c.version}</span><div id="review-area"></div><div class="metrics">${[['Destinatarios',m.recipients],['Correos preparados',m.prepared],['Aceptados por GHL',m.sent_confirmed],['Entregados',m.delivered],['Respuestas',m.responses],['Tareas disponibles',m.tasks_today]].map(([n,v])=>`<div class="metric"><strong>${v}</strong><span>${n}</span></div>`).join('')}</div><div class="actions">${!frozen?`<button class="primary" id="prepare">Preparar ${c.schedule.length} correos por destinatario</button>`:''}<button id="reload-campaign">Actualizar estado</button><button data-action="duplicate">Duplicar</button>${c.state==='active'?'<button data-action="pause">Pausar campaña</button>':''}${c.state==='paused'?'<button data-action="resume">Reanudar</button>':''}${['draft','paused','completed'].includes(c.state)?'<button data-action="archive">Archivar</button>':''}</div>${frozen?'<p class="locked-note">Contenido congelado. Duplica para crear una nueva versión.</p>':'<p class="muted">Se preparan borradores con el contexto disponible; revisa las afirmaciones. Esta acción no envía correos.</p>'}${d.issues.length?`<div class="review-issues"><b>Antes de activar</b><ul>${d.issues.map(x=>`<li>${esc(x)}</li>`).join('')}</ul></div>`:''}<p class="eyebrow">3 · CALENDARIO Y APROBACIÓN</p><h3>Un correo por día natural</h3><p class="muted">10:00 · Europe/Madrid · incluidos fines de semana. El proveedor detendrá los siguientes pasos si hay respuesta o baja.</p><ol class="date-list">${c.schedule.map((x,i)=>`<li><b>Paso ${i+1}</b><br>${esc(new Intl.DateTimeFormat('es-ES',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit',timeZone:'Europe/Madrid'}).format(new Date(x)))}</li>`).join('')}</ol>${!frozen?`<button id="reschedule">Actualizar fechas</button><div class="approval-box"><h3>Comenzar campaña de email</h3><p class="muted">${esc(ghlStatus())}${c.brand.sender_email?' · Remitente: '+esc(c.brand.sender_email):''}</p><label class="check"><input type="checkbox" id="approve-check"> He revisado los correos, el remitente, los destinatarios y las fechas</label><div class="actions"><button id="approve" ${ready?'disabled':''}>${ready?'Lote aprobado':'Aprobar lote'}</button><button class="primary" id="activate" ${!canStart?'disabled':''}>${catalog.mode==='local_demo'?'Activar simulación local':'Comenzar campaña de email'}</button>${catalog.mode!=='local_demo'?`<a class="button" href="/integrations/ghl?account=${encodeURIComponent(tenant)}">Ver conexión con GoHighLevel</a>`:''}</div><p class="muted">${catalog.mode==='local_demo'?'La simulación no conecta ni envía desde GHL.':'Después de aprobar, GoHighLevel usará este remitente y enviará un correo diario cuando la conexión y el flujo estén listos.'}</p></div>`:''}<h3>Estado por destinatario</h3><div class=recipient-status>${c.recipients.map(r=>`<p><b>${esc(r.contact.company)}</b> · ${esc(r.email||r.contact.linkedin)}<br><small>${esc(c.provider_states[r.key]?.status||(c.state==='draft'?'Preparado para revisión':c.delivery_mode==='simulated'?'Simulación local · cero envíos':'Pendiente del proveedor'))}${c.provider_states[r.key]?.next_step?' · siguiente paso '+esc(c.provider_states[r.key].next_step):''}</small></p>`).join('')}</div>${c.channels.includes('linkedin')?`<h3>Tareas de LinkedIn</h3><div id="linkedin-tasks">${d.tasks.filter(t=>t.state==='pending').map(t=>`<article class="task"><b>${esc(c.recipients.find(r=>r.key===t.recipient)?.contact.company)}</b><p>${({invitation:'Invitación manual',message:'Primer mensaje manual',followup:'Seguimiento manual'})[t.kind]}</p><p class="muted">${t.due>d.clock?'Disponible el '+new Date(t.due*1000).toLocaleString('es-ES'):'Disponible ahora'}</p><textarea readonly>${esc(t.payload.message)}</textarea><div class="actions"><button data-copy-task="${esc(t.id)}">Copiar mensaje</button><a class="button" href="${esc(t.payload.linkedin)}" rel="noopener noreferrer" target="_blank">Abrir perfil</a><button data-complete-task="${esc(t.id)}">Confirmar acción realizada</button></div></article>`).join('')||'<p class="muted">Sin tareas pendientes. Preparar un mensaje no confirma su envío.</p>'}</div><label>Destinatario LinkedIn<select id="manual-recipient">${c.recipients.map(r=>`<option value="${esc(r.key)}">${esc(r.contact.company)}</option>`).join('')}</select></label><div class="actions"><button data-manual="connection_accepted">Confirmar conexión aceptada</button><button data-manual="response">Registrar respuesta</button><button data-manual="opt_out">No contactar</button></div>`:''}<details><summary>Historial de la campaña</summary><div>${d.events.map(e=>`<p class="event">${esc(e.kind)} · ${esc(e.origin||'sistema')} · ${new Date(e.at*1000).toLocaleString('es-ES')}</p>`).join('')}</div></details>`;
el('#reload-campaign').onclick=()=>work(()=>open(c.id));el('#prepare')?.addEventListener('click',()=>work(async()=>{await prepareDraft(c.id);}));el('#reschedule')?.addEventListener('click',()=>work(async()=>{current=await api('/campaigns/'+c.id+'/reschedule','POST',{});renderDetail();}));el('#approve')?.addEventListener('click',()=>work(async()=>{if(!el('#approve-check').checked)throw Error('Confirma la revisión completa antes de aprobar.');current=await api('/campaigns/'+c.id+'/approve','POST',{digest:current.digest});renderDetail();}));el('#activate')?.addEventListener('click',()=>work(async()=>{if(!current.campaign.approval)throw Error('Aprueba primero el lote completo.');current=await api('/campaigns/'+c.id+'/actions','POST',{action:'activate'});await load();renderDetail();}));el('#detail-view').querySelectorAll('[data-action]').forEach(b=>b.onclick=()=>work(async()=>{const result=await api('/campaigns/'+c.id+'/actions','POST',{action:b.dataset.action});await load();await open(result.campaign?.id||result.id);}));el('#detail-view').querySelectorAll('[data-copy-task]').forEach(b=>b.onclick=()=>work(async()=>{await navigator.clipboard.writeText(d.tasks.find(t=>t.id===b.dataset.copyTask).payload.message);b.textContent='Copiado';}));el('#detail-view').querySelectorAll('[data-complete-task]').forEach(b=>b.onclick=()=>work(async()=>{const t=d.tasks.find(t=>t.id===b.dataset.completeTask);if(b.dataset.confirmed!=='yes'){b.dataset.confirmed='yes';b.textContent='Confirmar que ya la realicé';return;}current=await api('/campaigns/'+c.id+'/manual','POST',{recipient:t.recipient,event:{invitation:'invitation_sent',message:'message_sent',followup:'followup_sent'}[t.kind]});renderDetail();}));el('#detail-view').querySelectorAll('[data-manual]').forEach(b=>b.onclick=()=>work(async()=>{if(b.dataset.confirmed!=='yes'){b.dataset.confirmed='yes';b.textContent='Confirmar este hecho';return;}current=await api('/campaigns/'+c.id+'/manual','POST',{recipient:el('#manual-recipient').value,event:b.dataset.manual});renderDetail();}));renderReview();renderLinkedInReview();}
function emailPreview(email, brand, editor) {
  const color = /^#[0-9a-f]{6}$/i.test(brand.color || '') ? brand.color : '#d9af3c';
  const logo = /^(https:\/\/|\/static\/[a-z0-9])/i.test(brand.logo_url || '') ? brand.logo_url : '';
  return `<details class="email-preview-item" data-preview-step="${email.step}">
    <summary><span class="email-thumbnail" style="--preview-accent:${color}" aria-hidden="true">${logo ? `<img src="${esc(logo)}" alt="" loading="lazy">` : '✉'}</span><span><small>Correo ${email.step}</small><strong>${esc(email.subject)}</strong><small>${esc(email.preheader)}</small></span></summary>
    <div class="email-preview-content"><iframe title="Vista previa del correo ${email.step}" sandbox=""></iframe><p class="muted" data-preview-note role="status"></p>${editor}</div>
  </details>`;
}
function bindEmailPreviews(container, previewPath) {
  container.querySelectorAll('[data-preview-step]').forEach(item => {
    item.ontoggle = async () => {
      const iframe = item.querySelector('iframe');
      const note = item.querySelector('[data-preview-note]');
      if (!item.open || iframe.hasAttribute('srcdoc') || item.dataset.loading) return;
      item.dataset.loading = 'true';
      note.textContent = 'Cargando vista previa…';
      try {
        const preview = await api(previewPath(Number(item.dataset.previewStep)));
        iframe.srcdoc = preview.html;
        note.textContent = '';
      } catch (error) {
        note.textContent = error.message;
      } finally {
        delete item.dataset.loading;
      }
    };
  });
}
function emailEditor(email, action) {
  const labels = {subject:'Asunto', preheader:'Preencabezado', objective:'Objetivo', body:'Texto'};
  return `<details><summary>Editar correo</summary>${Object.entries(labels).map(([key,label]) => `<label>${label}
    ${key === 'body' ? `<textarea data-email-part="${key}">${esc(email[key])}</textarea>` : `<input data-email-part="${key}" value="${esc(email[key])}">`}
    </label>`).join('')}${action}</details>`;
}
function renderReview() {
  const c = current.campaign, keys = Object.keys(c.messages);
  if (!keys.length) return;
  reviewRecipient = keys.includes(reviewRecipient) ? reviewRecipient : keys[0];
  const recipient = reviewRecipient;
  const emails = c.messages[recipient].emails;
  if (!emails.length) return;
  const area = el('#review-area');
  const recipientLabel = key => esc(c.recipients.find(row => row.key === key).contact.company) + ' · ' + esc(key);
  area.innerHTML = `<section class="email-review" aria-labelledby="email-review-title">
    <h3 id="email-review-title">Vista previa de los ${emails.length} correos</h3>
    ${keys.length === 1 ? `<p class="muted">${recipientLabel(recipient)}</p>` :
      `<label>Destinatario<select id="review-recipient">${keys.map(key => `<option value="${esc(key)}" ${key === recipient ? 'selected' : ''}>${recipientLabel(key)}</option>`).join('')}</select></label>`}
    <div class="email-previews">${emails.map(email => emailPreview(email, c.brand, c.state === 'draft' ?
      emailEditor(email, '<button type="button" data-save-email>Guardar cambios</button>') : '')).join('')}</div>
  </section>`;
  el('#review-recipient')?.addEventListener('change', event => {reviewRecipient = event.target.value;renderReview();});
  bindEmailPreviews(area, step => '/campaigns/' + c.id + '/preview?recipient=' + encodeURIComponent(recipient) + '&step=' + step);
  area.querySelectorAll('[data-save-email]').forEach(button => button.onclick = () => work(async () => {
    const item = button.closest('[data-preview-step]');
    const next = structuredClone(emails);
    item.querySelectorAll('[data-email-part]').forEach(field => next[Number(item.dataset.previewStep)-1][field.dataset.emailPart] = field.value);
    current = await api('/campaigns/' + c.id + '/messages', 'PUT', {recipient, emails: next});
    renderDetail();
  }));
}

async function prepareDraft(id){
  const progress=el('#outreach-progress');
  const social = current.campaign.channels.includes('social');
  const count = current.campaign.recipients.length;
  progress.hidden = false;
  progress.textContent = social
    ? 'Preparando los mensajes personalizados para ' + count + (count === 1 ? ' lead…' : ' leads…')
    : 'Preparando ' + current.campaign.schedule.length + ' correos por destinatario para ' + count + (count === 1 ? ' lead…' : ' leads…');
  progress.scrollIntoView({block:'center'});
  try {
    current = await api('/campaigns/' + id + '/prepare', 'POST', {});
    renderDetail();
    await load();
    el(social ? '#detail-view h2' : '#email-review-title')?.scrollIntoView({block:'center'});
  } finally {
    progress.hidden = true;
  }
}

function renderSocial(){
  const c=current.campaign;
  el('#detail-view').innerHTML=`<h2>${esc(c.name)}</h2><p class="muted">Copia el mensaje y abre el perfil para contactar a esa empresa.</p>
    ${c.state==='draft'&&!Object.keys(c.messages).length?'<button class="primary" id="prepare-social">Preparar mensajes personalizados</button>':''}
    ${c.recipients.map(recipient=>`<section class="task"><h3>${esc(recipient.contact.company)}</h3>
      ${Object.entries(c.messages[recipient.key]?.social||{}).map(([network,text])=>`<details class="email-preview-item"><summary>${esc(networkNames[network])}</summary>
        <label>Mensaje personalizado<textarea readonly>${esc(text)}</textarea></label><div class="actions"><button data-copy-social>Copiar mensaje</button>
        <a class="button" target="_blank" rel="noopener noreferrer" href="${esc(recipient.contact.social_profiles[network])}">Abrir ${esc(networkNames[network])}</a></div>
      </details>`).join('')}</section>`).join('')}`;
  el('#prepare-social')?.addEventListener('click',()=>work(()=>prepareDraft(c.id)));
  el('#detail-view').querySelectorAll('[data-copy-social]').forEach(button=>button.onclick=()=>work(async()=>{
    await navigator.clipboard.writeText(button.closest('details').querySelector('textarea').value);button.textContent='Copiado';
  }));
}
function renderLinkedInReview(){const c=current.campaign;if(!c.channels.includes('linkedin')||!Object.keys(c.messages).length)return;const section=document.createElement('section');section.className='email-review';section.innerHTML='<h3>Mensajes de LinkedIn para revisar</h3><p class=muted>La invitación y los mensajes se copian y envían personalmente. No hay envío automático.</p>'+c.recipients.map(r=>`<details><summary>${esc(r.contact.company)}</summary>${Object.entries(c.messages[r.key].linkedin).map(([kind,text])=>`<label>${esc(({invitation:'Invitación (máximo 300 caracteres)',message:'Primer mensaje',followup:'Seguimiento'})[kind])}<textarea data-linkedin-recipient="${esc(r.key)}" data-linkedin-kind="${kind}" ${c.state==='draft'?'':'readonly'}>${esc(text)}</textarea></label>`).join('')}${c.state==='draft'?`<button data-save-linkedin="${esc(r.key)}">Guardar textos e invalidar aprobación</button>`:''}</details>`).join('');el('#review-area').after(section);section.querySelectorAll('[data-save-linkedin]').forEach(button=>button.onclick=()=>work(async()=>{const texts={};section.querySelectorAll('[data-linkedin-recipient]').forEach(input=>{if(input.dataset.linkedinRecipient===button.dataset.saveLinkedin)texts[input.dataset.linkedinKind]=input.value;});current=await api('/campaigns/'+c.id+'/linkedin','PUT',{recipient:button.dataset.saveLinkedin,texts});renderDetail();}));}

function library() {
  show('library');
  libraryEdit = catalog.libraries.find(item => item.emails.length === 5);
  const view = el('#library-view');
  view.innerHTML = `<h2>Plantillas base de tu empresa</h2>
    <p class="muted">Para preparar correos dirigidos a una persona, usa «Preparar email» en su tarjeta.</p>
    ${libraryEdit ? `<div class="email-review email-previews">${libraryEdit.emails.map(email =>
      emailPreview(email, catalog.brand, emailEditor(email, ''))).join('')}</div>
      <button id="save-library">Guardar nueva versión de las plantillas</button>`
    : '<button class="primary" id="generate-library">Preparar las cinco plantillas base</button>'}`;
  if (!libraryEdit) {
    el('#generate-library').onclick = () => work(async () => {
      const progress = el('#outreach-progress');
      progress.hidden = false;
      progress.textContent = 'Preparando las cinco plantillas base con el perfil de tu empresa…';
      progress.scrollIntoView({block:'center'});
      try {
        const result = await api('/library?steps=5', 'POST', {});
        catalog.libraries.unshift(result);
        library();
        view.querySelector('h2').scrollIntoView({block:'start'});
      } finally {
        progress.hidden = true;
      }
    });
    return;
  }
  bindEmailPreviews(view, step => '/library/' + libraryEdit.version + '/preview?step=' + step);
  el('#save-library').onclick = () => work(async () => {
    const emails = structuredClone(libraryEdit.emails);
    view.querySelectorAll('[data-preview-step]').forEach(item => {
      const email = emails[Number(item.dataset.previewStep)-1];
      item.querySelectorAll('[data-email-part]').forEach(field => email[field.dataset.emailPart] = field.value);
    });
    const result = await api('/library/' + libraryEdit.version, 'PUT', {emails});
    catalog.libraries.unshift(result);
    library();
  });
}
el('#recipient-filter').oninput=renderRecipients;document.querySelectorAll('[data-channel]').forEach(button=>button.onclick=()=>{setChannel(button.dataset.channel,true);renderRecipients();});el('#select-visible').onchange=e=>{for(const c of filtered()){if(eligible(c)){if(e.target.checked){if(!selected.has(c.id))selected.set(c.id,defaultEmail(c));}else selected.delete(c.id);}}renderRecipients();};el('#new-campaign').onclick=()=>{current=null;show('create');};el('#show-library').onclick=()=>work(library);el('#settings-toggle').onclick=()=>{el('#brand-panel').hidden=!el('#brand-panel').hidden;};el('#campaign-form').onsubmit=event=>{
  event.preventDefault();
  work(createAndPrepare);
};
el('#brand-form').onsubmit=e=>{e.preventDefault();work(async()=>{const f=new FormData(e.target),p=Object.fromEntries([...f].filter(([k])=>k!=='domain_verified'));for(const k of ['domain_verified','workflow_verified','unsubscribe_verified'])p[k]=f.get(k)==='on';p.template_ids=String(f.get('template_ids')||'').split(/\r?\n/).map(x=>x.trim()).filter(Boolean);await api('/brand','PUT',p);await load();el('#brand-panel').hidden=true;if(current)await open(current.campaign.id);});};el('#demo-brand').onclick=()=>{const values={company:catalog.profile.name,color:'#d9af3c',logo_url:'/static/three-dots-logo.svg',sender_name:'Equipo · demostración local',sender_email:'demo@example.test',reply_to:'demo@example.test',signature:catalog.profile.name+' · Ejemplo local',calendar_url:'https://example.test/agenda',authorization_record:'Prueba ficticia sin envíos ni destinatarios reales'};Object.entries(values).forEach(([k,v])=>el('#brand-form [name='+k+']').value=v);el('#brand-form [name=domain_verified]').checked=true;};
work(async()=>{const response=await fetch('/api/intelligence/accounts',{credentials:'same-origin'}),accounts=await response.json();if(!response.ok||!accounts.accounts?.length)throw Error('No hay una cuenta autorizada para esta sesión.');isAdmin=accounts.admin;el('#settings-toggle').hidden=!isAdmin;el('#account-select').innerHTML=accounts.accounts.map(a=>`<option value="${esc(a.id)}">${esc(a.name)}</option>`).join('');const requested=new URLSearchParams(location.search).get('account');tenant=accounts.accounts.find(a=>a.id===requested)?.id||accounts.accounts[0].id;el('#account-select').value=tenant;el('#account-select').onchange=()=>work(async()=>{tenant=el('#account-select').value;selected.clear();clearSelectionNotice();await load();show('create');});setChannel(new URLSearchParams(location.search).get('channel'));await load();if(isAdmin&&location.hash==='#ghl-setup'){el('#brand-panel').hidden=false;el('#ghl-setup').open=true;const locationInput=el('[name=location_id]');locationInput.focus();locationInput.scrollIntoView({block:'center'});}const direct=new URLSearchParams(location.search).get('campaign');if(direct&&catalog.campaigns.some(c=>c.id===direct))await open(direct);if (!direct) await prepareCrmSelection(new URLSearchParams(location.search));});
})();
