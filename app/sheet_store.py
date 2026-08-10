from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

from google.auth.transport.requests import AuthorizedSession
from google.oauth2.service_account import Credentials

from app.config import Settings, get_settings


SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
_quota_lock = threading.Lock()


@dataclass(frozen=True)
class AccessRecord:
    row: int
    email: str
    role: str
    state: str
    assigned: int
    used: int

    @property
    def available(self) -> int:
        return max(0, self.assigned - self.used)


class SheetStore:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def _session(self) -> AuthorizedSession:
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

    @property
    def _base(self) -> str:
        return f"https://sheets.googleapis.com/v4/spreadsheets/{self.settings.google_sheet_id}/values"

    def _get(self, a1_range: str) -> list[list]:
        session = self._session()
        response = session.get(f"{self._base}/{quote(a1_range, safe='')}", timeout=30)
        response.raise_for_status()
        return response.json().get("values") or []

    def _update(self, a1_range: str, values: list[list]) -> None:
        session = self._session()
        response = session.put(
            f"{self._base}/{quote(a1_range, safe='')}",
            params={"valueInputOption": "USER_ENTERED"},
            json={"range": a1_range, "majorDimension": "ROWS", "values": values},
            timeout=30,
        )
        response.raise_for_status()

    def _append(self, a1_range: str, values: list[list]) -> None:
        session = self._session()
        response = session.post(
            f"{self._base}/{quote(a1_range, safe='')}:append",
            params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
            json={"majorDimension": "ROWS", "values": values},
            timeout=30,
        )
        response.raise_for_status()

    @staticmethod
    def _int(value, fallback: int = 0) -> int:
        try:
            return int(float(str(value).replace(",", ".")))
        except (TypeError, ValueError):
            return fallback

    def access_records(self) -> list[AccessRecord]:
        rows = self._get(f"'{self.settings.google_access_tab}'!A2:I200")
        records: list[AccessRecord] = []
        for index, row in enumerate(rows, start=2):
            padded = row + [""] * (9 - len(row))
            email = str(padded[0]).strip().lower()
            if not email:
                continue
            records.append(
                AccessRecord(
                    row=index,
                    email=email,
                    role=str(padded[1]).strip() or "Cliente",
                    state=str(padded[2]).strip() or "Inactivo",
                    assigned=self._int(padded[4], 0),
                    used=self._int(padded[5], 0),
                )
            )
        return records

    def get_access(self, email: str) -> AccessRecord | None:
        normalized = email.strip().lower()
        return next(
            (record for record in self.access_records() if record.email == normalized and record.state.lower() == "activo"),
            None,
        )

    def reserve_execution(self, email: str) -> AccessRecord:
        with _quota_lock:
            record = self.get_access(email)
            if not record:
                raise PermissionError("Correo no autorizado o inactivo")
            if record.available <= 0:
                raise RuntimeError("No quedan ejecuciones disponibles")
            self._update(f"'{self.settings.google_access_tab}'!F{record.row}", [[record.used + 1]])
            return AccessRecord(record.row, record.email, record.role, record.state, record.assigned, record.used + 1)

    def refund_execution(self, email: str) -> None:
        with _quota_lock:
            record = self.get_access(email)
            if record and record.used > 0:
                self._update(f"'{self.settings.google_access_tab}'!F{record.row}", [[record.used - 1]])

    def append_execution(
        self,
        *,
        execution_id: str,
        email: str,
        company: str,
        website: str,
        status: str,
        gemini_model: str,
        prompt_tokens: int,
        output_tokens: int,
        total_tokens: int,
        error: str = "",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._append(
            f"'{self.settings.google_executions_tab}'!A:K",
            [[execution_id, now, email, company, website, status, gemini_model, prompt_tokens, output_tokens, total_tokens, error]],
        )

    def append_prospect(self, values: dict) -> None:
        evidence = values.get("evidence") or []
        social = values.get("social_links") or {}
        self._append(
            f"'{self.settings.google_sheet_tab}'!A:X",
            [[
                values.get("execution_id", ""),
                values.get("email", ""),
                datetime.now(timezone.utc).isoformat(),
                values.get("company", ""),
                values.get("website", ""),
                values.get("title", ""),
                values.get("description", ""),
                values.get("sector", ""),
                values.get("business_model", ""),
                values.get("city", ""),
                values.get("employees", ""),
                values.get("score", ""),
                values.get("classification", ""),
                values.get("summary", ""),
                values.get("entry_angle", ""),
                social.get("linkedin", ""),
                social.get("instagram", ""),
                social.get("facebook", ""),
                social.get("x", ""),
                social.get("youtube", ""),
                social.get("tiktok", ""),
                "\n".join(evidence),
                "Nuevo",
                datetime.now(timezone.utc).isoformat(),
            ]],
        )

    @staticmethod
    def _prospect_from_row(row: list) -> dict:
        padded = row + [""] * (24 - len(row))
        return {
            "execution_id": padded[0],
            "email": padded[1],
            "created_at": padded[2],
            "company": padded[3],
            "website": padded[4],
            "title": padded[5],
            "description": padded[6],
            "sector": padded[7],
            "business_model": padded[8],
            "city": padded[9],
            "employees": padded[10],
            "score": padded[11],
            "classification": padded[12],
            "summary": padded[13],
            "entry_angle": padded[14],
            "social_links": {
                "linkedin": padded[15],
                "instagram": padded[16],
                "facebook": padded[17],
                "x": padded[18],
                "youtube": padded[19],
                "tiktok": padded[20],
            },
            "evidence": [line for line in str(padded[21]).splitlines() if line.strip()],
            "lead_status": padded[22] or "Nuevo",
            "updated_at": padded[23] or padded[2],
        }

    def recent_prospects(self, email: str | None, limit: int = 50) -> list[dict]:
        rows = self._get(f"'{self.settings.google_sheet_tab}'!A2:X1000")
        if email:
            normalized = email.strip().lower()
            rows = [row for row in rows if len(row) > 1 and str(row[1]).strip().lower() == normalized]
        return [self._prospect_from_row(row) for row in reversed(rows[-limit:])]

    def recent_executions(self, email: str | None, limit: int = 20) -> list[dict]:
        rows = self._get(f"'{self.settings.google_executions_tab}'!A2:K1000")
        if email:
            normalized = email.strip().lower()
            rows = [row for row in rows if len(row) > 2 and str(row[2]).strip().lower() == normalized]
        output = []
        for row in reversed(rows[-limit:]):
            padded = row + [""] * (11 - len(row))
            output.append(
                {
                    "execution_id": padded[0],
                    "created_at": padded[1],
                    "email": padded[2],
                    "company": padded[3],
                    "website": padded[4],
                    "status": padded[5],
                    "gemini_model": padded[6],
                    "prompt_tokens": self._int(padded[7]),
                    "output_tokens": self._int(padded[8]),
                    "total_tokens": self._int(padded[9]),
                    "error": padded[10],
                }
            )
        return output

    def prospect_metrics(self, email: str | None) -> dict:
        prospects = self.recent_prospects(email, limit=1000)
        classifications = {"green": 0, "yellow": 0, "red": 0}
        statuses = {"Nuevo": 0, "Aprobado": 0, "Descartado": 0}
        for prospect in prospects:
            classification = str(prospect["classification"]).strip().lower()
            if classification in classifications:
                classifications[classification] += 1
            status = str(prospect["lead_status"]).strip().capitalize() or "Nuevo"
            if status in statuses:
                statuses[status] += 1
        return {"total": len(prospects), "classifications": classifications, "statuses": statuses}

    def update_prospect_status(self, execution_id: str, email: str, status: str, *, is_admin: bool = False) -> dict:
        rows = self._get(f"'{self.settings.google_sheet_tab}'!A2:X1000")
        normalized_email = email.strip().lower()
        for row_number, row in enumerate(rows, start=2):
            padded = row + [""] * (24 - len(row))
            if str(padded[0]).strip() != execution_id.strip():
                continue
            if not is_admin and str(padded[1]).strip().lower() != normalized_email:
                raise PermissionError("No puedes modificar un lead de otra cuenta")
            now = datetime.now(timezone.utc).isoformat()
            self._update(f"'{self.settings.google_sheet_tab}'!W{row_number}:X{row_number}", [[status, now]])
            padded[22] = status
            padded[23] = now
            return self._prospect_from_row(padded)
        raise LookupError("No se encontro el lead solicitado")

    def global_metrics(self) -> dict:
        records = [record for record in self.access_records() if record.state.lower() == "activo"]
        assigned = sum(record.assigned for record in records)
        used = sum(record.used for record in records)
        execution_rows = self._get(f"'{self.settings.google_executions_tab}'!F2:F1000")
        completed_requests = sum(
            1 for row in execution_rows if row and str(row[0]).strip().lower() == "completado"
        )
        failed_requests = sum(
            1 for row in execution_rows if row and str(row[0]).strip().lower() == "fallido"
        )
        remaining = max(0, assigned - used)
        ratio = remaining / assigned if assigned else 0
        state = "green" if ratio > 0.5 else "yellow" if ratio > 0.2 else "red"
        return {
            "active_users": len(records),
            "assigned": assigned,
            "used": used,
            "remaining": remaining,
            "remaining_ratio": ratio,
            "state": state,
            "gemini_internal_budget": self.settings.gemini_request_budget,
            "gemini_requests_used": completed_requests,
            "gemini_requests_remaining": max(0, self.settings.gemini_request_budget - completed_requests),
            "failed_requests": failed_requests,
        }
