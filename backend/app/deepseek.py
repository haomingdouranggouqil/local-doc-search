from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings


@dataclass
class PublicationResult:
    status: str
    info: dict[str, Any] | None
    citation: str | None
    error: str | None = None


def extract_publication_info(
    settings: Settings, rel_path: str, first_pages: str, last_pages: str
) -> PublicationResult:
    if not settings.publication_extract_enabled:
        return PublicationResult(status="disabled", info=None, citation=None)
    if not settings.deepseek_api_key:
        return PublicationResult(status="missing_key", info=None, citation=None)

    payload_text = trim_payload(first_pages, last_pages, settings.deepseek_max_chars)
    if not payload_text.strip():
        return PublicationResult(status="no_info", info={"has_publication_info": False}, citation=None)

    messages = [
        {
            "role": "system",
            "content": (
                "You extract bibliographic publication metadata from OCR text. "
                "Return one strict JSON object only. Do not include markdown."
            ),
        },
        {
            "role": "user",
            "content": (
                "判断下面 PDF 首五页和末五页 OCR 文本里是否有明确出版信息。"
                "如果没有出版信息，或该 PDF 不是书，返回 has_publication_info=false。"
                "如果有，尽量抽取 author_or_editor、title、publisher、publication_time。"
                "citation 必须按这个格式：著者编者《书名》，出版社，出版时间。"
                "缺失字段不要编造，citation 中缺失项可省略对应片段。\n\n"
                "Return JSON schema:\n"
                "{"
                "\"has_publication_info\": boolean,"
                "\"author_or_editor\": string|null,"
                "\"title\": string|null,"
                "\"publisher\": string|null,"
                "\"publication_time\": string|null,"
                "\"citation\": string|null,"
                "\"evidence\": string|null"
                "}\n\n"
                f"PDF path: {rel_path}\n\n{payload_text}"
            ),
        },
    ]

    try:
        response = httpx.post(
            f"{settings.deepseek_base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.deepseek_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.deepseek_model,
                "messages": messages,
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
            timeout=settings.deepseek_timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        info = parse_json_object(content)
        if not info.get("has_publication_info"):
            return PublicationResult(status="no_info", info=info, citation=None)
        citation = normalize_citation(info)
        return PublicationResult(status="ready", info=info, citation=citation)
    except Exception as exc:
        return PublicationResult(status="error", info=None, citation=None, error=f"{type(exc).__name__}: {exc}")


def trim_payload(first_pages: str, last_pages: str, max_chars: int) -> str:
    first = first_pages.strip()
    last = last_pages.strip()
    text = f"--- FIRST FIVE PAGES ---\n{first}\n\n--- LAST FIVE PAGES ---\n{last}"
    if len(text) <= max_chars:
        return text
    half = max(1000, max_chars // 2)
    return (
        f"--- FIRST FIVE PAGES ---\n{first[:half]}\n\n"
        f"--- LAST FIVE PAGES ---\n{last[-half:]}"
    )[:max_chars]


def parse_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("DeepSeek response is not a JSON object")
    return value


def normalize_citation(info: dict[str, Any]) -> str | None:
    explicit = clean(info.get("citation"))
    if explicit:
        return explicit

    author = clean(info.get("author_or_editor"))
    title = clean(info.get("title"))
    publisher = clean(info.get("publisher"))
    publication_time = clean(info.get("publication_time"))
    if not title:
        return None

    head = f"{author or ''}《{title}》"
    tail = "，".join(part for part in (publisher, publication_time) if part)
    return f"{head}，{tail}" if tail else head


def clean(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().split())
