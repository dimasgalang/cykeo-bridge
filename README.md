# Cykeo RFID Bridge Agent

Bridge agent untuk **PC client Windows** yang menghubungkan reader RFID
(Cykeo CK D5, atau mode `zebra`) ke **server RFID Scanner**.

Bridge ini **read-only terhadap device**: hanya membaca output reader lewat
RS232, tidak pernah menulis konfigurasi reader. Komunikasinya **hanya keluar**
(POST HTTP/HTTPS ke server) — bridge **tidak membuka port listener**, dan
server **tidak membuka port baru**.

> Kontrak payload & normalisasi EPC mengikuti `../CONTRACT.md`.
> Source of truth ada di sana, bukan di README ini.

---

## 1. Arsitektur singkat

```
   Zebra FX7500 (mode zebra)          Cykeo CK D5 (mode cykeo)
            |                                   |
            |  (device sudah terkonfigurasi)   |  USB/RS232 + GReader.dll
            v                                   v
        +---------------------------------------------------+
        |            Cykeo RFID Bridge Agent                 |
        |  device adapter -> normalisasi (dedup lokal) ->     |
        |  antrean persisted (JSONL) -> POST batch + retry    |
        +---------------------------------------------------+
                              |  HTTPS keluar (X-Reader-Api-Key)
                              v
              POST /api/v1/rfid/readers/{reader_code}/events
```

Mode `zebra` dan `cykeo` memakai **kontrak payload yang sama**; perbedaan
hanya di lapisan adapter device.

---

## 2. Yang bisa dites TANPA hardware

| Komponen | Butuh device? | Cara uji |
|---|---|---|
| `epc.normalize()` | tidak | `tests/test_epc.py` |
| Antrean persisted (JSONL) | tidak | `tests/test_queue.py` |
| Config load/save/validasi | tidak | `tests/test_config.py` |
| `devices/simulator.py` | tidak | `tests/test_devices.py` |
| HTTP client + payload | tidak | `tests/test_http_client.py` + `smoke_test.py` |
| `devices/serial_cykeo.py` | **ya** (Windows + `GReader.dll`) | `cykeo_bridge check` |

Semua test jalan di Linux/macOS karena adapter device di-inject.

Jalankan test:

```bash
python3 -m unittest discover -s tests -t .
```

Smoke test end-to-end (simulator -> antrean -> HTTP, ke server dummy loopback):

```bash
python3 smoke_test.py
```

> Smoke test **tidak pernah** menyentuh server RFID produksi.

---

## 3. Install di Windows client

### 3a. Build (di mesin Windows, bukan server Linux)

```bat
build_windows.bat
```

Script ini membuat `dist\cykeo_bridge.exe` (PyInstaller one-file).

### 3b. Buat installer `.exe` (opsional, perlu Inno Setup 6)

```bat
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\bridge_agent.iss
```

Hasil: `Output\CykeoBridgeSetup.exe`.

### 3c. Alternatif tanpa `.exe` installer

Copy folder proyek + `install_agent.ps1`:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_agent.ps1
```

---

## 4. Konfigurasi (first run)

Setelah install, buka **Configure Bridge (Wizard)** atau jalankan:

```bat
cykeo_bridge.exe wizard
```

Wizard meminta:

| Field | Keterangan | Contoh |
|---|---|---|
| `server_url` | **IP server RFID** + port | `http://192.168.1.229` |
| `reader_code` | Kode reader di server | `CYKEO-01` |
| `api_key` | API key reader (dari server) | `<isi-dari-server>` |
| `mode` | `zebra` / `cykeo` / `simulator` | `cykeo` |
| `com_port` | Port serial Cykeo | `COM12` |
| `baudrate` | Baudrate | `115200` |
| `helper_path` | Path `cykeo-helper.exe` (kosong = ikut folder bridge) | `<auto>` |
| `sdk_dir` | Folder SDK vendor berisi `GReaderApi.dll` | `<auto>` |
| `init_param` | Parameter handshake Cykeo (hex, opsional) | `<default resmi>` |

Wizard juga menanyakan apakah agent didaftarkan **auto-start** saat Windows
logon (startup task per-user).

Config disimpan di (lihat `cykeo_bridge config-path`):

```
%APPDATA%\CykeoBridge\config.json     (Windows)
~/.local/share/cykeo-bridge/config.json (Linux/macOS, untuk QA)
```

