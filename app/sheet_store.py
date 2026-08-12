from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

from google.auth.transport.requests import AuthorizedSession
from google.oauth2.service_account import Credentials

from app.config import Settings, get_settings
from app.onboarding import OnboardingSource


SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
_quota_lock = threading.Lock()

PROSPECT_HEADERS = [
    "execution_id", "email", "created_at", "company", "website", "title", "description", "sector",
    "business_model", "city", "employees", "score", "classification", "summary", "entry_angle",
    "linkedin", "instagram", "facebook", "x", "youtube", "tiktok", "evidence", "lead_status", "updated_at",
    "onboarding_id", "productora", "search_queries", "web_search_calls", "web_search_call_limit",
    "public_contacts_json", "research_sources_json", "no_contacts_reason", "prospect_found",
    "no_prospect_reason", "crm_owner", "crm_notes", "crm_next_action", "crm_follow_up_date",
    "public_signals_json", "public_signals_status", "country", "client_type", "decision_makers_json",
    "warmup_preparation", "warmup_approval",
]
EXECUTION_HEADERS = [
    "execution_id", "created_at", "email", "company", "website", "status", "model", "prompt_tokens",
    "output_tokens", "total_tokens", "error", "onboarding_id", "productora", "web_search_calls",
    "web_search_call_limit", "search_queries_json", "research_sources_json", "no_prospect_reason",
    "research_summary", "search_configuration_json", "adjustments_json", "research_provider",
    "search_trace_json", "duplicates_discarded",
]
DASHBOARD_HEADERS = [
    "updated_at", "active_users", "prospects", "new", "approved", "discarded", "executions_completed",
    "executions_failed", "web_search_calls", "openai_configured", "web_search_limit_per_execution",
]
AUTOMATION_HEADERS = [
    "onboarding_id", "email", "enabled", "interval_minutes", "next_run_at", "last_run_at",
    "last_status", "updated_at", "last_execution_id", "adjustments_json",
]


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

    @property
    def _spreadsheet_base(self) -> str:
        return f"https://sheets.googleapis.com/v4/spreadsheets/{self.settings.google_sheet_id}"

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

    def _sheet_properties(self) -> list[dict]:
        session = self._session()
        response = session.get(
            self._spreadsheet_base,
            params={"fields": "sheets.properties(sheetId,title,gridProperties(columnCount,rowCount))"},
            timeout=30,
        )
        response.raise_for_status()
        return [item.get("properties", {}) for item in response.json().get("sheets", [])]

    def _batch_update(self, requests: list[dict]) -> None:
        session = self._session()
        response = session.post(
            f"{self._spreadsheet_base}:batchUpdate",
            json={"requests": requests},
            timeout=30,
        )
        response.raise_for_status()

    def _ensure_sheet_capacity(self, tab: str, minimum_columns: int) -> None:
        properties = next((item for item in self._sheet_properties() if item.get("title") == tab), None)
        if properties is None:
            self._batch_update([
                {
                    "addSheet": {
                        "properties": {
                            "title": tab,
                            "gridProperties": {"rowCount": 1000, "columnCount": minimum_columns},
                        }
                    }
                }
            ])
            return
        current_columns = int(properties.get("gridProperties", {}).get("columnCount") or 0)
        if current_columns < minimum_columns:
            self._batch_update([
                {
                    "appendDimension": {
                        "sheetId": properties["sheetId"],
                        "dimension": "COLUMNS",
                        "length": minimum_columns - current_columns,
                    }
                }
            ])

    @staticmethod
    def _column_name(index: int) -> str:
        output = ""
        while index:
            index, remainder = divmod(index - 1, 26)
            output = chr(65 + remainder) + output
        return output

    def _ensure_header_row(self, tab: str, expected: list[str], legacy_aliases: dict[str, str] | None = None) -> None:
        current_rows = self._get(f"'{tab}'!A1:{self._column_name(len(expected))}1")
        current = [str(value).strip() for value in (current_rows[0] if current_rows else [])]
        while current and not current[-1]:
            current.pop()
        aliases = legacy_aliases or {}
        normalized = [aliases.get(value, value) for value in current]
        for index, (old, new) in enumerate(zip(current, normalized, strict=False), start=1):
            if old != new:
                column = self._column_name(index)
                self._update(f"'{tab}'!{column}1", [[new]])
        current = normalized
        if current == expected:
            return
        if current and current != expected[: len(current)]:
            raise RuntimeError(f"Los encabezados de {tab} no coinciden; aplica la migración documentada antes de continuar")
        missing = expected[len(current):]
        if missing:
            start = self._column_name(len(current) + 1)
            end = self._column_name(len(expected))
            self._update(f"'{tab}'!{start}1:{end}1", [missing])

    def ensure_operational_schema(self) -> None:
        """Expand operational tabs and append missing headers without rewriting data rows."""
        self._ensure_sheet_capacity(self.settings.google_sheet_tab, len(PROSPECT_HEADERS))
        self._ensure_sheet_capacity(self.settings.google_executions_tab, len(EXECUTION_HEADERS))
        self._ensure_sheet_capacity(self.settings.google_automation_tab, len(AUTOMATION_HEADERS))
        self._ensure_header_row(self.settings.google_sheet_tab, PROSPECT_HEADERS)
        self._ensure_header_row(
            self.settings.google_executions_tab,
            EXECUTION_HEADERS,
            legacy_aliases={"gemini_model": "model"},
        )
        self._ensure_header_row(self.settings.google_automation_tab, AUTOMATION_HEADERS)

    def automation_configs(self, email: str | None = None) -> list[dict]:
        rows = self._get(f"'{self.settings.google_automation_tab}'!A2:J1000")
        normalized_email = email.strip().lower() if email else None
        configs: list[dict] = []
        for index, row in enumerate(rows, start=2):
            padded = row + [""] * (10 - len(row))
            owner_email = str(padded[1]).strip().lower()
            if normalized_email and owner_email != normalized_email:
                continue
            try:
                adjustments = json.loads(str(padded[9]) or "{}")
            except json.JSONDecodeError:
                adjustments = {}
            configs.append({
                "row": index,
                "onboarding_id": str(padded[0]).strip(),
                "email": owner_email,
                "enabled": str(padded[2]).strip().lower() in {"true", "1", "si", "sí"},
                "interval_minutes": max(5, min(4320, self._int(padded[3], 1440))),
                "next_run_at": str(padded[4]).strip(),
                "last_run_at": str(padded[5]).strip(),
                "last_status": str(padded[6]).strip(),
                "updated_at": str(padded[7]).strip(),
                "last_execution_id": str(padded[8]).strip(),
                "adjustments": adjustments if isinstance(adjustments, dict) else {},
            })
        return [item for item in configs if item["onboarding_id"]]

    def get_automation_config(self, onboarding_id: str) -> dict | None:
        return next((item for item in self.automation_configs() if item["onboarding_id"] == onboarding_id), None)

    @staticmethod
    def _iso_after(minutes: int) -> str:
        from datetime import timedelta

        return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()

    def upsert_automation_config(
        self,
        onboarding_id: str,
        email: str,
        *,
        enabled: bool,
        interval_minutes: int,
        adjustments: dict | None = None,
    ) -> dict:
        interval = max(5, min(4320, int(interval_minutes)))
        existing = self.get_automation_config(onboarding_id)
        now = datetime.now(timezone.utc).isoformat()
        next_run = self._iso_after(interval) if enabled else ""
        values = [
            onboarding_id,
            email.strip().lower(),
            enabled,
            interval,
            next_run,
            existing["last_run_at"] if existing else "",
            "Programada" if enabled else "Desactivada",
            now,
            existing["last_execution_id"] if existing else "",
            json.dumps(adjustments or {}, ensure_ascii=False),
        ]
        if existing:
            self._update(f"'{self.settings.google_automation_tab}'!A{existing['row']}:J{existing['row']}", [values])
        else:
            self._append(f"'{self.settings.google_automation_tab}'!A:J", [values])
        return {
            "onboarding_id": onboarding_id,
            "email": email.strip().lower(),
            "enabled": enabled,
            "interval_minutes": interval,
            "next_run_at": next_run,
            "last_run_at": existing["last_run_at"] if existing else "",
            "last_status": "Programada" if enabled else "Desactivada",
            "updated_at": now,
            "last_execution_id": existing["last_execution_id"] if existing else "",
            "adjustments": adjustments or {},
        }

    def due_automation_configs(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        due: list[dict] = []
        for item in self.automation_configs():
            if not item["enabled"]:
                continue
            try:
                next_run = datetime.fromisoformat(item["next_run_at"].replace("Z", "+00:00"))
            except (TypeError, ValueError):
                next_run = now
            if next_run.tzinfo is None:
                next_run = next_run.replace(tzinfo=timezone.utc)
            if next_run <= now:
                due.append(item)
        return due

    def mark_automation_run(self, config: dict, status: str, execution_id: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        values = [[
            self._iso_after(config["interval_minutes"]),
            now,
            status[:300],
            now,
            execution_id,
        ]]
        self._update(f"'{self.settings.google_automation_tab}'!E{config['row']}:I{config['row']}", values)

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

    def onboarding_sources(self, email: str | None = None, limit: int = 200) -> list[OnboardingSource]:
        rows = self._get(f"'{self.settings.google_onboarding_tab}'!A1:ZZ1000")
        if not rows:
            return []
        headers = [str(value).strip() for value in rows[0]]
        normalized_email = email.strip().lower() if email else None
        sources: list[OnboardingSource] = []
        seen_ids: set[str] = set()
        for row in reversed(rows[1:]):
            padded = row + [""] * max(0, len(headers) - len(row))
            record = dict(zip(headers, padded, strict=False))
            source = OnboardingSource.from_sheet_record(record)
            if not source.record_id or source.record_id in seen_ids:
                continue
            seen_ids.add(source.record_id)
            if normalized_email and source.email != normalized_email:
                continue
            sources.append(source)
            if len(sources) >= limit:
                break
        return sources

    def get_onboarding_source(self, record_id: str, email: str | None = None) -> OnboardingSource | None:
        normalized_id = record_id.strip()
        return next(
            (source for source in self.onboarding_sources(email=email) if source.record_id == normalized_id),
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
        model: str,
        prompt_tokens: int,
        output_tokens: int,
        total_tokens: int,
        error: str = "",
        onboarding_id: str = "",
        productora: str = "",
        web_search_calls: int = 0,
        web_search_call_limit: int = 5,
        search_queries: list[str] | None = None,
        research_sources: list[dict] | None = None,
        no_prospect_reason: str = "",
        research_summary: str = "",
        search_configuration: dict | None = None,
        adjustments: dict | None = None,
        research_provider: str = "OpenAI Responses API + web_search",
        search_trace: list[dict] | None = None,
        duplicates_discarded: int = 0,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._append(
            f"'{self.settings.google_executions_tab}'!A:X",
            [[
                execution_id, now, email, company, website, status, model, prompt_tokens, output_tokens,
                total_tokens, error, onboarding_id, productora, web_search_calls, web_search_call_limit,
                json.dumps(search_queries or [], ensure_ascii=False),
                json.dumps(research_sources or [], ensure_ascii=False), no_prospect_reason, research_summary,
                json.dumps(search_configuration or {}, ensure_ascii=False), json.dumps(adjustments or {}, ensure_ascii=False),
                research_provider, json.dumps(search_trace or [], ensure_ascii=False), duplicates_discarded,
            ]],
        )

    def append_prospect(self, values: dict) -> None:
        evidence = values.get("evidence") or []
        social = values.get("social_links") or {}
        self._append(
            f"'{self.settings.google_sheet_tab}'!A:AS",
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
                values.get("onboarding_id", ""),
                values.get("productora", ""),
                json.dumps(values.get("search_queries") or [], ensure_ascii=False),
                values.get("web_search_calls", 0),
                values.get("web_search_call_limit", self.settings.web_search_call_limit),
                json.dumps(values.get("public_contacts") or [], ensure_ascii=False),
                json.dumps(values.get("research_sources") or [], ensure_ascii=False),
                values.get("no_contacts_reason", ""),
                bool(values.get("prospect_found", True)),
                values.get("no_prospect_reason", ""),
                values.get("crm_owner", ""),
                values.get("crm_notes", ""),
                values.get("crm_next_action", ""),
                values.get("crm_follow_up_date", ""),
                json.dumps(values.get("public_signals") or [], ensure_ascii=False),
                values.get("public_signals_status", "No encontrado públicamente"),
                values.get("country", ""),
                values.get("client_type", ""),
                json.dumps(values.get("decision_makers") or [], ensure_ascii=False),
                values.get("warmup_preparation", "No iniciada"),
                values.get("warmup_approval", "Pendiente"),
            ]],
        )

    @staticmethod
    def _json_list(value: object) -> list:
        if isinstance(value, list):
            return value
        try:
            parsed = json.loads(str(value or "[]"))
        except (json.JSONDecodeError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []

    @staticmethod
    def _json_object(value: object) -> dict:
        if isinstance(value, dict):
            return value
        try:
            parsed = json.loads(str(value or "{}"))
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _prospect_from_row(row: list) -> dict:
        padded = row + [""] * (45 - len(row))
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
            "onboarding_id": padded[24],
            "productora": padded[25],
            "search_queries": SheetStore._json_list(padded[26]),
            "web_search_calls": SheetStore._int(padded[27]),
            "web_search_call_limit": SheetStore._int(padded[28], 5),
            "public_contacts": SheetStore._json_list(padded[29]),
            "research_sources": SheetStore._json_list(padded[30]),
            "no_contacts_reason": padded[31],
            "prospect_found": str(padded[32]).strip().lower() not in {"false", "0", "no"},
            "no_prospect_reason": padded[33],
            "crm_owner": padded[34],
            "crm_notes": padded[35],
            "crm_next_action": padded[36],
            "crm_follow_up_date": padded[37],
            "public_signals": SheetStore._json_list(padded[38]),
            "public_signals_status": padded[39] or "No encontrado públicamente",
            "country": padded[40],
            "client_type": padded[41],
            "decision_makers": SheetStore._json_list(padded[42]),
            "warmup_preparation": padded[43] or "No iniciada",
            "warmup_approval": padded[44] or "Pendiente",
        }

    def recent_prospects(self, email: str | None, limit: int = 50) -> list[dict]:
        rows = self._get(f"'{self.settings.google_sheet_tab}'!A2:AS1000")
        if email:
            normalized = email.strip().lower()
            rows = [row for row in rows if len(row) > 1 and str(row[1]).strip().lower() == normalized]
        return [self._prospect_from_row(row) for row in reversed(rows[-limit:])]

    def get_prospect(self, execution_id: str, email: str | None = None) -> dict | None:
        normalized_id = execution_id.strip()
        return next(
            (item for item in self.recent_prospects(email, limit=1000) if str(item["execution_id"]).strip() == normalized_id),
            None,
        )

    def delete_prospect(self, execution_id: str) -> dict:
        """Delete one exact prospect row. Callers must enforce administrator authorization."""
        rows = self._get(f"'{self.settings.google_sheet_tab}'!A2:AS1000")
        matches = [
            (row_number, row)
            for row_number, row in enumerate(rows, start=2)
            if row and str(row[0]).strip() == execution_id.strip()
        ]
        if not matches:
            raise LookupError("No se encontro el lead solicitado")
        if len(matches) != 1:
            raise RuntimeError("El identificador del lead no es unico; no se elimino ninguna fila")
        row_number, row = matches[0]
        properties = next(
            (item for item in self._sheet_properties() if item.get("title") == self.settings.google_sheet_tab),
            None,
        )
        if not properties:
            raise RuntimeError("No se encontro la pestaña de prospectos")
        self._batch_update([{
            "deleteDimension": {
                "range": {
                    "sheetId": properties["sheetId"],
                    "dimension": "ROWS",
                    "startIndex": row_number - 1,
                    "endIndex": row_number,
                }
            }
        }])
        return self._prospect_from_row(row)

    def recent_executions(self, email: str | None, limit: int = 20) -> list[dict]:
        rows = self._get(f"'{self.settings.google_executions_tab}'!A2:X1000")
        if email:
            normalized = email.strip().lower()
            rows = [row for row in rows if len(row) > 2 and str(row[2]).strip().lower() == normalized]
        output = []
        for row in reversed(rows[-limit:]):
            padded = row + [""] * (24 - len(row))
            output.append(
                {
                    "execution_id": padded[0],
                    "created_at": padded[1],
                    "email": padded[2],
                    "company": padded[3],
                    "website": padded[4],
                    "status": padded[5],
                    "model": padded[6],
                    "prompt_tokens": self._int(padded[7]),
                    "output_tokens": self._int(padded[8]),
                    "total_tokens": self._int(padded[9]),
                    "error": padded[10],
                    "onboarding_id": padded[11],
                    "productora": padded[12],
                    "web_search_calls": self._int(padded[13]),
                    "web_search_call_limit": self._int(padded[14], 5),
                    "search_queries": self._json_list(padded[15]),
                    "research_sources": self._json_list(padded[16]),
                    "no_prospect_reason": padded[17],
                    "research_summary": padded[18],
                    "search_configuration": self._json_object(padded[19]),
                    "adjustments": self._json_object(padded[20]),
                    "research_provider": padded[21] or "OpenAI Responses API + web_search",
                    "search_trace": self._json_list(padded[22]),
                    "duplicates_discarded": self._int(padded[23]),
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
        rows = self._get(f"'{self.settings.google_sheet_tab}'!A2:AS1000")
        normalized_email = email.strip().lower()
        for row_number, row in enumerate(rows, start=2):
            padded = row + [""] * (45 - len(row))
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

    def update_prospect_crm(
        self,
        execution_id: str,
        email: str,
        *,
        status: str,
        owner: str,
        notes: str,
        next_action: str,
        follow_up_date: str,
        warmup_preparation: str,
        warmup_approval: str,
        is_admin: bool = False,
    ) -> dict:
        rows = self._get(f"'{self.settings.google_sheet_tab}'!A2:AS1000")
        normalized_email = email.strip().lower()
        for row_number, row in enumerate(rows, start=2):
            padded = row + [""] * (45 - len(row))
            if str(padded[0]).strip() != execution_id.strip():
                continue
            if not is_admin and str(padded[1]).strip().lower() != normalized_email:
                raise PermissionError("No puedes modificar un lead de otra cuenta")
            now = datetime.now(timezone.utc).isoformat()
            self._update(f"'{self.settings.google_sheet_tab}'!W{row_number}:X{row_number}", [[status, now]])
            self._update(
                f"'{self.settings.google_sheet_tab}'!AI{row_number}:AL{row_number}",
                [[owner, notes, next_action, follow_up_date]],
            )
            self._update(
                f"'{self.settings.google_sheet_tab}'!AR{row_number}:AS{row_number}",
                [[warmup_preparation, warmup_approval]],
            )
            padded[22:24] = [status, now]
            padded[34:38] = [owner, notes, next_action, follow_up_date]
            padded[43:45] = [warmup_preparation, warmup_approval]
            return self._prospect_from_row(padded)
        raise LookupError("No se encontro el lead solicitado")

    def latest_execution_for_onboarding(self, onboarding_id: str) -> dict | None:
        return next(
            (item for item in self.recent_executions(None, limit=1000) if item["onboarding_id"] == onboarding_id),
            None,
        )

    def existing_prospect_keys(self, onboarding_id: str) -> set[str]:
        from app.dedupe import company_dedupe_key

        return {
            company_dedupe_key({"commercial_name": item.get("company", ""), "website": item.get("website", ""), "city": item.get("city", "")})
            for item in self.recent_prospects(None, limit=1000)
            if item.get("onboarding_id") == onboarding_id
        }

    def refresh_dashboard_summary(self) -> dict:
        """Return the live summary without overwriting the formatted Sheets dashboard."""
        metrics = self.prospect_metrics(None)
        global_metrics = self.global_metrics()
        statuses = metrics["statuses"]
        return {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "active_users": global_metrics["active_users"],
            "prospects": metrics["total"],
            "new": statuses["Nuevo"],
            "approved": statuses["Aprobado"],
            "discarded": statuses["Descartado"],
            "executions_completed": global_metrics["openai_requests_used"],
            "executions_failed": global_metrics["failed_requests"],
            "web_search_calls": global_metrics["openai_web_search_calls_used"],
            "openai_configured": bool(self.settings.openai_api_key),
            "web_search_limit_per_execution": self.settings.web_search_call_limit,
        }

    def global_metrics(self) -> dict:
        records = [record for record in self.access_records() if record.state.lower() == "activo"]
        assigned = sum(record.assigned for record in records)
        used = sum(record.used for record in records)
        execution_rows = self._get(f"'{self.settings.google_executions_tab}'!F2:F1000")
        completed_requests = sum(
            1 for row in execution_rows if row and str(row[0]).strip().lower().startswith("completado")
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
            "openai_internal_budget": self.settings.openai_request_budget,
            "openai_requests_used": completed_requests,
            "openai_requests_remaining": max(0, self.settings.openai_request_budget - completed_requests),
            "openai_web_search_calls_used": sum(
                self._int(row[0]) for row in self._get(f"'{self.settings.google_executions_tab}'!N2:N1000") if row
            ),
            "failed_requests": failed_requests,
        }
