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
