from __future__ import annotations

import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from app.config import Settings, get_settings
from app.scoring import score_company


SOCIAL_HOSTS = {
    "linkedin": ("linkedin.com",),
    "instagram": ("instagram.com",),
    "facebook": ("facebook.com", "fb.com"),
    "x": ("x.com", "twitter.com"),
    "youtube": ("youtube.com", "youtu.be"),
    "tiktok": ("tiktok.com",),
}

DOSSIER_SECTIONS = (
    "identification", "activity", "size", "finances", "buying_signals", "decision_makers", "verdict"
)


def _public_signal_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "type": {
                "type": "string",
                "enum": ["revenue", "valuation", "funding", "advertising", "public_procurement", "other"],
            },
            "value": {"type": "string"},
            "date": {"type": ["string", "null"]},
            "source_url": {"type": "string"},
            "evidence_class": {
                "type": "string",
                "enum": ["company_declared", "public_secondary", "estimate_unverified"],
            },
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        },
        "required": ["type", "value", "date", "source_url", "evidence_class", "confidence"],
    }


@dataclass(frozen=True)
class WebEvidence:
    title: str
    description: str
    text: str
    pages: list[str]
    social_links: dict[str, str]
    public_contacts: list[dict[str, str]]


def _public_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("La web debe usar http o https")
    host = parsed.hostname.lower()
    if host in {"localhost", "localhost.localdomain"}:
        raise ValueError("No se permiten direcciones locales")
    try:
        default_port = 443 if parsed.scheme == "https" else 80
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or default_port, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError("No se pudo resolver el dominio") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("No se permiten redes privadas o reservadas")
    return parsed.geturl()


class PublicWebScraper:
    user_agent = "FocusBusinessProspeccion/1.0 (+public-web-research)"

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def _fetch(self, client: httpx.Client, url: str) -> httpx.Response:
        current = _public_url(url)
        for _ in range(4):
            response = client.get(current, headers={"User-Agent": self.user_agent}, timeout=15)
            if response.status_code not in {301, 302, 303, 307, 308}:
                response.raise_for_status()
                if "text/html" not in response.headers.get("content-type", ""):
                    raise ValueError("La URL no devuelve una pagina HTML")
                if len(response.content) > 2_000_000:
                    raise ValueError("La pagina supera el limite de 2 MB")
                return response
            location = response.headers.get("location")
            if not location:
                break
            current = _public_url(urljoin(current, location))
        raise ValueError("Demasiadas redirecciones")

    def _robots_allows(self, client: httpx.Client, url: str) -> bool:
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        try:
            response = client.get(robots_url, headers={"User-Agent": self.user_agent}, timeout=8)
            if response.status_code >= 400:
                return True
            parser = RobotFileParser()
            parser.set_url(robots_url)
            parser.parse(response.text.splitlines())
            return parser.can_fetch(self.user_agent, url)
        except httpx.HTTPError:
            return True

    @staticmethod
    def _social_links(soup: BeautifulSoup, base_url: str) -> dict[str, str]:
        found: dict[str, str] = {}
        for anchor in soup.select("a[href]"):
            href = urljoin(base_url, anchor.get("href", ""))
            host = (urlparse(href).hostname or "").lower().removeprefix("www.")
            for network, domains in SOCIAL_HOSTS.items():
                if network not in found and any(host == domain or host.endswith(f".{domain}") for domain in domains):
                    found[network] = href.split("#", 1)[0]
        return found

    @staticmethod
    def _public_contacts(soup: BeautifulSoup, source_url: str) -> list[dict[str, str]]:
        found: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for anchor in soup.select("a[href]"):
            href = str(anchor.get("href") or "").strip()
            kind = ""
            value = ""
            if href.lower().startswith("mailto:"):
                kind = "email"
                value = href[7:].split("?", 1)[0].strip()
                if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
                    continue
            elif href.lower().startswith("tel:"):
                kind = "phone"
                value = re.sub(r"[^\d+().\-\s]", "", href[4:]).strip()
                if len(re.sub(r"\D", "", value)) < 7:
                    continue
            if kind and value and (kind, value.lower()) not in seen:
                seen.add((kind, value.lower()))
                found.append({"type": kind, "value": value, "source_url": source_url})
        return found

    def scrape(self, website: str) -> WebEvidence:
        website = _public_url(website)
        origin_host = (urlparse(website).hostname or "").lower()
        pages: list[str] = []
        texts: list[str] = []
        social_links: dict[str, str] = {}
        public_contacts: list[dict[str, str]] = []
        seen_contacts: set[tuple[str, str]] = set()
        title = ""
        description = ""
        with httpx.Client(follow_redirects=False) as client:
            if not self._robots_allows(client, website):
                raise PermissionError("robots.txt no permite consultar esta pagina")
            queue = [website]
            visited: set[str] = set()
            while queue and len(pages) < max(1, self.settings.web_scraper_max_pages):
                url = queue.pop(0)
                if url in visited:
                    continue
                visited.add(url)
                if not self._robots_allows(client, url):
                    continue
                response = self._fetch(client, url)
                final_url = str(response.url)
                soup = BeautifulSoup(response.text, "html.parser")
                for node in soup(["script", "style", "noscript", "svg"]):
                    node.decompose()
                if not title:
                    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
                    meta = soup.select_one('meta[name="description"], meta[property="og:description"]')
                    description = (meta.get("content") or "").strip() if meta else ""
                visible = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:18_000]
                texts.append(f"FUENTE {final_url}\n{visible}")
                pages.append(final_url)
                social_links.update({k: v for k, v in self._social_links(soup, final_url).items() if k not in social_links})
                for contact in self._public_contacts(soup, final_url):
                    key = (contact["type"], contact["value"].lower())
                    if key not in seen_contacts:
                        seen_contacts.add(key)
                        public_contacts.append(contact)
                for anchor in soup.select("a[href]"):
                    candidate = urljoin(final_url, anchor.get("href", "")).split("#", 1)[0]
                    parsed = urlparse(candidate)
                    label = (anchor.get_text(" ", strip=True) + " " + parsed.path).lower()
                    if parsed.scheme in {"http", "https"} and parsed.hostname == origin_host and re.search(
                        r"about|empresa|nosotros|contact|servicios|services", label
                    ):
                        if candidate not in visited and candidate not in queue:
                            queue.append(candidate)
        return WebEvidence(title, description, "\n\n".join(texts)[:55_000], pages, social_links, public_contacts)


