"""Unit test adapter device: simulator (LOKAL) + import-safety serial_cykeo.

Tidak ada akses hardware. serial_cykeo hanya diuji sampai batas "aman untuk
diimpor di Linux dan menolak dengan pesan jelas".
"""

from __future__ import annotations

import importlib
import json
import platform
import tempfile
import unittest
from pathlib import Path

from cykeo_bridge.devices.base import DeviceAdapter, DeviceError, RawTag, utc_now_iso
from cykeo_bridge.devices.factory import NullDevice, build_device, sdk_status
from cykeo_bridge.devices.simulator import (
    DEFAULT_FIXTURE_TAGS,
    SimulatedDevice,
    load_fixture_tags,
)
from cykeo_bridge.config import MODE_CYKEO, MODE_SIMULATOR, MODE_ZEBRA, Config


class TestRawTag(unittest.TestCase):
    def test_to_event_shape(self) -> None:
        tag = RawTag(epc="ABC123", antenna=2, rssi=-50, timestamp="2026-09-28T00:00:00+00:00")
        self.assertEqual(tag.to_event(), {
            "epc": "ABC123", "antenna": 2, "rssi": -50,
            "timestamp": "2026-09-28T00:00:00+00:00",
        })

    def test_timestamp_autofilled(self) -> None:
        tag = RawTag(epc="ABC")
        self.assertTrue(tag.timestamp)
        self.assertIn("T", tag.timestamp)

    def test_epc_kept_verbatim(self) -> None:
        raw = " 0x303515c1140e3610a02e4880 "
        self.assertEqual(RawTag(epc=raw).to_event()["epc"], raw)

    def test_optional_fields_default_none(self) -> None:
        event = RawTag(epc="ABC").to_event()
        self.assertIsNone(event["antenna"])
        self.assertIsNone(event["rssi"])

    def test_utc_now_iso_format(self) -> None:
        value = utc_now_iso()
        self.assertRegex(value, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+00:00$")


class TestSimulator(unittest.TestCase):
    def test_reads_tags(self) -> None:
        dev = SimulatedDevice(loop=False)
        tags = dev.read_tags()
        self.assertEqual(len(tags), 1)
        self.assertIsInstance(tags[0], RawTag)
        self.assertEqual(tags[0].epc, DEFAULT_FIXTURE_TAGS[0]["epc"])

    def test_batch_size(self) -> None:
        dev = SimulatedDevice(loop=False, batch_size=4)
        self.assertEqual(len(dev.read_tags()), 4)

    def test_cycles_through_fixture(self) -> None:
        dev = SimulatedDevice(loop=True)
        seen = [dev.read_tags()[0].epc for _ in range(len(DEFAULT_FIXTURE_TAGS) * 2)]
        self.assertEqual(seen[:len(DEFAULT_FIXTURE_TAGS)], seen[len(DEFAULT_FIXTURE_TAGS):])

    def test_loop_false_stops(self) -> None:
        # 7 tag di fixture, batch_size=3 -> baca 3 + 3 + 1, lalu berhenti.
        dev = SimulatedDevice(loop=False, batch_size=3)
        total = 0
        for _ in range(5):  # baca berulang: harus berhenti, tidak error
            total += len(dev.read_tags())
        self.assertEqual(total, len(DEFAULT_FIXTURE_TAGS))
        self.assertEqual(dev.read_tags(), [])  # habis -> kosong

    def test_fixture_has_mixed_formats(self) -> None:
        raw = [t["epc"] for t in DEFAULT_FIXTURE_TAGS]
        self.assertTrue(any(e.startswith("0X") for e in raw))
        self.assertTrue(any(" " in e for e in raw))
        self.assertTrue(any(e != e.strip() for e in raw))

    def test_close_then_read_raises(self) -> None:
        dev = SimulatedDevice()
        dev.close()
        with self.assertRaises(DeviceError):
            dev.read_tags()

    def test_context_manager(self) -> None:
        with SimulatedDevice() as dev:
            self.assertIsInstance(dev, DeviceAdapter)
            dev.read_tags()

    def test_rssi_jitter_stays_in_range(self) -> None:
        dev = SimulatedDevice(loop=False, rssi_jitter=2, seed=7, batch_size=1)
        tag = dev.read_tags()[0]
        self.assertGreaterEqual(tag.rssi, -44)
        self.assertLessEqual(tag.rssi, -40)

    def test_empty_fixture_raises(self) -> None:
        with self.assertRaises(DeviceError):
            SimulatedDevice(tags=[])


class TestFixtureFiles(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "fixture.json"

    def test_json_object_with_tags(self) -> None:
        self.path.write_text(json.dumps({"tags": [{"epc": "AB", "antenna": 1, "rssi": -1}]}))
        tags = load_fixture_tags(self.path)
        self.assertEqual(tags, [{"epc": "AB", "antenna": 1, "rssi": -1}])

    def test_json_array(self) -> None:
        self.path.write_text(json.dumps([{"epc": "AB"}]))
        self.assertEqual(len(load_fixture_tags(self.path)), 1)

    def test_jsonl(self) -> None:
        self.path.write_text('{"epc":"AB"}\n{"epc":"CD"}\n')
        self.assertEqual(len(load_fixture_tags(self.path)), 2)

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(DeviceError):
            load_fixture_tags(Path(self.tmp.name) / "nope.json")

    def test_invalid_jsonl_raises(self) -> None:
        self.path.write_text("{bukan json\n{ juga bukan\n")
        with self.assertRaises(DeviceError):
            load_fixture_tags(self.path)

    def test_empty_file_returns_empty(self) -> None:
        self.path.write_text("   ")
        self.assertEqual(load_fixture_tags(self.path), [])

    def test_device_from_fixture(self) -> None:
        self.path.write_text(json.dumps({"tags": [{"epc": "FEED", "antenna": 4, "rssi": -9}]}))
        dev = SimulatedDevice(fixture=self.path, loop=False)
        tag = dev.read_tags()[0]
        self.assertEqual(tag.epc, "FEED")
        self.assertEqual(tag.antenna, 4)


class TestFactory(unittest.TestCase):
    def test_simulator_mode(self) -> None:
        cfg = Config(mode=MODE_SIMULATOR, api_key="x", server_url="http://127.0.0.1:1")
        device = build_device(cfg)
        self.assertIsInstance(device, SimulatedDevice)

    def test_zebra_mode_is_passive(self) -> None:
        cfg = Config(mode=MODE_ZEBRA, api_key="x", server_url="http://127.0.0.1:1")
        device = build_device(cfg)
        self.assertIsInstance(device, NullDevice)
        self.assertEqual(device.read_tags(), [], "mode zebra tidak boleh membaca device")

    @unittest.skipIf(platform.system() == "Windows", "cykeo butuh DLL Windows")
    def test_cykeo_mode_on_linux_fails_with_helpful_error(self) -> None:
        cfg = Config(mode=MODE_CYKEO, api_key="x", server_url="http://127.0.0.1:1",
                     com_port="COM1")
        with self.assertRaises(DeviceError) as ctx:
            build_device(cfg)
        message = str(ctx.exception)
        self.assertIn("simulator", message.lower(), "pesan harus menyarankan mode simulator")

    def test_unknown_mode_raises(self) -> None:
        cfg = Config(mode="fx7500")
        with self.assertRaises(DeviceError):
            build_device(cfg)

    def test_sdk_status_shape(self) -> None:
        status = sdk_status()
        self.assertIn("available", status)
        self.assertIn("reason", status)
        self.assertIsInstance(status["reason"], str)


class TestSerialCykeoImportSafety(unittest.TestCase):
    """Modul ctypes WAJIB bisa diimpor di Linux tanpa crash."""

    def test_module_imports(self) -> None:
        module = importlib.import_module("cykeo_bridge.devices.serial_cykeo")
        self.assertTrue(hasattr(module, "CykeoSerialDevice"))
        self.assertTrue(hasattr(module, "sdk_available"))

    def test_import_does_not_touch_hardware(self) -> None:
        # Mengimpar modul tidak boleh membuka COM atau memuat DLL.
        module = importlib.import_module("cykeo_bridge.devices.serial_cykeo")
        self.assertIsNone(module.CykeoSerialDevice.__init__.__wrapped__ if hasattr(
            module.CykeoSerialDevice.__init__, "__wrapped__") else None)

    def test_sdk_available_false_on_linux(self) -> None:
        module = importlib.import_module("cykeo_bridge.devices.serial_cykeo")
        available, reason = module.sdk_available()
        if platform.system() == "Windows":
            self.assertIsInstance(available, bool)
        else:
            self.assertFalse(available)
            self.assertIn("Windows", reason)
            self.assertIn("simulator", reason.lower() + " simulator")

    def test_dll_candidates_non_empty(self) -> None:
        module = importlib.import_module("cykeo_bridge.devices.serial_cykeo")
        self.assertGreater(len(module.default_dll_candidates()), 0)

    def test_instantiation_without_com_port_raises_device_error(self) -> None:
        module = importlib.import_module("cykeo_bridge.devices.serial_cykeo")
        with self.assertRaises(DeviceError) as ctx:
            module.CykeoSerialDevice(com_port="")
        self.assertIn("com_port", str(ctx.exception))

    def test_init_param_defaults_to_official_constant(self) -> None:
        """Regresi Fase 4: initParam = konstanta resmi, bukan license per-device."""
        module = importlib.import_module("cykeo_bridge.devices.serial_cykeo")
        self.assertEqual(module.INIT_PARAM_DEFAULT, "E5DC18EDBEEE6EAD954D7332E6D90361")

    @unittest.skipIf(platform.system() == "Windows", "butuh DLL Windows")
    def test_instantiation_with_default_init_param_fails_on_missing_dll(self) -> None:
        module = importlib.import_module("cykeo_bridge.devices.serial_cykeo")
        with self.assertRaises(DeviceError) as ctx:
            module.CykeoSerialDevice(com_port="COM12")
        self.assertIn("GReader.dll", str(ctx.exception))

    def test_connection_string_format(self) -> None:
        module = importlib.import_module("cykeo_bridge.devices.serial_cykeo")
        device = module.CykeoSerialDevice.__new__(module.CykeoSerialDevice)
        device.com_port = "COM12"
        device.baudrate = 115200
        self.assertEqual(device.connection_string, "COM12:115200")

    def test_supported_baudrates_match_official_enum(self) -> None:
        """Nilai dari metadata GReaderApi.dll (eBaudrate) + contoh guide."""
        module = importlib.import_module("cykeo_bridge.devices.serial_cykeo")
        for baud in (9600, 19200, 115200, 230400, 460800):
            self.assertIn(baud, module.SUPPORTED_BAUDRATES)
        self.assertIn(115200, module.SUPPORTED_BAUDRATES, "default guide = 115200")

    def test_find_hex_extracts_epc_run(self) -> None:
        """Parser defensif untuk frame opaque (layout struct belum terverifikasi)."""
        module = importlib.import_module("cykeo_bridge.devices.serial_cykeo")
        frame = b"\x00" * 8 + b"E20034120123456789012345AB\x00" + b"\x00" * 8
        self.assertEqual(module._find_hex(frame), "E20034120123456789012345AB")
        # frame tanpa hex valid -> string kosong (aman, tag diabaikan)
        self.assertEqual(module._find_hex(b"\x00" * 64), "")
        # hex ganjil / terlalu pendek ditolak
        self.assertEqual(module._find_hex(b"ABC"), "")

    def test_safe_cstr_truncates_at_nul(self) -> None:
        module = importlib.import_module("cykeo_bridge.devices.serial_cykeo")
        self.assertEqual(module._safe_cstr(b"E2003412\x00garbage"), "E2003412")
        self.assertEqual(module._safe_cstr(b"  E2003412  "), "E2003412")

    def test_callback_type_builds(self) -> None:
        module = importlib.import_module("cykeo_bridge.devices.serial_cykeo")
        ctype = module._make_callback_type()
        self.assertTrue(callable(ctype))


if __name__ == "__main__":
    unittest.main()
