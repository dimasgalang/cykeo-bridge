"""
Uji pembaca lokal tidak boleh pernah menyentuh jaringan.

Kalau modul ini sampai mengimpor http_client atau membuka socket, berarti
"uji lokal" tidak lagi lokal dan teknisi akan tertipu: dia melihat tag muncul
lokal padahal itu balasan server, bukan pembacaan hardware.
"""

import ast
import inspect
import socket
from pathlib import Path

import pytest

from cykeo_bridge import readertest


def test_modul_tidak_import_http_client():
    """Tidak boleh ada import yang bisa membuka koneksi HTTP."""
    sumber = Path(inspect.getfile(readertest)).read_text(encoding="utf-8")
    pohon = ast.parse(sumber)

    nama_berbahaya = {"http_client", "urllib", "requests", "http", "socket", "ssl"}
    ditemukan = []
    for simpul in ast.walk(pohon):
        if isinstance(simpul, ast.Import):
            for alias in simpul.names:
                if alias.name.split(".")[0] in nama_berbahaya:
                    ditemukan.append(alias.name)
        elif isinstance(simpul, ast.ImportFrom) and simpul.module:
            if simpul.module.split(".")[0] in nama_berbahaya:
                ditemukan.append(simpul.module)

    assert ditemukan == [], (
        f"readertest.py mengimpor {ditemukan} — uji lokal HARUS tanpa jaringan"
    )


def test_socket_tidak_pernah_dibuka_saat_uji_simulator(monkeypatch):
    """Jalankan uji end-to-end sementara socket dimatikan total."""
    dibuat = []

    class _SocketDilarang(socket.socket):  # type: ignore[misc,valid-type]
        def __init__(self, *a, **kw):
            dibuat.append("socket dibuat")
            raise AssertionError("uji lokal tidak boleh membuat socket")

    monkeypatch.setattr(socket, "socket", _SocketDilarang)
    monkeypatch.setattr(socket, "create_connection",
                        lambda *a, **kw: pytest.fail("uji lokal membuat koneksi"))

    from cykeo_bridge.config import Config

    hasil = readertest.jalankan_uji(
        Config(mode="simulator", com_port="", api_key="x" * 32,
               server_url="http://127.0.0.1:1"),
        durasi=0.4,
    )

    assert dibuat == []
    # Simulator MEMANG menghasilkan tag, jadi ini harus sukses.
    assert hasil.ok, f"uji simulator harus bisa membaca tag: {hasil.as_lines()}"
    assert hasil.epc_unik > 0
