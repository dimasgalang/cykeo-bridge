"""Regresi: helper .NET wajib dik-connect sebelum InventoryEpc.

Bug nyata di PC test (192.168.3.61, COM3): ``start()`` hanya membaca
handshake ``hello``, tapi tidak pernah mengirim perintah ``connect``.
Akibatnya InventoryEpc dibalas helper dengan
"helper belum terhubung; jalankan connect dulu" dan tidak ada tag terbaca.
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
from types import SimpleNamespace

sys.path.insert(0, "/home/hermes/workspace/cykeo-bridge")

from cykeo_bridge.devices import cykeo_helper as H  # noqa: E402


class FakeProc:
    """Proses helper palsu: stdout memberi handshake lalu diam."""

    def __init__(self):
        self.stdin = None
        self.stdout = self
        self.stderr = None
        self.terminated = False
        self.killed = False
        self.sent = []
        self._lock = threading.Lock()
        self.hello = json.dumps({"type": "hello", "sdkDir": "C:/sdk"}) + "\n"
        self._lines = [self.hello]
        self._done = threading.Event()
        self._done.set()

    def poll(self):
        return None if not self._done.is_set() else 0

    def readline(self):
        if self._lines:
            return self._lines.pop(0)
        return ""

    def __iter__(self):
        return iter(())

    def write(self, data):
        with self._lock:
            self.sent.append(data)
        return len(data)

    def flush(self):
        return None

    def close(self):
        return None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def make_device():
    dev = H.CykeoHelperDevice.__new__(H.CykeoHelperDevice)
    dev.com_port = "COM3"
    dev.baudrate = 115200
    dev.helper_path = None
    dev.sdk_dir = "C:/sdk"
    dev.connect_timeout = 60
    dev.name = "cykeo"
    dev._lock = threading.Lock()
    dev._pending_lock = threading.Lock()
    dev._pending = {}
    dev._next_id = 0
    dev._buffer = []
    dev._stop = threading.Event()
    dev._proc = None
    dev._reader = None
    dev._fail_reason = None
    return dev


class ConnectOnStartTest(unittest.TestCase):
    """start() harus mengirim cmd=connect dengan com_port dan baud."""

    def setUp(self):
        # Helper hanya boleh jalan di Windows; test ini memalsukan platform
        # supaya logika connect yang diuji bisa berjalan di Linux.
        self._real_system = H.platform.system
        H.platform.system = lambda: "Windows"
        self.addCleanup(lambda: setattr(H.platform, "system", self._real_system))
        self._real_locate = H._locate_helper
        H._locate_helper = lambda p: "C:/app/helper/cykeo-helper.exe"
        self.addCleanup(lambda: setattr(H, "_locate_helper", self._real_locate))
        self._real_folder_ok = H._helper_folder_ok
        H._helper_folder_ok = lambda exe: None
        self.addCleanup(lambda: setattr(H, "_helper_folder_ok", self._real_folder_ok))
        self._real_popen = H.subprocess.Popen
        H.subprocess.Popen = lambda *a, **k: FakeProc()
        self.addCleanup(lambda: setattr(H.subprocess, "Popen", self._real_popen))

    def _start_with_capture(self):
        dev = make_device()
        proc = FakeProc()
        dev._proc = proc
        dev._reader = threading.Thread(target=lambda: None, daemon=True)

        calls = []

        def fake_request(payload, timeout=10.0):
            calls.append(dict(payload))
            return {"ok": True}

        dev._request = fake_request
        H.CykeoHelperDevice._await_hello = lambda self: {"sdkDir": "C:/sdk"}
        dev.start()
        return dev, calls

    def test_01_start_mengirim_connect(self):
        _dev, calls = self._start_with_capture()
        cmds = [c.get("cmd") for c in calls]
        self.assertIn("connect", cmds,
                      "start() harus mengirim cmd=connect ke helper")

    def test_02_connect_bawa_com_port(self):
        _dev, calls = self._start_with_capture()
        conn = [c for c in calls if c.get("cmd") == "connect"][0]
        self.assertEqual(conn["com_port"], "COM3")
        self.assertEqual(conn["baud"], 115200)
        self.assertEqual(conn["timeout"], 60)

    def test_03_connect_pertama_sebelum_start(self):
        """Urutan penting: connect harus sebelum InventoryEpc."""
        dev = make_device()
        proc = FakeProc()
        dev._proc = proc
        dev._reader = threading.Thread(target=lambda: None, daemon=True)
        order = []
        dev._request = lambda payload, timeout=10.0: (
            order.append(payload.get("cmd")) or {"ok": True})
        H.CykeoHelperDevice._await_hello = lambda self: {"sdkDir": "C:/sdk"}
        dev.start()
        dev.begin_inventory()
        self.assertEqual(order, ["connect", "start"],
                         "urutan harus connect lalu start (InventoryEpc)")

    def test_04_gagal_connect_melempar(self):
        dev = make_device()
        dev._proc = FakeProc()
        dev._reader = threading.Thread(target=lambda: None, daemon=True)

        def boom(payload, timeout=10.0):
            if payload.get("cmd") == "connect":
                raise H.DeviceError("COM3 tidak ditemukan")
            return {"ok": True}

        dev._request = boom
        H.CykeoHelperDevice._await_hello = lambda self: {"sdkDir": "C:/sdk"}
        with self.assertRaises(H.DeviceError) as ctx:
            dev.start()
        self.assertIn("COM3 tidak ditemukan", str(ctx.exception))

    def test_05_baudrate_kustom_dikirim(self):
        dev = make_device()
        dev.baudrate = 57600
        dev._proc = FakeProc()
        dev._reader = threading.Thread(target=lambda: None, daemon=True)
        calls = []
        dev._request = lambda payload, timeout=10.0: (
            calls.append(dict(payload)) or {"ok": True})
        H.CykeoHelperDevice._await_hello = lambda self: {"sdkDir": "C:/sdk"}
        dev.start()
        conn = [c for c in calls if c.get("cmd") == "connect"][0]
        self.assertEqual(conn["baud"], 57600)

    def test_06_stop_menghentikan_inventory(self):
        """end_inventory tetap mengirim stop dan menelan error."""
        dev = make_device()
        calls = []
        dev._request = lambda payload, timeout=10.0: (
            calls.append(dict(payload)) or {"ok": True})
        dev.end_inventory()
        self.assertEqual(calls[0]["cmd"], "stop")

    def test_07_stop_tidak_melempar_saat_helper_mati(self):
        dev = make_device()

        def boom(payload, timeout=10.0):
            raise H.DeviceError("helper sudah tutup")

        dev._request = boom
        dev.end_inventory()   # tidak boleh melempar


if __name__ == "__main__":
    unittest.main(verbosity=2)
