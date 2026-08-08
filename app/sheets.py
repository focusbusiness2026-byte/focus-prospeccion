from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import quote

from google.auth.transport.requests import AuthorizedSession
from google.oauth2.service_account import Credentials

from app.config import Settings, get_settings
from app.models import Prospect, SearchJob


SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
HEADERS = [
    "execution_id",
    "client_id",
    "company",
    "legal_name",
    "city",
    "sector",
    "employees",
    "revenue_eur",
    "score",
    "classification",
    "website",
    "phone",
    "evidence_source",
    "evidence_url",
    "evidence_observed_at",
    "search_filters",
    "exported_at",
]


def build_sheet_rows(job: SearchJob, prospects: list[Prospect]) -> list[list]:
    exported_at = datetime.now(timezone.utc).isoformat()
    rows: list[list] = []
    for prospect in prospects:
        evidence = (prospect.evidence or [{}])[0]
        rows.append(
            [
                job.id,
                job.client_id,
                prospect.commercial_name,
                prospect.legal_name,
                prospect.city or "",
                prospect.sector or "",
                prospect.employees if prospect.employees is not None else "",
                prospect.revenue_eur if prospect.revenue_eur is not None else "",
                prospect.score,
                prospect.classification,
                prospect.website or "",
                prospect.phone or "",
                evidence.get("source") or "",
                evidence.get("url") or "",
                evidence.get("observed_at") or "",
                json.dumps(job.filters, ensure_ascii=False, sort_keys=True),
                exported_at,
            ]
        )
    return rows


class GoogleSheetsExporter:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def _authorized_session(self) -> AuthorizedSession:
        if not self.settings.google_sheet_id:
            raise RuntimeError("Falta GOOGLE_SHEET_ID")
        if not self.settings.google_service_account_json:
            raise RuntimeError("Falta GOOGLE_SERVICE_ACCOUNT_JSON")
        try:
            info = json.loads(self.settings.google_service_account_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON no contiene JSON valido") from exc
        credentials = Credentials.from_service_account_info(info, scopes=[SHEETS_SCOPE])
        return AuthorizedSession(credentials)

    def export(self, job: SearchJob, prospects: list[Prospect]) -> int:
        if not self.settings.google_sheets_enabled:
            return 0
        rows = build_sheet_rows(job, prospects)
        if not rows:
            return 0

        session = self._authorized_session()
        base = f"https://sheets.googleapis.com/v4/spreadsheets/{self.settings.google_sheet_id}/values"
        header_range = quote(f"'{self.settings.google_sheet_tab}'!A1:Q1", safe="")
        header_response = session.get(f"{base}/{header_range}", timeout=30)
        header_response.raise_for_status()
        existing_header = header_response.json().get("values") or []
        if not existing_header:
            write_header = session.put(
                f"{base}/{header_range}",
                params={"valueInputOption": "RAW"},
                json={"values": [HEADERS]},
                timeout=30,
            )
            write_header.raise_for_status()
        elif existing_header[0] != HEADERS:
            raise RuntimeError(
                f"La cabecera de la pestana {self.settings.google_sheet_tab} no coincide con el esquema esperado"
            )

        append_range = quote(f"'{self.settings.google_sheet_tab}'!A:Q", safe="")
        append_response = session.post(
            f"{base}/{append_range}:append",
            params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
            json={"majorDimension": "ROWS", "values": rows},
            timeout=30,
        )
        append_response.raise_for_status()
        return len(rows)

