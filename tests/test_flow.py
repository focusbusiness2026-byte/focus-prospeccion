from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Client, Quota
from app.services import claim_next_pending, dashboard, launch_search, run_search


def test_pending_job_is_isolated_and_charges_once():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as session:
        session.add(Client(id="client-a", name="Cliente A"))
        session.add(Quota(client_id="client-a", launches_total=2, launches_consumed=0))
        session.commit()

        job = launch_search(session, "client-a", {"location": "Espana", "sectors": ["SaaS"]}, "overpass")
        assert session.get(Quota, "client-a").launches_consumed == 1
        result = dashboard(session, "client-a")
        assert result["quota"]["available"] == 1
        assert result["prospects"] == []


def test_worker_claim_marks_job_running_before_processing():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as session:
        session.add(Client(id="client-b", name="Cliente B"))
        session.add(Quota(client_id="client-b", launches_total=1, launches_consumed=0))
        session.commit()
        created = launch_search(session, "client-b", {"location": "Espana"}, "overpass")

        claimed = claim_next_pending(session)

        assert claimed.id == created.id
        assert claimed.status == "running"
        assert claim_next_pending(session) is None
