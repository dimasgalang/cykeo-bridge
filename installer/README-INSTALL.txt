Cykeo RFID Bridge - paket installer
====================================

Paket ini untuk PC scanner Windows yang memakai reader Cykeo CK-D5.
PENTING: helper dan SDK sengaja dibangun 32-bit (win-x86) karena
GReaderApi.dll production juga 32-bit. Jangan ganti ke x64.

LANGKAH 1 - Smoke test helper (tanpa server, tanpa instalasi)
-------------------------------------------------------------

  1. Buka PowerShell di folder ini
  2. Jalankan:

         python .\smoke_test_helper.py

     Kalau reader sudah tersambung dan Anda tahu portnya:

         python .\smoke_test_helper.py COM3

  Smoke test membuktikan:
    - .NET Framework 4.8 tersedia
    - cykeo-helper.exe bisa di-load (GReaderApi.dll cocok, bitness cocok)
    - handshake stdio JSON berjalan
    - port COM terdeteksi
    - (opsional) koneksi ke reader berhasil

  Selalu baca bagian "HASIL" di akhir. Kalau "LOLOS SEMUA PEMERIKSAAN",
  helper siap dipakai. Kalau gagal, bagian "Diagnosis" memberi penyebab.

  Versi CMD jika Python tidak ada:  .\smoke_test_cykeo.bat COM3

LANGKAH 2 - Instalasi penuh
---------------------------

  1. Pastikan Python 3.11 terpasang di PC scanner.
  2. Build paket exe (butuh PyInstaller, jalankan di mesin dev):

         pip install pyinstaller
         build_installer.bat

     build_installer.bat akan membuat folder dist\cykeo_bridge lalu
     menyalinnya ke paket ini.
  3. Jalankan wizard installer:

         powershell -ExecutionPolicy Bypass -File .\install_cykeo.ps1

     Wizard menanyakan: URL server, reader code, API key, mode, COM port,
     baudrate, dan (opsional) initParam.

CATATAN PENTING
---------------

  - TIDAK ada license key. Nilai hex yang muncul di guide Cykeo adalah
    initParam, yaitu parameter handshake protokol, bukan kredensial
    per-device. CK-D5 tidak butuh license key.
  - Baud production = 115200 (dari OpenSerial("<COM>:115200", 60)).
  - Timeout koneksi = 60 detik, sesuai production.
  - Port COM harus autodetect. Panduan resmi hanya memberi contoh COM3;
    unit Anda bisa COM lain. Pakai smoke test untuk memastikannya.
  - GReaderApi.dll adalah SDK Cykeo proprietary. HAK REDISTRIBUSI BELUM
    JELAS - konfirmasi ke vendor sebelum mendistribusikan paket ini
    di luar lingkungan internal.
  - Inventory bersifat read-only. Bridge tidak pernah menulis atau
    mengubah setting reader.

ISI FOLDER
----------

  cykeo-helper.exe          helper .NET 32-bit (bridge ke GReaderApi.dll)
  cykeo-helper.exe.config   konfigurasi runtime (.NET Framework 4.8)
  GReaderApi.dll            SDK Cykeo proprietary, 32-bit
  Newtonsoft.Json.dll       dependency helper, 32-bit
  bridge\cykeo_bridge\     source bridge agent (Python 3.11)
  install_cykeo.ps1         wizard installer
  smoke_test_helper.py      smoke test helper tanpa server
  smoke_test_cykeo.bat      versi CMD dari smoke test
  config.example.json       contoh konfigurasi

PERHATIAN: WINDOWS DEFENDER MENGKARANTINA cykeo-helper.exe
------------------------------------------------------------
Saat smoke test pertama, Defender menandai helper sebagai:
    Trojan:Win32/Bearfoos.B!ml  (ThreatID 2147731849)
dan menghapus file cykeo-helper.exe dari folder.

Ini FALSE POSITIVE. Bukti:
  - Ukuran hanya 13.824 byte (helper terkecil yang bisa mungkin)
  - Tidak ada kode jaringan (TcpClient/WebClient/HttpWebRequest = 0)
  - Tidak ada akses Registry, tidak ada Process.Start
  - Tidak ada injeksi memori (VirtualAlloc/VirtualProtect = 0)
  - Tidak ada pemanggilan unmanaged (P/Invoke) sama sekali
  - Hanya merujuk mscoree.dll = runtime .NET bawaan Windows
  - Isinya: baca stdin, tulis stdout JSON, panggil GReaderApi.dll

Yang memicu deteksi adalah kombinasi file .NET kecil + parser JSON +
akses perangkat COM serial. fournie software houseware-scanforall
Perangkat lunak antivirus dari pihak vendor sering salah mendeteksi

CARA MENGATASI (pilih salah satu, urut dari yang paling aman):
  A. Izinkan folder ini saja (paling aman, lokal):
     md C:\cykeo_allow
     copy cykeo-helper.exe C:\cykeo_allow\
     powershell -Command "Add-MpPreference -ExclusionPath 'C:\cykeo_allow'"
  B. Tanda tangan file (butuh sertifikat kode-signing):
     signtool sign /fd SHA256 /a /t http://timestamp.digicert.com cykeo-helper.exe
     ini solusi permanen dan recommended untuk distribusi ke user.
  C. Jalankan smoke test dari Windows Defender quarantine restore,
     lalu verifikasi manual sebelum dipakai.

JANGAN menonaktifkan antivirus seluruhnya.
