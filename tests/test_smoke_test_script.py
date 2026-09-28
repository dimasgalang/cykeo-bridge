"""Test logika ``smoke_test_helper.py`` (script yang dikirim ke PC Test).

Script tujuan tidak bisa dijalankan di Linux karena butuh Windows + helper
.exe asli. Test ini memverifikasi bagian yang bisa diuji: parsing balasan
JSON, deteksi masalah, dan pelaporan — memakai helper palsu yang bicara
protokol stdio JSON yang sama persis dengan ``helper/Program.cs``.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "installer" / "smoke_test_helper.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("smoke_mod", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


smoke = _load_script()


# --- helper palsu ---------------------------------------------------------- #
def _fake_helper(ports, connect_ok=True):
    """Buat executable palsu yang bicara protokol persis seperti Program.cs."""
    body = f'''#!{sys.executable}
import json, sys
print(json.dumps({{"type": "hello", "helper": "cykeo-helper", "version": "1.0.0"}}), flush=True)
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    cmd, rid = req.get("cmd"), req.get("id")
    if cmd == "ping":
        print(json.dumps({{"id": rid, "ok": True, "result": {{}}}}), flush=True)
    elif cmd == "detect_ports":
        # Bentuk PERSIS seperti helper/Program.cs: array of {{"port": ...}}
        _pl = [{{"port": p}} for p in {ports!r}]
        print(json.dumps({{"id": rid, "ok": True, "result": {{"ports": _pl}}}}), flush=True)
    elif cmd == "connect":
        if {connect_ok!r} and {ports!r}:
            print(json.dumps({{"id": rid, "ok": True, "result": {{"connected": True}}}}), flush=True)
        else:
            print(json.dumps({{"id": rid, "ok": False, "error": "open_failed", "message": "gagal membuka COM"}}), flush=True)
    elif cmd == "shutdown":
        print(json.dumps({{"id": rid, "ok": True, "result": {{"bye": True}}}}), flush=True)
'''
    return body


def _install_fake_helper(tmp_path, ports=("COM3",), connect_ok=True):
    """Siapkan folder yang terlihat seperti paket install Windows."""
    for fname in ("GReaderApi.dll", "Newtonsoft.Json.dll"):
        (tmp_path / fname).write_bytes(b"MZ")
    fake = tmp_path / "cykeo-helper.exe"
    fake.write_text(_fake_helper(ports, connect_ok))
    fake.chmod(0o755)
    return fake


# --- test ------------------------------------------------------------------ #
def test_required_files_check_passes(tmp_path) -> None:
    _install_fake_helper(tmp_path)
    smoke.HELPER = tmp_path / "cykeo-helper.exe"
    smoke.HERE = tmp_path
    assert smoke.check_files() == 0


def test_required_files_check_fails_when_missing(tmp_path) -> None:
    smoke.HERE = tmp_path  # folder kosong
    assert smoke.check_files() == 1


def test_platform_check_blocks_non_windows(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert smoke.check_platform() == 1


def test_platform_check_passes_on_windows(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert smoke.check_platform() == 0


def test_run_helper_parses_replies(tmp_path) -> None:
    exe = _install_fake_helper(tmp_path, ports=("COM3", "COM7"))
    smoke.HELPER = exe
    rc, replies = smoke.run_helper(None)
    assert rc == 0
    ids = {r.get("id") for r in replies}
    assert {1, 2, 99} <= ids  # ping, detect_ports, shutdown
    assert any(r.get("type") == "hello" for r in replies)


def test_report_passes_all_when_everything_ok(tmp_path, capsys) -> None:
    replies = [
        {"type": "hello", "helper": "cykeo-helper", "version": "1.0.0"},
        {"id": 1, "ok": True},
        {"id": 2, "ok": True, "result": {"ports": [{"port": "COM3"}]}},
        {"id": 3, "ok": True, "result": {"connected": True}},
    ]
    assert smoke.report(replies, "COM3") == 0
    out = capsys.readouterr().out
    assert "LOLOS SEMUA PEMERIKSAAN" in out
    assert "COM3" in out


def test_report_flags_missing_handshake(capsys) -> None:
    replies = [{"id": 1, "ok": True}]
    assert smoke.report(replies, None) == 1
    assert "handshake gagal" in capsys.readouterr().out


def test_report_flags_no_ports(capsys) -> None:
    replies = [
        {"type": "hello", "helper": "cykeo-helper", "version": "1.0.0"},
        {"id": 1, "ok": True},
        {"id": 2, "ok": True, "result": {"ports": []}},
    ]
    rc = smoke.report(replies, "COM3")
    assert rc == 1
    assert "tidak ada di daftar" in capsys.readouterr().out


def test_report_flags_failed_connect(capsys) -> None:
    replies = [
        {"type": "hello", "helper": "cykeo-helper", "version": "1.0.0"},
        {"id": 1, "ok": True},
        {"id": 2, "ok": True, "result": {"ports": [{"port": "COM3"}]}},
        {"id": 3, "ok": False, "error": "open_failed"},
    ]
    rc = smoke.report(replies, "COM3")
    assert rc == 1
    assert "koneksi reader COM3 gagal" in capsys.readouterr().out


def test_report_flags_ping_failure(capsys) -> None:
    replies = [
        {"type": "hello", "helper": "cykeo-helper", "version": "1.0.0"},
        {"id": 1, "ok": False, "error": "boom"},
        {"id": 2, "ok": True, "result": {"ports": [{"port": "COM3"}]}},
    ]
    assert smoke.report(replies, None) == 1
    assert "ping gagal" in capsys.readouterr().out
