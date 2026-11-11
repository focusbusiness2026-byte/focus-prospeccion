"""Opt-in, authenticated, durable copy of ONE account. Never a production entry point.

Required secrets are supplied by hosting, never by a checked-in fixture. The
production reader has a read-only OAuth scope and no mutation methods.
"""
from __future__ import annotations
import json
import os
import sqlite3
from dataclasses import replace
from datetime import datetime,timezone
from urllib.parse import quote
from fastapi import HTTPException,Request,Depends
from fastapi.responses import RedirectResponse,HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.service_account import Credentials
from app import auth,main
from app.config import Settings
from app.auth import Identity,validate_csrf,verify_session,create_session
from app.sheet_store import SheetStore,_column_letter
from app.intelligence import CampaignEngine, CampaignError
from app.intelligence_api import campaign_router
from app.intelligence_adapters import SheetsSnapshot,PrivatePortalSnapshot,OpenAIMessageGenerator,PRODUCTION_BOOK
from app.sandbox_security import verify_handoff
from app.intelligence_contacts import prospect_contact, import_approved_selection

SOURCE_ID="ONB-A23D5DB5"
OWNER="sandbox-owner@example.test"
PORTAL="https://focus-business-sandbox-20261108.moisses.chatgpt.site"


class ApprovedSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[str] = Field(min_length=1, max_length=100)


class ReadOnlySource(SheetStore):
    def _session(self):
        info=json.loads(self.settings.google_service_account_json)
        return AuthorizedSession(Credentials.from_service_account_info(info,scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]))
    def _update(self,*_):raise RuntimeError("El origen es solo lectura")
    def _append(self,*_):raise RuntimeError("El origen es solo lectura")
    def _batch_update(self,*_):raise RuntimeError("El origen es solo lectura")


def selected_table(reader,prop,record_id,field="onboarding_id"):
    title=prop["title"];columns=prop["gridProperties"]["columnCount"];end=_column_letter(columns)
    header=(reader._get(f"'{title}'!A1:{end}1") or [[]])[0]
    if field not in header:return [header]
    col=_column_letter(header.index(field)+1);last=prop["gridProperties"]["rowCount"]
    matches=[i for i,r in enumerate(reader._get(f"'{title}'!{col}2:{col}{last}"),2) if r and str(r[0]).strip()==record_id]
    rows=[]
    # Read only matching client rows; the index scan contains account IDs alone.
    for offset in range(0,len(matches),40):
        ranges=[f"'{title}'!A{i}:{end}{i}" for i in matches[offset:offset+40]]
        response=reader._session().get(reader._base+":batchGet",params={"ranges":ranges},timeout=30)
        response.raise_for_status()
        rows.extend((v.get("values") or [[]])[0] for v in response.json().get("valueRanges",[]))
    return [header,*rows]


