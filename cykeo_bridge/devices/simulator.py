"""Adapter SIMULASI — menghasilkan tag dari fixture, tanpa hardware.

Dipakai untuk:
- smoke test end-to-end (simulator -> queue -> http) di Linux/CI.
- Mode "simulasi" di wizard ketika DLL Cykeo belum ada (CONTRACT.md §6).

Sumber fixture:
1. File JSON/JSONL (path dari config ``simulator_fixture``).
2. Daftar bawaan (built-in) bila file tidak diberikan.

Format fixture: object ``{"tags": [{"epc": ..., "antenna": ..., "rssi": ...}]}``
atau array of object, atau JSONL satu object per baris.
"""

from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from .base import DeviceAdapter, DeviceError, RawTag, utc_now_iso

__all__ = ["SimulatedDevice", "DEFAULT_FIXTURE_TAGS", "load_fixture_tags"]

PathLike = Union[str, os.PathLike]

#: Fixture bawaan — sengaja campur format (prefix 0X, lowercase, whitespace,
#: spasi di tengah, dan satu tag non-hex) supaya jalur normalisasi ikut teruji.
DEFAULT_FIXTURE_TAGS: List[Dict[str, Any]] = [
    {"epc": "303515C1140E3610A02E4880", "antenna": 1, "rssi": -42},
    {"epc": "0X303515C1140E3610A02E487C", "antenna": 1, "rssi": -51},
    {"epc": "0x303515c1140e3610a02e4882", "antenna": 2, "rssi": -63},
    {"epc": "  303515C1140E3610A02E4884  ", "antenna": 2, "rssi": -38},
    {"epc": "30 35 15 C1 14 0E 36 10 A0 2E 48 86", "antenna": 1, "rssi": -70},
    {"epc": "E2801130200020190FFD019B", "antenna": 3, "rssi": -55},
    # sengaja tidak hex -> ditolak normalisasi (payload tetap dikirim mentah)
    {"epc": "NOT-A-HEX", "antenna": 1, "rssi": -20},
]


def load_fixture_tags(path: PathLike) -> List[Dict[str, Any]]:
    """Baca fixture dari file JSON atau JSONL."""
    target = Path(path)
    if not target.exists():
        raise DeviceError(f"Fixture tidak ditemukan: {target}")
    text = target.read_text(encoding="utf-8").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # fallback: JSONL (satu object per baris)
        tags: List[Dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DeviceError(f"Baris JSONL tidak valid di {target}: {exc}") from exc
            if isinstance(obj, dict):
                tags.append(obj)
        return tags
    if isinstance(data, dict):
        tags = data.get("tags") or data.get("events") or []
    elif isinstance(data, list):
        tags = data
    else:
        raise DeviceError(f"Format fixture tidak dikenali di {target}")
    return [t for t in tags if isinstance(t, dict)]


class SimulatedDevice(DeviceAdapter):
    """Sumber tag realistis dari fixture (loop), tanpa membuka COM sama sekali."""

    name = "simulator"

    def __init__(self, tags: Optional[Sequence[Dict[str, Any]]] = None, *,
                 fixture: Optional[PathLike] = None,
                 loop: bool = True, delay: float = 0.0, rssi_jitter: int = 0,
                 seed: Optional[int] = None, batch_size: Optional[int] = None) -> None:
        self.fixture_path = str(fixture) if fixture else ""
        if fixture:
            self.tags = load_fixture_tags(fixture)
        elif tags is not None:
            self.tags = [dict(t) for t in tags]
        else:
            self.tags = [dict(t) for t in DEFAULT_FIXTURE_TAGS]
        if not self.tags:
            raise DeviceError("Simulator butuh minimal satu tag di fixture")
        self.loop = loop
        self.delay = max(0.0, float(delay))
        self.rssi_jitter = max(0, int(rssi_jitter))
        self.batch_size = batch_size
        self._index = 0
        self._rng = random.Random(seed)
        self._closed = False
        self._emitted = 0

    # ------------------------------------------------------------------ #
    def read_tags(self) -> List[RawTag]:
        """Kembalikan 1 tag (atau ``batch_size`` tag) per panggilan."""
        if self._closed:
            raise DeviceError("Adapter sudah ditutup")
        if self.delay:
            time.sleep(self.delay)
        count = max(1, self.batch_size or 1)
        out: List[RawTag] = []
        for _ in range(count):
            if not self.loop and self._index >= len(self.tags):
                break
            spec = self.tags[self._index % len(self.tags)]
            self._index += 1
            out.append(self._make_tag(spec))
        self._emitted += len(out)
        return out

    def _make_tag(self, spec: Dict[str, Any]) -> RawTag:
        rssi = spec.get("rssi")
        if self.rssi_jitter and isinstance(rssi, (int, float)):
            rssi = int(rssi) + self._rng.randint(-self.rssi_jitter, self.rssi_jitter)
        return RawTag(
            epc=spec.get("epc", ""),
            antenna=spec.get("antenna"),
            rssi=rssi,
            timestamp=spec.get("timestamp") or utc_now_iso(),
            tid=spec.get("tid"),
            pc=spec.get("pc"),
            reader_name="SIMULATOR",
        )

    def flush_buffer(self) -> List[RawTag]:
        return []

    def close(self) -> None:
        self._closed = True

    def describe(self) -> Dict[str, Any]:
        return {
            "adapter": self.name,
            "tags": len(self.tags),
            "emitted": self._emitted,
            "fixture": self.fixture_path or "(built-in)",
        }

    def __repr__(self) -> str:
        return (f"SimulatedDevice(tags={len(self.tags)}, index={self._index}, "
                f"emitted={self._emitted}, closed={self._closed})")
