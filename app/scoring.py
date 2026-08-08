from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ScoreResult:
    total: float
    classification: str
    size: float
    financial_health: float
    buying_moment: float
    decision_access: float
    disqualified: bool
    reasons: list[str]

    def as_dict(self) -> dict:
        return asdict(self)


def _size_score(employees: int | None, trend: str | None) -> float:
    if employees is None:
        base = 0.5
    elif employees < 10:
        base = 0.5
    elif employees < 50:
        base = 2.0
    elif employees <= 250:
        base = 2.5
    elif employees <= 1000:
        base = 1.5
    else:
        base = 1.0
    if trend == "growing":
        base += 0.5
    elif trend == "shrinking":
        base -= 0.5
    return max(0.0, min(2.5, base))


def _financial_score(trend: str | None, capital_event: bool, financial_alert: bool) -> float:
    base = {"growing": 2.5, "stable": 1.5, "shrinking": 0.5}.get(trend, 1.0)
    if capital_event:
        base += 0.5
    if financial_alert:
        base -= 1.0
    return max(0.0, min(2.5, base))


def _buying_score(signals: list[str]) -> float:
    return min(2.5, 0.5 * len(set(signals))) if signals else 0.5


def _decision_score(level: str | None, recent_role: bool) -> float:
    base = {"active": 2.5, "passive": 1.5, "registry_only": 0.5}.get(level, 0.5)
    if recent_role:
        base += 0.5
    return min(2.5, base)


def score_company(company: dict) -> ScoreResult:
    disqualifiers = company.get("disqualifiers") or []
    if disqualifiers:
        return ScoreResult(
            total=0.0,
            classification="red",
            size=0.0,
            financial_health=0.0,
            buying_moment=0.0,
            decision_access=0.0,
            disqualified=True,
            reasons=[f"Descalificador: {item}" for item in disqualifiers],
        )

    size = _size_score(company.get("employees"), company.get("employee_trend"))
    financial = _financial_score(
        company.get("revenue_trend"),
        bool(company.get("capital_event")),
        bool(company.get("financial_alert")),
    )
    buying = _buying_score(company.get("buying_signals") or [])
    decision = _decision_score(company.get("decision_access"), bool(company.get("decision_recent")))
    total = round(size + financial + buying + decision, 1)
    classification = "green" if total >= 7 else "yellow" if total >= 4 else "red"
    reasons = [
        f"Tamano: {size}/2.5",
        f"Salud financiera: {financial}/2.5",
        f"Momento de compra: {buying}/2.5",
        f"Accesibilidad: {decision}/2.5",
    ]
    return ScoreResult(total, classification, size, financial, buying, decision, False, reasons)

