r"""Regression test untuk fitur v1.6.11: shortcut Desktop + guardian tray.

Konteks. Dua keluhan nyata dari teknisi di PC test:

1. Kalau tray mati (crash / Task Manager "End task" / taskkill), tidak ada
   cara memunculkan ikon lagi tanpa harus_logoff-logon atau cari folder
   install. Shortcut Desktop dan Start Menu harus jadi jalan pulih yang
   bisa diklik user.

2. Tray harus pulih sendiri, TAPI tidak boleh menganggur kalau user sendiri
   yang sengaja keluar dari menu. Dua kasus ini harus BERBEDA:

       tray mati + tanpa penanda  -> guardian hidupkan lagi (crash/taskkill)
       tray mati + ada penanda    -> guardian jangan hidupkan (user mau stop)

   Pembeda yang dipakai adalah file penanda %APPDATA%\CykeoBridge\
   exit-disengaja.flag, ditulis HANYA di jalur Quit() disengaja. Proses yang
   dibunuh paksa tidak pernah sampai ke Quit(), jadi file itu tidak pernah
   ada - itulah yang membuat kedua kasus bisa dibedakan.

Yang dikunci test ini:

- Shortcut Desktop dibuat lewat P/Invoke .NET (IShellLinkW + IPersistFile),
  BUKAN WScript.Shell COM. WScript.Shell gagal di beberapa environment dan
  tidak bisa menulis WorkingDirectory.
- Flag $shortcutOk hanya True kalau file benar-benar ada (Test-Path), bukan
  hanya karena tidak ada error.
- Guardian dibedakan dari tray lewat named mutex, bukan nama proses - karena
  guardian dijalankan dari exe yang sama sehingga namanya identik.
- Penanda disengaja hanya ditulis di Quit() yang disengaja. Jalur Relaunch
  harus Quit(false) supaya guardian tidak salah thinks user mau berhenti.
- Guardian terdaftar di HKCU Run dan dijalankan di sesi installer, supaya
  ikon langsung muncul tanpa perlu logoff.

Test di bawah mengunci semuanya di level teks sumber. Ini bukan pengganti
uji di Windows, tapi polanya sudah pernah lolos review sekali, dan difix-nya
hanya beberapa baris yang mudah terhapus diam-diam.
"""
from __future__ import annotations

import os
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
    # Override env var dipakai uji sabotage: sumber dimodifikasi di staging
    # dan test harus membaca staging itu, bukan repo asli.
    override = os.environ.get("CYKEO_INSTALLER_REPO")
    if override:
        p = Path(override)
        if (p / INSTALLER).is_file():
            return p
    for cand in REPO_CANDIDATES:
        if (cand / INSTALLER).is_file():
            return cand
    if shutil.which("cykeo-installer-release"):  # pragma: no cover
        return Path(shutil.which("cykeo-installer-release"))
    raise unittest.SkipTest("repo cykeo-installer-release tidak ditemukan")


def _method_body(source: str, signature: str) -> str:
    """Kembalikan isi method/sfungsi tanpa blok pembuka/penutupnya."""
    m = re.search(signature, source)
    if not m:
        raise AssertionError("pola tidak ditemukan: %s" % signature)
    start = source.find("{", m.end() - 1)
    if start == -1:
        raise AssertionError("tidak ada '{' setelah %s" % signature)
    depth = 0
    for i in range(start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start + 1 : i]
    raise AssertionError("blok tidak tertutup: %s" % signature)


