"""Test suite bridge agent RFID (stdlib unittest — tanpa perlu install apa pun).

Jalankan dari root repo:
    python -m unittest discover -s tests -v

Vektor test ``normalize`` diambil LANGSUNG dari CONTRACT.md §2 supaya
server (PHP) dan bridge (Python) diuji dengan tabel kasus yang sama.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Pastikan paket utama bisa diimpor walau test dijalankan dari mana saja.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
