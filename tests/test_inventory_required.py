"""REGRESSION TEST untuk bug "reader terbuka tapi 0 tag".

BUG ASLI (v1.6.4 dan sebelumnya)
--------------------------------
``read_tags()`` HANYA menguras buffer tag. Reader CK-D5 tidak memindai
spontan: ia diam sampai diberi perintah inventory (``MsgBaseInventoryEpc``
lewat helper .NET). Driver tidak pernah memanggil perintah itu, sehingga:

  * Uji Pembaca selalu ``TIDAK_ADA_TAG`` / 0 EPC apa pun tag yang*didekatan*,
  * agent utama berjalan seharian dan tetap mengirim 0 event ke server.

TAPI handshake + ``OpenSerial(COM3:115200)`` SUDAH berhasil, jadi dialog
menyebut "Reader terbuka" dan "Port COM: COM3" —وجيه yang menipu: koneksi
benar, tapi reader tidak pernah disuruh memindai.

Test ini memakai FAKE HELPER yang hanya emitting tag saat ``reading=True``
(yaitu hanya setelah menerima perintah ``start``). Dengan begitu test bisa
membuktikan sendiriskuadanya: tanpa ``begin_inventory()`` buffer PASTI kosong.

BUKAN acceptance test hardware. Smoke test dengan reader asli wajib
dijalankan di PC Test (lihat docs / README bagian Uji Pembaca).
"""

from __future__ import annotations

import stat
import sys
import textwrap

import pytest

from cykeo_bridge.devices import cykeo_helper
from cykeo_bridge.devices.base import DeviceAdapter, RawTag

