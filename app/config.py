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
    google_executions_tab: str = "Ejecuciones"
    google_dashboard_tab: str = "Dashboard Prospeccion"
    google_service_account_json: str = ""
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash"
    gemini_request_budget: int = 500
    web_scraper_max_pages: int = 4

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def admin_emails(self) -> set[str]:
        return {
            email.strip().lower()
            for email in self.focus_admin_emails.split(",")
            if email.strip()
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
