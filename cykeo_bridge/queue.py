"""Antrean lokal persisted (JSONL) — supaya tag tidak hilang saat server offline.

Desain:
- ``append()`` menulis 1 event per baris JSON + ``fsync`` (durable).
- ``peek()`` membaca ulang dari disk (source of truth = file, bukan RAM),
  jadi data tetap ada setelah restart/crash.
- ``commit(n)`` menghapus n baris pertama SETELAH server menerima batch
  (atomic rewrite via file sementara + ``os.replace``).
- ``reject(n)`` memindahkan n baris ke file ``*.rejected.jsonl`` untuk 422
  (payload ditolak server) supaya tidak dikirim ulang tanpa henti.
- Rekor yang rusak (bukan JSON / bukan dict) dilewati dan ditulis ke
  ``*.corrupt.jsonl`` agar tidak memblokir antrean.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = ["PersistentQueue", "QueueStats"]

logger = logging.getLogger("cykeo_bridge.queue")


class QueueStats:
    """Snapshot sederhana jumlah antrean (dipakai log/health)."""

    def __init__(self, pending: int, rejected: int, corrupt: int) -> None:
        self.pending = pending
        self.rejected = rejected
        self.corrupt = corrupt

    def as_dict(self) -> Dict[str, int]:
        return {"pending": self.pending, "rejected": self.rejected, "corrupt": self.corrupt}

    def __repr__(self) -> str:  # pragma: no cover
        return f"QueueStats({self.as_dict()})"


class PersistentQueue:
    """Antrean event JSONL yang tahan restart."""

    def __init__(self, path: os.PathLike | str, *, max_events: int = 100_000) -> None:
        self.path = Path(path)
        self.max_events = max_events
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # path turunan
    # ------------------------------------------------------------------ #
    @property
    def rejected_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".rejected.jsonl")

    @property
    def corrupt_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".corrupt.jsonl")

    # ------------------------------------------------------------------ #
    # tulis
    # ------------------------------------------------------------------ #
    def append(self, events: Iterable[Dict[str, Any]]) -> int:
        """Tambah event ke antrean. Returns jumlah event yang ditulis."""
        batch = [e for e in events if e]
        if not batch:
            return 0
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                for event in batch:
                    fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
                    fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
        logger.debug("queue: +%d event (total sementara %d)", len(batch), self.count())
        return len(batch)

    # alias yang lebih jelas dipakai main loop
    enqueue = append

    # ------------------------------------------------------------------ #
    # baca
    # ------------------------------------------------------------------ #
    def _read_all(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        events: List[Dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    self._quarantine_raw(line)
                    continue
                if isinstance(obj, dict):
                    events.append(obj)
                else:
                    self._quarantine_raw(line)
        return events

    def _quarantine_raw(self, line: str) -> None:
        with self._lock:
            try:
                with self.corrupt_path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:  # pragma: no cover
                logger.error("queue: gagal menyimpan baris korup")
        logger.warning("queue: baris korup dipindah ke %s", self.corrupt_path.name)

    def peek(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Ambil N event pertama TANPA menghapus."""
        events = self._read_all()
        if limit is None:
            return events
        return events[: max(0, limit)]

    def count(self) -> int:
        return len(self._read_all())

    def __len__(self) -> int:
        return self.count()

    # ------------------------------------------------------------------ #
    # hapus / geser
    # ------------------------------------------------------------------ #
    def _rewrite(self, lines: List[str]) -> None:
        """Tulis ulang file antrean secara atomic."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix=self.path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for line in lines:
                    fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise

    def commit(self, n: int) -> int:
        """Hapus n event pertama setelah server MENERIMA batch."""
        if n <= 0:
            return 0
        with self._lock:
            events = self._read_all()
            keep = events[n:]
            removed = len(events) - len(keep)
            if removed <= 0:
                return 0
            self._rewrite([json.dumps(e, ensure_ascii=False, separators=(",", ":")) for e in keep])
            logger.info("queue: %d event di-commit, sisa %d", removed, len(keep))
            return removed

    def reject(self, n: int, reason: str = "") -> int:
        """Pindahkan n event pertama ke file rejected (HTTP 422)."""
        if n <= 0:
            return 0
        with self._lock:
            events = self._read_all()
            head, keep = events[:n], events[n:]
            if not head:
                return 0
            try:
                with self.rejected_path.open("a", encoding="utf-8") as fh:
                    for event in head:
                        fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
                        fh.write("\n")
            except OSError:  # pragma: no cover
                logger.error("queue: gagal menulis file rejected")
                return 0
            self._rewrite([json.dumps(e, ensure_ascii=False, separators=(",", ":")) for e in keep])
            logger.warning("queue: %d event ditolak server (%s) -> %s", len(head), reason or "422",
                           self.rejected_path.name)
            return len(head)

    def clear(self) -> int:
        """Kosongkan antrean (dipakai saat mode simulasi/testing)."""
        with self._lock:
            n = self.count()
            self._rewrite([])
            return n

    def requeue_front(self, events: Iterable[Dict[str, Any]]) -> int:
        """Kembalikan event ke depan antrean (retry gagal, mis. mati mendadak)."""
        batch = [e for e in events if e]
        if not batch:
            return 0
        with self._lock:
            existing = self._read_all()
            merged = batch + existing
            self._rewrite([json.dumps(e, ensure_ascii=False, separators=(",", ":")) for e in merged])
            return len(batch)

    # ------------------------------------------------------------------ #
    # misc
    # ------------------------------------------------------------------ #
    def enforce_max_events(self) -> int:
        """Buang event terlama kalau antrean melebihi ``max_events``.

        Returns jumlah event yang dibuang (0 = tidak ada yang dibuang).
        """
        with self._lock:
            events = self._read_all()
            if len(events) <= self.max_events:
                return 0
            drop = len(events) - self.max_events
            keep = events[drop:]
            self._rewrite([json.dumps(e, ensure_ascii=False, separators=(",", ":")) for e in keep])
            logger.warning("queue: batas %d event terlampaui, %d event terlama dibuang",
                           self.max_events, drop)
            return drop

    def stats(self) -> QueueStats:
        def _count_lines(p: Path) -> int:
            if not p.exists():
                return 0
            with p.open("r", encoding="utf-8", errors="replace") as fh:
                return sum(1 for line in fh if line.strip())

        return QueueStats(self.count(), _count_lines(self.rejected_path),
                          _count_lines(self.corrupt_path))

    def split_batch(self, limit: int) -> Tuple[List[Dict[str, Any]], bool]:
        """Ambil <= limit event; return (batch, ada_sisa)."""
        events = self.peek(limit)
        remaining = self.count() - len(events)
        return events, remaining > 0
