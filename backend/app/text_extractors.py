from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import fitz
from charset_normalizer import from_bytes
from docx import Document as DocxDocument


@dataclass
class ExtractedText:
    chunks: list[dict]
    page_count: int = 0
    text_chars: int = 0
    has_text_layer: bool = False


def extract_pdf_text(path: Path) -> ExtractedText:
    chunks: list[dict] = []
    text_chars = 0
    with fitz.open(path) as doc:
        for page_index, page in enumerate(doc, start=1):
            text = page.get_text("text") or ""
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            for line_no, line in enumerate(lines, start=1):
                chunks.append(
                    {
                        "page": page_index,
                        "ordinal": len(chunks),
                        "line": line_no,
                        "text": line,
                        "source": "pdf-text",
                    }
                )
                text_chars += len(line)
        return ExtractedText(
            chunks=chunks,
            page_count=doc.page_count,
            text_chars=text_chars,
            has_text_layer=text_chars > 0,
        )


def extract_plain_text(path: Path) -> ExtractedText:
    text = decode_text_bytes(path.read_bytes())
    return chunks_from_lines(text.splitlines(), source="text")


def decode_text_bytes(raw: bytes) -> str:
    if not raw:
        return ""
    candidates: list[tuple[float, str]] = []
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5", "big5hkscs", "cp950", "utf-16"):
        try:
            decoded = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        candidates.append((text_decode_score(decoded), decoded))

    try:
        best = from_bytes(raw).best()
        if best is not None:
            decoded = str(best)
            candidates.append((text_decode_score(decoded), decoded))
    except Exception:
        pass

    if not candidates:
        return raw.decode(errors="ignore")
    return max(candidates, key=lambda item: item[0])[1]


def text_decode_score(text: str) -> float:
    if not text:
        return 0.0
    printable = sum(1 for char in text if char.isprintable() or char in "\r\n\t")
    cjk = sum(1 for char in text if "\u3400" <= char <= "\u9fff" or "\uf900" <= char <= "\ufaff")
    replacement = text.count("\ufffd")
    controls = sum(1 for char in text if ord(char) < 32 and char not in "\r\n\t")
    return printable + cjk * 2.0 - replacement * 50.0 - controls * 20.0


def extract_docx(path: Path) -> ExtractedText:
    doc = DocxDocument(path)
    lines: list[str] = []
    for paragraph in doc.paragraphs:
        if paragraph.text.strip():
            lines.append(paragraph.text)
    for table in doc.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                lines.append(" | ".join(cells))
    return chunks_from_lines(lines, source="docx")


def extract_doc(path: Path) -> ExtractedText:
    commands = (
        ["antiword", str(path)],
        ["catdoc", str(path)],
    )
    for command in commands:
        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=90,
                errors="ignore",
            )
            if result.stdout.strip():
                return chunks_from_lines(result.stdout.splitlines(), source="doc")
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
    return ExtractedText(
        chunks=[],
        page_count=0,
        text_chars=0,
        has_text_layer=False,
    )


def chunks_from_lines(lines: Iterable[str], source: str) -> ExtractedText:
    chunks: list[dict] = []
    text_chars = 0
    for line_no, raw in enumerate(lines, start=1):
        line = " ".join(raw.strip().split())
        if not line:
            continue
        chunks.append(
            {
                "page": None,
                "ordinal": len(chunks),
                "line": line_no,
                "text": line,
                "source": source,
            }
        )
        text_chars += len(line)
    return ExtractedText(
        chunks=chunks,
        page_count=0,
        text_chars=text_chars,
        has_text_layer=text_chars > 0,
    )


def convert_office_to_pdf(path: Path, output_dir: Path) -> Path | None:
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "soffice",
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(output_dir),
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    candidate = output_dir / f"{path.stem}.pdf"
    return candidate if candidate.exists() else None
