import json

from fastapi.testclient import TestClient

from app.auth import CSRF_COOKIE, Identity, require_identity
from app.config import Settings
from app.config import get_settings
from app.lead_reviews import decorate_prospects, normalized_company, normalized_domain, summary_request_preview
from app.sheet_store import AccessRecord, LEAD_REVIEW_HEADERS, SheetStore
import app.main as main_module


def prospect(execution_id: str, email: str, company: str, website: str, onboarding_id: str = "ONB-1") -> dict:
    return {
        "execution_id": execution_id,
        "email": email,
        "onboarding_id": onboarding_id,
        "company": company,
        "website": website,
        "lead_status": "Nuevo",
    }


def event(execution_id: str, owner_email: str, event_type: str, decision: str, created_at: str) -> dict:
    return {
        "execution_id": execution_id,
        "owner_email": owner_email,
        "event_type": event_type,
        "decision": decision,
        "created_at": created_at,
        "actor_email": owner_email,
        "actor_role": "Cliente" if event_type == "client_decision" else "Administrador",
    }


def test_review_events_never_cross_accounts_even_when_execution_ids_match():
    leads = [
        prospect("SAME-ID", "alpha@example.com", "Alpha", "https://alpha.example"),
        prospect("SAME-ID", "beta@example.com", "Beta", "https://beta.example"),
    ]
    events = [
        event("SAME-ID", "alpha@example.com", "client_decision", "Aprobado", "2026-08-19T10:00:00Z"),
        event("SAME-ID", "beta@example.com", "client_decision", "Descartado", "2026-08-19T11:00:00Z"),
    ]

    decorated = decorate_prospects(leads, events)

    assert decorated[0]["client_decision"]["decision"] == "Aprobado"
    assert decorated[1]["client_decision"]["decision"] == "Descartado"
    assert len(decorated[0]["decision_history"]) == 1
    assert len(decorated[1]["decision_history"]) == 1


def test_admin_review_does_not_replace_client_decision_and_external_gate_is_explicit():
    lead = prospect("EXEC-1", "client@example.com", "Empresa", "https://empresa.example")
    events = [
        event("EXEC-1", "client@example.com", "client_decision", "Aprobado", "2026-08-19T10:00:00Z"),
        event("EXEC-1", "client@example.com", "admin_review", "En revisión", "2026-08-19T11:00:00Z"),
    ]

    optional_review = decorate_prospects([lead], events, require_admin_review=False)[0]
    required_review = decorate_prospects([lead], events, require_admin_review=True)[0]

    assert optional_review["client_decision"]["decision"] == "Aprobado"
    assert optional_review["admin_review"]["decision"] == "En revisión"
    assert optional_review["external_action_ready"] is True
    assert required_review["external_action_ready"] is False


def test_duplicate_signals_use_normalized_domain_and_company_without_merging():
    leads = [
        prospect("EXEC-1", "client@example.com", "Acme S.L.", "https://www.acme.example/contact"),
        prospect("EXEC-2", "client@example.com", "ACME SL", "acme.example"),
    ]

    decorated = decorate_prospects(leads, [])

    assert normalized_domain("https://www.acme.example/contact") == "acme.example"
    assert normalized_company("Acme S.L.") == "acme"
    assert decorated[0]["duplicate_signals"]["status"] == "Revisión necesaria"
    assert decorated[0]["duplicate_signals"]["matched_execution_ids"] == ["EXEC-2"]
    assert set(decorated[0]["duplicate_signals"]["matched_by"]) == {"domain", "company"}


def test_summary_preview_contains_only_requested_onboarding_account():
    leads = [
        {**prospect("EXEC-1", "client@example.com", "Uno", "https://uno.example", "ONB-1"), "client_decision": {"decision": "Aprobado"}},
        {**prospect("EXEC-2", "client@example.com", "Dos", "https://dos.example", "ONB-2"), "client_decision": {"decision": "Descartado"}},
    ]

    preview = summary_request_preview("ONB-1", leads)

    assert preview["lead_count"] == 1
    assert preview["decision_counts"] == {"Aprobado": 1}
    assert preview["external_generation_started"] is False
    assert preview["external_calls"] is False


