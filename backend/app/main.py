from __future__ import annotations

import os
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response

from .config import get_settings
from .database import Database
from .indexer import DocumentIndexer
from .pdf_preview import render_highlighted_page_png
from .quota import current_quota_day
from .resources import ResourcePolicy
from .scanner import DocumentScanner, run_scan_loop, run_worker_loop

settings = get_settings()
db = Database(settings.db_path, settings.sqlite_journal_mode)
resources = ResourcePolicy(settings)
scanner = DocumentScanner(settings, db, resources)
indexer = DocumentIndexer(settings, db, resources)
stop_event = threading.Event()
threads: list[threading.Thread] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop_event.clear()
    targets = []
    if settings.app_role in {"api", "all"}:
        targets.append((lambda: run_scan_loop(scanner, stop_event), "scanner"))
    if settings.app_role in {"worker", "all"}:
        requeued = db.requeue_interrupted_jobs()
        if requeued:
            db.record_event("job_requeue", f"Requeued interrupted OCR jobs: {requeued}")
        targets.append((lambda: run_worker_loop(indexer, db, stop_event), "worker"))

    for target, name in targets:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        threads.append(thread)
    yield
    stop_event.set()
    for thread in threads:
        thread.join(timeout=10)


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "document_root": str(settings.document_root), "state_dir": str(settings.state_dir)}


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    data = db.stats()
    quota_day = current_quota_day(settings.paddleocr_quota_timezone)
    quota_usage = db.ocr_usage_for_day(quota_day.date, quota_day.start_utc, quota_day.end_utc)
    daily_limit = max(0, int(settings.paddleocr_daily_page_limit or 0))
    used_pages = int(quota_usage["used_pages"])
    data["ocr"] = {
        "engine": settings.ocr_engine,
        "model": settings.paddleocr_api_model,
        "configured_device": settings.ocr_device,
        "actual_device": indexer.ocr_engine.actual_device,
        "quota": {
            "date": quota_day.date,
            "timezone": quota_day.timezone,
            "used_pages": used_pages,
            "daily_limit_pages": daily_limit,
            "remaining_pages": max(0, daily_limit - used_pages) if daily_limit else None,
            "source": quota_usage["source"],
        },
    }
    data["resources"] = resources.as_dict()
    return data


@app.post("/api/scan")
def scan(retry_failed: bool = Query(default=False)) -> dict[str, int]:
    return scanner.scan_once(retry_failed=retry_failed)


