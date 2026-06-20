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


if __name__ == "__main__":
    unittest.main()
