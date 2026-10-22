#!/usr/bin/env python3
"""Regresi v1.6.26: kontrak SDK power Cykeo dibuktikan dari DLL production.

Feature gain antena bergantung pada kontrak SDK yang spesifik. Kalau
kontraknya berubah (atau salah duga), helper akan gagal saat runtime
di PC user - jauh setelah paket terbit.

Jadi test ini bukan "cek teks sumber": test ini MENJALANKAN prober
.NET (MetadataLoadContext) terhadap DLL GReaderApi.dll production
sebenarnya, dan membandingkan tipe yang dibaca dari metadata DLL
dengan tipe yang dipakai helper.

Kenapa MetadataLoadContext, bukan load biasa:
  GReaderApi.dll adalah PE32 .NET Framework 4.x. Di Linux tidak ada
  runtime .NET Framework, jadi Assembly.LoadFrom akan gagal dengan
  BadImageFormatException / FileLoadException. MetadataLoadContext
  membaca METADATA tanpa menjalankan kode assembly sama sekali, jadi
  kontrak bisa dibuktikan di mesin build Linux.

Kontrak yang dikunci (sumber kebenaran = DLL production):
  MsgBaseSetPower.DicPower       = Dictionary<Byte,Byte>
  MsgBaseSetPower.IsPersistence  = Int32
  MsgBaseSetPower.Flag           = Byte
  MsgBaseGetCapabilities.MaxPower = Byte
  MsgBaseGetCapabilities.MinPower = Byte
  MsgBaseGetCapabilities.AntennaCount = ada
  MsgBaseGetPower.DicPower       = ada

Test ini DILEWATI (skip) kalau dotnet SDK atau DLL tidak tersedia -
mis. di kontainer CI tanpa .NET. Tapi kalau keduanya ADA, prober WAJIB
lulus; kegagalan berarti kontrak tidak cocok dengan asumsi helper.
"""

import os
import re
import shutil
import subprocess
import unittest

REPO = os.environ.get("CYKEO_BRIDGE_REPO", "/home/hermes/workspace/cykeo-bridge")
PROBE_DIR = os.path.join(REPO, "helper", "contractprobe")
HELPER_SRC = os.path.join(REPO, "helper", "Program.cs")

# Kandidat DLL production. Bridge Binary adalah salinan kanonik; vendor
# installer adalah salinan kedua yang harus identik.
DLL_CANDIDATES = [
    os.path.join(REPO, "dist", "GReaderApi.dll"),
    os.path.join(REPO, "dist", "install_cykeo", "GReaderApi.dll"),
    "/home/hermes/workspace/cykeo-installer-release/vendor/GReaderApi.dll",
]


