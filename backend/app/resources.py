from __future__ import annotations

import math
import os
import subprocess
from dataclasses import dataclass

from .config import Settings

BYTES_PER_GB = 1024**3


class ResourceLimitError(RuntimeError):
    pass


@dataclass(frozen=True)
class HardwareProfile:
    memory_bytes: int | None
    gpu_name: str | None
    gpu_memory_bytes: int | None


@dataclass(frozen=True)
class ResourceLimits:
    auto_tune: bool
    hardware: HardwareProfile
    max_file_mb: int
    max_output_pdf_mb: int
    preferred_ocr_dpi: int
    min_ocr_dpi: int
    max_page_pixels: int
    ocr_batch_size: int
    large_pdf_page_threshold: int
    large_pdf_dpi: int
    page_timeout_seconds: int


class ResourcePolicy:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.limits = choose_resource_limits(settings, detect_hardware())

    def effective_max_file_mb(self) -> int:
        return self.limits.max_file_mb

    def ocr_dpi_for_page(
        self, width_points: float, height_points: float, page_count: int | None = None
    ) -> int:
        dpi = max(self.limits.min_ocr_dpi, self.limits.preferred_ocr_dpi)
        if (
            page_count is not None
            and self.limits.large_pdf_page_threshold > 0
            and page_count >= self.limits.large_pdf_page_threshold
        ):
            dpi = min(dpi, max(self.limits.min_ocr_dpi, self.limits.large_pdf_dpi))
        pixels = page_pixels(width_points, height_points, dpi)
        if pixels <= self.limits.max_page_pixels:
            return dpi

        page_area = max(1.0, width_points * height_points)
        adjusted = int(math.floor(math.sqrt(self.limits.max_page_pixels / page_area) * 72))
        adjusted = max(self.limits.min_ocr_dpi, min(dpi, adjusted))
        if page_pixels(width_points, height_points, adjusted) <= self.limits.max_page_pixels:
            return adjusted

        raise ResourceLimitError(
            "PDF page is too large for OCR: "
            f"{int(width_points)}x{int(height_points)}pt exceeds "
            f"{self.limits.max_page_pixels:,} pixels even at {self.limits.min_ocr_dpi} DPI"
            )

    def ocr_batch_size(self) -> int:
        return max(1, self.limits.ocr_batch_size)

    def check_output_pdf_size(self, size_bytes: int) -> None:
        limit_bytes = self.limits.max_output_pdf_mb * 1024 * 1024
        if limit_bytes > 0 and size_bytes > limit_bytes:
            raise ResourceLimitError(
                f"OCR output PDF is too large: {size_bytes // (1024 * 1024)}MB "
                f"> {self.limits.max_output_pdf_mb}MB"
            )

    def as_dict(self) -> dict:
        hardware = self.limits.hardware
        return {
            "auto_tune": self.limits.auto_tune,
            "hardware": {
                "memory_gb": round(hardware.memory_bytes / BYTES_PER_GB, 1)
                if hardware.memory_bytes
                else None,
                "gpu_name": hardware.gpu_name,
                "gpu_memory_gb": round(hardware.gpu_memory_bytes / BYTES_PER_GB, 1)
                if hardware.gpu_memory_bytes
                else None,
            },
            "limits": {
                "max_file_mb": self.limits.max_file_mb,
                "max_output_pdf_mb": self.limits.max_output_pdf_mb,
                "preferred_ocr_dpi": self.limits.preferred_ocr_dpi,
                "min_ocr_dpi": self.limits.min_ocr_dpi,
                "max_page_pixels": self.limits.max_page_pixels,
                "ocr_batch_size": self.limits.ocr_batch_size,
                "large_pdf_page_threshold": self.limits.large_pdf_page_threshold,
                "large_pdf_dpi": self.limits.large_pdf_dpi,
                "page_timeout_seconds": self.limits.page_timeout_seconds,
            },
        }


