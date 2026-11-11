"""Additive campaign library and dispatch ledger sharing the durable campaign store.

No provider is enabled implicitly. GHL owns the sending clock; tick() never sends
emails. Every paid/provider operation is claimed durably before leaving a tx.
"""
from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timezone

from app.intelligence import CampaignError
from app.outreach_providers import TemplateSetupPending
from app.outreach_content import (DemoSequenceGenerator, QUICK_STEPS, STEPS, STOP_EVENTS, dates,
    evidence, fingerprint, personal_linkedin, render_email, social_profiles, valid_email, validate_brand, validate_emails)


class OutreachService:
    def __init__(self, engine, *, generator=None, provider=None):
        self.engine = engine
        self.generator = generator if generator is not None else (None if engine.real else DemoSequenceGenerator())
        self.provider = provider
        self.template_provider = None
        with engine.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS outreach_brand(tenant TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outreach_libraries(tenant TEXT,version INTEGER,payload TEXT NOT NULL,PRIMARY KEY(tenant,version));
            CREATE TABLE IF NOT EXISTS outreach_campaigns(tenant TEXT,id TEXT,payload TEXT NOT NULL,PRIMARY KEY(tenant,id));
            CREATE TABLE IF NOT EXISTS outreach_claims(tenant TEXT,id TEXT,status TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(tenant,id));
            CREATE TABLE IF NOT EXISTS outreach_reservations(tenant TEXT,recipient TEXT,campaign TEXT,PRIMARY KEY(tenant,recipient));
            CREATE TABLE IF NOT EXISTS outreach_events(tenant TEXT,id TEXT,payload TEXT NOT NULL,PRIMARY KEY(tenant,id));
            CREATE TABLE IF NOT EXISTS outreach_tasks(tenant TEXT,id TEXT,campaign TEXT,recipient TEXT,kind TEXT,due REAL,state TEXT,payload TEXT,PRIMARY KEY(tenant,id));
            CREATE TABLE IF NOT EXISTS outreach_suppression(tenant TEXT,recipient TEXT,reason TEXT,PRIMARY KEY(tenant,recipient));
            ''')

    def _read(self, db, table, tenant, key=None):
        # Table names are private constants, never request input.
        where, args = ("tenant=?", [tenant]) if key is None else ("tenant=? AND id=?", [tenant, key])
        row = db.execute(f"SELECT payload FROM {table} WHERE {where}", args).fetchone()
        return json.loads(row[0]) if row else None

    def _campaign(self, db, t, cid):
        self.engine._profile(db, t)
        c = self._read(db, "outreach_campaigns", t, cid)
        if not c:
            raise CampaignError("Campaña no encontrada en esta cuenta")
        return c

    def _save(self, db, t, c):
        remote = c.get("provider_states", {})
        if c["state"] == "active" and remote and len(remote) == len(c["recipients"]) and all(r.get("status") in {"completed", "stopped"} for r in remote.values()):
            pending = db.execute("SELECT 1 FROM outreach_tasks WHERE tenant=? AND campaign=? AND state='pending'", (t, c["id"])).fetchone()
            if not pending:
                c["state"] = "completed"
                db.execute("DELETE FROM outreach_reservations WHERE tenant=? AND campaign=?", (t, c["id"]))
        c["updated_at"] = self.engine.now(db)
        db.execute("INSERT INTO outreach_campaigns VALUES(?,?,?) ON CONFLICT(tenant,id) DO UPDATE SET payload=excluded.payload", (t, c["id"], json.dumps(c)))

    def _event(self, db, t, kind, cid="", **payload):
        value = {"kind": kind, "campaign": cid, "at": self.engine.now(db), **payload}
        db.execute("INSERT INTO outreach_events VALUES(?,?,?)", (t, str(uuid.uuid4()), json.dumps(value)))

    def _draft(self, c):
        if c["state"] not in {"draft", "generation_review_required"}:
            raise CampaignError("La campaña está congelada; duplica para preparar otra versión")

    def _invalidate(self, c):
        c["version"] += 1
        c["approval"] = None

    def brand(self, t):
        with self.engine.db() as db:
            p = self.engine._profile(db, t)
            b = self._read(db, "outreach_brand", t) or {"company": p["name"], "color": "#d9af3c", "version": 0}
            return b

    def configure_brand(self, t, payload, *, initial_location_only=False):
        payload = validate_brand(payload)
        with self.engine.db() as db:
            self.engine._profile(db, t)
            old = self._read(db, "outreach_brand", t) or {"version": 0}
            if initial_location_only and old.get("location_id"):
                if old["location_id"] != payload["location_id"]:
                    raise CampaignError("Ya hay una subcuenta vinculada. Revisa el cambio en Configuración administrativa")
                return old
            if old.get("location_id") and payload.get("location_id", old["location_id"]) != old["location_id"]:
                active = [json.loads(r[0]) for r in db.execute("SELECT payload FROM outreach_campaigns WHERE tenant=?", (t,))]
                if any(c.get("delivery_mode") == "live" and c["state"] not in {"completed", "archived"} for c in active):
                    raise CampaignError("Concilia y cierra las campañas GHL antes de cambiar su subcuenta")
            if any(k in payload and payload[k] != old.get(k) for k in ("logo_url", "color", "sender_name", "sender_email", "calendar_url", "signature", "location_id")):
                # Native templates include branding. Editing it requires fresh
                # administrative verification before another real activation.
                payload = {**payload, "workflow_verified": False, "unsubscribe_verified": False, "template_ids": []}
            brand = {**old, **payload, "version": old["version"] + 1}
            db.execute("INSERT INTO outreach_brand VALUES(?,?) ON CONFLICT(tenant) DO UPDATE SET payload=excluded.payload", (t, json.dumps(brand)))
            # Active campaigns retain their original immutable brand snapshot.
            for row in db.execute("SELECT payload FROM outreach_campaigns WHERE tenant=?", (t,)).fetchall():
                c = json.loads(row[0])
                if c["state"] == "draft":
                    c["brand"] = brand
                    self._invalidate(c)
                    self._save(db, t, c)
            self._event(db, t, "brand_updated", version=brand["version"])
            return brand

    def _contacts(self, db, t):
        return [{**json.loads(r["payload"]), "flags": json.loads(r["flags"])} for r in db.execute("SELECT payload,flags FROM contacts WHERE tenant=?", (t,))]

    def eligibility(self, t):
        with self.engine.db() as db:
            self.engine._profile(db, t)
            result = []
            for contact in self._contacts(db, t):
                options = contact.get("email_options") or ([{"email": contact["email"], "source_url": contact.get("source_url", "")} ] if valid_email(contact.get("email")) else [])
                unique_options = {}
                for option in options:
                    if valid_email(option.get("email")):
                        unique_options.setdefault(option["email"].strip().lower(), option)
                options = list(unique_options.values())
                reasons = []
                if not contact.get("approved"):
                    reasons.append("Pendiente de aprobación del lead")
                if any(contact.get("flags", {}).get(k) for k in ("opt_out", "responded", "meeting", "not_interested")):
                    reasons.append("Contacto detenido o excluido")
                identifiers = [o["email"].strip().lower() for o in options] + [contact.get("linkedin", "").rstrip("/").lower(), "lead:" + contact["id"]]
                if any(db.execute("SELECT 1 FROM outreach_reservations WHERE tenant=? AND recipient=?", (t, key)).fetchone() for key in identifiers if key):
                    reasons.append("Campaña activa o pausada para este contacto")
                if any(db.execute("SELECT 1 FROM outreach_suppression WHERE tenant=? AND recipient=?", (t, key)).fetchone() for key in identifiers if key):
                    reasons.append("Baja o condición de parada registrada")
                result.append({**contact, "email_options": options, "email_ready": bool(options) and not reasons,
                    "linkedin_ready": personal_linkedin(contact.get("linkedin")) and not reasons,
                    "social_ready": bool(social_profiles(contact)) and not reasons,
                    "social_profiles": social_profiles(contact),
                    "reasons": reasons + ([] if options else ["Sin email público disponible"]) + ([] if personal_linkedin(contact.get("linkedin")) else ["Sin perfil personal LinkedIn verificado"])})
            return result

    def catalog(self, t):
        contacts = self.eligibility(t)
        with self.engine.db() as db:
            profile = self.engine._profile(db, t)
            libs = [json.loads(r[0]) for r in db.execute("SELECT payload FROM outreach_libraries WHERE tenant=? ORDER BY version DESC", (t,))]
            campaigns = [json.loads(r[0]) for r in db.execute("SELECT payload FROM outreach_campaigns WHERE tenant=? ORDER BY rowid DESC", (t,))]
            return {"profile": profile, "contacts": contacts, "libraries": libs, "campaigns": campaigns,
                "brand": self._read(db, "outreach_brand", t) or {"company": profile["name"], "color": "#d9af3c", "version": 0},
                "generation_ready": self.generator is not None, "sending_ready": self.provider is not None,
                "mode": "real_isolated" if self.engine.real else "local_demo", "clock": self.engine.now(db)}

    def _generate(self, t, key, context, library=None):
        if self.generator is None:
            raise CampaignError("Preparación de IA pendiente del administrador; no se ha consumido API")
        with self.engine.db() as db:
            prior = self._read(db, "outreach_claims", t, key)
            if prior:
                if prior["status"] == "completed":
                    return prior["result"]
                raise CampaignError("Generación pendiente de revisión; no se repite una llamada de resultado incierto")
            claim = {"status": "started", "context_hash": fingerprint(context), "at": self.engine.now(db)}
            db.execute("INSERT INTO outreach_claims VALUES(?,?,?,?)", (t, key, "started", json.dumps(claim)))
        try:
            result = self.generator(context, library)
            if context.get("networks"):
                social = result["social"]
                if not isinstance(social, dict) or set(social) != set(context["networks"]) or any(
                        not isinstance(text, str) or not text.strip() or len(text) > 900 or "<" in text or ">" in text
                        for text in social.values()):
                    raise ValueError("Mensajes de redes incompletos")
                result["emails"] = []
            else:
                result["emails"] = validate_emails(result, {s["id"] for s in context["sources"]}, context.get("steps", STEPS))
        except Exception:
            with self.engine.db() as db:
                db.execute("UPDATE outreach_claims SET status='review_required',payload=? WHERE tenant=? AND id=?", (json.dumps({**claim, "status": "review_required"}), t, key))
            raise CampaignError("La generación necesita revisión; no se reintentará automáticamente ni se sustituyen textos reales por ejemplos") from None
        with self.engine.db() as db:
            db.execute("UPDATE outreach_claims SET status='completed',payload=? WHERE tenant=? AND id=?", (json.dumps({**claim, "status": "completed", "result": result}), t, key))
        return result

    def library(self, t, steps=STEPS):
        with self.engine.db() as db:
            p = self.engine._profile(db, t)
            ctx = evidence(p, include_saved_context=steps == QUICK_STEPS)
            if steps != STEPS:
                ctx["steps"] = steps
            digest = fingerprint(ctx)
            rows = db.execute("SELECT payload FROM outreach_libraries WHERE tenant=? ORDER BY version DESC", (t,)).fetchall()
            for row in rows:
                old = json.loads(row[0])
                if old["context_hash"] == digest:
                    return old
            version = len(rows) + 1
        result = self._generate(t, "library:" + digest, ctx)
        value = {**result, "version": version, "context": ctx, "context_hash": digest, "review_required": True}
        with self.engine.db() as db:
            # A concurrent request for the same context reuses the durable claim.
            for row in db.execute("SELECT payload FROM outreach_libraries WHERE tenant=?", (t,)):
                existing = json.loads(row[0])
                if existing["context_hash"] == digest:
                    return existing
            version = db.execute("SELECT COALESCE(MAX(version),0)+1 FROM outreach_libraries WHERE tenant=?", (t,)).fetchone()[0]
            value["version"] = version
            db.execute("INSERT INTO outreach_libraries VALUES(?,?,?)", (t, version, json.dumps(value)))
            self._event(db, t, "library_prepared", version=version, simulated=not self.engine.real)
        return value

    def create(self, t, payload, *, duplicate_from=None):
        name = str(payload.get("name", "")).strip()[:120]
        channels = list(dict.fromkeys(payload.get("channels", [])))
        selections = payload.get("recipients", [])
        steps = payload.get("steps", STEPS)
        if not channels or set(channels) - {"email", "linkedin", "social"} or not selections or len(selections) > 100 or steps not in {QUICK_STEPS, STEPS} or ("social" in channels and channels != ["social"]):
            raise CampaignError("Indica un canal y entre 1 y 100 destinatarios")
        eligible = {c["id"]: c for c in self.eligibility(t)}
        recipients = []
        seen = set()
        for chosen in selections:
            p = eligible.get(chosen.get("lead_id"))
            duplicate_ok = duplicate_from and p and any(r["lead_id"] == p["id"] for r in duplicate_from["recipients"]) and not any(p.get("flags", {}).get(k) for k in ("opt_out", "responded", "meeting", "not_interested")) and "Baja o condición de parada registrada" not in p["reasons"]
            if not p or not p.get("approved") or not (duplicate_ok or any(p.get(k) for k in ("email_ready", "linkedin_ready", "social_ready"))):
                raise CampaignError("Solo se admiten leads aprobados y disponibles de esta cuenta")
            email = str(chosen.get("email", "")).strip().lower()
            if "email" in channels:
                options = [o["email"].strip().lower() for o in p["email_options"]]
                if not email and len(options) == 1:
                    email = options[0]
                if not (p["email_ready"] or duplicate_ok) or email not in options:
                    raise CampaignError("Elige un correo público de la tarjeta para este destinatario")
            if "linkedin" in channels and not (p["linkedin_ready"] or duplicate_ok and personal_linkedin(p.get("linkedin"))):
                raise CampaignError("Selecciona un perfil personal LinkedIn verificado")
            if "social" in channels and not (p["social_ready"] or duplicate_ok and p["social_profiles"]):
                raise CampaignError("Selecciona un lead con un perfil público de LinkedIn, Instagram o Facebook")
            key = "lead:" + p["id"] if "social" in channels else email or p["linkedin"].rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            reservations = list(dict.fromkeys(x for x in (key, "lead:" + p["id"], p.get("linkedin", "").rstrip("/").lower()) if x))
            recipients.append({"key": key, "lead_id": p["id"], "email": email, "contact": p, "reservation_keys": reservations})
        if not recipients:
            raise CampaignError("No quedan destinatarios elegibles")
        with self.engine.db() as db:
            self.engine._profile(db, t)
            cid = str(uuid.uuid4())
            label = "Contacto personalizado" if "social" in channels else "Campaña de email"
            subject = recipients[0]["contact"]["company"] if len(recipients) == 1 else f"{len(recipients)} empresas"
            c = {"id": cid, "name": name or f"{label} · {subject}"[:120], "auto_name": not name,
                "channels": channels, "recipients": recipients, "state": "draft", "version": 1,
                "brand": self._read(db, "outreach_brand", t) or {}, "approval": None, "messages": {}, "library": None,
                "schedule": [] if "social" in channels else dates(self.engine.now(db), steps),
                "created_at": self.engine.now(db), "provider_states": {}}
            self._save(db, t, c)
            self._event(db, t, "campaign_created", cid)
            return c

    def edit_library(self, t, version, emails):
        with self.engine.db() as db:
            self.engine._profile(db, t)
            row = db.execute("SELECT payload FROM outreach_libraries WHERE tenant=? AND version=?", (t, version)).fetchone()
            if not row:
                raise CampaignError("Biblioteca no encontrada")
            lib = json.loads(row[0])
            rows = validate_emails({"emails": emails}, {s["id"] for s in lib["context"]["sources"]}, len(lib["emails"]))
            latest = db.execute("SELECT MAX(version) FROM outreach_libraries WHERE tenant=?", (t,)).fetchone()[0]
            if latest != version:
                raise CampaignError("La biblioteca cambió; recarga antes de editar")
            lib.update(version=version + 1, emails=rows, edited_manually=True, review_required=True)
            db.execute("INSERT INTO outreach_libraries VALUES(?,?,?)", (t, lib["version"], json.dumps(lib)))
            for row in db.execute("SELECT payload FROM outreach_campaigns WHERE tenant=?", (t,)).fetchall():
                c = json.loads(row[0])
                if c["state"] == "draft" and len(c["schedule"]) == len(lib["emails"]):
                    c.update(messages={}, library=None)
                    self._invalidate(c)
                    self._save(db, t, c)
            self._event(db, t, "library_edited", version=lib["version"], origin="manual")
            return lib

    def reschedule(self, t, cid):
        with self.engine.db() as db:
            c = self._campaign(db, t, cid)
            self._draft(c)
            c["schedule"] = dates(self.engine.now(db), len(c["schedule"]))
            self._invalidate(c)
            self._save(db, t, c)
        return self.detail(t, cid)

    def provision_templates(self, t, version):
        provider = self.template_provider or self.provider
        if provider is None:
            raise CampaignError("Preparación GHL deshabilitada; no se ha creado ninguna plantilla")
        with self.engine.db() as db:
            self.engine._profile(db, t)
            row = db.execute("SELECT payload FROM outreach_libraries WHERE tenant=? AND version=?", (t, version)).fetchone()
            if not row:
                raise CampaignError("Biblioteca no encontrada")
            library = json.loads(row[0])
            brand = self._read(db, "outreach_brand", t) or {}
            if not brand.get("location_id") or not brand.get("sender_email") or not brand.get("calendar_url"):
                raise CampaignError("Completa la configuración administrativa antes de preparar GHL")
            template_brand = {k: brand.get(k) for k in ("location_id", "company", "logo_url", "color", "sender_name", "sender_email", "signature", "calendar_url", "reply_to")}
            key = "provision:" + fingerprint({"library": library, "brand": template_brand})
            prior = self._read(db, "outreach_claims", t, key)
            if prior and len(library["emails"]) == QUICK_STEPS and prior.get("status") == "completed":
                return {"template_ids": prior["template_ids"], "workflow_verified": False, "reused": True}
            if prior:
                raise CampaignError("Preparación ya iniciada; revisa los IDs guardados, sin repetir automáticamente")
            db.execute("INSERT INTO outreach_claims VALUES(?,?,?,?)", (t, key, "started", json.dumps({"status":"started"})))
        try:
            ids = provider.provision_templates(t, brand, library)
            if len(ids) != len(library["emails"]):
                raise ValueError()
        except TemplateSetupPending as error:
            with self.engine.db() as db:
                db.execute("DELETE FROM outreach_claims WHERE tenant=? AND id=?", (t, key))
            raise CampaignError(str(error)) from None
        except Exception:
            raise CampaignError("GHL no confirmó todas las plantillas; revisa los IDs parciales del historial antes de repetir") from None
        with self.engine.db() as db:
            current = self._read(db, "outreach_brand", t)
            if current != brand:
                raise CampaignError("La marca cambió durante la preparación; revisa las plantillas creadas")
            current.update(template_ids=ids, workflow_verified=False, unsubscribe_verified=False)
            db.execute("UPDATE outreach_brand SET payload=? WHERE tenant=?", (json.dumps(current), t))
            db.execute("UPDATE outreach_claims SET status='completed',payload=? WHERE tenant=? AND id=?",
                (json.dumps({"status":"completed", "template_ids":ids}), t, key))
        return {"template_ids": ids, "workflow_verified": False}

    def prepare(self, t, cid):
        with self.engine.db() as db:
            c = self._campaign(db, t, cid)
            self._draft(c)
            version = c["version"]
            profile = self.engine._profile(db, t)
        steps = len(c["schedule"])
        social_only = "social" in c["channels"]
        lib = self.library(t) if not social_only and steps == STEPS else None
        prepared = {}
        for recipient in c["recipients"]:
            ctx = evidence(profile, recipient["contact"], include_saved_context=steps == QUICK_STEPS)
            if social_only or steps != STEPS:
                ctx["campaign"] = {"recipients":len(c["recipients"]), "company":profile.get("name", "")}
            if social_only:
                ctx["networks"] = list(recipient["contact"]["social_profiles"])
            else:
                if lib:
                    ctx["library_hash"] = fingerprint(lib["emails"])
                if steps != STEPS:
                    ctx["steps"] = steps
            key = "recipient:" + fingerprint({"context": ctx, "recipient": recipient["key"]})
            result = self._generate(t, key, ctx, lib["emails"] if lib else None)
            sender = str(profile.get("name") or "nuestro equipo")[:60]
            company = str(recipient["contact"].get("company") or "vuestra empresa")[:60]
            offer = str(profile.get("offer") or "nuestros servicios")[:180]
            linkedin = {"invitation": f"Hola, equipo de {company}. Soy de {sender}. Me gustaría conectar para explorar una posible colaboración.",
                "message": f"Gracias por conectar. En {sender} ofrecemos {offer}. ¿Os encajaría conversar para valorar si podemos colaborar?",
                "followup": f"Hola, equipo de {company}. Retomo mi propuesta por si os interesa conocer {offer}. Si no encaja, lo dejamos aquí sin problema."}
            prepared[recipient["key"]] = {**result, "linkedin": linkedin, "context": ctx, "context_hash": fingerprint(ctx), "history": []}
        with self.engine.db() as db:
            current = self._campaign(db, t, cid)
            self._draft(current)
            if current["version"] != version:
                raise CampaignError("El borrador cambió; revisa el contenido antes de continuar")
            current["messages"] = prepared
            current["library"] = lib
            if current.get("auto_name"):
                generated_name = (lib or {}).get("name") or next(iter(prepared.values())).get("name")
                name = " ".join(str(generated_name or "").split())[:120]
                if name and "<" not in name and ">" not in name:
                    current["name"] = name
                current["auto_name"] = False
            self._invalidate(current)
            self._save(db, t, current)
            self._event(db, t, "batch_prepared", cid, count=len(prepared), simulated=not self.engine.real)
        return self.detail(t, cid)

    def edit(self, t, cid, recipient, emails):
        with self.engine.db() as db:
            c = self._campaign(db, t, cid)
            self._draft(c)
            message = c["messages"].get(recipient)
            if not message:
                raise CampaignError("Prepara primero los textos de este destinatario")
            validated = validate_emails({"emails": emails}, {s["id"] for s in message["context"]["sources"]}, len(message["emails"]))
            message.setdefault("history", []).append({"emails": message["emails"], "at": self.engine.now(db)})
            message["emails"] = validated
            self._invalidate(c)
            self._save(db, t, c)
            self._event(db, t, "messages_edited", cid, recipient=recipient, origin="manual")
        return self.detail(t, cid)

    def digest(self, c):
        return fingerprint({k: c[k] for k in ("version", "brand", "channels", "recipients", "messages", "library", "schedule")})

    def edit_linkedin(self, t, cid, recipient, texts):
        limits = {"invitation": 300, "message": 1500, "followup": 1500}
        if set(texts) != set(limits) or any(not isinstance(texts[k], str) or not texts[k].strip() or len(texts[k]) > limit for k, limit in limits.items()):
            raise CampaignError("Revisa los tres textos de LinkedIn y sus límites")
        with self.engine.db() as db:
            c = self._campaign(db, t, cid)
            self._draft(c)
            if recipient not in c["messages"]:
                raise CampaignError("Prepara primero los mensajes")
            c["messages"][recipient]["linkedin"] = {k: v.strip() for k, v in texts.items()}
            self._invalidate(c)
            self._save(db, t, c)
        return self.detail(t, cid)

    def _issues(self, db, t, c, *, live):
        issues = []
        current = {p["id"]: p for p in self._contacts(db, t)}
        for r in c["recipients"]:
            if c.get("provider_states", {}).get(r["key"], {}).get("status") in {"stopped", "completed"}:
                continue
            p = current.get(r["lead_id"], {})
            if not p.get("approved") or any(p.get("flags", {}).get(k) for k in ("opt_out", "responded", "meeting", "not_interested")):
                issues.append("Un lead dejó de estar aprobado o disponible")
            saved_context = c.get("messages", {}).get(r["key"], {}).get("context", {})
            current_context = evidence(self.engine._profile(db, t), p, include_saved_context=saved_context.get("uses_saved_context", False))
            if current_context != {k: v for k, v in saved_context.items() if k not in {"library_hash", "steps", "networks", "campaign"}}:
                issues.append("El contexto cambió; prepara y revisa de nuevo los textos")
            if "email" in c["channels"]:
                emails = p.get("email_options") or [{"email": p.get("email", "")}]
                if r["email"] not in {str(o.get("email", "")).strip().lower() for o in emails}:
                    issues.append("El email seleccionado ya no figura en las fuentes del lead")
                if live and not any(str(o.get("email", "")).strip().lower() == r["email"] and str(o.get("source_url", "")).startswith(("https://", "http://")) for o in emails):
                    issues.append("Falta la procedencia verificable del email seleccionado")
            if "linkedin" in c["channels"] and (not personal_linkedin(p.get("linkedin")) or p.get("linkedin") != r["contact"].get("linkedin")):
                issues.append("El perfil personal LinkedIn cambió o no está verificado")
            if db.execute("SELECT 1 FROM outreach_suppression WHERE tenant=? AND recipient=?", (t, r["key"])).fetchone():
                issues.append("Un destinatario está excluido")
            owners = [db.execute("SELECT campaign FROM outreach_reservations WHERE tenant=? AND recipient=?", (t, key)).fetchone() for key in r.get("reservation_keys", [r["key"]])]
            if any(owner and owner[0] != c["id"] for owner in owners):
                issues.append("Un destinatario ya participa en una campaña activa")
            message = c["messages"].get(r["key"])
            if "social" in c["channels"]:
                if not message or set(message.get("social", {})) != set(social_profiles(p)):
                    issues.append("Revisa los perfiles públicos y prepara sus mensajes")
            elif not message or len(message["emails"]) != len(c["schedule"]):
                issues.append(f"Faltan los {len(c['schedule'])} textos de un destinatario")
        if "email" in c["channels"]:
            b = c["brand"]
            for field in ("company", "logo_url", "sender_name", "sender_email", "signature", "calendar_url", "authorization_record"):
                if not b.get(field):
                    issues.append("Falta configuración administrativa: " + field)
            if not b.get("domain_verified"):
                issues.append("Remitente pendiente de comprobación administrativa")
            if live and (not b.get("workflow_verified") or not b.get("unsubscribe_verified") or len(b.get("template_ids", [])) != len(c["schedule"]) or not b.get("workflow_id") or not b.get("location_id")):
                issues.append(f"Falta verificar el flujo GHL, sus {len(c['schedule'])} plantillas y la baja")
            if live and self.provider is None:
                issues.append("La conexión de envío está pendiente del administrador")
            if live and not b.get("logo_url", "").startswith("https://"):
                issues.append("El logo debe estar publicado para el envío real")
        return list(dict.fromkeys(issues))

    def approve(self, t, cid, digest, actor):
        with self.engine.db() as db:
            c = self._campaign(db, t, cid)
            self._draft(c)
            if digest != self.digest(c):
                raise CampaignError("El lote cambió; vuelve a revisar la vista previa")
            issues = self._issues(db, t, c, live=False)
            if issues:
                raise CampaignError("; ".join(issues))
            c["approval"] = {"digest": digest, "actor": actor, "at": self.engine.now(db)}
            self._save(db, t, c)
            self._event(db, t, "batch_approved", cid, actor=actor)
        return self.detail(t, cid)

    def _claim_provider(self, db, t, c, action):
        key = f"provider:{c['id']}:{action}:{c.get('operation_version', 0)}"
        prior = self._read(db, "outreach_claims", t, key)
        if prior:
            raise CampaignError("Operación ya iniciada; concilia el estado antes de repetir")
        db.execute("INSERT INTO outreach_claims VALUES(?,?,?,?)", (t, key, "started", json.dumps({"status": "started", "campaign": c["id"], "action": action})))
        return key

    def action(self, t, cid, action, *, live=False):
        if action == "duplicate":
            with self.engine.db() as db:
                old = self._campaign(db, t, cid)
            return self.create(t, {"name": old["name"] + " · copia", "channels": old["channels"], "recipients": old["recipients"],
                                   "steps": len(old["schedule"]) or QUICK_STEPS}, duplicate_from=old)
        with self.engine.db() as db:
            c = self._campaign(db, t, cid)
            if action == "activate":
                self._draft(c)
                if "social" in c["channels"]:
                    raise CampaignError("Copia el mensaje y envíalo desde la red elegida")
                if datetime.fromisoformat(c["schedule"][0]).timestamp() <= self.engine.now(db):
                    raise CampaignError("Las fechas caducaron; actualiza el calendario y vuelve a aprobar")
                if not c.get("approval") or c["approval"]["digest"] != self.digest(c):
                    raise CampaignError("Revisa y aprueba el lote antes de activarlo")
                if self.engine.real and "email" in c["channels"] and not live:
                    raise CampaignError("El ensayo de envío real requiere habilitación administrativa explícita")
                issues = self._issues(db, t, c, live=live)
                if issues:
                    raise CampaignError("; ".join(issues))
                for r in c["recipients"]:
                    for key in r.get("reservation_keys", [r["key"]]):
                        db.execute("INSERT INTO outreach_reservations VALUES(?,?,?) ON CONFLICT(tenant,recipient) DO UPDATE SET campaign=excluded.campaign WHERE campaign=excluded.campaign", (t, key, cid))
                c["delivery_mode"] = "live" if live else "simulated"
                c["state"] = "activating" if live and "email" in c["channels"] else "active"
            elif action in {"pause", "resume", "archive"}:
                allowed = {"pause": {"active", "activation_uncertain"}, "resume": {"paused"}, "archive": {"draft", "paused", "completed"}}
                if c["state"] not in allowed[action]:
                    raise CampaignError("Transición de campaña no permitida")
                if action == "resume":
                    issues = self._issues(db, t, c, live=c.get("delivery_mode") == "live")
                    if issues:
                        raise CampaignError("; ".join(issues))
                live = c.get("delivery_mode") == "live" and "email" in c["channels"]
                c["state"] = {"pause": "pause_pending" if live else "paused", "resume": "resuming" if live else "active", "archive": "archived"}[action]
            else:
                raise CampaignError("Acción no permitida")
            c["operation_version"] = c.get("operation_version", 0) + 1
            claim = self._claim_provider(db, t, c, action) if live and "email" in c["channels"] and action != "archive" else None
            self._save(db, t, c)
            self._event(db, t, "campaign_" + action + "_requested", cid)
            if not claim:
                if action == "activate" and "linkedin" in c["channels"]:
                    self._create_linkedin_tasks(db, t, c)
                if action == "archive":
                    db.execute("DELETE FROM outreach_reservations WHERE tenant=? AND campaign=?", (t, cid))
                return self.detail(t, cid, db=db)
        try:
            result = self.provider.transition(t, c, action)
        except Exception:
            with self.engine.db() as db:
                c = self._campaign(db, t, cid)
                c["state"] = "pause_pending" if action == "pause" else "activation_uncertain"
                self._save(db, t, c)
                db.execute("UPDATE outreach_claims SET status='review_required' WHERE tenant=? AND id=?", (t, claim))
            raise CampaignError("GHL no confirmó la operación; revisión requerida, sin repetir automáticamente") from None
        with self.engine.db() as db:
            c = self._campaign(db, t, cid)
            c["provider_states"] = result
            c["state"] = "paused" if action == "pause" else "active"
            db.execute("UPDATE outreach_claims SET status='completed' WHERE tenant=? AND id=?", (t, claim))
            if action == "activate" and "linkedin" in c["channels"]:
                self._create_linkedin_tasks(db, t, c)
            self._save(db, t, c)
        return self.detail(t, cid)

    def _create_linkedin_tasks(self, db, t, c):
        for r in c["recipients"]:
            task_id = f"{c['id']}:{r['lead_id']}:invitation"
            db.execute("INSERT OR IGNORE INTO outreach_tasks VALUES(?,?,?,?,?,?,?,?)", (t, task_id, c["id"], r["key"], "invitation", self.engine.now(db), "pending", json.dumps({"linkedin": r["contact"]["linkedin"], "message": c["messages"][r["key"]]["linkedin"]["invitation"]})))

    def linkedin_event(self, t, cid, recipient, event, actor):
        if event not in {"invitation_sent", "connection_accepted", "message_sent", "followup_sent", *STOP_EVENTS}:
            raise CampaignError("Hecho manual no permitido")
        with self.engine.db() as db:
            c = self._campaign(db, t, cid)
            if c["state"] != "active" or "linkedin" not in c["channels"]:
                raise CampaignError("La campaña LinkedIn debe estar activa")
            r = next((r for r in c["recipients"] if r["key"] == recipient), None)
            if not r:
                raise CampaignError("Destinatario fuera de campaña")
            if event in STOP_EVENTS:
                self._stop(db, t, recipient, event)
            else:
                kind, next_kind = {"invitation_sent": ("invitation", None), "connection_accepted": (None, "message"), "message_sent": ("message", "followup"), "followup_sent": ("followup", None)}[event]
                if kind:
                    task = db.execute("SELECT id,state,due FROM outreach_tasks WHERE tenant=? AND campaign=? AND recipient=? AND kind=?", (t, cid, recipient, kind)).fetchone()
                    if not task or task["due"] > self.engine.now(db):
                        raise CampaignError("Todavía no existe una tarea disponible para confirmar")
                    if task["state"] == "completed":
                        return self.detail(t, cid, db=db)
                    if task["state"] != "pending":
                        raise CampaignError("La tarea está detenida")
                    db.execute("UPDATE outreach_tasks SET state='completed' WHERE tenant=? AND id=?", (t, task["id"]))
                if event == "connection_accepted":
                    prior = db.execute("SELECT 1 FROM outreach_tasks WHERE tenant=? AND campaign=? AND recipient=? AND kind='invitation' AND state='completed'", (t, cid, recipient)).fetchone()
                    if not prior:
                        raise CampaignError("Confirma primero la invitación realizada")
                if next_kind:
                    due = self.engine.now(db) + (3 * 86400 if next_kind == "followup" else 0)
                    key = f"{cid}:{r['lead_id']}:{next_kind}"
                    text = c["messages"][recipient]["linkedin"][next_kind]
                    db.execute("INSERT OR IGNORE INTO outreach_tasks VALUES(?,?,?,?,?,?,?,?)", (t, key, cid, recipient, next_kind, due, "pending", json.dumps({"linkedin": r["contact"]["linkedin"], "message": text})))
            self._event(db, t, event, cid, recipient=recipient, origin="manual", actor=actor)
        if event in STOP_EVENTS:
            self.stop_remote(t, cid, recipient)
        return self.detail(t, cid)

    def stop_remote(self, t, cid, recipient):
        """One durable cancellation attempt; uncertainty remains visible."""
        with self.engine.db() as db:
            c = self._campaign(db, t, cid)
            if c.get("delivery_mode") != "live" or "email" not in c["channels"]:
                return
            remote = c["provider_states"].get(recipient, {})
            key = f"stop:{cid}:{recipient}"
            if self._read(db, "outreach_claims", t, key):
                return
            remote["status"] = "stop_pending"
            c["provider_states"][recipient] = remote
            self._save(db, t, c)
            db.execute("INSERT INTO outreach_claims VALUES(?,?,?,?)", (t, key, "started", json.dumps({"status":"started"})))
        try:
            self.provider.stop(t, c, remote)
        except Exception:
            with self.engine.db() as db:
                self._event(db, t, "stop_pending_review", cid, recipient=recipient)
            return
        with self.engine.db() as db:
            c = self._campaign(db, t, cid)
            c["provider_states"][recipient]["status"] = "stopped"
            db.execute("UPDATE outreach_claims SET status='completed' WHERE tenant=? AND id=?", (t, key))
            self._save(db, t, c)
            self._event(db, t, "stop_confirmed", cid, recipient=recipient, origin="ghl")

    def _stop(self, db, t, recipient, reason):
        db.execute("INSERT INTO outreach_suppression VALUES(?,?,?) ON CONFLICT(tenant,recipient) DO UPDATE SET reason=excluded.reason", (t, recipient, reason))
        db.execute("UPDATE outreach_tasks SET state='cancelled' WHERE tenant=? AND recipient=? AND state='pending'", (t, recipient))
        # Reserve the whole identity, even if a later campaign chooses its other channel.
        for row in db.execute("SELECT payload FROM outreach_campaigns WHERE tenant=?", (t,)).fetchall():
            for r in json.loads(row[0])["recipients"]:
                if r["key"] == recipient:
                    for key in r.get("reservation_keys", [recipient]):
                        db.execute("INSERT OR IGNORE INTO outreach_suppression VALUES(?,?,?)", (t, key, reason))

    def provider_event(self, t, event_id, location_id, contact_id, kind, *, message_id="", step=None, workflow_id=None):
        if kind not in {"accepted", "delivered", "response", "opt_out", "bounce", "meeting"} or not event_id:
            raise CampaignError("Evento del proveedor no admitido")
        brand = self.brand(t)
        if location_id != brand.get("location_id"):
            raise CampaignError("Evento de otra subcuenta")
        with self.engine.db() as db:
            if kind in {"accepted", "delivered"} and not message_id:
                raise CampaignError("Falta el identificador del mensaje del proveedor")
            semantic = "ghl-message:" + fingerprint([location_id, contact_id, message_id, kind]) if message_id else "ghl:" + event_id
            prior = self._read(db, "outreach_events", t, semantic)
            if prior:
                return {"ok": True, "deduplicated": True}
            affected = []
            for row in db.execute("SELECT payload FROM outreach_campaigns WHERE tenant=?", (t,)):
                c = json.loads(row[0])
                if c["state"] in {"draft", "archived"}:
                    continue
                if kind in {"accepted", "delivered"} and workflow_id != c["brand"].get("workflow_id"):
                    continue
                for recipient, remote in c.get("provider_states", {}).items():
                    if remote.get("contact_id") != contact_id:
                        continue
                    known = remote.get("message_ids", {})
                    if c["state"] == "completed" and (kind not in {"accepted", "delivered"} or message_id not in known.get("accepted", []) + known.get("delivered", [])):
                        continue
                    if kind in STOP_EVENTS:
                        self._stop(db, t, recipient, kind)
                        remote["stop_reason"] = kind
                        if remote.get("status") != "stopped":
                            remote["status"] = "stop_requested"
                    else:
                        remote.setdefault("message_ids", {}).setdefault(kind, [])
                        if message_id not in remote["message_ids"][kind]:
                            remote["message_ids"][kind].append(message_id)
                        if len(remote["message_ids"].get("accepted", [])) >= len(c["schedule"]):
                            remote["status"] = "completed"
                    affected.append({"campaign": c["id"], "recipient": recipient})
                if affected and any(a["campaign"] == c["id"] for a in affected):
                    self._save(db, t, c)
            if not affected:
                return {"ok": True, "ignored": True}
            payload = {"kind": kind, "origin": "ghl", "message_id": message_id, "step": step, "affected": affected, "at": self.engine.now(db)}
            db.execute("INSERT INTO outreach_events VALUES(?,?,?)", (t, semantic, json.dumps(payload)))
        if kind in STOP_EVENTS:
            for a in affected:
                self.stop_remote(t, a["campaign"], a["recipient"])
        return {"ok": True}

    def checkpoint(self, t, cid, recipient, state):
        with self.engine.db() as db:
            if cid == "templates":
                self._event(db, t, "template_prepared", step=recipient, **state)
                return
            c = self._campaign(db, t, cid)
            c["provider_states"][recipient] = state
            self._save(db, t, c)
            self._event(db, t, "provider_checkpoint", cid, recipient=recipient, status=state.get("status"))

    def detail(self, t, cid, db=None):
        if db is None:
            with self.engine.db() as db:
                return self.detail(t, cid, db=db)
        c = self._campaign(db, t, cid)
        events = [json.loads(r[0]) for r in db.execute("SELECT payload FROM outreach_events WHERE tenant=? ORDER BY rowid DESC", (t,)) if (value := json.loads(r[0])).get("campaign") == cid or any(a["campaign"] == cid for a in value.get("affected", []))]
        tasks = [{**dict(r), "payload": json.loads(r["payload"])} for r in db.execute("SELECT * FROM outreach_tasks WHERE tenant=? AND campaign=?", (t, cid))]
        issues = self._issues(db, t, c, live=self.engine.real)
        return {"campaign": c, "digest": self.digest(c), "issues": issues, "tasks": tasks, "events": events,
            "mode": "real_isolated" if self.engine.real else "local_demo", "clock": self.engine.now(db),
            "metrics": {"recipients": len(c["recipients"]), "prepared": sum(len(m["emails"]) for m in c["messages"].values()),
                "sent_confirmed": sum(e["kind"] == "accepted" and e.get("origin") == "ghl" for e in events),
                "delivered": sum(e["kind"] == "delivered" for e in events), "responses": sum(e["kind"] == "response" for e in events),
                "tasks_today": sum(x["state"] == "pending" and x["due"] <= self.engine.now(db) for x in tasks)}}

    def preview(self, t, cid, recipient, step):
        d = self.detail(t, cid)
        m = d["campaign"]["messages"].get(recipient)
        if not m or not 1 <= step <= len(m["emails"]):
            raise CampaignError("Prepara primero el correo seleccionado")
        return render_email(m["emails"][step - 1], d["campaign"]["brand"], preview=True)

    def preview_library(self, t, version, step):
        with self.engine.db() as db:
            self.engine._profile(db, t)
            row = db.execute("SELECT payload FROM outreach_libraries WHERE tenant=? AND version=?", (t, version)).fetchone()
        emails = json.loads(row[0])["emails"] if row else []
        if not 1 <= step <= len(emails):
            raise CampaignError("No se encontró la plantilla solicitada")
        return render_email(emails[step - 1], self.brand(t), preview=True)
