"""Test adapter Cykeo via helper .NET (``cykeo_helper``).

Test ini memakai FAKE HELPER — sebuah skrip Python yang berpura-pura jadi
``cykeo-helper.exe`` dan berbicara protokol stdio JSON yang sama.

TUJUAN: membuktikan kontrak bridge <-> helper benar (perintah, urutan, tag).
BUKAN membuktikan reader CK-D5 fisik berfungsi. Smoke test hardware
wajib dijalankan di PC Test dengan reader asli.
"""

from __future__ import annotations

import json
import os
import platform
import stat
import sys
import textwrap

import pytest

from cykeo_bridge.devices import cykeo_helper
from cykeo_bridge.devices.base import DeviceError, RawTag

IS_WINDOWS = platform.system() == "Windows"

# Fake helper: menerima perintah, membalas reply, lalu emit tag saat "start".
FAKE_HELPER = textwrap.dedent(
    """
    #!/usr/bin/env python3
    import json
    import sys
    import threading
    import time

    def emit(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    emit({"type": "hello", "helper": "cykeo-helper",
          "version": "fake", "sdkDir": "."})

    state = {"reading": False}

    def tag_emitter():
        # Emis 2 tag lalu berhenti.
        time.sleep(0.15)
        for epc, rssi, ant in (("E28011606000020536AB01", 61, 1),
                               ("0xE28011606000020536AB02", 55, 2)):
            if state["reading"]:
                emit({"type": "tag", "epc": epc, "rssi": rssi,
                      "antId": ant, "tid": "E2801160"})
        time.sleep(0.4)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        rid = req.get("id", 0)
        cmd = req.get("cmd", "")
        if cmd == "start":
            state["reading"] = True
            emit({"type": "reply", "id": rid, "ok": True, "reading": True,
                  "antennaCount": 2, "antennaEnable": 3, "inventoryMode": 1})
            threading.Thread(target=tag_emitter, daemon=True).start()
        elif cmd == "stop":
            state["reading"] = False
            emit({"type": "reply", "id": rid, "ok": True, "reading": False})
        elif cmd == "connect":
            emit({"type": "reply", "id": rid, "ok": True, "connected": True,
                  "connectionString": "%s:%s" % (req.get("com_port"),
                                                  req.get("baud")),
                  "antennaCount": 2})
        elif cmd == "disconnect":
            state["reading"] = False
            emit({"type": "reply", "id": rid, "ok": True, "connected": False})
        elif cmd == "status":
            emit({"type": "reply", "id": rid, "ok": True,
                  "connected": True, "reading": state["reading"],
                  "antennaCount": 2})
        elif cmd == "detect_ports":
            emit({"type": "reply", "id": rid, "ok": True,
                  "ports": [{"port": "COM3"}, {"port": "COM12"}]})
        elif cmd == "ping":
            emit({"type": "reply", "id": rid, "ok": True, "pong": True})
        elif cmd == "shutdown":
            emit({"type": "reply", "id": rid, "ok": True, "bye": True})
            sys.exit(0)
        else:
            emit({"type": "reply", "id": rid, "ok": False,
                  "code": "unknown_cmd", "message": "unknown"})
    """
)


@pytest.fixture
def fake_helper_dir(tmp_path, monkeypatch):
    """Buat folder berisi fake helper + DLL dummy.

    ``monkeypatch`` memaksa ``platform.system()`` jadi Windows supaya adapter
    lolos guard OS di Linux. Shebang fake helper juga diganti agar jalan di
    interpreter yang sedang dipakai test.
    """
    if not IS_WINDOWS:
        monkeypatch.setattr(platform, "system", lambda: "Windows")

    folder = tmp_path / "helper"
    folder.mkdir()

    exe = folder / cykeo_helper.HELPER_EXE_NAME
    body = FAKE_HELPER.strip() + "\n"
    # Ganti shebang dengan interpreter test supaya jalan di Linux.
    lines = body.splitlines()
    if lines and lines[0].startswith("#!"):
        lines[0] = "#!" + sys.executable
    body = "\n".join(lines) + "\n"
    exe.write_text(body)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    # DLL harus ada agar _helper_folder_ok() lolos.
    for dll in cykeo_helper.HELPER_SDK_DLLS:
        (folder / dll).write_bytes(b"MZ-fake-dll")

    return folder


