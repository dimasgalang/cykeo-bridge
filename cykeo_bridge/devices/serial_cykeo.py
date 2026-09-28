"""Adapter device Cykeo CK-D5 (Windows) via native GReader SDK.

STATUS IMPLEMENTASI — baca dulu sebelum dipakai
===============================================
Semua signature & konstanta di file ini diambil dari **dokumen resmi Cykeo**,
bukan tebakan:

* ``https://www.cykeorfid.com/wp-content/uploads/2024/12/RFID-Reader-Development-Guide-C_C_windows.pdf``
  (Quick Start + §3 Connection Description + §4 Events Description)
* ``https://www.cykeorfid.com/wp-content/uploads/2024/12/RFID-Demo-Software-Operation-Manual.pdf``
  (Quick Start — identik)
* Metadata assembly ``GReaderApi.dll`` (dari installer SML FlexiAudit 2.4.3) untuk
  nama enum & nama field struct.

Ringkas temuan (detail: ``cykeo-docs/FASE4-FINDINGS.md``):

1. **Nama DLL yang bisa dipanggil ctypes adalah ``GReader.dll``** (lib native yang
   direferensikan ``#pragma comment(lib, "GReader")`` di contoh panduan).
   ``GReaderApi.dll`` yang ikut dalam paket SML adalah **wrapper .NET terkelola**
   (import ``mscoree.dll``, namespace ``GDotnet.Reader.Api.*``) dan **tidak bisa**
   dipanggil lewat ctypes.
2. **``initParam`` BUKAN license key per-device.** Nilai contoh
   ``E5DC18EDBEEE6EAD954D7332E6D90361`` muncul identik di kedua dokumen resmi
   sebagai konstanta program contoh. Konfirmasi teknisi SML juga menyatakan tidak
   ada license key 32 hex. Jadi `initParam` = parameter handshake protokol.
3. **Baud default = 115200**; port COM harus autodetect (dokumen hanya contoh
   ``COM12:115200``).
4. Semua fungsi native ``__stdcall`` → di 32-bit wajib ``WINFUNCTYPE``.

Yang MASIH belum bisa diverifikasi tanpa hardware/native header:
  * nilai numerik enum ``EMESS_*`` / ``E*LogMid`` (dll .NET ter-obfuscate, nama
    enum-nya diacak). Nama-namanya sudah benar, angka)nya belum.
  * layout byte persis ``LogBaseEpcInfo`` (butuh header vendor ``message.h``).
  * nomor port COM pada unit sebenarnya.

Karena itu adapter memakai :class:`_OpaqueEpcInfo` — ia hanya menebak frame sekali
dengan aman lalu **tidak boleh** dianggap produksi sampai dicocokkan dengan header.
"""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import struct
import threading
import time
from ctypes import CFUNCTYPE, POINTER, c_char_p, c_int, c_void_p
from typing import Any, Dict, List, Optional

from .base import DeviceAdapter, DeviceError, RawTag, utc_now_iso

__all__ = [
    "CykeoSerialDevice",
    "sdk_available",
    "default_dll_candidates",
    "DEVICE_UNAVAILABLE_REASON",
    "INIT_PARAM_DEFAULT",
    "SUPPORTED_BAUDRATES",
]

logger = logging.getLogger("cykeo_bridge.device.cykeo")

#: Alasan kenapa adapter tidak bisa jalan di mesin ini (untuk pesan wizard).
DEVICE_UNAVAILABLE_REASON: Optional[str] = None

# --------------------------------------------------------------------------- #
# Nilai dari dokumen resmi (lihat FASE4-FINDINGS.md)
# --------------------------------------------------------------------------- #

#: ``initParam`` = parameter inisialisasi protokol. Nilai ini adalah konstanta
#: contoh yang dipakai di Quick Start kedua dokumen resmi Cykeo — BUKAN license
#: key per-device. Bisa dioverride lewat config/env kalau unit Anda memakai nilai
#: lain, tapi default-nya nilai resmi.
INIT_PARAM_DEFAULT = "E5DC18EDBEEE6EAD954D7332E6D90361"

