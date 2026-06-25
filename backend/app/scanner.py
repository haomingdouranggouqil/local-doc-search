from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ModuleNotFoundError:
    class FileSystemEventHandler:
        pass

    Observer = None

from .config import Settings
from .database import Database
from .indexer import document_id_for_rel_path
from .resources import ResourcePolicy


class DocumentScanner:
    def __init__(self, settings: Settings, db: Database, resources: ResourcePolicy):
        self.settings = settings
        self.db = db
        self.resources = resources

    def scan_once(self, retry_failed: bool = False) -> dict[str, int]:
        root = self.settings.document_root.resolve()
        seen: set[str] = set()
        added_or_changed = 0
        skipped = 0
        requeued = 0

        if not root.exists():
            root.mkdir(parents=True, exist_ok=True)

        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if self.is_excluded(path, root):
                skipped += 1
                continue
            if path.suffix.lower() not in self.settings.supported_suffixes:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            rel_path = path.relative_to(root).as_posix()
            document_id = document_id_for_rel_path(rel_path)
            max_file_mb = self.resources.effective_max_file_mb()
            if stat.st_size > max_file_mb * 1024 * 1024:
                error = (
                    f"File is too large for automatic OCR: "
                    f"{stat.st_size // (1024 * 1024)}MB > {max_file_mb}MB"
                )
                changed = self.db.upsert_skipped_document(
                    {
                        "id": document_id,
                        "path": str(path),
                        "rel_path": rel_path,
                        "title": path.name,
                        "ext": path.suffix.lower(),
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                        "sha256": quick_fingerprint(path, stat.st_size, stat.st_mtime),
                    },
                    error,
                )
                seen.add(document_id)
                skipped += 1
                if changed:
                    self.db.record_event("error", f"Skipped: {rel_path}; {error}", document_id, rel_path)
                continue

            seen.add(document_id)
            changed = self.db.upsert_document(
                {
                    "id": document_id,
                    "path": str(path),
                    "rel_path": rel_path,
                    "title": path.name,
                    "ext": path.suffix.lower(),
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "sha256": quick_fingerprint(path, stat.st_size, stat.st_mtime),
                }
            )
            if changed:
                added_or_changed += 1
                self.db.enqueue_job(document_id)
                self.db.record_event("change", f"Changed: {rel_path}", document_id, rel_path)
            elif retry_failed and self.db.requeue_unsuccessful_document(document_id):
                requeued += 1
                self.db.record_event("retry", f"Requeued failed document: {rel_path}", document_id, rel_path)

        deleted = self.db.mark_missing_deleted(seen)
        return {
            "changed": added_or_changed,
            "deleted": deleted,
            "skipped": skipped,
            "requeued": requeued,
        }

    def categories(self) -> list[dict[str, str]]:
        root = self.settings.document_root.resolve()
        if not root.exists():
            root.mkdir(parents=True, exist_ok=True)
        categories = [{"path": "", "label": "全部资料"}]
        for path in sorted((item for item in root.rglob("*") if item.is_dir()), key=lambda item: item.as_posix()):
            if self.is_excluded(path, root):
                continue
            rel_path = path.relative_to(root).as_posix()
            categories.append({"path": rel_path, "label": rel_path})
        return categories

    def is_excluded(self, path: Path, root: Path) -> bool:
        try:
            rel = path.relative_to(root)
        except ValueError:
            return True
        rel_posix = rel.as_posix()
        parts = set(rel.parts)
        if parts & self.settings.exclude_names:
            return True
        return any(
            rel_posix == excluded or rel_posix.startswith(f"{excluded}/")
            for excluded in self.settings.excluded_rel_paths
        )


def quick_fingerprint(path: Path, size: int, mtime: float) -> str:
    digest = hashlib.sha1()
    digest.update(str(size).encode())
    digest.update(str(mtime).encode())
    try:
        with path.open("rb") as handle:
            digest.update(handle.read(1024 * 1024))
            if size > 2 * 1024 * 1024:
                handle.seek(max(0, size - 1024 * 1024))
                digest.update(handle.read(1024 * 1024))
    except OSError:
        pass
    return digest.hexdigest()


class DebouncedScanHandler(FileSystemEventHandler):
    def __init__(self, callback, delay: float):
        self.callback = callback
        self.delay = delay
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def on_any_event(self, event):
        if event.is_directory:
            return
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self.delay, self.callback)
            self._timer.daemon = True
            self._timer.start()


def run_scan_loop(scanner: DocumentScanner, stop_event: threading.Event) -> None:
    scanner.scan_once()
    if Observer is None:
        while not stop_event.wait(scanner.settings.scan_interval_seconds):
            scanner.scan_once()
        return
    handler = DebouncedScanHandler(scanner.scan_once, scanner.settings.scan_debounce_seconds)
    observer = Observer()
    observer.schedule(handler, str(scanner.settings.document_root), recursive=True)
    observer.start()
    try:
        while not stop_event.wait(scanner.settings.scan_interval_seconds):
            scanner.scan_once()
    finally:
        observer.stop()
        observer.join(timeout=10)


def run_worker_loop(indexer, db: Database, stop_event: threading.Event, vector_indexer=None) -> None:
    vector_limit = 1
    if vector_indexer is not None:
        vector_limit = max(1, int(getattr(vector_indexer.settings, "vector_job_concurrency", 1) or 1))
    active_vector_jobs = {}
    with ThreadPoolExecutor(max_workers=vector_limit) as vector_executor:
        while not stop_event.is_set():
            done = [future for future in active_vector_jobs if future.done()]
            for future in done:
                active_vector_jobs.pop(future, None)
                try:
                    future.result()
                except Exception as exc:
                    if exc.__class__.__name__ == "VectorRemoteUnavailable":
                        stop_event.wait(120)
                    else:
                        time.sleep(1)

            if len(active_vector_jobs) >= vector_limit:
                wait(active_vector_jobs.keys(), timeout=1.0, return_when=FIRST_COMPLETED)
                continue

            job = db.claim_next_job()
            if job is None:
                stop_event.wait(1.5)
                continue
            try:
                if job["type"] == "vector":
                    if vector_indexer is None:
                        db.update_job(
                            job["id"],
                            status="failed",
                            progress=1,
                            message="Fuzzy index unavailable",
                            error="vector indexer is not configured",
                        )
                    else:
                        future = vector_executor.submit(
                            vector_indexer.process_document,
                            job["document_id"],
                            job["id"],
                        )
                        active_vector_jobs[future] = job["id"]
                else:
                    indexer.process(job["document_id"], job["id"])
            except Exception as exc:
                if exc.__class__.__name__ == "VectorRemoteUnavailable":
                    stop_event.wait(120)
                else:
                    time.sleep(1)

        for future in active_vector_jobs:
            future.cancel()
