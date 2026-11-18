"""Production adapters for the guided form. CRM and research keep their existing APIs."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from functools import lru_cache
from threading import RLock

from fastapi import HTTPException

from app.auth import require_identity
from app.config import get_settings
from app.prospecting_guide import (
    BUSINESS_CONTEXT_FIELDS,
    SESSION_SCHEMA,
    OpenAITargetingGuide,
    TargetingGuide,
    guide_intake,
    review_feedback,
)
from app.prospecting_guide_api import guide_router
from app.sheet_store import SheetStore


GUIDE_TAB = "Prospeccion Guiada"
GUIDE_STATE_ROWS = 2000
GUIDE_TABLES = ("targeting_sessions", "targeting_requests")
_service_lock = RLock()


class GuideStore:
    """Store guided drafts in their own tab, created on the first confirmed write."""

    def __init__(self, sheets):
        self.sheets = sheets
        self.lock = RLock()
        self.uncertain = False
        self.sheet_id = self._find_tab()
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SESSION_SCHEMA)
        if self.sheet_id is not None:
            self._restore()

    def _find_tab(self):
        return next((item["sheetId"] for item in self.sheets._sheet_properties()
                     if item["title"] == GUIDE_TAB), None)

    def _restore(self):
        rows = self.sheets._get(f"'{GUIDE_TAB}'!A1:A{GUIDE_STATE_ROWS}")
        if not rows or not rows[0]:
            return
        manifest = json.loads(rows[0][0])
        count = manifest["chunks"]
        if not isinstance(count, int) or not 1 <= count < GUIDE_STATE_ROWS or len(rows) < count + 1:
            raise RuntimeError("Estado de la guía incompleto")
        text = "".join(row[0] for row in rows[1:count + 1] if row)
        if hashlib.sha256(text.encode()).hexdigest() != manifest["sha256"]:
            raise RuntimeError("No se pudo verificar el estado de la guía")
        snapshot = json.loads(text)
        with self.connection as connection:
            connection.executemany("INSERT INTO targeting_sessions VALUES (?, ?, ?, ?)", snapshot["targeting_sessions"])
            connection.executemany("INSERT INTO targeting_requests VALUES (?, ?, ?, ?, ?, ?)", snapshot["targeting_requests"])

    def _save(self, connection):
        if self.uncertain:
            raise RuntimeError("Guardado de la guía pendiente de revisión")
        snapshot = {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                    for table in GUIDE_TABLES}
        text = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
        chunks = [text[offset:offset + 24000] for offset in range(0, len(text), 24000)]
        if len(chunks) >= GUIDE_STATE_ROWS:
            raise RuntimeError("El almacenamiento de la guía alcanzó su capacidad")
        manifest = json.dumps({"sha256": hashlib.sha256(text.encode()).hexdigest(), "chunks": len(chunks)})
        rows = [{"values": [{"userEnteredValue": {"stringValue": value}}]} for value in [manifest, *chunks]]
        try:
            if self.sheet_id is None:
                self.sheets._batch_update([{"addSheet": {"properties": {
                    "title": GUIDE_TAB, "hidden": True,
                    "gridProperties": {"rowCount": GUIDE_STATE_ROWS, "columnCount": 1},
                }}}])
                self.sheet_id = self._find_tab()
                if self.sheet_id is None:
                    raise RuntimeError("No se pudo verificar la pestaña de la guía")
            self.sheets._batch_update([{"updateCells": {
                "range": {"sheetId": self.sheet_id, "startRowIndex": 0, "endRowIndex": GUIDE_STATE_ROWS,
                          "startColumnIndex": 0, "endColumnIndex": 1},
                "rows": rows, "fields": "userEnteredValue",
            }}])
        except Exception as error:
            self.uncertain = True
            raise RuntimeError("No se pudo confirmar el guardado de la guía") from error

    @contextmanager
    def db(self):
        with self.lock, self.connection:
            previous = self.connection.total_changes
            yield self.connection
            if self.connection.total_changes != previous:
                self._save(self.connection)


@lru_cache(maxsize=1)
def _load_service():
    settings = get_settings()
    store = GuideStore(SheetStore(settings))
    generator = OpenAITargetingGuide(settings) if settings.openai_api_key else None
    return TargetingGuide(store, generator)


def service():
    with _service_lock:
        return _load_service()


def source_for(tenant, identity, store=None):
    from app.main import _account_scope_email, _is_authorized_admin

    settings = get_settings()
    if not settings.google_sheets_enabled:
        raise HTTPException(503, "Google Sheets no está disponible")
    store = store or SheetStore(settings)
    access = store.get_access(identity.email)
    if not access:
        raise HTTPException(403, "Acceso retirado en Google Sheets")
    scope = None if _is_authorized_admin(identity, access, settings) else _account_scope_email(identity, access)
    source = store.get_onboarding_source(tenant, scope)
    if not source:
        raise HTTPException(404, "No se encontró la productora")
    return source


def profile(source):
    from app.main import _questionnaire_source_profile

    result = _questionnaire_source_profile(source)
    result["business_context"] = {key: source.raw_form[key] for key in BUSINESS_CONTEXT_FIELDS if source.raw_form.get(key)}
    return result


def guide_profile(tenant, identity):
    return profile(source_for(tenant, identity))


def intake(tenant, identity):
    source = source_for(tenant, identity)
    return profile(source), guide_intake(source, editable=False)


def feedback(tenant, identity):
    store = SheetStore(get_settings())
    source = source_for(tenant, identity, store)
    return review_feedback(store.prospect_feedback_rows(source.email, source.record_id), source.record_id, source.email)


def save(tenant, identity, name, criteria):
    from app.main import ResearchAdjustments, _PORTAL_SHEETS_LOCK

    with _PORTAL_SHEETS_LOCK:
        store = SheetStore(get_settings())
        source = source_for(tenant, identity, store)
        return store.upsert_saved_search(
            source.record_id, source.email, name=name,
            adjustments=ResearchAdjustments.model_validate(criteria).model_dump(),
            favorite=False, created_by_email=identity.email, created_by_role=identity.role,
        )


def research(record_id, criteria, request, background_tasks, identity):
    from app.main import ResearchAdjustments, start_research_job

    return start_research_job(record_id, ResearchAdjustments.model_validate(criteria),
                                   request, background_tasks, identity)


def job(tenant, identity):
    from app.main import latest_manual_research_job

    store = SheetStore(get_settings())
    source = source_for(tenant, identity, store)
    result = latest_manual_research_job(source.record_id, identity.email)
    ids = {lead["execution_id"] for lead in (result or {}).get("saved_leads") or []}
    if result and result["status"] == "completed" and ids:
        result["highlights"] = [
            {"company": lead["company"], "facts": [lead[key] for key in ("sector", "city", "country") if lead.get(key)],
             "finding": lead.get("summary") or "", "potential": lead.get("entry_angle") or ""}
            for lead in store.recent_prospects(source.email, limit=1000)
            if lead["onboarding_id"] == source.record_id and lead["execution_id"] in ids
        ]
    return result


router = guide_router(service, require_identity, guide_profile, save, feedback, intake, research, job)
