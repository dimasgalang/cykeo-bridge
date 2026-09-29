"""
Uji pembaca LOKAL — baca EPC langsung dari reader, tanpa menyentuh server.

Kenapa modul ini ada
--------------------
Gejala yang dilaporkan: dashboard selalu "0 EPC" dan status reader nyangkut di
"testing". Dua kemungkinan yang harus dibedakan sebelum Curiga pada server:

  A. Reader/com helper memang tidak membaca apa pun (hardware/konfigurasi)
  B. Reader membaca dengan baik, tapi tidak pernah sampai ke server

Modul ini menjawab (A) secara lokal. Kalau tag terlihat di sini, layer
hardware + helper + COM sudah terbukti benar, dan itu menyisakan jalur
HTTP sebagai suspect utama. Kalau tidak ada tag, tidak ada gunanya调试
koneksi server dulu.

Prinsip
-------
- TIDAK pernah membuat koneksi HTTP. purposefully tidak ada import http_client
  di modul ini, supaya tidak bisa bocor jadi "kirim diam-diam".
- Config dibaca dari file yang sama dengan agent, jadi wizard yang sama
  menulis konfigurasi yang sama.
- Berhenti sendiri setelah durasi, dan selalu mengembalikan ringkasan
  apa pun yang terjadi (sukses maupun gagal) supaya teknisi bisa
  menyalin hasilnya.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .config import Config, load_config, default_config_path

logger = logging.getLogger("cykeo_bridge.readertest")

#: Hentikan uji otomatis setelah DURASI_AUTOMATIS detik. Default-nya cukup
#: lama untuk operator mengarahkan tag ke antena sambil menahan tombol.
DURASI_AUTOMATIS = 15.0

#: Jeda antar-siklus inventory. Too fast bisa membanjiri antrean; terlalu
#: lambat bikin terasa macet. 0.2sanalog dengan poll default agent.
JEDA_PEMBACAAN = 0.2


@dataclass
class HasilUji:
    """Ringkasan satu kali uji pembaca. Selalu bisa diserialisasi ke teks."""

    ok: bool
    ringkasan: str
    mode: str = ""
    com_port: str = ""
    total_dibaca: int = 0
    epc_unik: int = 0
    daftar_epc: List[str] = field(default_factory=list)
    durasi_detik: float = 0.0
    error: Optional[str] = None
    saran: str = ""

    def as_lines(self) -> List[str]:
        """Baris-baris siap tempel ke chat/email oleh teknisi."""
        lines = [
            "=== UJI PEMBACA LOKAL (tanpa kirim ke server) ===",
            f"Status      : {'BERHASIL' if self.ok else 'GAGAL'}",
            f"Ringkasan   : {self.ringkasan}",
            f"Mode        : {self.mode}",
            f"Port COM    : {self.com_port or '(tidak dipakai)'}",
            f"Durasi      : {self.durasi_detik:.1f} detik",
            f"Total baca  : {self.total_dibaca}",
            f"EPC unik    : {self.epc_unik}",
        ]
        if self.daftar_epc:
            lines.append("EPC terbaca :")
            lines.extend(f"  - {epc}" for epc in self.daftar_epc[:50])
            if len(self.daftar_epc) > 50:
                lines.append(f"  ... dan {len(self.daftar_epc) - 50} EPC lain")
        if self.error:
            lines.append(f"Error       : {self.error}")
        if self.saran:
            lines.append(f"Saran       : {self.saran}")
        lines.append("==============================================")
        return lines


def _panduan_saran(error: str, mode: str, com_port: str) -> str:
    """Saran yang bisa ditindaklanjuti, spesifik per penyebab.

    Sengaja tidak menebak-nebak: tiap cabang mengembalikan tindakan yang
    memang bisa dieksekusi teknisi di depan reader.
    """
    e = error.lower()

    if "port" in e and ("not found" in e or "tidak" in e or "no such" in e):
        return ("Port COM tidak ditemukan. Cek Device Manager: nama port bisa "
                "berubah setelah reader dicabut-dicolok ulang, lalu jalankan "
                "ulang wizard dan pilih port yang muncul.")
    if "access" in e or "denied" in e or "permission" in e:
        return ("Akses ditolak. Jalankan aplikasi sebagai Administrator, atau "
                "tutup program lain yang sedang memakai port COM ini.")
    if "helper" in e or "sdk" in e or "dll" in e:
        return ("Helper/SDK Cykeo tidak termuat. Install ulang dengan "
                "install_cykeo.ps1 supaya helper .NET dan DLL vendor ikut "
                "tersalin.")
    if "baud" in e:
        return "Baudrate tidak cocok. Coba default 115200."

    if mode == "simulator":
        return "Mode simulator aktif — hasil ini bukan pembacaan hardware nyata."

    if com_port:
        return ("Tidak ada tag terbaca. Arahkan RFID tag ke antena reader "
                "(jarak 5-15 cm), pastikan tag dalam keadaan aktif, lalu "
                "coba lagi. Jika tetap 0, ganti tag dengan yang lain untuk "
                "membuktikan apakah masalahnya tag atau antena.")

    return "Tidak ada tag terbaca. Pastikan reader terpasang dan aktif."


def jalankan_uji(config: Config, *, durasi: float = DURASI_AUTOMATIS,
                 on_tag=None) -> HasilUji:
    """Baca tag dari reader selama ``durasi`` detik. Tidak pernah kirim ke server.

    Berhenti otomatis setelah ``durasi`` — tidak ada kondisi di mana perintah ini
    menggantung tanpa batas, karena teknisi membutuhkannya juga sebagai
    pemeriksaan cepat "apakah reader hidup".

    ``on_tag(epc)`` dipanggil untuk setiap tag baru agar GUI bisa menampilkan
    daftar EPC secara langsung.
    """
    from .devices.factory import build_device

    waktu_mulai = time.monotonic()
    semua_epc: List[str] = []
    lihat_epc = set()  # type: set
    total_dibaca = 0

    dasar = dict(mode=config.mode, com_port=config.com_port or "")

    if config.mode == "zebra":
        return HasilUji(
            ok=False,
            ringkasan="Mode zebra tidak bisa dibaca lewat bridge ini.",
            error="MODE_TIDAK_MENDUKUR_UJI",
            saran=("Mode zebra (FX7500) dikelola lewat perangkatnya sendiri. "
                   "Uji lokal ini hanya untuk reader Cykeo. Ganti mode ke "
                   "'cykeo' di wizard, atau 'simulator' untuk menguji jalur "
                   "tanpa hardware."),
            durasi_detik=0.0,
            **dasar,
        )

    try:
        device = build_device(config)
    except Exception as exc:  # noqa: BLE001
        pesan = f"{type(exc).__name__}: {exc}"
        logger.warning("uji-pembaca: gagal membuat adapter - %s", pesan)
        return HasilUji(
            ok=False,
            ringkasan="Reader tidak bisa dibuka.",
            error=pesan,
            saran=_panduan_saran(pesan, config.mode, config.com_port or ""),
            durasi_detik=time.monotonic() - waktu_mulai,
            **dasar,
        )

    error_akhir: Optional[str] = None
    try:
        # DeviceAdapter tidak punya open() terpisah — read_tags() adalah
        # satu-satunya operasi, dan konstruktor adapter yang sudah membuka
        # koneksi ke reader (COM/helper).
        while time.monotonic() - waktu_mulai < durasi:
            for tag in device.read_tags():
                total_dibaca += 1
                epc = tag.epc
                if epc not in lihat_epc:
                    lihat_epc.add(epc)
                    semua_epc.append(epc)
                    if on_tag is not None:
                        on_tag(epc)
            time.sleep(JEDA_PEMBACAAN)
    except Exception as exc:  # noqa: BLE001
        pesan = f"{type(exc).__name__}: {exc}"
        error_akhir = pesan
        logger.warning("uji-pembaca: pembacaan terhenti - %s", pesan)
    finally:
        # Tutup selalu, walau error: port COM yang tertahan akan membuat
        # agent utama gagal connect setelahnya.
        try:
            device.close()
        except Exception:  # noqa: BLE001, S110
            pass

    durasi_riil = time.monotonic() - waktu_mulai
    if error_akhir is not None:
        return HasilUji(
            ok=False,
            ringkasan="Pembacaan terhenti karena error.",
            total_dibaca=total_dibaca,
            epc_unik=len(semua_epc),
            daftar_epc=semua_epc,
            durasi_detik=durasi_riil,
            error=error_akhir,
            saran=_panduan_saran(error_akhir, config.mode, config.com_port or ""),
            **dasar,
        )

    if semua_epc:
        return HasilUji(
            ok=True,
            ringkasan=f"Reader membaca {len(semua_epc)} EPC berbeda "
                      f"({total_dibaca} bacaan).",
            total_dibaca=total_dibaca,
            epc_unik=len(semua_epc),
            daftar_epc=semua_epc,
            durasi_detik=durasi_riil,
            saran=("Reader dan helper terbukti bekerja. Kalau dashboard server "
                   "tetap 0, masalahnya ada di jalur HTTP/API key, bukan di "
                   "hardware."),
            **dasar,
        )

    return HasilUji(
        ok=False,
        ringkasan="Reader terbuka, tapi tidak ada tag yang terbaca.",
        total_dibaca=0,
        durasi_detik=durasi_riil,
        error="TIDAK_ADA_TAG",
        saran=_panduan_saran("TIDAK_ADA_TAG", config.mode, config.com_port or ""),
        **dasar,
    )


def jalankan_uji_dari_file(config_path: Optional[str] = None, *,
                          durasi: float = DURASI_AUTOMATIS,
                          on_tag=None) -> HasilUji:
    """Bantu dipanggil CLI/tray: baca config, lalu jalankan uji."""
    path = Path(config_path) if config_path else default_config_path()
    if not path.exists():
        return HasilUji(
            ok=False,
            ringkasan="Config belum ada.",
            error=f"CONFIG_TIDAK_ADA: {path}",
            saran="Jalankan wizard konfigurasi lebih dulu.",
        )
    config = load_config(path)
    return jalankan_uji(config, durasi=durasi, on_tag=on_tag)
