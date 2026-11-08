"""Campaign domain and sandbox adapters. No network or production-store dependency."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
import threading
import hashlib
from contextlib import contextmanager
from pathlib import Path

KINDS = {"start", "message", "linkedin", "wait", "condition", "followup", "crm", "interested", "not_interested", "email", "stop"}
CONDITIONS = {"approved", "has_linkedin", "invitation_sent", "connection_accepted", "responded", "conversation_open", "opt_out", "meeting"}
EVENTS = {"invitation_sent", "connection_accepted", "response", "positive_response", "conversation_open", "opt_out", "meeting", "bounce", "opportunity_won", "not_interested"}
PLANS = {"starter": {"campaigns": 3, "contacts": 100}, "pro": {"campaigns": 20, "contacts": 1000}}
DEFAULT_STEPS = [{"kind":"start"}, {"kind":"message"}, {"kind":"crm"}, {"kind":"linkedin"},
    {"kind":"wait", "days":2},
    {"kind":"condition", "condition":"connection_accepted", "yes":6, "no":7},
    {"kind":"followup"}, {"kind":"stop"}]
TEMPLATES = {
    "linkedin": {"name":"LinkedIn asistido", "steps":DEFAULT_STEPS},
    "email": {"name":"Email · prueba sin envío", "steps":[{"kind":"start"},{"kind":"message"},{"kind":"crm"},{"kind":"email"},{"kind":"wait","days":3},{"kind":"followup","channel":"email"},{"kind":"stop"}]},
    "meeting": {"name":"Seguimiento de reunión", "steps":[{"kind":"start"},{"kind":"message"},{"kind":"condition","condition":"meeting","yes":3,"no":4},{"kind":"interested"},{"kind":"stop"}]},
}


class CampaignError(ValueError):
    pass


def validate_steps(steps):
    if not isinstance(steps,list) or not 2 <= len(steps) <= 30:
        raise CampaignError("La secuencia debe tener entre 2 y 30 bloques")
    if steps[0].get("kind") != "start" or sum(s.get("kind")=="start" for s in steps)!=1:
        raise CampaignError("Debe haber un único Inicio en el primer bloque")
    edges = {}
    for i,s in enumerate(steps):
        kind = s.get("kind")
        if kind not in KINDS:
            raise CampaignError("Tipo de bloque no admitido")
        if kind=="wait" and (not isinstance(s.get("days",1),(int,float)) or not 0 <= s.get("days",1) <= 365):
            raise CampaignError("Espera entre 0 y 365 días")
        if kind=="condition" and s.get("condition") not in CONDITIONS:
            raise CampaignError("Condición no admitida")
        dests = [] if kind=="stop" else ([s.get("yes",i+1),s.get("no",i+1)] if kind=="condition" else [s.get("next",i+1)])
        if any(type(d) is not int or d<0 or d>=len(steps) for d in dests):
            raise CampaignError("Cada rama debe apuntar a un bloque existente; termina con Detener")
        edges[i]=dests
    seen,active=set(),set()
    def visit(i):
        if i in active: raise CampaignError("La secuencia contiene un ciclo")
        if i in seen:return
        seen.add(i);active.add(i)
        for dest in edges[i]:visit(dest)
        active.remove(i)
    visit(0)
    if len(seen)!=len(steps):raise CampaignError("Hay bloques desconectados")
    if not any(s.get("kind")=="stop" for s in steps):raise CampaignError("Añade un bloque Detener")
    return steps


class CampaignEngine:
    """SQLite transactions are the claim boundary; all side effects are local mocks."""
    def __init__(self,path:Path,seed_path:Path|None=None,clock=time.time,*,persistence=None,generator=None,real=False):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True);self.clock=clock
        self.persistence=persistence;self.generator=generator;self.real=real;self.lock=threading.RLock()
        if persistence:
            snapshot=persistence.load()
            if snapshot:
                with sqlite3.connect(":memory:") as restored,sqlite3.connect(self.path) as target:
                    restored.executescript(snapshot);restored.backup(target)
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS tenants(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS contacts(tenant TEXT,id TEXT,payload TEXT NOT NULL,flags TEXT NOT NULL DEFAULT '{}',PRIMARY KEY(tenant,id));
            CREATE TABLE IF NOT EXISTS campaigns(tenant TEXT,id TEXT,payload TEXT NOT NULL,PRIMARY KEY(tenant,id));
            CREATE TABLE IF NOT EXISTS members(tenant TEXT,campaign TEXT,contact TEXT,step INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL DEFAULT 'queued',PRIMARY KEY(tenant,campaign,contact));
            CREATE TABLE IF NOT EXISTS jobs(tenant TEXT,campaign TEXT,contact TEXT,step INTEGER,due REAL NOT NULL,status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT,PRIMARY KEY(tenant,campaign,contact,step));
            CREATE TABLE IF NOT EXISTS messages(tenant TEXT,campaign TEXT,contact TEXT,payload TEXT NOT NULL,PRIMARY KEY(tenant,campaign,contact));
            CREATE TABLE IF NOT EXISTS tasks(tenant TEXT,id TEXT,campaign TEXT,contact TEXT,step INTEGER,kind TEXT,state TEXT NOT NULL DEFAULT 'pending',PRIMARY KEY(tenant,id),UNIQUE(tenant,campaign,contact,step));
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,tenant TEXT,campaign TEXT,contact TEXT,kind TEXT,origin TEXT,actor TEXT,payload TEXT,at REAL);
            CREATE TABLE IF NOT EXISTS incoming(tenant TEXT,id TEXT,PRIMARY KEY(tenant,id));
            CREATE TABLE IF NOT EXISTS crm(tenant TEXT,contact TEXT,payload TEXT NOT NULL,PRIMARY KEY(tenant,contact));
            CREATE TABLE IF NOT EXISTS outbox(tenant TEXT,id TEXT,campaign TEXT,contact TEXT,payload TEXT,status TEXT,PRIMARY KEY(tenant,id));
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS generation(tenant TEXT,campaign TEXT,contact TEXT,state TEXT,request_id TEXT,payload TEXT,PRIMARY KEY(tenant,campaign,contact));
            INSERT OR IGNORE INTO meta VALUES('offset',0);
            ''')
            if seed_path:
                data=json.loads(seed_path.read_text(encoding="utf-8"));profile=data["profile"]
                db.execute("INSERT OR IGNORE INTO tenants VALUES (?,?)",(profile["id"],json.dumps(profile)))
                for contact in data["contacts"]:
                    db.execute("INSERT OR IGNORE INTO contacts(tenant,id,payload) VALUES (?,?,?)",(profile["id"],contact["id"],json.dumps(contact)))
                other={"id":"control","name":"Estudio de control ficticio","offer":"Vídeo corporativo","website":"https://control.example.test","market":"España","plan":"starter"}
                db.execute("INSERT OR IGNORE INTO tenants VALUES (?,?)",("control",json.dumps(other)))
                db.execute("INSERT OR IGNORE INTO contacts(tenant,id,payload) VALUES (?,?,?)",("control","control-1",json.dumps({"id":"control-1","company":"Empresa ficticia de control","name":"Contacto ficticio","email":"control@example.test","sector":"Tecnología","city":"Madrid","approved":True,"linkedin":""})))

    @contextmanager
    def db(self):
        with self.lock:
            db=sqlite3.connect(self.path,timeout=15);db.row_factory=sqlite3.Row
            try:
                db.execute("PRAGMA foreign_keys=ON");db.execute("BEGIN IMMEDIATE")
                before=db.total_changes
                yield db
                if self.persistence and db.total_changes!=before:
                    self.persistence.save("\n".join(db.iterdump()))
                db.commit()
            except Exception:
                db.rollback();raise
            finally:db.close()

    def now(self,db):return self.clock()+db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()[0]

    def _profile(self,db,tenant):
        row=db.execute("SELECT payload FROM tenants WHERE id=?",(tenant,)).fetchone()
        if not row:raise CampaignError("Cuenta no disponible")
        return json.loads(row[0])

    def _campaign(self,db,tenant,cid):
        row=db.execute("SELECT payload FROM campaigns WHERE tenant=? AND id=?",(tenant,cid)).fetchone()
        if not row:raise CampaignError("Campaña no encontrada en esta cuenta")
        return json.loads(row[0])

    def _event(self,db,t,c,p,kind,payload=None,origin="system_simulated",actor="sandbox-admin"):
        db.execute("INSERT INTO events(tenant,campaign,contact,kind,origin,actor,payload,at) VALUES (?,?,?,?,?,?,?,?)",(t,c,p,kind,origin,actor,json.dumps(payload or {}),self.now(db)))

    def _save(self,db,t,c):
        c["updated_at"]=self.now(db)
        db.execute("INSERT INTO campaigns VALUES (?,?,?) ON CONFLICT(tenant,id) DO UPDATE SET payload=excluded.payload",(t,c["id"],json.dumps(c)))

    def _queue(self,db,t,c,p,step,due):
        db.execute("INSERT OR IGNORE INTO jobs(tenant,campaign,contact,step,due) VALUES (?,?,?,?,?)",(t,c,p,step,due))
        db.execute("UPDATE members SET step=?,state='queued' WHERE tenant=? AND campaign=? AND contact=?",(step,t,c,p))

    def catalog(self,tenant):
        with self.db() as db:
            profile=self._profile(db,tenant)
            contacts=[{**json.loads(r["payload"]),"flags":json.loads(r["flags"])} for r in db.execute("SELECT * FROM contacts WHERE tenant=?",(tenant,))]
            campaigns=[json.loads(r[0]) for r in db.execute("SELECT payload FROM campaigns WHERE tenant=? ORDER BY rowid DESC",(tenant,))]
            return {"profile":profile,"contacts":contacts,"campaigns":campaigns,"templates":TEMPLATES,"plans":PLANS,"clock":self.now(db),"mode":"real_isolated" if self.real else "sandbox","providers":{"messages":"OpenAI real · generación manual con consumo" if self.real else "Plantilla local simulada; OpenAI no invocado","crm":"GoHighLevel sin conexión ni escrituras","email":"Preparación local; sin envío"}}

    def create(self,tenant,payload):
        with self.db() as db:
            profile=self._profile(db,tenant);limit=PLANS[profile["plan"]]
            count=sum(json.loads(r[0])["state"]!="archived" for r in db.execute("SELECT payload FROM campaigns WHERE tenant=?",(tenant,)))
            if count>=limit["campaigns"]:raise CampaignError("Límite de campañas del plan de pruebas alcanzado")
            ids=list(dict.fromkeys(payload.get("contact_ids",[])))
            if not ids or len(ids)>limit["contacts"]:raise CampaignError("Selecciona contactos dentro del límite del plan")
            for pid in ids:
                row=db.execute("SELECT payload,flags FROM contacts WHERE tenant=? AND id=?",(tenant,pid)).fetchone()
                if not row or not json.loads(row[0]).get("approved"):raise CampaignError("Solo se admiten contactos aprobados de esta cuenta")
                if json.loads(row[1]).get("opt_out"):raise CampaignError("Un contacto está excluido")
            c={"id":str(uuid.uuid4()),"name":str(payload.get("name","")).strip()[:120],"objective":str(payload.get("objective","")).strip()[:500],
                "offer":str(payload.get("offer") or profile["offer"])[:300],"language":payload.get("language","es"),"tone":payload.get("tone","cercano"),"length":payload.get("length","breve"),
                "steps":validate_steps(payload.get("steps",DEFAULT_STEPS)),"contact_ids":ids,"state":"draft","version":1,
                "email_authorization":False,"email_origin":"","created_at":self.now(db),"simulated":not self.real}
            if not c["name"] or not c["objective"]:raise CampaignError("Indica nombre y objetivo comercial")
            if c["language"] not in {"es","en"} or c["tone"] not in {"cercano","formal"} or c["length"] not in {"breve","amplio"}:raise CampaignError("Idioma, tono o extensión no admitidos")
            self._save(db,tenant,c)
            db.executemany("INSERT INTO members(tenant,campaign,contact) VALUES (?,?,?)",[(tenant,c["id"],pid) for pid in ids])
            self._event(db,tenant,c["id"],"","campaign_created",origin="manual")
            return c

    def update(self,tenant,cid,payload):
        with self.db() as db:
            c=self._campaign(db,tenant,cid)
            if c["state"]!="draft":raise CampaignError("Solo se editan borradores; duplica una campaña activada")
            for key in ("name","objective","offer","language","tone","length","email_origin"):
                if key in payload:c[key]=str(payload[key])[:500]
            if "steps" in payload:c["steps"]=validate_steps(payload["steps"])
            c["email_authorization"]=payload.get("email_authorization",c["email_authorization"]) is True
            if c["language"] not in {"es","en"} or c["tone"] not in {"cercano","formal"} or c["length"] not in {"breve","amplio"}:raise CampaignError("Preferencias de mensaje no admitidas")
            if not c["name"].strip() or not c["objective"].strip():raise CampaignError("Nombre y objetivo obligatorios")
            c["version"]+=1;self._save(db,tenant,c)
            for row in db.execute("SELECT contact,payload FROM messages WHERE tenant=? AND campaign=?",(tenant,cid)).fetchall():
                message=json.loads(row["payload"]);message["approved"]=False;message["stale_context"]=True
                db.execute("UPDATE messages SET payload=? WHERE tenant=? AND campaign=? AND contact=?",(json.dumps(message),tenant,cid,row["contact"]))
            self._event(db,tenant,cid,"","draft_saved",origin="manual")
            return c

    def action(self,tenant,cid,action):
        if action=="duplicate":
            with self.db() as db:old=self._campaign(db,tenant,cid)
            return self.create(tenant,{**old,"name":old["name"]+" · copia"})
        with self.db() as db:
            c=self._campaign(db,tenant,cid)
            if action=="activate":
                if c["state"]!="draft":raise CampaignError("La campaña ya fue activada")
                validate_steps(c["steps"])
                if any(s["kind"]=="email" or s.get("channel")=="email" for s in c["steps"]) and not(c["email_authorization"] and c["email_origin"].strip()):
                    raise CampaignError("Documenta procedencia y autorización de prueba antes de simular email")
                c["state"]="active"
                for pid in c["contact_ids"]:self._queue(db,tenant,cid,pid,0,self.now(db))
            elif action=="pause" and c["state"]=="active":c["state"]="paused"
            elif action=="resume" and c["state"]=="paused":c["state"]="active"
            elif action=="archive":
                c["state"]="archived"
                db.execute("UPDATE jobs SET status='cancelled' WHERE tenant=? AND campaign=? AND status IN ('pending','retry')",(tenant,cid))
                db.execute("UPDATE tasks SET state='cancelled' WHERE tenant=? AND campaign=? AND state='pending'",(tenant,cid))
            else:raise CampaignError("Transición de campaña no permitida")
            self._save(db,tenant,c);self._event(db,tenant,cid,"","campaign_"+action,origin="manual")
            return c

    def _draft(self,db,t,c,p):
        row=db.execute("SELECT payload FROM messages WHERE tenant=? AND campaign=? AND contact=?",(t,c["id"],p["id"])).fetchone()
        if row:
            message=json.loads(row[0])
            if self.real and message.get("stale_context"):raise CampaignError("El contexto cambió; regenera y revisa el mensaje")
            return message
        if self.real:raise CampaignError("Genera primero el mensaje con OpenAI; no se sustituye por una simulación")
        profile=self._profile(db,t);en=c["language"]=="en"
        sector=p.get("sector","sector por confirmar");city=p.get("city","ubicación por confirmar")
        intro=(f"Hello, {p['company']} team. I work with {profile['name']}. For your {sector} activity in {city}, could {c['offer']} help explain your services?" if en else
            f"Hola, equipo de {p['company']}. Soy de {profile['name']}. Por vuestro contexto en {sector}, en {city}, quería explorar si {c['offer'].lower()} podría ayudaros a explicar lo que hacéis.")
        if c["tone"]=="formal":intro=intro.replace("Hola, equipo de","Buenos días, equipo de").replace("quería explorar","me gustaría valorar")
        extra=(" We would first review a topic and format together, without assuming your needs or budget." if en else " Primero revisaríamos juntos un tema y un formato, sin dar por hecho vuestras necesidades ni vuestro presupuesto.") if c["length"]=="amplio" else ""
        m={"version":1,"invitation":(f"Hello. I work on {c['offer']} at {profile['name']}. May we connect?" if en else f"Hola. Trabajo en {profile['name']} con {c['offer'].lower()}. ¿Conectamos?"),
            "first":intro+extra,"followup":("Would it be useful to review a concrete example? If this is not relevant, I will stop here." if en else "¿Os serviría revisar un ejemplo concreto? Si no encaja, lo dejamos aquí."),
            "email":intro+extra+(" May I share a short proposal?" if en else " ¿Os puedo compartir una propuesta breve?"),
            "rationale":f"Contexto declarado: {profile['name']}, oferta {c['offer']}; sector {sector}, ciudad {city}. Hipótesis: el formato podría ayudar a explicar sus servicios. No se atribuyen necesidades, presupuestos ni noticias no verificadas.",
            "context":{"client":profile["name"],"offer":c["offer"],"sector":sector,"city":city,"objective":c["objective"],"source":p.get("source","datos ficticios")},
            "approved":False,"provider":"local_template_simulation","billable_tokens":0,"history":[],"generated_at":self.now(db)}
        db.execute("INSERT INTO messages VALUES (?,?,?,?)",(t,c["id"],p["id"],json.dumps(m)))
        self._event(db,t,c["id"],p["id"],"message_prepared",{"provider":m["provider"],"version":1})
        return m

    def messages(self,tenant,cid,pid,payload=None):
        with self.db() as db:
            c=self._campaign(db,tenant,cid)
            if pid not in c["contact_ids"]:raise CampaignError("Contacto ajeno a la campaña")
            p=json.loads(db.execute("SELECT payload FROM contacts WHERE tenant=? AND id=?",(tenant,pid)).fetchone()[0])
            m=self._draft(db,tenant,c,p)
            if payload is not None:
                m["history"].append({**{k:m.get(k) for k in ("version","invitation","first","followup","email","rationale","context","usage","approved","provider_response_id")},"at":self.now(db)})
                for key in ("invitation","first","followup","email"):
                    if key in payload:m[key]=str(payload[key])[:3000]
                m["version"]+=1;m["approved"]=payload.get("approved") is True
                db.execute("UPDATE messages SET payload=? WHERE tenant=? AND campaign=? AND contact=?",(json.dumps(m),tenant,cid,pid))
                self._event(db,tenant,cid,pid,"message_edited",{"version":m["version"],"approved":m["approved"]},"manual")
            return m

    def generate(self,t,cid,pid):
        """Persist a claim before calling a paid provider. An uncertain call is never retried automatically."""
        if not self.real or not self.generator:raise CampaignError("OpenAI real no está configurado")
        with self.db() as db:
            c=self._campaign(db,t,cid)
            if pid not in c["contact_ids"]:raise CampaignError("Contacto ajeno a la campaña")
            found=db.execute("SELECT payload FROM messages WHERE tenant=? AND campaign=? AND contact=?",(t,cid,pid)).fetchone()
            old_message=json.loads(found[0]) if found else None
            if old_message and not old_message.get("stale_context"):return old_message
            previous=db.execute("SELECT state,payload FROM generation WHERE tenant=? AND campaign=? AND contact=?",(t,cid,pid)).fetchone()
            if previous and (previous["state"]!="completed" or json.loads(previous["payload"]).get("campaign_version")==c["version"]):
                raise CampaignError("Generación iniciada o pendiente de revisión; no se repite el consumo")
            p=json.loads(db.execute("SELECT payload FROM contacts WHERE tenant=? AND id=?",(t,pid)).fetchone()[0]);profile=self._profile(db,t)
            request_id=str(uuid.uuid4())
            db.execute("INSERT INTO generation VALUES (?,?,?,?,?,?) ON CONFLICT(tenant,campaign,contact) DO UPDATE SET state=excluded.state,request_id=excluded.request_id,payload=excluded.payload",(t,cid,pid,"started",request_id,json.dumps({"campaign_version":c["version"]})))
            self._event(db,t,cid,pid,"openai_started",{"request_id":request_id},"provider")
        try:
            message=self.generator(profile,c,p)
            for key in ("invitation","first","followup","email","rationale"):
                if not isinstance(message.get(key),str) or not message[key].strip():raise ValueError("Respuesta incompleta")
        except Exception:
            with self.db() as db:
                db.execute("UPDATE generation SET state='review_required' WHERE tenant=? AND campaign=? AND contact=?",(t,cid,pid))
                c=self._campaign(db,t,cid);c["state"]="paused";self._save(db,t,c)
                self._event(db,t,cid,pid,"openai_review_required",{"request_id":request_id},"provider")
            raise CampaignError("No se pudo confirmar la generación. Campaña pausada; revisa el consumo antes de repetir") from None
        history=[] if not old_message else [*old_message.get("history",[]),{k:old_message.get(k) for k in ("version","invitation","first","followup","email","rationale","context","usage","provider_response_id")}]
        message.update(version=1 if not old_message else old_message["version"]+1,approved=False,history=history,provider="openai",request_id=request_id,context={"client":profile["name"],"objective":c["objective"],"offer":c["offer"],"campaign_version":c["version"]})
        message["context"]["recipient"]={k:p.get(k,"") for k in ("id","company","website","sector","city","source")}
        message["context"]["facts_summary"]=str(p.get("summary") or "")[:4000]
        message["context"]["research_sha256"]=hashlib.sha256(str(p.get("research_dossier_json") or "").encode()).hexdigest()
        with self.db() as db:
            message["generated_at"]=self.now(db)
            latest=self._campaign(db,t,cid)
            message["stale_context"]=latest["version"]!=c["version"]
            db.execute("INSERT INTO messages VALUES (?,?,?,?) ON CONFLICT(tenant,campaign,contact) DO UPDATE SET payload=excluded.payload",(t,cid,pid,json.dumps(message)))
            db.execute("UPDATE generation SET state='completed',payload=? WHERE tenant=? AND campaign=? AND contact=?",(json.dumps({"usage":message.get("usage",{}),"campaign_version":c["version"]}),t,cid,pid))
            db.execute("UPDATE jobs SET status='pending' WHERE tenant=? AND campaign=? AND contact=? AND status='awaiting_generation'",(t,cid,pid))
            self._event(db,t,cid,pid,"openai_completed",{"usage":message.get("usage",{}),"provider_response_id":message.get("provider_response_id")},"provider")
        return message

    def tick(self,advance=0,tenant=None):
        if not 0<=advance<=30*86400 or self.real and advance:raise CampaignError("El reloj real no se puede adelantar")
        with self.db() as db:
            if advance:db.execute("UPDATE meta SET value=value+? WHERE key='offset'",(advance,))
            now=self.now(db);processed=0
            for _ in range(40):
                sql="SELECT j.* FROM jobs j JOIN campaigns c ON c.tenant=j.tenant AND c.id=j.campaign WHERE j.status IN ('pending','retry') AND j.due<=? AND json_extract(c.payload,'$.state')='active'"
                args=[now]
                if tenant:sql+=" AND j.tenant=?";args.append(tenant)
                jobs=db.execute(sql+" ORDER BY j.due LIMIT 100",args).fetchall()
                if not jobs:break
                progress=False
                for j in jobs:
                    t,cid,pid,index=j["tenant"],j["campaign"],j["contact"],j["step"]
                    c=self._campaign(db,t,cid);row=db.execute("SELECT payload,flags FROM contacts WHERE tenant=? AND id=?",(t,pid)).fetchone();p=json.loads(row[0]);flags=json.loads(row[1]);s=c["steps"][index];kind=s["kind"]
                    if any(flags.get(k) for k in ("opt_out","responded","meeting","not_interested")):
                        db.execute("UPDATE jobs SET status='cancelled' WHERE tenant=? AND campaign=? AND contact=? AND status IN ('pending','retry')",(t,cid,pid))
                        db.execute("UPDATE members SET state='stopped' WHERE tenant=? AND campaign=? AND contact=?",(t,cid,pid));progress=True;continue
                    # Controlled API failure simulation. No remote call is made.
                    failures=min(2,max(0,int(s.get("simulate_failures",0))))
                    if kind in {"crm","email"} and j["attempts"]<failures:
                        db.execute("UPDATE jobs SET status='retry',attempts=attempts+1,due=?,last_error='Simulated HTTP 429' WHERE tenant=? AND campaign=? AND contact=? AND step=?",(now+5*2**j["attempts"],t,cid,pid,index))
                        self._event(db,t,cid,pid,"api_retry_simulated",{"step":index,"attempt":j["attempts"]+1});progress=True;continue
                    nxt=s.get("next",index+1);due=now;terminal=False
                    if kind=="message":
                        try:self._draft(db,t,c,p)
                        except CampaignError:
                            db.execute("UPDATE jobs SET status='awaiting_generation' WHERE tenant=? AND campaign=? AND contact=? AND step=?",(t,cid,pid,index))
                            db.execute("UPDATE members SET state='awaiting_generation' WHERE tenant=? AND campaign=? AND contact=?",(t,cid,pid));progress=True;continue
                    elif kind in {"linkedin","followup"} and s.get("channel")!="email":
                        task_id=f"{cid}:{pid}:{index}"
                        db.execute("INSERT OR IGNORE INTO tasks(tenant,id,campaign,contact,step,kind) VALUES (?,?,?,?,?,?)",(t,task_id,cid,pid,index,kind))
                        db.execute("UPDATE members SET state='awaiting_manual' WHERE tenant=? AND campaign=? AND contact=?",(t,cid,pid))
                        terminal=True;self._event(db,t,cid,pid,"task_created",{"task_id":task_id,"kind":kind})
                    elif kind=="wait":due=now+float(s.get("days",1))*86400
                    elif kind=="condition":
                        key=s["condition"];result=p.get("approved",False) if key=="approved" else bool(p.get("linkedin")) if key=="has_linkedin" else bool(flags.get(key))
                        nxt=s.get("yes" if result else "no",index+1)
                        self._event(db,t,cid,pid,"condition_checked",{"condition":key,"result":result})
                    elif kind=="crm":
                        crm={"external_id":f"mock-{t}-{pid}","contact_id":pid,"company":p["company"],"email":p["email"],"sector":p.get("sector"),"city":p.get("city"),"offer":c["offer"],"campaign_id":cid,"status":"synced_simulated","provider":"mock_ghl"}
                        db.execute("INSERT INTO crm VALUES (?,?,?) ON CONFLICT(tenant,contact) DO UPDATE SET payload=excluded.payload",(t,pid,json.dumps(crm)))
                        self._event(db,t,cid,pid,"crm_upsert_simulated",{"external_id":crm["external_id"]})
                    elif kind=="email" or kind=="followup" and s.get("channel")=="email":
                        m=self._draft(db,t,c,p)
                        if flags.get("bounce"):
                            terminal=True
                            db.execute("UPDATE members SET state='stopped' WHERE tenant=? AND campaign=? AND contact=?",(t,cid,pid))
                        elif not(c["email_authorization"] and c["email_origin"].strip() and m["approved"]):
                            db.execute("UPDATE members SET state='awaiting_email_approval' WHERE tenant=? AND campaign=? AND contact=?",(t,cid,pid));continue
                        else:
                            key=f"{cid}:{pid}:{index}"
                            db.execute("INSERT OR IGNORE INTO outbox VALUES (?,?,?,?,?,?)",(t,key,cid,pid,json.dumps({"to":p["email"],"text":m["followup"] if kind=="followup" else m["email"],"message_version":m["version"],"origin":c["email_origin"]}),"simulated_only"))
                            self._event(db,t,cid,pid,"email_simulated",{"message_version":m["version"],"sent_real":False})
                    elif kind in {"interested","not_interested"}:
                        flags["interested" if kind=="interested" else "not_interested"]=True
                        db.execute("UPDATE contacts SET flags=? WHERE tenant=? AND id=?",(json.dumps(flags),t,pid))
                        self._event(db,t,cid,pid,kind)
                    elif kind=="stop":
                        db.execute("UPDATE members SET state='completed' WHERE tenant=? AND campaign=? AND contact=?",(t,cid,pid));terminal=True
                    db.execute("UPDATE jobs SET status='done',attempts=attempts+1 WHERE tenant=? AND campaign=? AND contact=? AND step=?",(t,cid,pid,index))
                    if not terminal:self._queue(db,t,cid,pid,nxt,due)
                    self._event(db,t,cid,pid,"step_completed",{"step":index,"kind":kind});processed+=1;progress=True
                if not progress:break
            return {"processed":processed,"clock":now,"real_calls":0}

    def complete_task(self,t,cid,task_id):
        with self.db() as db:
            c=self._campaign(db,t,cid)
            if c["state"]!="active":raise CampaignError("Reanuda la campaña antes de completar tareas")
            task=db.execute("SELECT * FROM tasks WHERE tenant=? AND campaign=? AND id=?",(t,cid,task_id)).fetchone()
            if not task:raise CampaignError("Tarea no encontrada")
            if task["state"]!="pending":return {"ok":True,"deduplicated":True}
            flags=json.loads(db.execute("SELECT flags FROM contacts WHERE tenant=? AND id=?",(t,task["contact"])).fetchone()[0])
            if any(flags.get(k) for k in ("opt_out","responded","meeting","not_interested")):raise CampaignError("Contacto detenido o excluido")
            db.execute("UPDATE tasks SET state='completed' WHERE tenant=? AND id=?",(t,task_id))
            self._event(db,t,cid,task["contact"],"task_completed",{"task_id":task_id,"kind":task["kind"]},"manual")
            step=c["steps"][task["step"]];self._queue(db,t,cid,task["contact"],step.get("next",task["step"]+1),self.now(db))
            return {"ok":True}

    def event(self,t,cid,pid,kind,event_id=None,origin="manual"):
        if kind not in EVENTS:raise CampaignError("Evento no admitido")
        with self.db() as db:
            c=self._campaign(db,t,cid)
            if pid not in c["contact_ids"]:raise CampaignError("Contacto fuera de campaña")
            if event_id:
                prior=db.execute("INSERT OR IGNORE INTO incoming VALUES (?,?)",(t,event_id))
                if not prior.rowcount:return {"ok":True,"deduplicated":True}
            row=db.execute("SELECT flags FROM contacts WHERE tenant=? AND id=?",(t,pid)).fetchone();flags=json.loads(row[0])
            flags[kind]=True
            if kind in {"response","positive_response"}:flags["responded"]=True
            db.execute("UPDATE contacts SET flags=? WHERE tenant=? AND id=?",(json.dumps(flags),t,pid))
            if kind in {"response","positive_response","opt_out","meeting","not_interested"}:
                db.execute("UPDATE jobs SET status='cancelled' WHERE tenant=? AND contact=? AND status IN ('pending','retry')",(t,pid))
                db.execute("UPDATE tasks SET state='cancelled' WHERE tenant=? AND contact=? AND state='pending'",(t,pid))
                db.execute("UPDATE members SET state='stopped' WHERE tenant=? AND contact=?",(t,pid))
            if kind=="bounce":
                self._event(db,t,cid,pid,"email_bounced_simulated",origin=origin)
            self._event(db,t,cid,pid,kind,{"event_id":event_id},origin)
            return {"ok":True}

    def plan(self,t,plan):
        if plan not in PLANS:raise CampaignError("Plan no admitido")
        with self.db() as db:
            profile=self._profile(db,t);profile["plan"]=plan
            db.execute("UPDATE tenants SET payload=? WHERE id=?",(json.dumps(profile),t))
            return {"ok":True,"plan":plan,"charge":0,"billing":"simulated"}

    def detail(self,t,cid):
        with self.db() as db:
            c=self._campaign(db,t,cid)
            members=[dict(r) for r in db.execute("SELECT contact,step,state FROM members WHERE tenant=? AND campaign=?",(t,cid))]
            messages={r["contact"]:json.loads(r["payload"]) for r in db.execute("SELECT * FROM messages WHERE tenant=? AND campaign=?",(t,cid))}
            tasks=[dict(r) for r in db.execute("SELECT id,contact,step,kind,state FROM tasks WHERE tenant=? AND campaign=?",(t,cid))]
            events=[{**dict(r),"payload":json.loads(r["payload"])} for r in db.execute("SELECT * FROM events WHERE tenant=? AND campaign=? ORDER BY seq DESC LIMIT 400",(t,cid))]
            crm=[json.loads(r[0]) for r in db.execute("SELECT payload FROM crm WHERE tenant=?",(t,)) if json.loads(r[0]).get("campaign_id")==cid]
            outbox=[{**dict(r),"payload":json.loads(r["payload"])} for r in db.execute("SELECT id,contact,payload,status FROM outbox WHERE tenant=? AND campaign=?",(t,cid))]
            jobs=[dict(r) for r in db.execute("SELECT contact,step,due,status,attempts,last_error FROM jobs WHERE tenant=? AND campaign=?",(t,cid))]
            all_events=[dict(r) for r in db.execute("SELECT kind,contact,origin,payload FROM events WHERE tenant=? AND campaign=?",(t,cid))]
            generations=[json.loads(e["payload"]).get("usage",{}) for e in all_events if e["kind"]=="openai_completed"]
            count=lambda k:len({e["contact"] for e in all_events if e["kind"]==k})
            metrics={"participants":len(members),"approved":len(members),"tasks_pending":sum(x["state"]=="pending" for x in tasks),"tasks_completed":sum(x["state"]=="completed" for x in tasks),
                "invitations_manual":count("invitation_sent"),"connections_manual":count("connection_accepted"),"responses_registered":len({e["contact"] for e in all_events if e["kind"] in {"response","positive_response"}}),
                "positive_responses":count("positive_response"),"meetings_registered":count("meeting"),"crm_contacts_simulated":len(crm),"emails_simulated":len(outbox),"customers_simulated":count("opportunity_won"),
                "real_messages_sent":0,"billable_calls":len(generations),"unconfirmed_calls":sum(e["kind"]=="openai_started" for e in all_events)-len(generations),"actual_cost_usd":None if self.real else 0,"prepared_messages":len(messages),
                "input_tokens":sum(u.get("input_tokens",0) for u in generations),"output_tokens":sum(u.get("output_tokens",0) for u in generations)}
            return {"campaign":c,"members":members,"messages":messages,"tasks":tasks,"events":events,"crm_mock":crm,"outbox":outbox,"jobs":jobs,"metrics":metrics,"clock":self.now(db),"mode":"sandbox"}
