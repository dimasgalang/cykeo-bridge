"""Regresi fitur gain / daya tx antena reader Cykeo.

Konteks kontrak (3 sumber independen):
- SDK ``GReaderApi.dll`` (reflection): ``MsgBaseSetPower.DicPower`` =
  ``Dictionary<Byte, Byte>``; ``MsgBaseGetCapabilities.MaxPower``/``MinPower``
  = ``Byte``.
- Decompile production ``FlexiAudit.Infrastructure.RfidReader.CykeoReader``:
  ``SetReaderPower`` mengisi dictionary dengan key antena **1-based**
  (``i + 1``) dan nilai = level daya.
- Manual resmi §6.2.2: rentang power dibaca dari capabilities
  (``MinPower``..``MaxPower``), nilainya per-unit.

Yang dikunci test ini:
1. ``Config.tx_power`` ada, punya default, coercion int, validasi, env override.
2. Adapter meneruskan nilai gain yang benar ke perintah ``start``.
3. Helper .NET memakai kontrak DLL yang benar (1-based antenna, clamp ke
   rentang reader, default = tertinggi).
4. Installer (PS1 + GUI setup + tray) menuliskan ``tx_power``/``-TxPower``.
5. Salinan paket ``dist/`` sinkron dengan sumber.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from cykeo_bridge.config import (
    ENV_TX_POWER,
    MODE_CYKEO,
    Config,
    config_from_env,
    load_config,
)

REPO = Path(__file__).resolve().parents[1]
HELPER_CS = REPO / "helper" / "Program.cs"
PS1 = REPO / "installer" / "install_cykeo.ps1"
TRAY_CS = REPO / "tray" / "ConfigureDialog.cs"
EXAMPLE = REPO / "config.example.json"
SETUP_CS = Path("/home/hermes/workspace/cykeo-installer-release/setup/CykeoSetupGui.cs")


# --------------------------------------------------------------------- #
# 1. Config
# --------------------------------------------------------------------- #
class TestConfigTxPower:
    def test_default_adalah_none(self):
        """Default: pakai level tertinggi yang didukung reader (runtime)."""
        assert Config().tx_power is None

    def test_field_ada_di_dataclass(self):
        assert "tx_power" in Config().to_dict()

    def test_coercion_dari_string(self):
        """File config diedit manual -> nilai string harus jadi int."""
        cfg = Config.from_dict({"tx_power": "25"})
        assert cfg.tx_power == 25
        assert isinstance(cfg.tx_power, int)

    def test_coercion_nilai_rukyah_dibuang(self):
        cfg = Config.from_dict({"tx_power": "abc"})
        assert cfg.tx_power is None

    def test_minus_satu_diterima(self):
        """-1 = sentinel "pakai level tertinggi yang didukung reader"."""
        cfg = Config(mode=MODE_CYKEO, api_key="x", tx_power=-1)
        assert "tx_power" not in " ".join(cfg.validate())

    def test_nilai_kurang_dari_minus_satu_ditolak(self):
        cfg = Config(mode=MODE_CYKEO, api_key="x", tx_power=-5)
        errors = cfg.validate()
        assert any("tx_power" in e for e in errors), errors

    def test_nol_diterima(self):
        """0 = jangan sentuh setting power reader."""
        cfg = Config(mode=MODE_CYKEO, api_key="x", tx_power=0)
        assert "tx_power" not in " ".join(cfg.validate())

    def test_nilai_positif_diterima(self):
        cfg = Config(mode=MODE_CYKEO, api_key="x", tx_power=30)
        assert "tx_power" not in " ".join(cfg.validate())

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv(ENV_TX_POWER, "27")
        cfg = config_from_env(mode=MODE_CYKEO, api_key="x")
        assert cfg.tx_power == 27

    def test_env_override_tidak_ikut_saat_kosong(self, monkeypatch):
        monkeypatch.setenv(ENV_TX_POWER, "")
        cfg = config_from_env(mode=MODE_CYKEO, api_key="x")
        assert cfg.tx_power is None

    def test_roundtrip_lewat_file(self, tmp_path):
        target = tmp_path / "config.json"
        cfg = Config(mode=MODE_CYKEO, api_key="k", tx_power=28)
        cfg.save(target)
        again = load_config(target)
        assert again.tx_power == 28


# --------------------------------------------------------------------- #
# 2. Adapter meneruskan gain
# --------------------------------------------------------------------- #
class TestAdapterTxPower:
    def _device(self, tx_power):
        from cykeo_bridge.devices import cykeo_helper as H

        dev = H.CykeoHelperDevice.__new__(H.CykeoHelperDevice)
        dev.tx_power = tx_power
        return dev

    def test_none_mengirim_minus_satu(self):
        """None -> -1: helper pilih level tertinggi dari capabilities."""
        assert self._device(None)._power_arg() == -1

    def test_nol_mengirim_nol(self):
        """0 -> jangan ubah setting power reader."""
        assert self._device(0)._power_arg() == 0

    def test_nilai_khusus_dikirim_apa_adanya(self):
        assert self._device(30)._power_arg() == 30

    def test_instance_tanpa_init_aman(self):
        """Instance yang dibuat via __new__ tetap aman."""
        from cykeo_bridge.devices import cykeo_helper as H

        dev = H.CykeoHelperDevice.__new__(H.CykeoHelperDevice)
        assert dev._power_arg() == -1

    def test_payload_start_memuat_power(self):
        """Perintah start ke helper harus membawa field power."""
        from cykeo_bridge.devices import cykeo_helper as H

        dev = H.CykeoHelperDevice.__new__(H.CykeoHelperDevice)
        dev.tx_power = 20
        dev._proc = object()  # sudah "jalan" supaya start() tidak dipanggil
        sent = {}

        def fake_request(payload, timeout=10.0):
            sent.update(payload)
            return {}

        dev._request = fake_request
        dev.begin_inventory()
        assert sent["cmd"] == "start"
        assert sent["power"] == 20

    def test_factory_meneruskan_tx_power(self):
        """Config.tx_power harus sampai ke adapter."""
        from cykeo_bridge.devices import factory

        captured = {}

        class FakeDevice:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def start(self):
                pass

        import cykeo_bridge.devices.cykeo_helper as H

        real = H.CykeoHelperDevice
        H.CykeoHelperDevice = FakeDevice
        try:
            factory.build_device(Config(mode=MODE_CYKEO, api_key="k", tx_power=22))
        finally:
            H.CykeoHelperDevice = real

        assert captured["tx_power"] == 22


# --------------------------------------------------------------------- #
# 3. Helper .NET memakai kontrak SDK yang benar
# --------------------------------------------------------------------- #
class TestHelperCsContract:
    @pytest.fixture(scope="class")
    def src(self):
        return HELPER_CS.read_text(encoding="utf-8")

    def test_ada_handler_set_power(self, src):
        assert '"set_power"' in src
        assert "HandleSetPower" in src

    def test_ada_handler_get_power(self, src):
        assert '"get_power"' in src
        assert "HandleGetPower" in src

    def test_memakai_msgbase_setpower(self, src):
        """Kontrak SDK: MsgBaseSetPower dengan DicPower."""
        assert "MsgBaseSetPower" in src
        assert "DicPower" in src

    def test_kunci_antena_satu_based(self, src):
        """Production memakai key 1-based: (byte)(i + 1)."""
        assert re.search(r"dic\.Add\(\(byte\)\(i \+ 1\)", src), \
            "kunci dictionary harus 1-based (i + 1), sama seperti production"

    def test_default_adalah_level_tertinggi(self, src):
        """Default set_power = MaxPower (level tertinggi yang didukung)."""
        assert re.search(r"ReadInt\(req, \"power\", _maxPower\)", src), \
            "default set_power harus _maxPower"

    def test_clamp_ke_rentang_reader(self, src):
        """Nilai di luar rentang harus di-clamp, bukan dikirim mentah."""
        assert "Math.Max(_minPower, Math.Min(_maxPower" in src

    def test_probe_capabilities_membaca_minmax(self, src):
        assert "ProbeCapabilities" in src
        assert "caps.MaxPower >= caps.MinPower" in src
        assert "_minPower = caps.MinPower" in src
        assert "_maxPower = caps.MaxPower" in src

    def test_power_diapakai_sebelum_inventory(self, src):
        """Reader hanya menerima pesan konfigurasi saat idle.

        Dicek di dalam method HandleStart saja, bukan seluruh file — nama
        MsgBaseInventoryEpc juga muncul di header komentar.
        """
        body = src[src.find("private static void HandleStart"):]
        idx_set = body.find("new MsgBaseSetPower { DicPower = dic }")
        idx_inv = body.find("new MsgBaseInventoryEpc")
        assert idx_set != -1, "HandleStart harus menerapkan power"
        assert idx_inv != -1
        assert idx_set < idx_inv, "set power harus dikirim sebelum inventory"

    def test_status_melaporkan_power(self, src):
        assert '"minPower"' in src
        assert '"maxPower"' in src
        assert '"txPower"' in src

    def test_menggunakan_msgbasegetpower(self, src):
        assert "MsgBaseGetPower" in src


# --------------------------------------------------------------------- #
# 4. Installer menuliskan gain
# --------------------------------------------------------------------- #
class TestInstallerTxPower:
    @pytest.fixture(scope="class")
    def ps1(self):
        return PS1.read_text(encoding="utf-8")

    def test_ps1_punya_parameter_txpower(self, ps1):
        assert re.search(r"\[int\]\$TxPower\s*=\s*-1", ps1), \
            "installer harus punya parameter -TxPower default -1"

    def test_ps1_menulis_tx_power_ke_config(self, ps1):
        assert "tx_power" in ps1

    def test_ps1_default_menunjukkan_tertinggi(self, ps1):
        """Default -1 = level tertinggi yang didukung reader."""
        assert "-TxPower = -1" in ps1 or "$TxPower = -1" in ps1

    def test_config_example_punya_tx_power(self):
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        assert "tx_power" in data
        assert data["tx_power"] == -1

    def test_config_example_ada_penjelasan(self):
        raw = EXAMPLE.read_text(encoding="utf-8")
        assert "_tx_power" in raw
        assert "TERTINGGI" in raw or "tertinggi" in raw


class TestTrayTxPower:
    @pytest.fixture(scope="class")
    def src(self):
        return TRAY_CS.read_text(encoding="utf-8")

    def test_ada_field_tx_power(self, src):
        assert "_txPower" in src

    def test_menulis_tx_power_ke_config_json(self, src):
        assert '"tx_power"' in src

    def test_membaca_tx_power_lama(self, src):
        assert 'Get(data, "tx_power"' in src

    def test_validasi_teks_bukan_angka(self, src):
        assert "Gain antena harus angka" in src

    def test_default_minus_satu(self, src):
        assert 'Get(data, "tx_power", "-1")' in src


class TestSetupGuiTxPower:
    @pytest.fixture(scope="class")
    def src(self):
        if not SETUP_CS.is_file():
            pytest.skip("repo installer-release tidak ada di mesin ini")
        return SETUP_CS.read_text(encoding="utf-8")

    def test_ada_kontrol_gain(self, src):
        assert "_txPower" in src

    def test_meneruskan_tx_power_ke_ps1(self, src):
        assert "-TxPower" in src

    def test_default_tertinggi(self, src):
        assert '_txPower.SelectedItem = "-1"' in src

    def test_form_tinggi_naik(self, src):
        """Tambah 1 baris field -> tinggi form harus naik, tombol tak terpotong."""
        assert "ClientSize = new Size(560, 590)" in src


# --------------------------------------------------------------------- #
# 5. Paket installer sinkron dengan sumber
# --------------------------------------------------------------------- #
class TestPackageSync:
    @pytest.mark.parametrize("rel", [
        "config.py",
        "devices/cykeo_helper.py",
        "devices/factory.py",
        "wizard.py",
    ])
    def test_salinan_paket_identik(self, rel):
        src = REPO / "cykeo_bridge" / rel
        dst = REPO / "dist" / "install_cykeo" / "bridge" / "cykeo_bridge" / rel
        if not dst.is_file():
            pytest.skip("paket belum dibangun di mesin ini")
        assert dst.read_text(encoding="utf-8") == src.read_text(encoding="utf-8"), \
            f"dist/.../{rel} beda dengan sumber - paket akan basi"

    def test_config_example_paket_identik(self):
        dst = REPO / "dist" / "install_cykeo" / "config.example.json"
        if not dst.is_file():
            pytest.skip("paket belum dibangun di mesin ini")
        assert dst.read_text(encoding="utf-8") == EXAMPLE.read_text(encoding="utf-8")

    def test_helper_exe_paket_ada(self):
        dst = REPO / "dist" / "install_cykeo" / "cykeo-helper.exe"
        if not dst.is_file():
            pytest.skip("paket belum dibangun di mesin ini")
        assert dst.stat().st_size > 0
