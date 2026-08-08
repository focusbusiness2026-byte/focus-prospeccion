from __future__ import annotations

import time
import argparse

from app.config import get_settings
from app.db import create_schema, session_scope
from app.services import claim_next_pending, run_search


def run_once() -> bool:
    with session_scope() as session:
        job = claim_next_pending(session)
        if not job:
            return False
        run_search(session, job)
        return True


def run_forever() -> None:
    create_schema()
    settings = get_settings()
    while True:
        run_once()
        time.sleep(max(5, settings.worker_poll_seconds))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Procesa como maximo una busqueda pendiente")
    args = parser.parse_args()
    create_schema()
    if args.once:
        raise SystemExit(0 if run_once() else 2)
    run_forever()
