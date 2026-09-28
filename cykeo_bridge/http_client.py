"""HTTP client — POST batch event ke server RFID (kontrak §4).

Endpoint (bentuknya TIDAK boleh diubah):
    POST {server_url}/api/v1/rfid/readers/{reader_code}/events
Header:
    X-Reader-Api-Key: <api key>
    Content-Type: application/json
Body:
    {"events": [{"epc": "<MENTAH dari device>", "antenna": 1, "rssi": -42, "timestamp": "..."}]}

Perilaku:
- 200 -> sukses, event di-commit dari antrean.
- 401 -> FATAL. Batch dikembalikan ke antrean, agent berhenti (api key salah).
- 422 -> batch DITOLAK server; event dipindah ke file rejected (tidak retry
  membludak), pesan dari server ikut dilog.
- 4xx lain (400/404/405/413/429) -> error, retry dengan backoff sesuai kode.
- 5xx / timeout / error jaringan -> retry exponential backoff.
- api_key tidak pernah masuk log.
"""

from __future__ import annotations

import json
import logging
import random
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from .config import Config

__all__ = [
    "HttpResult",
    "RfidHttpClient",
    "AuthError",
    "build_payload",
    "STATUS_AUTH_FAILED",
]

logger = logging.getLogger("cykeo_bridge.http")

STATUS_AUTH_FAILED = 401
STATUS_REJECTED = 422
USER_AGENT = "cykeo-bridge/1.0 (+python-urllib)"

#: Kode yang tidak perlu retry karena permintaan memang salah.
_NON_RETRYABLE = {400, 401, 404, 405, 406, 410, 415, 422, 501}


class AuthError(RuntimeError):
    """401 — API key ditolak. FATAL: jangan retry, hentikan agent."""

    def __init__(self, url: str, status: int = STATUS_AUTH_FAILED, message: str = "") -> None:
        super().__init__(message or f"Autentikasi ditolak server ({status}) di {url}")
        self.url = url
        self.status = status
        self.server_message = message


@dataclass
class HttpResult:
    """Hasil satu percobaan POST."""

    ok: bool
    status: Optional[int] = None
    accepted: int = 0
    message: str = ""
    error: str = ""
    attempts: int = 1
    fatal: bool = False
    rejected: bool = False
    retryable: bool = False
    duration_s: float = 0.0

    def as_log(self) -> str:
        if self.ok:
            return f"200 accepted={self.accepted} msg={self.message!r} attempts={self.attempts}"
        if self.rejected:
            return f"422 batch rejected: {self.message!r}"
        if self.fatal:
            return f"401 auth gagal: {self.message!r}"
        return f"gagal status={self.status} error={self.error or self.message!r} attempts={self.attempts}"


