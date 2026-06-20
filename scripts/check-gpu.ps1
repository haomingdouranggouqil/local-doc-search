$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot

docker compose exec backend python -c "from app.config import get_settings; s=get_settings(); print(f'OCR engine: {s.ocr_engine}'); print(f'OCR model: {s.paddleocr_api_model}'); print(f'PaddleOCR API token configured: {bool(s.paddleocr_api_token)}')"
