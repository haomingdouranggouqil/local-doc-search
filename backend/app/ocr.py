from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import fitz
import httpx

from .config import Settings
from .resources import ResourcePolicy

ProgressCallback = Callable[[float, int, str], None]


@dataclass
class OcrLine:
    text: str
    score: float | None
    box: list[list[float]]
    image_width: float | None = None
    image_height: float | None = None


@dataclass
class OcrPageResult:
    page_number: int
    image_width: float | None
    image_height: float | None
    lines: list[OcrLine]


@dataclass
class OcrPdfResult:
    output_pdf: Path
    chunks: list[dict]
    page_count: int
    text_chars: int


class PaddleOcrApiError(RuntimeError):
    pass


class PaddleOcrEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.actual_device = "api"
        self._client = PaddleOcrApiClient(settings)

    def recognize_pdf(
        self,
        source_pdf: Path,
        progress_callback: ProgressCallback | None = None,
    ) -> list[OcrPageResult]:
        return self._client.ocr_pdf(source_pdf, progress_callback)


class PaddleOcrApiClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def ocr_pdf(
        self,
        source_pdf: Path,
        progress_callback: ProgressCallback | None = None,
    ) -> list[OcrPageResult]:
        if not self.settings.paddleocr_api_token:
            raise PaddleOcrApiError("PADDLEOCR_API_TOKEN is not configured")
        if not source_pdf.exists():
            raise PaddleOcrApiError(f"File not found: {source_pdf}")

        with httpx.Client(
            timeout=httpx.Timeout(
                self.settings.paddleocr_api_request_timeout_seconds,
                connect=min(30, self.settings.paddleocr_api_request_timeout_seconds),
            ),
            follow_redirects=True,
        ) as client:
            job_id = self._submit_file(client, source_pdf)
            jsonl_url = self._poll_job(client, job_id, progress_callback)
            jsonl_text = self._download_jsonl(client, jsonl_url)
        return parse_api_jsonl(jsonl_text)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"bearer {self.settings.paddleocr_api_token}"}

    def _submit_file(self, client: httpx.Client, source_pdf: Path) -> str:
        optional_payload = {
            "useDocOrientationClassify": self.settings.paddleocr_use_doc_orientation_classify,
            "useDocUnwarping": self.settings.paddleocr_use_doc_unwarping,
            "useTextlineOrientation": self.settings.paddleocr_use_textline_orientation,
        }
        data = {
            "model": self.settings.paddleocr_api_model,
            "optionalPayload": json.dumps(optional_payload),
        }
        with source_pdf.open("rb") as handle:
            files = {"file": (source_pdf.name, handle, "application/pdf")}
            response = client.post(
                self.settings.paddleocr_api_url,
                headers=self.headers,
                data=data,
                files=files,
            )
        payload = self._json_response(response, "submit OCR job")
        try:
            return str(payload["data"]["jobId"])
        except KeyError as exc:
            raise PaddleOcrApiError(f"OCR API response did not include jobId: {payload}") from exc

    def _poll_job(
        self,
        client: httpx.Client,
        job_id: str,
        progress_callback: ProgressCallback | None,
    ) -> str:
        deadline = time.monotonic() + self.settings.paddleocr_api_timeout_seconds
        last_message = ""
        while True:
            if time.monotonic() > deadline:
                raise PaddleOcrApiError(f"OCR API job timed out: {job_id}")

            response = client.get(f"{self.settings.paddleocr_api_url}/{job_id}", headers=self.headers)
            payload = self._json_response(response, "poll OCR job")
            data = payload.get("data") or {}
            state = str(data.get("state") or "").lower()
            progress = data.get("extractProgress") or {}
            total_pages = int(progress.get("totalPages") or 0)
            extracted_pages = int(progress.get("extractedPages") or 0)

            if state in {"pending", "running", ""}:
                message = "Waiting for PaddleOCR API" if state == "pending" else "PaddleOCR API running"
                if progress_callback:
                    progress_callback(extracted_pages, total_pages, message)
                rendered = f"{message} {extracted_pages}/{total_pages}" if total_pages else message
                if rendered != last_message:
                    last_message = rendered
                time.sleep(max(1.0, self.settings.paddleocr_api_poll_seconds))
                continue

            if state == "done":
                if progress_callback:
                    progress_callback(max(extracted_pages, total_pages), max(total_pages, extracted_pages, 1), "PaddleOCR API completed")
                result_url = data.get("resultUrl") or {}
                json_url = result_url.get("jsonUrl")
                if not json_url:
                    raise PaddleOcrApiError(f"OCR API job completed without jsonUrl: {payload}")
                return str(json_url)

            if state == "failed":
                raise PaddleOcrApiError(str(data.get("errorMsg") or "OCR API job failed"))

            raise PaddleOcrApiError(f"Unexpected OCR API state {state!r}: {payload}")

    def _download_jsonl(self, client: httpx.Client, jsonl_url: str) -> str:
        response = client.get(jsonl_url)
        if response.status_code != 200:
            raise PaddleOcrApiError(
                f"download OCR JSONL failed: HTTP {response.status_code}; {response.text[:500]}"
            )
        return response.text

    def _json_response(self, response: httpx.Response, action: str) -> dict[str, Any]:
        if response.status_code != 200:
            raise PaddleOcrApiError(
                f"{action} failed: HTTP {response.status_code}; {response.text[:500]}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PaddleOcrApiError(f"{action} returned non-JSON response") from exc
        if int(payload.get("code", 0) or 0) != 0:
            raise PaddleOcrApiError(f"{action} failed: {payload}")
        return payload


def make_searchable_pdf(
    source_pdf: Path,
    output_pdf: Path,
    engine: PaddleOcrEngine,
    settings: Settings,
    progress_callback: ProgressCallback | None = None,
    max_pages: int | None = None,
    resources: ResourcePolicy | None = None,
) -> OcrPdfResult:
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    output_pdf.unlink(missing_ok=True)

    if progress_callback:
        progress_callback(0, 0, "Uploading PDF to PaddleOCR API")
    api_pages = engine.recognize_pdf(source_pdf, progress_callback)
    pages_by_number = {page.page_number: page for page in api_pages}

    chunks: list[dict] = []
    text_chars = 0
    with fitz.open(source_pdf) as doc:
        page_total = doc.page_count
        page_limit = settings.ocr_max_pages if max_pages is None else max_pages
        target_page_count = page_limit if page_limit and page_limit > 0 else page_total
        target_page_count = min(page_total, target_page_count)

        for page_number in range(1, target_page_count + 1):
            page = doc.load_page(page_number - 1)
            page_result = pages_by_number.get(page_number)
            if not page_result:
                if progress_callback:
                    progress_callback(page_number, page_total, "Embedding OCR text")
                continue

            image_width, image_height = page_image_size(page_result, page.rect)
            line_no = 0
            for line in page_result.lines:
                clean = " ".join(line.text.split())
                if not clean:
                    continue
                rect = image_box_to_pdf_rect(
                    line.box,
                    line.image_width or image_width,
                    line.image_height or image_height,
                    page.rect,
                )
                insert_hidden_text(page, rect, clean, settings.pdf_text_font)
                line_no += 1
                chunks.append(
                    {
                        "page": page_number,
                        "ordinal": len(chunks),
                        "line": line_no,
                        "text": clean,
                        "bbox": line.box,
                        "source": "paddleocr-api",
                    }
                )
                text_chars += len(clean)
            if progress_callback:
                progress_callback(page_number, page_total, "Embedding OCR text")

        if progress_callback:
            progress_callback(target_page_count, page_total, "Saving searchable PDF")
        doc.save(output_pdf, garbage=4, deflate=True)

    if resources:
        resources.check_output_pdf_size(output_pdf.stat().st_size)

    return OcrPdfResult(
        output_pdf=output_pdf,
        chunks=chunks,
        page_count=target_page_count,
        text_chars=text_chars,
    )


def parse_api_jsonl(jsonl_text: str) -> list[OcrPageResult]:
    pages: list[OcrPageResult] = []
    next_page_number = 1
    for raw_line in jsonl_text.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        payload = json.loads(raw_line)
        result = payload.get("result") or {}
        ocr_results = result.get("ocrResults") or []
        if not isinstance(ocr_results, list):
            ocr_results = [ocr_results]
        for res in ocr_results:
            if not isinstance(res, dict):
                continue
            pruned = res.get("prunedResult") or res.get("result") or res
            if not isinstance(pruned, dict):
                continue
            image_width, image_height = extract_image_size(pruned, res, result, payload)
            lines = parse_ocr_result(pruned, image_width, image_height)
            if not image_width or not image_height:
                image_width, image_height = infer_image_size(lines)
            page_number = extract_page_number(pruned, res, result) or next_page_number
            pages.append(
                OcrPageResult(
                    page_number=page_number,
                    image_width=image_width,
                    image_height=image_height,
                    lines=lines,
                )
            )
            next_page_number = max(next_page_number + 1, page_number + 1)
    return pages


def parse_ocr_result(
    data: dict[str, Any],
    image_width: float | None,
    image_height: float | None,
) -> list[OcrLine]:
    texts = data.get("rec_texts") or data.get("texts") or data.get("text") or data.get("rec_text")
    scores = data.get("rec_scores") or data.get("scores") or data.get("rec_score") or []
    boxes = data.get("rec_polys") or data.get("dt_polys") or data.get("rec_boxes") or data.get("boxes") or []
    if isinstance(texts, str):
        texts = [texts]
    if isinstance(scores, (int, float)):
        scores = [scores]
    if not isinstance(texts, list):
        return []
    if not isinstance(scores, list):
        scores = []
    if not isinstance(boxes, list):
        boxes = []

    lines: list[OcrLine] = []
    for idx, text in enumerate(texts):
        box = normalize_box(boxes[idx] if idx < len(boxes) else None)
        if not text or not box:
            continue
        score = None
        if idx < len(scores):
            try:
                score = float(scores[idx])
            except (TypeError, ValueError):
                score = None
        lines.append(
            OcrLine(
                text=str(text),
                score=score,
                box=box,
                image_width=image_width,
                image_height=image_height,
            )
        )
    return lines


def extract_page_number(*nodes: Any) -> int | None:
    for node in nodes:
        if not isinstance(node, dict):
            continue
        for key in ("page", "pageNo", "page_no", "pageNumber", "page_number"):
            value = node.get(key)
            if value is None:
                continue
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            return parsed if parsed >= 1 else parsed + 1
    return None


def extract_image_size(*nodes: Any) -> tuple[float | None, float | None]:
    for node in nodes:
        width, height = read_image_size(node)
        if width and height:
            return width, height
    return None, None


def read_image_size(node: Any) -> tuple[float | None, float | None]:
    if not isinstance(node, dict):
        return None, None
    data_info = node.get("dataInfo")
    if isinstance(data_info, dict):
        width, height = read_image_size(data_info)
        if width and height:
            return width, height
    for width_key, height_key in (
        ("width", "height"),
        ("imageWidth", "imageHeight"),
        ("img_width", "img_height"),
    ):
        if width_key in node and height_key in node:
            try:
                width = float(node[width_key])
                height = float(node[height_key])
            except (TypeError, ValueError):
                continue
            if width > 0 and height > 0:
                return width, height
    shape = node.get("input_img_shape") or node.get("image_shape") or node.get("img_shape")
    if isinstance(shape, (list, tuple)) and len(shape) >= 2:
        try:
            height = float(shape[0])
            width = float(shape[1])
        except (TypeError, ValueError):
            return None, None
        if width > 0 and height > 0:
            return width, height
    return None, None


def infer_image_size(lines: list[OcrLine]) -> tuple[float | None, float | None]:
    max_x = 0.0
    max_y = 0.0
    for line in lines:
        for x, y in line.box:
            max_x = max(max_x, float(x))
            max_y = max(max_y, float(y))
    if max_x > 0 and max_y > 0:
        return max_x, max_y
    return None, None


def page_image_size(page_result: OcrPageResult, page_rect: fitz.Rect) -> tuple[float, float]:
    width = page_result.image_width
    height = page_result.image_height
    if not width or not height:
        width, height = infer_image_size(page_result.lines)
    if not width or not height:
        width, height = page_rect.width, page_rect.height
    return max(1.0, float(width)), max(1.0, float(height))


def normalize_box(value: Any) -> list[list[float]] | None:
    if value is None:
        return None
    try:
        if len(value) == 4 and all(not isinstance(item, (list, tuple)) for item in value):
            x1, y1, x2, y2 = [float(item) for item in value]
            return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
        points = [[float(point[0]), float(point[1])] for point in value]
        if len(points) >= 4:
            return points[:4]
    except (TypeError, ValueError, IndexError):
        return None
    return None


def image_box_to_pdf_rect(
    box: list[list[float]],
    image_width: float,
    image_height: float,
    page_rect: fitz.Rect,
) -> fitz.Rect:
    xs = [float(point[0]) for point in box]
    ys = [float(point[1]) for point in box]
    image_width = max(float(image_width or 1), max(xs, default=1.0), 1.0)
    image_height = max(float(image_height or 1), max(ys, default=1.0), 1.0)
    x0 = min(xs) / image_width * page_rect.width
    x1 = max(xs) / image_width * page_rect.width
    y0 = min(ys) / image_height * page_rect.height
    y1 = max(ys) / image_height * page_rect.height
    x0 = max(0.0, min(page_rect.width, x0))
    x1 = max(0.0, min(page_rect.width, x1))
    y0 = max(0.0, min(page_rect.height, y0))
    y1 = max(0.0, min(page_rect.height, y1))
    height = max(4.0, y1 - y0)
    if x1 <= x0:
        x1 = min(page_rect.width, x0 + 1)
    if y1 <= y0:
        y1 = min(page_rect.height, y0 + height)
    return fitz.Rect(x0, y0, x1, y1)


def insert_hidden_text(page: fitz.Page, rect: fitz.Rect, text: str, fontname: str) -> None:
    fontsize = max(3.5, min(rect.height * 0.78, 18))
    for candidate_font in (fontname, "helv"):
        common = {
            "fontsize": fontsize,
            "fontname": candidate_font,
            "color": (0, 0, 0),
            "overlay": True,
        }
        for invisible in ({"render_mode": 3}, {"fill_opacity": 0}):
            try:
                remaining = page.insert_textbox(rect, text, **invisible, **common)
                if remaining is None or remaining >= 0:
                    return
            except Exception:
                pass
        try:
            point = fitz.Point(rect.x0, max(rect.y0 + fontsize, rect.y1))
            page.insert_text(point, text, render_mode=3, **common)
            return
        except Exception:
            pass


def paddle_cuda_available() -> bool:
    return False