#: Nilai enum ``eBaudrate`` dari metadata GReaderApi.dll + contoh guide.
SUPPORTED_BAUDRATES = (9600, 19200, 115200, 200000, 230400, 460800, 921600)

#: Nama event log (dari ``eBaseLogMid`` di metadata dll + §4 guide).
EVENT_TAG_EPC_LOG = "Epc"
EVENT_TAG_EPC_OVER = "EpcOver"

# --------------------------------------------------------------------------- #
# Konstanta enum — WAJIB DICOCOKKAN dengan header vendor sebelum produksi
# --------------------------------------------------------------------------- #
# Nama-nama ini BENAR (bersumber guide + metadata dll). ANGKA-ANGKANYA belum
# terverifikasi: dll .NET yang kita punya ter-obfuscate sehingga tabel Constant
# tidak bisa dibaca. JANGAN pakai nilai ini di produksi tanpa header asli.
EMESS_BASE_STOP = 0x10
EMESS_BASE_INVENTORY_EPC = 0x50
E_TAG_EPC_LOG = 0x01
E_TAG_EPC_OVER = 0x02

#: AntennaNo_1 — dari quick start panduan (``msg.AntennaEnable = AntennaNo_1``).
ANTENNA_NO_1 = 0x01

#: Frame ``LogBaseEpcInfo``: 4 string fixed + 4 int. Ukuran string masih TEBAKAN.
_EPC_BUF = 256
_TID_BUF = 64
_USERDATA_BUF = 128
_RESERVED_BUF = 64

#: Margin aman untuk mendeteksi frame yang lebih besar dari tebakan kita.
_STRUCT_GUARD = 64


class _OpaqueEpcInfo(ctypes.Structure):
    """Frame ``LogBaseEpcInfo`` versi "opaque + defensif".

    Kita tidak tahu layout persis struct aslinya, tapi guide menyatakan callback
    menerima struct **by value** (bukan pointer). Daripada menebak offset dengan
    ``ctypes.Structure`` lalu men-crash reader, objek ini memperlakukan seluruh
    frame sebagai buffer mentah dan menyalin field yang bisa dibaca dengan aman.
    """

    _fields_ = [("raw", ctypes.c_byte * (_EPC_BUF + _TID_BUF + _USERDATA_BUF
                                         + _RESERVED_BUF + 4 * 4 + _STRUCT_GUARD))]


def _safe_cstr(buf: bytes) -> str:
    """Decode C-string dari buffer, potong di NUL pertama, abaikan sisa."""
    nul = buf.find(b"\x00")
    if nul >= 0:
        buf = buf[:nul]
    return buf.decode("ascii", errors="ignore").strip()


def _find_hex(buf: bytes) -> str:
    """Ambil run karakter hex terpanjang dari dalam frame (fallback parse EPC).

    Dipakai karena offset field ``LogBaseEpcInfo`` belum terverifikasi (butuh
    header vendor). Kita pecah frame menjadi run karakter hex yang dipisahkan
    byte non-hex, lalu ambil yang terpanjang.

    PENTING: string device (EPC/TID) selalu NUL-terminated, jadi byte ``0x00``
    adalah pemisah field yang sah. Tanpa itu, padding nol byte sebelum TID bisa
    menyambung dengan digit TID dan menghasilkan run ganjil yang salah.
    """
    runs: List[str] = []
    cur = ""
    for b in buf:
        c = chr(b)
        if c in "0123456789ABCDEFabcdef":
            cur += c
        else:
            if cur:
                runs.append(cur)
            cur = ""
    if cur:
        runs.append(cur)
    hexish = [r for r in runs if len(r) >= 8 and len(r) % 2 == 0]
    return max(hexish, key=len) if hexish else ""


def default_dll_candidates() -> List[str]:
    """Lokasi DLL yang searched (urutan prioritas)."""
    base = os.environ.get("RFID_BRIDGE_SDK_DIR", "")
    names = ["GReader.dll", "GReaderApi.dll"]
    candidates: List[str] = []
    if base:
        candidates += [os.path.join(base, n) for n in names]
    app_root = os.environ.get("ProgramFiles", r"C:\Program Files")
    appdata = os.environ.get("ProgramData", r"C:\ProgramData")
    for root in (app_root, appdata, os.getcwd()):
        candidates += [os.path.join(root, "CykeoBridge", "sdk", n) for n in names]
        candidates += [os.path.join(root, "GReader", n) for n in names]
    candidates += names  # andalkan PATH / DLL search default Windows
    seen = set()
    return [c for c in candidates if not (c in seen or seen.add(c))]