def choose_resource_limits(settings: Settings, hardware: HardwareProfile) -> ResourceLimits:
    if not settings.resource_auto_tune:
        return ResourceLimits(
            auto_tune=False,
            hardware=hardware,
            max_file_mb=settings.max_file_mb or 512,
            max_output_pdf_mb=settings.max_output_pdf_mb,
            preferred_ocr_dpi=max(settings.ocr_min_dpi, settings.ocr_dpi),
            min_ocr_dpi=settings.ocr_min_dpi,
            max_page_pixels=settings.ocr_max_page_pixels or 25_000_000,
            ocr_batch_size=max(1, settings.ocr_batch_size or 1),
            large_pdf_page_threshold=settings.ocr_large_pdf_page_threshold,
            large_pdf_dpi=max(settings.ocr_min_dpi, settings.ocr_large_pdf_dpi),
            page_timeout_seconds=max(0, settings.ocr_page_timeout_seconds),
        )

    memory_gb = (hardware.memory_bytes or 0) / BYTES_PER_GB
    gpu_gb = (hardware.gpu_memory_bytes or 0) / BYTES_PER_GB

    if memory_gb >= 32 and gpu_gb >= 10:
        auto_file_mb, auto_output_mb, auto_pixels = 1024, 4096, 35_000_000
    elif memory_gb >= 16 and gpu_gb >= 6:
        auto_file_mb, auto_output_mb, auto_pixels = 768, 3072, 25_000_000
    elif memory_gb >= 12:
        auto_file_mb, auto_output_mb, auto_pixels = 512, 2048, 18_000_000
    elif memory_gb >= 8:
        auto_file_mb, auto_output_mb, auto_pixels = 384, 1536, 14_000_000
    else:
        auto_file_mb, auto_output_mb, auto_pixels = 256, 1024, 10_000_000

    if memory_gb >= 16 and gpu_gb >= 6:
        auto_dpi = settings.ocr_dpi
    elif memory_gb >= 8:
        auto_dpi = min(settings.ocr_dpi, 180)
    else:
        auto_dpi = min(settings.ocr_dpi, 150)

    auto_batch_size = settings.ocr_batch_size if settings.ocr_batch_size > 0 else 1

    return ResourceLimits(
        auto_tune=True,
        hardware=hardware,
        max_file_mb=min(settings.max_file_mb, auto_file_mb) if settings.max_file_mb > 0 else auto_file_mb,
        max_output_pdf_mb=settings.max_output_pdf_mb or auto_output_mb,
        preferred_ocr_dpi=max(settings.ocr_min_dpi, auto_dpi),
        min_ocr_dpi=settings.ocr_min_dpi,
        max_page_pixels=settings.ocr_max_page_pixels or auto_pixels,
        ocr_batch_size=max(1, auto_batch_size),
        large_pdf_page_threshold=settings.ocr_large_pdf_page_threshold,
        large_pdf_dpi=max(settings.ocr_min_dpi, min(auto_dpi, settings.ocr_large_pdf_dpi)),
        page_timeout_seconds=max(0, settings.ocr_page_timeout_seconds),
    )


def detect_hardware() -> HardwareProfile:
    gpu_name, gpu_memory_bytes = detect_gpu()
    return HardwareProfile(
        memory_bytes=detect_memory_bytes(),
        gpu_name=gpu_name,
        gpu_memory_bytes=gpu_memory_bytes,
    )


def detect_memory_bytes() -> int | None:
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = open(path, encoding="utf-8").read().strip()
        except OSError:
            continue
        if raw and raw != "max":
            value = int(raw)
            if 0 < value < 1 << 60:
                return value
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return int(pages * page_size)
    except (AttributeError, OSError, ValueError):
        return None


def detect_gpu() -> tuple[str | None, int | None]:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None, None
    line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    if not line:
        return None, None
    parts = [part.strip() for part in line.split(",")]
    name = parts[0] if parts else None
    try:
        memory_bytes = int(parts[1]) * 1024 * 1024 if len(parts) > 1 else None
    except ValueError:
        memory_bytes = None
    return name, memory_bytes


def page_pixels(width_points: float, height_points: float, dpi: int) -> int:
    return int(math.ceil((width_points * dpi / 72) * (height_points * dpi / 72)))
