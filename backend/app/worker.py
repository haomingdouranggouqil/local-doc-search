from __future__ import annotations

import signal
import threading

from .config import get_settings
from .database import Database
from .indexer import DocumentIndexer
from .resources import ResourcePolicy
from .scanner import run_worker_loop
from .vector_search import VectorSearchService


def main() -> None:
    settings = get_settings()
    db = Database(settings.db_path, settings.sqlite_journal_mode)
    resources = ResourcePolicy(settings)
    indexer = DocumentIndexer(settings, db, resources)
    vector_indexer = VectorSearchService(settings, db)
    stop_event = threading.Event()

    def stop(*_args) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    requeued = db.requeue_interrupted_jobs()
    if requeued:
        db.record_event("job_requeue", f"Requeued interrupted jobs: {requeued}")
    run_worker_loop(indexer, db, stop_event, vector_indexer)


if __name__ == "__main__":
    main()
