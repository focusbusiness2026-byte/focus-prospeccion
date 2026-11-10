'use strict';
// Progressive disclosure only in the private copy. Existing controls and values stay intact.
(() => {
let guidedApplied=false;
function refresh(){document.querySelectorAll('.research-workspace:not([data-guide-entry])').forEach(workspace=>{
workspace.dataset.guideEntry='true';const form=workspace.querySelector('.research-builder');if(!form)return;
const entry=document.createElement('section');entry.className='builder-card active';entry.innerHTML='<p class="eyebrow">CONFIGURACIÓN GUIADA POR IA</p><h3>Define tu cliente ideal conversando</h3><p>Usaremos tu onboarding y la web de tu empresa para aclarar contigo qué clientes buscas. Una pregunta cada vez, con un resumen para revisar antes de raspar.</p><a class="primary button" href="/prospecting-guide">Empezar con el asistente →</a><p>Si ya tienes una búsqueda guardada, puedes seguir utilizándola. Tus ajustes anteriores se conservan.</p>';
const details=document.createElement('details');details.style.marginTop='20px';const summary=document.createElement('summary');summary.textContent='Ajustes avanzados y configuración anterior';summary.style.cssText='cursor:pointer;color:#d9af3c;padding:12px 0';details.append(summary);
const advanced=[...form.children].filter(node=>node.matches('.builder-heading,.builder-tabs,[data-builder-step],[data-prospecting-improvements]'));advanced.forEach(node=>details.append(node));form.prepend(entry,details);
});
const params=new URLSearchParams(location.search),saved=params.get('guided_search'),source=params.get('guided_source');
if(saved&&source&&!guidedApplied){const workspace=document.querySelector('.research-workspace[data-source="'+CSS.escape(source)+'"]'),selector=workspace?.querySelector('[data-saved-search-selector]');if(selector&&[...selector.options].some(o=>o.value===saved)){guidedApplied=true;selector.value=saved;workspace.querySelector('[data-apply-saved-search]')?.click();}else{const company=document.querySelector('#source-selector');if(company&&company.value!==source&&[...company.options].some(o=>o.value===source)){company.value=source;company.dispatchEvent(new Event('change',{bubbles:true}));}}}
}
new MutationObserver(refresh).observe(document.querySelector('#source-list'),{childList:true,subtree:true});refresh();
})();
