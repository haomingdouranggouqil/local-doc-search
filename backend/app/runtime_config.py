from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SECRET_KEYS = {"paddleocr_api_token", "deepseek_api_key"}


def read_runtime_config(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def runtime_secret(path: Path, key: str) -> str:
    if key not in SECRET_KEYS:
        return ""
    value = read_runtime_config(path).get(key)
    return str(value or "").strip()


def write_runtime_secrets(path: Path, updates: dict[str, str]) -> dict[str, bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    current = read_runtime_config(path)
    for key, value in updates.items():
        if key not in SECRET_KEYS:
            continue
        cleaned = str(value or "").strip()
        if cleaned:
            current[key] = cleaned
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return {
        "paddleocr_api_token_configured": bool(str(current.get("paddleocr_api_token") or "").strip()),
        "deepseek_api_key_configured": bool(str(current.get("deepseek_api_key") or "").strip()),
    }
