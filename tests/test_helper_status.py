"""Test ``helper_status`` / ``resolve_helper_path`` di devices.factory.

Fungsi ini yang dipakai wizard + ``cykeo_bridge check`` untuk memberi pesan
yang jelas kalau helper .NET atau SDK vendor belum siap.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from cykeo_bridge.devices import factory

HELPER_REQUIRED = (
    "cykeo-helper.exe",
    "GReaderApi.dll",
    "Newtonsoft.Json.dll",
)


def _make_complete_helper(folder: Path) -> Path:
    """Buat folder helper yang lengkap (semua file wajib ada)."""
    folder.mkdir(parents=True, exist_ok=True)
    for fname in HELPER_REQUIRED:
        (folder / fname).write_bytes(b"MZ-fake")
    return folder / "cykeo-helper.exe"


def test_required_files_list_is_complete() -> None:
    assert factory.HELPER_REQUIRED_FILES == HELPER_REQUIRED


def test_resolve_helper_path_uses_explicit_override(tmp_path) -> None:
    exe = _make_complete_helper(tmp_path / "custom")
    got = factory.resolve_helper_path(str(exe))
    assert got == exe


def test_resolve_helper_path_matches_adapter_candidates(monkeypatch) -> None:
    """Factory dan adapter harusGX berada dipath yang sama.

    Ini guarding agar tidak ada dua daftar lokasi helper yang berbeda.
    """
    from cykeo_bridge.devices import cykeo_helper

    monkeypatch.setattr(cykeo_helper, "_locate_helper", lambda *_a, **_k: None)
    assert factory.resolve_helper_path() is None

    # give it a real path and make sure factory just passes through
    monkeypatch.setattr(cykeo_helper, "_locate_helper", lambda *_a, **_k: "/tmp/h.exe")
    assert str(factory.resolve_helper_path()) == "/tmp/h.exe"


def test_resolve_helper_path_returns_none_when_absent(tmp_path, monkeypatch) -> None:
    from cykeo_bridge.devices import cykeo_helper

    monkeypatch.setenv("CYKEO_HELPER_PATH", str(tmp_path / "tidak-ada.exe"))
    monkeypatch.setattr(cykeo_helper, "_locate_helper", lambda *_a, **_k: None)
    assert factory.resolve_helper_path() is None


class TestHelperStatus:
    def test_missing_everything_reports_helper_not_found(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("CYKEO_HELPER_PATH", str(tmp_path / "x.exe"))
        monkeypatch.setattr(
            factory, "resolve_helper_path", lambda *_a, **_k: None
        )
        st = factory.helper_status()
        assert st["available"] is False
        assert st["helper"] is None
        assert "cykeo-helper.exe" in st["missing"]
        assert "install_cykeo.ps1" in st["reason"]

    def test_partial_install_lists_missing_files(self, tmp_path, monkeypatch) -> None:
        """Hanya exe + GReaderApi.dll -> harus sebut yang kurang."""
        folder = tmp_path / "h"
        folder.mkdir()
        (folder / "cykeo-helper.exe").write_bytes(b"MZ")
        (folder / "GReaderApi.dll").write_bytes(b"MZ")
        monkeypatch.setattr(factory, "resolve_helper_path", lambda *_a, **_k: folder / "cykeo-helper.exe")

        st = factory.helper_status()
        assert st["available"] is False
        assert "Newtonsoft.Json.dll" in st["missing"]
        assert "belum lengkap" in st["reason"]

    def test_complete_install_on_windows_is_available(self, tmp_path, monkeypatch) -> None:
        folder = tmp_path / "h"
        exe = _make_complete_helper(folder)
        monkeypatch.setattr(factory, "resolve_helper_path", lambda *_a, **_k: exe)
        monkeypatch.setattr(factory.sys, "platform", "win32")

        st = factory.helper_status()
        assert st["available"] is True
        assert st["missing"] == []
        assert st["helper"] == str(exe)
        assert "siap" in st["reason"]

    def test_non_windows_blocks_even_when_files_exist(self, tmp_path, monkeypatch) -> None:
        """File lengkap di Linux tetap TIDAK bisa dipakai."""
        folder = tmp_path / "h"
        exe = _make_complete_helper(folder)
        monkeypatch.setattr(factory, "resolve_helper_path", lambda *_a, **_k: exe)
        monkeypatch.setattr(factory.sys, "platform", "linux")

        st = factory.helper_status()
        assert st["available"] is False
        assert "Windows" in st["reason"]

    def test_explicit_sdk_dir_overrides(self, tmp_path, monkeypatch) -> None:
        """sdk_dir eksplisit dipakai untuk cek GReaderApi.dll."""
        sdk = tmp_path / "vendor"
        sdk.mkdir()
        (sdk / "GReaderApi.dll").write_bytes(b"MZ")

        folder = tmp_path / "h"
        exe = _make_complete_helper(folder)
        monkeypatch.setattr(factory, "resolve_helper_path", lambda *_a, **_k: exe)
        monkeypatch.setattr(factory.sys, "platform", "win32")

        st = factory.helper_status(sdk_dir=str(sdk))
        assert st["sdk_dir"] == str(sdk)
        assert st["available"] is True

    def test_bad_sdk_dir_reported(self, tmp_path, monkeypatch) -> None:
        folder = tmp_path / "h"
        exe = _make_complete_helper(folder)
        monkeypatch.setattr(factory, "resolve_helper_path", lambda *_a, **_k: exe)
        monkeypatch.setattr(factory.sys, "platform", "win32")

        st = factory.helper_status(sdk_dir=str(tmp_path / "kosong"))
        assert st["available"] is False
        assert any("GReaderApi.dll" in m for m in st["missing"])


def test_greader_dll_config_is_not_required(tmp_path, monkeypatch) -> None:
    """Regresi: ``GReaderApi.dll.config`` TIDAK ada di production MSI.

    Hanya ``cykeo-helper.exe.config`` yang dipakai. Jadi ``helper_status``
    tidak boleh mewajibkan file config milik DLL vendor.
    """
    assert "GReaderApi.dll.config" not in factory.HELPER_REQUIRED_FILES

    folder = tmp_path / "h"
    exe = _make_complete_helper(folder)
    assert not (folder / "GReaderApi.dll.config").exists()
    monkeypatch.setattr(factory, "resolve_helper_path", lambda *_a, **_k: exe)
    monkeypatch.setattr(factory.sys, "platform", "win32")

    st = factory.helper_status()
    assert st["available"] is True, st