class TestShortcutDesktop(unittest.TestCase):
    """Fitur 1: shortcut Desktop harus benar-benar dibuat."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.repo = _find_repo()
        cls.ps1 = (cls.repo / INSTALLER).read_text(encoding="utf-8")

    def test_fungsi_shortcut_desktop_ada(self) -> None:
        """Installer harus punya fungsi khusus membuat shortcut Desktop."""
        self.assertIn("function New-DesktopShortcut", self.ps1)

    def test_shortcut_bukan_wscript_shell(self) -> None:
        """Shortcut Desktop harus P/Invoke, bukan WScript.Shell COM.

        WScript.Shell tidak bisa menulis WorkingDirectory, dan di beberapa
        Windows edition pemanggilannya diblokir. IShellLinkW + IPersistFile
        lewat Add-Type adalah cara yang dipakai.
        """
        blok = self._blok_shortcut()
        self.assertIn("IShellLinkW", blok)
        self.assertIn("IPersistFile", blok)
        self.assertNotIn("WScript.Shell", blok)

    def _blok_shortcut(self) -> str:
        """Blok kode shortcut: Add-Type C# + fungsi wrapper.

        Logika P/Invoke berada di blok C# yang di-Add-Type, sementara fungsi
        PowerShell hanya pembungkus tipis. Menguji satu-duanya akan salah,
        jadi keduanya diambil.
        """
        m = re.search(
            r"if\s*\(\s*-not\s*\(\s*'CykeoShellLink'\s+-as", self.ps1
        )
        self.assertIsNotNone(m, "blok Add-Type shortcut tidak ditemukan")
        akhir = self.ps1.index("function New-DesktopShortcut", m.start())
        return self.ps1[m.start() : akhir]

    def test_shortcut_set_working_directory(self) -> None:
        """Shortcut harus membuka folder install sebagai working directory.

        Tanpa ini, bridge python launched dari folder lain tidak menemukan
        cykeo_bridge package.
        """
        blok = self._blok_shortcut()
        self.assertIn("SetWorkingDirectory", blok)
        self.assertIn("SetPath", blok)
        self.assertIn("SetIconLocation", blok)
        # Pemanggilan SetWorkingDirectory harus DIPANGGIL, bukan hanya dideklarasikan
        # di interface. Menhapusnya dari blok Create harus membuat test gagal.
        self.assertRegex(
            blok,
            r"lnk\.SetWorkingDirectory\(workingDir\)",
            "SetWorkingDirectory tidak pernah dipanggil pada shortcut",
        )

    def test_shortcut_ok_hanya_true_kalau_file_ada(self) -> None:
        """$shortcutOk harus dibuktikan Test-Path, bukan asumsi.

        Ini pola bug yang sama seperti $trayOk v1.4.0: file ikut ter-copy,
        variabel tetap false - atau sebaliknya, variabel true padahal file
        tidak pernah ada karena COM gagal diam-diam.
        """
        m = re.search(r"\$shortcutOk\s*=\s*\$true", self.ps1)
        self.assertIsNotNone(m, r"tidak ada \$shortcutOk = \$true sama sekali")
        sebelum = self.ps1[: m.start()]
        # verifikasi harus benar-benar dijalankan sebelum flag di-set
        self.assertIn("Test-Path -LiteralPath $shortcutPath", sebelum)
        self.assertIn("Get-Item -LiteralPath $shortcutPath", sebelum)
        # posisi flag: SETELAH blok if yang berisi Test-Path
        self.assertLess(
            sebelum.rindex("Test-Path -LiteralPath $shortcutPath"),
            m.start(),
            "shortcutOk harus di-set True setelah file dibuktikan ada di disk",
        )

    def test_path_desktop_diverifikasi(self) -> None:
        """Folder Desktop harus di-Test-Path, dengan fallback USERPROFILE.

        GetFolderPath('Desktop') mengembalikan string kosong kalau foldernya
        belum pernah dibuat, dan kalau installer berjalan sebagai user lain
        path-nya menunjuk ke folder milik orang lain.
        """
        m = re.search(r"\$deskDir\s*=(.+)", self.ps1)
        self.assertIsNotNone(m, "tidak ada penetapan $deskDir")
        blok = self.ps1[m.start() : m.start() + 900]
        self.assertIn("GetFolderPath", blok)
        self.assertIn("Test-Path", blok)
        self.assertIn("USERPROFILE", blok)

    def test_shortcut_dipanggil_dan_laporan(self) -> None:
        """Shortcut harus dipanggil installer dan dilaporkan ke user.

        Kalau hanya didefinisikan tapi tidak pernah dipanggil, user tidak
        akan pernah melihat ikon - persis masalah yangtrying diselesaikan.
        """
        self.assertRegex(
            self.ps1,
            r"New-DesktopShortcut\s+-Path",
            "fungsi New-DesktopShortcut tidak pernah dipanggil",
        )
        self.assertIn("$shortcutPath", self.ps1)
        # harus muncul di laporan exit code / status, bukan cuma di log
        self.assertIn("Shortcut      :", self.ps1)


class TestTrayRecovery(unittest.TestCase):
    """Fitur 2: guardian harus pulihkan tray, tapi hormati exit disengaja."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.repo = _find_repo()
        cls.cs = (cls.repo / TRAY).read_text(encoding="utf-8")

    def test_penanda_disengaja_ada(self) -> None:
        """Harus ada konstanta nama file penanda."""
        self.assertIn("exit-disengaja.flag", self.cs)

    def test_mode_watchdog_dan_relay(self) -> None:
        """Harus ada mode --watchdog (guardian) dan --relay (restart tray)."""
        self.assertIn('WatchdogArg = "--watchdog"', self.cs)
        self.assertIn('RelayArg = "--relay"', self.cs)

    def test_guardian_dibedakan_lewat_mutex_bukan_nama_proses(self) -> None:
        """Single-instance dan guardian harus pakai named mutex.

        Guardian dijalankan dari exe yang sama, jadi nama prosesnya
        identik. Kalau logika masih memindai nama proses, guardian akan
        salah thinks tray sudah jalan - atau sebaliknya saling kill.
        """
        self.assertIn("WatchdogMutexName", self.cs)
        self.assertIn("TrayMutexName", self.cs)
        self.assertRegex(
            self.cs,
            r"new\s+Mutex\(\s*true,\s*WatchdogMutexName",
            "guardian harus memegang named mutex sendiri",
        )
        # Single-instance WAJIB lewat mutex: kalau pemindaian nama proses
        # kembali dipakai, guardian akan dianggap "sudah berjalan" dan tray
        # tidak akan pernah bisa start. Hanya pemindaian nama proses yang
        # BOLEH tersisa adalah KillByName (dipakai untuk process anak).
        main = _method_body(self.cs, r"private\s+static\s+void\s+Main\(")
        self.assertIn("TrayMutexName", main)
        self.assertNotIn(
            "GetProcessesByName",
            main,
            "single-instance di Main() tidak boleh memindai nama proses",
        )

    def test_flag_hanya_ditulis_di_quit_disengaja(self) -> None:
        """MarkIntentionalExit hanya boleh dipanggil dari Quit(true).

        Kalau ditulis lebih dari sekali (misal juga saat start tray), maka
        tray yang sedang hidup akan terlihat "mau berhenti" oleh guardian.
        """
        calls = re.findall(r"MarkIntentionalExit\(\)\s*;", self.cs)
        self.assertEqual(
            len(calls),
            1,
            "MarkIntentionalExit harus dipanggil tepat satu kali",
        )
        # dan pemanggilnya harus dari Quit(bool) dengan syarat disengaja
        self.assertRegex(
            self.cs,
            r"Quit\(bool\s+\w+\)\s*\{\s*if\s*\(\s*\w+\s*\)\s*MarkIntentionalExit\(\)",
            "MarkIntentionalExit harus dijalankan hanya saat disengaja=true",
        )

    def test_relaunch_quit_tanpa_flag(self) -> None:
        """'Jalankan ulang tray' harus Quit(false).

        Kalau wrote flag, guardian akan mengira user sudah minta berhenti
        tepat setelah user justru meminta tray hidup lagi.
        """
        # Harus DI DALAM RelaunchTray, bukan sekadar ada di file mana pun.
        # Kalau Quit(false) hilang dari sana, jalur "Jalankan ulang tray" akan
        # menandai exit disengaja dan guardian akan salah thinking user mau stop.
        body = _method_body(self.cs, r"private\s+static\s+void\s+RelaunchTray\(\)")
        self.assertRegex(
            body,
            r"Quit\(false\)",
            "RelaunchTray harus keluar dengan Quit(false) supaya guardian "
            "tidak mengira user sengaja keluar",
        )
        # Dan harus membuang penanda basi sebelum start tray baru
        self.assertIn(
            "ClearIntentionalExit()",
            body,
            "RelaunchTray harus membuang penanda exit disengaja yang basi",
        )

    def test_guardian_menghormati_penanda(self) -> None:
        """Guardian harus mengecek penanda sebelum menghidupkan tray."""
        body = _method_body(self.cs, r"private\s+static\s+void\s+RunGuardian\(\)")
        self.assertIn("IsIntentionalExit()", body)
        # penanda harus diperiksa SEBELUM memulai proses tray baru
        m = re.search(r"Process\.Start|new\s+ProcessStartInfo", body)
        self.assertIsNotNone(m, "guardian tidak memulai proses tray sama sekali")
        self.assertLess(
            body.index("IsIntentionalExit()"),
            m.start(),
            "guardian harus mengecek penanda sebelum memulai tray",
        )

    def test_flag_dibuang_ketika_tray_hidup(self) -> None:
        """Tray yang hidup harus membersihkan penanda dari sesi sebelumnya.

        Kalau tidak, guardian akan sees penanda basi dan tidak pernah
        memulihkan tray setelah crash berikutnya.
        """
        self.assertIn("ClearIntentionalExit()", self.cs)


