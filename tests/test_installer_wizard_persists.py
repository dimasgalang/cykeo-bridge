r"""Regression test: wizard install_cykeo.ps1 harus tersimpan di InstallDir.

Gejala di PC teknisi (2026-10-21): menu "Configure Reader" di tray agent
menampilkan dialog

    install_cykeo.ps1 tidak ditemukan di:
      C:\Users\user\AppData\Local\CykeoRfidBridge

Tray mencari wizard di dua tempat:

    <install dir>\install_cykeo.ps1
    <install dir>\staging\install_cykeo.ps1

Tapi installer hanya menyalin helper + 2 DLL ke InstallDir ($need). Wizard
tidak pernah ikut, jadi setelah instalasi Selesai tidak ada cara configuring
reader tanpa menjalankan installer lagi.

Test ini mengunci dua hal:

1. install_cykeo.ps1 ada di daftar $need sehingga ikut tercopy.
2. Loop copy tidak gagal kalau wizard dijalankan dari InstallDir sendiri
   (source == destination -> Copy-Item melempar error di PowerShell).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path("/home/hermes/workspace/cykeo-installer-release")
PS1 = REPO / "install_cykeo.ps1"
TRAY_CS = REPO / "tray" / "TrayAgent.cs"
CONFIGURE_CS = REPO / "tray" / "ConfigureDialog.cs"
CSPROJ = REPO / "tray" / "CykeoTrayAgent.csproj"


def _src() -> str:
    assert PS1.is_file(), f"{PS1} tidak ada"
    return PS1.read_text(encoding="utf-8", errors="replace")


def test_install_cykeo_ps1_masuk_daftar_need():
    """Wizard harus ikut disalin ke InstallDir, bukan cuma helper + DLL."""
    src = _src()
    m = re.search(r"^\$need\s*=\s*@\((.*?)\)\s*$", src, re.M)
    assert m, "baris $need = @(...) tidak ditemukan di install_cykeo.ps1"

    isi = m.group(1)
    assert "'install_cykeo.ps1'" in isi, (
        "install_cykeo.ps1 tidak ada di $need - wizard tidak akan tersalin "
        "ke InstallDir dan menu 'Configure Reader' akan gagal dengan "
        "'install_cykeo.ps1 tidak ditemukan'"
    )
    for wajib in ("GReaderApi.dll", "Newtonsoft.Json.dll"):
        assert wajib in isi, f"{wajib} hilang dari $need"


def _blok_loop_need(src: str) -> list[str]:
    """Kembalikan isi semua blok 'foreach ($f in $need) { ... }'.

    Dicari per-blok dengan brace matching, bukan regex greedy, karena ada
    DUA loop dengan header yang sama: satu mengecek keberadaan file
    ($missing), satu lagi menyalin (Copy-Item).
    """
    import re as _re

    hasil: list[str] = []
    for m in _re.finditer(r"foreach\s*\(\$f\s+in\s+\$need\)\s*\{", src):
        i = m.end() - 1  # posisi '{'
        depth = 0
        j = i
        while j < len(src):
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        hasil.append(src[i + 1 : j])
    return hasil


def test_loop_copy_skip_jika_source_sama_dengan_destination():
    """Copy-Item ke file yang sama melempar error -> harus di-skip eksplisit."""
    src = _src()

    blok_copy = [b for b in _blok_loop_need(src) if "Copy-Item" in b]
    assert blok_copy, (
        "loop copy 'foreach ($f in $need) { ... Copy-Item ... }' tidak "
        "ditemukan di install_cykeo.ps1"
    )

    for blok in blok_copy:
        assert "GetFullPath" in blok and "continue" in blok, (
            "loop copy tidak membersihkan source == destination - Copy-Item ke "
            "file yang sama melempar error di PowerShell dan wizard yang "
            "dijalankan dari InstallDir akan gagal"
        )


def test_tray_konfigurasi_memakai_wizard_python_elevated():
    """v1.6.23: konfigurasi lewat wizard Python + elevasi UAC.

    Menu lama "Jalankan wizard konfigurasi" memanggil install_cykeo.ps1
    secara interaktif, dan itu selalu error di PC teknisi: install_cykeo.ps1
    adalah installer (menyalin paket, memasang bridge, menulis ke %ProgramData%),
    bukan wizard konfigurasi. Dipanggil dari tray tanpa elevasi hasilnya
    error dan user tidak pernah bisa menyelesaikan wizard.

    Jalur yang benar (v1.6.23): wizard milik bridge sendiri (cykeo_bridge
    wizard) yang menulis config.json ke %ProgramData% - WAJIB elevated, jadi
    tray memicu dengan verb "runas" supaya Windows menampilkan UAC.

    Catatan v1.6.24: wizard Python itu DIGANTIKAN GUI native WinForms
    (ConfigureForm). Test ini tetap mengunci bagian yang belum berubah:
    menu lama tidak boleh dirender, menu baru wajib ada, dan pemicu
    konfigurasi harus minta elevasi UAC.
    """
    assert TRAY_CS.is_file(), f"{TRAY_CS} tidak ada"
    src = TRAY_CS.read_text(encoding="utf-8", errors="replace")

    # Menu lama harus benar-benar tidak dirender lagi.
    assert not re.search(
        r'_menu\.Items\.Add\(\s*"Jalankan wizard konfigurasi"', src
    ), (
        "menu 'Jalankan wizard konfigurasi' masih dirender - itu menu yang "
        "bikin error di PC teknisi"
    )

    # Menu baru harus ada.
    assert '"Konfigurasi reader (minta izin admin)"' in src, (
        "menu konfigurasi baru tidak ada di TrayAgent.cs"
    )

    # Jalur runtime harus lewat launcher, bukan "-m cykeo_bridge". Interpreter
    # embeddable punya python312._pth -> isolated mode -> sys.path diabaikan
    # -> "python -m <module>" selalu gagal dengan "No module named".
    # v1.6.24: jalur ini tidak lagi dipakai untuk wizard (GUI native),
    # tapi bridge_launcher.py tetap dipakai sebagai fallback runtime bridge.
    assert "bridge_launcher.py" in src, (
        "konfigurasi harus lewat bridge_launcher.py - 'python -m cykeo_bridge' "
        "selalu gagal di runtime embeddable"
    )

    # WAJIB elevasi: config.json ditulis ke %ProgramData% yang butuh admin.
    assert 'Verb = "runas"' in src, (
        "wizard konfigurasi tidak minta elevasi UAC - tanpa 'runas' wizard "
        "gagal menyimpan ke %ProgramData%\\CykeoBridge\\config.json"
    )

    # Batal di dialog UAC bukan error.
    assert "1223" in src, (
        "pembatalan UAC (ERROR_CANCELLED 1223) harus ditangani diam-diam, "
        "bukan ditampilkan sebagai error"
    )

    # Fungsi konfigurasi tidak boleh memanggil install_cykeo.ps1 lagi.
    m = re.search(
        r"private static void RunConfigureWizard\(\)(.*?)\n        \}", src, re.S
    )
    assert m, "RunConfigureWizard() tidak ditemukan di TrayAgent.cs"
    kode = "\n".join(
        baris for baris in m.group(1).splitlines()
        if not baris.strip().startswith("//")
    )
    assert not re.search(r'"[^"]*install_cykeo\.ps1[^"]*"', kode), (
        "RunConfigureWizard() masih memanggil install_cykeo.ps1 - itu "
        "installer, bukan wizard konfigurasi"
    )


def test_wizard_konfigurasi_guiform_native_v1624():
    """v1.6.24: wizard konfigurasi jadi GUI NATIVE WinForms.

    Wizard Python (tkinter) digantikan form WinForms supaya PC teknisi
    tidak butuh Python terpasang untuk membuka konfigurasi. Formnya
    menyatu di tray agent yang sama, dipanggil lewat mode `--wizard`,
    dan diproses SEBELUM mutex tray (kalau tidak, tray yang sudah jalan
    akan menolak dengan "tray sudah berjalan" dan form tidak pernah
    muncul).

    Yang dikunci test ini:
    1. File form native ada dan class-nya ConfigureForm.
    2. Form dikompilasi bersama tray agent (kalau tidak ikut, formnya
       tidak ada di PC user).
    3. Mode --wizard diproses sebelum mutex tray.
    4. Wizard dijalankan elevated (UAC) karena menulis ke %ProgramData%.
    5. Tidak ada lagi pemanggilan wizard Python untuk form konfigurasi.
    """
    assert CONFIGURE_CS.is_file(), f"{CONFIGURE_CS} tidak ada"
    form = CONFIGURE_CS.read_text(encoding="utf-8", errors="replace")
    src = TRAY_CS.read_text(encoding="utf-8", errors="replace")

    # 1. Class form native.
    assert re.search(r"class\s+ConfigureForm\b", form), (
        "class ConfigureForm tidak ada di ConfigureDialog.cs"
    )
    assert re.search(r"new\s+ConfigureForm\s*\(", form), (
        "ConfigureForm harus benar-benar di-instantiate - kalau tidak, "
        "formnya tidak pernah dibuka"
    )
    assert "Form" in form and "MessageBox" in form, (
        "ConfigureDialog.cs harus form WinForms sungguhan, bukan stub"
    )

    # 2. Form ikut dikompilasi oleh csproj tray agent.
    assert CSPROJ.is_file(), f"{CSPROJ} tidak ada"
    proj = CSPROJ.read_text(encoding="utf-8", errors="replace")
    assert "ConfigureDialog.cs" in proj, (
        "ConfigureDialog.cs tidak ikut dikompilasi - form wizard tidak "
        "akan ada di PC user"
    )

    # 3. Mode --wizard ada dan diproses sebelum mutex tray.
    assert "--wizard" in src, "mode --wizard tidak ada di TrayAgent.cs"
    pos_flag = src.find('"--wizard"')
    pos_mutex = src.find("Mutex")
    assert pos_flag >= 0 and pos_mutex >= 0, (
        "flag --wizard atau mutex tray tidak ditemukan"
    )
    assert pos_flag < pos_mutex, (
        "mode --wizard harus diproses SEBELUM mutex tray - kalau tidak, "
        "tray yang sudah jalan menolak dan form wizard tidak muncul"
    )

    # 4. Wizard elevated lewat UAC.
    assert 'Verb = "runas"' in src, (
        "wizard native harus dijalankan elevated (runas) karena menulis "
        "ke %ProgramData%"
    )

    # 5. Tidak ada pemanggilan wizard Python untuk form konfigurasi.
    #    RunConfigureWizard() harus memanggil executable tray itu sendiri
    #    dengan mode --wizard, BUKAN menjalankan Python/bridge_launcher.
    m = re.search(
        r"private static void RunConfigureWizard\(\)(.*?)\n        \}", src, re.S
    )
    assert m, "RunConfigureWizard() tidak ditemukan di TrayAgent.cs"
    kode = m.group(1)
    kode = "\n".join(
        b for b in kode.splitlines() if not b.strip().startswith("//")
    )
    assert "--wizard" in kode, (
        "RunConfigureWizard() harus memanggil executable tray sendiri "
        "dengan mode --wizard"
    )
    # Larangan keras: tidak boleh menjalankan Python untuk membuka form.
    # Pola sengaja longgar (cukup satu penanda), supaya variations kecil
    # di cara pemanggilan tidak lolos dari gate.
    for pola, pesan in (
        (r"cykeo_bridge",
         "RunConfigureWizard() menjalankan modul Python cykeo_bridge - form "
         "harus native WinForms, bukan Python"),
        (r"bridge_launcher",
         "RunConfigureWizard() memakai bridge_launcher.py - form harus "
         "native WinForms, bukan Python"),
        (r"install_cykeo\.ps1",
         "RunConfigureWizard() memanggil install_cykeo.ps1 - itu installer, "
         "bukan wizard konfigurasi"),
    ):
        assert not re.search(pola, kode), pesan
    assert "ConfigureForm" in form, (
        "form native harus ada di ConfigureDialog.cs"
    )
