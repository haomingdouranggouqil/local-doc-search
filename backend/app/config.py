from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .runtime_config import runtime_secret


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    document_root: Path = Field(default=Path("/library"))
    state_dir: Path = Field(default=Path("/data"))
    sqlite_journal_mode: str = "DELETE"

    exclude_dirs: str = (
        ".docsearch,.git,.svn,backend,frontend,scripts,node_modules,"
        "__pycache__,.pytest_cache,.venv,venv"
    )
    exclude_paths: str = "README.md,.env,.env.example,docker-compose.yml,docs/design"
    supported_extensions: str = ".pdf,.txt,.md,.doc,.docx,.caj"
    pdf_text_only_paths: str = "pdf/论文"
    caj_converter_command: str = "caj2pdf convert {input} -o {output}"
    caj_converter_timeout_seconds: int = 600
    max_file_mb: int = 0
    max_output_pdf_mb: int = 0
    resource_auto_tune: bool = True

    scan_interval_seconds: int = 20
    scan_debounce_seconds: float = 2.0

    ocr_engine: str = "api"
    ocr_version: str = "PP-OCRv6"
    ocr_device: str = "api"
    paddleocr_api_url: str = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
    paddleocr_api_token: str = ""
    paddleocr_api_model: str = "PP-OCRv6"
    paddleocr_daily_page_limit: int = 20000
    paddleocr_quota_timezone: str = "Asia/Shanghai"
    paddleocr_api_batch_pages: int = 100
    paddleocr_api_transport_retries: int = 4
    paddleocr_api_poll_seconds: float = 5.0
    paddleocr_api_timeout_seconds: int = 7200
    paddleocr_api_request_timeout_seconds: int = 300
    paddleocr_use_doc_orientation_classify: bool = False
    paddleocr_use_doc_unwarping: bool = False
    paddleocr_use_textline_orientation: bool = False
    ocr_dpi: int = 200
    ocr_min_dpi: int = 120
    ocr_max_page_pixels: int = 0
    ocr_batch_size: int = 0
    ocr_large_pdf_page_threshold: int = 200
    ocr_large_pdf_dpi: int = 160
    ocr_page_timeout_seconds: int = 300
    ocr_min_text_chars: int = 80
    ocr_max_pages: int = 0
    pdf_text_font: str = "china-s"

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-pro"
    deepseek_timeout_seconds: int = 120
    deepseek_max_chars: int = 60000
    publication_extract_enabled: bool = True
    local_open_enabled: bool = True

    app_name: str = "本地资料检索"
    app_role: str = "api"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "index" / "docsearch.sqlite"

    @property
    def ocr_dir(self) -> Path:
        return self.state_dir / "ocr"

    @property
    def preview_dir(self) -> Path:
        return self.state_dir / "preview"

    @property
    def temp_dir(self) -> Path:
        return self.state_dir / "tmp"

    @property
    def runtime_config_path(self) -> Path:
        return self.state_dir / "runtime-config.json"

    @property
    def effective_paddleocr_api_token(self) -> str:
        return self.paddleocr_api_token.strip() or runtime_secret(
            self.runtime_config_path, "paddleocr_api_token"
        )

    @property
    def effective_deepseek_api_key(self) -> str:
        return self.deepseek_api_key.strip() or runtime_secret(
            self.runtime_config_path, "deepseek_api_key"
        )

    @property
    def token_status(self) -> dict[str, bool]:
        return {
            "paddleocr_api_token_configured": bool(self.effective_paddleocr_api_token),
            "deepseek_api_key_configured": bool(self.effective_deepseek_api_key),
        }

    @property
    def exclude_names(self) -> set[str]:
        return {item.strip() for item in self.exclude_dirs.split(",") if item.strip()}

    @property
    def excluded_rel_paths(self) -> set[str]:
        return {
            item.strip().replace("\\", "/").strip("/")
            for item in self.exclude_paths.split(",")
            if item.strip()
        }

    @property
    def supported_suffixes(self) -> set[str]:
        return {
            item.strip().lower()
            for item in self.supported_extensions.split(",")
            if item.strip()
        }

    @property
    def pdf_text_only_rel_paths(self) -> set[str]:
        return {
            item.strip().replace("\\", "/").strip("/")
            for item in self.pdf_text_only_paths.split(",")
            if item.strip()
        }

    def ensure_dirs(self) -> None:
        for path in (self.state_dir, self.db_path.parent, self.ocr_dir, self.preview_dir, self.temp_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
