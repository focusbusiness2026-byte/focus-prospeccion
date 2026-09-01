from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.dedupe import company_dedupe_key
from app.models import Client, Prospect, Quota, SearchJob, SearchResult, SearchStatus, utcnow
from app.scoring import score_company
from app.sheets import GoogleSheetsExporter
from app.sources import OverpassSource


def launch_search(session: Session, client_id: str, filters: dict, source_mode: str | None = None) -> SearchJob:
    quota = session.get(Quota, client_id)
    if not quota or quota.launches_available <= 0:
        raise ValueError("El cliente no tiene lanzamientos disponibles")
    mode = source_mode or get_settings().source_mode
    job = SearchJob(client_id=client_id, filters=filters, source_mode=mode, quota_charged=True)
    quota.launches_consumed += 1
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def _source(mode: str):
    if mode == "overpass":
        return OverpassSource()
    raise ValueError(f"Fuente no soportada: {mode}")


def run_search(session: Session, job: SearchJob) -> SearchJob:
    if job.status not in {SearchStatus.PENDING.value, SearchStatus.RUNNING.value}:
        raise ValueError(f"La busqueda no se puede ejecutar desde el estado {job.status}")
    job.status = SearchStatus.RUNNING.value
    job.started_at = job.started_at or utcnow()
    job.error = None
    session.commit()
    try:
        candidates = _source(job.source_mode).discover(job.filters)
        ranked: list[tuple[Prospect, float]] = []
        for candidate in candidates:
            score = score_company(candidate)
            key = company_dedupe_key(candidate)
            prospect = session.scalar(
                select(Prospect).where(Prospect.client_id == job.client_id, Prospect.dedupe_key == key)
            )
            if not prospect:
                prospect = Prospect(
                    client_id=job.client_id,
                    dedupe_key=key,
                    legal_name=candidate.get("legal_name") or candidate.get("commercial_name") or "Sin nombre",
                    commercial_name=candidate.get("commercial_name") or candidate.get("legal_name") or "Sin nombre",
                )
                session.add(prospect)
                session.flush()
            prospect.website = candidate.get("website")
            prospect.phone = candidate.get("phone")
            prospect.city = candidate.get("city")
            prospect.sector = candidate.get("sector")
            prospect.employees = candidate.get("employees")
            prospect.revenue_eur = candidate.get("revenue_eur")
            prospect.score = score.total
            prospect.classification = score.classification
            prospect.score_detail = score.as_dict()
            prospect.evidence = candidate.get("evidence") or []
            session.flush()
            existing = session.scalar(
                select(SearchResult).where(SearchResult.search_id == job.id, SearchResult.prospect_id == prospect.id)
            )
            if not existing:
                session.add(SearchResult(search_id=job.id, prospect_id=prospect.id))
            ranked.append((prospect, score.total))

        session.flush()
        for rank, (prospect, _) in enumerate(sorted(ranked, key=lambda row: row[1], reverse=True), start=1):
            result = session.scalar(
                select(SearchResult).where(SearchResult.search_id == job.id, SearchResult.prospect_id == prospect.id)
            )
            result.rank = rank
        GoogleSheetsExporter().export(job, [prospect for prospect, _ in ranked])
        job.results_count = len(ranked)
        job.status = SearchStatus.COMPLETED.value
        job.finished_at = utcnow()
        session.commit()
    except Exception as exc:
        session.rollback()
        job = session.get(SearchJob, job.id)
        job.status = SearchStatus.FAILED.value
        job.error = str(exc)[:1000]
        job.finished_at = utcnow()
        if job.quota_charged and get_settings().refund_failed_searches:
            quota = session.get(Quota, job.client_id)
            quota.launches_consumed = max(0, quota.launches_consumed - 1)
            job.quota_charged = False
        session.commit()
    return job


def claim_next_pending(session: Session) -> SearchJob | None:
    job = session.scalar(
        select(SearchJob)
        .where(SearchJob.status == SearchStatus.PENDING.value)
        .order_by(SearchJob.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job:
        job.status = SearchStatus.RUNNING.value
        job.started_at = utcnow()
        session.commit()
    return job


def dashboard(session: Session, client_id: str) -> dict:
    client = session.get(Client, client_id)
    if not client:
        raise ValueError("Cliente no encontrado")
    quota = session.get(Quota, client_id)
    searches = list(
        session.scalars(select(SearchJob).where(SearchJob.client_id == client_id).order_by(SearchJob.created_at.desc()))
    )
    prospects = list(
        session.scalars(select(Prospect).where(Prospect.client_id == client_id).order_by(Prospect.score.desc()))
    )
    return {
        "client": {"id": client.id, "name": client.name},
        "quota": {
            "total": quota.launches_total,
            "consumed": quota.launches_consumed,
            "available": quota.launches_available,
        },
        "searches": [
            {
                "id": item.id,
                "status": item.status,
                "source_mode": item.source_mode,
                "filters": item.filters,
                "results_count": item.results_count,
                "created_at": item.created_at.isoformat(),
                "error": item.error,
            }
            for item in searches
        ],
        "prospects": [
            {
                "id": item.id,
                "name": item.commercial_name,
                "city": item.city,
                "sector": item.sector,
                "employees": item.employees,
                "revenue_eur": item.revenue_eur,
                "score": item.score,
                "classification": item.classification,
                "score_detail": item.score_detail,
                "evidence": item.evidence,
            }
            for item in prospects
        ],
    }
