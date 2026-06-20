from __future__ import annotations

import hashlib
import math
from pathlib import Path

from .config import Settings
from .database import Database
from .deepseek import extract_publication_info
from .ocr import PaddleOcrEngine, make_searchable_pdf
from .resources import ResourcePolicy
from .text_extractors import (
    ExtractedText,
    convert_office_to_pdf,
    extract_doc,
    extract_docx,
    extract_plain_text,
)


class DocumentIndexer:
    def __init__(self, settings: Settings, db: Database, resources: ResourcePolicy):
        self.settings = settings
        self.db = db
        self.resources = resources
        self.ocr_engine = PaddleOcrEngine(settings)

    def process(self, document_id: str, job_id: int | None = None) -> None:
        doc = self.db.get_document(document_id)
        if doc is None:
            return
        path = Path(doc["path"])
        if not path.exists():
            self.db.fail_document(document_id, "File does not exist")
            return

        try:
            if job_id is not None:
                self.db.update_job(job_id, progress=0.05, message="Reading file")
            result, searchable_pdf = self._extract_or_ocr(path, document_id, job_id)
            status = "ready" if result.text_chars > 0 else "empty"
            self.db.replace_chunks(
                document_id,
                result.chunks,
                status=status,
                searchable_pdf=str(searchable_pdf) if searchable_pdf else None,
                page_count=result.page_count,
                text_chars=result.text_chars,
                has_text_layer=result.has_text_layer,
            )
            if path.suffix.lower() == ".pdf":
                self._extract_publication_info(document_id, doc["rel_path"], result, job_id)
            if job_id is not None:
                self.db.update_job(job_id, status="done", progress=1, message="Done")
            self.db.record_event("index", f"Indexed: {doc['rel_path']}", document_id, doc["rel_path"])
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.db.fail_document(document_id, error)
            if job_id is not None:
                self.db.update_job(job_id, status="failed", progress=1, message="Failed", error=error)
            self.db.record_event("error", f"Failed: {doc['rel_path']}; {error}", document_id, doc["rel_path"])
            raise

    def _extract_or_ocr(
        self, path: Path, document_id: str, job_id: int | None
    ) -> tuple[ExtractedText, Path | None]:
        ext = path.suffix.lower()
        if ext == ".pdf":
            if job_id is not None:
                self.db.update_job(job_id, progress=0.12, message="Rebuilding searchable PDF with PaddleOCR API")
            output_pdf = path.with_name(f".{path.name}.{document_id}.ocr-tmp")

            def progress(page: float, total: int, message: str) -> None:
                if job_id is None:
                    return
                if page <= 0:
                    self.db.update_job(job_id, progress=0.1, message=message)
                    return
                pct = 0.12 + 0.82 * (page / max(total, 1))
                display_page = max(1, min(total, math.ceil(page)))
                self.db.update_job(
                    job_id,
                    progress=min(pct, 0.94),
                    message=f"{message} {display_page}/{total}",
                )

            try:
                ocr_result = make_searchable_pdf(
                    path,
                    output_pdf,
                    self.ocr_engine,
                    self.settings,
                    progress,
                    max_pages=0,
                    resources=self.resources,
                )
                self.resources.check_output_pdf_size(output_pdf.stat().st_size)
                output_pdf.replace(path)
                stat = path.stat()
                self.db.update_document_file_state(document_id, size=stat.st_size, mtime=stat.st_mtime)
            finally:
                output_pdf.unlink(missing_ok=True)

            return (
                ExtractedText(
                    chunks=ocr_result.chunks,
                    page_count=ocr_result.page_count,
                    text_chars=ocr_result.text_chars,
                    has_text_layer=True,
                ),
                path,
            )

        if ext in {".txt", ".md"}:
            return extract_plain_text(path), None

        if ext == ".docx":
            extracted = extract_docx(path)
            pdf = convert_office_to_pdf(path, self.settings.preview_dir / document_id)
            return extracted, pdf

        if ext == ".doc":
            extracted = extract_doc(path)
            pdf = convert_office_to_pdf(path, self.settings.preview_dir / document_id)
            return extracted, pdf

        return ExtractedText(chunks=[], text_chars=0), None

    def _extract_publication_info(
        self,
        document_id: str,
        rel_path: str,
        result: ExtractedText,
        job_id: int | None,
    ) -> None:
        if job_id is not None:
            self.db.update_job(job_id, progress=0.97, message="Extracting publication metadata")
        first_pages, last_pages = publication_text_window(result.chunks, result.page_count)
        pub = extract_publication_info(self.settings, rel_path, first_pages, last_pages)
        self.db.save_publication_info(
            document_id,
            status=pub.status,
            info=pub.info,
            citation=pub.citation,
            error=pub.error,
        )
        if pub.status == "ready":
            self.db.record_event("publication", f"Publication metadata found: {rel_path}", document_id, rel_path)
        elif pub.status == "error":
            self.db.record_event("publication_error", f"Publication metadata failed: {rel_path}", document_id, rel_path)


def publication_text_window(chunks: list[dict], page_count: int) -> tuple[str, str]:
    if not chunks or page_count <= 0:
        return "", ""
    first_pages = set(range(1, min(5, page_count) + 1))
    last_start = max(1, page_count - 4)
    last_pages = set(range(last_start, page_count + 1))
    first: list[str] = []
    last: list[str] = []
    for chunk in chunks:
        page = chunk.get("page")
        text = str(chunk.get("text") or "").strip()
        if not text:
            continue
        line = f"[p.{page}] {text}" if page else text
        if page in first_pages:
            first.append(line)
        if page in last_pages:
            last.append(line)
    return "\n".join(first), "\n".join(last)


def document_id_for_rel_path(rel_path: str) -> str:
    normalized = rel_path.replace("\\", "/").casefold()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:20]
