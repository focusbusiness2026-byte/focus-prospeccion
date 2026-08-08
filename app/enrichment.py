from __future__ import annotations

import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from google import genai
from google.genai import types

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


@dataclass(frozen=True)
class WebEvidence:
    title: str
    description: str
    text: str
    pages: list[str]
    social_links: dict[str, str]


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

    def scrape(self, website: str) -> WebEvidence:
        website = _public_url(website)
        origin_host = (urlparse(website).hostname or "").lower()
        pages: list[str] = []
        texts: list[str] = []
        social_links: dict[str, str] = {}
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
                for anchor in soup.select("a[href]"):
                    candidate = urljoin(final_url, anchor.get("href", "")).split("#", 1)[0]
                    parsed = urlparse(candidate)
                    label = (anchor.get_text(" ", strip=True) + " " + parsed.path).lower()
                    if parsed.scheme in {"http", "https"} and parsed.hostname == origin_host and re.search(
                        r"about|empresa|nosotros|contact|servicios|services", label
                    ):
                        if candidate not in visited and candidate not in queue:
                            queue.append(candidate)
        return WebEvidence(title, description, "\n\n".join(texts)[:55_000], pages, social_links)


class GeminiAnalyzer:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def analyze(self, company: str, evidence: WebEvidence) -> tuple[dict, dict]:
        if not self.settings.gemini_api_key:
            raise RuntimeError("Falta GEMINI_API_KEY")
        prompt = f"""
Eres el analista de prospeccion de Focus Business. Analiza exclusivamente la evidencia publica incluida.
No inventes cifras, empleados, ubicaciones, decisores ni senales. Usa null cuando no exista evidencia.
Devuelve un objeto JSON con estas claves exactas:
sector (string o null), business_model (string o null), city (string o null),
employees (entero o null), employee_trend ("growing", "stable", "shrinking" o null),
revenue_eur (numero o null), revenue_trend ("growing", "stable", "shrinking" o null),
capital_event (boolean), financial_alert (boolean), buying_signals (lista de strings),
decision_access ("active", "passive", "registry_only" o null), decision_recent (boolean),
disqualifiers (lista de strings), summary (string) y entry_angle (string).

Empresa indicada: {company}
Titulo web: {evidence.title}
Descripcion web: {evidence.description}
Redes enlazadas desde la web: {json.dumps(evidence.social_links, ensure_ascii=False)}

EVIDENCIA:
{evidence.text}
"""
        client = genai.Client(api_key=self.settings.gemini_api_key)
        response = client.models.generate_content(
            model=self.settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1),
        )
        data = json.loads(response.text or "{}")
        if not isinstance(data, dict):
            raise ValueError("Gemini no devolvio un objeto JSON")
        for key in ("buying_signals", "disqualifiers"):
            if not isinstance(data.get(key), list):
                data[key] = []
        for key in ("capital_event", "financial_alert", "decision_recent"):
            data[key] = data.get(key) is True
        employees = data.get("employees")
        if employees is not None and not isinstance(employees, int):
            try:
                data["employees"] = int(float(str(employees).replace(",", ".")))
            except ValueError:
                data["employees"] = None
        usage = getattr(response, "usage_metadata", None)
        usage_data = {
            "prompt_tokens": int(getattr(usage, "prompt_token_count", 0) or 0),
            "output_tokens": int(getattr(usage, "candidates_token_count", 0) or 0),
            "total_tokens": int(getattr(usage, "total_token_count", 0) or 0),
        }
        score = score_company(data)
        data["score"] = score.total
        data["classification"] = score.classification
        data["score_detail"] = score.as_dict()
        return data, usage_data
