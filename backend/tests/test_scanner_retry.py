from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.database import Database
from app.resources import ResourcePolicy
from app.scanner import DocumentScanner


class ScannerRetryTests(unittest.TestCase):
    def test_failed_documents_can_be_listed_and_requeued_individually(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = Database(root / "index.sqlite")
            db.upsert_document(
                {
                    "id": "doc1",
                    "path": str(root / "failed.doc"),
                    "rel_path": "doc/failed.doc",
                    "title": "failed.doc",
                    "ext": ".doc",
                    "size": 1,
                    "mtime": 1.0,
                    "sha256": "hash",
                    "status": "queued",
                }
            )
            db.fail_document("doc1", "temporary failure")

            failed = db.list_unsuccessful_documents()

            self.assertEqual(1, db.unsuccessful_document_count())
            self.assertEqual(1, len(failed))
            self.assertEqual("doc1", failed[0]["id"])
            self.assertEqual("temporary failure", failed[0]["error"])

            self.assertTrue(db.requeue_unsuccessful_document("doc1"))

            self.assertEqual(0, db.unsuccessful_document_count())
            stats = db.stats()
            self.assertEqual(1, stats["jobs"]["queued"])
            doc = db.get_document("doc1")
            self.assertEqual("queued", doc["status"])
            self.assertIsNone(doc["error"])

    def test_failed_documents_can_be_requeued_in_bulk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = Database(root / "index.sqlite")
            for index in range(2):
                document_id = f"doc{index}"
                db.upsert_document(
                    {
                        "id": document_id,
                        "path": str(root / f"failed-{index}.doc"),
                        "rel_path": f"doc/failed-{index}.doc",
                        "title": f"failed-{index}.doc",
                        "ext": ".doc",
                        "size": 1,
                        "mtime": float(index + 1),
                        "sha256": "hash",
                        "status": "queued",
                    }
                )
                db.fail_document(document_id, "temporary failure")

            self.assertEqual(2, db.requeue_unsuccessful_documents())

            self.assertEqual(0, db.unsuccessful_document_count())
            self.assertEqual(2, db.stats()["jobs"]["queued"])

    def test_scan_can_requeue_failed_unchanged_documents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = root / "library"
            library.mkdir()
            source = library / "sample.txt"
            source.write_text("sample", encoding="utf-8")
            settings = Settings(
                document_root=library,
                state_dir=root / ".state",
                supported_extensions=".txt",
                resource_auto_tune=False,
            )
            settings.ensure_dirs()
            db = Database(settings.db_path, settings.sqlite_journal_mode)
            scanner = DocumentScanner(settings, db, ResourcePolicy(settings))

            first = scanner.scan_once()
            self.assertEqual(1, first["changed"])
            job = db.claim_next_job()
            self.assertIsNotNone(job)
            db.fail_document(job["document_id"], "temporary failure")
            db.update_job(job["id"], status="failed", progress=1, message="Failed", error="temporary failure")

            normal_rescan = scanner.scan_once()
            self.assertEqual(0, normal_rescan["requeued"])
            self.assertEqual(0, db.stats()["jobs"]["queued"])

            retry_rescan = scanner.scan_once(retry_failed=True)

            self.assertEqual(1, retry_rescan["requeued"])
            stats = db.stats()
            self.assertEqual(1, stats["jobs"]["queued"])
            doc = db.get_document(job["document_id"])
            self.assertEqual("queued", doc["status"])
            self.assertIsNone(doc["error"])


if __name__ == "__main__":
    unittest.main()
