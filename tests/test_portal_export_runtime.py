"""Exercise the small browser-side export functions without client data."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


PORTAL = Path("app/templates/portal.html")


def _function(name: str) -> str:
    return next(
        line.strip()
        for line in PORTAL.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith(f"function {name}(")
    )


def _run_js(script: str) -> None:
    if not shutil.which("node"):
        pytest.skip("Node.js is required for portal JavaScript runtime tests")
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False, timeout=15)
    assert result.returncode == 0, result.stderr


def test_csv_download_waits_for_browser_and_escapes_formula_cells():
    js = "\n".join(_function(name) for name in ("safeCsvCell", "downloadLocalText", "downloadGhlContacts"))
    _run_js(
        "const assert=require('node:assert/strict');const vm=require('node:vm');"
        "const timers=[],downloads=[],revoked=[],links=[];"
        "const context={Blob:class{constructor(parts,options){this.parts=parts;this.type=options.type;}},"
        "URL:{createObjectURL(blob){downloads.push(blob);return 'blob:test';},revokeObjectURL(url){revoked.push(url);}},"
        "document:{body:{appendChild(link){link.connected=true;links.push(link);}},createElement(){return {connected:false,click(){assert.equal(this.connected,true);},remove(){this.connected=false;}};}},"
        "setTimeout(callback,delay){assert.equal(delay,30000);timers.push(callback);},"
        "exportScope(){return {valid:true,reason:'',duplicates:[]};},"
        "ghlContactRows(){return [{company:'=FORMULA',email:'test@example.invalid'}];},"
        "recordExportTrace(){},safeExportFilename(){return 'leads-prueba';},message:{textContent:''}};"
        "vm.createContext(context);vm.runInContext(" + json.dumps(js) + ",context);"
        "context.downloadGhlContacts([], 'Aprobado para descarga', ['company','email'], 'leads-prueba');"
        "assert.equal(downloads.length,1);assert.equal(downloads[0].parts[0],"
        + json.dumps("\ufeffcompany,email\r\n\"'=FORMULA\",\"test@example.invalid\"") + ");"
        "assert.equal(links[0].download,'leads-prueba.csv');"
        "assert.equal(links[0].connected,true);assert.equal(revoked.length,0);"
        "timers[0]();assert.equal(links[0].connected,false);assert.deepEqual(revoked,['blob:test']);"
        "assert.equal(context.safeCsvCell('@evil')," + json.dumps("\"'@evil\"") + ");"
    )


def test_csv_download_anchor_stays_inside_open_modal_dialog():
    js = _function("downloadLocalText")
    _run_js(
        "const assert=require('node:assert/strict');const vm=require('node:vm');"
        "const links=[],revoked=[],timers=[];"
        "const modal={appendChild(link){link.parent=this;links.push(link);}};"
        "const context={Blob:class{},URL:{createObjectURL(){return 'blob:test';},"
        "revokeObjectURL(url){revoked.push(url);}},"
        "document:{body:{appendChild(){throw Error('body is inert while modal is open');}},"
        "querySelector(selector){return selector==='dialog:modal'?modal:null;},"
        "createElement(){return {click(){assert.equal(this.parent,modal);},"
        "remove(){this.parent=null;}};}},setTimeout(callback,delay){assert.equal(delay,30000);timers.push(callback);}};"
        "vm.createContext(context);vm.runInContext(" + json.dumps(js) + ",context);"
        "context.downloadLocalText('leads-test.csv','company\\r\\nTest Company','text/csv');"
        "assert.equal(links.length,1);assert.equal(links[0].download,'leads-test.csv');"
        "assert.equal(links[0].parent,modal);assert.equal(revoked.length,0);"
        "timers[0]();assert.equal(links[0].parent,null);assert.deepEqual(revoked,['blob:test']);"
    )


def test_meta_export_requires_eligible_rows_and_one_logical_account():
    js = "\n".join(_function(name) for name in ("exportScope", "downloadAudienceCsv"))
    _run_js(
        "const assert=require('node:assert/strict');const vm=require('node:vm');"
        "let leads=[{execution_id:'a',onboarding_id:'ONB-OLD',email:'one@example.invalid',productora:'Estudio',lead_status:'Aprobado para descarga'},"
        "{execution_id:'b',onboarding_id:'ONB-NEW',email:'one@example.invalid',productora:'Estudio',lead_status:'Aprobado para descarga'}];"
        "const downloads=[],context={dashboardData:{sources:[{onboarding_id:'ONB-NEW',onboarding_alias_ids:['ONB-NEW','ONB-OLD'],email:'one@example.invalid'}]},"
        "filtered(){return leads;},dedupeExportLeads(items){return {unique:items,duplicates:[]};},"
        "eligibleAudienceLeads(){return leads;},audienceContactData(item){return {email:item.execution_id+'@example.invalid'};},"
        "safeCsvCell(value){return '\"'+value+'\"';},recordExportTrace(){},downloadLocalText(...args){downloads.push(args);},"
        "safeExportFilename(){return 'meta-prueba';},message:{textContent:''}};"
        "vm.createContext(context);vm.runInContext(" + json.dumps(js) + ",context);"
        "assert.equal(context.exportScope('Aprobado para descarga').valid,true);"
        "const form={elements:{kanban_status:{value:'Aprobado para descarga'},filename:{value:'meta-prueba'}},"
        "querySelectorAll(selector){return selector.includes('lead_ids')?leads.map(item=>({value:item.execution_id})):[{value:'email'}];}};"
        "context.downloadAudienceCsv(form);assert.equal(downloads.length,1);"
        "assert.equal(downloads[0][0],'meta-prueba.csv');assert.equal(downloads[0][1],"
        "'\\ufeffemail\\r\\n\"a@example.invalid\"\\r\\n\"b@example.invalid\"');"
        "leads=[...leads,{execution_id:'c',onboarding_id:'ONB-OTHER',email:'two@example.invalid',productora:'Estudio',lead_status:'Aprobado para descarga'}];"
        "assert.equal(context.exportScope('Aprobado para descarga').valid,false);"
        "context.downloadAudienceCsv(form);assert.equal(downloads.length,1);"
        "assert.match(context.message.textContent,/una sola cuenta/);"
    )


@pytest.mark.parametrize("storage_failure", ["corrupt", "denied"])
def test_real_ghl_rows_export_without_obsolete_favorite_key_even_if_trace_fails(storage_failure):
    js = "\n".join(_function(name) for name in (
        "safeCsvCell", "recordExportTrace", "preferredExportAutomation", "ghlContactRows", "downloadGhlContacts", "downloadAudienceCsv"
    ))
    _run_js(
        "const assert=require('node:assert/strict');const vm=require('node:vm');"
        "const downloads=[],lead={execution_id:'synthetic',onboarding_id:'ONB-SYN',company:'Test Company'};"
        "const context={localStorage:{getItem(){"
        + ("throw new Error('storage denied');" if storage_failure == "denied" else "return '{';") + "},"
        "setItem(){" + ("throw new Error('storage denied');" if storage_failure == "denied" else "") + "}},"
        "exportScope(){return {valid:true,reason:'',productora:'Test Company',duplicates:[],items:[lead]};},"
        "readCrmBoard(){return {assignments:{},columns:[]};},"
        "readPreviewAutomations(){return [{id:'ONB-SYN',name:'Automatización favorita',favorite:true}];},"
        "copyableLeadFields(){return [];},eligibleAudienceLeads(){return [lead];},"
        "audienceContactData(){return {email:'test@example.invalid'};},"
        "safeExportFilename(value){return value;},"
        "downloadLocalText(...args){downloads.push(args);},message:{textContent:''}};"
        "vm.createContext(context);vm.runInContext(" + json.dumps(js) + ",context);"
        "context.downloadGhlContacts([], 'Aprobado para descarga', ['company','search_origin'], 'ghl-test');"
        "const form={elements:{kanban_status:{value:'Aprobado para descarga'},filename:{value:'meta-test'}},"
        "querySelectorAll(selector){return selector.includes('lead_ids')?[{value:'synthetic'}]:[{value:'email'}];}};"
        "context.downloadAudienceCsv(form);"
        "assert.deepEqual(downloads.map(item=>item[0]),['ghl-test.csv','meta-test.csv']);"
        "assert.match(downloads[0][1],/Test Company/);assert.match(downloads[1][1],/test@example.invalid/);"
        "assert.match(downloads[0][1],/\"Automatización favorita\"/);"
    )


def test_meta_export_review_remains_clickable_when_preferences_storage_is_denied():
    js = _function("updateAudienceReview")
    _run_js(
        "const assert=require('node:assert/strict');const vm=require('node:vm');"
        "const button={disabled:true},form={elements:{kanban_status:{value:'Aprobado para descarga'},"
        "client:{value:'Test Company'},purpose:{value:'Legal B2B'},filename:{value:'meta-test'}},"
        "querySelectorAll(selector){return selector.includes('lead_ids')?[{value:'synthetic'}]:[{value:'email'}];}};"
        "const context={audiencePreferenceKey:'prefs',localStorage:{setItem(){throw Error('storage denied');}},"
        "exportScope(){return {valid:true,reason:'',duplicates:[]};},esc(value){return value;},setText(){},"
        "document:{querySelector(selector){return ({'#audience-export-form':form,"
        "'#audience-legal-confirmation':{checked:true},'#audience-review':{innerHTML:''},"
        "'#download-audience-csv':button})[selector];}}};"
        "vm.createContext(context);vm.runInContext(" + json.dumps(js) + ",context);"
        "context.updateAudienceReview();assert.equal(button.disabled,false);"
    )
