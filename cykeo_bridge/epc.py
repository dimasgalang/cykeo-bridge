"""Normalisasi EPC — WAJIB identik dengan CONTRACT.md §2.

Bridge (Python) dan server (PHP ``EpcNormalizer``) harus menghasilkan nilai yang
sama byte-per-byte. Jangan ubah urutan langkah di bawah tanpa persetujuan
eksplisit di CONTRACT.md.

Aturan (kontrak §2):
    1. trim
    2. kosong -> None (tolak)
    3. buang SEMUA whitespace di dalamnya (spasi, tab, \\r, \\n, NBSP, ...)
    4. awalan "0X"/"0x" (case-insensitive, hanya 2 char pertama) -> buang 2 char
    5. uppercase
    6. hasil bukan hex (^[0-9A-F]+$) -> None (tolak)
    7. kembalikan hasil
"""

from __future__ import annotations

import re
from typing import Optional, Union

__all__ = [
    "normalize",
    "is_hex_epc",
    "looks_like_hex",
    "strip_prefix",
    "RawEpc",
]

#: Tipe yang diterima sebagai EPC mentah (reader bisa kirim bytes via ctypes).
RawEpc = Union[str, bytes, bytearray, None]

#: Karakter yang dipangkas di tepi (langkah 1). Python ``str.strip()`` sudah
#: menangani Unicode whitespace termasuk NBSP, tapi BOM/non-breaking space
#: eksplisit supaya perilakunya tidak bergantung versi interpreter.
_TRIM_CHARS = " \t\r\n\v\f   ﻿"

#: Semua karakter whitespace di tengah string (langkah 3). ``\s`` di Python
#: sudah mencakup NBSP dan Unicode whitespace; ``\u200b``/``\u200c``/``\u200d``/
#: ``\ufeff`` ditambah eksplisit karena tidak dianggap whitespace oleh regex
#: Python tetapi kadang muncul di output reader.
_WHITESPACE_RE = re.compile("[\\s\u200b\u200c\u200d\ufeff]+")

#: Prefix hex yang dibuang pada langkah 4 — hanya 2 karakter pertama.
_HEX_PREFIX_RE = re.compile(r"^0[xX]")

#: Validasi akhir (langkah 6): hanya digit + A-F, minimal 1 karakter.
_HEX_RE = re.compile(r"^[0-9A-F]+$")


def strip_prefix(value: str) -> str:
    """Buang awalan ``0X``/``0x`` saja (langkah 4), case-insensitive."""
    return _HEX_PREFIX_RE.sub("", value, count=1)


def looks_like_hex(value: str) -> bool:
    """True bila ``value`` (sudah uppercase) cocok ``^[0-9A-F]+$``."""
    return bool(_HEX_RE.fullmatch(value))


def normalize(raw: RawEpc) -> Optional[str]:
    """Normalisasi EPC mentah dari device.

    Args:
        raw: nilai EPC apa adanya seperti keluar dari reader. ``None`` atau
            string kosong berarti reader belum mengembalikan tag yang valid.

    Returns:
        EPC kanonik (uppercase, tanpa prefix, tanpa whitespace) atau ``None``
        kalau input kosong / bukan hex (harus ditolak).
    """
    # (1) trim tepi
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = bytes(raw).decode("ascii")
        except UnicodeDecodeError:
            return None  # bukan ASCII hex -> tolak
    if not isinstance(raw, str):
        return None
    value = raw.strip(_TRIM_CHARS)

    # (2) kosong -> tolak
    if not value:
        return None

    # (3) buang SEMUA whitespace di dalam
    value = _WHITESPACE_RE.sub("", value)

    # (2b) bisa jadi habis whitespace semua (mis. "   " atau "\t\n")
    if not value:
        return None

    # (4) buang prefix 0X / 0x (hanya 2 char pertama)
    value = strip_prefix(value)

    # (2c) input hanya "0x" -> jadi kosong -> tolak
    if not value:
        return None

    # (5) uppercase
    value = value.upper()

    # (6) validasi hex
    if not looks_like_hex(value):
        return None

    # (7) kembalikan hasil
    return value


def is_hex_epc(raw: RawEpc) -> bool:
    """Normalisasi lalu cek apakah hasilnya hex valid (bool, bukan None)."""
    return normalize(raw) is not None
