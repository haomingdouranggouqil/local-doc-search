from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import Database


class DatabaseSearchTests(unittest.TestCase):
    def test_two_character_query_searches_chunk_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = Database(root / "index.sqlite")
            document_id = "doc1"
            db.upsert_document(
                {
                    "id": document_id,
                    "path": str(root / "sample.txt"),
                    "rel_path": "txt/诗话/sample.txt",
                    "title": "sample.txt",
                    "ext": ".txt",
                    "size": 1,
                    "mtime": 1.0,
                    "sha256": "hash",
                    "status": "queued",
                }
            )
            db.replace_chunks(
                document_id,
                [{"ordinal": 0, "line": 1, "text": "郑海藏有诗", "source": "text"}],
                status="ready",
                searchable_pdf=None,
                page_count=0,
                text_chars=5,
                has_text_layer=True,
            )
            with db.connect() as con:
                con.execute("DELETE FROM chunks_fts")

            rows = db.search("海藏")

            self.assertEqual(1, len(rows))
            self.assertEqual("郑<mark>海藏</mark>有诗", rows[0]["snippet"])

    def test_search_snippet_is_centered_on_match_in_long_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = Database(root / "index.sqlite")
            document_id = "doc1"
            db.upsert_document(
                {
                    "id": document_id,
                    "path": str(root / "sample.txt"),
                    "rel_path": "txt/sample.txt",
                    "title": "sample.txt",
                    "ext": ".txt",
                    "size": 1,
                    "mtime": 1.0,
                    "sha256": "hash",
                    "status": "queued",
                }
            )
            long_line = ("前" * 140) + "海藏" + ("后" * 140)
            db.replace_chunks(
                document_id,
                [{"ordinal": 0, "line": 1, "text": long_line, "source": "text"}],
                status="ready",
                searchable_pdf=None,
                page_count=0,
                text_chars=len(long_line),
                has_text_layer=True,
            )

            rows = db.search("海藏")

            self.assertEqual(1, len(rows))
            self.assertIn("<mark>海藏</mark>", rows[0]["snippet"])
            self.assertLess(len(rows[0]["snippet"]), len(long_line))
            self.assertTrue(rows[0]["snippet"].startswith("…"))

    def test_search_page_reports_more_results_for_large_result_sets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = Database(root / "index.sqlite")
            term = "\u6d77\u85cf"
            for index in range(3):
                document_id = f"doc{index}"
                db.upsert_document(
                    {
                        "id": document_id,
                        "path": str(root / f"sample-{index}.txt"),
                        "rel_path": f"txt/sample-{index}.txt",
                        "title": f"sample-{index}.txt",
                        "ext": ".txt",
                        "size": 1,
                        "mtime": float(index + 1),
                        "sha256": f"hash-{index}",
                        "status": "queued",
                    }
                )
                db.replace_chunks(
                    document_id,
                    [
                        {
                            "ordinal": 0,
                            "line": 1,
                            "text": f"{term} result {index}",
                            "source": "text",
                        }
                    ],
                    status="ready",
                    searchable_pdf=None,
                    page_count=0,
                    text_chars=8,
                    has_text_layer=True,
                )

            first_page = db.search_page(term, limit=2, offset=0)
            second_page = db.search_page(term, limit=2, offset=2)

            self.assertEqual(2, len(first_page["results"]))
            self.assertTrue(first_page["has_more"])
            self.assertEqual(2, first_page["next_offset"])
            self.assertEqual(1, len(second_page["results"]))
            self.assertFalse(second_page["has_more"])
            self.assertIsNone(second_page["next_offset"])

    def test_search_groups_collapses_hits_by_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = Database(root / "index.sqlite")
            term = "\u90d1\u5b5d\u80e5"
            for document_id, title, chunks in (
                (
                    "diary",
                    "\u90d1\u5b5d\u80e5\u65e5\u8bb0.pdf",
                    [
                        {"ordinal": 0, "page": 1, "text": f"{term} first hit", "source": "ocr"},
                        {"ordinal": 1, "page": 2, "text": f"{term} second hit", "source": "ocr"},
                    ],
                ),
                (
                    "letters",
                    "\u90d1\u5b5d\u80e5\u4e66\u4fe1.txt",
                    [{"ordinal": 0, "line": 1, "text": f"{term} letter hit", "source": "text"}],
                ),
            ):
                db.upsert_document(
                    {
                        "id": document_id,
                        "path": str(root / title),
                        "rel_path": f"pdf/{title}" if title.endswith(".pdf") else f"txt/{title}",
                        "title": title,
                        "ext": ".pdf" if title.endswith(".pdf") else ".txt",
                        "size": 1,
                        "mtime": 1.0,
                        "sha256": f"hash-{document_id}",
                        "status": "queued",
                    }
                )
                db.replace_chunks(
                    document_id,
                    chunks,
                    status="ready",
                    searchable_pdf=str(root / title) if title.endswith(".pdf") else None,
                    page_count=2 if title.endswith(".pdf") else 0,
                    text_chars=100,
                    has_text_layer=True,
                )

            page = db.search_groups_page(term, limit=10, offset=0)

            self.assertEqual(2, len(page["groups"]))
            self.assertEqual("diary", page["groups"][0]["document_id"])
            self.assertEqual(2, page["groups"][0]["match_count"])
            self.assertIn("<mark>", page["groups"][0]["snippet"])

    def test_search_document_page_limits_hits_to_one_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = Database(root / "index.sqlite")
            term = "\u90d1\u5b5d\u80e5"
            for document_id in ("doc1", "doc2"):
                db.upsert_document(
                    {
                        "id": document_id,
                        "path": str(root / f"{document_id}.txt"),
                        "rel_path": f"txt/{document_id}.txt",
                        "title": f"{document_id}.txt",
                        "ext": ".txt",
                        "size": 1,
                        "mtime": 1.0,
                        "sha256": f"hash-{document_id}",
                        "status": "queued",
                    }
                )
                db.replace_chunks(
                    document_id,
                    [
                        {
                            "ordinal": 0,
                            "line": 1,
                            "text": f"{term} in {document_id}",
                            "source": "text",
                        }
                    ],
                    status="ready",
                    searchable_pdf=None,
                    page_count=0,
                    text_chars=20,
                    has_text_layer=True,
                )

            page = db.search_document_page(term, document_id="doc2", limit=10, offset=0)

            self.assertEqual(1, len(page["results"]))
            self.assertEqual("doc2", page["results"][0]["document_id"])

    def test_file_change_keeps_previous_chunks_until_reindex_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = Database(root / "index.sqlite")
            document_id = "doc1"
            doc = {
                "id": document_id,
                "path": str(root / "sample.pdf"),
                "rel_path": "pdf/sample.pdf",
                "title": "sample.pdf",
                "ext": ".pdf",
                "size": 1,
                "mtime": 1.0,
                "sha256": "hash1",
                "status": "queued",
            }
            db.upsert_document(doc)
            db.replace_chunks(
                document_id,
                [{"ordinal": 0, "line": 1, "page": 1, "text": "stable old text", "source": "text"}],
                status="ready",
                searchable_pdf=str(root / "sample.pdf"),
                page_count=1,
                text_chars=15,
                has_text_layer=True,
            )

            changed_doc = {**doc, "size": 2, "mtime": 2.0, "sha256": "hash2"}
            changed = db.upsert_document(changed_doc)

            self.assertTrue(changed)
            self.assertEqual(1, len(db.search("stable old text")))


