"""Smoke test helper Cykeo CK-D5 tanpa server - jalankan di PC scanner.

Dipakai untuk membuktikan helper .NET benar-benar jalan di Windows SEBELUM
bridge di-install penuh. Menguji protokol stdio JSON yang sama dengan yang
dipakai bridge production:

    hello -> ping -> detect_ports -> connect (opsional) -> shutdown

Pemakaian:
    python smoke_test_helper.py            # tanpa koneksi reader
    python smoke_test_helper.py COM3       # plus coba konek COM3

Exit code:
    0 = helper hidup (lolos semua pemeriksaan wajib)
    1 = ada kegagalan (pesan diagnosis dicetak)
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HELPER = HERE / "cykeo-helper.exe"
REQUIRED = ("cykeo-helper.exe", "GReaderApi.dll", "Newtonsoft.Json.dll")
TIMEOUT_S = 120  # connect production 60s; beri kelonggaran


def _fail(msg: str) -> int:
    print(f"[X] {msg}")
    return 1


def check_files() -> int:
    print("[1/4] Cek file wajib ...")
    missing = [f for f in REQUIRED if not (HERE / f).is_file()]
    if missing:
        return _fail(f"File hilang: {', '.join(missing)}")
    print(f"      OK: {', '.join(REQUIRED)}")
    return 0


def check_platform() -> int:
    print("[2/4] Cek platform ...")
    if not sys.platform.startswith("win"):
        print("      [!] OS ini bukan Windows - helper hanya jalan di Windows.")
        print("          Script ini harus dijalankan di PC scanner, bukan di server.")
        return 1
    print("      OK: Windows")
    return 0


def run_helper(com_port):
    """Jalankan helper dengan sekuens request, kembalikan (exit, balasan)."""
    requests = [
        {"id": 1, "cmd": "ping"},
        {"id": 2, "cmd": "detect_ports"},
    ]
    if com_port:
        requests.append(
            {"id": 3, "cmd": "connect", "com": com_port,
             "baud": 115200, "timeout": 60}
        )
    requests.append({"id": 99, "cmd": "shutdown"})

    payload = "\n".join(json.dumps(r) for r in requests) + "\n"

    print("[3/4] Jalankan helper lewat stdio JSON ...")
    try:
        proc = subprocess.run(
            [str(HELPER)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
        )
    except FileNotFoundError:
        return _fail("cykeo-helper.exe tidak bisa dieksekusi."), []
    except subprocess.TimeoutExpired:
        return _fail(
            f"Helper tidak merespons dalam {TIMEOUT_S}s. "
            "Kemungkinan .NET Framework 4.8 belum ada atau helper hang."
        ), []

    replies = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            replies.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"      [!] baris non-JSON: {line[:120]}")

    if not replies:
        print("      STDERR:", proc.stderr[:400] if proc.stderr else "(kosong)")
        _fail("Helper tidak mengeluarkan JSON sama sekali.")
        return 1, []

    return 0, replies


def report(replies, com_port) -> int:
    print("[4/4] Nilai hasil ...")
    by_id = {r.get("id"): r for r in replies}

    problems = []

    # -- handshake ---------------------------------------------------------
    hello = next((r for r in replies if r.get("type") == "hello"), None)
    if hello is None:
        problems.append("tidak ada pesan 'hello' (handshake gagal)")
    else:
        print(f"      OK handshake: {hello.get('helper')} v{hello.get('version')}")

    # -- ping --------------------------------------------------------------
    ping = by_id.get(1, {})
    if not ping.get("ok"):
        problems.append(f"ping gagal: {ping.get('error') or ping}")

    # -- ports -------------------------------------------------------------
    ports_reply = by_id.get(2, {})
    if ports_reply.get("ok"):
        # Balasan helper menaruh daftar port di dalam ``result``. Bentuk datar
        # juga diterima supaya script tidak rapuh kalau helper berubah.
        result = ports_reply.get("result") or {}
        raw_ports = result.get("ports") or ports_reply.get("ports") or []
        # Helper mengirim [{"port": "COM3"}, ...]; bentuk datar juga diterima.
        ports = [
            p.get("port") if isinstance(p, dict) else str(p)
            for p in raw_ports
        ]
        ports = [p for p in ports if p]
        if ports:
            print(f"      Port COM terdeteksi: {', '.join(str(p) for p in ports)}")
        else:
            print("      [!] Tidak ada port COM terdeteksi.")
            print("          Cek: reader menyala? kabel USB terpasang? driver COM?")
            if com_port:
                problems.append(f"port {com_port} tidak ada di daftar")
    else:
        problems.append(f"detect_ports gagal: {ports_reply.get('error')}")

    # -- connect (opsional) ------------------------------------------------
    if com_port:
        conn = by_id.get(3, {})
        if conn.get("ok"):
            print(f"      OK koneksi reader {com_port} (baud 115200)")
        else:
            err = conn.get("error") or conn.get("message") or conn
            print(f"      [X] koneksi {com_port} gagal: {err}")
            problems.append(f"koneksi reader {com_port} gagal")

    print()
    if problems:
        print("=" * 42)
        print("HASIL: ADA MASALAH")
        for pr in problems:
            print(f"  - {pr}")
        print("=" * 42)
        return 1

    print("=" * 42)
    print("HASIL: HELPER LOLOS SEMUA PEMERIKSAAN")
    if not com_port:
        print("Jalankan ulang dengan port untuk tes koneksi:")
        print(f"  python {Path(__file__).name} COM3")
    print("=" * 42)
    return 0


def main() -> int:
    com_port = sys.argv[1] if len(sys.argv) > 1 else None
    for step in (check_files, check_platform):
        rc = step()
        if rc != 0:
            return rc
    rc, replies = run_helper(com_port)
    if rc != 0:
        return rc
    return report(replies, com_port)


if __name__ == "__main__":
    raise SystemExit(main())
