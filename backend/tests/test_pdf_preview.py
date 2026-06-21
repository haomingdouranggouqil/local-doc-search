from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.pdf_preview import highlight_rects, render_highlighted_page_png, stored_bbox_rect


class PdfPreviewTests(unittest.TestCase):
    def test_highlight_estimates_query_rect_inside_stored_line_box(self) -> None:
        with fitz.open() as doc:
            page = doc.new_page(width=200, height=100)
            page.insert_text(fitz.Point(20, 50), "alpha beta gamma", fontsize=12)
            chunk = {
                "text": "alpha beta gamma",
                "bbox": json.dumps([[15, 30], [150, 30], [150, 60], [15, 60]]),
            }

            rects = highlight_rects(page, chunk, [chunk], "beta")
            line_rect = stored_bbox_rect(page, chunk, [chunk])

            self.assertTrue(rects)
            self.assertIsNotNone(line_rect)
            self.assertLess(rects[0].width, line_rect.width)
            self.assertTrue(line_rect.intersects(rects[0]))

    def test_highlight_prefers_ocr_bbox_over_misaligned_hidden_text(self) -> None:
        with fitz.open() as doc:
            page = doc.new_page(width=200, height=100)
            page.insert_text(fitz.Point(5, 20), "target", fontsize=12, render_mode=3)
            chunk = {
                "text": "target",
                "bbox": json.dumps([[80, 40], [160, 40], [160, 60], [80, 60]]),
            }

            rects = highlight_rects(page, chunk, [chunk], "target")

            self.assertTrue(rects)
            self.assertGreater(rects[0].x0, 90)

    def test_render_highlighted_page_png_returns_png(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "sample.pdf"
            with fitz.open() as doc:
                page = doc.new_page(width=200, height=100)
                page.insert_text(fitz.Point(20, 50), "alpha beta gamma", fontsize=12)
                doc.save(pdf_path)

            image = render_highlighted_page_png(
                pdf_path,
                1,
                match_chunk={
                    "text": "alpha beta gamma",
                    "bbox": json.dumps([[15, 30], [150, 30], [150, 60], [15, 60]]),
                },
                page_chunks=[
                    {
                        "text": "alpha beta gamma",
                        "bbox": json.dumps([[15, 30], [150, 30], [150, 60], [15, 60]]),
                    }
                ],
                query="beta",
            )

            self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))


if __name__ == "__main__":
    unittest.main()
