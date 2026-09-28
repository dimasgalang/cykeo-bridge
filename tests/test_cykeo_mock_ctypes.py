"""Uji adapter Cykeo terhadap MOCK native GReader.so (bukan hardware).

Tujuan: membuktikan bahwa ``serial_cykeo`` benar-benar mengikat simbol native
dengan arity yang benar, mendaftarkan callback, memicu inventory READ-ONLY, dan
mengekstrak EPC dari frame `LogBaseEpcInfo` — termasuk saat layout struct aslinya
berbeda dari tebakan (parser defensif).

Mock sengaja memakai layout struct yang SALIH supaya test ini tidak become
tautology: kalau adapter bergantung pada offset tebakan, test akan gagal.

Test ini di-skip otomatis kalau gcc tidak tersedia (lalu build mock-nya).
"""

from __future__ import annotations

import ctypes
import importlib
import os
import platform
import shutil
import subprocess
import unittest
from pathlib import Path

from cykeo_bridge.devices.base import DeviceError

HERE = Path(__file__).resolve().parent
MOCK_SRC = HERE / "mock_greader.c"
MOCK_LIB = HERE / "lib" / "libGReader.so"

MODULE = "cykeo_bridge.devices.serial_cykeo"


def _build_mock() -> bool:
    """Build libGReader.so dari mock_greader.c. True kalau berhasil."""
    if platform.system() == "Windows":
        return False
    if not shutil.which("gcc"):
        return False
    MOCK_LIB.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["gcc", "-shared", "-fPIC", "-O1", "-o", str(MOCK_LIB), str(MOCK_SRC)],
            check=True, capture_output=True, timeout=120,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False
    return MOCK_LIB.exists()


_BUILD_OK = _build_mock()
_UNAVAILABLE_REASON = "butuh gcc + platform POSIX untuk membangun mock GReader.so"


