from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


HOST = os.environ.get("OPEN_HELPER_HOST", "127.0.0.1")
PORT = int(os.environ.get("OPEN_HELPER_PORT", "8765"))
DATA_ROOT = Path(os.environ.get("OPEN_HELPER_DATA_ROOT", Path.cwd() / "data")).resolve()
ALLOWED_ORIGINS = {
    origin.strip()
    for origin in os.environ.get(
        "OPEN_HELPER_ALLOWED_ORIGINS",
        "http://localhost:8517,http://127.0.0.1:8517",
    ).split(",")
    if origin.strip()
}


def resolve_data_file(rel_path: str) -> Path:
    rel_path = rel_path.replace("\\", "/").strip()
    if not rel_path:
        raise ValueError("missing rel_path")
    if rel_path.startswith("/") or re.match(r"^[A-Za-z]:", rel_path):
        raise ValueError("absolute paths are not allowed")

    parts = [part for part in rel_path.split("/") if part and part != "."]
    if any(part == ".." for part in parts):
        raise ValueError("path traversal is not allowed")

    candidate = (DATA_ROOT / Path(*parts)).resolve()
    if candidate != DATA_ROOT and DATA_ROOT not in candidate.parents:
        raise ValueError("path is outside data root")
    if not candidate.is_file():
        raise FileNotFoundError(str(candidate))
    return candidate


def open_file(path: Path) -> dict[str, object]:
    if sys.platform.startswith("win"):
        return open_file_windows(path)
    elif sys.platform == "darwin":
        process = subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"method": "open", "pid": process.pid}
    else:
        process = subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"method": "xdg-open", "pid": process.pid}


def open_file_windows(path: Path) -> dict[str, object]:
    if path.suffix.lower() in {".txt", ".md"}:
        return popen_visible(["notepad.exe", str(path)], "notepad")

    os.startfile(str(path))
    return {"method": "windows-startfile", "pid": None}


def popen_visible(command: list[str], method: str) -> dict[str, object]:
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    return {"method": method, "pid": process.pid}


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalDocOpenHelper/1.0"

    def log_message(self, format: str, *args: object) -> None:
        sys.stdout.write("%s - %s\n" % (self.address_string(), format % args))
        sys.stdout.flush()

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/health":
            self._send_json(200, {"ok": True, "data_root": str(DATA_ROOT)})
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._send_cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "content-type")
        self.end_headers()

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/open":
            self._send_json(404, {"ok": False, "error": "not found"})
            return

        try:
            length = int(self.headers.get("content-length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            path = resolve_data_file(str(payload.get("rel_path", "")))
            result = open_file(path)
            print(f"Opened file: {path} via {result}", flush=True)
            self._send_json(200, {"ok": True, "path": str(path), **result})
        except FileNotFoundError as exc:
            self._send_json(404, {"ok": False, "error": f"file not found: {exc}"})
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})

    def _send_cors_headers(self) -> None:
        origin = self.headers.get("origin")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Local document open helper listening on http://{HOST}:{PORT}")
    print(f"Data root: {DATA_ROOT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
