"""Pabrik adapter device — memilih implementasi sesuai config ``mode``.

``zebra``     : reader FX7500 tidak boleh dikonfigurasi/diubah (kontrak §1.1).
                Bridge dalam mode ini TIDAK membuka koneksi device apa pun; ia
                hanya diam (pasif) karena Zebra sudah mengirim datanya sendiri ke
                server. Mode ini dipakai kalau baris reader di server ditandai
                ``driver=zebra`` tapi agent tetap dijalankan untuk monitoring.
``cykeo``     : baca lewat helper .NET ``cykeo-helper.exe`` yang memanggil
                ``GReaderApi.dll`` (Windows). Ini jalur production: helper
                meniru alur ``FlexiAudit.Infrastructure.RfidReader.CykeoReader``
                (OpenSerial -> StartInventory -> OnEncapedTagEpcLog).
``simulator`` : baca dari fixture, tanpa hardware.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import (
    MODE_CYKEO,
    MODE_SIMULATOR,
    MODE_ZEBRA,
    Config,
)
from .base import DeviceAdapter, DeviceError
from .simulator import SimulatedDevice

__all__ = [
    "build_device",
    "NullDevice",
    "helper_status",
    "resolve_helper_path",
    "HELPER_REQUIRED_FILES",
]

logger = logging.getLogger("cykeo_bridge.device")


class NullDevice(DeviceAdapter):
    """Adapter pasif: tidak pernah membaca apa pun (mode zebra)."""

    name = "zebra-null"

    def read_tags(self):  # noqa: ANN201 - konsisten dengan base
        return []

    def describe(self) -> Dict[str, Any]:
        return {"adapter": self.name,
                "note": "FX7500 tidak dikonfigurasi/diubah; agent tidak baca device"}


def sdk_status(helper_path: Optional[str] = None) -> Dict[str, Any]:
    """Status helper .NET Cykeo untuk ditampilkan wizard/diagnostik."""
    from . import cykeo_helper  # import lokal: aman di semua platform

    available = cykeo_helper.sdk_available(helper_path)
    reason = "" if available else cykeo_helper.unavailable_reason()
    return {"available": available, "reason": reason,
            "candidates": cykeo_helper.default_helper_candidates()[:5]}


def build_device(config: Config, *, fixture: Optional[str] = None) -> DeviceAdapter:
    """Buat adapter sesuai ``config.mode``.

    Raises:
        DeviceError: kalau mode cykeo tapi SDK/DLL tidak bisa dipakai, dengan
            pesan yang menyarankan mode simulasi.
    """
    mode = (config.mode or MODE_CYKEO).lower()

    if mode == MODE_SIMULATOR:
        return SimulatedDevice(
            fixture=fixture or (config.simulator_fixture or None),
            loop=bool(config.simulator_loop),
            delay=float(config.simulator_delay),
        )

    if mode == MODE_ZEBRA:
        logger.info("mode zebra: bridge pasif, tidak menyentuh konfigurasi FX7500")
        return NullDevice()

    if mode == MODE_CYKEO:
        # Jalur production: helper .NET -> GReaderApi.dll (bukan ctypes).
        from .cykeo_helper import CykeoHelperDevice  # import lokal (Windows-only)

        try:
            device = CykeoHelperDevice(
                com_port=config.com_port,
                baudrate=config.baudrate,
                helper_path=(config.helper_path or None),
                sdk_dir=(config.sdk_dir or None),
                connect_timeout=int(config.connect_timeout),
                name=config.reader_code or "cykeo",
            )
            device.start()
            return device
        except DeviceError as exc:
            raise DeviceError(
                f"{exc}\n"
                "Saran: jalankan dengan mode=simulator (python -m cykeo_bridge run "
                "--simulate) untuk menguji tanpa device, atau jalankan "
                "install_cykeo.ps1 agar helper .NET terpasang."
            ) from exc

    raise DeviceError(f"Mode tidak dikenal: {config.mode!r} (pilihan: "
                      f"{MODE_ZEBRA}, {MODE_CYKEO}, {MODE_SIMULATOR})")


# ------------------------------------------------------------------------- #
# Status helper (dipakai wizard + perintah ``check``)
# ------------------------------------------------------------------------- #
#: File yang wajib ada agar helper .NET bisa jalan.
HELPER_REQUIRED_FILES = (
    "cykeo-helper.exe",
    "GReaderApi.dll",
    "Newtonsoft.Json.dll",
)


def resolve_helper_path(helper_path: Optional[str] = None) -> Optional[Path]:
    r"""Tentukan lokasi ``cykeo-helper.exe``.

    Delegasi ke ``cykeo_helper.default_helper_candidates()`` supaya hanya ada
    SATU sumber kebenaran soal lokasi helper. Urutan:

    1. override eksplisit dari config
    2. env ``CYKEO_HELPER_PATH``
    3. ``%LOCALAPPDATA%\CykeoRfidBridge`` / ``%APPDATA%\CykeoBridge``
    4. folder build di repo (dev) dan folder ``dist`` (installer)
    5. PATH
    """
    from .cykeo_helper import _locate_helper

    found = _locate_helper(helper_path)
    return Path(found) if found else None


def helper_status(*, helper_path: Optional[str] = None,
                  sdk_dir: Optional[str] = None) -> Dict[str, Any]:
    """Cek apakah helper .NET + dependency vendor sudah siap.

    Dipakai ``wizard`` dan ``cykeo_bridge check`` supaya user dapat pesan
   pesan yang jelas, bukan error generic dari Windows loader.
    """
    exe = resolve_helper_path(helper_path)
    base = exe.parent if exe else None

    missing: List[str] = []
    if exe is None:
        missing.append(HELPER_REQUIRED_FILES[0])
    else:
        for fname in HELPER_REQUIRED_FILES:
            if not (base / fname).is_file():
                missing.append(fname)

    sdk_resolved: Optional[Path] = None
    if sdk_dir:
        sdk_resolved = Path(sdk_dir)
    elif base is not None:
        # SDK biasanya ikut di folder yang sama dengan helper.
        sdk_resolved = base

    if sdk_resolved is not None and not (
        sdk_resolved / "GReaderApi.dll"
    ).is_file():
        missing.append("GReaderApi.dll (SDK vendor)")

    is_windows = sys.platform.startswith("win")
    if not is_windows and not missing:
        missing.append("helper hanya jalan di Windows")

    available = not missing

    if available:
        reason = "helper .NET + SDK vendor siap"
    elif exe is None:
        reason = "cykeo-helper.exe tidak ditemukan (jalankan install_cykeo.ps1)"
    elif not is_windows and missing == ["helper hanya jalan di Windows"]:
        reason = "helper siap, tapi OS ini bukan Windows"
    else:
        reason = "belum lengkap: " + ", ".join(missing)

    return {
        "available": available,
        "helper": str(exe) if exe else None,
        "sdk_dir": str(sdk_resolved) if sdk_resolved else None,
        "missing": missing,
        "reason": reason,
    }
