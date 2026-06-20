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
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.init()

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=30000")
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
            if changed:
                self._delete_chunks(con, doc["id"])
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
        self, query: str, limit: int = 50, offset: int = 0, scope: str | None = None
    ) -> list[dict[str, Any]]:
        variants = search_variants(query)
        if not variants:
            return []
        tokenizer = self.fts_tokenizer()
        min_match_chars = 3 if tokenizer == "trigram" else 2
        use_like = len(variants[0]) < min_match_chars
        scope = normalize_scope(scope)
        scope_clause = ""
        scope_params: list[Any] = []
        if scope:
            scope_clause = " AND d.rel_path LIKE ?"
            scope_params.append(f"{scope}/%")
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
                        WHERE d.deleted = 0 AND ({like_clause}){scope_clause}
                        ORDER BY d.updated_at DESC, c.page, c.ordinal
                        LIMIT ? OFFSET ?
                        """,
                        (*like_params, *scope_params, limit, offset),
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
                        WHERE chunks_fts MATCH ? AND d.deleted = 0{scope_clause}
                        ORDER BY bm25(chunks_fts), c.page, c.ordinal
                        LIMIT ? OFFSET ?
                        """,
                        (expr, *scope_params, limit, offset),
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
                        WHERE d.deleted = 0 AND ({like_clause}){scope_clause}
                        ORDER BY d.updated_at DESC, c.page, c.ordinal
                        LIMIT ? OFFSET ?
                        """,
                        (*like_params, *scope_params, limit, offset),
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
                    COUNT(*) FILTER (WHERE status = 'queued') AS queued,
                    COUNT(*) FILTER (WHERE status = 'processing') AS processing
                FROM jobs
                """
            ).fetchone()
            latest = con.execute(
                """
                SELECT * FROM jobs
                WHERE status IN ('queued', 'processing')
                ORDER BY status = 'processing' DESC, updated_at DESC
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

    return (
        f"{prefix}"
        f"{text[snippet_start:start_index]}"
        f"<mark>{text[start_index:end_index]}</mark>"
        f"{text[end_index:snippet_end]}"
        f"{suffix}"
    )


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


def trim_snippet(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}…"
