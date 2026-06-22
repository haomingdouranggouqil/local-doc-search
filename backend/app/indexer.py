from __future__ import annotations

import hashlib
import math
from pathlib import Path

from .config import Settings
from .database import Database
from .deepseek import extract_publication_info
from .ocr import PaddleOcrEngine, make_searchable_pdf
from .quota import current_quota_day
from .resources import ResourcePolicy
from .text_extractors import (
    ExtractedText,
    convert_caj_to_pdf,
    convert_office_to_pdf,
    extract_doc,
    extract_docx,
    extract_pdf_text,
    extract_plain_text,
)


class JobCancelled(RuntimeError):
    pass


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
                self._raise_if_cancelled(job_id)
                self.db.update_job(job_id, progress=0.05, message="Reading file")
            result, searchable_pdf = self._extract_or_ocr(path, document_id, job_id)
            self._raise_if_cancelled(job_id)
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
        except JobCancelled as exc:
            if job_id is not None:
                self.db.update_job(
                    job_id,
                    status="cancelled",
                    progress=1,
                    message="Cancelled",
                    error=str(exc),
                )
            self.db.fail_document(document_id, str(exc))
            self.db.record_event("cancel", f"Cancelled: {doc['rel_path']}", document_id, doc["rel_path"])
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
            if self._pdf_uses_embedded_text(path):
                if job_id is not None:
                    self.db.update_job(job_id, progress=0.2, message="Extracting embedded PDF text")
                return extract_pdf_text(path), path

            if job_id is not None:
                self.db.update_job(job_id, progress=0.12, message="Rebuilding searchable PDF with PaddleOCR API")
            output_pdf = path.with_name(f".{path.name}.{document_id}.ocr-tmp")
            ocr_result = self._rebuild_searchable_pdf(
                path,
                output_pdf,
                job_id,
                progress_start=0.12,
                progress_span=0.82,
                progress_cap=0.94,
            )
            stat = path.stat()
            self.db.update_document_file_state(document_id, size=stat.st_size, mtime=stat.st_mtime)

            return (
                ExtractedText(
                    chunks=ocr_result.chunks,
                    page_count=ocr_result.page_count,
                    text_chars=ocr_result.text_chars,
                    has_text_layer=True,
                ),
                path,
            )

        if ext == ".caj":
            if job_id is not None:
                self.db.update_job(job_id, progress=0.12, message="Converting CAJ to searchable PDF")
            pdf = convert_caj_to_pdf(
                path,
                self.settings.preview_dir / document_id,
                self.settings.caj_converter_command,
                self.settings.caj_converter_timeout_seconds,
            )
            self.resources.check_output_pdf_size(pdf.stat().st_size)
            if job_id is not None:
                self.db.update_job(job_id, progress=0.65, message="Extracting converted PDF text")
            extracted = extract_pdf_text(pdf)
            if extracted.text_chars >= self.settings.ocr_min_text_chars:
                return extracted, pdf

            if job_id is not None:
                self.db.update_job(job_id, progress=0.7, message="OCRing converted CAJ PDF")
            ocr_result = self._rebuild_searchable_pdf(
                pdf,
                pdf.with_name(f".{pdf.name}.{document_id}.ocr-tmp"),
                job_id,
                progress_start=0.7,
                progress_span=0.24,
                progress_cap=0.96,
            )
            return (
                ExtractedText(
                    chunks=ocr_result.chunks,
                    page_count=ocr_result.page_count,
                    text_chars=ocr_result.text_chars,
                    has_text_layer=True,
                ),
                pdf,
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

    def _pdf_uses_embedded_text(self, path: Path) -> bool:
        rel_path = relative_document_path(path, self.settings.document_root)
        return is_under_any_rel_path(rel_path, self.settings.pdf_text_only_rel_paths)

    def _rebuild_searchable_pdf(
        self,
        source_pdf: Path,
        output_pdf: Path,
        job_id: int | None,
        progress_start: float,
        progress_span: float,
        progress_cap: float,
    ):
        def progress(page: float, total: int, message: str) -> None:
            if job_id is None:
                return
            self._raise_if_cancelled(job_id)
            if page <= 0:
                self.db.update_job(job_id, progress=progress_start, message=message)
                return
            pct = progress_start + progress_span * (page / max(total, 1))
            display_page = max(1, min(total, math.ceil(page)))
            self.db.update_job(
                job_id,
                progress=min(pct, progress_cap),
                message=f"{message} {display_page}/{total}",
            )

        try:
            ocr_result = make_searchable_pdf(
                source_pdf,
                output_pdf,
                self.ocr_engine,
                self.settings,
                progress,
                cancel_callback=(lambda: self._raise_if_cancelled(job_id)) if job_id is not None else None,
                max_pages=0,
                resources=self.resources,
            )
            self._raise_if_cancelled(job_id)
            quota_day = current_quota_day(self.settings.paddleocr_quota_timezone)
            self.db.record_ocr_usage(
                quota_day.date,
                ocr_result.page_count,
                quota_day.start_utc,
                quota_day.end_utc,
            )
            self.resources.check_output_pdf_size(output_pdf.stat().st_size)
            output_pdf.replace(source_pdf)
            return ocr_result
        finally:
            output_pdf.unlink(missing_ok=True)

    def _raise_if_cancelled(self, job_id: int | None) -> None:
        if job_id is not None and self.db.job_cancelled(job_id):
            raise JobCancelled("Cancelled by user")

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


def relative_document_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def is_under_any_rel_path(rel_path: str, roots: set[str]) -> bool:
    normalized = rel_path.replace("\\", "/").strip("/")
    for root in roots:
        normalized_root = root.replace("\\", "/").strip("/")
        if not normalized_root:
            continue
        if normalized == normalized_root or normalized.startswith(f"{normalized_root}/"):
            return True
    return False


def document_id_for_rel_path(rel_path: str) -> str:
    normalized = rel_path.replace("\\", "/").casefold()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:20]