class DatabaseOcrUsageTests(unittest.TestCase):
    def test_ocr_usage_falls_back_to_pdf_documents_indexed_in_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = Database(root / "index.sqlite")
            document_id = "pdf1"
            db.upsert_document(
                {
                    "id": document_id,
                    "path": str(root / "sample.pdf"),
                    "rel_path": "pdf/sample.pdf",
                    "title": "sample.pdf",
                    "ext": ".pdf",
                    "size": 1,
                    "mtime": 1.0,
                    "sha256": "hash",
                    "status": "queued",
                }
            )
            db.replace_chunks(
                document_id,
                [],
                status="ready",
                searchable_pdf=str(root / "sample.pdf"),
                page_count=12,
                text_chars=0,
                has_text_layer=True,
            )

            usage = db.ocr_usage_for_day(
                "2026-06-20",
                "1970-01-01T00:00:00+00:00",
                "2999-01-01T00:00:00+00:00",
            )

            self.assertEqual(12, usage["used_pages"])
            self.assertEqual("documents", usage["source"])

    def test_record_ocr_usage_bootstraps_today_baseline_then_accumulates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = Database(root / "index.sqlite")
            document_id = "pdf1"
            db.upsert_document(
                {
                    "id": document_id,
                    "path": str(root / "sample.pdf"),
                    "rel_path": "pdf/sample.pdf",
                    "title": "sample.pdf",
                    "ext": ".pdf",
                    "size": 1,
                    "mtime": 1.0,
                    "sha256": "hash",
                    "status": "queued",
                }
            )
            db.replace_chunks(
                document_id,
                [],
                status="ready",
                searchable_pdf=str(root / "sample.pdf"),
                page_count=12,
                text_chars=0,
                has_text_layer=True,
            )

            db.record_ocr_usage(
                "2026-06-20",
                3,
                "1970-01-01T00:00:00+00:00",
                "2999-01-01T00:00:00+00:00",
            )
            db.record_ocr_usage(
                "2026-06-20",
                2,
                "1970-01-01T00:00:00+00:00",
                "2999-01-01T00:00:00+00:00",
            )

            usage = db.ocr_usage_for_day(
                "2026-06-20",
                "1970-01-01T00:00:00+00:00",
                "2999-01-01T00:00:00+00:00",
            )

            self.assertEqual(17, usage["used_pages"])
            self.assertEqual("usage_log", usage["source"])


