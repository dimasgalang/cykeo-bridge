"""Regression test untuk lokasi config: %ProgramData% (v1.6.20+).

Konteks. Keluhan v1.6.18: "api_key kosong" masih muncul padahal field API key
sudah ada di wizard installer, sementara PC test jalan di v1.6.17.

Akar masalahnya bukan bridge dan bukan field yang hilang. Tiga komponen
menulis/membaca file yang sama ke tempat berbeda:

    installer (install_cykeo.ps1, CykeoSetupGui.cs) -> %APPDATA%
    tray      (TrayAgent.cs)                         -> %APPDATA%
    bridge    (config.py::_default_dir)              -> %ProgramData%   <-- menyimpang

Bridge adalah satu-satunya yang benar-benar membaca config saat `run`, dan
dia mencari di folder yang tidak pernah diisi wizard. Jadi user mengisi API
key dengan benar, wizard bilang "config ditulis", tray tetap menampilkan
"Config : belum dibuat", lalu bridge restart 115x dengan "api_key kosong".
Semua komponen terlihat hijau; tidak ada yang salah kecuali satu hal: tidak
ada yang pernah mengecek apakah mereka menulis ke FILE YANG SAMA.

v1.6.19: bridge diubah ke %APPDATA% karena wizard jalan tanpa elevasi.
v1.6.20: BALIK ke %ProgramData% - permintaan eksplisit "config dicopy ke
folder %ProgramData% saja", dan wizard v1.6.20 tetap jalan elevated lewat
installer sehingga hak tulis bukan masalah lagi. Semua komponen (installer,
tray, bridge) sekarang menunjuk %ProgramData%\\CykeoBridge.

Yang dikunci test ini:

- Bridge harus resolve ke %ProgramData%\\CykeoBridge.
- Resolusi diuji lewat environment yang benar-benar disimulasikan, bukan
  lewat pembacaan teks source saja.
- Sumber installer dan tray harus tetap menunjuk folder yang sama.
- Kalau nanti ada yang memindahkan bridge ke %APPDATA% atau tray ke
  %APPDATA%, test ini harus gagal.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BRIDGE = REPO / "cykeo_bridge"
INSTALLER_REPO = Path("/home/hermes/workspace/cykeo-installer-release")

EXPECTED_FOLDER = "CykeoBridge"


def run_snippet(body: str) -> str:
    """Jalankan kode bridge di subprocess dengan os.name dipaksa 'nt'.

    Menguji perilaku nyata, bukan teks source: kalau _default_dir diubah ke
    path lain, hasil di sini ikut berubah.
    """
    program = (
        "import os, sys\n"
        "os.name = 'nt'\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "os.environ['APPDATA'] = r'C:\\Users\\Tester\\AppData\\Roaming'\n"
        "os.environ['ProgramData'] = r'C:\\ProgramData'\n"
        "os.environ.pop('RFID_BRIDGE_CONFIG', None)\n"
        + body
    )
    proc = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"subprocess gagal:\n{proc.stderr}"
    return proc.stdout


def test_default_dir_pakai_programdata_bukan_appdata():
    """Bridge menunjuk %ProgramData%, dan tidak lagi %APPDATA%."""
    src = (BRIDGE / "config.py").read_text(encoding="utf-8")
    body = src.split("def _default_dir", 1)[1].split("\ndef ", 1)[0]
    # Buang docstring utuh (dapat multi-baris), lalu buang komentar.
    parts = body.split('"""')
    code = parts[2] if len(parts) > 2 else body
    code = "\n".join(
        line for line in code.splitlines()
        if not line.strip().startswith("#")
    )
    assert "ProgramData" in code, (
        "_default_dir tidak lagi memakai ProgramData - bridge akan mencari "
        "config di folder yang tidak diisi installer atau tray"
    )
    assert "APPDATA" not in code, (
        "_default_dir masih memakai APPDATA. Lokasi itu dipakai v1.6.19 "
        "saja; sejak v1.6.20 semua komponen menunjuk %ProgramData%."
    )


def test_resolusi_riil_di_lingkungan_windows():
    """config.json harus benar-benar resolve ke ProgramData."""
    out = run_snippet(
        "from cykeo_bridge.config import _default_dir, default_config_path\n"
        "print('DIR=' + str(_default_dir()))\n"
        "print('CFG=' + str(default_config_path()))\n"
    )
    values = dict(
        line.split("=", 1) for line in out.strip().splitlines() if "=" in line
    )
    cfg = values.get("CFG", "")
    assert "ProgramData" in cfg, (
        f"config.json resolve ke {cfg!r}, harusnya di ProgramData"
    )
    assert "Roaming" not in cfg, f"config.json masih di AppData: {cfg!r}"
    assert cfg.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] == "config.json", cfg
    assert EXPECTED_FOLDER in cfg, cfg


def test_env_override_masih_dihormati():
    """RFID_BRIDGE_CONFIG tetap menang - jalur manual untuk teknisi."""
    program = (
        "import os, sys\n"
        "os.name = 'nt'\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "os.environ['APPDATA'] = r'C:\\Users\\Tester\\AppData\\Roaming'\n"
        "os.environ['RFID_BRIDGE_CONFIG'] = r'D:\\custom\\my.json'\n"
        "from cykeo_bridge.config import default_config_path\n"
        "print('CFG=' + str(default_config_path()))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "D:\\custom\\my.json" in proc.stdout, (
        f"env override diabaikan: {proc.stdout!r}"
    )


def test_wizard_dan_run_tulis_file_yang_sama():
    """Wizard harus menulis ke default yang sama dengan yang dibaca `run`."""
    out = run_snippet(
        "from cykeo_bridge.wizard import default_config_path as wiz\n"
        "from cykeo_bridge.config import default_config_path as run\n"
        "print('SAME=' + str(wiz() == run()))\n"
    )
    assert "SAME=True" in out, f"wizard dan run menulis file berbeda!\n{out}"


def test_salinan_paket_identik_dengan_sumber():
    """Salinan di dist/ harus identik, kalau tidak paket jadi basi."""
    copy = (REPO / "dist" / "install_cykeo" / "bridge"
            / "cykeo_bridge" / "config.py")
    if not copy.is_file():
        return
    assert copy.read_text(encoding="utf-8") == (
        BRIDGE / "config.py"
    ).read_text(encoding="utf-8"), (
        "dist/install_cykeo/bridge/cykeo_bridge/config.py beda dengan "
        "sumber - paket akan berisi default path yang sudah usang"
    )


def test_installer_dan_tray_tetap_ke_folder_yang_sama():
    """Installer dan tray harus tetap berbasis %ProgramData%."""
    if not INSTALLER_REPO.is_dir():
        return

    tray = INSTALLER_REPO / "tray" / "TrayAgent.cs"
    if tray.is_file():
        src = tray.read_text(encoding="utf-8", errors="replace")
        assert "CommonApplicationData" in src, (
            "TrayAgent.cs tidak memakai SpecialFolder.CommonApplicationData "
            "lagi - tray dan bridge akan menyimpang"
        )
        assert "SpecialFolder.ApplicationData" not in src, (
            "TrayAgent.cs memakai SpecialFolder.ApplicationData (%APPDATA%) - "
            "tray dan bridge akan menyimpang lagi"
        )

    ps1 = INSTALLER_REPO / "install_cykeo.ps1"
    if ps1.is_file():
        src = ps1.read_text(encoding="utf-8", errors="replace")
        assert "ProgramData" in src, (
            "install_cykeo.ps1 tidak menulis ke %ProgramData%\\CykeoBridge lagi"
        )