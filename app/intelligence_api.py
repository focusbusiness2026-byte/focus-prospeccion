"""Tenant-scoped campaign API, mounted exclusively by the sandbox entry point."""
from __future__ import annotations
import json
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from app.auth import validate_csrf
from app.intelligence import CampaignError


def campaign_router(engine,identity_dependency,template):
    router=APIRouter()

    def scope(tenant,identity):
        if engine.real:
            try:allowed=engine.catalog("three-dots")["profile"].get("access_emails",[])
            except CampaignError:raise HTTPException(409,"Importación privada pendiente; abre /sandbox/setup") from None
            if identity.email.lower() not in allowed or tenant!="three-dots":
                raise HTTPException(403,"Esta sesión no pertenece a la cuenta de pruebas")
        elif "admin" not in identity.role.lower() and identity.email!=f"{tenant}@example.test":
            raise HTTPException(403,"Cuenta no autorizada")
        return tenant

    def run(fn,*args):
        try:return fn(*args)
        except CampaignError as exc:raise HTTPException(422,str(exc)) from None
        except RuntimeError:raise HTTPException(503,"No se pudo confirmar la operación. Comprueba el estado guardado antes de repetir") from None

    @router.get("/campaigns")
    def page(request:Request,identity=Depends(identity_dependency)):
        scope("three-dots",identity)
        return template(request,"campaigns.html",identity=identity)

    @router.get("/api/intelligence/{tenant}/catalog")
    def catalog(tenant:str,identity=Depends(identity_dependency)):
        return run(engine.catalog,scope(tenant,identity))

    @router.post("/api/intelligence/{tenant}/campaigns")
    def create(tenant:str,payload:dict,request:Request,identity=Depends(identity_dependency)):
        validate_csrf(request);return run(engine.create,scope(tenant,identity),payload)

    @router.get("/api/intelligence/{tenant}/campaigns/{cid}")
    def detail(tenant:str,cid:str,identity=Depends(identity_dependency)):
        return run(engine.detail,scope(tenant,identity),cid)

    @router.put("/api/intelligence/{tenant}/campaigns/{cid}")
    def update(tenant:str,cid:str,payload:dict,request:Request,identity=Depends(identity_dependency)):
        validate_csrf(request);return run(engine.update,scope(tenant,identity),cid,payload)

    @router.post("/api/intelligence/{tenant}/campaigns/{cid}/actions")
    def action(tenant:str,cid:str,payload:dict,request:Request,identity=Depends(identity_dependency)):
        validate_csrf(request);t=scope(tenant,identity)
        result=run(engine.action,t,cid,payload.get("action"));engine.tick(tenant=t);return result

    @router.get("/api/intelligence/{tenant}/campaigns/{cid}/messages/{pid}")
    def messages(tenant:str,cid:str,pid:str,identity=Depends(identity_dependency)):
        return run(engine.messages,scope(tenant,identity),cid,pid)

    @router.put("/api/intelligence/{tenant}/campaigns/{cid}/messages/{pid}")
    def edit_messages(tenant:str,cid:str,pid:str,payload:dict,request:Request,identity=Depends(identity_dependency)):
        validate_csrf(request);return run(engine.messages,scope(tenant,identity),cid,pid,payload)

    @router.post("/api/intelligence/{tenant}/campaigns/{cid}/messages/{pid}/generate")
    def generate(tenant:str,cid:str,pid:str,request:Request,identity=Depends(identity_dependency)):
        validate_csrf(request);return run(engine.generate,scope(tenant,identity),cid,pid)

    @router.post("/api/intelligence/{tenant}/campaigns/{cid}/tasks/complete")
    def task(tenant:str,cid:str,payload:dict,request:Request,identity=Depends(identity_dependency)):
        validate_csrf(request);t=scope(tenant,identity)
        result=run(engine.complete_task,t,cid,payload.get("task_id"));engine.tick(tenant=t);return result

    @router.post("/api/intelligence/{tenant}/campaigns/{cid}/events")
    def event(tenant:str,cid:str,payload:dict,request:Request,identity=Depends(identity_dependency)):
        validate_csrf(request);return run(engine.event,scope(tenant,identity),cid,payload.get("contact_id"),payload.get("kind"),payload.get("event_id"),"manual")

    @router.post("/api/intelligence/{tenant}/tick")
    def tick(tenant:str,payload:dict,request:Request,identity=Depends(identity_dependency)):
        validate_csrf(request);t=scope(tenant,identity)
        if engine.real and payload.get("advance"):raise HTTPException(422,"El reloj real no se puede adelantar")
        return run(engine.tick,float(payload.get("advance",0)),t)

    @router.get("/api/intelligence/{tenant}/campaigns/{cid}/export")
    def export(tenant:str,cid:str,identity=Depends(identity_dependency)):
        data=run(engine.detail,scope(tenant,identity),cid)
        return Response(json.dumps(data,ensure_ascii=False,indent=2),media_type="application/json",headers={"Content-Disposition":'attachment; filename="campana-'+cid+'.json"',"Cache-Control":"no-store"})

    return router
