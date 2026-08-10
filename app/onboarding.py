from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urlparse


def _text(value: object) -> str:
    return str(value or "").strip()


def _items(value: object) -> list[str]:
    return [item.strip() for item in re.split(r"[,\n]+", _text(value)) if item.strip()]


def _accepted(value: object) -> bool:
    return _text(value).lower() in {"1", "true", "sí", "si", "yes", "verdadero"}


def _public_website(value: object) -> str:
    website = _text(value)
    try:
        parsed = urlparse(website)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    return website


@dataclass(frozen=True)
class OnboardingSource:
    record_id: str
    submitted_at: str
    status: str
    company: str
    website: str
    email: str
    activity: str
    location: str
    description: str
    main_service: str
    services: tuple[str, ...]
    audience: tuple[str, ...]
    sectors: tuple[str, ...]
    markets: tuple[str, ...]
    target_city: str
    target_region: str
    target_countries: tuple[str, ...]
    target_client_types: tuple[str, ...]
    ideal_company_size: str
    ideal_profile_detail: str
    decision_maker: str
    minimum_budget: str
    monthly_capacity: str
    portfolio_highlights: str
    reference_companies: tuple[str, ...]
    prospect_exclusions: str
    prospect_preferences: str
    objectives: tuple[str, ...]
    authorized: bool

    @classmethod
    def from_sheet_record(cls, record: dict[str, object]) -> "OnboardingSource":
        return cls(
            record_id=_text(record.get("ID registro")),
            submitted_at=_text(record.get("Fecha envío")),
            status=_text(record.get("Estado")) or "Nuevo",
            company=_text(record.get("Empresa")),
            website=_public_website(record.get("Web")),
            email=(_text(record.get("Email responsable")) or _text(record.get("Email corporativo"))).lower(),
            activity=_text(record.get("Actividad")),
            location=_text(record.get("Ciudad / país")),
            description=_text(record.get("Descripción")),
            main_service=_text(record.get("Servicio prioritario")),
            services=tuple(_items(record.get("Servicios"))),
            audience=tuple(_items(record.get("Público"))),
            sectors=tuple(_items(record.get("Sectores"))),
            markets=tuple(_items(record.get("Mercados"))),
            target_city=_text(record.get("Ciudad objetivo")),
            target_region=_text(record.get("Región objetivo")),
            target_countries=tuple(_items(record.get("Países objetivo"))),
            target_client_types=tuple(_items(record.get("Tipos de cliente objetivo"))),
            ideal_company_size=_text(record.get("Tamaño empresa ideal")),
            ideal_profile_detail=_text(record.get("Perfil ideal detallado")),
            decision_maker=_text(record.get("Decisor habitual")),
            minimum_budget=_text(record.get("Presupuesto mínimo")),
            monthly_capacity=_text(record.get("Capacidad mensual")),
            portfolio_highlights=_text(record.get("Casos de éxito / portafolio")),
            reference_companies=tuple(_items(record.get("Empresas de referencia"))),
            prospect_exclusions=_text(record.get("Exclusiones de prospección")),
            prospect_preferences=_text(record.get("Preferencias de prospección")),
            objectives=tuple(_items(record.get("Objetivos"))),
            authorized=_accepted(record.get("Autorización")),
        )

    @property
    def blockers(self) -> list[str]:
        blockers: list[str] = []
        if not self.record_id:
            blockers.append("Falta el ID del registro")
        if not self.company:
            blockers.append("Falta el nombre de la productora")
        if not self.website:
            blockers.append("Falta una web pública válida")
        if not self.email:
            blockers.append("Falta el correo responsable")
        if not self.main_service and not self.services:
            blockers.append("Faltan los servicios que se quieren promover")
        if not self.sectors:
            blockers.append("Faltan los sectores objetivo")
        if not self.target_countries and not self.markets:
            blockers.append("Faltan los países o mercados objetivo")
        if not self.target_client_types and not self.audience:
            blockers.append("Falta el tipo de cliente objetivo")
        if not self.ideal_profile_detail and not self.ideal_company_size:
            blockers.append("Falta definir el perfil o tamaño de empresa ideal")
        if not self.authorized:
            blockers.append("La autorización no está confirmada")
        return blockers

    @property
    def ready(self) -> bool:
        return not self.blockers

    def prospecting_profile(self) -> dict:
        """Return the normalized brief used by a future discovery provider.

        This method performs no network request and does not invent missing
        targeting criteria. A provider may only run when ``ready`` is true.
        """
        return {
            "onboarding_id": self.record_id,
            "productora": {
                "name": self.company,
                "website": self.website,
                "email": self.email,
                "activity": self.activity,
                "location": self.location,
                "description": self.description,
            },
            "targeting": {
                "main_service": self.main_service,
                "services": list(self.services),
                "audience": list(self.audience),
                "sectors": list(self.sectors),
                "markets": list(self.markets),
                "target_city": self.target_city,
                "target_region": self.target_region,
                "target_countries": list(self.target_countries),
                "target_client_types": list(self.target_client_types),
                "ideal_company_size": self.ideal_company_size,
                "ideal_profile_detail": self.ideal_profile_detail,
                "decision_maker": self.decision_maker,
                "minimum_budget": self.minimum_budget,
                "monthly_capacity": self.monthly_capacity,
                "portfolio_highlights": self.portfolio_highlights,
                "reference_companies": list(self.reference_companies),
                "prospect_exclusions": self.prospect_exclusions,
                "prospect_preferences": self.prospect_preferences,
                "objectives": list(self.objectives),
            },
        }

    def as_dict(self) -> dict:
        return {
            **self.prospecting_profile(),
            "submitted_at": self.submitted_at,
            "status": self.status,
            "ready": self.ready,
            "blockers": self.blockers,
        }