def sdk_available(dll_path: Optional[str] = None) -> tuple[bool, str]:
    """Cek apakah SDK bisa dimuat. Returns ``(available, reason)``."""
    if platform.system() != "Windows":
        return False, (f"SDK Cykeo (GReader.dll) hanya untuk Windows; platform ini "
                       f"{platform.system()}. Gunakan mode simulator untuk uji coba.")
    if os.name != "nt":
        return False, "os.name bukan 'nt' — ctypes.WinDLL tidak tersedia."
    candidates = [dll_path] if dll_path else default_dll_candidates()
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return True, f"SDK ditemukan: {candidate}"
    return False, ("GReader.dll tidak ditemukan. Salin DLL SDK Cykeo (Overland/Cykeo) "
                   "ke folder sdk/ atau set RFID_BRIDGE_SDK_DIR. Sementara itu pakai "
                   "mode simulasi.")


def _make_callback_type() -> Any:
    """Tipe callback stdcall sesuai guide (``void __stdcall TagEpcLog(...)``)."""
    if os.name == "nt" and ctypes.sizeof(c_void_p) == 4:  # noqa: PLR2004
        return ctypes.WINFUNCTYPE(None, c_char_p, POINTER(_OpaqueEpcInfo))  # type: ignore[attr-defined]
    return CFUNCTYPE(None, c_char_p, POINTER(_OpaqueEpcInfo))


