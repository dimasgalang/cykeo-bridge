"""Regression test untuk installer v1.6.8.

Konteks: di PC test, log installer v1.6.7 menunjukkan

    [!] Runtime Python mandiri TIDAK bisa dipakai.
        Bridge akan mencoba Python sistem sebagai cadangan.

lalu bridge berhenti. Tiga sebab yang bisa diperbaiki di sisi installer:

1. Installer hanya menghentikan proses bernama 'cykeo_bridge' dan
   'cykeo-helper'. Runtime Python embeddacle bernama 'python'/'pythonw',
   sehingga python.exe versi lama masih hidup saat folder python-embed
   dihapus -> Remove-Item/Copy-Item gagal sebagian dan meninggalkan
   runtime campuran dua versi yang tidak bisa di-import.

2. Setelah menyalin runtime, jumlah file tidak pernah dibandingkan dengan
   sumber, jadi salin yang tidak lengkap lolos tanpa pesan.

3. Probe runtime membuang stderr (2>$null), sehingga penyebab kegagalan
   tidak pernah terlihat oleh teknisi.

Test di bawah mengunci ketiga perbaikan itu di level teks installer.
Ini bukan pengganti uji di Windows, tapi gunanya nyata: pola bug ini
sudah pernah lolos review sekali, dan difix-nya hanya beberapa baris
yang mudah terhapus lagi.
"""
from __future__ import annotations

import re
import shutil
import unittest
from pathlib import Path

REPO_CANDIDATES = [
    Path("/home/hermes/workspace/cykeo-installer-release"),
    Path(__file__).resolve().parents[2] / "cykeo-installer-release",
]
INSTALLER = "install_cykeo.ps1"
TRAY = "tray/TrayAgent.cs"


def _find_repo() -> Path:
    for cand in REPO_CANDIDATES:
        if (cand / INSTALLER).is_file():
            return cand
    if shutil.which("cykeo-installer-release"):  # pragma: no cover
        return Path(shutil.which("cykeo-installer-release"))
    raise unittest.SkipTest("repo cykeo-installer-release tidak ditemukan")


