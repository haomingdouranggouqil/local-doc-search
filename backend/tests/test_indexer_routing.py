from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.indexer as indexer_module
from app.config import Settings
from app.database import Database
from app.indexer import DocumentIndexer
from app.resources import ResourcePolicy


class IndexerRoutingTests(unittest.TestCase):
    def test_pdf_under_text_only_path_uses_embedded_text_without_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path = root / "pdf" / "\u8bba\u6587" / "paper.pdf"
            pdf_path.parent.mkdir(parents=True)
            with fitz.open() as doc:
                page = doc.new_page(width=200, height=100)
                page.insert_text(fitz.Point(20, 50), "embedded paper text", fontsize=12)
                doc.save(pdf_path)

            indexer = make_indexer(root)

            extracted, searchable_pdf = indexer._extract_or_ocr(pdf_path, "doc1", None)

            self.assertEqual(pdf_path, searchable_pdf)
            self.assertTrue(extracted.has_text_layer)
            self.assertEqual("pdf-text", extracted.chunks[0]["source"])
            self.assertIn("embedded paper text", extracted.chunks[0]["text"])

    def test_caj_is_converted_to_searchable_pdf_and_indexed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            caj_path = root / "pdf" / "\u8bba\u6587" / "sample.caj"
            caj_path.parent.mkdir(parents=True)
            caj_path.write_bytes(b"fake caj payload")
            converter = root / "fake caj converter.py"
            converter.write_text(
                textwrap.dedent(
                    """
                    import sys
                    import fitz

                    output = sys.argv[2]
                    text = "CAJ searchable text " * 8
                    with fitz.open() as doc:
                        page = doc.new_page(width=300, height=120)
                        page.insert_text(fitz.Point(20, 60), text, fontsize=12)
                        doc.save(output)
                    """
                ),
                encoding="utf-8",
            )
            command = f'"{sys.executable}" "{converter}" {{input}} {{output}}'
            settings, db = make_settings_and_db(root, caj_converter_command=command)
            indexer = DocumentIndexer(settings, db, ResourcePolicy(settings))
            document_id = "cajdoc"
            db.upsert_document(
                {
                    "id": document_id,
                    "path": str(caj_path),
                    "rel_path": "pdf/\u8bba\u6587/sample.caj",
                    "title": "sample.caj",
                    "ext": ".caj",
                    "size": caj_path.stat().st_size,
                    "mtime": caj_path.stat().st_mtime,
                    "sha256": "hash",
                    "status": "queued",
                }
            )

            indexer.process(document_id)

            doc = db.get_document(document_id)
            self.assertIsNotNone(doc)
            self.assertTrue(str(doc["searchable_pdf"]).endswith(".pdf"))
            self.assertEqual("ready", doc["status"])
            self.assertEqual(1, len(db.search("CAJ searchable text")))

    def test_caj_conversion_without_text_falls_back_to_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            caj_path = root / "pdf" / "\u8bba\u6587" / "image-only.caj"
            caj_path.parent.mkdir(parents=True)
            caj_path.write_bytes(b"fake caj payload")
            converter = root / "fake blank converter.py"
            converter.write_text(
                textwrap.dedent(
                    """
                    import sys
                    import fitz

                    output = sys.argv[2]
                    with fitz.open() as doc:
                        doc.new_page(width=300, height=120)
                        doc.save(output)
                    """
                ),
                encoding="utf-8",
            )
            command = f'"{sys.executable}" "{converter}" {{input}} {{output}}'
            settings, db = make_settings_and_db(root, caj_converter_command=command)
            indexer = DocumentIndexer(settings, db, ResourcePolicy(settings))
            document_id = "imageonlycaj"
            db.upsert_document(
                {
                    "id": document_id,
                    "path": str(caj_path),
                    "rel_path": "pdf/\u8bba\u6587/image-only.caj",
                    "title": "image-only.caj",
                    "ext": ".caj",
                    "size": caj_path.stat().st_size,
                    "mtime": caj_path.stat().st_mtime,
                    "sha256": "hash",
                    "status": "queued",
                }
            )
            original_make_searchable_pdf = indexer_module.make_searchable_pdf
            calls: list[Path] = []

            def fake_make_searchable_pdf(
                source_pdf,
                output_pdf,
                engine,
                settings,
                progress_callback,
                max_pages,
                resources,
            ):
                calls.append(Path(source_pdf))
                text = "fallback ocr text"
                progress_callback(1, 1, "Fake OCR")
                with fitz.open() as doc:
                    page = doc.new_page(width=300, height=120)
                    page.insert_text(fitz.Point(20, 60), text, fontsize=12)
                    doc.save(output_pdf)
                return SimpleNamespace(
                    chunks=[{"page": 1, "ordinal": 0, "line": 1, "text": text, "source": "ocr"}],
                    page_count=1,
                    text_chars=len(text),
                )

            indexer_module.make_searchable_pdf = fake_make_searchable_pdf
            try:
                indexer.process(document_id)
            finally:
                indexer_module.make_searchable_pdf = original_make_searchable_pdf

            doc = db.get_document(document_id)
            self.assertIsNotNone(doc)
            self.assertEqual(1, len(calls))
            self.assertEqual("ready", doc["status"])
            self.assertEqual(1, len(db.search("fallback ocr text")))
            with fitz.open(doc["searchable_pdf"]) as converted:
                self.assertIn("fallback ocr text", converted[0].get_text("text"))


def make_indexer(root: Path) -> DocumentIndexer:
    settings, db = make_settings_and_db(root)
    return DocumentIndexer(settings, db, ResourcePolicy(settings))


def make_settings_and_db(root: Path, **overrides) -> tuple[Settings, Database]:
    settings = Settings(
        document_root=root,
        state_dir=root / ".state",
        pdf_text_only_paths="pdf/\u8bba\u6587",
        ocr_min_text_chars=1,
        resource_auto_tune=False,
        max_output_pdf_mb=0,
        **overrides,
    )
    settings.ensure_dirs()
    return settings, Database(settings.db_path, settings.sqlite_journal_mode)


if __name__ == "__main__":
    unittest.main()
