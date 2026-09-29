"""Regression test untuk bug 'isolated mode' pada runtime embeddable.

Bug yang dicegah (terdeteksi dari log produksi 29 Sep 2026):
    python: memakai runtime mandiri ...\\python-embed\\python.exe
    bridge dijalankan via python (pid 13708)
    [err] ...python-embed\\python.exe: No module named cykeo_bridge

Runtime-nya sendiri BERJALAN. Yang salah adalah `import`: saat file
`python312._pth` ada, Python berjalan dalam isolated mode - `sys.path`
dibangun HANYA dari isi _pth. `cwd`, `PYTHONPATH`, dan `PYTHONHOME`
DIABAIKAN. Padahal tray menjalankan `python -u -m cykeo_bridge run` dengan
cwd = folder install, dan package-nya ada di sana.

Dua lapis pertahanan diuji di sini:
  1. `..` di _pth  -> folder install masuk sys.path
  2. bridge_launcher.py -> jalan lewat path, bukan -m, jadi tidak
     bergantung pada sys.path sama sekali

Test murni stdlib supaya bisa jalan di host tanpa dependensi apa pun.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cykeo_bridge import runtime  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO / "cykeo_bridge" / "bridge_launcher.py"


def _pth_text(d: Path) -> str:
    return (d / runtime.PTH_NAME).read_text(encoding="utf-8")


def _build_install(tmp_path: Path, pth_body: str) -> Path:
    """Layout persis seperti PC: install/ berisi python-embed/ + package."""
    install = tmp_path / "install"
    rt = install / runtime.RUNTIME_DIRNAME
    rt.mkdir(parents=True, exist_ok=True)
    (rt / runtime.STDLIB_ZIP).write_bytes(b"PK\x03\x04")
    (rt / runtime.PTH_NAME).write_text(pth_body, encoding="utf-8")

    pkg = install / "cykeo_bridge"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text('MARKER = "package-bridge"\n', encoding="utf-8")
    (pkg / "__main__.py").write_text(
        "from cykeo_bridge import MARKER\n"
        "import json, ssl, sqlite3, ctypes, struct, socket, urllib.request\n"
        "print('cykeo_bridge OK', MARKER)\n",
        encoding="utf-8",
    )
    return install


# ----------------------------------------------------------------- entri '..'


def test_prepare_menambahkan_entri_parent(tmp_path):
    """WAJIB ada '..' supaya folder install masuk sys.path."""
    d = tmp_path / runtime.RUNTIME_DIRNAME
    d.mkdir(parents=True)
    (d / runtime.PTH_NAME).write_text("python312.zip\n.\n", encoding="utf-8")

    assert runtime.prepare(d) is True

    entries = [ln.strip() for ln in _pth_text(d).splitlines()]
    assert runtime.INSTALL_DIR_ENTRY in entries, (
        "prepare() harus menambahkan '..' ke _pth. Tanpa itu, isolated mode "
        "membuang folder install dari sys.path dan bridge gagal dengan "
        "'No module named cykeo_bridge'."
    )


def test_prepare_masih_mengaktifkan_import_site(tmp_path):
    """'..' dan 'import site' dua-duanya wajib."""
    d = tmp_path / runtime.RUNTIME_DIRNAME
    d.mkdir(parents=True)
    (d / runtime.PTH_NAME).write_text(
        "python312.zip\n.\n\n# Uncomment to run site.main() automatically\n#import site\n",
        encoding="utf-8",
    )

    assert runtime.prepare(d) is True

    text = _pth_text(d)
    assert "import site" in text
    assert "#import site" not in text


def test_prepare_idempoten_tidak_duplikasi_parent(tmp_path):
    """Jalankan dua kali: '..' jangan sampai dobel."""
    d = tmp_path / runtime.RUNTIME_DIRNAME
    d.mkdir(parents=True)
    (d / runtime.PTH_NAME).write_text("python312.zip\n.\n", encoding="utf-8")

    assert runtime.prepare(d) is True
    assert runtime.prepare(d) is True

    count = [ln.strip() for ln in _pth_text(d).splitlines()].count("..")
    assert count == 1, "'..' appeared %d times, harus tepat 1" % count


def test_prepare_gagal_karena_pth_tidak_ada(tmp_path):
    d = tmp_path / runtime.RUNTIME_DIRNAME
    d.mkdir(parents=True)
    assert runtime.prepare(d) is False


# ------------------------------------------------------- launcher (lapis 2)


def test_launcher_ada_dan_bersih():
    """Launcher wajib ada - tanpa itu, -m tetap rapuh."""
    assert LAUNCHER.is_file(), (
        "cykeo_bridge/bridge_launcher.py hilang. Ini lapisan kedua yang "
        "menjalankan module lewat path sehingga tidak bergantung sys.path."
    )
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "runpy" in text
    assert "sys.path.insert" in text


def test_launcher_tidak_import_dependensi_liar():
    """Launcher harus tetap pure stdlib supaya jalan di runtime embed."""
    import ast

    tree = ast.parse(LAUNCHER.read_text(encoding="utf-8"), filename=str(LAUNCHER))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and not node.level:
            mods.add((node.module or "").split(".")[0])
    outside = {m for m in mods if m and m not in sys.stdlib_module_names}
    assert outside == set(), "launcher pakai dependensi non-stdlib: %s" % outside


def test_launcher_menolak_package_yang_hilang(tmp_path):
    """Package absen -> harus keluar dengan kode != 0 dan pesan jelas.

    Menjalankan launcher langsung di host normal (bukan embed) tetap
    sah karena launcher tidak bergantung _pth.
    """
    fake = tmp_path / "install" / "cykeo_bridge"
    fake.mkdir(parents=True)
    launcher = fake / "bridge_launcher.py"
    launcher.write_text(
        LAUNCHER.read_text(encoding="utf-8"), encoding="utf-8"
    )
    # Buang isi package supaya launcher melihat folder ada tapi tidak bisa jalan
    (fake / "__init__.py").write_text("MARKER='x'\n", encoding="utf-8")
    (fake / "__main__.py").write_text("print('should not run')\n", encoding="utf-8")
    (fake / "__init__.py").unlink()

    out = subprocess.run(
        [sys.executable, "-u", str(launcher), "run"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 2, out.stderr
    assert "tidak lengkap" in out.stderr.lower()


def test_launcher_menjalankan_module_sebagai_main(tmp_path):
    """Launcher harus benar-benar jalan sampai module dieksekusi."""
    install = tmp_path / "install"
    pkg = install / "cykeo_bridge"
    pkg.mkdir(parents=True)
    launcher = pkg / "bridge_launcher.py"
    launcher.write_text(LAUNCHER.read_text(encoding="utf-8"), encoding="utf-8")
    (pkg / "__init__.py").write_text('MARKER = "package-bridge"\n', encoding="utf-8")
    (pkg / "__main__.py").write_text(
        "from cykeo_bridge import MARKER\nprint('cykeo_bridge OK', MARKER)\n",
        encoding="utf-8",
    )

    out = subprocess.run(
        [sys.executable, "-u", str(launcher), "run"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert "cykeo_bridge OK package-bridge" in out.stdout


# -------------------------------------------- reproduksi falha yang sebenarnya


@pytest.mark.skipif(
    not (REPO / "cykeo_bridge" / "runtime.py").is_file(),
    reason="butuh repo checkout",
)
def test_isolated_mode_menyirnakan_module(tmp_path):
    """Dokumentasi phenotype: _pth tanpa '..' = module tidak ketemu.

    Test ini tidak memakai interpreter embeddable asli (tidak ada di
    host Linux). Yang dikunci adalah KEPUTUSAN prepare(): harus ada
    entri yang menunjuk ke folder install. Kalau prepare() nanti diubah
    dan entri itu dihapus, test di atas akan gagal - dan ini yang pernah
    terjadi di produksi.
    """
    d = tmp_path / runtime.RUNTIME_DIRNAME
    d.mkdir(parents=True)
    (d / runtime.PTH_NAME).write_text("python312.zip\n.\n", encoding="utf-8")
    runtime.prepare(d)
    entries = [ln.strip() for ln in _pth_text(d).splitlines()]
    assert entries[-2:] == ["..", "import site"]
