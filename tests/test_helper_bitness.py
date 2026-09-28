"""Regresi bitness: helper WAJIB 32-bit (win-x86).

Temuan: ``GReaderApi.dll`` production adalah PE32 (32-bit). Kalau helper
dibangun x64, Windows akan menolak load dengan ``BadImageFormatException``,
smoke test di PC Test akan gagal, dan tidak ada yang tahu kenapa.

Test ini mengunci keputusan itu supaya tidak balik ke x64 tanpa sengaja.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

import pytest

BRIDGE_ROOT = Path(__file__).resolve().parent.parent
CSPROJ = BRIDGE_ROOT / "helper" / "CykeoHelper.csproj"
ADAPTER = BRIDGE_ROOT / "cykeo_bridge" / "devices" / "cykeo_helper.py"
SDK_DLL = Path(
    "/home/hermes/workspace/cykeo-docs/rev/prod_full/GReaderApi.dll"
)


def _pe_machine(path: Path) -> int | None:
    """Baca field Machine dari header PE. None kalau bukan PE."""
    try:
        with path.open("rb") as fh:
            if fh.read(2) != b"MZ":
                return None
            fh.seek(0x3C)
            pe_off = struct.unpack("<I", fh.read(4))[0]
            fh.seek(pe_off)
            if fh.read(4) != b"PE\0\0":
                return None
            machine = struct.unpack("<H", fh.read(2))[0]
    except OSError:
        return None
    return machine


#: IMAGE_FILE_MACHINE_I386 = 0x014C ; IMAGE_FILE_MACHINE_AMD64 = 0x8664
MACHINE_I386 = 0x014C
MACHINE_AMD64 = 0x8664


def test_csproj_targets_win_x86() -> None:
    xml = CSPROJ.read_text()
    assert '<RuntimeIdentifier>win-x86</RuntimeIdentifier>' in xml
    assert '<PlatformTarget>x86</PlatformTarget>' in xml
    # Hanya tag XML yang dicek; komentar dokumentasi boleh menyebut x64.
    tags = re.findall(r"<(\w+)>([^<]*)</\1>", xml)
    for name, value in tags:
        if name in {"RuntimeIdentifier", "PlatformTarget"}:
            assert "64" not in value, f"{name} masih 64-bit: {value}"
    assert re.search(r"<Prefer32Bit>\s*true\s*</Prefer32Bit>", xml)


def test_adapter_looks_for_win_x86_output() -> None:
    src = ADAPTER.read_text()
    assert "win-x86" in src
    assert "win-x64" not in src


def test_sdk_dll_is_32bit() -> None:
    """Pastikan asumsi kita benar: SDK production memang PE32."""
    if not SDK_DLL.is_file():
        pytest.skip("SDK production tidak ada di mesin ini")
    machine = _pe_machine(SDK_DLL)
    assert machine == MACHINE_I386, (
        f"GReaderApi.dll ternyata bukan 32-bit (machine={machine:#x}). "
        "Kalau vendor ganti ke x64, helper harus dibangun ulang x64 juga."
    )


def test_built_helper_matches_sdk_bitness() -> None:
    """Kalau helper sudah di-build, bitness-nya harus sama dengan SDK."""
    built = (
        BRIDGE_ROOT / "helper" / "bin" / "Release" / "net48" / "win-x86"
        / "cykeo-helper.exe"
    )
    if not built.is_file():
        pytest.skip("helper belum di-build di mesin ini")

    machine = _pe_machine(built)
    assert machine == MACHINE_I386, (
        f"helper di-build {machine:#x}; harus 0x{MACHINE_I386:04x} (x86)"
    )