def configure(app):
    from app import sandbox as sb
    if os.environ.get("SANDBOX_REAL_MODE")!="1":raise RuntimeError("Modo real no autorizado por configuración")
    private_token=os.environ.get("SANDBOX_STORAGE_TOKEN")
    required=["SANDBOX_OPENAI_API_KEY","SANDBOX_AUTH_SECRET"]
    required += ["SANDBOX_STORAGE_TOKEN"] if private_token else ["SANDBOX_STORAGE_SHEET_ID","SANDBOX_GOOGLE_SERVICE_ACCOUNT_JSON"]
    if any(not os.environ.get(k) for k in required):raise RuntimeError("Falta completar la configuración privada del entorno aislado")
    secret=os.environ["SANDBOX_AUTH_SECRET"]
    if len(secret)<32:raise RuntimeError("Secreto de sesión insuficiente")
    book=os.environ.get("SANDBOX_STORAGE_SHEET_ID","private-portal-d1")
    if book==PRODUCTION_BOOK:raise RuntimeError("El destino de producción está prohibido")
    settings=Settings(_env_file=None,app_env="sandbox-real",google_sheets_enabled=True,central_auth_enabled=False,
        auto_research_enabled=True,allow_external_sources=True,app_secret=secret,google_sheet_id=book,
        google_service_account_json=os.environ.get("SANDBOX_GOOGLE_SERVICE_ACCOUNT_JSON",""),
        openai_api_key=os.environ["SANDBOX_OPENAI_API_KEY"],openai_model=os.environ.get("SANDBOX_OPENAI_MODEL","gpt-5.5"),
        public_base_url=os.environ.get("RENDER_EXTERNAL_URL","http://127.0.0.1:8018"),central_auth_url=PORTAL,
        radar_portal_url="",focus_admin_emails=OWNER)
    sb.SETTINGS=settings
    cells_remote=PrivatePortalSnapshot(private_token,"SandboxCells") if private_token else SheetsSnapshot(settings,"SandboxCells")
    fi_remote=PrivatePortalSnapshot(private_token,"FICampaigns") if private_token else SheetsSnapshot(settings,"FICampaigns")
    path=sb.ROOT/"real-cells.sqlite3";sb.ROOT.mkdir(parents=True,exist_ok=True)
    snapshot=cells_remote.load()
    if snapshot or not path.exists():
        with sqlite3.connect(path) as db:
            if snapshot:
                with sqlite3.connect(":memory:") as restored:
                    restored.executescript(snapshot);restored.backup(db)
            else:
                db.execute("CREATE TABLE sandbox(id INTEGER PRIMARY KEY,payload TEXT NOT NULL)")
                db.execute("INSERT INTO sandbox VALUES(1,'{}')")
            db.execute("CREATE TABLE IF NOT EXISTS research_claims(account TEXT PRIMARY KEY,state TEXT,payload TEXT)")
            cells_remote.save("\n".join(db.iterdump()))

    class RealStore(sb.SandboxStore):
        def __init__(self,settings=None):
            SheetStore.__init__(self,sb.SETTINGS);self.path=path
        def _save(self,data):
            # Sheets calculates these columns with formulas. The isolated cell
            # store must derive them explicitly instead of retaining old values.
            for row in data.get("Accesos",[])[1:]:
                if len(row)<6 or str(row[4]).strip().casefold()=="ilimitado":continue
                try:assigned=int(float(row[4]));used=int(float(row[5]))
                except (TypeError,ValueError):continue
                row.extend([""]*max(0,9-len(row)))
                available=max(0,assigned-used);ratio=available/assigned if assigned>0 else 0
                row[6]=available;row[7]=ratio;row[8]="Agotado" if available==0 else "Atención" if ratio<=0.4 else "Disponible"
            with sb.LOCK,sqlite3.connect(path) as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE sandbox SET payload=? WHERE id=1",(json.dumps(data),))
                cells_remote.save("\n".join(db.iterdump()))
        def due_automation_configs(self):
            return [config for config in super().due_automation_configs()
                    if config["onboarding_id"] == SOURCE_ID and sb.is_guided_schedule(config)]
        def claim_bounded_automation_run(self, config):
            with sb.LOCK:
                if config["onboarding_id"] != SOURCE_ID or not sb.is_guided_schedule(config):
                    return None
                return super().claim_bounded_automation_run(config)
        def claim(self,state,payload=None):
            with sb.LOCK,sqlite3.connect(path) as db:
                db.execute("BEGIN IMMEDIATE")
                prior=db.execute("SELECT state FROM research_claims WHERE account=?",(SOURCE_ID,)).fetchone()
                if state=="started" and prior and prior[0] in {"started","review_required"}:
                    raise RuntimeError("Hay una investigación pendiente de revisión; no se repetirá el consumo")
                db.execute("INSERT INTO research_claims VALUES(?,?,?) ON CONFLICT(account) DO UPDATE SET state=excluded.state,payload=excluded.payload",(SOURCE_ID,state,json.dumps(payload or {})))
                cells_remote.save("\n".join(db.iterdump()))

    if RealStore()._load():RealStore()._save(RealStore()._load())
    main.SheetStore=RealStore
    async def identity(token,settings=None):
        result=verify_session(token,sb.SETTINGS)
        return result if result and result.email==OWNER else None
    async def required(request:Request):
        result=await identity(request.cookies.get(auth.SESSION_COOKIE))
        if not result:raise HTTPException(401,"Inicia sesión en el portal privado de pruebas")
        return result
    auth.verify_central_session=identity
    app.dependency_overrides[auth.require_identity]=required
    engine=CampaignEngine(sb.ROOT/"real-campaigns.sqlite3",persistence=fi_remote,generator=OpenAIMessageGenerator(settings),real=True)
    engine.outreach_settings=settings
    app.include_router(campaign_router(engine,required,main._template))
    original=main._run_onboarding_research
    def isolated_research(source,store,*args,**kwargs):
        if source.record_id!=SOURCE_ID:raise RuntimeError("Solo se permite la cuenta autorizada de esta copia")
        rows=store._load().get("Prospeccion",[])
        existing=[];seen=set()
        for row in rows[1:]:
            p=dict(zip(rows[0],row))
            if p.get("onboarding_id")!=SOURCE_ID:continue
            company=str(p.get("company") or "").strip()[:200];website=str(p.get("website") or "").strip()[:300]
            key=(company.casefold(),website.casefold())
            if not company or key in seen:continue
            seen.add(key);existing.append({"company":company,"website":website})
        controls=dict((args[0] if args else kwargs.get("adjustments")) or {})
        controls["already_in_crm"]=existing[:500]
        controls["exclusions"]=(str(controls.get("exclusions") or "")+"\nNo devolver las empresas de already_in_crm ni sus variantes de marca o dominio: ya existen en el CRM de esta cuenta. Busca candidatas nuevas sin relajar los demás criterios.").strip()
        if args:args=(controls,*args[1:])
        else:kwargs["adjustments"]=controls
        store.claim("started",{"at":datetime.now(timezone.utc).isoformat()})
        try:
            result=original(source,store,*args,**kwargs)
            store.claim("completed",{"execution_id":result["execution_id"],"leads":len(result["prospects"]),"usage":{k:result["trace"].get(k) for k in ("prompt_tokens","output_tokens","total_tokens","web_search_calls")}})
            return result
        except Exception:
            store.claim("review_required")
            raise
    main._run_onboarding_research=isolated_research

    @app.post("/api/intelligence/three-dots/sync-approved-selection")
    def sync_selected(payload:ApprovedSelection,request:Request,identity=Depends(required)):
        validate_csrf(request)
        try:
            added=import_approved_selection(engine,RealStore()._load().get("Prospeccion",[]),SOURCE_ID,payload.ids)
        except CampaignError as exc:raise HTTPException(422,str(exc)) from None
        return {"ok":True,"added":added,"production_written":False}

    @app.post("/api/intelligence/three-dots/sync-approved")
    def sync_approved(request:Request,identity=Depends(required)):
        validate_csrf(request)
        rows=RealStore()._load().get("Prospeccion",[])
        if not rows:raise HTTPException(409,"La cuenta todavía no está importada")
        with engine.db() as db:
            updated=0
            for row in rows[1:]:
                p=dict(zip(rows[0],row));pid=str(p.get("execution_id") or "")
                if p.get("onboarding_id")!=SOURCE_ID or not pid:continue
                old=db.execute("SELECT payload FROM contacts WHERE tenant='three-dots' AND id=?",(pid,)).fetchone()
                approved=p.get("lead_status")=="Aprobado para descarga"
                if not approved and not old:continue
                contact=prospect_contact(p)
                value=json.dumps(contact)
                result=db.execute("INSERT INTO contacts(tenant,id,payload) VALUES('three-dots',?,?) ON CONFLICT(tenant,id) DO UPDATE SET payload=excluded.payload WHERE contacts.payload<>excluded.payload",(pid,value))
                updated+=result.rowcount
                if not approved:
                    db.execute("UPDATE jobs SET status='cancelled' WHERE tenant='three-dots' AND contact=? AND status IN ('pending','retry','awaiting_generation')",(pid,))
                    db.execute("UPDATE tasks SET state='cancelled' WHERE tenant='three-dots' AND contact=? AND state='pending'",(pid,))
                    db.execute("UPDATE members SET state='stopped' WHERE tenant='three-dots' AND contact=?",(pid,))
        return {"ok":True,"updated":updated,"production_written":False}

    @app.post("/sandbox/session")
    async def handoff(request:Request):
        # The private portal's protected cross-site POST arrives with an opaque
        # Origin in the Codex browser. Trust still comes exclusively from the
        # expiring HMAC-signed, audience-bound, single-use handoff below.
        if request.headers.get("origin") not in {PORTAL,"null"}:raise HTTPException(403,"Origen de sesión no autorizado")
        if int(request.headers.get("content-length","0"))>4096:raise HTTPException(413,"Solicitud demasiado grande")
        from urllib.parse import parse_qs
        body=await request.body()
        if len(body)>4096:raise HTTPException(413,"Solicitud demasiado grande")
        token=parse_qs(body.decode()).get("token",[""])[0]
        try:claims=verify_handoff(token,secret,settings.public_base_url)
        except ValueError:raise HTTPException(401,"Acceso privado caducado o inválido") from None
        with engine.db() as db:
            nonce=db.execute("INSERT OR IGNORE INTO incoming VALUES (?,?)",("session",claims["nonce"]))
            if not nonce.rowcount:raise HTTPException(401,"Acceso privado ya utilizado")
        response=RedirectResponse("/sandbox/setup",status_code=303)
        response.set_cookie(auth.SESSION_COOKIE,create_session(Identity(OWNER,"Administrador","sandbox-owner",True,False),settings),httponly=True,secure=request.url.scheme=="https",samesite="lax",max_age=8*3600)
        response.headers["Cache-Control"]="no-store";response.headers["Referrer-Policy"]="no-referrer"
        return response

    @app.get("/sandbox/setup")
    def setup(identity=Depends(required)):
        if RealStore()._load():return RedirectResponse("/portal",status_code=303)
        return HTMLResponse('''<!doctype html><html lang="es"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/intelligence.css"><main><p class="eyebrow">COPIA PRIVADA</p><h1>Three Dots Studio</h1><p>Importa la ficha y sus prospectos reales a un almacenamiento separado. Se conservan los estados de producción y sus datos. Esta acción no inicia raspados, mensajes ni llamadas de IA.</p><button class="primary" id="import">Copiar cuenta autorizada</button><p id="result" role="status"></p></main><script>document.getElementById('import').onclick=async function(){this.disabled=true;const csrf=document.cookie.split('; ').find(x=>x.startsWith('focus_csrf='))?.split('=')[1]||'';try{const r=await fetch('/sandbox/import-client',{method:'POST',headers:{'X-CSRF-Token':csrf}});const p=await r.json();if(!r.ok)throw Error(p.detail||'No se confirmó la importación');location.href='/portal';}catch(e){document.getElementById('result').textContent=e.message;this.disabled=false;}}</script></html>''')

    @app.post("/sandbox/import-client")
    def import_client(request:Request,identity=Depends(required)):
        validate_csrf(request)
        store=RealStore()
        data=store._load()
        if data:
            try:
                engine.catalog("three-dots")
                return {"ok":True,"deduplicated":True,"source_id":SOURCE_ID}
            except ValueError:pass
        source_settings=Settings(_env_file=None,google_sheet_id=PRODUCTION_BOOK,google_service_account_json=settings.google_service_account_json)
        from app.onboarding import OnboardingSource
        if not data:
            if private_token:
                raw=PrivatePortalSnapshot(private_token,"SourceCopy").load()
                if not raw:raise HTTPException(409,"Falta preparar la copia privada de origen")
                data=json.loads(raw)
            else:
                reader=ReadOnlySource(source_settings)
                props={p["title"]:p for p in reader._sheet_properties()}
                data={"Onboarding":selected_table(reader,props["Onboarding"],SOURCE_ID,"ID registro")}
            if len(data["Onboarding"])!=2:raise HTTPException(409,"La ficha de origen no es única; no se copia")
            source=OnboardingSource.from_sheet_record(dict(zip(*data["Onboarding"])))
            import re
            forbidden=re.compile(r'contrase|password|token|api.?key|clave|secret|oauth|credencial|identidad|nif|cif|fiscal|dni|tel[eé]fono|iban|banc|tarjeta',re.I)
            record={k:v for k,v in source.raw_form.items() if not forbidden.search(k)}
            data["Onboarding"]=[list(record),list(record.values())]
            access=data.get("Accesos") if private_token else reader._get("'Accesos'!A1:N1000");email=source.email.lower()
            if not access:raise HTTPException(409,"Falta copiar la cuota del cliente")
            selected=[r for r in access[1:] if r and (str(r[0]).strip().lower()==email or len(r)>12 and str(r[12]).strip().lower()==email)]
            if not selected:raise HTTPException(409,"No se encontró acceso del cliente")
            owner=[OWNER,"Administrador","Activo",datetime.now(timezone.utc).isoformat(),"Ilimitado",0,"","","","","Permitido","No permitido","",""]
            data["Accesos"]=[access[0],owner,*selected]
            for title in ("Prospeccion","Ejecuciones","Revisiones Leads","Busquedas Guardadas","Automatizaciones"):
                if not private_token and title in props:data[title]=selected_table(reader,props[title],SOURCE_ID)
                if title in data:
                    rows=data[title]
                    if rows and "onboarding_id" in rows[0]:
                        pos=rows[0].index("onboarding_id")
                        if any(len(r)<=pos or str(r[pos]).strip()!=SOURCE_ID for r in rows[1:]):raise HTTPException(409,"El origen contiene datos de otra cuenta")
            schedules=data.get("Automatizaciones",[])
            if schedules:
                for row in schedules[1:]:
                    if len(row)>2:row[2]=False
                    if len(row)>4:row[4]=""
        source=OnboardingSource.from_sheet_record(dict(zip(*data["Onboarding"])))
        if source.record_id!=SOURCE_ID or source.company!="Three Dots Studio":raise HTTPException(409,"No coincide la cuenta autorizada")
        contacts=[]
        prospects=data.get("Prospeccion",[])
        for row in prospects[1:]:
            p=dict(zip(prospects[0],row))
            if p.get("lead_status")!="Aprobado para descarga":continue
            contacts.append(prospect_contact(p))
        profile={"id":"three-dots","name":source.company,"website":source.website,"offer":source.main_service,"services":list(source.services),"description":source.description,"audience":', '.join(source.audience),"market":', '.join(source.markets),"plan":"starter","access_emails":[OWNER],"source_id":SOURCE_ID}
        with engine.db() as db:
            db.execute("INSERT OR IGNORE INTO tenants VALUES (?,?)",("three-dots",json.dumps(profile)))
            db.executemany("INSERT OR IGNORE INTO contacts(tenant,id,payload) VALUES(?,?,?)",[("three-dots",c["id"],json.dumps(c)) for c in contacts])
        store._save(data)
        return {"ok":True,"source_id":SOURCE_ID,"prospects_copied":len(prospects)-1,"campaign_contacts":len(contacts),"production_written":False}

    return engine
