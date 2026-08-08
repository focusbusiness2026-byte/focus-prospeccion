from app.dedupe import company_dedupe_key


def test_cif_has_priority():
    assert company_dedupe_key({"cif": "B-12345678", "website": "https://example.com"}) == "cif:b12345678"


def test_domain_is_normalized():
    assert company_dedupe_key({"website": "https://www.Example.com/path"}) == "domain:example.com"


def test_name_and_city_fallback_ignores_accents():
    company = {"legal_name": "Óptica del Sur, S.L.", "city": "Málaga"}
    assert company_dedupe_key(company) == "name_city:opticadelsursl:malaga"

