from app.scoring import score_company


def test_priority_company_reaches_green():
    result = score_company(
        {
            "employees": 68,
            "employee_trend": "growing",
            "revenue_trend": "growing",
            "capital_event": True,
            "buying_signals": ["funding", "expansion", "commercial_hiring"],
            "decision_access": "active",
            "decision_recent": True,
        }
    )
    assert result.total == 9.0
    assert result.classification == "green"


def test_disqualifier_overrides_score():
    result = score_company({"employees": 80, "disqualifiers": ["liquidacion"]})
    assert result.total == 0
    assert result.disqualified is True
    assert result.classification == "red"


def test_missing_public_data_is_conservative():
    result = score_company({})
    assert result.total == 2.5
    assert result.classification == "red"

