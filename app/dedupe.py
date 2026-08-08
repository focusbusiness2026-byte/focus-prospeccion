from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlparse


def _slug(value: str | None) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _domain(website: str | None) -> str:
    if not website:
        return ""
    parsed = urlparse(website if "://" in website else f"https://{website}")
    return parsed.netloc.lower().removeprefix("www.").split(":")[0]


def company_dedupe_key(company: dict) -> str:
    if company.get("cif"):
        return f"cif:{_slug(company['cif'])}"
    domain = _domain(company.get("website"))
    if domain:
        return f"domain:{domain}"
    phone = re.sub(r"\D", "", company.get("phone") or "")
    if phone:
        return f"phone:{phone}"
    return f"name_city:{_slug(company.get('legal_name') or company.get('commercial_name'))}:{_slug(company.get('city'))}"

