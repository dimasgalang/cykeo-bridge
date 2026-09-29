"""Runtime Python mandiri untuk Cykeo RFID Bridge.

Bridge ini PURE STDLIB (28 import, nol pihak ketiga - sudah diverifikasi
dengan sys.stdlib_module_names). Jadi kita tidak butuh pip, tidak butuh
virtualenv, dan tidak butuh Python yang sudah terpasang. Satu interpreter
mandiri di dalam folder install cukup.

Kenapa tidak pakai "install Python 3 dan tick Add Python to PATH"?
Karena itu menaruh beban ke user, dan log produksi membuktikan itu gagal:
tray agent restart 100x lebih hanya karena interpreter tidak ketemu di
PATH, padahal paketnya sendiri sehat. Runtime ini membalik tanggung
jawabnya: paket yang membawa interpreter-nya sendiri.

"""
from __future__ import annotations

import os
import sys
from pathlib import Path

RUNTIME_DIRNAME = "python-embed"
STDLIB_ZIP = "python312.zip"
PTH_NAME = "python312._pth"
PTH_FIXED = """python312.zip
.

# Ditambahkan oleh installer Cykeo: aktifkan site agar zipimport + stdlib
# penuh tersedia. Build embeddable resmi meng-comment baris ini, dan
# akibatnya modul yang butuh site.py tidak bisa di-import sama sekali.
import site
"""


def _stderr(msg: str) -> None:
    try:
        sys.stderr.write("[cykeo-runtime] %s\n" % msg)
        sys.stderr.flush()
    except Exception:
        pass


def runtime_dir(install_dir) -> Path:
    """Folder runtime di dalam install dir."""
    return Path(install_dir) / RUNTIME_DIRNAME


def looks_like_runtime(path) -> bool:
    """True kalau folder ini benar-benar interpreter mandiri yang utuh.

    Ceknya sengaja ketat: python.exe saja tidak cukup, karena paket
    embeddable yang terpotong akan gagal dengan pesan yang membingungkan
    ("No module named encodings") jauh dari penyebab sebenarnya.
    """
    p = Path(path)
    if not p.is_dir():
        return False
    if not (p / "python.exe").is_file():
        return False
    if not (p / STDLIB_ZIP).is_file():
        return False
    return (p / PTH_NAME).is_file()


def prepare(python_dir) -> bool:
    """Aktifkan `import site` di _pth. Wajib, kalau tidak stdlib parsial.

    Build embeddable resmi datang dengan `import site` di-comment, dan itu
    bukan sekadar penghematan: tanpa site, modul yang resolve lewat
    site-packages/zipimport akan hilang. Bridge kita butuh import penuh.

    Return True kalau file _pth sudah benar setelah diproses.
    """
    p = Path(python_dir)
    pth = p / PTH_NAME
    if not pth.is_file():
        return False

    try:
        text = pth.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        _stderr("gagal baca %s: %s" % (pth, e))
        return False

    lines = [ln for ln in text.splitlines()]
    # Buang komentar/petunjuk bawaan embeddable, sisakan yang jadi isi _pth.
    kept = []
    for ln in lines:
        s = ln.strip()
        if s.startswith("#import site"):
            continue
        if s.lower().startswith("import site"):
            continue
        kept.append(ln)

    body = "\n".join(ln for ln in kept if ln.strip()) + "\nimport site\n"
    if body == text:
        return True

    # Tulis ke file sementara lalu replace: kalau/listrik mati di tengah,
    # file _pth asli tidak boleh tern salvage jadi setengah jadi.
    tmp = pth.with_suffix(".tmp")
    try:
        tmp.write_text(body, encoding="utf-8")
        os.replace(str(tmp), str(pth))
    except OSError as e:
        _stderr("gagal tulis %s: %s" % (pth, e))
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    return True


def build_command(install_dir, module_args, runtime=None):
    """Susun argv untuk menjalankan bridge: [python.exe, -u, -m, ...]."""
    if runtime is None:
        runtime = runtime_dir(install_dir)
    return [str(Path(runtime) / "python.exe"), "-u", "-m"] + list(module_args)


def self_check(runtime) -> bool:
    """Buktikan interpreter bisa benar-benar jalan, bukan cuma ada.

    Panggil sebagai subprocess: `python.exe -c "import json, ssl, ..."`.
    Ini yang membedakan "ada file python.exe" dari "interpreter hidup".
    """
    import subprocess

    r = Path(runtime)
    exe = r / "python.exe"
    if not exe.is_file():
        return False
    probe = "import json,ssl,sqlite3,ctypes,struct,socket,urllib.request;print('ok')"
    try:
        out = subprocess.run(
            [str(exe), "-c", probe],
            cwd=str(r),
            capture_output=True,
            timeout=60,
        )
    except Exception as e:
        _stderr("self-check gagal: %s" % e)
        return False
    if out.returncode != 0 or b"ok" not in (out.stdout or b""):
        _stderr(
            "self-check gagal: rc=%s err=%s"
            % (out.returncode, (out.stderr or b"").decode("utf-8", "replace")[:300])
        )
        return False
    return True


if __name__ == "__main__":
    # Dipakai installer untuk memverifikasi runtime setelah menyalinnya:
    #   python.exe -c "import cykeo_runtime; ..."
    import sys as _s

    rt = _s.argv[1] if len(_s.argv) > 1 else str(Path(__file__).resolve().parent)
    ok = looks_like_runtime(rt) and prepare(rt) and self_check(rt)
    print("RUNTIME_OK" if ok else "RUNTIME_BAD")
    raise SystemExit(0 if ok else 1)