class ReviewStore(SheetStore):
    def __init__(self, rows=None):
        super().__init__(Settings(google_sheet_id="sheet", google_service_account_json="{}"))
        self.rows = rows or []
        self.appends = []
        self.schema_calls = 0

    def _sheet_properties(self):
        return [{"sheetId": 9, "title": "Revisiones Leads", "gridProperties": {"columnCount": 13}}]

    def _get(self, a1_range):
        return self.rows if "A2:M2000" in a1_range else [LEAD_REVIEW_HEADERS]

    def _append(self, a1_range, values):
        self.appends.append((a1_range, values))

    def ensure_lead_review_schema(self):
        self.schema_calls += 1


def review_row(event_id: str, owner: str, execution_id: str) -> list:
    return [event_id, "client_decision", "ONB-1", owner, execution_id, owner, "Cliente", "Aprobado", "", "2026-08-19T10:00:00Z", "{}", "Registrada", ""]


def test_sheet_review_reads_are_scoped_by_owner():
    store = ReviewStore([
        review_row("EV-1", "alpha@example.com", "EXEC-1"),
        review_row("EV-2", "beta@example.com", "EXEC-2"),
    ])

    rows = store.review_events("ALPHA@example.com")

    assert [row["event_id"] for row in rows] == ["EV-1"]


def test_sheet_review_append_is_auditable_and_does_not_start_external_work():
    store = ReviewStore()

    saved = store.append_review_event(
        event_type="summary_request",
        onboarding_id="ONB-1",
        owner_email="CLIENT@example.com",
        actor_email="client@example.com",
        actor_role="Cliente",
        decision="Solicitada",
        scope={"lead_count": 3, "external_calls": False},
        result_status="Pendiente de preparación",
    )

    assert store.schema_calls == 1
    assert saved["owner_email"] == "client@example.com"
    assert saved["result_status"] == "Pendiente de preparación"
    assert store.appends[0][0] == "'Revisiones Leads'!A:M"
    assert json.loads(store.appends[0][1][0][10]) == {"lead_count": 3, "external_calls": False}


class ApiStore:
    prospects = {
        "EXEC-ALPHA": prospect("EXEC-ALPHA", "alpha@example.com", "Alpha", "https://alpha.example"),
        "EXEC-BETA": prospect("EXEC-BETA", "beta@example.com", "Beta", "https://beta.example"),
    }
    events = []
    status_updates = []

    def __init__(self, settings=None):
        pass

    def get_access(self, email):
        role = "Administrador" if email == "admin@example.com" else "Cliente"
        return AccessRecord(2, email, role, "Activo", 10, 0)

    def get_prospect(self, execution_id, email=None):
        item = self.prospects.get(execution_id)
        if not item or (email and item["email"] != email):
            return None
        return dict(item)

    def update_prospect_status(self, execution_id, email, status, is_admin=False):
        item = self.get_prospect(execution_id, None if is_admin else email)
        if not item:
            raise PermissionError("Lead de otra cuenta")
        item["lead_status"] = status
        self.prospects[execution_id] = item
        self.status_updates.append((execution_id, email, status, is_admin))
        return dict(item)

    def append_review_event(self, **values):
        saved = {**values, "event_id": f"EV-{len(self.events)+1}", "created_at": "2026-08-19T12:00:00Z"}
        self.events.append(saved)
        return saved

    def review_events(self, owner_email=None, *, onboarding_id=None, execution_id=None):
        rows = self.events
        if owner_email:
            rows = [row for row in rows if row["owner_email"] == owner_email]
        if onboarding_id:
            rows = [row for row in rows if row["onboarding_id"] == onboarding_id]
        if execution_id:
            rows = [row for row in rows if row.get("execution_id") == execution_id]
        return list(rows)

    def get_onboarding_source(self, record_id, email=None):
        owner = "alpha@example.com"
        if record_id != "ONB-1" or (email and email != owner):
            return None
        return type("Source", (), {"email": owner})()

    def recent_prospects(self, email=None, limit=1000):
        rows = list(self.prospects.values())
        return [dict(item) for item in rows if not email or item["email"] == email][:limit]


