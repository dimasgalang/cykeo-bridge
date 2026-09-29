"""Launcher untuk menjalankan bridge di interpreter embeddable.

Kenapa file ini ada
------------------
Interpreter embeddable Python sudah membawa `python312._pth`, dan begitu
file itu ada, Python berjalan dalam *isolated mode*: `sys.path` dibangun
HANYA dari isi _pth. `cwd`, `PYTHONPATH`, dan `PYTHONHOME` semuanya
DIABAIKAN.

Akibatnya perintah `python -u -m cykeo_bridge run` GAGAL dengan
"No module named cykeo_bridge" walaupun foldernya jelas ada di install
dir - persis yang terjadi di produksi (29 Sep 2026, bridge restart 100x
lebih, runtime terpakai dengan benar tapi module tidak pernah ketemu).

Entrypoint `python -m` bergantung pada sys.path, tapi `python <path>` tidak:
skrip yang diberi sebagai argumen DIEKSEKUSI langsung. Jadi launcher ini
dijalankan lewat path eksplisit, lalu ia sendiri yang menambahkan folder
install ke sys.path sebelum memanggil module sebagai __main__.

Dengan cara ini kita tidak bergantung pada isi _pth sama sekali - dan
perbaikan di _pth (lihat cykeo_bridge/runtime.py) tetap berguna untuk
pemanggilan lain yang juga memakai -m.

Pakai:
    python.exe -u cykeo_bridge\\bridge_launcher.py run
"""
from __future__ import annotations

import os
import runpy
import sys

# Folder install = parent dari folder package cykeo_bridge.
_INSTALL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ensure_importable() -> str:
    """Pastikan package cykeo_bridge bisa di-import. Return install dir."""
    if _INSTALL_DIR not in sys.path:
        # Sisipkan di depan supaya versi lokal install yang menang,
        # bukan module dengan nama sama yang mungkin ada di stdlib zip.
        sys.path.insert(0, _INSTALL_DIR)
    return _INSTALL_DIR


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    install_dir = ensure_importable()

    # Kalau dipanggil tanpa argumen, default-nya 'run' supaya
    # `bridge_launcher.py` saja tetap berguna.
    if not args:
        args = ["run"]

    # Pesan jelas kalau package-nya memang tidak lengkap - jangan biarkan
    # muncul sebagai ModuleNotFoundError yang membingungkan.
    #
    # Perhatikan: HANYA cek_is_dir TIDAK berguna di sini, karena launcher ini
    # sendiri berada di dalam folder cykeo_bridge\, jadi foldernya selalu
    # ada. Yang membedakan "paket utuh" dari "salinannya terpotong" adalah
    # file __init__.py + __main__.py - sama seperti sanity check di
    # install_cykeo.ps1.
    missing = [
        name
        for name in ("__init__.py", "__main__.py")
        if not os.path.isfile(os.path.join(install_dir, "cykeo_bridge", name))
    ]
    if missing:
        sys.stderr.write(
            "[cykeo-launcher] package cykeo_bridge tidak lengkap di %s "
            "(%s hilang)\n" % (install_dir, ", ".join(missing))
        )
        return 2

    sys.argv = ["cykeo_bridge"] + args
    try:
        runpy.run_module("cykeo_bridge", run_name="__main__", alter_sys=True)
    except SystemExit as e:
        return int(e.code or 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