class TestHelperDiscovery:
    """Penemuan helper dan pemeriksaan prasyarat."""

    def test_helper_name_is_exe(self):
        assert cykeo_helper.HELPER_EXE_NAME == "cykeo-helper.exe"

    def test_default_baud_is_115200(self):
        # Production memakai string "<COM>:115200" (lihat CYKeo-FLOW-PRODUCTION.md)
        assert cykeo_helper.BAUD_DEFAULT == 115200

    def test_required_sdks(self):
        # GReaderApi.dll wajib; Newtonsoft hanya dependency helper
        assert "GReaderApi.dll" in cykeo_helper.HELPER_SDK_DLLS

    def test_not_available_on_linux(self, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        assert cykeo_helper.sdk_available() is False
        assert "Windows" in cykeo_helper.unavailable_reason()

    def test_unavailable_reason_when_missing(self, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Windows")
        monkeypatch.setattr(cykeo_helper, "_locate_helper", lambda p=None: None)
        assert cykeo_helper.sdk_available() is False
        assert cykeo_helper.HELPER_EXE_NAME in cykeo_helper.unavailable_reason()

    def test_incomplete_sdk_folder_detected(self, monkeypatch, tmp_path):
        folder = tmp_path / "incomplete"
        folder.mkdir()
        exe = folder / cykeo_helper.HELPER_EXE_NAME
        exe.write_text("#!/bin/sh\n")
        # Tidak ada DLL sama sekali.
        monkeypatch.setattr(platform, "system", lambda: "Windows")
        monkeypatch.setattr(cykeo_helper, "_locate_helper", lambda p=None: str(exe))

        assert cykeo_helper.sdk_available() is False
        reason = cykeo_helper.unavailable_reason()
        assert "GReaderApi.dll" in reason

    def test_complete_sdk_folder_ok(self, monkeypatch, tmp_path):
        folder = tmp_path / "complete"
        folder.mkdir()
        exe = folder / cykeo_helper.HELPER_EXE_NAME
        exe.write_text("#!/bin/sh\n")
        for dll in cykeo_helper.HELPER_SDK_DLLS:
            (folder / dll).write_bytes(b"MZ")
        monkeypatch.setattr(platform, "system", lambda: "Windows")
        monkeypatch.setattr(cykeo_helper, "_locate_helper", lambda p=None: str(exe))

        assert cykeo_helper.sdk_available() is True
        assert cykeo_helper.unavailable_reason() == ""


class TestHelperProtocol:
    """Uji protokol stdio JSON dengan fake helper."""

    def _device(self, folder, **kw):
        kw.setdefault("com_port", "COM3")
        kw.setdefault("baudrate", 115200)
        return cykeo_helper.CykeoHelperDevice(
            helper_path=str(folder / cykeo_helper.HELPER_EXE_NAME), **kw
        )

    def test_handshake_hello(self, fake_helper_dir):
        dev = self._device(fake_helper_dir)
        dev.start()
        try:
            assert dev._proc is not None
            reply = dev._request({"cmd": "ping"})
            assert reply.get("pong") is True
        finally:
            dev.stop()

    def test_connect_sends_com_and_baud(self, fake_helper_dir):
        dev = self._device(fake_helper_dir)
        dev.start()
        try:
            reply = dev._request(
                {"cmd": "connect", "com_port": "COM3", "baud": 115200, "timeout": 60}
            )
            # Production: OpenSerial("<COM>:<baud>")
            assert reply["connectionString"] == "COM3:115200"
        finally:
            dev.stop()

    def test_start_uses_inventory_mode_1(self, fake_helper_dir):
        dev = self._device(fake_helper_dir)
        dev.start()
        try:
            reply = dev._request(
                {"cmd": "start", "inventory_mode": 1, "tid_len": 6, "tid_mode": 0}
            )
            assert reply["inventoryMode"] == 1
            assert reply["antennaEnable"] == 3  # 2 antena -> bitmask 0b11
        finally:
            dev.stop()

    def test_tag_events_are_buffered(self, fake_helper_dir):
        dev = self._device(fake_helper_dir)
        dev.start()
        try:
            dev.start_inventory(0.8)
            tags = dev.read_tags()
            assert len(tags) == 2
            assert tags[0].epc == "E28011606000020536AB01"
            assert tags[0].antenna == 1
            assert tags[0].rssi == 61
            assert tags[1].antenna == 2
        finally:
            dev.stop()

    def test_read_tags_drains_buffer(self, fake_helper_dir):
        dev = self._device(fake_helper_dir)
        dev.start()
        try:
            dev.start_inventory(0.8)
            first = dev.read_tags()
            second = dev.read_tags()
            assert len(first) == 2
            assert second == []
        finally:
            dev.stop()

    def test_event_shape_matches_contract(self, fake_helper_dir):
        dev = self._device(fake_helper_dir)
        dev.start()
        try:
            dev.start_inventory(0.8)
            tags = dev.read_tags()
        finally:
            dev.stop()
        ev = tags[0].to_event()
        # Kontrak §4: hanya key ini yang dikirim ke server.
        assert set(ev) == {"epc", "antenna", "rssi", "timestamp"}

    def test_helper_error_raises_device_error(self, fake_helper_dir):
        dev = self._device(fake_helper_dir)
        dev.start()
        try:
            with pytest.raises(DeviceError):
                dev._request({"cmd": "tidak_ada"})
        finally:
            dev.stop()

    def test_stop_is_idempotent(self, fake_helper_dir):
        dev = self._device(fake_helper_dir)
        dev.start()
        try:
            dev._request({"cmd": "start", "inventory_mode": 1})
            reply = dev._request({"cmd": "stop"})
            assert reply["reading"] is False
            reply2 = dev._request({"cmd": "stop"})
            assert reply2["reading"] is False
        finally:
            dev.stop()


class TestConfigIntegration:
    """Config harus bisa menerima helper_path / sdk_dir."""

    def test_config_fields_exist(self):
        from cykeo_bridge.config import Config

        cfg = Config()
        assert hasattr(cfg, "helper_path")
        assert hasattr(cfg, "sdk_dir")

    def test_factory_cykeo_uses_helper(self, monkeypatch):
        """Mode cykeo harus memakai CykeoHelperDevice, bukan adapter ctypes."""
        from cykeo_bridge.config import Config
        from cykeo_bridge.devices import factory

        called = {}

        class DummyHelper:
            def __init__(self, **kw):
                called.update(kw)

            def start(self):
                return None

        monkeypatch.setattr(
            "cykeo_bridge.devices.cykeo_helper.CykeoHelperDevice", DummyHelper
        )
        cfg = Config(mode="cykeo", com_port="COM3", baudrate=115200)
        factory.build_device(cfg)
        assert called["com_port"] == "COM3"
        assert called["baudrate"] == 115200
