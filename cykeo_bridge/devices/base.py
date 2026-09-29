"""Antarmuka adapter device.

Bridge hanya MEMBACA output reader (read-only). Adapter tidak boleh menulis
konfigurasi device apa pun (CONTRACT.md §1).
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

__all__ = ["RawTag", "DeviceAdapter", "DeviceError", "utc_now_iso"]


def utc_now_iso() -> str:
    """Timestamp ISO-8601 UTC, mis. ``2026-09-28T04:05:06.123456+00:00``."""
    return datetime.now(timezone.utc).isoformat()


class DeviceError(RuntimeError):
    """Kegagalan saat talking ke device (COM gagal dibuka, DLL hilang, ...)."""


@dataclass
class RawTag:
    """Satu hasil baca tag dari reader.

    ``epc`` disimpan APA ADANYA seperti keluar dari device — normalisasi hanya
    dipakai untuk dedup lokal, payload yang dikirim tetap mentah (kontrak §4).
    """

    epc: str
    antenna: Optional[int] = None
    rssi: Optional[int] = None
    timestamp: Optional[str] = None
    tid: Optional[str] = None
    pc: Optional[str] = None
    reader_name: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.timestamp is None:
            self.timestamp = utc_now_iso()

    def to_event(self) -> Dict[str, Any]:
        """Bentuk event sesuai kontrak §4 (tanpa key tambahan)."""
        return {
            "epc": self.epc,
            "antenna": self.antenna,
            "rssi": self.rssi,
            "timestamp": self.timestamp,
        }

    def log_fields(self) -> str:
        """Ringkasan aman untuk log (tidak pernah berisi EPC penuh kalau dikonfig)."""
        return f"ant={self.antenna} rssi={self.rssi} ts={self.timestamp}"


class DeviceAdapter(abc.ABC):
    """Adapter device: sumber tag untuk agent.

    Implementasi:
      - :class:`cykeo_bridge.devices.simulator.SimulatedDevice` (tanpa hardware)
      - :class:`cykeo_bridge.devices.serial_cykeo.CykeoSerialDevice` (Windows + SDK)
    """

    #: Nama mode untuk logging/diagnostik.
    name: str = "abstract"

    @abc.abstractmethod
    def read_tags(self) -> List[RawTag]:
        """Baca tag yang tersedia saat ini (bisa kosong)."""

    def begin_inventory(self) -> None:
        """Mulai pemindaian kontinu (non-blocking, read-only).

        WAJIB dipanggil setelah ``read_tags()`` jadi reader benar-benar
        memindai. ``read_tags()`` hanya menguras buffer; tanpa perintah
        inventory reader CK-D5 diam dan buffer tidak akan pernah terisi
        (gejala: "reader terbuka tapi 0 tag" selamanya).

        Default: no-op, untuk adapter yang sudah memindai sendiri
        (mis. simulator) atau yang tidak butuh perintah start.
        """
        return None

    def end_inventory(self) -> None:
        """Hentikan pemindaian (non-blocking). Default: no-op."""
        return None

    def close(self) -> None:
        """Tutup resource (COM handle, DLL, file). Default: no-op."""

    def flush_buffer(self) -> List[RawTag]:
        """Tag yang tertahan di buffer internal adapter (opsional)."""
        return []

    def describe(self) -> Dict[str, Any]:
        """Informasi non-rahasia untuk log startup."""
        return {"adapter": self.name}

    def __enter__(self) -> "DeviceAdapter":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()
