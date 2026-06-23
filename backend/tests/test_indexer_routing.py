from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

import fitz
from docx import Document as DocxDocument

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.indexer as indexer_module
import app.text_extractors as text_extractors
from app.config import Settings
from app.database import Database
from app.indexer import DocumentIndexer
from app.resources import ResourcePolicy


class IndexerRoutingTests(unittest.TestCase):
    def test_docx_falls_back_to_office_text_when_strict_parser_rejects_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docx_path = root / "wps-compatible.docx"
            docx_path.write_bytes(b"\xd0\xcf\x11\xe0 fake legacy office payload")
            original_docx_document = text_extractors.DocxDocument
            original_converter = text_extractors._convert_office_document

            def reject_docx(path):
                raise ValueError("not a strict WordprocessingML package")

            def fake_converter(path, output_dir, *, convert_to, output_suffix, timeout):
                self.assertEqual("txt:Text", convert_to)
                output = output_dir / f"{path.stem}{output_suffix}"
                output.write_text("fallback office text 明诗话", encoding="utf-8-sig")
                return output

            text_extractors.DocxDocument = reject_docx
            text_extractors._convert_office_document = fake_converter
            try:
                extracted = text_extractors.extract_docx(docx_path)
            finally:
                text_extractors.DocxDocument = original_docx_document
                text_extractors._convert_office_document = original_converter

            self.assertTrue(extracted.has_text_layer)
            self.assertEqual("docx", extracted.chunks[0]["source"])
            self.assertIn("fallback office text", extracted.chunks[0]["text"])

    def test_epub_uses_text_extractor_without_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            epub_path = root / "book.epub"
            write_minimal_epub(epub_path)

            indexer = make_indexer(root)
            indexer._rebuild_searchable_pdf = self.fail

            extracted, searchable_pdf = indexer._extract_or_ocr(epub_path, "epubdoc", None)

            self.assertIsNone(searchable_pdf)
            self.assertTrue(extracted.has_text_layer)
            self.assertGreater(extracted.text_chars, 0)
            self.assertEqual("epub", extracted.chunks[0]["source"])
            self.assertIn(
                "\u6d77\u85cf\u697c\u8bd7",
                "\n".join(chunk["text"] for chunk in extracted.chunks),
            )

    def test_docx_uses_text_extractor_without_preview_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docx_path = root / "note.docx"
            document = DocxDocument()
            document.add_paragraph("DOCX searchable text")
            document.save(docx_path)

            indexer = make_indexer(root)
            indexer._rebuild_searchable_pdf = self.fail

            extracted, searchable_pdf = indexer._extract_or_ocr(docx_path, "docxdoc", None)

            self.assertIsNone(searchable_pdf)
            self.assertTrue(extracted.has_text_layer)
            self.assertEqual("docx", extracted.chunks[0]["source"])
            self.assertIn("DOCX searchable text", extracted.chunks[0]["text"])

    def test_doc_uses_text_extractor_without_preview_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc_path = root / "legacy.doc"
            doc_path.write_bytes(b"legacy office payload")
            original_extract_doc = indexer_module.extract_doc

            def fake_extract_doc(path):
                self.assertEqual(doc_path, path)
                return text_extractors.ExtractedText(
                    chunks=[
                        {
                            "page": 0,
                            "ordinal": 0,
                            "line": 1,
                            "text": "DOC searchable text",
                            "source": "doc",
                        }
                    ],
                    page_count=0,
                    text_chars=len("DOC searchable text"),
                    has_text_layer=True,
                )

            indexer_module.extract_doc = fake_extract_doc
            try:
                indexer = make_indexer(root)
                extracted, searchable_pdf = indexer._extract_or_ocr(doc_path, "docdoc", None)
            finally:
                indexer_module.extract_doc = original_extract_doc

            self.assertIsNone(searchable_pdf)
            self.assertTrue(extracted.has_text_layer)
            self.assertEqual("doc", extracted.chunks[0]["source"])
            self.assertIn("DOC searchable text", extracted.chunks[0]["text"])

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
                cancel_callback,
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


def write_minimal_epub(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr(
            "META-INF/container.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
              <rootfiles>
                <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
              </rootfiles>
            </container>
            """,
        )
        archive.writestr(
            "OEBPS/content.opf",
            """<?xml version="1.0" encoding="UTF-8"?>
            <package version="3.0" xmlns="http://www.idpf.org/2007/opf">
              <manifest>
                <item id="chapter-1" href="chapter-1.xhtml" media-type="application/xhtml+xml"/>
              </manifest>
              <spine>
                <itemref idref="chapter-1"/>
              </spine>
            </package>
            """,
        )
        archive.writestr(
            "OEBPS/chapter-1.xhtml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <html xmlns="http://www.w3.org/1999/xhtml">
              <body>
                <h1>\u6d77\u85cf\u697c\u8bd7</h1>
                <p>\u8fd9\u662f EPUB \u6587\u672c\uff0c\u5e94\u76f4\u63a5\u5efa\u7acb\u7d22\u5f15\u3002</p>
              </body>
            </html>
            """,
        )


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