# Fake helper: SAMA seperti di test_cykeo_helper.py, tapi tag HANYA keluar
# kalau sudah menerima "start". Inilah yang membuat bug ini bisa dibuktikan.
FAKE_HELPER_GATED = textwrap.dedent(
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
          "version": "fake-gated", "sdkDir": "."})

    state = {"reading": False, "start_count": 0}

    def tag_emitter():
        for epc, rssi, ant in (("E28011606000020536AB01", 61, 1),
                               ("E28011606000020536AB02", 55, 2)):
            if state["reading"]:
                emit({"type": "tag", "epc": epc, "rssi": rssi,
                      "antId": ant, "tid": "E2801160"})
            time.sleep(0.05)
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
            state["start_count"] += 1
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
                  "startCount": state["start_count"], "antennaCount": 2})
        elif cmd == "shutdown":
            emit({"type": "reply", "id": rid, "ok": True, "bye": True})
            sys.exit(0)
        else:
            emit({"type": "reply", "id": rid, "ok": False,
                  "code": "unknown_cmd", "message": "unknown"})
    """
)


@pytest.fixture
def gated_helper_dir(tmp_path, monkeypatch):
    """Folder berisi fake helper TERGATE + DLL dummy (bukti untuk regression)."""
    import platform

    if platform.system() != "Windows":
        monkeypatch.setattr(platform, "system", lambda: "Windows")

    folder = tmp_path / "helper_gated"
    folder.mkdir()

    exe = folder / cykeo_helper.HELPER_EXE_NAME
    lines = FAKE_HELPER_GATED.strip().splitlines()
    if lines and lines[0].startswith("#!"):
        lines[0] = "#!" + sys.executable
    exe.write_text("\n".join(lines) + "\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    for dll in cykeo_helper.HELPER_SDK_DLLS:
        (folder / dll).write_bytes(b"MZ-fake-dll")

    return folder


def _device(folder, monkeypatch):
    """Bangun adapter yang menunjuk fake helper di ``folder``."""
    import platform

    if platform.system() != "Windows":
        monkeypatch.setattr(platform, "system", lambda: "Windows")

    dev = cykeo_helper.CykeoHelperDevice(
        com_port="COM3",
        baudrate=115200,
        helper_path=str(folder / cykeo_helper.HELPER_EXE_NAME),
        sdk_dir=str(folder),
    )
    dev.start()
    return dev


class TestInventoryIsRequired:
    """Bukti: tanpa inventory, reader MUSTAHIL membaca tag."""

    def test_without_begin_inventory_no_tag_is_read(self, gated_helper_dir, monkeypatch):
        """REGRESSION INTI: begin_inventory() tidak dipanggil -> 0 tag.

        Ini persis kondisi v1.6.4: connect succeed, loop read_tags() berjalan
        25 detik, hasilnya 0 tag. Test ini mengunci perilaku itu supaya
        tidak bisa "hilang" lagi tanpa sengaja.
        """
        dev = _device(gated_helper_dir, monkeypatch)
        try:
            # Sengaja TIDAK panggil begin_inventory() -> meniru driver lama.
            tags = []
            for _ in range(10):
                tags.extend(dev.read_tags())
                import time as _t
                _t.sleep(0.08)

            assert tags == [], (
                "fake helper gated: tanpa 'start' tidak boleh ada tag. "
                "Kalau test ini gagal, fake helper bocor -> test tidak valid."
            )
        finally:
            dev.stop()

    def test_with_begin_inventory_tag_is_read(self, gated_helper_dir, monkeypatch):
        """Bukti positif: begin_inventory() -> tag masuk buffer."""
        import time

        dev = _device(gated_helper_dir, monkeypatch)
        try:
            dev.begin_inventory()          # <-- perintah yang dulu hilang
            tags = []
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and len(tags) < 2:
                tags.extend(dev.read_tags())
                time.sleep(0.05)
            dev.end_inventory()

            eps = [t.epc for t in tags]
            assert "E28011606000020536AB01" in eps, (
                "begin_inventory() harus membuat reader mengirim EPC. "
                "Dapat: %r" % (eps,)
            )
        finally:
            dev.stop()

    def test_inventory_is_idempotent_per_device(self, gated_helper_dir, monkeypatch):
        """end_inventory() tidak boleh melempar walau dipanggil dua kali."""
        dev = _device(gated_helper_dir, monkeypatch)
        try:
            dev.begin_inventory()
            dev.end_inventory()
            dev.end_inventory()   # kedua kali: tidak apa-apa
        finally:
            dev.stop()

    def test_read_tags_still_drains_buffer(self, gated_helper_dir, monkeypatch):
        """read_tags() tetap drain-once, tidak jadi duplikat."""
        import time

        dev = _device(gated_helper_dir, monkeypatch)
        try:
            dev.begin_inventory()
            time.sleep(0.4)
            first = dev.read_tags()
            second = dev.read_tags()
            dev.end_inventory()
            assert len(first) == 2
            assert second == []
        finally:
            dev.stop()


class TestAdapterContract:
    """Interface: begin/end inventory harus ada dan aman dipanggil."""

    def test_base_defaults_are_noop(self):
        """Adapter tanpa dukungan inventory tidak boleh/error."""

        class Minimal(DeviceAdapter):
            name = "minimal"

            def read_tags(self):
                return []

            def describe(self):
                return {"adapter": self.name}

        dev = Minimal()
        assert dev.begin_inventory() is None
        assert dev.end_inventory() is None

    def test_readertest_calls_begin_inventory(self, monkeypatch):
        """Uji Pembaca WAJIB memanggil begin_inventory() sebelum loop baca."""
        import time as _t

        from cykeo_bridge import readertest
        from cykeo_bridge.config import Config

        dipanggil = {"begin": 0, "end": 0}

        class FakeDevice:
            def __init__(self):
                self.tag_sudah_keluar = False

            def begin_inventory(self):
                dipanggil["begin"] += 1
                self.tag_sudah_keluar = True

            def end_inventory(self):
                dipanggil["end"] += 1

            def read_tags(self):
                if not self.tag_sudah_keluar:
                    return []          # meniru fake helper gated
                if self.tag_sudah_keluar:
                    out = [RawTag(epc="E28011606000020536AB01",
                                  antenna=1, rssi=61,
                                  timestamp="2026-09-29T00:00:00Z")]
                    self.tag_sudah_keluar = False
                    return out
                return []

            def close(self):
                pass

        monkeypatch.setattr(
            "cykeo_bridge.devices.factory.build_device",
            lambda config: FakeDevice(),
        )
        monkeypatch.setattr(readertest, "JEDA_PEMBACAAN", 0.01)
        monkeypatch.setattr(readertest, "DURASI_AUTOMATIS", 0.4)

        cfg = Config(mode="cykeo", com_port="COM3")
        hasil = readertest.jalankan_uji(cfg, durasi=0.4)

        assert dipanggil["begin"] == 1, "Uji Pembaca harus mulai inventory 1x"
        assert dipanggil["end"] == 1, "Uji Pembaca harus stop inventory 1x"
        assert hasil.total_dibaca >= 1, (
            "Uji Pembaca harus baca tag setelah inventory dimulai. "
            "Dapat total=%r" % (hasil.total_dibaca,)
        )
        assert hasil.ok, "Uji Pembaca harus sukses: %s" % (hasil.error,)

    def test_readertest_closes_even_if_begin_fails(self, monkeypatch):
        """Kalau begin_inventory() error, Uji Pembaca tetap tutup device."""

        from cykeo_bridge import readertest
        from cykeo_bridge.config import Config

        state = {"closed": False, "end": 0}

        class BrokenBegin(DeviceAdapter):
            name = "broken"

            def begin_inventory(self):
                raise RuntimeError("port belum siap")

            def end_inventory(self):
                state["end"] += 1

            def read_tags(self):
                return []

            def close(self):
                state["closed"] = True

            def describe(self):
                return {"adapter": self.name}

        monkeypatch.setattr(
            "cykeo_bridge.devices.factory.build_device",
            lambda config: BrokenBegin(),
        )
        monkeypatch.setattr(readertest, "JEDA_PEMBACAAN", 0.01)

        cfg = Config(mode="cykeo", com_port="COM3")
        hasil = readertest.jalankan_uji(cfg, durasi=0.2)

        assert state["closed"] is True, "device harus ditutup walau begin gagal"
        assert hasil.error == "TIDAK_ADA_TAG"
