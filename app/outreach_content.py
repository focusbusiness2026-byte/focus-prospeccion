"""Versioned, evidence-bound email content and deterministic email-safe HTML."""
from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import re
from datetime import datetime, time, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from app.intelligence import CampaignError

STEPS = 15
QUICK_STEPS = 5
SOCIAL_NETWORKS = ("linkedin", "instagram", "facebook")
ZONE = ZoneInfo("Europe/Madrid")
STOP_EVENTS = {"response", "positive_response", "opt_out", "bounce", "meeting", "not_interested"}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def public_url(value, *, optional=False):
    value = str(value or "").strip()
    if not value and optional:
        return ""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.fragment:
            raise ValueError()
        if host.lower() in {"localhost", "metadata.google.internal"} or host.endswith((".local", ".internal")) or "." not in host:
            raise ValueError()
        try:
            if not ipaddress.ip_address(host).is_global:
                raise ValueError()
        except ValueError as exc:
            if re.fullmatch(r"[\d.:]+", host):
                raise ValueError() from exc
        if parsed.port not in (None, 443):
            raise ValueError()
    except ValueError:
        raise CampaignError("Indica una URL pública HTTPS válida") from None
    return value


def personal_linkedin(value):
    try:
        p = urlsplit(str(value or ""))
        return p.scheme == "https" and p.hostname in {"linkedin.com", "www.linkedin.com"} and not p.username and not p.password and p.port in (None, 443) and bool(re.fullmatch(r"/in/[A-Za-z0-9%_-]+/?", p.path)) and not p.query and not p.fragment
    except ValueError:
        return False


def valid_email(value):
    return bool(re.fullmatch(r"[^\s<>@\x00-\x1f]+@[^\s<>@.]+(?:\.[^\s<>@.]+)+", str(value or "")))


def social_profiles(contact):
    profiles = {}
    for network in SOCIAL_NETWORKS:
        url = contact.get(network) or (contact.get("social_links") or {}).get(network) or ""
        try:
            parsed = urlsplit(public_url(url))
            host = parsed.hostname.removeprefix("www.").removeprefix("m.")
            if host == network + ".com" and parsed.path.strip("/") and parsed.port in (None, 443):
                profiles[network] = url
        except (ValueError, CampaignError):
            pass
    return profiles


def dates(now, steps=STEPS):
    local = datetime.fromtimestamp(now, timezone.utc).astimezone(ZONE)
    first = datetime.combine(local.date(), time(10), ZONE)
    if local >= first:
        first += timedelta(days=1)
    return [datetime.combine(first.date() + timedelta(days=n), time(10), ZONE).isoformat() for n in range(steps)]


def evidence(profile, contact=None):
    """Only explicit fields; no private intake, credentials, or speculative names."""
    result = {"sender": {k: profile.get(k, "") for k in ("name", "offer", "website", "services", "description")}}
    result["sources"] = [{"id": "sender:declared", "kind": "declared", "facts": result["sender"]}]
    if contact:
        result["recipient"] = {k: contact.get(k, "") for k in ("company", "sector", "city", "website")}
        if contact.get("name_status") == "current_with_source" and contact.get("person_source_url"):
            result["recipient"].update({k: contact.get(k, "") for k in ("name", "role", "person_source_url")})
        result["sources"].append({"id": "recipient:research", "kind": "research", "facts": result["recipient"]})
        result["sources"][-1]["urls"] = list(dict.fromkeys(str(x.get("source_url")) for x in contact.get("email_options", []) if x.get("source_url")))
    return result


def validate_emails(payload, allowed_sources, steps=STEPS):
    rows = payload.get("emails") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) != steps:
        raise CampaignError(f"La secuencia debe contener exactamente {steps} correos")
    seen = set()
    clean = []
    for n, row in enumerate(rows, 1):
        if not isinstance(row, dict) or type(row.get("step")) is not int or row.get("step") != n:
            raise CampaignError(f"Los {steps} pasos deben estar completos y ordenados")
        item = {"step": n}
        for key, limit in (("subject", 180), ("preheader", 250), ("objective", 300), ("body", 3500)):
            value = str(row.get(key, "")).strip()
            if not value or len(value) > limit or re.search(r"[<>\x00-\x08\x0b\x0c\x0e-\x1f]|\{\{", value):
                raise CampaignError("Revisa el texto: hay campos vacíos, demasiado largos o contenido no permitido")
            item[key] = value
        if item["subject"].casefold() in seen:
            raise CampaignError(f"Los {steps} asuntos deben ser diferentes")
        seen.add(item["subject"].casefold())
        sources = row.get("source_ids", [])
        if not isinstance(sources, list) or not sources or any(s not in allowed_sources for s in sources):
            raise CampaignError("Falta trazabilidad de las fuentes del correo")
        item["source_ids"] = list(dict.fromkeys(sources))
        clean.append(item)
    return clean


