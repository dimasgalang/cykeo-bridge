"""Server HTTP dummy lokal (stdlib) untuk test http_client & smoke test.

TIDAK ada koneksi ke server RFID produksi — hanya loopback 127.0.0.1.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

__all__ = ["DummyServer", "RecordedRequest"]


class RecordedRequest:
    """Satu request yang diterima server dummy."""

    def __init__(self, path: str, headers: Dict[str, str], body: bytes) -> None:
        self.path = path
        self.headers = headers
        self.raw_body = body
        try:
            self.json: Any = json.loads(body.decode("utf-8")) if body else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.json = None

    @property
    def events(self) -> List[Dict[str, Any]]:
        if isinstance(self.json, dict):
            return self.json.get("events", [])
        return []

    def __repr__(self) -> str:  # pragma: no cover
        return f"<RecordedRequest {self.path} events={len(self.events)}>"


class DummyServer:
    """HTTP server yang merekam request dan mengembalikan respons terprogram.

    ``status_sequence`` dipakai untuk menguji retry: mis. ``[500, 500, 200]``
    berarti dua percobaan gagal lalu sukses.
    """

    def __init__(self, *, expected_path: str = "/api/v1/rfid/readers/CYKEO-01/events",
                 expected_api_key: Optional[str] = None,
                 status_sequence: Optional[List[int]] = None,
                 default_status: int = 200,
                 success_message: str = "ok") -> None:
        self.expected_path = expected_path
        self.expected_api_key = expected_api_key
        self.status_sequence = list(status_sequence or [])
        self.default_status = default_status
        self.success_message = success_message
        self.requests: List[RecordedRequest] = []
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ #
    def _make_handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:  # senyap
                pass

            def _read_body(self) -> bytes:
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802 - nama wajib BaseHTTPRequestHandler
                body = self._read_body()
                record = RecordedRequest(self.path, dict(self.headers), body)
                outer.requests.append(record)

                if outer.status_sequence:
                    status = outer.status_sequence.pop(0)
                else:
                    status = outer.default_status

                if outer.expected_api_key is not None:
                    sent = self.headers.get("X-Reader-Api-Key")
                    if sent != outer.expected_api_key:
                        self._send_json(401, {"success": False, "message": "API key salah"})
                        return

                if status == 200:
                    accepted = len(record.events)
                    self._send_json(200, {"success": True, "message": outer.success_message,
                                          "accepted": accepted})
                elif status == 401:
                    self._send_json(401, {"success": False, "message": "Unauthorized"})
                elif status == 422:
                    self._send_json(422, {"success": False, "message": "epc tidak valid"})
                elif status == 429:
                    self._send_json(429, {"success": False, "message": "slow down"})
                elif 500 <= status < 600:
                    self._send_json(status, {"success": False, "message": "server error"})
                else:
                    self._send_json(status, {"success": False, "message": f"status {status}"})

            def do_GET(self) -> None:  # noqa: N802
                outer.requests.append(RecordedRequest(self.path, dict(self.headers), b""))
                if self.path == outer.expected_path:
                    self._send_json(405, {"success": False, "message": "method not allowed"})
                else:
                    self._send_json(404, {"success": False, "message": "not found"})

        return Handler

    # ------------------------------------------------------------------ #
    def start(self) -> str:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("server belum start()")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "DummyServer":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()
