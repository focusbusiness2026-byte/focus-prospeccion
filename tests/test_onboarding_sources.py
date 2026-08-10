from app.config import Settings
from app.onboarding import OnboardingSource
from app.sheet_store import SheetStore


HEADERS = [
    "ID registro",
    "Fecha envío",
    "Estado",
    "Empresa",
    "Web",
    "Actividad",
    "Ciudad / país",
    "Descripción",
    "Servicio prioritario",
    "Servicios",
    "Público",
    "Sectores",
    "Mercados",
    "Ciudad objetivo",
    "Región objetivo",
    "Países objetivo",
    "Tipos de cliente objetivo",
    "Tamaño empresa ideal",
    "Perfil ideal detallado",
    "Decisor habitual",
    "Presupuesto mínimo",
    "Capacidad mensual",
    "Casos de éxito / portafolio",
    "Empresas de referencia",
    "Exclusiones de prospección",
    "Preferencias de prospección",
    "Objetivos",
    "Email responsable",
    "Autorización",
]


def onboarding_row(record_id: str, email: str, website: str = "https://productora.example") -> list:
    return [
        record_id,
        "2026-08-10T10:00:00Z",
        "Nuevo",
        "Productora Norte",
        website,
        "Productora audiovisual",
        "Madrid, España",
        "Vídeo corporativo y publicidad.",
        "Producción audiovisual",
        "Vídeo corporativo, Publicidad",
        "B2B",
        "Tecnología, Industria",
        "España, Europa",
        "Madrid",
        "Comunidad de Madrid",
        "España, Portugal",
        "Empresa privada B2B, Marca con equipo de marketing",
        "11–50 empleados",
        "Empresa B2B con equipo de marketing activo",
        "Dirección de marketing",
        "3.000 €",
        "2–3 proyectos",
        "https://productora.example/portfolio",
        "Marca Uno\nMarca Dos",
        "Clientes actuales y competidores",
        "Marketing activo y contratación reciente",
        "Captar clientes B2B, Aumentar reuniones",
        email,
        "true",
    ]


class OnboardingStore(SheetStore):
    def __init__(self):
        super().__init__(Settings(google_sheet_id="sheet", google_service_account_json="{}"))
        self.rows = [
            HEADERS,
            onboarding_row("ONB-UNO", "owner@example.com"),
            onboarding_row("ONB-DOS", "other@example.com", website=""),
        ]

    def _get(self, a1_range):
        return self.rows if "Onboarding" in a1_range else []


def test_onboarding_record_becomes_normalized_prospecting_source():
    record = dict(zip(HEADERS, onboarding_row("ONB-UNO", "OWNER@example.com"), strict=False))

    source = OnboardingSource.from_sheet_record(record)
    profile = source.prospecting_profile()

    assert source.ready is True
    assert source.email == "owner@example.com"
    assert profile["onboarding_id"] == "ONB-UNO"
    assert profile["productora"]["website"] == "https://productora.example"
    assert profile["targeting"]["sectors"] == ["Tecnología", "Industria"]
    assert profile["targeting"]["markets"] == ["España", "Europa"]
    assert profile["targeting"]["target_countries"] == ["España", "Portugal"]
    assert profile["targeting"]["target_client_types"] == ["Empresa privada B2B", "Marca con equipo de marketing"]
    assert profile["targeting"]["monthly_capacity"] == "2–3 proyectos"
    assert profile["targeting"]["reference_companies"] == ["Marca Uno", "Marca Dos"]
    assert profile["targeting"]["prospect_exclusions"] == "Clientes actuales y competidores"


def test_missing_website_blocks_work_without_inventing_a_fallback():
    record = dict(zip(HEADERS, onboarding_row("ONB-DOS", "owner@example.com", website=""), strict=False))

    source = OnboardingSource.from_sheet_record(record)

    assert source.ready is False
    assert "Falta una web pública válida" in source.blockers
    assert source.prospecting_profile()["productora"]["website"] == ""


def test_sheet_store_scopes_productoras_by_responsible_email():
    store = OnboardingStore()

    own_sources = store.onboarding_sources("OWNER@example.com")
    all_sources = store.onboarding_sources()

    assert [source.record_id for source in own_sources] == ["ONB-UNO"]
    assert [source.record_id for source in all_sources] == ["ONB-DOS", "ONB-UNO"]
