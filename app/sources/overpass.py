from __future__ import annotations

from datetime import datetime, timezone
import re

import httpx

from app.config import get_settings
from app.sources.base import CompanySource


class OverpassSource(CompanySource):
    name = "overpass"

    @staticmethod
    def _safe_city(value: str) -> str:
        value = value.strip()
        if not value or len(value) > 80 or not re.fullmatch(r"[\w\s.'\-]+", value, flags=re.UNICODE):
            raise ValueError("Ciudad no valida para la consulta de fuente")
        return value

    @staticmethod
    def _safe_activity(value: str) -> str:
        allowed = {"office", "craft", "shop", "industrial", "amenity"}
        if value not in allowed:
            raise ValueError(f"Actividad OSM no soportada: {value}")
        return value

    def discover(self, filters: dict) -> list[dict]:
        settings = get_settings()
        if not settings.allow_external_sources:
            raise RuntimeError("Las fuentes externas estan bloqueadas por ALLOW_EXTERNAL_SOURCES=false")

        city = self._safe_city(filters.get("city") or "Madrid")
        activity = self._safe_activity((filters.get("osm_activity") or "office").strip())
        limit = min(int(filters.get("limit") or 20), 50)
        query = f"""
        [out:json][timeout:25];
        area[\"name\"=\"{city}\"][\"boundary\"=\"administrative\"]->.searchArea;
        nwr[\"{activity}\"](area.searchArea);
        out center {limit};
        """
        response = httpx.post(
            settings.overpass_url,
            content=query.encode("utf-8"),
            headers={"User-Agent": settings.overpass_user_agent, "Content-Type": "text/plain"},
            timeout=30,
        )
        response.raise_for_status()
        observed_at = datetime.now(timezone.utc).isoformat()
        candidates: list[dict] = []
        for element in response.json().get("elements", [])[:limit]:
            tags = element.get("tags") or {}
            name = tags.get("name")
            if not name:
                continue
            candidates.append(
                {
                    "legal_name": name,
                    "commercial_name": name,
                    "website": tags.get("contact:website") or tags.get("website"),
                    "phone": tags.get("contact:phone") or tags.get("phone"),
                    "city": tags.get("addr:city") or city,
                    "sector": tags.get(activity) or tags.get("office") or activity,
                    "employees": None,
                    "employee_trend": None,
                    "revenue_eur": None,
                    "revenue_trend": None,
                    "capital_event": False,
                    "financial_alert": False,
                    "buying_signals": [],
                    "decision_access": None,
                    "decision_recent": False,
                    "disqualifiers": [],
                    "evidence": [
                        {
                            "source": "OpenStreetMap via Overpass API",
                            "url": f"https://www.openstreetmap.org/{element['type']}/{element['id']}",
                            "observed_at": observed_at,
                            "note": "Descubrimiento inicial; requiere enriquecimiento y verificacion registral.",
                        }
                    ],
                }
            )
        return candidates