def find_dotnet():
    for cand in (os.environ.get("DOTNET_BIN"),
                 os.path.expanduser("~/.dotnet/dotnet"),
                 shutil.which("dotnet")):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def find_dll():
    for p in DLL_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class SdkContractTest(unittest.TestCase):
    """Kontrak SDK dibaca langsung dari DLL production."""

    @classmethod
    def setUpClass(cls):
        cls.dotnet = find_dotnet()
        cls.dll = find_dll()
        cls.probe_out = None
        cls.probe_rc = None
        if not cls.dotnet or not cls.dll:
            return
        # Build prober dulu (paket NuGet mungkin belum ter-cache).
        try:
            r = subprocess.run(
                [cls.dotnet, "build", "-c", "Release", "--nologo", "-v", "quiet"],
                cwd=PROBE_DIR, capture_output=True, text=True, timeout=420)
        except Exception as exc:  # noqa: BLE001
            cls.probe_out = "build error: %s" % exc
            return
        if r.returncode != 0:
            cls.probe_out = "build failed:\n" + r.stdout + r.stderr
            return
        try:
            r2 = subprocess.run(
                [cls.dotnet, "run", "-c", "Release", "--no-build", "--", cls.dll],
                cwd=PROBE_DIR, capture_output=True, text=True, timeout=300)
        except Exception as exc:  # noqa: BLE001
            cls.probe_out = "run error: %s" % exc
            return
        cls.probe_out = r2.stdout + r2.stderr
        cls.probe_rc = r2.returncode

    def setUp(self):
        if not self.dotnet:
            self.skipTest("dotnet SDK tidak tersedia")
        if not self.dll:
            self.skipTest("GReaderApi.dll production tidak ditemukan")
        if self.probe_out is None:
            self.skipTest("prober belum menghasilkan output")
        if self.probe_out.startswith("build") or self.probe_out.startswith("run"):
            self.fail("prober kontrak gagal dijalankan:\n" + self.probe_out)

    def test_probe_exit_nol(self):
        self.assertEqual(
            self.probe_rc, 0,
            "prober kontrak SDK keluar dengan kode %s:\n%s"
            % (self.probe_rc, self.probe_out),
        )

    def test_hasik_ok(self):
        self.assertIn(
            "RESULT OK", self.probe_out,
            "kontrak SDK tidak cocok dengan asumsi helper:\n%s" % self.probe_out,
        )

    def test_dicpower_dictionary_byte_byte(self):
        self.assertRegex(
            self.probe_out,
            r"OK\s+MsgBaseSetPower\.DicPower = Dictionary<Byte,Byte>",
            "MsgBaseSetPower.DicPower bukan Dictionary<Byte,Byte>:\n%s"
            % self.probe_out,
        )

    def test_is_persistence_int32(self):
        self.assertRegex(
            self.probe_out,
            r"OK\s+MsgBaseSetPower\.IsPersistence = Int32",
            "MsgBaseSetPower.IsPersistence bukan Int32:\n%s" % self.probe_out,
        )

    def test_flag_byte(self):
        self.assertRegex(
            self.probe_out,
            r"OK\s+MsgBaseSetPower\.Flag = Byte",
            "MsgBaseSetPower.Flag bukan Byte:\n%s" % self.probe_out,
        )

    def test_capabilities_minmax_byte(self):
        self.assertRegex(
            self.probe_out,
            r"OK\s+MsgBaseGetCapabilities\.MaxPower = Byte",
            "MsgBaseGetCapabilities.MaxPower bukan Byte:\n%s" % self.probe_out,
        )
        self.assertRegex(
            self.probe_out,
            r"OK\s+MsgBaseGetCapabilities\.MinPower = Byte",
            "MsgBaseGetCapabilities.MinPower bukan Byte:\n%s" % self.probe_out,
        )

    def test_antenna_count_ada(self):
        self.assertRegex(
            self.probe_out,
            r"OK\s+MsgBaseGetCapabilities\.AntennaCount present",
            "MsgBaseGetCapabilities.AntennaCount tidak ada: helper tidak "
            "bisa tahu jumlah antena untuk diset level sama:\n%s"
            % self.probe_out,
        )


class HelperUsesRealContractTest(unittest.TestCase):
    """Helper harus memakai tipe yang sama seperti hasil probe di atas."""

    def setUp(self):
        self.src = read(HELPER_SRC)

    def test_helper_isi_dicpower_dictionary_byte_byte(self):
        self.assertRegex(
            self.src, r"Dictionary\s*<\s*byte\s*,\s*byte\s*>",
            "helper tidak mengisi DicPower dengan Dictionary<byte,byte>",
        )

    def test_helper_baca_minmax_dari_capabilities(self):
        self.assertIn("MsgBaseGetCapabilities", self.src)
        self.assertRegex(self.src, r"\.MaxPower", "helper tidak membaca MaxPower")
        self.assertRegex(self.src, r"\.MinPower", "helper tidak membaca MinPower")

    def test_helper_kunci_antena_satu_based(self):
        self.assertRegex(
            self.src, r"dic\.Add\(\s*\(byte\)\s*\(\s*i\s*\+\s*1\s*\)",
            "kunci DicPower harus 1-based (1..AntennaCount), sama seperti "
            "kode production",
        )

    def test_helper_clamp_ke_rentang_reader(self):
        self.assertRegex(
            self.src,
            r"Math\.Max\(\s*_minPower\s*,\s*Math\.Min\(\s*_maxPower",
            "helper tidak clamp level ke rentang reader: config salah bisa "
            "merusak setting unit",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
