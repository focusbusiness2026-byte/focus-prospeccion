from __future__ import annotations

import re
from collections import defaultdict
from urllib.parse import urlsplit


CLIENT_DECISIONS = {"Aprobado", "Descartado", "En revisión"}
ADMIN_DECISIONS = {"Confirmada", "Rechazada", "En revisión"}


def is_admin_role(role: str) -> bool:
    return "admin" in str(role or "").strip().lower()


def normalized_domain(value: str) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").strip(".")
    return host[4:] if host.startswith("www.") else host


def normalized_company(value: str) -> str:
    text = re.sub(r"[^a-z0-9áéíóúüñ]+", " ", str(value or "").casefold())
    text = re.sub(r"\bs\s+l\s+u\b", "slu", text)
    text = re.sub(r"\bs\s+l\b", "sl", text)
    text = re.sub(r"\bs\s+a\s+u\b", "sau", text)
    text = re.sub(r"\bs\s+a\b", "sa", text)
    suffixes = {"sl", "slu", "sa", "sau", "ltd", "llc", "inc", "gmbh"}
    return " ".join(part for part in text.split() if part not in suffixes)


def _latest(events: list[dict], event_type: str) -> dict | None:
    matches = [event for event in events if event.get("event_type") == event_type]
    return max(matches, key=lambda item: str(item.get("created_at") or ""), default=None)


def decorate_prospects(
    prospects: list[dict],
    events: list[dict],
    *,
    require_admin_review: bool = False,
) -> list[dict]:
    """Add review state without broadening the caller's account scope."""
    by_owner_and_execution: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for event in events:
        owner_email = str(event.get("owner_email") or "").strip().lower()
        execution_id = str(event.get("execution_id") or "").strip()
        if owner_email and execution_id:
            by_owner_and_execution[(owner_email, execution_id)].append(event)

    domains: dict[str, list[str]] = defaultdict(list)
    companies: dict[str, list[str]] = defaultdict(list)
    for prospect in prospects:
        execution_id = str(prospect.get("execution_id") or "")
        domain = normalized_domain(prospect.get("website", ""))
        company = normalized_company(prospect.get("company", ""))
        if domain:
            domains[domain].append(execution_id)
        if company:
            companies[company].append(execution_id)

    output = []
    for original in prospects:
        prospect = dict(original)
        owner_email = str(prospect.get("email") or "").strip().lower()
        execution_id = str(prospect.get("execution_id") or "")
        history = sorted(
            by_owner_and_execution.get((owner_email, execution_id), []),
            key=lambda item: str(item.get("created_at") or ""),
        )
        client_decision = _latest(history, "client_decision")
        admin_review = _latest(history, "admin_review")
        domain = normalized_domain(prospect.get("website", ""))
        company = normalized_company(prospect.get("company", ""))
        matched_ids = sorted({
            *([item for item in domains.get(domain, []) if item != execution_id] if domain else []),
            *([item for item in companies.get(company, []) if item != execution_id] if company else []),
        })
        duplicate_criteria = []
        if domain and len(domains[domain]) > 1:
            duplicate_criteria.append("domain")
        if company and len(companies[company]) > 1:
            duplicate_criteria.append("company")

        client_approved = bool(client_decision and client_decision.get("decision") == "Aprobado")
        admin_confirmed = bool(admin_review and admin_review.get("decision") == "Confirmada")
        prospect.update({
            "client_decision": client_decision or {
                "decision": "Pendiente",
                "actor_email": "",
                "actor_role": "",
                "created_at": "",
                "reason": "",
            },
            "admin_review": admin_review or {
                "decision": "Pendiente",
                "actor_email": "",
                "actor_role": "",
                "created_at": "",
                "reason": "",
            },
            "decision_history": history,
            "duplicate_signals": {
                "status": "Revisión necesaria" if matched_ids else "Sin coincidencias visibles",
                "normalized_domain": domain,
                "normalized_company": company,
                "matched_execution_ids": matched_ids,
                "matched_by": duplicate_criteria,
            },
            "external_action_ready": client_approved and (admin_confirmed if require_admin_review else True),
            "admin_review_required": require_admin_review,
        })
        output.append(prospect)
    return output


def summary_request_preview(onboarding_id: str, prospects: list[dict]) -> dict:
    scoped = [item for item in prospects if str(item.get("onboarding_id") or "") == onboarding_id]
    decisions = defaultdict(int)
    for prospect in scoped:
        decisions[str((prospect.get("client_decision") or {}).get("decision") or "Pendiente")] += 1
    return {
        "onboarding_id": onboarding_id,
        "lead_count": len(scoped),
        "decision_counts": dict(sorted(decisions.items())),
        "included_fields": [
            "empresa",
            "datos públicos disponibles",
            "fuentes y fecha",
            "puntuación",
            "señales de duplicado",
            "decisión del cliente",
        ],
        "external_generation_started": False,
        "external_calls": False,
    }