@app.get("/api/search")
def search(
    q: str = Query(default="", min_length=1),
    scope: str = Query(default=""),
    mode: str = Query(default="basic"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    page = db.search_page(q, limit=limit, offset=offset, scope=scope, mode=mode)
    return {
        "query": q,
        "scope": scope,
        "mode": mode,
        **page,
        "results": [dict(row) for row in page["results"]],
    }


@app.get("/api/search/groups")
def search_groups(
    q: str = Query(default="", min_length=1),
    scope: str = Query(default=""),
    mode: str = Query(default="basic"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    page = db.search_groups_page(q, limit=limit, offset=offset, scope=scope, mode=mode)
    return {"query": q, "scope": scope, "mode": mode, **page}


@app.get("/api/search/document/{document_id}")
def search_document(
    document_id: str,
    q: str = Query(default="", min_length=1),
    mode: str = Query(default="basic"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    if db.get_document(document_id) is None:
        raise HTTPException(status_code=404, detail="document not found")
    page = db.search_document_page(
        q, document_id=document_id, limit=limit, offset=offset, mode=mode
    )
    return {"query": q, "document_id": document_id, "mode": mode, **page}


@app.get("/api/categories")
def categories() -> dict[str, Any]:
    return {"categories": scanner.categories()}


@app.get("/api/documents")
def documents(limit: int = Query(default=200, ge=1, le=1000)) -> dict[str, Any]:
    return {"documents": [dict(row) for row in db.list_documents(limit)]}


@app.get("/api/documents/{document_id}")
def document(document_id: str) -> dict[str, Any]:
    row = db.get_document(document_id)
    if row is None:
        raise HTTPException(status_code=404, detail="document not found")
    return dict(row)


@app.get("/api/documents/{document_id}/text")
def text_context(
    document_id: str,
    match_id: int | None = Query(default=None),
) -> dict[str, Any]:
    try:
        return db.text_context(document_id, match_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="document not found")


@app.get("/api/files/{document_id}/original")
def original_file(document_id: str):
    row = db.get_document(document_id)
    if row is None:
        raise HTTPException(status_code=404, detail="document not found")
    path = Path(row["path"])
    ensure_allowed(path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="file missing")
    return FileResponse(path, filename=path.name)


@app.post("/api/files/{document_id}/open")
def open_original_file(document_id: str) -> dict[str, Any]:
    if not settings.local_open_enabled:
        raise HTTPException(status_code=403, detail="local file opening is disabled")
    row = db.get_document(document_id)
    if row is None:
        raise HTTPException(status_code=404, detail="document not found")
    path = Path(row["path"])
    ensure_allowed(path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="file missing")
    try:
        result = open_with_system(path)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"ok": True, "path": str(path), **result}


@app.get("/api/files/{document_id}/pdf")
def pdf_file(document_id: str):
    row = db.get_document(document_id)
    if row is None:
        raise HTTPException(status_code=404, detail="document not found")
    path = pdf_preview_path(row)
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/pdf",
        content_disposition_type="inline",
    )


@app.get("/api/files/{document_id}/page-image")
def pdf_page_image(
    document_id: str,
    page: int = Query(ge=1),
    match_id: int | None = Query(default=None),
    q: str = Query(default=""),
):
    row = db.get_document(document_id)
    if row is None:
        raise HTTPException(status_code=404, detail="document not found")
    path = pdf_preview_path(row)
    match_chunk = db.get_chunk(document_id, match_id) if match_id is not None else None
    page_chunks = db.chunks_for_page(document_id, page)
    try:
        image = render_highlighted_page_png(
            path,
            page,
            match_chunk=match_chunk,
            page_chunks=page_chunks,
            query=q,
        )
    except IndexError:
        raise HTTPException(status_code=404, detail="pdf page not found")
    return Response(
        content=image,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/jobs")
def jobs(limit: int = Query(default=30, ge=1, le=100)) -> dict[str, Any]:
    return {"jobs": [dict(row) for row in db.recent_jobs(limit)]}


@app.get("/api/events")
def events(limit: int = Query(default=30, ge=1, le=100)) -> dict[str, Any]:
    return {"events": [dict(row) for row in db.recent_events(limit)]}


def ensure_allowed(path: Path, allow_state: bool = False) -> None:
    resolved = path.resolve()
    roots = [settings.document_root.resolve()]
    if allow_state:
        roots.append(settings.state_dir.resolve())
    if not any(resolved == root or root in resolved.parents for root in roots):
        raise HTTPException(status_code=403, detail="path is outside allowed roots")


def pdf_preview_path(row) -> Path:
    candidate = row["searchable_pdf"] or (row["path"] if row["ext"] == ".pdf" else None)
    if not candidate:
        raise HTTPException(status_code=404, detail="pdf preview not available")
    path = Path(candidate)
    ensure_allowed(path, allow_state=True)
    if not path.exists():
        raise HTTPException(status_code=404, detail="pdf missing")
    return path


def open_with_system(path: Path) -> dict[str, Any]:
    if Path("/.dockerenv").exists():
        raise RuntimeError("backend is running inside Docker; use the local open helper")
    try:
        if sys.platform.startswith("win"):
            return open_windows_file(path)
        elif sys.platform == "darwin":
            process = subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"method": "open", "pid": process.pid}
        else:
            process = subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"method": "xdg-open", "pid": process.pid}
    except FileNotFoundError as exc:
        raise RuntimeError(f"system opener is not available: {exc}") from exc
    except OSError as exc:
        raise RuntimeError(f"failed to open file: {exc}") from exc


def open_windows_file(path: Path) -> dict[str, Any]:
    if path.suffix.lower() in {".txt", ".md"}:
        process = subprocess.Popen(
            ["notepad.exe", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        return {"method": "notepad", "pid": process.pid}

    os.startfile(str(path))
    return {"method": "windows-startfile", "pid": None}
