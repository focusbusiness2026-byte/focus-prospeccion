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
      controls.innerHTML = '<div><strong>Contactar a los leads seleccionados</strong><label><input type="checkbox" data-campaign-all> Seleccionar todos los aprobados visibles</label><small data-campaign-count></small></div><div class="crm-campaign-channels"><button class="primary" type="button" data-campaign-prepare="email">Email</button><button class="secondary" type="button" data-campaign-prepare="social">LinkedIn y redes</button></div><p data-campaign-status role="status" hidden></p>';
      board.before(controls);
      controls.querySelector('[data-campaign-all]').onchange = event => {
        board.querySelectorAll('[data-campaign-select]').forEach(input => {input.checked = event.target.checked; if(input.checked)selected.add(input.value);else selected.delete(input.value);});
        count();
      };
      controls.querySelectorAll('[data-campaign-prepare]').forEach(button => button.onclick = async () => {
        const visible = new Set([...board.querySelectorAll('[data-campaign-select]')].map(input => input.value));
        const ids=[...selected].filter(id=>visible.has(id));
        if(!ids.length||preparing)return;
        preparing=true;
        count();
        const status=controls.querySelector('[data-campaign-status]');
        status.hidden=false;
        status.textContent='Cargando los destinatarios…';
        try {
          const csrf=document.cookie.split('; ').find(row=>row.startsWith('focus_csrf='))?.split('=').slice(1).join('=')||'';
          const response=await fetch('/api/intelligence/three-dots/sync-approved-selection',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':decodeURIComponent(csrf)},body:JSON.stringify({ids})});
          const result=await response.json();
          if(!response.ok||!result.ok)throw Error(result.detail||'No se pudo preparar la selección.');
          const channel=button.dataset.campaignPrepare;
          sessionStorage.setItem('focus-campaign-selection',JSON.stringify({ids,channel}));
          window.top.location.assign('/campaigns/outreach?channel='+encodeURIComponent(channel));
        } catch(error) {
          status.textContent=error.message||'No se pudo preparar la selección.';
          preparing=false;
          count();
        }
      });
    }
    board.querySelectorAll('.crm-board-card[data-current-column="Aprobado para descarga"]').forEach(card => {
      if (card.querySelector('[data-campaign-select]')) return;
      const label = document.createElement('label');
      const input = document.createElement('input');
      input.type = 'checkbox'; input.value = card.dataset.dragProspect; input.dataset.campaignSelect = 'true';
      input.checked = selected.has(input.value); input.onchange = () => {if(input.checked)selected.add(input.value);else selected.delete(input.value);count();};
      label.append(input,document.createTextNode(' Seleccionar para campaña'));card.append(label);
    });
    count();
  }
  function count(){if(controls){const visible=[...document.querySelectorAll('#crm-preview-board [data-campaign-select]')].filter(input=>selected.has(input.value)).length;const label=controls.querySelector('[data-campaign-count]'),text=visible===1?'1 lead aprobado seleccionado':visible+' leads aprobados seleccionados';if(label.textContent!==text)label.textContent=text;controls.querySelectorAll('[data-campaign-prepare]').forEach(button=>button.disabled=preparing||visible===0);}}
  new MutationObserver(refresh).observe(document.body,{childList:true,subtree:true});
  refresh();
})();