class CykeoSerialDevice(DeviceAdapter):
    """Baca tag dari Cykeo CK-D5 via ``GReader.dll`` (READ-ONLY).

    Args:
        com_port: mis. ``COM12``.
        baudrate: default 115200 (enum ``eBaudrate`` + contoh guide).
        init_param: parameter handshake protokol; default = konstanta resmi.
        dll_path: path eksplisit ``GReader.dll`` (opsional).
        antenna: bitmask antenna, default antenna 1 saja.
        connect_timeout: detik konfirmasi koneksi (contoh guide ``3``).
    """

    name = "cykeo"

    def __init__(self, com_port: str, baudrate: int = 115200,
                 init_param: str = INIT_PARAM_DEFAULT, *, dll_path: Optional[str] = None,
                 antenna: int = ANTENNA_NO_1, connect_timeout: int = 3,
                 sdk_dir: Optional[str] = None) -> None:
        self.com_port = (com_port or "").strip()
        self.baudrate = int(baudrate or 115200)
        self.init_param = (init_param or INIT_PARAM_DEFAULT).strip()
        self.dll_path = dll_path
        self.antenna = int(antenna)
        self.connect_timeout = int(connect_timeout)
        if sdk_dir:
            os.environ.setdefault("RFID_BRIDGE_SDK_DIR", sdk_dir)

        self._dll = None
        self._client = None
        self._buffer: List[RawTag] = []
        self._closed = False
        self._cb_refs: List[Any] = []
        self._lock = threading.Lock()
        self._dll_loaded_from: str = ""
        self._inventory_sent = False

        if not self.com_port:
            raise DeviceError("com_port wajib diisi untuk mode cykeo (mis. COM12)")
        if self.baudrate not in SUPPORTED_BAUDRATES:
            logger.warning("baudrate %s di luar daftar eBaudrate; tetap dicoba",
                           self.baudrate)
        self._load_dll()

    # ------------------------------------------------------------------ #
    @property
    def connection_string(self) -> str:
        """``COM12:115200`` (guide §3.1)."""
        return f"{self.com_port}:{self.baudrate}"

    def _load_dll(self) -> None:
        available, reason = sdk_available(self.dll_path)
        if not available:
            raise DeviceError(f"SDK Cykeo tidak bisa dipakai: {reason}")
        candidates = [self.dll_path] if self.dll_path else default_dll_candidates()
        loader = ctypes.WinDLL  # type: ignore[attr-defined]
        last_error: Optional[Exception] = None
        for candidate in candidates:
            if not candidate or not os.path.exists(candidate):
                continue
            try:
                self._dll = loader(candidate)
                self._dll_loaded_from = candidate
                break
            except OSError as exc:  # pragma: no cover - hanya di Windows
                last_error = exc
        if self._dll is None:  # pragma: no cover
            raise DeviceError(f"Gagal memuat GReader.dll: {last_error or reason}")
        self._bind_symbols()
        logger.info("SDK Cykeo dimuat dari %s", self._dll_loaded_from)

    def _bind_symbols(self) -> None:  # pragma: no cover - butuh Windows + DLL
        dll = self._dll
        dll.CreateRS232.restype = c_void_p
        dll.CreateRS232.argtypes = [c_char_p, c_char_p, c_int, c_int]
        dll.Close.restype = c_int
        dll.Close.argtypes = [c_void_p]
        dll.RegCallBack.restype = c_int
        dll.RegCallBack.argtypes = [c_void_p, c_int, c_void_p]
        if hasattr(dll, "SendSynMsg"):
            dll.SendSynMsg.restype = c_int
            dll.SendSynMsg.argtypes = [c_void_p, c_int, c_void_p]

    # ------------------------------------------------------------------ #
    def connect(self) -> None:  # pragma: no cover - butuh Windows + hardware
        """Buka koneksi RS232 (READ-ONLY).

        Urutan WAJIB mengikuti Quick Start guide: ``CreateRS232`` dulu, baru
        ``RegCallBack``. Membalik urutan ini menghilangkan callback, karena
        ``CreateRS232`` menginisialisasi ulang objek client.
        """
        if self._client is not None:
            return
        if self._dll is None:
            raise DeviceError("DLL belum dimuat")
        handle = self._dll.CreateRS232(
            self.init_param.encode("ascii"),
            self.connection_string.encode("ascii"),
            self.connect_timeout,
            0,  # InitInventory (nilai enum belum terverifikasi -> 0 = default)
        )
        if not handle:
            raise DeviceError(
                f"Gagal membuka {self.connection_string}. Cek: kabel RS232, nomor port COM "
                "benar (port bisa autodetect), dan reader menyala."
            )
        self._client = c_void_p(handle)
        self._register_callbacks()
        logger.info("Terhubung ke Cykeo di %s", self.connection_string)

    def _register_callbacks(self) -> None:  # pragma: no cover
        """Daftarkan callback. WAJIB dipanggil SETELAH ``CreateRS232``."""
        ctype = _make_callback_type()
        cb_log = ctype(self._on_tag)
        cb_over = ctype(self._on_tag_over)
        self._cb_refs += [cb_log, cb_over]
        self._dll.RegCallBack(self._client, E_TAG_EPC_LOG, ctypes.cast(cb_log, c_void_p))
        self._dll.RegCallBack(self._client, E_TAG_EPC_OVER, ctypes.cast(cb_over, c_void_p))

    def start_inventory(self) -> None:  # pragma: no cover
        """Kirim EMESS_BaseInventoryEpc. Ini READ, tidak menulis konfigurasi.

        Quick Start mengirim ``MsgBaseStop`` dulu lalu ``MsgBaseInventoryEpc``.
        Karena layout msg belum diketahui, kita kirim frame zero-filled; SDK
        memakai default reader. Lihat blocker di FASE4-FINDINGS.md §8.
        """
        if self._dll is None or self._client is None:
            raise DeviceError("Belum terhubung")
        if not hasattr(self._dll, "SendSynMsg"):
            logger.warning("SendSynMsg tidak diekspor DLL; inventory dijalankan SDK-internal")
            return
        frame = _OpaqueEpcInfo()
        self._dll.SendSynMsg(self._client, EMESS_BASE_STOP, ctypes.byref(frame))
        self._dll.SendSynMsg(self._client, EMESS_BASE_INVENTORY_EPC, ctypes.byref(frame))
        self._inventory_sent = True
        logger.info("Inventory EPC dikirim (READ-ONLY)")

    def _stop_inventory(self) -> None:  # pragma: no cover
        if (self._dll is not None and self._client is not None
                and self._inventory_sent and hasattr(self._dll, "SendSynMsg")):
            self._dll.SendSynMsg(self._client, EMESS_BASE_STOP,
                                 ctypes.byref(_OpaqueEpcInfo()))

    # ------------------------------------------------------------------ #
    def _on_tag(self, reader_name, info_ptr) -> None:  # pragma: no cover - callback SDK
        """Callback dipanggil thread DLL — JANGAN blocking di sini (panduan tegas)."""
        try:
            info = self._coerce_info(info_ptr)
            if info is None:
                return
            raw = bytes(info.raw)
            #<Result == 0 berarti sukses; tanpa offset pasti kita	andalkan
            #heuristik: cari hex run. Bila tidak ketemu, abaikan (read-only, aman).
            epc = _find_hex(raw)
            if not epc:
                return
            self._buffer.append(RawTag(
                epc=epc,
                antenna=None,
                rssi=None,
                timestamp=utc_now_iso(),
                reader_name=reader_name.decode("ascii", errors="ignore") if reader_name else None,
                raw={"frame_len": len(raw)},
            ))
            if len(self._buffer) > 10_000:
                with self._lock:
                    del self._buffer[:-5_000]
        except Exception:  # noqa: BLE001 - callback tidak boleh melempar ke DLL
            logger.exception("cykeo: error di callback tag")

    def _on_tag_over(self, reader_name, _msg) -> None:  # pragma: no cover
        logger.debug("cykeo: tag report over (%s)",
                     reader_name.decode("ascii", errors="ignore") if reader_name else "-")

    def _coerce_info(self, info_ptr) -> Optional[_OpaqueEpcInfo]:  # pragma: no cover
        if info_ptr is None:
            return None
        if isinstance(info_ptr, _OpaqueEpcInfo):
            return info_ptr
        try:
            return ctypes.cast(info_ptr, POINTER(_OpaqueEpcInfo)).contents
        except (ctypes.ArgumentError, ValueError):
            return None

    # ------------------------------------------------------------------ #
    def read_tags(self) -> List[RawTag]:
        """Ambil tag yang buffered SDK (non-blocking)."""
        if self._closed:
            raise DeviceError("Adapter sudah ditutup")
        if self._client is None:
            self.connect()
        with self._lock:
            out, self._buffer = self._buffer, []
        return out

    def flush_buffer(self) -> List[RawTag]:
        with self._lock:
            out, self._buffer = self._buffer, []
        return out

    def close(self) -> None:  # pragma: no cover - butuh Windows
        if self._closed:
            return
        self._closed = True
        try:
            self._stop_inventory()
        except Exception:  # noqa: BLE001
            logger.debug("cykeo: stop inventory gagal saat close", exc_info=True)
        if self._dll is not None and self._client is not None:
            try:
                self._dll.Close(self._client)
            except Exception:  # noqa: BLE001
                logger.debug("cykeo: Close() gagal", exc_info=True)
        self._client = None
        self._cb_refs = []

    def describe(self) -> Dict[str, Any]:
        return {
            "adapter": self.name,
            "port": self.connection_string,
            "dll": self._dll_loaded_from or "(belum dimuat)",
            "antenna": self.antenna,
            "connected": self._client is not None,
            "buffered": len(self._buffer),
        }

    def __repr__(self) -> str:
        return (f"CykeoSerialDevice(port={self.connection_string!r}, "
                f"connected={self._client is not None})")


# Helper kecil supaya bisa diimpor di Linux tanpa efek samping.
def wait_for_tags(device: CykeoSerialDevice, seconds: float = 1.0,
                  poll: float = 0.1) -> List[RawTag]:  # pragma: no cover
    """Kumpulkan tag selama ``seconds`` (dipakai manual saat commissioning)."""
    deadline = time.monotonic() + seconds
    collected: List[RawTag] = []
    while time.monotonic() < deadline:
        collected.extend(device.read_tags())
        time.sleep(poll)
    return collected
