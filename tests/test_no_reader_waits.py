"""
Regression: reader belum dicolok harus TIDAK mematikan bridge.

Sebelum fix: DeviceError -> exit 3 -> tray restart tiap 10 detik selamanya
              (user melihat "Restart : 10 kali" lalu balapan).
Sesudah fix: DeviceError -> log sekali, tunggu, coba lagi.
"""
import logging
import sys
import time

sys.path.insert(0, "/home/hermes/workspace/cykeo-bridge")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

from cykeo_bridge.config import MODE_CYKEO, Config          # noqa: E402
from cykeo_bridge.devices.base import DeviceError          # noqa: E402
from cykeo_bridge.main import BridgeAgent                  # noqa: E402

LOGS = []


class FakeLog:
    """Tangkap logger.warning/error supaya bisa diinspeksi."""


class NoReaderDevice:
    """Reader belum dicolok: selalu DeviceError (COM port tidak ada)."""

    def __init__(self, fail_for=3):
        self.calls = 0
        self.fail_for = fail_for

    def describe(self):
        return {"kind": "no-reader"}

    def read_tags(self):
        self.calls += 1
        if self.calls > self.fail_for:
            raise KeyboardInterrupt("cukup")
        raise DeviceError("COM port tidak ada")

    def flush_buffer(self):
        return []

    def close(self):
        pass


def test_no_reader_not_fatal():
    cfg = Config.from_dict({
        "mode": MODE_CYKEO,
        "server_url": "http://127.0.0.1:9",
        "api_key": "x",
    })
    assert cfg.validate(strict=False) == [], "config harus valid"

    agent = BridgeAgent(cfg)
    agent.device = NoReaderDevice(fail_for=3)

    t0 = time.time()
    try:
        code = agent.run()
    except KeyboardInterrupt:
        code = None
    elapsed = time.time() - t0

    print(f"device_calls   = {agent.device.calls}")
    print(f"elapsed        = {elapsed:.1f}s")
    print(f"code           = {code}")

    assert agent.device.calls >= 4, \
        f"device harus dicoba berulang, baru {agent.device.calls} kali"
    assert elapsed >= 3, \
        "loop harus bertahan antar-percobaan (ada interval), bukan spin cepat"
    assert code is None or code == 0, \
        "tidak boleh keluar fatal (exit 3) saat reader belum ada"
    print("PASS: reader belum ada -> loop bertahan, retry, tidak fatal\n")


def test_healthy_reader_still_sends():
    """Tidak boleh ada regresi: device sehat tetap jalan normal."""
    cfg = Config.from_dict({
        "mode": MODE_CYKEO,
        "server_url": "http://127.0.0.1:9",
        "api_key": "x",
    })
    agent = BridgeAgent(cfg)

    class GoodDevice:
        def __init__(self):
            self.calls = 0

        def describe(self):
            return {"kind": "good"}

        def read_tags(self):
            self.calls += 1
            if self.calls > 2:
                raise KeyboardInterrupt("cukup")
            return []

        def flush_buffer(self):
            return []

        def close(self):
            pass

    agent.device = GoodDevice()
    try:
        agent.run()
    except KeyboardInterrupt:
        pass
    assert agent.device.calls >= 3, "device sehat harus tetap dipanggil"
    print(f"PASS: device sehat masih jalan ({agent.device.calls} kali)\n")


if __name__ == "__main__":
    test_no_reader_not_fatal()
    test_healthy_reader_still_sends()
    print("ALL REGRESSION CHECKS PASSED")