class DatabaseJobCleanupTests(unittest.TestCase):
    def test_deleted_documents_do_not_leave_active_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = Database(root / "index.sqlite")
            for document_id in ("processing-doc", "queued-doc"):
                db.upsert_document(
                    {
                        "id": document_id,
                        "path": str(root / f"{document_id}.pdf"),
                        "rel_path": f"pdf/{document_id}.pdf",
                        "title": f"{document_id}.pdf",
                        "ext": ".pdf",
                        "size": 1,
                        "mtime": 1.0,
                        "sha256": "hash",
                        "status": "queued",
                    }
                )
                db.enqueue_job(document_id)

            claimed = db.claim_next_job()
            self.assertIsNotNone(claimed)

            db.mark_missing_deleted(set())

            stats = db.stats()
            self.assertEqual(0, stats["jobs"]["queued"])
            self.assertEqual(0, stats["jobs"]["processing"])
            self.assertIsNone(stats["latest_job"])
            self.assertEqual([], db.recent_jobs())
            with db.connect() as con:
                statuses = [
                    row["status"]
                    for row in con.execute(
                        "SELECT status FROM jobs ORDER BY id"
                    ).fetchall()
                ]
            self.assertEqual(["cancelled", "cancelled"], statuses)

    def test_init_cleans_active_jobs_left_on_deleted_documents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "index.sqlite"
            db = Database(db_path)
            db.upsert_document(
                {
                    "id": "doc1",
                    "path": str(root / "sample.pdf"),
                    "rel_path": "pdf/sample.pdf",
                    "title": "sample.pdf",
                    "ext": ".pdf",
                    "size": 1,
                    "mtime": 1.0,
                    "sha256": "hash",
                    "status": "queued",
                }
            )
            db.enqueue_job("doc1")
            self.assertIsNotNone(db.claim_next_job())
            with db.connect() as con:
                con.execute(
                    "UPDATE documents SET deleted = 1, status = 'deleted' WHERE id = 'doc1'"
                )

            reopened = Database(db_path)

            stats = reopened.stats()
            self.assertEqual(0, stats["jobs"]["queued"])
            self.assertEqual(0, stats["jobs"]["processing"])
            with reopened.connect() as con:
                status = con.execute("SELECT status FROM jobs").fetchone()["status"]
            self.assertEqual("cancelled", status)


if __name__ == "__main__":
    unittest.main()