def api_client(monkeypatch, identity: Identity):
    monkeypatch.setenv("GOOGLE_SHEETS_ENABLED", "true")
    monkeypatch.setenv("FOCUS_ADMIN_EMAILS", "admin@example.com")
    get_settings.cache_clear()
    monkeypatch.setattr(main_module, "SheetStore", ApiStore)
    main_module.app.dependency_overrides[require_identity] = lambda: identity
    client = TestClient(main_module.app)
    client.cookies.set(CSRF_COOKIE, "csrf-test")
    return client


def reset_api_state():
    ApiStore.events = []
    ApiStore.status_updates = []
    ApiStore.prospects = {
        "EXEC-ALPHA": prospect("EXEC-ALPHA", "alpha@example.com", "Alpha", "https://alpha.example"),
        "EXEC-BETA": prospect("EXEC-BETA", "beta@example.com", "Beta", "https://beta.example"),
    }


def test_client_cannot_decide_on_another_accounts_lead(monkeypatch):
    reset_api_state()
    client = api_client(monkeypatch, Identity("alpha@example.com", "Cliente", "alpha"))
    try:
        response = client.post(
            "/api/prospects/EXEC-BETA/decision",
            headers={"X-CSRF-Token": "csrf-test"},
            json={"decision": "Aprobado", "reason": ""},
        )
        assert response.status_code == 404
        assert ApiStore.events == []
        assert ApiStore.status_updates == []
    finally:
        main_module.app.dependency_overrides.clear()
        get_settings.cache_clear()
def test_kanban_status_is_persisted_for_an_active_administrator(monkeypatch):
    reset_api_state()
    client = api_client(monkeypatch, Identity("admin@example.com", "Administrador", "admin"))
    try:
        response = client.post(
            "/api/prospects/EXEC-BETA/status",
            headers={"X-CSRF-Token": "csrf-test"},
            json={"status": "Aprobado para descarga"},
        )
        assert response.status_code == 200
        assert response.json()["prospect"]["lead_status"] == "Aprobado para descarga"
        assert ApiStore.status_updates == [("EXEC-BETA", "admin@example.com", "Aprobado para descarga", True)]
        assert ApiStore.events[-1]["event_type"] == "kanban_status"
    finally:
        main_module.app.dependency_overrides.clear()
        get_settings.cache_clear()
def test_client_cannot_move_another_accounts_kanban_card(monkeypatch):
    reset_api_state()
    client = api_client(monkeypatch, Identity("alpha@example.com", "Cliente", "alpha"))
    try:
        response = client.post(
            "/api/prospects/EXEC-BETA/status",
            headers={"X-CSRF-Token": "csrf-test"},
            json={"status": "Descartado"},
        )
        assert response.status_code == 404
        assert ApiStore.status_updates == []
    finally:
        main_module.app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_client_decision_is_audited_without_external_action(monkeypatch):
    reset_api_state()
    client = api_client(monkeypatch, Identity("alpha@example.com", "Cliente", "alpha"))
    try:
        response = client.post(
            "/api/prospects/EXEC-ALPHA/decision",
            headers={"X-CSRF-Token": "csrf-test"},
            json={"decision": "Aprobado", "reason": "Encaje validado"},
        )
        assert response.status_code == 200
        assert response.json()["external_action_started"] is False
        assert ApiStore.events[0]["event_type"] == "client_decision"
        assert ApiStore.events[0]["actor_email"] == "alpha@example.com"
        assert ApiStore.status_updates == [("EXEC-ALPHA", "alpha@example.com", "Aprobado", False)]
    finally:
        main_module.app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_admin_review_is_separate_and_does_not_overwrite_client_status(monkeypatch):
    reset_api_state()
    client = api_client(monkeypatch, Identity("admin@example.com", "Administrador", "admin"))
    try:
        response = client.post(
            "/api/prospects/EXEC-BETA/decision",
            headers={"X-CSRF-Token": "csrf-test"},
            json={"decision": "Confirmada", "reason": "Revisión administrativa"},
        )
        assert response.status_code == 200
        assert ApiStore.events[0]["event_type"] == "admin_review"
        assert ApiStore.events[0]["owner_email"] == "beta@example.com"
        assert ApiStore.status_updates == []
        assert ApiStore.prospects["EXEC-BETA"]["lead_status"] == "Nuevo"
    finally:
        main_module.app.dependency_overrides.clear()
        get_settings.cache_clear()


