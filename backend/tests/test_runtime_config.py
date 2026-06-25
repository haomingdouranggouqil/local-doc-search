from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.runtime_config import write_runtime_secrets


class RuntimeConfigTests(unittest.TestCase):
    def test_settings_reads_runtime_tokens_when_env_tokens_are_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                document_root=root / "library",
                state_dir=root / "state",
                paddleocr_api_token="",
                deepseek_api_key="",
                siliconflow_api_key="",
            )

            write_runtime_secrets(
                settings.runtime_config_path,
                {
                    "paddleocr_api_token": "ocr-token",
                    "deepseek_api_key": "ds-token",
                    "siliconflow_api_key": "sf-token",
                },
            )

            self.assertEqual("ocr-token", settings.effective_paddleocr_api_token)
            self.assertEqual("ds-token", settings.effective_deepseek_api_key)
            self.assertEqual("sf-token", settings.effective_siliconflow_api_key)
            self.assertEqual(
                {
                    "paddleocr_api_token_configured": True,
                    "deepseek_api_key_configured": True,
                    "siliconflow_api_key_configured": True,
                },
                settings.token_status,
            )

    def test_env_token_takes_priority_over_runtime_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                document_root=root / "library",
                state_dir=root / "state",
                paddleocr_api_token="env-token",
                deepseek_api_key="env-ds-token",
                siliconflow_api_key="env-sf-token",
            )

            write_runtime_secrets(
                settings.runtime_config_path,
                {
                    "paddleocr_api_token": "runtime-token",
                    "deepseek_api_key": "runtime-ds-token",
                    "siliconflow_api_key": "runtime-sf-token",
                },
            )

            self.assertEqual("env-token", settings.effective_paddleocr_api_token)
            self.assertEqual("env-ds-token", settings.effective_deepseek_api_key)
            self.assertEqual("env-sf-token", settings.effective_siliconflow_api_key)


if __name__ == "__main__":
    unittest.main()
