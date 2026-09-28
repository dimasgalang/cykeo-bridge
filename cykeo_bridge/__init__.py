"""Bridge agent RFID — reader Cykeo CK D5 / Zebra FX7500 -> server RFID.

Bridge bersifat READ-ONLY terhadap device (hanya membaca output reader lewat
RS232) dan hanya melakukan koneksi KELUAR (HTTP POST) ke server. Bridge tidak
membuka port/listener apa pun (CONTRACT.md §1.2).
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]
