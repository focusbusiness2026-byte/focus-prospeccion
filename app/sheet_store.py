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
            f"'{self.settings.google_sheet_tab}'!A:V",
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
            ]],
        )

    def recent_prospects(self, email: str, limit: int = 20) -> list[dict]:
        rows = self._get(f"'{self.settings.google_sheet_tab}'!A2:V500")
        matches = [row for row in rows if len(row) > 1 and str(row[1]).strip().lower() == email.strip().lower()]
        output = []
        for row in reversed(matches[-limit:]):
            padded = row + [""] * (22 - len(row))
            output.append(
                {
                    "execution_id": padded[0],
                    "created_at": padded[2],
                    "company": padded[3],
                    "website": padded[4],
                    "sector": padded[7],
                    "score": padded[11],
                    "classification": padded[12],
                    "summary": padded[13],
                    "entry_angle": padded[14],
                }
            )
        return output

    def global_metrics(self) -> dict:
        records = [record for record in self.access_records() if record.state.lower() == "activo"]
        assigned = sum(record.assigned for record in records)
        used = sum(record.used for record in records)
        execution_rows = self._get(f"'{self.settings.google_executions_tab}'!F2:F1000")
        completed_requests = sum(
            1 for row in execution_rows if row and str(row[0]).strip().lower() == "completado"
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
        }