def validate_brand(data):
    allowed = {"logo_url", "color", "company", "sender_name", "sender_email", "signature", "calendar_url", "reply_to", "domain_verified", "authorization_record", "location_id", "workflow_id", "workflow_verified", "template_ids", "unsubscribe_verified"}
    if set(data) - allowed:
        raise CampaignError("La configuración contiene campos no permitidos")
    result = dict(data)
    for k in ("logo_url", "calendar_url"):
        if result.get(k) and not (k == "logo_url" and result[k] == "/static/three-dots-logo.svg"):
            result[k] = public_url(result[k])
    if result.get("color") and not re.fullmatch(r"#[0-9a-fA-F]{6}", result["color"]):
        raise CampaignError("Color hexadecimal inválido")
    for k in ("sender_email", "reply_to"):
        if result.get(k) and not valid_email(result[k]):
            raise CampaignError("Correo remitente inválido")
    for k in ("company", "sender_name", "signature", "authorization_record", "location_id", "workflow_id"):
        if k in result:
            result[k] = str(result[k]).strip()[:1000]
    for k in ("domain_verified", "workflow_verified", "unsubscribe_verified"):
        if k in result and type(result[k]) is not bool:
            raise CampaignError("La comprobación administrativa debe ser explícita")
    if "template_ids" in result and (not isinstance(result["template_ids"], list) or any(not isinstance(x,str) or not x.strip() for x in result["template_ids"]) or len(result["template_ids"]) not in (0, QUICK_STEPS, STEPS) or len(set(result["template_ids"])) != len(result["template_ids"])):
        raise CampaignError("Se requieren los IDs de cada plantilla, sin duplicados")
    return result


def render_email(email, brand, *, unsubscribe_url="{{unsubscribe_link}}", preview=False):
    esc = lambda s: html.escape(str(s or ""), quote=True)
    color = brand.get("color") or "#d9af3c"
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        color = "#d9af3c"
    logo = brand.get("logo_url", "")
    if logo and logo != "/static/three-dots-logo.svg":
        logo = public_url(logo)
    agenda = public_url(brand["calendar_url"]) if brand.get("calendar_url") else ""
    if unsubscribe_url != "{{unsubscribe_link}}":
        public_url(unsubscribe_url)
    unsub = "#baja-solo-vista-previa" if preview else unsubscribe_url
    paragraphs = "".join(f'<p style="margin:0 0 18px;line-height:1.65">{esc(p)}</p>' for p in email["body"].split("\n\n"))
    image = f'<img src="{esc(logo)}" width="160" alt="{esc(brand.get("company"))}" style="max-width:160px;height:auto;border:0;margin-bottom:24px">' if logo else f'<h2>{esc(brand.get("company", "Marca pendiente"))}</h2>'
    button = f'<a href="{esc(agenda)}" style="display:inline-block;background:{color};color:#111827;padding:14px 24px;text-decoration:none;border-radius:6px;font-weight:bold">Agendar una llamada</a>' if agenda else '<p>Enlace de agenda pendiente de configurar.</p>'
    body = f'''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(email['subject'])}</title></head><body style="margin:0;background:#f3f4f6;color:#202938;font:16px Arial,sans-serif"><div style="display:none;max-height:0;overflow:hidden">{esc(email['preheader'])}</div><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:24px 12px"><table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:100%;max-width:600px;background:#fff;border-top:4px solid {color}"><tr><td style="padding:30px 24px">{image}{paragraphs}{button}<p style="margin-top:28px;font-size:14px">{esc(brand.get('signature') or brand.get('sender_name'))}</p><hr style="border:0;border-top:1px solid #ddd"><p style="font-size:12px;color:#58616e">{esc(brand.get('company'))} · <a href="{esc(unsub)}">Dejar de recibir estos correos</a></p></td></tr></table></td></tr></table></body></html>'''
    plain = f"{email['body']}\n\nAgendar una llamada: {agenda or '(pendiente)'}\n\n{brand.get('signature', '')}\nBaja: {unsub}"
    return {"html": body, "text": plain, "subject": email["subject"]}


class DemoSequenceGenerator:
    """Explicit local fixture. Never used as a fallback for a real provider."""
    def __call__(self, context, library=None):
        sender = context["sender"]
        recipient = context.get("recipient", {})
        group = context.get("campaign", {}).get("recipients", 1)
        title = f"{sender.get('offer') or 'Colaboración'} · " + (f"{group} empresas" if group > 1 else recipient.get('company') or sender.get('name') or 'Contacto')
        if context.get("networks"):
            company = recipient.get("company") or "vuestro equipo"
            offer = sender.get("offer") or "nuestros servicios"
            greetings = {"linkedin":"¿Os encajaría conectar y hablar sobre una colaboración?",
                         "instagram":"¿Os puedo compartir una idea en un mensaje?",
                         "facebook":"¿Con quién podría comentar una posible colaboración?"}
            return {"name": title, "social": {
                channel:f"Hola, equipo de {company}. En {sender.get('name')} ofrecemos {offer}. {greetings[channel]}"
                for channel in context["networks"]}, "model":"fixture-local-no-openai", "simulated":True}
        topics = ["Presentación", "Cómo trabajamos", "Explorar un formato", "Preparar el contenido", "Dar voz al equipo", "Compartir conocimiento", "Organizar la producción", "Conversaciones útiles", "Elegir un enfoque", "Revisar el formato", "Preparar una sesión", "Valorar una colaboración", "Resolver dudas", "Proponer una conversación", "Cerrar el seguimiento"]
        return {"name": title,
            "emails": [{"step": n, "subject": f"{topic} · {sender.get('offer', 'Servicios')}", "preheader": f"Una propuesta de {sender.get('name', 'nuestro equipo')}", "objective": topic,
            "body": f"Hola, equipo de {recipient.get('company', 'vuestra empresa')}.\n\nEn {sender.get('name')} ofrecemos {sender.get('offer')}. ¿Os encajaría conversar para valorar una posible colaboración?\n\nEste es el paso {n}: {topic.lower()}. Sin asumir necesidades ni resultados.",
            "source_ids": [s["id"] for s in context["sources"]]} for n, topic in enumerate(topics[:context.get("steps", STEPS)], 1)], "model": "fixture-local-no-openai", "usage": {"input_tokens": 0, "output_tokens": 0}, "simulated": True}
