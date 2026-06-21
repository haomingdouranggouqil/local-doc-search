from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .text_normalize import fts_query_expr, normalize_text, search_index_text, search_variants

SEARCH_NORMALIZER_VERSION = "2"
TEXT_EXTRACTOR_VERSION = "2"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path, journal_mode: str = "DELETE"):
        self.path = path
        self.journal_mode = normalize_journal_mode(journal_mode)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.init()

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("PRAGMA foreign_keys=ON")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def init(self) -> None:
        with self._lock, self.connect() as con:
            self._configure_journal_mode(con)
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    path TEXT UNIQUE NOT NULL,
                    rel_path TEXT NOT NULL,
                    title TEXT NOT NULL,
                    ext TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    sha256 TEXT,
                    status TEXT NOT NULL DEFAULT 'new',
                    has_text_layer INTEGER NOT NULL DEFAULT 0,
                    searchable_pdf TEXT,
                    page_count INTEGER NOT NULL DEFAULT 0,
                    text_chars INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    publication_status TEXT NOT NULL DEFAULT 'pending',
                    publication_info TEXT,
                    citation TEXT,
                    publication_checked_at TEXT,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    indexed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id TEXT NOT NULL,
                    page INTEGER,
                    ordinal INTEGER NOT NULL,
                    line INTEGER,
                    text TEXT NOT NULL,
                    bbox TEXT,
                    source TEXT NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 100,
                    progress REAL NOT NULL DEFAULT 0,
                    message TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_status_priority
                    ON jobs(status, priority, created_at);
                CREATE INDEX IF NOT EXISTS idx_chunks_document
                    ON chunks(document_id, page, ordinal);
                CREATE INDEX IF NOT EXISTS idx_documents_deleted_status
                    ON documents(deleted, status);

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,
                    document_id TEXT,
                    path TEXT,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ocr_usage_daily (
                    usage_date TEXT PRIMARY KEY,
                    pages INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._ensure_document_columns(con)

            if not self._fts_exists(con):
                tokenizer = "trigram"
                try:
                    con.execute(
                        """
                        CREATE VIRTUAL TABLE chunks_fts USING fts5(
                            text,
                            chunk_id UNINDEXED,
                            document_id UNINDEXED,
                            rel_path UNINDEXED,
                            page UNINDEXED,
                            tokenize='trigram'
                        )
                        """
                    )
                except sqlite3.OperationalError:
                    tokenizer = "unicode61"
                    con.execute(
                        """
                        CREATE VIRTUAL TABLE chunks_fts USING fts5(
                            text,
                            chunk_id UNINDEXED,
                            document_id UNINDEXED,
                            rel_path UNINDEXED,
                            page UNINDEXED,
                            tokenize='unicode61 remove_diacritics 2'
                        )
                        """
                    )
                con.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES('fts_tokenizer', ?)",
                    (tokenizer,),
                )
                con.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES('search_normalizer_version', ?)",
                    (SEARCH_NORMALIZER_VERSION,),
                )
            elif self._meta_value(con, "search_normalizer_version") is None:
                con.execute(
                    "INSERT INTO meta(key, value) VALUES('search_normalizer_version', ?)",
                    (SEARCH_NORMALIZER_VERSION,),
                )
            if self._meta_value(con, "text_extractor_version") != TEXT_EXTRACTOR_VERSION:
                self._requeue_text_documents(con)
            self._cancel_deleted_document_jobs(con)

    def _configure_journal_mode(self, con: sqlite3.Connection) -> None:
        if not self.journal_mode:
            return
        try:
            con.execute(f"PRAGMA journal_mode={self.journal_mode}")
        except sqlite3.OperationalError:
            if self.journal_mode == "DELETE":
                return
            try:
                con.execute("PRAGMA journal_mode=DELETE")
            except sqlite3.OperationalError:
                return

    def _fts_exists(self, con: sqlite3.Connection) -> bool:
        row = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
        ).fetchone()
        return row is not None

    def _search_normalizer_outdated(self, con: sqlite3.Connection) -> bool:
        return self._meta_value(con, "search_normalizer_version") != SEARCH_NORMALIZER_VERSION

    def _meta_value(self, con: sqlite3.Connection, key: str) -> str | None:
        row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def _rebuild_fts(self, con: sqlite3.Connection) -> None:
        con.execute("DELETE FROM chunks_fts")
        rows = list(
            con.execute(
                """
                SELECT c.id, c.document_id, c.page, c.text, d.rel_path
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE d.deleted = 0
                """
            )
        )
        for row in rows:
            indexed_text = search_index_text(row["text"])
            if not indexed_text:
                continue
            con.execute(
                """
                INSERT INTO chunks_fts(rowid, text, chunk_id, document_id, rel_path, page)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    indexed_text,
                    row["id"],
                    row["document_id"],
                    row["rel_path"],
                    row["page"],
                ),
            )
        con.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('search_normalizer_version', ?)",
            (SEARCH_NORMALIZER_VERSION,),
        )

    def _requeue_text_documents(self, con: sqlite3.Connection) -> None:
        ts = now_iso()
        rows = list(
            con.execute(
                """
                SELECT id FROM documents
                WHERE deleted = 0 AND ext IN ('.txt', '.md', '.doc', '.docx')
                """
            )
        )
        for row in rows:
            existing = con.execute(
                """
                SELECT id FROM jobs
                WHERE document_id = ? AND type = 'index' AND status IN ('queued', 'processing')
                """,
                (row["id"],),
            ).fetchone()
            if existing is None:
                con.execute(
                    """
                    INSERT INTO jobs(document_id, type, status, priority, message, created_at, updated_at)
                    VALUES(?, 'index', 'queued', 80, 'Queued for text decoder update', ?, ?)
                    """,
                    (row["id"], ts, ts),
                )
            con.execute(
                """
                UPDATE documents
                SET status = 'queued', error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (ts, row["id"]),
            )
        con.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('text_extractor_version', ?)",
            (TEXT_EXTRACTOR_VERSION,),
        )

    def _ensure_document_columns(self, con: sqlite3.Connection) -> None:
        existing = {
            row["name"]
            for row in con.execute("PRAGMA table_info(documents)").fetchall()
        }
        additions = {
            "publication_status": "TEXT NOT NULL DEFAULT 'pending'",
            "publication_info": "TEXT",
            "citation": "TEXT",
            "publication_checked_at": "TEXT",
        }
        for name, definition in additions.items():
            if name not in existing:
                con.execute(f"ALTER TABLE documents ADD COLUMN {name} {definition}")

    def fts_tokenizer(self) -> str:
        with self.connect() as con:
            row = con.execute("SELECT value FROM meta WHERE key='fts_tokenizer'").fetchone()
            return row["value"] if row else "unicode61"

    def record_event(
        self, event_type: str, message: str, document_id: str | None = None, path: str | None = None
    ) -> None:
        with self._lock, self.connect() as con:
            con.execute(
                """
                INSERT INTO events(type, document_id, path, message, created_at)
                VALUES(?, ?, ?, ?, ?)
                """,
                (event_type, document_id, path, message, now_iso()),
            )

    def upsert_document(self, doc: dict[str, Any]) -> bool:
        ts = now_iso()
        with self._lock, self.connect() as con:
            existing = con.execute(
                "SELECT size, mtime, deleted FROM documents WHERE id = ?", (doc["id"],)
            ).fetchone()
            changed = (
                existing is None
                or int(existing["size"]) != int(doc["size"])
                or float(existing["mtime"]) != float(doc["mtime"])
                or int(existing["deleted"]) == 1
            )
            con.execute(
                """
                INSERT INTO documents(
                    id, path, rel_path, title, ext, size, mtime, sha256, status,
                    created_at, updated_at, deleted
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(id) DO UPDATE SET
                    path=excluded.path,
                    rel_path=excluded.rel_path,
                    title=excluded.title,
                    ext=excluded.ext,
                    size=excluded.size,
                    mtime=excluded.mtime,
                    sha256=excluded.sha256,
                    status=CASE
                        WHEN documents.size != excluded.size
                          OR documents.mtime != excluded.mtime
                          OR documents.deleted = 1
                        THEN 'queued'
                        ELSE documents.status
                    END,
                    error=CASE
                        WHEN documents.size != excluded.size
                          OR documents.mtime != excluded.mtime
                          OR documents.deleted = 1
                        THEN NULL
                        ELSE documents.error
                    END,
                    publication_status=CASE
                        WHEN documents.size != excluded.size
                          OR documents.mtime != excluded.mtime
                          OR documents.deleted = 1
                        THEN 'pending'
                        ELSE documents.publication_status
                    END,
                    publication_info=CASE
                        WHEN documents.size != excluded.size
                          OR documents.mtime != excluded.mtime
                          OR documents.deleted = 1
                        THEN NULL
                        ELSE documents.publication_info
                    END,
                    citation=CASE
                        WHEN documents.size != excluded.size
                          OR documents.mtime != excluded.mtime
                          OR documents.deleted = 1
                        THEN NULL
                        ELSE documents.citation
                    END,
                    publication_checked_at=CASE
                        WHEN documents.size != excluded.size
                          OR documents.mtime != excluded.mtime
                          OR documents.deleted = 1
                        THEN NULL
                        ELSE documents.publication_checked_at
                    END,
                    updated_at=excluded.updated_at,
                    deleted=0
                """,
                (
                    doc["id"],
                    doc["path"],
                    doc["rel_path"],
                    doc["title"],
                    doc["ext"],
                    doc["size"],
                    doc["mtime"],
                    doc.get("sha256"),
                    "queued" if changed else doc.get("status", "ready"),
                    ts,
                    ts,
                ),
            )
            return changed

    def upsert_skipped_document(self, doc: dict[str, Any], error: str) -> bool:
        ts = now_iso()
        with self._lock, self.connect() as con:
            existing = con.execute(
                "SELECT size, mtime, deleted, status, error FROM documents WHERE id = ?",
                (doc["id"],),
            ).fetchone()
            changed = (
                existing is None
                or int(existing["size"]) != int(doc["size"])
                or float(existing["mtime"]) != float(doc["mtime"])
                or int(existing["deleted"]) == 1
                or existing["status"] != "error"
                or existing["error"] != error
            )
            con.execute(
                """
                INSERT INTO documents(
                    id, path, rel_path, title, ext, size, mtime, sha256, status,
                    error, created_at, updated_at, deleted
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'error', ?, ?, ?, 0)
                ON CONFLICT(id) DO UPDATE SET
                    path=excluded.path,
                    rel_path=excluded.rel_path,
                    title=excluded.title,
                    ext=excluded.ext,
                    size=excluded.size,
                    mtime=excluded.mtime,
                    sha256=excluded.sha256,
                    status='error',
                    error=excluded.error,
                    updated_at=excluded.updated_at,
                    deleted=0
                """,
                (
                    doc["id"],
                    doc["path"],
                    doc["rel_path"],
                    doc["title"],
                    doc["ext"],
                    doc["size"],
                    doc["mtime"],
                    doc.get("sha256"),
                    error,
                    ts,
                    ts,
                ),
            )
            if changed:
                self._delete_chunks(con, doc["id"])
            return changed

    def get_document(self, document_id: str) -> sqlite3.Row | None:
        with self.connect() as con:
            return con.execute(
                "SELECT * FROM documents WHERE id = ? AND deleted = 0", (document_id,)
            ).fetchone()

    def update_document_file_state(self, document_id: str, *, size: int, mtime: float) -> None:
        with self._lock, self.connect() as con:
            con.execute(
                """
                UPDATE documents
                SET size = ?, mtime = ?, updated_at = ?
                WHERE id = ?
                """,
                (size, mtime, now_iso(), document_id),
            )

    def list_documents(self, limit: int = 200) -> list[sqlite3.Row]:
        with self.connect() as con:
            return list(
                con.execute(
                    """
                    SELECT * FROM documents
                    WHERE deleted = 0
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            )

    def mark_missing_deleted(self, seen_ids: set[str]) -> int:
        with self._lock, self.connect() as con:
            current = list(
                con.execute("SELECT id, rel_path FROM documents WHERE deleted = 0")
            )
            deleted = 0
            for row in current:
                if row["id"] in seen_ids:
                    continue
                self._delete_chunks(con, row["id"])
                con.execute(
                    """
                    UPDATE documents
                    SET deleted = 1, status = 'deleted', updated_at = ?
                    WHERE id = ?
                    """,
                    (now_iso(), row["id"]),
                )
                con.execute(
                    """
                    INSERT INTO events(type, document_id, path, message, created_at)
                    VALUES('delete', ?, ?, ?, ?)
                    """,
                    (row["id"], row["rel_path"], f"Deleted: {row['rel_path']}", now_iso()),
                )
                self._cancel_document_jobs(con, row["id"], now_iso())
                deleted += 1
            return deleted

    def enqueue_job(self, document_id: str, job_type: str = "index", priority: int = 100) -> None:
        ts = now_iso()
        with self._lock, self.connect() as con:
            existing = con.execute(
                """
                SELECT id FROM jobs
                WHERE document_id = ? AND type = ? AND status IN ('queued', 'processing')
                """,
                (document_id, job_type),
            ).fetchone()
            if existing:
                return
            con.execute(
                """
                INSERT INTO jobs(document_id, type, status, priority, message, created_at, updated_at)
                VALUES(?, ?, 'queued', ?, 'Queued', ?, ?)
                """,
                (document_id, job_type, priority, ts, ts),
            )
            con.execute(
                "UPDATE documents SET status = 'queued', updated_at = ? WHERE id = ?",
                (ts, document_id),
            )

    def requeue_unsuccessful_document(self, document_id: str) -> bool:
        ts = now_iso()
        with self._lock, self.connect() as con:
            row = con.execute(
                """
                SELECT id, status FROM documents
                WHERE id = ? AND deleted = 0 AND status IN ('error', 'empty')
                """,
                (document_id,),
            ).fetchone()
            if row is None:
                return False
            existing = con.execute(
                """
                SELECT id FROM jobs
                WHERE document_id = ? AND type = 'index' AND status IN ('queued', 'processing')
                """,
                (document_id,),
            ).fetchone()
            if existing is not None:
                return False
            con.execute(
                """
                INSERT INTO jobs(document_id, type, status, priority, message, created_at, updated_at)
                VALUES(?, 'index', 'queued', 90, 'Retry failed document', ?, ?)
                """,
                (document_id, ts, ts),
            )
            con.execute(
                """
                UPDATE documents
                SET status = 'queued', error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (ts, document_id),
            )
            return True

    def claim_next_job(self) -> sqlite3.Row | None:
        ts = now_iso()
        with self._lock, self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            job = con.execute(
                """
                SELECT jobs.* FROM jobs
                JOIN documents ON documents.id = jobs.document_id
                WHERE jobs.status = 'queued' AND documents.deleted = 0
                ORDER BY jobs.priority ASC, jobs.created_at ASC
                LIMIT 1
                """
            ).fetchone()
            if job is None:
                return None
            con.execute(
                """
                UPDATE jobs
                SET status='processing', progress=0.01, message='Processing',
                    started_at=?, updated_at=?
                WHERE id=?
                """,
                (ts, ts, job["id"]),
            )
            con.execute(
                "UPDATE documents SET status='processing', updated_at=? WHERE id=?",
                (ts, job["document_id"]),
            )
            return con.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone()

    def requeue_interrupted_jobs(self) -> int:
        ts = now_iso()
        with self._lock, self.connect() as con:
            self._cancel_deleted_document_jobs(con, ts)
            rows = list(
                con.execute(
                    """
                    SELECT jobs.id, jobs.document_id
                    FROM jobs
                    JOIN documents ON documents.id = jobs.document_id
                    WHERE jobs.status = 'processing' AND documents.deleted = 0
                    """
                )
            )
            for row in rows:
                con.execute(
                    """
                    UPDATE jobs
                    SET status='queued', progress=0, message='Requeued after backend restart',
                        updated_at=?, started_at=NULL
                    WHERE id=?
                    """,
                    (ts, row["id"]),
                )
                con.execute(
                    "UPDATE documents SET status='queued', updated_at=? WHERE id=?",
                    (ts, row["document_id"]),
            )
            return len(rows)

    def _cancel_deleted_document_jobs(
        self, con: sqlite3.Connection, ts: str | None = None
    ) -> int:
        ts = ts or now_iso()
        cur = con.execute(
            """
            UPDATE jobs
            SET status='cancelled', progress=1, message='Cancelled because document was deleted',
                error=NULL, updated_at=?, finished_at=COALESCE(finished_at, ?)
            WHERE status IN ('queued', 'processing')
              AND document_id IN (
                  SELECT id FROM documents WHERE deleted = 1
              )
            """,
            (ts, ts),
        )
        return int(cur.rowcount or 0)

    def _cancel_document_jobs(
        self, con: sqlite3.Connection, document_id: str, ts: str | None = None
    ) -> int:
        ts = ts or now_iso()
        cur = con.execute(
            """
            UPDATE jobs
            SET status='cancelled', progress=1, message='Cancelled because document was deleted',
                error=NULL, updated_at=?, finished_at=COALESCE(finished_at, ?)
            WHERE document_id = ? AND status IN ('queued', 'processing')
            """,
            (ts, ts, document_id),
        )
        return int(cur.rowcount or 0)

    def update_job(
        self,
        job_id: int,
        status: str | None = None,
        progress: float | None = None,
        message: str | None = None,
        error: str | None = None,
    ) -> None:
        fields: list[str] = ["updated_at = ?"]
        values: list[Any] = [now_iso()]
        if status is not None:
            fields.append("status = ?")
            values.append(status)
            if status in {"done", "failed"}:
                fields.append("finished_at = ?")
                values.append(now_iso())
        if progress is not None:
            fields.append("progress = ?")
            values.append(progress)
        if message is not None:
            fields.append("message = ?")
            values.append(message)
        if error is not None:
            fields.append("error = ?")
            values.append(error)
        values.append(job_id)
        with self._lock, self.connect() as con:
            con.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?", values)

    def replace_chunks(
        self,
        document_id: str,
        chunks: Iterable[dict[str, Any]],
        *,
        status: str,
        searchable_pdf: str | None,
        page_count: int,
        text_chars: int,
        has_text_layer: bool,
        error: str | None = None,
    ) -> None:
        ts = now_iso()
        with self._lock, self.connect() as con:
            self._delete_chunks(con, document_id)
            doc = con.execute(
                "SELECT rel_path FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
            rel_path = doc["rel_path"] if doc else ""
            for chunk in chunks:
                text = normalize_text(chunk["text"])
                if not text:
                    continue
                indexed_text = search_index_text(text)
                cur = con.execute(
                    """
                    INSERT INTO chunks(document_id, page, ordinal, line, text, bbox, source)
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document_id,
                        chunk.get("page"),
                        int(chunk.get("ordinal") or 0),
                        chunk.get("line"),
                        text,
                        json.dumps(chunk.get("bbox"), ensure_ascii=False)
                        if chunk.get("bbox") is not None
                        else None,
                        chunk.get("source") or "text",
                    ),
                )
                chunk_id = cur.lastrowid
                con.execute(
                    """
                    INSERT INTO chunks_fts(rowid, text, chunk_id, document_id, rel_path, page)
                    VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (chunk_id, indexed_text, chunk_id, document_id, rel_path, chunk.get("page")),
                )
            con.execute(
                """
                UPDATE documents
                SET status=?, searchable_pdf=?, page_count=?, text_chars=?,
                    has_text_layer=?, error=?, indexed_at=?, updated_at=?
                WHERE id=?
                """,
                (
                    status,
                    searchable_pdf,
                    page_count,
                    text_chars,
                    1 if has_text_layer else 0,
                    error,
                    ts,
                    ts,
                    document_id,
                ),
            )

    def record_ocr_usage(
        self,
        usage_date: str,
        pages: int,
        day_start_utc: str,
        day_end_utc: str,
    ) -> None:
        page_count = max(0, int(pages or 0))
        if page_count <= 0:
            return

        ts = now_iso()
        with self._lock, self.connect() as con:
            existing = con.execute(
                "SELECT pages FROM ocr_usage_daily WHERE usage_date = ?",
                (usage_date,),
            ).fetchone()
            if existing is None:
                baseline = self._daily_indexed_pdf_pages(con, day_start_utc, day_end_utc)
                con.execute(
                    """
                    INSERT INTO ocr_usage_daily(usage_date, pages, updated_at)
                    VALUES(?, ?, ?)
                    ON CONFLICT(usage_date) DO UPDATE SET
                        pages = pages + ?,
                        updated_at = excluded.updated_at
                    """,
                    (usage_date, baseline + page_count, ts, page_count),
                )
                return

            con.execute(
                """
                UPDATE ocr_usage_daily
                SET pages = pages + ?, updated_at = ?
                WHERE usage_date = ?
                """,
                (page_count, ts, usage_date),
            )

    def ocr_usage_for_day(
        self,
        usage_date: str,
        day_start_utc: str,
        day_end_utc: str,
    ) -> dict[str, Any]:
        with self.connect() as con:
            row = con.execute(
                "SELECT pages, updated_at FROM ocr_usage_daily WHERE usage_date = ?",
                (usage_date,),
            ).fetchone()
            logged_pages = int(row["pages"] or 0) if row else 0
            indexed_pages = self._daily_indexed_pdf_pages(con, day_start_utc, day_end_utc)
            used_pages = max(logged_pages, indexed_pages)
            source = "usage_log" if row and logged_pages >= indexed_pages else "documents"
            return {
                "used_pages": used_pages,
                "logged_pages": logged_pages,
                "indexed_pages": indexed_pages,
                "source": source,
                "updated_at": row["updated_at"] if row else None,
            }

    def _daily_indexed_pdf_pages(
        self,
        con: sqlite3.Connection,
        day_start_utc: str,
        day_end_utc: str,
    ) -> int:
        row = con.execute(
            """
            SELECT COALESCE(SUM(page_count), 0) AS pages
            FROM documents
            WHERE deleted = 0
              AND ext = '.pdf'
              AND indexed_at IS NOT NULL
              AND indexed_at >= ?
              AND indexed_at < ?
            """,
            (day_start_utc, day_end_utc),
        ).fetchone()
        return int(row["pages"] or 0) if row else 0

    def _delete_chunks(self, con: sqlite3.Connection, document_id: str) -> None:
        rows = list(con.execute("SELECT id FROM chunks WHERE document_id = ?", (document_id,)))
        for row in rows:
            con.execute("DELETE FROM chunks_fts WHERE rowid = ?", (row["id"],))
        con.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))

    def fail_document(self, document_id: str, error: str) -> None:
        ts = now_iso()
        with self._lock, self.connect() as con:
            con.execute(
                """
                UPDATE documents
                SET status='error', error=?, updated_at=?
                WHERE id=?
                """,
                (error, ts, document_id),
            )

    def save_publication_info(
        self,
        document_id: str,
        *,
        status: str,
        info: dict[str, Any] | None = None,
        citation: str | None = None,
        error: str | None = None,
    ) -> None:
        payload = info or {}
        if error:
            payload = {**payload, "error": error}
        ts = now_iso()
        with self._lock, self.connect() as con:
            con.execute(
                """
                UPDATE documents
                SET publication_status=?, publication_info=?, citation=?,
                    publication_checked_at=?, updated_at=?
                WHERE id=?
                """,
                (
                    status,
                    json.dumps(payload, ensure_ascii=False) if payload else None,
                    citation,
                    ts,
                    ts,
                    document_id,
                ),
            )

    def search(
        self,
        query: str,
        limit: int = 50,
        offset: int = 0,
        scope: str | None = None,
        document_id: str | None = None,
    ) -> list[dict[str, Any]]:
        variants = search_variants(query)
        if not variants:
            return []
        tokenizer = self.fts_tokenizer()
        min_match_chars = 3 if tokenizer == "trigram" else 2
        use_like = len(variants[0]) < min_match_chars
        filter_sql, filter_params = search_document_filters(scope, document_id)
        like_clause = " OR ".join("c.text LIKE ?" for _ in variants)
        like_params = [f"%{variant}%" for variant in variants]
        with self.connect() as con:
            if use_like:
                rows = list(
                    con.execute(
                        f"""
                        SELECT c.id AS match_id, c.document_id, c.page, c.ordinal, c.line,
                               c.text AS snippet, c.source,
                               d.rel_path, d.title, d.ext, d.status, d.searchable_pdf,
                               d.citation, d.publication_status, d.publication_info
                        FROM chunks c
                        JOIN documents d ON d.id = c.document_id
                        WHERE {filter_sql} AND ({like_clause})
                        ORDER BY d.updated_at DESC, c.page, c.ordinal
                        LIMIT ? OFFSET ?
                        """,
                        (*filter_params, *like_params, limit, offset),
                    )
                )
                return self._rows_with_search_snippets(rows, variants)
            expr = fts_query_expr(query)
            try:
                rows = list(
                    con.execute(
                        f"""
                        SELECT c.id AS match_id, c.document_id, c.page, c.ordinal, c.line,
                               c.text AS snippet, c.source,
                               d.rel_path, d.title, d.ext, d.status, d.searchable_pdf,
                               d.citation, d.publication_status, d.publication_info
                        FROM chunks_fts
                        JOIN chunks c ON c.id = chunks_fts.chunk_id
                        JOIN documents d ON d.id = c.document_id
                        WHERE chunks_fts MATCH ? AND {filter_sql}
                        ORDER BY bm25(chunks_fts), c.page, c.ordinal
                        LIMIT ? OFFSET ?
                        """,
                        (expr, *filter_params, limit, offset),
                    )
                )
                return self._rows_with_search_snippets(rows, variants)
            except sqlite3.OperationalError:
                rows = list(
                    con.execute(
                        f"""
                        SELECT c.id AS match_id, c.document_id, c.page, c.ordinal, c.line,
                               c.text AS snippet, c.source,
                               d.rel_path, d.title, d.ext, d.status, d.searchable_pdf,
                               d.citation, d.publication_status, d.publication_info
                        FROM chunks c
                        JOIN documents d ON d.id = c.document_id
                        WHERE {filter_sql} AND ({like_clause})
                        ORDER BY d.updated_at DESC, c.page, c.ordinal
                        LIMIT ? OFFSET ?
                        """,
                        (*filter_params, *like_params, limit, offset),
                    )
                )
                return self._rows_with_search_snippets(rows, variants)

    def search_page(
        self,
        query: str,
        limit: int = 50,
        offset: int = 0,
        scope: str | None = None,
        mode: str | None = None,
    ) -> dict[str, Any]:
        page_size = max(1, int(limit))
        start = max(0, int(offset))
        search_mode = normalize_search_mode(mode)
        if search_mode == "basic":
            rows = self.search(query, limit=page_size + 1, offset=start, scope=scope)
        else:
            rows = self.search_advanced(
                query, search_mode, limit=page_size + 1, offset=start, scope=scope
            )
        has_more = len(rows) > page_size
        results = rows[:page_size]
        return {
            "results": results,
            "count": len(results),
            "returned_count": len(results),
            "offset": start,
            "limit": page_size,
            "has_more": has_more,
            "next_offset": start + len(results) if has_more else None,
        }

    def search_document_page(
        self,
        query: str,
        document_id: str,
        limit: int = 50,
        offset: int = 0,
        mode: str | None = None,
    ) -> dict[str, Any]:
        page_size = max(1, int(limit))
        start = max(0, int(offset))
        search_mode = normalize_search_mode(mode)
        if search_mode == "basic":
            rows = self.search(query, limit=page_size + 1, offset=start, document_id=document_id)
        else:
            rows = self.search_advanced(
                query,
                search_mode,
                limit=page_size + 1,
                offset=start,
                document_id=document_id,
            )
        has_more = len(rows) > page_size
        results = rows[:page_size]
        return {
            "results": results,
            "count": len(results),
            "returned_count": len(results),
            "offset": start,
            "limit": page_size,
            "has_more": has_more,
            "next_offset": start + len(results) if has_more else None,
        }

    def search_groups_page(
        self,
        query: str,
        limit: int = 50,
        offset: int = 0,
        scope: str | None = None,
        mode: str | None = None,
    ) -> dict[str, Any]:
        page_size = max(1, int(limit))
        start = max(0, int(offset))
        search_mode = normalize_search_mode(mode)
        if search_mode == "basic":
            groups = self.search_groups(query, limit=page_size + 1, offset=start, scope=scope)
        else:
            groups = self.search_advanced_groups(
                query, search_mode, limit=page_size + 1, offset=start, scope=scope
            )
        has_more = len(groups) > page_size
        results = groups[:page_size]
        return {
            "groups": results,
            "count": len(results),
            "returned_count": len(results),
            "offset": start,
            "limit": page_size,
            "has_more": has_more,
            "next_offset": start + len(results) if has_more else None,
        }

    def search_advanced(
        self,
        query: str,
        mode: str,
        limit: int = 50,
        offset: int = 0,
        scope: str | None = None,
        document_id: str | None = None,
    ) -> list[dict[str, Any]]:
        search_mode = normalize_search_mode(mode)
        variants_by_term = search_term_variants(query)
        if not variants_by_term:
            return []
        filter_sql, filter_params = search_document_filters(scope, document_id)
        variants = flatten_search_variants(variants_by_term)
        if search_mode == "document":
            return self._search_document_terms(
                variants_by_term, variants, filter_sql, filter_params, limit, offset
            )
        return self._search_same_chunk_terms(
            variants_by_term, variants, filter_sql, filter_params, limit, offset
        )

    def search_advanced_groups(
        self, query: str, mode: str, limit: int = 50, offset: int = 0, scope: str | None = None
    ) -> list[dict[str, Any]]:
        search_mode = normalize_search_mode(mode)
        variants_by_term = search_term_variants(query)
        if not variants_by_term:
            return []
        filter_sql, filter_params = search_document_filters(scope, None)
        variants = flatten_search_variants(variants_by_term)
        if search_mode == "document":
            return self._search_document_term_groups(
                variants_by_term, variants, filter_sql, filter_params, limit, offset
            )
        return self._search_same_chunk_term_groups(
            variants_by_term, variants, filter_sql, filter_params, limit, offset
        )

    def _search_same_chunk_terms(
        self,
        variants_by_term: list[list[str]],
        variants: list[str],
        filter_sql: str,
        filter_params: list[Any],
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        all_terms_clause, all_terms_params = all_terms_like_clause("c", variants_by_term)
        fts_expr = fts_prefilter_expr(variants_by_term, self.fts_tokenizer())
        if fts_expr:
            try:
                with self.connect() as con:
                    rows = list(
                        con.execute(
                            f"""
                            SELECT c.id AS match_id, c.document_id, c.page, c.ordinal, c.line,
                                   c.text AS snippet, c.source,
                                   d.rel_path, d.title, d.ext, d.status, d.searchable_pdf,
                                   d.citation, d.publication_status, d.publication_info
                            FROM chunks_fts
                            JOIN chunks c ON c.id = chunks_fts.chunk_id
                            JOIN documents d ON d.id = c.document_id
                            WHERE chunks_fts MATCH ? AND {filter_sql} AND {all_terms_clause}
                            ORDER BY bm25(chunks_fts), c.page, c.ordinal
                            LIMIT ? OFFSET ?
                            """,
                            (fts_expr, *filter_params, *all_terms_params, limit, offset),
                        )
                    )
                    return self._rows_with_search_snippets(rows, variants)
            except sqlite3.OperationalError:
                pass
        with self.connect() as con:
            rows = list(
                con.execute(
                    f"""
                    SELECT c.id AS match_id, c.document_id, c.page, c.ordinal, c.line,
                           c.text AS snippet, c.source,
                           d.rel_path, d.title, d.ext, d.status, d.searchable_pdf,
                           d.citation, d.publication_status, d.publication_info
                    FROM chunks c
                    JOIN documents d ON d.id = c.document_id
                    WHERE {filter_sql} AND {all_terms_clause}
                    ORDER BY d.updated_at DESC, c.page, c.ordinal
                    LIMIT ? OFFSET ?
                    """,
                    (*filter_params, *all_terms_params, limit, offset),
                )
            )
            return self._rows_with_search_snippets(rows, variants)

    def _search_same_chunk_term_groups(
        self,
        variants_by_term: list[list[str]],
        variants: list[str],
        filter_sql: str,
        filter_params: list[Any],
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        all_terms_clause, all_terms_params = all_terms_like_clause("c", variants_by_term)
        fts_expr = fts_prefilter_expr(variants_by_term, self.fts_tokenizer())
        if fts_expr:
            try:
                with self.connect() as con:
                    rows = list(
                        con.execute(
                            f"""
                            WITH matches AS (
                                SELECT c.id AS match_id, c.document_id, c.page, c.ordinal, c.line,
                                       c.text AS snippet, c.source, bm25(chunks_fts) AS rank,
                                       d.rel_path, d.title, d.ext, d.status, d.searchable_pdf,
                                       d.citation, d.publication_status, d.publication_info,
                                       d.updated_at
                                FROM chunks_fts
                                JOIN chunks c ON c.id = chunks_fts.chunk_id
                                JOIN documents d ON d.id = c.document_id
                                WHERE chunks_fts MATCH ? AND {filter_sql} AND {all_terms_clause}
                            ),
                            ranked AS (
                                SELECT *,
                                       COUNT(*) OVER (PARTITION BY document_id) AS match_count,
                                       ROW_NUMBER() OVER (
                                           PARTITION BY document_id
                                           ORDER BY rank, COALESCE(page, 0), ordinal, match_id
                                       ) AS group_row
                                FROM matches
                            )
                            SELECT match_id, document_id, page, ordinal, line, snippet, source,
                                   rel_path, title, ext, status, searchable_pdf, citation,
                                   publication_status, publication_info, match_count
                            FROM ranked
                            WHERE group_row = 1
                            ORDER BY match_count DESC, rank, updated_at DESC
                            LIMIT ? OFFSET ?
                            """,
                            (fts_expr, *filter_params, *all_terms_params, limit, offset),
                        )
                    )
                    return self._rows_with_search_snippets(rows, variants)
            except sqlite3.OperationalError:
                pass
        with self.connect() as con:
            rows = list(
                con.execute(
                    f"""
                    WITH matches AS (
                        SELECT c.id AS match_id, c.document_id, c.page, c.ordinal, c.line,
                               c.text AS snippet, c.source,
                               d.rel_path, d.title, d.ext, d.status, d.searchable_pdf,
                               d.citation, d.publication_status, d.publication_info,
                               d.updated_at
                        FROM chunks c
                        JOIN documents d ON d.id = c.document_id
                        WHERE {filter_sql} AND {all_terms_clause}
                    ),
                    ranked AS (
                        SELECT *,
                               COUNT(*) OVER (PARTITION BY document_id) AS match_count,
                               ROW_NUMBER() OVER (
                                   PARTITION BY document_id
                                   ORDER BY COALESCE(page, 0), ordinal, match_id
                               ) AS group_row
                        FROM matches
                    )
                    SELECT match_id, document_id, page, ordinal, line, snippet, source,
                           rel_path, title, ext, status, searchable_pdf, citation,
                           publication_status, publication_info, match_count
                    FROM ranked
                    WHERE group_row = 1
                    ORDER BY match_count DESC, updated_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (*filter_params, *all_terms_params, limit, offset),
                )
            )
            return self._rows_with_search_snippets(rows, variants)

    def _search_document_terms(
        self,
        variants_by_term: list[list[str]],
        variants: list[str],
        filter_sql: str,
        filter_params: list[Any],
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        cte_sql, cte_params = qualified_documents_cte(
            variants_by_term, filter_sql, filter_params, self.fts_tokenizer()
        )
        fts_any_expr = fts_any_terms_expr(variants_by_term, self.fts_tokenizer())
        if fts_any_expr:
            try:
                with self.connect() as con:
                    rows = list(
                        con.execute(
                            f"""
                            WITH {cte_sql}
                            SELECT c.id AS match_id, c.document_id, c.page, c.ordinal, c.line,
                                   c.text AS snippet, c.source,
                                   d.rel_path, d.title, d.ext, d.status, d.searchable_pdf,
                                   d.citation, d.publication_status, d.publication_info
                            FROM chunks_fts
                            JOIN chunks c ON c.id = chunks_fts.chunk_id
                            JOIN qualified_documents q ON q.document_id = c.document_id
                            JOIN documents d ON d.id = c.document_id
                            WHERE chunks_fts MATCH ?
                            ORDER BY bm25(chunks_fts), c.page, c.ordinal
                            LIMIT ? OFFSET ?
                            """,
                            (*cte_params, fts_any_expr, limit, offset),
                        )
                    )
                    return self._rows_with_search_snippets(rows, variants)
            except sqlite3.OperationalError:
                pass
        any_terms_clause, any_terms_params = any_term_like_clause("c", variants_by_term)
        with self.connect() as con:
            rows = list(
                con.execute(
                    f"""
                    WITH {cte_sql}
                    SELECT c.id AS match_id, c.document_id, c.page, c.ordinal, c.line,
                           c.text AS snippet, c.source,
                           d.rel_path, d.title, d.ext, d.status, d.searchable_pdf,
                           d.citation, d.publication_status, d.publication_info
                    FROM chunks c
                    JOIN qualified_documents q ON q.document_id = c.document_id
                    JOIN documents d ON d.id = c.document_id
                    WHERE {any_terms_clause}
                    ORDER BY d.updated_at DESC, c.page, c.ordinal
                    LIMIT ? OFFSET ?
                    """,
                    (*cte_params, *any_terms_params, limit, offset),
                )
            )
            return self._rows_with_search_snippets(rows, variants)

    def _search_document_term_groups(
        self,
        variants_by_term: list[list[str]],
        variants: list[str],
        filter_sql: str,
        filter_params: list[Any],
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        cte_sql, cte_params = qualified_documents_cte(
            variants_by_term, filter_sql, filter_params, self.fts_tokenizer()
        )
        fts_any_expr = fts_any_terms_expr(variants_by_term, self.fts_tokenizer())
        if fts_any_expr:
            try:
                with self.connect() as con:
                    rows = list(
                        con.execute(
                            f"""
                            WITH {cte_sql},
                            matches AS (
                                SELECT c.id AS match_id, c.document_id, c.page, c.ordinal, c.line,
                                       c.text AS snippet, c.source, bm25(chunks_fts) AS rank,
                                       d.rel_path, d.title, d.ext, d.status, d.searchable_pdf,
                                       d.citation, d.publication_status, d.publication_info,
                                       d.updated_at
                                FROM chunks_fts
                                JOIN chunks c ON c.id = chunks_fts.chunk_id
                                JOIN qualified_documents q ON q.document_id = c.document_id
                                JOIN documents d ON d.id = c.document_id
                                WHERE chunks_fts MATCH ?
                            ),
                            ranked AS (
                                SELECT *,
                                       COUNT(*) OVER (PARTITION BY document_id) AS match_count,
                                       ROW_NUMBER() OVER (
                                           PARTITION BY document_id
                                           ORDER BY rank, COALESCE(page, 0), ordinal, match_id
                                       ) AS group_row
                                FROM matches
                            )
                            SELECT match_id, document_id, page, ordinal, line, snippet, source,
                                   rel_path, title, ext, status, searchable_pdf, citation,
                                   publication_status, publication_info, match_count
                            FROM ranked
                            WHERE group_row = 1
                            ORDER BY match_count DESC, rank, updated_at DESC
                            LIMIT ? OFFSET ?
                            """,
                            (*cte_params, fts_any_expr, limit, offset),
                        )
                    )
                    return self._rows_with_search_snippets(rows, variants)
            except sqlite3.OperationalError:
                pass
        any_terms_clause, any_terms_params = any_term_like_clause("c", variants_by_term)
        with self.connect() as con:
            rows = list(
                con.execute(
                    f"""
                    WITH {cte_sql},
                    matches AS (
                        SELECT c.id AS match_id, c.document_id, c.page, c.ordinal, c.line,
                               c.text AS snippet, c.source,
                               d.rel_path, d.title, d.ext, d.status, d.searchable_pdf,
                               d.citation, d.publication_status, d.publication_info,
                               d.updated_at
                        FROM chunks c
                        JOIN qualified_documents q ON q.document_id = c.document_id
                        JOIN documents d ON d.id = c.document_id
                        WHERE {any_terms_clause}
                    ),
                    ranked AS (
                        SELECT *,
                               COUNT(*) OVER (PARTITION BY document_id) AS match_count,
                               ROW_NUMBER() OVER (
                                   PARTITION BY document_id
                                   ORDER BY COALESCE(page, 0), ordinal, match_id
                               ) AS group_row
                        FROM matches
                    )
                    SELECT match_id, document_id, page, ordinal, line, snippet, source,
                           rel_path, title, ext, status, searchable_pdf, citation,
                           publication_status, publication_info, match_count
                    FROM ranked
                    WHERE group_row = 1
                    ORDER BY match_count DESC, updated_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (*cte_params, *any_terms_params, limit, offset),
                )
            )
            return self._rows_with_search_snippets(rows, variants)

    def search_groups(
        self, query: str, limit: int = 50, offset: int = 0, scope: str | None = None
    ) -> list[dict[str, Any]]:
        variants = search_variants(query)
        if not variants:
            return []
        tokenizer = self.fts_tokenizer()
        min_match_chars = 3 if tokenizer == "trigram" else 2
        use_like = len(variants[0]) < min_match_chars
        filter_sql, filter_params = search_document_filters(scope, None)
        if use_like:
            return self._search_groups_like(variants, filter_sql, filter_params, limit, offset)
        expr = fts_query_expr(query)
        with self.connect() as con:
            try:
                rows = list(
                    con.execute(
                        f"""
                        WITH matches AS (
                            SELECT c.id AS match_id, c.document_id, c.page, c.ordinal, c.line,
                                   c.text AS snippet, c.source, bm25(chunks_fts) AS rank,
                                   d.rel_path, d.title, d.ext, d.status, d.searchable_pdf,
                                   d.citation, d.publication_status, d.publication_info,
                                   d.updated_at
                            FROM chunks_fts
                            JOIN chunks c ON c.id = chunks_fts.chunk_id
                            JOIN documents d ON d.id = c.document_id
                            WHERE chunks_fts MATCH ? AND {filter_sql}
                        ),
                        ranked AS (
                            SELECT *,
                                   COUNT(*) OVER (PARTITION BY document_id) AS match_count,
                                   ROW_NUMBER() OVER (
                                       PARTITION BY document_id
                                       ORDER BY rank, COALESCE(page, 0), ordinal, match_id
                                   ) AS group_row
                            FROM matches
                        )
                        SELECT match_id, document_id, page, ordinal, line, snippet, source,
                               rel_path, title, ext, status, searchable_pdf, citation,
                               publication_status, publication_info, match_count
                        FROM ranked
                        WHERE group_row = 1
                        ORDER BY match_count DESC, rank, updated_at DESC
                        LIMIT ? OFFSET ?
                        """,
                        (expr, *filter_params, limit, offset),
                    )
                )
                return self._rows_with_search_snippets(rows, variants)
            except sqlite3.OperationalError:
                return self._search_groups_like(variants, filter_sql, filter_params, limit, offset)

    def _search_groups_like(
        self,
        variants: list[str],
        filter_sql: str,
        filter_params: list[Any],
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        like_clause = " OR ".join("c.text LIKE ?" for _ in variants)
        like_params = [f"%{variant}%" for variant in variants]
        with self.connect() as con:
            rows = list(
                con.execute(
                    f"""
                    WITH matches AS (
                        SELECT c.id AS match_id, c.document_id, c.page, c.ordinal, c.line,
                               c.text AS snippet, c.source,
                               d.rel_path, d.title, d.ext, d.status, d.searchable_pdf,
                               d.citation, d.publication_status, d.publication_info,
                               d.updated_at
                        FROM chunks c
                        JOIN documents d ON d.id = c.document_id
                        WHERE {filter_sql} AND ({like_clause})
                    ),
                    ranked AS (
                        SELECT *,
                               COUNT(*) OVER (PARTITION BY document_id) AS match_count,
                               ROW_NUMBER() OVER (
                                   PARTITION BY document_id
                                   ORDER BY COALESCE(page, 0), ordinal, match_id
                               ) AS group_row
                        FROM matches
                    )
                    SELECT match_id, document_id, page, ordinal, line, snippet, source,
                           rel_path, title, ext, status, searchable_pdf, citation,
                           publication_status, publication_info, match_count
                    FROM ranked
                    WHERE group_row = 1
                    ORDER BY match_count DESC, updated_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (*filter_params, *like_params, limit, offset),
                )
            )
            return self._rows_with_search_snippets(rows, variants)

    def _rows_with_search_snippets(
        self, rows: Iterable[sqlite3.Row], variants: list[str]
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["snippet"] = search_result_snippet(str(item.get("snippet") or ""), variants)
            result.append(item)
        return result

    def text_context(
        self, document_id: str, match_id: int | None = None, radius: int = 8
    ) -> dict[str, Any]:
        with self.connect() as con:
            doc = con.execute(
                "SELECT * FROM documents WHERE id = ? AND deleted = 0", (document_id,)
            ).fetchone()
            if doc is None:
                raise KeyError(document_id)
            if match_id is None:
                rows = list(
                    con.execute(
                        """
                        SELECT * FROM chunks
                        WHERE document_id = ?
                        ORDER BY page, ordinal
                        LIMIT 300
                        """,
                        (document_id,),
                    )
                )
                return {"document": dict(doc), "chunks": [dict(row) for row in rows]}
            match = con.execute(
                "SELECT * FROM chunks WHERE id = ? AND document_id = ?",
                (match_id, document_id),
            ).fetchone()
            if match is None:
                rows = []
            else:
                rows = list(
                    con.execute(
                        """
                        SELECT * FROM chunks
                        WHERE document_id = ?
                          AND ordinal BETWEEN ? AND ?
                        ORDER BY page, ordinal
                        """,
                        (
                            document_id,
                            max(0, int(match["ordinal"]) - radius),
                            int(match["ordinal"]) + radius,
                        ),
                    )
                )
            return {
                "document": dict(doc),
                "match_id": match_id,
                "chunks": [dict(row) for row in rows],
            }

    def get_chunk(self, document_id: str, match_id: int) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute(
                """
                SELECT * FROM chunks
                WHERE id = ? AND document_id = ?
                """,
                (match_id, document_id),
            ).fetchone()
            return dict(row) if row else None

    def chunks_for_page(self, document_id: str, page: int) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = list(
                con.execute(
                    """
                    SELECT * FROM chunks
                    WHERE document_id = ? AND page = ?
                    ORDER BY ordinal
                    """,
                    (document_id, page),
                )
            )
            return [dict(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        with self.connect() as con:
            row = con.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE deleted = 0) AS total,
                    COUNT(*) FILTER (WHERE deleted = 0 AND status = 'ready') AS ready,
                    COUNT(*) FILTER (WHERE deleted = 0 AND status IN ('queued', 'processing')) AS active,
                    COUNT(*) FILTER (WHERE deleted = 0 AND status = 'error') AS errors,
                    COALESCE(SUM(text_chars) FILTER (WHERE deleted = 0), 0) AS text_chars
                FROM documents
                """
            ).fetchone()
            jobs = con.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE jobs.status = 'queued') AS queued,
                    COUNT(*) FILTER (WHERE jobs.status = 'processing') AS processing
                FROM jobs
                JOIN documents ON documents.id = jobs.document_id
                WHERE documents.deleted = 0
                """
            ).fetchone()
            latest = con.execute(
                """
                SELECT jobs.* FROM jobs
                JOIN documents ON documents.id = jobs.document_id
                WHERE jobs.status IN ('queued', 'processing')
                  AND documents.deleted = 0
                ORDER BY jobs.status = 'processing' DESC, jobs.updated_at DESC
                LIMIT 1
                """
            ).fetchone()
            return {
                "documents": dict(row),
                "jobs": dict(jobs),
                "latest_job": dict(latest) if latest else None,
                "fts_tokenizer": self.fts_tokenizer(),
            }

    def recent_jobs(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.connect() as con:
            return list(
                con.execute(
                    """
                    SELECT jobs.*, documents.rel_path, documents.title
                    FROM jobs
                    JOIN documents ON documents.id = jobs.document_id
                    WHERE documents.deleted = 0
                    ORDER BY jobs.updated_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            )

    def recent_events(self, limit: int = 30) -> list[sqlite3.Row]:
        with self.connect() as con:
            return list(
                con.execute(
                    "SELECT * FROM events ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            )


def normalize_scope(value: str | None) -> str:
    if not value:
        return ""
    normalized = str(value).replace("\\", "/").strip().strip("/")
    parts = [part for part in normalized.split("/") if part not in {"", ".", ".."}]
    return "/".join(parts)


def search_document_filters(
    scope: str | None = None, document_id: str | None = None
) -> tuple[str, list[Any]]:
    filters = ["d.deleted = 0"]
    params: list[Any] = []
    if document_id:
        filters.append("d.id = ?")
        params.append(document_id)
    normalized_scope = normalize_scope(scope)
    if normalized_scope:
        filters.append("d.rel_path LIKE ?")
        params.append(f"{normalized_scope}/%")
    return " AND ".join(filters), params


def normalize_search_mode(value: str | None) -> str:
    normalized = str(value or "basic").strip().lower()
    aliases = {
        "": "basic",
        "any": "basic",
        "normal": "basic",
        "basic": "basic",
        "line": "line",
        "chunk": "line",
        "same-line": "line",
        "same_chunk": "line",
        "document": "document",
        "file": "document",
        "same-file": "document",
        "same_document": "document",
    }
    return aliases.get(normalized, "basic")


def parse_search_terms(value: str | None) -> list[str]:
    normalized = normalize_text(value)
    if not normalized:
        return []

    terms: list[str] = []
    current: list[str] = []
    quote_end = ""
    quote_pairs = {'"': '"', "'": "'", "“": "”", "‘": "’"}
    separators = {",", "，", "、", ";", "；"}

    def flush() -> None:
        term = normalize_text("".join(current))
        current.clear()
        if term:
            terms.append(term)

    for char in normalized:
        if quote_end:
            if char == quote_end:
                quote_end = ""
            else:
                current.append(char)
            continue
        if char in quote_pairs:
            quote_end = quote_pairs[char]
            continue
        if char.isspace() or char in separators:
            flush()
            continue
        current.append(char)
    flush()
    return terms


def search_term_variants(query: str | None) -> list[list[str]]:
    result: list[list[str]] = []
    for term in parse_search_terms(query):
        variants = search_variants(term)
        if variants:
            result.append(variants)
    return result


def flatten_search_variants(variants_by_term: list[list[str]]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for variants in variants_by_term:
        for variant in variants:
            normalized = normalize_text(variant)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
    return result


def all_terms_like_clause(
    chunk_alias: str, variants_by_term: list[list[str]]
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for variants in variants_by_term:
        variant_clause, variant_params = variants_like_clause(chunk_alias, variants)
        clauses.append(f"({variant_clause})")
        params.extend(variant_params)
    return " AND ".join(clauses), params


def any_term_like_clause(
    chunk_alias: str, variants_by_term: list[list[str]]
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for variants in variants_by_term:
        variant_clause, variant_params = variants_like_clause(chunk_alias, variants)
        clauses.append(f"({variant_clause})")
        params.extend(variant_params)
    return " OR ".join(clauses), params


def variants_like_clause(chunk_alias: str, variants: list[str]) -> tuple[str, list[Any]]:
    clause = " OR ".join(f"{chunk_alias}.text LIKE ?" for _ in variants)
    return clause, [f"%{variant}%" for variant in variants]


def qualified_documents_cte(
    variants_by_term: list[list[str]],
    filter_sql: str,
    filter_params: list[Any],
    tokenizer: str,
) -> tuple[str, list[Any]]:
    term_selects: list[str] = []
    params: list[Any] = []
    for index, variants in enumerate(variants_by_term):
        fts_expr = fts_term_expr(variants, tokenizer)
        if fts_expr:
            term_selects.append(
                f"""
                SELECT DISTINCT c.document_id, {index} AS term_index
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.chunk_id
                JOIN documents d ON d.id = c.document_id
                WHERE chunks_fts MATCH ? AND {filter_sql}
                """
            )
            params.extend([fts_expr, *filter_params])
        else:
            variant_clause, variant_params = variants_like_clause("c", variants)
            term_selects.append(
                f"""
                SELECT DISTINCT c.document_id, {index} AS term_index
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE {filter_sql} AND ({variant_clause})
                """
            )
            params.extend([*filter_params, *variant_params])
    term_hits = "\nUNION ALL\n".join(term_selects)
    sql = f"""
    term_hits AS (
        {term_hits}
    ),
    qualified_documents AS (
        SELECT document_id
        FROM term_hits
        GROUP BY document_id
        HAVING COUNT(DISTINCT term_index) = {len(variants_by_term)}
    )
    """
    return sql, params


def fts_prefilter_expr(variants_by_term: list[list[str]], tokenizer: str) -> str:
    fts_terms = [
        variants for variants in variants_by_term if fts_term_expr(variants, tokenizer)
    ]
    if not fts_terms:
        return ""
    variants = max(fts_terms, key=lambda item: max(len(variant) for variant in item))
    return fts_query_expr_for_variants(variants)


def fts_any_terms_expr(variants_by_term: list[list[str]], tokenizer: str) -> str:
    if not variants_by_term:
        return ""
    for variants in variants_by_term:
        if not fts_term_expr(variants, tokenizer):
            return ""
    return fts_query_expr_for_variants(flatten_search_variants(variants_by_term))


def fts_term_expr(variants: list[str], tokenizer: str) -> str:
    min_match_chars = 3 if tokenizer == "trigram" else 2
    if not variants or min(len(variant) for variant in variants) < min_match_chars:
        return ""
    return fts_query_expr_for_variants(variants)


def fts_query_expr_for_variants(variants: list[str]) -> str:
    phrases = []
    for variant in variants:
        escaped = variant.replace('"', '""')
        phrases.append(f'"{escaped}"')
    return " OR ".join(phrases)


def normalize_journal_mode(value: str | None) -> str:
    normalized = str(value or "DELETE").strip().upper()
    allowed = {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}
    return normalized if normalized in allowed else "DELETE"


def search_result_snippet(
    text: str, variants: list[str], before_chars: int = 18, after_chars: int = 90
) -> str:
    if not text:
        return ""

    match = first_variant_match(text, variants)
    if match is None:
        return trim_snippet(text, before_chars + after_chars)

    start_index, end_index = match
    snippet_start = max(0, start_index - before_chars)
    snippet_end = min(len(text), end_index + after_chars)
    prefix = "…" if snippet_start > 0 else ""
    suffix = "…" if snippet_end < len(text) else ""
    body = text[snippet_start:snippet_end]

    return f"{prefix}{mark_variant_matches(body, variants)}{suffix}"


def first_variant_match(text: str, variants: list[str]) -> tuple[int, int] | None:
    lower_text = text.casefold()
    best: tuple[int, int] | None = None
    for variant in variants:
        needle = normalize_text(variant).casefold()
        if not needle:
            continue
        index = lower_text.find(needle)
        if index < 0:
            continue
        end = index + len(needle)
        if best is None or index < best[0] or (index == best[0] and end > best[1]):
            best = (index, end)
    return best


def variant_match_ranges(text: str, variants: list[str]) -> list[tuple[int, int]]:
    lower_text = text.casefold()
    ranges: list[tuple[int, int]] = []
    for variant in variants:
        needle = normalize_text(variant).casefold()
        if not needle:
            continue
        start = 0
        while True:
            index = lower_text.find(needle, start)
            if index < 0:
                break
            ranges.append((index, index + len(needle)))
            start = index + max(1, len(needle))

    selected: list[tuple[int, int]] = []
    for start, end in sorted(ranges, key=lambda item: (item[0], -(item[1] - item[0]))):
        if selected and start < selected[-1][1]:
            continue
        selected.append((start, end))
    return selected


def mark_variant_matches(text: str, variants: list[str]) -> str:
    ranges = variant_match_ranges(text, variants)
    if not ranges:
        return text
    parts: list[str] = []
    cursor = 0
    for start, end in ranges:
        parts.append(text[cursor:start])
        parts.append(f"<mark>{text[start:end]}</mark>")
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def trim_snippet(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}…"
