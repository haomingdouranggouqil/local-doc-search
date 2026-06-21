from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

import fitz

from .ocr import OcrLine, image_box_to_pdf_rect, infer_page_image_size


def render_highlighted_page_png(
    pdf_path: Path,
    page_number: int,
    *,
    match_chunk: dict[str, Any] | None = None,
    page_chunks: Iterable[dict[str, Any]] = (),
    query: str = "",
    zoom: float = 2.0,
) -> bytes:
    with fitz.open(pdf_path) as doc:
        if page_number < 1 or page_number > doc.page_count:
            raise IndexError(page_number)
        page = doc.load_page(page_number - 1)
        for rect in highlight_rects(page, match_chunk, list(page_chunks), query):
            draw_highlight(page, rect)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return pixmap.tobytes("png")


def highlight_rects(
    page: fitz.Page,
    match_chunk: dict[str, Any] | None,
    page_chunks: list[dict[str, Any]],
    query: str,
) -> list[fitz.Rect]:
    line_rect = stored_bbox_rect(page, match_chunk, page_chunks)
    if line_rect:
        query_rect = query_rect_from_line_bbox(line_rect, match_chunk, query)
        if query_rect:
            return [query_rect]
        return [line_rect]

    exact_rects = search_rects(page, query)
    if exact_rects:
        return exact_rects

    if match_chunk:
        chunk_text = str(match_chunk.get("text") or "").strip()
        for term in candidate_search_terms(chunk_text):
            rects = search_rects(page, term)
            if rects:
                return rects

    return []


def query_rect_from_line_bbox(
    line_rect: fitz.Rect,
    match_chunk: dict[str, Any] | None,
    query: str,
) -> fitz.Rect | None:
    if not match_chunk:
        return None
    text = compact_text(match_chunk.get("text"))
    query = compact_text(query)
    if not text or not query:
        return None
    start = text.find(query)
    if start < 0:
        return None
    end = start + len(query)
    width = max(1.0, line_rect.width)
    x0 = line_rect.x0 + width * (start / len(text))
    x1 = line_rect.x0 + width * (end / len(text))
    return fitz.Rect(x0, line_rect.y0, max(x1, x0 + 1.0), line_rect.y1)


def stored_bbox_rect(
    page: fitz.Page,
    match_chunk: dict[str, Any] | None,
    page_chunks: list[dict[str, Any]],
) -> fitz.Rect | None:
    if not match_chunk:
        return None
    box = parse_bbox(match_chunk.get("bbox"))
    if not box:
        return None
    image_width, image_height = inferred_page_image_size(page, page_chunks)
    return image_box_to_pdf_rect(box, image_width, image_height, page.rect)


def inferred_page_image_size(
    page: fitz.Page,
    page_chunks: list[dict[str, Any]],
) -> tuple[float, float]:
    lines = [
        OcrLine(text="", score=None, box=box)
        for chunk in page_chunks
        if (box := parse_bbox(chunk.get("bbox")))
    ]
    width, height = infer_page_image_size(lines, page.rect)
    if not width or not height:
        width, height = page.rect.width, page.rect.height
    return max(1.0, float(width)), max(1.0, float(height))


def parse_bbox(value: Any) -> list[list[float]] | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, list):
        return None
    try:
        points = [[float(point[0]), float(point[1])] for point in value]
    except (TypeError, ValueError, IndexError):
        return None
    return points[:4] if len(points) >= 4 else None


def search_rects(page: fitz.Page, term: str, limit: int = 20) -> list[fitz.Rect]:
    term = " ".join(str(term or "").split())
    if not term:
        return []
    rects = page.search_for(term)
    return rects[:limit]


def candidate_search_terms(text: str) -> list[str]:
    text = " ".join(str(text or "").split())
    if not text:
        return []
    return [text, text[:80]] if len(text) > 80 else [text]


def compact_text(value: Any) -> str:
    return "".join(str(value or "").split())


def draw_highlight(page: fitz.Page, rect: fitz.Rect) -> None:
    rect = padded_rect(rect, 1.5)
    shape = page.new_shape()
    shape.draw_rect(rect)
    shape.finish(
        color=(0.95, 0.48, 0.0),
        fill=(1.0, 0.85, 0.15),
        width=1.2,
        fill_opacity=0.38,
        stroke_opacity=0.95,
    )
    shape.commit(overlay=True)


def padded_rect(rect: fitz.Rect, padding: float) -> fitz.Rect:
    return fitz.Rect(
        rect.x0 - padding,
        rect.y0 - padding,
        rect.x1 + padding,
        rect.y1 + padding,
    )
