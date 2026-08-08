import json

from app.models import Prospect, SearchJob
from app.sheets import HEADERS, build_sheet_rows


def test_sheet_rows_have_stable_schema_and_traceability():
    job = SearchJob(id="search-1", client_id="client-a", filters={"city": "Madrid"}, source_mode="fixture")
    prospect = Prospect(
        id="prospect-1",
        client_id="client-a",
        dedupe_key="domain:example.com",
        legal_name="Empresa Ejemplo S.L.",
        commercial_name="Empresa Ejemplo",
        city="Madrid",
        sector="SaaS",
        score=8.5,
        classification="green",
        evidence=[{"source": "BORME", "url": "https://example.test/evidence", "observed_at": "2026-08-08"}],
    )

    rows = build_sheet_rows(job, [prospect])

    assert len(HEADERS) == 17
    assert len(rows[0]) == len(HEADERS)
    assert rows[0][0:4] == ["search-1", "client-a", "Empresa Ejemplo", "Empresa Ejemplo S.L."]
    assert rows[0][12:15] == ["BORME", "https://example.test/evidence", "2026-08-08"]
    assert json.loads(rows[0][15]) == {"city": "Madrid"}
