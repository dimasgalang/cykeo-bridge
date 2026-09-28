"""Adapter device Cykeo CK-D5 melalui helper .NET (Windows).

STATUS IMPLEMENTASI
===================
Adapter ini adalah jalur **production**. Seluruh nama API, urutan panggilan,
dan nama field diambil dari decompile ``FlexiAudit.Infrastructure.RfidReader.
CykeoReader`` di MSI production SML FlexiAudit (build 27 Agustus 2025),
yang memakai ``GReaderApi.dll`` (.NET terkelola):

    1. new GClient()
    2. client.OpenSerial("<COM>:<baud>", 60, out status)
    3. client.OnEncapedTagEpcLog += handler      (setelah Connect sukses)
    4. SendSynMsg(new MsgBaseGetCapabilities())  -> AntennaCount
    5. SendSynMsg(new MsgBaseInventoryEpc { AntennaEnable,
                                            InventoryMode = 1,
                                            ReadTid = { Mode = 0, Len = 6 } })
    6. handler: logBaseEpcInfo.Result == 0 -> Epc, Rssi, AntId
    7. SendSynMsg(new MsgBaseStop())

Kenapa helper .NET dan bukan ctypes?
------------------------------------
``GReaderApi.dll`` adalah assembly .NET **terkelola** (import mscoree.dll,
namespace ``GDotnet.Reader.Api.*``). ctypes hanya bisa memuat DLL native
PE export, sehingga tidak bisa memanggil kelas managed sama sekali.
Adapter ctypes lama (``serial_cykeo.py`` versi sebelumnya) memakai nama API
native dari guide (``CreateRS232`` / ``RegCallBack``) yang **tidak dipakai
production** — karena itu tidak boleh dipakai sebagai deliverable.

Ringkas: adapter lama tidak dihapus (masih berguna untuk regresi/lab), tapi
default mode ``cykeo`` kini memakai helper .NET.

Protokol helper <-> bridge (stdio JSON, satu request satu baris)
----------------------------------------------------------------
Bridge -> helper : {"id": <int>, "cmd": "...", ...}
Helper -> bridge : {"type": "reply", "id": <int>, "ok": true/false, ...}
Helper -> bridge : {"type": "tag", "epc": "...", "rssi": n, "antId": n, "tid": "..."}
Helper -> bridge : {"type": "log", "level": "...", "message": "..."}

Semua tag yang lolos sudah difilter ``Result == 0`` di sisi helper, sesuai
production. Adapter di sini tidak melakukan filter EPC kedua kali.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

from .base import DeviceAdapter, DeviceError, RawTag, utc_now_iso

__all__ = [
    "CykeoHelperDevice",
    "sdk_available",
    "default_helper_candidates",
    "DEVICE_UNAVAILABLE_REASON",
    "HELPER_EXE_NAME",
    "HELPER_SDK_DLLS",
]

logger = logging.getLogger("cykeo_bridge.device.cykeo_helper")

#: Nama executable helper.
HELPER_EXE_NAME = "cykeo-helper.exe"

#: DLL yang wajib ada di folder helper (dari MSI production).
HELPER_SDK_DLLS = ("GReaderApi.dll", "Newtonsoft.Json.dll")

#: Baud default Cykeo CK-D5 — production memakai string ``<COM>:115200``.
BAUD_DEFAULT = 115200

#: Alasan kenapa helper tidak bisa jalan di mesin ini.
DEVICE_UNAVAILABLE_REASON: Optional[str] = None


def default_helper_candidates() -> List[str]:
    """Kandidat lokasi ``cykeo-helper.exe``, urut prioritas."""
    out: List[str] = []

    env = os.environ.get("CYKEO_HELPER_PATH")
    if env:
        out.append(env)

    appdata = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""
    if appdata:
        out.append(os.path.join(appdata, "CykeoRfidBridge", HELPER_EXE_NAME))

    if appdata:
        out.append(os.path.join(appdata, "CykeoBridge", HELPER_EXE_NAME))

    # Relative ke folder bridge (skema development)
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(os.path.join(here, os.pardir, os.pardir))
    out.append(os.path.join(root, "helper", "bin", "Release", "net48", "win-x86", HELPER_EXE_NAME))
    out.append(os.path.join(root, "dist", "install_cykeo", HELPER_EXE_NAME))

    return [p for p in out if p]


def _locate_helper(explicit: Optional[str] = None) -> Optional[str]:
    """Cari helper yang bisa dijalankan; kembalikan path atau None."""
    candidates: List[str] = []
    if explicit:
        candidates.append(explicit)
    candidates.extend(default_helper_candidates())

    for path in candidates:
        if path and os.path.isfile(path):
            return os.path.abspath(path)
    return None


def _helper_folder_ok(exe_path: str) -> Optional[str]:
    """Pastikan DLL SDK ada di folder helper; kembalikan pesan error bila tidak."""
    folder = os.path.dirname(exe_path)
    missing = [d for d in HELPER_SDK_DLLS if not os.path.isfile(os.path.join(folder, d))]
    if missing:
        return "DLL helper tidak lengkap: {}".format(", ".join(missing))
    return None


def sdk_available(explicit: Optional[str] = None) -> bool:
    """True bila helper .NET tersedia DAN bisa dijalankan di platform ini."""
    if platform.system() != "Windows":
        return False
    exe = _locate_helper(explicit)
    if not exe:
        return False
    return _helper_folder_ok(exe) is None


def unavailable_reason() -> str:
    """Penjelasan singkat kenapa mode cykeo tidak bisa dipakai di mesin ini."""
    if platform.system() != "Windows":
        return "Helper Cykeo hanya berjalan di Windows (platform ini {})".format(platform.system())

    exe = _locate_helper(None)
    if not exe:
        return "{} tidak ditemukan. Jalankan install_cykeo.ps1 lebih dulu.".format(HELPER_EXE_NAME)

    problem = _helper_folder_ok(exe)
    if problem:
        return "{} (di {})".format(problem, os.path.dirname(exe))

    return ""


class CykeoHelperDevice(DeviceAdapter):
    """Adapter CK-D5 lewat helper .NET.

    Mode ini read-only: hanya ``OpenSerial`` + ``StartInventory`` +
    ``MsgBaseStop``. Tidak pernah memanggil SetPower / SetSession / WriteEpc.
    """

    #: Menandai adapter ini butuh helper .NET terpisah.
    requires_helper = True

    def __init__(
        self,
        com_port: str,
        baudrate: int = BAUD_DEFAULT,
        helper_path: Optional[str] = None,
        sdk_dir: Optional[str] = None,
        connect_timeout: int = 60,
        name: str = "cykeo",
    ) -> None:
        self.com_port = com_port
        self.baudrate = int(baudrate or BAUD_DEFAULT)
        self.helper_path = helper_path
        self.sdk_dir = sdk_dir
        self.connect_timeout = int(connect_timeout or 60)
        self.name = name

        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._next_id = 0
        self._stop = threading.Event()
        self._fail_reason: Optional[str] = None
        self._buffer: List[RawTag] = []
        #: id -> {"event": Event, "reply": dict}
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._pending_lock = threading.Lock()

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Jalankan helper .NET."""
        if platform.system() != "Windows":
            raise DeviceError(
                "Helper Cykeo hanya berjalan di Windows; platform ini {}".format(platform.system())
            )

        exe = _locate_helper(self.helper_path)
        if not exe:
            raise DeviceError(
                "{} tidak ditemukan. Jalankan install_cykeo.ps1 lebih dulu.".format(HELPER_EXE_NAME)
            )

        problem = _helper_folder_ok(exe)
        if problem:
            raise DeviceError("{} (di {})".format(problem, os.path.dirname(exe)))

        # Helper mencari DLL lewat current working directory, jadi jalankan
        # dari folder install agar GReaderApi.dll ketemu.
        args = [exe]
        if self.sdk_dir:
            args.append(self.sdk_dir)

        creationflags = 0
        if platform.system() == "Windows":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            self._proc = subprocess.Popen(
                args,
                cwd=os.path.dirname(exe),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise DeviceError("Gagal menjalankan helper: {}".format(exc)) from exc

        self._stop.clear()

        # Baca handshake hello SEBELUM pumpThread start, supaya tidak ada
        # dua pihak yang berebut stdout.
        hello = self._await_hello()
        if not hello:
            try:
                self._proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            self._proc = None
            raise DeviceError("Helper tidak memberi handshake hello")

        self._reader = threading.Thread(
            target=self._pump, name="cykeo-helper-reader", daemon=True
        )
        self._reader.start()

        logger.info("helper .NET aktif: %s", hello.get("sdkDir"))

    def stop(self) -> None:
        """Hentikan inventory lalu tutup helper."""
        self._stop.set()

        if self._proc is None:
            return

        try:
            self._request({"cmd": "disconnect"}, timeout=10.0)
        except Exception:  # noqa: BLE001 - best effort
            pass

        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                self._proc.kill()
            except Exception:  # noqa: BLE001
                pass

        self._proc = None
        self._reader = None

    # -- DeviceAdapter ---------------------------------------------------

    def read_tags(self) -> List[RawTag]:
        """Tag yang sudah terkumpul dari helper (read-only)."""
        with self._lock:
            out = list(self._buffer)
            self._buffer.clear()
        return out

    def start_inventory(self, duration: float = 1.0) -> None:
        """Mulai inventory read-only selama ``duration`` detik.

        Dipanggil worker bridge setelah ``start()``. Inventory dihentikan
        otomatis dengan ``MsgBaseStop`` (meniru production ``StopReading``).
        """
        if self._proc is None:
            self.start()

        self._request(
            {
                "cmd": "start",
                "inventory_mode": 1,
                "tid_len": 6,
                "tid_mode": 0,
            },
            timeout=15.0,
        )

        time.sleep(max(0.0, float(duration)))

        self._request({"cmd": "stop"}, timeout=10.0)

    def list_tags(self, duration: float = 1.0) -> List[RawTag]:
        """Convenience: inventory singkat lalu kembalikan tag yang terbaca."""
        self.start_inventory(duration)
        return self.read_tags()

    def flush_buffer(self) -> List[RawTag]:
        return self.read_tags()

    def close(self) -> None:
        self.stop()

    # -- internal --------------------------------------------------------

    def _pump(self) -> None:
        """SATU-SATUNYA pembaca stdout helper.

        Penting: tidak boleh ada dua pembacaan stdout. Semua reply dan tag
        melewati thread ini, lalu di-route ke antrean reply yang sesuai.
        """
        proc = self._proc
        if proc is None or proc.stdout is None:
            return

        try:
            for raw in proc.stdout:
                if self._stop.is_set():
                    break
                line = raw.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("helper kirim baris non-JSON: %s", line[:200])
                    continue
                self._route(msg)
        except Exception as exc:  # noqa: BLE001
            if not self._stop.is_set():
                self._fail_reason = str(exc)
                logger.warning("helper reader berhenti: %s", exc)
            self._fail_all_pending("helper berhenti: {}".format(exc))

    def _route(self, msg: Dict[str, Any]) -> None:
        """Arahkan pesan: reply -> penunggu id, selain itu -> _dispatch."""
        if msg.get("type") == "reply":
            rid = msg.get("id")
            slot = None
            if rid is not None:
                with self._pending_lock:
                    slot = self._pending.get(rid)
            if slot is not None:
                slot["reply"] = msg
                slot["event"].set()
                return
            # reply tanpa pneumatic (mis. unsolicited) -> abaikan
            return

        self._dispatch(msg)

    def _fail_all_pending(self, reason: str) -> None:
        with self._pending_lock:
            slots = list(self._pending.values())
        for slot in slots:
            if slot.get("reply") is None:
                slot["error"] = reason
                slot["event"].set()

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        mtype = msg.get("type")

        if mtype == "tag":
            epc = (msg.get("epc") or "").strip()
            if not epc:
                return
            raw = RawTag(
                epc=epc,
                rssi=msg.get("rssi"),
                antenna=msg.get("antId"),
                tid=msg.get("tid") or "",
                reader_name=self.name,
                raw={"source": "helper-dotnet"},
            )
            self._emit(raw)
            return

        if mtype == "log":
            level = (msg.get("level") or "info").lower()
            text = msg.get("message") or ""
            getattr(logger, level if level in ("debug", "info", "warning", "error") else "info", logger.info)(
                "helper: %s", text
            )
            return

        if mtype == "reply":
            # Di handled oleh _request lewat reply queue; abaikan di sini.
            return

    def _emit(self, raw: RawTag) -> None:
        """Simpan tag ke buffer internal; dibaca bridge lewat ``read_tags()``."""
        self._buffer.append(raw)

    def _await_hello(self) -> Optional[Dict[str, Any]]:
        deadline = time.time() + 15.0
        while time.time() < deadline:
            proc = self._proc
            if proc is None or proc.stdout is None:
                return None
            line = proc.stdout.readline()
            if not line:
                return None
            try:
                msg = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "hello":
                return msg
        return None

    def _request(self, payload: Dict[str, Any], timeout: float = 10.0) -> Dict[str, Any]:
        """Kirim request ke helper dan tunggu reply dengan id sama.

        Timeout dihitung sejak request dikirim (bukan sejak giliran gilir
        readers), supaya perintah paralel tidak mengunci satu sama lain.
        """
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise DeviceError("helper tidak berjalan")

        with self._lock:
            self._next_id += 1
            req_id = self._next_id
            body = dict(payload)
            body["id"] = req_id

        slot: Dict[str, Any] = {"event": threading.Event(), "reply": None, "error": None}
        with self._pending_lock:
            self._pending[req_id] = slot

        try:
            proc.stdin.write(json.dumps(body, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise DeviceError("helper terputus: {}".format(exc)) from exc

        if not slot["event"].wait(timeout):
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise DeviceError("helper tidak menjawab dalam {} detik".format(timeout))

        with self._pending_lock:
            self._pending.pop(req_id, None)

        if slot.get("error"):
            raise DeviceError(str(slot["error"]))

        reply = slot.get("reply")
        if not isinstance(reply, dict):
            raise DeviceError("helper tidak mengirim reply yang valid")

        if reply.get("ok"):
            return reply

        raise DeviceError(reply.get("message") or reply.get("code") or "helper menolak perintah")