**Keamanan:** `api_key` dan `init_param` tidak pernah ditulis ke log
(filter `SecretFilter` di `logging_setup.py` akan masking). Jangan pernah
commit config berisi API key ke git.

---

## 5. Menjalankan & mengecek

```bat
cykeo_bridge.exe run      # jalankan agent (loop baca + kirim)
cykeo_bridge.exe check    # validasi config + status device/SDK
cykeo_bridge.exe config-path
```

Arial flags umum untuk `run`:

| Flag | Fungsi |
|---|---|
| `--simulate` | Paksa mode simulator (fixture bawaan) |
| `--fixture <path>` | Pakai fixture JSON/JSONL untuk simulator |
| `--print-payload` | Cetak payload tiap batch (tanpa API key) |
| `--max-iterations N` | Berhenti setelah N iterasi (smoke test) |
| `-c/--config <path>` | Path config lain |

---

## 6. Perilaku penting

- **Normalisasi EPC** hanya dipakai untuk **dedup lokal**; payload yang dikirim
  ke server tetap **EPC mentah apa adanya** dari device. Normalisasi final
  dilakukan **di server** (Fase 1), supaya satu sumber kebenaran.
- **Antrean persisted (JSONL)** — kalau server offline atau PC mati, tag
  disimpan dan dikirim setelah online lagi. Batch yang sukses di-commit
  (dihapus) dari antrean; yang ditolak server (422) dipindah ke file
  `.rejected.jsonl` agar bisa diperiksa.
- **HTTP 401** (API key salah) diperlakukan **fatal**: event **tidak** di-commit
  dan agent berhenti dengan pesan jelas, supaya operator memperbaiki key.
- **Retry** memakai exponential backoff + jitter, dibatasi `backoff_max`.
- **Shutdown** bersih (SIGTERM/SIGINT / Ctrl+C) supaya antrean tidak korup.

---

## 7. Troubleshooting

| Gejala | Penyebab & solusi |
|---|---|
| `SDK Cykeo (GReader.dll) hanya untuk Windows` | Lihat tabel "Butuh device?" di atas. Pakai `--simulate` untuk uji. |
| `API key ditolak server (HTTP 401)` | `api_key` salah / tidak cocok dengan `config('rfid.reader_api_key')` di server. Perbaiki lalu restart. |
| `COM port` tidak bisa dibuka | Reader tidak tercolok, atau COM dipakai aplikasi lain (Device Manager). |
| Helper gagal start | `cykeo-helper.exe` atau `GReaderApi.dll` tidak ada. Jalankan `install_cykeo.ps1`. |
| `initParam` tidak diterima | Nilai handshake hex opsional. Kosongkan dulu; CK-D5 tidak butuh license key. |
| Event terkirim tapi `REFERENCE_NOT_FOUND` | EPC dari device tidak ada di `reference_rfid` server (lihat `../CONTRACT.md` §2). |
| Startup task tidak jalan | Cek `schtasks /Query /TN CykeoRfidBridge`. Wizard bisa didaftarkan ulang. |

---

## 8. Yang belum terverifikasi (blocker Fase 4)

Bridge **belum pernah** diuji dengan unit Cykeo CK-D5 fisik. Yang perlu diuji
(butuh hardware asli):

- Helper `cykeo-helper.exe` benar-benar load `GReaderApi.dll` di Windows dan
  `.NET Framework 4.8` tersedia.
- `OpenSerial("<COM>:115200", 60)` → `StartInventory` benar-benar mengembalikan
  event pada CK-D5 sungguhan.
- Filter `LogBaseEpcInfo.Result == 0` — apakah reader produksi selalu mengirim
  `Result == 0` untuk tag valid, atau ada kode lain yang harus dipetakan.
- **Port COM aktual** unit PC Test (production contoh `COM3`).
- Jumlah antena fisik reader.

Yang **sudah** terverifikasi tanpa hardware (lihat `../CONTRACT.md` §6.1):
alur API production sudah dikonfirmasi dengan decompile assembly
`FlexiAudit.Infrastructure.dll` — sehingga nama simbol/enum tidak lagi
diasumsikan. Adapter `serial_cykeo.py` (ctypes) adalah eksperimen lama dan
TIDAK dipakai di mode `cykeo`.

Semua logika **di luar** adapter device (normalisasi, antrean, HTTP, config,
autostart, protokol stdio helper) sudah ter-cover test — 176 test.