class TestInstallerGuardian(unittest.TestCase):
    """Guardian harus dipasang installer: HKCU Run + dijalankan di sesi ini."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.repo = _find_repo()
        cls.ps1 = (cls.repo / INSTALLER).read_text(encoding="utf-8")

    def test_guardian_didaftarkan_di_hkcu_run(self) -> None:
        """Guardian harus autostart tanpa butuh admin (HKCU, bukan HKLM)."""
        self.assertIn("CykeoRfidBridgeGuardian", self.ps1)
        m = re.search(r"HKCU:[^\n]*\\Run", self.ps1)
        self.assertIsNotNone(m, "tidak ada HKCU Run untuk guardian")

    def test_guardian_argumen_watchdog(self) -> None:
        """Nilai registry harus menyertakan --watchdog."""
        m = re.search(r"\$gValue\s*=\s*(.+)", self.ps1)
        self.assertIsNotNone(m, "tidak ada penetapan $gValue untuk guardian")
        self.assertIn("--watchdog", m.group(1))

    def test_guardian_dijalankan_di_sesi_ini(self) -> None:
        """Installer harus menjalankan guardian sekarang juga.

        Cuma menulis registry berarti user harus logoff dulu sebelum
        guardian hidup - persis pengalaman 'harus restart PC'.
        """
        self.assertRegex(
            self.ps1,
            r"Start-Process[^\n]*--watchdog|Start-Process[\s\S]{0,200}WatchdogArg",
        )

    def test_guardian_ok_hanya_true_kalau_terdaftar(self) -> None:
        """$guardianOk harus di-set True hanya setelah registry benar-benar ditulis."""
        self.assertIn("$guardianOk = $true", self.ps1)
        m = re.search(r"\$guardianOk\s*=\s*\$true", self.ps1)
        self.assertIsNotNone(m)
        sebelum = self.ps1[: m.start()]
        # registry harus ditulis dulu
        self.assertTrue(
            "Set-ItemProperty" in sebelum,
            "$guardianOk di-set True tanpa menulis nilai registry dulu",
        )
        # lalu dibaca balik dan DIBANDINGKAN, bukan diasumsikan berhasil.
        # Ini bentuk paling lemah dari flag: "tidak ada error" bukan bukti.
        self.assertRegex(
            sebelum,
            r"Get-ItemProperty[\s\S]{0,200}?if\s*\(\s*\$gBack\s*-eq\s*\$gValue\s*\)",
            "$guardianOk harus di-set hanya setelah nilai registry dibaca balik "
            "dan cocok; kalau tidak, tulis registry bisa gagal diam-diam",
        )

    def test_status_guardian_ditampilkan(self) -> None:
        """Status guardian harus muncul di laporan installer."""
        self.assertIn("Guardian      :", self.ps1)
        self.assertIn("$guardianOk", self.ps1)


class TestSourceHygiene(unittest.TestCase):
    """Syarat proyek: file .cs dan .ps1 harus ASCII-only."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.repo = _find_repo()

    def test_tidak_ada_karakter_non_ascii(self) -> None:
        for rel in (INSTALLER, TRAY):
            raw = (self.repo / rel).read_bytes()
            non_ascii = [b for b in raw if b > 0x7F]
            self.assertEqual(
                non_ascii,
                [],
                "%s punya %d byte non-ASCII" % (rel, len(non_ascii)),
            )

    def test_kurung_kurawal_seimbang(self) -> None:
        for rel in (INSTALLER, TRAY):
            text = (self.repo / rel).read_text(encoding="utf-8")
            self.assertEqual(
                text.count("{"),
                text.count("}"),
                "kurung kurawal tidak seimbang di %s" % rel,
            )


if __name__ == "__main__":
    unittest.main()
