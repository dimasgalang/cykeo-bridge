"""Single-owner lock untuk port reader (anti perebutan COM3).

Kenapa perlu
------------
Historically beberapa proses bisa membuka COM yang sama dalam hitungan detik
setelah boot: tray agent, bridge worker, dan `test-reader` (Reader Test).
Pemenang tabrakan_start dapat handle, sisanya dapat
``Access to the port 'COM3' is denied.`` Gejalanya menyesatkan: UI terlihat
sehat, session scan terbentuk, tapi 0 EPC masuk karena proses yang benar
justru yang kalah.

Aturan
------
* Satu lock per (com_port) - reader berbeda tidak saling mengunci.
* Lock berbasis file dengan OS-level exclusive lock, jadi lock ikut hilang
  kalau proses mati (tidak ada lock yatim setelah crash).
* Metadata pemilik disimpan di file TERPISAH dari file lock. Ini wajib:
  di Windows, byte-range lock dari ``msvcrt.locking`` juga memblokir
  pembacaan file itu, jadi kalau metadata disimpan di file yang sama,
  proses kedua tidak bisa membaca ``pid`` pemilik dan tidak bisa membedakan
  "lock hidup" dari "lock)yatim".
* Pelanggar tidak boleh mematikan pemilik yang sehat: dia hanya melapor dan
  keluar dengan exit code supaya supervisor tahu ini kondisi normal
  (sudah ada instance lain), bukan crash.
* ``--force-takeover`` tidak dipakai di produksi. Kalau memang perlu
  takeover, operator harus stop instance lama lebih dulu.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

__all__ = ["ReaderPortLock", "LockHeld", "EXIT_ALREADY_RUNNING"]

#: Exit code khusus "sudah ada instance lain yang memegang port".
EXIT_ALREADY_RUNNING = 3


def _lock_dir() -> Path:
    """Folder tempat file lock disimpan (per-user, selalu writable)."""
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_RUNTIME_DIR") or os.path.expanduser("~")
    d = Path(base) / "CykeoRfidBridge" / "locks"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        d = Path(os.path.expanduser("~")) / ".cykeo_locks"
        d.mkdir(parents=True, exist_ok=True)
    return d


def _normalize_com(com_port: Optional[str]) -> str:
    """``COM3`` / ``com3`` / ``COM 3`` -> ``COM3`` (kunci lock konsisten)."""
    raw = (com_port or "").strip().upper().replace(" ", "")
    if not raw:
        return "UNSPECIFIED"
    if raw.startswith("COM"):
        digits = raw[3:]
        return f"COM{int(digits)}" if digits.isdigit() else raw
    return raw if raw.isalpha() else f"COM{raw}"


class LockHeld(Exception):
    """Lock port sudah dipegang proses lain."""

    def __init__(self, info: Dict[str, Any]):
        self.info = info
        pid = info.get("pid", "?")
        since = info.get("since", "?")
        super().__init__(
            f"Port reader sudah dipakai proses lain (pid={pid}, sejak {since}). "
            "Bridge tidak dijalankan ulang - agar tidak merebut port dari "
            "instance yang sehat."
        )


class ReaderPortLock:
    """Lock eksklusif per port COM, dilepas otomatis saat proses mati."""

    def __init__(self, com_port: Optional[str], *, label: str = "cykeo-bridge"):
        self.com = _normalize_com(com_port)
        self.label = label
        # File lock HANYA per port - label sengaja tidak dipakai di nama file.
        # Kalau label ikut, "bridge" dan "test-reader" akan mengunci file
        # berbeda untuk COM yang sama, jadi perebutan port tetap terjadi.
        d = _lock_dir()
        self.path = d / f"{self.com}.lock"
        # Metadata terpisah: file yang di-lock tidak boleh dibaca proses lain.
        self.meta_path = d / f"{self.com}.owner.json"
        self._fh = None

    # -- internals ------------------------------------------------------- #
    def _read_holder(self) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(self.meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if sys.platform.startswith("win"):
            # Buka handle dengan akses query-info saja: bukti proses masih ada.
            try:
                import ctypes

                handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
                if not handle:
                    return False
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            except Exception:
                return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _acquire_os_lock(self) -> bool:
        """Exclusive lock level OS; ikut hilang kalau proses mati.

        Catatan Windows: ``msvcrt.locking`` mengunci N byte mulai dari posisi
        file saat ini, jadi file harus sudah berisi >= 1 byte. File lock yang
        baru dibuat masih 0 byte, jadi kita tulis satu byte placeholder lebih
        dulu, baru kunci posisinya. Tanpa ini, ``msvcrt.locking`` gagal dengan
        error yang menyesatkan, bukan ``LockHeld`` yang bersih.
        """
        try:
            if not self.path.exists() or self.path.stat().st_size == 0:
                self.path.write_text(" ", encoding="utf-8")
            fh = open(self.path, "r+", encoding="utf-8")
        except OSError:
            return False

        try:
            if sys.platform.startswith("win"):
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            try:
                fh.close()
            except OSError:
                pass
            return False
        self._fh = fh
        return True

    def _write_holder(self) -> None:
        """Catat pemilik lock di file metadata terpisah.

        Gagal menulis TIDAK boleh menggagalkan lock: lock level OS sudah
        dimiliki sebelum fungsi ini dipanggil, jadi metadata murni informasi
        tambahan untuk pesan error.
        """
        payload = {
            "pid": os.getpid(),
            "com": self.com,
            "label": self.label,
            "since": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        try:
            self.meta_path.write_text(json.dumps(payload), encoding="utf-8")
        except (OSError, ValueError):
            return

    def _clear_if_stale(self) -> bool:
        """Bersihkan metadata pemilik yang prosesnya sudah tidak ada.

        Return True kalau metadata pemilik lama sudah dibersihkan.
        """
        holder = self._read_holder()
        if not holder:
            return False
        pid = int(holder.get("pid") or 0)
        if pid and pid != os.getpid() and not self._pid_alive(pid):
            try:
                self.meta_path.unlink()
            except OSError:
                pass
            return True
        return False

    # -- public API ------------------------------------------------------ #
    def acquire(self, *, wait_seconds: float = 0.0) -> None:
        """Ambil lock; lempar :class:`LockHeld` kalau dipegang proses hidup."""
        deadline = time.monotonic() + max(0.0, wait_seconds)

        while True:
            self._clear_if_stale()
            if self._acquire_os_lock():
                self._write_holder()
                return

            # Lock level OS gagal. Baca metadata untuk membedakan dua kondisi:
            # owner masih hidup (tabrakan sungguhan) atau owner sudah mati
            # tapi file lock belum bisa diambil (mis. OS belum melepas).
            # Keduanya sama-sama harus dihormati; yang beda cuma pesan yang
            # dilaporkan ke operator.
            holder = self._read_holder() or {}
            pid = int(holder.get("pid") or 0)
            owner_alive = bool(pid) and self._pid_alive(pid) and pid != os.getpid()

            if time.monotonic() >= deadline:
                raise LockHeld(
                    {
                        "pid": pid or holder.get("pid"),
                        "since": holder.get("since"),
                        "com": holder.get("com", self.com),
                    }
                )

            # Owner hidup = tabrakan sungguhan: tunggu kalo operator minta
            # wait_seconds, kalau tidak langsung laporkan. Owner sudah mati =
            # lock level OS akan terlepas sendiri, jadi tunggu dan coba lagi.
            time.sleep(0.25 if owner_alive else 0.1)

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.close()
        except OSError:
            pass
        finally:
            self._fh = None
        try:
            self.path.unlink()
        except OSError:
            pass
        try:
            self.meta_path.unlink()
        except OSError:
            pass

    def __enter__(self) -> "ReaderPortLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.release()

    def describe(self) -> Dict[str, Any]:
        holder = self._read_holder()
        return {
            "path": str(self.path),
            "meta": str(self.meta_path),
            "com": self.com,
            "held_by": holder.get("pid") if holder else None,
        }