def build_payload(events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Bentuk body kontrak §4.

    Field per event dibatasi ke ``epc``/``antenna``/``rssi``/``timestamp`` dan
    ``epc`` dikirim apa adanya (TIDAK dinormalisasi) — normalisasi dilakukan
    server (Fase 1).
    """
    clean: List[Dict[str, Any]] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        epc = event.get("epc")
        if epc is None or (isinstance(epc, str) and epc.strip() == ""):
            # epc WAJIB string tidak kosong (kontrak §4)
            continue
        clean.append({
            "epc": epc,
            "antenna": event.get("antenna"),
            "rssi": event.get("rssi"),
            "timestamp": event.get("timestamp"),
        })
    return {"events": clean}


class RfidHttpClient:
    """Klien POST batch ke server RFID.

    Hanya koneksi KELUAR; bridge tidak pernah membuka listener.
    """

    def __init__(self, config: Config, *, opener: Optional[urllib.request.OpenerDirector] = None,
                 sleep: Optional[Callable[[float], None]] = None,
                 verify_tls: bool = True) -> None:
        self.config = config
        self.url = config.events_url
        self.timeout = float(config.request_timeout)
        self.max_retries = max(0, int(config.max_retries))
        self.backoff_base = float(config.backoff_base)
        self.backoff_max = float(config.backoff_max)
        self._sleep = sleep or time.sleep
        self._opener = opener or self._build_opener(verify_tls)

    # ------------------------------------------------------------------ #
    def _build_opener(self, verify_tls: bool) -> urllib.request.OpenerDirector:
        handlers: List[Any] = [urllib.request.HTTPRedirectHandler()]
        if not verify_tls:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            logger.warning("TLS verification DISABLED (hanya untuk test/lokal)")
            handlers.append(urllib.request.HTTPSHandler(context=context))
        return urllib.request.build_opener(*handlers)

    def headers(self) -> Dict[str, str]:
        return {
            "X-Reader-Api-Key": self.config.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

    # ------------------------------------------------------------------ #
    def backoff_delay(self, attempt: int) -> float:
        """Exponential backoff + jitter, dibatasi ``backoff_max``."""
        attempt = max(1, attempt)
        delay = min(self.backoff_base * (2 ** (attempt - 1)), self.backoff_max)
        return round(delay * (0.5 + random.random() * 0.5), 3)

    # ------------------------------------------------------------------ #
    def post_batch(self, events: Sequence[Dict[str, Any]]) -> HttpResult:
        """POST satu batch dengan retry. Tidak melempar exception biasa;
        401 dikembalikan sebagai ``fatal=True`` (dan caller wajib stop)."""
        payload = build_payload(events)
        if not payload["events"]:
            return HttpResult(ok=True, status=None, accepted=0, message="batch kosong, dilewati",
                              attempts=0)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = self.headers()
        total_attempts = self.max_retries + 1
        last: Optional[HttpResult] = None
        started = time.monotonic()

        for attempt in range(1, total_attempts + 1):
            result = self._post_once(body, headers, attempt)
            result.attempts = attempt
            last = result
            if result.ok or result.fatal or result.rejected or not result.retryable:
                break
            if attempt < total_attempts:
                delay = self.backoff_delay(attempt)
                logger.warning("http: percobaan %d/%d gagal (%s), retry dalam %.1fs",
                               attempt, total_attempts, result.as_log(), delay)
                self._sleep(delay)
            else:
                logger.error("http: semua percobaan gagal setelah %d kali: %s", attempt, result.as_log())

        assert last is not None
        last.duration_s = round(time.monotonic() - started, 3)
        return last

    # ------------------------------------------------------------------ #
    def _post_once(self, body: bytes, headers: Dict[str, str], attempt: int) -> HttpResult:
        request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = int(getattr(response, "status", 200) or 200)
                raw = response.read().decode("utf-8", errors="replace")
                data = _safe_json(raw)
                accepted = data.get("accepted")
                message = str(data.get("message", "") or "")
                if status == 200:
                    logger.info("http: batch terkirim ok, accepted=%s msg=%r",
                                accepted if accepted is not None else "?", message)
                    return HttpResult(ok=True, status=status,
                                      accepted=int(accepted) if isinstance(accepted, int) else 0,
                                      message=message)
                # 2xx lain yang tidak diharapkan -> anggap sukses agar tidak loop
                logger.warning("http: status %d (tak terduga) diperlakukan sukses: %r", status, message)
                return HttpResult(ok=True, status=status, accepted=0, message=message)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            try:
                raw = exc.read().decode("utf-8", errors="replace")
            except Exception:  # pragma: no cover
                raw = ""
            data = _safe_json(raw)
            message = str(data.get("message", "") or raw[:300] or exc.reason or "")
            if status == STATUS_AUTH_FAILED:
                logger.error("http: 401 AUTH GAGAL — API key ditolak server. "
                             "Perbaiki api_key di config lalu restart agent. msg=%r", message)
                return HttpResult(ok=False, status=status, message=message, fatal=True)
            if status == STATUS_REJECTED:
                logger.warning("http: 422 batch DITOLAK server: %r", message)
                return HttpResult(ok=False, status=status, message=message, rejected=True)
            retryable = ((status not in _NON_RETRYABLE and status >= 500) or status == 429)
            logger.error("http: HTTP %d percobaan %d: %r", status, attempt, message)
            return HttpResult(ok=False, status=status, message=message, retryable=retryable)
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            logger.error("http: koneksi gagal (percobaan %d): %s", attempt, reason)
            return HttpResult(ok=False, error=f"{type(exc).__name__}: {reason}", retryable=True)
        except (TimeoutError, OSError) as exc:
            logger.error("http: timeout/network error (percobaan %d): %s", attempt, exc)
            return HttpResult(ok=False, error=f"{type(exc).__name__}: {exc}", retryable=True)

    # ------------------------------------------------------------------ #
    def ping(self) -> HttpResult:
        """Cek konektivitas tanpa mengirim event (GET/HEAD, 405 = server hidup)."""
        request = urllib.request.Request(self.url, headers={"X-Reader-Api-Key": self.config.api_key},
                                         method="GET")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return HttpResult(ok=True, status=int(getattr(response, "status", 200) or 200),
                                  message="server terjangkau")
        except urllib.error.HTTPError as exc:
            # 405/401 = server ada, endpoint benar
            return HttpResult(ok=exc.code != 401, status=int(exc.code), message=str(exc.reason),
                              fatal=exc.code == 401)
        except Exception as exc:  # noqa: BLE001
            return HttpResult(ok=False, error=f"{type(exc).__name__}: {exc}")


def _safe_json(raw: str) -> Dict[str, Any]:
    try:
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