class OpenAIWebResearcher:
    endpoint = "https://api.openai.com/v1/responses"

    def __init__(self, settings: Settings | None = None, response_provider=None, max_search_calls: int | None = None):
        self.settings = settings or get_settings()
        self.response_provider = response_provider
        self.max_search_calls = max_search_calls or self.settings.web_search_call_limit

    @staticmethod
    def _schema() -> dict:
        nullable_string = {"type": ["string", "null"]}
        dossier_fact = {
            "type": "object", "additionalProperties": False,
            "properties": {
                "text": {"type": "string"},
                "date": nullable_string,
                "source_url": nullable_string,
                "certainty": {"type": "string", "enum": [
                    "confirmed", "historical", "estimate", "not_available", "recommendation"
                ]},
            },
            "required": ["text", "date", "source_url", "certainty"],
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "sector": nullable_string,
                "business_model": nullable_string,
                "city": nullable_string,
                "employees": {"type": ["integer", "null"]},
                "employee_trend": {"type": ["string", "null"], "enum": ["growing", "stable", "shrinking", None]},
                "revenue_eur": {"type": ["number", "null"]},
                "revenue_trend": {"type": ["string", "null"], "enum": ["growing", "stable", "shrinking", None]},
                "capital_event": {"type": "boolean"},
                "financial_alert": {"type": "boolean"},
                "legal_status": {"type": "string", "enum": ["active", "inactive", "absorbed", "extinct", "unknown"]},
                "legal_status_source_url": nullable_string,
                "buying_signals": {"type": "array", "items": {"type": "string"}},
                "decision_access": {"type": ["string", "null"], "enum": ["active", "passive", "registry_only", None]},
                "decision_recent": {"type": "boolean"},
                "disqualifiers": {"type": "array", "items": {"type": "string"}},
                "summary": {"type": "string"},
                "entry_angle": {"type": "string"},
                "prospect_found": {"type": "boolean"},
                "no_prospect_reason": nullable_string,
                "no_contacts_reason": nullable_string,
                "public_contacts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "type": {"type": "string", "enum": ["email", "phone", "contact_page", "person"]},
                            "value": {"type": "string"},
                            "source_url": {"type": "string"},
                        },
                        "required": ["type", "value", "source_url"],
                    },
                },
                "public_signals": {"type": "array", "items": _public_signal_schema()},
                "decision_makers": {
                    "type": "array",
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "role_status": {"type": "string", "enum": ["current", "historical", "unverified"]}, "public_contact": nullable_string, "source_url": {"type": "string"}},
                        "required": ["name", "role", "role_status", "public_contact", "source_url"],
                    },
                },
                "social_links": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {key: nullable_string for key in SOCIAL_HOSTS},
                    "required": list(SOCIAL_HOSTS),
                },
                "dossier": {
                    "type": "object", "additionalProperties": False,
                    "properties": {section: {"type": "array", "items": dossier_fact} for section in DOSSIER_SECTIONS},
                    "required": list(DOSSIER_SECTIONS),
                },
            },
            "required": [
                "sector", "business_model", "city", "employees", "employee_trend", "revenue_eur",
                "revenue_trend", "capital_event", "financial_alert", "legal_status", "legal_status_source_url", "buying_signals", "decision_access",
                "decision_recent", "disqualifiers", "summary", "entry_angle", "prospect_found",
                "no_prospect_reason", "no_contacts_reason", "public_contacts", "public_signals", "decision_makers", "social_links", "dossier",
            ],
        }

    def build_payload(self, company: str, evidence: WebEvidence, prospecting_profile: dict | None = None) -> dict:
        profile = prospecting_profile or {}
        prompt = f"""
Investiga la empresa indicada para Focus Business usando solo información empresarial pública y verificable.
Puedes realizar como máximo {self.max_search_calls} acciones de búsqueda web. No intentes iniciar sesión,
evadir CAPTCHA, consultar áreas privadas ni afirmar cobertura absoluta. Prioriza la web corporativa y fuentes primarias.
No inventes empleados, facturación, contactos, decisores, redes, señales ni prospectos. Usa null o listas vacías cuando
no haya evidencia. Cada contacto público debe incluir la URL exacta donde fue publicado. Indica de forma explícita si
se encontró un prospecto/resultado y el motivo cuando no se encuentre. La salida debe cumplir el esquema JSON.
Comprueba primero si la sociedad sigue operativa. Si fue absorbida, extinguida o está inactiva, marca
prospect_found=false, añade el descalificador y explica qué empresa continúa la actividad, con fuente y fecha.
No presentes a un presidente o directivo histórico como decisor actual. Marca role_status=current solo si una
fuente específica respalda el cargo actual; usa historical/unverified en caso contrario. Indica la URL exacta
del estado registral en legal_status_source_url. Una web genérica no acredita el cargo de una persona.
Para una productora, busca señales concretas de necesidad audiovisual (campañas, lanzamientos, ferias,
rebranding o producción de contenido), con fecha y fuente; distingue señal observada de recomendación comercial.
Redacta dossier como siete apartados de una ficha editorial extensa y legible: identidad y continuidad legal,
actividad/modelo, tamaño, finanzas, señales de compra, personas decisoras y veredicto comercial. En cada apartado
aporta datos concretos y contexto suficiente para una lectura semejante a un artículo; cada afirmación factual
debe citar su página exacta y fecha si está disponible. Separa lo confirmado, histórico, estimado y no disponible.
No rellenes apartados con texto genérico ni inventes datos para alcanzar una longitud. Las recomendaciones de
entrada comercial llevan certainty=recommendation y no se presentan como hechos observados.
Para ingresos, valoración/financiación, publicidad, contratación pública u otras señales, registra cada dato como
public_signals con fecha si existe, URL, clase de evidencia y confianza. Diferencia lo declarado por la empresa, una
fuente pública secundaria y una estimación/no verificada. No accedas ni infieras información privada o sensible.

Empresa: {company}
Perfil de la productora/onboarding: {json.dumps(profile, ensure_ascii=False)}
Título de la web corporativa: {evidence.title}
Descripción de la web corporativa: {evidence.description}
Contactos publicados en la web corporativa: {json.dumps(evidence.public_contacts, ensure_ascii=False)}
Redes enlazadas desde la web corporativa: {json.dumps(evidence.social_links, ensure_ascii=False)}
Páginas corporativas consultadas respetando robots.txt: {json.dumps(evidence.pages, ensure_ascii=False)}

CONTENIDO PÚBLICO DE LA WEB CORPORATIVA:
{evidence.text}
"""
        return {
            "model": self.settings.openai_model,
            "store": False,
            "reasoning": {"effort": "low"},
            "tools": [{"type": "web_search"}],
            "tool_choice": "required",
            "max_tool_calls": self.max_search_calls,
            "include": ["web_search_call.action.sources"],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "focus_business_public_research",
                    "strict": True,
                    "schema": self._schema(),
                }
            },
            "input": prompt,
        }

    def _request(self, payload: dict) -> dict:
        if self.response_provider:
            return self.response_provider(payload)
        if not self.settings.openai_api_key:
            raise RuntimeError("Falta OPENAI_API_KEY")
        with httpx.Client(timeout=120) as client:
            response = client.post(
                self.endpoint,
                headers={"Authorization": f"Bearer {self.settings.openai_api_key}", "Content-Type": "application/json"},
                json=payload,
            )
        if response.is_error:
            raise RuntimeError(f"OpenAI API devolvió HTTP {response.status_code}")
        return response.json()

    @staticmethod
    def _output_text(response: dict) -> str:
        for item in response.get("output") or []:
            if item.get("type") != "message":
                continue
            for content in item.get("content") or []:
                if content.get("type") == "output_text":
                    return str(content.get("text") or "")
        return ""

    @staticmethod
    def _trace(response: dict) -> tuple[list[str], list[dict[str, str]], int]:
        queries: list[str] = []
        sources: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        calls = 0
        for item in response.get("output") or []:
            if item.get("type") == "web_search_call":
                calls += 1
                action = item.get("action") or {}
                raw_queries = action.get("queries") or ([action.get("query")] if action.get("query") else [])
                queries.extend(str(value).strip() for value in raw_queries if str(value or "").strip())
                for source in action.get("sources") or []:
                    url = str(source.get("url") or "").strip()
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        sources.append({"url": url, "title": str(source.get("title") or url), "type": "openai_web_search"})
            if item.get("type") == "message":
                for content in item.get("content") or []:
                    for annotation in content.get("annotations") or []:
                        if annotation.get("type") != "url_citation":
                            continue
                        url = str(annotation.get("url") or "").strip()
                        if url and url not in seen_urls:
                            seen_urls.add(url)
                            sources.append({"url": url, "title": str(annotation.get("title") or url), "type": "citation"})
        return queries, sources, calls

    @staticmethod
    def _valid_url(value: object) -> str:
        try:
            parsed = urlparse(str(value or "").strip())
        except ValueError:
            return ""
        return parsed.geturl() if parsed.scheme in {"http", "https"} and parsed.hostname else ""

    @staticmethod
    def _source_match_key(url: str) -> tuple[str, str, str, str, str, str]:
        """Match harmless URL spelling differences without merging distinct pages."""
        parsed = urlparse(url)
        query = urlencode(sorted(
            (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}
        ))
        return (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/") or "/",
            parsed.params,
            query,
            "",  # A fragment points into the same cited document.
        )

    def analyze(
        self,
        company: str,
        evidence: WebEvidence,
        prospecting_profile: dict | None = None,
    ) -> tuple[dict, dict]:
        payload = self.build_payload(company, evidence, prospecting_profile)
        response = self._request(payload)
        queries, openai_sources, calls = self._trace(response)
        # The provider enforces max_tool_calls in the request. A returned
        # response may still contain more web_search_call output items than
        # processed calls; rejecting valid output here discards completed work.
        try:
            data = json.loads(self._output_text(response) or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("OpenAI no devolvió JSON válido") from exc
        if not isinstance(data, dict):
            raise ValueError("OpenAI no devolvió un objeto JSON")

        research_sources: list[dict[str, str]] = [
            {"url": url, "title": "Web corporativa", "type": "company_website"} for url in evidence.pages
        ]
        known_urls = {self._source_match_key(item["url"]): item["url"] for item in research_sources}
        for source in openai_sources:
            url = self._valid_url(source.get("url"))
            key = self._source_match_key(url) if url else None
            if key and key not in known_urls:
                known_urls[key] = url
                research_sources.append({**source, "url": url})

        def cited_source(value: object) -> str:
            url = self._valid_url(value)
            return known_urls.get(self._source_match_key(url), "") if url else ""

        contacts: list[dict[str, str]] = list(evidence.public_contacts)
        seen_contacts = {(item["type"], item["value"].lower()) for item in contacts}
        for item in data.get("public_contacts") or []:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "").strip()
            value = str(item.get("value") or "").strip()
            source_url = cited_source(item.get("source_url"))
            if kind not in {"email", "phone", "contact_page", "person"} or not value or not source_url:
                continue
            key = (kind, value.lower())
            if key not in seen_contacts:
                seen_contacts.add(key)
                contacts.append({"type": kind, "value": value, "source_url": source_url})

        public_signals = []
        for item in data.get("public_signals") or []:
            if not isinstance(item, dict):
                continue
            source_url = cited_source(item.get("source_url"))
            if not source_url:
                continue
            public_signals.append(
                {
                    "type": str(item.get("type") or "other"),
                    "value": str(item.get("value") or "").strip(),
                    "date": str(item.get("date") or "").strip(),
                    "source_url": source_url,
                    "evidence_class": str(item.get("evidence_class") or "estimate_unverified"),
                    "confidence": str(item.get("confidence") or "low"),
                }
            )

        decision_makers = []
        for item in data.get("decision_makers") or []:
            if not isinstance(item, dict):
                continue
            source_url = cited_source(item.get("source_url"))
            name = str(item.get("name") or "").strip()
            role = str(item.get("role") or "").strip()
            if source_url and name and role and item.get("role_status") == "current":
                decision_makers.append({"name": name, "role": role, "role_status": "current", "public_contact": str(item.get("public_contact") or "").strip(), "source_url": source_url})

        social_links = dict(evidence.social_links)
        for network, value in (data.get("social_links") or {}).items():
            url = self._valid_url(value)
            host = (urlparse(url).hostname or "").lower().removeprefix("www.") if url else ""
            if network in SOCIAL_HOSTS and url and any(host == domain or host.endswith(f".{domain}") for domain in SOCIAL_HOSTS[network]):
                social_links.setdefault(network, url)

        for key in ("buying_signals", "disqualifiers"):
            if not isinstance(data.get(key), list):
                data[key] = []
        for key in ("capital_event", "financial_alert", "decision_recent", "prospect_found"):
            data[key] = data.get(key) is True
        data["buying_signals"] = [
            signal["value"] for signal in public_signals
            if signal["type"] in {"funding", "advertising", "public_procurement", "other"}
            and signal["confidence"] in {"high", "medium"}
            and signal["evidence_class"] != "estimate_unverified"
        ]
        if not decision_makers:
            data["decision_access"] = None
            data["decision_recent"] = False
        employees = data.get("employees")
        if employees is not None and not isinstance(employees, int):
            try:
                data["employees"] = int(float(str(employees).replace(",", ".")))
            except ValueError:
                data["employees"] = None
        data["public_contacts"] = contacts
        data["public_signals"] = public_signals
        data["decision_makers"] = decision_makers
        data["public_signals_status"] = "Encontradas" if public_signals else "No encontrado públicamente"
        data["social_links"] = social_links
        data["legal_status_source_url"] = cited_source(data.get("legal_status_source_url"))
        if data.get("legal_status") in {"inactive", "absorbed", "extinct"}:
            data["prospect_found"] = False
            data["disqualifiers"].append(f"Sociedad {data['legal_status']}")
        raw_dossier = data.get("dossier")
        raw_dossier = raw_dossier if isinstance(raw_dossier, dict) else {}
        dossier = {}
        for section in DOSSIER_SECTIONS:
            facts = []
            section_items = raw_dossier.get(section)
            for item in section_items if isinstance(section_items, list) else []:
                if not isinstance(item, dict):
                    continue
                statement = str(item.get("text") or "").strip()[:700]
                certainty = str(item.get("certainty") or "")
                source_url = cited_source(item.get("source_url"))[:500]
                if not statement or certainty not in {
                    "confirmed", "historical", "estimate", "not_available", "recommendation"
                }:
                    continue
                if certainty in {"confirmed", "historical", "estimate"} and not source_url:
                    continue
                facts.append({
                    "text": statement,
                    "date": str(item.get("date") or "").strip()[:60],
                    "source_url": source_url,
                    "certainty": certainty,
                })
                if len(facts) == 5:
                    break
            dossier[section] = facts
        data["dossier"] = dossier
        data["research_sources"] = research_sources
        data["search_queries"] = queries[: self.max_search_calls]
        data["web_search_calls"] = calls
        data["web_search_call_limit"] = self.max_search_calls
        if not contacts:
            data["no_contacts_reason"] = str(data.get("no_contacts_reason") or "No se encontraron contactos públicos verificables.")
        else:
            data["no_contacts_reason"] = ""
        if not data["prospect_found"]:
            data["no_prospect_reason"] = str(data.get("no_prospect_reason") or "No se encontró evidencia suficiente para confirmar un prospecto.")
        else:
            data["no_prospect_reason"] = ""
        usage = response.get("usage") or {}
        usage_data = {
            "prompt_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
            "web_search_calls": calls,
            "web_search_call_limit": self.max_search_calls,
            "search_queries": data["search_queries"],
            "research_sources": research_sources,
        }
        score = score_company(data)
        data["score"] = score.total
        data["classification"] = score.classification
        data["score_detail"] = score.as_dict()
        return data, usage_data


