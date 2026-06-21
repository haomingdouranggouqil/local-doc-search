from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ocr import (
    OcrLine,
    OcrPageResult,
    PaddleOcrEngine,
    page_image_size,
    parse_api_jsonl,
    make_searchable_pdf,
    strip_invisible_text_content,
)


class OcrCoordinateTests(unittest.TestCase):
    def test_parse_api_jsonl_does_not_infer_page_size_from_text_extent(self) -> None:
        payload = {
            "result": {
                "ocrResults": [
                    {
                        "prunedResult": {
                            "rec_texts": ["正文"],
                            "rec_polys": [
                                [[100, 200], [500, 200], [500, 240], [100, 240]],
                            ],
                        }
                    }
                ]
            }
        }

        pages = parse_api_jsonl(json.dumps(payload, ensure_ascii=False))

        self.assertEqual(1, len(pages))
        self.assertIsNone(pages[0].image_width)
        self.assertIsNone(pages[0].image_height)

    def test_missing_page_size_is_inferred_from_page_aspect_ratio(self) -> None:
        page = OcrPageResult(
            page_number=1,
            image_width=None,
            image_height=None,
            lines=[
                OcrLine(
                    text="正文",
                    score=None,
                    box=[[100, 200], [1191, 200], [1191, 1156], [100, 1156]],
                )
            ],
        )
        page_rect = fitz.Rect(0, 0, 595.28, 852.06)

        width, height = page_image_size(page, page_rect)

        self.assertEqual(1191, width)
        self.assertAlmostEqual(1191 / (page_rect.width / page_rect.height), height)

    def test_strip_invisible_text_content_keeps_visible_content(self) -> None:
        with fitz.open() as doc:
            page = doc.new_page(width=200, height=100)
            page.draw_rect(fitz.Rect(10, 10, 80, 80), color=(0, 0, 0))
            page.insert_text(fitz.Point(20, 30), "visible", fontsize=12)
            page.insert_text(fitz.Point(20, 60), "hidden", fontsize=12, render_mode=3)

            strip_invisible_text_content(doc, page)
            payload = doc.tobytes(garbage=4, deflate=True)

        with fitz.open(stream=payload, filetype="pdf") as checked:
            page = checked[0]

            self.assertEqual(["visible"], [word[4] for word in page.get_text("words")])
            self.assertGreater(len(page.get_drawings()), 0)

    def test_make_searchable_pdf_uses_stripped_pdf_as_ocr_input(self) -> None:
        class FakeEngine:
            actual_device = "api"

            def __init__(self) -> None:
                self.words_seen: list[str] = []

            def recognize_pdf(self, source_pdf: Path, progress_callback=None):
                with fitz.open(source_pdf) as doc:
                    self.words_seen = [word[4] for word in doc[0].get_text("words")]
                return []

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.pdf"
            output = root / "output.pdf"
            with fitz.open() as doc:
                page = doc.new_page(width=200, height=100)
                page.insert_text(fitz.Point(20, 30), "visible", fontsize=12)
                page.insert_text(fitz.Point(20, 60), "hidden", fontsize=12, render_mode=3)
                doc.save(source)

            engine = FakeEngine()
            make_searchable_pdf(
                source,
                output,
                engine,  # type: ignore[arg-type]
                SimpleNamespace(ocr_max_pages=0, pdf_text_font="helv"),
            )

            self.assertEqual(["visible"], engine.words_seen)
            with fitz.open(output) as checked:
                self.assertEqual(["visible", "hidden"], [word[4] for word in checked[0].get_text("words")])

    def test_make_searchable_pdf_only_replaces_pages_with_api_results(self) -> None:
        class FakeEngine:
            actual_device = "api"

            def __init__(self) -> None:
                self.words_seen: list[list[str]] = []

            def recognize_pdf(self, source_pdf: Path, progress_callback=None):
                with fitz.open(source_pdf) as doc:
                    self.words_seen = [
                        [word[4] for word in page.get_text("words")]
                        for page in doc
                    ]
                return [
                    OcrPageResult(
                        page_number=1,
                        image_width=200,
                        image_height=100,
                        lines=[
                            OcrLine(
                                text="newocr",
                                score=None,
                                box=[[20, 50], [100, 50], [100, 70], [20, 70]],
                            )
                        ],
                    )
                ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.pdf"
            output = root / "output.pdf"
            with fitz.open() as doc:
                page = doc.new_page(width=200, height=100)
                page.insert_text(fitz.Point(20, 30), "visible1", fontsize=12)
                page.insert_text(fitz.Point(20, 60), "hidden1", fontsize=12, render_mode=3)
                page = doc.new_page(width=200, height=100)
                page.insert_text(fitz.Point(20, 30), "visible2", fontsize=12)
                page.insert_text(fitz.Point(20, 60), "hidden2", fontsize=12, render_mode=3)
                doc.save(source)

            engine = FakeEngine()
            make_searchable_pdf(
                source,
                output,
                engine,  # type: ignore[arg-type]
                SimpleNamespace(ocr_max_pages=0, pdf_text_font="helv"),
            )

            self.assertEqual([["visible1"], ["visible2"]], engine.words_seen)
            with fitz.open(output) as checked:
                page1_words = [word[4] for word in checked[0].get_text("words")]
                page2_words = [word[4] for word in checked[1].get_text("words")]

            self.assertIn("visible1", page1_words)
            self.assertIn("newocr", page1_words)
            self.assertNotIn("hidden1", page1_words)
            self.assertEqual(["visible2", "hidden2"], page2_words)

    def test_api_engine_batches_large_pdf_and_remaps_page_numbers(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.batch_page_counts: list[int] = []

            def ocr_pdf(self, source_pdf: Path, progress_callback=None):
                with fitz.open(source_pdf) as doc:
                    page_count = doc.page_count
                self.batch_page_counts.append(page_count)
                return [
                    OcrPageResult(
                        page_number=page_number,
                        image_width=200,
                        image_height=100,
                        lines=[],
                    )
                    for page_number in range(1, page_count + 1)
                ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.pdf"
            with fitz.open() as doc:
                for _ in range(3):
                    doc.new_page(width=200, height=100)
                doc.save(source)

            engine = PaddleOcrEngine(
                SimpleNamespace(
                    paddleocr_api_token="token",
                    paddleocr_api_batch_pages=2,
                    temp_dir=root,
                )
            )
            fake_client = FakeClient()
            engine._client = fake_client  # type: ignore[assignment]

            pages = engine.recognize_pdf(source)

            self.assertEqual([2, 1], fake_client.batch_page_counts)
            self.assertEqual([1, 2, 3], [page.page_number for page in pages])


if __name__ == "__main__":
    unittest.main()