@unittest.skipUnless(_BUILD_OK, _UNAVAILABLE_REASON)
class TestCykeoAdapterAgainstMock(unittest.TestCase):
    """Adapter ctypes diuji end-to-end terhadap mock native library."""

    def setUp(self) -> None:
        self.module = importlib.import_module(MODULE)
        # Paksa muat mock: stub WinDLL/platform supaya bisa jalan di Linux.
        # WAJIB pakai addCleanup — kalau tidak, monkeypatch ini bocor ke test
        # lain di suite dan membuat test non-mock ikut gagal.
        self._orig_win = getattr(self.module.ctypes, "WinDLL", None)
        self._orig_sys = self.module.platform.system
        self._orig_sdk = self.module.sdk_available
        self.addCleanup(self._restore)
        self.module.platform.system = lambda: "Windows"
        self.module.ctypes.WinDLL = ctypes.CDLL
        # sdk_available normally rejects non-Windows; the mock IS available.
        self.module.sdk_available = lambda dll_path=None: (True, "mock: test")
        self.device = self.module.CykeoSerialDevice(
            com_port="COM12", init_param=self.module.INIT_PARAM_DEFAULT,
            dll_path=str(MOCK_LIB),
        )
        # Mock GReader menyimpan client di global; reset per-test.
        self.lib = self.device._dll
        self.lib.mock_reset.restype = None
        self.lib.mock_reset.argtypes = []
        self.lib.mock_reset()
        self.lib.mock_emit_tag.restype = ctypes.c_int
        self.lib.mock_emit_tag.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
        self.lib.mock_emit_over.restype = ctypes.c_int
        self.lib.mock_emit_over.argtypes = []
        self.lib.mock_cb_log.restype = ctypes.c_int
        self.lib.mock_cb_over.restype = ctypes.c_int
        self.lib.mock_stop_count.restype = ctypes.c_int
        self.lib.mock_inventory_count.restype = ctypes.c_int
        self.lib.mock_was_closed.restype = ctypes.c_int

    def _restore(self) -> None:
        self.module.platform.system = self._orig_sys
        self.module.sdk_available = self._orig_sdk
        if self._orig_win is not None:
            self.module.ctypes.WinDLL = self._orig_win
        else:
            del self.module.ctypes.WinDLL
        try:
            self.device.close()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    def test_dll_loaded_and_symbols_bound(self) -> None:
        self.assertIsNotNone(self.device._dll)
        for sym in ("CreateRS232", "Close", "RegCallBack", "SendSynMsg"):
            self.assertTrue(hasattr(self.device._dll, sym), f"simbol {sym} tidak terikat")

    def test_connection_string_uses_official_format(self) -> None:
        self.assertEqual(self.device.connection_string, "COM12:115200")

    def test_connect_then_registers_both_callbacks(self) -> None:
        self.device.connect()
        self.assertIsNotNone(self.device._client)
        self.assertEqual(self.lib.mock_cb_log(), 1, "callback Epc belum terdaftar")
        self.assertEqual(self.lib.mock_cb_over(), 1, "callback EpcOver belum terdaftar")

    def test_start_inventory_sends_stop_then_inventory_readonly(self) -> None:
        self.device.connect()
        self.assertEqual(self.lib.mock_inventory_count(), 0, "belum ada inventory")
        self.device.start_inventory()
        # Quick Start: STOP dulu, lalu INVENTORY EPC.
        self.assertGreaterEqual(self.lib.mock_stop_count(), 1)
        self.assertEqual(self.lib.mock_inventory_count(), 1)
        self.assertTrue(self.device._inventory_sent)

    def test_emitted_tag_is_parsed_from_opaque_frame(self) -> None:
        self.device.connect()
        self.assertEqual(self.lib.mock_emit_tag(b"E20034120123456789012345AB", 1, -52), 1)
        tags = self.device.read_tags()
        self.assertEqual(len(tags), 1, "tag dari mock tidak terbaca")
        tag = tags[0]
        self.assertEqual(tag.epc, "E20034120123456789012345AB")
        self.assertEqual(tag.reader_name, "COM12:115200")
        self.assertIsNotNone(tag.timestamp)

    def test_multiple_tags_accumulate(self) -> None:
        self.device.connect()
        for epc in (b"E20034120123456789012345AB", b"0A0B0C0D0E0F1011121314"):
            self.lib.mock_emit_tag(epc, 1, -50)
        tags = self.device.read_tags()
        self.assertEqual(len(tags), 2)
        self.assertEqual([t.epc for t in tags],
                         ["E20034120123456789012345AB", "0A0B0C0D0E0F1011121314"])
        # Payload dikirim APA ADANYA dari device — agent tidak menormalisasi
        # (normalisasi authoritative ada di server, kontrak §2).
        self.assertEqual(tags[1].to_event()["epc"], "0A0B0C0D0E0F1011121314")

    def test_prefixed_epc_is_not_double_normalized(self) -> None:
        """Regression: parser defensif boleh buang '0X' (bukan hex) dari frame.

        Guide menyatakan device mengirim EPC sebagai 'hexadecimal character
        string' (tanpa prefix), jadi kasus ini tidak seharusnya terjadi di
        hardware. Bila terjadi, parser memberi hex polos — hasil yang sama
        dengan normalisasi server, jadi tag tetap collapse ke satu entri.
        """
        self.device.connect()
        self.lib.mock_emit_tag(b"0X0A0B0C0D0E0F1011121314", 1, -50)
        tags = self.device.read_tags()
        self.assertEqual(len(tags), 1)
        self.assertEqual(tags[0].epc, "0A0B0C0D0E0F1011121314")

    def test_tag_over_callback_is_harmless(self) -> None:
        self.device.connect()
        self.assertEqual(self.lib.mock_emit_over(), 1)
        self.assertEqual(self.device.read_tags(), [])

    def test_read_tags_drains_buffer_once(self) -> None:
        self.device.connect()
        self.lib.mock_emit_tag(b"E20034120123456789012345AB", 1, -50)
        self.assertEqual(len(self.device.read_tags()), 1)
        self.assertEqual(self.device.read_tags(), [], "buffer harus kosong setelah di-drain")

    def test_flush_buffer_drains_without_reconnect(self) -> None:
        self.device.connect()
        self.lib.mock_emit_tag(b"E20034120123456789012345AB", 1, -50)
        self.assertEqual(len(self.device.flush_buffer()), 1)
        self.assertEqual(self.device.flush_buffer(), [])

    def test_close_sends_stop_and_closes_client(self) -> None:
        self.device.connect()
        self.device.start_inventory()
        self.device.close()
        self.assertEqual(self.lib.mock_was_closed(), 1)
        self.assertIsNone(self.device._client)

    def test_describe_exposes_non_secret_state(self) -> None:
        self.device.connect()
        desc = self.device.describe()
        self.assertEqual(desc["port"], "COM12:115200")
        self.assertTrue(desc["connected"])
        self.assertNotIn(self.module.INIT_PARAM_DEFAULT, repr(desc))

    def test_read_tags_after_close_raises(self) -> None:
        self.device.connect()
        self.device.close()
        with self.assertRaises(DeviceError):
            self.device.read_tags()


if __name__ == "__main__":
    unittest.main()