class OpenAIProspectDiscovery(OpenAIWebResearcher):
    """Discover public business prospects from an Onboarding targeting profile.

    A single Responses API execution may invoke the hosted web-search tool up to
    five times. The class never performs login/private-data access and rejects
    contacts that do not point to a source returned by the research response.
    """

    @staticmethod
    def _schema() -> dict:
        nullable_string = {"type": ["string", "null"]}
        prospect = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "company": {"type": "string"},
                "website": {"type": "string"},
                "title": nullable_string,
                "description": nullable_string,
                "sector": nullable_string,
                "business_model": nullable_string,
                "city": nullable_string,
                "country": nullable_string,
                "client_type": nullable_string,
                "employees": {"type": ["integer", "null"]},
                "employee_trend": {"type": ["string", "null"], "enum": ["growing", "stable", "shrinking", None]},
                "revenue_eur": {"type": ["number", "null"]},
                "revenue_trend": {"type": ["string", "null"], "enum": ["growing", "stable", "shrinking", None]},
                "capital_event": {"type": "boolean"},
                "financial_alert": {"type": "boolean"},
                "legal_status": {"type": "string", "enum": ["active", "inactive", "absorbed", "extinct", "unknown"]},
                "legal_status_source_url": nullable_string,
                "buying_signals": {"type": "array", "items": {"type": "string"}},
                "decision_access": {"type": ["string", "null"], "enum": ["active", "passive", "registry_only", None]},
                "decision_recent": {"type": "boolean"},
                "disqualifiers": {"type": "array", "items": {"type": "string"}},
                "summary": {"type": "string"},
                "entry_angle": {"type": "string"},
                "public_contacts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "type": {"type": "string", "enum": ["email", "phone", "contact_page", "person"]},
                            "value": {"type": "string"},
                            "source_url": {"type": "string"},
                        },
                        "required": ["type", "value", "source_url"],
                    },
                },
                "public_signals": {"type": "array", "items": _public_signal_schema()},
                "decision_makers": {
                    "type": "array",
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "role_status": {"type": "string", "enum": ["current", "historical", "unverified"]}, "public_contact": nullable_string, "source_url": {"type": "string"}},
                        "required": ["name", "role", "role_status", "public_contact", "source_url"],
                    },
                },
                "social_links": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {key: nullable_string for key in SOCIAL_HOSTS},
                    "required": list(SOCIAL_HOSTS),
                },
                "evidence_urls": {"type": "array", "items": {"type": "string"}},
                "no_contacts_reason": nullable_string,
            },
            "required": [
                "company", "website", "title", "description", "sector", "business_model", "city", "country",
                "client_type", "employees", "employee_trend", "revenue_eur", "revenue_trend", "capital_event",
                "financial_alert", "legal_status", "legal_status_source_url", "buying_signals", "decision_access", "decision_recent", "disqualifiers",
                "summary", "entry_angle", "public_contacts", "public_signals", "decision_makers", "social_links", "evidence_urls", "no_contacts_reason",
            ],
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "prospects": {"type": "array", "items": prospect},
                "no_prospect_reason": nullable_string,
                "research_summary": {"type": "string"},
            },
            "required": ["prospects", "no_prospect_reason", "research_summary"],
        }

    def build_discovery_payload(self, prospecting_profile: dict, adjustments: dict | None = None) -> dict:
        controls = adjustments or {}
        prompt = f"""
Encuentra empresas que puedan ser prospectos comerciales adecuados para la productora descrita en el perfil de
Onboarding. La configuración del formulario es la fuente principal; los ajustes del dashboard solo acotan esa
configuración y no autorizan a inventar criterios ausentes.

El objetivo operativo es devolver 5 leads válidos y deduplicables. Si no encuentras cinco, entrega únicamente los
resultados reales que cumplan los criterios y explica el motivo del déficit; no inventes, rellenes ni relajes la calidad.
El objetivo comercial base es conseguir nuevos clientes para la productora y priorizar empresas con potencial de
contratación recurrente. La recurrencia no se presume: debe justificarse con señales públicas verificables, como una
necesidad continua de contenido, campañas frecuentes, lanzamientos, eventos o comunicación audiovisual sostenida.
Si no existe evidencia pública suficiente, indícalo como dato no disponible y no inventes esa señal.
Aplica todos los filtros, señales, decisores y exclusiones indicados en AJUSTES OPCIONALES, sin relajar criterios para completar la cantidad.
Si se indican decision_roles, busca primero personas con esos cargos y cita la página pública exacta que confirma
su nombre y función. La web corporativa general o el LinkedIn de la empresa no sustituyen a un decisor identificado.
Si require_identifiable_decision_maker es true, no incluyas empresas sin al menos una persona identificada con
nombre, cargo y fuente pública verificable. Para cada empresa busca también señales de compra concretas con su
fecha y fuente; si no existen, deja public_signals vacío en vez de inferirlas de una descripción genérica.
Comprueba la identidad de la entidad y la vigencia de los cargos: un nombramiento histórico no demuestra que
esa persona siga siendo decisora. Si una sociedad se extinguió o fue absorbida, no la presentes como cliente
potencial activo; distingue la marca comercial de la sociedad operadora actual y documenta la fuente en
legal_status_source_url. Para cada persona marca role_status=current solo con evidencia de cargo vigente;
historical y unverified no son decisores actuales.
Realiza como máximo {self.settings.web_search_call_limit} acciones de búsqueda web pública en total. Respeta robots,
términos y límites técnicos. No inicies sesión, no evadas CAPTCHA, no accedas a información privada y no afirmes
cobertura absoluta. Prioriza webs corporativas y fuentes primarias. No inventes empresas, webs, contactos, personas,
redes, tamaños, ingresos ni señales. Cada contacto debe incluir la URL exacta donde fue publicado y cada empresa debe
incluir URLs de evidencia. Excluye cualquier empresa que contradiga las exclusiones del perfil. Si no hay resultados
verificables, devuelve prospects=[] y explica el motivo disponible. La salida debe cumplir exactamente el esquema JSON.
Para facturación/ingresos, valoración o financiación, inversiones/campañas publicitarias, contratación pública de
productoras/agencias u otras señales empresariales, usa public_signals. Cada señal debe tener texto, fecha si existe,
URL, clase de evidencia y confianza. Si no hay evidencia pública, deja la lista vacía; nunca uses cero como sustituto.

PERFIL NORMALIZADO DEL ONBOARDING:
{json.dumps(prospecting_profile, ensure_ascii=False)}

AJUSTES OPCIONALES DEL DASHBOARD:
{json.dumps(controls, ensure_ascii=False)}
"""
        return {
            "model": self.settings.openai_model,
            "store": False,
            "reasoning": {"effort": "low"},
            "tools": [{"type": "web_search"}],
            "tool_choice": "required",
            "max_tool_calls": self.settings.web_search_call_limit,
            "include": ["web_search_call.action.sources"],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "focus_business_prospect_discovery",
                    "strict": True,
                    "schema": self._schema(),
                }
            },
            "input": prompt,
        }

    def discover(self, prospecting_profile: dict, adjustments: dict | None = None) -> tuple[list[dict], dict]:
        payload = self.build_discovery_payload(prospecting_profile, adjustments)
        response = self._request(payload)
        queries, sources, calls = self._trace(response)
        # Keep the observed output count for audit, but do not turn a valid
        # provider response into a failed scrape after the search has run.
        try:
            data = json.loads(self._output_text(response) or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("OpenAI no devolvió JSON válido") from exc
        if not isinstance(data, dict) or not isinstance(data.get("prospects"), list):
            raise ValueError("OpenAI no devolvió una lista de prospectos válida")

        valid_sources = []
        known_sources: dict[tuple[str, str, str, str, str, str], str] = {}
        for source in sources:
            url = self._valid_url(source.get("url"))
            key = self._source_match_key(url) if url else None
            if key and key not in known_sources:
                known_sources[key] = url
                valid_sources.append({**source, "url": url})

        def cited_source(value: object) -> str:
            url = self._valid_url(value)
            return known_sources.get(self._source_match_key(url), "") if url else ""

        prospects: list[dict] = []
        seen_prospects: set[tuple[str, str]] = set()
        require_identifiable_decision_maker = bool((adjustments or {}).get("require_identifiable_decision_maker"))
        rejected_without_decision_maker = 0
        for raw in data["prospects"]:
            if not isinstance(raw, dict):
                continue
            website = self._valid_url(raw.get("website"))
            company = str(raw.get("company") or "").strip()
            if not company or not website:
                continue
            prospect_key = (company.casefold(), (urlparse(website).hostname or "").lower().removeprefix("www."))
            if prospect_key in seen_prospects:
                continue
            evidence_urls = list(dict.fromkeys(
                url for url in (cited_source(value) for value in raw.get("evidence_urls") or []) if url
            ))
            if not evidence_urls:
                continue
            contacts = []
            for contact in raw.get("public_contacts") or []:
                if not isinstance(contact, dict):
                    continue
                source_url = cited_source(contact.get("source_url"))
                value = str(contact.get("value") or "").strip()
                kind = str(contact.get("type") or "").strip()
                if source_url and value and kind in {"email", "phone", "contact_page", "person"}:
                    contacts.append({"type": kind, "value": value, "source_url": source_url})
            public_signals = []
            for signal in raw.get("public_signals") or []:
                if not isinstance(signal, dict):
                    continue
                source_url = cited_source(signal.get("source_url"))
                value = str(signal.get("value") or "").strip()
                if source_url and value:
                    public_signals.append(
                        {
                            "type": str(signal.get("type") or "other"),
                            "value": value,
                            "date": str(signal.get("date") or "").strip(),
                            "source_url": source_url,
                            "evidence_class": str(signal.get("evidence_class") or "estimate_unverified"),
                            "confidence": str(signal.get("confidence") or "low"),
                        }
                    )
            decision_makers = []
            for person in raw.get("decision_makers") or []:
                if not isinstance(person, dict):
                    continue
                source_url = cited_source(person.get("source_url"))
                name = str(person.get("name") or "").strip()
                role = str(person.get("role") or "").strip()
                if source_url and name and role and person.get("role_status") == "current":
                    decision_makers.append({"name": name, "role": role, "role_status": "current", "public_contact": str(person.get("public_contact") or "").strip(), "source_url": source_url})
            if require_identifiable_decision_maker and not decision_makers:
                rejected_without_decision_maker += 1
                continue
            social_links: dict[str, str] = {}
            for network, value in (raw.get("social_links") or {}).items():
                url = self._valid_url(value)
                host = (urlparse(url).hostname or "").lower().removeprefix("www.") if url else ""
                if network in SOCIAL_HOSTS and any(host == domain or host.endswith(f".{domain}") for domain in SOCIAL_HOSTS[network]):
                    social_links[network] = url
            for key in ("buying_signals", "disqualifiers"):
                if not isinstance(raw.get(key), list):
                    raw[key] = []
            if raw["disqualifiers"]:
                continue
            raw["legal_status_source_url"] = cited_source(raw.get("legal_status_source_url"))
            if raw.get("legal_status") in {"inactive", "absorbed", "extinct"}:
                continue
            for key in ("capital_event", "financial_alert", "decision_recent"):
                raw[key] = raw.get(key) is True
            # Only source-backed opportunities may increase the buying score.
            raw["buying_signals"] = [
                signal["value"] for signal in public_signals
                if signal["type"] in {"funding", "advertising", "public_procurement"}
                and signal["confidence"] in {"high", "medium"}
                and signal["evidence_class"] != "estimate_unverified"
            ]
            if not decision_makers:
                raw["decision_access"] = None
                raw["decision_recent"] = False
            raw["website"] = website
            raw["public_contacts"] = contacts
            raw["public_signals"] = public_signals
            raw["decision_makers"] = decision_makers
            raw["public_signals_status"] = "Encontradas" if public_signals else "No encontrado públicamente"
            raw["social_links"] = social_links
            raw["evidence"] = evidence_urls
            raw["research_sources"] = [item for item in valid_sources if item["url"] in evidence_urls]
            raw["search_queries"] = queries[: self.settings.web_search_call_limit]
            raw["web_search_calls"] = calls
            raw["web_search_call_limit"] = self.settings.web_search_call_limit
            raw["prospect_found"] = True
            raw["no_prospect_reason"] = ""
            raw["no_contacts_reason"] = "" if contacts else str(
                raw.get("no_contacts_reason") or "No se encontraron contactos públicos verificables."
            )
            score = score_company(raw)
            raw["score"] = score.total
            raw["classification"] = score.classification
            raw["score_detail"] = score.as_dict()
            seen_prospects.add(prospect_key)
            prospects.append(raw)

        requested_leads = 5
        prospects = prospects[:requested_leads]
        if not prospects and rejected_without_decision_maker:
            data["no_prospect_reason"] = (
                "No se encontraron empresas con un decisor identificable y una fuente pública verificable "
                f"({rejected_without_decision_maker} resultado(s) descartado(s))."
            )
        usage = response.get("usage") or {}
        trace_queries = queries[: self.settings.web_search_call_limit]
        while len(trace_queries) < calls:
            trace_queries.append(f"Búsqueda web {len(trace_queries) + 1} (consulta administrada por OpenAI)")
        searched_at = datetime.now(timezone.utc).isoformat()
        search_trace = [
            {
                "query": query,
                "searched_at": searched_at,
                "status": "Completada",
                "provider": "OpenAI Responses API + web_search",
                "result": f"{len(valid_sources)} fuentes públicas registradas en la ejecución",
                "source_urls": [source["url"] for source in valid_sources],
            }
            for query in trace_queries[: self.settings.web_search_call_limit]
        ]
        trace = {
            "prompt_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
            "web_search_calls": calls,
            "web_search_call_limit": self.settings.web_search_call_limit,
            "search_queries": queries[: self.settings.web_search_call_limit],
            "search_trace": search_trace,
            "research_provider": "OpenAI Responses API + web_search",
            "research_sources": valid_sources,
            "research_summary": str(data.get("research_summary") or "").strip(),
            "no_prospect_reason": "" if prospects else str(
                data.get("no_prospect_reason") or "No se encontraron prospectos públicos verificables con estos criterios."
            ),
            "search_configuration": prospecting_profile,
            "adjustments": adjustments or {},
        }
        return prospects, trace


def enrich_discovered_prospects(
    prospects: list[dict],
    trace: dict,
    prospecting_profile: dict,
    adjustments: dict | None = None,
    *,
    settings: Settings | None = None,
    researcher: OpenAIWebResearcher | None = None,
    scraper: PublicWebScraper | None = None,
) -> tuple[list[dict], dict]:
    """Verify each candidate as an operating company before it enters the CRM.

    This optional, bounded second pass is deliberately separate from discovery:
    a broad search cannot establish current directors, corporate continuity and
    dated buying signals for five companies with the same depth as one dossier.
    """
    config = settings or get_settings()
    detailed = researcher or OpenAIWebResearcher(
        config, max_search_calls=config.company_research_call_limit
    )
    public_scraper = scraper or PublicWebScraper(config)
    enriched: list[dict] = []
    discarded = 0
    for lead in prospects[:5]:
        website = str(lead.get("website") or "").strip()
        try:
            evidence = public_scraper.scrape(website)
        except (ValueError, PermissionError, httpx.HTTPError):
            # Search can still investigate a public company if its site blocks
            # scraping, but do not send a private/reserved URL to the provider.
            try:
                _public_url(website)
            except ValueError:
                discarded += 1
                continue
            evidence = WebEvidence("", "", "", [], {}, [])

        analysis, usage = detailed.analyze(
            str(lead.get("company") or ""),
            evidence,
            {"producer_profile": prospecting_profile, "targeting_adjustments": adjustments or {},
             "discovery_candidate": {"company": lead.get("company"), "website": website,
                                     "summary": lead.get("summary"), "evidence": lead.get("evidence")}},
        )
        trace["prompt_tokens"] += usage["prompt_tokens"]
        trace["output_tokens"] += usage["output_tokens"]
        trace["total_tokens"] += usage["total_tokens"]
        trace["web_search_calls"] += usage["web_search_calls"]
        trace["web_search_call_limit"] += usage["web_search_call_limit"]
        trace["search_queries"].extend(usage["search_queries"])
        trace["research_sources"].extend(
            source for source in usage["research_sources"]
            if source["url"] not in {item["url"] for item in trace["research_sources"]}
        )
        trace["search_trace"].append({
            "query": f"Verificación detallada: {lead['company']}",
            "searched_at": datetime.now(timezone.utc).isoformat(),
            "status": "Completada",
            "provider": "OpenAI Responses API + web_search",
            "result": "Empresa verificada" if analysis["prospect_found"] and not analysis["disqualifiers"] else "Descartada",
            "source_urls": [source["url"] for source in usage["research_sources"]],
        })
        if not analysis["prospect_found"] or analysis["disqualifiers"]:
            discarded += 1
            continue
        if (adjustments or {}).get("require_identifiable_decision_maker") and not analysis["decision_makers"]:
            discarded += 1
            continue
        merged = {**lead}
        for field in (
            "sector", "business_model", "city", "employees", "employee_trend", "revenue_eur",
            "revenue_trend", "capital_event", "financial_alert", "buying_signals", "decision_access",
            "decision_recent", "summary", "entry_angle", "public_contacts", "public_signals",
            "public_signals_status", "decision_makers", "social_links", "no_contacts_reason",
            "prospect_found", "no_prospect_reason", "legal_status", "legal_status_source_url",
            "dossier", "score", "classification", "score_detail",
        ):
            merged[field] = analysis.get(field)
        merged["evidence"] = list(dict.fromkeys(
            [*lead.get("evidence", []), *(source["url"] for source in analysis["research_sources"])]
        ))
        merged["research_sources"] = list({
            source["url"]: source for source in [*lead.get("research_sources", []), *analysis["research_sources"]]
        }.values())
        merged["search_queries"] = [*lead.get("search_queries", []), *analysis["search_queries"]]
        merged["web_search_calls"] = lead.get("web_search_calls", 0) + analysis["web_search_calls"]
        merged["web_search_call_limit"] = lead.get("web_search_call_limit", 0) + analysis["web_search_call_limit"]
        enriched.append(merged)

    trace["research_summary"] = (
        f"{trace.get('research_summary', '').strip()} Verificación individual: "
        f"{len(enriched)} empresas aptas, {discarded} descartadas."
    ).strip()
    if not enriched and prospects:
        trace["no_prospect_reason"] = (
            "La verificación individual no confirmó empresas operativas con los criterios y fuentes requeridos."
        )
    return enriched, trace
