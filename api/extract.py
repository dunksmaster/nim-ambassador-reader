"""Vercel function: POST /api/extract with a JSON {"url": "https://..."} body.

The NVIDIA key is read from the NVIDIA_API_KEY environment variable by
ambassador_reader.core. This file does not read, store, or log the key.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ambassador_reader.web import client_key_from_headers, extract_for_web

_MAX_BODY_BYTES = 16_384


def _url_from_body(request: BaseHTTPRequestHandler) -> str:
    raw_length = request.headers.get("Content-Length")
    if raw_length is None or not str(raw_length).strip():
        raise ValueError("Request body must be a JSON object with a string url field.")
    try:
        length = int(str(raw_length).strip())
    except ValueError as exc:
        raise ValueError("Content-Length is not valid.") from exc
    if length < 0 or length > _MAX_BODY_BYTES:
        raise ValueError("Request body is too large.")
    raw = request.rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Request body must be JSON.") from exc
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object with a string url field.")
    url = data.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("Request body must be a JSON object with a string url field.")
    return url


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._send(405, {"error": "Send a POST request with a JSON url field."})

    def do_POST(self) -> None:
        try:
            status, payload = self._post()
        except Exception:
            status, payload = 500, {"error": "Unexpected server error."}
        self._send(status, payload)

    def _post(self) -> tuple[int, dict]:
        try:
            url = _url_from_body(self)
        except ValueError as exc:
            return 400, {"error": str(exc)}
        return extract_for_web(url, client_key=client_key_from_headers(self.headers))

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if status == 429:
            self.send_header("Retry-After", "60")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        # Request line and status code only. Do not log headers, the body, or the API key.
        sys.stderr.write("api/extract " + (fmt % args) + "\n")