class TestInstallerRuntimeFix(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo = _find_repo()
        cls.ps1 = (cls.repo / INSTALLER).read_text(encoding="utf-8")
        cls.cs = (cls.repo / TRAY).read_text(encoding="utf-8")

    # --- 1. proses runtime harus ikut dihentikan ------------------------
    def test_installer_kill_python_processes(self) -> None:
        """Installer harus ikut menghentikan python/pythonw, bukan hanya cykeo_bridge.

        Nama proses runtime embeddable adalah 'python'/'pythonw'. Kalau
        keduanya tidak ada di daftar kandidat, python.exe lama tetap
        memegang folder python-embed dan hasil install rusak.
        """
        m = re.search(
            r"\$cands\s*=\s*@\('cykeo_bridge'.*?\)", self.ps1, re.S
        )
        self.assertIsNotNone(
            m,
            "installer harus punya daftar $cands proses produk yang mau "
            "dihentikan sebelum folder install dihapus",
        )
        cands = m.group(0)
        for name in ("cykeo_bridge", "cykeo-helper", "python", "pythonw"):
            self.assertIn(
                name, cands,
                f"proses '{name}' harus ikut dihentikan sebelum folder "
                f"install dihapus, kalau tidak runtime lama mengunci file",
            )

    def test_installer_only_kills_own_python(self) -> None:
        """Python milik user sendiri tidak boleh dibunuh.

        Kalau installer killing semua proses bernama 'python', PC teknisi
        yang kebetulan menjalankan script Python lain ikut rusak. Jadi
        penentuan 'milik kita' harus lewat path di dalam folder install.
        """
        self.assertIn(
            "StartsWith", self.ps1,
            "installer harus mengecek path proses sebelum membunuhnya",
        )
        self.assertIn(
            "$inOwn", self.ps1,
            "installer harus menyimpan path folder install untuk perbandingan",
        )

    def test_installer_waits_after_kill(self) -> None:
        """Harus ada jeda setelah Kill supaya OS melepas file handle.

        Kill() hanya mengirim sinyal; Windows bisa butuh waktu singkat
        sebelum semua handle file ditutup. Tanpa jeda, Remove-Item
        masih bisa kalah dan diam-diam menyalin tidak lengkap.
        """
        kill_idx = self.ps1.find(".Kill()")
        self.assertNotEqual(kill_idx, -1, "installer harus memanggil .Kill()")
        after = self.ps1[kill_idx:kill_idx + 800]
        self.assertIn(
            "Start-Sleep", after,
            "perlu Start-Sleep setelah Kill agar file handle benar-benar lepas",
        )

    # --- 2. hasil salin runtime harus diverifikasi ----------------------
    def test_runtime_copy_verified(self) -> None:
        """Jumlah file runtime hasil salin harus dibandingkan dengan sumber."""
        self.assertRegex(
            self.ps1, r"\$srcCount\s*=",
            "installer harus menghitung file runtime di folder sumber",
        )
        self.assertRegex(
            self.ps1, r"\$dstCount\s*=",
            "installer harus menghitung file runtime di folder tujuan",
        )
        self.assertRegex(
            self.ps1, r"\$srcCount\s*-ne\s*\$dstCount",
            "perbedaan jumlah file harus dianggap kegagalan keras",
        )

    def test_incomplete_runtime_is_fatal(self) -> None:
        """Runtime tidak lengkap harus menghentikan installer.

        Kalau hanya warning, installer lanjut ke wizard lalu bridge
        restart tanpa henti. Itu persis gejala lapangan.
        """
        idx = self.ps1.find("$srcCount -ne $dstCount")
        self.assertNotEqual(idx, -1, "perbandingan jumlah file harus ada")
        window = self.ps1[idx:idx + 900]
        self.assertRegex(
            window, r"exit\s+1",
            "runtime tidak lengkap harus exit 1, bukan cuma warning",
        )

    # --- 3. probe runtime harus menampilkan stderr ---------------------
    def test_probe_keeps_stderr(self) -> None:
        """Probe import tidak boleh membuang stderr.

        Pola '2>$null' menghapus satu-satunya petunjuk kenapa runtime
        gagal, sehingga teknisi hanya melihat 'TIDAK bisa dipakai'.
        """
        probe_idx = self.ps1.find("$probe = 'import json,ssl,sqlite3")
        self.assertNotEqual(probe_idx, -1, "probe import harus ada di installer")
        window = self.ps1[probe_idx:probe_idx + 600]
        self.assertNotIn(
            "2>$null", window,
            "probe runtime membuang stderr; penyebab kegagalan jadi tidak terlihat",
        )
        self.assertIn(
            "2>&1", window,
            "probe runtime harus menangkap stderr (2>&1) supaya pesan tampil",
        )

    def test_probe_error_shown_to_user(self) -> None:
        """Pesan error dari interpreter harus benar-benar ditampilkan."""
        self.assertIn(
            "$probeErr", self.ps1,
            "hasil error probe harus disimpan untuk ditampilkan",
        )
        self.assertIn(
            "Pesan dari interpreter", self.ps1,
            "pesan error interpreter harus dicetak untuk teknisi",
        )

    # --- 4. tray: log penyebab kegagalan runtime ------------------------
    def test_tray_logs_runtime_failure(self) -> None:
        """Tray harus mencatat alasan runtime ditolak.

        IsUsablePython sebelumnya hanya mengembalikan false tanpa
       jejak, sehingga log tray tidak pernah menjelaskan kenapa bridge
        jatuh ke Python sistem.
        """
        def_idx = self.cs.find("private static bool IsUsablePython")
        self.assertNotEqual(
            def_idx, -1, "definisi IsUsablePython harus ada di tray"
        )
        window = self.cs[def_idx:def_idx + 2200]
        self.assertIn(
            "StandardError.ReadToEnd", window,
            "IsUsablePython harus membaca stderr, bukan cuma stdout",
        )
        self.assertIn(
            'Log("[runtime]', window,
            "kegagalan runtime harus ditulis ke log tray",
        )

    # --- 5. sumber bebas karakter asing ---------------------------------
    def test_sources_are_ascii_only(self) -> None:
        """File yang dikirim ke Windows harus ASCII murni.

        TrayAgent.cs tidak punya BOM, jadi byte non-ASCII bisa dibaca
        salah oleh compiler dan merusak baris komentar. Installer juga
        dibaca PowerShell dengan encoding yang berbeda antar versi.
        """
        for rel in (INSTALLER, TRAY):
            raw = (self.repo / rel).read_bytes()
            bad = [(i, b) for i, b in enumerate(raw) if b > 127]
            self.assertEqual(
                bad, [],
                f"{rel} punya {len(bad)} byte non-ASCII; sumber harus ASCII",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
