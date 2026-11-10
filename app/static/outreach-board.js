'use strict';
// Loaded exclusively by the sandbox entry point; never changes CRM state.
(() => {
  const selected = new Set();
  let controls;
  function refresh() {
    const board = document.querySelector('#crm-preview-board');
    if (!board) return;
    if (!controls || !controls.isConnected) {
      controls = document.createElement('div');
      controls.className = 'actions';
      controls.style.cssText = 'display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:14px 0';
      controls.innerHTML = '<label><input type="checkbox" data-campaign-all> Seleccionar todos los aprobados visibles</label><button type="button" data-campaign-prepare>Preparar campaña</button><span data-campaign-count></span>';
      board.before(controls);
      controls.querySelector('[data-campaign-all]').onchange = event => {
        board.querySelectorAll('[data-campaign-select]').forEach(input => {input.checked = event.target.checked; if(input.checked)selected.add(input.value);else selected.delete(input.value);});
        count();
      };
      controls.querySelector('[data-campaign-prepare]').onclick = async event => {
        const visible = new Set([...board.querySelectorAll('[data-campaign-select]')].map(input => input.value));
        const ids=[...selected].filter(id=>visible.has(id)), button=event.currentTarget;
        if(!ids.length)return;
        button.disabled=true;
        try {
          const csrf=document.cookie.split('; ').find(row=>row.startsWith('focus_csrf='))?.split('=').slice(1).join('=')||'';
          const response=await fetch('/api/intelligence/three-dots/sync-approved-selection',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':decodeURIComponent(csrf)},body:JSON.stringify({ids})});
          const result=await response.json();
          if(!response.ok||!result.ok)throw Error(result.detail||'No se pudo preparar la selección.');
          sessionStorage.setItem('focus-campaign-selection',JSON.stringify({ids}));
          window.location.assign('/campaigns/outreach');
        } catch(error) {
          controls.querySelector('[data-campaign-count]').textContent=error.message||'No se pudo preparar la selección.';
          button.disabled=false;
        }
      };
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
  function count(){if(controls){const visible=[...document.querySelectorAll('#crm-preview-board [data-campaign-select]')].filter(input=>selected.has(input.value)).length;const label=controls.querySelector('[data-campaign-count]'),text=visible+' aprobados visibles para preparar · '+selected.size+' seleccionados en total';if(label.textContent!==text)label.textContent=text;controls.querySelector('[data-campaign-prepare]').disabled=visible===0;}}
  new MutationObserver(refresh).observe(document.body,{childList:true,subtree:true});
  refresh();
})();
