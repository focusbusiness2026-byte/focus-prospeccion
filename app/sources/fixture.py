from __future__ import annotations

from datetime import datetime, timezone

from app.sources.base import CompanySource


class FixtureSource(CompanySource):
    name = "fixture"

    def discover(self, filters: dict) -> list[dict]:
        observed_at = datetime.now(timezone.utc).isoformat()
        location = filters.get("location") or "Espana"
        sector = (filters.get("sectors") or ["Servicios B2B"])[0]
        rows = [
            {
                "legal_name": "Norte Digital Demo S.L.",
                "commercial_name": "Norte Digital",
                "website": "https://norte-digital.example",
                "phone": "+34 910 000 101",
                "city": "Madrid",
                "sector": sector,
                "employees": 68,
                "employee_trend": "growing",
                "revenue_eur": 6_200_000,
                "revenue_trend": "growing",
                "capital_event": True,
                "financial_alert": False,
                "buying_signals": ["expansion", "commercial_hiring", "new_product"],
                "decision_access": "active",
                "decision_recent": True,
                "disqualifiers": [],
            },
            {
                "legal_name": "Levante Operaciones Demo S.L.",
                "commercial_name": "Levante Operaciones",
                "website": "https://levante-operaciones.example",
                "phone": "+34 960 000 202",
                "city": "Valencia",
                "sector": sector,
                "employees": 24,
                "employee_trend": "stable",
                "revenue_eur": 1_300_000,
                "revenue_trend": "stable",
                "capital_event": False,
                "financial_alert": False,
                "buying_signals": ["commercial_hiring"],
                "decision_access": "passive",
                "decision_recent": False,
                "disqualifiers": [],
            },
            {
                "legal_name": "Sur Comercio Demo S.L.",
                "commercial_name": "Sur Comercio",
                "website": "https://sur-comercio.example",
                "phone": "+34 950 000 303",
                "city": "Malaga",
                "sector": sector,
                "employees": 7,
                "employee_trend": "shrinking",
                "revenue_eur": None,
                "revenue_trend": None,
                "capital_event": False,
                "financial_alert": False,
                "buying_signals": [],
                "decision_access": "registry_only",
                "decision_recent": False,
                "disqualifiers": [],
            },
        ]
        for row in rows:
            row["evidence"] = [
                {
                    "source": "fixture local",
                    "url": None,
                    "observed_at": observed_at,
                    "note": f"Dato sintetico para validar el flujo en {location}; no es una empresa real.",
                }
            ]
        return rows

