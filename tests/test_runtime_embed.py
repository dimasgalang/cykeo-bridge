"""Regression test untuk runtime Python mandiri (bridge tanpa Python sistem).

Bug yang dicegah: paket bridge butuh interpreter Python tapi paket TIDAK
membawanya, jadi installer hanya MENYATAKAN "install Python 3 dan tick
Add Python to PATH". Di lapangan itu berarti tray restart 100x lebih tanpa
sekali pun membaca tag. Test ini mengunci: runtime ikut dikemas, dan
layoutnya diperiksa ketat.

Test di sini murni stdlib supaya bisa jalan di host yang tidak punya
dependensi apa pun - persis seperti kondisi yang kami cegah.
"""
from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cykeo_bridge import runtime  # noqa: E402


def _fake_runtime(base: Path, *, with_zip=True, with_pth=True, site_commented=True):
    """Buat folder yang MENIRU layout python-embed, tanpa exe sungguhan.

    Kita tidak butuh exe asli: yang diuji adalah keputusan/koreksi layout,
    bukan menjalankan Windows.
    """
    d = base / runtime.RUNTIME_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    (d / "python.exe").write_bytes(b"MZ" + b"\x00" * 64)
    if with_zip:
        (d / runtime.STDLIB_ZIP).write_bytes(b"PK\x03\x04")
    if with_pth:
        text = "python312.zip\n.\n\n# Uncomment to run site.main() automatically\n#import site\n"
        if not site_commented:
            text = "python312.zip\n.\nimport site\n"
        (d / runtime.PTH_NAME).write_text(text, encoding="utf-8")
    return d


# --------------------------------------------------------------- looks_like_runtime


def test_runtime_utuh_diterima(tmp_path):
    d = _fake_runtime(tmp_path)
    assert runtime.looks_like_runtime(d) is True


def test_tanpa_stdlib_zip_ditolak(tmp_path):
    """python.exe saja tidak cukup - ini yang bikin pesan error membingungkan."""
    d = _fake_runtime(tmp_path, with_zip=False)
    assert runtime.looks_like_runtime(d) is False


def test_tanpa_pth_ditolak(tmp_path):
    d = _fake_runtime(tmp_path, with_pth=False)
    assert runtime.looks_like_runtime(d) is False


def test_folder_tidak_ada_ditolak(tmp_path):
    assert runtime.looks_like_runtime(tmp_path / "nope") is False
    assert runtime.looks_like_runtime(tmp_path) is False


# --------------------------------------------------------------------- prepare


def test_prepare_mengaktifkan_import_site(tmp_path):
    """Ini inti fix: build embeddable resmi meng-comment `import site`."""
    d = _fake_runtime(tmp_path, site_commented=True)
    pth = d / runtime.PTH_NAME

    assert "import site" not in pth.read_text(encoding="utf-8").replace("#import site", "")
    assert runtime.prepare(d) is True

    text = pth.read_text(encoding="utf-8")
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    assert "import site" in lines, "import site harus aktif"
    assert "#import site" not in lines, "baris komentar harus hilang"


def test_prepare_idempoten(tmp_path):
    """Jalankan dua kali: hasil kedua harus sama, dan tetap benar."""
    d = _fake_runtime(tmp_path)
    assert runtime.prepare(d) is True
    first = (d / runtime.PTH_NAME).read_text(encoding="utf-8")

    assert runtime.prepare(d) is True
    second = (d / runtime.PTH_NAME).read_text(encoding="utf-8")

    assert first == second, "prepare harus idempoten"
    assert second.count("import site") == 1, "tidak boleh dobel"


def test_prepare_tidak_mengganggu_isi_penting(tmp_path):
    """Baris path di _pth harus tetap utuh - menghapusnya mematikan import."""
    d = _fake_runtime(tmp_path)
    runtime.prepare(d)
    text = (d / runtime.PTH_NAME).read_text(encoding="utf-8")
    assert "python312.zip" in text
    assert any(l.strip() == "." for l in text.splitlines())


def test_prepare_gagal_tanpa_pth(tmp_path):
    d = _fake_runtime(tmp_path, with_pth=False)
    assert runtime.prepare(d) is False


# ------------------------------------------------------------------ build_command


def test_build_command_urutan_benar(tmp_path):
    """Harus [python.exe, -u, <launcher>, ...] - BUKAN -m.

    -m bergantung sys.path, dan isolated mode membuat sys.path tidak
    memuat folder install. Lihat test_isolated_mode_pth.py.
    """
    cmd = runtime.build_command(tmp_path, ["cykeo_bridge", "run"])
    assert cmd[0].endswith("python.exe")
    assert "python-embed" in cmd[0]
    assert cmd[1] == "-u"
    assert cmd[2].endswith("bridge_launcher.py")
    assert cmd[3:] == ["run"]


def test_build_command_tidak_pernah_memakai_flag_m(tmp_path):
    """Penjaga: kalau suatu saat ada yang memunculkan -m lagi, test ini
    harus gagal. Jalur -m itulah yang rusak di produksi."""
    cmd = runtime.build_command(tmp_path, ["cykeo_bridge", "run"])
    assert "-m" not in cmd


def test_build_command_untuk_test_reader(tmp_path):
    cmd = runtime.build_command(tmp_path, ["cykeo_bridge", "test-reader", "--duration", "15"])
    assert cmd[3:] == ["test-reader", "--duration", "15"]


# --------------------------------------------------------------------- self_check


def test_self_check_gagal_karena_exe_palsu(tmp_path):
    """Guard: fake exe kita bukan interpreter, jadi self_check HARUS False.

    Ini yang memastikan self_check benar-benar menjalankan interpreter,
    bukan sekadar return True. Kalau test ini gagal, self_check-nya
    cuma hiasan dan installer akan percaya diri pada paket rusak.
    """
    d = _fake_runtime(tmp_path)
    assert runtime.self_check(d) is False


def test_self_check_gagal_karena_exe_tidak_ada(tmp_path):
    d = _fake_runtime(tmp_path)
    (d / "python.exe").unlink()
    assert runtime.self_check(d) is False


# ------------------------------------------------------------------ integritas paket


def test_runtime_dir_di_dalam_install_dir(tmp_path):
    """Runtime harus di dalam folder install, bukan temp/PATH."""
    d = runtime.runtime_dir(tmp_path)
    assert d.parent == tmp_path
    assert d.name == "python-embed"


def test_bridge_pure_stdlib_sehingga_runtime_embed_cukup():
    """Justifikasi seluruh desain: kalau bridge butuh pip, runtime embed gagal."""
    import ast

    pkg = Path(__file__).resolve().parents[1] / "cykeo_bridge"
    local_modules = {p.stem for p in pkg.glob("*.py")}
    local_modules |= {p.stem for p in (pkg / "devices").glob("*.py")}

    third_party = []
    for py in list(pkg.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relatif = import internal
                    continue
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for n in names:
                if not n or n in sys.stdlib_module_names or n in local_modules:
                    continue
                third_party.append((py.name, n))

    assert third_party == [], (
        "Bridge harus PURE STDLIB supaya runtime embeddable cukup. "
        "Kalau muncul dependensi baru, paket harus membawa installer pip: %s" % third_party
    )
