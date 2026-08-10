from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    database_url: str = "sqlite:///./data/focus_prospeccion.db"
    source_mode: str = "fixture"
    allow_external_sources: bool = False
    demo_auth_bypass: bool = True
    focus_admin_emails: str = ""
    app_secret: str = "change-me-in-production"
    public_base_url: str = "http://127.0.0.1:8000"
    google_oauth_client_id: str = ""
    worker_poll_seconds: int = 20
    refund_failed_searches: bool = True
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    overpass_user_agent: str = "FocusBusinessProspeccion/0.1 (contacto pendiente)"
    google_sheets_enabled: bool = False
    google_sheet_id: str = ""
    google_sheet_tab: str = "Prospeccion"
    google_access_tab: str = "Accesos"
    google_onboarding_tab: str = "Onboarding"
    google_executions_tab: str = "Ejecuciones"
    google_automation_tab: str = "Automatizaciones"
    google_dashboard_tab: str = "Dashboard Prospeccion"
    google_service_account_json: str = ""
    openai_api_key: str = ""
    openai_model: str = "gpt-5.5"
    openai_request_budget: int = 500
    openai_web_search_max_calls: int = 5
    auto_research_enabled: bool = True
    auto_research_poll_seconds: int = 60
    prospection_trigger_token: str = ""
    web_scraper_max_pages: int = 4

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def admin_emails(self) -> set[str]:
        return {
            email.strip().lower()
            for email in self.focus_admin_emails.split(",")
            if email.strip()
        }

    @property
    def web_search_call_limit(self) -> int:
        return min(5, max(1, self.openai_web_search_max_calls))


@lru_cache
def get_settings() -> Settings:
    return Settings()
