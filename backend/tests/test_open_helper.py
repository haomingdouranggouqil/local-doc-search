from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


def load_open_helper():
    script = Path(__file__).resolve().parents[2] / "scripts" / "open-helper.py"
    spec = importlib.util.spec_from_file_location("open_helper", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load open-helper.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OpenHelperTests(unittest.TestCase):
    def test_windows_pdf_open_uses_startfile_with_full_path(self) -> None:
        helper = load_open_helper()
        path = Path(r"C:\Users\Administrator\Desktop\资料\data\pdf\诗集\清诗三百首 (钱仲联 选 钱学增 注).pdf")
        calls: list[str] = []
        had_startfile = hasattr(helper.os, "startfile")
        original_startfile = getattr(helper.os, "startfile", None)
        helper.os.startfile = lambda target: calls.append(target)
        try:
            with patch.object(helper.subprocess, "run", side_effect=AssertionError("PowerShell must not be used")):
                result = helper.open_file_windows(path)
        finally:
            if had_startfile:
                helper.os.startfile = original_startfile
            else:
                delattr(helper.os, "startfile")

        self.assertEqual({"method": "windows-startfile", "pid": None}, result)
        self.assertEqual([str(path)], calls)


if __name__ == "__main__":
    unittest.main()
