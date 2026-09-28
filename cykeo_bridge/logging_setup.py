"""Logging rotating + penyaring rahasia.

Aturan keras: ``api_key`` dan ``init_param`` TIDAK BOLEH muncul di log.
:class:`SecretFilter` menutupi nilai rahasia kalau muncul tidak sengaja
(mis. dari repr objek atau traceback).
"""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path
from typing import Any, Iterable, Optional

__all__ = ["setup_logging", "SecretFilter", "register_secret", "get_logger", "REDACTED"]

REDACTED = "***REDACTED***"

#: Kunci config yang nilainya tidak boleh bocor.
_SECRET_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|license[_-]?key|x-reader-api-key|password|secret|token)"
    r"(\"?\s*[:=]\s*\"?)([^\s\"',}]+)"
)

#: Nilai rahasia runtime (didaftarkan saat start).
_SECRETS: set[str] = set()

logger = logging.getLogger("cykeo_bridge")


class SecretFilter(logging.Filter):
    """Sensor: ganti nilai rahasia dan pola ``key=value`` jadi ``***REDACTED***``."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D102
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - pesan rusak, biarkan lewat
            return True
        redacted = self.redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True

    @staticmethod
    def redact(text: str) -> str:
        """Tutupi rahasia yang sudah terdaftar + pola key=value."""
        for secret in _SECRETS:
            if secret and len(secret) >= 6 and secret in text:
                text = text.replace(secret, REDACTED)
        text = _SECRET_KEY_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
        return text


def register_secret(*values: Optional[str]) -> None:
    """Daftarkan nilai rahasia (api key, initParam) untuk disensor di log."""
    for value in values:
        if value and isinstance(value, str) and len(value) >= 6:
            _SECRETS.add(value)


def get_logger(name: Optional[str] = None) -> logging.Logger:
    return logging.getLogger(name or "cykeo_bridge")


def setup_logging(log_path: Optional[Any] = None, *, level: int = logging.INFO,
                  console: bool = True, max_bytes: int = 5 * 1024 * 1024,
                  backup_count: int = 5, secrets: Iterable[Optional[str]] = ()) -> logging.Logger:
    """Pasang rotating file handler + console handler.

    Args:
        log_path: file log; ``None`` -> hanya console (dipakai test).
        level: level root.
        console: tampilkan juga ke stdout/stderr.
        secrets: api_key / initParam yang harus disensor.
    """
    register_secret(*secrets)
    root = logger
    root.setLevel(level)
    root.propagate = False
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # pragma: no cover
            pass

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    sensor = SecretFilter()

    if log_path:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        file_handler.addFilter(sensor)
        root.addHandler(file_handler)

    if console or not log_path:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        stream.setLevel(level)
        stream.addFilter(sensor)
        root.addHandler(stream)

    return root
